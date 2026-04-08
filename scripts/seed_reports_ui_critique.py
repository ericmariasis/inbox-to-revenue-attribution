import argparse
import hashlib
import os
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import create_engine, text

from app.core.config import get_settings
from app.services.blocked_billing import BLOCKED_BILLING_REASON_CREATOR_NOT_BILLABLE
from app.services.invoice_payment_events import (
    UNATTRIBUTED_REASON_MISSING_TID,
    UNATTRIBUTED_REASON_UNKNOWN_STRIPE_INVOICE_ID,
)


FILTER_DATE = date(2026, 3, 8)


@dataclass(frozen=True)
class SeedOutput:
    creator_email: str
    login_token: str
    backup_login_token: str
    reports_url: str
    filtered_reports_url: str
    paid_tid: str
    waiting_tid: str
    blocked_tid: str
    empty_tid: str
    long_source_url: str


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Seed one dedicated /app/reports browser state for GPT Pro UI critique "
            "with paid, waiting, blocked, and no-bookings rows plus diagnostic backlog."
        ),
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="Browser base URL to print in the generated login and reports links.",
    )
    parser.add_argument(
        "--creator-email",
        help="Optional fixed creator email. Defaults to a generated address.",
    )
    parser.add_argument(
        "--creator-name",
        default="Reports UI Critique Creator",
        help="Creator display name to seed.",
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


def _insert_creator_user(
    *,
    conn,
    email: str,
    name: str,
    stripe_account_id: str,
    connected_at: datetime,
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
            "billing_connect_status": "connected",
            "billing_account_id": stripe_account_id,
            "billing_connected_at": connected_at,
            "stripe_connect_status": "connected",
            "stripe_account_id": stripe_account_id,
            "stripe_connected_at": connected_at,
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
    destination_url: str,
    billing_amount_cents: int,
    billing_currency: str,
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
            "destination_url": destination_url,
            "calendly_url": destination_url,
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
    created_at: datetime,
) -> str:
    content_id = str(uuid.uuid4())
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
    email: str,
    frozen_amount_cents: int,
    frozen_currency: str,
) -> str:
    booking_id = str(uuid.uuid4())
    conn.execute(
        text(
            "INSERT INTO bookings "
            "("
            "id, creator_id, booking_link_id, tid, provider, provider_booking_id, calendly_booking_uuid, email, status, "
            "attribution_status, unattributed_reason, frozen_billing_amount_cents, frozen_billing_currency, booked_at, canceled_at"
            ") VALUES ("
            ":id, :creator_id, :booking_link_id, :tid, :provider, :provider_booking_id, :calendly_booking_uuid, :email, :status, "
            ":attribution_status, :unattributed_reason, :frozen_billing_amount_cents, :frozen_billing_currency, :booked_at, :canceled_at"
            ")"
        ),
        {
            "id": booking_id,
            "creator_id": creator_id,
            "booking_link_id": booking_link_id,
            "tid": tid,
            "provider": "calendly",
            "provider_booking_id": calendly_booking_uuid,
            "calendly_booking_uuid": calendly_booking_uuid,
            "email": email,
            "status": "created",
            "attribution_status": "attributed",
            "unattributed_reason": None,
            "frozen_billing_amount_cents": frozen_amount_cents,
            "frozen_billing_currency": frozen_currency,
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
            "issued_at": issued_at,
            "paid_at": paid_at,
            "voided_at": None,
        },
    )
    return invoice_id


def _insert_matched_payment_event(
    *,
    conn,
    creator_id: str,
    booking_id: str,
    tid: str,
    invoice_id: str,
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
    return payment_event_id


def _insert_unmatched_payment_event(
    *,
    conn,
    creator_id: str,
    stripe_account_id: str,
    stripe_event_id: str,
    stripe_invoice_id: str,
    reason: str,
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
            "unattributed_reason": reason,
            "paid_at": paid_at,
            "received_at": paid_at,
            "processed_at": None,
        },
    )
    return payment_event_id


def _insert_blocked_billing_case(
    *,
    conn,
    creator_id: str,
    booking_id: str,
    tid: str,
    calendly_booking_uuid: str,
    stripe_account_id: str,
    blocked_at: datetime,
    frozen_amount_cents: int,
    frozen_currency: str,
) -> str:
    case_id = str(uuid.uuid4())
    conn.execute(
        text(
            "INSERT INTO blocked_billing_cases "
            "(id, creator_id, booking_id, invoice_id, tid, provider, provider_booking_id, calendly_booking_uuid, "
            "payment_provider, provider_account_id, stripe_account_id, frozen_amount_cents, frozen_currency, status, reason_code, "
            "provider_operation, provider_http_status, provider_error_code, first_blocked_at, last_blocked_at, "
            "last_retry_at, resolved_at, resolution_code) "
            "VALUES "
            "(:id, :creator_id, :booking_id, :invoice_id, :tid, :provider, :provider_booking_id, :calendly_booking_uuid, "
            ":payment_provider, :provider_account_id, :stripe_account_id, :frozen_amount_cents, :frozen_currency, :status, :reason_code, "
            ":provider_operation, :provider_http_status, :provider_error_code, :first_blocked_at, :last_blocked_at, "
            ":last_retry_at, :resolved_at, :resolution_code)"
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
            "payment_provider": "stripe",
            "provider_account_id": stripe_account_id,
            "stripe_account_id": stripe_account_id,
            "frozen_amount_cents": frozen_amount_cents,
            "frozen_currency": frozen_currency,
            "status": "open",
            "reason_code": BLOCKED_BILLING_REASON_CREATOR_NOT_BILLABLE,
            "provider_operation": None,
            "provider_http_status": None,
            "provider_error_code": None,
            "first_blocked_at": blocked_at,
            "last_blocked_at": blocked_at,
            "last_retry_at": None,
            "resolved_at": None,
            "resolution_code": None,
        },
    )
    return case_id


