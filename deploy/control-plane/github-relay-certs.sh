#!/bin/sh
# Make the GitHub relay's CA and its certificate for github.com and api.github.com.
#
#   deploy/control-plane/github-relay-certs.sh <output-dir>
#
# Writes, into <output-dir>:
#   ca.pem     the CA certificate. Public: every runner host gets it as
#              AGENT_SANDBOX_GITHUB_RELAY_CA_FILE, and sandbox members trust it.
#   ca.key     the CA's private key. Keep it offline; nothing at runtime needs it.
#   relay.pem  the relay's certificate, for the github-relay service.
#   relay.key  its key, for the github-relay service only.
#
# The CA is name-constrained to github.com and api.github.com (critical
# nameConstraints), so even its own key cannot sign a certificate a member would
# accept for any other name. Re-run to rotate; then redeploy the relay and the
# hosts' CA file together.
set -eu

out="${1:?usage: github-relay-certs.sh <output-dir>}"
command -v openssl >/dev/null || { echo "openssl is required" >&2; exit 1; }
mkdir -p "$out"
umask 077
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

cat > "$work/ca.cnf" <<'CNF'
[req]
distinguished_name = dn
prompt = no
[dn]
CN = FreeInference GitHub relay CA
[ca]
basicConstraints = critical, CA:TRUE, pathlen:0
keyUsage = critical, keyCertSign, cRLSign
nameConstraints = critical, permitted;DNS:github.com, permitted;DNS:api.github.com
subjectKeyIdentifier = hash
CNF

cat > "$work/relay.cnf" <<'CNF'
[req]
distinguished_name = dn
prompt = no
[dn]
CN = github.com
[leaf]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:github.com, DNS:api.github.com
authorityKeyIdentifier = keyid
CNF

openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
    -keyout "$out/ca.key" -out "$out/ca.pem" -days 1825 \
    -config "$work/ca.cnf" -extensions ca
openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
    -keyout "$out/relay.key" -out "$work/relay.csr" -config "$work/relay.cnf"
openssl x509 -req -in "$work/relay.csr" -CA "$out/ca.pem" -CAkey "$out/ca.key" \
    -CAcreateserial -out "$out/relay.pem" -days 397 \
    -extfile "$work/relay.cnf" -extensions leaf
rm -f "$out/ca.srl"
openssl verify -CAfile "$out/ca.pem" "$out/relay.pem"

# The github-relay container runs as uid 10001 (deploy/control-plane/Dockerfile)
# and reads relay.pem and relay.key through a read-only bind mount, which keeps
# their host owner and mode. So the directory must be traversable, and the key
# must belong to that uid; 0600 under the operator's own uid is unreadable there.
relay_uid=10001
chmod 0755 "$out"
chmod 0644 "$out/ca.pem" "$out/relay.pem"
chmod 0400 "$out/relay.key"
if [ "$(id -u)" = 0 ]; then
    chown "$relay_uid:$relay_uid" "$out/relay.key"
else
    echo "relay.key is owned by uid $(id -u); the relay runs as uid $relay_uid and cannot read it." >&2
    echo "Give it to the relay before starting it:" >&2
    echo "  sudo chown $relay_uid:$relay_uid $out/relay.key" >&2
fi
echo "Move $out/ca.key off this machine before mounting $out: nothing at runtime needs it." >&2
