import argparse
from datetime import datetime, timezone
from urllib.parse import urlencode

import uvicorn

from app.main import app
from app.services.billing_provider import (
    BILLING_ACCOUNT_READINESS_ISSUE_CONFIRM_PAYPAL_PRIMARY_EMAIL,
    BILLING_ACCOUNT_READINESS_ISSUE_ENABLE_PAYPAL_PAYMENTS_RECEIVABLE,
    BillingAccountReadiness,
    BillingProviderInvoiceCreateResult,
    BillingProviderInvoiceStopResult,
)
from app.services.paypal_provider import (
    PayPalConnectOnboardingResult,
    PayPalInvoicePaidSnapshot,
    PayPalProviderError,
    PayPalSellerStatus,
)
from app.services.stripe_provider import StripeAccountReadiness, StripeInvoiceCreateResult


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the local billing UI app with deterministic manual Stripe/PayPal stubs.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind the local app to.")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind the local app to.")
    parser.add_argument(
        "--log-level",
        default="info",
        choices=("critical", "error", "warning", "info", "debug", "trace"),
        help="Uvicorn log level.",
    )
    return parser


def _contains_profile(value: str, profile: str) -> bool:
    return profile in value.strip().lower()


def _manual_paypal_readiness(provider_account_id: str) -> BillingAccountReadiness:
    if _contains_profile(provider_account_id, "blocked"):
        raise PayPalProviderError(
            "paypal merchant status lookup failed",
            operation="paypal_merchant_status",
            http_status=500,
            error_code="MANUAL_HARNESS_BLOCKED",
        )
    if _contains_profile(provider_account_id, "not_ready"):
        return BillingAccountReadiness(
            can_create_invoices=False,
            creator_actionable_issue_codes=(
                BILLING_ACCOUNT_READINESS_ISSUE_CONFIRM_PAYPAL_PRIMARY_EMAIL,
                BILLING_ACCOUNT_READINESS_ISSUE_ENABLE_PAYPAL_PAYMENTS_RECEIVABLE,
            ),
        )
    return BillingAccountReadiness(can_create_invoices=True)


def _manual_stripe_readiness(stripe_account_id: str) -> StripeAccountReadiness:
    return StripeAccountReadiness(
        charges_enabled=not _contains_profile(stripe_account_id, "not_ready")
    )


