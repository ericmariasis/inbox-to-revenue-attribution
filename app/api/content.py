import logging
import uuid
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_auth_user
from app.core.config import get_settings
from app.db.session import get_db
from app.models.auth_user import AuthUser
from app.models.booking_link import BookingLink
from app.models.booking_provider import booking_provider_supports_tracked_content
from app.models.content import Content
from app.models.content_confirmed_topic import ContentConfirmedTopic
from app.models.content_extraction_artifact import ContentExtractionArtifact
from app.models.content_fetch_snapshot import ContentFetchSnapshot
from app.models.content_topic_candidate import ContentTopicCandidate
from app.schemas.content import (
    ContentAuthoritativeStateResponse,
    ContentConfirmedTopicResponse,
    ContentCreateRequest,
    ContentExtractionArtifactResponse,
    ContentFetchSnapshotResponse,
    ContentResponse,
    ContentTopicCandidateConfirmRequest,
    ContentTopicCandidateResponse,
    ContentTopicReviewResponse,
)
from app.services.authoritative_content_evidence import build_content_authority_state
from app.services.content_extraction import extract_content_from_snapshot
from app.services.content_fetch import (
    ContentFetchProvider,
    ContentFetchSuccess,
    build_default_content_fetch_provider,
)
from app.services.content_topics import (
    CONTENT_TOPIC_REVIEW_STATUS_CONFIRMED,
    CONTENT_TOPIC_REVIEW_STATUS_PENDING,
    CONTENT_TOPIC_REVIEW_STATUS_REJECTED,
    build_content_topic_suggestions,
    normalize_topic_label,
    normalize_topic_label_display,
)

router = APIRouter(prefix="/content", tags=["content"])
logger = logging.getLogger(__name__)


def _creator_owned_booking_link_query(
    *,
    booking_link_id: UUID,
    creator_id: UUID,
) -> Select[tuple[BookingLink]]:
    return select(BookingLink).where(
        BookingLink.id == booking_link_id,
        BookingLink.creator_id == creator_id,
    )


def _creator_scoped_content_query(*, creator_id: UUID) -> Select[tuple[Content]]:
    return (
        select(Content)
        .where(Content.creator_id == creator_id)
        .order_by(Content.created_at.asc(), Content.id.asc())
    )


def _creator_owned_content_by_tid_query(*, tid: str, creator_id: UUID) -> Select[tuple[Content]]:
    return select(Content).where(
        Content.tid == tid,
        Content.creator_id == creator_id,
    )


def _tracked_url_for_tid(tid: str) -> str:
    base_url = get_settings().tracked_link_base_url.rstrip("/")
    return f"{base_url}/r/{tid}"


def _build_content_response(content: Content) -> ContentResponse:
    return ContentResponse(
        id=str(content.id),
        booking_link_id=str(content.booking_link_id),
        source_url=content.source_url,
        tid=content.tid,
        tracked_url=_tracked_url_for_tid(content.tid),
    )


def _build_content_fetch_snapshot_response(
    snapshot: ContentFetchSnapshot,
) -> ContentFetchSnapshotResponse:
    return ContentFetchSnapshotResponse(
        id=str(snapshot.id),
        content_id=str(snapshot.content_id),
        content_tid=snapshot.content.tid,
        requested_url=snapshot.requested_url,
        fetched_url=snapshot.fetched_url,
        fetch_status=snapshot.fetch_status,
        http_status=snapshot.http_status,
        failure_reason_code=snapshot.failure_reason_code,
        failure_detail=snapshot.failure_detail,
        response_content_type=snapshot.response_content_type,
        response_content_charset=snapshot.response_content_charset,
        snapshot_text=snapshot.snapshot_text,
        fetched_at=snapshot.fetched_at,
    )


