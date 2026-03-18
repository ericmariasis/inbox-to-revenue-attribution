from pydantic import (
    AnyHttpUrl,
    BaseModel,
    Field,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from app.models.booking_provider import (
    BOOKING_PROVIDER_CALENDLY,
    BOOKING_PROVIDER_FULLSCOPE,
    SUPPORTED_BOOKING_PROVIDERS,
)

ABSOLUTE_URL_ADAPTER = TypeAdapter(AnyHttpUrl)
ALLOWED_CALENDLY_HOSTS = {"calendly.com", "www.calendly.com"}
ALLOWED_FULLSCOPE_HOSTS = {"links.fullscope.tools"}
SUPPORTED_FULLSCOPE_PATH_PREFIXES = (
    "/widget/bookings/",
    "/widget/booking/",
)
UNSUPPORTED_FULLSCOPE_PATH_PREFIXES = (
    "/widget/service-menus/",
)


def _validate_absolute_url(value: str) -> str:
    normalized = value.strip()
    try:
        ABSOLUTE_URL_ADAPTER.validate_python(normalized)
    except ValidationError as exc:
        raise ValueError("must be a valid absolute URL") from exc

    return normalized


def _validate_calendly_destination_url(value: str) -> str:
    normalized = _validate_absolute_url(value)
    parsed_url = ABSOLUTE_URL_ADAPTER.validate_python(normalized)

    if parsed_url.scheme != "https":
        raise ValueError("must use https")

    if parsed_url.host not in ALLOWED_CALENDLY_HOSTS:
        raise ValueError("must use calendly.com")

    if not parsed_url.path or not parsed_url.path.strip("/"):
        raise ValueError("must include a Calendly path")

    return normalized


def _validate_fullscope_destination_url(value: str) -> str:
    normalized = _validate_absolute_url(value)
    parsed_url = ABSOLUTE_URL_ADAPTER.validate_python(normalized)

    if parsed_url.scheme != "https":
        raise ValueError("must use https")

    if parsed_url.host not in ALLOWED_FULLSCOPE_HOSTS:
        raise ValueError("must use links.fullscope.tools")

    path = parsed_url.path or ""
    if any(path.startswith(prefix) for prefix in UNSUPPORTED_FULLSCOPE_PATH_PREFIXES):
        raise ValueError("must use a Personal Calendar or direct Service Calendar link")

    if not any(path.startswith(prefix) for prefix in SUPPORTED_FULLSCOPE_PATH_PREFIXES):
        raise ValueError("must use a FullScope booking URL")

    if not any(
        path.removeprefix(prefix).strip("/")
        for prefix in SUPPORTED_FULLSCOPE_PATH_PREFIXES
        if path.startswith(prefix)
    ):
        raise ValueError("must include a FullScope booking path")

    return normalized


class BookingLinkCreateRequest(BaseModel):
    provider: StrictStr = BOOKING_PROVIDER_CALENDLY
    name: str
    destination_url: str | None = None
    calendly_url: str | None = None
    fullscope_supported_calendar_confirmed: bool = Field(default=False, validate_default=True)
    billing_amount_cents: StrictInt | None = None
    billing_currency: StrictStr | None = None

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in SUPPORTED_BOOKING_PROVIDERS:
            raise ValueError("must be a supported booking provider")
        return normalized

    @field_validator("destination_url", "calendly_url")
    @classmethod
    def normalize_optional_urls(cls, value: str | None) -> str | None:
        if value is None:
            return None

        normalized = value.strip()
        return normalized or None

    @field_validator("fullscope_supported_calendar_confirmed")
    @classmethod
    def validate_fullscope_confirmation(cls, value: bool, info: ValidationInfo) -> bool:
        if info.data.get("provider") == BOOKING_PROVIDER_FULLSCOPE and value is not True:
            raise ValueError(
                "confirm this is a Personal Calendar or direct Service Calendar link"
            )
        return bool(value)

    @field_validator("billing_amount_cents")
    @classmethod
    def validate_billing_amount_cents(cls, value: int | None) -> int | None:
        if value is None:
            return None

        if value <= 0:
            raise ValueError("must be a positive integer amount in cents")

        return value

    @field_validator("billing_currency")
    @classmethod
    def validate_billing_currency(cls, value: str | None) -> str | None:
        if value is None:
            return None

        normalized = value.strip().upper()
        if len(normalized) != 3 or not normalized.isalpha():
            raise ValueError("must be a 3-letter currency code")

        return normalized

    @model_validator(mode="after")
    def validate_provider_specific_fields(self) -> "BookingLinkCreateRequest":
        if self.provider == BOOKING_PROVIDER_CALENDLY:
            if self.destination_url is None and self.calendly_url is None:
                raise ValueError("provide a destination URL")

            if (
                self.destination_url is not None
                and self.calendly_url is not None
                and self.destination_url != self.calendly_url
            ):
                raise ValueError(
                    "destination_url and calendly_url must match when both are provided"
                )

            resolved_url = self.destination_url or self.calendly_url
            assert resolved_url is not None
            validated_url = _validate_calendly_destination_url(resolved_url)
            self.destination_url = validated_url
            self.calendly_url = validated_url
            self.fullscope_supported_calendar_confirmed = False
            return self

        if self.calendly_url is not None:
            raise ValueError("FullScope requests must use destination_url, not calendly_url")

        if self.destination_url is None:
            raise ValueError("provide a destination URL")

        self.destination_url = _validate_fullscope_destination_url(self.destination_url)
        self.calendly_url = None
        return self


class BookingLinkResponse(BaseModel):
    id: str
    name: str
    provider: str
    destination_url: str
    calendly_url: str | None = None
    billing_amount_cents: int | None = None
    billing_currency: str | None = None
