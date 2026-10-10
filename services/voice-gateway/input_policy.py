"""Conservative transcript policy: acknowledgements never authorize work."""
import re

def needs_quick_ack(text):
    """Acknowledge an explicit request, without authorizing or submitting work."""
    if not isinstance(text,str):return False
    if any(word in text for word in ('不要','不用','别查','不查','别帮','不需要','如果','假如','怎么','为什么','什么意思','说的是','他说','她说','“','”','「','」','"')):return False
    return bool(re.match(r'^\s*(?:那|好|嗯|你|请|麻烦你|现在|再|就|，|,|\s)*(?:帮我|给我)?(?:查一下|查下|查一查|查查|查询|查|看看|看一下|找一下|找找|核实)',text))


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
