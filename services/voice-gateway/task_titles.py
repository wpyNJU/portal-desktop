"""Short, stable task labels; never replace the original Agent request."""
import re

MAX_TITLE = 24
GENERIC_TITLES = {'查询', '任务', '查询任务', '后台任务', '刚才的查询', '刚才的内容'}


def _clean(value):
    if not isinstance(value, str):
        return ''
    value = re.sub(r'https?://\S+', '链接', value[:12000])
    value = re.sub(r'[\x00-\x1f\x7f*#`<>「」『』“”\[\]]+', ' ', value)
    return re.sub(r'\s+', ' ', value).strip(' ，,。.!！?？:：;；\"\'')


def task_title(message, title=None):
    candidate = _clean(title)
    if not candidate or candidate in GENERIC_TITLES:
        candidate = _clean(message)
        # Strip politeness, but keep dates, targets, actions and negations.
        candidate = re.sub(r'^(?:(?:请问|麻烦|请|帮我|帮忙|给我|查询一下|查询|查一下|查看一下|查看|了解一下|看看|一下)[ ，,]*)+', '', candidate)
    return candidate[:MAX_TITLE].rstrip(' ，,。.!！?？:：;；') or '这件事'
