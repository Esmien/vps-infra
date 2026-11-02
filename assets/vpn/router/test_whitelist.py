#!/usr/bin/env python3
"""Test whitelist (Глушилки) nodes with alternative credentials.

For every selected link: build an Xray client with the credential swapped for the
given one, connect through it and print the exit IP. Used to check whether the PC
subscription key opens the mobile subscription's whitelist entries.

Usage: test_whitelist.py <sub file> <pc-sub file> <indices> [--mobile]
"""
import base64
import json
import subprocess
import sys
import urllib.parse
from pathlib import Path


def load(path: str) -> str:
    raw = Path(path).read_bytes().strip()
    try:
        return base64.b64decode(raw + b"=" * (-len(raw) % 4)).decode("utf-8", "ignore")
    except Exception:
        return raw.decode("utf-8", "ignore")


def cred_of(text: str, scheme: str) -> str:
    for line in text.splitlines():
        if line.startswith(scheme + "://"):
            u = urllib.parse.urlparse(line)
            if u.hostname != "0.0.0.0":
                return urllib.parse.unquote(u.username or "")
    raise SystemExit(f"no {scheme} credential found")


def build(link: str, cred: str, port: int) -> dict:
    u = urllib.parse.urlparse(link)
    q = dict(urllib.parse.parse_qsl(u.query))
    net, sec = q.get("type", "tcp"), q.get("security", "none")
    sni = q.get("sni") or u.hostname
    stream = {"network": net, "security": sec}
    if sec == "tls":
        stream["tlsSettings"] = {"serverName": sni, "alpn": q.get("alpn", "h2").split(","),
                                 "fingerprint": q.get("fp", "chrome")}
    elif sec == "reality":
        stream["realitySettings"] = {"serverName": sni, "fingerprint": q.get("fp", "chrome"),
                                     "publicKey": q["pbk"], "shortId": q.get("sid", ""),
                                     "spiderX": q.get("spx", "/")}
    if net == "grpc":
        stream["grpcSettings"] = {"serviceName": q.get("serviceName", ""), "multiMode": q.get("mode") == "multi"}
    if u.scheme == "vless":
        out = {"protocol": "vless", "settings": {"vnext": [{"address": u.hostname, "port": u.port,
               "users": [{"id": cred, "encryption": "none", "flow": q.get("flow", "")}]}]},
               "streamSettings": stream}
    else:
        out = {"protocol": "trojan", "settings": {"servers": [{"address": u.hostname, "port": u.port,
               "password": cred}]}, "streamSettings": stream}
    return {"log": {"loglevel": "error"},
            "inbounds": [{"listen": "127.0.0.1", "port": port, "protocol": "socks",
                          "settings": {"udp": False}}],
            "outbounds": [out]}


def main() -> int:
    sub_file, pc_file, idx_arg = sys.argv[1], sys.argv[2], sys.argv[3]
    use_mobile = "--mobile" in sys.argv
    sub_text, pc_text = load(sub_file), load(pc_file)
    links = [l for l in sub_text.splitlines() if "://" in l]
    order = [int(x) for x in idx_arg.split(",")]

    work = Path("/root/probe_wl")
    work.mkdir(exist_ok=True)
    script = ["#!/bin/sh"]
    for n, i in enumerate(order):
        link = links[i]
        scheme = urllib.parse.urlparse(link).scheme
        cred = cred_of(sub_text, scheme) if use_mobile else cred_of(pc_text, scheme)
        port = 19500 + n
        (work / f"w{i}.json").write_text(json.dumps(build(link, cred, port)))
        name = urllib.parse.unquote(urllib.parse.urlparse(link).fragment)[:34]
        script += [f"printf '%2d %-34s ' {i} '{name}'",
                   f"xray run -c {work}/w{i}.json >/dev/null 2>&1 & pid=$!",
                   "sleep 3",
                   f"out=$(curl -sS --max-time 12 --socks5-hostname 127.0.0.1:{port} https://ipinfo.io/json 2>/dev/null | "
                   "python3 -c \"import sys,json;d=json.load(sys.stdin);print(d['ip'],d['country'],d['city'])\" 2>/dev/null)",
                   'echo "${out:-NO RESPONSE}"', "kill $pid 2>/dev/null; wait $pid 2>/dev/null"]

    (work / "run.sh").write_text("\n".join(script) + "\n")
    r = subprocess.run(["sh", str(work / "run.sh")], capture_output=True, text=True, timeout=560)
    print(("mobile creds" if use_mobile else "pc creds") + ":")
    print(r.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
