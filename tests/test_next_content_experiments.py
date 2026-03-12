import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, selectinload

from app.models.booking_link import BookingLink
from app.models.content import Content
from app.models.content_confirmed_topic import ContentConfirmedTopic
from app.models.content_extraction_artifact import ContentExtractionArtifact
from app.models.content_fetch_snapshot import ContentFetchSnapshot
from app.models.content_topic_candidate import ContentTopicCandidate
from app.models.creator import Creator
from app.models.creator_claim_snapshot import CreatorClaimSnapshotRecord
from app.models.creator_experiment_run import CreatorExperimentRunRecord
from app.models.invoice import Invoice
from app.models.invoice_payment_event import InvoicePaymentEvent
from app.models.booking import Booking
from app.models.blocked_billing_case import BlockedBillingCase
from app.services.creator_claim_snapshots import resolve_creator_claim_snapshot
from app.services.next_content_experiments import (
    EXPERIMENT_CARD_CLAIM_CONTRACT_VERSION,
    EXPERIMENT_CARD_CLAIM_KIND,
    EXPERIMENT_RUN_CONTRACT_VERSION,
    EXPERIMENT_RUN_REDUCER_VERSION,
    EXPERIMENT_RUN_STATUS_READY,
    EXPERIMENT_RUN_STATUS_UNSUPPORTED,
    UNSUPPORTED_EXPERIMENTS_SUMMARY,
    create_creator_next_content_experiments_run,
    get_creator_next_content_experiment_card_drilldown,
    get_creator_next_content_experiments_run,
    get_current_creator_next_content_experiments_unsupported_explanation,
    get_latest_creator_next_content_experiments_run,
)


def _engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def _create_creator_fixture(session: Session, *, suffix: str) -> tuple[Creator, BookingLink]:
    creator = Creator(
        name=f"Experiment Creator {suffix}",
        stripe_connect_status="connected",
        stripe_account_id=f"acct_experiments_{suffix}",
    )
    session.add(creator)
    session.flush()

    booking_link = BookingLink(
        creator_id=creator.id,
        name=f"Experiment Link {suffix}",
        calendly_url=f"https://calendly.com/example/experiments-{suffix}",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    session.add(booking_link)
    session.flush()
    return creator, booking_link


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
        snapshot_text=f"<html><body><article><p>{content.source_url}</p></article></body></html>",
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
        title=f"Artifact for {content.tid}",
        published_at=None,
        published_at_raw=None,
        source_text_char_count=32,
        extracted_text_char_count=32,
        extracted_text_word_count=5,
        extracted_text=f"Artifact text for {content.tid}",
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
    stripe_event_id: str | None = None,
) -> tuple[Booking, Invoice, InvoicePaymentEvent | None]:
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


def test_create_creator_next_content_experiments_run_returns_unsupported_without_paid_evidence():
    engine = _engine()

    with Session(engine) as session:
        creator, booking_link = _create_creator_fixture(session, suffix="unsupported_no_paid")
        content = _create_content(
            session,
            creator=creator,
            booking_link=booking_link,
            tid="experiments_no_paid",
            source_url="https://example.com/posts/experiments-no-paid",
        )
        _create_authoritative_artifact(
            session,
            content=content,
            topic_labels=["Discovery Call Pricing"],
            fetched_at=datetime(2026, 3, 12, 10, 0, tzinfo=timezone.utc),
        )

        result = create_creator_next_content_experiments_run(
            creator_id=creator.id,
            db=session,
        )
        session.commit()

    with Session(engine) as session:
        run_count = session.execute(
            select(func.count()).select_from(CreatorExperimentRunRecord)
        ).scalar_one()
        card_count = session.execute(
            select(func.count()).select_from(CreatorClaimSnapshotRecord)
        ).scalar_one()

    assert result.status == EXPERIMENT_RUN_STATUS_UNSUPPORTED
    assert result.summary == UNSUPPORTED_EXPERIMENTS_SUMMARY
    assert result.experiments == []
    assert run_count == 1
    assert card_count == 0


