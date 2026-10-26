"""Error types."""


class CleanupError(RuntimeError):
    """A safety stop: the tool refuses to continue and has changed nothing."""
