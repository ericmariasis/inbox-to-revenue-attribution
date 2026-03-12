import hashlib
import hmac
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from time import time
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.main import app
from app.models.booking import Booking
from app.models.calendly_webhook_event import CalendlyWebhookEventRecord
from app.models.booking_link import BookingLink
from app.models.blocked_billing_case import BlockedBillingCase
from app.models.content import Content
from app.models.creator import Creator
from app.models.invoice import Invoice
from app.services.booking_attribution import (
    BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED,
    BOOKING_UNATTRIBUTED_REASON_MISSING_TID,
)
from app.services.calendly_webhooks import (
    CalendlyWebhookJournalRecordResult,
    DefaultCalendlyWebhookRouter,
    build_default_calendly_webhook_router,
    verify_and_parse_calendly_webhook,
)
from app.services.stripe_provider import (
    StripeAccountReadiness,
    StripeInvoiceCreateResult,
    StripeProviderError,
)


def _calendly_signature_header(*, payload: bytes, signing_key: str, timestamp: int | None = None) -> str:
    resolved_timestamp = timestamp or int(time())
    signed_payload = f"{resolved_timestamp}.".encode("utf-8") + payload
    signature = hmac.new(signing_key.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={resolved_timestamp},v1={signature}"


@contextmanager
def _override_app_state(name, value):
    had_attr = hasattr(app.state, name)
    previous_value = getattr(app.state, name, None)
    setattr(app.state, name, value)
    try:
        yield
    finally:
        if had_attr:
            setattr(app.state, name, previous_value)
        else:
            delattr(app.state, name)


class _StubSettings:
    calendly_webhook_signing_key = "whsec_story32_test"
    calendly_webhook_tolerance_seconds = 300


class _CaptureCalendlyWebhookRouter:
    def __init__(self):
        self.events: list[dict[str, str | None]] = []
        self.processed_record_ids: list[uuid.UUID] = []

    def record_event(self, *, event) -> CalendlyWebhookJournalRecordResult:
        self.events.append(
            {
                "provider_event_type": event.provider_event_type,
                "calendly_event_id": event.calendly_event_id,
                "calendly_event_id_path": event.calendly_event_id_path,
                "calendly_booking_uuid": event.calendly_booking_uuid,
                "calendly_booking_uuid_path": event.calendly_booking_uuid_path,
                "event_type": event.event_type,
                "tid": event.tid,
                "tid_path": event.tid_path,
            }
        )
        return CalendlyWebhookJournalRecordResult(
            outcome="recorded",
            record_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            delivery_count=1,
            processing_status="received",
            reducer_key=f"booking:{event.calendly_booking_uuid}",
            should_schedule_reducer=True,
        )

    def process_event(self, *, record_id: uuid.UUID, force: bool = False):
        self.processed_record_ids.append(record_id)

    def reprocess_event(self, *, record_id: uuid.UUID):
        raise AssertionError(f"unexpected replay call for {record_id}")


class _CaptureUnpaidInvoiceVoider:
    def __init__(self):
        self.bookings: list[dict[str, Any]] = []

    def void_unpaid_invoice(self, *, booking) -> None:
        self.bookings.append(
            {
                "booking_id": booking.booking_id,
                "creator_id": booking.creator_id,
                "booking_link_id": booking.booking_link_id,
                "tid": booking.tid,
                "calendly_booking_uuid": booking.calendly_booking_uuid,
                "canceled_at": booking.canceled_at,
            }
        )


class _ExplodingBookingCreatedHandler:
    def handle_event(self, *, event):
        if event.event_type == "booking.created":
            raise RuntimeError("story79 reducer explosion")
        return None


class _StubStripeProvider:
    def __init__(
        self,
        *,
        readiness: StripeAccountReadiness,
        created_invoice_id: str = "in_story45_created",
        readiness_error: StripeProviderError | None = None,
        create_error: StripeProviderError | None = None,
        void_error: StripeProviderError | None = None,
    ):
        self._readiness = readiness
        self._created_invoice_id = created_invoice_id
        self._readiness_error = readiness_error
        self._create_error = create_error
        self._void_error = void_error
        self.readiness_calls: list[str] = []
        self.create_calls: list[dict[str, object]] = []
        self.void_calls: list[dict[str, str]] = []

    def build_connect_onboarding_url(self, *, creator_id: str, state: str) -> str:
        raise AssertionError(f"unexpected onboarding call creator_id={creator_id} state={state}")

    def exchange_connect_callback(self, *, code: str, state: str) -> str:
        raise AssertionError(f"unexpected callback exchange code={code} state={state}")

    def get_account_readiness(self, *, stripe_account_id: str) -> StripeAccountReadiness:
        self.readiness_calls.append(stripe_account_id)
        if self._readiness_error is not None:
            raise self._readiness_error
        return self._readiness

    def create_invoice(
        self,
        *,
        stripe_account_id: str,
        amount_cents: int,
        currency: str,
        metadata: dict[str, str],
        idempotency_key: str,
    ) -> StripeInvoiceCreateResult:
        self.create_calls.append(
            {
                "stripe_account_id": stripe_account_id,
                "amount_cents": amount_cents,
                "currency": currency,
                "metadata": metadata,
                "idempotency_key": idempotency_key,
            }
        )
        if self._create_error is not None:
            raise self._create_error
        return StripeInvoiceCreateResult(stripe_invoice_id=self._created_invoice_id)

    def void_invoice(self, *, stripe_account_id: str, stripe_invoice_id: str) -> None:
        self.void_calls.append(
            {
                "stripe_account_id": stripe_account_id,
                "stripe_invoice_id": stripe_invoice_id,
            }
        )
        if self._void_error is not None:
            raise self._void_error


def _booking_count() -> int:
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    with Session(engine) as session:
        return len(session.scalars(select(Booking)).all())


def _invoice_count() -> int:
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    with Session(engine) as session:
        return len(session.scalars(select(Invoice)).all())


def _journal_records_for_event_id(*, calendly_event_id: str) -> list[CalendlyWebhookEventRecord]:
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    with Session(engine) as session:
        return session.scalars(
            select(CalendlyWebhookEventRecord).where(
                CalendlyWebhookEventRecord.calendly_event_id == calendly_event_id
            )
        ).all()


def _create_creator_booking_link_and_content(
    *,
    tid: str,
    stripe_account_id: str | None = None,
    billing_amount_cents: int | None = None,
    billing_currency: str | None = None,
) -> dict[str, Any]:
    engine = create_engine(os.environ["TEST_DATABASE_URL"])

    with Session(engine) as session:
        creator = Creator(
            name=f"Story 33 Creator {uuid.uuid4().hex}",
            stripe_account_id=stripe_account_id,
            stripe_connect_status="connected" if stripe_account_id else "pending",
        )
        session.add(creator)
        session.flush()

        booking_link = BookingLink(
            creator_id=creator.id,
            name="Story 33 Booking Call",
            calendly_url="https://calendly.com/example/story33-call",
            billing_amount_cents=billing_amount_cents,
            billing_currency=billing_currency,
        )
        session.add(booking_link)
        session.flush()

        content = Content(
            creator_id=creator.id,
            booking_link_id=booking_link.id,
            source_url="https://example.com/posts/story33-content",
            tid=tid,
        )
        session.add(content)
        session.commit()

        return {
            "creator_id": creator.id,
            "booking_link_id": booking_link.id,
            "tid": content.tid,
            "stripe_account_id": creator.stripe_account_id,
            "billing_amount_cents": booking_link.billing_amount_cents,
            "billing_currency": booking_link.billing_currency,
        }


def _bookings_for_uuid(*, calendly_booking_uuid: str) -> list[Booking]:
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    with Session(engine) as session:
        return session.scalars(
            select(Booking).where(Booking.calendly_booking_uuid == calendly_booking_uuid)
        ).all()


def _persist_unattributed_booking(
    *,
    creator_id,
    booking_link_id,
    calendly_booking_uuid: str,
    booked_at: datetime,
) -> None:
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    with Session(engine) as session:
        session.add(
            Booking(
                creator_id=creator_id,
                booking_link_id=booking_link_id,
                tid=None,
                calendly_booking_uuid=calendly_booking_uuid,
                email=f"{calendly_booking_uuid.lower()}@example.com",
                status="created",
                attribution_status=BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED,
                unattributed_reason=BOOKING_UNATTRIBUTED_REASON_MISSING_TID,
                booked_at=booked_at,
            )
        )
        session.commit()


def _invoices_for_booking_uuid(*, calendly_booking_uuid: str) -> list[Invoice]:
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    with Session(engine) as session:
        booking = session.scalar(
            select(Booking).where(Booking.calendly_booking_uuid == calendly_booking_uuid)
        )
        if booking is None:
            return []
        return session.scalars(select(Invoice).where(Invoice.booking_id == booking.id)).all()


def _blocked_cases_for_booking_uuid(*, calendly_booking_uuid: str) -> list[BlockedBillingCase]:
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    with Session(engine) as session:
        booking = session.scalar(
            select(Booking).where(Booking.calendly_booking_uuid == calendly_booking_uuid)
        )
        if booking is None:
            return []
        return session.scalars(
            select(BlockedBillingCase).where(BlockedBillingCase.booking_id == booking.id)
        ).all()


def _invitee_created_payload(
    *,
    event_id: str,
    calendly_booking_uuid: str,
    tid: str,
    email: str = "booked@example.com",
    created_at: str = "2026-03-07T14:30:00Z",
    extra_payload_fields: dict[str, Any] | None = None,
) -> bytes:
    payload = {
        "event": "invitee.created",
        "payload": {
            "event": f"https://api.calendly.com/scheduled_events/{event_id}",
            "uri": (
                "https://api.calendly.com/scheduled_events/"
                f"{event_id}/invitees/{calendly_booking_uuid}"
            ),
            "email": email,
            "created_at": created_at,
            "tracking": {"utm_content": tid},
        },
    }
    if extra_payload_fields:
        payload["payload"].update(extra_payload_fields)

    return json.dumps(payload).encode("utf-8")


def _invitee_canceled_payload(
    *,
    event_id: str,
    calendly_booking_uuid: str,
    tid: str | None = None,
    canceled_at: str = "2026-03-07T15:45:00Z",
    extra_payload_fields: dict[str, Any] | None = None,
) -> bytes:
    payload = {
        "event": "invitee.canceled",
        "payload": {
            "event": f"https://api.calendly.com/scheduled_events/{event_id}",
            "uri": (
                "https://api.calendly.com/scheduled_events/"
                f"{event_id}/invitees/{calendly_booking_uuid}"
            ),
            "canceled_at": canceled_at,
        },
    }
    if tid is not None:
        payload["payload"]["tracking"] = {"utm_content": tid}
    if extra_payload_fields:
        payload["payload"].update(extra_payload_fields)

    return json.dumps(payload).encode("utf-8")


def _verified_event_from_payload(payload: bytes):
    return verify_and_parse_calendly_webhook(
        payload=payload,
        signature_header=_calendly_signature_header(
            payload=payload,
            signing_key=_StubSettings.calendly_webhook_signing_key,
        ),
        signing_key=_StubSettings.calendly_webhook_signing_key,
        tolerance_seconds=_StubSettings.calendly_webhook_tolerance_seconds,
    )


def test_calendly_webhook_accepts_valid_signature_routes_verified_event_and_does_not_persist_bookings():
    payload = json.dumps(
        {
            "event": "invitee.created",
            "payload": {
                "event": "https://api.calendly.com/scheduled_events/EVT_story32_valid",
                "uri": "https://api.calendly.com/scheduled_events/EVT_story32_valid/invitees/BOOK_story32_valid",
                "tracking": {"utm_content": "story32_tid"},
            },
        }
    ).encode("utf-8")
    signature_header = _calendly_signature_header(
        payload=payload,
        signing_key=_StubSettings.calendly_webhook_signing_key,
    )
    capture_router = _CaptureCalendlyWebhookRouter()

    assert _booking_count() == 0

    with TestClient(app) as client:
        with _override_app_state("settings", _StubSettings()):
            with _override_app_state("calendly_webhook_router", capture_router):
                response = client.post(
                    "/webhooks/calendly",
                    content=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Calendly-Webhook-Signature": signature_header,
                    },
                )

    assert response.status_code == 200
    assert response.headers.get("X-Request-Id")
    assert response.json() == {"status": "ok"}
    assert capture_router.events == [
        {
            "provider_event_type": "invitee.created",
            "calendly_event_id": "EVT_story32_valid",
            "calendly_event_id_path": "payload.event",
            "calendly_booking_uuid": "BOOK_story32_valid",
            "calendly_booking_uuid_path": "payload.uri",
            "event_type": "booking.created",
            "tid": "story32_tid",
            "tid_path": "payload.tracking.utm_content",
        }
    ]
    assert capture_router.processed_record_ids == [
        uuid.UUID("00000000-0000-0000-0000-000000000001")
    ]
    assert _booking_count() == 0


