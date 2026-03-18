BOOKING_PROVIDER_CALENDLY = "calendly"
BOOKING_PROVIDER_FULLSCOPE = "fullscope"

SUPPORTED_BOOKING_PROVIDERS = frozenset(
    {
        BOOKING_PROVIDER_CALENDLY,
        BOOKING_PROVIDER_FULLSCOPE,
    }
)

TRACKED_DESTINATION_ENABLED_BOOKING_PROVIDERS = frozenset(
    {
        BOOKING_PROVIDER_CALENDLY,
        BOOKING_PROVIDER_FULLSCOPE,
    }
)

END_TO_END_TRACKED_CONTENT_ENABLED_BOOKING_PROVIDERS = frozenset(
    {
        BOOKING_PROVIDER_CALENDLY,
    }
)


def booking_provider_supports_tracked_destination(provider: str) -> bool:
    return provider in TRACKED_DESTINATION_ENABLED_BOOKING_PROVIDERS


def booking_provider_supports_tracked_content(provider: str) -> bool:
    return provider in END_TO_END_TRACKED_CONTENT_ENABLED_BOOKING_PROVIDERS
