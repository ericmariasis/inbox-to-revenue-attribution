import hashlib
import hmac
import json
import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Literal, Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.booking import Booking
from app.models.booking_link import BookingLink
from app.models.booking_provider import BOOKING_PROVIDER_FULLSCOPE
from app.models.content import Content
from app.models.fullscope_webhook_event import FullScopeWebhookEventRecord
from app.services.billing_provider import BillingProvider
from app.services.booking_attribution import BOOKING_ATTRIBUTION_STATUS_ATTRIBUTED
from app.services.blocked_billing import resolve_blocked_billing_case_for_booking_canceled
from app.services.billing import (
    BillingInvoiceResult,
    BillingInvoiceVoidResult,
    BillingOrchestrator,
)
from app.services.billing_provider import build_billing_provider_registry
from app.services.paypal_provider import build_default_paypal_provider
from app.services.stripe_provider import build_default_stripe_provider


logger = logging.getLogger(__name__)

_FULLSCOPE_CONFIRMED_STATUSES = frozenset({"confirmed"})
_FULLSCOPE_CANCELED_STATUSES = frozenset({"cancelled", "canceled"})


class FullScopeWebhookVerificationError(ValueError):
    pass


class FullScopeWebhookPayloadError(ValueError):
    pass


@dataclass(frozen=True)
class FullScopeWebhookEvent:
    provider_event_type: str
    event_type: str
    appointment_id: str
    appointment_id_path: str
    calendar_id: str
    calendar_id_path: str
    workflow_id: str | None
    workflow_id_path: str | None
    tid: str | None
    tid_path: str | None
    payload: dict[str, Any]
    payload_sha256: str
    occurred_at: datetime | None = None


@dataclass(frozen=True)
class FullScopeCanceledBookingContext:
    booking_id: uuid.UUID
    creator_id: uuid.UUID
    booking_link_id: uuid.UUID
    tid: str | None
    provider_booking_id: str
    canceled_at: datetime


FullScopeJournalRecordOutcome = Literal["recorded", "duplicate"]
FullScopeProcessingStatus = Literal[
    "received",
    "processing",
    "applied",
    "deferred_missing_booking",
    "ignored_missing_tid",
    "ignored_unknown_tid",
    "ignored_invalid_payload",
    "ignored_unsupported_event",
    "ignored_unsupported_source",
    "failed",
]
FullScopeReplayOutcome = Literal["missing", "reprocessed"]


@dataclass(frozen=True)
class FullScopeWebhookJournalRecordResult:
    outcome: FullScopeJournalRecordOutcome
    record_id: uuid.UUID
    delivery_count: int
    processing_status: FullScopeProcessingStatus
    reducer_key: str
    should_schedule_reducer: bool


@dataclass(frozen=True)
class FullScopeWebhookReducerResult:
    processing_status: FullScopeProcessingStatus


@dataclass(frozen=True)
class FullScopeWebhookReplayResult:
    outcome: FullScopeReplayOutcome
    processing_status: FullScopeProcessingStatus | None = None


class FullScopeWebhookRouter(Protocol):
    def record_event(
        self,
        *,
        event: FullScopeWebhookEvent,
    ) -> FullScopeWebhookJournalRecordResult: ...

    def process_event(
        self,
        *,
        record_id: uuid.UUID,
        force: bool = False,
    ) -> FullScopeProcessingStatus | None: ...

    def reprocess_event(self, *, record_id: uuid.UUID) -> FullScopeWebhookReplayResult: ...


class UnpaidInvoiceVoider(Protocol):
    def void_unpaid_invoice(self, *, booking: FullScopeCanceledBookingContext) -> None: ...


class NoopUnpaidInvoiceVoider:
    def void_unpaid_invoice(self, *, booking: FullScopeCanceledBookingContext) -> None:
        logger.info(
            "fullscope_webhook_booking_canceled_invoice_void_noop booking_id=%s provider_booking_id=%s creator_id=%s tid=%s",
            booking.booking_id,
            booking.provider_booking_id,
            booking.creator_id,
            booking.tid,
        )


class BookingBillingService(Protocol):
    def create_invoice_for_booking(self, *, booking_id: uuid.UUID) -> BillingInvoiceResult: ...

    def void_open_invoice_for_booking(
        self,
        *,
        booking_id: uuid.UUID,
    ) -> BillingInvoiceVoidResult: ...


