"""Accepted requests must survive a missing history cursor without another POST."""
import ast
import asyncio
import json
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit,urlunsplit,parse_qs
from agent_errors import AgentFailure
from background_tasks import BackgroundTasks
from task_store import TaskStore


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self,*,mode='queued',cursor_available=False,polls=None):
        sent=[];gets=[];output=[];stages=[]
        polls=list(polls or ['answer'])
        class Response:
            headers={'content-type':'text/event-stream'}
            def __init__(self,status=200,messages=None,body=None):
                self.status_code=status;self.messages=messages or [];self.body=body
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def aread(self):return b'{}'
            def json(self):return {'messages':self.messages}
            async def aiter_lines(self):
                identity={'scene_id':self.body['scene_id'],'stream_id':'owned'}
                events=[('meta',identity)]
                if mode=='partial_eof':events.append(('content_block_delta',{**identity,'delta':{'text':'已收到的一段。'}}))
                else:
                    events += [('content_block_start',{**identity,'content_block':{'type':'thinking'}}),('message_stop',identity)]
                    if mode=='continued':
                        events += [('content_block_delta',{**identity,'delta':{'text':'实际正文。'}}),('message_stop',identity)]
                for kind,data in events:
                    yield 'event: '+kind;yield 'data: '+json.dumps(data);yield ''
        class Client:
            def __init__(self,*args,**kwargs):pass
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def get(self,url,**kwargs):
                gets.append(url)
                if len(gets)==1:
                    if not cursor_available:raise TimeoutError('synthetic private network detail')
                    return Response(messages=[{'seq':100}])
                event=polls.pop(0) if polls else 'answer'
                if event=='network':raise ConnectionError('must never leak a credential-bearing URL')
                if event=='http':return Response(status=503)
                if event=='unrelated':return Response(messages=[{'seq':102,'role':'assistant','scene_id':'other-scene','content':'不能混入这段正文。'}])
                scene=sent[0]['scene_id']
                if event=='yielded':return Response(messages=[{'seq':103,'role':'user','from':'system','scene_id':scene,'content':'[breath yielded to human]'}])
                return Response(messages=[{'seq':104,'role':'assistant','scene_id':scene,'content':'排队后完整返回。'}])
            def stream(self,method,url,json):
                self.assert_post=method=='POST'
                sent.append(json)
                return Response(status=202 if mode=='queued' else 200,body=json)
        clock=SimpleNamespace(create_task=asyncio.create_task,wait_for=asyncio.wait_for,wait=asyncio.wait,
                              gather=asyncio.gather,sleep=lambda _:asyncio.sleep(0))
        tree=ast.parse(Path(__file__).with_name('agent_bridge.py').read_text(encoding='utf-8'))
        namespace=dict(asyncio=clock,json=json,time=time,uuid=uuid,Path=Path,urlsplit=urlsplit,urlunsplit=urlunsplit,
            AgentFailure=AgentFailure,httpx=SimpleNamespace(AsyncClient=Client,Timeout=lambda *a,**kw:None,HTTPError=ConnectionError))
        definitions=[n for n in tree.body if isinstance(n,(ast.AsyncFunctionDef,ast.ClassDef))]
        exec(compile(ast.Module(body=definitions,type_ignores=[]),'<adapter>','exec'),namespace)
        bridge=namespace['AgentBridge'](url='https://agent.example.test/fixture/api/chat/stream?token=fixture')
        error=None
        try:await asyncio.wait_for(bridge.stream('合成查询',{'scene':'fixture'},threading.Event(),output.append,stage=stages.append),1)
        except AgentFailure as exc:error=exc
        self.assertEqual(len(sent),1,'An accepted operation must never be submitted again')
        return output,gets,stages,error

    async def test_queued_after_preflight_timeout_recovers_by_scene(self):
        output,gets,_,error=await self.run_case(polls=['network','http','unrelated','answer'])
        self.assertIsNone(error);self.assertEqual(output,['排队后完整返回。'])
        self.assertNotIn('after',parse_qs(urlsplit(gets[1]).query))
        self.assertEqual(parse_qs(urlsplit(gets[-1]).query)['after'],['102'])

    async def test_existing_cursor_is_preserved_for_queue_polling(self):
        output,gets,_,error=await self.run_case(cursor_available=True)
        self.assertIsNone(error);self.assertEqual(output,['排队后完整返回。'])
        self.assertEqual(parse_qs(urlsplit(gets[1]).query)['after'],['100'])

    async def test_empty_intermediate_message_stop_does_not_discard_later_text(self):
        output,gets,_,error=await self.run_case(mode='continued')
        self.assertIsNone(error);self.assertEqual(output,['实际正文。'])
        self.assertEqual(len(gets),1)

    async def test_empty_stream_can_recover_a_saved_reply(self):
        output,_,stages,error=await self.run_case(mode='empty')
        self.assertIsNone(error);self.assertEqual(output,['排队后完整返回。'])
        self.assertIn('awaiting_reply',stages)

    async def test_explicit_upstream_yield_is_not_a_successful_empty_reply(self):
        output,_,_,error=await self.run_case(mode='empty',polls=['yielded'])
        self.assertEqual(output,[]);self.assertEqual(error.code,'upstream_interrupted')

    async def test_partial_broken_stream_does_not_duplicate_history(self):
        output,gets,_,error=await self.run_case(mode='partial_eof')
        self.assertEqual(output,['已收到的一段。']);self.assertEqual(len(gets),1)
        self.assertEqual(error.code,'upstream_connection_error')

    async def test_failure_details_are_persisted_without_raw_exception_text(self):
        store=TaskStore(':memory:');manager=BackgroundTasks(store)
        class Bridge:
            async def stream(self,*args,**kwargs):raise AgentFailure('upstream_interrupted')
        class Unsafe:
            async def stream(self,*args,**kwargs):raise RuntimeError('https://fixture.test/?token=do-not-copy')
        try:
            first,_=manager.submit('s',None,False,'合成查询',Bridge(),{'scene':'fixture'})
            second,_=manager.submit('s',None,False,'合成查询',Unsafe(),{'scene':'fixture'})
            await asyncio.gather(*list(manager.jobs.values()))
            row=store.page('s',first)
            self.assertEqual(row['state'],'interrupted');self.assertEqual(row['error_code'],'upstream_interrupted')
            self.assertIn('中断',row['error_message'])
            self.assertEqual(store.get('s',second)['error_code'],'unexpected_error')
            self.assertNotIn('do-not-copy',json.dumps(store.list('s')))
            self.assertEqual(store.get('s',second)['text'],'')
        finally:await manager.shutdown();store.db.close()


if __name__=='__main__':unittest.main()
