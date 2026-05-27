import argparse
import hashlib
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, text

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.config import get_settings
from app.db.url import normalize_database_url


@dataclass(frozen=True)
class GrowthLoopDemoSeed:
    creator_email: str
    login_url: str
    backup_login_url: str
    growth_loop_url: str
    app_url: str
    reports_url: str
    expected_stage: str
    expected_revenue: str


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Seed one paid PayPal-shaped Growth Loop Agent demo workspace and print "
            "browser URLs for the walkthrough."
        ),
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="Browser base URL used in printed login and app links.",
    )
    parser.add_argument(
        "--creator-email",
        default=None,
        help="Optional fixed creator email. Defaults to a generated demo address.",
    )
    return parser


def _database_url() -> str:
    database_url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if database_url:
        return normalize_database_url(database_url)
    return normalize_database_url(get_settings().database_url)


def _normalize_base_url(raw_value: str) -> str:
    value = str(raw_value).strip().rstrip("/")
    if not value.startswith(("http://", "https://")):
        raise SystemExit("error=--base-url must start with http:// or https://")
    return value


def _token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _magic_link_token(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _insert_magic_link_token(*, conn, user_id: str, raw_token: str) -> None:
    conn.execute(
        text(
            "INSERT INTO magic_link_tokens (id, user_id, token_hash, expires_at) "
            "VALUES (:id, :user_id, :token_hash, :expires_at)"
        ),
        {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "token_hash": _token_hash(raw_token),
            "expires_at": datetime.now(timezone.utc) + timedelta(hours=24),
        },
    )


def _insert_creator_user(*, conn, email: str, suffix: str) -> tuple[str, str, str]:
    creator_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    merchant_id = f"merchant_growth_loop_demo_{suffix}"
    connected_at = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)

    conn.execute(
        text(
            "INSERT INTO creators ("
            "id, name, billing_provider, billing_connect_status, billing_account_id, "
            "billing_provider_correlation_id, billing_connected_at, "
            "stripe_connect_status, stripe_account_id, stripe_connected_at"
            ") VALUES ("
            ":id, :name, :billing_provider, :billing_connect_status, :billing_account_id, "
            ":billing_provider_correlation_id, :billing_connected_at, "
            ":stripe_connect_status, :stripe_account_id, :stripe_connected_at"
            ")"
        ),
        {
            "id": creator_id,
            "name": "Growth Loop Demo Tutor",
            "billing_provider": "paypal",
            "billing_connect_status": "connected",
            "billing_account_id": merchant_id,
            "billing_provider_correlation_id": f"tracking_growth_loop_demo_{suffix}",
            "billing_connected_at": connected_at,
            "stripe_connect_status": "pending",
            "stripe_account_id": None,
            "stripe_connected_at": None,
        },
    )
    conn.execute(
        text(
            "INSERT INTO auth_users (id, creator_id, email) "
            "VALUES (:id, :creator_id, :email)"
        ),
        {"id": user_id, "creator_id": creator_id, "email": email},
    )
    return creator_id, user_id, merchant_id


def _insert_booking_link(*, conn, creator_id: str, suffix: str) -> str:
    booking_link_id = str(uuid.uuid4())
    calendly_url = f"https://calendly.com/example/growth-loop-demo-{suffix}"

    conn.execute(
        text(
            "INSERT INTO booking_links "
            "(id, creator_id, name, provider, destination_url, calendly_url, billing_amount_cents, billing_currency) "
            "VALUES "
            "(:id, :creator_id, :name, :provider, :destination_url, :calendly_url, :billing_amount_cents, :billing_currency)"
        ),
        {
            "id": booking_link_id,
            "creator_id": creator_id,
            "name": "Career-change strategy call",
            "provider": "calendly",
            "destination_url": calendly_url,
            "calendly_url": calendly_url,
            "billing_amount_cents": 19500,
            "billing_currency": "USD",
        },
    )
    return booking_link_id


