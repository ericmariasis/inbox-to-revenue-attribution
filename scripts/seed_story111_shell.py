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
from app.services.invoice_payment_events import UNATTRIBUTED_REASON_MISSING_TID


STATE_ORDER = (
    "no-provider",
    "connected-not-billable",
    "billable-no-content",
    "tracked-no-bookings",
    "bookings-no-paid",
    "first-paid",
    "diagnostic-review",
)


@dataclass(frozen=True)
class StateDefinition:
    title: str
    expectation: str
    reports_relevant: bool = False
    attention_relevant: bool = False


STATE_DEFINITIONS: dict[str, StateDefinition] = {
    "no-provider": StateDefinition(
        title="Choose billing provider",
        expectation="Primary CTA starts billing setup; proof says no provider is connected yet.",
    ),
    "connected-not-billable": StateDefinition(
        title="Connected, but not billable now",
        expectation="Primary CTA points to booking links; proof says the connection exists and defaults are missing.",
    ),
    "billable-no-content": StateDefinition(
        title="Billable now",
        expectation="Primary CTA points to content creation; proof says at least one saved link is billable.",
    ),
    "tracked-no-bookings": StateDefinition(
        title="Ready to track",
        expectation="Primary CTA points to content/tracked-link sharing; proof says no booking yet is not a setup failure.",
        reports_relevant=True,
    ),
    "bookings-no-paid": StateDefinition(
        title="Bookings are landing; paid proof is next",
        expectation="Primary CTA points to reports; proof says tracked booking activity is already captured.",
        reports_relevant=True,
    ),
    "first-paid": StateDefinition(
        title="First paid result is already landing",
        expectation="Primary CTA points to reports; proof says a canonical paid result is already counted.",
        reports_relevant=True,
    ),
    "diagnostic-review": StateDefinition(
        title="Bookings are landing; paid proof is next",
        expectation="Shell stays in the bookings-without-paid milestone, but a secondary diagnostic summary points to Attention for blocked billing and unmatched payment review.",
        reports_relevant=True,
        attention_relevant=True,
    ),
}


@dataclass(frozen=True)
class SeededState:
    state: str
    creator_email: str
    login_url: str
    backup_login_url: str
    app_url: str
    reports_url: str | None
    attention_url: str | None
    expected_title: str
    expectation: str


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Seed browser-login states for Story 111 / Story 113 shell and attention verification.",
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="Browser base URL used in printed login and page links.",
    )
    parser.add_argument(
        "--state",
        choices=STATE_ORDER,
        action="append",
        help="Seed only this state. Can be repeated. Defaults to all Story 111 states.",
    )
    return parser


def _database_url() -> str:
    database_url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if database_url:
        return database_url
    return get_settings().database_url


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


def _insert_creator_user(
    *,
    conn,
    email: str,
    name: str,
    stripe_connect_status: str = "pending",
    stripe_account_id: str | None = None,
    stripe_connected_at: datetime | None = None,
) -> tuple[str, str]:
    creator_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())

    conn.execute(
        text(
            "INSERT INTO creators ("
            "id, name, billing_provider, billing_connect_status, billing_account_id, billing_connected_at, "
            "stripe_connect_status, stripe_account_id, stripe_connected_at"
            ") VALUES ("
            ":id, :name, :billing_provider, :billing_connect_status, :billing_account_id, :billing_connected_at, "
            ":stripe_connect_status, :stripe_account_id, :stripe_connected_at"
            ")"
        ),
        {
            "id": creator_id,
            "name": name,
            "billing_provider": "stripe",
            "billing_connect_status": stripe_connect_status,
            "billing_account_id": stripe_account_id,
            "billing_connected_at": stripe_connected_at,
            "stripe_connect_status": stripe_connect_status,
            "stripe_account_id": stripe_account_id,
            "stripe_connected_at": stripe_connected_at,
        },
    )
    conn.execute(
        text(
            "INSERT INTO auth_users (id, creator_id, email) "
            "VALUES (:id, :creator_id, :email)"
        ),
        {"id": user_id, "creator_id": creator_id, "email": email},
    )
    return creator_id, user_id


def _insert_booking_link(
    *,
    conn,
    creator_id: str,
    name: str,
    calendly_url: str,
    billing_amount_cents: int | None = None,
    billing_currency: str | None = None,
) -> str:
    booking_link_id = str(uuid.uuid4())
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
            "name": name,
            "provider": "calendly",
            "destination_url": calendly_url,
            "calendly_url": calendly_url,
            "billing_amount_cents": billing_amount_cents,
            "billing_currency": billing_currency,
        },
    )
    return booking_link_id


def _insert_content(
    *,
    conn,
    creator_id: str,
    booking_link_id: str,
    source_url: str,
    tid: str,
) -> str:
    content_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc)
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
            "source_url": source_url,
            "tid": tid,
            "created_at": created_at,
            "updated_at": created_at,
        },
    )
    return content_id


