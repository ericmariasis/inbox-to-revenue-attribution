from dataclasses import dataclass

import pytest

from app.services.billing_provider import BillingAccountReadiness
from app.services.stripe_provider import (
    StripeApiRequestError,
    StripeOAuthProvider,
    StripeProviderError,
)


@dataclass(frozen=True)
class _TransportCall:
    method: str
    url: str
    api_key: str
    params: dict[str, object] | None
    stripe_account_id: str | None
    idempotency_key: str | None


class _StubStripeTransport:
    def __init__(
        self,
        *,
        responses: list[dict[str, object]] | None = None,
        errors: list[StripeApiRequestError] | None = None,
    ):
        self._responses = list(responses or [])
        self._errors = list(errors or [])
        self.calls: list[_TransportCall] = []

    def request(
        self,
        *,
        method: str,
        url: str,
        api_key: str,
        params: dict[str, object] | None = None,
        stripe_account_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        self.calls.append(
            _TransportCall(
                method=method,
                url=url,
                api_key=api_key,
                params=params,
                stripe_account_id=stripe_account_id,
                idempotency_key=idempotency_key,
            )
        )
        if self._errors:
            raise self._errors.pop(0)
        if not self._responses:
            raise AssertionError("No stubbed Stripe transport response remaining")
        return self._responses.pop(0)


def _provider(*, transport: _StubStripeTransport, booking_email: str | None = None) -> StripeOAuthProvider:
    return StripeOAuthProvider(
        authorize_url="https://connect.stripe.com/oauth/authorize",
        client_id="ca_story57_test",
        redirect_uri="https://creatortrust.test/stripe/connect/callback",
        client_secret="sk_test_story57",
        transport=transport,
        booking_email_lookup=lambda booking_provider, booking_uuid: (
            booking_email
            if booking_provider in {None, "calendly"} and booking_uuid == "BOOK_story57"
            else None
        ),
    )


def test_exchange_connect_callback_posts_oauth_code_and_returns_connected_account_id():
    transport = _StubStripeTransport(
        responses=[{"stripe_user_id": "acct_story57_connected"}]
    )
    provider = _provider(transport=transport)

    account_id = provider.exchange_connect_callback(
        code="auth_code_story57",
        state="state_story57",
    )

    assert account_id == "acct_story57_connected"
    assert transport.calls == [
        _TransportCall(
            method="POST",
            url="https://connect.stripe.com/oauth/token",
            api_key="sk_test_story57",
            params={
                "code": "auth_code_story57",
                "grant_type": "authorization_code",
            },
            stripe_account_id=None,
            idempotency_key=None,
        )
    ]


def test_exchange_connect_callback_maps_transport_failures_to_provider_error():
    transport = _StubStripeTransport(
        errors=[
            StripeApiRequestError(
                operation="POST",
                http_status=400,
                error_code="invalid_grant",
                error_type="invalid_request_error",
            )
        ]
    )
    provider = _provider(transport=transport)

    with pytest.raises(StripeProviderError) as exc_info:
        provider.exchange_connect_callback(
            code="auth_code_story57_invalid",
            state="state_story57_invalid",
        )

    assert str(exc_info.value) == "stripe callback exchange failed"
    assert exc_info.value.operation == "stripe_connect_callback_exchange"
    assert exc_info.value.http_status == 400
    assert exc_info.value.error_code == "invalid_grant"
    assert exc_info.value.error_type == "invalid_request_error"


def test_get_account_readiness_reads_charges_enabled_from_stripe_account():
    transport = _StubStripeTransport(
        responses=[{"id": "acct_story57_ready", "charges_enabled": True}]
    )
    provider = _provider(transport=transport)

    readiness = provider.get_account_readiness(stripe_account_id="acct_story57_ready")

    assert readiness.charges_enabled is True
    assert transport.calls == [
        _TransportCall(
            method="GET",
            url="https://api.stripe.com/v1/accounts/acct_story57_ready",
            api_key="sk_test_story57",
            params=None,
            stripe_account_id=None,
            idempotency_key=None,
        )
    ]


def test_get_billing_account_readiness_maps_charges_enabled_to_can_create_invoices():
    transport = _StubStripeTransport(
        responses=[{"id": "acct_story57_ready", "charges_enabled": True}]
    )
    provider = _provider(transport=transport)

    readiness = provider.get_billing_account_readiness(provider_account_id="acct_story57_ready")

    assert readiness == BillingAccountReadiness(can_create_invoices=True)
    assert provider.billing_provider_name == "stripe"


def test_create_invoice_creates_customer_invoice_item_invoice_and_finalizes():
    transport = _StubStripeTransport(
        responses=[
            {"id": "cus_story57"},
            {"id": "ii_story57"},
            {"id": "in_story57"},
            {"id": "in_story57", "status": "open"},
        ]
    )
    provider = _provider(
        transport=transport,
        booking_email="story57-booked@example.com",
    )

    result = provider.create_invoice(
        stripe_account_id="acct_story57_billable",
        amount_cents=19500,
        currency="USD",
        metadata={
            "creator_id": "creator_story57",
            "booking_uuid": "BOOK_story57",
            "tid": "story57_tid",
        },
        idempotency_key="billing:create:BOOK_story57",
    )

    assert result.stripe_invoice_id == "in_story57"
    assert transport.calls == [
        _TransportCall(
            method="POST",
            url="https://api.stripe.com/v1/customers",
            api_key="sk_test_story57",
            params={
                "metadata": {
                    "creator_id": "creator_story57",
                    "booking_uuid": "BOOK_story57",
                    "tid": "story57_tid",
                },
                "email": "story57-booked@example.com",
            },
            stripe_account_id="acct_story57_billable",
            idempotency_key="billing:create:BOOK_story57:customer",
        ),
        _TransportCall(
            method="POST",
            url="https://api.stripe.com/v1/invoiceitems",
            api_key="sk_test_story57",
            params={
                "amount": 19500,
                "currency": "usd",
                "customer": "cus_story57",
                "description": "Creator Compass booking BOOK_story57 (story57_tid)",
                "metadata": {
                    "creator_id": "creator_story57",
                    "booking_uuid": "BOOK_story57",
                    "tid": "story57_tid",
                },
            },
            stripe_account_id="acct_story57_billable",
            idempotency_key="billing:create:BOOK_story57:invoice_item",
        ),
        _TransportCall(
            method="POST",
            url="https://api.stripe.com/v1/invoices",
            api_key="sk_test_story57",
            params={
                "auto_advance": False,
                "collection_method": "send_invoice",
                "customer": "cus_story57",
                "days_until_due": 30,
                "metadata": {
                    "creator_id": "creator_story57",
                    "booking_uuid": "BOOK_story57",
                    "tid": "story57_tid",
                },
                "pending_invoice_items_behavior": "include",
            },
            stripe_account_id="acct_story57_billable",
            idempotency_key="billing:create:BOOK_story57:invoice",
        ),
        _TransportCall(
            method="POST",
            url="https://api.stripe.com/v1/invoices/in_story57/finalize",
            api_key="sk_test_story57",
            params=None,
            stripe_account_id="acct_story57_billable",
            idempotency_key="billing:create:BOOK_story57:finalize",
        ),
    ]


def test_create_billing_invoice_returns_provider_neutral_invoice_identity():
    transport = _StubStripeTransport(
        responses=[
            {"id": "cus_story57"},
            {"id": "ii_story57"},
            {"id": "in_story57"},
            {"id": "in_story57", "status": "open"},
        ]
    )
    provider = _provider(
        transport=transport,
        booking_email="story57-booked@example.com",
    )

    result = provider.create_billing_invoice(
        provider_account_id="acct_story57_billable",
        amount_cents=19500,
        currency="USD",
        metadata={
            "creator_id": "creator_story57",
            "booking_uuid": "BOOK_story57",
            "tid": "story57_tid",
        },
        idempotency_key="billing:create:BOOK_story57",
    )

    assert result.provider_account_id == "acct_story57_billable"
    assert result.provider_invoice_id == "in_story57"
    assert result.invoice_status == "open"


def test_create_invoice_accepts_paid_status_when_stripe_finalizes_zero_amount_invoice():
    transport = _StubStripeTransport(
        responses=[
            {"id": "cus_story57"},
            {"id": "ii_story57"},
            {"id": "in_story57_paid"},
            {"id": "in_story57_paid", "status": "paid"},
        ]
    )
    provider = _provider(transport=transport)

    result = provider.create_invoice(
        stripe_account_id="acct_story57_billable",
        amount_cents=19500,
        currency="USD",
        metadata={
            "creator_id": "creator_story57",
            "booking_uuid": "BOOK_story57",
            "tid": "story57_tid",
        },
        idempotency_key="billing:create:BOOK_story57",
    )

    assert result.stripe_invoice_id == "in_story57_paid"
    assert result.status == "paid"


def test_create_invoice_prefers_provider_aware_booking_identity_for_customer_lookup():
    transport = _StubStripeTransport(
        responses=[
            {"id": "cus_story57_fullscope"},
            {"id": "ii_story57_fullscope"},
            {"id": "in_story57_fullscope"},
            {"id": "in_story57_fullscope", "status": "open"},
        ]
    )
    lookups: list[tuple[str | None, str]] = []
    provider = StripeOAuthProvider(
        authorize_url="https://connect.stripe.com/oauth/authorize",
        client_id="ca_story57_test",
        redirect_uri="https://creatortrust.test/stripe/connect/callback",
        client_secret="sk_test_story57",
        transport=transport,
        booking_email_lookup=lambda booking_provider, booking_uuid: (
            lookups.append((booking_provider, booking_uuid)) or "story57-fullscope@example.com"
        ),
    )

    result = provider.create_invoice(
        stripe_account_id="acct_story57_billable",
        amount_cents=19500,
        currency="USD",
        metadata={
            "creator_id": "creator_story57",
            "booking_provider": "fullscope",
            "provider_booking_id": "APT_story57",
            "booking_uuid": "APT_story57",
            "tid": "story57_tid",
        },
        idempotency_key="billing:create:fullscope:APT_story57",
    )

    assert result.stripe_invoice_id == "in_story57_fullscope"
    assert lookups == [("fullscope", "APT_story57")]
    assert transport.calls[0] == _TransportCall(
        method="POST",
        url="https://api.stripe.com/v1/customers",
        api_key="sk_test_story57",
        params={
            "metadata": {
                "creator_id": "creator_story57",
                "booking_provider": "fullscope",
                "provider_booking_id": "APT_story57",
                "booking_uuid": "APT_story57",
                "tid": "story57_tid",
            },
            "email": "story57-fullscope@example.com",
        },
        stripe_account_id="acct_story57_billable",
        idempotency_key="billing:create:fullscope:APT_story57:customer",
    )
    assert transport.calls[1].params == {
        "amount": 19500,
        "currency": "usd",
        "customer": "cus_story57_fullscope",
        "description": "Creator Compass booking APT_story57 (story57_tid)",
        "metadata": {
            "creator_id": "creator_story57",
            "booking_provider": "fullscope",
            "provider_booking_id": "APT_story57",
            "booking_uuid": "APT_story57",
            "tid": "story57_tid",
        },
    }


def test_void_invoice_retrieves_and_voids_open_invoice():
    transport = _StubStripeTransport(
        responses=[
            {"id": "in_story57_void", "status": "open"},
            {"id": "in_story57_void", "status": "void"},
        ]
    )
    provider = _provider(transport=transport)

    provider.void_invoice(
        stripe_account_id="acct_story57_billable",
        stripe_invoice_id="in_story57_void",
    )

    assert transport.calls == [
        _TransportCall(
            method="GET",
            url="https://api.stripe.com/v1/invoices/in_story57_void",
            api_key="sk_test_story57",
            params=None,
            stripe_account_id="acct_story57_billable",
            idempotency_key=None,
        ),
        _TransportCall(
            method="POST",
            url="https://api.stripe.com/v1/invoices/in_story57_void/void",
            api_key="sk_test_story57",
            params=None,
            stripe_account_id="acct_story57_billable",
            idempotency_key="billing:void:in_story57_void",
        ),
    ]


def test_stop_billing_invoice_returns_provider_neutral_stop_result():
    transport = _StubStripeTransport(
        responses=[
            {"id": "in_story57_void", "status": "open"},
            {"id": "in_story57_void", "status": "void"},
        ]
    )
    provider = _provider(transport=transport)

    result = provider.stop_billing_invoice(
        provider_account_id="acct_story57_billable",
        provider_invoice_id="in_story57_void",
    )

    assert result.provider_account_id == "acct_story57_billable"
    assert result.provider_invoice_id == "in_story57_void"
    assert result.invoice_status == "void"


def test_void_invoice_is_idempotent_when_invoice_is_already_void():
    transport = _StubStripeTransport(
        responses=[{"id": "in_story57_void", "status": "void"}]
    )
    provider = _provider(transport=transport)

    provider.void_invoice(
        stripe_account_id="acct_story57_billable",
        stripe_invoice_id="in_story57_void",
    )

    assert transport.calls == [
        _TransportCall(
            method="GET",
            url="https://api.stripe.com/v1/invoices/in_story57_void",
            api_key="sk_test_story57",
            params=None,
            stripe_account_id="acct_story57_billable",
            idempotency_key=None,
        )
    ]