def test_calendly_router_record_event_persists_only_until_worker_processes():
    stored = _create_creator_booking_link_and_content(tid="story79_persist_only_tid")
    payload = _invitee_created_payload(
        event_id="EVT_story79_persist_only",
        calendly_booking_uuid="BOOK_story79_persist_only",
        tid=stored["tid"],
        email="story79-persist-only@example.com",
        created_at="2026-03-12T16:00:00Z",
    )
    router = DefaultCalendlyWebhookRouter()
    event = _verified_event_from_payload(payload)

    journal_result = router.record_event(event=event)
    journal_records = _journal_records_for_event_id(calendly_event_id="EVT_story79_persist_only")

    assert journal_result.outcome == "recorded"
    assert journal_result.processing_status == "received"
    assert journal_result.reducer_key == "booking:BOOK_story79_persist_only"
    assert journal_result.should_schedule_reducer is True
    assert len(journal_records) == 1
    assert journal_records[0].processing_status == "received"
    assert journal_records[0].reducer_key == "booking:BOOK_story79_persist_only"
    assert journal_records[0].reducer_attempt_count == 0
    assert journal_records[0].processed_at is None
    assert _bookings_for_uuid(calendly_booking_uuid="BOOK_story79_persist_only") == []

    processing_status = router.process_event(record_id=journal_result.record_id)
    bookings = _bookings_for_uuid(calendly_booking_uuid="BOOK_story79_persist_only")
    journal_records = _journal_records_for_event_id(calendly_event_id="EVT_story79_persist_only")

    assert processing_status == "applied"
    assert len(bookings) == 1
    assert bookings[0].status == "created"
    assert len(journal_records) == 1
    assert journal_records[0].processing_status == "applied"
    assert journal_records[0].reducer_key == "booking:BOOK_story79_persist_only"
    assert journal_records[0].reducer_attempt_count == 1
    assert journal_records[0].processed_at is not None


