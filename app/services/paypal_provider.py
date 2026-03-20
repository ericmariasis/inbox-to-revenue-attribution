import base64
import json
from datetime import date
from dataclasses import dataclass
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
    BillingAccountReadiness,
    BillingProviderError,
    BillingProviderInvoiceCreateResult,
    BillingProviderInvoiceStopResult,
)

DEFAULT_PAYPAL_PARTNER_REFERRALS_RETURN_URL_DESCRIPTION = "Creator Compass PayPal onboarding"


class PayPalProviderError(BillingProviderError):
    def __init__(
        self,
        message: str,
        *,
        operation: str,
        http_status: int | None = None,
        error_code: str | None = None,
    ):
        super().__init__(
            message,
            provider_name=BILLING_PROVIDER_PAYPAL,
            operation=operation,
            http_status=http_status,
            error_code=error_code,
        )


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
class PayPalApiRequestError(Exception):
    operation: str
    http_status: int | None = None
    error_code: str | None = None


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
    ) -> dict[str, Any]:
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
        except HTTPError as exc:
            raise _paypal_api_request_error_from_http_error(exc, operation=resolved_method) from exc
        except URLError as exc:
            raise PayPalApiRequestError(operation=resolved_method) from exc

        if not payload:
            return {}

        try:
            parsed_payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise PayPalApiRequestError(operation=resolved_method) from exc

        if not isinstance(parsed_payload, dict):
            raise PayPalApiRequestError(operation=resolved_method)

        return parsed_payload


class PayPalSandboxSellerOnboardingProvider:
    billing_provider_name = BILLING_PROVIDER_PAYPAL

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        partner_id: str,
        api_base_url: str,
        partner_attribution_id: str = "",
        transport: UrllibPayPalHttpTransport | None = None,
        booking_email_lookup: Callable[[str | None, str], str | None] | None = None,
    ):
        self._client_id = client_id.strip()
        self._client_secret = client_secret.strip()
        self._partner_id = partner_id.strip()
        self._api_base_url = api_base_url.rstrip("/")
        self._partner_attribution_id = partner_attribution_id.strip()
        self._transport = transport or UrllibPayPalHttpTransport()
        self._booking_email_lookup = booking_email_lookup or _lookup_booking_email

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
                                "features": [
                                    "PAYMENT",
                                    "REFUND",
                                ]
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
                "paypal sandbox partner id is not configured",
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
                "paypal sandbox partner id is not configured",
                operation="paypal_configuration",
            )

        access_token = self._oauth_access_token()
        seller_status = self._get_seller_status_by_merchant_id(
            access_token=access_token,
            merchant_id=provider_account_id,
        )
        return BillingAccountReadiness(
            can_create_invoices=(
                seller_status.payments_receivable and seller_status.primary_email_confirmed
            )
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
        if not self._client_id or not self._client_secret:
            raise PayPalProviderError(
                "paypal sandbox credentials are not configured",
                operation="paypal_configuration",
            )

        try:
            response = self._transport.request(
                method="POST",
                url=f"{self._api_base_url}/v1/oauth2/token",
                headers={
                    "Authorization": f"Basic {_basic_auth_token(self._client_id, self._client_secret)}",
                },
                form_body={"grant_type": "client_credentials"},
            )
        except PayPalApiRequestError as exc:
            raise PayPalProviderError(
                "paypal oauth token request failed",
                operation="paypal_oauth_token",
                http_status=exc.http_status,
                error_code=exc.error_code,
            ) from exc
        access_token = response.get("access_token")
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
            return self._transport.request(
                method=method,
                url=url,
                headers=request_headers,
                json_body=json_body,
            )
        except PayPalApiRequestError as exc:
            raise PayPalProviderError(
                _operation_message(operation),
                operation=operation,
                http_status=exc.http_status,
                error_code=exc.error_code,
            ) from exc

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


def build_default_paypal_provider(*, settings: Settings | None = None) -> PayPalProvider:
    resolved_settings = settings or get_settings()
    return PayPalSandboxSellerOnboardingProvider(
        client_id=resolved_settings.paypal_sandbox_client_id,
        client_secret=resolved_settings.paypal_sandbox_client_secret,
        partner_id=resolved_settings.paypal_sandbox_partner_id,
        api_base_url=resolved_settings.paypal_sandbox_api_base_url,
        partner_attribution_id=resolved_settings.paypal_partner_attribution_id,
    )


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
        return href.rstrip("/").rsplit("/", 1)[-1]

    links = payload.get("links")
    if isinstance(links, list):
        for link in links:
            if not isinstance(link, dict):
                continue
            if link.get("rel") not in {"self", "payer-view"}:
                continue
            link_href = link.get("href")
            if isinstance(link_href, str) and link_href:
                return link_href.rstrip("/").rsplit("/", 1)[-1]

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


def _operation_message(operation: str) -> str:
    if operation == "paypal_partner_referral_create":
        return "paypal onboarding start failed"
    if operation == "paypal_merchant_lookup_by_tracking_id":
        return "paypal merchant lookup failed"
    if operation == "paypal_merchant_status":
        return "paypal merchant status lookup failed"
    if operation == "paypal_oauth_token":
        return "paypal oauth token request failed"
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
