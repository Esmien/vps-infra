#!/usr/bin/env python3
"""
Open WebUI pending-signup approver for Telegram.

Every TICK_SECONDS reads pending signups straight from webui_db and sends the
owner a Telegram message with two inline buttons. The same process long-polls
getUpdates, so a button press is handled immediately:

  ✅ Принять  -> UPDATE "user" SET role='user' WHERE id=... AND role='pending'
  🗑 Удалить  -> second tap to confirm -> DELETE /api/v1/users/<id> (admin API,
                 so dependent rows and uploaded files are cleaned up too)

Only TG_OWNER may act. State (getUpdates offset + already-notified ids) lives in
STATE_FILE, so a restart neither re-notifies nor replays old button presses.

No message is ever sent to the person who registered.
"""

import html
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

CFG_DIR = "/root/.config/owui-signup-approver"
CFG_FILE = os.path.join(CFG_DIR, "env")
STACK_ENV = "/home/ai_service/ai-stack/ai/.env"
STATE_DIR = "/var/lib/owui-signup-approver"
STATE_FILE = os.path.join(STATE_DIR, "state.json")

UUID_RE = re.compile(r"^[0-9a-fA-F][0-9a-fA-F-]{7,63}$")
log = logging.getLogger("owui-approver")


# --------------------------------------------------------------------------- #
# config / state
# --------------------------------------------------------------------------- #
def read_env_file(path):
    out = {}
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            out[key.strip()] = val.strip().strip('"').strip("'")
    return out


CFG = read_env_file(CFG_FILE)
TG_TOKEN = CFG["TG_TOKEN"]
TG_OWNER = int(CFG["TG_OWNER"])
OWUI_BASE = CFG.get("OWUI_BASE", "http://127.0.0.1:8080").rstrip("/")
PGHOST = CFG.get("PGHOST", "127.0.0.1")
PGPORT = CFG.get("PGPORT", "5432")
PGDATABASE = CFG.get("PGDATABASE", "webui_db")
PGUSER = CFG.get("PGUSER", "webui")
TICK_SECONDS = int(CFG.get("TICK_SECONDS", "120"))

_stack = {}


def stack_secret(name):
    """Read a secret from the ai-stack .env without ever printing it."""
    if not _stack:
        _stack.update(read_env_file(STACK_ENV))
    return _stack[name]


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        st = {}
    st.setdefault("offset", None)
    st.setdefault("notified", {})
    return st


def save_state(state):
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)
    os.chmod(STATE_FILE, 0o600)


# --------------------------------------------------------------------------- #
# postgres (read-only reads + the single role UPDATE)
# --------------------------------------------------------------------------- #
class DbError(RuntimeError):
    pass


