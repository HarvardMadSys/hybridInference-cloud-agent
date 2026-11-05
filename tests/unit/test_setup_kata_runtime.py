"""Tests for the Kata Containers host provisioning script.

The script installs the runtime that puts each agent sandbox behind its own
kernel, so the cases that matter are the ones where it must *refuse*: an
unverifiable download, a host that cannot start a VM, a version that does not
match the pin. Each is exercised by running the real script with the host facts
it reads pointed at fixtures — `uname` faked on PATH, `/proc/cpuinfo` and
`/dev/kvm` redirected — rather than by stubbing out its logic.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP_SCRIPT = REPO_ROOT / "deploy/host/setup_kata_runtime.sh"

PINNED_VERSION = "3.32.0"
_HAS_ZSTD = shutil.which("zstd") is not None


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _fake_bin(tmp_path: Path) -> Path:
    """A PATH front-end that makes this machine look like an x86_64 Linux host.

    Only `uname` and `curl` are faked. Everything else the script calls (tar,
    zstd, shasum, grep) is the real thing, so the archive really is unpacked and
    the digest really is computed. `_host_path` covers a host with no zstd.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    _write_executable(
        fake_bin / "uname",
        '#!/bin/sh\ncase "$1" in\n  -m) echo x86_64 ;;\n  *) echo Linux ;;\nesac\n',
    )
    _write_executable(
        fake_bin / "curl",
        "#!/bin/sh\n"
        'printf "download\\n" >> "$CURL_LOG"\n'
        'dest=""\n'
        "while [ $# -gt 0 ]; do\n"
        '  case "$1" in\n'
        '    -o) dest="$2"; shift 2 ;;\n'
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        '[ -n "${FAKE_TARBALL:-}" ] && cp "$FAKE_TARBALL" "$dest"\n'
        "exit 0\n",
    )
    return fake_bin


def _host_path(tmp_path: Path, *, zstd: bool) -> str:
    """This machine's PATH, with zstd present or absent as the test needs.

    Every test that unpacks needs the real zstd and is skipped without it. The
    refusals that come after the tool check only need the script to find one,
    so a host without zstd gets a stand-in that fails if anything runs it.
    Absent means every other command on PATH, linked one by one: this host
    minus zstd, whatever else it has.
    """
    if zstd:
        if _HAS_ZSTD:
            return os.environ["PATH"]
        stand_in = tmp_path / "zstd-stand-in"
        stand_in.mkdir(exist_ok=True)
        _write_executable(
            stand_in / "zstd", "#!/bin/sh\necho 'stand-in zstd: this host has none' >&2\nexit 1\n"
        )
        return f"{stand_in}:{os.environ['PATH']}"
    mirror = tmp_path / "path-without-zstd"
    mirror.mkdir(exist_ok=True)
    for directory in os.environ["PATH"].split(os.pathsep):
        if not os.path.isdir(directory):
            continue
        with os.scandir(directory) as entries:
            for entry in entries:
                link = mirror / entry.name
                if entry.name != "zstd" and not os.path.lexists(link):
                    link.symlink_to(os.path.abspath(entry.path))
    return str(mirror)


def _cpuinfo(tmp_path: Path, *, virtualized: bool = True) -> Path:
    """A /proc/cpuinfo stand-in, with or without the virtualization flag."""
    flags = "fpu vme de pse tsc msr"
    if virtualized:
        flags += " vmx"
    path = tmp_path / "cpuinfo"
    path.write_text(f"processor\t: 0\nmodel name\t: Test CPU\nflags\t\t: {flags} pae mce\n")
    return path


def _kvm_device(tmp_path: Path) -> Path:
    """A /dev/kvm stand-in. Its presence is all the script checks."""
    path = tmp_path / "kvm"
    path.write_text("")
    return path


