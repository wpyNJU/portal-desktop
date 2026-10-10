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
from live_config import capabilities
from live_session import LiveSession
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
                        if d.get('type')=='error':
                            logger.warning('speech_synthesis_rejected')
                            return False
                finally:
                    await send({'type':'session.close'})
                    try:
                        async with asyncio.timeout(.75):
                            async for raw in voice_ws:
                                event=json.loads(raw);usage.observe(event)
                                if event.get('type')=='session.closed':break
                    except (TimeoutError,websockets.ConnectionClosed):pass
    except Exception as exc:
        logger.warning('speech_synthesis_failed kind=%s',type(exc).__name__)

@app.get('/health')
def health():
    providers=capabilities(ROOT)
    return {'ready':any(value['configured'] for value in providers.values()),'voice':'Doubao Seeduplex 3.0','agent':'Loom SSE','mode':'duplex','providers':providers}

AGENT_TIMEOUT=300
AGENT_MAX_WAIT=600
TOOL_REPLY_TIMEOUT=12

def query_identity(item):
    # Transport identity only. Request prose and titles are never task IDs.
    return ('call:'+str(item.get('call_id')),False)

class ToolCalls:
    def __init__(self,up,client,context=None,voice=DEFAULT_VOICE,agent=None,manager=None,natural_answers=False):
        self.manager=manager if manager is not None else background_tasks;self.closed=False;self.watcher=None;self.watch_started=time.time()
        self.voice=voice;self.bridge=agent if agent is not None else bridge
        self.up=up;self.client=client;self.epoch=0;self.pending=[];self.seen=set()
        self.response_ready=asyncio.Event();self.opening_stops=[];self.queries={};self.relays=set();self.scheduler=TurnScheduler()
        self.context=context if context is not None else {'scene':'voice-'+str(uuid.uuid4())}
        self.scope=self.context.get('scope',self.context['scene']);self.task_ids={}
        self.natural_ready={}
        self.quick_acks={};self.acknowledged_turns=set();self.natural_notice_turns={}
        self.last_ack=None;self.announced_tasks=set()
        async def forwarded(event):
            for receipt in self.natural_ready.values():
                if receipt['turn']!=self.audio.turn:
                    receipt['interrupted']=True;receipt['ready'].set()
            await self.client(event)
            if event.get('type')=='response.output_audio.delta':
                self.acknowledged_turns.add(self.audio.turn)
                self.response_ready.set()
                for receipt in self.natural_ready.values():
                    if (not receipt['interrupted'] and receipt['turn']==self.audio.turn and
                            receipt['question']==event.get('question_id')):
                        receipt['ready'].set()
        self.audio=SpeechOutput(self.scheduler,forwarded)
        self.natural_answers=natural_answers;self.natural_tasks=set();self.natural_timeout=12;self.natural_notices={};self.natural_turns=set()
        self.supplement_key=None;self.supplement_stop=None
        async def speak(text,client,stop,valid):return await speak_opening(text,client,stop,valid,voice=self.voice)
        self.reports=TaskReports(task_store,self.scope,self.scheduler,self.client,speak,output=self.audio)
        from task_progress import TaskProgress
        self.progress=TaskProgress(task_store,self.scope,self.scheduler,self.client,speak,self.audio)
    def acknowledge_request(self,delay=0):
        turn=self.audio.turn
        if turn in self.quick_acks:return
        from voice_notices import acknowledgement
        phrase=acknowledgement(self.last_ack);self.last_ack=phrase
        started=time.monotonic()
        async def acknowledge(stop):
            if not await self.audio.begin_scheduled(stop):return
            async def output(event):
                if self.audio.turn!=turn or stop.is_set():return
                sent=await self.audio.scheduled(event,stop)
                if sent and turn not in self.acknowledged_turns:
                    self.acknowledged_turns.add(turn)
                    logger.info('quick_ack_first_audio turn=%s elapsed=%.3f',turn,time.monotonic()-started)
                    await self.client({'type':'agent.opening','text':phrase})
            await speak_opening(phrase,output,stop,lambda:not self.closed and self.audio.turn==turn,voice=self.voice)
        async def deliver():
            if delay:await asyncio.sleep(delay)
            # Quick native answers need no filler; only cover a silent turn.
            valid=lambda:not self.closed and self.audio.turn==turn and turn not in self.acknowledged_turns
            if not valid():return
            await self.scheduler.deliver(acknowledge,foreground=True,
                priority=(float('inf'),3),valid=valid)
        task=asyncio.create_task(deliver())
        self.quick_acks={k:v for k,v in self.quick_acks.items() if not v.done()}
        self.quick_acks[turn]=task
        self.pending.append((task,threading.Event()))
    def interrupt(self,cancel_queries=False):
        self.scheduler.interrupt()
        for opening_stop in self.opening_stops:opening_stop.set()
        for relay in tuple(self.relays):relay.interrupt_audio()
        for receipt in self.natural_ready.values():
            if receipt['turn']!=self.audio.turn:
                receipt['interrupted']=True;receipt['ready'].set()
        if not cancel_queries:return
        if any(not task.done() for task,stop in self.pending):logger.info('agent_interrupted scene=%s',self.context['scene'])
        self.epoch+=1
        for task,stop in self.pending:stop.set()
    def supplement(self,key,offset):
        turn=self.natural_notice_turns.pop(key,self.audio.turn)
        if turn!=self.audio.turn:
            logger.info('supplement_skipped task=%s reason=new_turn',key)
            return
        row=task_store.get(self.scope,key)
        if row['state']!='completed' or row['delivery']=='deferred':return
        from completion_notice import completion_notice
        notice=completion_notice(row['text'])
        async def speak(stop):
            if not await self.audio.begin_scheduled(stop):return
            self.supplement_key=key;self.supplement_stop=stop
            text=f"{row['title']}，{notice}"
            logger.info('completion_notice task=%s source=final_section_or_status',key)
            async def output(event):
                if not stop.is_set():await self.audio.scheduled(event,stop)
            try:
                await speak_opening(text,output,stop,lambda:not self.closed and
                    task_store.get(self.scope,key)['delivery']=='awaiting_choice',voice=self.voice)
            finally:
                if self.supplement_stop is stop:self.supplement_key=None;self.supplement_stop=None
        # Consume the notice before queueing. Interruption never loops a retry,
        # while the complete source body and its cursor remain untouched.
        task=asyncio.create_task(self.scheduler.deliver(speak,priority=lambda:self.reports.priority(key),
            valid=lambda:not self.closed and self.audio.turn==turn and task_store.get(self.scope,key)['delivery']=='awaiting_choice'))
        self.pending.append((task,threading.Event()))
    async def announce_task(self,key,turn):
        if key in self.announced_tasks:return
        self.announced_tasks.add(key)
        from voice_notices import submitted_notice
        phrase=submitted_notice(task_store.get(self.scope,key)['title'])
        valid=lambda:not self.closed and self.audio.turn==turn and task_store.get(self.scope,key)['state']=='running' and not task_store.get(self.scope,key)['text']
        async def speak(stop):
            if not await self.audio.begin_scheduled(stop):return
            await self.client({'type':'agent.opening','text':phrase})
            logger.info('task_submission_notice task=%s turn=%s',key,turn)
            async def output(event):
                if not stop.is_set():await self.audio.scheduled(event,stop)
            await speak_opening(phrase,output,stop,lambda:not self.closed and self.audio.turn==turn,voice=self.voice)
        await self.scheduler.deliver(speak,priority=lambda:self.reports.priority(key,2),valid=valid,foreground=True)

    async def explicit_lookup(self):
        # Independent of provider tool selection; only a validated read-only intent.
        key,reused=self.manager.submit(self.scope,'explicit-campfire-latest',False,
            '查询篝火当前最新消息，仅查询，不发消息或修改任何内容。先返回可核实的新消息要点。',
            self.bridge,self.context,title='篝火最新消息')
        logger.info('explicit_lookup_submitted task=%s reused=%s',key,reused)
        await self.reports.event(key)
        turn=self.audio.turn
        if not reused:await self.announce_task(key,turn)

    async def defer(self,key):
        if self.supplement_key==key:
            self.scheduler.interrupt();await self.audio.clear_scheduled(self.supplement_stop)
        return await self.reports.defer(key)
    async def watch(self):
        offsets={};versions={};finished=set();last_progress={};observed=set()
        since=getattr(self,'watch_started',time.time())
        restored_finished=set()
        labels={'queued':'等待处理','awaiting_reply':'等待回复','accepted':'查询中','thinking':'查询中','tool':'正在获取信息','text':'正在返回结果'}
        while not self.closed:
            tracked={r['id'] for r in task_store.watchable(self.scope,since=since)}|observed
            observed.update(tracked)
            for key in sorted(tracked,key=lambda k:task_store.get(self.scope,k)['requested_at'],reverse=True):
                brief={'id':key}
                key=brief['id'];row=task_store.get(self.scope,key)
                self.progress.observe(row)
                if versions.get(key)!=row['updated']:
                    await self.reports.event(key);versions[key]=row['updated']
                offset=offsets.get(key,0)
                # Do not replay historical bodies/errors merely because delivery is unfinished.
                attached=(row['state']=='running' or row['delivery']!='reported' or key in offsets)
                if attached and len(row['text'])>offset:
                    await self.client({'type':'agent.text.delta','call_id':key,'task_id':key,'delta':row['text'][offset:],
                                       'offset':len(row['text'][:offset].encode('utf-16-le'))//2})
                    offsets[key]=len(row['text'])
                progress=(row.get('stage'),max(0,int(time.time()-row['created']))) if row['state']=='running' else None
                if progress and (progress[0],progress[1]//5)!=last_progress.get(key) and progress[0] in labels:
                    last_progress[key]=(progress[0],progress[1]//5)
                    await self.client({'type':'agent.progress','call_id':key,'text':labels[progress[0]],'stage':progress[0],'stage_updated':row.get('stage_updated',0),'title':row['title'],'elapsed':progress[1]})
                unread=row['text'][row['cursor']:]
                if key in self.natural_notices and key not in self.natural_tasks and row['state']!='running':
                    heard_length=self.natural_notices.pop(key)
                    if len(row['text'])>heard_length:
                        await self.client({'type':'agent.status','text':f"{row['title']}的后续内容已保存到任务记录，可按需查看或继续报告"})
                        self.supplement(key,heard_length)
                # A preview can consume the final sentence before the upstream
                # completion event. Completion must not leave a false resume choice.
                if row['state']=='completed' and row['text'] and not unread and row['delivery']=='awaiting_choice':
                    task_store.update(self.scope,key,delivery='reported');await self.reports.event(key)
                    row['delivery']='reported'
                if row['delivery']=='waiting' and row['text'] and key not in self.natural_tasks:
                    if row['state']!='running' or re.search(r'[。！？!?；;\n]',unread) or len(unread)>=160:
                        if row['state']=='completed' and not unread:
                            task_store.update(self.scope,key,delivery='reported');await self.reports.event(key)
                        else:self.reports.start(key,notice=row['state']!='running' and not unread,requested=False)
                if row['state']!='running' and key not in finished:
                    finished.add(key)
                    if attached:
                        restored=key in restored_finished
                        if row['state']=='completed':message='历史查询结果已恢复' if restored else '查询结果已返回'
                        else:
                            outcome='中断' if row['state']=='interrupted' else '失败'
                            message=('历史查询' if restored else '本次查询')+outcome
                            message+='，已保存部分正文' if row['text'] else '，未收到正文'
                            message+='；本次连接未重新查询' if restored else '，未自动重试'
                            if row.get('error_message'):message+='。'+row['error_message']
                        await self.client({'type':'agent.text.done' if row['state']=='completed' else 'agent.text.error',
                            'call_id':key,'task_id':key,'title':row['title'],'state':row['state'],
                            'restored':restored,'created_at':row['created'],'error_code':row.get('error_code',''),'message':message})
                    if not row['text'] and row['delivery']=='waiting' and key not in self.natural_tasks:
                        self.reports.start(key,notice=True,requested=False)
            await asyncio.sleep(.25)

    async def run(self,items,epoch,stop):
        results=[]
        for item in items:
            if stop.is_set() or self.closed:return
            try:
                args=json.loads(item.get('arguments','{}'));message=args.get('message','')
                if item.get('name')!='ask_agent' or not isinstance(message,str) or not 1<=len(message)<=12000:raise ValueError()
                if 'refresh' in args and not isinstance(args['refresh'],bool):raise ValueError()
                if args.get('existing_task_id') not in (None,''):
                    if not isinstance(args['existing_task_id'],str) or not args['existing_task_id']:raise ValueError()
                    task_store.get(self.scope,args['existing_task_id'])
                from task_routing import routing
                route=routing(args,item['call_id'],task_store,self.scope)
                identity,cacheable=query_identity(item)
                key,reused=self.manager.submit(self.scope,identity,cacheable,message,self.bridge,self.context,title=args.get('title'),**route)
                logger.info('task_routed task=%s operation=%s reused=%s structured=%s',key,args.get('operation','legacy'),reused,bool(route.get('intent')))
                if reused:
                    await self.existing_task(item,{'existing_task_id':key})
                    continue
                request_turn=self.audio.turn
                request_question=self.audio.question
                self.task_ids[item['call_id']]=key
                if self.natural_answers and not reused and request_turn not in self.natural_turns:
                    self.natural_tasks.add(key);self.natural_turns.add(request_turn)
                await self.reports.event(key)
                await self.announce_task(key,request_turn)
                while task_store.get(self.scope,key)['state']=='running':
                    if stop.is_set() or self.closed:return
                    row=task_store.get(self.scope,key)
                    # One real tool continuation can carry an early useful
                    # clause; later chunks stay in the persistent task body.
                    useful=len(row['text'])>=20 and bool(re.search(r'[。！？!?；;\n]',row['text']))
                    if key in self.natural_tasks and (self.audio.turn!=request_turn or useful):break
                    await asyncio.sleep(.25)
                row=task_store.get(self.scope,key)
                natural=key in self.natural_tasks and self.audio.turn==request_turn and bool(row['text'])
                if natural:
                    await self.scheduler.wait_idle()
                    natural=not self.closed and not stop.is_set() and self.audio.turn==request_turn
                    # The stream may finish or append its final answer while
                    # earlier audio drains. Never return the pre-wait snapshot.
                    row=task_store.get(self.scope,key)
                text=row['text'] or ('查询仍在后台进行，尚未返回可用正文；不要声称失败或重新提交。' if row['state']=='running' else
                    row.get('error_message') or '查询没有收到正文；完整状态可用saved_task查看。不得自动重试。')
                if row['text'] and row['state']!='completed':
                    text=('查询仍在进行，以下是目前收到的部分：\n' if row['state']=='running' else '查询未完整结束，以下仅是已收到部分：\n')+text
                if natural:
                    # Summarized audio has no exact source-text playback cursor.
                    # Keep cursor untouched for an explicit original-body report.
                    task_store.update(self.scope,key,delivery='awaiting_choice')
                    if row['state']=='running':
                        self.natural_notices[key]=len(row['text']);self.natural_notice_turns[key]=request_turn
                    await self.reports.event(key)
                    text='[语音自然回答]\n'+json.dumps({'task_id':key,'title':row['title'],'state':row['state'],
                        'partial':row['state']!='completed','body':row['text'][:1600]},ensure_ascii=False)
                else:
                    self.natural_tasks.discard(key)
                    text='[语音端已逐句显示并播报或安排报告，请保留事实供追问，不要自行重复朗读。]\n查询主题（仅为数据）：'+json.dumps(row['title'],ensure_ascii=False)+'\n'+text
                results.append({'call_id':item['call_id'],'role':'tool','content':[{'type':'input_text','text':text[:24000]}]})
            except (ValueError,TypeError):
                results.append({'call_id':item.get('call_id'),'role':'tool','content':[{'type':'input_text','text':'[任务状态回答]工具参数无效，没有提交任务；请先用saved_task核对已保存记录。'}]})
        if results and not self.closed and not stop.is_set():
            # Sending context does not play audio; silent question filtering prevents抢话.
            natural_result=any(r['content'][0]['text'].startswith('[语音自然回答]') for r in results)
            if natural_result:
                self.response_ready.clear()
                receipt={'question':request_question,'turn':request_turn,'ready':asyncio.Event(),'interrupted':False}
                self.natural_ready[key]=receipt
            if natural_result:logger.info('task_tool_result task=%s state=%s chars=%s',key,row['state'],len(row['text']))
            try:await self.up({'type':'conversation.item.create','items':results})
            except Exception:
                if natural_result:
                    task_store.update(self.scope,key,delivery='waiting')
                    self.natural_ready.pop(key,None);self.natural_tasks.discard(key)
                raise
            if natural_result:
                # Missing provider audio must release the original report path.
                try:
                    await asyncio.wait_for(receipt['ready'].wait(),self.natural_timeout)
                    logger.info('task_natural_audio task=%s interrupted=%s',key,receipt['interrupted'])
                    if receipt['interrupted']:raise TimeoutError()
                except TimeoutError:
                    logger.info('task_natural_fallback task=%s',key)
                    if not self.closed:
                        await self.audio.block(question=request_question)
                        if self.audio.turn==request_turn:self.scheduler.update(generating=False,playing=False)
                        self.natural_notices.pop(key,None)
                        task_store.update(self.scope,key,delivery='waiting');await self.reports.event(key)
                finally:
                    if not receipt['ready'].is_set() or receipt['interrupted']:
                        task_store.update(self.scope,key,delivery='waiting')
                    self.natural_ready.pop(key,None);self.natural_tasks.discard(key)
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
    async def existing_task(self,item,args):
        # An explicit durable ID is safer than guessing from similar titles.
        try:
            key=args.get('existing_task_id')
            if not isinstance(key,str) or not key:raise ValueError()
            row=task_store.get(self.scope,key)
            self.task_ids[item['call_id']]=row['id']
            task_store.touch(self.scope,row['id'])
            answer={**task_store.page(self.scope,row['id'],0,1200),
                    'reused':True,'new_query':False,'partial':row['state']=='running',
                    'state_meaning':'后台查询执行状态，不等于外部业务完成','stage':row.get('stage'),
                    'stage_updated':row.get('stage_updated',0)}
            text='[任务状态回答]'+json.dumps(answer,ensure_ascii=False)
        except (ValueError,TypeError):
            text='[任务状态回答]'+json.dumps({'error':'没有找到同一用户范围内的对应任务；未创建新查询。请先用saved_task列出任务，不猜测ID。','new_query':False},ensure_ascii=False)
        await self.up({'type':'conversation.item.create','items':[{'call_id':item['call_id'],'role':'tool','content':[{'type':'input_text','text':text}]}]})

    async def task_action(self,item):
        try:
            args=json.loads(item.get('arguments','{}'));action=args.get('action');key=args.get('task_id')
            if action in ('continue','replay'):
                from input_policy import classify_input
                utterance=next((h.get('text','') for h in reversed(self.context.get('history',[])) if h.get('role')=='user'),'')
                plain=re.sub(r'[\s，。！？、,.!?…]+','',utterance)
                if classify_input(utterance)=='backchannel' or plain in ('谢谢','谢谢你','知道了','明白了','收到'):
                    logger.info('report_replay_rejected reason=acknowledgement action=%s',action)
                    await self.up({'type':'conversation.item.create','items':[{'call_id':item['call_id'],'role':'tool','content':[{'type':'input_text','text':'[任务状态回答]用户只是确认收到，没有要求重播；未安排报告。简短接话即可，不重复结果，也不要追问是否朗读。'}]}]})
                    return
            if not key and action in ('continue','replay','defer'):key=self.reports.focus
            if action=='list':answer={'tasks':task_store.list(self.scope,args.get('offset',0)),'active_count':task_store.active_count(self.scope)}
            else:
                row=task_store.get(self.scope,key);key=row['id']
                if action=='get':
                    self.natural_notices.pop(key,None)
                    task_store.touch(self.scope,key);answer=task_store.page(self.scope,key,args.get('offset',0))
                elif action in ('continue','replay'):
                    self.natural_notices.pop(key,None)
                    answer={'message':self.reports.start(key,restart=action=='replay'),'task_id':key,'title':row['title']}
                elif action=='defer':answer={'message':await self.defer(key),'task_id':key,'title':row['title']}
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
            # Provider may serialize schema booleans as exact true/false strings.
            try:
                arguments=json.loads(item.get('arguments','{}'))
                value=arguments.get('refresh')
                if isinstance(value,str) and value.lower() in ('true','false'):
                    arguments['refresh']=value.lower()=='true'
                    item={**item,'arguments':json.dumps(arguments,ensure_ascii=False)}
            except (ValueError,TypeError,AttributeError):pass
            call_id=item.get('call_id')
            if not isinstance(call_id,str) or not call_id or call_id in self.seen:continue
            self.seen.add(call_id)
            if item.get('name')=='saved_task':
                task=asyncio.create_task(self.task_action(item));self.pending.append((task,threading.Event()));continue
            try:args=json.loads(item.get('arguments','{}'))
            except (ValueError,TypeError):args={}
            if (item.get('name')=='ask_agent' and isinstance(args,dict) and
                    (args.get('operation')=='read' or (not args.get('operation') and args.get('existing_task_id') not in (None,'') and args.get('refresh',False) is False))):
                task=asyncio.create_task(self.existing_task(item,args))
                self.pending.append((task,threading.Event()));continue
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
                        record['ok']=bool(results and results[0]['content'][0]['text'].startswith(('[语音端已逐句显示并播报','[语音自然回答]')))
                task.add_done_callback(done)
    async def close(self):
        if self.closed:return
        self.closed=True
        await self.progress.close()
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
    first_asr_at=None;first_audio_logged=False;audio_generations=set()
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
        kind=data.get('type')
        if kind in ('speech.input.pending','speech.input.resume','conversation.item.input_audio_transcription.completed','conversation.item.input_audio_transcription.failed','playback.clear'):
            logger.info('voice_transition id=%s event=%s generation=%s user=%s generating=%s playing=%s',connection_id,kind,data.get('generation'),tools.scheduler.user if tools else None,tools.scheduler.generating if tools else None,tools.scheduler.playing if tools else None)
        if kind in ('response.output_audio.delta','agent.audio.delta','opening.audio'):
            identity=(data.get('generation'),kind)
            if identity not in audio_generations:
                audio_generations.add(identity)
                logger.info('audio_forwarded id=%s event=%s generation=%s',connection_id,kind,data.get('generation'))
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
        provider=initial_request.get('provider','doubao')
        if provider not in ('doubao','gptlive'):
            await send_client({'type':'error','message':'不支持的语音模式'});return
        if provider=='gptlive':
            context['history']=normalize_history(initial_request.get('history',[]))
            session=LiveSession(ws,send_client,task_store,background_tasks,selected_bridge,context,
                                load_profile(profile_path),initial_request,query_identity)
            await session.run(ROOT)
            return
        voice=initial_request.get('voice',DEFAULT_VOICE)
        if not isinstance(voice,str) or voice not in VOICES:voice=DEFAULT_VOICE
        context['history']=normalize_history(initial_request.get('history',[]),limit=12000)
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
                base_instructions=lambda:history_instructions(context['history'])+session_instructions(profile),
                focus=lambda:tools.reports.focus if tools else task_store.resumable(scope),
                send=update_context,
                can_update=lambda:tools is not None and tools.audio.turn>0 and tools.scheduler.idle() and not tools.scheduler.lock.locked())
            instructions=context_sync.instructions()
            await send_up(create_session(instructions=instructions,voice=voice))
            initial=json.loads(await asyncio.wait_for(remote.recv(),20))
            if initial.get('type')!='session.created':
                await send_client({'type':'error','message':'豆包会话建立失败，请检查服务权限或音色配置'});return
            # The provider registers callable tools through session.update, even
            # when the same whitelist was included in the creation payload.
            # Complete that declaration before the phone begins sending PCM.
            await send_up({'type':'session.update','session':{'tools':create_session()['session']['tools']}})
            usage_started=time.monotonic();write_usage('call_started')
            await send_client(initial)
            logger.info('ready id=%s elapsed=%.2f restored_tasks=%d prompt_chars=%d',connection_id,time.monotonic()-started_at,context_sync.restored,len(instructions))
            current_question=None;previous_question=None;turn_interrupted=False;blocked=set();tool_questions={};streamed_questions=set();voice_text={}
            async def send_tool_result(data):
                # The provider may attach a tool continuation to its original question.
                # New speech may stop playback, but must not hide a completed query.
                for item in data.get('items',[]):
                    if any(c.get('text','').startswith(('[语音自然回答]','[任务状态回答]')) for c in item.get('content',[])):
                        question=tool_questions.get(item.get('call_id'))
                        if question and question==current_question:
                            blocked.discard(question);streamed_questions.discard(question)
                            tools.audio.blocked_questions.discard(question)
                            tools.audio.blocked_responses.difference_update(rid for rid,q in tools.audio.responses.items() if q==question)
                            tools.natural_response_question=question
                            tools.response_ready.clear();tools.scheduler.update(generating=True)
                    if any(c.get('text','').startswith('[语音端已逐句显示并播报') for c in item.get('content',[])):
                        question=tool_questions.get(item.get('call_id'))
                        if question:
                            streamed_questions.add(question)
                            await tools.audio.block(question=question)
                await send_up(data)
            async def task_client(data):
                if data.get('type')=='task.updated':context_sync.observe(data['task'])
                await send_client(data)
            tools=ToolCalls(send_tool_result,task_client,context=context,voice=voice,agent=selected_bridge,natural_answers=True)
            from input_health import InputHealth
            input_health=InputHealth()
            tools.watcher=asyncio.create_task(tools.watch())
            async def downstream():
                nonlocal current_question,previous_question,turn_interrupted
                from input_policy import classify_input, explicit_campfire_lookup, needs_quick_ack
                finished_inputs=set();input_open=False;pending_question=None;input_committed=False;input_was_audible=False;stop_cancelled=False
                async def commit_input():
                    nonlocal current_question,previous_question,turn_interrupted,input_committed
                    if input_committed:return
                    input_committed=True
                    tools.scheduler.update(user=True,generating=False)
                    previous_question=current_question;current_question=pending_question;turn_interrupted=True
                    if previous_question:finished_inputs.add(previous_question)
                    voice_text.clear()
                    if previous_question:blocked.add(previous_question)
                    tools.interrupt()
                    await tools.audio.user_started(current_question)
                    # user_started advances the committed semantic turn. Wake
                    # only its older natural replies as interrupted, never let
                    # audio for this new question count as their first frame.
                    if hasattr(tools,'natural_ready'):
                        for receipt in tools.natural_ready.values():
                            if receipt['turn']!=tools.audio.turn:
                                receipt['interrupted']=True;receipt['ready'].set()
                async def stop_input():
                    nonlocal stop_cancelled
                    await commit_input()
                    if current_question:blocked.add(current_question)
                    await tools.audio.block(question=current_question)
                    if not stop_cancelled:
                        stop_cancelled=True;await send_up({'type':'response.cancel'})
                async for raw in remote:
                    d=json.loads(raw);usage.observe(d);d=tools.audio.observe(d);kind=d.get('type','')
                    if kind.startswith('conversation.item.input_audio_transcription.'):
                        stage=kind.rsplit('.',1)[-1];input_health.asr(stage)
                        if stage!='delta':logger.info('input_asr id=%s stage=%s metrics=%s',connection_id,stage,json.dumps(input_health.snapshot()))
                    if kind.startswith('conversation.item.input_audio_transcription.') and d.get('item_id') in finished_inputs:continue
                    spoken=d.get('delta') or d.get('text') or ''
                    is_transcript=kind in ('conversation.item.input_audio_transcription.delta','conversation.item.input_audio_transcription.completed')
                    begins_input=(kind=='conversation.item.input_audio_transcription.started' or
                                  (is_transcript and isinstance(spoken,str) and bool(spoken.strip())))
                    if begins_input and (not input_open or (d.get('item_id') and d['item_id']!=pending_question)):
                        pending_question=d.get('item_id');input_open=True;input_committed=False;stop_cancelled=False
                        input_was_audible=tools.scheduler.playing or tools.scheduler.generating
                        tools.scheduler.update(user=True)
                        await send_client({'type':'speech.input.pending'})
                    if is_transcript:
                        if kind.endswith('.delta'):
                            tools.scheduler.update(user=True)
                            if classify_input(spoken)=='stop':await stop_input()
                    if kind=='conversation.item.input_audio_transcription.completed':
                        input_open=False
                        if d.get('item_id'):finished_inputs.add(d['item_id'])
                        if not input_committed and input_was_audible and classify_input(d.get('text'))=='backchannel':
                            if pending_question:
                                blocked.add(pending_question);await tools.audio.block(question=pending_question)
                            tools.scheduler.update(user=False)
                            await send_client({'type':'speech.input.resume'})
                            await send_client(d)
                            continue
                        await commit_input()
                        stopping=classify_input(d.get('text'))=='stop'
                        tools.scheduler.update(user=False,generating=not stopping)
                        if stopping:
                            await stop_input()
                        if isinstance(d.get('text'),str):context['history']=normalize_history(context['history']+[{'role':'user','text':d['text']}],limit=12000)
                        if not stopping and classify_input(d.get('text'))=='redirect':
                            tools.acknowledge_request(delay=0 if needs_quick_ack(d.get('text')) else .7)
                        if explicit_campfire_lookup(d.get('text')):
                            if current_question:
                                blocked.add(current_question);streamed_questions.add(current_question)
                            await tools.audio.block(question=current_question)
                            tools.scheduler.update(generating=False)
                            task=asyncio.create_task(tools.explicit_lookup())
                            tools.pending.append((task,threading.Event()))
                            await send_client(d)
                            continue
                    if kind=='conversation.item.input_audio_transcription.failed':
                        input_open=False
                        if d.get('item_id'):finished_inputs.add(d['item_id'])
                        tools.scheduler.update(user=False,generating=False)
                        if not input_committed:await send_client({'type':'speech.input.resume'})
                    if kind=='response.function_call_arguments.done':
                        if (d.get('question_id') or current_question) in blocked:continue
                        tools.scheduler.update(generating=False)
                        items=d.get('items',[])
                        for choice in items:
                            try:args=json.loads(choice.get('arguments','{}'))
                            except (ValueError,TypeError):args={}
                            logger.info('voice_tool_choice id=%s name=%s action=%s refresh=%s references_existing=%s',connection_id,choice.get('name'),args.get('action'),args.get('refresh'),bool(args.get('existing_task_id')))
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
                    if (kind=='response.output_audio.started' and
                            d.get('question_id')==getattr(tools,'natural_response_question',None)):
                        # Some provider versions reuse the function-call response
                        # ID for its continuation. Reopen only at a new stream
                        # boundary, never on an untagged late PCM tail.
                        tools.audio.ended_responses.discard(d.get('response_id'))
                        tools.audio.finished_responses.discard(d.get('response_id'))
                    if kind=='response.output_text.delta':voice_text.setdefault(d.get('response_id') or current_question,[]).append(d.get('delta',''))
                    if kind=='response.done':
                        completed=voice_text.pop(d.get('response_id') or current_question,[])
                        if completed:context['history']=normalize_history(context['history']+[{'role':'assistant','text':''.join(completed)}],limit=12000)
                    if kind=='error':
                        logging.warning('doubao_error code=%s',d.get('error',{}).get('code') if isinstance(d.get('error'),dict) else d.get('code'))
                        await send_client({'type':'error','message':'豆包语音返回错误，请重新开始通话'});continue
                    if await tools.audio.provider(d,observed=True):await send_client(d)
                    if kind=='session.closed':return
            receiving=asyncio.create_task(downstream())
            async def upstream():
                last_playback=None
                while True:
                    data=await ws.receive_json();kind=data.get('type')
                    if kind=='input_audio_buffer.append':
                        audio=data.get('audio','')
                        if not isinstance(audio,str) or len(audio)>2000:raise ValueError('audio frame')
                        pcm=base64.b64decode(audio,validate=True)
                        if len(pcm)!=640:raise ValueError('audio size')
                        await send_up({'type':kind,'audio':audio})
                        input_health.frame(pcm)
                        if time.monotonic()-input_health.last_log>=5:
                            logger.info('input_audio id=%s metrics=%s',connection_id,json.dumps(input_health.snapshot()))
                            input_health.last_log=time.monotonic()
                    elif kind in ('input_audio_mute.commit','input_audio_unmute.commit','input_audio_buffer.commit'):
                        if kind!='input_audio_buffer.commit':input_health.muted=kind=='input_audio_mute.commit'
                        await send_up({'type':kind})
                    elif kind=='text.message':
                        await send_client({'type':'text.rejected','message':'文字输入已关闭，请刷新页面使用语音交流'});continue
                    elif kind=='task.segment.played':
                        tools.reports.acknowledge(data.get('token'))
                    elif kind=='task.pause':
                        try:await tools.defer(task_store.get(scope,data.get('task_id'))['id'])
                        except ValueError:await send_client({'type':'agent.status','text':'没有找到这条任务'})
                    elif kind=='task.report':
                        try:
                            row=task_store.get(scope,data.get('task_id'))
                            tools.natural_notices.pop(row['id'],None)
                            message=tools.reports.start(row['id'],restart=data.get('restart') is True)
                            await send_client({'type':'agent.status','text':message})
                        except ValueError:await send_client({'type':'agent.status','text':'没有找到这条任务'})
                    elif kind=='client.playback':
                        state=(data.get('generation'),data.get('playing'))
                        if state!=last_playback:
                            logger.info('client_playback id=%s generation=%s playing=%s current=%s',connection_id,*state,tools.audio.generation)
                            last_playback=state
                        tools.audio.playback(data)
                    elif kind=='response.cancel':
                        tools.interrupt()
                        if current_question:blocked.add(current_question)
                        await tools.audio.user_started();await send_up({'type':kind})
                    elif kind=='client.diagnostic':
                        if data.get('reason')=='voice_audio_state':
                            state=data.get('state');context_state=data.get('context_state')
                            if state in ('scheduled','drained','paused-confirmed','paused-local','cleared','resumed') and context_state in ('running','suspended','closed','unavailable'):
                                metrics={}
                                for field in ('paused_ms','queued_ms','frames','generation'):
                                    value=data.get(field)
                                    if isinstance(value,(int,float)) and not isinstance(value,bool) and 0<=value<=1e9:metrics[field]=round(value)
                                logger.info('client_audio id=%s state=%s context=%s metrics=%s',connection_id,state,context_state,json.dumps(metrics,separators=(',',':')))
                        if data.get('reason')=='audio_backpressure':
                            amount=data.get('buffered_bytes')
                            if isinstance(amount,int):logger.warning('client_backpressure id=%s bytes=%d',connection_id,min(max(amount,0),10000000))
                    elif kind=='session.close':return
            async def watch_idle():
                timer=IdleCallTimer(time.monotonic())
                while True:
                    await asyncio.sleep(.25)
                    if input_health.stalled():
                        logger.warning('input_stalled id=%s metrics=%s',connection_id,json.dumps(input_health.snapshot()))
                        await send_client({'type':'agent.status','text':'这句话的语音识别还没有完成，暂未提交查询。请检查麦克风是否仍开启；若持续无响应，请重新接通。'})
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
