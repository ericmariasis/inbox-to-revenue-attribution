import os
from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models.booking import Booking
from app.models.booking_link import BookingLink
from app.models.blocked_billing_case import BlockedBillingCase
from app.models.billing_provider_switch_attempt import BillingProviderSwitchAttempt
from app.models.content import Content
from app.models.creator import Creator
from app.models.invoice import Invoice
from app.services.booking_attribution import (
    BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED,
    BOOKING_UNATTRIBUTED_REASON_MISSING_TID,
)
from app.services.billing import BillingOrchestrator
from app.services.billing_provider import (
    BillingAccountReadiness,
    BillingProviderInvoiceCreateResult,
    BillingProviderInvoiceStopResult,
)
from app.services.blocked_billing import BlockedBillingRetryService
from app.services.stripe_provider import (
    StripeAccountReadiness,
    StripeInvoiceCreateResult,
    StripeProviderError,
)


class _StubStripeProvider:
    billing_provider_name = "stripe"

    def __init__(
        self,
        *,
        readiness: StripeAccountReadiness,
        created_invoice_id: str = "in_story44_created",
        created_invoice_status: str = "open",
        readiness_error: StripeProviderError | None = None,
        create_error: StripeProviderError | None = None,
        void_error: StripeProviderError | None = None,
    ):
        self._readiness = readiness
        self._created_invoice_id = created_invoice_id
        self._created_invoice_status = created_invoice_status
        self._readiness_error = readiness_error
        self._create_error = create_error
        self._void_error = void_error
        self.readiness_calls: list[str] = []
        self.create_calls: list[dict[str, object]] = []
        self.void_calls: list[dict[str, str]] = []

    def build_connect_onboarding_url(self, *, creator_id: str, state: str) -> str:
        raise AssertionError(f"unexpected onboarding call creator_id={creator_id} state={state}")

    def exchange_connect_callback(self, *, code: str, state: str) -> str:
        raise AssertionError(f"unexpected callback exchange code={code} state={state}")

    def get_account_readiness(self, *, stripe_account_id: str) -> StripeAccountReadiness:
        self.readiness_calls.append(stripe_account_id)
        if self._readiness_error is not None:
            raise self._readiness_error
        return self._readiness

    def create_invoice(
        self,
        *,
        stripe_account_id: str,
        amount_cents: int,
        currency: str,
        metadata: dict[str, str],
        idempotency_key: str,
    ) -> StripeInvoiceCreateResult:
        self.create_calls.append(
            {
                "stripe_account_id": stripe_account_id,
                "amount_cents": amount_cents,
                "currency": currency,
                "metadata": metadata,
                "idempotency_key": idempotency_key,
            }
        )
        if self._create_error is not None:
            raise self._create_error
        return StripeInvoiceCreateResult(
            stripe_invoice_id=self._created_invoice_id,
            status=self._created_invoice_status,
        )

    def void_invoice(self, *, stripe_account_id: str, stripe_invoice_id: str) -> None:
        self.void_calls.append(
            {
                "stripe_account_id": stripe_account_id,
                "stripe_invoice_id": stripe_invoice_id,
            }
        )
        if self._void_error is not None:
            raise self._void_error


class _StubPayPalProvider:
    billing_provider_name = "paypal"

    def __init__(
        self,
        *,
        readiness: BillingAccountReadiness,
        created_invoice_id: str = "INV2_story44_created",
        created_invoice_status: str = "open",
        stop_invoice_status: str = "void",
        readiness_error: StripeProviderError | None = None,
        create_error: StripeProviderError | None = None,
        stop_error: StripeProviderError | None = None,
    ):
        self._readiness = readiness
        self._created_invoice_id = created_invoice_id
        self._created_invoice_status = created_invoice_status
        self._stop_invoice_status = stop_invoice_status
        self._readiness_error = readiness_error
        self._create_error = create_error
        self._stop_error = stop_error
        self.readiness_calls: list[str] = []
        self.create_calls: list[dict[str, object]] = []
        self.stop_calls: list[dict[str, str]] = []

    def get_billing_account_readiness(
        self,
        *,
        provider_account_id: str,
    ) -> BillingAccountReadiness:
        self.readiness_calls.append(provider_account_id)
        if self._readiness_error is not None:
            raise self._readiness_error
        return self._readiness

    def create_billing_invoice(
        self,
        *,
        provider_account_id: str,
        amount_cents: int,
        currency: str,
        metadata: dict[str, str],
        idempotency_key: str,
    ) -> BillingProviderInvoiceCreateResult:
        self.create_calls.append(
            {
                "provider_account_id": provider_account_id,
                "amount_cents": amount_cents,
                "currency": currency,
                "metadata": metadata,
                "idempotency_key": idempotency_key,
            }
        )
        if self._create_error is not None:
            raise self._create_error
        return BillingProviderInvoiceCreateResult(
            provider_account_id=provider_account_id,
            provider_invoice_id=self._created_invoice_id,
            invoice_status=self._created_invoice_status,
        )

    def stop_billing_invoice(
        self,
        *,
        provider_account_id: str,
        provider_invoice_id: str,
    ) -> BillingProviderInvoiceStopResult:
        self.stop_calls.append(
            {
                "provider_account_id": provider_account_id,
                "provider_invoice_id": provider_invoice_id,
            }
        )
        if self._stop_error is not None:
            raise self._stop_error
        return BillingProviderInvoiceStopResult(
            provider_account_id=provider_account_id,
            provider_invoice_id=provider_invoice_id,
            invoice_status=self._stop_invoice_status,
        )


def _persist_booking_graph(
    session: Session,
    *,
    booking_uuid: str = "BOOK_story44_primary",
    tid: str = "story44_tid",
    stripe_account_id: str | None = "acct_story44_billable",
    billing_provider: str = "stripe",
    billing_account_id: str | None = None,
    billing_connect_status: str | None = None,
    billing_amount_cents: int | None = 15000,
    billing_currency: str | None = "USD",
    provider: str = "calendly",
) -> tuple[Creator, BookingLink, Content, Booking]:
    creator = Creator(
        name="Story 44 Creator",
        billing_provider=billing_provider,
        billing_connect_status=(
            billing_connect_status
            or ("connected" if (billing_account_id or stripe_account_id) else "pending")
        ),
        billing_account_id=billing_account_id,
        stripe_connect_status="connected" if stripe_account_id else "pending",
        stripe_account_id=stripe_account_id,
    )
    session.add(creator)
    session.flush()

    if provider == "fullscope":
        booking_link = BookingLink(
            creator_id=creator.id,
            name="Story 44 Booking Link",
            provider="fullscope",
            destination_url="https://links.fullscope.tools/widget/bookings/story44-call",
            billing_amount_cents=billing_amount_cents,
            billing_currency=billing_currency,
        )
    else:
        booking_link = BookingLink(
            creator_id=creator.id,
            name="Story 44 Booking Link",
            calendly_url="https://calendly.com/example/story44-call",
            billing_amount_cents=billing_amount_cents,
            billing_currency=billing_currency,
        )
    session.add(booking_link)
    session.flush()

    content = Content(
        creator_id=creator.id,
        booking_link_id=booking_link.id,
        source_url="https://example.com/story44-content",
        tid=tid,
    )
    session.add(content)
    session.flush()

    booking = Booking(
        creator_id=creator.id,
        booking_link_id=booking_link.id,
        tid=content.tid,
        provider=provider,
        provider_booking_id=booking_uuid,
        calendly_booking_uuid=booking_uuid if provider == "calendly" else None,
        email="story44-booked@example.com",
        status="created",
        booked_at=datetime(2026, 3, 8, 18, 0, tzinfo=UTC),
    )
    session.add(booking)
    session.flush()

    return creator, booking_link, content, booking


