#!/usr/bin/env bash
# The VM agent's own user (ADR 0020 P2, ops/gcp/vm-setup.md §7).
#
# herdr on usher-prod runs as whoever its SSH connection logs in as. The owner's
# OS Login user has sudo and the docker group, so the agent and every herdr pane
# ran with production's full rights. This script makes `usher-agent`: a plain
# local user with no sudo and no docker, its own read-only clone of the
# production checkout, and one SSH key the host's `usher-prod-agent` alias uses.
#
# Run by the owner on usher-prod, from the production checkout, with sudo:
#   ops/gcp/agent-user.sh setup <host-public-key-file>   # once, or to change the key
#   ops/gcp/agent-user.sh refresh                        # after every git pull/fetch
#
# Everything written in the agent's home is written as the agent (sudo -u), so a
# file the agent planted there is never followed or run as root.
set -euo pipefail

AGENT=usher-agent
# Never 1000: that is the compose default for CLOUDFLARED_USER, and the agent
# would own cloudflared's process and read the tunnel credentials through
# /proc/<pid>/root if the VM's override were ever lost.
AGENT_UID=2900
SRC=$(git rev-parse --show-toplevel)
AGENT_HOME=/home/$AGENT

as_agent() { sudo -u "$AGENT" -H -- "$@"; }

refresh() {
    # The clone is fed from a bundle of tracked history (never .env or other
    # untracked files): production HEAD as prod/head, plus the owner's
    # origin/main from their last fetch, so `git log HEAD..origin/main` works.
    local tmp
    tmp=$(mktemp -d)
    chmod 755 "$tmp"
    git -C "$SRC" bundle create -q "$tmp/usher.bundle" HEAD refs/remotes/origin/main
    chmod 644 "$tmp/usher.bundle"
    [ -d "$AGENT_HOME/usher/.git" ] || as_agent git init -q "$AGENT_HOME/usher"
    as_agent git -C "$AGENT_HOME/usher" fetch -q "$tmp/usher.bundle" \
        '+HEAD:refs/remotes/prod/head' '+refs/remotes/origin/main:refs/remotes/origin/main'
    as_agent git -C "$AGENT_HOME/usher" checkout -q -f --detach refs/remotes/prod/head
    rm -rf "$tmp"
    echo "agent clone at $(as_agent git -C "$AGENT_HOME/usher" log -1 --format='%h %s')"
}

setup() {
    local key=${1:?usage: agent-user.sh setup <host-public-key-file>}
    grep -qE '^(ssh-ed25519|ecdsa-sha2-[a-z0-9-]+|ssh-rsa) [A-Za-z0-9+/=]+' "$key" \
        || { echo "$key is not an SSH public key" >&2; exit 1; }
    if ! id "$AGENT" >/dev/null 2>&1; then
        sudo useradd --create-home --user-group --uid "$AGENT_UID" --shell /bin/bash "$AGENT"
    elif [ "$(id -u "$AGENT")" != "$AGENT_UID" ]; then
        # An agent made before the pin: move it (usermod re-owns its home).
        if pgrep -u "$AGENT" >/dev/null; then
            echo "$AGENT has running processes; stop them first: sudo pkill -u $AGENT" >&2
            exit 1
        fi
        sudo groupmod -g "$AGENT_UID" "$AGENT"
        sudo usermod -u "$AGENT_UID" -g "$AGENT_UID" "$AGENT"
    fi
    if [ "$(id -u "$AGENT")" != "$AGENT_UID" ]; then
        echo "$AGENT has uid $(id -u "$AGENT"), not $AGENT_UID" >&2
        exit 1
    fi
    sudo chmod 750 "$AGENT_HOME"
    # Debian's useradd adds extra groups (video, ...); the agent keeps only its own.
    sudo usermod -G "" "$AGENT"
    # The whole point: no sudo, no docker, no device groups. Refuse to go on if one is there.
    if id -nG "$AGENT" | tr ' ' '\n' | grep -qxE 'docker|sudo|google-sudoers|adm|wheel|video|render|disk|kvm'; then
        echo "$AGENT is in a privileged group: $(id -nG "$AGENT")" >&2
        exit 1
    fi
    # No agent or X11 forwarding: the host's ssh-agent never reaches the VM.
    as_agent sh -c 'umask 077; mkdir -p ~/.ssh; cat > ~/.ssh/authorized_keys' \
        < <(printf 'no-agent-forwarding,no-X11-forwarding %s\n' "$(head -n1 "$key")")
    refresh
    echo "now, from the host: ssh usher-prod-agent id   # uid=$AGENT_UID($AGENT) groups=$AGENT_UID($AGENT)"
}

case ${1:-} in
    setup) shift; setup "$@" ;;
    refresh) refresh ;;
    *) echo "usage: $0 setup <host-public-key-file> | refresh" >&2; exit 2 ;;
esac
