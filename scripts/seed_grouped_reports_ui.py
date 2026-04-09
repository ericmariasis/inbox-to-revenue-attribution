import argparse
import hashlib
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.invoice_payment_events import (
    UNATTRIBUTED_REASON_MISSING_TID,
    UNATTRIBUTED_REASON_UNKNOWN_STRIPE_INVOICE_ID,
)
from tests.reporting_golden_fixture import (
    _attach_confirmed_topic,
    _create_blocked_billing_case,
    _create_booking,
    _create_booking_link,
    _create_content,
    _create_content_extraction_artifact,
    _create_creator_with_user,
    _create_matched_payment_event,
    _create_paid_invoice,
    _create_unmatched_payment_event,
)


FILTER_DATE = date(2026, 3, 8)


@dataclass(frozen=True)
class SeedOutput:
    creator_email: str
    login_token: str
    backup_login_token: str
    topics_url: str
    filtered_topics_url: str
    booking_links_url: str
    filtered_booking_links_url: str


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Seed one grouped-reports browser state for manual verification of "
            "/app/reports/topics and /app/reports/booking-links."
        ),
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="Browser base URL to print in the generated login and reports links.",
    )
    return parser


def _database_url() -> str:
    database_url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("error=Set TEST_DATABASE_URL or DATABASE_URL before running this seed.")
    return database_url


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


def seed_grouped_reports_ui(*, engine) -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:8]
    active_booking_link_name = "Discovery Call CTA"
    historical_booking_link_name = "Archived Webinar CTA"

    with Session(engine) as session:
        creator, user = _create_creator_with_user(
            session,
            suffix=f"grouped_{suffix}",
            stripe_account_id=f"acct_reports_grouped_{suffix}",
        )

        active_link = _create_booking_link(
            session,
            creator=creator,
            name=active_booking_link_name,
            destination_url="https://calendly.com/example/discovery-call-cta",
            billing_amount_cents=19500,
            billing_currency="USD",
        )
        historical_link = _create_booking_link(
            session,
            creator=creator,
            name="Legacy Webinar CTA",
            destination_url="https://calendly.com/example/legacy-webinar-cta",
            billing_amount_cents=5000,
            billing_currency="USD",
        )

        primary_tid = f"reportsgroupedprimary{suffix}"
        historical_tid = f"reportsgroupedhistorical{suffix}"

        primary_content = _create_content(
            session,
            creator=creator,
            booking_link=active_link,
            source_url="https://example.com/posts/reports-grouped-primary",
            tid=primary_tid,
        )
        historical_content = _create_content(
            session,
            creator=creator,
            booking_link=historical_link,
            source_url="https://example.com/posts/reports-grouped-historical",
            tid=historical_tid,
        )

        primary_artifact = _create_content_extraction_artifact(
            session,
            creator=creator,
            content=primary_content,
            title="Reports Grouped Primary",
            extracted_text="Grouped reports primary content for pricing and discovery calls.",
        )
        historical_artifact = _create_content_extraction_artifact(
            session,
            creator=creator,
            content=historical_content,
            title="Reports Grouped Historical",
            extracted_text="Grouped reports historical content for retention reviews.",
        )
        primary_content.authoritative_extraction_artifact_id = primary_artifact.id
        historical_content.authoritative_extraction_artifact_id = historical_artifact.id
        session.flush()

        _attach_confirmed_topic(
            session,
            creator=creator,
            content=primary_content,
            artifact=primary_artifact,
            label="Pricing Strategy",
            candidate_rank=1,
        )
        _attach_confirmed_topic(
            session,
            creator=creator,
            content=primary_content,
            artifact=primary_artifact,
            label="Discovery Calls",
            candidate_rank=2,
        )
        _attach_confirmed_topic(
            session,
            creator=creator,
            content=historical_content,
            artifact=historical_artifact,
            label="Retention Reviews",
            candidate_rank=1,
        )

        paid_booking = _create_booking(
            session,
            creator=creator,
            booking_link=active_link,
            content=primary_content,
            booking_uuid=f"BOOK_REPORTS_GROUPED_PAID_{suffix}",
            booked_at=datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc),
        )
        waiting_booking = _create_booking(
            session,
            creator=creator,
            booking_link=active_link,
            content=primary_content,
            booking_uuid=f"BOOK_REPORTS_GROUPED_WAITING_{suffix}",
            booked_at=datetime(2026, 3, 8, 10, 0, tzinfo=timezone.utc),
        )
        blocked_booking = _create_booking(
            session,
            creator=creator,
            booking_link=active_link,
            content=primary_content,
            booking_uuid=f"BOOK_REPORTS_GROUPED_BLOCKED_{suffix}",
            booked_at=datetime(2026, 3, 8, 11, 0, tzinfo=timezone.utc),
        )
        historical_booking = _create_booking(
            session,
            creator=creator,
            booking_link=historical_link,
            content=historical_content,
            booking_uuid=f"BOOK_REPORTS_GROUPED_HISTORICAL_{suffix}",
            booked_at=datetime(2026, 3, 7, 8, 0, tzinfo=timezone.utc),
        )

        paid_invoice = _create_paid_invoice(
            session,
            creator=creator,
            booking=paid_booking,
            stripe_invoice_id=f"in_reports_grouped_paid_{suffix}",
            amount_cents=19500,
            paid_at=datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc),
        )
        _create_matched_payment_event(
            session,
            creator=creator,
            booking=paid_booking,
            invoice=paid_invoice,
            stripe_event_id=f"evt_reports_grouped_paid_{suffix}",
            paid_at=datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc),
        )
        _create_paid_invoice(
            session,
            creator=creator,
            booking=historical_booking,
            stripe_invoice_id=f"in_reports_grouped_historical_{suffix}",
            amount_cents=5000,
            paid_at=datetime(2026, 3, 7, 9, 0, tzinfo=timezone.utc),
        )

        _create_blocked_billing_case(
            session,
            creator=creator,
            booking=blocked_booking,
            blocked_at=datetime(2026, 3, 8, 11, 5, tzinfo=timezone.utc),
        )
        _create_unmatched_payment_event(
            session,
            creator=creator,
            stripe_event_id=f"evt_reports_grouped_unknown_invoice_{suffix}",
            stripe_invoice_id=f"in_reports_grouped_unknown_invoice_{suffix}",
            reason=UNATTRIBUTED_REASON_UNKNOWN_STRIPE_INVOICE_ID,
            paid_at=datetime(2026, 3, 8, 11, 30, tzinfo=timezone.utc),
            booking=waiting_booking,
            tid=primary_tid,
        )
        _create_unmatched_payment_event(
            session,
            creator=creator,
            stripe_event_id=f"evt_reports_grouped_missing_tid_{suffix}",
            stripe_invoice_id=f"in_reports_grouped_missing_tid_{suffix}",
            reason=UNATTRIBUTED_REASON_MISSING_TID,
            paid_at=datetime(2026, 3, 8, 11, 40, tzinfo=timezone.utc),
        )

        historical_link.name = historical_booking_link_name
        historical_link.billing_amount_cents = None
        historical_link.billing_currency = None
        historical_link.destination_url = "https://calendly.com/example/archived-webinar-cta"
        session.commit()

        return str(user.id), user.email


