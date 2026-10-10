import { afterEach, expect, test, vi } from 'vitest';
import { CallAudio } from '../desktop/renderer/voice/services/audio';
afterEach(() => vi.useRealTimers());
function setup() {
 const nodes: any[] = [];
 const context = { currentTime: 0, state: 'running', destination: {}, close: vi.fn(() => Promise.resolve()),
  createBuffer: (_: number, length: number, rate: number) => ({ duration: length / rate, getChannelData: () => new Float32Array(length) }),
  createBufferSource: () => { const n = { buffer: null, onended: null, connect: vi.fn(), disconnect: vi.fn(), stop: vi.fn(), start: vi.fn() }; nodes.push(n); return n; } };
 const ack=vi.fn(), lost=vi.fn(), diagnostic=vi.fn(); const audio=new CallAudio(vi.fn(),vi.fn(),ack,vi.fn(),lost,diagnostic);
 (audio as any).context=context;
 const play=(seconds=1)=>audio.play(Buffer.from(new Float32Array(24000*seconds).buffer).toString('base64'));
 return {audio,context,nodes,ack,lost,play,diagnostic};
}
test('native pause freezes receipts and resumes the unplayed offset',()=>{
 const t=setup();t.play();t.audio.segment('a');t.context.currentTime=.4;
 const late=t.nodes[0].onended;t.audio.pause(true);expect(t.audio.busy).toBe(true);
 t.context.currentTime=4;t.play(.2);t.audio.segment('b');late();
 expect(t.ack).not.toHaveBeenCalled();expect(t.nodes).toHaveLength(1);
 t.audio.resume();expect(t.nodes).toHaveLength(3);expect(t.nodes[1].start.mock.calls[0][1]).toBeCloseTo(.365);
 expect(t.nodes[2].start.mock.calls[0][0]).toBeCloseTo(4.67);
 t.context.currentTime=4.68;t.nodes[1].onended();expect(t.ack.mock.calls).toEqual([['a']]);
 t.context.currentTime=4.88;t.nodes[2].onended();expect(t.ack.mock.calls).toEqual([['a'],['b']]);
});
test('confirmed pending owns an empty queue beyond the local timeout',()=>{
 vi.useFakeTimers();const t=setup();t.audio.pause(true);t.context.currentTime=4;t.play();
 vi.advanceTimersByTime(5000);expect(t.nodes).toHaveLength(0);expect(t.audio.busy).toBe(true);
 t.audio.resume();expect(t.nodes).toHaveLength(1);expect(t.nodes[0].start.mock.calls[0][0]).toBeCloseTo(4.07);
});
test('clear and close discard paused tails without acknowledging them',()=>{
 const t=setup();t.play();t.audio.segment('unheard');t.context.currentTime=.1;t.audio.pause(true);
 t.context.currentTime=10;t.audio.clear();t.audio.resume();expect(t.ack).not.toHaveBeenCalled();expect(t.audio.busy).toBe(false);
 t.play();t.audio.pause(true);t.audio.close();t.audio.resume();expect(t.audio.busy).toBe(false);
});
test('local energy hint requires echo cancellation and excludes transients',()=>{
 vi.useFakeTimers();const t=setup();t.play();t.audio.setNearEndProtection(true);
 const feed=(level:number,count:number)=>{for(let i=0;i<count;i++)(t.audio as any).observeNearEnd(new Int16Array(320).fill(level*32767).buffer);};
 feed(.1,20);expect(t.nodes[0].stop).not.toHaveBeenCalled();
 (t.audio as any).echoCancelled=true;feed(.1,5);expect(t.nodes[0].stop).not.toHaveBeenCalled();
 feed(0,20);feed(.1,16);expect(t.nodes[0].stop).toHaveBeenCalledOnce();
 t.audio.pause(true);vi.advanceTimersByTime(2000);feed(0,100);expect(t.nodes).toHaveLength(1);
 t.audio.resume();expect(t.nodes).toHaveLength(2);
});
test('local guesses recover; confirmed backlog ends instead of resuming over input',()=>{
 vi.useFakeTimers();const t=setup();t.play();t.audio.pause();vi.advanceTimersByTime(1200);expect(t.nodes).toHaveLength(2);
 t.audio.pause(true);for(let i=0;i<30;i++)t.play();expect(t.lost).toHaveBeenCalledOnce();expect(t.ack).not.toHaveBeenCalled();
});

test('reproduces lost ASR final: confirmed pending holds scheduled audio indefinitely until cleared', () => {
 vi.useFakeTimers(); const t=setup();t.audio.pause(true);t.context.currentTime=1;
 t.play(2);t.audio.segment('progress-delivered-but-not-played');
 vi.advanceTimersByTime(60000);t.context.currentTime=61;
 expect(t.audio.busy).toBe(true);expect(t.nodes).toHaveLength(0);expect(t.ack).not.toHaveBeenCalled();
 expect(t.lost).not.toHaveBeenCalled(); // A wall-clock deadline is absent; the bound only counts added PCM.
 t.audio.clear();expect(t.audio.busy).toBe(false);expect(t.ack).not.toHaveBeenCalled();
 t.play(.2);expect(t.nodes).toHaveLength(1); // A later generation can sound despite the older silent progress.
});

test('diagnostics are content-free transitions rather than per PCM packet',()=>{
 vi.useFakeTimers();const t=setup();t.play(.2);t.play(.2);t.play(.2);
 expect(t.diagnostic.mock.calls.map(([e])=>e.state)).toEqual(['scheduled']);
 t.audio.pause();t.audio.pause(true);t.audio.pause(true);vi.advanceTimersByTime(3000);t.context.currentTime=3;t.audio.resume();
 t.context.currentTime=4;for(const node of t.nodes.slice(3))node.onended();t.audio.clear();
 const events=t.diagnostic.mock.calls.map(([e])=>e);
 expect(events.map(e=>e.state)).toEqual(['scheduled','paused-local','paused-confirmed','scheduled','resumed','drained','cleared']);
 expect(events.find(e=>e.state==='resumed')?.paused_ms).toBe(3000);
 for(const event of events){expect(Object.keys(event).sort()).toEqual(['context_state','frames','paused_ms','queued_ms','reason','state','type'].sort());expect(event.reason).toBe('voice_audio_state');}
});

test('last onended before audio deadline still acknowledges without user input',()=>{
 vi.useFakeTimers();const t=setup();t.play();t.audio.segment('last');
 t.context.currentTime=1.034;t.nodes[0].onended();expect(t.ack).not.toHaveBeenCalled();
 t.context.currentTime=1.06;vi.advanceTimersByTime(30);
 expect(t.ack.mock.calls).toEqual([['last']]);vi.advanceTimersByTime(2000);expect(t.ack).toHaveBeenCalledOnce();t.audio.close();
});
test('receipt recheck never commits a paused, suspended or cleared tail',()=>{
 vi.useFakeTimers();const t=setup();t.play();t.audio.segment('tail');
 vi.advanceTimersByTime(3000);expect(t.ack).not.toHaveBeenCalled();
 t.context.currentTime=.5;t.audio.pause(true);t.context.currentTime=5;
 vi.advanceTimersByTime(3000);expect(t.ack).not.toHaveBeenCalled();
 t.audio.clear();vi.advanceTimersByTime(3000);expect(t.ack).not.toHaveBeenCalled();t.audio.close();
});
