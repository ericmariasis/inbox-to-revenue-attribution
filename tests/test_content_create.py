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


def _content_row(content_id: str) -> dict[str, str]:
    with _engine().connect() as conn:
        row = conn.execute(
            text(
                "SELECT id, creator_id, booking_link_id, source_url, tid "
                "FROM content "
                "WHERE id = :id"
            ),
            {"id": content_id},
        ).mappings().one()

    return {
        "id": str(row["id"]),
        "creator_id": str(row["creator_id"]),
        "booking_link_id": str(row["booking_link_id"]),
        "source_url": row["source_url"],
        "tid": row["tid"],
    }


def _content_tids_for_creator(*, creator_id: str) -> list[str]:
    with _engine().connect() as conn:
        rows = conn.execute(
            text(
                "SELECT tid "
                "FROM content "
                "WHERE creator_id = :creator_id "
                "ORDER BY created_at ASC, id ASC"
            ),
            {"creator_id": creator_id},
        ).scalars().all()

    return list(rows)


def test_create_content_requires_auth():
    with TestClient(app) as client:
        response = client.post(
            "/content",
            json={
                "source_url": "https://example.com/posts/high-ticket-math-lessons",
                "booking_link_id": str(uuid.uuid4()),
            },
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "not authenticated"}


def test_create_content_returns_201_and_persists_for_authenticated_creator():
    inserted = _insert_creator_user(email=f"content_{uuid.uuid4().hex}@example.com")
    token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="Strategy Call",
        calendly_url="https://calendly.com/example/strategy-call",
    )
    tracked_base_url = get_settings().tracked_link_base_url.rstrip("/")

    with TestClient(app) as client:
        response = client.post(
            "/content",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "source_url": "https://example.com/posts/high-ticket-math-lessons",
                "booking_link_id": booking_link_id,
            },
        )

    assert response.status_code == 201
    assert response.headers.get("X-Request-Id")
    payload = response.json()
    assert payload["id"]
    assert payload["booking_link_id"] == booking_link_id
    assert payload["source_url"] == "https://example.com/posts/high-ticket-math-lessons"
    assert payload["tid"]
    assert payload["tracked_url"] == f"{tracked_base_url}/r/{payload['tid']}"

    persisted = _content_row(payload["id"])
    assert persisted == {
        "id": payload["id"],
        "creator_id": inserted["creator_id"],
        "booking_link_id": booking_link_id,
        "source_url": "https://example.com/posts/high-ticket-math-lessons",
        "tid": payload["tid"],
    }


def test_create_content_returns_distinct_tids_for_repeated_creates():
    inserted = _insert_creator_user(email=f"content_{uuid.uuid4().hex}@example.com")
    token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="Creator Strategy Call",
        calendly_url="https://calendly.com/example/creator-strategy-call",
    )

    with TestClient(app) as client:
        first_response = client.post(
            "/content",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "source_url": "https://example.com/posts/first-breakdown",
                "booking_link_id": booking_link_id,
            },
        )
        second_response = client.post(
            "/content",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "source_url": "https://example.com/posts/second-breakdown",
                "booking_link_id": booking_link_id,
            },
        )

    assert first_response.status_code == 201
    assert second_response.status_code == 201

    first_payload = first_response.json()
    second_payload = second_response.json()
    assert first_payload["tid"] != second_payload["tid"]
    assert first_payload["tracked_url"].endswith(f"/r/{first_payload['tid']}")
    assert second_payload["tracked_url"].endswith(f"/r/{second_payload['tid']}")
    assert _content_tids_for_creator(creator_id=inserted["creator_id"]) == [
        first_payload["tid"],
        second_payload["tid"],
    ]


def test_create_content_returns_same_404_for_unknown_and_not_owned_booking_link():
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
        name="Creator A Intro",
        calendly_url="https://calendly.com/example/creator-a-intro",
    )
    unknown_booking_link_id = str(uuid.uuid4())

    with TestClient(app) as client:
        not_owned_response = client.post(
            "/content",
            headers={"Authorization": f"Bearer {creator_b_token}"},
            json={
                "source_url": "https://example.com/posts/creator-a-content",
                "booking_link_id": creator_a_booking_link_id,
            },
        )
        unknown_response = client.post(
            "/content",
            headers={"Authorization": f"Bearer {creator_b_token}"},
            json={
                "source_url": "https://example.com/posts/unknown-booking-link",
                "booking_link_id": unknown_booking_link_id,
            },
        )

    assert not_owned_response.status_code == 404
    assert unknown_response.status_code == 404
    assert not_owned_response.json() == {"detail": "booking link not found"}
    assert unknown_response.json() == {"detail": "booking link not found"}
    assert _content_tids_for_creator(creator_id=creator_b["creator_id"]) == []
