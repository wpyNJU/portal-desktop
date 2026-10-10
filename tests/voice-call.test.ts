import { afterEach, expect, test, vi } from 'vitest';
import { VoiceCall } from '../desktop/renderer/voice/models/call';
import type { CallAudio } from '../desktop/renderer/voice/services/audio';
import { VoiceHistoryStore, contextWindow } from '../desktop/renderer/voice/services/history';
import type { VoiceAPI, VoiceEvent } from '../desktop/shared/voice';

function setup() {
  vi.useFakeTimers();
  vi.spyOn(VoiceHistoryStore.prototype, 'read').mockResolvedValue([]);
  vi.spyOn(VoiceHistoryStore.prototype, 'write').mockResolvedValue();
  const listeners = new Set<(event: VoiceEvent) => void>();
  const api: VoiceAPI = { profile: vi.fn().mockResolvedValue({ updatedAt: '2026-09-17T10:56:24Z' }), start: vi.fn().mockResolvedValue(undefined), send: vi.fn().mockResolvedValue(undefined), stop: vi.fn().mockResolvedValue(undefined),
    onEvent: callback => { listeners.add(callback); return () => { listeners.delete(callback); }; } };
  const audio = { open: vi.fn().mockResolvedValue(undefined), close: vi.fn(), clear: vi.fn(), pause: vi.fn(), resume: vi.fn(), play: vi.fn(), segment: vi.fn(), setEnabled: vi.fn(), setNearEndProtection: vi.fn(), busy: false };
  let drained = () => {};
  let diagnostic: (...args: any[]) => void = () => {};
  const model = new VoiceCall(api, (...args) => { drained = args[3]; diagnostic = args[5]!; return audio as unknown as CallAudio; });
  const start = () => model.start('https://example.com/being', 'desktop-fixture', 'being');
  const event = (type: string, data: Record<string, unknown> = {}) => {
    const input = vi.mocked(api.start).mock.calls.at(-1)![0];
    for (const listener of listeners) listener({ callId: input.callId, data: { type, ...data } });
  };
  return { model, audio, api, start, event, listeners, diagnostic: (event: any) => diagnostic(event), drained: () => drained() };
}
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });
test('call is active only after microphone and service are ready, repeated clicks do not redial', async () => {
  const { model, start, event, api, audio } = setup(); await start(); expect(model.phase).toBe('connecting');
  event('session.created'); expect(model.phase).toBe('active'); expect(audio.setEnabled).toHaveBeenCalledWith(true);
  await start(); expect(api.start).toHaveBeenCalledOnce(); vi.advanceTimersByTime(2100); expect(model.elapsed).toBe(2); model.dispose();
});
test('cancel during microphone permission wait releases a late stream', async () => {
  const { model, audio, start, api } = setup(); let resolve!: () => void;
  audio.open.mockImplementation(() => new Promise<void>(done => { resolve = done; }));
  const pending = start(); await vi.waitFor(() => expect(audio.open).toHaveBeenCalled());
  model.dismiss(); resolve(); await pending;
  expect(model.open).toBe(false); expect(model.phase).toBe('ended'); expect(audio.close).toHaveBeenCalled(); expect(api.stop).toHaveBeenCalled();
});
test('muting controls capture and confirmed input pauses until semantic decision', async () => {
  const { model, start, event, api, audio } = setup(); await start(); event('session.created');
  model.toggleMute(); expect(audio.setEnabled).toHaveBeenLastCalledWith(false);
  expect(vi.mocked(api.send).mock.calls.at(-1)![1]).toEqual({ type: 'input_audio_mute.commit' });
  const clears = audio.clear.mock.calls.length;
  event('speech.input.pending'); expect(audio.pause).toHaveBeenCalledWith(true);
  event('conversation.item.input_audio_transcription.started'); expect(audio.clear).toHaveBeenCalledTimes(clears);
  event('speech.input.resume'); expect(audio.resume).toHaveBeenCalled();
  event('playback.clear'); expect(audio.clear).toHaveBeenCalledTimes(clears + 1);
  model.dismiss(); expect(api.stop).toHaveBeenCalled(); expect(api.send).not.toHaveBeenCalledWith(expect.anything(), { type: 'response.cancel' });
});
test('transport errors stay visible and retry creates a fresh call', async () => {
  const { model, start, event, api } = setup(); await start(); event('error', { message: '网络已断开' });
  expect(model.phase).toBe('error'); expect(model.status).toBe('网络已断开'); expect(model.open).toBe(true);
  await start(); expect(api.start).toHaveBeenCalledTimes(2); model.dispose();
});

