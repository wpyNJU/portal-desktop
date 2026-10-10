import { expect, test, vi } from 'vitest';
import { contextWindow, VoiceHistoryStore, type VoiceRecord } from '../desktop/renderer/voice/services/history';

test('task bodies remain stored but do not crowd out conversational context', () => {
  const rows: VoiceRecord[] = [
    { id: 's:user:1', scope: 's', at: 1, role: 'user', text: '刚才那个问题呢' },
    { id: 's:agent:task1', scope: 's', at: 2, role: 'assistant', text: '后台原文'.repeat(1000) },
    { id: 's:voice:1', scope: 's', at: 3, role: 'assistant', text: '需要你提供日志' },
  ];
  expect(contextWindow(rows, 30)).toEqual([
    { role: 'user', text: '刚才那个问题呢' },
    { role: 'assistant', text: '需要你提供日志' },
  ]);
  expect(rows[1].text.length).toBe(4000);
});

test('ordinary messages mentioning tasks and legacy history remain usable', () => {
  expect(contextWindow([{ role: 'user', text: '解释这个agent任务' }])).toEqual([{ role: 'user', text: '解释这个agent任务' }]);
});

// Fake only IndexedDB requests; run the shipped bounded reader and observe exactly
// which values it asks the database to clone, unlike a getAll-based fixture.
function historyReader(entries: { id: string; at: number; text?: string }[]) {
 const reads: string[] = [], requestedDirections: string[] = []; let scans = 0;
 const store = {
  index: (name: string) => { expect(name).toBe('scope-at');return { openKeyCursor: (_range: unknown, direction: string) => {
   requestedDirections.push(direction); const request: any = {}; let index = 0;
   const tick = () => queueMicrotask(() => {
    const item = entries[index++]; if (item) scans++;
    request.result = item ? { primaryKey: item.id, continue: tick } : null; request.onsuccess();
   });tick();return request;
  }}; },
  get: (id: string) => { reads.push(id);const request: any = {};
   queueMicrotask(() => {const row=entries.find(item=>item.id===id)!;if(row.text===undefined)throw new Error('Task body or old body was loaded');
    request.result={...row,scope:'s',role:'assistant'};request.onsuccess();});return request;
  },
 };
 const reader=new VoiceHistoryStore();(reader as any).database=Promise.resolve({transaction:()=>({objectStore:()=>store})});
 return {reader,reads,requestedDirections,scans:()=>scans};
}
test('startup reads only bounded recent dialogue values and never fetches task bodies',async()=>{
 vi.stubGlobal('IDBKeyRange',{bound:vi.fn(()=>({}))});
 try {
  const entries: {id:string;at:number;text?:string}[]=[...Array.from({length:100},(_,i)=>({id:`s:agent:new-${i}`,at:1000-i})),
   {id:'s:voice:new',at:800,text:'新'.repeat(20000)},{id:'s:voice:older',at:700,text:'旧'.repeat(20000)},
   ...Array.from({length:1000},(_,i)=>({id:`s:agent:old-${i}`,at:600-i}))];
  const t=historyReader(entries);const rows=await t.reader.read('s');
  expect(t.reads).toEqual(['s:voice:new','s:voice:older']);expect(t.requestedDirections).toEqual(['prev']);
  expect(rows.map(row=>row.id)).toEqual(['s:voice:older','s:voice:new']);expect(rows.every(row=>row.text.length===12000)).toBe(true);
  expect(entries[100].text?.length).toBe(20000);expect(t.scans()).toBeLessThan(110);
 } finally {vi.unstubAllGlobals();}
});
test('startup key scans and dialogue count remain bounded in huge stored histories',async()=>{
 vi.stubGlobal('IDBKeyRange',{bound:vi.fn(()=>({}))});
 try {
  const taskOnly=historyReader(Array.from({length:10000},(_,i)=>({id:`s:agent:${i}`,at:10000-i})));
  expect(await taskOnly.reader.read('s')).toEqual([]);expect(taskOnly.reads).toEqual([]);expect(taskOnly.scans()).toBeLessThanOrEqual(513);
  const dialogue=historyReader(Array.from({length:10000},(_,i)=>({id:`s:voice:${i}`,at:10000-i,text:'简短对话'})));
  expect(await dialogue.reader.read('s')).toHaveLength(48);expect(dialogue.reads).toHaveLength(48);
 } finally {vi.unstubAllGlobals();}
});
