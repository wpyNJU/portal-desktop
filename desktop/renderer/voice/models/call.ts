import { Store } from '../../shared/models/store';
import { VOICE_OPTIONS, type VoiceAPI, type VoiceCommand, type VoiceEvent } from '../../../shared/voice';
import { CallAudio } from '../services/audio';
import { VoiceHistoryStore, contextWindow, type VoiceRecord } from '../services/history';

export class VoiceCall extends Store {
  open = false;
  phase: 'idle' | 'preparing' | 'setup' | 'connecting' | 'active' | 'ended' | 'error' = 'idle';
  name = 'Being'; status = ''; muted = false; speaking = false; captions = true;
  heard = ''; answer = ''; elapsed = 0; startedAt = 0; cacheNotice = '';
  voice: string = VOICE_OPTIONS[0].id;
  profileState: 'loading' | 'ready' | 'missing' | 'updating' | 'error' = 'loading';
  profileUpdatedAt: string | null = null;
  profileMessage = '';
  private profileRevision = 0;
  endpoint = ''; sceneId = '';
  private id = '';
  private audio?: CallAudio;
  private timer?: ReturnType<typeof setInterval>;
  private deadline?: ReturnType<typeof setTimeout>;
  private stopEvents?: () => void;
  private connected = false;
  private audioReady = false;
  private ending = false;
  private requests = 0;
  private records = new Map<string, VoiceRecord>();
  private response = '';
  private playbackGeneration?: number;
  private replyComplete = false;
  private history = new VoiceHistoryStore();
  constructor(private api?: VoiceAPI, private createAudio: (...args: ConstructorParameters<typeof CallAudio>) => CallAudio = (...args) => new CallAudio(...args)) {
    super();
    try {
      const saved = localStorage.getItem('portal.voice.preferences');
      const options = saved ? JSON.parse(saved) : {};
      if (VOICE_OPTIONS.some(item => item.id === options.voice)) this.voice = options.voice;
      this.captions = options.captions !== false;
    } catch { /* Defaults remain usable with unavailable storage. */ }
  }
  get busy() { return ['preparing', 'setup', 'active', 'connecting'].includes(this.phase); }
  get onboarding() { return this.phase === 'preparing' || this.phase === 'setup'; }
  get profileWorking() { return this.profileState === 'loading' || this.profileState === 'updating'; }
  get scope() { return JSON.stringify([this.endpoint, this.sceneId]); }
  preferences(voice = this.voice, captions = this.captions) {
    this.voice = voice; this.captions = captions;
    try { localStorage.setItem('portal.voice.preferences', JSON.stringify({ voice, captions })); } catch { /* Session-only preferences. */ }
    this.changed();
  }
  async start(endpoint: string, sceneId: string, name: string) {
    if (this.busy) { this.open = true; this.changed(); return; }
    this.open = true; this.phase = 'preparing'; this.status = '正在读取助手信息…'; this.name = name;
    this.endpoint = endpoint; this.sceneId = sceneId; this.heard = ''; this.answer = ''; this.elapsed = 0;
    this.muted = false; this.speaking = false; this.connected = false; this.audioReady = false; this.ending = false;
    this.cacheNotice = ''; this.response = ''; this.records.clear(); this.requests = 0;
    this.playbackGeneration = undefined;
    this.replyComplete = false;
    this.profileUpdatedAt = null; this.profileMessage = ''; this.profileState = 'loading';
    const id = this.id = crypto.randomUUID();
    this.changed();
    if (!this.api || !endpoint || !sceneId) { this.fail('请先连接 Being，再开始语音通话。'); return; }
    await this.readProfile();
  }
  async readProfile() {
    if (!this.api || this.profileState === 'updating') return;
    const revision = ++this.profileRevision, id = this.id;
    this.profileState = 'loading'; this.profileMessage = '正在读取已保存的助手信息…'; this.changed();
    try {
      const profile = await this.api.profile(this.endpoint);
      if (revision !== this.profileRevision || id !== this.id) return;
      this.profileUpdatedAt = profile.updatedAt;
      this.profileState = profile.updatedAt ? 'ready' : 'missing';
      this.profileMessage = profile.updatedAt ? '已保存的身份、称呼和偏好会用于通话。' : '尚未对齐助手身份、称呼和偏好。仅在你点击后从 Agent 获取。';
      if (this.onboarding) {
        let skipped = false;
        try { skipped = localStorage.getItem(`portal.voice.profile-skipped:${this.endpoint}`) === '1'; } catch { /* Ask again if storage is unavailable. */ }
        if (profile.updatedAt || skipped) { await this.connect(id); return; }
        this.phase = 'setup'; this.status = '先让语音助手认识你';
      }
    } catch {
      if (revision !== this.profileRevision || id !== this.id) return;
      this.profileState = 'error'; this.profileMessage = '暂时无法读取资料状态，可重试或先开始通话。';
      if (this.onboarding) { this.phase = 'setup'; this.status = '助手资料状态暂不可用'; }
    }
    this.changed();
  }
  async updateProfile() {
    if (!this.api || this.profileWorking) return;
    const revision = ++this.profileRevision, id = this.id;
    this.profileState = 'updating'; this.profileMessage = '正在向 Agent 对齐身份、称呼和偏好，可能需要几十秒…'; this.changed();
    try {
      const profile = await this.api.profile(this.endpoint, true);
      if (revision !== this.profileRevision || id !== this.id) return;
      this.profileUpdatedAt = profile.updatedAt; this.profileState = 'ready';
      this.profileMessage = this.phase === 'active' ? '资料已更新，下次通话生效。' : '资料已保存，以后直接复用；需要时可主动更新。';
      if (this.phase === 'setup') { await this.connect(id); return; }
    } catch {
      if (revision !== this.profileRevision || id !== this.id) return;
      this.profileState = 'error'; this.profileMessage = '未能确认更新结果，已有资料仍可使用。可重新读取状态或稍后重试。';
    }
    this.changed();
  }
  async skipProfile() {
    if (this.phase !== 'setup' || this.profileWorking) return;
    try { localStorage.setItem(`portal.voice.profile-skipped:${this.endpoint}`, '1'); } catch { /* Current call can still proceed. */ }
    await this.connect(this.id);
  }
  private async connect(id: string) {
    if (!id || id !== this.id || !this.api) return;
    this.phase = 'connecting'; this.status = '正在连接…'; this.changed();
    this.stopEvents?.();
    this.stopEvents = this.api.onEvent(event => { if (this.id === id && event.callId === id) this.receive(event.data); });
    this.deadline = setTimeout(() => { if (this.id === id) this.fail('连接超时，请确认麦克风权限和网络后重试。'); }, 35000);
    try {
      const scope = this.scope;
      const rows = await this.history.read(scope).catch(() => { this.cacheNotice = '本机缓存暂不可用'; return []; });
      if (this.id !== id) return;
      await this.api.start({ callId: id, endpoint: this.endpoint, sceneId: this.sceneId, voice: this.voice, history: contextWindow(rows) });
      if (this.id !== id) { void this.api.stop(id); return; }
      const audio = this.audio = this.createAudio(
        data => { if (this.id === id && this.phase === 'active' && !this.muted) this.send({ type: 'input_audio_buffer.append', audio: data }); },
        playing => { if (this.id === id) { this.speaking = playing; this.send({ type: 'client.playback', playing, generation: this.playbackGeneration }); this.changed(); } },
        token => { if (this.id === id) this.send({ type: 'task.segment.played', token }); },
        () => {
          if (this.id !== id) return;
          if (this.ending) this.end();
          else if (this.replyComplete && this.phase === 'active') { this.status = '我在听，你可以继续说'; this.changed(); }
        },
        () => { if (this.id === id) this.fail('麦克风已断开，请重新连接设备后再试。'); });
      this.status = '正在启用麦克风…'; this.changed();
      await audio.open();
      if (this.id !== id) { audio.close(); return; }
      this.audioReady = true; this.activate();
    } catch (error) {
      if (this.id === id) this.fail(error instanceof DOMException && error.name === 'NotAllowedError'
        ? '麦克风未获授权，请在系统设置中允许后重试。' : '暂时无法开始通话，请检查语音服务与麦克风。');
    }
  }
  private activate() {
    if (!this.connected || !this.audioReady || this.phase !== 'connecting') return;
    clearTimeout(this.deadline); this.phase = 'active'; this.status = '我在听，你可以直接说'; this.startedAt = Date.now();
    this.audio?.setEnabled(true);
    this.timer = setInterval(() => { this.elapsed = Math.floor((Date.now() - this.startedAt) / 1000); this.changed(); }, 1000);
    this.changed();
  }
  private send(command: VoiceCommand) {
    const id = this.id;
    if (!id || !this.api) return;
    if (this.requests > 50) { this.fail('音频传输拥堵，请重新开始通话。'); return; }
    this.requests++;
    void this.api.send(id, command).catch(() => { if (this.id === id) this.fail('音频传输中断，请重新开始通话。'); })
      .finally(() => { if (this.id === id) this.requests--; });
  }
  toggleMute() {
    if (this.phase !== 'active' || this.ending) return;
    this.muted = !this.muted; this.audio?.setEnabled(!this.muted);
    this.send({ type: this.muted ? 'input_audio_mute.commit' : 'input_audio_unmute.commit' }); this.changed();
  }
  private remember(id: string, role: 'user' | 'assistant', text: string, append = false, offset?: number) {
    if (!text) return;
    const key = `${this.scope}:${id}`, old = this.records.get(key);
    if (offset !== undefined && offset > (old?.text.length || 0)) return;
    const row: VoiceRecord = { id: key, scope: this.scope, role, at: old?.at || Date.now(),
      text: append ? (old?.text || '') + text.slice(offset === undefined ? 0 : Math.max(0, (old?.text.length || 0) - offset)) : text };
    this.records.set(key, row);
    void this.history.write(row).catch(() => { this.cacheNotice = '本机缓存暂不可用'; this.changed(); });
    return row.text;
  }
  private receive(data: VoiceEvent['data']) {
    const text = (field: string) => typeof data[field] === 'string' ? data[field] as string : '';
    try {
      if (typeof data.generation === 'number' && Number.isSafeInteger(data.generation)) {
        if (this.playbackGeneration !== undefined && data.generation < this.playbackGeneration) return;
        if (data.generation !== this.playbackGeneration) {
          // Finish the old queue using its generation before adopting the new one.
          this.replyComplete = false; this.audio?.clear(); this.playbackGeneration = data.generation;
        }
      }
      switch (data.type) {
        case 'session.created': this.connected = true; this.activate(); break;
        case 'session.status': if (this.phase === 'connecting') this.status = '正在连接语音…'; break;
        case 'error': this.fail(text('message') || '语音连接异常。'); return;
        case 'call.closed': this.end(); return;
        case 'playback.clear': this.audio?.clear(); this.status = '我在听'; break;
        case 'conversation.item.input_audio_transcription.started':
          this.replyComplete = false; this.audio?.clear(); this.heard = ''; this.answer = ''; this.response = ''; this.status = '我在听'; break;
        case 'conversation.item.input_audio_transcription.delta': this.heard = text('delta'); break;
        case 'conversation.item.input_audio_transcription.completed':
          this.replyComplete = false;
          this.heard = text('text') || this.heard;
          this.remember(`${this.id}:u:${text('item_id') || crypto.randomUUID()}`, 'user', this.heard);
          this.status = '正在回应你'; break;
        case 'response.output_text.delta': {
          this.replyComplete = false;
          const response = text('response_id') || this.response || crypto.randomUUID();
          if (response !== this.response) this.answer = '';
          this.response = response; this.answer += text('delta');
          this.remember(`${this.id}:a:${response}`, 'assistant', text('delta'), true); break;
        }
        case 'response.output_audio.delta': case 'agent.audio.delta': case 'opening.audio': this.audio?.play(text('delta')); break;
        case 'task.segment.end': this.replyComplete = true; this.audio?.segment(text('token')); break;
        case 'agent.opening': case 'agent.status': this.status = text('text'); break;
        case 'agent.text.delta': {
          const answer = this.remember(`agent:${text('call_id')}`, 'assistant', text('delta'), true,
            typeof data.offset === 'number' ? data.offset : undefined);
          if (answer) this.answer = answer; break;
        }
        case 'agent.answer': this.answer = text('text'); this.remember(`${this.id}:agent:${crypto.randomUUID()}`, 'assistant', this.answer); break;
        case 'response.done': this.replyComplete = true; if (!this.audio?.busy) this.status = '我在听，你可以继续说'; break;
        case 'call.ending': this.audio?.clear(); this.muted = true; this.audio?.setEnabled(false); this.answer = text('text'); this.status = '正在告别'; break;
        case 'call.end':
          this.ending = true;
          if (!this.audio?.busy) this.end(data.reason === 'idle_timeout' ? '空闲超过 30 秒，通话已结束' : '通话已结束');
          break;
      }
    } catch { this.fail('语音播放异常，请重新开始通话。'); return; }
    this.changed();
  }
  private release() {
    const id = this.id; this.id = '';
    this.profileRevision++;
    if (this.profileWorking) { this.profileState = 'error'; this.profileMessage = '可重新读取助手资料状态。'; }
    clearTimeout(this.deadline); clearInterval(this.timer); this.stopEvents?.(); this.stopEvents = undefined;
    this.audio?.close(); this.audio = undefined; this.speaking = false;
    if (id) void this.api?.stop(id).catch(() => {});
  }
  end(message = '通话已结束') { this.release(); this.phase = 'ended'; this.status = message; this.changed(); }
  fail(message: string) { this.release(); this.phase = 'error'; this.status = message; this.changed(); }
  dismiss() { this.end(); this.open = false; this.changed(); }
  dispose() { this.release(); }
}