test('late audio and segment markers from an old generation cannot enter a newer reply', async () => {
  const { model, start, event, audio } = setup(); await start(); event('session.created');
  event('playback.clear', { generation: 3 });
  event('response.output_audio.delta', { generation: 3, delta: 'new' });
  event('agent.audio.delta', { generation: 2, delta: 'old' });
  event('task.segment.end', { generation: 2, token: 'old-token' });
  event('playback.clear', { generation: 2 });
  expect(audio.play.mock.calls).toEqual([['new']]); expect(audio.segment).not.toHaveBeenCalled();
  model.dispose();
});

test('generation completion does not claim audible playback has finished', async () => {
  const { model, start, event, audio, drained } = setup(); await start(); event('session.created');
  audio.busy = true; model.status = '正在回应你'; event('response.done');
  expect(model.status).toBe('正在回应你');
  audio.busy = false; drained(); expect(model.status).toBe('我在听，你可以继续说'); model.dispose();
});
test('agent replay offsets do not duplicate the stored answer', async () => {
  const { model, start, event } = setup(); await start(); event('session.created');
  event('agent.text.delta', { call_id: 'task', offset: 0, delta: '查询完成' });
  event('agent.text.delta', { call_id: 'task', offset: 0, delta: '查询完成' });
  event('agent.text.delta', { call_id: 'task', offset: 4, delta: '，有两个任务。' });
  expect(model.answer).toBe('查询完成，有两个任务。'); model.dispose();
});
test('farewell waits for audio to finish while idle close ends immediately', async () => {
  const { model, start, event, audio } = setup(); await start(); event('session.created'); audio.busy = true;
  event('call.end'); expect(model.phase).toBe('active'); model.dispose();
  const next = setup(); await next.start(); next.event('session.created'); next.event('call.end', { reason: 'idle_timeout' });
  expect(next.model.phase).toBe('ended'); expect(next.model.status).toContain('30 秒'); next.model.dispose();
});
test('context window is bounded without deleting stored history', () => {
  const rows = [{ role: 'user' as const, text: '旧的问题' }, { role: 'assistant' as const, text: '最新的回答' }];
  expect(contextWindow(rows, 5)).toEqual([{ role: 'assistant', text: '最新的回答' }]); expect(rows).toHaveLength(2);
});

test('first use waits for explicit alignment or skip before connecting or recording', async () => {
  const { model, api, start, audio } = setup(); vi.mocked(api.profile).mockResolvedValue({ updatedAt: null });
  await start(); expect(model.phase).toBe('setup'); expect(api.start).not.toHaveBeenCalled(); expect(audio.open).not.toHaveBeenCalled();
  expect(api.profile).toHaveBeenCalledWith('https://example.com/being');
  await model.skipProfile(); expect(api.start).toHaveBeenCalledOnce(); expect(api.profile).toHaveBeenCalledOnce(); model.dispose();
});

test('successful first alignment saves the timestamp and starts one call', async () => {
  const { model, api, start } = setup(); vi.mocked(api.profile).mockResolvedValueOnce({ updatedAt: null });
  await start(); await model.updateProfile();
  expect(api.profile).toHaveBeenLastCalledWith('https://example.com/being', true);
  expect(model.profileUpdatedAt).toBe('2026-09-17T10:56:24Z'); expect(api.start).toHaveBeenCalledOnce(); model.dispose();
});

test('refresh during a call keeps the call and explains next-call activation; failure preserves cache', async () => {
  const { model, api, start, event } = setup(); await start(); event('session.created'); await model.updateProfile();
  expect(model.profileMessage).toContain('下次通话'); expect(api.start).toHaveBeenCalledOnce(); expect(api.stop).not.toHaveBeenCalled();
  vi.mocked(api.profile).mockRejectedValueOnce(new Error('offline')); await model.updateProfile();
  expect(model.profileUpdatedAt).toBe('2026-09-17T10:56:24Z'); expect(model.phase).toBe('active'); model.dispose();
});

