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


def test_list_content_requires_auth():
    with TestClient(app) as client:
        response = client.get("/content")

    assert response.status_code == 401
    assert response.json() == {"detail": "not authenticated"}


def test_list_content_returns_only_current_creators_rows_in_deterministic_order():
    creator_a = _insert_creator_user(email=f"creator_a_{uuid.uuid4().hex}@example.com")
    creator_b = _insert_creator_user(email=f"creator_b_{uuid.uuid4().hex}@example.com")
    token = _access_token(
        user_id=creator_a["user_id"],
        creator_id=creator_a["creator_id"],
        email=creator_a["email"],
        expires_delta=timedelta(hours=24),
    )
    creator_a_booking_link_id = _insert_booking_link(
        creator_id=creator_a["creator_id"],
        name="Creator A Strategy Call",
        calendly_url="https://calendly.com/example/creator-a-strategy-call",
    )
    creator_b_booking_link_id = _insert_booking_link(
        creator_id=creator_b["creator_id"],
        name="Creator B Strategy Call",
        calendly_url="https://calendly.com/example/creator-b-strategy-call",
    )

    shared_created_at = datetime(2026, 3, 6, 13, 0, tzinfo=timezone.utc)
    earlier_created_at = datetime(2026, 3, 6, 12, 0, tzinfo=timezone.utc)
    first_creator_a_content_id = "00000000-0000-0000-0000-000000000001"
    second_creator_a_content_id = "00000000-0000-0000-0000-000000000002"

    _insert_content(
        content_id=second_creator_a_content_id,
        creator_id=creator_a["creator_id"],
        booking_link_id=creator_a_booking_link_id,
        source_url="https://example.com/posts/creator-a-later-post",
        tid="creatoralatertid",
        created_at=shared_created_at,
    )
    _insert_content(
        content_id=first_creator_a_content_id,
        creator_id=creator_a["creator_id"],
        booking_link_id=creator_a_booking_link_id,
        source_url="https://example.com/posts/creator-a-earlier-post",
        tid="creatoraearliertid",
        created_at=earlier_created_at,
    )
    _insert_content(
        content_id=str(uuid.uuid4()),
        creator_id=creator_b["creator_id"],
        booking_link_id=creator_b_booking_link_id,
        source_url="https://example.com/posts/creator-b-post",
        tid="creatorbposttid",
        created_at=shared_created_at,
    )

    tracked_base_url = get_settings().tracked_link_base_url.rstrip("/")

    with TestClient(app) as client:
        response = client.get("/content", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.headers.get("X-Request-Id")
    assert response.json() == [
        {
            "id": first_creator_a_content_id,
            "booking_link_id": creator_a_booking_link_id,
            "source_url": "https://example.com/posts/creator-a-earlier-post",
            "tid": "creatoraearliertid",
            "tracked_url": f"{tracked_base_url}/r/creatoraearliertid",
        },
        {
            "id": second_creator_a_content_id,
            "booking_link_id": creator_a_booking_link_id,
            "source_url": "https://example.com/posts/creator-a-later-post",
            "tid": "creatoralatertid",
            "tracked_url": f"{tracked_base_url}/r/creatoralatertid",
        },
    ]


def test_list_content_hides_other_creators_rows_for_second_creator():
    creator_a = _insert_creator_user(email=f"creator_a_{uuid.uuid4().hex}@example.com")
    creator_b = _insert_creator_user(email=f"creator_b_{uuid.uuid4().hex}@example.com")
    token = _access_token(
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
    creator_b_booking_link_id = _insert_booking_link(
        creator_id=creator_b["creator_id"],
        name="Creator B Intro",
        calendly_url="https://calendly.com/example/creator-b-intro",
    )
    creator_b_content_id = str(uuid.uuid4())

    _insert_content(
        content_id=str(uuid.uuid4()),
        creator_id=creator_a["creator_id"],
        booking_link_id=creator_a_booking_link_id,
        source_url="https://example.com/posts/creator-a-post",
        tid="creatoraposttid",
        created_at=datetime(2026, 3, 6, 12, 0, tzinfo=timezone.utc),
    )
    _insert_content(
        content_id=creator_b_content_id,
        creator_id=creator_b["creator_id"],
        booking_link_id=creator_b_booking_link_id,
        source_url="https://example.com/posts/creator-b-post",
        tid="creatorbposttid",
        created_at=datetime(2026, 3, 6, 12, 5, tzinfo=timezone.utc),
    )

    tracked_base_url = get_settings().tracked_link_base_url.rstrip("/")

    with TestClient(app) as client:
        response = client.get("/content", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": creator_b_content_id,
            "booking_link_id": creator_b_booking_link_id,
            "source_url": "https://example.com/posts/creator-b-post",
            "tid": "creatorbposttid",
            "tracked_url": f"{tracked_base_url}/r/creatorbposttid",
        }
    ]
