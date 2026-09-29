"""Replay actual ASR handling and playback/cancellation ordering without APIs."""
import ast
import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from conversation_memory import normalize_history
from speech_output import SpeechOutput
from task_reports import TaskReports
from task_store import TaskStore
from turn_scheduler import TurnScheduler


class InputTests(unittest.IsolatedAsyncioTestCase):
    async def replay(self, incoming):
        events=[];scheduler=TurnScheduler(.001)
        async def client(event):events.append(event)
        output=SpeechOutput(scheduler,client)
        tree=ast.parse(Path(__file__).with_name('doubao_server.py').read_text(encoding='utf-8'))
        downstream=next(n for n in ast.walk(tree) if isinstance(n,ast.AsyncFunctionDef) and n.name=='downstream')
        wrapper=ast.parse('''async def replay():
    current_question=None;previous_question=None;turn_interrupted=False
    blocked=set();streamed_questions=set();tool_questions={};voice_text={}
''').body[0]
        wrapper.body += [downstream,ast.parse('await downstream()').body[0]]
        async def remote():
            for event in incoming:yield json.dumps(event)
        context={'history':[]}
        tools=SimpleNamespace(scheduler=scheduler,audio=output,interrupt=scheduler.interrupt,
                              response_ready=asyncio.Event())
        namespace=dict(json=json,tools=tools,remote=remote(),usage=SimpleNamespace(observe=lambda event:None),
                       send_client=client,context=context,normalize_history=normalize_history)
        exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper],type_ignores=[])),'<ASR-replay>','exec'),namespace)
        await namespace['replay']()
        return events,output,scheduler,context

    async def test_missing_start_event_still_clears_output_and_blocks_old_audio(self):
        events,output,scheduler,context=await self.replay([
            {'type':'conversation.item.input_audio_transcription.completed','item_id':'q1','text':'你好'},
            {'type':'response.output_audio.started','response_id':'r1','question_id':'q1'},
            {'type':'response.output_audio.delta','delta':'old'},
            {'type':'conversation.item.input_audio_transcription.delta','item_id':'q2','delta':'先停一下'},
            {'type':'response.output_audio.delta','delta':'late-old'},
            {'type':'conversation.item.input_audio_transcription.completed','item_id':'q2','text':'先停一下'},
            {'type':'response.output_audio.started','response_id':'r2','question_id':'q2'},
            {'type':'response.output_audio.delta','delta':'new'},
        ])
        self.assertEqual([e['delta'] for e in events if e['type']=='response.output_audio.delta'],['old','new'])
        self.assertEqual(output.turn,2)
        self.assertEqual([m['text'] for m in context['history']],['你好','先停一下'])

    async def test_duplicate_start_and_late_old_completion_do_not_interrupt_new_turn(self):
        events,output,scheduler,context=await self.replay([
            {'type':'conversation.item.input_audio_transcription.started','item_id':'q1'},
            {'type':'conversation.item.input_audio_transcription.completed','item_id':'q1','text':'第一句'},
            {'type':'conversation.item.input_audio_transcription.started','item_id':'q2'},
            {'type':'conversation.item.input_audio_transcription.started','item_id':'q2'},
            {'type':'conversation.item.input_audio_transcription.completed','item_id':'q1','text':'迟到的第一句'},
            {'type':'conversation.item.input_audio_transcription.delta','item_id':'q2','delta':'第二句'},
        ])
        self.assertEqual(output.turn,2)
        self.assertEqual(output.question,'q2');self.assertTrue(scheduler.user)
        self.assertEqual([m['text'] for m in context['history']],['第一句'])

    async def test_consecutive_short_turns_without_item_ids_each_interrupt_once(self):
        incoming=[]
        for i in range(4):
            incoming.extend([{'type':'conversation.item.input_audio_transcription.started'},
                             {'type':'conversation.item.input_audio_transcription.delta','delta':str(i)},
                             {'type':'conversation.item.input_audio_transcription.completed','text':str(i)}])
        _,output,_,context=await self.replay(incoming)
        self.assertEqual(output.turn,4);self.assertEqual(len(context['history']),4)


class ReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=TaskStore(':memory:');self.key=self.store.create('s','最后一句测试')
        self.store.update('s',self.key,state='completed',text='最后一句。')
        self.scheduler=TurnScheduler(.001);self.ready=asyncio.Event();self.spoken=[];self.tokens=[]
        async def speak(text,client,stop,valid):
            await client({'type':'opening.audio','delta':text});return True
        async def client(event):
            if event['type']=='agent.audio.delta':self.spoken.append(event['delta'])
            if event['type']=='task.segment.end':
                if self.spoken[-1]=='最后一句。':self.tokens.append(event['token']);self.ready.set()
                else:
                    self.output.playback({'playing':False,'generation':event['generation']})
                    self.reports.acknowledge(event['token'])
        self.output=SpeechOutput(self.scheduler,client)
        self.reports=TaskReports(self.store,'s',self.scheduler,client,speak,output=self.output)
        self.reports.start(self.key);await asyncio.wait_for(self.ready.wait(),1)

    async def asyncTearDown(self):
        await self.reports.close();self.store.db.close()

    async def interrupt(self):
        self.scheduler.update(user=True,generating=False)
        await self.output.user_started('new-question')

    async def finish_turn(self):
        self.scheduler.update(user=False,generating=False,playing=False)
        await asyncio.wait_for(self.reports.jobs[self.key],1)

    async def test_ack_and_interrupt_same_tick_preserves_completed_cursor(self):
        self.reports.acknowledge(self.tokens[-1])
        await self.interrupt();await self.finish_turn()
        self.assertEqual(self.store.get('s',self.key)['delivery'],'reported')
        self.assertFalse(any('还要继续听' in text for text in self.spoken))

    async def test_final_playback_ack_arriving_after_interrupt_prevents_false_resume_question(self):
        token=self.tokens[-1]
        await self.interrupt();await asyncio.sleep(.02)
        self.reports.acknowledge(token)
        await self.finish_turn()
        self.assertEqual(self.store.get('s',self.key)['cursor'],len('最后一句。'))
        self.assertEqual(self.store.get('s',self.key)['delivery'],'reported')
        self.assertFalse(any('还要继续听' in text for text in self.spoken))

    async def test_partial_segment_is_unread_and_old_receipt_cannot_advance_replay(self):
        old=self.tokens[-1]
        await self.interrupt();await self.finish_turn()
        self.assertEqual(self.store.get('s',self.key)['cursor'],0)
        self.ready.clear();self.reports.start(self.key,restart=True)
        await asyncio.wait_for(self.ready.wait(),1)
        self.reports.acknowledge(old)
        self.assertEqual(self.store.get('s',self.key)['cursor'],0)
        self.reports.acknowledge(self.tokens[-1]);self.output.playback({'playing':False})
        await asyncio.wait_for(self.reports.jobs[self.key],1)
        self.assertEqual(self.store.get('s',self.key)['delivery'],'reported')

    async def test_user_can_request_continue_before_the_idle_resume_prompt(self):
        await self.interrupt();await asyncio.sleep(.02)
        self.ready.clear()
        self.assertIn('已安排',self.reports.start(self.key))
        self.scheduler.update(user=False,generating=False,playing=False)
        await asyncio.wait_for(self.ready.wait(),1)
        self.reports.acknowledge(self.tokens[-1]);self.output.playback({'playing':False})
        await asyncio.wait_for(self.reports.jobs[self.key],1)
        self.assertEqual(self.store.get('s',self.key)['delivery'],'reported')
        self.assertFalse(any('还要继续听' in text for text in self.spoken))


if __name__=='__main__':unittest.main()
