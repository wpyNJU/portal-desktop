"""Browser GPT-Live relay using client delegation and the shared Being task store.

Live commentary can paraphrase: delivery acknowledgments are never treated as
proof of word-for-word playback, and never advance the existing report cursor.
"""
import asyncio
import base64
import json
import logging
import re
import struct
import time
import uuid

import websockets
from live_config import URL, api_key, clip_bytes, network_proxy, session_config
from voice_usage import numeric, write as write_usage

logger = logging.getLogger('voice.connection')


class SerialBridge:
    """Avoid concurrent Live requests preempting each other in the same Being."""
    def __init__(self, bridge, lock):self.bridge = bridge; self.lock = lock

    async def stream(self, *args, stage=None, **kwargs):
        while True:
            try:
                await asyncio.wait_for(self.lock.acquire(), timeout=1)
                break
            except TimeoutError:
                if stage:stage('queued')
        try:await self.bridge.stream(*args, stage=stage, **kwargs)
        finally:self.lock.release()


class Transcript:
    """Group partial transcripts by provider time, never by packet arrival alone."""
    def __init__(self):self.rows = []; self.sequence = 0

    def append(self, event):
        text = event.get('delta')
        if not isinstance(text, str) or not text:return None, False
        start = event.get('start_ms'); end = event.get('end_ms')
        if not isinstance(start, (float, int)) or not isinstance(end, (float, int)):return None, False
        if end < start:return None, False
        last = self.rows[-1] if self.rows else None
        # Late fragments are retained separately but cannot replace the newest query.
        new = not last or start-last['end'] > 900 or start < last['start']
        if new:
            self.sequence += 1
            last = {'id': str(self.sequence), 'start': start, 'end': end, 'text': ''}
            self.rows.append(last)
            self.rows = self.rows[-100:]
        last['text'] += text; last['end'] = max(last['end'], end)
        return last, new

    def at(self, offset):
        candidates = [row for row in self.rows if row['start'] <= offset+250 and row['end'] >= offset-15000]
        return max(candidates, key=lambda row: row['start']) if candidates else None


def local_action(text):
    value = re.sub(r'[\W_]+', '', text.lower())
    if re.fullmatch(r'(?:请|那|好的|好)?(?:继续|继续报告|继续播报|接着说|接着读|继续读|继续说)(?:吧)?', value):return 'continue'
    if re.fullmatch(r'(?:请)?(?:重播|重播报告|重新读一遍|从头读|从头报告)(?:吧)?', value):return 'replay'
    if re.fullmatch(r'(?:暂时不读|不用读了|不用继续|先不读|稍后再说|停止报告)', value):return 'defer'
    if re.fullmatch(r'(?:再见|拜拜|结束通话|挂电话|挂了吧|先聊到这里)', value):return 'end'
    return None


