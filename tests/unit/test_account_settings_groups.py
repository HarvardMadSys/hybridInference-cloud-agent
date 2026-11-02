"""Every account setting has its place on Admin → Settings → Accounts.

Review finding F7: three settings of the registry were missing from the page's
groups and fell into a stray group of their own. The page's grouping lives in
the frontend; this reads it as text and compares the keys with the registry,
so a setting added later without a group fails here.
"""

from __future__ import annotations

import re
from pathlib import Path

from cloud_agent.accounts.settings_registry import ACCOUNT_SETTINGS

REPO_ROOT = Path(__file__).resolve().parents[2]
ORGANIZATION = REPO_ROOT / "apps/frontend/src/app/(agents)/admin/settings/settingsOrganization.ts"


def test_every_account_setting_belongs_to_a_group_on_the_settings_page() -> None:
    grouped = set(re.findall(r"'(accounts\.[a-z0-9_]+)'", ORGANIZATION.read_text()))
    registered = {setting.key for setting in ACCOUNT_SETTINGS}
    assert sorted(registered - grouped) == []
    assert sorted(grouped - registered) == []
