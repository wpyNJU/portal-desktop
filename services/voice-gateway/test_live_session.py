"""Actual Live adapter with deterministic provider/Agent transports; no credentials."""
import asyncio
import base64
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

usage = []
transport = SimpleNamespace(connect=None)
with patch.dict(sys.modules, {'websockets': transport, 'voice_usage': SimpleNamespace(
        numeric=lambda value: value, write=lambda kind, **value: usage.append((kind, value)))}):
    import live_session
    from live_session import LiveSession, SerialBridge, Transcript, local_action
from live_config import api_key, capabilities, session_config
from background_tasks import BackgroundTasks
from task_store import TaskStore


class Phone:
    def __init__(self):self.queue = asyncio.Queue(); self.received = []
    async def receive_json(self):return await self.queue.get()
    async def client(self, event):self.received.append(event)


class Provider:
    def __init__(self):self.queue = asyncio.Queue(); self.sent = []
    async def __aenter__(self):return self
    async def __aexit__(self, *args):pass
    async def send(self, raw):
        event = json.loads(raw); self.sent.append(event); kind = event['type']
        if kind == 'session.start':await self.queue.put({'type': 'session.started', 'session': {'id': 'fixture'}})
        elif kind == 'session.close':await self.queue.put({'type': 'session.closed', 'usage': {'seconds': 1.25}})
        elif kind.endswith('.append') and kind != 'session.input_audio.append':
            await self.queue.put({'type': kind+'ed', 'client_event_id': event['event_id']})
    async def recv(self):return json.dumps(await self.queue.get())
    def __aiter__(self):return self
    async def __anext__(self):return await self.recv()


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = TaskStore(':memory:'); self.manager = BackgroundTasks(self.store)
        self.phone = Phone(); self.provider = Provider(); self.requests = []
        self.release = asyncio.Event()
        owner = self
        class Bridge:
            async def stream(self, message, context, stop, emit, stage=None):
                owner.requests.append(message)
                if stage:stage('thinking')
                await owner.release.wait()
                emit('合成查询的完整结果。')
        self.bridge = Bridge()
        self.session = LiveSession(self.phone, self.phone.client, self.store, self.manager, self.bridge,
            {'scope': 'fixture', 'scene': 'fixture', 'history': []}, {}, {},
            lambda item: (json.loads(item['arguments'])['message'], False))
        self.session.up = self.provider
        self.original_append = self.session.append
        self.appends = []
        async def append(*args):self.appends.append(args)
        self.session.append = append

    async def asyncTearDown(self):
        self.release.set()
        for job in list(self.session.jobs):job.cancel()
        await asyncio.gather(*self.session.jobs, return_exceptions=True)
        await self.manager.shutdown(); self.store.db.close()

    async def until(self, predicate):
        async with asyncio.timeout(3):
            while not predicate():await asyncio.sleep(.01)

    def input(self, text, start=0, end=400):
        return {'type': 'session.input_transcript.delta', 'start_ms': start, 'end_ms': end, 'delta': text}

    def delegation(self, key, offset=500):
        return {'type': 'session.delegation.created', 'offset_ms': offset, 'delegation': {'id': key, 'target': 'client'}}

    async def test_missing_key_never_opens_provider_and_has_safe_message(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'OPENAI_API_KEY': ''}):
            with patch.object(transport, 'connect', side_effect=AssertionError('must not connect')):
                await self.session.run(root)
            self.assertIn('尚未配置', self.phone.received[-1]['message'])

    async def test_duplicate_delegations_reuse_one_query_and_preserve_full_request(self):
        text = '查询项目甲本周进度，先不要修改任何东西。'
        await self.session.provider_event(self.input(text))
        await self.session.delegate(self.delegation('d1'))
        await self.session.delegate(self.delegation('d1'))
        await self.session.delegate(self.delegation('d2'))
        await self.until(lambda: bool(self.requests))
        self.assertEqual(self.requests, [text]); self.assertEqual(len(self.store.list('fixture')), 1)
        self.assertEqual(self.store.list('other-account'), [])

    async def test_transcript_arriving_after_delegation_is_used_without_a_fake_question(self):
        job = asyncio.create_task(self.session.delegate(self.delegation('d-late')))
        await asyncio.sleep(.15)
        await self.session.provider_event(self.input('查一下项目乙。'))
        await job; await self.until(lambda: bool(self.requests))
        self.assertEqual(self.requests, ['查一下项目乙。'])

    async def test_later_repeated_question_reuses_task_and_resumes_its_report(self):
        await self.session.provider_event(self.input('查询项目甲。'))
        await self.session.delegate(self.delegation('d1'))
        key = self.session.focus
        self.session.tracked[key]['paused'] = True
        await self.session.provider_event(self.input('查询项目甲。', 3000, 3500))
        await self.session.delegate(self.delegation('d2', 3600))
        self.assertEqual(self.session.focus, key)
        self.assertFalse(self.session.tracked[key]['paused'])
        self.assertEqual(self.session.tracked[key]['delegation'], 'd2')
        self.assertEqual(len(self.store.list('fixture')), 1)

    async def test_goodbye_fragment_does_not_hang_up_a_longer_question(self):
        await self.session.provider_event(self.input('再见', 0, 200))
        await self.session.provider_event(self.input('的英文怎么说', 210, 600))
        await asyncio.sleep(.85)
        self.assertFalse(self.session.ending)
        self.assertFalse(any(e['type'] == 'call.end' for e in self.phone.received))

    async def test_serial_bridge_keeps_second_live_query_from_preempting_first(self):
        active = 0; peak = 0; order = []; release = asyncio.Event()
        class Bridge:
            async def stream(self, message, *args, **kwargs):
                nonlocal active, peak
                active += 1; peak = max(peak, active); order.append(message)
                await release.wait(); active -= 1
        bridge = SerialBridge(Bridge(), asyncio.Lock())
        first = asyncio.create_task(bridge.stream('first'))
        second = asyncio.create_task(bridge.stream('second'))
        await asyncio.sleep(.03); self.assertEqual(order, ['first'])
        release.set(); await asyncio.gather(first, second)
        self.assertEqual(order, ['first', 'second']); self.assertEqual(peak, 1)

    async def test_interrupt_clears_audio_not_query_and_late_audio_waits_for_silence(self):
        await self.session.provider_event(self.input('查询任务。'))
        await self.session.delegate(self.delegation('d1'))
        await self.until(lambda: bool(self.requests))
        loud = base64.b64encode(struct.pack('<320h', *([2000]*320))).decode()
        quiet = base64.b64encode(bytes(4800)).decode()
        self.session.last_packet = live_session.time.monotonic()
        await self.session.interrupt(manual=True)
        await self.session.provider_event({'type': 'session.output_audio.delta', 'delta': loud})
        self.assertFalse(any(e['type'] == 'live.audio.delta' for e in self.phone.received))
        self.assertTrue(self.manager.busy('fixture'))
        await self.session.provider_event({'type': 'session.output_audio.delta', 'delta': quiet})
        await self.session.provider_event({'type': 'session.output_audio.delta', 'delta': loud})
        audio = [e for e in self.phone.received if e['type'] == 'live.audio.delta'][-1]
        self.assertEqual(audio['sample_rate'], 16000); self.assertEqual(audio['format'], 'pcm16')
        self.release.set(); await self.until(lambda: not self.manager.busy('fixture'))
        self.assertEqual(self.store.list('fixture')[0]['state'], 'completed')

    async def test_commentary_receipt_does_not_mark_report_played_or_skip_interrupted_chunk(self):
        key = self.store.create('fixture', '合成查询')
        self.store.update('fixture', key, state='completed', text='完整正文。', cursor=0)
        self.session.tracked[key] = {'delegation': 'd1', 'sent': 0, 'paused': False}
        self.session.reporting = key
        async def append(*args):await self.session.interrupt()
        self.session.append = append
        await self.session.report(key)
        row = self.store.get('fixture', key)
        self.assertEqual(row['cursor'], 0); self.assertEqual(row['delivery'], 'paused')
        self.assertEqual(self.session.tracked[key]['sent'], 0)

    async def test_reconnect_does_not_replay_or_resubmit_historical_tasks(self):
        for _ in range(30):
            key = self.store.create('fixture', '旧查询')
            self.store.update('fixture', key, state='completed', text='已保存结果', delivery='paused')
        watcher = asyncio.create_task(self.session.watch())
        await asyncio.sleep(.2); self.session.ending = True; await watcher
        self.assertEqual(self.requests, []); self.assertEqual(self.appends, [])
        self.assertEqual(len(self.session.tracked), 30)

    async def test_interruption_after_ack_retries_whole_unconfirmed_piece(self):
        key = self.store.create('fixture', '测试报告')
        self.store.update('fixture', key, state='completed', text='第一段结果。第二段结果。')
        self.session.tracked[key] = {'delegation': 'd1', 'sent': 0, 'paused': False}
        self.session.reporting = key
        job = asyncio.create_task(self.session.report(key))
        await self.until(lambda: self.session.tracked[key]['sent'] > 0)
        await self.session.interrupt()
        await job
        self.assertEqual(self.session.tracked[key]['sent'], 0)
        self.assertEqual(self.store.get('fixture', key)['cursor'], 0)

    async def test_real_lifecycle_waits_for_final_usage_and_disconnect_keeps_agent_running(self):
        self.session.append = self.original_append
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'OPENAI_API_KEY': 'fixture-only'}), patch.object(transport, 'connect', return_value=self.provider):
            run = asyncio.create_task(self.session.run(root))
            await self.until(lambda: any(e['type'] == 'session.created' for e in self.phone.received))
            await self.provider.queue.put(self.input('查询当前任务。'))
            await self.provider.queue.put(self.delegation('d1'))
            await self.until(lambda: bool(self.requests))
            await self.phone.queue.put({'type': 'session.close'})
            await asyncio.wait_for(run, 3)
            self.assertTrue(self.session.finalized); self.assertEqual(self.session.usage['seconds'], 1.25)
            self.assertTrue(self.manager.busy('fixture'))
            self.assertFalse(any('fixture-only' in json.dumps(e) for e in self.phone.received))
            self.release.set(); await self.until(lambda: not self.manager.busy('fixture'))
            self.assertEqual(self.store.list('fixture')[0]['state'], 'completed')


