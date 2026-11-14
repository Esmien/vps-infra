#!/usr/bin/env python3
"""SIWC gateway: OpenAI-compatible bridge to the documented
Sign in with ChatGPT (OSS) flow -> https://api.openai.com/v1/responses.

Why: keeps the official route (no backend-api, no Codex impersonation) while
giving LiteLLM / Open WebUI / Hermes a plain /v1/chat/completions endpoint.

Endpoints:
  GET  /v1/models
  POST /v1/responses          (passthrough, store=false forced, stream bridged)
  POST /v1/chat/completions   (translated, stream + non-stream)
  GET  /healthz

Creds: AUTH_FILE (default /root/siwc/auth.json), refreshed in place, 0600.
Nothing secret is ever logged.
"""
import http.client
import itertools
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API_HOST = 'api.openai.com'
TOKEN_URL = 'https://auth.openai.com/api/accounts/oauth/token'
RESOURCE = 'https://api.openai.com/v1'
AUTH_FILE = os.environ.get('SIWC_AUTH_FILE', '/root/siwc/auth.json')
PORT = int(os.environ.get('SIWC_PORT', '4010'))
BIND = [b for b in os.environ.get('SIWC_BIND', '172.17.0.1,127.0.0.1').split(',') if b]
TIMEOUT = int(os.environ.get('SIWC_TIMEOUT', '600'))

LOCK = threading.Lock()


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, file=sys.stderr, flush=True)


def load_creds():
    with open(AUTH_FILE) as fh:
        return json.load(fh)


def save_creds(c):
    tmp = AUTH_FILE + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(c, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, AUTH_FILE)


def refresh(creds):
    form = urllib.parse.urlencode({
        'grant_type': 'refresh_token',
        'refresh_token': creds['refresh_token'],
        'client_id': creds['client_id'],
        'resource': RESOURCE,
    }).encode()
    req = urllib.request.Request(TOKEN_URL, form, {
        'Content-Type': 'application/x-www-form-urlencoded'})
    with urllib.request.urlopen(req, timeout=60) as resp:
        tok = json.load(resp)
    creds = dict(creds)
    creds.update({
        'access_token': tok['access_token'],
        'refresh_token': tok.get('refresh_token', creds['refresh_token']),
        'id_token': tok.get('id_token', creds.get('id_token')),
        'expires_in': tok.get('expires_in'),
        'saved_at': int(time.time()),
    })
    save_creds(creds)
    log('token refreshed')
    return creds


def access_token(force=False):
    with LOCK:
        creds = load_creds()
        expired = int(creds.get('saved_at', 0)) + int(creds.get('expires_in') or 3600) - 120 < time.time()
        if force or expired:
            try:
                creds = refresh(creds)
            except Exception as exc:  # noqa: BLE001
                log('refresh failed:', type(exc).__name__)
                raise
        return creds['access_token']


def upstream(method, path, body=None, token=None, stream=False):
    conn = http.client.HTTPSConnection(API_HOST, timeout=TIMEOUT)
    headers = {'Authorization': 'Bearer ' + (token or access_token()),
               'Content-Type': 'application/json'}
    payload = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, payload, headers)
    return conn, conn.getresponse()


def sse_events(resp):
    for raw in resp:
        line = raw.decode('utf-8', 'ignore').strip()
        if not line.startswith('data:'):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == '[DONE]':
            continue
        try:
            yield json.loads(chunk)
        except Exception:
            continue


FAILED_EVENTS = ('response.failed', 'error')
# События, которые не несут пользовательского вывода: копим поток, пока не начнётся текст,
# чтобы поймать квоту, приходящую уже после response.created/output_item.added.
QUIET_EVENTS = ('response.created', 'response.in_progress', 'response.queued',
                'response.output_item.added', 'response.content_part.added',
                'response.reasoning_summary_part.added')
PEEK_EVENT_LIMIT = 60
FAKE_QUOTA = os.environ.get('SIWC_FAKE_QUOTA') == '1'


class FakeResp:
    """Подмена ответа апстрима для учебного прогона квоты (SIWC_FAKE_QUOTA=1)."""

    def __init__(self, lines):
        self.status = 200
        self._lines = lines

    def __iter__(self):
        return iter(self._lines)

    def read(self):
        return b''.join(self._lines)