def _release_tree(base: Path, *, version: str = PINNED_VERSION) -> Path:
    """Write the `opt/kata` tree a kata-static release unpacks to, under `base`.

    Returns the default runtime config as the release ships it.
    """
    bin_dir = base / "opt" / "kata" / "bin"
    bin_dir.mkdir(parents=True)
    (base / "opt" / "kata" / "VERSION").write_text(f"{version}\n")
    _write_executable(bin_dir / "containerd-shim-kata-v2", "#!/bin/sh\nexit 0\n")
    _write_executable(
        bin_dir / "kata-runtime",
        f'#!/bin/sh\ncase "$1" in\n  --version) echo "kata-runtime  : {version}" ;;\n'
        "  check) exit 0 ;;\nesac\n",
    )
    _write_executable(bin_dir / "kata-collect-data.sh", "#!/bin/sh\nexit 0\n")
    kernel_dir = base / "opt" / "kata" / "share" / "kata-containers"
    kernel_dir.mkdir(parents=True)
    (kernel_dir / "vmlinuz-debug.container").write_bytes(b"debug kernel with BPF and BTF")
    config_dir = base / "opt" / "kata" / "share" / "defaults" / "kata-containers"
    config_dir.mkdir(parents=True)
    (config_dir / "configuration-qemu.toml").write_text(
        '[hypervisor.qemu]\nkernel = "/opt/kata/share/kata-containers/vmlinuz.container"\n'
        "[runtime]\ndisable_guest_seccomp = true\n"
    )
    (config_dir / "configuration.toml").symlink_to("configuration-qemu.toml")
    return config_dir / "configuration-qemu.toml"


def _fake_release_tarball(tmp_path: Path, *, version: str = PINNED_VERSION) -> Path:
    """Build an archive shaped like a real kata-static release.

    Members are `./opt/kata/...` — verified against the published 3.32.0
    tarball — which is what makes the script's `tar -C /` correct.
    """
    staging = tmp_path / "staging"
    _release_tree(staging, version=version)
    archive = tmp_path / f"kata-static-{version}-amd64.tar.zst"
    subprocess.run(
        f"tar -C {staging} -cf - ./opt | zstd -q -o {archive}",
        shell=True,
        check=True,
    )
    return archive


def _run(
    tmp_path: Path,
    *args: str,
    env_overrides: dict[str, str] | None = None,
    tarball: Path | None = None,
    virtualized: bool = True,
    kvm: bool = True,
    zstd: bool = True,
    umask: int | None = None,
) -> subprocess.CompletedProcess[str]:
    fake_bin = _fake_bin(tmp_path)
    install_root = tmp_path / "root"
    install_root.mkdir(mode=0o755, exist_ok=True)
    link_dir = tmp_path / "usr-local-bin"
    link_dir.mkdir(exist_ok=True)

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{_host_path(tmp_path, zstd=zstd)}",
        "KATA_EXTRACT_ROOT": str(install_root),
        "KATA_LINK_DIR": str(link_dir),
        "KATA_CPUINFO": str(_cpuinfo(tmp_path, virtualized=virtualized)),
        "KATA_KVM_DEVICE": str(_kvm_device(tmp_path) if kvm else tmp_path / "no-kvm"),
        "CURL_LOG": str(tmp_path / "curl.log"),
        **({"FAKE_TARBALL": str(tarball)} if tarball else {}),
        **(env_overrides or {}),
    }
    return subprocess.run(
        ["bash", str(SETUP_SCRIPT), *args],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        umask=-1 if umask is None else umask,
    )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _download_count(tmp_path: Path) -> int:
    log = tmp_path / "curl.log"
    return len(log.read_text().splitlines()) if log.exists() else 0


# ── Refusals: the cases where installing anyway would be the bug ──────────


def test_a_checksum_mismatch_installs_nothing(tmp_path: Path) -> None:
    """Upstream publishes no checksum file, so the pin is the only guard.

    An archive that does not match it is a supply-chain question, not a retry:
    this becomes the kernel every agent job runs under.
    """
    tarball = tmp_path / "wrong.tar.zst"
    tarball.write_bytes(b"not the release you pinned")

    result = _run(tmp_path, tarball=tarball, env_overrides={"KATA_SHA256": "00" * 32})

    assert result.returncode != 0
    assert "checksum mismatch" in result.stderr
    assert not (tmp_path / "root" / "opt" / "kata").exists()
    # And the bad archive is gone, not left for someone to unpack by hand.
    assert not list(tmp_path.glob("**/kata-static.tar.zst"))