def db_query(sql, timeout=30):
    env = dict(os.environ)
    env["PGPASSWORD"] = stack_secret("WEBUI_DB_PASSWORD")
    env["PGCONNECT_TIMEOUT"] = "10"
    proc = subprocess.run(
        ["psql", "-h", PGHOST, "-p", PGPORT, "-U", PGUSER, "-d", PGDATABASE,
         "-X", "-A", "-t", "-F", "\t", "-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True, text=True, env=env, timeout=timeout,
    )
    if proc.returncode != 0:
        raise DbError((proc.stderr or "").strip()[:400])
    out = proc.stdout.strip("\n")
    if not out:
        return []
    return [line.split("\t") for line in out.split("\n")]


def check_uid(uid):
    if not UUID_RE.match(uid or ""):
        raise ValueError("bad user id")
    return uid


def pending_users():
    rows = db_query(
        "select id, coalesce(name,''), email, created_at "
        'from "user" where role = \'pending\' order by created_at'
    )
    return [
        {"id": r[0], "name": r[1], "email": r[2], "created_at": int(r[3] or 0)}
        for r in rows
    ]


def admin_email():
    rows = db_query(
        'select email from "user" where role = \'admin\' order by created_at limit 1'
    )
    if not rows:
        raise DbError("no admin user found")
    return rows[0][0]


# --------------------------------------------------------------------------- #
# Open WebUI admin API (used for reject only)
# --------------------------------------------------------------------------- #
class ApiError(RuntimeError):
    pass


_api_token = {"value": None}


def api(method, path, body=None, token=None, timeout=30):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(OWUI_BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")[:400]
        return exc.code, raw
    except Exception as exc:  # noqa: BLE001 - surfaced to the owner
        raise ApiError(f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}") from None


def admin_token(force=False):
    if _api_token["value"] and not force:
        return _api_token["value"]
    status, payload = api(
        "POST", "/api/v1/auths/signin",
        {"email": admin_email(), "password": stack_secret("UI_PASSWORD")},
    )
    if status != 200 or not isinstance(payload, dict) or not payload.get("token"):
        raise ApiError(f"signin failed: HTTP {status}")
    _api_token["value"] = payload["token"]
    return _api_token["value"]


def delete_user(uid):
    """DELETE via the admin API so chats/files/refs are cleaned up as well."""
    for attempt in (0, 1):
        status, payload = api("DELETE", f"/api/v1/users/{uid}", token=admin_token(force=attempt))
        if status == 401 and attempt == 0:
            continue
        if status == 200 and payload is True:
            return
        raise ApiError(f"delete failed: HTTP {status} {payload if isinstance(payload, str) else ''}".strip())
    raise ApiError("delete failed: unauthorized")


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
class TgError(RuntimeError):
    pass


def tg(method, payload=None, timeout=30):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:400]
        raise TgError(f"{method}: HTTP {exc.code}: {body}") from None
    except Exception as exc:  # noqa: BLE001
        raise TgError(f"{method}: {type(exc).__name__}: {getattr(exc, 'reason', exc)}") from None


def human_age(ts):
    delta = max(0, int(time.time()) - int(ts))
    if delta < 90:
        return "только что"
    if delta < 3600:
        return f"{delta // 60} мин назад"
    if delta < 86400:
        return f"{delta // 3600} ч назад"
    return f"{delta // 86400} дн назад"


def notify_text(u):
    when = time.strftime("%d.%m.%Y %H:%M:%S", time.gmtime(u["created_at"])) + " UTC"
    return (
        "🆕 <b>Новая регистрация</b> в Open WebUI\n\n"
        f"<b>Имя:</b> {html.escape(u['name'] or '—')}\n"
        f"<b>Email:</b> <code>{html.escape(u['email'] or '—')}</code>\n"
        f"<b>ID:</b> <code>{u['id']}</code>\n"
        f"<b>Когда:</b> {when} ({human_age(u['created_at'])})\n\n"
        "Доступ не выдан (роль <code>pending</code>)."
    )


def main_keyboard(uid):
    return {"inline_keyboard": [[
        {"text": "✅ Принять", "callback_data": f"a:{uid}"},
        {"text": "🗑 Удалить", "callback_data": f"r:{uid}"},
    ]]}


def confirm_keyboard(uid):
    return {"inline_keyboard": [[
        {"text": "🗑 Точно удалить", "callback_data": f"r2:{uid}"},
        {"text": "Отмена", "callback_data": f"c:{uid}"},
    ]]}


def reply(chat_id, message_id, text, keyboard=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if keyboard is not None:
        payload["reply_markup"] = keyboard
    try:
        tg("editMessageText", payload)
    except TgError as exc:
        # "message is not modified" / deleted message: nothing to fix
        if "not modified" not in str(exc):
            log.warning("editMessageText failed: %s", exc)


# --------------------------------------------------------------------------- #
# scan + actions
# --------------------------------------------------------------------------- #
def scan(state):
    users = pending_users()
    current = {u["id"] for u in users}
    sent = 0
    for u in users:
        if u["id"] in state["notified"]:
            continue
        try:
            res = tg("sendMessage", {
                "chat_id": TG_OWNER,
                "text": notify_text(u),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": main_keyboard(u["id"]),
            })
        except TgError as exc:
            log.error("notify %s failed: %s", u["email"], exc)
            continue
        state["notified"][u["id"]] = {
            "message_id": res["result"]["message_id"],
            "email": u["email"],
            "ts": int(time.time()),
        }
        sent += 1
        log.info("notified about %s (id=%s, message_id=%s)", u["email"], u["id"], res["result"]["message_id"])

    for uid in [k for k in state["notified"] if k not in current]:
        del state["notified"][uid]
    save_state(state)
    log.info("scan: %d pending, %d new", len(users), sent)
    return sent


def approve(uid):
    check_uid(uid)
    rows = db_query(
        f"update \"user\" set role = 'user' where id = '{uid}' and role = 'pending' "
        "returning coalesce(name, ''), email"
    )
    if not rows:
        return False, "Уже обработано: пользователь не в статусе pending."
    log.info("approved %s (id=%s)", rows[0][1], uid)
    return True, f"✅ Принят: {rows[0][1]}"


def reject(uid):
    check_uid(uid)
    row = db_query(f"select email, role from \"user\" where id = '{uid}'")
    if not row:
        return False, "Уже обработано: пользователя нет в базе."
    delete_user(uid)
    log.info("rejected (deleted) %s (id=%s)", row[0][0], uid)
    return True, f"🗑 Удалён: {row[0][0]}"


def handle_callback(cb, state):
    cb_id = cb.get("id")
    from_id = (cb.get("from") or {}).get("id")
    data = cb.get("data") or ""
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    action, _, uid = data.partition(":")
    log.info("callback: action=%r uid=%s from=%s message_id=%s", action, uid, from_id, message_id)

    if from_id != TG_OWNER or action not in {"a", "r", "r2", "c"}:
        try:
            tg("answerCallbackQuery", {
                "callback_query_id": cb_id,
                "text": "⛔ нет доступа" if from_id != TG_OWNER else "Неизвестное действие",
                "show_alert": True,
            })
        except TgError as exc:
            log.warning("answerCallbackQuery failed: %s", exc)
        log.warning("ignored callback from %s: %r", from_id, data[:40])
        return

    if action == "r":
        log.info("delete confirmation requested for %s", uid)
        try:
            tg("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Подтвердите удаление"})
        except TgError as exc:
            log.warning("answerCallbackQuery failed: %s", exc)
        if chat_id and message_id:
            reply(chat_id, message_id, "🗑 Удалить пользователя?\nДействие необратимо.", confirm_keyboard(uid))
        return

    if action == "c":
        log.info("delete confirmation cancelled for %s", uid)
        try:
            tg("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Отменено"})
        except TgError as exc:
            log.warning("answerCallbackQuery failed: %s", exc)
        if chat_id and message_id:
            meta = state["notified"].get(uid) or {}
            text = notify_text({
                "id": uid,
                "name": "",
                "email": meta.get("email", "—"),
                "created_at": meta.get("ts", int(time.time())),
            })
            reply(chat_id, message_id, text, main_keyboard(uid))
        return

    try:
        tg("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Выполняю…"})
    except TgError as exc:
        log.warning("answerCallbackQuery failed: %s", exc)

    try:
        ok, text = approve(uid) if action == "a" else reject(uid)
    except (DbError, ApiError, ValueError) as exc:
        text, ok = f"⚠️ Ошибка: {exc}", False
        log.error("action %s on %s failed: %s", action, uid, exc)

    if chat_id and message_id:
        reply(chat_id, message_id, text, None if ok else (main_keyboard(uid) if action == "a" else confirm_keyboard(uid)))
    if ok:
        state["notified"].pop(uid, None)
        save_state(state)


