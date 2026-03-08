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
from app.services.invoice_payment_events import UNATTRIBUTED_REASON_MISSING_TID

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


def _insert_booking(
    *,
    creator_id: str,
    booking_link_id: str,
    tid: str,
    calendly_booking_uuid: str,
    booked_at: datetime,
    status: str = "created",
    canceled_at: datetime | None = None,
    email: str = "booked@example.com",
) -> str:
    booking_id = str(uuid.uuid4())

    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO bookings "
                "(id, creator_id, booking_link_id, tid, calendly_booking_uuid, email, status, booked_at, canceled_at) "
                "VALUES "
                "(:id, :creator_id, :booking_link_id, :tid, :calendly_booking_uuid, :email, :status, :booked_at, :canceled_at)"
            ),
            {
                "id": booking_id,
                "creator_id": creator_id,
                "booking_link_id": booking_link_id,
                "tid": tid,
                "calendly_booking_uuid": calendly_booking_uuid,
                "email": email,
                "status": status,
                "booked_at": booked_at,
                "canceled_at": canceled_at,
            },
        )

    return booking_id


def _insert_invoice(
    *,
    creator_id: str,
    booking_id: str,
    tid: str,
    stripe_account_id: str,
    stripe_invoice_id: str,
    amount_cents: int,
    paid_at: datetime,
    status: str = "paid",
    currency: str = "USD",
) -> str:
    invoice_id = str(uuid.uuid4())

    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO invoices "
                "(id, creator_id, booking_id, tid, stripe_account_id, stripe_invoice_id, amount_cents, currency, status, issued_at, paid_at, voided_at) "
                "VALUES "
                "(:id, :creator_id, :booking_id, :tid, :stripe_account_id, :stripe_invoice_id, :amount_cents, :currency, :status, :issued_at, :paid_at, :voided_at)"
            ),
            {
                "id": invoice_id,
                "creator_id": creator_id,
                "booking_id": booking_id,
                "tid": tid,
                "stripe_account_id": stripe_account_id,
                "stripe_invoice_id": stripe_invoice_id,
                "amount_cents": amount_cents,
                "currency": currency,
                "status": status,
                "issued_at": paid_at - timedelta(hours=1),
                "paid_at": paid_at,
                "voided_at": None,
            },
        )

    return invoice_id


def _insert_unmatched_payment_event(
    *,
    creator_id: str,
    stripe_account_id: str,
    stripe_event_id: str,
    stripe_invoice_id: str,
    reason: str,
    paid_at: datetime,
) -> str:
    payment_event_id = str(uuid.uuid4())

    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO invoice_payment_events "
                "(id, stripe_event_id, stripe_event_type, stripe_account_id, stripe_invoice_id, invoice_id, creator_id, booking_id, tid, status, unattributed_reason, paid_at, received_at, processed_at) "
                "VALUES "
                "(:id, :stripe_event_id, :stripe_event_type, :stripe_account_id, :stripe_invoice_id, :invoice_id, :creator_id, :booking_id, :tid, :status, :unattributed_reason, :paid_at, :received_at, :processed_at)"
            ),
            {
                "id": payment_event_id,
                "stripe_event_id": stripe_event_id,
                "stripe_event_type": "invoice.paid",
                "stripe_account_id": stripe_account_id,
                "stripe_invoice_id": stripe_invoice_id,
                "invoice_id": None,
                "creator_id": creator_id,
                "booking_id": None,
                "tid": None,
                "status": "unmatched",
                "unattributed_reason": reason,
                "paid_at": paid_at,
                "received_at": paid_at,
                "processed_at": None,
            },
        )

    return payment_event_id


