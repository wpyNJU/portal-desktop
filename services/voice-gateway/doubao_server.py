import asyncio,base64,json,logging,threading,time,uuid,re
from fastapi import FastAPI,WebSocket,WebSocketDisconnect
import websockets
from agent_bridge import AgentBridge
from agent_endpoint import parse_endpoint
from agent_stream_voice import AgentStreamVoice
from task_store import TaskStore
from background_tasks import BackgroundTasks
from task_reports import TaskReports
from task_context import TaskContextSync
from turn_scheduler import TurnScheduler,result_notice,IdleCallTimer
from speech_output import SpeechOutput
from conversation_memory import normalize_history,history_instructions
from doubao_config import ROOT,URL,headers,create_session,VOICES,DEFAULT_VOICE
from voice_usage import UsageCapture,call_id as usage_call_id,write as write_usage
from doubao_profile import fetch_profile,session_instructions,load_profile,save_profile,profile_updated_at

profile_update_lock=asyncio.Lock()
ROOT.mkdir(parents=True,exist_ok=True)
(ROOT/'logs').mkdir(exist_ok=True)
app=FastAPI();bridge=AgentBridge(ROOT/'agent-url.txt') if (ROOT/'agent-url.txt').is_file() else None
task_store=TaskStore(ROOT/'agent-tasks.sqlite3');task_store.recover()
background_tasks=BackgroundTasks(task_store)

@app.on_event('shutdown')
async def shutdown_background():await background_tasks.shutdown()
logging.getLogger('httpx').setLevel(logging.WARNING)
logger=logging.getLogger('voice.connection');logger.setLevel(logging.INFO)
if not logger.handlers:
    handler=logging.StreamHandler();handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    logger.addHandler(handler)
logger.propagate=False

async def speak_opening(text,client,stop,valid,voice=DEFAULT_VOICE):
    # Isolate acknowledgement synthesis: injecting it into the main dialogue
    # consumes its pending function-call response on the current provider version.
    got_audio=False;usage=UsageCapture('report_speech')
    try:
        async with asyncio.timeout(12):
            async with websockets.connect(URL,additional_headers=headers(),open_timeout=5,max_size=4_000_000) as voice_ws:
                async def send(d):await voice_ws.send(json.dumps(d,ensure_ascii=False))
                await send(create_session(instructions='只朗读指定文字，不添加内容。',tools=[],voice=voice))
                if json.loads(await voice_ws.recv()).get('type')!='session.created':return
                try:
                    await send({'type':'input_audio_mute.commit'})
                    await send({'type':'speech_text_buffer.commit','text':text})
                    while not stop.is_set() and valid():
                        d=json.loads(await voice_ws.recv());usage.observe(d)
                        if d.get('type')=='response.output_audio.delta':
                            got_audio=True
                            if not stop.is_set() and valid():await client({'type':'opening.audio','delta':d['delta']})
                        if d.get('type')=='response.output_audio.done':return got_audio
                        if d.get('type')=='error':return False
                finally:
                    await send({'type':'session.close'})
                    try:
                        async with asyncio.timeout(.75):
                            async for raw in voice_ws:
                                event=json.loads(raw);usage.observe(event)
                                if event.get('type')=='session.closed':break
                    except (TimeoutError,websockets.ConnectionClosed):pass
    except Exception:
        logging.warning('optional_opening_unavailable')

@app.get('/health')
def health():return {'ready':(ROOT/'doubao-api-key.txt').is_file(),'voice':'Doubao Seeduplex 3.0','agent':'Loom SSE','mode':'duplex'}

AGENT_TIMEOUT=300
AGENT_MAX_WAIT=600
TOOL_REPLY_TIMEOUT=12

def query_identity(item):
    try:
        args=json.loads(item.get('arguments','{}'))
        message=args.get('message','')
        if not isinstance(message,str) or not message.strip():return None,False
    except (ValueError,TypeError):return None,False
    normalized=re.sub(r'[\W_]+','',message.lower())
    # Only collapse generic task-list questions. Names, dates, filters and actions stay distinct.
    residue=re.sub(r'请问|请|帮我|帮忙|查询|查一下|查看|看看|告诉我|列出|一下|当前|现在|目前|正在|在做|进行中|进行|有什么|有哪些|什么|哪些|任务|工作|列表|状态|情况|进度|我的|你|我|的|了|吗|呢|在|做|有', '', normalized)
    overview=('任务' in normalized or '工作' in normalized) and not residue
    return ('task-overview' if overview else normalized),bool(overview)

