import os
import uuid
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def test_migrations_upgrade_and_downgrade():
    db_url = os.getenv("TEST_DATABASE_URL")
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", db_url)

    command.upgrade(cfg, "head")
    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            inspector = inspect(conn)
            table_names = inspector.get_table_names(schema="public")
            creator_columns = {column["name"] for column in inspector.get_columns("creators")}
            booking_columns = {column["name"] for column in inspector.get_columns("bookings")}
            content_columns = {column["name"] for column in inspector.get_columns("content")}
            invoice_columns = {column["name"] for column in inspector.get_columns("invoices")}
            payment_event_columns = {
                column["name"] for column in inspector.get_columns("invoice_payment_events")
            }
            calendly_columns = {
                column["name"] for column in inspector.get_columns("calendly_webhook_events")
            }
            fullscope_columns = {
                column["name"] for column in inspector.get_columns("fullscope_webhook_events")
            }
            assert "support_requests" in table_names
            assert "shared_rate_limit_events" in table_names
            assert "pending_magic_link_issuances" in table_names
            assert "creator_experiment_run_cards" in table_names
            assert "creator_experiment_runs" in table_names
            assert "creator_claim_paid_evidence_refs" in table_names
            assert "creator_claim_snapshots" in table_names
            assert "calendly_webhook_events" in table_names
            assert "fullscope_webhook_events" in table_names
            assert "content_topic_candidates" in table_names
            assert "content_confirmed_topics" in table_names
            assert "content_extraction_artifacts" in table_names
            assert "content_fetch_snapshots" in table_names
            assert "blocked_billing_cases" in table_names
            assert "invoice_payment_events" in table_names
            assert "invoices" in table_names
            assert "bookings" in table_names
            assert "content" in table_names
            assert "booking_links" in table_names
            assert "billing_provider" in creator_columns
            assert "billing_connect_status" in creator_columns
            assert "billing_connected_at" in creator_columns
            assert "billing_account_id" in creator_columns
            assert "payment_provider" in invoice_columns
            assert "provider_account_id" in invoice_columns
            assert "provider_invoice_id" in invoice_columns
            assert "payment_provider" in payment_event_columns
            assert "provider_event_id" in payment_event_columns
            assert "provider_event_type" in payment_event_columns
            assert "provider_account_id" in payment_event_columns
            assert "provider_invoice_id" in payment_event_columns
            assert "authoritative_extraction_artifact_id" in content_columns
            assert "attribution_status" in booking_columns
            assert "unattributed_reason" in booking_columns
            assert "reducer_key" in calendly_columns
            assert "reducer_attempt_count" in calendly_columns
            assert "payload_sha256" in fullscope_columns
            assert "frozen_billing_amount_cents" in booking_columns
            assert "frozen_billing_currency" in booking_columns
            assert "provider" in booking_columns
            assert "provider_booking_id" in booking_columns
            booking_link_columns = {
                column["name"] for column in inspector.get_columns("booking_links")
            }
            assert "billing_amount_cents" in booking_link_columns
            assert "billing_currency" in booking_link_columns
            assert "provider" in booking_link_columns
            assert "destination_url" in booking_link_columns

        command.downgrade(cfg, "-1")
        with engine.connect() as conn:
            inspector = inspect(conn)
            table_names = inspector.get_table_names(schema="public")
            creator_columns = {column["name"] for column in inspector.get_columns("creators")}
            booking_columns = {column["name"] for column in inspector.get_columns("bookings")}
            content_columns = {column["name"] for column in inspector.get_columns("content")}
            invoice_columns = {column["name"] for column in inspector.get_columns("invoices")}
            payment_event_columns = {
                column["name"] for column in inspector.get_columns("invoice_payment_events")
            }
            calendly_columns = {
                column["name"] for column in inspector.get_columns("calendly_webhook_events")
            }
            assert "support_requests" in table_names
            assert "shared_rate_limit_events" in table_names
            assert "pending_magic_link_issuances" in table_names
            assert "creator_experiment_run_cards" in table_names
            assert "creator_experiment_runs" in table_names
            assert "creator_claim_paid_evidence_refs" in table_names
            assert "creator_claim_snapshots" in table_names
            assert "calendly_webhook_events" in table_names
            assert "fullscope_webhook_events" in table_names
            assert "content_topic_candidates" in table_names
            assert "content_confirmed_topics" in table_names
            assert "content_extraction_artifacts" in table_names
            assert "content_fetch_snapshots" in table_names
            assert "blocked_billing_cases" in table_names
            assert "invoice_payment_events" in table_names
            assert "invoices" in table_names
            assert "bookings" in table_names
            assert "content" in table_names
            assert "booking_links" in table_names
            assert "billing_provider" in creator_columns
            assert "billing_connect_status" in creator_columns
            assert "billing_connected_at" in creator_columns
            assert "billing_account_id" in creator_columns
            assert "payment_provider" in invoice_columns
            assert "provider_account_id" in invoice_columns
            assert "provider_invoice_id" in invoice_columns
            assert "payment_provider" not in payment_event_columns
            assert "provider_event_id" not in payment_event_columns
            assert "provider_event_type" not in payment_event_columns
            assert "provider_account_id" not in payment_event_columns
            assert "provider_invoice_id" not in payment_event_columns
            assert "authoritative_extraction_artifact_id" in content_columns
            assert "attribution_status" in booking_columns
            assert "unattributed_reason" in booking_columns
            assert "reducer_key" in calendly_columns
            assert "reducer_attempt_count" in calendly_columns
            assert "frozen_billing_amount_cents" in booking_columns
            assert "frozen_billing_currency" in booking_columns
            assert "provider" in booking_columns
            assert "provider_booking_id" in booking_columns
            booking_link_columns = {
                column["name"] for column in inspector.get_columns("booking_links")
            }
            assert "billing_amount_cents" in booking_link_columns
            assert "billing_currency" in booking_link_columns
            assert "provider" in booking_link_columns
            assert "destination_url" in booking_link_columns

        command.downgrade(cfg, "-1")
        with engine.connect() as conn:
            inspector = inspect(conn)
            table_names = inspector.get_table_names(schema="public")
            creator_columns = {column["name"] for column in inspector.get_columns("creators")}
            booking_columns = {column["name"] for column in inspector.get_columns("bookings")}
            content_columns = {column["name"] for column in inspector.get_columns("content")}
            invoice_columns = {column["name"] for column in inspector.get_columns("invoices")}
            payment_event_columns = {
                column["name"] for column in inspector.get_columns("invoice_payment_events")
            }
            calendly_columns = {
                column["name"] for column in inspector.get_columns("calendly_webhook_events")
            }
            assert "support_requests" in table_names
            assert "shared_rate_limit_events" in table_names
            assert "pending_magic_link_issuances" in table_names
            assert "creator_experiment_run_cards" in table_names
            assert "creator_experiment_runs" in table_names
            assert "creator_claim_paid_evidence_refs" in table_names
            assert "creator_claim_snapshots" in table_names
            assert "calendly_webhook_events" in table_names
            assert "fullscope_webhook_events" in table_names
            assert "content_topic_candidates" in table_names
            assert "content_confirmed_topics" in table_names
            assert "content_extraction_artifacts" in table_names
            assert "content_fetch_snapshots" in table_names
            assert "blocked_billing_cases" in table_names
            assert "invoice_payment_events" in table_names
            assert "invoices" in table_names
            assert "bookings" in table_names
            assert "content" in table_names
            assert "booking_links" in table_names
            assert "billing_provider" in creator_columns
            assert "billing_connect_status" in creator_columns
            assert "billing_connected_at" in creator_columns
            assert "billing_account_id" in creator_columns
            assert "payment_provider" not in invoice_columns
            assert "provider_account_id" not in invoice_columns
            assert "provider_invoice_id" not in invoice_columns
            assert "payment_provider" not in payment_event_columns
            assert "provider_event_id" not in payment_event_columns
            assert "provider_event_type" not in payment_event_columns
            assert "provider_account_id" not in payment_event_columns
            assert "provider_invoice_id" not in payment_event_columns
            assert "authoritative_extraction_artifact_id" in content_columns
            assert "attribution_status" in booking_columns
            assert "unattributed_reason" in booking_columns
            assert "reducer_key" in calendly_columns
            assert "reducer_attempt_count" in calendly_columns
            assert "frozen_billing_amount_cents" in booking_columns
            assert "frozen_billing_currency" in booking_columns
            assert "provider" in booking_columns
            assert "provider_booking_id" in booking_columns
            booking_link_columns = {
                column["name"] for column in inspector.get_columns("booking_links")
            }
            assert "billing_amount_cents" in booking_link_columns
            assert "billing_currency" in booking_link_columns
            assert "provider" in booking_link_columns
            assert "destination_url" in booking_link_columns

        command.downgrade(cfg, "-1")
        with engine.connect() as conn:
            inspector = inspect(conn)
            table_names = inspector.get_table_names(schema="public")
            creator_columns = {column["name"] for column in inspector.get_columns("creators")}
            booking_columns = {column["name"] for column in inspector.get_columns("bookings")}
            content_columns = {column["name"] for column in inspector.get_columns("content")}
            calendly_columns = {
                column["name"] for column in inspector.get_columns("calendly_webhook_events")
            }
            assert "support_requests" in table_names
            assert "shared_rate_limit_events" in table_names
            assert "pending_magic_link_issuances" in table_names
            assert "creator_experiment_run_cards" in table_names
            assert "creator_experiment_runs" in table_names
            assert "creator_claim_paid_evidence_refs" in table_names
            assert "creator_claim_snapshots" in table_names
            assert "calendly_webhook_events" in table_names
            assert "fullscope_webhook_events" in table_names
            assert "content_topic_candidates" in table_names
            assert "content_confirmed_topics" in table_names
            assert "content_extraction_artifacts" in table_names
            assert "content_fetch_snapshots" in table_names
            assert "blocked_billing_cases" in table_names
            assert "invoice_payment_events" in table_names
            assert "invoices" in table_names
            assert "bookings" in table_names
            assert "content" in table_names
            assert "booking_links" in table_names
            assert "billing_provider" not in creator_columns
            assert "billing_connect_status" not in creator_columns
            assert "billing_connected_at" not in creator_columns
            assert "billing_account_id" not in creator_columns
            assert "authoritative_extraction_artifact_id" in content_columns
            assert "attribution_status" in booking_columns
            assert "unattributed_reason" in booking_columns
            assert "reducer_key" in calendly_columns
            assert "reducer_attempt_count" in calendly_columns
            assert "frozen_billing_amount_cents" in booking_columns
            assert "frozen_billing_currency" in booking_columns
            assert "provider" in booking_columns
            assert "provider_booking_id" in booking_columns
            booking_link_columns = {
                column["name"] for column in inspector.get_columns("booking_links")
            }
            assert "billing_amount_cents" in booking_link_columns
            assert "billing_currency" in booking_link_columns
            assert "provider" in booking_link_columns
            assert "destination_url" in booking_link_columns

        command.downgrade(cfg, "-1")
        with engine.connect() as conn:
            inspector = inspect(conn)
            table_names = inspector.get_table_names(schema="public")
            booking_columns = {column["name"] for column in inspector.get_columns("bookings")}
            content_columns = {column["name"] for column in inspector.get_columns("content")}
            calendly_columns = {
                column["name"] for column in inspector.get_columns("calendly_webhook_events")
            }
            assert "support_requests" in table_names
            assert "shared_rate_limit_events" in table_names
            assert "pending_magic_link_issuances" in table_names
            assert "creator_experiment_run_cards" in table_names
            assert "creator_experiment_runs" in table_names
            assert "creator_claim_paid_evidence_refs" in table_names
            assert "creator_claim_snapshots" in table_names
            assert "calendly_webhook_events" in table_names
            assert "fullscope_webhook_events" in table_names
            assert "content_topic_candidates" in table_names
            assert "content_confirmed_topics" in table_names
            assert "content_extraction_artifacts" in table_names
            assert "content_fetch_snapshots" in table_names
            assert "blocked_billing_cases" in table_names
            assert "invoice_payment_events" in table_names
            assert "invoices" in table_names
            assert "bookings" in table_names
            assert "content" in table_names
            assert "booking_links" in table_names
            assert "authoritative_extraction_artifact_id" in content_columns
            assert "attribution_status" in booking_columns
            assert "unattributed_reason" in booking_columns
            assert "reducer_key" in calendly_columns
            assert "reducer_attempt_count" in calendly_columns
            assert "frozen_billing_amount_cents" in booking_columns
            assert "frozen_billing_currency" in booking_columns
            booking_link_columns = {
                column["name"] for column in inspector.get_columns("booking_links")
            }
            assert "billing_amount_cents" in booking_link_columns
            assert "billing_currency" in booking_link_columns

        command.downgrade(cfg, "-1")
        with engine.connect() as conn:
            inspector = inspect(conn)
            table_names = inspector.get_table_names(schema="public")
            booking_columns = {column["name"] for column in inspector.get_columns("bookings")}
            content_columns = {column["name"] for column in inspector.get_columns("content")}
            calendly_columns = {
                column["name"] for column in inspector.get_columns("calendly_webhook_events")
            }
            assert "support_requests" in table_names
            assert "shared_rate_limit_events" in table_names
            assert "pending_magic_link_issuances" in table_names
            assert "creator_experiment_run_cards" in table_names
            assert "creator_experiment_runs" in table_names
            assert "creator_claim_paid_evidence_refs" in table_names
            assert "creator_claim_snapshots" in table_names
            assert "calendly_webhook_events" in table_names
            assert "content_topic_candidates" in table_names
            assert "content_confirmed_topics" in table_names
            assert "content_extraction_artifacts" in table_names
            assert "content_fetch_snapshots" in table_names
            assert "blocked_billing_cases" in table_names
            assert "invoice_payment_events" in table_names
            assert "invoices" in table_names
            assert "bookings" in table_names
            assert "content" in table_names
            assert "booking_links" in table_names
            assert "authoritative_extraction_artifact_id" in content_columns
            assert "attribution_status" in booking_columns
            assert "unattributed_reason" in booking_columns
            assert "reducer_key" in calendly_columns
            assert "reducer_attempt_count" in calendly_columns
            assert "frozen_billing_amount_cents" in booking_columns
            assert "frozen_billing_currency" in booking_columns
            booking_link_columns = {
                column["name"] for column in inspector.get_columns("booking_links")
            }
            assert "billing_amount_cents" in booking_link_columns
            assert "billing_currency" in booking_link_columns

        command.downgrade(cfg, "-1")
        with engine.connect() as conn:
            inspector = inspect(conn)
            table_names = inspector.get_table_names(schema="public")
            booking_columns = {column["name"] for column in inspector.get_columns("bookings")}
            content_columns = {column["name"] for column in inspector.get_columns("content")}
            calendly_columns = {
                column["name"] for column in inspector.get_columns("calendly_webhook_events")
            }
            assert "support_requests" in table_names
            assert "shared_rate_limit_events" in table_names
            assert "pending_magic_link_issuances" in table_names
            assert "creator_experiment_run_cards" in table_names
            assert "creator_experiment_runs" in table_names
            assert "creator_claim_paid_evidence_refs" in table_names
            assert "creator_claim_snapshots" in table_names
            assert "calendly_webhook_events" in table_names
            assert "content_topic_candidates" in table_names
            assert "content_confirmed_topics" in table_names
            assert "content_extraction_artifacts" in table_names
            assert "content_fetch_snapshots" in table_names
            assert "blocked_billing_cases" in table_names
            assert "invoice_payment_events" in table_names
            assert "invoices" in table_names
            assert "bookings" in table_names
            assert "content" in table_names
            assert "booking_links" in table_names
            assert "authoritative_extraction_artifact_id" in content_columns
            assert "attribution_status" in booking_columns
            assert "unattributed_reason" in booking_columns
            assert "reducer_key" in calendly_columns
            assert "reducer_attempt_count" in calendly_columns
            assert "frozen_billing_amount_cents" in booking_columns
            assert "frozen_billing_currency" in booking_columns
            booking_link_columns = {
                column["name"] for column in inspector.get_columns("booking_links")
            }
            assert "billing_amount_cents" in booking_link_columns
            assert "billing_currency" in booking_link_columns

        command.downgrade(cfg, "-1")
        with engine.connect() as conn:
            inspector = inspect(conn)
            table_names = inspector.get_table_names(schema="public")
            booking_columns = {column["name"] for column in inspector.get_columns("bookings")}
            content_columns = {column["name"] for column in inspector.get_columns("content")}
            calendly_columns = {
                column["name"] for column in inspector.get_columns("calendly_webhook_events")
            }
            assert "support_requests" in table_names
            assert "creator_experiment_run_cards" in table_names
            assert "creator_experiment_runs" in table_names
            assert "creator_claim_paid_evidence_refs" in table_names
            assert "creator_claim_snapshots" in table_names
            assert "calendly_webhook_events" in table_names
            assert "content_topic_candidates" in table_names
            assert "content_confirmed_topics" in table_names
            assert "content_extraction_artifacts" in table_names
            assert "content_fetch_snapshots" in table_names
            assert "blocked_billing_cases" in table_names
            assert "invoice_payment_events" in table_names
            assert "invoices" in table_names
            assert "bookings" in table_names
            assert "content" in table_names
            assert "booking_links" in table_names
            assert "authoritative_extraction_artifact_id" in content_columns
            assert "attribution_status" in booking_columns
            assert "unattributed_reason" in booking_columns
            assert "reducer_key" in calendly_columns
            assert "reducer_attempt_count" in calendly_columns
            assert "frozen_billing_amount_cents" in booking_columns
            assert "frozen_billing_currency" in booking_columns
            booking_link_columns = {
                column["name"] for column in inspector.get_columns("booking_links")
            }
            assert "billing_amount_cents" in booking_link_columns
            assert "billing_currency" in booking_link_columns

        command.downgrade(cfg, "-1")
        with engine.connect() as conn:
            inspector = inspect(conn)
            table_names = inspector.get_table_names(schema="public")
            booking_columns = {column["name"] for column in inspector.get_columns("bookings")}
            content_columns = {column["name"] for column in inspector.get_columns("content")}
            calendly_columns = {
                column["name"] for column in inspector.get_columns("calendly_webhook_events")
            }
            assert "creator_experiment_run_cards" in table_names
            assert "creator_experiment_runs" in table_names
            assert "creator_claim_paid_evidence_refs" in table_names
            assert "creator_claim_snapshots" in table_names
            assert "calendly_webhook_events" in table_names
            assert "content_topic_candidates" in table_names
            assert "content_confirmed_topics" in table_names
            assert "content_extraction_artifacts" in table_names
            assert "content_fetch_snapshots" in table_names
            assert "blocked_billing_cases" in table_names
            assert "invoice_payment_events" in table_names
            assert "invoices" in table_names
            assert "bookings" in table_names
            assert "content" in table_names
            assert "booking_links" in table_names
            assert "authoritative_extraction_artifact_id" in content_columns
            assert "attribution_status" in booking_columns
            assert "unattributed_reason" in booking_columns
            assert "reducer_key" in calendly_columns
            assert "reducer_attempt_count" in calendly_columns
            assert "frozen_billing_amount_cents" in booking_columns
            assert "frozen_billing_currency" in booking_columns
            booking_link_columns = {
                column["name"] for column in inspector.get_columns("booking_links")
            }
            assert "billing_amount_cents" in booking_link_columns
            assert "billing_currency" in booking_link_columns

        command.downgrade(cfg, "-1")
        with engine.connect() as conn:
            inspector = inspect(conn)
            table_names = inspector.get_table_names(schema="public")
            booking_columns = {column["name"] for column in inspector.get_columns("bookings")}
            content_columns = {column["name"] for column in inspector.get_columns("content")}
            calendly_columns = {
                column["name"] for column in inspector.get_columns("calendly_webhook_events")
            }
            assert "creator_experiment_run_cards" in table_names
            assert "creator_experiment_runs" in table_names
            assert "creator_claim_paid_evidence_refs" in table_names
            assert "creator_claim_snapshots" in table_names
            assert "calendly_webhook_events" in table_names
            assert "content_topic_candidates" in table_names
            assert "content_confirmed_topics" in table_names
            assert "content_extraction_artifacts" in table_names
            assert "content_fetch_snapshots" in table_names
            assert "blocked_billing_cases" in table_names
            assert "invoice_payment_events" in table_names
            assert "invoices" in table_names
            assert "bookings" in table_names
            assert "content" in table_names
            assert "booking_links" in table_names
            assert "authoritative_extraction_artifact_id" in content_columns
            assert "attribution_status" in booking_columns
            assert "reducer_key" in calendly_columns
            assert "reducer_attempt_count" in calendly_columns
            assert "frozen_billing_amount_cents" in booking_columns
            assert "frozen_billing_currency" in booking_columns
            booking_link_columns = {
                column["name"] for column in inspector.get_columns("booking_links")
            }
            assert "billing_amount_cents" in booking_link_columns
            assert "billing_currency" in booking_link_columns

        command.downgrade(cfg, "-1")
        with engine.connect() as conn:
            inspector = inspect(conn)
            table_names = inspector.get_table_names(schema="public")
            booking_columns = {column["name"] for column in inspector.get_columns("bookings")}
            content_columns = {column["name"] for column in inspector.get_columns("content")}
            assert "creator_experiment_run_cards" in table_names
            assert "creator_experiment_runs" in table_names
            assert "creator_claim_paid_evidence_refs" in table_names
            assert "creator_claim_snapshots" in table_names
            assert "calendly_webhook_events" in table_names
            assert "content_topic_candidates" in table_names
            assert "content_confirmed_topics" in table_names
            assert "content_extraction_artifacts" in table_names
            assert "content_fetch_snapshots" in table_names
            assert "blocked_billing_cases" in table_names
            assert "invoice_payment_events" in table_names
            assert "invoices" in table_names
            assert "bookings" in table_names
            assert "content" in table_names
            assert "booking_links" in table_names
            assert "authoritative_extraction_artifact_id" in content_columns
            assert "attribution_status" in booking_columns
            assert "frozen_billing_amount_cents" in booking_columns
            assert "frozen_billing_currency" in booking_columns
            booking_link_columns = {
                column["name"] for column in inspector.get_columns("booking_links")
            }
            assert "billing_amount_cents" in booking_link_columns
            assert "billing_currency" in booking_link_columns

        command.downgrade(cfg, "-1")
        with engine.connect() as conn:
            inspector = inspect(conn)
            table_names = inspector.get_table_names(schema="public")
            booking_columns = {column["name"] for column in inspector.get_columns("bookings")}
            content_columns = {column["name"] for column in inspector.get_columns("content")}
            assert "creator_claim_paid_evidence_refs" in table_names
            assert "creator_claim_snapshots" in table_names
            assert "calendly_webhook_events" in table_names
            assert "authoritative_extraction_artifact_id" in content_columns
            assert "attribution_status" in booking_columns
            assert "content_topic_candidates" in table_names
            assert "content_confirmed_topics" in table_names
            assert "content_extraction_artifacts" in table_names
            assert "content_fetch_snapshots" in table_names
            assert "blocked_billing_cases" in table_names
            assert "invoice_payment_events" in table_names
            assert "invoices" in table_names
            assert "bookings" in table_names
            assert "content" in table_names
            assert "booking_links" in table_names
            assert "frozen_billing_amount_cents" in booking_columns
            assert "frozen_billing_currency" in booking_columns
            booking_link_columns = {
                column["name"] for column in inspector.get_columns("booking_links")
            }
            assert "billing_amount_cents" in booking_link_columns
            assert "billing_currency" in booking_link_columns

        command.downgrade(cfg, "-1")
        with engine.connect() as conn:
            inspector = inspect(conn)
            table_names = inspector.get_table_names(schema="public")
            booking_columns = {column["name"] for column in inspector.get_columns("bookings")}
            content_columns = {column["name"] for column in inspector.get_columns("content")}
            assert "content_topic_candidates" in table_names
            assert "content_confirmed_topics" in table_names
            assert "content_extraction_artifacts" in table_names
            assert "content_fetch_snapshots" in table_names
            assert "blocked_billing_cases" in table_names
            assert "invoice_payment_events" in table_names
            assert "invoices" in table_names
            assert "bookings" in table_names
            assert "content" in table_names
            assert "booking_links" in table_names
            assert "authoritative_extraction_artifact_id" in content_columns
            assert "attribution_status" in booking_columns
            assert "frozen_billing_amount_cents" in booking_columns
            assert "frozen_billing_currency" in booking_columns
            booking_link_columns = {
                column["name"] for column in inspector.get_columns("booking_links")
            }
            assert "billing_amount_cents" in booking_link_columns
            assert "billing_currency" in booking_link_columns

        command.downgrade(cfg, "-1")
        with engine.connect() as conn:
            inspector = inspect(conn)
            table_names = inspector.get_table_names(schema="public")
            booking_columns = {column["name"] for column in inspector.get_columns("bookings")}
            assert "content_topic_candidates" in table_names
            assert "content_confirmed_topics" in table_names
            assert "content_extraction_artifacts" in table_names
            assert "content_fetch_snapshots" in table_names
            assert "blocked_billing_cases" in table_names
            assert "invoice_payment_events" in table_names
            assert "invoices" in table_names
            assert "bookings" in table_names
            assert "content" in table_names
            assert "booking_links" in table_names
            assert "frozen_billing_amount_cents" in booking_columns
            assert "frozen_billing_currency" in booking_columns
            booking_link_columns = {
                column["name"] for column in inspector.get_columns("booking_links")
            }
            assert "billing_amount_cents" in booking_link_columns
            assert "billing_currency" in booking_link_columns

        command.downgrade(cfg, "-1")
        with engine.connect() as conn:
            inspector = inspect(conn)
            table_names = inspector.get_table_names(schema="public")
            assert "content_topic_candidates" in table_names
            assert "content_confirmed_topics" in table_names
            assert "content_extraction_artifacts" in table_names
            assert "content_fetch_snapshots" in table_names
            assert "blocked_billing_cases" in table_names
            assert "invoice_payment_events" in table_names
            assert "invoices" in table_names
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
            assert "content_extraction_artifacts" in table_names
            assert "content_fetch_snapshots" in table_names
            assert "blocked_billing_cases" in table_names
            assert "invoice_payment_events" in table_names
            assert "invoices" in table_names
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
            assert "content_fetch_snapshots" in table_names
            assert "blocked_billing_cases" in table_names
            assert "invoice_payment_events" in table_names
            assert "invoices" in table_names
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
            assert "blocked_billing_cases" in table_names
            assert "invoice_payment_events" in table_names
            assert "invoices" in table_names
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
            assert "invoice_payment_events" in table_names
            assert "invoices" in table_names
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
            assert "invoices" in table_names
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
            assert "invoices" in table_names
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
            assert "invoices" in table_names
            assert "bookings" in table_names
            assert "content" in table_names
            assert "booking_links" in table_names

        command.downgrade(cfg, "-1")
        with engine.connect() as conn:
            inspector = inspect(conn)
            table_names = inspector.get_table_names(schema="public")
            assert "invoices" in table_names
            assert "bookings" in table_names
            assert "content" in table_names
            assert "booking_links" in table_names

        command.downgrade(cfg, "-1")
        with engine.connect() as conn:
            inspector = inspect(conn)
            table_names = inspector.get_table_names(schema="public")
            assert "invoices" not in table_names
            assert "bookings" in table_names
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
            "provider",
            "destination_url",
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


