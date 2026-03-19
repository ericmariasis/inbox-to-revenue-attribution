import base64
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from sqlalchemy import or_, select

from app.core.config import Settings, get_settings
from app.db.session import SessionLocal
from app.models.booking import Booking


DEFAULT_STRIPE_API_BASE_URL = "https://api.stripe.com/v1"
DEFAULT_STRIPE_CONNECT_TOKEN_URL = "https://connect.stripe.com/oauth/token"


class StripeProviderError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        operation: str,
        http_status: int | None = None,
        error_code: str | None = None,
        error_type: str | None = None,
    ):
        super().__init__(message)
        self.operation = operation
        self.http_status = http_status
        self.error_code = error_code
        self.error_type = error_type


@dataclass(frozen=True)
class StripeAccountReadiness:
    charges_enabled: bool


@dataclass(frozen=True)
class StripeInvoiceCreateResult:
    stripe_invoice_id: str
    status: str = "open"


@dataclass(frozen=True)
class StripeApiRequestError(Exception):
    operation: str
    http_status: int | None = None
    error_code: str | None = None
    error_type: str | None = None


class StripeProvider(Protocol):
    def build_connect_onboarding_url(self, *, creator_id: str, state: str) -> str: ...

    def exchange_connect_callback(self, *, code: str, state: str) -> str: ...

    def get_account_readiness(self, *, stripe_account_id: str) -> StripeAccountReadiness: ...

    def create_invoice(
        self,
        *,
        stripe_account_id: str,
        amount_cents: int,
        currency: str,
        metadata: dict[str, str],
        idempotency_key: str,
    ) -> StripeInvoiceCreateResult: ...

    def void_invoice(self, *, stripe_account_id: str, stripe_invoice_id: str) -> None: ...


class StripeHttpTransport(Protocol):
    def request(
        self,
        *,
        method: str,
        url: str,
        api_key: str,
        params: Mapping[str, Any] | None = None,
        stripe_account_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]: ...


