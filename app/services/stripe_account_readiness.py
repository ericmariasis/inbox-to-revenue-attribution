from app.models.creator import Creator
from app.models.billing_provider import BILLING_PROVIDER_STRIPE
from app.services.stripe_provider import StripeAccountReadiness, StripeProvider


def get_creator_stripe_account_readiness(
    *,
    creator: Creator,
    provider: StripeProvider,
) -> StripeAccountReadiness | None:
    if creator.resolved_billing_provider != BILLING_PROVIDER_STRIPE:
        return None

    stripe_account_id = creator.resolved_billing_account_id
    if not stripe_account_id:
        return None

    return provider.get_account_readiness(stripe_account_id=stripe_account_id)


def creator_has_billable_stripe_account(
    *,
    creator: Creator,
    provider: StripeProvider,
) -> bool:
    readiness = get_creator_stripe_account_readiness(creator=creator, provider=provider)
    return readiness is not None and readiness.charges_enabled
