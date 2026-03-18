import hashlib
import hmac
import json
import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Literal, Protocol
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.booking import Booking
from app.models.booking_provider import BOOKING_PROVIDER_CALENDLY
from app.models.calendly_webhook_event import CalendlyWebhookEventRecord
from app.models.content import Content
from app.services.booking_attribution import BOOKING_ATTRIBUTION_STATUS_ATTRIBUTED
from app.services.blocked_billing import resolve_blocked_billing_case_for_booking_canceled
from app.services.billing import (
    BillingInvoiceResult,
    BillingInvoiceVoidResult,
    BillingOrchestrator,
)
from app.services.stripe_provider import StripeProvider, build_default_stripe_provider


logger = logging.getLogger(__name__)

_PROVIDER_TO_INTERNAL_EVENT_TYPES = {
    "invitee.created": "booking.created",
    "invitee.canceled": "booking.canceled",
}


class CalendlyWebhookVerificationError(ValueError):
    pass


class CalendlyWebhookPayloadError(ValueError):
    pass


@dataclass(frozen=True)
class CalendlyWebhookEvent:
    provider_event_type: str
    calendly_event_id: str
    calendly_event_id_path: str
    event_type: str
    calendly_booking_uuid: str
    calendly_booking_uuid_path: str
    tid: str | None
    tid_path: str | None
    payload: dict[str, Any]


@dataclass(frozen=True)
class CanceledBookingContext:
    booking_id: uuid.UUID
    creator_id: uuid.UUID
    booking_link_id: uuid.UUID
    tid: str | None
    calendly_booking_uuid: str
    canceled_at: datetime


CalendlyJournalRecordOutcome = Literal["recorded", "duplicate"]
CalendlyProcessingStatus = Literal[
    "received",
    "processing",
    "applied",
    "deferred_missing_booking",
    "ignored_missing_tid",
    "ignored_unknown_tid",
    "ignored_invalid_payload",
    "ignored_unsupported_event",
    "failed",
]
CalendlyReplayOutcome = Literal["missing", "reprocessed"]


@dataclass(frozen=True)
class CalendlyWebhookJournalRecordResult:
    outcome: CalendlyJournalRecordOutcome
    record_id: uuid.UUID
    delivery_count: int
    processing_status: CalendlyProcessingStatus
    reducer_key: str
    should_schedule_reducer: bool


@dataclass(frozen=True)
class CalendlyWebhookReducerResult:
    processing_status: CalendlyProcessingStatus


@dataclass(frozen=True)
class CalendlyWebhookReplayResult:
    outcome: CalendlyReplayOutcome
    processing_status: CalendlyProcessingStatus | None = None


class CalendlyWebhookRouter(Protocol):
    def record_event(
        self,
        *,
        event: CalendlyWebhookEvent,
    ) -> CalendlyWebhookJournalRecordResult: ...

    def process_event(
        self,
        *,
        record_id: uuid.UUID,
        force: bool = False,
    ) -> CalendlyProcessingStatus | None: ...

    def reprocess_event(self, *, record_id: uuid.UUID) -> CalendlyWebhookReplayResult: ...


_LIVE_RETRYABLE_PROCESSING_STATUSES = frozenset(
    {"received", "deferred_missing_booking", "failed"}
)


