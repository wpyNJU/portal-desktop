import { expect, test, vi } from 'vitest';
import { CallAudio } from '../desktop/renderer/voice/services/audio';

function setup() {
  const nodes: any[] = [];
  const context = { currentTime: 0, state: 'running', destination: {},
    createBuffer: (_channels: number, length: number, rate: number) => ({ duration: length / rate, getChannelData: () => new Float32Array(length) }),
    createBufferSource: () => {
      const node = { buffer: null, onended: null, connect: vi.fn(), disconnect: vi.fn(), start: vi.fn(), stop: vi.fn() };
      nodes.push(node); return node;
    } };
  const played = vi.fn(), acknowledge = vi.fn(), drained = vi.fn();
  const audio = new CallAudio(vi.fn(), played, acknowledge, drained, vi.fn());
  // Fake only the device clock and AudioNodes; exercise the real queue/receipt code.
  Object.assign(audio, { context });
  const frame = Buffer.from(new Float32Array(2400).buffer).toString('base64'); // 100 ms
  return { audio, context, nodes, played, acknowledge, drained, frame };
}

test('interrupt a long queue: stop every source and do not acknowledge a partial segment', () => {
  const { audio, context, nodes, acknowledge, frame } = setup();
  for (let i = 0; i < 200; i++) audio.play(frame);
  audio.segment('long-report'); context.currentTime = 5;
  audio.clear();
  expect(nodes.every(node => node.stop.mock.calls.length === 1)).toBe(true);
  expect(audio.busy).toBe(false); expect(acknowledge).not.toHaveBeenCalled();
});

test('tail interruption acknowledges a finished segment even before onended is dispatched', () => {
  const { audio, context, acknowledge, frame } = setup();
  audio.play(frame); audio.segment('last-clause');
  context.currentTime = 0.14;
  audio.clear(); audio.clear();
  expect(acknowledge.mock.calls).toEqual([['last-clause']]);
});

test('a queued later reply does not delay the earlier completed segment receipt', () => {
  const { audio, context, nodes, acknowledge, frame } = setup();
  audio.play(frame); audio.segment('first'); audio.play(frame);
  context.currentTime = 0.14; nodes[0].onended();
  expect(audio.busy).toBe(true); expect(acknowledge).toHaveBeenCalledWith('first');
});

test('late completion from an interrupted reply cannot report the new reply as drained', () => {
  const { audio, context, nodes, played, drained, frame } = setup();
  audio.play(frame); const late = nodes[0].onended;
  context.currentTime = 0.05; audio.clear(); audio.play(frame); played.mockClear();
  late();
  expect(audio.busy).toBe(true); expect(played).not.toHaveBeenCalled(); expect(drained).not.toHaveBeenCalled();
});
