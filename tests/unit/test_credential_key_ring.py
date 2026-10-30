"""The credential cipher's key ring, the staged-replacement rule, and the domain manifest."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from cloud_agent import settings
from cloud_agent.credential_cipher import CredentialCipher, CredentialCipherError
from cloud_agent.credential_families import FAMILIES

OLD = "an-old-credential-key-of-thirty-two-bytes!"
NEW = "a-new-credential-key-of-thirty-two-bytes!!"
DOMAIN = b"test/domain/v1"


def test_the_ring_encrypts_with_the_new_key_and_still_opens_the_old_one():
    old_ciphertext = CredentialCipher(OLD, domain=DOMAIN).encrypt("value")
    ring = CredentialCipher(NEW, domain=DOMAIN, previous=OLD)
    assert ring.decrypt(old_ciphertext) == "value"
    assert not ring.opens_with_current(old_ciphertext)
    fresh = ring.encrypt("value")
    assert ring.opens_with_current(fresh)
    # The old key alone cannot open what the ring now writes.
    with pytest.raises(CredentialCipherError):
        CredentialCipher(OLD, domain=DOMAIN).decrypt(fresh)
    with pytest.raises(CredentialCipherError):
        CredentialCipher(NEW, domain=DOMAIN).decrypt(old_ciphertext)


def test_lookup_digests_follow_the_index_key_and_list_both_during_the_window():
    old_mac = CredentialCipher(OLD, domain=DOMAIN).mac(b"m")
    new_mac = CredentialCipher(NEW, domain=DOMAIN).mac(b"m")
    on_previous = CredentialCipher(NEW, domain=DOMAIN, previous=OLD, index_previous=True)
    assert on_previous.mac(b"m") == old_mac
    assert on_previous.macs(b"m") == [old_mac, new_mac]
    assert on_previous.mac_with_current(b"m") == new_mac
    assert on_previous.mac_with_previous(b"m") == old_mac
    switched = CredentialCipher(NEW, domain=DOMAIN, previous=OLD)
    assert switched.mac(b"m") == new_mac
    assert switched.macs(b"m") == [new_mac, old_mac]
    # Outside a rotation there is one digest, and no previous one.
    alone = CredentialCipher(NEW, domain=DOMAIN, index_previous=True)
    assert alone.macs(b"m") == [new_mac] and alone.mac_with_previous(b"m") is None


def _encoded(**values):
    return settings.encode_changes(values)


@pytest.fixture()
def root(monkeypatch):
    monkeypatch.setenv("AGENT_SESSION_SECRET", "a-session-secret-of-at-least-32-bytes!!")


def test_a_credential_key_is_replaced_only_by_a_staged_rotation(root):
    before = _encoded(source_control_encryption_key=OLD)
    with pytest.raises(ValueError, match="cannot be replaced"):
        settings.validate_settings_update(before, _encoded(source_control_encryption_key=NEW))
    settings.validate_settings_update(
        before,
        _encoded(source_control_encryption_key=NEW, source_control_encryption_key_previous=OLD),
    )
    # A second rotation may not replace the key while the first still keeps one.
    staged = _encoded(source_control_encryption_key=NEW, source_control_encryption_key_previous=OLD)
    with pytest.raises(ValueError, match="cannot be replaced"):
        settings.validate_settings_update(
            staged,
            _encoded(
                source_control_encryption_key="a-third-credential-key-of-thirty-two-b!",
                source_control_encryption_key_previous=NEW,
            ),
        )


def test_rotation_settings_are_shown_read_only_and_marked_workflow_only(root):
    view = settings.settings_response(1, {})
    by_key = {d["key"]: d for d in view["definitions"]}
    for key in (
        "source_control_encryption_key_previous",
        "compute_encryption_key_previous",
        "credential_index_uses_previous",
        "control_signing_secret_previous",
    ):
        assert by_key[key]["read_only"] is True
        assert by_key[key]["workflow_only"] is True


_OTHER_KEY_DOMAINS = {
    # Under the settings root key, rotated by the root key workflow.
    "cloud-agent/admin-settings/v1/",
    "cloud-agent/rotation-secret/v1",
    "_DOMAIN@sandbox_archive_credentials",
}


def _domain_names() -> set[str]:
    """Every domain expression a cipher is built with across the backend."""
    found: set[str] = set()
    root = Path(__file__).resolve().parents[2] / "apps" / "backend" / "cloud_agent"
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", getattr(node.func, "attr", ""))
            if name not in {"CredentialCipher", "user_credential_cipher", "_credential_cipher"}:
                continue
            exprs = [k.value for k in node.keywords if k.arg == "domain"]
            if name == "user_credential_cipher" and node.args:
                exprs.append(node.args[0])
            for expr in exprs:
                if isinstance(expr, ast.Name):
                    found.add(
                        f"{expr.id}@{path.stem}" if expr.id in {"_DOMAIN", "domain"} else expr.id
                    )
                elif isinstance(expr, ast.Constant):
                    found.add(expr.value.decode())
                elif isinstance(expr, ast.BinOp) and isinstance(expr.left, ast.Constant):
                    found.add(expr.left.value.decode())
                else:
                    found.add(ast.unparse(expr))
    return found


def test_every_cipher_domain_is_a_rotated_family_or_another_keys():
    import cloud_agent.application.model_connections as model_connections
    import cloud_agent.application.ssh_servers as ssh_servers
    import cloud_agent.mcp.registry as registry
    import cloud_agent.source_control as source_control
    from cloud_agent_shared import environment

    family_domains = {f.domain for f in FAMILIES}
    known = {
        "SOURCE_CONTROL_DOMAIN": source_control.SOURCE_CONTROL_DOMAIN,
        "ENVIRONMENT_DOMAIN": environment.ENVIRONMENT_DOMAIN,
        "SSH_SERVER_KEY_DOMAIN": ssh_servers.SSH_SERVER_KEY_DOMAIN,
        "MODEL_CONNECTION_KEY_DOMAIN": model_connections.MODEL_CONNECTION_KEY_DOMAIN,
        "MODEL_CONNECTION_SCAN_DOMAIN": model_connections.MODEL_CONNECTION_SCAN_DOMAIN,
        "MCP_CREDENTIAL_DOMAIN": registry.MCP_CREDENTIAL_DOMAIN,
        "MCP_INVOCATION_DOMAIN": registry.MCP_INVOCATION_DOMAIN,
        "MCP_OAUTH_DOMAIN": registry.MCP_OAUTH_DOMAIN,
        "_DOMAIN@compute_secrets": b"cloud-agent/compute-credentials/v1",
        # Pass-through parameters of the factories themselves.
        "domain@credential_cipher": None,
        "domain@registry": None,
        "domain@source_control": None,
        "domain@environments": None,
        "domain@credential_key_rotation": None,
    }
    unexplained = []
    for name in _domain_names():
        if name in _OTHER_KEY_DOMAINS:
            continue
        if name not in known:
            unexplained.append(name)
            continue
        if known[name] is not None and known[name] not in family_domains:
            unexplained.append(name)
    assert unexplained == [], "a cipher domain no family lists: " + ", ".join(sorted(unexplained))