def _build_content_extraction_artifact_response(
    artifact: ContentExtractionArtifact,
) -> ContentExtractionArtifactResponse:
    return ContentExtractionArtifactResponse(
        id=str(artifact.id),
        content_id=str(artifact.content_id),
        content_tid=artifact.content.tid,
        fetch_snapshot_id=str(artifact.fetch_snapshot_id),
        extraction_status=artifact.extraction_status,
        extraction_reason_code=artifact.extraction_reason_code,
        extraction_detail=artifact.extraction_detail,
        extraction_method=artifact.extraction_method,
        title=artifact.title,
        published_at=artifact.published_at,
        published_at_raw=artifact.published_at_raw,
        source_text_char_count=artifact.source_text_char_count or 0,
        extracted_text_char_count=artifact.extracted_text_char_count or 0,
        extracted_text_word_count=artifact.extracted_text_word_count or 0,
        extracted_text=artifact.extracted_text,
        created_at=artifact.created_at,
    )


def _build_content_topic_candidate_response(
    candidate: ContentTopicCandidate,
    *,
    content_tid: str,
) -> ContentTopicCandidateResponse:
    return ContentTopicCandidateResponse(
        id=str(candidate.id),
        content_id=str(candidate.content_id),
        content_tid=content_tid,
        extraction_artifact_id=str(candidate.extraction_artifact_id),
        confirmed_topic_id=str(candidate.confirmed_topic_id) if candidate.confirmed_topic_id else None,
        suggested_label=candidate.suggested_label,
        normalized_label=candidate.normalized_label,
        suggestion_method=candidate.suggestion_method,
        candidate_rank=candidate.candidate_rank,
        review_status=candidate.review_status,
        reviewed_at=candidate.reviewed_at,
        created_at=candidate.created_at,
    )


def _build_content_confirmed_topic_response(
    confirmed_topic: ContentConfirmedTopic,
    *,
    content_tid: str,
) -> ContentConfirmedTopicResponse:
    return ContentConfirmedTopicResponse(
        id=str(confirmed_topic.id),
        content_id=str(confirmed_topic.content_id),
        content_tid=content_tid,
        canonical_label=confirmed_topic.canonical_label,
        normalized_label=confirmed_topic.normalized_label,
        created_at=confirmed_topic.created_at,
        updated_at=confirmed_topic.updated_at,
    )


def _build_content_authoritative_state_response(
    *,
    authoritative_extraction_artifact_id: UUID | None,
    authoritative_fetch_snapshot_id: UUID | None,
    is_current_artifact_authoritative: bool,
    promotion_allowed: bool,
    promotion_block_reason: str | None,
) -> ContentAuthoritativeStateResponse:
    return ContentAuthoritativeStateResponse(
        authoritative_extraction_artifact_id=(
            str(authoritative_extraction_artifact_id)
            if authoritative_extraction_artifact_id is not None
            else None
        ),
        authoritative_fetch_snapshot_id=(
            str(authoritative_fetch_snapshot_id)
            if authoritative_fetch_snapshot_id is not None
            else None
        ),
        is_current_artifact_authoritative=is_current_artifact_authoritative,
        promotion_allowed=promotion_allowed,
        promotion_block_reason=promotion_block_reason,
    )


