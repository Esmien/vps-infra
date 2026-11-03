#!/usr/bin/env python3
"""Obtain a Let's Encrypt certificate, honouring the ACME "retry after" hint.

Runs certbot via webroot; on failure it looks for a retry-after timestamp in the
output (Let's Encrypt sends one on rate limits / repeated validation failures),
sleeps until that moment + a few seconds, and retries.  Each new retry-after
replaces the previous one.  Gives up only after 24 hours.

Usage: certbot_retry.py vpn.esmien.ru [--webroot /var/www/html]
"""
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

LOG = Path("/var/log/certbot-retry.log")
MAX_HOURS = 24
MIN_PAUSE = 20           # seconds between attempts when no hint is given
HINT_MARGIN = (3, 4)     # sleep retry_after + this many seconds


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    with LOG.open("a") as fh:
        fh.write(line + "\n")


def parse_retry_after(text: str):
    """Return aware datetime from certbot output, or None."""
    for pattern in (
        r"retry after\s+(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}Z?)",
        r'"retry_after"\s*:\s*"([^"]+)"',
        r"Retry-After:\s*(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}Z?)",
    ):
        m = re.search(pattern, text, re.I)
        if not m:
            continue
        raw = m.group(1).replace(" ", "T")
        if raw.endswith("Z"):
            raw = raw[:-1]
        try:
            return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def attempt(domain: str, webroot: str) -> tuple[bool, str]:
    cmd = ["certbot", "certonly", "--webroot", "-w", webroot, "-d", domain,
           "--cert-name", domain, "--non-interactive", "--agree-tos", "--expand"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, out


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    domain = sys.argv[1]
    webroot = sys.argv[sys.argv.index("--webroot") + 1] if "--webroot" in sys.argv else "/var/www/html"
    deadline = time.time() + MAX_HOURS * 3600
    tries = 0
    while time.time() < deadline:
        tries += 1
        ok, out = attempt(domain, webroot)
        if ok:
            log(f"OK {domain} (attempt {tries})")
            return 0
        hint = parse_retry_after(out)
        tail = " | ".join(l.strip() for l in out.strip().splitlines()[-3:] if l.strip())
        log(f"FAIL {domain} (attempt {tries}): {tail[:400]}")
        if hint:
            pause = (hint - datetime.now(timezone.utc)).total_seconds() + HINT_MARGIN[0] + (tries % 2)
            log(f"retry after {hint.isoformat()} -> sleeping {max(pause, MIN_PAUSE):.0f}s")
        else:
            pause = MIN_PAUSE * min(tries, 6)
            log(f"no retry-after hint, backing off {pause:.0f}s")
        pause = max(5, min(pause, deadline - time.time()))
        time.sleep(pause)
    log(f"GIVE UP {domain} after {MAX_HOURS}h")
    return 1


if __name__ == "__main__":
    sys.exit(main())
