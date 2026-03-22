import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, selectinload

from app.models.booking import Booking
from app.models.booking_link import BookingLink
from app.models.content import Content
from app.models.content_confirmed_topic import ContentConfirmedTopic
from app.models.content_extraction_artifact import ContentExtractionArtifact
from app.models.content_fetch_snapshot import ContentFetchSnapshot
from app.models.content_topic_candidate import ContentTopicCandidate
from app.models.creator import Creator
from app.models.creator_claim_snapshot import CreatorClaimSnapshotRecord
from app.models.invoice import Invoice
from app.models.invoice_payment_event import InvoicePaymentEvent
from app.services.authoritative_content_evidence import get_authoritative_content_evidence
from app.services.creator_claim_snapshots import (
    CreateCreatorClaimSnapshotInput,
    create_creator_claim_snapshot,
    resolve_creator_claim_snapshot,
)
from app.services.settled_paid_evidence import get_creator_settled_paid_evidence


def _engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def _create_authoritative_artifact(
    session: Session,
    *,
    content: Content,
    requested_url: str,
    title: str,
    topic_label: str,
    fetched_at: datetime,
    created_at: datetime,
) -> tuple[ContentFetchSnapshot, ContentExtractionArtifact]:
    fetch_snapshot = ContentFetchSnapshot(
        content_id=content.id,
        creator_id=content.creator_id,
        requested_url=requested_url,
        fetched_url=requested_url,
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

    extracted_text = f"{title} supporting text"
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
        source_text_char_count=len(extracted_text),
        extracted_text_char_count=len(extracted_text),
        extracted_text_word_count=len(extracted_text.split()),
        extracted_text=extracted_text,
        created_at=created_at,
    )
    session.add(artifact)

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
            candidate_rank=1,
            review_status="confirmed",
            reviewed_at=created_at,
        )
    )
    session.flush()
    return fetch_snapshot, artifact


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
    stripe_event_id: str | None = None,
) -> tuple[Booking, Invoice, InvoicePaymentEvent | None]:
    booking = Booking(
        creator_id=creator.id,
        booking_link_id=booking_link.id,
        tid=content.tid,
        calendly_booking_uuid=booking_uuid,
        email=f"{booking_uuid.lower()}@example.com",
        status="created",
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

    payment_event = None
    if stripe_event_id is not None:
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


def _seed_claim_snapshot_fixture(session: Session) -> dict[str, object]:
    creator = Creator(
        name="Claim Snapshot Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_claim_snapshot",
    )
    session.add(creator)
    session.flush()

    booking_link = BookingLink(
        creator_id=creator.id,
        name="Claim Snapshot Link",
        calendly_url="https://calendly.com/example/claim-snapshot",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    session.add(booking_link)
    session.flush()

    content = Content(
        creator_id=creator.id,
        booking_link_id=booking_link.id,
        source_url="https://example.com/posts/claim-snapshot",
        tid="claim_snapshot_tid",
    )
    session.add(content)
    session.flush()

    original_fetch_snapshot, original_artifact = _create_authoritative_artifact(
        session,
        content=content,
        requested_url="https://example.com/posts/claim-snapshot?revision=1",
        title="Original authoritative artifact",
        topic_label="Discovery Call Pricing",
        fetched_at=datetime(2026, 3, 11, 14, 0, tzinfo=timezone.utc),
        created_at=datetime(2026, 3, 11, 14, 5, tzinfo=timezone.utc),
    )
    content.authoritative_extraction_artifact_id = original_artifact.id
    session.flush()

    _create_paid_booking(
        session,
        creator=creator,
        booking_link=booking_link,
        content=content,
        booking_uuid="BOOK_CLAIM_SNAPSHOT_A",
        paid_at=datetime(2026, 3, 11, 15, 0, tzinfo=timezone.utc),
        amount_cents=19500,
        stripe_invoice_id="in_claim_snapshot_a",
        stripe_event_id="evt_claim_snapshot_a",
    )
    _create_paid_booking(
        session,
        creator=creator,
        booking_link=booking_link,
        content=content,
        booking_uuid="BOOK_CLAIM_SNAPSHOT_B",
        paid_at=datetime(2026, 3, 11, 16, 0, tzinfo=timezone.utc),
        amount_cents=25000,
        stripe_invoice_id="in_claim_snapshot_b",
        stripe_event_id=None,
    )
    session.flush()

    authoritative_evidence = get_authoritative_content_evidence(content=content, db=session)
    settled_snapshot = get_creator_settled_paid_evidence(
        creator_id=creator.id,
        db=session,
        tid=content.tid,
    )
    assert authoritative_evidence is not None

    claim_snapshot = create_creator_claim_snapshot(
        creator_id=creator.id,
        input=CreateCreatorClaimSnapshotInput(
            claim_kind="reports_paid_content_summary",
            content_id=content.id,
            authoritative_extraction_artifact_id=authoritative_evidence.artifact.id,
            authoritative_fetch_snapshot_id=authoritative_evidence.fetch_snapshot.id,
            settled_paid_evidence_rows=settled_snapshot.settled_rows,
            claim_contract_version="reports_paid_content_summary.v1",
            rendered_claim_text="This content has settled paid evidence.",
        ),
        db=session,
    )
    session.flush()

    return {
        "creator_id": creator.id,
        "booking_link_id": booking_link.id,
        "content_id": content.id,
        "content_tid": content.tid,
        "snapshot_id": claim_snapshot.id,
        "original_artifact_id": original_artifact.id,
        "original_fetch_snapshot_id": original_fetch_snapshot.id,
        "original_invoice_ids": [row.invoice_id for row in settled_snapshot.settled_rows],
        "original_booking_ids": [row.booking_id for row in settled_snapshot.settled_rows],
        "original_payment_event_ids": [row.payment_event_id for row in settled_snapshot.settled_rows],
    }


def test_create_creator_claim_snapshot_persists_versions_and_exact_paid_evidence_refs():
    engine = _engine()

    with Session(engine) as session:
        seeded = _seed_claim_snapshot_fixture(session)
        session.commit()

    with Session(engine) as session:
        snapshot_record = session.execute(
            select(CreatorClaimSnapshotRecord)
            .options(selectinload(CreatorClaimSnapshotRecord.paid_evidence_references))
            .where(CreatorClaimSnapshotRecord.id == seeded["snapshot_id"])
        ).scalar_one()

    assert snapshot_record.claim_kind == "reports_paid_content_summary"
    assert snapshot_record.claim_generator_type is None
    assert snapshot_record.claim_model_name is None
    assert snapshot_record.claim_config_version is None
    assert snapshot_record.claim_contract_version == "reports_paid_content_summary.v1"
    assert snapshot_record.claim_reducer_version is None
    assert snapshot_record.claim_prompt_version is None
    assert snapshot_record.rendered_claim_text == "This content has settled paid evidence."
    assert snapshot_record.content_id == seeded["content_id"]
    assert snapshot_record.authoritative_extraction_artifact_id == seeded["original_artifact_id"]
    assert snapshot_record.authoritative_fetch_snapshot_id == seeded["original_fetch_snapshot_id"]
    assert [
        (
            reference.evidence_order,
            reference.booking_id,
            reference.invoice_id,
            reference.payment_event_id,
        )
        for reference in snapshot_record.paid_evidence_references
    ] == [
        (
            index,
            booking_id,
            invoice_id,
            payment_event_id,
        )
        for index, (booking_id, invoice_id, payment_event_id) in enumerate(
            zip(
                seeded["original_booking_ids"],
                seeded["original_invoice_ids"],
                seeded["original_payment_event_ids"],
                strict=True,
            ),
            start=1,
        )
    ]


def test_resolve_creator_claim_snapshot_uses_stored_ids_instead_of_current_authority_or_current_paid_rows():
    engine = _engine()

    with Session(engine) as session:
        seeded = _seed_claim_snapshot_fixture(session)
        content = session.execute(select(Content).where(Content.id == seeded["content_id"])).scalar_one()
        creator = session.execute(select(Creator).where(Creator.id == seeded["creator_id"])).scalar_one()
        booking_link = session.execute(
            select(BookingLink).where(BookingLink.id == seeded["booking_link_id"])
        ).scalar_one()

        _, newer_artifact = _create_authoritative_artifact(
            session,
            content=content,
            requested_url="https://example.com/posts/claim-snapshot?revision=2",
            title="Newly promoted authoritative artifact",
            topic_label="Retainer Onboarding",
            fetched_at=datetime(2026, 3, 11, 18, 0, tzinfo=timezone.utc),
            created_at=datetime(2026, 3, 11, 18, 5, tzinfo=timezone.utc),
        )
        content.authoritative_extraction_artifact_id = newer_artifact.id
        session.flush()

        _create_paid_booking(
            session,
            creator=creator,
            booking_link=booking_link,
            content=content,
            booking_uuid="BOOK_CLAIM_SNAPSHOT_C",
            paid_at=datetime(2026, 3, 11, 19, 0, tzinfo=timezone.utc),
            amount_cents=5000,
            stripe_invoice_id="in_claim_snapshot_c",
            stripe_event_id="evt_claim_snapshot_c",
        )
        session.commit()

    with Session(engine) as session:
        content = session.execute(select(Content).where(Content.id == seeded["content_id"])).scalar_one()
        current_authoritative = get_authoritative_content_evidence(content=content, db=session)
        current_settled_snapshot = get_creator_settled_paid_evidence(
            creator_id=seeded["creator_id"],
            db=session,
            tid=seeded["content_tid"],
        )
        resolved = resolve_creator_claim_snapshot(
            creator_id=seeded["creator_id"],
            claim_snapshot_id=seeded["snapshot_id"],
            db=session,
        )

    assert current_authoritative is not None
    assert current_authoritative.artifact.id != seeded["original_artifact_id"]
    assert len(current_settled_snapshot.settled_rows) == 3

    assert resolved is not None
    assert resolved.snapshot.id == seeded["snapshot_id"]
    assert resolved.snapshot.claim_generator_type is None
    assert resolved.snapshot.claim_model_name is None
    assert resolved.snapshot.claim_config_version is None
    assert resolved.snapshot.claim_contract_version == "reports_paid_content_summary.v1"
    assert resolved.authoritative_content_evidence.artifact.id == seeded["original_artifact_id"]
    assert resolved.authoritative_content_evidence.fetch_snapshot.id == seeded["original_fetch_snapshot_id"]
    assert [row.invoice_id for row in resolved.settled_paid_evidence_rows] == seeded["original_invoice_ids"]
