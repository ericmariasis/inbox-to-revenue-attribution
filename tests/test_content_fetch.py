import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import create_engine, text

from app.core.config import get_settings
from app.main import app
from app.services.content_fetch import ContentFetchFailure, ContentFetchSuccess


def _engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def _insert_creator_user(*, email: str, name: str = "Content Fetch Creator") -> dict[str, str]:
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
                "stripe_connect_status": "pending",
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


def _insert_content(*, creator_id: str, booking_link_id: str, source_url: str, tid: str) -> str:
    content_id = str(uuid.uuid4())

    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO content "
                "(id, creator_id, booking_link_id, source_url, tid, created_at, updated_at) "
                "VALUES "
                "(:id, :creator_id, :booking_link_id, :source_url, :tid, NOW(), NOW())"
            ),
            {
                "id": content_id,
                "creator_id": creator_id,
                "booking_link_id": booking_link_id,
                "source_url": source_url,
                "tid": tid,
            },
        )

    return content_id


def _fetch_snapshot_rows(*, content_id: str) -> list[dict[str, object]]:
    with _engine().connect() as conn:
        rows = conn.execute(
            text(
                "SELECT content_id, requested_url, fetched_url, fetch_status, http_status, "
                "failure_reason_code, failure_detail, response_content_type, response_content_charset, snapshot_text "
                "FROM content_fetch_snapshots "
                "WHERE content_id = :content_id "
                "ORDER BY fetched_at ASC, id ASC"
            ),
            {"content_id": content_id},
        ).mappings().all()

    return [dict(row) for row in rows]


class _StubContentFetchProvider:
    def __init__(self, *results):
        self._results = list(results)
        self.calls: list[str] = []

    def fetch_public_url(self, *, source_url: str):
        self.calls.append(source_url)
        if not self._results:
            raise AssertionError("No stubbed fetch result remaining")
        return self._results.pop(0)


@contextmanager
def _override_app_state(name, value):
    had_attr = hasattr(app.state, name)
    previous_value = getattr(app.state, name, None)
    setattr(app.state, name, value)
    try:
        yield
    finally:
        if had_attr:
            setattr(app.state, name, previous_value)
        else:
            delattr(app.state, name)


def test_fetch_content_snapshot_requires_auth():
    with TestClient(app) as client:
        response = client.post(f"/content/{uuid.uuid4().hex}/fetch")

    assert response.status_code == 401
    assert response.json() == {"detail": "not authenticated"}


def test_fetch_content_snapshot_persists_successful_fetch_for_owned_content():
    inserted = _insert_creator_user(email=f"content_fetch_success_{uuid.uuid4().hex}@example.com")
    token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="Fetch Success Strategy",
        calendly_url="https://calendly.com/example/fetch-success-strategy",
    )
    tid = uuid.uuid4().hex
    content_id = _insert_content(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/fetch-success-story",
        tid=tid,
    )
    provider = _StubContentFetchProvider(
        ContentFetchSuccess(
            fetched_url="https://example.com/posts/fetch-success-story",
            http_status=200,
            response_content_type="text/html",
            response_content_charset="utf-8",
            snapshot_text="<html><body><article>Paid outcome proof</article></body></html>",
        )
    )

    with _override_app_state("content_fetch_provider", provider):
        with TestClient(app) as client:
            response = client.post(
                f"/content/{tid}/fetch",
                headers={"Authorization": f"Bearer {token}"},
            )

    assert response.status_code == 201
    assert response.headers.get("X-Request-Id")
    payload = response.json()
    assert payload["content_id"] == content_id
    assert payload["content_tid"] == tid
    assert payload["requested_url"] == "https://example.com/posts/fetch-success-story"
    assert payload["fetched_url"] == "https://example.com/posts/fetch-success-story"
    assert payload["fetch_status"] == "succeeded"
    assert payload["http_status"] == 200
    assert payload["failure_reason_code"] is None
    assert payload["response_content_type"] == "text/html"
    assert payload["response_content_charset"] == "utf-8"
    assert payload["snapshot_text"] == "<html><body><article>Paid outcome proof</article></body></html>"

    rows = _fetch_snapshot_rows(content_id=content_id)
    assert rows == [
        {
            "content_id": uuid.UUID(content_id),
            "requested_url": "https://example.com/posts/fetch-success-story",
            "fetched_url": "https://example.com/posts/fetch-success-story",
            "fetch_status": "succeeded",
            "http_status": 200,
            "failure_reason_code": None,
            "failure_detail": None,
            "response_content_type": "text/html",
            "response_content_charset": "utf-8",
            "snapshot_text": "<html><body><article>Paid outcome proof</article></body></html>",
        }
    ]
    assert provider.calls == ["https://example.com/posts/fetch-success-story"]