def _build_content_topic_review_response(
    *,
    content: Content,
    artifact: ContentExtractionArtifact,
    candidate_topics: list[ContentTopicCandidate],
    review_confirmed_topics: list[ContentConfirmedTopic],
    authoritative_confirmed_topics: list[ContentConfirmedTopic],
    authoritative_extraction_artifact_id: UUID | None,
    authoritative_fetch_snapshot_id: UUID | None,
    is_current_artifact_authoritative: bool,
    promotion_allowed: bool,
    promotion_block_reason: str | None,
) -> ContentTopicReviewResponse:
    return ContentTopicReviewResponse(
        content_id=str(content.id),
        content_tid=content.tid,
        source_url=content.source_url,
        tracked_url=_tracked_url_for_tid(content.tid),
        extraction_artifact_id=str(artifact.id),
        extraction_status=artifact.extraction_status,
        extraction_method=artifact.extraction_method,
        extraction_title=artifact.title,
        authoritative_state=_build_content_authoritative_state_response(
            authoritative_extraction_artifact_id=authoritative_extraction_artifact_id,
            authoritative_fetch_snapshot_id=authoritative_fetch_snapshot_id,
            is_current_artifact_authoritative=is_current_artifact_authoritative,
            promotion_allowed=promotion_allowed,
            promotion_block_reason=promotion_block_reason,
        ),
        candidate_topics=[
            _build_content_topic_candidate_response(candidate, content_tid=content.tid)
            for candidate in candidate_topics
        ],
        review_confirmed_topics=[
            _build_content_confirmed_topic_response(confirmed_topic, content_tid=content.tid)
            for confirmed_topic in review_confirmed_topics
        ],
        authoritative_confirmed_topics=[
            _build_content_confirmed_topic_response(confirmed_topic, content_tid=content.tid)
            for confirmed_topic in authoritative_confirmed_topics
        ],
    )


def _content_fetch_provider(request: Request) -> ContentFetchProvider:
    return getattr(request.app.state, "content_fetch_provider", build_default_content_fetch_provider())


def _latest_content_fetch_snapshot_query(
    *,
    content_id: UUID,
    creator_id: UUID,
) -> Select[tuple[ContentFetchSnapshot]]:
    return (
        select(ContentFetchSnapshot)
        .where(
            ContentFetchSnapshot.content_id == content_id,
            ContentFetchSnapshot.creator_id == creator_id,
        )
        .order_by(ContentFetchSnapshot.fetched_at.desc(), ContentFetchSnapshot.id.desc())
    )


def _content_extraction_artifact_by_fetch_snapshot_query(
    *,
    fetch_snapshot_id: UUID,
) -> Select[tuple[ContentExtractionArtifact]]:
    return select(ContentExtractionArtifact).where(
        ContentExtractionArtifact.fetch_snapshot_id == fetch_snapshot_id
    )


def _latest_content_extraction_artifact_query(
    *,
    content_id: UUID,
    creator_id: UUID,
) -> Select[tuple[ContentExtractionArtifact]]:
    return (
        select(ContentExtractionArtifact)
        .where(
            ContentExtractionArtifact.content_id == content_id,
            ContentExtractionArtifact.creator_id == creator_id,
        )
        .order_by(ContentExtractionArtifact.created_at.desc(), ContentExtractionArtifact.id.desc())
    )


def _content_topic_candidates_for_artifact_query(
    *,
    extraction_artifact_id: UUID,
) -> Select[tuple[ContentTopicCandidate]]:
    return (
        select(ContentTopicCandidate)
        .where(ContentTopicCandidate.extraction_artifact_id == extraction_artifact_id)
        .order_by(
            ContentTopicCandidate.candidate_rank.asc(),
            ContentTopicCandidate.created_at.asc(),
            ContentTopicCandidate.id.asc(),
        )
    )


def _content_topic_candidate_query(
    *,
    candidate_id: UUID,
    content_id: UUID,
    creator_id: UUID,
    extraction_artifact_id: UUID,
) -> Select[tuple[ContentTopicCandidate]]:
    return select(ContentTopicCandidate).where(
        ContentTopicCandidate.id == candidate_id,
        ContentTopicCandidate.content_id == content_id,
        ContentTopicCandidate.creator_id == creator_id,
        ContentTopicCandidate.extraction_artifact_id == extraction_artifact_id,
    )


def _content_confirmed_topic_by_normalized_label_query(
    *,
    content_id: UUID,
    normalized_label: str,
) -> Select[tuple[ContentConfirmedTopic]]:
    return select(ContentConfirmedTopic).where(
        ContentConfirmedTopic.content_id == content_id,
        ContentConfirmedTopic.normalized_label == normalized_label,
    )


