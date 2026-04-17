import logging

from app.models.booking import Booking


logger = logging.getLogger(__name__)


def resolve_booking_billing_terms(*, booking: Booking) -> tuple[int | None, str | None]:
    frozen_amount_cents = booking.frozen_billing_amount_cents
    frozen_currency = booking.frozen_billing_currency
    if frozen_amount_cents is not None and frozen_currency is not None:
        return frozen_amount_cents, frozen_currency.upper()

    blocked_case = booking.blocked_billing_case
    if blocked_case is not None:
        booking.frozen_billing_amount_cents = blocked_case.frozen_amount_cents
        booking.frozen_billing_currency = blocked_case.frozen_currency.upper()
        return booking.frozen_billing_amount_cents, booking.frozen_billing_currency

    if frozen_amount_cents is not None or frozen_currency is not None:
        logger.warning(
            "billing_booking_frozen_billing_partial booking_id=%s creator_id=%s amount_present=%s currency_present=%s",
            booking.id,
            booking.creator_id,
            frozen_amount_cents is not None,
            frozen_currency is not None,
        )

    billing_amount_cents = booking.booking_link.billing_amount_cents
    billing_currency = booking.booking_link.billing_currency
    if billing_amount_cents is None or billing_currency is None:
        return billing_amount_cents, billing_currency.upper() if billing_currency else None

    booking.frozen_billing_amount_cents = billing_amount_cents
    booking.frozen_billing_currency = billing_currency.upper()
    return booking.frozen_billing_amount_cents, booking.frozen_billing_currency
