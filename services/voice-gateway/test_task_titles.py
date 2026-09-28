import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from background_tasks import BackgroundTasks
from task_reports import TaskReports
from task_store import TaskStore
from task_titles import task_title
from turn_scheduler import TurnScheduler


class Titles(unittest.TestCase):
    def test_short_specific_fallbacks(self):
        self.assertEqual(task_title('请帮我查一下项目甲周五的发布进度'), '项目甲周五的发布进度')
        self.assertEqual(task_title('请帮我取消明天下午的会议'), '取消明天下午的会议')
        self.assertEqual(task_title('请不要取消明天下午的会议'), '不要取消明天下午的会议')
        self.assertEqual(task_title('帮我查一下明天下午的会议', '查询任务'), '明天下午的会议')
        self.assertEqual(task_title('完整问题', '项目甲的发布进度'), '项目甲的发布进度')
        self.assertEqual(task_title('明天的日程', {'invalid': True}), '明天的日程')
        self.assertLessEqual(len(task_title('项目甲'*100)), 24)
        self.assertNotIn('\n', task_title('任务', '**项目甲**\n进度'))

    def test_legacy_migration_keeps_request_and_playback(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'tasks.db'
            db=sqlite3.connect(path)
            db.execute('CREATE TABLE tasks (id TEXT PRIMARY KEY, scope TEXT NOT NULL, title TEXT, state TEXT, text TEXT, cursor INTEGER, delivery TEXT, created REAL, updated REAL)')
            original='请帮我查一下项目甲周五的发布进度，说明阻碍和预计时间，不要替我做任何修改'
            db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)', ('old','a',original,'completed','已返回的完整内容。',3,'paused',1,2))
            db.commit();db.close()
            store=TaskStore(path);row=store.get('a','old')
            self.assertEqual(row['request'],original)
            self.assertTrue(row['title'].startswith('项目甲周五的发布进度'))
            self.assertEqual((row['text'],row['cursor'],row['delivery']),('已返回的完整内容。',3,'paused'))
            key=store.create('a','带有完整条件的新问题','项目乙的测试进度')
            store.db.close();store=TaskStore(path)
            self.assertEqual(store.get('a',key)['title'],'项目乙的测试进度')
            self.assertEqual(store.page('a',key)['request'],'带有完整条件的新问题')
            self.assertEqual(store.list('a')[0]['title'],'项目乙的测试进度')
            self.assertNotIn('request',store.list('a')[0])
            self.assertEqual(store.list('other'),[])
            store.db.close()


class TaskFlows(unittest.IsolatedAsyncioTestCase):
    async def test_title_does_not_change_query_or_duplicate_task(self):
        store=TaskStore(':memory:');manager=BackgroundTasks(store);gate=asyncio.Event();received=[]
        class Bridge:
            async def stream(self,message,context,stop,emit,stage):
                received.append(message);await gate.wait();emit('结果。')
        bridge=Bridge();message='请查项目甲周五的发布进度，重点说阻碍，不要修改任务'
        key,_=manager.submit('a','project-a',True,message,bridge,{},title='项目甲周五的发布进度')
        reused,yes=manager.submit('a','project-a',True,message,bridge,{},title='不能覆盖原标题')
        self.assertTrue(yes);self.assertEqual(reused,key)
        gate.set();await asyncio.gather(*manager.jobs.values());await asyncio.sleep(0)
        self.assertEqual(received,[message])
        self.assertEqual(store.get('a',key)['title'],'项目甲周五的发布进度')
        self.assertEqual(store.get('a',key)['request'],message)
        reused,yes=manager.submit('a','project-a',True,message,bridge,{},title='再次换名')
        self.assertTrue(yes);self.assertEqual(reused,key)
        await manager.shutdown();store.db.close()

    async def test_queued_reports_name_correct_task_and_resume(self):
        store=TaskStore(':memory:');scheduler=TurnScheduler(.001);spoken=[];events=[]
        first=store.create('a','查项目甲','项目甲的发布进度')
        second=store.create('a','查会议','明天下午的会议安排')
        for key in (first,second):store.update('a',key,state='completed',text='第一句。第二句。')
        async def speak(text,client,stop,valid):
            await client({'type':'opening.audio','delta':text});return True
        async def client(data):
            events.append(data)
            if data['type']=='agent.audio.delta':spoken.append((data['call_id'],data['delta']))
            if data['type']=='task.segment.end':
                scheduler.update(playing=False);reports.acknowledge(data['token'])
        reports=TaskReports(store,'a',scheduler,client,speak)
        scheduler.update(user=True)
        reports.start(first);reports.start(second);await asyncio.sleep(.03)
        self.assertEqual(spoken,[])
        scheduler.update(user=False)
        await asyncio.wait_for(asyncio.gather(*reports.jobs.values()),2)
        self.assertIn((first,'刚才查的项目甲的发布进度，有内容返回了。'),spoken)
        self.assertIn((second,'刚才查的明天下午的会议安排，有内容返回了。'),spoken)
        self.assertTrue(all('request' not in e['task'] for e in events if e['type']=='task.updated'))
        store.update('a',first,cursor=len('第一句。'),delivery='paused')
        spoken.clear();reports.start(first);await asyncio.wait_for(reports.jobs[first],2)
        self.assertIn((first,'接着说项目甲的发布进度，从刚才的位置继续。'),spoken)
        self.assertNotIn((first,'第一句。'),spoken)
        self.assertEqual(store.get('a',first)['cursor'],len('第一句。第二句。'))
        await reports.close();store.db.close()


if __name__=='__main__':unittest.main()
