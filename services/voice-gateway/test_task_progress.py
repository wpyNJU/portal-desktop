"""Task progress uses real scheduler/output boundaries, with no provider calls."""
import asyncio
import unittest
from unittest.mock import patch
from speech_output import SpeechOutput
from task_progress import TaskProgress
from task_store import TaskStore
from turn_scheduler import TurnScheduler


class Clock:
    def __init__(self):self.value=1000
    def __call__(self):return self.value


class ProgressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock=Clock();self.store=TaskStore(':memory:');self.scheduler=TurnScheduler(.001)
        self.events=[];self.synthesized=[];self.before_audio=None;self.after_audio=None
        self.audio_ready=asyncio.Event();self.no_audio=False;self.fail_client=False
        async def client(event):
            if self.fail_client and event['type']=='opening.audio':raise ConnectionError('synthetic failed transport')
            self.events.append(event)
            if event['type']=='opening.audio':
                self.audio_ready.set()
                self.output.playback({'playing':False,'generation':event['generation']})
        self.output=SpeechOutput(self.scheduler,client)
        async def speak(text,output,stop,valid):
            self.synthesized.append(text)
            if self.before_audio:await self.before_audio.wait()
            if not valid() or stop.is_set() or self.no_audio:return False
            await output({'type':'opening.audio','delta':text})
            if self.after_audio:await self.after_audio.wait()
            return True
        self.progress=TaskProgress(self.store,'s',self.scheduler,client,speak,self.output,now=self.clock)

    async def asyncTearDown(self):
        if self.before_audio:self.before_audio.set()
        if self.after_audio:self.after_audio.set()
        await self.progress.close();self.scheduler.close();self.store.db.close()

    def task(self,title='项目甲',scope='s'):
        with patch('task_store.time.time',return_value=self.clock.value):key=self.store.create(scope,title)
        return key

    def row(self,key,stage='queued',scope='s',**fields):
        self.store.update(scope,key,stage=stage,stage_updated=self.clock.value,
                          **({'summary':fields['summary']} if 'summary' in fields else {}))
        return {**self.store.get(scope,key),'stage':stage,'stage_updated':self.clock.value,**fields}

    def audio(self):return [e['delta'] for e in self.events if e['type']=='opening.audio']

    async def finish(self):
        results=await asyncio.wait_for(asyncio.gather(*list(self.progress.jobs.values()),return_exceptions=True),2)
        for result in results:
            if isinstance(result,Exception):raise result

    async def until(self,predicate):
        async with asyncio.timeout(2):
            while not predicate():await asyncio.sleep(.005)

    async def test_technical_stages_are_silent_then_one_long_wait_notice(self):
        key=self.task()
        for stage in ('accepted','thinking','tool','awaiting_reply'):
            self.clock.value+=8
            self.assertFalse(self.progress.observe(self.row(key,stage)))
        self.clock.value=1045;self.assertTrue(self.progress.observe(self.row(key,'thinking')))
        await self.finish()
        self.assertEqual(len(self.audio()),1);self.assertIn('还没返回结果',self.audio()[0])
        for stage in ('tool','thinking','accepted'):
            self.clock.value+=90
            self.assertFalse(self.progress.observe(self.row(key,stage)))
        self.assertEqual(len(self.audio()),1)

    async def test_parallel_tasks_are_one_short_batch_not_round_robin(self):
        keys=[self.task('项目'+str(i)) for i in range(4)]
        self.clock.value=1045;self.scheduler.update(user=True)
        for key in keys:self.progress.observe(self.row(key,'thinking'))
        await asyncio.sleep(.02);self.assertEqual(self.audio(),[])
        self.scheduler.update(user=False);await self.finish()
        self.assertEqual(len(self.audio()),1);self.assertIn('等4项查询',self.audio()[0])
        self.assertLess(len(self.audio()[0]),90)
        self.clock.value=1200
        for key in keys:self.assertFalse(self.progress.observe(self.row(key,'tool')))
        self.assertEqual(set(self.progress.spoken),set(keys))

    async def test_progress_never_outranks_an_older_result(self):
        key=self.task();self.clock.value=1045;self.scheduler.update(user=True)
        self.progress.observe(self.row(key,'thinking'))
        order=[]
        async def result(stop):
            self.assertEqual(self.audio(),[], 'Progress must not jump a result')
            order.append('result')
        job=asyncio.create_task(self.scheduler.deliver(result,priority=(1,1)))
        await asyncio.sleep(.02)
        self.scheduler.update(user=False);await job;await self.finish()
        self.assertEqual(order,['result']);self.assertEqual(len(self.audio()),1)

    async def test_new_task_notice_respects_shared_cooldown(self):
        first=self.task();self.clock.value=1045
        self.progress.observe(self.row(first,'thinking'));await self.finish()
        second=self.task('项目乙');self.clock.value=1090
        self.assertFalse(self.progress.observe(self.row(second,'tool')))
        self.clock.value=1105;self.assertTrue(self.progress.observe(self.row(second,'tool')))
        await self.finish();self.assertEqual(len(self.audio()),2)

    async def test_completed_or_deferred_candidate_rechecks_store(self):
        for change in ({'state':'completed'},{'delivery':'deferred'},{'state':'error'}):
            key=self.task();self.clock.value+=65;self.scheduler.update(user=True)
            self.progress.observe(self.row(key,'tool'));await asyncio.sleep(.01)
            self.store.update('s',key,**change);self.scheduler.update(user=False);await self.finish()
        self.assertEqual(self.audio(),[])

    async def test_batch_result_arrives_during_synthesis_discards_stale_notice(self):
        first=self.task();second=self.task('项目乙');self.clock.value=1045
        self.scheduler.update(user=True);self.before_audio=asyncio.Event()
        for key in (first,second):self.progress.observe(self.row(key,'thinking'))
        self.scheduler.update(user=False);await self.until(lambda:bool(self.synthesized))
        self.store.update('s',second,state='completed',text='查到了。')
        self.before_audio.set();await self.finish();self.assertEqual(self.audio(),[])

    async def test_stage_changes_do_not_interrupt_generic_notice(self):
        key=self.task();self.clock.value=1045;self.after_audio=asyncio.Event()
        self.progress.observe(self.row(key,'accepted'));await self.audio_ready.wait()
        clears=sum(e['type']=='playback.clear' for e in self.events)
        self.progress.observe(self.row(key,'thinking'));await asyncio.sleep(.01)
        self.assertEqual(sum(e['type']=='playback.clear' for e in self.events),clears)
        self.after_audio.set();await self.finish();self.assertEqual(len(self.audio()),1)

    async def test_busy_expiry_and_user_turn_do_not_replay_backlog(self):
        key=self.task();self.clock.value=1045;self.scheduler.update(user=True)
        self.progress.observe(self.row(key,'thinking'));await asyncio.sleep(.01)
        self.clock.value=1061;self.scheduler.update(user=False);await self.finish()
        self.assertFalse(self.progress.observe(self.row(key,'thinking')))
        second=self.task();self.clock.value+=65;self.scheduler.update(user=True)
        self.progress.observe(self.row(second,'tool'));await self.output.user_started('new-question')
        self.scheduler.update(user=False);await self.finish();self.assertEqual(self.audio(),[])

    async def test_interruption_stops_batch_without_changing_tasks(self):
        keys=[self.task('项目甲'),self.task('项目乙')];self.clock.value=1045
        self.scheduler.update(user=True);self.after_audio=asyncio.Event()
        for key in keys:self.progress.observe(self.row(key,'thinking'))
        self.scheduler.update(user=False);await self.audio_ready.wait()
        await self.output.user_started('new-question');await self.finish()
        self.clock.value+=100
        for key in keys:
            self.assertEqual(self.store.get('s',key)['state'],'running')
            self.assertFalse(self.progress.observe(self.row(key,'tool')))
        self.assertEqual(len(self.audio()),1)

    async def test_private_detail_never_spoken_and_failed_delivery_not_retried(self):
        key=self.task();self.clock.value=1045;self.fail_client=True
        self.progress.observe(self.row(key,'thinking',reasoning='secret',stage_detail='private'))
        with self.assertRaises(ConnectionError):await self.finish()
        self.assertNotIn('secret',self.synthesized[0]);self.assertNotIn('private',self.synthesized[0])
        self.assertIsNone(self.progress.last_spoken)
        self.fail_client=False;self.assertFalse(self.progress.observe(self.row(key,'tool')))

    async def test_scope_and_result_body_are_not_generic_progress(self):
        key=self.task(scope='other');self.clock.value=1045
        self.assertFalse(self.progress.observe(self.row(key,'thinking',scope='other')))
        key=self.task();self.clock.value+=65;self.store.update('s',key,text='已收到正文')
        self.assertFalse(self.progress.observe(self.row(key,'text')))
        self.assertEqual(self.audio(),[])

if __name__=='__main__':unittest.main()
