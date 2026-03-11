import os
import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import create_engine, text

from app.core.config import get_settings
from app.main import app


def _engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def _insert_creator_user(*, email: str, name: str = "Content Topic Creator") -> dict[str, str]:
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
                "name": name,
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

    return {"creator_id": creator_id, "user_id": user_id, "email": email}


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


def _insert_booking_link(*, creator_id: str, name: str, calendly_url: str) -> str:
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
                "name": name,
                "calendly_url": calendly_url,
            },
        )

    return booking_link_id


def _insert_content(*, creator_id: str, booking_link_id: str, source_url: str, tid: str) -> str:
    content_id = str(uuid.uuid4())

    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO content "
                "(id, creator_id, booking_link_id, source_url, tid, created_at, updated_at) "
                "VALUES "
                "(:id, :creator_id, :booking_link_id, :source_url, :tid, NOW(), NOW())"
            ),
            {
                "id": content_id,
                "creator_id": creator_id,
                "booking_link_id": booking_link_id,
                "source_url": source_url,
                "tid": tid,
            },
        )

    return content_id


def _insert_fetch_snapshot(
    *,
    content_id: str,
    creator_id: str,
    requested_url: str,
    fetched_url: str | None,
    fetch_status: str,
    http_status: int | None,
    snapshot_text: str | None,
    fetched_at: datetime,
    response_content_type: str = "text/html",
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
                "fetched_url": fetched_url,
                "fetch_status": fetch_status,
                "http_status": http_status,
                "failure_reason_code": None,
                "failure_detail": None,
                "response_content_type": response_content_type,
                "response_content_charset": "utf-8",
                "snapshot_text": snapshot_text,
                "fetched_at": fetched_at,
            },
        )

    return snapshot_id


def _insert_extraction_artifact(
    *,
    content_id: str,
    creator_id: str,
    fetch_snapshot_id: str,
    extraction_status: str,
    title: str | None,
    extracted_text: str | None,
    created_at: datetime,
    extraction_method: str = "html_article",
) -> str:
    artifact_id = str(uuid.uuid4())
    extracted_text = extracted_text or ""

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
                "extraction_status": extraction_status,
                "extraction_reason_code": None,
                "extraction_detail": None,
                "extraction_method": extraction_method,
                "title": title,
                "published_at": None,
                "published_at_raw": None,
                "source_text_char_count": len(extracted_text),
                "extracted_text_char_count": len(extracted_text),
                "extracted_text_word_count": len(extracted_text.split()),
                "extracted_text": extracted_text or None,
                "created_at": created_at,
            },
        )

    return artifact_id


def _insert_topic_candidate(
    *,
    content_id: str,
    creator_id: str,
    extraction_artifact_id: str,
    suggested_label: str,
    normalized_label: str,
    candidate_rank: int,
    review_status: str = "pending",
    confirmed_topic_id: str | None = None,
    reviewed_at: datetime | None = None,
) -> str:
    candidate_id = str(uuid.uuid4())

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


def _fetch_candidate_rows(*, content_id: str) -> list[dict[str, object]]:
    with _engine().connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, extraction_artifact_id, confirmed_topic_id, suggested_label, normalized_label, "
                "review_status, candidate_rank, reviewed_at "
                "FROM content_topic_candidates "
                "WHERE content_id = :content_id "
                "ORDER BY candidate_rank ASC, created_at ASC, id ASC"
            ),
            {"content_id": content_id},
        ).mappings().all()

    return [dict(row) for row in rows]


def _fetch_confirmed_rows(*, content_id: str) -> list[dict[str, object]]:
    with _engine().connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, canonical_label, normalized_label "
                "FROM content_confirmed_topics "
                "WHERE content_id = :content_id "
                "ORDER BY created_at ASC, id ASC"
            ),
            {"content_id": content_id},
        ).mappings().all()

    return [dict(row) for row in rows]


def _fetch_content_authority_row(*, content_id: str) -> dict[str, object]:
    with _engine().connect() as conn:
        row = conn.execute(
            text(
                "SELECT authoritative_extraction_artifact_id "
                "FROM content "
                "WHERE id = :content_id"
            ),
            {"content_id": content_id},
        ).mappings().one()

    return dict(row)


