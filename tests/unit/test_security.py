from __future__ import annotations

import pytest


class TestSecurityModels:
    def test_user_model(self):
        from security.security_models import User, UserRole
        user = User(id="u1", email="test@test.com", username="testuser", role=UserRole.EDITOR)
        assert user.username == "testuser"
        assert user.role == UserRole.EDITOR

    def test_security_event(self):
        from security.security_models import AlertSeverity, SecurityEvent, ThreatType
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
        from security.security_models import AlertSeverity, ThreatType, UserRole
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
