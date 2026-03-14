"""Exception hierarchy for liora_tools."""


class LioraAPIError(Exception):
    """Base exception for API errors."""

    def __init__(self, message: str, status_code: int = None, response=None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class AuthenticationError(LioraAPIError):
    """Expired session or invalid token."""


class SafetyGuardError(LioraAPIError):
    """Blocked by phone number allowlist."""


class GraphQLError(LioraAPIError):
    """GraphQL response contained errors."""

    def __init__(self, message: str, errors: list = None):
        super().__init__(message)
        self.errors = errors or []


class RateLimitError(LioraAPIError):
    """429 Too Many Requests."""
