import os
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.core.config import get_settings
from app.main import app
from app.services.email_stub import get_magic_link_outbox


def _engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def _creator_id_for_email(email: str) -> str:
    with _engine().connect() as conn:
        creator_id = conn.execute(
            text(
                "SELECT c.id "
                "FROM creators c "
                "JOIN auth_users au ON au.creator_id = c.id "
                "WHERE au.email = :email"
            ),
            {"email": email},
        ).scalar_one()

    return str(creator_id)


def _latest_magic_link_token_for_email(email: str) -> str:
    outbox = get_magic_link_outbox()
    for message in reversed(outbox):
        if message["email"] == email:
            return message["token"]

    raise AssertionError(f"No magic-link token captured for {email}")


def _phase3_rows_for_emails(*, creator_a_email: str, creator_b_email: str) -> list[dict[str, str | None]]:
    with _engine().connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.id AS creator_id, au.email, "
                "bl.id AS booking_link_id, bl.name AS booking_link_name, bl.calendly_url, "
                "ct.id AS content_id, ct.source_url, ct.tid "
                "FROM creators c "
                "JOIN auth_users au ON au.creator_id = c.id "
                "LEFT JOIN content ct ON ct.creator_id = c.id "
                "LEFT JOIN booking_links bl ON bl.id = ct.booking_link_id "
                "WHERE au.email IN (:creator_a_email, :creator_b_email) "
                "ORDER BY au.email, ct.created_at ASC, ct.id ASC"
            ),
            {
                "creator_a_email": creator_a_email,
                "creator_b_email": creator_b_email,
            },
        ).mappings().all()

    return [
        {
            "creator_id": str(row["creator_id"]),
            "email": row["email"],
            "booking_link_id": (
                str(row["booking_link_id"]) if row["booking_link_id"] is not None else None
            ),
            "booking_link_name": row["booking_link_name"],
            "calendly_url": row["calendly_url"],
            "content_id": str(row["content_id"]) if row["content_id"] is not None else None,
            "source_url": row["source_url"],
            "tid": row["tid"],
        }
        for row in rows
    ]