def test_fetch_content_snapshot_persists_failure_state_without_changing_content_row():
    inserted = _insert_creator_user(email=f"content_fetch_failure_{uuid.uuid4().hex}@example.com")
    token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="Fetch Failure Strategy",
        calendly_url="https://calendly.com/example/fetch-failure-strategy",
    )
    tid = uuid.uuid4().hex
    content_id = _insert_content(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/missing-story",
        tid=tid,
    )
    provider = _StubContentFetchProvider(
        ContentFetchFailure(
            fetched_url="https://example.com/posts/missing-story",
            http_status=404,
            reason_code="HTTP_ERROR",
            detail="Fetch returned HTTP 404.",
            response_content_type="text/html",
        )
    )

    with _override_app_state("content_fetch_provider", provider):
        with TestClient(app) as client:
            response = client.post(
                f"/content/{tid}/fetch",
                headers={"Authorization": f"Bearer {token}"},
            )

    assert response.status_code == 201
    payload = response.json()
    assert payload["content_id"] == content_id
    assert payload["fetch_status"] == "failed"
    assert payload["http_status"] == 404
    assert payload["failure_reason_code"] == "HTTP_ERROR"
    assert payload["failure_detail"] == "Fetch returned HTTP 404."
    assert payload["snapshot_text"] is None

    rows = _fetch_snapshot_rows(content_id=content_id)
    assert rows == [
        {
            "content_id": uuid.UUID(content_id),
            "requested_url": "https://example.com/posts/missing-story",
            "fetched_url": "https://example.com/posts/missing-story",
            "fetch_status": "failed",
            "http_status": 404,
            "failure_reason_code": "HTTP_ERROR",
            "failure_detail": "Fetch returned HTTP 404.",
            "response_content_type": "text/html",
            "response_content_charset": None,
            "snapshot_text": None,
        }
    ]
    assert provider.calls == ["https://example.com/posts/missing-story"]

    with _engine().connect() as conn:
        content_row = conn.execute(
            text("SELECT id, source_url, tid FROM content WHERE id = :id"),
            {"id": content_id},
        ).mappings().one()

    assert dict(content_row) == {
        "id": uuid.UUID(content_id),
        "source_url": "https://example.com/posts/missing-story",
        "tid": tid,
    }


def test_fetch_content_snapshot_returns_same_404_for_unknown_and_not_owned_tid():
    creator_a = _insert_creator_user(email=f"content_fetch_owner_{uuid.uuid4().hex}@example.com")
    creator_b = _insert_creator_user(email=f"content_fetch_other_{uuid.uuid4().hex}@example.com")
    token_b = _access_token(
        user_id=creator_b["user_id"],
        creator_id=creator_b["creator_id"],
        email=creator_b["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=creator_a["creator_id"],
        name="Fetch Isolation Strategy",
        calendly_url="https://calendly.com/example/fetch-isolation-strategy",
    )
    owner_tid = uuid.uuid4().hex
    _insert_content(
        creator_id=creator_a["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/fetch-isolation-story",
        tid=owner_tid,
    )
    provider = _StubContentFetchProvider(
        ContentFetchSuccess(
            fetched_url="https://example.com/posts/fetch-isolation-story",
            http_status=200,
            response_content_type="text/html",
            response_content_charset="utf-8",
            snapshot_text="<html></html>",
        )
    )

    with _override_app_state("content_fetch_provider", provider):
        with TestClient(app) as client:
            not_owned_response = client.post(
                f"/content/{owner_tid}/fetch",
                headers={"Authorization": f"Bearer {token_b}"},
            )
            unknown_response = client.post(
                f"/content/{uuid.uuid4().hex}/fetch",
                headers={"Authorization": f"Bearer {token_b}"},
            )

    assert not_owned_response.status_code == 404
    assert unknown_response.status_code == 404
    assert not_owned_response.json() == {"detail": "content not found"}
    assert unknown_response.json() == {"detail": "content not found"}
    assert provider.calls == []