def _insert_booking(
    *,
    conn,
    creator_id: str,
    booking_link_id: str,
    tid: str,
    calendly_booking_uuid: str,
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
            "calendly_booking_uuid": calendly_booking_uuid,
            "email": "story111-booked@example.com",
            "status": "created",
            "attribution_status": "attributed",
            "unattributed_reason": None,
            "booked_at": booked_at,
            "canceled_at": None,
        },
    )
    return booking_id


def _insert_invoice(
    *,
    conn,
    creator_id: str,
    booking_id: str,
    tid: str,
    stripe_account_id: str,
    stripe_invoice_id: str,
    amount_cents: int,
    paid_at: datetime,
) -> str:
    invoice_id = str(uuid.uuid4())
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
            "payment_provider": "stripe",
            "provider_account_id": stripe_account_id,
            "provider_invoice_id": stripe_invoice_id,
            "stripe_account_id": stripe_account_id,
            "stripe_invoice_id": stripe_invoice_id,
            "amount_cents": amount_cents,
            "currency": "USD",
            "status": "paid",
            "issued_at": paid_at - timedelta(minutes=20),
            "paid_at": paid_at,
            "voided_at": None,
        },
    )
    return invoice_id


def _insert_blocked_billing_case(
    *,
    conn,
    creator_id: str,
    booking_id: str,
    tid: str,
    calendly_booking_uuid: str,
    stripe_account_id: str,
    first_blocked_at: datetime,
) -> str:
    case_id = str(uuid.uuid4())
    conn.execute(
        text(
            "INSERT INTO blocked_billing_cases "
            "(id, creator_id, booking_id, invoice_id, tid, provider, provider_booking_id, calendly_booking_uuid, stripe_account_id, "
            "frozen_amount_cents, frozen_currency, status, reason_code, provider_operation, provider_http_status, provider_error_code, "
            "first_blocked_at, last_blocked_at, last_retry_at, resolved_at, resolution_code) "
            "VALUES "
            "(:id, :creator_id, :booking_id, :invoice_id, :tid, :provider, :provider_booking_id, :calendly_booking_uuid, :stripe_account_id, "
            ":frozen_amount_cents, :frozen_currency, :status, :reason_code, :provider_operation, :provider_http_status, :provider_error_code, "
            ":first_blocked_at, :last_blocked_at, :last_retry_at, :resolved_at, :resolution_code)"
        ),
        {
            "id": case_id,
            "creator_id": creator_id,
            "booking_id": booking_id,
            "invoice_id": None,
            "tid": tid,
            "provider": "calendly",
            "provider_booking_id": calendly_booking_uuid,
            "calendly_booking_uuid": calendly_booking_uuid,
            "stripe_account_id": stripe_account_id,
            "frozen_amount_cents": 19500,
            "frozen_currency": "USD",
            "status": "open",
            "reason_code": "creator_not_billable",
            "provider_operation": None,
            "provider_http_status": None,
            "provider_error_code": None,
            "first_blocked_at": first_blocked_at,
            "last_blocked_at": first_blocked_at,
            "last_retry_at": None,
            "resolved_at": None,
            "resolution_code": None,
        },
    )
    return case_id


def _insert_unmatched_payment_event(
    *,
    conn,
    creator_id: str,
    stripe_account_id: str,
    stripe_event_id: str,
    stripe_invoice_id: str,
    paid_at: datetime,
) -> str:
    payment_event_id = str(uuid.uuid4())
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
            "id": payment_event_id,
            "payment_provider": "stripe",
            "provider_event_id": stripe_event_id,
            "provider_event_type": "invoice.paid",
            "provider_account_id": stripe_account_id,
            "provider_invoice_id": stripe_invoice_id,
            "stripe_event_id": stripe_event_id,
            "stripe_event_type": "invoice.paid",
            "stripe_account_id": stripe_account_id,
            "stripe_invoice_id": stripe_invoice_id,
            "invoice_id": None,
            "creator_id": creator_id,
            "booking_id": None,
            "tid": None,
            "status": "unmatched",
            "unattributed_reason": UNATTRIBUTED_REASON_MISSING_TID,
            "paid_at": paid_at,
            "received_at": paid_at,
            "processed_at": None,
        },
    )
    return payment_event_id


