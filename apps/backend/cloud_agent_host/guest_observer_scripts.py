"""The fixed scripts the guest observer's host side runs inside a Kata guest.

Each is a constant. Only the observer's own fixed paths and the cursor's
integers are passed as arguments, never job input, so nothing a turn controls
is run in the guest. Kept apart from ``guest_observer`` so that module stays
within its size budget.
"""

from __future__ import annotations

#: The status read every fixed observer script is built on, so the opening,
#: flushing and draining reads cannot drift into subtly different fences.
#: ``published`` returns the helper's status, journal size and guest clock only
#: once that status names a live process running the trusted binary and the
#: journal on disk is the file it publishes and at least as long. A
#: non-negative ``wanted`` waits for a publication from a later poll.
STATUS_READER = (
    "import json, os, sys, time\n"
    "def published(status_path, events_path, expected_binary, wanted, wait_s):\n"
    "    deadline = time.monotonic() + wait_s\n"
    "    while True:\n"
    "        try:\n"
    "            with open(status_path, encoding='utf-8') as handle:\n"
    "                status = json.load(handle)\n"
    "            pid = int(status.get('pid', 0))\n"
    "            if pid <= 0 or not os.path.exists(f'/proc/{pid}'):\n"
    "                raise RuntimeError('observer process is not alive')\n"
    "            try:\n"
    "                observer_alive = os.path.samefile(f'/proc/{pid}/exe', expected_binary)\n"
    "            except OSError:\n"
    "                observer_alive = False\n"
    "            if not observer_alive:\n"
    "                raise RuntimeError('observer pid does not name the trusted binary')\n"
    "            if wanted < 0 or int(status.get('poll_seq', -1)) > wanted:\n"
    "                break\n"
    "        except (OSError, ValueError, TypeError, json.JSONDecodeError):\n"
    "            status = None\n"
    "        if time.monotonic() >= deadline:\n"
    "            raise RuntimeError('observer status did not advance')\n"
    "        time.sleep(0.01)\n"
    "    size = int(status.get('journal_bytes', -1))\n"
    "    journal_dev = int(status.get('journal_dev', -1))\n"
    "    journal_ino = int(status.get('journal_ino', -1))\n"
    "    journal = os.stat(events_path)\n"
    "    if (size < 0 or int(journal.st_size) < size or journal_dev < 0 or journal_ino <= 0\n"
    "            or int(journal.st_dev) != journal_dev\n"
    "            or int(journal.st_ino) != journal_ino):\n"
    "        raise RuntimeError('observer journal is behind its published fence')\n"
    "    return {'status': status, 'journal_size': size, 'guest_mono_ns': time.monotonic_ns()}\n"
)

#: One status read. A path after the wait names the guest's boot identifier,
#: which is then read in the same exec: an unreadable one is reported empty
#: rather than invented, and does not fail the read.
SNAPSHOT_SCRIPT = STATUS_READER + (
    "status_path, events_path, expected_binary = sys.argv[1:4]\n"
    "snapshot = published(status_path, events_path, expected_binary, "
    "int(sys.argv[4]), float(sys.argv[5]))\n"
    "if len(sys.argv) > 6:\n"
    "    try:\n"
    "        with open(sys.argv[6], encoding='utf-8') as handle:\n"
    "            snapshot['boot_id'] = handle.read(256).strip()\n"
    "    except (OSError, ValueError):\n"
    "        snapshot['boot_id'] = ''\n"
    "print(json.dumps(snapshot, separators=(',', ':')))\n"
)

#: A member window's unread journal bytes: everything from the window's offset
#: to the end, read only from a journal file whose own device and inode are
#: still the cursor's, and never from behind that offset.
WINDOW_DELTA_SCRIPT = (
    "import os, sys\n"
    "filename, offset, expected_dev, expected_ino = sys.argv[1], int(sys.argv[2]), "
    "int(sys.argv[3]), int(sys.argv[4])\n"
    "with open(filename, 'rb', buffering=0) as handle:\n"
    "    journal = os.fstat(handle.fileno())\n"
    "    if (int(journal.st_dev) != expected_dev "
    "            or int(journal.st_ino) != expected_ino):\n"
    "        raise RuntimeError('observer journal changed before read')\n"
    "    if int(journal.st_size) < offset:\n"
    "        raise RuntimeError('observer journal is behind the read fence')\n"
    "    handle.seek(offset)\n"
    "    sys.stdout.buffer.write(handle.read(max(0, int(journal.st_size) - offset)))\n"
)

#: A turn's whole close in one exec: the closing publication, a wait until the
#: helper has published two polls after it, and the cursor's slice, read only
#: from a journal file whose own device and inode are still the cursor's. It
#: prints both publications as one JSON line, then the slice. A journal shorter
#: than the cursor's offset is not read at all; the host reports that from the
#: publication it returns.
DRAIN_SCRIPT = STATUS_READER + (
    "status_path, events_path, expected_binary = sys.argv[1:4]\n"
    "offset, expected_dev, expected_ino, limit = (int(item) for item in sys.argv[4:8])\n"
    "wait_s = float(sys.argv[8])\n"
    "closing = published(status_path, events_path, expected_binary, -1, wait_s)\n"
    "flushed = published(status_path, events_path, expected_binary, "
    "int(closing['status'].get('poll_seq', -1)) + 1, wait_s)\n"
    "count = min(flushed['journal_size'] - offset, limit)\n"
    "payload = b''\n"
    "if count >= 0:\n"
    "    with open(events_path, 'rb', buffering=0) as handle:\n"
    "        journal = os.fstat(handle.fileno())\n"
    "        if (int(journal.st_dev) != expected_dev "
    "                or int(journal.st_ino) != expected_ino):\n"
    "            raise RuntimeError('observer journal changed before read')\n"
    "        if int(journal.st_size) < offset + count:\n"
    "            raise RuntimeError('observer journal is behind the read fence')\n"
    "        handle.seek(offset)\n"
    "        payload = handle.read(count)\n"
    "header = json.dumps({'closing': closing, 'flushed': flushed}, separators=(',', ':'))\n"
    "sys.stdout.buffer.write(header.encode() + b'\\n' + payload)\n"
)

__all__ = ["DRAIN_SCRIPT", "SNAPSHOT_SCRIPT", "STATUS_READER", "WINDOW_DELTA_SCRIPT"]
