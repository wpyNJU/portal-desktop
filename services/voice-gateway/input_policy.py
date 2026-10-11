"""Conservative transcript policy: acknowledgements never authorize work."""
import re

def needs_quick_ack(text):
    """Acknowledge an explicit request, without authorizing or submitting work."""
    return bool(quick_ack_plan(text))


def quick_ack_plan(text,history=()):
    """Conservative transition prediction, never a decision to execute a tool.

    Ambiguous follow-ups and explanations stay with the provider. Context only
    supplies the topic for an explicit request for newer external information.
    """
    if not isinstance(text,str):return None
    if any(word in text for word in ('不要','不用','别查','不查','别帮','不需要','如果','假如','什么意思','怎么查询','如何查询','“','”','「','」','"')):return None
    value=re.sub(r'[\s，。！？、,.!?…]+','',text)
    messages=r'(?:篝火|私信|消息|留言|邮件)'
    progress=r'(?:任务|项目|训练|评测|查询|实验)'
    topic='messages' if re.search(messages,value) else 'progress' if re.search(progress,value) else 'lookup'
    if re.match(r'^(?:那|好|嗯|你|请|麻烦你|现在|再|就)*(?:帮我|给我)?(?:查一下|查下|查一查|查查|查询|查|看看|看一下|找一下|找找|核实)',value):
        return {'topic':topic,'delay':0}
    # Don't treat definitions, advice, quoted requests, or explanations as lookups.
    if any(word in value.replace('怎么样','') for word in ('是什么','怎么','如何','为什么','解释','意思','他说要','她说要')):return None
    fresh=bool(re.search(r'最近|最新|今天|昨天|刚才|现在|当前|后来|后面|又',value))
    inquiry=bool(re.search(r'什么|哪些|有没有|有没|有新|谁|多少|怎样|怎么样|进展|状态|到哪|如何了|了吗|了没|情况|更新',value))
    private_query=(re.search(messages,value) and fresh and inquiry)
    progress_query=(re.search(progress,value) and re.search(r'我们的|我的|那个|这个|刚才|现在|当前',value) and
                    re.search(r'进展|状态|到哪|怎么样|结束了吗|完成了吗|还在|运行',value))
    if private_query or progress_query:return {'topic':topic,'delay':.8}
    # The latest previous user topic matters, not an old task from elsewhere in
    # the history. "后来又说了什么" requests new facts; "具体是什么意思" doesn't.
    previous=[h.get('text','') for h in history if isinstance(h,dict) and h.get('role')=='user']
    if previous and previous[-1]==text:previous.pop()
    last=previous[-1] if previous else ''
    if (re.search(messages,last) and re.match(r'^(?:那|嗯)?(?:他|她|他们|她们|那个人)',value) and
            fresh and inquiry):
        return {'topic':'messages','delay':.8}
    return None


def classify_input(text):
    if not isinstance(text,str):return 'pending'
    normalized=re.sub(r'[\s，。！？、,.!?…]+','',text).lower()
    if not normalized:return 'pending'
    # Negations and quoted/discussed stop words are not stop commands. Restrict
    # this class to utterances which consist of a command and polite fillers.
    command=re.sub(r'^(?:诶|哎|欸|呃|嗯)+','',normalized)
    command=re.sub(r'^(?:你先|请先|先|请|麻烦你)', '',command)
    command=re.sub(r'(?:吧|好吗|可以吗)$','',command)
    if re.fullmatch(r'(?:等一下|等等|等会|等一等|停一下|停下|停止|暂停|停|别说了|不要说了|别读了|不要读了|先别说|先别读)+',command):
        return 'stop'
    if re.fullmatch(r'(?:嗯|嗯嗯|对|对对|对对对|是的|没错|好|好的|哦|啊|ok|okay)',normalized):
        return 'backchannel'
    return 'redirect'


def explicit_campfire_lookup(text):
    """Only unambiguous read-only requests; never route negations or quoted plans."""
    import re
    if not isinstance(text,str):return False
    value=re.sub(r'[\s，。！？、,.!?]','',text)
    return bool(re.fullmatch(
        r'(?:请|麻烦)?(?:你|帮我|给我)?(?:查|查询|查一下|查下|查询下|查询一下|查看|看看|看一下)'
        r'(?:一下)?(?:现在|当前|最新|实时)?(?:的)?篝火(?:上|里|服务)?(?:的)?'
        r'(?:现在|当前|实时)?(?:有什么|有哪些)?(?:最新|新|最近)?(?:的)?消息(?:吗|呢|吧)?', value))
