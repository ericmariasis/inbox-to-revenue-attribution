from app.models.creator import Creator
from app.models.billing_provider import BILLING_PROVIDER_STRIPE
from app.services.billing_provider import (
    BillingAccountReadiness,
    BillingProvider,
    get_billing_account_readiness,
    resolve_billing_provider_name,
)
from app.services.stripe_provider import StripeAccountReadiness


def get_creator_billing_account_readiness(
    *,
    creator: Creator,
    provider: BillingProvider,
) -> BillingAccountReadiness | None:
    if creator.resolved_billing_provider != resolve_billing_provider_name(provider=provider):
        return None

    provider_account_id = creator.resolved_billing_account_id
    if not provider_account_id:
        return None

    return get_billing_account_readiness(
        provider=provider,
        provider_account_id=provider_account_id,
    )


def creator_can_create_invoices(
    *,
    creator: Creator,
    provider: BillingProvider,
) -> bool:
    readiness = get_creator_billing_account_readiness(creator=creator, provider=provider)
    return readiness is not None and readiness.can_create_invoices


def get_creator_stripe_account_readiness(
    *,
    creator: Creator,
    provider: BillingProvider,
) -> StripeAccountReadiness | None:
    if resolve_billing_provider_name(provider=provider) != BILLING_PROVIDER_STRIPE:
        return None

    readiness = get_creator_billing_account_readiness(creator=creator, provider=provider)
    if readiness is None:
        return None
    return StripeAccountReadiness(charges_enabled=readiness.can_create_invoices)


def creator_has_billable_stripe_account(
    *,
    creator: Creator,
    provider: BillingProvider,
) -> bool:
    readiness = get_creator_stripe_account_readiness(creator=creator, provider=provider)
    return readiness is not None and readiness.charges_enabled
