"""Content-free input diagnostics; never stores audio or transcripts."""
import math,struct,time

class InputHealth:
    def __init__(self,now=time.monotonic):
        self.now=now;self.frames=0;self.last_frame=None;self.last_signal=None
        self.started=None;self.notified=False;self.muted=False;self.last_log=now()
    def frame(self,pcm):
        now=self.now();self.frames+=1;self.last_frame=now
        samples=struct.unpack('<320h',pcm)
        rms=math.sqrt(sum(v*v for v in samples)/320)/32768
        if rms>.008:self.last_signal=now
    def asr(self,kind):
        if kind in ('started','delta') and self.started is None:
            self.started=self.now();self.notified=False
        elif kind in ('completed','failed'):
            self.started=None;self.notified=False
    def snapshot(self):
        now=self.now()
        return {'frames':self.frames,'frame_gap':round(now-self.last_frame,2) if self.last_frame is not None else None,
                'signal_gap':round(now-self.last_signal,2) if self.last_signal is not None else None,
                'pending_seconds':round(now-self.started,2) if self.started is not None else 0,'muted':self.muted}
    def stalled(self):
        if self.started is None or self.notified or self.now()-self.started<12 or self.muted:return False
        # Continued speech must not be classified as a stuck utterance.
        if self.last_signal is not None and self.now()-self.last_signal<3:return False
        self.notified=True;return True
