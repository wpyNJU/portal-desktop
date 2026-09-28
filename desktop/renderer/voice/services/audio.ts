export class CallAudio {
  private context?: AudioContext;
  private mic?: MediaStream;
  private capture?: AudioWorkletNode;
  private source?: MediaStreamAudioSourceNode;
  private sink?: GainNode;
  private sources = new Set<AudioBufferSourceNode>();
  private tokens: string[] = [];
  private next = 0;
  private closed = false;
  enabled = false;
  constructor(private frame: (audio: string) => void, private playing: (value: boolean) => void,
    private acknowledge: (token: string) => void, private drained: () => void, private deviceLost: () => void) {}
  get busy() { return this.sources.size > 0; }
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
    for (const track of stream.getAudioTracks()) track.onended = () => { if (!this.closed) this.deviceLost(); };
    this.capture = new AudioWorkletNode(context, 'being-capture');
    this.capture.port.onmessage = event => {
      if (!this.closed && this.enabled) this.frame(btoa(String.fromCharCode(...new Uint8Array(event.data))));
    };
    this.source = context.createMediaStreamSource(stream);
    this.sink = context.createGain(); this.sink.gain.value = 0;
    this.source.connect(this.capture).connect(this.sink).connect(context.destination);
    this.setEnabled(this.enabled);
  }
  setEnabled(enabled: boolean) {
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
    const node = context.createBufferSource(); node.buffer = buffer; node.connect(context.destination);
    const wasPlaying = this.busy; this.sources.add(node);
    this.next = Math.max(this.next, context.currentTime + .035);
    node.start(this.next); this.next += buffer.duration;
    if (!wasPlaying) this.playing(true);
    node.onended = () => {
      node.disconnect(); this.sources.delete(node);
      if (!this.busy && !this.closed) { this.confirm(); this.playing(false); this.drained(); }
    };
  }
  segment(token: string) { this.tokens.push(token); if (!this.busy) this.confirm(); }
  private confirm() { for (const token of this.tokens.splice(0)) this.acknowledge(token); }
  clear() {
    this.tokens = [];
    for (const node of this.sources) { node.onended = null; try { node.stop(); } catch { /* Already ended. */ } node.disconnect(); }
    this.sources.clear(); this.next = this.context?.currentTime || 0; this.playing(false);
  }
  close() {
    this.closed = true; this.setEnabled(false); this.clear();
    this.mic?.getTracks().forEach(track => { track.onended = null; track.stop(); });
    this.source?.disconnect(); this.capture?.disconnect(); this.capture?.port.close(); this.sink?.disconnect();
    void this.context?.close().catch(() => {});
  }
}
