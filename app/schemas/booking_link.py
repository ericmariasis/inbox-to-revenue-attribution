from pydantic import (
    AnyHttpUrl,
    BaseModel,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
    field_validator,
)

CALENDLY_URL_ADAPTER = TypeAdapter(AnyHttpUrl)
ALLOWED_CALENDLY_HOSTS = {"calendly.com", "www.calendly.com"}


class BookingLinkCreateRequest(BaseModel):
    name: str
    calendly_url: str
    billing_amount_cents: StrictInt | None = None
    billing_currency: StrictStr | None = None

    @field_validator("calendly_url")
    @classmethod
    def validate_calendly_url(cls, value: str) -> str:
        try:
            parsed_url = CALENDLY_URL_ADAPTER.validate_python(value)
        except ValidationError as exc:
            raise ValueError("must be a valid absolute URL") from exc

        if parsed_url.scheme != "https":
            raise ValueError("must use https")

        if parsed_url.host not in ALLOWED_CALENDLY_HOSTS:
            raise ValueError("must use calendly.com")

        if not parsed_url.path or not parsed_url.path.strip("/"):
            raise ValueError("must include a Calendly path")

        return str(parsed_url)

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


class BookingLinkResponse(BaseModel):
    id: str
    name: str
    calendly_url: str
    billing_amount_cents: int | None = None
    billing_currency: str | None = None
