#!/usr/bin/env python3
"""Print VLESS-over-WebSocket links (real TLS, SNI vpn.esmien.ru).

8443 - primary entry, 2087 - backup entry if the ISP starts blocking 8443.
"""
import urllib.parse
from pathlib import Path

uid = Path("/root/mtproto-faketls/ws-client-uuid").read_text().strip()
for port, tag in ((8443, "mon-ws-8443"), (2087, "mon-ws-2087-backup")):
    params = {
        "type": "ws",
        "security": "tls",
        "sni": "vpn.esmien.ru",
        "host": "vpn.esmien.ru",
        "path": "/ws",
        "encryption": "none",
    }
    print(f"vless://{uid}@vpn.esmien.ru:{port}?" + urllib.parse.urlencode(params) + f"#{tag}")
