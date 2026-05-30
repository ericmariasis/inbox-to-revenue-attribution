import json
import os
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import create_engine, text

from app.core.config import get_settings
from app.main import app
from app.services.booking_attribution import (
    BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED,
    BOOKING_UNATTRIBUTED_REASON_MISSING_TID,
)
from app.services.billing_provider import (
    BILLING_ACCOUNT_READINESS_ISSUE_CONFIRM_PAYPAL_PRIMARY_EMAIL,
    BILLING_ACCOUNT_READINESS_ISSUE_ENABLE_PAYPAL_PAYMENTS_RECEIVABLE,
    BillingAccountReadiness,
)
from app.services.email_provider import (
    MagicLinkEmailDeliveryError,
    SupportRequestEmailDeliveryError,
)
from app.services.email_stub import get_magic_link_outbox, get_support_request_outbox
from app.services.growth_loop_agent import LoomiDiagnosticContext
from app.services.invoice_payment_events import (
    UNATTRIBUTED_REASON_MISSING_TID,
    UNATTRIBUTED_REASON_UNKNOWN_STRIPE_INVOICE_ID,
)
from app.services.next_content_experiments import UNSUPPORTED_EXPERIMENTS_SUMMARY
from app.services.operator_experiment_drafts import (
    OperatorExperimentDraftProviderOutput,
    OperatorExperimentDraftUnavailableError,
    _OperatorExperimentDraftCardPayload,
)
from app.services.paypal_provider import PayPalConnectOnboardingResult, PayPalProviderError
from app.services.rate_limit import (
    DEFAULT_SHARED_RATE_LIMITER,
    SUPPORT_REQUEST_SUBMIT_POLICY,
    build_support_request_rate_limit_bucket_key,
)
from app.services.stripe_provider import (
    StripeAccountReadiness,
    StripeInvoiceCreateResult,
    StripeProviderError,
)

HTML_ACCEPT_HEADERS = {"Accept": "text/html,application/xhtml+xml"}
SESSION_COOKIE_NAME = "ccp_creator_session"


def _engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def _auth_state_for_email(email: str) -> dict[str, int]:
    with _engine().connect() as conn:
        row = conn.execute(
            text(
                "SELECT "
                "  (SELECT count(*) FROM auth_users WHERE email = :email) AS auth_user_count, "
                "  ("
                "    SELECT count(*) "
                "    FROM creators c "
                "    JOIN auth_users au ON au.creator_id = c.id "
                "    WHERE au.email = :email"
                "  ) AS creator_count, "
                "  (SELECT count(*) FROM pending_magic_link_issuances WHERE email = :email) AS pending_count"
            ),
            {"email": email},
        ).mappings().one()
    return {
        "auth_users": row["auth_user_count"],
        "creators": row["creator_count"],
        "pending": row["pending_count"],
    }


def _latest_magic_link_token_for_email(email: str) -> str:
    for message in reversed(get_magic_link_outbox()):
        if message["email"] == email:
            return message["token"]
    raise AssertionError(f"No magic-link token found for {email}")


def _latest_support_request(request_type: str) -> dict[str, str]:
    for message in reversed(get_support_request_outbox()):
        if message["request_type"] == request_type:
            return message
    raise AssertionError(f"No support-request email found for {request_type}")


def _support_requests_for_creator(*, creator_id: str, request_type: str | None = None) -> list[dict[str, object]]:
    query = (
        "SELECT id, creator_id, request_type, requester_email, creator_name_snapshot, status, "
        "notification_attempted_at, notification_sent_at, notification_failed_at, closed_at, "
        "created_at, updated_at "
        "FROM support_requests WHERE creator_id = :creator_id"
    )
    params: dict[str, object] = {"creator_id": creator_id}
    if request_type is not None:
        query += " AND request_type = :request_type"
        params["request_type"] = request_type
    query += " ORDER BY created_at ASC, id ASC"

    with _engine().connect() as conn:
        rows = conn.execute(text(query), params).mappings().all()

    return [dict(row) for row in rows]


def _operator_allowlist_settings(*emails: str):
    settings = get_settings()
    return settings.model_copy(
        update={
            "app_env": "test",
            "operator_email_allowlist": ",".join(emails),
        }
    )


def _paypal_operator_only_settings(*emails: str, environment: str = "sandbox"):
    return _operator_allowlist_settings(*emails).model_copy(
        update={
            "paypal_environment": environment,
            "paypal_creator_access": "operator_only",
        }
    )


def _live_paypal_operator_only_settings(*emails: str):
    return _paypal_operator_only_settings(*emails, environment="live")


def _growth_loop_settings(
    *,
    enabled: bool,
    live_loomi_enabled: bool = False,
    bloomreach_segment_proof_enabled: bool = False,
):
    settings = get_settings()
    return settings.model_copy(
        update={
            "app_env": "test",
            "growth_loop_agent_feature_enabled": enabled,
            "growth_loop_loomi_mcp_enabled": live_loomi_enabled,
            "growth_loop_loomi_mcp_endpoint": (
                "https://loomi.example.test/mcp" if live_loomi_enabled else ""
            ),
            "growth_loop_loomi_mcp_access_token": (
                "ui-test-token" if live_loomi_enabled else ""
            ),
            "growth_loop_loomi_mcp_project_id": (
                "project_ui" if live_loomi_enabled else ""
            ),
            "growth_loop_bloomreach_segment_proof_enabled": bloomreach_segment_proof_enabled,
            "growth_loop_bloomreach_segment_proof_name": (
                "CCP Cart Recovery Demo" if bloomreach_segment_proof_enabled else ""
            ),
            "growth_loop_bloomreach_segment_proof_id": (
                "seg_ui_growth_loop_demo" if bloomreach_segment_proof_enabled else ""
            ),
        }
    )


class _FakeLiveLoomiProvider:
    def load_context(self) -> LoomiDiagnosticContext:
        return LoomiDiagnosticContext(
            source_label="Loomi live MCP diagnostics",
            source_kind="live_mcp",
            source_status_label="Loomi live MCP",
            source_status_kind="live_mcp",
            source_status_detail="Live Loomi MCP responded through the configured runtime provider.",
            segments=("Live high-intent booked prospects",),
            predictions=(),
            recommendations=("Review a live Loomi recommendation before sending.",),
            analytics=("Pacific Tutors live overview",),
            limitations=(
                "Loomi MCP results are diagnostic context only.",
                "They do not count revenue, prove causality, or replace app-owned booking and payment records.",
            ),
        )


def _insert_creator_user(
    *,
    email: str,
    name: str = "UI Creator",
    stripe_connect_status: str = "pending",
    stripe_account_id: str | None = None,
    stripe_connected_at: datetime | None = None,
    billing_provider: str = "stripe",
    billing_connect_status: str | None = None,
    billing_account_id: str | None = None,
    billing_connected_at: datetime | None = None,
) -> dict[str, str]:
    creator_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    resolved_billing_connect_status = billing_connect_status or stripe_connect_status
    resolved_billing_account_id = billing_account_id if billing_account_id is not None else stripe_account_id
    resolved_billing_connected_at = (
        billing_connected_at if billing_connected_at is not None else stripe_connected_at
    )

    with _engine().begin() as conn:
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
                "billing_provider": billing_provider,
                "billing_connect_status": resolved_billing_connect_status,
                "billing_account_id": resolved_billing_account_id,
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


def _insert_content(
    *,
    creator_id: str,
    booking_link_id: str,
    source_url: str,
    tid: str,
    content_id: str | None = None,
    created_at: datetime | None = None,
) -> str:
    content_id = content_id or str(uuid.uuid4())
    created_at = created_at or datetime.now(timezone.utc)

    with _engine().begin() as conn:
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


def _insert_fetch_snapshot(
    *,
    content_id: str,
    creator_id: str,
    requested_url: str,
    fetched_url: str | None,
    fetch_status: str,
    http_status: int | None,
    snapshot_text: str | None,
    fetched_at: datetime,
    response_content_type: str = "text/html",
) -> str:
    snapshot_id = str(uuid.uuid4())

    with _engine().begin() as conn:
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
                "id": snapshot_id,
                "content_id": content_id,
                "creator_id": creator_id,
                "requested_url": requested_url,
                "fetched_url": fetched_url,
                "fetch_status": fetch_status,
                "http_status": http_status,
                "failure_reason_code": None,
                "failure_detail": None,
                "response_content_type": response_content_type,
                "response_content_charset": "utf-8",
                "snapshot_text": snapshot_text,
                "fetched_at": fetched_at,
            },
        )

    return snapshot_id


def _insert_extraction_artifact(
    *,
    content_id: str,
    creator_id: str,
    fetch_snapshot_id: str,
    extraction_status: str,
    title: str | None,
    extracted_text: str | None,
    created_at: datetime,
    extraction_method: str = "html_article",
) -> str:
    artifact_id = str(uuid.uuid4())
    extracted_text = extracted_text or ""

    with _engine().begin() as conn:
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
                "id": artifact_id,
                "content_id": content_id,
                "creator_id": creator_id,
                "fetch_snapshot_id": fetch_snapshot_id,
                "extraction_status": extraction_status,
                "extraction_reason_code": None,
                "extraction_detail": None,
                "extraction_method": extraction_method,
                "title": title,
                "published_at": None,
                "published_at_raw": None,
                "source_text_char_count": len(extracted_text),
                "extracted_text_char_count": len(extracted_text),
                "extracted_text_word_count": len(extracted_text.split()),
                "extracted_text": extracted_text or None,
                "created_at": created_at,
            },
        )

    return artifact_id


def _insert_confirmed_topic(
    *,
    content_id: str,
    creator_id: str,
    extraction_artifact_id: str,
    label: str,
    candidate_rank: int = 1,
) -> str:
    topic_id = str(uuid.uuid4())
    reviewed_at = datetime.now(timezone.utc)

    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO content_confirmed_topics "
                "(id, content_id, creator_id, canonical_label, normalized_label, created_at, updated_at) "
                "VALUES "
                "(:id, :content_id, :creator_id, :canonical_label, :normalized_label, :created_at, :updated_at)"
            ),
            {
                "id": topic_id,
                "content_id": content_id,
                "creator_id": creator_id,
                "canonical_label": label,
                "normalized_label": label.casefold(),
                "created_at": reviewed_at,
                "updated_at": reviewed_at,
            },
        )
        conn.execute(
            text(
                "INSERT INTO content_topic_candidates "
                "("
                "id, content_id, creator_id, extraction_artifact_id, confirmed_topic_id, suggested_label, "
                "normalized_label, suggestion_method, candidate_rank, review_status, reviewed_at, created_at"
                ") VALUES ("
                ":id, :content_id, :creator_id, :extraction_artifact_id, :confirmed_topic_id, :suggested_label, "
                ":normalized_label, :suggestion_method, :candidate_rank, :review_status, :reviewed_at, :created_at"
                ")"
            ),
            {
                "id": str(uuid.uuid4()),
                "content_id": content_id,
                "creator_id": creator_id,
                "extraction_artifact_id": extraction_artifact_id,
                "confirmed_topic_id": topic_id,
                "suggested_label": label,
                "normalized_label": label.casefold(),
                "suggestion_method": "text_keywords",
                "candidate_rank": candidate_rank,
                "review_status": "confirmed",
                "reviewed_at": reviewed_at,
                "created_at": reviewed_at,
            },
        )

    return topic_id


def _set_authoritative_extraction_artifact(
    *,
    content_id: str,
    artifact_id: str | None,
) -> None:
    with _engine().begin() as conn:
        conn.execute(
            text(
                "UPDATE content "
                "SET authoritative_extraction_artifact_id = :artifact_id "
                "WHERE id = :content_id"
            ),
            {
                "content_id": content_id,
                "artifact_id": artifact_id,
            },
        )


def _fetch_topic_candidate_rows(*, content_id: str) -> list[dict[str, object]]:
    with _engine().connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, suggested_label, review_status "
                "FROM content_topic_candidates "
                "WHERE content_id = :content_id "
                "ORDER BY candidate_rank ASC, created_at ASC, id ASC"
            ),
            {"content_id": content_id},
        ).mappings().all()

    return [dict(row) for row in rows]


def _fetch_content_authority_row(*, content_id: str) -> dict[str, object]:
    with _engine().connect() as conn:
        row = conn.execute(
            text(
                "SELECT authoritative_extraction_artifact_id "
                "FROM content "
                "WHERE id = :content_id"
            ),
            {"content_id": content_id},
        ).mappings().one()

    return dict(row)


def _insert_booking(
    *,
    creator_id: str,
    booking_link_id: str,
    tid: str | None,
    calendly_booking_uuid: str,
    booked_at: datetime,
    status: str = "created",
    canceled_at: datetime | None = None,
    email: str = "booked@example.com",
    attribution_status: str | None = None,
    unattributed_reason: str | None = None,
) -> str:
    booking_id = str(uuid.uuid4())
    resolved_attribution_status = attribution_status or "attributed"

    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO bookings "
                "("
                "id, creator_id, booking_link_id, tid, calendly_booking_uuid, email, status, "
                "attribution_status, unattributed_reason, booked_at, canceled_at"
                ") "
                "VALUES "
                "("
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
                "email": email,
                "status": status,
                "attribution_status": resolved_attribution_status,
                "unattributed_reason": unattributed_reason,
                "booked_at": booked_at,
                "canceled_at": canceled_at,
            },
        )

    return booking_id


def _insert_invoice(
    *,
    creator_id: str,
    booking_id: str,
    tid: str,
    stripe_account_id: str | None = None,
    stripe_invoice_id: str | None = None,
    amount_cents: int,
    paid_at: datetime,
    status: str = "paid",
    currency: str = "USD",
    payment_provider: str = "stripe",
    provider_account_id: str | None = None,
    provider_invoice_id: str | None = None,
) -> str:
    invoice_id = str(uuid.uuid4())
    resolved_provider_account_id = (
        provider_account_id if provider_account_id is not None else stripe_account_id
    )
    resolved_provider_invoice_id = (
        provider_invoice_id if provider_invoice_id is not None else stripe_invoice_id
    )

    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO invoices "
                "(id, creator_id, booking_id, tid, payment_provider, provider_account_id, provider_invoice_id, stripe_account_id, stripe_invoice_id, amount_cents, currency, status, issued_at, paid_at, voided_at) "
                "VALUES "
                "(:id, :creator_id, :booking_id, :tid, :payment_provider, :provider_account_id, :provider_invoice_id, :stripe_account_id, :stripe_invoice_id, :amount_cents, :currency, :status, :issued_at, :paid_at, :voided_at)"
            ),
            {
                "id": invoice_id,
                "creator_id": creator_id,
                "booking_id": booking_id,
                "tid": tid,
                "payment_provider": payment_provider,
                "provider_account_id": resolved_provider_account_id,
                "provider_invoice_id": resolved_provider_invoice_id,
                "stripe_account_id": stripe_account_id,
                "stripe_invoice_id": stripe_invoice_id,
                "amount_cents": amount_cents,
                "currency": currency,
                "status": status,
                "issued_at": paid_at - timedelta(hours=1),
                "paid_at": paid_at,
                "voided_at": None,
            },
        )

    return invoice_id


def _insert_billing_provider_switch_attempt(
    *,
    creator_id: str,
    source_billing_provider: str,
    target_billing_provider: str,
    target_billing_connect_status: str = "pending",
    target_billing_account_id: str | None = None,
    target_billing_provider_correlation_id: str | None = None,
    target_billing_connected_at: datetime | None = None,
) -> str:
    attempt_id = str(uuid.uuid4())

    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO billing_provider_switch_attempts "
                "("
                "id, creator_id, source_billing_provider, target_billing_provider, "
                "target_billing_connect_status, target_billing_account_id, "
                "target_billing_provider_correlation_id, target_billing_connected_at"
                ") "
                "VALUES "
                "("
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


def _insert_unmatched_payment_event(
    *,
    creator_id: str,
    stripe_account_id: str | None = None,
    stripe_event_id: str | None = None,
    stripe_invoice_id: str | None = None,
    reason: str,
    paid_at: datetime,
    booking_id: str | None = None,
    tid: str | None = None,
    payment_provider: str = "stripe",
    provider_account_id: str | None = None,
    provider_event_id: str | None = None,
    provider_event_type: str | None = None,
    provider_invoice_id: str | None = None,
) -> str:
    payment_event_id = str(uuid.uuid4())
    resolved_provider_account_id = (
        provider_account_id if provider_account_id is not None else stripe_account_id
    )
    resolved_provider_event_id = (
        provider_event_id if provider_event_id is not None else stripe_event_id
    )
    resolved_provider_event_type = (
        provider_event_type if provider_event_type is not None else "invoice.paid"
    )
    resolved_provider_invoice_id = (
        provider_invoice_id if provider_invoice_id is not None else stripe_invoice_id
    )

    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO invoice_payment_events "
                "(id, payment_provider, provider_event_id, provider_event_type, provider_account_id, provider_invoice_id, stripe_event_id, stripe_event_type, stripe_account_id, stripe_invoice_id, invoice_id, creator_id, booking_id, tid, status, unattributed_reason, paid_at, received_at, processed_at) "
                "VALUES "
                "(:id, :payment_provider, :provider_event_id, :provider_event_type, :provider_account_id, :provider_invoice_id, :stripe_event_id, :stripe_event_type, :stripe_account_id, :stripe_invoice_id, :invoice_id, :creator_id, :booking_id, :tid, :status, :unattributed_reason, :paid_at, :received_at, :processed_at)"
            ),
            {
                "id": payment_event_id,
                "payment_provider": payment_provider,
                "provider_event_id": resolved_provider_event_id,
                "provider_event_type": resolved_provider_event_type,
                "provider_account_id": resolved_provider_account_id,
                "provider_invoice_id": resolved_provider_invoice_id,
                "stripe_event_id": stripe_event_id,
                "stripe_event_type": "invoice.paid" if stripe_event_id is not None else None,
                "stripe_account_id": stripe_account_id,
                "stripe_invoice_id": stripe_invoice_id,
                "invoice_id": None,
                "creator_id": creator_id,
                "booking_id": booking_id,
                "tid": tid,
                "status": "unmatched",
                "unattributed_reason": reason,
                "paid_at": paid_at,
                "received_at": paid_at,
                "processed_at": None,
            },
        )

    return payment_event_id


def _insert_calendly_event_record(
    *,
    tid: str,
    calendly_event_id: str,
    calendly_booking_uuid: str,
    processing_status: str,
) -> str:
    event_record_id = str(uuid.uuid4())
    received_at = datetime(2026, 3, 12, 14, 0, tzinfo=timezone.utc)
    processed_at = (
        None
        if processing_status == "received"
        else datetime(2026, 3, 12, 14, 5, tzinfo=timezone.utc)
    )

    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO calendly_webhook_events "
                "(id, calendly_event_id, provider_event_type, event_type, calendly_event_id_path, "
                "calendly_booking_uuid, calendly_booking_uuid_path, tid, tid_path, payload, reducer_key, "
                "delivery_count, processing_status, reducer_attempt_count, last_error, received_at, "
                "last_received_at, processed_at) "
                "VALUES "
                "(:id, :calendly_event_id, :provider_event_type, :event_type, :calendly_event_id_path, "
                ":calendly_booking_uuid, :calendly_booking_uuid_path, :tid, :tid_path, CAST(:payload AS JSONB), :reducer_key, "
                ":delivery_count, :processing_status, :reducer_attempt_count, :last_error, :received_at, "
                ":last_received_at, :processed_at)"
            ),
            {
                "id": event_record_id,
                "calendly_event_id": calendly_event_id,
                "provider_event_type": "invitee.created",
                "event_type": "booking.created",
                "calendly_event_id_path": "payload.event",
                "calendly_booking_uuid": calendly_booking_uuid,
                "calendly_booking_uuid_path": "payload.uri",
                "tid": tid,
                "tid_path": "payload.tracking.utm_content",
                "payload": json.dumps(
                    {
                        "event": "invitee.created",
                        "payload": {"tracking": {"utm_content": tid}},
                    }
                ),
                "reducer_key": f"booking:{calendly_booking_uuid}",
                "delivery_count": 1,
                "processing_status": processing_status,
                "reducer_attempt_count": 0 if processing_status == "received" else 1,
                "last_error": (
                    "RuntimeError: ui health test reducer failure"
                    if processing_status == "failed"
                    else None
                ),
                "received_at": received_at,
                "last_received_at": received_at,
                "processed_at": processed_at,
            },
        )

    return event_record_id


def _insert_blocked_billing_case(
    *,
    creator_id: str,
    booking_id: str,
    tid: str,
    calendly_booking_uuid: str,
    stripe_account_id: str | None,
    frozen_amount_cents: int,
    frozen_currency: str,
    reason_code: str,
    first_blocked_at: datetime,
    provider: str = "calendly",
    provider_booking_id: str | None = None,
    last_blocked_at: datetime | None = None,
    last_retry_at: datetime | None = None,
    status: str = "open",
    provider_operation: str | None = None,
    provider_http_status: int | None = None,
    provider_error_code: str | None = None,
    resolved_at: datetime | None = None,
    resolution_code: str | None = None,
) -> str:
    case_id = str(uuid.uuid4())

    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO blocked_billing_cases "
                "(id, creator_id, booking_id, invoice_id, tid, provider, provider_booking_id, calendly_booking_uuid, stripe_account_id, "
                "frozen_amount_cents, frozen_currency, status, reason_code, provider_operation, "
                "provider_http_status, provider_error_code, first_blocked_at, last_blocked_at, "
                "last_retry_at, resolved_at, resolution_code) "
                "VALUES "
                "(:id, :creator_id, :booking_id, :invoice_id, :tid, :provider, :provider_booking_id, :calendly_booking_uuid, :stripe_account_id, "
                ":frozen_amount_cents, :frozen_currency, :status, :reason_code, :provider_operation, "
                ":provider_http_status, :provider_error_code, :first_blocked_at, :last_blocked_at, "
                ":last_retry_at, :resolved_at, :resolution_code)"
            ),
            {
                "id": case_id,
                "creator_id": creator_id,
                "booking_id": booking_id,
                "invoice_id": None,
                "tid": tid,
                "provider": provider,
                "provider_booking_id": provider_booking_id or calendly_booking_uuid,
                "calendly_booking_uuid": calendly_booking_uuid,
                "stripe_account_id": stripe_account_id,
                "frozen_amount_cents": frozen_amount_cents,
                "frozen_currency": frozen_currency,
                "status": status,
                "reason_code": reason_code,
                "provider_operation": provider_operation,
                "provider_http_status": provider_http_status,
                "provider_error_code": provider_error_code,
                "first_blocked_at": first_blocked_at,
                "last_blocked_at": last_blocked_at or first_blocked_at,
                "last_retry_at": last_retry_at,
                "resolved_at": resolved_at,
                "resolution_code": resolution_code,
            },
        )

    return case_id


def _insert_matched_payment_event(
    *,
    creator_id: str,
    booking_id: str,
    tid: str,
    invoice_id: str,
    stripe_account_id: str | None = None,
    stripe_event_id: str | None = None,
    stripe_invoice_id: str | None = None,
    paid_at: datetime,
    status: str = "applied",
    payment_provider: str = "stripe",
    provider_account_id: str | None = None,
    provider_event_id: str | None = None,
    provider_event_type: str | None = None,
    provider_invoice_id: str | None = None,
) -> str:
    payment_event_id = str(uuid.uuid4())
    resolved_provider_account_id = (
        provider_account_id if provider_account_id is not None else stripe_account_id
    )
    resolved_provider_event_id = (
        provider_event_id if provider_event_id is not None else stripe_event_id
    )
    resolved_provider_event_type = (
        provider_event_type if provider_event_type is not None else "invoice.paid"
    )
    resolved_provider_invoice_id = (
        provider_invoice_id if provider_invoice_id is not None else stripe_invoice_id
    )

    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO invoice_payment_events "
                "(id, payment_provider, provider_event_id, provider_event_type, provider_account_id, provider_invoice_id, stripe_event_id, stripe_event_type, stripe_account_id, stripe_invoice_id, invoice_id, creator_id, booking_id, tid, status, unattributed_reason, paid_at, received_at, processed_at) "
                "VALUES "
                "(:id, :payment_provider, :provider_event_id, :provider_event_type, :provider_account_id, :provider_invoice_id, :stripe_event_id, :stripe_event_type, :stripe_account_id, :stripe_invoice_id, :invoice_id, :creator_id, :booking_id, :tid, :status, :unattributed_reason, :paid_at, :received_at, :processed_at)"
            ),
            {
                "id": payment_event_id,
                "payment_provider": payment_provider,
                "provider_event_id": resolved_provider_event_id,
                "provider_event_type": resolved_provider_event_type,
                "provider_account_id": resolved_provider_account_id,
                "provider_invoice_id": resolved_provider_invoice_id,
                "stripe_event_id": stripe_event_id,
                "stripe_event_type": "invoice.paid" if stripe_event_id is not None else None,
                "stripe_account_id": stripe_account_id,
                "stripe_invoice_id": stripe_invoice_id,
                "invoice_id": invoice_id,
                "creator_id": creator_id,
                "booking_id": booking_id,
                "tid": tid,
                "status": status,
                "unattributed_reason": None,
                "paid_at": paid_at,
                "received_at": paid_at,
                "processed_at": paid_at,
            },
        )

    return payment_event_id


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


class _StubStripeProvider:
    def __init__(
        self,
        *,
        account_id: str = "acct_ui_story38",
        readiness: StripeAccountReadiness | None = None,
        created_invoice_id: str = "in_ui_attention_created",
        created_invoice_status: str = "open",
        readiness_error: StripeProviderError | None = None,
        callback_error: StripeProviderError | None = None,
        create_error: StripeProviderError | None = None,
        void_error: StripeProviderError | None = None,
    ):
        self.account_id = account_id
        self._readiness = readiness or StripeAccountReadiness(charges_enabled=True)
        self._created_invoice_id = created_invoice_id
        self._created_invoice_status = created_invoice_status
        self._readiness_error = readiness_error
        self._callback_error = callback_error
        self._create_error = create_error
        self._void_error = void_error
        self.start_calls: list[dict[str, str]] = []
        self.callback_calls: list[dict[str, str]] = []
        self.readiness_calls: list[str] = []
        self.create_calls: list[dict[str, object]] = []
        self.void_calls: list[dict[str, str]] = []

    def build_connect_onboarding_url(self, *, creator_id: str, state: str) -> str:
        self.start_calls.append({"creator_id": creator_id, "state": state})
        return (
            "https://connect.stripe.com/oauth/authorize"
            f"?response_type=code&client_id=ca_story38_ui&state={state}&creator_id={creator_id}"
        )

    def exchange_connect_callback(self, *, code: str, state: str) -> str:
        self.callback_calls.append({"code": code, "state": state})
        if self._callback_error is not None:
            raise self._callback_error
        return self.account_id

    def get_account_readiness(self, *, stripe_account_id: str) -> StripeAccountReadiness:
        self.readiness_calls.append(stripe_account_id)
        if self._readiness_error is not None:
            raise self._readiness_error
        return self._readiness

    def create_invoice(
        self,
        *,
        stripe_account_id: str,
        amount_cents: int,
        currency: str,
        metadata: dict[str, str],
        idempotency_key: str,
    ) -> StripeInvoiceCreateResult:
        self.create_calls.append(
            {
                "stripe_account_id": stripe_account_id,
                "amount_cents": amount_cents,
                "currency": currency,
                "metadata": metadata,
                "idempotency_key": idempotency_key,
            }
        )
        if self._create_error is not None:
            raise self._create_error
        return StripeInvoiceCreateResult(
            stripe_invoice_id=self._created_invoice_id,
            status=self._created_invoice_status,
        )

    def void_invoice(self, *, stripe_account_id: str, stripe_invoice_id: str) -> None:
        self.void_calls.append(
            {
                "stripe_account_id": stripe_account_id,
                "stripe_invoice_id": stripe_invoice_id,
            }
        )
        if self._void_error is not None:
            raise self._void_error


class _StubPayPalProvider:
    billing_provider_name = "paypal"

    def __init__(
        self,
        *,
        readiness: BillingAccountReadiness,
        readiness_error: PayPalProviderError | None = None,
    ):
        self._readiness = readiness
        self._readiness_error = readiness_error
        self.readiness_calls: list[str] = []
        self.start_calls: list[dict[str, str]] = []

    def get_billing_account_readiness(
        self,
        *,
        provider_account_id: str,
    ) -> BillingAccountReadiness:
        self.readiness_calls.append(provider_account_id)
        if self._readiness_error is not None:
            raise self._readiness_error
        return self._readiness

    def create_connect_onboarding(
        self,
        *,
        tracking_id: str,
        return_url: str,
    ) -> PayPalConnectOnboardingResult:
        self.start_calls.append(
            {
                "tracking_id": tracking_id,
                "return_url": return_url,
            }
        )
        return PayPalConnectOnboardingResult(
            onboarding_url=(
                "https://www.sandbox.paypal.com/bizsignup/partner/entry"
                f"?tracking_id={tracking_id}"
            ),
            tracking_id=tracking_id,
        )

    def get_verified_seller_status(self, *, tracking_id: str):
        raise AssertionError(f"unexpected seller lookup tracking_id={tracking_id}")


class _StubOperatorExperimentDraftProvider:
    def __init__(
        self,
        *,
        configured: bool = True,
        model_name: str = "gpt-5.4-mini",
        prompt_version: str = "operator_draft_next_content_experiments.prompt.v1",
        cards: list[_OperatorExperimentDraftCardPayload] | None = None,
    ):
        self._configured = configured
        self._model_name = model_name
        self._prompt_version = prompt_version
        self._cards = cards or []
        self.calls = []

    def is_configured(self) -> bool:
        return self._configured

    def generate_draft(self, *, prompt_input):
        self.calls.append(prompt_input)
        if not self._configured:
            raise OperatorExperimentDraftUnavailableError("provider unavailable")
        return OperatorExperimentDraftProviderOutput(
            model_name=self._model_name,
            prompt_version=self._prompt_version,
            cards=self._cards,
        )


class _FailingEmailProvider:
    def __init__(self, *, error_text: str = "temporary outage"):
        self.error_text = error_text

    def send_magic_link(self, message) -> None:
        raise MagicLinkEmailDeliveryError(self.error_text)

    def send_support_request(self, message) -> None:
        raise SupportRequestEmailDeliveryError(self.error_text)


