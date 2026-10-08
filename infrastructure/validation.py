"""Request validation utilities and middleware."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from models.api_response import error_response

logger = logging.getLogger("infrastructure.validation")

# API bodies are small JSON documents (a URL, a channel handle, a few options).
MAX_BODY_SIZE = 64 * 1024
MAX_URL_LENGTH = 2048

YOUTUBE_URL_PATTERN = re.compile(
    r"^https?://(?:www\.)?(?:youtube\.com|youtu\.be)/",
    re.IGNORECASE,
)
VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")


class RequestValidationMiddleware(BaseHTTPMiddleware):
    """Middleware for request validation (size, content-type, sanitization)."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        rid = request.headers.get("X-Request-ID", "")

        if request.url.path.startswith("/api/"):
            if len(str(request.url)) > MAX_URL_LENGTH:
                return error_response(
                    message="URL too long",
                    status_code=414,
                    request_id=rid,
                )

            if request.method in ("POST", "PUT", "PATCH"):
                raw_length = request.headers.get("content-length")
                if raw_length is None and "transfer-encoding" in request.headers:
                    # A chunked body has no declared size, so the cap below could not apply.
                    return error_response(
                        message="Content-Length required",
                        status_code=411,
                        request_id=rid,
                    )
                try:
                    content_length = int(raw_length) if raw_length else 0
                except ValueError:
                    content_length = -1
                if content_length < 0:
                    return error_response(
                        message="Invalid Content-Length header",
                        status_code=400,
                        request_id=rid,
                    )
                if content_length > MAX_BODY_SIZE:
                    return error_response(
                        message="Request body too large",
                        status_code=413,
                        request_id=rid,
                    )

                # Body-less action POSTs (cancel/resume/logout) need no Content-Type
                has_body = content_length > 0 or "transfer-encoding" in request.headers
                content_type = request.headers.get("content-type", "")
                if has_body and "application/json" not in content_type:
                    return error_response(
                        message="Content-Type must be application/json",
                        status_code=415,
                        request_id=rid,
                    )

        response = await call_next(request)
        return response


def sanitize_input(value: str, max_length: int = 1000) -> str:
    """Sanitize and truncate user input."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)
    return value[:max_length]


def validate_video_id(video_id: str) -> bool:
    """Validate YouTube video ID format."""
    return bool(VIDEO_ID_PATTERN.match(video_id))


def validate_youtube_url(url: str) -> bool:
    """Validate that a URL is a YouTube URL."""
    return bool(YOUTUBE_URL_PATTERN.match(url.strip()))


def safe_json_loads(body: bytes) -> dict[str, Any] | None:
    """Safely parse JSON bytes, returning None on failure."""
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