def _invoice_rows() -> list[Invoice]:
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    with Session(engine) as session:
        return session.scalars(select(Invoice)).all()


def _blocked_case_rows() -> list[BlockedBillingCase]:
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    with Session(engine) as session:
        return session.scalars(select(BlockedBillingCase)).all()


def _booking_rows() -> list[Booking]:
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    with Session(engine) as session:
        return session.scalars(select(Booking)).all()


def test_billing_orchestrator_persists_open_invoice_from_trusted_booking_data():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    issued_at = datetime(2026, 3, 8, 18, 5, tzinfo=UTC)
    provider = _StubStripeProvider(
        readiness=StripeAccountReadiness(charges_enabled=True),
        created_invoice_id="in_story44_primary",
    )

    with Session(engine) as session:
        creator, _, content, booking = _persist_booking_graph(session)
        creator_id = creator.id
        booking_id = booking.id
        content_tid = content.tid
        session.commit()
        orchestrator = BillingOrchestrator(
            session_factory=lambda: Session(engine),
            provider=provider,
            now_fn=lambda: issued_at,
        )

        result = orchestrator.create_invoice_for_booking(booking_id=booking_id)

    invoices = _invoice_rows()

    assert result.outcome == "created"
    assert result.reason is None
    assert result.provider_account_id == "acct_story44_billable"
    assert result.provider_invoice_id == "in_story44_primary"
    assert result.stripe_invoice_id == "in_story44_primary"
    assert result.invoice_status == "open"
    assert provider.readiness_calls == ["acct_story44_billable"]
    assert provider.create_calls == [
        {
            "stripe_account_id": "acct_story44_billable",
            "amount_cents": 15000,
            "currency": "USD",
            "metadata": {
                "creator_id": str(creator_id),
                "booking_provider": "calendly",
                "provider_booking_id": "BOOK_story44_primary",
                "booking_uuid": "BOOK_story44_primary",
                "tid": content_tid,
            },
            "idempotency_key": "billing:create:calendly:BOOK_story44_primary",
        }
    ]
    assert provider.void_calls == []
    assert len(invoices) == 1
    assert len(_booking_rows()) == 1
    assert _booking_rows()[0].frozen_billing_amount_cents == 15000
    assert _booking_rows()[0].frozen_billing_currency == "USD"
    assert invoices[0].creator_id == creator_id
    assert invoices[0].booking_id == booking_id
    assert invoices[0].tid == content_tid
    assert invoices[0].payment_provider == "stripe"
    assert invoices[0].provider_account_id == "acct_story44_billable"
    assert invoices[0].provider_invoice_id == "in_story44_primary"
    assert invoices[0].stripe_account_id == "acct_story44_billable"
    assert invoices[0].stripe_invoice_id == "in_story44_primary"
    assert invoices[0].amount_cents == 15000
    assert invoices[0].currency == "USD"
    assert invoices[0].status == "open"
    assert invoices[0].issued_at == issued_at
    assert invoices[0].voided_at is None
    assert invoices[0].paid_at is None


def test_billing_orchestrator_uses_provider_aware_identity_for_fullscope_booking():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    issued_at = datetime(2026, 3, 8, 18, 7, tzinfo=UTC)
    provider = _StubStripeProvider(
        readiness=StripeAccountReadiness(charges_enabled=True),
        created_invoice_id="in_story44_fullscope",
    )

    with Session(engine) as session:
        creator, _, content, booking = _persist_booking_graph(
            session,
            booking_uuid="APT_story44_fullscope",
            tid="story44_fullscope_tid",
            provider="fullscope",
        )
        creator_id = creator.id
        booking_id = booking.id
        session.commit()

    orchestrator = BillingOrchestrator(
        session_factory=lambda: Session(engine),
        provider=provider,
        now_fn=lambda: issued_at,
    )

    result = orchestrator.create_invoice_for_booking(booking_id=booking_id)
    invoices = _invoice_rows()

    assert result.outcome == "created"
    assert result.provider_account_id == "acct_story44_billable"
    assert result.provider_invoice_id == "in_story44_fullscope"
    assert result.stripe_invoice_id == "in_story44_fullscope"
    assert provider.create_calls == [
        {
            "stripe_account_id": "acct_story44_billable",
            "amount_cents": 15000,
            "currency": "USD",
            "metadata": {
                "creator_id": str(creator_id),
                "booking_provider": "fullscope",
                "provider_booking_id": "APT_story44_fullscope",
                "booking_uuid": "APT_story44_fullscope",
                "tid": "story44_fullscope_tid",
            },
            "idempotency_key": "billing:create:fullscope:APT_story44_fullscope",
        }
    ]
    assert len(invoices) == 1
    assert invoices[0].booking_id == booking_id
    assert invoices[0].payment_provider == "stripe"
    assert invoices[0].provider_account_id == "acct_story44_billable"
    assert invoices[0].provider_invoice_id == "in_story44_fullscope"
    assert invoices[0].stripe_invoice_id == "in_story44_fullscope"
    assert _blocked_case_rows() == []


def test_billing_orchestrator_create_is_idempotent_for_same_booking():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    provider = _StubStripeProvider(
        readiness=StripeAccountReadiness(charges_enabled=True),
        created_invoice_id="in_story44_idempotent",
    )

    with Session(engine) as session:
        _, _, _, booking = _persist_booking_graph(
            session,
            booking_uuid="BOOK_story44_idempotent",
            tid="story44_idempotent_tid",
        )
        booking_id = booking.id
        session.commit()

    orchestrator = BillingOrchestrator(
        session_factory=lambda: Session(engine),
        provider=provider,
        now_fn=lambda: datetime(2026, 3, 8, 18, 10, tzinfo=UTC),
    )

    first_result = orchestrator.create_invoice_for_booking(booking_id=booking_id)
    second_result = orchestrator.create_invoice_for_booking(booking_id=booking_id)
    invoices = _invoice_rows()

    assert first_result.outcome == "created"
    assert second_result.outcome == "existing"
    assert second_result.reason is None
    assert second_result.invoice_id == first_result.invoice_id
    assert second_result.provider_account_id == "acct_story44_billable"
    assert second_result.provider_invoice_id == "in_story44_idempotent"
    assert second_result.stripe_invoice_id == "in_story44_idempotent"
    assert provider.readiness_calls == ["acct_story44_billable"]
    assert len(provider.create_calls) == 1
    assert len(invoices) == 1
    assert invoices[0].booking_id == booking_id
    assert invoices[0].stripe_invoice_id == "in_story44_idempotent"


