"""Deterministic Pokémon companions built from local, layered SVG artwork."""

from __future__ import annotations

import base64
import hashlib
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Sequence


class UserAvatar(BaseModel):
    """An illustrated companion displayed directly by an image element."""

    data_url: str
    style: Literal["illustrated"] = "illustrated"


# Work palettes color only the habitat; each Pokémon keeps its familiar colors.
# Values are background, halo, platform, and badge fill, respectively.
_PALETTES = (
    ("#E5EBFA", "#C5D1EE", "#5268A0", "#DAE3F7"),
    ("#E3F0E9", "#BDDACC", "#377968", "#D4EBDD"),
    ("#F9E9DE", "#EACAB1", "#AF684B", "#F3D9C7"),
    ("#EDE7F5", "#D7C9E9", "#7B609A", "#E4DAF0"),
    ("#F4ECD5", "#E7D6A5", "#8D7944", "#EFE2BF"),
    ("#DFF0F3", "#B4D8DF", "#3F7787", "#CEE7EC"),
    ("#F6E3EB", "#E4BDD0", "#A05E7D", "#EED1DE"),
)

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

# Each illustration is painted back to front: silhouette details, body, face,
# then highlights. Geometry and colors are constants, never user-supplied SVG.
_CREATURE_ARTWORK = {
    "pikachu": (
        '<g fill="#FFD34D" stroke="#71542E">'
        '<path d="M164 171L180 152L169 139L199 111L211 125L192 145L204 157L180 185Z"/>'
        '<path fill="#B78444" d="M164 171L177 159L187 172L180 185Z"/>'
        '<ellipse cx="103" cy="203" rx="18" ry="8"/>'
        '<ellipse cx="152" cy="203" rx="18" ry="8"/>'
        '<path d="M102 148C89 157 87 182 95 198Q128 216 162 197C169 179 163 157 152 148Z"/>'
        '<path fill="#FFF0A0" stroke="none" d="M111 155Q128 149 146 157L153 184Q127 197 104 185Z"/>'
        '<path d="M93 161Q76 174 93 187Q105 180 108 170M158 160Q179 168 162 182Q151 177 149 168"/>'
        '<path d="M93 96C83 76 74 42 83 27C98 36 107 67 109 87Z"/>'
        '<path fill="#383437" d="M83 27C93 34 99 49 103 61L87 68Q77 40 83 27Z"/>'
        '<path d="M146 90C148 64 162 33 176 26C187 41 175 74 162 99Z"/>'
        '<path fill="#383437" d="M176 26Q185 40 177 62L159 53Q169 32 176 26Z"/>'
        '<path d="M84 106Q91 82 126 85Q162 82 174 106L176 136Q172 160 128 164'
        'Q86 161 80 138Z"/>'
        '<path fill="#FFF5BD" stroke="none" opacity=".7" d="M92 106Q101 92 122 94Q102 100 97 117Z"/>'
        '<g fill="#F27261" stroke="none"><ellipse cx="91" cy="139" rx="10" ry="9"/>'
        '<ellipse cx="165" cy="139" rx="10" ry="9"/></g>'
        "</g>"
    ),
    "bulbasaur": (
        '<g stroke="#355B48">'
        '<path fill="#5C9B56" d="M91 121Q80 91 100 66L120 73L131 48L146 71L165 62'
        'Q185 98 164 126Z"/>'
        '<path fill="#80B965" d="M108 117Q98 92 100 66Q130 80 130 116Z"/>'
        '<path fill="#72AD5F" d="M130 116Q128 76 131 48Q150 78 151 117Z"/>'
        '<path fill="none" d="M151 117Q166 92 165 62"/>'
        '<g fill="#81C5AC">'
        '<path d="M94 144Q118 131 149 144Q178 158 174 187L159 201H96L80 185Z"/>'
        '<path d="M89 172Q80 184 86 201Q101 207 112 200L111 179M149 178L147 201'
        'Q164 207 176 199L172 177"/>'
        '<path d="M84 115L87 87L108 99Q128 89 149 100L168 87L172 119'
        'Q185 137 171 155Q152 174 127 174Q93 173 79 154Q66 138 84 115Z"/>'
        '</g><g fill="#468D79" stroke="none">'
        '<path d="M110 104L120 99L125 111L113 115ZM86 125L97 119L101 128L89 134Z"/>'
        '<path d="M154 151L163 143L170 151L158 157ZM119 185L128 177L133 188L123 193Z"/>'
        '</g><path fill="#BDE7CF" stroke="none" d="M97 109Q111 102 121 107L105 113Z"/>'
        '<g stroke="#E8F6E6" stroke-width="2"><path d="M91 199L91 194M99 202L99 197'
        'M157 202L157 197M165 201L165 196"/></g>'
        "</g>"
    ),
    "charmander": (
        '<g fill="#F6A24B" stroke="#855238">'
        '<path d="M155 185Q184 193 185 164Q185 150 197 147Q211 177 194 195'
        'Q178 211 152 199Z"/>'
        '<path fill="#F57343" d="M195 155Q180 144 189 128L199 107L203 126L212 117'
        'Q225 140 207 154Z"/>'
        '<path fill="#FFDB68" stroke="none" d="M196 150Q189 142 199 128L201 137'
        'L207 129Q212 144 203 150Z"/>'
        '<ellipse cx="106" cy="204" rx="17" ry="8"/>'
        '<ellipse cx="151" cy="204" rx="17" ry="8"/>'
        '<path d="M107 137Q92 161 98 193Q128 213 159 191Q165 160 150 137Z"/>'
        '<path fill="#FFE1A1" d="M117 150Q109 164 108 190Q130 204 149 189'
        'Q146 163 138 150Z"/>'
        '<path d="M102 155Q80 159 89 177L105 175M153 155Q175 158 166 176L151 174"/>'
        '<path d="M88 99Q91 73 126 73Q163 72 169 102L168 130'
        'Q156 154 128 154Q94 154 86 130Z"/>'
        '<path fill="#FFC779" stroke="none" d="M96 98Q104 81 128 82Q107 85 104 103Z"/>'
        '<path fill="none" stroke-width="2" d="M120 129L120 130M137 129L137 130"/>'
        '<g stroke="#FFF2C6" stroke-width="2"><path d="M97 209L99 205M105 211L107 206'
        'M147 211L149 207M155 209L157 205"/></g></g>'
    ),
    "squirtle": (
        '<g stroke="#466B7D">'
        '<path fill="#81C7DD" d="M157 172Q188 161 193 179Q198 199 179 202'
        'Q158 204 156 188Q156 177 169 177Q181 177 178 188Q174 193 169 188"/>'
        '<path fill="#A97751" stroke="#705948" d="M128 137Q167 128 179 157'
        'Q188 189 157 204L124 194Z"/>'
        '<path fill="none" stroke="#D5AE78" d="M153 141L165 158L163 183L144 197'
        'M165 158L178 160M163 183L177 191"/>'
        '<g fill="#81C7DD"><ellipse cx="105" cy="204" rx="17" ry="9"/>'
        '<ellipse cx="148" cy="204" rx="17" ry="9"/>'
        '<path d="M104 137Q92 157 97 189Q122 211 153 193Q166 172 149 140Z"/>'
        '<path d="M103 153Q84 151 85 166Q84 176 101 177L109 166'
        'M152 153Q172 147 175 160Q178 171 157 176L146 166"/>'
        '</g><path fill="#FFE3A3" stroke="#947D50" d="M113 144Q99 164 107 190'
        'Q126 205 149 189Q160 166 143 144Z"/>'
        '<path fill="none" stroke="#BD9D64" stroke-width="2" d="M105 165Q125 176 154 164'
        'M106 183Q128 194 151 181M128 149V199"/>'
        '<path fill="#81C7DD" d="M84 103Q87 75 125 75Q159 75 170 103'
        'Q182 129 166 148Q126 169 88 147Q76 128 84 103Z"/>'
        '<path fill="#BDEAF0" stroke="none" d="M95 101Q104 84 126 84Q108 89 103 106Z"/>'
        '<g fill="#F1B5A7" stroke="none" opacity=".55"><ellipse cx="96" cy="138" rx="8" ry="4"/>'
        '<ellipse cx="159" cy="138" rx="8" ry="4"/></g></g>'
    ),
    "eevee": (
        '<g stroke="#71513D">'
        '<path fill="#B78559" d="M91 171Q57 173 54 146Q38 123 50 110'
        'Q59 132 76 133Q92 140 104 165Z"/>'
        '<path fill="#F8E6B7" d="M50 110Q58 125 70 131L63 142L54 138L50 147'
        'Q36 125 50 110Z"/>'
        '<g fill="#B78559"><ellipse cx="104" cy="205" rx="16" ry="8"/>'
        '<ellipse cx="151" cy="205" rx="16" ry="8"/>'
        '<path d="M103 145Q90 169 97 198Q128 215 160 198Q167 170 151 146Z"/>'
        '<path d="M97 158Q78 166 92 182L105 170M155 158Q177 166 162 182L149 170"/>'
        '<path d="M91 100Q66 65 76 28Q105 43 113 89ZM145 91Q155 45 184 31'
        'Q191 68 164 104Z"/>'
        '</g><g fill="#5F443A"><path d="M94 88Q77 61 80 40Q101 56 104 88Z"/>'
        '<path d="M154 89Q161 58 180 42Q181 69 162 92Z"/></g>'
        '<path fill="#F8E6B7" d="M91 144L99 157L90 166L103 171L106 184L119 179'
        'L129 190L139 178L154 183L155 170L168 162L155 151L159 142Z"/>'
        '<path fill="#B78559" d="M81 112Q87 85 128 86Q163 84 176 111'
        'L174 133Q165 160 128 161Q91 159 82 136Z"/>'
        '<path fill="#D9AD7A" stroke="none" d="M93 107Q104 93 124 95Q106 99 102 112Z"/>'
        '<path fill="#D9AD7A" stroke="none" d="M104 137Q126 124 151 137'
        'Q149 152 128 155Q109 153 104 137Z"/></g>'
    ),
    "gengar": (
        '<g fill="#8B70B7" stroke="#53416D">'
        '<path d="M86 149L64 152L74 169L61 180L83 187L98 179M165 151L184 143'
        'L181 163L197 171L178 184L161 174"/>'
        '<ellipse cx="98" cy="206" rx="19" ry="9"/>'
        '<ellipse cx="158" cy="206" rx="19" ry="9"/>'
        '<path d="M82 115L77 78L102 89L117 62L127 77L144 67L151 89L178 78'
        'L174 119Q192 157 174 190Q161 210 127 211Q91 210 80 188Q65 155 82 115Z"/>'
        '<path fill="#A58ACC" stroke="none" d="M88 120Q93 98 113 98'
        'Q96 113 95 138Z"/>'
        '<path fill="#73589E" stroke="none" d="M96 190Q127 207 163 184'
        'Q151 207 124 205Q105 203 96 190Z"/>'
        '<path fill="none" stroke-width="2" d="M80 169L86 174M178 164L173 170"/>'
        "</g>"
    ),
    "jigglypuff": (
        '<g fill="#F3B6CD" stroke="#A56A89">'
        '<ellipse cx="89" cy="201" rx="19" ry="9" transform="rotate(-16 89 201)"/>'
        '<ellipse cx="165" cy="201" rx="19" ry="9" transform="rotate(16 165 201)"/>'
        '<path d="M84 106L79 58Q102 56 112 83M145 83Q155 60 179 58L174 108"/>'
        '<path fill="#725265" d="M88 87L88 67Q102 70 105 86Z'
        'M153 87Q159 72 171 68L166 91Z"/>'
        '<path d="M78 126Q67 102 61 115Q49 137 77 152M179 127Q194 113 198 126'
        'Q202 140 178 151"/>'
        '<path d="M72 132Q70 82 123 80Q176 78 185 126Q196 169 171 190'
        'Q130 222 86 189Q65 171 72 132Z"/>'
        '<path fill="#FAD4E1" stroke="none" d="M83 127Q87 99 111 95'
        'Q92 110 91 131Z"/>'
        '<path d="M107 93Q91 69 110 61Q139 52 149 78Q157 98 137 102'
        'Q120 103 122 87Q123 78 132 82Q137 86 131 90"/>'
        '<g fill="#E891B3" stroke="none"><ellipse cx="87" cy="157" rx="9" ry="5"/>'
        '<ellipse cx="168" cy="157" rx="9" ry="5"/></g></g>'
    ),
}

