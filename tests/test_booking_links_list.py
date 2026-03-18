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


def _insert_booking_link(
    *,
    creator_id: str,
    name: str,
    calendly_url: str | None = None,
    provider: str | None = None,
    destination_url: str | None = None,
    billing_amount_cents: int | None = None,
    billing_currency: str | None = None,
) -> str:
    booking_link_id = str(uuid.uuid4())
    provider = provider or "calendly"
    destination_url = destination_url or calendly_url

    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO booking_links "
                "(id, creator_id, name, provider, destination_url, calendly_url, billing_amount_cents, billing_currency) "
                "VALUES (:id, :creator_id, :name, :provider, :destination_url, :calendly_url, :billing_amount_cents, :billing_currency)"
            ),
            {
                "id": booking_link_id,
                "creator_id": creator_id,
                "name": name,
                "provider": provider,
                "destination_url": destination_url,
                "calendly_url": calendly_url,
                "billing_amount_cents": billing_amount_cents,
                "billing_currency": billing_currency,
            },
        )

    return booking_link_id


def test_list_booking_links_requires_auth():
    with TestClient(app) as client:
        response = client.get("/booking-links")

    assert response.status_code == 401
    assert response.json() == {"detail": "not authenticated"}


def test_list_booking_links_returns_only_current_creators_rows():
    creator_a = _insert_creator_user(email=f"creator_a_{uuid.uuid4().hex}@example.com")
    creator_b = _insert_creator_user(email=f"creator_b_{uuid.uuid4().hex}@example.com")
    token = _access_token(
        user_id=creator_a["user_id"],
        creator_id=creator_a["creator_id"],
        email=creator_a["email"],
        expires_delta=timedelta(hours=24),
    )

    discovery_id = _insert_booking_link(
        creator_id=creator_a["creator_id"],
        name="Discovery Call",
        provider="calendly",
        destination_url="https://calendly.com/example/discovery-call",
        calendly_url="https://calendly.com/example/discovery-call",
        billing_amount_cents=20000,
        billing_currency="USD",
    )
    strategy_id = _insert_booking_link(
        creator_id=creator_a["creator_id"],
        name="FS1 Personal Calendar",
        provider="fullscope",
        destination_url="https://links.fullscope.tools/widget/bookings/fs1-personal-calendar",
    )
    _insert_booking_link(
        creator_id=creator_b["creator_id"],
        name="Other Creator Intro",
        provider="calendly",
        destination_url="https://calendly.com/example/other-creator-intro",
        calendly_url="https://calendly.com/example/other-creator-intro",
    )

    with TestClient(app) as client:
        response = client.get("/booking-links", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.headers.get("X-Request-Id")
    assert response.json() == [
        {
            "id": discovery_id,
            "name": "Discovery Call",
            "provider": "calendly",
            "destination_url": "https://calendly.com/example/discovery-call",
            "calendly_url": "https://calendly.com/example/discovery-call",
            "billing_amount_cents": 20000,
            "billing_currency": "USD",
        },
        {
            "id": strategy_id,
            "name": "FS1 Personal Calendar",
            "provider": "fullscope",
            "destination_url": "https://links.fullscope.tools/widget/bookings/fs1-personal-calendar",
            "calendly_url": None,
            "billing_amount_cents": None,
            "billing_currency": None,
        },
    ]


def test_list_booking_links_hides_other_creators_rows_for_second_creator():
    creator_a = _insert_creator_user(email=f"creator_a_{uuid.uuid4().hex}@example.com")
    creator_b = _insert_creator_user(email=f"creator_b_{uuid.uuid4().hex}@example.com")
    token = _access_token(
        user_id=creator_b["user_id"],
        creator_id=creator_b["creator_id"],
        email=creator_b["email"],
        expires_delta=timedelta(hours=24),
    )

    _insert_booking_link(
        creator_id=creator_a["creator_id"],
        name="Creator A Session",
        provider="calendly",
        destination_url="https://calendly.com/example/creator-a-session",
        calendly_url="https://calendly.com/example/creator-a-session",
    )
    creator_b_link_id = _insert_booking_link(
        creator_id=creator_b["creator_id"],
        name="Creator B Session",
        provider="calendly",
        destination_url="https://calendly.com/example/creator-b-session",
        calendly_url="https://calendly.com/example/creator-b-session",
        billing_amount_cents=9000,
        billing_currency="EUR",
    )

    with TestClient(app) as client:
        response = client.get("/booking-links", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": creator_b_link_id,
            "name": "Creator B Session",
            "provider": "calendly",
            "destination_url": "https://calendly.com/example/creator-b-session",
            "calendly_url": "https://calendly.com/example/creator-b-session",
            "billing_amount_cents": 9000,
            "billing_currency": "EUR",
        }
    ]