def test_calendly_webhook_persists_booking_created_for_valid_tid():
    stored = _create_creator_booking_link_and_content(tid="story33_valid_tid")
    payload = _invitee_created_payload(
        event_id="EVT_story33_valid",
        calendly_booking_uuid="BOOK_story33_valid",
        tid=stored["tid"],
        email="story33-booked@example.com",
        created_at="2026-03-07T14:30:00Z",
    )
    signature_header = _calendly_signature_header(
        payload=payload,
        signing_key=_StubSettings.calendly_webhook_signing_key,
    )

    assert _booking_count() == 0

    with TestClient(app) as client:
        with _override_app_state("settings", _StubSettings()):
            response = client.post(
                "/webhooks/calendly",
                content=payload,
                headers={
                    "Content-Type": "application/json",
                    "Calendly-Webhook-Signature": signature_header,
                },
            )

    bookings = _bookings_for_uuid(calendly_booking_uuid="BOOK_story33_valid")
    journal_records = _journal_records_for_event_id(calendly_event_id="EVT_story33_valid")

    assert response.status_code == 200
    assert response.headers.get("X-Request-Id")
    assert response.json() == {"status": "ok"}
    assert len(bookings) == 1
    assert bookings[0].creator_id == stored["creator_id"]
    assert bookings[0].booking_link_id == stored["booking_link_id"]
    assert bookings[0].tid == stored["tid"]
    assert bookings[0].calendly_booking_uuid == "BOOK_story33_valid"
    assert bookings[0].email == "story33-booked@example.com"
    assert bookings[0].status == "created"
    assert bookings[0].booked_at == datetime(2026, 3, 7, 14, 30, tzinfo=timezone.utc)
    assert bookings[0].canceled_at is None
    assert len(journal_records) == 1
    assert journal_records[0].processing_status == "applied"
    assert journal_records[0].delivery_count == 1
    assert journal_records[0].tid == stored["tid"]
    assert journal_records[0].processed_at is not None
    assert _invoice_count() == 0


def test_calendly_webhook_duplicate_delivery_is_idempotent_by_booking_uuid():
    stored = _create_creator_booking_link_and_content(tid="story33_duplicate_tid")
    payload = _invitee_created_payload(
        event_id="EVT_story33_duplicate",
        calendly_booking_uuid="BOOK_story33_duplicate",
        tid=stored["tid"],
        email="story33-duplicate@example.com",
        created_at="2026-03-07T16:00:00Z",
    )
    signature_header = _calendly_signature_header(
        payload=payload,
        signing_key=_StubSettings.calendly_webhook_signing_key,
    )

    with TestClient(app) as client:
        with _override_app_state("settings", _StubSettings()):
            first_response = client.post(
                "/webhooks/calendly",
                content=payload,
                headers={
                    "Content-Type": "application/json",
                    "Calendly-Webhook-Signature": signature_header,
                },
            )
            second_response = client.post(
                "/webhooks/calendly",
                content=payload,
                headers={
                    "Content-Type": "application/json",
                    "Calendly-Webhook-Signature": signature_header,
                },
            )

    bookings = _bookings_for_uuid(calendly_booking_uuid="BOOK_story33_duplicate")
    journal_records = _journal_records_for_event_id(calendly_event_id="EVT_story33_duplicate")

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert len(journal_records) == 1
    assert journal_records[0].processing_status == "applied"
    assert journal_records[0].reducer_key == "booking:BOOK_story33_duplicate"
    assert journal_records[0].reducer_attempt_count == 1
    assert journal_records[0].delivery_count == 2
    assert len(bookings) == 1
    assert bookings[0].creator_id == stored["creator_id"]
    assert bookings[0].booking_link_id == stored["booking_link_id"]
    assert bookings[0].tid == stored["tid"]
    assert bookings[0].email == "story33-duplicate@example.com"
    assert bookings[0].status == "created"
    assert bookings[0].booked_at == datetime(2026, 3, 7, 16, 0, tzinfo=timezone.utc)
    assert _invoice_count() == 0


def test_calendly_webhook_failed_row_can_retry_through_same_live_reducer_path():
    stored = _create_creator_booking_link_and_content(tid="story79_failed_retry_tid")
    payload = _invitee_created_payload(
        event_id="EVT_story79_failed_retry",
        calendly_booking_uuid="BOOK_story79_failed_retry",
        tid=stored["tid"],
        email="story79-failed-retry@example.com",
        created_at="2026-03-12T17:00:00Z",
    )
    event = _verified_event_from_payload(payload)
    failing_router = DefaultCalendlyWebhookRouter(
        booking_created_handler=_ExplodingBookingCreatedHandler(),
    )

    recorded_result = failing_router.record_event(event=event)
    failed_status = failing_router.process_event(record_id=recorded_result.record_id)
    failed_records = _journal_records_for_event_id(calendly_event_id="EVT_story79_failed_retry")

    assert recorded_result.outcome == "recorded"
    assert failed_status == "failed"
    assert len(failed_records) == 1
    assert failed_records[0].processing_status == "failed"
    assert failed_records[0].reducer_key == "booking:BOOK_story79_failed_retry"
    assert failed_records[0].reducer_attempt_count == 1
    assert failed_records[0].last_error == "RuntimeError: story79 reducer explosion"
    assert _bookings_for_uuid(calendly_booking_uuid="BOOK_story79_failed_retry") == []

    retry_router = DefaultCalendlyWebhookRouter()
    duplicate_result = retry_router.record_event(event=event)
    applied_status = retry_router.process_event(record_id=recorded_result.record_id)
    recovered_records = _journal_records_for_event_id(calendly_event_id="EVT_story79_failed_retry")
    bookings = _bookings_for_uuid(calendly_booking_uuid="BOOK_story79_failed_retry")

    assert duplicate_result.outcome == "duplicate"
    assert duplicate_result.processing_status == "failed"
    assert duplicate_result.should_schedule_reducer is True
    assert duplicate_result.delivery_count == 2
    assert applied_status == "applied"
    assert len(bookings) == 1
    assert bookings[0].status == "created"
    assert len(recovered_records) == 1
    assert recovered_records[0].processing_status == "applied"
    assert recovered_records[0].reducer_attempt_count == 2
    assert recovered_records[0].delivery_count == 2
    assert recovered_records[0].last_error is None