def test_an_unknown_version_without_a_digest_refuses(tmp_path: Path) -> None:
    """Refusing beats falling back to an unverified download.

    Bumping KATA_VERSION and forgetting the digest is the easy mistake; silently
    skipping verification would make it an invisible one.
    """
    result = _run(tmp_path, env_overrides={"KATA_VERSION": "9.9.9"})

    assert result.returncode != 0
    assert "no known digest" in result.stderr
    assert _download_count(tmp_path) == 0, "it downloaded before deciding it could not verify"


def test_a_host_without_kvm_is_refused_before_downloading(tmp_path: Path) -> None:
    """Kata needs a VM. Without /dev/kvm every job would fail at spawn."""
    result = _run(tmp_path, kvm=False)

    assert result.returncode != 0
    assert "is missing" in result.stderr
    assert "nested virtualization" in result.stderr
    assert _download_count(tmp_path) == 0


def test_a_cpu_without_virtualization_extensions_is_refused(tmp_path: Path) -> None:
    """A guest with no nested virt gives a clearer error than a KVM failure."""
    result = _run(tmp_path, virtualized=False)

    assert result.returncode != 0
    assert "vmx/svm" in result.stderr
    assert _download_count(tmp_path) == 0


def test_a_host_without_zstd_is_refused_before_downloading(tmp_path: Path) -> None:
    """A tar that lists --zstd cannot unpack the release without the zstd program.

    GNU tar's --zstd runs that program. Taking the flag as enough downloaded
    1.5 GB and marked the runtime as updating before tar failed.
    """
    result = _run(tmp_path, zstd=False)

    assert result.returncode != 0
    assert "missing on this host: zstd" in result.stderr
    assert _download_count(tmp_path) == 0
    assert not (tmp_path / "root/var/lib/freeinference/kata-runtime/epoch.json").exists()


def test_check_refuses_a_host_that_has_no_kata(tmp_path: Path) -> None:
    """What deployment calls. It must never install as a side effect."""
    result = _run(tmp_path, "--check")

    assert result.returncode != 0
    assert "Kata is not installed" in result.stderr
    assert _download_count(tmp_path) == 0
    assert not (tmp_path / "root" / "opt").exists()
    assert not (tmp_path / "root/var").exists(), "--check must not initialize epoch state"


# ── The install path ─────────────────────────────────────────────────────


@pytest.mark.skipif(not _HAS_ZSTD, reason="zstd is needed to build a release-shaped fixture")
def test_installs_links_the_shim_and_passes_its_own_check(tmp_path: Path) -> None:
    """A successful install leaves the daemon able to find the shim.

    The symlink is the load-bearing part: the daemon resolves
    `io.containerd.kata.v2` to a `containerd-shim-kata-v2` on its own PATH, so
    an unpacked-but-unlinked tree is a host where every job still fails.
    """
    tarball = _fake_release_tarball(tmp_path)

    result = _run(tmp_path, tarball=tarball, env_overrides={"KATA_SHA256": _digest(tarball)})

    assert result.returncode == 0, result.stderr
    prefix = tmp_path / "root" / "opt" / "kata"
    assert (prefix / "VERSION").read_text().strip() == PINNED_VERSION
    shim = tmp_path / "usr-local-bin" / "containerd-shim-kata-v2"
    assert shim.is_symlink()
    assert os.readlink(shim) == str(prefix / "bin" / "containerd-shim-kata-v2")
    config = prefix / "share" / "defaults" / "kata-containers" / "configuration-qemu.toml"
    assert config.read_text() == (
        f'[hypervisor.qemu]\nkernel = "{prefix}/share/kata-containers/vmlinuz-debug.container"\n'
        "[runtime]\ndisable_guest_seccomp = false\n"
    )

    # And the deploy-time check now passes on this host.
    epoch = tmp_path / "root/var/lib/freeinference/kata-runtime/epoch.json"
    before = epoch.read_bytes()
    assert json.loads(before)["state"] == "ready"
    assert _run(tmp_path, "--check").returncode == 0
    assert epoch.read_bytes() == before, "verification must not change the runtime generation"


