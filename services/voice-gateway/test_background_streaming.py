import ast,asyncio,json,re,time,threading,logging
from pathlib import Path
from background_tasks import BackgroundTasks
from task_store import TaskStore
from task_reports import TaskReports
from turn_scheduler import TurnScheduler
from speech_output import SpeechOutput

async def main():
    store=TaskStore(':memory:');manager=BackgroundTasks(store,timeout=3)
    gate=asyncio.Event();started=asyncio.Event();calls=[]
    class Bridge:
        async def stream(self,message,context,stop,emit,stage):
            calls.append(message);stage('text');emit('先收到的完整句子。');started.set()
            await gate.wait();assert not stop.is_set();emit('后来返回的完整结果。')
    bridge=Bridge();events=[];spoken=[]
    async def speak(text,client,stop,valid,**kwargs):
        spoken.append(text)
        await client({'type':'opening.audio','delta':text});return True
    namespace=dict(asyncio=asyncio,json=json,re=re,time=time,threading=threading,logger=logging.getLogger('test'),
                   DEFAULT_VOICE='test',background_tasks=manager,bridge=bridge,TurnScheduler=TurnScheduler,
                   TaskReports=TaskReports,SpeechOutput=SpeechOutput,task_store=store,speak_opening=speak)
    tree=ast.parse(Path(__file__).with_name('doubao_server.py').read_text(encoding='utf-8'))
    exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n,(ast.ClassDef,ast.FunctionDef)) and n.name in ('ToolCalls','query_identity')],type_ignores=[]),'<test>','exec'),namespace)
    Tool=namespace['ToolCalls']
    def connection():
        async def client(d):
            events.append(d)
            if d['type'] in ('opening.audio','agent.audio.delta'):tool.scheduler.update(playing=False)
            if d['type']=='task.segment.end':tool.reports.acknowledge(d['token'])
        tool=Tool(client,client,context={'scene':'test','scope':'a'})
        tool.scheduler.quiet=.001;tool.watcher=asyncio.create_task(tool.watch())
        return tool
    first=connection()
    item={'call_id':'one','name':'ask_agent','arguments':json.dumps({'message':'查询当前任务','title':'当前任务进展'})}
    first.submit([item]);await asyncio.wait_for(started.wait(),1)
    async def until(predicate):
        async with asyncio.timeout(2):
            while not predicate():await asyncio.sleep(.01)
    await until(lambda:any(e['type']=='agent.audio.delta' and e['delta']=='先收到的完整句子。' for e in events))
    key=store.list('a')[0]['id'];assert store.get('a',key)['state']=='running'
    assert store.get('a',key)['title']=='当前任务进展'
    assert store.get('a',key)['request']=='查询当前任务'
    await until(lambda:store.get('a',key)['cursor']==len('先收到的完整句子。'))
    await first.close()
    assert store.get('a',key)['state']=='running' and not manager.jobs[key].done()
    second=connection()
    # Global dedup survives the old connection being closed.
    reused,yes=manager.submit('a','task-overview',True,'重复查询',bridge,{'scene':'new'})
    assert yes and reused==key and len(calls)==1
    second.reports.start(key)
    gate.set()
    await until(lambda:store.get('a',key)['delivery']=='reported')
    assert store.get('a',key)['text']=='先收到的完整句子。后来返回的完整结果。'
    assert store.get('a',key)['cursor']==len(store.get('a',key)['text'])
    audio=[e['delta'] for e in events if e['type']=='agent.audio.delta']
    assert audio.count('先收到的完整句子。')==1,audio
    assert audio.count('后来返回的完整结果。')==1,audio
    assert store.list('other')==[]
    await second.close();await manager.shutdown()
    print('PASS: stream speech before completion, disconnect keeps request, reconnect same task, cross-call dedup, acknowledged cursor, no whole-report replay')

asyncio.run(main())
