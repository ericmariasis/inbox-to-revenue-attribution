import json
import os
import uuid
from contextlib import contextmanager
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.main import app
from app.models.booking import Booking
from app.models.booking_link import BookingLink
from app.models.content import Content
from app.models.creator import Creator
from app.models.fullscope_webhook_event import FullScopeWebhookEventRecord
from app.services.fullscope_webhooks import (
    DefaultFullScopeWebhookRouter,
    FullScopeWebhookJournalRecordResult,
    verify_and_parse_fullscope_webhook,
)


def _fullscope_authorization(secret: str) -> str:
    return f"Bearer {secret}"


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


class _StubSettings:
    fullscope_webhook_shared_secret = "fullscope_story_fs5_test"


class _CaptureFullScopeWebhookRouter:
    def __init__(self):
        self.events: list[dict[str, str | None]] = []
        self.processed_record_ids: list[uuid.UUID] = []

    def record_event(self, *, event) -> FullScopeWebhookJournalRecordResult:
        self.events.append(
            {
                "provider_event_type": event.provider_event_type,
                "event_type": event.event_type,
                "appointment_id": event.appointment_id,
                "appointment_id_path": event.appointment_id_path,
                "calendar_id": event.calendar_id,
                "calendar_id_path": event.calendar_id_path,
                "workflow_id": event.workflow_id,
                "workflow_id_path": event.workflow_id_path,
                "tid": event.tid,
                "tid_path": event.tid_path,
            }
        )
        return FullScopeWebhookJournalRecordResult(
            outcome="recorded",
            record_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            delivery_count=1,
            processing_status="received",
            reducer_key=f"booking:{event.appointment_id}",
            should_schedule_reducer=True,
        )

    def process_event(self, *, record_id: uuid.UUID, force: bool = False):
        self.processed_record_ids.append(record_id)

    def reprocess_event(self, *, record_id: uuid.UUID):
        raise AssertionError(f"unexpected replay call for {record_id}")


class _CaptureUnpaidInvoiceVoider:
    def __init__(self):
        self.bookings: list[dict[str, Any]] = []

    def void_unpaid_invoice(self, *, booking) -> None:
        self.bookings.append(
            {
                "booking_id": booking.booking_id,
                "creator_id": booking.creator_id,
                "booking_link_id": booking.booking_link_id,
                "tid": booking.tid,
                "provider_booking_id": booking.provider_booking_id,
                "canceled_at": booking.canceled_at,
            }
        )


def _engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def _booking_count() -> int:
    with Session(_engine()) as session:
        return len(session.scalars(select(Booking)).all())


def _journal_records_for_appointment_id(*, appointment_id: str) -> list[FullScopeWebhookEventRecord]:
    with Session(_engine()) as session:
        return session.scalars(
            select(FullScopeWebhookEventRecord).where(
                FullScopeWebhookEventRecord.appointment_id == appointment_id
            )
        ).all()


def _create_creator_booking_link_and_content(
    *,
    tid: str,
    provider: str = "fullscope",
) -> dict[str, Any]:
    with Session(_engine()) as session:
        creator = Creator(name=f"FS5 Creator {uuid.uuid4().hex}")
        session.add(creator)
        session.flush()

        if provider == "fullscope":
            booking_link = BookingLink(
                creator_id=creator.id,
                name="FS5 FullScope Source",
                provider="fullscope",
                destination_url="https://links.fullscope.tools/widget/bookings/fs5-personal-calendar",
            )
        else:
            booking_link = BookingLink(
                creator_id=creator.id,
                name="FS5 Calendly Source",
                provider="calendly",
                destination_url="https://calendly.com/example/fs5-source",
                calendly_url="https://calendly.com/example/fs5-source",
            )
        session.add(booking_link)
        session.flush()

        content = Content(
            creator_id=creator.id,
            booking_link_id=booking_link.id,
            source_url="https://example.com/posts/fs5-content",
            tid=tid,
        )
        session.add(content)
        session.commit()

        return {
            "creator_id": creator.id,
            "booking_link_id": booking_link.id,
            "tid": content.tid,
        }


def _bookings_for_provider_booking_id(*, provider_booking_id: str) -> list[Booking]:
    with Session(_engine()) as session:
        return session.scalars(
            select(Booking).where(Booking.provider_booking_id == provider_booking_id)
        ).all()


