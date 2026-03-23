from datetime import UTC, datetime

from app.models.blocked_billing_case import BlockedBillingCase
from app.models.booking import Booking
from app.models.booking_link import BookingLink
from app.models.creator import Creator
from app.services.billing_lifecycle import (
    FREEZE_SOURCE_BLOCKED_CASE,
    FREEZE_SOURCE_CREATOR,
    resolve_billing_account_freeze,
)


def test_resolve_billing_account_freeze_prefers_open_blocked_case_snapshot():
    creator = Creator(
        name="Story 100 Creator",
        billing_provider="paypal",
        billing_connect_status="connected",
        billing_account_id="merchant_story100_current",
        stripe_connect_status="connected",
        stripe_account_id="acct_story100_legacy",
    )
    booking_link = BookingLink(
        name="Story 100 Link",
        calendly_url="https://calendly.com/example/story100",
    )
    booking = Booking(
        email="story100@example.com",
        status="created",
        booked_at=datetime(2026, 3, 23, 14, 0, tzinfo=UTC),
    )
    blocked_case = BlockedBillingCase(
        tid="story100_tid",
        provider="calendly",
        provider_booking_id="BOOK_story100",
        payment_provider="stripe",
        provider_account_id="acct_story100_frozen",
        frozen_amount_cents=15000,
        frozen_currency="USD",
        status="open",
        reason_code="creator_not_billable",
        first_blocked_at=datetime(2026, 3, 23, 14, 5, tzinfo=UTC),
        last_blocked_at=datetime(2026, 3, 23, 14, 5, tzinfo=UTC),
    )

    creator.bookings.append(booking)
    creator.booking_links.append(booking_link)
    creator.blocked_billing_cases.append(blocked_case)
    booking.booking_link = booking_link
    booking.blocked_billing_case = blocked_case

    freeze = resolve_billing_account_freeze(booking=booking)

    assert freeze.payment_provider == "stripe"
    assert freeze.provider_account_id == "acct_story100_frozen"
    assert freeze.source == FREEZE_SOURCE_BLOCKED_CASE


def test_resolve_billing_account_freeze_falls_back_to_current_creator_snapshot_without_blocked_case():
    creator = Creator(
        name="Story 100 Creator",
        billing_provider="paypal",
        billing_connect_status="connected",
        billing_account_id="merchant_story100_current",
        stripe_connect_status="pending",
        stripe_account_id=None,
    )
    booking_link = BookingLink(
        name="Story 100 Link",
        calendly_url="https://calendly.com/example/story100-current",
    )
    booking = Booking(
        email="story100-current@example.com",
        status="created",
        booked_at=datetime(2026, 3, 23, 14, 10, tzinfo=UTC),
    )

    creator.bookings.append(booking)
    creator.booking_links.append(booking_link)
    booking.booking_link = booking_link

    freeze = resolve_billing_account_freeze(booking=booking)

    assert freeze.payment_provider == "paypal"
    assert freeze.provider_account_id == "merchant_story100_current"
    assert freeze.source == FREEZE_SOURCE_CREATOR
