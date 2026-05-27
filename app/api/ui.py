import html
import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from urllib.parse import parse_qs, quote, urlencode, urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.api.bookings import (
    BookingActivityResponse,
    list_booking_activity_responses_for_creator,
)
from app.api.booking_links import (
    create_booking_link_response_for_creator,
    list_booking_link_responses_for_creator,
)
from app.api.content import (
    confirm_content_topic_candidate_response_for_creator,
    create_content_topic_candidates_response_for_creator,
    create_content_response_for_creator,
    get_content_response_for_creator_by_tid,
    get_content_topic_review_response_for_creator,
    list_content_responses_for_creator,
    promote_content_authoritative_evidence_response_for_creator,
    reject_content_topic_candidate_response_for_creator,
)
from app.api.deps import (
    browser_auth_user_is_allowlisted_operator,
    get_optional_browser_auth_user,
)
from app.api.paypal import build_paypal_connect_start_response
from app.api.stripe import (
    STRIPE_CONNECT_FAILED_STATUS,
    STRIPE_CONNECT_INTERRUPTED_STATUS,
    build_stripe_connect_start_response,
)
from app.core.config import get_settings
from app.db.session import SessionLocal, get_db
from app.models.auth_user import AuthUser
from app.models.billing_provider import (
    BILLING_CONNECT_STATUS_CONNECTED,
    BILLING_PROVIDER_PAYPAL,
    BILLING_PROVIDER_STRIPE,
)
from app.models.billing_provider_switch_attempt import BillingProviderSwitchAttempt
from app.models.booking_provider import (
    BOOKING_PROVIDER_CALENDLY,
    BOOKING_PROVIDER_FULLSCOPE,
    booking_provider_supports_creator_visible_tracked_content,
    booking_provider_supports_creator_visible_tracked_destination,
)
from app.models.support_request import SupportRequestRecord
from app.schemas.booking_link import BookingLinkCreateRequest, BookingLinkResponse
from app.schemas.auth import MagicLinkStartRequest
from app.schemas.content import (
    ContentCreateRequest,
    ContentResponse,
    ContentTopicCandidateConfirmRequest,
    ContentTopicReviewResponse,
)
from app.services.auth_magic_link import start_magic_link
from app.services.billing_provider import (
    BILLING_ACCOUNT_READINESS_ISSUE_COMPLETE_STRIPE_SETUP,
    BILLING_ACCOUNT_READINESS_ISSUE_CONFIRM_PAYPAL_PRIMARY_EMAIL,
    BILLING_ACCOUNT_READINESS_ISSUE_ENABLE_PAYPAL_PAYMENTS_RECEIVABLE,
    BILLING_ACCOUNT_READINESS_ISSUE_GRANT_PAYPAL_THIRD_PARTY_PERMISSIONS,
    BillingAccountReadiness,
    BillingProviderError,
    BillingProviderResolutionError,
    build_billing_provider_registry,
    get_billing_account_readiness,
    resolve_billing_provider,
)
from app.services.billing_provider_switch import (
    BILLING_PROVIDER_SWITCH_REASON_SWITCH_ATTEMPT_MISSING,
    BILLING_PROVIDER_SWITCH_REASON_SWITCH_NOT_CLEAN,
    BILLING_PROVIDER_SWITCH_REASON_SWITCH_REQUIRES_CONNECTED_PROVIDER,
    BILLING_PROVIDER_SWITCH_REASON_SWITCH_TARGET_ALREADY_CONNECTED,
    BILLING_PROVIDER_SWITCH_REASON_SWITCH_TARGET_NOT_CONNECTED,
    BILLING_PROVIDER_SWITCH_REASON_SWITCH_TARGET_NOT_READY,
    BillingProviderSwitchCleanState,
    BillingProviderSwitchError,
    cancel_billing_provider_switch_attempt,
    commit_billing_provider_switch_attempt,
    get_billing_provider_switch_attempt,
    get_billing_provider_switch_clean_state,
    restart_billing_provider_switch_attempt,
)
from app.services.blocked_billing import (
    BLOCKED_BILLING_REASON_CREATOR_NOT_BILLABLE,
    BLOCKED_BILLING_REASON_PROVIDER_ERROR,
    BlockedBillingCaseSummary,
    BlockedBillingRetryService,
    list_open_blocked_billing_cases,
)
from app.services.booking_attribution import (
    BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED,
    BOOKING_UNATTRIBUTED_REASON_MISSING_TID,
    BOOKING_UNATTRIBUTED_REASON_UNKNOWN_TID,
)
from app.services.browser_session import (
    clear_browser_session_cookie,
    get_browser_session_token,
)
from app.services.creator_workspace_state import (
    CreatorWorkspaceReadiness,
    CreatorWorkspaceState,
    build_creator_workspace_readiness,
    build_creator_workspace_state,
)
from app.services.creator_shell_view_model import (
    build_account_billing_management_view,
    build_attention_overview_view,
    build_setup_home_experiments_handoff_view,
    build_setup_home_attention_summary_view,
    build_setup_home_milestone_view,
)
from app.services.email_provider import (
    MagicLinkEmailDeliveryError,
    SupportRequestEmailDeliveryError,
)
from app.services.evidence_ingress_health import (
    AUTHORITATIVE_CONTENT_LAG_REASON_MISSING_AUTHORITY,
    AUTHORITATIVE_CONTENT_LAG_REASON_STALE_AUTHORITY,
    CreatorEvidenceIngressHealthSnapshot,
    PaymentProviderHealthSnapshot,
    ProviderIngressHealthSnapshot,
    get_creator_evidence_ingress_health_snapshot,
)
from app.services.growth_loop_agent import (
    GrowthLoopActionBrief,
    GrowthLoopWorkspaceEvidence,
    build_growth_loop_action_brief,
)
from app.services.loomi_mcp import build_growth_loop_loomi_context
from app.services.invoice_payment_events import (
    PAYMENT_PROVENANCE_STATE_CONFLICTING,
    PAYMENT_PROVENANCE_STATE_MATCHED,
    PAYMENT_PROVENANCE_STATE_PENDING,
    PAYMENT_PROVENANCE_STATE_UNMATCHED,
    UNATTRIBUTED_REASON_MISSING_TID,
    UNATTRIBUTED_REASON_UNKNOWN_BOOKING_UUID,
    UNATTRIBUTED_REASON_UNKNOWN_STRIPE_INVOICE_ID,
    UnmatchedPaymentEventSummary,
    list_current_unmatched_payment_events,
)
from app.services.next_content_experiments import (
    EXPERIMENT_RUN_STATUS_READY,
    EXPERIMENT_RUN_STATUS_UNSUPPORTED,
    CreatorNextContentExperimentsReadinessSummary,
    CreatorNextContentExperimentCardDrilldown,
    CreatorNextContentExperimentsResult,
    CreatorNextContentExperimentsRunComparison,
    HelperFreshnessPolicy,
    HelperGenerationLineage,
    HelperVersionSemantics,
    NextContentExperimentCard,
    compare_creator_next_content_experiments_runs,
    create_creator_next_content_experiments_run,
    get_creator_next_content_experiment_card_drilldown,
    get_creator_next_content_experiment_card_drilldown_by_card_id,
    get_creator_next_content_experiments_run,
    get_current_creator_next_content_experiments_readiness_summary,
)
from app.services.operator_experiment_drafts import (
    CreatorOperatorExperimentDraftRunResult,
    OperatorExperimentDraftNotReadyError,
    OperatorExperimentDraftProviderError,
    OperatorExperimentDraftUnavailableError,
    create_creator_operator_experiment_draft_run,
    get_creator_operator_experiment_draft_run,
    get_latest_creator_operator_experiment_draft_run,
)
from app.services.paypal_provider import build_default_paypal_provider
from app.services.rate_limit import (
    DEFAULT_SHARED_RATE_LIMITER,
    SUPPORT_REQUEST_SUBMIT_POLICY,
    build_support_request_rate_limit_bucket_key,
)
from app.services.reporting import (
    CreatorReportsContentDrilldown,
    CreatorReportsBookingLinkSummary,
    CreatorPaidAttributionExplanation,
    CreatorReportsSummary,
    CreatorReportsTopicSummary,
    PaidAttributionEvidence,
    ReportsBookingLinkSummaryRow,
    ReportsContentBooking,
    ReportsSummaryRow,
    ReportsTopicSummaryRow,
    build_reports_summary_csv,
    get_creator_paid_attribution_explanation,
    get_creator_reports_booking_link_summary,
    get_creator_reports_content_drilldown,
    get_creator_reports_summary,
    get_creator_reports_topic_summary,
)
from app.services.support_requests import (
    SUPPORT_REQUEST_TYPE_ACCOUNT_DELETION,
    SUPPORT_REQUEST_TYPE_WORKSPACE_RESET,
    create_or_get_active_support_request,
    get_support_request_by_id,
    list_active_support_requests_for_creator,
    list_latest_support_requests_for_creator,
    list_support_requests_for_operator,
    mark_support_request_notification_failed,
    mark_support_request_notification_succeeded,
    send_support_request_email,
    support_request_available_transitions,
    support_request_notification_state_display,
    support_request_public_id,
    support_request_status_display,
    support_request_status_label,
    support_request_type_label,
    transition_support_request_status,
)
from app.services.stripe_provider import build_default_stripe_provider

logger = logging.getLogger(__name__)

router = APIRouter(include_in_schema=False)

STATUS_MESSAGES = {
    "sent": {
        "title": "Check your inbox",
        "body": "If the address is valid, we sent a fresh sign-in link. Open it on this same device and browser. If you opened the email somewhere else or the link expires, come back here and request another one.",
        "notice_class": "notice success",
    },
    "invalid-email": {
        "title": "Enter a valid email",
        "body": "Use a real email address so we can send a secure sign-in link.",
        "notice_class": "notice error",
    },
    "invalid-link": {
        "title": "That sign-in link is invalid or expired",
        "body": "This usually means the link expired or it was opened on a different device or browser than the one where sign-in started. Enter your email below and we will send a fresh link for this same device and browser.",
        "notice_class": "notice error",
    },
    "retry": {
        "title": "Try again in a moment",
        "body": "We could not send the sign-in email just now. Try again in a few minutes.",
        "notice_class": "notice error",
    },
    STRIPE_CONNECT_INTERRUPTED_STATUS: {
        "title": "Stripe setup did not finish",
        "body": "Sign in again if needed, then restart Stripe from setup home. No changes were applied to this workspace.",
        "notice_class": "notice error",
    },
    STRIPE_CONNECT_FAILED_STATUS: {
        "title": "Stripe could not finish connecting",
        "body": "Return to setup home and try the Stripe step again. Your current workspace is still safe to use.",
        "notice_class": "notice error",
    },
}
SETUP_HOME_STATUS_MESSAGES = {
    STRIPE_CONNECT_INTERRUPTED_STATUS: {
        "title": "Stripe setup was interrupted",
        "body": "No changes were made to this workspace. Start the Stripe step again when you are ready.",
        "notice_class": "notice error",
    },
    STRIPE_CONNECT_FAILED_STATUS: {
        "title": "Stripe could not finish connecting",
        "body": "Try the Stripe step again from this page. If the problem repeats, confirm you are using the right Stripe account for this workspace.",
        "notice_class": "notice error",
    },
}

BOOKING_LINK_FORM_FIELDS = (
    "provider",
    "name",
    "destination_url",
    "fullscope_supported_calendar_confirmed",
    "billing_amount_cents",
    "billing_currency",
)
CONTENT_FORM_FIELDS = (
    "source_url",
    "booking_link_id",
)
REPORT_FILTER_FIELDS = (
    "start_date",
    "end_date",
)
CREATOR_VISIBLE_BOOKING_LINK_PROVIDERS = frozenset({BOOKING_PROVIDER_CALENDLY})
ACCOUNT_REQUEST_STATUS_MESSAGES = {
    "workspace-reset-requested": {
        "title": "Workspace reset requested",
        "body": "Your request was recorded for manual review. Keep using this workspace unless support confirms that reset work is complete.",
        "notice_class": "notice success",
    },
    "account-deletion-requested": {
        "title": "Account deletion requested",
        "body": "Your request was recorded for manual review. No local data has been removed yet.",
        "notice_class": "notice success",
    },
    "workspace-reset-retry": {
        "title": "Workspace reset saved, but notification failed",
        "body": "We recorded your workspace reset request, but we could not send the support email just now. The saved request remains visible below.",
        "notice_class": "notice error",
    },
    "account-deletion-retry": {
        "title": "Account deletion saved, but notification failed",
        "body": "We recorded your account deletion request, but we could not send the support email just now. The saved request remains visible below.",
        "notice_class": "notice error",
    },
    "workspace-reset-active": {
        "title": "Workspace reset already pending",
        "body": "You already have one active workspace reset request during beta. Review the saved request details below instead of opening another one.",
        "notice_class": "notice error",
    },
    "account-deletion-active": {
        "title": "Account deletion already pending",
        "body": "You already have one active account deletion request during beta. Review the saved request details below instead of opening another one.",
        "notice_class": "notice error",
    },
    "workspace-reset-throttled": {
        "title": "Too many workspace reset attempts",
        "body": "We recorded too many recent workspace reset submit attempts. Wait a few minutes before trying again.",
        "notice_class": "notice error",
    },
    "account-deletion-throttled": {
        "title": "Too many account deletion attempts",
        "body": "We recorded too many recent account deletion submit attempts. Wait a few minutes before trying again.",
        "notice_class": "notice error",
    },
    "billing-provider-switch-blocked": {
        "title": "Provider switch is blocked",
        "body": "Finish or clear any open invoices and other active billing work before starting or committing a provider switch.",
        "notice_class": "notice error",
    },
    "billing-provider-switch-connected": {
        "title": "Replacement provider saved",
        "body": "The replacement provider was connected for the pending switch. Review the billing connection card below to finish or cancel the switch.",
        "notice_class": "notice success",
    },
    "billing-provider-switch-failed": {
        "title": "Replacement provider could not be saved",
        "body": "The current billing provider stayed active. Start the replacement setup again or cancel the pending switch from the billing connection card below.",
        "notice_class": "notice error",
    },
    "billing-provider-switch-committed": {
        "title": "Billing provider switched",
        "body": "Future billing on this workspace now uses the replacement provider. Existing local history stays unchanged.",
        "notice_class": "notice success",
    },
    "billing-provider-switch-canceled": {
        "title": "Pending provider switch canceled",
        "body": "The pending replacement provider was cleared. The currently active billing provider on this workspace did not change.",
        "notice_class": "notice success",
    },
    "billing-provider-switch-missing": {
        "title": "No pending provider switch found",
        "body": "Start a new provider switch from the current billing connection when you are ready.",
        "notice_class": "notice error",
    },
    "paypal-unavailable": {
        "title": "PayPal setup is not available yet",
        "body": "PayPal setup is not yet available for general creators. Stripe remains the supported self-serve billing path for now.",
        "notice_class": "notice error",
    },
    "paypal-disconnected": {
        "title": "PayPal disconnected",
        "body": "This workspace no longer offers PayPal for future billing. Historical workspace records stay preserved locally.",
        "notice_class": "notice success",
    },
}
OPERATOR_SUPPORT_REQUEST_STATUS_MESSAGES = {
    "status-updated": {
        "title": "Request status updated",
        "body": "The support request review status was saved successfully.",
        "notice_class": "notice success",
    },
    "invalid-transition": {
        "title": "Request status was not changed",
        "body": "That review transition is not allowed from the current saved state.",
        "notice_class": "notice error",
    },
}
ACCOUNT_DANGER_ZONE_FRAGMENT = "#danger-zone"
ACCOUNT_BILLING_FRAGMENT = "#billing-connection"
PAYPAL_DISCONNECT_CONFIRM_VALUE = "disconnect-paypal"

_BILLING_PROVIDER_SETUP_STATE_NOT_APPLICABLE = "not_applicable"
_BILLING_PROVIDER_SETUP_STATE_PENDING_CONNECTION = "pending_connection"
_BILLING_PROVIDER_SETUP_STATE_READY = "ready"
_BILLING_PROVIDER_SETUP_STATE_NOT_READY = "not_ready"
_BILLING_PROVIDER_SETUP_STATE_BLOCKED = "blocked"
_PAYPAL_UNAVAILABLE_CREATOR_COPY = (
    "PayPal setup is not yet available for general creators. "
    "Stripe remains the supported self-serve billing path for now."
)
_PUBLIC_LEGAL_LINKS_HTML = (
    '<a href="/terms" class="inline-link">Terms and Conditions</a> '
    '<a href="/privacy" class="inline-link">Privacy Policy</a>'
)
_PAYPAL_DISCONNECT_CONFIRMATION_COPY = (
    "Disconnecting your PayPal account will prevent you from offering PayPal services "
    "and products on your website. Do you wish to continue?"
)


@dataclass(frozen=True)
class _BillingProviderSetupGuidance:
    state: str
    readiness: BillingAccountReadiness | None = None

    @property
    def ready(self) -> bool | None:
        if self.state == _BILLING_PROVIDER_SETUP_STATE_READY:
            return True
        if self.state in {
            _BILLING_PROVIDER_SETUP_STATE_NOT_READY,
            _BILLING_PROVIDER_SETUP_STATE_BLOCKED,
        }:
            return False
        return None

    @property
    def actionable_issue_codes(self) -> tuple[str, ...]:
        if self.readiness is None:
            return ()
        return self.readiness.creator_actionable_issue_codes


@router.get("/")
def root(
    request: Request,
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
) -> Response:
    if current_user is not None:
        return _redirect("/app")

    should_clear_cookie = get_browser_session_token(request) is not None
    response = _html_response(_render_public_home_page())
    if should_clear_cookie:
        clear_browser_session_cookie(response, settings=get_settings())
    return response


@router.get("/terms")
def terms_page() -> HTMLResponse:
    return _html_response(_render_terms_page())


@router.get("/privacy")
def privacy_page() -> HTMLResponse:
    return _html_response(_render_privacy_page())


@router.get("/sign-in")
def sign_in_page(
    request: Request,
    status_value: str | None = Query(default=None, alias="status"),
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
) -> Response:
    if current_user is not None:
        return _redirect("/app")

    response = _html_response(_render_sign_in_page(status_value))
    if get_browser_session_token(request) is not None:
        clear_browser_session_cookie(response, settings=get_settings())
    return response


@router.post("/sign-in")
async def sign_in_start(
    request: Request,
    db: Session = Depends(get_db),
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
) -> Response:
    if current_user is not None:
        return _redirect("/app")

    form_values = await _parse_form_values(request)
    email = form_values.get("email", "")

    try:
        payload = MagicLinkStartRequest(email=email)
    except ValidationError:
        return _redirect("/sign-in?status=invalid-email")

    try:
        start_magic_link(
            db,
            payload.email,
            provider=request.app.state.email_provider,
            client_ip=request.client.host if request.client is not None else None,
        )
    except MagicLinkEmailDeliveryError:
        return _redirect("/sign-in?status=retry")

    return _redirect("/sign-in?status=sent")


@router.get("/app")
def creator_app_shell(
    request: Request,
    status_value: str | None = Query(default=None, alias="status"),
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect(
            _sign_in_path(status_value=status_value),
            clear_session=should_clear_cookie,
        )

    booking_links = list_booking_link_responses_for_creator(
        creator_id=current_user.creator_id,
        db=db,
    )
    content_items = list_content_responses_for_creator(
        creator_id=current_user.creator_id,
        db=db,
    )
    summary = get_creator_reports_summary(
        creator_id=current_user.creator_id,
        db=db,
    )
    billing_provider_guidance = _creator_workspace_billing_provider_guidance(
        request=request,
        current_user=current_user,
    )
    paypal_available_to_creator = _paypal_available_to_creator(
        request=request,
        current_user=current_user,
    )
    workspace_state = build_creator_workspace_state(
        raw_billing_connect_status=current_user.creator.resolved_billing_connect_status,
        raw_billing_provider=current_user.creator.resolved_billing_provider,
        booking_links=booking_links,
        content_items=content_items,
        paid_invoice_count=summary.paid_invoice_count,
        blocked_billing_count=summary.blocked_summary.open_case_count,
        unmatched_payment_count=summary.unattributed_current_backlog.event_count,
        billing_provider_ready=billing_provider_guidance.ready,
        billing_provider_guidance_state=(
            None
            if billing_provider_guidance.state == _BILLING_PROVIDER_SETUP_STATE_NOT_APPLICABLE
            else billing_provider_guidance.state
        ),
        billing_provider_actionable_issue_codes=(
            billing_provider_guidance.actionable_issue_codes
        ),
    )
    experiments_readiness_summary = get_current_creator_next_content_experiments_readiness_summary(
        creator_id=current_user.creator_id,
        db=db,
    )

    return _html_response(
        _render_app_shell(
            current_user=current_user,
            workspace_state=workspace_state,
            reports_summary=summary,
            status_value=status_value,
            paypal_available_to_creator=paypal_available_to_creator,
            experiments_readiness_summary=experiments_readiness_summary,
            growth_loop_agent_feature_enabled=(
                _request_settings(request).growth_loop_agent_feature_enabled
            ),
        )
    )


@router.get("/app/growth-loop")
def creator_growth_loop_agent_page(
    request: Request,
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    settings = _request_settings(request)
    if not settings.growth_loop_agent_feature_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="growth loop agent disabled",
        )

    booking_links = list_booking_link_responses_for_creator(
        creator_id=current_user.creator_id,
        db=db,
    )
    content_items = list_content_responses_for_creator(
        creator_id=current_user.creator_id,
        db=db,
    )
    summary = get_creator_reports_summary(
        creator_id=current_user.creator_id,
        db=db,
    )
    billing_provider_guidance = _creator_workspace_billing_provider_guidance(
        request=request,
        current_user=current_user,
    )
    readiness = build_creator_workspace_readiness(
        raw_billing_connect_status=current_user.creator.resolved_billing_connect_status,
        raw_billing_provider=current_user.creator.resolved_billing_provider,
        booking_links=booking_links,
        content_items=content_items,
        paid_invoice_count=summary.paid_invoice_count,
        billing_provider_ready=billing_provider_guidance.ready,
        billing_provider_guidance_state=(
            None
            if billing_provider_guidance.state == _BILLING_PROVIDER_SETUP_STATE_NOT_APPLICABLE
            else billing_provider_guidance.state
        ),
        billing_provider_actionable_issue_codes=(
            billing_provider_guidance.actionable_issue_codes
        ),
    )
    tracked_booking_count = sum(row.booking_count for row in summary.rows)
    loomi_provider = getattr(request.app.state, "growth_loop_loomi_provider", None)
    loomi_context = build_growth_loop_loomi_context(
        settings=settings,
        provider=loomi_provider,
    )
    brief = build_growth_loop_action_brief(
        evidence=GrowthLoopWorkspaceEvidence(
            billing_connected=readiness.billing_connected,
            billable_now=readiness.billable_now,
            booking_links_count=readiness.booking_links_count,
            billing_ready_count=readiness.billing_ready_count,
            tracked_content_count=readiness.tracked_content_count,
            booking_count=tracked_booking_count,
            paid_invoice_count=summary.paid_invoice_count,
            paid_revenue_cents=summary.paid_revenue_cents,
            billing_provider=readiness.billing_provider,
        ),
        loomi_context=loomi_context,
    )

    return _html_response(
        _render_growth_loop_agent_page(
            current_user=current_user,
            brief=brief,
        )
    )


@router.get("/app/account")
def creator_account_page(
    request: Request,
    status_value: str | None = Query(default=None, alias="status"),
    confirm_value: str | None = Query(default=None, alias="confirm"),
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    booking_links = list_booking_link_responses_for_creator(
        creator_id=current_user.creator_id,
        db=db,
    )
    content_items = list_content_responses_for_creator(
        creator_id=current_user.creator_id,
        db=db,
    )
    summary = get_creator_reports_summary(
        creator_id=current_user.creator_id,
        db=db,
    )
    billing_provider_guidance = _creator_workspace_billing_provider_guidance(
        request=request,
        current_user=current_user,
    )
    readiness = build_creator_workspace_readiness(
        raw_billing_connect_status=current_user.creator.resolved_billing_connect_status,
        raw_billing_provider=current_user.creator.resolved_billing_provider,
        booking_links=booking_links,
        content_items=content_items,
        paid_invoice_count=summary.paid_invoice_count,
        billing_provider_ready=billing_provider_guidance.ready,
        billing_provider_guidance_state=(
            None
            if billing_provider_guidance.state == _BILLING_PROVIDER_SETUP_STATE_NOT_APPLICABLE
            else billing_provider_guidance.state
        ),
        billing_provider_actionable_issue_codes=(
            billing_provider_guidance.actionable_issue_codes
        ),
    )
    support_requests = list_latest_support_requests_for_creator(
        db,
        creator_id=current_user.creator_id,
    )
    active_support_requests = list_active_support_requests_for_creator(
        db,
        creator_id=current_user.creator_id,
    )
    switch_attempt = get_billing_provider_switch_attempt(
        db=db,
        creator_id=current_user.creator_id,
    )
    switch_clean_state = get_billing_provider_switch_clean_state(
        db=db,
        creator_id=current_user.creator_id,
    )
    switch_target_guidance = (
        _billing_provider_switch_target_guidance(
            request=request,
            switch_attempt=switch_attempt,
        )
        if switch_attempt is not None
        else _BillingProviderSetupGuidance(
            state=_BILLING_PROVIDER_SETUP_STATE_NOT_APPLICABLE
        )
    )
    paypal_available_to_creator = _paypal_available_to_creator(
        request=request,
        current_user=current_user,
    )
    return _html_response(
        _render_account_page(
            current_user=current_user,
            readiness=readiness,
            support_requests=support_requests,
            active_support_requests=active_support_requests,
            status_value=status_value,
            confirm_value=confirm_value,
            switch_attempt=switch_attempt,
            switch_clean_state=switch_clean_state,
            switch_target_guidance=switch_target_guidance,
            paypal_available_to_creator=paypal_available_to_creator,
        )
    )


@router.post("/app/account/requests/workspace-reset")
def creator_workspace_reset_request(
    request: Request,
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    return _account_support_request_response(
        request=request,
        current_user=current_user,
        db=db,
        request_type=SUPPORT_REQUEST_TYPE_WORKSPACE_RESET,
    )


@router.post("/app/account/requests/account-deletion")
def creator_account_deletion_request(
    request: Request,
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    return _account_support_request_response(
        request=request,
        current_user=current_user,
        db=db,
        request_type=SUPPORT_REQUEST_TYPE_ACCOUNT_DELETION,
    )


@router.post("/app/account/billing-switch/cancel")
def creator_billing_switch_cancel(
    request: Request,
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    canceled = cancel_billing_provider_switch_attempt(
        db=db,
        creator_id=current_user.creator_id,
    )
    db.commit()
    status_value = "billing-provider-switch-canceled" if canceled else "billing-provider-switch-missing"
    return _redirect(f"/app/account?status={status_value}")


@router.post("/app/account/billing-switch/commit")
def creator_billing_switch_commit(
    request: Request,
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    switch_attempt = get_billing_provider_switch_attempt(
        db=db,
        creator_id=current_user.creator_id,
    )
    if (
        switch_attempt is not None
        and switch_attempt.target_billing_provider == BILLING_PROVIDER_PAYPAL
        and not _paypal_available_to_creator(
            request=request,
            current_user=current_user,
        )
    ):
        return _redirect("/app/account?status=paypal-unavailable")

    try:
        commit_billing_provider_switch_attempt(
            db=db,
            creator=current_user.creator,
            providers=_ui_billing_providers(request),
        )
    except BillingProviderSwitchError as exc:
        return _redirect(
            f"/app/account?status={_billing_provider_switch_status_value(reason_code=exc.reason_code)}"
        )

    db.commit()
    return _redirect("/app/account?status=billing-provider-switch-committed")


@router.post("/app/account/billing-switch/restart")
def creator_billing_switch_restart(
    request: Request,
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    switch_attempt = get_billing_provider_switch_attempt(
        db=db,
        creator_id=current_user.creator_id,
    )
    if switch_attempt is None:
        return _redirect("/app/account?status=billing-provider-switch-missing")

    try:
        restart_target_provider = switch_attempt.target_billing_provider
        if (
            restart_target_provider == BILLING_PROVIDER_PAYPAL
            and not _paypal_available_to_creator(
                request=request,
                current_user=current_user,
            )
        ):
            return _redirect("/app/account?status=paypal-unavailable")
        restart_billing_provider_switch_attempt(
            db=db,
            creator=current_user.creator,
            target_provider=restart_target_provider,
        )
        if restart_target_provider == BILLING_PROVIDER_PAYPAL:
            start_response = build_paypal_connect_start_response(
                request=request,
                current_user=current_user,
                db=db,
            )
        else:
            start_response = build_stripe_connect_start_response(
                request=request,
                current_user=current_user,
                db=db,
            )
    except BillingProviderSwitchError as exc:
        return _redirect(
            f"/app/account?status={_billing_provider_switch_status_value(reason_code=exc.reason_code)}"
        )

    db.commit()
    return _redirect(str(start_response.onboarding_url))


@router.post("/app/account/paypal/disconnect")
def creator_paypal_disconnect(
    request: Request,
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    switch_attempt = get_billing_provider_switch_attempt(
        db=db,
        creator_id=current_user.creator_id,
    )
    if not _paypal_disconnect_available(
        current_user=current_user,
        switch_attempt=switch_attempt,
    ):
        return _redirect(f"/app/account{ACCOUNT_BILLING_FRAGMENT}")

    current_user.creator.billing_connect_status = "disconnected"
    current_user.creator.billing_connected_at = None
    db.add(current_user.creator)
    db.commit()
    return _redirect(f"/app/account?status=paypal-disconnected{ACCOUNT_BILLING_FRAGMENT}")


@router.get("/app/operator/support-requests")
def operator_support_request_queue_page(
    request: Request,
    status_value: str | None = Query(default=None, alias="status"),
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    operator_user = _allowlisted_operator_from_browser_request(
        request=request,
        current_user=current_user,
    )
    if isinstance(operator_user, Response):
        return operator_user

    support_requests = list_support_requests_for_operator(db)
    return _html_response(
        _render_operator_support_request_queue_page(
            current_user=operator_user,
            support_requests=support_requests,
            status_value=status_value,
        )
    )


@router.get("/app/operator/support-requests/{request_id}")
def operator_support_request_detail_page(
    request_id: uuid.UUID,
    request: Request,
    status_value: str | None = Query(default=None, alias="status"),
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    operator_user = _allowlisted_operator_from_browser_request(
        request=request,
        current_user=current_user,
    )
    if isinstance(operator_user, Response):
        return operator_user

    request_record = get_support_request_by_id(
        db,
        request_id=request_id,
    )
    if request_record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="support request not found")

    return _html_response(
        _render_operator_support_request_detail_page(
            current_user=operator_user,
            request_record=request_record,
            status_value=status_value,
        )
    )


@router.post("/app/operator/support-requests/{request_id}/status")
async def operator_support_request_status_update(
    request_id: uuid.UUID,
    request: Request,
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    operator_user = _allowlisted_operator_from_browser_request(
        request=request,
        current_user=current_user,
    )
    if isinstance(operator_user, Response):
        return operator_user

    request_record = get_support_request_by_id(
        db,
        request_id=request_id,
    )
    if request_record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="support request not found")

    form_values = await _parse_form_values(request)
    try:
        transition_support_request_status(
            db,
            request_record=request_record,
            new_status=form_values.get("status", ""),
        )
    except ValueError:
        return _redirect(f"/app/operator/support-requests/{request_id}?status=invalid-transition")

    return _redirect(f"/app/operator/support-requests/{request_id}?status=status-updated")


@router.post("/app/stripe/connect/start")
def creator_stripe_connect_start(
    request: Request,
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    try:
        start_response = build_stripe_connect_start_response(
            request=request,
            current_user=current_user,
            db=db,
        )
    except BillingProviderSwitchError as exc:
        return _redirect(
            f"/app/account?status={_billing_provider_switch_status_value(reason_code=exc.reason_code)}"
        )
    db.commit()
    return _redirect(str(start_response.onboarding_url))


@router.post("/app/paypal/connect/start")
def creator_paypal_connect_start(
    request: Request,
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    if not _paypal_available_to_creator(
        request=request,
        current_user=current_user,
    ):
        return _redirect("/app/account?status=paypal-unavailable")

    try:
        start_response = build_paypal_connect_start_response(
            request=request,
            current_user=current_user,
            db=db,
        )
    except BillingProviderSwitchError as exc:
        return _redirect(
            f"/app/account?status={_billing_provider_switch_status_value(reason_code=exc.reason_code)}"
        )
    db.commit()
    return _redirect(str(start_response.onboarding_url))


@router.get("/app/booking-links")
def creator_booking_links_page(
    request: Request,
    status_value: str | None = Query(default=None, alias="status"),
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    booking_links = list_booking_link_responses_for_creator(
        creator_id=current_user.creator_id,
        db=db,
    )
    return _html_response(
        _render_booking_links_page(
            current_user=current_user,
            booking_links=booking_links,
            form_values=_empty_booking_link_form_values(),
            field_errors={},
            status_value=status_value,
        )
    )


@router.post("/app/booking-links")
async def creator_booking_links_create(
    request: Request,
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    form_values = _booking_link_form_values(await _parse_form_values(request))
    payload, field_errors = _booking_link_payload_from_form(form_values)
    if field_errors:
        booking_links = list_booking_link_responses_for_creator(
            creator_id=current_user.creator_id,
            db=db,
        )
        return _html_response(
            _render_booking_links_page(
                current_user=current_user,
                booking_links=booking_links,
                form_values=form_values,
                field_errors=field_errors,
                status_value=None,
            )
        )

    created_booking_link = create_booking_link_response_for_creator(
        creator_id=current_user.creator_id,
        payload=payload,
        db=db,
    )
    return _redirect("/app/booking-links?status=created")


@router.get("/app/content")
def creator_content_page(
    request: Request,
    status_value: str | None = Query(default=None, alias="status"),
    created_tid: str | None = Query(default=None, alias="tid"),
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    booking_links = list_booking_link_responses_for_creator(
        creator_id=current_user.creator_id,
        db=db,
    )
    content_items = list_content_responses_for_creator(
        creator_id=current_user.creator_id,
        db=db,
    )
    created_content = None
    if status_value == "created" and created_tid:
        created_content = get_content_response_for_creator_by_tid(
            tid=created_tid,
            creator_id=current_user.creator_id,
            db=db,
        )

    return _html_response(
        _render_content_page(
            current_user=current_user,
            booking_links=booking_links,
            content_items=content_items,
            form_values=_empty_content_form_values(),
            field_errors={},
            status_value=status_value,
            created_content=created_content,
        )
    )


@router.post("/app/content")
async def creator_content_create(
    request: Request,
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    booking_links = list_booking_link_responses_for_creator(
        creator_id=current_user.creator_id,
        db=db,
    )
    content_items = list_content_responses_for_creator(
        creator_id=current_user.creator_id,
        db=db,
    )
    form_values = _content_form_values(await _parse_form_values(request))

    if not booking_links:
        return _html_response(
            _render_content_page(
                current_user=current_user,
                booking_links=booking_links,
                content_items=content_items,
                form_values=form_values,
                field_errors={},
                status_value=None,
                created_content=None,
            )
        )

    payload, field_errors = _content_payload_from_form(form_values)
    if field_errors:
        return _html_response(
            _render_content_page(
                current_user=current_user,
                booking_links=booking_links,
                content_items=content_items,
                form_values=form_values,
                field_errors=field_errors,
                status_value=None,
                created_content=None,
            )
        )

    selected_booking_link_id = str(payload.booking_link_id)
    selected_booking_link = next(
        (
            booking_link
            for booking_link in booking_links
            if booking_link.id == selected_booking_link_id
        ),
        None,
    )
    if (
        selected_booking_link is not None
        and not booking_provider_supports_creator_visible_tracked_content(
            selected_booking_link.provider
        )
    ):
        return _html_response(
            _render_content_page(
                current_user=current_user,
                booking_links=booking_links,
                content_items=content_items,
                form_values=form_values,
                field_errors={
                    "booking_link_id": (
                        "This saved booking source cannot generate tracked content yet. "
                        "Choose a supported tracked destination instead."
                    ),
                },
                status_value=None,
                created_content=None,
            )
        )

    try:
        created_content = create_content_response_for_creator(
            creator_id=current_user.creator_id,
            payload=payload,
            db=db,
        )
    except HTTPException as exc:
        if exc.status_code == status.HTTP_404_NOT_FOUND and exc.detail == "booking link not found":
            return _html_response(
                _render_content_page(
                    current_user=current_user,
                    booking_links=booking_links,
                    content_items=content_items,
                    form_values=form_values,
                    field_errors={
                        "booking_link_id": "Choose one of your saved booking links.",
                    },
                    status_value=None,
                    created_content=None,
                )
            )
        if (
            exc.status_code == status.HTTP_409_CONFLICT
            and exc.detail == "booking link provider not supported for tracked content"
        ):
            return _html_response(
                _render_content_page(
                    current_user=current_user,
                    booking_links=booking_links,
                    content_items=content_items,
                    form_values=form_values,
                    field_errors={
                        "booking_link_id": (
                            "This saved booking source cannot generate tracked content yet. "
                            "Choose a supported tracked destination instead."
                        ),
                    },
                    status_value=None,
                    created_content=None,
                )
            )
        raise

    return _redirect(f"/app/content?status=created&tid={created_content.tid}")


@router.get("/app/content/{tid}/topics")
def creator_content_topic_review_page(
    tid: str,
    request: Request,
    status_value: str | None = Query(default=None, alias="status"),
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    return _content_topic_review_page_response(
        current_user=current_user,
        tid=tid,
        db=db,
        status_value=status_value,
        candidate_field_errors={},
        candidate_form_values={},
    )


@router.post("/app/content/{tid}/topics/candidates")
def creator_content_topic_candidates_create(
    tid: str,
    request: Request,
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    try:
        create_content_topic_candidates_response_for_creator(
            tid=tid,
            creator_id=current_user.creator_id,
            db=db,
            response=Response(),
        )
    except HTTPException as exc:
        if exc.status_code == status.HTTP_409_CONFLICT:
            return _content_topic_review_page_response(
                current_user=current_user,
                tid=tid,
                db=db,
                status_value="unavailable",
                candidate_field_errors={},
                candidate_form_values={},
            )
        raise

    return _redirect(f"/app/content/{quote(tid, safe='')}/topics?status=generated")


@router.post("/app/content/{tid}/topics/{candidate_id}/confirm")
async def creator_content_topic_candidate_confirm(
    tid: str,
    candidate_id: uuid.UUID,
    request: Request,
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    form_values = await _parse_form_values(request)
    confirmed_label = form_values.get("confirmed_label", "").strip()
    if not confirmed_label:
        return _content_topic_review_page_response(
            current_user=current_user,
            tid=tid,
            db=db,
            status_value=None,
            candidate_field_errors={str(candidate_id): "Enter a topic label before saving."},
            candidate_form_values={str(candidate_id): confirmed_label},
        )

    try:
        payload = ContentTopicCandidateConfirmRequest(confirmed_label=confirmed_label)
        confirm_content_topic_candidate_response_for_creator(
            tid=tid,
            candidate_id=candidate_id,
            creator_id=current_user.creator_id,
            payload=payload,
            db=db,
        )
    except ValidationError as exc:
        error_text = exc.errors()[0]["msg"].removeprefix("Value error, ")
        return _content_topic_review_page_response(
            current_user=current_user,
            tid=tid,
            db=db,
            status_value=None,
            candidate_field_errors={str(candidate_id): error_text},
            candidate_form_values={str(candidate_id): confirmed_label},
        )
    except HTTPException as exc:
        if exc.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY:
            return _content_topic_review_page_response(
                current_user=current_user,
                tid=tid,
                db=db,
                status_value=None,
                candidate_field_errors={str(candidate_id): str(exc.detail)},
                candidate_form_values={str(candidate_id): confirmed_label},
            )
        if exc.status_code == status.HTTP_409_CONFLICT:
            return _content_topic_review_page_response(
                current_user=current_user,
                tid=tid,
                db=db,
                status_value="unavailable",
                candidate_field_errors={},
                candidate_form_values={},
            )
        raise

    return _redirect(f"/app/content/{quote(tid, safe='')}/topics?status=saved")


@router.post("/app/content/{tid}/topics/{candidate_id}/reject")
def creator_content_topic_candidate_reject(
    tid: str,
    candidate_id: uuid.UUID,
    request: Request,
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    try:
        reject_content_topic_candidate_response_for_creator(
            tid=tid,
            candidate_id=candidate_id,
            creator_id=current_user.creator_id,
            db=db,
        )
    except HTTPException as exc:
        if exc.status_code == status.HTTP_409_CONFLICT:
            return _content_topic_review_page_response(
                current_user=current_user,
                tid=tid,
                db=db,
                status_value="unavailable",
                candidate_field_errors={},
                candidate_form_values={},
            )
        raise

    return _redirect(f"/app/content/{quote(tid, safe='')}/topics?status=rejected")


@router.post("/app/content/{tid}/topics/promote")
def creator_content_authoritative_evidence_promote(
    tid: str,
    request: Request,
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    try:
        promote_content_authoritative_evidence_response_for_creator(
            tid=tid,
            creator_id=current_user.creator_id,
            db=db,
        )
    except HTTPException as exc:
        if exc.status_code == status.HTTP_409_CONFLICT:
            return _content_topic_review_page_response(
                current_user=current_user,
                tid=tid,
                db=db,
                status_value="promotion-unavailable",
                candidate_field_errors={},
                candidate_form_values={},
            )
        raise

    return _redirect(f"/app/content/{quote(tid, safe='')}/topics?status=promoted")


@router.get("/app/bookings")
def creator_booking_activity_page(
    request: Request,
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    booking_activity = list_booking_activity_responses_for_creator(
        creator_id=current_user.creator_id,
        db=db,
    )
    return _html_response(
        _render_booking_activity_page(
            current_user=current_user,
            booking_activity=booking_activity,
        )
    )


@router.get("/app/reports")
def creator_reports_page(
    request: Request,
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    content_items = list_content_responses_for_creator(
        creator_id=current_user.creator_id,
        db=db,
    )
    booking_links = list_booking_link_responses_for_creator(
        creator_id=current_user.creator_id,
        db=db,
    )
    filter_values = _reports_filter_values(dict(request.query_params))
    start_date, end_date, field_errors = _reports_date_filters_from_values(filter_values)

    overall_summary = get_creator_reports_summary(
        creator_id=current_user.creator_id,
        db=db,
    )
    summary = overall_summary
    if not field_errors:
        try:
            summary = get_creator_reports_summary(
                creator_id=current_user.creator_id,
                db=db,
                start_date=start_date,
                end_date=end_date,
            )
        except ValueError:
            field_errors["date_range"] = "Start date must be on or before end date."

    billing_provider_guidance = _creator_workspace_billing_provider_guidance(
        request=request,
        current_user=current_user,
    )
    readiness = build_creator_workspace_readiness(
        raw_billing_connect_status=current_user.creator.resolved_billing_connect_status,
        raw_billing_provider=current_user.creator.resolved_billing_provider,
        booking_links=booking_links,
        content_items=content_items,
        paid_invoice_count=overall_summary.paid_invoice_count,
        billing_provider_ready=billing_provider_guidance.ready,
        billing_provider_guidance_state=(
            None
            if billing_provider_guidance.state == _BILLING_PROVIDER_SETUP_STATE_NOT_APPLICABLE
            else billing_provider_guidance.state
        ),
        billing_provider_actionable_issue_codes=(
            billing_provider_guidance.actionable_issue_codes
        ),
    )

    return _html_response(
        _render_reports_page(
            current_user=current_user,
            content_items=content_items,
            readiness=readiness,
            summary=summary,
            filter_values=filter_values,
            field_errors=field_errors,
        )
    )


@router.get("/app/reports/topics")
def creator_reports_topics_page(
    request: Request,
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    content_items = list_content_responses_for_creator(
        creator_id=current_user.creator_id,
        db=db,
    )
    filter_values = _reports_filter_values(dict(request.query_params))
    start_date, end_date, field_errors = _reports_date_filters_from_values(filter_values)

    overall_summary = get_creator_reports_topic_summary(
        creator_id=current_user.creator_id,
        db=db,
    )
    summary = overall_summary
    if not field_errors:
        try:
            summary = get_creator_reports_topic_summary(
                creator_id=current_user.creator_id,
                db=db,
                start_date=start_date,
                end_date=end_date,
            )
        except ValueError:
            field_errors["date_range"] = "Start date must be on or before end date."

    return _html_response(
        _render_reports_topics_page(
            current_user=current_user,
            content_items=content_items,
            summary=summary,
            filter_values=filter_values,
            field_errors=field_errors,
        )
    )


@router.get("/app/reports/booking-links")
def creator_reports_booking_links_page(
    request: Request,
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    content_items = list_content_responses_for_creator(
        creator_id=current_user.creator_id,
        db=db,
    )
    booking_links = list_booking_link_responses_for_creator(
        creator_id=current_user.creator_id,
        db=db,
    )
    filter_values = _reports_filter_values(dict(request.query_params))
    start_date, end_date, field_errors = _reports_date_filters_from_values(filter_values)

    overall_summary = get_creator_reports_booking_link_summary(
        creator_id=current_user.creator_id,
        db=db,
    )
    summary = overall_summary
    if not field_errors:
        try:
            summary = get_creator_reports_booking_link_summary(
                creator_id=current_user.creator_id,
                db=db,
                start_date=start_date,
                end_date=end_date,
            )
        except ValueError:
            field_errors["date_range"] = "Start date must be on or before end date."

    return _html_response(
        _render_reports_booking_links_page(
            current_user=current_user,
            content_items=content_items,
            booking_links=booking_links,
            summary=summary,
            filter_values=filter_values,
            field_errors=field_errors,
        )
    )


@router.get("/app/reports/content/{tid}")
def creator_reports_content_drilldown_page(
    tid: str,
    request: Request,
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    filter_values = _reports_filter_values(dict(request.query_params))
    start_date, end_date, field_errors = _reports_date_filters_from_values(filter_values)
    if field_errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=_reports_filter_error_detail(field_errors),
        )

    drilldown = get_creator_reports_content_drilldown(
        creator_id=current_user.creator_id,
        tid=tid,
        db=db,
        start_date=start_date,
        end_date=end_date,
    )
    if drilldown is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="report drilldown not found",
        )

    return _html_response(
        _render_reports_content_drilldown_page(
            current_user=current_user,
            drilldown=drilldown,
            filter_values=filter_values,
        )
    )


@router.get("/app/experiments")
def creator_experiments_page(
    request: Request,
    status_value: str | None = Query(default=None, alias="status"),
    claim_snapshot_id: uuid.UUID | None = Query(default=None, alias="claim_snapshot_id"),
    operator_draft_run_id: uuid.UUID | None = Query(
        default=None,
        alias="operator_draft_run_id",
    ),
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    readiness_summary = get_current_creator_next_content_experiments_readiness_summary(
        creator_id=current_user.creator_id,
        db=db,
    )
    experiment_run = (
        get_creator_next_content_experiments_run(
            creator_id=current_user.creator_id,
            claim_snapshot_id=claim_snapshot_id,
            db=db,
        )
        if claim_snapshot_id is not None
        else readiness_summary.latest_run
    )
    if claim_snapshot_id is not None and experiment_run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="experiment snapshot not found",
        )
    operator_can_review_drafts = browser_auth_user_is_allowlisted_operator(
        current_user,
        settings=_request_settings(request),
    )
    operator_draft_run = None
    operator_draft_provider = _operator_experiment_draft_provider(request)
    if operator_can_review_drafts:
        operator_draft_run = (
            get_creator_operator_experiment_draft_run(
                creator_id=current_user.creator_id,
                draft_run_id=operator_draft_run_id,
                db=db,
            )
            if operator_draft_run_id is not None
            else get_latest_creator_operator_experiment_draft_run(
                creator_id=current_user.creator_id,
                db=db,
            )
        )
        if operator_draft_run_id is not None and operator_draft_run is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="operator draft run not found",
            )

    return _html_response(
        _render_experiments_page(
            current_user=current_user,
            experiment_run=experiment_run,
            status_value=status_value,
            readiness_summary=readiness_summary,
            showing_specific_snapshot=claim_snapshot_id is not None,
            operator_can_review_drafts=operator_can_review_drafts,
            operator_draft_provider_configured=operator_draft_provider.is_configured(),
            operator_draft_run=operator_draft_run,
            showing_specific_operator_draft=operator_draft_run_id is not None,
        )
    )


@router.post("/app/experiments")
def creator_experiments_generate(
    request: Request,
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    experiment_run = create_creator_next_content_experiments_run(
        creator_id=current_user.creator_id,
        db=db,
    )
    db.commit()
    return _redirect(
        f"/app/experiments?status=generated&claim_snapshot_id={experiment_run.claim_snapshot_id}"
    )


@router.post("/app/operator/experiments/drafts")
def operator_experiments_draft_generate(
    request: Request,
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    operator_user = _allowlisted_operator_from_browser_request(
        request=request,
        current_user=current_user,
    )
    if isinstance(operator_user, Response):
        return operator_user

    try:
        draft_run = create_creator_operator_experiment_draft_run(
            creator_id=operator_user.creator_id,
            creator_name=operator_user.creator.name,
            db=db,
            provider=_operator_experiment_draft_provider(request),
        )
    except OperatorExperimentDraftUnavailableError:
        db.rollback()
        return _redirect("/app/experiments?status=operator-draft-unavailable")
    except OperatorExperimentDraftProviderError as exc:
        db.rollback()
        logger.warning(
            "operator_experiment_draft_generation_failed creator_id=%s error=%s",
            operator_user.creator_id,
            exc,
            exc_info=True,
        )
        return _redirect("/app/experiments?status=operator-draft-failed")
    except OperatorExperimentDraftNotReadyError:
        db.rollback()
        return _redirect("/app/experiments?status=operator-draft-not-ready")

    db.commit()
    return _redirect(
        f"/app/experiments?status=operator-draft-generated&operator_draft_run_id={draft_run.draft_run_id}"
    )


@router.get("/app/experiments/compare")
def creator_experiments_compare_page(
    request: Request,
    baseline_claim_snapshot_id: uuid.UUID = Query(..., alias="baseline_claim_snapshot_id"),
    candidate_claim_snapshot_id: uuid.UUID = Query(..., alias="candidate_claim_snapshot_id"),
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    comparison = compare_creator_next_content_experiments_runs(
        creator_id=current_user.creator_id,
        baseline_claim_snapshot_id=baseline_claim_snapshot_id,
        candidate_claim_snapshot_id=candidate_claim_snapshot_id,
        db=db,
    )
    if comparison is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="experiment comparison not found",
        )

    return _html_response(
        _render_experiment_compare_page(
            current_user=current_user,
            comparison=comparison,
        )
    )


@router.get("/app/experiments/{run_claim_snapshot_id}/cards/{card_order}")
def creator_experiment_card_page(
    request: Request,
    run_claim_snapshot_id: uuid.UUID,
    card_order: int,
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    drilldown = get_creator_next_content_experiment_card_drilldown(
        creator_id=current_user.creator_id,
        run_claim_snapshot_id=run_claim_snapshot_id,
        card_order=card_order,
        db=db,
    )
    if drilldown is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="experiment card not found",
        )

    return _html_response(
        _render_experiment_card_drilldown_page(
            current_user=current_user,
            drilldown=drilldown,
        )
    )


@router.get("/app/experiments/{run_claim_snapshot_id}/cards/by-id/{card_id}")
def creator_experiment_card_by_id_page(
    request: Request,
    run_claim_snapshot_id: uuid.UUID,
    card_id: str,
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    drilldown = get_creator_next_content_experiment_card_drilldown_by_card_id(
        creator_id=current_user.creator_id,
        run_claim_snapshot_id=run_claim_snapshot_id,
        card_id=card_id,
        db=db,
    )
    if drilldown is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="experiment card not found",
        )

    return _html_response(
        _render_experiment_card_drilldown_page(
            current_user=current_user,
            drilldown=drilldown,
        )
    )


@router.get("/app/attention")
def creator_attention_page(
    request: Request,
    status_value: str | None = Query(default=None, alias="status"),
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    blocked_cases = list_open_blocked_billing_cases(
        creator_id=current_user.creator_id,
        db=db,
    )
    unmatched_events = list_current_unmatched_payment_events(
        creator_id=current_user.creator_id,
        db=db,
    )
    return _html_response(
        _render_attention_page(
            current_user=current_user,
            blocked_cases=blocked_cases,
            unmatched_events=unmatched_events,
            status_value=status_value,
        )
    )


@router.get("/app/health")
def creator_health_page(
    request: Request,
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    snapshot = get_creator_evidence_ingress_health_snapshot(
        creator_id=current_user.creator_id,
        db=db,
    )
    return _html_response(
        _render_health_page(
            current_user=current_user,
            snapshot=snapshot,
        )
    )


@router.post("/app/attention/blocked-billing/{case_id}/retry")
def creator_attention_retry_blocked_billing_case(
    case_id: uuid.UUID,
    request: Request,
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    retry_service = BlockedBillingRetryService(
        session_factory=SessionLocal,
        providers=_ui_billing_providers(request),
    )
    retry_result = retry_service.retry_case(
        case_id=case_id,
        creator_id=current_user.creator_id,
    )
    if retry_result.outcome == "missing":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="blocked billing case not found",
        )

    status_value = {
        "created": "recovered",
        "existing": "already-recovered",
        "still_blocked": "still-blocked",
        "already_resolved": "already-handled",
        "closed": "closed",
    }[retry_result.outcome]
    return _redirect(f"/app/attention?status={status_value}")


@router.get("/app/reports/explanations/paid/{tid}")
def creator_reports_paid_explanation_page(
    tid: str,
    request: Request,
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    filter_values = _reports_filter_values(dict(request.query_params))
    start_date, end_date, field_errors = _reports_date_filters_from_values(filter_values)
    if field_errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=_reports_filter_error_detail(field_errors),
        )

    explanation = get_creator_paid_attribution_explanation(
        creator_id=current_user.creator_id,
        tid=tid,
        db=db,
        start_date=start_date,
        end_date=end_date,
    )
    if explanation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="report explanation not found",
        )

    return _html_response(
        _render_reports_paid_explanation_page(
            current_user=current_user,
            explanation=explanation,
            filter_values=filter_values,
        )
    )


@router.get("/app/reports/explanations/unattributed")
def creator_reports_unattributed_explanation_page(
    request: Request,
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    filter_values = _reports_filter_values(dict(request.query_params))
    summary = get_creator_reports_summary(
        creator_id=current_user.creator_id,
        db=db,
    )
    return _html_response(
        _render_reports_unattributed_explanation_page(
            current_user=current_user,
            summary=summary,
            filter_values=filter_values,
        )
    )


@router.get("/app/reports/export.csv")
def creator_reports_csv_export(
    request: Request,
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    filter_values = _reports_filter_values(dict(request.query_params))
    start_date, end_date, field_errors = _reports_date_filters_from_values(filter_values)
    if field_errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=_reports_filter_error_detail(field_errors),
        )

    summary = get_creator_reports_summary(
        creator_id=current_user.creator_id,
        db=db,
        start_date=start_date,
        end_date=end_date,
    )
    response = Response(
        content=build_reports_summary_csv(summary),
        media_type="text/csv",
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Disposition"] = (
        f'attachment; filename="{_reports_csv_filename(start_date=start_date, end_date=end_date)}"'
    )
    return response


@router.post("/sign-out")
def sign_out() -> RedirectResponse:
    return _redirect("/sign-in", clear_session=True)


async def _parse_form_values(request: Request) -> dict[str, str]:
    parsed = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    return {
        key: values[-1]
        for key, values in parsed.items()
    }


def _redirect(url: str, *, clear_session: bool = False) -> RedirectResponse:
    response = RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)
    response.headers["Cache-Control"] = "no-store"
    if clear_session:
        clear_browser_session_cookie(response, settings=get_settings())
    return response


def _html_response(content: str) -> HTMLResponse:
    response = HTMLResponse(content=content)
    response.headers["Cache-Control"] = "no-store"
    return response


def _ui_stripe_provider(request: Request):
    return getattr(request.app.state, "stripe_provider", build_default_stripe_provider())


def _ui_billing_providers(request: Request):
    settings = getattr(request.app.state, "settings", get_settings())
    return build_billing_provider_registry(
        providers=[
            _ui_stripe_provider(request),
            getattr(
                request.app.state,
                "paypal_provider",
                build_default_paypal_provider(settings=settings),
            ),
        ]
    )


def _billing_provider_setup_guidance(
    *,
    request: Request,
    provider_name: str | None,
    provider_account_id: str | None,
) -> _BillingProviderSetupGuidance:
    if not provider_name or not provider_account_id:
        return _BillingProviderSetupGuidance(
            state=_BILLING_PROVIDER_SETUP_STATE_NOT_APPLICABLE
        )

    try:
        provider = resolve_billing_provider(
            providers=_ui_billing_providers(request),
            provider_name=provider_name,
        )
        readiness = get_billing_account_readiness(
            provider=provider,
            provider_account_id=provider_account_id,
        )
    except (BillingProviderError, BillingProviderResolutionError, TypeError):
        return _BillingProviderSetupGuidance(
            state=_BILLING_PROVIDER_SETUP_STATE_BLOCKED
        )

    return _BillingProviderSetupGuidance(
        state=(
            _BILLING_PROVIDER_SETUP_STATE_READY
            if readiness.can_create_invoices
            else _BILLING_PROVIDER_SETUP_STATE_NOT_READY
        ),
        readiness=readiness,
    )


def _creator_workspace_billing_provider_guidance(
    *,
    request: Request,
    current_user: AuthUser,
) -> _BillingProviderSetupGuidance:
    creator = current_user.creator
    if creator.resolved_billing_provider != BILLING_PROVIDER_PAYPAL:
        return _BillingProviderSetupGuidance(
            state=_BILLING_PROVIDER_SETUP_STATE_NOT_APPLICABLE
        )
    if (creator.resolved_billing_connect_status or "").strip().lower() != "connected":
        return _BillingProviderSetupGuidance(
            state=_BILLING_PROVIDER_SETUP_STATE_NOT_APPLICABLE
        )

    return _billing_provider_setup_guidance(
        request=request,
        provider_name=creator.resolved_billing_provider,
        provider_account_id=creator.resolved_billing_account_id,
    )


def _empty_booking_link_form_values() -> dict[str, str]:
    form_values = {field_name: "" for field_name in BOOKING_LINK_FORM_FIELDS}
    form_values["provider"] = BOOKING_PROVIDER_CALENDLY
    return form_values


BOOKING_LINK_ROOT_ERROR_FIELD_MAP = {
    "provide a destination URL": "destination_url",
    "destination_url and calendly_url must match when both are provided": "destination_url",
    "FullScope requests must use destination_url, not calendly_url": "destination_url",
    "must be a valid absolute URL": "destination_url",
    "must use https": "destination_url",
    "must use calendly.com": "destination_url",
    "must include a Calendly path": "destination_url",
    "must use links.fullscope.tools": "destination_url",
    "must use a Personal Calendar or direct Service Calendar link": "destination_url",
    "must use a FullScope booking URL": "destination_url",
    "must include a FullScope booking path": "destination_url",
}


def _booking_link_form_values(raw_values: dict[str, str]) -> dict[str, str]:
    destination_url = raw_values.get("destination_url", "").strip()
    legacy_calendly_url = raw_values.get("calendly_url", "").strip()
    form_values = _empty_booking_link_form_values()
    form_values.update(
        {
            "provider": raw_values.get("provider", BOOKING_PROVIDER_CALENDLY).strip().lower()
            or BOOKING_PROVIDER_CALENDLY,
            "name": raw_values.get("name", "").strip(),
            # Keep the legacy browser form field working for older validation flows
            # that still post `calendly_url` directly.
            "destination_url": destination_url or legacy_calendly_url,
            "fullscope_supported_calendar_confirmed": (
                "true"
                if raw_values.get("fullscope_supported_calendar_confirmed", "").strip().lower()
                in {"true", "on", "1", "yes"}
                else ""
            ),
            "billing_amount_cents": raw_values.get("billing_amount_cents", "").strip(),
            "billing_currency": raw_values.get("billing_currency", "").strip(),
        }
    )
    return form_values


def _booking_link_payload_from_form(
    form_values: dict[str, str],
) -> tuple[BookingLinkCreateRequest | None, dict[str, str]]:
    field_errors: dict[str, str] = {}
    billing_amount_cents: int | None = None

    if form_values["provider"] not in CREATOR_VISIBLE_BOOKING_LINK_PROVIDERS:
        field_errors["provider"] = "This booking provider is not available in creator setup right now."

    if form_values["billing_amount_cents"]:
        try:
            billing_amount_cents = int(form_values["billing_amount_cents"])
        except ValueError:
            field_errors["billing_amount_cents"] = "Enter a whole number of cents."

    try:
        payload = BookingLinkCreateRequest(
            provider=form_values["provider"],
            name=form_values["name"],
            destination_url=form_values["destination_url"] or None,
            fullscope_supported_calendar_confirmed=(
                form_values["fullscope_supported_calendar_confirmed"] == "true"
            ),
            billing_amount_cents=billing_amount_cents,
            billing_currency=form_values["billing_currency"] or None,
        )
    except ValidationError as exc:
        for field_name, message in _booking_link_field_errors(exc).items():
            field_errors.setdefault(field_name, message)
        return None, field_errors

    if field_errors:
        return None, field_errors

    return payload, {}


def _booking_link_field_errors(exc: ValidationError) -> dict[str, str]:
    errors: dict[str, str] = {}
    for error in exc.errors():
        location = error.get("loc") or ()
        field_name = str(location[-1]) if location else ""
        if not field_name:
            message = error["msg"].removeprefix("Value error, ")
            mapped_field_name = BOOKING_LINK_ROOT_ERROR_FIELD_MAP.get(message)
            if mapped_field_name and mapped_field_name not in errors:
                errors[mapped_field_name] = message
            continue
        if field_name in BOOKING_LINK_FORM_FIELDS and field_name not in errors:
            errors[field_name] = error["msg"].removeprefix("Value error, ")
    return errors


def _empty_content_form_values() -> dict[str, str]:
    return {field_name: "" for field_name in CONTENT_FORM_FIELDS}


def _content_form_values(raw_values: dict[str, str]) -> dict[str, str]:
    form_values = _empty_content_form_values()
    form_values.update(
        {
            "source_url": raw_values.get("source_url", "").strip(),
            "booking_link_id": raw_values.get("booking_link_id", "").strip(),
        }
    )
    return form_values


def _empty_reports_filter_values() -> dict[str, str]:
    return {field_name: "" for field_name in REPORT_FILTER_FIELDS}


def _reports_filter_values(raw_values: dict[str, str]) -> dict[str, str]:
    form_values = _empty_reports_filter_values()
    form_values.update(
        {
            "start_date": raw_values.get("start_date", "").strip(),
            "end_date": raw_values.get("end_date", "").strip(),
        }
    )
    return form_values


def _reports_date_filters_from_values(
    filter_values: dict[str, str],
) -> tuple[date | None, date | None, dict[str, str]]:
    field_errors: dict[str, str] = {}
    start_date = _parse_optional_reports_date(
        raw_value=filter_values["start_date"],
        field_name="start_date",
        field_errors=field_errors,
    )
    end_date = _parse_optional_reports_date(
        raw_value=filter_values["end_date"],
        field_name="end_date",
        field_errors=field_errors,
    )

    if (
        "start_date" not in field_errors
        and "end_date" not in field_errors
        and start_date is not None
        and end_date is not None
        and start_date > end_date
    ):
        field_errors["date_range"] = "Start date must be on or before end date."

    return start_date, end_date, field_errors


def _parse_optional_reports_date(
    *,
    raw_value: str,
    field_name: str,
    field_errors: dict[str, str],
) -> date | None:
    if not raw_value:
        return None

    try:
        return date.fromisoformat(raw_value)
    except ValueError:
        field_errors[field_name] = "Use a valid date in YYYY-MM-DD format."
        return None


def _content_payload_from_form(
    form_values: dict[str, str],
) -> tuple[ContentCreateRequest | None, dict[str, str]]:
    field_errors: dict[str, str] = {}

    if not form_values["source_url"]:
        field_errors["source_url"] = "Enter a full public URL starting with http or https."
    if not form_values["booking_link_id"]:
        field_errors["booking_link_id"] = "Choose one of your saved booking links."

    if field_errors:
        return None, field_errors

    try:
        payload = ContentCreateRequest(
            source_url=form_values["source_url"],
            booking_link_id=form_values["booking_link_id"],
        )
    except ValidationError as exc:
        return None, _content_field_errors(exc)

    return payload, {}


def _content_field_errors(exc: ValidationError) -> dict[str, str]:
    errors: dict[str, str] = {}
    for error in exc.errors():
        location = error.get("loc") or ()
        field_name = str(location[-1]) if location else ""
        if field_name == "source_url" and field_name not in errors:
            errors[field_name] = "Enter a full public URL starting with http or https."
        elif field_name == "booking_link_id" and field_name not in errors:
            errors[field_name] = "Choose one of your saved booking links."
    return errors


def _content_topic_prerequisite_copy(detail: str) -> str:
    if detail == "content extraction artifact required":
        return (
            "Run fetch and extract for this tracked content first so topic review starts from "
            "the canonical extraction artifact instead of a second ingestion path."
        )
    if detail == "usable content extraction artifact required":
        return (
            "The latest extraction artifact does not contain usable text yet. Re-run fetch and "
            "extract before reviewing or confirming topics for this content item."
        )
    if detail == "topic candidates unavailable for this extraction artifact":
        return (
            "This extraction artifact did not produce candidate topics yet. Update the source "
            "content or extraction quality, then try again."
        )
    return str(detail)


def _content_topic_review_page_response(
    *,
    current_user: AuthUser,
    tid: str,
    db: Session,
    status_value: str | None,
    candidate_field_errors: dict[str, str],
    candidate_form_values: dict[str, str],
) -> HTMLResponse:
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

    review: ContentTopicReviewResponse | None = None
    prerequisite_detail: str | None = None
    try:
        review = get_content_topic_review_response_for_creator(
            tid=tid,
            creator_id=current_user.creator_id,
            db=db,
        )
    except HTTPException as exc:
        if exc.status_code != status.HTTP_409_CONFLICT:
            raise
        prerequisite_detail = _content_topic_prerequisite_copy(str(exc.detail))

    return _html_response(
        _render_content_topic_review_page(
            current_user=current_user,
            content=content,
            review=review,
            status_value=status_value,
            candidate_field_errors=candidate_field_errors,
            candidate_form_values=candidate_form_values,
            prerequisite_detail=prerequisite_detail,
        )
    )


def _sign_in_path(*, status_value: str | None = None) -> str:
    if not status_value:
        return "/sign-in"
    return f"/sign-in?status={quote(status_value, safe='')}"


def _render_public_home_page() -> str:
    body = f"""
    <header class="public-nav">
      <a href="/" class="brand-link">Career Code Pro</a>
      <nav aria-label="Landing page sections">
        <a href="#benefits">Benefits</a>
        <a href="#how-it-works">How it works</a>
        <a href="#faqs">FAQs</a>
      </nav>
      <div class="public-nav-actions">
        <a href="/sign-in" class="inline-link">Sign in</a>
        <a href="/sign-in" class="button-link">Create workspace</a>
      </div>
    </header>

    <section class="landing-hero">
      <div class="landing-hero-copy">
        <p class="eyebrow">The workspace for independent tutors</p>
        <h1>Know what's actually bringing in paid students.</h1>
        <p class="lede">Connect your booking links, content, and outreach to confirmed paid bookings so you stop guessing where your students came from.</p>
        <div class="hero-actions">
          <a href="/sign-in" class="button-link">Create your workspace</a>
          <p class="footnote">Free to start. Takes about 30 seconds. No credit card required.</p>
        </div>
      </div>
      <div class="product-mockup" aria-label="Static preview of a Career Code Pro paid-booking report">
        <div class="mockup-window">
          <div class="mockup-topbar">
            <span></span>
            <span></span>
            <span></span>
          </div>
          <div class="mockup-header">
            <p class="eyebrow">Paid result</p>
            <h2>What was attached?</h2>
          </div>
          <div class="mockup-stat-row">
            <div>
              <strong>$125.00</strong>
              <span>Paid booking</span>
            </div>
            <div>
              <strong>3</strong>
              <span>Sources attached</span>
            </div>
            <div>
              <strong>PayPal</strong>
              <span>Confirmed</span>
            </div>
          </div>
          <div class="mockup-body">
            <div class="mockup-chart" aria-hidden="true">
              <span style="height: 38%"></span>
              <span style="height: 62%"></span>
              <span style="height: 47%"></span>
              <span style="height: 82%"></span>
              <span style="height: 55%"></span>
              <span style="height: 74%"></span>
            </div>
            <div class="mockup-sources">
              <p><strong>Booking link</strong><span>Calculus consult</span></p>
              <p><strong>Content</strong><span>AP exam prep guide</span></p>
              <p><strong>Outreach</strong><span>Parent email follow-up</span></p>
            </div>
          </div>
        </div>
      </div>
    </section>

    <section id="benefits" class="landing-section card stack">
      <div class="section-heading-centered">
        <p class="eyebrow">Tutor business clarity</p>
        <h2>Run your tutoring like a real business.</h2>
        <p>See exactly which booking links, content, and outreach show up next to your paid bookings without spreadsheets or guesswork.</p>
      </div>
      <div class="benefit-grid">
        <article class="benefit-card">
          <p class="benefit-icon">01</p>
          <h3>Stop guessing where students came from.</h3>
          <p>Every paid booking can show the links, content, and outreach attached to it so you can see what is actually working.</p>
        </article>
        <article class="benefit-card">
          <p class="benefit-icon">02</p>
          <h3>Trust the numbers, not your memory.</h3>
          <p>Results come from confirmed PayPal payments, not vanity metrics or self-reported guesses.</p>
        </article>
        <article class="benefit-card">
          <p class="benefit-icon">03</p>
          <h3>Spend your time on what works.</h3>
          <p>Use real paid-result evidence to decide what to share, improve, or stop doing next.</p>
        </article>
      </div>
    </section>

    <section id="how-it-works" class="landing-section how-it-works">
      <div class="how-copy">
        <p class="eyebrow">How it works</p>
        <h2>A short setup checklist gets your workspace ready before your next paid booking arrives.</h2>
        <a href="/sign-in" class="button-link">Create your workspace</a>
      </div>
      <ol class="how-steps">
        <li>
          <span>1</span>
          <div>
            <h3>Connect your PayPal account.</h3>
            <p>Use your own PayPal account so confirmed payments can appear as paid results in your workspace.</p>
          </div>
        </li>
        <li>
          <span>2</span>
          <div>
            <h3>Create and share tracked links.</h3>
            <p>Add booking links and tracked content or outreach links before students book with you.</p>
          </div>
        </li>
        <li>
          <span>3</span>
          <div>
            <h3>Review what was attached to a paid booking.</h3>
            <p>When a student pays, review the booking link, content, and outreach connected to that result.</p>
          </div>
        </li>
      </ol>
    </section>

    <section class="trust-panel">
      <div>
        <p class="eyebrow">Payment-account trust</p>
        <h2>Your PayPal stays yours.</h2>
        <p>You connect your existing PayPal account. Career Code Pro never holds your money or processes payouts.</p>
      </div>
      <div class="trust-list">
        <p>Payouts go directly to you, never through us.</p>
        <p>We only read confirmed payment-backed records for reporting.</p>
        <p>You can disconnect anytime in your account settings.</p>
        <a href="/sign-in" class="button-link trust-cta">Create your workspace</a>
      </div>
    </section>

    <section id="faqs" class="landing-section faq-section">
      <div>
        <p class="eyebrow">FAQs</p>
        <h2>Quick answers before you get started.</h2>
      </div>
      <div class="faq-grid">
        <article>
          <h3>Do I need to be technical to use this?</h3>
          <p>No. If you can copy and paste a link, you can use Career Code Pro. The setup starts from your email and PayPal account.</p>
        </article>
        <article>
          <h3>What if I already use PayPal?</h3>
          <p>You can connect your existing PayPal account. You keep using PayPal for payouts while Career Code Pro organizes paid-result context.</p>
        </article>
        <article>
          <h3>How long until I see my first paid result?</h3>
          <p>As soon as a student pays through a connected setup and the booking can be matched, the result can appear in reports.</p>
        </article>
        <article>
          <h3>Is my data shared or sold?</h3>
          <p>No. Your booking, content, payment, and student information stays in your workspace and is not sold to third parties.</p>
        </article>
        <article>
          <h3>Can I cancel and take my data with me?</h3>
          <p>You can disconnect PayPal anytime, and your historical workspace records remain available during beta review.</p>
        </article>
      </div>
      <p class="footnote">{_PUBLIC_LEGAL_LINKS_HTML}</p>
    </section>
    """
    return _page_layout(title="Career Code Pro for independent tutors", body=body)


def _render_terms_page() -> str:
    body = f"""
    <section class="hero stack">
      <div>
        <p class="eyebrow">Terms and Conditions</p>
        <h1>Career Code Pro Terms and Conditions</h1>
        <p class="lede">These terms govern use of the hosted Career Code Pro workspace for independent tutors and other creators who manage booking links, tracked content, billing setup, and reporting through this website.</p>
      </div>
      <section class="card stack">
        <div>
          <p class="eyebrow">Account use</p>
          <h2>Workspace access</h2>
        </div>
        <p>You are responsible for using a valid email address and for keeping access to your mailbox secure. Access to the workspace is tied to the email-based sign-in flow used on this website.</p>
        <p>During beta, we may suspend or limit access if a workspace is used for prohibited, fraudulent, or abusive activity, or if continued access would create operational, legal, or payment-provider risk.</p>
      </section>
      <section class="grid">
        <article class="card stack">
          <div>
            <p class="eyebrow">Payments</p>
            <h2>Your payment account</h2>
          </div>
          <p>Independent tutors use their own connected payment-provider account to accept payments for their services. Career Code Pro does not take ownership of that payment account.</p>
          <p>Disconnecting a payment provider in this workspace affects future billing readiness only. It does not automatically delete or reverse provider-side records, dashboards, or historical transactions.</p>
        </article>
        <article class="card stack">
          <div>
            <p class="eyebrow">Content and links</p>
            <h2>Your sources and bookings</h2>
          </div>
          <p>You are responsible for the destination URLs, tracked content, booking links, and other source material you place into the workspace. You must have the right to use that material and the right to offer the services advertised through it.</p>
          <p>You must not use the service for unlawful conduct, deceptive claims, payment abuse, or prohibited content.</p>
        </article>
      </section>
      <section class="card stack">
        <div>
          <p class="eyebrow">Beta operations</p>
          <h2>Manual review and support</h2>
        </div>
        <p>Some destructive changes, including workspace reset and account deletion, stay support-assisted during beta. Submitting a request does not guarantee an immediate change.</p>
        <p>Questions about these terms can be sent to <a href="mailto:eric@careercodepro.com">eric@careercodepro.com</a>.</p>
        <p class="footnote"><a href="/" class="inline-link">Return to the hosted onboarding page</a> {_PUBLIC_LEGAL_LINKS_HTML}</p>
      </section>
    </section>
    """
    return _page_layout(title="Career Code Pro Terms and Conditions", body=body)


def _render_privacy_page() -> str:
    body = f"""
    <section class="hero stack">
      <div>
        <p class="eyebrow">Privacy Policy</p>
        <h1>Career Code Pro Privacy Policy</h1>
        <p class="lede">This policy explains the data Career Code Pro stores and uses to run tutor workspaces, billing setup, tracked links, booking attribution, and reporting on this website.</p>
      </div>
      <section class="grid">
        <article class="card stack">
          <div>
            <p class="eyebrow">What we store</p>
            <h2>Workspace data</h2>
          </div>
          <p>We store account email addresses, creator names, booking links, tracked content URLs, booking activity, billing-connection metadata, support-request records, and provider event data needed to operate the product and explain what happened in a workspace.</p>
        </article>
        <article class="card stack">
          <div>
            <p class="eyebrow">How we use it</p>
            <h2>Product operations</h2>
          </div>
          <p>We use that data to authenticate creators, create and reconnect billing setups, attribute tracked bookings, prepare reports, surface attention items, and respond to support-assisted account requests.</p>
        </article>
      </section>
      <section class="grid">
        <article class="card stack">
          <div>
            <p class="eyebrow">Third-party services</p>
            <h2>Payment and booking providers</h2>
          </div>
          <p>Depending on the configuration used for a workspace, the product may exchange operational data with providers such as PayPal, Stripe, Calendly, and email-delivery services needed for sign-in or support notifications.</p>
          <p>Those providers operate under their own terms and privacy practices.</p>
        </article>
        <article class="card stack">
          <div>
            <p class="eyebrow">Retention and deletion</p>
            <h2>Beta deletion handling</h2>
          </div>
          <p>During beta, account deletion and workspace reset remain manual-review workflows. Provider-side accounts and records are not deleted automatically when a workspace is disconnected or when a deletion request is submitted.</p>
          <p>Questions about privacy or data handling can be sent to <a href="mailto:eric@careercodepro.com">eric@careercodepro.com</a>.</p>
        </article>
      </section>
      <p class="footnote"><a href="/" class="inline-link">Return to the hosted onboarding page</a> {_PUBLIC_LEGAL_LINKS_HTML}</p>
    </section>
    """
    return _page_layout(title="Career Code Pro Privacy Policy", body=body)


def _render_sign_in_page(status_value: str | None) -> str:
    message = STATUS_MESSAGES.get(status_value)
    message_block = ""
    if message is not None:
        message_block = (
            f'<section class="{html.escape(message["notice_class"])}">'
            f"<p class=\"eyebrow\">{html.escape(message['title'])}</p>"
            f"<p>{html.escape(message['body'])}</p>"
            f"</section>"
        )

    body = f"""
    <section class="sign-in-shell">
      <section class="hero sign-in-card">
        <p class="eyebrow">Getting started</p>
        <h1>Start or reopen your tutor workspace.</h1>
        <p class="lede">Enter your email to request a secure sign-in link for your workspace.</p>
        {message_block}
        <form action="/sign-in" method="post" class="sign-in-form">
          <label for="email">Email address</label>
          <input id="email" name="email" type="email" autocomplete="email" placeholder="you@example.com" required />
          <button type="submit">Send sign-in link</button>
        </form>
        <p class="form-help sign-in-guidance">For best results, open the email on this same device and browser where you requested it.</p>
        <p class="footnote">{_PUBLIC_LEGAL_LINKS_HTML}</p>
      </section>
    </section>
    """
    return _page_layout(title="Creator sign in", body=body)


def _render_app_shell(
    *,
    current_user: AuthUser,
    workspace_state: CreatorWorkspaceState,
    reports_summary: CreatorReportsSummary,
    status_value: str | None,
    paypal_available_to_creator: bool,
    experiments_readiness_summary: CreatorNextContentExperimentsReadinessSummary,
    growth_loop_agent_feature_enabled: bool,
) -> str:
    readiness = workspace_state.readiness
    show_provider_choice = _creator_needs_initial_billing_provider_choice(
        creator=current_user.creator,
        readiness=readiness,
    )
    setup_progress = _build_setup_home_progress(
        workspace_state=workspace_state,
        show_provider_choice=show_provider_choice,
        paypal_available_to_creator=paypal_available_to_creator,
    )
    tracked_booking_count = sum(row.booking_count for row in reports_summary.rows)
    progressed_setup_state = _setup_home_is_progressed_state(
        readiness=readiness,
    )
    if progressed_setup_state:
        setup_primary_section = _render_setup_home_milestone_section(
            current_user=current_user,
            workspace_state=workspace_state,
            setup_progress=setup_progress,
            tracked_booking_count=tracked_booking_count,
            show_provider_choice=show_provider_choice,
            paypal_available_to_creator=paypal_available_to_creator,
            experiments_readiness_summary=experiments_readiness_summary,
        )
        setup_secondary_section = f"""
        <section class="grid setup-secondary-grid">
          {_render_setup_home_checklist_card(
              setup_progress=setup_progress,
              compact=True,
          )}
          {_render_setup_home_workspace_proof_card(
              current_user=current_user,
              setup_progress=setup_progress,
              readiness=readiness,
              tracked_booking_count=tracked_booking_count,
          )}
        </section>
        """
    else:
        setup_primary_section = _render_setup_home_checklist_hero(
            current_user=current_user,
            workspace_state=workspace_state,
            setup_progress=setup_progress,
            tracked_booking_count=tracked_booking_count,
            show_provider_choice=show_provider_choice,
            paypal_available_to_creator=paypal_available_to_creator,
        )
        setup_secondary_section = f"""
        <section class="grid setup-secondary-grid">
          {_render_setup_home_workspace_proof_card(
              current_user=current_user,
              setup_progress=setup_progress,
              readiness=readiness,
              tracked_booking_count=tracked_booking_count,
          )}
        </section>
        """

    body = f"""
    <header class="shell-header">
      <div>
        <p class="eyebrow">Creator Home</p>
        <h1>Setup Home</h1>
        <p class="lede">Complete the few setup steps that let booking links turn into trackable paid proof.</p>
      </div>
      <form action="/sign-out" method="post">
        <button type="submit" class="secondary">Sign out</button>
      </form>
    </header>
    {_render_shell_nav(
        current_path="/app",
        growth_loop_agent_feature_enabled=growth_loop_agent_feature_enabled,
    )}
    {_render_setup_home_notice(status_value=status_value)}
    {setup_primary_section}
    {setup_secondary_section}
    """
    return _page_layout(title="Creator Home", body=body)


def _render_bullet_list(values: tuple[str, ...]) -> str:
    if not values:
        return "<p>No diagnostic context is available for this section.</p>"
    items = "".join(f"<li>{html.escape(value)}</li>" for value in values)
    return f'<ul class="reason-list">{items}</ul>'


def _render_growth_loop_agent_page(
    *,
    current_user: AuthUser,
    brief: GrowthLoopActionBrief,
) -> str:
    app_evidence_items = "".join(
        f"""
        <article class="topic-summary stack">
          <div>
            <p class="eyebrow">{html.escape(item.label)}</p>
            <h2>{html.escape(item.value)}</h2>
          </div>
          <p>{html.escape(item.detail)}</p>
        </article>
        """
        for item in brief.app_evidence
    )
    loomi_segments = _render_bullet_list(brief.loomi_context.segments)
    loomi_predictions = _render_bullet_list(brief.loomi_context.predictions)
    loomi_recommendations = _render_bullet_list(brief.loomi_context.recommendations)
    loomi_analytics = _render_bullet_list(brief.loomi_context.analytics)
    limitations = _render_bullet_list(brief.limitations + brief.loomi_context.limitations)
    if brief.loomi_context.source_kind == "live_mcp":
        loomi_demo_copy = (
            "This request is using live Loomi Marketing and Analytics MCP diagnostics. "
            "They inform review; they do not become paid truth."
        )
        loomi_context_copy = (
            "Live Loomi MCP results are mapped into this diagnostic context for review only."
        )
    else:
        loomi_demo_copy = (
            "Local demo diagnostics are fixture-backed and shaped after authenticated Marketing "
            "and Analytics MCP tool families. They inform review; they do not become paid truth."
        )
        loomi_context_copy = (
            "Fixture-backed Loomi context is shown because live MCP is disabled or unavailable."
        )

    body = f"""
    <header class="shell-header">
      <div>
        <p class="eyebrow">Growth Loop Agent</p>
        <h1>Growth Loop Agent</h1>
        <p class="lede">Review one evidence-backed next action using app-owned paid-result evidence and Loomi diagnostic context.</p>
      </div>
      <form action="/sign-out" method="post">
        <button type="submit" class="secondary">Sign out</button>
      </form>
    </header>
    {_render_shell_nav(
        current_path="/app/growth-loop",
        growth_loop_agent_feature_enabled=True,
    )}
    <section class="card stack">
      <div>
        <p class="eyebrow">Cross-system demo map</p>
        <h2>Loomi context, app attribution, and PayPal proof meet here</h2>
        <p>This demo loop reads Loomi Marketing and Analytics MCP-shaped diagnostics, checks this app's tracked content, booking, invoice, and payment evidence, then prepares one action for human review.</p>
      </div>
      <div class="grid">
        <section class="topic-summary stack">
          <div>
            <p class="eyebrow">Loomi read side</p>
            <h2>Diagnostic context</h2>
          </div>
          <p>{html.escape(loomi_demo_copy)}</p>
        </section>
        <section class="topic-summary stack">
          <div>
            <p class="eyebrow">App evidence</p>
            <h2>Attribution truth</h2>
          </div>
          <p>Tracked content, creator-scoped bookings, and canonical paid invoices decide what counted in this workspace.</p>
        </section>
        <section class="topic-summary stack">
          <div>
            <p class="eyebrow">PayPal outcome proof</p>
            <h2>Paid result state</h2>
          </div>
          <p>The local seed uses PayPal-shaped order and capture evidence to show the paid-result state without requiring a live PayPal payment.</p>
        </section>
        <section class="topic-summary stack">
          <div>
            <p class="eyebrow">Review gate</p>
            <h2>Prepared action only</h2>
          </div>
          <p>The agent prepares a next-step brief for review. It does not send campaigns, mutate external systems, or replace reporting totals.</p>
        </section>
      </div>
    </section>
    <section class="grid">
      <article class="card stack">
        <div>
          <p class="eyebrow">Diagnosis</p>
          <h2>{html.escape(brief.diagnosis_title)}</h2>
          <p>{html.escape(brief.diagnosis_summary)}</p>
        </div>
        <section class="topic-summary stack">
          <div class="status-row">
            <h2>{html.escape(brief.next_action_title)}</h2>
            <span class="pill-note">Human review</span>
          </div>
          <p>{html.escape(brief.next_action_summary)}</p>
        </section>
        <section class="topic-summary stack">
          <div>
            <p class="eyebrow">Prepared action</p>
            <h2>{html.escape(brief.prepared_action_title)}</h2>
          </div>
          <p>{html.escape(brief.prepared_action_body)}</p>
          <p><strong>{html.escape(brief.human_review_note)}</strong></p>
        </section>
      </article>
      <article class="card stack">
        <div>
          <p class="eyebrow">Confidence</p>
          <h2>{html.escape(brief.confidence_label)}</h2>
          <p>{html.escape(brief.confidence_summary)}</p>
        </div>
        <section class="topic-summary stack">
          <div>
            <p class="eyebrow">Stage</p>
            <h2>{html.escape(brief.stage.replace("_", " ").title())}</h2>
          </div>
          <p>This stage is rule-backed for Story 124. No live LLM call is required for this slice.</p>
        </section>
      </article>
    </section>
    <section class="card stack">
      <div>
        <p class="eyebrow">Evidence Boundary</p>
        <h2>App-owned evidence stays separate from Loomi diagnostics</h2>
        <p>Counts below come from this app's tracked content, bookings, invoices, and payment-backed records. Loomi context is diagnostic and does not become paid truth.</p>
      </div>
      <div class="grid">
        {app_evidence_items}
      </div>
    </section>
    <section class="grid">
      <article class="card stack">
        <div>
          <div class="status-row">
            <p class="eyebrow">{html.escape(brief.loomi_context.source_label)}</p>
            <span class="pill-note">{html.escape(brief.loomi_context.source_status_label)}</span>
          </div>
          <h2>Loomi diagnostic context</h2>
          <p>{html.escape(loomi_context_copy)}</p>
          <p>{html.escape(brief.loomi_context.source_status_detail)}</p>
        </div>
        <section class="topic-summary stack">
          <h2>Segments</h2>
          {loomi_segments}
        </section>
        <section class="topic-summary stack">
          <h2>Predictions</h2>
          {loomi_predictions}
        </section>
        <section class="topic-summary stack">
          <h2>Recommendations</h2>
          {loomi_recommendations}
        </section>
        <section class="topic-summary stack">
          <h2>Analytics</h2>
          {loomi_analytics}
        </section>
      </article>
      <article class="card stack">
        <div>
          <p class="eyebrow">Limits</p>
          <h2>What this does not claim</h2>
        </div>
        {limitations}
      </article>
    </section>
    """
    return _page_layout(title="Growth Loop Agent", body=body)


def _render_account_page(
    *,
    current_user: AuthUser,
    readiness: CreatorWorkspaceReadiness,
    support_requests: dict[str, SupportRequestRecord],
    active_support_requests: dict[str, SupportRequestRecord],
    status_value: str | None,
    confirm_value: str | None,
    switch_attempt: BillingProviderSwitchAttempt | None,
    switch_clean_state: BillingProviderSwitchCleanState,
    switch_target_guidance: _BillingProviderSetupGuidance,
    paypal_available_to_creator: bool,
) -> str:
    creator_name = html.escape(current_user.creator.name)
    creator_email = html.escape(current_user.email)
    show_provider_choice = _creator_needs_initial_billing_provider_choice(
        creator=current_user.creator,
        readiness=readiness,
    )
    billing_state = _account_billing_management_state(
        current_billing_provider=current_user.creator.resolved_billing_provider,
        readiness=readiness,
        show_provider_choice=show_provider_choice,
        switch_attempt=switch_attempt,
        switch_clean_state=switch_clean_state,
        switch_target_guidance=switch_target_guidance,
        paypal_available_to_creator=paypal_available_to_creator,
    )
    booking_links_count = readiness.booking_links_count
    trackable_booking_links_count = readiness.trackable_booking_links_count
    limited_tracking_booking_links_count = readiness.limited_tracking_booking_links_count
    billing_ready_count = readiness.billing_ready_count
    billing_action = (
        billing_state["actions_html"]
        + _render_paypal_disconnect_call_to_action(
            current_user=current_user,
            confirm_value=confirm_value,
            switch_attempt=switch_attempt,
        )
    )

    booking_links_summary = "No booking links are saved yet for this workspace."
    if booking_links_count > 0:
        booking_links_summary = f"This workspace currently has {html.escape(_count_copy(booking_links_count, 'saved booking link'))}. "
        if _has_limited_tracking_only_booking_links(readiness):
            booking_links_summary += (
                "Those booking sources can generate tracked redirects now, but billable-now "
                "and creator-readiness still wait for end-to-end provider support."
            )
        elif _has_inactive_creator_booking_links(readiness):
            booking_links_summary += (
                "Those booking sources stay saved, but they are not active in creator-tracked "
                "workflows right now."
            )
        else:
            booking_links_summary += html.escape(
                _account_billing_ready_summary_copy(billing_ready_count)
            )
            if limited_tracking_booking_links_count > 0:
                limited_tracking_summary = (
                    "1 booking source still stays limited to tracked redirects until end-to-end "
                    "provider support lands."
                    if limited_tracking_booking_links_count == 1
                    else (
                        f"{limited_tracking_booking_links_count} booking sources still stay "
                        "limited to tracked redirects until end-to-end provider support lands."
                    )
                )
                booking_links_summary += (
                    f" {html.escape(limited_tracking_summary)}"
                )

    body = f"""
    <header class="shell-header">
      <div>
        <p class="eyebrow">Account</p>
        <h1>Account</h1>
        <p class="lede">Review the billing setup, recovery status, and safe account actions for this workspace.</p>
      </div>
      <form action="/sign-out" method="post">
        <button type="submit" class="secondary">Sign out</button>
      </form>
    </header>
    {_render_shell_nav(current_path="/app/account")}
    {_render_account_request_notice(status_value=status_value)}
    {_render_account_billing_setup_section(
        current_user=current_user,
        readiness=readiness,
        billing_state=billing_state,
        billing_action=billing_action,
        switch_attempt=switch_attempt,
        switch_clean_state=switch_clean_state,
        switch_target_guidance=switch_target_guidance,
    )}
    <section class="grid">
      <article class="card stack">
        <div>
          <p class="eyebrow">Account context</p>
          <h2>Account context</h2>
        </div>
        <section class="topic-summary stack">
          <div>
            <p class="eyebrow">Current workspace</p>
            <h2 class="wrap-anywhere">{creator_name}</h2>
          </div>
          <p>Signed in as <strong class="wrap-anywhere">{creator_email}</strong>. This workspace currently holds your billing connection, booking links, tracked content, reports, and any blocked or unresolved items still waiting on review.</p>
        </section>
        <section class="topic-summary stack">
        <div>
          <p class="eyebrow">Session</p>
          <h2>Session</h2>
        </div>
          <p>Use the sign-out button above to end this browser session only. Your workspace data stays here when you sign back in.</p>
        </section>
      </article>
      <article class="card stack">
        <div>
          <p class="eyebrow">Booking links</p>
          <h2>Booking links</h2>
        </div>
        <p>Manage which booking links stay active for future tracked traffic and bookings. Turning off an old link should not remove the historical content, booking, or reporting records already tied to it.</p>
        <p>{html.escape(booking_links_summary)}</p>
        <p><strong>What this changes</strong></p>
        <p>Changing booking links affects future tracked behavior. Existing tracked links, bookings, invoices, and reports may still reflect earlier activity already recorded for this workspace.</p>
        <p><a href="/app/booking-links" class="button-link secondary">Manage booking links</a></p>
      </article>
    </section>
    <section id="danger-zone" class="card accent stack">
      <div>
        <p class="eyebrow">Danger zone</p>
        <h2>Support-assisted destructive changes</h2>
        <p>Workspace reset and account deletion stay support-assisted during beta. You can submit a request for manual review here, but no destructive changes are applied immediately.</p>
      </div>
      <div class="grid">
        <article class="topic-summary stack">
          <div class="status-row">
            <h2>Request workspace reset</h2>
            <span class="pill-note">Manual review</span>
          </div>
          <p>Use this only if you want to start over with the same email. During beta, workspace reset is reviewed manually before anything changes.</p>
          <p><strong>What reset may change</strong></p>
          <p>A reset can break tracked links, remove local setup state, and make earlier reports or history unavailable from this workspace. Reset does not delete or reverse anything in the payment provider or booking provider automatically.</p>
          <p>If your workspace already has booking, billing, or payment history, we may not be able to reset it safely.</p>
          {_render_account_request_current_state(
              request_record=support_requests.get(SUPPORT_REQUEST_TYPE_WORKSPACE_RESET),
          )}
          {_render_account_request_call_to_action(
              request_type=SUPPORT_REQUEST_TYPE_WORKSPACE_RESET,
              confirm_value=confirm_value,
              active_request=active_support_requests.get(SUPPORT_REQUEST_TYPE_WORKSPACE_RESET),
          )}
        </article>
        <article class="topic-summary stack">
          <div class="status-row">
            <h2>Request account deletion</h2>
            <span class="pill-note">Manual review</span>
          </div>
          <p>Use this if you want to close this local account and remove this workspace from the product. During beta, deletion is handled manually so we can avoid false promises about what is removed.</p>
          <p><strong>Before you request deletion</strong></p>
          <p>Deleting this account can remove local workspace data and end access to tracked links, reports, and historical workspace views. Some detached diagnostics may remain without workspace links, and payment-provider or booking-provider accounts are not deleted automatically.</p>
          <p>After deletion is complete, you may sign up again later with the same email, but it will be treated as a new workspace.</p>
          {_render_account_request_current_state(
              request_record=support_requests.get(SUPPORT_REQUEST_TYPE_ACCOUNT_DELETION),
          )}
          {_render_account_request_call_to_action(
              request_type=SUPPORT_REQUEST_TYPE_ACCOUNT_DELETION,
              confirm_value=confirm_value,
              active_request=active_support_requests.get(SUPPORT_REQUEST_TYPE_ACCOUNT_DELETION),
          )}
        </article>
      </div>
    </section>
    """
    return _page_layout(title="Account settings", body=body)


def _render_account_billing_setup_section(
    *,
    current_user: AuthUser,
    readiness: CreatorWorkspaceReadiness,
    billing_state: dict[str, str],
    billing_action: str,
    switch_attempt: BillingProviderSwitchAttempt | None,
    switch_clean_state: BillingProviderSwitchCleanState,
    switch_target_guidance: _BillingProviderSetupGuidance,
) -> str:
    pending_switch_html = ""
    if switch_attempt is not None:
        pending_switch_html = _render_account_pending_switch_summary(
            current_user=current_user,
            switch_attempt=switch_attempt,
            switch_clean_state=switch_clean_state,
            switch_target_guidance=switch_target_guidance,
        )

    return f"""
    <section id="billing-connection" class="hero milestone-hero stack account-billing-hero">
      <div class="status-row">
        <div>
          <p class="eyebrow">Billing setup</p>
          <h2>Billing connection</h2>
        </div>
        <span class="status-pill {html.escape(billing_state['badge_class'])}">{html.escape(billing_state['label'])}</span>
      </div>
      <p class="lede">Use this billing surface to confirm what is configured now, understand any recovery or switch blockers, and take the next safe action for future billing changes.</p>
      <div class="milestone-grid">
        {_render_account_current_provider_summary(current_user=current_user, readiness=readiness)}
        {_render_account_status_summary(billing_state=billing_state)}
        {_render_account_next_safe_action_summary(
            readiness=readiness,
            billing_action=billing_action,
            switch_attempt=switch_attempt,
        )}
      </div>
      {pending_switch_html}
      <div class="milestone-grid account-support-grid">
        {_render_readiness_summary(readiness=readiness)}
        <section class="topic-summary stack">
          <div>
            <p class="eyebrow">What this changes</p>
            <h2>Future billing and preserved history</h2>
          </div>
          <p>Changing the billing connection affects future billing readiness. It does not erase local history already recorded for this workspace, and it does not delete anything from the payment provider automatically.</p>
        </section>
      </div>
    </section>
    """


def _render_account_current_provider_summary(
    *,
    current_user: AuthUser,
    readiness: CreatorWorkspaceReadiness,
) -> str:
    configured_provider = (
        current_user.creator.resolved_billing_provider or readiness.billing_provider
    )
    provider_label = _billing_provider_label(configured_provider)
    if readiness.billing_connect_status == "connected":
        provider_status = "Connected for future billing review."
    elif readiness.billing_connect_status == "disconnected":
        provider_status = "Previously chosen, but currently disconnected."
    elif _creator_needs_initial_billing_provider_choice(
        creator=current_user.creator,
        readiness=readiness,
    ):
        provider_status = "No billing provider has been chosen yet."
    else:
        provider_status = "Setup has started, but a billing provider is not connected yet."

    detail_lines = [f"<p><strong>Billing provider</strong>: {html.escape(provider_label)}</p>"]
    if current_user.creator.resolved_billing_account_id:
        detail_lines.append(
            f"<p><strong>Billing account</strong>: "
            f'<span class="wrap-anywhere">{html.escape(current_user.creator.resolved_billing_account_id)}</span></p>'
        )
    else:
        detail_lines.append("<p><strong>Billing account</strong>: No active billing account is saved yet.</p>")
    if current_user.creator.resolved_billing_connected_at:
        detail_lines.append(
            f"<p><strong>Connected on</strong>: "
            f"{_format_connected_at(current_user.creator.resolved_billing_connected_at)}</p>"
        )

    return f"""
    <section class="topic-summary stack">
      <div>
        <p class="eyebrow">Current provider</p>
        <h2>{html.escape(provider_label)}</h2>
      </div>
      <p>{html.escape(provider_status)}</p>
      {"".join(detail_lines)}
    </section>
    """


def _render_account_status_summary(*, billing_state: dict[str, str]) -> str:
    return f"""
    <section class="topic-summary stack">
      <div>
        <p class="eyebrow">Current status</p>
        <h2>{html.escape(billing_state['label'])}</h2>
      </div>
      <p>{html.escape(billing_state['body'])}</p>
      <p><strong>What this means</strong>: review this state before changing future billing behavior.</p>
    </section>
    """


def _render_account_next_safe_action_summary(
    *,
    readiness: CreatorWorkspaceReadiness,
    billing_action: str,
    switch_attempt: BillingProviderSwitchAttempt | None,
) -> str:
    if billing_action:
        action_title = "Take the next safe action"
        action_copy = (
            "Use only the actions below for future billing changes. Historical records and earlier workspace history stay preserved."
        )
    elif readiness.billing_connect_status == "connected" and readiness.billable_now and switch_attempt is None:
        action_title = "No immediate action required"
        action_copy = (
            "This workspace is ready for future billing. Return here when you need to reconnect or switch providers."
        )
    else:
        action_title = "No automatic action available right now"
        action_copy = (
            "Review the current status first. The active provider stays in place until the blocker is cleared or a supported action becomes available."
        )

    return f"""
    <section class="topic-summary stack primary-action">
      <div>
        <p class="eyebrow">Next safe action</p>
        <h2>{html.escape(action_title)}</h2>
      </div>
      <p>{html.escape(action_copy)}</p>
      {billing_action}
    </section>
    """


def _render_account_pending_switch_summary(
    *,
    current_user: AuthUser,
    switch_attempt: BillingProviderSwitchAttempt,
    switch_clean_state: BillingProviderSwitchCleanState,
    switch_target_guidance: _BillingProviderSetupGuidance,
) -> str:
    target_provider_label = _billing_provider_label(switch_attempt.target_billing_provider)
    current_provider_label = _billing_provider_label(current_user.creator.resolved_billing_provider)

    if (
        switch_attempt.target_billing_connect_status != "connected"
        or switch_attempt.target_billing_account_id is None
    ):
        switch_label = "Waiting"
        switch_badge_class = "pending"
        switch_summary = (
            f"{target_provider_label} is selected as the replacement provider, but setup is not connected yet. "
            f"{current_provider_label} stays active until the replacement provider is connected and committed."
        )
    elif switch_target_guidance.state == _BILLING_PROVIDER_SETUP_STATE_BLOCKED:
        switch_label = "Needs review"
        switch_badge_class = "disconnected"
        switch_summary = (
            f"{target_provider_label} is connected for the pending switch, but its invoice readiness could not be verified yet."
        )
    elif switch_target_guidance.state == _BILLING_PROVIDER_SETUP_STATE_NOT_READY:
        switch_label = "Needs setup"
        switch_badge_class = "pending"
        switch_summary = (
            f"{target_provider_label} is connected for the pending switch, but it still needs more setup before the switch can be committed."
        )
    elif not switch_clean_state.is_clean:
        switch_label = "Blocked"
        switch_badge_class = "disconnected"
        switch_summary = (
            f"{target_provider_label} is connected, but the switch is blocked by active billing work that must be cleared first."
        )
    else:
        switch_label = "Ready"
        switch_badge_class = "connected"
        switch_summary = (
            f"{target_provider_label} is connected and ready. {current_provider_label} stays active until you commit the switch."
        )

    detail_lines = [
        f"<p><strong>Current provider</strong>: {html.escape(current_provider_label)}</p>",
        f"<p><strong>Pending switch target</strong>: {html.escape(target_provider_label)}</p>",
    ]
    if switch_attempt.target_billing_account_id:
        detail_lines.append(
            f"<p><strong>Pending target account</strong>: "
            f'<span class="wrap-anywhere">{html.escape(switch_attempt.target_billing_account_id)}</span></p>'
        )
    if switch_attempt.target_billing_connected_at:
        detail_lines.append(
            f"<p><strong>Pending target connected on</strong>: "
            f"{_format_connected_at(switch_attempt.target_billing_connected_at)}</p>"
        )

    return f"""
    <section class="topic-summary stack">
      <div class="status-row">
        <div>
          <p class="eyebrow">Switch state</p>
          <h2>Pending provider switch</h2>
        </div>
        <span class="status-pill {html.escape(switch_badge_class)}">{html.escape(switch_label)}</span>
      </div>
      <p>{html.escape(switch_summary)}</p>
      {"".join(detail_lines)}
    </section>
    """


def _render_setup_home_notice(*, status_value: str | None) -> str:
    message = SETUP_HOME_STATUS_MESSAGES.get(status_value)
    if message is None:
        return ""

    return f"""
    <section class="{html.escape(message['notice_class'])}">
      <p class="eyebrow">{html.escape(message['title'])}</p>
      <p>{html.escape(message['body'])}</p>
    </section>
    """


def _render_account_request_notice(*, status_value: str | None) -> str:
    message = ACCOUNT_REQUEST_STATUS_MESSAGES.get(status_value)
    if message is None:
        return ""

    return f"""
    <section class="{html.escape(message['notice_class'])}">
      <p class="eyebrow">{html.escape(message['title'])}</p>
      <p>{html.escape(message['body'])}</p>
    </section>
    """


def _render_operator_support_request_notice(*, status_value: str | None) -> str:
    message = OPERATOR_SUPPORT_REQUEST_STATUS_MESSAGES.get(status_value)
    if message is None:
        return ""

    return f"""
    <section class="{html.escape(message['notice_class'])}">
      <p class="eyebrow">{html.escape(message['title'])}</p>
      <p>{html.escape(message['body'])}</p>
    </section>
    """


def _render_operator_nav(*, current_path: str) -> str:
    href = "/app/operator/support-requests"
    class_name = (
        "nav-link active"
        if current_path == href or current_path.startswith(f"{href}/")
        else "nav-link"
    )
    return f'<nav class="shell-nav"><a href="{href}" class="{class_name}">Operator Queue</a></nav>'


def _render_account_request_call_to_action(
    *,
    request_type: str,
    confirm_value: str | None,
    active_request: SupportRequestRecord | None,
) -> str:
    flow = _account_request_flow(request_type)
    if flow is None:
        return ""

    if active_request is not None:
        return (
            "<p><strong>This request is already active.</strong> During beta, "
            "we keep one active request per request type until manual review closes it.</p>"
        )

    if confirm_value == request_type:
        return f"""
        <section class="topic-summary stack">
          <div>
            <p class="eyebrow">Confirm</p>
            <h2>{html.escape(flow['confirm_title'])}</h2>
          </div>
          <p>{html.escape(flow['confirm_body'])}</p>
          <form action="{html.escape(flow['submit_path'])}" method="post">
            <button type="submit">{html.escape(flow['confirm_button'])}</button>
          </form>
          <p><a href="/app/account{ACCOUNT_DANGER_ZONE_FRAGMENT}" class="inline-link">{html.escape(flow['cancel_button'])}</a></p>
        </section>
        """

    return (
        f'<p><a href="/app/account?confirm={quote(request_type, safe="")}{ACCOUNT_DANGER_ZONE_FRAGMENT}" class="inline-link">'
        f"{html.escape(flow['action_label'])}</a></p>"
    )


def _paypal_disconnect_available(
    *,
    current_user: AuthUser,
    switch_attempt: BillingProviderSwitchAttempt | None,
) -> bool:
    return (
        current_user.creator.resolved_billing_provider == BILLING_PROVIDER_PAYPAL
        and current_user.creator.resolved_billing_connect_status == BILLING_CONNECT_STATUS_CONNECTED
        and switch_attempt is None
    )


def _render_paypal_disconnect_call_to_action(
    *,
    current_user: AuthUser,
    confirm_value: str | None,
    switch_attempt: BillingProviderSwitchAttempt | None,
) -> str:
    if not _paypal_disconnect_available(
        current_user=current_user,
        switch_attempt=switch_attempt,
    ):
        return ""

    if confirm_value == PAYPAL_DISCONNECT_CONFIRM_VALUE:
        return f"""
        <section class="topic-summary stack">
          <div>
            <p class="eyebrow">Confirm</p>
            <h2>Disconnect PayPal?</h2>
          </div>
          <p>{html.escape(_PAYPAL_DISCONNECT_CONFIRMATION_COPY)}</p>
          <form action="/app/account/paypal/disconnect" method="post">
            <button type="submit">Disconnect PayPal</button>
          </form>
          <p><a href="/app/account{ACCOUNT_BILLING_FRAGMENT}" class="inline-link">Keep PayPal connected</a></p>
        </section>
        """

    return (
        f'<p><a href="/app/account?confirm={quote(PAYPAL_DISCONNECT_CONFIRM_VALUE, safe="")}{ACCOUNT_BILLING_FRAGMENT}" '
        'class="inline-link">Disconnect PayPal</a></p>'
    )


def _render_account_request_current_state(*, request_record: SupportRequestRecord | None) -> str:
    if request_record is None:
        return ""

    review_state = support_request_status_display(request_record)
    notification_state = support_request_notification_state_display(request_record)
    created_at_copy = _format_account_request_created_at(request_record.created_at)
    return f"""
    <section class="topic-summary stack">
      <div class="status-row">
        <div>
          <p class="eyebrow">Current request</p>
          <h2>{html.escape(review_state['label'])}</h2>
        </div>
        <span class="status-pill {html.escape(review_state['badge_class'])}">{html.escape(review_state['label'])}</span>
      </div>
      <p>{html.escape(review_state['body'])}</p>
      <p><strong>Request ID</strong>: <code>{html.escape(support_request_public_id(request_record))}</code></p>
      <p><strong>Submitted</strong>: {html.escape(created_at_copy)}</p>
      <p><strong>Email delivery</strong>: <span class="status-pill {html.escape(notification_state['badge_class'])}">{html.escape(notification_state['label'])}</span></p>
      <p>{html.escape(notification_state['body'])}</p>
    </section>
    """


def _format_account_request_created_at(created_at: datetime | None) -> str:
    if created_at is None:
        return "Saved recently"
    return created_at.astimezone(timezone.utc).strftime("%B %d, %Y at %H:%M UTC")


def _render_operator_support_request_queue_page(
    *,
    current_user: AuthUser,
    support_requests: list[SupportRequestRecord],
    status_value: str | None,
) -> str:
    operator_email = html.escape(current_user.email)
    queue_body = _render_operator_support_request_queue_rows(support_requests=support_requests)
    body = f"""
    <header class="shell-header">
      <div>
        <p class="eyebrow">Internal operator queue</p>
        <h1>Support request queue</h1>
        <p class="lede">Review the saved beta reset and account-deletion requests without writing directly to the database.</p>
      </div>
    </header>
    {_render_shell_nav(current_path="/app/operator/support-requests")}
    {_render_operator_nav(current_path="/app/operator/support-requests")}
    {_render_operator_support_request_notice(status_value=status_value)}
    <section class="card stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Queue</p>
          <h2>Support-assisted destructive requests</h2>
        </div>
        <p>Allowlisted operator signed in as <strong class="wrap-anywhere">{operator_email}</strong>.</p>
      </div>
      {queue_body}
    </section>
    """
    return _page_layout(title="Support request queue", body=body)


def _render_operator_support_request_queue_rows(
    *,
    support_requests: list[SupportRequestRecord],
) -> str:
    if not support_requests:
        return """
        <section class="empty-state">
          <p class="eyebrow">Clear</p>
          <h2>No support requests yet</h2>
          <p>New reset or account-deletion requests will appear here once creators submit them from the account page.</p>
        </section>
        """

    rows: list[str] = []
    for request_record in support_requests:
        review_state = support_request_status_display(request_record)
        notification_state = support_request_notification_state_display(request_record)
        rows.append(
            f"""
            <tr>
              <td><code>{html.escape(support_request_public_id(request_record))}</code></td>
              <td>{html.escape(support_request_type_label(request_record.request_type))}</td>
              <td class="wrap-anywhere">{html.escape(request_record.creator_name_snapshot)}</td>
              <td class="wrap-anywhere">{html.escape(request_record.requester_email)}</td>
              <td>{html.escape(review_state['label'])}</td>
              <td>{html.escape(notification_state['label'])}</td>
              <td>{html.escape(_format_account_request_created_at(request_record.created_at))}</td>
              <td><a href="/app/operator/support-requests/{support_request_public_id(request_record)}" class="inline-link">Review</a></td>
            </tr>
            """
        )

    return f"""
    <table class="data-table">
      <thead>
        <tr>
          <th>Request ID</th>
          <th>Type</th>
          <th>Workspace</th>
          <th>Requester</th>
          <th>Review</th>
          <th>Email</th>
          <th>Submitted</th>
          <th>Action</th>
        </tr>
      </thead>
      <tbody>
        {"".join(rows)}
      </tbody>
    </table>
    """


def _render_operator_support_request_detail_page(
    *,
    current_user: AuthUser,
    request_record: SupportRequestRecord,
    status_value: str | None,
) -> str:
    operator_email = html.escape(current_user.email)
    review_state = support_request_status_display(request_record)
    notification_state = support_request_notification_state_display(request_record)
    body = f"""
    <header class="shell-header">
      <div>
        <p class="eyebrow">Internal operator queue</p>
        <h1>{html.escape(support_request_type_label(request_record.request_type))}</h1>
        <p class="lede">Inspect the saved request context, keep email-delivery state visible, and move the request through the approved review states.</p>
      </div>
    </header>
    {_render_shell_nav(current_path=f"/app/operator/support-requests/{support_request_public_id(request_record)}")}
    {_render_operator_nav(current_path=f"/app/operator/support-requests/{support_request_public_id(request_record)}")}
    {_render_operator_support_request_notice(status_value=status_value)}
    <section class="card stack">
      <p><a href="/app/operator/support-requests" class="inline-link">Back to support request queue</a></p>
      <p>Allowlisted operator signed in as <strong class="wrap-anywhere">{operator_email}</strong>.</p>
    </section>
    <section class="grid">
      <article class="card stack">
        <div>
          <p class="eyebrow">Request context</p>
          <h2>{html.escape(support_request_type_label(request_record.request_type))}</h2>
        </div>
        <p><strong>Request ID</strong>: <code>{html.escape(support_request_public_id(request_record))}</code></p>
        <p><strong>Request type</strong>: {html.escape(support_request_type_label(request_record.request_type))}</p>
        <p><strong>Creator ID</strong>: <code>{html.escape(str(request_record.creator_id))}</code></p>
        <p><strong>Requester email</strong>: <span class="wrap-anywhere">{html.escape(request_record.requester_email)}</span></p>
        <p><strong>Workspace name</strong>: <span class="wrap-anywhere">{html.escape(request_record.creator_name_snapshot)}</span></p>
        <p><strong>Submitted</strong>: {html.escape(_format_account_request_created_at(request_record.created_at))}</p>
      </article>
      <article class="card stack">
        <div class="status-row">
          <div>
            <p class="eyebrow">Review status</p>
            <h2>{html.escape(review_state['label'])}</h2>
          </div>
          <span class="status-pill {html.escape(review_state['badge_class'])}">{html.escape(review_state['label'])}</span>
        </div>
        <p>{html.escape(review_state['body'])}</p>
        <p><strong>Email delivery</strong>: <span class="status-pill {html.escape(notification_state['badge_class'])}">{html.escape(notification_state['label'])}</span></p>
        <p>{html.escape(notification_state['body'])}</p>
        {_render_operator_support_request_transition_actions(request_record=request_record)}
      </article>
    </section>
    """
    return _page_layout(title="Support request detail", body=body)


def _render_operator_support_request_transition_actions(
    *,
    request_record: SupportRequestRecord,
) -> str:
    available_transitions = support_request_available_transitions(request_record)
    if not available_transitions:
        return (
            "<p><strong>Terminal status.</strong> This request can no longer move to another review state inside the app.</p>"
        )

    forms = []
    for next_status in available_transitions:
        forms.append(
            f"""
            <form action="/app/operator/support-requests/{support_request_public_id(request_record)}/status" method="post">
              <input type="hidden" name="status" value="{html.escape(next_status)}" />
              <button type="submit" class="secondary">{html.escape(support_request_status_label(next_status))}</button>
            </form>
            """
        )

    return f"""
    <div>
      <p><strong>Allowed transitions</strong></p>
      <p>Keep actual workspace reset or account deletion execution manual and outside the app.</p>
    </div>
    <div class="filter-actions">
      {"".join(forms)}
    </div>
    """


def _render_setup_progress_section(*, setup_progress: dict[str, object]) -> str:
    return f"""
    <section class="card stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Setup progress</p>
          <h2>{html.escape(str(setup_progress['completed_count']))} of {html.escape(str(setup_progress['total_steps']))} setup steps done</h2>
        </div>
        <p>{html.escape(str(setup_progress['progress_copy']))}</p>
      </div>
      <div class="stat-grid">
        <article class="stat-tile">
          <p class="eyebrow">Progress</p>
          <p class="stat-value">{html.escape(str(setup_progress['completed_count']))}/{html.escape(str(setup_progress['total_steps']))}</p>
          <p>Core setup steps complete.</p>
        </article>
        <article class="stat-tile">
          <p class="eyebrow">Booking links</p>
          <p class="stat-value">{html.escape(str(setup_progress['booking_links_count']))}</p>
          <p>Saved Calendly links.</p>
        </article>
        <article class="stat-tile">
          <p class="eyebrow">Billing-ready links</p>
          <p class="stat-value">{html.escape(str(setup_progress['billing_ready_count']))}</p>
          <p>Links with amount and currency set.</p>
        </article>
        <article class="stat-tile">
          <p class="eyebrow">Tracked links</p>
          <p class="stat-value">{html.escape(str(setup_progress['tracked_content_count']))}</p>
          <p>Links ready to share.</p>
        </article>
      </div>
      <p class="footnote">{_setup_attention_copy(setup_progress['attention_count'])}</p>
    </section>
    """


def _render_setup_checklist_items(
    steps: list[dict[str, object]],
    *,
    active_action: dict[str, object] | None = None,
    paypal_available_to_creator: bool = False,
    compact: bool = False,
) -> str:
    items = []
    active_index = None
    if not compact:
        active_index = next(
            (index for index, step in enumerate(steps) if not step["is_complete"]),
            None,
        )

    for index, step in enumerate(steps):
        is_active = active_index == index and active_action is not None
        is_future_locked = (
            active_index is not None
            and index > active_index
            and not bool(step["is_complete"])
        )
        item_class = str(step["item_class"])
        if is_active:
            item_class = f"{item_class} active"
        elif is_future_locked:
            item_class = f"{item_class} locked"
        if compact:
            item_class = f"{item_class} compact"
        badge_class = str(step["badge_class"])
        label = str(step["label"])
        if is_future_locked:
            badge_class = "pending"
            label = "Waiting"
        action_html = ""
        if is_active and active_action is not None:
            action_html = f"""
              <div class="active-step-action">
                {_render_setup_next_action_cta(
                    active_action,
                    paypal_available_to_creator=paypal_available_to_creator,
                )}
                <p><strong>Why this matters</strong>: {active_action['copy_html']}</p>
              </div>
            """
        copy_html = step.get("locked_copy_html") if is_future_locked else step["copy_html"]
        items.append(
            f"""
            <li class="checklist-item {html.escape(item_class)}">
              <div>
                <strong>{html.escape(str(step['title']))}</strong>
                <p>{copy_html}</p>
                {action_html}
              </div>
              <span class="status-pill {html.escape(badge_class)}">{html.escape(label)}</span>
            </li>
            """
        )
    return "".join(items)


def _render_setup_next_action_cta(
    next_action: dict[str, str],
    *,
    paypal_available_to_creator: bool,
) -> str:
    if next_action["action_method"] == "provider-choice":
        return _render_billing_provider_choice_actions(
            paypal_available_to_creator=paypal_available_to_creator
        )
    if next_action["action_method"] == "post":
        return f"""
        <form action="{html.escape(next_action['action_href'])}" method="post">
          <button type="submit">{html.escape(next_action['action_label'])}</button>
        </form>
        """

    return (
        f'<p><a href="{html.escape(next_action["action_href"])}" class="button-link">'
        f"{html.escape(next_action['action_label'])}</a></p>"
    )


def _setup_home_is_progressed_state(*, readiness: CreatorWorkspaceReadiness) -> bool:
    return readiness.waiting_for_first_paid_result or readiness.paid_invoice_count > 0


def _render_setup_home_checklist_hero(
    *,
    current_user: AuthUser,
    workspace_state: CreatorWorkspaceState,
    setup_progress: dict[str, object],
    tracked_booking_count: int,
    show_provider_choice: bool,
    paypal_available_to_creator: bool,
) -> str:
    readiness = workspace_state.readiness
    milestone = _build_setup_home_milestone(
        readiness=readiness,
        attention_count=workspace_state.attention_count,
        tracked_booking_count=tracked_booking_count,
        show_provider_choice=show_provider_choice,
        paypal_available_to_creator=paypal_available_to_creator,
    )
    next_action = setup_progress["next_action"]
    return f"""
    <section class="hero milestone-hero setup-checklist-hero stack">
      <div class="status-row">
        <div>
          <p class="eyebrow">Your path to first paid proof</p>
          <h2>{html.escape(milestone['title'])}</h2>
        </div>
        <span class="status-pill {html.escape(milestone['badge_class'])}">
          {html.escape(str(setup_progress['completed_count']))} of {html.escape(str(setup_progress['total_steps']))} setup milestones done
        </span>
      </div>
      <div class="milestone-copy">
        <p class="milestone-question"><strong>Main question</strong>: {html.escape(milestone['question'])}</p>
        <p>{html.escape(milestone['body'])}</p>
        <p><strong>{html.escape(milestone['proof_title'])}</strong>: {html.escape(milestone['proof_copy'])}</p>
      </div>
      <ul class="checklist setup-checklist primary-checklist">
        {_render_setup_checklist_items(
            setup_progress['steps'],
            active_action=next_action,
            paypal_available_to_creator=paypal_available_to_creator,
        )}
      </ul>
      <p class="footnote">
        Signed in as <strong class="wrap-anywhere">{html.escape(current_user.email)}</strong>.
        Future steps stay waiting until the active step is ready.
      </p>
    </section>
    """


def _render_setup_home_checklist_card(
    *,
    setup_progress: dict[str, object],
    compact: bool = False,
) -> str:
    compact_class = " compact-checklist" if compact else ""
    return f"""
    <article class="card stack setup-checklist-card{compact_class}">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Setup path</p>
          <h2>{html.escape(str(setup_progress['completed_count']))} of {html.escape(str(setup_progress['total_steps']))} setup milestones done</h2>
        </div>
        <p>{html.escape(str(setup_progress['progress_copy']))}</p>
      </div>
      <ul class="checklist setup-checklist">
        {_render_setup_checklist_items(setup_progress['steps'], compact=compact)}
      </ul>
    </article>
    """


def _render_setup_home_workspace_proof_card(
    *,
    current_user: AuthUser,
    setup_progress: dict[str, object],
    readiness: CreatorWorkspaceReadiness,
    tracked_booking_count: int,
) -> str:
    billing_detail_html = ""
    if current_user.creator.resolved_billing_account_id:
        billing_detail_html = f"""
        <div class="stat-tile">
          <p class="eyebrow">Billing connection</p>
          <p><strong>Provider</strong>: {html.escape(_billing_provider_label(current_user.creator.resolved_billing_provider))}</p>
          <p><strong>Billing account</strong>: <span class="wrap-anywhere">{html.escape(current_user.creator.resolved_billing_account_id)}</span></p>
        </div>
        """
    return f"""
    <article class="card accent stack setup-proof-card">
      <div>
        <p class="eyebrow">Workspace proof</p>
        <h2>What is already true in this workspace</h2>
      </div>
      <div class="stat-grid">
        {billing_detail_html}
        <article class="stat-tile">
          <p class="eyebrow">Booking links</p>
          <p class="stat-value">{html.escape(str(setup_progress['booking_links_count']))}</p>
          <p>Saved destinations in this workspace.</p>
        </article>
        <article class="stat-tile">
          <p class="eyebrow">Billable links</p>
          <p class="stat-value">{html.escape(str(setup_progress['billing_ready_count']))}</p>
          <p>Links with amount and currency saved.</p>
        </article>
        <article class="stat-tile">
          <p class="eyebrow">Tracked links</p>
          <p class="stat-value">{html.escape(str(setup_progress['tracked_content_count']))}</p>
          <p>Creator-visible links ready to share.</p>
        </article>
        <article class="stat-tile">
          <p class="eyebrow">Tracked bookings</p>
          <p class="stat-value">{html.escape(str(tracked_booking_count))}</p>
          <p>Bookings already visible in the funnel.</p>
        </article>
        <article class="stat-tile">
          <p class="eyebrow">Paid results</p>
          <p class="stat-value">{html.escape(str(readiness.paid_invoice_count))}</p>
          <p>Canonical paid invoices counted so far.</p>
        </article>
      </div>
      {_render_setup_home_attention_summary(setup_progress['attention_count'])}
    </article>
    """


def _render_setup_home_milestone_section(
    *,
    current_user: AuthUser,
    workspace_state: CreatorWorkspaceState,
    setup_progress: dict[str, object],
    tracked_booking_count: int,
    show_provider_choice: bool,
    paypal_available_to_creator: bool,
    experiments_readiness_summary: CreatorNextContentExperimentsReadinessSummary,
) -> str:
    readiness = workspace_state.readiness
    milestone = _build_setup_home_milestone(
        readiness=readiness,
        attention_count=workspace_state.attention_count,
        tracked_booking_count=tracked_booking_count,
        show_provider_choice=show_provider_choice,
        paypal_available_to_creator=paypal_available_to_creator,
    )
    experiments_handoff = _build_setup_home_experiments_handoff(
        readiness=readiness,
        experiments_readiness_summary=experiments_readiness_summary,
    )

    billing_detail_lines = []
    if current_user.creator.resolved_billing_account_id:
        billing_detail_lines.append(
            f"<p><strong>Billing provider</strong>: "
            f"{html.escape(_billing_provider_label(current_user.creator.resolved_billing_provider))}</p>"
        )
        billing_detail_lines.append(
            f"<p><strong>Billing account</strong>: "
            f'<span class="wrap-anywhere">{html.escape(current_user.creator.resolved_billing_account_id)}</span></p>'
        )
    if current_user.creator.resolved_billing_connected_at:
        billing_detail_lines.append(
            f"<p><strong>Connected on</strong>: "
            f"{_format_connected_at(current_user.creator.resolved_billing_connected_at)}</p>"
        )
    experiments_handoff_html = (
        _render_setup_home_experiments_handoff(experiments_handoff)
        if experiments_handoff is not None
        else ""
    )

    return f"""
    <section class="hero milestone-hero stack">
      <div class="status-row">
        <div>
          <p class="eyebrow">Current milestone</p>
          <h2>{html.escape(milestone['title'])}</h2>
        </div>
        <span class="status-pill {html.escape(milestone['badge_class'])}">{html.escape(milestone['badge_label'])}</span>
      </div>
      <div class="milestone-copy">
        <p class="milestone-question"><strong>Main question</strong>: {html.escape(milestone['question'])}</p>
        <p>{html.escape(milestone['body'])}</p>
      </div>
      <div class="milestone-grid">
        <article class="topic-summary stack">
          <div>
            <p class="eyebrow">What to do next</p>
            <h2>{html.escape(milestone['next_title'])}</h2>
          </div>
          <p>{html.escape(milestone['next_copy'])}</p>
          {_render_setup_next_action_cta(
              milestone['action'],
              paypal_available_to_creator=paypal_available_to_creator,
          )}
        </article>
        {experiments_handoff_html}
        <article class="topic-summary stack">
          <div>
            <p class="eyebrow">Proof this is real</p>
            <h2>{html.escape(milestone['proof_title'])}</h2>
          </div>
          <p>{html.escape(milestone['proof_copy'])}</p>
          <p class="milestone-note">{html.escape(str(setup_progress['progress_copy']))}</p>
        </article>
        <article class="topic-summary stack">
          <div>
            <p class="eyebrow">Workspace</p>
            <h2 class="wrap-anywhere">{html.escape(current_user.creator.name)}</h2>
          </div>
          <p>Signed in as <strong class="wrap-anywhere">{html.escape(current_user.email)}</strong></p>
          {"".join(billing_detail_lines)}
          <p><a href="/app/account" class="inline-link">Open account</a></p>
        </article>
      </div>
    </section>
    """


def _build_setup_home_milestone(
    *,
    readiness: CreatorWorkspaceReadiness,
    attention_count: int,
    tracked_booking_count: int,
    show_provider_choice: bool,
    paypal_available_to_creator: bool,
) -> dict[str, object]:
    milestone = build_setup_home_milestone_view(
        readiness=readiness,
        attention_count=attention_count,
        tracked_booking_count=tracked_booking_count,
        show_provider_choice=show_provider_choice,
        paypal_available_to_creator=paypal_available_to_creator,
    )
    return {
        "title": milestone.title,
        "badge_label": milestone.badge_label,
        "badge_class": milestone.badge_class,
        "question": milestone.question,
        "body": milestone.body,
        "next_title": milestone.next_title,
        "next_copy": milestone.next_copy,
        "proof_title": milestone.proof_title,
        "proof_copy": milestone.proof_copy,
        "action": milestone.action,
    }


def _build_setup_home_experiments_handoff(
    *,
    readiness: CreatorWorkspaceReadiness,
    experiments_readiness_summary: CreatorNextContentExperimentsReadinessSummary,
) -> dict[str, str] | None:
    handoff_view = build_setup_home_experiments_handoff_view(
        readiness=readiness,
        current_experiments_status=experiments_readiness_summary.current_status,
    )
    if handoff_view is None:
        return None
    return {
        "title": handoff_view.title,
        "body": handoff_view.body,
        "action_label": handoff_view.action["label"],
        "action_href": handoff_view.action["href"],
    }


def _setup_attention_copy(attention_count: int) -> str:
    if attention_count == 0:
        return (
            "Blocked billing and unresolved payments will appear on "
            '<a href="/app/attention" class="inline-link">Attention</a> if anything needs repair.'
        )
    return (
        f'<a href="/app/attention" class="inline-link">Review '
        f"{html.escape(_count_copy(attention_count, 'attention item'))}</a> already waiting in blocked billing or unresolved payments."
    )


def _render_setup_home_attention_summary(attention_count: int) -> str:
    summary_view = build_setup_home_attention_summary_view(attention_count)
    if summary_view.action is None:
        return (
            f'<p class="footnote">{html.escape(summary_view.inline_prefix or "")}'
            f'<a href="/app/attention" class="inline-link">{html.escape(summary_view.inline_link_label or "")}</a>'
            f"{html.escape(summary_view.inline_suffix or '')}</p>"
        )

    return f"""
    <section class="topic-summary stack">
      <div>
        <p class="eyebrow">Diagnostic summary</p>
        <h2>{html.escape(summary_view.title or "")}</h2>
      </div>
      <p>{html.escape(summary_view.body)}</p>
      <p><a href="{html.escape(summary_view.action['href'])}" class="button-link secondary">{html.escape(summary_view.action['label'])}</a></p>
    </section>
    """


def _readiness_stage_summary(readiness: CreatorWorkspaceReadiness) -> dict[str, str]:
    if readiness.paid_invoice_count > 0:
        return {
            "title": "Paid results are already landing",
            "copy": (
                "This workspace has moved past the first-value wait. Use Reports to review "
                "what is already counted."
            ),
        }

    if readiness.waiting_for_first_paid_result:
        return {
            "title": "Ready to track and waiting for first paid result",
            "copy": (
                "First value lands only after tracked content leads to a booking and the "
                "matching invoice is marked paid."
            ),
        }

    if readiness.billable_now:
        return {
            "title": "Billable now, but not ready to track",
            "copy": (
                "Create tracked content next so the shared link can carry attribution into "
                "bookings and later paid results."
            ),
        }

    if _billing_provider_is_connected_but_blocked(readiness):
        return {
            "title": "Connected, but billing setup is blocked",
            "copy": _billing_provider_blocked_copy(provider_name=readiness.billing_provider),
        }

    if _billing_provider_is_connected_but_not_ready(readiness):
        return {
            "title": "Connected, but not billable now",
            "copy": _billing_provider_not_ready_copy(readiness),
        }

    if _has_inactive_creator_booking_links(readiness):
        return {
            "title": "Connected, but not billable now",
            "copy": (
                "Saved booking sources are not active for creator-tracked workflows right now. "
                "Add a currently supported booking link next."
            ),
        }

    if readiness.billing_connected:
        return {
            "title": "Connected, but not billable now",
            "copy": (
                "Add amount and currency to at least one booking link so new bookings can "
                "move into invoicing."
            ),
        }

    return {
        "title": "Connected comes first",
        "copy": (
            "Value does not arrive right after sign-in. Connect a billing provider first, "
            "then make one booking link billable now, then create tracked content."
        ),
    }


def _readiness_line_items(readiness: CreatorWorkspaceReadiness) -> list[tuple[str, str, str]]:
    billing_connect_status = readiness.billing_connect_status
    booking_links_count = readiness.booking_links_count
    trackable_booking_links_count = readiness.trackable_booking_links_count
    limited_tracking_booking_links_count = readiness.limited_tracking_booking_links_count

    if readiness.billing_connected:
        connected_line = ("Connected", "Done", "A billing provider is connected to this workspace.")
    elif billing_connect_status == "disconnected":
        connected_line = (
            "Connected",
            "Not yet",
            "Reconnect billing setup before relying on new bookings.",
        )
    else:
        connected_line = ("Connected", "Not yet", "Finish billing setup first.")

    if readiness.billable_now:
        billable_line = (
            "Billable now",
            "Done",
            "At least one booking link has amount and currency saved.",
        )
    elif _billing_provider_is_connected_but_blocked(readiness):
        billable_line = (
            "Billable now",
            "Blocked",
            _billing_provider_blocked_copy(provider_name=readiness.billing_provider),
        )
    elif _billing_provider_is_connected_but_not_ready(readiness):
        billable_line = (
            "Billable now",
            "Not yet",
            _billing_provider_not_ready_copy(readiness),
        )
    elif readiness.billing_connected and _has_limited_tracking_only_booking_links(readiness):
        billable_line = (
            "Billable now",
            "Not yet",
            "Saved booking sources can generate tracked redirects now, but billable-now and invoice readiness still wait for end-to-end provider support.",
        )
    elif readiness.billing_connected and _has_inactive_creator_booking_links(readiness):
        billable_line = (
            "Billable now",
            "Not yet",
            "Saved booking sources are not active for creator-tracked workflows right now. Add a currently supported booking link.",
        )
    elif readiness.billing_connected and booking_links_count > 0:
        billable_line = (
            "Billable now",
            "Not yet",
            "Add amount and currency to at least one saved booking link.",
        )
    elif readiness.billing_connected:
        billable_line = (
            "Billable now",
            "Not yet",
            "Save a booking link, then add amount and currency.",
        )
    else:
        billable_line = (
            "Billable now",
            "Not yet",
            "A billing provider must be connected before this workspace can be billable now.",
        )

    if readiness.ready_to_track:
        ready_to_track_line = (
            "Ready to track",
            "Done",
            "At least one tracked link is ready to share on a billable setup.",
        )
    elif readiness.billable_now:
        ready_to_track_line = (
            "Ready to track",
            "Not yet",
            "Create tracked content so shared links can lead to attributed bookings.",
        )
    else:
        ready_to_track_line = (
            "Ready to track",
            "Not yet",
            "This milestone starts after the workspace is billable now.",
        )

    if readiness.paid_invoice_count > 0:
        waiting_line = (
            "Waiting for first paid result",
            "Done",
            "Reports already includes counted paid results for this workspace.",
        )
    elif readiness.waiting_for_first_paid_result:
        waiting_line = (
            "Waiting for first paid result",
            "Current",
            "This workspace is ready to track; first value lands after a tracked booking leads to a paid invoice.",
        )
    else:
        waiting_line = (
            "Waiting for first paid result",
            "Later",
            "This milestone starts after the workspace is ready to track.",
        )

    return [
        connected_line,
        billable_line,
        ready_to_track_line,
        waiting_line,
    ]


def _render_readiness_summary(*, readiness: CreatorWorkspaceReadiness) -> str:
    stage_summary = _readiness_stage_summary(readiness)
    line_items = "".join(
        (
            f"<p><strong>{html.escape(term)}</strong>: {html.escape(status)}. "
            f"{html.escape(detail)}</p>"
        )
        for term, status, detail in _readiness_line_items(readiness)
    )
    return f"""
    <section class="topic-summary stack">
      <div>
        <p class="eyebrow">Setup-to-value path</p>
        <p><strong>{html.escape(stage_summary["title"])}</strong></p>
      </div>
      <p>{html.escape(stage_summary["copy"])}</p>
      {line_items}
    </section>
    """


def _build_setup_home_progress(
    *,
    workspace_state: CreatorWorkspaceState,
    show_provider_choice: bool,
    paypal_available_to_creator: bool,
) -> dict[str, object]:
    readiness = workspace_state.readiness
    normalized_billing_status = readiness.billing_connect_status
    booking_links_count = readiness.booking_links_count
    trackable_booking_links_count = readiness.trackable_booking_links_count
    limited_tracking_booking_links_count = readiness.limited_tracking_booking_links_count
    billing_ready_count = readiness.billing_ready_count
    tracked_content_count = readiness.tracked_content_count
    billable_now = readiness.billable_now
    ready_to_track = readiness.ready_to_track
    paid_invoice_count = readiness.paid_invoice_count
    attention_count = workspace_state.attention_count

    if normalized_billing_status == "connected":
        billing_step = _setup_step(
            title="Connect billing provider",
            copy_html=(
                "A billing provider is connected. "
                + (
                    "This workspace is already billable now while you finish the rest of setup."
                    if billable_now
                    else _billing_provider_blocked_copy(provider_name=readiness.billing_provider)
                    if _billing_provider_is_connected_but_blocked(readiness)
                    else _billing_provider_not_ready_copy(readiness)
                    if _billing_provider_is_connected_but_not_ready(readiness)
                    else "Creator setup still needs a currently supported booking link before this workspace can become billable now."
                    if booking_links_count > 0
                    and trackable_booking_links_count == 0
                    and limited_tracking_booking_links_count == 0
                    else "The next milestone is billable now, which needs amount and currency on at least one booking link."
                )
            ),
            label="Done",
            badge_class="connected",
            item_class="done",
            is_complete=True,
        )
        next_action = (
            {
                "title": "Review billing connection",
                "copy_html": (
                    f"{html.escape(_billing_provider_label(readiness.billing_provider))} "
                    "is connected, but invoice readiness could not be verified right now. Review the current billing connection details before relying on new bookings."
                ),
                "action_label": "Open account",
                "action_href": "/app/account",
                "action_method": "get",
            }
            if _billing_provider_is_connected_but_blocked(readiness)
            else {
                "title": "Review billing readiness",
                "copy_html": (
                    f"{html.escape(_billing_provider_label(readiness.billing_provider))} "
                    "is connected, but it is not ready to create invoices yet. Review the current billing connection details before relying on new bookings."
                ),
                "action_label": "Open account",
                "action_href": "/app/account",
                "action_method": "get",
            }
            if _billing_provider_is_connected_but_not_ready(readiness)
            else None
        )
    elif normalized_billing_status == "disconnected":
        provider_action = _billing_provider_connect_action(
            provider_name=readiness.billing_provider,
            reconnect=True,
            paypal_available_to_creator=paypal_available_to_creator,
        )
        billing_step_copy_html = (
            f"{html.escape(_billing_provider_label(readiness.billing_provider))} was connected before, "
            "but it is disconnected now. Reconnect it before new bookings can move into invoicing."
        )
        if provider_action is None:
            billing_step_copy_html = (
                f"{html.escape(_billing_provider_label(readiness.billing_provider))} was connected before, "
                "but it is disconnected now. "
                f"{_PAYPAL_UNAVAILABLE_CREATOR_COPY}"
            )
            next_action = {
                "title": "Review billing connection",
                "copy_html": _PAYPAL_UNAVAILABLE_CREATOR_COPY,
                "action_label": "Open account",
                "action_href": "/app/account",
                "action_method": "get",
            }
        else:
            next_action = {
                "title": "Reconnect billing setup",
                "copy_html": (
                    f"Billing setup is the first setup blocker. {html.escape(provider_action['label'])} "
                    "from this page before you rely on new bookings."
                ),
                "action_label": provider_action["label"],
                "action_href": provider_action["href"],
                "action_method": "post",
            }
        billing_step = _setup_step(
            title="Connect billing provider",
            copy_html=billing_step_copy_html,
            label="Blocked",
            badge_class="disconnected",
            item_class="todo",
            is_complete=False,
        )
    else:
        if show_provider_choice:
            billing_step_copy_html = (
                "Connect the account that will receive payments for your tutoring services."
                if paypal_available_to_creator
                else "Connect Stripe to start billing setup. PayPal setup is not yet available for general creators."
            )
            next_action = {
                "title": "Connect billing provider",
                "copy_html": (
                    "Your billing provider lets the workspace create invoices when tracked bookings arrive."
                    if paypal_available_to_creator
                    else "Stripe is the available billing provider for this workspace right now."
                ),
                "action_label": "",
                "action_href": "",
                "action_method": "provider-choice",
                "paypal_available_to_creator": (
                    "true" if paypal_available_to_creator else "false"
                ),
            }
        else:
            provider_action = _billing_provider_connect_action(
                provider_name=readiness.billing_provider,
                reconnect=False,
                paypal_available_to_creator=paypal_available_to_creator,
            )
            if provider_action is None:
                billing_step_copy_html = _PAYPAL_UNAVAILABLE_CREATOR_COPY
                next_action = {
                    "title": "Review billing setup",
                    "copy_html": _PAYPAL_UNAVAILABLE_CREATOR_COPY,
                    "action_label": "Open account",
                    "action_href": "/app/account",
                    "action_method": "get",
                }
            else:
                billing_step_copy_html = (
                    f"Finish {html.escape(_billing_provider_label(readiness.billing_provider))} "
                    "setup so this workspace has a payment account ready for invoicing."
                )
                next_action = {
                    "title": "Finish billing setup",
                    "copy_html": (
                        f"{html.escape(provider_action['label'])} so the rest of the setup flow leads "
                        "to a billable workspace."
                    ),
                    "action_label": provider_action["label"],
                    "action_href": provider_action["href"],
                    "action_method": "post",
                }
        billing_step = _setup_step(
            title="Connect billing provider",
            copy_html=billing_step_copy_html,
            label="Needs action",
            badge_class="pending",
            item_class="todo",
            is_complete=False,
        )

    if booking_links_count > 0:
        booking_link_copy_html = (
            f"{html.escape(_count_copy(booking_links_count, 'booking link'))} saved. "
            "Keep the saved booking links here aligned with what you actually share."
        )
        if _has_limited_tracking_only_booking_links(readiness):
            booking_link_copy_html = (
                f"{html.escape(_count_copy(booking_links_count, 'booking link'))} saved. "
                "These booking sources can generate tracked redirects now, but end-to-end creator readiness still waits for provider support."
            )
        elif _has_inactive_creator_booking_links(readiness):
            booking_link_copy_html = (
                f"{html.escape(_count_copy(booking_links_count, 'booking link'))} saved. "
                "These booking sources stay saved, but creator setup currently needs a supported active booking link."
            )
        booking_link_step = _setup_step(
            title="Save a booking link",
            copy_html=booking_link_copy_html,
            label="Done",
            badge_class="connected",
            item_class="done",
            is_complete=True,
        )
    else:
        booking_link_step = _setup_step(
            title="Save a booking link",
            copy_html='Add the booking link you want this workspace to track. <a href="/app/booking-links" class="inline-link">Open booking links</a>.',
            locked_copy_html="Waiting for billing setup first.",
            label="Needs action",
            badge_class="pending",
            item_class="todo",
            is_complete=False,
        )
        if next_action is None:
            next_action = {
                "title": "Add your first booking link",
                "copy_html": "Save the booking URL you actually use so tracked content has a real booking destination.",
                "action_label": "Open booking links",
                "action_href": "/app/booking-links",
                "action_method": "get",
            }

    if billing_ready_count > 0:
        billing_defaults_step = _setup_step(
            title="Add billing defaults",
            copy_html=(
                f"{html.escape(_count_copy(billing_ready_count, 'saved link'))} "
                + (
                    "already has amount and currency so this workspace is billable now."
                    if billable_now
                    else (
                        f"already has amount and currency, but {html.escape(_billing_provider_label(readiness.billing_provider))} readiness could not be verified right now."
                    )
                    if _billing_provider_is_connected_but_blocked(readiness)
                    else (
                        f"already has amount and currency, but {html.escape(_billing_provider_label(readiness.billing_provider))} is not ready to create invoices yet."
                    )
                    if _billing_provider_is_connected_but_not_ready(readiness)
                    else "already has amount and currency. Connect billing setup so this workspace becomes billable now."
                )
            ),
            label="Done",
            badge_class="connected",
            item_class="done",
            is_complete=True,
        )
    elif _has_limited_tracking_only_booking_links(readiness):
        billing_defaults_step = _setup_step(
            title="Add billing defaults",
            copy_html='Saved booking sources can generate tracked redirects now, but billable-now readiness still waits for end-to-end provider support. <a href="/app/booking-links" class="inline-link">Open booking links</a>.',
            label="Blocked",
            badge_class="disconnected",
            item_class="todo",
            is_complete=False,
        )
        if next_action is None:
            next_action = {
                "title": "Add a tracked-content-ready link",
                "copy_html": "Tracked redirects are ready, but billable-now readiness still waits for end-to-end provider support.",
                "action_label": "Open booking links",
                "action_href": "/app/booking-links",
                "action_method": "get",
            }
    elif _has_inactive_creator_booking_links(readiness):
        billing_defaults_step = _setup_step(
            title="Add billing defaults",
            copy_html='Saved booking sources are not active for creator billing right now. Add a currently supported booking link first. <a href="/app/booking-links" class="inline-link">Open booking links</a>.',
            label="Blocked",
            badge_class="disconnected",
            item_class="todo",
            is_complete=False,
        )
        if next_action is None:
            next_action = {
                "title": "Add a supported booking link",
                "copy_html": "Creator setup currently needs a booking link with active tracked-content support before this workspace can become billable now.",
                "action_label": "Open booking links",
                "action_href": "/app/booking-links",
                "action_method": "get",
            }
    elif booking_links_count > 0:
        billing_defaults_step = _setup_step(
            title="Add billing defaults",
            copy_html='At least one saved booking link still needs both amount and currency before this workspace is billable now. <a href="/app/booking-links" class="inline-link">Add billing defaults</a>.',
            label="Blocked",
            badge_class="disconnected",
            item_class="todo",
            is_complete=False,
        )
        if next_action is None:
            next_action = {
                "title": "Become billable now",
                "copy_html": "Add amount and currency to at least one saved booking link so new tracked bookings can move into invoicing.",
                "action_label": "Add billing defaults",
                "action_href": "/app/booking-links",
                "action_method": "get",
            }
    else:
        billing_defaults_step = _setup_step(
            title="Add billing defaults",
            copy_html="Save a booking link first, then return here to add the amount and currency you want invoices to use.",
            locked_copy_html="Waiting for a saved booking link.",
            label="Waiting",
            badge_class="pending",
            item_class="next",
            is_complete=False,
        )

    if tracked_content_count > 0:
        tracked_link_step = _setup_step(
            title="Create a tracked link",
            copy_html=(
                f"{html.escape(_count_copy(tracked_content_count, 'tracked link'))} "
                + (
                    "is ready to share, so this workspace is ready to track."
                    if tracked_content_count == 1 and ready_to_track
                    else "are ready to share, so this workspace is ready to track."
                    if ready_to_track
                    else "saved, but this workspace still is not ready to track until it is billable now."
                )
            ),
            label="Done",
            badge_class="connected",
            item_class="done",
            is_complete=True,
        )
    elif billable_now:
        tracked_link_step = _setup_step(
            title="Create a tracked link",
            copy_html='Create one tracked link so this workspace becomes ready to track. <a href="/app/content" class="inline-link">Open content</a>.',
            label="Needs action",
            badge_class="pending",
            item_class="todo",
            is_complete=False,
        )
        if next_action is None:
            next_action = {
                "title": "Become ready to track",
                "copy_html": "Create tracked content so the shared link can carry attribution into real bookings and later paid results.",
                "action_label": "Open content",
                "action_href": "/app/content",
                "action_method": "get",
            }
    else:
        tracked_link_step = _setup_step(
            title="Create a tracked link",
            copy_html="Create tracked content after the workspace is billable now so the shared link can lead to attributable bookings.",
            locked_copy_html="Waiting until the workspace is billable now.",
            label="Waiting",
            badge_class="pending",
            item_class="next",
            is_complete=False,
        )

    steps = [
        billing_step,
        booking_link_step,
        billing_defaults_step,
        tracked_link_step,
    ]
    completed_count = sum(1 for step in steps if step["is_complete"])

    if next_action is None:
        if attention_count > 0:
            next_action = {
                "title": "Review attention items",
                "copy_html": "Core setup is complete, but some blocked billing or unresolved payment work still needs review before everything can be trusted end to end.",
                "action_label": "Open Attention",
                "action_href": "/app/attention",
                "action_method": "get",
            }
        elif paid_invoice_count > 0:
            next_action = {
                "title": "Review paid results",
                "copy_html": "This workspace already has counted paid results. Use Reports to review what landed and keep sharing tracked links for more proof.",
                "action_label": "Open Reports",
                "action_href": "/app/reports",
                "action_method": "get",
            }
        else:
            next_action = {
                "title": "Waiting for first paid result",
                "copy_html": "This workspace is ready to track. Share the tracked link, then wait for a real booking and a matching paid invoice before Reports fills in.",
                "action_label": "Open content",
                "action_href": "/app/content",
                "action_method": "get",
            }

    progress_copy = "Connect billing setup first, then make one booking link billable now and create tracked content."
    if _billing_provider_is_connected_but_blocked(readiness):
        progress_copy = _billing_provider_blocked_copy(provider_name=readiness.billing_provider)
    elif _billing_provider_is_connected_but_not_ready(readiness):
        progress_copy = _billing_provider_not_ready_copy(readiness)
    elif readiness.billing_connected:
        progress_copy = "Billing setup is connected. The next milestone is billable now."
    if billable_now:
        progress_copy = "This workspace is billable now. Create tracked content next to become ready to track."
    if ready_to_track and paid_invoice_count == 0:
        progress_copy = "This workspace is ready to track and waiting for the first paid result."
    if paid_invoice_count > 0:
        progress_copy = "This workspace is ready to track and already has counted paid results."

    return {
        "steps": steps,
        "completed_count": completed_count,
        "total_steps": len(steps),
        "booking_links_count": booking_links_count,
        "billing_ready_count": billing_ready_count,
        "tracked_content_count": tracked_content_count,
        "attention_count": attention_count,
        "next_action": next_action,
        "progress_copy": progress_copy,
    }


def _setup_step(
    *,
    title: str,
    copy_html: str,
    label: str,
    badge_class: str,
    item_class: str,
    is_complete: bool,
    locked_copy_html: str | None = None,
) -> dict[str, object]:
    return {
        "title": title,
        "copy_html": copy_html,
        "locked_copy_html": locked_copy_html or copy_html,
        "label": label,
        "badge_class": badge_class,
        "item_class": item_class,
        "is_complete": is_complete,
    }


def _render_shell_nav(
    *,
    current_path: str,
    growth_loop_agent_feature_enabled: bool | None = None,
) -> str:
    if growth_loop_agent_feature_enabled is None:
        growth_loop_agent_feature_enabled = (
            get_settings().growth_loop_agent_feature_enabled
        )
    links = [
        ("/app", "Setup Home"),
        ("/app/booking-links", "Booking Links"),
        ("/app/content", "Content"),
        ("/app/bookings", "Bookings"),
        ("/app/reports", "Reports"),
        ("/app/health", "Health"),
    ]
    if growth_loop_agent_feature_enabled:
        links.append(("/app/growth-loop", "Growth Loop"))
    links.extend(
        [
            ("/app/experiments", "Experiments"),
            ("/app/attention", "Attention"),
            ("/app/account", "Account"),
        ]
    )
    items = []
    for href, label in links:
        if href == current_path:
            items.append(
                f'<a href="{href}" class="nav-link active" aria-current="page">'
                f"{html.escape(label)}</a>"
            )
            continue
        items.append(f'<a href="{href}" class="nav-link">{html.escape(label)}</a>')
    return (
        '<nav class="shell-nav" aria-label="Primary shell navigation">'
        f'{"".join(items)}</nav>'
    )


def _booking_link_form_provider(form_values: dict[str, str]) -> str:
    provider = form_values.get("provider", BOOKING_PROVIDER_CALENDLY).strip().lower()
    if provider in CREATOR_VISIBLE_BOOKING_LINK_PROVIDERS:
        return provider
    return BOOKING_PROVIDER_CALENDLY


def _booking_link_destination_label(provider: str) -> str:
    return "Calendly URL"


def _booking_link_destination_placeholder(provider: str) -> str:
    return "https://calendly.com/example/discovery-call"


def _booking_link_destination_help(provider: str) -> str:
    return "Use the Calendly URL this creator actually shares today."


def _booking_link_provider_label(provider: str) -> str:
    if provider == BOOKING_PROVIDER_FULLSCOPE:
        return "FullScope"
    return "Calendly"


def _booking_link_setup_state_copy(booking_link: BookingLinkResponse) -> str:
    if booking_provider_supports_creator_visible_tracked_content(booking_link.provider):
        return "Ready for tracked content now."
    if booking_provider_supports_creator_visible_tracked_destination(booking_link.provider):
        return "Tracked redirect ready, but end-to-end provider support is still pending."
    return (
        "Setup only for now. This booking source is saved, but tracked redirects are "
        "not available yet."
    )


def _has_limited_tracking_only_booking_links(readiness: CreatorWorkspaceReadiness) -> bool:
    return (
        readiness.booking_links_count > 0
        and readiness.trackable_booking_links_count == 0
        and readiness.limited_tracking_booking_links_count > 0
    )


def _has_inactive_creator_booking_links(readiness: CreatorWorkspaceReadiness) -> bool:
    return (
        readiness.booking_links_count > 0
        and readiness.trackable_booking_links_count == 0
        and readiness.limited_tracking_booking_links_count == 0
    )


def _render_booking_links_page(
    *,
    current_user: AuthUser,
    booking_links: list[BookingLinkResponse],
    form_values: dict[str, str],
    field_errors: dict[str, str],
    status_value: str | None,
) -> str:
    creator_name = html.escape(current_user.creator.name)
    creator_email = html.escape(current_user.email)
    notice = _render_booking_link_notice(status_value=status_value, field_errors=field_errors)
    list_heading = "Your booking links" if booking_links else "No booking links yet"
    selected_provider = _booking_link_form_provider(form_values)
    destination_label = _booking_link_destination_label(selected_provider)
    destination_placeholder = _booking_link_destination_placeholder(selected_provider)
    destination_help = _booking_link_destination_help(selected_provider)

    body = f"""
    <header class="shell-header">
      <div>
        <p class="eyebrow">Creator Home</p>
        <h1>Booking Links</h1>
        <p class="lede">Add the booking destination URLs this creator actually uses and, when available, store billing defaults that later invoice automation can trust.</p>
      </div>
      <form action="/sign-out" method="post">
        <button type="submit" class="secondary">Sign out</button>
      </form>
    </header>
    {_render_shell_nav(current_path="/app/booking-links")}
    {notice}
    <section class="grid">
      <article class="card stack">
        <div>
          <p class="eyebrow">Create link</p>
          <h2>Add a booking link</h2>
          <p>Signed in as <strong class="wrap-anywhere">{creator_email}</strong> for <strong class="wrap-anywhere">{creator_name}</strong>.</p>
        </div>
        <form action="/app/booking-links" method="post">
          <label for="provider">Provider</label>
          <select
            id="provider"
            name="provider"
            aria-invalid="{str("provider" in field_errors).lower()}"
          >
            <option value="{BOOKING_PROVIDER_CALENDLY}" selected>Calendly</option>
          </select>
          <p class="form-help">Creator setup currently supports Calendly end to end.</p>
          {_render_booking_link_field_error(field_errors.get("provider"))}

          <label for="name">Name</label>
          <input
            id="name"
            name="name"
            type="text"
            value="{html.escape(form_values["name"])}"
            placeholder="Discovery Call"
            required
            aria-invalid="{str("name" in field_errors).lower()}"
          />
          {_render_booking_link_field_error(field_errors.get("name"))}

          <label id="destination_url_label" for="destination_url">{html.escape(destination_label)}</label>
          <input
            id="destination_url"
            name="destination_url"
            type="url"
            value="{html.escape(form_values["destination_url"])}"
            placeholder="{html.escape(destination_placeholder)}"
            required
            aria-invalid="{str("destination_url" in field_errors).lower()}"
          />
          <p id="destination_url_help" class="form-help">{html.escape(destination_help)}</p>
          {_render_booking_link_field_error(field_errors.get("destination_url"))}

          <label for="billing_amount_cents">Billing amount in cents</label>
          <input
            id="billing_amount_cents"
            name="billing_amount_cents"
            type="number"
            inputmode="numeric"
            min="1"
            step="1"
            value="{html.escape(form_values["billing_amount_cents"])}"
            placeholder="15000"
            aria-invalid="{str("billing_amount_cents" in field_errors).lower()}"
          />
          <p class="form-help">Leave blank to skip defaults for now. Example: 15000 means a USD 150.00 invoice default.</p>
          {_render_booking_link_field_error(field_errors.get("billing_amount_cents"))}

          <label for="billing_currency">Billing currency</label>
          <input
            id="billing_currency"
            name="billing_currency"
            type="text"
            value="{html.escape(form_values["billing_currency"])}"
            placeholder="USD"
            maxlength="3"
            aria-invalid="{str("billing_currency" in field_errors).lower()}"
          />
          <p class="form-help">Use a three-letter code such as USD or EUR. Leave blank if you are not ready to set currency yet.</p>
          {_render_booking_link_field_error(field_errors.get("billing_currency"))}

          <button type="submit">Save booking link</button>
        </form>
      </article>
      <article class="card accent stack">
        <div>
          <p class="eyebrow">Billing defaults</p>
          <h2>Why they matter</h2>
        </div>
        <p>These defaults are optional in Story 39, but later invoice automation will use the stored amount and currency instead of trusting webhook payload values.</p>
        <p>If you leave one or both billing fields blank, the UI will still save the booking link and show exactly what is missing.</p>
        <a href="/app" class="inline-link">Back to setup home</a>
      </article>
    </section>
    <section class="card stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Creator-owned links</p>
          <h2>{list_heading}</h2>
        </div>
        <p>{len(booking_links)} saved</p>
      </div>
      {_render_booking_links_list(booking_links)}
    </section>
    """
    return _page_layout(title="Booking Links", body=body)


def _render_booking_links_list(booking_links: list[BookingLinkResponse]) -> str:
    if not booking_links:
        return """
        <section class="empty-state">
          <p class="eyebrow">Empty state</p>
          <h2>Create the first booking link</h2>
          <p>Add a booking destination now so later tracked-content and invoice-default steps have a real creator-owned source to reference.</p>
        </section>
        """

    items = "".join(_render_booking_link_card(booking_link) for booking_link in booking_links)
    return f'<div class="booking-link-list">{items}</div>'


def _render_booking_link_card(booking_link: BookingLinkResponse) -> str:
    destination_url = html.escape(booking_link.destination_url)
    provider_label = html.escape(_booking_link_provider_label(booking_link.provider))
    return f"""
    <article class="booking-link-card">
      <div class="booking-link-header">
        <div>
          <p class="eyebrow">Booking source</p>
          <h2>{html.escape(booking_link.name)}</h2>
        </div>
        <p class="pill-note">{provider_label}</p>
      </div>
      <p><strong>Destination URL</strong>: <a href="{destination_url}" class="inline-link">{destination_url}</a></p>
      <p><strong>Setup state</strong>: {html.escape(_booking_link_setup_state_copy(booking_link))}</p>
      <p><strong>Stored defaults</strong>: {html.escape(_billing_defaults_copy(booking_link, long_form=True))}</p>
    </article>
    """


def _render_booking_link_field_error(message: str | None) -> str:
    if not message:
        return ""
    return f'<p class="field-error">{html.escape(message)}</p>'


def _render_booking_link_notice(
    *,
    status_value: str | None,
    field_errors: dict[str, str],
) -> str:
    if field_errors:
        return """
        <section class="notice error">
          <p class="eyebrow">Fix the highlighted fields</p>
          <p>Update the invalid values and submit the form again.</p>
        </section>
        """

    if status_value == "created":
        return """
        <section class="notice success">
          <p class="eyebrow">Booking link saved</p>
          <p>The creator-owned link is now available for later tracked-link and billing workflow steps.</p>
        </section>
        """

    return ""


def _render_content_page(
    *,
    current_user: AuthUser,
    booking_links: list[BookingLinkResponse],
    content_items: list[ContentResponse],
    form_values: dict[str, str],
    field_errors: dict[str, str],
    status_value: str | None,
    created_content: ContentResponse | None,
) -> str:
    creator_name = html.escape(current_user.creator.name)
    creator_email = html.escape(current_user.email)
    notice = _render_content_notice(
        status_value=status_value,
        field_errors=field_errors,
        created_content=created_content,
    )
    list_heading = "Your tracked content" if content_items else "No tracked content yet"
    booking_link_names = {
        booking_link.id: booking_link.name
        for booking_link in booking_links
    }

    body = f"""
    <header class="shell-header">
      <div>
        <p class="eyebrow">Creator Home</p>
        <h1>Content</h1>
        <p class="lede">Turn a public source URL into a tracked link that routes through the attribution redirect before it reaches your supported booking flow.</p>
      </div>
      <form action="/sign-out" method="post">
        <button type="submit" class="secondary">Sign out</button>
      </form>
    </header>
    {_render_shell_nav(current_path="/app/content")}
    {notice}
    <section class="grid">
      {_render_content_form_panel(
          creator_name=creator_name,
          creator_email=creator_email,
          booking_links=booking_links,
          form_values=form_values,
          field_errors=field_errors,
      )}
      <article class="card accent stack">
        <div>
          <p class="eyebrow">How tracking works</p>
          <h2>Copy the generated redirect URL into your post</h2>
        </div>
        <p>The tracked link uses the stored content `tid`, so the redirect can carry attribution into the supported booking flow before later booking capture reads it.</p>
        <p>Pick a saved booking link, paste in the public URL for the content you are publishing, then copy the generated tracked link into the content or CTA you share externally. Only booking links with active tracked-content support can be used here today.</p>
        <a href="/app/booking-links" class="inline-link">Review booking links</a>
      </article>
    </section>
    <section class="card stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Creator-owned content</p>
          <h2>{list_heading}</h2>
        </div>
        <p>{len(content_items)} saved</p>
      </div>
      {_render_content_list(content_items=content_items, booking_link_names=booking_link_names)}
    </section>
    """
    return _page_layout(title="Content", body=body)


def _render_content_form_panel(
    *,
    creator_name: str,
    creator_email: str,
    booking_links: list[BookingLinkResponse],
    form_values: dict[str, str],
    field_errors: dict[str, str],
) -> str:
    if not booking_links:
        return """
        <article class="card stack">
          <div>
            <p class="eyebrow">Booking-link prerequisite</p>
            <h2>Create a booking link first</h2>
            <p>You need at least one saved booking link before this page can generate tracked content.</p>
          </div>
          <p>The tracked redirect has to attach every content item to one of your creator-owned booking links, so start there before creating tracked URLs.</p>
          <a href="/app/booking-links" class="inline-link">Open booking-link manager</a>
        </article>
        """

    selectable_booking_links = [
        booking_link
        for booking_link in booking_links
        if booking_provider_supports_creator_visible_tracked_destination(
            booking_link.provider
        )
    ]
    submit_disabled = " disabled" if not selectable_booking_links else ""
    unavailable_note = ""
    if booking_links and not selectable_booking_links:
        unavailable_note = """
        <section class="notice">
          <p class="eyebrow">Tracked content unavailable for current saved links</p>
          <p>These booking sources stay saved, but creator-tracked links currently require a provider with active support. Add a Calendly link to continue.</p>
        </section>
        """

    return f"""
    <article class="card stack">
      <div>
        <p class="eyebrow">Create tracked content</p>
        <h2>Add a source URL</h2>
        <p>Signed in as <strong class="wrap-anywhere">{creator_email}</strong> for <strong class="wrap-anywhere">{creator_name}</strong>.</p>
      </div>
      {unavailable_note}
      <form action="/app/content" method="post">
        <label for="source_url">Public source URL</label>
        <input
          id="source_url"
          name="source_url"
          type="url"
          value="{html.escape(form_values["source_url"])}"
          placeholder="https://example.com/posts/launch-breakdown"
          required
          aria-invalid="{str("source_url" in field_errors).lower()}"
        />
        <p class="form-help">Use the public URL people will actually visit before choosing your booking CTA.</p>
        {_render_content_field_error(field_errors.get("source_url"))}

        <label for="booking_link_id">Booking link</label>
        <select
          id="booking_link_id"
          name="booking_link_id"
          required
          aria-invalid="{str("booking_link_id" in field_errors).lower()}"
        >
          <option value="">Choose one of your saved booking links</option>
          {_render_content_booking_link_options(
              booking_links=booking_links,
              selected_booking_link_id=form_values["booking_link_id"],
          )}
        </select>
        <p class="form-help">This keeps the tracked content aligned with the creator-owned booking link that downstream redirect handling expects. Only booking links with active tracked-content support can be used here.</p>
        {_render_content_field_error(field_errors.get("booking_link_id"))}

        <button type="submit"{submit_disabled}>Generate tracked link</button>
      </form>
    </article>
    """


def _render_content_booking_link_options(
    *,
    booking_links: list[BookingLinkResponse],
    selected_booking_link_id: str,
) -> str:
    options = []
    for booking_link in booking_links:
        supports_tracked_destination = (
            booking_provider_supports_creator_visible_tracked_destination(
                booking_link.provider
            )
        )
        supports_tracked_content = booking_provider_supports_creator_visible_tracked_content(
            booking_link.provider
        )
        selected_attr = " selected" if booking_link.id == selected_booking_link_id else ""
        disabled_attr = "" if supports_tracked_destination else " disabled"
        option_label = booking_link.name
        if supports_tracked_destination and not supports_tracked_content:
            option_label = (
                f"{booking_link.name} "
                "(tracked redirect ready - end-to-end support pending)"
            )
        elif not supports_tracked_destination:
            option_label = (
                f"{booking_link.name} "
                "(tracked redirect not available yet)"
            )
        options.append(
            f'<option value="{html.escape(booking_link.id)}"{selected_attr}{disabled_attr}>'
            f"{html.escape(option_label)}"
            f"</option>"
        )
    return "".join(options)


def _render_content_list(
    *,
    content_items: list[ContentResponse],
    booking_link_names: dict[str, str],
) -> str:
    if not content_items:
        return """
        <section class="empty-state">
          <p class="eyebrow">Empty state</p>
          <h2>Create the first tracked link</h2>
          <p>Add a public source URL above, choose a saved booking link, and this page will generate the redirect URL you can copy into external content.</p>
        </section>
        """

    items = "".join(
        _render_content_card(
            content=content_item,
            booking_link_name=booking_link_names.get(
                content_item.booking_link_id,
                "Unknown booking link",
            ),
        )
        for content_item in content_items
    )
    return f'<div class="content-list">{items}</div>'


def _render_content_card(
    *,
    content: ContentResponse,
    booking_link_name: str,
) -> str:
    return f"""
    <article class="content-card stack">
      <div class="content-card-header">
        <div>
          <p class="eyebrow">Tracked content</p>
          <h2>{html.escape(_content_card_title(content.source_url))}</h2>
        </div>
        <p class="pill-note">Booking link: {html.escape(booking_link_name)}</p>
      </div>
      <p><strong>Source URL</strong>: <a href="{html.escape(content.source_url)}" class="inline-link">{html.escape(content.source_url)}</a></p>
      <p><strong>Tracking ID</strong>: <code>{html.escape(content.tid)}</code></p>
      {_render_copy_field(
          input_id=f"tracked-url-{content.id}",
          label="Tracked link",
          value=content.tracked_url,
      )}
    </article>
    """


def _render_content_notice(
    *,
    status_value: str | None,
    field_errors: dict[str, str],
    created_content: ContentResponse | None,
) -> str:
    if field_errors:
        return """
        <section class="notice error">
          <p class="eyebrow">Fix the highlighted fields</p>
          <p>Use a public URL and one of your saved booking links, then submit again.</p>
        </section>
        """

    if status_value == "created" and created_content is not None:
        return f"""
        <section class="notice success stack">
          <div>
            <p class="eyebrow">Tracked link ready</p>
            <p>Copy this redirect URL into the external content or CTA that should route through attribution.</p>
          </div>
          {_render_copy_field(
              input_id="created-tracked-url",
              label="New tracked link",
              value=created_content.tracked_url,
          )}
          <p><strong>Source URL</strong>: <a href="{html.escape(created_content.source_url)}" class="inline-link">{html.escape(created_content.source_url)}</a></p>
        </section>
        """

    return ""


def _render_content_topic_review_page(
    *,
    current_user: AuthUser,
    content: ContentResponse,
    review: ContentTopicReviewResponse | None,
    status_value: str | None,
    candidate_field_errors: dict[str, str],
    candidate_form_values: dict[str, str],
    prerequisite_detail: str | None,
) -> str:
    creator_name = html.escape(current_user.creator.name)
    creator_email = html.escape(current_user.email)
    authoritative_confirmed_topics = (
        review.authoritative_confirmed_topics if review is not None else []
    )
    candidate_topics = review.candidate_topics if review is not None else []
    notice = _render_content_topic_review_notice(
        status_value=status_value,
        candidate_field_errors=candidate_field_errors,
    )

    body = f"""
    <header class="shell-header">
      <div>
        <p class="eyebrow">Creator Home</p>
        <h1>Topic Review</h1>
        <p class="lede">Confirm, edit, or reject lightweight topic suggestions for one tracked content item at a time before those labels become canonical metadata.</p>
      </div>
      <form action="/sign-out" method="post">
        <button type="submit" class="secondary">Sign out</button>
      </form>
    </header>
    {_render_shell_nav(current_path="/app/content")}
    {notice}
    <section class="grid">
      <article class="card stack">
        <div>
          <p class="eyebrow">Tracked content context</p>
          <h2>{html.escape(_content_card_title(content.source_url))}</h2>
          <p>Signed in as <strong class="wrap-anywhere">{creator_email}</strong> for <strong class="wrap-anywhere">{creator_name}</strong>.</p>
        </div>
        <p><strong>Source URL</strong>: <a href="{html.escape(content.source_url)}" class="inline-link wrap-anywhere">{html.escape(content.source_url)}</a></p>
        <p><strong>Tracking ID</strong>: <code>{html.escape(content.tid)}</code></p>
        {_render_copy_field(
            input_id=f"topic-review-tracked-url-{content.id}",
            label="Tracked link",
            value=content.tracked_url,
        )}
        {_render_content_topic_extraction_summary(review=review, prerequisite_detail=prerequisite_detail)}
      </article>
      <article class="card accent stack">
        <div>
          <p class="eyebrow">Current authority</p>
          <h2>{len(authoritative_confirmed_topics)} authoritative</h2>
        </div>
        <p>Only confirmed topics linked from the authoritative artifact count as current truth. Saved labels in the in-progress review below stay inspectable, but they do not become authoritative until you promote that review.</p>
        {_render_confirmed_topics_list(
            authoritative_confirmed_topics,
            empty_eyebrow="No authoritative topics yet",
            empty_body="Finish the current review and promote it to create the first authoritative topic set for this content.",
        )}
      </article>
    </section>
    {_render_content_topic_review_body(
        content=content,
        review=review,
        candidate_field_errors=candidate_field_errors,
        candidate_form_values=candidate_form_values,
        prerequisite_detail=prerequisite_detail,
    )}
    """
    return _page_layout(title="Topic Review", body=body)


def _render_content_topic_extraction_summary(
    *,
    review: ContentTopicReviewResponse | None,
    prerequisite_detail: str | None,
) -> str:
    if review is None:
        return f"""
        <section class="empty-state">
          <p class="eyebrow">Review prerequisite</p>
          <h2>Fetch and extract first</h2>
          <p>{html.escape(prerequisite_detail or "A canonical extraction artifact is required before topic review can begin.")}</p>
        </section>
        """

    extraction_title = (
        html.escape(review.extraction_title)
        if review.extraction_title
        else "No extracted title stored"
    )
    authority_heading, authority_copy = _content_authority_status_copy(review)
    return f"""
    <section class="topic-summary stack">
      <div class="status-row">
        <div>
          <p class="eyebrow">Latest extraction artifact</p>
          <h2>{extraction_title}</h2>
        </div>
        <span class="status-pill {_content_topic_review_status_badge(review.extraction_status)}">{html.escape(_content_topic_review_status_label(review.extraction_status))}</span>
      </div>
      <p><strong>Method</strong>: {html.escape(review.extraction_method or "Unknown extraction method")}</p>
      <p><strong>Candidate count</strong>: {len(review.candidate_topics)} suggested right now.</p>
      <p><strong>Authority</strong>: {html.escape(authority_heading)}</p>
      <p>{html.escape(authority_copy)}</p>
    </section>
    """


def _render_confirmed_topics_list(
    confirmed_topics,
    *,
    empty_eyebrow: str,
    empty_body: str,
) -> str:
    if not confirmed_topics:
        return f"""
        <section class="empty-state">
          <p class="eyebrow">{html.escape(empty_eyebrow)}</p>
          <p>{html.escape(empty_body)}</p>
        </section>
        """

    topic_chips = "".join(
        f'<span class="topic-chip">{html.escape(topic.canonical_label)}</span>'
        for topic in confirmed_topics
    )
    return f'<div class="topic-chip-list">{topic_chips}</div>'


def _render_content_topic_review_body(
    *,
    content: ContentResponse,
    review: ContentTopicReviewResponse | None,
    candidate_field_errors: dict[str, str],
    candidate_form_values: dict[str, str],
    prerequisite_detail: str | None,
) -> str:
    if review is None:
        return f"""
        <section class="card stack">
          <div>
            <p class="eyebrow">Topic review prerequisite</p>
            <h2>Review waits on the canonical extraction artifact</h2>
          </div>
          <p>{html.escape(prerequisite_detail or "Generate a fetch snapshot and extraction artifact first.")}</p>
          <p>This story intentionally builds on the shipped fetch and extraction contracts instead of widening the app with another ingestion or parsing path.</p>
        </section>
        """

    if not review.candidate_topics:
        return f"""
        <section class="card stack">
          <div class="section-heading">
            <div>
              <p class="eyebrow">Candidate topics</p>
              <h2>No suggestions yet</h2>
            </div>
            <p>{len(review.review_confirmed_topics)} confirmed in this review</p>
          </div>
          <p>Generate a lightweight candidate set from the latest extraction artifact, then confirm, edit, or reject each suggestion one content item at a time.</p>
          <form action="/app/content/{quote(content.tid, safe='')}/topics/candidates" method="post">
            <button type="submit">Generate topic candidates</button>
          </form>
        </section>
        """

    confirmed_label_by_id = {
        topic.id: topic.canonical_label
        for topic in review.review_confirmed_topics
    }
    candidate_cards = "".join(
        _render_content_topic_candidate_card(
            content_tid=content.tid,
            candidate=candidate,
            current_confirmed_label=confirmed_label_by_id.get(candidate.confirmed_topic_id),
            field_error=candidate_field_errors.get(candidate.id),
            form_value=candidate_form_values.get(candidate.id),
        )
        for candidate in review.candidate_topics
    )
    return f"""
        <section class="card stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Candidate topics</p>
          <h2>Review {len(review.candidate_topics)} suggestion{"s" if len(review.candidate_topics) != 1 else ""}</h2>
        </div>
        <form action="/app/content/{quote(content.tid, safe='')}/topics/candidates" method="post">
          <button type="submit" class="secondary">Reuse latest candidates</button>
        </form>
      </div>
      <p>Confirm the labels you trust, edit the ones that need cleanup, and reject weak suggestions before they become canonical metadata.</p>
      {_render_content_authoritative_promotion_controls(content=content, review=review)}
      <div class="content-list">{candidate_cards}</div>
    </section>
    """


def _render_content_authoritative_promotion_controls(
    *,
    content: ContentResponse,
    review: ContentTopicReviewResponse,
) -> str:
    authority_state = review.authoritative_state
    if authority_state.is_current_artifact_authoritative:
        return """
        <section class="notice success">
          <p class="eyebrow">Current authoritative evidence</p>
          <p>This latest reviewed artifact already supplies the current canonical content evidence and citation path.</p>
        </section>
        """

    if authority_state.promotion_allowed:
        replacement_copy = (
            "Promoting this review will replace the previous authoritative artifact."
            if authority_state.authoritative_extraction_artifact_id is not None
            else "Promoting this review will create the first authoritative evidence state for this content."
        )
        return f"""
        <section class="notice success">
          <p class="eyebrow">Review complete</p>
          <p>{html.escape(replacement_copy)}</p>
          <form action="/app/content/{quote(content.tid, safe='')}/topics/promote" method="post">
            <button type="submit">Promote as current evidence</button>
          </form>
        </section>
        """

    pending_copy = (
        "The previous authoritative artifact stays current until you promote a fully reviewed replacement."
        if authority_state.authoritative_extraction_artifact_id is not None
        else "This content still has no authoritative evidence yet."
    )
    return f"""
    <section class="notice error">
      <p class="eyebrow">Promotion not ready</p>
      <p>{html.escape(authority_state.promotion_block_reason or "Promotion is not available yet.")}</p>
      <p>{html.escape(pending_copy)}</p>
    </section>
    """


def _render_content_topic_candidate_card(
    *,
    content_tid: str,
    candidate,
    current_confirmed_label: str | None,
    field_error: str | None,
    form_value: str | None,
) -> str:
    input_value = form_value if form_value is not None else current_confirmed_label or candidate.suggested_label
    status_label = _content_topic_candidate_status_label(candidate.review_status)
    status_copy = _content_topic_candidate_status_copy(
        review_status=candidate.review_status,
        current_confirmed_label=current_confirmed_label,
    )
    return f"""
    <article class="content-card stack">
      <div class="content-card-header">
        <div>
          <p class="eyebrow">Candidate topic #{candidate.candidate_rank}</p>
          <h2>{html.escape(candidate.suggested_label)}</h2>
        </div>
        <span class="status-pill {_content_topic_candidate_status_badge(candidate.review_status)}">{html.escape(status_label)}</span>
      </div>
      <p><strong>Suggestion source</strong>: {html.escape(_content_topic_suggestion_method_label(candidate.suggestion_method))}</p>
      <p>{html.escape(status_copy)}</p>
      <form action="/app/content/{quote(content_tid, safe='')}/topics/{candidate.id}/confirm" method="post" class="stack">
        <label for="confirmed_label_{candidate.id}">Confirmed topic</label>
        <input
          id="confirmed_label_{candidate.id}"
          name="confirmed_label"
          type="text"
          value="{html.escape(input_value)}"
          aria-invalid="{str(field_error is not None).lower()}"
        />
        {_render_content_field_error(field_error)}
        <button type="submit">Save confirmed topic</button>
      </form>
      <form action="/app/content/{quote(content_tid, safe='')}/topics/{candidate.id}/reject" method="post">
        <button type="submit" class="secondary">Reject candidate</button>
      </form>
    </article>
    """


def _render_content_topic_review_notice(
    *,
    status_value: str | None,
    candidate_field_errors: dict[str, str],
) -> str:
    if candidate_field_errors:
        return """
        <section class="notice error">
          <p class="eyebrow">Fix the highlighted topic label</p>
          <p>Enter the confirmed topic you want to keep, then submit again.</p>
        </section>
        """

    if status_value == "generated":
        return """
        <section class="notice success">
          <p class="eyebrow">Topic candidates ready</p>
          <p>Review the suggested labels below, then confirm, edit, or reject them one content item at a time.</p>
        </section>
        """

    if status_value == "saved":
        return """
        <section class="notice success">
          <p class="eyebrow">Confirmed topic saved</p>
          <p>The canonical topic set for this content item now reflects your latest confirmed label.</p>
        </section>
        """

    if status_value == "rejected":
        return """
        <section class="notice success">
          <p class="eyebrow">Candidate rejected</p>
          <p>The rejected suggestion remains in lineage for this extraction artifact, but it no longer counts as canonical metadata.</p>
        </section>
        """

    if status_value == "promoted":
        return """
        <section class="notice success">
          <p class="eyebrow">Authoritative evidence updated</p>
          <p>The latest reviewed artifact is now the current canonical content evidence state for this content item.</p>
        </section>
        """

    if status_value == "unavailable":
        return """
        <section class="notice error">
          <p class="eyebrow">Topic review is not ready yet</p>
          <p>The latest extraction artifact is missing or does not contain usable text for candidate generation yet.</p>
        </section>
        """

    if status_value == "promotion-unavailable":
        return """
        <section class="notice error">
          <p class="eyebrow">Promotion is not ready yet</p>
          <p>Finish reviewing the latest topic candidate set before promoting it as the current authoritative evidence state.</p>
        </section>
        """

    return ""


def _content_authority_status_copy(review: ContentTopicReviewResponse) -> tuple[str, str]:
    authority_state = review.authoritative_state
    if authority_state.is_current_artifact_authoritative:
        return (
            "Current authoritative evidence",
            "This latest reviewed artifact already supplies the canonical content evidence and citation path.",
        )
    if authority_state.authoritative_extraction_artifact_id is not None:
        return (
            "Previous authority remains current",
            "A previously promoted artifact still supplies canonical evidence while this latest artifact is under review.",
        )
    return (
        "No authoritative evidence yet",
        "Finish the current review and promote it to create the first canonical content evidence state for this content.",
    )


def _content_topic_review_status_label(status_value: str) -> str:
    return status_value.replace("_", " ").title()


def _content_topic_review_status_badge(status_value: str) -> str:
    normalized = status_value.strip().lower()
    if normalized == "succeeded":
        return "confirmed"
    if normalized == "low_confidence":
        return "pending"
    return "rejected"


def _content_topic_candidate_status_label(status_value: str) -> str:
    normalized = status_value.strip().lower()
    if normalized == "confirmed":
        return "Confirmed"
    if normalized == "rejected":
        return "Rejected"
    return "Pending review"


def _content_topic_candidate_status_badge(status_value: str) -> str:
    normalized = status_value.strip().lower()
    if normalized == "confirmed":
        return "confirmed"
    if normalized == "rejected":
        return "rejected"
    return "pending"


def _content_topic_candidate_status_copy(
    *,
    review_status: str,
    current_confirmed_label: str | None,
) -> str:
    normalized = review_status.strip().lower()
    if normalized == "confirmed" and current_confirmed_label:
        return f"Currently confirmed as {current_confirmed_label}."
    if normalized == "rejected":
        return "This candidate is currently rejected for canonical metadata."
    return "This candidate is still waiting on creator review."


def _content_topic_suggestion_method_label(method: str) -> str:
    if method == "title_full":
        return "Suggested from the extracted title"
    if method == "title_segment":
        return "Suggested from a title segment"
    if method == "title_keywords":
        return "Suggested from title keywords"
    if method == "text_keywords":
        return "Suggested from extracted text keywords"
    return method.replace("_", " ").title()


def _render_booking_activity_page(
    *,
    current_user: AuthUser,
    booking_activity: list[BookingActivityResponse],
) -> str:
    creator_name = html.escape(current_user.creator.name)
    creator_email = html.escape(current_user.email)
    list_heading = "Recent booking activity" if booking_activity else "No booking activity yet"

    body = f"""
    <header class="shell-header">
      <div>
        <p class="eyebrow">Creator Home</p>
        <h1>Booking Activity</h1>
        <p class="lede">See whether tracked content is turning into verified provider bookings, without needing raw DB checks or API tooling.</p>
      </div>
      <form action="/sign-out" method="post">
        <button type="submit" class="secondary">Sign out</button>
      </form>
    </header>
    {_render_shell_nav(current_path="/app/bookings")}
    <section class="grid">
      <article class="card stack">
        <div>
          <p class="eyebrow">Captured bookings</p>
          <h2>Creator-scoped activity only</h2>
          <p>Signed in as <strong class="wrap-anywhere">{creator_email}</strong> for <strong class="wrap-anywhere">{creator_name}</strong>.</p>
        </div>
        <p>This visibility slice shows booking status, timestamps, booking-link context, and the current attribution state so unattributed bookings stay explicit instead of disappearing into null-only checks.</p>
        <p>Client PII, invoices, revenue reporting, and deeper analytics stay out of scope for Story 41.</p>
      </article>
      <article class="card accent stack">
        <div>
          <p class="eyebrow">Attribution timing</p>
          <h2>New bookings may take a moment to appear</h2>
        </div>
        <p>Bookings only show up here after someone uses a tracked link and the booking provider delivers the verified webhook back to this app. That handoff is not always instant.</p>
        <p>If you just created tracked content, publish the redirect URL first, complete a booking through that path, then refresh this page after the provider callback lands.</p>
        <a href="/app/content" class="inline-link">Open content manager</a>
      </article>
    </section>
    <section class="card stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Creator-owned bookings</p>
          <h2>{list_heading}</h2>
        </div>
        <p>{len(booking_activity)} captured</p>
      </div>
      {_render_booking_activity_list(booking_activity)}
    </section>
    """
    return _page_layout(title="Booking Activity", body=body)


def _render_experiments_page(
    *,
    current_user: AuthUser,
    experiment_run: CreatorNextContentExperimentsResult | None,
    status_value: str | None,
    readiness_summary: CreatorNextContentExperimentsReadinessSummary,
    showing_specific_snapshot: bool,
    operator_can_review_drafts: bool,
    operator_draft_provider_configured: bool,
    operator_draft_run: CreatorOperatorExperimentDraftRunResult | None,
    showing_specific_operator_draft: bool,
) -> str:
    snapshot_heading = _experiments_snapshot_heading(
        experiment_run=experiment_run,
        showing_specific_snapshot=showing_specific_snapshot,
    )

    body = f"""
    <header class="shell-header">
      <div>
        <p class="eyebrow">Creator Home</p>
        <h1>Experiments</h1>
        <p class="lede">Generate a strict evidence-backed read of the next content experiments most supported by your authoritative topics and attributed paid results.</p>
      </div>
      <form action="/sign-out" method="post">
        <button type="submit" class="secondary">Sign out</button>
      </form>
    </header>
    {_render_shell_nav(current_path="/app/experiments")}
    {_render_experiments_notice(status_value=status_value)}
    <section class="grid">
      {_render_experiments_readiness_panel(
          current_user=current_user,
          readiness_summary=readiness_summary,
      )}
      <article class="card accent stack">
        <div>
          <p class="eyebrow">Grounding rules</p>
          <h2>Keep unsupported and diagnostics separate</h2>
        </div>
        <p>Each card shown here must clear one authoritative content pattern plus one settled paid pattern. If that bar is not met, the page stays `unsupported` instead of guessing.</p>
        <p>Each stored snapshot now preserves explicit helper lineage plus drilldown links to the exact card evidence it used.</p>
      </article>
    </section>
    <section class="card stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Stored helper output</p>
          <h2>{snapshot_heading}</h2>
        </div>
        <p>{html.escape(_experiments_snapshot_meta(experiment_run))}</p>
      </div>
      {_render_experiment_results(
          experiment_run=experiment_run,
          readiness_summary=readiness_summary,
      )}
    </section>
    {_render_operator_experiment_draft_section(
        current_user=current_user,
        readiness_summary=readiness_summary,
        operator_can_review_drafts=operator_can_review_drafts,
        operator_draft_provider_configured=operator_draft_provider_configured,
        operator_draft_run=operator_draft_run,
        showing_specific_operator_draft=showing_specific_operator_draft,
    )}
    """
    return _page_layout(title="Experiments", body=body)


def _render_setup_home_experiments_handoff(
    experiments_handoff: dict[str, str],
) -> str:
    return f"""
        <article class="topic-summary stack">
          <div>
            <p class="eyebrow">Secondary helper</p>
            <h2>{html.escape(experiments_handoff['title'])}</h2>
          </div>
          <p>{html.escape(experiments_handoff['body'])}</p>
          <p><a href="{html.escape(experiments_handoff['action_href'])}" class="button-link secondary">{html.escape(experiments_handoff['action_label'])}</a></p>
        </article>
    """


def _render_experiments_readiness_panel(
    *,
    current_user: AuthUser,
    readiness_summary: CreatorNextContentExperimentsReadinessSummary,
) -> str:
    creator_name = html.escape(current_user.creator.name)
    creator_email = html.escape(current_user.email)
    current_ready = readiness_summary.current_status == EXPERIMENT_RUN_STATUS_READY
    badge_label = "Ready" if current_ready else "Unsupported"
    badge_class = "confirmed" if current_ready else "pending"
    heading = (
        "Helper is ready from current evidence"
        if current_ready
        else "Helper is unsupported from current evidence"
    )
    body_copy = (
        "The current authoritative content and settled paid results are enough for the helper to prepare a fresh stored snapshot."
        if current_ready
        else "The current authoritative content and settled paid results are not yet enough for the helper to prepare a fresh stored snapshot."
    )
    readiness_note = _render_experiments_readiness_note(
        readiness_summary=readiness_summary,
    )
    unsupported_reasons = _render_experiments_current_unsupported_reasons(
        readiness_summary=readiness_summary,
    )
    next_steps = _render_experiments_next_steps(
        readiness_summary=readiness_summary,
    )
    generate_button_class = "" if current_ready else ' class="secondary"'
    return f"""
      <article class="card stack">
        <div class="status-row">
          <div>
            <p class="eyebrow">Current helper readiness</p>
            <h2>{heading}</h2>
            <p>Signed in as <strong class="wrap-anywhere">{creator_email}</strong> for <strong class="wrap-anywhere">{creator_name}</strong>.</p>
          </div>
          <span class="status-pill {badge_class}">{badge_label}</span>
        </div>
        <p>This helper returns only `ready` or `unsupported`. It does not fill gaps with generic advice, and it uses stored authoritative content plus settled paid evidence rather than raw diagnostics.</p>
        <p>{body_copy}</p>
        {readiness_note}
        {unsupported_reasons}
        {next_steps}
        <form action="/app/experiments" method="post">
          <button type="submit"{generate_button_class}>Generate next experiments</button>
        </form>
      </article>
    """


def _render_experiments_readiness_note(
    *,
    readiness_summary: CreatorNextContentExperimentsReadinessSummary,
) -> str:
    current_ready = readiness_summary.current_status == EXPERIMENT_RUN_STATUS_READY
    latest_ready_run = readiness_summary.latest_ready_run
    latest_run = readiness_summary.latest_run
    if current_ready:
        if latest_ready_run is None:
            return (
                "<p>No ready snapshot is stored yet. Generate one when you want a fresh read-only snapshot.</p>"
            )
        ready_snapshot_link = (
            f'<p><a href="/app/experiments?claim_snapshot_id={html.escape(str(latest_ready_run.claim_snapshot_id))}" '
            'class="button-link secondary">Open latest ready snapshot</a></p>'
        )
        if latest_run is not None and latest_run.claim_snapshot_id == latest_ready_run.claim_snapshot_id:
            return (
                "<p>The latest stored snapshot is already ready. Review it directly or generate a fresh snapshot if the evidence has changed.</p>"
                + ready_snapshot_link
            )
        return (
            "<p>Current evidence is ready now, but the most recent ready snapshot is historical stored output. Review it directly or generate a fresh snapshot for the latest read.</p>"
            + ready_snapshot_link
        )

    latest_ready_note = ""
    if latest_ready_run is not None:
        latest_ready_note = (
            "<p>Current readiness stays unsupported, but you can still review the latest stored ready snapshot as a historical artifact. It does not auto-refresh into current status.</p>"
            f'<p><a href="/app/experiments?claim_snapshot_id={html.escape(str(latest_ready_run.claim_snapshot_id))}" class="button-link secondary">Open latest ready snapshot</a></p>'
        )
    return (
        "<p>Lead with current readiness here. Review stored snapshots only as historical helper output, not as current evidence status.</p>"
        + latest_ready_note
    )


def _render_experiments_current_unsupported_reasons(
    *,
    readiness_summary: CreatorNextContentExperimentsReadinessSummary,
) -> str:
    unsupported_explanation = readiness_summary.unsupported_explanation
    if unsupported_explanation is None:
        return ""

    reason_items = "".join(
        f"<li>{html.escape(reason)}</li>"
        for reason in unsupported_explanation.reasons
    )
    current_activity_note = ""
    if unsupported_explanation.has_excluded_current_activity:
        current_activity_note = (
            "<p>Some newer activity is still excluded here until it resolves into attributed booking state or settled paid evidence.</p>"
        )
    return f"""
    <div class="stack">
      <p class="eyebrow">Still blocked today</p>
      <h3>Why this helper is still unsupported</h3>
      <ul class="reason-list">{reason_items}</ul>
      {current_activity_note}
    </div>
    """


def _render_experiments_next_steps(
    *,
    readiness_summary: CreatorNextContentExperimentsReadinessSummary,
) -> str:
    if readiness_summary.current_status == EXPERIMENT_RUN_STATUS_READY:
        return ""

    return """
    <div class="stack">
      <p class="eyebrow">Next safe action</p>
      <h3>Review the evidence this helper still needs</h3>
      <p><a href="/app/content" class="button-link secondary">Review content evidence</a></p>
      <p><a href="/app/reports" class="button-link secondary">Review paid results</a></p>
    </div>
    """


def _render_experiments_notice(*, status_value: str | None) -> str:
    if status_value == "generated":
        return """
        <section class="notice success">
          <p class="eyebrow">Fresh snapshot ready</p>
          <p>Generated a new experiment snapshot from the current authoritative content and settled paid evidence.</p>
        </section>
        """
    if status_value == "operator-draft-generated":
        return """
        <section class="notice success">
          <p class="eyebrow">Operator draft ready</p>
          <p>Generated a new operator-only draft snapshot from the current evidence-backed candidate set.</p>
        </section>
        """
    if status_value == "operator-draft-unavailable":
        return """
        <section class="notice error">
          <p class="eyebrow">Operator draft generator unavailable</p>
          <p>Configure the OpenAI draft provider before generating an operator-only draft snapshot.</p>
        </section>
        """
    if status_value == "operator-draft-not-ready":
        return """
        <section class="notice error">
          <p class="eyebrow">Operator draft generator is blocked</p>
          <p>Current helper readiness must be <code>ready</code> before the operator-only draft generator can run.</p>
        </section>
        """
    if status_value == "operator-draft-failed":
        return """
        <section class="notice error">
          <p class="eyebrow">Operator draft generation failed</p>
          <p>The provider call did not produce a valid draft snapshot. No operator draft was stored.</p>
        </section>
        """
    return ""


def _render_operator_experiment_draft_section(
    *,
    current_user: AuthUser,
    readiness_summary: CreatorNextContentExperimentsReadinessSummary,
    operator_can_review_drafts: bool,
    operator_draft_provider_configured: bool,
    operator_draft_run: CreatorOperatorExperimentDraftRunResult | None,
    showing_specific_operator_draft: bool,
) -> str:
    if not operator_can_review_drafts:
        return ""

    current_ready = readiness_summary.current_status == EXPERIMENT_RUN_STATUS_READY
    availability_copy = ""
    if not current_ready:
        availability_copy = """
        <section class="empty-state">
          <p class="eyebrow">Operator-only draft generator</p>
          <h2>Draft generation stays locked until current readiness is ready</h2>
          <p>The deterministic helper above remains the source of current helper truth. Come back here only after the current workspace clears the normal evidence bar.</p>
        </section>
        """
    elif not operator_draft_provider_configured:
        availability_copy = """
        <section class="empty-state">
          <p class="eyebrow">Operator-only draft generator</p>
          <h2>OpenAI draft provider is not configured</h2>
          <p>This draft layer stays internal and operator-only. Configure the OpenAI API key before generating any draft hypotheses.</p>
        </section>
        """
    elif operator_draft_run is None:
        availability_copy = """
        <section class="empty-state">
          <p class="eyebrow">No operator draft yet</p>
          <h2>No operator-only draft snapshot exists yet</h2>
          <p>The deterministic helper above remains the shipped control path. Generate a separate operator-only draft when you want to compare an LLM-backed hypothesis pass against that baseline.</p>
        </section>
        """

    generate_button = ""
    if current_ready and operator_draft_provider_configured:
        generate_button = """
        <form action="/app/operator/experiments/drafts" method="post">
          <button type="submit" class="secondary">Generate operator draft</button>
        </form>
        """

    snapshot_heading = (
        "Selected operator draft"
        if showing_specific_operator_draft and operator_draft_run is not None
        else "Latest operator draft"
    )
    draft_results = ""
    if operator_draft_run is not None:
        draft_results = _render_operator_experiment_draft_results(
            operator_draft_run=operator_draft_run,
            snapshot_heading=snapshot_heading,
        )

    return f"""
    <section class="card stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Operator-only draft experiments</p>
          <h2>Review non-canonical LLM draft hypotheses beside the shipped helper</h2>
        </div>
        <p>Allowlisted operator only</p>
      </div>
      <p>Signed in as <strong class="wrap-anywhere">{html.escape(current_user.email)}</strong> for <strong class="wrap-anywhere">{html.escape(current_user.creator.name)}</strong>.</p>
      <p>The deterministic helper above remains the control path. This section stores separate operator-only draft runs so LLM output never becomes current creator-visible helper truth by accident.</p>
      {availability_copy}
      {generate_button}
      {draft_results}
    </section>
    """


def _render_operator_experiment_draft_results(
    *,
    operator_draft_run: CreatorOperatorExperimentDraftRunResult,
    snapshot_heading: str,
) -> str:
    items = "".join(
        _render_operator_experiment_draft_card(
            card=card,
            index=index,
        )
        for index, card in enumerate(operator_draft_run.cards, start=1)
    )
    return f"""
    <section class="stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Stored operator draft</p>
          <h2>{html.escape(snapshot_heading)}</h2>
        </div>
        <p>{_format_timestamp_in_utc(operator_draft_run.created_at)}</p>
      </div>
      <p><strong>Draft run ID</strong>: <code>{html.escape(str(operator_draft_run.draft_run_id))}</code></p>
      <p>{html.escape(operator_draft_run.summary)}</p>
      {_render_experiment_lineage_block(label="Draft run lineage", lineage=operator_draft_run.lineage)}
      {_render_experiment_version_semantics_block(
          label="Draft version semantics",
          version_semantics=operator_draft_run.version_semantics,
      )}
      {_render_experiment_freshness_policy_block(
          label="Draft freshness policy",
          freshness_policy=operator_draft_run.freshness_policy,
      )}
      <div class="content-list">{items}</div>
    </section>
    """


def _render_operator_experiment_draft_card(
    *,
    card,
    index: int,
) -> str:
    authoritative_topics = ", ".join(card.authoritative_topics) or "None recorded"
    paid_result_count = len(card.settled_paid_results)
    paid_revenue_by_currency: dict[str, int] = {}
    for paid_result in card.settled_paid_results:
        paid_revenue_by_currency[paid_result.currency] = (
            paid_revenue_by_currency.get(paid_result.currency, 0) + paid_result.amount_cents
        )
    paid_revenue_summary = ", ".join(
        f"{currency} {_format_money_from_cents(amount_cents)}"
        for currency, amount_cents in sorted(paid_revenue_by_currency.items())
    )
    if not paid_revenue_summary:
        paid_revenue_summary = "No settled paid revenue recorded"

    return f"""
    <article class="topic-summary stack">
      <div>
        <p class="eyebrow">Draft card {index}</p>
        <h3>{html.escape(card.title)}</h3>
      </div>
      <p><strong>Hypothesis</strong>: {html.escape(card.hypothesis)}</p>
      <p><strong>Why this might work</strong>: {html.escape(card.why_this_might_work)}</p>
      <p><strong>Evidence summary</strong>: {html.escape(card.evidence_summary)}</p>
      <p><strong>Ranking rationale</strong>: {html.escape(card.ranking_rationale or "Not recorded")}</p>
      <p><strong>Caution</strong>: {html.escape(card.caution)}</p>
      <p><strong>Tracking ID</strong>: <code>{html.escape(card.content_tid)}</code></p>
      <p><strong>Authoritative source</strong>: <a href="{html.escape(card.authoritative_source_url)}" class="inline-link">{html.escape(card.authoritative_source_url)}</a></p>
      <p><strong>Authoritative title</strong>: {html.escape(card.authoritative_artifact_title or "Untitled artifact")}</p>
      <p><strong>Confirmed topics</strong>: {html.escape(authoritative_topics)}</p>
      <p><strong>Settled paid pattern</strong>: {paid_result_count} paid result{"s" if paid_result_count != 1 else ""} totaling {html.escape(paid_revenue_summary)}.</p>
      <p><strong>Claim snapshot</strong>: <code>{html.escape(str(card.claim_snapshot_id))}</code></p>
      {_render_experiment_lineage_block(label="Draft card lineage", lineage=card.lineage)}
    </article>
    """


def _render_experiment_results(
    *,
    experiment_run: CreatorNextContentExperimentsResult | None,
    readiness_summary: CreatorNextContentExperimentsReadinessSummary,
) -> str:
    if experiment_run is None:
        if readiness_summary.current_status == EXPERIMENT_RUN_STATUS_READY:
            return """
        <section class="empty-state">
          <p class="eyebrow">No stored snapshot yet</p>
          <h2>Current evidence is ready, but no stored snapshot exists yet</h2>
          <p>Generate a fresh snapshot when you want the helper to store the current evidence-backed read. Refreshing the page does not create a new helper run.</p>
        </section>
        """
        return """
        <section class="empty-state">
          <p class="eyebrow">No stored snapshot yet</p>
          <h2>No stored helper snapshot exists yet</h2>
          <p>This page stays read-only until you explicitly generate a snapshot. Refreshing the page does not create a new helper run.</p>
        </section>
        """

    if experiment_run.status == EXPERIMENT_RUN_STATUS_UNSUPPORTED:
        return _render_experiment_unsupported_state(
            experiment_run=experiment_run,
            readiness_summary=readiness_summary,
        )

    items = "".join(
        _render_experiment_card(
            index=index,
            experiment=experiment,
            run_claim_snapshot_id=experiment_run.claim_snapshot_id,
        )
        for index, experiment in enumerate(experiment_run.experiments, start=1)
    )
    snapshot_context_note = _render_experiment_snapshot_context_note(
        experiment_run=experiment_run,
        readiness_summary=readiness_summary,
    )
    return f"""
    <section class="stack">
      <div class="status-row">
        <div>
          <p class="eyebrow">Stored snapshot status</p>
          <h2>{html.escape(experiment_run.summary)}</h2>
        </div>
        <span class="status-pill confirmed">Ready</span>
      </div>
      {snapshot_context_note}
      <p><strong>Claim snapshot</strong>: <code>{html.escape(str(experiment_run.claim_snapshot_id))}</code></p>
      {_render_experiment_lineage_block(label="Run lineage", lineage=experiment_run.lineage)}
      {_render_experiment_version_semantics_block(
          label="Version semantics",
          version_semantics=experiment_run.version_semantics,
      )}
      {_render_experiment_freshness_policy_block(
          label="Freshness policy",
          freshness_policy=experiment_run.freshness_policy,
      )}
      <div class="content-list">{items}</div>
    </section>
    """


def _render_experiment_unsupported_state(
    *,
    experiment_run: CreatorNextContentExperimentsResult,
    readiness_summary: CreatorNextContentExperimentsReadinessSummary,
) -> str:
    unsupported_explanation = readiness_summary.unsupported_explanation
    reasons_html = ""
    if unsupported_explanation is not None and unsupported_explanation.reasons:
        reason_items = "".join(
            f"<li>{html.escape(reason)}</li>"
            for reason in unsupported_explanation.reasons
        )
        explanation_heading = (
            "Why this helper is still unsupported"
            if readiness_summary.current_status == EXPERIMENT_RUN_STATUS_UNSUPPORTED
            else "Why this stored snapshot was unsupported"
        )
        reasons_html = f"""
        <div class="stack">
          <p class="eyebrow">Stored snapshot explanation</p>
          <h3>{html.escape(explanation_heading)}</h3>
          <ul class="reason-list">{reason_items}</ul>
        </div>
        """

    current_activity_note = ""
    if unsupported_explanation is not None and unsupported_explanation.has_excluded_current_activity:
        current_activity_note = (
            """
        <p>Some newer activity is still excluded here until it resolves into attributed booking state or settled paid evidence.</p>
        """
            if readiness_summary.current_status == EXPERIMENT_RUN_STATUS_UNSUPPORTED
            else """
        <p>At generation time, some newer activity was still excluded until it resolved into attributed booking state or settled paid evidence.</p>
        """
        )

    snapshot_context_note = _render_experiment_snapshot_context_note(
        experiment_run=experiment_run,
        readiness_summary=readiness_summary,
    )

    return f"""
    <section class="empty-state">
      <p class="eyebrow">Stored snapshot status</p>
      <h2>Not enough trusted evidence yet</h2>
      <p>{html.escape(experiment_run.summary)}</p>
      {snapshot_context_note}
      <p><strong>Claim snapshot</strong>: <code>{html.escape(str(experiment_run.claim_snapshot_id))}</code></p>
      {_render_experiment_lineage_block(label="Run lineage", lineage=experiment_run.lineage)}
      {_render_experiment_version_semantics_block(
          label="Version semantics",
          version_semantics=experiment_run.version_semantics,
      )}
      {_render_experiment_freshness_policy_block(
          label="Freshness policy",
          freshness_policy=experiment_run.freshness_policy,
      )}
      {reasons_html}
      {current_activity_note}
    </section>
    """


def _render_experiment_snapshot_context_note(
    *,
    experiment_run: CreatorNextContentExperimentsResult,
    readiness_summary: CreatorNextContentExperimentsReadinessSummary,
) -> str:
    if (
        readiness_summary.current_status == EXPERIMENT_RUN_STATUS_UNSUPPORTED
        and experiment_run.status == EXPERIMENT_RUN_STATUS_READY
    ):
        return (
            "<p>This ready snapshot is historical stored output. Current helper readiness is unsupported today, so treat this snapshot as earlier evidence-backed output rather than current status.</p>"
        )
    if (
        readiness_summary.current_status == EXPERIMENT_RUN_STATUS_READY
        and experiment_run.status == EXPERIMENT_RUN_STATUS_UNSUPPORTED
    ):
        return (
            "<p>This unsupported snapshot is historical stored output. Current evidence is ready now, so generate a fresh snapshot for the latest read.</p>"
        )
    return ""


def _render_experiment_card(
    *,
    index: int,
    experiment,
    run_claim_snapshot_id: uuid.UUID,
) -> str:
    content_tids = " ".join(
        f"<code>{html.escape(content_tid)}</code>"
        for content_tid in experiment.content_tids
    )
    card_id_html = (
        f"<p><strong>Card ID</strong>: <code>{html.escape(experiment.card_id)}</code></p>"
        if experiment.card_id is not None
        else ""
    )
    card_link = _experiment_card_link(
        run_claim_snapshot_id=run_claim_snapshot_id,
        experiment=experiment,
        index=index,
    )
    return f"""
    <article class="content-card stack">
      <div class="content-card-header">
        <div>
          <p class="eyebrow">Experiment {index}</p>
          <h2>{html.escape(experiment.title)}</h2>
        </div>
        <span class="status-pill confirmed">Hypothesis</span>
      </div>
      {card_id_html}
      <p><strong>Hypothesis</strong>: {html.escape(experiment.hypothesis)}</p>
      <p><strong>Why this might work</strong>: {html.escape(experiment.why_this_might_work)}</p>
      <p><strong>Evidence summary</strong>: {html.escape(experiment.evidence_summary)}</p>
      {_render_experiment_ranking_rationale(
          label="Why this is ranked here",
          ranking_rationale=experiment.ranking_rationale,
          include_not_recorded=False,
      )}
      <p><strong>Content tracking ID</strong>: {content_tids}</p>
      <p><strong>Caution</strong>: {html.escape(experiment.caution)}</p>
      <p><a href="{html.escape(card_link)}" class="inline-link">View evidence</a></p>
    </article>
    """


def _render_experiment_card_drilldown_page(
    *,
    current_user: AuthUser,
    drilldown: CreatorNextContentExperimentCardDrilldown,
) -> str:
    creator_name = html.escape(current_user.creator.name)
    creator_email = html.escape(current_user.email)
    topic_list = "".join(
        f"<li>{html.escape(topic)}</li>"
        for topic in drilldown.authoritative_topics
    )
    settled_rows = "".join(
        f"""
        <tr>
          <td><code>{html.escape(result.content_tid)}</code></td>
          <td>{_format_timestamp_in_utc(result.booked_at)}</td>
          <td>{_format_timestamp_in_utc(result.paid_at)}</td>
          <td>{html.escape(_reports_currency_amount_copy(result.currency, result.amount_cents))}</td>
        </tr>
        """
        for result in drilldown.settled_paid_results
    )
    authoritative_title = (
        f"<p><strong>Authoritative artifact title</strong>: {html.escape(drilldown.authoritative_artifact_title)}</p>"
        if drilldown.authoritative_artifact_title
        else ""
    )

    body = f"""
    <header class="shell-header">
      <div>
        <p class="eyebrow">Creator Home</p>
        <h1>Experiment evidence</h1>
        <p class="lede">Inspect the exact authoritative content and settled paid results behind this experiment card.</p>
      </div>
      <form action="/sign-out" method="post">
        <button type="submit" class="secondary">Sign out</button>
      </form>
    </header>
    {_render_shell_nav(current_path="/app/experiments")}
    <section class="card stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Card {drilldown.card_order}</p>
          <h2>{html.escape(drilldown.title)}</h2>
        </div>
        <a href="/app/experiments?claim_snapshot_id={html.escape(str(drilldown.run_claim_snapshot_id))}" class="inline-link">Back to experiments</a>
      </div>
      <p>Signed in as <strong class="wrap-anywhere">{creator_email}</strong> for <strong class="wrap-anywhere">{creator_name}</strong>.</p>
      <p><strong>Parent run snapshot</strong>: <code>{html.escape(str(drilldown.run_claim_snapshot_id))}</code></p>
      <p><strong>Card snapshot</strong>: <code>{html.escape(str(drilldown.card_claim_snapshot_id))}</code></p>
      <p><strong>Card ID</strong>: <code>{html.escape(_lineage_copy(drilldown.card_id))}</code></p>
      <p><strong>Generated</strong>: {_format_timestamp_in_utc(drilldown.created_at)}</p>
      {_render_experiment_lineage_block(label="Run lineage", lineage=drilldown.run_lineage)}
      {_render_experiment_lineage_block(label="Card lineage", lineage=drilldown.card_lineage)}
      {_render_experiment_version_semantics_block(
          label="Version semantics",
          version_semantics=drilldown.version_semantics,
      )}
      {_render_experiment_freshness_policy_block(
          label="Freshness policy",
          freshness_policy=drilldown.freshness_policy,
      )}
      <p><strong>Hypothesis</strong>: {html.escape(drilldown.hypothesis)}</p>
      <p><strong>Why this might work</strong>: {html.escape(drilldown.why_this_might_work)}</p>
      {_render_experiment_ranking_rationale(
          label="Why this is ranked here",
          ranking_rationale=drilldown.ranking_rationale,
          include_not_recorded=True,
      )}
      <p><strong>Caution</strong>: {html.escape(drilldown.caution)}</p>
    </section>
    <section class="grid">
      <article class="card stack">
        <div>
          <p class="eyebrow">Authoritative content used</p>
          <h2><code>{html.escape(drilldown.authoritative_content_tid)}</code></h2>
        </div>
        <p><strong>Source URL</strong>: <span class="wrap-anywhere">{html.escape(drilldown.authoritative_source_url)}</span></p>
        {authoritative_title}
        <div>
          <p><strong>Confirmed topics</strong></p>
          <ul class="reason-list">{topic_list}</ul>
        </div>
      </article>
      <article class="card stack">
        <div>
          <p class="eyebrow">Settled paid results used</p>
          <h2>{html.escape(_count_copy(len(drilldown.settled_paid_results), "paid result"))}</h2>
        </div>
        <table class="data-table">
          <thead>
            <tr>
              <th>TID</th>
              <th>Booked</th>
              <th>Paid</th>
              <th>Amount</th>
            </tr>
          </thead>
          <tbody>{settled_rows}</tbody>
        </table>
      </article>
    </section>
    """
    return _page_layout(title="Experiment Evidence", body=body)


def _render_experiment_compare_page(
    *,
    current_user: AuthUser,
    comparison: CreatorNextContentExperimentsRunComparison,
) -> str:
    creator_name = html.escape(current_user.creator.name)
    creator_email = html.escape(current_user.email)
    baseline = comparison.baseline_run
    candidate = comparison.candidate_run
    card_sections = "".join(
        _render_experiment_card_comparison(
            index=index,
            stable_card_id=card_comparison.stable_card_id,
            baseline_card=card_comparison.baseline_card,
            candidate_card=card_comparison.candidate_card,
        )
        for index, card_comparison in enumerate(comparison.card_comparisons, start=1)
    )
    if not card_sections:
        card_sections = """
        <section class="empty-state">
          <p class="eyebrow">No cards to compare</p>
          <p>Both runs are unsupported, so there are no ready cards to compare by stable card identity.</p>
        </section>
        """

    body = f"""
    <header class="shell-header">
      <div>
        <p class="eyebrow">Creator Home</p>
        <h1>Experiment comparison</h1>
        <p class="lede">Compare two stored experiment snapshots without changing the underlying evidence contract.</p>
      </div>
      <form action="/sign-out" method="post">
        <button type="submit" class="secondary">Sign out</button>
      </form>
    </header>
    {_render_shell_nav(current_path="/app/experiments")}
    <section class="card stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Stored run comparison</p>
          <h2>Historical vs current helper output</h2>
        </div>
        <p>{html.escape(_count_copy(len(comparison.card_comparisons), "card comparison"))}</p>
      </div>
      <p>Signed in as <strong class="wrap-anywhere">{creator_email}</strong> for <strong class="wrap-anywhere">{creator_name}</strong>.</p>
      <p><strong>Baseline snapshot</strong>: <code>{html.escape(str(baseline.claim_snapshot_id))}</code></p>
      <p><strong>Candidate snapshot</strong>: <code>{html.escape(str(candidate.claim_snapshot_id))}</code></p>
    </section>
    <section class="grid">
      {_render_experiment_run_comparison_card(label="Baseline run", experiment_run=baseline)}
      {_render_experiment_run_comparison_card(label="Candidate run", experiment_run=candidate)}
    </section>
    <section class="stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Card comparison</p>
          <h2>Stable-card compared stored output</h2>
        </div>
      </div>
      {card_sections}
    </section>
    """
    return _page_layout(title="Experiment Comparison", body=body)


def _render_experiment_run_comparison_card(
    *,
    label: str,
    experiment_run: CreatorNextContentExperimentsResult,
) -> str:
    status_copy = "Ready" if experiment_run.status == EXPERIMENT_RUN_STATUS_READY else "Unsupported"
    return f"""
    <article class="card stack">
      <div>
        <p class="eyebrow">{html.escape(label)}</p>
        <h2>{html.escape(experiment_run.summary)}</h2>
      </div>
      <p><strong>Status</strong>: {html.escape(status_copy)}</p>
      <p><strong>Claim snapshot</strong>: <code>{html.escape(str(experiment_run.claim_snapshot_id))}</code></p>
      <p><strong>Generated</strong>: {_format_timestamp_in_utc(experiment_run.created_at)}</p>
      {_render_experiment_lineage_block(label="Run lineage", lineage=experiment_run.lineage)}
      {_render_experiment_version_semantics_block(
          label="Version semantics",
          version_semantics=experiment_run.version_semantics,
      )}
      {_render_experiment_freshness_policy_block(
          label="Freshness policy",
          freshness_policy=experiment_run.freshness_policy,
      )}
      <p><a href="/app/experiments?claim_snapshot_id={html.escape(str(experiment_run.claim_snapshot_id))}" class="inline-link">Open this snapshot</a></p>
    </article>
    """


def _render_experiment_card_comparison(
    *,
    index: int,
    stable_card_id: str | None,
    baseline_card: NextContentExperimentCard | None,
    candidate_card: NextContentExperimentCard | None,
) -> str:
    stable_card_id_html = (
        f"<p><strong>Stable card ID</strong>: <code>{html.escape(stable_card_id)}</code></p>"
        if stable_card_id is not None
        else "<p><strong>Stable card ID</strong>: <code>Not recorded</code></p>"
    )
    return f"""
    <section class="card stack">
      <div>
        <p class="eyebrow">Comparison group {index}</p>
        <h2>Stored helper output</h2>
      </div>
      {stable_card_id_html}
      <div class="grid">
        {_render_experiment_card_comparison_side(label=f"Baseline card {index}", experiment=baseline_card)}
        {_render_experiment_card_comparison_side(label=f"Candidate card {index}", experiment=candidate_card)}
      </div>
    </section>
    """


def _render_experiment_card_comparison_side(
    *,
    label: str,
    experiment: NextContentExperimentCard | None,
) -> str:
    if experiment is None:
        return f"""
        <article class="card stack">
          <div>
            <p class="eyebrow">{html.escape(label)}</p>
            <h2>No card in this run</h2>
          </div>
        </article>
        """

    content_tids = " ".join(
        f"<code>{html.escape(content_tid)}</code>"
        for content_tid in experiment.content_tids
    )
    lineage_html = (
        _render_experiment_lineage_block(
            label="Card lineage",
            lineage=experiment.lineage,
        )
        if experiment.lineage is not None
        else ""
    )
    claim_snapshot_html = (
        f"<p><strong>Card snapshot</strong>: <code>{html.escape(str(experiment.card_claim_snapshot_id))}</code></p>"
        if experiment.card_claim_snapshot_id is not None
        else ""
    )
    card_id_html = (
        f"<p><strong>Card ID</strong>: <code>{html.escape(experiment.card_id)}</code></p>"
        if experiment.card_id is not None
        else "<p><strong>Card ID</strong>: <code>Not recorded</code></p>"
    )
    return f"""
    <article class="card stack">
      <div>
        <p class="eyebrow">{html.escape(label)}</p>
        <h2>{html.escape(experiment.title)}</h2>
      </div>
      {claim_snapshot_html}
      {card_id_html}
      <p><strong>Hypothesis</strong>: {html.escape(experiment.hypothesis)}</p>
      <p><strong>Why this might work</strong>: {html.escape(experiment.why_this_might_work)}</p>
      <p><strong>Evidence summary</strong>: {html.escape(experiment.evidence_summary)}</p>
      {_render_experiment_ranking_rationale(
          label="Why this is ranked here",
          ranking_rationale=experiment.ranking_rationale,
          include_not_recorded=True,
      )}
      <p><strong>Content tracking ID</strong>: {content_tids}</p>
      <p><strong>Caution</strong>: {html.escape(experiment.caution)}</p>
      {lineage_html}
    </article>
    """


def _render_experiment_lineage_block(
    *,
    label: str,
    lineage: HelperGenerationLineage,
) -> str:
    return f"""
    <div class="stack">
      <p class="eyebrow">{html.escape(label)}</p>
      <ul class="reason-list">
        <li><strong>Generator type</strong>: <code>{html.escape(_lineage_copy(lineage.generator_type))}</code></li>
        <li><strong>Model</strong>: <code>{html.escape(_lineage_copy(lineage.model_name))}</code></li>
        <li><strong>Prompt version</strong>: <code>{html.escape(_lineage_copy(lineage.prompt_version))}</code></li>
        <li><strong>Config version</strong>: <code>{html.escape(_lineage_copy(lineage.config_version))}</code></li>
        <li><strong>Contract version</strong>: <code>{html.escape(_lineage_copy(lineage.contract_version))}</code></li>
        <li><strong>Reducer version</strong>: <code>{html.escape(_lineage_copy(lineage.reducer_version))}</code></li>
      </ul>
    </div>
    """


def _render_experiment_version_semantics_block(
    *,
    label: str,
    version_semantics: HelperVersionSemantics,
) -> str:
    return f"""
    <div class="stack">
      <p class="eyebrow">{html.escape(label)}</p>
      <ul class="reason-list">
        <li><strong>Schema version</strong>: <code>{html.escape(version_semantics.schema_version)}</code></li>
        <li><strong>Evidence input version</strong>: <code>{html.escape(version_semantics.evidence_input_version)}</code></li>
        <li><strong>Generation config version</strong>: <code>{html.escape(_lineage_copy(version_semantics.generation_config_version))}</code></li>
      </ul>
    </div>
    """


def _render_experiment_freshness_policy_block(
    *,
    label: str,
    freshness_policy: HelperFreshnessPolicy,
) -> str:
    return f"""
    <div class="stack">
      <p class="eyebrow">{html.escape(label)}</p>
      <ul class="reason-list">
        <li><strong>Policy version</strong>: <code>{html.escape(freshness_policy.policy_version)}</code></li>
        <li><strong>Authoritative content window</strong>: {html.escape(freshness_policy.authoritative_content_window)}</li>
        <li><strong>Settled paid window</strong>: {html.escape(freshness_policy.settled_paid_window)}</li>
        <li><strong>Rerun behavior</strong>: {html.escape(freshness_policy.rerun_behavior)}</li>
      </ul>
    </div>
    """


def _render_experiment_ranking_rationale(
    *,
    label: str,
    ranking_rationale: str | None,
    include_not_recorded: bool,
) -> str:
    if ranking_rationale is None and not include_not_recorded:
        return ""
    return (
        f"<p><strong>{html.escape(label)}</strong>: "
        f"{html.escape(_lineage_copy(ranking_rationale) if include_not_recorded else ranking_rationale or '')}</p>"
    )


def _lineage_copy(value: str | None) -> str:
    if value is None:
        return "Not recorded"
    return value


def _experiment_card_link(
    *,
    run_claim_snapshot_id: uuid.UUID,
    experiment: NextContentExperimentCard,
    index: int,
) -> str:
    if experiment.card_id is not None:
        return f"/app/experiments/{run_claim_snapshot_id}/cards/by-id/{experiment.card_id}"
    return f"/app/experiments/{run_claim_snapshot_id}/cards/{index}"


def _experiments_snapshot_meta(
    experiment_run: CreatorNextContentExperimentsResult | None,
) -> str:
    if experiment_run is None:
        return "No runs yet"
    return _format_timestamp_in_utc(experiment_run.created_at)


def _experiments_snapshot_heading(
    *,
    experiment_run: CreatorNextContentExperimentsResult | None,
    showing_specific_snapshot: bool,
) -> str:
    if experiment_run is None:
        return "No stored snapshot yet"
    if showing_specific_snapshot:
        return "Stored snapshot"
    return "Latest stored snapshot"


def _render_reports_page(
    *,
    current_user: AuthUser,
    content_items: list[ContentResponse],
    readiness: dict[str, object],
    summary: CreatorReportsSummary,
    filter_values: dict[str, str],
    field_errors: dict[str, str],
) -> str:
    creator_name = html.escape(current_user.creator.name)
    creator_email = html.escape(current_user.email)
    filters_active = _reports_filters_are_active(filter_values)
    visible_row_count = len(summary.rows)
    total_row_count = len(content_items)
    list_heading = (
        "Content funnel summary"
        if summary.rows
        else ("No paid results in this window" if filters_active else "No tracked content yet")
    )
    clear_filters_link = (
        '<a href="/app/reports" class="inline-link">Clear filters</a>'
        if filters_active
        else ""
    )
    export_link = _render_reports_export_link(
        filter_values=filter_values,
        field_errors=field_errors,
    )
    results_visibility_copy = html.escape(
        _reports_row_visibility_copy(
            visible_count=visible_row_count,
            total_count=total_row_count,
            filters_active=filters_active,
        )
    )

    body = f"""
    <header class="shell-header">
      <div>
        <p class="eyebrow">Creator Home</p>
        <h1>Reports</h1>
        <p class="lede">Review which tracked content is producing bookings and paid results, while keeping invoice-backed revenue truth separate from diagnostic backlog state.</p>
      </div>
      <form action="/sign-out" method="post">
        <button type="submit" class="secondary">Sign out</button>
      </form>
    </header>
    {_render_shell_nav(current_path="/app/reports")}
    {_render_reports_surface_nav(
        current_path="/app/reports",
        filter_values=filter_values,
    )}
    {_render_reports_notice(field_errors=field_errors)}
    <section class="card stack report-toolbar-card">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Paid-date filter</p>
          <h2>Invoice-backed paid outcomes</h2>
        </div>
        <p class="report-toolbar-meta">Signed in as <strong class="wrap-anywhere">{creator_email}</strong> for <strong class="wrap-anywhere">{creator_name}</strong>.</p>
      </div>
      <p class="report-scope-note">Paid date changes only paid totals and paid window labels. Booking counts and row status below stay current across your tracked content.</p>
      <form action="/app/reports" method="get">
        <div class="filter-row">
          <div>
            <label for="start_date">Start date</label>
            <input
              id="start_date"
              name="start_date"
              type="date"
              value="{html.escape(filter_values["start_date"], quote=True)}"
              aria-invalid="{str("start_date" in field_errors).lower()}"
            />
            {_render_reports_field_error(field_errors.get("start_date"))}
          </div>
          <div>
            <label for="end_date">End date</label>
            <input
              id="end_date"
              name="end_date"
              type="date"
              value="{html.escape(filter_values["end_date"], quote=True)}"
              aria-invalid="{str("end_date" in field_errors or "date_range" in field_errors).lower()}"
            />
            {_render_reports_field_error(field_errors.get("end_date"))}
          </div>
        </div>
        {_render_reports_field_error(field_errors.get("date_range"))}
        <div class="filter-actions">
          <button type="submit">Apply filters</button>
          {clear_filters_link}
          {export_link}
        </div>
      </form>
    </section>
    <section class="report-answer-strip">
      <article class="stat-tile">
        <p class="eyebrow">Paid revenue</p>
        <p class="stat-value">{html.escape(_format_money_from_cents(summary.paid_revenue_cents))}</p>
      </article>
      <article class="stat-tile">
        <p class="eyebrow">Paid invoices</p>
        <p class="stat-value">{html.escape(str(summary.paid_invoice_count))}</p>
        <p>{html.escape(_count_copy(summary.paid_invoice_count, "paid invoice"))}</p>
      </article>
      <article class="stat-tile">
        <p class="eyebrow">Paid bookings</p>
        <p class="stat-value">{html.escape(str(summary.paid_booking_count))}</p>
        <p>{html.escape(_count_copy(summary.paid_booking_count, "paid booking"))}</p>
      </article>
      <article class="stat-tile report-diagnostic-tile">
        <div>
          <p class="eyebrow">Diagnostic only</p>
          <h2>Payments still outside totals</h2>
        </div>
        <p><strong>Current unmatched backlog</strong>: {html.escape(_unmatched_payment_backlog_copy(summary.unattributed_current_backlog.event_count))}</p>
        {_render_reports_unmatched_explainer(summary)}
        {_render_reports_unmatched_reasons(summary)}
        {_render_reports_unmatched_explanation_link(
            summary=summary,
            filter_values=filter_values,
        )}
      </article>
      <article class="stat-tile report-diagnostic-tile">
        <div>
          <p class="eyebrow">Blocked before invoicing</p>
          <h2>{html.escape(_blocked_billing_backlog_copy(summary.blocked_summary.open_case_count))}</h2>
        </div>
        {_render_reports_blocked_reasons(summary)}
        <p><a href="/app/attention" class="inline-link">Open Attention for case details and retry actions</a></p>
      </article>
    </section>
    <section class="card stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Content funnel</p>
          <h2>{list_heading}</h2>
        </div>
        <p>{results_visibility_copy}</p>
      </div>
      <p class="report-section-intro">Start with counted revenue, then compare current activity and anything still excluded from totals for each content row.</p>
      {_render_reports_results(
          content_items=content_items,
          readiness=readiness,
          summary=summary,
          filters_active=filters_active,
          filter_values=filter_values,
      )}
    </section>
    """
    return _page_layout(title="Reports", body=body)


def _render_reports_topics_page(
    *,
    current_user: AuthUser,
    content_items: list[ContentResponse],
    summary: CreatorReportsTopicSummary,
    filter_values: dict[str, str],
    field_errors: dict[str, str],
) -> str:
    creator_name = html.escape(current_user.creator.name)
    creator_email = html.escape(current_user.email)
    filters_active = _reports_filters_are_active(filter_values)
    list_heading = (
        "Topic analytics"
        if summary.rows
        else (
            "No topic results in this window"
            if filters_active and summary.has_any_authoritative_topics
            else (
                "No authoritative topics yet"
                if content_items
                else "No tracked content yet"
            )
        )
    )
    clear_filters_link = (
        '<a href="/app/reports/topics" class="inline-link">Clear filters</a>'
        if filters_active
        else ""
    )
    back_to_reports_href = html.escape(_reports_page_href(filter_values), quote=True)

    body = f"""
    <header class="shell-header">
      <div>
        <p class="eyebrow">Creator Home</p>
        <h1>Topic analytics</h1>
        <p class="lede">Compare where authoritative confirmed topics are showing up in the existing content funnel without treating topic rows as a second creator-wide total.</p>
      </div>
      <form action="/sign-out" method="post">
        <button type="submit" class="secondary">Sign out</button>
      </form>
    </header>
    {_render_shell_nav(current_path="/app/reports")}
    {_render_reports_surface_nav(
        current_path="/app/reports/topics",
        filter_values=filter_values,
    )}
    {_render_reports_notice(field_errors=field_errors)}
    <section class="grid">
      <article class="card stack">
        <div>
          <p class="eyebrow">Paid-date filter</p>
          <h2>Compare confirmed topics in the paid window</h2>
          <p>Signed in as <strong class="wrap-anywhere">{creator_email}</strong> for <strong class="wrap-anywhere">{creator_name}</strong>.</p>
        </div>
        <p>Use the paid date below to narrow grouped paid outcomes by when the counted invoice payment landed. Booking counts below still reflect the visible content rows attached to each topic grouping, not booking-date slices.</p>
        <form action="/app/reports/topics" method="get">
          <div class="filter-row">
            <div>
              <label for="start_date">Start date</label>
              <input
                id="start_date"
                name="start_date"
                type="date"
                value="{html.escape(filter_values["start_date"], quote=True)}"
                aria-invalid="{str("start_date" in field_errors).lower()}"
              />
              {_render_reports_field_error(field_errors.get("start_date"))}
            </div>
            <div>
              <label for="end_date">End date</label>
              <input
                id="end_date"
                name="end_date"
                type="date"
                value="{html.escape(filter_values["end_date"], quote=True)}"
                aria-invalid="{str("end_date" in field_errors or "date_range" in field_errors).lower()}"
              />
              {_render_reports_field_error(field_errors.get("end_date"))}
            </div>
          </div>
          {_render_reports_field_error(field_errors.get("date_range"))}
          <div class="filter-actions">
            <button type="submit">Apply filters</button>
            {clear_filters_link}
            <a href="{back_to_reports_href}" class="inline-link">View content funnel totals</a>
          </div>
        </form>
      </article>
      <article class="card accent stack">
        <div>
          <p class="eyebrow">Comparison rules</p>
          <h2>Confirmed-topic groupings are not a second total</h2>
        </div>
        <p>Only authoritative confirmed topics count here. Pending, rejected, or non-authoritative topic candidates stay out of this summary.</p>
        <p>A single content row can appear under more than one confirmed topic, so these grouped topic rows are comparisons rather than a partition of your overall revenue totals.</p>
        <p>Use the content funnel page when you want one creator-level total without topic overlap.</p>
      </article>
    </section>
    <section class="card stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Authoritative topics</p>
          <h2>{list_heading}</h2>
        </div>
        <p>{html.escape(_count_copy(len(summary.rows), "topic row"))} visible</p>
      </div>
      <p class="report-scope-note">Confirmed-topic rows can overlap. Use them to compare where paid outcomes are showing up, not as an additive creator-wide total.</p>
      {_render_reports_topic_results(
          content_items=content_items,
          summary=summary,
          filters_active=filters_active,
      )}
    </section>
    """
    return _page_layout(title="Topic analytics", body=body)


def _render_reports_booking_links_page(
    *,
    current_user: AuthUser,
    content_items: list[ContentResponse],
    booking_links: list[BookingLinkResponse],
    summary: CreatorReportsBookingLinkSummary,
    filter_values: dict[str, str],
    field_errors: dict[str, str],
) -> str:
    creator_name = html.escape(current_user.creator.name)
    creator_email = html.escape(current_user.email)
    filters_active = _reports_filters_are_active(filter_values)
    list_heading = (
        "Booking-link analytics"
        if summary.rows
        else (
            "No booking-link results in this window"
            if filters_active and booking_links
            else ("No booking links yet" if not booking_links else "No tracked content yet")
        )
    )
    clear_filters_link = (
        '<a href="/app/reports/booking-links" class="inline-link">Clear filters</a>'
        if filters_active
        else ""
    )
    back_to_reports_href = html.escape(_reports_page_href(filter_values), quote=True)

    body = f"""
    <header class="shell-header">
      <div>
        <p class="eyebrow">Creator Home</p>
        <h1>Booking-link analytics</h1>
        <p class="lede">Compare outcomes by saved booking-link identity without turning reports into CTA experimentation, campaign analytics, or a second revenue truth.</p>
      </div>
      <form action="/sign-out" method="post">
        <button type="submit" class="secondary">Sign out</button>
      </form>
    </header>
    {_render_shell_nav(current_path="/app/reports")}
    {_render_reports_surface_nav(
        current_path="/app/reports/booking-links",
        filter_values=filter_values,
    )}
    {_render_reports_notice(field_errors=field_errors)}
    <section class="grid">
      <article class="card stack">
        <div>
          <p class="eyebrow">Paid-date filter</p>
          <h2>Compare saved links in the paid window</h2>
          <p>Signed in as <strong class="wrap-anywhere">{creator_email}</strong> for <strong class="wrap-anywhere">{creator_name}</strong>.</p>
        </div>
        <p>Use the paid date below to narrow grouped paid outcomes by when the counted invoice payment landed. Grouped booking counts still describe the visible content rows attached to each saved booking link, not booking-date slices.</p>
        <form action="/app/reports/booking-links" method="get">
          <div class="filter-row">
            <div>
              <label for="start_date">Start date</label>
              <input
                id="start_date"
                name="start_date"
                type="date"
                value="{html.escape(filter_values["start_date"], quote=True)}"
                aria-invalid="{str("start_date" in field_errors).lower()}"
              />
              {_render_reports_field_error(field_errors.get("start_date"))}
            </div>
            <div>
              <label for="end_date">End date</label>
              <input
                id="end_date"
                name="end_date"
                type="date"
                value="{html.escape(filter_values["end_date"], quote=True)}"
                aria-invalid="{str("end_date" in field_errors or "date_range" in field_errors).lower()}"
              />
              {_render_reports_field_error(field_errors.get("end_date"))}
            </div>
          </div>
          {_render_reports_field_error(field_errors.get("date_range"))}
          <div class="filter-actions">
            <button type="submit">Apply filters</button>
            {clear_filters_link}
            <a href="{back_to_reports_href}" class="inline-link">View content funnel totals</a>
          </div>
        </form>
      </article>
      <article class="card accent stack">
        <div>
          <p class="eyebrow">Comparison rules</p>
          <h2>Booking-link rows stay tied to saved link identity</h2>
        </div>
        <p>This page groups visible content rows by stable booking-link identity, not by a mutable link name or destination string.</p>
        <p>If the current stored link name or billing defaults changed later, historical bookings and paid outcomes still stay attached to the same saved booking-link row here.</p>
        <p>Use the content funnel page when you want content-level totals, or the topic page when you want grouped content comparisons by authoritative confirmed topic.</p>
      </article>
    </section>
    <section class="card stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Saved booking links</p>
          <h2>{list_heading}</h2>
        </div>
        <p>{html.escape(_count_copy(len(summary.rows), "booking link row"))} visible</p>
      </div>
      <p class="report-scope-note">Each row stays tied to one saved booking-link identity. Current names, URLs, and defaults shown below are present-day metadata, not historical proof.</p>
      {_render_reports_booking_link_results(
          content_items=content_items,
          booking_links=booking_links,
          summary=summary,
          filters_active=filters_active,
      )}
    </section>
    """
    return _page_layout(title="Booking-link analytics", body=body)


def _render_attention_page(
    *,
    current_user: AuthUser,
    blocked_cases: list[BlockedBillingCaseSummary],
    unmatched_events: list[UnmatchedPaymentEventSummary],
    status_value: str | None,
) -> str:
    creator_name = html.escape(current_user.creator.name)
    creator_email = html.escape(current_user.email)
    blocked_count = len(blocked_cases)
    unmatched_count = len(unmatched_events)
    overview = build_attention_overview_view(
        blocked_count=blocked_count,
        unmatched_count=unmatched_count,
    )

    body = f"""
    <header class="shell-header">
      <div>
        <p class="eyebrow">Creator Home</p>
        <h1>Attention</h1>
        <p class="lede">Review the diagnostic items the shell keeps separate from paid totals: tracked bookings blocked before invoicing and verified payments whose attribution chain is still incomplete.</p>
      </div>
      <form action="/sign-out" method="post">
        <button type="submit" class="secondary">Sign out</button>
      </form>
    </header>
    {_render_shell_nav(current_path="/app/attention")}
    {_render_attention_notice(status_value=status_value)}
    <section class="grid">
      <article class="card stack">
        <div>
          <p class="eyebrow">Blocked billing</p>
          <h2>{html.escape(overview.blocked_heading)}</h2>
          <p>Signed in as <strong class="wrap-anywhere">{creator_email}</strong> for <strong class="wrap-anywhere">{creator_name}</strong>.</p>
        </div>
        <p>{html.escape(overview.blocked_backlog_copy)}</p>
        <p>{html.escape(overview.blocked_explainer)}</p>
      </article>
      <article class="card accent stack">
        <div>
          <p class="eyebrow">Unresolved payments</p>
          <h2>{html.escape(overview.unmatched_heading)}</h2>
        </div>
        <p>{html.escape(overview.unmatched_backlog_copy)}</p>
        <p>{html.escape(overview.unmatched_explainer)}</p>
      </article>
    </section>
    <section class="card stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Blocked billing</p>
          <h2>Current blocked billing details</h2>
        </div>
        <p>{html.escape(_count_copy(blocked_count, "open case"))}</p>
      </div>
      {_render_blocked_billing_case_list(blocked_cases)}
    </section>
    <section class="card stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Unresolved payments</p>
          <h2>Current unmatched payment diagnostics</h2>
        </div>
        <p>{html.escape(_count_copy(unmatched_count, "event"))}</p>
      </div>
      {_render_unmatched_payment_event_list(unmatched_events)}
    </section>
    """
    return _page_layout(title="Attention", body=body)


def _render_health_page(
    *,
    current_user: AuthUser,
    snapshot: CreatorEvidenceIngressHealthSnapshot,
) -> str:
    creator_name = html.escape(current_user.creator.name)
    creator_email = html.escape(current_user.email)
    payment_state_summary = ", ".join(
        f"{item.row_count} {_health_payment_state_label(item.state).lower()} row"
        f"{'' if item.row_count == 1 else 's'}"
        for item in snapshot.payment_provenance.settled_state_counts
    )

    body = f"""
    <header class="shell-header">
      <div>
        <p class="eyebrow">Creator Home</p>
        <h1>Health</h1>
        <p class="lede">Review the creator-scoped attribution, ingress, billing, and evidence checks that explain why this workspace is clear, degraded, or still unsupported.</p>
      </div>
      <form action="/sign-out" method="post">
        <button type="submit" class="secondary">Sign out</button>
      </form>
    </header>
    {_render_shell_nav(current_path="/app/health")}
    <section class="grid">
      <article class="card stack">
        <div>
          <p class="eyebrow">Workspace</p>
          <h2>{creator_name}</h2>
          <p>Signed in as <strong class="wrap-anywhere">{creator_email}</strong>.</p>
        </div>
        <p><strong>{_count_copy(snapshot.booking_attribution.unattributed_booking_count, "unattributed booking")}</strong> still waiting on canonical tracked-content linkage.</p>
      </article>
      <article class="card accent stack">
        <div>
          <p class="eyebrow">Calendly ingress</p>
          <h2>{_count_copy(snapshot.calendly_ingress.backlog_event_count, "backlog event")}</h2>
          <p>{_count_copy(snapshot.calendly_ingress.failed_event_count, "failed event")} currently need operator review.</p>
        </div>
      </article>
      <article class="card accent stack">
        <div>
          <p class="eyebrow">FullScope ingress</p>
          <h2>{_count_copy(snapshot.fullscope_ingress.backlog_event_count, "backlog event")}</h2>
          <p>{_count_copy(snapshot.fullscope_ingress.failed_event_count, "failed event")} currently need operator review.</p>
        </div>
      </article>
      <article class="card stack">
        <div>
          <p class="eyebrow">Payment provenance</p>
          <h2>{_count_copy(snapshot.payment_provenance.current_backlog_event_count, "backlog event")}</h2>
          <p>{html.escape(payment_state_summary)} across the current settled paid rows.</p>
        </div>
      </article>
      <article class="card stack">
        <div>
          <p class="eyebrow">Blocked billing</p>
          <h2>{_count_copy(snapshot.blocked_billing.open_case_count, "open case")}</h2>
          <p>Use Attention to retry invoice creation only when the stored blocking condition has actually changed.</p>
        </div>
      </article>
      <article class="card accent stack">
        <div>
          <p class="eyebrow">Authoritative content</p>
          <h2>{_count_copy(snapshot.authoritative_content.lagging_content_count, "lagging content item")}</h2>
          <p>These are the content rows most likely to keep helper output unsupported or stale.</p>
        </div>
      </article>
    </section>
    <section class="card stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Booking attribution</p>
          <h2>Bookings still missing trusted attribution</h2>
        </div>
        <p>{html.escape(_count_copy(snapshot.booking_attribution.unattributed_booking_count, "booking"))}</p>
      </div>
      {_render_health_reason_list(
          items=[
              f"{_count_copy(item.booking_count, 'booking')} with {_booking_attribution_reason_label(item.reason).lower()}. {_booking_attribution_reason_explanation(item.reason)}"
              for item in snapshot.booking_attribution.reasons
              if item.booking_count > 0
          ],
          empty_heading="No unattributed bookings are waiting right now",
          empty_body="Current bookings for this creator already have canonical tracked-content linkage.",
      )}
      <p><a href="/app/bookings" class="inline-link">Review booking activity</a></p>
    </section>
    {_render_health_ingress_section(
        provider_label="Calendly",
        snapshot=snapshot.calendly_ingress,
        empty_heading="No Calendly backlog or failures are waiting right now",
        empty_body="Verified Calendly events for this creator are not currently sitting in backlog or failure states.",
    )}
    {_render_health_ingress_section(
        provider_label="FullScope",
        snapshot=snapshot.fullscope_ingress,
        empty_heading="No FullScope backlog or failures are waiting right now",
        empty_body="Verified FullScope events for this creator are not currently sitting in backlog or failure states.",
    )}
    <section class="card stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Payment provenance</p>
          <h2>Paid rows and backlog still waiting on linkage</h2>
        </div>
        <p>{html.escape(_count_copy(snapshot.payment_provenance.current_backlog_event_count, "backlog event"))}</p>
      </div>
      {_render_health_reason_list(
          items=[
              f"{_count_copy(item.row_count, 'settled row')} currently marked {_health_payment_state_label(item.state).lower()}."
              for item in snapshot.payment_provenance.settled_state_counts
              if item.row_count > 0
          ]
          + [
              f"{_count_copy(item.event_count, 'backlog event')} due to {_reports_reason_label(item.reason).lower()}. {_reports_reason_explanation(item.reason)}"
              for item in snapshot.payment_provenance.current_backlog_reasons
              if item.event_count > 0
          ],
          empty_heading="No payment backlog is waiting right now",
          empty_body="Current creator-scoped paid rows do not have a separate unmatched payment backlog attached to them.",
      )}
      <p><a href="/app/attention" class="inline-link">Open Attention for blocked or unmatched details</a></p>
    </section>
    {"".join(
        _render_health_payment_provider_section(
            snapshot=item,
        )
        for item in snapshot.payment_provenance.provider_health
        if _should_render_health_payment_provider_section(
            snapshot=item,
            current_billing_provider=current_user.creator.resolved_billing_provider,
        )
    )}
    <section class="card stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Blocked billing</p>
          <h2>Invoice creation still waiting on a safe retry</h2>
        </div>
        <p>{html.escape(_count_copy(snapshot.blocked_billing.open_case_count, "open case"))}</p>
      </div>
      {_render_health_reason_list(
          items=[
              f"{_count_copy(item.case_count, 'open case')} due to {_blocked_billing_reason_label(item.reason_code).lower()}. {_blocked_billing_reason_explanation(item.reason_code)}"
              for item in snapshot.blocked_billing.reasons
              if item.case_count > 0
          ],
          empty_heading="No blocked billing cases are waiting right now",
          empty_body="This creator does not currently have invoice creation cases waiting on retry or repair.",
      )}
      <p><a href="/app/attention" class="inline-link">Review blocked billing cases</a></p>
    </section>
    <section class="card stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Authoritative content</p>
          <h2>Content evidence still lagging current truth</h2>
        </div>
        <p>{html.escape(_count_copy(snapshot.authoritative_content.lagging_content_count, "content item"))}</p>
      </div>
      {_render_health_reason_list(
          items=[
              f"{_count_copy(item.content_count, 'content item')} with {_health_authoritative_lag_reason_label(item.reason).lower()}. {_health_authoritative_lag_reason_explanation(item.reason)}"
              for item in snapshot.authoritative_content.reasons
              if item.content_count > 0
          ],
          empty_heading="No authoritative-content lag is waiting right now",
          empty_body="The latest promotable reviewed content for this creator is already authoritative or has not yet produced a lagging state.",
      )}
      <p><a href="/app/content" class="inline-link">Review content and topic authority</a> before expecting helper output to become ready.</p>
    </section>
    """
    return _page_layout(title="Health", body=body)


def _render_booking_activity_list(
    booking_activity: list[BookingActivityResponse],
) -> str:
    if not booking_activity:
        return """
        <section class="empty-state">
          <p class="eyebrow">Empty state</p>
          <h2>No bookings captured yet</h2>
          <p>Bookings appear here only after someone uses one of your tracked links and the verified provider webhook is processed, so a brand-new booking may not appear immediately.</p>
          <p>Create tracked content, make sure the redirect URL is the one being shared, then check back here after the provider handoff completes.</p>
          <a href="/app/content" class="inline-link">Create tracked content</a>
        </section>
        """

    items = "".join(
        _render_booking_activity_card(booking=booking)
        for booking in booking_activity
    )
    return f'<div class="activity-list">{items}</div>'


def _render_booking_activity_card(*, booking: BookingActivityResponse) -> str:
    status = _booking_activity_status(booking.status)
    canceled_at_line = ""
    if booking.canceled_at is not None:
        canceled_at_line = (
            f"<p><strong>Canceled at</strong>: "
            f"{_format_timestamp_in_utc(booking.canceled_at)}</p>"
        )
    source_url_line = (
        f'<p><strong>Source URL</strong>: <a href="{html.escape(booking.source_url)}" class="inline-link">{html.escape(booking.source_url)}</a></p>'
        if booking.source_url is not None
        else "<p><strong>Source URL</strong>: Not linked to tracked content yet.</p>"
    )
    tracking_id_line = (
        f"<p><strong>Tracking ID</strong>: <code>{html.escape(booking.tid)}</code></p>"
        if booking.tid is not None
        else "<p><strong>Tracking ID</strong>: Not available yet.</p>"
    )
    attribution_reason_line = ""
    if booking.attribution_status == BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED:
        attribution_reason_line = (
            f"<p><strong>Attribution reason</strong>: "
            f"{html.escape(_booking_attribution_reason_label(booking.attribution_reason))}. "
            f"{html.escape(_booking_attribution_reason_explanation(booking.attribution_reason))}</p>"
        )

    return f"""
    <article class="activity-card stack">
      <div class="activity-card-header">
        <div>
          <p class="eyebrow">Booking activity</p>
          <h2>{html.escape(_booking_activity_title(booking))}</h2>
        </div>
        <span class="status-pill {html.escape(status["badge_class"])}">{html.escape(status["label"])}</span>
      </div>
      <p><strong>Booked at</strong>: {_format_timestamp_in_utc(booking.booked_at)}</p>
      {canceled_at_line}
      <p><strong>Booking link</strong>: {html.escape(booking.booking_link_name)}</p>
      <p><strong>Attribution</strong>: {html.escape(_booking_attribution_status_label(booking.attribution_status))}</p>
      {attribution_reason_line}
      {source_url_line}
      {tracking_id_line}
    </article>
    """


def _render_reports_notice(*, field_errors: dict[str, str]) -> str:
    if not field_errors:
        return ""

    return """
    <section class="notice error">
      <p class="eyebrow">Fix the paid-date filters</p>
      <p>Use valid dates and keep the start date on or before the end date. The summary below is showing the current unfiltered results.</p>
    </section>
    """


def _render_reports_results(
    *,
    content_items: list[ContentResponse],
    readiness: CreatorWorkspaceReadiness,
    summary: CreatorReportsSummary,
    filters_active: bool,
    filter_values: dict[str, str],
) -> str:
    if not summary.rows:
        return _render_reports_empty_state(
            readiness=readiness,
            filters_active=filters_active,
        )

    items = "".join(
        _render_reports_row_card(
            row=row,
            filter_values=filter_values,
            filters_active=filters_active,
        )
        for row in summary.rows
    )
    return f'<div class="content-list">{items}</div>'


def _render_reports_topic_results(
    *,
    content_items: list[ContentResponse],
    summary: CreatorReportsTopicSummary,
    filters_active: bool,
) -> str:
    if not summary.rows:
        return _render_reports_topics_empty_state(
            content_items=content_items,
            summary=summary,
            filters_active=filters_active,
        )

    items = "".join(
        _render_reports_topic_row_card(
            row=row,
            filters_active=filters_active,
        )
        for row in summary.rows
    )
    return f'<div class="content-list">{items}</div>'


def _render_reports_booking_link_results(
    *,
    content_items: list[ContentResponse],
    booking_links: list[BookingLinkResponse],
    summary: CreatorReportsBookingLinkSummary,
    filters_active: bool,
) -> str:
    if not summary.rows:
        return _render_reports_booking_links_empty_state(
            content_items=content_items,
            booking_links=booking_links,
            filters_active=filters_active,
        )

    items = "".join(
        _render_reports_booking_link_row_card(
            row=row,
            filters_active=filters_active,
        )
        for row in summary.rows
    )
    return f'<div class="content-list">{items}</div>'


def _render_reports_topics_empty_state(
    *,
    content_items: list[ContentResponse],
    summary: CreatorReportsTopicSummary,
    filters_active: bool,
) -> str:
    if filters_active and summary.has_any_authoritative_topics:
        return """
        <section class="empty-state">
          <p class="eyebrow">No topic rows in this window</p>
          <h2>No authoritative topic rows match this paid-date filter</h2>
          <p>Try widening the paid-date range or clear the filters to see all authoritative confirmed topic groupings for this creator.</p>
          <a href="/app/reports/topics" class="inline-link">Clear filters</a>
        </section>
        """

    if not content_items:
        return """
        <section class="empty-state">
          <p class="eyebrow">No tracked content yet</p>
          <h2>Track content before topic analytics can appear</h2>
          <p>Topic analytics groups the existing content funnel. Save tracked content first, then review and confirm topics from the content workflow.</p>
          <a href="/app/content" class="inline-link">Open content</a>
        </section>
        """

    return """
    <section class="empty-state">
      <p class="eyebrow">No authoritative topics yet</p>
      <h2>Review and confirm topics before this summary can fill in</h2>
      <p>This page only counts authoritative confirmed topics. If tracked content exists but nothing appears here yet, finish the topic review flow on the content pages first.</p>
      <a href="/app/content" class="inline-link">Open content</a>
    </section>
    """


def _render_reports_booking_links_empty_state(
    *,
    content_items: list[ContentResponse],
    booking_links: list[BookingLinkResponse],
    filters_active: bool,
) -> str:
    if filters_active and booking_links:
        return """
        <section class="empty-state">
          <p class="eyebrow">No booking-link rows in this window</p>
          <h2>No booking-link rows match this paid-date filter</h2>
          <p>Try widening the paid-date range or clear the filters to see all visible booking-link identities for this creator.</p>
          <a href="/app/reports/booking-links" class="inline-link">Clear filters</a>
        </section>
        """

    if not booking_links:
        return """
        <section class="empty-state">
          <p class="eyebrow">No booking links yet</p>
          <h2>Create a booking link before booking-link analytics can appear</h2>
          <p>This page groups the existing content funnel by saved booking-link identity, so you need at least one saved booking link before any rows can show up here.</p>
          <a href="/app/booking-links" class="inline-link">Open booking links</a>
        </section>
        """

    if not content_items:
        return """
        <section class="empty-state">
          <p class="eyebrow">No tracked content yet</p>
          <h2>Track content before booking-link analytics can appear</h2>
          <p>Booking-link analytics reuses the existing content funnel. Save tracked content against one of your booking links first, then this summary can fill in.</p>
          <a href="/app/content" class="inline-link">Open content</a>
        </section>
        """

    return """
    <section class="empty-state">
      <p class="eyebrow">No booking-link rows yet</p>
      <h2>Booking-link analytics will fill in once tracked content starts using saved links</h2>
      <p>This page only shows grouped rows when visible content funnel rows exist for the creator's saved booking-link identities.</p>
      <a href="/app/reports" class="inline-link">Open content funnel</a>
    </section>
    """


def _render_reports_topic_row_card(
    *,
    row: ReportsTopicSummaryRow,
    filters_active: bool,
) -> str:
    blocked_copy = (
        f"{_count_copy(row.open_blocked_billing_case_count, 'open blocked billing case')} still outside paid totals and visible separately in Attention."
        if row.open_blocked_billing_case_count > 0
        else "No blocked billing is open for this grouped topic view right now."
    )

    return f"""
    <article class="content-card stack report-row-card">
      <div class="content-card-header">
        <div>
          <p class="eyebrow">Authoritative confirmed topic</p>
          <h2>{html.escape(row.canonical_label)}</h2>
        </div>
        <p class="pill-note">{html.escape(_reports_topic_funnel_status_label(row))}</p>
      </div>
      <p class="report-scope-note">Comparison view over {html.escape(_count_copy(row.content_count, "content row"))}. One content row can still appear here under more than one authoritative confirmed topic.</p>
      <div class="report-proof-grid">
        <section class="topic-summary stack report-proof-block">
          <div>
            <p class="eyebrow">Paid outcomes</p>
            <h3>{html.escape(_format_money_from_cents(row.paid_revenue_cents))}</h3>
          </div>
          <p>{html.escape(_count_copy(row.paid_invoice_count, "paid invoice"))} and {html.escape(_count_copy(row.paid_booking_count, "paid booking"))} currently counted in canonical reporting.</p>
          <p><strong>Paid window</strong>: {html.escape(_reports_topic_paid_window_copy(row, filters_active=filters_active))}</p>
        </section>
        <section class="topic-summary stack report-proof-block">
          <div>
            <p class="eyebrow">Grouped content</p>
            <h3>{html.escape(_count_copy(row.content_count, "content row"))}</h3>
          </div>
          <p>{html.escape(_count_copy(row.booking_count, "tracked booking"))} currently visible under this authoritative confirmed topic.</p>
          <p><strong>Current grouped state</strong>: {html.escape(_reports_topic_funnel_status_label(row))}</p>
        </section>
        <section class="topic-summary stack report-proof-block">
          <div>
            <p class="eyebrow">Diagnostic only</p>
            <h3>{html.escape(_reports_topic_funnel_status_label(row))}</h3>
          </div>
          <p>{html.escape(_reports_topic_funnel_status_summary(row))}</p>
          <p><strong>Blocked before invoicing</strong>: {html.escape(blocked_copy)}</p>
        </section>
      </div>
    </article>
    """


def _render_reports_booking_link_row_card(
    *,
    row: ReportsBookingLinkSummaryRow,
    filters_active: bool,
) -> str:
    blocked_copy = (
        f"{_count_copy(row.open_blocked_billing_case_count, 'open blocked billing case')} still outside paid totals and visible separately in Attention."
        if row.open_blocked_billing_case_count > 0
        else "No blocked billing is open for this saved link right now."
    )

    destination_line = (
        f'<p><strong>Current destination</strong>: <a href="{html.escape(row.booking_link_destination_url, quote=True)}" class="inline-link">{html.escape(row.booking_link_destination_url)}</a></p>'
        if row.booking_link_destination_url is not None
        else "<p><strong>Current destination</strong>: Not recorded.</p>"
    )

    return f"""
    <article class="content-card stack report-row-card">
      <div class="content-card-header">
        <div>
          <p class="eyebrow">Saved booking link</p>
          <h2>{html.escape(row.booking_link_name)}</h2>
        </div>
        <p class="pill-note">{html.escape(_reports_booking_link_funnel_status_label(row))}</p>
      </div>
      <p class="report-scope-note">This row compares one saved booking-link identity across {html.escape(_count_copy(row.content_count, "content row"))} and {html.escape(_count_copy(row.booking_count, "tracked booking"))} currently visible in the content funnel.</p>
      <div class="report-proof-grid">
        <section class="topic-summary stack report-proof-block">
          <div>
            <p class="eyebrow">Saved-link identity</p>
            <h3>{html.escape(_booking_link_provider_label(row.booking_link_provider))}</h3>
          </div>
          <p><strong>Grouped content rows</strong>: {html.escape(_count_copy(row.content_count, "content row"))}</p>
          <p><strong>Tracked bookings</strong>: {html.escape(_count_copy(row.booking_count, "tracked booking"))}</p>
          <p><strong>Current grouped state</strong>: {html.escape(_reports_booking_link_funnel_status_label(row))}</p>
        </section>
        <section class="topic-summary stack report-proof-block">
          <div>
            <p class="eyebrow">Paid outcomes</p>
            <h3>{html.escape(_format_money_from_cents(row.paid_revenue_cents))}</h3>
          </div>
          <p>{html.escape(_count_copy(row.paid_invoice_count, "paid invoice"))} and {html.escape(_count_copy(row.paid_booking_count, "paid booking"))} currently counted in canonical reporting.</p>
          <p><strong>Paid window</strong>: {html.escape(_reports_booking_link_paid_window_copy(row, filters_active=filters_active))}</p>
        </section>
        <section class="topic-summary stack report-proof-block">
          <div>
            <p class="eyebrow">Diagnostic only</p>
            <h3>{html.escape(_reports_booking_link_funnel_status_label(row))}</h3>
          </div>
          <p>{html.escape(_reports_booking_link_funnel_status_summary(row))}</p>
          <p><strong>Blocked before invoicing</strong>: {html.escape(blocked_copy)}</p>
        </section>
        <section class="topic-summary stack report-proof-block">
          <div>
            <p class="eyebrow">Current metadata today</p>
            <h3>Present-day context</h3>
          </div>
          {destination_line}
          <p><strong>Current stored defaults</strong>: {html.escape(_reports_booking_link_billing_defaults_copy(row))}</p>
          <p>Historical bookings and paid results stay attached to this saved booking-link identity even if the current link name or defaults changed later.</p>
        </section>
      </div>
    </article>
    """


def _render_illustrative_first_value_proof() -> str:
    return """
    <div class="topic-summary accent stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Illustrative preview</p>
          <p><strong>What first value will look like</strong></p>
        </div>
        <p class="pill-note">Illustrative only</p>
      </div>
      <p>This read-only preview is illustrative only. It does not use live bookings, invoices, or paid revenue from this workspace.</p>
      <div class="stat-grid">
        <article class="stat-tile">
          <p class="eyebrow">Tracked content</p>
          <p class="stat-value">1</p>
          <p>An example post shares one tracked link.</p>
        </article>
        <article class="stat-tile">
          <p class="eyebrow">Attributed booking</p>
          <p class="stat-value">1</p>
          <p>An example booking keeps that tracking ID.</p>
        </article>
        <article class="stat-tile">
          <p class="eyebrow">Counted paid result</p>
          <p class="stat-value">195.00</p>
          <p>Reports counts it only after the matching invoice is marked paid.</p>
        </article>
      </div>
      <div class="stack">
        <p><strong>Illustrative flow</strong></p>
        <ol class="reason-list">
          <li><strong>Share tracked content.</strong> A visitor uses the tracked link attached to one saved content item.</li>
          <li><strong>Capture the booking.</strong> The booking keeps that tracking ID, so the content-to-booking chain stays intact.</li>
          <li><strong>Count real paid revenue.</strong> Reports only fills in after the real matching invoice is marked paid.</li>
        </ol>
      </div>
      <div class="content-card stack">
        <p class="eyebrow">Illustrative sample outcome</p>
        <p><strong>Source URL</strong>: Example coaching breakdown post.</p>
        <p><strong>Tracking ID</strong>: Example tracked link.</p>
        <p><strong>Paid result</strong>: 1 paid booking, USD 195.00.</p>
        <p><strong>Why it counts</strong>: The booking kept the tracking ID from content through payment, and the matching invoice was paid.</p>
      </div>
    </div>
    """


def _render_reports_empty_state(
    *,
    readiness: CreatorWorkspaceReadiness,
    filters_active: bool,
) -> str:
    if filters_active:
        return """
        <section class="empty-state">
          <p class="eyebrow">No results in this window</p>
          <h2>No paid results match this paid-date filter</h2>
          <p>Try widening the paid-date range or clear the filters to see all invoice-backed paid results for this creator.</p>
          <a href="/app/reports" class="inline-link">Clear filters</a>
        </section>
        """

    if readiness.waiting_for_first_paid_result:
        return f"""
        <section class="empty-state stack">
          <p class="eyebrow">Waiting for first paid result</p>
          <h2>Waiting for first paid result</h2>
          <p>This workspace is ready to track. Reports stays empty until real tracked content leads to a booking and the matching invoice is marked paid.</p>
          {_render_illustrative_first_value_proof()}
          <a href="/app/content" class="inline-link">Review tracked content</a>
        </section>
        """

    if readiness.billable_now:
        return """
        <section class="empty-state">
          <p class="eyebrow">Not ready to track yet</p>
          <h2>Ready to track is the next milestone</h2>
          <p>This workspace is billable now, but Reports stays empty until you create tracked content and that tracked link leads to a paid invoice.</p>
          <a href="/app/content" class="inline-link">Create tracked content</a>
        </section>
        """

    if _billing_provider_is_connected_but_blocked(readiness):
        return f"""
        <section class="empty-state">
          <p class="eyebrow">Billing provider blocked</p>
          <h2>Billable now still waits on provider readiness</h2>
          <p>{html.escape(_billing_provider_blocked_copy(provider_name=readiness.billing_provider))} Reports stays empty until the provider is verified and a tracked invoice is marked paid.</p>
          <a href="/app/account" class="inline-link">Review billing connection</a>
        </section>
        """

    if _billing_provider_is_connected_but_not_ready(readiness):
        return f"""
        <section class="empty-state">
          <p class="eyebrow">Billing provider not ready yet</p>
          <h2>Billable now still waits on provider readiness</h2>
          <p>{html.escape(_billing_provider_not_ready_copy(readiness))} Reports stays empty until the provider is ready and a tracked invoice is marked paid.</p>
          <a href="/app/account" class="inline-link">Review billing connection</a>
        </section>
        """

    if readiness.billing_connected:
        return """
        <section class="empty-state">
          <p class="eyebrow">Not billable now</p>
          <h2>Billable now comes before paid results</h2>
          <p>A billing provider is connected, but this workspace is not billable now yet. Save amount and currency on at least one booking link, then create tracked content.</p>
          <a href="/app/booking-links" class="inline-link">Add billing defaults</a>
        </section>
        """

    return """
    <section class="empty-state">
      <p class="eyebrow">Not connected yet</p>
      <h2>Connected comes before paid results</h2>
      <p>Connect billing setup first. Then make one booking link billable now and create tracked content before Reports can fill in.</p>
      <a href="/app" class="inline-link">Open setup home</a>
    </section>
    """


def _render_blocked_billing_case_list(
    blocked_cases: list[BlockedBillingCaseSummary],
) -> str:
    if not blocked_cases:
        return """
        <section class="empty-state">
          <p class="eyebrow">Clear</p>
          <h2>No blocked billing cases are waiting right now</h2>
          <p>If invoice creation is deferred for a tracked booking, it will appear here with the reason, likely cause, and a safe next step.</p>
        </section>
        """

    items = "".join(
        _render_blocked_billing_case_card(blocked_case=blocked_case)
        for blocked_case in blocked_cases
    )
    return f'<div class="content-list">{items}</div>'


def _render_blocked_billing_case_card(*, blocked_case: BlockedBillingCaseSummary) -> str:
    reason_copy = _blocked_billing_reason_copy(blocked_case.reason_code)
    invoice_copy = "Not created yet"
    if blocked_case.invoice_id is not None or blocked_case.provider_invoice_id is not None:
        invoice_copy = (
            f'{html.escape(str(blocked_case.invoice_id or ""))} / '
            f'{html.escape(blocked_case.provider_invoice_id or "missing provider id")}'
        ).strip(" /")

    provider_details = ""
    if blocked_case.provider_operation or blocked_case.provider_http_status or blocked_case.provider_error_code:
        provider_details = f"""
        <p><strong>Provider context</strong>: operation <code>{html.escape(blocked_case.provider_operation or "unknown")}</code>, HTTP {html.escape(str(blocked_case.provider_http_status or "unknown"))}, code <code>{html.escape(blocked_case.provider_error_code or "unknown")}</code>.</p>
        """

    last_retry_copy = (
        _format_timestamp_in_utc(blocked_case.last_retry_at)
        if blocked_case.last_retry_at is not None
        else "Not retried yet"
    )

    return f"""
    <article class="content-card stack">
      <div class="content-card-header">
        <div>
          <p class="eyebrow">Blocked billing</p>
          <h2>{html.escape(blocked_case.provider_booking_id)}</h2>
        </div>
        <span class="status-pill pending">Blocked</span>
      </div>
      <p><strong>Reason</strong>: {html.escape(reason_copy.label)} (<code>{html.escape(blocked_case.reason_code)}</code>)</p>
      <p>{html.escape(reason_copy.summary)}</p>
      <p><strong>Likely cause</strong>: {html.escape(reason_copy.likely_cause)}</p>
      <p><strong>What to do next</strong>: {html.escape(reason_copy.next_step)}</p>
      <p><strong>Booking</strong>: <code>{html.escape(str(blocked_case.booking_id))}</code> ({html.escape(blocked_case.booking_status)})</p>
      <p><strong>TID</strong>: <code>{html.escape(blocked_case.tid)}</code></p>
      <p><strong>Invoice</strong>: {invoice_copy}</p>
      <p><strong>Frozen billing</strong>: {html.escape(_reports_currency_amount_copy(blocked_case.frozen_currency, blocked_case.frozen_amount_cents))}</p>
      <p><strong>Billing account</strong>: <code>{html.escape(blocked_case.provider_account_id or "not_connected")}</code></p>
      <p><strong>First blocked</strong>: {_format_timestamp_in_utc(blocked_case.first_blocked_at)}</p>
      <p><strong>Last blocked</strong>: {_format_timestamp_in_utc(blocked_case.last_blocked_at)}</p>
      <p><strong>Last retry</strong>: {last_retry_copy}</p>
      {provider_details}
      <form action="/app/attention/blocked-billing/{html.escape(str(blocked_case.case_id))}/retry" method="post">
        <button type="submit">Retry invoice creation</button>
      </form>
    </article>
    """


def _render_unmatched_payment_event_list(
    unmatched_events: list[UnmatchedPaymentEventSummary],
) -> str:
    if not unmatched_events:
        return """
        <section class="empty-state">
          <p class="eyebrow">Clear</p>
          <h2>No unmatched payment events are waiting right now</h2>
          <p>If a paid provider event cannot be linked back to canonical local booking or invoice state yet, it will appear here with the reason, likely cause, and next step.</p>
        </section>
        """

    items = "".join(
        _render_unmatched_payment_event_card(payment_event=payment_event)
        for payment_event in unmatched_events
    )
    return f'<div class="content-list">{items}</div>'


def _render_unmatched_payment_event_card(*, payment_event: UnmatchedPaymentEventSummary) -> str:
    reason_copy = _unmatched_payment_reason_copy(payment_event.unattributed_reason)
    booking_copy = (
        f'<code>{html.escape(str(payment_event.booking_id))}</code> / '
        f'<code>{html.escape(payment_event.booking_uuid or "missing")}</code>'
        if payment_event.booking_id is not None or payment_event.booking_uuid is not None
        else "Not linked yet"
    )
    tid_copy = (
        f"<code>{html.escape(payment_event.tid)}</code>"
        if payment_event.tid is not None
        else "Not linked yet"
    )
    processed_copy = (
        _format_timestamp_in_utc(payment_event.processed_at)
        if payment_event.processed_at is not None
        else "Waiting on repair"
    )
    paid_at_copy = (
        _format_timestamp_in_utc(payment_event.paid_at)
        if payment_event.paid_at is not None
        else "Unknown"
    )

    return f"""
    <article class="content-card stack">
      <div class="content-card-header">
        <div>
          <p class="eyebrow">Unresolved payment</p>
          <h2>{html.escape(reason_copy.label)}</h2>
        </div>
        <span class="status-pill pending">{html.escape(_reports_payment_event_status_label(payment_event.status))}</span>
      </div>
      <p>{html.escape(reason_copy.summary)}</p>
      <p><strong>Likely cause</strong>: {html.escape(reason_copy.likely_cause)}</p>
      <p><strong>What to do next</strong>: {html.escape(reason_copy.next_step)}</p>
      <p><strong>Payment provider</strong>: <code>{html.escape(_billing_provider_label(payment_event.payment_provider))}</code></p>
      <p><strong>Payment event</strong>: <code>{html.escape(payment_event.provider_event_id)}</code></p>
      <p><strong>Provider invoice</strong>: <code>{html.escape(payment_event.provider_invoice_id)}</code></p>
      <p><strong>Billing account</strong>: <code>{html.escape(payment_event.provider_account_id or "unknown")}</code></p>
      <p><strong>Booking</strong>: {booking_copy}</p>
      <p><strong>TID</strong>: {tid_copy}</p>
      <p><strong>Reason code</strong>: <code>{html.escape(payment_event.unattributed_reason or "unknown")}</code></p>
      <p><strong>Paid at</strong>: {paid_at_copy}</p>
      <p><strong>Received at</strong>: {_format_timestamp_in_utc(payment_event.received_at)}</p>
      <p><strong>Processed at</strong>: {processed_copy}</p>
    </article>
    """


def _render_reports_row_card(
    *,
    row: ReportsSummaryRow,
    filter_values: dict[str, str],
    filters_active: bool,
) -> str:
    details_href = html.escape(
        _reports_content_drilldown_href(
            tid=row.tid,
            filter_values=filter_values,
        ),
        quote=True,
    )
    details_link = f'<a href="{details_href}" class="inline-link">Open funnel details</a>'
    explanation_link = ""
    if row.paid_invoice_count > 0:
        explanation_href = html.escape(
            _reports_paid_explanation_href(
                tid=row.tid,
                filter_values=filter_values,
            ),
            quote=True,
        )
        explanation_link = (
            f'<a href="{explanation_href}" class="inline-link">Why this revenue counted</a>'
        )

    footer_links = details_link
    if explanation_link:
        footer_links = f"{details_link} {explanation_link}"

    return f"""
    <article class="content-card stack report-row-card">
      <div class="content-card-header">
        <div>
          <p class="eyebrow">Tracked content</p>
          <h2>{html.escape(_content_card_title(row.source_url))}</h2>
        </div>
        <p class="pill-note">{html.escape(_reports_funnel_status_label(row))}</p>
      </div>
      <div class="report-row-meta">
        <p><strong>Source URL</strong>: <a href="{html.escape(row.source_url)}" class="inline-link">{html.escape(row.source_url)}</a></p>
        <p><strong>Tracking ID</strong>: <code>{html.escape(row.tid)}</code></p>
      </div>
      <div class="report-snapshot-grid">
        <section class="report-snapshot">
          <p class="eyebrow">Paid outcomes</p>
          <p class="report-snapshot-value">{html.escape(_format_money_from_cents(row.paid_revenue_cents))}</p>
          <p>{html.escape(_count_copy(row.paid_invoice_count, "paid invoice"))} and {html.escape(_count_copy(row.paid_booking_count, "paid booking"))}</p>
          <p><strong>Paid window</strong>: {html.escape(_reports_paid_window_copy(row, filters_active=filters_active))}</p>
        </section>
        <section class="report-snapshot">
          <p class="eyebrow">Tracked activity</p>
          <p class="report-snapshot-value">{html.escape(str(row.booking_count))}</p>
          <p>{html.escape(_count_copy(row.booking_count, "tracked booking"))}</p>
          <p>{html.escape(_reports_funnel_status_summary(row))}</p>
        </section>
        <section class="report-snapshot report-snapshot-diagnostic">
          <p class="eyebrow">Diagnostic only</p>
          <p class="report-snapshot-value">{html.escape(str(row.open_blocked_billing_case_count))}</p>
          <p>{html.escape(_reports_row_diagnostic_copy(row))}</p>
        </section>
      </div>
      <div class="report-row-actions">
        {footer_links}
      </div>
    </article>
    """


def _render_reports_export_link(
    *,
    filter_values: dict[str, str],
    field_errors: dict[str, str],
) -> str:
    if field_errors:
        return ""

    return (
        f'<a href="{html.escape(_reports_export_href(filter_values), quote=True)}" '
        'class="inline-link">Export CSV</a>'
    )


def _render_reports_unmatched_explanation_link(
    *,
    summary: CreatorReportsSummary,
    filter_values: dict[str, str],
) -> str:
    if summary.unattributed_current_backlog.event_count == 0:
        return ""

    return (
        f'<a href="{html.escape(_reports_unattributed_explanation_href(filter_values), quote=True)}" '
        'class="inline-link">Why some payments stay outside totals</a>'
    )


def _render_reports_paid_explanation_page(
    *,
    current_user: AuthUser,
    explanation: CreatorPaidAttributionExplanation,
    filter_values: dict[str, str],
) -> str:
    creator_name = html.escape(current_user.creator.name)
    creator_email = html.escape(current_user.email)
    row = explanation.summary_row
    back_href = html.escape(_reports_page_href(filter_values), quote=True)
    provider_event_count = sum(
        1 for evidence in explanation.evidence if evidence.provider_event_id is not None
    )

    body = f"""
    <header class="shell-header">
      <div>
        <p class="eyebrow">Creator Home</p>
        <h1>Why this revenue counted</h1>
        <p class="lede">This paid result stays in totals because one tracked content row, one creator-scoped booking, and one canonical paid invoice still align inside the selected paid window.</p>
      </div>
      <form action="/sign-out" method="post">
        <button type="submit" class="secondary">Sign out</button>
      </form>
    </header>
    {_render_shell_nav(current_path="/app/reports")}
    <section class="card stack report-focus-card">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Counting decision</p>
          <h2>Counted in paid totals for this selected window</h2>
        </div>
        <p class="pill-note">Paid</p>
      </div>
      <p class="report-toolbar-meta">Signed in as <strong class="wrap-anywhere">{creator_email}</strong> for <strong class="wrap-anywhere">{creator_name}</strong>.</p>
      <p class="report-scope-note">This creator-scoped row stays counted because the booking and canonical paid invoice still align for the selected paid window: {html.escape(_reports_paid_window_copy(row, filters_active=_reports_filters_are_active(filter_values)))}</p>
      <div class="report-row-meta">
        <p><strong>Source URL</strong>: <a href="{html.escape(row.source_url)}" class="inline-link">{html.escape(row.source_url)}</a></p>
        <p><strong>Tracking ID</strong>: <code>{html.escape(row.tid)}</code></p>
      </div>
      <div class="report-answer-strip report-answer-strip-compact">
        <article class="stat-tile">
          <p class="eyebrow">Counted revenue</p>
          <p class="stat-value">{html.escape(_format_money_from_cents(row.paid_revenue_cents))}</p>
        </article>
        <article class="stat-tile">
          <p class="eyebrow">Canonical invoices</p>
          <p class="stat-value">{html.escape(str(row.paid_invoice_count))}</p>
          <p>{html.escape(_count_copy(row.paid_invoice_count, "paid invoice"))}</p>
        </article>
        <article class="stat-tile">
          <p class="eyebrow">Counted bookings</p>
          <p class="stat-value">{html.escape(str(row.paid_booking_count))}</p>
          <p>{html.escape(_count_copy(row.paid_booking_count, "paid booking"))}</p>
        </article>
        <article class="stat-tile">
          <p class="eyebrow">Supporting provider events</p>
          <p class="stat-value">{html.escape(str(provider_event_count))}</p>
          <p>{html.escape(_count_copy(provider_event_count, "linked provider event"))}</p>
        </article>
      </div>
      <div class="report-row-actions">
        <a href="{back_href}" class="inline-link">Back to reports</a>
      </div>
    </section>
    <section class="grid">
      <article class="topic-summary stack">
        <div>
          <p class="eyebrow">Required truth</p>
          <h2>What had to line up</h2>
        </div>
        <ul class="reason-list">
          <li>The tracked content row and its stored tracking ID still match this counted result.</li>
          <li>A creator-scoped booking still carries that same tracking ID.</li>
          <li>A canonical invoice for that booking is marked paid inside the selected paid window.</li>
        </ul>
      </article>
      <article class="card accent stack">
        <div>
          <p class="eyebrow">Supporting provider evidence</p>
          <h2>Provider events support the decision</h2>
        </div>
        <p>Stored payment events can confirm provider timing and provenance, but they do not replace canonical booking and invoice truth.</p>
        <p>If the payment event is missing, the invoice can still count here when canonical invoice state is already trusted for this creator-scoped row.</p>
      </article>
    </section>
    <section class="card stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Evidence used for this decision</p>
          <h2>Attribution link, canonical paid record, and provider proof</h2>
        </div>
        <p>{html.escape(_count_copy(len(explanation.evidence), "invoice chain"))} shown</p>
      </div>
      <p class="report-section-intro">Review the creator-scoped attribution link, the canonical paid invoice, and any supporting provider event stored for each counted chain.</p>
      {_render_reports_paid_evidence(explanation)}
    </section>
    """
    return _page_layout(title="Why this revenue counted", body=body)


def _render_reports_content_drilldown_page(
    *,
    current_user: AuthUser,
    drilldown: CreatorReportsContentDrilldown,
    filter_values: dict[str, str],
) -> str:
    creator_name = html.escape(current_user.creator.name)
    creator_email = html.escape(current_user.email)
    row = drilldown.current_summary_row
    paid_window = drilldown.paid_window
    filters_active = _reports_filters_are_active(filter_values)
    back_href = html.escape(_reports_page_href(filter_values), quote=True)
    clear_filter_href = html.escape(
        _reports_content_drilldown_href(
            tid=row.tid,
            filter_values=_empty_reports_filter_values(),
        ),
        quote=True,
    )
    paid_explanation_link = ""
    if drilldown.paid_explanation is not None:
        paid_explanation_link = (
            f'<a href="{html.escape(_reports_paid_explanation_href(tid=row.tid, filter_values=filter_values), quote=True)}" '
            'class="inline-link">Why this revenue counted</a>'
        )
    clear_filter_link = (
        f'<a href="{clear_filter_href}" class="inline-link">View all paid history</a>'
        if filters_active
        else ""
    )
    diagnostic_count = len(drilldown.blocked_cases) + len(drilldown.unmatched_payment_events)

    body = f"""
    <header class="shell-header">
      <div>
        <p class="eyebrow">Creator Home</p>
        <h1>Content funnel drilldown</h1>
        <p class="lede">Inspect one tracked content item across bookings, paid outcomes, and any content-scoped diagnostic state without turning reports into an operator console.</p>
      </div>
      <form action="/sign-out" method="post">
        <button type="submit" class="secondary">Sign out</button>
      </form>
    </header>
    {_render_shell_nav(current_path="/app/reports")}
    <section class="card stack report-focus-card">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Tracked content</p>
          <h2>{html.escape(_content_card_title(row.source_url))}</h2>
        </div>
        <p class="pill-note">{html.escape(_reports_funnel_status_label(row))}</p>
      </div>
      <p class="report-toolbar-meta">Signed in as <strong class="wrap-anywhere">{creator_email}</strong> for <strong class="wrap-anywhere">{creator_name}</strong>.</p>
      <div class="report-row-meta">
        <p><strong>Source URL</strong>: <a href="{html.escape(row.source_url)}" class="inline-link">{html.escape(row.source_url)}</a></p>
        <p><strong>Tracking ID</strong>: <code>{html.escape(row.tid)}</code></p>
        <p><strong>Booking link</strong>: {html.escape(drilldown.booking_link_name)}</p>
      </div>
      <p class="report-scope-note">All attributed bookings stay visible on this page. Only counted paid outcomes follow the selected paid window: {html.escape(_reports_content_paid_window_copy(drilldown=drilldown, filters_active=filters_active))}</p>
      <div class="report-answer-strip report-answer-strip-compact">
        <article class="stat-tile">
          <p class="eyebrow">Attributed bookings</p>
          <p class="stat-value">{html.escape(str(row.booking_count))}</p>
          <p>{html.escape(_count_copy(row.booking_count, "tracked booking"))}</p>
        </article>
        <article class="stat-tile">
          <p class="eyebrow">Counted revenue in view</p>
          <p class="stat-value">{html.escape(_format_money_from_cents(paid_window.paid_revenue_cents))}</p>
          <p>{html.escape(_count_copy(paid_window.paid_invoice_count, "paid invoice"))}</p>
        </article>
        <article class="stat-tile">
          <p class="eyebrow">Counted paid bookings</p>
          <p class="stat-value">{html.escape(str(paid_window.paid_booking_count))}</p>
          <p>{html.escape(_count_copy(paid_window.paid_booking_count, "paid booking"))}</p>
        </article>
        <article class="stat-tile report-diagnostic-tile">
          <p class="eyebrow">Diagnostics excluded</p>
          <p class="stat-value">{html.escape(str(diagnostic_count))}</p>
          <p>{html.escape(_count_copy(diagnostic_count, "diagnostic item"))} currently sit outside paid totals.</p>
        </article>
      </div>
      <div class="report-row-actions">
        <a href="{back_href}" class="inline-link">Back to reports</a>
        {clear_filter_link}
        {paid_explanation_link}
      </div>
    </section>
    <section class="card stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Step 1</p>
          <h2>Bookings attributed to this content</h2>
        </div>
        <p>{html.escape(_count_copy(len(drilldown.bookings), "booking"))} shown</p>
      </div>
      <p class="report-section-intro">These bookings still carry this content's stored tracking ID in canonical booking truth.</p>
      {_render_reports_content_booking_list(drilldown.bookings)}
    </section>
    <section class="card stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Step 2</p>
          <h2>Paid outcomes counted in this window</h2>
        </div>
        <p>{html.escape(_count_copy(paid_window.paid_invoice_count, "paid invoice"))} counted</p>
      </div>
      <p class="report-section-intro">Only invoice-backed paid results count here. Provider events can support the chain, but they do not replace canonical booking and invoice truth.</p>
      {_render_reports_content_paid_outcomes(
          drilldown=drilldown,
          filter_values=filter_values,
      )}
    </section>
    <section class="card stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Step 3</p>
          <h2>Diagnostics excluded from totals</h2>
        </div>
        <p>{html.escape(_count_copy(diagnostic_count, "diagnostic item"))} shown</p>
      </div>
      <p class="report-section-intro">Everything below stays outside paid totals until the missing link or blocked billing issue is resolved.</p>
      {_render_reports_content_diagnostics(
          drilldown=drilldown,
          filter_values=filter_values,
      )}
    </section>
    """
    return _page_layout(title="Content funnel drilldown", body=body)


def _render_reports_content_booking_list(bookings: list[ReportsContentBooking]) -> str:
    if not bookings:
        return """
        <section class="empty-state">
          <p class="eyebrow">No bookings yet</p>
          <h2>No canonical booking is tied to this content yet</h2>
          <p>This content is tracked, but no attributed booking carrying this stored tracking ID has landed in canonical booking truth yet.</p>
        </section>
        """

    items = "".join(
        _render_reports_content_booking_card(booking=booking)
        for booking in bookings
    )
    return f'<div class="activity-list">{items}</div>'


def _render_reports_content_booking_card(*, booking: ReportsContentBooking) -> str:
    status = _booking_activity_status(booking.status)
    canceled_at_line = ""
    if booking.canceled_at is not None:
        canceled_at_line = (
            f"<p><strong>Canceled at</strong>: "
            f"{_format_timestamp_in_utc(booking.canceled_at)}</p>"
        )

    return f"""
    <article class="activity-card stack">
      <div class="activity-card-header">
        <div>
          <p class="eyebrow">Tracked booking</p>
          <h2>{html.escape(booking.provider_booking_id)}</h2>
        </div>
        <span class="status-pill {html.escape(status["badge_class"])}">{html.escape(status["label"])}</span>
      </div>
      <p><strong>Booked at</strong>: {_format_timestamp_in_utc(booking.booked_at)}</p>
      {canceled_at_line}
      <p><strong>Booking link</strong>: {html.escape(booking.booking_link_name)}</p>
      <p>This booking is counted here because it still points to this content's stored tracking ID in canonical booking truth.</p>
    </article>
    """


def _render_reports_content_paid_outcomes(
    *,
    drilldown: CreatorReportsContentDrilldown,
    filter_values: dict[str, str],
) -> str:
    row = drilldown.current_summary_row
    paid_window = drilldown.paid_window
    filters_active = _reports_filters_are_active(filter_values)
    clear_filter_link = (
        f'<a href="{html.escape(_reports_content_drilldown_href(tid=row.tid, filter_values=_empty_reports_filter_values()), quote=True)}" '
        'class="inline-link">View all paid history</a>'
        if filters_active
        else ""
    )

    if paid_window.paid_invoice_count == 0:
        empty_copy = (
            "This content has invoice-backed paid history, but none of it landed inside the current paid-date view."
            if filters_active and row.paid_invoice_count > 0
            else "No invoice-backed paid result is counted for this content yet."
        )
        return f"""
        <section class="empty-state">
          <p class="eyebrow">No paid outcomes in view</p>
          <h2>No paid result is counted in this paid window</h2>
          <p>{html.escape(empty_copy)}</p>
          {clear_filter_link}
        </section>
        """

    explanation_link = (
        f'<a href="{html.escape(_reports_paid_explanation_href(tid=row.tid, filter_values=filter_values), quote=True)}" '
        'class="inline-link">Why this revenue counted</a>'
    )

    return f"""
    <div class="stack">
      <article class="topic-summary report-inline-proof">
        <p class="eyebrow">Counted in this view</p>
        <p class="report-snapshot-value">{html.escape(_count_copy(paid_window.paid_invoice_count, "paid invoice"))}</p>
        <p>{html.escape(_reports_content_inline_proof_copy(drilldown=drilldown, filters_active=filters_active))}</p>
      </article>
      <div class="stat-grid">
        <article class="stat-tile">
          <p class="eyebrow">Paid revenue</p>
          <p class="stat-value">{html.escape(_format_money_from_cents(paid_window.paid_revenue_cents))}</p>
        </article>
        <article class="stat-tile">
          <p class="eyebrow">Paid invoices</p>
          <p class="stat-value">{html.escape(str(paid_window.paid_invoice_count))}</p>
          <p>{html.escape(_count_copy(paid_window.paid_invoice_count, "paid invoice"))}</p>
        </article>
        <article class="stat-tile">
          <p class="eyebrow">Paid bookings</p>
          <p class="stat-value">{html.escape(str(paid_window.paid_booking_count))}</p>
          <p>{html.escape(_count_copy(paid_window.paid_booking_count, "paid booking"))}</p>
        </article>
      </div>
      <p><strong>Paid window</strong>: {html.escape(_reports_content_paid_window_copy(drilldown=drilldown, filters_active=filters_active))}</p>
      <div class="report-row-actions">
        {explanation_link}
        {clear_filter_link}
      </div>
    </div>
    """


def _render_reports_content_diagnostics(
    *,
    drilldown: CreatorReportsContentDrilldown,
    filter_values: dict[str, str],
) -> str:
    if not drilldown.blocked_cases and not drilldown.unmatched_payment_events:
        return """
        <section class="empty-state">
          <p class="eyebrow">Clear</p>
          <h2>No content-scoped diagnostics are open right now</h2>
          <p>No blocked billing case or unmatched payment signal still carries this content's stored tracking ID today.</p>
        </section>
        """

    global_unmatched_link = html.escape(
        _reports_unattributed_explanation_href(filter_values),
        quote=True,
    )
    blocked_items = "".join(
        _render_reports_content_blocked_case_card(blocked_case=blocked_case)
        for blocked_case in drilldown.blocked_cases
    )
    unmatched_items = "".join(
        _render_reports_content_unmatched_payment_card(payment_event=payment_event)
        for payment_event in drilldown.unmatched_payment_events
    )
    blocked_section = (
        f"""
        <div class="stack">
          <p class="eyebrow">Blocked before invoicing</p>
          <h2>{html.escape(_count_copy(len(drilldown.blocked_cases), "open case"))}</h2>
          <div class="content-list">{blocked_items}</div>
        </div>
        """
        if drilldown.blocked_cases
        else "<p>No blocked billing case tied to this content is open right now.</p>"
    )
    unmatched_section = (
        f"""
        <div class="stack">
          <p class="eyebrow">Payment signals still outside totals</p>
          <h2>{html.escape(_count_copy(len(drilldown.unmatched_payment_events), "backlog event"))}</h2>
          <div class="content-list">{unmatched_items}</div>
        </div>
        """
        if drilldown.unmatched_payment_events
        else "<p>No unmatched payment signal still points back to this content's tracking ID right now.</p>"
    )

    return f"""
    <div class="stack">
      <p>Only blocked cases and unmatched payment signals that still carry this content's stored tracking ID appear here. Anything without a safe content link stays on the broader diagnostic pages instead of being guessed onto this row.</p>
      {blocked_section}
      {unmatched_section}
      <p><a href="/app/attention" class="inline-link">Open Attention for fuller blocked-case detail</a></p>
      <p><a href="{global_unmatched_link}" class="inline-link">Open the global unmatched-payment explanation</a></p>
    </div>
    """


def _render_reports_content_blocked_case_card(*, blocked_case: BlockedBillingCaseSummary) -> str:
    reason_copy = _blocked_billing_reason_copy(blocked_case.reason_code)
    return f"""
    <article class="content-card stack">
      <div class="content-card-header">
        <div>
          <p class="eyebrow">Blocked billing</p>
          <h2>{html.escape(reason_copy.label)}</h2>
        </div>
        <span class="status-pill pending">Blocked</span>
      </div>
      <p>{html.escape(reason_copy.summary)}</p>
      <p><strong>Likely cause</strong>: {html.escape(reason_copy.likely_cause)}</p>
      <p><strong>What to do next</strong>: {html.escape(reason_copy.next_step)}</p>
      <p><strong>Booking</strong>: <code>{html.escape(blocked_case.provider_booking_id)}</code></p>
      <p><strong>First blocked</strong>: {_format_timestamp_in_utc(blocked_case.first_blocked_at)}</p>
      <p><strong>Last blocked</strong>: {_format_timestamp_in_utc(blocked_case.last_blocked_at)}</p>
    </article>
    """


def _render_reports_content_unmatched_payment_card(
    *,
    payment_event: UnmatchedPaymentEventSummary,
) -> str:
    reason_copy = _unmatched_payment_reason_copy(payment_event.unattributed_reason)
    paid_at_copy = (
        _format_timestamp_in_utc(payment_event.paid_at)
        if payment_event.paid_at is not None
        else "Unknown"
    )

    return f"""
    <article class="content-card stack">
      <div class="content-card-header">
        <div>
          <p class="eyebrow">Unmatched payment</p>
          <h2>{html.escape(reason_copy.label)}</h2>
        </div>
        <span class="status-pill pending">{html.escape(_reports_payment_event_status_label(payment_event.status))}</span>
      </div>
      <p>{html.escape(reason_copy.summary)}</p>
      <p><strong>Likely cause</strong>: {html.escape(reason_copy.likely_cause)}</p>
      <p><strong>What to do next</strong>: {html.escape(reason_copy.next_step)}</p>
      <p><strong>Paid at</strong>: {paid_at_copy}</p>
      <p><strong>Received at</strong>: {_format_timestamp_in_utc(payment_event.received_at)}</p>
    </article>
    """


def _render_reports_paid_evidence(
    explanation: CreatorPaidAttributionExplanation,
) -> str:
    if not explanation.evidence:
        return """
        <section class="empty-state">
          <p class="eyebrow">Evidence pending</p>
          <h2>No linked payment event is stored for this row yet</h2>
          <p>The paid row is visible from canonical invoice state, but the matching payment-event evidence is not available in local reporting data yet.</p>
        </section>
        """

    items = "".join(
        _render_reports_paid_evidence_card(
            index=index,
            evidence=evidence,
        )
        for index, evidence in enumerate(explanation.evidence, start=1)
    )
    return f'<div class="content-list">{items}</div>'


def _render_reports_paid_evidence_card(
    *,
    index: int,
    evidence: PaidAttributionEvidence,
) -> str:
    payment_provider_label = _billing_provider_label(evidence.payment_provider)
    payment_event_status_label = (
        _reports_payment_event_status_label(evidence.payment_event_status)
        if evidence.payment_event_status is not None
        else "Invoice-settled"
    )
    provider_event_summary = ""
    if evidence.provider_event_id is not None and evidence.payment_event_received_at is not None:
        provider_paid_line = (
            f"<p><strong>Provider paid time</strong>: {_format_timestamp_in_utc(evidence.payment_event_paid_at)}</p>"
            if evidence.payment_event_paid_at is not None
            else ""
        )
        provider_event_summary = f"""
        <p><strong>Payment event</strong>: <code>{html.escape(evidence.provider_event_id)}</code></p>
        <p><strong>Status</strong>: {html.escape(payment_event_status_label)}</p>
        <p><strong>Received at</strong>: {_format_timestamp_in_utc(evidence.payment_event_received_at)}</p>
        {provider_paid_line}
        """
    else:
        provider_event_summary = """
        <p>No linked payment event is stored for this invoice yet.</p>
        <p>The canonical invoice still controls whether this row counts in paid totals.</p>
        """

    return f"""
    <article class="content-card stack">
      <div class="content-card-header">
        <div>
          <p class="eyebrow">Counted chain {index}</p>
          <h2>{html.escape(_reports_currency_amount_copy(evidence.invoice_currency, evidence.invoice_amount_cents))}</h2>
        </div>
        <p class="pill-note">{html.escape(payment_event_status_label)}</p>
      </div>
      <div class="report-proof-grid">
        <section class="topic-summary stack report-proof-block">
          <div>
            <p class="eyebrow">Attribution link</p>
            <h3>Booking matched to this tracked content</h3>
          </div>
          <p><strong>Booking</strong>: <code>{html.escape(evidence.booking_uuid)}</code></p>
          <p><strong>Booked at</strong>: {_format_timestamp_in_utc(evidence.booked_at)}</p>
        </section>
        <section class="topic-summary stack report-proof-block">
          <div>
            <p class="eyebrow">Canonical paid record</p>
            <h3>Invoice counted in paid totals</h3>
          </div>
          <p><strong>Invoice</strong>: <code>{html.escape(evidence.provider_invoice_id)}</code></p>
          <p><strong>Marked paid</strong>: {_format_timestamp_in_utc(evidence.invoice_paid_at)}</p>
          <p><strong>Payment provider</strong>: <code>{html.escape(payment_provider_label)}</code></p>
        </section>
        <section class="topic-summary stack report-proof-block">
          <div>
            <p class="eyebrow">Supporting provider evidence</p>
            <h3>Provider event and provenance</h3>
          </div>
          {provider_event_summary}
        </section>
      </div>
    </article>
    """


def _render_reports_unattributed_explanation_page(
    *,
    current_user: AuthUser,
    summary: CreatorReportsSummary,
    filter_values: dict[str, str],
) -> str:
    creator_name = html.escape(current_user.creator.name)
    creator_email = html.escape(current_user.email)
    backlog = summary.unattributed_current_backlog
    back_href = html.escape(_reports_page_href(filter_values), quote=True)

    body = f"""
    <header class="shell-header">
      <div>
        <p class="eyebrow">Creator Home</p>
        <h1>Why some payments stay outside totals</h1>
        <p class="lede">These payment signals are real, but they stay outside paid totals until the creator-scoped booking and invoice chain is clear enough to trust as revenue.</p>
      </div>
      <form action="/sign-out" method="post">
        <button type="submit" class="secondary">Sign out</button>
      </form>
    </header>
    {_render_shell_nav(current_path="/app/reports")}
    <section class="grid">
      <article class="card stack report-focus-card">
        <div class="section-heading">
          <div>
            <p class="eyebrow">Diagnostic only</p>
            <h2>These payment signals stay outside paid totals</h2>
          </div>
          <p class="pill-note">No revenue estimate</p>
        </div>
        <p class="report-toolbar-meta">Signed in as <strong class="wrap-anywhere">{creator_email}</strong> for <strong class="wrap-anywhere">{creator_name}</strong>.</p>
        <p class="report-scope-note">This page explains what is unresolved right now. It does not forecast future revenue or move anything into paid totals early.</p>
        <div class="report-answer-strip report-answer-strip-compact">
          <article class="stat-tile report-diagnostic-tile">
            <p class="eyebrow">Current unmatched backlog</p>
            <p class="stat-value">{html.escape(str(backlog.event_count))}</p>
            <p>{html.escape(_count_copy(backlog.event_count, "event"))} currently stay outside paid totals.</p>
          </article>
          <article class="stat-tile">
            <p class="eyebrow">Reasons visible</p>
            <p class="stat-value">{html.escape(str(len(backlog.reasons)))}</p>
            <p>{html.escape(_count_copy(len(backlog.reasons), "reason"))} currently explain this backlog.</p>
          </article>
        </div>
        <div class="report-row-actions">
          <a href="{back_href}" class="inline-link">Back to reports</a>
        </div>
      </article>
      <article class="card accent stack">
        <div>
          <p class="eyebrow">When this can change</p>
          <h2>Only repaired chains move into paid totals</h2>
        </div>
        <p>Some unmatched reasons are creator-fixable, like missing tracked-link setup. Others need later provider or system reconciliation before they can be trusted.</p>
        <p>Until the booking and invoice chain is clear enough to trust, these events stay outside paid totals, CSV export, and the main paid-results views.</p>
      </article>
    </section>
    <section class="card stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Current unmatched reasons</p>
          <h2>What is keeping payments outside totals</h2>
        </div>
        <p>{html.escape(_count_copy(len(backlog.reasons), "reason"))} visible</p>
      </div>
      <p class="report-section-intro">Each card explains what happened, whether the chain can still recover, and what to do next.</p>
      {_render_reports_unattributed_reason_cards(summary)}
    </section>
    """
    return _page_layout(title="Why some payments stay outside totals", body=body)


def _render_reports_unattributed_reason_cards(summary: CreatorReportsSummary) -> str:
    backlog = summary.unattributed_current_backlog
    if backlog.event_count == 0:
        return """
        <section class="empty-state">
          <p class="eyebrow">Clear</p>
          <h2>No payment signals are outside totals right now</h2>
          <p>Your current paid totals do not have a separate unmatched-payment backlog attached to them.</p>
        </section>
        """

    items = "".join(
        _render_reports_unattributed_reason_card(reason=reason.reason, event_count=reason.event_count)
        for reason in backlog.reasons
    )
    return f'<div class="content-list">{items}</div>'


def _render_reports_unattributed_reason_card(*, reason: str | None, event_count: int) -> str:
    reason_copy = _unmatched_payment_reason_copy(reason)
    return f"""
        <article class="content-card stack">
          <div class="content-card-header">
            <div>
              <p class="eyebrow">Diagnostic reason</p>
              <h2>{html.escape(reason_copy.label)}</h2>
            </div>
            <p class="pill-note">{html.escape(_count_copy(event_count, "event"))}</p>
          </div>
          <p><strong>What happened</strong>: {html.escape(reason_copy.summary)}</p>
          <p><strong>Most likely cause</strong>: {html.escape(reason_copy.likely_cause)}</p>
          <p><strong>Can this recover?</strong>: {html.escape(_reports_unmatched_recovery_outlook(reason))}</p>
          <p><strong>What to do now</strong>: {html.escape(reason_copy.next_step)}</p>
        </article>
        """


def _render_reports_unmatched_reasons(summary: CreatorReportsSummary) -> str:
    backlog = summary.unattributed_current_backlog
    if backlog.event_count == 0:
        return "<p>No unmatched payment backlog is waiting right now.</p>"

    items = "".join(
        _render_reports_unmatched_reason_item(reason=reason.reason, event_count=reason.event_count)
        for reason in backlog.reasons
    )
    return f'<ul class="reason-list">{items}</ul>'


def _render_reports_unmatched_explainer(summary: CreatorReportsSummary) -> str:
    backlog = summary.unattributed_current_backlog
    if backlog.event_count == 0:
        return ""

    return (
        "<p>These unmatched events are diagnostic only, not a second revenue total. "
        "Some point to creator-fixable tracking gaps, while others reflect provider "
        "or system ambiguity until more booking or invoice context arrives.</p>"
    )


def _render_attention_notice(*, status_value: str | None) -> str:
    if status_value == "recovered":
        return """
        <section class="notice success">
          <p><strong>Invoice recovered.</strong> The blocked case was retried successfully and no longer needs attention.</p>
        </section>
        """
    if status_value == "already-recovered":
        return """
        <section class="notice success">
          <p><strong>Already recovered.</strong> That booking already had a canonical invoice, so the blocked case was cleared without creating a duplicate.</p>
        </section>
        """
    if status_value == "still-blocked":
        return """
        <section class="notice error">
          <p><strong>Still blocked.</strong> The retry was safe, but the current billing-readiness or provider state still prevented invoice creation.</p>
        </section>
        """
    if status_value == "already-handled":
        return """
        <section class="notice">
          <p><strong>No action needed.</strong> That blocked case was already resolved before this retry attempt landed.</p>
        </section>
        """
    if status_value == "closed":
        return """
        <section class="notice">
          <p><strong>Case closed.</strong> The booking is no longer invoice-eligible, so the blocked case was closed without creating a new invoice.</p>
        </section>
        """
    return ""


def _render_content_field_error(message: str | None) -> str:
    if not message:
        return ""
    return f'<p class="field-error">{html.escape(message)}</p>'


def _render_reports_field_error(message: str | None) -> str:
    if not message:
        return ""
    return f'<p class="field-error">{html.escape(message)}</p>'


def _render_copy_field(
    *,
    input_id: str,
    label: str,
    value: str,
) -> str:
    escaped_input_id = html.escape(input_id, quote=True)
    escaped_label = html.escape(label)
    escaped_value = html.escape(value, quote=True)
    return f"""
    <div class="copy-field">
      <label for="{escaped_input_id}">{escaped_label}</label>
      <div class="copy-row">
        <input
          id="{escaped_input_id}"
          type="text"
          value="{escaped_value}"
          readonly
          onclick="this.select()"
        />
        <button type="button" class="secondary copy-button" data-copy-source="{escaped_input_id}">Copy link</button>
      </div>
    </div>
    """


def _content_card_title(source_url: str) -> str:
    parsed = urlparse(source_url)
    display_value = f"{parsed.netloc}{parsed.path}"
    if parsed.query:
        display_value = f"{display_value}?{parsed.query}"
    return display_value or source_url


def _booking_activity_title(booking: BookingActivityResponse) -> str:
    if booking.source_url is None:
        return "Unattributed booking"
    return _content_card_title(booking.source_url)


def _booking_attribution_status_label(status_value: str) -> str:
    if status_value == BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED:
        return "Unattributed"
    return "Attributed"


def _booking_attribution_reason_label(reason: str | None) -> str:
    if reason == BOOKING_UNATTRIBUTED_REASON_MISSING_TID:
        return "Missing tracking ID"
    if reason == BOOKING_UNATTRIBUTED_REASON_UNKNOWN_TID:
        return "Unknown tracking ID"
    return (reason or "Unknown reason").replace("_", " ").title()


def _booking_attribution_reason_explanation(reason: str | None) -> str:
    if reason == BOOKING_UNATTRIBUTED_REASON_MISSING_TID:
        return "The booking was captured without a creator-scoped tracking ID."
    if reason == BOOKING_UNATTRIBUTED_REASON_UNKNOWN_TID:
        return "The booking carried a tracking ID, but it did not match a current tracked content row."
    return "The booking still needs a canonical content link before it can be treated as attributed."


def _billing_defaults_copy(
    booking_link: BookingLinkResponse,
    *,
    long_form: bool = False,
) -> str:
    amount = booking_link.billing_amount_cents
    currency = booking_link.billing_currency

    if amount is not None and currency is not None:
        prefix = "Ready for invoice defaults" if not long_form else "Amount and currency set"
        return f"{prefix}: {currency} {_format_billing_amount(amount)}"

    if amount is not None:
        prefix = "Incomplete defaults" if not long_form else "Amount set"
        return f"{prefix}: {_format_billing_amount(amount)} and currency still missing"

    if currency is not None:
        prefix = "Incomplete defaults" if not long_form else "Currency set"
        return f"{prefix}: {currency} and amount still missing"

    return "No billing defaults yet"


def _format_billing_amount(amount_cents: int) -> str:
    return f"{amount_cents / 100:,.2f}"


def _format_money_from_cents(amount_cents: int) -> str:
    return f"{amount_cents / 100:,.2f}"


def _billing_setup_home_state(
    *,
    readiness: CreatorWorkspaceReadiness,
    show_provider_choice: bool,
    paypal_available_to_creator: bool,
) -> dict[str, str]:
    normalized_status = readiness.billing_connect_status
    if normalized_status == "connected":
        description = (
            "A billing provider is connected, but this workspace is not billable now yet. Save amount "
            "and currency on at least one booking link."
        )
        checklist_copy = (
            "Billing setup is connected. The next milestone is billable now, which needs amount "
            "and currency on at least one booking link."
        )
        if _billing_provider_is_connected_but_blocked(readiness):
            description = _billing_provider_blocked_copy(provider_name=readiness.billing_provider)
            checklist_copy = _billing_provider_blocked_copy(provider_name=readiness.billing_provider)
        elif _billing_provider_is_connected_but_not_ready(readiness):
            description = _billing_provider_not_ready_copy(readiness)
            checklist_copy = _billing_provider_not_ready_copy(readiness)
        if readiness.billable_now:
            description = (
                "A billing provider is connected and this workspace is billable now. Keep going until "
                "it is also ready to track."
            )
            checklist_copy = (
                "Billing setup is connected. This workspace is already billable now while you "
                "finish the rest of setup."
            )
        return {
            "label": "Blocked" if _billing_provider_is_connected_but_blocked(readiness) else "Connected",
            "heading": "Billing provider is connected",
            "description": description,
            "button_label": "",
            "button_href": "",
            "badge_class": "disconnected" if _billing_provider_is_connected_but_blocked(readiness) else "connected",
            "item_class": "todo" if _billing_provider_is_connected_but_blocked(readiness) else "done",
            "checklist_label": "Blocked" if _billing_provider_is_connected_but_blocked(readiness) else "Done",
            "checklist_copy": checklist_copy,
        }

    if normalized_status == "disconnected":
        provider_action = _billing_provider_connect_action(
            provider_name=readiness.billing_provider,
            reconnect=True,
            paypal_available_to_creator=paypal_available_to_creator,
        )
        description = (
            f"This workspace was connected to {html.escape(_billing_provider_label(readiness.billing_provider))} "
            "before, but it is disconnected now. Reconnect it before new bookings can move into invoicing."
        )
        button_label = provider_action["label"] if provider_action is not None else ""
        button_href = provider_action["href"] if provider_action is not None else ""
        if provider_action is None:
            description = (
                f"This workspace was connected to {html.escape(_billing_provider_label(readiness.billing_provider))} "
                "before, but it is disconnected now. "
                f"{_PAYPAL_UNAVAILABLE_CREATOR_COPY}"
            )
        return {
            "label": "Disconnected",
            "heading": "Billing connection is disconnected",
            "description": description,
            "button_label": button_label,
            "button_href": button_href,
            "badge_class": "disconnected",
            "item_class": "todo",
            "checklist_label": "Blocked",
            "checklist_copy": "Reconnect billing setup before new bookings can move into invoicing for this workspace.",
        }

    if show_provider_choice:
        description = (
            "A billing provider is required before this workspace can turn new bookings into invoices. "
            "Choose Stripe or PayPal to continue. No billing provider is preselected for this workspace."
            if paypal_available_to_creator
            else "A billing provider is required before this workspace can turn new bookings into invoices. "
            "Choose Stripe to continue. PayPal setup is not yet available for general creators."
        )
        button_label = ""
        button_href = ""
    else:
        provider_action = _billing_provider_connect_action(
            provider_name=readiness.billing_provider,
            reconnect=False,
            paypal_available_to_creator=paypal_available_to_creator,
        )
        if provider_action is None:
            description = _PAYPAL_UNAVAILABLE_CREATOR_COPY
            button_label = ""
            button_href = ""
        else:
            description = (
                "A billing provider is required before this workspace can turn new bookings into invoices. "
                "Start or resume the connection from this page."
            )
            button_label = provider_action["label"]
            button_href = provider_action["href"]
    return {
        "label": "Pending",
        "heading": "Billing setup is still pending",
        "description": description,
        "button_label": button_label,
        "button_href": button_href,
        "badge_class": "pending",
        "item_class": "todo",
        "checklist_label": "Needs action",
        "checklist_copy": "Finish billing setup so this workspace has a payment account ready for invoicing.",
    }


def _account_billing_management_state(
    *,
    current_billing_provider: str | None,
    readiness: CreatorWorkspaceReadiness,
    show_provider_choice: bool,
    switch_attempt: BillingProviderSwitchAttempt | None,
    switch_clean_state: BillingProviderSwitchCleanState,
    switch_target_guidance: _BillingProviderSetupGuidance,
    paypal_available_to_creator: bool,
) -> dict[str, str]:
    management_view = build_account_billing_management_view(
        current_billing_provider=current_billing_provider,
        readiness=readiness,
        show_provider_choice=show_provider_choice,
        switch_attempt=switch_attempt,
        switch_clean_state=switch_clean_state,
        switch_target_guidance_state=switch_target_guidance.state,
        switch_target_actionable_issue_codes=switch_target_guidance.actionable_issue_codes,
        paypal_available_to_creator=paypal_available_to_creator,
    )
    actions_html = ""
    if management_view.action_mode == "switch-attempt" and switch_attempt is not None:
        actions_html = _render_billing_provider_switch_attempt_actions(
            switch_attempt=switch_attempt,
            switch_clean_state=switch_clean_state,
            switch_target_guidance=switch_target_guidance,
            paypal_available_to_creator=paypal_available_to_creator,
        )
    elif management_view.action_mode == "provider-choice":
        actions_html = _render_billing_provider_choice_actions(
            paypal_available_to_creator=paypal_available_to_creator
        )
    elif management_view.action_mode == "simple" and management_view.action is not None:
        actions_html = _render_post_action_button(
            action=management_view.action,
            label=management_view.action_label_override,
        )
    return {
        "label": management_view.label,
        "body": management_view.body,
        "badge_class": management_view.badge_class,
        "actions_html": actions_html,
    }


def _render_billing_provider_switch_attempt_actions(
    *,
    switch_attempt: BillingProviderSwitchAttempt,
    switch_clean_state: BillingProviderSwitchCleanState,
    switch_target_guidance: _BillingProviderSetupGuidance,
    paypal_available_to_creator: bool,
) -> str:
    target_provider_label = _billing_provider_label(switch_attempt.target_billing_provider)
    actions: list[str] = []
    paypal_target_available = (
        switch_attempt.target_billing_provider != BILLING_PROVIDER_PAYPAL
        or paypal_available_to_creator
    )
    if (
        switch_attempt.target_billing_connect_status != "connected"
        or switch_attempt.target_billing_account_id is None
    ) and paypal_target_available:
        target_action = _billing_provider_connect_action(
            provider_name=switch_attempt.target_billing_provider,
            reconnect=False,
            paypal_available_to_creator=paypal_available_to_creator,
        )
        if target_action is not None:
            actions.append(
                _render_post_action_button(
                    action=target_action,
                    label=f"Resume {target_provider_label} setup",
                )
            )
    elif (
        switch_target_guidance.ready is True
        and switch_clean_state.is_clean
        and paypal_target_available
    ):
        actions.append(
            _render_post_action_button(
                action={"href": "/app/account/billing-switch/commit", "label": ""},
                label=f"Switch to {target_provider_label}",
            )
        )
    if paypal_target_available:
        actions.append(
            _render_post_action_button(
                action={"href": "/app/account/billing-switch/restart", "label": ""},
                label="Restart switch",
                secondary=True,
            )
        )
    actions.append(
        _render_post_action_button(
            action={"href": "/app/account/billing-switch/cancel", "label": ""},
            label="Cancel switch",
            secondary=True,
        )
    )
    return "".join(actions)


def _render_post_action_button(
    *,
    action: dict[str, str],
    label: str | None = None,
    secondary: bool = False,
) -> str:
    button_class = ' class="secondary"' if secondary else ""
    return (
        f'<form action="{html.escape(action["href"])}" method="post">'
        f'<button type="submit"{button_class}>{html.escape(label or action["label"])}</button>'
        "</form>"
    )


def _billing_provider_switch_status_value(*, reason_code: str) -> str:
    if reason_code == BILLING_PROVIDER_SWITCH_REASON_SWITCH_ATTEMPT_MISSING:
        return "billing-provider-switch-missing"
    if reason_code == BILLING_PROVIDER_SWITCH_REASON_SWITCH_TARGET_ALREADY_CONNECTED:
        return "billing-provider-switch-connected"
    if reason_code in {
        BILLING_PROVIDER_SWITCH_REASON_SWITCH_NOT_CLEAN,
        BILLING_PROVIDER_SWITCH_REASON_SWITCH_REQUIRES_CONNECTED_PROVIDER,
        BILLING_PROVIDER_SWITCH_REASON_SWITCH_TARGET_NOT_CONNECTED,
        BILLING_PROVIDER_SWITCH_REASON_SWITCH_TARGET_NOT_READY,
    }:
        return "billing-provider-switch-blocked"
    return "billing-provider-switch-failed"


def _billing_provider_switch_target_guidance(
    *,
    request: Request,
    switch_attempt: BillingProviderSwitchAttempt,
) -> _BillingProviderSetupGuidance:
    if (
        switch_attempt.target_billing_connect_status != "connected"
        or switch_attempt.target_billing_account_id is None
    ):
        return _BillingProviderSetupGuidance(
            state=_BILLING_PROVIDER_SETUP_STATE_PENDING_CONNECTION
        )
    return _billing_provider_setup_guidance(
        request=request,
        provider_name=switch_attempt.target_billing_provider,
        provider_account_id=switch_attempt.target_billing_account_id,
    )


def _booking_activity_status(raw_status: str) -> dict[str, str]:
    normalized_status = raw_status.strip().lower()
    if normalized_status == "canceled":
        return {
            "label": "Canceled",
            "badge_class": "canceled",
        }

    if normalized_status == "created":
        return {
            "label": "Created",
            "badge_class": "created",
        }

    return {
        "label": normalized_status.title() or "Unknown",
        "badge_class": "pending",
    }


def _format_timestamp_in_utc(value) -> str:
    return html.escape(
        value.astimezone(timezone.utc).strftime("%B %d, %Y at %I:%M %p UTC")
    )


def _format_connected_at(value) -> str:
    return _format_timestamp_in_utc(value)


def _creator_needs_initial_billing_provider_choice(
    *,
    creator,
    readiness: CreatorWorkspaceReadiness,
) -> bool:
    return (
        readiness.billing_connect_status == "pending"
        and creator.resolved_billing_account_id is None
        and creator.resolved_billing_connected_at is None
    )


def _billing_provider_connect_action(
    *,
    provider_name: str | None,
    reconnect: bool,
    paypal_available_to_creator: bool = True,
) -> dict[str, str] | None:
    normalized_provider = (provider_name or BILLING_PROVIDER_STRIPE).strip().lower()
    if normalized_provider == BILLING_PROVIDER_PAYPAL:
        if not paypal_available_to_creator:
            return None
        return {
            "label": "Reconnect PayPal" if reconnect else "Start PayPal setup",
            "href": "/app/paypal/connect/start",
        }
    return {
        "label": "Reconnect Stripe" if reconnect else "Start Stripe setup",
        "href": "/app/stripe/connect/start",
    }


def _render_billing_provider_choice_actions(
    *,
    paypal_available_to_creator: bool,
) -> str:
    stripe_action = _billing_provider_connect_action(
        provider_name=BILLING_PROVIDER_STRIPE,
        reconnect=False,
    )
    paypal_action = _billing_provider_connect_action(
        provider_name=BILLING_PROVIDER_PAYPAL,
        reconnect=False,
        paypal_available_to_creator=paypal_available_to_creator,
    )
    paypal_card_html = ""
    if paypal_action is not None:
        paypal_card_html = f"""
        <article class="topic-summary stack">
          <div>
            <p class="eyebrow">Provider option</p>
            <h2>PayPal</h2>
          </div>
          <p>Connect PayPal for invoice-based billing through the same creator setup flow.</p>
          <form action="{html.escape(paypal_action['href'])}" method="post">
            <button type="submit">{html.escape(paypal_action['label'])}</button>
          </form>
        </article>
        """
    return f"""
    <section class="stack">
      <div class="grid">
        <article class="topic-summary stack">
          <div>
            <p class="eyebrow">Provider option</p>
            <h2>Stripe</h2>
          </div>
          <p>Connect Stripe for the existing card-based billing path.</p>
          <form action="{html.escape(stripe_action['href'])}" method="post">
            <button type="submit">{html.escape(stripe_action['label'])}</button>
          </form>
        </article>
        {paypal_card_html}
      </div>
    </section>
    """


def _billing_provider_label(raw_provider: str | None) -> str:
    normalized_provider = (raw_provider or "").strip().lower()
    if normalized_provider == BILLING_PROVIDER_STRIPE:
        return "Stripe"
    if normalized_provider == BILLING_PROVIDER_PAYPAL:
        return "PayPal"
    if normalized_provider:
        return normalized_provider.replace("_", " ").title()
    return "Not connected"


def _billing_provider_is_connected_but_blocked(
    readiness: CreatorWorkspaceReadiness,
) -> bool:
    return (
        readiness.billing_connected
        and readiness.billing_provider_guidance_state == _BILLING_PROVIDER_SETUP_STATE_BLOCKED
    )


def _billing_provider_is_connected_but_not_ready(
    readiness: CreatorWorkspaceReadiness,
) -> bool:
    return (
        readiness.billing_connected
        and readiness.billing_provider_guidance_state == _BILLING_PROVIDER_SETUP_STATE_NOT_READY
    )


def _billing_provider_blocked_copy(*, provider_name: str | None) -> str:
    provider_label = _billing_provider_label(provider_name)
    return (
        f"{provider_label} is connected, but its invoice readiness could not be verified right now. "
        "Try again later before relying on new bookings."
    )


def _billing_provider_not_ready_copy(
    readiness: CreatorWorkspaceReadiness,
) -> str:
    provider_label = _billing_provider_label(readiness.billing_provider)
    return (
        f"{provider_label} is connected, but it still needs this setup work before it can create "
        f"invoices: "
        f"{_billing_provider_actionable_issue_copy(readiness.billing_provider, readiness.billing_provider_actionable_issue_codes)}."
    )


def _billing_provider_actionable_issue_copy(
    provider_name: str | None,
    issue_codes: tuple[str, ...],
) -> str:
    provider_label = _billing_provider_label(provider_name)
    ordered_issue_codes = tuple(dict.fromkeys(issue_codes))
    actions = [
        action
        for issue_code, action in (
            (
                BILLING_ACCOUNT_READINESS_ISSUE_COMPLETE_STRIPE_SETUP,
                "finish the remaining Stripe account setup in Stripe",
            ),
            (
                BILLING_ACCOUNT_READINESS_ISSUE_CONFIRM_PAYPAL_PRIMARY_EMAIL,
                "confirm the primary email on the connected PayPal business account",
            ),
            (
                BILLING_ACCOUNT_READINESS_ISSUE_ENABLE_PAYPAL_PAYMENTS_RECEIVABLE,
                "finish the PayPal payments-receivable setup",
            ),
            (
                BILLING_ACCOUNT_READINESS_ISSUE_GRANT_PAYPAL_THIRD_PARTY_PERMISSIONS,
                "reconnect the PayPal business account and grant this platform the required PayPal permissions",
            ),
        )
        if issue_code in ordered_issue_codes
    ]
    if not actions:
        return f"finish the remaining {provider_label} account setup"
    return _human_join(actions)


def _billing_provider_switch_blockers_copy(
    *,
    switch_clean_state: BillingProviderSwitchCleanState,
) -> str:
    blockers: list[str] = []
    if switch_clean_state.open_invoice_count > 0:
        blockers.append(
            _count_copy(switch_clean_state.open_invoice_count, "open invoice")
        )
    if switch_clean_state.blocked_billing_count > 0:
        blockers.append(
            _count_copy(
                switch_clean_state.blocked_billing_count,
                "billing issue that still needs review",
                "billing issues that still need review",
            )
        )
    return _human_join(blockers)


def _human_join(parts: list[str]) -> str:
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return f"{', '.join(parts[:-1])}, and {parts[-1]}"


def _count_copy(count: int, singular: str, plural: str | None = None) -> str:
    label = singular if count == 1 else plural or f"{singular}s"
    return f"{count} {label}"


def _account_billing_ready_summary_copy(billing_ready_count: int) -> str:
    if billing_ready_count == 1:
        return "1 saved link already has amount and currency so this workspace can be billable now."
    return (
        f"{_count_copy(billing_ready_count, 'saved link')} already have amount and currency "
        "so this workspace can be billable now."
    )


def _account_request_flow(request_type: str) -> dict[str, str] | None:
    if request_type == SUPPORT_REQUEST_TYPE_WORKSPACE_RESET:
        return {
            "action_label": "Request workspace reset",
            "confirm_title": "Request workspace reset?",
            "confirm_body": (
                "This sends a manual review request for a fresh start with the same email. "
                "If approved, your current tracked links, reports, and setup history may stop "
                "working from this workspace. No changes are applied immediately."
            ),
            "confirm_button": "Submit reset request",
            "cancel_button": "Keep workspace",
            "submit_path": "/app/account/requests/workspace-reset",
            "success_status": "workspace-reset-requested",
            "failure_status": "workspace-reset-retry",
            "duplicate_status": "workspace-reset-active",
            "throttled_status": "workspace-reset-throttled",
        }
    if request_type == SUPPORT_REQUEST_TYPE_ACCOUNT_DELETION:
        return {
            "action_label": "Request account deletion",
            "confirm_title": "Request account deletion?",
            "confirm_body": (
                "This sends a manual request to remove this local workspace where possible. "
                "It does not automatically delete payment-provider or booking-provider "
                "accounts, and historical workspace access may not be recoverable after "
                "deletion work begins."
            ),
            "confirm_button": "Submit deletion request",
            "cancel_button": "Keep account",
            "submit_path": "/app/account/requests/account-deletion",
            "success_status": "account-deletion-requested",
            "failure_status": "account-deletion-retry",
            "duplicate_status": "account-deletion-active",
            "throttled_status": "account-deletion-throttled",
        }
    return None


def _support_request_rate_limiter(request: Request):
    return getattr(request.app.state, "support_request_rate_limiter", DEFAULT_SHARED_RATE_LIMITER)


def _support_request_submit_policy(request: Request):
    return getattr(request.app.state, "support_request_submit_policy", SUPPORT_REQUEST_SUBMIT_POLICY)


def _request_settings(request: Request):
    return getattr(request.app.state, "settings", get_settings())


def _operator_experiment_draft_provider(request: Request):
    return getattr(request.app.state, "operator_experiment_draft_provider")


def _paypal_available_to_creator(
    *,
    request: Request,
    current_user: AuthUser | None,
) -> bool:
    if current_user is None:
        return False
    return _request_settings(request).paypal_available_to_creator(current_user.email)


def _allowlisted_operator_from_browser_request(
    *,
    request: Request,
    current_user: AuthUser | None,
) -> AuthUser | Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    if not browser_auth_user_is_allowlisted_operator(
        current_user,
        settings=_request_settings(request),
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="operator queue not found",
        )

    return current_user


def _account_support_request_response(
    *,
    request: Request,
    current_user: AuthUser | None,
    db: Session,
    request_type: str,
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    flow = _account_request_flow(request_type)
    if flow is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="account request type not found",
        )

    existing_request = list_active_support_requests_for_creator(
        db,
        creator_id=current_user.creator_id,
    ).get(request_type)
    if existing_request is not None:
        return _redirect(
            f"/app/account?status={quote(flow['duplicate_status'], safe='')}{ACCOUNT_DANGER_ZONE_FRAGMENT}"
        )

    rate_limit_state = _support_request_rate_limiter(request).try_acquire(
        policy=_support_request_submit_policy(request),
        bucket_key=build_support_request_rate_limit_bucket_key(
            creator_id=str(current_user.creator_id),
            request_type=request_type,
        ),
    )
    if rate_limit_state.limited:
        return _redirect(
            f"/app/account?status={quote(flow['throttled_status'], safe='')}{ACCOUNT_DANGER_ZONE_FRAGMENT}"
        )

    upsert_result = create_or_get_active_support_request(
        db,
        creator_id=current_user.creator_id,
        request_type=request_type,
        creator_name=current_user.creator.name,
        requester_email=current_user.email,
    )
    if not upsert_result.created:
        return _redirect(
            f"/app/account?status={quote(flow['duplicate_status'], safe='')}{ACCOUNT_DANGER_ZONE_FRAGMENT}"
        )

    try:
        send_support_request_email(
            provider=request.app.state.email_provider,
            request_record=upsert_result.request_record,
        )
    except SupportRequestEmailDeliveryError:
        mark_support_request_notification_failed(
            db,
            request_record=upsert_result.request_record,
        )
        return _redirect(
            f"/app/account?status={quote(flow['failure_status'], safe='')}{ACCOUNT_DANGER_ZONE_FRAGMENT}"
        )

    mark_support_request_notification_succeeded(
        db,
        request_record=upsert_result.request_record,
    )
    return _redirect(f"/app/account?status={quote(flow['success_status'], safe='')}{ACCOUNT_DANGER_ZONE_FRAGMENT}")


def _reports_filter_error_detail(field_errors: dict[str, str]) -> str:
    for key in ("start_date", "end_date", "date_range"):
        if key in field_errors:
            return field_errors[key]
    return "invalid paid-date filters"


def _reports_filters_are_active(filter_values: dict[str, str]) -> bool:
    return any(filter_values[field_name] for field_name in REPORT_FILTER_FIELDS)


def _reports_page_href(filter_values: dict[str, str]) -> str:
    return f"/app/reports{_reports_query_string(filter_values)}"


def _reports_topics_page_href(filter_values: dict[str, str]) -> str:
    return f"/app/reports/topics{_reports_query_string(filter_values)}"


def _reports_booking_links_page_href(filter_values: dict[str, str]) -> str:
    return f"/app/reports/booking-links{_reports_query_string(filter_values)}"


def _reports_export_href(filter_values: dict[str, str]) -> str:
    return f"/app/reports/export.csv{_reports_query_string(filter_values)}"


def _reports_unattributed_explanation_href(filter_values: dict[str, str]) -> str:
    return f"/app/reports/explanations/unattributed{_reports_query_string(filter_values)}"


def _reports_content_drilldown_href(*, tid: str, filter_values: dict[str, str]) -> str:
    return f"/app/reports/content/{quote(tid, safe='')}{_reports_query_string(filter_values)}"


def _reports_paid_explanation_href(*, tid: str, filter_values: dict[str, str]) -> str:
    return f"/app/reports/explanations/paid/{quote(tid, safe='')}{_reports_query_string(filter_values)}"


def _reports_query_string(filter_values: dict[str, str]) -> str:
    query_values = [
        (field_name, filter_values[field_name])
        for field_name in REPORT_FILTER_FIELDS
        if filter_values[field_name]
    ]
    if not query_values:
        return ""
    return f"?{urlencode(query_values)}"


def _reports_csv_filename(*, start_date: date | None, end_date: date | None) -> str:
    if start_date is None and end_date is None:
        return "reports-summary.csv"

    start_label = start_date.isoformat() if start_date is not None else "open"
    end_label = end_date.isoformat() if end_date is not None else "open"
    return f"reports-summary-{start_label}-to-{end_label}.csv"


def _render_reports_surface_nav(
    *,
    current_path: str,
    filter_values: dict[str, str],
) -> str:
    links = [
        (_reports_page_href(filter_values), "/app/reports", "Content"),
        (_reports_topics_page_href(filter_values), "/app/reports/topics", "Topics"),
        (
            _reports_booking_links_page_href(filter_values),
            "/app/reports/booking-links",
            "Booking links",
        ),
    ]
    rendered_links = "".join(
        (
            f'<a href="{html.escape(href, quote=True)}" '
            f'class="nav-link{" active" if current_path == base_path else ""}">{html.escape(label)}</a>'
        )
        for href, base_path, label in links
    )
    return f'<nav class="shell-nav">{rendered_links}</nav>'


def _reports_funnel_status_label(row: ReportsSummaryRow) -> str:
    if row.funnel_status == "paid_result_recorded":
        return "Paid"
    if row.funnel_status == "blocked_before_invoicing":
        return "Blocked"
    if row.funnel_status == "waiting_for_first_paid_result":
        return "Waiting"
    return "No bookings yet"


def _reports_funnel_status_summary(row: ReportsSummaryRow) -> str:
    if row.funnel_status == "paid_result_recorded":
        if row.open_blocked_billing_case_count > 0:
            return (
                "Counted revenue already exists here, but some newer activity is still blocked before invoicing."
            )
        return "At least one booking from this content already became counted revenue."
    if row.funnel_status == "blocked_before_invoicing":
        return (
            "Bookings exist, but billing is blocked before any revenue can count."
        )
    if row.funnel_status == "waiting_for_first_paid_result":
        return "Bookings exist, but no invoice-backed payment counts yet."
    return "This content is tracked, but no booking has landed yet."


def _reports_paid_window_copy(row: ReportsSummaryRow, *, filters_active: bool) -> str:
    if row.first_paid_at is None or row.last_paid_at is None:
        if filters_active:
            return "No invoice-backed paid result is counted in this paid-date view yet."
        return "No invoice-backed paid result is counted for this content yet."
    if row.first_paid_at == row.last_paid_at:
        return row.first_paid_at.astimezone(timezone.utc).strftime("%B %d, %Y")
    return (
        f"{row.first_paid_at.astimezone(timezone.utc).strftime('%B %d, %Y')} to "
        f"{row.last_paid_at.astimezone(timezone.utc).strftime('%B %d, %Y')}"
    )


def _reports_row_visibility_copy(
    *,
    visible_count: int,
    total_count: int,
    filters_active: bool,
) -> str:
    if filters_active and total_count > visible_count:
        return f"Showing {visible_count} of {total_count} tracked content rows in this paid view."
    return f"{_count_copy(visible_count, 'content row')} visible"


def _reports_row_diagnostic_copy(row: ReportsSummaryRow) -> str:
    if row.open_blocked_billing_case_count > 0:
        return (
            f"{_count_copy(row.open_blocked_billing_case_count, 'open blocked billing case')} "
            "still sits outside paid totals."
        )
    return "Nothing open for this content right now."


def _reports_topic_funnel_status_label(row: ReportsTopicSummaryRow) -> str:
    if row.funnel_status == "paid_result_recorded":
        return "Paid result recorded"
    if row.funnel_status == "blocked_before_invoicing":
        return "Blocked before invoicing"
    if row.funnel_status == "waiting_for_first_paid_result":
        return "Waiting for first paid result"
    return "No bookings yet"


def _reports_topic_funnel_status_summary(row: ReportsTopicSummaryRow) -> str:
    if row.funnel_status == "paid_result_recorded":
        if row.open_blocked_billing_case_count > 0:
            return (
                "At least one content row in this topic group already has counted paid results, "
                "but some newer booking activity is still blocked before invoicing."
            )
        return (
            "At least one content row in this authoritative topic group already has "
            "invoice-backed paid results in canonical reporting."
        )
    if row.funnel_status == "blocked_before_invoicing":
        return (
            "Grouped bookings under this authoritative topic reached billing, but at least "
            "one open blocked billing case still keeps that activity outside paid totals."
        )
    if row.funnel_status == "waiting_for_first_paid_result":
        return (
            "Grouped content rows under this authoritative topic have canonical bookings, "
            "but no invoice-backed paid result is counted yet."
        )
    return (
        "This authoritative topic is attached to tracked content, but no canonical booking "
        "has been recorded for those visible content rows yet."
    )


def _reports_topic_paid_window_copy(
    row: ReportsTopicSummaryRow,
    *,
    filters_active: bool,
) -> str:
    if row.first_paid_at is None or row.last_paid_at is None:
        if filters_active:
            return "No invoice-backed paid result is counted in this paid-date view yet."
        return "No invoice-backed paid result is counted for these topic rows yet."
    if row.first_paid_at == row.last_paid_at:
        return row.first_paid_at.astimezone(timezone.utc).strftime("%B %d, %Y")
    return (
        f"{row.first_paid_at.astimezone(timezone.utc).strftime('%B %d, %Y')} to "
        f"{row.last_paid_at.astimezone(timezone.utc).strftime('%B %d, %Y')}"
    )


def _reports_booking_link_funnel_status_label(row: ReportsBookingLinkSummaryRow) -> str:
    if row.funnel_status == "paid_result_recorded":
        return "Paid result recorded"
    if row.funnel_status == "blocked_before_invoicing":
        return "Blocked before invoicing"
    if row.funnel_status == "waiting_for_first_paid_result":
        return "Waiting for first paid result"
    return "No bookings yet"


def _reports_booking_link_funnel_status_summary(row: ReportsBookingLinkSummaryRow) -> str:
    if row.funnel_status == "paid_result_recorded":
        if row.open_blocked_billing_case_count > 0:
            return (
                "This saved booking link already has counted paid results, but some newer "
                "booking activity is still blocked before invoicing."
            )
        return (
            "This saved booking link already has invoice-backed paid results in canonical "
            "reporting."
        )
    if row.funnel_status == "blocked_before_invoicing":
        return (
            "Tracked bookings under this saved booking link reached billing, but at least one "
            "open blocked billing case still keeps that activity outside paid totals."
        )
    if row.funnel_status == "waiting_for_first_paid_result":
        return (
            "Tracked content rows attached to this saved booking link have canonical bookings, "
            "but no invoice-backed paid result is counted yet."
        )
    return (
        "This saved booking link is attached to tracked content, but no canonical booking has "
        "been recorded for those visible content rows yet."
    )


def _reports_booking_link_paid_window_copy(
    row: ReportsBookingLinkSummaryRow,
    *,
    filters_active: bool,
) -> str:
    if row.first_paid_at is None or row.last_paid_at is None:
        if filters_active:
            return "No invoice-backed paid result is counted in this paid-date view yet."
        return "No invoice-backed paid result is counted for this booking link yet."
    if row.first_paid_at == row.last_paid_at:
        return row.first_paid_at.astimezone(timezone.utc).strftime("%B %d, %Y")
    return (
        f"{row.first_paid_at.astimezone(timezone.utc).strftime('%B %d, %Y')} to "
        f"{row.last_paid_at.astimezone(timezone.utc).strftime('%B %d, %Y')}"
    )


def _reports_booking_link_billing_defaults_copy(row: ReportsBookingLinkSummaryRow) -> str:
    amount = row.booking_link_billing_amount_cents
    currency = row.booking_link_billing_currency

    if amount is not None and currency is not None:
        return f"Amount and currency set: {currency} {_format_billing_amount(amount)}"
    if amount is not None:
        return f"Amount set: {_format_billing_amount(amount)} and currency still missing"
    if currency is not None:
        return f"Currency set: {currency} and amount still missing"
    return "No billing defaults yet"


def _reports_content_paid_window_copy(
    *,
    drilldown: CreatorReportsContentDrilldown,
    filters_active: bool,
) -> str:
    paid_window = drilldown.paid_window
    if paid_window.first_paid_at is None or paid_window.last_paid_at is None:
        if filters_active:
            return "No invoice-backed paid result is counted in this paid-date view yet."
        return "No invoice-backed paid result is counted for this content yet."
    if paid_window.first_paid_at == paid_window.last_paid_at:
        return paid_window.first_paid_at.astimezone(timezone.utc).strftime("%B %d, %Y")
    return (
        f"{paid_window.first_paid_at.astimezone(timezone.utc).strftime('%B %d, %Y')} to "
        f"{paid_window.last_paid_at.astimezone(timezone.utc).strftime('%B %d, %Y')}"
    )


def _reports_content_inline_proof_copy(
    *,
    drilldown: CreatorReportsContentDrilldown,
    filters_active: bool,
) -> str:
    paid_window = drilldown.paid_window
    booking_copy = _count_copy(paid_window.paid_booking_count, "attributed booking")
    invoice_copy = _count_copy(paid_window.paid_invoice_count, "paid invoice")
    if filters_active:
        return (
            f"{booking_copy.capitalize()} from this content produced {invoice_copy} inside the "
            "selected paid window. Use the explanation page for the full booking-to-invoice-to-payment chain."
        )
    return (
        f"{booking_copy.capitalize()} from this content produced {invoice_copy} in canonical paid "
        "reporting. Use the explanation page for the full booking-to-invoice-to-payment chain."
    )


@dataclass(frozen=True)
class _DiagnosticCopy:
    label: str
    summary: str
    likely_cause: str
    next_step: str


def _reports_reason_label(reason: str | None) -> str:
    return _unmatched_payment_reason_copy(reason).label


def _reports_reason_explanation(reason: str | None) -> str:
    return _unmatched_payment_reason_copy(reason).summary


def _reports_payment_event_status_label(status_value: str | None) -> str:
    if status_value == "applied":
        return "Applied"
    if status_value == "reconciled":
        return "Reconciled"
    if status_value:
        return status_value.replace("_", " ").title()
    return "Pending evidence"


def _reports_currency_amount_copy(currency: str, amount_cents: int) -> str:
    return f"{currency} {_format_billing_amount(amount_cents)}"


def _blocked_billing_backlog_copy(blocked_billing_count: int) -> str:
    if blocked_billing_count == 0:
        return "No tracked bookings are blocked before invoicing right now."
    return (
        f"{_count_copy(blocked_billing_count, 'booking')} still blocked before invoicing "
        "and outside paid totals."
    )


def _unmatched_payment_backlog_copy(event_count: int) -> str:
    if event_count == 0:
        return "No unmatched payment events are waiting right now."
    return (
        f"{_count_copy(event_count, 'payment event')} still diagnostic only and outside paid totals "
        "while the attribution chain is incomplete."
    )


def _render_reports_unmatched_reason_item(*, reason: str | None, event_count: int) -> str:
    reason_copy = _unmatched_payment_reason_copy(reason)
    return (
        f"<li><strong>{html.escape(reason_copy.label)}</strong> "
        f"({html.escape(_count_copy(event_count, 'event'))}): "
        f"{html.escape(reason_copy.summary)} "
        f"What to do next: {html.escape(reason_copy.next_step)}</li>"
    )


def _render_reports_blocked_reasons(summary: CreatorReportsSummary) -> str:
    blocked_summary = summary.blocked_summary
    if blocked_summary.open_case_count == 0:
        return "<p>No blocked billing backlog is waiting right now.</p>"

    items = "".join(
        _render_reports_blocked_reason_item(
            reason_code=item.reason_code,
            case_count=item.case_count,
        )
        for item in blocked_summary.reasons
    )
    return f'<ul class="reason-list">{items}</ul>'


def _render_reports_blocked_reason_item(*, reason_code: str, case_count: int) -> str:
    reason_copy = _blocked_billing_reason_copy(reason_code)
    return (
        f"<li><strong>{html.escape(reason_copy.label)}</strong> "
        f"({html.escape(_count_copy(case_count, 'open case'))}): "
        f"{html.escape(reason_copy.summary)} "
        f"What to do next: {html.escape(reason_copy.next_step)}</li>"
    )


def _render_health_reason_list(
    *,
    items: list[str],
    empty_heading: str,
    empty_body: str,
) -> str:
    if not items:
        return f"""
        <section class="empty-state">
          <p class="eyebrow">Clear</p>
          <h2>{html.escape(empty_heading)}</h2>
          <p>{html.escape(empty_body)}</p>
        </section>
        """

    rows = "".join(f"<li>{html.escape(item)}</li>" for item in items)
    return f'<ul class="reason-list">{rows}</ul>'


def _render_health_ingress_section(
    *,
    provider_label: str,
    snapshot: ProviderIngressHealthSnapshot,
    empty_heading: str,
    empty_body: str,
) -> str:
    return f"""
    <section class="card stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">{html.escape(provider_label)} ingress</p>
          <h2>Webhook backlog and failure counts</h2>
        </div>
        <p>{html.escape(_count_copy(snapshot.backlog_event_count + snapshot.failed_event_count, "event"))}</p>
      </div>
      {_render_health_reason_list(
          items=[
              f"{_count_copy(item.event_count, 'event')} currently marked {_health_ingress_status_label(item.processing_status).lower()}."
              for item in snapshot.statuses
              if item.event_count > 0
          ],
          empty_heading=empty_heading,
          empty_body=empty_body,
      )}
      <p>Use structured webhook logs for event-level identifiers and replay context when these counts rise.</p>
    </section>
    """


def _should_render_health_payment_provider_section(
    *,
    snapshot: PaymentProviderHealthSnapshot,
    current_billing_provider: str | None,
) -> bool:
    normalized_current_billing_provider = (current_billing_provider or "").strip().lower()
    if snapshot.payment_provider == normalized_current_billing_provider:
        return True
    if snapshot.current_backlog_event_count > 0:
        return True
    return any(item.row_count > 0 for item in snapshot.settled_state_counts)


def _render_health_payment_provider_section(
    *,
    snapshot: PaymentProviderHealthSnapshot,
) -> str:
    provider_label = _billing_provider_label(snapshot.payment_provider)
    return f"""
    <section class="card stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">{html.escape(provider_label)} payment truth</p>
          <h2>{html.escape(provider_label)} settled rows and unmatched backlog</h2>
        </div>
        <p>{html.escape(_count_copy(snapshot.current_backlog_event_count, 'backlog event'))}</p>
      </div>
      {_render_health_reason_list(
          items=[
              f"{_count_copy(item.row_count, 'settled row')} currently marked {_health_payment_state_label(item.state).lower()}."
              for item in snapshot.settled_state_counts
              if item.row_count > 0
          ]
          + [
              f"{_count_copy(item.event_count, 'backlog event')} due to {_reports_reason_label(item.reason).lower()}. {_reports_reason_explanation(item.reason)}"
              for item in snapshot.current_backlog_reasons
              if item.event_count > 0
          ],
          empty_heading=f"No {provider_label} payment rows or backlog are waiting right now",
          empty_body=f"Current creator-scoped {provider_label} payment truth does not have matched rows or unmatched backlog waiting right now.",
      )}
    </section>
    """


def _health_ingress_status_label(processing_status: str) -> str:
    if processing_status == "received":
        return "Received"
    if processing_status == "processing":
        return "Processing"
    if processing_status == "deferred_missing_booking":
        return "Deferred waiting on booking"
    if processing_status == "failed":
        return "Failed"
    return processing_status.replace("_", " ").title()


def _health_payment_state_label(state: str) -> str:
    if state == PAYMENT_PROVENANCE_STATE_MATCHED:
        return "Matched"
    if state == PAYMENT_PROVENANCE_STATE_PENDING:
        return "Pending"
    if state == PAYMENT_PROVENANCE_STATE_UNMATCHED:
        return "Unmatched"
    if state == PAYMENT_PROVENANCE_STATE_CONFLICTING:
        return "Conflicting"
    return state.replace("_", " ").title()


def _blocked_billing_reason_label(reason_code: str) -> str:
    return _blocked_billing_reason_copy(reason_code).label


def _blocked_billing_reason_explanation(reason_code: str) -> str:
    return _blocked_billing_reason_copy(reason_code).summary


def _unmatched_payment_reason_copy(reason: str | None) -> _DiagnosticCopy:
    if reason == UNATTRIBUTED_REASON_MISSING_TID:
        return _DiagnosticCopy(
            label="Missing tracking ID",
            summary=(
                "A verified payment event arrived without a usable tracking ID, so it stays "
                "diagnostic and outside paid totals."
            ),
            likely_cause=(
                "This often means the booking came through an untracked link, the tracking "
                "parameter was stripped, or browser or provider privacy prevented the ID from arriving."
            ),
            next_step=(
                "Use the tracked link consistently going forward. This event can move into paid "
                "totals only if enough booking or invoice context is later recovered."
            ),
        )
    if reason == UNATTRIBUTED_REASON_UNKNOWN_BOOKING_UUID:
        return _DiagnosticCopy(
            label="Unknown booking",
            summary=(
                "The payment event carried invoice context, but the matching booking is still "
                "missing from the current creator-scoped chain, so it stays diagnostic and outside paid totals."
            ),
            likely_cause=(
                "This is usually provider or system ambiguity rather than proof that the payment "
                "already belongs in counted revenue."
            ),
            next_step=(
                "Treat it as unresolved until the booking link exists in local data. If it "
                "persists, investigate the booking and invoice IDs shown here rather than counting it."
            ),
        )
    if reason == UNATTRIBUTED_REASON_UNKNOWN_STRIPE_INVOICE_ID:
        return _DiagnosticCopy(
            label="Unknown invoice",
            summary=(
                "The payment event could not be matched to a canonical stored invoice yet, so it "
                "stays diagnostic and outside paid totals."
            ),
            likely_cause=(
                "This usually means provider or system ambiguity, or an invoice that did not "
                "arrive through the canonical local invoice path."
            ),
            next_step=(
                "Treat it as unresolved until the invoice link exists in local data. Later "
                "reconciliation may recover it, but this page does not count it early."
            ),
        )
    return _DiagnosticCopy(
        label=(reason or "Unknown reason").replace("_", " ").title(),
        summary=(
            "The payment event is missing canonical attribution context, so it stays diagnostic "
            "and outside paid totals for now."
        ),
        likely_cause="The available provider and local data still do not explain the missing link.",
        next_step="Leave it outside paid totals until the booking and invoice chain is clear.",
    )


def _reports_unmatched_recovery_outlook(reason: str | None) -> str:
    if reason == UNATTRIBUTED_REASON_MISSING_TID:
        return (
            "Sometimes. This can move into paid totals only if enough booking or invoice context "
            "is later recovered for the missing tracking ID."
        )
    if reason == UNATTRIBUTED_REASON_UNKNOWN_BOOKING_UUID:
        return (
            "Sometimes. It can move into paid totals only if the missing booking is later "
            "recovered in canonical local data."
        )
    if reason == UNATTRIBUTED_REASON_UNKNOWN_STRIPE_INVOICE_ID:
        return (
            "Sometimes. It can move into paid totals only if the missing invoice link is later "
            "recovered in canonical local data."
        )
    return (
        "Only if the missing canonical booking or invoice chain is later recovered. Until then, "
        "it stays diagnostic only."
    )


def _blocked_billing_reason_copy(reason_code: str) -> _DiagnosticCopy:
    if reason_code == BLOCKED_BILLING_REASON_CREATOR_NOT_BILLABLE:
        return _DiagnosticCopy(
            label="Creator not billable",
            summary=(
                "We kept this booking blocked before invoicing because the workspace was not "
                "billable when invoice creation ran."
            ),
            likely_cause=(
                "This is usually creator-fixable setup work: billing connection, billing "
                "readiness, or required account details were not ready yet."
            ),
            next_step=(
                "Finish the billing setup and then retry invoice creation. Until an "
                "invoice exists, this booking stays outside paid totals."
            ),
        )
    if reason_code == BLOCKED_BILLING_REASON_PROVIDER_ERROR:
        return _DiagnosticCopy(
            label="Provider error",
            summary=(
                "Invoice creation hit a provider error, so the booking stayed blocked instead "
                "of creating an uncertain invoice record."
            ),
            likely_cause=(
                "This is usually provider or system ambiguity, not proof that your setup is wrong."
            ),
            next_step=(
                "Retry invoice creation after the provider issue clears. If it keeps happening, "
                "use the provider context below to investigate."
            ),
        )
    return _DiagnosticCopy(
        label=reason_code.replace("_", " ").title(),
        summary=(
            "This booking is still blocked before invoicing, so it stays diagnostic and outside "
            "paid totals."
        ),
        likely_cause="The stored billing condition is incomplete or still unclear.",
        next_step="Review the stored billing condition and retry only after it is repaired.",
    )


def _health_authoritative_lag_reason_label(reason: str) -> str:
    if reason == AUTHORITATIVE_CONTENT_LAG_REASON_MISSING_AUTHORITY:
        return "Missing authoritative evidence"
    if reason == AUTHORITATIVE_CONTENT_LAG_REASON_STALE_AUTHORITY:
        return "Stale authoritative evidence"
    return reason.replace("_", " ").title()


def _health_authoritative_lag_reason_explanation(reason: str) -> str:
    if reason == AUTHORITATIVE_CONTENT_LAG_REASON_MISSING_AUTHORITY:
        return (
            "A reviewed latest artifact exists for at least one content row, "
            "but no authoritative artifact has been promoted yet."
        )
    if reason == AUTHORITATIVE_CONTENT_LAG_REASON_STALE_AUTHORITY:
        return (
            "A newer reviewed artifact exists, but the current authoritative selection "
            "still points to an older artifact."
        )
    return "The current authoritative content selection still needs review."


def _page_layout(*, title: str, body: str) -> str:
    escaped_title = html.escape(title)
    return f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{escaped_title}</title>
    <style>
      :root {{
        color-scheme: light;
        --page: #f5f1e8;
        --panel: rgba(255, 252, 245, 0.88);
        --panel-strong: #fff9ef;
        --ink: #1f1c1a;
        --muted: #655a4f;
        --accent: #a34a28;
        --accent-soft: #f3dfd4;
        --line: rgba(58, 38, 28, 0.12);
        --shadow: 0 24px 60px rgba(41, 29, 22, 0.12);
      }}

      * {{
        box-sizing: border-box;
      }}

      body {{
        margin: 0;
        min-height: 100vh;
        font-family: "Avenir Next", "Trebuchet MS", sans-serif;
        color: var(--ink);
        background:
          radial-gradient(circle at top left, rgba(163, 74, 40, 0.16), transparent 32%),
          linear-gradient(160deg, #f9f5ed 0%, #efe4d4 100%);
      }}

      main {{
        width: min(1120px, calc(100% - 40px));
        margin: 0 auto;
        padding: 48px 0 64px;
      }}

      h1, h2 {{
        font-family: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", Georgia, serif;
        margin: 0;
        line-height: 1.1;
      }}

      h1 {{
        font-size: clamp(2.6rem, 4vw, 4.2rem);
        letter-spacing: -0.04em;
      }}

      h2 {{
        font-size: 1.4rem;
        margin-bottom: 12px;
      }}

      p {{
        margin: 0;
        color: var(--muted);
        line-height: 1.6;
      }}

      a {{
        color: var(--accent);
        text-decoration-thickness: 1.5px;
        text-underline-offset: 0.16em;
      }}

      strong {{
        color: var(--ink);
      }}

      .wrap-anywhere {{
        overflow-wrap: anywhere;
        word-break: break-word;
      }}

      code {{
        font-family: "SFMono-Regular", "Consolas", monospace;
        font-size: 0.94em;
      }}

      .hero,
      .card {{
        border: 1px solid var(--line);
        border-radius: 24px;
        background: var(--panel);
        box-shadow: var(--shadow);
        backdrop-filter: blur(10px);
      }}

      .hero {{
        padding: 32px;
      }}

      .milestone-hero {{
        margin-bottom: 16px;
        background:
          linear-gradient(155deg, rgba(255, 249, 239, 0.98), rgba(243, 223, 212, 0.72));
      }}

      .card {{
        padding: 24px;
      }}

      .grid {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
        gap: 16px;
        margin-bottom: 16px;
      }}

      .grid > * {{
        min-width: 0;
      }}

      .shell-header {{
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        gap: 16px;
        margin-bottom: 16px;
      }}

      .shell-header > form {{
        flex-shrink: 0;
      }}

      .shell-header h1 {{
        font-size: clamp(2.2rem, 3vw, 3.4rem);
      }}

      .shell-nav {{
        display: flex;
        flex-wrap: wrap;
        gap: 12px;
        margin: 0 0 16px;
      }}

      .nav-link {{
        display: inline-flex;
        align-items: center;
        padding: 10px 14px;
        border-radius: 999px;
        border: 1px solid var(--line);
        background: rgba(255, 249, 239, 0.74);
        color: var(--ink);
        font-weight: 700;
        text-decoration: none;
      }}

      .nav-link.active {{
        background: var(--accent);
        border-color: var(--accent);
        color: #fff8f3;
      }}

      .nav-link:focus-visible,
      .button-link:focus-visible,
      .inline-link:focus-visible,
      button:focus-visible,
      input:focus-visible,
      select:focus-visible {{
        outline: 3px solid rgba(47, 95, 91, 0.34);
        outline-offset: 3px;
      }}

      .status-row {{
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 16px;
        margin-bottom: 12px;
      }}

      .eyebrow {{
        text-transform: uppercase;
        letter-spacing: 0.14em;
        font-size: 0.78rem;
        font-weight: 700;
        color: var(--accent);
        margin-bottom: 12px;
      }}

      .lede {{
        max-width: 40rem;
        margin: 16px 0 24px;
        font-size: 1.08rem;
      }}

      .milestone-copy {{
        display: grid;
        gap: 10px;
        max-width: 52rem;
      }}

      .milestone-question {{
        color: var(--ink);
      }}

      .milestone-grid {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
        gap: 12px;
      }}

      .account-billing-hero .topic-summary {{
        height: 100%;
      }}

      .primary-action {{
        background: var(--panel-strong);
        border-color: rgba(163, 74, 40, 0.16);
      }}

      .primary-action button {{
        width: 100%;
      }}

      .milestone-note {{
        font-size: 0.95rem;
      }}

      .notice {{
        margin: 0 0 24px;
        padding: 16px 18px;
        border-radius: 18px;
        background: var(--accent-soft);
        border: 1px solid rgba(163, 74, 40, 0.16);
      }}

      .notice.success {{
        background: #dfeee7;
        border-color: rgba(31, 94, 88, 0.18);
      }}

      .notice.error {{
        background: #f7ddd6;
        border-color: rgba(151, 47, 23, 0.18);
      }}

      .status-pill {{
        display: inline-flex;
        align-items: center;
        padding: 8px 12px;
        border-radius: 999px;
        font-size: 0.82rem;
        font-weight: 700;
        letter-spacing: 0.04em;
      }}

      .status-pill.pending {{
        background: #f3dfd4;
        color: #8c3b1e;
      }}

      .status-pill.disconnected {{
        background: #f6d7d0;
        color: #972f17;
      }}

      .status-pill.connected {{
        background: #d9ede8;
        color: #1f5e58;
      }}

      .status-pill.created {{
        background: #d9ede8;
        color: #1f5e58;
      }}

      .status-pill.canceled {{
        background: #f6d7d0;
        color: #972f17;
      }}

      .status-pill.confirmed {{
        background: #d9ede8;
        color: #1f5e58;
      }}

      .status-pill.rejected {{
        background: #f6d7d0;
        color: #972f17;
      }}

      .checklist {{
        list-style: none;
        padding: 0;
        margin: 20px 0 0;
        display: grid;
        gap: 12px;
      }}

      .checklist-item {{
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        gap: 16px;
        padding: 16px 18px;
        border-radius: 18px;
        border: 1px solid var(--line);
        background: var(--panel-strong);
      }}

      .checklist-item strong {{
        display: block;
        margin-bottom: 6px;
      }}

      .checklist-item.done {{
        background: #eef6f2;
      }}

      .checklist-item.todo {{
        background: #fff2ea;
      }}

      .checklist-item.next {{
        background: #faf4eb;
      }}

      .checklist-item.active {{
        border-color: rgba(163, 74, 40, 0.32);
        background:
          linear-gradient(145deg, rgba(255, 249, 239, 0.98), rgba(255, 238, 226, 0.92));
        box-shadow: 0 16px 36px rgba(163, 74, 40, 0.12);
      }}

      .checklist-item.active strong {{
        font-size: 1.08rem;
      }}

      .checklist-item.locked {{
        background: rgba(250, 244, 235, 0.62);
        color: var(--muted);
      }}

      .checklist-item.locked p {{
        font-size: 0.94rem;
      }}

      .checklist-item.compact {{
        padding: 14px 16px;
      }}

      .active-step-action {{
        display: grid;
        gap: 12px;
        margin-top: 14px;
        padding-top: 14px;
        border-top: 1px solid var(--line);
      }}

      .active-step-action .button-link,
      .active-step-action button {{
        width: fit-content;
      }}

      .active-step-action form,
      .active-step-action p {{
        margin: 0;
      }}

      .setup-checklist-hero .checklist {{
        margin-top: 24px;
      }}

      .setup-checklist-card .checklist {{
        margin-top: 12px;
      }}

      .list-state {{
        white-space: nowrap;
        font-size: 0.84rem;
        font-weight: 700;
        color: var(--accent);
      }}

      .stack {{
        display: grid;
        gap: 16px;
      }}

      .section-heading {{
        display: flex;
        justify-content: space-between;
        align-items: flex-end;
        gap: 16px;
      }}

      .filter-row {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
        gap: 12px;
      }}

      .filter-row > div {{
        min-width: 0;
      }}

      .filter-actions {{
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 12px;
      }}

      form {{
        display: grid;
        gap: 12px;
      }}

      label {{
        font-weight: 700;
      }}

      input,
      select {{
        width: 100%;
        padding: 14px 16px;
        border-radius: 14px;
        border: 1px solid var(--line);
        background: var(--panel-strong);
        color: var(--ink);
        font: inherit;
      }}

      input[aria-invalid="true"],
      select[aria-invalid="true"] {{
        border-color: rgba(151, 47, 23, 0.42);
        background: #fff3ef;
      }}

      input[readonly] {{
        background: #fffdf7;
      }}

      .form-help {{
        margin-top: -4px;
        font-size: 0.94rem;
      }}

      .field-error {{
        margin-top: -6px;
        color: #972f17;
        font-weight: 700;
      }}

      .stat-grid {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
        gap: 12px;
      }}

      .stat-tile {{
        padding: 18px;
        border-radius: 18px;
        border: 1px solid var(--line);
        background: var(--panel-strong);
      }}

      .stat-value {{
        color: var(--ink);
        font-family: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", Georgia, serif;
        font-size: 2rem;
        line-height: 1.05;
      }}

      button {{
        width: fit-content;
        padding: 12px 18px;
        border: 0;
        border-radius: 999px;
        background: var(--accent);
        color: #fff8f3;
        font: inherit;
        font-weight: 700;
        cursor: pointer;
      }}

      button.secondary {{
        background: #2f5f5b;
      }}

      .button-link {{
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: fit-content;
        padding: 12px 18px;
        border-radius: 999px;
        border: 1px solid transparent;
        background: var(--accent);
        color: #fff8f3;
        font-weight: 700;
        text-decoration: none;
        box-shadow: 0 10px 24px rgba(163, 74, 40, 0.16);
      }}

      .button-link.secondary {{
        background: rgba(47, 95, 91, 0.1);
        border-color: rgba(47, 95, 91, 0.18);
        box-shadow: none;
        color: #224845;
      }}

      .accent {{
        background:
          linear-gradient(145deg, rgba(243, 223, 212, 0.94), rgba(255, 251, 244, 0.96));
      }}

      .footnote {{
        margin-top: 20px;
        font-size: 0.94rem;
      }}

      .inline-link {{
        font-weight: 700;
      }}

      .public-nav {{
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 18px;
        margin-bottom: 28px;
        font-size: 0.94rem;
      }}

      .brand-link {{
        color: var(--ink);
        font-weight: 800;
        text-decoration: none;
      }}

      .public-nav nav,
      .public-nav-actions {{
        display: flex;
        align-items: center;
        gap: 18px;
      }}

      .public-nav nav a {{
        color: var(--ink);
        font-weight: 700;
        text-decoration: none;
      }}

      .public-nav .button-link {{
        padding: 9px 14px;
      }}

      .landing-hero {{
        display: grid;
        grid-template-columns: minmax(0, 1fr) minmax(320px, 0.95fr);
        align-items: center;
        gap: 40px;
        margin-bottom: 36px;
        padding: 40px 0 24px;
      }}

      .landing-hero-copy {{
        display: grid;
        gap: 18px;
      }}

      .hero-actions {{
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 14px;
      }}

      .product-mockup {{
        padding: 16px;
        border-radius: 28px;
        background:
          radial-gradient(circle at top right, rgba(47, 95, 91, 0.16), transparent 34%),
          rgba(255, 252, 245, 0.72);
        border: 1px solid var(--line);
        box-shadow: var(--shadow);
      }}

      .mockup-window {{
        display: grid;
        gap: 18px;
        padding: 18px;
        border-radius: 20px;
        background: #fffdf8;
        border: 1px solid var(--line);
      }}

      .mockup-topbar {{
        display: flex;
        gap: 7px;
      }}

      .mockup-topbar span {{
        width: 9px;
        height: 9px;
        border-radius: 999px;
        background: rgba(31, 28, 26, 0.18);
      }}

      .mockup-header h2 {{
        margin-bottom: 0;
      }}

      .mockup-stat-row {{
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 10px;
      }}

      .mockup-stat-row div {{
        padding: 12px;
        border-radius: 14px;
        background: var(--panel-strong);
        border: 1px solid var(--line);
      }}

      .mockup-stat-row strong,
      .mockup-stat-row span {{
        display: block;
      }}

      .mockup-stat-row span {{
        margin-top: 4px;
        color: var(--muted);
        font-size: 0.82rem;
      }}

      .mockup-body {{
        display: grid;
        grid-template-columns: 1.1fr 0.9fr;
        gap: 14px;
        align-items: stretch;
      }}

      .mockup-chart {{
        min-height: 180px;
        display: flex;
        align-items: end;
        gap: 9px;
        padding: 16px;
        border-radius: 16px;
        background: linear-gradient(180deg, #fffaf1, #f4e7da);
        border: 1px solid var(--line);
      }}

      .mockup-chart span {{
        flex: 1;
        min-height: 28px;
        border-radius: 999px 999px 6px 6px;
        background: #1f1c1a;
      }}

      .mockup-sources {{
        display: grid;
        gap: 10px;
      }}

      .mockup-sources p {{
        display: grid;
        gap: 3px;
        padding: 12px;
        border-radius: 14px;
        background: rgba(47, 95, 91, 0.08);
        border: 1px solid rgba(47, 95, 91, 0.14);
        font-size: 0.92rem;
      }}

      .landing-section {{
        margin-bottom: 36px;
      }}

      .section-heading-centered {{
        display: grid;
        justify-items: center;
        text-align: center;
        gap: 8px;
        max-width: 720px;
        margin: 0 auto;
      }}

      .sign-in-shell {{
        min-height: calc(100vh - 112px);
        display: grid;
        place-items: center;
      }}

      .sign-in-card {{
        width: min(100%, 560px);
      }}

      .sign-in-form button {{
        width: 100%;
        justify-content: center;
      }}

      .sign-in-guidance {{
        margin-top: 16px;
        padding: 14px 16px;
        border-radius: 18px;
        border: 1px solid rgba(47, 95, 91, 0.18);
        background: rgba(47, 95, 91, 0.08);
        color: #224845;
      }}

      .benefit-grid,
      .faq-grid {{
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 16px;
      }}

      .benefit-card {{
        display: grid;
        gap: 12px;
        padding: 18px;
        border-radius: 18px;
        background: var(--panel-strong);
        border: 1px solid var(--line);
      }}

      .benefit-card h3,
      .faq-grid h3,
      .how-steps h3 {{
        margin: 0;
        color: var(--ink);
        line-height: 1.25;
      }}

      .benefit-icon {{
        width: 34px;
        height: 34px;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        border-radius: 999px;
        background: rgba(47, 95, 91, 0.1);
        color: #224845;
        font-weight: 800;
        font-size: 0.82rem;
      }}

      .how-it-works {{
        display: grid;
        grid-template-columns: minmax(240px, 0.8fr) minmax(0, 1.2fr);
        gap: 34px;
        align-items: start;
      }}

      .how-copy {{
        display: grid;
        gap: 16px;
      }}

      .how-steps {{
        display: grid;
        gap: 16px;
        margin: 0;
        padding: 0;
        list-style: none;
      }}

      .how-steps li {{
        display: grid;
        grid-template-columns: auto 1fr;
        gap: 14px;
        align-items: start;
        padding: 18px;
        border-radius: 20px;
        border: 1px solid var(--line);
        background: rgba(255, 249, 239, 0.74);
      }}

      .how-steps li > span {{
        width: 34px;
        height: 34px;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        border-radius: 999px;
        background: var(--accent);
        color: #fff8f3;
        font-weight: 800;
      }}

      .how-steps li div {{
        display: grid;
        gap: 7px;
      }}

      .trust-panel {{
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 28px;
        margin-bottom: 36px;
        padding: 32px;
        border-radius: 24px;
        background: #1f1c1a;
        box-shadow: var(--shadow);
      }}

      .trust-panel h2,
      .trust-panel p,
      .trust-panel .eyebrow {{
        color: #fff8f3;
      }}

      .trust-panel p {{
        opacity: 0.86;
      }}

      .trust-list {{
        display: grid;
        gap: 12px;
        align-content: start;
      }}

      .trust-list p {{
        padding-left: 24px;
        position: relative;
      }}

      .trust-list p::before {{
        content: "";
        position: absolute;
        left: 0;
        top: 0.75em;
        width: 8px;
        height: 8px;
        border-radius: 999px;
        background: #d9ede8;
      }}

      .trust-cta {{
        background: #fff8f3;
        color: #1f1c1a;
        box-shadow: none;
        margin-top: 6px;
      }}

      .faq-section {{
        display: grid;
        gap: 18px;
      }}

      .faq-grid {{
        grid-template-columns: repeat(2, 1fr);
      }}

      .faq-grid article {{
        display: grid;
        gap: 8px;
      }}

      .empty-state,
      .booking-link-card,
      .content-card,
      .activity-card {{
        border-radius: 20px;
        border: 1px solid var(--line);
        background: var(--panel-strong);
      }}

      .empty-state {{
        padding: 24px;
        border-style: dashed;
      }}

      .booking-link-list,
      .content-list,
      .activity-list {{
        display: grid;
        gap: 12px;
      }}

      .reason-list {{
        margin: 0;
        padding-left: 20px;
        display: grid;
        gap: 8px;
        color: var(--muted);
      }}

      .booking-link-card,
      .content-card,
      .activity-card {{
        padding: 20px;
      }}

      .booking-link-header,
      .content-card-header,
      .activity-card-header {{
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        gap: 16px;
        margin-bottom: 12px;
      }}

      .pill-note {{
        padding: 8px 12px;
        border-radius: 999px;
        background: rgba(163, 74, 40, 0.1);
        font-size: 0.88rem;
        font-weight: 700;
      }}

      .report-toolbar-card {{
        margin-bottom: 16px;
      }}

      .report-toolbar-meta {{
        margin: 0;
        color: var(--muted);
      }}

      .report-scope-note,
      .report-section-intro {{
        margin: 0;
        color: var(--muted);
      }}

      .report-answer-strip {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
        gap: 12px;
        margin-bottom: 16px;
      }}

      .report-answer-strip-compact {{
        margin-bottom: 0;
      }}

      .report-diagnostic-tile {{
        display: grid;
        gap: 10px;
      }}

      .report-row-card {{
        gap: 14px;
      }}

      .report-row-meta {{
        display: grid;
        gap: 8px;
        color: var(--muted);
      }}

      .report-snapshot-grid {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
        gap: 12px;
      }}

      .report-snapshot {{
        padding: 16px 18px;
        border-radius: 18px;
        border: 1px solid var(--line);
        background: rgba(255, 249, 239, 0.74);
        display: grid;
        gap: 10px;
      }}

      .report-snapshot-diagnostic {{
        background: rgba(243, 223, 212, 0.28);
      }}

      .report-snapshot-value {{
        margin: 0;
        color: var(--ink);
        font-family: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", Georgia, serif;
        font-size: 1.8rem;
        line-height: 1.05;
      }}

      .report-row-actions {{
        display: flex;
        flex-wrap: wrap;
        gap: 12px;
        align-items: center;
      }}

      .report-focus-card {{
        margin-bottom: 16px;
      }}

      .report-inline-proof {{
        display: grid;
        gap: 10px;
      }}

      .report-proof-grid {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
        gap: 12px;
      }}

      .report-proof-block h3 {{
        margin: 0;
        font-size: 1.05rem;
        line-height: 1.35;
      }}

      .topic-summary {{
        padding: 18px;
        border-radius: 20px;
        border: 1px solid var(--line);
        background: rgba(255, 249, 239, 0.74);
      }}

      .topic-chip-list {{
        display: flex;
        flex-wrap: wrap;
        gap: 10px;
      }}

      .topic-chip {{
        display: inline-flex;
        align-items: center;
        padding: 9px 14px;
        border-radius: 999px;
        background: rgba(47, 95, 91, 0.12);
        border: 1px solid rgba(47, 95, 91, 0.18);
        color: var(--ink);
        font-weight: 700;
      }}

      .copy-field {{
        display: grid;
        gap: 10px;
      }}

      .copy-row {{
        display: flex;
        flex-wrap: wrap;
        gap: 12px;
        align-items: center;
      }}

      .copy-row input {{
        flex: 1 1 320px;
      }}

      .copy-button {{
        white-space: nowrap;
      }}

      .data-table {{
        width: 100%;
        border-collapse: collapse;
        font-size: 0.96rem;
      }}

      .data-table th,
      .data-table td {{
        padding: 12px 10px;
        border-bottom: 1px solid var(--line);
        text-align: left;
        vertical-align: top;
      }}

      .data-table th {{
        color: var(--ink);
        font-size: 0.84rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
      }}

      @media (max-width: 720px) {{
        main {{
          width: min(100%, calc(100% - 24px));
          padding-top: 28px;
        }}

        .hero,
        .card {{
          border-radius: 20px;
        }}

        .shell-header {{
          flex-direction: column;
        }}

        .shell-header > form,
        .shell-header > form button {{
          width: 100%;
        }}

        .shell-nav {{
          gap: 8px;
        }}

        .public-nav {{
          align-items: flex-start;
          flex-direction: column;
        }}

        .public-nav nav,
        .public-nav-actions,
        .hero-actions {{
          width: 100%;
          flex-direction: column;
          align-items: stretch;
        }}

        .public-nav .button-link,
        .hero-actions .button-link {{
          width: 100%;
        }}

        .landing-hero,
        .how-it-works,
        .trust-panel {{
          grid-template-columns: 1fr;
        }}

        .landing-hero {{
          gap: 24px;
          padding-top: 18px;
        }}

        .mockup-body,
        .mockup-stat-row,
        .benefit-grid,
        .faq-grid {{
          grid-template-columns: 1fr;
        }}

        .nav-link,
        .button-link {{
          width: 100%;
          justify-content: center;
        }}

        .status-row,
        .checklist-item,
        .section-heading,
        .booking-link-header,
        .content-card-header,
        .activity-card-header {{
          flex-direction: column;
          align-items: flex-start;
        }}

        .grid,
        .milestone-grid,
        .stat-grid,
        .filter-row,
        .report-answer-strip,
        .report-snapshot-grid,
        .report-proof-grid {{
          grid-template-columns: 1fr;
        }}

        .filter-actions,
        .report-row-actions,
        .copy-row {{
          flex-direction: column;
          align-items: stretch;
        }}

        .copy-button,
        .primary-action button,
        .primary-action .button-link {{
          width: 100%;
        }}
      }}
    </style>
  </head>
  <body>
    <main>
      {body}
    </main>
    <script>
      document.addEventListener("click", async (event) => {{
        if (!(event.target instanceof Element)) {{
          return;
        }}

        const button = event.target.closest("[data-copy-source]");
        if (!button) {{
          return;
        }}

        const input = document.getElementById(button.getAttribute("data-copy-source"));
        if (!input) {{
          return;
        }}

        input.focus();
        input.select();

        try {{
          if (navigator.clipboard && navigator.clipboard.writeText) {{
            await navigator.clipboard.writeText(input.value);
            const originalLabel = button.dataset.originalLabel || button.textContent || "Copy link";
            button.dataset.originalLabel = originalLabel;
            button.textContent = "Copied";
            window.setTimeout(() => {{
              button.textContent = originalLabel;
            }}, 1500);
          }}
        }} catch {{
        }}
      }});
    </script>
  </body>
</html>
"""
