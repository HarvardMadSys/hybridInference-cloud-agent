#!/usr/bin/env bash
#
# Install and register Kata Containers on an agent-runner host.
#
# Why this exists: the agent sandbox has shipped with `AGENT_SANDBOX_BACKEND=kata`
# as its default for a while, and the `--runtime io.containerd.kata.v2` flag
# provably reaches the Docker daemon — but no host was ever provisioned with the
# shim, so staging historically carried an
# `AGENT_SANDBOX_BACKEND=container` override and those jobs ran on a shared
# host kernel. The gap was never the code; it was that nothing installed the
# runtime. This is that missing step.
#
# What Kata buys: an ordinary container isolates processes, and every sandbox
# still issues syscalls straight at the host kernel. Kata puts each job in a
# lightweight VM with its own kernel, so a container escape has a hypervisor
# boundary behind it. That matters here because the sandbox runs arbitrary code
# out of a user's repository — build scripts, test suites, package lifecycle
# hooks.
#
# Usage, on the runner host:
#
#   sudo deploy/host/setup_kata_runtime.sh          # install (idempotent)
#   deploy/host/setup_kata_runtime.sh --check       # verify only, never installs
#
# `--check` is what deployment calls. It never installs, because a deploy that
# silently provisions a kernel-level runtime is not something to discover from a
# log line afterwards.
#
# Overrides, all optional:
#   KATA_VERSION       release to pin (default below)
#   KATA_SHA256        digest for a version this script does not know
#   KATA_EXTRACT_ROOT  filesystem root to unpack into (default /) — tests use this
#   KATA_LINK_DIR      where the shim is linked (default /usr/local/bin)
#   KATA_CONFIG_PATH   default runtime config to point at the packaged eBPF kernel

set -Eeuo pipefail

# Pinned, not "latest". This is the host's isolation boundary: it changes when
# someone decides it changes, having read the release notes.
#
# 3.32.0 rather than the newer 4.0.0 on purpose. 4.0.0 is a major bump published
# days ago; the runtime enforcing the security boundary is the worst place to
# meet a fresh-major regression, and 3.32.0 has had a release cycle to settle.
# Upgrading is this constant plus its digest, and `--check` refuses any host
# still on the old one.
KATA_VERSION="${KATA_VERSION:-3.32.0}"
KATA_EXTRACT_ROOT="${KATA_EXTRACT_ROOT:-/}"
KATA_LINK_DIR="${KATA_LINK_DIR:-/usr/local/bin}"
KATA_DOWNLOAD_BASE="${KATA_DOWNLOAD_BASE:-https://github.com/kata-containers/kata-containers/releases/download}"

# Upstream publishes no checksum file, so these come from the GitHub release
# API's own asset digests. A version that is not listed here must supply
# KATA_SHA256 — the script refuses rather than downgrading to an unverified
# download, because "no checksum" on a 1.5 GB binary that becomes the kernel
# boundary is not a degraded mode worth having.
known_sha256() {
  case "$1" in
    3.32.0) printf '1449ecea50bd91fa73a94648db195d18950fe869ba4b1f12d05f55f1fa7c1b01' ;;
    4.0.0) printf '2c3b9dfeba355582b40aee462b12916c9740654d0230f696adf719d67b063a8c' ;;
    *) printf '' ;;
  esac
}

# Must match KATA_RUNTIME in cloud_agent_host/sandbox.py. The
# sandbox passes this to `docker run --runtime`; the daemon resolves it to a
# `containerd-shim-kata-v2` binary on its own PATH.
KATA_RUNTIME="io.containerd.kata.v2"

# Kata's packages hardcode a /opt/kata prefix (verified against the release
# tarball, whose members are all `./opt/kata/...`), so the extract root is a
# prefix in front of it rather than a relocation.
PREFIX="${KATA_EXTRACT_ROOT%/}/opt/kata"
SHIM_BINARY="${PREFIX}/bin/containerd-shim-kata-v2"
RUNTIME_BINARY="${PREFIX}/bin/kata-runtime"
# Kata 3.29+ ships a separate debug kernel built with its reviewed eBPF/BTF
# fragment.  Process events are observed inside the guest, so merely installing
# the ordinary VM kernel is not sufficient: the runtime's actual default config
# must boot this asset.  These paths stay overrideable for release-layout tests
# and distro packages, but are never a switch that disables the check.
KATA_CONFIG_PATH="${KATA_CONFIG_PATH:-${PREFIX}/share/defaults/kata-containers/configuration.toml}"
KATA_DEBUG_KERNEL_PATH="${KATA_DEBUG_KERNEL_PATH:-${PREFIX}/share/kata-containers/vmlinuz-debug.container}"
# Symlinked rather than added to PATH: containerd is started by systemd, and a
# symlink into a directory already on the unit's default PATH means its unit
# file never has to be edited. Upstream recommends this for the same reason.
LINKED_COMMANDS=(containerd-shim-kata-v2 kata-runtime kata-collect-data.sh)

