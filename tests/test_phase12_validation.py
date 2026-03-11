import hashlib
import hmac
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from time import time

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.evals.content_pipeline import load_story64_seed_dataset
from app.main import app
from app.models.booking import Booking
from app.models.calendly_webhook_event import CalendlyWebhookEventRecord
from app.models.content import Content
from app.models.invoice import Invoice
from app.services.authoritative_content_evidence import get_authoritative_content_evidence
from app.services.calendly_webhooks import build_default_calendly_webhook_router
from app.services.content_fetch import ContentFetchSuccess
from app.services.email_stub import get_magic_link_outbox
from app.services.settled_paid_evidence import get_creator_settled_paid_evidence
from app.services.stripe_provider import StripeAccountReadiness, StripeInvoiceCreateResult


def _engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def _latest_magic_link_token_for_email(email: str) -> str:
    outbox = get_magic_link_outbox()
    for message in reversed(outbox):
        if message["email"] == email:
            return message["token"]

    raise AssertionError(f"No magic-link token captured for {email}")


def _calendly_signature_header(
    *,
    payload: bytes,
    signing_key: str,
    timestamp: int | None = None,
) -> str:
    resolved_timestamp = timestamp or int(time())
    signed_payload = f"{resolved_timestamp}.".encode("utf-8") + payload
    signature = hmac.new(signing_key.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={resolved_timestamp},v1={signature}"


def _stripe_signature_header(
    *,
    payload: bytes,
    secret: str,
    timestamp: int | None = None,
) -> str:
    resolved_timestamp = timestamp or int(time())
    signed_payload = f"{resolved_timestamp}.".encode("utf-8") + payload
    signature = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={resolved_timestamp},v1={signature}"


def _invitee_created_payload(
    *,
    event_id: str,
    calendly_booking_uuid: str,
    tid: str,
    email: str,
    created_at: str,
) -> bytes:
    return json.dumps(
        {
            "event": "invitee.created",
            "payload": {
                "event": f"https://api.calendly.com/scheduled_events/{event_id}",
                "uri": (
                    "https://api.calendly.com/scheduled_events/"
                    f"{event_id}/invitees/{calendly_booking_uuid}"
                ),
                "email": email,
                "created_at": created_at,
                "tracking": {"utm_content": tid},
            },
        }
    ).encode("utf-8")


def _invitee_canceled_payload(
    *,
    event_id: str,
    calendly_booking_uuid: str,
    tid: str,
    canceled_at: str,
) -> bytes:
    return json.dumps(
        {
            "event": "invitee.canceled",
            "payload": {
                "event": f"https://api.calendly.com/scheduled_events/{event_id}",
                "uri": (
                    "https://api.calendly.com/scheduled_events/"
                    f"{event_id}/invitees/{calendly_booking_uuid}"
                ),
                "tracking": {"utm_content": tid},
                "canceled_at": canceled_at,
            },
        }
    ).encode("utf-8")


def _invoice_paid_payload(
    *,
    stripe_event_id: str,
    stripe_account_id: str,
    stripe_invoice_id: str,
    paid_at: datetime,
    metadata: dict[str, str] | None = None,
) -> bytes:
    return json.dumps(
        {
            "id": stripe_event_id,
            "type": "invoice.paid",
            "account": stripe_account_id,
            "data": {
                "object": {
                    "id": stripe_invoice_id,
                    "object": "invoice",
                    "status": "paid",
                    "status_transitions": {"paid_at": int(paid_at.timestamp())},
                    "metadata": metadata or {},
                }
            },
        }
    ).encode("utf-8")


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


class _SequencedStubStripeProvider:
    def __init__(
        self,
        *,
        account_id: str,
        readiness: StripeAccountReadiness,
        created_invoice_ids: list[str],
    ):
        self.account_id = account_id
        self.readiness = readiness
        self._created_invoice_ids = list(created_invoice_ids)
        self.onboarding_calls: list[dict[str, str]] = []
        self.exchange_calls: list[dict[str, str]] = []
        self.readiness_calls: list[str] = []
        self.create_calls: list[dict[str, object]] = []
        self.void_calls: list[dict[str, str]] = []

    def build_connect_onboarding_url(self, *, creator_id: str, state: str) -> str:
        self.onboarding_calls.append({"creator_id": creator_id, "state": state})
        return (
            "https://connect.stripe.com/oauth/authorize"
            f"?response_type=code&client_id=ca_test_story70&state={state}&creator_id={creator_id}"
        )

    def exchange_connect_callback(self, *, code: str, state: str) -> str:
        self.exchange_calls.append({"code": code, "state": state})
        return self.account_id

    def get_account_readiness(self, *, stripe_account_id: str) -> StripeAccountReadiness:
        self.readiness_calls.append(stripe_account_id)
        return self.readiness

    def create_invoice(
        self,
        *,
        stripe_account_id: str,
        amount_cents: int,
        currency: str,
        metadata: dict[str, str],
        idempotency_key: str,
    ) -> StripeInvoiceCreateResult:
        if not self._created_invoice_ids:
            raise AssertionError("No stubbed Stripe invoice id remaining for create_invoice")

        self.create_calls.append(
            {
                "stripe_account_id": stripe_account_id,
                "amount_cents": amount_cents,
                "currency": currency,
                "metadata": metadata,
                "idempotency_key": idempotency_key,
            }
        )
        return StripeInvoiceCreateResult(stripe_invoice_id=self._created_invoice_ids.pop(0))

    def void_invoice(self, *, stripe_account_id: str, stripe_invoice_id: str) -> None:
        self.void_calls.append(
            {
                "stripe_account_id": stripe_account_id,
                "stripe_invoice_id": stripe_invoice_id,
            }
        )


class _SequencedContentFetchProvider:
    def __init__(self, *, results: list[ContentFetchSuccess]):
        self._results = list(results)
        self.calls: list[str] = []

    def fetch_public_url(self, *, source_url: str) -> ContentFetchSuccess:
        self.calls.append(source_url)
        if not self._results:
            raise AssertionError("No stubbed fetch result remaining")
        return self._results.pop(0)


def test_phase12_evidence_truth_and_replay_flow_end_to_end():
    engine = _engine()
    settings = get_settings()
    creator_email = f"phase12_creator_{uuid.uuid4().hex}@example.com"
    story64_dataset = load_story64_seed_dataset()
    older_case = story64_dataset.cases[0]
    newer_case = story64_dataset.cases[1]
    provider = _SequencedStubStripeProvider(
        account_id="acct_story70_connected",
        readiness=StripeAccountReadiness(charges_enabled=True),
        created_invoice_ids=["in_story70_paid", "in_story70_replay"],
    )
    fetch_provider = _SequencedContentFetchProvider(
        results=[
            ContentFetchSuccess(
                fetched_url=older_case.source_url,
                http_status=older_case.snapshot.http_status or 200,
                response_content_type=older_case.snapshot.response_content_type,
                response_content_charset=older_case.snapshot.response_content_charset,
                snapshot_text=older_case.snapshot.snapshot_text or "",
            ),
            ContentFetchSuccess(
                fetched_url=older_case.source_url,
                http_status=newer_case.snapshot.http_status or 200,
                response_content_type=newer_case.snapshot.response_content_type,
                response_content_charset=newer_case.snapshot.response_content_charset,
                snapshot_text=newer_case.snapshot.snapshot_text or "",
            ),
        ]
    )
    calendly_webhook_router = build_default_calendly_webhook_router(provider=provider)

    with _override_app_state("stripe_provider", provider):
        with _override_app_state("content_fetch_provider", fetch_provider):
            with _override_app_state("calendly_webhook_router", calendly_webhook_router):
                with TestClient(app) as client:
                    start_response = client.post(
                        "/auth/magic-link/start",
                        json={"email": creator_email},
                    )
                    verify_response = client.get(
                        "/auth/magic-link/verify",
                        params={"token": _latest_magic_link_token_for_email(creator_email)},
                    )
                    access_token = verify_response.json()["access_token"]
                    auth_headers = {"Authorization": f"Bearer {access_token}"}

                    connect_start_response = client.post(
                        "/stripe/connect/start",
                        headers=auth_headers,
                    )
                    connect_start_payload = connect_start_response.json()
                    callback_response = client.get(
                        "/stripe/connect/callback",
                        params={
                            "code": "auth_code_story70",
                            "state": connect_start_payload["state"],
                        },
                    )
                    me_response = client.get("/me", headers=auth_headers)
                    creator_id = uuid.UUID(me_response.json()["id"])

                    booking_link_response = client.post(
                        "/booking-links",
                        headers=auth_headers,
                        json={
                            "name": "Phase 12 Validation Call",
                            "calendly_url": "https://calendly.com/example/phase12-validation-call",
                            "billing_amount_cents": 19500,
                            "billing_currency": " usd ",
                        },
                    )
                    booking_link = booking_link_response.json()

                    content_response = client.post(
                        "/content",
                        headers=auth_headers,
                        json={
                            "source_url": older_case.source_url,
                            "booking_link_id": booking_link["id"],
                        },
                    )
                    content = content_response.json()

                    older_fetch_response = client.post(
                        f"/content/{content['tid']}/fetch",
                        headers=auth_headers,
                    )
                    older_extract_response = client.post(
                        f"/content/{content['tid']}/extract",
                        headers=auth_headers,
                    )
                    older_candidates_response = client.post(
                        f"/content/{content['tid']}/topics/candidates",
                        headers=auth_headers,
                    )
                    older_candidates = older_candidates_response.json()["candidate_topics"]
                    authoritative_candidate = older_candidates[0]

                    confirm_response = client.post(
                        f"/content/{content['tid']}/topics/{authoritative_candidate['id']}/confirm",
                        headers=auth_headers,
                        json={"confirmed_label": authoritative_candidate["suggested_label"]},
                    )
                    reject_responses = []
                    for candidate in older_candidates[1:]:
                        reject_responses.append(
                            client.post(
                                f"/content/{content['tid']}/topics/{candidate['id']}/reject",
                                headers=auth_headers,
                            )
                        )
                    promote_response = client.post(
                        f"/content/{content['tid']}/authoritative-evidence/promote",
                        headers=auth_headers,
                    )

                    newer_fetch_response = client.post(
                        f"/content/{content['tid']}/fetch",
                        headers=auth_headers,
                    )
                    newer_extract_response = client.post(
                        f"/content/{content['tid']}/extract",
                        headers=auth_headers,
                    )

                    paid_booking_payload = _invitee_created_payload(
                        event_id="EVT_story70_paid_create",
                        calendly_booking_uuid="BOOK_story70_paid",
                        tid=content["tid"],
                        email="phase12-paid@example.com",
                        created_at="2026-03-11T18:00:00Z",
                    )
                    paid_booking_signature = _calendly_signature_header(
                        payload=paid_booking_payload,
                        signing_key=settings.calendly_webhook_signing_key,
                    )
                    paid_booking_response = client.post(
                        "/webhooks/calendly",
                        content=paid_booking_payload,
                        headers={
                            "Content-Type": "application/json",
                            "Calendly-Webhook-Signature": paid_booking_signature,
                        },
                    )

                    paid_at = datetime(2026, 3, 11, 18, 30, tzinfo=timezone.utc)
                    paid_payload = _invoice_paid_payload(
                        stripe_event_id="evt_story70_paid",
                        stripe_account_id=provider.account_id,
                        stripe_invoice_id="in_story70_paid",
                        paid_at=paid_at,
                    )
                    paid_signature = _stripe_signature_header(
                        payload=paid_payload,
                        secret=settings.stripe_webhook_secret,
                    )
                    paid_response = client.post(
                        "/webhooks/stripe",
                        content=paid_payload,
                        headers={
                            "Content-Type": "application/json",
                            "Stripe-Signature": paid_signature,
                        },
                    )

                    deferred_cancel_payload = _invitee_canceled_payload(
                        event_id="EVT_story70_replay_cancel",
                        calendly_booking_uuid="BOOK_story70_replay",
                        tid=content["tid"],
                        canceled_at="2026-03-11T19:00:00Z",
                    )
                    deferred_cancel_signature = _calendly_signature_header(
                        payload=deferred_cancel_payload,
                        signing_key=settings.calendly_webhook_signing_key,
                    )
                    deferred_cancel_response = client.post(
                        "/webhooks/calendly",
                        content=deferred_cancel_payload,
                        headers={
                            "Content-Type": "application/json",
                            "Calendly-Webhook-Signature": deferred_cancel_signature,
                        },
                    )

                    with Session(engine) as session:
                        deferred_record = session.scalar(
                            select(CalendlyWebhookEventRecord).where(
                                CalendlyWebhookEventRecord.calendly_event_id
                                == "EVT_story70_replay_cancel"
                            )
                        )

                    replay_create_payload = _invitee_created_payload(
                        event_id="EVT_story70_replay_create",
                        calendly_booking_uuid="BOOK_story70_replay",
                        tid=content["tid"],
                        email="phase12-replay@example.com",
                        created_at="2026-03-11T18:45:00Z",
                    )
                    replay_create_signature = _calendly_signature_header(
                        payload=replay_create_payload,
                        signing_key=settings.calendly_webhook_signing_key,
                    )
                    replay_create_response = client.post(
                        "/webhooks/calendly",
                        content=replay_create_payload,
                        headers={
                            "Content-Type": "application/json",
                            "Calendly-Webhook-Signature": replay_create_signature,
                        },
                    )
                    replay_result = calendly_webhook_router.reprocess_event(
                        record_id=deferred_record.id
                    )

    with Session(engine) as session:
        content_row = session.scalar(select(Content).where(Content.tid == content["tid"]))
        authoritative_evidence = get_authoritative_content_evidence(
            content=content_row,
            db=session,
        )
        settled_snapshot = get_creator_settled_paid_evidence(
            creator_id=creator_id,
            db=session,
        )
        paid_booking = session.scalar(
            select(Booking).where(Booking.calendly_booking_uuid == "BOOK_story70_paid")
        )
        replay_booking = session.scalar(
            select(Booking).where(Booking.calendly_booking_uuid == "BOOK_story70_replay")
        )
        paid_invoice = session.scalar(
            select(Invoice).join(Booking).where(Booking.calendly_booking_uuid == "BOOK_story70_paid")
        )
        replay_invoice = session.scalar(
            select(Invoice).join(Booking).where(Booking.calendly_booking_uuid == "BOOK_story70_replay")
        )
        deferred_cancel_record = session.scalar(
            select(CalendlyWebhookEventRecord).where(
                CalendlyWebhookEventRecord.calendly_event_id == "EVT_story70_replay_cancel"
            )
        )
        replay_create_record = session.scalar(
            select(CalendlyWebhookEventRecord).where(
                CalendlyWebhookEventRecord.calendly_event_id == "EVT_story70_replay_create"
            )
        )

    assert start_response.status_code == 200
    assert start_response.json() == {"status": "ok"}
    assert verify_response.status_code == 200
    assert connect_start_response.status_code == 200
    assert callback_response.status_code == 200
    assert me_response.status_code == 200

    assert booking_link_response.status_code == 201
    assert booking_link["billing_amount_cents"] == 19500
    assert booking_link["billing_currency"] == "USD"
    assert content_response.status_code == 201

    assert older_fetch_response.status_code == 201
    assert older_fetch_response.json()["fetch_status"] == "succeeded"
    assert older_extract_response.status_code == 201
    assert older_extract_response.json()["title"] == older_case.expected_extraction.title
    assert older_candidates_response.status_code == 201
    assert older_candidates
    assert confirm_response.status_code == 200
    assert all(response.status_code == 200 for response in reject_responses)
    assert promote_response.status_code == 200
    assert (
        promote_response.json()["authoritative_state"]["authoritative_extraction_artifact_id"]
        == older_extract_response.json()["id"]
    )

    assert newer_fetch_response.status_code == 201
    assert newer_fetch_response.json()["fetch_status"] == "succeeded"
    assert newer_extract_response.status_code == 201
    assert newer_extract_response.json()["title"] == newer_case.expected_extraction.title
    assert newer_extract_response.json()["id"] != older_extract_response.json()["id"]

    assert paid_booking_response.status_code == 200
    assert paid_booking_response.json() == {"status": "ok"}
    assert paid_response.status_code == 200
    assert paid_response.json() == {"status": "ok"}
    assert deferred_cancel_response.status_code == 200
    assert deferred_cancel_response.json() == {"status": "ok"}
    assert replay_create_response.status_code == 200
    assert replay_create_response.json() == {"status": "ok"}
    assert replay_result.outcome == "reprocessed"
    assert replay_result.processing_status == "applied"

    assert authoritative_evidence is not None
    assert str(authoritative_evidence.artifact.id) == older_extract_response.json()["id"]
    assert authoritative_evidence.artifact.title == older_case.expected_extraction.title
    assert [topic.canonical_label for topic in authoritative_evidence.confirmed_topics] == [
        authoritative_candidate["suggested_label"]
    ]

    assert len(settled_snapshot.settled_rows) == 1
    settled_row = settled_snapshot.settled_rows[0]
    assert settled_row.tid == content["tid"]
    assert settled_row.source_url == older_case.source_url
    assert settled_row.stripe_invoice_id == "in_story70_paid"
    assert settled_row.payment_event_status == "applied"
    assert settled_row.invoice_paid_at == paid_at
    assert settled_snapshot.unmatched_payment_backlog.event_count == 0
    assert settled_snapshot.unmatched_payment_backlog.reasons == []
    assert settled_snapshot.blocked_billing_backlog.open_case_count == 0
    assert settled_snapshot.blocked_billing_backlog.reasons == []

    assert paid_booking is not None
    assert paid_booking.status == "created"
    assert paid_booking.frozen_billing_amount_cents == 19500
    assert paid_booking.frozen_billing_currency == "USD"
    assert replay_booking is not None
    assert replay_booking.status == "canceled"
    assert replay_booking.canceled_at == datetime(2026, 3, 11, 19, 0, tzinfo=timezone.utc)
    assert replay_booking.frozen_billing_amount_cents == 19500
    assert replay_booking.frozen_billing_currency == "USD"

    assert paid_invoice is not None
    assert paid_invoice.status == "paid"
    assert paid_invoice.stripe_invoice_id == "in_story70_paid"
    assert paid_invoice.paid_at == paid_at
    assert replay_invoice is not None
    assert replay_invoice.status == "void"
    assert replay_invoice.stripe_invoice_id == "in_story70_replay"
    assert replay_invoice.paid_at is None

    assert deferred_cancel_record is not None
    assert deferred_cancel_record.processing_status == "applied"
    assert deferred_cancel_record.delivery_count == 1
    assert deferred_cancel_record.tid == content["tid"]
    assert deferred_cancel_record.processed_at is not None
    assert replay_create_record is not None
    assert replay_create_record.processing_status == "applied"
    assert replay_create_record.delivery_count == 1
    assert replay_create_record.tid == content["tid"]
    assert replay_create_record.processed_at is not None

    assert fetch_provider.calls == [older_case.source_url, older_case.source_url]
    assert provider.onboarding_calls == [
        {
            "creator_id": me_response.json()["id"],
            "state": connect_start_payload["state"],
        }
    ]
    assert provider.exchange_calls == [
        {
            "code": "auth_code_story70",
            "state": connect_start_payload["state"],
        }
    ]
    assert provider.readiness_calls == [provider.account_id, provider.account_id]
    assert provider.create_calls == [
        {
            "stripe_account_id": provider.account_id,
            "amount_cents": 19500,
            "currency": "USD",
            "metadata": {
                "creator_id": me_response.json()["id"],
                "booking_uuid": "BOOK_story70_paid",
                "tid": content["tid"],
            },
            "idempotency_key": "billing:create:BOOK_story70_paid",
        },
        {
            "stripe_account_id": provider.account_id,
            "amount_cents": 19500,
            "currency": "USD",
            "metadata": {
                "creator_id": me_response.json()["id"],
                "booking_uuid": "BOOK_story70_replay",
                "tid": content["tid"],
            },
            "idempotency_key": "billing:create:BOOK_story70_replay",
        },
    ]
    assert provider.void_calls == [
        {
            "stripe_account_id": provider.account_id,
            "stripe_invoice_id": "in_story70_replay",
        }
    ]