def test_create_creator_next_content_experiments_run_returns_unsupported_without_authoritative_topics():
    engine = _engine()

    with Session(engine) as session:
        creator, booking_link = _create_creator_fixture(session, suffix="unsupported_no_topics")
        content = _create_content(
            session,
            creator=creator,
            booking_link=booking_link,
            tid="experiments_no_topics",
            source_url="https://example.com/posts/experiments-no-topics",
        )
        _create_paid_booking(
            session,
            creator=creator,
            booking_link=booking_link,
            content=content,
            booking_uuid="BOOK_EXPERIMENTS_NO_TOPICS",
            paid_at=datetime(2026, 3, 12, 12, 0, tzinfo=timezone.utc),
            amount_cents=19500,
            stripe_invoice_id="in_experiments_no_topics",
            stripe_event_id="evt_experiments_no_topics",
        )

        result = create_creator_next_content_experiments_run(
            creator_id=creator.id,
            db=session,
        )
        session.commit()

    assert result.status == EXPERIMENT_RUN_STATUS_UNSUPPORTED
    assert result.summary == UNSUPPORTED_EXPERIMENTS_SUMMARY
    assert result.experiments == []


def test_create_creator_next_content_experiments_run_persists_ranked_cards_and_child_claim_snapshots():
    engine = _engine()

    with Session(engine) as session:
        creator, booking_link = _create_creator_fixture(session, suffix="ranked")
        creator_id = creator.id
        tids = {
            "a": "experiments_ranked_a",
            "b": "experiments_ranked_b",
            "c": "experiments_ranked_c",
            "d": "experiments_ranked_d",
        }
        contents = {
            key: _create_content(
                session,
                creator=creator,
                booking_link=booking_link,
                tid=tid,
                source_url=f"https://example.com/posts/{tid}",
            )
            for key, tid in tids.items()
        }
        ranked_content_a_id = contents["a"].id
        for index, key in enumerate(("a", "b", "c", "d"), start=1):
            _create_authoritative_artifact(
                session,
                content=contents[key],
                topic_labels=[f"Topic {key.upper()}"],
                fetched_at=datetime(2026, 3, 12, 9 + index, 0, tzinfo=timezone.utc),
            )

        for index in range(3):
            _create_paid_booking(
                session,
                creator=creator,
                booking_link=booking_link,
                content=contents["a"],
                booking_uuid=f"BOOK_EXPERIMENTS_A_{index}",
                paid_at=datetime(2026, 3, 13, 10 + index, 0, tzinfo=timezone.utc),
                amount_cents=10000,
                stripe_invoice_id=f"in_experiments_a_{index}",
                stripe_event_id=f"evt_experiments_a_{index}",
            )
        for index in range(2):
            _create_paid_booking(
                session,
                creator=creator,
                booking_link=booking_link,
                content=contents["b"],
                booking_uuid=f"BOOK_EXPERIMENTS_B_{index}",
                paid_at=datetime(2026, 3, 14, 10 + index, 0, tzinfo=timezone.utc),
                amount_cents=20000,
                stripe_invoice_id=f"in_experiments_b_{index}",
                stripe_event_id=f"evt_experiments_b_{index}",
            )
        for index in range(2):
            _create_paid_booking(
                session,
                creator=creator,
                booking_link=booking_link,
                content=contents["c"],
                booking_uuid=f"BOOK_EXPERIMENTS_C_{index}",
                paid_at=datetime(2026, 3, 15, 10 + index, 0, tzinfo=timezone.utc),
                amount_cents=15000,
                stripe_invoice_id=f"in_experiments_c_{index}",
                stripe_event_id=f"evt_experiments_c_{index}",
            )
        _create_paid_booking(
            session,
            creator=creator,
            booking_link=booking_link,
            content=contents["d"],
            booking_uuid="BOOK_EXPERIMENTS_D",
            paid_at=datetime(2026, 3, 16, 10, 0, tzinfo=timezone.utc),
            amount_cents=50000,
            stripe_invoice_id="in_experiments_d",
            stripe_event_id="evt_experiments_d",
        )

        created = create_creator_next_content_experiments_run(
            creator_id=creator.id,
            db=session,
        )
        latest = get_latest_creator_next_content_experiments_run(
            creator_id=creator.id,
            db=session,
        )
        specific = get_creator_next_content_experiments_run(
            creator_id=creator.id,
            claim_snapshot_id=created.claim_snapshot_id,
            db=session,
        )
        session.commit()

    with Session(engine) as session:
        stored_run = session.execute(
            select(CreatorExperimentRunRecord)
            .options(
                selectinload(CreatorExperimentRunRecord.cards),
            )
            .where(CreatorExperimentRunRecord.id == created.claim_snapshot_id)
        ).scalar_one()
        child_snapshot = session.execute(
            select(CreatorClaimSnapshotRecord).where(
                CreatorClaimSnapshotRecord.id == stored_run.cards[0].claim_snapshot_id
            )
        ).scalar_one()
        resolved_child = resolve_creator_claim_snapshot(
            creator_id=creator_id,
            claim_snapshot_id=child_snapshot.id,
            db=session,
        )

    assert created.status == EXPERIMENT_RUN_STATUS_READY
    assert created.summary.startswith("Here are the next content experiments")
    assert [card.content_tids for card in created.experiments] == [
        [tids["a"]],
        [tids["b"]],
        [tids["c"]],
    ]
    assert latest is not None
    assert latest.claim_snapshot_id == created.claim_snapshot_id
    assert specific is not None
    assert specific.claim_snapshot_id == created.claim_snapshot_id
    assert stored_run.run_contract_version == EXPERIMENT_RUN_CONTRACT_VERSION
    assert stored_run.run_reducer_version == EXPERIMENT_RUN_REDUCER_VERSION
    assert stored_run.status == EXPERIMENT_RUN_STATUS_READY
    assert len(stored_run.cards) == 3
    assert child_snapshot.claim_kind == EXPERIMENT_CARD_CLAIM_KIND
    assert child_snapshot.claim_contract_version == EXPERIMENT_CARD_CLAIM_CONTRACT_VERSION
    assert resolved_child is not None
    assert resolved_child.snapshot.content_id == ranked_content_a_id
    assert [row.tid for row in resolved_child.settled_paid_evidence_rows] == [tids["a"]] * 3


