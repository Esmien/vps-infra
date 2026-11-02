#!/usr/bin/env python3
"""Import every usable node of an upstream subscription into our router.

For each node we create an inbound path on our server (/p<index>) that forwards to
that node as an outbound; the whitelist (RU entry) nodes are NOT relayed — their raw
links are exported separately so clients connect to them directly.

Usage: import_sub.py /root/sub_server.b64
"""
import base64
import json
import os
import re
import shutil
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, "/root/vpn-router")
from probe_sub import build as build_client  # noqa: E402

CONFIG = Path("/root/vpn-router/config.json")
BRAND = Path("/root/vpn-router/brand.json")
RELAY = Path("/root/vpn-router/relay_nodes.json")
WHITE = Path("/root/vpn-router/whitelist_links.json")
BASE_PORT = 10100
WHITELIST_RE = re.compile(os.environ.get("WHITELIST_RE", "Москв|СПБ|Новосиб|Росси|РФ"), re.I)


def decode(raw: bytes) -> str:
    raw = raw.strip()
    try:
        return base64.b64decode(raw + b"=" * (-len(raw) % 4)).decode("utf-8", "ignore")
    except Exception:
        return raw.decode("utf-8", "ignore")


def outbound_from(cfg: dict, tag: str) -> dict:
    ob = dict(cfg["outbounds"][0])
    ob["tag"] = tag
    return ob


def main() -> int:
    text = decode(Path(sys.argv[1]).read_bytes())
    links = [l for l in text.splitlines() if "://" in l]

    shutil.copy2(CONFIG, CONFIG.with_suffix(".json.bak-import"))
    cfg = json.loads(CONFIG.read_text())
    brand = json.loads(BRAND.read_text())

    dead = {int(x) for x in (sys.argv[2].split(",") if len(sys.argv) > 2 else []) if x.strip()}
    relay, white = [], []
    for i, link in enumerate(links):
        if i in dead:
            print(f"skip {i:2} — dead on probe")
            continue
        u = urllib.parse.urlparse(link)
        name = urllib.parse.unquote(u.fragment).strip()
        if u.scheme not in ("vless", "trojan"):
            print(f"skip {i:2} {name[:30]} — protocol {u.scheme} not supported by xray")
            continue
        built = build_client(link, 0)
        if built is None:
            continue
        if WHITELIST_RE.search(name):
            white.append({"index": i, "name": name, "link": link})
            continue
        tag = f"p{i}"
        path = f"/p{i}"
        port = BASE_PORT + i
        if not any(o.get("tag") == tag for o in cfg["outbounds"]):
            cfg["outbounds"].insert(len(cfg["outbounds"]) - 3, outbound_from(built, tag))
        uid = cfg["inbounds"][3]["settings"]["clients"][0]["id"]
        if not any(x.get("tag") == f"in-{tag}" for x in cfg["inbounds"]):
            cfg["inbounds"].append({
                "tag": f"in-{tag}", "listen": "127.0.0.1", "port": port, "protocol": "vless",
                "settings": {"clients": [{"id": uid, "email": f"in-{tag}"}], "decryption": "none"},
                "streamSettings": {"network": "ws", "security": "none", "wsSettings": {"path": path}},
                "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"], "routeOnly": False},
            })
        if not any(r.get("inboundTag") == [f"in-{tag}"] for r in cfg["routing"]["rules"]):
            cfg["routing"]["rules"].append({"type": "field", "inboundTag": [f"in-{tag}"], "outboundTag": tag})
        brand["paths"][f"p{i}"] = path
        brand["upstreams"][path] = port
        relay.append({"index": i, "name": name, "path": path})

    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    BRAND.write_text(json.dumps(brand, ensure_ascii=False, indent=2) + "\n")
    RELAY.write_text(json.dumps(relay, ensure_ascii=False, indent=2) + "\n")
    WHITE.write_text(json.dumps(white, ensure_ascii=False, indent=2) + "\n")
    WHITE.chmod(0o600)
    print(f"relayed: {len(relay)}  whitelist(direct): {len(white)}")
    for w in white:
        print(f"  direct: {w['name'][:40]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
