"""Stable illustrated account portraits without remote image generation."""

from __future__ import annotations

import base64
import hashlib
from typing import Literal

from pydantic import BaseModel


class UserAvatar(BaseModel):
    """An illustrated portrait that can be displayed directly by an image element."""

    data_url: str
    style: Literal["illustrated"] = "illustrated"


# Each palette keeps the portrait, its clothing, and its backdrop coordinated.
_PALETTES = (
    ("#E5EBFA", "#C5D1EE", "#5268A0", "#DAE3F7"),
    ("#E3F0E9", "#BDDACC", "#377968", "#D4EBDD"),
    ("#F9E9DE", "#EACAB1", "#AF684B", "#F3D9C7"),
    ("#EDE7F5", "#D7C9E9", "#7B609A", "#E4DAF0"),
    ("#F4ECD5", "#E7D6A5", "#8D7944", "#EFE2BF"),
    ("#DFF0F3", "#B4D8DF", "#3F7787", "#CEE7EC"),
    ("#F6E3EB", "#E4BDD0", "#A05E7D", "#EED1DE"),
)
_SKIN_COLORS = ("#F4D5B7", "#E9BC98", "#D5A37D", "#B67F59", "#915E43", "#6F4835")
_HAIR_COLORS = ("#2D292B", "#49362F", "#6B4938", "#A16745", "#D3B382", "#6E6873")

# All artwork is fixed SVG geometry. Account data never enters the markup.
_HAIR_BACK = (
    "",
    '<path d="M76 101C73 65 96 44 128 44S183 65 180 101L177 150H79Z"/>',
    "",
    '<path d="M71 112C63 63 96 43 127 43C166 43 189 69 185 114L190 185'
    'Q173 194 158 178L95 178Q77 194 65 183Z"/>',
    "",
    '<circle cx="128" cy="47" r="27"/><path d="M78 110C72 64 95 48 128 48S184 64 178 110Z"/>',
)
_HAIR_FRONT = (
    '<path d="M79 110C70 75 89 48 125 47C164 44 187 69 176 110L164 87'
    'Q141 94 112 71Q98 90 85 91Z"/>',
    '<circle cx="85" cy="80" r="15"/><circle cx="94" cy="62" r="16"/>'
    '<circle cx="115" cy="54" r="17"/><circle cx="139" cy="54" r="17"/>'
    '<circle cx="159" cy="62" r="16"/><circle cx="172" cy="82" r="14"/>'
    '<circle cx="81" cy="102" r="10"/><circle cx="175" cy="103" r="10"/>',
    '<path d="M79 108C72 77 85 49 125 47C161 44 181 67 177 107L166 83Q128 71 91 83Z"/>',
    '<path d="M78 108C69 68 97 44 128 46C171 45 188 75 177 115L165 102'
    'Q168 79 150 73Q123 94 87 91L86 117Z"/>',
    '<path d="M79 105C73 67 96 47 128 47S182 68 177 106L167 86Q130 65 90 86Z"/>',
    '<path d="M78 107C71 73 93 49 128 49S184 73 178 107L166 85Q128 74 90 85Z"/>',
)


