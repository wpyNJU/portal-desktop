'use strict';
const $=id=>document.getElementById(id);
// Keep cached older pages usable while the script and HTML update independently.
if(!$('profile-panel')&&$('refresh-profile')){
 const panel=document.createElement('section');panel.id='profile-panel';panel.setAttribute('aria-label','助手信息');
 const heading=document.createElement('h3');heading.textContent='助手信息';
 const refresh=$('refresh-profile');refresh.before(panel);panel.append(heading,$('profile-updated'),$('profile-state'),refresh);
 for(const [id,label] of [['profile-retry','重新读取状态'],['profile-continue','先跳过，直接通话']]){
  const button=document.createElement('button');button.id=id;button.type='button';button.className='setting-button hidden';button.textContent=label;panel.append(button);
 }
 $('profile-state').setAttribute('role','status');
}
let ctx,ws,mic,source,capture,sink,active=false,starting=false,muted=false,closing=false;
let nodes=new Set(),nextTime=0,timer,startTime=0,responseId='',questionId='',firstAt=0,firstAudio=false;
let congestedSince=null,networkWarning=false,endAfterPlayback=false;
let callGeneration=0,cancelStartup=null,stopping=false;
let profileInfo={scope:null,state:'unknown',updated:null,message:''},profilePending=null,profileIntent=null,profileGate=false;
let agentConfig=null;try{agentConfig=JSON.parse(localStorage.getItem('voice.agent')||'null');}catch{}
if(!agentConfig?.url||!agentConfig?.scope)agentConfig=null;
// Full text history stays on this browser; only a bounded recent window enters each model session.
let historyDB=null,historyRows=new Map(),historyCall='',historyUser='',historyAssistant='',historyFailed=false;
const historyReady=new Promise(resolve=>{
 try{
  const request=indexedDB.open('voice-conversation',1);
  request.onupgradeneeded=()=>request.result.createObjectStore('messages',{keyPath:'id'});
  request.onerror=()=>{historyFailure();resolve();};
  request.onsuccess=()=>{historyDB=request.result;const read=historyDB.transaction('messages').objectStore('messages').getAll();
   read.onsuccess=()=>{for(const row of read.result)historyRows.set(row.id,row);renderHistory();resolve();};
   read.onerror=()=>{historyFailure();resolve();};};
 }catch{historyFailure();resolve();}
});
function historyFailure(){historyFailed=true;$('history-state').textContent='本机缓存不可用，本次记录可能无法保存';}
function orderedHistory(){return [...historyRows.values()].filter(row=>row.scope===agentConfig?.scope).sort((a,b)=>a.at-b.at||a.order-b.order);}
function renderHistory(){
 if(!historyFailed)$('history-state').textContent=`本机已保存 ${orderedHistory().length} 条记录 · 重连自动续聊`;
 const box=$('history-list');box.textContent='';
 for(const row of orderedHistory()){
  const card=document.createElement('div');card.className='card';
  const label=document.createElement('div');label.className='label';label.textContent=(row.role==='user'?'你':row.source==='agent'?'助手 · 查询结果':'助手')+' · '+new Date(row.at).toLocaleString();
  const body=document.createElement('div');body.className='answer';body.textContent=row.text;card.append(label,body);box.append(card);
 }
}
function remember(id,role,text,source='voice',append=false,offset=null){
 if(!id||!text)return;
 const key=source==='agent'?'agent:'+agentConfig?.scope+':'+id:historyCall+':'+id,previous=historyRows.get(key);
 let value=text;
 if(append){const old=previous?.text||'';if(Number.isInteger(offset)&&offset>old.length)return;value=old+text.slice(Number.isInteger(offset)?Math.max(0,old.length-offset):0);}
 const row={id:key,scope:agentConfig?.scope,role,text:value,source,at:previous?.at||Date.now(),order:previous?.order??historyRows.size};historyRows.set(key,row);
 if(historyDB){try{const tx=historyDB.transaction('messages','readwrite');tx.objectStore('messages').put(row);tx.onerror=historyFailure;tx.oncomplete=()=>{if(!historyFailed)$('history-state').textContent=`本机已保存 ${orderedHistory().length} 条记录 · 重连自动续聊`;};}catch{historyFailure();}}
}
function historyWindow(){
 const rows=orderedHistory(),selected=[];let left=24000;
 for(let i=rows.length-1;i>=0&&left>0;i--){const row=rows[i],text=row.text.slice(-left);selected.unshift({role:row.role,text,source:row.source,at:row.at});left-=text.length;}
 return selected;
}
function rememberEvent(d){
 if(d.type==='conversation.item.input_audio_transcription.started')historyUser=d.item_id||crypto.randomUUID();
 if(d.type==='conversation.item.input_audio_transcription.completed')remember('u-'+(d.item_id||historyUser),'user',d.text);
 if(d.type==='response.output_text.delta'){historyAssistant=d.response_id||historyAssistant||crypto.randomUUID();remember('a-'+historyAssistant,'assistant',d.delta,'voice',true);}
 if(d.type==='response.done')historyAssistant='';
 if(d.type==='agent.text.delta')remember('tool-'+d.call_id,'assistant',d.delta,'agent',true,d.offset);
 if(d.type==='agent.answer')remember('fallback-'+crypto.randomUUID(),'assistant',d.text,'agent');
 if(d.type==='call.ending')remember('bye','assistant',d.text);
}
$('history-view').ontoggle=()=>{if($('history-view').open)renderHistory();};
function deadline(promise,ms,message){let t;return Promise.race([promise,new Promise((_,reject)=>{t=setTimeout(()=>reject(new Error(message)),ms);})]).finally(()=>clearTimeout(t));}
function finishFarewell(){if(endAfterPlayback&&!nodes.size){endAfterPlayback=false;hangup();}}
function networkPressure(bytes,now){
 if(bytes<=16000){congestedSince=null;return 'ok';}
 if(bytes<32000&&congestedSince===null)return 'ok';
 if(congestedSince===null)congestedSince=now;
 if(bytes>262144||now-congestedSince>=8000)return 'failed';
 return 'slow';
}
const worklet=`class BeingCapture extends AudioWorkletProcessor {
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
registerProcessor('voice-capture', BeingCapture);
`;
function status(text,title){$('state').textContent=text;if(title)$('title').textContent=title;}
function controls(){
 for(const id of ['mute','interrupt'])$(id).disabled=!active||stopping;
 $('hangup').disabled=stopping||(!active&&!starting);$('test').disabled=active||starting||stopping;
 const button=$('call-toggle');
 button.disabled=stopping;
 button.classList.toggle('ending',active||starting);
 const label=stopping?'正在结束通话…':starting?'连接中 · 点击取消':active?'点击结束通话':'点击开始通话';
 $('slide-hint').textContent=label;
 button.setAttribute('aria-label',label);
}

