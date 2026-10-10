from input_health import InputHealth
"""Replay real tool-result gating and provider output without provider APIs."""
import ast
import asyncio
import json
import logging
import re
import threading
import time
import unittest
from pathlib import Path
from background_tasks import BackgroundTasks
from conversation_memory import normalize_history
from speech_output import SpeechOutput
from task_reports import TaskReports
from task_store import TaskStore
from turn_scheduler import TurnScheduler
from types import SimpleNamespace


class NaturalReplyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=TaskStore(':memory:');self.manager=BackgroundTasks(self.store,timeout=3)
        self.events=[];self.upstream=[];self.remote=asyncio.Queue();self.provider_audio=True;self.provider_response='natural-r'
        self.gate=asyncio.Event();self.started=asyncio.Event();self.provider_seen=asyncio.Event()
        self.body='已经核实北辰项目有两个尚未完成的验收项。'
        owner=self
        class Bridge:
            async def stream(self,message,context,stop,emit,stage):
                owner.started.set();await owner.gate.wait();emit(owner.body)
        self.bridge=Bridge()
        async def speak(text,client,stop,valid,**kwargs):
            await client({'type':'opening.audio','delta':text});return True
        async def client(event):
            self.events.append(event)
            if event['type'] in ('opening.audio','agent.audio.delta'):
                self.tool.audio.playback({'playing':False,'generation':event.get('generation')})
            if event['type']=='task.segment.end':
                self.tool.audio.playback({'playing':False});self.tool.reports.acknowledge(event['token'])
            if event['type']=='response.output_audio.delta':
                self.provider_seen.set();self.tool.audio.playback({'playing':False,'generation':event['generation']})
        async def send_up(data):
            self.upstream.append(data)
            if self.provider_audio:
                for event in [
                    {'type':'response.output_audio.started','response_id':self.provider_response,'question_id':'q1'},
                    {'type':'response.output_audio.delta','delta':'spoken-key-facts'},
                    {'type':'response.output_audio.done','response_id':self.provider_response,'question_id':'q1'},
                    {'type':'response.done','response_id':self.provider_response,'question_id':'q1'},
                ]:await self.remote.put(json.dumps(event))
        async def remote():
            while True:
                raw=await self.remote.get()
                if raw is None:return
                yield raw
        tree=ast.parse(Path(__file__).with_name('doubao_server.py').read_text())
        namespace=dict(connection_id="test",input_health=InputHealth(),asyncio=asyncio,json=json,re=re,time=time,threading=threading,
                       logger=logging.getLogger('natural-test'),logging=logging,
                       DEFAULT_VOICE='test',background_tasks=self.manager,bridge=self.bridge,
                       task_store=self.store,TaskReports=TaskReports,TurnScheduler=TurnScheduler,
                       SpeechOutput=SpeechOutput,speak_opening=speak,normalize_history=normalize_history,
                       usage=SimpleNamespace(observe=lambda event:None),send_up=send_up,send_client=client,
                       remote=remote(),context={'history':[]})
        definitions=[n for n in tree.body if isinstance(n,(ast.ClassDef,ast.FunctionDef)) and n.name in ('ToolCalls','query_identity')]
        exec(compile(ast.Module(body=definitions,type_ignores=[]),'<real-tool-calls>','exec'),namespace)
        self.tool=namespace['ToolCalls'](send_up,client,context={'scene':'natural-test','scope':'s'},natural_answers=True)
        self.tool.scheduler.quiet=.001;self.tool.natural_timeout=.08
        namespace['tools']=self.tool
        callback=next(n for n in ast.walk(tree) if isinstance(n,ast.AsyncFunctionDef) and n.name=='send_tool_result')
        downstream=next(n for n in ast.walk(tree) if isinstance(n,ast.AsyncFunctionDef) and n.name=='downstream')
        wrapper=ast.parse('''async def setup():
    current_question='q1';previous_question=None;turn_interrupted=True
    blocked=set();streamed_questions={'q1'};tool_questions={'c1':'q1'};voice_text={}
''').body[0]
        wrapper.body += [callback,downstream,*ast.parse('tools.up=send_tool_result\nreturn downstream').body]
        exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper],type_ignores=[])),'<real-downstream>','exec'),namespace)
        self.receiver=asyncio.create_task((await namespace['setup']())())
        await self.tool.audio.user_started('q1');await self.tool.audio.block(question='q1')
        self.tool.watcher=asyncio.create_task(self.tool.watch())

    async def asyncTearDown(self):
        self.gate.set();await self.tool.close();await self.manager.shutdown()
        await self.remote.put(None);await self.receiver
        self.store.db.close()

    def submit(self):
        self.tool.submit([{'call_id':'c1','name':'ask_agent','arguments':json.dumps({'message':'查询北辰项目验收进度','title':'北辰验收进度'})}])

    async def until(self,predicate):
        async with asyncio.timeout(3):
            while not predicate():await asyncio.sleep(.005)

    async def test_real_tool_return_unlocks_natural_voice_once_without_body_tts(self):
        self.submit();await self.started.wait();self.gate.set()
        await asyncio.wait_for(self.provider_seen.wait(),2)
        await self.until(lambda:all(task.done() for task,stop in self.tool.pending))
        tool_returns=[d for d in self.upstream if d['type']=='conversation.item.create']
        self.assertEqual(len(tool_returns),1)
        self.assertTrue(tool_returns[0]['items'][0]['content'][0]['text'].startswith('[语音自然回答]'))
        self.assertEqual([e['delta'] for e in self.events if e['type']=='response.output_audio.delta'],['spoken-key-facts'])
        row=self.store.get('s');self.assertEqual(row['cursor'],0)
        self.assertEqual(row['delivery'],'awaiting_choice')
        self.assertFalse(any(e['type']=='agent.audio.delta' and e['delta']==self.body for e in self.events))
        self.tool.submit([{'call_id':'c1','name':'ask_agent','arguments':'{}'}])
        await asyncio.sleep(.02);self.assertEqual(len(self.upstream),1)

    async def test_new_question_keeps_old_result_silent_and_uses_original_preview(self):
        self.provider_audio=False;self.submit();await self.started.wait()
        await self.remote.put(json.dumps({'type':'conversation.item.input_audio_transcription.completed','item_id':'q2','text':'我想问另一个问题'}))
        await self.until(lambda:self.tool.audio.turn==2)
        self.tool.scheduler.update(generating=False);self.gate.set()
        await self.until(lambda:any(e['type']=='agent.audio.delta' and e['delta']==self.body for e in self.events))
        self.assertFalse(any('[语音自然回答]' in d['items'][0]['content'][0]['text'] for d in self.upstream))
        self.assertFalse(any(e['type']=='response.output_audio.delta' for e in self.events))

    async def test_no_provider_audio_falls_back_without_resending_tool_call(self):
        self.provider_audio=False;self.submit();await self.started.wait();self.gate.set()
        await self.until(lambda:any(e['type']=='agent.audio.delta' and e['delta']==self.body for e in self.events))
        self.assertEqual(len(self.upstream),1)
        self.assertEqual(self.store.get('s')['cursor'],len(self.body))
        for event in [
            {'type':'response.output_audio.started','response_id':'too-late','question_id':'q1'},
            {'type':'response.output_audio.delta','delta':'late-natural-audio'},
        ]:await self.remote.put(json.dumps(event))
        await asyncio.sleep(.02)
        self.assertFalse(any(e['type']=='response.output_audio.delta' for e in self.events))

    async def test_early_useful_stage_returns_once_and_keeps_later_body_saved(self):
        finish=asyncio.Event();owner=self
        class StreamingBridge:
            async def stream(self,message,context,stop,emit,stage):
                emit(owner.body);owner.started.set();await finish.wait();emit('最终结果：其他验收项已经完成。')
        self.tool.bridge=StreamingBridge();self.submit();await self.started.wait()
        try:
            await asyncio.wait_for(self.provider_seen.wait(),2)
            self.assertEqual(self.store.get('s')['state'],'running')
            payload=self.upstream[0]['items'][0]['content'][0]['text']
            self.assertIn('"partial": true',payload)
            finish.set();await self.until(lambda:self.store.get('s')['state']=='completed')
            await asyncio.sleep(.3)
            self.assertEqual(len(self.upstream),1)
            self.assertTrue(self.store.get('s')['text'].endswith('其他验收项已经完成。'))
            self.assertEqual(self.store.get('s')['cursor'],0)
            notices=[e for e in self.events if e['type']=='agent.status' and '后续内容已保存' in e['text']]
            self.assertEqual(len(notices),1)
            self.assertEqual(len([e for e in self.events if e['type']=='opening.audio' and '刚才任务的完整结果：其他验收项已经完成。' in e['delta']]),1)
        finally:finish.set()

    async def test_redirect_during_natural_wait_keeps_new_turn_busy_and_blocks_old_audio(self):
        self.provider_audio=False;self.submit();await self.started.wait();self.gate.set()
        await self.until(lambda:bool(self.upstream))
        await self.remote.put(json.dumps({'type':'conversation.item.input_audio_transcription.completed','item_id':'q2','text':'现在回答我的新问题'}))
        await self.until(lambda:self.tool.audio.turn==2)
        await self.until(lambda:all(task.done() for task,stop in self.tool.pending))
        self.assertTrue(self.tool.scheduler.generating,'Old timeout must not clear new question generation')
        self.assertFalse(any(e['type']=='agent.audio.delta' and e['delta']==self.body for e in self.events))
        for event in [
            {'type':'response.output_audio.started','response_id':'old-late','question_id':'q1'},
            {'type':'response.output_audio.delta','delta':'late-old'},
        ]:await self.remote.put(json.dumps(event))
        await asyncio.sleep(.02)
        self.assertFalse(any(e['type']=='response.output_audio.delta' for e in self.events))
        # A real forwarded q2 answer must never count as q1's first audio.
        for event in [
            {'type':'response.output_audio.started','response_id':'new-r','question_id':'q2'},
            {'type':'response.output_audio.delta','delta':'new-question-audio'},
            {'type':'response.output_audio.done','response_id':'new-r','question_id':'q2'},
            {'type':'response.done','response_id':'new-r','question_id':'q2'},
        ]:await self.remote.put(json.dumps(event))
        await self.until(lambda:any(e['type']=='agent.audio.delta' and e['delta']==self.body for e in self.events))
        self.assertEqual([e['delta'] for e in self.events if e['type']=='response.output_audio.delta'],['new-question-audio'])
        self.assertEqual(self.store.get('s')['cursor'],len(self.body))

    async def test_unforwarded_ended_stream_cannot_ack_natural_reply(self):
        self.provider_audio=False;self.submit();await self.started.wait();self.gate.set()
        await self.until(lambda:bool(self.upstream))
        self.tool.audio.observe({'type':'response.output_audio.started','response_id':'ended-r','question_id':'q1'})
        self.tool.audio.observe({'type':'response.output_audio.done','response_id':'ended-r','question_id':'q1'})
        await self.remote.put(json.dumps({'type':'response.output_audio.delta','delta':'ended-tail'}))
        await self.until(lambda:any(e['type']=='agent.audio.delta' and e['delta']==self.body for e in self.events))
        self.assertFalse(any(e['type']=='response.output_audio.delta' for e in self.events))
        self.assertEqual(self.store.get('s')['cursor'],len(self.body))

    async def test_redirect_before_first_body_reports_waiting_not_failure(self):
        self.provider_audio=False;self.submit();await self.started.wait()
        await self.remote.put(json.dumps({'type':'conversation.item.input_audio_transcription.completed','item_id':'q2','text':'换一个话题'}))
        await self.until(lambda:self.tool.audio.turn==2)
        self.tool.scheduler.update(generating=False)
        await self.until(lambda:bool(self.upstream))
        text=self.upstream[0]['items'][0]['content'][0]['text']
        self.assertIn('查询仍在后台进行',text)
        self.assertNotIn('查询没有收到正文',text)
        self.assertEqual(self.store.get('s')['state'],'running')

    async def test_provider_can_reuse_ended_function_response_id(self):
        self.provider_response='function-r'
        self.tool.audio.observe({'type':'response.output_audio.started','response_id':'function-r','question_id':'q1'})
        self.tool.audio.observe({'type':'response.output_audio.done','response_id':'function-r','question_id':'q1'})
        await self.tool.audio.block({'response_id':'function-r','question_id':'q1'})
        self.submit();await self.started.wait();self.gate.set()
        await asyncio.wait_for(self.provider_seen.wait(),2)
        self.assertEqual([e['delta'] for e in self.events if e['type']=='response.output_audio.delta'],['spoken-key-facts'])

    async def partial(self,additional='最终结果：新增的两项验收已经完成。'):
        owner=self
        class Bridge:
            async def stream(self,message,context,stop,emit,stage):
                emit(owner.body);owner.started.set();await owner.gate.wait()
                if additional:emit(additional)
        self.tool.bridge=Bridge();self.submit();await self.started.wait()
        await asyncio.wait_for(self.provider_seen.wait(),2)
        await self.until(lambda:all(task.done() for task,stop in self.tool.pending))
        return self.store.get('s')['id']

    async def test_later_source_clause_waits_for_new_question_and_is_spoken_once(self):
        key=await self.partial();self.tool.scheduler.update(user=True,generating=True)
        self.gate.set()
        await self.until(lambda:any(e['type']=='agent.status' and '后续内容已保存' in e['text'] for e in self.events))
        await asyncio.sleep(.03)
        supplements=lambda:[e for e in self.events if e['type']=='opening.audio' and '刚才任务的完整结果：' in e['delta']]
        self.assertEqual(supplements(),[])
        self.tool.scheduler.update(user=False,generating=False)
        await self.until(lambda:bool(supplements()));await asyncio.sleep(.3)
        self.assertEqual(len(supplements()),1)
        self.assertIn('新增的两项验收已经完成。',supplements()[0]['delta'])
        self.assertEqual(self.store.get('s',key)['cursor'],0)
        self.assertEqual(len(self.upstream),1)

    async def test_deferred_natural_task_has_no_later_supplement(self):
        key=await self.partial();await self.tool.defer(key);self.gate.set()
        await self.until(lambda:self.store.get('s',key)['state']=='completed');await asyncio.sleep(.3)
        self.assertFalse(any(e['type']=='opening.audio' and '刚才任务的完整结果：' in e['delta'] for e in self.events))
        self.assertEqual(self.store.get('s',key)['delivery'],'deferred')
        self.assertEqual(self.store.get('s',key)['cursor'],0)

    async def test_late_completion_without_new_body_has_no_supplement(self):
        key=await self.partial(additional='');self.gate.set()
        await self.until(lambda:self.store.get('s',key)['state']=='completed');await asyncio.sleep(.3)
        self.assertFalse(any(e['type']=='opening.audio' and '刚才任务的完整结果：' in e['delta'] for e in self.events))
        self.assertEqual(self.store.get('s',key)['cursor'],0)


    async def test_completion_during_audio_wait_returns_fresh_state_and_body(self):
        owner=self;waiting=asyncio.Event();release=asyncio.Event()
        class Bridge:
            async def stream(self,message,context,stop,emit,stage):
                emit(owner.body);owner.started.set();await owner.gate.wait()
                emit('最终结果：全部验收已完成。')
        self.tool.bridge=Bridge()
        original=self.tool.scheduler.wait_idle
        async def wait_idle(stop=None,**kwargs):
            if stop is None:
                waiting.set();await release.wait()
            return await original(stop,**kwargs)
        self.tool.scheduler.wait_idle=wait_idle
        self.submit();await asyncio.wait_for(waiting.wait(),2)
        self.gate.set();await self.until(lambda:self.store.get('s')['state']=='completed')
        release.set();await asyncio.wait_for(self.provider_seen.wait(),2)
        result=self.upstream[0]['items'][0]['content'][0]['text']
        self.assertIn('"state": "completed"',result)
        self.assertIn('"partial": false',result)
        self.assertIn('全部验收已完成',result)
        self.assertFalse(self.tool.natural_notices)

if __name__=='__main__':unittest.main()
