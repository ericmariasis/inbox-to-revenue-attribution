from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.services.reporting import (
    REPORTS_FUNNEL_STATUS_PAID,
    get_creator_paid_attribution_explanation,
    get_creator_reports_booking_link_summary,
    get_creator_reports_content_drilldown,
    get_creator_reports_summary,
    get_creator_reports_topic_summary,
)
from tests.reporting_golden_fixture import (
    ReportingGoldenFixture,
    reporting_test_engine,
    seed_reporting_contract_fixture,
)

HTML_ACCEPT_HEADERS = {"Accept": "text/html,application/xhtml+xml"}
SESSION_COOKIE_NAME = "ccp_creator_session"


def _seed_fixture() -> tuple[object, ReportingGoldenFixture]:
    engine = reporting_test_engine()
    fixture = seed_reporting_contract_fixture(engine=engine)
    return engine, fixture


def test_reporting_contract_golden_service_lane():
    engine, fixture = _seed_fixture()

    with Session(engine) as session:
        full_summary = get_creator_reports_summary(
            creator_id=fixture.creator_id,
            db=session,
        )
        filtered_summary = get_creator_reports_summary(
            creator_id=fixture.creator_id,
            db=session,
            start_date=fixture.filter_start_date,
            end_date=fixture.filter_end_date,
        )
        drilldown = get_creator_reports_content_drilldown(
            creator_id=fixture.creator_id,
            tid=fixture.primary_tid,
            db=session,
            start_date=fixture.filter_start_date,
            end_date=fixture.filter_end_date,
        )
        full_topic_summary = get_creator_reports_topic_summary(
            creator_id=fixture.creator_id,
            db=session,
        )
        filtered_topic_summary = get_creator_reports_topic_summary(
            creator_id=fixture.creator_id,
            db=session,
            start_date=fixture.filter_start_date,
            end_date=fixture.filter_end_date,
        )
        full_booking_link_summary = get_creator_reports_booking_link_summary(
            creator_id=fixture.creator_id,
            db=session,
        )
        filtered_booking_link_summary = get_creator_reports_booking_link_summary(
            creator_id=fixture.creator_id,
            db=session,
            start_date=fixture.filter_start_date,
            end_date=fixture.filter_end_date,
        )
        paid_explanation = get_creator_paid_attribution_explanation(
            creator_id=fixture.creator_id,
            tid=fixture.primary_tid,
            db=session,
            start_date=fixture.filter_start_date,
            end_date=fixture.filter_end_date,
        )

    full_rows_by_tid = {row.tid: row for row in full_summary.rows}
    filtered_rows_by_tid = {row.tid: row for row in filtered_summary.rows}

    assert set(full_rows_by_tid) == {fixture.primary_tid, fixture.historical_tid}
    assert full_summary.paid_revenue_cents == 24500
    assert full_summary.unattributed_current_backlog.event_count == 2

    primary_full_row = full_rows_by_tid[fixture.primary_tid]
    assert primary_full_row.booking_count == 3
    assert primary_full_row.paid_revenue_cents == 19500
    assert primary_full_row.open_blocked_billing_case_count == 1
    assert primary_full_row.funnel_status == REPORTS_FUNNEL_STATUS_PAID

    assert set(filtered_rows_by_tid) == {fixture.primary_tid}
    assert filtered_summary.paid_revenue_cents == 19500
    assert filtered_rows_by_tid[fixture.primary_tid].booking_count == 3
    assert filtered_rows_by_tid[fixture.primary_tid].paid_booking_count == 1

    assert drilldown is not None
    assert drilldown.current_summary_row.booking_count == 3
    assert drilldown.paid_window.paid_revenue_cents == 19500
    assert len(drilldown.bookings) == 3
    assert len(drilldown.blocked_cases) == 1
    assert len(drilldown.unmatched_payment_events) == 1
    assert drilldown.bookings[0].provider_booking_id == fixture.blocked_booking_uuid

    assert full_topic_summary.has_any_authoritative_topics is True
    assert {row.canonical_label for row in full_topic_summary.rows} == {
        "Discovery Calls",
        "Pricing Strategy",
        "Retention Reviews",
    }
    filtered_topic_labels = {row.canonical_label for row in filtered_topic_summary.rows}
    assert filtered_topic_labels == {"Discovery Calls", "Pricing Strategy"}
    assert sum(row.paid_revenue_cents for row in filtered_topic_summary.rows) == 39000
    assert sum(row.paid_revenue_cents for row in filtered_topic_summary.rows) > filtered_summary.paid_revenue_cents

    assert len(full_booking_link_summary.rows) == 2
    historical_row = next(
        row for row in full_booking_link_summary.rows if row.booking_link_name == fixture.historical_booking_link_name
    )
    assert historical_row.paid_revenue_cents == 5000
    assert historical_row.booking_link_billing_amount_cents is None
    assert historical_row.booking_link_billing_currency is None

    assert len(filtered_booking_link_summary.rows) == 1
    assert filtered_booking_link_summary.rows[0].booking_link_name == fixture.active_booking_link_name
    assert filtered_booking_link_summary.rows[0].paid_revenue_cents == 19500

    assert paid_explanation is not None
    assert paid_explanation.summary_row.tid == fixture.primary_tid
    assert len(paid_explanation.evidence) == 1
    assert paid_explanation.evidence[0].booking_uuid == fixture.paid_booking_uuid
    assert paid_explanation.evidence[0].stripe_invoice_id == fixture.provider_invoice_id
    assert paid_explanation.evidence[0].stripe_event_id == fixture.provider_event_id