@pytest.mark.skipif(not _HAS_ZSTD, reason="zstd is needed to build a release-shaped fixture")
def test_install_refuses_a_release_without_the_ebpf_guest_kernel(tmp_path: Path) -> None:
    """VM isolation alone does not make guest lifecycle tracepoints available."""
    tarball = _fake_release_tarball(tmp_path)
    staging_kernel = tmp_path / "staging" / "opt" / "kata" / "share" / "kata-containers"
    (staging_kernel / "vmlinuz-debug.container").unlink()
    subprocess.run(
        f"tar -C {tmp_path / 'staging'} -cf - ./opt | zstd -q -f -o {tarball}",
        shell=True,
        check=True,
    )

    result = _run(tmp_path, tarball=tarball, env_overrides={"KATA_SHA256": _digest(tarball)})

    assert result.returncode != 0
    assert "eBPF/BTF guest kernel" in result.stderr
    marker = tmp_path / "root/var/lib/freeinference/kata-runtime/epoch.json"
    assert json.loads(marker.read_text())["state"] == "updating"
    before = marker.read_bytes()
    assert _run(tmp_path, "--check").returncode != 0
    assert marker.read_bytes() == before


@pytest.mark.skipif(not _HAS_ZSTD, reason="zstd is needed to build a release-shaped fixture")
def test_a_second_run_downloads_nothing(tmp_path: Path) -> None:
    """Provisioning is re-run on every host rebuild; 1.5 GB each time is not it."""
    tarball = _fake_release_tarball(tmp_path)
    env = {"KATA_SHA256": _digest(tarball)}

    assert _run(tmp_path, tarball=tarball, env_overrides=env).returncode == 0
    assert _download_count(tmp_path) == 1
    marker = tmp_path / "root/var/lib/freeinference/kata-runtime/epoch.json"
    previous_generation = json.loads(marker.read_text())["generation"]

    second = _run(tmp_path, tarball=tarball, env_overrides=env)

    assert second.returncode == 0
    assert "already installed" in second.stdout
    assert _download_count(tmp_path) == 1, "a repeat run re-downloaded the release"
    current = json.loads(marker.read_text())
    assert current["state"] == "ready"
    assert current["generation"] != previous_generation


@pytest.mark.skipif(not _HAS_ZSTD, reason="zstd is needed to build a release-shaped fixture")
def test_epoch_is_updating_before_the_first_runtime_write(tmp_path: Path) -> None:
    tarball = _fake_release_tarball(tmp_path)
    real_tar = shutil.which("tar")
    fake_bin = _fake_bin(tmp_path)
    _write_executable(
        fake_bin / "tar",
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "if '-xf' in sys.argv:\n"
        "    marker = pathlib.Path(os.environ['KATA_EXTRACT_ROOT']) / "
        "'var/lib/freeinference/kata-runtime/epoch.json'\n"
        "    assert json.loads(marker.read_text())['state'] == 'updating'\n"
        f"os.execv({real_tar!r}, [{real_tar!r}, *sys.argv[1:]])\n",
    )
    result = _run(tmp_path, tarball=tarball, env_overrides={"KATA_SHA256": _digest(tarball)})
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not _HAS_ZSTD, reason="zstd is needed to build a release-shaped fixture")
def test_check_rejects_disabled_guest_seccomp_and_install_repairs_without_download(tmp_path):
    """A BTF kernel with Kata ignoring the OCI filter must not pass readiness."""
    tarball = _fake_release_tarball(tmp_path)
    env = {"KATA_SHA256": _digest(tarball)}
    assert _run(tmp_path, tarball=tarball, env_overrides=env).returncode == 0
    config = tmp_path / "root/opt/kata/share/defaults/kata-containers/configuration-qemu.toml"
    config.write_text(
        config.read_text().replace("disable_guest_seccomp = false", "disable_guest_seccomp = true")
    )
    before = config.read_bytes()
    checked = _run(tmp_path, "--check")
    assert checked.returncode != 0
    assert "does not enforce guest seccomp" in checked.stderr
    assert config.read_bytes() == before
    assert _run(tmp_path, env_overrides=env).returncode == 0
    assert _run(tmp_path, "--check").returncode == 0
    assert _download_count(tmp_path) == 1