def test_calendly_webhook_resolves_creator_and_booking_link_from_stored_content_not_payload_fields():
    stored = _create_creator_booking_link_and_content(tid="story33_spoof_tid")
    payload = _invitee_created_payload(
        event_id="EVT_story33_spoof",
        calendly_booking_uuid="BOOK_story33_spoof",
        tid=stored["tid"],
        email="story33-spoof@example.com",
        created_at="2026-03-07T18:00:00Z",
        extra_payload_fields={
            "creator_id": str(uuid.uuid4()),
            "booking_link_id": str(uuid.uuid4()),
            "tid": "spoofed_tid_should_not_win",
        },
    )
    signature_header = _calendly_signature_header(
        payload=payload,
        signing_key=_StubSettings.calendly_webhook_signing_key,
    )

    with TestClient(app) as client:
        with _override_app_state("settings", _StubSettings()):
            response = client.post(
                "/webhooks/calendly",
                content=payload,
                headers={
                    "Content-Type": "application/json",
                    "Calendly-Webhook-Signature": signature_header,
                },
            )

    bookings = _bookings_for_uuid(calendly_booking_uuid="BOOK_story33_spoof")

    assert response.status_code == 200
    assert response.headers.get("X-Request-Id")
    assert response.json() == {"status": "ok"}
    assert len(bookings) == 1
    assert bookings[0].creator_id == stored["creator_id"]
    assert bookings[0].booking_link_id == stored["booking_link_id"]
    assert bookings[0].tid == stored["tid"]
    assert bookings[0].email == "story33-spoof@example.com"
    assert bookings[0].status == "created"
    assert _invoice_count() == 0


def test_calendly_webhook_billable_booking_created_persists_invoice_and_duplicate_is_idempotent():
    stored = _create_creator_booking_link_and_content(
        tid="story45_billable_tid",
        stripe_account_id="acct_story45_billable",
        billing_amount_cents=15000,
        billing_currency="USD",
    )
    payload = _invitee_created_payload(
        event_id="EVT_story45_billable",
        calendly_booking_uuid="BOOK_story45_billable",
        tid=stored["tid"],
        email="story45-billable@example.com",
        created_at="2026-03-08T20:00:00Z",
    )
    signature_header = _calendly_signature_header(
        payload=payload,
        signing_key=_StubSettings.calendly_webhook_signing_key,
    )
    provider = _StubStripeProvider(
        readiness=StripeAccountReadiness(charges_enabled=True),
        created_invoice_id="in_story45_billable",
    )
    router = build_default_calendly_webhook_router(provider=provider)

    with TestClient(app) as client:
        with _override_app_state("settings", _StubSettings()):
            with _override_app_state("calendly_webhook_router", router):
                first_response = client.post(
                    "/webhooks/calendly",
                    content=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Calendly-Webhook-Signature": signature_header,
                    },
                )
                second_response = client.post(
                    "/webhooks/calendly",
                    content=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Calendly-Webhook-Signature": signature_header,
                    },
                )

    bookings = _bookings_for_uuid(calendly_booking_uuid="BOOK_story45_billable")
    invoices = _invoices_for_booking_uuid(calendly_booking_uuid="BOOK_story45_billable")
    journal_records = _journal_records_for_event_id(calendly_event_id="EVT_story45_billable")

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert len(journal_records) == 1
    assert journal_records[0].processing_status == "applied"
    assert journal_records[0].delivery_count == 2
    assert len(bookings) == 1
    assert bookings[0].frozen_billing_amount_cents == 15000
    assert bookings[0].frozen_billing_currency == "USD"
    assert len(invoices) == 1
    assert invoices[0].creator_id == stored["creator_id"]
    assert invoices[0].booking_id == bookings[0].id
    assert invoices[0].tid == stored["tid"]
    assert invoices[0].stripe_account_id == "acct_story45_billable"
    assert invoices[0].stripe_invoice_id == "in_story45_billable"
    assert invoices[0].amount_cents == 15000
    assert invoices[0].currency == "USD"
    assert invoices[0].status == "open"
    assert invoices[0].voided_at is None
    assert provider.readiness_calls == ["acct_story45_billable"]
    assert len(provider.create_calls) == 1
    assert provider.create_calls[0] == {
        "stripe_account_id": "acct_story45_billable",
        "amount_cents": 15000,
        "currency": "USD",
        "metadata": {
            "creator_id": str(stored["creator_id"]),
            "booking_uuid": "BOOK_story45_billable",
            "tid": stored["tid"],
        },
        "idempotency_key": "billing:create:BOOK_story45_billable",
    }
    assert provider.void_calls == []


def test_calendly_webhook_booking_canceled_voids_open_invoice_once_and_repeat_is_safe():
    stored = _create_creator_booking_link_and_content(
        tid="story45_cancel_tid",
        stripe_account_id="acct_story45_cancel",
        billing_amount_cents=18000,
        billing_currency="USD",
    )
    created_payload = _invitee_created_payload(
        event_id="EVT_story45_cancel",
        calendly_booking_uuid="BOOK_story45_cancel",
        tid=stored["tid"],
        email="story45-cancel@example.com",
        created_at="2026-03-08T20:15:00Z",
    )
    canceled_payload = _invitee_canceled_payload(
        event_id="EVT_story45_cancel",
        calendly_booking_uuid="BOOK_story45_cancel",
        tid=stored["tid"],
        canceled_at="2026-03-08T20:45:00Z",
    )
    created_signature_header = _calendly_signature_header(
        payload=created_payload,
        signing_key=_StubSettings.calendly_webhook_signing_key,
    )
    canceled_signature_header = _calendly_signature_header(
        payload=canceled_payload,
        signing_key=_StubSettings.calendly_webhook_signing_key,
    )
    provider = _StubStripeProvider(
        readiness=StripeAccountReadiness(charges_enabled=True),
        created_invoice_id="in_story45_cancel",
    )
    router = build_default_calendly_webhook_router(provider=provider)

    with TestClient(app) as client:
        with _override_app_state("settings", _StubSettings()):
            with _override_app_state("calendly_webhook_router", router):
                created_response = client.post(
                    "/webhooks/calendly",
                    content=created_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Calendly-Webhook-Signature": created_signature_header,
                    },
                )
                first_canceled_response = client.post(
                    "/webhooks/calendly",
                    content=canceled_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Calendly-Webhook-Signature": canceled_signature_header,
                    },
                )
                second_canceled_response = client.post(
                    "/webhooks/calendly",
                    content=canceled_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Calendly-Webhook-Signature": canceled_signature_header,
                    },
                )

    bookings = _bookings_for_uuid(calendly_booking_uuid="BOOK_story45_cancel")
    invoices = _invoices_for_booking_uuid(calendly_booking_uuid="BOOK_story45_cancel")

    assert created_response.status_code == 200
    assert first_canceled_response.status_code == 200
    assert second_canceled_response.status_code == 200
    assert len(bookings) == 1
    assert bookings[0].status == "canceled"
    assert len(invoices) == 1
    assert invoices[0].status == "void"
    assert invoices[0].voided_at is not None
    assert len(provider.create_calls) == 1
    assert provider.void_calls == [
        {
            "stripe_account_id": "acct_story45_cancel",
            "stripe_invoice_id": "in_story45_cancel",
        }
    ]


