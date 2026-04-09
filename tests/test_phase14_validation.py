import os
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.main import app
from app.models.auth_user import AuthUser
from app.models.booking import Booking
from app.models.booking_link import BookingLink
from app.models.content import Content
from app.models.content_confirmed_topic import ContentConfirmedTopic
from app.models.content_extraction_artifact import ContentExtractionArtifact
from app.models.content_fetch_snapshot import ContentFetchSnapshot
from app.models.content_topic_candidate import ContentTopicCandidate
from app.models.creator import Creator
from app.models.invoice import Invoice
from app.models.invoice_payment_event import InvoicePaymentEvent
from app.services.browser_session import SESSION_COOKIE_NAME
from app.services.invoice_payment_events import UNATTRIBUTED_REASON_UNKNOWN_BOOKING_UUID

HTML_ACCEPT_HEADERS = {"Accept": "text/html,application/xhtml+xml"}


def _engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def _access_token(*, user_id: uuid.UUID, creator_id: uuid.UUID, email: str) -> str:
    settings = get_settings()
    issued_at = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "creator_id": str(creator_id),
        "email": email,
        "iat": issued_at,
        "exp": issued_at + timedelta(hours=24),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def _create_browser_creator_fixture(
    session: Session,
    *,
    suffix: str,
) -> tuple[Creator, BookingLink, str]:
    creator = Creator(
        name=f"Phase 14 Creator {suffix}",
        stripe_connect_status="connected",
        stripe_account_id=f"acct_phase14_{suffix}",
    )
    session.add(creator)
    session.flush()

    user = AuthUser(
        creator_id=creator.id,
        email=f"phase14_{suffix}@example.com",
    )
    session.add(user)
    session.flush()

    booking_link = BookingLink(
        creator_id=creator.id,
        name=f"Phase 14 Link {suffix}",
        calendly_url=f"https://calendly.com/example/phase14-{suffix}",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    session.add(booking_link)
    session.flush()

    return creator, booking_link, _access_token(
        user_id=user.id,
        creator_id=creator.id,
        email=user.email,
    )


def _create_content(
    session: Session,
    *,
    creator: Creator,
    booking_link: BookingLink,
    tid: str,
    source_url: str,
) -> Content:
    content = Content(
        creator_id=creator.id,
        booking_link_id=booking_link.id,
        source_url=source_url,
        tid=tid,
    )
    session.add(content)
    session.flush()
    return content


def _create_authoritative_artifact(
    session: Session,
    *,
    content: Content,
    topic_labels: list[str],
    fetched_at: datetime,
    title: str,
) -> ContentExtractionArtifact:
    fetch_snapshot = ContentFetchSnapshot(
        content_id=content.id,
        creator_id=content.creator_id,
        requested_url=content.source_url,
        fetched_url=content.source_url,
        fetch_status="succeeded",
        http_status=200,
        failure_reason_code=None,
        failure_detail=None,
        response_content_type="text/html",
        response_content_charset="utf-8",
        snapshot_text=f"<html><body><article><p>{title}</p></article></body></html>",
        fetched_at=fetched_at,
    )
    session.add(fetch_snapshot)
    session.flush()

    artifact = ContentExtractionArtifact(
        content_id=content.id,
        creator_id=content.creator_id,
        fetch_snapshot_id=fetch_snapshot.id,
        extraction_status="succeeded",
        extraction_reason_code=None,
        extraction_detail=None,
        extraction_method="html_article",
        title=title,
        published_at=None,
        published_at_raw=None,
        source_text_char_count=32,
        extracted_text_char_count=32,
        extracted_text_word_count=5,
        extracted_text=f"Artifact text for {title}",
        created_at=fetched_at + timedelta(minutes=5),
    )
    session.add(artifact)
    session.flush()

    for index, topic_label in enumerate(topic_labels, start=1):
        topic = ContentConfirmedTopic(
            content_id=content.id,
            creator_id=content.creator_id,
            canonical_label=topic_label,
            normalized_label=topic_label.casefold(),
        )
        session.add(topic)
        session.flush()
        session.add(
            ContentTopicCandidate(
                content_id=content.id,
                creator_id=content.creator_id,
                extraction_artifact_id=artifact.id,
                confirmed_topic_id=topic.id,
                suggested_label=topic_label,
                normalized_label=topic_label.casefold(),
                suggestion_method="text_keywords",
                candidate_rank=index,
                review_status="confirmed",
                reviewed_at=fetched_at + timedelta(minutes=6),
            )
        )

    content.authoritative_extraction_artifact_id = artifact.id
    session.flush()
    return artifact


def _create_paid_booking(
    session: Session,
    *,
    creator: Creator,
    booking_link: BookingLink,
    content: Content,
    booking_uuid: str,
    paid_at: datetime,
    amount_cents: int,
    stripe_invoice_id: str,
    stripe_event_id: str,
) -> None:
    booking = Booking(
        creator_id=creator.id,
        booking_link_id=booking_link.id,
        tid=content.tid,
        calendly_booking_uuid=booking_uuid,
        email=f"{booking_uuid.lower()}@example.com",
        status="created",
        attribution_status="attributed",
        unattributed_reason=None,
        booked_at=paid_at - timedelta(hours=2),
    )
    session.add(booking)
    session.flush()

    invoice = Invoice(
        creator_id=creator.id,
        booking_id=booking.id,
        tid=content.tid,
        stripe_account_id=creator.stripe_account_id,
        stripe_invoice_id=stripe_invoice_id,
        amount_cents=amount_cents,
        currency="USD",
        status="paid",
        issued_at=paid_at - timedelta(hours=1),
        paid_at=paid_at,
    )
    session.add(invoice)
    session.flush()

    session.add(
        InvoicePaymentEvent(
            stripe_event_id=stripe_event_id,
            stripe_event_type="invoice.paid",
            stripe_account_id=creator.stripe_account_id,
            stripe_invoice_id=stripe_invoice_id,
            invoice_id=invoice.id,
            creator_id=creator.id,
            booking_id=booking.id,
            tid=content.tid,
            status="applied",
            unattributed_reason=None,
            paid_at=paid_at,
            received_at=paid_at,
            processed_at=paid_at,
        )
    )
    session.flush()


def test_phase14_launch_surfaces_cover_warm_creator_and_operator_paths():
    engine = _engine()
    suffix = uuid.uuid4().hex[:8]
    paid_at = datetime(2026, 3, 13, 14, 30, tzinfo=timezone.utc)
    source_url = "https://example.com/posts/phase14-launch"
    content_tid = f"phase14{suffix}"
    unmatched_event_id = f"evt_phase14_unmatched_{suffix}"
    unmatched_invoice_id = f"in_phase14_unmatched_{suffix}"

    with Session(engine) as session:
        creator, booking_link, access_token = _create_browser_creator_fixture(
            session,
            suffix=suffix,
        )
        creator_id = creator.id
        content = _create_content(
            session,
            creator=creator,
            booking_link=booking_link,
            tid=content_tid,
            source_url=source_url,
        )
        _create_authoritative_artifact(
            session,
            content=content,
            topic_labels=["Retention Reviews"],
            fetched_at=datetime(2026, 3, 13, 10, 0, tzinfo=timezone.utc),
            title="Phase 14 Launch Artifact",
        )
        _create_paid_booking(
            session,
            creator=creator,
            booking_link=booking_link,
            content=content,
            booking_uuid=f"BOOK_PHASE14_READY_{suffix}",
            paid_at=paid_at,
            amount_cents=19500,
            stripe_invoice_id=f"in_phase14_ready_{suffix}",
            stripe_event_id=f"evt_phase14_ready_{suffix}",
        )
        session.add(
            InvoicePaymentEvent(
                stripe_event_id=unmatched_event_id,
                stripe_event_type="invoice.paid",
                stripe_account_id=creator.stripe_account_id,
                stripe_invoice_id=unmatched_invoice_id,
                invoice_id=None,
                creator_id=creator.id,
                booking_id=None,
                tid=None,
                status="unmatched",
                unattributed_reason=UNATTRIBUTED_REASON_UNKNOWN_BOOKING_UUID,
                paid_at=paid_at + timedelta(minutes=5),
                received_at=paid_at + timedelta(minutes=5),
                processed_at=None,
            )
        )
        session.commit()

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, access_token)

        process_health_response = client.get("/health")
        home_response = client.get("/app", headers=HTML_ACCEPT_HEADERS)
        content_response = client.get("/app/content", headers=HTML_ACCEPT_HEADERS)
        reports_response = client.get(
            "/app/reports",
            params={"start_date": "2026-03-13", "end_date": "2026-03-13"},
            headers=HTML_ACCEPT_HEADERS,
        )
        experiments_generate_response = client.post(
            "/app/experiments",
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )
        experiments_page_response = client.get(
            experiments_generate_response.headers["location"],
            headers=HTML_ACCEPT_HEADERS,
        )
        run_claim_snapshot_id = uuid.UUID(
            parse_qs(urlparse(experiments_generate_response.headers["location"]).query)[
                "claim_snapshot_id"
            ][0]
        )
        evidence_response = client.get(
            f"/app/experiments/{run_claim_snapshot_id}/cards/1",
            headers=HTML_ACCEPT_HEADERS,
        )
        attention_response = client.get("/app/attention", headers=HTML_ACCEPT_HEADERS)
        health_page_response = client.get("/app/health", headers=HTML_ACCEPT_HEADERS)
        reports_health_response = client.get(
            "/reports/health",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    assert process_health_response.status_code == 200
    assert process_health_response.json() == {"status": "ok"}

    assert home_response.status_code == 200
    assert "Setup Home" in home_response.text
    assert "4 of 4 setup steps done" in home_response.text
    assert "Review attention items" in home_response.text
    assert "Review 1 attention item" in home_response.text
    assert 'href="/app/attention"' in home_response.text

    assert content_response.status_code == 200
    assert source_url in content_response.text
    assert content_tid in content_response.text

    assert reports_response.status_code == 200
    assert "Reports" in reports_response.text
    assert "195.00" in reports_response.text
    assert "1 paid invoice" in reports_response.text
    assert "1 paid booking" in reports_response.text
    assert source_url in reports_response.text
    assert content_tid in reports_response.text
    assert "Why some payments stay outside totals" in reports_response.text
    assert "Unknown booking" in reports_response.text
    assert 'href="/app/attention"' in reports_response.text

    assert experiments_generate_response.status_code == 303
    assert experiments_generate_response.headers["location"].startswith(
        "/app/experiments?status=generated&claim_snapshot_id="
    )
    assert experiments_page_response.status_code == 200
    assert "Fresh snapshot ready" in experiments_page_response.text
    assert "Here is the next content experiment most grounded" in experiments_page_response.text
    assert content_tid in experiments_page_response.text

    assert evidence_response.status_code == 200
    assert "Experiment evidence" in evidence_response.text
    assert "Authoritative content used" in evidence_response.text
    assert "Settled paid results used" in evidence_response.text
    assert "Phase 14 Launch Artifact" in evidence_response.text
    assert "Retention Reviews" in evidence_response.text

    assert attention_response.status_code == 200
    assert "Attention" in attention_response.text
    assert unmatched_event_id in attention_response.text
    assert unmatched_invoice_id in attention_response.text
    assert "Unknown booking" in attention_response.text

    assert health_page_response.status_code == 200
    assert "Health" in health_page_response.text
    assert "1 backlog event" in health_page_response.text
    assert "1 settled row currently marked matched." in health_page_response.text
    assert "1 backlog event due to unknown booking." in health_page_response.text
    assert "No blocked billing cases are waiting right now" in health_page_response.text

    assert reports_health_response.status_code == 200
    assert reports_health_response.json()["creator_id"] == str(creator_id)
    assert reports_health_response.json()["booking_attribution"]["unattributed_booking_count"] == 0
    assert reports_health_response.json()["calendly_ingress"]["backlog_event_count"] == 0
    assert reports_health_response.json()["calendly_ingress"]["failed_event_count"] == 0
    assert reports_health_response.json()["fullscope_ingress"]["backlog_event_count"] == 0
    assert reports_health_response.json()["fullscope_ingress"]["failed_event_count"] == 0
    assert reports_health_response.json()["payment_provenance"]["current_backlog_event_count"] == 1
    assert reports_health_response.json()["payment_provenance"]["current_backlog_reasons"] == [
        {"reason": "UNKNOWN_BOOKING_UUID", "event_count": 1},
    ]
    assert reports_health_response.json()["blocked_billing"] == {
        "open_case_count": 0,
        "reasons": [],
    }
