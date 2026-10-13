#!/usr/bin/env bash
#
# Install one agent CLI at every version this image offers, and put the default
# on PATH.
#
#   install-runtimes.sh <runtime-id> <npm-package> <binary> <versions-csv>
#
# Build-time only, and deleted from the image afterwards: an agent that can
# install another CLI version into its own sandbox is an agent that can escape
# the version stamp.
#
# **Every version lives at its own prefix**, keyed by the runtime id the control
# plane knows it by:
#
#   /opt/runtimes/<runtime-id>/<version>/bin/<binary>
#
# so the runner selects one by prepending a directory to the sandbox's PATH and
# nothing else has to change. All five CLIs resolve their binary through PATH —
# `binary = "claude"` in runtimes.py, and the pi/opencode/kilo wrappers end in
# `os.execvp("pi", ...)` — so the adapters and the wrappers stay untouched by
# version selection. That is the whole reason for this layout.
#
# **The first version in the list is the default** and is symlinked onto PATH.
# A job whose attempt carries no version — an older control plane, a host that
# never reported its menu — gets it, which is exactly the behaviour of the image
# before it could hold more than one.
#
# **Each version is verified against its own binary before it counts.** The
# labels this image carries are derived from these same arguments, and the
# version-stamp pipeline treats a label as fact: the heartbeat reports it, the
# claim writes it onto every attempt, and native-session admission compares it before
# letting a thread resume its session. A dist-tag or a range would install
# something the label does not describe, so it is refused here rather than
# discovered later in a thread that would otherwise resume with the wrong binary.
set -euo pipefail

runtime_id="${1:?runtime id}"
package="${2:?npm package}"
binary="${3:?binary name}"
versions="${4:?comma-separated versions}"

root="/opt/runtimes/${runtime_id}"
default=""

# Whether `--version` output names exactly this version, as a whole token:
# `2.1` is not in `2.1.220`, nor `0.145.0` in `10.145.0` or `0.145.0-alpha.1`.
# Shared, character for character, with the image's final check in
# Dockerfile.agent-sandbox, which runs under `sh`.
reports_version() {
    case " $1 " in
        *[!0-9.]"$2"[!0-9A-Za-z.+-]*) return 0 ;;
    esac
    return 1
}

# `read -a` on a comma-separated string, so an empty entry from a stray comma
# becomes a loud failure below rather than an `npm install pkg@`.
IFS=',' read -r -a wanted <<<"${versions}"

for version in "${wanted[@]}"; do
    if [ -z "${version}" ]; then
        echo "ERROR: ${runtime_id} has an empty version in '${versions}'." >&2
        echo "A stray comma would otherwise install whatever 'pkg@' resolves to." >&2
        exit 1
    fi
    # npm reads a partial version such as `2.1` as a range, and a dist-tag or a
    # range installs whatever it resolves to today; only an exact version is
    # what its label claims.
    if ! [[ "${version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$ ]]; then
        echo "ERROR: ${runtime_id} version '${version}' is not an exact version." >&2
        echo "Name MAJOR.MINOR.PATCH (optionally -PRERELEASE): a partial version, a" >&2
        echo "range or a dist-tag makes the version label, and every record derived" >&2
        echo "from it, a lie." >&2
        exit 1
    fi

    prefix="${root}/${version}"
    if [ "${runtime_id}" = kilo ]; then
        # Every selectable Kilo version must carry the reviewed isolation fix.
        # Refuse an unreviewed extra version at build time, before advertising it.
        test "${version}" = 7.4.17
        mkdir -p "${prefix}"
        cp -a /opt/kilo/. "${prefix}/"
        ln -sf kilo "${prefix}/bin/kilocode"
    else
        npm install -g --prefix "${prefix}" "${package}@${version}"
    fi

    got="$("${prefix}/bin/${binary}" --version 2>&1 | tail -1)"
    if reports_version "${got}" "${version}"; then
        echo "${runtime_id} ${version}: ${got}"
    else
        echo "ERROR: ${runtime_id} ${version} installed a binary reporting '${got}'." >&2
        echo "A runtime version must be exact — a dist-tag or a range makes the" >&2
        echo "version label, and every record derived from it, a lie." >&2
        exit 1
    fi

    if [ -z "${default}" ]; then
        default="${version}"
    fi
done

# The default onto PATH. Symlinked rather than installed twice: these binaries
# are large (claude-code alone is ~260MB), and npm's bin entries resolve their
# package through their own real path, so a link behaves exactly like the file.
for path in "${root}/${default}/bin/"*; do
    ln -sf "${path}" "/usr/local/bin/$(basename "${path}")"
done
