"""Pokémon companions are stable, distinct, and safe standalone images."""

import base64
import xml.etree.ElementTree as ET

import pytest

from cloud_agent.user_avatar import UserAvatar, generate_user_avatar
from cloud_agent.work_insights import _FOCUS_AREAS

_SVG_NAMESPACE = "{http://www.w3.org/2000/svg}"
_WORK_AREAS = tuple(area for area, _ in _FOCUS_AREAS)
_SPECIES = {"pikachu", "bulbasaur", "charmander", "squirtle", "eevee", "gengar", "jigglypuff"}


def _svg(avatar: UserAvatar) -> str:
    prefix, encoded = avatar.data_url.split(",", 1)
    assert prefix == "data:image/svg+xml;base64"
    return base64.b64decode(encoded, validate=True).decode("utf-8")


def _identity_artwork(avatar: UserAvatar) -> bytes:
    root = ET.fromstring(_svg(avatar))
    creature = root.find(".//*[@id='avatar-creature']")
    assert creature is not None
    assert creature.attrib["data-species"] in _SPECIES
    return ET.tostring(creature)


def _theme_colors(avatar: UserAvatar) -> tuple[str, str, str]:
    root = ET.fromstring(_svg(avatar))
    colors = []
    for layer in ("background", "halo", "platform"):
        element = root.find(f".//*[@id='avatar-{layer}']")
        assert element is not None
        colors.append(element.attrib["fill"])
    return tuple(colors)


def test_avatar_is_stable_across_calls_and_model_serialization():
    first = generate_user_avatar("user-alex")
    assert first == generate_user_avatar("user-alex")
    assert first.style == "illustrated"
    assert UserAvatar.model_validate(first.model_dump()) == first


def test_distinct_accounts_receive_distinct_companions():
    avatars = {generate_user_avatar(f"user-{index}").data_url for index in range(100)}
    assert len(avatars) == 100


@pytest.fixture(params=sorted(_SPECIES))
def companion_account(request):
    for index in range(256):
        user_id = f"companion-{index}"
        root = ET.fromstring(_svg(generate_user_avatar(user_id)))
        creature = root.find(".//*[@id='avatar-creature']")
        assert creature is not None
        if creature.attrib["data-species"] == request.param:
            return user_id
    pytest.fail(f"No account received the supported companion {request.param}")


def test_every_species_keeps_its_artwork_through_all_work_themes(companion_account):
    identity = _identity_artwork(generate_user_avatar(companion_account))
    for area in _WORK_AREAS:
        themed = generate_user_avatar(companion_account, focus_areas=[area])
        assert _identity_artwork(themed) == identity
        assert area not in _svg(themed)


def test_every_species_is_safe_in_the_full_ranked_habitat(companion_account):
    avatar = generate_user_avatar(companion_account, focus_areas=_WORK_AREAS)
    root = ET.fromstring(_svg(avatar))
    ids = [element.attrib["id"] for element in root.iter() if "id" in element.attrib]
    assert len(ids) == len(set(ids))
    for element in root.iter():
        assert element.tag.removeprefix(_SVG_NAMESPACE) in {
            "svg",
            "title",
            "defs",
            "clipPath",
            "g",
            "path",
            "circle",
            "ellipse",
            "rect",
        }
        for name, value in element.attrib.items():
            assert not name.lower().startswith("on")
            assert "href" not in name.lower()
            assert "http:" not in value and "https:" not in value
            if "url(" in value:
                assert value == "url(#portrait-circle)"


@pytest.mark.parametrize("area", _WORK_AREAS)
def test_each_observed_work_area_changes_the_habitat_and_preserves_the_companion(area):
    neutral = generate_user_avatar("user-alex")
    themed = generate_user_avatar("user-alex", focus_areas=[area])

    assert themed != neutral
    assert themed == generate_user_avatar("user-alex", focus_areas=[area])
    assert _identity_artwork(themed) == _identity_artwork(neutral)
    assert area not in _svg(themed)


