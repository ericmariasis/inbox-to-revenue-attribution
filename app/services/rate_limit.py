from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

MAGIC_LINK_WINDOW = timedelta(hours=1)
MAGIC_LINK_MAX_ATTEMPTS = 5
REDIRECT_SOFT_LIMIT_WINDOW = timedelta(minutes=5)
REDIRECT_SOFT_LIMIT_MAX_ATTEMPTS = 10

_attempts: dict[str, list[datetime]] = defaultdict(list)


@dataclass(frozen=True)
class RedirectSoftLimitState:
    attempt_count: int
    limit: int
    soft_limited: bool
    window_seconds: int


class RedirectSoftRateLimiter:
    def __init__(
        self,
        *,
        window: timedelta = REDIRECT_SOFT_LIMIT_WINDOW,
        max_attempts: int = REDIRECT_SOFT_LIMIT_MAX_ATTEMPTS,
    ) -> None:
        self.window = window
        self.max_attempts = max_attempts
        self._attempts: dict[tuple[str, str], list[datetime]] = defaultdict(list)

    def record_attempt(
        self,
        *,
        hashed_ip: str,
        tid: str,
        now: datetime | None = None,
    ) -> RedirectSoftLimitState:
        observed_at = now or datetime.now(timezone.utc)
        key = (hashed_ip, tid)
        recent = self._recent_attempts(key=key, cutoff=observed_at - self.window)
        recent.append(observed_at)
        self._attempts[key] = recent
        return self._build_state(attempt_count=len(recent))

    def snapshot_bucket(
        self,
        *,
        hashed_ip: str,
        tid: str,
        now: datetime | None = None,
    ) -> RedirectSoftLimitState:
        observed_at = now or datetime.now(timezone.utc)
        key = (hashed_ip, tid)
        recent = self._recent_attempts(key=key, cutoff=observed_at - self.window)
        if recent or key in self._attempts:
            self._attempts[key] = recent
        return self._build_state(attempt_count=len(recent))

    def _recent_attempts(
        self,
        *,
        key: tuple[str, str],
        cutoff: datetime,
    ) -> list[datetime]:
        return [ts for ts in self._attempts.get(key, []) if ts >= cutoff]

    def _build_state(self, *, attempt_count: int) -> RedirectSoftLimitState:
        return RedirectSoftLimitState(
            attempt_count=attempt_count,
            limit=self.max_attempts,
            soft_limited=attempt_count > self.max_attempts,
            window_seconds=int(self.window.total_seconds()),
        )


def allow_magic_link_start(email: str) -> bool:
    now = datetime.now(timezone.utc)
    cutoff = now - MAGIC_LINK_WINDOW
    recent = [ts for ts in _attempts[email] if ts >= cutoff]
    if len(recent) >= MAGIC_LINK_MAX_ATTEMPTS:
        _attempts[email] = recent
        return False

    recent.append(now)
    _attempts[email] = recent
    return True
