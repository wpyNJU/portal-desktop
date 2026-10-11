"""Explicit durable IDs prevent follow-up requests without guessing by title."""
import asyncio,json,unittest
import test_natural_reply as natural_fixture

class ExistingTaskTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=natural_fixture.NaturalReplyTests();await self.fixture.asyncSetUp()
        self.fixture.provider_audio=False
    async def asyncTearDown(self):await self.fixture.asyncTearDown()
    def submit(self,call_id,**args):
        self.fixture.tool.submit([{'call_id':call_id,'name':'ask_agent','arguments':json.dumps({'message':'查询北辰项目验收进度','title':'北辰验收进度','acknowledgement':'我去了解验收进展。',**args})}])
    async def reply(self,call_id):
        f=self.fixture
        await f.until(lambda:any(d['items'][0]['call_id']==call_id for d in f.upstream))
        return next(d['items'][0]['content'][0]['text'] for d in f.upstream if d['items'][0]['call_id']==call_id)
    async def test_running_and_completed_followups_return_immediately_without_submit(self):
        f=self.fixture;key=f.store.create('s','北辰验收进度')
        f.store.update('s',key,stage='thinking',text='甲'*1500,delivery='deferred')
        self.submit('reuse-running',existing_task_id=key)
        answer=json.loads((await self.reply('reuse-running')).removeprefix('[任务状态回答]'))
        self.assertEqual(answer['state'],'running');self.assertEqual(answer['stage'],'thinking')
        self.assertTrue(answer['reused']);self.assertFalse(answer['new_query'])
        self.assertLessEqual(len(answer['text']),1200)
        self.assertEqual(f.tool.task_ids['reuse-running'],key)
        self.assertFalse(f.started.is_set());self.assertEqual(len(f.store.list('s')),1)
        f.store.update('s',key,state='completed')
        self.submit('reuse-completed',existing_task_id=key,refresh=False)
        self.assertIn('completed',await self.reply('reuse-completed'))
        self.assertFalse(f.started.is_set());self.assertEqual(len(f.store.list('s')),1)
    async def test_actual_downstream_reopens_same_question_for_reused_native_answer(self):
        f=self.fixture;f.provider_audio=True
        key=f.store.create('s','旧诊断')
        f.store.update('s',key,state='completed',text='没有服务器访问权限，需要提供日志。',delivery='deferred')
        self.submit('c1',existing_task_id=key)
        await asyncio.wait_for(f.provider_seen.wait(),2)
        self.assertTrue(any(e['type']=='response.output_audio.delta' for e in f.events))
        self.assertFalse(any(e['type']=='agent.audio.delta' for e in f.events))
        self.assertFalse(f.started.is_set());self.assertEqual(len(f.store.list('s')),1)
        self.assertEqual(f.store.get('s',key)['delivery'],'deferred')
        self.assertEqual(f.store.get('s',key)['cursor'],0)
        self.assertEqual(f.tool.audio.turn,1)

    async def test_invalid_id_also_reopens_same_question_for_spoken_error(self):
        f=self.fixture;f.provider_audio=True
        self.submit('c1',existing_task_id='missing')
        await asyncio.wait_for(f.provider_seen.wait(),2)
        self.assertTrue((await self.reply('c1')).startswith('[任务状态回答]'))
        self.assertFalse(f.started.is_set());self.assertEqual(len(f.store.list('s')),0)

    async def test_foreign_or_unknown_id_never_submits_or_binds(self):
        f=self.fixture;foreign=f.store.create('other','同标题')
        for i,key in enumerate((foreign,'missing')):
            call='invalid-'+str(i);self.submit(call,existing_task_id=key)
            self.assertIn('未创建新查询',await self.reply(call));self.assertNotIn(call,f.tool.task_ids)
        self.assertFalse(f.started.is_set());self.assertEqual(len(f.store.list('s')),0)
    async def test_explicit_read_uses_id_despite_changed_wording(self):
        f=self.fixture;key=f.store.create('s','original request')
        self.submit('read-id',operation='read',request_id='read-1',existing_task_id=key,message='Any followup wording')
        self.assertIn('running',await self.reply('read-id'))
        self.assertFalse(f.started.is_set());self.assertEqual(len(f.store.list('s')),1)
    async def test_acknowledgement_cannot_trigger_report_replay(self):
        f=self.fixture;key=f.store.create('s','already answered')
        f.store.update('s',key,state='completed',text='结果已经回答。',delivery='awaiting_choice')
        f.tool.context['history']=[{'role':'user','text':'好的，谢谢。'}]
        # Pure acknowledgement variants must not schedule original-body TTS.
        for i,phrase in enumerate(('好的','谢谢','知道了')):
            f.tool.context['history'][-1]['text']=phrase
            call='replay-ack-'+str(i)
            await f.tool.task_action({'call_id':call,'arguments':json.dumps({'action':'continue','task_id':key})})
            self.assertIn('未安排报告',await self.reply(call))
        self.assertFalse(f.tool.reports.jobs)
    async def test_quick_ack_precedes_tool_and_query_does_not_wait_for_synthesis(self):
        f=self.fixture;entered=asyncio.Event();release=asyncio.Event();spoken=[]
        async def slow_speech(text,client,stop,valid,**kwargs):
            spoken.append(text);entered.set();await release.wait()
            if not stop.is_set() and valid():await client({'type':'opening.audio','delta':text})
        f.tool.run.__globals__['speak_opening']=slow_speech
        f.tool.scheduler.update(generating=True)
        f.tool.acknowledge_request()
        await asyncio.wait_for(entered.wait(),.3)
        self.assertFalse(f.started.is_set())
        self.submit('parallel-query')
        await asyncio.wait_for(f.started.wait(),.3)
        self.assertFalse(release.is_set(),'Query must start before acknowledgement finishes')
        release.set();f.tool.scheduler.update(generating=False)
        await f.until(lambda:bool(f.tool.acknowledged_turns))
        f.gate.set();await asyncio.sleep(.05)
        from voice_notices import ACKNOWLEDGEMENTS
        self.assertIn(spoken[0],ACKNOWLEDGEMENTS)
        self.assertEqual(len(spoken),1,'Do not repeat a transition when the real query starts')
    async def test_interrupted_quick_ack_does_not_speak_in_next_turn(self):
        f=self.fixture;f.tool.scheduler.update(user=True,generating=True)
        f.tool.acknowledge_request()
        await f.tool.audio.user_started('q2')
        f.tool.scheduler.update(user=False,generating=False)
        await asyncio.sleep(.06)
        self.assertFalse(any(e['type']=='opening.audio' for e in f.events))
    async def test_fast_tool_cannot_race_delayed_transition_into_two_notices(self):
        f=self.fixture;f.tool.acknowledge_request(delay=.05)
        self.submit('fast-tool')
        await asyncio.wait_for(f.started.wait(),.3)
        await f.until(lambda:any(e['type']=='agent.opening' for e in f.events))
        await asyncio.sleep(.15)
        self.assertEqual(len([e for e in f.events if e['type']=='agent.opening']),1)
        from voice_notices import ACKNOWLEDGEMENTS
        self.assertIn(next(e['text'] for e in f.events if e['type']=='agent.opening'),ACKNOWLEDGEMENTS)
        f.gate.set()
    async def test_native_reply_suppresses_delayed_ack(self):
        f=self.fixture;f.tool.acknowledge_request(delay=.05)
        f.tool.acknowledged_turns.add(f.tool.audio.turn)
        await asyncio.sleep(.08)
        self.assertFalse(any(e['type']=='opening.audio' for e in f.events))
    async def test_simple_question_waits_for_its_answer_without_canned_speech(self):
        f=self.fixture
        await f.remote.put(json.dumps({'type':'conversation.item.input_audio_transcription.completed',
                                      'item_id':'q2','text':'二加二等于多少？'}))
        await f.until(lambda:f.tool.audio.question=='q2')
        # Reproduce a reply whose captions precede audio beyond the old .7s timer.
        await f.remote.put(json.dumps({'type':'response.output_text.delta','response_id':'r2',
                                      'question_id':'q2','delta':'二加二等于四。'}))
        await f.until(lambda:any(e['type']=='response.output_text.delta' for e in f.events))
        await asyncio.sleep(.8)
        await f.remote.put(json.dumps({'type':'response.output_audio.started','response_id':'r2','question_id':'q2'}))
        await f.remote.put(json.dumps({'type':'response.output_audio.delta','delta':'four'}))
        await f.until(lambda:any(e.get('delta')=='four' for e in f.events))
        self.assertEqual(f.tool.quick_acks,{})
        self.assertFalse(f.started.is_set())
        self.assertFalse(any(e['type']=='opening.audio' for e in f.events))
    async def test_implicit_query_gets_transition_before_slow_tool_selection(self):
        f=self.fixture
        f.tool.run.__globals__['context']['history']=[{'role':'user','text':'刚才篝火里张三说了什么？'}]
        await f.remote.put(json.dumps({'type':'conversation.item.input_audio_transcription.completed',
                                      'item_id':'q2','text':'那他后来又说了什么？'}))
        await f.until(lambda:any(e['type']=='opening.audio' for e in f.events))
        self.assertFalse(f.started.is_set(),'A transition must not submit a query by itself')
        self.assertEqual(len([e for e in f.events if e['type']=='agent.opening']),1)
        self.submit('implicit-tool')
        await asyncio.wait_for(f.started.wait(),.3)
        await asyncio.sleep(.1)
        self.assertEqual(len([e for e in f.events if e['type']=='agent.opening']),1)
        f.gate.set()
    async def test_native_captions_cancel_pending_filler_and_preserve_audio(self):
        f=self.fixture;await f.tool.audio.user_started('q2')
        f.tool.acknowledge_request(delay=.03)
        await f.tool.audio.provider({'type':'response.output_text.delta','response_id':'r2',
                                     'question_id':'q2','delta':'我知道，答案是四。'})
        await asyncio.sleep(.06)
        await f.tool.audio.provider({'type':'response.output_audio.started','response_id':'r2','question_id':'q2'})
        await f.tool.audio.provider({'type':'response.output_audio.delta','delta':'answer'})
        self.assertFalse(any(e['type']=='opening.audio' for e in f.events))
        self.assertTrue(any(e.get('delta')=='answer' for e in f.events))
        self.assertNotIn('r2',f.tool.audio.blocked_responses)
    async def test_native_answer_preempts_filler_still_being_synthesized(self):
        f=self.fixture;await f.tool.audio.user_started('q2')
        entered=asyncio.Event();release=asyncio.Event()
        async def slow_speech(text,client,stop,valid,**kwargs):
            entered.set();await release.wait()
            if not stop.is_set() and valid():await client({'type':'opening.audio','delta':text})
        f.tool.run.__globals__['speak_opening']=slow_speech
        f.tool.acknowledge_request()
        await asyncio.wait_for(entered.wait(),.3)
        await f.tool.audio.provider({'type':'response.output_text.delta','response_id':'r2',
                                     'question_id':'q2','delta':'直接回答。'})
        release.set()
        await f.tool.audio.provider({'type':'response.output_audio.started','response_id':'r2','question_id':'q2'})
        await f.tool.audio.provider({'type':'response.output_audio.delta','delta':'answer'})
        await asyncio.sleep(.03)
        self.assertFalse(any(e['type']=='opening.audio' for e in f.events))
        self.assertTrue(any(e.get('delta')=='answer' for e in f.events))
    async def test_old_task_supplement_does_not_interrupt_new_topic(self):
        f=self.fixture;key=f.store.create('s','old question')
        f.store.update('s',key,state='completed',delivery='awaiting_choice',text='后来追加的结果应该留在记录里面。')
        f.tool.natural_notice_turns[key]=f.tool.audio.turn
        await f.tool.audio.user_started('new-topic')
        f.tool.supplement(key,0)
        await asyncio.sleep(.05)
        self.assertFalse(any(e['type']=='opening.audio' for e in f.events))
    async def test_structured_duplicate_returns_status_without_another_job(self):
        f=self.fixture
        intent={'kind':'lookup','subject':'Hard18','goal':'purpose','time_range':'','conditions':[]}
        self.submit('intent-one',operation='create',request_id='intent-1',intent=intent)
        await asyncio.wait_for(f.started.wait(),2)
        self.submit('intent-two',operation='create',request_id='intent-2',intent=intent,message='A completely different paraphrase')
        answer=await self.reply('intent-two')
        self.assertIn('"reused": true',answer);self.assertEqual(len(f.store.list('s')),1)
        f.gate.set()
    async def test_refresh_is_boolean_and_valid_id_is_required_before_submit(self):
        f=self.fixture;key=f.store.create('s','原查询');f.store.update('s',key,state='completed',delivery='deferred')
        self.submit('bad-bool',existing_task_id=key,refresh='yes')
        self.assertIn('参数无效',await self.reply('bad-bool'));self.assertFalse(f.started.is_set())
        self.submit('bad-refresh-id',existing_task_id='missing',refresh=True)
        self.assertIn('参数无效',await self.reply('bad-refresh-id'));self.assertFalse(f.started.is_set())
        self.submit('reuse-string-false',existing_task_id=key,refresh='False')
        self.assertIn('completed',await self.reply('reuse-string-false'));self.assertFalse(f.started.is_set())
        self.submit('refresh-one',existing_task_id=key,refresh='True')
        await asyncio.wait_for(f.started.wait(),2)
        self.assertEqual(len(f.store.list('s')),2)
        self.submit('refresh-two',existing_task_id=key,refresh=True)
        await f.until(lambda:len(f.store.list('s'))==3)
        f.gate.set()
    async def test_explicit_lookup_reuses_running_but_refreshes_completed(self):
        f=self.fixture
        first=asyncio.create_task(f.tool.explicit_lookup())
        await asyncio.wait_for(f.started.wait(),2)
        second=asyncio.create_task(f.tool.explicit_lookup())
        await asyncio.sleep(.05)
        self.assertEqual(len(f.store.list('s')),1)
        f.gate.set();await asyncio.gather(*f.tool.manager.jobs.values())
        await asyncio.gather(first,second)
        await f.tool.explicit_lookup()
        self.assertEqual(len(f.store.list('s')),2)
        self.assertEqual(f.upstream,[], 'Explicit route must not invent provider tool call IDs')

    async def test_new_operation_without_old_id_remains_new_and_exact_duplicate_reuses(self):
        f=self.fixture
        self.submit('new-op',existing_task_id='',message='启动北辰项目诊断并保存新日志')
        await asyncio.wait_for(f.started.wait(),2)
        self.submit('same-op',request_id='new-op',message='启动北辰项目诊断并保存新日志')
        await asyncio.sleep(.06);self.assertEqual(len(f.store.list('s')),1)
        self.submit('different-op',message='重启另一个项目并保存新日志')
        await f.until(lambda:len(f.store.list('s'))==2);f.gate.set()

if __name__=='__main__':unittest.main()
