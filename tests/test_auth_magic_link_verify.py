import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import create_engine, text

from app.main import app


def _engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def _insert_user_with_token(email: str, raw_token: str, *, expires_at: datetime, used_at: datetime | None = None):
    creator_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    token_id = str(uuid.uuid4())
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    with _engine().begin() as conn:
        conn.execute(
            text("INSERT INTO creators (id, name) VALUES (:id, :name)"),
            {"id": creator_id, "name": "Verify Creator"},
        )
        conn.execute(
            text(
                "INSERT INTO auth_users (id, creator_id, email) "
                "VALUES (:id, :creator_id, :email)"
            ),
            {"id": user_id, "creator_id": creator_id, "email": email},
        )
        conn.execute(
            text(
                "INSERT INTO magic_link_tokens (id, user_id, token_hash, expires_at, used_at) "
                "VALUES (:id, :user_id, :token_hash, :expires_at, :used_at)"
            ),
            {
                "id": token_id,
                "user_id": user_id,
                "token_hash": token_hash,
                "expires_at": expires_at,
                "used_at": used_at,
            },
        )

    return {
        "creator_id": creator_id,
        "user_id": user_id,
        "token_hash": token_hash,
    }


def test_verify_valid_token_returns_jwt_and_marks_used():
    raw_token = "verify-valid-token"
    email = f"verify_{uuid.uuid4().hex}@example.com"
    inserted = _insert_user_with_token(
        email,
        raw_token,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
    )

    with TestClient(app) as client:
        response = client.get("/auth/magic-link/verify", params={"token": raw_token})

    assert response.status_code == 200
    assert response.headers.get("X-Request-Id")
    assert response.json()["token_type"] == "bearer"

    token = response.json()["access_token"]
    payload = jwt.get_unverified_claims(token)
    assert payload["sub"] == inserted["user_id"]
    assert payload["creator_id"] == inserted["creator_id"]
    assert payload["email"] == email
    assert payload["exp"] > payload["iat"]

    with _engine().connect() as conn:
        used_at = conn.execute(
            text(
                "SELECT used_at FROM magic_link_tokens "
                "WHERE token_hash = :token_hash"
            ),
            {"token_hash": inserted["token_hash"]},
        ).scalar_one()

    assert used_at is not None


def test_verify_token_can_only_be_used_once():
    raw_token = "verify-once-token"
    email = f"verify_once_{uuid.uuid4().hex}@example.com"
    _insert_user_with_token(
        email,
        raw_token,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
    )

    with TestClient(app) as client:
        first = client.get("/auth/magic-link/verify", params={"token": raw_token})
        second = client.get("/auth/magic-link/verify", params={"token": raw_token})

    assert first.status_code == 200
    assert second.status_code == 401
    assert second.json() == {"detail": "invalid or expired token"}


def test_verify_expired_token_returns_uniform_401():
    raw_token = "verify-expired-token"
    email = f"verify_expired_{uuid.uuid4().hex}@example.com"
    _insert_user_with_token(
        email,
        raw_token,
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )

    with TestClient(app) as client:
        response = client.get("/auth/magic-link/verify", params={"token": raw_token})

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid or expired token"}


def test_verify_unknown_token_returns_uniform_401():
    with TestClient(app) as client:
        response = client.get("/auth/magic-link/verify", params={"token": "unknown-token"})

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid or expired token"}


def test_verify_logs_do_not_include_plaintext_token_or_hash():
    raw_token = "verify-log-token"
    email = f"verify_logs_{uuid.uuid4().hex}@example.com"
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    _insert_user_with_token(
        email,
        raw_token,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
    )

    with patch("app.services.auth_magic_link.logger.info") as info_log, patch(
        "app.services.auth_magic_link.logger.warning"
    ) as warning_log:
        with TestClient(app) as client:
            response = client.get("/auth/magic-link/verify", params={"token": raw_token})

    assert response.status_code == 200
    assert response.headers.get("X-Request-Id")

    combined = "\n".join([str(c) for c in info_log.call_args_list + warning_log.call_args_list])
    assert email in combined
    assert raw_token not in combined
    assert token_hash not in combined
    assert "token_hash" not in combined
