import { Dialog } from '../shared/components/dialog';
import { useModel } from '../shared/hooks/use-model';
import type { VoiceCall } from './models/call';
import { PhoneIcon, MicIcon } from './components/icons';
import { VOICE_OPTIONS } from '../../shared/voice';

export function VoiceCallDialog({ model, onRetry }: { model: VoiceCall; onRetry: () => void }) {
  const call = useModel(model);
  const clock = `${String(Math.floor(call.elapsed / 60)).padStart(2, '0')}:${String(call.elapsed % 60).padStart(2, '0')}`;
  return <Dialog id="voice-call" className="voice-call" open={call.open} onClose={() => call.dismiss()}
    aria-labelledby="voice-call-name" aria-describedby="voice-call-status">
    <div className="voice-call-heading"><span><span className="voice-call-dot" />语音通话</span>
      <button type="button" className="close" aria-label="关闭并结束通话" title="关闭并结束通话" onClick={() => call.dismiss()} />
    </div>
    <div className="voice-call-person">
      <div className={`voice-call-avatar${call.speaking ? ' speaking' : ''}`} aria-hidden="true">b<span /></div>
      <h2 id="voice-call-name">{call.name}</h2>
      <p id="voice-call-status" role={call.phase === 'error' ? 'alert' : 'status'}>
        {call.phase === 'active' ? (call.muted ? '麦克风已静音' : call.speaking ? '正在说话 · 你可以直接插话' : call.status) : call.status}
      </p>
      <span className="voice-call-time">{call.phase === 'active' || call.elapsed ? clock : '与你的 Being 自然交流'}</span>
    </div>
    {!call.onboarding && <><div className={`voice-call-wave${call.speaking ? ' speaking' : ''}${call.phase === 'connecting' ? ' connecting' : ''}`} aria-hidden="true">
      {[10, 18, 28, 38, 25, 16, 9].map((height, index) => <i key={index} style={{ height, animationDelay: `${index * -.12}s` }} />)}
    </div>
    <section className="voice-call-captions" aria-label="通话字幕" hidden={!call.captions}>
      {call.heard && <p className="voice-call-heard"><span>你</span>{call.heard}</p>}
      {call.answer ? <p>{call.answer}</p> : <p className="voice-call-caption-empty">{call.phase === 'connecting' ? '接通后，直接开口就好' : call.phase === 'active' ? '听见彼此，也留一点安静' : '下次见，随时再聊'}</p>}
    </section>
    <div className="voice-call-controls">
      <div><button type="button" className={`voice-call-control${call.muted ? ' selected' : ''}`} aria-label={call.muted ? '取消静音' : '静音麦克风'}
        aria-pressed={call.muted} disabled={call.phase !== 'active'} onClick={() => call.toggleMute()}><MicIcon muted={call.muted} /></button><span>麦克风</span></div>
      <div><button type="button" className={`voice-call-control primary-call ${call.busy ? 'hangup' : 'redial'}`}
        aria-label={call.busy ? (call.phase === 'connecting' ? '取消连接' : '挂断通话') : '重新呼叫'}
        onClick={() => call.busy ? call.dismiss() : onRetry()}><PhoneIcon hangup={call.busy} /></button><span>{call.busy ? '挂断' : '再聊一次'}</span></div>
      <div><button type="button" className={`voice-call-control${call.captions ? ' selected' : ''}`} aria-label={call.captions ? '关闭字幕' : '显示字幕'}
        aria-pressed={call.captions} onClick={() => call.preferences(call.voice, !call.captions)}><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="3" /><path d="M7 10h4m2 0h4M7 14h10" /></svg></button><span>字幕</span></div>
    </div></>}
    <section className={`voice-call-profile${call.onboarding ? ' onboarding' : ''}`} aria-label="助手信息">
      <div><strong>助手信息</strong><span>{call.profileWorking ? '处理中' : call.profileState === 'error' ? '状态待确认' : call.profileUpdatedAt ? '已对齐' : '尚未对齐'}</span></div>
      <p id="voice-profile-time">{call.profileUpdatedAt ? `上次更新：${new Date(call.profileUpdatedAt).toLocaleString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })}` : call.profileState === 'loading' ? '正在读取更新时间…' : call.profileState === 'error' ? '上次更新：暂未确认' : '上次更新：尚未更新'}</p>
      {(call.onboarding || call.profileWorking || call.profileState === 'error' || call.profileMessage.includes('下次通话')) && <p role="status">{call.profileMessage}</p>}
      <div className="voice-profile-actions">
        <button type="button" disabled={call.profileWorking} onClick={() => void call.updateProfile()}>
          {call.profileState === 'updating' ? '正在对齐…' : call.onboarding ? '对齐并开始通话' : '更新助手信息'}</button>
        {call.profileState === 'error' && <button type="button" onClick={() => void call.readProfile()}>重新读取状态</button>}
        {call.onboarding && <button type="button" disabled={call.profileWorking} onClick={() => void call.skipProfile()}>先跳过，直接通话</button>}
      </div>
    </section>
    <div className="voice-call-footer"><label>{call.busy && !call.onboarding ? '下次通话音色' : '音色'} <select aria-label="通话音色" value={call.voice}
      onChange={event => call.preferences(event.target.value)}>{VOICE_OPTIONS.map(voice => <option key={voice.id} value={voice.id}>{voice.label}</option>)}</select></label>
      <span>{call.cacheNotice || (call.busy ? '随时说，随时停' : '语音记录保存在这台设备')}</span></div>
  </Dialog>;
}
