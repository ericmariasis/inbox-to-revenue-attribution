import os
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

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


def test_phase2_booking_links_flow_end_to_end():
    creator_a_email = f"phase2_creator_a_{uuid.uuid4().hex}@example.com"
    creator_b_email = f"phase2_creator_b_{uuid.uuid4().hex}@example.com"

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

        creator_a_create_response = client.post(
            "/booking-links",
            headers={"Authorization": f"Bearer {creator_a_access_token}"},
            json={
                "name": "Creator A Intro",
                "calendly_url": "https://calendly.com/example/creator-a-intro",
            },
        )
        creator_a_list_response = client.get(
            "/booking-links",
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

        creator_b_list_response = client.get(
            "/booking-links",
            headers={"Authorization": f"Bearer {creator_b_access_token}"},
        )
        unauthenticated_list_response = client.get("/booking-links")

    creator_a_id = _creator_id_for_email(creator_a_email)
    creator_b_id = _creator_id_for_email(creator_b_email)
    created_booking_link = creator_a_create_response.json()

    assert creator_a_start_response.status_code == 200
    assert creator_a_start_response.json() == {"status": "ok"}
    assert creator_a_start_response.headers.get("X-Request-Id")

    assert creator_a_verify_response.status_code == 200
    assert creator_a_verify_response.json()["token_type"] == "bearer"
    assert creator_a_verify_response.headers.get("X-Request-Id")

    assert creator_a_create_response.status_code == 201
    assert creator_a_create_response.headers.get("X-Request-Id")
    assert created_booking_link["id"]
    assert created_booking_link["name"] == "Creator A Intro"
    assert created_booking_link["calendly_url"] == "https://calendly.com/example/creator-a-intro"

    assert creator_a_list_response.status_code == 200
    assert creator_a_list_response.headers.get("X-Request-Id")
    assert creator_a_list_response.json() == [created_booking_link]

    assert creator_b_start_response.status_code == 200
    assert creator_b_start_response.json() == {"status": "ok"}
    assert creator_b_start_response.headers.get("X-Request-Id")

    assert creator_b_verify_response.status_code == 200
    assert creator_b_verify_response.json()["token_type"] == "bearer"
    assert creator_b_verify_response.headers.get("X-Request-Id")

    assert creator_b_list_response.status_code == 200
    assert creator_b_list_response.headers.get("X-Request-Id")
    assert creator_b_list_response.json() == []

    assert unauthenticated_list_response.status_code == 401
    assert unauthenticated_list_response.json() == {"detail": "not authenticated"}

    assert creator_a_id != creator_b_id
    with _engine().connect() as conn:
        persisted_rows = conn.execute(
            text(
                "SELECT c.id AS creator_id, au.email, bl.name, bl.calendly_url "
                "FROM creators c "
                "JOIN auth_users au ON au.creator_id = c.id "
                "LEFT JOIN booking_links bl ON bl.creator_id = c.id "
                "WHERE au.email IN (:creator_a_email, :creator_b_email) "
                "ORDER BY au.email"
            ),
            {
                "creator_a_email": creator_a_email,
                "creator_b_email": creator_b_email,
            },
        ).mappings().all()

    assert persisted_rows == [
        {
            "creator_id": uuid.UUID(creator_a_id),
            "email": creator_a_email,
            "name": "Creator A Intro",
            "calendly_url": "https://calendly.com/example/creator-a-intro",
        },
        {
            "creator_id": uuid.UUID(creator_b_id),
            "email": creator_b_email,
            "name": None,
            "calendly_url": None,
        },
    ]
