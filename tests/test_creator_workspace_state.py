from app.schemas.booking_link import BookingLinkResponse
from app.schemas.content import ContentResponse
from app.services.creator_workspace_state import (
    build_creator_workspace_readiness,
    build_creator_workspace_state,
)


def _booking_link(
    *,
    id: str = "booking-link-1",
    billing_amount_cents: int | None = None,
    billing_currency: str | None = None,
) -> BookingLinkResponse:
    return BookingLinkResponse(
        id=id,
        name="Strategy Call",
        calendly_url="https://calendly.com/example/strategy-call",
        billing_amount_cents=billing_amount_cents,
        billing_currency=billing_currency,
    )


def _content(*, booking_link_id: str = "booking-link-1") -> ContentResponse:
    return ContentResponse(
        id="content-1",
        booking_link_id=booking_link_id,
        source_url="https://example.com/posts/test",
        tid="tid_test_123",
        tracked_url="https://example.com/t/tid_test_123",
    )


def test_build_creator_workspace_readiness_connected_but_not_billable_now():
    readiness = build_creator_workspace_readiness(
        raw_stripe_status=" Connected ",
        booking_links=[_booking_link()],
        content_items=[],
        paid_invoice_count=0,
    )

    assert readiness.stripe_status == "connected"
    assert readiness.connected is True
    assert readiness.billable_now is False
    assert readiness.ready_to_track is False
    assert readiness.waiting_for_first_paid_result is False
    assert readiness.booking_links_count == 1
    assert readiness.billing_ready_count == 0
    assert readiness.tracked_content_count == 0
    assert readiness.paid_invoice_count == 0


def test_build_creator_workspace_readiness_ready_to_track_waiting_for_first_paid_result():
    readiness = build_creator_workspace_readiness(
        raw_stripe_status="connected",
        booking_links=[_booking_link(billing_amount_cents=15000, billing_currency="USD")],
        content_items=[_content()],
        paid_invoice_count=0,
    )

    assert readiness.connected is True
    assert readiness.billable_now is True
    assert readiness.ready_to_track is True
    assert readiness.waiting_for_first_paid_result is True
    assert readiness.booking_links_count == 1
    assert readiness.billing_ready_count == 1
    assert readiness.tracked_content_count == 1
    assert readiness.paid_invoice_count == 0


def test_build_creator_workspace_readiness_paid_results_clear_waiting_state():
    readiness = build_creator_workspace_readiness(
        raw_stripe_status="connected",
        booking_links=[_booking_link(billing_amount_cents=15000, billing_currency="USD")],
        content_items=[_content()],
        paid_invoice_count=2,
    )

    assert readiness.billable_now is True
    assert readiness.ready_to_track is True
    assert readiness.waiting_for_first_paid_result is False
    assert readiness.paid_invoice_count == 2


def test_build_creator_workspace_state_sums_attention_backlog_counts():
    workspace_state = build_creator_workspace_state(
        raw_stripe_status="pending",
        booking_links=[],
        content_items=[],
        paid_invoice_count=0,
        blocked_billing_count=2,
        unmatched_payment_count=3,
    )

    assert workspace_state.readiness.connected is False
    assert workspace_state.readiness.billable_now is False
    assert workspace_state.readiness.ready_to_track is False
    assert workspace_state.readiness.waiting_for_first_paid_result is False
    assert workspace_state.blocked_billing_count == 2
    assert workspace_state.unmatched_payment_count == 3
    assert workspace_state.attention_count == 5