def test_calendly_webhook_booking_created_with_missing_billing_defaults_creates_no_invoice():
    stored = _create_creator_booking_link_and_content(
        tid="story45_missing_defaults_tid",
        stripe_account_id="acct_story45_missing_defaults",
    )
    payload = _invitee_created_payload(
        event_id="EVT_story45_missing_defaults",
        calendly_booking_uuid="BOOK_story45_missing_defaults",
        tid=stored["tid"],
        email="story45-missing-defaults@example.com",
        created_at="2026-03-08T21:00:00Z",
    )
    signature_header = _calendly_signature_header(
        payload=payload,
        signing_key=_StubSettings.calendly_webhook_signing_key,
    )
    provider = _StubStripeProvider(readiness=StripeAccountReadiness(charges_enabled=True))
    router = build_default_calendly_webhook_router(provider=provider)

    with TestClient(app) as client:
        with _override_app_state("settings", _StubSettings()):
            with _override_app_state("calendly_webhook_router", router):
                response = client.post(
                    "/webhooks/calendly",
                    content=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Calendly-Webhook-Signature": signature_header,
                    },
                )

    bookings = _bookings_for_uuid(calendly_booking_uuid="BOOK_story45_missing_defaults")
    invoices = _invoices_for_booking_uuid(calendly_booking_uuid="BOOK_story45_missing_defaults")

    assert response.status_code == 200
    assert len(bookings) == 1
    assert invoices == []
    assert provider.readiness_calls == []
    assert provider.create_calls == []
    assert provider.void_calls == []


def test_calendly_webhook_booking_created_with_provider_error_returns_safe_ok_and_no_invoice():
    stored = _create_creator_booking_link_and_content(
        tid="story57_provider_error_tid",
        stripe_account_id="acct_story57_provider_error",
        billing_amount_cents=15000,
        billing_currency="USD",
    )
    payload = _invitee_created_payload(
        event_id="EVT_story57_provider_error",
        calendly_booking_uuid="BOOK_story57_provider_error",
        tid=stored["tid"],
        email="story57-provider-error@example.com",
        created_at="2026-03-09T10:00:00Z",
    )
    signature_header = _calendly_signature_header(
        payload=payload,
        signing_key=_StubSettings.calendly_webhook_signing_key,
    )
    provider = _StubStripeProvider(
        readiness=StripeAccountReadiness(charges_enabled=True),
        create_error=StripeProviderError(
            "stripe invoice creation failed",
            operation="stripe_invoice_create",
            http_status=502,
            error_code="api_error",
        ),
    )
    router = build_default_calendly_webhook_router(provider=provider)

    with TestClient(app) as client:
        with _override_app_state("settings", _StubSettings()):
            with _override_app_state("calendly_webhook_router", router):
                response = client.post(
                    "/webhooks/calendly",
                    content=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Calendly-Webhook-Signature": signature_header,
                    },
                )

    bookings = _bookings_for_uuid(calendly_booking_uuid="BOOK_story57_provider_error")
    invoices = _invoices_for_booking_uuid(calendly_booking_uuid="BOOK_story57_provider_error")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert len(bookings) == 1
    assert invoices == []
    assert provider.readiness_calls == ["acct_story57_provider_error"]
    assert len(provider.create_calls) == 1
    assert provider.void_calls == []


def test_calendly_webhook_booking_created_with_non_billable_creator_creates_no_invoice():
    stored = _create_creator_booking_link_and_content(
        tid="story45_not_billable_tid",
        stripe_account_id="acct_story45_not_billable",
        billing_amount_cents=22000,
        billing_currency="USD",
    )
    payload = _invitee_created_payload(
        event_id="EVT_story45_not_billable",
        calendly_booking_uuid="BOOK_story45_not_billable",
        tid=stored["tid"],
        email="story45-not-billable@example.com",
        created_at="2026-03-08T21:15:00Z",
    )
    signature_header = _calendly_signature_header(
        payload=payload,
        signing_key=_StubSettings.calendly_webhook_signing_key,
    )
    provider = _StubStripeProvider(readiness=StripeAccountReadiness(charges_enabled=False))
    router = build_default_calendly_webhook_router(provider=provider)

    with TestClient(app) as client:
        with _override_app_state("settings", _StubSettings()):
            with _override_app_state("calendly_webhook_router", router):
                response = client.post(
                    "/webhooks/calendly",
                    content=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Calendly-Webhook-Signature": signature_header,
                    },
                )

    bookings = _bookings_for_uuid(calendly_booking_uuid="BOOK_story45_not_billable")
    invoices = _invoices_for_booking_uuid(calendly_booking_uuid="BOOK_story45_not_billable")
    blocked_cases = _blocked_cases_for_booking_uuid(
        calendly_booking_uuid="BOOK_story45_not_billable"
    )

    assert response.status_code == 200
    assert len(bookings) == 1
    assert bookings[0].frozen_billing_amount_cents == 22000
    assert bookings[0].frozen_billing_currency == "USD"
    assert invoices == []
    assert len(blocked_cases) == 1
    assert blocked_cases[0].reason_code == "creator_not_billable"
    assert blocked_cases[0].status == "open"
    assert provider.readiness_calls == ["acct_story45_not_billable"]
    assert provider.create_calls == []
    assert provider.void_calls == []


def test_calendly_webhook_booking_canceled_closes_open_blocked_billing_case():
    stored = _create_creator_booking_link_and_content(
        tid="story58_cancel_blocked_tid",
        stripe_account_id="acct_story58_cancel_blocked",
        billing_amount_cents=22000,
        billing_currency="USD",
    )
    created_payload = _invitee_created_payload(
        event_id="EVT_story58_cancel_blocked",
        calendly_booking_uuid="BOOK_story58_cancel_blocked",
        tid=stored["tid"],
        email="story58-cancel-blocked@example.com",
        created_at="2026-03-09T11:00:00Z",
    )
    canceled_payload = _invitee_canceled_payload(
        event_id="EVT_story58_cancel_blocked",
        calendly_booking_uuid="BOOK_story58_cancel_blocked",
        tid=stored["tid"],
        canceled_at="2026-03-09T11:30:00Z",
    )
    created_signature_header = _calendly_signature_header(
        payload=created_payload,
        signing_key=_StubSettings.calendly_webhook_signing_key,
    )
    canceled_signature_header = _calendly_signature_header(
        payload=canceled_payload,
        signing_key=_StubSettings.calendly_webhook_signing_key,
    )
    provider = _StubStripeProvider(readiness=StripeAccountReadiness(charges_enabled=False))
    router = build_default_calendly_webhook_router(provider=provider)

    with TestClient(app) as client:
        with _override_app_state("settings", _StubSettings()):
            with _override_app_state("calendly_webhook_router", router):
                created_response = client.post(
                    "/webhooks/calendly",
                    content=created_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Calendly-Webhook-Signature": created_signature_header,
                    },
                )
                canceled_response = client.post(
                    "/webhooks/calendly",
                    content=canceled_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Calendly-Webhook-Signature": canceled_signature_header,
                    },
                )

    blocked_cases = _blocked_cases_for_booking_uuid(
        calendly_booking_uuid="BOOK_story58_cancel_blocked"
    )
    bookings = _bookings_for_uuid(calendly_booking_uuid="BOOK_story58_cancel_blocked")

    assert created_response.status_code == 200
    assert canceled_response.status_code == 200
    assert len(bookings) == 1
    assert bookings[0].status == "canceled"
    assert bookings[0].frozen_billing_amount_cents == 22000
    assert bookings[0].frozen_billing_currency == "USD"
    assert len(blocked_cases) == 1
    assert blocked_cases[0].status == "resolved"
    assert blocked_cases[0].resolution_code == "booking_canceled"
    assert blocked_cases[0].resolved_at == datetime(2026, 3, 9, 11, 30, tzinfo=timezone.utc)