def test_creators_table_has_expected_billing_provider_identity_columns():
    db_url = os.getenv("TEST_DATABASE_URL")
    engine = create_engine(db_url)

    with engine.connect() as conn:
        inspector = inspect(conn)
        columns = {column["name"] for column in inspector.get_columns("creators")}
        assert columns == {
            "id",
            "name",
            "billing_provider",
            "billing_connect_status",
            "billing_connected_at",
            "billing_account_id",
            "stripe_connect_status",
            "stripe_connected_at",
            "stripe_account_id",
            "created_at",
        }


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
            "authoritative_extraction_artifact_id",
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
        assert any(
            fk["referred_table"] == "content_extraction_artifacts"
            and fk["constrained_columns"] == ["authoritative_extraction_artifact_id"]
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


def test_content_fetch_snapshots_table_has_expected_columns_fk_and_indexes():
    db_url = os.getenv("TEST_DATABASE_URL")
    engine = create_engine(db_url)

    with engine.connect() as conn:
        inspector = inspect(conn)
        columns = {column["name"] for column in inspector.get_columns("content_fetch_snapshots")}
        assert columns == {
            "id",
            "content_id",
            "creator_id",
            "requested_url",
            "fetched_url",
            "fetch_status",
            "http_status",
            "failure_reason_code",
            "failure_detail",
            "response_content_type",
            "response_content_charset",
            "snapshot_text",
            "fetched_at",
        }

        foreign_keys = inspector.get_foreign_keys("content_fetch_snapshots")
        assert any(
            fk["referred_table"] == "content"
            and fk["constrained_columns"] == ["content_id"]
            for fk in foreign_keys
        )
        assert any(
            fk["referred_table"] == "creators"
            and fk["constrained_columns"] == ["creator_id"]
            for fk in foreign_keys
        )

        indexes = inspector.get_indexes("content_fetch_snapshots")
        assert any(
            index["name"] == "ix_content_fetch_snapshots_content_id"
            and index["column_names"] == ["content_id"]
            for index in indexes
        )
        assert any(
            index["name"] == "ix_content_fetch_snapshots_creator_id"
            and index["column_names"] == ["creator_id"]
            for index in indexes
        )
        assert any(
            index["name"] == "ix_content_fetch_snapshots_fetch_status"
            and index["column_names"] == ["fetch_status"]
            for index in indexes
        )


def test_content_extraction_artifacts_table_has_expected_columns_fk_indexes_and_unique_snapshot():
    db_url = os.getenv("TEST_DATABASE_URL")
    engine = create_engine(db_url)

    with engine.connect() as conn:
        inspector = inspect(conn)
        columns = {column["name"] for column in inspector.get_columns("content_extraction_artifacts")}
        assert columns == {
            "id",
            "content_id",
            "creator_id",
            "fetch_snapshot_id",
            "extraction_status",
            "extraction_reason_code",
            "extraction_detail",
            "extraction_method",
            "title",
            "published_at",
            "published_at_raw",
            "source_text_char_count",
            "extracted_text_char_count",
            "extracted_text_word_count",
            "extracted_text",
            "created_at",
        }

        foreign_keys = inspector.get_foreign_keys("content_extraction_artifacts")
        assert any(
            fk["referred_table"] == "content"
            and fk["constrained_columns"] == ["content_id"]
            for fk in foreign_keys
        )
        assert any(
            fk["referred_table"] == "creators"
            and fk["constrained_columns"] == ["creator_id"]
            for fk in foreign_keys
        )
        assert any(
            fk["referred_table"] == "content_fetch_snapshots"
            and fk["constrained_columns"] == ["fetch_snapshot_id"]
            for fk in foreign_keys
        )

        indexes = inspector.get_indexes("content_extraction_artifacts")
        assert any(
            index["name"] == "ix_content_extraction_artifacts_content_id"
            and index["column_names"] == ["content_id"]
            for index in indexes
        )
        assert any(
            index["name"] == "ix_content_extraction_artifacts_creator_id"
            and index["column_names"] == ["creator_id"]
            for index in indexes
        )
        assert any(
            index["name"] == "ix_content_extraction_artifacts_extraction_status"
            and index["column_names"] == ["extraction_status"]
            for index in indexes
        )

        unique_constraints = inspector.get_unique_constraints("content_extraction_artifacts")
        assert any(
            constraint["name"] == "uq_content_extraction_artifacts_fetch_snapshot_id"
            and constraint["column_names"] == ["fetch_snapshot_id"]
            for constraint in unique_constraints
        )


def test_creator_experiment_runs_table_has_expected_columns_fk_and_indexes():
    db_url = os.getenv("TEST_DATABASE_URL")
    engine = create_engine(db_url)

    with engine.connect() as conn:
        inspector = inspect(conn)
        columns = {column["name"] for column in inspector.get_columns("creator_experiment_runs")}
        assert columns == {
            "id",
            "creator_id",
            "status",
            "summary_text",
            "run_contract_version",
            "run_reducer_version",
            "run_prompt_version",
            "created_at",
        }

        foreign_keys = inspector.get_foreign_keys("creator_experiment_runs")
        assert any(
            fk["referred_table"] == "creators"
            and fk["constrained_columns"] == ["creator_id"]
            for fk in foreign_keys
        )

        indexes = inspector.get_indexes("creator_experiment_runs")
        assert any(
            index["name"] == "ix_creator_experiment_runs_creator_id"
            and index["column_names"] == ["creator_id"]
            for index in indexes
        )
        assert any(
            index["name"] == "ix_creator_experiment_runs_status"
            and index["column_names"] == ["status"]
            for index in indexes
        )
        assert any(
            index["name"] == "ix_creator_experiment_runs_created_at"
            and index["column_names"] == ["created_at"]
            for index in indexes
        )


def test_creator_experiment_run_cards_table_has_expected_columns_fk_indexes_and_unique_constraints():
    db_url = os.getenv("TEST_DATABASE_URL")
    engine = create_engine(db_url)

    with engine.connect() as conn:
        inspector = inspect(conn)
        columns = {column["name"] for column in inspector.get_columns("creator_experiment_run_cards")}
        assert columns == {
            "id",
            "run_id",
            "claim_snapshot_id",
            "content_tid",
            "title",
            "hypothesis",
            "why_this_might_work",
            "evidence_summary",
            "caution",
            "card_order",
        }

        foreign_keys = inspector.get_foreign_keys("creator_experiment_run_cards")
        assert any(
            fk["referred_table"] == "creator_experiment_runs"
            and fk["constrained_columns"] == ["run_id"]
            for fk in foreign_keys
        )
        assert any(
            fk["referred_table"] == "creator_claim_snapshots"
            and fk["constrained_columns"] == ["claim_snapshot_id"]
            for fk in foreign_keys
        )

        indexes = inspector.get_indexes("creator_experiment_run_cards")
        assert any(
            index["name"] == "ix_creator_experiment_run_cards_run_id"
            and index["column_names"] == ["run_id"]
            for index in indexes
        )
        assert any(
            index["name"] == "ix_creator_experiment_run_cards_claim_snapshot_id"
            and index["column_names"] == ["claim_snapshot_id"]
            for index in indexes
        )

        unique_constraints = inspector.get_unique_constraints("creator_experiment_run_cards")
        assert any(
            constraint["name"] == "uq_creator_experiment_run_cards_run_order"
            and constraint["column_names"] == ["run_id", "card_order"]
            for constraint in unique_constraints
        )
        assert any(
            constraint["name"] == "uq_creator_experiment_run_cards_run_claim_snapshot"
            and constraint["column_names"] == ["run_id", "claim_snapshot_id"]
            for constraint in unique_constraints
        )


def test_pending_magic_link_issuances_table_has_expected_columns_index_and_unique_constraint():
    db_url = os.getenv("TEST_DATABASE_URL")
    engine = create_engine(db_url)

    with engine.connect() as conn:
        inspector = inspect(conn)
        columns = {column["name"] for column in inspector.get_columns("pending_magic_link_issuances")}
        assert columns == {
            "id",
            "email",
            "token_hash",
            "expires_at",
            "used_at",
            "created_at",
        }

        foreign_keys = inspector.get_foreign_keys("pending_magic_link_issuances")
        assert foreign_keys == []

        indexes = inspector.get_indexes("pending_magic_link_issuances")
        assert any(
            index["name"] == "ix_pending_magic_link_issuances_email"
            and index["column_names"] == ["email"]
            for index in indexes
        )

        unique_constraints = inspector.get_unique_constraints("pending_magic_link_issuances")
        assert any(
            constraint["name"] == "uq_pending_magic_link_issuances_token_hash"
            and constraint["column_names"] == ["token_hash"]
            for constraint in unique_constraints
        )


def test_shared_rate_limit_events_table_has_expected_columns_and_index():
    db_url = os.getenv("TEST_DATABASE_URL")
    engine = create_engine(db_url)

    with engine.connect() as conn:
        inspector = inspect(conn)
        columns = {column["name"] for column in inspector.get_columns("shared_rate_limit_events")}
        assert columns == {
            "id",
            "namespace",
            "bucket_key",
            "observed_at",
        }

        foreign_keys = inspector.get_foreign_keys("shared_rate_limit_events")
        assert foreign_keys == []

        indexes = inspector.get_indexes("shared_rate_limit_events")
        assert any(
            index["name"] == "ix_shared_rate_limit_events_namespace_bucket_observed_at"
            and index["column_names"] == ["namespace", "bucket_key", "observed_at"]
            for index in indexes
        )


def test_support_requests_table_has_expected_columns_fk_and_indexes():
    db_url = os.getenv("TEST_DATABASE_URL")
    engine = create_engine(db_url)

    with engine.connect() as conn:
        inspector = inspect(conn)
        columns = {column["name"] for column in inspector.get_columns("support_requests")}
        assert columns == {
            "id",
            "creator_id",
            "request_type",
            "requester_email",
            "creator_name_snapshot",
            "status",
            "notification_attempted_at",
            "notification_sent_at",
            "notification_failed_at",
            "closed_at",
            "created_at",
            "updated_at",
        }

        foreign_keys = inspector.get_foreign_keys("support_requests")
        assert any(
            fk["referred_table"] == "creators"
            and fk["constrained_columns"] == ["creator_id"]
            for fk in foreign_keys
        )

        indexes = inspector.get_indexes("support_requests")
        assert any(
            index["name"] == "ix_support_requests_creator_id"
            and index["column_names"] == ["creator_id"]
            for index in indexes
        )
        assert any(
            index["name"] == "ix_support_requests_status"
            and index["column_names"] == ["status"]
            for index in indexes
        )
        assert any(
            index["name"] == "uq_support_requests_active_creator_request_type"
            and index["column_names"] == ["creator_id", "request_type"]
            and index["unique"]
            for index in indexes
        )


def test_support_request_status_migration_rewrites_story88_statuses_to_submitted():
    db_url = os.getenv("TEST_DATABASE_URL")
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", db_url)
    engine = create_engine(db_url)

    creator_one = str(uuid.uuid4())
    creator_two = str(uuid.uuid4())
    creator_three = str(uuid.uuid4())

    command.downgrade(cfg, "2b3c4d5e6f7a")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO creators (id, name, stripe_connect_status) VALUES "
                    "(:id_one, 'Migration One', 'pending'), "
                    "(:id_two, 'Migration Two', 'pending'), "
                    "(:id_three, 'Migration Three', 'pending')"
                ),
                {
                    "id_one": creator_one,
                    "id_two": creator_two,
                    "id_three": creator_three,
                },
            )
            conn.execute(
                text(
                    "INSERT INTO support_requests ("
                    "id, creator_id, request_type, requester_email, creator_name_snapshot, status, "
                    "notification_attempted_at, notification_sent_at, notification_failed_at, closed_at"
                    ") VALUES "
                    "("
                    ":id_one, :creator_one, 'workspace-reset', 'pending@example.com', 'Migration One', 'notification_pending', "
                    "NULL, NULL, NULL, NULL"
                    "),"
                    "("
                    ":id_two, :creator_two, 'account-deletion', 'delivered@example.com', 'Migration Two', 'pending', "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, NULL, NULL"
                    "),"
                    "("
                    ":id_three, :creator_three, 'workspace-reset', 'failed@example.com', 'Migration Three', 'notification_failed', "
                    "CURRENT_TIMESTAMP, NULL, CURRENT_TIMESTAMP, NULL"
                    ")"
                ),
                {
                    "id_one": str(uuid.uuid4()),
                    "creator_one": creator_one,
                    "id_two": str(uuid.uuid4()),
                    "creator_two": creator_two,
                    "id_three": str(uuid.uuid4()),
                    "creator_three": creator_three,
                },
            )

        command.upgrade(cfg, "head")
        with engine.connect() as conn:
            upgraded_rows = conn.execute(
                text(
                    "SELECT requester_email, status "
                    "FROM support_requests "
                    "ORDER BY requester_email ASC"
                )
            ).mappings().all()

        assert upgraded_rows == [
            {"requester_email": "delivered@example.com", "status": "submitted"},
            {"requester_email": "failed@example.com", "status": "submitted"},
            {"requester_email": "pending@example.com", "status": "submitted"},
        ]

        command.downgrade(cfg, "2b3c4d5e6f7a")
        with engine.connect() as conn:
            downgraded_rows = conn.execute(
                text(
                    "SELECT requester_email, status "
                    "FROM support_requests "
                    "ORDER BY requester_email ASC"
                )
            ).mappings().all()

        assert downgraded_rows == [
            {"requester_email": "delivered@example.com", "status": "pending"},
            {"requester_email": "failed@example.com", "status": "notification_failed"},
            {"requester_email": "pending@example.com", "status": "notification_pending"},
        ]
    finally:
        command.upgrade(cfg, "head")
        engine.dispose()


