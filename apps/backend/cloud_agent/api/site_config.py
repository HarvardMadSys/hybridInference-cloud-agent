"""Public display settings needed before a user signs in."""

from fastapi import APIRouter, Response

from cloud_agent.deployment.site_content import site_content
from cloud_agent.settings import text_setting

router = APIRouter(prefix="/v1/agent")


@router.get("/site-config")
async def site_config(response: Response) -> dict[str, object]:
    """Return only the allowlisted site identity and the deployment's public site content."""
    response.headers["Cache-Control"] = "no-store"
    content = site_content()
    return {
        "branding": {
            "appName": text_setting("site.app_name"),
            "orgName": text_setting("site.org_name"),
            "orgUrl": text_setting("site.org_url"),
            "statusUrl": text_setting("site.status_url"),
            "docsUrl": text_setting("site.docs_url"),
            "contactUrl": text_setting("site.contact_url"),
        },
        # From the deployment's site files, not Admin: see `site_content`.
        "content": {
            "sourceUrl": content.source_url,
            "team": [member.public() for member in content.team],
        },
    }
