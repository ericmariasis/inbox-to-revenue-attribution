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


def _insert_creator_user(
    *,
    email: str,
    name: str = "Content Creator",
    stripe_connect_status: str = "pending",
):
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
                "stripe_connect_status": stripe_connect_status,
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


def _insert_content(
    *,
    content_id: str,
    creator_id: str,
    booking_link_id: str,
    source_url: str,
    tid: str,
    created_at: datetime,
) -> None:
    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO content "
                "(id, creator_id, booking_link_id, source_url, tid, created_at, updated_at) "
                "VALUES "
                "(:id, :creator_id, :booking_link_id, :source_url, :tid, :created_at, :updated_at)"
            ),
            {
                "id": content_id,
                "creator_id": creator_id,
                "booking_link_id": booking_link_id,
                "source_url": source_url,
                "tid": tid,
                "created_at": created_at,
                "updated_at": created_at,
            },
        )


def test_get_content_detail_requires_auth():
    with TestClient(app) as client:
        response = client.get("/content/missing-content-tid")

    assert response.status_code == 401
    assert response.json() == {"detail": "not authenticated"}


def test_get_content_detail_returns_current_creators_row_by_tid():
    creator = _insert_creator_user(email=f"content_{uuid.uuid4().hex}@example.com")
    token = _access_token(
        user_id=creator["user_id"],
        creator_id=creator["creator_id"],
        email=creator["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=creator["creator_id"],
        name="Creator Strategy Call",
        calendly_url="https://calendly.com/example/creator-strategy-call",
    )
    content_id = "00000000-0000-0000-0000-000000000123"
    tid = "creatorownedcontenttid"
    _insert_content(
        content_id=content_id,
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/high-ticket-math-lessons",
        tid=tid,
        created_at=datetime(2026, 3, 6, 14, 0, tzinfo=timezone.utc),
    )

    tracked_base_url = get_settings().tracked_link_base_url.rstrip("/")

    with TestClient(app) as client:
        response = client.get(
            f"/content/{tid}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.headers.get("X-Request-Id")
    assert response.json() == {
        "id": content_id,
        "booking_link_id": booking_link_id,
        "source_url": "https://example.com/posts/high-ticket-math-lessons",
        "tid": tid,
        "tracked_url": f"{tracked_base_url}/r/{tid}",
    }


def test_get_content_detail_returns_same_404_for_unknown_and_not_owned_tid():
    creator_a = _insert_creator_user(email=f"creator_a_{uuid.uuid4().hex}@example.com")
    creator_b = _insert_creator_user(email=f"creator_b_{uuid.uuid4().hex}@example.com")
    creator_b_token = _access_token(
        user_id=creator_b["user_id"],
        creator_id=creator_b["creator_id"],
        email=creator_b["email"],
        expires_delta=timedelta(hours=24),
    )
    creator_a_booking_link_id = _insert_booking_link(
        creator_id=creator_a["creator_id"],
        name="Creator A Strategy Call",
        calendly_url="https://calendly.com/example/creator-a-strategy-call",
    )
    creator_a_tid = "creatoracontenttid"
    _insert_content(
        content_id=str(uuid.uuid4()),
        creator_id=creator_a["creator_id"],
        booking_link_id=creator_a_booking_link_id,
        source_url="https://example.com/posts/creator-a-content",
        tid=creator_a_tid,
        created_at=datetime(2026, 3, 6, 14, 5, tzinfo=timezone.utc),
    )

    with TestClient(app) as client:
        not_owned_response = client.get(
            f"/content/{creator_a_tid}",
            headers={"Authorization": f"Bearer {creator_b_token}"},
        )
        unknown_response = client.get(
            "/content/unknown-content-tid",
            headers={"Authorization": f"Bearer {creator_b_token}"},
        )

    assert not_owned_response.status_code == 404
    assert unknown_response.status_code == 404
    assert not_owned_response.json() == {"detail": "content not found"}
    assert unknown_response.json() == {"detail": "content not found"}
