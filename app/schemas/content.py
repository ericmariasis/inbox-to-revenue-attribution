from datetime import datetime
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, field_validator


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


class ContentTopicCandidateResponse(BaseModel):
    id: str
    content_id: str
    content_tid: str
    extraction_artifact_id: str
    confirmed_topic_id: str | None
    suggested_label: str
    normalized_label: str
    suggestion_method: str
    candidate_rank: int
    review_status: str
    reviewed_at: datetime | None
    created_at: datetime


class ContentConfirmedTopicResponse(BaseModel):
    id: str
    content_id: str
    content_tid: str
    canonical_label: str
    normalized_label: str
    created_at: datetime
    updated_at: datetime


class ContentTopicReviewResponse(BaseModel):
    content_id: str
    content_tid: str
    source_url: str
    tracked_url: str
    extraction_artifact_id: str
    extraction_status: str
    extraction_method: str | None
    extraction_title: str | None
    candidate_topics: list[ContentTopicCandidateResponse]
    confirmed_topics: list[ContentConfirmedTopicResponse]


class ContentTopicCandidateConfirmRequest(BaseModel):
    confirmed_label: str | None = None

    @field_validator("confirmed_label")
    @classmethod
    def _normalize_confirmed_label(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("confirmed_label cannot be blank")
        if len(normalized) > 255:
            raise ValueError("confirmed_label must be 255 characters or fewer")
        return normalized