function send(data){if(ws?.readyState===1)ws.send(JSON.stringify(data));}
let reportSegments=[],playbackGeneration,playbackEpoch=0;
function playback(playing){send({type:'client.playback',playing,generation:playbackGeneration});}
function acknowledgeSegment(){
 const now=ctx?.currentTime||0,complete=reportSegments.filter(s=>s.end<=now);
 reportSegments=reportSegments.filter(s=>s.end>now);
 for(const s of complete)send({type:'task.segment.played',token:s.token});
}
function clearAudio(){acknowledgeSegment();reportSegments=[];playbackEpoch++;for(const n of nodes){n.onended=null;try{n.stop();}catch{}n.disconnect();}nodes.clear();playback(false);nextTime=ctx?.currentTime||0;document.body.classList.remove('talking');}
function listen(){if(active)status(muted?'麦克风已关闭':'我在听，你可以继续说',muted?'已静音':'正在聆听');}
function play(data){
 if(!ctx||!active)return;
 const bytes=Uint8Array.from(atob(data.delta),c=>c.charCodeAt(0)),pcm=new Float32Array(bytes.buffer);
 if(!pcm.length)return;
 if(!firstAudio){firstAudio=true;if(firstAt)$('metrics').textContent=`首段语音 ${((performance.now()-firstAt)/1000).toFixed(1)} 秒 · 豆包全双工` ;}
 const b=ctx.createBuffer(1,pcm.length,24000),a=b.getChannelData(0);for(let i=0;i<a.length;i++)a[i]=Number.isFinite(pcm[i])?Math.max(-1,Math.min(1,pcm[i])):0;
 const n=ctx.createBufferSource();n.buffer=b;n.connect(ctx.destination);const wasPlaying=nodes.size>0,epoch=playbackEpoch;nodes.add(n);if(!wasPlaying)playback(true);
 nextTime=Math.max(nextTime,ctx.currentTime+.06);n.start(nextTime);nextTime+=b.duration;
 n.onended=()=>{n.disconnect();nodes.delete(n);if(epoch!==playbackEpoch)return;acknowledgeSegment();if(!nodes.size){playback(false);document.body.classList.remove('talking');if(endAfterPlayback)finishFarewell();else listen();}};
 document.body.classList.add('talking');status('你可以直接插话','正在说话');
}
function event(d){
 if(Number.isSafeInteger(d.generation)){
  if(playbackGeneration!==undefined&&d.generation<playbackGeneration)return;
  if(d.generation!==playbackGeneration){clearAudio();playbackGeneration=d.generation;}
 }
 rememberEvent(d);
 const type=d.type;
 if(type==='task.segment.end'){reportSegments.push({token:d.token,end:nextTime});acknowledgeSegment();return;}
 if(type==='task.updated'){taskRows.set(d.task.id,d.task);renderTasks();return;}
 if(type==='agent.delivery'||type==='agent.progress'||type==='agent.text.delta'||type==='agent.text.done'||type==='agent.text.error'){
  let card=document.getElementById('agent-'+d.call_id);
  if(!card){card=document.createElement('div');card.id='agent-'+d.call_id;card.className='card';const label=document.createElement('div');label.className='label';label.textContent='助手 · 查询结果';const body=document.createElement('div');body.className='answer';card.append(label,body);$('agent-results').append(card);}
  if(type==='agent.text.delta'){const previous=card.lastElementChild.textContent;
   const offset=Number.isInteger(d.offset)?d.offset:previous.length;
   if(offset<=previous.length){const overlap=previous.length-offset;card.lastElementChild.textContent+=d.delta.slice(overlap);}
   $('caption-empty').classList.add('hidden');}
  if(type==='agent.delivery'){card.firstElementChild.textContent=d.text;}
  if(type==='agent.progress'){card.firstElementChild.textContent=(taskRows.get(d.call_id)?.title||'查询')+' · '+d.text+' · '+d.elapsed+' 秒';}
  if(type==='agent.text.done'){card.firstElementChild.textContent='助手 · 查询结果已返回';$('ack').textContent='';}
  if(type==='agent.text.error'){card.firstElementChild.textContent=d.message;$('ack').textContent='';}
  return;
 }
 if(type==='agent.audio.delta'){play(d);return;}

 if(type==='call.ending'){clearAudio();muted=true;capture?.port.postMessage(false);$('answer').textContent=d.text;status('说完再见后结束通话','正在告别');return;}
 if(type==='call.end'){if(d.reason==='idle_timeout'){hangup().then(()=>status('空闲超过 30 秒，点击可继续聊','通话已自动结束'));return;}endAfterPlayback=true;finishFarewell();return;}
 if(type==='session.status'){status(d.message,'连接中');return;}
 if(type==='playback.clear'){clearAudio();$('ack').textContent='';return;}
 if(type==='conversation.item.input_audio_transcription.started'){
  clearAudio();
  questionId=d.item_id;responseId='';firstAt=0;firstAudio=false;$('heard').textContent='';$('answer').textContent='';$('ack').textContent='';status('你说，我在听','正在聆听');
 }
 if(type==='conversation.item.input_audio_transcription.delta')$('heard').textContent=d.delta||'';
 if(type==='conversation.item.input_audio_transcription.completed'){$('heard').textContent=d.text||$('heard').textContent;firstAt=performance.now();status('正在回应你','正在回应');}
 if(type==='response.output_text.delta'){
  if(responseId&&d.response_id!==responseId)$('answer').textContent+='\n';responseId=d.response_id;$('answer').textContent+=d.delta||'';
 }
 if(type==='response.output_audio.delta')play(d);
 if(type==='opening.audio')play(d);
 if(type==='agent.status'){$('ack').textContent=d.text;if(!nodes.size)status(d.text,'正在处理');}
 if(type==='agent.opening')$('ack').textContent=d.text;
 if(type==='agent.answer')$('answer').textContent=d.text;
 if(type==='response.done'){$('ack').textContent='';if(!nodes.size)listen();}
 if(type==='error'){const message=d.message||'语音服务暂时不可用';hangup().then(()=>status(message,'连接异常'));}
}
async function startCall(test=false,profileAccepted=false){
 if(!agentConfig){openSettings();$('agent-config-state').textContent='先粘贴你的助手链接并保存，再开始通话';$('agent-url').focus();return;}
 if(starting||active||stopping||profileGate)return;
 if(!profileAccepted){
  const scope=agentConfig.scope,generation=++callGeneration;profileGate=true;status('正在读取已保存的助手信息','准备通话');
  if(profileInfo.scope!==scope||['unknown','error','loading','updating'].includes(profileInfo.state))await readProfileTime();
  if(generation!==callGeneration||scope!==agentConfig?.scope)return;profileGate=false;
  if(!profileInfo.updated&&readSetting('voice.profile-skipped:'+scope,'')!=='1'){
   profileIntent={scope,test};openSettings(false);renderProfilePanel();$('profile-panel').scrollIntoView({block:'center'});return;
  }
 }
 if(starting||active||stopping)return;const generation=++callGeneration;starting=true;closing=false;playbackGeneration=undefined;controls();status('正在启动手机音频','准备通话');
 const check=()=>{if(generation!==callGeneration)throw new Error('通话已取消');};
 const cancelled=new Promise((_,reject)=>{cancelStartup=()=>reject(new Error('通话已取消'));});
 const step=(p,ms,message)=>Promise.race([deadline(p,ms,message),cancelled]);
 try{
  await step(historyReady,5000,'读取本机对话缓存超时，请重试');check();historyCall=crypto.randomUUID();historyUser='';historyAssistant='';
  ctx=new(window.AudioContext||window.webkitAudioContext)({sampleRate:16000});
  const audioContext=ctx;
  $('audio-unlock').onclick=()=>{audioContext.resume().catch(()=>{});};
  const unlockTimer=setTimeout(()=>{if(generation===callGeneration&&audioContext.state!=='running'){$('audio-unlock').classList.remove('hidden');status('手机需要一次点击来启用声音','点击下方启用声音');}},900);
  try{await step(audioContext.resume(),20000,'手机音频未启动，请再次点击启用声音');check();}
  finally{clearTimeout(unlockTimer);if(generation===callGeneration)$('audio-unlock').classList.add('hidden');}
  if(ctx.sampleRate!==16000)throw new Error('浏览器未支持16k音频，请换用最新版浏览器');
  const url=URL.createObjectURL(new Blob([worklet],{type:'text/javascript'}));try{await step(ctx.audioWorklet.addModule(url),10000,'音频组件加载超时，请刷新页面重试');check();}finally{URL.revokeObjectURL(url);}
  capture=new AudioWorkletNode(ctx,'voice-capture');sink=ctx.createGain();sink.gain.value=0;capture.connect(sink).connect(ctx.destination);
  if(test){const b=await ctx.decodeAudioData(await(await fetch('/call-assets/self-test.wav')).arrayBuffer());source=ctx.createBufferSource();source.buffer=b;}
  else{status('请允许使用麦克风','准备通话');const request=navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true,channelCount:1},video:false});
   request.then(stream=>{if(generation!==callGeneration)stream.getTracks().forEach(t=>t.stop());},()=>{});
   mic=await step(request,25000,'麦克风授权未完成，请允许麦克风后重试');check();source=ctx.createMediaStreamSource(mic);}
  status('正在连接语音服务','连接中');
  await step(new Promise((resolve,reject)=>{
   const s=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/pipeline/ws');ws=s;
   s.onopen=()=>s.send(JSON.stringify({type:'session.start',agent_url:agentConfig.url,voice:$('voice-select').value,history:historyWindow()}));
   const timeout=setTimeout(()=>{s.close();reject(new Error('语音连接超时，请检查网络后重试'));},18000);
   s.onmessage=e=>{if(ws!==s)return;const d=JSON.parse(e.data);if(d.type==='session.created'){clearTimeout(timeout);resolve();}else if(d.type==='error'&&!active){clearTimeout(timeout);reject(new Error(d.message||'会话建立失败'));}else event(d);};
   s.onerror=()=>{clearTimeout(timeout);reject(new Error('连接失败'));};
   s.onclose=()=>{clearTimeout(timeout);if(ws!==s)return;if(starting)reject(new Error('连接中断'));if(active&&!closing)hangup().then(()=>status('连接已断开，请重新开始','通话已结束'));};
  }),20000,'语音连接超时，请重试');check();cancelStartup=null;
  active=true;starting=false;muted=false;firstAudio=false;firstAt=0;responseId='';questionId='';congestedSince=null;networkWarning=false;$('agent-results').textContent='';$('heard').textContent='—';$('answer').textContent='';$('ack').textContent='';$('mute').textContent='麦克风开';
  capture.port.onmessage=e=>{
   if(!active||muted)return;
   if(ws?.readyState!==1)return;
   const pressure=networkPressure(ws.bufferedAmount,performance.now());
   if(pressure==='failed'){
    send({type:'client.diagnostic',reason:'audio_backpressure',buffered_bytes:ws.bufferedAmount});
    hangup().then(()=>status('网络持续拥堵，通话已结束，请重新开始','连接不稳定'));return;
   }
   if(pressure==='slow'&&!networkWarning){networkWarning=true;status('网络有波动，正在等待恢复','通话中');}
   if(pressure==='ok'&&networkWarning){networkWarning=false;if(!nodes.size)listen();}
   send({type:'input_audio_buffer.append',audio:btoa(String.fromCharCode(...new Uint8Array(e.data)))});
  };
  capture.port.postMessage(true);source.connect(capture);if(test)source.start(ctx.currentTime+.2);
  startTime=Date.now();timer=setInterval(()=>{let s=Math.floor((Date.now()-startTime)/1000);$('clock').textContent=`${String(Math.floor(s/60)).padStart(2,'0')}:${String(s%60).padStart(2,'0')}`;},1000);
  document.body.classList.add('active');$('caption-empty').classList.add('hidden');$('transcript').classList.remove('hidden');controls();listen();
 }catch(e){if(generation===callGeneration){await hangup();status(e.name==='NotAllowedError'?'请允许麦克风权限后重试':e.message,'暂时无法开始');}}
 finally{if(generation===callGeneration){cancelStartup=null;starting=false;controls();}}
}
async function hangup(){
 if(stopping)return;stopping=true;controls();
 profileGate=false;profileIntent=null;
 ++callGeneration;if(cancelStartup){cancelStartup();cancelStartup=null;}$('audio-unlock').classList.add('hidden');
 closing=true;active=false;endAfterPlayback=false;clearInterval(timer);clearAudio();capture?.port.postMessage(false);
 try{source?.disconnect();capture?.disconnect();sink?.disconnect();}catch{}
 mic?.getTracks().forEach(t=>t.stop());mic=null;
 const old=ws;ws=null;if(old?.readyState===1){old.send(JSON.stringify({type:'session.close'}));setTimeout(()=>old.close(),1200);}else old?.close();
 if(ctx&&ctx.state!=='closed')await deadline(ctx.close(),1500,'关闭音频超时').catch(()=>{});ctx=null;source=null;capture=null;starting=false;
 stopping=false;document.body.classList.remove('active');$('clock').textContent='未通话';$('ack').textContent='';controls();status('点击可以再次开始','通话已结束');
}
$('test').onclick=()=>{$('settings').close();startCall(true);};$('hangup').onclick=hangup;
$('interrupt').onclick=()=>{clearAudio();send({type:'response.cancel'});listen();};
$('mute').onclick=()=>{muted=!muted;capture?.port.postMessage(!muted);send({type:muted?'input_audio_mute.commit':'input_audio_unmute.commit'});$('mute').textContent=muted?'麦克风关':'麦克风开';listen();};
document.addEventListener('visibilitychange',()=>{if(document.hidden&&active)hangup();});
window.addEventListener('pagehide',()=>{mic?.getTracks().forEach(t=>t.stop());send({type:'session.close'});});


