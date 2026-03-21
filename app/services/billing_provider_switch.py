from dataclasses import dataclass
from datetime import datetime
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.billing_provider import (
    BILLING_CONNECT_STATUS_CONNECTED,
    BILLING_CONNECT_STATUS_PENDING,
    BILLING_PROVIDER_PAYPAL,
    BILLING_PROVIDER_STRIPE,
)
from app.models.billing_provider_switch_attempt import BillingProviderSwitchAttempt
from app.models.blocked_billing_case import BlockedBillingCase
from app.models.creator import Creator
from app.models.invoice import Invoice
from app.services.billing_provider import (
    BillingProviderError,
    BillingProviderRegistry,
    BillingProviderResolutionError,
    get_billing_account_readiness,
    resolve_billing_provider,
)
from app.services.paypal_connect import build_paypal_tracking_id


BILLING_PROVIDER_SWITCH_REASON_ALREADY_ACTIVE_PROVIDER = "already_active_provider"
BILLING_PROVIDER_SWITCH_REASON_SWITCH_REQUIRES_CONNECTED_PROVIDER = "switch_requires_connected_provider"
BILLING_PROVIDER_SWITCH_REASON_SWITCH_NOT_CLEAN = "switch_not_clean"
BILLING_PROVIDER_SWITCH_REASON_SWITCH_ATTEMPT_MISSING = "switch_attempt_missing"
BILLING_PROVIDER_SWITCH_REASON_SWITCH_TARGET_ALREADY_CONNECTED = "switch_target_already_connected"
BILLING_PROVIDER_SWITCH_REASON_SWITCH_TARGET_NOT_CONNECTED = "switch_target_not_connected"
BILLING_PROVIDER_SWITCH_REASON_SWITCH_TARGET_NOT_READY = "switch_target_not_ready"
BILLING_PROVIDER_SWITCH_REASON_SWITCH_PROVIDER_NOT_CONFIGURED = "switch_provider_not_configured"


class BillingProviderSwitchError(ValueError):
    def __init__(self, *, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True)
class BillingProviderSwitchCleanState:
    open_invoice_count: int
    blocked_billing_count: int

    @property
    def is_clean(self) -> bool:
        return self.open_invoice_count == 0 and self.blocked_billing_count == 0


def replacement_billing_provider_name(*, current_provider: str | None) -> str:
    normalized_provider = (current_provider or BILLING_PROVIDER_STRIPE).strip().lower()
    if normalized_provider == BILLING_PROVIDER_PAYPAL:
        return BILLING_PROVIDER_STRIPE
    return BILLING_PROVIDER_PAYPAL


def get_billing_provider_switch_attempt(
    *,
    db: Session,
    creator_id: uuid.UUID,
) -> BillingProviderSwitchAttempt | None:
    return db.scalar(
        select(BillingProviderSwitchAttempt).where(
            BillingProviderSwitchAttempt.creator_id == creator_id
        )
    )


def get_billing_provider_switch_attempt_by_id(
    *,
    db: Session,
    creator_id: uuid.UUID,
    switch_attempt_id: uuid.UUID,
) -> BillingProviderSwitchAttempt | None:
    attempt = db.get(BillingProviderSwitchAttempt, switch_attempt_id)
    if attempt is None or attempt.creator_id != creator_id:
        return None
    return attempt


def get_billing_provider_switch_clean_state(
    *,
    db: Session,
    creator_id: uuid.UUID,
) -> BillingProviderSwitchCleanState:
    open_invoice_count = db.scalar(
        select(func.count())
        .select_from(Invoice)
        .where(
            Invoice.creator_id == creator_id,
            Invoice.status == "open",
        )
    )
    blocked_billing_count = db.scalar(
        select(func.count())
        .select_from(BlockedBillingCase)
        .where(
            BlockedBillingCase.creator_id == creator_id,
            BlockedBillingCase.status == "open",
        )
    )
    return BillingProviderSwitchCleanState(
        open_invoice_count=int(open_invoice_count or 0),
        blocked_billing_count=int(blocked_billing_count or 0),
    )


