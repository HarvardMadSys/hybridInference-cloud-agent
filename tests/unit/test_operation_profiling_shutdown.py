"""Subprocess regression for the collector's interpreter-exit delivery contract."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cloud_agent_shared import operation_profiling as profiling

_CHILD_SCRIPT = """
import atexit
import logging
import threading
import time

from cloud_agent_shared import operation_profiling as profiling

publishing = threading.Event()
exiting = threading.Event()


class WaitForExit(logging.Handler):
    def createLock(self):
        # The events synchronize this handler. Keep logging.shutdown from
        # draining it independently of the collector's exit callback.
        self.lock = None

    def emit(self, record):
        publishing.set()
        if not exiting.wait(5):
            raise RuntimeError("interpreter exit did not release the log handler")
        # Keep publication in flight after the exit callback releases it.
        # A daemon thread alone cannot guarantee delivery before process exit.
        time.sleep(0.05)


logger = logging.getLogger(profiling.__name__)
logger.setLevel(logging.INFO)
logger.addHandler(WaitForExit())

measurement = profiling.start_operation(
    operation="attempt.startup", host="shutdown-host"
)
assert measurement is not None
assert profiling.bind(measurement, host="shutdown-host", job_id="ajob_shutdown")
profiling.mark(measurement, "prompt_dispatch")
profiling.finish(measurement, outcome="dispatched")
assert publishing.wait(5), "the finished record never reached publication"

# Registered last, so this releases publication before the collector's own
# exit callback runs. No caller explicitly flushes or closes the sink.
atexit.register(exiting.set)
print(measurement.measurement_id)
"""


def test_interpreter_exit_drains_a_record_still_waiting_to_publish(tmp_path):
    """Normal process exit delivers a finished sample without a caller flushing."""
    sink = tmp_path / "shutdown-records.jsonl"
    run_id = "interpreter-exit-run"
    descriptor = {
        "run_id": run_id,
        "operations": [profiling.STARTUP_OPERATION],
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "max_operations": 1,
        "target": {"host": "shutdown-host"},
    }
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.pathsep.join(
            filter(
                None,
                [
                    str(Path(profiling.__file__).resolve().parents[1]),
                    os.environ.get("PYTHONPATH", ""),
                ],
            )
        ),
        profiling.ENV_PROFILING_MODE: profiling.MODE_ENVIRONMENT,
        profiling.ENV_PROFILING_ENABLED: "true",
        profiling.ENV_PROFILING_RUN: json.dumps(descriptor),
        profiling.ENV_PROFILING_SINK: str(sink),
    }

    completed = subprocess.run(
        [sys.executable, "-c", _CHILD_SCRIPT],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    records = [json.loads(line) for line in sink.read_text().splitlines()]
    assert len(records) == 1
    record = records[0]
    assert record["measurement_id"] == completed.stdout.strip()
    assert record["profiling_run_id"] == run_id
    assert record["schema_version"] == profiling.SCHEMA_VERSION
    assert record["operation"] == profiling.STARTUP_OPERATION
    assert record["startup_outcome"] == "dispatched"
    assert record["collection_complete"] is True
    assert record["boundary_frozen"] is True
    assert record["facts"]["job_id"] == "ajob_shutdown"
