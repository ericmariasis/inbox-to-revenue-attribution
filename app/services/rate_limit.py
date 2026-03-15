from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.orm import sessionmaker

from app.db.session import SessionLocal
from app.models.shared_rate_limit_event import SharedRateLimitEvent

MAGIC_LINK_WINDOW = timedelta(hours=1)
MAGIC_LINK_MAX_ATTEMPTS = 5
REDIRECT_SOFT_LIMIT_WINDOW = timedelta(minutes=5)
REDIRECT_SOFT_LIMIT_MAX_ATTEMPTS = 10
SUPPORT_REQUEST_SUBMIT_WINDOW = timedelta(minutes=15)
SUPPORT_REQUEST_SUBMIT_MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class RateLimitPolicy:
    namespace: str
    window: timedelta
    max_attempts: int


MAGIC_LINK_EMAIL_POLICY = RateLimitPolicy(
    namespace="magic_link_start_email",
    window=MAGIC_LINK_WINDOW,
    max_attempts=MAGIC_LINK_MAX_ATTEMPTS,
)
MAGIC_LINK_CLIENT_POLICY = RateLimitPolicy(
    namespace="magic_link_start_client",
    window=MAGIC_LINK_WINDOW,
    max_attempts=MAGIC_LINK_MAX_ATTEMPTS,
)
REDIRECT_SOFT_LIMIT_POLICY = RateLimitPolicy(
    namespace="redirect_soft_limit",
    window=REDIRECT_SOFT_LIMIT_WINDOW,
    max_attempts=REDIRECT_SOFT_LIMIT_MAX_ATTEMPTS,
)
SUPPORT_REQUEST_SUBMIT_POLICY = RateLimitPolicy(
    namespace="support_request_submit",
    window=SUPPORT_REQUEST_SUBMIT_WINDOW,
    max_attempts=SUPPORT_REQUEST_SUBMIT_MAX_ATTEMPTS,
)


@dataclass(frozen=True)
class SharedRateLimitState:
    attempt_count: int
    limit: int
    limited: bool
    window_seconds: int
    attempt_id: uuid.UUID | None = None

    @property
    def soft_limited(self) -> bool:
        return self.limited


class SharedRateLimiter:
    def __init__(self, *, session_factory: sessionmaker = SessionLocal) -> None:
        self._session_factory = session_factory

    def try_acquire(
        self,
        *,
        policy: RateLimitPolicy,
        bucket_key: str,
        now: datetime | None = None,
    ) -> SharedRateLimitState:
        observed_at = now or datetime.now(timezone.utc)
        cutoff = observed_at - policy.window

        with self._session_factory() as db:
            self._prune_expired(db=db, policy=policy, bucket_key=bucket_key, cutoff=cutoff)
            recent_count = self._count_recent(
                db=db,
                policy=policy,
                bucket_key=bucket_key,
                cutoff=cutoff,
            )
            if recent_count >= policy.max_attempts:
                db.commit()
                return self._build_state(
                    attempt_count=recent_count,
                    limit=policy.max_attempts,
                    limited=True,
                    window=policy.window,
                )

            attempt = SharedRateLimitEvent(
                namespace=policy.namespace,
                bucket_key=bucket_key,
                observed_at=observed_at,
            )
            db.add(attempt)
            db.flush()
            db.commit()
            return self._build_state(
                attempt_count=recent_count + 1,
                limit=policy.max_attempts,
                limited=False,
                window=policy.window,
                attempt_id=attempt.id,
            )

    def record_attempt(
        self,
        *,
        policy: RateLimitPolicy,
        bucket_key: str,
        now: datetime | None = None,
    ) -> SharedRateLimitState:
        observed_at = now or datetime.now(timezone.utc)
        cutoff = observed_at - policy.window

        with self._session_factory() as db:
            self._prune_expired(db=db, policy=policy, bucket_key=bucket_key, cutoff=cutoff)
            recent_count = self._count_recent(
                db=db,
                policy=policy,
                bucket_key=bucket_key,
                cutoff=cutoff,
            )
            attempt = SharedRateLimitEvent(
                namespace=policy.namespace,
                bucket_key=bucket_key,
                observed_at=observed_at,
            )
            db.add(attempt)
            db.flush()
            db.commit()
            attempt_count = recent_count + 1
            return self._build_state(
                attempt_count=attempt_count,
                limit=policy.max_attempts,
                limited=attempt_count > policy.max_attempts,
                window=policy.window,
                attempt_id=attempt.id,
            )

    def snapshot_bucket(
        self,
        *,
        policy: RateLimitPolicy,
        bucket_key: str,
        now: datetime | None = None,
    ) -> SharedRateLimitState:
        observed_at = now or datetime.now(timezone.utc)
        cutoff = observed_at - policy.window

        with self._session_factory() as db:
            self._prune_expired(db=db, policy=policy, bucket_key=bucket_key, cutoff=cutoff)
            recent_count = self._count_recent(
                db=db,
                policy=policy,
                bucket_key=bucket_key,
                cutoff=cutoff,
            )
            db.commit()
            return self._build_state(
                attempt_count=recent_count,
                limit=policy.max_attempts,
                limited=recent_count > policy.max_attempts,
                window=policy.window,
            )

    def release(self, *, attempt_id: uuid.UUID | None) -> None:
        if attempt_id is None:
            return

        with self._session_factory() as db:
            db.execute(delete(SharedRateLimitEvent).where(SharedRateLimitEvent.id == attempt_id))
            db.commit()

    def _count_recent(
        self,
        *,
        db,
        policy: RateLimitPolicy,
        bucket_key: str,
        cutoff: datetime,
    ) -> int:
        return db.execute(
            select(func.count())
            .select_from(SharedRateLimitEvent)
            .where(
                SharedRateLimitEvent.namespace == policy.namespace,
                SharedRateLimitEvent.bucket_key == bucket_key,
                SharedRateLimitEvent.observed_at >= cutoff,
            )
        ).scalar_one()

    def _prune_expired(
        self,
        *,
        db,
        policy: RateLimitPolicy,
        bucket_key: str,
        cutoff: datetime,
    ) -> None:
        db.execute(
            delete(SharedRateLimitEvent).where(
                SharedRateLimitEvent.namespace == policy.namespace,
                SharedRateLimitEvent.bucket_key == bucket_key,
                SharedRateLimitEvent.observed_at < cutoff,
            )
        )

    def _build_state(
        self,
        *,
        attempt_count: int,
        limit: int,
        limited: bool,
        window: timedelta,
        attempt_id: uuid.UUID | None = None,
    ) -> SharedRateLimitState:
        return SharedRateLimitState(
            attempt_count=attempt_count,
            limit=limit,
            limited=limited,
            window_seconds=int(window.total_seconds()),
            attempt_id=attempt_id,
        )


DEFAULT_SHARED_RATE_LIMITER = SharedRateLimiter()


def build_redirect_rate_limit_bucket_key(*, hashed_ip: str, tid: str) -> str:
    return f"{hashed_ip}:{tid}"


def build_support_request_rate_limit_bucket_key(*, creator_id: str, request_type: str) -> str:
    return f"{creator_id}:{request_type}"
