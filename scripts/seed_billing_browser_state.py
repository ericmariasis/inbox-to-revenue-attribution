import argparse
import hashlib
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, text

from app.core.config import get_settings
from app.services.invoice_payment_events import UNATTRIBUTED_REASON_MISSING_TID


@dataclass(frozen=True)
class SeedOutput:
    creator_email: str
    login_token: str
    backup_login_token: str
    landing_path: str
    description: str


@dataclass(frozen=True)
class StateDefinition:
    description: str
    landing_path: str


STATE_DEFINITIONS: dict[str, StateDefinition] = {
    "pp11a-pending-choice": StateDefinition(
        description="First-time billing provider choice on /app and /app/account.",
        landing_path="/app",
    ),
    "pp11a-paypal-disconnected": StateDefinition(
        description="Disconnected PayPal creator state with Reconnect PayPal on /app.",
        landing_path="/app",
    ),
    "pp11b-stripe-clean-switch-start": StateDefinition(
        description="Clean Stripe-connected account page with Start PayPal switch.",
        landing_path="/app/account",
    ),
    "pp11b-paypal-switch-pending": StateDefinition(
        description="Pending PayPal switch with Resume, Restart, and Cancel actions.",
        landing_path="/app/account",
    ),
    "pp11b-paypal-switch-ready": StateDefinition(
        description="Ready PayPal switch with Switch to PayPal commit action.",
        landing_path="/app/account",
    ),
    "pp11b-switch-blocked-open-invoice": StateDefinition(
        description="Connected Stripe account with switching blocked by one open invoice.",
        landing_path="/app/account",
    ),
    "pp11c-paypal-active-not-ready": StateDefinition(
        description="Active PayPal account with actionable not-ready guidance.",
        landing_path="/app",
    ),
    "pp11c-paypal-active-blocked": StateDefinition(
        description="Active PayPal account with blocked fallback guidance.",
        landing_path="/app",
    ),
    "pp11c-paypal-switch-not-ready": StateDefinition(
        description="Pending PayPal switch target with actionable not-ready guidance.",
        landing_path="/app/account",
    ),
    "pp11c-paypal-switch-blocked": StateDefinition(
        description="Pending PayPal switch target with blocked fallback guidance.",
        landing_path="/app/account",
    ),
    "pp12-diagnostics": StateDefinition(
        description="Creator diagnostics with blocked billing and unmatched payment attention.",
        landing_path="/app/attention",
    ),
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Seed one local browser-login billing state and print the URLs for manual UI checks.",
    )
    parser.add_argument(
        "--state",
        choices=sorted(STATE_DEFINITIONS),
        help="Named billing browser state to seed.",
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="Browser base URL to print in the generated login and page links.",
    )
    parser.add_argument(
        "--creator-email",
        help="Optional fixed creator email. Defaults to a generated address.",
    )
    parser.add_argument(
        "--list-states",
        action="store_true",
        help="Print the available state names and descriptions, then exit.",
    )
    return parser


