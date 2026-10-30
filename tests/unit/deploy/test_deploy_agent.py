"""The deployment agent on a temporary deployment root with a recorded Docker (WP6-WP8).

These run the agent's real store, contract checks, journal and evidence
rules; only the commands it would run are recorded instead of executed.
"""

from __future__ import annotations

import hashlib
import io
import json
import stat
import tarfile
from pathlib import Path
from typing import Any

import pytest
from deploy.agent import releases
from deploy.agent.cli import main
from deploy.agent.client import sync_once
from deploy.agent.deployment_contract import (
    ContractError,
    check_config_changes,
    check_operation,
    generate_keypair,
    open_sealed,
    seal,
    sealed_purpose,
)
from deploy.agent.operations import Executor
from deploy.agent.store import AgentStore, parse_env, render_env, write_json
from tests.support.deploy_agent_fakes import FakeMachine

REPO = Path(__file__).resolve().parents[3]


def _agent(
    tmp_path: Path, role: str = "control-plane", env: str = ""
) -> tuple[Executor, FakeMachine]:
    root = tmp_path / "deploy-root"
    root.mkdir(parents=True)
    (root / ".env").write_text(
        env or "AGENT_CONTROL_PLANE_PORT=8000\nAGENT_POSTGRES_PASSWORD=keep-me\n"
    )
    assert main(["--root", str(root), "init", "--role", role, "--name", f"{role}-1"]) == 0
    store = AgentStore(root)
    identity = store.identity()
    identity["agent_id"] = "a" * 32
    store.save_identity(identity)
    write_json(
        store.root / "policy.json",
        {
            "allowed": {
                "AGENT_SANDBOX_RUNTIME": ["io.containerd.kata.v2"],
                "AGENT_EGRESS_DNS_SERVER": ["10.0.0.53"],
                "AGENT_OPERATION_PROFILING_MODE": ["disabled", "admin"],
            },
            "kata": [{"version": "3.32.0", "sha256": "1" * 64}],
            "kata_helper": ["sudo", "-n", "/opt/kata-helper"],
            "compose": {"project": "cloud-agent"},
        },
        0o644,
    )
    machine = FakeMachine(root)
    executor = Executor(store, machine, health_timeout=0.05, health_interval=0.01)
    executor.http_check = lambda url: True
    return executor, machine


def test_render_env_keeps_every_other_line_byte_for_byte() -> None:
    before = "# comment\nA=1\nexport B='two words'\n\nC=3\n"
    after = render_env(before, {"A": "9", "D": "4"})
    assert after == "# comment\nA=9\nexport B='two words'\n\nC=3\nD=4\n"
    assert parse_env(after)["B"] == "'two words'"


def test_init_adopts_the_existing_env_as_version_zero(tmp_path) -> None:
    executor, _ = _agent(tmp_path)
    store = executor.store
    assert store.versions() == [0]
    assert store.version_text(0) == store.current_env()
    assert stat.S_IMODE((store.root / "key.pem").stat().st_mode) == 0o600
    assert stat.S_IMODE((store.root / "config" / "0.env").stat().st_mode) == 0o600


def test_a_healthy_change_is_kept_and_restarts_only_its_services(tmp_path) -> None:
    executor, machine = _agent(tmp_path)
    outcome = executor.execute("apply_config", {"changes": {"AGENT_WEB_PORT": "3010"}})
    assert outcome.state == "succeeded", outcome.detail
    assert parse_env(executor.store.current_env())["AGENT_WEB_PORT"] == "3010"
    # An unmanaged line is untouched.
    assert "AGENT_POSTGRES_PASSWORD=keep-me" in executor.store.current_env()
    recreate = next(c for c in machine.calls if "up" in c)
    assert recreate[-4:] == ["control-plane", "mcp-relay", "web", "edge"]
    assert executor.store.state()["config_rev"] == 1


def test_an_unhealthy_change_restores_the_last_good_version(tmp_path) -> None:
    executor, machine = _agent(tmp_path)
    machine.unhealthy_when = {"web": "AGENT_WEB_PORT=3999"}
    outcome = executor.execute("apply_config", {"changes": {"AGENT_WEB_PORT": "3999"}})
    assert outcome.state == "failed"
    assert "restored version 0" in outcome.detail
    assert "AGENT_WEB_PORT" not in parse_env(executor.store.current_env())
    assert executor.store.state()["config_rev"] == 0
    assert outcome.evidence["failed_rev"] == 1


