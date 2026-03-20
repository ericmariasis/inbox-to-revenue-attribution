from dataclasses import dataclass

from app.models.booking_provider import (
    booking_provider_supports_creator_visible_tracked_content,
    booking_provider_supports_creator_visible_tracked_destination,
)
from app.schemas.booking_link import BookingLinkResponse
from app.schemas.content import ContentResponse


@dataclass(frozen=True)
class CreatorWorkspaceReadiness:
    billing_connect_status: str
    billing_connected: bool
    billable_now: bool
    ready_to_track: bool
    waiting_for_first_paid_result: bool
    booking_links_count: int
    trackable_booking_links_count: int
    limited_tracking_booking_links_count: int
    billing_ready_count: int
    tracked_content_count: int
    paid_invoice_count: int

    @property
    def stripe_status(self) -> str:
        return self.billing_connect_status

    @property
    def connected(self) -> bool:
        return self.billing_connected


@dataclass(frozen=True)
class CreatorWorkspaceState:
    readiness: CreatorWorkspaceReadiness
    blocked_billing_count: int
    unmatched_payment_count: int
    attention_count: int


def build_creator_workspace_readiness(
    *,
    raw_billing_connect_status: str,
    booking_links: list[BookingLinkResponse],
    content_items: list[ContentResponse],
    paid_invoice_count: int,
) -> CreatorWorkspaceReadiness:
    normalized_billing_connect_status = raw_billing_connect_status.strip().lower()
    booking_links_count = len(booking_links)
    trackable_booking_links_count = sum(
        1
        for booking_link in booking_links
        if booking_provider_supports_creator_visible_tracked_content(booking_link.provider)
    )
    limited_tracking_booking_links_count = sum(
        1
        for booking_link in booking_links
        if booking_provider_supports_creator_visible_tracked_destination(booking_link.provider)
        and not booking_provider_supports_creator_visible_tracked_content(booking_link.provider)
    )
    billing_ready_count = sum(
        1
        for booking_link in booking_links
        if booking_provider_supports_creator_visible_tracked_content(booking_link.provider)
        if booking_link.billing_amount_cents is not None
        and booking_link.billing_currency is not None
    )
    tracked_content_count = len(content_items)
    billing_connected = normalized_billing_connect_status == "connected"
    billable_now = billing_connected and billing_ready_count > 0
    ready_to_track = billable_now and tracked_content_count > 0
    waiting_for_first_paid_result = ready_to_track and paid_invoice_count == 0

    return CreatorWorkspaceReadiness(
        billing_connect_status=normalized_billing_connect_status,
        billing_connected=billing_connected,
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
    raw_billing_connect_status: str,
    booking_links: list[BookingLinkResponse],
    content_items: list[ContentResponse],
    paid_invoice_count: int,
    blocked_billing_count: int,
    unmatched_payment_count: int,
) -> CreatorWorkspaceState:
    readiness = build_creator_workspace_readiness(
        raw_billing_connect_status=raw_billing_connect_status,
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
