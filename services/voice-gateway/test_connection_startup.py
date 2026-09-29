"""Exercise the actual connection/watch loop with a fake provider, no credentials."""
import asyncio
import base64
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

_data = tempfile.TemporaryDirectory()
_previous = os.environ.get('VOICE_DATA_DIR')
os.environ['VOICE_DATA_DIR'] = _data.name
import doubao_server as gateway
from agent_endpoint import parse_endpoint
from background_tasks import BackgroundTasks
from task_store import TaskStore
if _previous is None:
    os.environ.pop('VOICE_DATA_DIR', None)
else:
    os.environ['VOICE_DATA_DIR'] = _previous


class Provider:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.first_audio_updates = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def send(self, raw):
        event = json.loads(raw)
        self.sent.append(event)
        kind = event['type']
        if kind == 'session.create':
            await self.incoming.put({'type': 'session.created'})
        elif kind == 'input_audio_buffer.append':
            self.first_audio_updates = sum(e['type'] == 'session.update' for e in self.sent)
            for event in [
                {'type': 'conversation.item.input_audio_transcription.started', 'item_id': 'q1'},
                {'type': 'conversation.item.input_audio_transcription.completed', 'item_id': 'q1', 'text': '你好'},
                {'type': 'response.output_audio.started', 'question_id': 'q1', 'response_id': 'r1'},
                {'type': 'response.output_audio.delta', 'delta': 'synthetic-audio'},
                {'type': 'response.output_audio.done', 'question_id': 'q1', 'response_id': 'r1'},
                {'type': 'response.done', 'response_id': 'r1'},
            ]:
                await self.incoming.put(event)
        elif kind == 'session.close':
            await self.incoming.put({'type': 'session.closed'})

    async def recv(self):
        return json.dumps(await self.incoming.get())

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.recv()


class Phone:
    def __init__(self, link):
        self.incoming = asyncio.Queue()
        self.incoming.put_nowait({'agent_url': link, 'history': [{'role': 'user', 'text': '合成历史记录。' * 3000}]})
        self.received = []

    async def accept(self):
        pass

    async def receive_json(self):
        return await self.incoming.get()

    async def send_json(self, event):
        self.received.append(event)
        if event['type'] == 'session.created':
            self.incoming.put_nowait({'type': 'input_audio_buffer.append', 'audio': base64.b64encode(bytes(640)).decode()})
        elif event['type'] == 'response.output_audio.delta':
            self.incoming.put_nowait({'type': 'session.close'})

    async def close(self):
        pass


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_audio_does_not_queue_behind_historical_prompt_updates(self):
        link = 'https://echo.beings.town/fixture/?token=test-link-1234'
        scope = parse_endpoint(link)[1]
        store = TaskStore(':memory:')
        for state in ['awaiting_choice'] * 14 + ['paused'] * 26 + ['deferred'] * 4:
            key = store.create(scope, '合成任务', title='测试任务')
            store.update(scope, key, state='completed', delivery=state, text='尚未播完的合成结果。', cursor=2)
        before = list(store.db.execute('SELECT id,text,cursor FROM tasks'))
        manager = BackgroundTasks(store)
        provider = Provider()
        phone = Phone(link)
        try:
            with patch.object(gateway, 'task_store', store), patch.object(gateway, 'background_tasks', manager), \
                 patch.object(gateway, 'bridge', None), patch.object(gateway, 'headers', return_value={}), \
                 patch.object(gateway.websockets, 'connect', return_value=provider), \
                 patch.object(gateway.AgentBridge, 'verify_access', new=AsyncMock()), \
                 self.assertLogs(gateway.logger, level='INFO') as logs:
                await asyncio.wait_for(gateway.connection(phone), 3)
            timing = [line for line in logs.output if 'first_reply ' in line]
            self.assertEqual(len(timing), 1)
            self.assertIn('context_updates=0', timing[0])
            self.assertEqual(provider.first_audio_updates, 0)
            self.assertEqual(sum(e['type'] == 'session.create' for e in provider.sent), 1)
            self.assertFalse(any(e['type'] == 'session.update' for e in provider.sent))
            self.assertEqual(sum(e['type'] == 'task.updated' for e in phone.received), 44)
            self.assertTrue(any(e['type'] == 'response.output_audio.delta' for e in phone.received))
            self.assertEqual([tuple(r) for r in store.db.execute('SELECT id,text,cursor FROM tasks')], [tuple(r) for r in before])
        finally:
            await manager.shutdown()
            store.db.close()


if __name__ == '__main__':
    try:
        unittest.main()
    finally:
        gateway.task_store.db.close()
        _data.cleanup()