def test_calendly_webhook_booking_canceled_with_provider_void_error_returns_safe_ok_and_leaves_invoice_open():
    stored = _create_creator_booking_link_and_content(
        tid="story57_void_provider_error_tid",
        stripe_account_id="acct_story57_void_provider_error",
        billing_amount_cents=17000,
        billing_currency="USD",
    )
    created_payload = _invitee_created_payload(
        event_id="EVT_story57_void_provider_error",
        calendly_booking_uuid="BOOK_story57_void_provider_error",
        tid=stored["tid"],
        email="story57-void-provider-error@example.com",
        created_at="2026-03-09T10:15:00Z",
    )
    canceled_payload = _invitee_canceled_payload(
        event_id="EVT_story57_void_provider_error",
        calendly_booking_uuid="BOOK_story57_void_provider_error",
        tid=stored["tid"],
        canceled_at="2026-03-09T10:45:00Z",
    )
    created_signature_header = _calendly_signature_header(
        payload=created_payload,
        signing_key=_StubSettings.calendly_webhook_signing_key,
    )
    canceled_signature_header = _calendly_signature_header(
        payload=canceled_payload,
        signing_key=_StubSettings.calendly_webhook_signing_key,
    )
    provider = _StubStripeProvider(
        readiness=StripeAccountReadiness(charges_enabled=True),
        created_invoice_id="in_story57_void_provider_error",
        void_error=StripeProviderError(
            "stripe invoice void failed",
            operation="stripe_invoice_void",
            http_status=409,
            error_code="invoice_invalid_state",
        ),
    )
    router = build_default_calendly_webhook_router(provider=provider)

    with TestClient(app) as client:
        with _override_app_state("settings", _StubSettings()):
            with _override_app_state("calendly_webhook_router", router):
                created_response = client.post(
                    "/webhooks/calendly",
                    content=created_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Calendly-Webhook-Signature": created_signature_header,
                    },
                )
                canceled_response = client.post(
                    "/webhooks/calendly",
                    content=canceled_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Calendly-Webhook-Signature": canceled_signature_header,
                    },
                )

    bookings = _bookings_for_uuid(calendly_booking_uuid="BOOK_story57_void_provider_error")
    invoices = _invoices_for_booking_uuid(calendly_booking_uuid="BOOK_story57_void_provider_error")

    assert created_response.status_code == 200
    assert canceled_response.status_code == 200
    assert len(bookings) == 1
    assert bookings[0].status == "canceled"
    assert len(invoices) == 1
    assert invoices[0].status == "open"
    assert invoices[0].voided_at is None
    assert len(provider.create_calls) == 1
    assert provider.void_calls == [
        {
            "stripe_account_id": "acct_story57_void_provider_error",
            "stripe_invoice_id": "in_story57_void_provider_error",
        }
    ]


def test_calendly_webhook_marks_booking_canceled_and_stays_safe_without_invoice_persistence():
    stored = _create_creator_booking_link_and_content(tid="story34_cancel_tid")
    created_payload = _invitee_created_payload(
        event_id="EVT_story34_cancel",
        calendly_booking_uuid="BOOK_story34_cancel",
        tid=stored["tid"],
        email="story34-cancel@example.com",
        created_at="2026-03-07T14:30:00Z",
    )
    canceled_payload = _invitee_canceled_payload(
        event_id="EVT_story34_cancel",
        calendly_booking_uuid="BOOK_story34_cancel",
        tid=stored["tid"],
        canceled_at="2026-03-07T15:45:00Z",
    )
    created_signature_header = _calendly_signature_header(
        payload=created_payload,
        signing_key=_StubSettings.calendly_webhook_signing_key,
    )
    canceled_signature_header = _calendly_signature_header(
        payload=canceled_payload,
        signing_key=_StubSettings.calendly_webhook_signing_key,
    )

    with TestClient(app) as client:
        with _override_app_state("settings", _StubSettings()):
            created_response = client.post(
                "/webhooks/calendly",
                content=created_payload,
                headers={
                    "Content-Type": "application/json",
                    "Calendly-Webhook-Signature": created_signature_header,
                },
            )
            canceled_response = client.post(
                "/webhooks/calendly",
                content=canceled_payload,
                headers={
                    "Content-Type": "application/json",
                    "Calendly-Webhook-Signature": canceled_signature_header,
                },
            )

    bookings = _bookings_for_uuid(calendly_booking_uuid="BOOK_story34_cancel")

    assert created_response.status_code == 200
    assert canceled_response.status_code == 200
    assert len(bookings) == 1
    assert bookings[0].creator_id == stored["creator_id"]
    assert bookings[0].booking_link_id == stored["booking_link_id"]
    assert bookings[0].tid == stored["tid"]
    assert bookings[0].email == "story34-cancel@example.com"
    assert bookings[0].status == "canceled"
    assert bookings[0].booked_at == datetime(2026, 3, 7, 14, 30, tzinfo=timezone.utc)
    assert bookings[0].canceled_at == datetime(2026, 3, 7, 15, 45, tzinfo=timezone.utc)


