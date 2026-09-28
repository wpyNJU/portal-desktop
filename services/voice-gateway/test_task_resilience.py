import asyncio
from task_store import TaskStore
from task_reports import TaskReports
from turn_scheduler import TurnScheduler
from background_tasks import BackgroundTasks

async def main():
    store=TaskStore(':memory:');scheduler=TurnScheduler(.001);spoken=[]
    async def speak(text,output,stop,valid):
        spoken.append(text);await output({'type':'opening.audio','delta':text});return True
    async def client(d):
        if d['type']=='task.segment.end':
            scheduler.update(playing=False);reports.acknowledge(d['token'])
    reports=TaskReports(store,'a',scheduler,client,speak)
    first=store.create('a','slow');store.update('a',first,text='先收到的一句。')
    reports.start(first)
    await asyncio.wait_for(reports.jobs[first],2)
    assert store.get('a',first)['delivery']=='waiting'
    assert store.get('a',first)['cursor']==len('先收到的一句。')
    assert not any('继续听吗' in s for s in spoken)
    second=store.create('a','fast');store.update('a',second,state='completed',text='第二个任务先完成。')
    reports.start(second);await asyncio.wait_for(reports.jobs[second],1)
    assert store.get('a',second)['delivery']=='reported'
    # A late failed stream must distinguish partial data from a complete result.
    store.update('a',first,state='error',text='先收到的一句。后来的部分。')
    reports.start(first);await asyncio.wait_for(reports.jobs[first],1)
    assert '关于slow，查询没有完整结束，下面是已收到的部分内容。' in spoken
    assert spoken.count('先收到的一句。')==1
    empty=store.create('a','empty error');store.update('a',empty,state='error')
    reports.start(empty,notice=True);await asyncio.wait_for(reports.jobs[empty],1)
    assert any('没有收到可用正文' in s for s in spoken)
    assert store.get('a',empty)['delivery']=='reported'
    # Old active tasks cannot fall out of the observer when UI pagination advances.
    old=store.create('a','old active')
    for _ in range(25):
        key=store.create('a','new completed');store.update('a',key,state='completed',delivery='reported')
    assert old not in {r['id'] for r in store.list('a')}
    assert old in {r['id'] for r in store.watchable('a')}
    assert store.watchable('other')==[]
    manager=BackgroundTasks(store,timeout=.04);cancelled=asyncio.Event()
    class Bridge:
        async def stream(self,*args,**kwargs):
            try:await asyncio.sleep(2)
            finally:cancelled.set()
    key,_=manager.submit('a','slow',False,'test',Bridge(),{'scene':'test'})
    assert manager.busy('a') and not manager.busy('other')
    await asyncio.gather(*manager.jobs.values());await asyncio.sleep(0)
    assert cancelled.is_set() and not manager.busy('a')
    assert store.get('a',key)['state']=='error'
    await reports.close();await manager.shutdown()
    print('PASS: stalled stream yields queue, no redundant resume question, partial/error feedback, >20 active task observation, scope-specific busy, timeout cleanup')

asyncio.run(main())
