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

SUPPORT_REQUEST_STATUS_SUBMITTED = "submitted"
SUPPORT_REQUEST_STATUS_IN_REVIEW = "in_review"
SUPPORT_REQUEST_STATUS_COMPLETED = "completed"
SUPPORT_REQUEST_STATUS_REJECTED = "rejected"
SUPPORT_REQUEST_STATUS_CANCELED = "canceled"
SUPPORT_REQUEST_ACTIVE_STATUSES = frozenset(
    {
        SUPPORT_REQUEST_STATUS_SUBMITTED,
        SUPPORT_REQUEST_STATUS_IN_REVIEW,
    }
)
SUPPORT_REQUEST_TERMINAL_STATUSES = frozenset(
    {
        SUPPORT_REQUEST_STATUS_COMPLETED,
        SUPPORT_REQUEST_STATUS_REJECTED,
        SUPPORT_REQUEST_STATUS_CANCELED,
    }
)
SUPPORT_REQUEST_ALLOWED_TRANSITIONS = {
    SUPPORT_REQUEST_STATUS_SUBMITTED: frozenset(
        {
            SUPPORT_REQUEST_STATUS_IN_REVIEW,
            SUPPORT_REQUEST_STATUS_COMPLETED,
            SUPPORT_REQUEST_STATUS_REJECTED,
            SUPPORT_REQUEST_STATUS_CANCELED,
        }
    ),
    SUPPORT_REQUEST_STATUS_IN_REVIEW: frozenset(
        {
            SUPPORT_REQUEST_STATUS_COMPLETED,
            SUPPORT_REQUEST_STATUS_REJECTED,
            SUPPORT_REQUEST_STATUS_CANCELED,
        }
    ),
    SUPPORT_REQUEST_STATUS_COMPLETED: frozenset(),
    SUPPORT_REQUEST_STATUS_REJECTED: frozenset(),
    SUPPORT_REQUEST_STATUS_CANCELED: frozenset(),
}


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
        status=SUPPORT_REQUEST_STATUS_SUBMITTED,
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


def list_latest_support_requests_for_creator(
    db: Session,
    *,
    creator_id,
) -> dict[str, SupportRequestRecord]:
    rows = (
        db.execute(
            select(SupportRequestRecord)
            .where(SupportRequestRecord.creator_id == creator_id)
            .order_by(SupportRequestRecord.created_at.desc(), SupportRequestRecord.id.desc())
        )
        .scalars()
        .all()
    )

    latest_rows: dict[str, SupportRequestRecord] = {}
    for row in rows:
        latest_rows.setdefault(row.request_type, row)
    return latest_rows


def list_support_requests_for_operator(db: Session) -> list[SupportRequestRecord]:
    return (
        db.execute(
            select(SupportRequestRecord).order_by(
                SupportRequestRecord.closed_at.is_not(None),
                SupportRequestRecord.created_at.desc(),
                SupportRequestRecord.id.desc(),
            )
        )
        .scalars()
        .all()
    )


def get_support_request_by_id(
    db: Session,
    *,
    request_id,
) -> SupportRequestRecord | None:
    return db.get(SupportRequestRecord, request_id)


def mark_support_request_notification_succeeded(
    db: Session,
    *,
    request_record: SupportRequestRecord,
    attempted_at: datetime | None = None,
) -> SupportRequestRecord:
    timestamp = attempted_at or datetime.now(timezone.utc)
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
    request_record.notification_attempted_at = timestamp
    request_record.notification_sent_at = None
    request_record.notification_failed_at = timestamp
    db.add(request_record)
    db.commit()
    db.refresh(request_record)
    return request_record


def transition_support_request_status(
    db: Session,
    *,
    request_record: SupportRequestRecord,
    new_status: str,
    transitioned_at: datetime | None = None,
) -> SupportRequestRecord:
    normalized_status = new_status.strip().lower()
    current_status = request_record.status.strip().lower()

    if normalized_status == current_status:
        return request_record

    allowed_transitions = SUPPORT_REQUEST_ALLOWED_TRANSITIONS.get(current_status)
    if allowed_transitions is None or normalized_status not in allowed_transitions:
        raise ValueError(
            f"invalid support request transition from {request_record.status!r} to {new_status!r}"
        )

    request_record.status = normalized_status
    request_record.closed_at = (
        (transitioned_at or datetime.now(timezone.utc))
        if normalized_status in SUPPORT_REQUEST_TERMINAL_STATUSES
        else None
    )
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
    return f"{support_request_type_label(request_type)} request for {requester_email}"