@pytest.mark.skipif(not _HAS_ZSTD, reason="zstd is needed to build a release-shaped fixture")
def test_check_refuses_a_host_whose_shim_is_not_linked(tmp_path: Path) -> None:
    """Unpacked but unlinked is the failure that looks like success.

    /opt/kata is fully populated, so anyone eyeballing the host concludes Kata
    is installed — while the daemon still cannot resolve the runtime.
    """
    tarball = _fake_release_tarball(tmp_path)
    assert (
        _run(tmp_path, tarball=tarball, env_overrides={"KATA_SHA256": _digest(tarball)}).returncode
        == 0
    )
    (tmp_path / "usr-local-bin" / "containerd-shim-kata-v2").unlink()

    result = _run(tmp_path, "--check")

    assert result.returncode != 0
    assert "not linked" in result.stderr


@pytest.mark.skipif(not _HAS_ZSTD, reason="zstd is needed to build a release-shaped fixture")
def test_check_refuses_a_host_pinned_to_a_different_version(tmp_path: Path) -> None:
    """A host left on an old release must not read as provisioned.

    Upgrading the pin is how a Kata CVE gets rolled out, so deployment has to
    notice the host that never got it.
    """
    tarball = _fake_release_tarball(tmp_path)
    assert (
        _run(tmp_path, tarball=tarball, env_overrides={"KATA_SHA256": _digest(tarball)}).returncode
        == 0
    )

    result = _run(tmp_path, "--check", env_overrides={"KATA_VERSION": "4.0.0"})

    assert result.returncode != 0
    assert PINNED_VERSION in result.stderr
    assert "4.0.0" in result.stderr


def test_help_lists_every_override_and_opt_in(tmp_path: Path) -> None:
    result = _run(tmp_path, "--help")

    assert result.returncode == 0
    for name in ("KATA_CONFIG_PATH", "KATA_STATIC_SANDBOX_RESOURCES", "KATA_DEFAULT_MEMORY_MIB"):
        assert name in result.stdout
    assert "set -Eeuo pipefail" not in result.stdout


# ── VM boot opt-ins (#1054) ──────────────────────────────────────────────

BOOT_DROP_IN = "config.d/cloud-agent-vm-boot.toml"
EPOCH = "root/var/lib/freeinference/kata-runtime/epoch.json"


def _provisioned_host(tmp_path: Path) -> Path:
    """A host as an install left it before the boot opt-ins existed.

    The release's tree is laid out directly rather than unpacked, so these
    tests need no zstd. Its config carries the 3.32.0 release's own values for
    the two settings the opt-ins change, and there is no `config.d`. Returns
    that config.
    """
    root = tmp_path / "root"
    root.mkdir(mode=0o755)
    config = _release_tree(root)
    kernel = root / "opt/kata/share/kata-containers/vmlinuz-debug.container"
    config.write_text(
        f'[hypervisor.qemu]\nkernel = "{kernel}"\ndefault_memory = 2048\n'
        "[runtime]\ndisable_guest_seccomp = false\nstatic_sandbox_resource_mgmt = false\n"
    )
    link_dir = tmp_path / "usr-local-bin"
    link_dir.mkdir()
    for name in ("containerd-shim-kata-v2", "kata-runtime", "kata-collect-data.sh"):
        (link_dir / name).symlink_to(root / "opt/kata/bin" / name)
    return config


def _settings(drop_in: Path) -> str:
    """The drop-in without its comment: what Kata applies."""
    lines = drop_in.read_text().splitlines(keepends=True)
    return "".join(line for line in lines if not line.startswith("#"))