def test_sign_in_page_is_browser_accessible():
    with TestClient(app) as client:
        response = client.get("/sign-in", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<form" in response.text
    assert 'action="/sign-in"' in response.text
    assert 'name="email"' in response.text
    assert "Getting started" in response.text
    assert "Start or reopen your tutor workspace." in response.text
    assert "Enter your email to request a secure sign-in link for your workspace." in response.text
    assert "Send sign-in link" in response.text
    assert "open the email on this same device and browser where you requested it" in response.text
    assert "finish billing, booking-link, and tracked-link setup" not in response.text
    assert "Independent tutors create or reopen their workspace here" not in response.text
    assert 'href="/terms"' in response.text
    assert 'href="/privacy"' in response.text


def test_public_home_page_explains_tutor_onboarding_and_links_to_legal_pages():
    with TestClient(app) as client:
        response = client.get("/", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Know what's actually bringing in paid students." in response.text
    assert "Connect your booking links, content, and outreach to confirmed paid bookings" in response.text
    assert "Run your tutoring like a real business." in response.text
    assert "Connect your PayPal account." in response.text
    assert "Your PayPal stays yours." in response.text
    assert "Payouts go directly to you, never through us." in response.text
    assert "Quick answers before you get started." in response.text
    assert "Create your workspace" in response.text
    assert 'href="/sign-in"' in response.text
    assert 'href="/terms"' in response.text
    assert 'href="/privacy"' in response.text
    assert "How tutors register" not in response.text
    assert "Start with email sign-in" not in response.text
    assert "Pricing" not in response.text


def test_public_terms_and_privacy_pages_are_browser_accessible():
    with TestClient(app) as client:
        terms_response = client.get("/terms", headers=HTML_ACCEPT_HEADERS)
        privacy_response = client.get("/privacy", headers=HTML_ACCEPT_HEADERS)

    assert terms_response.status_code == 200
    assert "Career Code Pro Terms and Conditions" in terms_response.text
    assert "Independent tutors use their own connected payment-provider account" in terms_response.text
    assert privacy_response.status_code == 200
    assert "Career Code Pro Privacy Policy" in privacy_response.text
    assert "We store account email addresses, creator names, booking links" in privacy_response.text


def test_sign_in_page_invalid_link_notice_explains_how_to_recover():
    with TestClient(app) as client:
        response = client.get(
            "/sign-in",
            params={"status": "invalid-link"},
            headers=HTML_ACCEPT_HEADERS,
        )

    assert response.status_code == 200
    assert "That sign-in link is invalid or expired" in response.text
    assert "This usually means the link expired or it was opened on a different device or browser" in response.text
    assert "we will send a fresh link for this same device and browser." in response.text
    assert 'action="/sign-in"' in response.text


def test_sign_in_start_redirects_to_confirmation_without_echoing_email():
    email = f"ui_sign_in_{uuid.uuid4().hex}@example.com"

    with TestClient(app) as client:
        response = client.post(
            "/sign-in",
            data={"email": email},
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )
        confirmation_response = client.get(
            response.headers["location"],
            headers=HTML_ACCEPT_HEADERS,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/sign-in?status=sent"
    assert email not in response.headers["location"]
    assert confirmation_response.status_code == 200
    assert "Check your inbox" in confirmation_response.text
    assert "Open it on this same device and browser." in confirmation_response.text
    assert "If you opened the email somewhere else or the link expires" in confirmation_response.text
    assert _latest_magic_link_token_for_email(email)
    assert _auth_state_for_email(email) == {"auth_users": 0, "creators": 0, "pending": 1}


def test_sign_in_start_provider_failure_redirects_to_retry_without_echoing_provider_details():
    email = f"ui_sign_in_retry_{uuid.uuid4().hex}@example.com"
    provider = _FailingEmailProvider(error_text="smtp timeout from sandbox provider")

    with _override_app_state("email_provider", provider):
        with TestClient(app) as client:
            response = client.post(
                "/sign-in",
                data={"email": email},
                headers=HTML_ACCEPT_HEADERS,
                follow_redirects=False,
            )

    assert response.status_code == 303
    assert response.headers["location"] == "/sign-in?status=retry"
    assert email not in response.headers["location"]
    assert "smtp timeout" not in response.headers["location"]


def test_app_shell_redirects_unauthenticated_browser_requests():
    with TestClient(app) as client:
        response = client.get("/app", headers=HTML_ACCEPT_HEADERS, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/sign-in"


def test_booking_links_page_redirects_unauthenticated_browser_requests():
    with TestClient(app) as client:
        response = client.get(
            "/app/booking-links",
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/sign-in"


def test_content_page_redirects_unauthenticated_browser_requests():
    with TestClient(app) as client:
        response = client.get(
            "/app/content",
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/sign-in"


def test_booking_activity_page_redirects_unauthenticated_browser_requests():
    with TestClient(app) as client:
        response = client.get(
            "/app/bookings",
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/sign-in"


def test_reports_page_redirects_unauthenticated_browser_requests():
    with TestClient(app) as client:
        response = client.get(
            "/app/reports",
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/sign-in"


def test_experiments_page_redirects_unauthenticated_browser_requests():
    with TestClient(app) as client:
        response = client.get(
            "/app/experiments",
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/sign-in"


def test_growth_loop_page_redirects_unauthenticated_browser_requests_when_enabled():
    with _override_app_state("settings", _growth_loop_settings(enabled=True)):
        with TestClient(app) as client:
            response = client.get(
                "/app/growth-loop",
                headers=HTML_ACCEPT_HEADERS,
                follow_redirects=False,
            )

    assert response.status_code == 303
    assert response.headers["location"] == "/sign-in"


def test_experiment_card_page_redirects_unauthenticated_browser_requests():
    with TestClient(app) as client:
        response = client.get(
            f"/app/experiments/{uuid.uuid4()}/cards/1",
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/sign-in"


def test_attention_page_redirects_unauthenticated_browser_requests():
    with TestClient(app) as client:
        response = client.get(
            "/app/attention",
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/sign-in"


def test_health_page_redirects_unauthenticated_browser_requests():
    with TestClient(app) as client:
        response = client.get(
            "/app/health",
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/sign-in"


def test_account_page_redirects_unauthenticated_browser_requests():
    with TestClient(app) as client:
        response = client.get(
            "/app/account",
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/sign-in"


def test_workspace_reset_request_route_redirects_unauthenticated_browser_requests():
    with TestClient(app) as client:
        response = client.post(
            "/app/account/requests/workspace-reset",
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/sign-in"


def test_browser_magic_link_verify_sets_session_cookie_and_lands_in_app_shell():
    email = f"ui_shell_{uuid.uuid4().hex}@example.com"

    with TestClient(app) as client:
        start_response = client.post(
            "/sign-in",
            data={"email": email},
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )
        raw_token = _latest_magic_link_token_for_email(email)
        assert _auth_state_for_email(email) == {"auth_users": 0, "creators": 0, "pending": 1}

        verify_response = client.get(
            "/auth/magic-link/verify",
            params={"token": raw_token},
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )
        shell_response = client.get("/app", headers=HTML_ACCEPT_HEADERS)

    assert start_response.status_code == 303
    assert verify_response.status_code == 303
    assert verify_response.headers["location"] == "/app"
    assert raw_token not in verify_response.headers["location"]
    assert raw_token not in verify_response.text
    assert f"{SESSION_COOKIE_NAME}=" in verify_response.headers["set-cookie"]
    assert raw_token not in verify_response.headers["set-cookie"]

    assert shell_response.status_code == 200
    assert shell_response.headers["content-type"].startswith("text/html")
    assert "Setup Home" in shell_response.text
    assert "Creator Home" in shell_response.text
    assert email in shell_response.text
    assert raw_token not in shell_response.text
    assert _auth_state_for_email(email) == {"auth_users": 1, "creators": 1, "pending": 1}


def test_growth_loop_page_is_disabled_by_default_for_signed_in_creator():
    inserted = _insert_creator_user(
        email=f"ui_growth_loop_disabled_{uuid.uuid4().hex}@example.com",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_growth_loop_disabled",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    with _override_app_state("settings", _growth_loop_settings(enabled=False)):
        with TestClient(app) as client:
            client.cookies.set(SESSION_COOKIE_NAME, access_token)
            home_response = client.get("/app", headers=HTML_ACCEPT_HEADERS)
            growth_response = client.get("/app/growth-loop", headers=HTML_ACCEPT_HEADERS)

    assert home_response.status_code == 200
    assert 'href="/app/growth-loop"' not in home_response.text
    assert "Growth Loop" not in home_response.text
    assert growth_response.status_code == 404


def test_growth_loop_page_hides_reviewable_recovery_brief_without_paid_result():
    inserted = _insert_creator_user(
        email=f"ui_growth_loop_unpaid_{uuid.uuid4().hex}@example.com",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_growth_loop_unpaid",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    with _override_app_state("settings", _growth_loop_settings(enabled=True)):
        with TestClient(app) as client:
            client.cookies.set(SESSION_COOKIE_NAME, access_token)
            response = client.get("/app/growth-loop", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Growth Loop Agent" in response.text
    assert "Live Loomi schema proof" in response.text
    assert "Judge demo cockpit" not in response.text
    assert "Signal -> Proof -> Action" not in response.text
    assert "Agent console" not in response.text
    assert "View review packet" not in response.text
    assert "Review packet" not in response.text
    assert "Reviewable recovery brief" not in response.text
    assert "Copy-ready recovery brief" not in response.text
    assert "Decision trace" not in response.text
    assert "Rule-backed decision trace" not in response.text
    assert "Bloomreach-ready segment recipe" not in response.text
    assert "Measurement plan" not in response.text
    assert "Sandbox proof" not in response.text


def test_growth_loop_page_renders_enabled_paid_result_evidence_boundary():
    inserted = _insert_creator_user(
        email=f"ui_growth_loop_paid_{uuid.uuid4().hex}@example.com",
        name="Growth Loop Paid Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_growth_loop_paid",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="Growth Loop Call",
        calendly_url="https://calendly.com/example/growth-loop",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    tid = f"uigrowthloop{uuid.uuid4().hex[:8]}"
    _insert_content(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/growth-loop",
        tid=tid,
    )
    booking_id = _insert_booking(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        tid=tid,
        calendly_booking_uuid=f"BOOK_GROWTH_LOOP_{uuid.uuid4().hex[:8]}",
        booked_at=datetime.now(timezone.utc),
    )
    invoice_id = _insert_invoice(
        creator_id=inserted["creator_id"],
        booking_id=booking_id,
        tid=tid,
        stripe_account_id="acct_ui_growth_loop_paid",
        stripe_invoice_id=f"in_growth_loop_{uuid.uuid4().hex[:8]}",
        amount_cents=19500,
        paid_at=datetime.now(timezone.utc),
    )
    _insert_matched_payment_event(
        creator_id=inserted["creator_id"],
        booking_id=booking_id,
        tid=tid,
        invoice_id=invoice_id,
        stripe_account_id="acct_ui_growth_loop_paid",
        stripe_event_id=f"evt_growth_loop_{uuid.uuid4().hex[:8]}",
        stripe_invoice_id=f"in_growth_loop_{uuid.uuid4().hex[:8]}",
        paid_at=datetime.now(timezone.utc),
    )

    with _override_app_state("settings", _growth_loop_settings(enabled=True)):
        with TestClient(app) as client:
            client.cookies.set(SESSION_COOKIE_NAME, access_token)
            home_response = client.get("/app", headers=HTML_ACCEPT_HEADERS)
            growth_response = client.get("/app/growth-loop", headers=HTML_ACCEPT_HEADERS)

    assert home_response.status_code == 200
    assert 'href="/app/growth-loop" class="nav-link">Growth Loop</a>' in home_response.text
    assert growth_response.status_code == 200
    assert '<a href="/app/growth-loop" class="nav-link active" aria-current="page">Growth Loop</a>' in growth_response.text
    assert "Growth Loop Agent" in growth_response.text
    assert "Judge demo cockpit" in growth_response.text
    assert "Agent console" in growth_response.text
    assert "90-second judge path" in growth_response.text
    assert "Signal -> Proof -> Action" in growth_response.text
    assert "Bloomreach/Loomi signal" in growth_response.text
    assert "cart, checkout, purchase, campaign, and retargeting fields" in growth_response.text
    assert "Sandbox proof" in growth_response.text
    assert "Story 137 passed" in growth_response.text
    assert "Pacific Apparel shopping context" in growth_response.text
    assert "sleepy-goose activation and measurement" in growth_response.text
    assert "Storefront shopping context" in growth_response.text
    assert "Engagement event and activation surfaces" in growth_response.text
    assert "app-owned invoices and payments remain paid truth" in growth_response.text
    assert "Review-ready action" in growth_response.text
    assert "Measurement boundary" in growth_response.text
    assert "Hold out first, measure app-owned paid revenue later" in growth_response.text
    assert "Evidence appendix" in growth_response.text
    assert "Full proof, recipes, and boundaries" in growth_response.text
    assert "Full proof stack for reviewers" in growth_response.text
    assert (
        '<details id="growth-loop-evidence-appendix" class="growth-loop-detail evidence-appendix">'
        in growth_response.text
    )
    assert '<details id="growth-loop-sandbox" class="growth-loop-detail">' in growth_response.text
    assert "Engagement + Storefront proof" in growth_response.text
    assert "Handbags product grid" in growth_response.text
    assert "campaigns, analyses, segmentations, reports, funnels" in growth_response.text
    assert "sandbox observations do not count revenue" in growth_response.text
    assert "No live Engagement or Storefront call is made by this page." in growth_response.text
    assert "No customer data, screenshots, raw event payloads, or private URLs are embedded." in growth_response.text
    assert "No campaign, report, checkout, payment, export, or Storefront mutation is performed by this page." in growth_response.text
    assert "No lift, causality, or new paid-truth source is claimed." in growth_response.text
    assert "View review packet" in growth_response.text
    assert "Guided agent workflow" in growth_response.text
    assert "Run agent" in growth_response.text
    assert "Step 1 of 6" in growth_response.text
    assert "Run next step" in growth_response.text
    assert "Inspect paid proof" in growth_response.text
    assert "Read Loomi schema evidence" in growth_response.text
    assert "Score candidate actions" in growth_response.text
    assert "Prepare recovery brief" in growth_response.text
    assert "Generate segment recipe" in growth_response.text
    assert "Attach measurement plan" in growth_response.text
    assert "Review packet assembled" in growth_response.text
    assert "not for automatic send, export, or mutation" in growth_response.text
    assert "data-agent-run" in growth_response.text
    assert "data-agent-run-next" in growth_response.text
    assert 'href="#growth-loop-review-packet"' in growth_response.text
    assert 'href="#growth-loop-proof"' in growth_response.text
    assert 'href="#growth-loop-sandbox"' in growth_response.text
    assert 'href="#growth-loop-action"' in growth_response.text
    assert 'href="#growth-loop-decision"' in growth_response.text
    assert 'href="#growth-loop-segment"' in growth_response.text
    assert 'href="#growth-loop-measure"' in growth_response.text
    assert 'href="#growth-loop-boundaries"' in growth_response.text
    assert "Proof" in growth_response.text
    assert "Schema" in growth_response.text
    assert "Action" in growth_response.text
    assert "Segment" in growth_response.text
    assert "Measure" in growth_response.text
    assert "Canonical invoice and payment records decide revenue." in growth_response.text
    assert "Review packet" in growth_response.text
    assert "Review-ready artifact" in growth_response.text
    assert "Proof chain" in growth_response.text
    assert "Boundaries" in growth_response.text
    assert "App-owned invoice and payment records remain paid truth." in growth_response.text
    assert "Cross-system demo map" in growth_response.text
    assert "Loomi context, app attribution, and PayPal proof meet here" in growth_response.text
    assert "Marketing and Analytics MCP-shaped diagnostics" in growth_response.text
    assert "fixture-backed and shaped after authenticated Marketing and Analytics MCP tool families" in growth_response.text
    assert "Tracked content, creator-scoped bookings, and canonical paid invoices decide what counted" in growth_response.text
    assert "PayPal-shaped order and capture evidence" in growth_response.text
    assert "does not send campaigns, mutate external systems, or replace reporting totals" in growth_response.text
    assert "Live Loomi schema proof" in growth_response.text
    assert "Verified via Cursor MCP" in growth_response.text
    assert "Cart-abandon recover &amp; convert" in growth_response.text
    assert "sleepy-goose" in growth_response.text
    assert "b15c09b0-5469-11f1-b333-862b79b06b65" in growth_response.text
    assert "Booking-step recovery analogue" in growth_response.text
    assert "Required segment definition" in growth_response.text
    assert "Evidence that would prove it worked" in growth_response.text
    assert "cart_update.total_quantity" in growth_response.text
    assert "purchase.purchase_status" in growth_response.text
    assert "campaign.status" in growth_response.text
    assert "retargeting.action" in growth_response.text
    assert "not a live page-load MCP call" in growth_response.text
    assert "does not count revenue, prove causality, or replace app-owned booking" in growth_response.text
    assert "Reviewable recovery brief" in growth_response.text
    assert "Target segment" in growth_response.text
    assert "Message outline" in growth_response.text
    assert "Draft Bloomreach segment spec" in growth_response.text
    assert "Success evidence" in growth_response.text
    assert "Diagnostic signals" in growth_response.text
    assert "Static copy-ready block" in growth_response.text
    assert "Copy-ready recovery brief" in growth_response.text
    assert "Copy review brief" in growth_response.text
    assert "Copies review text only; it does not send, export, or mutate Bloomreach." in growth_response.text
    assert "app-owned paid conversion lift" in growth_response.text
    assert "stored booking, invoice, and payment-backed records" in growth_response.text
    assert "campaign.status" in growth_response.text
    assert "retargeting.audience" in growth_response.text
    assert "does not mutate Bloomreach" in growth_response.text
    assert "saved segment, campaign, or recommendation" in growth_response.text
    assert "Rule-backed decision trace" in growth_response.text
    assert "Decision trace" in growth_response.text
    assert "No autonomous execution" in growth_response.text
    assert "no live LLM call is required" in growth_response.text
    assert "no campaign is sent" in growth_response.text
    assert "no Bloomreach object is mutated" in growth_response.text
    assert "Booking-step recovery brief" in growth_response.text
    assert "Broad nurture follow-up" in growth_response.text
    assert "Direct Bloomreach segment or campaign mutation" in growth_response.text
    assert "9/10" in growth_response.text
    assert "6/10" in growth_response.text
    assert "3/10" in growth_response.text
    assert "Schema fit" in growth_response.text
    assert "App evidence fit" in growth_response.text
    assert "Review safety" in growth_response.text
    assert "Evidence chain" in growth_response.text
    assert "Bloomreach-ready segment recipe" in growth_response.text
    assert "Manual Bloomreach recreation" in growth_response.text
    assert "Review-only recipe" in growth_response.text
    assert "Include" in growth_response.text
    assert "Exclude" in growth_response.text
    assert "24-hour recovery window" in growth_response.text
    assert "Message variables" in growth_response.text
    assert "Measure" in growth_response.text
    assert "Conversation MCP note" in growth_response.text
    assert "Review boundary" in growth_response.text
    assert "cart_update" in growth_response.text
    assert "total_quantity is greater than zero" in growth_response.text
    assert "completed purchase event" in growth_response.text
    assert "app-owned paid invoices and payment-backed records" in growth_response.text
    assert "holdout or non-targeted group" in growth_response.text
    assert "catalog-proxy signals" in growth_response.text
    assert "support, checkout, booking, refund, or payment-failure telemetry" in growth_response.text
    assert "does not create a saved Bloomreach segment" in growth_response.text
    assert "No campaign is sent and no external system is mutated" in growth_response.text
    assert "Measurement plan" in growth_response.text
    assert "Holdout-first measurement" in growth_response.text
    assert "No lift yet" in growth_response.text
    assert "Primary metric" in growth_response.text
    assert "Paid revenue" in growth_response.text
    assert "app-owned paid invoices and payment-backed records" in growth_response.text
    assert "Supporting metric" in growth_response.text
    assert "Paid conversion rate" in growth_response.text
    assert "Comparison design" in growth_response.text
    assert "Withheld holdout first" in growth_response.text
    assert "non-targeted comparison" in growth_response.text
    assert "Timing" in growth_response.text
    assert "24h send, 7d observe" in growth_response.text
    assert "within 24 hours" in growth_response.text
    assert "for 7 days" in growth_response.text
    assert "Diagnostic context" in growth_response.text
    assert "Engagement is not revenue" in growth_response.text
    assert "campaign.status" in growth_response.text
    assert "retargeting.audience" in growth_response.text
    assert "does not report measured lift or causal impact" in growth_response.text
    assert "canonical invoice and payment records remain paid truth" in growth_response.text
    assert "Paid proof exists; choose the next reviewed action." in growth_response.text
    assert "App-owned evidence stays separate from Loomi diagnostics" in growth_response.text
    assert "1 content item" in growth_response.text
    assert "1 booking" in growth_response.text
    assert "1 paid invoice" in growth_response.text
    assert "$195.00" in growth_response.text
    assert "Loomi fixture diagnostics" in growth_response.text
    assert "Loomi fixture fallback" in growth_response.text
    assert "fixture diagnostics are shown" in growth_response.text
    assert "does not become paid truth" in growth_response.text
    assert "Human review" in growth_response.text
    assert "caused revenue" not in growth_response.text.lower()
    assert "causal lift" not in growth_response.text.lower()
    assert "live loomi runtime call" not in growth_response.text.lower()


def test_growth_loop_page_renders_live_loomi_status_when_provider_succeeds():
    inserted = _insert_creator_user(
        email=f"ui_growth_loop_live_{uuid.uuid4().hex}@example.com",
        name="Growth Loop Live Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_growth_loop_live",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    with _override_app_state("settings", _growth_loop_settings(enabled=True, live_loomi_enabled=True)):
        with _override_app_state("growth_loop_loomi_provider", _FakeLiveLoomiProvider()):
            with TestClient(app) as client:
                client.cookies.set(SESSION_COOKIE_NAME, access_token)
                response = client.get("/app/growth-loop", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Loomi live MCP diagnostics" in response.text
    assert "Loomi live MCP" in response.text
    assert "Live Loomi MCP responded through the configured runtime provider." in response.text
    assert "Live high-intent booked prospects" in response.text
    assert "Review a live Loomi recommendation before sending." in response.text
    assert "Pacific Tutors live overview" in response.text
    assert "Loomi MCP results are diagnostic context only." in response.text
    assert "ui-test-token" not in response.text
    assert "caused revenue" not in response.text.lower()
    assert "causal lift" not in response.text.lower()


def test_growth_loop_page_renders_recorded_bloomreach_saved_segment_proof():
    inserted = _insert_creator_user(
        email=f"ui_growth_loop_segment_{uuid.uuid4().hex}@example.com",
        name="Growth Loop Segment Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_growth_loop_segment",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="Growth Loop Segment Call",
        calendly_url="https://calendly.com/example/growth-loop-segment",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    tid = f"uigrowthsegment{uuid.uuid4().hex[:8]}"
    _insert_content(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/growth-loop-segment",
        tid=tid,
    )
    booking_id = _insert_booking(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        tid=tid,
        calendly_booking_uuid=f"BOOK_GROWTH_SEGMENT_{uuid.uuid4().hex[:8]}",
        booked_at=datetime.now(timezone.utc),
    )
    invoice_id = _insert_invoice(
        creator_id=inserted["creator_id"],
        booking_id=booking_id,
        tid=tid,
        stripe_account_id="acct_ui_growth_loop_segment",
        stripe_invoice_id=f"in_growth_segment_{uuid.uuid4().hex[:8]}",
        amount_cents=19500,
        paid_at=datetime.now(timezone.utc),
    )
    _insert_matched_payment_event(
        creator_id=inserted["creator_id"],
        booking_id=booking_id,
        tid=tid,
        invoice_id=invoice_id,
        stripe_account_id="acct_ui_growth_loop_segment",
        stripe_event_id=f"evt_growth_segment_{uuid.uuid4().hex[:8]}",
        stripe_invoice_id=f"in_growth_segment_{uuid.uuid4().hex[:8]}",
        paid_at=datetime.now(timezone.utc),
    )

    settings = _growth_loop_settings(
        enabled=True,
        bloomreach_segment_proof_enabled=True,
    )
    with _override_app_state("settings", settings):
        with TestClient(app) as client:
            client.cookies.set(SESSION_COOKIE_NAME, access_token)
            response = client.get("/app/growth-loop", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Bloomreach saved segment proof" in response.text
    assert "Real Bloomreach object proof" in response.text
    assert "Real sandbox object proof" in response.text
    assert "CCP Cart Recovery Demo" in response.text
    assert "seg_ui_growth_loop_demo" in response.text
    assert "Created in Engagement UI" in response.text
    assert "Object ID" in response.text
    assert "Saved segment created in sleepy-goose / Hackathon Workspace through Bloomreach Engagement UI." in response.text
    assert "Inspect saved segment proof" in response.text
    assert 'href="#growth-loop-bloomreach-object"' in response.text
    assert '<details id="growth-loop-bloomreach-object" class="growth-loop-detail">' in response.text
    assert "Recorded saved segment proof remains review-only." in response.text
    assert "No Bloomreach object is created or changed by this page load." in response.text
    assert "This page does not create, update, or delete Bloomreach objects on load." in response.text
    assert "The created segment does not count revenue or prove lift/causality." in response.text
    assert "Direct in-app Bloomreach mutation" in response.text
    assert "No campaign or additional saved object is created by this page." in response.text
    assert "Bloomreach Engagement UI" in response.text
    assert "app-owned invoice and payment records remain paid truth" in response.text.lower()
    assert "caused revenue" not in response.text.lower()
    assert "causal lift" not in response.text.lower()


def test_browser_magic_link_verify_failure_redirects_without_echoing_token():
    raw_token = "invalid_browser_magic_link_token"

    with TestClient(app) as client:
        response = client.get(
            "/auth/magic-link/verify",
            params={"token": raw_token},
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )
        invalid_link_page = client.get(
            response.headers["location"],
            headers=HTML_ACCEPT_HEADERS,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/sign-in?status=invalid-link"
    assert raw_token not in response.headers["location"]
    assert raw_token not in response.text
    assert invalid_link_page.status_code == 200
    assert "different device or browser than the one where sign-in started" in invalid_link_page.text


def test_setup_home_pending_billing_state_keeps_provider_choice_for_allowlisted_operator():
    inserted = _insert_creator_user(
        email=f"ui_pending_{uuid.uuid4().hex}@example.com",
        name="Pending Creator",
        stripe_connect_status="pending",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    settings = _paypal_operator_only_settings(inserted["email"], environment="sandbox")

    with _override_app_state("settings", settings):
        with TestClient(app) as client:
            client.cookies.set(SESSION_COOKIE_NAME, access_token)
            response = client.get("/app", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Setup Home" in response.text
    assert "0 of 4 setup milestones done" in response.text
    assert "Your path to first paid proof" in response.text
    assert "Current milestone" not in response.text
    assert "Connect billing provider" in response.text
    assert "What do I need to do first?" in response.text
    assert "No provider is connected yet" in response.text
    assert "Complete the few setup steps that let booking links turn into trackable paid proof." in response.text
    assert "Connect the account that will receive payments for your tutoring services." in response.text
    assert 'class="checklist-item todo active"' in response.text
    assert 'class="checklist-item todo locked"' in response.text
    assert 'class="checklist-item next locked"' in response.text
    assert "Why this matters" in response.text
    assert "Your billing provider lets the workspace create invoices when tracked bookings arrive." in response.text
    assert "Start Stripe setup" in response.text
    assert 'action="/app/stripe/connect/start"' in response.text
    assert "Start PayPal setup" in response.text
    assert 'action="/app/paypal/connect/start"' in response.text
    assert "Save a booking link" in response.text
    assert 'href="/app/booking-links"' in response.text
    assert "Add billing defaults" in response.text
    assert "Create a tracked link" in response.text
    assert 'href="/app/content"' in response.text
    assert "Choose Stripe or PayPal to start billing setup." not in response.text
    assert "Milestone path" not in response.text
    assert 'href="/app/reports"' in response.text
    assert 'class="wrap-anywhere"' in response.text
    assert "Blocked billing and unresolved payments will appear on" in response.text


def test_setup_home_pending_billing_state_hides_paypal_choice_for_non_operator_creator():
    inserted = _insert_creator_user(
        email=f"ui_pending_hidden_{uuid.uuid4().hex}@example.com",
        name="Pending Hidden Creator",
        stripe_connect_status="pending",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    settings = _paypal_operator_only_settings("ops@creatortrust.co", environment="sandbox")

    with _override_app_state("settings", settings):
        with TestClient(app) as client:
            client.cookies.set(SESSION_COOKIE_NAME, access_token)
            response = client.get("/app", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Connect billing provider" in response.text
    assert "Connect Stripe to start billing setup. PayPal setup is not yet available for general creators." in response.text
    assert "Stripe is the available billing provider for this workspace right now." in response.text
    assert "Start Stripe setup" in response.text
    assert 'action="/app/stripe/connect/start"' in response.text
    assert "Start PayPal setup" not in response.text
    assert 'action="/app/paypal/connect/start"' not in response.text
    assert "Choose Stripe or PayPal to start billing setup." not in response.text


def test_setup_home_pending_billing_state_hides_paypal_choice_for_non_operator_live_creator():
    inserted = _insert_creator_user(
        email=f"ui_pending_live_{uuid.uuid4().hex}@example.com",
        name="Pending Live Creator",
        stripe_connect_status="pending",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    settings = _live_paypal_operator_only_settings("ops@creatortrust.co")

    with _override_app_state("settings", settings):
        with TestClient(app) as client:
            client.cookies.set(SESSION_COOKIE_NAME, access_token)
            response = client.get("/app", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Connect billing provider" in response.text
    assert "Connect Stripe to start billing setup. PayPal setup is not yet available for general creators." in response.text
    assert "Stripe is the available billing provider for this workspace right now." in response.text
    assert "Start Stripe setup" in response.text
    assert 'action="/app/stripe/connect/start"' in response.text
    assert "Start PayPal setup" not in response.text
    assert 'action="/app/paypal/connect/start"' not in response.text
    assert "Choose Stripe or PayPal to start billing setup." not in response.text


def test_setup_home_pending_billing_state_keeps_paypal_choice_for_allowlisted_live_operator():
    inserted = _insert_creator_user(
        email=f"ui_pending_live_operator_{uuid.uuid4().hex}@example.com",
        name="Pending Live Operator",
        stripe_connect_status="pending",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    settings = _live_paypal_operator_only_settings(inserted["email"])

    with _override_app_state("settings", settings):
        with TestClient(app) as client:
            client.cookies.set(SESSION_COOKIE_NAME, access_token)
            response = client.get("/app", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Connect the account that will receive payments for your tutoring services." in response.text
    assert "Your billing provider lets the workspace create invoices when tracked bookings arrive." in response.text
    assert "Start Stripe setup" in response.text
    assert "Start PayPal setup" in response.text
    assert 'action="/app/paypal/connect/start"' in response.text


def test_setup_home_missing_billing_defaults_state_shows_blocked_next_action():
    inserted = _insert_creator_user(
        email=f"ui_missing_defaults_{uuid.uuid4().hex}@example.com",
        name="Missing Defaults Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_missing_defaults",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="Missing Defaults Call",
        calendly_url="https://calendly.com/example/missing-defaults",
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get("/app", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "2 of 4 setup milestones done" in response.text
    assert "Your path to first paid proof" in response.text
    assert 'class="checklist-item todo active"' in response.text
    assert 'class="checklist-item next locked"' in response.text
    assert "Connected, but not billable now" in response.text
    assert (
        "A billing provider is connected, but this workspace still needs amount and currency on at least one saved booking link."
        in response.text
    )
    assert "Add billing defaults" in response.text
    assert (
        "The workspace has moved beyond connection. The remaining gap is making one saved link usable for creator billing."
        in response.text
    )
    assert "Add billing defaults" in response.text
    assert "Why this matters" in response.text
    assert "Milestone path" not in response.text
    assert 'href="/app/booking-links"' in response.text


def test_account_page_pending_billing_state_keeps_provider_choice_for_allowlisted_operator():
    inserted = _insert_creator_user(
        email=f"ui_account_pending_{uuid.uuid4().hex}@example.com",
        name="Pending Account Creator",
        stripe_connect_status="pending",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    settings = _paypal_operator_only_settings(inserted["email"], environment="sandbox")

    with _override_app_state("settings", settings):
        with TestClient(app) as client:
            client.cookies.set(SESSION_COOKIE_NAME, access_token)
            response = client.get("/app/account", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Billing connection" in response.text
    assert "Choose Stripe or PayPal here when you are ready." in response.text
    assert "No billing provider is preselected for this workspace." in response.text
    assert "Start Stripe setup" in response.text
    assert 'action="/app/stripe/connect/start"' in response.text
    assert "Start PayPal setup" in response.text
    assert 'action="/app/paypal/connect/start"' in response.text


def test_account_page_pending_billing_state_hides_paypal_choice_for_non_operator_creator():
    inserted = _insert_creator_user(
        email=f"ui_account_pending_hidden_{uuid.uuid4().hex}@example.com",
        name="Pending Hidden Account Creator",
        stripe_connect_status="pending",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    settings = _paypal_operator_only_settings("ops@creatortrust.co", environment="sandbox")

    with _override_app_state("settings", settings):
        with TestClient(app) as client:
            client.cookies.set(SESSION_COOKIE_NAME, access_token)
            response = client.get("/app/account", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Billing connection" in response.text
    assert "Choose Stripe here when you are ready." in response.text
    assert "Start Stripe setup" in response.text
    assert 'action="/app/stripe/connect/start"' in response.text
    assert "Start PayPal setup" not in response.text
    assert 'action="/app/paypal/connect/start"' not in response.text


def test_setup_and_account_pages_reuse_connected_but_not_billable_now_vocabulary():
    inserted = _insert_creator_user(
        email=f"ui_connected_not_billable_{uuid.uuid4().hex}@example.com",
        name="Connected Not Billable Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_connected_not_billable",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="Connected Not Billable Call",
        calendly_url="https://calendly.com/example/connected-not-billable",
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        setup_response = client.get("/app", headers=HTML_ACCEPT_HEADERS)
        account_response = client.get("/app/account", headers=HTML_ACCEPT_HEADERS)

    assert setup_response.status_code == 200
    assert account_response.status_code == 200
    assert "Connected, but not billable now" in setup_response.text
    assert "Connected, but not billable now" in account_response.text
    assert "What is keeping this workspace from becoming billable now?" in setup_response.text
    assert (
        "The workspace has moved beyond connection. The remaining gap is making one saved link usable for creator billing."
        in setup_response.text
    )
    assert "Add billing defaults" in setup_response.text
    assert "Why this matters" in setup_response.text
    assert "Connected</strong>: Done. A billing provider is connected to this workspace." in account_response.text
    assert "Billable now</strong>: Not yet. Add amount and currency to at least one saved booking link." in account_response.text
    assert "Ready to track</strong>: Not yet. This milestone starts after the workspace is billable now." in account_response.text


def test_setup_and_account_pages_keep_fullscope_sources_out_of_billable_now():
    inserted = _insert_creator_user(
        email=f"ui_fullscope_setup_only_{uuid.uuid4().hex}@example.com",
        name="FullScope Setup Only Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_fullscope_setup_only",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="FS1 Personal Calendar",
        provider="fullscope",
        destination_url="https://links.fullscope.tools/widget/bookings/fs1-personal-calendar",
        billing_amount_cents=15000,
        billing_currency="USD",
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        setup_response = client.get("/app", headers=HTML_ACCEPT_HEADERS)
        account_response = client.get("/app/account", headers=HTML_ACCEPT_HEADERS)

    assert setup_response.status_code == 200
    assert account_response.status_code == 200
    assert "Connected, but not billable now" in setup_response.text
    assert "Connected, but not billable now" in account_response.text
    assert (
        "Saved booking sources are not active for creator-tracked workflows right now, so this workspace is still not billable now."
        in setup_response.text
    )
    assert "Saved booking sources are not active for creator-tracked workflows right now. Add a currently supported booking link." in account_response.text
    assert "The workspace has moved beyond connection. The remaining gap is making one saved link usable for creator billing." in setup_response.text
    assert "Those booking sources stay saved, but they are not active in creator-tracked workflows right now." in account_response.text


def test_setup_and_account_pages_show_paypal_not_ready_truth_and_offer_stripe_switch():
    inserted = _insert_creator_user(
        email=f"ui_paypal_not_ready_{uuid.uuid4().hex}@example.com",
        name="PayPal Not Ready Creator",
        stripe_connect_status="pending",
        stripe_account_id=None,
        billing_provider="paypal",
        billing_connect_status="connected",
        billing_account_id="merchant_ui_paypal_not_ready",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="PayPal Not Ready Call",
        calendly_url="https://calendly.com/example/paypal-not-ready",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    provider = _StubPayPalProvider(
        readiness=BillingAccountReadiness(
            can_create_invoices=False,
            creator_actionable_issue_codes=(
                BILLING_ACCOUNT_READINESS_ISSUE_CONFIRM_PAYPAL_PRIMARY_EMAIL,
                BILLING_ACCOUNT_READINESS_ISSUE_ENABLE_PAYPAL_PAYMENTS_RECEIVABLE,
            ),
        )
    )

    with _override_app_state("paypal_provider", provider):
        with TestClient(app) as client:
            client.cookies.set(SESSION_COOKIE_NAME, access_token)
            setup_response = client.get("/app", headers=HTML_ACCEPT_HEADERS)
            account_response = client.get("/app/account", headers=HTML_ACCEPT_HEADERS)

    assert setup_response.status_code == 200
    assert account_response.status_code == 200
    assert (
        "PayPal is connected, but it still needs this setup work before it can create invoices: confirm the primary email on the connected PayPal business account and finish the PayPal payments-receivable setup."
        in setup_response.text
    )
    assert (
        "PayPal is connected, but it still needs this setup work before it can create invoices: confirm the primary email on the connected PayPal business account and finish the PayPal payments-receivable setup."
        in account_response.text
    )
    assert "Start Stripe switch" in account_response.text
    assert 'action="/app/stripe/connect/start"' in account_response.text
    assert provider.readiness_calls == ["merchant_ui_paypal_not_ready", "merchant_ui_paypal_not_ready"]


def test_setup_and_account_pages_collapse_paypal_readiness_failures_into_blocked_state():
    inserted = _insert_creator_user(
        email=f"ui_paypal_blocked_{uuid.uuid4().hex}@example.com",
        name="PayPal Blocked Creator",
        stripe_connect_status="pending",
        stripe_account_id=None,
        billing_provider="paypal",
        billing_connect_status="connected",
        billing_account_id="merchant_ui_paypal_blocked",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="PayPal Blocked Call",
        calendly_url="https://calendly.com/example/paypal-blocked",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    provider = _StubPayPalProvider(
        readiness=BillingAccountReadiness(can_create_invoices=False),
        readiness_error=PayPalProviderError(
            "paypal merchant status lookup failed",
            operation="paypal_merchant_status",
            http_status=500,
            error_code="INTERNAL_SERVER_ERROR",
        ),
    )

    with _override_app_state("paypal_provider", provider):
        with TestClient(app) as client:
            client.cookies.set(SESSION_COOKIE_NAME, access_token)
            setup_response = client.get("/app", headers=HTML_ACCEPT_HEADERS)
            account_response = client.get("/app/account", headers=HTML_ACCEPT_HEADERS)

    assert setup_response.status_code == 200
    assert account_response.status_code == 200
    assert "Billing setup needs review" in setup_response.text
    assert "Is something wrong before this workspace becomes billable now?" in setup_response.text
    assert (
        "PayPal is connected, but its invoice readiness could not be verified right now. Try again later before relying on new bookings."
        in setup_response.text
    )
    assert (
        "PayPal is connected, but its invoice readiness could not be verified right now. Try again later before relying on new bookings."
        in account_response.text
    )
    assert "Start Stripe switch" in account_response.text
    assert provider.readiness_calls == ["merchant_ui_paypal_blocked", "merchant_ui_paypal_blocked"]


def test_booking_links_page_empty_state_renders_form_and_next_step_copy():
    inserted = _insert_creator_user(
        email=f"ui_booking_links_empty_{uuid.uuid4().hex}@example.com",
        name="Empty State Creator",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get("/app/booking-links", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Booking Links" in response.text
    assert "Create the first booking link" in response.text
    assert 'action="/app/booking-links"' in response.text
    assert "Billing amount in cents" in response.text
    assert "0 saved" in response.text
    assert 'class="wrap-anywhere"' in response.text
    assert 'option value="fullscope"' not in response.text


def test_booking_links_page_create_success_shows_saved_link_and_billing_defaults():
    inserted = _insert_creator_user(
        email=f"ui_booking_links_create_{uuid.uuid4().hex}@example.com",
        name="Booking Link Creator",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        create_response = client.post(
            "/app/booking-links",
            data={
                "provider": "calendly",
                "name": "Paid Deep Dive",
                "destination_url": "https://calendly.com/example/paid-deep-dive",
                "billing_amount_cents": "15000",
                "billing_currency": " usd ",
            },
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )
        page_response = client.get(
            create_response.headers["location"],
            headers=HTML_ACCEPT_HEADERS,
        )

    assert create_response.status_code == 303
    assert create_response.headers["location"] == "/app/booking-links?status=created"

    assert page_response.status_code == 200
    assert "Booking link saved" in page_response.text
    assert "Paid Deep Dive" in page_response.text
    assert "https://calendly.com/example/paid-deep-dive" in page_response.text
    assert "Amount and currency set: USD 150.00" in page_response.text
    assert "1 saved" in page_response.text


def test_booking_links_page_accepts_legacy_calendly_url_browser_field():
    inserted = _insert_creator_user(
        email=f"ui_booking_links_legacy_calendly_{uuid.uuid4().hex}@example.com",
        name="Legacy Calendly Form Creator",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        create_response = client.post(
            "/app/booking-links",
            data={
                "name": "Legacy Calendly Form",
                "calendly_url": "https://calendly.com/example/legacy-browser-form",
                "billing_amount_cents": "12500",
                "billing_currency": " usd ",
            },
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )
        page_response = client.get(
            create_response.headers["location"],
            headers=HTML_ACCEPT_HEADERS,
        )

    assert create_response.status_code == 303
    assert create_response.headers["location"] == "/app/booking-links?status=created"

    assert page_response.status_code == 200
    assert "Booking link saved" in page_response.text
    assert "Legacy Calendly Form" in page_response.text
    assert "https://calendly.com/example/legacy-browser-form" in page_response.text
    assert "Amount and currency set: USD 125.00" in page_response.text


def test_booking_links_page_validation_feedback_preserves_input_and_page_state():
    inserted = _insert_creator_user(
        email=f"ui_booking_links_invalid_{uuid.uuid4().hex}@example.com",
        name="Validation Creator",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.post(
            "/app/booking-links",
            data={
                "provider": "calendly",
                "name": "Broken Link",
                "destination_url": "http://example.com/not-calendly",
                "billing_amount_cents": "0",
                "billing_currency": "USDX",
            },
            headers=HTML_ACCEPT_HEADERS,
    )

    assert response.status_code == 200
    assert "Fix the highlighted fields" in response.text
    assert "must be a positive integer amount in cents" in response.text
    assert "must be a 3-letter currency code" in response.text
    assert 'value="Broken Link"' in response.text
    assert 'value="http://example.com/not-calendly"' in response.text
    assert 'value="0"' in response.text
    assert 'value="USDX"' in response.text


def test_booking_links_page_rejects_fullscope_source_from_creator_ui():
    inserted = _insert_creator_user(
        email=f"ui_booking_links_fullscope_{uuid.uuid4().hex}@example.com",
        name="FullScope Setup Creator",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.post(
            "/app/booking-links",
            data={
                "provider": "fullscope",
                "name": "FS1 Personal Calendar",
                "destination_url": "https://links.fullscope.tools/widget/bookings/fs1-personal-calendar",
                "fullscope_supported_calendar_confirmed": "true",
            },
            headers=HTML_ACCEPT_HEADERS,
        )
    assert response.status_code == 200
    assert "This booking provider is not available in creator setup right now." in response.text
    assert 'option value="fullscope"' not in response.text
    assert 'value="https://links.fullscope.tools/widget/bookings/fs1-personal-calendar"' in response.text
    assert "0 saved" in response.text


def test_booking_links_page_does_not_render_fullscope_confirmation_copy_anymore():
    inserted = _insert_creator_user(
        email=f"ui_booking_links_fullscope_invalid_{uuid.uuid4().hex}@example.com",
        name="FullScope Validation Creator",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.post(
            "/app/booking-links",
            data={
                "provider": "fullscope",
                "name": "Unconfirmed FullScope",
                "destination_url": "https://links.fullscope.tools/widget/bookings/fs1-personal-calendar",
            },
            headers=HTML_ACCEPT_HEADERS,
        )

    assert response.status_code == 200
    assert "This booking provider is not available in creator setup right now." in response.text
    assert "confirm this is a Personal Calendar or direct Service Calendar link" not in response.text
    assert 'option value="fullscope"' not in response.text
    assert 'value="https://links.fullscope.tools/widget/bookings/fs1-personal-calendar"' in response.text


def test_booking_links_page_lists_only_current_creators_links():
    creator_a = _insert_creator_user(
        email=f"ui_booking_links_creator_a_{uuid.uuid4().hex}@example.com",
        name="Creator A",
    )
    creator_b = _insert_creator_user(
        email=f"ui_booking_links_creator_b_{uuid.uuid4().hex}@example.com",
        name="Creator B",
    )
    access_token = _access_token(
        user_id=creator_a["user_id"],
        creator_id=creator_a["creator_id"],
        email=creator_a["email"],
        expires_delta=timedelta(hours=24),
    )

    _insert_booking_link(
        creator_id=creator_a["creator_id"],
        name="Creator A Strategy",
        calendly_url="https://calendly.com/example/creator-a-strategy",
    )
    _insert_booking_link(
        creator_id=creator_b["creator_id"],
        name="Creator B Intro",
        calendly_url="https://calendly.com/example/creator-b-intro",
        billing_amount_cents=9000,
        billing_currency="EUR",
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get("/app/booking-links", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Creator A Strategy" in response.text
    assert "https://calendly.com/example/creator-a-strategy" in response.text
    assert "No billing defaults yet" in response.text
    assert "Creator B Intro" not in response.text
    assert "https://calendly.com/example/creator-b-intro" not in response.text


def test_content_page_without_booking_links_explains_prerequisite():
    inserted = _insert_creator_user(
        email=f"ui_content_empty_{uuid.uuid4().hex}@example.com",
        name="No Booking Link Creator",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get("/app/content", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Content" in response.text
    assert "Create a booking link first" in response.text
    assert 'href="/app/booking-links"' in response.text
    assert 'action="/app/content"' not in response.text
    assert "0 saved" in response.text
    assert 'class="wrap-anywhere"' not in response.text


def test_content_page_shows_fullscope_sources_as_saved_but_unavailable():
    inserted = _insert_creator_user(
        email=f"ui_content_fullscope_{uuid.uuid4().hex}@example.com",
        name="FullScope Content Creator",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="FS1 Personal Calendar",
        provider="fullscope",
        destination_url="https://links.fullscope.tools/widget/bookings/fs1-personal-calendar",
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get("/app/content", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Tracked content unavailable for current saved links" in response.text
    assert "Add a Calendly link to continue." in response.text
    assert ">FS1 Personal Calendar (tracked redirect not available yet)</option>" in response.text
    assert '<option value="' in response.text
    assert '<button type="submit" disabled>Generate tracked link</button>' in response.text


def test_content_page_rejects_fullscope_links_for_tracked_content():
    inserted = _insert_creator_user(
        email=f"ui_content_fullscope_create_{uuid.uuid4().hex}@example.com",
        name="FullScope Bridge Content Creator",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="FS1 Personal Calendar",
        provider="fullscope",
        destination_url="https://links.fullscope.tools/widget/bookings/fs1-personal-calendar",
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.post(
            "/app/content",
            data={
                "source_url": "https://example.com/posts/fullscope-fs4-launch",
                "booking_link_id": booking_link_id,
            },
            headers=HTML_ACCEPT_HEADERS,
        )
    assert response.status_code == 200
    assert "This saved booking source cannot generate tracked content yet. Choose a supported tracked destination instead." in response.text
    assert "Tracked link ready" not in response.text
    assert "fullscope-fs4-launch" in response.text
    assert ">FS1 Personal Calendar (tracked redirect not available yet)</option>" in response.text


def test_booking_activity_page_empty_state_explains_delay_and_next_steps():
    inserted = _insert_creator_user(
        email=f"ui_booking_activity_empty_{uuid.uuid4().hex}@example.com",
        name="No Booking Activity Creator",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get("/app/bookings", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Booking Activity" in response.text
    assert "No bookings captured yet" in response.text
    assert "may not appear immediately" in response.text
    assert "Create tracked content" in response.text
    assert 'href="/app/content"' in response.text
    assert "0 captured" in response.text
    assert 'class="wrap-anywhere"' in response.text


def test_content_page_create_success_shows_tracked_link_and_saved_item():
    inserted = _insert_creator_user(
        email=f"ui_content_create_{uuid.uuid4().hex}@example.com",
        name="Tracked Content Creator",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="Strategy Call",
        calendly_url="https://calendly.com/example/strategy-call",
    )
    tracked_base_url = get_settings().tracked_link_base_url.rstrip("/")

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        create_response = client.post(
            "/app/content",
            data={
                "source_url": "https://example.com/posts/story40-launch-plan",
                "booking_link_id": booking_link_id,
            },
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )
        page_response = client.get(
            create_response.headers["location"],
            headers=HTML_ACCEPT_HEADERS,
        )

    created_tid = parse_qs(urlparse(create_response.headers["location"]).query)["tid"][0]

    assert create_response.status_code == 303
    assert create_response.headers["location"] == f"/app/content?status=created&tid={created_tid}"

    assert page_response.status_code == 200
    assert "Tracked link ready" in page_response.text
    assert f"{tracked_base_url}/r/{created_tid}" in page_response.text
    assert "story40-launch-plan" in page_response.text
    assert "Strategy Call" in page_response.text
    assert 'data-copy-source="created-tracked-url"' in page_response.text
    assert "1 saved" in page_response.text
    assert "Review topics for this content" not in page_response.text


def test_content_page_lists_only_current_creators_content():
    creator_a = _insert_creator_user(
        email=f"ui_content_creator_a_{uuid.uuid4().hex}@example.com",
        name="Content Creator A",
    )
    creator_b = _insert_creator_user(
        email=f"ui_content_creator_b_{uuid.uuid4().hex}@example.com",
        name="Content Creator B",
    )
    access_token = _access_token(
        user_id=creator_a["user_id"],
        creator_id=creator_a["creator_id"],
        email=creator_a["email"],
        expires_delta=timedelta(hours=24),
    )
    creator_a_booking_link_id = _insert_booking_link(
        creator_id=creator_a["creator_id"],
        name="Creator A Strategy",
        calendly_url="https://calendly.com/example/creator-a-strategy",
    )
    creator_b_booking_link_id = _insert_booking_link(
        creator_id=creator_b["creator_id"],
        name="Creator B Strategy",
        calendly_url="https://calendly.com/example/creator-b-strategy",
    )
    tracked_base_url = get_settings().tracked_link_base_url.rstrip("/")

    _insert_content(
        creator_id=creator_a["creator_id"],
        booking_link_id=creator_a_booking_link_id,
        source_url="https://example.com/posts/creator-a-content",
        tid="uiacontenttid",
    )
    _insert_content(
        creator_id=creator_b["creator_id"],
        booking_link_id=creator_b_booking_link_id,
        source_url="https://example.com/posts/creator-b-content",
        tid="uibcontenttid",
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get("/app/content", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "creator-a-content" in response.text
    assert f"{tracked_base_url}/r/uiacontenttid" in response.text
    assert "Creator A Strategy" in response.text
    assert "Review topics for this content" not in response.text
    assert "creator-b-content" not in response.text
    assert f"{tracked_base_url}/r/uibcontenttid" not in response.text
    assert "Creator B Strategy" not in response.text


def test_content_topic_review_page_without_extraction_artifact_explains_prerequisite():
    inserted = _insert_creator_user(
        email=f"ui_topic_review_missing_{uuid.uuid4().hex}@example.com",
        name="Missing Topic Review Creator",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="Topic Review Strategy",
        calendly_url="https://calendly.com/example/topic-review-strategy",
    )
    tid = uuid.uuid4().hex
    _insert_content(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/topic-review-prerequisite",
        tid=tid,
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get(f"/app/content/{tid}/topics", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Topic Review" in response.text
    assert "Fetch and extract first" in response.text
    assert "Run fetch and extract for this tracked content first" in response.text
    assert f'action="/app/content/{tid}/topics/candidates"' not in response.text


def test_content_topic_review_page_generates_candidates_and_supports_confirm_and_reject():
    inserted = _insert_creator_user(
        email=f"ui_topic_review_{uuid.uuid4().hex}@example.com",
        name="Topic Review Creator",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="Topic Review Strategy",
        calendly_url="https://calendly.com/example/topic-review-browser",
    )
    tid = uuid.uuid4().hex
    content_id = _insert_content(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/topic-review-browser",
        tid=tid,
    )
    snapshot_id = _insert_fetch_snapshot(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        requested_url="https://example.com/posts/topic-review-browser",
        fetched_url="https://example.com/posts/topic-review-browser",
        fetch_status="succeeded",
        http_status=200,
        snapshot_text="<html><body><article><p>Topic review browser text.</p></article></body></html>",
        fetched_at=datetime(2026, 3, 10, 15, 0, tzinfo=timezone.utc),
    )
    _insert_extraction_artifact(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        fetch_snapshot_id=snapshot_id,
        extraction_status="succeeded",
        title="Launch Pricing Breakdown",
        extracted_text=(
            "Discovery calls for new leads close faster with pricing upfront.\n"
            "Retainer onboarding steps keep active students moving without confusion.\n"
            "Boilerplate welcome copy should stay out of the canonical set."
        ),
        created_at=datetime(2026, 3, 10, 15, 1, tzinfo=timezone.utc),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)

        start_response = client.get(f"/app/content/{tid}/topics", headers=HTML_ACCEPT_HEADERS)
        generate_response = client.post(
            f"/app/content/{tid}/topics/candidates",
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )

        candidate_rows = _fetch_topic_candidate_rows(content_id=content_id)
        first_candidate_id = candidate_rows[0]["id"]
        second_candidate_id = candidate_rows[1]["id"]
        remaining_candidate_ids = [row["id"] for row in candidate_rows[2:]]

        generated_page = client.get(generate_response.headers["location"], headers=HTML_ACCEPT_HEADERS)
        confirm_response = client.post(
            f"/app/content/{tid}/topics/{first_candidate_id}/confirm",
            data={"confirmed_label": "Discovery Call Pricing"},
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )
        confirmed_page = client.get(confirm_response.headers["location"], headers=HTML_ACCEPT_HEADERS)
        reject_response = client.post(
            f"/app/content/{tid}/topics/{second_candidate_id}/reject",
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )
        rejected_page = client.get(reject_response.headers["location"], headers=HTML_ACCEPT_HEADERS)
        blocked_promote_response = client.post(
            f"/app/content/{tid}/topics/promote",
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )
        final_review_page = rejected_page
        for candidate_id in remaining_candidate_ids:
            resolve_response = client.post(
                f"/app/content/{tid}/topics/{candidate_id}/reject",
                headers=HTML_ACCEPT_HEADERS,
                follow_redirects=False,
            )
            final_review_page = client.get(
                resolve_response.headers["location"],
                headers=HTML_ACCEPT_HEADERS,
            )
        promote_response = client.post(
            f"/app/content/{tid}/topics/promote",
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )
        promoted_page = client.get(promote_response.headers["location"], headers=HTML_ACCEPT_HEADERS)

    assert start_response.status_code == 200
    assert "No suggestions yet" in start_response.text
    assert "No authoritative topics yet" in start_response.text
    assert f'action="/app/content/{tid}/topics/candidates"' in start_response.text

    assert generate_response.status_code == 303
    assert generate_response.headers["location"] == f"/app/content/{tid}/topics?status=generated"
    assert len(candidate_rows) >= 2
    assert "Topic candidates ready" in generated_page.text
    assert candidate_rows[0]["suggested_label"] in generated_page.text
    assert candidate_rows[1]["suggested_label"] in generated_page.text
    assert "Promotion not ready" in generated_page.text

    assert confirm_response.status_code == 303
    assert confirm_response.headers["location"] == f"/app/content/{tid}/topics?status=saved"
    assert "Confirmed topic saved" in confirmed_page.text
    assert "Discovery Call Pricing" in confirmed_page.text
    assert "Promotion not ready" in confirmed_page.text

    assert reject_response.status_code == 303
    assert reject_response.headers["location"] == f"/app/content/{tid}/topics?status=rejected"
    assert "Candidate rejected" in rejected_page.text
    assert "Discovery Call Pricing" in rejected_page.text
    assert "Rejected" in rejected_page.text
    assert "Promotion not ready" in rejected_page.text
    assert "Promote as current evidence" not in rejected_page.text

    assert blocked_promote_response.status_code == 200
    assert "Promotion is not ready yet" in blocked_promote_response.text
    assert "Promote as current evidence" not in blocked_promote_response.text

    assert "Promote as current evidence" in final_review_page.text

    assert promote_response.status_code == 303
    assert promote_response.headers["location"] == f"/app/content/{tid}/topics?status=promoted"
    assert "Authoritative evidence updated" in promoted_page.text
    assert "Current authoritative evidence" in promoted_page.text
    assert "Discovery Call Pricing" in promoted_page.text

    authority_row = _fetch_content_authority_row(content_id=content_id)
    assert authority_row["authoritative_extraction_artifact_id"] is not None


def test_booking_activity_page_lists_only_current_creators_bookings_with_context_and_status():
    creator_a = _insert_creator_user(
        email=f"ui_booking_activity_creator_a_{uuid.uuid4().hex}@example.com",
        name="Booking Activity Creator A",
    )
    creator_b = _insert_creator_user(
        email=f"ui_booking_activity_creator_b_{uuid.uuid4().hex}@example.com",
        name="Booking Activity Creator B",
    )
    access_token = _access_token(
        user_id=creator_a["user_id"],
        creator_id=creator_a["creator_id"],
        email=creator_a["email"],
        expires_delta=timedelta(hours=24),
    )

    creator_a_booking_link_created = _insert_booking_link(
        creator_id=creator_a["creator_id"],
        name="Creator A Strategy",
        calendly_url="https://calendly.com/example/creator-a-strategy",
    )
    creator_a_booking_link_canceled = _insert_booking_link(
        creator_id=creator_a["creator_id"],
        name="Creator A Workshop",
        calendly_url="https://calendly.com/example/creator-a-workshop",
    )
    creator_b_booking_link_id = _insert_booking_link(
        creator_id=creator_b["creator_id"],
        name="Creator B Intro",
        calendly_url="https://calendly.com/example/creator-b-intro",
    )

    _insert_content(
        creator_id=creator_a["creator_id"],
        booking_link_id=creator_a_booking_link_created,
        source_url="https://example.com/posts/creator-a-created",
        tid="uiactivitycreated",
    )
    _insert_content(
        creator_id=creator_a["creator_id"],
        booking_link_id=creator_a_booking_link_canceled,
        source_url="https://example.com/posts/creator-a-canceled",
        tid="uiactivitycanceled",
    )
    _insert_content(
        creator_id=creator_b["creator_id"],
        booking_link_id=creator_b_booking_link_id,
        source_url="https://example.com/posts/creator-b-booking",
        tid="uibbookingactivity",
    )

    _insert_booking(
        creator_id=creator_a["creator_id"],
        booking_link_id=creator_a_booking_link_created,
        tid="uiactivitycreated",
        calendly_booking_uuid="BOOK_UI_ACTIVITY_CREATED",
        booked_at=datetime(2026, 3, 7, 17, 0, tzinfo=timezone.utc),
        status="created",
    )
    _insert_booking(
        creator_id=creator_a["creator_id"],
        booking_link_id=creator_a_booking_link_canceled,
        tid="uiactivitycanceled",
        calendly_booking_uuid="BOOK_UI_ACTIVITY_CANCELED",
        booked_at=datetime(2026, 3, 7, 18, 0, tzinfo=timezone.utc),
        status="canceled",
        canceled_at=datetime(2026, 3, 7, 18, 30, tzinfo=timezone.utc),
    )
    _insert_booking(
        creator_id=creator_b["creator_id"],
        booking_link_id=creator_b_booking_link_id,
        tid="uibbookingactivity",
        calendly_booking_uuid="BOOK_UI_ACTIVITY_OTHER_CREATOR",
        booked_at=datetime(2026, 3, 7, 19, 0, tzinfo=timezone.utc),
        status="created",
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get("/app/bookings", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Recent booking activity" in response.text
    assert "2 captured" in response.text
    assert "Creator A Strategy" in response.text
    assert "Creator A Workshop" in response.text
    assert "https://example.com/posts/creator-a-created" in response.text
    assert "https://example.com/posts/creator-a-canceled" in response.text
    assert "uiactivitycreated" in response.text
    assert "uiactivitycanceled" in response.text
    assert "Created" in response.text
    assert "Canceled" in response.text
    assert "March 07, 2026 at 06:00 PM UTC" in response.text
    assert "March 07, 2026 at 06:30 PM UTC" in response.text
    assert "Creator B Intro" not in response.text
    assert "https://example.com/posts/creator-b-booking" not in response.text
    assert "uibbookingactivity" not in response.text
    assert response.text.index("creator-a-canceled") < response.text.index("creator-a-created")


def test_booking_activity_page_shows_unattributed_booking_current_state():
    creator = _insert_creator_user(
        email=f"ui_activity_unattributed_{uuid.uuid4().hex}@example.com",
        name="UI Activity Unattributed",
    )
    access_token = _access_token(
        user_id=creator["user_id"],
        creator_id=creator["creator_id"],
        email=creator["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=creator["creator_id"],
        name="UI Activity Unattributed Link",
        calendly_url="https://calendly.com/example/ui-activity-unattributed",
    )
    _insert_booking(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        tid=None,
        calendly_booking_uuid="BOOK_UI_ACTIVITY_UNATTRIBUTED",
        booked_at=datetime(2026, 3, 12, 15, 0, tzinfo=timezone.utc),
        attribution_status=BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED,
        unattributed_reason=BOOKING_UNATTRIBUTED_REASON_MISSING_TID,
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get("/app/bookings", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Unattributed booking" in response.text
    assert "Attribution</strong>: Unattributed" in response.text
    assert "Attribution reason</strong>: Missing tracking ID." in response.text
    assert "The booking was captured without a creator-scoped tracking ID." in response.text
    assert "Source URL</strong>: Not linked to tracked content yet." in response.text
    assert "Tracking ID</strong>: Not available yet." in response.text


def test_reports_page_lists_invoice_backed_rows_and_supports_paid_date_filters():
    creator_a = _insert_creator_user(
        email=f"ui_reports_creator_a_{uuid.uuid4().hex}@example.com",
        name="Reports Creator A",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_reports_a",
    )
    creator_b = _insert_creator_user(
        email=f"ui_reports_creator_b_{uuid.uuid4().hex}@example.com",
        name="Reports Creator B",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_reports_b",
    )
    access_token = _access_token(
        user_id=creator_a["user_id"],
        creator_id=creator_a["creator_id"],
        email=creator_a["email"],
        expires_delta=timedelta(hours=24),
    )

    creator_a_booking_link_id = _insert_booking_link(
        creator_id=creator_a["creator_id"],
        name="Creator A Strategy",
        calendly_url="https://calendly.com/example/creator-a-strategy",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    old_content_tid = f"uireportsold{uuid.uuid4().hex[:8]}"
    current_content_tid = f"uireportscurrent{uuid.uuid4().hex[:8]}"
    _insert_content(
        creator_id=creator_a["creator_id"],
        booking_link_id=creator_a_booking_link_id,
        source_url="https://example.com/posts/reports-old",
        tid=old_content_tid,
    )
    _insert_content(
        creator_id=creator_a["creator_id"],
        booking_link_id=creator_a_booking_link_id,
        source_url="https://example.com/posts/reports-current",
        tid=current_content_tid,
    )

    old_booking_id = _insert_booking(
        creator_id=creator_a["creator_id"],
        booking_link_id=creator_a_booking_link_id,
        tid=old_content_tid,
        calendly_booking_uuid=f"BOOK_UI_REPORTS_OLD_{uuid.uuid4().hex[:8]}",
        booked_at=datetime(2026, 3, 7, 12, 0, tzinfo=timezone.utc),
    )
    current_booking_id = _insert_booking(
        creator_id=creator_a["creator_id"],
        booking_link_id=creator_a_booking_link_id,
        tid=current_content_tid,
        calendly_booking_uuid=f"BOOK_UI_REPORTS_CURRENT_{uuid.uuid4().hex[:8]}",
        booked_at=datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc),
    )
    _insert_invoice(
        creator_id=creator_a["creator_id"],
        booking_id=old_booking_id,
        tid=old_content_tid,
        stripe_account_id="acct_ui_reports_a",
        stripe_invoice_id=f"in_ui_reports_old_{uuid.uuid4().hex[:8]}",
        amount_cents=5000,
        paid_at=datetime(2026, 3, 7, 13, 0, tzinfo=timezone.utc),
    )
    _insert_invoice(
        creator_id=creator_a["creator_id"],
        booking_id=current_booking_id,
        tid=current_content_tid,
        stripe_account_id="acct_ui_reports_a",
        stripe_invoice_id=f"in_ui_reports_current_{uuid.uuid4().hex[:8]}",
        amount_cents=19500,
        paid_at=datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc),
    )
    _insert_unmatched_payment_event(
        creator_id=creator_a["creator_id"],
        stripe_account_id="acct_ui_reports_a",
        stripe_event_id=f"evt_ui_reports_{uuid.uuid4().hex[:8]}",
        stripe_invoice_id=f"in_ui_reports_unmatched_{uuid.uuid4().hex[:8]}",
        reason=UNATTRIBUTED_REASON_MISSING_TID,
        paid_at=datetime(2026, 3, 8, 11, 0, tzinfo=timezone.utc),
    )

    creator_b_booking_link_id = _insert_booking_link(
        creator_id=creator_b["creator_id"],
        name="Creator B Strategy",
        calendly_url="https://calendly.com/example/creator-b-strategy",
        billing_amount_cents=88000,
        billing_currency="USD",
    )
    creator_b_tid = f"uireportsb{uuid.uuid4().hex[:8]}"
    _insert_content(
        creator_id=creator_b["creator_id"],
        booking_link_id=creator_b_booking_link_id,
        source_url="https://example.com/posts/reports-other-creator",
        tid=creator_b_tid,
    )
    creator_b_booking_id = _insert_booking(
        creator_id=creator_b["creator_id"],
        booking_link_id=creator_b_booking_link_id,
        tid=creator_b_tid,
        calendly_booking_uuid=f"BOOK_UI_REPORTS_OTHER_{uuid.uuid4().hex[:8]}",
        booked_at=datetime(2026, 3, 8, 14, 0, tzinfo=timezone.utc),
    )
    _insert_invoice(
        creator_id=creator_b["creator_id"],
        booking_id=creator_b_booking_id,
        tid=creator_b_tid,
        stripe_account_id="acct_ui_reports_b",
        stripe_invoice_id=f"in_ui_reports_other_{uuid.uuid4().hex[:8]}",
        amount_cents=88000,
        paid_at=datetime(2026, 3, 8, 15, 0, tzinfo=timezone.utc),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get(
            "/app/reports",
            params={"start_date": "2026-03-08", "end_date": "2026-03-08"},
            headers=HTML_ACCEPT_HEADERS,
        )

    assert response.status_code == 200
    assert "Reports" in response.text
    assert '<a href="/app/reports" class="nav-link active" aria-current="page">Reports</a>' in response.text
    assert "reports-current" in response.text
    assert "reports-old" not in response.text
    assert "reports-other-creator" not in response.text
    assert response.text.count('value="2026-03-08"') == 2
    assert "19.50" not in response.text
    assert "195.00" in response.text
    assert "1 paid invoice" in response.text
    assert "1 paid booking" in response.text
    assert "1 tracked booking" in response.text
    assert "Paid" in response.text
    assert "At least one booking from this content already became counted revenue." in response.text
    assert "Showing 1 of 2 tracked content rows in this paid view." in response.text
    assert "Illustrative preview" not in response.text
    assert "This read-only preview is illustrative only." not in response.text
    assert "Missing tracking ID" in response.text
    assert (
        "1 payment event still diagnostic only and outside paid totals while the attribution chain is incomplete."
        in response.text
    )
    assert "These unmatched events are diagnostic only, not a second revenue total" in response.text
    assert (
        'href="/app/reports/export.csv?start_date=2026-03-08&amp;end_date=2026-03-08"'
        in response.text
    )
    assert (
        'href="/app/reports/explanations/unattributed?start_date=2026-03-08&amp;end_date=2026-03-08"'
        in response.text
    )
    assert (
        'href="/app/reports/topics?start_date=2026-03-08&amp;end_date=2026-03-08"'
        in response.text
    )
    assert (
        'href="/app/reports/booking-links?start_date=2026-03-08&amp;end_date=2026-03-08"'
        in response.text
    )
    assert (
        f'href="/app/reports/content/{current_content_tid}?start_date=2026-03-08&amp;end_date=2026-03-08"'
        in response.text
    )
    assert (
        f'href="/app/reports/explanations/paid/{current_content_tid}?start_date=2026-03-08&amp;end_date=2026-03-08"'
        in response.text
    )
    assert "No tracked bookings are blocked before invoicing right now." in response.text
    assert 'href="/app/attention"' in response.text


def test_reports_topics_page_renders_authoritative_confirmed_topics_only():
    creator = _insert_creator_user(
        email=f"ui_reports_topics_{uuid.uuid4().hex}@example.com",
        name="Reports Topics Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_reports_topics",
    )
    other_creator = _insert_creator_user(
        email=f"ui_reports_topics_other_{uuid.uuid4().hex}@example.com",
        name="Reports Topics Other Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_reports_topics_other",
    )
    access_token = _access_token(
        user_id=creator["user_id"],
        creator_id=creator["creator_id"],
        email=creator["email"],
        expires_delta=timedelta(hours=24),
    )

    booking_link_id = _insert_booking_link(
        creator_id=creator["creator_id"],
        name="Topics Strategy",
        calendly_url="https://calendly.com/example/topics-strategy",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    paid_tid = f"uireportstopicspaid{uuid.uuid4().hex[:8]}"
    paid_content_id = _insert_content(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/reports-topics-paid",
        tid=paid_tid,
    )
    paid_booking_id = _insert_booking(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        tid=paid_tid,
        calendly_booking_uuid=f"BOOK_UI_REPORTS_TOPICS_PAID_{uuid.uuid4().hex[:8]}",
        booked_at=datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc),
    )
    _insert_invoice(
        creator_id=creator["creator_id"],
        booking_id=paid_booking_id,
        tid=paid_tid,
        stripe_account_id="acct_ui_reports_topics",
        stripe_invoice_id=f"in_ui_reports_topics_paid_{uuid.uuid4().hex[:8]}",
        amount_cents=19500,
        paid_at=datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc),
    )
    paid_snapshot_id = _insert_fetch_snapshot(
        content_id=paid_content_id,
        creator_id=creator["creator_id"],
        requested_url="https://example.com/posts/reports-topics-paid",
        fetched_url="https://example.com/posts/reports-topics-paid",
        fetch_status="succeeded",
        http_status=200,
        snapshot_text="<article>Reports topics paid</article>",
        fetched_at=datetime(2026, 3, 8, 9, 5, tzinfo=timezone.utc),
    )
    paid_artifact_id = _insert_extraction_artifact(
        content_id=paid_content_id,
        creator_id=creator["creator_id"],
        fetch_snapshot_id=paid_snapshot_id,
        extraction_status="succeeded",
        title="Reports Topics Paid",
        extracted_text="Reports topics paid extracted text.",
        created_at=datetime(2026, 3, 8, 9, 10, tzinfo=timezone.utc),
    )
    _insert_confirmed_topic(
        content_id=paid_content_id,
        creator_id=creator["creator_id"],
        extraction_artifact_id=paid_artifact_id,
        label="Discovery Calls",
        candidate_rank=1,
    )
    _insert_confirmed_topic(
        content_id=paid_content_id,
        creator_id=creator["creator_id"],
        extraction_artifact_id=paid_artifact_id,
        label="Pricing Strategy",
        candidate_rank=2,
    )
    _set_authoritative_extraction_artifact(
        content_id=paid_content_id,
        artifact_id=paid_artifact_id,
    )

    waiting_tid = f"uireportstopicswaiting{uuid.uuid4().hex[:8]}"
    waiting_content_id = _insert_content(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/reports-topics-waiting",
        tid=waiting_tid,
    )
    _insert_booking(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        tid=waiting_tid,
        calendly_booking_uuid=f"BOOK_UI_REPORTS_TOPICS_WAITING_{uuid.uuid4().hex[:8]}",
        booked_at=datetime(2026, 3, 8, 10, 0, tzinfo=timezone.utc),
    )
    waiting_snapshot_id = _insert_fetch_snapshot(
        content_id=waiting_content_id,
        creator_id=creator["creator_id"],
        requested_url="https://example.com/posts/reports-topics-waiting",
        fetched_url="https://example.com/posts/reports-topics-waiting",
        fetch_status="succeeded",
        http_status=200,
        snapshot_text="<article>Reports topics waiting</article>",
        fetched_at=datetime(2026, 3, 8, 10, 5, tzinfo=timezone.utc),
    )
    waiting_artifact_id = _insert_extraction_artifact(
        content_id=waiting_content_id,
        creator_id=creator["creator_id"],
        fetch_snapshot_id=waiting_snapshot_id,
        extraction_status="succeeded",
        title="Reports Topics Waiting",
        extracted_text="Reports topics waiting extracted text.",
        created_at=datetime(2026, 3, 8, 10, 10, tzinfo=timezone.utc),
    )
    _insert_confirmed_topic(
        content_id=waiting_content_id,
        creator_id=creator["creator_id"],
        extraction_artifact_id=waiting_artifact_id,
        label="Discovery Calls",
    )
    _set_authoritative_extraction_artifact(
        content_id=waiting_content_id,
        artifact_id=waiting_artifact_id,
    )

    older_paid_tid = f"uireportstopicsolder{uuid.uuid4().hex[:8]}"
    older_paid_content_id = _insert_content(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/reports-topics-older",
        tid=older_paid_tid,
    )
    older_paid_booking_id = _insert_booking(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        tid=older_paid_tid,
        calendly_booking_uuid=f"BOOK_UI_REPORTS_TOPICS_OLDER_{uuid.uuid4().hex[:8]}",
        booked_at=datetime(2026, 3, 7, 8, 0, tzinfo=timezone.utc),
    )
    _insert_invoice(
        creator_id=creator["creator_id"],
        booking_id=older_paid_booking_id,
        tid=older_paid_tid,
        stripe_account_id="acct_ui_reports_topics",
        stripe_invoice_id=f"in_ui_reports_topics_older_{uuid.uuid4().hex[:8]}",
        amount_cents=5000,
        paid_at=datetime(2026, 3, 7, 9, 0, tzinfo=timezone.utc),
    )
    older_paid_snapshot_id = _insert_fetch_snapshot(
        content_id=older_paid_content_id,
        creator_id=creator["creator_id"],
        requested_url="https://example.com/posts/reports-topics-older",
        fetched_url="https://example.com/posts/reports-topics-older",
        fetch_status="succeeded",
        http_status=200,
        snapshot_text="<article>Reports topics older</article>",
        fetched_at=datetime(2026, 3, 7, 9, 5, tzinfo=timezone.utc),
    )
    older_paid_artifact_id = _insert_extraction_artifact(
        content_id=older_paid_content_id,
        creator_id=creator["creator_id"],
        fetch_snapshot_id=older_paid_snapshot_id,
        extraction_status="succeeded",
        title="Reports Topics Older",
        extracted_text="Reports topics older extracted text.",
        created_at=datetime(2026, 3, 7, 9, 10, tzinfo=timezone.utc),
    )
    _insert_confirmed_topic(
        content_id=older_paid_content_id,
        creator_id=creator["creator_id"],
        extraction_artifact_id=older_paid_artifact_id,
        label="Retention Reviews",
    )
    _set_authoritative_extraction_artifact(
        content_id=older_paid_content_id,
        artifact_id=older_paid_artifact_id,
    )

    ghost_tid = f"uireportstopicsghost{uuid.uuid4().hex[:8]}"
    ghost_content_id = _insert_content(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/reports-topics-ghost",
        tid=ghost_tid,
    )
    ghost_booking_id = _insert_booking(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        tid=ghost_tid,
        calendly_booking_uuid=f"BOOK_UI_REPORTS_TOPICS_GHOST_{uuid.uuid4().hex[:8]}",
        booked_at=datetime(2026, 3, 8, 11, 0, tzinfo=timezone.utc),
    )
    _insert_invoice(
        creator_id=creator["creator_id"],
        booking_id=ghost_booking_id,
        tid=ghost_tid,
        stripe_account_id="acct_ui_reports_topics",
        stripe_invoice_id=f"in_ui_reports_topics_ghost_{uuid.uuid4().hex[:8]}",
        amount_cents=8800,
        paid_at=datetime(2026, 3, 8, 12, 0, tzinfo=timezone.utc),
    )
    ghost_snapshot_id = _insert_fetch_snapshot(
        content_id=ghost_content_id,
        creator_id=creator["creator_id"],
        requested_url="https://example.com/posts/reports-topics-ghost",
        fetched_url="https://example.com/posts/reports-topics-ghost",
        fetch_status="succeeded",
        http_status=200,
        snapshot_text="<article>Reports topics ghost</article>",
        fetched_at=datetime(2026, 3, 8, 12, 5, tzinfo=timezone.utc),
    )
    ghost_artifact_id = _insert_extraction_artifact(
        content_id=ghost_content_id,
        creator_id=creator["creator_id"],
        fetch_snapshot_id=ghost_snapshot_id,
        extraction_status="succeeded",
        title="Reports Topics Ghost",
        extracted_text="Reports topics ghost extracted text.",
        created_at=datetime(2026, 3, 8, 12, 10, tzinfo=timezone.utc),
    )
    _insert_confirmed_topic(
        content_id=ghost_content_id,
        creator_id=creator["creator_id"],
        extraction_artifact_id=ghost_artifact_id,
        label="Ghost Topic",
    )

    other_booking_link_id = _insert_booking_link(
        creator_id=other_creator["creator_id"],
        name="Topics Other Strategy",
        calendly_url="https://calendly.com/example/topics-other-strategy",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    other_tid = f"uireportstopicsother{uuid.uuid4().hex[:8]}"
    other_content_id = _insert_content(
        creator_id=other_creator["creator_id"],
        booking_link_id=other_booking_link_id,
        source_url="https://example.com/posts/reports-topics-other",
        tid=other_tid,
    )
    other_booking_id = _insert_booking(
        creator_id=other_creator["creator_id"],
        booking_link_id=other_booking_link_id,
        tid=other_tid,
        calendly_booking_uuid=f"BOOK_UI_REPORTS_TOPICS_OTHER_{uuid.uuid4().hex[:8]}",
        booked_at=datetime(2026, 3, 8, 13, 0, tzinfo=timezone.utc),
    )
    _insert_invoice(
        creator_id=other_creator["creator_id"],
        booking_id=other_booking_id,
        tid=other_tid,
        stripe_account_id="acct_ui_reports_topics_other",
        stripe_invoice_id=f"in_ui_reports_topics_other_{uuid.uuid4().hex[:8]}",
        amount_cents=42000,
        paid_at=datetime(2026, 3, 8, 14, 0, tzinfo=timezone.utc),
    )
    other_snapshot_id = _insert_fetch_snapshot(
        content_id=other_content_id,
        creator_id=other_creator["creator_id"],
        requested_url="https://example.com/posts/reports-topics-other",
        fetched_url="https://example.com/posts/reports-topics-other",
        fetch_status="succeeded",
        http_status=200,
        snapshot_text="<article>Reports topics other</article>",
        fetched_at=datetime(2026, 3, 8, 14, 5, tzinfo=timezone.utc),
    )
    other_artifact_id = _insert_extraction_artifact(
        content_id=other_content_id,
        creator_id=other_creator["creator_id"],
        fetch_snapshot_id=other_snapshot_id,
        extraction_status="succeeded",
        title="Reports Topics Other",
        extracted_text="Reports topics other extracted text.",
        created_at=datetime(2026, 3, 8, 14, 10, tzinfo=timezone.utc),
    )
    _insert_confirmed_topic(
        content_id=other_content_id,
        creator_id=other_creator["creator_id"],
        extraction_artifact_id=other_artifact_id,
        label="Sales Coaching",
    )
    _set_authoritative_extraction_artifact(
        content_id=other_content_id,
        artifact_id=other_artifact_id,
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get(
            "/app/reports/topics",
            params={"start_date": "2026-03-08", "end_date": "2026-03-08"},
            headers=HTML_ACCEPT_HEADERS,
        )

    assert response.status_code == 200
    assert "Topic analytics" in response.text
    assert "Only authoritative confirmed topics count here." in response.text
    assert "A single content row can appear under more than one confirmed topic" in response.text
    assert "Confirmed-topic rows can overlap." in response.text
    assert "Paid outcomes" in response.text
    assert "Grouped content" in response.text
    assert "Diagnostic only" in response.text
    assert "2 topic rows visible" in response.text
    assert "Discovery Calls" in response.text
    assert "Pricing Strategy" in response.text
    assert "195.00" in response.text
    assert "Retention Reviews" not in response.text
    assert "Ghost Topic" not in response.text
    assert "Sales Coaching" not in response.text
    assert (
        'href="/app/reports?start_date=2026-03-08&amp;end_date=2026-03-08"'
        in response.text
    )
    assert (
        'href="/app/reports/topics?start_date=2026-03-08&amp;end_date=2026-03-08"'
        in response.text
    )
    assert (
        'href="/app/reports/booking-links?start_date=2026-03-08&amp;end_date=2026-03-08"'
        in response.text
    )


def test_reports_booking_links_page_groups_rows_by_saved_link_identity():
    creator = _insert_creator_user(
        email=f"ui_reports_booking_links_{uuid.uuid4().hex}@example.com",
        name="Reports Booking Links Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_reports_booking_links",
    )
    other_creator = _insert_creator_user(
        email=f"ui_reports_booking_links_other_{uuid.uuid4().hex}@example.com",
        name="Reports Booking Links Other Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_reports_booking_links_other",
    )
    access_token = _access_token(
        user_id=creator["user_id"],
        creator_id=creator["creator_id"],
        email=creator["email"],
        expires_delta=timedelta(hours=24),
    )

    active_booking_link_id = _insert_booking_link(
        creator_id=creator["creator_id"],
        name="Discovery Call CTA",
        calendly_url="https://calendly.com/example/discovery-call-cta",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    historical_booking_link_id = _insert_booking_link(
        creator_id=creator["creator_id"],
        name="Legacy Webinar CTA",
        calendly_url="https://calendly.com/example/legacy-webinar-cta",
        billing_amount_cents=5000,
        billing_currency="USD",
    )
    paid_tid = f"uireportsbookinglinkspaid{uuid.uuid4().hex[:8]}"
    waiting_tid = f"uireportsbookinglinkswaiting{uuid.uuid4().hex[:8]}"
    historical_tid = f"uireportsbookinglinkshistorical{uuid.uuid4().hex[:8]}"

    _insert_content(
        creator_id=creator["creator_id"],
        booking_link_id=active_booking_link_id,
        source_url="https://example.com/posts/reports-booking-links-paid",
        tid=paid_tid,
    )
    _insert_content(
        creator_id=creator["creator_id"],
        booking_link_id=active_booking_link_id,
        source_url="https://example.com/posts/reports-booking-links-waiting",
        tid=waiting_tid,
    )
    _insert_content(
        creator_id=creator["creator_id"],
        booking_link_id=historical_booking_link_id,
        source_url="https://example.com/posts/reports-booking-links-historical",
        tid=historical_tid,
    )

    paid_booking_id = _insert_booking(
        creator_id=creator["creator_id"],
        booking_link_id=active_booking_link_id,
        tid=paid_tid,
        calendly_booking_uuid=f"BOOK_UI_REPORTS_BOOKING_LINKS_PAID_{uuid.uuid4().hex[:8]}",
        booked_at=datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc),
    )
    _insert_booking(
        creator_id=creator["creator_id"],
        booking_link_id=active_booking_link_id,
        tid=waiting_tid,
        calendly_booking_uuid=f"BOOK_UI_REPORTS_BOOKING_LINKS_WAITING_{uuid.uuid4().hex[:8]}",
        booked_at=datetime(2026, 3, 8, 10, 0, tzinfo=timezone.utc),
    )
    historical_booking_id = _insert_booking(
        creator_id=creator["creator_id"],
        booking_link_id=historical_booking_link_id,
        tid=historical_tid,
        calendly_booking_uuid=f"BOOK_UI_REPORTS_BOOKING_LINKS_HISTORICAL_{uuid.uuid4().hex[:8]}",
        booked_at=datetime(2026, 3, 7, 8, 0, tzinfo=timezone.utc),
    )
    _insert_invoice(
        creator_id=creator["creator_id"],
        booking_id=paid_booking_id,
        tid=paid_tid,
        stripe_account_id="acct_ui_reports_booking_links",
        stripe_invoice_id=f"in_ui_reports_booking_links_paid_{uuid.uuid4().hex[:8]}",
        amount_cents=19500,
        paid_at=datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc),
    )
    _insert_invoice(
        creator_id=creator["creator_id"],
        booking_id=historical_booking_id,
        tid=historical_tid,
        stripe_account_id="acct_ui_reports_booking_links",
        stripe_invoice_id=f"in_ui_reports_booking_links_historical_{uuid.uuid4().hex[:8]}",
        amount_cents=5000,
        paid_at=datetime(2026, 3, 7, 9, 0, tzinfo=timezone.utc),
    )

    with _engine().begin() as conn:
        conn.execute(
            text(
                "UPDATE booking_links "
                "SET name = :name, billing_amount_cents = NULL, billing_currency = NULL "
                "WHERE id = :id"
            ),
            {
                "id": historical_booking_link_id,
                "name": "Archived Webinar CTA",
            },
        )

    other_booking_link_id = _insert_booking_link(
        creator_id=other_creator["creator_id"],
        name="Other Creator CTA",
        calendly_url="https://calendly.com/example/other-creator-cta",
        billing_amount_cents=42000,
        billing_currency="USD",
    )
    other_tid = f"uireportsbookinglinksother{uuid.uuid4().hex[:8]}"
    _insert_content(
        creator_id=other_creator["creator_id"],
        booking_link_id=other_booking_link_id,
        source_url="https://example.com/posts/reports-booking-links-other",
        tid=other_tid,
    )
    other_booking_id = _insert_booking(
        creator_id=other_creator["creator_id"],
        booking_link_id=other_booking_link_id,
        tid=other_tid,
        calendly_booking_uuid=f"BOOK_UI_REPORTS_BOOKING_LINKS_OTHER_{uuid.uuid4().hex[:8]}",
        booked_at=datetime(2026, 3, 8, 13, 0, tzinfo=timezone.utc),
    )
    _insert_invoice(
        creator_id=other_creator["creator_id"],
        booking_id=other_booking_id,
        tid=other_tid,
        stripe_account_id="acct_ui_reports_booking_links_other",
        stripe_invoice_id=f"in_ui_reports_booking_links_other_{uuid.uuid4().hex[:8]}",
        amount_cents=42000,
        paid_at=datetime(2026, 3, 8, 14, 0, tzinfo=timezone.utc),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        unfiltered_response = client.get(
            "/app/reports/booking-links",
            headers=HTML_ACCEPT_HEADERS,
        )
        filtered_response = client.get(
            "/app/reports/booking-links",
            params={"start_date": "2026-03-08", "end_date": "2026-03-08"},
            headers=HTML_ACCEPT_HEADERS,
        )

    assert unfiltered_response.status_code == 200
    assert "Booking-link analytics" in unfiltered_response.text
    assert "Booking-link rows stay tied to saved link identity" in unfiltered_response.text
    assert "Each row stays tied to one saved booking-link identity." in unfiltered_response.text
    assert "Saved-link identity" in unfiltered_response.text
    assert "Current metadata today" in unfiltered_response.text
    assert "Paid outcomes" in unfiltered_response.text
    assert "Diagnostic only" in unfiltered_response.text
    assert "2 booking link rows visible" in unfiltered_response.text
    assert "Discovery Call CTA" in unfiltered_response.text
    assert "Archived Webinar CTA" in unfiltered_response.text
    assert "No billing defaults yet" in unfiltered_response.text
    assert "Other Creator CTA" not in unfiltered_response.text
    assert 'href="/app/reports/topics"' in unfiltered_response.text
    assert 'href="/app/reports/booking-links" class="nav-link active"' in unfiltered_response.text

    assert filtered_response.status_code == 200
    assert "1 booking link row visible" in filtered_response.text
    assert "Discovery Call CTA" in filtered_response.text
    assert "Archived Webinar CTA" not in filtered_response.text
    assert (
        'href="/app/reports?start_date=2026-03-08&amp;end_date=2026-03-08"'
        in filtered_response.text
    )
    assert (
        'href="/app/reports/topics?start_date=2026-03-08&amp;end_date=2026-03-08"'
        in filtered_response.text
    )
    assert (
        'href="/app/reports/booking-links?start_date=2026-03-08&amp;end_date=2026-03-08"'
        in filtered_response.text
    )


def test_attention_page_renders_blocked_and_unmatched_cases():
    creator = _insert_creator_user(
        email=f"ui_attention_{uuid.uuid4().hex}@example.com",
        name="Attention Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_attention",
    )
    access_token = _access_token(
        user_id=creator["user_id"],
        creator_id=creator["creator_id"],
        email=creator["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=creator["creator_id"],
        name="Attention Strategy",
        calendly_url="https://calendly.com/example/attention-strategy",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    tid = f"uiattention{uuid.uuid4().hex[:8]}"
    booking_uuid = f"BOOK_UI_ATTENTION_{uuid.uuid4().hex[:8]}"
    _insert_content(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/attention",
        tid=tid,
    )
    booking_id = _insert_booking(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        tid=tid,
        calendly_booking_uuid=booking_uuid,
        booked_at=datetime(2026, 3, 8, 10, 0, tzinfo=timezone.utc),
    )
    case_id = _insert_blocked_billing_case(
        creator_id=creator["creator_id"],
        booking_id=booking_id,
        tid=tid,
        calendly_booking_uuid=booking_uuid,
        stripe_account_id="acct_ui_attention",
        frozen_amount_cents=19500,
        frozen_currency="USD",
        reason_code="creator_not_billable",
        first_blocked_at=datetime(2026, 3, 8, 10, 5, tzinfo=timezone.utc),
    )
    stripe_event_id = f"evt_ui_attention_{uuid.uuid4().hex[:8]}"
    stripe_invoice_id = f"in_ui_attention_{uuid.uuid4().hex[:8]}"
    _insert_unmatched_payment_event(
        creator_id=creator["creator_id"],
        stripe_account_id="acct_ui_attention",
        stripe_event_id=stripe_event_id,
        stripe_invoice_id=stripe_invoice_id,
        reason=UNATTRIBUTED_REASON_MISSING_TID,
        paid_at=datetime(2026, 3, 8, 11, 0, tzinfo=timezone.utc),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get("/app/attention", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Attention" in response.text
    assert '<a href="/app/attention" class="nav-link active" aria-current="page">Attention</a>' in response.text
    assert (
        "Review the diagnostic items the shell keeps separate from paid totals"
        in response.text
    )
    assert "Tracked bookings blocked before invoicing" in response.text
    assert "Verified payments still diagnostic-only" in response.text
    assert booking_uuid in response.text
    assert "Creator not billable" in response.text
    assert "creator_not_billable" in response.text
    assert "Finish the billing setup and then retry invoice creation." in response.text
    assert "Retry invoice creation" in response.text
    assert f'/app/attention/blocked-billing/{case_id}/retry' in response.text
    assert "Current blocked billing details" in response.text
    assert (
        "These cases explain why a tracked booking did not become an invoice yet."
        in response.text
    )
    assert stripe_event_id in response.text
    assert stripe_invoice_id in response.text
    assert "Missing tracking ID" in response.text
    assert "Use the tracked link consistently going forward." in response.text
    assert "Current unmatched payment diagnostics" in response.text
    assert (
        "These are real provider payment events, but they stay diagnostic until the attribution chain is complete enough to enter canonical paid truth."
        in response.text
    )


def test_health_page_renders_creator_scoped_snapshot():
    creator = _insert_creator_user(
        email=f"ui_health_{uuid.uuid4().hex}@example.com",
        name="Health Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_health",
    )
    other_creator = _insert_creator_user(
        email=f"ui_health_other_{uuid.uuid4().hex}@example.com",
        name="Other Health Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_health_other",
    )
    access_token = _access_token(
        user_id=creator["user_id"],
        creator_id=creator["creator_id"],
        email=creator["email"],
        expires_delta=timedelta(hours=24),
    )

    booking_link_id = _insert_booking_link(
        creator_id=creator["creator_id"],
        name="Health Strategy",
        calendly_url="https://calendly.com/example/health-strategy",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    other_booking_link_id = _insert_booking_link(
        creator_id=other_creator["creator_id"],
        name="Other Health Strategy",
        calendly_url="https://calendly.com/example/other-health-strategy",
        billing_amount_cents=19500,
        billing_currency="USD",
    )

    tracked_tid = f"uihealth{uuid.uuid4().hex[:8]}"
    _insert_content(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/health-current",
        tid=tracked_tid,
    )
    _insert_booking(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        tid=None,
        calendly_booking_uuid=f"BOOK_UI_HEALTH_UNATTRIBUTED_{uuid.uuid4().hex[:8]}",
        booked_at=datetime(2026, 3, 12, 10, 0, tzinfo=timezone.utc),
        attribution_status=BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED,
        unattributed_reason=BOOKING_UNATTRIBUTED_REASON_MISSING_TID,
    )
    _insert_calendly_event_record(
        tid=tracked_tid,
        calendly_event_id=f"EVT_UI_HEALTH_FAILED_{uuid.uuid4().hex[:8]}",
        calendly_booking_uuid=f"BOOK_UI_HEALTH_FAILED_{uuid.uuid4().hex[:8]}",
        processing_status="failed",
    )

    blocked_tid = f"uihealthblocked{uuid.uuid4().hex[:8]}"
    _insert_content(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/health-blocked",
        tid=blocked_tid,
    )
    blocked_booking_id = _insert_booking(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        tid=blocked_tid,
        calendly_booking_uuid=f"BOOK_UI_HEALTH_BLOCKED_{uuid.uuid4().hex[:8]}",
        booked_at=datetime(2026, 3, 12, 10, 15, tzinfo=timezone.utc),
    )
    _insert_blocked_billing_case(
        creator_id=creator["creator_id"],
        booking_id=blocked_booking_id,
        tid=blocked_tid,
        calendly_booking_uuid=f"BOOK_UI_HEALTH_BLOCKED_CASE_{uuid.uuid4().hex[:8]}",
        stripe_account_id="acct_ui_health",
        frozen_amount_cents=19500,
        frozen_currency="USD",
        reason_code="creator_not_billable",
        first_blocked_at=datetime(2026, 3, 12, 10, 20, tzinfo=timezone.utc),
    )

    pending_tid = f"uihealthpending{uuid.uuid4().hex[:8]}"
    _insert_content(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/health-pending",
        tid=pending_tid,
    )
    pending_booking_id = _insert_booking(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        tid=pending_tid,
        calendly_booking_uuid=f"BOOK_UI_HEALTH_PENDING_{uuid.uuid4().hex[:8]}",
        booked_at=datetime(2026, 3, 12, 10, 30, tzinfo=timezone.utc),
    )
    _insert_invoice(
        creator_id=creator["creator_id"],
        booking_id=pending_booking_id,
        tid=pending_tid,
        stripe_account_id="acct_ui_health",
        stripe_invoice_id=f"in_ui_health_pending_{uuid.uuid4().hex[:8]}",
        amount_cents=19500,
        paid_at=datetime(2026, 3, 12, 11, 0, tzinfo=timezone.utc),
    )
    _insert_unmatched_payment_event(
        creator_id=creator["creator_id"],
        stripe_account_id="acct_ui_health",
        stripe_event_id=f"evt_ui_health_unmatched_{uuid.uuid4().hex[:8]}",
        stripe_invoice_id=f"in_ui_health_unmatched_{uuid.uuid4().hex[:8]}",
        reason=UNATTRIBUTED_REASON_UNKNOWN_STRIPE_INVOICE_ID,
        paid_at=datetime(2026, 3, 12, 11, 5, tzinfo=timezone.utc),
    )

    lag_tid = f"uihealthlag{uuid.uuid4().hex[:8]}"
    lag_content_id = _insert_content(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/health-lag",
        tid=lag_tid,
    )
    lag_snapshot_id = _insert_fetch_snapshot(
        content_id=lag_content_id,
        creator_id=creator["creator_id"],
        requested_url="https://example.com/posts/health-lag",
        fetched_url="https://example.com/posts/health-lag",
        fetch_status="succeeded",
        http_status=200,
        snapshot_text="<html><body><article><p>Health lag.</p></article></body></html>",
        fetched_at=datetime(2026, 3, 12, 11, 10, tzinfo=timezone.utc),
    )
    lag_artifact_id = _insert_extraction_artifact(
        content_id=lag_content_id,
        creator_id=creator["creator_id"],
        fetch_snapshot_id=lag_snapshot_id,
        extraction_status="succeeded",
        title="Health Lag Artifact",
        extracted_text="Health lag extracted text.",
        created_at=datetime(2026, 3, 12, 11, 15, tzinfo=timezone.utc),
    )
    _insert_confirmed_topic(
        content_id=lag_content_id,
        creator_id=creator["creator_id"],
        extraction_artifact_id=lag_artifact_id,
        label="Lagging Authority",
    )

    other_tid = f"uihealthother{uuid.uuid4().hex[:8]}"
    _insert_content(
        creator_id=other_creator["creator_id"],
        booking_link_id=other_booking_link_id,
        source_url="https://example.com/posts/other-health",
        tid=other_tid,
    )
    _insert_calendly_event_record(
        tid=other_tid,
        calendly_event_id=f"EVT_UI_HEALTH_OTHER_FAILED_{uuid.uuid4().hex[:8]}",
        calendly_booking_uuid=f"BOOK_UI_HEALTH_OTHER_FAILED_{uuid.uuid4().hex[:8]}",
        processing_status="failed",
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get("/app/health", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Health" in response.text
    assert '<a href="/app/health" class="nav-link active" aria-current="page">Health</a>' in response.text
    assert "FullScope ingress" in response.text
    assert "1 unattributed booking" in response.text
    assert "1 failed event currently need operator review." in response.text
    assert "1 backlog event" in response.text
    assert "1 open case" in response.text
    assert "1 lagging content item" in response.text
    assert "1 booking with missing tracking id." in response.text
    assert "1 event currently marked failed." in response.text
    assert "1 settled row currently marked pending." in response.text
    assert "1 backlog event due to unknown invoice." in response.text
    assert "1 open case due to creator not billable." in response.text
    assert "1 content item with missing authoritative evidence." in response.text
    assert 'href="/app/attention"' in response.text
    assert 'href="/app/content"' in response.text


def test_health_page_surfaces_paypal_payment_truth_section():
    creator = _insert_creator_user(
        email=f"ui_health_paypal_{uuid.uuid4().hex}@example.com",
        name="Health PayPal Creator",
        stripe_connect_status="pending",
        stripe_account_id=None,
        billing_provider="paypal",
        billing_connect_status="connected",
        billing_account_id="merchant_ui_health_paypal",
    )
    access_token = _access_token(
        user_id=creator["user_id"],
        creator_id=creator["creator_id"],
        email=creator["email"],
        expires_delta=timedelta(hours=24),
    )

    booking_link_id = _insert_booking_link(
        creator_id=creator["creator_id"],
        name="Health PayPal Strategy",
        calendly_url="https://calendly.com/example/health-paypal-strategy",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    tracked_tid = f"uihealthpaypal{uuid.uuid4().hex[:8]}"
    booking_uuid = f"BOOK_UI_HEALTH_PAYPAL_{uuid.uuid4().hex[:8]}"
    matched_provider_invoice_id = f"INV2-UI-HEALTH-PAYPAL-{uuid.uuid4().hex[:8]}"
    _insert_content(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/health-paypal",
        tid=tracked_tid,
    )
    booking_id = _insert_booking(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        tid=tracked_tid,
        calendly_booking_uuid=booking_uuid,
        booked_at=datetime(2026, 3, 12, 12, 0, tzinfo=timezone.utc),
    )
    invoice_id = _insert_invoice(
        creator_id=creator["creator_id"],
        booking_id=booking_id,
        tid=tracked_tid,
        amount_cents=19500,
        paid_at=datetime(2026, 3, 12, 12, 30, tzinfo=timezone.utc),
        payment_provider="paypal",
        provider_account_id="merchant_ui_health_paypal",
        provider_invoice_id=matched_provider_invoice_id,
    )
    _insert_matched_payment_event(
        creator_id=creator["creator_id"],
        booking_id=booking_id,
        tid=tracked_tid,
        invoice_id=invoice_id,
        paid_at=datetime(2026, 3, 12, 12, 30, tzinfo=timezone.utc),
        payment_provider="paypal",
        provider_account_id="merchant_ui_health_paypal",
        provider_event_id=f"WH-UI-HEALTH-PAYPAL-{uuid.uuid4().hex[:8]}",
        provider_event_type="INVOICING.INVOICE.PAID",
        provider_invoice_id=matched_provider_invoice_id,
    )
    _insert_unmatched_payment_event(
        creator_id=creator["creator_id"],
        reason=UNATTRIBUTED_REASON_MISSING_TID,
        paid_at=datetime(2026, 3, 12, 12, 35, tzinfo=timezone.utc),
        payment_provider="paypal",
        provider_account_id="merchant_ui_health_paypal",
        provider_event_id=f"WH-UI-HEALTH-PAYPAL-UNMATCHED-{uuid.uuid4().hex[:8]}",
        provider_event_type="INVOICING.INVOICE.PAID",
        provider_invoice_id=f"INV2-UI-HEALTH-PAYPAL-UNMATCHED-{uuid.uuid4().hex[:8]}",
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get("/app/health", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "PayPal payment truth" in response.text
    assert "PayPal settled rows and unmatched backlog" in response.text
    assert "1 settled row currently marked matched." in response.text
    assert "1 backlog event due to missing tracking id." in response.text
    assert "Stripe payment truth" not in response.text


def test_attention_retry_route_recovers_blocked_case_with_frozen_inputs():
    creator = _insert_creator_user(
        email=f"ui_attention_retry_{uuid.uuid4().hex}@example.com",
        name="Attention Retry Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_attention_retry",
    )
    access_token = _access_token(
        user_id=creator["user_id"],
        creator_id=creator["creator_id"],
        email=creator["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=creator["creator_id"],
        name="Attention Retry Strategy",
        calendly_url="https://calendly.com/example/attention-retry-strategy",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    tid = f"uiattentionretry{uuid.uuid4().hex[:8]}"
    booking_uuid = f"BOOK_UI_ATTENTION_RETRY_{uuid.uuid4().hex[:8]}"
    _insert_content(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/attention-retry",
        tid=tid,
    )
    booking_id = _insert_booking(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        tid=tid,
        calendly_booking_uuid=booking_uuid,
        booked_at=datetime(2026, 3, 8, 12, 0, tzinfo=timezone.utc),
    )
    case_id = _insert_blocked_billing_case(
        creator_id=creator["creator_id"],
        booking_id=booking_id,
        tid=tid,
        calendly_booking_uuid=booking_uuid,
        stripe_account_id="acct_ui_attention_retry",
        frozen_amount_cents=19500,
        frozen_currency="USD",
        reason_code="creator_not_billable",
        first_blocked_at=datetime(2026, 3, 8, 12, 5, tzinfo=timezone.utc),
    )

    with _engine().begin() as conn:
        conn.execute(
            text(
                "UPDATE booking_links "
                "SET billing_amount_cents = :billing_amount_cents, billing_currency = :billing_currency "
                "WHERE id = :id"
            ),
            {
                "id": booking_link_id,
                "billing_amount_cents": 9900,
                "billing_currency": "EUR",
            },
        )

    provider = _StubStripeProvider(
        account_id="acct_ui_attention_retry",
        readiness=StripeAccountReadiness(charges_enabled=True),
        created_invoice_id="in_ui_attention_retry_created",
    )

    with _override_app_state("stripe_provider", provider):
        with TestClient(app) as client:
            client.cookies.set(SESSION_COOKIE_NAME, access_token)
            response = client.post(
                f"/app/attention/blocked-billing/{case_id}/retry",
                headers=HTML_ACCEPT_HEADERS,
                follow_redirects=False,
            )

    with _engine().begin() as conn:
        invoice_row = conn.execute(
            text(
                "SELECT amount_cents, currency, stripe_invoice_id, status "
                "FROM invoices WHERE booking_id = :booking_id"
            ),
            {"booking_id": booking_id},
        ).mappings().one()
        blocked_case_row = conn.execute(
            text(
                "SELECT status, resolution_code, last_retry_at, resolved_at "
                "FROM blocked_billing_cases WHERE id = :id"
            ),
            {"id": case_id},
        ).mappings().one()

    assert response.status_code == 303
    assert response.headers["location"] == "/app/attention?status=recovered"
    assert len(provider.create_calls) == 1
    assert provider.create_calls[0]["amount_cents"] == 19500
    assert provider.create_calls[0]["currency"] == "USD"
    assert invoice_row["amount_cents"] == 19500
    assert invoice_row["currency"] == "USD"
    assert invoice_row["stripe_invoice_id"] == "in_ui_attention_retry_created"
    assert invoice_row["status"] == "open"
    assert blocked_case_row["status"] == "resolved"
    assert blocked_case_row["resolution_code"] == "invoice_created"
    assert blocked_case_row["last_retry_at"] is not None
    assert blocked_case_row["resolved_at"] is not None


def test_reports_paid_explanation_page_renders_creator_scoped_canonical_chain():
    creator = _insert_creator_user(
        email=f"ui_reports_explanation_{uuid.uuid4().hex}@example.com",
        name="Reports Explanation Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_reports_explanation",
    )
    access_token = _access_token(
        user_id=creator["user_id"],
        creator_id=creator["creator_id"],
        email=creator["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=creator["creator_id"],
        name="Explanation Strategy",
        calendly_url="https://calendly.com/example/explanation-strategy",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    content_tid = f"uireportsexplanation{uuid.uuid4().hex[:8]}"
    source_url = "https://example.com/posts/reports-explanation"
    booking_uuid = f"BOOK_UI_REPORTS_EXPLANATION_{uuid.uuid4().hex[:8]}"
    stripe_invoice_id = f"in_ui_reports_explanation_{uuid.uuid4().hex[:8]}"
    stripe_event_id = f"evt_ui_reports_explanation_{uuid.uuid4().hex[:8]}"

    _insert_content(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        source_url=source_url,
        tid=content_tid,
    )
    booking_id = _insert_booking(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        tid=content_tid,
        calendly_booking_uuid=booking_uuid,
        booked_at=datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc),
    )
    invoice_id = _insert_invoice(
        creator_id=creator["creator_id"],
        booking_id=booking_id,
        tid=content_tid,
        stripe_account_id="acct_ui_reports_explanation",
        stripe_invoice_id=stripe_invoice_id,
        amount_cents=19500,
        paid_at=datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc),
    )
    _insert_matched_payment_event(
        creator_id=creator["creator_id"],
        booking_id=booking_id,
        tid=content_tid,
        invoice_id=invoice_id,
        stripe_account_id="acct_ui_reports_explanation",
        stripe_event_id=stripe_event_id,
        stripe_invoice_id=stripe_invoice_id,
        paid_at=datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get(
            f"/app/reports/explanations/paid/{content_tid}",
            params={"start_date": "2026-03-08", "end_date": "2026-03-08"},
            headers=HTML_ACCEPT_HEADERS,
        )

    assert response.status_code == 200
    assert "Why this revenue counted" in response.text
    assert "Counted in paid totals for this selected window" in response.text
    assert "What had to line up" in response.text
    assert source_url in response.text
    assert content_tid in response.text
    assert booking_uuid in response.text
    assert stripe_invoice_id in response.text
    assert stripe_event_id in response.text
    assert "Applied" in response.text
    assert (
        'href="/app/reports?start_date=2026-03-08&amp;end_date=2026-03-08"'
        in response.text
    )


def test_reports_paid_explanation_page_returns_404_for_other_creators_row():
    creator_a = _insert_creator_user(
        email=f"ui_reports_explanation_a_{uuid.uuid4().hex}@example.com",
        name="Reports Explanation Creator A",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_reports_explanation_a",
    )
    creator_b = _insert_creator_user(
        email=f"ui_reports_explanation_b_{uuid.uuid4().hex}@example.com",
        name="Reports Explanation Creator B",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_reports_explanation_b",
    )
    access_token_b = _access_token(
        user_id=creator_b["user_id"],
        creator_id=creator_b["creator_id"],
        email=creator_b["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=creator_a["creator_id"],
        name="Explanation Isolation Strategy",
        calendly_url="https://calendly.com/example/explanation-isolation",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    content_tid = f"uireportsexplanationhidden{uuid.uuid4().hex[:8]}"
    stripe_invoice_id = f"in_ui_reports_explanation_hidden_{uuid.uuid4().hex[:8]}"

    _insert_content(
        creator_id=creator_a["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/reports-explanation-hidden",
        tid=content_tid,
    )
    booking_id = _insert_booking(
        creator_id=creator_a["creator_id"],
        booking_link_id=booking_link_id,
        tid=content_tid,
        calendly_booking_uuid=f"BOOK_UI_REPORTS_EXPLANATION_HIDDEN_{uuid.uuid4().hex[:8]}",
        booked_at=datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc),
    )
    invoice_id = _insert_invoice(
        creator_id=creator_a["creator_id"],
        booking_id=booking_id,
        tid=content_tid,
        stripe_account_id="acct_ui_reports_explanation_a",
        stripe_invoice_id=stripe_invoice_id,
        amount_cents=19500,
        paid_at=datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc),
    )
    _insert_matched_payment_event(
        creator_id=creator_a["creator_id"],
        booking_id=booking_id,
        tid=content_tid,
        invoice_id=invoice_id,
        stripe_account_id="acct_ui_reports_explanation_a",
        stripe_event_id=f"evt_ui_reports_explanation_hidden_{uuid.uuid4().hex[:8]}",
        stripe_invoice_id=stripe_invoice_id,
        paid_at=datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token_b)
        response = client.get(
            f"/app/reports/explanations/paid/{content_tid}",
            headers=HTML_ACCEPT_HEADERS,
        )

    assert response.status_code == 404
    assert response.json() == {"detail": "report explanation not found"}


def test_reports_content_drilldown_page_renders_creator_scoped_bookings_paid_and_diagnostics():
    creator = _insert_creator_user(
        email=f"ui_reports_drilldown_{uuid.uuid4().hex}@example.com",
        name="Reports Drilldown Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_reports_drilldown",
    )
    access_token = _access_token(
        user_id=creator["user_id"],
        creator_id=creator["creator_id"],
        email=creator["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=creator["creator_id"],
        name="Drilldown Strategy",
        calendly_url="https://calendly.com/example/drilldown-strategy",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    content_tid = f"uireportsdrilldown{uuid.uuid4().hex[:8]}"
    source_url = "https://example.com/posts/reports-drilldown"

    _insert_content(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        source_url=source_url,
        tid=content_tid,
    )
    paid_booking_uuid = f"BOOK_UI_REPORTS_DRILLDOWN_PAID_{uuid.uuid4().hex[:8]}"
    paid_booking_id = _insert_booking(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        tid=content_tid,
        calendly_booking_uuid=paid_booking_uuid,
        booked_at=datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc),
    )
    invoice_id = _insert_invoice(
        creator_id=creator["creator_id"],
        booking_id=paid_booking_id,
        tid=content_tid,
        stripe_account_id="acct_ui_reports_drilldown",
        stripe_invoice_id=f"in_ui_reports_drilldown_paid_{uuid.uuid4().hex[:8]}",
        amount_cents=19500,
        paid_at=datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc),
    )
    _insert_matched_payment_event(
        creator_id=creator["creator_id"],
        booking_id=paid_booking_id,
        tid=content_tid,
        invoice_id=invoice_id,
        stripe_account_id="acct_ui_reports_drilldown",
        stripe_event_id=f"evt_ui_reports_drilldown_paid_{uuid.uuid4().hex[:8]}",
        stripe_invoice_id=f"in_ui_reports_drilldown_paid_{uuid.uuid4().hex[:8]}",
        paid_at=datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc),
    )
    waiting_booking_uuid = f"BOOK_UI_REPORTS_DRILLDOWN_WAITING_{uuid.uuid4().hex[:8]}"
    waiting_booking_id = _insert_booking(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        tid=content_tid,
        calendly_booking_uuid=waiting_booking_uuid,
        booked_at=datetime(2026, 3, 8, 10, 0, tzinfo=timezone.utc),
    )
    blocked_booking_uuid = f"BOOK_UI_REPORTS_DRILLDOWN_BLOCKED_{uuid.uuid4().hex[:8]}"
    blocked_booking_id = _insert_booking(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        tid=content_tid,
        calendly_booking_uuid=blocked_booking_uuid,
        booked_at=datetime(2026, 3, 8, 11, 0, tzinfo=timezone.utc),
    )
    _insert_blocked_billing_case(
        creator_id=creator["creator_id"],
        booking_id=blocked_booking_id,
        tid=content_tid,
        calendly_booking_uuid=blocked_booking_uuid,
        stripe_account_id="acct_ui_reports_drilldown",
        frozen_amount_cents=19500,
        frozen_currency="USD",
        reason_code="creator_not_billable",
        first_blocked_at=datetime(2026, 3, 8, 11, 5, tzinfo=timezone.utc),
    )
    _insert_unmatched_payment_event(
        creator_id=creator["creator_id"],
        stripe_account_id="acct_ui_reports_drilldown",
        stripe_event_id=f"evt_ui_reports_drilldown_unmatched_{uuid.uuid4().hex[:8]}",
        stripe_invoice_id=f"in_ui_reports_drilldown_unmatched_{uuid.uuid4().hex[:8]}",
        reason=UNATTRIBUTED_REASON_UNKNOWN_STRIPE_INVOICE_ID,
        paid_at=datetime(2026, 3, 8, 11, 30, tzinfo=timezone.utc),
        booking_id=waiting_booking_id,
        tid=content_tid,
    )
    _insert_unmatched_payment_event(
        creator_id=creator["creator_id"],
        stripe_account_id="acct_ui_reports_drilldown",
        stripe_event_id=f"evt_ui_reports_drilldown_missing_tid_{uuid.uuid4().hex[:8]}",
        stripe_invoice_id=f"in_ui_reports_drilldown_missing_tid_{uuid.uuid4().hex[:8]}",
        reason=UNATTRIBUTED_REASON_MISSING_TID,
        paid_at=datetime(2026, 3, 8, 11, 40, tzinfo=timezone.utc),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get(
            f"/app/reports/content/{content_tid}",
            params={"start_date": "2026-03-08", "end_date": "2026-03-08"},
            headers=HTML_ACCEPT_HEADERS,
        )

    assert response.status_code == 200
    assert "Content funnel drilldown" in response.text
    assert source_url in response.text
    assert content_tid in response.text
    assert "Drilldown Strategy" in response.text
    assert paid_booking_uuid in response.text
    assert waiting_booking_uuid in response.text
    assert blocked_booking_uuid in response.text
    assert "Paid outcomes counted in this window" in response.text
    assert "Counted in this view" in response.text
    assert "195.00" in response.text
    assert "Creator not billable" in response.text
    assert "Unknown invoice" in response.text
    assert "Missing tracking ID" not in response.text
    assert (
        f'href="/app/reports/explanations/paid/{content_tid}?start_date=2026-03-08&amp;end_date=2026-03-08"'
        in response.text
    )
    assert (
        'href="/app/reports?start_date=2026-03-08&amp;end_date=2026-03-08"'
        in response.text
    )


def test_reports_content_drilldown_page_returns_404_for_other_creators_row():
    creator_a = _insert_creator_user(
        email=f"ui_reports_drilldown_a_{uuid.uuid4().hex}@example.com",
        name="Reports Drilldown Creator A",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_reports_drilldown_a",
    )
    creator_b = _insert_creator_user(
        email=f"ui_reports_drilldown_b_{uuid.uuid4().hex}@example.com",
        name="Reports Drilldown Creator B",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_reports_drilldown_b",
    )
    access_token_b = _access_token(
        user_id=creator_b["user_id"],
        creator_id=creator_b["creator_id"],
        email=creator_b["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=creator_a["creator_id"],
        name="Drilldown Isolation Strategy",
        calendly_url="https://calendly.com/example/drilldown-isolation",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    content_tid = f"uireportsdrilldownhidden{uuid.uuid4().hex[:8]}"

    _insert_content(
        creator_id=creator_a["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/reports-drilldown-hidden",
        tid=content_tid,
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token_b)
        response = client.get(
            f"/app/reports/content/{content_tid}",
            headers=HTML_ACCEPT_HEADERS,
        )

    assert response.status_code == 404
    assert response.json() == {"detail": "report drilldown not found"}


def test_reports_unattributed_explanation_page_renders_current_backlog_reason():
    creator = _insert_creator_user(
        email=f"ui_reports_unattributed_{uuid.uuid4().hex}@example.com",
        name="Reports Unattributed Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_reports_unattributed",
    )
    access_token = _access_token(
        user_id=creator["user_id"],
        creator_id=creator["creator_id"],
        email=creator["email"],
        expires_delta=timedelta(hours=24),
    )
    _insert_unmatched_payment_event(
        creator_id=creator["creator_id"],
        stripe_account_id="acct_ui_reports_unattributed",
        stripe_event_id=f"evt_ui_reports_unattributed_{uuid.uuid4().hex[:8]}",
        stripe_invoice_id=f"in_ui_reports_unattributed_{uuid.uuid4().hex[:8]}",
        reason=UNATTRIBUTED_REASON_MISSING_TID,
        paid_at=datetime(2026, 3, 8, 11, 0, tzinfo=timezone.utc),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get(
            "/app/reports/explanations/unattributed",
            params={"start_date": "2026-03-08", "end_date": "2026-03-08"},
            headers=HTML_ACCEPT_HEADERS,
        )

    assert response.status_code == 200
    assert "Why some payments stay outside totals" in response.text
    assert "Diagnostic only" in response.text
    assert "No revenue estimate" in response.text
    assert "Missing tracking ID" in response.text
    assert "Use the tracked link consistently going forward." in response.text
    assert (
        'href="/app/reports?start_date=2026-03-08&amp;end_date=2026-03-08"'
        in response.text
    )


def test_reports_csv_export_uses_same_filtered_creator_scoped_dataset():
    creator_a = _insert_creator_user(
        email=f"ui_reports_export_a_{uuid.uuid4().hex}@example.com",
        name="Reports Export Creator A",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_reports_export_a",
    )
    creator_b = _insert_creator_user(
        email=f"ui_reports_export_b_{uuid.uuid4().hex}@example.com",
        name="Reports Export Creator B",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_reports_export_b",
    )
    access_token = _access_token(
        user_id=creator_a["user_id"],
        creator_id=creator_a["creator_id"],
        email=creator_a["email"],
        expires_delta=timedelta(hours=24),
    )

    creator_a_booking_link_id = _insert_booking_link(
        creator_id=creator_a["creator_id"],
        name="Export Creator A Strategy",
        calendly_url="https://calendly.com/example/export-creator-a-strategy",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    old_tid = f"uireportsexportold{uuid.uuid4().hex[:8]}"
    current_tid = f"uireportsexportcurrent{uuid.uuid4().hex[:8]}"
    _insert_content(
        creator_id=creator_a["creator_id"],
        booking_link_id=creator_a_booking_link_id,
        source_url="https://example.com/posts/reports-export-old",
        tid=old_tid,
    )
    _insert_content(
        creator_id=creator_a["creator_id"],
        booking_link_id=creator_a_booking_link_id,
        source_url="https://example.com/posts/reports-export-current",
        tid=current_tid,
    )
    old_booking_id = _insert_booking(
        creator_id=creator_a["creator_id"],
        booking_link_id=creator_a_booking_link_id,
        tid=old_tid,
        calendly_booking_uuid=f"BOOK_UI_REPORTS_EXPORT_OLD_{uuid.uuid4().hex[:8]}",
        booked_at=datetime(2026, 3, 7, 12, 0, tzinfo=timezone.utc),
    )
    current_booking_id = _insert_booking(
        creator_id=creator_a["creator_id"],
        booking_link_id=creator_a_booking_link_id,
        tid=current_tid,
        calendly_booking_uuid=f"BOOK_UI_REPORTS_EXPORT_CURRENT_{uuid.uuid4().hex[:8]}",
        booked_at=datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc),
    )
    _insert_invoice(
        creator_id=creator_a["creator_id"],
        booking_id=old_booking_id,
        tid=old_tid,
        stripe_account_id="acct_ui_reports_export_a",
        stripe_invoice_id=f"in_ui_reports_export_old_{uuid.uuid4().hex[:8]}",
        amount_cents=5000,
        paid_at=datetime(2026, 3, 7, 13, 0, tzinfo=timezone.utc),
    )
    _insert_invoice(
        creator_id=creator_a["creator_id"],
        booking_id=current_booking_id,
        tid=current_tid,
        stripe_account_id="acct_ui_reports_export_a",
        stripe_invoice_id=f"in_ui_reports_export_current_{uuid.uuid4().hex[:8]}",
        amount_cents=19500,
        paid_at=datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc),
    )

    creator_b_booking_link_id = _insert_booking_link(
        creator_id=creator_b["creator_id"],
        name="Export Creator B Strategy",
        calendly_url="https://calendly.com/example/export-creator-b-strategy",
        billing_amount_cents=88000,
        billing_currency="USD",
    )
    hidden_tid = f"uireportsexporthidden{uuid.uuid4().hex[:8]}"
    _insert_content(
        creator_id=creator_b["creator_id"],
        booking_link_id=creator_b_booking_link_id,
        source_url="https://example.com/posts/reports-export-hidden",
        tid=hidden_tid,
    )
    hidden_booking_id = _insert_booking(
        creator_id=creator_b["creator_id"],
        booking_link_id=creator_b_booking_link_id,
        tid=hidden_tid,
        calendly_booking_uuid=f"BOOK_UI_REPORTS_EXPORT_HIDDEN_{uuid.uuid4().hex[:8]}",
        booked_at=datetime(2026, 3, 8, 14, 0, tzinfo=timezone.utc),
    )
    _insert_invoice(
        creator_id=creator_b["creator_id"],
        booking_id=hidden_booking_id,
        tid=hidden_tid,
        stripe_account_id="acct_ui_reports_export_b",
        stripe_invoice_id=f"in_ui_reports_export_hidden_{uuid.uuid4().hex[:8]}",
        amount_cents=88000,
        paid_at=datetime(2026, 3, 8, 15, 0, tzinfo=timezone.utc),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get(
            "/app/reports/export.csv",
            params={"start_date": "2026-03-08", "end_date": "2026-03-08"},
            headers=HTML_ACCEPT_HEADERS,
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="reports-summary-2026-03-08-to-2026-03-08.csv"'
    )
    assert response.text.startswith(
        "content_id,booking_link_id,tid,source_url,booking_count,paid_revenue_cents,paid_invoice_count,paid_booking_count,open_blocked_billing_case_count,funnel_status,first_paid_at,last_paid_at\n"
    )
    assert current_tid in response.text
    assert "https://example.com/posts/reports-export-current" in response.text
    assert "19500" in response.text
    assert "2026-03-08T09:00:00Z" in response.text
    assert old_tid not in response.text
    assert "https://example.com/posts/reports-export-old" not in response.text
    assert hidden_tid not in response.text
    assert "https://example.com/posts/reports-export-hidden" not in response.text


def test_reports_page_without_tracked_content_explains_prerequisite():
    inserted = _insert_creator_user(
        email=f"ui_reports_empty_{uuid.uuid4().hex}@example.com",
        name="No Reporting Content Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_reports_empty",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get("/app/reports", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Billable now comes before paid results" in response.text
    assert "A billing provider is connected, but this workspace is not billable now yet." in response.text
    assert 'href="/app/booking-links"' in response.text


def test_reports_page_with_tracked_content_but_no_paid_invoices_shows_funnel_row():
    inserted = _insert_creator_user(
        email=f"ui_reports_no_paid_{uuid.uuid4().hex}@example.com",
        name="No Paid Reports Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_reports_no_paid",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="No Paid Strategy",
        calendly_url="https://calendly.com/example/no-paid-strategy",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    content_tid = f"uireportsnopaid{uuid.uuid4().hex[:8]}"
    _insert_content(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/reports-no-paid",
        tid=content_tid,
    )
    _insert_booking(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        tid=content_tid,
        calendly_booking_uuid=f"BOOK_UI_REPORTS_NO_PAID_{uuid.uuid4().hex[:8]}",
        booked_at=datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get("/app/reports", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Content funnel summary" in response.text
    assert "reports-no-paid" in response.text
    assert "1 tracked booking" in response.text
    assert "0 paid bookings" in response.text
    assert "Waiting" in response.text
    assert "Bookings exist, but no invoice-backed payment counts yet." in response.text
    assert "No invoice-backed paid result is counted for this content yet." in response.text
    assert "Illustrative preview" not in response.text


def test_experiments_page_without_prior_run_renders_current_unsupported_readiness_and_does_not_write():
    inserted = _insert_creator_user(
        email=f"ui_experiments_empty_{uuid.uuid4().hex}@example.com",
        name="Empty Experiments Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_experiments_empty",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    with _engine().connect() as conn:
        before_count = conn.execute(
            text("SELECT COUNT(*) FROM creator_experiment_runs WHERE creator_id = :creator_id"),
            {"creator_id": inserted["creator_id"]},
        ).scalar_one()

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get("/app/experiments", headers=HTML_ACCEPT_HEADERS)

    with _engine().connect() as conn:
        after_count = conn.execute(
            text("SELECT COUNT(*) FROM creator_experiment_runs WHERE creator_id = :creator_id"),
            {"creator_id": inserted["creator_id"]},
        ).scalar_one()

    assert response.status_code == 200
    assert "Experiments" in response.text
    assert '<a href="/app/experiments" class="nav-link active" aria-current="page">Experiments</a>' in response.text
    assert "Helper is unsupported from current evidence" in response.text
    assert "No stored helper snapshot exists yet" in response.text
    assert "Review content evidence" in response.text
    assert "Review paid results" in response.text
    assert '<button type="submit" class="secondary">Generate next experiments</button>' in response.text
    assert "Refreshing the page does not create a new helper run." in response.text
    assert 'action="/app/experiments"' in response.text
    assert "Operator-only draft experiments" not in response.text
    assert before_count == 0
    assert after_count == 0


def _seed_ready_experiments_workspace(*, email: str, name: str, stripe_account_id: str) -> tuple[dict[str, str], str, str]:
    inserted = _insert_creator_user(
        email=email,
        name=name,
        stripe_connect_status="connected",
        stripe_account_id=stripe_account_id,
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=inserted["creator_id"],
        name=f"{name} Link",
        calendly_url="https://calendly.com/example/operator-ready-no-snapshot",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    content_tid = f"uiexperimentsoperator{uuid.uuid4().hex[:8]}"
    content_id = _insert_content(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/experiments-operator-ready",
        tid=content_tid,
    )
    snapshot_id = _insert_fetch_snapshot(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        requested_url="https://example.com/posts/experiments-operator-ready",
        fetched_url="https://example.com/posts/experiments-operator-ready",
        fetch_status="succeeded",
        http_status=200,
        snapshot_text="<html><body><article><p>Operator ready experiments.</p></article></body></html>",
        fetched_at=datetime(2026, 3, 12, 11, 0, tzinfo=timezone.utc),
    )
    artifact_id = _insert_extraction_artifact(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        fetch_snapshot_id=snapshot_id,
        extraction_status="succeeded",
        title="Operator Ready Artifact",
        extracted_text="Retention review content for operator experiments.",
        created_at=datetime(2026, 3, 12, 11, 5, tzinfo=timezone.utc),
    )
    _insert_confirmed_topic(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        extraction_artifact_id=artifact_id,
        label="Retention Reviews",
    )
    _set_authoritative_extraction_artifact(
        content_id=content_id,
        artifact_id=artifact_id,
    )
    booking_id = _insert_booking(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        tid=content_tid,
        calendly_booking_uuid=f"BOOK_UI_EXPERIMENTS_OPERATOR_{uuid.uuid4().hex[:8]}",
        booked_at=datetime(2026, 3, 12, 12, 0, tzinfo=timezone.utc),
    )
    stripe_invoice_id = f"in_ui_experiments_operator_{uuid.uuid4().hex[:8]}"
    invoice_id = _insert_invoice(
        creator_id=inserted["creator_id"],
        booking_id=booking_id,
        tid=content_tid,
        stripe_account_id=stripe_account_id,
        stripe_invoice_id=stripe_invoice_id,
        amount_cents=19500,
        paid_at=datetime(2026, 3, 12, 13, 0, tzinfo=timezone.utc),
    )
    _insert_matched_payment_event(
        creator_id=inserted["creator_id"],
        booking_id=booking_id,
        tid=content_tid,
        invoice_id=invoice_id,
        stripe_account_id=stripe_account_id,
        stripe_event_id=f"evt_ui_experiments_operator_{uuid.uuid4().hex[:8]}",
        stripe_invoice_id=stripe_invoice_id,
        paid_at=datetime(2026, 3, 12, 13, 0, tzinfo=timezone.utc),
    )
    return inserted, access_token, content_tid


def test_experiments_page_without_prior_run_renders_current_ready_readiness_without_writing():
    inserted = _insert_creator_user(
        email=f"ui_experiments_ready_no_snapshot_{uuid.uuid4().hex}@example.com",
        name="Ready No Snapshot Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_experiments_ready_no_snapshot",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="Ready No Snapshot Link",
        calendly_url="https://calendly.com/example/ready-no-snapshot",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    content_tid = f"uiexperimentsreadynosnapshot{uuid.uuid4().hex[:8]}"
    content_id = _insert_content(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/experiments-ready-no-snapshot",
        tid=content_tid,
    )
    snapshot_id = _insert_fetch_snapshot(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        requested_url="https://example.com/posts/experiments-ready-no-snapshot",
        fetched_url="https://example.com/posts/experiments-ready-no-snapshot",
        fetch_status="succeeded",
        http_status=200,
        snapshot_text="<html><body><article><p>Ready no snapshot.</p></article></body></html>",
        fetched_at=datetime(2026, 3, 12, 11, 0, tzinfo=timezone.utc),
    )
    artifact_id = _insert_extraction_artifact(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        fetch_snapshot_id=snapshot_id,
        extraction_status="succeeded",
        title="Ready No Snapshot Artifact",
        extracted_text="Retention review content for ready-no-snapshot experiments.",
        created_at=datetime(2026, 3, 12, 11, 5, tzinfo=timezone.utc),
    )
    _insert_confirmed_topic(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        extraction_artifact_id=artifact_id,
        label="Retention Reviews",
    )
    _set_authoritative_extraction_artifact(
        content_id=content_id,
        artifact_id=artifact_id,
    )
    booking_id = _insert_booking(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        tid=content_tid,
        calendly_booking_uuid=f"BOOK_UI_EXPERIMENTS_READY_NO_SNAPSHOT_{uuid.uuid4().hex[:8]}",
        booked_at=datetime(2026, 3, 12, 12, 0, tzinfo=timezone.utc),
    )
    stripe_invoice_id = f"in_ui_experiments_ready_no_snapshot_{uuid.uuid4().hex[:8]}"
    invoice_id = _insert_invoice(
        creator_id=inserted["creator_id"],
        booking_id=booking_id,
        tid=content_tid,
        stripe_account_id="acct_ui_experiments_ready_no_snapshot",
        stripe_invoice_id=stripe_invoice_id,
        amount_cents=19500,
        paid_at=datetime(2026, 3, 12, 13, 0, tzinfo=timezone.utc),
    )
    _insert_matched_payment_event(
        creator_id=inserted["creator_id"],
        booking_id=booking_id,
        tid=content_tid,
        invoice_id=invoice_id,
        stripe_account_id="acct_ui_experiments_ready_no_snapshot",
        stripe_event_id=f"evt_ui_experiments_ready_no_snapshot_{uuid.uuid4().hex[:8]}",
        stripe_invoice_id=stripe_invoice_id,
        paid_at=datetime(2026, 3, 12, 13, 0, tzinfo=timezone.utc),
    )

    with _engine().connect() as conn:
        before_count = conn.execute(
            text("SELECT COUNT(*) FROM creator_experiment_runs WHERE creator_id = :creator_id"),
            {"creator_id": inserted["creator_id"]},
        ).scalar_one()

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get("/app/experiments", headers=HTML_ACCEPT_HEADERS)

    with _engine().connect() as conn:
        after_count = conn.execute(
            text("SELECT COUNT(*) FROM creator_experiment_runs WHERE creator_id = :creator_id"),
            {"creator_id": inserted["creator_id"]},
        ).scalar_one()

    assert response.status_code == 200
    assert "Helper is ready from current evidence" in response.text
    assert "No ready snapshot is stored yet" in response.text
    assert (
        "Current evidence is ready, but no stored snapshot exists yet" in response.text
    )
    assert '<button type="submit">Generate next experiments</button>' in response.text
    assert "Operator-only draft experiments" not in response.text
    assert before_count == 0
    assert after_count == 0


def test_experiments_page_shows_operator_draft_section_for_allowlisted_ready_operator():
    inserted, access_token, _ = _seed_ready_experiments_workspace(
        email=f"ui_experiments_operator_{uuid.uuid4().hex}@example.com",
        name="Operator Draft Creator",
        stripe_account_id="acct_ui_experiments_operator",
    )
    settings = _operator_allowlist_settings(inserted["email"])
    provider = _StubOperatorExperimentDraftProvider(cards=[])

    with _override_app_state("settings", settings):
        with _override_app_state("operator_experiment_draft_provider", provider):
            with TestClient(app) as client:
                client.cookies.set(SESSION_COOKIE_NAME, access_token)
                response = client.get("/app/experiments", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Operator-only draft experiments" in response.text
    assert "Review non-canonical LLM draft hypotheses beside the shipped helper" in response.text
    assert "Generate operator draft" in response.text
    assert 'action="/app/operator/experiments/drafts"' in response.text
    assert "No operator-only draft snapshot exists yet" in response.text


def test_operator_experiment_draft_generate_route_creates_run_and_renders_results():
    inserted, access_token, content_tid = _seed_ready_experiments_workspace(
        email=f"ui_experiments_operator_generate_{uuid.uuid4().hex}@example.com",
        name="Operator Draft Generate Creator",
        stripe_account_id="acct_ui_experiments_operator_generate",
    )
    settings = _operator_allowlist_settings(inserted["email"])
    provider = _StubOperatorExperimentDraftProvider(
        cards=[
            _OperatorExperimentDraftCardPayload(
                content_tid=content_tid,
                title="Test another retention-review proof angle",
                hypothesis="Test whether another retention-review proof post may drive more attributed paid bookings.",
                why_this_might_work="The authoritative retention-review pattern already has a settled paid result to build on.",
                evidence_summary="Retention Reviews already has one paid booking and one paid invoice totaling USD 195.00.",
                ranking_rationale="It is the only supported candidate in this operator draft set, so it remains first.",
            )
        ]
    )

    with _override_app_state("settings", settings):
        with _override_app_state("operator_experiment_draft_provider", provider):
            with TestClient(app) as client:
                client.cookies.set(SESSION_COOKIE_NAME, access_token)
                create_response = client.post(
                    "/app/operator/experiments/drafts",
                    headers=HTML_ACCEPT_HEADERS,
                    follow_redirects=False,
                )
                page_response = client.get(
                    create_response.headers["location"],
                    headers=HTML_ACCEPT_HEADERS,
                )

    with _engine().connect() as conn:
        run_count = conn.execute(
            text(
                "SELECT COUNT(*) FROM creator_operator_experiment_draft_runs WHERE creator_id = :creator_id"
            ),
            {"creator_id": inserted["creator_id"]},
        ).scalar_one()
        card_count = conn.execute(
            text(
                "SELECT COUNT(*) FROM creator_operator_experiment_draft_run_cards WHERE run_id IN ("
                "SELECT id FROM creator_operator_experiment_draft_runs WHERE creator_id = :creator_id"
                ")"
            ),
            {"creator_id": inserted["creator_id"]},
        ).scalar_one()

    assert create_response.status_code == 303
    assert create_response.headers["location"].startswith(
        "/app/experiments?status=operator-draft-generated&operator_draft_run_id="
    )
    assert page_response.status_code == 200
    assert "Operator draft ready" in page_response.text
    assert "Stored operator draft" in page_response.text
    assert "Test another retention-review proof angle" in page_response.text
    assert "Operator-only draft experiments" in page_response.text
    assert "Draft run lineage" in page_response.text
    assert run_count == 1
    assert card_count == 1


def test_experiments_generate_route_creates_ready_snapshot_and_renders_cards():
    inserted = _insert_creator_user(
        email=f"ui_experiments_ready_{uuid.uuid4().hex}@example.com",
        name="Ready Experiments Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_experiments_ready",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="Experiments Strategy",
        calendly_url="https://calendly.com/example/experiments-strategy",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    content_tid = f"uiexperimentsready{uuid.uuid4().hex[:8]}"
    content_id = _insert_content(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/experiments-ready",
        tid=content_tid,
    )
    snapshot_id = _insert_fetch_snapshot(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        requested_url="https://example.com/posts/experiments-ready",
        fetched_url="https://example.com/posts/experiments-ready",
        fetch_status="succeeded",
        http_status=200,
        snapshot_text="<html><body><article><p>Experiments ready.</p></article></body></html>",
        fetched_at=datetime(2026, 3, 12, 11, 0, tzinfo=timezone.utc),
    )
    artifact_id = _insert_extraction_artifact(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        fetch_snapshot_id=snapshot_id,
        extraction_status="succeeded",
        title="Experiments Ready Artifact",
        extracted_text="Retention review content for ready experiments.",
        created_at=datetime(2026, 3, 12, 11, 5, tzinfo=timezone.utc),
    )
    _insert_confirmed_topic(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        extraction_artifact_id=artifact_id,
        label="Retention Reviews",
    )
    _set_authoritative_extraction_artifact(
        content_id=content_id,
        artifact_id=artifact_id,
    )
    booking_id = _insert_booking(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        tid=content_tid,
        calendly_booking_uuid=f"BOOK_UI_EXPERIMENTS_READY_{uuid.uuid4().hex[:8]}",
        booked_at=datetime(2026, 3, 12, 12, 0, tzinfo=timezone.utc),
    )
    stripe_invoice_id = f"in_ui_experiments_ready_{uuid.uuid4().hex[:8]}"
    invoice_id = _insert_invoice(
        creator_id=inserted["creator_id"],
        booking_id=booking_id,
        tid=content_tid,
        stripe_account_id="acct_ui_experiments_ready",
        stripe_invoice_id=stripe_invoice_id,
        amount_cents=19500,
        paid_at=datetime(2026, 3, 12, 13, 0, tzinfo=timezone.utc),
    )
    _insert_matched_payment_event(
        creator_id=inserted["creator_id"],
        booking_id=booking_id,
        tid=content_tid,
        invoice_id=invoice_id,
        stripe_account_id="acct_ui_experiments_ready",
        stripe_event_id=f"evt_ui_experiments_ready_{uuid.uuid4().hex[:8]}",
        stripe_invoice_id=stripe_invoice_id,
        paid_at=datetime(2026, 3, 12, 13, 0, tzinfo=timezone.utc),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        create_response = client.post(
            "/app/experiments",
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )
        page_response = client.get(create_response.headers["location"], headers=HTML_ACCEPT_HEADERS)
        parsed_location = urlparse(create_response.headers["location"])
        run_claim_snapshot_id = parse_qs(parsed_location.query)["claim_snapshot_id"][0]

    with _engine().connect() as conn:
        run_count = conn.execute(
            text("SELECT COUNT(*) FROM creator_experiment_runs WHERE creator_id = :creator_id"),
            {"creator_id": inserted["creator_id"]},
        ).scalar_one()
        card_id = conn.execute(
            text(
                "SELECT card_id FROM creator_experiment_run_cards "
                "WHERE run_id = :run_id AND card_order = 1"
            ),
            {"run_id": run_claim_snapshot_id},
        ).scalar_one()

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        evidence_response = client.get(
            f"/app/experiments/{run_claim_snapshot_id}/cards/by-id/{card_id}",
            headers=HTML_ACCEPT_HEADERS,
        )

    assert create_response.status_code == 303
    assert create_response.headers["location"].startswith(
        "/app/experiments?status=generated&claim_snapshot_id="
    )
    assert page_response.status_code == 200
    assert "Fresh snapshot ready" in page_response.text
    assert "Helper is ready from current evidence" in page_response.text
    assert "The latest stored snapshot is already ready." in page_response.text
    assert "Here is the next content experiment most grounded" in page_response.text
    assert "Test another Retention Reviews angle" in page_response.text
    assert "Test whether another post about Retention Reviews may lead to more attributed paid bookings." in page_response.text
    assert "Claim snapshot" in page_response.text
    assert "Run lineage" in page_response.text
    assert "Version semantics" in page_response.text
    assert "Freshness policy" in page_response.text
    assert "Why this is ranked here" in page_response.text
    assert "only supported pattern in your current snapshot" in page_response.text
    assert "deterministic_rules" in page_response.text
    assert "next_content_experiments.helper_config.v2" in page_response.text
    assert "next_content_experiments.result.v2" in page_response.text
    assert "next_content_experiments.snapshot_inputs.v1" in page_response.text
    assert "next_content_experiments.freshness_policy.v1" in page_response.text
    assert f'href="/app/experiments/{run_claim_snapshot_id}/cards/by-id/{card_id}"' in page_response.text
    assert "<code>" in page_response.text
    assert evidence_response.status_code == 200
    assert "Experiment evidence" in evidence_response.text
    assert "Authoritative content used" in evidence_response.text
    assert "Settled paid results used" in evidence_response.text
    assert "Parent run snapshot" in evidence_response.text
    assert "Card snapshot" in evidence_response.text
    assert "Card ID" in evidence_response.text
    assert "Card lineage" in evidence_response.text
    assert "Version semantics" in evidence_response.text
    assert "Freshness policy" in evidence_response.text
    assert "Why this is ranked here" in evidence_response.text
    assert "only supported pattern in your current snapshot" in evidence_response.text
    assert "next_content_experiment_card.rendering_config.v2" in evidence_response.text
    assert "Experiments Ready Artifact" in evidence_response.text
    assert content_tid in evidence_response.text
    assert "USD 195.00" in evidence_response.text
    assert stripe_invoice_id not in evidence_response.text
    assert run_count == 1


def test_experiments_generate_route_renders_unsupported_state_without_generic_tips():
    inserted = _insert_creator_user(
        email=f"ui_experiments_unsupported_{uuid.uuid4().hex}@example.com",
        name="Unsupported Experiments Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_experiments_unsupported",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="Unsupported Experiments Strategy",
        calendly_url="https://calendly.com/example/experiments-unsupported",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    content_id = _insert_content(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/experiments-unsupported",
        tid=f"uiexperimentsunsupported{uuid.uuid4().hex[:8]}",
    )
    snapshot_id = _insert_fetch_snapshot(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        requested_url="https://example.com/posts/experiments-unsupported",
        fetched_url="https://example.com/posts/experiments-unsupported",
        fetch_status="succeeded",
        http_status=200,
        snapshot_text="<html><body><article><p>Unsupported experiments.</p></article></body></html>",
        fetched_at=datetime(2026, 3, 12, 11, 0, tzinfo=timezone.utc),
    )
    artifact_id = _insert_extraction_artifact(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        fetch_snapshot_id=snapshot_id,
        extraction_status="succeeded",
        title="Experiments Unsupported Artifact",
        extracted_text="Unsupported experiments content.",
        created_at=datetime(2026, 3, 12, 11, 5, tzinfo=timezone.utc),
    )
    _insert_confirmed_topic(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        extraction_artifact_id=artifact_id,
        label="Discovery Calls",
    )
    _set_authoritative_extraction_artifact(
        content_id=content_id,
        artifact_id=artifact_id,
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        create_response = client.post(
            "/app/experiments",
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )
        page_response = client.get(create_response.headers["location"], headers=HTML_ACCEPT_HEADERS)

    assert create_response.status_code == 303
    assert page_response.status_code == 200
    assert "Helper is unsupported from current evidence" in page_response.text
    assert "Not enough trusted evidence yet" in page_response.text
    assert UNSUPPORTED_EXPERIMENTS_SUMMARY in page_response.text
    assert "Why this helper is still unsupported" in page_response.text
    assert "No settled attributed paid results exist yet for this workspace." in page_response.text
    assert "generic fallback tips" not in page_response.text
    assert "Test whether another post about" not in page_response.text


def test_experiments_page_leads_with_current_unsupported_readiness_over_historical_ready_snapshot():
    inserted = _insert_creator_user(
        email=f"ui_experiments_historical_ready_{uuid.uuid4().hex}@example.com",
        name="Historical Ready Experiments Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_experiments_historical_ready",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="Historical Ready Experiments Strategy",
        calendly_url="https://calendly.com/example/experiments-historical-ready",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    content_tid = f"uiexperimentshistoricalready{uuid.uuid4().hex[:8]}"
    content_id = _insert_content(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/experiments-historical-ready",
        tid=content_tid,
    )
    snapshot_id = _insert_fetch_snapshot(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        requested_url="https://example.com/posts/experiments-historical-ready",
        fetched_url="https://example.com/posts/experiments-historical-ready",
        fetch_status="succeeded",
        http_status=200,
        snapshot_text="<html><body><article><p>Historical ready experiments.</p></article></body></html>",
        fetched_at=datetime(2026, 3, 12, 11, 0, tzinfo=timezone.utc),
    )
    artifact_id = _insert_extraction_artifact(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        fetch_snapshot_id=snapshot_id,
        extraction_status="succeeded",
        title="Historical Ready Artifact",
        extracted_text="Retention review content for historical-ready experiments.",
        created_at=datetime(2026, 3, 12, 11, 5, tzinfo=timezone.utc),
    )
    _insert_confirmed_topic(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        extraction_artifact_id=artifact_id,
        label="Retention Reviews",
    )
    _set_authoritative_extraction_artifact(
        content_id=content_id,
        artifact_id=artifact_id,
    )
    booking_id = _insert_booking(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        tid=content_tid,
        calendly_booking_uuid=f"BOOK_UI_EXPERIMENTS_HISTORICAL_READY_{uuid.uuid4().hex[:8]}",
        booked_at=datetime(2026, 3, 12, 12, 0, tzinfo=timezone.utc),
    )
    stripe_invoice_id = f"in_ui_experiments_historical_ready_{uuid.uuid4().hex[:8]}"
    invoice_id = _insert_invoice(
        creator_id=inserted["creator_id"],
        booking_id=booking_id,
        tid=content_tid,
        stripe_account_id="acct_ui_experiments_historical_ready",
        stripe_invoice_id=stripe_invoice_id,
        amount_cents=19500,
        paid_at=datetime(2026, 3, 12, 13, 0, tzinfo=timezone.utc),
    )
    _insert_matched_payment_event(
        creator_id=inserted["creator_id"],
        booking_id=booking_id,
        tid=content_tid,
        invoice_id=invoice_id,
        stripe_account_id="acct_ui_experiments_historical_ready",
        stripe_event_id=f"evt_ui_experiments_historical_ready_{uuid.uuid4().hex[:8]}",
        stripe_invoice_id=stripe_invoice_id,
        paid_at=datetime(2026, 3, 12, 13, 0, tzinfo=timezone.utc),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        create_response = client.post(
            "/app/experiments",
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )

    _set_authoritative_extraction_artifact(
        content_id=content_id,
        artifact_id=None,
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        page_response = client.get("/app/experiments", headers=HTML_ACCEPT_HEADERS)

    assert create_response.status_code == 303
    assert page_response.status_code == 200
    assert "Helper is unsupported from current evidence" in page_response.text
    assert "Open latest ready snapshot" in page_response.text
    assert "historical artifact" in page_response.text
    assert "This ready snapshot is historical stored output." in page_response.text
    assert "Here is the next content experiment most grounded" in page_response.text


def test_experiments_compare_page_renders_two_stored_runs_with_lineage():
    inserted = _insert_creator_user(
        email=f"ui_experiments_compare_{uuid.uuid4().hex}@example.com",
        name="Compare Experiments Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_experiments_compare",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="Compare Experiments Strategy",
        calendly_url="https://calendly.com/example/experiments-compare",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    content_tid = f"ui_experiments_compare_{uuid.uuid4().hex[:8]}"
    content_id = _insert_content(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        source_url=f"https://example.com/posts/{content_tid}",
        tid=content_tid,
    )
    snapshot_id = _insert_fetch_snapshot(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        requested_url=f"https://example.com/posts/{content_tid}",
        fetched_url=f"https://example.com/posts/{content_tid}",
        fetch_status="succeeded",
        http_status=200,
        snapshot_text="<html><body><article><p>Compare experiments.</p></article></body></html>",
        fetched_at=datetime(2026, 3, 12, 11, 0, tzinfo=timezone.utc),
    )
    artifact_id = _insert_extraction_artifact(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        fetch_snapshot_id=snapshot_id,
        extraction_status="succeeded",
        title="Experiments Compare Artifact",
        extracted_text="Compare experiments content.",
        created_at=datetime(2026, 3, 12, 11, 5, tzinfo=timezone.utc),
    )
    _insert_confirmed_topic(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        extraction_artifact_id=artifact_id,
        label="Retention Reviews",
    )
    _set_authoritative_extraction_artifact(
        content_id=content_id,
        artifact_id=artifact_id,
    )
    booking_id = _insert_booking(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        tid=content_tid,
        calendly_booking_uuid=f"BOOK_UI_EXPERIMENTS_COMPARE_{uuid.uuid4().hex[:8]}",
        booked_at=datetime(2026, 3, 12, 12, 0, tzinfo=timezone.utc),
    )
    stripe_invoice_id = f"in_ui_experiments_compare_{uuid.uuid4().hex[:8]}"
    invoice_id = _insert_invoice(
        creator_id=inserted["creator_id"],
        booking_id=booking_id,
        tid=content_tid,
        stripe_account_id="acct_ui_experiments_compare",
        stripe_invoice_id=stripe_invoice_id,
        amount_cents=19500,
        paid_at=datetime(2026, 3, 12, 13, 0, tzinfo=timezone.utc),
    )
    _insert_matched_payment_event(
        creator_id=inserted["creator_id"],
        booking_id=booking_id,
        tid=content_tid,
        invoice_id=invoice_id,
        stripe_account_id="acct_ui_experiments_compare",
        stripe_event_id=f"evt_ui_experiments_compare_{uuid.uuid4().hex[:8]}",
        stripe_invoice_id=stripe_invoice_id,
        paid_at=datetime(2026, 3, 12, 13, 0, tzinfo=timezone.utc),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        baseline_response = client.post(
            "/app/experiments",
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )
        candidate_response = client.post(
            "/app/experiments",
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )
        baseline_run_claim_snapshot_id = parse_qs(
            urlparse(baseline_response.headers["location"]).query
        )["claim_snapshot_id"][0]
        candidate_run_claim_snapshot_id = parse_qs(
            urlparse(candidate_response.headers["location"]).query
        )["claim_snapshot_id"][0]
        compare_response = client.get(
            "/app/experiments/compare"
            f"?baseline_claim_snapshot_id={baseline_run_claim_snapshot_id}"
            f"&candidate_claim_snapshot_id={candidate_run_claim_snapshot_id}",
            headers=HTML_ACCEPT_HEADERS,
        )

    assert compare_response.status_code == 200
    assert "Experiment comparison" in compare_response.text
    assert "Historical vs current helper output" in compare_response.text
    assert "Baseline snapshot" in compare_response.text
    assert "Candidate snapshot" in compare_response.text
    assert baseline_run_claim_snapshot_id in compare_response.text
    assert candidate_run_claim_snapshot_id in compare_response.text
    assert "Run lineage" in compare_response.text
    assert "Version semantics" in compare_response.text
    assert "Freshness policy" in compare_response.text
    assert "Card lineage" in compare_response.text
    assert "Stable card ID" in compare_response.text
    assert "Why this is ranked here" in compare_response.text
    assert "only supported pattern in your current snapshot" in compare_response.text
    assert "deterministic_rules" in compare_response.text
    assert "next_content_experiments.helper_config.v2" in compare_response.text
    assert "next_content_experiments.result.v2" in compare_response.text
    assert "next_content_experiments.snapshot_inputs.v1" in compare_response.text
    assert "next_content_experiments.freshness_policy.v1" in compare_response.text
    assert "next_content_experiment_card.rendering_config.v2" in compare_response.text


def test_experiments_pages_render_legacy_null_lineage_as_not_recorded():
    inserted = _insert_creator_user(
        email=f"ui_experiments_legacy_{uuid.uuid4().hex}@example.com",
        name="Legacy Experiments Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_experiments_legacy",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="Legacy Experiments Strategy",
        calendly_url="https://calendly.com/example/experiments-legacy",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    content_tid = f"ui_experiments_legacy_{uuid.uuid4().hex[:8]}"
    content_id = _insert_content(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        source_url=f"https://example.com/posts/{content_tid}",
        tid=content_tid,
    )
    snapshot_id = _insert_fetch_snapshot(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        requested_url=f"https://example.com/posts/{content_tid}",
        fetched_url=f"https://example.com/posts/{content_tid}",
        fetch_status="succeeded",
        http_status=200,
        snapshot_text="<html><body><article><p>Legacy experiments.</p></article></body></html>",
        fetched_at=datetime(2026, 3, 12, 11, 0, tzinfo=timezone.utc),
    )
    artifact_id = _insert_extraction_artifact(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        fetch_snapshot_id=snapshot_id,
        extraction_status="succeeded",
        title="Experiments Legacy Artifact",
        extracted_text="Legacy experiments content.",
        created_at=datetime(2026, 3, 12, 11, 5, tzinfo=timezone.utc),
    )
    _insert_confirmed_topic(
        content_id=content_id,
        creator_id=inserted["creator_id"],
        extraction_artifact_id=artifact_id,
        label="Retention Reviews",
    )
    _set_authoritative_extraction_artifact(
        content_id=content_id,
        artifact_id=artifact_id,
    )
    booking_id = _insert_booking(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        tid=content_tid,
        calendly_booking_uuid=f"BOOK_UI_EXPERIMENTS_LEGACY_{uuid.uuid4().hex[:8]}",
        booked_at=datetime(2026, 3, 12, 12, 0, tzinfo=timezone.utc),
    )
    stripe_invoice_id = f"in_ui_experiments_legacy_{uuid.uuid4().hex[:8]}"
    invoice_id = _insert_invoice(
        creator_id=inserted["creator_id"],
        booking_id=booking_id,
        tid=content_tid,
        stripe_account_id="acct_ui_experiments_legacy",
        stripe_invoice_id=stripe_invoice_id,
        amount_cents=19500,
        paid_at=datetime(2026, 3, 12, 13, 0, tzinfo=timezone.utc),
    )
    _insert_matched_payment_event(
        creator_id=inserted["creator_id"],
        booking_id=booking_id,
        tid=content_tid,
        invoice_id=invoice_id,
        stripe_account_id="acct_ui_experiments_legacy",
        stripe_event_id=f"evt_ui_experiments_legacy_{uuid.uuid4().hex[:8]}",
        stripe_invoice_id=stripe_invoice_id,
        paid_at=datetime(2026, 3, 12, 13, 0, tzinfo=timezone.utc),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        create_response = client.post(
            "/app/experiments",
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )
        run_claim_snapshot_id = parse_qs(urlparse(create_response.headers["location"]).query)[
            "claim_snapshot_id"
        ][0]

    with _engine().begin() as conn:
        conn.execute(
            text(
                "UPDATE creator_experiment_runs "
                "SET run_generator_type = NULL, run_model_name = NULL, run_config_version = NULL "
                "WHERE id = :run_id"
            ),
            {"run_id": run_claim_snapshot_id},
        )
        conn.execute(
            text(
                "UPDATE creator_experiment_run_cards "
                "SET card_id = NULL, ranking_rationale = NULL "
                "WHERE run_id = :run_id"
            ),
            {"run_id": run_claim_snapshot_id},
        )
        conn.execute(
            text(
                "UPDATE creator_claim_snapshots "
                "SET claim_generator_type = NULL, claim_model_name = NULL, claim_config_version = NULL "
                "WHERE id IN ("
                "  SELECT claim_snapshot_id "
                "  FROM creator_experiment_run_cards "
                "  WHERE run_id = :run_id"
                ")"
            ),
            {"run_id": run_claim_snapshot_id},
        )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        page_response = client.get(
            f"/app/experiments?claim_snapshot_id={run_claim_snapshot_id}",
            headers=HTML_ACCEPT_HEADERS,
        )
        evidence_response = client.get(
            f"/app/experiments/{run_claim_snapshot_id}/cards/1",
            headers=HTML_ACCEPT_HEADERS,
        )

    assert page_response.status_code == 200
    assert "Run lineage" in page_response.text
    assert "Version semantics" in page_response.text
    assert "Freshness policy" in page_response.text
    assert "Not recorded" in page_response.text
    assert f'href="/app/experiments/{run_claim_snapshot_id}/cards/1"' in page_response.text
    assert evidence_response.status_code == 200
    assert "Run lineage" in evidence_response.text
    assert "Card lineage" in evidence_response.text
    assert "Version semantics" in evidence_response.text
    assert "Freshness policy" in evidence_response.text
    assert "Why this is ranked here" in evidence_response.text
    assert "Not recorded" in evidence_response.text


def test_setup_home_disconnected_stripe_state_shows_reconnect_cta():
    inserted = _insert_creator_user(
        email=f"ui_disconnected_{uuid.uuid4().hex}@example.com",
        name="Disconnected Creator",
        stripe_connect_status="disconnected",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get("/app", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Reconnect billing setup" in response.text
    assert "What do I need to restore before new bookings can move into invoicing?" in response.text
    assert "Reconnect Stripe" in response.text
    assert "Reconnect it before new bookings can move into invoicing." in response.text
    assert 'action="/app/stripe/connect/start"' in response.text


def test_setup_home_disconnected_paypal_state_keeps_reconnect_for_allowlisted_operator():
    inserted = _insert_creator_user(
        email=f"ui_paypal_disconnected_{uuid.uuid4().hex}@example.com",
        name="Disconnected PayPal Creator",
        stripe_connect_status="pending",
        billing_provider="paypal",
        billing_connect_status="disconnected",
        billing_account_id="merchant_ui_disconnected",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    settings = _paypal_operator_only_settings(inserted["email"], environment="sandbox")

    with _override_app_state("settings", settings):
        with TestClient(app) as client:
            client.cookies.set(SESSION_COOKIE_NAME, access_token)
            response = client.get("/app", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Reconnect billing setup" in response.text
    assert "This workspace was connected to PayPal before, but it is disconnected now." in response.text
    assert "Reconnect PayPal" in response.text
    assert 'action="/app/paypal/connect/start"' in response.text


def test_setup_home_disconnected_paypal_state_hides_reconnect_for_non_operator_live_creator():
    inserted = _insert_creator_user(
        email=f"ui_paypal_disconnected_live_{uuid.uuid4().hex}@example.com",
        name="Disconnected Live PayPal Creator",
        stripe_connect_status="pending",
        billing_provider="paypal",
        billing_connect_status="disconnected",
        billing_account_id="merchant_ui_disconnected_live",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    settings = _live_paypal_operator_only_settings("ops@creatortrust.co")

    with _override_app_state("settings", settings):
        with TestClient(app) as client:
            client.cookies.set(SESSION_COOKIE_NAME, access_token)
            response = client.get("/app", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Reconnect billing setup" in response.text
    assert "PayPal setup is not yet available for general creators." in response.text
    assert "Reconnect PayPal" not in response.text
    assert 'action="/app/paypal/connect/start"' not in response.text


def test_setup_home_connected_stripe_state_shows_connected_details():
    connected_at = datetime.now(timezone.utc).replace(microsecond=0)
    inserted = _insert_creator_user(
        email=f"ui_connected_{uuid.uuid4().hex}@example.com",
        name="Connected Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_connected",
        stripe_connected_at=connected_at,
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get("/app", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Connected, but not billable now" in response.text
    assert "Save a booking link" in response.text
    assert "Open booking links" in response.text
    assert "acct_ui_connected" in response.text
    assert 'action="/app/stripe/connect/start"' not in response.text
    assert "Billing account" in response.text


def test_account_page_connected_state_keeps_switch_entry_point_for_allowlisted_operator():
    connected_at = datetime.now(timezone.utc).replace(microsecond=0)
    inserted = _insert_creator_user(
        email=f"ui_account_connected_{uuid.uuid4().hex}@example.com",
        name="Account Connected Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_account_connected",
        stripe_connected_at=connected_at,
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="Account Strategy",
        calendly_url="https://calendly.com/example/account-strategy",
        billing_amount_cents=19500,
        billing_currency="USD",
    )

    settings = _paypal_operator_only_settings(inserted["email"], environment="sandbox")

    with _override_app_state("settings", settings):
        with TestClient(app) as client:
            client.cookies.set(SESSION_COOKIE_NAME, access_token)
            response = client.get("/app/account", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Account settings" in response.text
    assert "Billing setup" in response.text
    assert "Current provider" in response.text
    assert "Current status" in response.text
    assert "Next safe action" in response.text
    assert "Account context" in response.text
    assert '<a href="/app/account" class="nav-link active" aria-current="page">Account</a>' in response.text
    assert "Current workspace" in response.text
    assert inserted["email"] in response.text
    assert "Use the sign-out button above to end this browser session only." in response.text
    assert 'action="/sign-out"' in response.text
    assert "This workspace has a connected billing provider and is billable now for future invoicing." in response.text
    assert "Changing the billing connection affects future billing readiness." in response.text
    assert "acct_ui_account_connected" in response.text
    assert 'class="wrap-anywhere">acct_ui_account_connected</span>' in response.text
    assert "Start PayPal switch" in response.text
    assert 'action="/app/paypal/connect/start"' in response.text
    assert "Stripe stays active until PayPal is connected, ready, and you commit the switch." in response.text
    assert "Manage which booking links stay active for future tracked traffic and bookings." in response.text
    assert "1 saved booking link" in response.text
    assert 'href="/app/booking-links"' in response.text
    assert "Danger zone" in response.text
    assert "Request workspace reset" in response.text
    assert "Request account deletion" in response.text
    assert 'href="/app/account?confirm=workspace-reset#danger-zone"' in response.text
    assert 'href="/app/account?confirm=account-deletion#danger-zone"' in response.text
    assert "Submit reset request" not in response.text
    assert "Submit deletion request" not in response.text


def test_account_page_connected_state_hides_paypal_switch_for_non_operator_live_creator():
    connected_at = datetime.now(timezone.utc).replace(microsecond=0)
    inserted = _insert_creator_user(
        email=f"ui_account_connected_live_{uuid.uuid4().hex}@example.com",
        name="Account Connected Live Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_account_connected_live",
        stripe_connected_at=connected_at,
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="Live Strategy",
        calendly_url="https://calendly.com/example/live-strategy",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    settings = _live_paypal_operator_only_settings("ops@creatortrust.co")

    with _override_app_state("settings", settings):
        with TestClient(app) as client:
            client.cookies.set(SESSION_COOKIE_NAME, access_token)
            response = client.get("/app/account", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "PayPal setup is not yet available for general creators." in response.text
    assert "Start PayPal switch" not in response.text
    assert 'action="/app/paypal/connect/start"' not in response.text


def test_account_page_connected_state_blocks_switch_when_open_invoice_exists():
    connected_at = datetime.now(timezone.utc).replace(microsecond=0)
    inserted = _insert_creator_user(
        email=f"ui_account_switch_blocked_{uuid.uuid4().hex}@example.com",
        name="Account Switch Blocked Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_account_switch_blocked",
        stripe_connected_at=connected_at,
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="Blocked Switch Call",
        calendly_url="https://calendly.com/example/blocked-switch",
        billing_amount_cents=17500,
        billing_currency="USD",
    )
    _insert_content(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/blocked-switch",
        tid="uiblockedswitchtid",
    )
    booking_id = _insert_booking(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        tid="uiblockedswitchtid",
        calendly_booking_uuid=f"BOOK_{uuid.uuid4().hex[:8]}",
        booked_at=datetime(2026, 3, 21, 10, 0, tzinfo=timezone.utc),
    )
    _insert_invoice(
        creator_id=inserted["creator_id"],
        booking_id=booking_id,
        tid="uiblockedswitchtid",
        stripe_account_id="acct_ui_account_switch_blocked",
        stripe_invoice_id=f"in_{uuid.uuid4().hex[:8]}",
        amount_cents=17500,
        paid_at=datetime(2026, 3, 21, 11, 0, tzinfo=timezone.utc),
        status="open",
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get("/app/account", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert (
        "Provider switching is blocked right now because this workspace still has 1 open invoice. Clear those items before starting a PayPal switch."
        in response.text
    )
    assert "Start PayPal switch" not in response.text
    assert 'action="/app/paypal/connect/start"' not in response.text


def test_account_page_pending_paypal_switch_keeps_resume_restart_and_cancel_for_allowlisted_operator():
    connected_at = datetime.now(timezone.utc).replace(microsecond=0)
    inserted = _insert_creator_user(
        email=f"ui_account_switch_pending_{uuid.uuid4().hex}@example.com",
        name="Pending Switch Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_account_switch_pending",
        stripe_connected_at=connected_at,
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    _insert_billing_provider_switch_attempt(
        creator_id=inserted["creator_id"],
        source_billing_provider="stripe",
        target_billing_provider="paypal",
        target_billing_connect_status="pending",
        target_billing_provider_correlation_id="tracking_ui_account_switch_pending",
    )

    settings = _paypal_operator_only_settings(inserted["email"], environment="sandbox")

    with _override_app_state("settings", settings):
        with TestClient(app) as client:
            client.cookies.set(SESSION_COOKIE_NAME, access_token)
            response = client.get("/app/account", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Pending provider switch" in response.text
    assert "Switch state" in response.text
    assert "Current provider" in response.text
    assert "Pending switch target" in response.text
    assert "PayPal" in response.text
    assert "Resume PayPal setup" in response.text
    assert 'action="/app/paypal/connect/start"' in response.text
    assert 'action="/app/account/billing-switch/restart"' in response.text
    assert 'action="/app/account/billing-switch/cancel"' in response.text


def test_account_page_pending_paypal_switch_hides_resume_and_restart_for_non_operator_live_creator():
    connected_at = datetime.now(timezone.utc).replace(microsecond=0)
    inserted = _insert_creator_user(
        email=f"ui_account_switch_pending_live_{uuid.uuid4().hex}@example.com",
        name="Pending Switch Live Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_account_switch_pending_live",
        stripe_connected_at=connected_at,
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    _insert_billing_provider_switch_attempt(
        creator_id=inserted["creator_id"],
        source_billing_provider="stripe",
        target_billing_provider="paypal",
        target_billing_connect_status="pending",
        target_billing_provider_correlation_id="tracking_ui_account_switch_pending_live",
    )
    settings = _live_paypal_operator_only_settings("ops@creatortrust.co")

    with _override_app_state("settings", settings):
        with TestClient(app) as client:
            client.cookies.set(SESSION_COOKIE_NAME, access_token)
            response = client.get("/app/account", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Pending switch target" in response.text
    assert "PayPal setup is not yet available for general creators." in response.text
    assert "Resume PayPal setup" not in response.text
    assert "Restart switch" not in response.text
    assert 'action="/app/account/billing-switch/cancel"' in response.text
    assert 'action="/app/paypal/connect/start"' not in response.text


def test_account_page_restart_switch_route_redirects_non_operator_live_creator_to_unavailable_notice():
    connected_at = datetime.now(timezone.utc).replace(microsecond=0)
    inserted = _insert_creator_user(
        email=f"ui_account_switch_restart_live_{uuid.uuid4().hex}@example.com",
        name="Restart Switch Live Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_account_switch_restart_live",
        stripe_connected_at=connected_at,
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    _insert_billing_provider_switch_attempt(
        creator_id=inserted["creator_id"],
        source_billing_provider="stripe",
        target_billing_provider="paypal",
        target_billing_connect_status="pending",
        target_billing_provider_correlation_id="tracking_ui_account_switch_restart_live",
    )
    provider = _StubPayPalProvider(
        readiness=BillingAccountReadiness(can_create_invoices=True)
    )
    settings = _live_paypal_operator_only_settings("ops@creatortrust.co")

    with _override_app_state("settings", settings):
        with _override_app_state("paypal_provider", provider):
            with TestClient(app) as client:
                client.cookies.set(SESSION_COOKIE_NAME, access_token)
                response = client.post(
                    "/app/account/billing-switch/restart",
                    headers=HTML_ACCEPT_HEADERS,
                    follow_redirects=False,
                )

    assert response.status_code == 303
    assert response.headers["location"] == "/app/account?status=paypal-unavailable"
    assert provider.start_calls == []


def test_account_page_ready_pending_paypal_switch_keeps_commit_action_for_allowlisted_operator():
    connected_at = datetime.now(timezone.utc).replace(microsecond=0)
    inserted = _insert_creator_user(
        email=f"ui_account_switch_ready_{uuid.uuid4().hex}@example.com",
        name="Ready Switch Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_account_switch_ready",
        stripe_connected_at=connected_at,
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    _insert_billing_provider_switch_attempt(
        creator_id=inserted["creator_id"],
        source_billing_provider="stripe",
        target_billing_provider="paypal",
        target_billing_connect_status="connected",
        target_billing_account_id="merchant_ui_account_switch_ready",
        target_billing_provider_correlation_id="tracking_ui_account_switch_ready",
        target_billing_connected_at=datetime(2026, 3, 21, 12, 0, tzinfo=timezone.utc),
    )
    provider = _StubPayPalProvider(
        readiness=BillingAccountReadiness(can_create_invoices=True)
    )

    settings = _paypal_operator_only_settings(inserted["email"], environment="sandbox")

    with _override_app_state("settings", settings):
        with _override_app_state("paypal_provider", provider):
            with TestClient(app) as client:
                client.cookies.set(SESSION_COOKIE_NAME, access_token)
                response = client.get("/app/account", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Pending provider switch" in response.text
    assert "Switch state" in response.text
    assert "Pending target account" in response.text
    assert "merchant_ui_account_switch_ready" in response.text
    assert 'class="wrap-anywhere">merchant_ui_account_switch_ready</span>' in response.text
    assert "Switch to PayPal" in response.text
    assert 'action="/app/account/billing-switch/commit"' in response.text
    assert provider.readiness_calls == ["merchant_ui_account_switch_ready"]


def test_account_page_ready_pending_paypal_switch_hides_commit_for_non_operator_live_creator():
    connected_at = datetime.now(timezone.utc).replace(microsecond=0)
    inserted = _insert_creator_user(
        email=f"ui_account_switch_ready_live_{uuid.uuid4().hex}@example.com",
        name="Ready Switch Live Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_account_switch_ready_live",
        stripe_connected_at=connected_at,
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    _insert_billing_provider_switch_attempt(
        creator_id=inserted["creator_id"],
        source_billing_provider="stripe",
        target_billing_provider="paypal",
        target_billing_connect_status="connected",
        target_billing_account_id="merchant_ui_account_switch_ready_live",
        target_billing_provider_correlation_id="tracking_ui_account_switch_ready_live",
        target_billing_connected_at=datetime(2026, 3, 21, 12, 0, tzinfo=timezone.utc),
    )
    provider = _StubPayPalProvider(
        readiness=BillingAccountReadiness(can_create_invoices=True)
    )
    settings = _live_paypal_operator_only_settings("ops@creatortrust.co")

    with _override_app_state("settings", settings):
        with _override_app_state("paypal_provider", provider):
            with TestClient(app) as client:
                client.cookies.set(SESSION_COOKIE_NAME, access_token)
                response = client.get("/app/account", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Pending target account" in response.text
    assert "PayPal setup is not yet available for general creators." in response.text
    assert "Switch to PayPal" not in response.text
    assert "Restart switch" not in response.text
    assert 'action="/app/account/billing-switch/cancel"' in response.text
    assert provider.readiness_calls == ["merchant_ui_account_switch_ready_live"]


def test_account_page_commit_switch_route_redirects_non_operator_live_creator_to_unavailable_notice():
    connected_at = datetime.now(timezone.utc).replace(microsecond=0)
    inserted = _insert_creator_user(
        email=f"ui_account_switch_commit_live_{uuid.uuid4().hex}@example.com",
        name="Commit Switch Live Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_account_switch_commit_live",
        stripe_connected_at=connected_at,
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    _insert_billing_provider_switch_attempt(
        creator_id=inserted["creator_id"],
        source_billing_provider="stripe",
        target_billing_provider="paypal",
        target_billing_connect_status="connected",
        target_billing_account_id="merchant_ui_account_switch_commit_live",
        target_billing_provider_correlation_id="tracking_ui_account_switch_commit_live",
        target_billing_connected_at=datetime(2026, 3, 21, 13, 0, tzinfo=timezone.utc),
    )
    settings = _live_paypal_operator_only_settings("ops@creatortrust.co")

    with _override_app_state("settings", settings):
        with TestClient(app) as client:
            client.cookies.set(SESSION_COOKIE_NAME, access_token)
            response = client.post(
                "/app/account/billing-switch/commit",
                headers=HTML_ACCEPT_HEADERS,
                follow_redirects=False,
            )

    assert response.status_code == 303
    assert response.headers["location"] == "/app/account?status=paypal-unavailable"
    with _engine().connect() as conn:
        attempt_rows = conn.execute(
            text("SELECT id FROM billing_provider_switch_attempts WHERE creator_id = :creator_id"),
            {"creator_id": inserted["creator_id"]},
        ).mappings().all()
        creator_row = conn.execute(
            text(
                "SELECT billing_provider, billing_account_id FROM creators WHERE id = :creator_id"
            ),
            {"creator_id": inserted["creator_id"]},
        ).mappings().one()
    assert len(attempt_rows) == 1
    assert creator_row["billing_provider"] == "stripe"
    assert creator_row["billing_account_id"] == "acct_ui_account_switch_commit_live"


def test_account_page_pending_paypal_switch_shows_actionable_not_ready_steps():
    connected_at = datetime.now(timezone.utc).replace(microsecond=0)
    inserted = _insert_creator_user(
        email=f"ui_account_switch_not_ready_{uuid.uuid4().hex}@example.com",
        name="Not Ready Switch Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_account_switch_not_ready",
        stripe_connected_at=connected_at,
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    _insert_billing_provider_switch_attempt(
        creator_id=inserted["creator_id"],
        source_billing_provider="stripe",
        target_billing_provider="paypal",
        target_billing_connect_status="connected",
        target_billing_account_id="merchant_ui_account_switch_not_ready",
        target_billing_provider_correlation_id="tracking_ui_account_switch_not_ready",
        target_billing_connected_at=datetime(2026, 3, 21, 12, 15, tzinfo=timezone.utc),
    )
    provider = _StubPayPalProvider(
        readiness=BillingAccountReadiness(
            can_create_invoices=False,
            creator_actionable_issue_codes=(
                BILLING_ACCOUNT_READINESS_ISSUE_CONFIRM_PAYPAL_PRIMARY_EMAIL,
                BILLING_ACCOUNT_READINESS_ISSUE_ENABLE_PAYPAL_PAYMENTS_RECEIVABLE,
            ),
        )
    )
    settings = _paypal_operator_only_settings(inserted["email"], environment="sandbox")

    with _override_app_state("settings", settings):
        with _override_app_state("paypal_provider", provider):
            with TestClient(app) as client:
                client.cookies.set(SESSION_COOKIE_NAME, access_token)
                response = client.get("/app/account", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert (
        "PayPal is connected for the pending switch, but it still needs this setup work before it can create invoices: confirm the primary email on the connected PayPal business account and finish the PayPal payments-receivable setup. Stripe stays active until PayPal is ready and you commit the switch."
        in response.text
    )
    assert "Switch to PayPal" not in response.text
    assert 'action="/app/account/billing-switch/restart"' in response.text
    assert 'action="/app/account/billing-switch/cancel"' in response.text
    assert provider.readiness_calls == ["merchant_ui_account_switch_not_ready"]


def test_account_page_pending_paypal_switch_collapses_provider_failure_into_blocked_state():
    connected_at = datetime.now(timezone.utc).replace(microsecond=0)
    inserted = _insert_creator_user(
        email=f"ui_account_switch_blocked_pending_{uuid.uuid4().hex}@example.com",
        name="Blocked Switch Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_account_switch_blocked_pending",
        stripe_connected_at=connected_at,
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    _insert_billing_provider_switch_attempt(
        creator_id=inserted["creator_id"],
        source_billing_provider="stripe",
        target_billing_provider="paypal",
        target_billing_connect_status="connected",
        target_billing_account_id="merchant_ui_account_switch_blocked_pending",
        target_billing_provider_correlation_id="tracking_ui_account_switch_blocked_pending",
        target_billing_connected_at=datetime(2026, 3, 21, 12, 20, tzinfo=timezone.utc),
    )
    provider = _StubPayPalProvider(
        readiness=BillingAccountReadiness(can_create_invoices=False),
        readiness_error=PayPalProviderError(
            "paypal merchant status lookup failed",
            operation="paypal_merchant_status",
            http_status=500,
            error_code="INTERNAL_SERVER_ERROR",
        ),
    )
    settings = _paypal_operator_only_settings(inserted["email"], environment="sandbox")

    with _override_app_state("settings", settings):
        with _override_app_state("paypal_provider", provider):
            with TestClient(app) as client:
                client.cookies.set(SESSION_COOKIE_NAME, access_token)
                response = client.get("/app/account", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert (
        "PayPal is connected for the pending switch, but its invoice readiness could not be verified right now. Stripe stays active until the readiness check succeeds and you commit the switch."
        in response.text
    )
    assert "Switch to PayPal" not in response.text
    assert 'action="/app/account/billing-switch/restart"' in response.text
    assert 'action="/app/account/billing-switch/cancel"' in response.text
    assert provider.readiness_calls == ["merchant_ui_account_switch_blocked_pending"]


def test_account_page_cancel_switch_route_clears_pending_attempt_without_switching_provider():
    connected_at = datetime.now(timezone.utc).replace(microsecond=0)
    inserted = _insert_creator_user(
        email=f"ui_account_switch_cancel_{uuid.uuid4().hex}@example.com",
        name="Cancel Switch Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_account_switch_cancel",
        stripe_connected_at=connected_at,
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    _insert_billing_provider_switch_attempt(
        creator_id=inserted["creator_id"],
        source_billing_provider="stripe",
        target_billing_provider="paypal",
        target_billing_connect_status="connected",
        target_billing_account_id="merchant_ui_account_switch_cancel",
        target_billing_provider_correlation_id="tracking_ui_account_switch_cancel",
        target_billing_connected_at=datetime(2026, 3, 21, 12, 30, tzinfo=timezone.utc),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.post(
            "/app/account/billing-switch/cancel",
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/app/account?status=billing-provider-switch-canceled"
    with _engine().connect() as conn:
        attempt_rows = conn.execute(
            text("SELECT id FROM billing_provider_switch_attempts WHERE creator_id = :creator_id"),
            {"creator_id": inserted["creator_id"]},
        ).mappings().all()
        creator_row = conn.execute(
            text(
                "SELECT billing_provider, billing_account_id FROM creators WHERE id = :creator_id"
            ),
            {"creator_id": inserted["creator_id"]},
        ).mappings().one()
    assert attempt_rows == []
    assert creator_row["billing_provider"] == "stripe"
    assert creator_row["billing_account_id"] == "acct_ui_account_switch_cancel"


def test_account_page_commit_switch_route_promotes_ready_target_provider():
    connected_at = datetime.now(timezone.utc).replace(microsecond=0)
    inserted = _insert_creator_user(
        email=f"ui_account_switch_commit_{uuid.uuid4().hex}@example.com",
        name="Commit Switch Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_account_switch_commit",
        stripe_connected_at=connected_at,
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    _insert_billing_provider_switch_attempt(
        creator_id=inserted["creator_id"],
        source_billing_provider="stripe",
        target_billing_provider="paypal",
        target_billing_connect_status="connected",
        target_billing_account_id="merchant_ui_account_switch_commit",
        target_billing_provider_correlation_id="tracking_ui_account_switch_commit",
        target_billing_connected_at=datetime(2026, 3, 21, 13, 0, tzinfo=timezone.utc),
    )
    provider = _StubPayPalProvider(
        readiness=BillingAccountReadiness(can_create_invoices=True)
    )
    settings = _paypal_operator_only_settings(inserted["email"], environment="sandbox")

    with _override_app_state("settings", settings):
        with _override_app_state("paypal_provider", provider):
            with TestClient(app) as client:
                client.cookies.set(SESSION_COOKIE_NAME, access_token)
                response = client.post(
                    "/app/account/billing-switch/commit",
                    headers=HTML_ACCEPT_HEADERS,
                    follow_redirects=False,
                )

    assert response.status_code == 303
    assert response.headers["location"] == "/app/account?status=billing-provider-switch-committed"
    with _engine().connect() as conn:
        attempt_rows = conn.execute(
            text("SELECT id FROM billing_provider_switch_attempts WHERE creator_id = :creator_id"),
            {"creator_id": inserted["creator_id"]},
        ).mappings().all()
        creator_row = conn.execute(
            text(
                "SELECT billing_provider, billing_account_id, billing_provider_correlation_id, stripe_account_id "
                "FROM creators WHERE id = :creator_id"
            ),
            {"creator_id": inserted["creator_id"]},
        ).mappings().one()
    assert attempt_rows == []
    assert creator_row["billing_provider"] == "paypal"
    assert creator_row["billing_account_id"] == "merchant_ui_account_switch_commit"
    assert creator_row["billing_provider_correlation_id"] == "tracking_ui_account_switch_commit"
    assert creator_row["stripe_account_id"] == "acct_ui_account_switch_commit"
    assert provider.readiness_calls == ["merchant_ui_account_switch_commit"]


def test_setup_and_account_pages_reuse_waiting_for_first_paid_result_vocabulary():
    inserted = _insert_creator_user(
        email=f"ui_waiting_first_paid_{uuid.uuid4().hex}@example.com",
        name="Waiting First Paid Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_waiting_first_paid",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="Waiting First Paid Strategy",
        calendly_url="https://calendly.com/example/waiting-first-paid",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    _insert_content(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/waiting-first-paid",
        tid=f"uiwaitingfirstpaid{uuid.uuid4().hex[:8]}",
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        setup_response = client.get("/app", headers=HTML_ACCEPT_HEADERS)
        account_response = client.get("/app/account", headers=HTML_ACCEPT_HEADERS)
        reports_response = client.get("/app/reports", headers=HTML_ACCEPT_HEADERS)

    assert setup_response.status_code == 200
    assert account_response.status_code == 200
    assert reports_response.status_code == 200
    assert "Ready to track" in setup_response.text
    assert "Am I set up correctly?" in setup_response.text
    assert "Copy or share a tracked link" in setup_response.text
    assert "Your path to first paid proof" not in setup_response.text
    assert "Setup path" in setup_response.text
    assert "4 of 4 setup milestones done" in setup_response.text
    assert "1 tracked link ready to share" in setup_response.text
    assert (
        "Tracking is ready. Reports stay quiet until real activity lands, which is different from setup failing."
        in setup_response.text
    )
    assert "Ready to track and waiting for first paid result" in account_response.text
    assert "Ready to track</strong>: Done. At least one tracked link is ready to share on a billable setup." in account_response.text
    assert "Waiting for first paid result</strong>: Current. This workspace is ready to track; first value lands after a tracked booking leads to a paid invoice." in account_response.text
    assert "Content funnel summary" in reports_response.text
    assert "waiting-first-paid" in reports_response.text
    assert "No bookings yet" in reports_response.text
    assert "This content is tracked, but no booking has landed yet." in reports_response.text
    assert "No invoice-backed paid result is counted for this content yet." in reports_response.text
    assert "Illustrative preview" not in reports_response.text
    assert "Open Experiments" not in setup_response.text
    assert "Review Experiments" not in setup_response.text


def test_setup_home_bookings_without_paid_result_promotes_reports_review():
    inserted = _insert_creator_user(
        email=f"ui_bookings_waiting_paid_{uuid.uuid4().hex}@example.com",
        name="Bookings Waiting Paid Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_bookings_waiting_paid",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="Bookings Waiting Paid Call",
        calendly_url="https://calendly.com/example/bookings-waiting-paid",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    tid = f"uibookingswaitingpaid{uuid.uuid4().hex[:8]}"
    _insert_content(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/bookings-waiting-paid",
        tid=tid,
    )
    _insert_booking(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        tid=tid,
        calendly_booking_uuid=f"BOOK_{uuid.uuid4().hex[:12]}",
        booked_at=datetime.now(timezone.utc),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get("/app", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Bookings are landing; paid proof is next" in response.text
    assert "Why do bookings show up before revenue?" in response.text
    assert "Your path to first paid proof" not in response.text
    assert "Setup path" in response.text
    assert "4 of 4 setup milestones done" in response.text
    assert "1 tracked booking already recorded" in response.text
    assert "Activity is already visible. This is a waiting-on-paid-truth state, not a broken-tracking state." in response.text
    assert 'href="/app/reports"' in response.text
    assert "Open Reports" in response.text


def test_setup_home_attention_summary_keeps_diagnostics_secondary_and_points_to_attention():
    creator = _insert_creator_user(
        email=f"ui_shell_attention_{uuid.uuid4().hex}@example.com",
        name="Shell Attention Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_shell_attention",
    )
    access_token = _access_token(
        user_id=creator["user_id"],
        creator_id=creator["creator_id"],
        email=creator["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=creator["creator_id"],
        name="Shell Attention Call",
        calendly_url="https://calendly.com/example/shell-attention",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    tid = f"uishellattention{uuid.uuid4().hex[:8]}"
    booking_uuid = f"BOOK_UI_SHELL_ATTENTION_{uuid.uuid4().hex[:8]}"
    _insert_content(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/shell-attention",
        tid=tid,
    )
    booking_id = _insert_booking(
        creator_id=creator["creator_id"],
        booking_link_id=booking_link_id,
        tid=tid,
        calendly_booking_uuid=booking_uuid,
        booked_at=datetime.now(timezone.utc),
    )
    _insert_blocked_billing_case(
        creator_id=creator["creator_id"],
        booking_id=booking_id,
        tid=tid,
        calendly_booking_uuid=booking_uuid,
        stripe_account_id="acct_ui_shell_attention",
        frozen_amount_cents=19500,
        frozen_currency="USD",
        reason_code="creator_not_billable",
        first_blocked_at=datetime.now(timezone.utc),
    )
    _insert_unmatched_payment_event(
        creator_id=creator["creator_id"],
        stripe_account_id="acct_ui_shell_attention",
        stripe_event_id=f"evt_ui_shell_attention_{uuid.uuid4().hex[:8]}",
        stripe_invoice_id=f"in_ui_shell_attention_{uuid.uuid4().hex[:8]}",
        reason=UNATTRIBUTED_REASON_MISSING_TID,
        paid_at=datetime.now(timezone.utc),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get("/app", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Bookings are landing; paid proof is next" in response.text
    assert "2 attention items still need review" in response.text
    assert (
        "Blocked billing and unresolved payments stay outside paid totals until the repair or attribution issue is resolved."
        in response.text
    )
    assert "Open Attention" in response.text
    assert 'class="button-link secondary">Open Attention</a>' in response.text
    assert 'href="/app/attention"' in response.text


def test_setup_home_first_paid_result_promotes_reports_review():
    inserted = _insert_creator_user(
        email=f"ui_first_paid_shell_{uuid.uuid4().hex}@example.com",
        name="First Paid Shell Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_first_paid_shell",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    booking_link_id = _insert_booking_link(
        creator_id=inserted["creator_id"],
        name="First Paid Shell Call",
        calendly_url="https://calendly.com/example/first-paid-shell",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    tid = f"uifirstpaidshell{uuid.uuid4().hex[:8]}"
    _insert_content(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/first-paid-shell",
        tid=tid,
    )
    booking_id = _insert_booking(
        creator_id=inserted["creator_id"],
        booking_link_id=booking_link_id,
        tid=tid,
        calendly_booking_uuid=f"BOOK_{uuid.uuid4().hex[:12]}",
        booked_at=datetime.now(timezone.utc),
    )
    _insert_invoice(
        creator_id=inserted["creator_id"],
        booking_id=booking_id,
        tid=tid,
        stripe_account_id="acct_ui_first_paid_shell",
        stripe_invoice_id=f"in_{uuid.uuid4().hex[:12]}",
        amount_cents=19500,
        paid_at=datetime.now(timezone.utc),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get("/app", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "First paid result is already landing" in response.text
    assert "What is working and where should I look next?" in response.text
    assert "Your path to first paid proof" not in response.text
    assert "Setup path" in response.text
    assert "4 of 4 setup milestones done" in response.text
    assert "1 paid result already counted" in response.text
    assert (
        "This is the first-value milestone, not just booking activity. Canonical paid invoices are already counted."
        in response.text
    )
    assert 'href="/app/reports"' in response.text
    assert 'class="button-link">Open Reports</a>' in response.text
    assert "Open Reports" in response.text
    assert "Experiments still need more evidence" in response.text
    assert (
        "Reports stay primary here. Open Experiments to review the current gap before you generate a new snapshot."
        in response.text
    )
    assert 'class="button-link secondary">Review Experiments</a>' in response.text
    assert 'href="/app/experiments"' in response.text


def test_account_page_disconnected_state_renders_reconnect_copy_without_destructive_forms():
    inserted = _insert_creator_user(
        email=f"ui_account_disconnected_{uuid.uuid4().hex}@example.com",
        name="Account Disconnected Creator",
        stripe_connect_status="disconnected",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get("/app/account", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert "Disconnected" in response.text
    assert "This workspace is not currently connected to Stripe for invoicing." in response.text
    assert "Reconnect Stripe" in response.text
    assert "No booking links are saved yet for this workspace." in response.text
    assert "Workspace reset and account deletion stay support-assisted during beta." in response.text
    assert 'href="/app/account?confirm=workspace-reset#danger-zone"' in response.text
    assert 'href="/app/account?confirm=account-deletion#danger-zone"' in response.text
    assert 'action="/app/stripe/connect/start"' in response.text
    assert 'action="/app/account"' not in response.text


def test_account_page_connected_paypal_state_renders_disconnect_confirmation_flow():
    connected_at = datetime.now(timezone.utc).replace(microsecond=0)
    inserted = _insert_creator_user(
        email=f"ui_account_paypal_disconnect_{uuid.uuid4().hex}@example.com",
        name="PayPal Disconnect Creator",
        stripe_connect_status="pending",
        billing_provider="paypal",
        billing_connect_status="connected",
        billing_account_id="merchant_ui_disconnect",
        billing_connected_at=connected_at,
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        base_response = client.get("/app/account", headers=HTML_ACCEPT_HEADERS)
        confirm_response = client.get(
            "/app/account",
            params={"confirm": "disconnect-paypal"},
            headers=HTML_ACCEPT_HEADERS,
        )

    assert base_response.status_code == 200
    assert 'href="/app/account?confirm=disconnect-paypal#billing-connection"' in base_response.text
    assert "Disconnect PayPal" in base_response.text

    assert confirm_response.status_code == 200
    assert "Disconnect PayPal?" in confirm_response.text
    assert (
        "Disconnecting your PayPal account will prevent you from offering PayPal services and products on your website. Do you wish to continue?"
        in confirm_response.text
    )
    assert 'action="/app/account/paypal/disconnect"' in confirm_response.text
    assert "Keep PayPal connected" in confirm_response.text


def test_account_page_paypal_disconnect_submit_marks_workspace_disconnected():
    connected_at = datetime.now(timezone.utc).replace(microsecond=0)
    inserted = _insert_creator_user(
        email=f"ui_account_paypal_disconnect_submit_{uuid.uuid4().hex}@example.com",
        name="PayPal Disconnect Submit Creator",
        stripe_connect_status="pending",
        billing_provider="paypal",
        billing_connect_status="connected",
        billing_account_id="merchant_ui_disconnect_submit",
        billing_connected_at=connected_at,
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    settings = _paypal_operator_only_settings(inserted["email"], environment="sandbox")

    with _override_app_state("settings", settings):
        with TestClient(app) as client:
            client.cookies.set(SESSION_COOKIE_NAME, access_token)
            submit_response = client.post(
                "/app/account/paypal/disconnect",
                headers=HTML_ACCEPT_HEADERS,
                follow_redirects=False,
            )
            page_response = client.get(
                submit_response.headers["location"],
                headers=HTML_ACCEPT_HEADERS,
            )

    with _engine().connect() as conn:
        creator_row = conn.execute(
            text(
                "SELECT billing_provider, billing_connect_status, billing_account_id, billing_connected_at "
                "FROM creators WHERE id = :creator_id"
            ),
            {"creator_id": inserted["creator_id"]},
        ).mappings().one()

    assert submit_response.status_code == 303
    assert submit_response.headers["location"] == "/app/account?status=paypal-disconnected#billing-connection"
    assert page_response.status_code == 200
    assert "PayPal disconnected" in page_response.text
    assert "Reconnect PayPal" in page_response.text
    assert creator_row["billing_provider"] == "paypal"
    assert creator_row["billing_connect_status"] == "disconnected"
    assert creator_row["billing_account_id"] == "merchant_ui_disconnect_submit"
    assert creator_row["billing_connected_at"] is None


def test_account_page_reset_confirmation_renders_before_send():
    inserted = _insert_creator_user(
        email=f"ui_account_confirm_reset_{uuid.uuid4().hex}@example.com",
        name="Reset Confirm Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_account_confirm_reset",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        response = client.get(
            "/app/account",
            params={"confirm": "workspace-reset"},
            headers=HTML_ACCEPT_HEADERS,
        )

    assert response.status_code == 200
    assert "Request workspace reset?" in response.text
    assert "This sends a manual review request for a fresh start with the same email." in response.text
    assert 'action="/app/account/requests/workspace-reset"' in response.text
    assert "Submit reset request" in response.text
    assert "Keep workspace" in response.text


def test_account_page_reset_submit_sends_support_request_and_shows_requested_state():
    inserted = _insert_creator_user(
        email=f"ui_account_reset_submit_{uuid.uuid4().hex}@example.com",
        name="Reset Submit Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_account_reset_submit",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        submit_response = client.post(
            "/app/account/requests/workspace-reset",
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )
        page_response = client.get(
            submit_response.headers["location"],
            headers=HTML_ACCEPT_HEADERS,
        )

    captured = _latest_support_request("workspace-reset")
    support_requests = _support_requests_for_creator(
        creator_id=inserted["creator_id"],
        request_type="workspace-reset",
    )
    support_request = support_requests[0]
    request_id = str(support_request["id"])

    assert submit_response.status_code == 303
    assert submit_response.headers["location"] == "/app/account?status=workspace-reset-requested#danger-zone"
    assert page_response.status_code == 200
    assert "Workspace reset requested" in page_response.text
    assert "Keep using this workspace unless support confirms that reset work is complete." in page_response.text
    assert len(support_requests) == 1
    assert support_request["requester_email"] == inserted["email"]
    assert support_request["creator_name_snapshot"] == "Reset Submit Creator"
    assert support_request["status"] == "submitted"
    assert support_request["notification_attempted_at"] is not None
    assert support_request["notification_sent_at"] is not None
    assert support_request["notification_failed_at"] is None
    assert captured["support_email"] == "eric@careercodepro.com"
    assert captured["request_id"] == request_id
    assert captured["requester_email"] == inserted["email"]
    assert captured["creator_name"] == "Reset Submit Creator"
    assert captured["creator_id"] == inserted["creator_id"]
    assert captured["subject"] == f"Workspace reset request for {inserted['email']}"
    assert f"Request id: {request_id}" in captured["body"]
    assert "Request type: workspace-reset" in captured["body"]
    assert "Current request" in page_response.text
    assert "Submitted" in page_response.text
    assert "Email delivery" in page_response.text
    assert "Delivered" in page_response.text
    assert request_id in page_response.text
    assert "Support email notification succeeded" in page_response.text
    assert 'href="/app/account?confirm=workspace-reset#danger-zone"' not in page_response.text
    assert 'action="/app/account/requests/workspace-reset"' not in page_response.text


def test_account_page_deletion_submit_sends_support_request_and_shows_requested_state():
    inserted = _insert_creator_user(
        email=f"ui_account_delete_submit_{uuid.uuid4().hex}@example.com",
        name="Deletion Submit Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_account_delete_submit",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        submit_response = client.post(
            "/app/account/requests/account-deletion",
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )
        page_response = client.get(
            submit_response.headers["location"],
            headers=HTML_ACCEPT_HEADERS,
        )

    captured = _latest_support_request("account-deletion")
    support_requests = _support_requests_for_creator(
        creator_id=inserted["creator_id"],
        request_type="account-deletion",
    )
    support_request = support_requests[0]
    request_id = str(support_request["id"])

    assert submit_response.status_code == 303
    assert submit_response.headers["location"] == "/app/account?status=account-deletion-requested#danger-zone"
    assert page_response.status_code == 200
    assert "Account deletion requested" in page_response.text
    assert "No local data has been removed yet." in page_response.text
    assert len(support_requests) == 1
    assert support_request["status"] == "submitted"
    assert support_request["notification_sent_at"] is not None
    assert support_request["notification_failed_at"] is None
    assert captured["support_email"] == "eric@careercodepro.com"
    assert captured["request_id"] == request_id
    assert captured["requester_email"] == inserted["email"]
    assert captured["creator_name"] == "Deletion Submit Creator"
    assert captured["creator_id"] == inserted["creator_id"]
    assert captured["subject"] == f"Account deletion request for {inserted['email']}"
    assert f"Request id: {request_id}" in captured["body"]
    assert "Request type: account-deletion" in captured["body"]
    assert "Submitted" in page_response.text
    assert "Delivered" in page_response.text
    assert request_id in page_response.text


def test_account_page_duplicate_active_request_reuses_existing_row_without_second_email():
    inserted = _insert_creator_user(
        email=f"ui_account_request_duplicate_{uuid.uuid4().hex}@example.com",
        name="Duplicate Request Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_account_request_duplicate",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)
        first_submit = client.post(
            "/app/account/requests/workspace-reset",
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )
        second_submit = client.post(
            "/app/account/requests/workspace-reset",
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )
        page_response = client.get(
            second_submit.headers["location"],
            headers=HTML_ACCEPT_HEADERS,
        )

    support_requests = _support_requests_for_creator(
        creator_id=inserted["creator_id"],
        request_type="workspace-reset",
    )

    assert first_submit.status_code == 303
    assert second_submit.status_code == 303
    assert second_submit.headers["location"] == "/app/account?status=workspace-reset-active#danger-zone"
    assert len(support_requests) == 1
    assert len(get_support_request_outbox()) == 1
    assert "Workspace reset already pending" in page_response.text
    assert "one active workspace reset request during beta" in page_response.text
    assert str(support_requests[0]["id"]) in page_response.text
    assert "Submitted" in page_response.text


def test_account_page_support_request_failure_keeps_saved_request_visible():
    inserted = _insert_creator_user(
        email=f"ui_account_request_failure_{uuid.uuid4().hex}@example.com",
        name="Failure Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_account_request_failure",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    provider = _FailingEmailProvider(error_text="smtp timeout from sandbox provider")

    with _override_app_state("email_provider", provider):
        with TestClient(app) as client:
            client.cookies.set(SESSION_COOKIE_NAME, access_token)
            submit_response = client.post(
                "/app/account/requests/workspace-reset",
                headers=HTML_ACCEPT_HEADERS,
                follow_redirects=False,
            )
            page_response = client.get(
                submit_response.headers["location"],
                headers=HTML_ACCEPT_HEADERS,
            )

    support_requests = _support_requests_for_creator(
        creator_id=inserted["creator_id"],
        request_type="workspace-reset",
    )
    support_request = support_requests[0]

    assert submit_response.status_code == 303
    assert submit_response.headers["location"] == "/app/account?status=workspace-reset-retry#danger-zone"
    assert page_response.status_code == 200
    assert len(support_requests) == 1
    assert support_request["status"] == "submitted"
    assert support_request["notification_attempted_at"] is not None
    assert support_request["notification_sent_at"] is None
    assert support_request["notification_failed_at"] is not None
    assert "Workspace reset saved, but notification failed" in page_response.text
    assert "We recorded your workspace reset request" in page_response.text
    assert "Submitted" in page_response.text
    assert "Email delivery" in page_response.text
    assert "Failed" in page_response.text
    assert str(support_request["id"]) in page_response.text
    assert "Support email notification failed" in page_response.text
    assert 'action="/app/account/requests/workspace-reset"' not in page_response.text


def test_operator_support_queue_denies_non_allowlisted_browser_user():
    inserted = _insert_creator_user(
        email=f"ui_operator_denied_{uuid.uuid4().hex}@example.com",
        name="Denied Operator User",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    with _override_app_state("settings", _operator_allowlist_settings("ops@creatortrust.co")):
        with TestClient(app) as client:
            client.cookies.set(SESSION_COOKIE_NAME, access_token)
            response = client.get(
                "/app/operator/support-requests",
                headers=HTML_ACCEPT_HEADERS,
            )

    assert response.status_code == 404
    assert response.json() == {"detail": "operator queue not found"}


def test_operator_support_queue_lists_requests_and_transitions_statuses():
    requester_one = _insert_creator_user(
        email=f"ui_operator_requester_one_{uuid.uuid4().hex}@example.com",
        name="Operator Queue One",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_operator_queue_one",
    )
    requester_two = _insert_creator_user(
        email=f"ui_operator_requester_two_{uuid.uuid4().hex}@example.com",
        name="Operator Queue Two",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_operator_queue_two",
    )
    operator_user = _insert_creator_user(
        email="ops@creatortrust.co",
        name="Operator Reviewer",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_operator_queue_reviewer",
    )

    requester_one_token = _access_token(
        user_id=requester_one["user_id"],
        creator_id=requester_one["creator_id"],
        email=requester_one["email"],
        expires_delta=timedelta(hours=24),
    )
    requester_two_token = _access_token(
        user_id=requester_two["user_id"],
        creator_id=requester_two["creator_id"],
        email=requester_two["email"],
        expires_delta=timedelta(hours=24),
    )
    operator_token = _access_token(
        user_id=operator_user["user_id"],
        creator_id=operator_user["creator_id"],
        email=operator_user["email"],
        expires_delta=timedelta(hours=24),
    )

    with TestClient(app) as requester_client:
        requester_client.cookies.set(SESSION_COOKIE_NAME, requester_one_token)
        first_submit = requester_client.post(
            "/app/account/requests/workspace-reset",
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )

    failing_provider = _FailingEmailProvider(error_text="operator queue notification failure")
    with _override_app_state("email_provider", failing_provider):
        with TestClient(app) as requester_client:
            requester_client.cookies.set(SESSION_COOKIE_NAME, requester_two_token)
            second_submit = requester_client.post(
                "/app/account/requests/account-deletion",
                headers=HTML_ACCEPT_HEADERS,
                follow_redirects=False,
            )

    requester_one_request = _support_requests_for_creator(
        creator_id=requester_one["creator_id"],
        request_type="workspace-reset",
    )[0]
    requester_two_request = _support_requests_for_creator(
        creator_id=requester_two["creator_id"],
        request_type="account-deletion",
    )[0]

    with _override_app_state("settings", _operator_allowlist_settings(operator_user["email"])):
        with TestClient(app) as operator_client:
            operator_client.cookies.set(SESSION_COOKIE_NAME, operator_token)
            queue_response = operator_client.get(
                "/app/operator/support-requests",
                headers=HTML_ACCEPT_HEADERS,
            )
            detail_response = operator_client.get(
                f"/app/operator/support-requests/{requester_one_request['id']}",
                headers=HTML_ACCEPT_HEADERS,
            )
            in_review_response = operator_client.post(
                f"/app/operator/support-requests/{requester_one_request['id']}/status",
                data={"status": "in_review"},
                headers=HTML_ACCEPT_HEADERS,
                follow_redirects=False,
            )
            completed_response = operator_client.post(
                f"/app/operator/support-requests/{requester_one_request['id']}/status",
                data={"status": "completed"},
                headers=HTML_ACCEPT_HEADERS,
                follow_redirects=False,
            )
            invalid_transition_response = operator_client.post(
                f"/app/operator/support-requests/{requester_one_request['id']}/status",
                data={"status": "submitted"},
                headers=HTML_ACCEPT_HEADERS,
                follow_redirects=False,
            )
            invalid_transition_page = operator_client.get(
                invalid_transition_response.headers["location"],
                headers=HTML_ACCEPT_HEADERS,
            )

    with TestClient(app) as requester_client:
        requester_client.cookies.set(SESSION_COOKIE_NAME, requester_one_token)
        requester_one_account_response = requester_client.get(
            "/app/account",
            headers=HTML_ACCEPT_HEADERS,
        )

    with TestClient(app) as requester_client:
        requester_client.cookies.set(SESSION_COOKIE_NAME, requester_two_token)
        requester_two_account_response = requester_client.get(
            "/app/account",
            headers=HTML_ACCEPT_HEADERS,
        )

    requester_one_request_rows = _support_requests_for_creator(
        creator_id=requester_one["creator_id"],
        request_type="workspace-reset",
    )

    assert first_submit.status_code == 303
    assert first_submit.headers["location"] == "/app/account?status=workspace-reset-requested#danger-zone"
    assert second_submit.status_code == 303
    assert second_submit.headers["location"] == "/app/account?status=account-deletion-retry#danger-zone"
    assert queue_response.status_code == 200
    assert "Support request queue" in queue_response.text
    assert str(requester_one_request["id"]) in queue_response.text
    assert str(requester_two_request["id"]) in queue_response.text
    assert "Workspace reset" in queue_response.text
    assert "Account deletion" in queue_response.text
    assert "Delivered" in queue_response.text
    assert "Failed" in queue_response.text
    assert detail_response.status_code == 200
    assert "Request context" in detail_response.text
    assert requester_one["creator_id"] in detail_response.text
    assert requester_one["email"] in detail_response.text
    assert "Allowed transitions" in detail_response.text
    assert in_review_response.status_code == 303
    assert in_review_response.headers["location"] == f"/app/operator/support-requests/{requester_one_request['id']}?status=status-updated"
    assert completed_response.status_code == 303
    assert completed_response.headers["location"] == f"/app/operator/support-requests/{requester_one_request['id']}?status=status-updated"
    assert invalid_transition_response.status_code == 303
    assert invalid_transition_response.headers["location"] == f"/app/operator/support-requests/{requester_one_request['id']}?status=invalid-transition"
    assert invalid_transition_page.status_code == 200
    assert "Request status was not changed" in invalid_transition_page.text
    assert "That review transition is not allowed from the current saved state." in invalid_transition_page.text
    assert requester_one_request_rows[0]["status"] == "completed"
    assert requester_one_request_rows[0]["closed_at"] is not None
    assert "Completed" in requester_one_account_response.text
    assert "Support email notification succeeded" in requester_one_account_response.text
    assert str(requester_one_request["id"]) in requester_one_account_response.text
    assert "Submitted" in requester_two_account_response.text
    assert "Failed" in requester_two_account_response.text
    assert str(requester_two_request["id"]) in requester_two_account_response.text


def test_account_page_support_request_submit_uses_shared_rate_limit_state():
    inserted = _insert_creator_user(
        email=f"ui_account_request_throttled_{uuid.uuid4().hex}@example.com",
        name="Throttled Request Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_ui_account_request_throttled",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    throttled_policy = replace(SUPPORT_REQUEST_SUBMIT_POLICY, max_attempts=1)
    bucket_key = build_support_request_rate_limit_bucket_key(
        creator_id=inserted["creator_id"],
        request_type="account-deletion",
    )
    DEFAULT_SHARED_RATE_LIMITER.try_acquire(
        policy=throttled_policy,
        bucket_key=bucket_key,
    )

    with _override_app_state("support_request_submit_policy", throttled_policy):
        with TestClient(app) as client:
            client.cookies.set(SESSION_COOKIE_NAME, access_token)
            submit_response = client.post(
                "/app/account/requests/account-deletion",
                headers=HTML_ACCEPT_HEADERS,
                follow_redirects=False,
            )
            page_response = client.get(
                submit_response.headers["location"],
                headers=HTML_ACCEPT_HEADERS,
            )

    support_requests = _support_requests_for_creator(
        creator_id=inserted["creator_id"],
        request_type="account-deletion",
    )

    assert submit_response.status_code == 303
    assert submit_response.headers["location"] == "/app/account?status=account-deletion-throttled#danger-zone"
    assert page_response.status_code == 200
    assert support_requests == []
    assert get_support_request_outbox() == []
    assert "Too many account deletion attempts" in page_response.text
    assert "Wait a few minutes before trying again." in page_response.text


def test_setup_home_connect_cta_redirects_to_stripe_and_callback_returns_to_app():
    inserted = _insert_creator_user(
        email=f"ui_cta_{uuid.uuid4().hex}@example.com",
        name="CTA Creator",
        stripe_connect_status="pending",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    provider = _StubStripeProvider(account_id="acct_story38_browser")

    with _override_app_state("stripe_provider", provider):
        with TestClient(app) as client:
            client.cookies.set(SESSION_COOKIE_NAME, access_token)

            start_response = client.post(
                "/app/stripe/connect/start",
                headers=HTML_ACCEPT_HEADERS,
                follow_redirects=False,
            )
            start_location = start_response.headers["location"]
            start_query = parse_qs(urlparse(start_location).query)
            callback_response = client.get(
                "/stripe/connect/callback",
                params={
                    "code": "auth_code_story38_browser",
                    "state": start_query["state"][0],
                },
                headers=HTML_ACCEPT_HEADERS,
                follow_redirects=False,
            )
            app_response = client.get("/app", headers=HTML_ACCEPT_HEADERS)

    assert start_response.status_code == 303
    assert start_location.startswith("https://connect.stripe.com/oauth/authorize")
    assert provider.start_calls == [
        {
            "creator_id": inserted["creator_id"],
            "state": start_query["state"][0],
        }
    ]

    assert callback_response.status_code == 303
    assert callback_response.headers["location"] == "/app"
    assert provider.callback_calls == [
        {
            "code": "auth_code_story38_browser",
            "state": start_query["state"][0],
        }
    ]

    with _engine().connect() as conn:
        creator_row = conn.execute(
            text(
                "SELECT billing_provider, billing_connect_status, billing_account_id, billing_connected_at "
                "FROM creators WHERE id = :creator_id"
            ),
            {"creator_id": inserted["creator_id"]},
        ).mappings().one()

    assert app_response.status_code == 200
    assert "Connected, but not billable now" in app_response.text
    assert "Save a booking link" in app_response.text
    assert "Open booking links" in app_response.text
    assert "acct_story38_browser" in app_response.text
    assert creator_row["billing_provider"] == "stripe"
    assert creator_row["billing_connect_status"] == "connected"
    assert creator_row["billing_account_id"] == "acct_story38_browser"
    assert creator_row["billing_connected_at"] is not None


def test_setup_home_paypal_connect_cta_keeps_operator_path_in_sandbox():
    inserted = _insert_creator_user(
        email=f"ui_paypal_cta_{uuid.uuid4().hex}@example.com",
        name="PayPal CTA Creator",
        stripe_connect_status="pending",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    provider = _StubPayPalProvider(
        readiness=BillingAccountReadiness(can_create_invoices=True)
    )
    settings = _paypal_operator_only_settings(inserted["email"], environment="sandbox")

    with _override_app_state("settings", settings):
        with _override_app_state("paypal_provider", provider):
            with TestClient(app) as client:
                client.cookies.set(SESSION_COOKIE_NAME, access_token)

                start_response = client.post(
                    "/app/paypal/connect/start",
                    headers=HTML_ACCEPT_HEADERS,
                    follow_redirects=False,
                )

    assert start_response.status_code == 303
    assert start_response.headers["location"].startswith(
        "https://www.sandbox.paypal.com/bizsignup/partner/entry"
    )
    assert len(provider.start_calls) == 1
    assert provider.start_calls[0]["tracking_id"].startswith("ccp-paypal-")
    assert "state=" in provider.start_calls[0]["return_url"]


def test_setup_home_paypal_connect_cta_redirects_non_operator_creator_to_unavailable_notice():
    inserted = _insert_creator_user(
        email=f"ui_paypal_cta_hidden_{uuid.uuid4().hex}@example.com",
        name="PayPal CTA Hidden Creator",
        stripe_connect_status="pending",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    provider = _StubPayPalProvider(
        readiness=BillingAccountReadiness(can_create_invoices=True)
    )
    settings = _paypal_operator_only_settings("ops@creatortrust.co", environment="sandbox")

    with _override_app_state("settings", settings):
        with _override_app_state("paypal_provider", provider):
            with TestClient(app) as client:
                client.cookies.set(SESSION_COOKIE_NAME, access_token)
                start_response = client.post(
                    "/app/paypal/connect/start",
                    headers=HTML_ACCEPT_HEADERS,
                    follow_redirects=False,
                )

    assert start_response.status_code == 303
    assert start_response.headers["location"] == "/app/account?status=paypal-unavailable"
    assert provider.start_calls == []


def test_setup_home_paypal_connect_cta_redirects_non_operator_live_creator_to_unavailable_notice():
    inserted = _insert_creator_user(
        email=f"ui_paypal_cta_live_{uuid.uuid4().hex}@example.com",
        name="PayPal CTA Live Creator",
        stripe_connect_status="pending",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    provider = _StubPayPalProvider(
        readiness=BillingAccountReadiness(can_create_invoices=True)
    )
    settings = _live_paypal_operator_only_settings("ops@creatortrust.co")

    with _override_app_state("settings", settings):
        with _override_app_state("paypal_provider", provider):
            with TestClient(app) as client:
                client.cookies.set(SESSION_COOKIE_NAME, access_token)
                start_response = client.post(
                    "/app/paypal/connect/start",
                    headers=HTML_ACCEPT_HEADERS,
                    follow_redirects=False,
                )

    assert start_response.status_code == 303
    assert start_response.headers["location"] == "/app/account?status=paypal-unavailable"
    assert provider.start_calls == []


def test_setup_home_paypal_connect_cta_keeps_live_operator_path():
    inserted = _insert_creator_user(
        email=f"ui_paypal_cta_live_operator_{uuid.uuid4().hex}@example.com",
        name="PayPal CTA Live Operator",
        stripe_connect_status="pending",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    provider = _StubPayPalProvider(
        readiness=BillingAccountReadiness(can_create_invoices=True)
    )
    settings = _live_paypal_operator_only_settings(inserted["email"])

    with _override_app_state("settings", settings):
        with _override_app_state("paypal_provider", provider):
            with TestClient(app) as client:
                client.cookies.set(SESSION_COOKIE_NAME, access_token)
                start_response = client.post(
                    "/app/paypal/connect/start",
                    headers=HTML_ACCEPT_HEADERS,
                    follow_redirects=False,
                )

    assert start_response.status_code == 303
    assert start_response.headers["location"].startswith(
        "https://www.sandbox.paypal.com/bizsignup/partner/entry"
    )
    assert len(provider.start_calls) == 1


def test_browser_stripe_connect_callback_interrupted_redirects_to_setup_recovery():
    inserted = _insert_creator_user(
        email=f"ui_stripe_interrupted_{uuid.uuid4().hex}@example.com",
        name="Interrupted Stripe Creator",
        stripe_connect_status="pending",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    provider = _StubStripeProvider(account_id="acct_story59_interrupted")

    with _override_app_state("stripe_provider", provider):
        with TestClient(app) as client:
            client.cookies.set(SESSION_COOKIE_NAME, access_token)
            start_response = client.post(
                "/app/stripe/connect/start",
                headers=HTML_ACCEPT_HEADERS,
                follow_redirects=False,
            )
            start_location = start_response.headers["location"]
            start_query = parse_qs(urlparse(start_location).query)
            callback_response = client.get(
                "/stripe/connect/callback",
                params={
                    "error": "access_denied",
                    "state": start_query["state"][0],
                },
                headers=HTML_ACCEPT_HEADERS,
                follow_redirects=False,
            )
            recovery_response = client.get(
                callback_response.headers["location"],
                headers=HTML_ACCEPT_HEADERS,
            )

    assert start_response.status_code == 303
    assert callback_response.status_code == 303
    assert callback_response.headers["location"] == "/app?status=stripe-connect-interrupted"
    assert recovery_response.status_code == 200
    assert "Stripe setup was interrupted" in recovery_response.text
    assert "Start the Stripe step again when you are ready." in recovery_response.text
    assert "Start Stripe setup" in recovery_response.text


def test_browser_stripe_connect_callback_provider_failure_redirects_to_setup_recovery():
    inserted = _insert_creator_user(
        email=f"ui_stripe_failed_{uuid.uuid4().hex}@example.com",
        name="Failed Stripe Creator",
        stripe_connect_status="pending",
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    provider = _StubStripeProvider(
        callback_error=StripeProviderError(
            "stripe callback exchange failed",
            operation="stripe_connect_callback_exchange",
            http_status=400,
            error_code="invalid_grant",
        )
    )

    with _override_app_state("stripe_provider", provider):
        with TestClient(app) as client:
            client.cookies.set(SESSION_COOKIE_NAME, access_token)
            start_response = client.post(
                "/app/stripe/connect/start",
                headers=HTML_ACCEPT_HEADERS,
                follow_redirects=False,
            )
            start_location = start_response.headers["location"]
            start_query = parse_qs(urlparse(start_location).query)
            callback_response = client.get(
                "/stripe/connect/callback",
                params={
                    "code": "auth_code_story59_failed",
                    "state": start_query["state"][0],
                },
                headers=HTML_ACCEPT_HEADERS,
                follow_redirects=False,
            )
            recovery_response = client.get(
                callback_response.headers["location"],
                headers=HTML_ACCEPT_HEADERS,
            )

    assert start_response.status_code == 303
    assert callback_response.status_code == 303
    assert callback_response.headers["location"] == "/app?status=stripe-connect-failed"
    assert recovery_response.status_code == 200
    assert "Stripe could not finish connecting" in recovery_response.text
    assert "Try the Stripe step again from this page." in recovery_response.text
    assert "Start Stripe setup" in recovery_response.text


def test_app_shell_clears_expired_session_cookie():
    inserted = _insert_creator_user(email=f"ui_expired_{uuid.uuid4().hex}@example.com")
    expired_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(minutes=-1),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, expired_token)
        response = client.get("/app", headers=HTML_ACCEPT_HEADERS, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/sign-in"
    assert f"{SESSION_COOKIE_NAME}=" in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]


def test_account_page_clears_expired_session_cookie():
    inserted = _insert_creator_user(email=f"ui_account_expired_{uuid.uuid4().hex}@example.com")
    expired_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(minutes=-1),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, expired_token)
        response = client.get("/app/account", headers=HTML_ACCEPT_HEADERS, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/sign-in"
    assert f"{SESSION_COOKIE_NAME}=" in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]
