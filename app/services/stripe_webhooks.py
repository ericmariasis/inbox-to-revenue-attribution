import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.invoice import Invoice
from app.models.invoice_payment_event import InvoicePaymentEvent

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class StripeWebhookVerificationError(ValueError):
    pass


class StripeWebhookPayloadError(ValueError):
    pass


@dataclass(frozen=True)
class StripeWebhookEvent:
    stripe_event_id: str
    event_type: str
    stripe_account_id: str | None
    payload: dict[str, Any]


class StripeWebhookRouter(Protocol):
    def handle_event(self, *, event: StripeWebhookEvent) -> None: ...


class InvoicePaidStripeWebhookHandler:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        now_fn: Callable[[], datetime] | None = None,
    ):
        self._session_factory = session_factory
        self._now_fn = now_fn or _utc_now

    def handle_event(self, *, event: StripeWebhookEvent) -> bool:
        if event.event_type != "invoice.paid":
            return False

        stripe_invoice_id = _extract_stripe_invoice_id(event.payload)
        if stripe_invoice_id is None:
            logger.warning(
                "stripe_webhook_invoice_paid_unhandled_missing_invoice_id stripe_event_id=%s stripe_account_id=%s",
                event.stripe_event_id,
                event.stripe_account_id,
            )
            return True

        if event.stripe_account_id is None:
            logger.warning(
                "stripe_webhook_invoice_paid_unhandled_missing_account_id stripe_event_id=%s stripe_invoice_id=%s",
                event.stripe_event_id,
                stripe_invoice_id,
            )
            return True

        handled_at = self._now_fn()
        paid_at = _extract_paid_at(event.payload) or handled_at

        with self._session_factory() as session:
            invoice = session.scalar(
                select(Invoice)
                .where(
                    Invoice.stripe_invoice_id == stripe_invoice_id,
                    Invoice.stripe_account_id == event.stripe_account_id,
                )
                .with_for_update()
            )
            if invoice is None:
                logger.info(
                    "stripe_webhook_invoice_paid_noop_unmatched stripe_event_id=%s stripe_invoice_id=%s stripe_account_id=%s",
                    event.stripe_event_id,
                    stripe_invoice_id,
                    event.stripe_account_id,
                )
                return True

            existing_payment_event = session.scalar(
                select(InvoicePaymentEvent).where(
                    InvoicePaymentEvent.stripe_event_id == event.stripe_event_id
                )
            )
            if existing_payment_event is not None:
                logger.info(
                    "stripe_webhook_invoice_paid_duplicate_event stripe_event_id=%s stripe_invoice_id=%s stripe_account_id=%s invoice_id=%s",
                    event.stripe_event_id,
                    stripe_invoice_id,
                    event.stripe_account_id,
                    existing_payment_event.invoice_id,
                )
                return True

            if invoice.status == "paid":
                logger.info(
                    "stripe_webhook_invoice_paid_noop_already_paid stripe_event_id=%s stripe_invoice_id=%s stripe_account_id=%s invoice_id=%s",
                    event.stripe_event_id,
                    stripe_invoice_id,
                    event.stripe_account_id,
                    invoice.id,
                )
                return True

            if invoice.status != "open":
                logger.info(
                    "stripe_webhook_invoice_paid_noop_non_open stripe_event_id=%s stripe_invoice_id=%s stripe_account_id=%s invoice_id=%s status=%s",
                    event.stripe_event_id,
                    stripe_invoice_id,
                    event.stripe_account_id,
                    invoice.id,
                    invoice.status,
                )
                return True

            invoice_id = invoice.id
            invoice.status = "paid"
            invoice.paid_at = paid_at
            payment_event = InvoicePaymentEvent(
                stripe_event_id=event.stripe_event_id,
                stripe_event_type=event.event_type,
                stripe_account_id=event.stripe_account_id,
                stripe_invoice_id=stripe_invoice_id,
                invoice_id=invoice_id,
                creator_id=invoice.creator_id,
                booking_id=invoice.booking_id,
                tid=invoice.tid,
                status="applied",
                paid_at=paid_at,
                received_at=handled_at,
                processed_at=handled_at,
            )
            session.add(payment_event)

            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                logger.info(
                    "stripe_webhook_invoice_paid_duplicate_event stripe_event_id=%s stripe_invoice_id=%s stripe_account_id=%s invoice_id=%s",
                    event.stripe_event_id,
                    stripe_invoice_id,
                    event.stripe_account_id,
                    invoice_id,
                )
                return True

            session.refresh(invoice)
            session.refresh(payment_event)
            logger.info(
                "stripe_webhook_invoice_paid_applied stripe_event_id=%s stripe_invoice_id=%s stripe_account_id=%s invoice_id=%s creator_id=%s booking_uuid=%s tid=%s paid_at=%s payment_event_id=%s",
                event.stripe_event_id,
                stripe_invoice_id,
                event.stripe_account_id,
                invoice.id,
                invoice.creator_id,
                invoice.booking.calendly_booking_uuid,
                invoice.tid,
                invoice.paid_at.isoformat(),
                payment_event.id,
            )
            return True


