import asyncio,re,threading,json,uuid,time

STREAM_PENDING=object()
from task_store import next_segment

class TaskReports:
    def __init__(self,store,scope,scheduler,client,speak,output=None):
        self.store=store;self.scope=scope;self.scheduler=scheduler;self.client=client;self.speak=speak
        self.jobs={};self.closed=False;self.acks={};self.focus=store.resumable(scope);self.active_key=None
        self.output=output;self.active_stop=None
        self.attempts={}
    async def event(self,key):
        row=self.store.get(self.scope,key);row.pop('scope');row.pop('request',None);row['length']=len(row.pop('text'))
        await self.client({'type':'task.updated','task':row})
    def priority(self,key,phase=1):return (self.store.get(self.scope,key)['requested_at'],phase)
    def start(self,key,restart=False,notice=False,requested=True):
        row=self.store.get(self.scope,key)
        if requested:
            self.store.touch(self.scope,key);self.focus=key
        if key in self.jobs and not self.jobs[key].done():
            if row['delivery'] in ('paused','asking','awaiting_choice'):self.jobs[key].cancel()
            else:return '报告已经在进行或排队'
        if not notice and not row['text'] and row['state']!='running':return '任务尚未返回可报告的正文，请查看任务状态'
        if not notice and not restart and row['state']!='running' and row['cursor']>=len(row['text']):return '这份报告已经读完，如需再次收听请选择从头报告'
        if restart:self.store.update(self.scope,key,cursor=0)
        self.attempts[key]=self.attempts.get(key,0)+1
        announce=row['delivery']!='waiting' or row['cursor']==0 or restart or row['state'] not in ('completed','running')
        self.store.update(self.scope,key,delivery='pending')
        task=asyncio.create_task(self.report(key,announce=announce));self.jobs[key]=task
        return '已安排完整报告，将在空闲时从保存的位置继续'
    async def say(self,key,text,stop,frames=None,end=None):
        async def output(d):
            if stop.is_set():return
            if d.get('type')=='opening.audio':
                if not self.output:self.scheduler.update(playing=True)
                d={'type':'agent.audio.delta','call_id':key,'delta':d['delta']}
            if self.output:await self.output.scheduled(d,stop)
            else:await self.client(d)
        if frames is None:
            ok=await self.speak(text,output,stop,lambda:not self.closed and not stop.is_set())
        else:
            ok=bool(frames)
            for frame in frames:
                if stop.is_set():return False
                await output(frame)
        if not ok or stop.is_set():return False
        token=uuid.uuid4().hex;future=asyncio.get_running_loop().create_future()
        self.prune_acks()
        receipt={'future':future,'key':key,'end':end,'attempt':self.attempts.get(key),
                 'expires':float('inf'),'played':False}
        self.acks[token]=receipt
        try:
            event={'type':'task.segment.end','task_id':key,'token':token}
            if self.output:
                if not await self.output.scheduled(event,stop):return False
            else:await self.client(event)
            await asyncio.wait_for(future,45)
            return not stop.is_set()
        finally:
            # Playback completion and provider speech detection travel on
            # different connections. Accept a short-lived receipt for a segment
            # actually heard before interruption, even if cancellation won first.
            if end is None:self.acks.pop(token,None)
            else:receipt['expires']=time.monotonic()+3
    def prune_acks(self):
        now=time.monotonic()
        self.acks={token:r for token,r in self.acks.items() if r['expires']>now}
    def acknowledge(self,token):
        self.prune_acks();receipt=self.acks.get(token)
        if receipt is None or receipt['played']:return
        future,key,end=receipt['future'],receipt['key'],receipt['end']
        if receipt['attempt']!=self.attempts.get(key):return
        receipt['played']=True
        # Commit in the acknowledgement handler. A user turn may cancel the
        # report coroutine before it resumes from await, even at the final word.
        if end is not None:
            row=self.store.get(self.scope,key)
            if row['cursor']<end:self.store.update(self.scope,key,cursor=end)
        if not future.done():future.set_result(True)
    def unread(self,key):
        row=self.store.get(self.scope,key)
        return row['state']=='running' or row['cursor']<len(row['text'])
    async def report(self,key,announce=True):
        await self.event(key)
        stream_gap=False;yielded=False
        async def prepare(cursor,stop):
            waiting_since=time.monotonic()
            # Only complete clauses are announced while the stream is still running.
            while not self.closed and not stop.is_set():
                row=self.store.get(self.scope,key)
                tail=row['text'][cursor:]
                match=re.search(r'[。！？!?；;\n]',tail[:160])
                if match:end=cursor+match.end()
                elif row['state']!='running':end=min(len(row['text']),cursor+160)
                elif len(tail)>=160:
                    clause=list(re.finditer(r'[，,：:]',tail[:160]))
                    end=cursor+(clause[-1].end() if clause else 160)
                else:
                    if time.monotonic()-waiting_since>=1.2:return STREAM_PENDING
                    await asyncio.sleep(.1);continue
                if end==cursor:return None
                segment=row['text'][cursor:end]
                spoken=re.sub(r'[*#`]+','',segment).strip()
                frames=[]
                if spoken:
                    async def capture(d):
                        if not stop.is_set():frames.append(d)
                    ok=await self.speak(spoken,capture,stop,lambda:not self.closed and not stop.is_set())
                    if not ok:raise RuntimeError('audio unavailable')
                return end,frames
            return None
        async def read(stop):
            nonlocal stream_gap,yielded
            self.active_key=key
            self.active_stop=stop
            if self.output and not await self.output.begin_scheduled(stop):return
            switched=self.focus!=key;self.focus=key
            self.store.update(self.scope,key,delivery='reporting');await self.event(key)
            row=self.store.get(self.scope,key)
            title=row['title']
            intro=f'刚才查的{title}，有内容返回了。' if row['cursor']==0 else f'接着说{title}，从刚才的位置继续。'
            if row['state'] not in ('completed','running'):intro=f'关于{title}，查询没有完整结束，下面是已收到的部分内容。'
            if row['text'] and row['cursor']>=len(row['text']) and row['state'] not in ('completed','running'):intro=f'关于{title}，查询没有完整结束，已收到的内容都已保存，剩余结果还不能确认。'
            if not row['text'] and row['state']!='running':intro=f'刚才查的{title}，没有收到可用正文，任务状态已经保存，没有自动重新提交。'
            initial_state=row['state']
            prepared=None
            try:
                prepared=asyncio.create_task(prepare(row['cursor'],stop))
                if (announce or switched) and not await self.say(key,intro,stop):raise RuntimeError('audio unavailable')
                while not stop.is_set() and not self.closed:
                    # Yield only at an acknowledged sentence boundary. Prefetched
                    # audio is discarded; its unread cursor remains unchanged.
                    if self.scheduler.has_newer(lambda:self.priority(key)):
                        yielded=True;return
                    result=await prepared
                    if result is STREAM_PENDING:
                        stream_gap=True;return
                    if result is None:
                        if initial_state=='running' and self.store.get(self.scope,key)['state']!='completed':
                            if not await self.say(key,f'关于{title}，查询没有完整结束，刚才是已收到的部分，剩余结果还不能确认。',stop):raise RuntimeError('audio unavailable')
                        return
                    end,frames=result
                    # Prepare exactly one following segment while this segment is playing.
                    prepared=asyncio.create_task(prepare(end,stop))
                    if frames and not await self.say(key,'',stop,frames=frames,end=end):raise RuntimeError('audio unavailable')
                    if stop.is_set():return
                    if not frames:self.store.update(self.scope,key,cursor=end)
            finally:
                if prepared:
                    if not prepared.done():prepared.cancel()
                    await asyncio.gather(prepared,return_exceptions=True)
        try:
            delivered=await self.scheduler.deliver(read,priority=lambda:self.priority(key))
            if self.active_key==key:self.active_key=None
            row=self.store.get(self.scope,key)
            if row['delivery']=='deferred':return
            if (stream_gap or yielded) and delivered:
                self.store.update(self.scope,key,delivery='waiting');await self.event(key);return
            # A new question during the final quiet interval must not turn an
            # already acknowledged full report back into an unfinished one.
            complete=(delivered or bool(row['text'])) and row['state']!='running' and row['cursor']>=len(row['text'])
            self.store.update(self.scope,key,delivery='reported' if complete else 'paused');await self.event(key)
            if not complete and not self.closed:
                async def ask(stop):
                    self.active_key=key
                    self.active_stop=stop
                    if self.output and not await self.output.begin_scheduled(stop):return
                    self.focus=key
                    self.store.update(self.scope,key,delivery='asking');await self.event(key)
                    title=self.store.get(self.scope,key)['title']
                    await self.say(key,f'关于{title}，刚才还没讲完，你还要继续听吗？',stop)
                asked=await self.scheduler.deliver(ask,priority=lambda:self.priority(key,0),valid=lambda:self.unread(key))
                if self.active_key==key:self.active_key=None
                if not self.closed:
                    self.store.update(self.scope,key,delivery='reported' if not self.unread(key) else 'awaiting_choice' if asked else 'paused')
                    await self.event(key)
        except asyncio.CancelledError:
            if self.jobs.get(key) is asyncio.current_task() and self.store.get(self.scope,key)['delivery']!='deferred':self.store.update(self.scope,key,delivery='paused')
            raise
        except Exception:
            if self.active_key==key:
                if self.output:await self.output.clear_scheduled(self.active_stop)
                else:await self.client({'type':'playback.clear'});self.scheduler.update(playing=False)
                self.active_key=None
            self.store.update(self.scope,key,delivery='paused');await self.event(key)
            await self.client({'type':'agent.status','text':'报告音频暂时不可用，全文和播放位置已保存，可点击继续报告重试'})
    async def defer(self,key):
        self.store.get(self.scope,key)
        self.store.update(self.scope,key,delivery='deferred')
        if self.active_key==key:
            self.scheduler.interrupt()
            if self.output:await self.output.clear_scheduled(self.active_stop)
            else:await self.client({'type':'playback.clear'});self.scheduler.update(playing=False)
            self.active_key=None
        job=self.jobs.get(key)
        if job and not job.done():
            job.cancel();await asyncio.gather(job,return_exceptions=True)
        self.store.update(self.scope,key,delivery='deferred');await self.event(key)
        return '已暂停报告，完整结果仍保留，随时可以继续或重新报告'
    def busy(self):return any(not t.done() for t in self.jobs.values())
    async def close(self):
        self.closed=True
        for job in self.jobs.values():job.cancel()
        await asyncio.gather(*self.jobs.values(),return_exceptions=True)
