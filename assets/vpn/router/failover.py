#!/usr/bin/env python3
"""Failover for the VPN router: keeps the default route on the external FR exit
while it is reachable, falls back to direct (EE) if it stops answering.

State file: /root/vpn-router/failover.state  ({"fails": N, "mode": "fr"|"direct"})
"""
import json
import os
import socket
import subprocess
import sys

CONFIG = "/root/vpn-router/config.json"
STATE = "/root/vpn-router/failover.state"
CONTAINER = "vpn-router"
PROBE_HOST = "main.uimobile3.shop"
PROBE_PORT = 443
FAIL_THRESHOLD = 2   # consecutive failed checks before switching to direct
OK_THRESHOLD = 2     # consecutive successful checks before switching back


def probe(host: str, port: int, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def load_state() -> dict:
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"fails": 0, "oks": 0, "mode": "fr"}


def save_state(state: dict) -> None:
    tmp = STATE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh)
    os.replace(tmp, STATE)


def set_default(target: str) -> bool:
    with open(CONFIG) as fh:
        cfg = json.load(fh)
    changed = False
    for rule in cfg["routing"]["rules"]:
        if rule.get("network") in ("tcp,udp", "tcp", "udp") and rule.get("outboundTag") in ("fr", "direct"):
            if rule["outboundTag"] != target:
                rule["outboundTag"] = target
                changed = True
    if not changed:
        return False
    tmp = CONFIG + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(cfg, fh, indent=1)
    os.replace(tmp, CONFIG)
    subprocess.run(["docker", "restart", CONTAINER], capture_output=True, check=False)
    return True


def main() -> int:
    state = load_state()
    alive = probe(PROBE_HOST, PROBE_PORT)
    if alive:
        state["fails"] = 0
        state["oks"] = state.get("oks", 0) + 1
        if state["mode"] != "fr" and state["oks"] >= OK_THRESHOLD:
            if set_default("fr"):
                state["mode"] = "fr"
                print("exit restored -> fr")
    else:
        state["oks"] = 0
        state["fails"] = state.get("fails", 0) + 1
        if state["mode"] != "direct" and state["fails"] >= FAIL_THRESHOLD:
            if set_default("direct"):
                state["mode"] = "direct"
                print("exit down -> direct")
    save_state(state)
    print(f"probe={'up' if alive else 'down'} mode={state['mode']} fails={state['fails']} oks={state['oks']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
