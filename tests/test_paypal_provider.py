import json
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from app.core.config import Settings
from app.services.billing_provider import (
    BILLING_ACCOUNT_READINESS_ISSUE_CONFIRM_PAYPAL_PRIMARY_EMAIL,
    BILLING_ACCOUNT_READINESS_ISSUE_ENABLE_PAYPAL_PAYMENTS_RECEIVABLE,
    BillingAccountReadiness,
)
from app.services.paypal_provider import (
    FilePayPalApiTraceRecorder,
    PayPalApiResponse,
    PayPalApiRequestError,
    PayPalApiTraceRecord,
    PayPalInvoicePaidSnapshot,
    PAYPAL_SELLER_ONBOARDING_FEATURES,
    PayPalProviderError,
    PayPalSellerOnboardingProvider,
    PayPalSandboxSellerOnboardingProvider,
    build_default_paypal_provider,
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
        responses: list[object] | None = None,
        errors: list[PayPalApiRequestError] | None = None,
        outcomes: list[object] | None = None,
    ):
        self._responses = list(responses or [])
        self._errors = list(errors or [])
        self._outcomes = list(outcomes or [])
        self.calls: list[_TransportCall] = []

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        json_body: dict[str, object] | None = None,
        form_body: dict[str, str] | None = None,
    ) -> object:
        self.calls.append(
            _TransportCall(
                method=method,
                url=url,
                headers=headers,
                json_body=json_body,
                form_body=form_body,
            )
        )
        if self._outcomes:
            next_outcome = self._outcomes.pop(0)
            if isinstance(next_outcome, Exception):
                raise next_outcome
            return next_outcome
        if self._errors:
            raise self._errors.pop(0)
        if not self._responses:
            raise AssertionError("No stubbed PayPal transport response remaining")
        return self._responses.pop(0)


def _provider(
    *,
    transport: _StubPayPalTransport,
    booking_email: str | None = None,
    request_trace_recorder=None,
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
        request_trace_recorder=request_trace_recorder,
    )