class LiveSession:
    def __init__(self, ws, client, store, manager, bridge, context, profile, initial, identity):
        self.ws = ws; self.client = client; self.store = store; self.manager = manager
        self.scope = context['scope']; self.context = context; self.profile = profile; self.initial = initial
        self.identity = identity
        if not hasattr(manager, 'live_locks'):manager.live_locks = {}
        self.bridge = SerialBridge(bridge, manager.live_locks.setdefault(self.scope, asyncio.Lock()))
        self.up = None; self.lock = asyncio.Lock(); self.finished = asyncio.Event(); self.ending = False
        self.inputs = Transcript(); self.outputs = Transcript(); self.delegations = set(); self.jobs = set()
        self.pending_acks = {}; self.tracked = {}; self.focus = None; self.reporting = None
        self.generation = 0; self.playing = False; self.muted = False
        self.last_input = 0.; self.last_sound = 0.; self.last_activity = time.monotonic()
        self.output_blocked = False; self.last_packet = 0.; self.silent_samples = 0
        self.seen = set(); self.seen_order = []; self.usage = {}; self.finalized = False

    async def send(self, data):
        data.setdefault('event_id', uuid.uuid4().hex)
        async with self.lock:await self.up.send(json.dumps(data, ensure_ascii=False))

    async def append(self, kind, content, delegation=None):
        key = uuid.uuid4().hex; future = asyncio.get_running_loop().create_future()
        self.pending_acks[key] = future
        try:
            await self.send({'type': kind, 'event_id': key, 'content': clip_bytes(content, 480), 'delegation_id': delegation})
            await asyncio.wait_for(future, 25)
        finally:self.pending_acks.pop(key, None)

    def spawn(self, coroutine):
        job = asyncio.create_task(coroutine); self.jobs.add(job)
        def done(task):
            self.jobs.discard(task)
            if not task.cancelled() and task.exception():
                logger.warning('live_background_failed kind=%s', type(task.exception()).__name__)
        job.add_done_callback(done)

    async def interrupt(self, manual=False):
        self.generation += 1; self.output_blocked = True; self.silent_samples = 0
        self.playing = False; self.last_activity = time.monotonic()
        if self.reporting and self.reporting in self.tracked:
            entry = self.tracked[self.reporting]; entry['paused'] = True
            self.store.update(self.scope, self.reporting, delivery='paused')
        await self.client({'type': 'playback.clear', 'generation': self.generation})
        if manual:
            self.spawn(self.append('session.instructions.append', '用户点击了打断：停止当前播报，等待用户下一句话。不要取消后台查询。'))

    async def delegate(self, event):
        record = event.get('delegation') or {}; delegation = record.get('id')
        if record.get('target') != 'client' or not isinstance(delegation, str) or not delegation or delegation in self.delegations:return
        self.delegations.add(delegation)
        offset = event.get('offset_ms')
        if not isinstance(offset, (int, float)):return
        # Transcription and delegation can arrive in either order.
        row = None
        for _ in range(20):
            await asyncio.sleep(.1); row = self.inputs.at(offset)
            if row and time.monotonic()-self.last_input >= .3:break
        if not row:
            await self.append('session.commentary.append', '没有获取到明确的查询内容，需要用户再说明一下。', delegation);return
        message = row['text'].strip(); action = local_action(message)
        if action in ('continue', 'replay', 'defer'):
            await self.saved(action, delegation);return
        if action == 'end':return
        if not message or len(message) > 12000:
            await self.append('session.commentary.append', '查询内容过长，需要用户明确本次要处理的事项。', delegation);return
        query, cacheable = self.identity({'arguments': json.dumps({'message': message})})
        key, reused = self.manager.submit(self.scope, query, cacheable, message, self.bridge, self.context)
        task = self.store.get(self.scope, key); self.focus = key
        if key not in self.tracked:
            self.tracked[key] = {'delegation': delegation, 'sent': 0, 'shown': 0, 'terminal': False,
                                 'paused': False, 'offered': False, 'input': row['id'], 'notice': False}
        elif self.tracked[key]['input'] != row['id']:
            entry = self.tracked[key]
            entry.update(delegation=delegation, paused=False, offered=True, input=row['id'])
            if task['state'] != 'running' and entry['sent'] >= len(task['text']):entry['sent'] = 0
        await self.client({'type': 'task.updated', 'task': task})
        await self.client({'type': 'agent.status', 'text': ('继续等待原查询：' if reused else '正在查询：')+task['title']})
        await self.append('session.thinking.append', '查询主题：'+task['title']+'。'+('已复用原任务，没有重复提交。' if reused else '已提交 Being，结果未返回；用户可以继续交流。'), delegation)

    async def saved(self, action, delegation=None, key=None):
        key = key or self.focus or self.store.resumable(self.scope)
        if not key:
            await self.append('session.commentary.append', '目前没有待继续的报告。', delegation);return
        try:row = self.store.get(self.scope, key)
        except ValueError:return
        self.focus = key
        if key not in self.tracked:
            self.tracked[key] = {'delegation': delegation, 'sent': row['cursor'], 'shown': 0,
                                 'terminal': False, 'paused': True, 'offered': False, 'input': '', 'notice': False}
        entry = self.tracked[key]
        if delegation:entry['delegation'] = delegation
        if action == 'defer':
            entry['paused'] = True; entry['offered'] = True
            self.store.update(self.scope, key, delivery='deferred');return
        self.store.touch(self.scope, key)
        entry['paused'] = False; entry['offered'] = True; entry['input'] = self.inputs.rows[-1]['id'] if self.inputs.rows else ''
        if action == 'replay' or entry['sent'] >= len(row['text']):entry['sent'] = 0
        self.store.update(self.scope, key, delivery='pending')

    def idle(self):
        now = time.monotonic()
        return not self.playing and now-self.last_input > 1.0 and now-self.last_sound > .8

    async def watch(self):
        labels = {'queued': '正在等待前一项查询或 Agent 回复', 'thinking': '正在分析', 'tool': '正在查询', 'text': '正在返回结果'}
        last_progress = {}; pending_notice = set()
        # Restore once without speaking every historical task or re-submitting it.
        for brief in self.store.watchable(self.scope):
            row = self.store.get(self.scope, brief['id'])
            self.tracked[row['id']] = {'delegation': None, 'sent': row['cursor'], 'shown': 0,
                                      'terminal': False, 'paused': True, 'offered': False, 'input': '', 'notice': False}
        while not self.ending:
            await asyncio.sleep(.15)
            for key in sorted(list(self.tracked), key=lambda k: self.store.get(self.scope, k)['requested_at'], reverse=True):
                entry = self.tracked[key]; row = self.store.get(self.scope, key); text = row['text']
                if len(text) > entry['shown']:
                    offset = entry['shown']
                    await self.client({'type': 'agent.text.delta', 'call_id': key, 'task_id': key,
                                       'delta': text[offset:], 'offset': len(text[:offset].encode('utf-16-le'))//2})
                    entry['shown'] = len(text)
                if row['state'] != 'running' and not entry['terminal']:
                    entry['terminal'] = True
                    await self.client({'type': 'task.updated', 'task': row})
                    await self.client({'type': 'agent.text.done' if row['state'] == 'completed' else 'agent.text.error',
                                       'call_id': key, 'state': row['state'], 'message': row.get('error_message') or '查询结果已保存'})
                progress = self.manager.progress.get(key)
                if progress and progress[0] != last_progress.get(key):
                    last_progress[key] = progress[0]
                    await self.client({'type': 'agent.progress', 'call_id': key,
                                       'text': labels.get(progress[0], '正在处理'), 'elapsed': round(time.time()-row['created'])})
                if not self.idle() or not self.inputs.rows or self.reporting:continue
                if entry['paused'] or (entry['input'] and entry['input'] != self.inputs.rows[-1]['id'] and not entry['offered']):
                    if text and entry['sent'] < len(text) and not entry['offered'] and row['delivery'] != 'deferred' and not pending_notice:
                        self.focus = key; entry['offered'] = True; pending_notice.add(key)
                        entry['paused'] = True
                        self.spawn(self.append('session.commentary.append', '关于“'+clip_bytes(row['title'], 60)+'”的查询已有内容保存，还没报告完，可以询问用户是否继续听。', entry['delegation']))
                    continue
                if len(text) > entry['sent']:
                    remaining = text[entry['sent']:]
                    if row['state'] == 'running' and len(remaining.encode('utf-8')) < 300 and not re.search(r'[。！？!?；;\n]', remaining):continue
                    self.reporting = key
                    self.spawn(self.report(key))
                elif row['state'] != 'running' and not text and not entry['notice']:
                    entry['notice'] = True
                    self.spawn(self.append('session.commentary.append', clip_bytes(row['title'], 60)+'：'+(row.get('error_message') or '未收到查询正文，没有自动重试。'), entry['delegation']))

    async def report(self, key):
        entry = self.tracked[key]; generation = self.generation; start = entry['sent']
        try:
            row = self.store.get(self.scope, key); start = entry['sent']
            piece = clip_bytes(row['text'][start:], 330)
            boundary = max(piece.rfind(mark) for mark in ('。', '！', '？', '\n', ';', '；'))
            if boundary >= len(piece)//2:piece = piece[:boundary+1]
            if not piece:return
            prefix = '关于“'+clip_bytes(row['title'], 45)+'”的查询结果：' if not start else ''
            self.store.update(self.scope, key, delivery='paused')
            before = time.monotonic()
            await self.append('session.commentary.append', prefix+piece, entry['delegation'])
            # Context acceptance != playback receipt. Preserve durable exact-read cursor.
            entry['sent'] = start+len(piece) if generation == self.generation else start
            deadline = time.monotonic()+30
            while time.monotonic() < deadline and generation == self.generation:
                await asyncio.sleep(.1)
                if self.last_sound > before and self.idle():return
            if generation == self.generation:
                entry['paused'] = True
                await self.client({'type': 'agent.status', 'text': '结果已保存，暂未确认播报完成，可说“继续报告”'})
        except (TimeoutError, RuntimeError):
            entry['paused'] = True; entry['sent'] = start
            await self.client({'type': 'agent.status', 'text': '语音结果接收超时，原查询结果仍已保存'})
        finally:
            if generation != self.generation:entry['sent'] = start
            self.reporting = None

    async def provider_event(self, event):
        kind = event.get('type'); event_id = event.get('event_id')
        if isinstance(event_id, str):
            if event_id in self.seen:return
            self.seen.add(event_id); self.seen_order.append(event_id)
            if len(self.seen_order) > 2048:self.seen.discard(self.seen_order.pop(0))
        error = event.get('error') or {}
        ack = event.get('client_event_id') or (error.get('client_event_id') if isinstance(error, dict) else None)
        if ack in self.pending_acks:
            future = self.pending_acks[ack]
            if not future.done():
                if kind == 'error':future.set_exception(RuntimeError('GPT-Live 拒绝了上下文更新'))
                else:future.set_result(event)
        now = time.monotonic()
        if kind == 'session.input_transcript.delta':
            row, new = self.inputs.append(event)
            if not row:return
            self.last_input = self.last_activity = now
            if new:
                await self.interrupt()
                await self.client({'type': 'conversation.item.input_audio_transcription.started', 'item_id': 'live-u-'+row['id']})
            await self.client({'type': 'conversation.item.input_audio_transcription.delta', 'delta': row['text']})
            # Cumulative replacement saves full input even if there is no turn-done event.
            await self.client({'type': 'live.input.saved', 'item_id': 'live-u-'+row['id'], 'text': row['text']})
            if local_action(row['text']) == 'end':self.spawn(self.maybe_farewell(row['id'], row['text']))
        elif kind == 'session.output_transcript.delta':
            row, new = self.outputs.append(event)
            if row:
                await self.client({'type': 'response.output_text.delta', 'response_id': 'live-a-'+row['id'], 'delta': event['delta']})
        elif kind == 'session.output_audio.delta':
            raw = base64.b64decode(event.get('delta', ''), validate=True)
            if len(raw)%2:raise ValueError('Invalid PCM frame')
            pcm = struct.unpack('<'+'h'*(len(raw)//2), raw)
            loud = any(abs(sample) > 90 for sample in pcm)
            gap = now-self.last_packet; self.last_packet = now
            self.silent_samples = 0 if loud else self.silent_samples+len(pcm)
            if self.output_blocked and (self.silent_samples >= 2400 or gap > .4):self.output_blocked = False
            if self.output_blocked:return
            if loud:self.last_sound = self.last_activity = now
            if loud or now-self.last_sound < .25:
                await self.client({'type': 'live.audio.delta', 'delta': event['delta'], 'generation': self.generation,
                                   'format': 'pcm16', 'sample_rate': 16000})
        elif kind == 'session.delegation.created':self.spawn(self.delegate(event))
        elif kind in ('session.usage.updated', 'session.closed'):
            if isinstance(event.get('usage'), dict):self.usage = numeric(event['usage'])
            if kind == 'session.closed':self.finalized = True; self.finished.set()
        elif kind == 'error' and not ack:
            await self.client({'type': 'error', 'message': 'GPT-Live 会话异常，请检查服务权限或稍后重试'})
            self.finished.set()

    async def maybe_farewell(self, identity, text):
        # A fragment such as “再见” may continue as “再见的英文怎么说”.
        await asyncio.sleep(.8)
        row = self.inputs.rows[-1] if self.inputs.rows else None
        if row and row['id'] == identity and row['text'] == text and time.monotonic()-self.last_input >= .7:
            await self.farewell()

    async def farewell(self):
        if self.ending:return
        self.ending = True
        try:
            await self.append('session.instructions.append', '用户明确告别，请现在只说一句简短的再见，然后停止说话。')
            await asyncio.sleep(1)
            for _ in range(60):
                if self.idle():break
                await asyncio.sleep(.1)
            await self.client({'type': 'call.end', 'reason': 'farewell'})
        finally:self.finished.set()

    async def receive(self):
        async for raw in self.up:
            await self.provider_event(json.loads(raw))
            if self.finalized:return

    async def browser(self):
        while True:
            event = await self.ws.receive_json(); kind = event.get('type')
            if kind == 'session.close':return
            if kind == 'input_audio_buffer.append' and not self.muted:
                audio = event.get('audio', '')
                if not isinstance(audio, str) or len(audio) > 64000:raise ValueError('Invalid microphone frame')
                raw = base64.b64decode(audio, validate=True)
                if len(raw)%2:raise ValueError('Invalid microphone PCM')
                if any(abs(value[0]) > 180 for value in struct.iter_unpack('<h', raw)):self.last_activity = time.monotonic()
                await self.send({'type': 'session.input_audio.append', 'audio': audio})
            elif kind in ('input_audio_mute.commit', 'input_audio_unmute.commit'):
                self.muted = kind == 'input_audio_mute.commit'
                await self.send({'type': 'session.input_audio.mute' if self.muted else 'session.input_audio.unmute'})
            elif kind == 'response.cancel':await self.interrupt(manual=True)
            elif kind == 'client.playback':
                if event.get('generation') == self.generation:self.playing = event.get('playing') is True
            elif kind in ('task.continue', 'task.replay', 'task.defer'):
                self.spawn(self.saved(kind.split('.')[1], key=event.get('task_id')))

    async def idle_watch(self):
        while True:
            await asyncio.sleep(.5)
            if not self.manager.busy(self.scope) and not self.jobs and not self.playing and time.monotonic()-self.last_activity > 30:
                await self.client({'type': 'call.end', 'reason': 'idle_timeout'});return

    async def run(self, root):
        key = api_key(root)
        if not key:
            await self.client({'type': 'error', 'message': 'GPT-Live 尚未配置 OpenAI API Key，请在服务器配置后再试，或切换豆包。'})
            return
        await self.client({'type': 'session.status', 'message': '正在连接 GPT-Live'})
        workers = []; started = False
        try:
            async with websockets.connect(URL, additional_headers={'Authorization': 'Bearer '+key}, proxy=network_proxy(root), open_timeout=12,
                                          max_size=4_000_000, ping_interval=20, ping_timeout=20) as self.up:
                await self.send({'type': 'session.start', 'session': session_config(self.context.get('history', []), self.profile,
                                 self.store.list(self.scope), self.initial.get('live_voice', 'marin'))})
                first = json.loads(await asyncio.wait_for(self.up.recv(), 18))
                if first.get('type') != 'session.started':raise RuntimeError('GPT-Live startup rejected')
                started = True; self.last_activity = time.monotonic()
                write_usage('call_started', provider='gptlive', model='gpt-live-1')
                await self.client({'type': 'session.created', 'provider': 'gptlive', 'model': 'gpt-live-1'})
                receive = asyncio.create_task(self.receive()); browser = asyncio.create_task(self.browser())
                watcher = asyncio.create_task(self.watch()); idle = asyncio.create_task(self.idle_watch())
                ended = asyncio.create_task(self.finished.wait()); workers = [receive, browser, watcher, idle, ended]
                try:
                    done, _ = await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:task.result()
                finally:
                    self.ending = True
                    for job in [browser, watcher, idle, ended, *self.jobs]:job.cancel()
                    await asyncio.gather(browser, watcher, idle, ended, *self.jobs, return_exceptions=True)
                    # Receiver stays alive through session.closed so billing is finalized.
                    if not self.finalized:
                        try:
                            await self.send({'type': 'session.close'})
                            await asyncio.wait_for(asyncio.shield(receive), 8)
                        except Exception:pass
        except Exception as exc:
            # Do not expose exceptions: websocket handshakes may contain credentials.
            logger.warning('live_connection_failed kind=%s', type(exc).__name__)
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            message = 'GPT-Live 连接失败，请检查 OpenAI Key 权限及服务器网络。'
            if isinstance(exc, TimeoutError):message = 'GPT-Live 连接超时，请检查服务器到 OpenAI 的出网连接或代理配置。'
            elif status in (401, 403):message = 'GPT-Live 访问被拒绝，请检查 OpenAI Key、接口权限及服务可用区域。'
            elif status == 429:message = 'GPT-Live 当前额度或并发受限，请检查账户用量后重试。'
            try:await self.client({'type': 'error', 'message': message})
            except Exception:pass
        finally:
            for task in workers:task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            if started:write_usage('live_session_usage', provider='gptlive', usage=self.usage, finalized=self.finalized)
            # The shared BackgroundTasks manager owns Being jobs; never cancel them here.
