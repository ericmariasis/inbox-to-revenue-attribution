import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from jose import jwt
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.auth_user import AuthUser
from app.models.blocked_billing_case import BlockedBillingCase
from app.models.booking import Booking
from app.models.booking_link import BookingLink
from app.models.content import Content
from app.models.content_confirmed_topic import ContentConfirmedTopic
from app.models.content_extraction_artifact import ContentExtractionArtifact
from app.models.content_fetch_snapshot import ContentFetchSnapshot
from app.models.content_topic_candidate import ContentTopicCandidate
from app.models.creator import Creator
from app.models.invoice import Invoice
from app.models.invoice_payment_event import InvoicePaymentEvent
from app.services.blocked_billing import BLOCKED_BILLING_REASON_CREATOR_NOT_BILLABLE
from app.services.content_topics import (
    CONTENT_TOPIC_REVIEW_STATUS_CONFIRMED,
    normalize_topic_label,
)
from app.services.invoice_payment_events import (
    UNATTRIBUTED_REASON_MISSING_TID,
    UNATTRIBUTED_REASON_UNKNOWN_STRIPE_INVOICE_ID,
)


@dataclass(frozen=True)
class ReportingGoldenFixture:
    creator_id: UUID
    user_id: UUID
    email: str
    access_token: str
    primary_tid: str
    primary_source_url: str
    historical_tid: str
    historical_source_url: str
    active_booking_link_name: str
    historical_booking_link_name: str
    paid_booking_uuid: str
    waiting_booking_uuid: str
    blocked_booking_uuid: str
    provider_invoice_id: str
    provider_event_id: str
    filter_start_date: date
    filter_end_date: date


def reporting_test_engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def _access_token(*, user_id: str, creator_id: str, email: str, expires_delta: timedelta) -> str:
    settings = get_settings()
    issued_at = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "creator_id": creator_id,
        "email": email,
        "iat": issued_at,
        "exp": issued_at + expires_delta,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def _create_creator_with_user(
    session: Session,
    *,
    suffix: str,
    stripe_account_id: str,
) -> tuple[Creator, AuthUser]:
    creator = Creator(
        name=f"Reports Golden Creator {suffix}",
        stripe_connect_status="connected",
        stripe_account_id=stripe_account_id,
    )
    session.add(creator)
    session.flush()

    user = AuthUser(
        creator_id=creator.id,
        email=f"reports_golden_{suffix}@example.com",
    )
    session.add(user)
    session.flush()
    return creator, user


def _create_booking_link(
    session: Session,
    *,
    creator: Creator,
    name: str,
    destination_url: str,
    billing_amount_cents: int | None,
    billing_currency: str | None,
) -> BookingLink:
    booking_link = BookingLink(
        creator_id=creator.id,
        name=name,
        destination_url=destination_url,
        calendly_url=destination_url,
        billing_amount_cents=billing_amount_cents,
        billing_currency=billing_currency,
    )
    session.add(booking_link)
    session.flush()
    return booking_link


def _create_content(
    session: Session,
    *,
    creator: Creator,
    booking_link: BookingLink,
    source_url: str,
    tid: str,
) -> Content:
    content = Content(
        creator_id=creator.id,
        booking_link_id=booking_link.id,
        source_url=source_url,
        tid=tid,
    )
    session.add(content)
    session.flush()
    return content


def _create_content_extraction_artifact(
    session: Session,
    *,
    creator: Creator,
    content: Content,
    title: str,
    extracted_text: str,
) -> ContentExtractionArtifact:
    created_at = datetime.now(timezone.utc)
    fetch_snapshot = ContentFetchSnapshot(
        content_id=content.id,
        creator_id=creator.id,
        requested_url=content.source_url,
        fetched_url=content.source_url,
        fetch_status="succeeded",
        http_status=200,
        snapshot_text=f"<article>{extracted_text}</article>",
        response_content_type="text/html",
        response_content_charset="utf-8",
        fetched_at=created_at,
    )
    session.add(fetch_snapshot)
    session.flush()

    artifact = ContentExtractionArtifact(
        content_id=content.id,
        creator_id=creator.id,
        fetch_snapshot_id=fetch_snapshot.id,
        extraction_status="succeeded",
        extraction_method="html_article",
        title=title,
        source_text_char_count=len(extracted_text),
        extracted_text_char_count=len(extracted_text),
        extracted_text_word_count=len(extracted_text.split()),
        extracted_text=extracted_text,
        created_at=created_at,
    )
    session.add(artifact)
    session.flush()
    return artifact


