"""Проверка, что квота подписки, приходящая событием response.failed внутри 200-потока,
превращается в HTTP 429 (иначе LiteLLM не ретраит и не уходит на order-2)."""
import importlib.util
import io
import json

spec = importlib.util.spec_from_file_location("gw", "/root/siwc/gateway.py")
gw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gw)

QUOTA = {"type": "response.failed", "response": {"id": "resp_1", "status": "failed",
         "error": {"code": "usage_limit_reached",
                   "message": "The ChatGPT user has reached their Subscription Sharing usage limit. "
                              "Ask the user to try again after their usage limit resets or use an API key instead."}}}
CREATED = {"type": "response.created", "response": {"id": "resp_2", "status": "in_progress"}}
DELTA = {"type": "response.output_text.delta", "delta": "pong"}
DONE = {"type": "response.completed", "response": {"id": "resp_2", "status": "completed",
        "output": [{"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "pong"}]}], "usage": {}}}
PLAIN_FAIL = {"type": "response.failed", "response": {"status": "failed",
              "error": {"message": "Input must be a list"}}}


class Conn:
    def close(self):
        pass


class Resp:
    def __init__(self, events, status=200):
        self.status = status
        self._lines = [('data: ' + json.dumps(e) + '\n\n').encode() for e in events]
        self._lines.append(b'data: [DONE]\n\n')

    def __iter__(self):
        return iter(self._lines)

    def read(self):
        return b''.join(self._lines)


def make_handler():
    h = object.__new__(gw.Handler)
    h.wfile = io.BytesIO()
    h.status = None
    h.close_connection = False
    h.send_response = lambda code, *a: setattr(h, 'status', code)
    h.send_header = lambda *a: None
    h.end_headers = lambda: None
    return h


def run(method, events, payload, status=200):
    resp = Resp(events, status=status)
    gw.upstream = lambda *a, **k: (Conn(), resp)
    h = make_handler()
    getattr(h, method)(payload)
    return h.status, h.wfile.getvalue().decode('utf-8', 'ignore')


cases = [
    ("responses stream, quota first", 'handle_responses', [QUOTA],
     {'model': 'gpt-6.1-terra', 'stream': True}, 200, 429),
    ("responses stream, quota after preamble", 'handle_responses', [CREATED, QUOTA],
     {'model': 'gpt-6.1-terra', 'stream': True}, 200, 429),
    ("responses stream, quota after item.added", 'handle_responses',
     [CREATED, {'type': 'response.in_progress', 'response': {'id': 'r'}},
      {'type': 'response.output_item.added', 'item': {'id': 'i', 'type': 'message'}}, QUOTA],
     {'model': 'gpt-6.1-terra', 'stream': True}, 200, 429),
    ("responses stream, normal", 'handle_responses', [CREATED, DELTA, DONE],
     {'model': 'gpt-6.1-terra', 'stream': True}, 200, 200),
    ("responses sync, quota", 'handle_responses', [QUOTA],
     {'model': 'gpt-6.1-terra'}, 200, 429),
    ("responses stream, plain fail", 'handle_responses', [PLAIN_FAIL],
     {'model': 'gpt-6.1-terra', 'stream': True}, 200, 400),
    ("chat stream, quota first", 'handle_chat', [QUOTA],
     {'model': 'gpt-6.1-terra', 'stream': True, 'messages': [{'role': 'user', 'content': 'x'}]}, 200, 429),
    ("chat stream, quota after preamble", 'handle_chat', [CREATED, QUOTA],
     {'model': 'gpt-6.1-terra', 'stream': True, 'messages': [{'role': 'user', 'content': 'x'}]}, 200, 429),
    ("chat stream, normal", 'handle_chat', [CREATED, DELTA, DONE],
     {'model': 'gpt-6.1-terra', 'stream': True, 'messages': [{'role': 'user', 'content': 'x'}]}, 200, 200),
    ("chat sync, quota", 'handle_chat', [QUOTA],
     {'model': 'gpt-6.1-terra', 'messages': [{'role': 'user', 'content': 'x'}]}, 200, 429),
    ("chat stream, fail after content", 'handle_chat', [CREATED, DELTA, QUOTA],
     {'model': 'gpt-6.1-terra', 'stream': True, 'messages': [{'role': 'user', 'content': 'x'}]}, 200, 200),
    ("responses http 429 passthrough", 'handle_responses', [],
     {'model': 'gpt-6.1-terra', 'stream': True}, 429, 429),
]

ok = True
for name, method, events, payload, upstream_status, expect in cases:
    status, body = run(method, events, payload, upstream_status)
    passed = status == expect
    extra = ''
    if 'quota' in name and status == 429:
        extra = ' | type=' + str(json.loads(body).get('error', {}).get('type'))
    if 'normal' in name and method == 'handle_chat':
        extra = ' | has_text=' + str('pong' in body)
    ok = ok and passed
    print(('OK  ' if passed else 'FAIL'), name, '->', status, '(expect', str(expect) + ')', extra)
print('ALL PASSED' if ok else 'FAILURES PRESENT')
