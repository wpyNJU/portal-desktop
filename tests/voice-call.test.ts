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
  const audio = { open: vi.fn().mockResolvedValue(undefined), close: vi.fn(), clear: vi.fn(), play: vi.fn(), segment: vi.fn(), setEnabled: vi.fn(), busy: false };
  let drained = () => {};
  const model = new VoiceCall(api, (...args) => { drained = args[3]; return audio as unknown as CallAudio; });
  const start = () => model.start('https://example.com/being', 'desktop-fixture', 'being');
  const event = (type: string, data: Record<string, unknown> = {}) => {
    const input = vi.mocked(api.start).mock.calls.at(-1)![0];
    for (const listener of listeners) listener({ callId: input.callId, data: { type, ...data } });
  };
  return { model, audio, api, start, event, listeners, drained: () => drained() };
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
test('muting controls the capture device and protocol, user speech clears all playback', async () => {
  const { model, start, event, api, audio } = setup(); await start(); event('session.created');
  model.toggleMute(); expect(audio.setEnabled).toHaveBeenLastCalledWith(false);
  expect(vi.mocked(api.send).mock.calls.at(-1)![1]).toEqual({ type: 'input_audio_mute.commit' });
  event('conversation.item.input_audio_transcription.started'); expect(audio.clear).toHaveBeenCalled();
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