def main() -> None:
    args = _parser().parse_args()
    base_url = _normalize_base_url(args.base_url)
    engine = create_engine(_database_url())
    user_id, creator_email = seed_grouped_reports_ui(engine=engine)

    login_token = _magic_link_token("grouped-reports-ui")
    backup_login_token = _magic_link_token("grouped-reports-ui-backup")

    with engine.begin() as conn:
        _insert_magic_link_token(conn=conn, user_id=user_id, raw_token=login_token)
        _insert_magic_link_token(conn=conn, user_id=user_id, raw_token=backup_login_token)

    output = SeedOutput(
        creator_email=creator_email,
        login_token=login_token,
        backup_login_token=backup_login_token,
        topics_url=f"{base_url}/app/reports/topics",
        filtered_topics_url=(
            f"{base_url}/app/reports/topics?"
            f"start_date={FILTER_DATE.isoformat()}&end_date={FILTER_DATE.isoformat()}"
        ),
        booking_links_url=f"{base_url}/app/reports/booking-links",
        filtered_booking_links_url=(
            f"{base_url}/app/reports/booking-links?"
            f"start_date={FILTER_DATE.isoformat()}&end_date={FILTER_DATE.isoformat()}"
        ),
    )

    print(f"CREATOR_EMAIL={output.creator_email}")
    print(f"LOGIN_URL={base_url}/auth/magic-link/verify?token={output.login_token}")
    print(f"BACKUP_LOGIN_URL={base_url}/auth/magic-link/verify?token={output.backup_login_token}")
    print(f"TOPICS_URL={output.topics_url}")
    print(f"FILTERED_TOPICS_URL={output.filtered_topics_url}")
    print(f"BOOKING_LINKS_URL={output.booking_links_url}")
    print(f"FILTERED_BOOKING_LINKS_URL={output.filtered_booking_links_url}")


if __name__ == "__main__":
    main()
