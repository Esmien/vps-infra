import json
import sys
import time
import urllib.request

import websocket

DEBUG = 'http://127.0.0.1:9222'
token = open('/root/.hermes/cache/scratch/token.txt').read().strip()
url = sys.argv[1]
prompt = sys.argv[2]
tag = sys.argv[3] if len(sys.argv) > 3 else 'run'

req = urllib.request.Request(DEBUG + '/json/new?about:blank', method='PUT')
t = json.load(urllib.request.urlopen(req))
ws = websocket.create_connection(t['webSocketDebuggerUrl'], timeout=60)
i = 0


def send(method, **params):
    global i
    i += 1
    ws.send(json.dumps({'id': i, 'method': method, 'params': params}))
    while True:
        msg = json.loads(ws.recv())
        if msg.get('id') == i:
            if 'error' in msg:
                raise RuntimeError(msg['error'])
            return msg.get('result', {})


def js(expr, aw=False):
    r = send('Runtime.evaluate', expression=expr, returnByValue=True, awaitPromise=aw)
    return r.get('result', {}).get('value')


send('Page.enable')
send('Network.enable')
send('Emulation.setDeviceMetricsOverride', width=1440, height=900, deviceScaleFactor=1, mobile=False)
send('Network.setCookie', name='token', value=token, domain='ai.esmien.ru', path='/',
     secure=True, httpOnly=True, sameSite='Lax')
send('Page.navigate', url='https://ai.esmien.ru/auth')
time.sleep(3)
js('localStorage.setItem("token", %s)' % json.dumps(token))
send('Page.navigate', url=url)
time.sleep(8)
print('url:', js('location.href'), flush=True)

box = js("""(() => { const e = document.querySelector('#chat-input'); if (!e) return null;
  const r = e.getBoundingClientRect(); return {x: r.x + r.width/2, y: r.y + r.height/2, tag: e.tagName}; })()""")
print('input box:', box, flush=True)

send('Input.dispatchMouseEvent', type='mousePressed', x=box['x'], y=box['y'], button='left', clickCount=1)
send('Input.dispatchMouseEvent', type='mouseReleased', x=box['x'], y=box['y'], button='left', clickCount=1)
time.sleep(0.5)
send('Input.insertText', text=prompt)
time.sleep(1)
print('typed value:', repr(js("document.querySelector('#chat-input').value")), flush=True)
print('active el:', js("document.activeElement && (document.activeElement.id || document.activeElement.tagName)"), flush=True)

send('Input.dispatchKeyEvent', type='keyDown', key='Enter', code='Enter', windowsVirtualKeyCode=13, nativeVirtualKeyCode=13)
time.sleep(0.2)
send('Input.dispatchKeyEvent', type='keyUp', key='Enter', code='Enter', windowsVirtualKeyCode=13, nativeVirtualKeyCode=13)
print('after enter, input value:', repr(js("document.querySelector('#chat-input').value")), flush=True)

t0 = time.time()
time.sleep(4)
last = ''
stable = 0
while time.time() - t0 < 150:
    txt = js("(() => { const c = document.querySelector('#messages-container'); return c ? c.innerText : ''; })()") or ''
    grew = len(txt) > len(prompt) + 30
    if grew and txt == last:
        stable += 1
        if stable >= 6:
            break
    else:
        stable = 0
        last = txt
    time.sleep(1)

print('MESSAGES:', last[:600].replace('\n', ' | '), flush=True)
data = send('Page.captureScreenshot', format='png')['data']
import base64
with open('/root/.hermes/cache/scratch/shot-%s.png' % tag, 'wb') as fh:
    fh.write(base64.b64decode(data))
print('shot:', '/root/.hermes/cache/scratch/shot-%s.png' % tag, flush=True)
urllib.request.urlopen(DEBUG + '/json/close/' + t['id']).read()