$('refresh-profile').onclick=()=>requestProfile(true);
$('profile-retry').onclick=()=>readProfileTime();
$('profile-continue').onclick=()=>{
 if(!agentConfig||['loading','updating'].includes(profileInfo.state))return;
 const test=profileIntent?.scope===agentConfig.scope&&profileIntent.test;
 if(!profileInfo.updated)storeSetting('voice.profile-skipped:'+agentConfig.scope,'1');
 profileIntent=null;$('settings').close();startCall(Boolean(test),true);
};
$('settings').addEventListener('close',()=>{profileIntent=null;});

function readSetting(key,fallback){try{return localStorage.getItem(key)??fallback;}catch{return fallback;}}
function storeSetting(key,value){try{localStorage.setItem(key,value);}catch{}}
let captionsOn=readSetting('voice.captions','true')==='true';
function renderCaptions(){$('captions-toggle').setAttribute('aria-checked',String(captionsOn));$('caption-content').classList.toggle('hidden',!captionsOn);}
$('captions-toggle').onclick=()=>{captionsOn=!captionsOn;storeSetting('voice.captions',String(captionsOn));renderCaptions();};
$('settings-open').onclick=()=>openSettings();
$('settings-close').onclick=()=>$('settings').close();
$('call-toggle').onclick=()=>{if(stopping)return;if(active||starting||profileGate)hangup();else startCall();};
renderCaptions();controls();