class BillingBackedUnpaidInvoiceVoider:
    def __init__(self, *, billing_service: BookingBillingService):
        self._billing_service = billing_service

    def void_unpaid_invoice(self, *, booking: FullScopeCanceledBookingContext) -> None:
        result = self._billing_service.void_open_invoice_for_booking(booking_id=booking.booking_id)
        logger.info(
            "fullscope_webhook_booking_canceled_invoice_result booking_id=%s provider_booking_id=%s outcome=%s reason=%s invoice_id=%s provider_invoice_id=%s invoice_status=%s",
            booking.booking_id,
            booking.provider_booking_id,
            result.outcome,
            result.reason,
            result.invoice_id,
            result.provider_invoice_id,
            result.invoice_status,
        )


_LIVE_RETRYABLE_PROCESSING_STATUSES = frozenset(
    {"received", "deferred_missing_booking", "failed"}
)


class _FullScopeReducerLockRegistry:
    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def acquire(self, *, reducer_key: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.setdefault(reducer_key, threading.Lock())
        lock.acquire()
        return lock


_FULLSCOPE_REDUCER_LOCKS = _FullScopeReducerLockRegistry()


class BookingCreatedFullScopeWebhookHandler:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        billing_service: BookingBillingService | None = None,
    ):
        self._session_factory = session_factory
        self._billing_service = billing_service

    def handle_event(self, *, event: FullScopeWebhookEvent) -> FullScopeWebhookReducerResult | None:
        if event.event_type != "booking.created":
            return None

        booking_id_for_billing: uuid.UUID | None = None

        with self._session_factory() as session:
            existing_booking = session.scalar(
                select(Booking).where(
                    Booking.provider == BOOKING_PROVIDER_FULLSCOPE,
                    Booking.provider_booking_id == event.appointment_id,
                )
            )
            if existing_booking is not None:
                email = _extract_booking_email(event.payload) or existing_booking.email
                booked_at = _extract_booked_at(event.payload) or existing_booking.booked_at
                if not email or booked_at is None:
                    logger.warning(
                        "fullscope_webhook_booking_created_unhandled appointment_id=%s tid=%s missing_email=%s missing_booked_at=%s",
                        event.appointment_id,
                        event.tid,
                        not email,
                        booked_at is None,
                    )
                    return FullScopeWebhookReducerResult(processing_status="ignored_invalid_payload")

                if existing_booking.status == "canceled":
                    logger.info(
                        "fullscope_webhook_booking_created_stale_after_cancel appointment_id=%s tid=%s creator_id=%s booking_link_id=%s",
                        event.appointment_id,
                        existing_booking.tid,
                        existing_booking.creator_id,
                        existing_booking.booking_link_id,
                    )
                    return FullScopeWebhookReducerResult(processing_status="applied")

                existing_booking.email = email
                existing_booking.booked_at = booked_at
                existing_booking.status = "created"
                existing_booking.canceled_at = None
                session.commit()
                booking_id_for_billing = existing_booking.id
                logger.info(
                    "fullscope_webhook_booking_created_updated appointment_id=%s tid=%s creator_id=%s booking_link_id=%s",
                    event.appointment_id,
                    existing_booking.tid,
                    existing_booking.creator_id,
                    existing_booking.booking_link_id,
                )
                return FullScopeWebhookReducerResult(processing_status="applied")

            if not event.tid:
                logger.warning(
                    "fullscope_webhook_booking_created_missing_tid appointment_id=%s provider_event_type=%s calendar_id=%s",
                    event.appointment_id,
                    event.provider_event_type,
                    event.calendar_id,
                )
                return FullScopeWebhookReducerResult(processing_status="ignored_missing_tid")

            content_row = session.execute(
                select(Content, BookingLink.provider)
                .join(BookingLink, BookingLink.id == Content.booking_link_id)
                .where(Content.tid == event.tid)
            ).one_or_none()
            if content_row is None:
                logger.warning(
                    "fullscope_webhook_booking_created_unknown_tid appointment_id=%s tid=%s calendar_id=%s",
                    event.appointment_id,
                    event.tid,
                    event.calendar_id,
                )
                return FullScopeWebhookReducerResult(processing_status="ignored_unknown_tid")

            content, booking_provider = content_row
            if booking_provider != BOOKING_PROVIDER_FULLSCOPE:
                logger.warning(
                    "fullscope_webhook_booking_created_unsupported_source appointment_id=%s tid=%s booking_link_id=%s provider=%s",
                    event.appointment_id,
                    event.tid,
                    content.booking_link_id,
                    booking_provider,
                )
                return FullScopeWebhookReducerResult(
                    processing_status="ignored_unsupported_source"
                )

            email = _extract_booking_email(event.payload)
            booked_at = _extract_booked_at(event.payload)
            if email is None or booked_at is None:
                logger.warning(
                    "fullscope_webhook_booking_created_unhandled appointment_id=%s tid=%s missing_email=%s missing_booked_at=%s",
                    event.appointment_id,
                    event.tid,
                    email is None,
                    booked_at is None,
                )
                return FullScopeWebhookReducerResult(processing_status="ignored_invalid_payload")

            booking = Booking(
                creator_id=content.creator_id,
                booking_link_id=content.booking_link_id,
                tid=content.tid,
                provider=BOOKING_PROVIDER_FULLSCOPE,
                provider_booking_id=event.appointment_id,
                calendly_booking_uuid=None,
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
                        Booking.provider == BOOKING_PROVIDER_FULLSCOPE,
                        Booking.provider_booking_id == event.appointment_id,
                    )
                )
                if existing_booking is None:
                    raise
                logger.info(
                    "fullscope_webhook_booking_created_duplicate appointment_id=%s tid=%s",
                    event.appointment_id,
                    event.tid,
                )
                booking_id_for_billing = existing_booking.id
            else:
                session.refresh(booking)
                booking_id_for_billing = booking.id
                logger.info(
                    "fullscope_webhook_booking_created_persisted appointment_id=%s tid=%s creator_id=%s booking_link_id=%s",
                    event.appointment_id,
                    content.tid,
                    content.creator_id,
                    content.booking_link_id,
                )
        if booking_id_for_billing is not None and self._billing_service is not None:
            billing_result = self._billing_service.create_invoice_for_booking(
                booking_id=booking_id_for_billing
            )
            logger.info(
                "fullscope_webhook_booking_created_invoice_result booking_id=%s provider_booking_id=%s outcome=%s reason=%s invoice_id=%s provider_invoice_id=%s invoice_status=%s",
                booking_id_for_billing,
                event.appointment_id,
                billing_result.outcome,
                billing_result.reason,
                billing_result.invoice_id,
                billing_result.provider_invoice_id,
                billing_result.invoice_status,
            )
        return FullScopeWebhookReducerResult(processing_status="applied")