class _CalendlyReducerLockRegistry:
    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def acquire(self, *, reducer_key: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.setdefault(reducer_key, threading.Lock())
        lock.acquire()
        return lock


_CALENDLY_REDUCER_LOCKS = _CalendlyReducerLockRegistry()


class UnpaidInvoiceVoider(Protocol):
    def void_unpaid_invoice(self, *, booking: CanceledBookingContext) -> None: ...


class BookingBillingService(Protocol):
    def create_invoice_for_booking(self, *, booking_id: uuid.UUID) -> BillingInvoiceResult: ...

    def void_open_invoice_for_booking(
        self,
        *,
        booking_id: uuid.UUID,
    ) -> BillingInvoiceVoidResult: ...


class NoopUnpaidInvoiceVoider:
    def void_unpaid_invoice(self, *, booking: CanceledBookingContext) -> None:
        logger.info(
            "calendly_webhook_booking_canceled_invoice_void_noop booking_id=%s calendly_booking_uuid=%s creator_id=%s tid=%s",
            booking.booking_id,
            booking.calendly_booking_uuid,
            booking.creator_id,
            booking.tid,
        )


class BillingBackedUnpaidInvoiceVoider:
    def __init__(self, *, billing_service: BookingBillingService):
        self._billing_service = billing_service

    def void_unpaid_invoice(self, *, booking: CanceledBookingContext) -> None:
        result = self._billing_service.void_open_invoice_for_booking(booking_id=booking.booking_id)
        logger.info(
            "calendly_webhook_booking_canceled_invoice_result booking_id=%s calendly_booking_uuid=%s outcome=%s reason=%s invoice_id=%s stripe_invoice_id=%s invoice_status=%s",
            booking.booking_id,
            booking.calendly_booking_uuid,
            result.outcome,
            result.reason,
            result.invoice_id,
            result.stripe_invoice_id,
            result.invoice_status,
        )


class BookingCreatedCalendlyWebhookHandler:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        billing_service: BookingBillingService | None = None,
    ):
        self._session_factory = session_factory
        self._billing_service = billing_service

    def handle_event(self, *, event: CalendlyWebhookEvent) -> CalendlyWebhookReducerResult | None:
        if event.event_type != "booking.created":
            return None

        event_payload = event.payload.get("payload")
        if not isinstance(event_payload, dict):
            return CalendlyWebhookReducerResult(processing_status="ignored_invalid_payload")

        if not event.tid:
            logger.warning(
                "calendly_webhook_booking_created_missing_tid calendly_booking_uuid=%s provider_event_type=%s calendly_event_id=%s",
                event.calendly_booking_uuid,
                event.provider_event_type,
                event.calendly_event_id,
            )
            return CalendlyWebhookReducerResult(processing_status="ignored_missing_tid")

        booking_id_for_billing: uuid.UUID | None = None

        with self._session_factory() as session:
            content = session.scalar(select(Content).where(Content.tid == event.tid))
            if content is None:
                logger.warning(
                    "calendly_webhook_booking_created_unknown_tid calendly_booking_uuid=%s tid=%s calendly_event_id=%s",
                    event.calendly_booking_uuid,
                    event.tid,
                    event.calendly_event_id,
                )
                return CalendlyWebhookReducerResult(processing_status="ignored_unknown_tid")
            creator_id = content.creator_id
            booking_link_id = content.booking_link_id
            resolved_tid = content.tid

            email = _extract_booking_email(event_payload)
            booked_at = _extract_booked_at(event_payload)
            if email is None or booked_at is None:
                logger.warning(
                    "calendly_webhook_booking_created_unhandled calendly_booking_uuid=%s tid=%s missing_email=%s missing_booked_at=%s",
                    event.calendly_booking_uuid,
                    resolved_tid,
                    email is None,
                    booked_at is None,
                )
                return CalendlyWebhookReducerResult(processing_status="ignored_invalid_payload")

            existing_booking = session.scalar(
                select(Booking).where(
                    Booking.calendly_booking_uuid == event.calendly_booking_uuid
                )
            )
            if existing_booking is not None:
                logger.info(
                    "calendly_webhook_booking_created_duplicate calendly_booking_uuid=%s tid=%s",
                    event.calendly_booking_uuid,
                    resolved_tid,
                )
                booking_id_for_billing = existing_booking.id
            else:
                booking = Booking(
                    creator_id=creator_id,
                    booking_link_id=booking_link_id,
                    tid=resolved_tid,
                    provider=BOOKING_PROVIDER_CALENDLY,
                    provider_booking_id=event.calendly_booking_uuid,
                    calendly_booking_uuid=event.calendly_booking_uuid,
                    email=email,
                    booked_at=booked_at,
                    status="created",
                    attribution_status=BOOKING_ATTRIBUTION_STATUS_ATTRIBUTED,
                )
                session.add(booking)

                try:
                    session.commit()
                except IntegrityError:
                    session.rollback()
                    existing_booking = session.scalar(
                        select(Booking).where(
                            Booking.calendly_booking_uuid == event.calendly_booking_uuid
                        )
                    )
                    if existing_booking is None:
                        raise
                    logger.info(
                        "calendly_webhook_booking_created_duplicate calendly_booking_uuid=%s tid=%s",
                        event.calendly_booking_uuid,
                        resolved_tid,
                    )
                    booking_id_for_billing = existing_booking.id
                else:
                    session.refresh(booking)
                    booking_id_for_billing = booking.id
                    logger.info(
                        "calendly_webhook_booking_created_persisted calendly_booking_uuid=%s tid=%s creator_id=%s booking_link_id=%s",
                        event.calendly_booking_uuid,
                        resolved_tid,
                        creator_id,
                        booking_link_id,
                    )

        if booking_id_for_billing is not None and self._billing_service is not None:
            billing_result = self._billing_service.create_invoice_for_booking(
                booking_id=booking_id_for_billing
            )
            logger.info(
                "calendly_webhook_booking_created_invoice_result booking_id=%s calendly_booking_uuid=%s outcome=%s reason=%s invoice_id=%s stripe_invoice_id=%s invoice_status=%s",
                booking_id_for_billing,
                event.calendly_booking_uuid,
                billing_result.outcome,
                billing_result.reason,
                billing_result.invoice_id,
                billing_result.stripe_invoice_id,
                billing_result.invoice_status,
            )
        return CalendlyWebhookReducerResult(processing_status="applied")


