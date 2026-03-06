import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.auth_user import AuthUser
from app.models.creator import Creator
from app.models.magic_link_token import MagicLinkToken
from app.services.email_stub import send_magic_link_email
from app.services.rate_limit import allow_magic_link_start

logger = logging.getLogger(__name__)


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _creator_name_from_email(email: str) -> str:
    local = email.split("@", 1)[0]
    return (local or "new_creator")[:255]


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def start_magic_link(db: Session, email: str) -> None:
    normalized_email = _normalize_email(email)
    settings = get_settings()

    if not allow_magic_link_start(normalized_email):
        logger.info("magic_link_start_rate_limited email=%s", normalized_email)
        return

    user = db.execute(
        select(AuthUser).where(AuthUser.email == normalized_email)
    ).scalar_one_or_none()

    if user is None:
        creator = Creator(name=_creator_name_from_email(normalized_email))
        db.add(creator)
        db.flush()

        user = AuthUser(creator_id=creator.id, email=normalized_email)
        db.add(user)
        db.flush()

    raw_token = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw_token)
    expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=settings.magic_link_token_ttl_minutes
    )
    db.add(
        MagicLinkToken(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=expires_at,
        )
    )
    db.commit()

    send_magic_link_email(email=normalized_email, token=raw_token)
    logger.info("magic_link_start_issued email=%s", normalized_email)