# Face geometry is local artwork too: eye positions, size, iris, and mouth height.
_FACE_LAYOUTS = {
    "pikachu": (107, 150, 122, 7, 10, "#383437", 141),
    "bulbasaur": (103, 153, 136, 9, 11, "#B44952", 154),
    "charmander": (105, 149, 112, 7, 11, "#385D65", 136),
    "squirtle": (104, 151, 119, 8, 12, "#75474F", 141),
    "eevee": (104, 151, 121, 9, 12, "#573F35", 143),
    "jigglypuff": (101, 156, 139, 13, 16, "#43899E", 168),
}
_SPECIES = tuple(_CREATURE_ARTWORK)


def _face(species: str, expression: int, seed: bytes) -> str:
    """Add bright eyes and an account-stable smile, cheer, or wink."""
    if species == "gengar":
        eyes = (
            '<path d="M87 119L114 129L106 139L89 135Z'
            'M169 119L143 129L150 139L167 135Z" fill="#E96B83"/>'
            '<path d="M101 126V135M156 126V135" stroke="#53416D" stroke-width="4"/>'
        )
        if expression == 2:
            eyes = (
                '<path d="M89 127L110 133" fill="none" stroke="#53416D" stroke-width="4"/>'
                '<path d="M169 119L143 129L150 139L167 135Z" fill="#E96B83"/>'
                '<path d="M156 126V135" stroke="#53416D" stroke-width="4"/>'
            )
        grin = (178 if expression == 1 else 172) + seed[6] % 3 - 1
        return (
            eyes + f'<path d="M89 148Q127 167 169 146Q165 {grin + 12} 128 {grin + 13}'
            f'Q93 {grin + 8} 89 148Z" fill="#FFF5DF" stroke="#53416D"/>'
            + f'<path d="M106 157L109 {grin + 9}M125 162L126 {grin + 13}'
            f'M146 158L144 {grin + 10}" fill="none" stroke="#B0A2BE" stroke-width="2"/>'
        )
    left, right, eye_y, rx, ry, iris, mouth_y = _FACE_LAYOUTS[species]
    # Small account traits preserve the silhouette while adding individual charm.
    spacing = seed[4] % 3 - 1
    left, right = left - spacing, right + spacing
    eye_y += seed[5] % 3 - 1
    mouth_y += seed[6] % 3 - 1
    eyes = ""
    for index, x in enumerate((left, right)):
        if expression == 2 and index == 0:
            eyes += (
                f'<path d="M{x - rx} {eye_y}Q{x} {eye_y - 7} {x + rx} {eye_y}"'
                ' fill="none" stroke="#4F4546" stroke-width="3.5"/>'
            )
        else:
            eyes += (
                f'<ellipse cx="{x}" cy="{eye_y}" rx="{rx}" ry="{ry}"'
                f' fill="{iris}" stroke="#4F4546" stroke-width="2"/>'
                f'<ellipse cx="{x + 1}" cy="{eye_y + 3}" rx="{rx * 0.55}"'
                f' ry="{ry * 0.5}" fill="#252F36" stroke="none"/>'
                f'<ellipse cx="{x - 2}" cy="{eye_y - 4}" rx="{rx * 0.35}"'
                f' ry="{ry * 0.33}" fill="#FFF" stroke="none"/>'
                f'<circle cx="{x + 3}" cy="{eye_y + 5}" r="1.5" fill="#FFF" stroke="none"/>'
            )
    nose = ""
    if species in ("pikachu", "eevee"):
        nose = f'<path d="M124 {mouth_y - 8}H132L128 {mouth_y - 4}Z" fill="#4F4546" stroke="none"/>'
    if expression == 1:
        smile = (
            f'<path d="M118 {mouth_y}Q128 {mouth_y + 5} 138 {mouth_y}'
            f"Q137 {mouth_y + 15} 128 {mouth_y + 15}"
            f'Q119 {mouth_y + 15} 118 {mouth_y}Z" fill="#714D55"'
            ' stroke="#5E4649" stroke-width="2"/>'
            f'<path d="M122 {mouth_y + 11}Q128 {mouth_y + 6} 134 {mouth_y + 11}'
            f'Q128 {mouth_y + 17} 122 {mouth_y + 11}Z" fill="#F2A0AC" stroke="none"/>'
        )
    else:
        smile = (
            f'<path d="M118 {mouth_y}Q128 {mouth_y + 10} 138 {mouth_y}"'
            ' fill="none" stroke="#5E4649" stroke-width="2.5"/>'
        )
    return eyes + nose + smile


