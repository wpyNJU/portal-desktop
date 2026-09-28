import asyncio
from task_reports import TaskReports
from task_store import TaskStore
from turn_scheduler import TurnScheduler

async def main():
    store=TaskStore(':memory:');key=store.create('s','prefetch')
    store.update('s',key,state='completed',text='第一句。第二句。第三句。')
    scheduler=TurnScheduler(.001);tokens=[];frames=[];prepared=asyncio.Event()
    async def speak(text,output,stop,valid):
        if text=='第二句。':prepared.set()
        await output({'type':'opening.audio','delta':text});return True
    async def client(d):
        if d['type']=='agent.audio.delta':frames.append(d['delta'])
        if d['type']=='task.segment.end':
            if len(frames)==1:
                scheduler.update(playing=False);report.acknowledge(d['token'])
            else:tokens.append(d['token'])
    report=TaskReports(store,'s',scheduler,client,speak)
    report.start(key)
    await asyncio.wait_for(prepared.wait(),1)
    async with asyncio.timeout(1):
        while not tokens:await asyncio.sleep(.01)
    assert store.get('s',key)['cursor']==0
    assert '第一句。' in frames and '第二句。' not in frames
    # Prefetch is complete, but no playback acknowledgement means no cursor advance.
    scheduler.update(user=True);scheduler.interrupt()
    await asyncio.sleep(.03)
    assert store.get('s',key)['cursor']==0
    assert '第二句。' not in frames
    await report.close()
    print('PASS: next sentence synthesized during current playback, unplayed prefetch never sent or committed on interruption')

asyncio.run(main())