def _insert_content(*, conn, creator_id: str, booking_link_id: str, suffix: str) -> str:
    content_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    tid = f"growthloopdemo{suffix}"

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
            "source_url": f"https://example.com/posts/growth-loop-demo-{suffix}",
            "tid": tid,
            "created_at": now,
            "updated_at": now,
        },
    )
    return tid


def _insert_booking(
    *,
    conn,
    creator_id: str,
    booking_link_id: str,
    tid: str,
    suffix: str,
    booked_at: datetime,
) -> str:
    booking_id = str(uuid.uuid4())

    conn.execute(
        text(
            "INSERT INTO bookings "
            "("
            "id, creator_id, booking_link_id, tid, calendly_booking_uuid, email, status, "
            "attribution_status, unattributed_reason, booked_at, canceled_at"
            ") VALUES ("
            ":id, :creator_id, :booking_link_id, :tid, :calendly_booking_uuid, :email, :status, "
            ":attribution_status, :unattributed_reason, :booked_at, :canceled_at"
            ")"
        ),
        {
            "id": booking_id,
            "creator_id": creator_id,
            "booking_link_id": booking_link_id,
            "tid": tid,
            "calendly_booking_uuid": f"BOOK_GROWTH_LOOP_DEMO_{suffix}".upper(),
            "email": "demo-student@example.com",
            "status": "created",
            "attribution_status": "attributed",
            "unattributed_reason": None,
            "booked_at": booked_at,
            "canceled_at": None,
        },
    )
    return booking_id


def _insert_paypal_paid_result(
    *,
    conn,
    creator_id: str,
    booking_id: str,
    merchant_id: str,
    tid: str,
    suffix: str,
    paid_at: datetime,
) -> None:
    invoice_id = str(uuid.uuid4())
    provider_order_id = f"ORDER_GROWTH_LOOP_DEMO_{suffix}".upper()
    provider_capture_id = f"CAPTURE_GROWTH_LOOP_DEMO_{suffix}".upper()

    conn.execute(
        text(
            "INSERT INTO invoices "
            "(id, creator_id, booking_id, tid, payment_provider, provider_account_id, provider_invoice_id, "
            "stripe_account_id, stripe_invoice_id, amount_cents, currency, status, issued_at, paid_at, voided_at) "
            "VALUES "
            "(:id, :creator_id, :booking_id, :tid, :payment_provider, :provider_account_id, :provider_invoice_id, "
            ":stripe_account_id, :stripe_invoice_id, :amount_cents, :currency, :status, :issued_at, :paid_at, :voided_at)"
        ),
        {
            "id": invoice_id,
            "creator_id": creator_id,
            "booking_id": booking_id,
            "tid": tid,
            "payment_provider": "paypal",
            "provider_account_id": merchant_id,
            "provider_invoice_id": provider_order_id,
            "stripe_account_id": None,
            "stripe_invoice_id": None,
            "amount_cents": 19500,
            "currency": "USD",
            "status": "paid",
            "issued_at": paid_at - timedelta(minutes=20),
            "paid_at": paid_at,
            "voided_at": None,
        },
    )
    conn.execute(
        text(
            "INSERT INTO invoice_payment_events "
            "(id, payment_provider, provider_event_id, provider_event_type, provider_account_id, provider_invoice_id, "
            "stripe_event_id, stripe_event_type, stripe_account_id, stripe_invoice_id, invoice_id, creator_id, booking_id, tid, "
            "status, unattributed_reason, paid_at, received_at, processed_at) "
            "VALUES "
            "(:id, :payment_provider, :provider_event_id, :provider_event_type, :provider_account_id, :provider_invoice_id, "
            ":stripe_event_id, :stripe_event_type, :stripe_account_id, :stripe_invoice_id, :invoice_id, :creator_id, :booking_id, :tid, "
            ":status, :unattributed_reason, :paid_at, :received_at, :processed_at)"
        ),
        {
            "id": str(uuid.uuid4()),
            "payment_provider": "paypal",
            "provider_event_id": provider_capture_id,
            "provider_event_type": "PAYMENT.CAPTURE.COMPLETED",
            "provider_account_id": merchant_id,
            "provider_invoice_id": provider_order_id,
            "stripe_event_id": None,
            "stripe_event_type": None,
            "stripe_account_id": None,
            "stripe_invoice_id": None,
            "invoice_id": invoice_id,
            "creator_id": creator_id,
            "booking_id": booking_id,
            "tid": tid,
            "status": "applied",
            "unattributed_reason": None,
            "paid_at": paid_at,
            "received_at": paid_at,
            "processed_at": paid_at,
        },
    )


