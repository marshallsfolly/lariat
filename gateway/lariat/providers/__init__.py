"""Dashboard providers. Each one reports normalized items, handles its own
allowlisted actions and maps a terminal request to a fixed argv."""


class ActionError(Exception):
    """An action the provider refused or that failed. Safe to show the client."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status