def test_a_host_provisioned_before_the_opt_ins_passes_its_check_unchanged(tmp_path: Path) -> None:
    """Deployment checks every host; one that never opted in reads as it did."""
    config = _provisioned_host(tmp_path)
    before = config.read_bytes()

    result = _run(tmp_path, "--check")

    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        f"[kata-setup] Kata {PINNED_VERSION} is installed, observer-ready, and this host can "
        "start VMs.\n"
    )
    assert config.read_bytes() == before
    assert not (config.parent / "config.d").exists()


@pytest.mark.parametrize(
    ("opt_ins", "settings", "in_effect"),
    [
        pytest.param(
            {"KATA_STATIC_SANDBOX_RESOURCES": "true"},
            "[runtime]\nstatic_sandbox_resource_mgmt = true\n",
            "static sandbox sizing",
            id="static-sizing",
        ),
        pytest.param(
            {"KATA_DEFAULT_MEMORY_MIB": "1024"},
            "[hypervisor.qemu]\ndefault_memory = 1024\n",
            "default_memory 1024 MiB",
            id="default-memory",
        ),
        pytest.param(
            {"KATA_STATIC_SANDBOX_RESOURCES": "true", "KATA_DEFAULT_MEMORY_MIB": "256"},
            "[hypervisor.qemu]\ndefault_memory = 256\n[runtime]\nstatic_sandbox_resource_mgmt = true\n",
            "static sandbox sizing, default_memory 256 MiB",
            id="both-at-katas-floor",
        ),
    ],
)
def test_install_records_its_opt_ins_and_check_reads_them_back(
    tmp_path: Path, opt_ins: dict[str, str], settings: str, in_effect: str
) -> None:
    """The drop-in is both the setting and its record, so deployment passes nothing."""
    config = _provisioned_host(tmp_path)
    before = config.read_bytes()

    installed = _run(tmp_path, env_overrides=opt_ins)

    assert installed.returncode == 0, installed.stderr
    assert _download_count(tmp_path) == 0
    assert _settings(config.parent / BOOT_DROP_IN) == settings
    assert config.read_bytes() == before, (
        "the opt-ins belong in the drop-in, not the release's config"
    )
    assert json.loads((tmp_path / EPOCH).read_text())["state"] == "ready"

    checked = _run(tmp_path, "--check")

    assert checked.returncode == 0, checked.stderr
    assert f"VM boot opt-ins in effect: {in_effect}\n" in checked.stdout


@pytest.mark.parametrize(
    "without",
    [
        pytest.param({}, id="left-out"),
        pytest.param({"KATA_STATIC_SANDBOX_RESOURCES": "false"}, id="false"),
    ],
)
def test_an_install_without_opt_ins_restores_katas_defaults(
    tmp_path: Path, without: dict[str, str]
) -> None:
    """A re-run that leaves them out lands on the qualified defaults, not on a mix."""
    config = _provisioned_host(tmp_path)
    before = config.read_bytes()
    opt_ins = {"KATA_STATIC_SANDBOX_RESOURCES": "true", "KATA_DEFAULT_MEMORY_MIB": "1024"}
    assert _run(tmp_path, env_overrides=opt_ins).returncode == 0
    opted_in = json.loads((tmp_path / EPOCH).read_text())["generation"]

    restored = _run(tmp_path, env_overrides=without)

    assert restored.returncode == 0, restored.stderr
    # Said out loud: the control plane's automated deploy reverts them this way.
    assert "removed the VM boot opt-ins" in restored.stderr
    assert not (config.parent / "config.d").exists()
    assert config.read_bytes() == before
    current = json.loads((tmp_path / EPOCH).read_text())
    assert current["state"] == "ready"
    assert current["generation"] != opted_in, "runners must requalify the changed runtime"
    checked = _run(tmp_path, "--check")
    assert checked.returncode == 0, checked.stderr
    assert "opt-ins" not in checked.stdout
    again = _run(tmp_path, env_overrides=without)
    assert again.returncode == 0, again.stderr
    assert "removed" not in again.stderr, "nothing was there to remove"


