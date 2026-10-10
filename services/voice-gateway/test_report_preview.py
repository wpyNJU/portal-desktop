"""Automatic body excerpts stay short; explicit report requests stay complete."""
import asyncio
import unittest
from task_reports import TaskReports
from task_store import TaskStore
from turn_scheduler import TurnScheduler


class PreviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=TaskStore(':memory:');self.scheduler=TurnScheduler(.001)
        self.spoken=[];self.events=[];self.hold=None;self.held=None;self.ready=asyncio.Event()
        async def speak(text,client,stop,valid):
            await client({'type':'opening.audio','delta':text});return True
        async def client(event):
            self.events.append(event)
            if event['type']=='agent.audio.delta':self.spoken.append(event['delta'])
            if event['type']=='task.segment.end':
                if self.spoken[-1]==self.hold:
                    self.held=event['token'];self.ready.set()
                else:
                    self.scheduler.update(playing=False);self.reports.acknowledge(event['token'])
        self.reports=TaskReports(self.store,'s',self.scheduler,client,speak)

    async def asyncTearDown(self):
        self.scheduler.close();await self.reports.close();self.store.db.close()

    def task(self,text,state='completed'):
        key=self.store.create('s','预览测试');self.store.update('s',key,text=text,state=state);return key

    async def finish(self,key):
        await asyncio.wait_for(self.reports.jobs[key],3)

    async def test_two_body_segments_then_explicit_continue_reads_remainder(self):
        text='第一句。第二句。第三句。第四句。';key=self.task(text)
        self.reports.start(key,requested=False);await self.finish(key)
        self.assertEqual(self.store.get('s',key)['cursor'],len('第一句。第二句。'))
        self.assertEqual(self.store.get('s',key)['delivery'],'awaiting_choice')
        self.assertNotIn('第三句。',self.spoken)
        self.assertFalse(any('还要继续听' in s for s in self.spoken))
        before=list(self.spoken);self.reports.start(key,requested=False)
        await asyncio.sleep(.01);self.assertEqual(self.spoken,before)
        self.reports.start(key);await self.finish(key)
        self.assertIn('第三句。',self.spoken);self.assertIn('第四句。',self.spoken)
        self.assertEqual(self.store.get('s',key)['delivery'],'reported')
        self.reports.start(key,restart=True);await self.finish(key)
        self.assertEqual(self.spoken.count('第一句。'),2)

    async def test_stream_waiting_reentry_keeps_consumed_segment_budget(self):
        key=self.task('第一句。',state='running')
        self.reports.start(key,requested=False);await self.finish(key)
        self.assertEqual(self.store.get('s',key)['delivery'],'waiting')
        self.store.update('s',key,text='第一句。第二句。')
        self.reports.start(key,requested=False);await self.finish(key)
        self.assertEqual(self.store.get('s',key)['delivery'],'awaiting_choice')
        self.store.update('s',key,state='completed',text='第一句。第二句。第三句。')
        self.reports.start(key,requested=False);await asyncio.sleep(.01)
        self.assertNotIn('第三句。',self.spoken)
        self.assertEqual(self.store.get('s',key)['cursor'],len('第一句。第二句。'))

    async def test_short_complete_answer_is_reported(self):
        key=self.task('短回答。')
        self.reports.start(key,requested=False);await self.finish(key)
        self.assertEqual(self.store.get('s',key)['delivery'],'reported')
        self.assertEqual(self.store.get('s',key)['cursor'],len('短回答。'))

    async def test_unplayed_excerpt_does_not_advance_cursor_or_auto_ask(self):
        key=self.task('第一句。第二句。第三句。');self.hold='第一句。'
        self.reports.start(key,requested=False);await asyncio.wait_for(self.ready.wait(),1)
        self.scheduler.update(user=True,playing=False);self.scheduler.interrupt()
        await asyncio.sleep(.02);self.scheduler.update(user=False)
        await self.finish(key)
        self.assertEqual(self.store.get('s',key)['cursor'],0)
        self.assertEqual(self.reports.previews[key]['segments'],0)
        self.assertFalse(any('还要继续听' in s for s in self.spoken))
        self.assertNotIn('第二句。',self.spoken)

    async def test_character_cap_preserves_original_body_and_precise_cursor(self):
        text='甲'*250+'。';key=self.task(text)
        self.reports.start(key,requested=False);await self.finish(key)
        self.assertEqual(self.store.get('s',key)['cursor'],180)
        self.assertEqual(self.store.get('s',key)['text'],text)
        self.assertEqual(self.store.get('s',key)['delivery'],'awaiting_choice')
        self.assertIn('甲'*160,self.spoken);self.assertIn('甲'*20,self.spoken)

    async def test_no_returned_content_intro_before_first_speakable_clause(self):
        key=self.task('未结束的片段',state='running')
        self.reports.start(key,requested=False);await self.finish(key)
        self.assertEqual(self.spoken,[])
        self.assertEqual(self.store.get('s',key)['cursor'],0)
        self.assertEqual(self.store.get('s',key)['delivery'],'waiting')

    async def test_explicit_full_stream_stays_full_after_watcher_reentry(self):
        key=self.task('第一句。',state='running')
        self.reports.start(key);await self.finish(key)
        self.assertEqual(self.store.get('s',key)['delivery'],'waiting')
        text='第一句。第二句。第三句。第四句。'
        self.store.update('s',key,state='completed',text=text)
        self.reports.start(key,requested=False);await self.finish(key)
        self.assertEqual(self.store.get('s',key)['cursor'],len(text))
        self.assertEqual(self.store.get('s',key)['delivery'],'reported')
        self.assertIn('第四句。',self.spoken)


if __name__=='__main__':unittest.main()
