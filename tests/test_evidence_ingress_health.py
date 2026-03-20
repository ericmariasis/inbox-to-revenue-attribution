import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.main import app
from app.models.auth_user import AuthUser
from app.models.blocked_billing_case import BlockedBillingCase
from app.models.booking import Booking
from app.models.booking_link import BookingLink
from app.models.calendly_webhook_event import CalendlyWebhookEventRecord
from app.models.content import Content
from app.models.content_extraction_artifact import ContentExtractionArtifact
from app.models.content_fetch_snapshot import ContentFetchSnapshot
from app.models.content_topic_candidate import ContentTopicCandidate
from app.models.creator import Creator
from app.models.fullscope_webhook_event import FullScopeWebhookEventRecord
from app.models.invoice import Invoice
from app.models.invoice_payment_event import InvoicePaymentEvent
from app.services.blocked_billing import BLOCKED_BILLING_REASON_CREATOR_NOT_BILLABLE
from app.services.booking_attribution import (
    BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED,
    BOOKING_UNATTRIBUTED_REASON_MISSING_TID,
)
from app.services.content_topics import (
    CONTENT_TOPIC_REVIEW_STATUS_CONFIRMED,
    CONTENT_TOPIC_REVIEW_STATUS_PENDING,
    CONTENT_TOPIC_REVIEW_STATUS_REJECTED,
)
from app.services.evidence_ingress_health import (
    AUTHORITATIVE_CONTENT_LAG_REASON_MISSING_AUTHORITY,
    AUTHORITATIVE_CONTENT_LAG_REASON_STALE_AUTHORITY,
    get_creator_evidence_ingress_health_snapshot,
)
from app.services.invoice_payment_events import (
    PAYMENT_PROVENANCE_STATE_CONFLICTING,
    PAYMENT_PROVENANCE_STATE_MATCHED,
    PAYMENT_PROVENANCE_STATE_PENDING,
    PAYMENT_PROVENANCE_STATE_UNMATCHED,
    UNATTRIBUTED_REASON_MISSING_TID,
    UNATTRIBUTED_REASON_UNKNOWN_BOOKING_UUID,
    UNATTRIBUTED_REASON_UNKNOWN_STRIPE_INVOICE_ID,
)


def _engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def _access_token(*, user_id: str, creator_id: str, email: str, expires_delta: timedelta) -> str:
    settings = get_settings()
    issued_at = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "creator_id": creator_id,
        "email": email,
        "iat": issued_at,
        "exp": issued_at + expires_delta,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def _create_creator_with_user(
    session: Session,
    *,
    suffix: str,
    stripe_account_id: str,
) -> tuple[Creator, AuthUser]:
    creator = Creator(
        name=f"Health Creator {suffix}",
        stripe_connect_status="connected",
        stripe_account_id=stripe_account_id,
    )
    session.add(creator)
    session.flush()

    user = AuthUser(
        creator_id=creator.id,
        email=f"health_{suffix}@example.com",
    )
    session.add(user)
    session.flush()
    return creator, user


