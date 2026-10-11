"""First-start credential publication survives contention and interrupted writes."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from cloud_agent import pgadmin_setup


def test_concurrent_administrators_cannot_mix_or_replace_credentials(tmp_path):
    def claim(number):
        try:
            pgadmin_setup.initialize_pgadmin(
                tmp_path, f"admin-{number}@example.org", f"password-{number}"
            )
            return number
        except FileExistsError:
            return None

    with ThreadPoolExecutor(max_workers=8) as executor:
        winners = [number for number in executor.map(claim, range(8)) if number is not None]
    assert len(winners) == 1
    winner = winners[0]
    assert (tmp_path / "email").read_text() == f"admin-{winner}@example.org"
    assert (tmp_path / "password").read_text() == f"password-{winner}"


def test_interrupted_publication_does_not_start_pgadmin_and_can_be_retried(tmp_path, monkeypatch):
    replace = pgadmin_setup.os.replace

    def interrupt(source, destination):
        if Path(destination).name == "email":
            raise OSError("write interrupted")
        replace(source, destination)

    monkeypatch.setattr(pgadmin_setup.os, "replace", interrupt)
    with pytest.raises(OSError):
        pgadmin_setup.initialize_pgadmin(tmp_path, "first@example.org", "first-password")
    assert not (tmp_path / "email").exists()
    assert not list(tmp_path.glob(".setup-*"))
    monkeypatch.setattr(pgadmin_setup.os, "replace", replace)
    pgadmin_setup.initialize_pgadmin(tmp_path, "second@example.org", "second-password")
    assert (tmp_path / "email").read_text() == "second@example.org"
    assert (tmp_path / "password").read_text() == "second-password"


def test_a_symlink_cannot_redirect_setup_storage(tmp_path):
    volume = tmp_path / "volume"
    volume.mkdir()
    link = tmp_path / "link"
    link.symlink_to(volume)
    with pytest.raises(OSError):
        pgadmin_setup.initialize_pgadmin(link, "admin@example.org", "password")
    assert list(volume.iterdir()) == []