def test_dominant_area_changes_background_halo_and_platform():
    backend = generate_user_avatar("user-alex", focus_areas=["Backend and APIs"])
    frontend = generate_user_avatar("user-alex", focus_areas=["Frontend and UI"])

    assert all(
        left != right
        for left, right in zip(_theme_colors(backend), _theme_colors(frontend), strict=True)
    )
    portraits = {
        generate_user_avatar("user-alex", focus_areas=[area]).data_url for area in _WORK_AREAS
    }
    assert len(portraits) == len(_WORK_AREAS)


def test_ranked_usage_mix_updates_without_changing_account_identity():
    neutral_identity = _identity_artwork(generate_user_avatar("user-alex"))
    portraits = [
        generate_user_avatar("user-alex", focus_areas=_WORK_AREAS[:size])
        for size in range(1, len(_WORK_AREAS) + 1)
    ]

    assert len({avatar.data_url for avatar in portraits}) == len(_WORK_AREAS)
    assert all(_identity_artwork(avatar) == neutral_identity for avatar in portraits)
    reversed_rank = generate_user_avatar("user-alex", focus_areas=_WORK_AREAS[::-1])
    assert reversed_rank != portraits[-1]
    assert _identity_artwork(reversed_rank) == neutral_identity
    assert portraits[-1] == generate_user_avatar("user-alex", focus_areas=_WORK_AREAS)
    assert portraits[-1] != generate_user_avatar("another-account", focus_areas=_WORK_AREAS)


def test_empty_unknown_and_duplicate_usage_never_change_the_assigned_theme():
    neutral = generate_user_avatar("user-alex")
    untrusted = '</svg><script>alert("private-activity")</script>'

    assert neutral == generate_user_avatar("user-alex", focus_areas=[])
    assert neutral == generate_user_avatar("user-alex", focus_areas=["Unknown area", untrusted])
    backend = generate_user_avatar("user-alex", focus_areas=["Backend and APIs"])
    assert backend == generate_user_avatar(
        "user-alex", focus_areas=[untrusted, "Backend and APIs", "Backend and APIs", "Unknown area"]
    )
    assert "private-activity" not in _svg(backend)


@pytest.mark.parametrize(
    "user_id", ["", "账户-🎨", "user@example.com", "</svg><script>alert(1)</script>"]
)
def test_avatar_is_an_accessible_well_formed_svg(user_id):
    root = ET.fromstring(_svg(generate_user_avatar(user_id)))
    assert root.tag == f"{_SVG_NAMESPACE}svg"
    assert root.attrib["viewBox"] == "0 0 256 256"
    assert root.attrib["role"] == "img"
    title = root.find(f"{_SVG_NAMESPACE}title")
    assert title is not None
    assert title.attrib["id"] == root.attrib["aria-labelledby"]
    assert title.text == "Automatically generated Pokémon companion avatar"
    assert root.find(f"{_SVG_NAMESPACE}g") is not None


@pytest.mark.parametrize("areas", [(), *_WORK_AREAS, _WORK_AREAS])
def test_portrait_never_embeds_account_identity_or_active_content(areas):
    user_id = 'private-identity@example.com"><script>alert("secret")</script>'
    focus_areas = (areas,) if isinstance(areas, str) else areas
    svg = _svg(generate_user_avatar(user_id, focus_areas=focus_areas))
    assert user_id not in svg
    assert "private-identity" not in svg
    assert "secret" not in svg
    allowed_tags = {"svg", "title", "defs", "clipPath", "g", "path", "circle", "ellipse", "rect"}
    for element in ET.fromstring(svg).iter():
        assert element.tag.removeprefix(_SVG_NAMESPACE) in allowed_tags
        for name, value in element.attrib.items():
            assert not name.lower().startswith("on")
            assert "href" not in name.lower()
            assert "http:" not in value and "https:" not in value
            if "url(" in value:
                assert value == "url(#portrait-circle)"
