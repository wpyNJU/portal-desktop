import asyncio
import json
import unittest
from unittest.mock import patch
from being_conversation import BeingConversation,split_reply,MARKER,END
from background_tasks import BackgroundTasks
from task_store import TaskStore


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def test_spliced_inputs_one_reply_no_duplicate_no_repost(self):
        store=TaskStore(':memory:');sent=[];second=asyncio.Event();posts=[]
        class Bridge:
            url='https://fixture.invalid/chat'
            def endpoint(self,path):return path
        class Response:
            headers={'content-type':'text/event-stream'}
            def __init__(self,status=200,body=None):self.status_code=status;self.body=body
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def aread(self):return b'{"spliced":true}'
            def json(self):return {'messages':[]}
            async def aiter_lines(self):
                await second.wait()
                identity={'scene_id':self.body['scene_id'],'stream_id':'shared'}
                raw='评测结果已找到，手机延迟也已定位。'+MARKER+json.dumps({'answered':keys,'pending':[]})+END
                events=[('meta',identity)]
                # Split at every character: control markup must never reach audio.
                for piece in raw:events.append(('content_block_delta',{**identity,'delta':{'text':piece}}))
                events.append(('message_stop',identity))
                for kind,data in events:
                    yield 'event: '+kind;yield 'data: '+json.dumps(data);yield ''
        class Client:
            def __init__(self,**kwargs):pass
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def get(self,*args,**kwargs):return Response()
            def stream(self,method,url,json):
                posts.append(json)
                if len(posts)==2:second.set()
                return Response(200 if len(posts)==1 else 202,json)
        group=BeingConversation(store,'s',Bridge(),client_factory=Client,poll_interval=.01)
        keys=[];manager=BackgroundTasks(store)
        try:
            with patch('being_conversation.BeingConversation',return_value=group):
                for message in ('查评测','查手机延迟'):
                    key,_=manager.submit('s',None,False,message,Bridge(),{'conversation_mode':True})
                    keys.append(key)
                await asyncio.wait_for(asyncio.gather(*list(manager.jobs.values())),2)
            self.assertEqual(len(posts),2)
            self.assertEqual(posts[0]['scene_id'],posts[1]['scene_id'])
            self.assertEqual([store.get('s',k)['state'] for k in keys],['completed','completed'])
            self.assertEqual(store.get('s',keys[0])['reply_task_id'],store.get('s',keys[1])['reply_task_id'])
            self.assertEqual([store.get('s',k)['delivery'] for k in keys],['reported','reported'])
            self.assertEqual(store.page('s',keys[0])['text'],store.page('s',keys[1])['text'])
            rows=store.db.execute("select text from tasks where kind='reply'").fetchall()
            self.assertEqual([r['text'] for r in rows],['评测结果已找到，手机延迟也已定位。'])
            self.assertTrue(group.closed)
        finally:await manager.shutdown();await group.close();store.db.close()

    async def test_unrelated_or_partial_reply_cannot_complete_all_requests(self):
        store=TaskStore(':memory:')
        group=BeingConversation(store,'s',object())
        a=store.create('s','查评测');b=store.create('s','查手机')
        for key in (a,b):group.members[key]={'future':asyncio.get_running_loop().create_future(),'sent':True,'stage':lambda _:None}
        try:
            raw='正在查评测。'+MARKER+json.dumps({'answered':[],'pending':[a,b]})+END
            reply=group.append_reply(('one',0),raw);group.finish_reply(reply,raw)
            self.assertFalse(group.members[a]['future'].done())
            raw='手机已定位。'+MARKER+json.dumps({'answered':[b],'pending':[a]})+END
            reply=group.append_reply(('one',1),raw);group.finish_reply(reply,raw)
            self.assertTrue(group.members[b]['future'].done());self.assertFalse(group.members[a]['future'].done())
            self.assertEqual(store.active_count('s'),2) # Reply rows never inflate request counts.
            raw='未知来源。'+MARKER+json.dumps({'answered':[a,'foreign'],'pending':[]})+END
            reply=group.append_reply(('one',2),raw);group.finish_reply(reply,raw)
            self.assertFalse(group.members[a]['future'].done())
            self.assertNotIn(MARKER,store.get('s',reply)['text'])
        finally:
            for v in group.members.values():v['future'].cancel()
            await group.close();store.db.close()

    def test_footer_split_and_invalid_coverage(self):
        for i in range(1,len(MARKER)):
            self.assertEqual(split_reply('正文'+MARKER[:i])[0],'正文')
        self.assertEqual(split_reply('正文'+MARKER+'broken'+END),('正文',None))
        self.assertEqual(split_reply('正文'+MARKER+json.dumps({'answered':['a'],'pending':['a']})+END),('正文',None))

    async def test_history_takes_over_partial_stream_without_replaying_heard_prefix(self):
        store=TaskStore(':memory:');group=BeingConversation(store,'s',object())
        task=store.create('s','查询')
        group.members[task]={'future':asyncio.get_running_loop().create_future(),'sent':True,'stage':lambda _:None}
        try:
            key=group.append_reply(('stream',0),'已查到。')
            store.update('s',key,cursor=4,delivery='paused')
            raw='已查到。完整结果。'+MARKER+json.dumps({'answered':[task],'pending':[]})+END
            group.snapshots[raw]=key;group.finish_reply(key,raw)
            group.append_reply(('stream',0),'已查到。迟到残片')
            group.finish_reply(key,'已查到。迟到残片',interrupted=True)
            row=store.get('s',key)
            self.assertEqual(row['state'],'completed');self.assertEqual(row['text'],'已查到。完整结果。')
            self.assertEqual(row['cursor'],4);self.assertEqual(row['delivery'],'paused')
            self.assertEqual(store.page('s',task)['text'],row['text'])
            self.assertEqual(len(store.list('s')),1)
        finally:
            for v in group.members.values():v['future'].cancel()
            await group.close();store.db.close()


if __name__=='__main__':unittest.main()
