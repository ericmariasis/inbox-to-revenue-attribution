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


def test_phase1_magic_link_flow_end_to_end():
    email = f"phase1_{uuid.uuid4().hex}@example.com"

    with TestClient(app) as client:
        start_response = client.post("/auth/magic-link/start", json={"email": email})

        outbox = get_magic_link_outbox()
        assert len(outbox) == 1
        assert outbox[0]["email"] == email
        raw_token = outbox[0]["token"]

        verify_response = client.get(
            "/auth/magic-link/verify",
            params={"token": raw_token},
        )
        access_token = verify_response.json()["access_token"]

        me_response = client.get(
            "/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        unauthenticated_me_response = client.get("/me")

    creator_id = _creator_id_for_email(email)

    assert start_response.status_code == 200
    assert start_response.json() == {"status": "ok"}
    assert start_response.headers.get("X-Request-Id")

    assert verify_response.status_code == 200
    assert verify_response.json()["token_type"] == "bearer"
    assert verify_response.headers.get("X-Request-Id")

    assert me_response.status_code == 200
    assert me_response.headers.get("X-Request-Id")
    assert me_response.json()["id"] == creator_id
    assert me_response.json()["email"] == email

    assert unauthenticated_me_response.status_code == 401
    assert unauthenticated_me_response.json() == {"detail": "not authenticated"}
