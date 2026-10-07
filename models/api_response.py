"""Standardized API response schemas.

Every endpoint MUST return responses conforming to these models.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any, Generic, TypeVar

from fastapi.responses import JSONResponse
from pydantic import BaseModel

from infrastructure.request_context import current_request_id

T = TypeVar("T")


class ErrorDetail(BaseModel):
    code: str = "INTERNAL_ERROR"
    field: str | None = None
    detail: str = ""


class APIResponse(BaseModel):
    success: bool
    data: Any = None
    message: str = ""
    errors: list[ErrorDetail] | None = None
    request_id: str = ""
    timestamp: str = ""


def success_response(
    data: Any = None,
    message: str = "OK",
    request_id: str | None = None,
    status_code: int = 200,
) -> JSONResponse:
    rid = request_id or current_request_id()
    return JSONResponse(
        status_code=status_code,
        content={
            "success": True,
            "data": data,
            "message": message,
            "errors": None,
            "request_id": rid,
            "timestamp": _now(),
        },
    )


def error_response(
    message: str = "An error occurred",
    errors: list[ErrorDetail | dict] | None = None,
    request_id: str | None = None,
    status_code: int = 500,
    error_code: str | None = None,
) -> JSONResponse:
    rid = request_id or current_request_id()
    if not errors and error_code:
        errors = [ErrorDetail(code=error_code, detail=message)]
    serialized_errors = None
    if errors:
        serialized_errors = []
        for e in errors:
            if isinstance(e, ErrorDetail):
                serialized_errors.append(e.model_dump())
            elif isinstance(e, dict):
                serialized_errors.append(e)
    return JSONResponse(
        status_code=status_code,
        content={
            "success": False,
            "data": None,
            "message": message,
            "errors": serialized_errors,
            "request_id": rid,
            "timestamp": _now(),
        },
    )


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


def http_status_for(error_code: str) -> int:
    mapping = {
        "VALIDATION_ERROR": 400,
        "NOT_FOUND": 404,
        "AUTHENTICATION_ERROR": 401,
        "AUTHORIZATION_ERROR": 403,
        "RATE_LIMIT_ERROR": 429,
        "CONFLICT": 409,
        "BUSINESS_ERROR": 422,
        "TIMEOUT": 504,
        "EXTERNAL_SERVICE_ERROR": 502,
        "LLM_PROVIDER_ERROR": 502,
        "DATABASE_ERROR": 503,
        "YOUTUBE_API_ERROR": 502,
        "YOUTUBE_QUOTA_EXCEEDED": 429,
    }
    return mapping.get(error_code, 500)
