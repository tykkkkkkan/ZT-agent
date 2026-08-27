"""Day5 全流程本地测试：验证 chat 视图"""
import os, json
os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings'
import django
django.setup()

from django.test import RequestFactory
from agent.views import chat, _keyword_match

# Test keyword matching
print('=== keyword_match tests ===')
for msg in ['红虫颗粒多少钱一包', '螺鲤3号库存', '查订单DD20240801', '你好']:
    result = _keyword_match(msg)
    print(f'  {msg} -> {result}')

print()
print('=== full chat tests ===')
factory = RequestFactory()
for msg in ['红虫颗粒多少钱一包', '螺鲤3号库存', '查订单DD20240801', '你好']:
    request = factory.post('/api/agent/chat/',
        data=json.dumps({'message': msg}),
        content_type='application/json')
    response = chat(request)
    data = json.loads(response.content)
    reply = data['reply']
    print(f'  Q: {msg}')
    print(f'  A: {reply}')
    print()
