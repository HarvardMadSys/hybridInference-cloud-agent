"""Public display settings needed before a user signs in."""

from fastapi import APIRouter, Response

from cloud_agent.settings import boolean_setting, text_setting

router = APIRouter(prefix="/v1/agent")


@router.get("/site-config")
async def site_config(response: Response) -> dict[str, object]:
    """Return only the allowlisted site identity and display feature switch."""
    response.headers["Cache-Control"] = "no-store"
    return {
        "branding": {
            "appName": text_setting("site.app_name"),
            "orgName": text_setting("site.org_name"),
            "orgUrl": text_setting("site.org_url"),
            "statusUrl": text_setting("site.status_url"),
            "gatewayUrl": text_setting("site.gateway_url"),
        },
        "features": {"rag": boolean_setting("site.feature_rag")},
    }
