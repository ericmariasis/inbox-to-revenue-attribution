from datetime import datetime, timezone

from app.services.billing_provider import BillingAccountReadiness
from app.models.billing_provider import BILLING_PROVIDER_PAYPAL, BILLING_PROVIDER_STRIPE
from app.models.creator import Creator
from app.services.stripe_account_readiness import (
    creator_can_create_invoices,
    creator_has_billable_stripe_account,
    get_creator_billing_account_readiness,
    get_creator_stripe_account_readiness,
)
from app.services.stripe_provider import StripeAccountReadiness


class _StubStripeProvider:
    billing_provider_name = BILLING_PROVIDER_STRIPE

    def __init__(self, *, readiness: StripeAccountReadiness):
        self.readiness = readiness
        self.readiness_calls: list[str] = []

    def build_connect_onboarding_url(self, *, creator_id: str, state: str) -> str:
        raise AssertionError(f"unexpected onboarding call creator_id={creator_id} state={state}")

    def exchange_connect_callback(self, *, code: str, state: str) -> str:
        raise AssertionError(f"unexpected callback exchange code={code} state={state}")

    def get_account_readiness(self, *, stripe_account_id: str) -> StripeAccountReadiness:
        self.readiness_calls.append(stripe_account_id)
        return self.readiness


class _StubPayPalProvider:
    billing_provider_name = BILLING_PROVIDER_PAYPAL

    def __init__(self, *, readiness: BillingAccountReadiness):
        self.readiness = readiness
        self.readiness_calls: list[str] = []

    def get_billing_account_readiness(
        self,
        *,
        provider_account_id: str,
    ) -> BillingAccountReadiness:
        self.readiness_calls.append(provider_account_id)
        return self.readiness


def test_get_creator_billing_account_readiness_returns_none_without_stored_account_id():
    creator = Creator(name="PP4 Pending Creator", stripe_connect_status="pending")
    provider = _StubStripeProvider(readiness=StripeAccountReadiness(charges_enabled=True))

    readiness = get_creator_billing_account_readiness(creator=creator, provider=provider)

    assert readiness is None
    assert creator_can_create_invoices(creator=creator, provider=provider) is False
    assert provider.readiness_calls == []


def test_get_creator_billing_account_readiness_uses_active_billing_identity_fields():
    creator = Creator(
        name="PP4 Stripe Billing Identity Creator",
        billing_provider=BILLING_PROVIDER_STRIPE,
        billing_connect_status="connected",
        billing_account_id="acct_pp4_lookup",
    )
    provider = _StubStripeProvider(readiness=StripeAccountReadiness(charges_enabled=True))

    readiness = get_creator_billing_account_readiness(creator=creator, provider=provider)

    assert readiness == BillingAccountReadiness(can_create_invoices=True)
    assert provider.readiness_calls == ["acct_pp4_lookup"]
    assert creator_can_create_invoices(creator=creator, provider=provider) is True
    assert provider.readiness_calls == ["acct_pp4_lookup", "acct_pp4_lookup"]


def test_get_creator_billing_account_readiness_supports_paypal_provider_identity_fields():
    creator = Creator(
        name="PP9 PayPal Billing Identity Creator",
        billing_provider=BILLING_PROVIDER_PAYPAL,
        billing_connect_status="connected",
        billing_account_id="merchant_pp9_lookup",
    )
    provider = _StubPayPalProvider(
        readiness=BillingAccountReadiness(can_create_invoices=False)
    )

    readiness = get_creator_billing_account_readiness(creator=creator, provider=provider)

    assert readiness == BillingAccountReadiness(can_create_invoices=False)
    assert provider.readiness_calls == ["merchant_pp9_lookup"]
    assert creator_can_create_invoices(creator=creator, provider=provider) is False
    assert provider.readiness_calls == ["merchant_pp9_lookup", "merchant_pp9_lookup"]


