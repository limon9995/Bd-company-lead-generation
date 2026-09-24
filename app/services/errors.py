class SkipStage(Exception):
    """A stage cannot run (e.g. an API key is not configured). The job is marked 'skipped', not retried."""


class BudgetExceeded(Exception):
    """Daily call cap for a paid provider was reached. The job is postponed to the next day."""

    def __init__(self, provider: str):
        super().__init__(f"Daily cap reached for {provider}")
        self.provider = provider


class ProviderError(Exception):
    """A remote API returned an error. Retried with backoff."""