def test_create_creator_next_content_experiments_run_ignores_diagnostic_backlog_in_output():
    engine = _engine()

    with Session(engine) as session:
        creator, booking_link = _create_creator_fixture(session, suffix="diagnostics")
        content = _create_content(
            session,
            creator=creator,
            booking_link=booking_link,
            tid="experiments_diagnostics",
            source_url="https://example.com/posts/experiments-diagnostics",
        )
        booking, _, _ = _create_paid_booking(
            session,
            creator=creator,
            booking_link=booking_link,
            content=content,
            booking_uuid="BOOK_EXPERIMENTS_DIAGNOSTICS",
            paid_at=datetime(2026, 3, 12, 14, 0, tzinfo=timezone.utc),
            amount_cents=19500,
            stripe_invoice_id="in_experiments_diagnostics",
            stripe_event_id="evt_experiments_diagnostics",
        )
        _create_authoritative_artifact(
            session,
            content=content,
            topic_labels=["Retention Reviews"],
            fetched_at=datetime(2026, 3, 12, 13, 0, tzinfo=timezone.utc),
        )
        session.add(
            InvoicePaymentEvent(
                stripe_event_id="evt_experiments_diagnostics_unmatched",
                stripe_event_type="invoice.paid",
                stripe_account_id=creator.stripe_account_id,
                stripe_invoice_id="in_experiments_diagnostics_unmatched",
                invoice_id=None,
                creator_id=creator.id,
                booking_id=None,
                tid=None,
                status="unmatched",
                unattributed_reason="MISSING_TID",
                paid_at=datetime(2026, 3, 12, 15, 0, tzinfo=timezone.utc),
                received_at=datetime(2026, 3, 12, 15, 0, tzinfo=timezone.utc),
                processed_at=None,
            )
        )
        session.add(
            BlockedBillingCase(
                creator_id=creator.id,
                booking_id=booking.id,
                invoice_id=None,
                tid=content.tid,
                calendly_booking_uuid=booking.calendly_booking_uuid,
                stripe_account_id=creator.stripe_account_id,
                frozen_amount_cents=19500,
                frozen_currency="USD",
                status="open",
                reason_code="creator_not_billable",
                provider_operation=None,
                provider_http_status=None,
                provider_error_code=None,
                first_blocked_at=datetime(2026, 3, 12, 15, 5, tzinfo=timezone.utc),
                last_blocked_at=datetime(2026, 3, 12, 15, 5, tzinfo=timezone.utc),
                last_retry_at=None,
                resolved_at=None,
                resolution_code=None,
            )
        )
        session.flush()

        result = create_creator_next_content_experiments_run(
            creator_id=creator.id,
            db=session,
        )
        session.commit()

    combined_text = " ".join(
        [
            result.summary,
            *(
                " ".join(
                    [
                        card.title,
                        card.hypothesis,
                        card.why_this_might_work,
                        card.evidence_summary,
                        card.caution,
                    ]
                )
                for card in result.experiments
            ),
        ]
    )
    assert result.status == EXPERIMENT_RUN_STATUS_READY
    assert "Missing tracking ID" not in combined_text
    assert "creator_not_billable" not in combined_text


