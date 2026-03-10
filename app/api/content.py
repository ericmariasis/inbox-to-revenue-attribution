import logging
import uuid
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_auth_user
from app.core.config import get_settings
from app.db.session import get_db
from app.models.auth_user import AuthUser
from app.models.booking_link import BookingLink
from app.models.content import Content
from app.models.content_fetch_snapshot import ContentFetchSnapshot
from app.schemas.content import ContentCreateRequest, ContentFetchSnapshotResponse, ContentResponse
from app.services.content_fetch import (
    ContentFetchProvider,
    ContentFetchSuccess,
    build_default_content_fetch_provider,
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


def _content_fetch_provider(request: Request) -> ContentFetchProvider:
    return getattr(request.app.state, "content_fetch_provider", build_default_content_fetch_provider())


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
