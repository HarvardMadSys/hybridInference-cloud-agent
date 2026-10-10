"""Cloud Agent sandbox host: one bare-metal node that registers its SSH host keys.

Instantiate this profile only through the Cloud Agent control plane. For each
experiment it supplies a one-time registration token and the HTTPS address of
its node-registration endpoint. At first boot the node gives itself fresh SSH
host keys and reports their public halves there; the control plane then
connects to this node only with those keys.

Publish it once per CloudLab project as a project-private profile (not public):
the token arrives as a parameter, which every member of the project can read.
Members of a project already administer all of its nodes, so that grants
nothing new; nobody outside the project sees it. The control plane never logs
the token and stores only its hash.

Like the Portal's own example profiles, it builds strings by concatenation
only, which every geni-lib interpreter the Portal uses accepts.
"""

import base64
import re

import geni.portal as portal
import geni.rspec.pg as pg

# Runs as root on every boot. Keys are regenerated once (a stock image may share
# its host keys with other nodes); later boots report the same keys, which the
# control plane accepts as a repeat of the same registration.
REGISTER = r"""#!/bin/bash
set -u
url="$1"
token="$2"
# Overridable only so the script can be exercised outside a node.
ssh_dir="${CLOUD_AGENT_SSH_DIR:-/etc/ssh}"
state="${CLOUD_AGENT_STATE_DIR:-/var/lib/cloud-agent-node}"
mkdir -p "$state"
chmod 700 "$state"
if [ ! -e "$state/host-keys-regenerated" ]; then
    rm -f "$ssh_dir"/ssh_host_*_key "$ssh_dir"/ssh_host_*_key.pub
    ssh-keygen -A -f "${CLOUD_AGENT_KEYGEN_ROOT:-/}" >/dev/null
    systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || true
    touch "$state/host-keys-regenerated"
fi
keys=""
for file in "$ssh_dir/ssh_host_ed25519_key.pub" "$ssh_dir/ssh_host_ecdsa_key.pub" "$ssh_dir/ssh_host_rsa_key.pub"; do
    if [ -r "$file" ]; then
        keys="$keys$(awk '{print $1" "$2}' "$file")
"
    fi
done
body=$(python3 -c 'import json, sys; print(json.dumps({"token": sys.argv[1], "host_keys": [k for k in sys.argv[2].splitlines() if k]}))' "$token" "$keys")
attempt=0
while [ "$attempt" -lt 120 ]; do
    attempt=$((attempt + 1))
    status=$(printf '%s' "$body" | curl --silent --output /dev/null --write-out '%{http_code}' \
        --proto '=https' --tlsv1.2 --max-time 20 \
        -H 'Content-Type: application/json' --data-binary @- "$url")
    case "$status" in
        200) echo "cloud-agent: host keys registered" > "$state/registration"; exit 0 ;;
        404|409|422) echo "cloud-agent: registration refused ($status)" > "$state/registration"; exit 1 ;;
    esac
    sleep 30
done
echo "cloud-agent: control plane unreachable" > "$state/registration"
exit 1
"""

pc = portal.Context()
pc.defineParameter(
    "phystype",
    "Node type",
    portal.ParameterType.NODETYPE,
    "",
    longDescription="CloudLab hardware type, for example r6615.",
)
pc.defineParameter(
    "osImage",
    "Disk image",
    portal.ParameterType.IMAGE,
    "urn:publicid:IDN+emulab.net+image+emulab-ops//UBUNTU24-64-STD",
)
pc.defineParameter(
    "dataMount",
    "Local data disk mount point",
    portal.ParameterType.STRING,
    "/mydata",
    advanced=True,
)
pc.defineParameter(
    "dataSize",
    "Local data disk size in GB (0 uses all free space)",
    portal.ParameterType.INTEGER,
    0,
    advanced=True,
)
pc.defineParameter(
    "registrationUrl",
    "Host key registration address",
    portal.ParameterType.STRING,
    "",
    advanced=True,
    hide=True,
)
pc.defineParameter(
    "registrationToken",
    "One-time host key registration token",
    portal.ParameterType.STRING,
    "",
    advanced=True,
    hide=True,
)
params = pc.bindParameters()

# The Portal also runs this script with the empty defaults (when the profile is
# saved, or instantiated by hand): such a node simply does not register. Values
# the control plane supplies end up in a shell command line, so accept only
# plain characters.
registering = bool(params.registrationUrl or params.registrationToken)
if registering and not re.match(r"^https://[A-Za-z0-9.:/_-]+$", params.registrationUrl):
    pc.reportError(
        portal.ParameterError("Use the control plane's HTTPS address.", ["registrationUrl"])
    )
if registering and not re.match(r"^[A-Za-z0-9_-]{32,128}$", params.registrationToken):
    pc.reportError(
        portal.ParameterError("The registration token is malformed.", ["registrationToken"])
    )
if not re.match(r"^/[A-Za-z0-9/_-]+$", params.dataMount):
    pc.reportError(portal.ParameterError("Use an absolute mount point.", ["dataMount"]))
if params.dataSize < 0:
    pc.reportError(portal.ParameterError("The size cannot be negative.", ["dataSize"]))
pc.verifyParameters()

request = pc.makeRequestRSpec()
node = request.RawPC("node0")
if params.phystype:
    node.hardware_type = params.phystype
node.disk_image = params.osImage
disk = node.Blockstore("node0-data", params.dataMount)
disk.size = "0GB" if params.dataSize == 0 else str(params.dataSize) + "GB"
disk.placement = "any"

# Piped straight into root's shell: no file another user could swap in between.
encoded = base64.b64encode(REGISTER.encode()).decode()
if registering:
    node.addService(
        pg.Execute(
            shell="bash",
            command="echo "
            + encoded
            + " | base64 -d | sudo bash -s -- '"
            + params.registrationUrl
            + "' '"
            + params.registrationToken
            + "'",
        )
    )

pc.printRequestRSpec(request)
