import os
from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models.booking_link import BookingLink
from app.models.content import Content
from app.models.content_extraction_artifact import ContentExtractionArtifact
from app.models.content_fetch_snapshot import ContentFetchSnapshot
from app.models.creator import Creator


def test_content_can_store_extraction_artifacts_linked_to_fetch_snapshots():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])

    with Session(engine) as session:
        creator = Creator(name="Content Extraction Creator")
        session.add(creator)
        session.flush()

        booking_link = BookingLink(
            creator_id=creator.id,
            name="Extraction Strategy Call",
            calendly_url="https://calendly.com/example/extraction-strategy-call",
        )
        session.add(booking_link)
        session.flush()

        content = Content(
            creator_id=creator.id,
            booking_link_id=booking_link.id,
            source_url="https://example.com/posts/extraction-strategy-breakdown",
            tid="tid_extraction_strategy_breakdown",
        )
        session.add(content)
        session.flush()

        success_snapshot = ContentFetchSnapshot(
            content_id=content.id,
            creator_id=creator.id,
            requested_url=content.source_url,
            fetched_url=content.source_url,
            fetch_status="succeeded",
            http_status=200,
            response_content_type="text/html",
            response_content_charset="utf-8",
            snapshot_text=(
                "<html><body><article><p>"
                "This longer extraction text stays comfortably above the low-confidence threshold so later "
                "topic review can rely on it as a canonical artifact without re-reading the provider page."
                "</p></article></body></html>"
            ),
            fetched_at=datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc),
        )
        short_snapshot = ContentFetchSnapshot(
            content_id=content.id,
            creator_id=creator.id,
            requested_url=content.source_url,
            fetched_url=content.source_url,
            fetch_status="succeeded",
            http_status=200,
            response_content_type="text/html",
            response_content_charset="utf-8",
            snapshot_text="<html><body><article><p>Short note.</p></article></body></html>",
            fetched_at=datetime(2026, 3, 10, 12, 5, tzinfo=timezone.utc),
        )
        session.add_all([success_snapshot, short_snapshot])
        session.flush()

        success_artifact = ContentExtractionArtifact(
            content_id=content.id,
            creator_id=creator.id,
            fetch_snapshot_id=success_snapshot.id,
            extraction_status="succeeded",
            extraction_method="html_article",
            title="Extraction Strategy Breakdown",
            source_text_char_count=217,
            extracted_text_char_count=213,
            extracted_text_word_count=35,
            extracted_text=(
                "This longer extraction text stays comfortably above the low-confidence threshold so later "
                "topic review can rely on it as a canonical artifact without re-reading the provider page or "
                "guessing which source snapshot produced the extracted text."
            ),
            created_at=datetime(2026, 3, 10, 12, 1, tzinfo=timezone.utc),
        )
        short_artifact = ContentExtractionArtifact(
            content_id=content.id,
            creator_id=creator.id,
            fetch_snapshot_id=short_snapshot.id,
            extraction_status="low_confidence",
            extraction_reason_code="TEXT_TOO_SHORT",
            extraction_detail="Extracted text was too short to trust as a clean content artifact (2 words).",
            extraction_method="html_article",
            source_text_char_count=57,
            extracted_text_char_count=11,
            extracted_text_word_count=2,
            extracted_text="Short note.",
            created_at=datetime(2026, 3, 10, 12, 6, tzinfo=timezone.utc),
        )
        session.add_all([success_artifact, short_artifact])
        session.commit()

        artifacts = session.scalars(
            select(ContentExtractionArtifact)
            .where(ContentExtractionArtifact.content_id == content.id)
            .order_by(ContentExtractionArtifact.created_at.asc(), ContentExtractionArtifact.id.asc())
        ).all()

        assert [
            (artifact.extraction_status, artifact.fetch_snapshot_id) for artifact in artifacts
        ] == [
            ("succeeded", success_snapshot.id),
            ("low_confidence", short_snapshot.id),
        ]
        assert artifacts[0].content is not None
        assert artifacts[0].content.tid == content.tid
        assert artifacts[0].creator is not None
        assert artifacts[0].creator.id == creator.id
        assert artifacts[0].fetch_snapshot is not None
        assert artifacts[0].fetch_snapshot.id == success_snapshot.id
        assert success_snapshot.extraction_artifact is not None
        assert success_snapshot.extraction_artifact.id == success_artifact.id
        assert short_snapshot.extraction_artifact is not None
        assert short_snapshot.extraction_artifact.id == short_artifact.id
        assert {artifact.id for artifact in content.extraction_artifacts} == {
            success_artifact.id,
            short_artifact.id,
        }
        assert {artifact.id for artifact in creator.content_extraction_artifacts} == {
            success_artifact.id,
            short_artifact.id,
        }
