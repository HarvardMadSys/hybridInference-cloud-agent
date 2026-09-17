#!/usr/bin/env bash
# Build the reviewed Kilo source and apply the project-config isolation fix.
set -euo pipefail

source_revision=a0364858a6e1b69a2e2dc5434a82d5cefbe79ea7
source_dir="${1:?source directory}"
patch_file="${2:?isolation patch}"
output_dir="${3:?output directory}"

# Use the same Zig release as Kilo's Linux release workflow; verify the
# architecture-specific archive against the official release checksum.
case "$(uname -m)" in
    x86_64) zig_arch=x86_64; zig_sha=473ec26806133cf4d1918caf1a410f8403a13d979726a9045b421b685031a982 ;;
    aarch64) zig_arch=aarch64; zig_sha=ab64e3ea277f6fc5f3d723dcd95d9ce1ab282c8ed0f431b4de880d30df891e4f ;;
    *) echo 'Unsupported Kilo build architecture' >&2; exit 1 ;;
esac
curl --fail --location --retry 3 \
    "https://ziglang.org/download/0.14.0/zig-linux-${zig_arch}-0.14.0.tar.xz" -o /tmp/zig.tar.xz
printf '%s  %s\n' "${zig_sha}" /tmp/zig.tar.xz | sha256sum --check --status
mkdir -p /opt/zig
tar -xJf /tmp/zig.tar.xz --strip-components=1 -C /opt/zig
export ZIG=/opt/zig/zig

git init "${source_dir}"
git -C "${source_dir}" remote add origin https://github.com/Kilo-Org/kilocode.git
git -C "${source_dir}" fetch --depth 1 origin "${source_revision}"
git -C "${source_dir}" -c filter.lfs.smudge= -c filter.lfs.required=false \
    -c filter.lfs.process= checkout --detach FETCH_HEAD
test "$(git -C "${source_dir}" rev-parse HEAD)" = "${source_revision}"
git -C "${source_dir}" apply --check "${patch_file}"
git -C "${source_dir}" apply "${patch_file}"
cd "${source_dir}"
bun install --frozen-lockfile --filter @kilocode/cli --filter @kilocode/kilo-console
KILO_VERSION=7.4.17 KILO_CHANNEL=latest \
    bun run packages/opencode/script/build.ts --single --skip-install
platform="$(bun -e 'process.stdout.write(process.platform + "-" + process.arch)')"
mkdir -p "${output_dir}"
cp -a "packages/opencode/dist/@kilocode/cli-${platform}/bin" "${output_dir}/bin"
cp LICENSE "${output_dir}/LICENSE"
printf '%s\n' "${source_revision}" > "${output_dir}/source-revision"
sha256sum "${patch_file}" | cut -d ' ' -f 1 > "${output_dir}/mcp-isolation-patch.sha256"
test "$("${output_dir}/bin/kilo" --version | tail -1)" = 7.4.17
