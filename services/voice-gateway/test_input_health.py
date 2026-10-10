import struct,unittest
from input_health import InputHealth

class InputHealthTests(unittest.TestCase):
    def setUp(self):
        self.t=0;self.health=InputHealth(lambda:self.t)
    def test_no_frames_and_silent_pending_recognition_notify_once(self):
        self.health.asr('started');self.t=11;self.assertFalse(self.health.stalled())
        self.t=12;self.assertTrue(self.health.stalled());self.assertFalse(self.health.stalled())
        self.health.asr('completed');self.t=30;self.assertFalse(self.health.stalled())
    def test_speech_and_mute_are_not_declared_stalled(self):
        self.health.asr('started');self.t=15
        self.health.frame(struct.pack('<320h',*([1000]*320)))
        self.assertFalse(self.health.stalled())
        self.t=20;self.health.muted=True;self.assertFalse(self.health.stalled())
        self.health.muted=False;self.assertTrue(self.health.stalled())
    def test_diagnostics_have_no_payload_and_track_silence(self):
        self.health.asr('delta');self.t=4;self.health.frame(bytes(640));self.t=5
        self.assertEqual(self.health.snapshot(),{'frames':1,'frame_gap':1,'signal_gap':None,'pending_seconds':5,'muted':False})
        self.health.asr('failed');self.assertEqual(self.health.snapshot()['pending_seconds'],0)

if __name__=='__main__':unittest.main()
