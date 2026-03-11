from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.content import Content
from app.models.content_confirmed_topic import ContentConfirmedTopic
from app.models.content_extraction_artifact import ContentExtractionArtifact
from app.models.content_fetch_snapshot import ContentFetchSnapshot
from app.models.content_topic_candidate import ContentTopicCandidate
from app.services.content_topics import (
    CONTENT_TOPIC_REVIEW_STATUS_CONFIRMED,
    CONTENT_TOPIC_REVIEW_STATUS_PENDING,
)

PROMOTION_BLOCK_REASON_ALREADY_AUTHORITATIVE = (
    "Latest extraction artifact is already authoritative."
)
PROMOTION_BLOCK_REASON_NO_CANDIDATES = (
    "Generate topic candidates before promoting current evidence."
)
PROMOTION_BLOCK_REASON_PENDING_CANDIDATES = (
    "Resolve all pending topic candidates before promoting current evidence."
)


@dataclass(frozen=True)
class AuthoritativeContentEvidence:
    artifact: ContentExtractionArtifact
    fetch_snapshot: ContentFetchSnapshot
    confirmed_topics: list[ContentConfirmedTopic]


@dataclass(frozen=True)
class ContentAuthorityState:
    authoritative_extraction_artifact_id: UUID | None
    authoritative_fetch_snapshot_id: UUID | None
    is_current_artifact_authoritative: bool
    promotion_allowed: bool
    promotion_block_reason: str | None
    review_confirmed_topics: list[ContentConfirmedTopic]
    authoritative_confirmed_topics: list[ContentConfirmedTopic]


def get_authoritative_content_evidence(
    *,
    content: Content,
    db: Session,
) -> AuthoritativeContentEvidence | None:
    if content.authoritative_extraction_artifact_id is None:
        return None

    artifact = db.execute(
        select(ContentExtractionArtifact).where(
            ContentExtractionArtifact.id == content.authoritative_extraction_artifact_id,
            ContentExtractionArtifact.content_id == content.id,
            ContentExtractionArtifact.creator_id == content.creator_id,
        )
    ).scalar_one_or_none()
    if artifact is None:
        return None

    fetch_snapshot = db.execute(
        select(ContentFetchSnapshot).where(
            ContentFetchSnapshot.id == artifact.fetch_snapshot_id,
            ContentFetchSnapshot.content_id == content.id,
            ContentFetchSnapshot.creator_id == content.creator_id,
        )
    ).scalar_one_or_none()
    if fetch_snapshot is None:
        return None

    return AuthoritativeContentEvidence(
        artifact=artifact,
        fetch_snapshot=fetch_snapshot,
        confirmed_topics=get_confirmed_topics_for_artifact(
            extraction_artifact_id=artifact.id,
            db=db,
        ),
    )


def get_confirmed_topics_for_artifact(
    *,
    extraction_artifact_id: UUID,
    db: Session,
) -> list[ContentConfirmedTopic]:
    confirmed_topic_ids = db.execute(
        select(ContentTopicCandidate.confirmed_topic_id)
        .where(
            ContentTopicCandidate.extraction_artifact_id == extraction_artifact_id,
            ContentTopicCandidate.review_status == CONTENT_TOPIC_REVIEW_STATUS_CONFIRMED,
            ContentTopicCandidate.confirmed_topic_id.is_not(None),
        )
        .order_by(
            ContentTopicCandidate.candidate_rank.asc(),
            ContentTopicCandidate.created_at.asc(),
            ContentTopicCandidate.id.asc(),
        )
    ).scalars().all()

    ordered_topic_ids: list[UUID] = []
    seen_topic_ids: set[UUID] = set()
    for topic_id in confirmed_topic_ids:
        if topic_id is None or topic_id in seen_topic_ids:
            continue
        seen_topic_ids.add(topic_id)
        ordered_topic_ids.append(topic_id)

    if not ordered_topic_ids:
        return []

    topic_rows = db.execute(
        select(ContentConfirmedTopic).where(ContentConfirmedTopic.id.in_(ordered_topic_ids))
    ).scalars().all()
    topics_by_id = {topic.id: topic for topic in topic_rows}
    return [topics_by_id[topic_id] for topic_id in ordered_topic_ids if topic_id in topics_by_id]


def build_content_authority_state(
    *,
    content: Content,
    artifact: ContentExtractionArtifact,
    candidate_topics: list[ContentTopicCandidate],
    db: Session,
) -> ContentAuthorityState:
    review_confirmed_topics = get_confirmed_topics_for_artifact(
        extraction_artifact_id=artifact.id,
        db=db,
    )
    authoritative_evidence = get_authoritative_content_evidence(content=content, db=db)

    authoritative_artifact_id = (
        authoritative_evidence.artifact.id if authoritative_evidence is not None else None
    )
    authoritative_fetch_snapshot_id = (
        authoritative_evidence.fetch_snapshot.id if authoritative_evidence is not None else None
    )
    authoritative_confirmed_topics = (
        authoritative_evidence.confirmed_topics if authoritative_evidence is not None else []
    )
    is_current_artifact_authoritative = authoritative_artifact_id == artifact.id

    promotion_allowed = True
    promotion_block_reason: str | None = None
    if is_current_artifact_authoritative:
        promotion_allowed = False
        promotion_block_reason = PROMOTION_BLOCK_REASON_ALREADY_AUTHORITATIVE
    elif not candidate_topics:
        promotion_allowed = False
        promotion_block_reason = PROMOTION_BLOCK_REASON_NO_CANDIDATES
    elif any(
        candidate.review_status == CONTENT_TOPIC_REVIEW_STATUS_PENDING
        for candidate in candidate_topics
    ):
        promotion_allowed = False
        promotion_block_reason = PROMOTION_BLOCK_REASON_PENDING_CANDIDATES

    return ContentAuthorityState(
        authoritative_extraction_artifact_id=authoritative_artifact_id,
        authoritative_fetch_snapshot_id=authoritative_fetch_snapshot_id,
        is_current_artifact_authoritative=is_current_artifact_authoritative,
        promotion_allowed=promotion_allowed,
        promotion_block_reason=promotion_block_reason,
        review_confirmed_topics=review_confirmed_topics,
        authoritative_confirmed_topics=authoritative_confirmed_topics,
    )