def test_a_sealed_secret_is_opened_only_by_this_agent_and_never_journaled(tmp_path) -> None:
    executor, _ = _agent(tmp_path)
    public = executor.store.identity()["public_key"]
    box = seal(
        public,
        "new-admin-token-of-at-least-32-bytes",
        sealed_purpose(executor.agent_id, "ADMIN_TOKEN"),
    )
    outcome = executor.execute("apply_config", {"changes": {}}, {"ADMIN_TOKEN": box})
    assert outcome.state == "succeeded", outcome.detail
    assert (
        parse_env(executor.store.current_env())["ADMIN_TOKEN"]
        == "new-admin-token-of-at-least-32-bytes"
    )
    journal = json.dumps(executor.store.operations())
    assert "new-admin-token" not in journal
    assert stat.S_IMODE(executor.store.env_path.stat().st_mode) == 0o600
    # Bound to its purpose: the same box does not open as another key.
    with pytest.raises(ContractError):
        open_sealed(executor.store.private_key(), box, sealed_purpose(executor.agent_id, "OTHER"))


def test_a_secret_sent_in_the_clear_or_a_forbidden_key_is_rejected(tmp_path) -> None:
    executor, machine = _agent(tmp_path)
    clear = executor.execute("apply_config", {"changes": {"ADMIN_TOKEN": "plain"}})
    assert clear.state == "rejected" and "sealed" in clear.detail
    host, _ = _agent(tmp_path / "h", role="host", env="AGENT_EGRESS_PROTECTED_CIDRS=10.0.0.0/8\n")
    widened = host.execute("apply_config", {"changes": {"AGENT_SANDBOX_ALLOW_UNISOLATED": "1"}})
    assert widened.state == "rejected"
    assert machine.calls == []


def test_boundary_values_follow_the_local_policy_and_protection_only_grows(tmp_path) -> None:
    host, _ = _agent(tmp_path, role="host", env="AGENT_EGRESS_PROTECTED_CIDRS=10.0.0.0/8\n")
    assert (
        host.execute("apply_config", {"changes": {"AGENT_SANDBOX_RUNTIME": "runc"}}).state
        == "rejected"
    )
    shrink = host.execute(
        "apply_config", {"changes": {"AGENT_EGRESS_PROTECTED_CIDRS": "192.168.0.0/16"}}
    )
    assert shrink.state == "rejected" and "only add" in shrink.detail
    grow = host.execute(
        "apply_config", {"changes": {"AGENT_EGRESS_PROTECTED_CIDRS": "10.0.0.0/8,192.168.0.0/16"}}
    )
    assert grow.state == "succeeded", grow.detail
    allowed = host.execute(
        "apply_config", {"changes": {"AGENT_SANDBOX_RUNTIME": "io.containerd.kata.v2"}}
    )
    assert allowed.state == "succeeded", allowed.detail


def test_local_rollback_works_without_any_control_plane(tmp_path) -> None:
    executor, _ = _agent(tmp_path)
    executor.execute("apply_config", {"changes": {"AGENT_WEB_PORT": "3010"}})
    executor.execute("apply_config", {"changes": {"AGENT_WEB_PORT": "3020"}})
    root = str(executor.store.deploy_root)
    # The CLI builds its own executor with the real runner; use the API here.
    outcome = executor.execute("rollback_config", {"to_rev": 1})
    assert outcome.state == "succeeded", outcome.detail
    assert parse_env(executor.store.current_env())["AGENT_WEB_PORT"] == "3010"
    assert root


class FakeClient:
    def __init__(self, intents: list[dict[str, Any]]) -> None:
        self._intents = intents
        self.results: list[tuple[str, str, str]] = []
        self.reports: list[dict[str, Any]] = []

    def report(self, observed, operations) -> None:
        self.reports.append({"observed": observed, "operations": operations})

    def intents(self, after_seq):
        return [i for i in self._intents if i["seq"] > after_seq] + [
            i for i in self._intents if i.get("replay")
        ]

    def result(self, intent_id, state, detail, evidence) -> None:
        self.results.append((intent_id, state, detail))


