import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import create_engine, text

from app.core.config import get_settings
from app.main import app
from app.services.email_stub import get_magic_link_outbox

HTML_ACCEPT_HEADERS = {"Accept": "text/html,application/xhtml+xml"}
SESSION_COOKIE_NAME = "ccp_creator_session"


def _engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def _latest_magic_link_token_for_email(email: str) -> str:
    for message in reversed(get_magic_link_outbox()):
        if message["email"] == email:
            return message["token"]
    raise AssertionError(f"No magic-link token found for {email}")


def _insert_creator_user(
    *,
    email: str,
    name: str = "UI Creator",
    stripe_connect_status: str = "pending",
    stripe_account_id: str | None = None,
    stripe_connected_at: datetime | None = None,
) -> dict[str, str]:
    creator_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())

    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO creators ("
                "id, name, stripe_connect_status, stripe_account_id, stripe_connected_at"
                ") VALUES ("
                ":id, :name, :stripe_connect_status, :stripe_account_id, :stripe_connected_at"
                ")"
            ),
            {
                "id": creator_id,
                "name": name,
                "stripe_connect_status": stripe_connect_status,
                "stripe_account_id": stripe_account_id,
                "stripe_connected_at": stripe_connected_at,
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


def _access_token(*, user_id: str, creator_id: str, email: str, expires_delta: timedelta) -> str:
    settings = get_settings()
    issued_at = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "creator_id": creator_id,
        "email": email,
        "iat": issued_at,
        "exp": issued_at + expires_delta,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def _insert_booking_link(
    *,
    creator_id: str,
    name: str,
    calendly_url: str,
    billing_amount_cents: int | None = None,
    billing_currency: str | None = None,
) -> str:
    booking_link_id = str(uuid.uuid4())

    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO booking_links "
                "(id, creator_id, name, calendly_url, billing_amount_cents, billing_currency) "
                "VALUES (:id, :creator_id, :name, :calendly_url, :billing_amount_cents, :billing_currency)"
            ),
            {
                "id": booking_link_id,
                "creator_id": creator_id,
                "name": name,
                "calendly_url": calendly_url,
                "billing_amount_cents": billing_amount_cents,
                "billing_currency": billing_currency,
            },
        )

    return booking_link_id


def _insert_content(
    *,
    creator_id: str,
    booking_link_id: str,
    source_url: str,
    tid: str,
    content_id: str | None = None,
    created_at: datetime | None = None,
) -> str:
    content_id = content_id or str(uuid.uuid4())
    created_at = created_at or datetime.now(timezone.utc)

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


class _StubStripeProvider:
    def __init__(self, *, account_id: str = "acct_ui_story38"):
        self.account_id = account_id
        self.start_calls: list[dict[str, str]] = []
        self.callback_calls: list[dict[str, str]] = []

    def build_connect_onboarding_url(self, *, creator_id: str, state: str) -> str:
        self.start_calls.append({"creator_id": creator_id, "state": state})
        return (
            "https://connect.stripe.com/oauth/authorize"
            f"?response_type=code&client_id=ca_story38_ui&state={state}&creator_id={creator_id}"
        )

    def exchange_connect_callback(self, *, code: str, state: str) -> str:
        self.callback_calls.append({"code": code, "state": state})
        return self.account_id


