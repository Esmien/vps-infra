#!/usr/bin/env python3
"""Report which country Google assigns to an exit IP, and whether Gemini allows it.

How it works: the Gemini web app template embeds Google's own geo decision for
the requesting IP in `this.gbar_={CONFIG:[[[0,"www.gstatic.com","<build>","<cc>","<lang>"`.
That two-letter code is Google's country for the connection — not ipinfo's, and
not the DNS resolver's. Gemini only works when it is a supported country.

Usage:
    gemini_geo_check.py                 # direct from this host
    gemini_geo_check.py 127.0.0.1:10808 # through a socks5 proxy

Exit code 0 = supported country, 1 = blocked/unsupported, 2 = check failed.
"""
import re
import subprocess
import sys

BLOCKED = {"ru", "by", "ir", "kp", "sy", "cu", "cn", "mm", "sd", "tm", "ve", "af"}
MARKER = re.compile(
    r'this\.gbar_=\{CONFIG:\[\[\[0,"www\.gstatic\.com","[^"]+","([a-z]{2})","([a-z]{2})"'
)


def fetch(socks: str | None) -> str:
    cmd = ["curl", "-sS", "--max-time", "25", "-H", "Accept-Language: en"]
    if socks:
        cmd += ["--socks5-hostname", socks]
    cmd.append("https://gemini.google.com/app")
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def main() -> int:
    socks = sys.argv[1] if len(sys.argv) > 1 else None
    html = fetch(socks)
    m = MARKER.search(html)
    if not m:
        print(f"check failed: no geo marker in response ({len(html)} bytes)")
        return 2
    country, lang = m.groups()
    verdict = "UNSUPPORTED (Google sees a blocked country)" if country in BLOCKED else "supported"
    print(f"google_country={country} lang={lang} -> {verdict}")
    return 1 if country in BLOCKED else 0


if __name__ == "__main__":
    sys.exit(main())
