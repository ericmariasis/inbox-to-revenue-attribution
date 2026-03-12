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
from app.models.invoice_payment_event import InvoicePaymentEvent
from app.services.authoritative_content_evidence import get_authoritative_content_evidence
from app.services.booking_attribution import (
    BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED,
    BOOKING_UNATTRIBUTED_REASON_MISSING_TID,
    list_creator_booking_attribution_rows,
)
from app.services.calendly_webhooks import (
    build_default_calendly_webhook_router,
    verify_and_parse_calendly_webhook,
)
from app.services.content_fetch import ContentFetchSuccess
from app.services.creator_claim_snapshots import (
    CreateCreatorClaimSnapshotInput,
    create_creator_claim_snapshot,
    resolve_creator_claim_snapshot,
)
from app.services.email_stub import get_magic_link_outbox
from app.services.invoice_payment_events import (
    PAYMENT_PROVENANCE_CONFLICT_STATUS_NONE,
    PAYMENT_PROVENANCE_CONFLICT_STATUS_UNMATCHED_PROVIDER_SIGNAL,
    PAYMENT_PROVENANCE_STATE_CONFLICTING,
    PAYMENT_PROVENANCE_STATE_MATCHED,
    PAYMENT_PROVENANCE_STATE_PENDING,
    PAYMENT_PROVENANCE_STATE_UNMATCHED,
    PAYMENT_PROVENANCE_STATUS_MATCHED,
    UNATTRIBUTED_REASON_UNKNOWN_BOOKING_UUID,
)
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


def _verified_calendly_event_from_payload(*, payload: bytes):
    settings = get_settings()
    return verify_and_parse_calendly_webhook(
        payload=payload,
        signature_header=_calendly_signature_header(
            payload=payload,
            signing_key=settings.calendly_webhook_signing_key,
        ),
        signing_key=settings.calendly_webhook_signing_key,
        tolerance_seconds=settings.calendly_webhook_tolerance_seconds,
    )


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
            f"?response_type=code&client_id=ca_test_story82&state={state}&creator_id={creator_id}"
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