class BookingCanceledCalendlyWebhookHandler:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        unpaid_invoice_voider: UnpaidInvoiceVoider,
    ):
        self._session_factory = session_factory
        self._unpaid_invoice_voider = unpaid_invoice_voider

    def handle_event(self, *, event: CalendlyWebhookEvent) -> CalendlyWebhookReducerResult | None:
        if event.event_type != "booking.canceled":
            return None

        event_payload = event.payload.get("payload")
        if not isinstance(event_payload, dict):
            return CalendlyWebhookReducerResult(processing_status="ignored_invalid_payload")

        with self._session_factory() as session:
            booking = session.scalar(
                select(Booking).where(
                    Booking.calendly_booking_uuid == event.calendly_booking_uuid
                )
            )
            if booking is None:
                logger.info(
                    "calendly_webhook_booking_canceled_missing_booking calendly_booking_uuid=%s tid=%s",
                    event.calendly_booking_uuid,
                    event.tid,
                )
                return CalendlyWebhookReducerResult(processing_status="deferred_missing_booking")

            if booking.status == "canceled":
                logger.info(
                    "calendly_webhook_booking_canceled_duplicate calendly_booking_uuid=%s tid=%s",
                    booking.calendly_booking_uuid,
                    booking.tid,
                )
                return CalendlyWebhookReducerResult(processing_status="applied")

            canceled_at = _extract_canceled_at(event_payload) or datetime.now(UTC)
            booking.status = "canceled"
            booking.canceled_at = canceled_at
            resolve_blocked_billing_case_for_booking_canceled(
                session,
                booking_id=booking.id,
                resolved_at=canceled_at,
            )
            session.commit()

            booking_context = CanceledBookingContext(
                booking_id=booking.id,
                creator_id=booking.creator_id,
                booking_link_id=booking.booking_link_id,
                tid=booking.tid,
                calendly_booking_uuid=booking.calendly_booking_uuid,
                canceled_at=canceled_at,
            )

        logger.info(
            "calendly_webhook_booking_canceled_persisted calendly_booking_uuid=%s tid=%s creator_id=%s booking_link_id=%s",
            booking_context.calendly_booking_uuid,
            booking_context.tid,
            booking_context.creator_id,
            booking_context.booking_link_id,
        )
        self._unpaid_invoice_voider.void_unpaid_invoice(booking=booking_context)
        return CalendlyWebhookReducerResult(processing_status="applied")


