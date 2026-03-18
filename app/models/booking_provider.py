BOOKING_PROVIDER_CALENDLY = "calendly"
BOOKING_PROVIDER_FULLSCOPE = "fullscope"

SUPPORTED_BOOKING_PROVIDERS = frozenset(
    {
        BOOKING_PROVIDER_CALENDLY,
        BOOKING_PROVIDER_FULLSCOPE,
    }
)

TRACKED_CONTENT_ENABLED_BOOKING_PROVIDERS = frozenset(
    {
        BOOKING_PROVIDER_CALENDLY,
    }
)


def booking_provider_supports_tracked_content(provider: str) -> bool:
    return provider in TRACKED_CONTENT_ENABLED_BOOKING_PROVIDERS
