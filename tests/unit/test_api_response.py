"""Tests for the standardized API response model."""

from __future__ import annotations

from models.api_response import (
    ErrorDetail,
    error_response,
    http_status_for,
    success_response,
)


class TestErrorDetail:
    def test_default_values(self):
        err = ErrorDetail()
        assert err.code == "INTERNAL_ERROR"
        assert err.field is None
        assert err.detail == ""

    def test_custom_values(self):
        err = ErrorDetail(code="VALIDATION_ERROR", field="email", detail="Invalid email")
        assert err.code == "VALIDATION_ERROR"
        assert err.field == "email"
        assert err.detail == "Invalid email"


class TestAPIResponse:
    def test_success_structure(self):
        resp = success_response(data={"key": "value"}, message="OK", request_id="abc123")
        content = resp.body
        import json
        body = json.loads(content)
        assert body["success"] is True
        assert body["data"] == {"key": "value"}
        assert body["message"] == "OK"
        assert body["errors"] is None
        assert body["request_id"] == "abc123"
        assert "timestamp" in body
        assert resp.status_code == 200

    def test_error_structure(self):
        resp = error_response(
            message="Not found",
            errors=[ErrorDetail(code="NOT_FOUND", detail="Resource not found")],
            request_id="abc123",
            status_code=404,
            error_code="NOT_FOUND",
        )
        import json
        body = json.loads(resp.body)
        assert body["success"] is False
        assert body["data"] is None
        assert body["message"] == "Not found"
        assert body["request_id"] == "abc123"
        assert len(body["errors"]) == 1
        assert body["errors"][0]["code"] == "NOT_FOUND"
        assert body["errors"][0]["detail"] == "Resource not found"
        assert resp.status_code == 404

    def test_error_without_errors_autogenerates(self):
        resp = error_response(
            message="Server error",
            status_code=500,
            error_code="INTERNAL_ERROR",
        )
        import json
        body = json.loads(resp.body)
        assert body["success"] is False
        assert len(body["errors"]) == 1
        assert body["errors"][0]["code"] == "INTERNAL_ERROR"
        assert body["errors"][0]["detail"] == "Server error"

    def test_success_without_request_id_generates_one(self):
        resp = success_response(data={"a": 1})
        import json
        body = json.loads(resp.body)
        assert body["request_id"] != ""

    def test_timestamp_format(self):
        resp = success_response()
        import json
        body = json.loads(resp.body)
        ts = body["timestamp"]
        assert ts.endswith("Z")
        assert "T" in ts


class TestHttpStatusMapping:
    def test_validation_error(self):
        assert http_status_for("VALIDATION_ERROR") == 400

    def test_not_found(self):
        assert http_status_for("NOT_FOUND") == 404

    def test_rate_limit(self):
        assert http_status_for("RATE_LIMIT_ERROR") == 429

    def test_timeout(self):
        assert http_status_for("TIMEOUT") == 504

    def test_external_service(self):
        assert http_status_for("EXTERNAL_SERVICE_ERROR") == 502

    def test_database_error(self):
        assert http_status_for("DATABASE_ERROR") == 503

    def test_youtube_quota(self):
        assert http_status_for("YOUTUBE_QUOTA_EXCEEDED") == 429

    def test_unknown(self):
        assert http_status_for("SOME_UNKNOWN_CODE") == 500