def _intent(executor: Executor, seq: int, **overrides: Any) -> dict[str, Any]:
    intent = {
        "intent_id": f"{seq:032x}",
        "agent_id": executor.agent_id,
        "seq": seq,
        "operation": "restart_services",
        "params": {"services": ["web"]},
        "sealed": {},
        "expires_at": 4_000_000_000,
    }
    intent.update(overrides)
    return intent


def test_sync_runs_new_intents_and_refuses_replayed_expired_and_foreign_ones(tmp_path) -> None:
    executor, _ = _agent(tmp_path)
    client = FakeClient(
        [
            _intent(executor, 1),
            _intent(executor, 2, expires_at=1),
            _intent(executor, 3, agent_id="b" * 32),
        ]
    )
    sync_once(executor, client)  # type: ignore[arg-type]
    finals = {intent_id: state for intent_id, state, _ in client.results if state != "running"}
    assert finals == {f"{1:032x}": "succeeded", f"{2:032x}": "rejected", f"{3:032x}": "rejected"}
    # The same intent again is a replay: nothing runs.
    again = FakeClient([_intent(executor, 1, replay=True)])
    sync_once(executor, again)  # type: ignore[arg-type]
    assert again.results == [
        (f"{1:032x}", "rejected", "replayed intent: its sequence number was already used")
    ]
    observed = client.reports[-1]["observed"]
    assert "keep-me" not in json.dumps(observed)
    assert "AGENT_POSTGRES_PASSWORD" in observed["unmanaged_keys"]


def _release(
    tmp_path: Path, commit: str = "c" * 40, revision: str = "0109_deployment_agents"
) -> tuple[Path, Path, str]:
    recipe = tmp_path / "recipe.tar.gz"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for role in ("control-plane", "host"):
            data = b"#!/bin/bash\nexit 0\n"
            info = tarfile.TarInfo(f"recipe/deploy/{role}/deploy.sh")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    recipe.write_bytes(buffer.getvalue())
    manifest = json.loads((REPO / "tests/fixtures/release_manifest_v1.json").read_text())
    manifest["source_commit"] = commit
    manifest["database_revision"] = revision
    manifest["recipe"]["sha256"] = hashlib.sha256(recipe.read_bytes()).hexdigest()
    path = tmp_path / "release.json"
    path.write_text(json.dumps(manifest))
    return path, recipe, hashlib.sha256(path.read_bytes()).hexdigest()


def test_a_release_imports_only_with_a_matching_pin_and_is_rechecked_before_use(tmp_path) -> None:
    executor, machine = _agent(tmp_path)
    manifest, recipe, digest = _release(tmp_path)
    with pytest.raises(releases.ReleaseError, match="pin"):
        releases.import_release(executor.store, manifest, recipe)
    with pytest.raises(releases.ReleaseError, match="does not match"):
        releases.import_release(executor.store, manifest, recipe, "0" * 64)
    entry = releases.import_release(executor.store, manifest, recipe, digest)
    assert entry["manifest_sha256"] == digest
    # Tampering after import is refused before any phase runs.
    stored = executor.store.release_dir(digest) / "release.json"
    stored.write_text(stored.read_text() + " ")
    outcome = executor.execute("release_prepare", {"manifest_sha256": digest})
    assert outcome.state == "failed" and "changed after it was imported" in outcome.detail
    assert not any(call[0] == "bash" for call in machine.calls)


def test_activate_succeeds_only_when_the_deployment_records_the_release(tmp_path) -> None:
    executor, machine = _agent(tmp_path)
    manifest, recipe, digest = _release(tmp_path)
    releases.import_release(executor.store, manifest, recipe, digest)
    # Exit 0 without .deployed-sha changing is not success.
    machine.activate_writes = None
    lying = executor.execute("release_activate", {"manifest_sha256": digest})
    assert lying.state == "failed" and "does not record" in lying.detail
    machine.activate_writes = "c" * 40
    real = executor.execute("release_activate", {"manifest_sha256": digest})
    assert real.state == "succeeded", real.detail
    call = next(c for c in machine.calls if c[0] == "bash")
    assert call[-2:] == ["--phase", "activate"]
    env = machine.envs[machine.calls.index(call)]
    assert env["AGENT_DEPLOY_MODE"] == "artifacts"
    assert env["AGENT_RELEASE_MANIFEST"].endswith(f"{digest}/release.json")


