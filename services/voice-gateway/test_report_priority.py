import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from background_tasks import BackgroundTasks
from task_reports import TaskReports
from task_store import TaskStore
from turn_scheduler import TurnScheduler


class PriorityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=TaskStore(':memory:');self.scheduler=TurnScheduler(.001)
        self.spoken=[];self.hold=None;self.held=None;self.reached=asyncio.Event()
        async def speak(text,client,stop,valid):
            await client({'type':'opening.audio','delta':text});return True
        async def client(event):
            if event['type']=='agent.audio.delta':self.spoken.append((event['call_id'],event['delta']))
            if event['type']=='task.segment.end':
                if self.spoken[-1][1]==self.hold:
                    self.held=event['token'];self.reached.set()
                else:
                    self.scheduler.update(playing=False);self.reports.acknowledge(event['token'])
        self.reports=TaskReports(self.store,'s',self.scheduler,client,speak)

    async def asyncTearDown(self):
        self.scheduler.close();await self.reports.close();self.store.db.close()

    def task(self,title,requested,text='结果。'):
        with patch('task_store.time.time',return_value=requested):key=self.store.create('s',title)
        self.store.update('s',key,state='completed',text=text)
        return key

    async def finish(self):
        await asyncio.wait_for(asyncio.gather(*self.reports.jobs.values()),3)

    async def test_current_reply_then_newest_pending_then_older_pending(self):
        old=self.task('旧项目',100);new=self.task('新项目',200)
        self.scheduler.update(user=True)
        self.reports.start(old,requested=False);await asyncio.sleep(.01)
        self.reports.start(new,requested=False);await asyncio.sleep(.01)
        # A late Agent completion must not promote the earlier user question.
        self.store.update('s',old,text='晚到的旧项目结果。')
        self.scheduler.update(user=False,generating=True);await asyncio.sleep(.06)
        self.assertEqual(self.spoken,[])
        self.scheduler.update(generating=False,playing=True);await asyncio.sleep(.06)
        self.assertEqual(self.spoken,[])
        self.scheduler.update(playing=False);await self.finish()
        order=[key for key,text in self.spoken if text.startswith('刚才查的')]
        self.assertEqual(order,[new,old])

    async def test_user_followup_reorders_an_already_queued_old_task(self):
        old=self.task('旧项目',100);new=self.task('新项目',200)
        self.scheduler.update(user=True)
        self.reports.start(new,requested=False);self.reports.start(old,requested=False)
        await asyncio.sleep(.01)
        with patch('task_store.time.time',return_value=300):
            message=self.reports.start(old)  # Reuse pending report, update relevance only.
        self.assertIn('已经在',message)
        self.assertEqual(len(self.reports.jobs),2)
        self.scheduler.update(user=False);await self.finish()
        self.assertEqual([key for key,text in self.spoken if text.startswith('刚才查的')],[old,new])

    async def test_newer_result_yields_at_played_sentence_and_resumes_without_replay(self):
        old=self.task('旧项目',100,'旧的第一句。旧的第二句。')
        new=self.task('新项目',200,'新的结果。')
        self.hold='旧的第一句。';self.reports.start(old,requested=False)
        await asyncio.wait_for(self.reached.wait(),1)
        self.reports.start(new,requested=False);await asyncio.sleep(.02)
        self.hold=None;self.scheduler.update(playing=False);self.reports.acknowledge(self.held)
        await self.finish()
        self.assertEqual(self.store.get('s',old)['cursor'],len('旧的第一句。'))
        self.assertEqual(self.store.get('s',old)['delivery'],'waiting')
        self.assertNotIn((old,'旧的第二句。'),self.spoken)
        self.assertEqual(self.store.get('s',new)['delivery'],'reported')
        self.reports.start(old,requested=False);await self.finish()
        self.assertEqual([text for key,text in self.spoken if text in ('旧的第一句。','新的结果。','旧的第二句。')],
                         ['旧的第一句。','新的结果。','旧的第二句。'])
        self.assertEqual(self.store.get('s',old)['cursor'],len('旧的第一句。旧的第二句。'))
        self.assertFalse(any('还要继续听' in text for key,text in self.spoken),'Scheduling yield is not a user interruption')

    async def test_old_resume_question_waits_for_current_reply_and_new_task(self):
        old=self.task('旧项目',100,'旧的第一句。旧的第二句。');new=self.task('新项目',200,'新的结果。')
        self.hold='旧的第一句。';self.reports.start(old,requested=False)
        await asyncio.wait_for(self.reached.wait(),1)
        self.scheduler.update(user=True,playing=False);self.scheduler.interrupt()
        self.reports.start(new,requested=False);await asyncio.sleep(.03)
        before=len(self.spoken)
        self.scheduler.update(user=False,generating=True);await asyncio.sleep(.06)
        self.assertEqual(len(self.spoken),before)
        self.hold=None;self.scheduler.update(generating=False,playing=False);await self.finish()
        following=[text for key,text in self.spoken[before:]]
        self.assertLess(following.index('新的结果。'),next(i for i,text in enumerate(following) if '还要继续听' in text))
        self.assertEqual(self.store.get('s',old)['cursor'],0)
        self.assertEqual(self.store.get('s',old)['delivery'],'awaiting_choice')

    async def test_ack_precedes_same_query_report_but_never_newer_query(self):
        self.scheduler.update(user=True);heard=[]
        def action(label):
            async def speak(stop):heard.append(label)
            return speak
        tasks=[asyncio.create_task(self.scheduler.deliver(action('old report'),priority=(100,1))),
               asyncio.create_task(self.scheduler.deliver(action('old ack'),priority=(100,2))),
               asyncio.create_task(self.scheduler.deliver(action('new report'),priority=(200,1)))]
        await asyncio.sleep(.01);self.scheduler.update(user=False)
        await asyncio.wait_for(asyncio.gather(*tasks),1)
        self.assertEqual(heard,['new report','old ack','old report'])

    async def test_new_question_after_last_played_sentence_does_not_ask_to_resume(self):
        old=self.task('旧项目',100,'已完整播完。')
        self.scheduler.quiet=.08;self.reports.start(old,requested=False)
        async with asyncio.timeout(1):
            while self.store.get('s',old)['cursor']<len('已完整播完。'):await asyncio.sleep(.001)
        self.scheduler.update(user=True);self.scheduler.interrupt()
        await asyncio.sleep(.01);self.scheduler.update(user=False)
        await self.finish()
        self.assertEqual(self.store.get('s',old)['delivery'],'reported')
        self.assertFalse(any('还要继续听' in text for key,text in self.spoken))

    async def test_obsolete_ack_is_removed_without_canceling_pending_result(self):
        self.scheduler.update(user=True);heard=[];current={'turn':1}
        async def ack(stop):heard.append('old ack')
        async def result(stop):heard.append('saved result')
        old=asyncio.create_task(self.scheduler.deliver(ack,priority=(100,2),valid=lambda:current['turn']==1))
        saved=asyncio.create_task(self.scheduler.deliver(result,priority=(100,1)))
        await asyncio.sleep(.01);current['turn']=2
        self.assertFalse(await asyncio.wait_for(old,1))
        self.scheduler.update(user=False);await asyncio.wait_for(saved,1)
        self.assertEqual(heard,['saved result'])
        self.assertEqual(self.scheduler.waiting,[])

    async def test_duplicate_query_promotes_existing_task_without_resubmitting(self):
        manager=BackgroundTasks(self.store);gate=asyncio.Event();requests=[]
        class Bridge:
            async def stream(self,message,context,stop,emit,stage):
                requests.append(message);await gate.wait();emit('结果。')
        bridge=Bridge()
        try:
            with patch('task_store.time.time',return_value=100):
                key,_=manager.submit('s','same',True,'旧项目',bridge,{})
            await asyncio.sleep(0)
            with patch('task_store.time.time',return_value=300):
                repeated,reused=manager.submit('s','same',True,'旧项目',bridge,{})
            self.assertTrue(reused);self.assertEqual(repeated,key)
            self.assertEqual(self.store.get('s',key)['requested_at'],300)
            gate.set();await asyncio.gather(*manager.jobs.values());await asyncio.sleep(0)
            self.assertEqual(requests,['旧项目'])
            self.assertEqual(self.store.get('s',key)['requested_at'],300)
        finally:await manager.shutdown()


class PriorityMigration(unittest.TestCase):
    def test_relevance_survives_restart_without_promoting_late_results(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'tasks.db';db=sqlite3.connect(path)
            db.execute('CREATE TABLE tasks (id TEXT PRIMARY KEY, scope TEXT NOT NULL, title TEXT, state TEXT, text TEXT, cursor INTEGER, delivery TEXT, created REAL, updated REAL, request TEXT)')
            db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)',('old','s','旧项目','completed','完整结果。',2,'paused',100,900,'旧项目'))
            db.commit();db.close()
            store=TaskStore(path)
            self.assertEqual(store.get('s','old')['requested_at'],100)
            with patch('task_store.time.time',return_value=200):new=store.create('s','新项目')
            store.update('s','old',text='完整结果。后来补充。')
            self.assertEqual(store.list('s')[0]['id'],new)
            with patch('task_store.time.time',return_value=300):store.touch('s','old')
            store.db.close();store=TaskStore(path)
            row=store.get('s');self.assertEqual(row['id'],'old')
            self.assertEqual((row['requested_at'],row['cursor'],row['text']),(300,2,'完整结果。后来补充。'))
            with self.assertRaises(ValueError):store.touch('other','old')
            store.db.close()


if __name__=='__main__':unittest.main()