const savedVoice=readSetting('voice.selected','zh_female_vv_jupiter_bigtts');
if([...$('voice-select').options].some(o=>o.value===savedVoice))$('voice-select').value=savedVoice;
$('voice-select').onchange=()=>storeSetting('voice.selected',$('voice-select').value);

function renderProfileTime(value){
 const date=value?new Date(value):null;
 $('profile-updated').textContent=date&&!Number.isNaN(date.getTime())?'上次更新：'+date.toLocaleString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}):'上次更新：尚未更新';
}
function renderProfilePanel(){
 const info=profileInfo,configured=agentConfig&&info.scope===agentConfig.scope;
 const pending=configured&&['loading','updating'].includes(info.state);
 $('refresh-profile').disabled=!configured||pending;
 $('refresh-profile').textContent=info.state==='updating'?'正在对齐…':info.updated?'更新助手信息':'对齐助手信息';
 $('profile-state').textContent=configured?info.message:'先保存 being 链接，再对齐助手信息。';
 renderProfileTime(configured?info.updated:null);
 if(configured&&!info.updated&&['unknown','loading','error'].includes(info.state))$('profile-updated').textContent=info.state==='loading'?'正在读取更新时间…':'上次更新：暂未确认';
 $('profile-retry').classList.toggle('hidden',!configured||info.state!=='error');
 $('profile-continue').classList.toggle('hidden',!configured||active||starting||stopping);
 $('profile-continue').disabled=pending;
 $('profile-continue').textContent=info.updated?'开始通话':'先跳过，直接通话';
}
function requestProfile(refresh=false){
 const config=agentConfig;
 if(!config){renderProfilePanel();return Promise.resolve(false);}
 if(profilePending?.scope===config.scope)return profilePending.promise;
 if(profilePending)profilePending.cancel();
 if(profileInfo.scope!==config.scope)profileInfo={scope:config.scope,state:'unknown',updated:null,message:''};
 profileInfo.state=refresh?'updating':'loading';profileInfo.message=refresh?'正在向 Agent 对齐身份、称呼和偏好，可能需要几十秒…':'正在读取已保存的助手信息…';renderProfilePanel();
 const operation={scope:config.scope,promise:null,cancel:()=>{}};profilePending=operation;
 operation.promise=new Promise(resolve=>{
  let socket,settled=false;
  const finish=(error,updated)=>{
   if(settled)return;settled=true;clearTimeout(timeout);
   if(socket){socket.onopen=socket.onmessage=socket.onclose=socket.onerror=null;socket.close();}
   if(profilePending===operation)profilePending=null;
   if(agentConfig?.scope===config.scope){
    profileInfo.state=error?'error':updated?'ready':'missing';
    if(!error)profileInfo.updated=updated;
    profileInfo.message=error||(updated?(refresh?(active?'资料已保存，下次通话生效。':'对齐完成，开始通话即可生效；以后直接复用。'):'已保存身份、称呼和偏好。通话直接复用，不会重新查询。'):'首次使用：建议先对齐助手身份、称呼和偏好，也可以跳过。仅在点击对齐后向 Agent 获取。');
    renderProfilePanel();
   }
   resolve(!error);
  };
  operation.cancel=()=>finish('读取已取消，可重新读取状态。');
  const timeout=setTimeout(()=>finish(refresh?'未能确认更新结果，已有资料仍保留。请重新读取状态。':'暂时无法读取资料状态，可重试或先通话。'),refresh?60000:10000);
  try{
   socket=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/pipeline/ws');
   socket.onopen=()=>{if(agentConfig?.scope!==config.scope){operation.cancel();return;}socket.send(JSON.stringify({type:refresh?'profile.refresh':'profile.status',agent_url:config.url}));};
   socket.onmessage=e=>{try{
    if(agentConfig?.scope!==config.scope){operation.cancel();return;}
    const d=JSON.parse(e.data);
    if(d.type==='error'){finish('助手信息暂时无法更新或读取，已有资料仍保留。请稍后重试。');return;}
    if(d.type!==(refresh?'profile.updated':'profile.status'))return;
    if(d.updated_at!==null&&(typeof d.updated_at!=='string'||!Number.isFinite(Date.parse(d.updated_at))))throw new Error();
    if(refresh&&!d.updated_at)throw new Error();
    finish(null,d.updated_at);
   }catch{finish('助手资料返回异常，可重新读取状态。');}};
   socket.onerror=socket.onclose=()=>finish('助手信息连接中断，已有资料仍保留。可重试或先通话。');
  }catch{finish('助手信息连接失败，可重试或先通话。');}
 });
 return operation.promise;
}
function readProfileTime(){return requestProfile(false);}

