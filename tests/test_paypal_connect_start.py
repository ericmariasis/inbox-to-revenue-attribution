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
from app.services.paypal_connect import decode_paypal_connect_state
from app.services.paypal_provider import PayPalConnectOnboardingResult


def _engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def _insert_creator_user(
    *,
    email: str,
    name: str = "PayPal Creator",
    stripe_connect_status: str = "pending",
    stripe_account_id: str | None = None,
    stripe_connected_at: datetime | None = None,
    billing_provider: str = "stripe",
    billing_connect_status: str | None = None,
    billing_account_id: str | None = None,
    billing_connected_at: datetime | None = None,
):
    creator_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    resolved_billing_connect_status = billing_connect_status or stripe_connect_status
    resolved_billing_account_id = billing_account_id if billing_account_id is not None else stripe_account_id
    resolved_billing_connected_at = (
        billing_connected_at if billing_connected_at is not None else stripe_connected_at
    )

    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO creators ("
                "id, name, billing_provider, billing_connect_status, billing_account_id, billing_connected_at, "
                "stripe_connect_status, stripe_account_id, stripe_connected_at"
                ") VALUES ("
                ":id, :name, :billing_provider, :billing_connect_status, :billing_account_id, :billing_connected_at, "
                ":stripe_connect_status, :stripe_account_id, :stripe_connected_at"
                ")"
            ),
            {
                "id": creator_id,
                "name": name,
                "billing_provider": billing_provider,
                "billing_connect_status": resolved_billing_connect_status,
                "billing_account_id": resolved_billing_account_id,
                "billing_connected_at": resolved_billing_connected_at,
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


def _switch_attempt_rows():
    with _engine().connect() as conn:
        return conn.execute(
            text(
                "SELECT id::text AS id, creator_id::text AS creator_id, "
                "source_billing_provider, target_billing_provider, "
                "target_billing_connect_status, target_billing_account_id, "
                "target_billing_provider_correlation_id "
                "FROM billing_provider_switch_attempts ORDER BY created_at"
            )
        ).mappings().all()


def _insert_open_invoice_for_creator(*, creator_id: str) -> None:
    booking_link_id = str(uuid.uuid4())
    content_id = str(uuid.uuid4())
    booking_id = str(uuid.uuid4())
    issued_at = datetime.now(timezone.utc).replace(microsecond=0)

    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO booking_links "
                "(id, creator_id, name, provider, destination_url, calendly_url, billing_amount_cents, billing_currency) "
                "VALUES "
                "(:id, :creator_id, :name, :provider, :destination_url, :calendly_url, :billing_amount_cents, :billing_currency)"
            ),
            {
                "id": booking_link_id,
                "creator_id": creator_id,
                "name": "Switch Start Link",
                "provider": "calendly",
                "destination_url": "https://calendly.com/example/switch-start",
                "calendly_url": "https://calendly.com/example/switch-start",
                "billing_amount_cents": 15000,
                "billing_currency": "USD",
            },
        )
        conn.execute(
            text(
                "INSERT INTO content (id, creator_id, booking_link_id, source_url, tid, created_at, updated_at) "
                "VALUES (:id, :creator_id, :booking_link_id, :source_url, :tid, :created_at, :updated_at)"
            ),
            {
                "id": content_id,
                "creator_id": creator_id,
                "booking_link_id": booking_link_id,
                "source_url": "https://example.com/posts/switch-start",
                "tid": "paypal_switch_start_tid",
                "created_at": issued_at,
                "updated_at": issued_at,
            },
        )
        conn.execute(
            text(
                "INSERT INTO bookings "
                "(id, creator_id, booking_link_id, tid, calendly_booking_uuid, email, status, attribution_status, unattributed_reason, booked_at, canceled_at) "
                "VALUES "
                "(:id, :creator_id, :booking_link_id, :tid, :calendly_booking_uuid, :email, :status, :attribution_status, :unattributed_reason, :booked_at, :canceled_at)"
            ),
            {
                "id": booking_id,
                "creator_id": creator_id,
                "booking_link_id": booking_link_id,
                "tid": "paypal_switch_start_tid",
                "calendly_booking_uuid": "BOOK_paypal_switch_start",
                "email": "switch-start@example.com",
                "status": "created",
                "attribution_status": "attributed",
                "unattributed_reason": None,
                "booked_at": issued_at,
                "canceled_at": None,
            },
        )
        conn.execute(
            text(
                "INSERT INTO invoices "
                "(id, creator_id, booking_id, tid, payment_provider, provider_account_id, provider_invoice_id, stripe_account_id, stripe_invoice_id, amount_cents, currency, status, issued_at, paid_at, voided_at) "
                "VALUES "
                "(:id, :creator_id, :booking_id, :tid, :payment_provider, :provider_account_id, :provider_invoice_id, :stripe_account_id, :stripe_invoice_id, :amount_cents, :currency, :status, :issued_at, :paid_at, :voided_at)"
            ),
            {
                "id": str(uuid.uuid4()),
                "creator_id": creator_id,
                "booking_id": booking_id,
                "tid": "paypal_switch_start_tid",
                "payment_provider": "stripe",
                "provider_account_id": "acct_open_switch_start",
                "provider_invoice_id": "in_open_switch_start",
                "stripe_account_id": "acct_open_switch_start",
                "stripe_invoice_id": "in_open_switch_start",
                "amount_cents": 15000,
                "currency": "USD",
                "status": "open",
                "issued_at": issued_at,
                "paid_at": None,
                "voided_at": None,
            },
        )


