from datetime import datetime, timezone
import uuid

from app.models.billing_provider import BILLING_PROVIDER_PAYPAL, BILLING_PROVIDER_STRIPE
from app.models.billing_provider_switch_attempt import BillingProviderSwitchAttempt
from app.services.billing_provider import (
    BILLING_ACCOUNT_READINESS_ISSUE_CONFIRM_PAYPAL_PRIMARY_EMAIL,
)
from app.services.billing_provider_switch import BillingProviderSwitchCleanState
from app.services.creator_shell_view_model import (
    build_account_billing_management_view,
    build_attention_overview_view,
    build_setup_home_attention_summary_view,
    build_setup_home_milestone_view,
)
from app.services.creator_workspace_state import CreatorWorkspaceReadiness


def _readiness(
    *,
    billing_connect_status: str = "pending",
    billing_connected: bool = False,
    billable_now: bool = False,
    ready_to_track: bool = False,
    waiting_for_first_paid_result: bool = False,
    booking_links_count: int = 0,
    trackable_booking_links_count: int = 0,
    limited_tracking_booking_links_count: int = 0,
    billing_ready_count: int = 0,
    tracked_content_count: int = 0,
    paid_invoice_count: int = 0,
    billing_provider: str | None = None,
    billing_provider_guidance_state: str | None = None,
    billing_provider_actionable_issue_codes: tuple[str, ...] = (),
) -> CreatorWorkspaceReadiness:
    return CreatorWorkspaceReadiness(
        billing_connect_status=billing_connect_status,
        billing_connected=billing_connected,
        billable_now=billable_now,
        ready_to_track=ready_to_track,
        waiting_for_first_paid_result=waiting_for_first_paid_result,
        booking_links_count=booking_links_count,
        trackable_booking_links_count=trackable_booking_links_count,
        limited_tracking_booking_links_count=limited_tracking_booking_links_count,
        billing_ready_count=billing_ready_count,
        tracked_content_count=tracked_content_count,
        paid_invoice_count=paid_invoice_count,
        billing_provider=billing_provider,
        billing_provider_guidance_state=billing_provider_guidance_state,
        billing_provider_actionable_issue_codes=billing_provider_actionable_issue_codes,
    )


def _switch_attempt(
    *,
    target_billing_provider: str = BILLING_PROVIDER_PAYPAL,
    target_billing_connect_status: str = "pending",
    target_billing_account_id: str | None = None,
) -> BillingProviderSwitchAttempt:
    now = datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc)
    return BillingProviderSwitchAttempt(
        id=uuid.uuid4(),
        creator_id=uuid.uuid4(),
        source_billing_provider=BILLING_PROVIDER_STRIPE,
        target_billing_provider=target_billing_provider,
        target_billing_connect_status=target_billing_connect_status,
        target_billing_account_id=target_billing_account_id,
        target_billing_connected_at=now if target_billing_account_id else None,
        target_billing_provider_correlation_id=None,
        created_at=now,
        updated_at=now,
    )


def test_build_setup_home_milestone_view_keeps_bookings_without_paid_result_state():
    milestone = build_setup_home_milestone_view(
        readiness=_readiness(
            billing_connect_status="connected",
            billing_connected=True,
            billable_now=True,
            ready_to_track=True,
            waiting_for_first_paid_result=True,
            booking_links_count=1,
            trackable_booking_links_count=1,
            billing_ready_count=1,
            tracked_content_count=1,
            billing_provider=BILLING_PROVIDER_STRIPE,
        ),
        attention_count=0,
        tracked_booking_count=2,
        show_provider_choice=False,
        paypal_available_to_creator=True,
    )

    assert milestone.title == "Bookings are landing; paid proof is next"
    assert milestone.next_title == "Review the content funnel"
    assert milestone.action["action_href"] == "/app/reports"


def test_build_setup_home_attention_summary_view_zero_attention_keeps_inline_handoff():
    summary = build_setup_home_attention_summary_view(0)

    assert summary.title is None
    assert summary.action is None
    assert summary.inline_link_label == "Attention"
    assert summary.inline_suffix == " if anything needs repair."


def test_build_attention_overview_view_uses_current_backlog_counts():
    overview = build_attention_overview_view(blocked_count=2, unmatched_count=1)

    assert overview.blocked_heading == "Tracked bookings blocked before invoicing"
    assert overview.blocked_backlog_copy == "2 bookings still blocked before invoicing and outside paid totals."
    assert overview.unmatched_heading == "Verified payments still diagnostic-only"
    assert overview.unmatched_backlog_copy == "1 payment event still diagnostic only and outside paid totals while the attribution chain is incomplete."


def test_build_account_billing_management_view_connected_clean_state_offers_switch_start():
    management = build_account_billing_management_view(
        current_billing_provider=BILLING_PROVIDER_STRIPE,
        readiness=_readiness(
            billing_connect_status="connected",
            billing_connected=True,
            billable_now=True,
            booking_links_count=1,
            trackable_booking_links_count=1,
            billing_ready_count=1,
            billing_provider=BILLING_PROVIDER_STRIPE,
        ),
        show_provider_choice=False,
        switch_attempt=None,
        switch_clean_state=BillingProviderSwitchCleanState(
            open_invoice_count=0,
            blocked_billing_count=0,
        ),
        switch_target_guidance_state="ready",
        switch_target_actionable_issue_codes=(),
        paypal_available_to_creator=True,
    )

    assert management.label == "Connected"
    assert management.action_mode == "simple"
    assert management.action is not None
    assert management.action["href"] == "/app/paypal/connect/start"
    assert management.action_label_override == "Start PayPal switch"


def test_build_account_billing_management_view_pending_switch_not_ready_keeps_current_provider_active():
    management = build_account_billing_management_view(
        current_billing_provider=BILLING_PROVIDER_STRIPE,
        readiness=_readiness(
            billing_connect_status="connected",
            billing_connected=True,
            billing_provider=BILLING_PROVIDER_STRIPE,
        ),
        show_provider_choice=False,
        switch_attempt=_switch_attempt(
            target_billing_provider=BILLING_PROVIDER_PAYPAL,
            target_billing_connect_status="connected",
            target_billing_account_id="merchant_switch_target",
        ),
        switch_clean_state=BillingProviderSwitchCleanState(
            open_invoice_count=0,
            blocked_billing_count=0,
        ),
        switch_target_guidance_state="not_ready",
        switch_target_actionable_issue_codes=(
            BILLING_ACCOUNT_READINESS_ISSUE_CONFIRM_PAYPAL_PRIMARY_EMAIL,
        ),
        paypal_available_to_creator=True,
    )

    assert management.label == "Connected"
    assert management.action_mode == "switch-attempt"
    assert "confirm the primary email on the connected PayPal business account" in management.body
    assert "Stripe stays active until PayPal is ready and you commit the switch." in management.body
