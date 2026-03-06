from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel


class ContentCreateRequest(BaseModel):
    source_url: AnyHttpUrl
    booking_link_id: UUID


class ContentResponse(BaseModel):
    id: str
    booking_link_id: str
    source_url: str
    tid: str
    tracked_url: str