def test_the_drop_in_stays_readable_whatever_umask_sudo_passes_on(tmp_path: Path) -> None:
    """Deployment runs `--check` as a user who must still be able to read it."""
    config = _provisioned_host(tmp_path)

    installed = _run(tmp_path, env_overrides={"KATA_STATIC_SANDBOX_RESOURCES": "true"}, umask=0o077)

    assert installed.returncode == 0, installed.stderr
    drop_in = config.parent / BOOT_DROP_IN
    assert stat.S_IMODE(drop_in.parent.stat().st_mode) == 0o755
    assert stat.S_IMODE(drop_in.stat().st_mode) == 0o644


@pytest.mark.parametrize(
    "settings",
    [
        pytest.param(
            "[runtime]\nstatic_sandbox_resource_mgmt = true\n[factory]\nenable_template = true\n",
            id="templating-added",
        ),
        pytest.param(
            "[runtime]\nstatic_sandbox_resource_mgmt = true\n[hypervisor.qemu]\ndefault_memory = 1024\n",
            id="reordered",
        ),
        pytest.param("[hypervisor.qemu]\ndefault_memory = 128\n", id="below-katas-floor"),
        pytest.param("", id="no-opt-in"),
        pytest.param(None, id="empty-file"),
    ],
)
def test_check_refuses_a_drop_in_the_install_did_not_write(
    tmp_path: Path, settings: str | None
) -> None:
    """The drop-in carries the opt-ins and nothing else: VM templating stays off."""
    config = _provisioned_host(tmp_path)
    assert _run(tmp_path, env_overrides={"KATA_STATIC_SANDBOX_RESOURCES": "true"}).returncode == 0
    drop_in = config.parent / BOOT_DROP_IN
    comment = drop_in.read_text().splitlines(keepends=True)[0]
    drop_in.write_text("" if settings is None else comment + settings)
    before = drop_in.read_bytes()

    result = _run(tmp_path, "--check")

    assert result.returncode != 0
    assert "is not what this script writes" in result.stderr
    assert drop_in.read_bytes() == before


def test_check_refuses_any_other_drop_in(tmp_path: Path) -> None:
    """Kata applies every file in config.d, so `--check` refuses one it did not write.

    Another could turn guest seccomp back off underneath a check that reads
    only the main config.
    """
    config = _provisioned_host(tmp_path)
    foreign = config.parent / "config.d" / "99-local.toml"
    foreign.parent.mkdir()
    foreign.write_text("[runtime]\ndisable_guest_seccomp = true\n")

    result = _run(tmp_path, "--check")

    assert result.returncode != 0
    assert "99-local.toml was not written by this script" in result.stderr


def test_check_given_an_opt_in_requires_this_host_to_have_it(tmp_path: Path) -> None:
    """Told an opt-in, the check confirms it instead of reading back the drop-in."""
    _provisioned_host(tmp_path)

    unsized = _run(tmp_path, "--check", env_overrides={"KATA_STATIC_SANDBOX_RESOURCES": "true"})

    assert unsized.returncode != 0
    assert "static sandbox sizing is false on this host, not true" in unsized.stderr

    assert _run(tmp_path, env_overrides={"KATA_DEFAULT_MEMORY_MIB": "1024"}).returncode == 0
    other = _run(tmp_path, "--check", env_overrides={"KATA_DEFAULT_MEMORY_MIB": "512"})

    assert other.returncode != 0
    assert "default_memory is 1024 on this host, not 512" in other.stderr
    for agreeing in (
        {"KATA_DEFAULT_MEMORY_MIB": "1024"},
        {"KATA_STATIC_SANDBOX_RESOURCES": "false"},
    ):
        assert _run(tmp_path, "--check", env_overrides=agreeing).returncode == 0


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("KATA_STATIC_SANDBOX_RESOURCES", "yes"),
        ("KATA_DEFAULT_MEMORY_MIB", "255"),
        ("KATA_DEFAULT_MEMORY_MIB", "0512"),
        ("KATA_DEFAULT_MEMORY_MIB", "1g"),
        ("KATA_DEFAULT_MEMORY_MIB", "10000000"),
    ],
)
def test_a_malformed_opt_in_is_refused_before_anything_changes(
    tmp_path: Path, name: str, value: str
) -> None:
    """A typo must not quietly mean "off", nor a size Kata would replace with 2048 MiB."""
    config = _provisioned_host(tmp_path)
    before = config.read_bytes()

    installed = _run(tmp_path, env_overrides={name: value})

    assert installed.returncode != 0
    assert f"{name} must be" in installed.stderr
    assert config.read_bytes() == before
    assert not (config.parent / "config.d").exists()
    assert not (tmp_path / EPOCH).exists(), "the runtime generation must not be invalidated"
    checked = _run(tmp_path, "--check", env_overrides={name: value})
    assert checked.returncode != 0
    assert f"{name} must be" in checked.stderr


