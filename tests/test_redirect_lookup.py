import os
import hashlib
import re
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.main import app
from app.services.rate_limit import (
    REDIRECT_SOFT_LIMIT_POLICY,
    SharedRateLimiter,
    build_redirect_rate_limit_bucket_key,
)


def _engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def _insert_creator_user(
    *,
    email: str,
    name: str = "Redirect Creator",
    stripe_connect_status: str = "pending",
):
    creator_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())

    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO creators (id, name, stripe_connect_status) "
                "VALUES (:id, :name, :stripe_connect_status)"
            ),
            {
                "id": creator_id,
                "name": name,
                "stripe_connect_status": stripe_connect_status,
            },
        )
        conn.execute(
            text(
                "INSERT INTO auth_users (id, creator_id, email) "
                "VALUES (:id, :creator_id, :email)"
            ),
            {"id": user_id, "creator_id": creator_id, "email": email},
        )

    return {"creator_id": creator_id, "user_id": user_id, "email": email}


def _insert_booking_link(*, creator_id: str, name: str, calendly_url: str) -> str:
    booking_link_id = str(uuid.uuid4())

    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO booking_links (id, creator_id, name, calendly_url) "
                "VALUES (:id, :creator_id, :name, :calendly_url)"
            ),
            {
                "id": booking_link_id,
                "creator_id": creator_id,
                "name": name,
                "calendly_url": calendly_url,
            },
        )

    return booking_link_id


def _insert_content(
    *,
    creator_id: str,
    booking_link_id: str,
    source_url: str,
    tid: str,
    created_at: datetime,
) -> str:
    content_id = str(uuid.uuid4())

    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO content "
                "(id, creator_id, booking_link_id, source_url, tid, created_at, updated_at) "
                "VALUES "
                "(:id, :creator_id, :booking_link_id, :source_url, :tid, :created_at, :updated_at)"
            ),
            {
                "id": content_id,
                "creator_id": creator_id,
                "booking_link_id": booking_link_id,
                "source_url": source_url,
                "tid": tid,
                "created_at": created_at,
                "updated_at": created_at,
            },
        )

    return content_id


def _redirect_cookie(response):
    cookie_header = response.headers.get("set-cookie")
    assert cookie_header

    parsed_cookie = SimpleCookie()
    parsed_cookie.load(cookie_header)
    return parsed_cookie["ccp_sid"]


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


class _FailingClickEventPublisher:
    def publish(self, event):
        raise RuntimeError("publisher unavailable")


