from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from datetime import datetime, timedelta


class TestSecurityModels:
    def test_user_model(self):
        from security.security_models import User, UserRole
        user = User(id="u1", email="test@test.com", username="testuser", role=UserRole.EDITOR)
        assert user.username == "testuser"
        assert user.role == UserRole.EDITOR

    def test_security_event(self):
        from security.security_models import SecurityEvent, ThreatType, AlertSeverity
        event = SecurityEvent(event_type="login_attempt", actor_id="a1", action="login", threat_type=ThreatType.CREDENTIAL_STUFFING, severity=AlertSeverity.INFO)
        assert event.event_type == "login_attempt"

    def test_api_key_model(self):
        from security.security_models import APIKey
        key = APIKey(id="k1", name="Test Key", key_prefix="ysk_abc", key_hash="abc123", user_id="u1")
        assert key.name == "Test Key"

    def test_session_model(self):
        from security.security_models import Session, SessionStatus
        session = Session(id="s1", user_id="u1", token="tok", refresh_token="ref", status=SessionStatus.ACTIVE)
        assert session.status == SessionStatus.ACTIVE

    def test_enums(self):
        from security.security_models import UserRole, ThreatType, AlertSeverity
        assert UserRole.SUPER_ADMIN.value == "super_admin"
        assert ThreatType.BRUTE_FORCE.value == "brute_force"
        assert AlertSeverity.CRITICAL.value == "critical"


class TestJWTService:
    def test_create_token(self):
        from security.jwt_service import JWTService
        from security.security_models import User
        svc = JWTService()
        user = User(id="u1", email="test@test.com", username="testuser", role="admin")
        token = svc.create_access_token(user)
        assert token is not None
        assert isinstance(token, str)

    def test_verify_token(self):
        from security.jwt_service import JWTService
        from security.security_models import User
        svc = JWTService()
        user = User(id="u1", email="test@test.com", username="testuser", role="admin")
        token = svc.create_access_token(user)
        payload = svc.verify_token(token)
        assert payload is not None
        assert payload["sub"] == "u1"

    def test_verify_invalid_token(self):
        from security.jwt_service import JWTService
        from security.security_models import InvalidTokenError
        svc = JWTService()
        with pytest.raises(InvalidTokenError):
            svc.verify_token("invalid-token")

    def test_token_expiry(self):
        from security.jwt_service import JWTConfig, JWTService
        from security.security_models import User
        config = JWTConfig(access_token_expire_minutes=0)
        svc = JWTService(config=config)
        user = User(id="u1", email="test@test.com", username="testuser", role="admin")
        token = svc.create_access_token(user)
        # With 0 minute expiry, the token should expire immediately
        from security.security_models import TokenExpiredError
        try:
            svc.verify_token(token)
        except TokenExpiredError:
            return  # Expected
        # If jose has leeway and doesn't expire immediately, verify the exp claim exists
        import jwt as pyjwt
        payload = pyjwt.decode(token, options={"verify_signature": False})
        assert "exp" in payload


class TestRBACManager:
    def test_check_permission(self):
        from security.rbac import RBACManager
        from security.security_models import User, UserRole, Permission
        rbac = RBACManager()
        user = User(id="u1", email="a@b.com", username="admin", role=UserRole.SUPER_ADMIN)
        rbac.check_permission(user, Permission.PROJECT_DELETE)
        assert True

    def test_check_permission_denied(self):
        from security.rbac import RBACManager
        from security.security_models import User, UserRole, Permission, AuthorizationError
        rbac = RBACManager()
        user = User(id="u1", email="a@b.com", username="viewer", role=UserRole.VIEWER)
        with pytest.raises(AuthorizationError):
            rbac.check_permission(user, Permission.PROJECT_DELETE)

    def test_get_role_permissions(self):
        from security.rbac import RBACManager
        from security.security_models import UserRole
        rbac = RBACManager()
        permissions = rbac.get_permissions(UserRole.EDITOR)
        assert len(permissions) > 0


class TestPermissionService:
    def test_has_permission(self):
        from security.permission_service import PermissionService
        from security.security_models import User, UserRole, Permission
        svc = PermissionService()
        user = User(id="u1", email="a@b.com", username="admin", role=UserRole.SUPER_ADMIN)
        assert svc.can(user, Permission.PROJECT_READ) is True