def test_phase12_5_claim_snapshot_replay_and_health_compose_end_to_end():
    engine = _engine()
    settings = get_settings()
    creator_email = f"phase12_5_creator_{uuid.uuid4().hex}@example.com"
    story64_dataset = load_story64_seed_dataset()
    original_case = story64_dataset.cases[0]
    promoted_case = story64_dataset.cases[1]
    provider = _SequencedStubStripeProvider(
        account_id="acct_story82_connected",
        readiness=StripeAccountReadiness(charges_enabled=True),
        created_invoice_ids=[
            "in_story82_snapshot",
            "in_story82_replay",
            "in_story82_conflict",
        ],
    )
    fetch_provider = _SequencedContentFetchProvider(
        results=[
            ContentFetchSuccess(
                fetched_url=original_case.source_url,
                http_status=original_case.snapshot.http_status or 200,
                response_content_type=original_case.snapshot.response_content_type,
                response_content_charset=original_case.snapshot.response_content_charset,
                snapshot_text=original_case.snapshot.snapshot_text or "",
            ),
            ContentFetchSuccess(
                fetched_url=original_case.source_url,
                http_status=promoted_case.snapshot.http_status or 200,
                response_content_type=promoted_case.snapshot.response_content_type,
                response_content_charset=promoted_case.snapshot.response_content_charset,
                snapshot_text=promoted_case.snapshot.snapshot_text or "",
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
                            "code": "auth_code_story82",
                            "state": connect_start_payload["state"],
                        },
                    )
                    me_response = client.get("/me", headers=auth_headers)
                    creator_id = uuid.UUID(me_response.json()["id"])

                    booking_link_response = client.post(
                        "/booking-links",
                        headers=auth_headers,
                        json={
                            "name": "Phase 12.5 Validation Call",
                            "calendly_url": "https://calendly.com/example/phase12-5-validation",
                            "billing_amount_cents": 19500,
                            "billing_currency": " usd ",
                        },
                    )
                    booking_link = booking_link_response.json()

                    content_response = client.post(
                        "/content",
                        headers=auth_headers,
                        json={
                            "source_url": original_case.source_url,
                            "booking_link_id": booking_link["id"],
                        },
                    )
                    content = content_response.json()

                    original_fetch_response = client.post(
                        f"/content/{content['tid']}/fetch",
                        headers=auth_headers,
                    )
                    original_extract_response = client.post(
                        f"/content/{content['tid']}/extract",
                        headers=auth_headers,
                    )
                    original_candidates_response = client.post(
                        f"/content/{content['tid']}/topics/candidates",
                        headers=auth_headers,
                    )
                    original_candidates = original_candidates_response.json()["candidate_topics"]
                    original_authoritative_candidate = original_candidates[0]
                    original_confirm_response = client.post(
                        f"/content/{content['tid']}/topics/{original_authoritative_candidate['id']}/confirm",
                        headers=auth_headers,
                        json={
                            "confirmed_label": original_authoritative_candidate["suggested_label"],
                        },
                    )
                    original_reject_responses = [
                        client.post(
                            f"/content/{content['tid']}/topics/{candidate['id']}/reject",
                            headers=auth_headers,
                        )
                        for candidate in original_candidates[1:]
                    ]
                    original_promote_response = client.post(
                        f"/content/{content['tid']}/authoritative-evidence/promote",
                        headers=auth_headers,
                    )

                    snapshot_create_payload = _invitee_created_payload(
                        event_id="EVT_story82_snapshot_create",
                        calendly_booking_uuid="BOOK_story82_snapshot",
                        tid=content["tid"],
                        email="phase12-5-snapshot@example.com",
                        created_at="2026-03-12T15:00:00Z",
                    )
                    snapshot_record_result = calendly_webhook_router.record_event(
                        event=_verified_calendly_event_from_payload(payload=snapshot_create_payload)
                    )
                    snapshot_processing_status = calendly_webhook_router.process_event(
                        record_id=snapshot_record_result.record_id
                    )

                    snapshot_paid_at = datetime(2026, 3, 12, 15, 30, tzinfo=timezone.utc)
                    snapshot_paid_payload = _invoice_paid_payload(
                        stripe_event_id="evt_story82_snapshot_paid",
                        stripe_account_id=provider.account_id,
                        stripe_invoice_id="in_story82_snapshot",
                        paid_at=snapshot_paid_at,
                    )
                    snapshot_paid_response = client.post(
                        "/webhooks/stripe",
                        content=snapshot_paid_payload,
                        headers={
                            "Content-Type": "application/json",
                            "Stripe-Signature": _stripe_signature_header(
                                payload=snapshot_paid_payload,
                                secret=settings.stripe_webhook_secret,
                            ),
                        },
                    )

                    with Session(engine) as session:
                        content_row = session.scalar(select(Content).where(Content.tid == content["tid"]))
                        authoritative_evidence = get_authoritative_content_evidence(
                            content=content_row,
                            db=session,
                        )
                        current_settled_snapshot = get_creator_settled_paid_evidence(
                            creator_id=creator_id,
                            db=session,
                            tid=content["tid"],
                        )
                        snapshot_row = next(
                            row
                            for row in current_settled_snapshot.settled_rows
                            if row.stripe_invoice_id == "in_story82_snapshot"
                        )
                        claim_snapshot = create_creator_claim_snapshot(
                            creator_id=creator_id,
                            input=CreateCreatorClaimSnapshotInput(
                                claim_kind="phase12_5_validation",
                                content_id=content_row.id,
                                authoritative_extraction_artifact_id=authoritative_evidence.artifact.id,
                                authoritative_fetch_snapshot_id=authoritative_evidence.fetch_snapshot.id,
                                settled_paid_evidence_rows=[snapshot_row],
                                claim_contract_version="phase12_5_validation.v1",
                                claim_reducer_version="phase12_5_validation.reducer.v1",
                                rendered_claim_text="Original claim snapshot",
                            ),
                            db=session,
                        )
                        unattributed_booking = Booking(
                            creator_id=creator_id,
                            booking_link_id=uuid.UUID(booking_link["id"]),
                            tid=None,
                            calendly_booking_uuid="BOOK_story82_unattributed",
                            email="phase12-5-unattributed@example.com",
                            status="created",
                            attribution_status=BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED,
                            unattributed_reason=BOOKING_UNATTRIBUTED_REASON_MISSING_TID,
                            booked_at=datetime(2026, 3, 12, 15, 45, tzinfo=timezone.utc),
                        )
                        session.add(unattributed_booking)
                        session.commit()

                    promoted_fetch_response = client.post(
                        f"/content/{content['tid']}/fetch",
                        headers=auth_headers,
                    )
                    promoted_extract_response = client.post(
                        f"/content/{content['tid']}/extract",
                        headers=auth_headers,
                    )
                    promoted_candidates_response = client.post(
                        f"/content/{content['tid']}/topics/candidates",
                        headers=auth_headers,
                    )
                    promoted_candidates = promoted_candidates_response.json()["candidate_topics"]
                    promoted_authoritative_candidate = promoted_candidates[0]
                    promoted_confirm_response = client.post(
                        f"/content/{content['tid']}/topics/{promoted_authoritative_candidate['id']}/confirm",
                        headers=auth_headers,
                        json={
                            "confirmed_label": promoted_authoritative_candidate["suggested_label"],
                        },
                    )
                    promoted_reject_responses = [
                        client.post(
                            f"/content/{content['tid']}/topics/{candidate['id']}/reject",
                            headers=auth_headers,
                        )
                        for candidate in promoted_candidates[1:]
                    ]
                    promoted_response = client.post(
                        f"/content/{content['tid']}/authoritative-evidence/promote",
                        headers=auth_headers,
                    )

                    replay_cancel_payload = _invitee_canceled_payload(
                        event_id="EVT_story82_replay_cancel",
                        calendly_booking_uuid="BOOK_story82_replay",
                        tid=content["tid"],
                        canceled_at="2026-03-12T16:30:00Z",
                    )
                    replay_cancel_result = calendly_webhook_router.record_event(
                        event=_verified_calendly_event_from_payload(payload=replay_cancel_payload)
                    )
                    replay_cancel_status = calendly_webhook_router.process_event(
                        record_id=replay_cancel_result.record_id
                    )
                    replay_create_payload = _invitee_created_payload(
                        event_id="EVT_story82_replay_create",
                        calendly_booking_uuid="BOOK_story82_replay",
                        tid=content["tid"],
                        email="phase12-5-replay@example.com",
                        created_at="2026-03-12T16:00:00Z",
                    )
                    replay_create_result = calendly_webhook_router.record_event(
                        event=_verified_calendly_event_from_payload(payload=replay_create_payload)
                    )
                    replay_create_status = calendly_webhook_router.process_event(
                        record_id=replay_create_result.record_id
                    )
                    replay_result = calendly_webhook_router.reprocess_event(
                        record_id=replay_cancel_result.record_id
                    )

                    conflict_create_payload = _invitee_created_payload(
                        event_id="EVT_story82_conflict_create",
                        calendly_booking_uuid="BOOK_story82_conflict",
                        tid=content["tid"],
                        email="phase12-5-conflict@example.com",
                        created_at="2026-03-12T17:00:00Z",
                    )
                    conflict_record_result = calendly_webhook_router.record_event(
                        event=_verified_calendly_event_from_payload(payload=conflict_create_payload)
                    )
                    conflict_processing_status = calendly_webhook_router.process_event(
                        record_id=conflict_record_result.record_id
                    )
                    conflict_paid_at = datetime(2026, 3, 12, 17, 20, tzinfo=timezone.utc)
                    conflict_paid_payload = _invoice_paid_payload(
                        stripe_event_id="evt_story82_conflict_paid",
                        stripe_account_id=provider.account_id,
                        stripe_invoice_id="in_story82_conflict",
                        paid_at=conflict_paid_at,
                    )
                    conflict_paid_response = client.post(
                        "/webhooks/stripe",
                        content=conflict_paid_payload,
                        headers={
                            "Content-Type": "application/json",
                            "Stripe-Signature": _stripe_signature_header(
                                payload=conflict_paid_payload,
                                secret=settings.stripe_webhook_secret,
                            ),
                        },
                    )

                    with Session(engine) as session:
                        session.add(
                            InvoicePaymentEvent(
                                stripe_event_id="evt_story82_conflict_unmatched",
                                stripe_event_type="invoice.paid",
                                stripe_account_id=provider.account_id,
                                stripe_invoice_id="in_story82_conflict",
                                invoice_id=None,
                                creator_id=creator_id,
                                booking_id=None,
                                tid=None,
                                status="unmatched",
                                unattributed_reason=UNATTRIBUTED_REASON_UNKNOWN_BOOKING_UUID,
                                paid_at=conflict_paid_at,
                                received_at=conflict_paid_at,
                                processed_at=None,
                            )
                        )
                        session.commit()

                    health_response = client.get("/reports/health", headers=auth_headers)

    with Session(engine) as session:
        content_row = session.scalar(select(Content).where(Content.tid == content["tid"]))
        current_authoritative = get_authoritative_content_evidence(content=content_row, db=session)
        current_settled_snapshot = get_creator_settled_paid_evidence(
            creator_id=creator_id,
            db=session,
            tid=content["tid"],
        )
        resolved_claim_snapshot = resolve_creator_claim_snapshot(
            creator_id=creator_id,
            claim_snapshot_id=claim_snapshot.id,
            db=session,
        )
        attribution_rows = list_creator_booking_attribution_rows(
            creator_id=creator_id,
            db=session,
        )
        replay_booking = session.scalar(
            select(Booking).where(Booking.calendly_booking_uuid == "BOOK_story82_replay")
        )
        replay_invoice = session.scalar(
            select(Invoice)
            .join(Booking, Booking.id == Invoice.booking_id)
            .where(Booking.calendly_booking_uuid == "BOOK_story82_replay")
        )
        snapshot_record = session.scalar(
            select(CalendlyWebhookEventRecord).where(
                CalendlyWebhookEventRecord.calendly_event_id == "EVT_story82_snapshot_create"
            )
        )
        replay_cancel_record = session.scalar(
            select(CalendlyWebhookEventRecord).where(
                CalendlyWebhookEventRecord.calendly_event_id == "EVT_story82_replay_cancel"
            )
        )
        replay_create_record = session.scalar(
            select(CalendlyWebhookEventRecord).where(
                CalendlyWebhookEventRecord.calendly_event_id == "EVT_story82_replay_create"
            )
        )
        conflict_record = session.scalar(
            select(CalendlyWebhookEventRecord).where(
                CalendlyWebhookEventRecord.calendly_event_id == "EVT_story82_conflict_create"
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

    assert original_fetch_response.status_code == 201
    assert original_extract_response.status_code == 201
    assert original_candidates_response.status_code == 201
    assert original_confirm_response.status_code == 200
    assert all(response.status_code == 200 for response in original_reject_responses)
    assert original_promote_response.status_code == 200
    assert (
        original_promote_response.json()["authoritative_state"][
            "authoritative_extraction_artifact_id"
        ]
        == original_extract_response.json()["id"]
    )

    assert snapshot_record_result.outcome == "recorded"
    assert snapshot_record_result.processing_status == "received"
    assert snapshot_record_result.reducer_key == "booking:BOOK_story82_snapshot"
    assert snapshot_record_result.should_schedule_reducer is True
    assert snapshot_processing_status == "applied"
    assert snapshot_paid_response.status_code == 200
    assert snapshot_paid_response.json() == {"status": "ok"}

    assert promoted_fetch_response.status_code == 201
    assert promoted_extract_response.status_code == 201
    assert promoted_candidates_response.status_code == 201
    assert promoted_confirm_response.status_code == 200
    assert all(response.status_code == 200 for response in promoted_reject_responses)
    assert promoted_response.status_code == 200
    assert (
        promoted_response.json()["authoritative_state"]["authoritative_extraction_artifact_id"]
        == promoted_extract_response.json()["id"]
    )
    assert promoted_extract_response.json()["id"] != original_extract_response.json()["id"]

    assert replay_cancel_result.outcome == "recorded"
    assert replay_cancel_status == "deferred_missing_booking"
    assert replay_create_result.outcome == "recorded"
    assert replay_create_status == "applied"
    assert replay_result.outcome == "reprocessed"
    assert replay_result.processing_status == "applied"

    assert conflict_record_result.outcome == "recorded"
    assert conflict_processing_status == "applied"
    assert conflict_paid_response.status_code == 200
    assert conflict_paid_response.json() == {"status": "ok"}

    assert current_authoritative is not None
    assert str(current_authoritative.artifact.id) == promoted_extract_response.json()["id"]
    assert current_authoritative.artifact.title == promoted_case.expected_extraction.title
    assert [topic.canonical_label for topic in current_authoritative.confirmed_topics] == [
        promoted_authoritative_candidate["suggested_label"]
    ]

    assert len(current_settled_snapshot.settled_rows) == 2
    snapshot_row = next(
        row
        for row in current_settled_snapshot.settled_rows
        if row.stripe_invoice_id == "in_story82_snapshot"
    )
    conflict_row = next(
        row
        for row in current_settled_snapshot.settled_rows
        if row.stripe_invoice_id == "in_story82_conflict"
    )
    assert snapshot_row.payment_provenance.status == PAYMENT_PROVENANCE_STATUS_MATCHED
    assert snapshot_row.payment_provenance.conflict_status == PAYMENT_PROVENANCE_CONFLICT_STATUS_NONE
    assert snapshot_row.payment_provenance.state == PAYMENT_PROVENANCE_STATE_MATCHED
    assert conflict_row.payment_provenance.status == PAYMENT_PROVENANCE_STATUS_MATCHED
    assert (
        conflict_row.payment_provenance.conflict_status
        == PAYMENT_PROVENANCE_CONFLICT_STATUS_UNMATCHED_PROVIDER_SIGNAL
    )
    assert conflict_row.payment_provenance.conflict_event_count == 1
    assert conflict_row.payment_provenance.conflict_reasons == (
        UNATTRIBUTED_REASON_UNKNOWN_BOOKING_UUID,
    )
    assert conflict_row.payment_provenance.state == PAYMENT_PROVENANCE_STATE_CONFLICTING
    assert current_settled_snapshot.unmatched_payment_backlog.event_count == 1
    assert [
        (item.reason, item.event_count)
        for item in current_settled_snapshot.unmatched_payment_backlog.reasons
    ] == [
        (UNATTRIBUTED_REASON_UNKNOWN_BOOKING_UUID, 1),
    ]
    assert current_settled_snapshot.blocked_billing_backlog.open_case_count == 0
    assert current_settled_snapshot.blocked_billing_backlog.reasons == []

    assert resolved_claim_snapshot is not None
    assert resolved_claim_snapshot.snapshot.claim_contract_version == "phase12_5_validation.v1"
    assert resolved_claim_snapshot.snapshot.claim_reducer_version == "phase12_5_validation.reducer.v1"
    assert resolved_claim_snapshot.snapshot.rendered_claim_text == "Original claim snapshot"
    assert str(resolved_claim_snapshot.authoritative_content_evidence.artifact.id) == (
        original_extract_response.json()["id"]
    )
    assert str(resolved_claim_snapshot.authoritative_content_evidence.fetch_snapshot.id) == (
        original_fetch_response.json()["id"]
    )
    assert [row.stripe_invoice_id for row in resolved_claim_snapshot.settled_paid_evidence_rows] == [
        "in_story82_snapshot"
    ]
    assert [
        row.payment_provenance.state for row in resolved_claim_snapshot.settled_paid_evidence_rows
    ] == [
        PAYMENT_PROVENANCE_STATE_MATCHED,
    ]

    unattributed_row = next(
        row
        for row in attribution_rows
        if row.attribution.status == BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED
    )
    assert unattributed_row.attribution.unattributed_reason == BOOKING_UNATTRIBUTED_REASON_MISSING_TID
    assert unattributed_row.attribution.tid is None
    assert unattributed_row.source_url is None

    assert replay_booking is not None
    assert replay_booking.status == "canceled"
    assert replay_booking.canceled_at == datetime(2026, 3, 12, 16, 30, tzinfo=timezone.utc)
    assert replay_booking.frozen_billing_amount_cents == 19500
    assert replay_booking.frozen_billing_currency == "USD"
    assert replay_invoice is not None
    assert replay_invoice.status == "void"
    assert replay_invoice.stripe_invoice_id == "in_story82_replay"
    assert replay_invoice.paid_at is None

    assert snapshot_record is not None
    assert snapshot_record.processing_status == "applied"
    assert snapshot_record.reducer_key == "booking:BOOK_story82_snapshot"
    assert snapshot_record.reducer_attempt_count == 1
    assert snapshot_record.processed_at is not None
    assert replay_cancel_record is not None
    assert replay_cancel_record.processing_status == "applied"
    assert replay_cancel_record.reducer_key == "booking:BOOK_story82_replay"
    assert replay_cancel_record.reducer_attempt_count == 2
    assert replay_create_record is not None
    assert replay_create_record.processing_status == "applied"
    assert replay_create_record.reducer_key == "booking:BOOK_story82_replay"
    assert replay_create_record.reducer_attempt_count == 1
    assert conflict_record is not None
    assert conflict_record.processing_status == "applied"
    assert conflict_record.reducer_key == "booking:BOOK_story82_conflict"
    assert conflict_record.reducer_attempt_count == 1

    assert health_response.status_code == 200
    assert health_response.json()["booking_attribution"]["unattributed_booking_count"] == 1
    assert health_response.json()["booking_attribution"]["reasons"] == [
        {"reason": "MISSING_TID", "booking_count": 1},
        {"reason": "UNKNOWN_TID", "booking_count": 0},
    ]
    assert health_response.json()["calendly_ingress"]["backlog_event_count"] == 0
    assert health_response.json()["calendly_ingress"]["failed_event_count"] == 0
    assert health_response.json()["payment_provenance"]["settled_state_counts"] == [
        {"state": PAYMENT_PROVENANCE_STATE_MATCHED, "row_count": 1},
        {"state": PAYMENT_PROVENANCE_STATE_PENDING, "row_count": 0},
        {"state": PAYMENT_PROVENANCE_STATE_UNMATCHED, "row_count": 0},
        {"state": PAYMENT_PROVENANCE_STATE_CONFLICTING, "row_count": 1},
    ]
    assert health_response.json()["payment_provenance"]["current_backlog_event_count"] == 1
    assert health_response.json()["payment_provenance"]["current_backlog_reasons"] == [
        {"reason": UNATTRIBUTED_REASON_UNKNOWN_BOOKING_UUID, "event_count": 1},
    ]
    assert health_response.json()["blocked_billing"] == {
        "open_case_count": 0,
        "reasons": [],
    }
    assert health_response.json()["authoritative_content"] == {
        "lagging_content_count": 0,
        "reasons": [
            {"reason": "missing_authoritative_evidence", "content_count": 0},
            {"reason": "stale_authoritative_evidence", "content_count": 0},
        ],
    }

    assert fetch_provider.calls == [original_case.source_url, original_case.source_url]
    assert provider.exchange_calls == [
        {
            "code": "auth_code_story82",
            "state": connect_start_payload["state"],
        }
    ]
    assert provider.readiness_calls == [
        provider.account_id,
        provider.account_id,
        provider.account_id,
    ]
    assert provider.create_calls == [
        {
            "stripe_account_id": provider.account_id,
            "amount_cents": 19500,
            "currency": "USD",
            "metadata": {
                "creator_id": me_response.json()["id"],
                "booking_uuid": "BOOK_story82_snapshot",
                "tid": content["tid"],
            },
            "idempotency_key": "billing:create:BOOK_story82_snapshot",
        },
        {
            "stripe_account_id": provider.account_id,
            "amount_cents": 19500,
            "currency": "USD",
            "metadata": {
                "creator_id": me_response.json()["id"],
                "booking_uuid": "BOOK_story82_replay",
                "tid": content["tid"],
            },
            "idempotency_key": "billing:create:BOOK_story82_replay",
        },
        {
            "stripe_account_id": provider.account_id,
            "amount_cents": 19500,
            "currency": "USD",
            "metadata": {
                "creator_id": me_response.json()["id"],
                "booking_uuid": "BOOK_story82_conflict",
                "tid": content["tid"],
            },
            "idempotency_key": "billing:create:BOOK_story82_conflict",
        },
    ]
    assert provider.void_calls == [
        {
            "stripe_account_id": provider.account_id,
            "stripe_invoice_id": "in_story82_replay",
        }
    ]
