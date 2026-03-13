import os
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.main import app
from app.models.auth_user import AuthUser
from app.models.booking import Booking
from app.models.booking_link import BookingLink
from app.models.content import Content
from app.models.content_confirmed_topic import ContentConfirmedTopic
from app.models.content_extraction_artifact import ContentExtractionArtifact
from app.models.content_fetch_snapshot import ContentFetchSnapshot
from app.models.content_topic_candidate import ContentTopicCandidate
from app.models.creator import Creator
from app.models.creator_experiment_run import CreatorExperimentRunRecord
from app.models.invoice import Invoice
from app.models.invoice_payment_event import InvoicePaymentEvent
from app.services.authoritative_content_evidence import get_authoritative_content_evidence
from app.services.booking_attribution import (
    BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED,
    BOOKING_UNATTRIBUTED_REASON_MISSING_TID,
)
from app.services.browser_session import SESSION_COOKIE_NAME
from app.services.creator_claim_snapshots import resolve_creator_claim_snapshot
from app.services.next_content_experiments import (
    EXPERIMENT_RUN_STATUS_READY,
    EXPERIMENT_RUN_STATUS_UNSUPPORTED,
    UNSUPPORTED_EXPERIMENTS_SUMMARY,
)

HTML_ACCEPT_HEADERS = {"Accept": "text/html,application/xhtml+xml"}