def test_calendly_webhook_duplicate_cancellation_is_idempotent_and_voids_invoice_once():
    stored = _create_creator_booking_link_and_content(tid="story34_duplicate_cancel_tid")
    created_payload = _invitee_created_payload(
        event_id="EVT_story34_duplicate_cancel",
        calendly_booking_uuid="BOOK_story34_duplicate_cancel",
        tid=stored["tid"],
        email="story34-duplicate-cancel@example.com",
        created_at="2026-03-07T16:00:00Z",
    )
    canceled_payload = _invitee_canceled_payload(
        event_id="EVT_story34_duplicate_cancel",
        calendly_booking_uuid="BOOK_story34_duplicate_cancel",
        tid=stored["tid"],
        canceled_at="2026-03-07T16:30:00Z",
    )
    created_signature_header = _calendly_signature_header(
        payload=created_payload,
        signing_key=_StubSettings.calendly_webhook_signing_key,
    )
    canceled_signature_header = _calendly_signature_header(
        payload=canceled_payload,
        signing_key=_StubSettings.calendly_webhook_signing_key,
    )
    capture_voider = _CaptureUnpaidInvoiceVoider()
    router = DefaultCalendlyWebhookRouter(unpaid_invoice_voider=capture_voider)

    with TestClient(app) as client:
        with _override_app_state("settings", _StubSettings()):
            with _override_app_state("calendly_webhook_router", router):
                created_response = client.post(
                    "/webhooks/calendly",
                    content=created_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Calendly-Webhook-Signature": created_signature_header,
                    },
                )
                first_canceled_response = client.post(
                    "/webhooks/calendly",
                    content=canceled_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Calendly-Webhook-Signature": canceled_signature_header,
                    },
                )
                second_canceled_response = client.post(
                    "/webhooks/calendly",
                    content=canceled_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Calendly-Webhook-Signature": canceled_signature_header,
                    },
                )

    bookings = _bookings_for_uuid(calendly_booking_uuid="BOOK_story34_duplicate_cancel")

    assert created_response.status_code == 200
    assert first_canceled_response.status_code == 200
    assert second_canceled_response.status_code == 200
    assert len(bookings) == 1
    assert bookings[0].status == "canceled"
    assert bookings[0].canceled_at == datetime(2026, 3, 7, 16, 30, tzinfo=timezone.utc)
    assert capture_voider.bookings == [
        {
            "booking_id": bookings[0].id,
            "creator_id": stored["creator_id"],
            "booking_link_id": stored["booking_link_id"],
            "tid": stored["tid"],
            "calendly_booking_uuid": "BOOK_story34_duplicate_cancel",
            "canceled_at": datetime(2026, 3, 7, 16, 30, tzinfo=timezone.utc),
        }
    ]


def test_calendly_webhook_canceled_before_created_is_deferred_and_replay_applies_it():
    stored = _create_creator_booking_link_and_content(tid="story69_out_of_order_tid")
    canceled_payload = _invitee_canceled_payload(
        event_id="EVT_story69_out_of_order_cancel",
        calendly_booking_uuid="BOOK_story69_out_of_order",
        tid=stored["tid"],
        canceled_at="2026-03-11T18:00:00Z",
    )
    created_payload = _invitee_created_payload(
        event_id="EVT_story69_out_of_order_create",
        calendly_booking_uuid="BOOK_story69_out_of_order",
        tid=stored["tid"],
        email="story69-out-of-order@example.com",
        created_at="2026-03-11T17:30:00Z",
    )
    canceled_signature_header = _calendly_signature_header(
        payload=canceled_payload,
        signing_key=_StubSettings.calendly_webhook_signing_key,
    )
    created_signature_header = _calendly_signature_header(
        payload=created_payload,
        signing_key=_StubSettings.calendly_webhook_signing_key,
    )
    capture_voider = _CaptureUnpaidInvoiceVoider()
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    router = DefaultCalendlyWebhookRouter(
        session_factory=lambda: Session(engine),
        unpaid_invoice_voider=capture_voider,
    )

    with TestClient(app) as client:
        with _override_app_state("settings", _StubSettings()):
            with _override_app_state("calendly_webhook_router", router):
                canceled_response = client.post(
                    "/webhooks/calendly",
                    content=canceled_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Calendly-Webhook-Signature": canceled_signature_header,
                    },
                )
                deferred_records = _journal_records_for_event_id(
                    calendly_event_id="EVT_story69_out_of_order_cancel"
                )
                created_response = client.post(
                    "/webhooks/calendly",
                    content=created_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Calendly-Webhook-Signature": created_signature_header,
                    },
                )
                replay_result = router.reprocess_event(
                    record_id=deferred_records[0].id
                )

    canceled_records = _journal_records_for_event_id(
        calendly_event_id="EVT_story69_out_of_order_cancel"
    )
    created_records = _journal_records_for_event_id(
        calendly_event_id="EVT_story69_out_of_order_create"
    )
    bookings = _bookings_for_uuid(calendly_booking_uuid="BOOK_story69_out_of_order")

    assert canceled_response.status_code == 200
    assert created_response.status_code == 200
    assert len(deferred_records) == 1
    assert deferred_records[0].processing_status == "deferred_missing_booking"
    assert deferred_records[0].reducer_key == "booking:BOOK_story69_out_of_order"
    assert deferred_records[0].reducer_attempt_count == 1
    assert replay_result.outcome == "reprocessed"
    assert replay_result.processing_status == "applied"
    assert len(canceled_records) == 1
    assert canceled_records[0].processing_status == "applied"
    assert canceled_records[0].reducer_key == "booking:BOOK_story69_out_of_order"
    assert canceled_records[0].reducer_attempt_count == 2
    assert canceled_records[0].tid == stored["tid"]
    assert canceled_records[0].processed_at is not None
    assert len(created_records) == 1
    assert created_records[0].processing_status == "applied"
    assert created_records[0].reducer_key == "booking:BOOK_story69_out_of_order"
    assert created_records[0].reducer_attempt_count == 1
    assert len(bookings) == 1
    assert bookings[0].status == "canceled"
    assert bookings[0].canceled_at == datetime(2026, 3, 11, 18, 0, tzinfo=timezone.utc)
    assert capture_voider.bookings == [
        {
            "booking_id": bookings[0].id,
            "creator_id": stored["creator_id"],
            "booking_link_id": stored["booking_link_id"],
            "tid": stored["tid"],
            "calendly_booking_uuid": "BOOK_story69_out_of_order",
            "canceled_at": datetime(2026, 3, 11, 18, 0, tzinfo=timezone.utc),
        }
    ]


def test_calendly_webhook_canceled_preserves_unattributed_booking_current_state():
    stored = _create_creator_booking_link_and_content(tid="story78_unattributed_cancel_seed")
    _persist_unattributed_booking(
        creator_id=stored["creator_id"],
        booking_link_id=stored["booking_link_id"],
        calendly_booking_uuid="BOOK_story78_unattributed_cancel",
        booked_at=datetime(2026, 3, 12, 16, 0, tzinfo=timezone.utc),
    )
    canceled_payload = _invitee_canceled_payload(
        event_id="EVT_story78_unattributed_cancel",
        calendly_booking_uuid="BOOK_story78_unattributed_cancel",
        tid=None,
        canceled_at="2026-03-12T16:45:00Z",
    )
    canceled_signature_header = _calendly_signature_header(
        payload=canceled_payload,
        signing_key=_StubSettings.calendly_webhook_signing_key,
    )
    capture_voider = _CaptureUnpaidInvoiceVoider()
    router = DefaultCalendlyWebhookRouter(unpaid_invoice_voider=capture_voider)

    with TestClient(app) as client:
        with _override_app_state("settings", _StubSettings()):
            with _override_app_state("calendly_webhook_router", router):
                response = client.post(
                    "/webhooks/calendly",
                    content=canceled_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Calendly-Webhook-Signature": canceled_signature_header,
                    },
                )

    bookings = _bookings_for_uuid(calendly_booking_uuid="BOOK_story78_unattributed_cancel")
    journal_records = _journal_records_for_event_id(
        calendly_event_id="EVT_story78_unattributed_cancel"
    )

    assert response.status_code == 200
    assert len(bookings) == 1
    assert bookings[0].status == "canceled"
    assert bookings[0].canceled_at == datetime(2026, 3, 12, 16, 45, tzinfo=timezone.utc)
    assert bookings[0].tid is None
    assert bookings[0].attribution_status == BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED
    assert bookings[0].unattributed_reason == BOOKING_UNATTRIBUTED_REASON_MISSING_TID
    assert len(journal_records) == 1
    assert journal_records[0].processing_status == "applied"
    assert capture_voider.bookings == [
        {
            "booking_id": bookings[0].id,
            "creator_id": stored["creator_id"],
            "booking_link_id": stored["booking_link_id"],
            "tid": None,
            "calendly_booking_uuid": "BOOK_story78_unattributed_cancel",
            "canceled_at": datetime(2026, 3, 12, 16, 45, tzinfo=timezone.utc),
        }
    ]