def _support_request_body(*, request_record: SupportRequestRecord) -> str:
    request_label = support_request_type_label(request_record.request_type)
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


def support_request_type_label(request_type: str) -> str:
    if request_type == SUPPORT_REQUEST_TYPE_WORKSPACE_RESET:
        return "Workspace reset"
    if request_type == SUPPORT_REQUEST_TYPE_ACCOUNT_DELETION:
        return "Account deletion"
    return request_type.replace("-", " ").title()


def support_request_status_display(request_record: SupportRequestRecord) -> dict[str, str]:
    if request_record.status == SUPPORT_REQUEST_STATUS_SUBMITTED:
        return {
            "label": "Submitted",
            "badge_class": "pending",
            "body": (
                "Your request is recorded and waiting for manual beta review. "
                "No destructive changes have been applied yet."
            ),
        }
    if request_record.status == SUPPORT_REQUEST_STATUS_IN_REVIEW:
        return {
            "label": "In review",
            "badge_class": "pending",
            "body": (
                "An operator is reviewing this request manually. "
                "No destructive changes are completed in-app."
            ),
        }
    if request_record.status == SUPPORT_REQUEST_STATUS_COMPLETED:
        return {
            "label": "Completed",
            "badge_class": "created",
            "body": (
                "This request was marked complete after manual operator handling. "
                "Any destructive work remains outside the app."
            ),
        }
    if request_record.status == SUPPORT_REQUEST_STATUS_REJECTED:
        return {
            "label": "Rejected",
            "badge_class": "rejected",
            "body": (
                "This request was reviewed and rejected. "
                "No destructive changes were applied in-app."
            ),
        }
    if request_record.status == SUPPORT_REQUEST_STATUS_CANCELED:
        return {
            "label": "Canceled",
            "badge_class": "canceled",
            "body": (
                "This request was canceled before destructive work was completed. "
                "No in-app destructive changes were applied."
            ),
        }
    return {
        "label": request_record.status.replace("_", " ").title(),
        "badge_class": "pending",
        "body": "This request is saved with an unrecognized review state.",
    }


def support_request_notification_state_display(request_record: SupportRequestRecord) -> dict[str, str]:
    if request_record.notification_sent_at is not None:
        sent_at = request_record.notification_sent_at.astimezone(timezone.utc).strftime(
            "%B %d, %Y at %H:%M UTC"
        )
        return {
            "label": "Delivered",
            "badge_class": "connected",
            "body": f"Support email notification succeeded on {sent_at}.",
        }
    if request_record.notification_failed_at is not None:
        failed_at = request_record.notification_failed_at.astimezone(timezone.utc).strftime(
            "%B %d, %Y at %H:%M UTC"
        )
        return {
            "label": "Failed",
            "badge_class": "disconnected",
            "body": f"Support email notification failed on {failed_at}, but the request is still saved.",
        }
    return {
        "label": "Pending",
        "badge_class": "pending",
        "body": "Support email delivery has not finished yet.",
    }


def support_request_available_transitions(request_record: SupportRequestRecord) -> tuple[str, ...]:
    return tuple(
        sorted(
            SUPPORT_REQUEST_ALLOWED_TRANSITIONS.get(request_record.status, frozenset()),
            key=_support_request_status_sort_key,
        )
    )


def support_request_status_label(status: str) -> str:
    return status.replace("_", " ").title()


def support_request_public_id(request_record: SupportRequestRecord) -> str:
    return str(uuid.UUID(str(request_record.id)))


def _support_request_status_sort_key(status: str) -> int:
    order = {
        SUPPORT_REQUEST_STATUS_IN_REVIEW: 0,
        SUPPORT_REQUEST_STATUS_COMPLETED: 1,
        SUPPORT_REQUEST_STATUS_REJECTED: 2,
        SUPPORT_REQUEST_STATUS_CANCELED: 3,
    }
    return order.get(status, 99)