def _create_booking_link(
    session: Session,
    *,
    creator: Creator,
    suffix: str,
    provider: str = "calendly",
    destination_url: str | None = None,
) -> BookingLink:
    resolved_destination_url = destination_url or f"https://calendly.com/example/health-{suffix}"
    booking_link = BookingLink(
        creator_id=creator.id,
        name=f"Health Link {suffix}",
        provider=provider,
        destination_url=resolved_destination_url,
        calendly_url=resolved_destination_url if provider == "calendly" else None,
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    session.add(booking_link)
    session.flush()
    return booking_link


def _create_content(
    session: Session,
    *,
    creator: Creator,
    booking_link: BookingLink,
    suffix: str,
) -> Content:
    content = Content(
        creator_id=creator.id,
        booking_link_id=booking_link.id,
        source_url=f"https://example.com/posts/health-{suffix}",
        tid=f"health_tid_{suffix}",
    )
    session.add(content)
    session.flush()
    return content


def _create_booking(
    session: Session,
    *,
    creator: Creator,
    booking_link: BookingLink,
    content: Content | None,
    booking_uuid: str,
    booked_at: datetime,
    provider: str = "calendly",
    provider_booking_id: str | None = None,
    attribution_status: str = "attributed",
    unattributed_reason: str | None = None,
) -> Booking:
    resolved_provider_booking_id = provider_booking_id or booking_uuid
    booking = Booking(
        creator_id=creator.id,
        booking_link_id=booking_link.id,
        tid=content.tid if content is not None else None,
        provider=provider,
        provider_booking_id=resolved_provider_booking_id,
        calendly_booking_uuid=booking_uuid if provider == "calendly" else None,
        email=f"{booking_uuid.lower()}@example.com",
        status="created",
        attribution_status=attribution_status,
        unattributed_reason=unattributed_reason,
        booked_at=booked_at,
    )
    session.add(booking)
    session.flush()
    return booking


def _create_paid_invoice(
    session: Session,
    *,
    creator: Creator,
    booking: Booking,
    stripe_invoice_id: str,
    amount_cents: int,
    paid_at: datetime,
) -> Invoice:
    invoice = Invoice(
        creator_id=creator.id,
        booking_id=booking.id,
        tid=booking.tid,
        stripe_account_id=creator.stripe_account_id,
        stripe_invoice_id=stripe_invoice_id,
        amount_cents=amount_cents,
        currency="USD",
        status="paid",
        issued_at=paid_at - timedelta(hours=1),
        paid_at=paid_at,
    )
    session.add(invoice)
    session.flush()
    return invoice


def _create_matched_payment_event(
    session: Session,
    *,
    creator: Creator,
    booking: Booking,
    invoice: Invoice,
    stripe_event_id: str,
    paid_at: datetime,
) -> InvoicePaymentEvent:
    event = InvoicePaymentEvent(
        stripe_event_id=stripe_event_id,
        stripe_event_type="invoice.paid",
        stripe_account_id=creator.stripe_account_id,
        stripe_invoice_id=invoice.stripe_invoice_id,
        invoice_id=invoice.id,
        creator_id=creator.id,
        booking_id=booking.id,
        tid=booking.tid,
        status="applied",
        unattributed_reason=None,
        paid_at=paid_at,
        received_at=paid_at,
        processed_at=paid_at,
    )
    session.add(event)
    session.flush()
    return event


def _create_unmatched_payment_event(
    session: Session,
    *,
    creator: Creator,
    stripe_event_id: str,
    stripe_invoice_id: str,
    reason: str,
    paid_at: datetime,
) -> InvoicePaymentEvent:
    event = InvoicePaymentEvent(
        stripe_event_id=stripe_event_id,
        stripe_event_type="invoice.paid",
        stripe_account_id=creator.stripe_account_id,
        stripe_invoice_id=stripe_invoice_id,
        invoice_id=None,
        creator_id=creator.id,
        booking_id=None,
        tid=None,
        status="unmatched",
        unattributed_reason=reason,
        paid_at=paid_at,
        received_at=paid_at,
        processed_at=None,
    )
    session.add(event)
    session.flush()
    return event


def _create_blocked_billing_case(
    session: Session,
    *,
    creator: Creator,
    booking: Booking,
    blocked_at: datetime,
) -> BlockedBillingCase:
    blocked_case = BlockedBillingCase(
        creator_id=creator.id,
        booking_id=booking.id,
        invoice_id=None,
        tid=booking.tid,
        calendly_booking_uuid=booking.calendly_booking_uuid,
        stripe_account_id=creator.stripe_account_id,
        frozen_amount_cents=19500,
        frozen_currency="USD",
        status="open",
        reason_code=BLOCKED_BILLING_REASON_CREATOR_NOT_BILLABLE,
        provider_operation=None,
        provider_http_status=None,
        provider_error_code=None,
        first_blocked_at=blocked_at,
        last_blocked_at=blocked_at,
        last_retry_at=None,
        resolved_at=None,
        resolution_code=None,
    )
    session.add(blocked_case)
    session.flush()
    return blocked_case


def _create_calendly_event_record(
    session: Session,
    *,
    event_id: str,
    booking_uuid: str,
    tid: str | None,
    processing_status: str,
) -> CalendlyWebhookEventRecord:
    record = CalendlyWebhookEventRecord(
        calendly_event_id=event_id,
        provider_event_type="invitee.created",
        event_type="booking.created",
        calendly_event_id_path="payload.event",
        calendly_booking_uuid=booking_uuid,
        calendly_booking_uuid_path="payload.uri",
        tid=tid,
        tid_path="payload.tracking.utm_content" if tid is not None else None,
        payload={"event": "invitee.created", "payload": {"tracking": {"utm_content": tid}}},
        reducer_key=f"booking:{booking_uuid}",
        delivery_count=1,
        processing_status=processing_status,
        reducer_attempt_count=1 if processing_status != "received" else 0,
        last_error=(
            "RuntimeError: health test reducer failure"
            if processing_status == "failed"
            else None
        ),
        received_at=datetime(2026, 3, 12, 10, 0, tzinfo=timezone.utc),
        last_received_at=datetime(2026, 3, 12, 10, 0, tzinfo=timezone.utc),
        processed_at=(
            None
            if processing_status == "received"
            else datetime(2026, 3, 12, 10, 5, tzinfo=timezone.utc)
        ),
    )
    session.add(record)
    session.flush()
    return record


def _create_fullscope_event_record(
    session: Session,
    *,
    event_id: str,
    appointment_id: str,
    tid: str | None,
    processing_status: str,
) -> FullScopeWebhookEventRecord:
    record = FullScopeWebhookEventRecord(
        provider_event_type="appointment.created",
        event_type="booking.created",
        appointment_id=appointment_id,
        appointment_id_path="payload.appointment.id",
        calendar_id="calendar_health",
        calendar_id_path="payload.calendar.id",
        workflow_id=None,
        workflow_id_path=None,
        tid=tid,
        tid_path="payload.metadata.ccp_attribution_tid" if tid is not None else None,
        payload={
            "event": "appointment.created",
            "payload": {
                "appointment": {"id": appointment_id},
                "metadata": {"ccp_attribution_tid": tid},
            },
        },
        payload_sha256=f"health-{uuid.uuid4().hex}",
        reducer_key=f"booking:{appointment_id}",
        delivery_count=1,
        processing_status=processing_status,
        reducer_attempt_count=1 if processing_status != "received" else 0,
        last_error=(
            "RuntimeError: health test reducer failure"
            if processing_status == "failed"
            else None
        ),
        received_at=datetime(2026, 3, 12, 10, 10, tzinfo=timezone.utc),
        last_received_at=datetime(2026, 3, 12, 10, 10, tzinfo=timezone.utc),
        processed_at=(
            None
            if processing_status == "received"
            else datetime(2026, 3, 12, 10, 15, tzinfo=timezone.utc)
        ),
    )
    session.add(record)
    session.flush()
    return record


def _create_fetch_snapshot(
    session: Session,
    *,
    creator: Creator,
    content: Content,
    suffix: str,
    fetched_at: datetime,
) -> ContentFetchSnapshot:
    snapshot = ContentFetchSnapshot(
        creator_id=creator.id,
        content_id=content.id,
        requested_url=f"{content.source_url}?snapshot={suffix}",
        fetched_url=f"{content.source_url}?snapshot={suffix}",
        fetch_status="succeeded",
        http_status=200,
        response_content_type="text/html",
        response_content_charset="utf-8",
        snapshot_text=f"Snapshot text {suffix}",
        fetched_at=fetched_at,
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def _create_extraction_artifact(
    session: Session,
    *,
    creator: Creator,
    content: Content,
    fetch_snapshot: ContentFetchSnapshot,
    suffix: str,
    created_at: datetime,
) -> ContentExtractionArtifact:
    artifact = ContentExtractionArtifact(
        creator_id=creator.id,
        content_id=content.id,
        fetch_snapshot_id=fetch_snapshot.id,
        extraction_status="succeeded",
        extraction_reason_code=None,
        extraction_detail=None,
        extraction_method="test",
        title=f"Health Artifact {suffix}",
        published_at=None,
        published_at_raw=None,
        source_text_char_count=100,
        extracted_text_char_count=100,
        extracted_text_word_count=20,
        extracted_text=f"Extracted text {suffix}",
        created_at=created_at,
    )
    session.add(artifact)
    session.flush()
    return artifact


def _create_topic_candidate(
    session: Session,
    *,
    creator: Creator,
    content: Content,
    artifact: ContentExtractionArtifact,
    suffix: str,
    review_status: str,
    candidate_rank: int,
) -> ContentTopicCandidate:
    candidate = ContentTopicCandidate(
        creator_id=creator.id,
        content_id=content.id,
        extraction_artifact_id=artifact.id,
        confirmed_topic_id=None,
        suggested_label=f"Health Topic {suffix}",
        normalized_label=f"health-topic-{suffix}",
        suggestion_method="test",
        candidate_rank=candidate_rank,
        review_status=review_status,
        reviewed_at=(
            None
            if review_status == CONTENT_TOPIC_REVIEW_STATUS_PENDING
            else datetime(2026, 3, 12, 12, 0, tzinfo=timezone.utc)
        ),
    )
    session.add(candidate)
    session.flush()
    return candidate


def test_creator_evidence_ingress_health_snapshot_surfaces_degraded_current_state():
    engine = _engine()

    with Session(engine) as session:
        creator, _ = _create_creator_with_user(
            session,
            suffix="service",
            stripe_account_id="acct_health_service",
        )
        other_creator, _ = _create_creator_with_user(
            session,
            suffix="service_other",
            stripe_account_id="acct_health_service_other",
        )
        booking_link = _create_booking_link(session, creator=creator, suffix="service")
        other_booking_link = _create_booking_link(
            session,
            creator=other_creator,
            suffix="service_other",
        )

        pending_content = _create_content(
            session,
            creator=creator,
            booking_link=booking_link,
            suffix="pending",
        )
        pending_booking = _create_booking(
            session,
            creator=creator,
            booking_link=booking_link,
            content=pending_content,
            booking_uuid="BOOK_HEALTH_PENDING",
            booked_at=datetime(2026, 3, 12, 8, 0, tzinfo=timezone.utc),
        )
        _create_paid_invoice(
            session,
            creator=creator,
            booking=pending_booking,
            stripe_invoice_id="in_health_pending",
            amount_cents=19500,
            paid_at=datetime(2026, 3, 12, 9, 0, tzinfo=timezone.utc),
        )

        unmatched_content = _create_content(
            session,
            creator=creator,
            booking_link=booking_link,
            suffix="unmatched",
        )
        unmatched_booking = _create_booking(
            session,
            creator=creator,
            booking_link=booking_link,
            content=unmatched_content,
            booking_uuid="BOOK_HEALTH_UNMATCHED",
            booked_at=datetime(2026, 3, 12, 9, 30, tzinfo=timezone.utc),
        )
        unmatched_invoice = _create_paid_invoice(
            session,
            creator=creator,
            booking=unmatched_booking,
            stripe_invoice_id="in_health_unmatched",
            amount_cents=20500,
            paid_at=datetime(2026, 3, 12, 10, 0, tzinfo=timezone.utc),
        )
        _create_unmatched_payment_event(
            session,
            creator=creator,
            stripe_event_id="evt_health_unmatched",
            stripe_invoice_id=unmatched_invoice.stripe_invoice_id,
            reason=UNATTRIBUTED_REASON_UNKNOWN_STRIPE_INVOICE_ID,
            paid_at=unmatched_invoice.paid_at,
        )

        conflicting_content = _create_content(
            session,
            creator=creator,
            booking_link=booking_link,
            suffix="conflicting",
        )
        conflicting_booking = _create_booking(
            session,
            creator=creator,
            booking_link=booking_link,
            content=conflicting_content,
            booking_uuid="BOOK_HEALTH_CONFLICTING",
            booked_at=datetime(2026, 3, 12, 10, 30, tzinfo=timezone.utc),
        )
        conflicting_invoice = _create_paid_invoice(
            session,
            creator=creator,
            booking=conflicting_booking,
            stripe_invoice_id="in_health_conflicting",
            amount_cents=21500,
            paid_at=datetime(2026, 3, 12, 11, 0, tzinfo=timezone.utc),
        )
        _create_matched_payment_event(
            session,
            creator=creator,
            booking=conflicting_booking,
            invoice=conflicting_invoice,
            stripe_event_id="evt_health_conflicting_applied",
            paid_at=conflicting_invoice.paid_at,
        )
        _create_unmatched_payment_event(
            session,
            creator=creator,
            stripe_event_id="evt_health_conflicting_unmatched",
            stripe_invoice_id=conflicting_invoice.stripe_invoice_id,
            reason=UNATTRIBUTED_REASON_UNKNOWN_BOOKING_UUID,
            paid_at=conflicting_invoice.paid_at,
        )

        blocked_content = _create_content(
            session,
            creator=creator,
            booking_link=booking_link,
            suffix="blocked",
        )
        blocked_booking = _create_booking(
            session,
            creator=creator,
            booking_link=booking_link,
            content=blocked_content,
            booking_uuid="BOOK_HEALTH_BLOCKED",
            booked_at=datetime(2026, 3, 12, 11, 30, tzinfo=timezone.utc),
        )
        _create_blocked_billing_case(
            session,
            creator=creator,
            booking=blocked_booking,
            blocked_at=datetime(2026, 3, 12, 11, 35, tzinfo=timezone.utc),
        )

        _create_booking(
            session,
            creator=creator,
            booking_link=booking_link,
            content=None,
            booking_uuid="BOOK_HEALTH_UNATTRIBUTED",
            booked_at=datetime(2026, 3, 12, 11, 45, tzinfo=timezone.utc),
            attribution_status=BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED,
            unattributed_reason=BOOKING_UNATTRIBUTED_REASON_MISSING_TID,
        )

        _create_calendly_event_record(
            session,
            event_id="EVT_HEALTH_RECEIVED",
            booking_uuid="BOOK_HEALTH_RECEIVED",
            tid=pending_content.tid,
            processing_status="received",
        )
        _create_calendly_event_record(
            session,
            event_id="EVT_HEALTH_DEFERRED",
            booking_uuid="BOOK_HEALTH_DEFERRED",
            tid=pending_content.tid,
            processing_status="deferred_missing_booking",
        )
        _create_calendly_event_record(
            session,
            event_id="EVT_HEALTH_FAILED",
            booking_uuid="BOOK_HEALTH_FAILED",
            tid=conflicting_content.tid,
            processing_status="failed",
        )
        fullscope_booking_link = _create_booking_link(
            session,
            creator=creator,
            suffix="fullscope",
            provider="fullscope",
            destination_url="https://links.fullscope.tools/widget/bookings/health-fullscope",
        )
        fullscope_content = _create_content(
            session,
            creator=creator,
            booking_link=fullscope_booking_link,
            suffix="fullscope",
        )
        fullscope_booking = _create_booking(
            session,
            creator=creator,
            booking_link=fullscope_booking_link,
            content=fullscope_content,
            booking_uuid="BOOK_HEALTH_FULLSCOPE",
            provider="fullscope",
            provider_booking_id="FS_BOOK_HEALTH_FULLSCOPE",
            booked_at=datetime(2026, 3, 12, 10, 45, tzinfo=timezone.utc),
        )
        _create_fullscope_event_record(
            session,
            event_id="EVT_HEALTH_FULLSCOPE_RECEIVED",
            appointment_id="FS_APP_HEALTH_RECEIVED",
            tid=pending_content.tid,
            processing_status="received",
        )
        _create_fullscope_event_record(
            session,
            event_id="EVT_HEALTH_FULLSCOPE_DEFERRED",
            appointment_id="FS_APP_HEALTH_DEFERRED",
            tid=pending_content.tid,
            processing_status="deferred_missing_booking",
        )
        _create_fullscope_event_record(
            session,
            event_id="EVT_HEALTH_FULLSCOPE_FAILED",
            appointment_id=fullscope_booking.resolved_provider_booking_id,
            tid=None,
            processing_status="failed",
        )

        old_snapshot = _create_fetch_snapshot(
            session,
            creator=creator,
            content=blocked_content,
            suffix="old",
            fetched_at=datetime(2026, 3, 12, 7, 0, tzinfo=timezone.utc),
        )
        old_artifact = _create_extraction_artifact(
            session,
            creator=creator,
            content=blocked_content,
            fetch_snapshot=old_snapshot,
            suffix="old",
            created_at=datetime(2026, 3, 12, 7, 5, tzinfo=timezone.utc),
        )
        _create_topic_candidate(
            session,
            creator=creator,
            content=blocked_content,
            artifact=old_artifact,
            suffix="old-confirmed",
            review_status=CONTENT_TOPIC_REVIEW_STATUS_CONFIRMED,
            candidate_rank=1,
        )
        blocked_content.authoritative_extraction_artifact_id = old_artifact.id

        latest_snapshot = _create_fetch_snapshot(
            session,
            creator=creator,
            content=blocked_content,
            suffix="latest",
            fetched_at=datetime(2026, 3, 12, 8, 0, tzinfo=timezone.utc),
        )
        latest_artifact = _create_extraction_artifact(
            session,
            creator=creator,
            content=blocked_content,
            fetch_snapshot=latest_snapshot,
            suffix="latest",
            created_at=datetime(2026, 3, 12, 8, 5, tzinfo=timezone.utc),
        )
        _create_topic_candidate(
            session,
            creator=creator,
            content=blocked_content,
            artifact=latest_artifact,
            suffix="latest-rejected",
            review_status=CONTENT_TOPIC_REVIEW_STATUS_REJECTED,
            candidate_rank=1,
        )

        missing_authority_content = _create_content(
            session,
            creator=creator,
            booking_link=booking_link,
            suffix="missing_authority",
        )
        missing_authority_snapshot = _create_fetch_snapshot(
            session,
            creator=creator,
            content=missing_authority_content,
            suffix="missing-authority",
            fetched_at=datetime(2026, 3, 12, 8, 30, tzinfo=timezone.utc),
        )
        missing_authority_artifact = _create_extraction_artifact(
            session,
            creator=creator,
            content=missing_authority_content,
            fetch_snapshot=missing_authority_snapshot,
            suffix="missing-authority",
            created_at=datetime(2026, 3, 12, 8, 35, tzinfo=timezone.utc),
        )
        _create_topic_candidate(
            session,
            creator=creator,
            content=missing_authority_content,
            artifact=missing_authority_artifact,
            suffix="missing-authority-confirmed",
            review_status=CONTENT_TOPIC_REVIEW_STATUS_CONFIRMED,
            candidate_rank=1,
        )

        ready_content = _create_content(
            session,
            creator=creator,
            booking_link=booking_link,
            suffix="authoritative_ready",
        )
        ready_snapshot = _create_fetch_snapshot(
            session,
            creator=creator,
            content=ready_content,
            suffix="ready",
            fetched_at=datetime(2026, 3, 12, 9, 0, tzinfo=timezone.utc),
        )
        ready_artifact = _create_extraction_artifact(
            session,
            creator=creator,
            content=ready_content,
            fetch_snapshot=ready_snapshot,
            suffix="ready",
            created_at=datetime(2026, 3, 12, 9, 5, tzinfo=timezone.utc),
        )
        _create_topic_candidate(
            session,
            creator=creator,
            content=ready_content,
            artifact=ready_artifact,
            suffix="ready-confirmed",
            review_status=CONTENT_TOPIC_REVIEW_STATUS_CONFIRMED,
            candidate_rank=1,
        )
        ready_content.authoritative_extraction_artifact_id = ready_artifact.id

        pending_review_content = _create_content(
            session,
            creator=creator,
            booking_link=booking_link,
            suffix="pending_review",
        )
        pending_review_snapshot = _create_fetch_snapshot(
            session,
            creator=creator,
            content=pending_review_content,
            suffix="pending-review",
            fetched_at=datetime(2026, 3, 12, 9, 30, tzinfo=timezone.utc),
        )
        pending_review_artifact = _create_extraction_artifact(
            session,
            creator=creator,
            content=pending_review_content,
            fetch_snapshot=pending_review_snapshot,
            suffix="pending-review",
            created_at=datetime(2026, 3, 12, 9, 35, tzinfo=timezone.utc),
        )
        _create_topic_candidate(
            session,
            creator=creator,
            content=pending_review_content,
            artifact=pending_review_artifact,
            suffix="pending-review-pending",
            review_status=CONTENT_TOPIC_REVIEW_STATUS_PENDING,
            candidate_rank=1,
        )

        other_content = _create_content(
            session,
            creator=other_creator,
            booking_link=other_booking_link,
            suffix="other",
        )
        other_booking = _create_booking(
            session,
            creator=other_creator,
            booking_link=other_booking_link,
            content=other_content,
            booking_uuid="BOOK_HEALTH_OTHER",
            booked_at=datetime(2026, 3, 12, 12, 0, tzinfo=timezone.utc),
        )
        other_invoice = _create_paid_invoice(
            session,
            creator=other_creator,
            booking=other_booking,
            stripe_invoice_id="in_health_other",
            amount_cents=5000,
            paid_at=datetime(2026, 3, 12, 12, 30, tzinfo=timezone.utc),
        )
        _create_matched_payment_event(
            session,
            creator=other_creator,
            booking=other_booking,
            invoice=other_invoice,
            stripe_event_id="evt_health_other",
            paid_at=other_invoice.paid_at,
        )
        _create_calendly_event_record(
            session,
            event_id="EVT_HEALTH_OTHER_FAILED",
            booking_uuid="BOOK_HEALTH_OTHER_FAILED",
            tid=other_content.tid,
            processing_status="failed",
        )

        creator_id = creator.id
        session.commit()

    with Session(engine) as session:
        snapshot = get_creator_evidence_ingress_health_snapshot(
            creator_id=creator_id,
            db=session,
        )

    assert snapshot.creator_id == creator_id
    assert snapshot.booking_attribution.unattributed_booking_count == 1
    assert [(item.reason, item.booking_count) for item in snapshot.booking_attribution.reasons] == [
        (BOOKING_UNATTRIBUTED_REASON_MISSING_TID, 1),
        ("UNKNOWN_TID", 0),
    ]

    assert snapshot.calendly_ingress.backlog_event_count == 2
    assert snapshot.calendly_ingress.failed_event_count == 1
    assert [
        (item.processing_status, item.event_count)
        for item in snapshot.calendly_ingress.statuses
    ] == [
        ("received", 1),
        ("processing", 0),
        ("deferred_missing_booking", 1),
        ("failed", 1),
    ]
    assert snapshot.fullscope_ingress.backlog_event_count == 2
    assert snapshot.fullscope_ingress.failed_event_count == 1
    assert [
        (item.processing_status, item.event_count)
        for item in snapshot.fullscope_ingress.statuses
    ] == [
        ("received", 1),
        ("processing", 0),
        ("deferred_missing_booking", 1),
        ("failed", 1),
    ]

    assert [
        (item.state, item.row_count)
        for item in snapshot.payment_provenance.settled_state_counts
    ] == [
        (PAYMENT_PROVENANCE_STATE_MATCHED, 0),
        (PAYMENT_PROVENANCE_STATE_PENDING, 1),
        (PAYMENT_PROVENANCE_STATE_UNMATCHED, 1),
        (PAYMENT_PROVENANCE_STATE_CONFLICTING, 1),
    ]
    assert snapshot.payment_provenance.current_backlog_event_count == 2
    assert [
        (item.reason, item.event_count)
        for item in snapshot.payment_provenance.current_backlog_reasons
    ] == [
        (UNATTRIBUTED_REASON_UNKNOWN_BOOKING_UUID, 1),
        (UNATTRIBUTED_REASON_UNKNOWN_STRIPE_INVOICE_ID, 1),
    ]

    assert snapshot.blocked_billing.open_case_count == 1
    assert [(item.reason_code, item.case_count) for item in snapshot.blocked_billing.reasons] == [
        (BLOCKED_BILLING_REASON_CREATOR_NOT_BILLABLE, 1),
    ]

    assert snapshot.authoritative_content.lagging_content_count == 2
    assert [
        (item.reason, item.content_count)
        for item in snapshot.authoritative_content.reasons
    ] == [
        (AUTHORITATIVE_CONTENT_LAG_REASON_MISSING_AUTHORITY, 1),
        (AUTHORITATIVE_CONTENT_LAG_REASON_STALE_AUTHORITY, 1),
    ]


def test_creator_evidence_ingress_health_snapshot_groups_payment_provenance_by_provider():
    engine = _engine()

    with Session(engine) as session:
        creator = Creator(
            name="Health PayPal Creator",
            billing_provider="paypal",
            billing_connect_status="connected",
            billing_account_id="merchant_health_paypal",
        )
        session.add(creator)
        session.flush()

        booking_link = _create_booking_link(session, creator=creator, suffix="paypal")
        content = _create_content(
            session,
            creator=creator,
            booking_link=booking_link,
            suffix="paypal",
        )
        booking = _create_booking(
            session,
            creator=creator,
            booking_link=booking_link,
            content=content,
            booking_uuid="BOOK_HEALTH_PAYPAL",
            booked_at=datetime(2026, 3, 13, 8, 0, tzinfo=timezone.utc),
        )
        invoice = Invoice(
            creator_id=creator.id,
            booking_id=booking.id,
            tid=booking.tid,
            payment_provider="paypal",
            provider_account_id="merchant_health_paypal",
            provider_invoice_id="INV_HEALTH_PAYPAL",
            amount_cents=19500,
            currency="USD",
            status="paid",
            issued_at=datetime(2026, 3, 13, 8, 30, tzinfo=timezone.utc),
            paid_at=datetime(2026, 3, 13, 9, 0, tzinfo=timezone.utc),
        )
        session.add(invoice)
        session.flush()

        session.add(
            InvoicePaymentEvent(
                payment_provider="paypal",
                provider_event_id="WH_HEALTH_PAYPAL",
                provider_event_type="INVOICING.INVOICE.PAID",
                provider_account_id="merchant_health_paypal",
                provider_invoice_id="INV_HEALTH_PAYPAL",
                invoice_id=invoice.id,
                creator_id=creator.id,
                booking_id=booking.id,
                tid=booking.tid,
                status="applied",
                paid_at=invoice.paid_at,
                received_at=invoice.paid_at,
                processed_at=invoice.paid_at,
            )
        )
        session.add(
            InvoicePaymentEvent(
                payment_provider="paypal",
                provider_event_id="WH_HEALTH_PAYPAL_UNMATCHED",
                provider_event_type="INVOICING.INVOICE.PAID",
                provider_account_id="merchant_health_paypal",
                provider_invoice_id="INV_HEALTH_PAYPAL_UNMATCHED",
                invoice_id=None,
                creator_id=creator.id,
                booking_id=None,
                tid=None,
                status="unmatched",
                unattributed_reason=UNATTRIBUTED_REASON_UNKNOWN_BOOKING_UUID,
                paid_at=datetime(2026, 3, 13, 10, 0, tzinfo=timezone.utc),
                received_at=datetime(2026, 3, 13, 10, 0, tzinfo=timezone.utc),
                processed_at=None,
            )
        )
        creator_id = creator.id
        session.commit()

    with Session(engine) as session:
        snapshot = get_creator_evidence_ingress_health_snapshot(
            creator_id=creator_id,
            db=session,
        )

    provider_health = {
        item.payment_provider: item
        for item in snapshot.payment_provenance.provider_health
    }
    assert [
        (item.state, item.row_count)
        for item in provider_health["paypal"].settled_state_counts
    ] == [
        (PAYMENT_PROVENANCE_STATE_MATCHED, 1),
        (PAYMENT_PROVENANCE_STATE_PENDING, 0),
        (PAYMENT_PROVENANCE_STATE_UNMATCHED, 0),
        (PAYMENT_PROVENANCE_STATE_CONFLICTING, 0),
    ]
    assert provider_health["paypal"].current_backlog_event_count == 1
    assert [
        (item.reason, item.event_count)
        for item in provider_health["paypal"].current_backlog_reasons
    ] == [
        (UNATTRIBUTED_REASON_UNKNOWN_BOOKING_UUID, 1),
    ]
    assert provider_health["stripe"].current_backlog_event_count == 0


def test_reports_health_requires_auth():
    with TestClient(app) as client:
        response = client.get("/reports/health")

    assert response.status_code == 401
    assert response.json() == {"detail": "not authenticated"}


def test_reports_health_returns_creator_scoped_snapshot_and_logs_counts():
    engine = _engine()

    with Session(engine) as session:
        creator, user = _create_creator_with_user(
            session,
            suffix="api",
            stripe_account_id="acct_health_api",
        )
        other_creator, _ = _create_creator_with_user(
            session,
            suffix="api_other",
            stripe_account_id="acct_health_api_other",
        )
        booking_link = _create_booking_link(session, creator=creator, suffix="api")
        other_booking_link = _create_booking_link(
            session,
            creator=other_creator,
            suffix="api_other",
        )
        content = _create_content(
            session,
            creator=creator,
            booking_link=booking_link,
            suffix="api",
        )
        _create_booking(
            session,
            creator=creator,
            booking_link=booking_link,
            content=None,
            booking_uuid="BOOK_HEALTH_API_UNATTRIBUTED",
            booked_at=datetime(2026, 3, 12, 13, 0, tzinfo=timezone.utc),
            attribution_status=BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED,
            unattributed_reason=BOOKING_UNATTRIBUTED_REASON_MISSING_TID,
        )
        _create_calendly_event_record(
            session,
            event_id="EVT_HEALTH_API_FAILED",
            booking_uuid="BOOK_HEALTH_API_FAILED",
            tid=content.tid,
            processing_status="failed",
        )
        _create_fullscope_event_record(
            session,
            event_id="EVT_HEALTH_API_FULLSCOPE_FAILED",
            appointment_id="FS_APP_HEALTH_API_FAILED",
            tid=content.tid,
            processing_status="failed",
        )

        blocked_booking = _create_booking(
            session,
            creator=creator,
            booking_link=booking_link,
            content=content,
            booking_uuid="BOOK_HEALTH_API_BLOCKED",
            booked_at=datetime(2026, 3, 12, 13, 15, tzinfo=timezone.utc),
        )
        _create_blocked_billing_case(
            session,
            creator=creator,
            booking=blocked_booking,
            blocked_at=datetime(2026, 3, 12, 13, 20, tzinfo=timezone.utc),
        )

        other_content = _create_content(
            session,
            creator=other_creator,
            booking_link=other_booking_link,
            suffix="api_other",
        )
        _create_calendly_event_record(
            session,
            event_id="EVT_HEALTH_API_OTHER_FAILED",
            booking_uuid="BOOK_HEALTH_API_OTHER_FAILED",
            tid=other_content.tid,
            processing_status="failed",
        )
        _create_fullscope_event_record(
            session,
            event_id="EVT_HEALTH_API_OTHER_FULLSCOPE_FAILED",
            appointment_id="FS_APP_HEALTH_API_OTHER_FAILED",
            tid=other_content.tid,
            processing_status="failed",
        )

        token = _access_token(
            user_id=str(user.id),
            creator_id=str(creator.id),
            email=user.email,
            expires_delta=timedelta(hours=24),
        )
        session.commit()

    with patch("app.api.reports.logger.info") as info_log:
        with TestClient(app) as client:
            response = client.get(
                "/reports/health",
                headers={"Authorization": f"Bearer {token}"},
            )

    assert response.status_code == 200
    assert response.json()["booking_attribution"]["unattributed_booking_count"] == 1
    assert response.json()["calendly_ingress"]["backlog_event_count"] == 0
    assert response.json()["calendly_ingress"]["failed_event_count"] == 1
    assert response.json()["fullscope_ingress"]["backlog_event_count"] == 0
    assert response.json()["fullscope_ingress"]["failed_event_count"] == 1
    assert response.json()["blocked_billing"]["open_case_count"] == 1
    assert response.json()["payment_provenance"]["current_backlog_event_count"] == 0
    assert response.json()["payment_provenance"]["provider_health"][0]["payment_provider"] == "stripe"
    assert response.json()["payment_provenance"]["provider_health"][1]["payment_provider"] == "paypal"
    assert response.json()["authoritative_content"]["lagging_content_count"] == 0
    info_log.assert_called_once()
    assert (
        info_log.call_args.args[0]
        == "reports_health_snapshot attribution_unattributed_booking_count=%s calendly_backlog_event_count=%s calendly_failed_event_count=%s fullscope_backlog_event_count=%s fullscope_failed_event_count=%s payment_backlog_event_count=%s payment_pending_count=%s payment_unmatched_count=%s payment_conflicting_count=%s blocked_billing_open_case_count=%s authoritative_lagging_content_count=%s"
    )
    assert info_log.call_args.args[1:] == (
        1,
        0,
        1,
        0,
        1,
        0,
        0,
        0,
        0,
        1,
        0,
    )
