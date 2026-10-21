"""CI's external steps fail when their tests decline to run.

Each external test skips when the job did not hand it its image, binary or SDK,
and pytest reports a run that only skipped as a success. The steps therefore
keep a junit report, and `scripts/ci_tests_ran.py` reads it. The script is
exercised here against reports real pytest writes, and the workflow is checked
for an external run that writes no report or never has it read, and for a
database tier whose report is read with a floor below 100, or none, or counting
a module skipped at collection.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import yaml
from scripts import ci_tests_ran

REPO_ROOT = Path(__file__).resolve().parents[2]
CI = REPO_ROOT / ".github/workflows/ci.yml"


def _report(tmp_path: Path, *sources: str) -> Path:
    """The junit report real pytest writes for these test modules, outside this repo's config."""
    modules = [tmp_path / f"test_sample_{index}.py" for index in range(len(sources))]
    for module, source in zip(modules, sources, strict=True):
        module.write_text(textwrap.dedent(source))
    report = tmp_path / "report.xml"
    environment = {key: value for key, value in os.environ.items() if key != "PYTEST_ADDOPTS"}
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-c",
            os.devnull,
            "--rootdir",
            str(tmp_path),
            f"--junitxml={report}",
            *(str(module) for module in modules),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert report.exists(), "pytest wrote no junit report"
    return report


def test_a_report_in_which_every_test_ran_passes(tmp_path: Path) -> None:
    """An expected failure ran; junit merely files it under `skipped`."""
    report = _report(
        tmp_path,
        """
        import pytest

        def test_passes():
            pass

        @pytest.mark.xfail(reason="known", strict=True)
        def test_fails_as_expected():
            raise AssertionError
        """,
    )
    assert ci_tests_ran.declined(report) == (2, [])
    assert ci_tests_ran.main([str(report)]) == 0


def test_a_test_that_skipped_fails_the_step(tmp_path: Path) -> None:
    report = _report(
        tmp_path,
        """
        import pytest

        def test_passes():
            pass

        def test_missing_image():
            pytest.skip("set THE_TEST_IMAGE")

        def test_missing_sdk():
            pytest.importorskip("a_module_nobody_installed")

        @pytest.mark.skipif(True, reason="set THE_TEST_BINARY")
        def test_missing_binary():
            pass
        """,
    )
    ran, skipped = ci_tests_ran.declined(report)
    assert ran == 4
    assert sorted(name.rsplit("::", 1)[1] for name in skipped) == [
        "test_missing_binary",
        "test_missing_image",
        "test_missing_sdk",
    ]
    assert ci_tests_ran.main([str(report)]) == 1


def test_a_module_skipped_at_collection_fails_the_step(tmp_path: Path) -> None:
    """junit records this skip without the type a skipped test carries."""
    report = _report(
        tmp_path,
        """
        import pytest

        pytest.skip("needs a Docker daemon", allow_module_level=True)

        def test_never_collected():
            pass
        """,
    )
    ran, skipped = ci_tests_ran.declined(report)
    assert ran == 1
    assert len(skipped) == 1
    assert ci_tests_ran.main([str(report)]) == 1


def test_a_report_without_a_single_test_fails_the_step(tmp_path: Path) -> None:
    report = _report(tmp_path, "")
    assert ci_tests_ran.declined(report) == (0, [])
    assert ci_tests_ran.main([str(report)]) == 1


def test_a_report_below_its_floor_fails_the_step(tmp_path: Path) -> None:
    """A tier that stopped being selected still ran something; the floor sees it."""
    report = _report(
        tmp_path,
        """
        def test_one():
            pass

        def test_two():
            pass
        """,
    )
    assert ci_tests_ran.main(["--min", "2", str(report)]) == 0
    assert ci_tests_ran.main(["--min", "3", str(report)]) == 1


def test_running_skips_only_lets_a_module_skipped_at_collection_pass(tmp_path: Path) -> None:
    """The database tier selects by marker, so its run imports modules outside it too."""
    report = _report(
        tmp_path,
        """
        def test_reads_the_database():
            pass
        """,
        """
        import pytest

        pytest.skip("needs a Docker daemon", allow_module_level=True)
        """,
    )
    assert ci_tests_ran.main([str(report)]) == 1
    assert ci_tests_ran.main(["--running-skips-only", str(report)]) == 0


def test_running_skips_only_still_fails_a_test_that_skipped(tmp_path: Path) -> None:
    """What every database test does when its fixture cannot reach PostgreSQL."""
    report = _report(
        tmp_path,
        """
        import pytest

        @pytest.fixture
        def database():
            pytest.skip("PostgreSQL not available")

        def test_reads_the_database(database):
            pass

        @pytest.mark.xfail(reason="known", strict=True)
        def test_fails_as_expected():
            raise AssertionError
        """,
    )
    ran, skipped = ci_tests_ran.declined(report, running_only=True)
    assert ran == 2
    assert [name.rsplit("::", 1)[1] for name in skipped] == ["test_reads_the_database"]
    assert ci_tests_ran.main(["--running-skips-only", str(report)]) == 1


def _floor(command: str) -> int:
    """The `--min` a `scripts/ci_tests_ran.py` command reads its reports with, or 0."""
    found = re.search(r"scripts/ci_tests_ran\.py .*--min (\d+)", command)
    return int(found.group(1)) if found else 0


def test_the_database_tier_is_read_with_a_floor() -> None:
    """The dbtest run keeps a report, and a later step of its job reads it with `--min`.

    At least 100: a floor much lower passes a run that selected almost nothing.
    And with `--running-skips-only`: unsharded, the run imports every test
    module, and one outside the tier may skip at import.
    """
    for job_name, job in yaml.safe_load(CI.read_text())["jobs"].items():
        runs = [str(step.get("run", "")).replace("\\\n", " ") for step in job.get("steps", [])]
        for index, run in enumerate(runs):
            if not re.search(r'pytest .*-m "dbtest"', run):
                continue
            report = _REPORT.search(run)
            assert report, f"{job_name}: the dbtest run keeps no junit report"
            readers = [
                later
                for later in runs[index + 1 :]
                if _floor(later) >= 100
                and "--running-skips-only" in later
                and f'"{report.group(1)}"' in later
            ]
            assert readers, (
                f"{job_name}: the dbtest report is never read with a floor of 100 "
                "and --running-skips-only"
            )
            return
    raise AssertionError("no job runs the dbtest tier")


_REPORT = re.compile(r'--junitxml="(\$\{RUNNER_TEMP\}/[\w.-]+\.xml)"')


def test_every_external_run_in_ci_fails_when_its_tests_skip() -> None:
    """Each `-m external` run keeps a report, and the same step reads it afterwards."""
    guarded: dict[str, list[str]] = {}
    for job_name, job in yaml.safe_load(CI.read_text())["jobs"].items():
        for step in job.get("steps", []):
            run = str(step.get("run", "")).replace("\\\n", " ")
            commands = [line.strip() for line in run.splitlines() if line.strip()]
            for index, command in enumerate(commands):
                if "pytest" not in command or not re.search(r"-m external\b", command):
                    continue
                report = _REPORT.search(command)
                assert report, f"{job_name}: {step['name']!r} keeps no junit report"
                readers = [
                    later for later in commands[index + 1 :] if "scripts/ci_tests_ran.py" in later
                ]
                assert readers, f"{job_name}: {step['name']!r} never checks its report"
                assert f'"{report.group(1)}"' in readers[0], readers[0]
                guarded.setdefault(job_name, []).append(step["name"])
    assert {"test", "artifacts", "mcp", "compose"} <= set(guarded), guarded
