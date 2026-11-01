#!/usr/bin/env python3
"""Rebuild nginx entry configs + VPN subscriptions from /root/vpn-router/brand.json.

Switching the public host name (e.g. mon.esmien.qd.je -> ai.esmien.qd.je):
  1. edit "host" and "cert_dir" in brand.json
  2. python3 /root/vpn-router/apply_brand.py
  3. nginx -t && systemctl reload nginx
Client links change accordingly because the subscription files are regenerated.
"""
import json
import subprocess
from pathlib import Path

BASE = Path("/root/vpn-router")
CONF = json.loads((BASE / "brand.json").read_text())
HOST = CONF["host"]
CERT = CONF["cert_dir"]
PORTS = (CONF["port"], CONF["alt_port"])
UPSTREAMS = [(path, port) for path, port in CONF["upstreams"].items()]

TEMPLATE = """server {{
    listen {port} ssl;
    listen [::]:{port} ssl;
    server_name {host};

    ssl_certificate {cert}/fullchain.pem;
    ssl_certificate_key {cert}/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;
    access_log /var/log/nginx/vpn-{logname}.log;

    location = / {{ default_type text/plain; return 200 "ok\\n"; }}
{locations}
    location ~ ^/sub/(?<esmien_sub_token>[A-Za-z0-9_-]+)/?$ {{
        root /var/www/esmien-sub;
        try_files /$esmien_sub_token/$esmien_sub_file =404;
        default_type text/plain;
        add_header Cache-Control "no-store";
    }}
}}
"""

LOCATION = """    location {path} {{
        proxy_pass http://127.0.0.1:{upstream};
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }}
"""


def render(port: int) -> str:
    locations = "\n".join(LOCATION.format(path=path, upstream=up) for path, up in UPSTREAMS)
    return TEMPLATE.format(port=port, host=HOST, cert=CERT, logname=port, locations=locations.rstrip("\n"))


def main() -> int:
    for port in PORTS:
        target = Path(f"/etc/nginx/conf.d/vpn-{port}.conf")
        target.write_text(render(port))
        print(f"written {target} (server_name {HOST}, cert {CERT})")
    subprocess.run(["python3", str(BASE / "make_sub.py")], check=False)
    print("done: nginx -t && systemctl reload nginx")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