def _get_creator_owned_content_or_404(
    *,
    tid: str,
    creator_id: UUID,
    db: Session,
) -> Content:
    content = db.execute(
        _creator_owned_content_by_tid_query(
            tid=tid,
            creator_id=creator_id,
        )
    ).scalar_one_or_none()
    if content is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="content not found",
        )
    return content


def _get_latest_content_extraction_artifact_or_409(
    *,
    content_id: UUID,
    creator_id: UUID,
    db: Session,
) -> ContentExtractionArtifact:
    artifact = db.execute(
        _latest_content_extraction_artifact_query(
            content_id=content_id,
            creator_id=creator_id,
        )
    ).scalars().first()
    if artifact is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="content extraction artifact required",
        )
    return artifact


def _content_topic_review_response_for_state(
    *,
    content: Content,
    artifact: ContentExtractionArtifact,
    db: Session,
) -> ContentTopicReviewResponse:
    candidate_topics = db.execute(
        _content_topic_candidates_for_artifact_query(
            extraction_artifact_id=artifact.id,
        )
    ).scalars().all()
    authority_state = build_content_authority_state(
        content=content,
        artifact=artifact,
        candidate_topics=candidate_topics,
        db=db,
    )
    return _build_content_topic_review_response(
        content=content,
        artifact=artifact,
        candidate_topics=candidate_topics,
        review_confirmed_topics=authority_state.review_confirmed_topics,
        authoritative_confirmed_topics=authority_state.authoritative_confirmed_topics,
        authoritative_extraction_artifact_id=authority_state.authoritative_extraction_artifact_id,
        authoritative_fetch_snapshot_id=authority_state.authoritative_fetch_snapshot_id,
        is_current_artifact_authoritative=authority_state.is_current_artifact_authoritative,
        promotion_allowed=authority_state.promotion_allowed,
        promotion_block_reason=authority_state.promotion_block_reason,
    )


def _delete_confirmed_topic_if_unused(
    *,
    confirmed_topic_id: UUID | None,
    db: Session,
) -> None:
    if confirmed_topic_id is None:
        return

    still_used = db.execute(
        select(ContentTopicCandidate.id)
        .where(ContentTopicCandidate.confirmed_topic_id == confirmed_topic_id)
        .limit(1)
    ).first()
    if still_used is not None:
        return

    confirmed_topic = db.get(ContentConfirmedTopic, confirmed_topic_id)
    if confirmed_topic is not None:
        db.delete(confirmed_topic)


def create_content_response_for_creator(
    *,
    creator_id: UUID,
    payload: ContentCreateRequest,
    db: Session,
) -> ContentResponse:
    booking_link = db.execute(
        _creator_owned_booking_link_query(
            booking_link_id=payload.booking_link_id,
            creator_id=creator_id,
        )
    ).scalar_one_or_none()
    if booking_link is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="booking link not found",
        )

    if not booking_provider_supports_tracked_content(booking_link.provider):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="booking link provider not supported for tracked content",
        )

    content = Content(
        creator_id=creator_id,
        booking_link_id=booking_link.id,
        source_url=str(payload.source_url),
        tid=uuid.uuid4().hex,
    )
    db.add(content)
    db.commit()
    db.refresh(content)

    logger.info("content_created")

    return _build_content_response(content)


def create_content_fetch_snapshot_response_for_creator(
    *,
    tid: str,
    creator_id: UUID,
    db: Session,
    provider: ContentFetchProvider,
) -> ContentFetchSnapshotResponse:
    content = _get_creator_owned_content_or_404(
        tid=tid,
        creator_id=creator_id,
        db=db,
    )

    fetch_result = provider.fetch_public_url(source_url=content.source_url)
    snapshot = ContentFetchSnapshot(
        content_id=content.id,
        creator_id=creator_id,
        requested_url=content.source_url,
        fetched_url=fetch_result.fetched_url,
        fetch_status=fetch_result.fetch_status,
        http_status=fetch_result.http_status,
        failure_reason_code=(
            fetch_result.failure_reason_code
            if isinstance(fetch_result, ContentFetchSuccess)
            else fetch_result.reason_code
        ),
        failure_detail=(
            fetch_result.failure_detail
            if isinstance(fetch_result, ContentFetchSuccess)
            else fetch_result.detail
        ),
        response_content_type=fetch_result.response_content_type,
        response_content_charset=fetch_result.response_content_charset,
        snapshot_text=fetch_result.snapshot_text,
    )
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)

    logger.info("content_fetch_snapshot_created status=%s", snapshot.fetch_status)

    return _build_content_fetch_snapshot_response(snapshot)


