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
let liveVoiceSocket;
const metrics = { starts: 0, frames: 0, badFrames: 0, played: 0, closes: 0, diagnosticStates: [] };
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
  if (data.type === 'session.start') { liveVoiceSocket = socket; metrics.starts++; socket.send('{"type":"session.created"}'); }
  if (data.type === 'session.close') { metrics.closes++; socket.close(); }
  if (data.type === 'task.segment.played') metrics.played++;
  if (data.type === 'client.diagnostic') {
    assert.equal(data.reason, 'voice_audio_state');
    assert.ok(!('audio' in data) && !('text' in data) && !('transcript' in data));
    metrics.diagnosticStates.push(data.state);
  }
  if (data.type === 'input_audio_buffer.append') {
    metrics.frames++;
    if (Buffer.from(data.audio, 'base64').length !== 640) metrics.badFrames++;
    if (metrics.frames === 1) {
      socket.send(JSON.stringify({ type: 'response.output_text.delta', response_id: 'package-response', delta: '桌面通话已接通。' }));
      socket.send(JSON.stringify({ type: 'response.output_audio.delta', delta: Buffer.alloc(4800 * 4).toString('base64') }));
      socket.send(JSON.stringify({ type: 'task.segment.end', token: 'package-segment' }));
      socket.send('{"type":"response.done"}');
      for (const event of [
        { type: 'task.updated', task: { id: 'package-task-one', title: '核对项目部署', state: 'running', stage: 'thinking', stage_updated: 10, summary: '', cursor: 0, length: 0 } },
        { type: 'task.updated', task: { id: 'package-task-two', title: '比较语音模型', state: 'running', stage: 'queued', stage_updated: 10, summary: '', cursor: 0, length: 0 } },
        { type: 'agent.progress', call_id: 'package-task-one', stage: 'tool', text: '已核对第一项', elapsed: 4 },
        { type: 'agent.progress', call_id: 'package-task-two', stage: 'thinking', text: '正在比较模型', elapsed: 2 },
      ]) socket.send(JSON.stringify(event));
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
  const chatInput = page.frameLocator('#chat-frame').locator('#input');
  const fullChatWidth = (await page.locator('#chat-frame').boundingBox()).width;
  await chatInput.fill('保留在左侧的通话草稿');
  await page.locator('#start-voice-call').click();
  try { await page.getByText('桌面通话已接通。', { exact: true }).waitFor(); }
  catch (error) {
    console.error(JSON.stringify({ metrics, errors, panel: await page.locator('#voice-call').innerText() }));
    throw error;
  }
  const panel = await page.locator('#voice-call').boundingBox();
  const splitChat = await page.locator('#chat-frame').boundingBox();
  assert.ok(splitChat.x + splitChat.width <= panel.x + 1, 'Call panel must sit beside chat, not overlap it');
  assert.ok(splitChat.width < fullChatWidth - 200, 'Chat yields space to a separate native call panel');
  assert.equal(await page.locator('dialog#voice-call').count(), 0);
  assert.equal(await chatInput.inputValue(), '保留在左侧的通话草稿');
  await chatInput.click(); await chatInput.fill('通话中仍可编辑左侧草稿');
  assert.ok(await chatInput.evaluate(element => element === element.ownerDocument.activeElement));
  await chatInput.press('Escape');
  assert.equal(await page.locator('#voice-call').isVisible(), true, 'Escape in chat cannot accidentally hang up');
  assert.equal(await chatInput.inputValue(), '通话中仍可编辑左侧草稿');
  const tasks = page.getByRole('region', { name: '后台任务', exact: true });
  await tasks.getByText('核对项目部署', { exact: true }).waitFor();
  await tasks.getByText('比较语音模型', { exact: true }).waitFor();
  await tasks.getByText('已核对第一项', { exact: true }).waitFor();
  await tasks.getByText('正在比较模型', { exact: true }).waitFor();
  assert.equal(await page.locator('.voice-call-captions').textContent(), '桌面通话已接通。', 'Task progress must preserve the current answer');
  await page.waitForTimeout(400);
  await page.screenshot({ path: 'test-results/voice/packaged-connected.png' });
  await application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setSize(760, 840));
  await page.waitForTimeout(100);
  const narrowChat = await page.locator('#chat-frame').boundingBox();
  const narrowPanel = await page.locator('#voice-call').boundingBox();
  assert.ok(narrowChat.width >= 200 && narrowPanel.width >= 240, 'Both native panes remain usable at a narrow window width');
  assert.ok(narrowChat.x + narrowChat.width <= narrowPanel.x + 1);
  await chatInput.click(); assert.ok(await chatInput.evaluate(element => element === element.ownerDocument.activeElement));
  await page.screenshot({ path: 'test-results/voice/packaged-split-narrow.png' });
  await application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setSize(1100, 840));
  await page.waitForTimeout(100);
  assert.ok(metrics.frames > 0); assert.equal(metrics.played, 1); assert.equal(metrics.badFrames, 0);
  assert.ok(metrics.diagnosticStates.includes('scheduled'));
  assert.ok(metrics.diagnosticStates.includes('drained'));
  for (const event of [
    { type: 'task.updated', task: { id: 'package-task-one', title: '核对项目部署', state: 'completed', delivery: 'awaiting_choice', stage: 'completed', stage_updated: 20, summary: '原文摘录：部署配置已经核对。', cursor: 0, length: 13 } },
    { type: 'agent.progress', call_id: 'package-task-one', stage: 'thinking', text: '迟到的旧阶段', elapsed: 1 },
  ]) liveVoiceSocket.send(JSON.stringify(event));
  await page.waitForFunction(() => ![...document.querySelectorAll('.voice-call-tasks strong')].some(element => element.textContent === '核对项目部署'));
  assert.equal(await tasks.getByText('历史任务（1）', { exact: true }).count(), 0, 'Completed history has no entry in the call UI');
  assert.equal(await tasks.getByText('原文摘录：部署配置已经核对。', { exact: true }).count(), 0);
  await page.screenshot({ path: 'test-results/voice/packaged-running-only.png' });
  assert.equal(await tasks.getByText('迟到的旧阶段', { exact: true }).count(), 0, 'Late progress cannot regress terminal task state');
  assert.equal(await tasks.getByText('正在比较模型', { exact: true }).count(), 1, 'The second task must keep its own stage');
  assert.equal(await page.locator('.voice-call-captions').textContent(), '桌面通话已接通。');
  assert.equal(metrics.played, 1, 'Metadata updates cannot create extra audio playback');
  await page.screenshot({ path: 'test-results/voice/packaged-tasks-completed.png' });
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
  const restoredChat = await page.locator('#chat-frame').boundingBox();
  assert.ok(Math.abs(restoredChat.width - fullChatWidth) < 2, 'Hangup restores full chat width');
  assert.equal(await chatInput.inputValue(), '通话中仍可编辑左侧草稿');
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: true, packaged: true, hiddenCapture: true, nativeTaskStages: true, interactiveSplit: true, ...metrics }));
} finally {
  try { if (application) await application.close(); }
  finally {
    for (const socket of [...voice.clients, ...relay.clients]) socket.terminate();
    voice.close(); relay.close(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
  }
}