log() { printf '[kata-setup] %s\n' "$*"; }
warn() { printf '[kata-setup] WARNING: %s\n' "$*" >&2; }
die() {
  printf '[kata-setup] ERROR: %s\n' "$*" >&2
  exit 1
}

# ── Host capability checks ───────────────────────────────────────────────

require_linux_x86_64() {
  local kernel machine
  kernel="$(uname -s)"
  machine="$(uname -m)"
  [[ "$kernel" == "Linux" ]] || die \
    "Kata needs a Linux host; this is ${kernel}. Run this on the runner host, not a laptop."
  # The pin above names an amd64 asset. Kata publishes arm64/s390x/ppc64le too,
  # but each has its own digest, so silently fetching a different one would
  # defeat the checksum.
  [[ "$machine" == "x86_64" ]] || die \
    "this script pins the amd64 build; this host is ${machine}. Pin the matching asset and its digest first."
}

require_virtualization() {
  # /dev/kvm is the thing that actually has to work. The cpuinfo flag is checked
  # first only to turn "permission denied on /dev/kvm" into the more useful
  # "this machine is not a bare-metal or nested-virt host".
  #
  # Both are *path* overrides rather than a skip switch, so the checks stay live
  # under test — a fixture points them at a CPU without vmx, or at a /dev/kvm
  # that is not there, and the real failure path runs. The defaults are the real
  # files, and nothing here can be turned off, only pointed elsewhere.
  local cpuinfo="${KATA_CPUINFO:-/proc/cpuinfo}"
  local kvm_device="${KATA_KVM_DEVICE:-/dev/kvm}"

  if [[ -r "$cpuinfo" ]] && ! grep -qE '^flags[^:]*:.*[[:space:]](vmx|svm)([[:space:]]|$)' "$cpuinfo"; then
    die "this CPU exposes no vmx/svm flag: Kata needs bare metal or nested virtualization enabled."
  fi
  [[ -e "$kvm_device" ]] || die \
    "${kvm_device} is missing. On a VM guest, enable nested virtualization; on bare metal, enable VT-x/AMD-V in firmware and load the kvm module."
}