def ensure_billing_provider_switch_attempt(
    *,
    db: Session,
    creator: Creator,
    target_provider: str,
) -> BillingProviderSwitchAttempt:
    current_provider = creator.resolved_billing_provider
    normalized_target_provider = target_provider.strip().lower()
    if creator.resolved_billing_connect_status != BILLING_CONNECT_STATUS_CONNECTED:
        raise BillingProviderSwitchError(
            reason_code=BILLING_PROVIDER_SWITCH_REASON_SWITCH_REQUIRES_CONNECTED_PROVIDER
        )
    if current_provider == normalized_target_provider:
        raise BillingProviderSwitchError(
            reason_code=BILLING_PROVIDER_SWITCH_REASON_ALREADY_ACTIVE_PROVIDER
        )

    clean_state = get_billing_provider_switch_clean_state(db=db, creator_id=creator.id)
    if not clean_state.is_clean:
        raise BillingProviderSwitchError(
            reason_code=BILLING_PROVIDER_SWITCH_REASON_SWITCH_NOT_CLEAN
        )

    attempt = get_billing_provider_switch_attempt(db=db, creator_id=creator.id)
    if (
        attempt is not None
        and (
            attempt.source_billing_provider != current_provider
            or attempt.target_billing_provider != normalized_target_provider
        )
    ):
        db.delete(attempt)
        db.flush()
        attempt = None

    if attempt is None:
        attempt = BillingProviderSwitchAttempt(
            creator_id=creator.id,
            source_billing_provider=current_provider,
            target_billing_provider=normalized_target_provider,
        )
        if normalized_target_provider == BILLING_PROVIDER_PAYPAL:
            attempt.target_billing_provider_correlation_id = build_paypal_tracking_id()
        db.add(attempt)
        db.flush()

    if (
        normalized_target_provider == BILLING_PROVIDER_PAYPAL
        and not attempt.target_billing_provider_correlation_id
    ):
        attempt.target_billing_provider_correlation_id = build_paypal_tracking_id()
        db.add(attempt)
        db.flush()

    return attempt


def restart_billing_provider_switch_attempt(
    *,
    db: Session,
    creator: Creator,
    target_provider: str,
) -> BillingProviderSwitchAttempt:
    existing_attempt = get_billing_provider_switch_attempt(db=db, creator_id=creator.id)
    if existing_attempt is not None:
        db.delete(existing_attempt)
        db.flush()
    return ensure_billing_provider_switch_attempt(
        db=db,
        creator=creator,
        target_provider=target_provider,
    )


def cancel_billing_provider_switch_attempt(
    *,
    db: Session,
    creator_id: uuid.UUID,
) -> bool:
    attempt = get_billing_provider_switch_attempt(db=db, creator_id=creator_id)
    if attempt is None:
        return False
    db.delete(attempt)
    db.flush()
    return True


def record_billing_provider_switch_target_connection(
    *,
    db: Session,
    creator: Creator,
    switch_attempt_id: uuid.UUID,
    target_provider: str,
    target_account_id: str,
    connected_at: datetime,
    target_provider_correlation_id: str | None = None,
) -> BillingProviderSwitchAttempt:
    attempt = get_billing_provider_switch_attempt_by_id(
        db=db,
        creator_id=creator.id,
        switch_attempt_id=switch_attempt_id,
    )
    if attempt is None or attempt.target_billing_provider != target_provider:
        raise BillingProviderSwitchError(
            reason_code=BILLING_PROVIDER_SWITCH_REASON_SWITCH_ATTEMPT_MISSING
        )
    if creator.resolved_billing_provider != attempt.source_billing_provider:
        raise BillingProviderSwitchError(
            reason_code=BILLING_PROVIDER_SWITCH_REASON_SWITCH_ATTEMPT_MISSING
        )

    if attempt.target_billing_connect_status == BILLING_CONNECT_STATUS_CONNECTED:
        if attempt.target_billing_account_id != target_account_id:
            raise BillingProviderSwitchError(
                reason_code=BILLING_PROVIDER_SWITCH_REASON_SWITCH_TARGET_ALREADY_CONNECTED
            )
        if (
            target_provider_correlation_id is not None
            and attempt.target_billing_provider_correlation_id != target_provider_correlation_id
        ):
            raise BillingProviderSwitchError(
                reason_code=BILLING_PROVIDER_SWITCH_REASON_SWITCH_TARGET_ALREADY_CONNECTED
            )
        return attempt

    attempt.target_billing_connect_status = BILLING_CONNECT_STATUS_CONNECTED
    attempt.target_billing_account_id = target_account_id
    attempt.target_billing_connected_at = connected_at
    if target_provider_correlation_id is not None:
        attempt.target_billing_provider_correlation_id = target_provider_correlation_id
    db.add(attempt)
    db.flush()
    return attempt


