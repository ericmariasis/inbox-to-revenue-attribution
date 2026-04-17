import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.billing_provider import BILLING_PROVIDER_PAYPAL
from app.models.creator import Creator
from app.models.invoice import Invoice
from app.services.invoice_payment_events import (
    InvoicePaymentEventService,
    UNATTRIBUTED_REASON_UNKNOWN_PROVIDER_INVOICE_ID,
)
from app.services.paypal_provider import (
    PayPalProvider,
    PayPalProviderError,
    build_default_paypal_provider,
)

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class PayPalWebhookVerificationError(ValueError):
    pass


class PayPalWebhookPayloadError(ValueError):
    pass


@dataclass(frozen=True)
class PayPalWebhookEvent:
    paypal_event_id: str
    event_type: str
    provider_invoice_id: str | None
    resource_id: str | None
    payload: dict[str, Any]
    created_at: datetime | None


class PayPalWebhookRouter(Protocol):
    def handle_event(self, *, event: PayPalWebhookEvent) -> None: ...


@dataclass(frozen=True)
class _PayPalInvoiceContext:
    provider_account_id: str | None


class InvoicePaidPayPalWebhookHandler:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        provider: PayPalProvider,
        now_fn: Callable[[], datetime] | None = None,
    ):
        self._provider = provider
        self._now_fn = now_fn or _utc_now
        self._payment_event_service = InvoicePaymentEventService(
            session_factory=session_factory,
            now_fn=self._now_fn,
        )
        self._session_factory = session_factory

    def handle_event(self, *, event: PayPalWebhookEvent) -> bool:
        if event.event_type != "INVOICING.INVOICE.PAID":
            return False

        if event.provider_invoice_id is None:
            logger.warning(
                "paypal_webhook_invoice_paid_unhandled_missing_invoice_id paypal_event_id=%s",
                event.paypal_event_id,
            )
            return True

        handled_at = self._now_fn()
        invoice_context = _load_paypal_invoice_context(
            session_factory=self._session_factory,
            provider_invoice_id=event.provider_invoice_id,
        )
        if invoice_context is None or invoice_context.provider_account_id is None:
            result = self._payment_event_service.handle_provider_invoice_paid_event(
                payment_provider=BILLING_PROVIDER_PAYPAL,
                provider_event_id=event.paypal_event_id,
                provider_event_type=event.event_type,
                provider_account_id=None,
                provider_invoice_id=event.provider_invoice_id,
                paid_at=event.created_at or handled_at,
                received_at=handled_at,
                unmatched_reason_override=UNATTRIBUTED_REASON_UNKNOWN_PROVIDER_INVOICE_ID,
            )
            logger.info(
                "paypal_webhook_invoice_paid_unmatched paypal_event_id=%s provider_invoice_id=%s outcome=%s payment_event_id=%s unattributed_reason=%s",
                event.paypal_event_id,
                event.provider_invoice_id,
                result.outcome,
                result.payment_event_id,
                result.unattributed_reason,
            )
            return True

        try:
            paid_snapshot = self._provider.get_invoice_paid_snapshot(
                provider_account_id=invoice_context.provider_account_id,
                provider_invoice_id=event.provider_invoice_id,
            )
        except PayPalProviderError as exc:
            logger.warning(
                "paypal_webhook_invoice_paid_unhandled_provider_error paypal_event_id=%s provider_invoice_id=%s provider_account_id=%s operation=%s http_status=%s error_code=%s",
                event.paypal_event_id,
                event.provider_invoice_id,
                invoice_context.provider_account_id,
                exc.operation,
                exc.http_status,
                exc.error_code,
            )
            return True

        if not paid_snapshot.is_canonical_paid:
            logger.info(
                "paypal_webhook_invoice_paid_noop_unsupported_paid_truth paypal_event_id=%s provider_invoice_id=%s status=%s payment_type=%s payment_method=%s transaction_status=%s",
                event.paypal_event_id,
                event.provider_invoice_id,
                paid_snapshot.status,
                paid_snapshot.payment_type,
                paid_snapshot.payment_method,
                paid_snapshot.transaction_status,
            )
            return True

        result = self._payment_event_service.handle_provider_invoice_paid_event(
            payment_provider=BILLING_PROVIDER_PAYPAL,
            provider_event_id=event.paypal_event_id,
            provider_event_type=event.event_type,
            provider_account_id=invoice_context.provider_account_id,
            provider_invoice_id=event.provider_invoice_id,
            paid_at=paid_snapshot.paid_at or event.created_at or handled_at,
            received_at=handled_at,
        )

        if result.outcome == "applied":
            logger.info(
                "paypal_webhook_invoice_paid_applied paypal_event_id=%s provider_invoice_id=%s provider_account_id=%s invoice_id=%s creator_id=%s booking_uuid=%s tid=%s payment_event_id=%s",
                event.paypal_event_id,
                event.provider_invoice_id,
                invoice_context.provider_account_id,
                result.invoice_id,
                result.creator_id,
                result.booking_uuid,
                result.tid,
                result.payment_event_id,
            )
            return True

        if result.outcome == "reconciled":
            logger.info(
                "paypal_webhook_invoice_paid_reconciled paypal_event_id=%s provider_invoice_id=%s provider_account_id=%s invoice_id=%s creator_id=%s booking_uuid=%s tid=%s payment_event_id=%s",
                event.paypal_event_id,
                event.provider_invoice_id,
                invoice_context.provider_account_id,
                result.invoice_id,
                result.creator_id,
                result.booking_uuid,
                result.tid,
                result.payment_event_id,
            )
            return True

        if result.outcome == "duplicate":
            logger.info(
                "paypal_webhook_invoice_paid_duplicate_event paypal_event_id=%s provider_invoice_id=%s provider_account_id=%s invoice_id=%s payment_event_id=%s unattributed_reason=%s",
                event.paypal_event_id,
                event.provider_invoice_id,
                invoice_context.provider_account_id,
                result.invoice_id,
                result.payment_event_id,
                result.unattributed_reason,
            )
            return True

        if result.outcome == "noop_already_paid":
            logger.info(
                "paypal_webhook_invoice_paid_noop_already_paid paypal_event_id=%s provider_invoice_id=%s provider_account_id=%s invoice_id=%s",
                event.paypal_event_id,
                event.provider_invoice_id,
                invoice_context.provider_account_id,
                result.invoice_id,
            )
            return True

        if result.outcome == "noop_non_open":
            logger.info(
                "paypal_webhook_invoice_paid_noop_non_open paypal_event_id=%s provider_invoice_id=%s provider_account_id=%s invoice_id=%s status=%s",
                event.paypal_event_id,
                event.provider_invoice_id,
                invoice_context.provider_account_id,
                result.invoice_id,
                result.invoice_status,
            )
            return True

        logger.info(
            "paypal_webhook_invoice_paid_unmatched paypal_event_id=%s provider_invoice_id=%s outcome=%s payment_event_id=%s unattributed_reason=%s",
            event.paypal_event_id,
            event.provider_invoice_id,
            result.outcome,
            result.payment_event_id,
            result.unattributed_reason,
        )
        return True