def _attach_confirmed_topic(
    session: Session,
    *,
    creator: Creator,
    content: Content,
    artifact: ContentExtractionArtifact,
    label: str,
    candidate_rank: int = 1,
) -> ContentConfirmedTopic:
    normalized_label = normalize_topic_label(label)
    reviewed_at = datetime.now(timezone.utc)
    confirmed_topic = ContentConfirmedTopic(
        content_id=content.id,
        creator_id=creator.id,
        canonical_label=label,
        normalized_label=normalized_label,
        created_at=reviewed_at,
        updated_at=reviewed_at,
    )
    session.add(confirmed_topic)
    session.flush()

    candidate = ContentTopicCandidate(
        content_id=content.id,
        creator_id=creator.id,
        extraction_artifact_id=artifact.id,
        confirmed_topic_id=confirmed_topic.id,
        suggested_label=label,
        normalized_label=normalized_label,
        suggestion_method="text_keywords",
        candidate_rank=candidate_rank,
        review_status=CONTENT_TOPIC_REVIEW_STATUS_CONFIRMED,
        reviewed_at=reviewed_at,
        created_at=reviewed_at,
    )
    session.add(candidate)
    session.flush()
    return confirmed_topic


def _create_booking(
    session: Session,
    *,
    creator: Creator,
    booking_link: BookingLink,
    content: Content,
    booking_uuid: str,
    booked_at: datetime,
) -> Booking:
    booking = Booking(
        creator_id=creator.id,
        booking_link_id=booking_link.id,
        tid=content.tid,
        calendly_booking_uuid=booking_uuid,
        email=f"{booking_uuid.lower()}@example.com",
        status="created",
        booked_at=booked_at,
    )
    session.add(booking)
    session.flush()
    return booking


def _create_paid_invoice(
    session: Session,
    *,
    creator: Creator,
    booking: Booking,
    stripe_invoice_id: str,
    amount_cents: int,
    paid_at: datetime,
) -> Invoice:
    invoice = Invoice(
        creator_id=creator.id,
        booking_id=booking.id,
        tid=booking.tid,
        stripe_account_id=creator.stripe_account_id,
        stripe_invoice_id=stripe_invoice_id,
        amount_cents=amount_cents,
        currency="USD",
        status="paid",
        issued_at=paid_at - timedelta(hours=1),
        paid_at=paid_at,
    )
    session.add(invoice)
    session.flush()
    return invoice


def _create_matched_payment_event(
    session: Session,
    *,
    creator: Creator,
    booking: Booking,
    invoice: Invoice,
    stripe_event_id: str,
    paid_at: datetime,
) -> InvoicePaymentEvent:
    event = InvoicePaymentEvent(
        stripe_event_id=stripe_event_id,
        stripe_event_type="invoice.paid",
        stripe_account_id=creator.stripe_account_id,
        stripe_invoice_id=invoice.stripe_invoice_id,
        invoice_id=invoice.id,
        creator_id=creator.id,
        booking_id=booking.id,
        tid=booking.tid,
        status="applied",
        unattributed_reason=None,
        paid_at=paid_at,
        received_at=paid_at,
        processed_at=paid_at,
    )
    session.add(event)
    session.flush()
    return event


def _create_unmatched_payment_event(
    session: Session,
    *,
    creator: Creator,
    stripe_event_id: str,
    stripe_invoice_id: str,
    reason: str,
    paid_at: datetime,
    booking: Booking | None = None,
    tid: str | None = None,
) -> InvoicePaymentEvent:
    event = InvoicePaymentEvent(
        stripe_event_id=stripe_event_id,
        stripe_event_type="invoice.paid",
        stripe_account_id=creator.stripe_account_id,
        stripe_invoice_id=stripe_invoice_id,
        invoice_id=None,
        creator_id=creator.id,
        booking_id=booking.id if booking is not None else None,
        tid=tid if tid is not None else (booking.tid if booking is not None else None),
        status="unmatched",
        unattributed_reason=reason,
        paid_at=paid_at,
        received_at=paid_at,
        processed_at=None,
    )
    session.add(event)
    session.flush()
    return event