class ConfigurationTests(unittest.TestCase):
    def test_history_budget_voice_and_configuration(self):
        config = session_config([{'role': 'user', 'text': '历史'*10000}]*150, {}, [], 'invalid-voice')
        self.assertLessEqual(len(config['input']), 60)
        self.assertLessEqual(sum(len(m['content'][0]['text'].encode()) for m in config['input']), 6500)
        self.assertEqual(config['audio']['format'], {'type': 'audio/pcm', 'rate': 16000})
        self.assertEqual(config['audio']['output']['voice'], 'marin')
        self.assertEqual(config['delegation'], {'type': 'client'}); self.assertFalse(config['store'])

    def test_key_can_be_added_after_start_without_exposing_value_in_capabilities(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'OPENAI_API_KEY': ''}):
            self.assertFalse(capabilities(root)['gptlive']['configured'])
            (Path(root)/'openai-api-key.txt').write_text('fixture-private-value')
            self.assertTrue(capabilities(root)['gptlive']['configured'])
            self.assertEqual(api_key(root), 'fixture-private-value')
            self.assertNotIn('fixture-private-value', json.dumps(capabilities(root)))

    def test_fragment_timeline_and_literal_commands(self):
        transcript = Transcript()
        transcript.append({'delta': '查一下', 'start_ms': 0, 'end_ms': 200})
        transcript.append({'delta': '昨天的消息', 'start_ms': 200, 'end_ms': 700})
        transcript.append({'delta': '新的问题', 'start_ms': 3000, 'end_ms': 3500})
        self.assertEqual(transcript.at(750)['text'], '查一下昨天的消息')
        self.assertIsNone(transcript.at(20000))
        self.assertEqual(local_action('继续报告。'), 'continue')
        self.assertIsNone(local_action('帮我发一条再见的消息'))


if __name__ == '__main__':unittest.main()
