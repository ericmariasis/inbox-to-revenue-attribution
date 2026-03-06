import os

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models.booking_link import BookingLink
from app.models.content import Content
from app.models.creator import Creator


def test_creator_can_own_multiple_content_rows_for_owned_booking_links():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])

    with Session(engine) as session:
        creator = Creator(name="Content Creator")
        session.add(creator)
        session.flush()

        booking_links = [
            BookingLink(
                creator_id=creator.id,
                name="Strategy Call",
                calendly_url="https://calendly.com/example/strategy-call",
            ),
            BookingLink(
                creator_id=creator.id,
                name="Audit Call",
                calendly_url="https://calendly.com/example/audit-call",
            ),
        ]
        session.add_all(booking_links)
        session.flush()

        session.add_all(
            [
                Content(
                    creator_id=creator.id,
                    booking_link_id=booking_links[0].id,
                    source_url="https://example.com/posts/strategy-breakdown",
                    tid="tid_strategy_breakdown",
                ),
                Content(
                    creator_id=creator.id,
                    booking_link_id=booking_links[1].id,
                    source_url="https://example.com/posts/audit-breakdown",
                    tid="tid_audit_breakdown",
                ),
            ]
        )
        session.commit()

        rows = session.scalars(
            select(Content)
            .where(Content.creator_id == creator.id)
            .order_by(Content.tid)
        ).all()

        assert [(row.tid, row.source_url) for row in rows] == [
            ("tid_audit_breakdown", "https://example.com/posts/audit-breakdown"),
            ("tid_strategy_breakdown", "https://example.com/posts/strategy-breakdown"),
        ]
        assert {row.booking_link_id for row in rows} == {booking_links[0].id, booking_links[1].id}
        assert all(row.created_at is not None and row.updated_at is not None for row in rows)

        fetched = session.scalar(select(Content).where(Content.tid == "tid_strategy_breakdown"))
        assert fetched is not None
        assert fetched.creator_id == creator.id
        assert fetched.booking_link_id == booking_links[0].id
