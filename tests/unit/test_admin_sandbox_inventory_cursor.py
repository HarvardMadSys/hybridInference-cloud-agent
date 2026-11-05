"""Inventory cursor parsing never passes invalid transaction snapshots to SQL."""

import pytest

from cloud_agent.admin_observability import encode_cursor
from cloud_agent.admin_sandbox_inventory import SandboxInventoryCursor, decode_inventory_cursor


@pytest.mark.parametrize(
    "snapshot", ["0:1:", "5:4:", "1:18446744073709551616:", "1:3:3", "1:4:2,2", "1:4:3,2"]
)
def test_invalid_transaction_snapshot_is_rejected(snapshot):
    with pytest.raises(ValueError):
        SandboxInventoryCursor(scope="scope", database_snapshot=snapshot)


@pytest.mark.parametrize("snapshot", ["1:1:", "1:4:1,3", "4294967296:4294967298:4294967297"])
def test_transaction_snapshot_round_trips(snapshot):
    cursor = SandboxInventoryCursor(scope="scope", database_snapshot=snapshot)
    assert decode_inventory_cursor(encode_cursor(cursor), "scope") == cursor


def test_snapshot_page_is_bounded_to_postgres_integer():
    with pytest.raises(ValueError):
        SandboxInventoryCursor(scope="scope", snapshot_id="snapshot", page=2**31)
