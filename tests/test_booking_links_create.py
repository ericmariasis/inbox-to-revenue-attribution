import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
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
    name: str = "Booking Link Creator",
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


def _booking_link_row(booking_link_id: str) -> dict[str, str | int | None]:
    with _engine().connect() as conn:
        row = conn.execute(
            text(
                "SELECT id, creator_id, name, calendly_url, billing_amount_cents, billing_currency "
                "FROM booking_links "
                "WHERE id = :id"
            ),
            {"id": booking_link_id},
        ).mappings().one()

    return {
        "id": str(row["id"]),
        "creator_id": str(row["creator_id"]),
        "name": row["name"],
        "calendly_url": row["calendly_url"],
        "billing_amount_cents": row["billing_amount_cents"],
        "billing_currency": row["billing_currency"],
    }


def test_create_booking_link_requires_auth():
    with TestClient(app) as client:
        response = client.post(
            "/booking-links",
            json={
                "name": "Free Consultation",
                "calendly_url": "https://calendly.com/example/free-consult",
            },
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "not authenticated"}


def test_create_booking_link_returns_201_and_persists_for_authenticated_creator():
    inserted = _insert_creator_user(email=f"booking_link_{uuid.uuid4().hex}@example.com")
    token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    with TestClient(app) as client:
        response = client.post(
            "/booking-links",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "name": "Free Consultation",
                "calendly_url": "https://calendly.com/example/free-consult",
            },
        )

    assert response.status_code == 201
    assert response.headers.get("X-Request-Id")
    payload = response.json()
    assert payload["name"] == "Free Consultation"
    assert payload["calendly_url"] == "https://calendly.com/example/free-consult"
    assert payload["billing_amount_cents"] is None
    assert payload["billing_currency"] is None
    assert payload["id"]

    persisted = _booking_link_row(payload["id"])
    assert persisted == {
        "id": payload["id"],
        "creator_id": inserted["creator_id"],
        "name": "Free Consultation",
        "calendly_url": "https://calendly.com/example/free-consult",
        "billing_amount_cents": None,
        "billing_currency": None,
    }


def test_create_booking_link_accepts_billing_defaults_and_normalizes_currency():
    inserted = _insert_creator_user(email=f"booking_link_{uuid.uuid4().hex}@example.com")
    token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    with TestClient(app) as client:
        response = client.post(
            "/booking-links",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "name": "Paid Deep Dive",
                "calendly_url": "https://calendly.com/example/paid-deep-dive",
                "billing_amount_cents": 15000,
                "billing_currency": " usd ",
            },
        )

    assert response.status_code == 201
    payload = response.json()
    assert payload["billing_amount_cents"] == 15000
    assert payload["billing_currency"] == "USD"

    persisted = _booking_link_row(payload["id"])
    assert persisted["billing_amount_cents"] == 15000
    assert persisted["billing_currency"] == "USD"


def test_create_booking_link_accepts_www_calendly_host():
    inserted = _insert_creator_user(email=f"booking_link_{uuid.uuid4().hex}@example.com")
    token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    with TestClient(app) as client:
        response = client.post(
            "/booking-links",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "name": "Website Consult",
                "calendly_url": "https://www.calendly.com/example/website-consult",
            },
        )

    assert response.status_code == 201
    payload = response.json()
    assert payload["calendly_url"] == "https://www.calendly.com/example/website-consult"


def test_create_booking_link_ignores_client_creator_id_input():
    inserted = _insert_creator_user(email=f"owner_{uuid.uuid4().hex}@example.com")
    token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    spoofed_creator_id = str(uuid.uuid4())

    with TestClient(app) as client:
        response = client.post(
            "/booking-links",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "creator_id": spoofed_creator_id,
                "name": "Discovery Call",
                "calendly_url": "https://calendly.com/example/discovery-call",
            },
        )

    assert response.status_code == 201
    persisted = _booking_link_row(response.json()["id"])
    assert persisted["creator_id"] == inserted["creator_id"]
    assert persisted["creator_id"] != spoofed_creator_id


@pytest.mark.parametrize(
    "payload_overrides",
    [
        {"billing_amount_cents": 0},
        {"billing_amount_cents": -100},
        {"billing_amount_cents": 12.5},
        {"billing_amount_cents": "1500"},
        {"billing_currency": "US"},
        {"billing_currency": "USDX"},
        {"billing_currency": "1$D"},
    ],
)
def test_create_booking_link_rejects_invalid_billing_input(payload_overrides: dict[str, object]):
    inserted = _insert_creator_user(email=f"invalid_booking_link_{uuid.uuid4().hex}@example.com")
    token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    payload = {
        "name": "Invalid Billing Booking Link",
        "calendly_url": "https://calendly.com/example/invalid-billing",
        **payload_overrides,
    }

    with TestClient(app) as client:
        response = client.post(
            "/booking-links",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "invalid_url",
    [
        "not-a-url",
        "http://calendly.com/example/free-consult",
        "https://example.com/free-consult",
        "https://calendly.com",
        "https://www.calendly.com/",
    ],
)
def test_create_booking_link_rejects_invalid_calendly_urls(invalid_url: str):
    inserted = _insert_creator_user(email=f"invalid_booking_link_{uuid.uuid4().hex}@example.com")
    token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    with TestClient(app) as client:
        response = client.post(
            "/booking-links",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "name": "Invalid Booking Link",
                "calendly_url": invalid_url,
            },
        )

    assert response.status_code == 422
