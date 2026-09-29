import type { Connection } from '../chat/connection';
import { redact } from '../chat/connection';
import { VOICE_OPTIONS, type VoiceCommand, type VoiceEvent, type VoiceStart, type VoiceProfile } from '../../shared/voice';

// Existing being voice gateway. Credentials are added here, never in a renderer URL.
export const VOICE_GATEWAY = 'wss://6af7181982ba42a8-55442.cn-south-nas-3.gpu-instance.ppinfra.com/pipeline/ws';
type Socket = Pick<WebSocket, 'readyState' | 'bufferedAmount' | 'send' | 'close' | 'onopen' | 'onmessage' | 'onerror' | 'onclose'>;

export class VoiceService {
  private profiles = new Map<string, Promise<VoiceProfile>>();
  private current?: { id: string; link: string; scene: string; socket: Socket; connected: boolean; timer: ReturnType<typeof setTimeout> };
  constructor(private connection: () => Connection | null, private scene: () => string | undefined,
    private emit: (event: VoiceEvent) => void, private connect: (url: string) => Socket = url => new WebSocket(url)) {}
  get active() { return !!this.current; }
  profile(endpoint: string, refresh = false): Promise<VoiceProfile> {
    const connection = this.connection();
    if (!connection || endpoint !== connection.endpoint || typeof refresh !== 'boolean')
      return Promise.reject(new Error('请先连接当前 Being。'));
    const key = `${connection.link}:${refresh}`;
    const pending = this.profiles.get(key);
    if (pending) return pending;
    const request = new Promise<VoiceProfile>((resolve, reject) => {
      const socket = this.connect(VOICE_GATEWAY);
      let settled = false;
      const finish = (error?: string, value?: VoiceProfile) => {
        if (settled) return; settled = true; clearTimeout(timer);
        socket.onopen = socket.onmessage = socket.onclose = socket.onerror = null;
        socket.close();
        if (error) reject(new Error(error)); else resolve(value!);
      };
      const valid = () => {
        if (this.connection()?.link === connection.link) return true;
        finish('Being 已切换，请重新读取助手信息。'); return false;
      };
      const timer = setTimeout(() => finish(refresh ? '未能确认更新结果，请稍后重新读取状态。' : '读取助手信息超时，可重试或先开始通话。'), refresh ? 60000 : 10000);
      socket.onopen = () => {
        if (valid()) socket.send(JSON.stringify({ type: refresh ? 'profile.refresh' : 'profile.status', agent_url: connection.link }));
      };
      socket.onmessage = event => {
        if (!valid()) return;
        try {
          if (typeof event.data !== 'string' || event.data.length > 16000) throw new Error();
          const data = JSON.parse(event.data);
          if (data.type === 'error') { finish('助手信息暂时无法更新或读取，请稍后重试。'); return; }
          if (data.type !== (refresh ? 'profile.updated' : 'profile.status')) return;
          const value = data.updated_at;
          if (value !== null && (typeof value !== 'string' || value.length > 80 || !Number.isFinite(Date.parse(value)))) throw new Error();
          if (refresh && !value) throw new Error();
          finish(undefined, { updatedAt: value });
        } catch { finish('助手信息返回异常，请重新读取状态。'); }
      };
      socket.onerror = socket.onclose = () => finish('助手信息连接中断，请稍后重试。');
    });
    this.profiles.set(key, request);
    const cleanup = () => { if (this.profiles.get(key) === request) this.profiles.delete(key); };
    void request.then(cleanup, cleanup);
    return request;
  }
  start(input: VoiceStart) {
    const connection = this.connection();
    if (!connection || !input || input.endpoint !== connection.endpoint || !input.sceneId || input.sceneId !== this.scene())
      throw new Error('请先连接 Being，并在当前场景开始通话。');
    if (this.current) throw new Error('已有通话正在进行。');
    if (!/^[a-zA-Z0-9-]{8,80}$/.test(input.callId) || !VOICE_OPTIONS.some(item => item.id === input.voice))
      throw new Error('无效的通话配置。');
    if (!Array.isArray(input.history) || input.history.length > 500 || input.history.some(row =>
      !row || !['user', 'assistant'].includes(row.role) || typeof row.text !== 'string') ||
      input.history.reduce((size, row) => size + row.text.length, 0) > 24000) throw new Error('通话上下文过长。');
    const socket = this.connect(VOICE_GATEWAY);
    const call = { id: input.callId, link: connection.link, scene: input.sceneId, socket, connected: false,
      timer: setTimeout(() => this.fail(input.callId, '语音连接超时，请稍后重试。'), 30_000) };
    this.current = call;
    socket.onopen = () => {
      if (this.current !== call || !this.matches()) { this.stop(call.id); return; }
      socket.send(JSON.stringify({ type: 'session.start', agent_url: connection.link, voice: input.voice, history: input.history }));
    };
    socket.onmessage = event => {
      if (this.current !== call) return;
      if (!this.matches()) { this.fail(call.id, 'Being 或场景已切换，通话已结束。'); return; }
      try {
        if (typeof event.data !== 'string' || event.data.length > 4_000_000) throw new Error('Invalid voice event');
        const data = JSON.parse(event.data);
        if (!data || typeof data.type !== 'string') throw new Error('Invalid voice event');
        if (data.type === 'session.created') { call.connected = true; clearTimeout(call.timer); }
        if (data.type === 'error') { this.fail(call.id, '语音服务暂时不可用，请稍后重试。'); return; }
        if (data.type === 'session.closed') { this.stop(call.id); return; }
        // Session settings and provider internals do not belong in the UI.
        if (data.type === 'session.created') this.emit({ callId: call.id, data: { type: data.type } });
        else this.emit({ callId: call.id, data: JSON.parse(redact(JSON.stringify(data), [connection.token, connection.relaySecret])) });
      } catch { this.fail(call.id, '语音数据接收异常，请重新开始通话。'); }
    };
    socket.onerror = () => this.fail(call.id, call.connected ? '语音连接已断开，点击可重新通话。' : '语音连接失败，请检查网络后重试。');
    socket.onclose = () => {
      if (this.current === call) this.fail(call.id, '语音连接已断开，点击可重新通话。');
    };
  }
  private matches() { return this.current?.link === this.connection()?.link && this.current?.scene === this.scene(); }
  private fail(id: string, message: string) {
    if (this.current?.id !== id) return;
    this.emit({ callId: id, data: { type: 'error', message } });
    this.stop(id);
  }
  send(id: string, command: VoiceCommand) {
    const call = this.current;
    if (!call || call.id !== id) return;
    if (!this.matches()) { this.fail(id, 'Being 或场景已切换，通话已结束。'); return; }
    if (!command || typeof command !== 'object') throw new Error('无效的语音指令。');
    let data: VoiceCommand;
    switch (command.type) {
      case 'input_audio_buffer.append':
        if (typeof command.audio !== 'string' || !/^[A-Za-z0-9+/]{854}==$/.test(command.audio) || Buffer.from(command.audio, 'base64').length !== 640)
          throw new Error('无效的音频帧。');
        data = { type: command.type, audio: command.audio }; break;
      case 'client.playback':
        if (command.generation !== undefined && (!Number.isSafeInteger(command.generation) || command.generation < 0))
          throw new Error('无效的播放轮次。');
        data = { type: command.type, playing: command.playing === true, generation: command.generation }; break;
      case 'task.segment.played':
        if (typeof command.token !== 'string' || command.token.length > 256) throw new Error('无效的播放确认。');
        data = { type: command.type, token: command.token }; break;
      case 'input_audio_mute.commit': case 'input_audio_unmute.commit': case 'response.cancel': data = { type: command.type }; break;
      default: throw new Error('不支持的语音指令。');
    }
    if (call.socket.bufferedAmount > 262144) { this.fail(id, '网络持续拥堵，请重新开始通话。'); return; }
    if (call.socket.readyState === 1) call.socket.send(JSON.stringify(data));
  }
  stop(id?: string) {
    const call = this.current;
    if (!call || (id && id !== call.id)) return;
    this.current = undefined;
    clearTimeout(call.timer);
    call.socket.onopen = call.socket.onmessage = call.socket.onerror = call.socket.onclose = null;
    if (call.socket.readyState === 1) call.socket.send(JSON.stringify({ type: 'session.close' }));
    call.socket.close();
    this.emit({ callId: call.id, data: { type: 'call.closed' } });
  }
}
