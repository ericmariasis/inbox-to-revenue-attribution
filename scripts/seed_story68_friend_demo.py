import argparse
import hashlib
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, text

from app.core.config import get_settings
from app.services.invoice_payment_events import UNATTRIBUTED_REASON_UNKNOWN_BOOKING_UUID


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Seed one operator-driven Story 68 friend demo package.",
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8050",
        help="Browser base URL to print in the generated demo links.",
    )
    parser.add_argument(
        "--creator-email",
        help="Optional fixed creator email. Defaults to a generated demo email.",
    )
    parser.add_argument(
        "--creator-name",
        default="Story 68 Friend Demo",
        help="Creator name to seed for the browser walkthrough.",
    )
    return parser


def _normalize_base_url(raw_value: str) -> str:
    value = str(raw_value).strip().rstrip("/")
    if not value.startswith(("http://", "https://")):
        raise SystemExit("error=--base-url must start with http:// or https://")
    return value


def _magic_link_token() -> str:
    return f"story68-demo-{uuid.uuid4().hex}"


def main() -> int:
    args = _parser().parse_args()
    base_url = _normalize_base_url(args.base_url)
    settings = get_settings()
    engine = create_engine(settings.database_url)

    suffix = uuid.uuid4().hex[:8]
    creator_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    booking_link_id = str(uuid.uuid4())
    reporting_content_id = str(uuid.uuid4())
    review_content_id = str(uuid.uuid4())
    paid_booking_id = str(uuid.uuid4())
    blocked_booking_id = str(uuid.uuid4())
    paid_invoice_id = str(uuid.uuid4())
    fetch_snapshot_id = str(uuid.uuid4())
    extraction_artifact_id = str(uuid.uuid4())
    creator_email = args.creator_email or f"story68.friend.{suffix}@example.com"
    report_tid = f"friendreport{suffix}"
    review_tid = f"friendreview{suffix}"
    login_token = _magic_link_token()
    backup_login_token = _magic_link_token()

    paid_at = datetime(2026, 3, 10, 14, 30, tzinfo=timezone.utc)
    blocked_at = datetime(2026, 3, 10, 14, 12, tzinfo=timezone.utc)
    extracted_at = datetime(2026, 3, 10, 15, 1, tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO creators "
                "(id, name, stripe_connect_status, stripe_account_id, stripe_connected_at) "
                "VALUES "
                "(:id, :name, :stripe_connect_status, :stripe_account_id, :stripe_connected_at)"
            ),
            {
                "id": creator_id,
                "name": args.creator_name,
                "stripe_connect_status": "connected",
                "stripe_account_id": f"acct_story68_demo_{suffix}",
                "stripe_connected_at": paid_at - timedelta(days=1),
            },
        )
        conn.execute(
            text(
                "INSERT INTO auth_users (id, creator_id, email) "
                "VALUES (:id, :creator_id, :email)"
            ),
            {"id": user_id, "creator_id": creator_id, "email": creator_email},
        )

        for raw_token in (login_token, backup_login_token):
            conn.execute(
                text(
                    "INSERT INTO magic_link_tokens "
                    "(id, user_id, token_hash, expires_at) "
                    "VALUES (:id, :user_id, :token_hash, :expires_at)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "user_id": user_id,
                    "token_hash": hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
                    "expires_at": now + timedelta(hours=24),
                },
            )

        conn.execute(
            text(
                "INSERT INTO booking_links "
                "(id, creator_id, name, calendly_url, billing_amount_cents, billing_currency) "
                "VALUES "
                "(:id, :creator_id, :name, :calendly_url, :billing_amount_cents, :billing_currency)"
            ),
            {
                "id": booking_link_id,
                "creator_id": creator_id,
                "name": "Strategy Session",
                "calendly_url": "https://calendly.com/example/story68-friend-demo",
                "billing_amount_cents": 19500,
                "billing_currency": "USD",
            },
        )

        conn.execute(
            text(
                "INSERT INTO content "
                "(id, creator_id, booking_link_id, source_url, tid, created_at, updated_at) "
                "VALUES "
                "(:id, :creator_id, :booking_link_id, :source_url, :tid, :created_at, :updated_at)"
            ),
            {
                "id": reporting_content_id,
                "creator_id": creator_id,
                "booking_link_id": booking_link_id,
                "source_url": "https://example.com/posts/referral-playbook-demo",
                "tid": report_tid,
                "created_at": paid_at - timedelta(days=2),
                "updated_at": paid_at - timedelta(days=2),
            },
        )
        conn.execute(
            text(
                "INSERT INTO content "
                "(id, creator_id, booking_link_id, source_url, tid, created_at, updated_at) "
                "VALUES "
                "(:id, :creator_id, :booking_link_id, :source_url, :tid, :created_at, :updated_at)"
            ),
            {
                "id": review_content_id,
                "creator_id": creator_id,
                "booking_link_id": booking_link_id,
                "source_url": "https://example.com/posts/launch-pricing-breakdown-demo",
                "tid": review_tid,
                "created_at": extracted_at - timedelta(hours=1),
                "updated_at": extracted_at - timedelta(hours=1),
            },
        )

        conn.execute(
            text(
                "INSERT INTO bookings "
                "(id, creator_id, booking_link_id, tid, calendly_booking_uuid, email, status, booked_at, canceled_at, "
                "frozen_billing_amount_cents, frozen_billing_currency) "
                "VALUES "
                "(:id, :creator_id, :booking_link_id, :tid, :calendly_booking_uuid, :email, :status, :booked_at, :canceled_at, "
                ":frozen_billing_amount_cents, :frozen_billing_currency)"
            ),
            {
                "id": paid_booking_id,
                "creator_id": creator_id,
                "booking_link_id": booking_link_id,
                "tid": report_tid,
                "calendly_booking_uuid": f"BOOK_story68_demo_paid_{suffix}",
                "email": "paid-demo@example.com",
                "status": "created",
                "booked_at": paid_at - timedelta(minutes=30),
                "canceled_at": None,
                "frozen_billing_amount_cents": 19500,
                "frozen_billing_currency": "USD",
            },
        )
        conn.execute(
            text(
                "INSERT INTO bookings "
                "(id, creator_id, booking_link_id, tid, calendly_booking_uuid, email, status, booked_at, canceled_at, "
                "frozen_billing_amount_cents, frozen_billing_currency) "
                "VALUES "
                "(:id, :creator_id, :booking_link_id, :tid, :calendly_booking_uuid, :email, :status, :booked_at, :canceled_at, "
                ":frozen_billing_amount_cents, :frozen_billing_currency)"
            ),
            {
                "id": blocked_booking_id,
                "creator_id": creator_id,
                "booking_link_id": booking_link_id,
                "tid": report_tid,
                "calendly_booking_uuid": f"BOOK_story68_demo_blocked_{suffix}",
                "email": "blocked-demo@example.com",
                "status": "created",
                "booked_at": paid_at - timedelta(minutes=20),
                "canceled_at": None,
                "frozen_billing_amount_cents": 19500,
                "frozen_billing_currency": "USD",
            },
        )

        conn.execute(
            text(
                "INSERT INTO invoices "
                "(id, creator_id, booking_id, tid, stripe_account_id, stripe_invoice_id, amount_cents, currency, status, issued_at, paid_at, voided_at) "
                "VALUES "
                "(:id, :creator_id, :booking_id, :tid, :stripe_account_id, :stripe_invoice_id, :amount_cents, :currency, :status, :issued_at, :paid_at, :voided_at)"
            ),
            {
                "id": paid_invoice_id,
                "creator_id": creator_id,
                "booking_id": paid_booking_id,
                "tid": report_tid,
                "stripe_account_id": f"acct_story68_demo_{suffix}",
                "stripe_invoice_id": f"in_story68_demo_paid_{suffix}",
                "amount_cents": 19500,
                "currency": "USD",
                "status": "paid",
                "issued_at": paid_at - timedelta(hours=1),
                "paid_at": paid_at,
                "voided_at": None,
            },
        )

        conn.execute(
            text(
                "INSERT INTO invoice_payment_events "
                "(id, stripe_event_id, stripe_event_type, stripe_account_id, stripe_invoice_id, invoice_id, creator_id, booking_id, tid, status, unattributed_reason, paid_at, received_at, processed_at) "
                "VALUES "
                "(:id, :stripe_event_id, :stripe_event_type, :stripe_account_id, :stripe_invoice_id, :invoice_id, :creator_id, :booking_id, :tid, :status, :unattributed_reason, :paid_at, :received_at, :processed_at)"
            ),
            {
                "id": str(uuid.uuid4()),
                "stripe_event_id": f"evt_story68_demo_paid_{suffix}",
                "stripe_event_type": "invoice.paid",
                "stripe_account_id": f"acct_story68_demo_{suffix}",
                "stripe_invoice_id": f"in_story68_demo_paid_{suffix}",
                "invoice_id": paid_invoice_id,
                "creator_id": creator_id,
                "booking_id": paid_booking_id,
                "tid": report_tid,
                "status": "applied",
                "unattributed_reason": None,
                "paid_at": paid_at,
                "received_at": paid_at,
                "processed_at": paid_at,
            },
        )

        conn.execute(
            text(
                "INSERT INTO blocked_billing_cases "
                "(id, creator_id, booking_id, invoice_id, tid, calendly_booking_uuid, stripe_account_id, "
                "frozen_amount_cents, frozen_currency, status, reason_code, provider_operation, "
                "provider_http_status, provider_error_code, first_blocked_at, last_blocked_at, "
                "last_retry_at, resolved_at, resolution_code) "
                "VALUES "
                "(:id, :creator_id, :booking_id, :invoice_id, :tid, :calendly_booking_uuid, :stripe_account_id, "
                ":frozen_amount_cents, :frozen_currency, :status, :reason_code, :provider_operation, "
                ":provider_http_status, :provider_error_code, :first_blocked_at, :last_blocked_at, "
                ":last_retry_at, :resolved_at, :resolution_code)"
            ),
            {
                "id": str(uuid.uuid4()),
                "creator_id": creator_id,
                "booking_id": blocked_booking_id,
                "invoice_id": None,
                "tid": report_tid,
                "calendly_booking_uuid": f"BOOK_story68_demo_blocked_{suffix}",
                "stripe_account_id": f"acct_story68_demo_{suffix}",
                "frozen_amount_cents": 19500,
                "frozen_currency": "USD",
                "status": "open",
                "reason_code": "provider_error",
                "provider_operation": "stripe_invoice_create",
                "provider_http_status": 502,
                "provider_error_code": "api_connection_error",
                "first_blocked_at": blocked_at,
                "last_blocked_at": blocked_at,
                "last_retry_at": None,
                "resolved_at": None,
                "resolution_code": None,
            },
        )

        conn.execute(
            text(
                "INSERT INTO invoice_payment_events "
                "(id, stripe_event_id, stripe_event_type, stripe_account_id, stripe_invoice_id, invoice_id, creator_id, booking_id, tid, status, unattributed_reason, paid_at, received_at, processed_at) "
                "VALUES "
                "(:id, :stripe_event_id, :stripe_event_type, :stripe_account_id, :stripe_invoice_id, :invoice_id, :creator_id, :booking_id, :tid, :status, :unattributed_reason, :paid_at, :received_at, :processed_at)"
            ),
            {
                "id": str(uuid.uuid4()),
                "stripe_event_id": f"evt_story68_demo_unmatched_{suffix}",
                "stripe_event_type": "invoice.paid",
                "stripe_account_id": f"acct_story68_demo_{suffix}",
                "stripe_invoice_id": f"in_story68_demo_unmatched_{suffix}",
                "invoice_id": None,
                "creator_id": creator_id,
                "booking_id": None,
                "tid": None,
                "status": "unmatched",
                "unattributed_reason": UNATTRIBUTED_REASON_UNKNOWN_BOOKING_UUID,
                "paid_at": paid_at + timedelta(minutes=15),
                "received_at": paid_at + timedelta(minutes=15),
                "processed_at": None,
            },
        )

        conn.execute(
            text(
                "INSERT INTO content_fetch_snapshots "
                "("
                "id, content_id, creator_id, requested_url, fetched_url, fetch_status, http_status, "
                "failure_reason_code, failure_detail, response_content_type, response_content_charset, "
                "snapshot_text, fetched_at"
                ") "
                "VALUES "
                "("
                ":id, :content_id, :creator_id, :requested_url, :fetched_url, :fetch_status, :http_status, "
                ":failure_reason_code, :failure_detail, :response_content_type, :response_content_charset, "
                ":snapshot_text, :fetched_at"
                ")"
            ),
            {
                "id": fetch_snapshot_id,
                "content_id": review_content_id,
                "creator_id": creator_id,
                "requested_url": "https://example.com/posts/launch-pricing-breakdown-demo",
                "fetched_url": "https://example.com/posts/launch-pricing-breakdown-demo",
                "fetch_status": "succeeded",
                "http_status": 200,
                "failure_reason_code": None,
                "failure_detail": None,
                "response_content_type": "text/html",
                "response_content_charset": "utf-8",
                "snapshot_text": (
                    "<html><body><article><h1>Launch Pricing Breakdown</h1>"
                    "<p>Discovery calls convert better when pricing expectations are clear.</p>"
                    "<p>Retainer onboarding gives new clients a faster start.</p>"
                    "<p>Workshop followup notes can create noisy draft topics.</p>"
                    "</article></body></html>"
                ),
                "fetched_at": extracted_at - timedelta(minutes=1),
            },
        )

        extracted_text = (
            "Discovery calls convert better when pricing expectations are clear.\n"
            "Retainer onboarding gives new clients a faster start.\n"
            "Workshop followup notes can create noisy draft topics.\n"
            "Boilerplate welcome copy should stay out of the canonical set."
        )
        conn.execute(
            text(
                "INSERT INTO content_extraction_artifacts "
                "("
                "id, content_id, creator_id, fetch_snapshot_id, extraction_status, extraction_reason_code, "
                "extraction_detail, extraction_method, title, published_at, published_at_raw, "
                "source_text_char_count, extracted_text_char_count, extracted_text_word_count, extracted_text, created_at"
                ") "
                "VALUES "
                "("
                ":id, :content_id, :creator_id, :fetch_snapshot_id, :extraction_status, :extraction_reason_code, "
                ":extraction_detail, :extraction_method, :title, :published_at, :published_at_raw, "
                ":source_text_char_count, :extracted_text_char_count, :extracted_text_word_count, :extracted_text, :created_at"
                ")"
            ),
            {
                "id": extraction_artifact_id,
                "content_id": review_content_id,
                "creator_id": creator_id,
                "fetch_snapshot_id": fetch_snapshot_id,
                "extraction_status": "succeeded",
                "extraction_reason_code": None,
                "extraction_detail": None,
                "extraction_method": "html_article",
                "title": "Launch Pricing Breakdown",
                "published_at": None,
                "published_at_raw": None,
                "source_text_char_count": len(extracted_text),
                "extracted_text_char_count": len(extracted_text),
                "extracted_text_word_count": len(extracted_text.split()),
                "extracted_text": extracted_text,
                "created_at": extracted_at,
            },
        )

    print(f"LOGIN_URL={base_url}/auth/magic-link/verify?token={login_token}")
    print(f"BACKUP_LOGIN_URL={base_url}/auth/magic-link/verify?token={backup_login_token}")
    print(f"HOME_URL={base_url}/app")
    print(f"REPORTS_URL={base_url}/app/reports")
    print(f"ATTENTION_URL={base_url}/app/attention")
    print(f"TOPICS_URL={base_url}/app/content/{review_tid}/topics")
    print(f"REVIEW_TID={review_tid}")
    print(f"CREATOR_EMAIL={creator_email}")
    print(f"REPORT_TID={report_tid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