def generate_user_avatar(user_id: str) -> UserAvatar:
    """Create a deterministic face portrait using only a hash of the account ID.

    The illustration is an assigned avatar, not an inference about the user's
    appearance. It needs no network access, inference grant, or persisted asset.
    """
    seed = hashlib.sha256(b"cloud-agent-avatar-v1\0" + user_id.encode("utf-8")).digest()
    background, halo, clothing, accent = _PALETTES[seed[0] % len(_PALETTES)]
    skin = _SKIN_COLORS[seed[1] % len(_SKIN_COLORS)]
    hair = _HAIR_COLORS[seed[2] % len(_HAIR_COLORS)]
    hairstyle = seed[3] % len(_HAIR_FRONT)
    eye_y = 112 + seed[4] % 5
    eye_spacing = 18 + seed[5] % 5
    left_eye, right_eye = 128 - eye_spacing, 128 + eye_spacing
    smile_y = 145 + seed[6] % 5
    nose_x = 126 + seed[7] % 5
    glasses = ""
    if seed[8] % 4 == 0:
        glasses = (
            f'<g fill="none" stroke="{hair}" stroke-width="3">'
            f'<rect x="{left_eye - 13}" y="{eye_y - 9}" width="26" height="20" rx="8"/>'
            f'<rect x="{right_eye - 13}" y="{eye_y - 9}" width="26" height="20" rx="8"/>'
            f'<path d="M{left_eye + 13} {eye_y - 2}Q128 {eye_y - 7}'
            f' {right_eye - 13} {eye_y - 2}"/></g>'
        )
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256"'
        ' width="256" height="256" role="img" aria-labelledby="avatar-title">'
        '<title id="avatar-title">Automatically generated illustrated avatar</title>'
        '<defs><clipPath id="portrait-circle"><circle cx="128" cy="128" r="128"/>'
        '</clipPath></defs><g clip-path="url(#portrait-circle)">'
        f'<path fill="{background}" d="M0 0H256V256H0Z"/>'
        f'<circle cx="128" cy="115" r="91" fill="{halo}"/>'
        f'<circle cx="207" cy="55" r="12" fill="{accent}"/>'
        f'<circle cx="45" cy="172" r="7" fill="{accent}"/>'
        f'<g fill="{hair}">{_HAIR_BACK[hairstyle]}</g>'
        f'<path fill="{clothing}" d="M47 256L53 222Q59 191 106 185H150'
        'Q197 191 203 222L209 256Z"/>'
        f'<path fill="{accent}" d="M96 189Q128 215 160 189L166 196'
        'Q128 224 90 196Z"/>'
        f'<path fill="{skin}" d="M111 157H145V186Q128 203 111 186Z"/>'
        '<path fill="#332622" opacity=".12" d="M111 164H145V177Q128 187 111 177Z"/>'
        f'<g fill="{skin}"><ellipse cx="82" cy="119" rx="10" ry="15"/>'
        '<ellipse cx="174" cy="119" rx="10" ry="15"/>'
        '<path d="M82 99C82 70 102 57 128 57S174 70 174 99L170 136'
        'C168 162 149 178 128 178S88 162 86 136Z"/></g>'
        '<path fill="#FFF" opacity=".08" d="M89 98C89 74 106 63 125 63'
        'C106 83 99 121 107 151C95 142 89 121 89 98Z"/>'
        f'<g fill="{hair}">{_HAIR_FRONT[hairstyle]}</g>'
        f'<g fill="none" stroke="{hair}" stroke-width="3" stroke-linecap="round">'
        f'<path d="M{left_eye - 8} {eye_y - 11}Q{left_eye} {eye_y - 15}'
        f' {left_eye + 7} {eye_y - 11}"/>'
        f'<path d="M{right_eye - 7} {eye_y - 11}Q{right_eye} {eye_y - 15}'
        f' {right_eye + 8} {eye_y - 11}"/></g>'
        f'<g fill="#302D32"><ellipse cx="{left_eye}" cy="{eye_y}" rx="3" ry="4"/>'
        f'<ellipse cx="{right_eye}" cy="{eye_y}" rx="3" ry="4"/></g>'
        f'<g fill="#FFF"><circle cx="{left_eye + 1}" cy="{eye_y - 1}" r="1"/>'
        f'<circle cx="{right_eye + 1}" cy="{eye_y - 1}" r="1"/></g>'
        f'<path d="M{nose_x} 122L{nose_x - 3} 134Q{nose_x} 137 {nose_x + 4} 134"'
        ' fill="none" stroke="#533B32" stroke-opacity=".38" stroke-width="2.5"'
        ' stroke-linecap="round" stroke-linejoin="round"/>'
        '<g fill="#C67169" opacity=".18"><ellipse cx="101" cy="137" rx="8" ry="4"/>'
        '<ellipse cx="155" cy="137" rx="8" ry="4"/></g>'
        f'<path d="M117 {smile_y}Q128 {smile_y + 9} 139 {smile_y}" fill="none"'
        ' stroke="#704439" stroke-width="3" stroke-linecap="round"/>'
        f"{glasses}</g></svg>"
    )
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return UserAvatar(data_url=f"data:image/svg+xml;base64,{encoded}")
