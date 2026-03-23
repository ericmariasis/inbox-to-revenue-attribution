"""Explicit billing lifecycle contract for booking -> invoice -> payment transitions.

This module is the repo-visible declaration for the V1 billing lifecycle:

- A booking can produce at most one invoice.
- Booking cancellation before invoice creation closes any open blocked billing case and
  prevents later retries from creating an invoice.
- Booking cancellation after invoice creation may void the open invoice, but repeated
  cancel deliveries remain safe no-ops once the invoice is already void or non-open.
- The first invoice-eligible attempt freezes billing amount/currency and the active
  billing provider/account snapshot.
  - successful create: freeze is persisted on the invoice
  - blocked create: freeze is persisted on the blocked billing case
- Blocked billing retries reuse the frozen provider/account snapshot from the blocked
  case instead of re-reading the creator's current billing connection.
- Unmatched payment events are diagnostic only until they complete the approved
  unmatched -> reconciled path against an existing open or already-paid invoice.
"""

from dataclasses import dataclass
from typing import Literal

from app.models.billing_provider import DEFAULT_BILLING_PROVIDER
from app.models.booking import Booking


FREEZE_SOURCE_BLOCKED_CASE = "blocked_case"
FREEZE_SOURCE_CREATOR = "creator"
RECONCILE_REASON_MISSING_PROVIDER_ACCOUNT_ID = "missing_provider_account_id"

BillingAccountFreezeSource = Literal["blocked_case", "creator"]


@dataclass(frozen=True)
class BillingAccountFreeze:
    payment_provider: str
    provider_account_id: str | None
    source: BillingAccountFreezeSource


def resolve_billing_account_freeze(*, booking: Booking) -> BillingAccountFreeze:
    blocked_case = booking.blocked_billing_case
    if blocked_case is not None and blocked_case.status == "open":
        return BillingAccountFreeze(
            payment_provider=blocked_case.resolved_payment_provider,
            provider_account_id=blocked_case.resolved_provider_account_id,
            source=FREEZE_SOURCE_BLOCKED_CASE,
        )

    creator = booking.creator
    return BillingAccountFreeze(
        payment_provider=creator.resolved_billing_provider or DEFAULT_BILLING_PROVIDER,
        provider_account_id=creator.resolved_billing_account_id,
        source=FREEZE_SOURCE_CREATOR,
    )
