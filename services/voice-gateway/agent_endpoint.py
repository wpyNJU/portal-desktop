import hashlib,re
from urllib.parse import urlsplit,urlunsplit,parse_qs,urlencode

def parse_endpoint(value):
    if not isinstance(value,str) or len(value)>4096:raise ValueError('请填写有效的 Agent 链接')
    try:
        u=urlsplit(value.strip());port=u.port
        if u.scheme!='https' or u.hostname!='echo.beings.town' or port not in (None,443) or u.username or u.password or u.fragment:raise ValueError()
        path=u.path.rstrip('/')
        if path.endswith('/api/chat/stream'):path=path[:-16]
        if not re.fullmatch(r'/[A-Za-z0-9_-]+',path):raise ValueError()
        tokens=parse_qs(u.query).get('token',[])
        if len(tokens)!=1 or not re.fullmatch(r'[A-Za-z0-9_-]{8,512}',tokens[0]):raise ValueError()
        endpoint=urlunsplit(('https','echo.beings.town',path+'/api/chat/stream',urlencode({'token':tokens[0]}),''))
        return endpoint,hashlib.sha256(endpoint.encode()).hexdigest()
    except ValueError:raise ValueError('请粘贴含 token 的 echo.beings.town 助手链接或 streaming 接口地址') from None