@contextmanager
def _override_app_state(name, value):
    had_attr = hasattr(app.state, name)
    previous_value = getattr(app.state, name, None)
    marker_name = f"_{name}_overridden"
    had_marker = hasattr(app.state, marker_name)
    previous_marker = getattr(app.state, marker_name, None)
    setattr(app.state, name, value)
    setattr(app.state, marker_name, True)
    try:
        yield
    finally:
        if had_attr:
            setattr(app.state, name, previous_value)
        else:
            delattr(app.state, name)
        if had_marker:
            setattr(app.state, marker_name, previous_marker)
        else:
            delattr(app.state, marker_name)


class _StubPayPalProvider:
    def __init__(self):
        self.start_calls: list[dict[str, str]] = []

    def create_connect_onboarding(
        self,
        *,
        tracking_id: str,
        return_url: str,
    ) -> PayPalConnectOnboardingResult:
        self.start_calls.append(
            {
                "tracking_id": tracking_id,
                "return_url": return_url,
            }
        )
        return PayPalConnectOnboardingResult(
            onboarding_url=(
                "https://www.sandbox.paypal.com/bizsignup/partner/entry"
                f"?tracking_id={tracking_id}"
            ),
            tracking_id=tracking_id,
        )

    def get_verified_seller_status(self, *, tracking_id: str):
        raise AssertionError(f"unexpected seller lookup tracking_id={tracking_id}")


def _live_paypal_operator_only_settings(*emails: str):
    settings = getattr(app.state, "settings", get_settings())
    return settings.model_copy(
        update={
            "paypal_environment": "live",
            "paypal_live_creator_access": "operator_only",
            "operator_email_allowlist": ",".join(emails),
        }
    )


def test_paypal_connect_start_returns_provider_url_and_app_issued_state():
    inserted = _insert_creator_user(email=f"paypal_start_{uuid.uuid4().hex}@example.com")
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    provider = _StubPayPalProvider()

    with _override_app_state("paypal_provider", provider):
        with TestClient(app) as client:
            response = client.post(
                "/paypal/connect/start",
                headers={"Authorization": f"Bearer {access_token}"},
            )

    assert response.status_code == 200
    assert response.headers.get("X-Request-Id")
    payload = response.json()
    decoded_state = decode_paypal_connect_state(payload["state"])
    callback_query = parse_qs(urlparse(provider.start_calls[0]["return_url"]).query)

    assert payload["state"]
    assert len(payload["state"]) > 20
    assert payload["onboarding_url"].startswith("https://www.sandbox.paypal.com/bizsignup/partner/entry")
    assert decoded_state["sub"] == inserted["creator_id"]
    assert decoded_state["tracking_id"].startswith("ccp-paypal-")
    assert callback_query["state"] == [payload["state"]]
    assert provider.start_calls == [
        {
            "tracking_id": decoded_state["tracking_id"],
            "return_url": provider.start_calls[0]["return_url"],
        }
    ]

    with _engine().connect() as conn:
        creator_row = conn.execute(
            text(
                "SELECT billing_provider, billing_connect_status, billing_account_id, "
                "billing_provider_correlation_id "
                "FROM creators WHERE id = :creator_id"
            ),
            {"creator_id": inserted["creator_id"]},
        ).mappings().one()

    assert creator_row["billing_provider"] == "stripe"
    assert creator_row["billing_connect_status"] == "pending"
    assert creator_row["billing_account_id"] is None
    assert creator_row["billing_provider_correlation_id"] is None
    assert _switch_attempt_rows() == []


def test_paypal_connect_start_requires_auth():
    with TestClient(app) as client:
        response = client.post("/paypal/connect/start")

    assert response.status_code == 401
    assert response.json() == {"detail": "not authenticated"}