def test_default_memory_is_refused_for_a_config_that_is_not_qemus(tmp_path: Path) -> None:
    """A [hypervisor.qemu] drop-in over another hypervisor would give Kata two.

    Refused before the runtime generation is invalidated: an `updating` epoch
    would keep a working host from running any turn until it is reinstalled.
    """
    config = _provisioned_host(tmp_path)
    assert _run(tmp_path).returncode == 0
    config.write_text(config.read_text().replace("[hypervisor.qemu]", "[hypervisor.clh]"))
    epoch = (tmp_path / EPOCH).read_bytes()
    assert json.loads(epoch)["state"] == "ready"

    result = _run(tmp_path, env_overrides={"KATA_DEFAULT_MEMORY_MIB": "1024"})

    assert result.returncode != 0
    assert "does not configure QEMU" in result.stderr
    assert (tmp_path / EPOCH).read_bytes() == epoch
    assert not (config.parent / "config.d").exists()


@pytest.mark.skipif(not _HAS_ZSTD, reason="zstd is needed to build a release-shaped fixture")
def test_a_fresh_install_applies_its_opt_ins(tmp_path: Path) -> None:
    """The config they extend only exists once the archive is unpacked."""
    tarball = _fake_release_tarball(tmp_path)
    opt_ins = {
        "KATA_SHA256": _digest(tarball),
        "KATA_STATIC_SANDBOX_RESOURCES": "true",
        "KATA_DEFAULT_MEMORY_MIB": "1024",
    }

    result = _run(tmp_path, tarball=tarball, env_overrides=opt_ins)

    assert result.returncode == 0, result.stderr
    drop_in = tmp_path / "root/opt/kata/share/defaults/kata-containers" / BOOT_DROP_IN
    assert _settings(drop_in) == (
        "[hypervisor.qemu]\ndefault_memory = 1024\n[runtime]\nstatic_sandbox_resource_mgmt = true\n"
    )
    assert _run(tmp_path, "--check").returncode == 0


@pytest.mark.skipif(not _HAS_ZSTD, reason="zstd is needed to build a release-shaped fixture")
def test_a_fresh_install_refuses_default_memory_for_an_unpacked_config_that_is_not_qemus(
    tmp_path: Path,
) -> None:
    """A config the archive brings can only be checked once it is unpacked.

    That host had no runtime for an `updating` epoch to take out of service.
    """
    tarball = _fake_release_tarball(tmp_path)
    staging = tmp_path / "staging"
    (staging / "opt/kata/share/defaults/kata-containers/configuration-clh.toml").write_text(
        '[hypervisor.clh]\nkernel = "/opt/kata/share/kata-containers/vmlinux.container"\n'
        "[runtime]\ndisable_guest_seccomp = true\n"
    )
    subprocess.run(
        f"tar -C {staging} -cf - ./opt | zstd -q -f -o {tarball}",
        shell=True,
        check=True,
    )
    config = tmp_path / "root/opt/kata/share/defaults/kata-containers/configuration-clh.toml"
    opt_ins = {
        "KATA_SHA256": _digest(tarball),
        "KATA_CONFIG_PATH": str(config),
        "KATA_DEFAULT_MEMORY_MIB": "1024",
    }

    result = _run(tmp_path, tarball=tarball, env_overrides=opt_ins)

    assert result.returncode != 0
    assert "does not configure QEMU" in result.stderr
    assert not (config.parent / "config.d").exists()
