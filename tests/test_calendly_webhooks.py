import hashlib
import hmac
import json
import os
from contextlib import contextmanager
from time import time

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.main import app
from app.models.booking import Booking


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

    def handle_event(self, *, event) -> None:
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


def _booking_count() -> int:
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    with Session(engine) as session:
        return len(session.scalars(select(Booking)).all())


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
    assert _booking_count() == 0


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

    assert response.status_code == 200
    assert response.headers.get("X-Request-Id")
    assert response.json() == {"status": "ok"}
