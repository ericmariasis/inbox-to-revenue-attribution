from pydantic import BaseModel


class BookingLinkCreateRequest(BaseModel):
    name: str
    calendly_url: str


class BookingLinkResponse(BaseModel):
    id: str
    name: str
    calendly_url: str