def test_phase3_content_tracking_flow_end_to_end():
    creator_a_email = f"phase3_creator_a_{uuid.uuid4().hex}@example.com"
    creator_b_email = f"phase3_creator_b_{uuid.uuid4().hex}@example.com"
    tracked_base_url = get_settings().tracked_link_base_url.rstrip("/")

    with TestClient(app) as client:
        creator_a_start_response = client.post(
            "/auth/magic-link/start",
            json={"email": creator_a_email},
        )
        creator_a_verify_response = client.get(
            "/auth/magic-link/verify",
            params={"token": _latest_magic_link_token_for_email(creator_a_email)},
        )
        creator_a_access_token = creator_a_verify_response.json()["access_token"]

        creator_a_create_booking_link_response = client.post(
            "/booking-links",
            headers={"Authorization": f"Bearer {creator_a_access_token}"},
            json={
                "name": "Creator A Attribution Call",
                "calendly_url": "https://calendly.com/example/creator-a-attribution-call",
            },
        )
        creator_a_booking_link = creator_a_create_booking_link_response.json()

        creator_a_create_content_response = client.post(
            "/content",
            headers={"Authorization": f"Bearer {creator_a_access_token}"},
            json={
                "source_url": "https://example.com/posts/phase3-revenue-breakdown",
                "booking_link_id": creator_a_booking_link["id"],
            },
        )
        created_content = creator_a_create_content_response.json()
        creator_a_list_content_response = client.get(
            "/content",
            headers={"Authorization": f"Bearer {creator_a_access_token}"},
        )
        creator_a_detail_content_response = client.get(
            f"/content/{created_content['tid']}",
            headers={"Authorization": f"Bearer {creator_a_access_token}"},
        )

        creator_b_start_response = client.post(
            "/auth/magic-link/start",
            json={"email": creator_b_email},
        )
        creator_b_verify_response = client.get(
            "/auth/magic-link/verify",
            params={"token": _latest_magic_link_token_for_email(creator_b_email)},
        )
        creator_b_access_token = creator_b_verify_response.json()["access_token"]

        creator_b_list_content_response = client.get(
            "/content",
            headers={"Authorization": f"Bearer {creator_b_access_token}"},
        )
        creator_b_detail_content_response = client.get(
            f"/content/{created_content['tid']}",
            headers={"Authorization": f"Bearer {creator_b_access_token}"},
        )
        creator_b_not_owned_create_response = client.post(
            "/content",
            headers={"Authorization": f"Bearer {creator_b_access_token}"},
            json={
                "source_url": "https://example.com/posts/not-owned-phase3-content",
                "booking_link_id": creator_a_booking_link["id"],
            },
        )

        unauthenticated_create_response = client.post(
            "/content",
            json={
                "source_url": "https://example.com/posts/unauth-phase3-content",
                "booking_link_id": creator_a_booking_link["id"],
            },
        )
        unauthenticated_list_response = client.get("/content")
        unauthenticated_detail_response = client.get(f"/content/{created_content['tid']}")

    creator_a_id = _creator_id_for_email(creator_a_email)
    creator_b_id = _creator_id_for_email(creator_b_email)

    assert creator_a_start_response.status_code == 200
    assert creator_a_start_response.json() == {"status": "ok"}
    assert creator_a_start_response.headers.get("X-Request-Id")

    assert creator_a_verify_response.status_code == 200
    assert creator_a_verify_response.json()["token_type"] == "bearer"
    assert creator_a_verify_response.headers.get("X-Request-Id")

    assert creator_a_create_booking_link_response.status_code == 201
    assert creator_a_create_booking_link_response.headers.get("X-Request-Id")
    assert creator_a_booking_link["id"]
    assert creator_a_booking_link["name"] == "Creator A Attribution Call"
    assert (
        creator_a_booking_link["calendly_url"]
        == "https://calendly.com/example/creator-a-attribution-call"
    )

    assert creator_a_create_content_response.status_code == 201
    assert creator_a_create_content_response.headers.get("X-Request-Id")
    assert created_content["id"]
    assert created_content["booking_link_id"] == creator_a_booking_link["id"]
    assert created_content["source_url"] == "https://example.com/posts/phase3-revenue-breakdown"
    assert created_content["tracked_url"] == f"{tracked_base_url}/r/{created_content['tid']}"
    assert uuid.UUID(hex=created_content["tid"]).hex == created_content["tid"]

    assert creator_a_list_content_response.status_code == 200
    assert creator_a_list_content_response.headers.get("X-Request-Id")
    assert creator_a_list_content_response.json() == [created_content]

    assert creator_a_detail_content_response.status_code == 200
    assert creator_a_detail_content_response.headers.get("X-Request-Id")
    assert creator_a_detail_content_response.json() == created_content

    assert creator_b_start_response.status_code == 200
    assert creator_b_start_response.json() == {"status": "ok"}
    assert creator_b_start_response.headers.get("X-Request-Id")

    assert creator_b_verify_response.status_code == 200
    assert creator_b_verify_response.json()["token_type"] == "bearer"
    assert creator_b_verify_response.headers.get("X-Request-Id")

    assert creator_b_list_content_response.status_code == 200
    assert creator_b_list_content_response.headers.get("X-Request-Id")
    assert creator_b_list_content_response.json() == []

    assert creator_b_detail_content_response.status_code == 404
    assert creator_b_detail_content_response.json() == {"detail": "content not found"}

    assert creator_b_not_owned_create_response.status_code == 404
    assert creator_b_not_owned_create_response.json() == {"detail": "booking link not found"}

    assert unauthenticated_create_response.status_code == 401
    assert unauthenticated_create_response.json() == {"detail": "not authenticated"}

    assert unauthenticated_list_response.status_code == 401
    assert unauthenticated_list_response.json() == {"detail": "not authenticated"}

    assert unauthenticated_detail_response.status_code == 401
    assert unauthenticated_detail_response.json() == {"detail": "not authenticated"}

    assert creator_a_id != creator_b_id
    assert _phase3_rows_for_emails(
        creator_a_email=creator_a_email,
        creator_b_email=creator_b_email,
    ) == [
        {
            "creator_id": creator_a_id,
            "email": creator_a_email,
            "booking_link_id": creator_a_booking_link["id"],
            "booking_link_name": "Creator A Attribution Call",
            "calendly_url": "https://calendly.com/example/creator-a-attribution-call",
            "content_id": created_content["id"],
            "source_url": "https://example.com/posts/phase3-revenue-breakdown",
            "tid": created_content["tid"],
        },
        {
            "creator_id": creator_b_id,
            "email": creator_b_email,
            "booking_link_id": None,
            "booking_link_name": None,
            "calendly_url": None,
            "content_id": None,
            "source_url": None,
            "tid": None,
        },
    ]
