import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import create_engine, text

from app.core.config import get_settings
from app.main import app
from app.services.paypal_connect import decode_paypal_connect_state
from app.services.paypal_provider import (
    PayPalConnectOnboardingResult,
    PayPalProviderError,
    PayPalSellerStatus,
)

HTML_ACCEPT_HEADERS = {"Accept": "text/html,application/xhtml+xml"}


def _engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def _insert_creator_user(
    *,
    email: str,
    name: str = "PayPal Callback Creator",
    stripe_connect_status: str = "pending",
    stripe_account_id: str | None = None,
    stripe_connected_at: datetime | None = None,
    billing_provider: str = "stripe",
    billing_connect_status: str | None = None,
    billing_account_id: str | None = None,
    billing_connected_at: datetime | None = None,
    billing_provider_correlation_id: str | None = None,
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
                "billing_provider_correlation_id, stripe_connect_status, stripe_account_id, stripe_connected_at"
                ") VALUES ("
                ":id, :name, :billing_provider, :billing_connect_status, :billing_account_id, :billing_connected_at, "
                ":billing_provider_correlation_id, :stripe_connect_status, :stripe_account_id, :stripe_connected_at"
                ")"
            ),
            {
                "id": creator_id,
                "name": name,
                "billing_provider": billing_provider,
                "billing_connect_status": resolved_billing_connect_status,
                "billing_account_id": resolved_billing_account_id,
                "billing_connected_at": resolved_billing_connected_at,
                "billing_provider_correlation_id": billing_provider_correlation_id,
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
                "SELECT creator_id::text AS creator_id, source_billing_provider, target_billing_provider, "
                "target_billing_connect_status, target_billing_account_id, "
                "target_billing_provider_correlation_id, target_billing_connected_at "
                "FROM billing_provider_switch_attempts ORDER BY created_at"
            )
        ).mappings().all()


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


def _paypal_operator_only_settings(*emails: str, environment: str = "sandbox"):
    settings = get_settings()
    return settings.model_copy(
        update={
            "app_env": "test",
            "paypal_environment": environment,
            "paypal_creator_access": "operator_only",
            "operator_email_allowlist": ",".join(emails),
        }
    )


class _StubPayPalProvider:
    def __init__(
        self,
        *,
        seller_status: PayPalSellerStatus | None = None,
        status_error: PayPalProviderError | None = None,
    ):
        self.seller_status = seller_status
        self.status_error = status_error
        self.start_calls: list[dict[str, str]] = []
        self.status_calls: list[str] = []

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
            onboarding_url="https://www.sandbox.paypal.com/bizsignup/partner/entry?token=pp6",
            tracking_id=tracking_id,
        )

    def get_verified_seller_status(
        self,
        *,
        tracking_id: str,
    ) -> PayPalSellerStatus:
        self.status_calls.append(tracking_id)
        if self.status_error is not None:
            raise self.status_error
        if self.seller_status is None:
            raise AssertionError("seller_status must be configured")
        return self.seller_status