function openSettings(readProfile=true){
 $('agent-url').value=agentConfig?.url||'';
 $('agent-save').disabled=active||starting||stopping;
 $('agent-url').disabled=active||starting||stopping;
 $('agent-config-state').textContent=active||starting?'结束通话后可更换地址':agentConfig?'地址已保存在此浏览器，token 已隐藏':'首次使用：粘贴含 token 的助手链接，自动解析 streaming 接口';
 if(!$('settings').open)$('settings').showModal();
 if(readProfile)readProfileTime();else renderProfilePanel();
}
$('agent-save').onclick=async()=>{
 if(active||starting||stopping)return;
 const button=$('agent-save'),label=$('agent-config-state'),url=$('agent-url').value.trim();
 if(!url){label.textContent='请先粘贴助手链接';return;}
 button.disabled=true;label.textContent='正在验证 Agent token…';
 try{
  const result=await new Promise((resolve,reject)=>{
   const socket=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/pipeline/ws');let settled=false;
   const finish=(error,data)=>{if(settled)return;settled=true;clearTimeout(timer);socket.close();error?reject(error):resolve(data);};
   const timer=setTimeout(()=>finish(new Error('保存超时，请重试')),10000);
   socket.onopen=()=>socket.send(JSON.stringify({type:'agent.configure',agent_url:url}));
   socket.onmessage=e=>{const d=JSON.parse(e.data);if(d.type==='agent.configured')finish(null,d);else if(d.type==='error')finish(new Error(d.message));};
   socket.onerror=()=>finish(new Error('连接失败，请重试'));
   socket.onclose=()=>finish(new Error('连接中断，请重试'));
  });
  await historyReady;
  const config={url,scope:result.scope};localStorage.setItem('voice.agent',JSON.stringify(config));
  if(profileGate){++callGeneration;profileGate=false;}agentConfig=config;taskRows.clear();renderTasks();$('task-detail').classList.add('hidden');taskSelection=null;
  if(result.legacy&&historyDB){const tx=historyDB.transaction('messages','readwrite');for(const row of historyRows.values()){if(!row.scope){row.scope=result.scope;tx.objectStore('messages').put(row);}}tx.onerror=historyFailure;}
  $('agent-results').textContent='';$('heard').textContent='—';$('answer').textContent='';renderHistory();
  label.textContent='已保存 · '+result.endpoint+'（连接通话时使用）';
  profileIntent=null;await readProfileTime();
  if(profileInfo.state==='missing')$('profile-panel').scrollIntoView({block:'center'});
 }catch(e){label.textContent=e.message||'保存失败，请重试';}
 finally{button.disabled=false;}
};
if(!agentConfig)openSettings();