def _database_url() -> str:
    return (
        os.environ.get("TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or get_settings().database_url
    )


def _normalize_base_url(raw_value: str) -> str:
    value = str(raw_value).strip().rstrip("/")
    if not value.startswith(("http://", "https://")):
        raise SystemExit("error=--base-url must start with http:// or https://")
    return value


def _token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _magic_link_token(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _print_states() -> int:
    for state_name in sorted(STATE_DEFINITIONS):
        print(f"{state_name}: {STATE_DEFINITIONS[state_name].description}")
    return 0


def _insert_creator_user(
    *,
    conn,
    email: str,
    name: str,
    stripe_connect_status: str = "pending",
    stripe_account_id: str | None = None,
    stripe_connected_at: datetime | None = None,
    billing_provider: str = "stripe",
    billing_connect_status: str | None = None,
    billing_account_id: str | None = None,
    billing_provider_correlation_id: str | None = None,
    billing_connected_at: datetime | None = None,
) -> tuple[str, str]:
    creator_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    resolved_billing_connect_status = billing_connect_status or stripe_connect_status
    resolved_billing_account_id = (
        billing_account_id if billing_account_id is not None else stripe_account_id
    )
    resolved_billing_connected_at = (
        billing_connected_at if billing_connected_at is not None else stripe_connected_at
    )

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
            "name": name,
            "billing_provider": billing_provider,
            "billing_connect_status": resolved_billing_connect_status,
            "billing_account_id": resolved_billing_account_id,
            "billing_provider_correlation_id": billing_provider_correlation_id,
            "billing_connected_at": resolved_billing_connected_at,
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
    now = datetime.now(timezone.utc)
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
            "created_at": now,
            "updated_at": now,
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
            "email": "manual-booking@example.com",
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
    issued_at: datetime,
    status: str,
    paid_at: datetime | None = None,
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
            "status": status,
            "issued_at": issued_at,
            "paid_at": paid_at,
            "voided_at": None,
        },
    )
    return invoice_id


def _insert_billing_provider_switch_attempt(
    *,
    conn,
    creator_id: str,
    source_billing_provider: str,
    target_billing_provider: str,
    target_billing_connect_status: str = "pending",
    target_billing_account_id: str | None = None,
    target_billing_provider_correlation_id: str | None = None,
    target_billing_connected_at: datetime | None = None,
) -> str:
    attempt_id = str(uuid.uuid4())
    conn.execute(
        text(
            "INSERT INTO billing_provider_switch_attempts "
            "("
            "id, creator_id, source_billing_provider, target_billing_provider, "
            "target_billing_connect_status, target_billing_account_id, "
            "target_billing_provider_correlation_id, target_billing_connected_at"
            ") VALUES ("
            ":id, :creator_id, :source_billing_provider, :target_billing_provider, "
            ":target_billing_connect_status, :target_billing_account_id, "
            ":target_billing_provider_correlation_id, :target_billing_connected_at"
            ")"
        ),
        {
            "id": attempt_id,
            "creator_id": creator_id,
            "source_billing_provider": source_billing_provider,
            "target_billing_provider": target_billing_provider,
            "target_billing_connect_status": target_billing_connect_status,
            "target_billing_account_id": target_billing_account_id,
            "target_billing_provider_correlation_id": target_billing_provider_correlation_id,
            "target_billing_connected_at": target_billing_connected_at,
        },
    )
    return attempt_id


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