class BookingCanceledFullScopeWebhookHandler:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        unpaid_invoice_voider: UnpaidInvoiceVoider,
    ):
        self._session_factory = session_factory
        self._unpaid_invoice_voider = unpaid_invoice_voider

    def handle_event(self, *, event: FullScopeWebhookEvent) -> FullScopeWebhookReducerResult | None:
        if event.event_type != "booking.canceled":
            return None

        with self._session_factory() as session:
            booking = session.scalar(
                select(Booking).where(
                    Booking.provider == BOOKING_PROVIDER_FULLSCOPE,
                    Booking.provider_booking_id == event.appointment_id,
                )
            )
            if booking is None:
                logger.info(
                    "fullscope_webhook_booking_canceled_missing_booking appointment_id=%s tid=%s calendar_id=%s",
                    event.appointment_id,
                    event.tid,
                    event.calendar_id,
                )
                return FullScopeWebhookReducerResult(processing_status="deferred_missing_booking")

            if booking.status == "canceled":
                logger.info(
                    "fullscope_webhook_booking_canceled_duplicate appointment_id=%s tid=%s",
                    booking.provider_booking_id,
                    booking.tid,
                )
                return FullScopeWebhookReducerResult(processing_status="applied")

            canceled_at = event.occurred_at or datetime.now(UTC)
            booking.status = "canceled"
            booking.canceled_at = canceled_at
            resolve_blocked_billing_case_for_booking_canceled(
                session,
                booking_id=booking.id,
                resolved_at=canceled_at,
            )
            session.commit()

            booking_context = FullScopeCanceledBookingContext(
                booking_id=booking.id,
                creator_id=booking.creator_id,
                booking_link_id=booking.booking_link_id,
                tid=booking.tid,
                provider_booking_id=booking.provider_booking_id or event.appointment_id,
                canceled_at=canceled_at,
            )

        logger.info(
            "fullscope_webhook_booking_canceled_persisted appointment_id=%s tid=%s creator_id=%s booking_link_id=%s",
            booking_context.provider_booking_id,
            booking_context.tid,
            booking_context.creator_id,
            booking_context.booking_link_id,
        )
        self._unpaid_invoice_voider.void_unpaid_invoice(booking=booking_context)
        return FullScopeWebhookReducerResult(processing_status="applied")


