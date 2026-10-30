"""A large checkpoint downloads as parallel ranges of one exact object, checked whole."""

from __future__ import annotations

import hashlib
import io
import os
import threading
import time
from typing import Any

import pytest

from cloud_agent_host import sandbox_archive_ranges, sandbox_archive_s3
from cloud_agent_host.sandbox_archive_s3 import (
    SandboxArchiveDownloadError,
    SandboxArchiveS3Config,
    SandboxArchiveS3Uploader,
)
from cloud_agent_shared.sandbox_archive_settings import SandboxArchivePolicy

_RANGE = 1024
_BODY = os.urandom(10 * _RANGE + 17)
_OWNER = "123456789012"


class _Replaced(Exception):
    """What S3 answers a ranged read whose If-Match no longer holds."""


class _RangedS3:
    """One stored object, answering ranged reads of it as S3 does."""

    def __init__(self, body: bytes = _BODY, *, etag: str = '"etag-one"') -> None:
        self.body, self.etag = body, etag
        self.requests: list[dict[str, Any]] = []
        self.breaks: dict[int, int] = {}
        self._lock = threading.Lock()

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        with self._lock:
            self.requests.append(kwargs)
            start, end = (int(part) for part in kwargs["Range"].removeprefix("bytes=").split("-"))
            if kwargs.get("IfMatch", self.etag) != self.etag:
                raise _Replaced("PreconditionFailed")
            piece = self.body[start : end + 1]
            if self.breaks.get(start):
                self.breaks[start] -= 1
                piece = piece[: len(piece) // 2]
        return {
            "Body": io.BytesIO(piece),
            "ContentLength": end - start + 1,
            "ContentRange": f"bytes {start}-{end}/{len(self.body)}",
            "ETag": self.etag,
        }


@pytest.fixture(autouse=True)
def _small_ranges(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox_archive_ranges, "RANGE_BYTES", _RANGE)
    monkeypatch.setattr(sandbox_archive_s3, "RANGED_DOWNLOAD_MIN_BYTES", 2 * _RANGE)


def _uploader(client: _RangedS3) -> SandboxArchiveS3Uploader:
    policy = SandboxArchivePolicy(
        enabled=True,
        region="us-east-1",
        aws_access_key_id="A" * 20,
        aws_secret_access_key="s" * 40,
        expected_bucket_owner=_OWNER,
        revision=1,
    )
    config = SandboxArchiveS3Config.from_policy(
        policy,
        aws_access_key_id=policy.aws_access_key_id,
        aws_secret_access_key=policy.aws_secret_access_key,
    )
    return SandboxArchiveS3Uploader(config, client=client)  # type: ignore[arg-type]


def _download(client: _RangedS3, output: Any, **overrides: Any) -> None:
    arguments: dict[str, Any] = {
        "sandbox_id": "asbx_shared",
        "key": sandbox_archive_s3.checkpoint_object_key(
            sandbox_id="asbx_shared", generation=1, checkpoint_id="a" * 32
        ),
        "version_id": "version-1",
        "size": len(_BODY),
        "sha256": hashlib.sha256(_BODY).hexdigest(),
        "output": output,
    }
    arguments.update(overrides)
    _uploader(client).download_checkpoint(**arguments)


def _ranges(client: _RangedS3) -> list[str]:
    return sorted({request["Range"] for request in client.requests})


def test_a_large_checkpoint_downloads_as_ranges_of_its_exact_version(tmp_path):
    client = _RangedS3()

    with (tmp_path / "spool").open("w+b") as output:
        _download(client, output)
        output.seek(0)
        assert output.read() == _BODY

    assert len(client.requests) == len(_ranges(client)) == 11, "each range read once"
    assert all(request["VersionId"] == "version-1" for request in client.requests)
    assert all(request["ExpectedBucketOwner"] == _OWNER for request in client.requests)
    assert not any("IfMatch" in request for request in client.requests)


def test_without_a_version_every_later_range_requires_the_first_ones_etag(tmp_path):
    client = _RangedS3()

    with (tmp_path / "spool").open("w+b") as output:
        _download(client, output, version_id=None)

    first, *later = client.requests
    assert first["Range"] == f"bytes=0-{_RANGE - 1}" and "IfMatch" not in first
    assert later and all(request["IfMatch"] == '"etag-one"' for request in later)


def test_an_object_replaced_mid_download_is_never_mixed_into_it(tmp_path):
    client = _RangedS3()
    client.etag = '"etag-one"'
    original_get = client.get_object

    def replaced_after_the_first(**kwargs: Any) -> dict[str, Any]:
        answer = original_get(**kwargs)
        client.etag = '"etag-two"'
        return answer

    client.get_object = replaced_after_the_first  # type: ignore[method-assign]

    with (tmp_path / "spool").open("w+b") as output, pytest.raises(_Replaced):
        _download(client, output, version_id=None)


def test_a_range_whose_stream_breaks_is_fetched_again(tmp_path):
    client = _RangedS3()
    client.breaks[3 * _RANGE] = 2

    with (tmp_path / "spool").open("w+b") as output:
        _download(client, output)
        output.seek(0)
        assert output.read() == _BODY

    asked = [request["Range"] for request in client.requests]
    assert asked.count(f"bytes={3 * _RANGE}-{4 * _RANGE - 1}") == 3


def test_a_range_that_keeps_breaking_fails_the_download(tmp_path):
    client = _RangedS3()
    client.breaks[3 * _RANGE] = sandbox_archive_ranges.RANGE_ATTEMPTS

    with (
        (tmp_path / "spool").open("w+b") as output,
        pytest.raises(SandboxArchiveDownloadError, match="ended early"),
    ):
        _download(client, output)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"size": len(_BODY) - 1}, "not the size its receipt names"),
        ({"sha256": "0" * 64}, "does not match its digest"),
    ],
)
def test_an_object_that_is_not_the_receipts_is_refused_without_retrying(
    tmp_path, overrides, message
):
    client = _RangedS3()

    with (
        (tmp_path / "spool").open("w+b") as output,
        pytest.raises(SandboxArchiveDownloadError, match=message),
    ):
        _download(client, output, **overrides)

    assert len(client.requests) == len(_ranges(client)), "no range was fetched twice"


