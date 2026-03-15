import hashlib
import os
import uuid
from contextlib import contextmanager
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.main import app
from app.services.auth_magic_link import START_RETRY_DETAIL
from app.services.email_provider import MagicLinkEmailDeliveryError


def _engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def _count_rows(conn, table_name: str) -> int:
    return conn.execute(text(f"SELECT count(*) FROM {table_name}")).scalar_one()


def _issuance_count_for_email(conn, email: str) -> int:
    return conn.execute(
        text(
            "SELECT "
            "  (SELECT count(*) FROM pending_magic_link_issuances WHERE email = :email) "
            "+ ("
            "    SELECT count(*) "
            "    FROM magic_link_tokens mlt "
            "    JOIN auth_users au ON au.id = mlt.user_id "
            "    WHERE au.email = :email"
            "  )"
        ),
        {"email": email},
    ).scalar_one()


def _identity_counts_for_email(conn, email: str) -> dict[str, int]:
    row = conn.execute(
        text(
            "SELECT "
            "  (SELECT count(*) FROM auth_users WHERE email = :email) AS auth_user_count, "
            "  ("
            "    SELECT count(*) "
            "    FROM creators c "
            "    JOIN auth_users au ON au.creator_id = c.id "
            "    WHERE au.email = :email"
            "  ) AS creator_count"
        ),
        {"email": email},
    ).mappings().one()
    return {
        "auth_users": row["auth_user_count"],
        "creators": row["creator_count"],
    }


@contextmanager
def _override_app_state(name, value):
    had_attr = hasattr(app.state, name)
    previous_value = getattr(app.state, name, None)
    marker_name = f"_{name}_overridden"
    had_marker = hasattr(app.state, marker_name)
    previous_marker = getattr(app.state, marker_name, None)
    setattr(app.state, name, value)
    setattr(app.state, marker_name, True)
    try:
        yield
    finally:
        if had_attr:
            setattr(app.state, name, previous_value)
        else:
            delattr(app.state, name)
        if had_marker:
            setattr(app.state, marker_name, previous_marker)
        else:
            delattr(app.state, marker_name)


class _CaptureEmailProvider:
    def __init__(self):
        self.messages = []

    def send_magic_link(self, message) -> None:
        self.messages.append(message)


class _FailingEmailProvider:
    def __init__(self, *, error_text: str):
        self.error_text = error_text

    def send_magic_link(self, message) -> None:
        raise MagicLinkEmailDeliveryError(self.error_text)


def test_start_new_email_creates_pending_issuance_only():
    email = f"new_{uuid.uuid4().hex}@example.com"

    with TestClient(app) as client:
        response = client.post("/auth/magic-link/start", json={"email": email})

    assert response.status_code == 200

    with _engine().connect() as conn:
        assert _identity_counts_for_email(conn, email) == {"auth_users": 0, "creators": 0}
        assert _count_rows(conn, "creators") == 0
        assert _count_rows(conn, "auth_users") == 0
        assert _count_rows(conn, "magic_link_tokens") == 0
        assert _count_rows(conn, "pending_magic_link_issuances") == 1

        row = conn.execute(
            text(
                "SELECT email, token_hash "
                "FROM pending_magic_link_issuances "
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
        pending = _count_rows(conn, "pending_magic_link_issuances")

        assert creators == 1
        assert users == 1
        assert tokens == 1
        assert pending == 0


def test_start_uses_configured_provider_and_generates_magic_link_url():
    email = f"provider_{uuid.uuid4().hex}@example.com"
    provider = _CaptureEmailProvider()

    with _override_app_state("email_provider", provider):
        with TestClient(app) as client:
            response = client.post("/auth/magic-link/start", json={"email": email})

    assert response.status_code == 200
    assert len(provider.messages) == 1

    message = provider.messages[0]
    assert message.email == email
    parsed_url = urlparse(message.magic_link_url)
    assert parsed_url.scheme == "http"
    assert parsed_url.netloc == "localhost:8000"
    assert parsed_url.path == "/auth/magic-link/verify"
    assert parse_qs(parsed_url.query) == {"token": [message.raw_token]}

    with _engine().connect() as conn:
        token_hash = conn.execute(
            text(
                "SELECT token_hash "
                "FROM pending_magic_link_issuances "
                "WHERE email = :email "
                "ORDER BY created_at DESC "
                "LIMIT 1"
            ),
            {"email": email},
        ).scalar_one()

    assert token_hash == hashlib.sha256(message.raw_token.encode("utf-8")).hexdigest()


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
                "SELECT token_hash "
                "FROM pending_magic_link_issuances "
                "WHERE email = :email "
                "ORDER BY created_at DESC "
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
        token_count = _issuance_count_for_email(conn, email)

    assert token_count == 5


def test_start_provider_failure_returns_generic_retry_guidance(monkeypatch):
    email = f"provider_fail_{uuid.uuid4().hex}@example.com"
    raw_token = "RAW_TOKEN_PROVIDER_FAILURE_789"
    provider_error = "smtp timeout: provider outage"
    provider = _FailingEmailProvider(error_text=provider_error)

    monkeypatch.setattr(
        "app.services.auth_magic_link.secrets.token_urlsafe",
        lambda _: raw_token,
    )

    with _override_app_state("email_provider", provider), patch(
        "app.services.auth_magic_link.logger.warning"
    ) as warning_log:
        with TestClient(app) as client:
            response = client.post("/auth/magic-link/start", json={"email": email})

    assert response.status_code == 503
    assert response.json() == {"detail": START_RETRY_DETAIL}
    assert provider_error not in response.text

    with _engine().connect() as conn:
        assert _identity_counts_for_email(conn, email) == {"auth_users": 0, "creators": 0}
        assert _issuance_count_for_email(conn, email) == 1

    call_text = "\n".join(str(call) for call in warning_log.call_args_list)
    assert email in call_text
    assert raw_token not in call_text
    assert "token_hash" not in call_text


def test_start_provider_failure_does_not_consume_rate_limit():
    email = f"provider_retry_{uuid.uuid4().hex}@example.com"
    provider = _FailingEmailProvider(error_text="smtp timeout: provider outage")

    with _override_app_state("email_provider", provider):
        with TestClient(app) as client:
            failed_response = client.post("/auth/magic-link/start", json={"email": email})

    assert failed_response.status_code == 503

    with TestClient(app) as client:
        success_responses = [
            client.post("/auth/magic-link/start", json={"email": email})
            for _ in range(5)
        ]

    assert all(response.status_code == 200 for response in success_responses)

    with _engine().connect() as conn:
        assert _issuance_count_for_email(conn, email) == 6
        assert _identity_counts_for_email(conn, email) == {"auth_users": 0, "creators": 0}


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