def test_calendly_webhook_verified_booking_created_without_tid_logs_and_persists_no_booking():
    payload = json.dumps(
        {
            "event": "invitee.created",
            "payload": {
                "event": "https://api.calendly.com/scheduled_events/EVT_story34_missing_tid",
                "uri": "https://api.calendly.com/scheduled_events/EVT_story34_missing_tid/invitees/BOOK_story34_missing_tid",
                "email": "story34-missing-tid@example.com",
                "created_at": "2026-03-07T17:00:00Z",
            },
        }
    ).encode("utf-8")
    signature_header = _calendly_signature_header(
        payload=payload,
        signing_key=_StubSettings.calendly_webhook_signing_key,
    )

    assert _booking_count() == 0

    with patch("app.services.calendly_webhooks.logger.warning") as warning_log:
        with TestClient(app) as client:
            with _override_app_state("settings", _StubSettings()):
                response = client.post(
                    "/webhooks/calendly",
                    content=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Calendly-Webhook-Signature": signature_header,
                    },
                )
    journal_records = _journal_records_for_event_id(calendly_event_id="EVT_story34_missing_tid")

    assert response.status_code == 200
    assert response.headers.get("X-Request-Id")
    assert response.json() == {"status": "ok"}
    assert _booking_count() == 0
    warning_log.assert_called_once()
    assert (
        warning_log.call_args.args[0]
        == "calendly_webhook_booking_created_missing_tid calendly_booking_uuid=%s provider_event_type=%s calendly_event_id=%s"
    )
    assert warning_log.call_args.args[1] == "BOOK_story34_missing_tid"
    assert warning_log.call_args.args[2] == "invitee.created"
    assert warning_log.call_args.args[3] == "EVT_story34_missing_tid"
    assert len(journal_records) == 1
    assert journal_records[0].processing_status == "ignored_missing_tid"
    assert journal_records[0].delivery_count == 1
    assert journal_records[0].processed_at is not None


def test_calendly_webhook_verified_booking_created_with_unknown_tid_logs_and_persists_no_booking():
    payload = _invitee_created_payload(
        event_id="EVT_story34_unknown_tid",
        calendly_booking_uuid="BOOK_story34_unknown_tid",
        tid="story34_unknown_tid",
        email="story34-unknown-tid@example.com",
        created_at="2026-03-07T17:15:00Z",
    )
    signature_header = _calendly_signature_header(
        payload=payload,
        signing_key=_StubSettings.calendly_webhook_signing_key,
    )

    assert _booking_count() == 0

    with patch("app.services.calendly_webhooks.logger.warning") as warning_log:
        with TestClient(app) as client:
            with _override_app_state("settings", _StubSettings()):
                response = client.post(
                    "/webhooks/calendly",
                    content=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Calendly-Webhook-Signature": signature_header,
                    },
                )
    journal_records = _journal_records_for_event_id(calendly_event_id="EVT_story34_unknown_tid")

    assert response.status_code == 200
    assert response.headers.get("X-Request-Id")
    assert response.json() == {"status": "ok"}
    assert _booking_count() == 0
    warning_log.assert_called_once()
    assert (
        warning_log.call_args.args[0]
        == "calendly_webhook_booking_created_unknown_tid calendly_booking_uuid=%s tid=%s calendly_event_id=%s"
    )
    assert warning_log.call_args.args[1] == "BOOK_story34_unknown_tid"
    assert warning_log.call_args.args[2] == "story34_unknown_tid"
    assert warning_log.call_args.args[3] == "EVT_story34_unknown_tid"
    assert len(journal_records) == 1
    assert journal_records[0].processing_status == "ignored_unknown_tid"
    assert journal_records[0].delivery_count == 1
    assert journal_records[0].processed_at is not None


def test_calendly_webhook_rejects_invalid_signature_without_routing_or_persisting_bookings():
    payload = json.dumps(
        {
            "event": "invitee.created",
            "payload": {
                "scheduled_event": {"uri": "https://api.calendly.com/scheduled_events/EVT_story32_invalid"},
                "invitee": {"uri": "https://api.calendly.com/scheduled_events/EVT_story32_invalid/invitees/BOOK_story32_invalid"},
                "tracking": {"utm_content": "story32_invalid_tid"},
            },
        }
    ).encode("utf-8")
    capture_router = _CaptureCalendlyWebhookRouter()

    assert _booking_count() == 0

    with TestClient(app) as client:
        with _override_app_state("settings", _StubSettings()):
            with _override_app_state("calendly_webhook_router", capture_router):
                response = client.post(
                    "/webhooks/calendly",
                    content=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Calendly-Webhook-Signature": "t=123,v1=not-a-real-signature",
                    },
                )

    assert response.status_code == 400
    assert response.headers.get("X-Request-Id")
    assert response.json() == {"detail": "invalid calendly webhook signature"}
    assert capture_router.events == []
    assert _booking_count() == 0


def test_calendly_webhook_accepts_verified_unsupported_event_type_as_safe_noop():
    payload = json.dumps(
        {
            "event": "routing_form_submission.created",
            "payload": {
                "event": "https://api.calendly.com/scheduled_events/EVT_story32_noop",
                "uri": "https://api.calendly.com/scheduled_events/EVT_story32_noop/invitees/BOOK_story32_noop",
            },
        }
    ).encode("utf-8")
    signature_header = _calendly_signature_header(
        payload=payload,
        signing_key=_StubSettings.calendly_webhook_signing_key,
    )

    with TestClient(app) as client:
        with _override_app_state("settings", _StubSettings()):
            response = client.post(
                "/webhooks/calendly",
                content=payload,
                headers={
                    "Content-Type": "application/json",
                    "Calendly-Webhook-Signature": signature_header,
                },
            )
    journal_records = _journal_records_for_event_id(calendly_event_id="EVT_story32_noop")

    assert response.status_code == 200
    assert response.headers.get("X-Request-Id")
    assert response.json() == {"status": "ok"}
    assert len(journal_records) == 1
    assert journal_records[0].processing_status == "ignored_unsupported_event"
    assert journal_records[0].delivery_count == 1