class DefaultCalendlyWebhookRouter:
    def __init__(
        self,
        *,
        booking_created_handler: BookingCreatedCalendlyWebhookHandler | None = None,
        booking_canceled_handler: BookingCanceledCalendlyWebhookHandler | None = None,
        billing_service: BookingBillingService | None = None,
        unpaid_invoice_voider: UnpaidInvoiceVoider | None = None,
        session_factory: Callable[[], Session] = SessionLocal,
    ):
        self._session_factory = session_factory
        resolved_unpaid_invoice_voider = unpaid_invoice_voider
        if resolved_unpaid_invoice_voider is None:
            if billing_service is None:
                resolved_unpaid_invoice_voider = NoopUnpaidInvoiceVoider()
            else:
                resolved_unpaid_invoice_voider = BillingBackedUnpaidInvoiceVoider(
                    billing_service=billing_service
                )
        self._booking_created_handler = booking_created_handler or BookingCreatedCalendlyWebhookHandler(
            session_factory=session_factory,
            billing_service=billing_service,
        )
        self._booking_canceled_handler = booking_canceled_handler or BookingCanceledCalendlyWebhookHandler(
            session_factory=session_factory,
            unpaid_invoice_voider=resolved_unpaid_invoice_voider,
        )

    def record_event(
        self,
        *,
        event: CalendlyWebhookEvent,
    ) -> CalendlyWebhookJournalRecordResult:
        logger.info(
            "calendly_webhook_event_verified provider_event_type=%s calendly_event_id=%s calendly_event_id_path=%s calendly_booking_uuid=%s calendly_booking_uuid_path=%s event_type=%s tid=%s tid_path=%s",
            event.provider_event_type,
            event.calendly_event_id,
            event.calendly_event_id_path,
            event.calendly_booking_uuid,
            event.calendly_booking_uuid_path,
            event.event_type,
            event.tid,
            event.tid_path,
        )
        journal_result = _record_calendly_webhook_event(
            session_factory=self._session_factory,
            event=event,
        )
        if journal_result.outcome == "duplicate":
            logger.info(
                "calendly_webhook_event_duplicate provider_event_type=%s calendly_event_id=%s calendly_booking_uuid=%s delivery_count=%s processing_status=%s reducer_key=%s should_schedule_reducer=%s",
                event.provider_event_type,
                event.calendly_event_id,
                event.calendly_booking_uuid,
                journal_result.delivery_count,
                journal_result.processing_status,
                journal_result.reducer_key,
                journal_result.should_schedule_reducer,
            )
        return journal_result

    def process_event(
        self,
        *,
        record_id: uuid.UUID,
        force: bool = False,
    ) -> CalendlyProcessingStatus | None:
        record = _load_calendly_webhook_event_record(
            session_factory=self._session_factory,
            record_id=record_id,
        )
        if record is None:
            return None

        reducer_key = record.reducer_key
        reducer_lock = _CALENDLY_REDUCER_LOCKS.acquire(reducer_key=reducer_key)
        try:
            record = _load_calendly_webhook_event_record(
                session_factory=self._session_factory,
                record_id=record_id,
            )
            if record is None:
                return None
            if not force and record.processing_status not in _LIVE_RETRYABLE_PROCESSING_STATUSES:
                return record.processing_status

            event = _calendly_webhook_event_from_record(record)
            _mark_calendly_webhook_event_processing_started(
                session_factory=self._session_factory,
                record_id=record_id,
            )
            try:
                result = self._reduce_event(event=event)
            except Exception as exc:
                logger.exception(
                    "calendly_webhook_event_processing_failed record_id=%s reducer_key=%s",
                    record_id,
                    reducer_key,
                )
                _update_calendly_webhook_event_processing(
                    session_factory=self._session_factory,
                    record_id=record_id,
                    processing_status="failed",
                    last_error=f"{type(exc).__name__}: {exc}",
                )
                return "failed"

            _update_calendly_webhook_event_processing(
                session_factory=self._session_factory,
                record_id=record_id,
                processing_status=result.processing_status,
                last_error=None,
            )
            return result.processing_status
        finally:
            reducer_lock.release()

    def reprocess_event(self, *, record_id: uuid.UUID) -> CalendlyWebhookReplayResult:
        processing_status = self.process_event(record_id=record_id, force=True)
        if processing_status is None:
            return CalendlyWebhookReplayResult(outcome="missing")

        return CalendlyWebhookReplayResult(
            outcome="reprocessed",
            processing_status=processing_status,
        )

    def _reduce_event(self, *, event: CalendlyWebhookEvent) -> CalendlyWebhookReducerResult:
        created_result = self._booking_created_handler.handle_event(event=event)
        if created_result is not None:
            return created_result

        canceled_result = self._booking_canceled_handler.handle_event(event=event)
        if canceled_result is not None:
            return canceled_result

        logger.info(
            "calendly_webhook_event_noop provider_event_type=%s calendly_event_id=%s calendly_event_id_path=%s calendly_booking_uuid=%s calendly_booking_uuid_path=%s event_type=%s tid=%s tid_path=%s",
            event.provider_event_type,
            event.calendly_event_id,
            event.calendly_event_id_path,
            event.calendly_booking_uuid,
            event.calendly_booking_uuid_path,
            event.event_type,
            event.tid,
            event.tid_path,
        )
        return CalendlyWebhookReducerResult(processing_status="ignored_unsupported_event")