def test_file_paypal_api_trace_recorder_appends_json_lines(tmp_path):
    trace_path = tmp_path / "paypal-api-trace.jsonl"
    recorder = FilePayPalApiTraceRecorder(path=str(trace_path))

    recorder(
        PayPalApiTraceRecord(
            recorded_at="2026-04-08T12:00:00+00:00",
            operation="paypal_invoice_get",
            method="GET",
            url="https://api-m.sandbox.paypal.com/v2/invoicing/invoices/INV2-story-pp7",
            http_status=200,
            debug_id="debug-story-pp7",
            error_code=None,
            summary={"invoice_id": "INV2-story-pp7", "status": "PAID"},
        )
    )

    written_records = [
        json.loads(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert written_records == [
        {
            "debug_id": "debug-story-pp7",
            "error_code": None,
            "http_status": 200,
            "method": "GET",
            "operation": "paypal_invoice_get",
            "recorded_at": "2026-04-08T12:00:00+00:00",
            "summary": {"invoice_id": "INV2-story-pp7", "status": "PAID"},
            "url": "https://api-m.sandbox.paypal.com/v2/invoicing/invoices/INV2-story-pp7",
        }
    ]


def test_build_default_paypal_provider_uses_selected_live_settings():
    settings = Settings.model_validate(
        {
            "app_env": "local",
            "paypal_environment": "live",
            "paypal_live_client_id": "live-client",
            "paypal_live_client_secret": "live-secret",
            "paypal_live_partner_id": "live-partner",
            "paypal_live_api_base_url": "https://api-m.paypal.com",
            "paypal_sandbox_client_id": "sandbox-client",
            "paypal_sandbox_client_secret": "sandbox-secret",
            "paypal_sandbox_partner_id": "sandbox-partner",
            "paypal_sandbox_api_base_url": "https://api-m.sandbox.paypal.com",
        }
    )

    provider = build_default_paypal_provider(settings=settings)

    assert isinstance(provider, PayPalSellerOnboardingProvider)
    assert provider._environment == "live"
    assert provider._client_id == "live-client"
    assert provider._client_secret == "live-secret"
    assert provider._partner_id == "live-partner"
    assert provider._api_base_url == "https://api-m.paypal.com"


def test_create_connect_onboarding_requests_paypal_invoicing_features():
    transport = _StubPayPalTransport(
        responses=[
            {"access_token": "oauth_story_pp7"},
            {
                "links": [
                    {"rel": "self", "href": "https://api-m.sandbox.paypal.com/v2/customer/partner-referrals/REF-story-pp7"},
                    {"rel": "action_url", "href": "https://www.sandbox.paypal.com/bizsignup/partner/entry?referralToken=story-pp7"},
                ]
            },
        ]
    )
    provider = _provider(transport=transport)

    result = provider.create_connect_onboarding(
        tracking_id="tracking_story_pp7",
        return_url="https://example.ngrok-free.dev/paypal/connect/callback",
    )

    assert result.onboarding_url == "https://www.sandbox.paypal.com/bizsignup/partner/entry?referralToken=story-pp7"
    assert result.tracking_id == "tracking_story_pp7"
    assert transport.calls[1].method == "POST"
    assert transport.calls[1].url == "https://api-m.sandbox.paypal.com/v2/customer/partner-referrals"
    assert transport.calls[1].json_body is not None
    third_party_details = transport.calls[1].json_body["operations"][0]["api_integration_preference"]["rest_api_integration"]["third_party_details"]
    assert third_party_details["features"] == list(PAYPAL_SELLER_ONBOARDING_FEATURES)
    assert transport.calls[1].json_body["products"] == ["EXPRESS_CHECKOUT"]
    assert transport.calls[1].json_body["partner_config_override"]["return_url"] == "https://example.ngrok-free.dev/paypal/connect/callback"


def test_create_billing_invoice_records_sanitized_paypal_api_trace():
    transport = _StubPayPalTransport(
        responses=[
            PayPalApiResponse(
                payload={
                    "access_token": "oauth_story_pp7",
                    "token_type": "Bearer",
                    "scope": "openid",
                    "expires_in": 32400,
                },
                http_status=200,
                debug_id="debug-oauth-story-pp7",
            ),
            PayPalApiResponse(
                payload={
                    "href": "https://api-m.sandbox.paypal.com/v2/invoicing/invoices/INV2-story-pp7"
                },
                http_status=201,
                debug_id="debug-create-story-pp7",
            ),
            PayPalApiResponse(
                payload={
                    "href": "https://www.sandbox.paypal.com/invoice/p/#INV2-story-pp7"
                },
                http_status=202,
                debug_id="debug-send-story-pp7",
            ),
            PayPalApiResponse(
                payload={
                    "id": "INV2-story-pp7",
                    "status": "UNPAID",
                },
                http_status=200,
                debug_id="debug-get-story-pp7",
            ),
        ]
    )
    trace_records: list[PayPalApiTraceRecord] = []
    provider = _provider(
        transport=transport,
        booking_email="booked@example.com",
        request_trace_recorder=trace_records.append,
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

    assert result.provider_invoice_id == "INV2-story-pp7"
    assert [record.operation for record in trace_records] == [
        "paypal_oauth_token",
        "paypal_invoice_create",
        "paypal_invoice_send",
        "paypal_invoice_get",
    ]
    assert trace_records[0].debug_id == "debug-oauth-story-pp7"
    assert trace_records[0].summary == {
        "expires_in": 32400,
        "keys": ["access_token", "expires_in", "scope", "token_type"],
        "scope": "openid",
        "token_type": "Bearer",
    }
    assert trace_records[1].summary["invoice_id"] == "INV2-story-pp7"
    assert trace_records[1].debug_id == "debug-create-story-pp7"
    assert trace_records[2].summary["invoice_id"] == "INV2-story-pp7"
    assert trace_records[2].debug_id == "debug-send-story-pp7"
    assert trace_records[3].summary["invoice_id"] == "INV2-story-pp7"
    assert trace_records[3].summary["status"] == "UNPAID"
    assert trace_records[3].debug_id == "debug-get-story-pp7"


def test_verify_webhook_event_records_paypal_debug_id_on_error_before_raising():
    transport = _StubPayPalTransport(
        outcomes=[
            PayPalApiResponse(
                payload={"access_token": "oauth_story_pp8"},
                http_status=200,
                debug_id="debug-oauth-story-pp8",
            ),
            PayPalApiRequestError(
                operation="POST",
                http_status=422,
                error_code="INVALID_REQUEST",
                debug_id="debug-webhook-story-pp8",
            ),
        ]
    )
    trace_records: list[PayPalApiTraceRecord] = []
    provider = _provider(
        transport=transport,
        request_trace_recorder=trace_records.append,
    )

    with pytest.raises(PayPalProviderError) as exc_info:
        provider.verify_webhook_event(
            webhook_id="WH_story_pp8",
            auth_algo="SHA256withRSA",
            cert_url="https://api.sandbox.paypal.com/v1/notifications/certs/CERT-story-pp8",
            transmission_id="transmission_story_pp8",
            transmission_sig="sig_story_pp8",
            transmission_time="2026-03-20T04:52:23Z",
            webhook_event={
                "id": "WH-PP8-STORY",
                "event_type": "INVOICING.INVOICE.PAID",
            },
        )

    assert str(exc_info.value) == "paypal webhook verification failed"
    assert exc_info.value.operation == "paypal_webhook_verify"
    assert exc_info.value.http_status == 422
    assert exc_info.value.error_code == "INVALID_REQUEST"
    assert exc_info.value.debug_id == "debug-webhook-story-pp8"
    assert [record.operation for record in trace_records] == [
        "paypal_oauth_token",
        "paypal_webhook_verify",
    ]
    assert trace_records[1].debug_id == "debug-webhook-story-pp8"
    assert trace_records[1].http_status == 422
    assert trace_records[1].error_code == "INVALID_REQUEST"
    assert trace_records[1].summary == {"keys": []}


def test_create_connect_onboarding_records_partner_referral_error_payload_in_trace():
    transport = _StubPayPalTransport(
        outcomes=[
            PayPalApiResponse(
                payload={"access_token": "oauth_story_pp14c"},
                http_status=200,
                debug_id="debug-oauth-story-pp14c",
            ),
            PayPalApiRequestError(
                operation="POST",
                http_status=422,
                error_code="UNPROCESSABLE_ENTITY",
                debug_id="debug-referral-story-pp14c",
                payload={
                    "name": "UNPROCESSABLE_ENTITY",
                    "message": "Request is not well-formed, syntactically incorrect, or violates schema.",
                    "details": [
                        {
                            "issue": "INVALID_PARAMETER_VALUE",
                            "field": "/operations/0/api_integration_preference/rest_api_integration/third_party_details/features/0",
                            "description": "Value is not supported for this partner configuration.",
                        }
                    ],
                },
            ),
        ]
    )
    trace_records: list[PayPalApiTraceRecord] = []
    provider = _provider(
        transport=transport,
        request_trace_recorder=trace_records.append,
    )

    with pytest.raises(PayPalProviderError) as exc_info:
        provider.create_connect_onboarding(
            tracking_id="tracking_story_pp14c",
            return_url="https://example.ngrok-free.dev/paypal/connect/callback",
        )

    assert str(exc_info.value) == "paypal onboarding start failed"
    assert exc_info.value.operation == "paypal_partner_referral_create"
    assert exc_info.value.http_status == 422
    assert exc_info.value.error_code == "UNPROCESSABLE_ENTITY"
    assert exc_info.value.debug_id == "debug-referral-story-pp14c"
    assert [record.operation for record in trace_records] == [
        "paypal_oauth_token",
        "paypal_partner_referral_create",
    ]
    assert trace_records[1].debug_id == "debug-referral-story-pp14c"
    assert trace_records[1].http_status == 422
    assert trace_records[1].error_code == "UNPROCESSABLE_ENTITY"
    assert trace_records[1].summary == {
        "action_url": None,
        "details": [
            {
                "description": "Value is not supported for this partner configuration.",
                "field": "/operations/0/api_integration_preference/rest_api_integration/third_party_details/features/0",
                "issue": "INVALID_PARAMETER_VALUE",
                "location": None,
            }
        ],
        "keys": ["details", "message", "name"],
        "link_rels": [],
        "message": "Request is not well-formed, syntactically incorrect, or violates schema.",
        "name": "UNPROCESSABLE_ENTITY",
    }


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


def test_get_billing_account_readiness_maps_paypal_seller_gaps_to_creator_actions():
    transport = _StubPayPalTransport(
        responses=[
            {"access_token": "oauth_story_pp7"},
            {
                "merchant_id": "merchant_story_pp7",
                "tracking_id": "tracking_story_pp7",
                "payments_receivable": False,
                "primary_email_confirmed": False,
            },
        ]
    )
    provider = _provider(transport=transport)

    readiness = provider.get_billing_account_readiness(
        provider_account_id="merchant_story_pp7"
    )

    assert readiness == BillingAccountReadiness(
        can_create_invoices=False,
        creator_actionable_issue_codes=(
            BILLING_ACCOUNT_READINESS_ISSUE_CONFIRM_PAYPAL_PRIMARY_EMAIL,
            BILLING_ACCOUNT_READINESS_ISSUE_ENABLE_PAYPAL_PAYMENTS_RECEIVABLE,
        ),
    )


def test_verify_webhook_event_posts_paypal_verification_payload_and_returns_true():
    transport = _StubPayPalTransport(
        responses=[
            {"access_token": "oauth_story_pp8"},
            {"verification_status": "SUCCESS"},
        ]
    )
    provider = _provider(transport=transport)

    verified = provider.verify_webhook_event(
        webhook_id="WH_story_pp8",
        auth_algo="SHA256withRSA",
        cert_url="https://api.sandbox.paypal.com/v1/notifications/certs/CERT-story-pp8",
        transmission_id="transmission_story_pp8",
        transmission_sig="sig_story_pp8",
        transmission_time="2026-03-20T04:52:23Z",
        webhook_event={
            "id": "WH-PP8-STORY",
            "event_type": "INVOICING.INVOICE.PAID",
        },
    )

    assert verified is True
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
            method="POST",
            url="https://api-m.sandbox.paypal.com/v1/notifications/verify-webhook-signature",
            headers={
                "Authorization": "Bearer oauth_story_pp8",
                "PayPal-Partner-Attribution-Id": "pp_partner_story_pp7",
            },
            json_body={
                "auth_algo": "SHA256withRSA",
                "cert_url": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-story-pp8",
                "transmission_id": "transmission_story_pp8",
                "transmission_sig": "sig_story_pp8",
                "transmission_time": "2026-03-20T04:52:23Z",
                "webhook_id": "WH_story_pp8",
                "webhook_event": {
                    "id": "WH-PP8-STORY",
                    "event_type": "INVOICING.INVOICE.PAID",
                },
            },
            form_body=None,
        ),
    ]


def test_get_invoice_paid_snapshot_reads_hosted_buyer_paid_shape():
    transport = _StubPayPalTransport(
        responses=[
            {"access_token": "oauth_story_pp8"},
            {
                "id": "INV2-story-pp8",
                "status": "PAID",
                "payments": {
                    "transactions": [
                        {
                            "type": "PAYPAL",
                            "method": "PAYPAL",
                            "transaction_status": "SUCCESS",
                            "payment_date_time": "2026-03-20T04:52:07Z",
                        }
                    ]
                },
            },
        ]
    )
    provider = _provider(transport=transport)

    snapshot = provider.get_invoice_paid_snapshot(
        provider_account_id="merchant_story_pp8",
        provider_invoice_id="INV2-story-pp8",
    )

    assert snapshot == PayPalInvoicePaidSnapshot(
        invoice_id="INV2-story-pp8",
        status="PAID",
        payment_type="PAYPAL",
        payment_method="PAYPAL",
        transaction_status="SUCCESS",
        paid_at=datetime(2026, 3, 20, 4, 52, 7, tzinfo=timezone.utc),
    )
    assert snapshot.is_canonical_paid is True
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
            url="https://api-m.sandbox.paypal.com/v2/invoicing/invoices/INV2-story-pp8",
            headers={
                "Authorization": "Bearer oauth_story_pp8",
                "PayPal-Partner-Attribution-Id": "pp_partner_story_pp7",
                "PayPal-Auth-Assertion": "eyJhbGciOiJub25lIn0.eyJpc3MiOiJjbGllbnRfc3RvcnlfcHA3IiwicGF5ZXJfaWQiOiJtZXJjaGFudF9zdG9yeV9wcDgifQ.",
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
