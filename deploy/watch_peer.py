"""Stop a readiness container when the other deployment job has failed.

Only the read-only readiness wait is supervised: never interrupt a migration
or Compose replacement. An unavailable GitHub API leaves the normal bounded
readiness check in charge. Credentials stay on the Actions host.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
import urllib.request


def peer_failure(peer: str) -> str | None:
    """Read this attempt, so a previous failed attempt cannot cancel a retry."""
    base = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    repo = os.environ["GITHUB_REPOSITORY"]
    run = os.environ["GITHUB_RUN_ID"]
    attempt = os.environ["GITHUB_RUN_ATTEMPT"]
    for page in range(1, 101):
        request = urllib.request.Request(
            f"{base}/repos/{repo}/actions/runs/{run}/attempts/{attempt}/jobs"
            f"?per_page=100&page={page}",
            headers={
                "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            jobs = json.load(response)["jobs"]
        for job in jobs:
            if job["name"] == peer and job["status"] == "completed":
                conclusion = job["conclusion"]
                if conclusion != "success":
                    return f"{peer}: {conclusion} ({job['html_url']})"
                return None
        if len(jobs) < 100:
            return None
    raise RuntimeError("too many pages of workflow jobs")


def supervise(command: list[str], *, peer: str, container: str, interval: float = 10) -> int:
    """Keep healthy cold builds waiting; reap only our own one-off on failure."""
    child = subprocess.Popen(command, start_new_session=True)
    next_check = 0.0
    warned = False
    try:
        while child.poll() is None:
            if time.monotonic() >= next_check:
                try:
                    failure = peer_failure(peer)
                    warned = False
                except Exception as exc:
                    # HTTPError includes URLs; never print tokens or response bodies.
                    if not warned:
                        print(
                            f"[deploy-peer] status unavailable ({type(exc).__name__}); "
                            "keeping the bounded readiness wait",
                            flush=True,
                        )
                    warned = True
                else:
                    if failure:
                        print(
                            f"[deploy-peer] ERROR: {failure}; stopping readiness wait", flush=True
                        )
                        return 1
                next_check = time.monotonic() + interval
            time.sleep(0.2)
        return child.returncode
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
            # Killing the Compose client does not reliably stop its container.
            subprocess.run(
                ["docker", "rm", "-f", container],
                check=False,
                timeout=20,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


def main() -> int:
    """Run the command after -- under peer-job supervision."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--peer", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("missing readiness command")

    # A cancelled workflow should clean up its one-off too.
    def interrupted(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    return supervise(command, peer=args.peer, container=args.container)


if __name__ == "__main__":
    raise SystemExit(main())
