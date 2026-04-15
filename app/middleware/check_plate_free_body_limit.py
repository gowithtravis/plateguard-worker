"""
Reject oversized JSON bodies for ``POST /api/check-plate-free`` (abuse / oversized payloads).
"""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# Maximum JSON body size for the free plate-check endpoint (bytes).
CHECK_PLATE_FREE_MAX_BODY_BYTES = 1024


async def check_plate_free_body_size_middleware(request: Request, call_next) -> Response:
    if request.method != "POST":
        return await call_next(request)
    path = request.url.path.rstrip("/") or "/"
    if path != "/api/check-plate-free":
        return await call_next(request)

    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > CHECK_PLATE_FREE_MAX_BODY_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "Request body exceeds the maximum allowed size (1KB)."},
                )
        except ValueError:
            return JSONResponse(
                status_code=413,
                content={"detail": "Invalid Content-Length header."},
            )

    return await call_next(request)
