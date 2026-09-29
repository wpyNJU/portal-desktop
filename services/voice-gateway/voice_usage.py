"""Numeric provider usage only: never persist dialogue, credentials or audio."""
import contextvars
import json
import logging
import time
from doubao_config import ROOT

call_id = contextvars.ContextVar('usage_call_id', default='unlinked')
PATH = ROOT/'logs/voice-usage.jsonl'


def numeric(value):
    if isinstance(value, dict):
        return {k: numeric(v) for k, v in value.items()
                if isinstance(v, (dict, int, float)) and not isinstance(v, bool)}
    return value


def write(kind, **data):
    try:
        with PATH.open('a', encoding='utf-8') as f:
            f.write(json.dumps({'time': time.time(), 'call_id': call_id.get(),
                                'kind': kind, **data}, ensure_ascii=False) + '\n')
    except OSError:
        logging.warning('voice_usage_write_failed')


class UsageCapture:
    def __init__(self, source):
        self.source = source
        self.seen = set()

    def observe(self, event):
        # response.done is a per-response counter; do not also sum session totals.
        if event.get('type') != 'response.done':
            return
        response = event.get('response') or {}
        usage = response.get('usage')
        if not isinstance(usage, dict):
            return
        identity = response.get('id') or event.get('response_id') or event.get('event_id')
        if identity and identity in self.seen:
            return
        if identity:
            self.seen.add(identity)
        write('usage', source=self.source, usage=numeric(usage))
