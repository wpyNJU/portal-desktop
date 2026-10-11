"""Replace a stalled provider session without dropping the phone or its tasks."""
import asyncio
import json
import websockets


class RecoveringProvider:
    def __init__(self,socket,connect):
        self.socket=socket;self.connect=connect;self.lock=asyncio.Lock()
        self.recovering=False;self.closed=False;self.recoveries=0

    async def send(self,raw):
        # Do not queue stale microphone samples across a replacement session.
        if self.recovering and json.loads(raw).get('type')=='input_audio_buffer.append':return
        async with self.lock:await self.socket.send(raw)

    async def recv(self):return await self.socket.recv()

    async def __aiter__(self):
        while not self.closed:
            current=self.socket
            try:
                async for raw in current:
                    if current is not self.socket:break
                    yield raw
            except websockets.ConnectionClosed:
                if current is self.socket:raise
            if current is self.socket:return

    async def restart(self,session,tools):
        if self.recovering or self.recoveries>=2:return False
        self.recovering=True;replacement=None
        try:
            async with self.lock:
                async with asyncio.timeout(12):
                    replacement=await self.connect()
                    await replacement.send(json.dumps(session,ensure_ascii=False))
                    initial=json.loads(await replacement.recv())
                    if initial.get('type')!='session.created':raise ValueError('provider handshake')
                    await replacement.send(json.dumps({'type':'session.update','session':{'tools':tools}},ensure_ascii=False))
                    old=self.socket;self.socket=replacement;self.recoveries+=1
                    await old.close()
            return True
        except BaseException:
            if replacement is not None and replacement is not self.socket:await replacement.close()
            raise
        finally:self.recovering=False

    async def close(self):
        self.closed=True
        await self.socket.close()
