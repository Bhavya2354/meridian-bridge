from __future__ import annotations


class MeridianError(Exception):
    code = "meridian_error"
    retryable = False

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message, "details": self.details}


class ConfigurationError(MeridianError):
    code = "configuration_error"


class AuthenticationError(MeridianError):
    code = "authentication_error"


class AuthorizationError(MeridianError):
    code = "permission_denied"


class NotFoundError(MeridianError):
    code = "not_found"


class ValidationError(MeridianError):
    code = "invalid_request"


class RateLimitError(MeridianError):
    code = "rate_limited"
    retryable = True

    def __init__(self, message: str, *, retry_after: float | None = None, details: dict | None = None) -> None:
        super().__init__(message, details=details)
        self.retry_after = retry_after


class UpstreamError(MeridianError):
    code = "upstream_error"
    retryable = True


class WriteNotAllowedError(MeridianError):
    code = "write_not_allowed"