def test_billing_orchestrator_persists_paid_invoice_when_provider_returns_paid_status():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    issued_at = datetime(2026, 3, 8, 18, 12, tzinfo=UTC)
    provider = _StubStripeProvider(
        readiness=StripeAccountReadiness(charges_enabled=True),
        created_invoice_id="in_story57_paid",
        created_invoice_status="paid",
    )

    with Session(engine) as session:
        _, _, _, booking = _persist_booking_graph(
            session,
            booking_uuid="BOOK_story57_paid",
            tid="story57_paid_tid",
        )
        booking_id = booking.id
        session.commit()

    orchestrator = BillingOrchestrator(
        session_factory=lambda: Session(engine),
        provider=provider,
        now_fn=lambda: issued_at,
    )

    result = orchestrator.create_invoice_for_booking(booking_id=booking_id)
    invoices = _invoice_rows()

    assert result.outcome == "created"
    assert result.reason is None
    assert result.provider_account_id == "acct_story44_billable"
    assert result.provider_invoice_id == "in_story57_paid"
    assert result.stripe_invoice_id == "in_story57_paid"
    assert result.invoice_status == "paid"
    assert len(invoices) == 1
    assert len(_booking_rows()) == 1
    assert _booking_rows()[0].frozen_billing_amount_cents == 15000
    assert _booking_rows()[0].frozen_billing_currency == "USD"
    assert invoices[0].payment_provider == "stripe"
    assert invoices[0].provider_account_id == "acct_story44_billable"
    assert invoices[0].provider_invoice_id == "in_story57_paid"
    assert invoices[0].status == "paid"
    assert invoices[0].issued_at == issued_at
    assert invoices[0].paid_at == issued_at


def test_billing_orchestrator_defers_when_billing_defaults_are_missing():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    provider = _StubStripeProvider(readiness=StripeAccountReadiness(charges_enabled=True))

    with Session(engine) as session:
        _, _, _, booking = _persist_booking_graph(
            session,
            booking_uuid="BOOK_story44_missing_defaults",
            tid="story44_missing_defaults_tid",
            billing_amount_cents=None,
            billing_currency=None,
        )
        booking_id = booking.id
        session.commit()

    orchestrator = BillingOrchestrator(
        session_factory=lambda: Session(engine),
        provider=provider,
    )

    result = orchestrator.create_invoice_for_booking(booking_id=booking_id)
    bookings = _booking_rows()

    assert result.outcome == "deferred"
    assert result.reason == "missing_billing_defaults"
    assert result.invoice_id is None
    assert provider.readiness_calls == []
    assert provider.create_calls == []
    assert _invoice_rows() == []
    assert _blocked_case_rows() == []
    assert len(bookings) == 1
    assert bookings[0].frozen_billing_amount_cents is None
    assert bookings[0].frozen_billing_currency is None


def test_billing_orchestrator_defers_when_booking_is_unattributed():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    provider = _StubStripeProvider(readiness=StripeAccountReadiness(charges_enabled=True))

    with Session(engine) as session:
        _, _, _, booking = _persist_booking_graph(
            session,
            booking_uuid="BOOK_story78_unattributed",
            tid="story78_unattributed_tid",
        )
        booking.tid = None
        booking.attribution_status = BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED
        booking.unattributed_reason = BOOKING_UNATTRIBUTED_REASON_MISSING_TID
        booking_id = booking.id
        session.commit()

    orchestrator = BillingOrchestrator(
        session_factory=lambda: Session(engine),
        provider=provider,
    )

    result = orchestrator.create_invoice_for_booking(booking_id=booking_id)
    bookings = _booking_rows()

    assert result.outcome == "deferred"
    assert result.reason == "booking_unattributed"
    assert result.invoice_id is None
    assert provider.readiness_calls == []
    assert provider.create_calls == []
    assert _invoice_rows() == []
    assert _blocked_case_rows() == []
    assert len(bookings) == 1
    assert bookings[0].tid is None
    assert bookings[0].attribution_status == BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED
    assert bookings[0].unattributed_reason == BOOKING_UNATTRIBUTED_REASON_MISSING_TID


def test_billing_orchestrator_freezes_once_defaults_become_complete_after_initial_missing_defaults():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    provider = _StubStripeProvider(
        readiness=StripeAccountReadiness(charges_enabled=True),
        created_invoice_id="in_story67_late_defaults",
    )

    with Session(engine) as session:
        _, booking_link, _, booking = _persist_booking_graph(
            session,
            booking_uuid="BOOK_story67_late_defaults",
            tid="story67_late_defaults_tid",
            billing_amount_cents=None,
            billing_currency=None,
        )
        booking_id = booking.id
        booking_link_id = booking_link.id
        session.commit()

    orchestrator = BillingOrchestrator(
        session_factory=lambda: Session(engine),
        provider=provider,
        now_fn=lambda: datetime(2026, 3, 8, 18, 11, tzinfo=UTC),
    )

    first_result = orchestrator.create_invoice_for_booking(booking_id=booking_id)

    with Session(engine) as session:
        booking_link = session.get(BookingLink, booking_link_id)
        assert booking_link is not None
        booking_link.billing_amount_cents = 12500
        booking_link.billing_currency = "cad"
        session.commit()

    second_result = orchestrator.create_invoice_for_booking(booking_id=booking_id)
    bookings = _booking_rows()
    invoices = _invoice_rows()

    assert first_result.outcome == "deferred"
    assert first_result.reason == "missing_billing_defaults"
    assert second_result.outcome == "created"
    assert second_result.reason is None
    assert len(provider.create_calls) == 1
    assert provider.create_calls[0]["amount_cents"] == 12500
    assert provider.create_calls[0]["currency"] == "CAD"
    assert len(bookings) == 1
    assert bookings[0].frozen_billing_amount_cents == 12500
    assert bookings[0].frozen_billing_currency == "CAD"
    assert len(invoices) == 1
    assert invoices[0].amount_cents == 12500
    assert invoices[0].currency == "CAD"


def test_billing_orchestrator_uses_booking_frozen_values_before_current_booking_link_defaults():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    provider = _StubStripeProvider(
        readiness=StripeAccountReadiness(charges_enabled=True),
        created_invoice_id="in_story67_booking_frozen",
    )

    with Session(engine) as session:
        _, booking_link, _, booking = _persist_booking_graph(
            session,
            booking_uuid="BOOK_story67_booking_frozen",
            tid="story67_booking_frozen_tid",
            billing_amount_cents=9900,
            billing_currency="EUR",
        )
        booking_id = booking.id
        booking.frozen_billing_amount_cents = 15000
        booking.frozen_billing_currency = "USD"
        session.commit()

    orchestrator = BillingOrchestrator(
        session_factory=lambda: Session(engine),
        provider=provider,
        now_fn=lambda: datetime(2026, 3, 8, 18, 12, tzinfo=UTC),
    )

    result = orchestrator.create_invoice_for_booking(booking_id=booking_id)
    invoices = _invoice_rows()

    assert result.outcome == "created"
    assert result.reason is None
    assert len(provider.create_calls) == 1
    assert provider.create_calls[0]["amount_cents"] == 15000
    assert provider.create_calls[0]["currency"] == "USD"
    assert len(invoices) == 1
    assert invoices[0].amount_cents == 15000
    assert invoices[0].currency == "USD"


