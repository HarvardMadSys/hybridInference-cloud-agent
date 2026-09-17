"""Connection aliases reserve explicit choices and never collide."""

import pytest

from cloud_agent.mcp.binding_aliases import binding_aliases


def test_automatic_aliases_leave_later_explicit_names_untouched():
    assert binding_aliases([(1, None, "github"), (2, "github", "github")]) == ["github-1", "github"]


def test_collisions_with_generated_suffixes_are_also_avoided():
    assert binding_aliases([(1, "github", ""), (2, None, "github"), (3, "github-2", "")]) == [
        "github",
        "github-2-2",
        "github-2",
    ]


def test_long_aliases_leave_room_for_the_connection_suffix():
    result = binding_aliases([(1, "a" * 40, ""), (2, None, "a" * 40)])
    assert result == ["a" * 40, "a" * 38 + "-2"]


@pytest.mark.parametrize("preferred", ["invalid/name", "bad alias", "a" * 41, ""])
def test_invalid_defaults_use_the_connection_id(preferred):
    assert binding_aliases([(7, None, preferred)]) == ["mcp7"]


@pytest.mark.parametrize(
    "entries",
    [
        [(1, "github", ""), (2, " github ", "")],
        [(1, "first", ""), (1, "second", "")],
        [(1, "invalid/name", "")],
    ],
)
def test_invalid_explicit_identities_are_refused(entries):
    with pytest.raises(ValueError):
        binding_aliases(entries)
