"""``/healthz``: for uptime checks, not for the PWA (spec §3)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError

from evcrawl.api.db import DatabaseNotConfigured, Reader
from evcrawl.api.routes import get_db
from evcrawl.api.schemas import Health
from evcrawl.config import Settings

router = APIRouter()


@router.get(
    "/healthz",
    response_model=Health,
    responses={503: {"model": Health, "description": "The database does not answer."}},
)
async def healthz(request: Request, db: Annotated[Reader, Depends(get_db)]) -> JSONResponse:
    """The build and whether ``select 1`` as ``api_ro`` answers."""
    settings: Settings = request.app.state.settings
    git_sha = settings.evcrawl_git_sha
    try:
        await db.rows("select 1")
    except (DBAPIError, DatabaseNotConfigured):
        body = Health(ok=False, git_sha=git_sha, db="unavailable")
        return JSONResponse(status_code=503, content=body.model_dump())
    return JSONResponse(content=Health(ok=True, git_sha=git_sha, db="ok").model_dump())