def test_creator_claim_snapshots_table_has_expected_columns_fk_and_indexes():
    db_url = os.getenv("TEST_DATABASE_URL")
    engine = create_engine(db_url)

    with engine.connect() as conn:
        inspector = inspect(conn)
        columns = {column["name"] for column in inspector.get_columns("creator_claim_snapshots")}
        assert columns == {
            "id",
            "creator_id",
            "content_id",
            "authoritative_extraction_artifact_id",
            "authoritative_fetch_snapshot_id",
            "claim_kind",
            "claim_contract_version",
            "claim_reducer_version",
            "claim_prompt_version",
            "rendered_claim_text",
            "created_at",
        }

        foreign_keys = inspector.get_foreign_keys("creator_claim_snapshots")
        assert any(
            fk["referred_table"] == "creators"
            and fk["constrained_columns"] == ["creator_id"]
            for fk in foreign_keys
        )
        assert any(
            fk["referred_table"] == "content"
            and fk["constrained_columns"] == ["content_id"]
            for fk in foreign_keys
        )
        assert any(
            fk["referred_table"] == "content_extraction_artifacts"
            and fk["constrained_columns"] == ["authoritative_extraction_artifact_id"]
            for fk in foreign_keys
        )
        assert any(
            fk["referred_table"] == "content_fetch_snapshots"
            and fk["constrained_columns"] == ["authoritative_fetch_snapshot_id"]
            for fk in foreign_keys
        )

        indexes = inspector.get_indexes("creator_claim_snapshots")
        assert any(
            index["name"] == "ix_creator_claim_snapshots_creator_id"
            and index["column_names"] == ["creator_id"]
            for index in indexes
        )
        assert any(
            index["name"] == "ix_creator_claim_snapshots_content_id"
            and index["column_names"] == ["content_id"]
            for index in indexes
        )
        assert any(
            index["name"] == "ix_creator_claim_snapshots_claim_kind"
            and index["column_names"] == ["claim_kind"]
            for index in indexes
        )


