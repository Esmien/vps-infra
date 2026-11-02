#!/usr/bin/env python3
"""Turn the live router config into a secret-free template for a fresh deployment.

Keeps the base routes (our own inbounds and the fr/fi/direct/blocked outbounds),
drops every imported relay route (in-p*, p*), and masks credentials.

Usage: make_template.py [/root/vpn-router/config.json] [/root/vps-infra/assets/vpn/router/config.template.json]
"""
import json
import sys
from pathlib import Path

SRC = Path(sys.argv[1] if len(sys.argv) > 1 else "/root/vpn-router/config.json")
DST = Path(sys.argv[2] if len(sys.argv) > 2 else "/root/vps-infra/assets/vpn/router/config.template.json")
KEEP_IN = {"ws-in", "in-general", "in-france", "in-test", "in-fi"}
KEEP_OUT = {"fr", "fi", "direct", "blocked"}


def mask(obj):
    if isinstance(obj, dict):
        return {k: ("<VLESS_UUID>" if k == "id" else
                    "<PASSWORD>" if k == "password" else
                    "<RELAY_PASSWORD>" if k == "privateKey" else mask(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask(x) for x in obj]
    return obj


cfg = json.loads(SRC.read_text())
cfg["inbounds"] = [i for i in cfg["inbounds"] if i["tag"] in KEEP_IN]
cfg["outbounds"] = [o for o in cfg["outbounds"] if o["tag"] in KEEP_OUT]
for i in cfg["inbounds"]:
    i["listen"] = "127.0.0.1"
rules = []
for r in cfg["routing"]["rules"]:
    tag = (r.get("inboundTag") or [None])[0]
    out = r.get("outboundTag")
    if tag and tag not in KEEP_IN:
        continue
    if out and out not in KEEP_OUT:
        continue
    rules.append(r)
cfg["routing"]["rules"] = rules
cfg["log"] = {"loglevel": "warning"}
cfg = mask(cfg)
DST.write_text(json.dumps(cfg, ensure_ascii=False, indent=1) + "\n")
print(f"template written: {DST} ({len(cfg['inbounds'])} inbounds, {len(cfg['outbounds'])} outbounds, {len(rules)} rules)")