def test_get_creator_next_content_experiment_card_drilldown_returns_snapshot_backed_evidence():
    engine = _engine()

    with Session(engine) as session:
        creator, booking_link = _create_creator_fixture(session, suffix="drilldown")
        content = _create_content(
            session,
            creator=creator,
            booking_link=booking_link,
            tid="experiments_drilldown",
            source_url="https://example.com/posts/experiments-drilldown",
        )
        _create_authoritative_artifact(
            session,
            content=content,
            topic_labels=["Retention Reviews"],
            fetched_at=datetime(2026, 3, 12, 10, 0, tzinfo=timezone.utc),
        )
        _create_paid_booking(
            session,
            creator=creator,
            booking_link=booking_link,
            content=content,
            booking_uuid="BOOK_EXPERIMENTS_DRILLDOWN",
            paid_at=datetime(2026, 3, 12, 14, 0, tzinfo=timezone.utc),
            amount_cents=19500,
            stripe_invoice_id="in_experiments_drilldown",
            stripe_event_id="evt_experiments_drilldown",
        )

        created = create_creator_next_content_experiments_run(
            creator_id=creator.id,
            db=session,
        )
        drilldown = get_creator_next_content_experiment_card_drilldown(
            creator_id=creator.id,
            run_claim_snapshot_id=created.claim_snapshot_id,
            card_order=1,
            db=session,
        )

    assert drilldown is not None
    assert drilldown.run_claim_snapshot_id == created.claim_snapshot_id
    assert drilldown.card_order == 1
    assert drilldown.authoritative_source_url == "https://example.com/posts/experiments-drilldown"
    assert drilldown.authoritative_content_tid == "experiments_drilldown"
    assert drilldown.authoritative_topics == ["Retention Reviews"]
    assert drilldown.authoritative_artifact_title == "Artifact for experiments_drilldown"
    assert len(drilldown.settled_paid_results) == 1
    assert drilldown.settled_paid_results[0].amount_cents == 19500
    assert drilldown.settled_paid_results[0].currency == "USD"
    assert drilldown.settled_paid_results[0].content_tid == "experiments_drilldown"


