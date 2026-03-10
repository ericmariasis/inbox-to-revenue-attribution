from datetime import datetime
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


class ContentFetchSnapshotResponse(BaseModel):
    id: str
    content_id: str
    content_tid: str
    requested_url: str
    fetched_url: str | None
    fetch_status: str
    http_status: int | None
    failure_reason_code: str | None
    failure_detail: str | None
    response_content_type: str | None
    response_content_charset: str | None
    snapshot_text: str | None
    fetched_at: datetime


class ContentExtractionArtifactResponse(BaseModel):
    id: str
    content_id: str
    content_tid: str
    fetch_snapshot_id: str
    extraction_status: str
    extraction_reason_code: str | None
    extraction_detail: str | None
    extraction_method: str | None
    title: str | None
    published_at: datetime | None
    published_at_raw: str | None
    source_text_char_count: int
    extracted_text_char_count: int
    extracted_text_word_count: int
    extracted_text: str | None
    created_at: datetime
