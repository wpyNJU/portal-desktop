// Isolated desktop fixture: real preload, local protocol, permissions and voice
// transport; synthetic microphone and a loopback voice server, no live credentials.
import { app, BrowserWindow, ipcMain } from 'electron';
import { WebSocketServer } from 'ws';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { VoiceService } from '../../desktop/main/voice/service';
import { configureLocalSession, registerLocalProtocol } from '../../desktop/main/app/protocol';
import { ChatProxy } from '../../desktop/main/chat/proxy';
import { parseConnection } from '../../desktop/main/chat/connection';
import { protocol } from 'electron';

app.setPath('userData', path.resolve('test-results/voice/profile'));
app.commandLine.appendSwitch('use-fake-device-for-media-stream');
protocol.registerSchemesAsPrivileged([{ scheme: 'beings', privileges: { standard: true, secure: true, supportFetchAPI: true, stream: true } }]);
app.whenReady().then(async () => {
const scene = { scene_id: 'desktop-voice-fixture', scene_meta: { client: 'portal-desktop/test', scene_label: '日常交流' } };
const connection = parseConnection('https://example.com/being/?token=fixture-only');
const snapshot = { settings: { endpoint: connection.endpoint, being: 'being', hasToken: true, workspace: process.cwd(),
  portalName: 'Heart Portal', portalBinary: '', autoStart: false, allowExec: false, kitsEnabled: false },
  portal: { phase: 'stopped', message: '未启动', logs: [] }, chatScene: scene, chatSessions: [scene] };
const metrics = { frames: 0, starts: 0, closes: 0, muted: 0, unmuted: 0, played: 0, badFrames: 0, permissions: [], profiles: 0, refreshes: 0 };
let updatedAt = null;
globalThis.voiceMetrics = metrics;
const server = new WebSocketServer({ host: '127.0.0.1', port: 0 });
await new Promise(resolve => server.on('listening', resolve));
let active;
server.on('connection', socket => {
  active = socket;
  socket.on('message', bytes => {
    const data = JSON.parse(bytes.toString());
    if (data.type === 'profile.status') { metrics.profiles++; socket.send(JSON.stringify({ type: 'profile.status', updated_at: updatedAt })); }
    if (data.type === 'profile.refresh') { metrics.refreshes++; updatedAt = '2026-09-28T10:00:00Z'; socket.send(JSON.stringify({ type: 'profile.updated', updated_at: updatedAt })); }
    if (data.type === 'session.start') { metrics.starts++; socket.send('{"type":"session.created"}'); }
    if (data.type === 'session.close') { metrics.closes++; socket.close(); }
    if (data.type === 'input_audio_mute.commit') metrics.muted++;
    if (data.type === 'input_audio_unmute.commit') metrics.unmuted++;
    if (data.type === 'task.segment.played') metrics.played++;
    if (data.type === 'input_audio_buffer.append') {
      metrics.frames++;
      if (Buffer.from(data.audio, 'base64').length !== 640) metrics.badFrames++;
      if (metrics.frames === 1) {
        socket.send(JSON.stringify({ type: 'conversation.item.input_audio_transcription.completed', item_id: 'u1', text: '今天有什么计划？' }));
        socket.send(JSON.stringify({ type: 'response.output_text.delta', response_id: 'a1', delta: '我在，慢慢说。我们一起安排今天的事情。' }));
        socket.send(JSON.stringify({ type: 'response.output_audio.delta', delta: Buffer.alloc(2400 * 4).toString('base64') }));
        socket.send(JSON.stringify({ type: 'task.segment.end', token: 'fixture-segment' }));
        socket.send('{"type":"response.done"}');
      }
    }
  });
});
globalThis.voiceFixtureDisconnect = () => active?.terminate();
const proxy = new ChatProxy(() => connection, async request => {
  const pathname = new URL(request).pathname;
  const value = pathname.endsWith('/history') ? { messages: [] } : pathname.endsWith('/active') ? { active: false }
    : pathname.endsWith('/status') ? { being_name: 'being', tools: 0 } : { sbs_enabled: false, model: 'fixture' };
  return Response.json(value);
}, scene);
registerLocalProtocol(path.resolve('.vite/renderer/main_window'), proxy);
const window = new BrowserWindow({ show: false, width: 1100, height: 840, webPreferences: {
  preload: path.resolve('test-results/voice/preload.cjs'), sandbox: true, contextIsolation: true, nodeIntegration: false,
} });
const voice = new VoiceService(() => connection, () => scene.scene_id,
  event => window.webContents.send('beings:voice-event', event), () => new WebSocket(`ws://127.0.0.1:${server.address().port}`));
configureLocalSession({ contents: () => window.webContents, url: () => 'beings://desktop/', active: () => voice.active });
const handlers = {
  'beings:voice-profile': (endpoint, refresh) => voice.profile(endpoint, refresh),
  'beings:voice-start': input => voice.start(input), 'beings:voice-send': (id, data) => voice.send(id, data), 'beings:voice-stop': id => voice.stop(id),
  'beings:snapshot': () => snapshot, 'beings:appearance': () => 'light', 'beings:notification-target': () => null,
  'beings:update-state': () => ({ phase: 'current', currentVersion: '0.1.7', message: '', releaseUrl: '' }),
  'beings:browser-state': () => ({ open: false, title: '', address: '', loading: false, canGoBack: false, canGoForward: false }),
  'beings:town-auth': () => ({ configured: false }),
  'beings:town-live': () => ({ phase: 'unpaired', generation: 0, revision: 0, sync: 0, message: '', versions: { bonfire: 0, mail: 0, firesides: 0 } }),
  'beings:scene-tasks': () => ({ endpoint: connection.endpoint, tasks: [] }),
  'beings:town': () => ({ items: [] }),
};
const channels = [...readFileSync('desktop/preload/preload.ts', 'utf8').matchAll(/ipcRenderer\.invoke\(['"]([^'"]+)/g)].map(match => match[1]);
for (const channel of new Set(channels)) ipcMain.handle(channel, (event, ...args) => {
  if (event.sender !== window.webContents || event.senderFrame !== window.webContents.mainFrame) throw new Error('Untrusted IPC');
  return handlers[channel]?.(...args);
});
await window.loadURL('beings://desktop/');
app.on('before-quit', () => { voice.stop(); server.close(); });
}).catch(error => { console.error(error); app.exit(1); });