def test_a_range_stopped_by_a_later_ones_mismatch_does_not_hide_it(tmp_path):
    """The first range is still being read when a later one finds another object."""
    client = _RangedS3()
    mismatched = threading.Event()
    original_get = client.get_object

    class _Trickle(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            mismatched.wait(5)
            time.sleep(0.001)
            return super().read(1)

    def a_later_range_is_another_objects(**kwargs: Any) -> dict[str, Any]:
        answer = original_get(**kwargs)
        if kwargs["Range"].startswith("bytes=0-"):
            answer["Body"] = _Trickle(answer["Body"].getvalue())
        elif kwargs["Range"].startswith(f"bytes={3 * _RANGE}-"):
            answer["ContentRange"] = f"bytes {3 * _RANGE}-{4 * _RANGE - 1}/{len(_BODY) + 1}"
            mismatched.set()
        return answer

    client.get_object = a_later_range_is_another_objects  # type: ignore[method-assign]

    with (
        (tmp_path / "spool").open("w+b") as output,
        pytest.raises(SandboxArchiveDownloadError, match="not the size its receipt names"),
    ):
        _download(client, output)


def test_a_first_range_that_cannot_be_read_back_stops_the_others(tmp_path, monkeypatch):
    """Without a version the first range is hashed before the rest are waited on."""
    body = os.urandom(64 * _RANGE)
    client = _RangedS3(body)
    original_get = client.get_object

    def slow(**kwargs: Any) -> dict[str, Any]:
        if not kwargs["Range"].startswith("bytes=0-"):
            time.sleep(0.02)
        return original_get(**kwargs)

    client.get_object = slow  # type: ignore[method-assign]
    hashed = sandbox_archive_ranges._hash

    def unreadable_first(fd: int, hasher: Any, start: int, size: int) -> None:
        if start == 0:
            raise OSError("the spool cannot be read back")
        hashed(fd, hasher, start, size)

    monkeypatch.setattr(sandbox_archive_ranges, "_hash", unreadable_first)

    with (tmp_path / "spool").open("w+b") as output, pytest.raises(OSError, match="read back"):
        _download(
            client,
            output,
            version_id=None,
            size=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
        )

    assert len(_ranges(client)) < 32, "the ranges still queued were never fetched"


def test_a_range_refused_on_its_headers_still_closes_its_body(tmp_path):
    client = _RangedS3()
    bodies: list[io.BytesIO] = []
    original_get = client.get_object

    def recorded(**kwargs: Any) -> dict[str, Any]:
        answer = original_get(**kwargs)
        bodies.append(answer["Body"])
        return answer

    client.get_object = recorded  # type: ignore[method-assign]

    with (
        (tmp_path / "spool").open("w+b") as output,
        pytest.raises(SandboxArchiveDownloadError, match="not the size its receipt names"),
    ):
        _download(client, output, size=len(_BODY) - 1)

    assert bodies and all(body.closed for body in bodies)


@pytest.mark.parametrize("mode", ["a+b", "wb"])
def test_a_file_that_could_not_take_ranges_keeps_one_stream(tmp_path, mode):
    """Appends ignore the offset, and a file open only to write cannot be hashed back."""
    client = _RangedS3()
    ranged = client.get_object

    def whole_or_ranged(**kwargs: Any) -> dict[str, Any]:
        if "Range" in kwargs:
            return ranged(**kwargs)
        client.requests.append(kwargs)
        return {"Body": io.BytesIO(client.body), "ContentLength": len(client.body)}

    client.get_object = whole_or_ranged  # type: ignore[method-assign]

    with (tmp_path / "spool").open(mode) as output:
        _download(client, output)

    assert (tmp_path / "spool").read_bytes() == _BODY
    assert [("Range" in request) for request in client.requests] == [False]


def test_a_small_object_or_an_output_that_is_not_a_file_keeps_one_stream(tmp_path):
    class _Stream(_RangedS3):
        def get_object(self, **kwargs: Any) -> dict[str, Any]:
            self.requests.append(kwargs)
            return {"Body": io.BytesIO(self.body), "ContentLength": len(self.body)}

    client = _Stream()
    output = io.BytesIO()

    _download(client, output)

    assert output.getvalue() == _BODY
    assert [("Range" in request) for request in client.requests] == [False]
