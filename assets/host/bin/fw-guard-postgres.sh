#!/bin/bash
# Закрывает 5432 (PostgreSQL) от внешних адресов, оставляя localhost и docker-сети 172.16.0.0/12.
set -u
D=(-p tcp --dport 5432)
iptables -D INPUT "${D[@]}" -s 127.0.0.1/32 -j ACCEPT 2>/dev/null || true
iptables -D INPUT "${D[@]}" -s 172.16.0.0/12 -j ACCEPT 2>/dev/null || true
iptables -D INPUT "${D[@]}" -j DROP 2>/dev/null || true
iptables -I INPUT 1 "${D[@]}" -j DROP
iptables -I INPUT 1 "${D[@]}" -s 172.16.0.0/12 -j ACCEPT
iptables -I INPUT 1 "${D[@]}" -s 127.0.0.1/32 -j ACCEPT
