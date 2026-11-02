#!/usr/bin/env python3
"""Probe every node of a subscription: exit IP, geo, and Google's country verdict.

Builds an Xray client config per node from the subscription links, runs it inside
the 3xui container (xray core lives there), and asks Google directly which country
it assigns to the exit (the field that decides Gemini availability).

Usage: probe_sub.py <b64-or-plain sub file> [index,index,...]
"""
import base64
import json
import subprocess
import sys
import urllib.parse
from pathlib import Path

WORK = Path("/root/probe")


def decode(raw: bytes) -> str:
    raw = raw.strip()
    try:
        return base64.b64decode(raw + b"=" * (-len(raw) % 4)).decode("utf-8", "ignore")
    except Exception:
        return raw.decode("utf-8", "ignore")


def build(link: str, socks_port: int) -> dict | None:
    u = urllib.parse.urlparse(link)
    q = dict(urllib.parse.parse_qsl(u.query))
    net = q.get("type", "tcp")
    sec = q.get("security", "none")
    sni = q.get("sni") or q.get("host") or u.hostname
    if u.scheme == "hysteria2":
        return None
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
    elif net == "ws":
        stream["wsSettings"] = {"path": q.get("path", "/"), "host": q.get("host", sni)}
    elif net == "xhttp":
        stream["xhttpSettings"] = {"path": q.get("path", "/"), "host": sni, "mode": q.get("mode", "auto")}
    if u.scheme == "vless":
        out = {"protocol": "vless",
               "settings": {"vnext": [{"address": u.hostname, "port": u.port,
                                       "users": [{"id": urllib.parse.unquote(u.username or ""),
                                                  "encryption": "none", "flow": q.get("flow", "")}]}]},
               "streamSettings": stream}
    elif u.scheme == "trojan":
        out = {"protocol": "trojan",
               "settings": {"servers": [{"address": u.hostname, "port": u.port,
                                         "password": urllib.parse.unquote(u.username or u.password or "")}]},
               "streamSettings": stream}
    else:
        return None
    return {"log": {"loglevel": "error"},
            "inbounds": [{"listen": "127.0.0.1", "port": socks_port, "protocol": "socks",
                          "settings": {"udp": False}}],
            "outbounds": [out]}


def main() -> int:
    src = Path(sys.argv[1])
    text = decode(src.read_bytes())
    links = [l for l in text.splitlines() if "://" in l]
    pick = [int(x) for x in sys.argv[2].split(",")] if len(sys.argv) > 2 else range(len(links))
    names = [urllib.parse.unquote(urllib.parse.urlparse(l).fragment) for l in links]

    WORK.mkdir(exist_ok=True)
    plan = []
    for i in pick:
        cfg = build(links[i], 19400 + i)
        if cfg is None:
            print(f"{i:2} {names[i][:38]:40} skipped (protocol not supported by xray)")
            continue
        (WORK / f"n{i}.json").write_text(json.dumps(cfg))
        plan.append(i)

    
    lines = ["#!/bin/sh"]
    for i in plan:
        p = 19400 + i
        lines.append(f"printf '%2d %-34s ' {i} '{names[i][:34]}'")
        lines.append(f"xray run -c {WORK}/n{i}.json >/dev/null 2>&1 & pid=$!")
        lines.append("sleep 3")
        lines.append(f"ip=$(curl -sS --max-time 12 --socks5-hostname 127.0.0.1:{p} https://ipinfo.io/json 2>/dev/null | "
                     "python3 -c \"import sys,json;d=json.load(sys.stdin);print(d['ip'],d['country'],d['city'])\" 2>/dev/null)")
        lines.append(f"geo=$(python3 /root/vpn-router/gemini_geo_check.py 127.0.0.1:{p} 2>/dev/null | head -1)")
        lines.append('echo "$ip | $geo"')
        lines.append("kill $pid 2>/dev/null; wait $pid 2>/dev/null")
    (WORK / "run.sh").write_text("\n".join(lines) + "\n")
    res = subprocess.run(["sh", str(WORK / "run.sh")], capture_output=True, text=True, timeout=540)
    print(res.stdout)
    if res.returncode:
        print("stderr:", res.stderr[:300])
    return 0


if __name__ == "__main__":
    sys.exit(main())
