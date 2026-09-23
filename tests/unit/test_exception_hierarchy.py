"""Tests for the centralized exception hierarchy."""

from __future__ import annotations

from exceptions import (
    ApplicationException,
    AuthenticationException,
    AuthorizationException,
    BusinessException,
    ConfigurationException,
    ConflictException,
    DatabaseException,
    ExportException,
    ExternalServiceException,
    LogLevel,
    NotFoundException,
    PipelineException,
    RateLimitException,
    TimeoutException,
    ValidationException,
    YouTubeAPIException,
    YouTubeQuotaException,
)


class TestApplicationException:
    def test_default_values(self):
        exc = ApplicationException()
        assert exc.error_code == "INTERNAL_ERROR"
        assert exc.status_code == 500
        assert exc.recoverable is False
        assert exc.log_level == LogLevel.ERROR
        assert exc.request_id != ""

    def test_custom_values(self):
        exc = ApplicationException(
            message="Custom error",
            context={"key": "value"},
            request_id="abc123",
            error_code="CUSTOM_CODE",
            recoverable=True,
            log_level=LogLevel.WARNING,
            status_code=400,
        )
        assert exc.message == "Custom error"
        assert exc.context == {"key": "value"}
        assert exc.request_id == "abc123"
        assert exc.error_code == "CUSTOM_CODE"
        assert exc.recoverable is True
        assert exc.log_level == LogLevel.WARNING
        assert exc.status_code == 400

    def test_to_dict(self):
        exc = ApplicationException(message="test", request_id="abc")
        d = exc.to_dict()
        assert d["error_code"] == "INTERNAL_ERROR"
        assert d["message"] == "test"
        assert d["request_id"] == "abc"
        assert d["recoverable"] is False
        assert d["log_level"] == "ERROR"

    def test_auto_request_id(self):
        exc = ApplicationException()
        assert len(exc.request_id) == 8
        assert all(c in "0123456789abcdef" for c in exc.request_id)

    def test_string_representation(self):
        exc = ApplicationException("test error")
        assert str(exc) == "test error"


class TestConcreteExceptions:
    def test_validation_exception(self):
        exc = ValidationException("Invalid input")
        assert exc.error_code == "VALIDATION_ERROR"
        assert exc.status_code == 400
        assert exc.log_level == LogLevel.WARNING

    def test_not_found_exception(self):
        exc = NotFoundException("Resource")
        assert exc.error_code == "NOT_FOUND"
        assert exc.status_code == 404

    def test_rate_limit_exception(self):
        exc = RateLimitException("Too many")
        assert exc.error_code == "RATE_LIMIT_ERROR"
        assert exc.status_code == 429
        assert exc.recoverable is True

    def test_authentication_exception(self):
        exc = AuthenticationException()
        assert exc.error_code == "AUTHENTICATION_ERROR"
        assert exc.status_code == 401

    def test_authorization_exception(self):
        exc = AuthorizationException()
        assert exc.error_code == "AUTHORIZATION_ERROR"
        assert exc.status_code == 403

    def test_business_exception(self):
        exc = BusinessException("Cannot process")
        assert exc.error_code == "BUSINESS_ERROR"
        assert exc.status_code == 422

    def test_database_exception(self):
        exc = DatabaseException("Connection failed")
        assert exc.error_code == "DATABASE_ERROR"
        assert exc.recoverable is True

    def test_external_service_exception(self):
        exc = ExternalServiceException("API down")
        assert exc.error_code == "EXTERNAL_SERVICE_ERROR"
        assert exc.recoverable is True

    def test_pipeline_exception(self):
        exc = PipelineException("Stage failed")
        assert exc.error_code == "PIPELINE_ERROR"
        assert exc.recoverable is True

    def test_export_exception(self):
        exc = ExportException("Export failed")
        assert exc.error_code == "EXPORT_ERROR"
        assert exc.recoverable is True

    def test_configuration_exception(self):
        exc = ConfigurationException("Bad config")
        assert exc.error_code == "CONFIGURATION_ERROR"
        assert exc.status_code == 500

    def test_conflict_exception(self):
        exc = ConflictException("Already exists")
        assert exc.error_code == "CONFLICT"
        assert exc.status_code == 409

    def test_timeout_exception(self):
        exc = TimeoutException("Timed out")
        assert exc.error_code == "TIMEOUT"
        assert exc.recoverable is True

    def test_youtube_api_exception(self):
        exc = YouTubeAPIException("API error")
        assert exc.error_code == "YOUTUBE_API_ERROR"
        assert exc.status_code == 502
        assert exc.recoverable is True

    def test_youtube_quota_exception(self):
        exc = YouTubeQuotaException("Quota exceeded")
        assert exc.error_code == "YOUTUBE_QUOTA_EXCEEDED"
        assert exc.status_code == 429
        assert exc.recoverable is True
