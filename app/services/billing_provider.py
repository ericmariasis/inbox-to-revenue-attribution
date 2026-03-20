from dataclasses import dataclass
from typing import Protocol

from app.models.billing_provider import BILLING_PROVIDER_STRIPE


class BillingProviderError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        provider_name: str,
        operation: str,
        http_status: int | None = None,
        error_code: str | None = None,
        error_type: str | None = None,
    ):
        super().__init__(message)
        self.provider_name = provider_name
        self.operation = operation
        self.http_status = http_status
        self.error_code = error_code
        self.error_type = error_type


@dataclass(frozen=True)
class BillingAccountReadiness:
    can_create_invoices: bool


@dataclass(frozen=True)
class BillingProviderInvoiceCreateResult:
    provider_account_id: str
    provider_invoice_id: str
    invoice_status: str = "open"


@dataclass(frozen=True)
class BillingProviderInvoiceStopResult:
    provider_account_id: str
    provider_invoice_id: str
    invoice_status: str = "void"


class BillingProvider(Protocol):
    billing_provider_name: str

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


def resolve_billing_provider_name(*, provider: object) -> str:
    provider_name = getattr(provider, "billing_provider_name", None)
    if isinstance(provider_name, str) and provider_name:
        return provider_name
    # Transitional fallback while older Stripe-oriented test doubles still use the
    # pre-PP-4 method names and do not expose an explicit provider name.
    return BILLING_PROVIDER_STRIPE


def get_billing_account_readiness(
    *,
    provider: object,
    provider_account_id: str,
) -> BillingAccountReadiness:
    billing_method = getattr(provider, "get_billing_account_readiness", None)
    if callable(billing_method):
        return billing_method(provider_account_id=provider_account_id)

    legacy_method = getattr(provider, "get_account_readiness", None)
    if callable(legacy_method):
        legacy_readiness = legacy_method(stripe_account_id=provider_account_id)
        can_create_invoices = getattr(legacy_readiness, "charges_enabled", None)
        if isinstance(can_create_invoices, bool):
            return BillingAccountReadiness(can_create_invoices=can_create_invoices)

    raise TypeError("billing provider does not implement account readiness")


def create_billing_invoice(
    *,
    provider: object,
    provider_account_id: str,
    amount_cents: int,
    currency: str,
    metadata: dict[str, str],
    idempotency_key: str,
) -> BillingProviderInvoiceCreateResult:
    billing_method = getattr(provider, "create_billing_invoice", None)
    if callable(billing_method):
        return billing_method(
            provider_account_id=provider_account_id,
            amount_cents=amount_cents,
            currency=currency,
            metadata=metadata,
            idempotency_key=idempotency_key,
        )

    legacy_method = getattr(provider, "create_invoice", None)
    if callable(legacy_method):
        legacy_result = legacy_method(
            stripe_account_id=provider_account_id,
            amount_cents=amount_cents,
            currency=currency,
            metadata=metadata,
            idempotency_key=idempotency_key,
        )
        provider_invoice_id = getattr(legacy_result, "stripe_invoice_id", None)
        invoice_status = getattr(legacy_result, "status", None)
        if isinstance(provider_invoice_id, str) and provider_invoice_id:
            return BillingProviderInvoiceCreateResult(
                provider_account_id=provider_account_id,
                provider_invoice_id=provider_invoice_id,
                invoice_status=invoice_status if isinstance(invoice_status, str) and invoice_status else "open",
            )

    raise TypeError("billing provider does not implement invoice creation")


def stop_billing_invoice(
    *,
    provider: object,
    provider_account_id: str,
    provider_invoice_id: str,
) -> BillingProviderInvoiceStopResult:
    billing_method = getattr(provider, "stop_billing_invoice", None)
    if callable(billing_method):
        return billing_method(
            provider_account_id=provider_account_id,
            provider_invoice_id=provider_invoice_id,
        )

    legacy_method = getattr(provider, "void_invoice", None)
    if callable(legacy_method):
        legacy_method(
            stripe_account_id=provider_account_id,
            stripe_invoice_id=provider_invoice_id,
        )
        return BillingProviderInvoiceStopResult(
            provider_account_id=provider_account_id,
            provider_invoice_id=provider_invoice_id,
            invoice_status="void",
        )

    raise TypeError("billing provider does not implement invoice stop")
