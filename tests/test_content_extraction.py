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


def _insert_creator_user(*, email: str, name: str = "Content Extraction Creator") -> dict[str, str]:
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
    response_content_charset: str | None = "utf-8",
    failure_reason_code: str | None = None,
    failure_detail: str | None = None,
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
                "failure_reason_code": failure_reason_code,
                "failure_detail": failure_detail,
                "response_content_type": response_content_type,
                "response_content_charset": response_content_charset,
                "snapshot_text": snapshot_text,
                "fetched_at": fetched_at,
            },
        )

    return snapshot_id


def _fetch_extraction_rows(*, content_id: str) -> list[dict[str, object]]:
    with _engine().connect() as conn:
        rows = conn.execute(
            text(
                "SELECT fetch_snapshot_id, extraction_status, extraction_reason_code, title, "
                "published_at_raw, extraction_method, extracted_text_word_count "
                "FROM content_extraction_artifacts "
                "WHERE content_id = :content_id "
                "ORDER BY created_at ASC, id ASC"
            ),
            {"content_id": content_id},
        ).mappings().all()

    return [dict(row) for row in rows]


def test_extract_content_artifact_requires_auth():
    with TestClient(app) as client:
        response = client.post(f"/content/{uuid.uuid4().hex}/extract")

    assert response.status_code == 401
    assert response.json() == {"detail": "not authenticated"}


