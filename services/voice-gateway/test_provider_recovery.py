import asyncio
import json
import unittest
from provider_recovery import RecoveringProvider


class Socket:
    def __init__(self):self.sent=[];self.events=asyncio.Queue();self.closed=False
    async def send(self,raw):self.sent.append(json.loads(raw))
    async def recv(self):return json.dumps({'type':'session.created'})
    async def close(self):self.closed=True;await self.events.put(None)
    async def __aiter__(self):
        while True:
            value=await self.events.get()
            if value is None:return
            yield value


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_phone_reader_continues_new_provider_and_drops_old_audio(self):
        old=Socket();new=Socket();gate=asyncio.Event();opening=asyncio.Event()
        async def connect():opening.set();await gate.wait();return new
        provider=RecoveringProvider(old,connect)
        values=[]
        async def read():
            async for value in provider:values.append(value)
        reader=asyncio.create_task(read())
        try:
            await old.events.put('before');await asyncio.sleep(0)
            recovery=asyncio.create_task(provider.restart({'type':'session.create'},[]))
            await opening.wait()
            await provider.send(json.dumps({'type':'input_audio_buffer.append','audio':'stale'}))
            gate.set();self.assertTrue(await recovery)
            await new.events.put('after');await asyncio.sleep(0)
            self.assertEqual(values,['before','after'])
            self.assertTrue(old.closed);self.assertFalse(reader.done())
            self.assertFalse(any(e.get('audio') for e in old.sent+new.sent))
            self.assertEqual([e['type'] for e in new.sent],['session.create','session.update'])
        finally:await provider.close();await reader

    async def test_failed_handshake_keeps_old_connection_and_closes_candidate(self):
        old=Socket();bad=Socket()
        async def recv():return '{"type":"error"}'
        bad.recv=recv
        async def connect():return bad
        provider=RecoveringProvider(old,connect)
        with self.assertRaises(ValueError):await provider.restart({},[])
        self.assertIs(provider.socket,old);self.assertTrue(bad.closed)
        self.assertFalse(provider.recovering)
        await provider.close()


if __name__=='__main__':unittest.main()
