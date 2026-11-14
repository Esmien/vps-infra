import json
import os
import sys
import time
import urllib.request

import jwt

cfg = json.load(sys.stdin)
secret = os.environ.get('WEBUI_SECRET_KEY') or os.environ.get('WEBUI_SECRET')
now = int(time.time())
claims = {'id': cfg['uid'], 'session_stamp': cfg['stamp'], 'typ': 'session',
         'mfa_verified': True, 'iat': now, 'exp': now + 1800}
tok = jwt.encode(claims, secret, algorithm=os.environ.get('ALGORITHM', 'HS256'))


def chat(prompt, model):
    body = {'model': model, 'messages': [{'role': 'user', 'content': prompt}],
            'stream': True, 'chat_id': None, 'id': None,
            'session_id': 'e2e-quota-check', 'background_tasks': {}}
    req = urllib.request.Request('http://127.0.0.1:8080/api/chat/completions',
                                 data=json.dumps(body).encode(),
                                 headers={'Authorization': 'Bearer ' + tok,
                                          'Content-Type': 'application/json'})
    t0 = time.time()
    try:
        r = urllib.request.urlopen(req, timeout=240)
        raw = r.read().decode('utf-8', 'ignore')
        dt = time.time() - t0
        text, err = [], None
        for line in raw.splitlines():
            if not line.startswith('data:'):
                continue
            chunk = line[5:].strip()
            if chunk in ('', '[DONE]'):
                continue
            try:
                ev = json.loads(chunk)
            except Exception:
                continue
            if ev.get('error'):
                err = ev['error']
            for ch in (ev.get('choices') or []):
                d = ch.get('delta') or {}
                if d.get('content'):
                    text.append(d['content'])
        out = ''.join(text)
        print('status=%s dt=%.1fs chars=%d err=%s' % (r.status, dt, len(out), str(err)[:200]))
        print('   text=%r' % out[:200])
        return out
    except urllib.error.HTTPError as e:
        print('HTTP', e.code, e.read().decode('utf-8', 'ignore')[:300])
    except Exception as e:  # noqa: BLE001
        print('ERR', type(e).__name__, str(e)[:200])


for i, p in enumerate(['Одним предложением: что такое идемпотентность?',
                       'Назови три отличия TCP от UDP, по строке на каждое.',
                       'Короткое четверостишие про сервер в три часа ночи.']):
    print('--- call', i + 1)
    chat(p, cfg['model'])
