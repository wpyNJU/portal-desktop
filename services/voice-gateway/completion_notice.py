"""Completion notices must not replay the first stale intermediate update."""
import re

def completion_notice(body):
    # Prefer an explicit final-result section from the source. Do not infer
    # operation success merely from the transport task reaching completed.
    markers=list(re.finditer(r'(?:两句结论|最终结论|最终结果|执行结果)(?:\*\*)?\s*[:：]\s*',body))
    if not markers:return '刚才的任务已返回完整结果，详情已保存，可以继续问我。'
    tail=body[markers[-1].end():].strip()
    tail=re.sub(r'[*#`]+','',tail)
    clauses=re.split(r'(?<=[。！？!?])|\n',tail)
    excerpt=''.join(c.strip() for c in clauses[:2]).strip()
    if not excerpt or len(excerpt)>220:return '刚才的任务已返回完整结果，详情已保存，可以继续问我。'
    return '刚才任务的完整结果：'+excerpt
