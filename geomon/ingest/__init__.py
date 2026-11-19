"""Data acquisition steps. Run with: python -m geomon.ingest [all|admin|rpg|osm|dem|soil|insee]"""

import datetime as _dt


class RetryLater(RuntimeError):
    """A step failure not worth retrying before ``retry_at`` (e.g. a daily quota): the scheduler
    waits until then instead of its usual 30 minutes."""

    def __init__(self, message: str, retry_at: _dt.datetime):
        super().__init__(message)
        self.retry_at = retry_at