class UrllibStripeHttpTransport:
    def __init__(self, *, timeout_seconds: int = 10):
        self._timeout_seconds = timeout_seconds

    def request(
        self,
        *,
        method: str,
        url: str,
        api_key: str,
        params: Mapping[str, Any] | None = None,
        stripe_account_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        resolved_method = method.upper()
        resolved_url = url
        encoded_params = urlencode(_flatten_form_fields(params or {})).encode("utf-8")
        request_data: bytes | None = None
        headers = {
            "Authorization": f"Basic {_basic_auth_token(api_key)}",
        }
        if stripe_account_id:
            headers["Stripe-Account"] = stripe_account_id
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        if resolved_method == "GET":
            if encoded_params:
                resolved_url = f"{url}?{encoded_params.decode('utf-8')}"
        else:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            request_data = encoded_params

        request = Request(
            resolved_url,
            data=request_data,
            headers=headers,
            method=resolved_method,
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                payload = response.read().decode("utf-8")
        except HTTPError as exc:
            raise _stripe_api_request_error_from_http_error(exc, operation=resolved_method) from exc
        except URLError as exc:
            raise StripeApiRequestError(operation=resolved_method) from exc

        try:
            parsed_payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise StripeApiRequestError(operation=resolved_method) from exc

        if not isinstance(parsed_payload, dict):
            raise StripeApiRequestError(operation=resolved_method)

        return parsed_payload


class StripeOAuthProvider:
    def __init__(
        self,
        *,
        authorize_url: str,
        client_id: str,
        redirect_uri: str,
        client_secret: str,
        api_base_url: str = DEFAULT_STRIPE_API_BASE_URL,
        connect_token_url: str = DEFAULT_STRIPE_CONNECT_TOKEN_URL,
        transport: StripeHttpTransport | None = None,
        booking_email_lookup: Callable[[str | None, str], str | None] | None = None,
    ):
        self._authorize_url = authorize_url.rstrip("?")
        self._client_id = client_id
        self._redirect_uri = redirect_uri
        self._client_secret = client_secret
        self._api_base_url = api_base_url.rstrip("/")
        self._connect_token_url = connect_token_url
        self._transport = transport or UrllibStripeHttpTransport()
        self._booking_email_lookup = booking_email_lookup or _lookup_booking_email

    def build_connect_onboarding_url(self, *, creator_id: str, state: str) -> str:
        del creator_id
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._client_id,
                "scope": "read_write",
                "state": state,
                "redirect_uri": self._redirect_uri,
            }
        )
        return f"{self._authorize_url}?{query}"

    def exchange_connect_callback(self, *, code: str, state: str) -> str:
        del state
        response = self._request(
            operation="stripe_connect_callback_exchange",
            method="POST",
            url=self._connect_token_url,
            params={
                "code": code,
                "grant_type": "authorization_code",
            },
        )
        stripe_account_id = response.get("stripe_user_id")
        if not isinstance(stripe_account_id, str) or not stripe_account_id:
            raise StripeProviderError(
                "stripe callback exchange failed",
                operation="stripe_connect_callback_exchange",
            )
        return stripe_account_id

    def get_account_readiness(self, *, stripe_account_id: str) -> StripeAccountReadiness:
        response = self._request(
            operation="stripe_account_readiness",
            method="GET",
            url=f"{self._api_base_url}/accounts/{quote(stripe_account_id, safe='')}",
        )
        charges_enabled = response.get("charges_enabled")
        if not isinstance(charges_enabled, bool):
            raise StripeProviderError(
                "stripe account readiness lookup failed",
                operation="stripe_account_readiness",
            )
        return StripeAccountReadiness(charges_enabled=charges_enabled)

    def create_invoice(
        self,
        *,
        stripe_account_id: str,
        amount_cents: int,
        currency: str,
        metadata: dict[str, str],
        idempotency_key: str,
    ) -> StripeInvoiceCreateResult:
        customer_id = self._create_customer(
            stripe_account_id=stripe_account_id,
            metadata=metadata,
            idempotency_key=idempotency_key,
        )
        self._create_invoice_item(
            stripe_account_id=stripe_account_id,
            customer_id=customer_id,
            amount_cents=amount_cents,
            currency=currency,
            metadata=metadata,
            idempotency_key=idempotency_key,
        )
        invoice_response = self._request(
            operation="stripe_invoice_create",
            method="POST",
            url=f"{self._api_base_url}/invoices",
            stripe_account_id=stripe_account_id,
            idempotency_key=f"{idempotency_key}:invoice",
            params={
                "auto_advance": False,
                "collection_method": "send_invoice",
                "customer": customer_id,
                "days_until_due": 30,
                "metadata": metadata,
                "pending_invoice_items_behavior": "include",
            },
        )
        stripe_invoice_id = _required_object_id(
            invoice_response,
            operation="stripe_invoice_create",
            message="stripe invoice creation failed",
        )
        finalized_response = self._request(
            operation="stripe_invoice_finalize",
            method="POST",
            url=f"{self._api_base_url}/invoices/{quote(stripe_invoice_id, safe='')}/finalize",
            stripe_account_id=stripe_account_id,
            idempotency_key=f"{idempotency_key}:finalize",
        )
        finalized_invoice_id = _required_object_id(
            finalized_response,
            operation="stripe_invoice_finalize",
            message="stripe invoice finalization failed",
        )
        finalized_status = finalized_response.get("status")
        if finalized_invoice_id != stripe_invoice_id or finalized_status not in {"open", "paid"}:
            raise StripeProviderError(
                "stripe invoice finalization failed",
                operation="stripe_invoice_finalize",
            )
        return StripeInvoiceCreateResult(
            stripe_invoice_id=finalized_invoice_id,
            status=finalized_status,
        )

    def void_invoice(self, *, stripe_account_id: str, stripe_invoice_id: str) -> None:
        invoice_response = self._request(
            operation="stripe_invoice_retrieve_for_void",
            method="GET",
            url=f"{self._api_base_url}/invoices/{quote(stripe_invoice_id, safe='')}",
            stripe_account_id=stripe_account_id,
        )
        current_status = invoice_response.get("status")
        if current_status == "void":
            return
        if current_status not in {"open", "uncollectible"}:
            raise StripeProviderError(
                "stripe invoice void failed",
                operation="stripe_invoice_void",
                error_code=f"invoice_status_{current_status}",
            )

        voided_response = self._request(
            operation="stripe_invoice_void",
            method="POST",
            url=f"{self._api_base_url}/invoices/{quote(stripe_invoice_id, safe='')}/void",
            stripe_account_id=stripe_account_id,
            idempotency_key=f"billing:void:{stripe_invoice_id}",
        )
        voided_invoice_id = _required_object_id(
            voided_response,
            operation="stripe_invoice_void",
            message="stripe invoice void failed",
        )
        if voided_invoice_id != stripe_invoice_id or voided_response.get("status") != "void":
            raise StripeProviderError(
                "stripe invoice void failed",
                operation="stripe_invoice_void",
            )

    def _create_customer(
        self,
        *,
        stripe_account_id: str,
        metadata: dict[str, str],
        idempotency_key: str,
    ) -> str:
        booking_provider, booking_identifier = _metadata_booking_identity(metadata)
        booking_email = (
            self._booking_email_lookup(booking_provider, booking_identifier)
            if booking_identifier is not None
            else None
        )
        customer_params: dict[str, Any] = {
            "metadata": metadata,
        }
        if booking_email:
            customer_params["email"] = booking_email

        customer_response = self._request(
            operation="stripe_customer_create",
            method="POST",
            url=f"{self._api_base_url}/customers",
            stripe_account_id=stripe_account_id,
            idempotency_key=f"{idempotency_key}:customer",
            params=customer_params,
        )
        return _required_object_id(
            customer_response,
            operation="stripe_customer_create",
            message="stripe customer creation failed",
        )

    def _create_invoice_item(
        self,
        *,
        stripe_account_id: str,
        customer_id: str,
        amount_cents: int,
        currency: str,
        metadata: dict[str, str],
        idempotency_key: str,
    ) -> None:
        invoice_item_response = self._request(
            operation="stripe_invoice_item_create",
            method="POST",
            url=f"{self._api_base_url}/invoiceitems",
            stripe_account_id=stripe_account_id,
            idempotency_key=f"{idempotency_key}:invoice_item",
            params={
                "amount": amount_cents,
                "currency": currency.lower(),
                "customer": customer_id,
                "description": _invoice_description(metadata),
                "metadata": metadata,
            },
        )
        _required_object_id(
            invoice_item_response,
            operation="stripe_invoice_item_create",
            message="stripe invoice item creation failed",
        )

    def _request(
        self,
        *,
        operation: str,
        method: str,
        url: str,
        params: Mapping[str, Any] | None = None,
        stripe_account_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        try:
            return self._transport.request(
                method=method,
                url=url,
                api_key=self._client_secret,
                params=params,
                stripe_account_id=stripe_account_id,
                idempotency_key=idempotency_key,
            )
        except StripeApiRequestError as exc:
            raise StripeProviderError(
                _operation_message(operation),
                operation=operation,
                http_status=exc.http_status,
                error_code=exc.error_code,
                error_type=exc.error_type,
            ) from exc


def build_default_stripe_provider(*, settings: Settings | None = None) -> StripeProvider:
    resolved_settings = settings or get_settings()
    return StripeOAuthProvider(
        authorize_url=resolved_settings.stripe_connect_authorize_url,
        client_id=resolved_settings.stripe_connect_client_id,
        redirect_uri=resolved_settings.stripe_connect_redirect_uri,
        client_secret=resolved_settings.stripe_secret_key,
    )


def _basic_auth_token(api_key: str) -> str:
    encoded = base64.b64encode(f"{api_key}:".encode("utf-8"))
    return encoded.decode("ascii")


def _flatten_form_fields(params: Mapping[str, Any]) -> list[tuple[str, str]]:
    flattened: list[tuple[str, str]] = []
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                flattened.append((f"{key}[{child_key}]", str(child_value)))
            continue
        if isinstance(value, bool):
            flattened.append((key, "true" if value else "false"))
            continue
        flattened.append((key, str(value)))
    return flattened


def _stripe_api_request_error_from_http_error(
    exc: HTTPError,
    *,
    operation: str,
) -> StripeApiRequestError:
    error_code: str | None = None
    error_type: str | None = None
    try:
        raw_body = exc.read().decode("utf-8")
        parsed_body = json.loads(raw_body)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        parsed_body = None

    if isinstance(parsed_body, dict):
        error_payload = parsed_body.get("error")
        if isinstance(error_payload, dict):
            raw_code = error_payload.get("code")
            raw_type = error_payload.get("type")
            if isinstance(raw_code, str) and raw_code:
                error_code = raw_code
            if isinstance(raw_type, str) and raw_type:
                error_type = raw_type

    return StripeApiRequestError(
        operation=operation,
        http_status=exc.code,
        error_code=error_code,
        error_type=error_type,
    )


def _required_object_id(
    payload: dict[str, Any],
    *,
    operation: str,
    message: str,
) -> str:
    object_id = payload.get("id")
    if not isinstance(object_id, str) or not object_id:
        raise StripeProviderError(message, operation=operation)
    return object_id


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


def _operation_message(operation: str) -> str:
    messages = {
        "stripe_connect_callback_exchange": "stripe callback exchange failed",
        "stripe_account_readiness": "stripe account readiness lookup failed",
        "stripe_customer_create": "stripe customer creation failed",
        "stripe_invoice_item_create": "stripe invoice item creation failed",
        "stripe_invoice_create": "stripe invoice creation failed",
        "stripe_invoice_finalize": "stripe invoice finalization failed",
        "stripe_invoice_retrieve_for_void": "stripe invoice retrieval failed",
        "stripe_invoice_void": "stripe invoice void failed",
    }
    return messages.get(operation, "stripe provider request failed")
