"""CI shards cover every collected test file exactly once, in balanced groups."""

from __future__ import annotations

import pytest
from scripts import ci_shard

COUNTS = {
    "tests/a.py": 172,
    "tests/b.py": 120,
    "tests/c.py": 96,
    "tests/d.py": 89,
    "tests/e.py": 5,
    "tests/f.py": 1,
}


@pytest.mark.parametrize("total", [1, 2, 3])
def test_shards_partition_the_files(total: int) -> None:
    shards = ci_shard.partition(COUNTS, total)
    assert len(shards) == total
    every = [path for shard in shards for path in shard]
    assert sorted(every) == sorted(COUNTS), "a file is missing or runs twice"


def test_shards_are_balanced_by_test_count() -> None:
    loads = [sum(COUNTS[path] for path in shard) for shard in ci_shard.partition(COUNTS, 2)]
    assert max(loads) - min(loads) <= max(COUNTS.values())
    assert loads == [261, 222]


def test_every_shard_of_a_run_computes_the_same_split() -> None:
    """Shards run on different machines and never talk; only the input may decide."""
    reordered = dict(reversed(list(COUNTS.items())))
    assert ci_shard.partition(COUNTS, 3) == ci_shard.partition(reordered, 3)


def test_main_prints_one_shard(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(ci_shard, "collect", lambda args: seen.append(args) or COUNTS)
    assert ci_shard.main(["--shard", "2/2", "--", "-m", "dbtest"]) == 0
    assert seen == [["-m", "dbtest"]]
    assert capsys.readouterr().out.split() == ci_shard.partition(COUNTS, 2)[1]


def test_an_empty_shard_is_refused_rather_than_running_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pytest given no paths runs its whole testpaths, so empty must be an error."""
    monkeypatch.setattr(ci_shard, "collect", lambda args: {"tests/only.py": 3})
    with pytest.raises(SystemExit, match="received no files"):
        ci_shard.main(["--shard", "2/2"])


@pytest.mark.parametrize("shard", ["0/2", "3/2"])
def test_an_out_of_range_shard_is_refused(shard: str) -> None:
    with pytest.raises(SystemExit):
        ci_shard.main(["--shard", shard])


def test_real_collection_is_read_by_file() -> None:
    """The parser must agree with pytest's own `--collect-only -q` output."""
    counts = ci_shard.collect(["-m", "dbtest"])
    assert counts and all(path.startswith("tests/") and path.endswith(".py") for path in counts)
    assert sum(counts.values()) >= 100