class ManualStripeProvider:
    billing_provider_name = "stripe"

    def build_connect_onboarding_url(self, *, creator_id: str, state: str) -> str:
        return (
            "https://connect.stripe.com/oauth/authorize?"
            + urlencode(
                {
                    "response_type": "code",
                    "client_id": "ca_manual_browser",
                    "scope": "read_write",
                    "state": state,
                    "creator_id": creator_id,
                }
            )
        )

    def exchange_connect_callback(self, *, code: str, state: str) -> str:
        del code
        return f"acct_manual_{state[-12:]}"

    def get_account_readiness(self, *, stripe_account_id: str) -> StripeAccountReadiness:
        return _manual_stripe_readiness(stripe_account_id)

    def get_billing_account_readiness(
        self,
        *,
        provider_account_id: str,
    ) -> BillingAccountReadiness:
        readiness = self.get_account_readiness(stripe_account_id=provider_account_id)
        return BillingAccountReadiness(
            can_create_invoices=readiness.charges_enabled,
            creator_actionable_issue_codes=(() if readiness.charges_enabled else ("complete_stripe_setup",)),
        )

    def create_invoice(
        self,
        *,
        stripe_account_id: str,
        amount_cents: int,
        currency: str,
        metadata: dict[str, str],
        idempotency_key: str,
    ) -> StripeInvoiceCreateResult:
        del stripe_account_id, amount_cents, currency, metadata
        return StripeInvoiceCreateResult(
            stripe_invoice_id=f"in_manual_{idempotency_key[-12:]}",
            status="open",
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
        invoice = self.create_invoice(
            stripe_account_id=provider_account_id,
            amount_cents=amount_cents,
            currency=currency,
            metadata=metadata,
            idempotency_key=idempotency_key,
        )
        return BillingProviderInvoiceCreateResult(
            provider_account_id=provider_account_id,
            provider_invoice_id=invoice.stripe_invoice_id,
            invoice_status=invoice.status,
        )

    def void_invoice(self, *, stripe_account_id: str, stripe_invoice_id: str) -> None:
        del stripe_account_id, stripe_invoice_id

    def stop_billing_invoice(
        self,
        *,
        provider_account_id: str,
        provider_invoice_id: str,
    ) -> BillingProviderInvoiceStopResult:
        self.void_invoice(
            stripe_account_id=provider_account_id,
            stripe_invoice_id=provider_invoice_id,
        )
        return BillingProviderInvoiceStopResult(
            provider_account_id=provider_account_id,
            provider_invoice_id=provider_invoice_id,
            invoice_status="void",
        )


class ManualPayPalProvider:
    billing_provider_name = "paypal"

    def create_connect_onboarding(
        self,
        *,
        tracking_id: str,
        return_url: str,
    ) -> PayPalConnectOnboardingResult:
        return PayPalConnectOnboardingResult(
            onboarding_url=(
                "https://www.sandbox.paypal.com/bizsignup/partner/entry?"
                + urlencode({"tracking_id": tracking_id, "return_url": return_url})
            ),
            tracking_id=tracking_id,
        )

    def get_verified_seller_status(self, *, tracking_id: str) -> PayPalSellerStatus:
        suffix = tracking_id[-12:]
        return PayPalSellerStatus(
            merchant_id=f"merchant_manual_{suffix}",
            tracking_id=tracking_id,
            payments_receivable=True,
            primary_email_confirmed=True,
        )

    def get_billing_account_readiness(
        self,
        *,
        provider_account_id: str,
    ) -> BillingAccountReadiness:
        return _manual_paypal_readiness(provider_account_id)

    def verify_webhook_event(
        self,
        *,
        webhook_id: str,
        auth_algo: str,
        cert_url: str,
        transmission_id: str,
        transmission_sig: str,
        transmission_time: str,
        webhook_event,
    ) -> bool:
        del (
            webhook_id,
            auth_algo,
            cert_url,
            transmission_id,
            transmission_sig,
            transmission_time,
            webhook_event,
        )
        return True

    def get_invoice_paid_snapshot(
        self,
        *,
        provider_account_id: str,
        provider_invoice_id: str,
    ) -> PayPalInvoicePaidSnapshot:
        del provider_account_id
        return PayPalInvoicePaidSnapshot(
            invoice_id=provider_invoice_id,
            status="PAID",
            payment_type="PAYPAL",
            payment_method="PAYPAL",
            transaction_status="SUCCESS",
            paid_at=datetime.now(timezone.utc),
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
        del amount_cents, currency, metadata
        return BillingProviderInvoiceCreateResult(
            provider_account_id=provider_account_id,
            provider_invoice_id=f"paypal-inv-{idempotency_key[-12:]}",
            invoice_status="open",
        )

    def stop_billing_invoice(
        self,
        *,
        provider_account_id: str,
        provider_invoice_id: str,
    ) -> BillingProviderInvoiceStopResult:
        return BillingProviderInvoiceStopResult(
            provider_account_id=provider_account_id,
            provider_invoice_id=provider_invoice_id,
            invoice_status="void",
        )


def main() -> int:
    args = _parser().parse_args()

    app.state.stripe_provider = ManualStripeProvider()
    app.state.paypal_provider = ManualPayPalProvider()

    print("manual_billing_ui_harness=active")
    print("paypal_profile_rule=account ids containing 'not_ready' show actionable setup guidance")
    print("paypal_profile_rule_blocked=account ids containing 'blocked' collapse into blocked guidance")
    print("stripe_profile_rule=account ids containing 'not_ready' report Stripe as not ready")
    print(f"browser_base_url=http://{args.host}:{args.port}")

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