def test_billing_orchestrator_defers_when_creator_is_not_billable():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    blocked_at = datetime(2026, 3, 8, 18, 13, tzinfo=UTC)
    provider = _StubStripeProvider(readiness=StripeAccountReadiness(charges_enabled=False))

    with Session(engine) as session:
        _, _, _, booking = _persist_booking_graph(
            session,
            booking_uuid="BOOK_story44_not_billable",
            tid="story44_not_billable_tid",
            stripe_account_id="acct_story44_not_billable",
        )
        booking_id = booking.id
        session.commit()

    orchestrator = BillingOrchestrator(
        session_factory=lambda: Session(engine),
        provider=provider,
        now_fn=lambda: blocked_at,
    )

    result = orchestrator.create_invoice_for_booking(booking_id=booking_id)
    blocked_cases = _blocked_case_rows()

    assert result.outcome == "deferred"
    assert result.reason == "creator_not_billable"
    assert result.invoice_id is None
    assert provider.readiness_calls == ["acct_story44_not_billable"]
    assert provider.create_calls == []
    assert _invoice_rows() == []
    assert len(blocked_cases) == 1
    assert len(_booking_rows()) == 1
    assert _booking_rows()[0].frozen_billing_amount_cents == 15000
    assert _booking_rows()[0].frozen_billing_currency == "USD"
    assert blocked_cases[0].booking_id == booking_id
    assert blocked_cases[0].provider == "calendly"
    assert blocked_cases[0].provider_booking_id == "BOOK_story44_not_billable"
    assert blocked_cases[0].calendly_booking_uuid == "BOOK_story44_not_billable"
    assert blocked_cases[0].payment_provider == "stripe"
    assert blocked_cases[0].provider_account_id == "acct_story44_not_billable"
    assert blocked_cases[0].reason_code == "creator_not_billable"
    assert blocked_cases[0].frozen_amount_cents == 15000
    assert blocked_cases[0].frozen_currency == "USD"
    assert blocked_cases[0].status == "open"
    assert blocked_cases[0].first_blocked_at == blocked_at
    assert blocked_cases[0].last_blocked_at == blocked_at
    assert blocked_cases[0].resolved_at is None


def test_billing_orchestrator_defers_when_readiness_lookup_raises_provider_error():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    blocked_at = datetime(2026, 3, 8, 18, 14, tzinfo=UTC)
    provider = _StubStripeProvider(
        readiness=StripeAccountReadiness(charges_enabled=True),
        readiness_error=StripeProviderError(
            "stripe account readiness lookup failed",
            operation="stripe_account_readiness",
            http_status=503,
            error_code="api_connection_error",
        ),
    )

    with Session(engine) as session:
        _, _, _, booking = _persist_booking_graph(
            session,
            booking_uuid="BOOK_story57_readiness_failure",
            tid="story57_readiness_failure_tid",
            stripe_account_id="acct_story57_readiness_failure",
        )
        booking_id = booking.id
        session.commit()

    orchestrator = BillingOrchestrator(
        session_factory=lambda: Session(engine),
        provider=provider,
        now_fn=lambda: blocked_at,
    )

    result = orchestrator.create_invoice_for_booking(booking_id=booking_id)
    blocked_cases = _blocked_case_rows()

    assert result.outcome == "deferred"
    assert result.reason == "provider_error"
    assert result.invoice_id is None
    assert provider.readiness_calls == ["acct_story57_readiness_failure"]
    assert provider.create_calls == []
    assert _invoice_rows() == []
    assert len(blocked_cases) == 1
    assert len(_booking_rows()) == 1
    assert _booking_rows()[0].frozen_billing_amount_cents == 15000
    assert _booking_rows()[0].frozen_billing_currency == "USD"
    assert blocked_cases[0].booking_id == booking_id
    assert blocked_cases[0].provider == "calendly"
    assert blocked_cases[0].provider_booking_id == "BOOK_story57_readiness_failure"
    assert blocked_cases[0].reason_code == "provider_error"
    assert blocked_cases[0].provider_operation == "stripe_account_readiness"
    assert blocked_cases[0].provider_http_status == 503
    assert blocked_cases[0].provider_error_code == "api_connection_error"
    assert blocked_cases[0].first_blocked_at == blocked_at


def test_billing_orchestrator_defers_when_invoice_create_raises_provider_error():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    blocked_at = datetime(2026, 3, 8, 18, 16, tzinfo=UTC)
    provider = _StubStripeProvider(
        readiness=StripeAccountReadiness(charges_enabled=True),
        create_error=StripeProviderError(
            "stripe invoice creation failed",
            operation="stripe_invoice_create",
            http_status=502,
            error_code="api_error",
        ),
    )

    with Session(engine) as session:
        _, _, _, booking = _persist_booking_graph(
            session,
            booking_uuid="BOOK_story57_create_failure",
            tid="story57_create_failure_tid",
            stripe_account_id="acct_story57_create_failure",
        )
        booking_id = booking.id
        session.commit()

    orchestrator = BillingOrchestrator(
        session_factory=lambda: Session(engine),
        provider=provider,
        now_fn=lambda: blocked_at,
    )

    result = orchestrator.create_invoice_for_booking(booking_id=booking_id)
    blocked_cases = _blocked_case_rows()

    assert result.outcome == "deferred"
    assert result.reason == "provider_error"
    assert result.invoice_id is None
    assert provider.readiness_calls == ["acct_story57_create_failure"]
    assert len(provider.create_calls) == 1
    assert _invoice_rows() == []
    assert len(blocked_cases) == 1
    assert len(_booking_rows()) == 1
    assert _booking_rows()[0].frozen_billing_amount_cents == 15000
    assert _booking_rows()[0].frozen_billing_currency == "USD"
    assert blocked_cases[0].booking_id == booking_id
    assert blocked_cases[0].provider == "calendly"
    assert blocked_cases[0].provider_booking_id == "BOOK_story57_create_failure"
    assert blocked_cases[0].reason_code == "provider_error"
    assert blocked_cases[0].provider_operation == "stripe_invoice_create"
    assert blocked_cases[0].provider_http_status == 502
    assert blocked_cases[0].provider_error_code == "api_error"