DEFAULT_CALENDLY_WEBHOOK_ROUTER = DefaultCalendlyWebhookRouter()


def build_default_calendly_webhook_router(
    *,
    provider: StripeProvider | None = None,
    session_factory: Callable[[], Session] = SessionLocal,
) -> DefaultCalendlyWebhookRouter:
    billing_service = BillingOrchestrator(
        session_factory=session_factory,
        provider=provider or build_default_stripe_provider(),
    )
    return DefaultCalendlyWebhookRouter(
        booking_created_handler=BookingCreatedCalendlyWebhookHandler(
            session_factory=session_factory,
            billing_service=billing_service,
        ),
        booking_canceled_handler=BookingCanceledCalendlyWebhookHandler(
            session_factory=session_factory,
            unpaid_invoice_voider=BillingBackedUnpaidInvoiceVoider(
                billing_service=billing_service
            ),
        ),
        session_factory=session_factory,
    )


def reprocess_calendly_webhook_event(
    *,
    record_id: uuid.UUID,
    provider: StripeProvider | None = None,
    session_factory: Callable[[], Session] = SessionLocal,
) -> CalendlyWebhookReplayResult:
    router = build_default_calendly_webhook_router(
        provider=provider,
        session_factory=session_factory,
    )
    return router.reprocess_event(record_id=record_id)