def create_content_extraction_artifact_response_for_creator(
    *,
    tid: str,
    creator_id: UUID,
    db: Session,
    response: Response,
) -> ContentExtractionArtifactResponse:
    content = _get_creator_owned_content_or_404(
        tid=tid,
        creator_id=creator_id,
        db=db,
    )
    latest_snapshot = db.execute(
        _latest_content_fetch_snapshot_query(
            content_id=content.id,
            creator_id=creator_id,
        )
    ).scalars().first()
    if latest_snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="content fetch snapshot required",
        )

    artifact = db.execute(
        _content_extraction_artifact_by_fetch_snapshot_query(
            fetch_snapshot_id=latest_snapshot.id,
        )
    ).scalar_one_or_none()
    if artifact is not None:
        response.status_code = status.HTTP_200_OK
        logger.info("content_extraction_artifact_reused status=%s", artifact.extraction_status)
        return _build_content_extraction_artifact_response(artifact)

    extraction_result = extract_content_from_snapshot(latest_snapshot)
    artifact = ContentExtractionArtifact(
        content_id=content.id,
        creator_id=creator_id,
        fetch_snapshot_id=latest_snapshot.id,
        extraction_status=extraction_result.extraction_status,
        extraction_reason_code=extraction_result.extraction_reason_code,
        extraction_detail=extraction_result.extraction_detail,
        extraction_method=extraction_result.extraction_method,
        title=extraction_result.title,
        published_at=extraction_result.published_at,
        published_at_raw=extraction_result.published_at_raw,
        source_text_char_count=extraction_result.source_text_char_count,
        extracted_text_char_count=extraction_result.extracted_text_char_count,
        extracted_text_word_count=extraction_result.extracted_text_word_count,
        extracted_text=extraction_result.extracted_text,
    )
    db.add(artifact)
    db.commit()
    db.refresh(artifact)

    logger.info("content_extraction_artifact_created status=%s", artifact.extraction_status)

    return _build_content_extraction_artifact_response(artifact)


def get_content_topic_review_response_for_creator(
    *,
    tid: str,
    creator_id: UUID,
    db: Session,
) -> ContentTopicReviewResponse:
    content = _get_creator_owned_content_or_404(
        tid=tid,
        creator_id=creator_id,
        db=db,
    )
    artifact = _get_latest_content_extraction_artifact_or_409(
        content_id=content.id,
        creator_id=creator_id,
        db=db,
    )
    return _content_topic_review_response_for_state(
        content=content,
        artifact=artifact,
        db=db,
    )


