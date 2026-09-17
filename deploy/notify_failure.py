"""Report a failed deploy through Actions and the configured operator webhook."""

from __future__ import annotations

import json
import os
import urllib.request


def main() -> None:
    """Send only deployment metadata; never include logs or environment dumps."""
    link = (
        f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
        f"{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    )
    text = f"Staging deployment failed ({os.environ['GITHUB_JOB']}): {link}"
    print(f"::error title=Staging deployment failed::{text}")
    url = os.environ.get("AGENT_ALERT_WEBHOOK_URL", "").strip()
    if not url:
        print("No operator webhook configured; GitHub Actions failure notifications apply.")
        return
    request = urllib.request.Request(
        url,
        data=json.dumps({"text": text, "event": "deployment_failed", "run_url": link}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    # Do not forward a webhook payload across redirects to an unknown receiver.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=10):
            pass
    except Exception as exc:
        print(
            f"::error title=Alert delivery failed::{type(exc).__name__}; check the configured receiver"
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