def verify_and_parse_calendly_webhook(
    *,
    payload: bytes,
    signature_header: str | None,
    signing_key: str,
    tolerance_seconds: int,
    now: datetime | None = None,
) -> CalendlyWebhookEvent:
    timestamp, signatures = _parse_signature_header(signature_header)
    _verify_signature(
        payload=payload,
        timestamp=timestamp,
        signatures=signatures,
        signing_key=signing_key,
        tolerance_seconds=tolerance_seconds,
        now=now,
    )

    try:
        parsed_payload = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise CalendlyWebhookPayloadError("invalid calendly webhook payload") from exc

    provider_event_type = parsed_payload.get("event")
    if not isinstance(provider_event_type, str) or not provider_event_type:
        raise CalendlyWebhookPayloadError("missing calendly event type")

    event_payload = parsed_payload.get("payload")
    if not isinstance(event_payload, dict):
        raise CalendlyWebhookPayloadError("missing calendly payload")

    calendly_event_id, calendly_event_id_path = _extract_calendly_event_id(event_payload)
    if calendly_event_id is None or calendly_event_id_path is None:
        raise CalendlyWebhookPayloadError("missing calendly event id")

    calendly_booking_uuid, calendly_booking_uuid_path = _extract_calendly_booking_uuid(event_payload)
    if calendly_booking_uuid is None or calendly_booking_uuid_path is None:
        raise CalendlyWebhookPayloadError("missing calendly booking uuid")

    tid, tid_path = _extract_tid(event_payload)

    return CalendlyWebhookEvent(
        provider_event_type=provider_event_type,
        calendly_event_id=calendly_event_id,
        calendly_event_id_path=calendly_event_id_path,
        event_type=_PROVIDER_TO_INTERNAL_EVENT_TYPES.get(provider_event_type, provider_event_type),
        calendly_booking_uuid=calendly_booking_uuid,
        calendly_booking_uuid_path=calendly_booking_uuid_path,
        tid=tid,
        tid_path=tid_path,
        payload=parsed_payload,
    )


def _record_calendly_webhook_event(
    *,
    session_factory: Callable[[], Session],
    event: CalendlyWebhookEvent,
    received_at: datetime | None = None,
) -> CalendlyWebhookJournalRecordResult:
    resolved_received_at = received_at or datetime.now(UTC)
    reducer_key = _reducer_key_for_event(event=event)

    with session_factory() as session:
        existing_record = session.scalar(
            select(CalendlyWebhookEventRecord).where(
                CalendlyWebhookEventRecord.provider_event_type == event.provider_event_type,
                CalendlyWebhookEventRecord.calendly_event_id == event.calendly_event_id,
                CalendlyWebhookEventRecord.calendly_booking_uuid == event.calendly_booking_uuid,
            )
        )
        if existing_record is not None:
            existing_record.delivery_count += 1
            existing_record.last_received_at = resolved_received_at
            session.commit()
            return CalendlyWebhookJournalRecordResult(
                outcome="duplicate",
                record_id=existing_record.id,
                delivery_count=existing_record.delivery_count,
                processing_status=existing_record.processing_status,
                reducer_key=existing_record.reducer_key,
                should_schedule_reducer=(
                    existing_record.processing_status in _LIVE_RETRYABLE_PROCESSING_STATUSES
                ),
            )

        record = CalendlyWebhookEventRecord(
            calendly_event_id=event.calendly_event_id,
            provider_event_type=event.provider_event_type,
            event_type=event.event_type,
            calendly_event_id_path=event.calendly_event_id_path,
            calendly_booking_uuid=event.calendly_booking_uuid,
            calendly_booking_uuid_path=event.calendly_booking_uuid_path,
            tid=event.tid,
            tid_path=event.tid_path,
            payload=event.payload,
            reducer_key=reducer_key,
            delivery_count=1,
            processing_status="received",
            reducer_attempt_count=0,
            received_at=resolved_received_at,
            last_received_at=resolved_received_at,
        )
        session.add(record)

        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            existing_record = session.scalar(
                select(CalendlyWebhookEventRecord).where(
                    CalendlyWebhookEventRecord.provider_event_type == event.provider_event_type,
                    CalendlyWebhookEventRecord.calendly_event_id == event.calendly_event_id,
                    CalendlyWebhookEventRecord.calendly_booking_uuid == event.calendly_booking_uuid,
                )
            )
            if existing_record is None:
                raise
            existing_record.delivery_count += 1
            existing_record.last_received_at = resolved_received_at
            session.commit()
            return CalendlyWebhookJournalRecordResult(
                outcome="duplicate",
                record_id=existing_record.id,
                delivery_count=existing_record.delivery_count,
                processing_status=existing_record.processing_status,
                reducer_key=existing_record.reducer_key,
                should_schedule_reducer=(
                    existing_record.processing_status in _LIVE_RETRYABLE_PROCESSING_STATUSES
                ),
            )

        return CalendlyWebhookJournalRecordResult(
            outcome="recorded",
            record_id=record.id,
            delivery_count=record.delivery_count,
            processing_status=record.processing_status,
            reducer_key=record.reducer_key,
            should_schedule_reducer=True,
        )


