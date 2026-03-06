import hashlib
import io
import logging
import os
import uuid

from unittest.mock import patch
import json

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.core.logging import JsonFormatter, RequestContextFilter
from app.main import app


def _engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def _count_rows(conn, table_name: str) -> int:
    return conn.execute(text(f"SELECT count(*) FROM {table_name}")).scalar_one()


def _token_count_for_email(conn, email: str) -> int:
    return conn.execute(
        text(
            "SELECT count(*) "
            "FROM magic_link_tokens mlt "
            "JOIN auth_users au ON au.id = mlt.user_id "
            "WHERE au.email = :email"
        ),
        {"email": email},
    ).scalar_one()


def test_start_new_email_creates_creator_user_and_token():
    email = f"new_{uuid.uuid4().hex}@example.com"

    with TestClient(app) as client:
        response = client.post("/auth/magic-link/start", json={"email": email})

    assert response.status_code == 200

    with _engine().connect() as conn:
        assert _count_rows(conn, "creators") == 1
        assert _count_rows(conn, "auth_users") == 1
        assert _count_rows(conn, "magic_link_tokens") == 1

        row = conn.execute(
            text(
                "SELECT au.email, mlt.token_hash "
                "FROM auth_users au "
                "JOIN magic_link_tokens mlt ON mlt.user_id = au.id "
                "LIMIT 1"
            )
        ).mappings().one()
        assert row["email"] == email
        assert row["token_hash"]


def test_start_existing_email_returns_200_and_creates_new_token():
    email = f"existing_{uuid.uuid4().hex}@example.com"
    creator_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())

    with _engine().begin() as conn:
        conn.execute(
            text("INSERT INTO creators (id, name) VALUES (:id, :name)"),
            {"id": creator_id, "name": "Existing Creator"},
        )
        conn.execute(
            text(
                "INSERT INTO auth_users (id, creator_id, email) "
                "VALUES (:id, :creator_id, :email)"
            ),
            {"id": user_id, "creator_id": creator_id, "email": email},
        )

    with TestClient(app) as client:
        response = client.post("/auth/magic-link/start", json={"email": email})

    assert response.status_code == 200

    with _engine().connect() as conn:
        creators = _count_rows(conn, "creators")
        users = _count_rows(conn, "auth_users")
        tokens = _count_rows(conn, "magic_link_tokens")

        assert creators == 1
        assert users == 1
        assert tokens == 1


def test_token_is_hashed_not_plaintext(monkeypatch):
    email = f"token_{uuid.uuid4().hex}@example.com"
    raw_token = "RAW_TOKEN_FOR_TEST_ONLY_123"

    monkeypatch.setattr(
        "app.services.auth_magic_link.secrets.token_urlsafe",
        lambda _: raw_token,
    )

    with TestClient(app) as client:
        response = client.post("/auth/magic-link/start", json={"email": email})

    assert response.status_code == 200

    payload = response.json()
    assert "token" not in payload
    assert "magic_link" not in payload
    assert "magic_link_token" not in payload

    with _engine().connect() as conn:
        token_hash = conn.execute(
            text(
                "SELECT mlt.token_hash "
                "FROM magic_link_tokens mlt "
                "JOIN auth_users au ON au.id = mlt.user_id "
                "WHERE au.email = :email "
                "ORDER BY mlt.created_at DESC "
                "LIMIT 1"
            ),
            {"email": email},
        ).scalar_one()

    assert token_hash
    assert token_hash != raw_token
    assert token_hash == hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def test_rate_limit_max_5_per_hour_per_email():
    email = f"ratelimit_{uuid.uuid4().hex}@example.com"

    with TestClient(app) as client:
        responses = [
            client.post("/auth/magic-link/start", json={"email": email})
            for _ in range(6)
        ]

    first_five = responses[:5]
    sixth = responses[5]

    assert all(r.status_code == 200 for r in first_five)
    # v1 choice: keep anti-enumeration behavior consistent by returning 200 on overflow.
    assert sixth.status_code == 200

    with _engine().connect() as conn:
        token_count = _token_count_for_email(conn, email)

    assert token_count == 5


def test_logs_do_not_include_token_or_hash(monkeypatch):
    email = f"logs_{uuid.uuid4().hex}@example.com"
    raw_token = "RAW_TOKEN_FOR_LOG_CHECK_456"
    raw_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    monkeypatch.setattr(
        "app.services.auth_magic_link.secrets.token_urlsafe",
        lambda _: raw_token,
    )

    with patch("app.services.auth_magic_link.logger.info") as auth_log, patch(
        "app.services.email_stub.logger.info"
    ) as email_log:
        with TestClient(app) as client:
            response = client.post("/auth/magic-link/start", json={"email": email})

    assert response.status_code == 200

    call_text = "\n".join(
        [str(c) for c in auth_log.call_args_list + email_log.call_args_list]
    )

    # expected observability
    assert email in call_text

    # sensitive values must not be logged
    assert raw_token not in call_text
    assert raw_hash not in call_text
    assert "token_hash" not in call_text


def test_invalid_email_returns_422():
    with TestClient(app) as client:
        response = client.post("/auth/magic-link/start", json={"email": "not-an-email"})

    assert response.status_code == 422

def test_start_response_identical_for_new_and_existing_email():
    new_email = f"new_{uuid.uuid4().hex}@example.com"
    existing_email = f"existing_{uuid.uuid4().hex}@example.com"

    creator_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    with _engine().begin() as conn:
        conn.execute(
            text("INSERT INTO creators (id, name) VALUES (:id, :name)"),
            {"id": creator_id, "name": "Existing Creator"},
        )
        conn.execute(
            text(
                "INSERT INTO auth_users (id, creator_id, email) "
                "VALUES (:id, :creator_id, :email)"
            ),
            {"id": user_id, "creator_id": creator_id, "email": existing_email},
        )

    with TestClient(app) as client:
        r_new = client.post("/auth/magic-link/start", json={"email": new_email})
        r_existing = client.post("/auth/magic-link/start", json={"email": existing_email})

    assert r_new.status_code == 200
    assert r_existing.status_code == 200
    assert r_new.json() == {"status": "ok"}
    assert r_existing.json() == {"status": "ok"}

def test_auth_start_logs_include_request_id_and_email_without_token(monkeypatch):
    email = f"logreal_{uuid.uuid4().hex}@example.com"
    raw_token = "RAW_TOKEN_FOR_REAL_LOG_TEST"
    raw_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    monkeypatch.setattr(
        "app.services.auth_magic_link.secrets.token_urlsafe",
        lambda _: raw_token,
    )

    with patch("app.services.auth_magic_link.logger.info") as auth_log, patch(
        "app.services.email_stub.logger.info"
    ) as email_log:
        with TestClient(app) as client:
            response = client.post("/auth/magic-link/start", json={"email": email})
        assert response.status_code == 200

    assert response.headers.get("X-Request-Id")

    combined = "\n".join(
        [str(c) for c in auth_log.call_args_list + email_log.call_args_list]
    )
    assert email in combined

    assert raw_token not in combined
    assert raw_hash not in combined
    assert "token_hash" not in combined
