class ApiError(Exception):
    """Error with a stable code. Bodies never include secrets or sensitive context (doc 06)."""

    def __init__(self, status: int, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.detail = status, code, message, detail


class Rejected(Exception):
    """A tool call the API refuses. The code is stable; the message is safe to say to the caller."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message
