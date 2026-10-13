"""Actual Docker lifecycle acceptance, without inference or external network."""

from __future__ import annotations

import time
import uuid

import pytest
from tests.external.test_retained_command_limits import (
    _docker,
    retained_command_container as retained_command_container,
)

from cloud_agent_host.environment_preparation import EnvironmentPreparation
from cloud_agent_host.sandbox import SandboxSpec
from cloud_agent_shared.environment import BoundEnvironment, EnvironmentVariable

pytestmark = [pytest.mark.external, pytest.mark.timeout(120)]


def test_environment_services_and_stage_boundaries_in_real_container(
    retained_command_container, tmp_path
):
    backend = retained_command_container
    env = BoundEnvironment(
        binding_id="container-binding",
        variables=[
            EnvironmentVariable(name="BUILD_TOKEN", value="setup-secret-example", phase="setup"),
            EnvironmentVariable(name="APP_TOKEN", value="runtime-secret-example", phase="runtime"),
        ],
        setup_script='set -eu; test "$BUILD_TOKEN" = setup-secret-example; test -z "${APP_TOKEN:-}"; printf setup >> setup-count; printf "%s" "$BUILD_TOKEN"; setsid sleep 60 &',
        start_script='set -eu; test -z "${BUILD_TOKEN:-}"; test "$APP_TOKEN" = runtime-secret-example; printf start >> start-count; python3 -m http.server 18991 >/dev/null 2>&1 &',
    )
    # Match production shared-directory permissions for all operation UIDs.
    backend.manager.exec_capture(
        backend.spec,
        ["/bin/sh", "-c", "chmod 2770 /workspace"],
        user_uid=backend.manager.backend.uid,
        user_gid=backend.manager.backend.gid,
        timeout_s=10,
    )
    prepare = EnvironmentPreparation(tmp_path)
    first = prepare.prepare(backend, env)
    assert first["steps"][0]["state"] == "succeeded"
    assert "setup-secret-example" not in str(first)
    assert "[secret:BUILD_TOKEN]" in str(first)
    assert prepare.prepare(backend, env)["boot_id"] == first["boot_id"]
    read = backend.manager.exec_capture(
        backend.spec,
        [
            "/bin/sh",
            "-c",
            'test "$(cat setup-count)" = setup; test "$(cat start-count)" = start; python3 -c "import urllib.request; urllib.request.urlopen(\'http://127.0.0.1:18991\')"',
        ],
        timeout_s=10,
    )
    assert read.returncode == 0, (
        read.stderr,
        backend.manager.exec_capture(
            backend.spec,
            ["/bin/sh", "-c", "id; ls -ldn /workspace /workspace/*"],
            user_uid=0,
            user_gid=0,
            timeout_s=10,
        ).stdout,
    )
    count = backend.manager.exec_capture(
        backend.spec,
        [
            "python3",
            "-c",
            "import os; assert not any(os.stat('/proc/'+p).st_uid==30000 and open('/proc/'+p+'/stat').read().rsplit(')',1)[1].split()[0] not in ('Z','X') for p in os.listdir('/proc') if p.isdigit())",
        ],
        timeout_s=10,
    )
    assert count.returncode == 0, count.stderr
    backend.manager.stop(backend.spec)
    # This fixture deliberately has no network: boot the exact named container
    # through Docker; production start also verifies the configured group guard.
    _docker("start", backend.spec.container_name)
    resumed = prepare.prepare(backend, env)
    assert resumed["boot_id"] != first["boot_id"]
    read = backend.manager.exec_capture(
        backend.spec,
        [
            "/bin/sh",
            "-c",
            'test "$(cat setup-count)" = setup; test "$(cat start-count)" = startstart',
        ],
        timeout_s=10,
    )
    assert read.returncode == 0, read.stderr


def test_real_action_executes_command_in_subdirectory_and_stops_only_its_identity(
    retained_command_container,
):
    backend = retained_command_container
    backend.manager.exec_capture(
        backend.spec,
        [
            "/bin/sh",
            "-c",
            "chmod 2770 /workspace; mkdir /workspace/sub; chmod 2770 /workspace/sub",
        ],
        user_uid=backend.manager.backend.uid,
        user_gid=backend.manager.backend.gid,
        timeout_s=10,
    )
    process = backend.spawn_terminal(
        SandboxSpec(
            argv=[
                "/bin/sh",
                "-lc",
                'test "$ACTION_VALUE" = action-secret-example; pwd; setsid sleep 60 & sleep 60',
            ],
            workdir="/workspace/sub",
            env={"ACTION_VALUE": "action-secret-example", "AGENT_ACTION_UID": "31000"},
        ),
        rows=24,
        cols=80,
    )
    time.sleep(0.5)
    sibling = backend.manager.exec_capture(
        backend.spec, ["/bin/sh", "-c", "sleep 60 >/dev/null 2>&1 & echo $!"], timeout_s=10
    )
    assert sibling.returncode == 0
    process.kill()
    assert process.wait() != 0
    result = backend.manager.exec_capture(
        backend.spec,
        [
            "python3",
            "-c",
            "import os; uids=[os.stat('/proc/'+p).st_uid for p in os.listdir('/proc') if p.isdigit() and open('/proc/'+p+'/stat').read().rsplit(')',1)[1].split()[0] not in ('Z','X')]; assert 31000 not in uids; assert 10001 in uids",
        ],
        timeout_s=10,
    )
    assert result.returncode == 0, result.stderr


