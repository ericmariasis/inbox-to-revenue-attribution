import hashlib
import re
import uuid
from contextlib import contextmanager
from datetime import timezone

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app
from app.services.email_stub import get_magic_link_outbox


def _latest_magic_link_token_for_email(email: str) -> str:
    outbox = get_magic_link_outbox()
    for message in reversed(outbox):
        if message["email"] == email:
            return message["token"]

    raise AssertionError(f"No magic-link token captured for {email}")


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


class _CaptureClickEventPublisher:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)


def test_phase4_redirect_flow_end_to_end():
    creator_email = f"phase4_creator_{uuid.uuid4().hex}@example.com"
    tracked_base_url = get_settings().tracked_link_base_url.rstrip("/")
    client_ip = "203.0.113.25"
    capture_publisher = _CaptureClickEventPublisher()

    with _override_app_state("click_event_publisher", capture_publisher):
        with TestClient(app, client=(client_ip, 50007)) as client:
            start_response = client.post(
                "/auth/magic-link/start",
                json={"email": creator_email},
            )
            verify_response = client.get(
                "/auth/magic-link/verify",
                params={"token": _latest_magic_link_token_for_email(creator_email)},
            )
            access_token = verify_response.json()["access_token"]

            booking_link_response = client.post(
                "/booking-links",
                headers={"Authorization": f"Bearer {access_token}"},
                json={
                    "name": "Phase 4 Validation Call",
                    "calendly_url": "https://calendly.com/example/phase4-validation-call?utm_source=linkedin&month=2026-03",
                },
            )
            booking_link = booking_link_response.json()

            content_response = client.post(
                "/content",
                headers={"Authorization": f"Bearer {access_token}"},
                json={
                    "source_url": "https://example.com/posts/phase4-validation-content",
                    "booking_link_id": booking_link["id"],
                },
            )
            content = content_response.json()

            first_redirect_response = client.get(
                f"/r/{content['tid']}",
                follow_redirects=False,
            )
            second_redirect_response = client.get(
                f"/r/{content['tid']}",
                follow_redirects=False,
            )
            unknown_redirect_response = client.get(
                "/r/phase4-unknown-tid",
                follow_redirects=False,
            )

    expected_location = (
        "https://calendly.com/example/phase4-validation-call"
        f"?utm_source=linkedin&month=2026-03&tid={content['tid']}"
    )
    expected_hashed_ip = hashlib.sha256(client_ip.encode("utf-8")).hexdigest()
    first_session_id = first_redirect_response.cookies.get("ccp_sid")
    second_session_id = second_redirect_response.cookies.get("ccp_sid")

    assert start_response.status_code == 200
    assert start_response.json() == {"status": "ok"}
    assert start_response.headers.get("X-Request-Id")

    assert verify_response.status_code == 200
    assert verify_response.json()["token_type"] == "bearer"
    assert verify_response.headers.get("X-Request-Id")

    assert booking_link_response.status_code == 201
    assert booking_link_response.headers.get("X-Request-Id")
    assert booking_link["name"] == "Phase 4 Validation Call"
    assert (
        booking_link["calendly_url"]
        == "https://calendly.com/example/phase4-validation-call?utm_source=linkedin&month=2026-03"
    )

    assert content_response.status_code == 201
    assert content_response.headers.get("X-Request-Id")
    assert content["booking_link_id"] == booking_link["id"]
    assert content["source_url"] == "https://example.com/posts/phase4-validation-content"
    assert content["tracked_url"] == f"{tracked_base_url}/r/{content['tid']}"
    assert uuid.UUID(hex=content["tid"]).hex == content["tid"]

    assert first_redirect_response.status_code == 302
    assert first_redirect_response.headers.get("X-Request-Id")
    assert first_redirect_response.headers["location"] == expected_location
    assert first_session_id
    assert re.fullmatch(r"[0-9a-f]{32}", first_session_id)

    assert second_redirect_response.status_code == 302
    assert second_redirect_response.headers.get("X-Request-Id")
    assert second_redirect_response.headers["location"] == expected_location
    assert second_session_id == first_session_id

    assert len(capture_publisher.events) == 2
    assert re.fullmatch(r"[0-9a-f]{32}", capture_publisher.events[0].event_id)
    assert re.fullmatch(r"[0-9a-f]{32}", capture_publisher.events[1].event_id)
    assert capture_publisher.events[0].event_id != capture_publisher.events[1].event_id
    assert capture_publisher.events[0].tid == content["tid"]
    assert capture_publisher.events[1].tid == content["tid"]
    assert capture_publisher.events[0].session_id == first_session_id
    assert capture_publisher.events[1].session_id == first_session_id
    assert capture_publisher.events[0].hashed_ip == expected_hashed_ip
    assert capture_publisher.events[1].hashed_ip == expected_hashed_ip
    assert capture_publisher.events[0].timestamp.tzinfo == timezone.utc
    assert capture_publisher.events[1].timestamp.tzinfo == timezone.utc

    assert unknown_redirect_response.status_code == 404
    assert unknown_redirect_response.headers.get("X-Request-Id")
    assert unknown_redirect_response.json() == {"detail": "link not found"}
