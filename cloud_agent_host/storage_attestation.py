"""Read capacity from two deployment-only, non-recursive read-only directory binds.

Docker resolves the root-owned source directories for the deployment user. The
helper needs no capabilities, network, host PID namespace or Docker socket.
Before measuring, it checks both binds' device, filesystem root and type against
the host mount-table plan. A lexical plan alone is not proof: symlinks, shadowed
mounts or a topology change must not make the helper attest the wrong disk.
"""

from __future__ import annotations

import json
import os
import re
import signal
import sys
from pathlib import Path

from cloud_agent_host.host_capacity import storage_bytes_in_range


def _unescape(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def mount_for_fd(fd: int, *, proc: Path = Path("/proc/self")) -> tuple[str, str, str]:
    """Get the open directory's exact read-only mount: device, fs root and type."""
    info = (proc / "fdinfo" / str(fd)).read_text()
    mount_id = next(line.split()[1] for line in info.splitlines() if line.startswith("mnt_id:"))
    for line in (proc / "mountinfo").read_text().splitlines():
        fields = line.split()
        if fields[0] == mount_id:
            separator = fields.index("-")
            if "ro" not in fields[5].split(","):
                raise ValueError("host view is not read-only")
            return fields[2], _unescape(fields[3]), fields[separator + 1]
    raise ValueError("directory mount is absent from mountinfo")


def measure(
    target: str, device: str, fsroot: str, upper_root: str, fstype: str
) -> dict[str, str | int]:
    """Check the mounted directories against the plan, then read their capacities."""
    readings = []
    for path, expected_root in (("/disk", fsroot), ("/upper", upper_root)):
        # `O_PATH` opens a directory as a reference without reading it, which
        # is why this helper needs no permission on what it is pointed at. It
        # is a Linux flag and typeshed says so, while this module is also
        # type-checked and unit-tested on macOS — hence `getattr`, read at the
        # call so a test can still substitute it. In the Linux container this
        # runs in, the flag is always there.
        fd = os.open(path, getattr(os, "O_PATH", 0) | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            if mount_for_fd(fd) != (device, expected_root, fstype):
                raise ValueError("mounted directory does not match the host mount-table plan")
            readings.append(os.fstatvfs(fd))
        finally:
            os.close(fd)
    disk, upper = readings
    total, free, available = (
        disk.f_frsize * disk.f_blocks,
        disk.f_frsize * disk.f_bfree,
        disk.f_frsize * disk.f_bavail,
    )
    if not storage_bytes_in_range(total_bytes=total, free_bytes=free, available_bytes=available):
        raise ValueError("invalid filesystem counters")
    return {
        "mount_root": target,
        "fstype": fstype,
        "total_bytes": total,
        "used_bytes": total - free,
        "available_bytes": available,
        "upper_total_bytes": upper.f_frsize * upper.f_blocks,
    }


def main() -> int:
    """Print one metadata record and exit, with an independent process deadline."""
    if len(sys.argv) != 7 or not sys.argv[6].isdigit() or not 1 <= int(sys.argv[6]) <= 5:
        return 2
    # The Docker client can be killed while the daemon still runs a container.
    # This timer belongs to the helper itself; --rm removes it on termination.
    # PID 1 ignores default signal dispositions, so both need real handlers.
    signal.signal(signal.SIGALRM, lambda *_: sys.exit(124))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    signal.alarm(int(sys.argv[6]))
    try:
        print(json.dumps(measure(*sys.argv[1:6]), sort_keys=True))
    except (OSError, ValueError, StopIteration) as error:
        print(f"storage metadata unavailable: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
