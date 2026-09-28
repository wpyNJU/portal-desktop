import asyncio
from task_store import TaskStore
from task_reports import TaskReports
from turn_scheduler import TurnScheduler
async def main():
    store=TaskStore(':memory:');s=TurnScheduler(.01);events=[];started=asyncio.Event()
    key=store.create('a','unfinished');store.update('a',key,state='completed',text='完整内容。',delivery='paused')
    newer=store.create('a','newer completed');store.update('a',newer,state='completed',text='完。',cursor=2,delivery='reported')
    async def client(d):events.append(d)
    async def speak(text,output,stop,valid):
        started.set();await output({'type':'opening.audio','delta':'test'});await asyncio.sleep(20)
    reports=TaskReports(store,'a',s,client,speak)
    assert reports.focus==key
    reports.start(key);await asyncio.wait_for(started.wait(),1)
    await asyncio.wait_for(reports.defer(key),1)
    assert store.get('a',key)['delivery']=='deferred' and store.get('a',key)['cursor']==0
    assert any(d['type']=='playback.clear' for d in events)
    assert not reports.busy()
    assert '已经读完' in reports.start(newer)
    empty=store.create('a','empty');store.update('a',empty,state='error')
    assert '尚未返回' in reports.start(empty)
    async def fail(*args):return False
    reports.speak=fail;events.clear();reports.start(key);await asyncio.wait_for(reports.jobs[key],1)
    assert store.get('a',key)['delivery']=='paused'
    assert any(d['type']=='agent.status' and '音频暂时不可用' in d['text'] for d in events)
    await reports.close();store.db.close()
    print('PASS: resume correct task after reconnect, stop queued audio, defer has no follow-up, empty/already-read feedback, synthesis failure visible')
asyncio.run(main())
