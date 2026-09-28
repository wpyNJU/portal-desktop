from agent_endpoint import parse_endpoint
x,s=parse_endpoint('https://echo.beings.town/alice/?token=test_token_123')
assert x=='https://echo.beings.town/alice/api/chat/stream?token=test_token_123'
assert parse_endpoint(x)==(x,s)
assert parse_endpoint('https://echo.beings.town/bob/?token=test_token_123')[1]!=s
assert parse_endpoint('https://echo.beings.town/alice/?token=other_token_123')[1]!=s
for value in ['http://echo.beings.town/alice/?token=test_token_123','https://127.0.0.1/alice/?token=test_token_123','https://echo.beings.town/alice/','https://echo.beings.town/alice/?token=test_token_123&token=other_token_123','https://echo.beings.town/alice/../../admin?token=test_token_123']:
 try:parse_endpoint(value)
 except ValueError:pass
 else:raise AssertionError('invalid accepted')
print('PASS: entry/stream URL normalization, account/token isolation, invalid URL rejection')