def fake_quota_resp():
    evs = [
        {'type': 'response.created', 'response': {'id': 'resp_fake', 'status': 'in_progress'}},
        {'type': 'response.in_progress', 'response': {'id': 'resp_fake'}},
        {'type': 'response.output_item.added',
         'item': {'id': 'msg_fake', 'type': 'message', 'role': 'assistant'}},
        {'type': 'response.failed', 'response': {'id': 'resp_fake', 'status': 'failed',
         'error': {'code': 'usage_limit_reached',
                   'message': 'The ChatGPT user has reached their Subscription Sharing usage limit. '
                              'Ask the user to try again after their usage limit resets or use an API key instead.'}}},
    ]
    return FakeResp([('data: ' + json.dumps(e) + '\n\n').encode() for e in evs])


def event_error_payload(ev):
    """Текст ошибки из события response.failed/error в формате тела HTTP-ошибки."""
    err = (ev.get('response') or {}).get('error') or ev.get('error') or {}
    if isinstance(err, dict):
        msg = err.get('message') or json.dumps(err)[:300]
    else:
        msg = str(err)[:300]
    return {'error': {'message': msg, 'type': 'server_error'}}


def aggregate(events):
    """Fold Responses SSE events into a single response object."""
    out = {'id': None, 'object': 'response', 'created_at': int(time.time()),
           'model': None, 'status': 'completed', 'output': [], 'usage': {}}
    for ev in events:
        t = ev.get('type')
        if t in ('response.created', 'response.completed', 'response.incomplete'):
            r = ev.get('response') or {}
            for k in ('id', 'model', 'created_at', 'status', 'usage', 'output'):
                if k in r and r[k] is not None:
                    if k == 'output' and r[k]:
                        out['output'] = r[k]
                    elif k != 'output':
                        out[k] = r[k]
        elif t == 'response.output_item.done':
            item = ev.get('item') or {}
            if item and all(i.get('id') != item.get('id') for i in out['output']):
                out['output'].append(item)
        elif t == 'error':
            out['status'] = 'failed'
            out['error'] = ev.get('error') or ev
        elif t == 'response.failed':
            r = ev.get('response') or {}
            out['status'] = 'failed'
            out['error'] = r.get('error') or ev.get('error') or r
            if r.get('id'):
                out['id'] = out['id'] or r['id']
    return out


def to_chat_payload(resp_obj, model):
    text_parts, tool_calls = [], []
    for item in resp_obj.get('output') or []:
        if item.get('type') == 'message':
            for part in item.get('content') or []:
                if isinstance(part, dict) and part.get('text'):
                    text_parts.append(part['text'])
        elif item.get('type') == 'function_call':
            tool_calls.append({
                'id': item.get('call_id') or item.get('id') or 'call_' + uuid.uuid4().hex[:20],
                'type': 'function',
                'function': {'name': item.get('name'), 'arguments': item.get('arguments') or '{}'},
            })
    message = {'role': 'assistant', 'content': ''.join(text_parts) or None}
    if tool_calls:
        message['tool_calls'] = tool_calls
    usage = resp_obj.get('usage') or {}
    return {
        'id': 'chatcmpl-' + (resp_obj.get('id') or uuid.uuid4().hex),
        'object': 'chat.completion',
        'created': int(time.time()),
        'model': model,
        'choices': [{'index': 0, 'message': message,
                     'finish_reason': 'tool_calls' if tool_calls else 'stop'}],
        'usage': {
            'prompt_tokens': usage.get('input_tokens', 0),
            'completion_tokens': usage.get('output_tokens', 0),
            'total_tokens': usage.get('total_tokens', 0),
        },
    }


def _image_part(part):
    """Превращает image_url-часть chat-формата в input_image для Responses API."""
    payload = part.get('image_url')
    if payload is None:
        payload = part.get('url') or part.get('image')
    detail = part.get('detail')
    if isinstance(payload, dict):
        detail = detail or payload.get('detail')
        url = payload.get('url')
    else:
        url = payload
    if not isinstance(url, str) or not url:
        return None
    entry = {'type': 'input_image', 'image_url': url, 'detail': detail or 'auto'}
    return entry


def convert_messages(messages):
    items = []
    for msg in messages:
        role = msg.get('role')
        content = msg.get('content')
        images = []
        text = content
        if isinstance(content, list):
            texts = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get('type')
                if ptype in ('text', 'input_text'):
                    texts.append(part.get('text') or '')
                elif ptype in ('image_url', 'input_image'):
                    img = _image_part(part)
                    if img:
                        images.append(img)
            text = '\n'.join(t for t in texts if t)
        if role in ('system', 'developer', 'user'):
            if role == 'user' and images:
                parts = [{'type': 'input_text', 'text': text or ''}] + images
                items.append({'role': 'user', 'content': parts})
            else:
                items.append({'role': 'user' if role == 'user' else 'system', 'content': text or ''})
        elif role == 'assistant':
            if text:
                items.append({'role': 'assistant', 'content': [{'type': 'output_text', 'text': text}]})
            for call in msg.get('tool_calls') or []:
                fn = call.get('function') or {}
                items.append({'type': 'function_call', 'call_id': call.get('id'),
                              'name': fn.get('name'), 'arguments': fn.get('arguments') or '{}'})
        elif role == 'tool':
            items.append({'type': 'function_call_output',
                          'call_id': msg.get('tool_call_id'),
                          'output': text if isinstance(text, str) else json.dumps(text)})
    return items


