"""Owner-authored display names and sanitized profile pictures."""

from __future__ import annotations

import base64
import binascii
import io
import unicodedata
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_PROFILE_IMAGE_BYTES = 2 * 1024 * 1024
MAX_PROFILE_IMAGE_DATA_URL = 4 * ((MAX_PROFILE_IMAGE_BYTES + 2) // 3) + 32
MAX_PROFILE_REQUEST_BYTES = MAX_PROFILE_IMAGE_DATA_URL + 4096
MAX_PROFILE_IMAGE_PIXELS = 16_000_000
_IMAGE_FORMATS = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}


class ProfileConflict(ValueError):
    """The profile changed after the owner opened its editor."""


class ProfileView(BaseModel):
    """An owner's saved display fields and their current revision."""

    first_name: str = ""
    last_name: str = ""
    image_data_url: str | None = None
    revision: int = 0


class ProfileResponse(ProfileView):
    """Combine Agent-owned display fields with a read-only account email.

    ``companion_data_url`` is the account's fixed Pokémon companion, shown
    wherever no picture was uploaded. It depends on the account alone, unlike
    the home page's, whose habitat follows recent work.
    """

    email: str
    companion_data_url: str


class UpdateProfile(BaseModel):
    """Save names and optionally replace or remove the profile picture."""

    model_config = ConfigDict(extra="forbid", strict=True)

    first_name: str = Field(max_length=100)
    last_name: str = Field(max_length=100)
    expected_revision: int = Field(ge=0)
    # Omitted means keep the saved image; null explicitly removes it.
    image_data_url: str | None = Field(default=None, max_length=MAX_PROFILE_IMAGE_DATA_URL)

    @field_validator("first_name", "last_name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        """Preserve international names while rejecting control characters."""
        if any(unicodedata.category(char) in {"Cc", "Cs"} for char in value):
            raise ValueError("Names must not contain control characters")
        return value.strip()


def sanitize_profile_image(data_url: str) -> str:
    """Decode an allowed image, bound its pixels, and discard file metadata.

    Stored images are single-frame PNGs, at most 512 by 512. Arbitrary SVG,
    declared MIME types that disagree with the image, and oversized files are
    refused before storage. Re-encoding removes metadata and trailing payloads.
    """
    prefix, separator, encoded = data_url.partition(",")
    media_type = prefix.removeprefix("data:").removesuffix(";base64")
    if (
        not separator
        or prefix != f"data:{media_type};base64"
        or media_type not in _IMAGE_FORMATS
        or len(data_url) > MAX_PROFILE_IMAGE_DATA_URL
    ):
        raise ValueError("Choose a PNG, JPEG, or WebP image up to 2 MB.")
    try:
        raw = base64.b64decode(encoded, validate=True)
        if not raw or len(raw) > MAX_PROFILE_IMAGE_BYTES:
            raise ValueError("Choose an image up to 2 MB.")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                if source.format != _IMAGE_FORMATS[media_type]:
                    raise ValueError("The image format does not match its file type.")
                if source.width * source.height > MAX_PROFILE_IMAGE_PIXELS:
                    raise ValueError("Choose an image with at most 16 million pixels.")
                source.load()
                picture = ImageOps.exif_transpose(source).convert("RGBA")
                picture.thumbnail((512, 512), Image.Resampling.LANCZOS)
                # A fresh image carries no EXIF, comments, or other input metadata.
                clean = Image.new("RGBA", picture.size)
                clean.paste(picture)
                output = io.BytesIO()
                clean.save(output, format="PNG")
        return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")
    except (
        binascii.Error,
        OSError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ValueError("Choose a valid PNG, JPEG, or WebP image up to 2 MB.") from exc
