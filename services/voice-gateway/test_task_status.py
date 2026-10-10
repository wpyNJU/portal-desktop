"""Replay the real task watcher: restoration is distinct from a new failed query."""
import ast
import asyncio
import re
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from task_reports import TaskReports
from task_store import TaskStore
from turn_scheduler import TurnScheduler


class StatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_failure_new_failure_and_success_have_distinct_events(self):
        store=TaskStore(':memory:');events=[];scheduler=TurnScheduler()
        def task(state,text=''):
            key=store.create('s','合成查询')
            store.update('s',key,state=state,text=text,delivery='paused')
            return key
        old_error=task('error')
        old_success=task('completed','已保存的成功结果。')
        for _ in range(90):task('completed','旧正文。'*10000)
        running=task('running')
        async def client(event):events.append(event)
        async def speak(*args):raise AssertionError('Status restoration must not synthesize speech')
        reports=TaskReports(store,'s',scheduler,client,speak)
        owner=SimpleNamespace(scope='s',closed=False,progress=SimpleNamespace(observe=lambda row:None),reports=reports,client=client,manager=SimpleNamespace(progress={}),natural_notices={},natural_tasks=set())
        tree=ast.parse(Path(__file__).with_name('doubao_server.py').read_text(encoding='utf-8'))
        method=next(n for n in ast.walk(tree) if isinstance(n,ast.AsyncFunctionDef) and n.name=='watch')
        namespace={'task_store':store,'asyncio':asyncio,'re':re,'time':time}
        exec(compile(ast.Module(body=[method],type_ignores=[]),'<watch-test>','exec'),namespace)
        watcher=asyncio.create_task(namespace['watch'](owner))
        async def until(predicate):
            async with asyncio.timeout(2):
                while not predicate():await asyncio.sleep(.01)
        def terminal(key):return next((e for e in events if e.get('call_id')==key and e['type'] in ('agent.text.done','agent.text.error')),None)
        try:
            await until(lambda:any(e.get('task',{}).get('id')==running for e in events))
            self.assertEqual(len([e for e in events if e['type']=='task.updated']),1)
            self.assertFalse(any(e['type']=='agent.text.delta' for e in events))
            self.assertIsNone(terminal(old_error))
            self.assertIsNone(terminal(old_success))
            self.assertFalse(any(e.get('call_id') in (old_error,old_success) or e.get('task',{}).get('id') in (old_error,old_success) for e in events))
            store.update('s',running,state='error',text='仅收到部分结果。')
            new_success=task('completed','本次的新结果。')
            await until(lambda:terminal(running) and terminal(new_success))
            self.assertFalse(terminal(running)['restored'])
            self.assertIn('本次查询失败',terminal(running)['message'])
            self.assertIn('已保存部分正文',terminal(running)['message'])
            self.assertFalse(terminal(new_success)['restored'])
            self.assertEqual(terminal(new_success)['message'],'查询结果已返回')
            self.assertEqual(store.get('s',old_error)['state'],'error')
            self.assertEqual(store.get('s',running)['text'],'仅收到部分结果。')
        finally:
            watcher.cancel();await asyncio.gather(watcher,return_exceptions=True)
            await reports.close();store.db.close()


if __name__=='__main__':unittest.main()