def test_creator_claim_paid_evidence_refs_table_has_expected_columns_fk_indexes_and_unique_constraints():
    db_url = os.getenv("TEST_DATABASE_URL")
    engine = create_engine(db_url)

    with engine.connect() as conn:
        inspector = inspect(conn)
        columns = {column["name"] for column in inspector.get_columns("creator_claim_paid_evidence_refs")}
        assert columns == {
            "id",
            "claim_snapshot_id",
            "booking_id",
            "invoice_id",
            "payment_event_id",
            "evidence_order",
        }

        foreign_keys = inspector.get_foreign_keys("creator_claim_paid_evidence_refs")
        assert any(
            fk["referred_table"] == "creator_claim_snapshots"
            and fk["constrained_columns"] == ["claim_snapshot_id"]
            for fk in foreign_keys
        )
        assert any(
            fk["referred_table"] == "bookings"
            and fk["constrained_columns"] == ["booking_id"]
            for fk in foreign_keys
        )
        assert any(
            fk["referred_table"] == "invoices"
            and fk["constrained_columns"] == ["invoice_id"]
            for fk in foreign_keys
        )
        assert any(
            fk["referred_table"] == "invoice_payment_events"
            and fk["constrained_columns"] == ["payment_event_id"]
            for fk in foreign_keys
        )

        indexes = inspector.get_indexes("creator_claim_paid_evidence_refs")
        assert any(
            index["name"] == "ix_creator_claim_paid_refs_snapshot_id"
            and index["column_names"] == ["claim_snapshot_id"]
            for index in indexes
        )
        assert any(
            index["name"] == "ix_creator_claim_paid_refs_invoice_id"
            and index["column_names"] == ["invoice_id"]
            for index in indexes
        )

        unique_constraints = inspector.get_unique_constraints("creator_claim_paid_evidence_refs")
        assert any(
            constraint["name"] == "uq_creator_claim_paid_refs_snapshot_order"
            and constraint["column_names"] == ["claim_snapshot_id", "evidence_order"]
            for constraint in unique_constraints
        )
        assert any(
            constraint["name"] == "uq_creator_claim_paid_refs_snapshot_invoice"
            and constraint["column_names"] == ["claim_snapshot_id", "invoice_id"]
            for constraint in unique_constraints
        )


