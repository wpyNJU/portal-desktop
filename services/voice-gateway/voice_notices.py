import random

NOTICE_POOLS={
    'lookup':('我来查一下。','我看看相关信息。'),
    'messages':('我来看看消息。','我帮你查一下消息。'),
    'progress':('我看一下进展。','我查一下目前的情况。'),
}
ACKNOWLEDGEMENTS=tuple(text for pool in NOTICE_POOLS.values() for text in pool)

def acknowledgement(previous=None,topic=None):
    pool=NOTICE_POOLS.get(topic,ACKNOWLEDGEMENTS)
    return random.choice([text for text in pool if text!=previous])

def submitted_notice(title):
    # Task title is display data, not an instruction or a success assertion.
    return '我正在查：'+str(title).strip()[:40]+'。'
