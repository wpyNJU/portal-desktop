import asyncio
import ast
import json
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from conversation_memory import normalize_history
from speech_output import SpeechOutput
from task_reports import TaskReports
from task_store import TaskStore
from turn_scheduler import TurnScheduler


class SpeechTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scheduler=TurnScheduler(.001)
        self.events=[]
        async def client(data):self.events.append(data)
        self.output=SpeechOutput(self.scheduler,client)

    def frame(self,response,question='q1',value='reply'):
        return {'type':'response.output_audio.delta','response_id':response,'question_id':question,'delta':value}

    async def test_real_provider_unlabelled_pcm_and_usage_only_completion(self):
        for number in (1,2):
            q=f'q{number}';r=f'r{number}'
            await self.output.user_started(q)
            await self.output.provider({'type':'response.output_text.delta','response_id':r,'question_id':q,'delta':'你好'})
            await self.output.provider({'type':'response.output_audio.started','response_id':r,'question_id':q})
            generation=self.output.generation
            for _ in range(3):await self.output.provider({'type':'response.output_audio.delta','delta':'pcm'})
            self.assertEqual(self.output.generation,generation,'PCM must keep its audio.started owner')
            await self.output.provider({'type':'response.output_audio.done','response_id':r,'question_id':q})
            self.assertTrue(self.scheduler.generating)
            await self.output.provider({'type':'response.done','response':{'usage':{}}})
            self.assertFalse(self.scheduler.generating)
            self.assertTrue(self.scheduler.playing,'Generation completion must not claim playback drained')
            self.output.playback({'playing':False,'generation':generation})
        self.assertEqual(len([e for e in self.events if e['type']=='response.output_audio.delta']),6)

    async def test_muted_stream_metadata_still_labels_late_unlabelled_audio(self):
        await self.output.user_started('q1')
        await self.output.block({'response_id':'tool-response','question_id':'q1'})
        # A continuation starts after a new user turn; it must still stay muted.
        await self.output.user_started('q2')
        await self.output.provider({'type':'response.output_audio.started','response_id':'continuation','question_id':'q1'})
        await self.output.provider({'type':'response.output_audio.delta','delta':'muted continuation'})
        await self.output.provider({'type':'response.output_text.delta','response_id':'r2','question_id':'q2','delta':'new text'})
        await self.output.provider({'type':'response.output_audio.delta','delta':'late old PCM'})
        await self.output.provider({'type':'response.output_audio.done','response_id':'continuation','question_id':'q1'})
        await self.output.provider({'type':'response.done','response':{'usage':{}}})
        self.assertTrue(self.scheduler.generating,'Old completion cannot finish the new response')
        await self.output.provider({'type':'response.output_audio.started','response_id':'r2','question_id':'q2'})
        await self.output.provider({'type':'response.output_audio.delta','delta':'new PCM'})
        self.assertEqual([e['delta'] for e in self.events if e['type']=='response.output_audio.delta'],['new PCM'])

    async def test_late_provider_audio_cannot_take_over_a_scheduled_report(self):
        await self.output.provider(self.frame('r1'))
        await self.output.provider({'type':'response.output_audio.done','response_id':'r1'})
        await self.output.provider({'type':'response.done','response':{'usage':{}}})
        stop=threading.Event()
        await self.output.begin_scheduled(stop)
        await self.output.scheduled({'type':'agent.audio.delta','delta':'report'},stop)
        await self.output.provider(self.frame('r1',value='late reply'))
        self.assertEqual(self.events[-1]['delta'],'report')
        self.assertEqual(self.output.owner,('scheduled',stop))

    async def test_unattributed_audio_never_opens_an_output(self):
        await self.output.user_started('q1')
        await self.output.provider({'type':'response.output_audio.delta','delta':'orphan PCM'})
        self.assertFalse(any('delta' in e for e in self.events))

    async def test_server_observes_muted_headers_before_early_filtering(self):
        # Exercise the actual downstream loop without importing the live database/app.
        tree=ast.parse(Path(__file__).with_name('doubao_server.py').read_text(encoding='utf-8'))
        downstream=next(n for n in ast.walk(tree) if isinstance(n,ast.AsyncFunctionDef) and n.name=='downstream')
        wrapper=ast.parse('''async def replay():
    current_question=None;previous_question=None;turn_interrupted=False
    blocked=set();streamed_questions=set();tool_questions={};voice_text={}
''').body[0]
        wrapper.body.extend([downstream,ast.Expr(value=ast.Await(value=ast.Call(func=ast.Name(id='downstream',ctx=ast.Load()),args=[],keywords=[])))])
        incoming=[
            {'type':'conversation.item.input_audio_transcription.started','item_id':'q1'},
            {'type':'conversation.item.input_audio_transcription.completed','item_id':'q1','text':'查一下测试任务'},
            {'type':'response.output_text.delta','response_id':'r1','question_id':'q1','delta':'我来查'},
            {'type':'response.function_call_arguments.done','items':[{'name':'ask_agent','call_id':'c1'}]},
            {'type':'conversation.item.input_audio_transcription.started','item_id':'q2'},
            {'type':'conversation.item.input_audio_transcription.completed','item_id':'q2','text':'你好'},
            {'type':'response.output_text.delta','response_id':'r2','question_id':'q2','delta':'你好'},
            {'type':'response.output_audio.started','response_id':'continuation','question_id':'q1'},
            {'type':'response.output_audio.delta','delta':'old PCM'},
            {'type':'response.output_audio.done','response_id':'continuation','question_id':'q1'},
            {'type':'response.done','response':{'usage':{}}},
            {'type':'response.output_audio.started','response_id':'r2','question_id':'q2'},
            {'type':'response.output_audio.delta','delta':'new PCM'},
            {'type':'response.output_audio.done','response_id':'r2','question_id':'q2'},
            {'type':'response.done','response':{'usage':{}}},
        ]
        async def remote():
            for event in incoming:yield json.dumps(event)
        async def client(event):self.events.append(event)
        tools=SimpleNamespace(scheduler=self.scheduler,audio=self.output,interrupt=self.scheduler.interrupt,
                              submit=lambda items:None,response_ready=asyncio.Event())
        context={'history':[]}
        namespace=dict(json=json,tools=tools,remote=remote(),usage=SimpleNamespace(observe=lambda event:None),
                       send_client=client,context=context,normalize_history=normalize_history)
        exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper],type_ignores=[])),'<downstream>','exec'),namespace)
        await namespace['replay']()
        self.assertEqual([e['delta'] for e in self.events if e['type']=='response.output_audio.delta'],['new PCM'])
        self.assertFalse(self.scheduler.generating)
        self.assertEqual(context['history'][-1]['text'],'你好')

    async def test_foreground_preempts_report_and_drops_late_synthesis(self):
        started=asyncio.Event();finished=asyncio.Event();stops=[]
        async def report(stop):
            stops.append(stop)
            await self.output.begin_scheduled(stop)
            await self.output.scheduled({'type':'agent.audio.delta','delta':'report'},stop)
            started.set()
            try:await asyncio.sleep(10)
            finally:finished.set()
        pending=asyncio.create_task(self.scheduler.deliver(report))
        await started.wait()
        await self.output.provider(self.frame('r1'))
        await finished.wait()
        self.assertTrue(stops[0].is_set())
        self.assertFalse(await self.output.scheduled({'type':'agent.audio.delta','delta':'late report'},stops[0]))
        self.assertFalse(await self.output.scheduled({'type':'task.segment.end','token':'old'},stops[0]))
        audio=[e for e in self.events if 'delta' in e]
        self.assertEqual([e['delta'] for e in audio],['report','reply'])
        self.assertNotEqual(audio[0]['generation'],audio[1]['generation'])
        self.assertEqual(self.events[-2]['type'],'playback.clear')
        await self.output.provider({'type':'response.done','response':{'id':'r1'}})
        await self.output.provider({'type':'response.output_audio.done','response_id':'r1'})
        self.output.playback({'playing':False,'generation':self.output.generation})
        self.assertFalse(await asyncio.wait_for(pending,1))

    async def test_tool_handoff_silences_ack_and_continuation_immediately(self):
        await self.output.user_started('q1')
        await self.output.provider({'type':'response.created','response':{'id':'r1'},'question_id':'q1'})
        await self.output.block({'question_id':'q1','response_id':'r1'})
        self.assertFalse(await self.output.provider(self.frame('r1')))
        # Some provider frames omit question_id; the registered response still belongs to q1.
        self.assertFalse(await self.output.provider({'type':'response.output_audio.delta','response_id':'r1','delta':'late'}))
        self.assertFalse(await self.output.provider(self.frame('continuation','q1')))
        await self.output.user_started('q2')
        await self.output.provider(self.frame('r2','q2','new reply'))
        self.assertEqual([e['delta'] for e in self.events if 'delta' in e],['new reply'])

    async def test_old_response_and_playback_events_cannot_reopen_report_window(self):
        await self.output.user_started('q1')
        await self.output.provider(self.frame('r1'))
        old_generation=self.output.generation
        await self.output.user_started('q2')
        await self.output.provider(self.frame('r2','q2'))
        await self.output.provider({'type':'response.done','response':{'id':'r1'}})
        self.output.playback({'playing':False,'generation':old_generation})
        self.assertTrue(self.scheduler.generating)
        self.assertTrue(self.scheduler.playing)
        self.assertFalse(await self.output.provider(self.frame('r1')))
        await self.output.provider({'type':'response.done','response':{'id':'r2'}})
        self.assertTrue(self.scheduler.generating,'Audio completion must also be observed')
        await self.output.provider({'type':'response.output_audio.done','response_id':'r2'})
        self.assertFalse(self.scheduler.generating)

    async def test_alternating_responses_do_not_interleave(self):
        await self.output.provider(self.frame('r1',value='first'))
        await self.output.provider(self.frame('r2',value='second'))
        await self.output.provider(self.frame('r1',value='late first'))
        await self.output.provider(self.frame('r2',value='second continues'))
        self.assertEqual([e['delta'] for e in self.events if 'delta' in e],['first','second','second continues'])

    async def test_cleanup_of_old_report_does_not_clear_new_reply(self):
        stop=threading.Event()
        await self.output.begin_scheduled(stop)
        await self.output.provider(self.frame('r1'))
        count=len(self.events)
        await self.output.clear_scheduled(stop)
        self.assertEqual(len(self.events),count)

    async def test_interrupted_report_keeps_cursor_and_asks_after_new_reply_drains(self):
        store=TaskStore(':memory:');key=store.create('s','项目甲进度')
        store.update('s',key,state='completed',text='第一句。第二句。')
        second_playing=asyncio.Event();events=[];queue=[]
        async def speak(text,client,stop,valid):
            await client({'type':'opening.audio','delta':text});return True
        async def client(event):
            events.append(event)
            if event['type']=='playback.clear':queue.clear()
            if event['type'] in ('agent.audio.delta','response.output_audio.delta'):queue.append(event['delta'])
            if event['type']=='task.segment.end':
                if queue[-1]=='第二句。':second_playing.set()
                else:
                    output.playback({'playing':False,'generation':event['generation']})
                    reports.acknowledge(event['token'])
        output=SpeechOutput(self.scheduler,client)
        reports=TaskReports(store,'s',self.scheduler,client,speak,output=output)
        try:
            reports.start(key);await asyncio.wait_for(second_playing.wait(),1)
            self.assertEqual(store.get('s',key)['cursor'],len('第一句。'))
            self.scheduler.update(user=True)
            await output.user_started('q-new')
            self.scheduler.update(user=False,generating=True)
            await output.provider(self.frame('r-new','q-new','新问题的回复'))
            await asyncio.sleep(.05)
            self.assertEqual(queue,['新问题的回复'])
            self.assertFalse(any('还要继续听' in e.get('delta','') for e in events))
            await output.provider({'type':'response.done','response':{'id':'r-new'}})
            await output.provider({'type':'response.output_audio.done','response_id':'r-new'})
            await asyncio.sleep(.02)
            self.assertFalse(any('还要继续听' in e.get('delta','') for e in events))
            output.playback({'playing':False,'generation':output.generation})
            await asyncio.wait_for(reports.jobs[key],1)
            self.assertEqual(store.get('s',key)['cursor'],len('第一句。'))
            self.assertEqual(store.get('s',key)['delivery'],'awaiting_choice')
            self.assertTrue(any('关于项目甲进度' in e.get('delta','') and '还要继续听' in e['delta'] for e in events))
        finally:
            await reports.close();store.db.close()


if __name__=='__main__':unittest.main()
