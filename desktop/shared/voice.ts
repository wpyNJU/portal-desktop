export const VOICE_OPTIONS = [
  { id: 'zh_female_vv_jupiter_bigtts', label: '自然女声 · Vivi' },
  { id: 'zh_female_xiaohe_jupiter_bigtts', label: '亲切女声 · 小何' },
  { id: 'zh_male_yunzhou_jupiter_bigtts', label: '沉稳男声 · 云舟' },
  { id: 'zh_male_xiaotian_jupiter_bigtts', label: '清爽男声 · 小天' },
] as const;
export interface VoiceHistory { role: 'user' | 'assistant'; text: string }
export interface VoiceProfile { updatedAt: string | null }
export interface VoiceStart {
  callId: string; endpoint: string; sceneId: string; voice: string; history: VoiceHistory[];
}
export interface VoiceEvent { callId: string; data: { type: string; [key: string]: unknown } }
export type VoiceCommand =
  | { type: 'input_audio_buffer.append'; audio: string }
  | { type: 'input_audio_mute.commit' | 'input_audio_unmute.commit' | 'response.cancel' }
  | { type: 'client.playback'; playing: boolean; generation?: number }
  | { type: 'task.segment.played'; token: string };
export interface VoiceAPI {
  profile(endpoint: string, refresh?: boolean): Promise<VoiceProfile>;
  start(input: VoiceStart): Promise<void>;
  send(callId: string, command: VoiceCommand): Promise<void>;
  stop(callId: string): Promise<void>;
  onEvent(callback: (event: VoiceEvent) => void): () => void;
}
