import os
from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models.booking import Booking
from app.models.booking_link import BookingLink
from app.models.blocked_billing_case import BlockedBillingCase
from app.models.content import Content
from app.models.creator import Creator
from app.models.invoice import Invoice
from app.services.billing import BillingOrchestrator
from app.services.blocked_billing import BlockedBillingRetryService
from app.services.stripe_provider import (
    StripeAccountReadiness,
    StripeInvoiceCreateResult,
    StripeProviderError,
)


class _StubStripeProvider:
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


def _persist_booking_graph(
    session: Session,
    *,
    booking_uuid: str = "BOOK_story44_primary",
    tid: str = "story44_tid",
    stripe_account_id: str = "acct_story44_billable",
    billing_amount_cents: int | None = 15000,
    billing_currency: str | None = "USD",
) -> tuple[Creator, BookingLink, Content, Booking]:
    creator = Creator(
        name="Story 44 Creator",
        stripe_connect_status="connected",
        stripe_account_id=stripe_account_id,
    )
    session.add(creator)
    session.flush()

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
        calendly_booking_uuid=booking_uuid,
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
                "booking_uuid": "BOOK_story44_primary",
                "tid": content_tid,
            },
            "idempotency_key": "billing:create:BOOK_story44_primary",
        }
    ]
    assert provider.void_calls == []
    assert len(invoices) == 1
    assert invoices[0].creator_id == creator_id
    assert invoices[0].booking_id == booking_id
    assert invoices[0].tid == content_tid
    assert invoices[0].stripe_account_id == "acct_story44_billable"
    assert invoices[0].stripe_invoice_id == "in_story44_primary"
    assert invoices[0].amount_cents == 15000
    assert invoices[0].currency == "USD"
    assert invoices[0].status == "open"
    assert invoices[0].issued_at == issued_at
    assert invoices[0].voided_at is None
    assert invoices[0].paid_at is None


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
    assert result.stripe_invoice_id == "in_story57_paid"
    assert result.invoice_status == "paid"
    assert len(invoices) == 1
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

    assert result.outcome == "deferred"
    assert result.reason == "missing_billing_defaults"
    assert result.invoice_id is None
    assert provider.readiness_calls == []
    assert provider.create_calls == []
    assert _invoice_rows() == []
    assert _blocked_case_rows() == []


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
    assert blocked_cases[0].booking_id == booking_id
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
    assert blocked_cases[0].booking_id == booking_id
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
    assert blocked_cases[0].booking_id == booking_id
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

    assert initial_result.outcome == "deferred"
    assert initial_result.reason == "creator_not_billable"
    assert first_retry.outcome == "created"
    assert second_retry.outcome == "already_resolved"
    assert len(retry_provider.create_calls) == 1
    assert retry_provider.create_calls[0]["amount_cents"] == 15000
    assert retry_provider.create_calls[0]["currency"] == "USD"
    assert len(invoices) == 1
    assert invoices[0].booking_id == booking_id
    assert invoices[0].amount_cents == 15000
    assert invoices[0].currency == "USD"
    assert len(blocked_cases) == 1
    assert blocked_cases[0].status == "resolved"
    assert blocked_cases[0].invoice_id == invoices[0].id
    assert blocked_cases[0].resolution_code == "invoice_created"
    assert blocked_cases[0].resolved_at == recovered_at
    assert blocked_cases[0].last_retry_at == recovered_at


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
