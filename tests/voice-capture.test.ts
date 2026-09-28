import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { expect, test } from 'vitest';
const source = readFileSync('desktop/renderer/voice/services/capture-worklet.js', 'utf8');
test.each([16000, 44100, 48000])('capture at %i Hz emits exactly 50 PCM16 frames per second', sampleRate => {
  const frames: ArrayBuffer[] = [];
  let Capture: any;
  runInNewContext(source, { sampleRate, AudioWorkletProcessor: class {
    port = { onmessage: (_event: unknown) => {}, postMessage: (buffer: ArrayBuffer) => frames.push(buffer) };
  }, registerProcessor: (_name: string, processor: unknown) => { Capture = processor; } });
  const capture = new Capture();
  capture.port.onmessage({ data: true });
  for (let i = 0; i < sampleRate; i += 128) capture.process([[new Float32Array(Math.min(128, sampleRate - i)).fill(.5)]]);
  expect(frames).toHaveLength(50);
  expect(frames.every(frame => frame.byteLength === 640)).toBe(true);
  expect(new Int16Array(frames[0])[0]).toBe(16383);
  capture.port.onmessage({ data: false });
  capture.process([[new Float32Array(1024)]]);
  expect(frames).toHaveLength(50);
});