def test_blocked_billing_retry_uses_frozen_inputs_and_is_idempotent():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    blocked_at = datetime(2026, 3, 8, 18, 18, tzinfo=UTC)
    recovered_at = datetime(2026, 3, 8, 18, 30, tzinfo=UTC)
    initial_provider = _StubStripeProvider(readiness=StripeAccountReadiness(charges_enabled=False))

    with Session(engine) as session:
        creator, booking_link, _, booking = _persist_booking_graph(
            session,
            booking_uuid="BOOK_story58_retry",
            tid="story58_retry_tid",
            stripe_account_id="acct_story58_retry",
        )
        creator_id = creator.id
        booking_id = booking.id
        booking_link_id = booking_link.id
        session.commit()

    initial_orchestrator = BillingOrchestrator(
        session_factory=lambda: Session(engine),
        provider=initial_provider,
        now_fn=lambda: blocked_at,
    )
    initial_result = initial_orchestrator.create_invoice_for_booking(booking_id=booking_id)

    with Session(engine) as session:
        booking_link = session.get(BookingLink, booking_link_id)
        assert booking_link is not None
        booking_link.billing_amount_cents = 9900
        booking_link.billing_currency = "EUR"
        session.commit()

        blocked_case = session.scalar(
            select(BlockedBillingCase).where(BlockedBillingCase.booking_id == booking_id)
        )
        assert blocked_case is not None
        blocked_case_id = blocked_case.id

    retry_provider = _StubStripeProvider(
        readiness=StripeAccountReadiness(charges_enabled=True),
        created_invoice_id="in_story58_retry_recovered",
    )
    retry_service = BlockedBillingRetryService(
        session_factory=lambda: Session(engine),
        provider=retry_provider,
        now_fn=lambda: recovered_at,
    )

    first_retry = retry_service.retry_case(
        case_id=blocked_case_id,
        creator_id=creator_id,
    )
    second_retry = retry_service.retry_case(
        case_id=blocked_case_id,
        creator_id=creator_id,
    )
    invoices = _invoice_rows()
    blocked_cases = _blocked_case_rows()
    bookings = _booking_rows()

    assert initial_result.outcome == "deferred"
    assert initial_result.reason == "creator_not_billable"
    assert first_retry.outcome == "created"
    assert second_retry.outcome == "already_resolved"
    assert first_retry.provider_account_id == "acct_story58_retry"
    assert first_retry.provider_invoice_id == "in_story58_retry_recovered"
    assert second_retry.provider_account_id == "acct_story58_retry"
    assert second_retry.provider_invoice_id == "in_story58_retry_recovered"
    assert len(retry_provider.create_calls) == 1
    assert retry_provider.create_calls[0]["amount_cents"] == 15000
    assert retry_provider.create_calls[0]["currency"] == "USD"
    assert len(invoices) == 1
    assert invoices[0].booking_id == booking_id
    assert invoices[0].amount_cents == 15000
    assert invoices[0].currency == "USD"
    assert len(bookings) == 1
    assert bookings[0].frozen_billing_amount_cents == 15000
    assert bookings[0].frozen_billing_currency == "USD"
    assert len(blocked_cases) == 1
    assert blocked_cases[0].status == "resolved"
    assert blocked_cases[0].invoice_id == invoices[0].id
    assert blocked_cases[0].resolution_code == "invoice_created"
    assert blocked_cases[0].resolved_at == recovered_at
    assert blocked_cases[0].last_retry_at == recovered_at


def test_blocked_billing_retry_uses_frozen_provider_snapshot_after_creator_switch():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    blocked_at = datetime(2026, 3, 23, 10, 0, tzinfo=UTC)
    recovered_at = datetime(2026, 3, 23, 10, 15, tzinfo=UTC)
    initial_stripe_provider = _StubStripeProvider(
        readiness=StripeAccountReadiness(charges_enabled=False)
    )

    with Session(engine) as session:
        creator, _, _, booking = _persist_booking_graph(
            session,
            booking_uuid="BOOK_story100_frozen_provider",
            tid="story100_frozen_provider_tid",
            stripe_account_id="acct_story100_frozen",
        )
        creator_id = creator.id
        booking_id = booking.id
        session.commit()

    initial_orchestrator = BillingOrchestrator(
        session_factory=lambda: Session(engine),
        providers={
            "stripe": initial_stripe_provider,
            "paypal": _StubPayPalProvider(
                readiness=BillingAccountReadiness(can_create_invoices=True),
            ),
        },
        now_fn=lambda: blocked_at,
    )

    initial_result = initial_orchestrator.create_invoice_for_booking(booking_id=booking_id)

    with Session(engine) as session:
        creator = session.get(Creator, creator_id)
        blocked_case = session.scalar(
            select(BlockedBillingCase).where(BlockedBillingCase.booking_id == booking_id)
        )
        assert creator is not None
        assert blocked_case is not None
        blocked_case_id = blocked_case.id
        assert blocked_case.payment_provider == "stripe"
        assert blocked_case.provider_account_id == "acct_story100_frozen"
        creator.billing_provider = "paypal"
        creator.billing_account_id = "merchant_story100_switched"
        creator.billing_connect_status = "connected"
        session.commit()

    retry_stripe_provider = _StubStripeProvider(
        readiness=StripeAccountReadiness(charges_enabled=True),
        created_invoice_id="in_story100_frozen_provider",
    )
    retry_paypal_provider = _StubPayPalProvider(
        readiness=BillingAccountReadiness(can_create_invoices=True),
        created_invoice_id="INV2_story100_should_not_be_used",
    )
    retry_service = BlockedBillingRetryService(
        session_factory=lambda: Session(engine),
        providers={
            "stripe": retry_stripe_provider,
            "paypal": retry_paypal_provider,
        },
        now_fn=lambda: recovered_at,
    )

    retry_result = retry_service.retry_case(
        case_id=blocked_case_id,
        creator_id=creator_id,
    )
    invoices = _invoice_rows()
    blocked_cases = _blocked_case_rows()

    assert initial_result.outcome == "deferred"
    assert initial_result.reason == "creator_not_billable"
    assert retry_result.outcome == "created"
    assert retry_result.provider_account_id == "acct_story100_frozen"
    assert retry_result.provider_invoice_id == "in_story100_frozen_provider"
    assert retry_stripe_provider.readiness_calls == ["acct_story100_frozen"]
    assert retry_stripe_provider.create_calls == [
        {
            "stripe_account_id": "acct_story100_frozen",
            "amount_cents": 15000,
            "currency": "USD",
            "metadata": {
                "creator_id": str(creator_id),
                "booking_provider": "calendly",
                "provider_booking_id": "BOOK_story100_frozen_provider",
                "booking_uuid": "BOOK_story100_frozen_provider",
                "tid": "story100_frozen_provider_tid",
            },
            "idempotency_key": "billing:create:calendly:BOOK_story100_frozen_provider",
        }
    ]
    assert retry_paypal_provider.readiness_calls == []
    assert retry_paypal_provider.create_calls == []
    assert len(invoices) == 1
    assert invoices[0].payment_provider == "stripe"
    assert invoices[0].provider_account_id == "acct_story100_frozen"
    assert len(blocked_cases) == 1
    assert blocked_cases[0].payment_provider == "stripe"
    assert blocked_cases[0].provider_account_id == "acct_story100_frozen"
    assert blocked_cases[0].resolution_code == "invoice_created"


