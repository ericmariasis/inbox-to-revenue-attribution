import os

from sqlalchemy import create_engine, text

from scripts.seed_growth_loop_demo import seed_growth_loop_demo


def test_seed_growth_loop_demo_creates_paypal_paid_proof_workspace() -> None:
    engine = create_engine(os.environ["TEST_DATABASE_URL"])

    with engine.begin() as conn:
        seed = seed_growth_loop_demo(
            conn=conn,
            base_url="http://127.0.0.1:8000",
            creator_email="growth-loop-demo-test@example.com",
        )

        row = conn.execute(
            text(
                "SELECT "
                "c.billing_provider, c.billing_connect_status, c.billing_account_id, "
                "count(DISTINCT bl.id) AS booking_links, "
                "count(DISTINCT ct.id) AS content_items, "
                "count(DISTINCT b.id) AS bookings, "
                "count(DISTINCT i.id) AS invoices, "
                "count(DISTINCT ipe.id) AS payment_events, "
                "min(i.payment_provider) AS invoice_payment_provider, "
                "min(ipe.payment_provider) AS event_payment_provider, "
                "min(ipe.provider_event_type) AS provider_event_type, "
                "min(ipe.status) AS event_status, "
                "sum(i.amount_cents) AS revenue_cents "
                "FROM auth_users au "
                "JOIN creators c ON c.id = au.creator_id "
                "JOIN booking_links bl ON bl.creator_id = c.id "
                "JOIN content ct ON ct.creator_id = c.id "
                "JOIN bookings b ON b.creator_id = c.id "
                "JOIN invoices i ON i.creator_id = c.id "
                "JOIN invoice_payment_events ipe ON ipe.invoice_id = i.id "
                "WHERE au.email = :email "
                "GROUP BY c.id"
            ),
            {"email": "growth-loop-demo-test@example.com"},
        ).mappings().one()

    assert seed.creator_email == "growth-loop-demo-test@example.com"
    assert seed.login_url.startswith("http://127.0.0.1:8000/auth/magic-link/verify?token=")
    assert seed.backup_login_url.startswith(
        "http://127.0.0.1:8000/auth/magic-link/verify?token="
    )
    assert seed.growth_loop_url == "http://127.0.0.1:8000/app/growth-loop"
    assert seed.reports_url == "http://127.0.0.1:8000/app/reports"
    assert seed.expected_stage == "Paid Result Exists"
    assert seed.expected_revenue == "$195.00"

    assert row["billing_provider"] == "paypal"
    assert row["billing_connect_status"] == "connected"
    assert row["billing_account_id"].startswith("merchant_growth_loop_demo_")
    assert row["booking_links"] == 1
    assert row["content_items"] == 1
    assert row["bookings"] == 1
    assert row["invoices"] == 1
    assert row["payment_events"] == 1
    assert row["invoice_payment_provider"] == "paypal"
    assert row["event_payment_provider"] == "paypal"
    assert row["provider_event_type"] == "PAYMENT.CAPTURE.COMPLETED"
    assert row["event_status"] == "applied"
    assert row["revenue_cents"] == 19500
