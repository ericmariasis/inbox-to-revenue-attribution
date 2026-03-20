from dataclasses import dataclass

import pytest

from app.services.billing_provider import BillingAccountReadiness
from app.services.paypal_provider import (
    PayPalApiRequestError,
    PayPalProviderError,
    PayPalSandboxSellerOnboardingProvider,
)


@dataclass(frozen=True)
class _TransportCall:
    method: str
    url: str
    headers: dict[str, str] | None
    json_body: dict[str, object] | None
    form_body: dict[str, str] | None


class _StubPayPalTransport:
    def __init__(
        self,
        *,
        responses: list[dict[str, object]] | None = None,
        errors: list[PayPalApiRequestError] | None = None,
    ):
        self._responses = list(responses or [])
        self._errors = list(errors or [])
        self.calls: list[_TransportCall] = []

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        json_body: dict[str, object] | None = None,
        form_body: dict[str, str] | None = None,
    ) -> dict[str, object]:
        self.calls.append(
            _TransportCall(
                method=method,
                url=url,
                headers=headers,
                json_body=json_body,
                form_body=form_body,
            )
        )
        if self._errors:
            raise self._errors.pop(0)
        if not self._responses:
            raise AssertionError("No stubbed PayPal transport response remaining")
        return self._responses.pop(0)


def _provider(
    *,
    transport: _StubPayPalTransport,
    booking_email: str | None = None,
) -> PayPalSandboxSellerOnboardingProvider:
    return PayPalSandboxSellerOnboardingProvider(
        client_id="client_story_pp7",
        client_secret="secret_story_pp7",
        partner_id="partner_story_pp7",
        api_base_url="https://api-m.sandbox.paypal.com",
        partner_attribution_id="pp_partner_story_pp7",
        transport=transport,
        booking_email_lookup=lambda booking_provider, booking_uuid: (
            booking_email
            if booking_provider in {None, "calendly"} and booking_uuid == "BOOK_story_pp7"
            else None
        ),
    )


def test_get_billing_account_readiness_maps_paypal_seller_status_to_can_create_invoices():
    transport = _StubPayPalTransport(
        responses=[
            {"access_token": "oauth_story_pp7"},
            {
                "merchant_id": "merchant_story_pp7",
                "tracking_id": "tracking_story_pp7",
                "payments_receivable": True,
                "primary_email_confirmed": True,
            },
        ]
    )
    provider = _provider(transport=transport)

    readiness = provider.get_billing_account_readiness(
        provider_account_id="merchant_story_pp7"
    )

    assert readiness == BillingAccountReadiness(can_create_invoices=True)
    assert provider.billing_provider_name == "paypal"
    assert transport.calls == [
        _TransportCall(
            method="POST",
            url="https://api-m.sandbox.paypal.com/v1/oauth2/token",
            headers={
                "Authorization": "Basic Y2xpZW50X3N0b3J5X3BwNzpzZWNyZXRfc3RvcnlfcHA3",
            },
            json_body=None,
            form_body={"grant_type": "client_credentials"},
        ),
        _TransportCall(
            method="GET",
            url=(
                "https://api-m.sandbox.paypal.com/v1/customer/partners/partner_story_pp7/"
                "merchant-integrations/merchant_story_pp7"
            ),
            headers={
                "Authorization": "Bearer oauth_story_pp7",
                "PayPal-Partner-Attribution-Id": "pp_partner_story_pp7",
            },
            json_body=None,
            form_body=None,
        ),
    ]