class CaptureCompletedPayPalWebhookHandler:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        now_fn: Callable[[], datetime] | None = None,
    ):
        self._now_fn = now_fn or _utc_now
        self._payment_event_service = InvoicePaymentEventService(
            session_factory=session_factory,
            now_fn=self._now_fn,
        )
        self._session_factory = session_factory

    def handle_event(self, *, event: PayPalWebhookEvent) -> bool:
        if event.event_type != "PAYMENT.CAPTURE.COMPLETED":
            return False

        if event.provider_invoice_id is None or event.resource_id is None:
            logger.warning(
                "paypal_webhook_capture_completed_unhandled_missing_identity paypal_event_id=%s provider_invoice_id=%s resource_id=%s",
                event.paypal_event_id,
                event.provider_invoice_id,
                event.resource_id,
            )
            return True

        handled_at = self._now_fn()
        invoice_context = _load_paypal_invoice_context(
            session_factory=self._session_factory,
            provider_invoice_id=event.provider_invoice_id,
        )
        provider_account_id = (
            invoice_context.provider_account_id if invoice_context is not None else None
        )

        result = self._payment_event_service.handle_provider_invoice_paid_event(
            payment_provider=BILLING_PROVIDER_PAYPAL,
            provider_event_id=event.resource_id,
            provider_event_type=event.event_type,
            provider_account_id=provider_account_id,
            provider_invoice_id=event.provider_invoice_id,
            paid_at=event.created_at or handled_at,
            received_at=handled_at,
            unmatched_reason_override=UNATTRIBUTED_REASON_UNKNOWN_PROVIDER_INVOICE_ID,
        )

        if result.outcome == "applied":
            logger.info(
                "paypal_webhook_capture_completed_applied paypal_event_id=%s capture_id=%s provider_order_id=%s provider_account_id=%s invoice_id=%s creator_id=%s booking_uuid=%s tid=%s payment_event_id=%s",
                event.paypal_event_id,
                event.resource_id,
                event.provider_invoice_id,
                provider_account_id,
                result.invoice_id,
                result.creator_id,
                result.booking_uuid,
                result.tid,
                result.payment_event_id,
            )
            return True

        if result.outcome == "reconciled":
            logger.info(
                "paypal_webhook_capture_completed_reconciled paypal_event_id=%s capture_id=%s provider_order_id=%s provider_account_id=%s invoice_id=%s creator_id=%s booking_uuid=%s tid=%s payment_event_id=%s",
                event.paypal_event_id,
                event.resource_id,
                event.provider_invoice_id,
                provider_account_id,
                result.invoice_id,
                result.creator_id,
                result.booking_uuid,
                result.tid,
                result.payment_event_id,
            )
            return True

        if result.outcome == "duplicate":
            logger.info(
                "paypal_webhook_capture_completed_duplicate_event paypal_event_id=%s capture_id=%s provider_order_id=%s provider_account_id=%s invoice_id=%s payment_event_id=%s unattributed_reason=%s",
                event.paypal_event_id,
                event.resource_id,
                event.provider_invoice_id,
                provider_account_id,
                result.invoice_id,
                result.payment_event_id,
                result.unattributed_reason,
            )
            return True

        if result.outcome == "noop_already_paid":
            logger.info(
                "paypal_webhook_capture_completed_noop_already_paid paypal_event_id=%s capture_id=%s provider_order_id=%s provider_account_id=%s invoice_id=%s",
                event.paypal_event_id,
                event.resource_id,
                event.provider_invoice_id,
                provider_account_id,
                result.invoice_id,
            )
            return True

        if result.outcome == "noop_non_open":
            logger.info(
                "paypal_webhook_capture_completed_noop_non_open paypal_event_id=%s capture_id=%s provider_order_id=%s provider_account_id=%s invoice_id=%s status=%s",
                event.paypal_event_id,
                event.resource_id,
                event.provider_invoice_id,
                provider_account_id,
                result.invoice_id,
                result.invoice_status,
            )
            return True

        logger.info(
            "paypal_webhook_capture_completed_unmatched paypal_event_id=%s capture_id=%s provider_order_id=%s outcome=%s payment_event_id=%s unattributed_reason=%s",
            event.paypal_event_id,
            event.resource_id,
            event.provider_invoice_id,
            result.outcome,
            result.payment_event_id,
            result.unattributed_reason,
        )
        return True


