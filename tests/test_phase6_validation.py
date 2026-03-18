import hashlib
import hmac
import json
import os
import re
import uuid
from datetime import datetime, timezone
from time import time
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.main import app
from app.models.booking import Booking
from app.services.email_stub import get_magic_link_outbox

HTML_ACCEPT_HEADERS = {"Accept": "text/html,application/xhtml+xml"}


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


def _option_value_for_label(*, rendered_html: str, label: str) -> str:
    match = re.search(
        rf'<option value="([^"]+)"(?: selected)?>{re.escape(label)}</option>',
        rendered_html,
    )
    if match is None:
        raise AssertionError(f"No option found for label {label!r}")
    return match.group(1)


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


def test_phase65_creator_browser_workflow_end_to_end():
    creator_email = f"phase65_browser_{uuid.uuid4().hex}@example.com"
    settings = get_settings()
    booking_link_name = "Phase 6.5 Browser Validation Call"
    source_url = "https://example.com/posts/phase65-browser-validation"
    calendly_booking_uuid = "BOOK_phase65_browser_valid"
    tracked_base_url = settings.tracked_link_base_url.rstrip("/")

    with TestClient(app) as client:
        sign_in_response = client.post(
            "/sign-in",
            data={"email": creator_email},
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )
        raw_token = _latest_magic_link_token_for_email(creator_email)

        verify_response = client.get(
            "/auth/magic-link/verify",
            params={"token": raw_token},
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )
        home_response = client.get("/app", headers=HTML_ACCEPT_HEADERS)

        create_booking_link_response = client.post(
            "/app/booking-links",
            data={
                "name": booking_link_name,
                "calendly_url": "https://calendly.com/example/phase65-browser-validation",
                "billing_amount_cents": "15000",
                "billing_currency": " usd ",
            },
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )
        booking_links_page_response = client.get(
            create_booking_link_response.headers["location"],
            headers=HTML_ACCEPT_HEADERS,
        )

        content_page_response = client.get("/app/content", headers=HTML_ACCEPT_HEADERS)
        booking_link_id = _option_value_for_label(
            rendered_html=content_page_response.text,
            label=booking_link_name,
        )

        create_content_response = client.post(
            "/app/content",
            data={
                "source_url": source_url,
                "booking_link_id": booking_link_id,
            },
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )
        created_tid = parse_qs(
            urlparse(create_content_response.headers["location"]).query
        )["tid"][0]
        content_success_page_response = client.get(
            create_content_response.headers["location"],
            headers=HTML_ACCEPT_HEADERS,
        )

        empty_booking_page_response = client.get(
            "/app/bookings",
            headers=HTML_ACCEPT_HEADERS,
        )

        created_payload = _invitee_created_payload(
            event_id="EVT_phase65_browser_valid",
            calendly_booking_uuid=calendly_booking_uuid,
            tid=created_tid,
            email="phase65-booked@example.com",
            created_at="2026-03-08T15:00:00Z",
        )
        created_signature = _calendly_signature_header(
            payload=created_payload,
            signing_key=settings.calendly_webhook_signing_key,
        )
        webhook_response = client.post(
            "/webhooks/calendly",
            content=created_payload,
            headers={
                "Content-Type": "application/json",
                "Calendly-Webhook-Signature": created_signature,
            },
        )

        populated_booking_page_response = client.get(
            "/app/bookings",
            headers=HTML_ACCEPT_HEADERS,
        )

    assert sign_in_response.status_code == 303
    assert sign_in_response.headers["location"] == "/sign-in?status=sent"
    assert raw_token not in sign_in_response.headers["location"]

    assert verify_response.status_code == 303
    assert verify_response.headers["location"] == "/app"
    assert raw_token not in verify_response.headers["location"]
    assert raw_token not in verify_response.text
    assert "ccp_creator_session=" in verify_response.headers["set-cookie"]
    assert raw_token not in verify_response.headers["set-cookie"]

    assert home_response.status_code == 200
    assert "Setup Home" in home_response.text
    assert creator_email in home_response.text
    assert "0 of 4 setup steps done" in home_response.text
    assert "Save a booking link" in home_response.text
    assert "Add billing defaults" in home_response.text
    assert "Create a tracked link" in home_response.text
    assert 'href="/app/booking-links"' in home_response.text
    assert 'href="/app/content"' in home_response.text
    assert 'href="/app/bookings"' in home_response.text

    assert create_booking_link_response.status_code == 303
    assert create_booking_link_response.headers["location"] == "/app/booking-links?status=created"

    assert booking_links_page_response.status_code == 200
    assert "Booking link saved" in booking_links_page_response.text
    assert booking_link_name in booking_links_page_response.text
    assert "https://calendly.com/example/phase65-browser-validation" in booking_links_page_response.text
    assert "Amount and currency set: USD 150.00" in booking_links_page_response.text

    assert content_page_response.status_code == 200
    assert 'action="/app/content"' in content_page_response.text

    assert create_content_response.status_code == 303
    assert (
        create_content_response.headers["location"]
        == f"/app/content?status=created&tid={created_tid}"
    )

    assert content_success_page_response.status_code == 200
    assert "Tracked link ready" in content_success_page_response.text
    assert f"{tracked_base_url}/r/{created_tid}" in content_success_page_response.text
    assert source_url in content_success_page_response.text
    assert booking_link_name in content_success_page_response.text
    assert 'data-copy-source="created-tracked-url"' in content_success_page_response.text

    assert empty_booking_page_response.status_code == 200
    assert "Booking Activity" in empty_booking_page_response.text
    assert "No bookings captured yet" in empty_booking_page_response.text
    assert "may not appear immediately" in empty_booking_page_response.text
    assert "0 captured" in empty_booking_page_response.text
    assert source_url not in empty_booking_page_response.text

    assert webhook_response.status_code == 200
    assert webhook_response.json() == {"status": "ok"}

    assert populated_booking_page_response.status_code == 200
    assert "Booking Activity" in populated_booking_page_response.text
    assert "1 captured" in populated_booking_page_response.text
    assert booking_link_name in populated_booking_page_response.text
    assert source_url in populated_booking_page_response.text
    assert created_tid in populated_booking_page_response.text
    assert "Created" in populated_booking_page_response.text
    assert "March 08, 2026 at 03:00 PM UTC" in populated_booking_page_response.text
    assert "No bookings captured yet" not in populated_booking_page_response.text
    assert "phase65-booked@example.com" not in populated_booking_page_response.text