class DefaultFullScopeWebhookRouter:
    def __init__(
        self,
        *,
        booking_created_handler: BookingCreatedFullScopeWebhookHandler | None = None,
        booking_canceled_handler: BookingCanceledFullScopeWebhookHandler | None = None,
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
        self._booking_created_handler = booking_created_handler or BookingCreatedFullScopeWebhookHandler(
            session_factory=session_factory,
            billing_service=billing_service,
        )
        self._booking_canceled_handler = booking_canceled_handler or BookingCanceledFullScopeWebhookHandler(
            session_factory=session_factory,
            unpaid_invoice_voider=resolved_unpaid_invoice_voider,
        )

    def record_event(
        self,
        *,
        event: FullScopeWebhookEvent,
    ) -> FullScopeWebhookJournalRecordResult:
        logger.info(
            "fullscope_webhook_event_verified provider_event_type=%s appointment_id=%s appointment_id_path=%s calendar_id=%s calendar_id_path=%s event_type=%s tid=%s tid_path=%s workflow_id=%s workflow_id_path=%s payload_sha256=%s",
            event.provider_event_type,
            event.appointment_id,
            event.appointment_id_path,
            event.calendar_id,
            event.calendar_id_path,
            event.event_type,
            event.tid,
            event.tid_path,
            event.workflow_id,
            event.workflow_id_path,
            event.payload_sha256,
        )
        journal_result = _record_fullscope_webhook_event(
            session_factory=self._session_factory,
            event=event,
        )
        if journal_result.outcome == "duplicate":
            logger.info(
                "fullscope_webhook_event_duplicate provider_event_type=%s appointment_id=%s delivery_count=%s processing_status=%s reducer_key=%s should_schedule_reducer=%s",
                event.provider_event_type,
                event.appointment_id,
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
    ) -> FullScopeProcessingStatus | None:
        record = _load_fullscope_webhook_event_record(
            session_factory=self._session_factory,
            record_id=record_id,
        )
        if record is None:
            return None

        reducer_lock = _FULLSCOPE_REDUCER_LOCKS.acquire(reducer_key=record.reducer_key)
        try:
            record = _load_fullscope_webhook_event_record(
                session_factory=self._session_factory,
                record_id=record_id,
            )
            if record is None:
                return None
            if not force and record.processing_status not in _LIVE_RETRYABLE_PROCESSING_STATUSES:
                return record.processing_status

            event = _fullscope_webhook_event_from_record(record)
            _mark_fullscope_webhook_event_processing_started(
                session_factory=self._session_factory,
                record_id=record_id,
            )
            try:
                result = self._reduce_event(event=event)
            except Exception as exc:
                logger.exception(
                    "fullscope_webhook_event_processing_failed record_id=%s reducer_key=%s",
                    record_id,
                    record.reducer_key,
                )
                _update_fullscope_webhook_event_processing(
                    session_factory=self._session_factory,
                    record_id=record_id,
                    processing_status="failed",
                    last_error=f"{type(exc).__name__}: {exc}",
                )
                return "failed"

            _update_fullscope_webhook_event_processing(
                session_factory=self._session_factory,
                record_id=record_id,
                processing_status=result.processing_status,
                last_error=None,
            )
            return result.processing_status
        finally:
            reducer_lock.release()

    def reprocess_event(self, *, record_id: uuid.UUID) -> FullScopeWebhookReplayResult:
        processing_status = self.process_event(record_id=record_id, force=True)
        if processing_status is None:
            return FullScopeWebhookReplayResult(outcome="missing")
        return FullScopeWebhookReplayResult(
            outcome="reprocessed",
            processing_status=processing_status,
        )

    def _reduce_event(self, *, event: FullScopeWebhookEvent) -> FullScopeWebhookReducerResult:
        created_result = self._booking_created_handler.handle_event(event=event)
        if created_result is not None:
            return created_result

        canceled_result = self._booking_canceled_handler.handle_event(event=event)
        if canceled_result is not None:
            return canceled_result

        logger.info(
            "fullscope_webhook_event_noop provider_event_type=%s appointment_id=%s appointment_id_path=%s calendar_id=%s calendar_id_path=%s event_type=%s tid=%s tid_path=%s workflow_id=%s workflow_id_path=%s payload_sha256=%s",
            event.provider_event_type,
            event.appointment_id,
            event.appointment_id_path,
            event.calendar_id,
            event.calendar_id_path,
            event.event_type,
            event.tid,
            event.tid_path,
            event.workflow_id,
            event.workflow_id_path,
            event.payload_sha256,
        )
        return FullScopeWebhookReducerResult(processing_status="ignored_unsupported_event")


DEFAULT_FULLSCOPE_WEBHOOK_ROUTER = DefaultFullScopeWebhookRouter()


def build_default_fullscope_webhook_router(
    *,
    provider: BillingProvider | None = None,
    providers: dict[str, BillingProvider] | None = None,
    session_factory: Callable[[], Session] = SessionLocal,
) -> DefaultFullScopeWebhookRouter:
    if providers is not None:
        billing_service = BillingOrchestrator(
            session_factory=session_factory,
            providers=providers,
        )
    elif provider is not None:
        billing_service = BillingOrchestrator(
            session_factory=session_factory,
            provider=provider,
        )
    else:
        billing_service = BillingOrchestrator(
            session_factory=session_factory,
            providers=build_billing_provider_registry(
                providers=[
                    build_default_stripe_provider(),
                    build_default_paypal_provider(),
                ]
            ),
        )
    return DefaultFullScopeWebhookRouter(
        booking_created_handler=BookingCreatedFullScopeWebhookHandler(
            session_factory=session_factory,
            billing_service=billing_service,
        ),
        booking_canceled_handler=BookingCanceledFullScopeWebhookHandler(
            session_factory=session_factory,
            unpaid_invoice_voider=BillingBackedUnpaidInvoiceVoider(
                billing_service=billing_service
            ),
        ),
        session_factory=session_factory,
    )


def verify_and_parse_fullscope_webhook(
    *,
    payload: bytes,
    authorization_header: str | None,
    shared_secret: str,
) -> FullScopeWebhookEvent:
    _verify_fullscope_authorization(
        authorization_header=authorization_header,
        shared_secret=shared_secret,
    )

    try:
        parsed_payload = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise FullScopeWebhookPayloadError("invalid fullscope webhook payload") from exc

    if not isinstance(parsed_payload, dict):
        raise FullScopeWebhookPayloadError("fullscope webhook payload must be an object")

    appointment_id, appointment_id_path = _extract_appointment_id(parsed_payload)
    if appointment_id is None or appointment_id_path is None:
        raise FullScopeWebhookPayloadError("missing fullscope appointment id")

    calendar_id, calendar_id_path = _extract_calendar_id(parsed_payload)
    if calendar_id is None or calendar_id_path is None:
        raise FullScopeWebhookPayloadError("missing fullscope calendar id")

    workflow_id, workflow_id_path = _extract_workflow_id(parsed_payload)
    tid, tid_path = _extract_tid(parsed_payload)
    provider_event_type = _provider_event_type(parsed_payload)

    return FullScopeWebhookEvent(
        provider_event_type=provider_event_type,
        event_type=_event_type(parsed_payload, provider_event_type=provider_event_type),
        appointment_id=appointment_id,
        appointment_id_path=appointment_id_path,
        calendar_id=calendar_id,
        calendar_id_path=calendar_id_path,
        workflow_id=workflow_id,
        workflow_id_path=workflow_id_path,
        tid=tid,
        tid_path=tid_path,
        payload=parsed_payload,
        payload_sha256=_payload_sha256(parsed_payload),
    )


def _record_fullscope_webhook_event(
    *,
    session_factory: Callable[[], Session],
    event: FullScopeWebhookEvent,
    received_at: datetime | None = None,
) -> FullScopeWebhookJournalRecordResult:
    resolved_received_at = received_at or datetime.now(UTC)
    reducer_key = _reducer_key_for_event(event=event)

    with session_factory() as session:
        existing_record = session.scalar(
            select(FullScopeWebhookEventRecord).where(
                FullScopeWebhookEventRecord.provider_event_type == event.provider_event_type,
                FullScopeWebhookEventRecord.appointment_id == event.appointment_id,
                FullScopeWebhookEventRecord.payload_sha256 == event.payload_sha256,
            )
        )
        if existing_record is not None:
            existing_record.delivery_count += 1
            existing_record.last_received_at = resolved_received_at
            session.commit()
            return FullScopeWebhookJournalRecordResult(
                outcome="duplicate",
                record_id=existing_record.id,
                delivery_count=existing_record.delivery_count,
                processing_status=existing_record.processing_status,
                reducer_key=existing_record.reducer_key,
                should_schedule_reducer=(
                    existing_record.processing_status in _LIVE_RETRYABLE_PROCESSING_STATUSES
                ),
            )

        record = FullScopeWebhookEventRecord(
            provider_event_type=event.provider_event_type,
            event_type=event.event_type,
            appointment_id=event.appointment_id,
            appointment_id_path=event.appointment_id_path,
            calendar_id=event.calendar_id,
            calendar_id_path=event.calendar_id_path,
            workflow_id=event.workflow_id,
            workflow_id_path=event.workflow_id_path,
            tid=event.tid,
            tid_path=event.tid_path,
            payload=event.payload,
            payload_sha256=event.payload_sha256,
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
                select(FullScopeWebhookEventRecord).where(
                    FullScopeWebhookEventRecord.provider_event_type == event.provider_event_type,
                    FullScopeWebhookEventRecord.appointment_id == event.appointment_id,
                    FullScopeWebhookEventRecord.payload_sha256 == event.payload_sha256,
                )
            )
            if existing_record is None:
                raise
            existing_record.delivery_count += 1
            existing_record.last_received_at = resolved_received_at
            session.commit()
            return FullScopeWebhookJournalRecordResult(
                outcome="duplicate",
                record_id=existing_record.id,
                delivery_count=existing_record.delivery_count,
                processing_status=existing_record.processing_status,
                reducer_key=existing_record.reducer_key,
                should_schedule_reducer=(
                    existing_record.processing_status in _LIVE_RETRYABLE_PROCESSING_STATUSES
                ),
            )

        return FullScopeWebhookJournalRecordResult(
            outcome="recorded",
            record_id=record.id,
            delivery_count=record.delivery_count,
            processing_status=record.processing_status,
            reducer_key=record.reducer_key,
            should_schedule_reducer=True,
        )


def _load_fullscope_webhook_event_record(
    *,
    session_factory: Callable[[], Session],
    record_id: uuid.UUID,
) -> FullScopeWebhookEventRecord | None:
    with session_factory() as session:
        return session.scalar(
            select(FullScopeWebhookEventRecord).where(FullScopeWebhookEventRecord.id == record_id)
        )


def _fullscope_webhook_event_from_record(
    record: FullScopeWebhookEventRecord,
) -> FullScopeWebhookEvent:
    return FullScopeWebhookEvent(
        provider_event_type=record.provider_event_type,
        event_type=record.event_type,
        appointment_id=record.appointment_id,
        appointment_id_path=record.appointment_id_path,
        calendar_id=record.calendar_id,
        calendar_id_path=record.calendar_id_path,
        workflow_id=record.workflow_id,
        workflow_id_path=record.workflow_id_path,
        tid=record.tid,
        tid_path=record.tid_path,
        payload=record.payload,
        payload_sha256=record.payload_sha256,
        occurred_at=record.received_at,
    )


def _mark_fullscope_webhook_event_processing_started(
    *,
    session_factory: Callable[[], Session],
    record_id: uuid.UUID,
) -> None:
    with session_factory() as session:
        record = session.scalar(
            select(FullScopeWebhookEventRecord).where(FullScopeWebhookEventRecord.id == record_id)
        )
        if record is None:
            raise ValueError(f"missing fullscope webhook journal row for {record_id}")
        record.processing_status = "processing"
        record.reducer_attempt_count += 1
        record.last_error = None
        record.processed_at = None
        session.commit()


def _update_fullscope_webhook_event_processing(
    *,
    session_factory: Callable[[], Session],
    record_id: uuid.UUID,
    processing_status: FullScopeProcessingStatus,
    last_error: str | None,
) -> None:
    with session_factory() as session:
        record = session.scalar(
            select(FullScopeWebhookEventRecord).where(FullScopeWebhookEventRecord.id == record_id)
        )
        if record is None:
            raise ValueError(f"missing fullscope webhook journal row for {record_id}")
        record.processing_status = processing_status
        record.last_error = last_error
        record.processed_at = datetime.now(UTC)
        session.commit()


def _reducer_key_for_event(*, event: FullScopeWebhookEvent) -> str:
    return f"booking:{event.appointment_id}"


def _verify_fullscope_authorization(
    *,
    authorization_header: str | None,
    shared_secret: str,
) -> None:
    if not authorization_header:
        raise FullScopeWebhookVerificationError("missing fullscope authorization header")
    scheme, separator, token = authorization_header.partition(" ")
    if separator != " " or scheme.strip().lower() != "bearer" or not token.strip():
        raise FullScopeWebhookVerificationError("invalid fullscope authorization header")
    if not hmac.compare_digest(token.strip(), shared_secret.strip()):
        raise FullScopeWebhookVerificationError("invalid fullscope shared secret")


def _extract_appointment_id(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    calendar = payload.get("calendar")
    if isinstance(calendar, dict):
        return _extract_string(calendar, ("appointmentId", "calendar.appointmentId"))
    return None, None


def _extract_calendar_id(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    calendar = payload.get("calendar")
    if isinstance(calendar, dict):
        return _extract_string(calendar, ("id", "calendar.id"))
    return None, None


def _extract_workflow_id(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    workflow = payload.get("workflow")
    if isinstance(workflow, dict):
        return _extract_string(workflow, ("id", "workflow.id"))
    return None, None


def _extract_tid(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    for key in ("ccp_attribution_tid", "attribution_tid", "tid"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip(), key

    custom_data = payload.get("customData")
    if isinstance(custom_data, dict):
        for key in ("attribution_tid", "ccp_attribution_tid", "tid"):
            value = custom_data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip(), f"customData.{key}"

    return None, None


def _provider_event_type(payload: dict[str, Any]) -> str:
    appointment_status = _extract_appointment_status(payload)
    if appointment_status is not None:
        return f"appointment.{appointment_status}"

    booking_status = _extract_booking_status(payload)
    if booking_status is not None:
        return f"calendar.{booking_status}"

    return "calendar.unknown"


def _event_type(payload: dict[str, Any], *, provider_event_type: str) -> str:
    appointment_status = _extract_appointment_status(payload)
    if appointment_status in _FULLSCOPE_CANCELED_STATUSES:
        return "booking.canceled"
    if appointment_status in _FULLSCOPE_CONFIRMED_STATUSES:
        return "booking.created"

    booking_status = _extract_booking_status(payload)
    if booking_status == "booked":
        return "booking.created"

    return provider_event_type


def _extract_appointment_status(payload: dict[str, Any]) -> str | None:
    calendar = payload.get("calendar")
    if not isinstance(calendar, dict):
        return None
    for key in ("appoinmentStatus", "appointmentStatus"):
        value = calendar.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return None


def _extract_booking_status(payload: dict[str, Any]) -> str | None:
    calendar = payload.get("calendar")
    if not isinstance(calendar, dict):
        return None
    value = calendar.get("status")
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    return None


def _extract_booking_email(payload: dict[str, Any]) -> str | None:
    for key in ("email", "contact_email"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()

    contact = payload.get("contact")
    if isinstance(contact, dict):
        value = contact.get("email")
        if isinstance(value, str) and value.strip():
            return value.strip().lower()

    return None


def _extract_booked_at(payload: dict[str, Any]) -> datetime | None:
    calendar = payload.get("calendar")
    if not isinstance(calendar, dict):
        return None

    for key in ("date_created", "dateCreated"):
        value = calendar.get(key)
        parsed = _parse_datetime(value)
        if parsed is not None:
            return parsed

    return None


def _extract_string(
    source: dict[str, Any],
    candidate: tuple[str, str],
) -> tuple[str | None, str | None]:
    key, path = candidate
    value = source.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip(), path
    return None, None


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None

    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _payload_sha256(payload: dict[str, Any]) -> str:
    normalized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()
