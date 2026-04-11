import base64
import json
from datetime import date, datetime, time, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from sqlalchemy import or_, select

from app.core.config import Settings, get_settings
from app.db.session import SessionLocal
from app.models.billing_provider import BILLING_PROVIDER_PAYPAL
from app.models.booking import Booking
from app.services.billing_provider import (
    BILLING_ACCOUNT_READINESS_ISSUE_CONFIRM_PAYPAL_PRIMARY_EMAIL,
    BILLING_ACCOUNT_READINESS_ISSUE_ENABLE_PAYPAL_PAYMENTS_RECEIVABLE,
    BillingAccountReadiness,
    BillingProviderError,
    BillingProviderInvoiceCreateResult,
    BillingProviderInvoiceStopResult,
)

DEFAULT_PAYPAL_PARTNER_REFERRALS_RETURN_URL_DESCRIPTION = "Creator Compass PayPal onboarding"
PAYPAL_SELLER_ONBOARDING_FEATURES = (
    "INVOICE_READ_WRITE",
    "ACCESS_MERCHANT_INFORMATION",
)


class PayPalProviderError(BillingProviderError):
    def __init__(
        self,
        message: str,
        *,
        operation: str,
        http_status: int | None = None,
        error_code: str | None = None,
        debug_id: str | None = None,
    ):
        super().__init__(
            message,
            provider_name=BILLING_PROVIDER_PAYPAL,
            operation=operation,
            http_status=http_status,
            error_code=error_code,
        )
        self.debug_id = debug_id


@dataclass(frozen=True)
class PayPalConnectOnboardingResult:
    onboarding_url: str
    tracking_id: str


@dataclass(frozen=True)
class PayPalSellerStatus:
    merchant_id: str
    tracking_id: str
    payments_receivable: bool
    primary_email_confirmed: bool


@dataclass(frozen=True)
class PayPalInvoicePaidSnapshot:
    invoice_id: str
    status: str
    payment_type: str | None
    payment_method: str | None
    transaction_status: str | None
    paid_at: datetime | None

    @property
    def is_canonical_paid(self) -> bool:
        return (
            self.status == "PAID"
            and self.payment_type == "PAYPAL"
            and self.payment_method == "PAYPAL"
            and self.transaction_status == "SUCCESS"
        )


@dataclass(frozen=True)
class PayPalApiResponse:
    payload: dict[str, Any]
    http_status: int | None = None
    debug_id: str | None = None


@dataclass(frozen=True)
class PayPalApiRequestError(Exception):
    operation: str
    http_status: int | None = None
    error_code: str | None = None
    debug_id: str | None = None
    payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class PayPalApiTraceRecord:
    recorded_at: str
    operation: str
    method: str
    url: str
    http_status: int | None
    debug_id: str | None
    error_code: str | None
    summary: dict[str, Any]


class FilePayPalApiTraceRecorder:
    def __init__(self, *, path: str):
        self._path = Path(path)

    @property
    def path(self) -> str:
        return str(self._path)

    def __call__(self, record: PayPalApiTraceRecord) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "recorded_at": record.recorded_at,
                        "operation": record.operation,
                        "method": record.method,
                        "url": record.url,
                        "http_status": record.http_status,
                        "debug_id": record.debug_id,
                        "error_code": record.error_code,
                        "summary": record.summary,
                    },
                    sort_keys=True,
                )
                + "\n"
            )


class PayPalProvider(Protocol):
    billing_provider_name: str

    def create_connect_onboarding(
        self,
        *,
        tracking_id: str,
        return_url: str,
    ) -> PayPalConnectOnboardingResult: ...

    def get_verified_seller_status(
        self,
        *,
        tracking_id: str,
    ) -> PayPalSellerStatus: ...

    def get_billing_account_readiness(
        self,
        *,
        provider_account_id: str,
    ) -> BillingAccountReadiness: ...

    def verify_webhook_event(
        self,
        *,
        webhook_id: str,
        auth_algo: str,
        cert_url: str,
        transmission_id: str,
        transmission_sig: str,
        transmission_time: str,
        webhook_event: Mapping[str, Any],
    ) -> bool: ...

    def get_invoice_paid_snapshot(
        self,
        *,
        provider_account_id: str,
        provider_invoice_id: str,
    ) -> PayPalInvoicePaidSnapshot: ...

    def create_billing_invoice(
        self,
        *,
        provider_account_id: str,
        amount_cents: int,
        currency: str,
        metadata: dict[str, str],
        idempotency_key: str,
    ) -> BillingProviderInvoiceCreateResult: ...

    def stop_billing_invoice(
        self,
        *,
        provider_account_id: str,
        provider_invoice_id: str,
    ) -> BillingProviderInvoiceStopResult: ...