def _insert_matched_payment_event(
    *,
    creator_id: str,
    booking_id: str,
    tid: str,
    invoice_id: str,
    stripe_account_id: str,
    stripe_event_id: str,
    stripe_invoice_id: str,
    paid_at: datetime,
    status: str = "applied",
) -> str:
    payment_event_id = str(uuid.uuid4())

    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO invoice_payment_events "
                "(id, stripe_event_id, stripe_event_type, stripe_account_id, stripe_invoice_id, invoice_id, creator_id, booking_id, tid, status, unattributed_reason, paid_at, received_at, processed_at) "
                "VALUES "
                "(:id, :stripe_event_id, :stripe_event_type, :stripe_account_id, :stripe_invoice_id, :invoice_id, :creator_id, :booking_id, :tid, :status, :unattributed_reason, :paid_at, :received_at, :processed_at)"
            ),
            {
                "id": payment_event_id,
                "stripe_event_id": stripe_event_id,
                "stripe_event_type": "invoice.paid",
                "stripe_account_id": stripe_account_id,
                "stripe_invoice_id": stripe_invoice_id,
                "invoice_id": invoice_id,
                "creator_id": creator_id,
                "booking_id": booking_id,
                "tid": tid,
                "status": status,
                "unattributed_reason": None,
                "paid_at": paid_at,
                "received_at": paid_at,
                "processed_at": paid_at,
            },
        )

    return payment_event_id


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


