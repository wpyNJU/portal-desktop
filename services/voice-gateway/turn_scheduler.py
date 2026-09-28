import asyncio,time,threading

class TurnScheduler:
    def __init__(self,quiet=1.2):
        self.quiet=quiet;self.user=False;self.generating=False;self.playing=False
        self.changed=time.monotonic();self.lock=asyncio.Lock();self.closed=False
        self.current=None;self.audio_stop=None
    def update(self,**states):
        for key,value in states.items():setattr(self,key,value)
        self.changed=time.monotonic()
    def interrupt(self):
        if self.audio_stop:self.audio_stop.set()
        if self.current and not self.current.done():self.current.cancel()
    async def wait_idle(self):
        while not self.closed:
            if not(self.user or self.generating or self.playing) and time.monotonic()-self.changed>=self.quiet:return True
            await asyncio.sleep(.05)
        return False
    async def deliver(self,action):
        async with self.lock:
            if not await self.wait_idle():return False
            stop=threading.Event();self.audio_stop=stop
            self.current=asyncio.create_task(action(stop))
            try:
                await self.current
                # Synthesis completion is not playback completion.
                await self.wait_idle()
                return not stop.is_set()
            except asyncio.CancelledError:
                if not stop.is_set():raise
                return False
            finally:self.current=None;self.audio_stop=None;self.changed=time.monotonic()
    def close(self):
        self.closed=True;self.interrupt()

def result_notice(text):
    import re
    sentence=re.split(r'(?<=[。！？!?])|\n',text.strip())[0]
    sentence=re.sub(r'[*#`]+','',sentence).strip()
    if 0<len(sentence)<=100:return '刚才的查询有结果了。'+sentence
    return '刚才的查询有结果了，详细内容已经放在字幕里，你想听的时候告诉我。'


class IdleCallTimer:
    def __init__(self,now,timeout=30):
        self.since=now;self.timeout=timeout
    def expired(self,now,*,busy,last_activity):
        self.since=max(self.since,last_activity)
        if busy:self.since=now;return False
        return now-self.since>=self.timeout