# --------------------------------------------------------------------------- #
# main loop
# --------------------------------------------------------------------------- #
def skip_backlog(state):
    """On first start ignore old updates so we never replay stale button presses."""
    res = tg("getUpdates", {"offset": -1, "timeout": 0, "allowed_updates": ["callback_query"]})
    updates = res.get("result") or []
    state["offset"] = (updates[-1]["update_id"] + 1) if updates else 0
    save_state(state)


def run():
    state = load_state()
    if state.get("offset") is None:
        skip_backlog(state)
    next_scan = 0.0
    last_iter = time.monotonic()
    log.info("started: tick=%ss, owner=%s, db=%s", TICK_SECONDS, TG_OWNER, PGDATABASE)
    while True:
        now = time.monotonic()
        gap = now - last_iter
        if gap > 90:
            log.warning("poll loop was stalled for %.0fs", gap)
        last_iter = now
        if time.monotonic() >= next_scan:
            try:
                scan(state)
            except (DbError, TgError) as exc:
                log.error("scan failed: %s", exc)
            next_scan = time.monotonic() + TICK_SECONDS
        try:
            res = tg("getUpdates", {
                "offset": state.get("offset"),
                "timeout": 25,
                "allowed_updates": ["callback_query"],
            }, timeout=40)
        except TgError as exc:
            log.error("getUpdates failed: %s", exc)
            time.sleep(5)
            continue
        updates = res.get("result") or []
        if updates:
            log.info("getUpdates: %d update(s)", len(updates))
        for upd in updates:
            state["offset"] = upd["update_id"] + 1
            if upd.get("callback_query"):
                try:
                    handle_callback(upd["callback_query"], state)
                except Exception as exc:  # noqa: BLE001 - keep the loop alive
                    log.exception("callback handling failed: %s", exc)
        save_state(state)


def main(argv):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "--check":
        for u in pending_users():
            print(u["id"], u["email"], human_age(u["created_at"]))
        print("pending:", len(pending_users()))
        return 0
    if cmd == "--whoami":
        me = tg("getMe")["result"]
        print("bot:", me.get("username"), "id:", me.get("id"))
        return 0
    if cmd in {"--approve", "--reject"}:
        uid = argv[2] if len(argv) > 2 else ""
        ok, text = approve(uid) if cmd == "--approve" else reject(uid)
        print(text)
        return 0 if ok else 1
    if cmd == "--once":
        state = load_state()
        scan(state)
        return 0

    run()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