def test_real_snapshot_freezes_and_thaws_environment_services_and_actions(
    retained_command_container, tmp_path
):
    backend = retained_command_container
    prepare = EnvironmentPreparation(tmp_path)
    prepare.prepare(
        backend, BoundEnvironment(binding_id="writer-binding", start_script="sleep 60 &")
    )
    action = backend.spawn_terminal(
        SandboxSpec(
            argv=["/bin/sh", "-c", "sleep 60"],
            workdir="/workspace",
            env={"AGENT_ACTION_UID": "31000"},
        ),
        rows=24,
        cols=80,
    )
    time.sleep(0.3)
    container_id = backend.manager._require(backend.spec).container_id
    manifest = backend.manager.freeze_shared_writers(
        backend.spec, slot_ids=[], token=uuid.uuid4().hex, expected_container_id=container_id
    )
    try:
        checked = backend.manager.exec_capture(
            backend.spec,
            [
                "python3",
                "-c",
                "import os; rows=[(os.stat('/proc/'+p).st_uid,open('/proc/'+p+'/stat').read().rsplit(')',1)[1].split()[0]) for p in os.listdir('/proc') if p.isdigit()]; assert any(u==30001 and s=='T' for u,s in rows); assert any(u==31000 and s=='T' for u,s in rows); assert all(s in ('T','Z','X') for u,s in rows if u in (30001,31000))",
            ],
            user_uid=0,
            user_gid=0,
            workdir="/",
            timeout_s=10,
        )
        assert checked.returncode == 0, checked.stderr
    finally:
        backend.manager.thaw_shared_writers(
            backend.spec, slot_ids=[], manifest=manifest, expected_container_id=container_id
        )
    checked = backend.manager.exec_capture(
        backend.spec,
        [
            "python3",
            "-c",
            "import os; rows=[(os.stat('/proc/'+p).st_uid,open('/proc/'+p+'/stat').read().rsplit(')',1)[1].split()[0]) for p in os.listdir('/proc') if p.isdigit()]; assert any(u==30001 and s not in ('T','Z','X') for u,s in rows); assert any(u==31000 and s not in ('T','Z','X') for u,s in rows)",
        ],
        workdir="/",
        timeout_s=10,
    )
    assert checked.returncode == 0, checked.stderr
    action.kill()
    action.wait()


def test_real_manual_action_keeps_one_execution_after_terminal_eviction_and_restart(
    retained_command_container, tmp_path
):
    from cloud_agent_host.action_receipts import ActionReceipts
    from cloud_agent_host.workspace_broker import _TerminalManager
    from cloud_agent_shared.environment import EnvironmentAction

    backend = retained_command_container
    backend.manager.exec_capture(
        backend.spec,
        ["chmod", "2770", "/workspace"],
        user_uid=backend.manager.backend.uid,
        user_gid=backend.manager.backend.gid,
        timeout_s=10,
    )
    bound = BoundEnvironment(
        binding_id="action-receipt-binding",
        variables=[EnvironmentVariable(name="TOKEN", value="action-private-example")],
        actions=[
            EnvironmentAction(
                id="deploy",
                name="Deploy",
                command='printf x >> /workspace/action-count; printf "%s" "$TOKEN"',
            )
        ],
    )
    receipts = ActionReceipts(tmp_path)
    manager = _TerminalManager(
        idle_ttl_s=60, hard_ttl_s=120, max_sessions=4, action_receipts=receipts
    )
    arguments = {
        "rows": 24,
        "cols": 80,
        "backend": backend,
        "environment": bound,
        "action_id": "deploy",
        "run_id": str(uuid.uuid4()),
    }
    result = manager.create("workspace", "/workspace", **arguments)
    try:
        deadline = time.monotonic() + 30
        while receipts.read("workspace", result["id"])["state"] != "exited":
            assert time.monotonic() < deadline
            time.sleep(0.05)
        manager.delete("workspace", result["id"])
        restarted = _TerminalManager(
            idle_ttl_s=60, hard_ttl_s=120, max_sessions=4, action_receipts=ActionReceipts(tmp_path)
        )
        repeated = restarted.create("workspace", "/workspace", **arguments)
        assert repeated["state"] == "exited" and repeated["exit_code"] == 0
        assert "action-private-example" not in repeated["output"]
        assert "[secret:TOKEN]" in repeated["output"]
        count = backend.manager.exec_capture(
            backend.spec, ["cat", "/workspace/action-count"], timeout_s=10
        )
        assert count.returncode == 0 and count.stdout == b"x"
    finally:
        manager.delete("workspace", result["id"])
