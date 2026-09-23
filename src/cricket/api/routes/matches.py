"""Match management routes — list, start, and stop commentary for matches."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from cricket.infra.factory import ProviderFactory

router = APIRouter()

# Track active pipelines (in production, this would be in Redis)
_active_pipelines: dict[str, object] = {}


class StartMatchRequest(BaseModel):
    """Request body for starting commentary on a match."""

    match_id: str
    poll_interval: int = 30  # seconds


class MatchResponse(BaseModel):
    """Response for match operations."""

    match_id: str
    status: str
    message: str


@router.get("/matches/live")
async def list_live_matches() -> dict:
    """List all currently live matches from the data feed.

    Use this to discover match IDs, then start commentary
    on a specific match via POST /matches/start.
    """
    factory = ProviderFactory()
    data_feed = factory.create_data_feed()

    try:
        matches = await data_feed.get_live_matches()
        return {
            "count": len(matches),
            "matches": [
                {
                    "match_id": m.match_id,
                    "title": m.title,
                    "format": m.format.value,
                    "venue": m.venue,
                    "teams": {
                        "home": m.team_home.name,
                        "away": m.team_away.name,
                    },
                }
                for m in matches
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data feed error: {e}") from e


@router.post("/matches/start", response_model=MatchResponse)
async def start_match_commentary(
    request: StartMatchRequest,
    background_tasks: BackgroundTasks,
) -> MatchResponse:
    """Start the commentary pipeline for a specific match.

    The pipeline runs as a background task, polling for new balls
    and generating commentary + audio in near-real-time.
    """
    if request.match_id in _active_pipelines:
        return MatchResponse(
            match_id=request.match_id,
            status="already_running",
            message="Commentary pipeline is already running for this match.",
        )

    factory = ProviderFactory()
    pipeline = factory.create_pipeline()

    _active_pipelines[request.match_id] = pipeline

    # Start the pipeline in the background
    background_tasks.add_task(
        _run_pipeline,
        pipeline,
        request.match_id,
        request.poll_interval,
    )

    return MatchResponse(
        match_id=request.match_id,
        status="started",
        message=f"Commentary pipeline started with {request.poll_interval}s polling interval.",
    )


@router.post("/matches/{match_id}/stop", response_model=MatchResponse)
async def stop_match_commentary(match_id: str) -> MatchResponse:
    """Stop the commentary pipeline for a specific match."""
    pipeline = _active_pipelines.get(match_id)

    if pipeline is None:
        raise HTTPException(
            status_code=404,
            detail=f"No active pipeline found for match {match_id}",
        )

    await pipeline.stop()  # type: ignore[union-attr]
    del _active_pipelines[match_id]

    return MatchResponse(
        match_id=match_id,
        status="stopped",
        message="Commentary pipeline stopped.",
    )


@router.get("/matches/active")
async def list_active_pipelines() -> dict:
    """List all currently active commentary pipelines."""
    return {
        "count": len(_active_pipelines),
        "matches": list(_active_pipelines.keys()),
    }


async def _run_pipeline(pipeline: object, match_id: str, poll_interval: int) -> None:
    """Run the pipeline in the background.

    This wrapper handles cleanup when the pipeline finishes or crashes.
    """
    try:
        await pipeline.start(match_id=match_id, poll_interval=poll_interval)  # type: ignore[union-attr]
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Pipeline crashed for match %s", match_id)
    finally:
        _active_pipelines.pop(match_id, None)
