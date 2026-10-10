import ast,asyncio,json,re,time,threading,logging
from pathlib import Path
from turn_scheduler import TurnScheduler,result_notice
from task_store import TaskStore
from task_reports import TaskReports
from background_tasks import BackgroundTasks
from speech_output import SpeechOutput
source=ast.parse(Path(__file__).with_name('doubao_server.py').read_text(encoding='utf-8'))
ns=dict(asyncio=asyncio,json=json,re=re,time=time,threading=threading,logger=logging.getLogger('test'),DEFAULT_VOICE='test',bridge=None,TurnScheduler=TurnScheduler,result_notice=result_notice,TaskReports=TaskReports,task_store=TaskStore(':memory:'))
ns['background_tasks']=BackgroundTasks(ns['task_store'])
ns['SpeechOutput']=SpeechOutput
exec(compile(ast.Module(body=[n for n in source.body if isinstance(n,(ast.ClassDef,ast.FunctionDef)) and n.name in ('ToolCalls','query_identity')],type_ignores=[]),'<test>','exec'),ns,ns)
Tool=ns['ToolCalls'];identity=ns['query_identity'];original_run=Tool.run
def item(i,text):return {'call_id':i,'name':'ask_agent','arguments':json.dumps({'message':text})}
assert identity(item('same','first wording')) == identity(item('same','other wording'))
assert identity(item('one','same wording')) != identity(item('two','same wording'))
print('PASS: transport identity is independent of request prose')

async def slow_test():
    class Relay:
        def __init__(self,*a):self.parts=[];self.last=time.monotonic();self.had_audio=True;self.audio_ok=True;self.audio_interrupted=False
        def emit(self,text):self.parts.append(text);self.last=time.monotonic()
        async def finish(self):pass
        async def close(self):pass
    class Bridge:
        async def stream(self,message,context,stop,emit,stage):
            stage('thinking');await asyncio.sleep(.65);stage('text');emit('延迟返回的结果')
    async def speak(*a,**kw):return True
    ns.update(AgentStreamVoice=Relay,bridge=Bridge(),speak_opening=speak,AGENT_TIMEOUT=2,AGENT_MAX_WAIT=3,TOOL_REPLY_TIMEOUT=.1)
    events=[]
    async def out(d):events.append(d)
    t=Tool(out,out,context={'scene':'slow-test'})
    t.scheduler.quiet=.01
    t.watcher=asyncio.create_task(t.watch())
    results=await original_run(t,[item('slow','任务')],0,threading.Event())
    assert '延迟返回的结果' in results[0]['content'][0]['text']
    assert not any(e['type']=='agent.text.error' for e in events)
    assert any(e['type']=='agent.progress' and e['text']=='查询中' for e in events)
    assert any(e['type']=='agent.text.delta' and '延迟返回的结果' in e['delta'] for e in events)
    await t.close()
    print('PASS: delayed stream survives idle wait and delivers result (scaled timing)')
asyncio.run(slow_test())