def test_extract_content_artifact_uses_latest_snapshot_and_reuses_existing_artifact():
    inserted = _insert_creator_user(email=f"content_extract_success_{uuid.uuid4().hex}@example.com")
    token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="Extraction Strategy",
        calendly_url="https://calendly.com/example/extraction-strategy",
    )
    tid = uuid.uuid4().hex
    content_id = _insert_content(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/extraction-strategy",
        tid=tid,
    )
    _insert_fetch_snapshot(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        requested_url="https://example.com/posts/extraction-strategy",
        fetched_url="https://example.com/posts/extraction-strategy?old=1",
        fetch_status="succeeded",
        http_status=200,
        snapshot_text="<html><body><article><p>Older snapshot text should not win.</p></article></body></html>",
        fetched_at=datetime(2026, 3, 10, 11, 0, tzinfo=timezone.utc),
    )
    latest_snapshot_id = _insert_fetch_snapshot(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        requested_url="https://example.com/posts/extraction-strategy",
        fetched_url="https://example.com/posts/extraction-strategy",
        fetch_status="succeeded",
        http_status=200,
        snapshot_text="""
<!doctype html>
<html lang="en">
  <head>
    <title>Fallback Title</title>
    <meta property="og:title" content="Story 62 Canonical Extraction" />
    <meta property="article:published_time" content="2026-03-01T15:30:00Z" />
  </head>
  <body>
    <article>
      <h1>Story 62 Canonical Extraction</h1>
      <p>The canonical extraction artifact should preserve the main article text without making a second fetch.</p>
      <p>This paragraph adds enough words to clear the low-confidence threshold and keep the artifact useful for later analysis.</p>
      <p>Future topic review can now build on inspectable extracted text instead of reparsing provider pages by hand.</p>
    </article>
  </body>
</html>
""",
        fetched_at=datetime(2026, 3, 10, 11, 5, tzinfo=timezone.utc),
    )

    with TestClient(app) as client:
        first_response = client.post(
            f"/content/{tid}/extract",
            headers={"Authorization": f"Bearer {token}"},
        )
        second_response = client.post(
            f"/content/{tid}/extract",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert first_response.status_code == 201
    assert second_response.status_code == 200

    first_payload = first_response.json()
    second_payload = second_response.json()
    assert first_payload["id"] == second_payload["id"]
    assert first_payload["content_id"] == content_id
    assert first_payload["content_tid"] == tid
    assert first_payload["fetch_snapshot_id"] == latest_snapshot_id
    assert first_payload["extraction_status"] == "succeeded"
    assert first_payload["extraction_reason_code"] is None
    assert first_payload["extraction_method"] == "html_article"
    assert first_payload["title"] == "Story 62 Canonical Extraction"
    assert datetime.fromisoformat(first_payload["published_at"]).astimezone(timezone.utc) == datetime(
        2026, 3, 1, 15, 30, tzinfo=timezone.utc
    )
    assert first_payload["published_at_raw"] == "2026-03-01T15:30:00Z"
    assert "second fetch" in first_payload["extracted_text"]
    assert first_payload["extracted_text_word_count"] >= 30
    assert first_payload["extracted_text_char_count"] > 0
    assert first_payload["source_text_char_count"] > first_payload["extracted_text_char_count"]

    rows = _fetch_extraction_rows(content_id=content_id)
    assert rows == [
        {
            "fetch_snapshot_id": uuid.UUID(latest_snapshot_id),
            "extraction_status": "succeeded",
            "extraction_reason_code": None,
            "title": "Story 62 Canonical Extraction",
            "published_at_raw": "2026-03-01T15:30:00Z",
            "extraction_method": "html_article",
            "extracted_text_word_count": first_payload["extracted_text_word_count"],
        }
    ]


def test_extract_content_artifact_persists_low_confidence_result_for_short_snapshot():
    inserted = _insert_creator_user(email=f"content_extract_short_{uuid.uuid4().hex}@example.com")
    token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="Short Extraction Strategy",
        calendly_url="https://calendly.com/example/short-extraction-strategy",
    )
    tid = uuid.uuid4().hex
    content_id = _insert_content(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/short-extraction-strategy",
        tid=tid,
    )
    snapshot_id = _insert_fetch_snapshot(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        requested_url="https://example.com/posts/short-extraction-strategy",
        fetched_url="https://example.com/posts/short-extraction-strategy",
        fetch_status="succeeded",
        http_status=200,
        snapshot_text="""
<html>
  <body>
    <article>
      <h1>Short note</h1>
      <p>Tiny outcome proof only.</p>
    </article>
  </body>
</html>
""",
        fetched_at=datetime(2026, 3, 10, 11, 10, tzinfo=timezone.utc),
    )

    with TestClient(app) as client:
        response = client.post(
            f"/content/{tid}/extract",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 201
    payload = response.json()
    assert payload["content_id"] == content_id
    assert payload["fetch_snapshot_id"] == snapshot_id
    assert payload["extraction_status"] == "low_confidence"
    assert payload["extraction_reason_code"] == "TEXT_TOO_SHORT"
    assert "too short" in payload["extraction_detail"]
    assert payload["extraction_method"] == "html_article"
    assert payload["extracted_text"] == "Short note\n\nTiny outcome proof only."
    assert payload["extracted_text_word_count"] < 30

    rows = _fetch_extraction_rows(content_id=content_id)
    assert rows == [
        {
            "fetch_snapshot_id": uuid.UUID(snapshot_id),
            "extraction_status": "low_confidence",
            "extraction_reason_code": "TEXT_TOO_SHORT",
            "title": None,
            "published_at_raw": None,
            "extraction_method": "html_article",
            "extracted_text_word_count": payload["extracted_text_word_count"],
        }
    ]


def test_extract_content_artifact_requires_existing_fetch_snapshot():
    inserted = _insert_creator_user(email=f"content_extract_missing_snapshot_{uuid.uuid4().hex}@example.com")
    token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="Missing Snapshot Strategy",
        calendly_url="https://calendly.com/example/missing-snapshot-strategy",
    )
    tid = uuid.uuid4().hex
    _insert_content(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/missing-snapshot-strategy",
        tid=tid,
    )

    with TestClient(app) as client:
        response = client.post(
            f"/content/{tid}/extract",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 409
    assert response.json() == {"detail": "content fetch snapshot required"}


def test_extract_content_artifact_returns_same_404_for_unknown_and_not_owned_tid():
    creator_a = _insert_creator_user(email=f"content_extract_owner_{uuid.uuid4().hex}@example.com")
    creator_b = _insert_creator_user(email=f"content_extract_other_{uuid.uuid4().hex}@example.com")
    token_b = _access_token(
        user_id=creator_b["user_id"],
        creator_id=creator_b["creator_id"],
        email=creator_b["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=creator_a["creator_id"],
        name="Extraction Isolation Strategy",
        calendly_url="https://calendly.com/example/extraction-isolation-strategy",
    )
    owner_tid = uuid.uuid4().hex
    content_id = _insert_content(
        creator_id=creator_a["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/extraction-isolation-strategy",
        tid=owner_tid,
    )
    _insert_fetch_snapshot(
        content_id=content_id,
        creator_id=creator_a["creator_id"],
        requested_url="https://example.com/posts/extraction-isolation-strategy",
        fetched_url="https://example.com/posts/extraction-isolation-strategy",
        fetch_status="succeeded",
        http_status=200,
        snapshot_text="<html><body><article><p>Visible only to the owner.</p></article></body></html>",
        fetched_at=datetime(2026, 3, 10, 11, 15, tzinfo=timezone.utc),
    )

    with TestClient(app) as client:
        not_owned_response = client.post(
            f"/content/{owner_tid}/extract",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        unknown_response = client.post(
            f"/content/{uuid.uuid4().hex}/extract",
            headers={"Authorization": f"Bearer {token_b}"},
        )

    assert not_owned_response.status_code == 404
    assert unknown_response.status_code == 404
    assert not_owned_response.json() == {"detail": "content not found"}
    assert unknown_response.json() == {"detail": "content not found"}