def _engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def _access_token(*, user_id: uuid.UUID, creator_id: uuid.UUID, email: str) -> str:
    settings = get_settings()
    issued_at = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "creator_id": str(creator_id),
        "email": email,
        "iat": issued_at,
        "exp": issued_at + timedelta(hours=24),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def _create_browser_creator_fixture(
    session: Session,
    *,
    suffix: str,
) -> tuple[Creator, AuthUser, BookingLink, str]:
    creator = Creator(
        name=f"Phase 13 Creator {suffix}",
        stripe_connect_status="connected",
        stripe_account_id=f"acct_phase13_{suffix}",
    )
    session.add(creator)
    session.flush()

    user = AuthUser(
        creator_id=creator.id,
        email=f"phase13_{suffix}@example.com",
    )
    session.add(user)
    session.flush()

    booking_link = BookingLink(
        creator_id=creator.id,
        name=f"Phase 13 Link {suffix}",
        calendly_url=f"https://calendly.com/example/phase13-{suffix}",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    session.add(booking_link)
    session.flush()

    return creator, user, booking_link, _access_token(
        user_id=user.id,
        creator_id=creator.id,
        email=user.email,
    )


def _create_content(
    session: Session,
    *,
    creator: Creator,
    booking_link: BookingLink,
    tid: str,
    source_url: str,
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


def _create_authoritative_artifact(
    session: Session,
    *,
    content: Content,
    topic_labels: list[str],
    fetched_at: datetime,
    title: str,
) -> ContentExtractionArtifact:
    fetch_snapshot = ContentFetchSnapshot(
        content_id=content.id,
        creator_id=content.creator_id,
        requested_url=content.source_url,
        fetched_url=content.source_url,
        fetch_status="succeeded",
        http_status=200,
        failure_reason_code=None,
        failure_detail=None,
        response_content_type="text/html",
        response_content_charset="utf-8",
        snapshot_text=f"<html><body><article><p>{title}</p></article></body></html>",
        fetched_at=fetched_at,
    )
    session.add(fetch_snapshot)
    session.flush()

    artifact = ContentExtractionArtifact(
        content_id=content.id,
        creator_id=content.creator_id,
        fetch_snapshot_id=fetch_snapshot.id,
        extraction_status="succeeded",
        extraction_reason_code=None,
        extraction_detail=None,
        extraction_method="html_article",
        title=title,
        published_at=None,
        published_at_raw=None,
        source_text_char_count=32,
        extracted_text_char_count=32,
        extracted_text_word_count=5,
        extracted_text=f"Artifact text for {title}",
        created_at=fetched_at + timedelta(minutes=5),
    )
    session.add(artifact)
    session.flush()

    for index, topic_label in enumerate(topic_labels, start=1):
        topic = ContentConfirmedTopic(
            content_id=content.id,
            creator_id=content.creator_id,
            canonical_label=topic_label,
            normalized_label=topic_label.casefold(),
        )
        session.add(topic)
        session.flush()
        session.add(
            ContentTopicCandidate(
                content_id=content.id,
                creator_id=content.creator_id,
                extraction_artifact_id=artifact.id,
                confirmed_topic_id=topic.id,
                suggested_label=topic_label,
                normalized_label=topic_label.casefold(),
                suggestion_method="text_keywords",
                candidate_rank=index,
                review_status="confirmed",
                reviewed_at=fetched_at + timedelta(minutes=6),
            )
        )

    content.authoritative_extraction_artifact_id = artifact.id
    session.flush()
    return artifact


def _create_paid_booking(
    session: Session,
    *,
    creator: Creator,
    booking_link: BookingLink,
    content: Content,
    booking_uuid: str,
    paid_at: datetime,
    amount_cents: int,
    stripe_invoice_id: str,
    stripe_event_id: str,
) -> tuple[Booking, Invoice, InvoicePaymentEvent]:
    booking = Booking(
        creator_id=creator.id,
        booking_link_id=booking_link.id,
        tid=content.tid,
        calendly_booking_uuid=booking_uuid,
        email=f"{booking_uuid.lower()}@example.com",
        status="created",
        attribution_status="attributed",
        unattributed_reason=None,
        booked_at=paid_at - timedelta(hours=2),
    )
    session.add(booking)
    session.flush()

    invoice = Invoice(
        creator_id=creator.id,
        booking_id=booking.id,
        tid=content.tid,
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

    payment_event = InvoicePaymentEvent(
        stripe_event_id=stripe_event_id,
        stripe_event_type="invoice.paid",
        stripe_account_id=creator.stripe_account_id,
        stripe_invoice_id=stripe_invoice_id,
        invoice_id=invoice.id,
        creator_id=creator.id,
        booking_id=booking.id,
        tid=content.tid,
        status="applied",
        unattributed_reason=None,
        paid_at=paid_at,
        received_at=paid_at,
        processed_at=paid_at,
    )
    session.add(payment_event)
    session.flush()

    return booking, invoice, payment_event


def test_phase13_helper_stays_honest_cited_and_snapshot_reproducible_end_to_end():
    engine = _engine()
    suffix = uuid.uuid4().hex[:8]
    original_topic = "Retention Reviews"
    promoted_topic = "Lifecycle Audits"
    original_title = "Phase 13 Original Artifact"
    promoted_title = "Phase 13 Promoted Artifact"
    booking_uuid = f"BOOK_PHASE13_READY_{suffix}"
    unattributed_booking_uuid = f"BOOK_PHASE13_UNATTRIBUTED_{suffix}"
    stripe_invoice_id = f"in_phase13_ready_{suffix}"
    stripe_event_id = f"evt_phase13_ready_{suffix}"

    with Session(engine) as session:
        creator, user, booking_link, access_token = _create_browser_creator_fixture(
            session,
            suffix=suffix,
        )
        creator_id = creator.id
        content = _create_content(
            session,
            creator=creator,
            booking_link=booking_link,
            tid=f"phase13{suffix}",
            source_url="https://example.com/posts/phase13-validation",
        )
        content_id = content.id
        content_tid = content.tid
        booking_link_id = booking_link.id
        original_artifact = _create_authoritative_artifact(
            session,
            content=content,
            topic_labels=[original_topic],
            fetched_at=datetime(2026, 3, 13, 10, 0, tzinfo=timezone.utc),
            title=original_title,
        )
        original_artifact_id = original_artifact.id
        session.add(
            Booking(
                creator_id=creator.id,
                booking_link_id=booking_link.id,
                tid=None,
                calendly_booking_uuid=unattributed_booking_uuid,
                email="phase13-unattributed@example.com",
                status="created",
                attribution_status=BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED,
                unattributed_reason=BOOKING_UNATTRIBUTED_REASON_MISSING_TID,
                booked_at=datetime(2026, 3, 13, 11, 0, tzinfo=timezone.utc),
            )
        )
        session.commit()

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)

        unsupported_create_response = client.post(
            "/app/experiments",
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )
        unsupported_page_response = client.get(
            unsupported_create_response.headers["location"],
            headers=HTML_ACCEPT_HEADERS,
            )

        unsupported_run_claim_snapshot_id = uuid.UUID(
            parse_qs(urlparse(unsupported_create_response.headers["location"]).query)[
                "claim_snapshot_id"
            ][0]
        )

        with Session(engine) as session:
            creator = session.get(Creator, creator_id)
            booking_link = session.get(BookingLink, booking_link_id)
            content = session.get(Content, content_id)
            _create_paid_booking(
                session,
                creator=creator,
                booking_link=booking_link,
                content=content,
                booking_uuid=booking_uuid,
                paid_at=datetime(2026, 3, 13, 14, 0, tzinfo=timezone.utc),
                amount_cents=19500,
                stripe_invoice_id=stripe_invoice_id,
                stripe_event_id=stripe_event_id,
            )
            session.commit()

        ready_create_response = client.post(
            "/app/experiments",
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )
        ready_page_response = client.get(
            ready_create_response.headers["location"],
            headers=HTML_ACCEPT_HEADERS,
        )

        ready_run_claim_snapshot_id = uuid.UUID(
            parse_qs(urlparse(ready_create_response.headers["location"]).query)[
                "claim_snapshot_id"
            ][0]
        )
        evidence_response_before_drift = client.get(
            f"/app/experiments/{ready_run_claim_snapshot_id}/cards/1",
            headers=HTML_ACCEPT_HEADERS,
        )

    with Session(engine) as session:
        content = session.get(Content, content_id)
        unsupported_run = session.execute(
            select(CreatorExperimentRunRecord)
            .options(selectinload(CreatorExperimentRunRecord.cards))
            .where(CreatorExperimentRunRecord.id == unsupported_run_claim_snapshot_id)
        ).scalar_one()
        ready_run = session.execute(
            select(CreatorExperimentRunRecord)
            .options(selectinload(CreatorExperimentRunRecord.cards))
            .where(CreatorExperimentRunRecord.id == ready_run_claim_snapshot_id)
        ).scalar_one()
        unsupported_run_status = unsupported_run.status
        unsupported_run_summary = unsupported_run.summary_text
        unsupported_run_card_count = len(unsupported_run.cards)
        ready_run_status = ready_run.status
        ready_run_card_count = len(ready_run.cards)
        ready_card_claim_snapshot_id = ready_run.cards[0].claim_snapshot_id
        resolved_snapshot_before_drift = resolve_creator_claim_snapshot(
            creator_id=creator_id,
            claim_snapshot_id=ready_card_claim_snapshot_id,
            db=session,
        )
        current_authoritative_before_drift = get_authoritative_content_evidence(
            content=content,
            db=session,
        )
        current_authoritative_before_artifact_id = current_authoritative_before_drift.artifact.id
        resolved_snapshot_before_artifact_id = (
            resolved_snapshot_before_drift.authoritative_content_evidence.artifact.id
        )
        resolved_snapshot_before_topics = [
            topic.canonical_label
            for topic in resolved_snapshot_before_drift.authoritative_content_evidence.confirmed_topics
        ]
        resolved_snapshot_before_invoice_ids = [
            row.stripe_invoice_id for row in resolved_snapshot_before_drift.settled_paid_evidence_rows
        ]
        promoted_artifact = _create_authoritative_artifact(
            session,
            content=content,
            topic_labels=[promoted_topic],
            fetched_at=datetime(2026, 3, 13, 16, 0, tzinfo=timezone.utc),
            title=promoted_title,
        )
        promoted_artifact_id = promoted_artifact.id
        session.commit()

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        evidence_response_after_drift = client.get(
            f"/app/experiments/{ready_run_claim_snapshot_id}/cards/1",
            headers=HTML_ACCEPT_HEADERS,
        )

    with Session(engine) as session:
        content = session.get(Content, content_id)
        current_authoritative_after_drift = get_authoritative_content_evidence(
            content=content,
            db=session,
        )
        resolved_snapshot_after_drift = resolve_creator_claim_snapshot(
            creator_id=creator_id,
            claim_snapshot_id=ready_card_claim_snapshot_id,
            db=session,
        )
        current_authoritative_after_artifact_id = current_authoritative_after_drift.artifact.id
        current_authoritative_after_topics = [
            topic.canonical_label for topic in current_authoritative_after_drift.confirmed_topics
        ]
        resolved_snapshot_after_artifact_id = (
            resolved_snapshot_after_drift.authoritative_content_evidence.artifact.id
        )
        resolved_snapshot_after_topics = [
            topic.canonical_label
            for topic in resolved_snapshot_after_drift.authoritative_content_evidence.confirmed_topics
        ]
        resolved_snapshot_after_invoice_ids = [
            row.stripe_invoice_id for row in resolved_snapshot_after_drift.settled_paid_evidence_rows
        ]

    assert unsupported_create_response.status_code == 303
    assert unsupported_create_response.headers["location"].startswith(
        "/app/experiments?status=generated&claim_snapshot_id="
    )
    assert unsupported_page_response.status_code == 200
    assert unsupported_run_status == EXPERIMENT_RUN_STATUS_UNSUPPORTED
    assert unsupported_run_summary == UNSUPPORTED_EXPERIMENTS_SUMMARY
    assert unsupported_run_card_count == 0
    assert "Not enough trusted evidence yet" in unsupported_page_response.text
    assert UNSUPPORTED_EXPERIMENTS_SUMMARY in unsupported_page_response.text
    assert "Why this helper is still unsupported" in unsupported_page_response.text
    assert "No settled attributed paid results exist yet for this workspace." in (
        unsupported_page_response.text
    )
    assert "Some newer activity is still excluded here" in unsupported_page_response.text
    assert BOOKING_UNATTRIBUTED_REASON_MISSING_TID not in unsupported_page_response.text
    assert unattributed_booking_uuid not in unsupported_page_response.text

    assert ready_create_response.status_code == 303
    assert ready_create_response.headers["location"].startswith(
        "/app/experiments?status=generated&claim_snapshot_id="
    )
    assert ready_page_response.status_code == 200
    assert ready_run_status == EXPERIMENT_RUN_STATUS_READY
    assert ready_run_card_count == 1
    assert "Fresh snapshot ready" in ready_page_response.text
    assert "Here is the next content experiment most grounded" in ready_page_response.text
    assert f"Test another {original_topic} angle" in ready_page_response.text
    assert (
        f"Test whether another post about {original_topic} may lead to more attributed paid bookings."
        in ready_page_response.text
    )
    assert content_tid in ready_page_response.text
    assert BOOKING_UNATTRIBUTED_REASON_MISSING_TID not in ready_page_response.text
    assert unattributed_booking_uuid not in ready_page_response.text

    assert evidence_response_before_drift.status_code == 200
    assert "Experiment evidence" in evidence_response_before_drift.text
    assert "Authoritative content used" in evidence_response_before_drift.text
    assert "Settled paid results used" in evidence_response_before_drift.text
    assert "Parent run snapshot" in evidence_response_before_drift.text
    assert "Card snapshot" in evidence_response_before_drift.text
    assert original_title in evidence_response_before_drift.text
    assert original_topic in evidence_response_before_drift.text
    assert promoted_title not in evidence_response_before_drift.text
    assert promoted_topic not in evidence_response_before_drift.text
    assert "USD 195.00" in evidence_response_before_drift.text
    assert stripe_invoice_id not in evidence_response_before_drift.text
    assert stripe_event_id not in evidence_response_before_drift.text
    assert booking_uuid not in evidence_response_before_drift.text
    assert unattributed_booking_uuid not in evidence_response_before_drift.text
    assert BOOKING_UNATTRIBUTED_REASON_MISSING_TID not in evidence_response_before_drift.text

    assert current_authoritative_before_drift is not None
    assert current_authoritative_before_artifact_id == original_artifact_id
    assert resolved_snapshot_before_drift is not None
    assert resolved_snapshot_before_artifact_id == original_artifact_id
    assert resolved_snapshot_before_topics == [original_topic]
    assert resolved_snapshot_before_invoice_ids == [stripe_invoice_id]

    assert current_authoritative_after_drift is not None
    assert current_authoritative_after_artifact_id == promoted_artifact_id
    assert current_authoritative_after_topics == [promoted_topic]
    assert resolved_snapshot_after_drift is not None
    assert resolved_snapshot_after_artifact_id == original_artifact_id
    assert resolved_snapshot_after_topics == [original_topic]
    assert resolved_snapshot_after_invoice_ids == [stripe_invoice_id]

    assert evidence_response_after_drift.status_code == 200
    assert original_title in evidence_response_after_drift.text
    assert original_topic in evidence_response_after_drift.text
    assert promoted_title not in evidence_response_after_drift.text
    assert promoted_topic not in evidence_response_after_drift.text
    assert stripe_invoice_id not in evidence_response_after_drift.text
    assert stripe_event_id not in evidence_response_after_drift.text