def create_content_topic_candidates_response_for_creator(
    *,
    tid: str,
    creator_id: UUID,
    db: Session,
    response: Response,
) -> ContentTopicReviewResponse:
    content = _get_creator_owned_content_or_404(
        tid=tid,
        creator_id=creator_id,
        db=db,
    )
    artifact = _get_latest_content_extraction_artifact_or_409(
        content_id=content.id,
        creator_id=creator_id,
        db=db,
    )

    existing_candidates = db.execute(
        _content_topic_candidates_for_artifact_query(
            extraction_artifact_id=artifact.id,
        )
    ).scalars().all()
    if existing_candidates:
        response.status_code = status.HTTP_200_OK
        logger.info(
            "content_topic_candidates_reused extraction_artifact_id=%s count=%s",
            artifact.id,
            len(existing_candidates),
        )
        return _content_topic_review_response_for_state(
            content=content,
            artifact=artifact,
            db=db,
        )

    if not artifact.extracted_text:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="usable content extraction artifact required",
        )

    suggestions = build_content_topic_suggestions(
        title=artifact.title,
        extracted_text=artifact.extracted_text,
    )
    if not suggestions:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="topic candidates unavailable for this extraction artifact",
        )

    for rank, suggestion in enumerate(suggestions, start=1):
        db.add(
            ContentTopicCandidate(
                content_id=content.id,
                creator_id=creator_id,
                extraction_artifact_id=artifact.id,
                suggested_label=suggestion.suggested_label,
                normalized_label=suggestion.normalized_label,
                suggestion_method=suggestion.suggestion_method,
                candidate_rank=rank,
                review_status=CONTENT_TOPIC_REVIEW_STATUS_PENDING,
            )
        )
    db.commit()

    logger.info(
        "content_topic_candidates_created extraction_artifact_id=%s count=%s",
        artifact.id,
        len(suggestions),
    )

    return _content_topic_review_response_for_state(
        content=content,
        artifact=artifact,
        db=db,
    )


def confirm_content_topic_candidate_response_for_creator(
    *,
    tid: str,
    candidate_id: UUID,
    creator_id: UUID,
    payload: ContentTopicCandidateConfirmRequest,
    db: Session,
) -> ContentTopicReviewResponse:
    content = _get_creator_owned_content_or_404(
        tid=tid,
        creator_id=creator_id,
        db=db,
    )
    artifact = _get_latest_content_extraction_artifact_or_409(
        content_id=content.id,
        creator_id=creator_id,
        db=db,
    )
    candidate = db.execute(
        _content_topic_candidate_query(
            candidate_id=candidate_id,
            content_id=content.id,
            creator_id=creator_id,
            extraction_artifact_id=artifact.id,
        )
    ).scalar_one_or_none()
    if candidate is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="topic candidate not found",
        )

    raw_confirmed_label = payload.confirmed_label or candidate.suggested_label
    try:
        canonical_label = normalize_topic_label_display(raw_confirmed_label)
        normalized_label = normalize_topic_label(raw_confirmed_label)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    previous_confirmed_topic_id = candidate.confirmed_topic_id
    confirmed_topic = db.execute(
        _content_confirmed_topic_by_normalized_label_query(
            content_id=content.id,
            normalized_label=normalized_label,
        )
    ).scalar_one_or_none()
    if confirmed_topic is None:
        confirmed_topic = ContentConfirmedTopic(
            content_id=content.id,
            creator_id=creator_id,
            canonical_label=canonical_label,
            normalized_label=normalized_label,
        )
        db.add(confirmed_topic)
        db.flush()
    else:
        confirmed_topic.canonical_label = canonical_label

    candidate.review_status = CONTENT_TOPIC_REVIEW_STATUS_CONFIRMED
    candidate.confirmed_topic_id = confirmed_topic.id
    candidate.reviewed_at = datetime.now(timezone.utc)
    db.flush()

    if previous_confirmed_topic_id and previous_confirmed_topic_id != confirmed_topic.id:
        _delete_confirmed_topic_if_unused(
            confirmed_topic_id=previous_confirmed_topic_id,
            db=db,
        )

    db.commit()

    logger.info(
        "content_topic_candidate_confirmed candidate_id=%s confirmed_topic_id=%s",
        candidate.id,
        confirmed_topic.id,
    )

    return _content_topic_review_response_for_state(
        content=content,
        artifact=artifact,
        db=db,
    )


