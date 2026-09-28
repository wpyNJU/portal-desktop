// Verify the actual packaged main/preload/renderer using local services only.
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdir, mkdtemp } from 'node:fs/promises';
import path from 'node:path';
import { WebSocketServer } from 'ws';
import { launchDesktop } from './support/electron-lifecycle.mjs';
import { desktopExecutable, waitForChatReady } from './support/desktop.mjs';

await mkdir('test-results/voice', { recursive: true });
const directory = await mkdtemp(path.resolve('test-results/voice/package-'));
const metrics = { starts: 0, frames: 0, badFrames: 0, played: 0, closes: 0 };
const server = createServer((request, response) => {
  const route = new URL(request.url, 'http://localhost').pathname;
  const data = route.endsWith('/api/status') ? { being_name: 'being', description: '桌面通话测试' }
    : route.endsWith('/api/history') ? { messages: [] } : route.endsWith('/health') ? { status: 'ok' } : {};
  if (route.endsWith('/api/stream/active')) { response.writeHead(204); response.end(); return; }
  response.writeHead(200, { 'Content-Type': 'application/json' }); response.end(JSON.stringify(data));
});
const relay = new WebSocketServer({ noServer: true });
const voice = new WebSocketServer({ noServer: true });
server.on('upgrade', (request, socket, head) => {
  const selected = request.url.startsWith('/voice') ? voice : relay;
  selected.handleUpgrade(request, socket, head, client => selected.emit('connection', client));
});
relay.on('connection', socket => socket.on('message', bytes => {
  const data = JSON.parse(bytes.toString());
  if (data.loom_token) socket.send(JSON.stringify({ ok: true, being_id: 'fixture', relay_keepalive: 'text-v1' }));
  if (data.type === 'keepalive') socket.send('{"type":"keepalive_ack"}');
}));
voice.on('connection', socket => socket.on('message', bytes => {
  const data = JSON.parse(bytes.toString());
  if (data.type === 'profile.status') socket.send(JSON.stringify({ type: 'profile.status', updated_at: '2026-09-28T10:00:00Z' }));
  if (data.type === 'session.start') { metrics.starts++; socket.send('{"type":"session.created"}'); }
  if (data.type === 'session.close') { metrics.closes++; socket.close(); }
  if (data.type === 'task.segment.played') metrics.played++;
  if (data.type === 'input_audio_buffer.append') {
    metrics.frames++;
    if (Buffer.from(data.audio, 'base64').length !== 640) metrics.badFrames++;
    if (metrics.frames === 1) {
      socket.send(JSON.stringify({ type: 'response.output_text.delta', response_id: 'package-response', delta: '桌面通话已接通。' }));
      socket.send(JSON.stringify({ type: 'response.output_audio.delta', delta: Buffer.alloc(4800 * 4).toString('base64') }));
      socket.send(JSON.stringify({ type: 'task.segment.end', token: 'package-segment' }));
      socket.send('{"type":"response.done"}');
    }
  }
}));
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const port = server.address().port;
let application;
try {
  const env = { ...process.env, PORTAL_DESKTOP_USER_DATA: path.join(directory, 'profile') };
  delete env.ELECTRON_RUN_AS_NODE;
  application = await launchDesktop({ executablePath: await desktopExecutable(), args: ['--use-fake-device-for-media-stream'], env });
  const page = await application.firstWindow();
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  // Test-only routing inside this isolated process, without a product override.
  await application.evaluate(({ BrowserWindow }, voiceUrl) => {
    const NativeSocket = globalThis.WebSocket;
    globalThis.WebSocket = class extends NativeSocket {
      constructor(url, protocols) { super(String(url).endsWith('/pipeline/ws') ? voiceUrl : url, protocols); }
    };
    BrowserWindow.getAllWindows()[0].setSize(1100, 840);
  }, `ws://127.0.0.1:${port}/voice`);
  await page.getByRole('button', { name: '连接我的 Being' }).click();
  await page.locator('#connection-link').fill(`http://127.0.0.1:${port}/fixture/?token=package-fixture-only`);
  await page.locator('#portal-name-input').fill('voice-package-fixture');
  await page.locator('#workspace-input').fill(path.join(directory, 'workspace'));
  await page.locator('#background-input').uncheck();
  await page.getByRole('button', { name: '保存、连接并启动' }).click();
  await page.waitForFunction(() => !document.querySelector('#settings-dialog')?.open);
  await waitForChatReady(page);
  await page.locator('#start-voice-call').click();
  await page.getByText('桌面通话已接通。', { exact: true }).waitFor();
  await page.waitForTimeout(400);
  await page.screenshot({ path: 'test-results/voice/packaged-connected.png' });
  assert.ok(metrics.frames > 0); assert.equal(metrics.played, 1); assert.equal(metrics.badFrames, 0);
  const beforeHide = metrics.frames;
  await application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].hide());
  await page.waitForTimeout(900);
  assert.ok(metrics.frames > beforeHide + 10, 'Microphone capture should continue while the window is hidden');
  await application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].show());
  await page.getByRole('button', { name: '挂断通话', exact: true }).click();
  await page.waitForTimeout(150);
  const afterHangup = metrics.frames;
  await page.waitForTimeout(150);
  assert.equal(metrics.frames, afterHangup); assert.equal(metrics.closes, 1);
  assert.equal(await page.locator('#voice-call').isVisible(), false);
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: true, packaged: true, hiddenCapture: true, ...metrics }));
} finally {
  try { if (application) await application.close(); }
  finally {
    for (const socket of [...voice.clients, ...relay.clients]) socket.terminate();
    voice.close(); relay.close(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
  }
}
