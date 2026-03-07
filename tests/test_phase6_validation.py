import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timezone
from time import time
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.main import app
from app.models.booking import Booking
from app.services.email_stub import get_magic_link_outbox


def _engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def _latest_magic_link_token_for_email(email: str) -> str:
    outbox = get_magic_link_outbox()
    for message in reversed(outbox):
        if message["email"] == email:
            return message["token"]

    raise AssertionError(f"No magic-link token captured for {email}")


def _calendly_signature_header(*, payload: bytes, signing_key: str, timestamp: int | None = None) -> str:
    resolved_timestamp = timestamp or int(time())
    signed_payload = f"{resolved_timestamp}.".encode("utf-8") + payload
    signature = hmac.new(signing_key.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={resolved_timestamp},v1={signature}"


def _invitee_created_payload(
    *,
    event_id: str,
    calendly_booking_uuid: str,
    tid: str | None,
    email: str,
    created_at: str,
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
        },
    }
    if tid is not None:
        payload["payload"]["tracking"] = {"utm_content": tid}

    return json.dumps(payload).encode("utf-8")


def _invitee_canceled_payload(
    *,
    event_id: str,
    calendly_booking_uuid: str,
    tid: str,
    canceled_at: str,
) -> bytes:
    payload = {
        "event": "invitee.canceled",
        "payload": {
            "event": f"https://api.calendly.com/scheduled_events/{event_id}",
            "uri": (
                "https://api.calendly.com/scheduled_events/"
                f"{event_id}/invitees/{calendly_booking_uuid}"
            ),
            "tracking": {"utm_content": tid},
            "canceled_at": canceled_at,
        },
    }
    return json.dumps(payload).encode("utf-8")


