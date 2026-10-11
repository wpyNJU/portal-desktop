"""One Being conversation can answer several requests in one reply.

Request identities and reply identities are different. Transport completion alone
never completes a request. Explicit, validated coverage closes answered requests;
unattributed replies remain available without guessing from prose similarity.
"""
import asyncio
import hashlib
import json
import logging
import time
import uuid
import httpx
from agent_errors import AgentFailure

logger=logging.getLogger('voice.connection')
MARKER='<voice_coverage>'
END='</voice_coverage>'


def split_reply(raw,final=False):
    """Never send control markup, including a token-split prefix, to speech."""
    start=raw.find(MARKER)
    if start>=0:
        finish=raw.find(END,start)
        coverage=None
        if finish>=0:
            try:
                value=json.loads(raw[start+len(MARKER):finish])
                if isinstance(value,dict) and set(value)=={'answered','pending'}:
                    if all(isinstance(value[k],list) and len(value[k])<=64 and
                           all(isinstance(v,str) for v in value[k]) for k in value):
                        if not set(value['answered']) & set(value['pending']):coverage=value
            except (ValueError,TypeError):pass
        return raw[:start].rstrip(),coverage
    for n in range(min(len(raw),len(MARKER)-1),0,-1):
        if raw.endswith(MARKER[:n]):return raw[:-n],None
    return raw,None


