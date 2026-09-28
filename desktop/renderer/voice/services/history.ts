import type { VoiceHistory } from '../../../shared/voice';
export interface VoiceRecord extends VoiceHistory { id: string; scope: string; at: number }
export class VoiceHistoryStore {
  private database?: Promise<IDBDatabase>;
  private open() {
    return this.database ||= new Promise((resolve, reject) => {
      const request = indexedDB.open('portal-voice-history', 1);
      request.onupgradeneeded = () => { request.result.createObjectStore('messages', { keyPath: 'id' }).createIndex('scope', 'scope'); };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  }
  async read(scope: string): Promise<VoiceRecord[]> {
    const db = await this.open();
    return new Promise((resolve, reject) => {
      const request = db.transaction('messages').objectStore('messages').index('scope').getAll(scope);
      request.onsuccess = () => resolve((request.result as VoiceRecord[]).sort((a, b) => a.at - b.at));
      request.onerror = () => reject(request.error);
    });
  }
  async write(row: VoiceRecord) {
    const db = await this.open();
    await new Promise<void>((resolve, reject) => {
      const transaction = db.transaction('messages', 'readwrite');
      transaction.objectStore('messages').put(row);
      transaction.oncomplete = () => resolve();
      transaction.onerror = transaction.onabort = () => reject(transaction.error);
    });
  }
}
export function contextWindow(rows: VoiceHistory[], max = 24000) {
  const selected: VoiceHistory[] = [];
  for (let i = rows.length - 1; i >= 0 && max > 0 && selected.length < 500; i--) {
    const text = rows[i].text.slice(-max); max -= text.length;
    if (text) selected.unshift({ role: rows[i].role, text });
  }
  return selected;
}
