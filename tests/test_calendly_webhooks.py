import hashlib
import hmac
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from time import time
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.main import app
from app.models.booking import Booking
from app.models.booking_link import BookingLink
from app.models.content import Content
from app.models.creator import Creator


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


def _create_creator_booking_link_and_content(*, tid: str) -> dict[str, Any]:
    engine = create_engine(os.environ["TEST_DATABASE_URL"])

    with Session(engine) as session:
        creator = Creator(name=f"Story 33 Creator {uuid.uuid4().hex}")
        session.add(creator)
        session.flush()

        booking_link = BookingLink(
            creator_id=creator.id,
            name="Story 33 Booking Call",
            calendly_url="https://calendly.com/example/story33-call",
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
        }


def _bookings_for_uuid(*, calendly_booking_uuid: str) -> list[Booking]:
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    with Session(engine) as session:
        return session.scalars(
            select(Booking).where(Booking.calendly_booking_uuid == calendly_booking_uuid)
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

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert len(bookings) == 1
    assert bookings[0].creator_id == stored["creator_id"]
    assert bookings[0].booking_link_id == stored["booking_link_id"]
    assert bookings[0].tid == stored["tid"]
    assert bookings[0].email == "story33-duplicate@example.com"
    assert bookings[0].status == "created"
    assert bookings[0].booked_at == datetime(2026, 3, 7, 16, 0, tzinfo=timezone.utc)


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