def test_blocked_billing_retry_backfills_booking_from_legacy_blocked_case_snapshot():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    blocked_at = datetime(2026, 3, 8, 18, 19, tzinfo=UTC)
    recovered_at = datetime(2026, 3, 8, 18, 31, tzinfo=UTC)

    with Session(engine) as session:
        creator, _, _, booking = _persist_booking_graph(
            session,
            booking_uuid="BOOK_story67_legacy_blocked",
            tid="story67_legacy_blocked_tid",
            stripe_account_id="acct_story67_legacy_blocked",
            billing_amount_cents=9900,
            billing_currency="EUR",
        )
        creator_id = creator.id
        booking_id = booking.id
        session.add(
            BlockedBillingCase(
                creator_id=creator.id,
                booking_id=booking.id,
                invoice_id=None,
                tid=booking.tid,
                calendly_booking_uuid=booking.calendly_booking_uuid,
                stripe_account_id=creator.stripe_account_id,
                frozen_amount_cents=15000,
                frozen_currency="USD",
                status="open",
                reason_code="creator_not_billable",
                first_blocked_at=blocked_at,
                last_blocked_at=blocked_at,
                last_retry_at=None,
                resolved_at=None,
                resolution_code=None,
            )
        )
        session.commit()

    retry_provider = _StubStripeProvider(
        readiness=StripeAccountReadiness(charges_enabled=True),
        created_invoice_id="in_story67_legacy_blocked",
    )
    retry_service = BlockedBillingRetryService(
        session_factory=lambda: Session(engine),
        provider=retry_provider,
        now_fn=lambda: recovered_at,
    )

    with Session(engine) as session:
        blocked_case = session.scalar(
            select(BlockedBillingCase).where(BlockedBillingCase.booking_id == booking_id)
        )
        assert blocked_case is not None
        blocked_case_id = blocked_case.id

    result = retry_service.retry_case(case_id=blocked_case_id, creator_id=creator_id)
    bookings = _booking_rows()
    invoices = _invoice_rows()

    assert result.outcome == "created"
    assert len(retry_provider.create_calls) == 1
    assert retry_provider.create_calls[0]["amount_cents"] == 15000
    assert retry_provider.create_calls[0]["currency"] == "USD"
    assert len(bookings) == 1
    assert bookings[0].frozen_billing_amount_cents == 15000
    assert bookings[0].frozen_billing_currency == "USD"
    assert len(invoices) == 1
    assert invoices[0].amount_cents == 15000
    assert invoices[0].currency == "USD"


def test_billing_orchestrator_voids_open_invoice_and_is_idempotent():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    voided_at = datetime(2026, 3, 8, 18, 20, tzinfo=UTC)
    provider = _StubStripeProvider(readiness=StripeAccountReadiness(charges_enabled=True))

    with Session(engine) as session:
        creator, _, content, booking = _persist_booking_graph(
            session,
            booking_uuid="BOOK_story44_void",
            tid="story44_void_tid",
        )
        creator_id = creator.id
        booking_id = booking.id
        content_tid = content.tid
        session.add(
            Invoice(
                creator_id=creator_id,
                booking_id=booking_id,
                tid=content_tid,
                stripe_account_id="acct_story44_billable",
                stripe_invoice_id="in_story44_void",
                amount_cents=15000,
                currency="USD",
                status="open",
                issued_at=datetime(2026, 3, 8, 18, 15, tzinfo=UTC),
            )
        )
        session.commit()

    orchestrator = BillingOrchestrator(
        session_factory=lambda: Session(engine),
        provider=provider,
        now_fn=lambda: voided_at,
    )

    first_result = orchestrator.void_open_invoice_for_booking(booking_id=booking_id)
    second_result = orchestrator.void_open_invoice_for_booking(booking_id=booking_id)
    invoices = _invoice_rows()

    assert first_result.outcome == "voided"
    assert first_result.reason is None
    assert first_result.provider_account_id == "acct_story44_billable"
    assert first_result.provider_invoice_id == "in_story44_void"
    assert first_result.stripe_invoice_id == "in_story44_void"
    assert first_result.invoice_status == "void"
    assert second_result.outcome == "noop"
    assert second_result.reason == "invoice_already_void"
    assert second_result.invoice_id == first_result.invoice_id
    assert provider.void_calls == [
        {
            "stripe_account_id": "acct_story44_billable",
            "stripe_invoice_id": "in_story44_void",
        }
    ]
    assert len(invoices) == 1
    assert invoices[0].status == "void"
    assert invoices[0].voided_at == voided_at


def test_billing_orchestrator_void_returns_noop_when_provider_void_raises_error():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    provider = _StubStripeProvider(
        readiness=StripeAccountReadiness(charges_enabled=True),
        void_error=StripeProviderError(
            "stripe invoice void failed",
            operation="stripe_invoice_void",
            http_status=409,
            error_code="invoice_invalid_state",
        ),
    )

    with Session(engine) as session:
        creator, _, content, booking = _persist_booking_graph(
            session,
            booking_uuid="BOOK_story57_void_failure",
            tid="story57_void_failure_tid",
        )
        booking_id = booking.id
        session.add(
            Invoice(
                creator_id=creator.id,
                booking_id=booking_id,
                tid=content.tid,
                stripe_account_id="acct_story44_billable",
                stripe_invoice_id="in_story57_void_failure",
                amount_cents=15000,
                currency="USD",
                status="open",
                issued_at=datetime(2026, 3, 8, 18, 25, tzinfo=UTC),
            )
        )
        session.commit()

    orchestrator = BillingOrchestrator(
        session_factory=lambda: Session(engine),
        provider=provider,
        now_fn=lambda: datetime(2026, 3, 8, 18, 30, tzinfo=UTC),
    )

    result = orchestrator.void_open_invoice_for_booking(booking_id=booking_id)
    invoices = _invoice_rows()

    assert result.outcome == "noop"
    assert result.reason == "provider_error"
    assert result.invoice_status == "open"
    assert provider.void_calls == [
        {
            "stripe_account_id": "acct_story44_billable",
            "stripe_invoice_id": "in_story57_void_failure",
        }
    ]
    assert len(invoices) == 1
    assert invoices[0].status == "open"
    assert invoices[0].voided_at is None