def _create_blocked_billing_case(
    session: Session,
    *,
    creator: Creator,
    booking: Booking,
    blocked_at: datetime,
) -> BlockedBillingCase:
    blocked_case = BlockedBillingCase(
        creator_id=creator.id,
        booking_id=booking.id,
        invoice_id=None,
        tid=booking.tid,
        calendly_booking_uuid=booking.calendly_booking_uuid,
        stripe_account_id=creator.stripe_account_id,
        frozen_amount_cents=19500,
        frozen_currency="USD",
        status="open",
        reason_code=BLOCKED_BILLING_REASON_CREATOR_NOT_BILLABLE,
        provider_operation=None,
        provider_http_status=None,
        provider_error_code=None,
        first_blocked_at=blocked_at,
        last_blocked_at=blocked_at,
        last_retry_at=None,
        resolved_at=None,
        resolution_code=None,
    )
    session.add(blocked_case)
    session.flush()
    return blocked_case


def seed_reporting_contract_fixture(*, engine=None) -> ReportingGoldenFixture:
    engine = engine or reporting_test_engine()
    filter_start = date(2026, 3, 8)
    filter_end = date(2026, 3, 8)

    with Session(engine) as session:
        creator, user = _create_creator_with_user(
            session,
            suffix="contract",
            stripe_account_id="acct_reports_golden",
        )

        active_booking_link_name = "Discovery Call CTA"
        historical_booking_link_name = "Archived Webinar CTA"

        active_link = _create_booking_link(
            session,
            creator=creator,
            name=active_booking_link_name,
            destination_url="https://calendly.com/example/discovery-call-cta",
            billing_amount_cents=19500,
            billing_currency="USD",
        )
        historical_link = _create_booking_link(
            session,
            creator=creator,
            name="Legacy Webinar CTA",
            destination_url="https://calendly.com/example/legacy-webinar-cta",
            billing_amount_cents=5000,
            billing_currency="USD",
        )

        primary_tid = "reportsgoldenprimary"
        historical_tid = "reportsgoldenhistorical"
        primary_source_url = "https://example.com/posts/reports-golden-primary"
        historical_source_url = "https://example.com/posts/reports-golden-historical"

        primary_content = _create_content(
            session,
            creator=creator,
            booking_link=active_link,
            source_url=primary_source_url,
            tid=primary_tid,
        )
        historical_content = _create_content(
            session,
            creator=creator,
            booking_link=historical_link,
            source_url=historical_source_url,
            tid=historical_tid,
        )

        primary_artifact = _create_content_extraction_artifact(
            session,
            creator=creator,
            content=primary_content,
            title="Reports Golden Primary",
            extracted_text="Primary reporting golden fixture content.",
        )
        primary_content.authoritative_extraction_artifact_id = primary_artifact.id
        historical_artifact = _create_content_extraction_artifact(
            session,
            creator=creator,
            content=historical_content,
            title="Reports Golden Historical",
            extracted_text="Historical reporting golden fixture content.",
        )
        historical_content.authoritative_extraction_artifact_id = historical_artifact.id
        session.flush()

        _attach_confirmed_topic(
            session,
            creator=creator,
            content=primary_content,
            artifact=primary_artifact,
            label="Pricing Strategy",
            candidate_rank=1,
        )
        _attach_confirmed_topic(
            session,
            creator=creator,
            content=primary_content,
            artifact=primary_artifact,
            label="Discovery Calls",
            candidate_rank=2,
        )
        _attach_confirmed_topic(
            session,
            creator=creator,
            content=historical_content,
            artifact=historical_artifact,
            label="Retention Reviews",
            candidate_rank=1,
        )

        paid_booking_uuid = "BOOK_REPORTS_GOLDEN_PAID"
        waiting_booking_uuid = "BOOK_REPORTS_GOLDEN_WAITING"
        blocked_booking_uuid = "BOOK_REPORTS_GOLDEN_BLOCKED"

        paid_booking = _create_booking(
            session,
            creator=creator,
            booking_link=active_link,
            content=primary_content,
            booking_uuid=paid_booking_uuid,
            booked_at=datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc),
        )
        waiting_booking = _create_booking(
            session,
            creator=creator,
            booking_link=active_link,
            content=primary_content,
            booking_uuid=waiting_booking_uuid,
            booked_at=datetime(2026, 3, 8, 10, 0, tzinfo=timezone.utc),
        )
        blocked_booking = _create_booking(
            session,
            creator=creator,
            booking_link=active_link,
            content=primary_content,
            booking_uuid=blocked_booking_uuid,
            booked_at=datetime(2026, 3, 8, 11, 0, tzinfo=timezone.utc),
        )
        historical_booking = _create_booking(
            session,
            creator=creator,
            booking_link=historical_link,
            content=historical_content,
            booking_uuid="BOOK_REPORTS_GOLDEN_HISTORICAL",
            booked_at=datetime(2026, 3, 7, 8, 0, tzinfo=timezone.utc),
        )

        provider_invoice_id = "in_reports_golden_paid"
        provider_event_id = "evt_reports_golden_paid"
        paid_invoice = _create_paid_invoice(
            session,
            creator=creator,
            booking=paid_booking,
            stripe_invoice_id=provider_invoice_id,
            amount_cents=19500,
            paid_at=datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc),
        )
        _create_matched_payment_event(
            session,
            creator=creator,
            booking=paid_booking,
            invoice=paid_invoice,
            stripe_event_id=provider_event_id,
            paid_at=datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc),
        )
        _create_paid_invoice(
            session,
            creator=creator,
            booking=historical_booking,
            stripe_invoice_id="in_reports_golden_historical",
            amount_cents=5000,
            paid_at=datetime(2026, 3, 7, 9, 0, tzinfo=timezone.utc),
        )

        _create_blocked_billing_case(
            session,
            creator=creator,
            booking=blocked_booking,
            blocked_at=datetime(2026, 3, 8, 11, 5, tzinfo=timezone.utc),
        )
        _create_unmatched_payment_event(
            session,
            creator=creator,
            stripe_event_id="evt_reports_golden_unknown_invoice",
            stripe_invoice_id="in_reports_golden_unknown_invoice",
            reason=UNATTRIBUTED_REASON_UNKNOWN_STRIPE_INVOICE_ID,
            paid_at=datetime(2026, 3, 8, 11, 30, tzinfo=timezone.utc),
            booking=waiting_booking,
            tid=primary_tid,
        )
        _create_unmatched_payment_event(
            session,
            creator=creator,
            stripe_event_id="evt_reports_golden_missing_tid",
            stripe_invoice_id="in_reports_golden_missing_tid",
            reason=UNATTRIBUTED_REASON_MISSING_TID,
            paid_at=datetime(2026, 3, 8, 11, 40, tzinfo=timezone.utc),
        )

        historical_link.name = historical_booking_link_name
        historical_link.billing_amount_cents = None
        historical_link.billing_currency = None
        historical_link.destination_url = "https://calendly.com/example/archived-webinar-cta"
        session.commit()

        creator_id = creator.id
        user_id = user.id
        email = user.email

    return ReportingGoldenFixture(
        creator_id=creator_id,
        user_id=user_id,
        email=email,
        access_token=_access_token(
            user_id=str(user_id),
            creator_id=str(creator_id),
            email=email,
            expires_delta=timedelta(hours=24),
        ),
        primary_tid=primary_tid,
        primary_source_url=primary_source_url,
        historical_tid=historical_tid,
        historical_source_url=historical_source_url,
        active_booking_link_name=active_booking_link_name,
        historical_booking_link_name=historical_booking_link_name,
        paid_booking_uuid=paid_booking_uuid,
        waiting_booking_uuid=waiting_booking_uuid,
        blocked_booking_uuid=blocked_booking_uuid,
        provider_invoice_id=provider_invoice_id,
        provider_event_id=provider_event_id,
        filter_start_date=filter_start,
        filter_end_date=filter_end,
    )