def test_content_confirmed_topics_table_has_expected_columns_fk_indexes_and_unique_normalized_label():
    db_url = os.getenv("TEST_DATABASE_URL")
    engine = create_engine(db_url)

    with engine.connect() as conn:
        inspector = inspect(conn)
        columns = {column["name"] for column in inspector.get_columns("content_confirmed_topics")}
        assert columns == {
            "id",
            "content_id",
            "creator_id",
            "canonical_label",
            "normalized_label",
            "created_at",
            "updated_at",
        }

        foreign_keys = inspector.get_foreign_keys("content_confirmed_topics")
        assert any(
            fk["referred_table"] == "content"
            and fk["constrained_columns"] == ["content_id"]
            for fk in foreign_keys
        )
        assert any(
            fk["referred_table"] == "creators"
            and fk["constrained_columns"] == ["creator_id"]
            for fk in foreign_keys
        )

        indexes = inspector.get_indexes("content_confirmed_topics")
        assert any(
            index["name"] == "ix_content_confirmed_topics_content_id"
            and index["column_names"] == ["content_id"]
            for index in indexes
        )
        assert any(
            index["name"] == "ix_content_confirmed_topics_creator_id"
            and index["column_names"] == ["creator_id"]
            for index in indexes
        )

        unique_constraints = inspector.get_unique_constraints("content_confirmed_topics")
        assert any(
            constraint["name"] == "uq_content_confirmed_topics_content_id_normalized_label"
            and constraint["column_names"] == ["content_id", "normalized_label"]
            for constraint in unique_constraints
        )