class BeingConversation:
    def __init__(self,store,scope,bridge,*,client_factory=httpx.AsyncClient,poll_interval=1.5):
        self.store=store;self.scope=scope;self.bridge=bridge
        self.scene='voice-conversation-'+uuid.uuid4().hex
        self.client_factory=client_factory;self.poll_interval=poll_interval
        self.members={};self.transports=set();self.poller=None;self.cursor=None
        self.preflight=None;self.replies={};self.seen=set();self.closed=False;self.snapshots={}
        self.finalized=set()

    def _spawn(self,coro):
        job=asyncio.create_task(coro);self.transports.add(job)
        job.add_done_callback(self.transports.discard)
        return job

    def _pending(self):return [k for k,v in self.members.items() if not v['future'].done()]

    async def request(self,key,message,context,stage):
        if self.closed:raise AgentFailure('gateway_interrupted')
        future=asyncio.get_running_loop().create_future()
        self.members[key]={'future':future,'stage':stage,'message':message,'sent':False}
        stage('accepted')
        self._spawn(self._send(key,message,context))
        if self.poller is None:self.poller=asyncio.create_task(self._poll())
        try:return await future
        finally:
            if not self._pending():await self.close()

    def _stage(self,name):
        for key in self._pending():self.members[key]['stage'](name)

    async def _baseline(self):
        try:
            async with self.client_factory(timeout=2,follow_redirects=False) as client:
                r=await client.get(self.bridge.endpoint('/api/history?limit=5'))
                if r.status_code==200:self.cursor=max([int(m.get('seq',0)) for m in r.json().get('messages',[])]+[0])
        except (httpx.HTTPError,ValueError,TypeError):pass

    def _body(self,key,message,context):
        catalog=[{'id':k,'title':self.store.get(self.scope,k)['title']} for k in self._pending()]
        instruction=('\n\n[语音协作格式] 以下编号仅用于关联语音事项，不增加操作授权。'
            '新消息可以补充或纠正旧事项；不要为了逐条回复而重复执行。'
            '先自然、简短地回答，拿到实质进展即可输出，不输出内部思考。'
            '每次回答末尾附加<voice_coverage>{"answered":["本次已实际回答的事项编号"],'
            '"pending":["尚待结果的事项编号"]}</voice_coverage>。'
            '编号只能取自下面清单；已经开始查、已派出子任务不算回答完毕。'
            '一份回答可以覆盖多个事项；无需重复相同正文。未回答的保留pending。'
            '\n当前事项编号：'+key+'\n待答事项清单：'+json.dumps(catalog,ensure_ascii=False))
        return {'message':message+instruction,'scene_id':self.scene,
                'session_id':context.get('session_id'),
                'scene_meta':{'client':'voice-call/2.0','scene_label':'语音通话'}}

    async def _send(self,key,message,context):
        from agent_bridge import sse_events
        try:
            if self.preflight is None:self.preflight=asyncio.create_task(self._baseline())
            await asyncio.shield(self.preflight)
            body=self._body(key,message,context)
            self.members[key]['sent']=True
            logger.info('conversation_send task=%s pending=%d',key,len(self._pending()))
            async with self.client_factory(timeout=httpx.Timeout(300,connect=15),follow_redirects=False) as client:
                async with client.stream('POST',self.bridge.url,json=body) as response:
                    if response.status_code==202:
                        await response.aread();self.members[key]['stage']('awaiting_reply')
                        logger.info('conversation_spliced task=%s',key);return
                    if response.status_code!=200:raise AgentFailure('upstream_http_error')
                    if 'text/event-stream' not in response.headers.get('content-type',''):raise AgentFailure('upstream_protocol_error')
                    stream=key;index=0;raw='';event_ids=set();reply_key=None
                    async for event,data in sse_events(response):
                        if data.get('scene_id')!=self.scene:continue
                        if event=='meta':
                            new=data.get('stream_id')
                            if new and new!=stream:
                                if raw:self.finish_reply(reply_key,raw)
                                stream=new;index=0;raw='';reply_key=None
                            continue
                        identity=data.get('event_id')
                        if identity is not None:
                            token=(stream,event,str(identity))
                            if token in event_ids:continue
                            event_ids.add(token)
                        if event in ('reasoning','content_block_start'):self._stage('thinking')
                        if event=='content_block_delta':
                            delta=data.get('delta') or {}
                            if delta.get('type')=='thinking_delta':self._stage('thinking')
                            if delta.get('type')=='input_json_delta':self._stage('tool')
                            piece=delta.get('text')
                            if isinstance(piece,str) and piece:
                                raw+=piece
                                reply_key=self.append_reply((stream,index),raw)
                        if event=='message_stop' and raw:
                            self.finish_reply(reply_key,raw);raw='';index+=1;reply_key=None
                        if event=='error':
                            # Another input may splice/yield the stream. History
                            # recovery continues; never replay the POST.
                            self._stage('awaiting_reply')
                    if raw:self.finish_reply(reply_key,raw,interrupted=True)
        except asyncio.CancelledError:raise
        except Exception as exc:
            logger.warning('conversation_transport_failed task=%s kind=%s',key,type(exc).__name__)
            if isinstance(exc,AgentFailure) and exc.code in ('upstream_http_error','upstream_protocol_error'):
                future=self.members[key]['future']
                if not future.done():future.set_exception(exc)
            else:self._stage('awaiting_reply')

    def append_reply(self,identity,raw):
        entry=self.replies.get(identity)
        if entry and entry.get('snapshot') and not entry['snapshot'].startswith(raw):
            # A new reply can begin with the same words as a prior one. Delay
            # only the common prefix, never discard a distinct continuation.
            entry=None;self.replies.pop(identity,None)
        if entry is None:
            matches=[(body,key) for body,key in self.snapshots.items() if body.startswith(raw)]
            if len(matches)==1:
                body,key=matches[0]
                self.replies[identity]={'key':key,'raw':raw,'snapshot':body}
                return key
            pending=self._pending()
            if not pending:return None
            owner=pending[-1];title=self.store.get(self.scope,owner)['title'] if len(pending)==1 else '刚才几个问题的回复'
            key=self.store.create(self.scope,'Being会话回复',title)
            self.store.update(self.scope,key,kind='reply',stage='text')
            entry=self.replies[identity]={'key':key,'raw':''}
        entry['raw']=raw
        if entry['key'] in self.finalized:return entry['key']
        text,_=split_reply(raw)
        self.store.update(self.scope,entry['key'],text=text)
        self._stage('awaiting_reply')
        return entry['key']

    def finish_reply(self,key,raw,interrupted=False):
        if not key or key in self.finalized:return
        text,coverage=split_reply(raw,final=True)
        digest=hashlib.sha256(raw.encode()).hexdigest()
        if not interrupted:self.seen.add(digest)
        old=self.store.get(self.scope,key)
        delivery=old['delivery'] if old['delivery'] in ('paused','deferred','awaiting_choice','reported','reporting','pending') else 'waiting'
        self.store.update(self.scope,key,text=text,state='error' if interrupted else 'completed',
                          stage='error' if interrupted else 'completed',
                          summary='原文摘录：'+text[:235] if text else '',
                          delivery=delivery if text else 'reported')
        if interrupted:return
        self.finalized.add(key)
        pending=set(self._pending())
        valid=coverage is not None and all(v in self.members and self.members[v]['sent'] for v in coverage['answered']+coverage['pending'])
        answered=set(coverage['answered']) & pending if valid and text.strip() else set()
        for member in pending:
            self.store.update(self.scope,member,reply_task_id=key,
                summary='已有会话回复；该事项是否答完尚未确认' if member not in answered else '已回答；完整内容保存在关联会话回复中')
            if member in answered:self.members[member]['future'].set_result({'reply_task_id':key})
        logger.info('conversation_reply reply=%s covered=%d pending=%d attributed=%s',key,len(answered),len(pending-answered),valid)

    async def _poll(self):
        try:
            while not self.closed:
                await asyncio.sleep(self.poll_interval)
                path='/api/history?limit=100'+(f'&after={self.cursor}' if self.cursor is not None else '')
                try:
                    async with self.client_factory(timeout=15,follow_redirects=False) as client:
                        r=await client.get(self.bridge.endpoint(path))
                    if r.status_code!=200:continue
                    for m in sorted(r.json().get('messages',[]),key=lambda v:int(v.get('seq',0))):
                        seq=int(m.get('seq',0))
                        if self.cursor is not None and seq<=self.cursor:continue
                        self.cursor=max(self.cursor or 0,seq)
                        if m.get('scene_id')!=self.scene or m.get('role') not in ('being','assistant'):continue
                        raw=m.get('content','')
                        if not isinstance(raw,str) or not raw or raw.startswith('[breath '):continue
                        digest=hashlib.sha256(raw.encode()).hexdigest()
                        if digest in self.seen:continue
                        partial=next((v for v in self.replies.values() if v['raw'] and raw.startswith(v['raw']) and
                                      v['key'] not in self.finalized),None)
                        key=partial['key'] if partial else self.append_reply(('history',seq),raw)
                        self.snapshots[raw]=key
                        self.finish_reply(key,raw)
                except (httpx.HTTPError,ValueError,TypeError):continue
        except asyncio.CancelledError:raise

    async def close(self):
        if self.closed:return
        self.closed=True
        jobs=list(self.transports)+[job for job in (self.poller,self.preflight) if job]
        for job in jobs:job.cancel()
        await asyncio.gather(*jobs,return_exceptions=True)
        for entry in self.replies.values():
            row=self.store.get(self.scope,entry['key'])
            if row['state']=='running':self.finish_reply(entry['key'],entry['raw'],interrupted=True)
