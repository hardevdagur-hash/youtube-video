from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any


class AuthProvider(Enum):
    EMAIL = "email"
    GOOGLE = "google"
    GITHUB = "github"
    MICROSOFT = "microsoft"
    API_KEY = "api_key"


class UserRole(Enum):
    SUPER_ADMIN = "super_admin"
    ORG_ADMIN = "org_admin"
    EDITOR = "editor"
    WRITER = "writer"
    REVIEWER = "reviewer"
    VIEWER = "viewer"
    API_CLIENT = "api_client"


class Permission(Enum):
    PROJECT_CREATE = "project:create"
    PROJECT_READ = "project:read"
    PROJECT_UPDATE = "project:update"
    PROJECT_DELETE = "project:delete"
    PROJECT_LIST = "project:list"
    EXPORT_CREATE = "export:create"
    EXPORT_READ = "export:read"
    EXPORT_DELETE = "export:delete"
    AI_GENERATE = "ai:generate"
    AI_READ = "ai:read"
    PROMPT_MANAGE = "prompt:manage"
    PROMPT_READ = "prompt:read"
    ADMIN_ACCESS = "admin:access"
    USER_MANAGE = "user:manage"
    API_KEY_MANAGE = "api_key:manage"
    SECURITY_MANAGE = "security:manage"
    SETTINGS_MANAGE = "settings:manage"
    ORG_MANAGE = "org:manage"
    AUDIT_READ = "audit:read"


ROLE_PERMISSIONS: dict[UserRole, list[Permission]] = {
    UserRole.SUPER_ADMIN: list(Permission),
    UserRole.ORG_ADMIN: [
        Permission.PROJECT_CREATE, Permission.PROJECT_READ, Permission.PROJECT_UPDATE,
        Permission.PROJECT_DELETE, Permission.PROJECT_LIST,
        Permission.EXPORT_CREATE, Permission.EXPORT_READ, Permission.EXPORT_DELETE,
        Permission.AI_GENERATE, Permission.AI_READ,
        Permission.PROMPT_MANAGE, Permission.PROMPT_READ,
        Permission.USER_MANAGE,
        Permission.AUDIT_READ,
        Permission.SETTINGS_MANAGE,
    ],
    UserRole.EDITOR: [
        Permission.PROJECT_CREATE, Permission.PROJECT_READ, Permission.PROJECT_UPDATE,
        Permission.PROJECT_LIST,
        Permission.EXPORT_CREATE, Permission.EXPORT_READ,
        Permission.AI_GENERATE, Permission.AI_READ,
        Permission.PROMPT_READ,
    ],
    UserRole.WRITER: [
        Permission.PROJECT_READ, Permission.PROJECT_UPDATE, Permission.PROJECT_LIST,
        Permission.EXPORT_CREATE, Permission.EXPORT_READ,
        Permission.AI_GENERATE, Permission.AI_READ,
        Permission.PROMPT_READ,
    ],
    UserRole.REVIEWER: [
        Permission.PROJECT_READ, Permission.PROJECT_LIST,
        Permission.EXPORT_READ,
        Permission.AI_READ,
        Permission.PROMPT_READ,
    ],
    UserRole.VIEWER: [
        Permission.PROJECT_READ, Permission.PROJECT_LIST,
        Permission.EXPORT_READ,
        Permission.AI_READ,
        Permission.PROMPT_READ,
    ],
    UserRole.API_CLIENT: [
        Permission.PROJECT_CREATE, Permission.PROJECT_READ, Permission.PROJECT_LIST,
        Permission.EXPORT_CREATE, Permission.EXPORT_READ,
        Permission.AI_GENERATE, Permission.AI_READ,
    ],
}


class ThreatType(Enum):
    BRUTE_FORCE = "brute_force"
    CREDENTIAL_STUFFING = "credential_stuffing"
    API_ABUSE = "api_abuse"
    PROMPT_INJECTION = "prompt_injection"
    SQL_INJECTION = "sql_injection"
    XSS = "xss"
    CSRF = "csrf"
    PATH_TRAVERSAL = "path_traversal"
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
    SUSPICIOUS_TOKEN = "suspicious_token"  # noqa: S105 - threat category name, not a secret
    UNAUTHORIZED_ACCESS = "unauthorized_access"
    SENSITIVE_DATA_EXPOSURE = "sensitive_data_exposure"


