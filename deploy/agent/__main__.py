"""``python -m deploy.agent --root <DEPLOY_ROOT> <command>``."""

from __future__ import annotations

import sys

from deploy.agent.cli import main

sys.exit(main())