const taskRows=new Map();let taskOffset=0,taskSelection=null,taskNext=null,taskPageOffset=0;
const taskStates={running:'查询中',completed:'查询已返回',error:'查询未完整返回',interrupted:'查询连接已中断',waiting:'等待结果',pending:'等待报告',reporting:'正在报告',paused:'报告已暂停',asking:'正在询问是否继续',awaiting_choice:'等待你决定是否继续',reported:'报告已读完',deferred:'暂不报告'};
function renderTasks(){
 const box=$('tasks-list');box.textContent='';
 for(const row of taskRows.values()){
  const card=document.createElement('div');card.className='card';const title=document.createElement('div');title.textContent=row.title;
  const state=document.createElement('div');state.className='note';state.textContent=(taskStates[row.state]||row.state)+' · '+(taskStates[row.delivery]||row.delivery)+' · '+new Date(row.created*1000).toLocaleString();
  const button=document.createElement('button');button.className='setting-button';button.textContent='查看完整结果';button.onclick=()=>getTask(row.id,0);card.append(title,state,button);box.append(card);
 }
}
function taskRequest(data){return new Promise((resolve,reject)=>{
 if(!agentConfig){reject(new Error('请先设置 being链接'));return;}
 const scope=agentConfig.scope,socket=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/pipeline/ws');let settled=false;
 const done=(error,value)=>{if(settled)return;settled=true;clearTimeout(timer);socket.close();error?reject(error):resolve(value);};
 const timer=setTimeout(()=>done(new Error('读取任务超时，请重试')),15000);
 socket.onopen=()=>socket.send(JSON.stringify({...data,agent_url:agentConfig.url}));
 socket.onmessage=e=>{const d=JSON.parse(e.data);if(scope!==agentConfig?.scope){done(new Error('being链接已切换'));return;}if(d.type==='tasks.result')done(null,d);else if(d.type==='error')done(new Error(d.message));};
 socket.onerror=()=>done(new Error('连接失败'));socket.onclose=()=>done(new Error('连接中断'));
});}
async function readTasks(offset=0){
 try{const d=await taskRequest({type:'tasks.list',offset});taskOffset=offset;taskRows.clear();for(const row of d.tasks)taskRows.set(row.id,row);renderTasks();$('tasks-state').textContent=d.tasks.length?'查询状态与报告进度分别保存，可反复查看':'暂无任务记录';$('tasks-older').disabled=d.tasks.length<20;}
 catch(e){$('tasks-state').textContent=e.message;}
}
async function getTask(id,offset){
 try{const d=await taskRequest({type:'tasks.get',task_id:id,offset});const row=d.task;taskSelection=row.id;taskPageOffset=row.offset;taskNext=row.next_offset;
 $('task-detail').classList.remove('hidden');$('task-detail-title').textContent=row.title+' · '+(taskStates[row.state]||row.state)+' · '+row.offset+'/'+row.total+' 字';$('task-detail-text').textContent=row.text||'尚未收到正文';$('task-prev').disabled=offset===0;$('task-next').disabled=taskNext===null;}
 catch(e){$('tasks-state').textContent=e.message;}
}
$('tasks-refresh').onclick=()=>readTasks(0);$('tasks-older').onclick=()=>readTasks(taskOffset+20);
$('task-panel').ontoggle=()=>{if($('task-panel').open)readTasks(0);};
$('task-prev').onclick=()=>getTask(taskSelection,Math.max(0,taskPageOffset-8000));$('task-next').onclick=()=>{if(taskNext!==null)getTask(taskSelection,taskNext);};
function reportTask(restart){if(!active){$('tasks-state').textContent='先点击听筒开始通话，再继续或重播报告';return;}send({type:'task.report',task_id:taskSelection,restart});$('tasks-state').textContent='已安排，空闲后报告';}
$('task-resume').onclick=()=>reportTask(false);$('task-replay').onclick=()=>reportTask(true);

$('task-pause').onclick=()=>{if(!active){$('tasks-state').textContent='当前未在通话，报告位置已保留';return;}send({type:'task.pause',task_id:taskSelection});};
