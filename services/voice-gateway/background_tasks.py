"""Agent requests belong to the service, never to a phone WebSocket."""
import asyncio
import copy
import logging
import threading
import time

logger = logging.getLogger('voice.connection')


class BackgroundTasks:
    def __init__(self, store, timeout=600, idle_timeout=300):
        self.store = store
        self.timeout = timeout
        self.idle_timeout = idle_timeout
        self.entries = {}
        self.jobs = {}
        self.progress = {}
        self.scopes = {}

    def submit(self, scope, identity, cacheable, message, bridge, context, title=None):
        now = time.monotonic()
        self.entries = {k: v for k, v in self.entries.items()
                        if not v['job'].done() or now-v['finished'] < 60}
        key = (scope, identity)
        previous = self.entries.get(key) if identity else None
        if previous and (not previous['job'].done() or
                         (cacheable and self.store.get(scope, previous['id'])['state'] == 'completed')):
            return previous['id'], True
        task_id = self.store.create(scope, message, title)
        job = asyncio.create_task(self.run(task_id, scope, message, bridge, copy.deepcopy(context)))
        entry = {'id': task_id, 'job': job, 'finished': float('inf')}
        if identity:
            self.entries[key] = entry
        self.jobs[task_id] = job
        self.scopes[task_id] = scope
        def finished(task):
            entry['finished'] = time.monotonic()
            self.jobs.pop(task_id, None)
            self.scopes.pop(task_id, None)
            self.progress.pop(task_id, None)
        job.add_done_callback(finished)
        return task_id, False

    async def run(self, task_id, scope, message, bridge, context):
        parts = []; last = time.monotonic(); started = last
        stop = threading.Event(); previous_stage = None
        def stage(name):
            nonlocal last,previous_stage
            if name!=previous_stage:
                logger.info("background_task_stage id=%s stage=%s elapsed=%.3f",task_id,name,time.monotonic()-started);previous_stage=name
            last = time.monotonic()
            self.progress[task_id] = (name, round(last-started))
        def emit(text):
            if not isinstance(text, str) or not text:
                return
            parts.append(text); stage('text')
            self.store.update(scope, task_id, text=''.join(parts))
        request = asyncio.create_task(bridge.stream(message, context, stop, emit, stage=stage))
        state = 'error'
        try:
            async with asyncio.timeout(self.timeout):
                while not request.done():
                    await asyncio.wait([request], timeout=.25)
                    if time.monotonic()-last > self.idle_timeout:
                        raise TimeoutError()
                await request
            state = 'completed' if parts else 'error'
        except asyncio.CancelledError:
            state = 'interrupted'
            raise
        except Exception as exc:
            logger.warning('background_task_failed id=%s kind=%s', task_id, type(exc).__name__)
        finally:
            if not request.done():request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            # A disconnected listener may have paused/deferred delivery. Preserve that choice.
            self.store.update(scope, task_id, state=state, text=''.join(parts))
            logger.info('background_task_done id=%s state=%s elapsed=%.3f', task_id, state, time.monotonic()-started)

    def busy(self, scope):
        return any(owner == scope for owner in self.scopes.values())

    async def shutdown(self):
        jobs=list(self.jobs.values())
        for job in jobs:job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