def reject_content_topic_candidate_response_for_creator(
    *,
    tid: str,
    candidate_id: UUID,
    creator_id: UUID,
    db: Session,
) -> ContentTopicReviewResponse:
    content = _get_creator_owned_content_or_404(
        tid=tid,
        creator_id=creator_id,
        db=db,
    )
    artifact = _get_latest_content_extraction_artifact_or_409(
        content_id=content.id,
        creator_id=creator_id,
        db=db,
    )
    candidate = db.execute(
        _content_topic_candidate_query(
            candidate_id=candidate_id,
            content_id=content.id,
            creator_id=creator_id,
            extraction_artifact_id=artifact.id,
        )
    ).scalar_one_or_none()
    if candidate is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="topic candidate not found",
        )

    previous_confirmed_topic_id = candidate.confirmed_topic_id
    candidate.review_status = CONTENT_TOPIC_REVIEW_STATUS_REJECTED
    candidate.confirmed_topic_id = None
    candidate.reviewed_at = datetime.now(timezone.utc)
    db.flush()

    _delete_confirmed_topic_if_unused(
        confirmed_topic_id=previous_confirmed_topic_id,
        db=db,
    )
    db.commit()

    logger.info("content_topic_candidate_rejected candidate_id=%s", candidate.id)

    return _content_topic_review_response_for_state(
        content=content,
        artifact=artifact,
        db=db,
    )


def promote_content_authoritative_evidence_response_for_creator(
    *,
    tid: str,
    creator_id: UUID,
    db: Session,
) -> ContentTopicReviewResponse:
    content = _get_creator_owned_content_or_404(
        tid=tid,
        creator_id=creator_id,
        db=db,
    )
    artifact = _get_latest_content_extraction_artifact_or_409(
        content_id=content.id,
        creator_id=creator_id,
        db=db,
    )
    candidate_topics = db.execute(
        _content_topic_candidates_for_artifact_query(
            extraction_artifact_id=artifact.id,
        )
    ).scalars().all()
    authority_state = build_content_authority_state(
        content=content,
        artifact=artifact,
        candidate_topics=candidate_topics,
        db=db,
    )

    if authority_state.is_current_artifact_authoritative:
        logger.info(
            "content_authoritative_evidence_already_current extraction_artifact_id=%s",
            artifact.id,
        )
        return _content_topic_review_response_for_state(
            content=content,
            artifact=artifact,
            db=db,
        )

    if not authority_state.promotion_allowed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=authority_state.promotion_block_reason or "promotion unavailable",
        )

    content.authoritative_extraction_artifact_id = artifact.id
    db.commit()
    db.refresh(content)

    logger.info(
        "content_authoritative_evidence_promoted extraction_artifact_id=%s",
        artifact.id,
    )

    return _content_topic_review_response_for_state(
        content=content,
        artifact=artifact,
        db=db,
    )


def list_content_responses_for_creator(
    *,
    creator_id: UUID,
    db: Session,
) -> list[ContentResponse]:
    content_rows = db.execute(
        _creator_scoped_content_query(creator_id=creator_id)
    ).scalars().all()

    logger.info("content_listed")

    return [_build_content_response(content) for content in content_rows]


def get_content_response_for_creator_by_tid(
    *,
    tid: str,
    creator_id: UUID,
    db: Session,
) -> ContentResponse | None:
    content = db.execute(
        _creator_owned_content_by_tid_query(
            tid=tid,
            creator_id=creator_id,
        )
    ).scalar_one_or_none()
    if content is None:
        return None

    logger.info("content_detail_fetched")

    return _build_content_response(content)


@router.post("", response_model=ContentResponse, status_code=status.HTTP_201_CREATED)
def create_content(
    payload: ContentCreateRequest,
    current_user: AuthUser = Depends(get_current_auth_user),
    db: Session = Depends(get_db),
) -> ContentResponse:
    return create_content_response_for_creator(
        creator_id=current_user.creator_id,
        payload=payload,
        db=db,
    )


@router.get("", response_model=list[ContentResponse])
def list_content(
    current_user: AuthUser = Depends(get_current_auth_user),
    db: Session = Depends(get_db),
) -> list[ContentResponse]:
    return list_content_responses_for_creator(
        creator_id=current_user.creator_id,
        db=db,
    )


