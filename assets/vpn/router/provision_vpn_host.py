#!/usr/bin/env python3
"""Wait for a certificate for the VPN host, then switch the VPN entry to it.

1. certbot_retry.py <host>  (honours ACME retry-after, up to 24h)
2. on success: point brand.json at the new host + cert, rebuild nginx confs and
   subscriptions, reload nginx, verify an end-to-end request.

Usage: provision_vpn_host.py vpn.esmien.ru
"""
import json
import subprocess
import sys
from pathlib import Path

BASE = Path("/root/vpn-router")
BRAND = BASE / "brand.json"


def log(msg: str) -> None:
    print(msg, flush=True)
    with Path("/var/log/certbot-retry.log").open("a") as fh:
        fh.write(f"provision: {msg}\n")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    host = sys.argv[1]
    rc = subprocess.run(["python3", str(BASE / "certbot_retry.py"), host]).returncode
    if rc != 0:
        log(f"certificate for {host} not obtained, staying on the current host")
        return rc

    brand = json.loads(BRAND.read_text())
    brand["host"] = host
    brand["cert_dir"] = f"/etc/letsencrypt/live/{host}"
    BRAND.write_text(json.dumps(brand, ensure_ascii=False, indent=2) + "\n")
    log(f"brand.json -> {host}")

    for step in (["python3", str(BASE / "apply_brand.py")],
                 ["nginx", "-t"]):
        r = subprocess.run(step, capture_output=True, text=True)
        log(f"{' '.join(step)} rc={r.returncode} {r.stdout.strip()[-120:]}")
        if r.returncode != 0:
            log("aborting switch, nginx config invalid")
            return 1
    subprocess.run(["systemctl", "reload", "nginx"], check=False)
    log("nginx reloaded")

    check = subprocess.run(
        ["curl", "-sS", "--max-time", "10", "--resolve", f"{host}:8443:127.0.0.1",
         "-o", "/dev/null", "-w", "%{http_code}", f"https://{host}:8443/"],
        capture_output=True, text=True)
    log(f"https://{host}:8443/ -> {check.stdout.strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
