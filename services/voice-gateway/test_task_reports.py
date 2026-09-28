import asyncio,tempfile
from pathlib import Path
from task_store import TaskStore
from task_reports import TaskReports
from turn_scheduler import TurnScheduler
async def main():
    with tempfile.TemporaryDirectory() as folder:
        path=Path(folder)/'tasks.db';store=TaskStore(path);scope='a';key=store.create(scope,'测试长报告')
        text='第一段完整报告。第二段不能丢失。第三段最后的结果。'
        store.update(scope,key,text=text,state='completed',delivery='pending')
        scheduler=TurnScheduler(.01);spoken=[];blocked=asyncio.Event();block=True;events=[]
        async def speak(text,client,stop,valid):
            spoken.append(text)
            if text=='第二段不能丢失。' and block:
                blocked.set();await asyncio.sleep(20)
            await client({'type':'opening.audio','delta':'test'})
            return True
        async def client(d):
            events.append(d)
            if d['type']=='task.segment.end':
                scheduler.update(playing=False);reports.acknowledge(d['token'])
        reports=TaskReports(store,scope,scheduler,client,speak)
        reports.start(key);await asyncio.wait_for(blocked.wait(),2)
        scheduler.update(user=True);scheduler.interrupt();await asyncio.sleep(.1)
        assert store.get(scope,key)['cursor']==len('第一段完整报告。')
        assert not any('还要继续听' in t for t in spoken)
        scheduler.update(user=False);await asyncio.wait_for(reports.jobs[key],2)
        assert sum('还要继续听' in t for t in spoken)==1
        assert '关于测试长报告，刚才还没讲完，你还要继续听吗？' in spoken
        assert store.get(scope,key)['delivery']=='awaiting_choice'
        block=False;spoken.clear();reports.start(key);await asyncio.wait_for(reports.jobs[key],2)
        assert spoken[1:]==['第二段不能丢失。','第三段最后的结果。'],spoken
        assert store.get(scope,key)['cursor']==len(text)
        reports.start(key,True);await asyncio.wait_for(reports.jobs[key],2)
        assert spoken[-3:]==['第一段完整报告。','第二段不能丢失。','第三段最后的结果。']
        big=store.create(scope,'超长');full='长文本内容。'*10000;store.update(scope,big,text=full,state='completed')
        rebuilt='';offset=0
        while True:
            page=store.page(scope,big,offset);rebuilt+=page['text']
            if page['next_offset'] is None:break
            offset=page['next_offset']
        assert rebuilt==full
        assert store.list('other')==[]
        try:store.get('other',key)
        except ValueError:pass
        else:raise AssertionError('cross scope')
        await reports.close();store.db.close()
        reopened=TaskStore(path);assert reopened.get(scope,key)['cursor']==len(text);assert reopened.get(scope,big)['text']==full;reopened.db.close()
    print('PASS: full report, playback checkpoint, interruption, idle confirmation once, resume/replay, 60000 chars, persistence, scope isolation')
asyncio.run(main())