def test_prepare_requires_the_release_images_to_be_present(tmp_path) -> None:
    executor, machine = _agent(tmp_path)
    manifest, recipe, digest = _release(tmp_path)
    releases.import_release(executor.store, manifest, recipe, digest)
    image = json.loads(manifest.read_text())["artifacts"][0]["image"]
    machine.missing_images = {image}
    outcome = executor.execute("release_prepare", {"manifest_sha256": digest})
    assert outcome.state == "failed" and "not present" in outcome.detail


def test_an_unverified_move_to_an_older_database_revision_is_refused(tmp_path) -> None:
    executor, _ = _agent(tmp_path)
    (tmp_path / "new").mkdir()
    (tmp_path / "old").mkdir()
    new_manifest, new_recipe, new_digest = _release(
        tmp_path / "new", "d" * 40, "0109_deployment_agents"
    )
    old_manifest, old_recipe, old_digest = _release(tmp_path / "old", "e" * 40, "0100_older")
    releases.import_release(executor.store, new_manifest, new_recipe, new_digest)
    releases.import_release(executor.store, old_manifest, old_recipe, old_digest)
    (executor.store.deploy_root / ".deployed-sha").write_text("d" * 40)
    outcome = executor.execute("release_activate", {"manifest_sha256": old_digest})
    assert outcome.state == "failed" and "never verified" in outcome.detail


def test_kata_install_needs_a_reviewed_pair_and_the_scripts_own_check(tmp_path) -> None:
    host, machine = _agent(tmp_path, role="host", env="AGENT_RUNNER_HOST=gpu-1\n")
    unreviewed = host.execute("kata_install", {"version": "4.0.0", "sha256": "2" * 64})
    assert unreviewed.state == "rejected"
    machine.kata_check_exit = 1
    failed = host.execute("kata_install", {"version": "3.32.0", "sha256": "1" * 64})
    assert failed.state == "failed"
    machine.kata_check_exit = 0
    ok = host.execute("kata_install", {"version": "3.32.0", "sha256": "1" * 64})
    assert ok.state == "succeeded", ok.detail
    helper_calls = [c for c in machine.calls if c[:3] == ["sudo", "-n", "/opt/kata-helper"]]
    assert ["sudo", "-n", "/opt/kata-helper", "install", "3.32.0", "1" * 64] in helper_calls
    # The runner was stopped first and started again after.
    assert any("stop" in c and "runner" in c for c in machine.calls)
    assert host.observed()["kata"]["version"] == "3.32.0"


def test_the_contract_refuses_unknown_operations_and_parameters() -> None:
    with pytest.raises(ContractError):
        check_operation("shell", "host", {})
    with pytest.raises(ContractError):
        check_operation("restart_services", "host", {"services": ["postgres"]})
    with pytest.raises(ContractError):
        check_operation("restart_services", "host", {"services": ["runner"], "command": "x"})
    with pytest.raises(ContractError):
        check_operation("release_prepare_database", "host", {"manifest_sha256": "a" * 64})
    with pytest.raises(ContractError):
        check_config_changes("control-plane", {"AGENT_CONTROL_PLANE_COMPOSE_OVERRIDE_FILE": "/x"})


def test_a_keypair_seals_and_opens() -> None:
    pem, public = generate_keypair()
    box = seal(public, "value", "agent:KEY")
    assert open_sealed(pem, box, "agent:KEY") == "value"
    other_pem, _ = generate_keypair()
    with pytest.raises(ContractError):
        open_sealed(other_pem, box, "agent:KEY")


