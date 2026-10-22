"""Combined stream-page behavior for the server API's in-memory storage fake."""

from typing import Any

from cloud_agent.domain.execution import JobStreamState


async def job_stream_page(
    storage: Any, *, job_id: str, user_id: str, after_id: int = 0, limit: int = 500
) -> tuple[list[dict[str, Any]], JobStreamState] | None:
    job = storage.jobs.get(job_id)
    if job is None or job["user_id"] != user_id:
        return None
    events = await storage.list_events_after(job_id=job_id, after_id=after_id, limit=limit)
    return events, JobStreamState(job["state"], job.get("published_pr_url"))