def _seed_reports_ui_critique(
    *,
    conn,
    base_url: str,
    creator_email: str,
    creator_name: str,
) -> SeedOutput:
    suffix = uuid.uuid4().hex[:8]
    stripe_account_id = f"acct_reports_ui_critique_{suffix}"
    connected_at = datetime(2026, 3, 7, 18, 0, tzinfo=timezone.utc)
    creator_id, user_id = _insert_creator_user(
        conn=conn,
        email=creator_email,
        name=creator_name,
        stripe_account_id=stripe_account_id,
        connected_at=connected_at,
    )

    login_token = _magic_link_token("reports-ui-critique")
    backup_login_token = _magic_link_token("reports-ui-critique-backup")
    _insert_magic_link_token(conn=conn, user_id=user_id, raw_token=login_token)
    _insert_magic_link_token(conn=conn, user_id=user_id, raw_token=backup_login_token)

    booking_link_id = _insert_booking_link(
        conn=conn,
        creator_id=creator_id,
        name="Strategy Session",
        destination_url=f"https://calendly.com/example/reports-ui-critique-{suffix}",
        billing_amount_cents=19500,
        billing_currency="USD",
    )

    paid_tid = f"reportscritiquepaid{suffix}"
    waiting_tid = f"reportscritiquewait{suffix}"
    blocked_tid = f"reportscritiqueblocked{suffix}"
    empty_tid = f"reportscritiqueempty{suffix}"

    long_source_url = (
        "https://example.com/insights/"
        "pricing-frameworks-and-client-conversion-patterns-for-independent-service-providers-"
        f"with-a-long-tail-slug-{suffix}"
    )

    _insert_content(
        conn=conn,
        creator_id=creator_id,
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/reports-ui-critique-paid",
        tid=paid_tid,
        created_at=datetime(2026, 3, 6, 9, 0, tzinfo=timezone.utc),
    )
    _insert_content(
        conn=conn,
        creator_id=creator_id,
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/reports-ui-critique-waiting",
        tid=waiting_tid,
        created_at=datetime(2026, 3, 6, 10, 0, tzinfo=timezone.utc),
    )
    _insert_content(
        conn=conn,
        creator_id=creator_id,
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/reports-ui-critique-blocked",
        tid=blocked_tid,
        created_at=datetime(2026, 3, 6, 11, 0, tzinfo=timezone.utc),
    )
    _insert_content(
        conn=conn,
        creator_id=creator_id,
        booking_link_id=booking_link_id,
        source_url=long_source_url,
        tid=empty_tid,
        created_at=datetime(2026, 3, 6, 12, 0, tzinfo=timezone.utc),
    )

    paid_booking_uuid = f"BOOK_REPORTS_UI_PAID_{suffix}"
    waiting_booking_uuid = f"BOOK_REPORTS_UI_WAITING_{suffix}"
    blocked_booking_uuid = f"BOOK_REPORTS_UI_BLOCKED_{suffix}"

    paid_booking_id = _insert_booking(
        conn=conn,
        creator_id=creator_id,
        booking_link_id=booking_link_id,
        tid=paid_tid,
        calendly_booking_uuid=paid_booking_uuid,
        booked_at=datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc),
        email=f"reports-ui-paid-{suffix}@example.com",
        frozen_amount_cents=19500,
        frozen_currency="USD",
    )
    _insert_booking(
        conn=conn,
        creator_id=creator_id,
        booking_link_id=booking_link_id,
        tid=waiting_tid,
        calendly_booking_uuid=waiting_booking_uuid,
        booked_at=datetime(2026, 3, 8, 10, 0, tzinfo=timezone.utc),
        email=f"reports-ui-waiting-{suffix}@example.com",
        frozen_amount_cents=19500,
        frozen_currency="USD",
    )
    blocked_booking_id = _insert_booking(
        conn=conn,
        creator_id=creator_id,
        booking_link_id=booking_link_id,
        tid=blocked_tid,
        calendly_booking_uuid=blocked_booking_uuid,
        booked_at=datetime(2026, 3, 8, 11, 0, tzinfo=timezone.utc),
        email=f"reports-ui-blocked-{suffix}@example.com",
        frozen_amount_cents=19500,
        frozen_currency="USD",
    )

    provider_invoice_id = f"in_reports_ui_critique_paid_{suffix}"
    provider_event_id = f"evt_reports_ui_critique_paid_{suffix}"
    invoice_id = _insert_invoice(
        conn=conn,
        creator_id=creator_id,
        booking_id=paid_booking_id,
        tid=paid_tid,
        stripe_account_id=stripe_account_id,
        stripe_invoice_id=provider_invoice_id,
        amount_cents=19500,
        issued_at=datetime(2026, 3, 8, 8, 30, tzinfo=timezone.utc),
        paid_at=datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc),
    )
    _insert_matched_payment_event(
        conn=conn,
        creator_id=creator_id,
        booking_id=paid_booking_id,
        tid=paid_tid,
        invoice_id=invoice_id,
        stripe_account_id=stripe_account_id,
        stripe_event_id=provider_event_id,
        stripe_invoice_id=provider_invoice_id,
        paid_at=datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc),
    )

    _insert_blocked_billing_case(
        conn=conn,
        creator_id=creator_id,
        booking_id=blocked_booking_id,
        tid=blocked_tid,
        calendly_booking_uuid=blocked_booking_uuid,
        stripe_account_id=stripe_account_id,
        blocked_at=datetime(2026, 3, 8, 11, 5, tzinfo=timezone.utc),
        frozen_amount_cents=19500,
        frozen_currency="USD",
    )

    _insert_unmatched_payment_event(
        conn=conn,
        creator_id=creator_id,
        stripe_account_id=stripe_account_id,
        stripe_event_id=f"evt_reports_ui_missing_tid_{suffix}",
        stripe_invoice_id=f"in_reports_ui_missing_tid_{suffix}",
        reason=UNATTRIBUTED_REASON_MISSING_TID,
        paid_at=datetime(2026, 3, 8, 11, 30, tzinfo=timezone.utc),
    )
    _insert_unmatched_payment_event(
        conn=conn,
        creator_id=creator_id,
        stripe_account_id=stripe_account_id,
        stripe_event_id=f"evt_reports_ui_unknown_invoice_{suffix}",
        stripe_invoice_id=f"in_reports_ui_unknown_invoice_{suffix}",
        reason=UNATTRIBUTED_REASON_UNKNOWN_STRIPE_INVOICE_ID,
        paid_at=datetime(2026, 3, 8, 11, 40, tzinfo=timezone.utc),
    )

    reports_url = f"{base_url}/app/reports"
    filtered_reports_url = (
        f"{base_url}/app/reports?start_date={FILTER_DATE.isoformat()}&end_date={FILTER_DATE.isoformat()}"
    )

    return SeedOutput(
        creator_email=creator_email,
        login_token=login_token,
        backup_login_token=backup_login_token,
        reports_url=reports_url,
        filtered_reports_url=filtered_reports_url,
        paid_tid=paid_tid,
        waiting_tid=waiting_tid,
        blocked_tid=blocked_tid,
        empty_tid=empty_tid,
        long_source_url=long_source_url,
    )


