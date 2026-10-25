"""
Typed exceptions for Freebuff API Gateway.
Provides standardized error classification and OpenAI-compatible error payloads.
"""

from typing import Optional, Dict, Any

class FreebuffException(Exception):
    """Base exception for all Freebuff Gateway errors."""
    def __init__(
        self,
        message: str,
        status_code: int = 500,
        error_type: str = "api_error",
        error_code: Optional[str] = None,
        retry_after: Optional[float] = None
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_type = error_type
        self.error_code = error_code
        self.retry_after = retry_after

    def to_dict(self) -> Dict[str, Any]:
        err = {
            "message": self.message,
            "type": self.error_type,
            "code": self.error_code or self.status_code
        }
        if self.error_code:
            err["error_code"] = self.error_code
        return {"error": err}


class FreebuffAuthError(FreebuffException):
    """Authentication failure: invalid, revoked, or expired token."""
    def __init__(
        self,
        message: str = "Freebuff authentication failed: invalid or expired token. Run ./login.sh to re-authenticate.",
        error_code: Optional[str] = "invalid_api_key"
    ):
        super().__init__(
            message=message,
            status_code=401,
            error_type="authentication_error",
            error_code=error_code
        )


class FreebuffRateLimitError(FreebuffException):
    """Rate limit or spend limit reached."""
    def __init__(self, message: str, error_code: Optional[str] = "rate_limit_exceeded", retry_after: Optional[float] = None):
        super().__init__(
            message=message,
            status_code=429,
            error_type="rate_limit_error",
            error_code=error_code,
            retry_after=retry_after
        )


class FreebuffPermissionError(FreebuffException):
    """Permission denied: geoblocked, anonymous proxy blocked, or CLI verification failed."""
    def __init__(self, message: str, error_code: Optional[str] = "permission_denied"):
        super().__init__(
            message=message,
            status_code=403,
            error_type="permission_error",
            error_code=error_code
        )


class FreebuffServerError(FreebuffException):
    """Upstream Freebuff service error (5xx)."""
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(
            message=message,
            status_code=status_code,
            error_type="api_error",
            error_code="upstream_error"
        )


class FreebuffTimeoutError(FreebuffException):
    """Turn execution timeout."""
    def __init__(self, message: str = "Freebuff inference turn timed out."):
        super().__init__(
            message=message,
            status_code=504,
            error_type="timeout_error",
            error_code="turn_timeout"
        )


class FreebuffBadRequestError(FreebuffException):
    def __init__(self, message, error_code="invalid_request"):
        super().__init__(message, 400, "invalid_request_error", error_code)


class FreebuffPaymentError(FreebuffException):
    def __init__(self, message, error_code="payment_required"):
        super().__init__(message, 402, "billing_error", error_code)


class FreebuffInteractionRequiredError(FreebuffException):
    def __init__(
        self,
        message: str = "The CLI requires interactive input; complete it in the CLI before retrying.",
        error_code: Optional[str] = "interaction_required",
    ):
        super().__init__(message, 409, "interaction_required", error_code)