class UrllibPayPalHttpTransport:
    def __init__(self, *, timeout_seconds: int = 10):
        self._timeout_seconds = timeout_seconds

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
        form_body: Mapping[str, str] | None = None,
    ) -> PayPalApiResponse:
        resolved_method = method.upper()
        request_headers = dict(headers or {})
        request_data: bytes | None = None

        if json_body is not None and form_body is not None:
            raise ValueError("json_body and form_body cannot both be set")

        if json_body is not None:
            request_headers.setdefault("Content-Type", "application/json")
            request_data = json.dumps(json_body).encode("utf-8")
        elif form_body is not None:
            request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
            request_data = urlencode(form_body).encode("utf-8")

        request = Request(
            url,
            data=request_data,
            headers=request_headers,
            method=resolved_method,
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                payload = response.read().decode("utf-8")
                http_status = response.status
                debug_id = _paypal_debug_id_from_headers(response.headers)
        except HTTPError as exc:
            raise _paypal_api_request_error_from_http_error(exc, operation=resolved_method) from exc
        except URLError as exc:
            raise PayPalApiRequestError(operation=resolved_method) from exc

        if not payload:
            return PayPalApiResponse(
                payload={},
                http_status=http_status,
                debug_id=debug_id,
            )

        try:
            parsed_payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise PayPalApiRequestError(operation=resolved_method) from exc

        if not isinstance(parsed_payload, dict):
            raise PayPalApiRequestError(operation=resolved_method)

        return PayPalApiResponse(
            payload=parsed_payload,
            http_status=http_status,
            debug_id=debug_id,
        )


class PayPalSellerOnboardingProvider:
    billing_provider_name = BILLING_PROVIDER_PAYPAL

    def __init__(
        self,
        *,
        environment: str = "sandbox",
        client_id: str,
        client_secret: str,
        partner_id: str,
        api_base_url: str,
        partner_attribution_id: str = "",
        transport: UrllibPayPalHttpTransport | None = None,
        booking_email_lookup: Callable[[str | None, str], str | None] | None = None,
        request_trace_recorder: Callable[[PayPalApiTraceRecord], None] | None = None,
    ):
        self._environment = environment.strip().lower() or "sandbox"
        self._client_id = client_id.strip()
        self._client_secret = client_secret.strip()
        self._partner_id = partner_id.strip()
        self._api_base_url = api_base_url.rstrip("/")
        self._partner_attribution_id = partner_attribution_id.strip()
        self._transport = transport or UrllibPayPalHttpTransport()
        self._booking_email_lookup = booking_email_lookup or _lookup_booking_email
        self._request_trace_recorder = request_trace_recorder

    def create_connect_onboarding(
        self,
        *,
        tracking_id: str,
        return_url: str,
    ) -> PayPalConnectOnboardingResult:
        access_token = self._oauth_access_token()
        payload = {
            "tracking_id": tracking_id,
            "operations": [
                {
                    "operation": "API_INTEGRATION",
                    "api_integration_preference": {
                        "rest_api_integration": {
                            "integration_method": "PAYPAL",
                            "integration_type": "THIRD_PARTY",
                            "third_party_details": {
                                "features": list(PAYPAL_SELLER_ONBOARDING_FEATURES)
                            },
                        }
                    },
                }
            ],
            "products": ["EXPRESS_CHECKOUT"],
            "legal_consents": [
                {
                    "type": "SHARE_DATA_CONSENT",
                    "granted": True,
                }
            ],
            "partner_config_override": {
                "return_url": return_url,
                "return_url_description": DEFAULT_PAYPAL_PARTNER_REFERRALS_RETURN_URL_DESCRIPTION,
            },
        }
        response = self._request(
            operation="paypal_partner_referral_create",
            method="POST",
            url=f"{self._api_base_url}/v2/customer/partner-referrals",
            access_token=access_token,
            json_body=payload,
        )
        action_url = _required_link_href(
            response,
            rel="action_url",
            operation="paypal_partner_referral_create",
            message="paypal onboarding start failed",
        )
        return PayPalConnectOnboardingResult(
            onboarding_url=action_url,
            tracking_id=tracking_id,
        )

    def get_verified_seller_status(
        self,
        *,
        tracking_id: str,
    ) -> PayPalSellerStatus:
        if not self._partner_id:
            raise PayPalProviderError(
                f"paypal {self._environment} partner id is not configured",
                operation="paypal_configuration",
            )

        access_token = self._oauth_access_token()
        tracking_lookup_response = self._request(
            operation="paypal_merchant_lookup_by_tracking_id",
            method="GET",
            url=(
                f"{self._api_base_url}/v1/customer/partners/{quote(self._partner_id, safe='')}"
                f"/merchant-integrations?{urlencode({'tracking_id': tracking_id})}"
            ),
            access_token=access_token,
        )
        tracking_record = _single_merchant_integration_record(
            tracking_lookup_response,
            operation="paypal_merchant_lookup_by_tracking_id",
            message="paypal merchant lookup failed",
        )
        merchant_id = _required_string(
            tracking_record,
            field_name="merchant_id",
            operation="paypal_merchant_lookup_by_tracking_id",
            message="paypal merchant lookup failed",
        )
        resolved_tracking_id = _required_string(
            tracking_record,
            field_name="tracking_id",
            operation="paypal_merchant_lookup_by_tracking_id",
            message="paypal merchant lookup failed",
        )

        seller_status = self._get_seller_status_by_merchant_id(
            access_token=access_token,
            merchant_id=merchant_id,
        )
        if (
            seller_status.merchant_id != merchant_id
            or seller_status.tracking_id != resolved_tracking_id
        ):
            raise PayPalProviderError(
                "paypal merchant status lookup failed",
                operation="paypal_merchant_status",
            )
        return seller_status

    def get_billing_account_readiness(
        self,
        *,
        provider_account_id: str,
    ) -> BillingAccountReadiness:
        if not self._partner_id:
            raise PayPalProviderError(
                f"paypal {self._environment} partner id is not configured",
                operation="paypal_configuration",
            )

        access_token = self._oauth_access_token()
        seller_status = self._get_seller_status_by_merchant_id(
            access_token=access_token,
            merchant_id=provider_account_id,
        )
        issue_codes: list[str] = []
        if not seller_status.primary_email_confirmed:
            issue_codes.append(BILLING_ACCOUNT_READINESS_ISSUE_CONFIRM_PAYPAL_PRIMARY_EMAIL)
        if not seller_status.payments_receivable:
            issue_codes.append(BILLING_ACCOUNT_READINESS_ISSUE_ENABLE_PAYPAL_PAYMENTS_RECEIVABLE)
        return BillingAccountReadiness(
            can_create_invoices=(
                seller_status.payments_receivable and seller_status.primary_email_confirmed
            ),
            creator_actionable_issue_codes=tuple(issue_codes),
        )

    def verify_webhook_event(
        self,
        *,
        webhook_id: str,
        auth_algo: str,
        cert_url: str,
        transmission_id: str,
        transmission_sig: str,
        transmission_time: str,
        webhook_event: Mapping[str, Any],
    ) -> bool:
        resolved_webhook_id = webhook_id.strip()
        if not resolved_webhook_id:
            raise PayPalProviderError(
                "paypal webhook verification is not configured",
                operation="paypal_configuration",
            )

        access_token = self._oauth_access_token()
        verification_response = self._request(
            operation="paypal_webhook_verify",
            method="POST",
            url=f"{self._api_base_url}/v1/notifications/verify-webhook-signature",
            access_token=access_token,
            json_body={
                "auth_algo": auth_algo,
                "cert_url": cert_url,
                "transmission_id": transmission_id,
                "transmission_sig": transmission_sig,
                "transmission_time": transmission_time,
                "webhook_id": resolved_webhook_id,
                "webhook_event": dict(webhook_event),
            },
        )
        verification_status = _required_string(
            verification_response,
            field_name="verification_status",
            operation="paypal_webhook_verify",
            message="paypal webhook verification failed",
        )
        return verification_status == "SUCCESS"

    def get_invoice_paid_snapshot(
        self,
        *,
        provider_account_id: str,
        provider_invoice_id: str,
    ) -> PayPalInvoicePaidSnapshot:
        access_token = self._oauth_access_token()
        auth_assertion = _paypal_auth_assertion(
            client_id=self._client_id,
            merchant_id=provider_account_id,
        )
        invoice_response = self._request(
            operation="paypal_invoice_get",
            method="GET",
            url=f"{self._api_base_url}/v2/invoicing/invoices/{quote(provider_invoice_id, safe='')}",
            access_token=access_token,
            headers={
                "PayPal-Auth-Assertion": auth_assertion,
            },
        )
        invoice_id = _required_string(
            invoice_response,
            field_name="id",
            operation="paypal_invoice_get",
            message="paypal invoice retrieval failed",
        )
        if invoice_id != provider_invoice_id:
            raise PayPalProviderError(
                "paypal invoice retrieval failed",
                operation="paypal_invoice_get",
            )
        invoice_status = _required_string(
            invoice_response,
            field_name="status",
            operation="paypal_invoice_get",
            message="paypal invoice retrieval failed",
        )
        first_transaction = _first_paypal_payment_transaction(invoice_response)
        return PayPalInvoicePaidSnapshot(
            invoice_id=invoice_id,
            status=invoice_status,
            payment_type=_optional_string(first_transaction, field_name="type"),
            payment_method=_optional_string(first_transaction, field_name="method"),
            transaction_status=_optional_string(first_transaction, field_name="transaction_status"),
            paid_at=_optional_paypal_payment_timestamp(first_transaction),
        )

    def create_billing_invoice(
        self,
        *,
        provider_account_id: str,
        amount_cents: int,
        currency: str,
        metadata: dict[str, str],
        idempotency_key: str,
    ) -> BillingProviderInvoiceCreateResult:
        _, provider_booking_id = _metadata_booking_identity(metadata)
        if provider_booking_id is None:
            raise PayPalProviderError(
                "paypal invoice creation failed",
                operation="paypal_invoice_create",
            )

        recipient_email = self._booking_email_lookup(
            metadata.get("booking_provider"),
            provider_booking_id,
        )
        if recipient_email is None:
            raise PayPalProviderError(
                "paypal invoice recipient lookup failed",
                operation="paypal_invoice_recipient_lookup",
            )

        access_token = self._oauth_access_token()
        auth_assertion = _paypal_auth_assertion(
            client_id=self._client_id,
            merchant_id=provider_account_id,
        )
        create_response = self._request(
            operation="paypal_invoice_create",
            method="POST",
            url=f"{self._api_base_url}/v2/invoicing/invoices",
            access_token=access_token,
            headers={
                "PayPal-Auth-Assertion": auth_assertion,
                "PayPal-Request-Id": idempotency_key,
            },
            json_body=_paypal_invoice_payload(
                amount_cents=amount_cents,
                currency=currency,
                recipient_email=recipient_email,
                metadata=metadata,
                idempotency_key=idempotency_key,
            ),
        )
        invoice_id = _required_invoice_id(
            create_response,
            operation="paypal_invoice_create",
            message="paypal invoice creation failed",
        )
        self._request(
            operation="paypal_invoice_send",
            method="POST",
            url=f"{self._api_base_url}/v2/invoicing/invoices/{quote(invoice_id, safe='')}/send",
            access_token=access_token,
            headers={
                "PayPal-Auth-Assertion": auth_assertion,
                "PayPal-Request-Id": f"{idempotency_key}:send",
            },
            json_body={
                "send_to_invoicer": False,
                "send_to_recipient": True,
                "note": "Creator Compass invoice",
            },
        )
        invoice_response = self._request(
            operation="paypal_invoice_get",
            method="GET",
            url=f"{self._api_base_url}/v2/invoicing/invoices/{quote(invoice_id, safe='')}",
            access_token=access_token,
            headers={
                "PayPal-Auth-Assertion": auth_assertion,
            },
        )
        resolved_invoice_id = _required_string(
            invoice_response,
            field_name="id",
            operation="paypal_invoice_get",
            message="paypal invoice retrieval failed",
        )
        if resolved_invoice_id != invoice_id:
            raise PayPalProviderError(
                "paypal invoice retrieval failed",
                operation="paypal_invoice_get",
            )
        invoice_status = _required_string(
            invoice_response,
            field_name="status",
            operation="paypal_invoice_get",
            message="paypal invoice retrieval failed",
        )
        return BillingProviderInvoiceCreateResult(
            provider_account_id=provider_account_id,
            provider_invoice_id=invoice_id,
            invoice_status=_map_paypal_invoice_status_to_local_for_create(invoice_status),
        )

    def stop_billing_invoice(
        self,
        *,
        provider_account_id: str,
        provider_invoice_id: str,
    ) -> BillingProviderInvoiceStopResult:
        access_token = self._oauth_access_token()
        auth_assertion = _paypal_auth_assertion(
            client_id=self._client_id,
            merchant_id=provider_account_id,
        )
        invoice_response = self._request(
            operation="paypal_invoice_get",
            method="GET",
            url=(
                f"{self._api_base_url}/v2/invoicing/invoices/"
                f"{quote(provider_invoice_id, safe='')}"
            ),
            access_token=access_token,
            headers={
                "PayPal-Auth-Assertion": auth_assertion,
            },
        )
        invoice_status = _required_string(
            invoice_response,
            field_name="status",
            operation="paypal_invoice_get",
            message="paypal invoice retrieval failed",
        )
        if invoice_status != "CANCELLED":
            self._request(
                operation="paypal_invoice_cancel",
                method="POST",
                url=(
                    f"{self._api_base_url}/v2/invoicing/invoices/"
                    f"{quote(provider_invoice_id, safe='')}/cancel"
                ),
                access_token=access_token,
                headers={
                    "PayPal-Auth-Assertion": auth_assertion,
                    "PayPal-Request-Id": f"billing:void:{provider_invoice_id}",
                },
                json_body={
                    "send_to_invoicer": False,
                    "send_to_recipient": False,
                    "note": "Creator Compass booking canceled",
                },
            )
            invoice_response = self._request(
                operation="paypal_invoice_get",
                method="GET",
                url=(
                    f"{self._api_base_url}/v2/invoicing/invoices/"
                    f"{quote(provider_invoice_id, safe='')}"
                ),
                access_token=access_token,
                headers={
                    "PayPal-Auth-Assertion": auth_assertion,
                },
            )
            invoice_status = _required_string(
                invoice_response,
                field_name="status",
                operation="paypal_invoice_get",
                message="paypal invoice retrieval failed",
            )

        return BillingProviderInvoiceStopResult(
            provider_account_id=provider_account_id,
            provider_invoice_id=provider_invoice_id,
            invoice_status=_map_paypal_invoice_status_to_local_for_stop(invoice_status),
        )

    def _oauth_access_token(self) -> str:
        if not self._api_base_url:
            raise PayPalProviderError(
                f"paypal {self._environment} api base url is not configured",
                operation="paypal_configuration",
            )
        if not self._client_id or not self._client_secret:
            raise PayPalProviderError(
                f"paypal {self._environment} credentials are not configured",
                operation="paypal_configuration",
            )
        oauth_url = f"{self._api_base_url}/v1/oauth2/token"

        try:
            response = _coerce_paypal_api_response(
                self._transport.request(
                    method="POST",
                    url=oauth_url,
                    headers={
                        "Authorization": f"Basic {_basic_auth_token(self._client_id, self._client_secret)}",
                    },
                    form_body={"grant_type": "client_credentials"},
                )
            )
        except PayPalApiRequestError as exc:
            self._record_api_trace(
                operation="paypal_oauth_token",
                method="POST",
                url=oauth_url,
                payload=exc.payload or {},
                http_status=exc.http_status,
                debug_id=exc.debug_id,
                error_code=exc.error_code,
            )
            raise PayPalProviderError(
                "paypal oauth token request failed",
                operation="paypal_oauth_token",
                http_status=exc.http_status,
                error_code=exc.error_code,
                debug_id=exc.debug_id,
            ) from exc
        self._record_api_trace(
            operation="paypal_oauth_token",
            method="POST",
            url=oauth_url,
            payload=response.payload,
            http_status=response.http_status,
            debug_id=response.debug_id,
            error_code=None,
        )
        access_token = response.payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise PayPalProviderError(
                "paypal oauth token request failed",
                operation="paypal_oauth_token",
            )
        return access_token

    def _request(
        self,
        *,
        operation: str,
        method: str,
        url: str,
        access_token: str,
        headers: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        request_headers = {
            "Authorization": f"Bearer {access_token}",
        }
        if self._partner_attribution_id:
            request_headers["PayPal-Partner-Attribution-Id"] = self._partner_attribution_id
        if headers:
            request_headers.update(headers)

        try:
            response = _coerce_paypal_api_response(
                self._transport.request(
                    method=method,
                    url=url,
                    headers=request_headers,
                    json_body=json_body,
                )
            )
        except PayPalApiRequestError as exc:
            self._record_api_trace(
                operation=operation,
                method=method,
                url=url,
                payload=exc.payload or {},
                http_status=exc.http_status,
                debug_id=exc.debug_id,
                error_code=exc.error_code,
            )
            raise PayPalProviderError(
                _operation_message(operation),
                operation=operation,
                http_status=exc.http_status,
                error_code=exc.error_code,
                debug_id=exc.debug_id,
            ) from exc
        self._record_api_trace(
            operation=operation,
            method=method,
            url=url,
            payload=response.payload,
            http_status=response.http_status,
            debug_id=response.debug_id,
            error_code=None,
        )
        return response.payload

    def _record_api_trace(
        self,
        *,
        operation: str,
        method: str,
        url: str,
        payload: Mapping[str, Any],
        http_status: int | None,
        debug_id: str | None,
        error_code: str | None,
    ) -> None:
        if self._request_trace_recorder is None:
            return
        self._request_trace_recorder(
            PayPalApiTraceRecord(
                recorded_at=datetime.now(timezone.utc).isoformat(),
                operation=operation,
                method=method.upper(),
                url=url,
                http_status=http_status,
                debug_id=debug_id,
                error_code=error_code,
                summary=_paypal_trace_summary(operation=operation, payload=payload),
            )
        )

    def _get_seller_status_by_merchant_id(
        self,
        *,
        access_token: str,
        merchant_id: str,
    ) -> PayPalSellerStatus:
        seller_status_response = self._request(
            operation="paypal_merchant_status",
            method="GET",
            url=(
                f"{self._api_base_url}/v1/customer/partners/{quote(self._partner_id, safe='')}"
                f"/merchant-integrations/{quote(merchant_id, safe='')}"
            ),
            access_token=access_token,
        )
        status_merchant_id = _required_string(
            seller_status_response,
            field_name="merchant_id",
            operation="paypal_merchant_status",
            message="paypal merchant status lookup failed",
        )
        status_tracking_id = _required_string(
            seller_status_response,
            field_name="tracking_id",
            operation="paypal_merchant_status",
            message="paypal merchant status lookup failed",
        )
        payments_receivable = _required_bool(
            seller_status_response,
            field_name="payments_receivable",
            operation="paypal_merchant_status",
            message="paypal merchant status lookup failed",
        )
        primary_email_confirmed = _required_bool(
            seller_status_response,
            field_name="primary_email_confirmed",
            operation="paypal_merchant_status",
            message="paypal merchant status lookup failed",
        )
        return PayPalSellerStatus(
            merchant_id=status_merchant_id,
            tracking_id=status_tracking_id,
            payments_receivable=payments_receivable,
            primary_email_confirmed=primary_email_confirmed,
        )


PayPalSandboxSellerOnboardingProvider = PayPalSellerOnboardingProvider


def build_default_paypal_provider(*, settings: Settings | None = None) -> PayPalProvider:
    resolved_settings = settings or get_settings()
    return PayPalSellerOnboardingProvider(
        environment=resolved_settings.paypal_environment_value(),
        client_id=resolved_settings.selected_paypal_client_id(),
        client_secret=resolved_settings.selected_paypal_client_secret(),
        partner_id=resolved_settings.selected_paypal_partner_id(),
        api_base_url=resolved_settings.selected_paypal_api_base_url(),
        partner_attribution_id=resolved_settings.paypal_partner_attribution_id,
        request_trace_recorder=_build_paypal_api_trace_recorder(settings=resolved_settings),
    )


def _build_paypal_api_trace_recorder(
    *,
    settings: Settings,
) -> Callable[[PayPalApiTraceRecord], None] | None:
    trace_path = settings.paypal_api_trace_path.strip()
    if not trace_path:
        return None
    return FilePayPalApiTraceRecorder(path=trace_path)


def _coerce_paypal_api_response(response: Any) -> PayPalApiResponse:
    if isinstance(response, PayPalApiResponse):
        return response
    if isinstance(response, dict):
        return PayPalApiResponse(payload=response)
    raise PayPalApiRequestError(operation="unsupported_transport_response")


def _paypal_debug_id_from_headers(headers: Mapping[str, Any] | None) -> str | None:
    if headers is None:
        return None
    for header_name in ("PayPal-Debug-Id", "paypal-debug-id", "Paypal-Debug-Id"):
        header_value = headers.get(header_name)
        if isinstance(header_value, str) and header_value:
            return header_value
    return None


def _paypal_trace_summary(
    *,
    operation: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    summary: dict[str, Any] = {"keys": sorted(payload.keys())}
    error_name = _optional_string(payload, field_name="name")
    error_message = _optional_string(payload, field_name="message")
    error_details = _paypal_error_details_summary(payload)
    if error_name is not None:
        summary["name"] = error_name
    if error_message is not None:
        summary["message"] = error_message
    if error_details:
        summary["details"] = error_details

    if operation == "paypal_partner_referral_create":
        summary["link_rels"] = _paypal_link_rels(payload)
        summary["action_url"] = _optional_link_href(payload, rel="action_url")
        return summary

    if operation == "paypal_oauth_token":
        summary["token_type"] = _optional_string(payload, field_name="token_type")
        summary["scope"] = _optional_string(payload, field_name="scope")
        summary["expires_in"] = _optional_int(payload, field_name="expires_in")
        return summary

    if operation in {"paypal_merchant_lookup_by_tracking_id", "paypal_merchant_status"}:
        summary["merchant_id"] = _optional_string(payload, field_name="merchant_id")
        summary["tracking_id"] = _optional_string(payload, field_name="tracking_id")
        summary["payments_receivable"] = _optional_bool(payload, field_name="payments_receivable")
        summary["primary_email_confirmed"] = _optional_bool(
            payload,
            field_name="primary_email_confirmed",
        )
        return summary

    if operation == "paypal_webhook_verify":
        verification_status = _optional_string(payload, field_name="verification_status")
        if verification_status is not None:
            summary["verification_status"] = verification_status
        return summary

    if operation in {
        "paypal_invoice_create",
        "paypal_invoice_send",
        "paypal_invoice_get",
        "paypal_invoice_cancel",
    }:
        payment_transaction = _first_paypal_payment_transaction(payload)
        summary["invoice_id"] = _trace_invoice_id(payload)
        summary["status"] = _optional_string(payload, field_name="status")
        summary["link_rels"] = _paypal_link_rels(payload)
        summary["payment_id"] = _optional_string(payment_transaction, field_name="payment_id")
        summary["payment_type"] = _optional_string(payment_transaction, field_name="type")
        summary["payment_method"] = _optional_string(payment_transaction, field_name="method")
        summary["transaction_status"] = _optional_string(
            payment_transaction,
            field_name="transaction_status",
        )
        return summary

    return summary


def _basic_auth_token(client_id: str, client_secret: str) -> str:
    encoded = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8"))
    return encoded.decode("ascii")


def _paypal_auth_assertion(*, client_id: str, merchant_id: str) -> str:
    return (
        f"{_base64url_json({'alg': 'none'})}."
        f"{_base64url_json({'iss': client_id, 'payer_id': merchant_id})}."
    )


def _base64url_json(payload: Mapping[str, str]) -> str:
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return encoded.rstrip("=")


def _paypal_api_request_error_from_http_error(
    exc: HTTPError,
    *,
    operation: str,
) -> PayPalApiRequestError:
    error_code: str | None = None
    debug_id = _paypal_debug_id_from_headers(exc.headers)
    try:
        raw_body = exc.read().decode("utf-8")
        parsed_body = json.loads(raw_body)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        parsed_body = None

    if isinstance(parsed_body, dict):
        raw_error_code = parsed_body.get("name")
        if isinstance(raw_error_code, str) and raw_error_code:
            error_code = raw_error_code

    return PayPalApiRequestError(
        operation=operation,
        http_status=exc.code,
        error_code=error_code,
        debug_id=debug_id,
        payload=parsed_body if isinstance(parsed_body, dict) else None,
    )


def _required_link_href(
    payload: dict[str, Any],
    *,
    rel: str,
    operation: str,
    message: str,
) -> str:
    links = payload.get("links")
    if not isinstance(links, list):
        raise PayPalProviderError(message, operation=operation)

    for link in links:
        if not isinstance(link, dict):
            continue
        if link.get("rel") != rel:
            continue
        href = link.get("href")
        if isinstance(href, str) and href:
            return href

    raise PayPalProviderError(message, operation=operation)


def _required_invoice_id(
    payload: dict[str, Any],
    *,
    operation: str,
    message: str,
) -> str:
    invoice_id = payload.get("id")
    if isinstance(invoice_id, str) and invoice_id:
        return invoice_id

    href = payload.get("href")
    if isinstance(href, str) and href:
        return _invoice_id_from_href(href)

    links = payload.get("links")
    if isinstance(links, list):
        for link in links:
            if not isinstance(link, dict):
                continue
            if link.get("rel") not in {"self", "payer-view"}:
                continue
            link_href = link.get("href")
            if isinstance(link_href, str) and link_href:
                return _invoice_id_from_href(link_href)

    raise PayPalProviderError(message, operation=operation)


def _single_merchant_integration_record(
    payload: dict[str, Any],
    *,
    operation: str,
    message: str,
) -> dict[str, Any]:
    if isinstance(payload.get("merchant_id"), str):
        return payload

    for field_name in ("merchant_integrations", "items", "results"):
        candidate_records = payload.get(field_name)
        if not isinstance(candidate_records, list):
            continue
        valid_records = [record for record in candidate_records if isinstance(record, dict)]
        if len(valid_records) == 1:
            return valid_records[0]

    raise PayPalProviderError(message, operation=operation)


def _required_string(
    payload: Mapping[str, Any],
    *,
    field_name: str,
    operation: str,
    message: str,
) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value:
        raise PayPalProviderError(message, operation=operation)
    return value


def _required_bool(
    payload: Mapping[str, Any],
    *,
    field_name: str,
    operation: str,
    message: str,
) -> bool:
    value = payload.get(field_name)
    if not isinstance(value, bool):
        raise PayPalProviderError(message, operation=operation)
    return value


def _optional_string(payload: Mapping[str, Any] | None, *, field_name: str) -> str | None:
    if payload is None:
        return None
    value = payload.get(field_name)
    if isinstance(value, str) and value:
        return value
    return None


def _optional_bool(payload: Mapping[str, Any] | None, *, field_name: str) -> bool | None:
    if payload is None:
        return None
    value = payload.get(field_name)
    if isinstance(value, bool):
        return value
    return None


def _optional_int(payload: Mapping[str, Any] | None, *, field_name: str) -> int | None:
    if payload is None:
        return None
    value = payload.get(field_name)
    if isinstance(value, int):
        return value
    return None


def _optional_link_href(payload: Mapping[str, Any], *, rel: str) -> str | None:
    links = payload.get("links")
    if not isinstance(links, list):
        return None
    for link in links:
        if not isinstance(link, dict):
            continue
        if link.get("rel") != rel:
            continue
        href = link.get("href")
        if isinstance(href, str) and href:
            return href
    return None


def _paypal_link_rels(payload: Mapping[str, Any]) -> list[str]:
    links = payload.get("links")
    if not isinstance(links, list):
        return []
    rels: list[str] = []
    for link in links:
        if not isinstance(link, dict):
            continue
        rel = link.get("rel")
        if isinstance(rel, str) and rel:
            rels.append(rel)
    return rels


def _paypal_error_details_summary(payload: Mapping[str, Any]) -> list[dict[str, str | None]]:
    raw_details = payload.get("details")
    if not isinstance(raw_details, list):
        return []
    details: list[dict[str, str | None]] = []
    for entry in raw_details:
        if not isinstance(entry, dict):
            continue
        details.append(
            {
                "issue": _optional_string(entry, field_name="issue"),
                "field": _optional_string(entry, field_name="field"),
                "location": _optional_string(entry, field_name="location"),
                "description": _optional_string(entry, field_name="description"),
            }
        )
    return details


def _trace_invoice_id(payload: Mapping[str, Any]) -> str | None:
    invoice_id = _optional_string(payload, field_name="id")
    if invoice_id is not None:
        return invoice_id

    href = _optional_string(payload, field_name="href")
    if href is not None:
        return _invoice_id_from_href(href)

    links = payload.get("links")
    if not isinstance(links, list):
        return None
    for link in links:
        if not isinstance(link, dict):
            continue
        if link.get("rel") not in {"self", "payer-view"}:
            continue
        link_href = link.get("href")
        if isinstance(link_href, str) and link_href:
            return _invoice_id_from_href(link_href)
    return None


def _invoice_id_from_href(href: str) -> str:
    return href.rstrip("/").rsplit("/", 1)[-1].lstrip("#")


def _first_paypal_payment_transaction(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    payments = payload.get("payments")
    if not isinstance(payments, dict):
        return None
    transactions = payments.get("transactions")
    if not isinstance(transactions, list):
        return None
    for transaction in transactions:
        if isinstance(transaction, dict):
            return transaction
    return None


def _optional_paypal_payment_timestamp(payload: Mapping[str, Any] | None) -> datetime | None:
    payment_date_time = _optional_string(payload, field_name="payment_date_time")
    parsed_payment_date_time = _parse_paypal_timestamp(payment_date_time)
    if parsed_payment_date_time is not None:
        return parsed_payment_date_time

    payment_date = _optional_string(payload, field_name="payment_date")
    if payment_date is None:
        return None
    try:
        parsed_payment_date = date.fromisoformat(payment_date)
    except ValueError:
        return None
    return datetime.combine(parsed_payment_date, time.min, tzinfo=timezone.utc)


def _parse_paypal_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _operation_message(operation: str) -> str:
    if operation == "paypal_partner_referral_create":
        return "paypal onboarding start failed"
    if operation == "paypal_merchant_lookup_by_tracking_id":
        return "paypal merchant lookup failed"
    if operation == "paypal_merchant_status":
        return "paypal merchant status lookup failed"
    if operation == "paypal_oauth_token":
        return "paypal oauth token request failed"
    if operation == "paypal_webhook_verify":
        return "paypal webhook verification failed"
    if operation == "paypal_invoice_recipient_lookup":
        return "paypal invoice recipient lookup failed"
    if operation == "paypal_invoice_create":
        return "paypal invoice creation failed"
    if operation == "paypal_invoice_send":
        return "paypal invoice send failed"
    if operation == "paypal_invoice_get":
        return "paypal invoice retrieval failed"
    if operation == "paypal_invoice_cancel":
        return "paypal invoice cancel failed"
    return "paypal provider request failed"


def _paypal_invoice_payload(
    *,
    amount_cents: int,
    currency: str,
    recipient_email: str,
    metadata: Mapping[str, str],
    idempotency_key: str,
) -> dict[str, Any]:
    return {
        "detail": {
            "invoice_number": _paypal_invoice_number(idempotency_key=idempotency_key),
            "reference": _paypal_invoice_reference(
                metadata=metadata,
                idempotency_key=idempotency_key,
            ),
            "invoice_date": date.today().isoformat(),
            "currency_code": currency.upper(),
            "note": "Creator Compass booking invoice",
            "memo": _invoice_description(metadata),
            "payment_term": {"term_type": "DUE_ON_RECEIPT"},
        },
        "primary_recipients": [
            {
                "billing_info": {
                    "email_address": recipient_email,
                }
            }
        ],
        "items": [
            {
                "name": _invoice_item_name(metadata),
                "description": _invoice_description(metadata),
                "quantity": "1",
                "unit_amount": {
                    "currency_code": currency.upper(),
                    "value": f"{amount_cents / 100:.2f}",
                },
            }
        ],
    }


def _paypal_invoice_number(*, idempotency_key: str) -> str:
    sanitized = "".join(
        character if character.isalnum() else "-"
        for character in idempotency_key.upper()
    ).strip("-")
    return f"CCP-{sanitized[-20:]}"[:25]


def _paypal_invoice_reference(
    *,
    metadata: Mapping[str, str],
    idempotency_key: str,
) -> str:
    _, provider_booking_id = _metadata_booking_identity(metadata)
    if provider_booking_id:
        return provider_booking_id[:120]
    tid = metadata.get("tid")
    if isinstance(tid, str) and tid:
        return tid[:120]
    return idempotency_key[:120]


def _invoice_item_name(metadata: Mapping[str, str]) -> str:
    _, booking_identifier = _metadata_booking_identity(metadata)
    if booking_identifier:
        return f"Creator Compass booking {booking_identifier}"
    return "Creator Compass booking"


def _invoice_description(metadata: Mapping[str, str]) -> str:
    _, booking_identifier = _metadata_booking_identity(metadata)
    tid = metadata.get("tid")
    if booking_identifier and tid:
        return f"Creator Compass booking {booking_identifier} ({tid})"
    if booking_identifier:
        return f"Creator Compass booking {booking_identifier}"
    if tid:
        return f"Creator Compass tracked booking {tid}"
    return "Creator Compass booking"


def _metadata_booking_identity(metadata: Mapping[str, str]) -> tuple[str | None, str | None]:
    booking_provider = metadata.get("booking_provider")
    resolved_provider = (
        booking_provider if isinstance(booking_provider, str) and booking_provider else None
    )
    provider_booking_id = metadata.get("provider_booking_id")
    if isinstance(provider_booking_id, str) and provider_booking_id:
        return resolved_provider, provider_booking_id

    booking_uuid = metadata.get("booking_uuid")
    if isinstance(booking_uuid, str) and booking_uuid:
        return resolved_provider, booking_uuid

    return resolved_provider, None


def _lookup_booking_email(booking_provider: str | None, provider_booking_id: str) -> str | None:
    with SessionLocal() as session:
        query = select(Booking.email)
        if booking_provider:
            query = query.where(
                Booking.provider == booking_provider,
                Booking.provider_booking_id == provider_booking_id,
            )
        else:
            query = query.where(
                or_(
                    Booking.provider_booking_id == provider_booking_id,
                    Booking.calendly_booking_uuid == provider_booking_id,
                )
            )
        return session.scalar(query)


def _map_paypal_invoice_status_to_local_for_create(invoice_status: str) -> str:
    if invoice_status in {"UNPAID", "SENT"}:
        return "open"
    if invoice_status == "PAID":
        return "paid"
    raise PayPalProviderError(
        "paypal invoice send failed",
        operation="paypal_invoice_send",
        error_code=f"invoice_status_{invoice_status.lower()}",
    )


def _map_paypal_invoice_status_to_local_for_stop(invoice_status: str) -> str:
    if invoice_status == "CANCELLED":
        return "void"
    raise PayPalProviderError(
        "paypal invoice cancel failed",
        operation="paypal_invoice_cancel",
        error_code=f"invoice_status_{invoice_status.lower()}",
    )