def test_a_sandbox_manager_credential_rotation_stages_then_promotes(tmp_path) -> None:
    from deploy.agent.rotation import register

    host, _ = _agent(
        tmp_path,
        role="host",
        env="AGENT_SANDBOX_MANAGER_TOKEN=old-credential-of-thirty-two-bytes!!\n",
    )
    register(host)
    new = "new-credential-of-thirty-two-bytes!!"
    box = seal(
        host.store.identity()["public_key"],
        new,
        sealed_purpose(host.agent_id, "AGENT_SANDBOX_MANAGER_TOKEN_NEXT"),
    )
    params = {"workflow": "sandbox_manager_token", "operation_id": "3" * 32, "step": "stage"}
    staged = host.execute("rotation_step", params, {"AGENT_SANDBOX_MANAGER_TOKEN_NEXT": box})
    assert staged.state == "succeeded", staged.detail
    env = parse_env(host.store.current_env())
    assert env["AGENT_SANDBOX_MANAGER_TOKEN"].startswith("old-")
    assert env["AGENT_SANDBOX_MANAGER_TOKEN_NEXT"] == new
    from deploy.agent.deployment_contract import key_identifier

    assert host.observed()["secret_ids"]["AGENT_SANDBOX_MANAGER_TOKEN_NEXT"] == key_identifier(new)
    promoted = host.execute("rotation_step", {**params, "step": "promote"})
    assert promoted.state == "succeeded", promoted.detail
    env = parse_env(host.store.current_env())
    assert env["AGENT_SANDBOX_MANAGER_TOKEN"] == new
    assert "AGENT_SANDBOX_MANAGER_TOKEN_NEXT" not in env
    assert new not in json.dumps(host.store.operations())


def test_a_host_restart_drains_the_runner_before_the_sandbox_manager(tmp_path) -> None:
    """A Compose stop would kill a long turn at its grace; a rotation must not."""
    host, machine = _agent(tmp_path, role="host", env="AGENT_RUNNER_HOST=gpu-1\n")
    healthy, _states = host._restart_and_check(["sandbox-manager", "runner"])
    assert healthy
    stops = [call for call in machine.calls if call[:2] == ["docker", "stop"]]
    assert stops == [
        ["docker", "stop", "--time=-1", "runner-container"],
        ["docker", "stop", "--time=-1", "sandbox-manager-container"],
    ]
    recreate = next(i for i, c in enumerate(machine.calls) if "--force-recreate" in c)
    assert machine.calls.index(stops[1]) < recreate


def _tunnel_host(tmp_path: Path) -> tuple[Executor, FakeMachine, dict[str, Path]]:
    from deploy.agent.rotation import register

    keys = tmp_path / "keys"
    keys.mkdir()
    paths = {name: keys / f"{name}.key" for name in ("gateway", "mcp", "control")}
    env = ""
    for name, prefix in (
        ("gateway", "AGENT_TUNNEL"),
        ("mcp", "AGENT_MCP_TUNNEL"),
        ("control", "AGENT_CONTROL_PLANE_TUNNEL"),
    ):
        paths[name].write_text(f"old {name}")
        env += f"{prefix}_SSH_KEY={paths[name]}\n{prefix}_SSH_DESTINATION=tunnel@far\n"
    host, machine = _agent(tmp_path, role="host", env=env)
    register(host)
    return host, machine, paths


def _tunnel(step: str, **more: Any) -> dict[str, Any]:
    return {"workflow": "tunnel_keys", "operation_id": "4" * 32, "step": step, **more}


def test_a_tunnel_key_is_generated_here_proved_then_swapped_in(tmp_path) -> None:
    host, machine, paths = _tunnel_host(tmp_path)
    made = host.execute("rotation_step", _tunnel("generate"))
    assert made.state == "succeeded", made.detail
    public = made.evidence["public_keys"]
    assert set(public["new"]) == {"gateway", "mcp", "control"}
    assert public["new"]["mcp"] != public["old"]["mcp"]
    assert "private" not in json.dumps(public)
    # Asking again returns the same keys rather than a second set.
    assert host.execute("rotation_step", _tunnel("generate")).evidence == made.evidence

    switched = host.execute("rotation_step", _tunnel("switch"))
    assert switched.state == "succeeded", switched.detail
    assert paths["gateway"].read_text().startswith("private key")
    assert (paths["gateway"].with_name("gateway.key.previous")).read_text() == "old gateway"
    tries = [c for c in machine.calls if c[0] == "ssh"]
    assert len(tries) == 3 and all("-o" in c and "BatchMode=yes" in c for c in tries)
    assert any("--force-recreate" in c and "control-plane-tunnel" in c for c in machine.calls)

    assert host.execute("rotation_step", _tunnel("finish")).state == "succeeded"
    assert not (paths["gateway"].with_name("gateway.key.previous")).exists()


def test_a_key_the_far_end_refuses_changes_nothing(tmp_path) -> None:
    host, machine, paths = _tunnel_host(tmp_path)
    assert host.execute("rotation_step", _tunnel("generate")).state == "succeeded"
    machine.ssh_exit = 255
    refused = host.execute("rotation_step", _tunnel("switch"))
    assert refused.state == "failed" and "refuses the new key" in refused.detail
    assert paths["mcp"].read_text() == "old mcp"
    assert not any("--force-recreate" in c for c in machine.calls)


