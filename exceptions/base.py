"""Centralized exception hierarchy for the entire application.

Every exception carries:
- error_code: machine-readable identifier
- message: human-readable description
- context: dict of additional metadata
- request_id: correlation id
- recoverable: whether retry may succeed
- log_level: severity for logging
"""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Any


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class ApplicationException(Exception):
    """Base exception for all application errors."""

    error_code: str = "INTERNAL_ERROR"
    message: str = "An unexpected error occurred"
    context: dict[str, Any] = {}
    request_id: str = ""
    recoverable: bool = False
    log_level: LogLevel = LogLevel.ERROR
    status_code: int = 500

    def __init__(
        self,
        message: str | None = None,
        context: dict[str, Any] | None = None,
        request_id: str | None = None,
        error_code: str | None = None,
        recoverable: bool | None = None,
        log_level: LogLevel | None = None,
        status_code: int | None = None,
    ) -> None:
        if message:
            self.message = message
        if context:
            self.context = context
        if request_id:
            self.request_id = request_id
        if error_code:
            self.error_code = error_code
        if recoverable is not None:
            self.recoverable = recoverable
        if log_level:
            self.log_level = log_level
        if status_code is not None:
            self.status_code = status_code
        if not self.request_id:
            self.request_id = uuid.uuid4().hex[:8]
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_code": self.error_code,
            "message": self.message,
            "context": self.context,
            "request_id": self.request_id,
            "recoverable": self.recoverable,
            "log_level": self.log_level.value,
            "status_code": self.status_code,
        }


class ValidationException(ApplicationException):
    error_code: str = "VALIDATION_ERROR"
    message: str = "Request validation failed"
    status_code: int = 400
    log_level: LogLevel = LogLevel.WARNING


class BusinessException(ApplicationException):
    error_code: str = "BUSINESS_ERROR"
    message: str = "Operation could not be completed"
    status_code: int = 422
    recoverable: bool = False


class ExternalServiceException(ApplicationException):
    error_code: str = "EXTERNAL_SERVICE_ERROR"
    message: str = "External service failed"
    recoverable: bool = True
    status_code: int = 502


class DatabaseException(ApplicationException):
    error_code: str = "DATABASE_ERROR"
    message: str = "Database operation failed"
    recoverable: bool = True


class AuthenticationException(ApplicationException):
    error_code: str = "AUTHENTICATION_ERROR"
    message: str = "Authentication failed"
    status_code: int = 401


class AuthorizationException(ApplicationException):
    error_code: str = "AUTHORIZATION_ERROR"
    message: str = "Not authorized"
    status_code: int = 403


class PipelineException(ApplicationException):
    error_code: str = "PIPELINE_ERROR"
    message: str = "Pipeline execution failed"
    recoverable: bool = True


class ExportException(ApplicationException):
    error_code: str = "EXPORT_ERROR"
    message: str = "Export operation failed"
    recoverable: bool = True


class ConfigurationException(ApplicationException):
    error_code: str = "CONFIGURATION_ERROR"
    message: str = "Application configuration is invalid"
    status_code: int = 500


class RateLimitException(ApplicationException):
    error_code: str = "RATE_LIMIT_ERROR"
    message: str = "Rate limit exceeded"
    status_code: int = 429
    recoverable: bool = True
    log_level: LogLevel = LogLevel.WARNING


class NotFoundException(ApplicationException):
    error_code: str = "NOT_FOUND"
    message: str = "Resource not found"
    status_code: int = 404


class ConflictException(ApplicationException):
    error_code: str = "CONFLICT"
    message: str = "Resource conflict"
    status_code: int = 409


class TimeoutException(ApplicationException):
    error_code: str = "TIMEOUT"
    message: str = "Operation timed out"
    recoverable: bool = True
    status_code: int = 504


class YouTubeAPIException(ExternalServiceException):
    error_code: str = "YOUTUBE_API_ERROR"
    message: str = "YouTube API request failed"
    recoverable: bool = True


class YouTubeQuotaException(RateLimitException):
    error_code: str = "YOUTUBE_QUOTA_EXCEEDED"
    message: str = "YouTube API quota exceeded"


class TranscriptNotFoundException(NotFoundException):
    error_code: str = "TRANSCRIPT_NOT_FOUND"
    message: str = "Transcript not available for this video"


class VideoNotFoundException(NotFoundException):
    error_code: str = "VIDEO_NOT_FOUND"
    message: str = "Video not found"


class InvalidInputException(ValidationException):
    error_code: str = "INVALID_INPUT"
    message: str = "Invalid input provided"
