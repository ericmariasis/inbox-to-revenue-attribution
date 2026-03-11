import os
import uuid
from datetime import datetime, timezone

from sqlalchemy import create_engine, text, select

from app.db.session import SessionLocal
from app.models.content import Content
from app.models.content_extraction_artifact import ContentExtractionArtifact
from app.models.content_topic_candidate import ContentTopicCandidate
from app.services.authoritative_content_evidence import (
    build_content_authority_state,
    get_authoritative_content_evidence,
)


def _engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def _insert_creator(*, email: str) -> dict[str, str]:
    creator_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO creators (id, name, stripe_connect_status) "
                "VALUES (:id, :name, :stripe_connect_status)"
            ),
            {
                "id": creator_id,
                "name": "Authority Test Creator",
                "stripe_connect_status": "pending",
            },
        )
        conn.execute(
            text(
                "INSERT INTO auth_users (id, creator_id, email) "
                "VALUES (:id, :creator_id, :email)"
            ),
            {"id": user_id, "creator_id": creator_id, "email": email},
        )
    return {"creator_id": creator_id, "user_id": user_id}


def _insert_booking_link(*, creator_id: str) -> str:
    booking_link_id = str(uuid.uuid4())
    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO booking_links (id, creator_id, name, calendly_url) "
                "VALUES (:id, :creator_id, :name, :calendly_url)"
            ),
            {
                "id": booking_link_id,
                "creator_id": creator_id,
                "name": "Authority Test Link",
                "calendly_url": "https://calendly.com/example/authority-test-link",
            },
        )
    return booking_link_id


def _insert_content(*, creator_id: str, booking_link_id: str) -> dict[str, str]:
    content_id = str(uuid.uuid4())
    tid = uuid.uuid4().hex
    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO content "
                "(id, creator_id, booking_link_id, source_url, tid, created_at, updated_at) "
                "VALUES (:id, :creator_id, :booking_link_id, :source_url, :tid, NOW(), NOW())"
            ),
            {
                "id": content_id,
                "creator_id": creator_id,
                "booking_link_id": booking_link_id,
                "source_url": "https://example.com/posts/authority-test",
                "tid": tid,
            },
        )
    return {"content_id": content_id, "tid": tid}


def _insert_fetch_snapshot(
    *,
    content_id: str,
    creator_id: str,
    requested_url: str,
    fetched_at: datetime,
) -> str:
    snapshot_id = str(uuid.uuid4())
    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO content_fetch_snapshots "
                "("
                "id, content_id, creator_id, requested_url, fetched_url, fetch_status, http_status, "
                "failure_reason_code, failure_detail, response_content_type, response_content_charset, "
                "snapshot_text, fetched_at"
                ") "
                "VALUES "
                "("
                ":id, :content_id, :creator_id, :requested_url, :fetched_url, :fetch_status, :http_status, "
                ":failure_reason_code, :failure_detail, :response_content_type, :response_content_charset, "
                ":snapshot_text, :fetched_at"
                ")"
            ),
            {
                "id": snapshot_id,
                "content_id": content_id,
                "creator_id": creator_id,
                "requested_url": requested_url,
                "fetched_url": requested_url,
                "fetch_status": "succeeded",
                "http_status": 200,
                "failure_reason_code": None,
                "failure_detail": None,
                "response_content_type": "text/html",
                "response_content_charset": "utf-8",
                "snapshot_text": "<html><body><article><p>Authority test text.</p></article></body></html>",
                "fetched_at": fetched_at,
            },
        )
    return snapshot_id


