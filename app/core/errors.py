"""Domain errors. Services raise these; the web layer turns them into responses.

Every message here is written for the person using the app. Internal details go to
the log, never into a message.
"""


class DomainError(Exception):
    """Base class. `code` is stable and machine-readable; `message` is user-safe."""

    code = "error"
    default_message = "The request could not be completed."

    def __init__(self, message: str | None = None, *, code: str | None = None) -> None:
        self.message = message or self.default_message
        if code is not None:
            self.code = code
        super().__init__(self.message)


class NotFound(DomainError):
    code = "not_found"
    default_message = "We couldn't find that. It may have been removed, or you may not have access."


class PermissionDenied(DomainError):
    code = "permission_denied"
    default_message = "You don't have access to do that."


class ValidationFailed(DomainError):
    code = "validation_failed"
    default_message = "Some details need your attention."

    def __init__(
        self,
        message: str | None = None,
        *,
        field_errors: dict[str, str] | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message, code=code)
        self.field_errors = field_errors or {}


class Conflict(DomainError):
    code = "conflict"
    default_message = "This record changed while you were working on it. Reload and try again."
