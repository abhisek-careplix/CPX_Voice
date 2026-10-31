class ApiError(Exception):
    """Error with a stable code. Bodies never include secrets or sensitive context (doc 06)."""

    def __init__(self, status: int, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.detail = status, code, message, detail
