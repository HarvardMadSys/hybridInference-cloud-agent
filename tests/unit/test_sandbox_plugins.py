"""Plugin configuration rejects ambiguous names and credential-bearing sources."""

import pytest
from pydantic import ValidationError

from cloud_agent_shared.plugins import (
    SandboxPlugin,
    supported_plugin_runtimes,
    validate_sandbox_plugins,
)


@pytest.mark.parametrize(
    "source",
    [
        "anthropics/claude-plugins-official",
        "https://github.com/example/plugins.git",
        "https://plugins.example.com:443/marketplace.json",
    ],
)
def test_plugin_sources_preserve_explicit_marketplace(source):
    plugin = SandboxPlugin(plugin="code-review@claude-plugins-official", marketplace=source)
    assert plugin.model_dump() == {
        "plugin": "code-review@claude-plugins-official",
        "marketplace": source,
    }


def test_plugin_inputs_trim_surrounding_whitespace():
    plugin = SandboxPlugin(plugin=" review@example ", marketplace=" example/plugins ")
    assert plugin.plugin == "review@example"
    assert plugin.marketplace == "example/plugins"


@pytest.mark.parametrize(
    "name",
    [
        "review",
        "--help",
        "--review@example",
        "review@--example",
        "review@example;id",
        "review@example\nother",
        "review@",
        "review@@example",
    ],
)
def test_plugin_names_reject_options_and_ambiguous_marketplaces(name):
    with pytest.raises(ValueError):
        validate_sandbox_plugins(
            "claude-code", [{"plugin": name, "marketplace": "example/plugins"}]
        )


@pytest.mark.parametrize(
    "source",
    [
        "--help",
        "/workspace/plugins",
        "../plugins",
        "http://example.com/plugins.git",
        "git@example.com:plugins.git",
        "https://user:token@example.com/plugins.git",
        "https://example.com/plugins.git?token=secret",
        "https://example.com/plugins.git#secret",
        "https://example.com:8443/plugins.git",
        "https://example.com:notaport/plugins.git",
        "https://example.com/a b",
        "https://example.com/\nplugins.git",
        "https:///plugins.git",
    ],
)
def test_plugin_sources_reject_private_paths_and_embedded_credentials(source):
    with pytest.raises(ValidationError):
        SandboxPlugin(plugin="review@example", marketplace=source)


def test_plugin_configuration_has_no_arbitrary_command_fields():
    with pytest.raises(ValidationError):
        SandboxPlugin.model_validate(
            {"plugin": "review@example", "marketplace": "example/plugins", "install": "echo unsafe"}
        )


def test_plugin_runtime_support_is_explicit():
    assert supported_plugin_runtimes() == ["claude-code", "codex", "pi", "opencode", "kilo"]


@pytest.mark.parametrize("runtime", ["pi", "opencode", "kilo"])
@pytest.mark.parametrize(
    "package",
    ["my-plugin", "@example/plugin", "my-plugin@1.2.3", "@example/plugin@1.2.3-rc.1+build"],
)
def test_npm_plugin_specs_normalize_native_prefix(runtime, package):
    assert validate_sandbox_plugins(runtime, [{"plugin": "npm:" + package}]) == [
        {"plugin": package}
    ]


@pytest.mark.parametrize(
    "package",
    [
        "../plugin",
        "/tmp/plugin",
        "https://example.com/plugin",
        "--help",
        "plugin@latest",
        "plugin@^1.2.3",
        "plugin;touch",
        "npm:pkg@",
        "@scope/../plugin",
    ],
)
def test_npm_plugin_specs_reject_paths_options_and_ambiguous_versions(package):
    with pytest.raises(ValueError):
        validate_sandbox_plugins("pi", [{"plugin": package}])


def test_runtime_validation_requires_the_matching_plugin_format():
    with pytest.raises(ValueError, match="marketplace source"):
        validate_sandbox_plugins("codex", [{"plugin": "review@example"}])
    with pytest.raises(ValueError, match="no marketplace"):
        validate_sandbox_plugins(
            "opencode", [{"plugin": "review", "marketplace": "example/plugins"}]
        )


def test_codex_plugin_names_follow_its_native_segment_grammar():
    selection = {"plugin": "review.v2@example.marketplace", "marketplace": "example/plugins"}
    with pytest.raises(ValueError):
        validate_sandbox_plugins("codex", [selection])
    assert validate_sandbox_plugins("claude-code", [selection]) == [selection]


def test_duplicate_plugin_normalization_cannot_hide_repeated_selections():
    with pytest.raises(ValueError, match="more than once"):
        validate_sandbox_plugins("pi", [{"plugin": "npm:review"}, {"plugin": "review"}])


@pytest.mark.parametrize("runtime", ["claude-code", "codex"])
def test_marketplace_identity_cannot_name_conflicting_sources(runtime):
    with pytest.raises(ValueError, match="same source"):
        validate_sandbox_plugins(
            runtime,
            [
                {"plugin": "review@example", "marketplace": "example/plugins"},
                {"plugin": "format@example", "marketplace": "another/plugins"},
            ],
        )


@pytest.mark.parametrize("runtime", ["pi", "opencode", "kilo"])
def test_multiple_versions_of_the_same_npm_plugin_are_rejected(runtime):
    with pytest.raises(ValueError, match="more than once"):
        validate_sandbox_plugins(
            runtime, [{"plugin": "@example/review@1.2.3"}, {"plugin": "@example/review@2.0.0"}]
        )


def test_plugins_are_bounded_and_unknown_runtimes_rejected():
    with pytest.raises(ValueError, match="at most"):
        validate_sandbox_plugins("pi", [{"plugin": f"plugin-{index}"} for index in range(9)])
    with pytest.raises(ValueError, match="not supported"):
        validate_sandbox_plugins("unknown", [{"plugin": "review"}])
    assert validate_sandbox_plugins("unknown", []) == []