def test_get_creator_stripe_account_readiness_returns_none_without_stored_account_id():
    creator = Creator(name="Story 28 Pending Creator", stripe_connect_status="pending")
    provider = _StubStripeProvider(readiness=StripeAccountReadiness(charges_enabled=True))

    readiness = get_creator_stripe_account_readiness(creator=creator, provider=provider)

    assert readiness is None
    assert creator_has_billable_stripe_account(creator=creator, provider=provider) is False
    assert provider.readiness_calls == []


def test_get_creator_stripe_account_readiness_uses_provider_abstraction():
    creator = Creator(
        name="Story 28 Connected Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_story28_lookup",
    )
    provider = _StubStripeProvider(readiness=StripeAccountReadiness(charges_enabled=False))

    readiness = get_creator_stripe_account_readiness(creator=creator, provider=provider)

    assert readiness == StripeAccountReadiness(charges_enabled=False)
    assert provider.readiness_calls == ["acct_story28_lookup"]


def test_get_creator_stripe_account_readiness_uses_active_billing_provider_identity_fields():
    creator = Creator(
        name="PP1 Stripe Billing Identity Creator",
        billing_provider=BILLING_PROVIDER_STRIPE,
        billing_connect_status="connected",
        billing_account_id="acct_pp1_lookup",
    )
    provider = _StubStripeProvider(readiness=StripeAccountReadiness(charges_enabled=True))

    readiness = get_creator_stripe_account_readiness(creator=creator, provider=provider)

    assert readiness == StripeAccountReadiness(charges_enabled=True)
    assert provider.readiness_calls == ["acct_pp1_lookup"]


def test_get_creator_stripe_account_readiness_returns_none_when_active_provider_is_not_stripe():
    creator = Creator(
        name="PP1 PayPal Active Provider Creator",
        billing_provider=BILLING_PROVIDER_PAYPAL,
        billing_connect_status="connected",
        billing_account_id="merchant_pp1_paypal",
        stripe_connect_status="connected",
        stripe_account_id="acct_legacy_stripe_should_not_apply",
    )
    provider = _StubStripeProvider(readiness=StripeAccountReadiness(charges_enabled=True))

    readiness = get_creator_stripe_account_readiness(creator=creator, provider=provider)

    assert readiness is None
    assert creator_has_billable_stripe_account(creator=creator, provider=provider) is False
    assert provider.readiness_calls == []


def test_creator_has_billable_stripe_account_returns_false_when_charges_disabled():
    connected_at = datetime(2026, 3, 6, 15, 30, tzinfo=timezone.utc)
    creator = Creator(
        name="Story 28 Not Ready Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_story28_not_ready",
        stripe_connected_at=connected_at,
    )
    provider = _StubStripeProvider(readiness=StripeAccountReadiness(charges_enabled=False))

    is_billable = creator_has_billable_stripe_account(creator=creator, provider=provider)

    assert is_billable is False
    assert provider.readiness_calls == ["acct_story28_not_ready"]
    assert creator.stripe_connect_status == "connected"
    assert creator.stripe_account_id == "acct_story28_not_ready"
    assert creator.stripe_connected_at == connected_at


def test_creator_has_billable_stripe_account_returns_true_when_charges_enabled():
    connected_at = datetime(2026, 3, 6, 15, 45, tzinfo=timezone.utc)
    creator = Creator(
        name="Story 28 Billable Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_story28_billable",
        stripe_connected_at=connected_at,
    )
    provider = _StubStripeProvider(readiness=StripeAccountReadiness(charges_enabled=True))

    is_billable = creator_has_billable_stripe_account(creator=creator, provider=provider)

    assert is_billable is True
    assert provider.readiness_calls == ["acct_story28_billable"]
    assert creator.stripe_connect_status == "connected"
    assert creator.stripe_account_id == "acct_story28_billable"
    assert creator.stripe_connected_at == connected_at