class DefaultPayPalWebhookRouter:
    def __init__(
        self,
        *,
        capture_completed_handler: CaptureCompletedPayPalWebhookHandler | None = None,
        invoice_paid_handler: InvoicePaidPayPalWebhookHandler | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ):
        self._capture_completed_handler = (
            capture_completed_handler
            or CaptureCompletedPayPalWebhookHandler(
                session_factory=SessionLocal,
                now_fn=now_fn,
            )
        )
        self._invoice_paid_handler = invoice_paid_handler or InvoicePaidPayPalWebhookHandler(
            session_factory=SessionLocal,
            provider=build_default_paypal_provider(),
            now_fn=now_fn,
        )

    def handle_event(self, *, event: PayPalWebhookEvent) -> None:
        logger.info(
            "paypal_webhook_event_verified paypal_event_id=%s event_type=%s provider_invoice_id=%s",
            event.paypal_event_id,
            event.event_type,
            event.provider_invoice_id,
        )
        if self._capture_completed_handler.handle_event(event=event):
            return
        if self._invoice_paid_handler.handle_event(event=event):
            return
        logger.info(
            "paypal_webhook_event_noop paypal_event_id=%s event_type=%s provider_invoice_id=%s",
            event.paypal_event_id,
            event.event_type,
            event.provider_invoice_id,
        )


