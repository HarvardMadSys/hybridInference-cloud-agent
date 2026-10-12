"""Account portraits with stable faces and activity-based visual themes."""

from __future__ import annotations

import base64
import hashlib
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Sequence


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

# The keys are the work profile's ranked focus labels. Only these exact values
# affect the image; no account data or arbitrary activity text enters the SVG.
# Motifs use a fixed 24 by 24 canvas and show subjects of work, not achievement.
_WORK_THEMES = {
    "Backend and APIs": (
        0,
        '<path d="M8 5L3 12L8 19M16 5L21 12L16 19M14 4L10 20"/>',
    ),
    "Frontend and UI": (
        6,
        '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M3 9H21M10 9V20"/>',
    ),
    "Testing": (1, '<circle cx="12" cy="12" r="9"/><path d="M7 12L11 16L17 8"/>'),
    "Reliability and debugging": (
        4,
        '<rect x="8" y="8" width="8" height="12" rx="4"/>'
        '<path d="M9 8V6L7 4M15 8V6L17 4M4 10H8M16 10H20'
        'M4 15H8M16 15H20M5 20L8 18M16 18L19 20M12 9V18"/>',
    ),
    "Security": (
        3,
        '<path d="M12 3L20 6V11Q20 17 12 21Q4 17 4 11V6ZM8 11L11 14L16 9"/>',
    ),
    "Data and storage": (
        5,
        '<ellipse cx="12" cy="6" rx="8" ry="3"/>'
        '<path d="M4 6V18C4 22 20 22 20 18V6M4 12C4 16 20 16 20 12"/>',
    ),
    "Deployment and infrastructure": (
        2,
        '<path d="M7 19H18C24 19 24 10 19 9C18 2 8 2 6 9C0 9 1 19 7 19ZM12 16V8M9 11L12 8L15 11"/>',
    ),
}

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


def _work_badge(area: str, *, x: int, y: int, radius: int, scale: float) -> str:
    """Render fixed artwork for an already allowlisted work area."""
    palette_index, motif = _WORK_THEMES[area]
    _, _, stroke, fill = _PALETTES[palette_index]
    offset = round(-12 * scale, 1)
    return (
        f'<g transform="translate({x} {y})">'
        f'<circle r="{radius}" fill="{fill}" stroke="#FFF" stroke-opacity=".55"'
        ' stroke-width="1.5"/>'
        f'<g transform="translate({offset} {offset}) scale({scale})"'
        f' fill="none" stroke="{stroke}" stroke-width="2" stroke-linecap="round"'
        f' stroke-linejoin="round">{motif}</g></g>'
    )


def generate_user_avatar(user_id: str, *, focus_areas: Sequence[str] = ()) -> UserAvatar:
    """Create a stable account face with a theme reflecting ranked recent work.

    The illustration is an assigned avatar, not an inference about the user's
    appearance or ability. Activity changes the palette and fixed work motifs;
    the account ID alone determines facial features, skin, hair, and glasses.
    Unknown work labels are ignored, and no activity retains the neutral theme.
    It needs no network access, inference grant, or persisted asset.
    """
    seed = hashlib.sha256(b"cloud-agent-avatar-v1\0" + user_id.encode("utf-8")).digest()
    areas = tuple(dict.fromkeys(area for area in focus_areas if area in _WORK_THEMES))
    palette_index = _WORK_THEMES[areas[0]][0] if areas else seed[0] % len(_PALETTES)
    background, halo, clothing, accent = _PALETTES[palette_index]
    decoration = (
        f'<circle cx="207" cy="55" r="12" fill="{accent}"/>'
        f'<circle cx="45" cy="172" r="7" fill="{accent}"/>'
    )
    clothing_motifs = ""
    if areas:
        decoration = _work_badge(areas[0], x=207, y=61, radius=21, scale=1.15)
        if len(areas) > 1:
            decoration += _work_badge(areas[1], x=44, y=156, radius=18, scale=1.0)
        # Smaller badges show the remaining mix without covering the face.
        remaining = areas[2:]
        start = 128 - (len(remaining) - 1) * 11
        clothing_motifs = "".join(
            _work_badge(area, x=start + index * 22, y=232, radius=10, scale=0.6)
            for index, area in enumerate(remaining)
        )
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
        f'{decoration}<g id="avatar-hair-back" fill="{hair}">{_HAIR_BACK[hairstyle]}</g>'
        f'<path fill="{clothing}" d="M47 256L53 222Q59 191 106 185H150'
        'Q197 191 203 222L209 256Z"/>'
        f'<path fill="{accent}" d="M96 189Q128 215 160 189L166 196'
        'Q128 224 90 196Z"/>'
        f'{clothing_motifs}<g id="avatar-face">'
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
        f"{glasses}</g></g></svg>"
    )
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return UserAvatar(data_url=f"data:image/svg+xml;base64,{encoded}")