def test_create_billing_invoice_creates_sends_and_reads_paypal_invoice():
    transport = _StubPayPalTransport(
        responses=[
            {"access_token": "oauth_story_pp7"},
            {
                "href": "https://api-m.sandbox.paypal.com/v2/invoicing/invoices/INV2-story-pp7"
            },
            {
                "href": "https://www.sandbox.paypal.com/invoice/p/#INV2-story-pp7"
            },
            {
                "id": "INV2-story-pp7",
                "status": "UNPAID",
            },
        ]
    )
    provider = _provider(
        transport=transport,
        booking_email="booked@example.com",
    )

    result = provider.create_billing_invoice(
        provider_account_id="merchant_story_pp7",
        amount_cents=19500,
        currency="USD",
        metadata={
            "creator_id": "creator_story_pp7",
            "booking_provider": "calendly",
            "provider_booking_id": "BOOK_story_pp7",
            "booking_uuid": "BOOK_story_pp7",
            "tid": "story_pp7_tid",
        },
        idempotency_key="billing:create:calendly:BOOK_story_pp7",
    )

    assert result.provider_account_id == "merchant_story_pp7"
    assert result.provider_invoice_id == "INV2-story-pp7"
    assert result.invoice_status == "open"
    assert transport.calls[0] == _TransportCall(
        method="POST",
        url="https://api-m.sandbox.paypal.com/v1/oauth2/token",
        headers={
            "Authorization": "Basic Y2xpZW50X3N0b3J5X3BwNzpzZWNyZXRfc3RvcnlfcHA3",
        },
        json_body=None,
        form_body={"grant_type": "client_credentials"},
    )
    assert transport.calls[1].method == "POST"
    assert transport.calls[1].url == "https://api-m.sandbox.paypal.com/v2/invoicing/invoices"
    assert transport.calls[1].headers == {
        "Authorization": "Bearer oauth_story_pp7",
        "PayPal-Partner-Attribution-Id": "pp_partner_story_pp7",
        "PayPal-Auth-Assertion": "eyJhbGciOiJub25lIn0.eyJpc3MiOiJjbGllbnRfc3RvcnlfcHA3IiwicGF5ZXJfaWQiOiJtZXJjaGFudF9zdG9yeV9wcDcifQ.",
        "PayPal-Request-Id": "billing:create:calendly:BOOK_story_pp7",
    }
    assert transport.calls[1].json_body is not None
    assert transport.calls[1].json_body["detail"]["reference"] == "BOOK_story_pp7"
    assert transport.calls[1].json_body["detail"]["currency_code"] == "USD"
    assert transport.calls[1].json_body["primary_recipients"] == [
        {"billing_info": {"email_address": "booked@example.com"}}
    ]
    assert transport.calls[1].json_body["items"] == [
        {
            "name": "Creator Compass booking BOOK_story_pp7",
            "description": "Creator Compass booking BOOK_story_pp7 (story_pp7_tid)",
            "quantity": "1",
            "unit_amount": {"currency_code": "USD", "value": "195.00"},
        }
    ]
    assert transport.calls[2] == _TransportCall(
        method="POST",
        url="https://api-m.sandbox.paypal.com/v2/invoicing/invoices/INV2-story-pp7/send",
        headers={
            "Authorization": "Bearer oauth_story_pp7",
            "PayPal-Partner-Attribution-Id": "pp_partner_story_pp7",
            "PayPal-Auth-Assertion": "eyJhbGciOiJub25lIn0.eyJpc3MiOiJjbGllbnRfc3RvcnlfcHA3IiwicGF5ZXJfaWQiOiJtZXJjaGFudF9zdG9yeV9wcDcifQ.",
            "PayPal-Request-Id": "billing:create:calendly:BOOK_story_pp7:send",
        },
        json_body={
            "send_to_invoicer": False,
            "send_to_recipient": True,
            "note": "Creator Compass invoice",
        },
        form_body=None,
    )
    assert transport.calls[3] == _TransportCall(
        method="GET",
        url="https://api-m.sandbox.paypal.com/v2/invoicing/invoices/INV2-story-pp7",
        headers={
            "Authorization": "Bearer oauth_story_pp7",
            "PayPal-Partner-Attribution-Id": "pp_partner_story_pp7",
            "PayPal-Auth-Assertion": "eyJhbGciOiJub25lIn0.eyJpc3MiOiJjbGllbnRfc3RvcnlfcHA3IiwicGF5ZXJfaWQiOiJtZXJjaGFudF9zdG9yeV9wcDcifQ.",
        },
        json_body=None,
        form_body=None,
    )