def test_content_topic_candidates_table_has_expected_columns_fk_indexes_and_unique_normalized_label():
    db_url = os.getenv("TEST_DATABASE_URL")
    engine = create_engine(db_url)

    with engine.connect() as conn:
        inspector = inspect(conn)
        columns = {column["name"] for column in inspector.get_columns("content_topic_candidates")}
        assert columns == {
            "id",
            "content_id",
            "creator_id",
            "extraction_artifact_id",
            "confirmed_topic_id",
            "suggested_label",
            "normalized_label",
            "suggestion_method",
            "candidate_rank",
            "review_status",
            "reviewed_at",
            "created_at",
        }

        foreign_keys = inspector.get_foreign_keys("content_topic_candidates")
        assert any(
            fk["referred_table"] == "content"
            and fk["constrained_columns"] == ["content_id"]
            for fk in foreign_keys
        )
        assert any(
            fk["referred_table"] == "creators"
            and fk["constrained_columns"] == ["creator_id"]
            for fk in foreign_keys
        )
        assert any(
            fk["referred_table"] == "content_extraction_artifacts"
            and fk["constrained_columns"] == ["extraction_artifact_id"]
            for fk in foreign_keys
        )
        assert any(
            fk["referred_table"] == "content_confirmed_topics"
            and fk["constrained_columns"] == ["confirmed_topic_id"]
            for fk in foreign_keys
        )

        indexes = inspector.get_indexes("content_topic_candidates")
        assert any(
            index["name"] == "ix_content_topic_candidates_content_id"
            and index["column_names"] == ["content_id"]
            for index in indexes
        )
        assert any(
            index["name"] == "ix_content_topic_candidates_creator_id"
            and index["column_names"] == ["creator_id"]
            for index in indexes
        )
        assert any(
            index["name"] == "ix_content_topic_candidates_extraction_artifact_id"
            and index["column_names"] == ["extraction_artifact_id"]
            for index in indexes
        )
        assert any(
            index["name"] == "ix_content_topic_candidates_review_status"
            and index["column_names"] == ["review_status"]
            for index in indexes
        )

        unique_constraints = inspector.get_unique_constraints("content_topic_candidates")
        assert any(
            constraint["name"] == "uq_content_topic_candidates_artifact_id_normalized_label"
            and constraint["column_names"] == ["extraction_artifact_id", "normalized_label"]
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
            "provider",
            "provider_booking_id",
            "calendly_booking_uuid",
            "email",
            "status",
            "attribution_status",
            "unattributed_reason",
            "frozen_billing_amount_cents",
            "frozen_billing_currency",
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
        assert any(
            index["name"] == "ix_bookings_attribution_status"
            and index["column_names"] == ["attribution_status"]
            for index in indexes
        )

        unique_constraints = inspector.get_unique_constraints("bookings")
        assert any(
            constraint["name"] == "uq_bookings_calendly_booking_uuid"
            and constraint["column_names"] == ["calendly_booking_uuid"]
            for constraint in unique_constraints
        )
        assert any(
            constraint["name"] == "uq_bookings_provider_provider_booking_id"
            and constraint["column_names"] == ["provider", "provider_booking_id"]
            for constraint in unique_constraints
        )

        check_constraints = inspector.get_check_constraints("bookings")
        assert any(
            constraint["name"] == "ck_bookings_attribution_current_state"
            for constraint in check_constraints
        )


def test_invoices_table_has_expected_columns_fk_indexes_and_unique_constraints():
    db_url = os.getenv("TEST_DATABASE_URL")
    engine = create_engine(db_url)

    with engine.connect() as conn:
        inspector = inspect(conn)
        columns_by_name = {
            column["name"]: column for column in inspector.get_columns("invoices")
        }
        columns = set(columns_by_name)
        assert columns == {
            "id",
            "creator_id",
            "booking_id",
            "tid",
            "payment_provider",
            "provider_account_id",
            "provider_invoice_id",
            "stripe_account_id",
            "stripe_invoice_id",
            "amount_cents",
            "currency",
            "status",
            "issued_at",
            "paid_at",
            "voided_at",
        }
        assert columns_by_name["payment_provider"]["nullable"] is False
        assert columns_by_name["provider_account_id"]["nullable"] is True
        assert columns_by_name["provider_invoice_id"]["nullable"] is True
        assert columns_by_name["stripe_account_id"]["nullable"] is True
        assert columns_by_name["stripe_invoice_id"]["nullable"] is True

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
            constraint["name"] == "uq_invoices_provider_invoice_identity"
            and constraint["column_names"]
            == ["payment_provider", "provider_account_id", "provider_invoice_id"]
            for constraint in unique_constraints
        )
        assert any(
            constraint["name"] == "uq_invoices_stripe_invoice_id"
            and constraint["column_names"] == ["stripe_invoice_id"]
            for constraint in unique_constraints
        )


