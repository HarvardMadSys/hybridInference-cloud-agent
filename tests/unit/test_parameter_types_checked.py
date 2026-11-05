"""The shared parameter check reads each route's own types.

It refuses a NUL in any path or query parameter, and an integer too wide for a
``bigint`` only where the route, or one of its dependencies, reads an integer.
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI, Query
from fastapi.testclient import TestClient

from cloud_agent.api._parameters import BIGINT_MAX, BIGINT_MIN, refuse_unstorable_parameters

WIDE = str(BIGINT_MAX + 1)
NARROW = str(BIGINT_MIN - 1)


def _cursor(after: int | None = Query(None)) -> int | None:
    return after


@pytest.fixture
def client() -> TestClient:
    app = FastAPI(dependencies=[Depends(refuse_unstorable_parameters)])

    @app.get("/items/{item_id}")
    async def item(
        item_id: int,
        q: str | None = None,
        exact: bool = False,
        ids: list[int] = Query(default_factory=list),
        after: int | None = Depends(_cursor),
    ) -> dict[str, object]:
        return {"item_id": item_id, "q": q, "ids": ids, "after": after}

    @app.get("/names/{name}")
    async def name(name: str) -> dict[str, str]:
        return {"name": name}

    return TestClient(app)


@pytest.mark.parametrize(
    ("path", "location"),
    [
        (f"/items/{WIDE}", ["path", "item_id"]),
        (f"/items/1?ids=2&ids={WIDE}", ["query", "ids"]),
        (f"/items/1?after={NARROW}", ["query", "after"]),
        ("/items/1?q=a%00b", ["query", "q"]),
        ("/names/a%00b", ["path", "name"]),
    ],
)
def test_what_no_column_holds_is_refused(client: TestClient, path: str, location: list[str]):
    response = client.get(path)

    assert response.status_code == 422
    assert [error["loc"] for error in response.json()["detail"]] == [location]


@pytest.mark.parametrize(
    "path",
    [
        f"/items/{BIGINT_MAX}?after={BIGINT_MIN}&ids={BIGINT_MAX}",
        f"/items/1?q={WIDE}",
        f"/names/{WIDE}",
    ],
)
def test_the_widest_bigint_and_text_of_digits_pass(client: TestClient, path: str):
    assert client.get(path).status_code == 200