def test_paypal_connect_callback_persists_connected_fields_and_returns_browser_success_page():
    inserted = _insert_creator_user(email=f"paypal_callback_{uuid.uuid4().hex}@example.com")
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    provider = _StubPayPalProvider(
        seller_status=PayPalSellerStatus(
            merchant_id="merchant_pp6_connected",
            tracking_id="placeholder",
            payments_receivable=True,
            primary_email_confirmed=True,
        )
    )
    settings = _paypal_operator_only_settings(inserted["email"], environment="sandbox")

    with _override_app_state("settings", settings):
        with _override_app_state("paypal_provider", provider):
            with TestClient(app) as client:
                start_response = client.post(
                    "/paypal/connect/start",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                state = start_response.json()["state"]
                tracking_id = decode_paypal_connect_state(state)["tracking_id"]
                provider.seller_status = PayPalSellerStatus(
                    merchant_id="merchant_pp6_connected",
                    tracking_id=tracking_id,
                    payments_receivable=True,
                    primary_email_confirmed=True,
                )
                callback_response = client.get(
                    "/paypal/connect/callback",
                    params={
                        "state": state,
                        "merchantId": tracking_id,
                        "merchantIdInPayPal": "merchant_pp6_connected",
                        "permissionsGranted": "true",
                        "consentStatus": "true",
                    },
                    headers=HTML_ACCEPT_HEADERS,
                )
                me_response = client.get("/me", headers={"Authorization": f"Bearer {access_token}"})

    assert start_response.status_code == 200
    assert callback_response.status_code == 200
    assert callback_response.headers.get("X-Request-Id")
    assert "PayPal setup completed" in callback_response.text
    assert "merchant_pp6_connected" in callback_response.text
    assert provider.status_calls == [tracking_id]

    with _engine().connect() as conn:
        creator_row = conn.execute(
            text(
                "SELECT billing_provider, billing_connect_status, billing_account_id, "
                "billing_provider_correlation_id, billing_connected_at, stripe_connect_status, stripe_account_id "
                "FROM creators WHERE id = :creator_id"
            ),
            {"creator_id": inserted["creator_id"]},
        ).mappings().one()

    assert creator_row["billing_provider"] == "paypal"
    assert creator_row["billing_connect_status"] == "connected"
    assert creator_row["billing_account_id"] == "merchant_pp6_connected"
    assert creator_row["billing_provider_correlation_id"] == tracking_id
    assert creator_row["billing_connected_at"] is not None
    assert creator_row["stripe_connect_status"] == "pending"
    assert creator_row["stripe_account_id"] is None

    assert me_response.status_code == 200
    payload = me_response.json()
    assert payload["billing_provider"] == "paypal"
    assert payload["billing_connect_status"] == "connected"
    assert payload["billing_account_id"] == "merchant_pp6_connected"
    assert payload["billing_connected_at"] is not None
    assert payload["stripe_connect_status"] == "pending"
    assert payload["stripe_account_id"] is None


def test_paypal_connect_callback_stores_pending_switch_attempt_without_mutating_active_provider():
    connected_at = datetime.now(timezone.utc).replace(microsecond=0)
    inserted = _insert_creator_user(
        email=f"paypal_switch_callback_{uuid.uuid4().hex}@example.com",
        stripe_connect_status="connected",
        stripe_account_id="acct_pp11b_source",
        stripe_connected_at=connected_at,
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    provider = _StubPayPalProvider()
    settings = _paypal_operator_only_settings(inserted["email"], environment="sandbox")

    with _override_app_state("settings", settings):
        with _override_app_state("paypal_provider", provider):
            with TestClient(app) as client:
                start_response = client.post(
                    "/paypal/connect/start",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                state = start_response.json()["state"]
                tracking_id = decode_paypal_connect_state(state)["tracking_id"]
                provider.seller_status = PayPalSellerStatus(
                    merchant_id="merchant_pp11b_target",
                    tracking_id=tracking_id,
                    payments_receivable=True,
                    primary_email_confirmed=True,
                )
                callback_response = client.get(
                    "/paypal/connect/callback",
                    params={
                        "state": state,
                        "merchantId": tracking_id,
                        "merchantIdInPayPal": "merchant_pp11b_target",
                        "permissionsGranted": "true",
                        "consentStatus": "true",
                    },
                    headers=HTML_ACCEPT_HEADERS,
                )
                me_response = client.get("/me", headers={"Authorization": f"Bearer {access_token}"})

    assert start_response.status_code == 200
    assert callback_response.status_code == 200
    assert "PayPal switch setup completed" in callback_response.text
    assert "merchant_pp11b_target" in callback_response.text
    assert provider.status_calls == [tracking_id]

    with _engine().connect() as conn:
        creator_row = conn.execute(
            text(
                "SELECT billing_provider, billing_connect_status, billing_account_id, "
                "billing_provider_correlation_id, stripe_connect_status, stripe_account_id "
                "FROM creators WHERE id = :creator_id"
            ),
            {"creator_id": inserted["creator_id"]},
        ).mappings().one()

    switch_attempt_rows = _switch_attempt_rows()
    assert creator_row["billing_provider"] == "stripe"
    assert creator_row["billing_connect_status"] == "connected"
    assert creator_row["billing_account_id"] == "acct_pp11b_source"
    assert creator_row["billing_provider_correlation_id"] is None
    assert creator_row["stripe_connect_status"] == "connected"
    assert creator_row["stripe_account_id"] == "acct_pp11b_source"
    assert len(switch_attempt_rows) == 1
    assert switch_attempt_rows[0]["creator_id"] == inserted["creator_id"]
    assert switch_attempt_rows[0]["source_billing_provider"] == "stripe"
    assert switch_attempt_rows[0]["target_billing_provider"] == "paypal"
    assert switch_attempt_rows[0]["target_billing_connect_status"] == "connected"
    assert switch_attempt_rows[0]["target_billing_account_id"] == "merchant_pp11b_target"
    assert switch_attempt_rows[0]["target_billing_provider_correlation_id"] == tracking_id
    assert switch_attempt_rows[0]["target_billing_connected_at"] is not None
    assert me_response.status_code == 200
    assert me_response.json()["billing_provider"] == "stripe"
    assert me_response.json()["billing_account_id"] == "acct_pp11b_source"


def test_paypal_connect_callback_rejects_invalid_state_without_mutating_creator():
    inserted = _insert_creator_user(email=f"paypal_invalid_{uuid.uuid4().hex}@example.com")
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    provider = _StubPayPalProvider(
        seller_status=PayPalSellerStatus(
            merchant_id="merchant_should_not_persist",
            tracking_id="tracking_should_not_persist",
            payments_receivable=True,
            primary_email_confirmed=True,
        )
    )
    settings = _paypal_operator_only_settings(inserted["email"], environment="sandbox")

    with _override_app_state("settings", settings):
        with _override_app_state("paypal_provider", provider):
            with TestClient(app) as client:
                start_response = client.post(
                    "/paypal/connect/start",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                invalid_state = f"{start_response.json()['state']}tampered"
                callback_response = client.get(
                    "/paypal/connect/callback",
                    params={
                        "state": invalid_state,
                        "merchantId": "tracking_invalid",
                        "merchantIdInPayPal": "merchant_invalid",
                    },
                )
                me_response = client.get("/me", headers={"Authorization": f"Bearer {access_token}"})

    assert start_response.status_code == 200
    assert callback_response.status_code == 400
    assert callback_response.json() == {"detail": "invalid paypal connect state"}
    assert provider.status_calls == []
    assert me_response.status_code == 200
    assert me_response.json()["billing_provider"] == "stripe"
    assert me_response.json()["billing_connect_status"] == "pending"
    assert me_response.json()["billing_account_id"] is None


def test_paypal_connect_callback_rejects_tracking_id_mismatch_without_mutation():
    inserted = _insert_creator_user(email=f"paypal_tracking_{uuid.uuid4().hex}@example.com")
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    provider = _StubPayPalProvider(
        seller_status=PayPalSellerStatus(
            merchant_id="merchant_pp6_mismatch",
            tracking_id="placeholder",
            payments_receivable=True,
            primary_email_confirmed=True,
        )
    )
    settings = _paypal_operator_only_settings(inserted["email"], environment="sandbox")

    with _override_app_state("settings", settings):
        with _override_app_state("paypal_provider", provider):
            with TestClient(app) as client:
                start_response = client.post(
                    "/paypal/connect/start",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                state = start_response.json()["state"]
                tracking_id = decode_paypal_connect_state(state)["tracking_id"]
                provider.seller_status = PayPalSellerStatus(
                    merchant_id="merchant_pp6_mismatch",
                    tracking_id=tracking_id,
                    payments_receivable=True,
                    primary_email_confirmed=True,
                )
                callback_response = client.get(
                    "/paypal/connect/callback",
                    params={
                        "state": state,
                        "merchantId": "wrong-tracking-id",
                        "merchantIdInPayPal": "merchant_pp6_mismatch",
                    },
                )

    assert start_response.status_code == 200
    assert callback_response.status_code == 400
    assert callback_response.json() == {"detail": "invalid paypal connect callback"}
    assert provider.status_calls == [tracking_id]


def test_paypal_connect_callback_rejects_provider_verification_failure():
    inserted = _insert_creator_user(email=f"paypal_failure_{uuid.uuid4().hex}@example.com")
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    provider = _StubPayPalProvider(
        status_error=PayPalProviderError(
            "paypal merchant lookup failed",
            operation="paypal_merchant_status",
            http_status=500,
            error_code="INTERNAL_SERVER_ERROR",
        )
    )
    settings = _paypal_operator_only_settings(inserted["email"], environment="sandbox")

    with _override_app_state("settings", settings):
        with _override_app_state("paypal_provider", provider):
            with TestClient(app) as client:
                start_response = client.post(
                    "/paypal/connect/start",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                state = start_response.json()["state"]
                tracking_id = decode_paypal_connect_state(state)["tracking_id"]
                callback_response = client.get(
                    "/paypal/connect/callback",
                    params={
                        "state": state,
                        "merchantId": tracking_id,
                        "merchantIdInPayPal": "merchant_pp6_failure",
                    },
                )

    assert start_response.status_code == 200
    assert callback_response.status_code == 400
    assert callback_response.json() == {"detail": "invalid paypal connect callback"}
    assert provider.status_calls == [tracking_id]


def test_paypal_connect_callback_browser_response_shows_payments_receivable_failure_copy():
    inserted = _insert_creator_user(email=f"paypal_browser_receivable_{uuid.uuid4().hex}@example.com")
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    provider = _StubPayPalProvider(
        seller_status=PayPalSellerStatus(
            merchant_id="merchant_pp6_receivable",
            tracking_id="placeholder",
            payments_receivable=False,
            primary_email_confirmed=True,
        )
    )
    settings = _paypal_operator_only_settings(inserted["email"], environment="sandbox")

    with _override_app_state("settings", settings):
        with _override_app_state("paypal_provider", provider):
            with TestClient(app) as client:
                start_response = client.post(
                    "/paypal/connect/start",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                state = start_response.json()["state"]
                tracking_id = decode_paypal_connect_state(state)["tracking_id"]
                provider.seller_status = PayPalSellerStatus(
                    merchant_id="merchant_pp6_receivable",
                    tracking_id=tracking_id,
                    payments_receivable=False,
                    primary_email_confirmed=True,
                )
                callback_response = client.get(
                    "/paypal/connect/callback",
                    params={
                        "state": state,
                        "merchantId": tracking_id,
                        "merchantIdInPayPal": "merchant_pp6_receivable",
                        "permissionsGranted": "true",
                        "consentStatus": "true",
                    },
                    headers=HTML_ACCEPT_HEADERS,
                )

    assert start_response.status_code == 200
    assert callback_response.status_code == 400
    assert (
        "Attention: You currently cannot receive payments due to restriction on your PayPal account."
        in callback_response.text
    )
    assert "https://www.paypal.com for more information." in callback_response.text


def test_paypal_connect_callback_browser_response_shows_primary_email_failure_copy():
    inserted = _insert_creator_user(email=f"paypal_browser_email_{uuid.uuid4().hex}@example.com")
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    provider = _StubPayPalProvider(
        seller_status=PayPalSellerStatus(
            merchant_id="merchant_pp6_email",
            tracking_id="placeholder",
            payments_receivable=True,
            primary_email_confirmed=False,
        )
    )
    settings = _paypal_operator_only_settings(inserted["email"], environment="sandbox")

    with _override_app_state("settings", settings):
        with _override_app_state("paypal_provider", provider):
            with TestClient(app) as client:
                start_response = client.post(
                    "/paypal/connect/start",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                state = start_response.json()["state"]
                tracking_id = decode_paypal_connect_state(state)["tracking_id"]
                provider.seller_status = PayPalSellerStatus(
                    merchant_id="merchant_pp6_email",
                    tracking_id=tracking_id,
                    payments_receivable=True,
                    primary_email_confirmed=False,
                )
                callback_response = client.get(
                    "/paypal/connect/callback",
                    params={
                        "state": state,
                        "merchantId": tracking_id,
                        "merchantIdInPayPal": "merchant_pp6_email",
                        "permissionsGranted": "true",
                        "consentStatus": "true",
                    },
                    headers=HTML_ACCEPT_HEADERS,
                )

    assert start_response.status_code == 200
    assert callback_response.status_code == 400
    assert (
        "Attention: Please confirm your email address on https://www.paypal.com/businessprofile/settings in order to receive payments!"
        in callback_response.text
    )
    assert "You currently cannot receive payments." in callback_response.text


def test_paypal_connect_callback_browser_response_shows_permissions_failure_copy():
    inserted = _insert_creator_user(email=f"paypal_browser_permissions_{uuid.uuid4().hex}@example.com")
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    provider = _StubPayPalProvider(
        seller_status=PayPalSellerStatus(
            merchant_id="merchant_permissions_denied",
            tracking_id="placeholder",
            payments_receivable=True,
            primary_email_confirmed=True,
        )
    )
    settings = _paypal_operator_only_settings(inserted["email"], environment="sandbox")

    with _override_app_state("settings", settings):
        with _override_app_state("paypal_provider", provider):
            with TestClient(app) as client:
                start_response = client.post(
                    "/paypal/connect/start",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                state = start_response.json()["state"]
                callback_response = client.get(
                    "/paypal/connect/callback",
                    params={
                        "state": state,
                        "merchantId": "tracking_permissions_denied",
                        "merchantIdInPayPal": "merchant_permissions_denied",
                        "permissionsGranted": "false",
                        "consentStatus": "false",
                    },
                    headers=HTML_ACCEPT_HEADERS,
                )

    assert start_response.status_code == 200
    assert callback_response.status_code == 400
    assert "The required PayPal permissions were not granted to this platform." in callback_response.text