class AlertSeverity(Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class MFAStatus(Enum):
    DISABLED = "disabled"
    EMAIL = "email"
    TOTP = "totp"
    SMS = "sms"


class SessionStatus(Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


@dataclass
class User:
    id: str
    email: str
    username: str
    role: UserRole = UserRole.VIEWER
    organization_id: str = ""
    is_active: bool = True
    is_verified: bool = False
    mfa_status: MFAStatus = MFAStatus.DISABLED
    password_hash: str = ""
    created_at: str = ""
    last_login: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now(UTC).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "email": self.email,
            "username": self.username,
            "role": self.role.value,
            "organization_id": self.organization_id,
            "is_active": self.is_active,
            "is_verified": self.is_verified,
            "mfa_status": self.mfa_status.value,
            "created_at": self.created_at,
            "last_login": self.last_login,
        }


@dataclass
class Session:
    id: str
    user_id: str
    token: str
    refresh_token: str
    device_info: str = ""
    ip_address: str = ""
    status: SessionStatus = SessionStatus.ACTIVE
    created_at: str = ""
    expires_at: str = ""
    last_activity: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now(UTC).isoformat()

    def is_expired(self) -> bool:
        if not self.expires_at:
            return False
        return datetime.fromisoformat(self.expires_at) < datetime.now(UTC)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "device_info": self.device_info,
            "ip_address": self.ip_address,
            "status": self.status.value,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "last_activity": self.last_activity,
        }


@dataclass
class APIKey:
    id: str
    name: str
    key_prefix: str
    key_hash: str
    user_id: str
    organization_id: str = ""
    permissions: list[Permission] = field(default_factory=lambda: list(Permission))
    is_active: bool = True
    expires_at: str = ""
    created_at: str = ""
    last_used_at: str = ""
    allowed_ips: list[str] = field(default_factory=list)
    allowed_origins: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now(UTC).isoformat()

    def is_expired(self) -> bool:
        if not self.expires_at:
            return False
        return datetime.fromisoformat(self.expires_at) < datetime.now(UTC)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "key_prefix": self.key_prefix,
            "user_id": self.user_id,
            "organization_id": self.organization_id,
            "permissions": [p.value for p in self.permissions],
            "is_active": self.is_active,
            "expires_at": self.expires_at,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
        }


@dataclass
class SecurityEvent:
    event_type: str
    actor_id: str
    action: str
    resource_type: str = ""
    resource_id: str = ""
    threat_type: ThreatType | None = None
    severity: AlertSeverity = AlertSeverity.INFO
    details: dict[str, Any] = field(default_factory=dict)
    ip_address: str = ""
    user_agent: str = ""
    trace_id: str = ""
    timestamp: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(UTC).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "actor_id": self.actor_id,
            "action": self.action,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "threat_type": self.threat_type.value if self.threat_type else None,
            "severity": self.severity.value,
            "details": self.details,
            "ip_address": self.ip_address,
            "user_agent": self.user_agent,
            "trace_id": self.trace_id,
            "timestamp": self.timestamp,
        }


class SecurityError(Exception):
    pass


class AuthenticationError(SecurityError):
    pass


class AuthorizationError(SecurityError):
    pass


class RateLimitError(SecurityError):
    pass


class InvalidTokenError(SecurityError):
    pass


class TokenExpiredError(SecurityError):
    pass


class APIKeyError(SecurityError):
    pass


class PromptInjectionError(SecurityError):
    pass


class ValidationError(SecurityError):
    pass


class ThreatDetectedError(SecurityError):
    def __init__(self, message: str, threat_type: ThreatType, severity: AlertSeverity = AlertSeverity.HIGH):
        super().__init__(message)
        self.threat_type = threat_type
        self.severity = severity