def seed_growth_loop_demo(
    *,
    conn,
    base_url: str,
    creator_email: str | None = None,
) -> GrowthLoopDemoSeed:
    normalized_base_url = _normalize_base_url(base_url)
    suffix = uuid.uuid4().hex[:8]
    email = creator_email or f"growth-loop-demo-{suffix}@example.com"
    creator_id, user_id, merchant_id = _insert_creator_user(
        conn=conn,
        email=email,
        suffix=suffix,
    )
    booking_link_id = _insert_booking_link(
        conn=conn,
        creator_id=creator_id,
        suffix=suffix,
    )
    tid = _insert_content(
        conn=conn,
        creator_id=creator_id,
        booking_link_id=booking_link_id,
        suffix=suffix,
    )
    booked_at = datetime(2026, 5, 27, 12, 30, tzinfo=timezone.utc)
    booking_id = _insert_booking(
        conn=conn,
        creator_id=creator_id,
        booking_link_id=booking_link_id,
        tid=tid,
        suffix=suffix,
        booked_at=booked_at,
    )
    _insert_paypal_paid_result(
        conn=conn,
        creator_id=creator_id,
        booking_id=booking_id,
        merchant_id=merchant_id,
        tid=tid,
        suffix=suffix,
        paid_at=booked_at + timedelta(minutes=30),
    )

    login_token = _magic_link_token("growth-loop-demo")
    backup_login_token = _magic_link_token("growth-loop-demo-backup")
    _insert_magic_link_token(conn=conn, user_id=user_id, raw_token=login_token)
    _insert_magic_link_token(conn=conn, user_id=user_id, raw_token=backup_login_token)

    return GrowthLoopDemoSeed(
        creator_email=email,
        login_url=f"{normalized_base_url}/auth/magic-link/verify?token={login_token}",
        backup_login_url=f"{normalized_base_url}/auth/magic-link/verify?token={backup_login_token}",
        growth_loop_url=f"{normalized_base_url}/app/growth-loop",
        app_url=f"{normalized_base_url}/app",
        reports_url=f"{normalized_base_url}/app/reports",
        expected_stage="Paid Result Exists",
        expected_revenue="$195.00",
    )


def _print_seeded_demo(seed: GrowthLoopDemoSeed) -> None:
    print("STATE=paid-paypal-proof")
    print(f"CREATOR_EMAIL={seed.creator_email}")
    print(f"LOGIN_URL={seed.login_url}")
    print(f"BACKUP_LOGIN_URL={seed.backup_login_url}")
    print(f"GROWTH_LOOP_URL={seed.growth_loop_url}")
    print(f"APP_URL={seed.app_url}")
    print(f"REPORTS_URL={seed.reports_url}")
    print(f"EXPECTED_STAGE={seed.expected_stage}")
    print(f"EXPECTED_REVENUE={seed.expected_revenue}")
    print(
        "NEXT_STEP=Open LOGIN_URL in the browser, then open GROWTH_LOOP_URL with "
        "GROWTH_LOOP_AGENT_FEATURE_ENABLED=true."
    )


def main() -> int:
    args = _parser().parse_args()
    engine = create_engine(_database_url())
    base_url = _normalize_base_url(args.base_url)

    with engine.begin() as conn:
        seed = seed_growth_loop_demo(
            conn=conn,
            base_url=base_url,
            creator_email=args.creator_email,
        )

    _print_seeded_demo(seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
