import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.support_request import SupportRequestRecord
from app.services.email_provider import (
    EmailProvider,
    SupportRequestEmailDeliveryError,
    SupportRequestEmailMessage,
)

logger = logging.getLogger(__name__)

SUPPORT_REQUEST_TYPE_WORKSPACE_RESET = "workspace-reset"
SUPPORT_REQUEST_TYPE_ACCOUNT_DELETION = "account-deletion"
ACCOUNT_SUPPORT_EMAIL = "eric@careercodepro.com"

SUPPORT_REQUEST_STATUS_NOTIFICATION_PENDING = "notification_pending"
SUPPORT_REQUEST_STATUS_PENDING = "pending"
SUPPORT_REQUEST_STATUS_NOTIFICATION_FAILED = "notification_failed"


@dataclass(frozen=True)
class SupportRequestUpsertResult:
    request_record: SupportRequestRecord
    created: bool


def create_or_get_active_support_request(
    db: Session,
    *,
    creator_id,
    request_type: str,
    requester_email: str,
    creator_name: str,
) -> SupportRequestUpsertResult:
    active_request = get_active_support_request(
        db,
        creator_id=creator_id,
        request_type=request_type,
    )
    if active_request is not None:
        return SupportRequestUpsertResult(request_record=active_request, created=False)

    request_record = SupportRequestRecord(
        creator_id=creator_id,
        request_type=request_type,
        requester_email=requester_email,
        creator_name_snapshot=creator_name,
        status=SUPPORT_REQUEST_STATUS_NOTIFICATION_PENDING,
    )
    db.add(request_record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        active_request = get_active_support_request(
            db,
            creator_id=creator_id,
            request_type=request_type,
        )
        if active_request is not None:
            return SupportRequestUpsertResult(request_record=active_request, created=False)
        raise

    db.refresh(request_record)
    return SupportRequestUpsertResult(request_record=request_record, created=True)


def get_active_support_request(
    db: Session,
    *,
    creator_id,
    request_type: str,
) -> SupportRequestRecord | None:
    return (
        db.execute(
            select(SupportRequestRecord)
            .where(
                SupportRequestRecord.creator_id == creator_id,
                SupportRequestRecord.request_type == request_type,
                SupportRequestRecord.closed_at.is_(None),
            )
            .order_by(SupportRequestRecord.created_at.desc(), SupportRequestRecord.id.desc())
        )
        .scalars()
        .first()
    )


def list_active_support_requests_for_creator(
    db: Session,
    *,
    creator_id,
) -> dict[str, SupportRequestRecord]:
    rows = (
        db.execute(
            select(SupportRequestRecord)
            .where(
                SupportRequestRecord.creator_id == creator_id,
                SupportRequestRecord.closed_at.is_(None),
            )
            .order_by(SupportRequestRecord.created_at.desc(), SupportRequestRecord.id.desc())
        )
        .scalars()
        .all()
    )
    return {row.request_type: row for row in rows}


def mark_support_request_notification_succeeded(
    db: Session,
    *,
    request_record: SupportRequestRecord,
    attempted_at: datetime | None = None,
) -> SupportRequestRecord:
    timestamp = attempted_at or datetime.now(timezone.utc)
    request_record.status = SUPPORT_REQUEST_STATUS_PENDING
    request_record.notification_attempted_at = timestamp
    request_record.notification_sent_at = timestamp
    request_record.notification_failed_at = None
    db.add(request_record)
    db.commit()
    db.refresh(request_record)
    return request_record


def mark_support_request_notification_failed(
    db: Session,
    *,
    request_record: SupportRequestRecord,
    attempted_at: datetime | None = None,
) -> SupportRequestRecord:
    timestamp = attempted_at or datetime.now(timezone.utc)
    request_record.status = SUPPORT_REQUEST_STATUS_NOTIFICATION_FAILED
    request_record.notification_attempted_at = timestamp
    request_record.notification_failed_at = timestamp
    db.add(request_record)
    db.commit()
    db.refresh(request_record)
    return request_record


def send_support_request_email(
    *,
    provider: EmailProvider,
    request_record: SupportRequestRecord,
) -> None:
    message = SupportRequestEmailMessage(
        request_id=str(request_record.id),
        support_email=ACCOUNT_SUPPORT_EMAIL,
        request_type=request_record.request_type,
        requester_email=request_record.requester_email,
        creator_name=request_record.creator_name_snapshot,
        creator_id=str(request_record.creator_id),
        requested_at=_requested_at_isoformat(request_record.created_at),
        subject=_support_request_subject(request_record.request_type, request_record.requester_email),
        body=_support_request_body(request_record=request_record),
    )

    try:
        provider.send_support_request(message)
    except SupportRequestEmailDeliveryError:
        logger.warning(
            "support_request_delivery_failed request_id=%s request_type=%s support_email=%s requester_email=%s creator_id=%s",
            request_record.id,
            request_record.request_type,
            ACCOUNT_SUPPORT_EMAIL,
            request_record.requester_email,
            request_record.creator_id,
        )
        raise

    logger.info(
        "support_request_delivery_succeeded request_id=%s request_type=%s support_email=%s requester_email=%s creator_id=%s",
        request_record.id,
        request_record.request_type,
        ACCOUNT_SUPPORT_EMAIL,
        request_record.requester_email,
        request_record.creator_id,
    )


def _requested_at_isoformat(requested_at: datetime | None) -> str:
    if requested_at is None:
        requested_at = datetime.now(timezone.utc)
    return requested_at.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _support_request_subject(request_type: str, requester_email: str) -> str:
    if request_type == SUPPORT_REQUEST_TYPE_WORKSPACE_RESET:
        return f"Workspace reset request for {requester_email}"
    return f"Account deletion request for {requester_email}"


def _support_request_body(*, request_record: SupportRequestRecord) -> str:
    request_label = (
        "Workspace reset"
        if request_record.request_type == SUPPORT_REQUEST_TYPE_WORKSPACE_RESET
        else "Account deletion"
    )
    requested_at = request_record.created_at or datetime.now(timezone.utc)
    return (
        f"{request_label} request\n\n"
        "This beta request was submitted from the signed-in account page.\n\n"
        f"Request id: {request_record.id}\n"
        f"Request type: {request_record.request_type}\n"
        f"Signed-in email: {request_record.requester_email}\n"
        f"Workspace name: {request_record.creator_name_snapshot}\n"
        f"Creator id: {request_record.creator_id}\n"
        f"Requested at UTC: {requested_at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
    )


def support_request_status_display(request_record: SupportRequestRecord) -> dict[str, str]:
    if request_record.status == SUPPORT_REQUEST_STATUS_PENDING:
        return {
            "label": "Pending review",
            "badge_class": "pending",
            "body": (
                "Your request is recorded and waiting for manual beta review. "
                "No destructive changes have been applied yet."
            ),
        }
    if request_record.status == SUPPORT_REQUEST_STATUS_NOTIFICATION_FAILED:
        return {
            "label": "Notification failed",
            "badge_class": "disconnected",
            "body": (
                "Your request is saved, but support-email delivery failed. "
                "The request remains recorded for follow-up."
            ),
        }
    return {
        "label": "Pending notification",
        "badge_class": "pending",
        "body": (
            "Your request is saved locally and is still waiting for support-notification delivery."
        ),
    }


def support_request_public_id(request_record: SupportRequestRecord) -> str:
    return str(uuid.UUID(str(request_record.id)))
