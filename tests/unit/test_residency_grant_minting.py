"""The residency-grant mint: what it links, and what it revokes when it cannot.

``_mint_residency_grant`` is where a supervisor-owned member process gets its
credential, and it is the only writer of ``link_residency_grant``'s optional
``binding_id``. That parameter arrived in #121; a fake store that never received
it would let this subject pass without exercising it, so the cases below assert
on the link call itself rather than only on the credential that comes back.
"""

from __future__ import annotations

from typing import Any

from cloud_agent.api.hosts import _mint_residency_grant


class _Grants:
    """The grant client before #121: it carries no installation to bind."""

    def __init__(self) -> None:
        self.minted: list[dict[str, Any]] = []
        self.revoked: list[str] = []
        self.fail_mint = False

    async def mint(self, **kwargs: Any) -> dict[str, Any]:
        if self.fail_mint:
            raise RuntimeError("gateway is down")
        self.minted.append(kwargs)
        return {"grant_id": "agr-1", "token": "tok-1"}

    async def revoke(self, grant_id: str) -> bool:
        self.revoked.append(grant_id)
        return True


class _BoundGrants(_Grants):
    """The #121 shape: the client also carries the installation it is bound to."""

    def __init__(self, binding_id: str) -> None:
        super().__init__()
        self.binding_id = binding_id


class _Store:
    """Records the link call, and can refuse it the way a lost fence does."""

    def __init__(self, *, link_ok: bool = True) -> None:
        self.link_ok = link_ok
        self.linked: list[dict[str, Any]] = []

    async def link_residency_grant(self, **kwargs: Any) -> bool:
        self.linked.append(kwargs)
        return self.link_ok


def _row() -> dict[str, Any]:
    """The residency row ``establish_residency`` hands the mint."""
    return {
        "thread_id": "athr_1",
        "generation": 4,
        "establishing_attempt_id": "aatt_9",
        "user_id": "user-1",
        "model": "claude-sonnet-4",
    }


async def test_a_bound_client_hands_its_installation_to_the_link() -> None:
    store = _Store()
    grants = _BoundGrants(binding_id="gw_installation_1")

    credential = await _mint_residency_grant(store, grants, row=_row())

    assert credential == "tok-1"
    assert store.linked == [
        {
            "thread_id": "athr_1",
            "generation": 4,
            "grant_id": "agr-1",
            "binding_id": "gw_installation_1",
        }
    ]


async def test_an_unbound_client_links_without_an_installation() -> None:
    store = _Store()

    credential = await _mint_residency_grant(store, _Grants(), row=_row())

    assert credential == "tok-1"
    assert store.linked == [{"thread_id": "athr_1", "generation": 4, "grant_id": "agr-1"}]


async def test_a_link_that_matched_nothing_revokes_the_orphan_grant() -> None:
    store = _Store(link_ok=False)
    grants = _BoundGrants(binding_id="gw_installation_1")

    credential = await _mint_residency_grant(store, grants, row=_row())

    assert credential is None
    assert grants.revoked == ["agr-1"]


async def test_a_mint_failure_yields_no_credential() -> None:
    store = _Store()
    grants = _Grants()
    grants.fail_mint = True

    credential = await _mint_residency_grant(store, grants, row=_row())

    assert credential is None
    assert store.linked == []