def test_billing_orchestrator_dispatches_paypal_creator_to_paypal_provider():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    issued_at = datetime(2026, 3, 20, 12, 5, tzinfo=UTC)
    stripe_provider = _StubStripeProvider(
        readiness=StripeAccountReadiness(charges_enabled=True),
        created_invoice_id="in_story44_unused_stripe",
    )
    paypal_provider = _StubPayPalProvider(
        readiness=BillingAccountReadiness(can_create_invoices=True),
        created_invoice_id="INV2_story44_paypal",
    )

    with Session(engine) as session:
        creator, _, content, booking = _persist_booking_graph(
            session,
            booking_uuid="BOOK_story44_paypal",
            tid="story44_paypal_tid",
            stripe_account_id=None,
            billing_provider="paypal",
            billing_account_id="merchant_story44_paypal",
        )
        creator_id = creator.id
        booking_id = booking.id
        session.commit()

    orchestrator = BillingOrchestrator(
        session_factory=lambda: Session(engine),
        providers={
            "stripe": stripe_provider,
            "paypal": paypal_provider,
        },
        now_fn=lambda: issued_at,
    )

    result = orchestrator.create_invoice_for_booking(booking_id=booking_id)
    invoices = _invoice_rows()

    assert result.outcome == "created"
    assert result.reason is None
    assert result.provider_account_id == "merchant_story44_paypal"
    assert result.provider_invoice_id == "INV2_story44_paypal"
    assert result.stripe_invoice_id == "INV2_story44_paypal"
    assert result.invoice_status == "open"
    assert stripe_provider.readiness_calls == []
    assert stripe_provider.create_calls == []
    assert paypal_provider.readiness_calls == ["merchant_story44_paypal"]
    assert paypal_provider.create_calls == [
        {
            "provider_account_id": "merchant_story44_paypal",
            "amount_cents": 15000,
            "currency": "USD",
            "metadata": {
                "creator_id": str(creator_id),
                "booking_provider": "calendly",
                "provider_booking_id": "BOOK_story44_paypal",
                "booking_uuid": "BOOK_story44_paypal",
                "tid": "story44_paypal_tid",
            },
            "idempotency_key": "billing:create:calendly:BOOK_story44_paypal",
        }
    ]
    assert len(invoices) == 1
    assert invoices[0].payment_provider == "paypal"
    assert invoices[0].provider_account_id == "merchant_story44_paypal"
    assert invoices[0].provider_invoice_id == "INV2_story44_paypal"
    assert invoices[0].stripe_account_id is None
    assert invoices[0].stripe_invoice_id is None
    assert invoices[0].status == "open"


def test_billing_orchestrator_keeps_active_provider_authoritative_while_switch_attempt_is_pending():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    issued_at = datetime(2026, 3, 21, 12, 5, tzinfo=UTC)
    stripe_provider = _StubStripeProvider(
        readiness=StripeAccountReadiness(charges_enabled=True),
        created_invoice_id="in_story44_pending_switch",
    )
    paypal_provider = _StubPayPalProvider(
        readiness=BillingAccountReadiness(can_create_invoices=True),
        created_invoice_id="INV2_story44_unused_switch",
    )

    with Session(engine) as session:
        creator, _, _, booking = _persist_booking_graph(
            session,
            booking_uuid="BOOK_story44_pending_switch",
            tid="story44_pending_switch_tid",
            stripe_account_id="acct_story44_pending_switch",
        )
        booking_id = booking.id
        session.add(
            BillingProviderSwitchAttempt(
                creator_id=creator.id,
                source_billing_provider="stripe",
                target_billing_provider="paypal",
                target_billing_connect_status="connected",
                target_billing_account_id="merchant_story44_pending_switch",
                target_billing_connected_at=datetime(2026, 3, 21, 12, 0, tzinfo=UTC),
                target_billing_provider_correlation_id="tracking_story44_pending_switch",
            )
        )
        session.commit()

    orchestrator = BillingOrchestrator(
        session_factory=lambda: Session(engine),
        providers={
            "stripe": stripe_provider,
            "paypal": paypal_provider,
        },
        now_fn=lambda: issued_at,
    )

    result = orchestrator.create_invoice_for_booking(booking_id=booking_id)
    invoices = _invoice_rows()

    assert result.outcome == "created"
    assert stripe_provider.readiness_calls == ["acct_story44_pending_switch"]
    assert len(stripe_provider.create_calls) == 1
    assert paypal_provider.readiness_calls == []
    assert paypal_provider.create_calls == []
    assert len(invoices) == 1
    assert invoices[0].payment_provider == "stripe"
    assert invoices[0].provider_account_id == "acct_story44_pending_switch"


def test_billing_orchestrator_voids_paypal_invoice_via_provider_registry():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    voided_at = datetime(2026, 3, 20, 12, 10, tzinfo=UTC)
    stripe_provider = _StubStripeProvider(readiness=StripeAccountReadiness(charges_enabled=True))
    paypal_provider = _StubPayPalProvider(
        readiness=BillingAccountReadiness(can_create_invoices=True),
    )

    with Session(engine) as session:
        creator, _, content, booking = _persist_booking_graph(
            session,
            booking_uuid="BOOK_story44_paypal_void",
            tid="story44_paypal_void_tid",
            stripe_account_id=None,
            billing_provider="paypal",
            billing_account_id="merchant_story44_paypal_void",
        )
        booking_id = booking.id
        session.add(
            Invoice(
                creator_id=creator.id,
                booking_id=booking_id,
                tid=content.tid,
                payment_provider="paypal",
                provider_account_id="merchant_story44_paypal_void",
                provider_invoice_id="INV2_story44_paypal_void",
                amount_cents=15000,
                currency="USD",
                status="open",
                issued_at=datetime(2026, 3, 20, 12, 0, tzinfo=UTC),
            )
        )
        session.commit()

    orchestrator = BillingOrchestrator(
        session_factory=lambda: Session(engine),
        providers={
            "stripe": stripe_provider,
            "paypal": paypal_provider,
        },
        now_fn=lambda: voided_at,
    )

    result = orchestrator.void_open_invoice_for_booking(booking_id=booking_id)
    invoices = _invoice_rows()

    assert result.outcome == "voided"
    assert result.reason is None
    assert result.provider_account_id == "merchant_story44_paypal_void"
    assert result.provider_invoice_id == "INV2_story44_paypal_void"
    assert result.invoice_status == "void"
    assert stripe_provider.void_calls == []
    assert paypal_provider.stop_calls == [
        {
            "provider_account_id": "merchant_story44_paypal_void",
            "provider_invoice_id": "INV2_story44_paypal_void",
        }
    ]
    assert len(invoices) == 1
    assert invoices[0].status == "void"
    assert invoices[0].voided_at == voided_at