def convert_tools(tools):
    out = []
    for tool in tools or []:
        fn = tool.get('function') or tool
        if not fn.get('name'):
            continue
        entry = {'type': 'function', 'name': fn['name'],
                 'description': fn.get('description') or '',
                 'parameters': fn.get('parameters') or {'type': 'object', 'properties': {}}}
        if fn.get('strict') and entry['parameters'].get('additionalProperties') is False:
            entry['strict'] = True
        out.append(entry)
    return out


def build_responses_body(body):
    out = {
        'model': body.get('model'),
        'store': False,
        'stream': True,
        'input': convert_messages(body.get('messages') or []),
    }
    tools = convert_tools(body.get('tools'))
    if tools:
        out['tools'] = tools
        choice = body.get('tool_choice')
        if isinstance(choice, str) and choice in ('auto', 'none', 'required'):
            out['tool_choice'] = choice
        elif isinstance(choice, dict):
            name = (choice.get('function') or {}).get('name')
            if name:
                out['tool_choice'] = {'type': 'function', 'name': name}
    limit = body.get('max_completion_tokens') or body.get('max_tokens')
    if limit:
        # upstream rejects values below 16
        out['max_output_tokens'] = max(16, int(limit))
    for src, dst in (('temperature', 'temperature'), ('top_p', 'top_p'),
                     ('parallel_tool_calls', 'parallel_tool_calls')):
        if body.get(src) is not None:
            out[dst] = body[src]
    effort = body.get('reasoning_effort') or (body.get('reasoning') or {}).get('effort')
    if effort:
        out['reasoning'] = {'effort': effort, 'summary': 'auto'}
    return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    # ------------------------------------------------------------------ helpers
    def json_out(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def err(self, code, message, etype='invalid_request_error'):
        self.json_out(code, {'error': {'message': message, 'type': etype, 'code': code}})

    def relay_error(self, status, raw):
        """Проксировать ошибку апстрима, но исчерпанную квоту подписки сделать повторяемой.

        OpenAI отдаёт "ChatGPT user has reached their Subscription Sharing usage limit"
        как 400, а LiteLLM ретраит и уходит на следующую ногу группы только на
        408/409/429/5xx (litellm.should_retry). На 400 цепочка обрывается, и
        order-фолбэк (OpenRouter) не срабатывает. Отдаём такую ошибку как 429.
        """
        try:
            payload = json.loads(raw or b'{}')
        except Exception:
            payload = {'error': {'message': raw[:300].decode('utf-8', 'ignore')}}
        text = json.dumps(payload).lower()
        quota = ('usage limit', 'subscription sharing')
        if status in (400, 403) and any(m in text for m in quota):
            err = payload.get('error')
            if not isinstance(err, dict):
                err = payload['error'] = {'message': str(err)[:300]}
            err['type'] = 'rate_limit_error'
            err['code'] = 429
            status = 429
        return self.json_out(status, payload)

    def body(self):
        n = int(self.headers.get('Content-Length') or 0)
        return json.loads(self.rfile.read(n) or b'{}')

    # ------------------------------------------------------------------- routes
    def do_GET(self):
        if self.path.startswith('/healthz'):
            return self.json_out(200, {'ok': True})
        if self.path.startswith('/v1/models'):
            try:
                conn, resp = upstream('GET', '/v1/models')
                raw = resp.read()
                if resp.status != 200:
                    return self.json_out(resp.status, json.loads(raw or b'{}'))
                data = json.loads(raw)
                models = [{'id': m.get('slug'), 'object': 'model', 'owned_by': 'openai'}
                          for m in data.get('models', []) if m.get('visibility') == 'list']
                return self.json_out(200, {'object': 'list', 'data': models})
            except Exception as exc:  # noqa: BLE001
                log('models error', type(exc).__name__)
                return self.err(502, 'upstream models failed')
        return self.err(404, 'not found')

    def do_POST(self):
        try:
            body = self.body()
        except Exception:
            return self.err(400, 'bad json')
        path = urllib.parse.urlparse(self.path).path
        if path == '/v1/responses':
            return self.handle_responses(body)
        if path == '/v1/chat/completions':
            return self.handle_chat(body)
        return self.err(404, 'not found')

    # ---------------------------------------------------------------- responses
    def handle_responses(self, body):
        log('responses', body.get('model'))
        body = dict(body)
        body.pop('_client_stream', None)
        want_stream = bool(body.get('stream'))
        body['store'] = False
        body['stream'] = True
        try:
            conn, resp = upstream('POST', '/v1/responses', body)
        except Exception as exc:  # noqa: BLE001
            log('responses error', type(exc).__name__)
            return self.err(502, 'upstream failed')
        if resp.status == 401:
            conn.close()
            try:
                conn, resp = upstream('POST', '/v1/responses', body, token=access_token(force=True))
            except Exception:
                return self.err(502, 'auth refresh failed')
        if FAKE_QUOTA:
            resp = fake_quota_resp()
        if resp.status != 200:
            raw = resp.read()
            return self.relay_error(resp.status, raw)

        if not want_stream:
            obj = aggregate(sse_events(resp))
            conn.close()
            if obj.get('status') == 'failed':
                return self.relay_error(
                    400, json.dumps(event_error_payload({'response': obj})).encode())
            return self.json_out(200, obj)

        # Первое событие читаем ДО отправки 200: исчерпанная квота приходит не HTTP-кодом,
        # а событием response.failed внутри потока. Отдав её как 200, мы лишаем LiteLLM
        # ретрая и перехода на order-2 (он видит только BadRequestError). Ошибка может идти
        # и после preamble (response.created/in_progress), поэтому ждём первое событие с
        # содержанием, а не буквально первое.
        head = []
        first = None
        seen = 0
        peek_types = []
        for raw in resp:
            head.append(raw)
            line = raw.decode('utf-8', 'ignore').strip()
            if line.startswith('data:'):
                chunk = line[5:].strip()
                if chunk and chunk != '[DONE]':
                    try:
                        ev = json.loads(chunk)
                    except Exception:
                        ev = None
                    if isinstance(ev, dict) and ev.get('type'):
                        seen += 1
                        peek_types.append(ev['type'])
                        if ev['type'] in FAILED_EVENTS:
                            first = ev
                            break
                        if ev['type'] not in QUIET_EVENTS or seen >= PEEK_EVENT_LIMIT:
                            break
        if first is not None and first.get('type') in FAILED_EVENTS:
            conn.close()
            log('responses failed before stream:', first.get('type'),
                str(event_error_payload(first)['error'].get('message'))[:120])
            return self.relay_error(400, json.dumps(event_error_payload(first)).encode())
        log('peek types:', ','.join(peek_types))

        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True

        terminal_seen = False
        last_id = None
        model = body.get('model')
        count = 0
        try:
            for raw in itertools.chain(head, resp):
                line = raw.decode('utf-8', 'ignore').strip()
                if line.startswith('data:'):
                    chunk = line[5:].strip()
                    if chunk and chunk != '[DONE]':
                        try:
                            ev = json.loads(chunk)
                        except Exception:
                            ev = None
                        if ev:
                            count += 1
                            t = ev.get('type', '')
                            if t == 'response.created':
                                last_id = (ev.get('response') or {}).get('id')
                            if t in ('response.completed', 'response.incomplete', 'response.failed'):
                                terminal_seen = True
                                if t in FAILED_EVENTS:
                                    log('WARN upstream failed mid-stream:',
                                        str(event_error_payload(ev)['error'].get('message'))[:120])
                self.wfile.write(raw)
            if not terminal_seen:
                # Upstream hung up without a terminal event. LiteLLM (and most
                # clients) treat that as a broken stream, so close it cleanly.
                log('WARN upstream ended without terminal event, events=%d model=%s' % (count, model))
                done = {'type': 'response.completed',
                        'response': {'id': last_id or ('resp_' + uuid.uuid4().hex),
                                     'object': 'response', 'status': 'completed',
                                     'created_at': int(time.time()), 'model': model,
                                     'output': [], 'usage': {}}}
                self.wfile.write(('data: ' + json.dumps(done) + '\n\n').encode())
            self.wfile.flush()
        except Exception as exc:  # noqa: BLE001
            log('WARN relay error:', type(exc).__name__)
        finally:
            conn.close()

    # --------------------------------------------------------------------- chat
    def handle_chat(self, body):
        model = body.get('model') or 'gpt-6.1-terra'
        log('chat', model, 'stream' if body.get('stream') else 'sync')
        want_stream = bool(body.get('stream'))
        upstream_body = build_responses_body(body)
        try:
            conn, resp = upstream('POST', '/v1/responses', upstream_body)
        except Exception as exc:  # noqa: BLE001
            log('chat upstream error', type(exc).__name__)
            return self.err(502, 'upstream failed')
        if resp.status == 401:
            conn.close()
            try:
                conn, resp = upstream('POST', '/v1/responses', upstream_body,
                                      token=access_token(force=True))
            except Exception:
                return self.err(502, 'auth refresh failed')
        if FAKE_QUOTA:
            resp = fake_quota_resp()
        if resp.status != 200:
            raw = resp.read()
            conn.close()
            return self.relay_error(resp.status, raw)

        if not want_stream:
            obj = aggregate(sse_events(resp))
            conn.close()
            if obj.get('status') == 'failed':
                return self.relay_error(
                    400, json.dumps(event_error_payload({'response': obj})).encode())
            return self.json_out(200, to_chat_payload(obj, model))

        # Квота подписки приходит событием response.failed внутри 200-потока (иногда после
        # preamble), поэтому до отправки заголовков читаем до первого содержательного события.
        gen = sse_events(resp)
        pre = []
        failed = None
        for ev in gen:
            t = ev.get('type', '')
            if t in FAILED_EVENTS:
                failed = ev
                break
            pre.append(ev)
            if t not in QUIET_EVENTS or len(pre) >= PEEK_EVENT_LIMIT:
                break
        if failed is not None:
            conn.close()
            log('chat failed before stream:', failed.get('type'),
                str(event_error_payload(failed)['error'].get('message'))[:120])
            return self.relay_error(400, json.dumps(event_error_payload(failed)).encode())

        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True

        cid = 'chatcmpl-' + uuid.uuid4().hex
        created = int(time.time())
        sent_role = False
        tool_index = -1
        tool_sent = {}

        def chunk(delta, finish=None):
            payload = {'id': cid, 'object': 'chat.completion.chunk', 'created': created,
                       'model': model,
                       'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
            self.wfile.write(('data: ' + json.dumps(payload) + '\n\n').encode())

        try:
            stream_evs = gen if not pre else itertools.chain(pre, gen)
            for ev in stream_evs:
                t = ev.get('type', '')
                if t == 'response.output_text.delta':
                    if not sent_role:
                        chunk({'role': 'assistant', 'content': ''})
                        sent_role = True
                    chunk({'content': ev.get('delta', '')})
                elif t == 'response.output_item.added':
                    item = ev.get('item') or {}
                    if item.get('type') == 'function_call':
                        tool_index += 1
                        tool_sent[item.get('id')] = tool_index
                        chunk({'tool_calls': [{
                            'index': tool_index, 'id': item.get('call_id') or item.get('id'),
                            'type': 'function',
                            'function': {'name': item.get('name'), 'arguments': ''}}]})
                elif t == 'response.function_call_arguments.delta':
                    idx = tool_sent.get(ev.get('item_id'), 0)
                    chunk({'tool_calls': [{'index': idx,
                                           'function': {'arguments': ev.get('delta', '')}}]})
                elif t == 'response.completed':
                    chunk({}, 'tool_calls' if tool_sent else 'stop')
                elif t in FAILED_EVENTS:
                    log('WARN upstream failed mid-stream:',
                        str(event_error_payload(ev)['error'].get('message'))[:120])
            self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()
        except Exception:
            pass
        finally:
            conn.close()


def serve(host):
    # bind_retry: на загрузке docker0 может ещё не существовать, и 172.17.0.1
    # отвечает EADDRNOTAVAIL. Без повтора поток умирает молча, а LiteLLM потом
    # получает 'Connection refused' на host.docker.internal.
    delay = 1
    for attempt in range(1, 9):
        try:
            srv = ThreadingHTTPServer((host, PORT), Handler)
            break
        except OSError as exc:
            log(f'bind {host}:{PORT} failed ({exc}); retry {attempt} in {delay}s')
            time.sleep(delay)
            delay = min(delay * 2, 10)
    else:
        log(f'giving up on {host}:{PORT}')
        return
    srv.daemon_threads = True
    log('listening on', host, PORT)
    srv.serve_forever()


def main():
    if not os.path.exists(AUTH_FILE):
        log('missing auth file', AUTH_FILE)
        sys.exit(1)
    threads = [threading.Thread(target=serve, args=(h,), daemon=True) for h in BIND]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


if __name__ == '__main__':
    main()