def _fullscope_payload(
    *,
    appointment_id: str,
    calendar_id: str,
    workflow_id: str,
    appointment_status: str,
    tid: str | None,
    email: str,
    calendar_date_created: str,
    last_updated_source: str = "booking_widget",
) -> bytes:
    payload: dict[str, Any] = {
        "email": email,
        "calendar": {
            "appointmentId": appointment_id,
            "id": calendar_id,
            "status": "booked",
            "appoinmentStatus": appointment_status,
            "date_created": calendar_date_created,
            "last_updated_by_meta": {"source": last_updated_source},
        },
        "workflow": {"id": workflow_id},
    }
    if tid is not None:
        payload["ccp_attribution_tid"] = tid
        payload["customData"] = {"attribution_tid": tid}
    return json.dumps(payload).encode("utf-8")


def _verified_event_from_payload(payload: bytes):
    return verify_and_parse_fullscope_webhook(
        payload=payload,
        authorization_header=_fullscope_authorization(
            _StubSettings.fullscope_webhook_shared_secret
        ),
        shared_secret=_StubSettings.fullscope_webhook_shared_secret,
    )


def test_fullscope_webhook_accepts_valid_authorization_routes_verified_event_and_does_not_persist_bookings():
    payload = _fullscope_payload(
        appointment_id="APT_fs5_valid",
        calendar_id="CAL_fs5_valid",
        workflow_id="WF_fs5_valid",
        appointment_status="confirmed",
        tid="fs5_valid_tid",
        email="fs5-valid@example.com",
        calendar_date_created="2026-03-18T16:00:00Z",
    )
    capture_router = _CaptureFullScopeWebhookRouter()

    assert _booking_count() == 0

    with TestClient(app) as client:
        with _override_app_state("settings", _StubSettings()):
            with _override_app_state("fullscope_webhook_router", capture_router):
                response = client.post(
                    "/webhooks/fullscope",
                    content=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": _fullscope_authorization(
                            _StubSettings.fullscope_webhook_shared_secret
                        ),
                    },
                )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert capture_router.events == [
        {
            "provider_event_type": "appointment.confirmed",
            "event_type": "booking.created",
            "appointment_id": "APT_fs5_valid",
            "appointment_id_path": "calendar.appointmentId",
            "calendar_id": "CAL_fs5_valid",
            "calendar_id_path": "calendar.id",
            "workflow_id": "WF_fs5_valid",
            "workflow_id_path": "workflow.id",
            "tid": "fs5_valid_tid",
            "tid_path": "ccp_attribution_tid",
        }
    ]
    assert capture_router.processed_record_ids == [
        uuid.UUID("00000000-0000-0000-0000-000000000001")
    ]
    assert _booking_count() == 0


def test_fullscope_router_record_event_persists_only_until_worker_processes():
    stored = _create_creator_booking_link_and_content(tid="fs5_persist_only_tid")
    payload = _fullscope_payload(
        appointment_id="APT_fs5_persist_only",
        calendar_id="CAL_fs5_persist_only",
        workflow_id="WF_fs5_persist_only",
        appointment_status="confirmed",
        tid=stored["tid"],
        email="fs5-persist-only@example.com",
        calendar_date_created="2026-03-18T17:00:00Z",
    )
    router = DefaultFullScopeWebhookRouter()
    event = _verified_event_from_payload(payload)

    journal_result = router.record_event(event=event)
    journal_records = _journal_records_for_appointment_id(
        appointment_id="APT_fs5_persist_only"
    )

    assert journal_result.outcome == "recorded"
    assert journal_result.processing_status == "received"
    assert journal_result.reducer_key == "booking:APT_fs5_persist_only"
    assert journal_result.should_schedule_reducer is True
    assert len(journal_records) == 1
    assert journal_records[0].processing_status == "received"
    assert journal_records[0].reducer_attempt_count == 0
    assert _bookings_for_provider_booking_id(provider_booking_id="APT_fs5_persist_only") == []

    processing_status = router.process_event(record_id=journal_result.record_id)
    bookings = _bookings_for_provider_booking_id(provider_booking_id="APT_fs5_persist_only")
    journal_records = _journal_records_for_appointment_id(
        appointment_id="APT_fs5_persist_only"
    )

    assert processing_status == "applied"
    assert len(bookings) == 1
    assert bookings[0].provider == "fullscope"
    assert bookings[0].status == "created"
    assert bookings[0].tid == stored["tid"]
    assert len(journal_records) == 1
    assert journal_records[0].processing_status == "applied"
    assert journal_records[0].reducer_attempt_count == 1
    assert journal_records[0].processed_at is not None


