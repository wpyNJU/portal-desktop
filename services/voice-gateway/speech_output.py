"""One audible owner per call, including provider speech and task reports."""
import asyncio
from collections import deque


class SpeechOutput:
    def __init__(self, scheduler, client):
        self.scheduler = scheduler
        self.client = client
        self.lock = asyncio.Lock()
        self.owner = None
        self.question = None
        self.turn = 0
        self.generation = 0
        self.responses = {}
        self.audio_stream = (None, None)
        self.response_stream = (None, None)
        self.completed_streams = deque()
        self.ended_responses = set()
        self.finished_responses = set()
        self.blocked_questions = set()
        self.blocked_responses = set()
        self.response_done = False
        self.audio_done = False
        self.audio_seen = False

    def identity(self, event):
        response = event.get('response') or {}
        rid = event.get('response_id') or response.get('id')
        question = event.get('question_id') or response.get('question_id')
        if rid:
            question = question or self.responses.get(rid) or self.question
            self.responses.setdefault(rid, question)
        return rid, question or self.question

    def observe(self, event):
        """Attach stream identity before filtering, including for muted responses.

        Doubao sends IDs on audio.started/audio.done, but not on PCM deltas;
        response.done only contains usage. Never infer those frames from the
        latest user turn: they may still belong to a cancelled older response.
        """
        kind = event.get('type', '')
        if not kind.startswith('response.'):
            return event
        rid, question = self.identity(event)
        if kind == 'response.output_audio.delta' and not rid:
            rid, question = self.audio_stream
        elif kind in ('response.done', 'response.canceled') and not rid:
            rid, question = (self.completed_streams[0] if self.completed_streams
                             else self.response_stream)
        if rid:
            self.responses[rid] = question
            if kind == 'response.output_audio.started':
                self.audio_stream = (rid, question)
            if kind == 'response.output_audio.done':
                self.ended_responses.add(rid)
                if rid not in self.finished_responses and not any(item[0] == rid for item in self.completed_streams):
                    self.completed_streams.append((rid, question))
            if kind in ('response.done', 'response.canceled'):
                self.finished_responses.add(rid)
                if kind == 'response.canceled':
                    self.ended_responses.add(rid)
                self.completed_streams = deque(item for item in self.completed_streams if item[0] != rid)
            elif kind not in ('response.output_audio.delta', 'response.output_audio.done', 'response.output_text.done'):
                self.response_stream = (rid, question)
            return {**event, 'response_id': rid, 'question_id': question}
        return event

    async def switch(self, owner):
        # The lock keeps clear-before-audio ordering even across cancelled senders.
        if self.owner and self.owner[0] == 'provider' and self.owner != owner:
            self.blocked_responses.add(self.owner[1])
        self.owner = owner
        self.generation += 1
        self.scheduler.update(playing=False)
        await self.client({'type': 'playback.clear', 'generation': self.generation})

    async def user_started(self, question=None):
        async with self.lock:
            if self.question:
                self.blocked_questions.add(self.question)
            if self.owner and self.owner[0] == 'provider':
                self.blocked_responses.add(self.owner[1])
                if self.owner[2]:
                    self.blocked_questions.add(self.owner[2])
            self.scheduler.interrupt()
            self.turn += 1
            self.question = question or f'local-turn-{self.turn}'
            await self.switch(None)

    async def block(self, event=None, question=None):
        rid, current = self.identity(event or {})
        current = question or current
        if current:
            self.blocked_questions.add(current)
        if rid:
            self.blocked_responses.add(rid)
        async with self.lock:
            if self.owner and self.owner[0] == 'provider' and (
                    self.owner[1] == rid or self.owner[2] == current):
                self.scheduler.update(generating=False)
                await self.switch(None)

    async def begin_scheduled(self, stop):
        async with self.lock:
            if stop.is_set() or self.scheduler.closed:
                return False
            await self.switch(('scheduled', stop))
            return True

    async def scheduled(self, event, stop):
        async with self.lock:
            if stop.is_set() or self.scheduler.closed or self.owner != ('scheduled', stop):
                return False
            if event.get('type') in ('opening.audio', 'agent.audio.delta'):
                self.scheduler.update(playing=True)
            await self.client({**event, 'generation': self.generation})
            return True

    async def clear_scheduled(self, stop):
        async with self.lock:
            if self.owner == ('scheduled', stop):
                await self.switch(None)

    def is_blocked(self, event):
        rid, question = self.identity(event)
        return rid in self.blocked_responses or question in self.blocked_questions

    async def provider(self, event, *, observed=False):
        if not observed:
            event = self.observe(event)
        kind = event.get('type', '')
        if not kind.startswith('response.output_') and kind not in ('response.done', 'response.canceled'):
            return True
        rid, question = self.identity(event)
        if self.is_blocked(event):
            return False
        identity = ('provider', rid or question or f'turn-{self.turn}', question)
        async with self.lock:
            if kind == 'response.output_audio.delta' and (not rid or rid in self.ended_responses):
                return False
            if kind in ('response.output_audio.started', 'response.output_audio.delta', 'response.output_text.delta'):
                if self.owner != identity:
                    # A foreground answer preempts a report; its unplayed cursor is retained.
                    self.scheduler.interrupt()
                    await self.switch(identity)
                    self.response_done = self.audio_done = self.audio_seen = False
                if kind == 'response.output_audio.delta':
                    self.audio_seen = True
                    self.scheduler.update(generating=True, playing=True)
                else:
                    self.scheduler.update(generating=True)
                await self.client({**event, 'generation': self.generation})
                return False  # Already forwarded through the single output lock.
            if self.owner == identity:
                if kind in ('response.done', 'response.canceled'):
                    self.response_done = True
                if kind in ('response.output_audio.done', 'response.canceled'):
                    self.audio_done = True
                if self.response_done and (self.audio_done or not self.audio_seen):
                    self.scheduler.update(generating=False)
            return True

    def playback(self, event):
        generation = event.get('generation')
        if generation is None or generation == self.generation:
            self.scheduler.update(playing=event.get('playing') is True)
