import random

ACKNOWLEDGEMENTS=('好，我看看。','嗯，我来了解一下。','收到，我看一下。','好，我确认一下。')

def acknowledgement(previous=None):
    return random.choice([text for text in ACKNOWLEDGEMENTS if text!=previous])

def submitted_notice(title):
    # Task title is display data, not an instruction or a success assertion.
    return '我正在查：'+str(title).strip()[:40]+'。'