def _load_calendly_webhook_event_record(
    *,
    session_factory: Callable[[], Session],
    record_id: uuid.UUID,
) -> CalendlyWebhookEventRecord | None:
    with session_factory() as session:
        return session.scalar(
            select(CalendlyWebhookEventRecord).where(
                CalendlyWebhookEventRecord.id == record_id
            )
        )


def _calendly_webhook_event_from_record(
    record: CalendlyWebhookEventRecord,
) -> CalendlyWebhookEvent:
    return CalendlyWebhookEvent(
        provider_event_type=record.provider_event_type,
        calendly_event_id=record.calendly_event_id,
        calendly_event_id_path=record.calendly_event_id_path,
        event_type=record.event_type,
        calendly_booking_uuid=record.calendly_booking_uuid,
        calendly_booking_uuid_path=record.calendly_booking_uuid_path,
        tid=record.tid,
        tid_path=record.tid_path,
        payload=record.payload,
    )


def _mark_calendly_webhook_event_processing_started(
    *,
    session_factory: Callable[[], Session],
    record_id: uuid.UUID,
) -> None:
    with session_factory() as session:
        record = session.scalar(
            select(CalendlyWebhookEventRecord).where(
                CalendlyWebhookEventRecord.id == record_id
            )
        )
        if record is None:
            raise ValueError(f"missing calendly webhook journal row for {record_id}")
        record.processing_status = "processing"
        record.reducer_attempt_count += 1
        record.last_error = None
        record.processed_at = None
        session.commit()


def _update_calendly_webhook_event_processing(
    *,
    session_factory: Callable[[], Session],
    record_id: uuid.UUID,
    processing_status: CalendlyProcessingStatus,
    last_error: str | None,
) -> None:
    with session_factory() as session:
        record = session.scalar(
            select(CalendlyWebhookEventRecord).where(
                CalendlyWebhookEventRecord.id == record_id
            )
        )
        if record is None:
            raise ValueError(f"missing calendly webhook journal row for {record_id}")
        record.processing_status = processing_status
        record.last_error = last_error
        record.processed_at = datetime.now(UTC)
        session.commit()


def _reducer_key_for_event(*, event: CalendlyWebhookEvent) -> str:
    return f"booking:{event.calendly_booking_uuid}"


def _parse_signature_header(signature_header: str | None) -> tuple[int, list[str]]:
    if not signature_header:
        raise CalendlyWebhookVerificationError("missing calendly signature header")

    timestamp: int | None = None
    signatures: list[str] = []
    for item in signature_header.split(","):
        key, separator, value = item.partition("=")
        if separator != "=" or not value:
            continue
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError as exc:
                raise CalendlyWebhookVerificationError(
                    "invalid calendly signature timestamp"
                ) from exc
        elif key == "v1":
            signatures.append(value)

    if timestamp is None or not signatures:
        raise CalendlyWebhookVerificationError("invalid calendly signature header")

    return timestamp, signatures