def _insert_extraction_artifact(
    *,
    content_id: str,
    creator_id: str,
    fetch_snapshot_id: str,
    title: str,
    extracted_text: str,
    created_at: datetime,
) -> str:
    artifact_id = str(uuid.uuid4())
    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO content_extraction_artifacts "
                "("
                "id, content_id, creator_id, fetch_snapshot_id, extraction_status, extraction_reason_code, "
                "extraction_detail, extraction_method, title, published_at, published_at_raw, "
                "source_text_char_count, extracted_text_char_count, extracted_text_word_count, extracted_text, created_at"
                ") "
                "VALUES "
                "("
                ":id, :content_id, :creator_id, :fetch_snapshot_id, :extraction_status, :extraction_reason_code, "
                ":extraction_detail, :extraction_method, :title, :published_at, :published_at_raw, "
                ":source_text_char_count, :extracted_text_char_count, :extracted_text_word_count, :extracted_text, :created_at"
                ")"
            ),
            {
                "id": artifact_id,
                "content_id": content_id,
                "creator_id": creator_id,
                "fetch_snapshot_id": fetch_snapshot_id,
                "extraction_status": "succeeded",
                "extraction_reason_code": None,
                "extraction_detail": None,
                "extraction_method": "html_article",
                "title": title,
                "published_at": None,
                "published_at_raw": None,
                "source_text_char_count": len(extracted_text),
                "extracted_text_char_count": len(extracted_text),
                "extracted_text_word_count": len(extracted_text.split()),
                "extracted_text": extracted_text,
                "created_at": created_at,
            },
        )
    return artifact_id


def _insert_confirmed_topic(*, content_id: str, creator_id: str, canonical_label: str) -> str:
    topic_id = str(uuid.uuid4())
    normalized_label = canonical_label.casefold()
    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO content_confirmed_topics "
                "(id, content_id, creator_id, canonical_label, normalized_label, created_at, updated_at) "
                "VALUES (:id, :content_id, :creator_id, :canonical_label, :normalized_label, NOW(), NOW())"
            ),
            {
                "id": topic_id,
                "content_id": content_id,
                "creator_id": creator_id,
                "canonical_label": canonical_label,
                "normalized_label": normalized_label,
            },
        )
    return topic_id


def _insert_topic_candidate(
    *,
    content_id: str,
    creator_id: str,
    extraction_artifact_id: str,
    suggested_label: str,
    normalized_label: str,
    candidate_rank: int,
    review_status: str,
    confirmed_topic_id: str | None = None,
) -> str:
    candidate_id = str(uuid.uuid4())
    reviewed_at = datetime.now(timezone.utc) if review_status != "pending" else None
    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO content_topic_candidates "
                "("
                "id, content_id, creator_id, extraction_artifact_id, confirmed_topic_id, suggested_label, "
                "normalized_label, suggestion_method, candidate_rank, review_status, reviewed_at, created_at"
                ") "
                "VALUES "
                "("
                ":id, :content_id, :creator_id, :extraction_artifact_id, :confirmed_topic_id, :suggested_label, "
                ":normalized_label, :suggestion_method, :candidate_rank, :review_status, :reviewed_at, NOW()"
                ")"
            ),
            {
                "id": candidate_id,
                "content_id": content_id,
                "creator_id": creator_id,
                "extraction_artifact_id": extraction_artifact_id,
                "confirmed_topic_id": confirmed_topic_id,
                "suggested_label": suggested_label,
                "normalized_label": normalized_label,
                "suggestion_method": "text_keywords",
                "candidate_rank": candidate_rank,
                "review_status": review_status,
                "reviewed_at": reviewed_at,
            },
        )
    return candidate_id


def _set_content_authority(*, content_id: str, artifact_id: str) -> None:
    with _engine().begin() as conn:
        conn.execute(
            text(
                "UPDATE content "
                "SET authoritative_extraction_artifact_id = :artifact_id "
                "WHERE id = :content_id"
            ),
            {"content_id": content_id, "artifact_id": artifact_id},
        )


