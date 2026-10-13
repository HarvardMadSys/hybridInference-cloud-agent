"""Profile uploads accept real raster files and retain only bounded image pixels."""

from __future__ import annotations

import base64
import io
import struct
import zlib

import pytest
from PIL import Image, PngImagePlugin
from pydantic import ValidationError

from cloud_agent.user_profile import MAX_PROFILE_IMAGE_BYTES, UpdateProfile, sanitize_profile_image


def picture(format="PNG", size=(40, 20)):
    output = io.BytesIO()
    options = {}
    if format == "PNG":
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("Comment", "Private image metadata")
        options["pnginfo"] = metadata
    Image.new("RGB", size, "blue").save(output, format=format, **options)
    return output.getvalue()


def data_url(raw, mime="image/png"):
    return f"data:{mime};base64," + base64.b64encode(raw).decode()


@pytest.mark.parametrize(
    "format,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")]
)
def test_uploaded_images_are_resized_and_metadata_is_removed(format, mime):
    result = sanitize_profile_image(data_url(picture(format, (1000, 500)), mime))
    assert result.startswith("data:image/png;base64,")
    with Image.open(io.BytesIO(base64.b64decode(result.split(",", 1)[1]))) as image:
        assert image.size == (512, 256)
        assert image.format == "PNG"
        assert image.info == {}


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com/avatar.png",
        "data:image/svg+xml;base64,PHN2Zy8+",
        "data:image/png;base64,%%%",
        "data:image/png;base64,",
        "data:image/png;base64,AAAA",
        data_url(picture(), "image/jpeg"),
        data_url(picture("GIF")),
    ],
)
def test_invalid_or_mislabeled_uploads_are_refused(value):
    with pytest.raises(ValueError):
        sanitize_profile_image(value)


def test_rejects_oversized_files_and_pixel_bombs_before_decoding():
    with pytest.raises(ValueError):
        sanitize_profile_image(data_url(b"x" * (MAX_PROFILE_IMAGE_BYTES + 1)))
    raw = picture()
    ihdr = struct.pack(">II", 4001, 4001) + raw[24:29]
    oversized = raw[:16] + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr)) + raw[33:]
    with pytest.raises(ValueError, match="16 million"):
        sanitize_profile_image(data_url(oversized))


def test_names_preserve_unicode_and_email_cannot_be_changed():
    profile = UpdateProfile(first_name="  沐昕  ", last_name="田", expected_revision=0)
    assert profile.first_name == "沐昕"
    assert "image_data_url" not in profile.model_fields_set
    for update in [
        {"first_name": "x\x00y"},
        {"last_name": "a" * 101},
        {"email": "other@example.com"},
        {"expected_revision": True},
    ]:
        with pytest.raises(ValidationError):
            UpdateProfile.model_validate(
                {"first_name": "", "last_name": "", "expected_revision": 0, **update}
            )
