from app.models.creator import Creator
from app.services.stripe_provider import StripeAccountReadiness, StripeProvider


def get_creator_stripe_account_readiness(
    *,
    creator: Creator,
    provider: StripeProvider,
) -> StripeAccountReadiness | None:
    if not creator.stripe_account_id:
        return None

    return provider.get_account_readiness(stripe_account_id=creator.stripe_account_id)


def creator_has_billable_stripe_account(
    *,
    creator: Creator,
    provider: StripeProvider,
) -> bool:
    readiness = get_creator_stripe_account_readiness(creator=creator, provider=provider)
    return readiness is not None and readiness.charges_enabled
