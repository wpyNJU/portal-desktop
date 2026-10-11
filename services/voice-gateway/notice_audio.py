"""Bounded cache of public, fixed acknowledgement audio; never cache task text."""
import asyncio
import base64
import binascii
import threading
from voice_notices import ACKNOWLEDGEMENTS


class NoticeAudioCache:
    def __init__(self):
        self.frames={};self.warmers={};self.slots=asyncio.Semaphore(2)

    async def speak(self,text,client,stop,valid,*,voice,synthesize):
        key=(voice,text)
        cached=self.frames.get(key)
        if cached:
            for delta in cached:
                if stop.is_set() or not valid():return False
                await client({'type':'opening.audio','delta':delta})
                await asyncio.sleep(0)  # Allow a new utterance to cancel queued frames.
            return True
        chunks=[];size=0;cacheable=text in ACKNOWLEDGEMENTS
        async def capture(event):
            nonlocal size,cacheable
            if cacheable and event.get('type')=='opening.audio':
                try:
                    raw=base64.b64decode(event['delta'],validate=True)
                    if not raw or len(raw)%4:raise ValueError('Invalid float PCM')
                    size+=len(raw)
                    if size>512000:raise ValueError('Fixed phrase exceeds cache bound')
                    chunks.append(event['delta'])
                except (ValueError,KeyError,TypeError,binascii.Error):cacheable=False;chunks.clear()
            await client(event)
        complete=await synthesize(text,capture,stop,valid,voice=voice)
        if complete and cacheable and chunks and not stop.is_set() and valid():
            # Four supported voices and six fixed phrases bound both memory and
            # synthesis. An interrupted or failed stream is never a cache hit.
            if key in self.frames or len(self.frames)<24:self.frames[key]=tuple(chunks)
        return complete

    def prepare(self,voice,synthesize):
        if voice in self.warmers:return
        async def warm(text):
            async with self.slots:
                if (voice,text) in self.frames:return
                async def discard(event):pass
                await self.speak(text,discard,threading.Event(),lambda:True,voice=voice,synthesize=synthesize)
        async def prepare():
            await asyncio.gather(*(warm(text) for text in ACKNOWLEDGEMENTS),return_exceptions=True)
        self.warmers[voice]=asyncio.create_task(prepare())

    async def close(self):
        for task in self.warmers.values():task.cancel()
        await asyncio.gather(*self.warmers.values(),return_exceptions=True)


shared_cache=NoticeAudioCache()
