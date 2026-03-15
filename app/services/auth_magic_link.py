import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from jose import jwt
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.auth_user import AuthUser
from app.models.creator import Creator
from app.models.magic_link_token import MagicLinkToken
from app.models.pending_magic_link_issuance import PendingMagicLinkIssuance
from app.services.email_provider import EmailProvider, MagicLinkEmailDeliveryError, MagicLinkEmailMessage
from app.services.rate_limit import allow_magic_link_start, release_magic_link_start

logger = logging.getLogger(__name__)
VERIFY_FAILURE_DETAIL = "invalid or expired token"
START_RETRY_DETAIL = "unable to send sign-in email right now; please try again in a few minutes"


@dataclass(frozen=True)
class _MagicLinkSubject:
    user_id: str
    creator_id: str
    email: str


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _creator_name_from_email(email: str) -> str:
    local = email.split("@", 1)[0]
    return (local or "new_creator")[:255]


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def start_magic_link(db: Session, email: str, *, provider: EmailProvider) -> None:
    normalized_email = _normalize_email(email)
    settings = get_settings()

    if not allow_magic_link_start(normalized_email):
        logger.info("magic_link_start_rate_limited email=%s", normalized_email)
        return

    user = db.execute(
        select(AuthUser).where(AuthUser.email == normalized_email)
    ).scalar_one_or_none()

    raw_token = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw_token)
    expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=settings.magic_link_token_ttl_minutes
    )
    if user is None:
        db.add(
            PendingMagicLinkIssuance(
                email=normalized_email,
                token_hash=token_hash,
                expires_at=expires_at,
            )
        )
    else:
        db.add(
            MagicLinkToken(
                user_id=user.id,
                token_hash=token_hash,
                expires_at=expires_at,
            )
        )
    db.commit()

    try:
        provider.send_magic_link(
            MagicLinkEmailMessage(
                email=normalized_email,
                raw_token=raw_token,
                magic_link_url=_build_magic_link_url(token=raw_token, settings=settings),
                expires_in_minutes=settings.magic_link_token_ttl_minutes,
            )
        )
    except MagicLinkEmailDeliveryError:
        release_magic_link_start(normalized_email)
        logger.warning(
            "magic_link_start_delivery_failed email=%s provider=%s",
            normalized_email,
            type(provider).__name__,
        )
        raise

    logger.info(
        "magic_link_start_issued email=%s provider=%s",
        normalized_email,
        type(provider).__name__,
    )


def verify_magic_link_token(db: Session, token: str) -> str:
    token_hash = _hash_token(token)
    now = datetime.now(timezone.utc)

    try:
        subject = _consume_existing_user_magic_link(db, token_hash=token_hash, now=now)
        if subject is None:
            subject = _consume_pending_magic_link(db, token_hash=token_hash, now=now)
    except ValueError:
        logger.warning("magic_link_verify_failed")
        raise ValueError(VERIFY_FAILURE_DETAIL)

    if subject is None:
        db.rollback()
        logger.warning("magic_link_verify_failed")
        raise ValueError(VERIFY_FAILURE_DETAIL)

    access_token = _create_access_token(
        user_id=subject.user_id,
        creator_id=subject.creator_id,
        email=subject.email,
    )
    logger.info("magic_link_verify_succeeded email=%s", subject.email)
    return access_token


def _consume_existing_user_magic_link(
    db: Session,
    *,
    token_hash: str,
    now: datetime,
) -> _MagicLinkSubject | None:
    token_row = db.execute(
        select(MagicLinkToken)
        .where(MagicLinkToken.token_hash == token_hash)
        .with_for_update()
    ).scalar_one_or_none()
    if token_row is None:
        return None

    if token_row.used_at is not None or token_row.expires_at <= now:
        db.rollback()
        raise ValueError(VERIFY_FAILURE_DETAIL)

    token_row.used_at = now
    subject = _build_magic_link_subject(token_row.user)
    db.commit()
    return subject


def _consume_pending_magic_link(
    db: Session,
    *,
    token_hash: str,
    now: datetime,
) -> _MagicLinkSubject | None:
    for attempt in range(2):
        issuance = db.execute(
            select(PendingMagicLinkIssuance)
            .where(PendingMagicLinkIssuance.token_hash == token_hash)
            .with_for_update()
        ).scalar_one_or_none()
        if issuance is None:
            return None

        if issuance.used_at is not None or issuance.expires_at <= now:
            db.rollback()
            raise ValueError(VERIFY_FAILURE_DETAIL)

        user = db.execute(
            select(AuthUser).where(AuthUser.email == issuance.email)
        ).scalar_one_or_none()
        if user is None:
            creator = Creator(name=_creator_name_from_email(issuance.email))
            db.add(creator)
            db.flush()

            user = AuthUser(creator_id=creator.id, email=issuance.email)
            db.add(user)
            try:
                db.flush()
            except IntegrityError:
                db.rollback()
                if attempt == 0:
                    continue
                raise

        issuance.used_at = now
        subject = _build_magic_link_subject(user)
        db.commit()
        return subject

    return None


def _build_magic_link_subject(user: AuthUser) -> _MagicLinkSubject:
    return _MagicLinkSubject(
        user_id=str(user.id),
        creator_id=str(user.creator_id),
        email=user.email,
    )


def _create_access_token(*, user_id: str, creator_id: str, email: str) -> str:
    settings = get_settings()
    issued_at = datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(hours=settings.jwt_access_token_ttl_hours)
    payload = {
        "sub": user_id,
        "creator_id": creator_id,
        "email": email,
        "iat": issued_at,
        "exp": expires_at,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def _build_magic_link_url(*, token: str, settings) -> str:
    base_url = settings.magic_link_base_url.rstrip("/")
    return f"{base_url}/auth/magic-link/verify?{urlencode({'token': token})}"