def _work_badge(area: str, *, x: int, y: int, radius: int, scale: float) -> str:
    """Render a ranked work badge from the fixed allowlist."""
    palette_index, motif = _WORK_THEMES[area]
    _, _, stroke, fill = _PALETTES[palette_index]
    offset = round(-12 * scale, 1)
    return (
        f'<g transform="translate({x} {y})">'
        f'<circle r="{radius}" fill="{fill}" stroke="#FFF" stroke-opacity=".8"'
        ' stroke-width="2"/>'
        f'<g transform="translate({offset} {offset}) scale({scale})"'
        f' fill="none" stroke="{stroke}" stroke-width="2" stroke-linecap="round"'
        f' stroke-linejoin="round">{motif}</g></g>'
    )


def generate_user_avatar(user_id: str, *, focus_areas: Sequence[str] = ()) -> UserAvatar:
    """Build a stable companion inside a habitat reflecting ranked recent work.

    A versioned account hash selects species, expression, and pose independently
    of activity. Work changes only the habitat palette and fixed ranked badges.
    Unknown or repeated labels are ignored; no account data enters the SVG.
    Every silhouette, highlight, and motif is drawn locally without image assets,
    network access, inference grants, or persisted state.
    """
    seed = hashlib.sha256(b"cloud-agent-avatar-v2\0" + user_id.encode("utf-8")).digest()
    species = _SPECIES[seed[0] % len(_SPECIES)]
    expression = seed[1] % 3
    rotation = (-2, 0, 2)[seed[2] % 3]
    sparkle_x, sparkle_y = 186 + seed[7] % 12, 92 + seed[8] % 12
    sparkle_size = 3 + seed[9] % 3
    areas = tuple(dict.fromkeys(area for area in focus_areas if area in _WORK_THEMES))
    palette_index = _WORK_THEMES[areas[0]][0] if areas else seed[3] % len(_PALETTES)
    background, halo, platform, accent = _PALETTES[palette_index]
    badges = ""
    if areas:
        badges = _work_badge(areas[0], x=210, y=60, radius=20, scale=1.1)
        if len(areas) > 1:
            badges += _work_badge(areas[1], x=44, y=163, radius=18, scale=1.0)
        remaining = areas[2:]
        start = 128 - (len(remaining) - 1) * 11
        badges += "".join(
            _work_badge(area, x=start + index * 22, y=235, radius=10, scale=0.6)
            for index, area in enumerate(remaining)
        )
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256"'
        ' width="256" height="256" role="img" aria-labelledby="avatar-title">'
        '<title id="avatar-title">Automatically generated Pokémon companion avatar</title>'
        '<defs><clipPath id="portrait-circle"><circle cx="128" cy="128" r="128"/>'
        '</clipPath></defs><g clip-path="url(#portrait-circle)">'
        f'<path id="avatar-background" fill="{background}" d="M0 0H256V256H0Z"/>'
        f'<circle id="avatar-halo" cx="128" cy="117" r="91" fill="{halo}"/>'
        '<g fill="#FFF" opacity=".6"><path d="M47 80L50 86L57 89L50 92L47 99'
        'L44 92L37 89L44 86Z"/><circle cx="193" cy="193" r="3"/>'
        '<circle cx="63" cy="45" r="3"/></g>'
        f'<ellipse id="avatar-platform" cx="128" cy="218" rx="78" ry="17" fill="{platform}"/>'
        f'<ellipse cx="128" cy="214" rx="69" ry="10" fill="{accent}" opacity=".45"/>'
        '<ellipse cx="128" cy="213" rx="48" ry="6" fill="#302D42" opacity=".12"/>'
        f'<g id="avatar-creature" data-species="{species}"'
        f' transform="rotate({rotation} 128 170)" stroke-width="3"'
        ' stroke-linecap="round" stroke-linejoin="round">'
        f"{_CREATURE_ARTWORK[species]}{_face(species, expression, seed)}"
        f'<path d="M{sparkle_x} {sparkle_y - sparkle_size}'
        f"L{sparkle_x + 1} {sparkle_y - 1}L{sparkle_x + sparkle_size} {sparkle_y}"
        f"L{sparkle_x + 1} {sparkle_y + 1}L{sparkle_x} {sparkle_y + sparkle_size}"
        f"L{sparkle_x - 1} {sparkle_y + 1}L{sparkle_x - sparkle_size} {sparkle_y}"
        f'L{sparkle_x - 1} {sparkle_y - 1}Z" fill="#FFF" stroke="none" opacity=".85"/>'
        "</g>"
        '<g transform="translate(128 220)" stroke="#FFF" stroke-width="1.2">'
        '<circle r="5" fill="#FFF"/><path d="M-5 0A5 5 0 0 1 5 0Z" fill="#EE8A89"/>'
        '<path d="M-5 0H5" stroke="#596073"/><circle r="1.7" fill="#FFF" stroke="#596073"/>'
        f"</g>{badges}</g></svg>"
    )
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return UserAvatar(data_url=f"data:image/svg+xml;base64,{encoded}")