def _seed_state(
    *,
    conn,
    state: str,
    creator_email: str,
) -> SeedOutput:
    suffix = uuid.uuid4().hex[:8]
    state_definition = STATE_DEFINITIONS[state]
    login_token = _magic_link_token(state)
    backup_login_token = _magic_link_token(f"{state}-backup")
    connected_at = datetime(2026, 3, 21, 12, 0, tzinfo=timezone.utc)

    if state == "pp11a-pending-choice":
        creator_id, user_id = _insert_creator_user(
            conn=conn,
            email=creator_email,
            name="PP11A Pending Choice Creator",
            stripe_connect_status="pending",
        )
    elif state == "pp11a-paypal-disconnected":
        creator_id, user_id = _insert_creator_user(
            conn=conn,
            email=creator_email,
            name="PP11A Disconnected PayPal Creator",
            stripe_connect_status="pending",
            billing_provider="paypal",
            billing_connect_status="disconnected",
            billing_account_id=f"merchant_manual_paypal_disconnected_{suffix}",
            billing_provider_correlation_id=f"tracking_manual_paypal_disconnected_{suffix}",
        )
    elif state == "pp11b-stripe-clean-switch-start":
        creator_id, user_id = _insert_creator_user(
            conn=conn,
            email=creator_email,
            name="PP11B Clean Stripe Switch Creator",
            stripe_connect_status="connected",
            stripe_account_id=f"acct_manual_stripe_clean_{suffix}",
            stripe_connected_at=connected_at,
        )
        _insert_booking_link(
            conn=conn,
            creator_id=creator_id,
            name="PP11B Clean Switch Call",
            calendly_url=f"https://calendly.com/example/pp11b-clean-switch-{suffix}",
            billing_amount_cents=19500,
            billing_currency="USD",
        )
    elif state == "pp11b-paypal-switch-pending":
        creator_id, user_id = _insert_creator_user(
            conn=conn,
            email=creator_email,
            name="PP11B Pending PayPal Switch Creator",
            stripe_connect_status="connected",
            stripe_account_id=f"acct_manual_switch_pending_{suffix}",
            stripe_connected_at=connected_at,
        )
        _insert_booking_link(
            conn=conn,
            creator_id=creator_id,
            name="PP11B Pending Switch Call",
            calendly_url=f"https://calendly.com/example/pp11b-switch-pending-{suffix}",
            billing_amount_cents=19500,
            billing_currency="USD",
        )
        _insert_billing_provider_switch_attempt(
            conn=conn,
            creator_id=creator_id,
            source_billing_provider="stripe",
            target_billing_provider="paypal",
            target_billing_connect_status="pending",
            target_billing_provider_correlation_id=f"tracking_manual_switch_pending_{suffix}",
        )
    elif state == "pp11b-paypal-switch-ready":
        creator_id, user_id = _insert_creator_user(
            conn=conn,
            email=creator_email,
            name="PP11B Ready PayPal Switch Creator",
            stripe_connect_status="connected",
            stripe_account_id=f"acct_manual_switch_ready_{suffix}",
            stripe_connected_at=connected_at,
        )
        _insert_booking_link(
            conn=conn,
            creator_id=creator_id,
            name="PP11B Ready Switch Call",
            calendly_url=f"https://calendly.com/example/pp11b-switch-ready-{suffix}",
            billing_amount_cents=19500,
            billing_currency="USD",
        )
        _insert_billing_provider_switch_attempt(
            conn=conn,
            creator_id=creator_id,
            source_billing_provider="stripe",
            target_billing_provider="paypal",
            target_billing_connect_status="connected",
            target_billing_account_id=f"merchant_manual_switch_ready_{suffix}",
            target_billing_provider_correlation_id=f"tracking_manual_switch_ready_{suffix}",
            target_billing_connected_at=connected_at + timedelta(minutes=15),
        )
    elif state == "pp11b-switch-blocked-open-invoice":
        creator_id, user_id = _insert_creator_user(
            conn=conn,
            email=creator_email,
            name="PP11B Open Invoice Block Creator",
            stripe_connect_status="connected",
            stripe_account_id=f"acct_manual_switch_blocked_{suffix}",
            stripe_connected_at=connected_at,
        )
        booking_link_id = _insert_booking_link(
            conn=conn,
            creator_id=creator_id,
            name="PP11B Blocked Switch Call",
            calendly_url=f"https://calendly.com/example/pp11b-switch-blocked-{suffix}",
            billing_amount_cents=17500,
            billing_currency="USD",
        )
        tid = f"pp11bblocked{suffix}"
        _insert_content(
            conn=conn,
            creator_id=creator_id,
            booking_link_id=booking_link_id,
            source_url=f"https://example.com/posts/pp11b-switch-blocked-{suffix}",
            tid=tid,
        )
        booking_id = _insert_booking(
            conn=conn,
            creator_id=creator_id,
            booking_link_id=booking_link_id,
            tid=tid,
            calendly_booking_uuid=f"BOOK_PP11B_BLOCKED_{suffix}",
            booked_at=datetime(2026, 3, 21, 10, 0, tzinfo=timezone.utc),
        )
        _insert_invoice(
            conn=conn,
            creator_id=creator_id,
            booking_id=booking_id,
            tid=tid,
            stripe_account_id=f"acct_manual_switch_blocked_{suffix}",
            stripe_invoice_id=f"in_manual_switch_blocked_{suffix}",
            amount_cents=17500,
            issued_at=datetime(2026, 3, 21, 11, 0, tzinfo=timezone.utc),
            status="open",
        )
    elif state == "pp11c-paypal-active-not-ready":
        creator_id, user_id = _insert_creator_user(
            conn=conn,
            email=creator_email,
            name="PP11C Active PayPal Not Ready Creator",
            stripe_connect_status="pending",
            billing_provider="paypal",
            billing_connect_status="connected",
            billing_account_id=f"merchant_manual_paypal_not_ready_{suffix}",
            billing_provider_correlation_id=f"tracking_manual_paypal_not_ready_{suffix}",
            billing_connected_at=connected_at,
        )
        _insert_booking_link(
            conn=conn,
            creator_id=creator_id,
            name="PP11C Not Ready PayPal Call",
            calendly_url=f"https://calendly.com/example/pp11c-paypal-not-ready-{suffix}",
            billing_amount_cents=19500,
            billing_currency="USD",
        )
    elif state == "pp11c-paypal-active-blocked":
        creator_id, user_id = _insert_creator_user(
            conn=conn,
            email=creator_email,
            name="PP11C Active PayPal Blocked Creator",
            stripe_connect_status="pending",
            billing_provider="paypal",
            billing_connect_status="connected",
            billing_account_id=f"merchant_manual_paypal_blocked_{suffix}",
            billing_provider_correlation_id=f"tracking_manual_paypal_blocked_{suffix}",
            billing_connected_at=connected_at,
        )
        _insert_booking_link(
            conn=conn,
            creator_id=creator_id,
            name="PP11C Blocked PayPal Call",
            calendly_url=f"https://calendly.com/example/pp11c-paypal-blocked-{suffix}",
            billing_amount_cents=19500,
            billing_currency="USD",
        )
    elif state == "pp11c-paypal-switch-not-ready":
        creator_id, user_id = _insert_creator_user(
            conn=conn,
            email=creator_email,
            name="PP11C Pending PayPal Not Ready Switch Creator",
            stripe_connect_status="connected",
            stripe_account_id=f"acct_manual_switch_not_ready_{suffix}",
            stripe_connected_at=connected_at,
        )
        _insert_booking_link(
            conn=conn,
            creator_id=creator_id,
            name="PP11C Switch Not Ready Call",
            calendly_url=f"https://calendly.com/example/pp11c-switch-not-ready-{suffix}",
            billing_amount_cents=19500,
            billing_currency="USD",
        )
        _insert_billing_provider_switch_attempt(
            conn=conn,
            creator_id=creator_id,
            source_billing_provider="stripe",
            target_billing_provider="paypal",
            target_billing_connect_status="connected",
            target_billing_account_id=f"merchant_manual_switch_not_ready_{suffix}",
            target_billing_provider_correlation_id=f"tracking_manual_switch_not_ready_{suffix}",
            target_billing_connected_at=connected_at + timedelta(minutes=15),
        )
    elif state == "pp11c-paypal-switch-blocked":
        creator_id, user_id = _insert_creator_user(
            conn=conn,
            email=creator_email,
            name="PP11C Pending PayPal Blocked Switch Creator",
            stripe_connect_status="connected",
            stripe_account_id=f"acct_manual_switch_blocked_pending_{suffix}",
            stripe_connected_at=connected_at,
        )
        _insert_booking_link(
            conn=conn,
            creator_id=creator_id,
            name="PP11C Switch Blocked Call",
            calendly_url=f"https://calendly.com/example/pp11c-switch-blocked-{suffix}",
            billing_amount_cents=19500,
            billing_currency="USD",
        )
        _insert_billing_provider_switch_attempt(
            conn=conn,
            creator_id=creator_id,
            source_billing_provider="stripe",
            target_billing_provider="paypal",
            target_billing_connect_status="connected",
            target_billing_account_id=f"merchant_manual_switch_blocked_{suffix}",
            target_billing_provider_correlation_id=f"tracking_manual_switch_blocked_{suffix}",
            target_billing_connected_at=connected_at + timedelta(minutes=20),
        )
    elif state == "pp12-diagnostics":
        creator_id, user_id = _insert_creator_user(
            conn=conn,
            email=creator_email,
            name="PP12 Diagnostics Creator",
            stripe_connect_status="connected",
            stripe_account_id=f"acct_manual_diagnostics_{suffix}",
            stripe_connected_at=connected_at,
        )
        booking_link_id = _insert_booking_link(
            conn=conn,
            creator_id=creator_id,
            name="PP12 Diagnostics Strategy",
            calendly_url=f"https://calendly.com/example/pp12-diagnostics-{suffix}",
            billing_amount_cents=19500,
            billing_currency="USD",
        )
        tid = f"pp12diag{suffix}"
        _insert_content(
            conn=conn,
            creator_id=creator_id,
            booking_link_id=booking_link_id,
            source_url=f"https://example.com/posts/pp12-diagnostics-{suffix}",
            tid=tid,
        )
        booking_id = _insert_booking(
            conn=conn,
            creator_id=creator_id,
            booking_link_id=booking_link_id,
            tid=tid,
            calendly_booking_uuid=f"BOOK_PP12_DIAG_{suffix}",
            booked_at=datetime(2026, 3, 21, 9, 30, tzinfo=timezone.utc),
        )
        _insert_blocked_billing_case(
            conn=conn,
            creator_id=creator_id,
            booking_id=booking_id,
            tid=tid,
            calendly_booking_uuid=f"BOOK_PP12_DIAG_{suffix}",
            stripe_account_id=f"acct_manual_diagnostics_{suffix}",
            first_blocked_at=datetime(2026, 3, 21, 9, 35, tzinfo=timezone.utc),
        )
        _insert_unmatched_payment_event(
            conn=conn,
            creator_id=creator_id,
            stripe_account_id=f"acct_manual_diagnostics_{suffix}",
            stripe_event_id=f"evt_manual_diag_{suffix}",
            stripe_invoice_id=f"in_manual_diag_{suffix}",
            paid_at=datetime(2026, 3, 21, 9, 45, tzinfo=timezone.utc),
        )
    else:
        raise SystemExit(f"error=unknown state {state}")

    _insert_magic_link_token(conn=conn, user_id=user_id, raw_token=login_token)
    _insert_magic_link_token(conn=conn, user_id=user_id, raw_token=backup_login_token)

    return SeedOutput(
        creator_email=creator_email,
        login_token=login_token,
        backup_login_token=backup_login_token,
        landing_path=state_definition.landing_path,
        description=state_definition.description,
    )


