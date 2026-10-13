"""CI's external steps fail when their tests decline to run.

Each external test skips when the job did not hand it its image, binary or SDK,
and pytest reports a run that only skipped as a success. The steps therefore
keep a junit report, and `scripts/ci_tests_ran.py` reads it. The script is
exercised here against reports real pytest writes, and the workflow is checked
for an external run that writes no report or never has it read.
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


def _report(tmp_path: Path, source: str) -> Path:
    """The junit report real pytest writes for one test module, outside this repo's config."""
    module = tmp_path / "test_sample.py"
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
            str(module),
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
