import os
from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models.booking_link import BookingLink
from app.models.content import Content
from app.models.content_fetch_snapshot import ContentFetchSnapshot
from app.models.creator import Creator


def test_content_can_store_success_and_failure_fetch_snapshots():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])

    with Session(engine) as session:
        creator = Creator(name="Content Fetch Creator")
        session.add(creator)
        session.flush()

        booking_link = BookingLink(
            creator_id=creator.id,
            name="Fetch Strategy Call",
            calendly_url="https://calendly.com/example/fetch-strategy-call",
        )
        session.add(booking_link)
        session.flush()

        content = Content(
            creator_id=creator.id,
            booking_link_id=booking_link.id,
            source_url="https://example.com/posts/fetch-strategy-breakdown",
            tid="tid_fetch_strategy_breakdown",
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
            snapshot_text="<html><body><h1>Strategy breakdown</h1></body></html>",
            fetched_at=datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc),
        )
        failure_snapshot = ContentFetchSnapshot(
            content_id=content.id,
            creator_id=creator.id,
            requested_url="https://example.com/posts/missing-story",
            fetched_url="https://example.com/posts/missing-story",
            fetch_status="failed",
            http_status=404,
            failure_reason_code="HTTP_ERROR",
            failure_detail="Fetch returned HTTP 404.",
            response_content_type="text/html",
            snapshot_text=None,
            fetched_at=datetime(2026, 3, 10, 12, 5, tzinfo=timezone.utc),
        )
        session.add_all([success_snapshot, failure_snapshot])
        session.commit()

        snapshots = session.scalars(
            select(ContentFetchSnapshot)
            .where(ContentFetchSnapshot.content_id == content.id)
            .order_by(ContentFetchSnapshot.fetched_at.asc(), ContentFetchSnapshot.id.asc())
        ).all()

        assert [(row.fetch_status, row.http_status, row.failure_reason_code) for row in snapshots] == [
            ("succeeded", 200, None),
            ("failed", 404, "HTTP_ERROR"),
        ]
        assert snapshots[0].snapshot_text == "<html><body><h1>Strategy breakdown</h1></body></html>"
        assert snapshots[1].snapshot_text is None
        assert snapshots[0].content is not None
        assert snapshots[0].content.tid == content.tid
        assert snapshots[0].creator is not None
        assert snapshots[0].creator.id == creator.id
        assert {row.id for row in content.fetch_snapshots} == {
            success_snapshot.id,
            failure_snapshot.id,
        }