def main() -> int:
    args = _parser().parse_args()
    if args.list_states:
        return _print_states()
    if not args.state:
        raise SystemExit("error=--state is required unless --list-states is used")

    base_url = _normalize_base_url(args.base_url)
    engine = create_engine(_database_url())
    creator_email = args.creator_email or f"{args.state}.{uuid.uuid4().hex[:8]}@example.com"

    with engine.begin() as conn:
        seeded = _seed_state(
            conn=conn,
            state=args.state,
            creator_email=creator_email,
        )

    print(f"STATE={args.state}")
    print(f"DESCRIPTION={seeded.description}")
    print(f"CREATOR_EMAIL={seeded.creator_email}")
    print(f"LOGIN_URL={base_url}/auth/magic-link/verify?token={seeded.login_token}")
    print(f"BACKUP_LOGIN_URL={base_url}/auth/magic-link/verify?token={seeded.backup_login_token}")
    print(f"LANDING_URL={base_url}{seeded.landing_path}")
    print(f"SETUP_URL={base_url}/app")
    print(f"ACCOUNT_URL={base_url}/app/account")
    print(f"ATTENTION_URL={base_url}/app/attention")
    print(f"HEALTH_URL={base_url}/app/health")
    print("NEXT_STEP=Open LOGIN_URL in the browser, let it set the session cookie, then open LANDING_URL.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