def _seed_state(*, conn, state: str, base_url: str) -> SeededState:
    suffix = uuid.uuid4().hex[:8]
    normalized_state = state.replace("-", "_")
    email = f"story111_{normalized_state}_{suffix}@example.com"
    definition = STATE_DEFINITIONS[state]
    connected_at = datetime(2026, 4, 9, 12, 0, tzinfo=timezone.utc)
    stripe_account_id = f"acct_story111_{normalized_state}_{suffix}"

    creator_id, user_id = _insert_creator_user(
        conn=conn,
        email=email,
        name=f"Story 111 {definition.title}",
        stripe_connect_status="pending" if state == "no-provider" else "connected",
        stripe_account_id=None if state == "no-provider" else stripe_account_id,
        stripe_connected_at=None if state == "no-provider" else connected_at,
    )

    booking_link_id = None
    tid = f"story111{normalized_state}{suffix}"
    if state in {
        "connected-not-billable",
        "billable-no-content",
        "tracked-no-bookings",
        "bookings-no-paid",
        "first-paid",
        "diagnostic-review",
    }:
        booking_link_id = _insert_booking_link(
            conn=conn,
            creator_id=creator_id,
            name=f"Story 111 {definition.title} Call",
            calendly_url=f"https://calendly.com/example/story111-{state}-{suffix}",
            billing_amount_cents=None if state == "connected-not-billable" else 19500,
            billing_currency=None if state == "connected-not-billable" else "USD",
        )

    if state in {"tracked-no-bookings", "bookings-no-paid", "first-paid", "diagnostic-review"}:
        if booking_link_id is None:
            raise AssertionError("booking_link_id should be available for tracked states")
        _insert_content(
            conn=conn,
            creator_id=creator_id,
            booking_link_id=booking_link_id,
            source_url=f"https://example.com/posts/story111-{state}",
            tid=tid,
        )

    booking_id = None
    if state in {"bookings-no-paid", "first-paid", "diagnostic-review"}:
        if booking_link_id is None:
            raise AssertionError("booking_link_id should be available for booking states")
        booking_id = _insert_booking(
            conn=conn,
            creator_id=creator_id,
            booking_link_id=booking_link_id,
            tid=tid,
            calendly_booking_uuid=f"BOOK_STORY111_{normalized_state}_{suffix}".upper(),
            booked_at=datetime(2026, 4, 9, 13, 0, tzinfo=timezone.utc),
        )

    if state == "first-paid":
        if booking_id is None:
            raise AssertionError("booking_id should be available for first-paid")
        _insert_invoice(
            conn=conn,
            creator_id=creator_id,
            booking_id=booking_id,
            tid=tid,
            stripe_account_id=stripe_account_id,
            stripe_invoice_id=f"in_story111_{normalized_state}_{suffix}",
            amount_cents=19500,
            paid_at=datetime(2026, 4, 9, 13, 30, tzinfo=timezone.utc),
        )

    if state == "diagnostic-review":
        if booking_id is None:
            raise AssertionError("booking_id should be available for diagnostic-review")
        _insert_blocked_billing_case(
            conn=conn,
            creator_id=creator_id,
            booking_id=booking_id,
            tid=tid,
            calendly_booking_uuid=f"BOOK_STORY111_{normalized_state}_{suffix}".upper(),
            stripe_account_id=stripe_account_id,
            first_blocked_at=datetime(2026, 4, 9, 13, 15, tzinfo=timezone.utc),
        )
        _insert_unmatched_payment_event(
            conn=conn,
            creator_id=creator_id,
            stripe_account_id=stripe_account_id,
            stripe_event_id=f"evt_story111_{normalized_state}_{suffix}",
            stripe_invoice_id=f"in_story111_{normalized_state}_{suffix}",
            paid_at=datetime(2026, 4, 9, 13, 20, tzinfo=timezone.utc),
        )

    login_token = _magic_link_token(f"story111-{state}")
    backup_login_token = _magic_link_token(f"story111-{state}-backup")
    _insert_magic_link_token(conn=conn, user_id=user_id, raw_token=login_token)
    _insert_magic_link_token(conn=conn, user_id=user_id, raw_token=backup_login_token)

    return SeededState(
        state=state,
        creator_email=email,
        login_url=f"{base_url}/auth/magic-link/verify?token={login_token}",
        backup_login_url=f"{base_url}/auth/magic-link/verify?token={backup_login_token}",
        app_url=f"{base_url}/app",
        reports_url=f"{base_url}/app/reports" if definition.reports_relevant else None,
        attention_url=f"{base_url}/app/attention" if definition.attention_relevant else None,
        expected_title=definition.title,
        expectation=definition.expectation,
    )


def _print_seeded_state(seeded: SeededState) -> None:
    print(f"STATE={seeded.state}")
    print(f"CREATOR_EMAIL={seeded.creator_email}")
    print(f"LOGIN_URL={seeded.login_url}")
    print(f"BACKUP_LOGIN_URL={seeded.backup_login_url}")
    print(f"APP_URL={seeded.app_url}")
    if seeded.reports_url is not None:
        print(f"REPORTS_URL={seeded.reports_url}")
    if seeded.attention_url is not None:
        print(f"ATTENTION_URL={seeded.attention_url}")
    print(f"EXPECTED_TITLE={seeded.expected_title}")
    print(f"EXPECTATION={seeded.expectation}")
    print("---")


def main() -> None:
    args = _parser().parse_args()
    base_url = _normalize_base_url(args.base_url)
    states = tuple(args.state or STATE_ORDER)
    engine = create_engine(_database_url())

    with engine.begin() as conn:
        seeded_states = [
            _seed_state(conn=conn, state=state, base_url=base_url)
            for state in states
        ]

    for seeded in seeded_states:
        _print_seeded_state(seeded)


if __name__ == "__main__":
    main()
