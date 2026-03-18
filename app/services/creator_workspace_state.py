from dataclasses import dataclass

from app.models.booking_provider import booking_provider_supports_tracked_content
from app.schemas.booking_link import BookingLinkResponse
from app.schemas.content import ContentResponse


@dataclass(frozen=True)
class CreatorWorkspaceReadiness:
    stripe_status: str
    connected: bool
    billable_now: bool
    ready_to_track: bool
    waiting_for_first_paid_result: bool
    booking_links_count: int
    trackable_booking_links_count: int
    limited_tracking_booking_links_count: int
    billing_ready_count: int
    tracked_content_count: int
    paid_invoice_count: int


@dataclass(frozen=True)
class CreatorWorkspaceState:
    readiness: CreatorWorkspaceReadiness
    blocked_billing_count: int
    unmatched_payment_count: int
    attention_count: int


def build_creator_workspace_readiness(
    *,
    raw_stripe_status: str,
    booking_links: list[BookingLinkResponse],
    content_items: list[ContentResponse],
    paid_invoice_count: int,
) -> CreatorWorkspaceReadiness:
    normalized_stripe_status = raw_stripe_status.strip().lower()
    booking_links_count = len(booking_links)
    trackable_booking_links_count = sum(
        1
        for booking_link in booking_links
        if booking_provider_supports_tracked_content(booking_link.provider)
    )
    limited_tracking_booking_links_count = booking_links_count - trackable_booking_links_count
    billing_ready_count = sum(
        1
        for booking_link in booking_links
        if booking_provider_supports_tracked_content(booking_link.provider)
        if booking_link.billing_amount_cents is not None
        and booking_link.billing_currency is not None
    )
    tracked_content_count = len(content_items)
    connected = normalized_stripe_status == "connected"
    billable_now = connected and billing_ready_count > 0
    ready_to_track = billable_now and tracked_content_count > 0
    waiting_for_first_paid_result = ready_to_track and paid_invoice_count == 0

    return CreatorWorkspaceReadiness(
        stripe_status=normalized_stripe_status,
        connected=connected,
        billable_now=billable_now,
        ready_to_track=ready_to_track,
        waiting_for_first_paid_result=waiting_for_first_paid_result,
        booking_links_count=booking_links_count,
        trackable_booking_links_count=trackable_booking_links_count,
        limited_tracking_booking_links_count=limited_tracking_booking_links_count,
        billing_ready_count=billing_ready_count,
        tracked_content_count=tracked_content_count,
        paid_invoice_count=paid_invoice_count,
    )


def build_creator_workspace_state(
    *,
    raw_stripe_status: str,
    booking_links: list[BookingLinkResponse],
    content_items: list[ContentResponse],
    paid_invoice_count: int,
    blocked_billing_count: int,
    unmatched_payment_count: int,
) -> CreatorWorkspaceState:
    readiness = build_creator_workspace_readiness(
        raw_stripe_status=raw_stripe_status,
        booking_links=booking_links,
        content_items=content_items,
        paid_invoice_count=paid_invoice_count,
    )
    return CreatorWorkspaceState(
        readiness=readiness,
        blocked_billing_count=blocked_billing_count,
        unmatched_payment_count=unmatched_payment_count,
        attention_count=blocked_billing_count + unmatched_payment_count,
    )