def test_create_content_topic_candidates_uses_latest_artifact_and_reuses_existing_candidates():
    inserted = _insert_creator_user(email=f"content_topics_{uuid.uuid4().hex}@example.com")
    token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="Topic Review Strategy",
        calendly_url="https://calendly.com/example/topic-review-strategy",
    )
    tid = uuid.uuid4().hex
    content_id = _insert_content(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/topic-review-strategy",
        tid=tid,
    )

    older_snapshot_id = _insert_fetch_snapshot(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        requested_url="https://example.com/posts/topic-review-strategy",
        fetched_url="https://example.com/posts/topic-review-strategy?old=1",
        fetch_status="succeeded",
        http_status=200,
        snapshot_text="<html><body><article><p>Older artifact text.</p></article></body></html>",
        fetched_at=datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc),
    )
    _insert_extraction_artifact(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        fetch_snapshot_id=older_snapshot_id,
        extraction_status="succeeded",
        title="Older Topic Artifact",
        extracted_text="Older topic artifact should not generate the active review set.",
        created_at=datetime(2026, 3, 10, 12, 1, tzinfo=timezone.utc),
    )
    latest_snapshot_id = _insert_fetch_snapshot(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        requested_url="https://example.com/posts/topic-review-strategy",
        fetched_url="https://example.com/posts/topic-review-strategy",
        fetch_status="succeeded",
        http_status=200,
        snapshot_text="<html><body><article><p>Latest artifact text.</p></article></body></html>",
        fetched_at=datetime(2026, 3, 10, 12, 5, tzinfo=timezone.utc),
    )
    latest_artifact_id = _insert_extraction_artifact(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        fetch_snapshot_id=latest_snapshot_id,
        extraction_status="succeeded",
        title="Launch Pricing Breakdown",
        extracted_text=(
            "Discovery calls for new leads close faster with pricing upfront.\n"
            "Retainer onboarding steps keep active students moving without confusion.\n"
            "Boilerplate welcome copy should stay out of the canonical set."
        ),
        created_at=datetime(2026, 3, 10, 12, 6, tzinfo=timezone.utc),
    )

    with TestClient(app) as client:
        first_response = client.post(
            f"/content/{tid}/topics/candidates",
            headers={"Authorization": f"Bearer {token}"},
        )
        second_response = client.post(
            f"/content/{tid}/topics/candidates",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert first_response.status_code == 201
    assert second_response.status_code == 200

    first_payload = first_response.json()
    second_payload = second_response.json()
    assert first_payload["content_id"] == content_id
    assert first_payload["content_tid"] == tid
    assert first_payload["extraction_artifact_id"] == latest_artifact_id
    assert first_payload["extraction_title"] == "Launch Pricing Breakdown"
    assert first_payload["review_confirmed_topics"] == []
    assert first_payload["authoritative_confirmed_topics"] == []
    assert first_payload["authoritative_state"] == {
        "authoritative_extraction_artifact_id": None,
        "authoritative_fetch_snapshot_id": None,
        "is_current_artifact_authoritative": False,
        "promotion_allowed": False,
        "promotion_block_reason": "Resolve all pending topic candidates before promoting current evidence.",
    }
    assert len(first_payload["candidate_topics"]) >= 3
    assert first_payload["candidate_topics"][0]["suggested_label"] == "Launch Pricing Breakdown"
    assert all(
        candidate["extraction_artifact_id"] == latest_artifact_id
        for candidate in first_payload["candidate_topics"]
    )
    assert [candidate["id"] for candidate in first_payload["candidate_topics"]] == [
        candidate["id"] for candidate in second_payload["candidate_topics"]
    ]

    candidate_rows = _fetch_candidate_rows(content_id=content_id)
    assert len(candidate_rows) == len(first_payload["candidate_topics"])
    assert {str(row["extraction_artifact_id"]) for row in candidate_rows} == {latest_artifact_id}
    assert all(row["review_status"] == "pending" for row in candidate_rows)


def test_confirm_candidate_edit_dedupes_normalized_variants_and_preserves_original_suggestion():
    inserted = _insert_creator_user(email=f"content_topics_confirm_{uuid.uuid4().hex}@example.com")
    token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="Confirm Topic Strategy",
        calendly_url="https://calendly.com/example/confirm-topic-strategy",
    )
    tid = uuid.uuid4().hex
    content_id = _insert_content(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/confirm-topic-strategy",
        tid=tid,
    )
    snapshot_id = _insert_fetch_snapshot(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        requested_url="https://example.com/posts/confirm-topic-strategy",
        fetched_url="https://example.com/posts/confirm-topic-strategy",
        fetch_status="succeeded",
        http_status=200,
        snapshot_text="<html><body><article><p>Confirm topic text.</p></article></body></html>",
        fetched_at=datetime(2026, 3, 10, 13, 0, tzinfo=timezone.utc),
    )
    artifact_id = _insert_extraction_artifact(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        fetch_snapshot_id=snapshot_id,
        extraction_status="succeeded",
        title="Confirm Topic Strategy",
        extracted_text="Discovery call pricing is the main theme here.",
        created_at=datetime(2026, 3, 10, 13, 1, tzinfo=timezone.utc),
    )
    first_candidate_id = _insert_topic_candidate(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        extraction_artifact_id=artifact_id,
        suggested_label="Discovery Call Pricing",
        normalized_label="discovery call pricing",
        candidate_rank=1,
    )
    second_candidate_id = _insert_topic_candidate(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        extraction_artifact_id=artifact_id,
        suggested_label="Discovery-Call Pricing Draft",
        normalized_label="discovery call pricing draft",
        candidate_rank=2,
    )

    with TestClient(app) as client:
        first_response = client.post(
            f"/content/{tid}/topics/{first_candidate_id}/confirm",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )
        second_response = client.post(
            f"/content/{tid}/topics/{second_candidate_id}/confirm",
            json={"confirmed_label": "Discovery Call Pricing"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 200

    payload = second_response.json()
    assert len(payload["review_confirmed_topics"]) == 1
    assert payload["review_confirmed_topics"][0]["canonical_label"] == "Discovery Call Pricing"
    assert payload["authoritative_confirmed_topics"] == []
    assert payload["authoritative_state"] == {
        "authoritative_extraction_artifact_id": None,
        "authoritative_fetch_snapshot_id": None,
        "is_current_artifact_authoritative": False,
        "promotion_allowed": True,
        "promotion_block_reason": None,
    }
    assert payload["candidate_topics"][0]["review_status"] == "confirmed"
    assert payload["candidate_topics"][1]["review_status"] == "confirmed"
    assert payload["candidate_topics"][0]["confirmed_topic_id"] == payload["candidate_topics"][1]["confirmed_topic_id"]

    candidate_rows = _fetch_candidate_rows(content_id=content_id)
    assert candidate_rows[0]["suggested_label"] == "Discovery Call Pricing"
    assert candidate_rows[1]["suggested_label"] == "Discovery-Call Pricing Draft"
    assert str(candidate_rows[0]["confirmed_topic_id"]) == str(candidate_rows[1]["confirmed_topic_id"])

    confirmed_rows = _fetch_confirmed_rows(content_id=content_id)
    assert confirmed_rows == [
        {
            "id": candidate_rows[0]["confirmed_topic_id"],
            "canonical_label": "Discovery Call Pricing",
            "normalized_label": "discovery call pricing",
        }
    ]


def test_rejecting_confirmed_candidate_removes_unused_canonical_topic():
    inserted = _insert_creator_user(email=f"content_topics_reject_{uuid.uuid4().hex}@example.com")
    token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="Reject Topic Strategy",
        calendly_url="https://calendly.com/example/reject-topic-strategy",
    )
    tid = uuid.uuid4().hex
    content_id = _insert_content(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/reject-topic-strategy",
        tid=tid,
    )
    snapshot_id = _insert_fetch_snapshot(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        requested_url="https://example.com/posts/reject-topic-strategy",
        fetched_url="https://example.com/posts/reject-topic-strategy",
        fetch_status="succeeded",
        http_status=200,
        snapshot_text="<html><body><article><p>Reject topic text.</p></article></body></html>",
        fetched_at=datetime(2026, 3, 10, 13, 30, tzinfo=timezone.utc),
    )
    artifact_id = _insert_extraction_artifact(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        fetch_snapshot_id=snapshot_id,
        extraction_status="succeeded",
        title="Reject Topic Strategy",
        extracted_text="Legacy onboarding checklist is only a weak theme here.",
        created_at=datetime(2026, 3, 10, 13, 31, tzinfo=timezone.utc),
    )
    candidate_id = _insert_topic_candidate(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        extraction_artifact_id=artifact_id,
        suggested_label="Legacy Onboarding Checklist",
        normalized_label="legacy onboarding checklist",
        candidate_rank=1,
    )

    with TestClient(app) as client:
        confirm_response = client.post(
            f"/content/{tid}/topics/{candidate_id}/confirm",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )
        reject_response = client.post(
            f"/content/{tid}/topics/{candidate_id}/reject",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert confirm_response.status_code == 200
    assert reject_response.status_code == 200

    payload = reject_response.json()
    assert payload["review_confirmed_topics"] == []
    assert payload["authoritative_confirmed_topics"] == []
    assert payload["authoritative_state"] == {
        "authoritative_extraction_artifact_id": None,
        "authoritative_fetch_snapshot_id": None,
        "is_current_artifact_authoritative": False,
        "promotion_allowed": True,
        "promotion_block_reason": None,
    }
    assert payload["candidate_topics"] == [
        {
            "id": candidate_id,
            "content_id": content_id,
            "content_tid": tid,
            "extraction_artifact_id": artifact_id,
            "confirmed_topic_id": None,
            "suggested_label": "Legacy Onboarding Checklist",
            "normalized_label": "legacy onboarding checklist",
            "suggestion_method": "text_keywords",
            "candidate_rank": 1,
            "review_status": "rejected",
            "reviewed_at": payload["candidate_topics"][0]["reviewed_at"],
            "created_at": payload["candidate_topics"][0]["created_at"],
        }
    ]
    assert payload["candidate_topics"][0]["reviewed_at"] is not None

    candidate_rows = _fetch_candidate_rows(content_id=content_id)
    assert candidate_rows[0]["review_status"] == "rejected"
    assert candidate_rows[0]["confirmed_topic_id"] is None
    assert candidate_rows[0]["reviewed_at"] is not None
    assert _fetch_confirmed_rows(content_id=content_id) == []


def test_promote_authoritative_evidence_requires_completed_review_and_keeps_previous_authority():
    inserted = _insert_creator_user(email=f"content_topics_promote_{uuid.uuid4().hex}@example.com")
    token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="Promote Topic Strategy",
        calendly_url="https://calendly.com/example/promote-topic-strategy",
    )
    tid = uuid.uuid4().hex
    content_id = _insert_content(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/promote-topic-strategy",
        tid=tid,
    )
    older_snapshot_id = _insert_fetch_snapshot(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        requested_url="https://example.com/posts/promote-topic-strategy?old=1",
        fetched_url="https://example.com/posts/promote-topic-strategy?old=1",
        fetch_status="succeeded",
        http_status=200,
        snapshot_text="<html><body><article><p>Older authoritative text.</p></article></body></html>",
        fetched_at=datetime(2026, 3, 10, 15, 0, tzinfo=timezone.utc),
    )
    older_artifact_id = _insert_extraction_artifact(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        fetch_snapshot_id=older_snapshot_id,
        extraction_status="succeeded",
        title="Older Canonical Topic",
        extracted_text="Discovery call pricing anchored the older authoritative topic set.",
        created_at=datetime(2026, 3, 10, 15, 1, tzinfo=timezone.utc),
    )
    older_candidate_id = _insert_topic_candidate(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        extraction_artifact_id=older_artifact_id,
        suggested_label="Discovery Call Pricing",
        normalized_label="discovery call pricing",
        candidate_rank=1,
    )

    with TestClient(app) as client:
        confirm_old_response = client.post(
            f"/content/{tid}/topics/{older_candidate_id}/confirm",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )
        promote_old_response = client.post(
            f"/content/{tid}/authoritative-evidence/promote",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert confirm_old_response.status_code == 200
    assert promote_old_response.status_code == 200
    assert promote_old_response.json()["authoritative_state"]["authoritative_extraction_artifact_id"] == older_artifact_id

    latest_snapshot_id = _insert_fetch_snapshot(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        requested_url="https://example.com/posts/promote-topic-strategy",
        fetched_url="https://example.com/posts/promote-topic-strategy",
        fetch_status="succeeded",
        http_status=200,
        snapshot_text="<html><body><article><p>Latest replacement text.</p></article></body></html>",
        fetched_at=datetime(2026, 3, 10, 15, 5, tzinfo=timezone.utc),
    )
    latest_artifact_id = _insert_extraction_artifact(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        fetch_snapshot_id=latest_snapshot_id,
        extraction_status="succeeded",
        title="Latest Canonical Topic",
        extracted_text=(
            "Retainer onboarding checklists now dominate the latest review text.\n"
            "Legacy pricing details matter less in this revised artifact."
        ),
        created_at=datetime(2026, 3, 10, 15, 6, tzinfo=timezone.utc),
    )
    latest_candidate_id = _insert_topic_candidate(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        extraction_artifact_id=latest_artifact_id,
        suggested_label="Retainer Onboarding Checklist",
        normalized_label="retainer onboarding checklist",
        candidate_rank=1,
    )
    latest_rejected_candidate_id = _insert_topic_candidate(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        extraction_artifact_id=latest_artifact_id,
        suggested_label="Legacy Pricing Details",
        normalized_label="legacy pricing details",
        candidate_rank=2,
    )

    with TestClient(app) as client:
        latest_review_response = client.get(
            f"/content/{tid}/topics",
            headers={"Authorization": f"Bearer {token}"},
        )
        confirm_latest_response = client.post(
            f"/content/{tid}/topics/{latest_candidate_id}/confirm",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )
        reject_latest_response = client.post(
            f"/content/{tid}/topics/{latest_rejected_candidate_id}/reject",
            headers={"Authorization": f"Bearer {token}"},
        )
        promote_latest_response = client.post(
            f"/content/{tid}/authoritative-evidence/promote",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert latest_review_response.status_code == 200
    latest_review_payload = latest_review_response.json()
    assert latest_review_payload["authoritative_state"] == {
        "authoritative_extraction_artifact_id": older_artifact_id,
        "authoritative_fetch_snapshot_id": older_snapshot_id,
        "is_current_artifact_authoritative": False,
        "promotion_allowed": False,
        "promotion_block_reason": "Resolve all pending topic candidates before promoting current evidence.",
    }
    assert [topic["canonical_label"] for topic in latest_review_payload["authoritative_confirmed_topics"]] == [
        "Discovery Call Pricing"
    ]
    assert latest_review_payload["review_confirmed_topics"] == []

    assert confirm_latest_response.status_code == 200
    assert confirm_latest_response.json()["authoritative_state"]["promotion_allowed"] is False

    assert reject_latest_response.status_code == 200
    reject_payload = reject_latest_response.json()
    assert reject_payload["authoritative_state"] == {
        "authoritative_extraction_artifact_id": older_artifact_id,
        "authoritative_fetch_snapshot_id": older_snapshot_id,
        "is_current_artifact_authoritative": False,
        "promotion_allowed": True,
        "promotion_block_reason": None,
    }
    assert [topic["canonical_label"] for topic in reject_payload["review_confirmed_topics"]] == [
        "Retainer Onboarding Checklist"
    ]
    assert [topic["canonical_label"] for topic in reject_payload["authoritative_confirmed_topics"]] == [
        "Discovery Call Pricing"
    ]

    assert promote_latest_response.status_code == 200
    promote_payload = promote_latest_response.json()
    assert promote_payload["authoritative_state"] == {
        "authoritative_extraction_artifact_id": latest_artifact_id,
        "authoritative_fetch_snapshot_id": latest_snapshot_id,
        "is_current_artifact_authoritative": True,
        "promotion_allowed": False,
        "promotion_block_reason": "Latest extraction artifact is already authoritative.",
    }
    assert [topic["canonical_label"] for topic in promote_payload["review_confirmed_topics"]] == [
        "Retainer Onboarding Checklist"
    ]
    assert [topic["canonical_label"] for topic in promote_payload["authoritative_confirmed_topics"]] == [
        "Retainer Onboarding Checklist"
    ]

    authority_row = _fetch_content_authority_row(content_id=content_id)
    assert authority_row == {
        "authoritative_extraction_artifact_id": uuid.UUID(latest_artifact_id),
    }
    confirmed_rows = _fetch_confirmed_rows(content_id=content_id)
    assert [row["canonical_label"] for row in confirmed_rows] == [
        "Discovery Call Pricing",
        "Retainer Onboarding Checklist",
    ]


def test_promote_authoritative_evidence_returns_409_for_missing_review_prerequisites():
    inserted = _insert_creator_user(email=f"content_topics_promote_blocked_{uuid.uuid4().hex}@example.com")
    token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="Blocked Promotion Strategy",
        calendly_url="https://calendly.com/example/blocked-promotion-strategy",
    )
    tid = uuid.uuid4().hex
    content_id = _insert_content(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/blocked-promotion-strategy",
        tid=tid,
    )

    with TestClient(app) as client:
        missing_artifact_response = client.post(
            f"/content/{tid}/authoritative-evidence/promote",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert missing_artifact_response.status_code == 409
    assert missing_artifact_response.json() == {"detail": "content extraction artifact required"}

    snapshot_id = _insert_fetch_snapshot(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        requested_url="https://example.com/posts/blocked-promotion-strategy",
        fetched_url="https://example.com/posts/blocked-promotion-strategy",
        fetch_status="succeeded",
        http_status=200,
        snapshot_text="<html><body><article><p>Blocked promotion text.</p></article></body></html>",
        fetched_at=datetime(2026, 3, 10, 16, 0, tzinfo=timezone.utc),
    )
    artifact_id = _insert_extraction_artifact(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        fetch_snapshot_id=snapshot_id,
        extraction_status="succeeded",
        title="Blocked Promotion Strategy",
        extracted_text="Blocked promotion strategy text still needs candidate review.",
        created_at=datetime(2026, 3, 10, 16, 1, tzinfo=timezone.utc),
    )

    with TestClient(app) as client:
        missing_candidates_response = client.post(
            f"/content/{tid}/authoritative-evidence/promote",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert missing_candidates_response.status_code == 409
    assert missing_candidates_response.json() == {
        "detail": "Generate topic candidates before promoting current evidence."
    }

    _insert_topic_candidate(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        extraction_artifact_id=artifact_id,
        suggested_label="Blocked Promotion Checklist",
        normalized_label="blocked promotion checklist",
        candidate_rank=1,
    )

    with TestClient(app) as client:
        pending_candidates_response = client.post(
            f"/content/{tid}/authoritative-evidence/promote",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert pending_candidates_response.status_code == 409
    assert pending_candidates_response.json() == {
        "detail": "Resolve all pending topic candidates before promoting current evidence."
    }


def test_content_topic_review_routes_require_auth_and_enforce_creator_isolation():
    creator_a = _insert_creator_user(email=f"content_topics_owner_{uuid.uuid4().hex}@example.com")
    creator_b = _insert_creator_user(email=f"content_topics_other_{uuid.uuid4().hex}@example.com")
    token_b = _access_token(
        user_id=creator_b["user_id"],
        creator_id=creator_b["creator_id"],
        email=creator_b["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=creator_a["creator_id"],
        name="Isolation Topic Strategy",
        calendly_url="https://calendly.com/example/isolation-topic-strategy",
    )
    tid = uuid.uuid4().hex
    content_id = _insert_content(
        creator_id=creator_a["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/isolation-topic-strategy",
        tid=tid,
    )
    snapshot_id = _insert_fetch_snapshot(
        content_id=content_id,
        creator_id=creator_a["creator_id"],
        requested_url="https://example.com/posts/isolation-topic-strategy",
        fetched_url="https://example.com/posts/isolation-topic-strategy",
        fetch_status="succeeded",
        http_status=200,
        snapshot_text="<html><body><article><p>Isolation topic text.</p></article></body></html>",
        fetched_at=datetime(2026, 3, 10, 14, 0, tzinfo=timezone.utc),
    )
    artifact_id = _insert_extraction_artifact(
        content_id=content_id,
        creator_id=creator_a["creator_id"],
        fetch_snapshot_id=snapshot_id,
        extraction_status="succeeded",
        title="Isolation Topic Strategy",
        extracted_text="Student onboarding checklist stays private to creator A.",
        created_at=datetime(2026, 3, 10, 14, 1, tzinfo=timezone.utc),
    )
    candidate_id = _insert_topic_candidate(
        content_id=content_id,
        creator_id=creator_a["creator_id"],
        extraction_artifact_id=artifact_id,
        suggested_label="Student Onboarding Checklist",
        normalized_label="student onboarding checklist",
        candidate_rank=1,
    )

    with TestClient(app) as client:
        unauthenticated_get = client.get(f"/content/{tid}/topics")
        not_owned_get = client.get(
            f"/content/{tid}/topics",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        not_owned_confirm = client.post(
            f"/content/{tid}/topics/{candidate_id}/confirm",
            json={},
            headers={"Authorization": f"Bearer {token_b}"},
        )
        not_owned_reject = client.post(
            f"/content/{tid}/topics/{candidate_id}/reject",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        not_owned_promote = client.post(
            f"/content/{tid}/authoritative-evidence/promote",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        unauthenticated_promote = client.post(f"/content/{tid}/authoritative-evidence/promote")

    assert unauthenticated_get.status_code == 401
    assert unauthenticated_get.json() == {"detail": "not authenticated"}
    assert unauthenticated_promote.status_code == 401
    assert unauthenticated_promote.json() == {"detail": "not authenticated"}
    assert not_owned_get.status_code == 404
    assert not_owned_get.json() == {"detail": "content not found"}
    assert not_owned_confirm.status_code == 404
    assert not_owned_confirm.json() == {"detail": "content not found"}
    assert not_owned_reject.status_code == 404
    assert not_owned_reject.json() == {"detail": "content not found"}
    assert not_owned_promote.status_code == 404
    assert not_owned_promote.json() == {"detail": "content not found"}
