class BeingCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.enabled = false;
    this.frame = new Int16Array(320);
    this.index = 0; this.sum = 0; this.weight = 0;
    this.ratio = sampleRate / 16000;
    this.port.onmessage = event => {
      this.enabled = event.data === true;
      this.index = 0; this.sum = 0; this.weight = 0;
    };
  }
  process(inputs) {
    if (!this.enabled) return true;
    const samples = inputs[0]?.[0];
    if (!samples) return true;
    for (const sample of samples) {
      let remaining = 1;
      while (remaining > 1e-8) {
        const used = Math.min(remaining, this.ratio - this.weight);
        this.sum += sample * used; this.weight += used; remaining -= used;
        if (this.weight >= this.ratio - 1e-8) {
          this.frame[this.index++] = Math.max(-1, Math.min(1, this.sum / this.weight)) * 32767;
          this.sum = 0; this.weight = 0;
          if (this.index === 320) {
            this.port.postMessage(this.frame.buffer, [this.frame.buffer]);
            this.frame = new Int16Array(320); this.index = 0;
          }
        }
      }
    }
    return true;
  }
}
registerProcessor('being-capture', BeingCapture);
