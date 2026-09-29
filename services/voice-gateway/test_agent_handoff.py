"""Exercise the real streaming adapter with a captured, offline HTTP boundary."""
import ast
import asyncio
import copy
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from urllib.parse import urlsplit,urlunsplit
import uuid


class HandoffTests(unittest.IsolatedAsyncioTestCase):
    async def check_request(self,history):
        requests=[]
        class Response:
            status_code=200
            headers={'content-type':'text/event-stream'}
            def __init__(self,body=None):self.body=body
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            def json(self):return {'messages':[]}
            async def aiter_lines(self):
                identity={'scene_id':self.body['scene_id'],'stream_id':'stream-test'}
                events=[('meta',identity),
                        ('content_block_delta',{**identity,'event_id':1,'delta':{'text':'测试进度已返回。'}}),
                        ('message_stop',{**identity,'session_id':'session-next'})]
                for kind,data in events:
                    yield 'event: '+kind
                    yield 'data: '+json.dumps(data,ensure_ascii=False)
                    yield ''
        class Client:
            def __init__(self,*args,**kwargs):pass
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def get(self,*args,**kwargs):return Response()
            def stream(self,method,url,json):
                requests.append((method,url,copy.deepcopy(json)))
                return Response(json)
        # Avoid importing HTTP dependencies or reading any deployed credentials.
        tree=ast.parse(Path(__file__).with_name('agent_bridge.py').read_text(encoding='utf-8'))
        definitions=[n for n in tree.body if isinstance(n,(ast.AsyncFunctionDef,ast.ClassDef))]
        namespace=dict(asyncio=asyncio,json=json,time=time,uuid=uuid,Path=Path,urlsplit=urlsplit,urlunsplit=urlunsplit,
                       httpx=SimpleNamespace(AsyncClient=Client,Timeout=lambda *args,**kwargs:None,HTTPError=ConnectionError))
        exec(compile(ast.Module(body=definitions,type_ignores=[]),'<adapter>','exec'),namespace)
        bridge=namespace['AgentBridge'](url='https://agent.example.test/being/api/chat/stream?token=test-only')
        question='查询北辰项目本周的发布进度，仅查询，不修改任何内容；重点说明还未完成的验收项。'
        context={'scene':'voice-test','session_id':'session-existing','history':copy.deepcopy(history)}
        output=[]
        await bridge.stream(question,context,threading.Event(),output.append)
        self.assertEqual(len(requests),1)
        method,url,body=requests[0]
        self.assertEqual(method,'POST')
        self.assertEqual(body['message'],question)
        self.assertNotIn('history',body)
        self.assertNotIn('saved_conversation_json',json.dumps(body))
        self.assertNotIn('history-only-marker',json.dumps(body))
        self.assertTrue(body['scene_id'].startswith('voice-test-request-'))
        self.assertEqual(body['session_id'],'session-existing')
        self.assertEqual(body['scene_meta']['client'],'voice-call/1.0')
        self.assertEqual(output,['测试进度已返回。'])
        self.assertEqual(context['history'],history,'The voice model must keep its call history')
        self.assertEqual(context['session_id'],'session-next')

    async def test_empty_history(self):await self.check_request([])

    async def test_long_call_history_is_not_appended_to_agent_request(self):
        history=[{'role':'user','text':'history-only-marker。'+('这是一条与当前查询无关的通话记录。'*1000)},
                 {'role':'assistant','text':'已记住之前的通话信息。'},
                 {'role':'user','text':'现在讨论北辰项目。'}]
        await self.check_request(history)


if __name__=='__main__':unittest.main()
