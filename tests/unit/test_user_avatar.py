"""Account avatars are stable, distinct, and safe standalone images."""

import base64
import xml.etree.ElementTree as ET

import pytest

from cloud_agent.user_avatar import UserAvatar, generate_user_avatar

_SVG_NAMESPACE = "{http://www.w3.org/2000/svg}"


def _svg(avatar: UserAvatar) -> str:
    prefix, encoded = avatar.data_url.split(",", 1)
    assert prefix == "data:image/svg+xml;base64"
    return base64.b64decode(encoded, validate=True).decode("utf-8")


def test_avatar_is_stable_across_calls_and_model_serialization():
    first = generate_user_avatar("user-alex")
    assert first == generate_user_avatar("user-alex")
    assert first.style == "illustrated"
    assert UserAvatar.model_validate(first.model_dump()) == first


def test_distinct_accounts_receive_distinct_portraits():
    avatars = {generate_user_avatar(f"user-{index}").data_url for index in range(100)}
    assert len(avatars) == 100


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
    assert title.text == "Automatically generated illustrated avatar"
    assert root.find(f"{_SVG_NAMESPACE}g") is not None


def test_portrait_never_embeds_account_identity_or_active_content():
    user_id = 'private-identity@example.com"><script>alert("secret")</script>'
    svg = _svg(generate_user_avatar(user_id))
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
