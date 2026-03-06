import os
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

def test_migrations_upgrade_and_downgrade():
    db_url = os.getenv("TEST_DATABASE_URL")
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", db_url)

    command.upgrade(cfg, "head")
    engine = create_engine(db_url)
    with engine.connect() as conn:
        inspector = inspect(conn)
        assert "content" in inspector.get_table_names(schema="public")
        assert "booking_links" in inspector.get_table_names(schema="public")

    command.downgrade(cfg, "-1")
    with engine.connect() as conn:
        inspector = inspect(conn)
        table_names = inspector.get_table_names(schema="public")
        assert "content" not in table_names
        assert "booking_links" in table_names

    command.upgrade(cfg, "head")


def test_booking_links_table_has_expected_columns_fk_and_index():
    db_url = os.getenv("TEST_DATABASE_URL")
    engine = create_engine(db_url)

    with engine.connect() as conn:
        inspector = inspect(conn)
        columns = {column["name"] for column in inspector.get_columns("booking_links")}
        assert columns == {
            "id",
            "creator_id",
            "name",
            "calendly_url",
            "created_at",
            "updated_at",
        }

        foreign_keys = inspector.get_foreign_keys("booking_links")
        assert any(
            fk["referred_table"] == "creators"
            and fk["constrained_columns"] == ["creator_id"]
            for fk in foreign_keys
        )

        indexes = inspector.get_indexes("booking_links")
        assert any(
            index["name"] == "ix_booking_links_creator_id"
            and index["column_names"] == ["creator_id"]
            for index in indexes
        )


def test_content_table_has_expected_columns_fk_indexes_and_unique_tid():
    db_url = os.getenv("TEST_DATABASE_URL")
    engine = create_engine(db_url)

    with engine.connect() as conn:
        inspector = inspect(conn)
        columns = {column["name"] for column in inspector.get_columns("content")}
        assert columns == {
            "id",
            "creator_id",
            "booking_link_id",
            "source_url",
            "tid",
            "created_at",
            "updated_at",
        }

        foreign_keys = inspector.get_foreign_keys("content")
        assert any(
            fk["referred_table"] == "creators"
            and fk["constrained_columns"] == ["creator_id"]
            for fk in foreign_keys
        )
        assert any(
            fk["referred_table"] == "booking_links"
            and fk["constrained_columns"] == ["booking_link_id"]
            for fk in foreign_keys
        )

        indexes = inspector.get_indexes("content")
        assert any(
            index["name"] == "ix_content_creator_id"
            and index["column_names"] == ["creator_id"]
            for index in indexes
        )
        assert any(
            index["name"] == "ix_content_booking_link_id"
            and index["column_names"] == ["booking_link_id"]
            for index in indexes
        )

        unique_constraints = inspector.get_unique_constraints("content")
        assert any(
            constraint["name"] == "uq_content_tid"
            and constraint["column_names"] == ["tid"]
            for constraint in unique_constraints
        )