def test_sign_in_page_is_browser_accessible():
    with TestClient(app) as client:
        response = client.get("/sign-in", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<form" in response.text
    assert 'action="/sign-in"' in response.text
    assert 'name="email"' in response.text
    assert "Creator sign in" in response.text


def test_sign_in_start_redirects_to_confirmation_without_echoing_email():
    email = f"ui_sign_in_{uuid.uuid4().hex}@example.com"

    with TestClient(app) as client:
        response = client.post(
            "/sign-in",
            data={"email": email},
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/sign-in?status=sent"
    assert email not in response.headers["location"]
    assert _latest_magic_link_token_for_email(email)


def test_app_shell_redirects_unauthenticated_browser_requests():
    with TestClient(app) as client:
        response = client.get("/app", headers=HTML_ACCEPT_HEADERS, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/sign-in"


def test_booking_links_page_redirects_unauthenticated_browser_requests():
    with TestClient(app) as client:
        response = client.get(
            "/app/booking-links",
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/sign-in"


def test_content_page_redirects_unauthenticated_browser_requests():
    with TestClient(app) as client:
        response = client.get(
            "/app/content",
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/sign-in"


def test_browser_magic_link_verify_sets_session_cookie_and_lands_in_app_shell():
    email = f"ui_shell_{uuid.uuid4().hex}@example.com"

    with TestClient(app) as client:
        start_response = client.post(
            "/sign-in",
            data={"email": email},
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )
        raw_token = _latest_magic_link_token_for_email(email)

        verify_response = client.get(
            "/auth/magic-link/verify",
            params={"token": raw_token},
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )
        shell_response = client.get("/app", headers=HTML_ACCEPT_HEADERS)

    assert start_response.status_code == 303
    assert verify_response.status_code == 303
    assert verify_response.headers["location"] == "/app"
    assert raw_token not in verify_response.headers["location"]
    assert raw_token not in verify_response.text
    assert f"{SESSION_COOKIE_NAME}=" in verify_response.headers["set-cookie"]
    assert raw_token not in verify_response.headers["set-cookie"]

    assert shell_response.status_code == 200
    assert shell_response.headers["content-type"].startswith("text/html")
    assert "Setup Home" in shell_response.text
    assert "Creator Home" in shell_response.text
    assert email in shell_response.text
    assert raw_token not in shell_response.text


def test_browser_magic_link_verify_failure_redirects_without_echoing_token():
    raw_token = "invalid_browser_magic_link_token"

    with TestClient(app) as client:
        response = client.get(
            "/auth/magic-link/verify",
            params={"token": raw_token},
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/sign-in?status=invalid-link"
    assert raw_token not in response.headers["location"]
    assert raw_token not in response.text


def test_setup_home_pending_stripe_state_shows_connect_cta_and_checklist():
    inserted = _insert_creator_user(
        email=f"ui_pending_{uuid.uuid4().hex}@example.com",
        name="Pending Creator",
        stripe_connect_status="pending",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get("/app", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Setup Home" in response.text
    assert "Stripe setup is still pending" in response.text
    assert "Start Stripe setup" in response.text
    assert 'action="/app/stripe/connect/start"' in response.text
    assert "Add a booking link" in response.text
    assert 'href="/app/booking-links"' in response.text
    assert "Create a tracked link" in response.text
    assert 'href="/app/content"' in response.text
    assert "later invoice automation can create invoices on your account" in response.text


def test_booking_links_page_empty_state_renders_form_and_next_step_copy():
    inserted = _insert_creator_user(
        email=f"ui_booking_links_empty_{uuid.uuid4().hex}@example.com",
        name="Empty State Creator",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get("/app/booking-links", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Booking Links" in response.text
    assert "Create the first booking link" in response.text
    assert 'action="/app/booking-links"' in response.text
    assert "Billing amount in cents" in response.text
    assert "0 saved" in response.text


def test_booking_links_page_create_success_shows_saved_link_and_billing_defaults():
    inserted = _insert_creator_user(
        email=f"ui_booking_links_create_{uuid.uuid4().hex}@example.com",
        name="Booking Link Creator",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        create_response = client.post(
            "/app/booking-links",
            data={
                "name": "Paid Deep Dive",
                "calendly_url": "https://calendly.com/example/paid-deep-dive",
                "billing_amount_cents": "15000",
                "billing_currency": " usd ",
            },
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )
        page_response = client.get(
            create_response.headers["location"],
            headers=HTML_ACCEPT_HEADERS,
        )

    assert create_response.status_code == 303
    assert create_response.headers["location"] == "/app/booking-links?status=created"

    assert page_response.status_code == 200
    assert "Booking link saved" in page_response.text
    assert "Paid Deep Dive" in page_response.text
    assert "https://calendly.com/example/paid-deep-dive" in page_response.text
    assert "Ready for invoice defaults: USD 150.00" in page_response.text
    assert "1 saved" in page_response.text


def test_booking_links_page_validation_feedback_preserves_input_and_page_state():
    inserted = _insert_creator_user(
        email=f"ui_booking_links_invalid_{uuid.uuid4().hex}@example.com",
        name="Validation Creator",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.post(
            "/app/booking-links",
            data={
                "name": "Broken Link",
                "calendly_url": "http://example.com/not-calendly",
                "billing_amount_cents": "0",
                "billing_currency": "USDX",
            },
            headers=HTML_ACCEPT_HEADERS,
        )

    assert response.status_code == 200
    assert "Fix the highlighted fields" in response.text
    assert "must use https" in response.text
    assert "must be a positive integer amount in cents" in response.text
    assert "must be a 3-letter currency code" in response.text
    assert 'value="Broken Link"' in response.text
    assert 'value="http://example.com/not-calendly"' in response.text
    assert 'value="0"' in response.text
    assert 'value="USDX"' in response.text


def test_booking_links_page_lists_only_current_creators_links():
    creator_a = _insert_creator_user(
        email=f"ui_booking_links_creator_a_{uuid.uuid4().hex}@example.com",
        name="Creator A",
    )
    creator_b = _insert_creator_user(
        email=f"ui_booking_links_creator_b_{uuid.uuid4().hex}@example.com",
        name="Creator B",
    )
    access_token = _access_token(
        user_id=creator_a["user_id"],
        creator_id=creator_a["creator_id"],
        email=creator_a["email"],
        expires_delta=timedelta(hours=24),
    )

    _insert_booking_link(
        creator_id=creator_a["creator_id"],
        name="Creator A Strategy",
        calendly_url="https://calendly.com/example/creator-a-strategy",
    )
    _insert_booking_link(
        creator_id=creator_b["creator_id"],
        name="Creator B Intro",
        calendly_url="https://calendly.com/example/creator-b-intro",
        billing_amount_cents=9000,
        billing_currency="EUR",
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get("/app/booking-links", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Creator A Strategy" in response.text
    assert "https://calendly.com/example/creator-a-strategy" in response.text
    assert "No billing defaults yet" in response.text
    assert "Creator B Intro" not in response.text
    assert "https://calendly.com/example/creator-b-intro" not in response.text


def test_content_page_without_booking_links_explains_prerequisite():
    inserted = _insert_creator_user(
        email=f"ui_content_empty_{uuid.uuid4().hex}@example.com",
        name="No Booking Link Creator",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get("/app/content", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Content" in response.text
    assert "Create a booking link first" in response.text
    assert 'href="/app/booking-links"' in response.text
    assert 'action="/app/content"' not in response.text
    assert "0 saved" in response.text


def test_content_page_create_success_shows_tracked_link_and_saved_item():
    inserted = _insert_creator_user(
        email=f"ui_content_create_{uuid.uuid4().hex}@example.com",
        name="Tracked Content Creator",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="Strategy Call",
        calendly_url="https://calendly.com/example/strategy-call",
    )
    tracked_base_url = get_settings().tracked_link_base_url.rstrip("/")

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        create_response = client.post(
            "/app/content",
            data={
                "source_url": "https://example.com/posts/story40-launch-plan",
                "booking_link_id": booking_link_id,
            },
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )
        page_response = client.get(
            create_response.headers["location"],
            headers=HTML_ACCEPT_HEADERS,
        )

    created_tid = parse_qs(urlparse(create_response.headers["location"]).query)["tid"][0]

    assert create_response.status_code == 303
    assert create_response.headers["location"] == f"/app/content?status=created&tid={created_tid}"

    assert page_response.status_code == 200
    assert "Tracked link ready" in page_response.text
    assert f"{tracked_base_url}/r/{created_tid}" in page_response.text
    assert "story40-launch-plan" in page_response.text
    assert "Strategy Call" in page_response.text
    assert 'data-copy-source="created-tracked-url"' in page_response.text
    assert "1 saved" in page_response.text


def test_content_page_lists_only_current_creators_content():
    creator_a = _insert_creator_user(
        email=f"ui_content_creator_a_{uuid.uuid4().hex}@example.com",
        name="Content Creator A",
    )
    creator_b = _insert_creator_user(
        email=f"ui_content_creator_b_{uuid.uuid4().hex}@example.com",
        name="Content Creator B",
    )
    access_token = _access_token(
        user_id=creator_a["user_id"],
        creator_id=creator_a["creator_id"],
        email=creator_a["email"],
        expires_delta=timedelta(hours=24),
    )
    creator_a_booking_link_id = _insert_booking_link(
        creator_id=creator_a["creator_id"],
        name="Creator A Strategy",
        calendly_url="https://calendly.com/example/creator-a-strategy",
    )
    creator_b_booking_link_id = _insert_booking_link(
        creator_id=creator_b["creator_id"],
        name="Creator B Strategy",
        calendly_url="https://calendly.com/example/creator-b-strategy",
    )
    tracked_base_url = get_settings().tracked_link_base_url.rstrip("/")

    _insert_content(
        creator_id=creator_a["creator_id"],
        booking_link_id=creator_a_booking_link_id,
        source_url="https://example.com/posts/creator-a-content",
        tid="uiacontenttid",
    )
    _insert_content(
        creator_id=creator_b["creator_id"],
        booking_link_id=creator_b_booking_link_id,
        source_url="https://example.com/posts/creator-b-content",
        tid="uibcontenttid",
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get("/app/content", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "creator-a-content" in response.text
    assert f"{tracked_base_url}/r/uiacontenttid" in response.text
    assert "Creator A Strategy" in response.text
    assert "creator-b-content" not in response.text
    assert f"{tracked_base_url}/r/uibcontenttid" not in response.text
    assert "Creator B Strategy" not in response.text


def test_setup_home_disconnected_stripe_state_shows_reconnect_cta():
    inserted = _insert_creator_user(
        email=f"ui_disconnected_{uuid.uuid4().hex}@example.com",
        name="Disconnected Creator",
        stripe_connect_status="disconnected",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get("/app", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Stripe is disconnected" in response.text
    assert "Reconnect Stripe" in response.text
    assert 'action="/app/stripe/connect/start"' in response.text


def test_setup_home_connected_stripe_state_shows_connected_details():
    connected_at = datetime.now(timezone.utc).replace(microsecond=0)
    inserted = _insert_creator_user(
        email=f"ui_connected_{uuid.uuid4().hex}@example.com",
        name="Connected Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_connected",
        stripe_connected_at=connected_at,
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get("/app", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Stripe is connected" in response.text
    assert "acct_ui_connected" in response.text
    assert 'action="/app/stripe/connect/start"' not in response.text
    assert "Connected account" in response.text


def test_setup_home_connect_cta_redirects_to_stripe_and_callback_returns_to_app():
    inserted = _insert_creator_user(
        email=f"ui_cta_{uuid.uuid4().hex}@example.com",
        name="CTA Creator",
        stripe_connect_status="pending",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    provider = _StubStripeProvider(account_id="acct_story38_browser")

    with _override_app_state("stripe_provider", provider):
        with TestClient(app) as client:
            client.cookies.set(SESSION_COOKIE_NAME, access_token)

            start_response = client.post(
                "/app/stripe/connect/start",
                headers=HTML_ACCEPT_HEADERS,
                follow_redirects=False,
            )
            start_location = start_response.headers["location"]
            start_query = parse_qs(urlparse(start_location).query)
            callback_response = client.get(
                "/stripe/connect/callback",
                params={
                    "code": "auth_code_story38_browser",
                    "state": start_query["state"][0],
                },
                headers=HTML_ACCEPT_HEADERS,
                follow_redirects=False,
            )
            app_response = client.get("/app", headers=HTML_ACCEPT_HEADERS)

    assert start_response.status_code == 303
    assert start_location.startswith("https://connect.stripe.com/oauth/authorize")
    assert provider.start_calls == [
        {
            "creator_id": inserted["creator_id"],
            "state": start_query["state"][0],
        }
    ]

    assert callback_response.status_code == 303
    assert callback_response.headers["location"] == "/app"
    assert provider.callback_calls == [
        {
            "code": "auth_code_story38_browser",
            "state": start_query["state"][0],
        }
    ]

    assert app_response.status_code == 200
    assert "Stripe is connected" in app_response.text
    assert "acct_story38_browser" in app_response.text


def test_app_shell_clears_expired_session_cookie():
    inserted = _insert_creator_user(email=f"ui_expired_{uuid.uuid4().hex}@example.com")
    expired_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(minutes=-1),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, expired_token)
        response = client.get("/app", headers=HTML_ACCEPT_HEADERS, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/sign-in"
    assert f"{SESSION_COOKIE_NAME}=" in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]
