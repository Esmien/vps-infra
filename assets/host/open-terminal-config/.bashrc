# ~/.bashrc: executed by bash(1) for non-login shells.

# Note: PS1 is set in /etc/profile, and the default umask is defined
# in /etc/login.defs. You should not need this unless you want different
# defaults for root.
# PS1='${debian_chroot:+($debian_chroot)}\h:\w\$ '
# umask 022

# You may uncomment the following lines if you want `ls' to be colorized:
# export LS_OPTIONS='--color=auto'
# eval "$(dircolors)"
# alias ls='ls $LS_OPTIONS'
# alias ll='ls $LS_OPTIONS -l'
# alias l='ls $LS_OPTIONS -lA'
#
# Some more alias to avoid making mistakes:
# alias rm='rm -i'
# alias cp='cp -i'
# alias mv='mv -i'

alias chroot-host="chroot /mnt/host /bin/bash"

# --- два хоста из одного терминала (разделённая схема) ------------------------
# Ключ и known_hosts лежат на хосте RU (/root/.ssh) и видны отсюда через /mnt/host,
# поэтому переживают пересоздание контейнера open-terminal. На однхостовой
# установке ключа нет — блок просто не активируется.
EDGE_HOST=138.124.180.251
SSH_KEY=/mnt/host/root/.ssh/id_ed25519_webui_edge
SSH_KNOWN=/mnt/host/root/.ssh/known_hosts

if [ -f "$SSH_KEY" ]; then
    # RU — это sshd хоста, доступный по шлюзу docker-сети. host.docker.internal
    # приоритетнее, если его когда-нибудь добавят в compose: адрес шлюза зависит
    # от подсети stack-net.
    RU_HOST=$(getent hosts host.docker.internal 2>/dev/null | awk '{print $1}')
    [ -n "$RU_HOST" ] || RU_HOST=172.18.0.1

    # ru / edge — интерактивный шелл на нужном хосте
    ru() {
        ssh -t -i "$SSH_KEY" -o UserKnownHostsFile="$SSH_KNOWN" -o IdentitiesOnly=yes "root@$RU_HOST" "$@"
    }
    edge() {
        ssh -t -i "$SSH_KEY" -o UserKnownHostsFile="$SSH_KNOWN" -o IdentitiesOnly=yes "root@$EDGE_HOST" "$@"
    }

    # both-run 'systemctl status nginx' — одна команда на оба хоста, вывод подряд
    both-run() {
        if [ $# -eq 0 ]; then echo "usage: both-run '<command>'"; return 1; fi
        echo "=== ru ($RU_HOST) ==="
        ssh -i "$SSH_KEY" -o UserKnownHostsFile="$SSH_KNOWN" -o IdentitiesOnly=yes \
            -o BatchMode=yes "root@$RU_HOST" "$*"
        echo "=== edge ($EDGE_HOST) ==="
        ssh -i "$SSH_KEY" -o UserKnownHostsFile="$SSH_KNOWN" -o IdentitiesOnly=yes \
            -o BatchMode=yes "root@$EDGE_HOST" "$*"
    }

    # both — tmux: слева RU, справа edge
    both() {
        tmux attach -t both 2>/dev/null && return
        tmux new-session -s both -d \
            "ssh -t -i $SSH_KEY -o UserKnownHostsFile=$SSH_KNOWN -o IdentitiesOnly=yes root@$RU_HOST" \; \
            split-window -t both -h \
            "ssh -t -i $SSH_KEY -o UserKnownHostsFile=$SSH_KNOWN -o IdentitiesOnly=yes root@$EDGE_HOST" \; \
            attach
    }
fi
