import os
import uuid
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.main import app


def _engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def _insert_creator_user(
    *,
    email: str,
    name: str = "Redirect Creator",
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
    creator_id: str,
    booking_link_id: str,
    source_url: str,
    tid: str,
    created_at: datetime,
) -> str:
    content_id = str(uuid.uuid4())

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

    return content_id


def test_redirect_lookup_returns_public_302_to_persisted_booking_link():
    creator = _insert_creator_user(email=f"redirect_{uuid.uuid4().hex}@example.com")
    booking_link_url = "https://calendly.com/example/redirect-strategy-call"
    booking_link_id = _insert_booking_link(
        creator_id=creator["creator_id"],
        name="Redirect Strategy Call",
        calendly_url=booking_link_url,
    )
    tid = "redirectlookupknowntid"
    _insert_content(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/redirect-breakdown",
        tid=tid,
        created_at=datetime(2026, 3, 6, 15, 0, tzinfo=timezone.utc),
    )

    with TestClient(app) as client:
        response = client.get(f"/r/{tid}", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers.get("X-Request-Id")
    assert response.headers["location"] == booking_link_url


def test_redirect_lookup_returns_safe_404_for_unknown_tid():
    with TestClient(app) as client:
        response = client.get("/r/unknown-redirect-tid", follow_redirects=False)

    assert response.status_code == 404
    assert response.headers.get("X-Request-Id")
    assert response.json() == {"detail": "link not found"}
