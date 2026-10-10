// Exercise the shipped playback handlers with a deterministic fake audio clock.
const {readFileSync}=require('node:fs');
const {runInNewContext}=require('node:vm');
const assert=require('node:assert/strict');
const source=readFileSync(__dirname+'/mobile/call.js','utf8');
const handlers=source.slice(source.indexOf('function send(data)'),source.indexOf('async function startCall'));
function setup(provider='doubao',enabled=true){
 const sent=[],created=[],timers=new Map();let id=0;
 const el={textContent:'',classList:{add(){},remove(){}}};
 const ctx={currentTime:0,destination:{},createBuffer:(_,length,rate)=>({duration:length/rate,getChannelData:()=>new Float32Array(length)}),
 createBufferSource:()=>{const n={connect(){},disconnect(){},stop(){this.stopped=true;},start(time,offset){this.time=time;this.offset=offset;}};created.push(n);return n;}};
 const box={ctx,nodes:new Set(),nextTime:0,activeProvider:provider,active:true,muted:false,firstAudio:true,endAfterPlayback:false,
 firstAt:0,responseId:'',questionId:'',document:{body:el},$:()=>el,status(){},rememberEvent(){},
 atob:s=>Buffer.from(s,'base64').toString('binary'),ws:{readyState:1,send:s=>sent.push(JSON.parse(s))},
 setTimeout:fn=>{timers.set(++id,fn);return id;},clearTimeout:id=>timers.delete(id)};
 runInNewContext(handlers+`\nnearEndEnabled=${enabled};this.play=play;this.event=event;this.pause=pauseNearEnd;this.resume=resumeNearEnd;this.feed=observeNearEnd;`,box);
 const audio=(seconds=.2)=>box.play({delta:Buffer.from(new Float32Array(Math.round(24000*seconds)).buffer).toString('base64')});
 const feed=(level,count)=>{for(let i=0;i<count;i++)box.feed(new Int16Array(320).fill(Math.round(level*32767)).buffer);};
 const timeout=()=>{for(const [id,fn] of [...timers.entries()]){timers.delete(id);fn();}};
 return {box,ctx,sent,created,timers,audio,feed,timeout};
}
let t=setup();t.audio(1);t.box.event({type:'task.segment.end',token:'a'});t.ctx.currentTime=.4;
t.feed(.1,5);assert.ok(!t.created[0].stopped);t.feed(0,20);t.feed(.1,16);
assert.ok(t.created[0].stopped);assert.equal(t.box.nodes.size,0);assert.equal(t.sent.filter(x=>x.type==='task.segment.played').length,0);
t.ctx.currentTime=2;t.audio(.2);t.box.event({type:'task.segment.end',token:'b'});
assert.equal(t.created.length,1);assert.equal(t.sent.filter(x=>x.type==='task.segment.played').length,0);
t.timeout();assert.equal(t.created.length,3);assert.ok(Math.abs(t.created[1].offset-.34)<1e-8);
assert.ok(Math.abs(t.created[2].time-2.72)<1e-8);
t.ctx.currentTime=2.73;t.created[1].onended();assert.deepEqual(t.sent.filter(x=>x.type==='task.segment.played').map(x=>x.token),['a']);
t.ctx.currentTime=2.93;t.created[2].onended();assert.deepEqual(t.sent.filter(x=>x.type==='task.segment.played').map(x=>x.token),['a','b']);
t=setup();t.audio(1);t.box.event({type:'task.segment.end',token:'unheard'});t.ctx.currentTime=.1;
const late=t.created[0].onended;t.box.pause();t.ctx.currentTime=10;t.box.event({type:'playback.clear'});late();t.timeout();
assert.equal(t.created.length,1);assert.equal(t.sent.filter(x=>x.type==='task.segment.played').length,0);assert.equal(t.timers.size,0);
t=setup();t.audio();t.box.event({type:'speech.input.pending'});t.box.event({type:'conversation.item.input_audio_transcription.started'});
assert.equal(t.box.nodes.size,0);t.box.event({type:'speech.input.resume'});assert.equal(t.box.nodes.size,1);
t=setup();t.audio(1);t.feed(.1,16);t.ctx.currentTime=2;t.timeout();const count=t.created.length;
t.feed(.1,100);assert.equal(t.created.length,count);t.feed(0,20);t.feed(.1,16);assert.equal(t.box.nodes.size,0);
for(const [provider,enabled] of [['gptlive',true],['doubao',false]]){t=setup(provider,enabled);t.audio();t.feed(.2,100);assert.ok(!t.created[0].stopped);}
t=setup();t.audio(1);t.box.pause();for(let i=0;i<41;i++)t.audio(.2);assert.ok(t.box.nodes.size>0);assert.equal(t.timers.size,0);
// Confirmed ASR input owns the pause until a semantic verdict, beyond the
// local 1.2-second energy fallback and even across a quiet microphone gap.
t=setup();t.audio(1);t.ctx.currentTime=.2;t.feed(.1,16);
assert.equal(t.timers.size,1);t.box.event({type:'speech.input.pending'});assert.equal(t.timers.size,0);
t.ctx.currentTime=5;t.timeout();t.feed(0,100);assert.equal(t.box.nodes.size,0);
t.box.event({type:'speech.input.resume'});assert.equal(t.box.nodes.size,1);
assert.ok(Math.abs(t.created.at(-1).offset-.14)<1e-8);
t=setup();t.box.event({type:'speech.input.pending'});t.ctx.currentTime=4;t.audio(.5);
assert.equal(t.created.length,0);t.box.event({type:'task.segment.end',token:'pending-only'});
assert.equal(t.sent.filter(x=>x.type==='task.segment.played').length,0);
t.box.event({type:'speech.input.resume'});assert.equal(t.created.length,1);
assert.ok(t.created[0].time>=4);assert.equal(t.created[0].offset,0);
t=setup();t.audio(1);t.box.event({type:'speech.input.pending'});t.ctx.currentTime=6;t.audio(.2);
t.box.event({type:'playback.clear'});t.box.event({type:'speech.input.resume'});
assert.equal(t.created.length,1);assert.equal(t.box.nodes.size,0);
assert.equal(t.sent.filter(x=>x.type==='task.segment.played').length,0);
t=setup();t.audio(1);t.box.event({type:'speech.input.pending'});t.box.event({type:'conversation.item.input_audio_transcription.failed'});assert.equal(t.box.nodes.size,1);
console.log('resumable near-end playback checks passed');
