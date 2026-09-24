"""Cached JSON responses and the error envelope (spec §3 "Caching", ``docs/api/README.md``)."""

import hashlib

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from evcrawl.api.schemas import ErrorDetail, ErrorResponse

LIST_MAX_AGE = 300  # /v1/events
LONG_MAX_AGE = 900  # an event's detail, categories, cities


def cached(request: Request, payload: BaseModel, *, max_age: int, rows: int) -> Response:
    """``payload`` as JSON with ``Cache-Control`` and a weak ``ETag``; ``304`` when it matches.

    The ETag hashes the body, so it changes exactly when the answer does.
    ``rows`` is what the request log line reports.
    """
    request.state.rows = rows
    body = payload.model_dump_json().encode()
    etag = f'W/"{hashlib.sha256(body).hexdigest()[:20]}"'
    headers = {"Cache-Control": f"public, max-age={max_age}", "ETag": etag}
    if etag in request.headers.get("if-none-match", ""):
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


def error(
    status_code: int,
    code: str,
    message: str,
    field: str | None = None,
    *,
    status: str | None = None,
) -> JSONResponse:
    """The envelope; ``status`` is the event's, with code ``gone`` only."""
    body = ErrorResponse(error=ErrorDetail(code=code, message=message, field=field, status=status))
    return JSONResponse(status_code=status_code, content=body.model_dump(exclude_none=True))
