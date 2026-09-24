class SkipStage(Exception):
    """A stage cannot run (e.g. an API key is not configured). The job is marked 'skipped', not retried."""


class BudgetExceeded(Exception):
    """Daily call cap for a paid provider was reached. The job is postponed to the next day."""

    def __init__(self, provider: str):
        super().__init__(f"Daily cap reached for {provider}")
        self.provider = provider


class ProviderError(Exception):
    """A remote API returned an error. Retried with backoff."""


class SourceBlocked(Exception):
    """A website showed a CAPTCHA, login wall or block page. We stop (never bypass) and pause that source."""

    def __init__(self, source: str, detail: str = ""):
        super().__init__(f"{source} blocked automated access{': ' + detail if detail else ''}")
        self.source = source
