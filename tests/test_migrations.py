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
    try:
        with engine.connect() as conn:
            inspector = inspect(conn)
            assert "invoices" in inspector.get_table_names(schema="public")
            assert "bookings" in inspector.get_table_names(schema="public")
            assert "content" in inspector.get_table_names(schema="public")
            assert "booking_links" in inspector.get_table_names(schema="public")
            booking_link_columns = {
                column["name"] for column in inspector.get_columns("booking_links")
            }
            assert "billing_amount_cents" in booking_link_columns
            assert "billing_currency" in booking_link_columns

        command.downgrade(cfg, "-1")
        with engine.connect() as conn:
            inspector = inspect(conn)
            table_names = inspector.get_table_names(schema="public")
            assert "invoices" not in table_names
            assert "bookings" in table_names
            assert "content" in table_names
            assert "booking_links" in table_names
            booking_link_columns = {
                column["name"] for column in inspector.get_columns("booking_links")
            }
            assert "billing_amount_cents" in booking_link_columns
            assert "billing_currency" in booking_link_columns

        command.downgrade(cfg, "-1")
        with engine.connect() as conn:
            inspector = inspect(conn)
            table_names = inspector.get_table_names(schema="public")
            assert "bookings" in table_names
            assert "content" in table_names
            assert "booking_links" in table_names
            booking_link_columns = {
                column["name"] for column in inspector.get_columns("booking_links")
            }
            assert "billing_amount_cents" not in booking_link_columns
            assert "billing_currency" not in booking_link_columns

        command.downgrade(cfg, "-1")
        with engine.connect() as conn:
            inspector = inspect(conn)
            table_names = inspector.get_table_names(schema="public")
            assert "invoices" not in table_names
            assert "bookings" not in table_names
            assert "content" in table_names
            assert "booking_links" in table_names
    finally:
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
            "billing_amount_cents",
            "billing_currency",
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


def test_bookings_table_has_expected_columns_fk_indexes_and_unique_uuid():
    db_url = os.getenv("TEST_DATABASE_URL")
    engine = create_engine(db_url)

    with engine.connect() as conn:
        inspector = inspect(conn)
        columns = {column["name"] for column in inspector.get_columns("bookings")}
        assert columns == {
            "id",
            "creator_id",
            "tid",
            "booking_link_id",
            "calendly_booking_uuid",
            "email",
            "status",
            "booked_at",
            "canceled_at",
        }

        foreign_keys = inspector.get_foreign_keys("bookings")
        assert any(
            fk["referred_table"] == "creators"
            and fk["constrained_columns"] == ["creator_id"]
            for fk in foreign_keys
        )
        assert any(
            fk["referred_table"] == "content"
            and fk["constrained_columns"] == ["tid"]
            and fk["referred_columns"] == ["tid"]
            for fk in foreign_keys
        )
        assert any(
            fk["referred_table"] == "booking_links"
            and fk["constrained_columns"] == ["booking_link_id"]
            for fk in foreign_keys
        )

        indexes = inspector.get_indexes("bookings")
        assert any(
            index["name"] == "ix_bookings_creator_id"
            and index["column_names"] == ["creator_id"]
            for index in indexes
        )
        assert any(
            index["name"] == "ix_bookings_booking_link_id"
            and index["column_names"] == ["booking_link_id"]
            for index in indexes
        )
        assert any(
            index["name"] == "ix_bookings_tid"
            and index["column_names"] == ["tid"]
            for index in indexes
        )

        unique_constraints = inspector.get_unique_constraints("bookings")
        assert any(
            constraint["name"] == "uq_bookings_calendly_booking_uuid"
            and constraint["column_names"] == ["calendly_booking_uuid"]
            for constraint in unique_constraints
        )


def test_invoices_table_has_expected_columns_fk_indexes_and_unique_constraints():
    db_url = os.getenv("TEST_DATABASE_URL")
    engine = create_engine(db_url)

    with engine.connect() as conn:
        inspector = inspect(conn)
        columns = {column["name"] for column in inspector.get_columns("invoices")}
        assert columns == {
            "id",
            "creator_id",
            "booking_id",
            "tid",
            "stripe_account_id",
            "stripe_invoice_id",
            "amount_cents",
            "currency",
            "status",
            "issued_at",
            "paid_at",
            "voided_at",
        }

        foreign_keys = inspector.get_foreign_keys("invoices")
        assert any(
            fk["referred_table"] == "creators"
            and fk["constrained_columns"] == ["creator_id"]
            for fk in foreign_keys
        )
        assert any(
            fk["referred_table"] == "bookings"
            and fk["constrained_columns"] == ["booking_id"]
            for fk in foreign_keys
        )
        assert any(
            fk["referred_table"] == "content"
            and fk["constrained_columns"] == ["tid"]
            and fk["referred_columns"] == ["tid"]
            for fk in foreign_keys
        )

        indexes = inspector.get_indexes("invoices")
        assert any(
            index["name"] == "ix_invoices_creator_id"
            and index["column_names"] == ["creator_id"]
            for index in indexes
        )
        assert any(
            index["name"] == "ix_invoices_tid"
            and index["column_names"] == ["tid"]
            for index in indexes
        )

        unique_constraints = inspector.get_unique_constraints("invoices")
        assert any(
            constraint["name"] == "uq_invoices_booking_id"
            and constraint["column_names"] == ["booking_id"]
            for constraint in unique_constraints
        )
        assert any(
            constraint["name"] == "uq_invoices_stripe_invoice_id"
            and constraint["column_names"] == ["stripe_invoice_id"]
            for constraint in unique_constraints
        )