def get_billing_provider_switch_target_ready(
    *,
    attempt: BillingProviderSwitchAttempt,
    providers: BillingProviderRegistry,
) -> bool | None:
    if (
        attempt.target_billing_connect_status != BILLING_CONNECT_STATUS_CONNECTED
        or attempt.target_billing_account_id is None
    ):
        return None

    try:
        provider = resolve_billing_provider(
            providers=providers,
            provider_name=attempt.target_billing_provider,
        )
        readiness = get_billing_account_readiness(
            provider=provider,
            provider_account_id=attempt.target_billing_account_id,
        )
    except (BillingProviderError, BillingProviderResolutionError):
        return False
    return readiness.can_create_invoices


def commit_billing_provider_switch_attempt(
    *,
    db: Session,
    creator: Creator,
    providers: BillingProviderRegistry,
) -> BillingProviderSwitchAttempt:
    attempt = get_billing_provider_switch_attempt(db=db, creator_id=creator.id)
    if attempt is None or creator.resolved_billing_provider != attempt.source_billing_provider:
        raise BillingProviderSwitchError(
            reason_code=BILLING_PROVIDER_SWITCH_REASON_SWITCH_ATTEMPT_MISSING
        )

    clean_state = get_billing_provider_switch_clean_state(db=db, creator_id=creator.id)
    if not clean_state.is_clean:
        raise BillingProviderSwitchError(
            reason_code=BILLING_PROVIDER_SWITCH_REASON_SWITCH_NOT_CLEAN
        )

    if (
        attempt.target_billing_connect_status != BILLING_CONNECT_STATUS_CONNECTED
        or attempt.target_billing_account_id is None
        or attempt.target_billing_connected_at is None
        or (
            attempt.target_billing_provider == BILLING_PROVIDER_PAYPAL
            and attempt.target_billing_provider_correlation_id is None
        )
    ):
        raise BillingProviderSwitchError(
            reason_code=BILLING_PROVIDER_SWITCH_REASON_SWITCH_TARGET_NOT_CONNECTED
        )

    target_ready = get_billing_provider_switch_target_ready(
        attempt=attempt,
        providers=providers,
    )
    if target_ready is None:
        raise BillingProviderSwitchError(
            reason_code=BILLING_PROVIDER_SWITCH_REASON_SWITCH_TARGET_NOT_CONNECTED
        )
    if target_ready is not True:
        raise BillingProviderSwitchError(
            reason_code=BILLING_PROVIDER_SWITCH_REASON_SWITCH_TARGET_NOT_READY
        )

    creator.billing_provider = attempt.target_billing_provider
    creator.billing_connect_status = BILLING_CONNECT_STATUS_CONNECTED
    creator.billing_account_id = attempt.target_billing_account_id
    creator.billing_connected_at = attempt.target_billing_connected_at
    if attempt.target_billing_provider == BILLING_PROVIDER_PAYPAL:
        creator.billing_provider_correlation_id = (
            attempt.target_billing_provider_correlation_id
        )
    else:
        creator.stripe_connect_status = BILLING_CONNECT_STATUS_CONNECTED
        creator.stripe_account_id = attempt.target_billing_account_id
        creator.stripe_connected_at = attempt.target_billing_connected_at

    db.add(creator)
    db.delete(attempt)
    db.flush()
    return attempt