def main() -> int:
    args = _parser().parse_args()
    base_url = _normalize_base_url(args.base_url)
    engine = create_engine(_database_url())
    suffix = uuid.uuid4().hex[:8]
    creator_email = args.creator_email or f"reports.ui.critique.{suffix}@example.com"

    with engine.begin() as conn:
        seeded = _seed_reports_ui_critique(
            conn=conn,
            base_url=base_url,
            creator_email=creator_email,
            creator_name=args.creator_name,
        )

    print("STATE=reports-ui-critique")
    print("DESCRIPTION=Dedicated /app/reports mixed-state browser seed for GPT Pro UI critique.")
    print(f"CREATOR_EMAIL={seeded.creator_email}")
    print(f"LOGIN_URL={base_url}/auth/magic-link/verify?token={seeded.login_token}")
    print(f"BACKUP_LOGIN_URL={base_url}/auth/magic-link/verify?token={seeded.backup_login_token}")
    print(f"REPORTS_URL={seeded.reports_url}")
    print(f"FILTERED_REPORTS_URL={seeded.filtered_reports_url}")
    print(f"FILTER_DATE={FILTER_DATE.isoformat()}")
    print(f"PAID_TID={seeded.paid_tid}")
    print(f"WAITING_TID={seeded.waiting_tid}")
    print(f"BLOCKED_TID={seeded.blocked_tid}")
    print(f"EMPTY_TID={seeded.empty_tid}")
    print(f"LONG_SOURCE_URL={seeded.long_source_url}")
    print("VISIBLE_BACKLOG_REASONS=Missing tracking ID|Unknown invoice")
    print("SCREENSHOT_ORDER=desktop_unfiltered|desktop_filtered|top_cards_crop|row_list_crop|mobile_unfiltered")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