def test_blocked_billing_retry_dispatches_paypal_case_to_paypal_provider():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    blocked_at = datetime(2026, 3, 20, 12, 15, tzinfo=UTC)
    recovered_at = datetime(2026, 3, 20, 12, 30, tzinfo=UTC)
    initial_paypal_provider = _StubPayPalProvider(
        readiness=BillingAccountReadiness(can_create_invoices=False),
    )

    with Session(engine) as session:
        creator, booking_link, _, booking = _persist_booking_graph(
            session,
            booking_uuid="BOOK_story44_paypal_retry",
            tid="story44_paypal_retry_tid",
            stripe_account_id=None,
            billing_provider="paypal",
            billing_account_id="merchant_story44_paypal_retry",
        )
        creator_id = creator.id
        booking_id = booking.id
        booking_link_id = booking_link.id
        session.commit()

    initial_orchestrator = BillingOrchestrator(
        session_factory=lambda: Session(engine),
        providers={
            "stripe": _StubStripeProvider(readiness=StripeAccountReadiness(charges_enabled=True)),
            "paypal": initial_paypal_provider,
        },
        now_fn=lambda: blocked_at,
    )
    initial_result = initial_orchestrator.create_invoice_for_booking(booking_id=booking_id)

    with Session(engine) as session:
        booking_link = session.get(BookingLink, booking_link_id)
        assert booking_link is not None
        booking_link.billing_amount_cents = 9900
        booking_link.billing_currency = "EUR"
        session.commit()

        blocked_case = session.scalar(
            select(BlockedBillingCase).where(BlockedBillingCase.booking_id == booking_id)
        )
        assert blocked_case is not None
        blocked_case_id = blocked_case.id
        assert blocked_case.payment_provider == "paypal"
        assert blocked_case.provider_account_id == "merchant_story44_paypal_retry"

    retry_paypal_provider = _StubPayPalProvider(
        readiness=BillingAccountReadiness(can_create_invoices=True),
        created_invoice_id="INV2_story44_paypal_retry",
    )
    retry_service = BlockedBillingRetryService(
        session_factory=lambda: Session(engine),
        providers={
            "stripe": _StubStripeProvider(readiness=StripeAccountReadiness(charges_enabled=True)),
            "paypal": retry_paypal_provider,
        },
        now_fn=lambda: recovered_at,
    )

    first_retry = retry_service.retry_case(
        case_id=blocked_case_id,
        creator_id=creator_id,
    )
    second_retry = retry_service.retry_case(
        case_id=blocked_case_id,
        creator_id=creator_id,
    )
    invoices = _invoice_rows()
    blocked_cases = _blocked_case_rows()

    assert initial_result.outcome == "deferred"
    assert initial_result.reason == "creator_not_billable"
    assert first_retry.outcome == "created"
    assert second_retry.outcome == "already_resolved"
    assert first_retry.provider_account_id == "merchant_story44_paypal_retry"
    assert first_retry.provider_invoice_id == "INV2_story44_paypal_retry"
    assert second_retry.provider_account_id == "merchant_story44_paypal_retry"
    assert second_retry.provider_invoice_id == "INV2_story44_paypal_retry"
    assert retry_paypal_provider.readiness_calls == ["merchant_story44_paypal_retry"]
    assert retry_paypal_provider.create_calls == [
        {
            "provider_account_id": "merchant_story44_paypal_retry",
            "amount_cents": 15000,
            "currency": "USD",
            "metadata": {
                "creator_id": str(creator_id),
                "booking_provider": "calendly",
                "provider_booking_id": "BOOK_story44_paypal_retry",
                "booking_uuid": "BOOK_story44_paypal_retry",
                "tid": "story44_paypal_retry_tid",
            },
            "idempotency_key": "billing:create:calendly:BOOK_story44_paypal_retry",
        }
    ]
    assert len(invoices) == 1
    assert invoices[0].payment_provider == "paypal"
    assert invoices[0].provider_account_id == "merchant_story44_paypal_retry"
    assert invoices[0].provider_invoice_id == "INV2_story44_paypal_retry"


def test_billing_orchestrator_treats_unexpected_create_status_as_provider_error():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    blocked_at = datetime(2026, 3, 20, 13, 0, tzinfo=UTC)
    paypal_provider = _StubPayPalProvider(
        readiness=BillingAccountReadiness(can_create_invoices=True),
        created_invoice_id="INV2_story_pp10_invalid_create",
        created_invoice_status="draft",
    )

    with Session(engine) as session:
        creator, _, _, booking = _persist_booking_graph(
            session,
            booking_uuid="BOOK_story_pp10_invalid_create",
            tid="story_pp10_invalid_create_tid",
            stripe_account_id=None,
            billing_provider="paypal",
            billing_account_id="merchant_story_pp10_invalid_create",
        )
        creator_id = creator.id
        booking_id = booking.id
        session.commit()

    orchestrator = BillingOrchestrator(
        session_factory=lambda: Session(engine),
        providers={
            "stripe": _StubStripeProvider(readiness=StripeAccountReadiness(charges_enabled=True)),
            "paypal": paypal_provider,
        },
        now_fn=lambda: blocked_at,
    )

    result = orchestrator.create_invoice_for_booking(booking_id=booking_id)
    invoices = _invoice_rows()
    blocked_cases = _blocked_case_rows()

    assert result.outcome == "deferred"
    assert result.reason == "provider_error"
    assert result.invoice_id is None
    assert paypal_provider.create_calls == [
        {
            "provider_account_id": "merchant_story_pp10_invalid_create",
            "amount_cents": 15000,
            "currency": "USD",
            "metadata": {
                "creator_id": str(creator_id),
                "booking_provider": "calendly",
                "provider_booking_id": "BOOK_story_pp10_invalid_create",
                "booking_uuid": "BOOK_story_pp10_invalid_create",
                "tid": "story_pp10_invalid_create_tid",
            },
            "idempotency_key": "billing:create:calendly:BOOK_story_pp10_invalid_create",
        }
    ]
    assert invoices == []
    assert len(blocked_cases) == 1
    assert blocked_cases[0].booking_id == booking_id
    assert blocked_cases[0].reason_code == "provider_error"


def test_billing_orchestrator_void_keeps_invoice_open_when_provider_returns_non_void_status():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    attempted_void_at = datetime(2026, 3, 20, 13, 10, tzinfo=UTC)
    paypal_provider = _StubPayPalProvider(
        readiness=BillingAccountReadiness(can_create_invoices=True),
        stop_invoice_status="open",
    )

    with Session(engine) as session:
        creator, _, content, booking = _persist_booking_graph(
            session,
            booking_uuid="BOOK_story_pp10_invalid_void",
            tid="story_pp10_invalid_void_tid",
            stripe_account_id=None,
            billing_provider="paypal",
            billing_account_id="merchant_story_pp10_invalid_void",
        )
        booking_id = booking.id
        session.add(
            Invoice(
                creator_id=creator.id,
                booking_id=booking_id,
                tid=content.tid,
                payment_provider="paypal",
                provider_account_id="merchant_story_pp10_invalid_void",
                provider_invoice_id="INV2_story_pp10_invalid_void",
                amount_cents=15000,
                currency="USD",
                status="open",
                issued_at=datetime(2026, 3, 20, 13, 0, tzinfo=UTC),
            )
        )
        session.commit()

    orchestrator = BillingOrchestrator(
        session_factory=lambda: Session(engine),
        providers={
            "stripe": _StubStripeProvider(readiness=StripeAccountReadiness(charges_enabled=True)),
            "paypal": paypal_provider,
        },
        now_fn=lambda: attempted_void_at,
    )

    result = orchestrator.void_open_invoice_for_booking(booking_id=booking_id)
    invoices = _invoice_rows()

    assert result.outcome == "noop"
    assert result.reason == "provider_error"
    assert result.invoice_status == "open"
    assert paypal_provider.stop_calls == [
        {
            "provider_account_id": "merchant_story_pp10_invalid_void",
            "provider_invoice_id": "INV2_story_pp10_invalid_void",
        }
    ]
    assert len(invoices) == 1
    assert invoices[0].status == "open"
    assert invoices[0].voided_at is None
    assert invoices[0].stripe_account_id is None
    assert invoices[0].stripe_invoice_id is None
