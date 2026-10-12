class FriendlyError(Exception):
    """An error with a plain-language message for the user and technical details for the log."""

    def __init__(self, message, details=""):
        super().__init__(message)
        self.message = message
        self.details = details


class Cancelled(Exception):
    """The user pressed Cancel."""
