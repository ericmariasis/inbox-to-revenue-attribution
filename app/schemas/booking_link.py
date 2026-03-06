from pydantic import AnyHttpUrl, BaseModel, TypeAdapter, ValidationError, field_validator

CALENDLY_URL_ADAPTER = TypeAdapter(AnyHttpUrl)
ALLOWED_CALENDLY_HOSTS = {"calendly.com", "www.calendly.com"}


class BookingLinkCreateRequest(BaseModel):
    name: str
    calendly_url: str

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


class BookingLinkResponse(BaseModel):
    id: str
    name: str
    calendly_url: str
