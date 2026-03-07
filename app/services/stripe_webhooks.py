import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol


logger = logging.getLogger(__name__)


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


class LoggingStripeWebhookRouter:
    def handle_event(self, *, event: StripeWebhookEvent) -> None:
        logger.info(
            "stripe_webhook_event_verified stripe_event_id=%s stripe_account_id=%s event_type=%s",
            event.stripe_event_id,
            event.stripe_account_id,
            event.event_type,
        )
        logger.info(
            "stripe_webhook_event_noop stripe_event_id=%s stripe_account_id=%s event_type=%s",
            event.stripe_event_id,
            event.stripe_account_id,
            event.event_type,
        )


DEFAULT_STRIPE_WEBHOOK_ROUTER = LoggingStripeWebhookRouter()


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