def test_blocked_billing_cases_table_has_expected_columns_fk_indexes_and_unique_constraints():
    db_url = os.getenv("TEST_DATABASE_URL")
    engine = create_engine(db_url)

    with engine.connect() as conn:
        inspector = inspect(conn)
        columns = {column["name"] for column in inspector.get_columns("blocked_billing_cases")}
        assert columns == {
            "id",
            "creator_id",
            "booking_id",
            "invoice_id",
            "tid",
            "provider",
            "provider_booking_id",
            "calendly_booking_uuid",
            "stripe_account_id",
            "frozen_amount_cents",
            "frozen_currency",
            "status",
            "reason_code",
            "provider_operation",
            "provider_http_status",
            "provider_error_code",
            "first_blocked_at",
            "last_blocked_at",
            "last_retry_at",
            "resolved_at",
            "resolution_code",
        }

        foreign_keys = inspector.get_foreign_keys("blocked_billing_cases")
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
            fk["referred_table"] == "invoices"
            and fk["constrained_columns"] == ["invoice_id"]
            for fk in foreign_keys
        )

        indexes = inspector.get_indexes("blocked_billing_cases")
        assert any(
            index["name"] == "ix_blocked_billing_cases_creator_id"
            and index["column_names"] == ["creator_id"]
            for index in indexes
        )
        assert any(
            index["name"] == "ix_blocked_billing_cases_status"
            and index["column_names"] == ["status"]
            for index in indexes
        )
        assert any(
            index["name"] == "ix_blocked_billing_cases_tid"
            and index["column_names"] == ["tid"]
            for index in indexes
        )

        unique_constraints = inspector.get_unique_constraints("blocked_billing_cases")
        assert any(
            constraint["name"] == "uq_blocked_billing_cases_booking_id"
            and constraint["column_names"] == ["booking_id"]
            for constraint in unique_constraints
        )