def test_reporting_contract_golden_browser_lane():
    _, fixture = _seed_fixture()

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, fixture.access_token)

        reports_unfiltered = client.get("/app/reports", headers=HTML_ACCEPT_HEADERS)
        reports_filtered = client.get(
            "/app/reports",
            params={
                "start_date": fixture.filter_start_date.isoformat(),
                "end_date": fixture.filter_end_date.isoformat(),
            },
            headers=HTML_ACCEPT_HEADERS,
        )
        drilldown = client.get(
            f"/app/reports/content/{fixture.primary_tid}",
            params={
                "start_date": fixture.filter_start_date.isoformat(),
                "end_date": fixture.filter_end_date.isoformat(),
            },
            headers=HTML_ACCEPT_HEADERS,
        )
        topics_filtered = client.get(
            "/app/reports/topics",
            params={
                "start_date": fixture.filter_start_date.isoformat(),
                "end_date": fixture.filter_end_date.isoformat(),
            },
            headers=HTML_ACCEPT_HEADERS,
        )
        booking_links_unfiltered = client.get(
            "/app/reports/booking-links",
            headers=HTML_ACCEPT_HEADERS,
        )
        booking_links_filtered = client.get(
            "/app/reports/booking-links",
            params={
                "start_date": fixture.filter_start_date.isoformat(),
                "end_date": fixture.filter_end_date.isoformat(),
            },
            headers=HTML_ACCEPT_HEADERS,
        )
        paid_explanation = client.get(
            f"/app/reports/explanations/paid/{fixture.primary_tid}",
            params={
                "start_date": fixture.filter_start_date.isoformat(),
                "end_date": fixture.filter_end_date.isoformat(),
            },
            headers=HTML_ACCEPT_HEADERS,
        )
        unattributed_explanation = client.get(
            "/app/reports/explanations/unattributed",
            params={
                "start_date": fixture.filter_start_date.isoformat(),
                "end_date": fixture.filter_end_date.isoformat(),
            },
            headers=HTML_ACCEPT_HEADERS,
        )

    assert reports_unfiltered.status_code == 200
    assert "Content funnel summary" in reports_unfiltered.text
    assert fixture.primary_source_url in reports_unfiltered.text
    assert fixture.historical_source_url in reports_unfiltered.text

    assert reports_filtered.status_code == 200
    assert "Showing 1 of 2 tracked content rows in this paid view." in reports_filtered.text
    assert fixture.primary_source_url in reports_filtered.text
    assert fixture.historical_source_url not in reports_filtered.text
    assert "Missing tracking ID" in reports_filtered.text
    assert "Unknown invoice" in reports_filtered.text

    assert drilldown.status_code == 200
    assert "Content funnel drilldown" in drilldown.text
    assert fixture.primary_tid in drilldown.text
    assert fixture.paid_booking_uuid in drilldown.text
    assert fixture.waiting_booking_uuid in drilldown.text
    assert fixture.blocked_booking_uuid in drilldown.text
    assert "Unknown invoice" in drilldown.text
    assert "Missing tracking ID" not in drilldown.text

    assert topics_filtered.status_code == 200
    assert "Topic analytics" in topics_filtered.text
    assert "Pricing Strategy" in topics_filtered.text
    assert "Discovery Calls" in topics_filtered.text
    assert "Retention Reviews" not in topics_filtered.text
    assert "2 topic rows visible" in topics_filtered.text

    assert booking_links_unfiltered.status_code == 200
    assert "Booking-link analytics" in booking_links_unfiltered.text
    assert fixture.active_booking_link_name in booking_links_unfiltered.text
    assert fixture.historical_booking_link_name in booking_links_unfiltered.text
    assert "No billing defaults yet" in booking_links_unfiltered.text

    assert booking_links_filtered.status_code == 200
    assert "1 booking link row visible" in booking_links_filtered.text
    assert fixture.active_booking_link_name in booking_links_filtered.text
    assert fixture.historical_booking_link_name not in booking_links_filtered.text

    assert paid_explanation.status_code == 200
    assert "Why this revenue counted" in paid_explanation.text
    assert fixture.primary_source_url in paid_explanation.text
    assert fixture.paid_booking_uuid in paid_explanation.text
    assert fixture.provider_invoice_id in paid_explanation.text
    assert fixture.provider_event_id in paid_explanation.text

    assert unattributed_explanation.status_code == 200
    assert "Why some payments are not counted yet" in unattributed_explanation.text
    assert "Missing tracking ID" in unattributed_explanation.text
    assert "Unknown invoice" in unattributed_explanation.text
