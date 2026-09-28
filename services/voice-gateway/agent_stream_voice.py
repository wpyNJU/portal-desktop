import asyncio,re,time

class AgentStreamVoice:
    def __init__(self,call_id,client,speak,stop,valid):
        self.call_id=call_id;self.client=client;self.speak=speak;self.stop=stop;self.valid=valid
        self.incoming=asyncio.Queue();self.sentences=asyncio.Queue();self.parts=[];self.last=time.monotonic();self.audio_ok=True;self.had_audio=False
        self.speaking=False;self.audio_interrupted=False
        self.reader=asyncio.create_task(self.read());self.speaker=asyncio.create_task(self.talk())
    def emit(self,text):
        if isinstance(text,str) and text:
            self.parts.append(text);self.last=time.monotonic();self.incoming.put_nowait(text)
    def interrupt_audio(self):
        # A waiting query is not cancelled or silenced merely because the user speaks.
        if not (self.speaking or self.had_audio):return
        self.audio_interrupted=True
        self.speaker.cancel()
        while not self.sentences.empty():self.sentences.get_nowait()
    def enqueue(self,text):
        if not self.audio_interrupted:self.sentences.put_nowait(text)
    async def read(self):
        buffer='';offset=0
        try:
            while True:
                try:item=await asyncio.wait_for(self.incoming.get(),1.2)
                except TimeoutError:
                    if buffer:self.enqueue(buffer);buffer=''
                    continue
                if item is None:break
                await self.client({'type':'agent.text.delta','call_id':self.call_id,'delta':item,'offset':offset})
                offset+=len(item.encode('utf-16-le'))//2
                buffer+=item
                while buffer:
                    match=re.search(r'[。！？!?；;\n]',buffer)
                    end=min(match.end(),48) if match else (48 if len(buffer)>=48 else 0)
                    if not end:break
                    self.enqueue(buffer[:end]);buffer=buffer[end:]
            if buffer:self.enqueue(buffer)
        finally:self.sentences.put_nowait(None)
    async def talk(self):
        while True:
            text=await self.sentences.get()
            if text is None:return
            if self.stop.is_set() or not self.valid():return
            text=re.sub(r'[*#`]+','',text).strip()
            if not text:continue
            self.speaking=True
            try:
                ok=await self.speak(text)
                self.had_audio|=bool(ok);self.audio_ok &= bool(ok)
            finally:self.speaking=False
    async def finish(self):
        self.incoming.put_nowait(None)
        await self.reader
        await self.client({'type':'agent.text.done','call_id':self.call_id})
        if self.audio_interrupted:await asyncio.gather(self.speaker,return_exceptions=True)
        else:await self.speaker
    async def close(self):
        for task in (self.reader,self.speaker):
            if not task.done():task.cancel()
        await asyncio.gather(self.reader,self.speaker,return_exceptions=True)
