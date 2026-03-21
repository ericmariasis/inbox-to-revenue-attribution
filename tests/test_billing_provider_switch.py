import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models.booking import Booking
from app.models.booking_link import BookingLink
from app.models.content import Content
from app.models.creator import Creator
from app.models.invoice import Invoice
from app.models.billing_provider_switch_attempt import BillingProviderSwitchAttempt
from app.services.billing_provider import BillingAccountReadiness
from app.services.billing_provider_switch import (
    BILLING_PROVIDER_SWITCH_REASON_SWITCH_NOT_CLEAN,
    BillingProviderSwitchError,
    cancel_billing_provider_switch_attempt,
    commit_billing_provider_switch_attempt,
    ensure_billing_provider_switch_attempt,
    get_billing_provider_switch_attempt,
    record_billing_provider_switch_target_connection,
    restart_billing_provider_switch_attempt,
)


class _ReadyProvider:
    def __init__(self, *, provider_name: str, can_create_invoices: bool):
        self.billing_provider_name = provider_name
        self.can_create_invoices = can_create_invoices
        self.readiness_calls: list[str] = []

    def get_billing_account_readiness(self, *, provider_account_id: str) -> BillingAccountReadiness:
        self.readiness_calls.append(provider_account_id)
        return BillingAccountReadiness(can_create_invoices=self.can_create_invoices)


def _engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def _persist_creator(
    session: Session,
    *,
    billing_provider: str = "stripe",
    billing_account_id: str | None = None,
    billing_connected_at: datetime | None = None,
    stripe_account_id: str | None = None,
    stripe_connected_at: datetime | None = None,
) -> Creator:
    creator = Creator(
        name="Switch Creator",
        billing_provider=billing_provider,
        billing_connect_status="connected" if billing_account_id or stripe_account_id else "pending",
        billing_account_id=billing_account_id,
        billing_connected_at=billing_connected_at,
        stripe_connect_status="connected" if stripe_account_id else "pending",
        stripe_account_id=stripe_account_id,
        stripe_connected_at=stripe_connected_at,
    )
    session.add(creator)
    session.flush()
    return creator


def _persist_open_invoice(session: Session, *, creator: Creator) -> None:
    booking_link = BookingLink(
        creator_id=creator.id,
        name="Switch Call",
        calendly_url="https://calendly.com/example/switch-call",
        billing_amount_cents=15000,
        billing_currency="USD",
    )
    session.add(booking_link)
    session.flush()

    content = Content(
        creator_id=creator.id,
        booking_link_id=booking_link.id,
        source_url="https://example.com/posts/switch-call",
        tid="switch_tid",
    )
    session.add(content)
    session.flush()

    booking = Booking(
        creator_id=creator.id,
        booking_link_id=booking_link.id,
        tid=content.tid,
        provider="calendly",
        provider_booking_id="BOOK_switch_open",
        calendly_booking_uuid="BOOK_switch_open",
        email="switch@example.com",
        status="created",
        booked_at=datetime(2026, 3, 21, 14, 0, tzinfo=UTC),
    )
    session.add(booking)
    session.flush()

    session.add(
        Invoice(
            creator_id=creator.id,
            booking_id=booking.id,
            tid=content.tid,
            payment_provider=creator.resolved_billing_provider,
            provider_account_id=creator.resolved_billing_account_id,
            provider_invoice_id="in_switch_open",
            stripe_account_id=creator.stripe_account_id,
            stripe_invoice_id="in_switch_open" if creator.resolved_billing_provider == "stripe" else None,
            amount_cents=15000,
            currency="USD",
            status="open",
            issued_at=datetime(2026, 3, 21, 14, 5, tzinfo=UTC),
        )
    )


def test_ensure_switch_attempt_blocks_unclean_creator_with_open_invoice():
    engine = _engine()

    with Session(engine) as session:
        creator = _persist_creator(
            session,
            billing_provider="stripe",
            stripe_account_id="acct_switch_unclean",
            stripe_connected_at=datetime(2026, 3, 21, 13, 0, tzinfo=UTC),
        )
        _persist_open_invoice(session, creator=creator)
        session.commit()

    with Session(engine) as session:
        creator = session.scalar(select(Creator).where(Creator.stripe_account_id == "acct_switch_unclean"))
        assert creator is not None
        with pytest.raises(BillingProviderSwitchError) as exc_info:
            ensure_billing_provider_switch_attempt(
                db=session,
                creator=creator,
                target_provider="paypal",
            )

    assert exc_info.value.reason_code == BILLING_PROVIDER_SWITCH_REASON_SWITCH_NOT_CLEAN


