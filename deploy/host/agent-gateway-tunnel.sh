#!/bin/sh
# One encrypted hop to the gateway, and nothing else.
#
# A sandbox runs untrusted repository code on a chat-private network declared
# `internal: true` — no route off the host at all. Lifecycle code attaches this
# relay container to that network after discovering it on the compose
# backplane. The relay's other leg is SSH: one fixed forwarded port to one fixed
# destination.
#
# It is deliberately not a proxy. There is no CONNECT, no request routing, no
# destination the caller can choose: this instance's address is fixed at
# startup. Each chat Agent-phase network contains two such instances, one for
# inference and one for the MCP-only listener, and no other chat or general
# route. (Setup runs earlier, on its own tier, and may also reach the allowlist
# proxy; this tunnel is not on that path.)
#
# SSH rather than plain TCP because the hop crosses a shared network and the
# traffic is not incidental: every claim carries an enrolled-host credential, and
# every job call carries a capability token for that job. Those must not cross
# a lab LAN in the clear, and it lets the gateway keep listening on loopback.
set -eu

KEY_SRC="${TUNNEL_SSH_KEY:-/etc/agent-tunnel/id}"
KNOWN_HOSTS="${TUNNEL_SSH_KNOWN_HOSTS:-/etc/agent-tunnel/known_hosts}"
LISTEN_PORT="${TUNNEL_LISTEN_PORT:-8080}"
REMOTE_HOST="${TUNNEL_REMOTE_HOST:-127.0.0.1}"
REMOTE_PORT="${TUNNEL_REMOTE_PORT:-8080}"
SSH_PORT="${TUNNEL_SSH_PORT:-22}"

# Optional, and only the control-plane tunnel sets it: the far end reaching
# back. `-L` carries this machine's traffic out; `-R` publishes one local
# service on the far host, over the same connection.
#
# It exists because the route is one-way. The control plane must reach this
# host's workspace broker for every file listing, terminal keystroke and
# git status — but this host is the only one that can open a connection: the
# control plane's machine cannot resolve or reach it at all. So the reverse
# forward rides the connection that already goes the other way.
#
# Binding a non-loopback address on the far host needs `GatewayPorts
# clientspecified` for this key's account in its sshd, with that account's
# PermitListen and the key's permitlisten naming this one address and port
# (deploy/host/sshd-tunnels.conf.example). Never `yes`: that silently moves
# every remote forward to the wildcard address. Bind the far host's *docker
# bridge gateway* rather than 0.0.0.0: the audience is containers on that
# bridge, and 0.0.0.0 would publish the broker to the whole LAN behind one
# shared token.
#
# The far-side pair is what turns it on, because that is the deployment-
# specific half; the target defaults to this stack's own broker.
REVERSE_BIND="${TUNNEL_REVERSE_BIND:-}"
REVERSE_PORT="${TUNNEL_REVERSE_PORT:-}"
REVERSE_TARGET_HOST="${TUNNEL_REVERSE_TARGET_HOST:-agent-workspace-broker}"
REVERSE_TARGET_PORT="${TUNNEL_REVERSE_TARGET_PORT:-8092}"

die() { printf '[agent-gateway-tunnel] ERROR: %s\n' "$*" >&2; exit 1; }

[ -n "${TUNNEL_SSH_DESTINATION:-}" ] || die \
    "TUNNEL_SSH_DESTINATION is required (user@gateway-host)"
[ -r "$KEY_SRC" ] || die "no readable ssh key at $KEY_SRC (mount one read-only)"

# Host verification is not optional here. Turning it off would hand the
# credentials this tunnel exists to protect to whatever answers on that
# address — the exact attack encrypting the hop is meant to prevent.
[ -s "$KNOWN_HOSTS" ] || die \
    "no known_hosts at $KNOWN_HOSTS. Generate it on the runner host with
     'ssh-keyscan -p $SSH_PORT <gateway-host>', check the fingerprint, and mount
     it. This is not disabled by an option: an unverified hop would leak the
     enrolled-host credential to anything that answers."

# Both or neither. A port with no bind address is a loopback-only forward: it
# binds successfully on the far host, ssh reports nothing wrong, and the
# containers that need it cannot reach it — a working tunnel to nowhere, which
# is the failure this whole change exists to stop happening quietly.
REVERSE_SET=""
if [ -n "$REVERSE_BIND" ] || [ -n "$REVERSE_PORT" ]; then
    [ -n "$REVERSE_BIND" ] && [ -n "$REVERSE_PORT" ] || die \
        "a reverse forward needs both TUNNEL_REVERSE_BIND and TUNNEL_REVERSE_PORT;
     got bind='$REVERSE_BIND' port='$REVERSE_PORT'. Bind the far host's docker
     bridge gateway (e.g. 172.24.0.1), not 127.0.0.1 — a loopback bind is
     invisible to the containers that need it."
    REVERSE_SET="yes"
fi

# ssh refuses a key others can read, and a bind mount keeps the host's mode.
install -m 600 "$KEY_SRC" /tmp/tunnel-key

printf '[agent-gateway-tunnel] %s -> %s:%s via %s\n' \
    "0.0.0.0:${LISTEN_PORT}" "$REMOTE_HOST" "$REMOTE_PORT" "$TUNNEL_SSH_DESTINATION"

# ExitOnForwardFailure: without it ssh stays up having bound nothing, and every
# sandbox gets connection-refused against a container that looks healthy.
# Dying instead lets the restart policy be the supervisor. It covers the
# reverse forward too, so a far host whose sshd refuses the bind (a port in
# use, or one PermitListen/permitlisten does not allow) fails visibly here
# instead of leaving the control plane with an address nothing answers on. It
# cannot see a wrong GatewayPorts: sshd then binds loopback or the wildcard
# address instead and reports success.
set -- -N \
    -i /tmp/tunnel-key \
    -p "$SSH_PORT" \
    -o BatchMode=yes \
    -o IdentitiesOnly=yes \
    -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile="$KNOWN_HOSTS" \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=15 \
    -o ServerAliveCountMax=3 \
    -o TCPKeepAlive=yes \
    -L "0.0.0.0:${LISTEN_PORT}:${REMOTE_HOST}:${REMOTE_PORT}"

if [ -n "$REVERSE_SET" ]; then
    printf '[agent-gateway-tunnel] reverse: %s:%s on the far host -> %s:%s here\n' \
        "$REVERSE_BIND" "$REVERSE_PORT" "$REVERSE_TARGET_HOST" "$REVERSE_TARGET_PORT"
    set -- "$@" -R "${REVERSE_BIND}:${REVERSE_PORT}:${REVERSE_TARGET_HOST}:${REVERSE_TARGET_PORT}"
fi

exec ssh "$@" "$TUNNEL_SSH_DESTINATION"
