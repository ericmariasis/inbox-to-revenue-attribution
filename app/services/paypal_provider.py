import base64
import json
from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from app.core.config import Settings, get_settings

DEFAULT_PAYPAL_PARTNER_REFERRALS_RETURN_URL_DESCRIPTION = "Creator Compass PayPal onboarding"


class PayPalProviderError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        operation: str,
        http_status: int | None = None,
        error_code: str | None = None,
    ):
        super().__init__(message)
        self.operation = operation
        self.http_status = http_status
        self.error_code = error_code


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

        try:
            parsed_payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise PayPalApiRequestError(operation=resolved_method) from exc

        if not isinstance(parsed_payload, dict):
            raise PayPalApiRequestError(operation=resolved_method)

        return parsed_payload


class PayPalSandboxSellerOnboardingProvider:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        partner_id: str,
        api_base_url: str,
        partner_attribution_id: str = "",
        transport: UrllibPayPalHttpTransport | None = None,
    ):
        self._client_id = client_id.strip()
        self._client_secret = client_secret.strip()
        self._partner_id = partner_id.strip()
        self._api_base_url = api_base_url.rstrip("/")
        self._partner_attribution_id = partner_attribution_id.strip()
        self._transport = transport or UrllibPayPalHttpTransport()

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

        if status_merchant_id != merchant_id or status_tracking_id != resolved_tracking_id:
            raise PayPalProviderError(
                "paypal merchant status lookup failed",
                operation="paypal_merchant_status",
            )

        return PayPalSellerStatus(
            merchant_id=status_merchant_id,
            tracking_id=status_tracking_id,
            payments_receivable=payments_receivable,
            primary_email_confirmed=primary_email_confirmed,
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
        json_body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {access_token}",
        }
        if self._partner_attribution_id:
            headers["PayPal-Partner-Attribution-Id"] = self._partner_attribution_id

        try:
            return self._transport.request(
                method=method,
                url=url,
                headers=headers,
                json_body=json_body,
            )
        except PayPalApiRequestError as exc:
            raise PayPalProviderError(
                _operation_message(operation),
                operation=operation,
                http_status=exc.http_status,
                error_code=exc.error_code,
            ) from exc


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
    return "paypal provider request failed"
