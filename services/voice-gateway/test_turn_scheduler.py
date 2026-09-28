import asyncio
from turn_scheduler import TurnScheduler,result_notice
async def main():
    s=TurnScheduler(quiet=.04);events=[]
    async def action(stop):events.append('notified')
    s.update(user=True)
    task=asyncio.create_task(s.deliver(action));await asyncio.sleep(.08);assert not events
    s.update(user=False,generating=True);await asyncio.sleep(.08);assert not events
    s.update(generating=False,playing=True);await asyncio.sleep(.08);assert not events
    s.update(playing=False);await task;assert events==['notified']
    started=asyncio.Event()
    async def long_audio(stop):
        started.set()
        try:await asyncio.sleep(20)
        finally:events.append('stopped')
    task=asyncio.create_task(s.deliver(long_audio));await started.wait()
    s.update(user=True);s.interrupt();assert await task is False
    assert events[-1]=='stopped'
    # An interruption while waiting must not discard the pending result.
    task=asyncio.create_task(s.deliver(action));s.interrupt();await asyncio.sleep(.08);assert not task.done()
    s.update(user=False);assert await task
    # Notifications serialize and wait for real playback drainage.
    order=[]
    async def first(stop):order.append(1);s.update(playing=True)
    async def second(stop):order.append(2)
    a=asyncio.create_task(s.deliver(first));b=asyncio.create_task(s.deliver(second))
    await asyncio.sleep(.15);assert order==[1]
    s.update(playing=False);await asyncio.gather(a,b);assert order==[1,2]
    s.update(user=True);task=asyncio.create_task(s.deliver(action));await asyncio.sleep(.01);s.close();assert not await task
    assert '任务已经结束' not in result_notice('任务仍然进行中。')
    print('PASS: user/generation/playback busy, quiet interval, interrupt, pending retention, FIFO, close, honest notice')
asyncio.run(main())