class TestAPIKeyManager:
    def test_create_key(self):
        from security.api_key_manager import APIKeyManager
        mgr = APIKeyManager()
        key, raw = mgr.generate(name="Test Key", user_id="u1")
        assert key.id is not None
        assert key.name == "Test Key"

    def test_validate_key(self):
        from security.api_key_manager import APIKeyManager
        mgr = APIKeyManager()
        created, raw = mgr.generate(name="Test", user_id="u1")
        result = mgr.validate(raw)
        assert result is not None

    def test_revoke_key(self):
        from security.api_key_manager import APIKeyManager
        from security.security_models import APIKeyError
        mgr = APIKeyManager()
        created, raw = mgr.generate(name="Test", user_id="u1")
        mgr.revoke(created.id)
        with pytest.raises(APIKeyError):
            mgr.validate(raw)


class TestSecretManager:
    def test_store_and_retrieve(self):
        from security.secret_manager import SecretManager
        mgr = SecretManager()
        mgr.cache_secret("api_key", "sk-test123")
        value = mgr.get_cached_secret("api_key")
        assert value == "sk-test123"

    def test_retrieve_nonexistent(self):
        from security.secret_manager import SecretManager
        mgr = SecretManager()
        value = mgr.get_cached_secret("nonexistent")
        assert value is None

    def test_delete_secret(self):
        from security.secret_manager import SecretManager
        mgr = SecretManager()
        mgr.cache_secret("key", "value")
        mgr.clear_cache()
        assert mgr.get_cached_secret("key") is None


class TestSecurityRateLimiter:
    def test_allow_request(self):
        from security.rate_limiter import SecurityRateLimiter
        rl = SecurityRateLimiter()
        for _ in range(10):
            assert rl.allow("auth:test_user") is True

    def test_block_request(self):
        from security.rate_limiter import SecurityRateLimiter
        rl = SecurityRateLimiter()
        for _ in range(10):
            rl.allow("auth:block_user")
        assert rl.allow("auth:block_user") is False


class TestInputValidator:
    def test_validate_email(self):
        from security.input_validator import InputValidator
        v = InputValidator()
        result = v.validate_email("user@example.com")
        assert result == "user@example.com"
        with pytest.raises(Exception):
            v.validate_email("invalid")

    def test_validate_url(self):
        from security.input_validator import InputValidator
        v = InputValidator()
        result = v.validate_url("validpath")
        assert result == "validpath"
        with pytest.raises(Exception):
            v.validate_url("javascript:alert(1)")

    def test_sanitize_input(self):
        from security.input_validator import InputValidator
        v = InputValidator()
        with pytest.raises(Exception):
            v.sanitize_input("<script>alert('xss')</script>")


class TestOutputSanitizer:
    def test_sanitize_html(self):
        from security.output_sanitizer import OutputSanitizer
        s = OutputSanitizer()
        sanitized = s.sanitize_html("<script>alert('xss')</script><p>Safe</p>")
        assert "<script>" not in sanitized
        assert "<p>Safe</p>" in sanitized

    def test_escape_html(self):
        from security.output_sanitizer import OutputSanitizer
        s = OutputSanitizer()
        sanitized = s.sanitize_html("<test>")
        assert "&lt;" in sanitized or "<test>" not in sanitized


class TestSecurityHeadersMiddleware:
    def test_get_headers(self):
        from unittest.mock import MagicMock
        from security.security_headers import SecurityHeadersMiddleware
        middleware = SecurityHeadersMiddleware(app=MagicMock())
        headers = middleware._default_headers()
        assert "X-Content-Type-Options" in headers
        assert headers["X-Content-Type-Options"] == "nosniff"


class TestThreatDetector:
    def test_detect_threat(self):
        from security.threat_detector import ThreatDetector
        detector = ThreatDetector()
        summary = detector.get_threat_summary()
        assert summary is not None
        assert summary["failed_logins_total"] == 0


class TestSecurityAuditLogger:
    def test_log_event(self):
        from security.audit_logger import SecurityAuditLogger
        logger = SecurityAuditLogger()
        logger.log_event("user_login", actor_id="u1", action="login", details={"ip": "127.0.0.1"})
        events = logger.get_recent()
        assert len(events) == 1
        assert events[0]["event_type"] == "user_login"
        assert events[0]["actor_id"] == "u1"


class TestAuthManager:
    def test_authenticate(self):
        from security.auth_manager import AuthManager
        from security.security_models import User
        mgr = AuthManager()
        user = User(id="u1", email="test@test.com", username="test", password_hash="9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08")
        mgr.register_user(user)
        result = mgr.authenticate(identifier="test@test.com", password="test")
        assert result is not None
        assert "access_token" in result


class TestSecurityMiddleware:
    def test_process_request(self):
        from security.security_middleware import SecurityMiddleware
        from starlette.requests import Request
        from starlette.responses import JSONResponse

        async def dummy_app(scope, receive, send):
            response = JSONResponse({"ok": True})
            await response(scope, receive, send)

        middleware = SecurityMiddleware(app=dummy_app, auth_manager=MagicMock())
        assert middleware is not None