def build_default_paypal_webhook_router(
    *,
    session_factory: Callable[[], Session] = SessionLocal,
    provider: PayPalProvider | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> DefaultPayPalWebhookRouter:
    return DefaultPayPalWebhookRouter(
        capture_completed_handler=CaptureCompletedPayPalWebhookHandler(
            session_factory=session_factory,
            now_fn=now_fn,
        ),
        invoice_paid_handler=InvoicePaidPayPalWebhookHandler(
            session_factory=session_factory,
            provider=provider or build_default_paypal_provider(),
            now_fn=now_fn,
        )
    )


DEFAULT_PAYPAL_WEBHOOK_ROUTER = build_default_paypal_webhook_router()


def verify_and_parse_paypal_webhook(
    *,
    payload: bytes,
    headers: Mapping[str, str],
    provider: PayPalProvider,
    webhook_id: str,
) -> PayPalWebhookEvent:
    try:
        parsed_payload = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise PayPalWebhookPayloadError("invalid paypal webhook payload") from exc

    if not isinstance(parsed_payload, dict):
        raise PayPalWebhookPayloadError("invalid paypal webhook payload")

    event_id = _required_string(parsed_payload, field_name="id")
    event_type = _required_string(parsed_payload, field_name="event_type")
    provider_invoice_id = _extract_paypal_provider_invoice_id(
        parsed_payload,
        event_type=event_type,
    )
    resource_id = _extract_paypal_resource_id(parsed_payload)
    if event_type == "INVOICING.INVOICE.PAID" and provider_invoice_id is None:
        raise PayPalWebhookPayloadError("missing paypal invoice id")
    if event_type == "PAYMENT.CAPTURE.COMPLETED" and provider_invoice_id is None:
        raise PayPalWebhookPayloadError("missing paypal order id")
    if event_type == "PAYMENT.CAPTURE.COMPLETED" and resource_id is None:
        raise PayPalWebhookPayloadError("missing paypal capture id")

    try:
        verified = provider.verify_webhook_event(
            webhook_id=webhook_id,
            auth_algo=_required_header(headers, header_name="paypal-auth-algo"),
            cert_url=_required_header(headers, header_name="paypal-cert-url"),
            transmission_id=_required_header(headers, header_name="paypal-transmission-id"),
            transmission_sig=_required_header(headers, header_name="paypal-transmission-sig"),
            transmission_time=_required_header(headers, header_name="paypal-transmission-time"),
            webhook_event=parsed_payload,
        )
    except PayPalProviderError as exc:
        raise PayPalWebhookVerificationError("paypal webhook verification failed") from exc

    if not verified:
        raise PayPalWebhookVerificationError("paypal webhook verification failed")

    return PayPalWebhookEvent(
        paypal_event_id=event_id,
        event_type=event_type,
        provider_invoice_id=provider_invoice_id,
        resource_id=resource_id,
        payload=parsed_payload,
        created_at=_optional_timestamp(parsed_payload, field_name="create_time"),
    )


def _required_header(headers: Mapping[str, str], *, header_name: str) -> str:
    header_value = headers.get(header_name)
    if isinstance(header_value, str) and header_value:
        return header_value
    raise PayPalWebhookVerificationError("missing paypal webhook verification headers")


def _required_string(payload: Mapping[str, Any], *, field_name: str) -> str:
    value = payload.get(field_name)
    if isinstance(value, str) and value:
        return value
    raise PayPalWebhookPayloadError(f"missing paypal {field_name}")


def _extract_paypal_provider_invoice_id(
    payload: Mapping[str, Any],
    *,
    event_type: str,
) -> str | None:
    if event_type == "INVOICING.INVOICE.PAID":
        return _extract_paypal_invoice_id(payload)
    if event_type == "PAYMENT.CAPTURE.COMPLETED":
        return _extract_paypal_capture_order_id(payload)
    return None


def _extract_paypal_invoice_id(payload: Mapping[str, Any]) -> str | None:
    resource = payload.get("resource")
    if not isinstance(resource, dict):
        return None
    invoice = resource.get("invoice")
    if not isinstance(invoice, dict):
        return None
    invoice_id = invoice.get("id")
    if isinstance(invoice_id, str) and invoice_id:
        return invoice_id
    return None


def _extract_paypal_capture_order_id(payload: Mapping[str, Any]) -> str | None:
    resource = payload.get("resource")
    if not isinstance(resource, dict):
        return None

    supplementary_data = resource.get("supplementary_data")
    if isinstance(supplementary_data, dict):
        related_ids = supplementary_data.get("related_ids")
        if isinstance(related_ids, dict):
            order_id = related_ids.get("order_id")
            if isinstance(order_id, str) and order_id:
                return order_id

    links = resource.get("links")
    if isinstance(links, list):
        for link in links:
            if not isinstance(link, dict):
                continue
            if link.get("rel") != "up":
                continue
            href = link.get("href")
            if not isinstance(href, str) or not href:
                continue
            marker = "/v2/checkout/orders/"
            if marker not in href:
                continue
            order_id = href.rsplit(marker, 1)[-1].strip("/")
            if order_id:
                return order_id

    return None


def _extract_paypal_resource_id(payload: Mapping[str, Any]) -> str | None:
    resource = payload.get("resource")
    if not isinstance(resource, dict):
        return None
    resource_id = resource.get("id")
    if isinstance(resource_id, str) and resource_id:
        return resource_id
    return None


def _optional_timestamp(payload: Mapping[str, Any], *, field_name: str) -> datetime | None:
    raw_value = payload.get(field_name)
    if not isinstance(raw_value, str) or not raw_value:
        return None
    try:
        return datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _load_paypal_invoice_context(
    *,
    session_factory: Callable[[], Session],
    provider_invoice_id: str,
) -> _PayPalInvoiceContext | None:
    with session_factory() as session:
        row = session.execute(
            select(
                Invoice.provider_account_id,
                Creator.billing_provider,
                Creator.billing_account_id,
            )
            .join(Creator, Creator.id == Invoice.creator_id)
            .where(
                Invoice.payment_provider == BILLING_PROVIDER_PAYPAL,
                Invoice.provider_invoice_id == provider_invoice_id,
            )
        ).first()
        if row is None:
            return None

    invoice_provider_account_id, creator_billing_provider, creator_billing_account_id = row
    if creator_billing_provider != BILLING_PROVIDER_PAYPAL:
        return _PayPalInvoiceContext(provider_account_id=None)
    if (
        invoice_provider_account_id is not None
        and creator_billing_account_id is not None
        and invoice_provider_account_id != creator_billing_account_id
    ):
        return _PayPalInvoiceContext(provider_account_id=None)
    return _PayPalInvoiceContext(
        provider_account_id=creator_billing_account_id or invoice_provider_account_id
    )
