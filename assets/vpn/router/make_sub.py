#!/usr/bin/env python3
"""Generate the Esmien VPN subscription bundle (clash.yaml / list.txt / singbox.json).

Usage:
    python3 make_sub.py            # rewrite existing token dirs
    python3 make_sub.py --new      # create a new token dir

Routes: /general -> Estonia (direct exit), /france -> France (external exit).
nginx serves https://mon.esmien.qd.je:8443/sub/<token> and picks the format by User-Agent.
"""
import base64
import json
import sys
import urllib.parse
from pathlib import Path

_BRAND = json.loads(Path("/root/vpn-router/brand.json").read_text())
HOST = _BRAND["host"]
PORT = _BRAND["port"]
ROOT = Path("/var/www/esmien-sub")
UID_FILE = Path("/root/mtproto-faketls/ws-client-uuid")

EE = "🇪🇪 Esmien EE (General)"
FR = "🇫🇷 Esmien FR (Gemini)"
TEST = "🛠 Esmien TEST (Develop)"


def vless(uid: str, path: str, tag: str) -> str:
    query = {"type": "ws", "security": "tls", "sni": HOST, "host": HOST, "path": path, "encryption": "none"}
    return f"vless://{uid}@{HOST}:{PORT}?" + urllib.parse.urlencode(query) + "#" + urllib.parse.quote(tag)


def clash_proxy(uid: str, name: str, path: str) -> str:
    return f"""  - name: "{name}"
    type: vless
    server: {HOST}
    port: {PORT}
    uuid: {uid}
    network: ws
    tls: true
    udp: true
    sni: {HOST}
    servername: {HOST}
    client-fingerprint: chrome
    alpn: [http/1.1]
    ws-opts:
      path: "{path}"
      headers:
        Host: {HOST}
"""


def build(token_dir: Path, uid: str) -> None:
    links = [vless(uid, _BRAND["paths"]["general"], EE),
             vless(uid, _BRAND["paths"]["france"], FR),
             vless(uid, _BRAND["paths"]["test"], TEST)]
    (token_dir / "list.txt").write_text(base64.b64encode(("\n".join(links) + "\n").encode()).decode() + "\n")

    (token_dir / "clash.yaml").write_text(f"""# Esmien VPN — 2 маршрута
# Эстония (General) — обычный трафик; Франция (Gemini) — Google (иначе Gemini упирается в страну)
mixed-port: 7890
allow-lan: false
mode: rule
log-level: info
ipv6: false

dns:
  enable: true
  ipv6: false
  enhanced-mode: fake-ip
  fake-ip-range: 198.18.0.1/16
  nameserver: [1.1.1.1, 8.8.8.8]

proxies:
{clash_proxy(uid, EE, _BRAND["paths"]["general"])}{clash_proxy(uid, FR, _BRAND["paths"]["france"])}{clash_proxy(uid, TEST, _BRAND["paths"]["test"])}
proxy-groups:
  - name: "Esmien VPN"
    type: select
    proxies:
      - "{EE}"
      - "{FR}"
      - "{TEST}"

rules:
  - IP-CIDR,127.0.0.0/8,DIRECT,no-resolve
  - IP-CIDR,10.0.0.0/8,DIRECT,no-resolve
  - IP-CIDR,172.16.0.0/12,DIRECT,no-resolve
  - IP-CIDR,192.168.0.0/16,DIRECT,no-resolve
  - DOMAIN-SUFFIX,local,DIRECT
  - DOMAIN-SUFFIX,google.com,{FR}
  - DOMAIN-SUFFIX,googleapis.com,{FR}
  - DOMAIN-SUFFIX,gstatic.com,{FR}
  - DOMAIN-SUFFIX,googleusercontent.com,{FR}
  - DOMAIN-SUFFIX,withgoogle.com,{FR}
  - DOMAIN-SUFFIX,google.dev,{FR}
  - MATCH,{EE}
""")

    def sb(name: str, path: str) -> dict:
        return {"type": "vless", "tag": name, "server": HOST, "server_port": PORT, "uuid": uid,
                "transport": {"type": "ws", "path": path, "headers": {"Host": HOST}},
                "tls": {"enabled": True, "server_name": HOST}}

    sing = {"log": {"level": "warn"},
            "inbounds": [{"type": "mixed", "tag": "mixed-in", "listen": "127.0.0.1", "listen_port": 2080}],
            "outbounds": [sb(EE, _BRAND["paths"]["general"]), sb(FR, _BRAND["paths"]["france"]), sb(TEST, _BRAND["paths"]["test"]), {"type": "direct", "tag": "direct"}],
            "route": {"rules": [
                {"domain_suffix": ["google.com", "googleapis.com", "gstatic.com",
                                   "googleusercontent.com", "withgoogle.com"], "outbound": FR},
                {"outbound": EE}]}}
    (token_dir / "singbox.json").write_text(json.dumps(sing, indent=2, ensure_ascii=False))
    token_dir.chmod(0o755)


def main() -> int:
    uid = UID_FILE.read_text().strip()
    if "--new" in sys.argv or not any(ROOT.iterdir() if ROOT.is_dir() else []):
        import secrets
        token = secrets.token_urlsafe(14).replace("-", "").replace("_", "")[:18]
        ROOT.mkdir(parents=True, exist_ok=True)
        target = ROOT / token
        target.mkdir(exist_ok=True)
        build(target, uid)
        print(f"https://{HOST}:{PORT}/sub/{token}")
        return 0
    for target in sorted(p for p in ROOT.iterdir() if p.is_dir()):
        build(target, uid)
        print(f"https://{HOST}:{PORT}/sub/{target.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
