import asyncio
import unittest
from task_context import TaskContextSync
from task_store import TaskStore


class ContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = TaskStore(':memory:')
        self.sent = []
        self.allowed = False
        self.focus = None
        self.worker = None
        self.delivered = asyncio.Event()

    async def asyncTearDown(self):
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        self.store.db.close()

    def task(self, delivery, scope='s'):
        key = self.store.create(scope, '合成测试问题', title='合成测试主题')
        self.store.update(scope, key, text='完整结果保留用于续播。', state='completed', delivery=delivery)
        return key

    def start(self):
        async def send(instructions):
            self.sent.append(instructions)
            self.delivered.set()
        self.sync = TaskContextSync(self.store, 's', lambda: '初始提示词与历史',
                                   lambda: self.focus, send, lambda: self.allowed, debounce=.01)
        self.worker = asyncio.create_task(self.sync.run())

    def event(self, key):
        self.sync.observe(self.store.get('s', key))

    async def test_many_restored_tasks_never_reload_prompt_or_modify_results(self):
        keys = [self.task(state) for state in ['awaiting_choice'] * 14 + ['paused'] * 26 + ['deferred'] * 4]
        self.focus = keys[0]
        other = self.task('awaiting_choice', scope='other')
        before = [self.store.get('s', key) for key in keys]
        self.start()
        self.allowed = True
        for _ in range(2):
            for key in keys:
                self.event(key)
        await asyncio.sleep(.05)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.sync.restored, 44)
        self.assertIn(keys[0], self.sync.instructions())  # Focus can be beyond the eight-item index.
        self.assertNotIn(other, self.sync.instructions())
        self.assertEqual([self.store.get('s', key) for key in keys], before)

    async def test_live_changes_wait_for_first_reply_and_idle_then_coalesce(self):
        keys = [self.task('paused') for _ in range(12)]
        self.start()
        for key in keys:
            self.store.update('s', key, delivery='deferred')
            self.event(key)
        await asyncio.sleep(.04)
        self.assertEqual(self.sent, [])
        self.allowed = True
        await asyncio.wait_for(self.delivered.wait(), .5)
        self.assertEqual(len(self.sent), 1)

    async def test_asking_then_awaiting_and_cursor_ticks_do_not_repeat_update(self):
        key = self.task('paused')
        self.focus = key
        self.start()
        self.allowed = True
        self.store.update('s', key, delivery='asking')
        self.event(key)
        await asyncio.wait_for(self.delivered.wait(), .5)
        self.store.update('s', key, delivery='awaiting_choice', cursor=2)
        for _ in range(10):
            self.event(key)
        await asyncio.sleep(.05)
        self.assertEqual(len(self.sent), 1)
        self.assertIn(key, self.sent[0])

    async def test_superseded_notice_does_not_publish_stale_control_state(self):
        key = self.task('paused')
        self.start()
        self.store.update('s', key, delivery='asking')
        self.event(key)
        self.store.update('s', key, delivery='reporting')
        self.event(key)
        self.allowed = True
        await asyncio.sleep(.05)
        self.assertEqual(self.sent, [])


if __name__ == '__main__':
    unittest.main()