def test_create_billing_invoice_raises_when_send_does_not_produce_payable_status():
    transport = _StubPayPalTransport(
        responses=[
            {"access_token": "oauth_story_pp7"},
            {
                "href": "https://api-m.sandbox.paypal.com/v2/invoicing/invoices/INV2-story-pp7"
            },
            {
                "href": "https://www.sandbox.paypal.com/invoice/p/#INV2-story-pp7"
            },
            {
                "id": "INV2-story-pp7",
                "status": "DRAFT",
            },
        ]
    )
    provider = _provider(
        transport=transport,
        booking_email="booked@example.com",
    )

    with pytest.raises(PayPalProviderError) as exc_info:
        provider.create_billing_invoice(
            provider_account_id="merchant_story_pp7",
            amount_cents=19500,
            currency="USD",
            metadata={
                "creator_id": "creator_story_pp7",
                "booking_provider": "calendly",
                "provider_booking_id": "BOOK_story_pp7",
                "booking_uuid": "BOOK_story_pp7",
                "tid": "story_pp7_tid",
            },
            idempotency_key="billing:create:calendly:BOOK_story_pp7",
        )

    assert str(exc_info.value) == "paypal invoice send failed"
    assert exc_info.value.operation == "paypal_invoice_send"
    assert exc_info.value.error_code == "invoice_status_draft"


def test_stop_billing_invoice_cancels_paypal_invoice_and_returns_void():
    transport = _StubPayPalTransport(
        responses=[
            {"access_token": "oauth_story_pp7"},
            {"id": "INV2-story-pp7", "status": "UNPAID"},
            {},
            {"id": "INV2-story-pp7", "status": "CANCELLED"},
        ]
    )
    provider = _provider(transport=transport)

    result = provider.stop_billing_invoice(
        provider_account_id="merchant_story_pp7",
        provider_invoice_id="INV2-story-pp7",
    )

    assert result.provider_account_id == "merchant_story_pp7"
    assert result.provider_invoice_id == "INV2-story-pp7"
    assert result.invoice_status == "void"
    assert transport.calls[1] == _TransportCall(
        method="GET",
        url="https://api-m.sandbox.paypal.com/v2/invoicing/invoices/INV2-story-pp7",
        headers={
            "Authorization": "Bearer oauth_story_pp7",
            "PayPal-Partner-Attribution-Id": "pp_partner_story_pp7",
            "PayPal-Auth-Assertion": "eyJhbGciOiJub25lIn0.eyJpc3MiOiJjbGllbnRfc3RvcnlfcHA3IiwicGF5ZXJfaWQiOiJtZXJjaGFudF9zdG9yeV9wcDcifQ.",
        },
        json_body=None,
        form_body=None,
    )
    assert transport.calls[2] == _TransportCall(
        method="POST",
        url="https://api-m.sandbox.paypal.com/v2/invoicing/invoices/INV2-story-pp7/cancel",
        headers={
            "Authorization": "Bearer oauth_story_pp7",
            "PayPal-Partner-Attribution-Id": "pp_partner_story_pp7",
            "PayPal-Auth-Assertion": "eyJhbGciOiJub25lIn0.eyJpc3MiOiJjbGllbnRfc3RvcnlfcHA3IiwicGF5ZXJfaWQiOiJtZXJjaGFudF9zdG9yeV9wcDcifQ.",
            "PayPal-Request-Id": "billing:void:INV2-story-pp7",
        },
        json_body={
            "send_to_invoicer": False,
            "send_to_recipient": False,
            "note": "Creator Compass booking canceled",
        },
        form_body=None,
    )


def test_stop_billing_invoice_is_idempotent_when_invoice_is_already_cancelled():
    transport = _StubPayPalTransport(
        responses=[
            {"access_token": "oauth_story_pp7"},
            {"id": "INV2-story-pp7", "status": "CANCELLED"},
        ]
    )
    provider = _provider(transport=transport)

    result = provider.stop_billing_invoice(
        provider_account_id="merchant_story_pp7",
        provider_invoice_id="INV2-story-pp7",
    )

    assert result.provider_account_id == "merchant_story_pp7"
    assert result.provider_invoice_id == "INV2-story-pp7"
    assert result.invoice_status == "void"
    assert len(transport.calls) == 2
