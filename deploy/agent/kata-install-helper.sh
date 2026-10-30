#!/usr/bin/env bash
#
# The one privileged step a deployment agent may ask for: install, or check,
# one Kata Containers version and digest with the release recipe's own
# setup_kata_runtime.sh. It accepts exactly
#
#   kata-install-helper.sh install <version> <sha256>
#   kata-install-helper.sh check   <version> <sha256>
#
# and nothing else: no path, no download source, no environment from the
# caller. Install it root-owned beside a root-owned copy of
# setup_kata_runtime.sh and kata_runtime_epoch.py, and allow only it in
# sudoers for the deployment user, for example:
#
#   deploy ALL=(root) NOPASSWD: /opt/cloud-agent/libexec/kata-install-helper.sh
#
# The deployment agent's policy then names
#   "kata_helper": ["sudo", "-n", "/opt/cloud-agent/libexec/kata-install-helper.sh"]
# and lists the reviewed version and digest pairs it may pass. Downloads come
# from the script's fixed upstream and are verified against the digest, so a
# wrong digest fails the install rather than installing something else.

set -Eeuo pipefail
umask 022

if [ "$#" -ne 3 ]; then
  echo "usage: kata-install-helper.sh install|check <version> <sha256>" >&2
  exit 2
fi
action="$1"
version="$2"
digest="$3"
case "$action" in
  install|check) ;;
  *) echo "kata-install-helper: unknown action" >&2; exit 2 ;;
esac
if ! [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "kata-install-helper: the version is not MAJOR.MINOR.PATCH" >&2
  exit 2
fi
if ! [[ "$digest" =~ ^[0-9a-f]{64}$ ]]; then
  echo "kata-install-helper: the digest is not a lowercase SHA-256" >&2
  exit 2
fi
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
setup="${here}/setup_kata_runtime.sh"
if [ ! -f "$setup" ]; then
  echo "kata-install-helper: setup_kata_runtime.sh is not installed beside this helper" >&2
  exit 2
fi
flag=()
if [ "$action" = "check" ]; then
  flag=(--check)
fi
# A clean environment: nothing from the caller reaches the script.
exec env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  KATA_VERSION="$version" KATA_SHA256="$digest" bash "$setup" "${flag[@]}"