def test_tunnels_that_do_not_come_back_get_their_old_keys(tmp_path) -> None:
    host, machine, paths = _tunnel_host(tmp_path)
    assert host.execute("rotation_step", _tunnel("generate")).state == "succeeded"
    machine.unhealthy.add("mcp-tunnel")
    failed = host.execute("rotation_step", _tunnel("switch"))
    assert failed.state == "failed" and "previous keys were restored" in failed.detail
    assert paths["mcp"].read_text() == "old mcp"


def test_a_far_end_is_edited_only_where_local_policy_says(tmp_path) -> None:
    from deploy.agent.rotation import register

    cp, _ = _agent(tmp_path)
    register(cp)
    keys = tmp_path / "authorized_keys"
    keys.write_text("ssh-ed25519 KEEPTHIS other\n")
    write_json(
        cp.store.root / "policy.json",
        {
            **cp.store.policy(),
            "tunnel_authorized_keys": {
                "mcp": {
                    "path": str(keys),
                    "options": 'restrict,port-forwarding,permitopen="127.0.0.1:8001"',
                }
            },
        },
        0o644,
    )
    new, old = "ssh-ed25519 " + "A" * 43, "ssh-ed25519 " + "B" * 43
    add = cp.execute(
        "rotation_step",
        _tunnel("authorize", host="gpu-1", tunnels={"mcp": new}),
    )
    assert add.state == "succeeded", add.detail
    assert f'restrict,port-forwarding,permitopen="127.0.0.1:8001" {new}' in keys.read_text()
    assert "KEEPTHIS" in keys.read_text()
    refused = cp.execute(
        "rotation_step", _tunnel("authorize", host="gpu-1", tunnels={"control": new})
    )
    assert refused.state == "failed" and "does not manage" in refused.detail
    # A key that is not an Ed25519 public key never reaches the file.
    bad = cp.execute(
        "rotation_step", _tunnel("authorize", host="gpu-1", tunnels={"mcp": "ssh-rsa AAAA x"})
    )
    assert bad.state == "rejected"
    keys.write_text(keys.read_text() + f"{old} legacy\n")
    assert (
        cp.execute("rotation_step", _tunnel("revoke", host="gpu-1", tunnels={"mcp": old})).state
        == "succeeded"
    )
    assert old not in keys.read_text() and new in keys.read_text()


def test_the_sandbox_manager_accepts_the_next_credential_during_a_rotation(monkeypatch) -> None:
    from fastapi.testclient import TestClient
    from tests.unit.test_agent_sandbox_manager import _SharedSupervisor

    from cloud_agent_host import sandbox_manager
    from cloud_agent_host.sandbox import ProcessBackend

    monkeypatch.setenv("AGENT_SANDBOX_MANAGER_TOKEN_NEXT", "next-credential-of-thirty-two-bytes!")
    client = TestClient(
        sandbox_manager.create_app(
            backend=ProcessBackend(acknowledged_unsafe=True),
            token="current-credential-thirty-two-bytes!",
            shared_session_supervisor=_SharedSupervisor(),  # type: ignore[arg-type]
        )
    )
    for token, status in (
        ("current-credential-thirty-two-bytes!", 200),
        ("next-credential-of-thirty-two-bytes!", 200),
        ("someone-elses-credential-32-bytes!!!", 401),
    ):
        answer = client.post(
            "/profiling-policy", json={}, headers={"X-Agent-Workspace-Token": token}
        )
        assert (answer.status_code == 401) == (status == 401), (token, answer.status_code)


def test_a_managed_server_environment_follows_the_same_key_rules() -> None:
    from deploy.agent.deployment_contract import check_rendered_environment

    check_rendered_environment(
        "host", {"AGENT_SANDBOX_IMAGE_PULL": "true", "AGENT_EGRESS_DNS_SERVER": "10.0.0.53"}
    )
    with pytest.raises(ContractError):
        check_rendered_environment("host", {"AGENT_SANDBOX_IMAGE_PULL": "always"})
    with pytest.raises(ContractError):
        check_rendered_environment("host", {"AGENT_SANDBOX_ALLOW_UNISOLATED": "1"})