def test_booking_activity_page_redirects_unauthenticated_browser_requests():
    with TestClient(app) as client:
        response = client.get(
            "/app/bookings",
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/sign-in"


def test_reports_page_redirects_unauthenticated_browser_requests():
    with TestClient(app) as client:
        response = client.get(
            "/app/reports",
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
    assert "Review booking activity" in response.text
    assert 'href="/app/bookings"' in response.text
    assert 'href="/app/reports"' in response.text
    assert 'class="wrap-anywhere"' in response.text
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
    assert 'class="wrap-anywhere"' in response.text


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
    assert 'class="wrap-anywhere"' not in response.text


def test_booking_activity_page_empty_state_explains_delay_and_next_steps():
    inserted = _insert_creator_user(
        email=f"ui_booking_activity_empty_{uuid.uuid4().hex}@example.com",
        name="No Booking Activity Creator",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get("/app/bookings", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Booking Activity" in response.text
    assert "No bookings captured yet" in response.text
    assert "may not appear immediately" in response.text
    assert "Create tracked content" in response.text
    assert 'href="/app/content"' in response.text
    assert "0 captured" in response.text
    assert 'class="wrap-anywhere"' in response.text


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


def test_booking_activity_page_lists_only_current_creators_bookings_with_context_and_status():
    creator_a = _insert_creator_user(
        email=f"ui_booking_activity_creator_a_{uuid.uuid4().hex}@example.com",
        name="Booking Activity Creator A",
    )
    creator_b = _insert_creator_user(
        email=f"ui_booking_activity_creator_b_{uuid.uuid4().hex}@example.com",
        name="Booking Activity Creator B",
    )
    access_token = _access_token(
        user_id=creator_a["user_id"],
        creator_id=creator_a["creator_id"],
        email=creator_a["email"],
        expires_delta=timedelta(hours=24),
    )

    creator_a_booking_link_created = _insert_booking_link(
        creator_id=creator_a["creator_id"],
        name="Creator A Strategy",
        calendly_url="https://calendly.com/example/creator-a-strategy",
    )
    creator_a_booking_link_canceled = _insert_booking_link(
        creator_id=creator_a["creator_id"],
        name="Creator A Workshop",
        calendly_url="https://calendly.com/example/creator-a-workshop",
    )
    creator_b_booking_link_id = _insert_booking_link(
        creator_id=creator_b["creator_id"],
        name="Creator B Intro",
        calendly_url="https://calendly.com/example/creator-b-intro",
    )

    _insert_content(
        creator_id=creator_a["creator_id"],
        booking_link_id=creator_a_booking_link_created,
        source_url="https://example.com/posts/creator-a-created",
        tid="uiactivitycreated",
    )
    _insert_content(
        creator_id=creator_a["creator_id"],
        booking_link_id=creator_a_booking_link_canceled,
        source_url="https://example.com/posts/creator-a-canceled",
        tid="uiactivitycanceled",
    )
    _insert_content(
        creator_id=creator_b["creator_id"],
        booking_link_id=creator_b_booking_link_id,
        source_url="https://example.com/posts/creator-b-booking",
        tid="uibbookingactivity",
    )

    _insert_booking(
        creator_id=creator_a["creator_id"],
        booking_link_id=creator_a_booking_link_created,
        tid="uiactivitycreated",
        calendly_booking_uuid="BOOK_UI_ACTIVITY_CREATED",
        booked_at=datetime(2026, 3, 7, 17, 0, tzinfo=timezone.utc),
        status="created",
    )
    _insert_booking(
        creator_id=creator_a["creator_id"],
        booking_link_id=creator_a_booking_link_canceled,
        tid="uiactivitycanceled",
        calendly_booking_uuid="BOOK_UI_ACTIVITY_CANCELED",
        booked_at=datetime(2026, 3, 7, 18, 0, tzinfo=timezone.utc),
        status="canceled",
        canceled_at=datetime(2026, 3, 7, 18, 30, tzinfo=timezone.utc),
    )
    _insert_booking(
        creator_id=creator_b["creator_id"],
        booking_link_id=creator_b_booking_link_id,
        tid="uibbookingactivity",
        calendly_booking_uuid="BOOK_UI_ACTIVITY_OTHER_CREATOR",
        booked_at=datetime(2026, 3, 7, 19, 0, tzinfo=timezone.utc),
        status="created",
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get("/app/bookings", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Recent booking activity" in response.text
    assert "2 captured" in response.text
    assert "Creator A Strategy" in response.text
    assert "Creator A Workshop" in response.text
    assert "https://example.com/posts/creator-a-created" in response.text
    assert "https://example.com/posts/creator-a-canceled" in response.text
    assert "uiactivitycreated" in response.text
    assert "uiactivitycanceled" in response.text
    assert "Created" in response.text
    assert "Canceled" in response.text
    assert "March 07, 2026 at 06:00 PM UTC" in response.text
    assert "March 07, 2026 at 06:30 PM UTC" in response.text
    assert "Creator B Intro" not in response.text
    assert "https://example.com/posts/creator-b-booking" not in response.text
    assert "uibbookingactivity" not in response.text
    assert response.text.index("creator-a-canceled") < response.text.index("creator-a-created")


def test_reports_page_lists_invoice_backed_rows_and_supports_paid_date_filters():
    creator_a = _insert_creator_user(
        email=f"ui_reports_creator_a_{uuid.uuid4().hex}@example.com",
        name="Reports Creator A",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_reports_a",
    )
    creator_b = _insert_creator_user(
        email=f"ui_reports_creator_b_{uuid.uuid4().hex}@example.com",
        name="Reports Creator B",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_reports_b",
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
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    old_content_tid = f"uireportsold{uuid.uuid4().hex[:8]}"
    current_content_tid = f"uireportscurrent{uuid.uuid4().hex[:8]}"
    _insert_content(
        creator_id=creator_a["creator_id"],
        booking_link_id=creator_a_booking_link_id,
        source_url="https://example.com/posts/reports-old",
        tid=old_content_tid,
    )
    _insert_content(
        creator_id=creator_a["creator_id"],
        booking_link_id=creator_a_booking_link_id,
        source_url="https://example.com/posts/reports-current",
        tid=current_content_tid,
    )

    old_booking_id = _insert_booking(
        creator_id=creator_a["creator_id"],
        booking_link_id=creator_a_booking_link_id,
        tid=old_content_tid,
        calendly_booking_uuid=f"BOOK_UI_REPORTS_OLD_{uuid.uuid4().hex[:8]}",
        booked_at=datetime(2026, 3, 7, 12, 0, tzinfo=timezone.utc),
    )
    current_booking_id = _insert_booking(
        creator_id=creator_a["creator_id"],
        booking_link_id=creator_a_booking_link_id,
        tid=current_content_tid,
        calendly_booking_uuid=f"BOOK_UI_REPORTS_CURRENT_{uuid.uuid4().hex[:8]}",
        booked_at=datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc),
    )
    _insert_invoice(
        creator_id=creator_a["creator_id"],
        booking_id=old_booking_id,
        tid=old_content_tid,
        stripe_account_id="acct_ui_reports_a",
        stripe_invoice_id=f"in_ui_reports_old_{uuid.uuid4().hex[:8]}",
        amount_cents=5000,
        paid_at=datetime(2026, 3, 7, 13, 0, tzinfo=timezone.utc),
    )
    _insert_invoice(
        creator_id=creator_a["creator_id"],
        booking_id=current_booking_id,
        tid=current_content_tid,
        stripe_account_id="acct_ui_reports_a",
        stripe_invoice_id=f"in_ui_reports_current_{uuid.uuid4().hex[:8]}",
        amount_cents=19500,
        paid_at=datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc),
    )
    _insert_unmatched_payment_event(
        creator_id=creator_a["creator_id"],
        stripe_account_id="acct_ui_reports_a",
        stripe_event_id=f"evt_ui_reports_{uuid.uuid4().hex[:8]}",
        stripe_invoice_id=f"in_ui_reports_unmatched_{uuid.uuid4().hex[:8]}",
        reason=UNATTRIBUTED_REASON_MISSING_TID,
        paid_at=datetime(2026, 3, 8, 11, 0, tzinfo=timezone.utc),
    )

    creator_b_booking_link_id = _insert_booking_link(
        creator_id=creator_b["creator_id"],
        name="Creator B Strategy",
        calendly_url="https://calendly.com/example/creator-b-strategy",
        billing_amount_cents=88000,
        billing_currency="USD",
    )
    creator_b_tid = f"uireportsb{uuid.uuid4().hex[:8]}"
    _insert_content(
        creator_id=creator_b["creator_id"],
        booking_link_id=creator_b_booking_link_id,
        source_url="https://example.com/posts/reports-other-creator",
        tid=creator_b_tid,
    )
    creator_b_booking_id = _insert_booking(
        creator_id=creator_b["creator_id"],
        booking_link_id=creator_b_booking_link_id,
        tid=creator_b_tid,
        calendly_booking_uuid=f"BOOK_UI_REPORTS_OTHER_{uuid.uuid4().hex[:8]}",
        booked_at=datetime(2026, 3, 8, 14, 0, tzinfo=timezone.utc),
    )
    _insert_invoice(
        creator_id=creator_b["creator_id"],
        booking_id=creator_b_booking_id,
        tid=creator_b_tid,
        stripe_account_id="acct_ui_reports_b",
        stripe_invoice_id=f"in_ui_reports_other_{uuid.uuid4().hex[:8]}",
        amount_cents=88000,
        paid_at=datetime(2026, 3, 8, 15, 0, tzinfo=timezone.utc),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get(
            "/app/reports",
            params={"start_date": "2026-03-08", "end_date": "2026-03-08"},
            headers=HTML_ACCEPT_HEADERS,
        )

    assert response.status_code == 200
    assert "Reports" in response.text
    assert '<a href="/app/reports" class="nav-link active">Reports</a>' in response.text
    assert "reports-current" in response.text
    assert "reports-old" not in response.text
    assert "reports-other-creator" not in response.text
    assert response.text.count('value="2026-03-08"') == 2
    assert "19.50" not in response.text
    assert "195.00" in response.text
    assert "1 paid invoice" in response.text
    assert "1 paid booking" in response.text
    assert "Missing tracking ID" in response.text
    assert "1 event waiting on more attribution context" in response.text
    assert "These backlog events are separate from the paid content rows below" in response.text
    assert (
        'href="/app/reports/export.csv?start_date=2026-03-08&amp;end_date=2026-03-08"'
        in response.text
    )
    assert (
        'href="/app/reports/explanations/unattributed?start_date=2026-03-08&amp;end_date=2026-03-08"'
        in response.text
    )
    assert (
        f'href="/app/reports/explanations/paid/{current_content_tid}?start_date=2026-03-08&amp;end_date=2026-03-08"'
        in response.text
    )
    assert "does not show a numeric blocked count yet" in response.text


def test_reports_paid_explanation_page_renders_creator_scoped_canonical_chain():
    creator = _insert_creator_user(
        email=f"ui_reports_explanation_{uuid.uuid4().hex}@example.com",
        name="Reports Explanation Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_reports_explanation",
    )
    access_token = _access_token(
        user_id=creator["user_id"],
        creator_id=creator["creator_id"],
        email=creator["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=creator["creator_id"],
        name="Explanation Strategy",
        calendly_url="https://calendly.com/example/explanation-strategy",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    content_tid = f"uireportsexplanation{uuid.uuid4().hex[:8]}"
    source_url = "https://example.com/posts/reports-explanation"
    booking_uuid = f"BOOK_UI_REPORTS_EXPLANATION_{uuid.uuid4().hex[:8]}"
    stripe_invoice_id = f"in_ui_reports_explanation_{uuid.uuid4().hex[:8]}"
    stripe_event_id = f"evt_ui_reports_explanation_{uuid.uuid4().hex[:8]}"

    _insert_content(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        source_url=source_url,
        tid=content_tid,
    )
    booking_id = _insert_booking(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        tid=content_tid,
        calendly_booking_uuid=booking_uuid,
        booked_at=datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc),
    )
    invoice_id = _insert_invoice(
        creator_id=creator["creator_id"],
        booking_id=booking_id,
        tid=content_tid,
        stripe_account_id="acct_ui_reports_explanation",
        stripe_invoice_id=stripe_invoice_id,
        amount_cents=19500,
        paid_at=datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc),
    )
    _insert_matched_payment_event(
        creator_id=creator["creator_id"],
        booking_id=booking_id,
        tid=content_tid,
        invoice_id=invoice_id,
        stripe_account_id="acct_ui_reports_explanation",
        stripe_event_id=stripe_event_id,
        stripe_invoice_id=stripe_invoice_id,
        paid_at=datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get(
            f"/app/reports/explanations/paid/{content_tid}",
            params={"start_date": "2026-03-08", "end_date": "2026-03-08"},
            headers=HTML_ACCEPT_HEADERS,
        )

    assert response.status_code == 200
    assert "Why this revenue counted" in response.text
    assert "the same tracking ID moved through your stored content, booking, invoice, and payment record chain" in response.text
    assert source_url in response.text
    assert content_tid in response.text
    assert booking_uuid in response.text
    assert stripe_invoice_id in response.text
    assert stripe_event_id in response.text
    assert "Applied" in response.text
    assert (
        'href="/app/reports?start_date=2026-03-08&amp;end_date=2026-03-08"'
        in response.text
    )


def test_reports_paid_explanation_page_returns_404_for_other_creators_row():
    creator_a = _insert_creator_user(
        email=f"ui_reports_explanation_a_{uuid.uuid4().hex}@example.com",
        name="Reports Explanation Creator A",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_reports_explanation_a",
    )
    creator_b = _insert_creator_user(
        email=f"ui_reports_explanation_b_{uuid.uuid4().hex}@example.com",
        name="Reports Explanation Creator B",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_reports_explanation_b",
    )
    access_token_b = _access_token(
        user_id=creator_b["user_id"],
        creator_id=creator_b["creator_id"],
        email=creator_b["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=creator_a["creator_id"],
        name="Explanation Isolation Strategy",
        calendly_url="https://calendly.com/example/explanation-isolation",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    content_tid = f"uireportsexplanationhidden{uuid.uuid4().hex[:8]}"
    stripe_invoice_id = f"in_ui_reports_explanation_hidden_{uuid.uuid4().hex[:8]}"

    _insert_content(
        creator_id=creator_a["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/reports-explanation-hidden",
        tid=content_tid,
    )
    booking_id = _insert_booking(
        creator_id=creator_a["creator_id"],
        booking_link_id=booking_link_id,
        tid=content_tid,
        calendly_booking_uuid=f"BOOK_UI_REPORTS_EXPLANATION_HIDDEN_{uuid.uuid4().hex[:8]}",
        booked_at=datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc),
    )
    invoice_id = _insert_invoice(
        creator_id=creator_a["creator_id"],
        booking_id=booking_id,
        tid=content_tid,
        stripe_account_id="acct_ui_reports_explanation_a",
        stripe_invoice_id=stripe_invoice_id,
        amount_cents=19500,
        paid_at=datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc),
    )
    _insert_matched_payment_event(
        creator_id=creator_a["creator_id"],
        booking_id=booking_id,
        tid=content_tid,
        invoice_id=invoice_id,
        stripe_account_id="acct_ui_reports_explanation_a",
        stripe_event_id=f"evt_ui_reports_explanation_hidden_{uuid.uuid4().hex[:8]}",
        stripe_invoice_id=stripe_invoice_id,
        paid_at=datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token_b)
        response = client.get(
            f"/app/reports/explanations/paid/{content_tid}",
            headers=HTML_ACCEPT_HEADERS,
        )

    assert response.status_code == 404
    assert response.json() == {"detail": "report explanation not found"}


def test_reports_unattributed_explanation_page_renders_current_backlog_reason():
    creator = _insert_creator_user(
        email=f"ui_reports_unattributed_{uuid.uuid4().hex}@example.com",
        name="Reports Unattributed Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_reports_unattributed",
    )
    access_token = _access_token(
        user_id=creator["user_id"],
        creator_id=creator["creator_id"],
        email=creator["email"],
        expires_delta=timedelta(hours=24),
    )
    _insert_unmatched_payment_event(
        creator_id=creator["creator_id"],
        stripe_account_id="acct_ui_reports_unattributed",
        stripe_event_id=f"evt_ui_reports_unattributed_{uuid.uuid4().hex[:8]}",
        stripe_invoice_id=f"in_ui_reports_unattributed_{uuid.uuid4().hex[:8]}",
        reason=UNATTRIBUTED_REASON_MISSING_TID,
        paid_at=datetime(2026, 3, 8, 11, 0, tzinfo=timezone.utc),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get(
            "/app/reports/explanations/unattributed",
            params={"start_date": "2026-03-08", "end_date": "2026-03-08"},
            headers=HTML_ACCEPT_HEADERS,
        )

    assert response.status_code == 200
    assert "Why some payments are not counted yet" in response.text
    assert "counts and reasons only" in response.text
    assert "Missing tracking ID" in response.text
    assert "stays out of paid totals until that creator-scoped link can be repaired" in response.text
    assert (
        'href="/app/reports?start_date=2026-03-08&amp;end_date=2026-03-08"'
        in response.text
    )


def test_reports_csv_export_uses_same_filtered_creator_scoped_dataset():
    creator_a = _insert_creator_user(
        email=f"ui_reports_export_a_{uuid.uuid4().hex}@example.com",
        name="Reports Export Creator A",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_reports_export_a",
    )
    creator_b = _insert_creator_user(
        email=f"ui_reports_export_b_{uuid.uuid4().hex}@example.com",
        name="Reports Export Creator B",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_reports_export_b",
    )
    access_token = _access_token(
        user_id=creator_a["user_id"],
        creator_id=creator_a["creator_id"],
        email=creator_a["email"],
        expires_delta=timedelta(hours=24),
    )

    creator_a_booking_link_id = _insert_booking_link(
        creator_id=creator_a["creator_id"],
        name="Export Creator A Strategy",
        calendly_url="https://calendly.com/example/export-creator-a-strategy",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    old_tid = f"uireportsexportold{uuid.uuid4().hex[:8]}"
    current_tid = f"uireportsexportcurrent{uuid.uuid4().hex[:8]}"
    _insert_content(
        creator_id=creator_a["creator_id"],
        booking_link_id=creator_a_booking_link_id,
        source_url="https://example.com/posts/reports-export-old",
        tid=old_tid,
    )
    _insert_content(
        creator_id=creator_a["creator_id"],
        booking_link_id=creator_a_booking_link_id,
        source_url="https://example.com/posts/reports-export-current",
        tid=current_tid,
    )
    old_booking_id = _insert_booking(
        creator_id=creator_a["creator_id"],
        booking_link_id=creator_a_booking_link_id,
        tid=old_tid,
        calendly_booking_uuid=f"BOOK_UI_REPORTS_EXPORT_OLD_{uuid.uuid4().hex[:8]}",
        booked_at=datetime(2026, 3, 7, 12, 0, tzinfo=timezone.utc),
    )
    current_booking_id = _insert_booking(
        creator_id=creator_a["creator_id"],
        booking_link_id=creator_a_booking_link_id,
        tid=current_tid,
        calendly_booking_uuid=f"BOOK_UI_REPORTS_EXPORT_CURRENT_{uuid.uuid4().hex[:8]}",
        booked_at=datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc),
    )
    _insert_invoice(
        creator_id=creator_a["creator_id"],
        booking_id=old_booking_id,
        tid=old_tid,
        stripe_account_id="acct_ui_reports_export_a",
        stripe_invoice_id=f"in_ui_reports_export_old_{uuid.uuid4().hex[:8]}",
        amount_cents=5000,
        paid_at=datetime(2026, 3, 7, 13, 0, tzinfo=timezone.utc),
    )
    _insert_invoice(
        creator_id=creator_a["creator_id"],
        booking_id=current_booking_id,
        tid=current_tid,
        stripe_account_id="acct_ui_reports_export_a",
        stripe_invoice_id=f"in_ui_reports_export_current_{uuid.uuid4().hex[:8]}",
        amount_cents=19500,
        paid_at=datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc),
    )

    creator_b_booking_link_id = _insert_booking_link(
        creator_id=creator_b["creator_id"],
        name="Export Creator B Strategy",
        calendly_url="https://calendly.com/example/export-creator-b-strategy",
        billing_amount_cents=88000,
        billing_currency="USD",
    )
    hidden_tid = f"uireportsexporthidden{uuid.uuid4().hex[:8]}"
    _insert_content(
        creator_id=creator_b["creator_id"],
        booking_link_id=creator_b_booking_link_id,
        source_url="https://example.com/posts/reports-export-hidden",
        tid=hidden_tid,
    )
    hidden_booking_id = _insert_booking(
        creator_id=creator_b["creator_id"],
        booking_link_id=creator_b_booking_link_id,
        tid=hidden_tid,
        calendly_booking_uuid=f"BOOK_UI_REPORTS_EXPORT_HIDDEN_{uuid.uuid4().hex[:8]}",
        booked_at=datetime(2026, 3, 8, 14, 0, tzinfo=timezone.utc),
    )
    _insert_invoice(
        creator_id=creator_b["creator_id"],
        booking_id=hidden_booking_id,
        tid=hidden_tid,
        stripe_account_id="acct_ui_reports_export_b",
        stripe_invoice_id=f"in_ui_reports_export_hidden_{uuid.uuid4().hex[:8]}",
        amount_cents=88000,
        paid_at=datetime(2026, 3, 8, 15, 0, tzinfo=timezone.utc),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get(
            "/app/reports/export.csv",
            params={"start_date": "2026-03-08", "end_date": "2026-03-08"},
            headers=HTML_ACCEPT_HEADERS,
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="reports-summary-2026-03-08-to-2026-03-08.csv"'
    )
    assert response.text.startswith(
        "content_id,booking_link_id,tid,source_url,paid_revenue_cents,paid_invoice_count,paid_booking_count,first_paid_at,last_paid_at\n"
    )
    assert current_tid in response.text
    assert "https://example.com/posts/reports-export-current" in response.text
    assert "19500" in response.text
    assert "2026-03-08T09:00:00Z" in response.text
    assert old_tid not in response.text
    assert "https://example.com/posts/reports-export-old" not in response.text
    assert hidden_tid not in response.text
    assert "https://example.com/posts/reports-export-hidden" not in response.text


def test_reports_page_without_tracked_content_explains_prerequisite():
    inserted = _insert_creator_user(
        email=f"ui_reports_empty_{uuid.uuid4().hex}@example.com",
        name="No Reporting Content Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_reports_empty",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get("/app/reports", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Create tracked content first" in response.text
    assert 'href="/app/content"' in response.text
    assert "No paid results yet" in response.text


def test_reports_page_with_tracked_content_but_no_paid_invoices_shows_empty_paid_state():
    inserted = _insert_creator_user(
        email=f"ui_reports_no_paid_{uuid.uuid4().hex}@example.com",
        name="No Paid Reports Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_reports_no_paid",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="No Paid Strategy",
        calendly_url="https://calendly.com/example/no-paid-strategy",
    )
    _insert_content(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/reports-no-paid",
        tid=f"uireportsnopaid{uuid.uuid4().hex[:8]}",
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get("/app/reports", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "No paid results yet" in response.text
    assert "nothing is counted here until a matching invoice is marked paid" in response.text
    assert 'href="/app/content"' in response.text


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