def test_get_authoritative_content_evidence_returns_only_topics_linked_to_the_authoritative_artifact():
    inserted = _insert_creator(email=f"authority_service_{uuid.uuid4().hex}@example.com")
    booking_link_id = _insert_booking_link(creator_id=inserted["creator_id"])
    content = _insert_content(creator_id=inserted["creator_id"], booking_link_id=booking_link_id)

    old_snapshot_id = _insert_fetch_snapshot(
        content_id=content["content_id"],
        creator_id=inserted["creator_id"],
        requested_url="https://example.com/posts/authority-test?old=1",
        fetched_at=datetime(2026, 3, 10, 17, 0, tzinfo=timezone.utc),
    )
    old_artifact_id = _insert_extraction_artifact(
        content_id=content["content_id"],
        creator_id=inserted["creator_id"],
        fetch_snapshot_id=old_snapshot_id,
        title="Older Authority",
        extracted_text="Older authority text.",
        created_at=datetime(2026, 3, 10, 17, 1, tzinfo=timezone.utc),
    )
    old_topic_id = _insert_confirmed_topic(
        content_id=content["content_id"],
        creator_id=inserted["creator_id"],
        canonical_label="Discovery Call Pricing",
    )
    _insert_topic_candidate(
        content_id=content["content_id"],
        creator_id=inserted["creator_id"],
        extraction_artifact_id=old_artifact_id,
        suggested_label="Discovery Call Pricing",
        normalized_label="discovery call pricing",
        candidate_rank=1,
        review_status="confirmed",
        confirmed_topic_id=old_topic_id,
    )

    latest_snapshot_id = _insert_fetch_snapshot(
        content_id=content["content_id"],
        creator_id=inserted["creator_id"],
        requested_url="https://example.com/posts/authority-test",
        fetched_at=datetime(2026, 3, 10, 17, 5, tzinfo=timezone.utc),
    )
    latest_artifact_id = _insert_extraction_artifact(
        content_id=content["content_id"],
        creator_id=inserted["creator_id"],
        fetch_snapshot_id=latest_snapshot_id,
        title="Latest Authority",
        extracted_text="Latest authority text.",
        created_at=datetime(2026, 3, 10, 17, 6, tzinfo=timezone.utc),
    )
    latest_topic_id = _insert_confirmed_topic(
        content_id=content["content_id"],
        creator_id=inserted["creator_id"],
        canonical_label="Retainer Onboarding Checklist",
    )
    _insert_topic_candidate(
        content_id=content["content_id"],
        creator_id=inserted["creator_id"],
        extraction_artifact_id=latest_artifact_id,
        suggested_label="Retainer Onboarding Checklist",
        normalized_label="retainer onboarding checklist",
        candidate_rank=1,
        review_status="confirmed",
        confirmed_topic_id=latest_topic_id,
    )
    _set_content_authority(content_id=content["content_id"], artifact_id=latest_artifact_id)

    with SessionLocal() as db:
        content_row = db.execute(
            select(Content).where(Content.id == uuid.UUID(content["content_id"]))
        ).scalar_one()
        evidence = get_authoritative_content_evidence(content=content_row, db=db)

    assert evidence is not None
    assert evidence.artifact.id == uuid.UUID(latest_artifact_id)
    assert evidence.fetch_snapshot.id == uuid.UUID(latest_snapshot_id)
    assert [topic.canonical_label for topic in evidence.confirmed_topics] == [
        "Retainer Onboarding Checklist"
    ]