def test_phase6_calendly_webhook_flow_end_to_end():
    creator_email = f"phase6_creator_{uuid.uuid4().hex}@example.com"
    settings = get_settings()
    calendly_booking_uuid = "BOOK_phase6_valid"

    with TestClient(app) as client:
        start_response = client.post(
            "/auth/magic-link/start",
            json={"email": creator_email},
        )
        verify_response = client.get(
            "/auth/magic-link/verify",
            params={"token": _latest_magic_link_token_for_email(creator_email)},
        )
        access_token = verify_response.json()["access_token"]
        auth_headers = {"Authorization": f"Bearer {access_token}"}

        me_response = client.get("/me", headers=auth_headers)
        booking_link_response = client.post(
            "/booking-links",
            headers=auth_headers,
            json={
                "name": "Phase 6 Validation Call",
                "calendly_url": "https://calendly.com/example/phase6-validation-call",
            },
        )
        booking_link = booking_link_response.json()

        content_response = client.post(
            "/content",
            headers=auth_headers,
            json={
                "source_url": "https://example.com/posts/phase6-validation-content",
                "booking_link_id": booking_link["id"],
            },
        )
        content = content_response.json()

        created_payload = _invitee_created_payload(
            event_id="EVT_phase6_valid",
            calendly_booking_uuid=calendly_booking_uuid,
            tid=content["tid"],
            email="phase6-booked@example.com",
            created_at="2026-03-07T18:00:00Z",
        )
        created_signature = _calendly_signature_header(
            payload=created_payload,
            signing_key=settings.calendly_webhook_signing_key,
        )
        first_created_response = client.post(
            "/webhooks/calendly",
            content=created_payload,
            headers={
                "Content-Type": "application/json",
                "Calendly-Webhook-Signature": created_signature,
            },
        )
        duplicate_created_response = client.post(
            "/webhooks/calendly",
            content=created_payload,
            headers={
                "Content-Type": "application/json",
                "Calendly-Webhook-Signature": created_signature,
            },
        )

        with Session(_engine()) as db:
            created_bookings = db.scalars(
                select(Booking).where(Booking.calendly_booking_uuid == calendly_booking_uuid)
            ).all()

        canceled_payload = _invitee_canceled_payload(
            event_id="EVT_phase6_valid",
            calendly_booking_uuid=calendly_booking_uuid,
            tid=content["tid"],
            canceled_at="2026-03-07T18:45:00Z",
        )
        canceled_signature = _calendly_signature_header(
            payload=canceled_payload,
            signing_key=settings.calendly_webhook_signing_key,
        )
        canceled_response = client.post(
            "/webhooks/calendly",
            content=canceled_payload,
            headers={
                "Content-Type": "application/json",
                "Calendly-Webhook-Signature": canceled_signature,
            },
        )

        missing_tid_payload = _invitee_created_payload(
            event_id="EVT_phase6_missing_tid",
            calendly_booking_uuid="BOOK_phase6_missing_tid",
            tid=None,
            email="phase6-missing-tid@example.com",
            created_at="2026-03-07T19:00:00Z",
        )
        missing_tid_signature = _calendly_signature_header(
            payload=missing_tid_payload,
            signing_key=settings.calendly_webhook_signing_key,
        )
        with patch("app.services.calendly_webhooks.logger.warning") as warning_log:
            missing_tid_response = client.post(
                "/webhooks/calendly",
                content=missing_tid_payload,
                headers={
                    "Content-Type": "application/json",
                    "Calendly-Webhook-Signature": missing_tid_signature,
                },
            )

    with Session(_engine()) as db:
        persisted_bookings = db.scalars(select(Booking).order_by(Booking.calendly_booking_uuid.asc())).all()
        booking = db.scalar(select(Booking).where(Booking.calendly_booking_uuid == calendly_booking_uuid))

    assert start_response.status_code == 200
    assert start_response.headers.get("X-Request-Id")
    assert start_response.json() == {"status": "ok"}

    assert verify_response.status_code == 200
    assert verify_response.headers.get("X-Request-Id")
    assert verify_response.json()["token_type"] == "bearer"

    assert me_response.status_code == 200
    assert me_response.headers.get("X-Request-Id")
    assert me_response.json()["email"] == creator_email

    assert booking_link_response.status_code == 201
    assert booking_link_response.headers.get("X-Request-Id")
    assert booking_link["name"] == "Phase 6 Validation Call"
    assert booking_link["calendly_url"] == "https://calendly.com/example/phase6-validation-call"

    assert content_response.status_code == 201
    assert content_response.headers.get("X-Request-Id")
    assert content["booking_link_id"] == booking_link["id"]
    assert content["source_url"] == "https://example.com/posts/phase6-validation-content"
    assert uuid.UUID(hex=content["tid"]).hex == content["tid"]

    assert first_created_response.status_code == 200
    assert first_created_response.headers.get("X-Request-Id")
    assert first_created_response.json() == {"status": "ok"}

    assert duplicate_created_response.status_code == 200
    assert duplicate_created_response.headers.get("X-Request-Id")
    assert duplicate_created_response.json() == {"status": "ok"}

    assert len(created_bookings) == 1
    assert str(created_bookings[0].creator_id) == me_response.json()["id"]
    assert str(created_bookings[0].booking_link_id) == booking_link["id"]
    assert created_bookings[0].tid == content["tid"]
    assert created_bookings[0].email == "phase6-booked@example.com"
    assert created_bookings[0].status == "created"
    assert created_bookings[0].booked_at == datetime(2026, 3, 7, 18, 0, tzinfo=timezone.utc)
    assert created_bookings[0].canceled_at is None

    assert canceled_response.status_code == 200
    assert canceled_response.headers.get("X-Request-Id")
    assert canceled_response.json() == {"status": "ok"}

    assert missing_tid_response.status_code == 200
    assert missing_tid_response.headers.get("X-Request-Id")
    assert missing_tid_response.json() == {"status": "ok"}

    warning_log.assert_called_once()
    assert (
        warning_log.call_args.args[0]
        == "calendly_webhook_booking_created_missing_tid calendly_booking_uuid=%s provider_event_type=%s calendly_event_id=%s"
    )
    assert warning_log.call_args.args[1] == "BOOK_phase6_missing_tid"
    assert warning_log.call_args.args[2] == "invitee.created"
    assert warning_log.call_args.args[3] == "EVT_phase6_missing_tid"

    assert len(persisted_bookings) == 1
    assert booking is not None
    assert str(booking.creator_id) == me_response.json()["id"]
    assert str(booking.booking_link_id) == booking_link["id"]
    assert booking.tid == content["tid"]
    assert booking.calendly_booking_uuid == calendly_booking_uuid
    assert booking.email == "phase6-booked@example.com"
    assert booking.status == "canceled"
    assert booking.booked_at == datetime(2026, 3, 7, 18, 0, tzinfo=timezone.utc)
    assert booking.canceled_at == datetime(2026, 3, 7, 18, 45, tzinfo=timezone.utc)