test('late first alignment cannot open the microphone after the dialog is dismissed', async () => {
  const { model, api, start, audio } = setup(); vi.mocked(api.profile).mockResolvedValueOnce({ updatedAt: null }); await start();
  let finish!: (value: { updatedAt: string }) => void;
  vi.mocked(api.profile).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  const pending = model.updateProfile(); model.dismiss(); finish({ updatedAt: '2026-09-28T10:00:00Z' }); await pending;
  expect(model.open).toBe(false); expect(audio.open).not.toHaveBeenCalled(); expect(api.start).not.toHaveBeenCalled(); model.dispose();
});

test('unavailable profile status allows retry without pretending the cache is empty', async () => {
  const { model, api, start } = setup(); vi.mocked(api.profile).mockRejectedValueOnce(new Error('offline'));
  await start(); expect(model.phase).toBe('setup'); expect(model.profileState).toBe('error'); expect(api.start).not.toHaveBeenCalled();
  await model.readProfile(); expect(api.start).toHaveBeenCalledOnce(); model.dispose();
});

test('a backchannel resumes buffered audio and preserves the current answer', async () => {
  const { model, start, event, audio } = setup(); await start(); event('session.created');
  event('response.output_text.delta', { response_id: 'r1', delta: '目前有两个待确认事项。' });
  event('speech.input.pending'); event('conversation.item.input_audio_transcription.started');
  event('speech.input.resume'); event('conversation.item.input_audio_transcription.completed', { item_id: 'q2', text: '嗯' });
  expect(model.answer).toBe('目前有两个待确认事项。'); expect(audio.resume).toHaveBeenCalledOnce();
  event('speech.input.pending'); event('playback.clear');
  event('conversation.item.input_audio_transcription.completed', { item_id: 'q3', text: '换一个问题' });
  expect(model.answer).toBe(''); expect(model.status).toBe('正在回应你'); model.dispose();
});

test('native background tasks have independent stages and reject late terminal progress', async () => {
 const t=setup();await t.start();t.event('session.created');
 t.event('task.updated',{task:{id:'one',title:'查询一',state:'running',stage:'thinking',summary:'正在核对',stage_updated:10}});
 t.event('task.updated',{task:{id:'two',title:'查询二',state:'running',stage:'queued',stage_updated:10}});
 t.event('agent.progress',{call_id:'one',stage:'tool',text:'已找到两条信息',elapsed:20});
 t.event('agent.progress',{call_id:'two',stage:'thinking',text:'正在分析第二项',elapsed:5});
 expect(t.model.tasks.get('one')?.progress).toBe('已找到两条信息');expect(t.model.tasks.get('two')?.stage).toBe('thinking');
 t.event('task.updated',{task:{id:'one',state:'completed',delivery:'awaiting_choice',cursor:0,length:80,stage:'completed',summary:'已核对两项',stage_updated:30}});
 t.event('agent.progress',{call_id:'one',stage:'thinking',text:'仍在查询',elapsed:1});
 t.event('task.updated',{task:{id:'one',state:'running',stage:'tool',stage_updated:25}});
 expect(t.model.tasks.get('one')?.state).toBe('completed');expect(t.model.taskLabel(t.model.tasks.get('one')!)).toBe('已完成');
 expect(t.model.tasks.get('two')?.progress).toBe('正在分析第二项');t.model.dispose();
});
test('background progress and completion do not overwrite foreground text or control audio', async () => {
 const t=setup();await t.start();t.event('session.created');
 t.event('conversation.item.input_audio_transcription.completed',{item_id:'now',text:'先回答我当前这个问题'});
 t.event('response.output_text.delta',{response_id:'current',delta:'当前回答'});
 const clears=t.audio.clear.mock.calls.length,status=t.model.status;
 t.event('agent.progress',{generation:999,call_id:'old',stage:'thinking',text:'旧任务正在分析',elapsed:3});
 t.event('agent.opening',{text:'我去查一下'});t.event('agent.status',{text:'旧结果已保存'});
 t.event('agent.text.delta',{call_id:'old',offset:0,delta:'旧任务完整结果'});
 t.event('task.updated',{task:{id:'old',title:'旧查询',state:'completed',summary:'已完成',stage_updated:20}});
 expect(t.model.heard).toBe('先回答我当前这个问题');expect(t.model.answer).toBe('当前回答');expect(t.model.status).toBe(status);
 expect(t.model.tasks.get('old')?.body).toBe('旧任务完整结果');expect(t.audio.clear).toHaveBeenCalledTimes(clears);expect(t.audio.play).not.toHaveBeenCalled();
 t.model.dispose();
});
test('restored completion stays quiet, task errors remain distinct, and reconnect resets state', async () => {
 const t=setup();await t.start();t.event('session.created');const status=t.model.status;
 t.event('task.updated',{task:{id:'restored',title:'之前的任务',state:'completed',summary:'保存的结果',stage_updated:100}});
 t.event('task.updated',{task:{id:'failed',title:'失败任务',state:'failed',summary:'网络失败',stage_updated:200}});
 expect(t.model.taskLabel(t.model.tasks.get('restored')!)).toBe('已完成');expect(t.model.taskLabel(t.model.tasks.get('failed')!)).toBe('查询失败');
 expect(t.model.status).toBe(status);expect(t.model.phase).toBe('active');expect(t.audio.play).not.toHaveBeenCalled();
 t.model.end();expect(t.model.tasks.size).toBe(0);expect(t.model.taskStatus).toBe('');await t.start();t.event('session.created');
 expect(t.model.taskRows).toEqual([]);expect(t.model.taskStatus).toBe('');t.model.dispose();
});

