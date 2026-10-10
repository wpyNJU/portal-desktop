import type { VoiceAudioDiagnostic } from '../../../shared/voice';

export class CallAudio {
  private context?: AudioContext;
  private mic?: MediaStream;
  private capture?: AudioWorkletNode;
  private source?: MediaStreamAudioSourceNode;
  private sink?: GainNode;
  private sources = new Map<AudioBufferSourceNode, { start: number; offset: number }>();
  private paused?: { at: number; frames: { buffer: AudioBuffer; start: number; offset: number }[]; confirmed: boolean; added: number; since: number };
  private pauseTimer?: ReturnType<typeof setTimeout>;
  private receiptTimer?: ReturnType<typeof setTimeout>;
  private protection = false;
  private echoCancelled = false;
  private noise = .008;
  private loudRun = 0;
  private quietRun = 0;
  private latched = false;
  private tokens: { token: string; end: number }[] = [];
  private playbackEpoch = 0;
  private next = 0;
  private closed = false;
  enabled = false;
  constructor(private frame: (audio: string) => void, private playing: (value: boolean) => void,
    private acknowledge: (token: string) => void, private drained: () => void, private deviceLost: () => void,
    private diagnostic: (event: Omit<VoiceAudioDiagnostic, 'generation'>) => void = () => {}) {}
  private diagnose(state: VoiceAudioDiagnostic['state'], pausedMs?: number) {
    const paused = this.paused;
    const queued = paused ? paused.frames.reduce((sum, frame) => sum + frame.buffer.duration - frame.offset, 0)
      : Math.max(0, this.next - (this.context?.currentTime ?? 0));
    // Scheduling proves queue acceptance, not audible output at the speaker.
    try { this.diagnostic({ type: 'client.diagnostic', reason: 'voice_audio_state', state, context_state: this.context?.state || 'closed',
      paused_ms: Math.min(3600000, Math.max(0, Math.round(pausedMs ?? (paused ? Date.now() - paused.since : 0)))),
      queued_ms: Math.min(3600000, Math.max(0, Math.round(queued * 1000))),
      frames: Math.min(100000, paused?.frames.length ?? this.sources.size) }); } catch { /* Diagnostics cannot interrupt audio. */ }
  }
  get busy() { return this.sources.size > 0 || Boolean(this.paused); }
  setNearEndProtection(enabled: boolean) { this.protection = enabled; }
  async open() {
    const context = this.context = new AudioContext({ sampleRate: 16000 });
    await context.resume();
    if (this.closed) return;
    await context.audioWorklet.addModule(new URL('./capture-worklet.js?no-inline', import.meta.url));
    if (this.closed) return;
    const stream = await navigator.mediaDevices.getUserMedia({ audio: {
      echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1,
    }, video: false });
    if (this.closed) { stream.getTracks().forEach(track => track.stop()); return; }
    this.mic = stream;
    this.echoCancelled = stream.getAudioTracks()[0]?.getSettings().echoCancellation === true;
    for (const track of stream.getAudioTracks()) track.onended = () => { if (!this.closed) this.deviceLost(); };
    this.capture = new AudioWorkletNode(context, 'being-capture');
    this.capture.port.onmessage = event => {
      if (!this.closed && this.enabled) {
        this.observeNearEnd(event.data);
        this.frame(btoa(String.fromCharCode(...new Uint8Array(event.data))));
      }
    };
    this.source = context.createMediaStreamSource(stream);
    this.sink = context.createGain(); this.sink.gain.value = 0;
    this.source.connect(this.capture).connect(this.sink).connect(context.destination);
    this.setEnabled(this.enabled);
  }
  setEnabled(enabled: boolean) {
    if (!enabled && !this.paused?.confirmed) this.resume();
    this.enabled = enabled;
    this.mic?.getTracks().forEach(track => { track.enabled = enabled; });
    this.capture?.port.postMessage(enabled);
  }
  play(encoded: string) {
    const context = this.context;
    if (this.closed || !context || context.state === 'closed') return;
    const bytes = Uint8Array.from(atob(encoded), character => character.charCodeAt(0));
    if (!bytes.length || bytes.length % 4) return;
    const samples = new Float32Array(bytes.buffer);
    const buffer = context.createBuffer(1, samples.length, 24000);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < samples.length; i++) channel[i] = Number.isFinite(samples[i]) ? Math.max(-1, Math.min(1, samples[i])) : 0;
    this.next = Math.max(this.next, (this.paused?.at ?? context.currentTime) + .035);
    const start = this.next; this.next += buffer.duration;
    if (this.paused) {
      this.paused.frames.push({ buffer, start, offset: 0 }); this.paused.added += buffer.duration;
      if (this.paused.confirmed) {
        if (this.paused.added >= 30 || this.paused.frames.length >= 2048) { this.deviceLost(); this.clear(); }
      } else if (this.paused.added >= 8 || this.paused.frames.length >= 128) this.resume();
    } else this.schedule(buffer, start, 0);
  }
  private schedule(buffer: AudioBuffer, start: number, offset: number) {
    const context = this.context!;
    const node = context.createBufferSource(); node.buffer = buffer; node.connect(context.destination);
    const wasPlaying = this.sources.size > 0; this.sources.set(node, { start, offset });
    const epoch = this.playbackEpoch;
    node.start(start, offset);
    if (!wasPlaying) { this.playing(true); this.diagnose('scheduled'); }
    node.onended = () => {
      node.disconnect(); this.sources.delete(node);
      if (epoch !== this.playbackEpoch) return;
      this.confirm();
      if (!this.busy && !this.closed) { this.playing(false); this.diagnose('drained'); this.drained(); }
    };
  }
  pause(confirmed = false) {
    if (this.closed || !this.context) return;
    const wasConfirmed = this.paused?.confirmed;
    const wasPaused = Boolean(this.paused);
    this.cancelPauseTimer();
    if (!this.paused && (confirmed || this.sources.size)) {
      const at = this.context.currentTime;
      this.confirm(); this.playbackEpoch++;
      const frames: { buffer: AudioBuffer; start: number; offset: number }[] = [];
      for (const [node, plan] of this.sources) {
        const offset = plan.offset + Math.max(0, at - plan.start);
        if (node.buffer && offset < node.buffer.duration) frames.push({ buffer: node.buffer, start: Math.max(at, plan.start), offset });
        node.onended = null; try { node.stop(); } catch { /* Already ended. */ } node.disconnect();
      }
      this.sources.clear(); this.paused = { at, frames, confirmed, added: 0, since: Date.now() }; this.playing(false);
    }
    if (this.paused && confirmed) this.paused.confirmed = true;
    if (this.paused && (!wasPaused || (confirmed && !wasConfirmed)))
      this.diagnose(this.paused.confirmed ? 'paused-confirmed' : 'paused-local');
    if (this.paused && !this.paused.confirmed) this.pauseTimer = setTimeout(() => this.resume(), 1200);
  }
  resume() {
    this.cancelPauseTimer();
    const paused = this.paused, context = this.context;
    if (!paused || !context || this.closed) return;
    this.paused = undefined;
    const shift = context.currentTime + .035 - paused.at;
    this.next += shift;
    for (const token of this.tokens) token.end += shift;
    for (const frame of paused.frames) this.schedule(frame.buffer, frame.start + shift, frame.offset);
    this.confirm();
    this.diagnose('resumed', Date.now() - paused.since);
    if (!this.busy) { this.playing(false); this.diagnose('drained'); this.drained(); }
  }
  private cancelPauseTimer() { if (this.pauseTimer !== undefined) clearTimeout(this.pauseTimer); this.pauseTimer = undefined; }
  // Conservative energy hint only; it does not distinguish stop commands from acknowledgement.
  private observeNearEnd(buffer: ArrayBuffer) {
    if (!this.protection || !this.echoCancelled || this.paused?.confirmed) return;
    const pcm = new Int16Array(buffer); if (!pcm.length) return;
    let energy = 0; for (const value of pcm) energy += (value / 32768) ** 2;
    const rms = Math.sqrt(energy / pcm.length), duration = Math.min(pcm.length / 16000, .04);
    if (!this.busy) { this.noise = .98 * this.noise + .02 * Math.min(rms, .03); this.loudRun = 0; this.quietRun = 0; this.latched = false; return; }
    if (rms <= Math.max(.045, this.noise * 5)) {
      this.loudRun = 0; this.quietRun += duration;
      if (this.quietRun >= .4) { this.resume(); this.latched = false; }
    } else {
      this.quietRun = 0; this.loudRun += duration;
      if (this.loudRun >= .3 && !this.latched) { this.latched = true; this.pause(); }
    }
  }
  segment(token: string) { this.tokens.push({ token, end: this.next }); this.confirm(); }
  private confirm() {
    if (this.receiptTimer !== undefined) clearTimeout(this.receiptTimer);
    this.receiptTimer = undefined;
    const now = this.paused?.at ?? this.context?.currentTime ?? 0;
    const complete = this.tokens.filter(item => item.end <= now);
    this.tokens = this.tokens.filter(item => item.end > now);
    for (const { token } of complete) this.acknowledge(token);
    // onended can precede the last scheduled deadline by an audio render quantum.
    // Recheck against the audio clock, never wait for another input/playback event.
    if (this.tokens.length && !this.paused && !this.closed) {
      const remaining = Math.min(...this.tokens.map(item => item.end)) - now;
      this.receiptTimer = setTimeout(() => this.confirm(), Math.min(1000, Math.max(20, remaining * 1000 + 20)));
    }
  }
  clear() {
    // Audio can have ended before its JS onended callback gets CPU time.
    // Preserve only physically completed segments, never a partially heard one.
    const pausedMs = this.paused ? Date.now() - this.paused.since : 0;
    this.confirm(); this.playbackEpoch++; this.cancelPauseTimer(); this.paused = undefined;
    this.loudRun = 0; this.quietRun = 0; this.latched = false;
    this.tokens = [];
    if (this.receiptTimer !== undefined) clearTimeout(this.receiptTimer);
    this.receiptTimer = undefined;
    for (const node of this.sources.keys()) { node.onended = null; try { node.stop(); } catch { /* Already ended. */ } node.disconnect(); }
    this.sources.clear(); this.next = this.context?.currentTime || 0; this.playing(false); this.diagnose('cleared', pausedMs);
  }
  close() {
    this.closed = true; this.setEnabled(false); this.clear();
    this.mic?.getTracks().forEach(track => { track.onended = null; track.stop(); });
    this.source?.disconnect(); this.capture?.disconnect(); this.capture?.port.close(); this.sink?.disconnect();
    void this.context?.close().catch(() => {});
  }
}