def test_fullscope_webhook_duplicate_delivery_is_idempotent_by_payload_hash():
    stored = _create_creator_booking_link_and_content(tid="fs5_duplicate_tid")
    payload = _fullscope_payload(
        appointment_id="APT_fs5_duplicate",
        calendar_id="CAL_fs5_duplicate",
        workflow_id="WF_fs5_duplicate",
        appointment_status="confirmed",
        tid=stored["tid"],
        email="fs5-duplicate@example.com",
        calendar_date_created="2026-03-18T18:00:00Z",
    )

    with TestClient(app) as client:
        with _override_app_state("settings", _StubSettings()):
            first_response = client.post(
                "/webhooks/fullscope",
                content=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": _fullscope_authorization(
                        _StubSettings.fullscope_webhook_shared_secret
                    ),
                },
            )
            second_response = client.post(
                "/webhooks/fullscope",
                content=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": _fullscope_authorization(
                        _StubSettings.fullscope_webhook_shared_secret
                    ),
                },
            )

    bookings = _bookings_for_provider_booking_id(provider_booking_id="APT_fs5_duplicate")
    journal_records = _journal_records_for_appointment_id(appointment_id="APT_fs5_duplicate")

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert len(bookings) == 1
    assert len(journal_records) == 1
    assert journal_records[0].delivery_count == 2
    assert journal_records[0].processing_status == "applied"


def test_fullscope_webhook_confirmed_updates_existing_booking_lineage_without_duplication():
    stored = _create_creator_booking_link_and_content(tid="fs5_reschedule_tid")
    created_payload = _fullscope_payload(
        appointment_id="APT_fs5_reschedule",
        calendar_id="CAL_fs5_reschedule",
        workflow_id="WF_fs5_reschedule",
        appointment_status="confirmed",
        tid=stored["tid"],
        email="fs5-original@example.com",
        calendar_date_created="2026-03-18T19:00:00Z",
    )
    rescheduled_payload = _fullscope_payload(
        appointment_id="APT_fs5_reschedule",
        calendar_id="CAL_fs5_reschedule",
        workflow_id="WF_fs5_reschedule",
        appointment_status="confirmed",
        tid=stored["tid"],
        email="fs5-rescheduled@example.com",
        calendar_date_created="2026-03-18T19:00:00Z",
        last_updated_source="appointments_page",
    )

    with TestClient(app) as client:
        with _override_app_state("settings", _StubSettings()):
            created_response = client.post(
                "/webhooks/fullscope",
                content=created_payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": _fullscope_authorization(
                        _StubSettings.fullscope_webhook_shared_secret
                    ),
                },
            )
            rescheduled_response = client.post(
                "/webhooks/fullscope",
                content=rescheduled_payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": _fullscope_authorization(
                        _StubSettings.fullscope_webhook_shared_secret
                    ),
                },
            )

    bookings = _bookings_for_provider_booking_id(provider_booking_id="APT_fs5_reschedule")
    journal_records = _journal_records_for_appointment_id(appointment_id="APT_fs5_reschedule")

    assert created_response.status_code == 200
    assert rescheduled_response.status_code == 200
    assert len(bookings) == 1
    assert bookings[0].email == "fs5-rescheduled@example.com"
    assert bookings[0].status == "created"
    assert bookings[0].canceled_at is None
    assert len(journal_records) == 2
    assert {record.processing_status for record in journal_records} == {"applied"}


