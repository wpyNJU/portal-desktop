"""SQLite migration and real background lifecycle checks, no upstream traffic."""
import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from task_store import TaskStore
from background_tasks import BackgroundTasks
from agent_errors import AgentFailure

class StageStoreTests(unittest.TestCase):
    def test_legacy_migration_preserves_body_cursor_and_is_repeatable(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'tasks.db';db=sqlite3.connect(path)
            db.execute('CREATE TABLE tasks(id TEXT PRIMARY KEY,scope TEXT,title TEXT,state TEXT,text TEXT,cursor INTEGER,delivery TEXT,created REAL,updated REAL)')
            db.execute("INSERT INTO tasks VALUES('old','scope','旧查询','completed','完整正文保留',3,'paused',1,1)")
            db.commit();db.close()
            store=TaskStore(path);row=store.get('scope','old')
            self.assertEqual((row['text'],row['cursor']),('完整正文保留',3))
            self.assertEqual((row['stage'],row['stage_updated'],row['summary']),('',0,''))
            store.db.close();store=TaskStore(path)
            self.assertEqual(store.get('scope','old')['cursor'],3);store.db.close()
    def test_context_prefers_old_active_work_over_many_recent_completions(self):
        store=TaskStore(':memory:');active=store.create('s','最早运行任务')
        row=store.get('s',active);self.assertEqual(row['stage'],'queued');self.assertGreater(row['stage_updated'],0)
        for n in range(25):
            key=store.create('s',f'已完成任务{n}');store.update('s',key,state='completed',text='正文'*1000,summary='原文摘录：正文')
        store.create('other','另一用户任务')
        self.assertNotIn(active,[r['id'] for r in store.list('s')])
        rows=store.context_rows('s');self.assertEqual(len(rows),12);self.assertEqual(rows[0]['id'],active)
        self.assertTrue(all('text' not in row and 'request' not in row for row in rows))
        self.assertTrue(all('length' in row and 'summary' in row for row in rows))
        self.assertEqual(store.active_count('s'),1);self.assertEqual(store.active_count('missing'),0)
        self.assertIn('stage_updated',store.list('s')[0]);store.db.close()

class BackgroundStageTests(unittest.IsolatedAsyncioTestCase):
    async def test_safe_stage_persistence_and_terminal_excerpt_preserve_full_body(self):
        store=TaskStore(':memory:');manager=BackgroundTasks(store);started=asyncio.Event();release=asyncio.Event()
        body='这是实际返回的正文。'*80
        class Bridge:
            async def stream(self,message,context,stop,emit,stage):
                stage('thinking');started.set();await release.wait()
                stage('不要保存的推理内容');emit(body)
        key,_=manager.submit('s','a',False,'请求',Bridge(),{})
        await started.wait();row=store.get('s',key);self.assertEqual(row['stage'],'thinking')
        store.update('s',key,cursor=3,delivery='paused');release.set();await manager.jobs[key]
        row=store.get('s',key);self.assertEqual(row['state'],'completed');self.assertEqual(row['stage'],'completed')
        self.assertEqual(row['text'],body);self.assertEqual(row['cursor'],3);self.assertEqual(row['delivery'],'paused')
        self.assertEqual(row['summary'],'原文摘录：'+body[:235]);self.assertLessEqual(len(row['summary']),240)
        self.assertNotIn('推理',str(store.list('s')));store.db.close()
    async def test_failed_partial_output_remains_excerpt_with_error_authoritative(self):
        store=TaskStore(':memory:');manager=BackgroundTasks(store)
        class Bridge:
            async def stream(self,message,context,stop,emit,stage):
                emit('只收到部分原文。');raise AgentFailure('reply_timeout')
        key,_=manager.submit('s','b',False,'请求',Bridge(),{});await manager.jobs[key]
        row=store.get('s',key);self.assertEqual(row['state'],'error');self.assertEqual(row['stage'],'error')
        self.assertEqual(row['text'],'只收到部分原文。');self.assertEqual(row['summary'],'原文摘录：只收到部分原文。')
        self.assertEqual(row['error_code'],'reply_timeout');store.db.close()

if __name__=='__main__':unittest.main()
