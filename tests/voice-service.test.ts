import { afterEach, expect, test, vi } from 'vitest';
import { VoiceService, VOICE_GATEWAY } from '../desktop/main/voice/service';
import { allowVoicePermission } from '../desktop/main/voice/permissions';
import { parseConnection } from '../desktop/main/chat/connection';
import { VOICE_OPTIONS, type VoiceEvent, type VoiceStart } from '../desktop/shared/voice';

function setup() {
  vi.useFakeTimers();
  let connection = parseConnection('https://example.com/being/?token=private-fixture-token');
  let scene = 'desktop-fixture';
  const socket = { readyState: 1, bufferedAmount: 0, send: vi.fn(), close: vi.fn(),
    onopen: null as null | (() => void), onmessage: null as null | ((event: { data: string }) => void),
    onerror: null as null | (() => void), onclose: null as null | (() => void) };
  const events: VoiceEvent[] = [];
  const connect = vi.fn(() => socket as unknown as WebSocket);
  const service = new VoiceService(() => connection, () => scene, event => events.push(event), connect);
  const input: VoiceStart = { callId: 'call-fixture-001', endpoint: connection.endpoint, sceneId: scene, voice: VOICE_OPTIONS[0].id, history: [] };
  return { service, socket, events, connect, input, change: () => { scene = 'other-scene'; },
    reauth: () => { connection = parseConnection('https://example.com/being/?token=different-token'); } };
}
afterEach(() => vi.useRealTimers());
test('profile status uses the saved identity and only returns a timestamp; duplicate refresh is shared', async () => {
  const { service, socket, connect, input } = setup();
  const first = service.profile(input.endpoint, true), second = service.profile(input.endpoint, true);
  expect(first).toBe(second); expect(connect).toHaveBeenCalledOnce(); expect(service.active).toBe(false);
  socket.onopen!(); expect(JSON.parse(socket.send.mock.calls[0][0]).type).toBe('profile.refresh');
  socket.onmessage!({ data: JSON.stringify({ type: 'profile.updated', updated_at: '2026-09-28T10:00:00Z', token: 'never-forward' }) });
  await expect(first).resolves.toEqual({ updatedAt: '2026-09-28T10:00:00Z' }); expect(socket.close).toHaveBeenCalledOnce();
});
test('missing profile is distinct from errors and status never triggers refresh', async () => {
  const { service, socket, input } = setup(); const pending = service.profile(input.endpoint);
  socket.onopen!(); expect(JSON.parse(socket.send.mock.calls[0][0]).type).toBe('profile.status');
  socket.onmessage!({ data: '{"type":"profile.status","updated_at":null}' }); await expect(pending).resolves.toEqual({ updatedAt: null });
});
test('profile requests reject other endpoints and ignore responses after credential changes', async () => {
  const { service, socket, input, reauth, connect } = setup();
  await expect(service.profile('https://other.test')).rejects.toThrow(); expect(connect).not.toHaveBeenCalled();
  const pending = service.profile(input.endpoint); reauth(); socket.onmessage!({ data: '{"type":"profile.status","updated_at":null}' });
  await expect(pending).rejects.toThrow('Being 已切换');
});
test('profile timeout releases its socket and can be retried', async () => {
  const { service, socket, input, connect } = setup(); const pending = service.profile(input.endpoint);
  const result = expect(pending).rejects.toThrow('超时'); vi.advanceTimersByTime(10001); await result;
  expect(socket.close).toHaveBeenCalledOnce(); const again = service.profile(input.endpoint);
  expect(connect).toHaveBeenCalledTimes(2); socket.onmessage!({ data: '{"type":"profile.status","updated_at":null}' }); await again;
});
test('voice uses the fixed gateway and main-process Being credentials', () => {
  const { service, socket, events, connect, input } = setup(); service.start(input); socket.onopen!();
  expect(connect).toHaveBeenCalledWith(VOICE_GATEWAY);
  expect(JSON.parse(socket.send.mock.calls[0][0])).toMatchObject({ type: 'session.start', agent_url: 'https://example.com/being/?token=private-fixture-token' });
  socket.onmessage!({ data: JSON.stringify({ type: 'session.created', secret: 'not-for-renderer' }) });
  expect(events).toEqual([{ callId: input.callId, data: { type: 'session.created' } }]); service.stop();
});
test('duplicate calls and changed scopes cannot create another upstream request', () => {
  const { service, connect, input } = setup();
  expect(() => service.start({ ...input, endpoint: 'https://attacker.test' })).toThrow();
  expect(() => service.start({ ...input, sceneId: 'other-scene' })).toThrow();
  service.start(input); expect(() => service.start(input)).toThrow(); expect(connect).toHaveBeenCalledTimes(1); service.stop();
});
test('old call cancellation does not close a new call', () => {
  const { service, socket, input } = setup(); service.start(input); service.stop('old-call-id');
  expect(service.active).toBe(true); expect(socket.close).not.toHaveBeenCalled(); service.stop();
  expect(JSON.parse(socket.send.mock.calls.at(-1)![0])).toEqual({ type: 'session.close' });
});
test('connection timeout closes the socket and reports an actionable error', () => {
  const { service, socket, input, events } = setup(); service.start(input); vi.advanceTimersByTime(30001);
  expect(service.active).toBe(false); expect(socket.close).toHaveBeenCalledOnce();
  expect(events.map(event => event.data.type)).toEqual(['error', 'call.closed']);
});
test.each(['scene', 'identity'])('stale %s callbacks cannot leak results into a different call', mode => {
  const { service, socket, input, events, change, reauth } = setup(); service.start(input);
  (mode === 'scene' ? change : reauth)(); socket.onmessage!({ data: '{"type":"response.output_text.delta","delta":"old result"}' });
  expect(events.some(event => event.data.delta === 'old result')).toBe(false); expect(service.active).toBe(false);
});
test('audio commands are bounded and cannot replace credentials or send arbitrary provider events', () => {
  const { service, socket, input } = setup(); service.start(input);
  service.send(input.callId, { type: 'input_audio_buffer.append', audio: Buffer.alloc(640).toString('base64') });
  expect(socket.send).toHaveBeenCalledOnce();
  expect(() => service.send(input.callId, { type: 'input_audio_buffer.append', audio: 'bad' })).toThrow();
  expect(() => service.send(input.callId, { type: 'session.start' } as never)).toThrow();
  socket.bufferedAmount = 300000;
  service.send(input.callId, { type: 'client.playback', playing: true }); expect(service.active).toBe(false);
});
test('stop clears the startup deadline and drops late socket callbacks', () => {
  const { service, socket, input, events } = setup(); service.start(input); const late = socket.onmessage!;
  service.stop(); late({ data: '{"type":"session.created"}' }); vi.advanceTimersByTime(60000);
  expect(events.map(event => event.data.type)).toEqual(['call.closed']);
});
test('only the trusted main frame may request microphone access', () => {
  const url = 'beings://desktop/';
  expect(allowVoicePermission(true, 'media', true, url, url, ['audio'])).toBe(true);
  for (const media of [[], ['video'], ['audio', 'video'], ['unknown']]) expect(allowVoicePermission(true, 'media', true, url, url, media)).toBe(false);
  expect(allowVoicePermission(false, 'media', true, url, url, ['audio'])).toBe(false);
  expect(allowVoicePermission(true, 'media', false, url, url, ['audio'])).toBe(false);
  expect(allowVoicePermission(true, 'media', true, 'beings://chat/', url, ['audio'])).toBe(false);
});
