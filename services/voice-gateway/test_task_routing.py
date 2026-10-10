import asyncio,unittest,tempfile
from pathlib import Path
from task_store import TaskStore
from background_tasks import BackgroundTasks
from task_routing import routing

class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.path=Path(self.directory.name)/'tasks.sqlite3'
        self.store=TaskStore(self.path);self.manager=BackgroundTasks(self.store)
        self.gate=asyncio.Event();self.calls=0
        self.intent={'kind':'lookup','subject':'Hard18','goal':'purpose and RL relationship','time_range':'','conditions':[]}
    async def asyncTearDown(self):
        self.gate.set();await self.manager.shutdown();self.store.db.close();self.directory.cleanup()
    async def stream(self,message,context,stop,emit,stage):
        self.calls+=1;await self.gate.wait();emit('test result')
    def submit(self,request_id,message='first wording',scope='s',**changes):
        args={'operation':'create','request_id':request_id,'intent':self.intent,**changes}
        route=routing(args,request_id,self.store,scope)
        return self.manager.submit(scope,'call:'+request_id,False,message,self,{},**route)
    async def test_paraphrases_coalesce_by_intent_not_prose(self):
        a,_=self.submit('one');b,reused=self.submit('two','completely different words')
        self.assertEqual(a,b);self.assertTrue(reused)
        await asyncio.sleep(0.02);self.assertEqual(self.calls,1)
        self.assertEqual(self.store.context_rows('s')[0]['intent'],self.intent)
    async def test_identical_prose_different_conditions_and_scope_stay_separate(self):
        a,_=self.submit('one')
        b,_=self.submit('two',intent={**self.intent,'conditions':['only failed experiments']})
        c,_=self.submit('three',intent={**self.intent,'time_range':'yesterday'})
        d,_=self.submit('four',scope='other')
        self.assertEqual(len({a,b,c,d}),4)
    async def test_retry_persists_after_completion_and_store_reopen(self):
        a,_=self.submit('one');self.gate.set();await asyncio.gather(*self.manager.jobs.values())
        self.store.db.close();self.store=TaskStore(self.path);self.manager=BackgroundTasks(self.store)
        b,reused=self.submit('one','paraphrased retry')
        self.assertEqual(a,b);self.assertTrue(reused);self.assertFalse(self.manager.jobs)
        c,reused=self.submit('two');self.assertNotEqual(a,c)
    async def test_conflicting_request_id_rejected(self):
        self.submit('one')
        with self.assertRaises(ValueError):self.submit('one',intent={**self.intent,'subject':'Hard19'})
    async def test_requery_requires_owned_parent_and_coalesces_while_running(self):
        old,_=self.submit('one');self.gate.set();await asyncio.gather(*self.manager.jobs.values());self.gate.clear()
        with self.assertRaises(ValueError):self.submit('two',operation='requery')
        with self.assertRaises(ValueError):self.submit('two',operation='requery',existing_task_id=old,scope='other')
        a,_=self.submit('two',operation='requery',existing_task_id=old)
        b,reused=self.submit('three',operation='requery',existing_task_id=old)
        self.assertNotEqual(old,a);self.assertEqual(a,b);self.assertTrue(reused)
    async def test_actions_only_reuse_exact_request_id(self):
        intent={**self.intent,'kind':'action'}
        a,_=self.submit('one',intent=intent);b,_=self.submit('two',intent=intent)
        c,reused=self.submit('one',intent=intent)
        self.assertNotEqual(a,b);self.assertEqual(a,c);self.assertTrue(reused)
    async def test_missing_or_invalid_intent_does_not_start(self):
        for intent in (None,{},dict(self.intent,conditions='invalid')):
            with self.assertRaises(ValueError):self.submit('one',intent=intent)
        self.assertFalse(self.manager.jobs)

if __name__=='__main__':unittest.main()
