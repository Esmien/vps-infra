#!/usr/bin/env python3
"""Turnstile gate для регистрации в Open WebUI.

Логика: nginx делает auth_request на этот сервис перед POST /api/v1/auths/signup.
Токен приходит заголовком cf-turnstile-response, проверяется в CF siteverify.

Режимы:
  * ключи не заданы -> 200 на всё (гейт выключен, регистрация работает как раньше);
  * ключи заданы    -> без токена 403, невалидный токен 403;
  * CF недоступен   -> 200 (fail-open: падение внешнего сервиса не должно
                        закрывать регистрацию; бот этим воспользоваться не может).
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ENV_FILE = os.environ.get("TURNSTILE_ENV", "/etc/turnstile-gate.env")
VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
PORT = int(os.environ.get("TURNSTILE_PORT", "8099"))


def load_env(path: str) -> None:
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


def sitekey() -> str:
    return os.environ.get("TURNSTILE_SITEKEY", "").strip()


def secret() -> str:
    return os.environ.get("TURNSTILE_SECRET", "").strip()


def verify(token: str, remote_ip: str) -> bool | None:
    """True/False по ответу CF, None если CF недоступен."""
    data = urllib.parse.urlencode(
        {"secret": secret(), "response": token, "remoteip": remote_ip}
    ).encode()
    req = urllib.request.Request(VERIFY_URL, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            payload = json.load(resp)
    except Exception as exc:  # сеть, таймаут, 5xx
        print(f"siteverify unavailable: {type(exc).__name__}", file=sys.stderr, flush=True)
        return None
    ok = bool(payload.get("success"))
    if not ok:
        print(f"siteverify reject codes={payload.get('error-codes')}", file=sys.stderr, flush=True)
    return ok


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        # auth_request подзапрос приходит как GET, прямой вызов — как POST.
        if self.path == "/verify":
            self._verify()
        elif self.path == "/config":
            self._send(200, {"sitekey": sitekey(), "enabled": bool(sitekey() and secret())})
        elif self.path == "/health":
            self._send(200, {"ok": True, "enabled": bool(sitekey() and secret())})
        else:
            self._send(404, {"ok": False})

    def do_POST(self) -> None:
        if self.path != "/verify":
            self._send(404, {"ok": False})
            return
        self._verify()

    def _verify(self) -> None:
        if not (sitekey() and secret()):
            self._send(200, {"ok": True, "enabled": False})
            return
        token = (self.headers.get("X-Turnstile-Token") or "").strip()
        remote_ip = (self.headers.get("X-Client-IP") or "").strip()
        if not token:
            self._send(403, {"ok": False, "reason": "missing token"})
            return
        result = verify(token, remote_ip)
        if result is None:
            self._send(200, {"ok": True, "degraded": True})
        elif result:
            self._send(200, {"ok": True})
        else:
            self._send(403, {"ok": False, "reason": "invalid token"})

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} {fmt % args}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    load_env(ENV_FILE)
    print(
        f"turnstile-gate on 127.0.0.1:{PORT} enabled={bool(sitekey() and secret())}",
        flush=True,
    )
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