test('native task panel prioritizes older running work over restored completed history', async () => {
 const t=setup();await t.start();t.event('session.created');
 t.event('task.updated',{task:{id:'still-running',title:'还在执行的旧任务',state:'running',stage:'tool'}});
 for(let i=0;i<10;i++)t.event('task.updated',{task:{id:`history-${i}`,title:`历史任务${i}`,state:'completed'}});
 expect(t.model.taskRows).toHaveLength(1);expect(t.model.taskRows[0].id).toBe('still-running');
 expect(t.model.historyTaskRows).toHaveLength(10);expect(t.model.historyTaskRows[0].id).toBe('history-9');
 t.model.dispose();
});

test('interrupted tasks remain terminal and reject late running stage updates', async () => {
 const t=setup();await t.start();t.event('session.created');
 t.event('task.updated',{task:{id:'interrupted',title:'中断任务',state:'interrupted',stage:'interrupted',stage_updated:30}});
 t.event('agent.progress',{call_id:'interrupted',stage:'tool',text:'迟到阶段',elapsed:10,stage_updated:10});
 t.event('task.updated',{task:{id:'interrupted',state:'running',stage:'thinking',stage_updated:20}});
 const row=t.model.tasks.get('interrupted')!;expect(row.state).toBe('interrupted');expect(t.model.taskLabel(row)).toBe('查询中断');
 t.model.dispose();
});

test('renderer audio diagnostics carry the active generation',async()=>{
 const t=setup();await t.start();t.event('session.created');t.event('playback.clear',{generation:8});
 t.diagnostic({type:'client.diagnostic',reason:'voice_audio_state',state:'scheduled',context_state:'running',paused_ms:0,queued_ms:200,frames:1});
 expect(vi.mocked(t.api.send).mock.calls.at(-1)![1]).toEqual({type:'client.diagnostic',reason:'voice_audio_state',state:'scheduled',context_state:'running',paused_ms:0,queued_ms:200,frames:1,generation:8});t.model.dispose();
});

test('compact task panel shows three live items without deleting folded history',async()=>{
 const t=setup();await t.start();t.event('session.created');
 for(let i=0;i<5;i++)t.event('task.updated',{task:{id:`live-${i}`,state:'running',title:`执行${i}`}});
 for(let i=0;i<45;i++)t.event('task.updated',{task:{id:`past-${i}`,state:i===0?'interrupted':'completed',summary:'保留的原文摘录'}});
 expect(t.model.taskRows.map(row=>row.id)).toEqual(['live-4','live-3','live-2']);expect(t.model.historyTaskRows).toHaveLength(45);
 expect(t.model.tasks.has('live-0')).toBe(true);expect(t.model.tasks.has('past-0')).toBe(true);t.model.dispose();
});

test('ending a native call closes the side panel and leaves error recovery available',async()=>{
 const t=setup();await t.start();t.event('session.created');expect(t.model.open).toBe(true);
 t.model.end();expect(t.model.open).toBe(false);await t.start();t.event('error',{message:'连接错误'});
 expect(t.model.open).toBe(true);expect(t.model.phase).toBe('error');t.model.dispose();
});