def test_fullscope_webhook_canceled_before_created_is_deferred_and_replay_applies_it():
    stored = _create_creator_booking_link_and_content(tid="fs5_replay_tid")
    canceled_payload = _fullscope_payload(
        appointment_id="APT_fs5_replay",
        calendar_id="CAL_fs5_replay",
        workflow_id="WF_fs5_replay",
        appointment_status="cancelled",
        tid=stored["tid"],
        email="fs5-replay@example.com",
        calendar_date_created="2026-03-18T20:00:00Z",
        last_updated_source="appointment_page",
    )
    created_payload = _fullscope_payload(
        appointment_id="APT_fs5_replay",
        calendar_id="CAL_fs5_replay",
        workflow_id="WF_fs5_replay",
        appointment_status="confirmed",
        tid=stored["tid"],
        email="fs5-replay@example.com",
        calendar_date_created="2026-03-18T20:00:00Z",
    )
    capture_voider = _CaptureUnpaidInvoiceVoider()
    router = DefaultFullScopeWebhookRouter(unpaid_invoice_voider=capture_voider)

    with TestClient(app) as client:
        with _override_app_state("settings", _StubSettings()):
            with _override_app_state("fullscope_webhook_router", router):
                canceled_response = client.post(
                    "/webhooks/fullscope",
                    content=canceled_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": _fullscope_authorization(
                            _StubSettings.fullscope_webhook_shared_secret
                        ),
                    },
                )
                deferred_records = _journal_records_for_appointment_id(
                    appointment_id="APT_fs5_replay"
                )
                created_response = client.post(
                    "/webhooks/fullscope",
                    content=created_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": _fullscope_authorization(
                            _StubSettings.fullscope_webhook_shared_secret
                        ),
                    },
                )
                replay_result = router.reprocess_event(record_id=deferred_records[0].id)

    bookings = _bookings_for_provider_booking_id(provider_booking_id="APT_fs5_replay")
    journal_records = _journal_records_for_appointment_id(appointment_id="APT_fs5_replay")

    assert canceled_response.status_code == 200
    assert created_response.status_code == 200
    assert replay_result.outcome == "reprocessed"
    assert replay_result.processing_status == "applied"
    assert len(bookings) == 1
    assert bookings[0].status == "canceled"
    assert bookings[0].canceled_at is not None
    assert len(journal_records) == 2
    assert capture_voider.bookings == [
        {
            "booking_id": bookings[0].id,
            "creator_id": stored["creator_id"],
            "booking_link_id": stored["booking_link_id"],
            "tid": stored["tid"],
            "provider_booking_id": "APT_fs5_replay",
            "canceled_at": bookings[0].canceled_at,
        }
    ]


def test_fullscope_webhook_confirmed_for_non_fullscope_content_is_ignored():
    _create_creator_booking_link_and_content(
        tid="fs5_unsupported_source_tid",
        provider="calendly",
    )
    payload = _fullscope_payload(
        appointment_id="APT_fs5_unsupported_source",
        calendar_id="CAL_fs5_unsupported_source",
        workflow_id="WF_fs5_unsupported_source",
        appointment_status="confirmed",
        tid="fs5_unsupported_source_tid",
        email="fs5-unsupported@example.com",
        calendar_date_created="2026-03-18T21:00:00Z",
    )

    with TestClient(app) as client:
        with _override_app_state("settings", _StubSettings()):
            response = client.post(
                "/webhooks/fullscope",
                content=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": _fullscope_authorization(
                        _StubSettings.fullscope_webhook_shared_secret
                    ),
                },
            )

    bookings = _bookings_for_provider_booking_id(
        provider_booking_id="APT_fs5_unsupported_source"
    )
    journal_records = _journal_records_for_appointment_id(
        appointment_id="APT_fs5_unsupported_source"
    )

    assert response.status_code == 200
    assert bookings == []
    assert len(journal_records) == 1
    assert journal_records[0].processing_status == "ignored_unsupported_source"


def test_fullscope_webhook_rejects_invalid_authorization_without_routing_or_persisting_bookings():
    payload = _fullscope_payload(
        appointment_id="APT_fs5_invalid_auth",
        calendar_id="CAL_fs5_invalid_auth",
        workflow_id="WF_fs5_invalid_auth",
        appointment_status="confirmed",
        tid="fs5_invalid_auth_tid",
        email="fs5-invalid-auth@example.com",
        calendar_date_created="2026-03-18T22:00:00Z",
    )
    capture_router = _CaptureFullScopeWebhookRouter()

    assert _booking_count() == 0

    with TestClient(app) as client:
        with _override_app_state("settings", _StubSettings()):
            with _override_app_state("fullscope_webhook_router", capture_router):
                response = client.post(
                    "/webhooks/fullscope",
                    content=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": "Bearer not-the-right-secret",
                    },
                )

    assert response.status_code == 400
    assert response.json() == {"detail": "invalid fullscope webhook authorization"}
    assert capture_router.events == []
    assert _booking_count() == 0