def test_paypal_connect_start_hides_live_path_for_non_operator_creator():
    inserted = _insert_creator_user(email=f"paypal_start_live_{uuid.uuid4().hex}@example.com")
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    provider = _StubPayPalProvider()
    settings = _live_paypal_operator_only_settings("ops@creatortrust.co")

    with _override_app_state("settings", settings):
        with _override_app_state("paypal_provider", provider):
            with TestClient(app) as client:
                response = client.post(
                    "/paypal/connect/start",
                    headers={"Authorization": f"Bearer {access_token}"},
                )

    assert response.status_code == 404
    assert response.json() == {"detail": "paypal connect not found"}
    assert provider.start_calls == []


def test_paypal_connect_start_keeps_live_path_for_allowlisted_operator():
    inserted = _insert_creator_user(email=f"paypal_start_live_ops_{uuid.uuid4().hex}@example.com")
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    provider = _StubPayPalProvider()
    settings = _live_paypal_operator_only_settings(inserted["email"])

    with _override_app_state("settings", settings):
        with _override_app_state("paypal_provider", provider):
            with TestClient(app) as client:
                response = client.post(
                    "/paypal/connect/start",
                    headers={"Authorization": f"Bearer {access_token}"},
                )

    assert response.status_code == 200
    assert response.json()["onboarding_url"].startswith(
        "https://www.sandbox.paypal.com/bizsignup/partner/entry"
    )
    assert len(provider.start_calls) == 1


def test_paypal_connect_start_creates_pending_switch_attempt_for_clean_connected_stripe_creator():
    connected_at = datetime.now(timezone.utc)
    inserted = _insert_creator_user(
        email=f"paypal_switch_{uuid.uuid4().hex}@example.com",
        stripe_connect_status="connected",
        stripe_account_id="acct_existing_connected",
        stripe_connected_at=connected_at,
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    provider = _StubPayPalProvider()

    with _override_app_state("paypal_provider", provider):
        with TestClient(app) as client:
            first_response = client.post(
                "/paypal/connect/start",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            second_response = client.post(
                "/paypal/connect/start",
                headers={"Authorization": f"Bearer {access_token}"},
            )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    first_state = decode_paypal_connect_state(first_response.json()["state"])
    second_state = decode_paypal_connect_state(second_response.json()["state"])
    switch_attempt_rows = _switch_attempt_rows()
    assert len(switch_attempt_rows) == 1
    assert first_state["switch_attempt_id"] == second_state["switch_attempt_id"]
    assert first_state["tracking_id"] == second_state["tracking_id"]
    with _engine().connect() as conn:
        creator_row = conn.execute(
            text(
                "SELECT billing_provider, billing_connect_status, billing_account_id "
                "FROM creators WHERE id = :creator_id"
            ),
            {"creator_id": inserted["creator_id"]},
        ).mappings().one()
    assert creator_row["billing_provider"] == "stripe"
    assert creator_row["billing_connect_status"] == "connected"
    assert creator_row["billing_account_id"] == "acct_existing_connected"
    assert switch_attempt_rows[0]["creator_id"] == inserted["creator_id"]
    assert switch_attempt_rows[0]["source_billing_provider"] == "stripe"
    assert switch_attempt_rows[0]["target_billing_provider"] == "paypal"
    assert switch_attempt_rows[0]["target_billing_connect_status"] == "pending"
    assert switch_attempt_rows[0]["target_billing_account_id"] is None
    assert (
        switch_attempt_rows[0]["target_billing_provider_correlation_id"]
        == first_state["tracking_id"]
    )
    assert len(provider.start_calls) == 2
    assert provider.start_calls[0]["tracking_id"] == first_state["tracking_id"]
    assert provider.start_calls[1]["tracking_id"] == first_state["tracking_id"]


def test_paypal_connect_start_blocks_connected_switch_when_current_provider_is_not_clean():
    connected_at = datetime.now(timezone.utc)
    inserted = _insert_creator_user(
        email=f"paypal_switch_blocked_{uuid.uuid4().hex}@example.com",
        stripe_connect_status="connected",
        stripe_account_id="acct_switch_blocked",
        stripe_connected_at=connected_at,
    )
    _insert_open_invoice_for_creator(creator_id=inserted["creator_id"])
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    provider = _StubPayPalProvider()

    with _override_app_state("paypal_provider", provider):
        with TestClient(app) as client:
            response = client.post(
                "/paypal/connect/start",
                headers={"Authorization": f"Bearer {access_token}"},
            )

    assert response.status_code == 409
    assert response.json() == {"detail": "switch_not_clean"}
    assert provider.start_calls == []
    assert _switch_attempt_rows() == []
