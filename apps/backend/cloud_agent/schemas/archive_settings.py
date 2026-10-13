"""Revisioned sandbox archive policies for administrators and host brokers."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic resolves annotations at runtime.
from typing import TYPE_CHECKING, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    model_serializer,
    model_validator,
)

from cloud_agent_shared.sandbox_archive_settings import (
    DEFAULT_MAX_ARCHIVE_BYTES,
    DEFAULT_MULTIPART_PART_BYTES,
    DEFAULT_MULTIPART_THRESHOLD_BYTES,
    MAX_ARCHIVE_BYTES,
    MAX_MULTIPART_PART_BYTES,
    MAX_SINGLE_UPLOAD_BYTES,
    MIN_MULTIPART_PART_BYTES,
    SANDBOX_ARCHIVE_S3_URI,
    SandboxArchivePolicy,
    SandboxArchivePolicyError,
)

if TYPE_CHECKING:
    from pydantic import GetJsonSchemaHandler
    from pydantic.json_schema import JsonSchemaValue
    from pydantic_core import CoreSchema


class SandboxArchiveSettingsResponse(BaseModel):
    """The revisioned archive policy stored for every sandbox host.

    The secret access key is write-only. ``aws_secret_access_key_configured``
    tells the editor whether a blank secret keeps the stored one.
    """

    enabled: bool = False
    s3_uri: Literal["s3://freeinference/cloud-agent/sandbox"] = SANDBOX_ARCHIVE_S3_URI
    region: str = ""
    kms_key_arn: str = ""
    expected_bucket_owner: str = ""
    aws_access_key_id: str = ""
    aws_secret_access_key_configured: bool = False
    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES
    multipart_threshold_bytes: int = DEFAULT_MULTIPART_THRESHOLD_BYTES
    multipart_part_bytes: int = DEFAULT_MULTIPART_PART_BYTES
    revision: int = Field(0, ge=0)
    updated_at: datetime | None = None


class HostSandboxArchiveSettings(SandboxArchiveSettingsResponse):
    """Heartbeat copy. Includes the secret and omits empty credential fields.

    Older sandbox managers reject unknown policy keys. An unset access key must not
    appear on the wire; a set key is delivered only to a sandbox manager that can
    apply it. The admin configured flag is not part of this payload.
    """

    aws_secret_access_key: str = ""

    @model_serializer(mode="wrap")
    def _omit_empty_credentials(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        if not data.get("aws_access_key_id"):
            data.pop("aws_access_key_id", None)
        if not data.get("aws_secret_access_key"):
            data.pop("aws_secret_access_key", None)
        data.pop("aws_secret_access_key_configured", None)
        return data

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        del core_schema, handler
        schema = SandboxArchiveSettingsResponse.model_json_schema()
        properties = dict(schema["properties"])
        properties.pop("aws_secret_access_key_configured", None)
        properties["aws_secret_access_key"] = {
            "default": "",
            "description": "Included only when an access key is configured.",
            "title": "Aws Secret Access Key",
            "type": "string",
        }
        return {
            **schema,
            "title": "HostSandboxArchiveSettings",
            "description": "Archive policy delivered to a sandbox manager. Empty access-key fields are omitted.",
            "properties": properties,
        }


class SetSandboxArchiveSettingsRequest(BaseModel):
    """Replace the whole archive policy if the displayed revision is current."""

    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool
    region: str = Field("", max_length=128)
    kms_key_arn: str = Field("", max_length=2048)
    expected_bucket_owner: str = Field("", max_length=12)
    aws_access_key_id: str = Field("", max_length=128)
    aws_secret_access_key: str = Field("", max_length=256)
    max_archive_bytes: StrictInt = Field(DEFAULT_MAX_ARCHIVE_BYTES, ge=1, le=MAX_ARCHIVE_BYTES)
    multipart_threshold_bytes: StrictInt = Field(
        DEFAULT_MULTIPART_THRESHOLD_BYTES, ge=1, le=MAX_SINGLE_UPLOAD_BYTES
    )
    multipart_part_bytes: StrictInt = Field(
        DEFAULT_MULTIPART_PART_BYTES,
        ge=MIN_MULTIPART_PART_BYTES,
        le=MAX_MULTIPART_PART_BYTES,
    )
    revision: StrictInt = Field(..., ge=0, description="Revision returned by the last GET.")

    @model_validator(mode="before")
    @classmethod
    def _default_optional_limits(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        values = dict(data)
        for key, default in (
            ("max_archive_bytes", DEFAULT_MAX_ARCHIVE_BYTES),
            ("multipart_threshold_bytes", DEFAULT_MULTIPART_THRESHOLD_BYTES),
            ("multipart_part_bytes", DEFAULT_MULTIPART_PART_BYTES),
        ):
            if values.get(key) is None:
                values[key] = default
        return values

    @model_validator(mode="after")
    def _safe_archive_policy(self) -> SetSandboxArchiveSettingsRequest:
        # A blank secret can mean "keep the stored secret" when the access key
        # id is unchanged. The route merges that secret and validates again.
        if self.aws_access_key_id and not self.aws_secret_access_key:
            return self
        try:
            SandboxArchivePolicy(
                enabled=self.enabled,
                region=self.region,
                kms_key_arn=self.kms_key_arn,
                expected_bucket_owner=self.expected_bucket_owner,
                aws_access_key_id=self.aws_access_key_id,
                aws_secret_access_key=self.aws_secret_access_key,
                max_archive_bytes=self.max_archive_bytes,
                multipart_threshold_bytes=self.multipart_threshold_bytes,
                multipart_part_bytes=self.multipart_part_bytes,
                revision=self.revision,
            )
        except SandboxArchivePolicyError as exc:
            raise ValueError(str(exc)) from exc
        return self
