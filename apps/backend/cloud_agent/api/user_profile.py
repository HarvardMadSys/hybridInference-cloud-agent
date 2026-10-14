"""Owner-only display profile editing and read-only Gateway email."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool

from cloud_agent.api._body_limits import payload_too_large, read_bounded_body
from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_gateway_client, get_storage, require_store
from cloud_agent.gateway import GatewayClient, GatewayError
from cloud_agent.user_avatar import generate_user_avatar
from cloud_agent.user_profile import (
    MAX_PROFILE_REQUEST_BYTES,
    ProfileConflict,
    ProfileResponse,
    ProfileView,
    UpdateProfile,
    sanitize_profile_image,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from cloud_agent.ports.user_profile import UserProfileRepository


class _ProfileRoute(APIRoute):
    """Bound uploads before parsing and keep submitted personal data out of errors."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """Apply a streamed ceiling even when Content-Length is absent or incorrect."""
        handler = super().get_route_handler()

        async def guarded(request: Request) -> Response:
            if request.method == "PATCH":
                body = await read_bounded_body(request, MAX_PROFILE_REQUEST_BYTES)
                if body is None:
                    return payload_too_large(MAX_PROFILE_REQUEST_BYTES, "A profile request")
                request._body = body
            try:
                return await handler(request)
            except RequestValidationError:
                return JSONResponse(
                    status_code=422,
                    content={"detail": "Check the profile fields and image size."},
                    headers={"Cache-Control": "no-store"},
                )

        return guarded


router = APIRouter(prefix="/v1/agent/profile", tags=["profile"], route_class=_ProfileRoute)


class _ProfileStorage(Protocol):
    """The profile surface reaches only its owner's display-profile repository."""

    user_profile: UserProfileRepository


def _response(saved: ProfileView, *, user_id: str, email: str) -> ProfileResponse:
    """Answer with the saved fields, the email and the account's fixed companion."""
    return ProfileResponse(
        **saved.model_dump(),
        email=email,
        companion_data_url=generate_user_avatar(user_id).data_url,
    )


async def _email(gateway: GatewayClient, user_id: str) -> str:
    try:
        return await gateway.user_email(user_id)
    except GatewayError:
        raise HTTPException(
            503, "Account email is temporarily unavailable. Please try again."
        ) from None


@router.get("", response_model=ProfileResponse)
async def read_profile(
    response: Response,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _ProfileStorage | None = Depends(get_storage),
    gateway: GatewayClient = Depends(get_gateway_client),
) -> ProfileResponse:
    """Read the signed-in owner's display profile and current account email."""
    response.headers["Cache-Control"] = "no-store"
    saved = await require_store(store).user_profile.get_user_profile(user_id=user["user_id"])
    email = await _email(gateway, user["user_id"])
    return _response(saved, user_id=user["user_id"], email=email)


@router.patch("", response_model=ProfileResponse)
async def save_profile(
    body: UpdateProfile,
    response: Response,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _ProfileStorage | None = Depends(get_storage),
    gateway: GatewayClient = Depends(get_gateway_client),
) -> ProfileResponse:
    """Persist names and a sanitized picture at the profile revision the owner read."""
    response.headers["Cache-Control"] = "no-store"
    image = body.image_data_url
    if image is not None:
        try:
            image = await run_in_threadpool(sanitize_profile_image, image)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
    # Resolve the read-only field before writing, so an upstream failure cannot
    # report a failed save after having committed the owner's draft.
    email = await _email(gateway, user["user_id"])
    try:
        saved = await require_store(store).user_profile.save_user_profile(
            user_id=user["user_id"],
            first_name=body.first_name,
            last_name=body.last_name,
            expected_revision=body.expected_revision,
            replace_image="image_data_url" in body.model_fields_set,
            image_data_url=image,
        )
    except ProfileConflict:
        raise HTTPException(409, "Your profile changed. Reload it before saving again.") from None
    return _response(saved, user_id=user["user_id"], email=email)