def test_restart_switch_attempt_replaces_pending_paypal_tracking_id():
    engine = _engine()

    with Session(engine) as session:
        creator = _persist_creator(
            session,
            billing_provider="stripe",
            stripe_account_id="acct_switch_restart",
            stripe_connected_at=datetime(2026, 3, 21, 13, 30, tzinfo=UTC),
        )
        session.commit()
        creator_id = creator.id

    with Session(engine) as session:
        creator = session.get(Creator, creator_id)
        assert creator is not None
        first_attempt = ensure_billing_provider_switch_attempt(
            db=session,
            creator=creator,
            target_provider="paypal",
        )
        session.commit()
        first_attempt_id = first_attempt.id
        first_tracking_id = first_attempt.target_billing_provider_correlation_id

    with Session(engine) as session:
        creator = session.get(Creator, creator_id)
        assert creator is not None
        restarted_attempt = restart_billing_provider_switch_attempt(
            db=session,
            creator=creator,
            target_provider="paypal",
        )
        session.commit()
        restarted_attempt_id = restarted_attempt.id
        restarted_tracking_id = restarted_attempt.target_billing_provider_correlation_id

    assert restarted_attempt_id != first_attempt_id
    assert restarted_tracking_id != first_tracking_id
    with Session(engine) as session:
        attempts = session.scalars(select(BillingProviderSwitchAttempt)).all()
    assert len(attempts) == 1
    assert attempts[0].id == restarted_attempt_id


def test_commit_switch_attempt_promotes_ready_paypal_target_and_clears_attempt():
    engine = _engine()
    stripe_connected_at = datetime(2026, 3, 21, 15, 0, tzinfo=UTC)
    paypal_connected_at = datetime(2026, 3, 21, 15, 15, tzinfo=UTC)

    with Session(engine) as session:
        creator = _persist_creator(
            session,
            billing_provider="stripe",
            stripe_account_id="acct_switch_commit",
            stripe_connected_at=stripe_connected_at,
        )
        creator_id = creator.id
        attempt = ensure_billing_provider_switch_attempt(
            db=session,
            creator=creator,
            target_provider="paypal",
        )
        record_billing_provider_switch_target_connection(
            db=session,
            creator=creator,
            switch_attempt_id=attempt.id,
            target_provider="paypal",
            target_account_id="merchant_switch_commit",
            connected_at=paypal_connected_at,
            target_provider_correlation_id=attempt.target_billing_provider_correlation_id,
        )
        session.commit()

    provider = _ReadyProvider(provider_name="paypal", can_create_invoices=True)
    with Session(engine) as session:
        creator = session.get(Creator, creator_id)
        assert creator is not None
        committed_attempt = commit_billing_provider_switch_attempt(
            db=session,
            creator=creator,
            providers={"paypal": provider},
        )
        session.commit()

    assert committed_attempt.target_billing_provider == "paypal"
    assert provider.readiness_calls == ["merchant_switch_commit"]
    with Session(engine) as session:
        creator = session.get(Creator, creator_id)
        attempt = get_billing_provider_switch_attempt(db=session, creator_id=creator_id)
        assert creator is not None
        assert creator.billing_provider == "paypal"
        assert creator.billing_account_id == "merchant_switch_commit"
        assert creator.billing_provider_correlation_id == committed_attempt.target_billing_provider_correlation_id
        assert creator.stripe_account_id == "acct_switch_commit"
        assert creator.stripe_connected_at == stripe_connected_at
        assert attempt is None


def test_cancel_switch_attempt_clears_pending_connected_target_without_switching_active_provider():
    engine = _engine()
    paypal_connected_at = datetime(2026, 3, 21, 16, 0, tzinfo=UTC)
    stripe_connected_at = datetime(2026, 3, 21, 16, 15, tzinfo=UTC)

    with Session(engine) as session:
        creator = _persist_creator(
            session,
            billing_provider="paypal",
            billing_account_id="merchant_switch_source",
            billing_connected_at=paypal_connected_at,
        )
        creator_id = creator.id
        attempt = ensure_billing_provider_switch_attempt(
            db=session,
            creator=creator,
            target_provider="stripe",
        )
        record_billing_provider_switch_target_connection(
            db=session,
            creator=creator,
            switch_attempt_id=attempt.id,
            target_provider="stripe",
            target_account_id="acct_switch_target",
            connected_at=stripe_connected_at,
        )
        session.commit()

    with Session(engine) as session:
        canceled = cancel_billing_provider_switch_attempt(
            db=session,
            creator_id=creator_id,
        )
        session.commit()

    assert canceled is True
    with Session(engine) as session:
        creator = session.get(Creator, creator_id)
        attempt = get_billing_provider_switch_attempt(db=session, creator_id=creator_id)
        assert creator is not None
        assert creator.billing_provider == "paypal"
        assert creator.billing_account_id == "merchant_switch_source"
        assert attempt is None