class ToolCalls:
    def __init__(self,up,client,context=None,voice=DEFAULT_VOICE,agent=None,manager=None):
        self.manager=manager if manager is not None else background_tasks;self.closed=False;self.watcher=None
        self.voice=voice;self.bridge=agent if agent is not None else bridge
        self.up=up;self.client=client;self.epoch=0;self.pending=[];self.seen=set()
        self.response_ready=asyncio.Event();self.opening_stops=[];self.queries={};self.relays=set();self.scheduler=TurnScheduler()
        self.context=context if context is not None else {'scene':'voice-'+str(uuid.uuid4())}
        self.scope=self.context.get('scope',self.context['scene']);self.task_ids={}
        self.audio=SpeechOutput(self.scheduler,self.client)
        async def speak(text,client,stop,valid):return await speak_opening(text,client,stop,valid,voice=self.voice)
        self.reports=TaskReports(task_store,self.scope,self.scheduler,self.client,speak,output=self.audio)
    def interrupt(self,cancel_queries=False):
        self.scheduler.interrupt()
        for opening_stop in self.opening_stops:opening_stop.set()
        for relay in tuple(self.relays):relay.interrupt_audio()
        if not cancel_queries:return
        if any(not task.done() for task,stop in self.pending):logger.info('agent_interrupted scene=%s',self.context['scene'])
        self.epoch+=1
        for task,stop in self.pending:stop.set()
    async def watch(self):
        offsets={};versions={};finished=set();last_progress={}
        labels={'queued':'Agent 已接收，仍在等待回复','accepted':'Agent 已接收请求','thinking':'正在分析你的问题','tool':'正在调用工具获取信息','text':'正在生成回答'}
        while not self.closed:
            tracked={r['id'] for r in task_store.watchable(self.scope)}|set(offsets)
            for key in sorted(tracked,key=lambda k:task_store.get(self.scope,k)['requested_at'],reverse=True):
                brief={'id':key}
                key=brief['id'];row=task_store.get(self.scope,key)
                if versions.get(key)!=row['updated']:
                    await self.reports.event(key);versions[key]=row['updated']
                offset=offsets.get(key,0)
                # Reattach only unfinished/unreported tasks; historical completed reports stay in the task panel.
                attached=(row['state']=='running' or row['delivery']!='reported' or key in offsets)
                if attached and len(row['text'])>offset:
                    await self.client({'type':'agent.text.delta','call_id':key,'task_id':key,'delta':row['text'][offset:],
                                       'offset':len(row['text'][:offset].encode('utf-16-le'))//2})
                    offsets[key]=len(row['text'])
                progress=self.manager.progress.get(key)
                if progress and row['state']=='running':progress=(progress[0],max(0,round(time.time()-row['created'])))
                if progress and progress!=last_progress.get(key) and progress[0] in labels:
                    last_progress[key]=progress
                    await self.client({'type':'agent.progress','call_id':key,'text':labels[progress[0]],'elapsed':progress[1]})
                unread=row['text'][row['cursor']:]
                if row['delivery']=='waiting' and row['text']:
                    if row['state']!='running' or re.search(r'[。！？!?；;\n]',unread) or len(unread)>=160:
                        if row['state']=='completed' and not unread:
                            task_store.update(self.scope,key,delivery='reported');await self.reports.event(key)
                        else:self.reports.start(key,notice=row['state']!='running' and not unread,requested=False)
                if row['state']!='running' and key not in finished:
                    finished.add(key)
                    if attached:
                        await self.client({'type':'agent.text.done' if row['state']=='completed' else 'agent.text.error','call_id':key,'message':'查询未完整结束，已保留收到的内容，未自动重试'})
                    if not row['text'] and row['delivery']=='waiting':
                        self.reports.start(key,notice=True,requested=False)
            await asyncio.sleep(.25)

    async def run(self,items,epoch,stop):
        results=[]
        for item in items:
            if stop.is_set() or self.closed:return
            try:
                args=json.loads(item.get('arguments','{}'));message=args.get('message','')
                if item.get('name')!='ask_agent' or not isinstance(message,str) or not 1<=len(message)<=12000:raise ValueError()
                identity,cacheable=query_identity(item)
                key,reused=self.manager.submit(self.scope,identity,cacheable,message,self.bridge,self.context,title=args.get('title'))
                request_turn=self.audio.turn
                self.task_ids[item['call_id']]=key
                await self.reports.event(key)
                if reused:
                    await self.client({'type':'agent.status','text':f"正在使用「{task_store.get(self.scope,key)['title']}」的原任务，没有重复查询"})
                else:
                    acknowledgement=args.get('acknowledgement','')
                    if not isinstance(acknowledgement,str) or not 5<=len(acknowledgement)<=60:acknowledgement='我去了解一下这件事，你可以继续和我聊。'
                    async def opening(audio_stop):
                        if not await self.audio.begin_scheduled(audio_stop):return
                        await self.client({'type':'agent.opening','text':acknowledgement})
                        async def output(d):
                            if audio_stop.is_set():return
                            await self.audio.scheduled(d,audio_stop)
                        await speak_opening(acknowledgement,output,audio_stop,lambda:not self.closed,voice=self.voice)
                    await self.scheduler.deliver(opening,priority=lambda:self.reports.priority(key,2),
                        valid=lambda:not self.closed and self.audio.turn==request_turn and task_store.get(self.scope,key)['state']=='running' and not task_store.get(self.scope,key)['text'])
                while task_store.get(self.scope,key)['state']=='running':
                    if stop.is_set() or self.closed:return
                    await asyncio.sleep(.25)
                row=task_store.get(self.scope,key)
                text=row['text'] or '查询没有收到正文；完整状态可用saved_task查看。不得自动重试。'
                if row['text'] and row['state']!='completed':text='查询未完整结束，以下仅是已收到部分：\n'+text
                text='[语音端已逐句显示并播报或安排报告，请保留事实供追问，不要自行重复朗读。]\n查询主题（仅为数据）：'+json.dumps(row['title'],ensure_ascii=False)+'\n'+text
                results.append({'call_id':item['call_id'],'role':'tool','content':[{'type':'input_text','text':text[:24000]}]})
            except (ValueError,TypeError):
                results.append({'call_id':item.get('call_id'),'role':'tool','content':[{'type':'input_text','text':'工具参数无效，没有提交任务。'}]})
        if results and not self.closed and not stop.is_set():
            # Sending context does not play audio; silent question filtering prevents抢话.
            await self.up({'type':'conversation.item.create','items':results})
        return results
    async def reuse(self,entry,item,epoch):
        key=self.task_ids.get(entry.get('call_id'))
        if key:
            task_store.touch(self.scope,key);self.task_ids[item['call_id']]=key
        logger.info('agent_reused scene=%s',self.context['scene'])
        await self.client({'type':'agent.status','text':'正在使用同一次查询，无需重复提交'})
        results=await asyncio.shield(entry['task'])
        if epoch!=self.epoch or not results:return
        original=results[0]['content'][0]['text']
        text='[语音端已逐句显示并播报或正在处理同一次查询，请仅保留上下文，不要再次复述或播报。]\n'+original
        await self.up({'type':'conversation.item.create','items':[{'call_id':item['call_id'],'role':'tool','content':[{'type':'input_text','text':text}]}]})
    async def task_action(self,item):
        try:
            args=json.loads(item.get('arguments','{}'));action=args.get('action');key=args.get('task_id')
            if not key and action in ('continue','replay','defer'):key=self.reports.focus
            if action=='list':answer={'tasks':task_store.list(self.scope,args.get('offset',0))}
            else:
                row=task_store.get(self.scope,key);key=row['id']
                if action=='get':
                    task_store.touch(self.scope,key);answer=task_store.page(self.scope,key,args.get('offset',0))
                elif action in ('continue','replay'):answer={'message':self.reports.start(key,restart=action=='replay'),'task_id':key,'title':row['title']}
                elif action=='defer':answer={'message':await self.reports.defer(key),'task_id':key,'title':row['title']}
                else:raise ValueError('不支持的任务操作')
            text=json.dumps(answer,ensure_ascii=False)
            if action in ('continue','replay') and answer.get('message','').startswith(('已安排','报告已经在')):text='[语音端已逐句显示并播报或安排完整报告，请静默等待，不要自行复述。]'+text
        except (ValueError,TypeError):text='没有找到对应任务或参数无效，请先列出已保存任务。'
        await self.up({'type':'conversation.item.create','items':[{'call_id':item['call_id'],'role':'tool','content':[{'type':'input_text','text':text}]}]})
    def submit(self,items):
        now=time.monotonic()
        self.queries={k:v for k,v in self.queries.items() if not v['task'].done() or now-v['finished']<60}
        self.pending=[(t,s) for t,s in self.pending if not t.done()]
        for item in items:
            call_id=item.get('call_id')
            if not isinstance(call_id,str) or not call_id or call_id in self.seen:continue
            self.seen.add(call_id)
            if item.get('name')=='saved_task':
                task=asyncio.create_task(self.task_action(item));self.pending.append((task,threading.Event()));continue
            key,cacheable=query_identity(item) if item.get('name')=='ask_agent' else (None,False)
            entry=self.queries.get(key) if key else None
            if entry and (not entry['task'].done() or (cacheable and entry['ok'])):
                task=asyncio.create_task(self.reuse(entry,item,self.epoch))
                self.pending.append((task,entry['stop']))
                continue
            stop=threading.Event();task=asyncio.create_task(self.run([item],self.epoch,stop))
            self.pending.append((task,stop))
            if key:
                entry={'task':task,'stop':stop,'finished':float('inf'),'ok':False,'call_id':call_id}
                self.queries[key]=entry
                def done(t,record=entry):
                    record['finished']=time.monotonic()
                    if not t.cancelled() and t.exception() is None:
                        results=t.result() or []
                        record['ok']=bool(results and results[0]['content'][0]['text'].startswith('[语音端已逐句显示并播报'))
                task.add_done_callback(done)
    async def close(self):
        if self.closed:return
        self.closed=True
        self.scheduler.close()
        await self.reports.close()
        self.interrupt(cancel_queries=True)
        tasks=[t for t,s in self.pending]
        if self.watcher:tasks.append(self.watcher)
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        # BackgroundTasks owns the actual requests; none are cancelled here.

@app.websocket('/ws')
async def connection(ws:WebSocket):
    await ws.accept();tools=None;receiving=None;context_sync=None
    connection_id=uuid.uuid4().hex[:10];started_at=time.monotonic()
    usage_token=usage_call_id.set(connection_id);usage=UsageCapture('dialogue');usage_started=None
    lock=asyncio.Lock();output=asyncio.Lock()
    first_asr_at=None;first_audio_logged=False
    async def send_client(data):
        nonlocal first_asr_at,first_audio_logged
        if data.get('type')=='conversation.item.input_audio_transcription.completed' and first_asr_at is None:
            first_asr_at=time.monotonic()
        if data.get('type') in ('response.output_audio.delta','agent.audio.delta','opening.audio') and not first_audio_logged:
            first_audio_logged=True;now=time.monotonic()
            logger.info('first_reply id=%s asr_to_audio_ms=%d ready_to_audio_ms=%d context_updates=%d',
                connection_id,round((now-first_asr_at)*1000) if first_asr_at else -1,
                round((now-usage_started)*1000) if usage_started else -1,context_sync.updates if context_sync else 0)
        async with output:await ws.send_json(data)
    try:
        context={'scene':'voice-'+str(uuid.uuid4())}
        try:
            initial_request=await asyncio.wait_for(ws.receive_json(),5)
        except TimeoutError:initial_request={}
        if initial_request.get('type')=='session.close':return
        try:agent_url,scope=parse_endpoint(initial_request.get('agent_url'))
        except ValueError as e:
            await send_client({'type':'error','message':str(e)});return
        context['scope']=scope
        selected_bridge=AgentBridge(url=agent_url)
        try:await selected_bridge.verify_access()
        except ValueError as e:
            await send_client({'type':'error','message':str(e)});return
        if initial_request.get('type') in ('tasks.list','tasks.get'):
            try:
                if initial_request['type']=='tasks.list':data={'tasks':task_store.list(scope,initial_request.get('offset',0))}
                else:data={'task':task_store.page(scope,initial_request.get('task_id'),initial_request.get('offset',0),8000)}
                await send_client({'type':'tasks.result',**data})
            except (ValueError,TypeError):await send_client({'type':'error','message':'任务记录不存在或参数无效'})
            return
        legacy=bool(bridge and agent_url==parse_endpoint(bridge.url)[0])
        profile_path=ROOT/('assistant-profile-'+scope+'.json')
        if legacy and not profile_path.exists():profile_path=ROOT/'assistant-profile.json'
        if initial_request.get('type')=='agent.configure':
            await send_client({'type':'agent.configured','scope':scope,'legacy':legacy,'endpoint':selected_bridge.parsed.path});return
        if initial_request.get('type')=='profile.status':
            await send_client({'type':'profile.status','updated_at':profile_updated_at(profile_path)});return
        if initial_request.get('type')=='profile.refresh':
            if profile_update_lock.locked():
                await send_client({'type':'error','message':'助手资料正在更新，请稍后再试'});return
            async with profile_update_lock:
                await send_client({'type':'session.status','message':'正在向 Agent 更新助手资料'})
                try:
                    profile=await fetch_profile(selected_bridge,context)
                    save_profile(profile,profile_path)
                    await send_client({'type':'profile.updated','message':'助手资料已保存，下次通话生效','updated_at':profile_updated_at(profile_path)})
                except Exception as e:
                    logger.warning('profile_update_failed kind=%s',type(e).__name__)
                    await send_client({'type':'error','message':'更新未完成，已保留原有资料'})
            return
        if initial_request.get('type')=='session.close':return
        voice=initial_request.get('voice',DEFAULT_VOICE)
        if not isinstance(voice,str) or voice not in VOICES:voice=DEFAULT_VOICE
        context['history']=normalize_history(initial_request.get('history',[]))
        profile=load_profile(profile_path)
        await send_client({'type':'session.status','message':'正在连接语音，使用已保存资料' if profile else '正在连接语音；可在设置中主动更新助手资料'})
        async with websockets.connect(URL,additional_headers=headers(),open_timeout=15,max_size=4_000_000) as remote:
            async def send_up(data):
                data.setdefault('event_id',str(uuid.uuid4()))
                async with lock:await remote.send(json.dumps(data,ensure_ascii=False))
            async def update_context(instructions):
                update=create_session(instructions=instructions,voice=voice);update['type']='session.update'
                await send_up(update)
            context_sync=TaskContextSync(task_store,scope,
                base_instructions=lambda:session_instructions(profile)+history_instructions(context['history']),
                focus=lambda:tools.reports.focus if tools else task_store.resumable(scope),
                send=update_context,
                can_update=lambda:tools is not None and tools.audio.turn>0 and tools.scheduler.idle() and not tools.scheduler.lock.locked())
            instructions=context_sync.instructions()
            await send_up(create_session(instructions=instructions,voice=voice))
            initial=json.loads(await asyncio.wait_for(remote.recv(),20))
            if initial.get('type')!='session.created':
                await send_client({'type':'error','message':'豆包会话建立失败，请检查服务权限或音色配置'});return
            usage_started=time.monotonic();write_usage('call_started')
            await send_client(initial)
            logger.info('ready id=%s elapsed=%.2f restored_tasks=%d prompt_chars=%d',connection_id,time.monotonic()-started_at,context_sync.restored,len(instructions))
            current_question=None;previous_question=None;turn_interrupted=False;blocked=set();tool_questions={};streamed_questions=set();voice_text={}
            async def send_tool_result(data):
                # The provider may attach a tool continuation to its original question.
                # New speech may stop playback, but must not hide a completed query.
                for item in data.get('items',[]):
                    if any(c.get('text','').startswith('[语音端已逐句显示并播报') for c in item.get('content',[])):
                        question=tool_questions.get(item.get('call_id'))
                        if question:
                            streamed_questions.add(question)
                            await tools.audio.block(question=question)
                await send_up(data)
            async def task_client(data):
                if data.get('type')=='task.updated':context_sync.observe(data['task'])
                await send_client(data)
            tools=ToolCalls(send_tool_result,task_client,context=context,voice=voice,agent=selected_bridge)
            tools.watcher=asyncio.create_task(tools.watch())
            async def downstream():
                nonlocal current_question,previous_question,turn_interrupted
                finished_inputs=set();input_open=False
                async for raw in remote:
                    d=json.loads(raw);usage.observe(d);d=tools.audio.observe(d);kind=d.get('type','')
                    if kind.startswith('conversation.item.input_audio_transcription.') and d.get('item_id') in finished_inputs:continue
                    spoken=d.get('delta') or d.get('text') or ''
                    is_transcript=kind in ('conversation.item.input_audio_transcription.delta','conversation.item.input_audio_transcription.completed')
                    begins_input=(kind=='conversation.item.input_audio_transcription.started' or
                                  (is_transcript and isinstance(spoken,str) and bool(spoken.strip())))
                    if begins_input and (not turn_interrupted or (d.get('item_id') and d['item_id']!=current_question) or
                                         (not d.get('item_id') and not input_open)):
                        tools.scheduler.update(user=True,generating=False)
                        previous_question=current_question;current_question=d.get('item_id');turn_interrupted=True
                        input_open=True
                        if previous_question:finished_inputs.add(previous_question)
                        voice_text.clear()
                        if previous_question:blocked.add(previous_question)
                        tools.interrupt()
                        await tools.audio.user_started(current_question)
                    if is_transcript:
                        if kind.endswith('.delta'):tools.scheduler.update(user=True)
                    if kind=='conversation.item.input_audio_transcription.completed':
                        input_open=False
                        if d.get('item_id'):finished_inputs.add(d['item_id'])
                        tools.scheduler.update(user=False,generating=True)
                        if isinstance(d.get('text'),str):context['history']=normalize_history(context['history']+[{'role':'user','text':d['text']}])
                    if kind=='conversation.item.input_audio_transcription.failed':
                        input_open=False
                        if d.get('item_id'):finished_inputs.add(d['item_id'])
                        tools.scheduler.update(user=False,generating=False)
                    if kind=='response.function_call_arguments.done':
                        tools.scheduler.update(generating=False)
                        items=d.get('items',[])
                        # Hand-off owns this turn immediately, including the provider's acknowledgement.
                        handled=any(i.get('name') in ('ask_agent','end_call') for i in items)
                        for item in items:
                            if item.get('name')=='saved_task':
                                try:handled=handled or json.loads(item.get('arguments','{}')).get('action') in ('continue','replay')
                                except (ValueError,TypeError,AttributeError):pass
                        if handled:
                            question=d.get('question_id') or current_question
                            if question:streamed_questions.add(question)
                            await tools.audio.block(d,question=question)
                        goodbye=next((i for i in items if i.get('name')=='end_call'),None)
                        if goodbye:
                            tools.interrupt(cancel_queries=True)
                            try:
                                farewell=json.loads(goodbye.get('arguments','{}')).get('farewell','')
                            except (ValueError,AttributeError):farewell=''
                            if not isinstance(farewell,str) or not 1<=len(farewell)<=40:farewell='再见，下次聊。'
                            await send_client({'type':'call.ending','text':farewell})
                            await send_up({'type':'input_audio_mute.commit'})
                            farewell_stop=threading.Event()
                            await tools.audio.begin_scheduled(farewell_stop)
                            async def farewell_output(event):await tools.audio.scheduled(event,farewell_stop)
                            await speak_opening(farewell,farewell_output,farewell_stop,lambda:True,voice=voice)
                            await send_client({'type':'call.end'})
                            continue
                        for item in items:tool_questions[item.get('call_id')]=d.get('question_id') or current_question
                        tools.submit(items);continue
                    if kind.startswith('response.output_') and d.get('question_id') in (blocked|streamed_questions):continue
                    if kind.startswith('response.') and tools.audio.is_blocked(d):continue
                    if kind=='response.output_text.delta':voice_text.setdefault(d.get('response_id') or current_question,[]).append(d.get('delta',''))
                    if kind=='response.done':
                        completed=voice_text.pop(d.get('response_id') or current_question,[])
                        if completed:context['history']=normalize_history(context['history']+[{'role':'assistant','text':''.join(completed)}])
                    if kind=='response.output_audio.delta':
                        tools.response_ready.set()
                    if kind=='error':
                        logging.warning('doubao_error code=%s',d.get('error',{}).get('code') if isinstance(d.get('error'),dict) else d.get('code'))
                        await send_client({'type':'error','message':'豆包语音返回错误，请重新开始通话'});continue
                    if await tools.audio.provider(d,observed=True):await send_client(d)
                    if kind=='session.closed':return
            receiving=asyncio.create_task(downstream())
            async def upstream():
                while True:
                    data=await ws.receive_json();kind=data.get('type')
                    if kind=='input_audio_buffer.append':
                        audio=data.get('audio','')
                        if not isinstance(audio,str) or len(audio)>2000:raise ValueError('audio frame')
                        if len(base64.b64decode(audio,validate=True))!=640:raise ValueError('audio size')
                        await send_up({'type':kind,'audio':audio})
                    elif kind in ('input_audio_mute.commit','input_audio_unmute.commit','input_audio_buffer.commit'):
                        await send_up({'type':kind})
                    elif kind=='text.message':
                        await send_client({'type':'text.rejected','message':'文字输入已关闭，请刷新页面使用语音交流'});continue
                    elif kind=='task.segment.played':
                        tools.reports.acknowledge(data.get('token'))
                    elif kind=='task.pause':
                        try:await tools.reports.defer(task_store.get(scope,data.get('task_id'))['id'])
                        except ValueError:await send_client({'type':'agent.status','text':'没有找到这条任务'})
                    elif kind=='task.report':
                        try:
                            row=task_store.get(scope,data.get('task_id'))
                            message=tools.reports.start(row['id'],restart=data.get('restart') is True)
                            await send_client({'type':'agent.status','text':message})
                        except ValueError:await send_client({'type':'agent.status','text':'没有找到这条任务'})
                    elif kind=='client.playback':
                        tools.audio.playback(data)
                    elif kind=='response.cancel':
                        tools.interrupt()
                        if current_question:blocked.add(current_question)
                        await tools.audio.user_started();await send_up({'type':kind})
                    elif kind=='client.diagnostic':
                        if data.get('reason')=='audio_backpressure':
                            amount=data.get('buffered_bytes')
                            if isinstance(amount,int):logger.warning('client_backpressure id=%s bytes=%d',connection_id,min(max(amount,0),10000000))
                    elif kind=='session.close':return
            async def watch_idle():
                timer=IdleCallTimer(time.monotonic())
                while True:
                    await asyncio.sleep(.25)
                    scheduler=tools.scheduler
                    busy=(scheduler.user or scheduler.generating or scheduler.playing or scheduler.lock.locked()
                          or tools.reports.busy() or tools.manager.busy(scope) or any(not task.done() for task,stop in tools.pending))
                    if timer.expired(time.monotonic(),busy=busy,last_activity=scheduler.changed):
                        logger.info('idle_hangup id=%s timeout=30',connection_id)
                        await send_client({'type':'call.end','reason':'idle_timeout','message':'空闲超过 30 秒，通话已自动结束'})
                        return
            sending=asyncio.create_task(upstream())
            idle_watch=asyncio.create_task(watch_idle())
            syncing=asyncio.create_task(context_sync.run())
            try:
                done,_=await asyncio.wait([receiving,sending,idle_watch,tools.watcher,syncing],return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    try:task.result()
                    except WebSocketDisconnect as e:logger.info('phone_disconnected id=%s code=%s',connection_id,e.code)
                    except websockets.ConnectionClosed as e:logger.warning('provider_disconnected id=%s code=%s',connection_id,e.rcvd.code if e.rcvd else 'no_close_frame')
                    except Exception as e:logger.warning('stream_failed id=%s direction=%s kind=%s',connection_id,'context' if task is syncing else 'provider' if task is receiving else 'phone',type(e).__name__)
            finally:
                syncing.cancel();await asyncio.gather(syncing,return_exceptions=True)
                logger.info('task_context id=%s restored=%d updates=%d',connection_id,context_sync.restored,context_sync.updates)
                try:
                    await tools.close()
                    await send_up({'type':'session.close'})
                    try:await asyncio.wait_for(asyncio.shield(receiving),3)
                    except Exception:pass
                finally:
                    for task in (sending,receiving,idle_watch):task.cancel()
                    await asyncio.gather(sending,receiving,idle_watch,return_exceptions=True)
    except (WebSocketDisconnect,websockets.ConnectionClosed):pass
    except Exception as e:
        logging.warning('duplex_connection_failed kind=%s',type(e).__name__)
        try:await send_client({'type':'error','message':'语音连接失败，请重新开始通话'})
        except Exception:pass
    finally:
        if usage_started is not None:write_usage('call_ended',seconds=round(time.monotonic()-usage_started,3))
        logger.info('ended id=%s elapsed=%.2f',connection_id,time.monotonic()-started_at)
        if tools:await tools.close()
        try:await ws.close()
        except Exception:pass
        usage_call_id.reset(usage_token)

if __name__=='__main__':
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=22601,ws_max_size=4_000_000)