def test_build_content_authority_state_keeps_previous_authority_until_latest_review_is_promotable():
    inserted = _insert_creator(email=f"authority_state_{uuid.uuid4().hex}@example.com")
    booking_link_id = _insert_booking_link(creator_id=inserted["creator_id"])
    content = _insert_content(creator_id=inserted["creator_id"], booking_link_id=booking_link_id)

    old_snapshot_id = _insert_fetch_snapshot(
        content_id=content["content_id"],
        creator_id=inserted["creator_id"],
        requested_url="https://example.com/posts/authority-state?old=1",
        fetched_at=datetime(2026, 3, 10, 18, 0, tzinfo=timezone.utc),
    )
    old_artifact_id = _insert_extraction_artifact(
        content_id=content["content_id"],
        creator_id=inserted["creator_id"],
        fetch_snapshot_id=old_snapshot_id,
        title="Older State",
        extracted_text="Older state text.",
        created_at=datetime(2026, 3, 10, 18, 1, tzinfo=timezone.utc),
    )
    old_topic_id = _insert_confirmed_topic(
        content_id=content["content_id"],
        creator_id=inserted["creator_id"],
        canonical_label="Discovery Call Pricing",
    )
    _insert_topic_candidate(
        content_id=content["content_id"],
        creator_id=inserted["creator_id"],
        extraction_artifact_id=old_artifact_id,
        suggested_label="Discovery Call Pricing",
        normalized_label="discovery call pricing",
        candidate_rank=1,
        review_status="confirmed",
        confirmed_topic_id=old_topic_id,
    )
    _set_content_authority(content_id=content["content_id"], artifact_id=old_artifact_id)

    latest_snapshot_id = _insert_fetch_snapshot(
        content_id=content["content_id"],
        creator_id=inserted["creator_id"],
        requested_url="https://example.com/posts/authority-state",
        fetched_at=datetime(2026, 3, 10, 18, 5, tzinfo=timezone.utc),
    )
    latest_artifact_id = _insert_extraction_artifact(
        content_id=content["content_id"],
        creator_id=inserted["creator_id"],
        fetch_snapshot_id=latest_snapshot_id,
        title="Latest State",
        extracted_text="Latest state text.",
        created_at=datetime(2026, 3, 10, 18, 6, tzinfo=timezone.utc),
    )
    latest_topic_id = _insert_confirmed_topic(
        content_id=content["content_id"],
        creator_id=inserted["creator_id"],
        canonical_label="Retainer Onboarding Checklist",
    )
    latest_confirmed_candidate_id = _insert_topic_candidate(
        content_id=content["content_id"],
        creator_id=inserted["creator_id"],
        extraction_artifact_id=latest_artifact_id,
        suggested_label="Retainer Onboarding Checklist",
        normalized_label="retainer onboarding checklist",
        candidate_rank=1,
        review_status="confirmed",
        confirmed_topic_id=latest_topic_id,
    )
    latest_pending_candidate_id = _insert_topic_candidate(
        content_id=content["content_id"],
        creator_id=inserted["creator_id"],
        extraction_artifact_id=latest_artifact_id,
        suggested_label="Pending Followup Topic",
        normalized_label="pending followup topic",
        candidate_rank=2,
        review_status="pending",
    )

    with SessionLocal() as db:
        content_row = db.execute(
            select(Content).where(Content.id == uuid.UUID(content["content_id"]))
        ).scalar_one()
        artifact_row = db.execute(
            select(ContentExtractionArtifact).where(
                ContentExtractionArtifact.id == uuid.UUID(latest_artifact_id)
            )
        ).scalar_one()
        candidate_rows = db.execute(
            select(ContentTopicCandidate).where(
                ContentTopicCandidate.id.in_(
                    [
                        uuid.UUID(latest_confirmed_candidate_id),
                        uuid.UUID(latest_pending_candidate_id),
                    ]
                )
            )
        ).scalars().all()
        state = build_content_authority_state(
            content=content_row,
            artifact=artifact_row,
            candidate_topics=candidate_rows,
            db=db,
        )

    assert state.authoritative_extraction_artifact_id == uuid.UUID(old_artifact_id)
    assert state.authoritative_fetch_snapshot_id == uuid.UUID(old_snapshot_id)
    assert state.is_current_artifact_authoritative is False
    assert state.promotion_allowed is False
    assert state.promotion_block_reason == (
        "Resolve all pending topic candidates before promoting current evidence."
    )
    assert [topic.canonical_label for topic in state.authoritative_confirmed_topics] == [
        "Discovery Call Pricing"
    ]
    assert [topic.canonical_label for topic in state.review_confirmed_topics] == [
        "Retainer Onboarding Checklist"
    ]
