#!/usr/bin/env python3
"""Rebuild nginx entry configs + VPN subscriptions from /root/vpn-router/brand.json.

Switching the public host name (e.g. vpn.esmien.qd.je -> vpn.esmien.ru):
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
    access_log /var/log/nginx/vpn-{logname}.log vpnx;

    location = / {{ default_type text/plain; return 200 "ok\\n"; }}
{locations}
    location ~ ^/sub/(?<esmien_sub_token>[A-Za-z0-9_-]+)/?$ {{
        root /var/www/esmien-sub;
        try_files /$esmien_sub_token/$esmien_sub_file =404;
        default_type text/plain;
        add_header Cache-Control "no-store";
        add_header profile-title $esmien_sub_title;
        add_header profile-update-interval 12;
        add_header Content-Disposition "inline; filename=$esmien_sub_fname";
    }}
}}
"""

PANEL_LOCATION = """    location /panel/ {{
        limit_req zone=xui_panel burst=5 nodelay;
        limit_req_status 429;
        limit_conn conn_per_ip 20;
        proxy_pass http://127.0.0.1:{upstream};
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
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


def render(port: int, host: str = HOST, cert: str = CERT) -> str:
    locations = "\n".join(
        (PANEL_LOCATION if path == "/panel/" else LOCATION).format(path=path, upstream=up)
        for path, up in UPSTREAMS)
    logname = str(port) if host == HOST else f"{port}-{host.split('.')[0]}"
    return TEMPLATE.format(port=port, host=host, cert=cert, logname=logname, locations=locations.rstrip("\n"))


def write_title_map() -> None:
    """Profile title per subscription token: whitelist bundle is the LTE one."""
    titles = {"default": "Esmien VPN"}
    for d in sorted(Path("/var/www/esmien-sub").iterdir()):
        if not d.is_dir():
            continue
        kind_file = d / "kind"
        kind = kind_file.read_text().strip() if kind_file.exists() else "sub"
        titles[d.name] = "Esmien VPN (LTE)" if kind == "whitelist" else "Esmien VPN"
    body = "map $esmien_sub_token $esmien_sub_title {\n" + "".join(
        f'    "{t}" "{n}";\n' for t, n in titles.items()) + "}\n"
    body += "map $esmien_sub_token $esmien_sub_fname {\n"
    body += "".join(f'    "{t}" "{"esmien-vpn-lte" if n.endswith("(LTE)") else "esmien-vpn"}";\n'
                    for t, n in titles.items())
    body += "}\n"
    Path("/etc/nginx/conf.d/00-esmien-sub-title.conf").write_text(body)
    print("written 00-esmien-sub-title.conf:", {k: v for k, v in titles.items() if k != "default"})


def main() -> int:
    write_title_map()
    for port in PORTS:
        target = Path(f"/etc/nginx/conf.d/vpn-{port}.conf")
        target.write_text(render(port))
        print(f"written {target} (server_name {HOST}, cert {CERT})")
    # Хосты *.esmien.qd.je снесены 03.11.2026: домен мёртв, compat-блок не нужен.
    compat_path = Path("/etc/nginx/conf.d/vpn-compat.conf")
    if compat_path.exists():
        compat_path.unlink()
        print(f"removed {compat_path}")
    subprocess.run(["python3", str(BASE / "make_sub.py")], check=False)
    print("done: nginx -t && systemctl reload nginx")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
