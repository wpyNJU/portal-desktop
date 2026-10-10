import type { VoiceHistory } from '../../../shared/voice';
export interface VoiceRecord extends VoiceHistory { id: string; scope: string; at: number }
export class VoiceHistoryStore {
  private database?: Promise<IDBDatabase>;
  private open() {
    return this.database ||= new Promise((resolve, reject) => {
      const request = indexedDB.open('portal-voice-history', 2);
      request.onupgradeneeded = () => {
        const store = request.result.objectStoreNames.contains('messages')
          ? request.transaction!.objectStore('messages') : request.result.createObjectStore('messages', { keyPath: 'id' });
        if (!store.indexNames.contains('scope')) store.createIndex('scope', 'scope');
        if (!store.indexNames.contains('scope-at')) store.createIndex('scope-at', ['scope', 'at']);
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  }
  async read(scope: string): Promise<VoiceRecord[]> {
    const db = await this.open();
    return new Promise((resolve, reject) => {
      const transaction = db.transaction('messages');
      const store = transaction.objectStore('messages');
      // Key-only traversal excludes task bodies before structured-cloning their values.
      const request = store.index('scope-at').openKeyCursor(IDBKeyRange.bound([scope, 0], [scope, Number.MAX_SAFE_INTEGER]), 'prev');
      const rows: VoiceRecord[] = []; let scanned = 0, remaining = 24000;
      const done = () => resolve(rows.reverse());
      transaction.onerror = transaction.onabort = () => reject(transaction.error);
      request.onerror = () => reject(request.error);
      request.onsuccess = () => {
        const cursor = request.result;
        if (!cursor || scanned >= 512 || rows.length >= 48 || remaining <= 0) { done(); return; }
        scanned++;
        const key = cursor.primaryKey;
        if (typeof key !== 'string' || key.startsWith(`${scope}:agent:`)) { cursor.continue(); return; }
        const value = store.get(key);
        value.onerror = () => reject(value.error);
        value.onsuccess = () => {
          const row = value.result as VoiceRecord | undefined;
          if (row?.scope === scope && typeof row.text === 'string' && ['user', 'assistant'].includes(row.role)) {
            const text = row.text.slice(-Math.min(12000, remaining));
            if (text) { rows.push({ ...row, text }); remaining -= text.length; }
          }
          cursor.continue();
        };
      };
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
export function contextWindow(rows: (VoiceHistory & Partial<Pick<VoiceRecord, 'id' | 'scope'>>)[], max = 24000) {
  const selected: VoiceHistory[] = [];
  for (let i = rows.length - 1; i >= 0 && max > 0 && selected.length < 500; i--) {
    const row = rows[i];
    // Task bodies remain in local history and the task store. Their bounded
    // task index is injected separately; don't impersonate a spoken reply here.
    if (row.scope && row.id?.startsWith(`${row.scope}:agent:`)) continue;
    const text = row.text.slice(-max); max -= text.length;
    if (text) selected.unshift({ role: rows[i].role, text });
  }
  return selected;
}
