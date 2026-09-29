import { readFileSync } from 'node:fs';
import { createContext, runInContext, runInNewContext } from 'node:vm';
import { expect, test, vi } from 'vitest';

const source = readFileSync('services/voice-gateway/mobile/call.js', 'utf8');
const worklet = source.match(/const worklet=`([\s\S]*?)`;/)![1];

test.each([16000, 44100, 48000])('mobile %i Hz capture preserves real-time 16 kHz frame duration', sampleRate => {
  const frames: ArrayBuffer[] = []; let Capture: any;
  runInNewContext(worklet, { sampleRate, AudioWorkletProcessor: class {
    port = { onmessage: (_event: unknown) => {}, postMessage: (buffer: ArrayBuffer) => frames.push(buffer) };
  }, registerProcessor: (_name: string, value: unknown) => { Capture = value; } });
  const capture = new Capture(); capture.port.onmessage({ data: true });
  for (let i = 0; i < sampleRate; i += 128) capture.process([[new Float32Array(Math.min(128, sampleRate - i)).fill(.1)]]);
  expect(frames).toHaveLength(50); expect(frames.every(frame => frame.byteLength === 640)).toBe(true);
  capture.port.onmessage({ data: false }); capture.process([[new Float32Array(1024)]]);
  expect(frames).toHaveLength(50);
});

function setup() {
  const sent: any[] = [], nodes: any[] = [];
  const context = { currentTime: 0, destination: {},
    createBuffer: (_channels: number, length: number, rate: number) => ({ duration: length / rate, getChannelData: () => new Float32Array(length) }),
    createBufferSource: () => {
      const node = { buffer: null, onended: null, connect: vi.fn(), disconnect: vi.fn(), start: vi.fn(), stop: vi.fn() };
      nodes.push(node); return node;
    } };
  const element = { textContent: '', classList: { add: vi.fn(), remove: vi.fn() } };
  const sandbox = createContext({ ctx: context, nodes: new Set(), nextTime: 0, active: true, muted: false,
    firstAudio: true, firstAt: 0, responseId: '', questionId: '', endAfterPlayback: false,
    ws: { readyState: 1, send: (raw: string) => sent.push(JSON.parse(raw)) },
    atob: (text: string) => Buffer.from(text, 'base64').toString('binary'),
    document: { body: element }, $: () => element, status: vi.fn(), rememberEvent: vi.fn() });
  // Run the shipped playback/event handlers, replacing only browser devices/DOM.
  runInContext(source.slice(source.indexOf('function send(data)'), source.indexOf('async function startCall')), sandbox);
  const event = (data: object) => { sandbox.data = data; runInContext('event(data)', sandbox); };
  const pcm = Buffer.from(new Float32Array(2400).buffer).toString('base64');
  return { context, nodes, sent, event, pcm };
}

test('mobile middle interruption stops a long queue, rejects old frames, and tags receipts', () => {
  const { event, nodes, context, sent, pcm } = setup();
  event({ type: 'playback.clear', generation: 1 });
  for (let i = 0; i < 100; i++) event({ type: 'agent.audio.delta', generation: 1, delta: pcm });
  event({ type: 'task.segment.end', generation: 1, token: 'partial' });
  context.currentTime = 2; event({ type: 'playback.clear', generation: 2 });
  event({ type: 'agent.audio.delta', generation: 1, delta: pcm });
  expect(nodes).toHaveLength(100); expect(nodes.every(node => node.stop.mock.calls.length === 1)).toBe(true);
  expect(sent.filter(event => event.type === 'task.segment.played')).toEqual([]);
  event({ type: 'response.output_audio.delta', generation: 2, delta: pcm });
  expect(sent.at(-1)).toEqual({ type: 'client.playback', playing: true, generation: 2 });
});

test('mobile tail interruption preserves completion even if the end callback was delayed', () => {
  const { event, context, sent, pcm } = setup();
  event({ type: 'agent.audio.delta', generation: 1, delta: pcm });
  event({ type: 'task.segment.end', generation: 1, token: 'fully-heard' });
  context.currentTime = .17; event({ type: 'playback.clear', generation: 2 });
  expect(sent.filter(event => event.type === 'task.segment.played')).toEqual([{ type: 'task.segment.played', token: 'fully-heard' }]);
});

test('mobile old onended callback cannot drain a newer short reply', () => {
  const { event, nodes, context, sent, pcm } = setup();
  event({ type: 'agent.audio.delta', generation: 1, delta: pcm }); const late = nodes[0].onended;
  context.currentTime = .08; event({ type: 'playback.clear', generation: 2 });
  event({ type: 'response.output_audio.delta', generation: 2, delta: pcm });
  sent.length = 0; late(); expect(sent).toEqual([]);
});
