"""Select native image checks without rebuilding unrelated release artifacts.

The complete catalog matches publication. Backend source changes also select the
sandbox: its real-CLI conformance tests execute the control plane and host code,
even though the sandbox Dockerfile does not COPY those Python packages.
"""

from __future__ import annotations

import argparse
import json


def _catalog():
    # This file is executed by path in minimal CI, where sys.path starts at
    # scripts/ rather than the repository root. Import lazily after adding the
    # root, without importing any application package or third-party library.
    import sys
    from pathlib import Path

    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    from deploy.release.artifact_catalog import artifacts

    return artifacts()


def image_matrix(scope: str) -> list[dict[str, str]]:
    """Return the complete catalog unless the classifier proves a narrow scope."""
    entries = _catalog()
    if scope in {"web", "backend"}:
        entries = tuple(entry for entry in entries if scope in entry.ci_scopes)
    return [entry.matrix_cell() for entry in entries]


def main() -> None:
    """Emit a compact Actions output; an unknown scope conservatively selects all."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scope")
    args = parser.parse_args()
    print("artifacts=" + json.dumps(image_matrix(args.scope), separators=(",", ":")))


if __name__ == "__main__":
    main()