@router.post(
    "/{tid}/fetch",
    response_model=ContentFetchSnapshotResponse,
    status_code=status.HTTP_201_CREATED,
)
def fetch_content_snapshot(
    tid: str,
    request: Request,
    current_user: AuthUser = Depends(get_current_auth_user),
    db: Session = Depends(get_db),
) -> ContentFetchSnapshotResponse:
    return create_content_fetch_snapshot_response_for_creator(
        tid=tid,
        creator_id=current_user.creator_id,
        db=db,
        provider=_content_fetch_provider(request),
    )


@router.post(
    "/{tid}/extract",
    response_model=ContentExtractionArtifactResponse,
    status_code=status.HTTP_201_CREATED,
)
def extract_content_artifact(
    tid: str,
    response: Response,
    current_user: AuthUser = Depends(get_current_auth_user),
    db: Session = Depends(get_db),
) -> ContentExtractionArtifactResponse:
    return create_content_extraction_artifact_response_for_creator(
        tid=tid,
        creator_id=current_user.creator_id,
        db=db,
        response=response,
    )


@router.get(
    "/{tid}/topics",
    response_model=ContentTopicReviewResponse,
)
def get_content_topic_review(
    tid: str,
    current_user: AuthUser = Depends(get_current_auth_user),
    db: Session = Depends(get_db),
) -> ContentTopicReviewResponse:
    return get_content_topic_review_response_for_creator(
        tid=tid,
        creator_id=current_user.creator_id,
        db=db,
    )


@router.post(
    "/{tid}/topics/candidates",
    response_model=ContentTopicReviewResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_content_topic_candidates(
    tid: str,
    response: Response,
    current_user: AuthUser = Depends(get_current_auth_user),
    db: Session = Depends(get_db),
) -> ContentTopicReviewResponse:
    return create_content_topic_candidates_response_for_creator(
        tid=tid,
        creator_id=current_user.creator_id,
        db=db,
        response=response,
    )


@router.post(
    "/{tid}/topics/{candidate_id}/confirm",
    response_model=ContentTopicReviewResponse,
)
def confirm_content_topic_candidate(
    tid: str,
    candidate_id: UUID,
    payload: ContentTopicCandidateConfirmRequest,
    current_user: AuthUser = Depends(get_current_auth_user),
    db: Session = Depends(get_db),
) -> ContentTopicReviewResponse:
    return confirm_content_topic_candidate_response_for_creator(
        tid=tid,
        candidate_id=candidate_id,
        creator_id=current_user.creator_id,
        payload=payload,
        db=db,
    )


@router.post(
    "/{tid}/topics/{candidate_id}/reject",
    response_model=ContentTopicReviewResponse,
)
def reject_content_topic_candidate(
    tid: str,
    candidate_id: UUID,
    current_user: AuthUser = Depends(get_current_auth_user),
    db: Session = Depends(get_db),
) -> ContentTopicReviewResponse:
    return reject_content_topic_candidate_response_for_creator(
        tid=tid,
        candidate_id=candidate_id,
        creator_id=current_user.creator_id,
        db=db,
    )


@router.post(
    "/{tid}/authoritative-evidence/promote",
    response_model=ContentTopicReviewResponse,
)
def promote_content_authoritative_evidence(
    tid: str,
    current_user: AuthUser = Depends(get_current_auth_user),
    db: Session = Depends(get_db),
) -> ContentTopicReviewResponse:
    return promote_content_authoritative_evidence_response_for_creator(
        tid=tid,
        creator_id=current_user.creator_id,
        db=db,
    )


@router.get("/{tid}", response_model=ContentResponse)
def get_content_detail(
    tid: str,
    current_user: AuthUser = Depends(get_current_auth_user),
    db: Session = Depends(get_db),
) -> ContentResponse:
    content = get_content_response_for_creator_by_tid(
        tid=tid,
        creator_id=current_user.creator_id,
        db=db,
    )
    if content is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="content not found",
        )

    return content
