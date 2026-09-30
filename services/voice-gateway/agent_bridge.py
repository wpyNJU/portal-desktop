"""Loom SSE adapter. Credentials stay on the server; only final text leaves this module."""
import asyncio,json,time,uuid
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit
import httpx
from agent_errors import AgentFailure

async def sse_events(response):
    event='message';data=[]
    async for line in response.aiter_lines():
        if not line:
            if data:
                yield event,json.loads('\n'.join(data))
            event='message';data=[]
        elif line.startswith('event:'):event=line[6:].strip()
        elif line.startswith('data:'):data.append(line[5:].lstrip())
    if data:yield event,json.loads('\n'.join(data))

class AgentBridge:
    def __init__(self,path=None,*,url=None):
        self.url=url if url is not None else Path(path).read_text().strip()
        parsed=urlsplit(self.url)
        self.base=parsed.path.removesuffix('/api/chat/stream')
        self.parsed=parsed
    def endpoint(self,path):
        parsed=urlsplit(path)
        query='&'.join(x for x in [self.parsed.query,parsed.query] if x)
        return urlunsplit((self.parsed.scheme,self.parsed.netloc,self.base+parsed.path,query,''))
    async def verify_access(self):
        try:
            async with httpx.AsyncClient(timeout=8,follow_redirects=False) as client:
                response=await client.get(self.endpoint('/api/history?limit=1'))
            if response.status_code in (401,403):raise ValueError('Agent token 无效或已失效，请重新填写链接')
            if response.status_code!=200:raise ValueError('Agent 暂时无法验证，请稍后重试')
            data=response.json()
            if not isinstance(data,dict) or not isinstance(data.get('messages'),list):raise ValueError('Agent 验证响应异常，请检查链接')
        except (httpx.HTTPError,ValueError) as e:
            if isinstance(e,ValueError) and str(e).startswith('Agent '):raise
            raise ValueError('Agent 验证失败，请检查链接或稍后重试') from None
    async def stream(self,text,context,stop,emit,stage=None):
        scene=context['scene']+'-request-'+uuid.uuid4().hex;owned_stream=None;finished=False
        seen_events=set()
        def report(name):
            if stage:stage(name)
        async with httpx.AsyncClient(timeout=httpx.Timeout(300,connect=15),follow_redirects=False) as client:
            async def consume():
                nonlocal owned_stream,finished
                # Cursor is for queued replies only; unrelated history is never sent to the browser.
                cursor=None
                report('history_start')
                try:
                    # This is only a fallback cursor, not the actual chat request.
                    # A slow history endpoint must not hold up a live voice turn.
                    r=await asyncio.wait_for(client.get(self.endpoint('/api/history?limit=5'),timeout=2),timeout=2)
                    if r.status_code==200:cursor=max([int(m.get('seq',0)) for m in r.json().get('messages',[])]+[0])
                except (httpx.HTTPError,ValueError,TimeoutError):pass
                report('request_start')
                # Doubao resolves references in the tool message; saved call history stays in its session.
                body={'message':text,'scene_id':scene,'session_id':context.get('session_id'),
                      'scene_meta':{'client':'voice-call/1.0','scene_label':'语音通话'}}
                async with client.stream('POST',self.url,json=body) as response:
                    if response.status_code==202:
                        report('queued')
                        await response.aread()
                        # A missing optimization cursor must not turn an accepted
                        # request into a failure. Recover by its unique scene below.
                    elif response.status_code!=200:
                        report('http_'+str(response.status_code))
                        raise AgentFailure('upstream_http_error')
                    else:
                        report('connected')
                        if 'text/event-stream' not in response.headers.get('content-type',''):
                            raise AgentFailure('upstream_protocol_error')
                        received_text=False
                        async for event,data in sse_events(response):
                            if event=='meta':
                                if data.get('scene_id')==scene:
                                    owned_stream=data.get('stream_id');report('accepted')
                                continue
                            if event=='error':
                                if data.get('scene_id') and data['scene_id']!=scene:continue
                                if owned_stream and data.get('stream_id') and data['stream_id']!=owned_stream:continue
                                raise AgentFailure('upstream_error')
                            if data.get('scene_id')!=scene:continue
                            if owned_stream and data.get('stream_id') and data['stream_id']!=owned_stream:continue
                            event_id=data.get('event_id')
                            if event_id is not None:
                                key=(event,str(event_id))
                                if key in seen_events:continue
                                seen_events.add(key)
                            if event=='reasoning':report('thinking')
                            block=data.get('content_block') or {}
                            delta=data.get('delta') or {}
                            if event=='content_block_start' and block.get('type') in ('thinking','redacted_thinking'):report('thinking')
                            if event=='content_block_delta' and delta.get('type')=='thinking_delta':report('thinking')
                            if event=='content_block_start' and block.get('type')=='tool_use':report('tool')
                            if event=='content_block_delta' and delta.get('type')=='input_json_delta':report('tool')
                            if event=='content_block_delta':
                                piece=data.get('delta',{}).get('text','')
                                if isinstance(piece,str) and piece:received_text=True;report('text');emit(piece)
                            elif event=='message_stop':
                                if data.get('session_id'):context['session_id']=data['session_id']
                                # Heart may stop an empty/thinking reply then continue.
                                # Do not close the stream before the actual text arrives.
                                if received_text:finished=True;return
                                report('awaiting_reply')
                        if received_text:raise AgentFailure('upstream_connection_error')
                        report('awaiting_reply')
                # A 202 is an accepted, queued message: never resubmit it (could duplicate actions).
                deadline=time.monotonic()+540
                while time.monotonic()<deadline:
                    await asyncio.sleep(1.5)
                    path='/api/history?limit=100'+(f'&after={cursor}' if cursor is not None else '')
                    try:r=await client.get(self.endpoint(path),timeout=15)
                    except httpx.HTTPError:continue
                    if r.status_code!=200:continue
                    yielded=False
                    for m in sorted(r.json().get('messages',[]),key=lambda m:int(m.get('seq',0))):
                        seq=int(m.get('seq',0))
                        if cursor is not None and seq<=cursor:continue
                        cursor=max(cursor or 0,seq)
                        if m.get('scene_id')==scene and m.get('from')=='system' and m.get('content')=='[breath yielded to human]':
                            yielded=True
                        if m.get('scene_id')==scene and m.get('role') in ('being','assistant'):
                            content=m.get('content','')
                            if isinstance(content,str) and content and not content.startswith('[breath '):
                                report('text');emit(content);finished=True;return
                    if yielded:raise AgentFailure('upstream_interrupted')
                raise AgentFailure('reply_timeout')
            task=asyncio.create_task(consume())
            try:
                while not task.done():
                    if stop.is_set():
                        task.cancel()
                        await asyncio.gather(task,return_exceptions=True)
                        if owned_stream and not finished:
                            try:
                                await client.post(self.endpoint('/api/stop'),json={'stream_id':owned_stream},timeout=5)
                            except httpx.HTTPError:pass
                        return
                    await asyncio.wait([task],timeout=.1)
                await task
            except httpx.HTTPError:
                # Exception strings contain the credential-bearing request URL. Never forward them.
                raise AgentFailure('upstream_connection_error') from None
            finally:
                if not task.done():task.cancel()
                await asyncio.gather(task,return_exceptions=True)
