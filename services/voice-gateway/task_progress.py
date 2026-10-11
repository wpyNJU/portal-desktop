"""Occasional spoken progress from observable Agent stages, never reasoning."""
import asyncio
import logging
import math
import re
import time


STAGE_TEXT = {
    'queued': '还在等待后台处理，正文尚未返回。',
    'accepted': '后台已接收查询，正文尚未返回。',
    'thinking': '后台正在分析问题，正文尚未返回。',
    'tool': '正在调用工具获取信息，正文尚未返回。',
    'text': '后台正在返回内容，暂时还没收到可用正文。',
    'awaiting_reply': '还在等待后台回复，正文尚未返回。',
    'request_start': '正在提交后台查询，正文尚未返回。',
    'history_start': '正在连接后台，正文尚未返回。',
    'connected': '已连接后台，正在等待正文。',
}
# Technical stages are not separate user-facing discoveries.
STAGE_GROUP = {stage: 'waiting' for stage in STAGE_TEXT}


class TaskProgress:
    def __init__(self, store, scope, scheduler, client, speak, output=None, *,
                 first_delay=45, cooldown=60, max_queue_age=15, now=time.time):
        self.store = store
        self.scope = scope
        self.scheduler = scheduler
        self.client = client
        self.speak = speak
        self.output = output
        self.first_delay = max(0, float(first_delay))
        self.cooldown = max(0, float(cooldown))
        self.max_queue_age = max(0, float(max_queue_age))
        self.now = now
        self.closed = False
        self.rows = {}
        self.candidates = {}
        self.jobs = {}
        self.attempted = {}
        self.spoken = {}
        self.last_spoken = None
        self.logger = logging.getLogger('voice.connection')

    def _current(self, key):
        observed = self.rows.get(key)
        if observed is None:
            return None
        try:
            stored = self.store.get(self.scope, key)
        except ValueError:
            return None
        # Some callers attach stage metadata from their stream observer; newer
        # stores persist it. In both cases database task/control state wins.
        current = {**observed, **stored}
        if not stored.get('stage'):
            current['stage'] = observed.get('stage')
            current['stage_updated'] = observed.get('stage_updated')
        return current

    def _running(self, row):
        return (row is not None and row.get('state') == 'running' and
                row.get('delivery') != 'deferred' and not row.get('text') and
                not row.get('summary') and row.get('stage') in STAGE_TEXT)

    def _timing(self, row):
        try:
            created = float(row['created'])
            updated = float(row['stage_updated'])
        except (KeyError, TypeError, ValueError):
            return False
        now = self.now()
        return (math.isfinite(created) and math.isfinite(updated) and
                now - created >= self.first_delay and
                (self.last_spoken is None or now - self.last_spoken >= self.cooldown))

    def _valid(self, candidate):
        row = self._current(candidate['key'])
        if not candidate['started'] and self.now()-candidate.get('last_wait_log',-float('inf'))>=5:
            candidate['last_wait_log']=self.now()
            self.logger.info('progress_wait task=%s user=%s generating=%s playing=%s locked=%s',candidate['key'],self.scheduler.user,self.scheduler.generating,self.scheduler.playing,self.scheduler.lock.locked())
        return (not self.closed and not candidate['obsolete'] and
                (candidate['stop'] is None or not candidate['stop'].is_set()) and
                self._running(row) and
                (self.output is None or self.output.turn == candidate['turn']) and
                all(self._running(self._current(key)) for key in candidate.get('batch',[])) and
                (candidate['played'] or self.now() <= candidate['expires']))

    def _discard(self, candidate):
        candidate['obsolete'] = True
        if candidate['stop'] is not None:
            candidate['stop'].set()
        job = candidate.get('job')
        if job is not None and not job.done():
            job.cancel()

    def observe(self, row):
        """Merge the latest real stage; polling never repeats an attempted stage."""
        if self.closed or not isinstance(row, dict) or not row.get('id') or row.get('kind')=='reply':
            return False
        key = row['id']
        self.rows[key] = dict(row)
        current = self._current(key)
        candidate = self.candidates.get(key)
        if (candidate is not None and self._running(current) and not candidate['started'] and
                STAGE_GROUP[current['stage']] == candidate['group']):
            candidate['stage'] = current['stage']
        if candidate is not None and not self._valid(candidate):
            self._discard(candidate)
        elif candidate is not None and candidate['played']:
            # A real stage transition must not clear the short sentence already
            # queued at the client. Results, stop and defer still invalidate it.
            return False
        if not self._running(current) or not self._timing(current):
            return False
        stage = current['stage']
        group = STAGE_GROUP[stage]
        if group in self.attempted.get(key, set()):
            return False
        # Rapid stage transitions replace, rather than append to, a pending
        # announcement. The old action revalidates before each outbound frame.
        if candidate is not None:
            self._discard(candidate)
        candidate = {'key': key, 'stage': stage, 'group': group,'expires': self.now() + self.max_queue_age,
                     'played': False, 'started': False, 'obsolete': False, 'stop': None, 'job': None,
                     'turn': self.output.turn if self.output else None}
        self.attempted.setdefault(key, set()).add(group)
        self.candidates[key] = candidate
        self.logger.info('progress_queued task=%s stage=%s', key, stage)
        job = asyncio.create_task(self._deliver(candidate))
        candidate['job'] = job
        self.jobs[key] = job

        def finished(task):
            if self.jobs.get(key) is task:
                self.jobs.pop(key, None)
            if self.candidates.get(key) is candidate:
                self.candidates.pop(key, None)
            if not task.cancelled() and task.exception() is not None:
                self.logger.warning('task_progress_unavailable kind=%s', type(task.exception()).__name__)
        job.add_done_callback(finished)
        return True

    async def _deliver(self, candidate):
        key = candidate['key']

        async def action(stop):
            candidate['stop'] = stop
            row = self._current(key)
            if not self._valid(candidate) or not self._timing(row):
                return
            candidate['started'] = True
            self.logger.info('progress_synthesis task=%s stage=%s', key, candidate['stage'])
            if self.output and not await self.output.begin_scheduled(stop):
                return
            # One foreground announcement merges every eligible waiting task.
            # Consume other candidates now; interruption must not replay a backlog.
            batch = [row]
            for other_key in self.rows:
                if other_key == key:continue
                other = self._current(other_key)
                pending = self.candidates.get(other_key)
                if (self._running(other) and self._timing(other) and
                        (pending is not None and not pending['obsolete'] or
                         'waiting' not in self.attempted.get(other_key, set()))):
                    batch.append(other)
                    self.attempted.setdefault(other_key, set()).add('waiting')
                    if pending is not None:self._discard(pending)
            batch.sort(key=lambda r:r.get('requested_at',r['created']),reverse=True)
            candidate['batch'] = [r['id'] for r in batch]
            titles = [re.sub(r'\s+', ' ', str(r.get('title') or '这项查询')).strip()[:18] for r in batch]
            if len(batch) == 1:text = f"{titles[0]}还没返回结果，我会继续留意。"
            else:
                subjects = '、'.join(titles[:2])
                if len(batch)>2:subjects += f"等{len(batch)}项查询"
                text = f"{subjects}都还没返回结果，有消息我再告诉你。"

            async def output(event):
                if not self._valid(candidate):
                    return
                if self.output:
                    forwarded = await self.output.scheduled(event, stop)
                else:
                    if event.get('type') == 'opening.audio':
                        self.scheduler.update(playing=True)
                    await self.client(event)
                    forwarded = True
                # Synthesis/queueing are not evidence of audible delivery.
                # Only a successful first nonempty audio frame starts cooldown.
                if (forwarded and event.get('type') == 'opening.audio' and
                        event.get('delta') and not candidate['played']):
                    candidate['played'] = True
                    for task_key in candidate.get('batch',[key]):
                        self.spoken.setdefault(task_key, set()).add('waiting')
                    self.last_spoken = self.now()
                    self.logger.info('progress_forwarded task=%s stage=%s generation=%s', key, candidate['stage'], getattr(self.output,'generation',None))


            await self.speak(text, output, stop, lambda: self._valid(candidate))

        def priority():
            row = self._current(key) or {}
            # Results and their acknowledgements win over progress at equal
            # relevance. No progress announcement jumps a newer user question.
            return (-float('inf'), -1)

        completed = False
        try:
            while self._valid(candidate):
                if not self._timing(self._current(key)):
                    await asyncio.sleep(.05)
                    continue
                await self.scheduler.deliver(action, priority=priority,
                                             valid=lambda: self._valid(candidate))
                if candidate['started'] or not self._valid(candidate):
                    break
            completed = True
        finally:
            self.logger.info('progress_finished task=%s stage=%s forwarded=%s',key,candidate['stage'],candidate['played'])
            stop = candidate['stop']
            if stop is not None and (not completed or not candidate['played'] or
                                     stop.is_set() or candidate['obsolete'] or self.closed):
                if self.output:
                    await self.output.clear_scheduled(stop)
                elif self.scheduler.audio_stop is stop:
                    await self.client({'type': 'playback.clear'})
                    self.scheduler.update(playing=False)

    async def close(self):
        """Stop only this owner's work; leave the shared scheduler operational."""
        self.closed = True
        for candidate in list(self.candidates.values()):
            self._discard(candidate)
        await asyncio.gather(*list(self.jobs.values()), return_exceptions=True)
        self.jobs.clear()
        self.candidates.clear()
