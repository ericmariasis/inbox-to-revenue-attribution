import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from secrets import token_hex
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClickEvent:
    event_id: str
    tid: str
    session_id: str
    hashed_ip: str
    timestamp: datetime


class ClickEventPublisher(Protocol):
    def publish(self, event: ClickEvent) -> None: ...


class LoggingClickEventPublisher:
    def publish(self, event: ClickEvent) -> None:
        logger.info(
            "click_event_published event_id=%s tid=%s session_id=%s hashed_ip=%s timestamp=%s",
            event.event_id,
            event.tid,
            event.session_id,
            event.hashed_ip,
            event.timestamp.isoformat(),
        )


DEFAULT_CLICK_EVENT_PUBLISHER: ClickEventPublisher = LoggingClickEventPublisher()


def hash_ip_address(*, ip_address: str | None) -> str:
    normalized_ip = ip_address or "unknown"
    return sha256(normalized_ip.encode("utf-8")).hexdigest()


def build_click_event(
    *,
    tid: str,
    session_id: str,
    ip_address: str | None,
) -> ClickEvent:
    return ClickEvent(
        event_id=token_hex(16),
        tid=tid,
        session_id=session_id,
        hashed_ip=hash_ip_address(ip_address=ip_address),
        timestamp=datetime.now(timezone.utc),
    )
