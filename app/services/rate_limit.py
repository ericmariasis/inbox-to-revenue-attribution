from collections import defaultdict
from datetime import datetime, timedelta, timezone

WINDOW = timedelta(hours=1)
MAX_ATTEMPTS = 5

_attempts: dict[str, list[datetime]] = defaultdict(list)


def allow_magic_link_start(email: str) -> bool:
    now = datetime.now(timezone.utc)
    cutoff = now - WINDOW
    recent = [ts for ts in _attempts[email] if ts >= cutoff]
    if len(recent) >= MAX_ATTEMPTS:
        _attempts[email] = recent
        return False

    recent.append(now)
    _attempts[email] = recent
    return True