def test_invoice_payment_events_table_has_expected_columns_fk_indexes_and_unique_constraints():
    db_url = os.getenv("TEST_DATABASE_URL")
    engine = create_engine(db_url)

    with engine.connect() as conn:
        inspector = inspect(conn)
        columns_by_name = {
            column["name"]: column for column in inspector.get_columns("invoice_payment_events")
        }
        columns = set(columns_by_name)
        assert columns == {
            "id",
            "payment_provider",
            "provider_event_id",
            "provider_event_type",
            "provider_account_id",
            "provider_invoice_id",
            "stripe_event_id",
            "stripe_event_type",
            "stripe_account_id",
            "stripe_invoice_id",
            "invoice_id",
            "creator_id",
            "booking_id",
            "tid",
            "status",
            "unattributed_reason",
            "paid_at",
            "received_at",
            "processed_at",
        }
        assert columns_by_name["payment_provider"]["nullable"] is False
        assert columns_by_name["provider_event_id"]["nullable"] is True
        assert columns_by_name["provider_event_type"]["nullable"] is True
        assert columns_by_name["provider_account_id"]["nullable"] is True
        assert columns_by_name["provider_invoice_id"]["nullable"] is True
        assert columns_by_name["stripe_event_id"]["nullable"] is True
        assert columns_by_name["stripe_event_type"]["nullable"] is True
        assert columns_by_name["stripe_account_id"]["nullable"] is True
        assert columns_by_name["stripe_invoice_id"]["nullable"] is True

        foreign_keys = inspector.get_foreign_keys("invoice_payment_events")
        assert any(
            fk["referred_table"] == "invoices"
            and fk["constrained_columns"] == ["invoice_id"]
            for fk in foreign_keys
        )
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

        indexes = inspector.get_indexes("invoice_payment_events")
        assert any(
            index["name"] == "ix_invoice_payment_events_invoice_id"
            and index["column_names"] == ["invoice_id"]
            for index in indexes
        )
        assert any(
            index["name"] == "ix_invoice_payment_events_creator_id"
            and index["column_names"] == ["creator_id"]
            for index in indexes
        )
        assert any(
            index["name"] == "ix_invoice_payment_events_booking_id"
            and index["column_names"] == ["booking_id"]
            for index in indexes
        )
        assert any(
            index["name"] == "ix_invoice_payment_events_tid"
            and index["column_names"] == ["tid"]
            for index in indexes
        )
        assert any(
            index["name"] == "ix_invoice_payment_events_stripe_invoice_id"
            and index["column_names"] == ["stripe_invoice_id"]
            for index in indexes
        )

        unique_constraints = inspector.get_unique_constraints("invoice_payment_events")
        assert any(
            constraint["name"] == "uq_invoice_payment_events_provider_event_identity"
            and constraint["column_names"] == ["payment_provider", "provider_event_id"]
            for constraint in unique_constraints
        )
        assert any(
            constraint["name"] == "uq_invoice_payment_events_stripe_event_id"
            and constraint["column_names"] == ["stripe_event_id"]
            for constraint in unique_constraints
        )


def test_calendly_webhook_events_table_has_expected_columns_indexes_and_unique_constraints():
    db_url = os.getenv("TEST_DATABASE_URL")
    engine = create_engine(db_url)

    with engine.connect() as conn:
        inspector = inspect(conn)
        columns = {column["name"] for column in inspector.get_columns("calendly_webhook_events")}
        assert columns == {
            "id",
            "calendly_event_id",
            "provider_event_type",
            "event_type",
            "calendly_event_id_path",
            "calendly_booking_uuid",
            "calendly_booking_uuid_path",
            "tid",
            "tid_path",
            "payload",
            "reducer_key",
            "delivery_count",
            "processing_status",
            "reducer_attempt_count",
            "last_error",
            "received_at",
            "last_received_at",
            "processed_at",
        }

        foreign_keys = inspector.get_foreign_keys("calendly_webhook_events")
        assert foreign_keys == []

        indexes = inspector.get_indexes("calendly_webhook_events")
        assert any(
            index["name"] == "ix_calendly_webhook_events_booking_uuid"
            and index["column_names"] == ["calendly_booking_uuid"]
            for index in indexes
        )
        assert any(
            index["name"] == "ix_calendly_webhook_events_event_type"
            and index["column_names"] == ["event_type"]
            for index in indexes
        )
        assert any(
            index["name"] == "ix_calendly_webhook_events_processing_status"
            and index["column_names"] == ["processing_status"]
            for index in indexes
        )
        assert any(
            index["name"] == "ix_calendly_webhook_events_reducer_key"
            and index["column_names"] == ["reducer_key"]
            for index in indexes
        )

        unique_constraints = inspector.get_unique_constraints("calendly_webhook_events")
        assert any(
            constraint["name"] == "uq_calendly_webhook_events_provider_type_event_booking"
            and constraint["column_names"]
            == ["provider_event_type", "calendly_event_id", "calendly_booking_uuid"]
            for constraint in unique_constraints
        )


def test_fullscope_webhook_events_table_has_expected_columns_indexes_and_unique_constraints():
    db_url = os.getenv("TEST_DATABASE_URL")
    engine = create_engine(db_url)

    with engine.connect() as conn:
        inspector = inspect(conn)
        columns = {column["name"] for column in inspector.get_columns("fullscope_webhook_events")}
        assert columns == {
            "id",
            "provider_event_type",
            "event_type",
            "appointment_id",
            "appointment_id_path",
            "calendar_id",
            "calendar_id_path",
            "workflow_id",
            "workflow_id_path",
            "tid",
            "tid_path",
            "payload",
            "payload_sha256",
            "reducer_key",
            "delivery_count",
            "processing_status",
            "reducer_attempt_count",
            "last_error",
            "received_at",
            "last_received_at",
            "processed_at",
        }

        foreign_keys = inspector.get_foreign_keys("fullscope_webhook_events")
        assert foreign_keys == []

        indexes = inspector.get_indexes("fullscope_webhook_events")
        assert any(
            index["name"] == "ix_fullscope_webhook_events_appointment_id"
            and index["column_names"] == ["appointment_id"]
            for index in indexes
        )
        assert any(
            index["name"] == "ix_fullscope_webhook_events_event_type"
            and index["column_names"] == ["event_type"]
            for index in indexes
        )
        assert any(
            index["name"] == "ix_fullscope_webhook_events_processing_status"
            and index["column_names"] == ["processing_status"]
            for index in indexes
        )
        assert any(
            index["name"] == "ix_fullscope_webhook_events_reducer_key"
            and index["column_names"] == ["reducer_key"]
            for index in indexes
        )

        unique_constraints = inspector.get_unique_constraints("fullscope_webhook_events")
        assert any(
            constraint["name"] == "uq_fullscope_webhook_events_provider_type_appointment_hash"
            and constraint["column_names"]
            == ["provider_event_type", "appointment_id", "payload_sha256"]
            for constraint in unique_constraints
        )