def _verify_signature(
    *,
    payload: bytes,
    timestamp: int,
    signatures: list[str],
    signing_key: str,
    tolerance_seconds: int,
    now: datetime | None,
) -> None:
    current_time = now or datetime.now(UTC)
    if abs(int(current_time.timestamp()) - timestamp) > tolerance_seconds:
        raise CalendlyWebhookVerificationError("calendly signature timestamp outside tolerance")

    signed_payload = f"{timestamp}.".encode("utf-8") + payload
    expected_signature = hmac.new(
        signing_key.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()
    if not any(hmac.compare_digest(expected_signature, signature) for signature in signatures):
        raise CalendlyWebhookVerificationError("invalid calendly signature")


def _extract_calendly_event_id(event_payload: dict[str, Any]) -> tuple[str | None, str | None]:
    return _resource_identifier(
        ("payload.event", event_payload.get("event")),
        ("payload.scheduled_event", event_payload.get("scheduled_event")),
    )


def _extract_calendly_booking_uuid(event_payload: dict[str, Any]) -> tuple[str | None, str | None]:
    return _resource_identifier(
        ("payload.uri", event_payload.get("uri")),
        ("payload.invitee", event_payload.get("invitee")),
        ("payload.invitee_uri", event_payload.get("invitee_uri")),
        ("payload.uuid", event_payload.get("uuid")),
    )


def _extract_tid(event_payload: dict[str, Any]) -> tuple[str | None, str | None]:
    tracking = event_payload.get("tracking")
    if isinstance(tracking, dict):
        for key in ("utm_content", "tid"):
            value = tracking.get(key)
            if isinstance(value, str) and value:
                return value, f"payload.tracking.{key}"

    for key in ("tid", "utm_content"):
        value = event_payload.get(key)
        if isinstance(value, str) and value:
            return value, f"payload.{key}"

    return None, None


def _extract_booking_email(event_payload: dict[str, Any]) -> str | None:
    email = event_payload.get("email")
    if isinstance(email, str) and email:
        return email

    invitee = event_payload.get("invitee")
    if isinstance(invitee, dict):
        nested_email = invitee.get("email")
        if isinstance(nested_email, str) and nested_email:
            return nested_email

    return None


def _extract_booked_at(event_payload: dict[str, Any]) -> datetime | None:
    for candidate in (
        event_payload.get("created_at"),
        event_payload.get("start_time"),
    ):
        parsed = _parse_datetime(candidate)
        if parsed is not None:
            return parsed

    scheduled_event = event_payload.get("scheduled_event")
    if isinstance(scheduled_event, dict):
        parsed = _parse_datetime(scheduled_event.get("start_time"))
        if parsed is not None:
            return parsed

    event_value = event_payload.get("event")
    if isinstance(event_value, dict):
        parsed = _parse_datetime(event_value.get("start_time"))
        if parsed is not None:
            return parsed

    return None


def _extract_canceled_at(event_payload: dict[str, Any]) -> datetime | None:
    for candidate in (
        event_payload.get("canceled_at"),
        event_payload.get("cancelled_at"),
        event_payload.get("updated_at"),
    ):
        parsed = _parse_datetime(candidate)
        if parsed is not None:
            return parsed

    return None


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None

    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)

    return parsed


def _resource_identifier(*candidates: tuple[str, Any]) -> tuple[str | None, str | None]:
    for path, candidate in candidates:
        identifier, identifier_path = _identifier_from_candidate(path, candidate)
        if identifier:
            return identifier, identifier_path
    return None, None


def _identifier_from_candidate(path: str, candidate: Any) -> tuple[str | None, str | None]:
    if isinstance(candidate, str) and candidate:
        return _identifier_from_string(path, candidate)

    if isinstance(candidate, dict):
        uuid_value = candidate.get("uuid")
        if isinstance(uuid_value, str) and uuid_value:
            return uuid_value, f"{path}.uuid"
        uri_value = candidate.get("uri")
        if isinstance(uri_value, str) and uri_value:
            return _identifier_from_string(f"{path}.uri", uri_value)

    return None, None


def _identifier_from_string(source_path: str, value: str) -> tuple[str | None, str | None]:
    if "://" not in value:
        return value, source_path

    parsed = urlsplit(value)
    resource_path = parsed.path.rstrip("/")
    if not resource_path:
        return None, None

    identifier = resource_path.rsplit("/", 1)[-1]
    if not identifier:
        return None, None
    return identifier, source_path
