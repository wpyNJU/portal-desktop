"""Agent requests belong to the service, never to a phone WebSocket."""
import asyncio
import copy
import logging
import threading
import time
import json
from agent_errors import AgentFailure

SAFE_STAGES = frozenset({'queued','accepted','awaiting_reply','thinking','tool','text'})

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
        self.conversations = {}

    def submit(self, scope, identity, cacheable, message, bridge, context, title=None, request_id=None, intent=None, parent_id=''):
        intent_json=json.dumps(intent,sort_keys=True,ensure_ascii=False) if intent else ''
        fingerprint=json.dumps([intent if intent else message,parent_id],sort_keys=True,ensure_ascii=False)
        if request_id:
            prior=self.store.request_match(scope,request_id,fingerprint)
            if prior:return prior,True
        if intent_json:
            prior=self.store.running_intent(scope,intent_json,parent_id)
            if prior:
                if request_id:self.store.bind_request(scope,request_id,prior,fingerprint)
                self.store.touch(scope,prior)
                return prior,True
        now = time.monotonic()
        self.entries = {k: v for k, v in self.entries.items()
                        if not v['job'].done() or now-v['finished'] < 60}
        key = (scope, identity)
        previous = self.entries.get(key) if identity else None
        if previous and (not previous['job'].done() or
                         (cacheable and self.store.get(scope, previous['id'])['state'] == 'completed')):
            self.store.touch(scope,previous['id'])
            return previous['id'], True
        task_id = self.store.create(scope, message, title)
        if intent_json:self.store.set_intent(task_id,intent_json,parent_id)
        if request_id:self.store.bind_request(scope,request_id,task_id,fingerprint)
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
            last = time.monotonic()
            if not isinstance(name,str) or name not in SAFE_STAGES:return
            if name!=previous_stage:
                self.store.update(scope, task_id, stage=name, stage_updated=time.time())
                logger.info("background_task_stage id=%s stage=%s elapsed=%.3f",task_id,name,time.monotonic()-started);previous_stage=name
            self.progress[task_id] = (name, round(last-started))
        def emit(text):
            if not isinstance(text, str) or not text:
                return
            parts.append(text); stage('text')
            self.store.update(scope, task_id, text=''.join(parts))
        conversation=None
        if context.get('conversation_mode') and hasattr(bridge,'endpoint'):
            from being_conversation import BeingConversation
            conversation=self.conversations.get(scope)
            if conversation is None or conversation.closed:
                conversation=BeingConversation(self.store,scope,bridge)
                self.conversations[scope]=conversation
            request=asyncio.create_task(conversation.request(task_id,message,context,stage))
        else:
            request = asyncio.create_task(bridge.stream(message, context, stop, emit, stage=stage))
        shared_reply=''
        state = 'error'
        failure = None
        try:
            async with asyncio.timeout(self.timeout):
                while not request.done():
                    await asyncio.wait([request], timeout=.25)
                    if time.monotonic()-last > self.idle_timeout:
                        raise TimeoutError()
                result=await request
            if conversation and isinstance(result,dict):shared_reply=result.get('reply_task_id','')
            if not parts and not shared_reply:raise AgentFailure('empty_reply')
            state = 'completed'
        except asyncio.CancelledError:
            state = 'interrupted'
            failure = AgentFailure('gateway_interrupted')
            raise
        except Exception as exc:
            code='reply_timeout'
            if conversation and self.store.get(scope,task_id).get('reply_task_id'):code='reply_unattributed'
            failure=exc if isinstance(exc,AgentFailure) else AgentFailure(code if isinstance(exc,TimeoutError) else 'unexpected_error')
            if failure.code=='upstream_interrupted':state='interrupted'
            logger.warning('background_task_failed id=%s kind=%s code=%s', task_id, type(exc).__name__,failure.code)
        finally:
            if not request.done():request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            # A disconnected listener may have paused/deferred delivery. Preserve that choice.
            body=''.join(parts)
            # A bounded literal excerpt, never reasoning or a generated conclusion.
            summary=('原文摘录：'+body.strip()[:235]) if body.strip() else ''
            if conversation:
                summary=self.store.get(scope,task_id).get('summary','')
            if shared_reply:self.store.update(scope,task_id,reply_task_id=shared_reply,delivery='reported')
            self.store.update(scope, task_id, state=state, text=body,stage=state,stage_updated=time.time(),summary=summary,
                              error_code=failure.code if failure else '',error_message=failure.public_message if failure else '')
            logger.info('background_task_done id=%s state=%s elapsed=%.3f', task_id, state, time.monotonic()-started)

    def busy(self, scope):
        return any(owner == scope for owner in self.scopes.values())

    async def shutdown(self):
        jobs=list(self.jobs.values())
        for job in jobs:job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        await asyncio.gather(*(c.close() for c in self.conversations.values()),return_exceptions=True)