def test_get_current_creator_next_content_experiments_unsupported_explanation_reports_missing_authority_and_paid_results():
    engine = _engine()

    with Session(engine) as session:
        creator, booking_link = _create_creator_fixture(session, suffix="unsupported_reasons_empty")
        _create_content(
            session,
            creator=creator,
            booking_link=booking_link,
            tid="experiments_unsupported_reasons_empty",
            source_url="https://example.com/posts/experiments-unsupported-reasons-empty",
        )

        explanation = get_current_creator_next_content_experiments_unsupported_explanation(
            creator_id=creator.id,
            db=session,
        )

    assert explanation.reasons == [
        "No authoritative reviewed topics exist yet on your tracked content.",
        "No settled attributed paid results exist yet for this workspace.",
    ]
    assert explanation.has_excluded_current_activity is False


def test_get_current_creator_next_content_experiments_unsupported_explanation_reports_no_overlap_and_current_activity():
    engine = _engine()

    with Session(engine) as session:
        creator, booking_link = _create_creator_fixture(session, suffix="unsupported_reasons_overlap")
        authoritative_content = _create_content(
            session,
            creator=creator,
            booking_link=booking_link,
            tid="experiments_authoritative_only",
            source_url="https://example.com/posts/experiments-authoritative-only",
        )
        paid_content = _create_content(
            session,
            creator=creator,
            booking_link=booking_link,
            tid="experiments_paid_only",
            source_url="https://example.com/posts/experiments-paid-only",
        )
        _create_authoritative_artifact(
            session,
            content=authoritative_content,
            topic_labels=["Authority Without Paid"],
            fetched_at=datetime(2026, 3, 12, 9, 0, tzinfo=timezone.utc),
        )
        booking, _, _ = _create_paid_booking(
            session,
            creator=creator,
            booking_link=booking_link,
            content=paid_content,
            booking_uuid="BOOK_EXPERIMENTS_PAID_ONLY",
            paid_at=datetime(2026, 3, 12, 14, 0, tzinfo=timezone.utc),
            amount_cents=19500,
            stripe_invoice_id="in_experiments_paid_only",
            stripe_event_id="evt_experiments_paid_only",
        )
        session.add(
            InvoicePaymentEvent(
                stripe_event_id="evt_experiments_overlap_unmatched",
                stripe_event_type="invoice.paid",
                stripe_account_id=creator.stripe_account_id,
                stripe_invoice_id="in_experiments_overlap_unmatched",
                invoice_id=None,
                creator_id=creator.id,
                booking_id=None,
                tid=None,
                status="unmatched",
                unattributed_reason="MISSING_TID",
                paid_at=datetime(2026, 3, 12, 15, 0, tzinfo=timezone.utc),
                received_at=datetime(2026, 3, 12, 15, 0, tzinfo=timezone.utc),
                processed_at=None,
            )
        )
        session.add(
            BlockedBillingCase(
                creator_id=creator.id,
                booking_id=booking.id,
                invoice_id=None,
                tid=paid_content.tid,
                calendly_booking_uuid=booking.calendly_booking_uuid,
                stripe_account_id=creator.stripe_account_id,
                frozen_amount_cents=19500,
                frozen_currency="USD",
                status="open",
                reason_code="creator_not_billable",
                provider_operation=None,
                provider_http_status=None,
                provider_error_code=None,
                first_blocked_at=datetime(2026, 3, 12, 15, 5, tzinfo=timezone.utc),
                last_blocked_at=datetime(2026, 3, 12, 15, 5, tzinfo=timezone.utc),
                last_retry_at=None,
                resolved_at=None,
                resolution_code=None,
            )
        )
        session.flush()

        explanation = get_current_creator_next_content_experiments_unsupported_explanation(
            creator_id=creator.id,
            db=session,
        )

    assert explanation.reasons == [
        "Your reviewed topics and settled paid results do not overlap on the same tracked content yet."
    ]
    assert explanation.has_excluded_current_activity is True
