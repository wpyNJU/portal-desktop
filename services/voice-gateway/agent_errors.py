"""Public failure reasons are fixed text, never raw credential-bearing exceptions."""
MESSAGES={
    'upstream_interrupted':'Agent 因新消息让位，中断了本次查询，未自动重新提交。',
    'upstream_error':'Agent 返回错误，未收到完整结果。',
    'upstream_http_error':'Agent 接口未接受本次请求。',
    'upstream_protocol_error':'Agent 返回的数据格式不符合预期。',
    'upstream_connection_error':'与 Agent 的网络连接异常，未自动重新提交。',
    'reply_timeout':'等待 Agent 正文超时；请求已经发送，未重复提交。',
    'empty_reply':'Agent 结束了回复，但没有返回正文。',
    'gateway_interrupted':'语音服务中断了结果接收，未自动重新提交。',
    'unexpected_error':'查询异常结束，未收到完整结果。',
}


class AgentFailure(RuntimeError):
    def __init__(self,code):
        self.code=code if code in MESSAGES else 'unexpected_error'
        self.public_message=MESSAGES[self.code]
        super().__init__(self.public_message)