def test_redirect_lookup_appends_canonical_tid_without_dropping_existing_path():
    creator = _insert_creator_user(email=f"redirect_{uuid.uuid4().hex}@example.com")
    booking_link_url = "https://calendly.com/example/redirect-strategy-call"
    booking_link_id = _insert_booking_link(
        creator_id=creator["creator_id"],
        name="Redirect Strategy Call",
        calendly_url=booking_link_url,
    )
    tid = "redirectlookupknowntid"
    _insert_content(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/redirect-breakdown",
        tid=tid,
        created_at=datetime(2026, 3, 6, 15, 0, tzinfo=timezone.utc),
    )

    with TestClient(app) as client:
        response = client.get(f"/r/{tid}", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers.get("X-Request-Id")
    assert response.headers["location"] == f"{booking_link_url}?utm_content={tid}"


def test_redirect_lookup_sets_ccp_sid_cookie_with_14_day_ttl():
    creator = _insert_creator_user(email=f"redirect_{uuid.uuid4().hex}@example.com")
    booking_link_url = "https://calendly.com/example/redirect-cookie-strategy-call"
    booking_link_id = _insert_booking_link(
        creator_id=creator["creator_id"],
        name="Redirect Cookie Strategy Call",
        calendly_url=booking_link_url,
    )
    tid = "redirectlookupcookieset"
    _insert_content(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/redirect-cookie-breakdown",
        tid=tid,
        created_at=datetime(2026, 3, 6, 15, 1, tzinfo=timezone.utc),
    )

    with TestClient(app) as client:
        response = client.get(f"/r/{tid}", follow_redirects=False)

    cookie = _redirect_cookie(response)
    expires_at = parsedate_to_datetime(cookie["expires"])
    remaining = expires_at - datetime.now(timezone.utc)

    assert response.status_code == 302
    assert response.headers.get("X-Request-Id")
    assert response.headers["location"] == f"{booking_link_url}?utm_content={tid}"
    assert cookie.value
    assert cookie["max-age"] == str(14 * 24 * 60 * 60)
    assert cookie["path"] == "/r"
    assert cookie["httponly"]
    assert cookie["samesite"].lower() == "lax"
    assert not cookie["secure"]
    assert timedelta(days=13, hours=23, minutes=59) <= remaining <= timedelta(days=14, minutes=1)


def test_redirect_lookup_reuses_existing_ccp_sid_cookie_value():
    creator = _insert_creator_user(email=f"redirect_{uuid.uuid4().hex}@example.com")
    booking_link_id = _insert_booking_link(
        creator_id=creator["creator_id"],
        name="Redirect Cookie Reuse Call",
        calendly_url="https://calendly.com/example/redirect-cookie-reuse-call",
    )
    tid = "redirectlookupcookiereuse"
    _insert_content(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/redirect-cookie-reuse-breakdown",
        tid=tid,
        created_at=datetime(2026, 3, 6, 15, 2, tzinfo=timezone.utc),
    )

    with TestClient(app) as client:
        first_response = client.get(f"/r/{tid}", follow_redirects=False)
        first_session_id = first_response.cookies.get("ccp_sid")

        second_response = client.get(f"/r/{tid}", follow_redirects=False)
        second_session_id = second_response.cookies.get("ccp_sid")

    assert first_response.status_code == 302
    assert second_response.status_code == 302
    assert first_session_id
    assert second_session_id == first_session_id


def test_redirect_lookup_sets_opaque_ccp_sid_cookie_not_derived_from_tid():
    creator = _insert_creator_user(email=f"redirect_{uuid.uuid4().hex}@example.com")
    booking_link_id = _insert_booking_link(
        creator_id=creator["creator_id"],
        name="Redirect Cookie Opaque Call",
        calendly_url="https://calendly.com/example/redirect-cookie-opaque-call",
    )
    tid = "redirectlookupcookieopaque"
    _insert_content(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/redirect-cookie-opaque-breakdown",
        tid=tid,
        created_at=datetime(2026, 3, 6, 15, 3, tzinfo=timezone.utc),
    )

    with TestClient(app) as client:
        response = client.get(f"/r/{tid}", follow_redirects=False)

    cookie = _redirect_cookie(response)

    assert response.status_code == 302
    assert cookie.value != tid
    assert tid not in cookie.value
    assert re.fullmatch(r"[0-9a-f]{32}", cookie.value)


def test_redirect_lookup_emits_click_event_with_hashed_ip_and_cookie_session():
    creator = _insert_creator_user(email=f"redirect_{uuid.uuid4().hex}@example.com")
    booking_link_url = "https://calendly.com/example/redirect-click-event-call"
    booking_link_id = _insert_booking_link(
        creator_id=creator["creator_id"],
        name="Redirect Click Event Call",
        calendly_url=booking_link_url,
    )
    tid = "redirectlookupclickevent"
    _insert_content(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/redirect-click-breakdown",
        tid=tid,
        created_at=datetime(2026, 3, 6, 15, 4, tzinfo=timezone.utc),
    )
    capture_publisher = _CaptureClickEventPublisher()

    with _override_app_state("click_event_publisher", capture_publisher):
        with TestClient(app, client=("203.0.113.10", 50001)) as client:
            response = client.get(f"/r/{tid}", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == f"{booking_link_url}?utm_content={tid}"
    assert len(capture_publisher.events) == 1

    event = capture_publisher.events[0]
    assert re.fullmatch(r"[0-9a-f]{32}", event.event_id)
    assert event.tid == tid
    assert event.session_id == response.cookies.get("ccp_sid")
    assert event.hashed_ip == hashlib.sha256(b"203.0.113.10").hexdigest()
    assert event.hashed_ip != "203.0.113.10"
    assert event.timestamp.tzinfo == timezone.utc


def test_redirect_lookup_emits_distinct_click_event_ids_for_repeated_redirects():
    creator = _insert_creator_user(email=f"redirect_{uuid.uuid4().hex}@example.com")
    booking_link_id = _insert_booking_link(
        creator_id=creator["creator_id"],
        name="Redirect Repeated Click Event Call",
        calendly_url="https://calendly.com/example/redirect-repeat-click-event-call",
    )
    tid = "redirectlookuprepeatedclickevent"
    _insert_content(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/redirect-repeat-click-breakdown",
        tid=tid,
        created_at=datetime(2026, 3, 6, 15, 6, tzinfo=timezone.utc),
    )
    capture_publisher = _CaptureClickEventPublisher()

    with _override_app_state("click_event_publisher", capture_publisher):
        with TestClient(app, client=("203.0.113.11", 50002)) as client:
            first_response = client.get(f"/r/{tid}", follow_redirects=False)
            second_response = client.get(f"/r/{tid}", follow_redirects=False)

    assert first_response.status_code == 302
    assert second_response.status_code == 302
    assert len(capture_publisher.events) == 2
    assert capture_publisher.events[0].event_id != capture_publisher.events[1].event_id
    assert capture_publisher.events[0].session_id == first_response.cookies.get("ccp_sid")
    assert capture_publisher.events[1].session_id == second_response.cookies.get("ccp_sid")
    assert capture_publisher.events[1].session_id == capture_publisher.events[0].session_id


def test_redirect_lookup_soft_rate_limit_tracks_repeated_bucket_without_blocking_redirect():
    creator = _insert_creator_user(email=f"redirect_{uuid.uuid4().hex}@example.com")
    booking_link_url = "https://calendly.com/example/redirect-soft-limit-call"
    booking_link_id = _insert_booking_link(
        creator_id=creator["creator_id"],
        name="Redirect Soft Limit Call",
        calendly_url=booking_link_url,
    )
    tid = "redirectlookupsoftlimit"
    _insert_content(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/redirect-soft-limit-breakdown",
        tid=tid,
        created_at=datetime(2026, 3, 6, 15, 7, tzinfo=timezone.utc),
    )
    capture_publisher = _CaptureClickEventPublisher()
    redirect_soft_limit_policy = replace(REDIRECT_SOFT_LIMIT_POLICY, max_attempts=2)
    client_ip = "203.0.113.13"

    with patch("app.api.redirects.logger.info") as info_log:
        with _override_app_state("click_event_publisher", capture_publisher):
            with _override_app_state("redirect_soft_limit_policy", redirect_soft_limit_policy):
                with TestClient(app, client=(client_ip, 50004)) as client:
                    first_responses = [
                        client.get(f"/r/{tid}", follow_redirects=False)
                        for _ in range(2)
                    ]
                with TestClient(app, client=(client_ip, 50006)) as client:
                    third_response = client.get(f"/r/{tid}", follow_redirects=False)

    responses = first_responses + [third_response]

    assert all(response.status_code == 302 for response in responses)
    assert all(
        response.headers["location"] == f"{booking_link_url}?utm_content={tid}"
        for response in responses
    )
    assert len(capture_publisher.events) == 3

    hashed_ip = hashlib.sha256(client_ip.encode("utf-8")).hexdigest()
    state = SharedRateLimiter().snapshot_bucket(
        policy=redirect_soft_limit_policy,
        bucket_key=build_redirect_rate_limit_bucket_key(
            hashed_ip=hashed_ip,
            tid=tid,
        ),
    )
    assert state.attempt_count == 3
    assert state.soft_limited is True
    assert state.limit == 2

    resolved_calls = [
        call for call in info_log.call_args_list if call.args[0].startswith("redirect_resolved")
    ]
    limited_calls = [
        call for call in info_log.call_args_list if call.args[0].startswith("redirect_rate_limited")
    ]

    assert len(resolved_calls) == 3
    assert len(limited_calls) == 1

    last_resolved_call = resolved_calls[-1]
    assert (
        last_resolved_call.args[0]
        == "redirect_resolved tid=%s click_event_id=%s soft_limited=%s attempt_count=%s limit=%s"
    )
    assert last_resolved_call.args[1] == tid
    assert re.fullmatch(r"[0-9a-f]{32}", last_resolved_call.args[2])
    assert last_resolved_call.args[3] is True
    assert last_resolved_call.args[4] == 3
    assert last_resolved_call.args[5] == 2

    limited_call = limited_calls[0]
    assert (
        limited_call.args[0]
        == "redirect_rate_limited namespace=%s tid=%s hashed_ip=%s attempt_count=%s limit=%s window_seconds=%s"
    )
    assert limited_call.args[1] == redirect_soft_limit_policy.namespace
    assert limited_call.args[2] == tid
    assert limited_call.args[3] == hashed_ip
    assert limited_call.args[4] == 3
    assert limited_call.args[5] == 2
    assert limited_call.args[6] == int(redirect_soft_limit_policy.window.total_seconds())
    assert client_ip not in "\n".join(str(call) for call in info_log.call_args_list)


def test_redirect_lookup_soft_rate_limit_state_is_tracked_separately_per_tid():
    creator = _insert_creator_user(email=f"redirect_{uuid.uuid4().hex}@example.com")
    booking_link_id = _insert_booking_link(
        creator_id=creator["creator_id"],
        name="Redirect Soft Limit Per Tid Call",
        calendly_url="https://calendly.com/example/redirect-soft-limit-per-tid-call",
    )
    first_tid = "redirectlookupsoftlimitfirst"
    second_tid = "redirectlookupsoftlimitsecond"
    _insert_content(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/redirect-soft-limit-first",
        tid=first_tid,
        created_at=datetime(2026, 3, 6, 15, 8, tzinfo=timezone.utc),
    )
    _insert_content(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/redirect-soft-limit-second",
        tid=second_tid,
        created_at=datetime(2026, 3, 6, 15, 9, tzinfo=timezone.utc),
    )
    redirect_soft_limit_policy = replace(REDIRECT_SOFT_LIMIT_POLICY, max_attempts=2)
    client_ip = "203.0.113.14"

    with _override_app_state("redirect_soft_limit_policy", redirect_soft_limit_policy):
        with TestClient(app, client=(client_ip, 50005)) as client:
            responses = [
                client.get(f"/r/{first_tid}", follow_redirects=False),
                client.get(f"/r/{first_tid}", follow_redirects=False),
                client.get(f"/r/{first_tid}", follow_redirects=False),
                client.get(f"/r/{second_tid}", follow_redirects=False),
            ]

    assert all(response.status_code == 302 for response in responses)

    hashed_ip = hashlib.sha256(client_ip.encode("utf-8")).hexdigest()
    fresh_rate_limiter = SharedRateLimiter()
    first_state = fresh_rate_limiter.snapshot_bucket(
        policy=redirect_soft_limit_policy,
        bucket_key=build_redirect_rate_limit_bucket_key(
            hashed_ip=hashed_ip,
            tid=first_tid,
        ),
    )
    second_state = fresh_rate_limiter.snapshot_bucket(
        policy=redirect_soft_limit_policy,
        bucket_key=build_redirect_rate_limit_bucket_key(
            hashed_ip=hashed_ip,
            tid=second_tid,
        ),
    )

    assert first_state.attempt_count == 3
    assert first_state.soft_limited is True
    assert second_state.attempt_count == 1
    assert second_state.soft_limited is False


def test_redirect_lookup_preserves_existing_query_params_when_appending_canonical_tid():
    creator = _insert_creator_user(email=f"redirect_{uuid.uuid4().hex}@example.com")
    tid = "redirectlookupqueryparamstid"
    booking_link_id = _insert_booking_link(
        creator_id=creator["creator_id"],
        name="Redirect Strategy Call With Query Params",
        calendly_url="https://calendly.com/example/redirect-strategy-call?month=2026-03&utm_source=linkedin",
    )
    _insert_content(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/redirect-query-breakdown",
        tid=tid,
        created_at=datetime(2026, 3, 6, 15, 5, tzinfo=timezone.utc),
    )

    with TestClient(app) as client:
        response = client.get(f"/r/{tid}", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers.get("X-Request-Id")
    assert (
        response.headers["location"]
        == "https://calendly.com/example/redirect-strategy-call?month=2026-03&utm_source=linkedin&utm_content=redirectlookupqueryparamstid"
    )


def test_redirect_lookup_rewrites_existing_utm_content_query_param_to_canonical_tid():
    creator = _insert_creator_user(email=f"redirect_{uuid.uuid4().hex}@example.com")
    tid = "redirectlookupcanonicaltid"
    booking_link_id = _insert_booking_link(
        creator_id=creator["creator_id"],
        name="Redirect Strategy Call With Existing Tid",
        calendly_url="https://calendly.com/example/redirect-strategy-call?month=2026-03&utm_content=stale-tid",
    )
    _insert_content(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/redirect-canonical-breakdown",
        tid=tid,
        created_at=datetime(2026, 3, 6, 15, 10, tzinfo=timezone.utc),
    )

    with TestClient(app) as client:
        response = client.get(f"/r/{tid}", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers.get("X-Request-Id")
    assert (
        response.headers["location"]
        == "https://calendly.com/example/redirect-strategy-call?month=2026-03&utm_content=redirectlookupcanonicaltid"
    )


def test_redirect_lookup_rewrites_legacy_tid_query_param_to_canonical_utm_content():
    creator = _insert_creator_user(email=f"redirect_{uuid.uuid4().hex}@example.com")
    tid = "redirectlookuplegacytid"
    booking_link_id = _insert_booking_link(
        creator_id=creator["creator_id"],
        name="Redirect Strategy Call With Legacy Tid",
        calendly_url="https://calendly.com/example/redirect-strategy-call?month=2026-03&tid=stale-tid",
    )
    _insert_content(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/redirect-legacy-tid-breakdown",
        tid=tid,
        created_at=datetime(2026, 3, 6, 15, 10, tzinfo=timezone.utc),
    )

    with TestClient(app) as client:
        response = client.get(f"/r/{tid}", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers.get("X-Request-Id")
    assert (
        response.headers["location"]
        == "https://calendly.com/example/redirect-strategy-call?month=2026-03&utm_content=redirectlookuplegacytid"
    )


def test_redirect_lookup_returns_safe_404_for_unknown_tid():
    with TestClient(app) as client:
        response = client.get("/r/unknown-redirect-tid", follow_redirects=False)

    assert response.status_code == 404
    assert response.headers.get("X-Request-Id")
    assert response.json() == {"detail": "link not found"}


def test_redirect_lookup_returns_302_when_click_event_publish_fails():
    creator = _insert_creator_user(email=f"redirect_{uuid.uuid4().hex}@example.com")
    booking_link_url = "https://calendly.com/example/redirect-click-publish-failure-call"
    booking_link_id = _insert_booking_link(
        creator_id=creator["creator_id"],
        name="Redirect Click Publish Failure Call",
        calendly_url=booking_link_url,
    )
    tid = "redirectlookupclickpublishfailure"
    _insert_content(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/redirect-click-failure-breakdown",
        tid=tid,
        created_at=datetime(2026, 3, 6, 15, 11, tzinfo=timezone.utc),
    )

    with _override_app_state("click_event_publisher", _FailingClickEventPublisher()):
        with patch("app.api.redirects.logger.warning") as warning_log:
            with TestClient(app, client=("203.0.113.12", 50003)) as client:
                response = client.get(f"/r/{tid}", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == f"{booking_link_url}?utm_content={tid}"
    assert response.cookies.get("ccp_sid")
    warning_log.assert_called_once()
    assert "click_event_publish_failed" in warning_log.call_args.args[0]