require_tools() {
  local missing=()
  local tool
  for tool in curl tar; do
    command -v "$tool" >/dev/null || missing+=("$tool")
  done
  sha256_command >/dev/null || missing+=("sha256sum")
  # The release moved from .tar.xz to .tar.zst, so unpacking needs zstd — either
  # standalone or compiled into tar. Ubuntu 24.04 has tar --zstd once the zstd
  # package is present, which is why both spellings count.
  if ! command -v zstd >/dev/null && ! tar --help 2>/dev/null | grep -q -- '--zstd'; then
    missing+=("zstd")
  fi
  if [[ ${#missing[@]} -gt 0 ]]; then
    die "missing on this host: ${missing[*]} (apt-get install -y ${missing[*]})"
  fi
}

sha256_command() {
  if command -v sha256sum >/dev/null; then
    printf 'sha256sum'
  elif command -v shasum >/dev/null; then
    printf 'shasum -a 256'
  else
    return 1
  fi
}

# ── Install state ────────────────────────────────────────────────────────

installed_version() {
  # Kata writes its own version here; reading it is what makes a repeat run a
  # no-op instead of a 1.5 GB download.
  [[ -r "${PREFIX}/VERSION" ]] || return 1
  tr -d '[:space:]' <"${PREFIX}/VERSION"
}

links_are_current() {
  local name target
  for name in "${LINKED_COMMANDS[@]}"; do
    # Only what the release actually ships. Requiring a link for a command this
    # version omits would make `runtime_is_installed` permanently false, and a repeat
    # run would re-download 1.5 GB every time rather than doing nothing.
    [[ -e "${PREFIX}/bin/${name}" ]] || continue
    target="${KATA_LINK_DIR%/}/${name}"
    [[ -L "$target" ]] || return 1
    [[ "$(readlink "$target")" == "${PREFIX}/bin/${name}" ]] || return 1
  done
  return 0
}

runtime_is_installed() {
  local current
  current="$(installed_version 2>/dev/null)" || return 1
  [[ "$current" == "$KATA_VERSION" ]] || return 1
  [[ -x "$SHIM_BINARY" ]] || return 1
  links_are_current
}

debug_kernel_is_configured() {
  [[ -e "$KATA_DEBUG_KERNEL_PATH" ]] || return 1
  [[ -r "$KATA_CONFIG_PATH" ]] || return 1
  awk -v expected="kernel = \"${KATA_DEBUG_KERNEL_PATH}\"" \
    '$0 == expected { found = 1 } END { exit(found ? 0 : 1) }' \
    "$KATA_CONFIG_PATH"
}

# ── Install ──────────────────────────────────────────────────────────────

resolve_sha256() {
  local digest
  digest="${KATA_SHA256:-$(known_sha256 "$KATA_VERSION")}"
  [[ -n "$digest" ]] || die \
    "no known digest for Kata ${KATA_VERSION}. Read it from the release asset (gh api repos/kata-containers/kata-containers/releases/tags/${KATA_VERSION} --jq '.assets[]|select(.name|test(\"amd64\"))|.digest') and pass it as KATA_SHA256."
  printf '%s' "$digest"
}

download_and_verify() {
  local destination="$1" expected="$2" url actual
  url="${KATA_DOWNLOAD_BASE}/${KATA_VERSION}/kata-static-${KATA_VERSION}-amd64.tar.zst"

  log "downloading Kata ${KATA_VERSION} (about 1.5 GB)"
  curl -fSL --retry 3 --retry-delay 5 -o "$destination" "$url" \
    || die "download failed: ${url}"

  # Unquoted on purpose: `shasum -a 256` has to word-split into argv.
  # shellcheck disable=SC2046
  actual="$($(sha256_command) "$destination" | awk '{print $1}')"
  if [[ "$actual" != "$expected" ]]; then
    # Removed, not left for inspection: a mismatched archive on disk is the one
    # a tired operator unpacks by hand tomorrow.
    rm -f "$destination"
    die "checksum mismatch for Kata ${KATA_VERSION}: expected ${expected}, got ${actual}. Refusing to install."
  fi
  log "checksum verified"
}

extract() {
  local archive="$1"
  log "unpacking into ${PREFIX}"
  # Trailing slash on purpose: the default root trims to the empty string, and
  # `mkdir -p ""` is an error where `mkdir -p /` is a no-op.
  mkdir -p "${KATA_EXTRACT_ROOT%/}/"
  # Members are `./opt/kata/...`, so the root is the extraction target.
  if command -v zstd >/dev/null; then
    zstd -dc "$archive" | tar -C "${KATA_EXTRACT_ROOT%/}/" -xf -
  else
    tar --zstd -C "${KATA_EXTRACT_ROOT%/}/" -xf "$archive"
  fi
  [[ -x "$SHIM_BINARY" ]] || die \
    "unpacked, but ${SHIM_BINARY} is missing or not executable — the archive layout is not what this script expects."
}

link_binaries() {
  local name target
  mkdir -p "${KATA_LINK_DIR%/}"
  for name in "${LINKED_COMMANDS[@]}"; do
    target="${KATA_LINK_DIR%/}/${name}"
    [[ -e "${PREFIX}/bin/${name}" ]] || continue
    ln -sfn "${PREFIX}/bin/${name}" "$target"
  done
  log "linked the shim into ${KATA_LINK_DIR%/} (the daemon finds it there without a unit-file edit)"
}

configure_ebpf_kernel() {
  local config_target temporary
  [[ -e "$KATA_DEBUG_KERNEL_PATH" ]] || die \
    "${KATA_DEBUG_KERNEL_PATH} is missing. Kata ${KATA_VERSION} must include the eBPF/BTF guest kernel asset."
  [[ -r "$KATA_CONFIG_PATH" ]] || die \
    "${KATA_CONFIG_PATH} is missing. Cannot select the Kata guest kernel used by ${KATA_RUNTIME}."
  config_target="$(readlink -f "$KATA_CONFIG_PATH")"
  [[ -n "$config_target" && -f "$config_target" ]] || die \
    "${KATA_CONFIG_PATH} does not resolve to a regular Kata runtime config."
  temporary="$(mktemp "${config_target}.ebpf.XXXXXX")"
  if ! awk -v kernel="$KATA_DEBUG_KERNEL_PATH" '
      !changed && $0 ~ /^[[:space:]]*kernel[[:space:]]*=/ {
        print "kernel = \"" kernel "\""
        changed = 1
        next
      }
      { print }
      END { if (!changed) exit 42 }
    ' "$config_target" >"$temporary"; then
    rm -f "$temporary"
    die "${KATA_CONFIG_PATH} has no active kernel setting to replace."
  fi
  chmod 0644 "$temporary"
  mv -f "$temporary" "$config_target"
  debug_kernel_is_configured || die \
    "failed to configure ${KATA_RUNTIME} to boot ${KATA_DEBUG_KERNEL_PATH}."
  log "configured the Kata eBPF/BTF guest kernel: ${KATA_DEBUG_KERNEL_PATH}"
}

verify_install() {
  # `kata-runtime check` is upstream's own hardware verdict. It is advisory here
  # rather than fatal: it warns about things (a missing kernel module for one
  # hypervisor among several) that do not stop the configured one from working,
  # and the authoritative test is the real container the deploy script starts.
  if [[ -x "$RUNTIME_BINARY" ]]; then
    log "$("$RUNTIME_BINARY" --version 2>&1 | head -1)"
    if ! "$RUNTIME_BINARY" check >/dev/null 2>&1; then
      warn "'kata-runtime check' reported problems; run '${RUNTIME_BINARY} check' to see them."
    fi
  fi
}

cmd_install() {
  require_linux_x86_64
  require_virtualization
  require_tools

  if runtime_is_installed; then
    if ! debug_kernel_is_configured; then
      configure_ebpf_kernel
    fi
    log "Kata ${KATA_VERSION} is already installed, linked, and observer-ready; nothing to download."
    verify_install
    return 0
  fi

  local current
  if current="$(installed_version 2>/dev/null)" && [[ -n "$current" ]]; then
    log "replacing Kata ${current} with ${KATA_VERSION}"
  fi

  # The default root trims to the empty string, which is exactly the case that
  # writes to the real /opt/kata. A test root is a non-empty path and needs no
  # privilege, which is what keeps this script runnable under pytest.
  if [[ "${KATA_EXTRACT_ROOT%/}" == "" && "$(id -u)" != "0" ]]; then
    die "installing into /opt/kata needs root: re-run with sudo."
  fi

  local expected workdir archive
  expected="$(resolve_sha256)"
  workdir="$(mktemp -d)"
  # shellcheck disable=SC2064  # workdir is expanded now on purpose.
  trap "rm -rf '${workdir}'" EXIT
  archive="${workdir}/kata-static.tar.zst"

  download_and_verify "$archive" "$expected"
  extract "$archive"
  link_binaries
  configure_ebpf_kernel
  verify_install

  log "done. Kata ${KATA_VERSION} is installed; '${0} --check' now passes."
  log "next: remove any AGENT_SANDBOX_BACKEND=container override from .env, then redeploy the runner."
}

cmd_check() {
  local current
  require_linux_x86_64

  if ! current="$(installed_version 2>/dev/null)" || [[ -z "$current" ]]; then
    die "Kata is not installed on this host (${PREFIX} has no VERSION). Install it with: sudo ${0}"
  fi
  if [[ "$current" != "$KATA_VERSION" ]]; then
    die "this host has Kata ${current}, but the pin is ${KATA_VERSION}. Re-run: sudo ${0}"
  fi
  [[ -x "$SHIM_BINARY" ]] || die \
    "${SHIM_BINARY} is missing or not executable. Re-run: sudo ${0}"
  links_are_current || die \
    "the ${KATA_RUNTIME} shim is not linked into ${KATA_LINK_DIR%/}, so the Docker daemon cannot find it. Re-run: sudo ${0}"
  [[ -e "$KATA_DEBUG_KERNEL_PATH" ]] || die \
    "the packaged eBPF/BTF guest kernel is missing at ${KATA_DEBUG_KERNEL_PATH}. Re-run: sudo ${0}"
  debug_kernel_is_configured || die \
    "${KATA_RUNTIME} is not configured to boot the packaged eBPF/BTF kernel. Re-run: sudo ${0}"
  require_virtualization

  log "Kata ${current} is installed, observer-ready, and this host can start VMs."
}

case "${1:-install}" in
  --check | check) cmd_check ;;
  # 32 is the last header line; 33 onwards is `set -Eeuo pipefail` and the
  # version-pin rationale, which is not usage text.
  --help | -h) sed -n '2,32p' "$0" ;;
  install) cmd_install ;;
  *) die "usage: $0 [install|--check]" ;;
esac
