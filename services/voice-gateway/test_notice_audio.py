import asyncio
import base64
import threading
import unittest
from notice_audio import NoticeAudioCache
from voice_notices import ACKNOWLEDGEMENTS


class CacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cache=NoticeAudioCache();self.calls=[];self.output=[]
        self.chunks=[base64.b64encode(bytes([n])*4).decode() for n in (1,2)]
        async def synthesize(text,client,stop,valid,*,voice):
            self.calls.append((voice,text))
            for chunk in self.chunks:await client({'type':'opening.audio','delta':chunk})
            return True
        self.synthesize=synthesize
    async def asyncTearDown(self):await self.cache.close()
    async def client(self,event):self.output.append(event['delta'])
    async def speak(self,text=None,voice='voice1',**kwargs):
        return await self.cache.speak(text or ACKNOWLEDGEMENTS[0],kwargs.get('client',self.client),
            kwargs.get('stop',threading.Event()),kwargs.get('valid',lambda:True),voice=voice,
            synthesize=kwargs.get('synthesize',self.synthesize))
    async def test_reuses_complete_audio_only_with_same_voice_and_phrase(self):
        await self.speak();await self.speak()
        self.assertEqual(len(self.calls),1);self.assertEqual(self.output,self.chunks*2)
        await self.speak(voice='voice2');await self.speak(ACKNOWLEDGEMENTS[1])
        self.assertEqual(len(self.calls),3)
    async def test_never_cache_task_text_or_incomplete_synthesis(self):
        await self.speak('用户私有任务结果');await self.speak('用户私有任务结果')
        async def partial(*args,**kwargs):await self.synthesize(*args,**kwargs);return False
        await self.speak(synthesize=partial)
        self.assertEqual(self.cache.frames,{})
        await self.speak();self.assertEqual(len(self.calls),4)
    async def test_interrupt_stops_cached_frames(self):
        await self.speak();stop=threading.Event();heard=[]
        async def interrupt(event):heard.append(event);stop.set()
        self.assertFalse(await self.speak(client=interrupt,stop=stop))
        self.assertEqual(len(heard),1);self.assertEqual(len(self.calls),1)
    async def test_prepare_once_per_voice_without_blocking_caller(self):
        entered=asyncio.Event();release=asyncio.Event();active=0;maximum=0
        async def waiting(*args,**kwargs):
            nonlocal active,maximum
            active+=1;maximum=max(maximum,active);entered.set();await release.wait()
            try:return await self.synthesize(*args,**kwargs)
            finally:active-=1
        self.cache.prepare('voice1',waiting);self.cache.prepare('voice1',waiting)
        await asyncio.wait_for(entered.wait(),.2)
        self.assertFalse(self.cache.warmers['voice1'].done());release.set()
        await self.cache.warmers['voice1']
        self.assertEqual(maximum,2);self.assertEqual(len(self.calls),len(ACKNOWLEDGEMENTS))
        await self.speak();self.assertEqual(len(self.calls),len(ACKNOWLEDGEMENTS))

if __name__=='__main__':unittest.main()