class DefaultStripeWebhookRouter:
    def __init__(
        self,
        *,
        invoice_paid_handler: InvoicePaidStripeWebhookHandler | None = None,
    ):
        self._invoice_paid_handler = invoice_paid_handler or InvoicePaidStripeWebhookHandler(
            session_factory=SessionLocal
        )

    def handle_event(self, *, event: StripeWebhookEvent) -> None:
        logger.info(
            "stripe_webhook_event_verified stripe_event_id=%s stripe_account_id=%s event_type=%s",
            event.stripe_event_id,
            event.stripe_account_id,
            event.event_type,
        )
        if self._invoice_paid_handler.handle_event(event=event):
            return
        logger.info(
            "stripe_webhook_event_noop stripe_event_id=%s stripe_account_id=%s event_type=%s",
            event.stripe_event_id,
            event.stripe_account_id,
            event.event_type,
        )


def build_default_stripe_webhook_router(
    *,
    session_factory: Callable[[], Session] = SessionLocal,
    now_fn: Callable[[], datetime] | None = None,
) -> DefaultStripeWebhookRouter:
    return DefaultStripeWebhookRouter(
        invoice_paid_handler=InvoicePaidStripeWebhookHandler(
            session_factory=session_factory,
            now_fn=now_fn,
        )
    )


DEFAULT_STRIPE_WEBHOOK_ROUTER = build_default_stripe_webhook_router()


def verify_and_parse_stripe_webhook(
    *,
    payload: bytes,
    signature_header: str | None,
    secret: str,
    tolerance_seconds: int,
    now: datetime | None = None,
) -> StripeWebhookEvent:
    timestamp, signatures = _parse_signature_header(signature_header)
    _verify_signature(
        payload=payload,
        timestamp=timestamp,
        signatures=signatures,
        secret=secret,
        tolerance_seconds=tolerance_seconds,
        now=now,
    )

    try:
        parsed_payload = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise StripeWebhookPayloadError("invalid stripe webhook payload") from exc

    event_id = parsed_payload.get("id")
    event_type = parsed_payload.get("type")
    if not isinstance(event_id, str) or not event_id:
        raise StripeWebhookPayloadError("missing stripe event id")
    if not isinstance(event_type, str) or not event_type:
        raise StripeWebhookPayloadError("missing stripe event type")

    return StripeWebhookEvent(
        stripe_event_id=event_id,
        event_type=event_type,
        stripe_account_id=_extract_stripe_account_id(parsed_payload),
        payload=parsed_payload,
    )


def _parse_signature_header(signature_header: str | None) -> tuple[int, list[str]]:
    if not signature_header:
        raise StripeWebhookVerificationError("missing stripe signature header")

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
                raise StripeWebhookVerificationError("invalid stripe signature timestamp") from exc
        elif key == "v1":
            signatures.append(value)

    if timestamp is None or not signatures:
        raise StripeWebhookVerificationError("invalid stripe signature header")

    return timestamp, signatures


def _verify_signature(
    *,
    payload: bytes,
    timestamp: int,
    signatures: list[str],
    secret: str,
    tolerance_seconds: int,
    now: datetime | None,
) -> None:
    current_time = now or datetime.now(UTC)
    if abs(int(current_time.timestamp()) - timestamp) > tolerance_seconds:
        raise StripeWebhookVerificationError("stripe signature timestamp outside tolerance")

    signed_payload = f"{timestamp}.".encode("utf-8") + payload
    expected_signature = hmac.new(
        secret.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()
    if not any(hmac.compare_digest(expected_signature, signature) for signature in signatures):
        raise StripeWebhookVerificationError("invalid stripe signature")


def _extract_stripe_account_id(payload: dict[str, Any]) -> str | None:
    account_id = payload.get("account")
    if isinstance(account_id, str) and account_id:
        return account_id

    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    event_object = data.get("object")
    if not isinstance(event_object, dict):
        return None
    nested_account_id = event_object.get("account")
    if isinstance(nested_account_id, str) and nested_account_id:
        return nested_account_id
    return None


def _extract_stripe_invoice_id(payload: dict[str, Any]) -> str | None:
    event_object = _extract_event_object(payload)
    if event_object is None:
        return None

    invoice_id = event_object.get("id")
    if isinstance(invoice_id, str) and invoice_id:
        return invoice_id
    return None


def _extract_paid_at(payload: dict[str, Any]) -> datetime | None:
    event_object = _extract_event_object(payload)
    if event_object is None:
        return None

    status_transitions = event_object.get("status_transitions")
    if isinstance(status_transitions, dict):
        paid_at_timestamp = status_transitions.get("paid_at")
        parsed_paid_at = _datetime_from_unix_timestamp(paid_at_timestamp)
        if parsed_paid_at is not None:
            return parsed_paid_at

    return _datetime_from_unix_timestamp(event_object.get("paid_at"))


def _extract_event_object(payload: dict[str, Any]) -> dict[str, Any] | None:
    data = payload.get("data")
    if not isinstance(data, dict):
        return None

    event_object = data.get("object")
    if not isinstance(event_object, dict):
        return None
    return event_object


def _datetime_from_unix_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return datetime.fromtimestamp(value, tz=UTC)
