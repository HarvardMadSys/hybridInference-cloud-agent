"""Compatibility checks for the credential KDF shared by storage domains."""

from __future__ import annotations

import pytest

from cloud_agent.credential_cipher import CredentialCipher, CredentialCipherError

_SECRET = "compatibility-key"

# These tokens were produced by the pre-extraction derivation:
# Fernet(urlsafe_b64encode(sha256(domain + b"\\0" + secret))).
_LEGACY_CIPHERTEXTS = (
    (
        b"hybridinference/source-control/v1",
        "source-value",
        "gAAAAABlU_EAzbNgTuVAK8SF9y-D1FhDi8-8e9yxseLaH0EW6KMAxDMtwJL8LudTGTBHDo46jV4Z88Qwff7SYY6i5zAagF77eg==",
    ),
    (
        b"hybridinference/environment-variable/v1",
        "environment-value",
        "gAAAAABlU_EADKa9eqjaKYI5pCEVoJAJof3TfPDWYeov8PEEoIaN8toaRE4UsVNVANo4pmSTGZxeooI_AwlZDs2HZhc-dCITT7uX2qUQAwvKn_61vES-5D4=",
    ),
    (
        b"hybridinference/mcp-connection/v1",
        "mcp-value",
        "gAAAAABlU_EAOo06vANwP6ShICwvQLfeJFT3vB7IXSXnBfXFAAOz26lI2jNe7DTed_C8RAcELHUsVaJIw-AZb_M5ncl3SKP4hQ==",
    ),
    (
        b"hybridinference/mcp-invocation/v1",
        "invocation-value",
        "gAAAAABlU_EAWJ55LpuBQCy7mHBbKUv5JJ4xPYVNjM8h-sD0MFZwXRJoxQTmNUYYx5C2p9-YsXgsVJ-IxTqmcOjSsdn67woruE_ZJSkmBZfvG4R4r8rpMig=",
    ),
    (
        b"hybridinference/mcp-oauth/v1",
        "oauth-value",
        "gAAAAABlU_EA0FKYIpG50rwEKm_Xa1waXNRIqyfRN_z-a0cy8_rQMpATQPvQ4CW2Zk24DUFWf0U29ZUNbXVxw4f4vJrWC4L0vw==",
    ),
    (
        b"hybridinference/ssh-server-key/v1",
        "ssh-value",
        "gAAAAABlU_EAb1r1WIfmeEslHoxC5nbQ0GjYnlSRY1CMAi0LAw81nhmfUS-e5aEfizjVtm5MDUeStvF5AY5ZSXRRqgIzmbeZkw==",
    ),
    (
        b"cloud-agent/sandbox-archive-aws/v1",
        "archive-value",
        "gAAAAABlU_EAsMGkHiumG-IhMbsNfyDKu6TlQcsoWIWYiWsQ1ijYStwzt7wPyrnaT0iR67p3yYaxpFFnQHQjMXJVJMjOYRWyWg==",
    ),
    (
        b"cloud-agent/admin-settings/v1/example",
        "settings-value",
        "gAAAAABlU_EAbjicHIqBWKSk2BdXbpBzbF34wtp7FCX41sbQNVIHIZFnT3i48di0rPs0jNWM56frzHrLvp89Hyk9Po84w3YJhA==",
    ),
    (
        b"cloud-agent/compute-credentials/v1",
        "compute-value",
        "gAAAAABlU_EAT0aJYzWUi4PmW2kGnWVFjQhmiXAlcdfJrev6UH2Z27ZOXfSSVBdZw3HYxUr18dZcuC4PiPoiV6NgEciM14q2Ng==",
    ),
)


@pytest.mark.parametrize(("domain", "plaintext", "ciphertext"), _LEGACY_CIPHERTEXTS)
def test_legacy_ciphertext_remains_readable(domain: bytes, plaintext: str, ciphertext: str) -> None:
    assert CredentialCipher(_SECRET, domain=domain).decrypt(ciphertext) == plaintext


def test_ciphertexts_remain_domain_separated() -> None:
    source_domain, _, source_ciphertext = _LEGACY_CIPHERTEXTS[0]
    mcp_domain, _, _ = _LEGACY_CIPHERTEXTS[2]

    assert source_domain != mcp_domain
    with pytest.raises(CredentialCipherError):
        CredentialCipher(_SECRET, domain=mcp_domain).decrypt(source_ciphertext)
