import html
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
from app.models.billing_provider import BILLING_PROVIDER_PAYPAL, BILLING_PROVIDER_STRIPE
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
    replacement_billing_provider_name,
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
    CreatorNextContentExperimentCardDrilldown,
    CreatorNextContentExperimentsResult,
    CreatorNextContentExperimentsRunComparison,
    HelperFreshnessPolicy,
    HelperGenerationLineage,
    HelperVersionSemantics,
    NextContentExperimentCard,
    NextContentExperimentUnsupportedExplanation,
    compare_creator_next_content_experiments_runs,
    create_creator_next_content_experiments_run,
    get_creator_next_content_experiment_card_drilldown,
    get_creator_next_content_experiment_card_drilldown_by_card_id,
    get_creator_next_content_experiments_run,
    get_current_creator_next_content_experiments_unsupported_explanation,
    get_latest_creator_next_content_experiments_run,
)
from app.services.paypal_provider import build_default_paypal_provider
from app.services.rate_limit import (
    DEFAULT_SHARED_RATE_LIMITER,
    SUPPORT_REQUEST_SUBMIT_POLICY,
    build_support_request_rate_limit_bucket_key,
)
from app.services.reporting import (
    CreatorReportsContentDrilldown,
    CreatorPaidAttributionExplanation,
    CreatorReportsSummary,
    CreatorReportsTopicSummary,
    PaidAttributionEvidence,
    ReportsContentBooking,
    ReportsSummaryRow,
    ReportsTopicSummaryRow,
    build_reports_summary_csv,
    get_creator_paid_attribution_explanation,
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

_BILLING_PROVIDER_SETUP_STATE_NOT_APPLICABLE = "not_applicable"
_BILLING_PROVIDER_SETUP_STATE_PENDING_CONNECTION = "pending_connection"
_BILLING_PROVIDER_SETUP_STATE_READY = "ready"
_BILLING_PROVIDER_SETUP_STATE_NOT_READY = "not_ready"
_BILLING_PROVIDER_SETUP_STATE_BLOCKED = "blocked"
_PAYPAL_UNAVAILABLE_CREATOR_COPY = (
    "PayPal setup is not yet available for general creators. "
    "Stripe remains the supported self-serve billing path for now."
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
) -> RedirectResponse:
    if current_user is not None:
        return _redirect("/app")

    should_clear_cookie = get_browser_session_token(request) is not None
    return _redirect("/sign-in", clear_session=should_clear_cookie)


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

    return _html_response(
        _render_app_shell(
            current_user=current_user,
            workspace_state=workspace_state,
            status_value=status_value,
            paypal_available_to_creator=paypal_available_to_creator,
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
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    experiment_run = (
        get_creator_next_content_experiments_run(
            creator_id=current_user.creator_id,
            claim_snapshot_id=claim_snapshot_id,
            db=db,
        )
        if claim_snapshot_id is not None
        else get_latest_creator_next_content_experiments_run(
            creator_id=current_user.creator_id,
            db=db,
        )
    )
    if claim_snapshot_id is not None and experiment_run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="experiment snapshot not found",
        )
    unsupported_explanation = (
        get_current_creator_next_content_experiments_unsupported_explanation(
            creator_id=current_user.creator_id,
            db=db,
        )
        if experiment_run is not None and experiment_run.status == EXPERIMENT_RUN_STATUS_UNSUPPORTED
        else None
    )

    return _html_response(
        _render_experiments_page(
            current_user=current_user,
            experiment_run=experiment_run,
            status_value=status_value,
            unsupported_explanation=unsupported_explanation,
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
    <section class="hero">
      <p class="eyebrow">Self-serve setup</p>
      <h1>Sign in to your creator workspace</h1>
      <p class="lede">Use your email to get a secure sign-in link, then open it on this same device and browser to finish billing, booking-link, and tracked-link setup inside the app.</p>
      {message_block}
      <form action="/sign-in" method="post" class="card">
        <label for="email">Email</label>
        <input id="email" name="email" type="email" autocomplete="email" placeholder="creator@example.com" required />
        <button type="submit">Send magic link</button>
      </form>
      <p class="footnote">If the last link expired, the setup tab was closed, or you opened the email on another device, request another email here and continue from this browser.</p>
    </section>
    """
    return _page_layout(title="Creator sign in", body=body)


def _render_app_shell(
    *,
    current_user: AuthUser,
    workspace_state: CreatorWorkspaceState,
    status_value: str | None,
    paypal_available_to_creator: bool,
) -> str:
    readiness = workspace_state.readiness
    creator_name = html.escape(current_user.creator.name)
    creator_email = html.escape(current_user.email)
    show_provider_choice = _creator_needs_initial_billing_provider_choice(
        creator=current_user.creator,
        readiness=readiness,
    )
    billing_status = _billing_setup_home_state(
        readiness=readiness,
        show_provider_choice=show_provider_choice,
        paypal_available_to_creator=paypal_available_to_creator,
    )
    setup_progress = _build_setup_home_progress(
        workspace_state=workspace_state,
        show_provider_choice=show_provider_choice,
        paypal_available_to_creator=paypal_available_to_creator,
    )

    billing_detail_lines = []
    if current_user.creator.resolved_billing_account_id:
        billing_detail_lines.append(
            f"<p><strong>Billing provider</strong>: "
            f"{html.escape(_billing_provider_label(current_user.creator.resolved_billing_provider))}</p>"
        )
        billing_detail_lines.append(
            f"<p><strong>Billing account</strong>: "
            f"{html.escape(current_user.creator.resolved_billing_account_id)}</p>"
        )
    if current_user.creator.resolved_billing_connected_at:
        billing_detail_lines.append(
            f"<p><strong>Connected on</strong>: "
            f"{_format_connected_at(current_user.creator.resolved_billing_connected_at)}</p>"
        )
    billing_action = ""
    if show_provider_choice:
        billing_action = _render_billing_provider_choice_actions(
            paypal_available_to_creator=paypal_available_to_creator
        )
    elif billing_status["button_label"]:
        billing_action = f"""
        <form action="{html.escape(billing_status['button_href'])}" method="post">
          <button type="submit">{html.escape(billing_status["button_label"])}</button>
        </form>
        """

    body = f"""
    <header class="shell-header">
      <div>
        <p class="eyebrow">Creator Home</p>
        <h1>Setup Home</h1>
        <p class="lede">See what is done, what still needs attention, and what to finish next before new bookings can turn into attributed revenue.</p>
      </div>
      <form action="/sign-out" method="post">
        <button type="submit" class="secondary">Sign out</button>
      </form>
    </header>
    {_render_shell_nav(current_path="/app")}
    {_render_setup_home_notice(status_value=status_value)}
    <section class="grid">
      <article class="card">
        <p class="eyebrow">Account</p>
        <h2 class="wrap-anywhere">{creator_name}</h2>
        <p>Signed in as <strong class="wrap-anywhere">{creator_email}</strong></p>
        <p>This workspace holds your billing connection, booking links, tracked content, and any blocked or unresolved items that still need review.</p>
      </article>
      <article class="card accent">
        <p class="eyebrow">Billing status</p>
        <div class="status-row">
          <h2>{html.escape(billing_status['heading'])}</h2>
          <span class="status-pill {html.escape(billing_status['badge_class'])}">{html.escape(billing_status['label'])}</span>
        </div>
        <p>{html.escape(billing_status['description'])}</p>
        {"".join(billing_detail_lines)}
        {_render_readiness_summary(readiness=readiness)}
        {billing_action}
      </article>
    </section>
    {_render_setup_progress_section(setup_progress=setup_progress)}
    <section class="grid">
      <article class="card">
        <p class="eyebrow">Setup checklist</p>
        <h2>What still needs to happen</h2>
        <ul class="checklist">
          {_render_setup_checklist_items(setup_progress['steps'])}
        </ul>
      </article>
        <article class="card accent stack">
        <div>
          <p class="eyebrow">Next step</p>
          <h2>{html.escape(setup_progress['next_action']['title'])}</h2>
        </div>
        <p>{setup_progress['next_action']['copy_html']}</p>
        {_render_setup_next_action_cta(
            setup_progress['next_action'],
            paypal_available_to_creator=paypal_available_to_creator,
        )}
        <p class="footnote">{_setup_attention_copy(setup_progress['attention_count'])}</p>
      </article>
    </section>
    """
    return _page_layout(title="Creator Home", body=body)


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

    billing_detail_lines = []
    if current_user.creator.resolved_billing_account_id:
        billing_detail_lines.append(
            f"<p><strong>Billing provider</strong>: "
            f"{html.escape(_billing_provider_label(current_user.creator.resolved_billing_provider))}</p>"
        )
        billing_detail_lines.append(
            f"<p><strong>Billing account</strong>: "
            f"{html.escape(current_user.creator.resolved_billing_account_id)}</p>"
        )
    if current_user.creator.resolved_billing_connected_at:
        billing_detail_lines.append(
            f"<p><strong>Connected on</strong>: "
            f"{_format_connected_at(current_user.creator.resolved_billing_connected_at)}</p>"
        )
    if switch_attempt is not None:
        billing_detail_lines.append(
            f"<p><strong>Pending switch target</strong>: "
            f"{html.escape(_billing_provider_label(switch_attempt.target_billing_provider))}</p>"
        )
        if switch_attempt.target_billing_account_id:
            billing_detail_lines.append(
                f"<p><strong>Pending target account</strong>: "
                f"{html.escape(switch_attempt.target_billing_account_id)}</p>"
            )
        if switch_attempt.target_billing_connected_at:
            billing_detail_lines.append(
                f"<p><strong>Pending target connected on</strong>: "
                f"{_format_connected_at(switch_attempt.target_billing_connected_at)}</p>"
            )
    billing_action = billing_state["actions_html"]

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
        <h1>Account settings</h1>
        <p class="lede">Manage your sign-in session, payment connection, active booking setup, and the beta policies for starting over or closing this workspace.</p>
      </div>
    </header>
    {_render_shell_nav(current_path="/app/account")}
    {_render_account_request_notice(status_value=status_value)}
    <section class="grid">
      <article class="card stack">
        <div>
          <p class="eyebrow">Current workspace</p>
          <h2 class="wrap-anywhere">{creator_name}</h2>
        </div>
        <p>Signed in as <strong class="wrap-anywhere">{creator_email}</strong>. This workspace currently holds your billing connection, booking links, tracked content, reports, and any blocked or unresolved items still waiting on review.</p>
      </article>
      <article class="card stack">
        <div>
          <p class="eyebrow">Session</p>
          <h2>Session</h2>
        </div>
        <p>Signing out ends this browser session only. Your workspace data stays here when you sign back in.</p>
        <form action="/sign-out" method="post">
          <button type="submit" class="secondary">Sign out</button>
        </form>
      </article>
    </section>
    <section class="grid">
      <article class="card stack">
        <div class="status-row">
          <div>
            <p class="eyebrow">Billing connection</p>
            <h2>Billing connection</h2>
          </div>
          <span class="status-pill {html.escape(billing_state['badge_class'])}">{html.escape(billing_state['label'])}</span>
        </div>
        <p>{html.escape(billing_state['body'])}</p>
        {"".join(billing_detail_lines)}
        {_render_readiness_summary(readiness=readiness)}
        <p><strong>What this changes</strong></p>
        <p>Changing the billing connection affects future billing readiness. It does not erase local history already recorded for this workspace, and it does not delete anything from the payment provider automatically.</p>
        {billing_action}
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
        <p><a href="/app/booking-links" class="inline-link">Manage booking links</a></p>
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


def _render_setup_checklist_items(steps: list[dict[str, object]]) -> str:
    items = []
    for step in steps:
        items.append(
            f"""
            <li class="checklist-item {html.escape(str(step['item_class']))}">
              <div>
                <strong>{html.escape(str(step['title']))}</strong>
                <p>{step['copy_html']}</p>
              </div>
              <span class="status-pill {html.escape(str(step['badge_class']))}">{html.escape(str(step['label']))}</span>
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
        f'<p><a href="{html.escape(next_action["action_href"])}" class="inline-link">'
        f"{html.escape(next_action['action_label'])}</a></p>"
    )


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
                "Choose Stripe or PayPal to start billing setup. No billing provider is preselected "
                "for this workspace."
                if paypal_available_to_creator
                else "Choose Stripe to start billing setup. PayPal setup is not yet available for general creators."
            )
            next_action = {
                "title": "Choose billing provider",
                "copy_html": (
                    "Choose Stripe or PayPal to start billing setup. This release still keeps one "
                    "active billing provider per creator."
                    if paypal_available_to_creator
                    else "Choose Stripe to start billing setup. PayPal setup is not yet available for general creators."
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
) -> dict[str, object]:
    return {
        "title": title,
        "copy_html": copy_html,
        "label": label,
        "badge_class": badge_class,
        "item_class": item_class,
        "is_complete": is_complete,
    }


def _render_shell_nav(*, current_path: str) -> str:
    links = [
        ("/app", "Setup Home"),
        ("/app/booking-links", "Booking Links"),
        ("/app/content", "Content"),
        ("/app/bookings", "Bookings"),
        ("/app/reports", "Reports"),
        ("/app/health", "Health"),
        ("/app/experiments", "Experiments"),
        ("/app/attention", "Attention"),
        ("/app/account", "Account"),
    ]
    items = []
    for href, label in links:
        class_name = "nav-link active" if href == current_path else "nav-link"
        items.append(
            f'<a href="{href}" class="{class_name}">{html.escape(label)}</a>'
        )
    return f'<nav class="shell-nav">{"".join(items)}</nav>'


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
    unsupported_explanation: NextContentExperimentUnsupportedExplanation | None,
) -> str:
    creator_name = html.escape(current_user.creator.name)
    creator_email = html.escape(current_user.email)
    result_heading = (
        "Latest experiment snapshot"
        if experiment_run is not None
        else "No experiment snapshot yet"
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
      <article class="card stack">
        <div>
          <p class="eyebrow">Generate snapshot</p>
          <h2>{result_heading}</h2>
          <p>Signed in as <strong class="wrap-anywhere">{creator_email}</strong> for <strong class="wrap-anywhere">{creator_name}</strong>.</p>
        </div>
        <p>This helper returns only `ready` or `unsupported`. It does not fill gaps with generic advice, and it uses stored authoritative content plus settled paid evidence rather than raw diagnostics.</p>
        <form action="/app/experiments" method="post">
          <button type="submit">Generate next experiments</button>
        </form>
      </article>
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
          <p class="eyebrow">Helper output</p>
          <h2>{result_heading}</h2>
        </div>
        <p>{html.escape(_experiments_snapshot_meta(experiment_run))}</p>
      </div>
      {_render_experiment_results(experiment_run=experiment_run, unsupported_explanation=unsupported_explanation)}
    </section>
    """
    return _page_layout(title="Experiments", body=body)


def _render_experiments_notice(*, status_value: str | None) -> str:
    if status_value != "generated":
        return ""

    return """
    <section class="notice success">
      <p class="eyebrow">Fresh snapshot ready</p>
      <p>Generated a new experiment snapshot from the current authoritative content and settled paid evidence.</p>
    </section>
    """


def _render_experiment_results(
    *,
    experiment_run: CreatorNextContentExperimentsResult | None,
    unsupported_explanation: NextContentExperimentUnsupportedExplanation | None,
) -> str:
    if experiment_run is None:
        return """
        <section class="empty-state">
          <p class="eyebrow">No snapshot yet</p>
          <h2>Generate your first experiment snapshot</h2>
          <p>This page stays read-only until you explicitly generate a snapshot. Refreshing the page does not create a new helper run.</p>
        </section>
        """

    if experiment_run.status == EXPERIMENT_RUN_STATUS_UNSUPPORTED:
        return _render_experiment_unsupported_state(
            experiment_run=experiment_run,
            unsupported_explanation=unsupported_explanation,
        )

    items = "".join(
        _render_experiment_card(
            index=index,
            experiment=experiment,
            run_claim_snapshot_id=experiment_run.claim_snapshot_id,
        )
        for index, experiment in enumerate(experiment_run.experiments, start=1)
    )
    return f"""
    <section class="stack">
      <div class="status-row">
        <div>
          <p class="eyebrow">Current status</p>
          <h2>{html.escape(experiment_run.summary)}</h2>
        </div>
        <span class="status-pill confirmed">Ready</span>
      </div>
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
    unsupported_explanation: NextContentExperimentUnsupportedExplanation | None,
) -> str:
    reasons_html = ""
    if unsupported_explanation is not None and unsupported_explanation.reasons:
        reason_items = "".join(
            f"<li>{html.escape(reason)}</li>"
            for reason in unsupported_explanation.reasons
        )
        reasons_html = f"""
        <div class="stack">
          <p class="eyebrow">Still blocked today</p>
          <h3>Why this helper is still unsupported</h3>
          <ul class="reason-list">{reason_items}</ul>
        </div>
        """

    current_activity_note = ""
    if unsupported_explanation is not None and unsupported_explanation.has_excluded_current_activity:
        current_activity_note = """
        <p>Some newer activity is still excluded here until it resolves into attributed booking state or settled paid evidence.</p>
        """

    return f"""
    <section class="empty-state">
      <p class="eyebrow">Unsupported</p>
      <h2>Not enough trusted evidence yet</h2>
      <p>{html.escape(experiment_run.summary)}</p>
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
    <section class="grid">
      <article class="card stack">
        <div>
          <p class="eyebrow">Paid-date filter</p>
          <h2>Invoice-backed paid outcomes</h2>
          <p>Signed in as <strong class="wrap-anywhere">{creator_email}</strong> for <strong class="wrap-anywhere">{creator_name}</strong>.</p>
        </div>
        <p>Use the paid date below to narrow paid outcome columns by when the invoice payment actually landed. Booking counts and funnel state below stay creator-scoped current totals rather than booking-date filters.</p>
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
        <div class="stat-grid">
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
        </div>
      </article>
      <article class="card accent stack">
        <div>
          <p class="eyebrow">What is not counted yet</p>
          <h2>Keep diagnostic backlog separate from paid totals</h2>
        </div>
        <p>Paid totals on this page come only from invoices that are marked paid and matched back to your tracked content through the stored booking chain.</p>
        <p><strong>Current unmatched backlog</strong>: {html.escape(_unmatched_payment_backlog_copy(summary.unattributed_current_backlog.event_count))}</p>
        {_render_reports_unmatched_explainer(summary)}
        {_render_reports_unmatched_reasons(summary)}
        {_render_reports_unmatched_explanation_link(
            summary=summary,
            filter_values=filter_values,
        )}
        <p><strong>Blocked billing backlog</strong>: {html.escape(_blocked_billing_backlog_copy(summary.blocked_summary.open_case_count))}</p>
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
        <p>{html.escape(_count_copy(len(summary.rows), "content row"))} visible</p>
      </div>
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
        <p class="lede">Group the existing content funnel by authoritative confirmed topics only, without inventing a second revenue truth or speculative taxonomy layer.</p>
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
          <h2>Topic rows in the paid window</h2>
          <p>Signed in as <strong class="wrap-anywhere">{creator_email}</strong> for <strong class="wrap-anywhere">{creator_name}</strong>.</p>
        </div>
        <p>Use the paid date below to narrow topic rows by when the counted invoice payment landed. Booking counts below still reflect the current content rows visible in this topic grouping, not booking-date slices.</p>
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
          <p class="eyebrow">Reading rules</p>
          <h2>Confirmed-topic groupings are not a second total</h2>
        </div>
        <p>Only authoritative confirmed topics count here. Pending, rejected, or non-authoritative topic candidates stay out of this summary.</p>
        <p>A single content row can appear under more than one confirmed topic, so these grouped topic rows are useful comparisons rather than a partition of your overall revenue totals.</p>
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
      {_render_reports_topic_results(
          content_items=content_items,
          summary=summary,
          filters_active=filters_active,
      )}
    </section>
    """
    return _page_layout(title="Topic analytics", body=body)


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

    body = f"""
    <header class="shell-header">
      <div>
        <p class="eyebrow">Creator Home</p>
        <h1>Attention</h1>
        <p class="lede">Review diagnostic items that are still outside paid totals: bookings blocked before invoicing and verified payments whose attribution chain is still incomplete.</p>
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
          <h2>Bookings blocked before invoicing</h2>
          <p>Signed in as <strong class="wrap-anywhere">{creator_email}</strong> for <strong class="wrap-anywhere">{creator_name}</strong>.</p>
        </div>
        <p>{html.escape(_blocked_billing_backlog_copy(blocked_count))}</p>
        <p>Some blocked cases are creator-fixable setup gaps. Others reflect provider ambiguity and stay diagnostic until retry succeeds.</p>
      </article>
      <article class="card accent stack">
        <div>
          <p class="eyebrow">Unresolved payments</p>
          <h2>Verified payments still unmatched</h2>
        </div>
        <p>{html.escape(_unmatched_payment_backlog_copy(unmatched_count))}</p>
        <p>Some unmatched events point to missing tracking. Others reflect provider or system ambiguity and may not be something you can fix directly.</p>
      </article>
    </section>
    <section class="card stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Blocked billing</p>
          <h2>Current blocked invoice cases</h2>
        </div>
        <p>{html.escape(_count_copy(blocked_count, "open case"))}</p>
      </div>
      {_render_blocked_billing_case_list(blocked_cases)}
    </section>
    <section class="card stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Unresolved payments</p>
          <h2>Current unmatched payment events</h2>
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


def _render_reports_topic_row_card(
    *,
    row: ReportsTopicSummaryRow,
    filters_active: bool,
) -> str:
    blocked_line = ""
    if row.open_blocked_billing_case_count > 0:
        blocked_line = (
            f"<p><strong>Blocked before invoicing</strong>: "
            f"{html.escape(_count_copy(row.open_blocked_billing_case_count, 'open blocked billing case'))} "
            "still outside paid totals and visible separately in Attention.</p>"
        )

    return f"""
    <article class="content-card stack">
      <div class="content-card-header">
        <div>
          <p class="eyebrow">Authoritative confirmed topic</p>
          <h2>{html.escape(row.canonical_label)}</h2>
        </div>
        <p class="pill-note">{html.escape(_reports_topic_funnel_status_label(row))}</p>
      </div>
      <p><strong>Grouped content rows</strong>: {html.escape(_count_copy(row.content_count, "content row"))}</p>
      <p><strong>Tracked bookings</strong>: {html.escape(_count_copy(row.booking_count, "tracked booking"))}</p>
      <p><strong>Paid revenue</strong>: {html.escape(_format_money_from_cents(row.paid_revenue_cents))}</p>
      <p><strong>Paid invoices</strong>: {html.escape(_count_copy(row.paid_invoice_count, "paid invoice"))}</p>
      <p><strong>Paid bookings</strong>: {html.escape(_count_copy(row.paid_booking_count, "paid booking"))}</p>
      <p><strong>Current grouped state</strong>: {html.escape(_reports_topic_funnel_status_summary(row))}</p>
      {blocked_line}
      <p><strong>Paid window</strong>: {html.escape(_reports_topic_paid_window_copy(row, filters_active=filters_active))}</p>
      <p>This grouped view reuses the existing content funnel truth. One content row can appear here under more than one authoritative topic.</p>
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

    blocked_line = ""
    if row.open_blocked_billing_case_count > 0:
        blocked_line = (
            f"<p><strong>Blocked before invoicing</strong>: "
            f"{html.escape(_count_copy(row.open_blocked_billing_case_count, 'open blocked billing case'))} "
            "still outside paid totals and visible separately in Attention.</p>"
        )

    footer_links = details_link
    if explanation_link:
        footer_links = f"{details_link} {explanation_link}"

    return f"""
    <article class="content-card stack">
      <div class="content-card-header">
        <div>
          <p class="eyebrow">Content funnel</p>
          <h2>{html.escape(_content_card_title(row.source_url))}</h2>
        </div>
        <p class="pill-note">{html.escape(_reports_funnel_status_label(row))}</p>
      </div>
      <p><strong>Source URL</strong>: <a href="{html.escape(row.source_url)}" class="inline-link">{html.escape(row.source_url)}</a></p>
      <p><strong>Tracking ID</strong>: <code>{html.escape(row.tid)}</code></p>
      <p><strong>Bookings</strong>: {html.escape(_count_copy(row.booking_count, "tracked booking"))}</p>
      <p><strong>Paid revenue</strong>: {html.escape(_format_money_from_cents(row.paid_revenue_cents))}</p>
      <p><strong>Paid bookings</strong>: {html.escape(_count_copy(row.paid_booking_count, "paid booking"))}</p>
      <p><strong>Current funnel state</strong>: {html.escape(_reports_funnel_status_summary(row))}</p>
      {blocked_line}
      <p><strong>Paid window</strong>: {html.escape(_reports_paid_window_copy(row, filters_active=filters_active))}</p>
      {footer_links}
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
        'class="inline-link">Why some payments are not counted yet</a>'
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

    body = f"""
    <header class="shell-header">
      <div>
        <p class="eyebrow">Creator Home</p>
        <h1>Why this revenue counted</h1>
        <p class="lede">This result is counted because the same tracking ID moved through your stored content, booking, invoice, and payment record chain.</p>
      </div>
      <form action="/sign-out" method="post">
        <button type="submit" class="secondary">Sign out</button>
      </form>
    </header>
    {_render_shell_nav(current_path="/app/reports")}
    <section class="grid">
      <article class="card stack">
        <div>
          <p class="eyebrow">Creator-scoped evidence</p>
          <h2>Paid attribution for one tracked content row</h2>
          <p>Signed in as <strong class="wrap-anywhere">{creator_email}</strong> for <strong class="wrap-anywhere">{creator_name}</strong>.</p>
        </div>
        <p><strong>Source URL</strong>: <a href="{html.escape(row.source_url)}" class="inline-link">{html.escape(row.source_url)}</a></p>
        <p><strong>Tracking ID</strong>: <code>{html.escape(row.tid)}</code></p>
        <p><strong>Paid window</strong>: {html.escape(_reports_paid_window_copy(row, filters_active=_reports_filters_are_active(filter_values)))}</p>
        <a href="{back_href}" class="inline-link">Back to reports</a>
        <div class="stat-grid">
          <article class="stat-tile">
            <p class="eyebrow">Paid revenue</p>
            <p class="stat-value">{html.escape(_format_money_from_cents(row.paid_revenue_cents))}</p>
          </article>
          <article class="stat-tile">
            <p class="eyebrow">Paid invoices</p>
            <p class="stat-value">{html.escape(str(row.paid_invoice_count))}</p>
            <p>{html.escape(_count_copy(row.paid_invoice_count, "paid invoice"))}</p>
          </article>
          <article class="stat-tile">
            <p class="eyebrow">Paid bookings</p>
            <p class="stat-value">{html.escape(str(row.paid_booking_count))}</p>
            <p>{html.escape(_count_copy(row.paid_booking_count, "paid booking"))}</p>
          </article>
        </div>
      </article>
      <article class="card accent stack">
        <div>
          <p class="eyebrow">How attribution works here</p>
          <h2>The stored chain decides what counts</h2>
        </div>
        <p>This row stays in paid totals when the stored content, booking, and invoice records all point back to the same creator-scoped tracking ID.</p>
        <p>A linked payment event, when present, is supporting provider evidence rather than the settlement gate. If the content, booking, or invoice link is missing, the payment is explained separately and kept out of paid totals until the missing link is repaired.</p>
      </article>
    </section>
    <section class="card stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Counted payment evidence</p>
          <h2>Content to booking to invoice to payment event</h2>
        </div>
        <p>{html.escape(_count_copy(len(explanation.evidence), "invoice chain"))} shown</p>
      </div>
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
    <section class="grid">
      <article class="card stack">
        <div>
          <p class="eyebrow">Tracked content</p>
          <h2>{html.escape(_content_card_title(row.source_url))}</h2>
          <p>Signed in as <strong class="wrap-anywhere">{creator_email}</strong> for <strong class="wrap-anywhere">{creator_name}</strong>.</p>
        </div>
        <p><strong>Source URL</strong>: <a href="{html.escape(row.source_url)}" class="inline-link">{html.escape(row.source_url)}</a></p>
        <p><strong>Tracking ID</strong>: <code>{html.escape(row.tid)}</code></p>
        <p><strong>Booking link</strong>: {html.escape(drilldown.booking_link_name)}</p>
        <p><strong>Current funnel state</strong>: {html.escape(_reports_funnel_status_summary(row))}</p>
        <p><strong>Paid window on this page</strong>: {html.escape(_reports_content_paid_window_copy(drilldown=drilldown, filters_active=filters_active))}</p>
        <a href="{back_href}" class="inline-link">Back to reports</a>
        <div class="stat-grid">
          <article class="stat-tile">
            <p class="eyebrow">Tracked bookings</p>
            <p class="stat-value">{html.escape(str(row.booking_count))}</p>
            <p>{html.escape(_count_copy(row.booking_count, "tracked booking"))}</p>
          </article>
          <article class="stat-tile">
            <p class="eyebrow">All-time paid bookings</p>
            <p class="stat-value">{html.escape(str(row.paid_booking_count))}</p>
            <p>{html.escape(_count_copy(row.paid_booking_count, "paid booking"))}</p>
          </article>
          <article class="stat-tile">
            <p class="eyebrow">All-time paid revenue</p>
            <p class="stat-value">{html.escape(_format_money_from_cents(row.paid_revenue_cents))}</p>
          </article>
        </div>
      </article>
      <article class="card accent stack">
        <div>
          <p class="eyebrow">Reading rules</p>
          <h2>Current funnel truth stays separate from filtered paid outcomes</h2>
        </div>
        <p>Bookings and current funnel state here stay tied to the stored tracking ID for this content. Paid outcomes still come only from canonical invoice and payment truth.</p>
        <p>If you opened this page from a paid-date-filtered reports view, the paid-results section below follows that same window. Diagnostic items only appear here when they still carry this content's canonical tracking ID.</p>
        {clear_filter_link}
        {paid_explanation_link}
      </article>
    </section>
    <section class="card stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Tracked bookings</p>
          <h2>Bookings tied to this content</h2>
        </div>
        <p>{html.escape(_count_copy(len(drilldown.bookings), "booking"))} shown</p>
      </div>
      {_render_reports_content_booking_list(drilldown.bookings)}
    </section>
    <section class="card stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Paid outcomes</p>
          <h2>Invoice-backed results in the current paid window</h2>
        </div>
        <p>{html.escape(_count_copy(paid_window.paid_invoice_count, "paid invoice"))} counted</p>
      </div>
      {_render_reports_content_paid_outcomes(
          drilldown=drilldown,
          filter_values=filter_values,
      )}
    </section>
    <section class="card stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Diagnostic state</p>
          <h2>Items still outside paid totals</h2>
        </div>
        <p>{html.escape(_count_copy(len(drilldown.blocked_cases) + len(drilldown.unmatched_payment_events), "diagnostic item"))} shown</p>
      </div>
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
      <p>Only canonical invoice-backed paid results are counted here. Matching payment events remain supporting evidence, and the deeper chain stays on the existing explanation page.</p>
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
      {explanation_link}
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
          <h2>No content-scoped diagnostics are waiting right now</h2>
          <p>No open blocked billing case or unmatched payment event still carries this content's stored tracking ID today.</p>
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
          <p class="eyebrow">Unmatched payment signals</p>
          <h2>{html.escape(_count_copy(len(drilldown.unmatched_payment_events), "backlog event"))}</h2>
          <div class="content-list">{unmatched_items}</div>
        </div>
        """
        if drilldown.unmatched_payment_events
        else "<p>No unmatched payment event still points back to this content's tracking ID right now.</p>"
    )

    return f"""
    <div class="stack">
      <p>Only blocked cases and unmatched payment events that still carry this content's stored tracking ID appear here. Anything without a safe content link stays on the broader diagnostic pages instead of being guessed onto this row.</p>
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
    payment_event_line = (
        f"<p><strong>Payment event</strong>: "
        f"<code>{html.escape(evidence.provider_event_id or '')}</code> "
        f"stored as {html.escape(payment_event_status_label)} "
        f"and received {_format_timestamp_in_utc(evidence.payment_event_received_at)}.</p>"
        if evidence.provider_event_id is not None and evidence.payment_event_received_at is not None
        else "<p><strong>Payment event</strong>: No linked payment event is stored for this invoice yet.</p>"
    )
    payment_paid_line = (
        f"<p><strong>Provider paid time</strong>: {_format_timestamp_in_utc(evidence.payment_event_paid_at)}</p>"
        if evidence.payment_event_paid_at is not None
        else ""
    )

    return f"""
    <article class="content-card stack">
      <div class="content-card-header">
        <div>
          <p class="eyebrow">Invoice chain {index}</p>
          <h2>{html.escape(_reports_currency_amount_copy(evidence.invoice_currency, evidence.invoice_amount_cents))}</h2>
        </div>
        <p class="pill-note">{html.escape(payment_event_status_label)}</p>
      </div>
      <p><strong>Booking</strong>: <code>{html.escape(evidence.booking_uuid)}</code> captured {_format_timestamp_in_utc(evidence.booked_at)}.</p>
      <p><strong>Payment provider</strong>: <code>{html.escape(payment_provider_label)}</code></p>
      <p><strong>Invoice</strong>: <code>{html.escape(evidence.provider_invoice_id)}</code> marked paid {_format_timestamp_in_utc(evidence.invoice_paid_at)}.</p>
      {payment_event_line}
      {payment_paid_line}
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
        <h1>Why some payments are not counted yet</h1>
        <p class="lede">These are verified payment events that still cannot be trusted as paid content revenue because the creator-scoped attribution chain is incomplete.</p>
      </div>
      <form action="/sign-out" method="post">
        <button type="submit" class="secondary">Sign out</button>
      </form>
    </header>
    {_render_shell_nav(current_path="/app/reports")}
    <section class="grid">
      <article class="card stack">
        <div>
          <p class="eyebrow">Current backlog</p>
          <h2>Unmatched payments stay separate from paid totals</h2>
          <p>Signed in as <strong class="wrap-anywhere">{creator_email}</strong> for <strong class="wrap-anywhere">{creator_name}</strong>.</p>
        </div>
        <p>This page is diagnostic only. It shows counts, causes, and next steps, but it does not estimate revenue for unmatched events.</p>
        <p><strong>Current unmatched backlog</strong>: {html.escape(_unmatched_payment_backlog_copy(backlog.event_count))}</p>
        <a href="{back_href}" class="inline-link">Back to reports</a>
      </article>
      <article class="card accent stack">
        <div>
          <p class="eyebrow">What happens next</p>
          <h2>Only repaired chains move into paid totals</h2>
        </div>
        <p>Some unmatched reasons are creator-fixable, like missing tracked-link setup. Others stay ambiguous because the provider or local system never produced enough booking or invoice context to trust them as revenue yet.</p>
        <p>Only repaired chains move into paid totals, CSV export, and the main paid-results table. Until then, this backlog stays diagnostic only.</p>
      </article>
    </section>
    <section class="card stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Reason summary</p>
          <h2>Why payments are still unmatched</h2>
        </div>
        <p>{html.escape(_count_copy(len(backlog.reasons), "reason"))} visible</p>
      </div>
      {_render_reports_unattributed_reason_cards(summary)}
    </section>
    """
    return _page_layout(title="Why some payments are not counted yet", body=body)


def _render_reports_unattributed_reason_cards(summary: CreatorReportsSummary) -> str:
    backlog = summary.unattributed_current_backlog
    if backlog.event_count == 0:
        return """
        <section class="empty-state">
          <p class="eyebrow">No backlog</p>
          <h2>No unmatched payments are waiting right now</h2>
          <p>Your current paid totals do not have a separate unmatched payment backlog attached to them.</p>
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
              <p class="eyebrow">Unmatched reason</p>
              <h2>{html.escape(reason_copy.label)}</h2>
            </div>
            <p class="pill-note">{html.escape(_count_copy(event_count, "event"))}</p>
          </div>
          <p>{html.escape(reason_copy.summary)}</p>
          <p><strong>Likely cause</strong>: {html.escape(reason_copy.likely_cause)}</p>
          <p><strong>What to do next</strong>: {html.escape(reason_copy.next_step)}</p>
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
    normalized_status = readiness.billing_connect_status
    if normalized_status == "connected":
        current_provider_label = _billing_provider_label(current_billing_provider)
        target_provider_name = replacement_billing_provider_name(
            current_provider=current_billing_provider
        )
        target_provider_label = _billing_provider_label(target_provider_name)
        body = _connected_account_billing_body(readiness)
        actions_html = ""
        if switch_attempt is not None:
            body = _billing_provider_switch_attempt_body(
                current_provider_label=current_provider_label,
                switch_attempt=switch_attempt,
                switch_clean_state=switch_clean_state,
                switch_target_guidance=switch_target_guidance,
            )
            actions_html = _render_billing_provider_switch_attempt_actions(
                switch_attempt=switch_attempt,
                switch_clean_state=switch_clean_state,
                switch_target_guidance=switch_target_guidance,
                paypal_available_to_creator=paypal_available_to_creator,
            )
            if (
                switch_attempt.target_billing_provider == BILLING_PROVIDER_PAYPAL
                and not paypal_available_to_creator
            ):
                body = (
                f"{body} {_PAYPAL_UNAVAILABLE_CREATOR_COPY} "
                    "Cancel the pending switch if you need to stay on the current provider."
                )
        elif switch_clean_state.is_clean:
            target_provider_action = _billing_provider_connect_action(
                provider_name=target_provider_name,
                reconnect=False,
                paypal_available_to_creator=paypal_available_to_creator,
            )
            if target_provider_action is None:
                body = (
                    f"{body} {_PAYPAL_UNAVAILABLE_CREATOR_COPY}"
                )
            else:
                body = (
                    f"{body} You can start a {target_provider_label} switch here. "
                    f"{current_provider_label} stays active until {target_provider_label} is connected, "
                    "ready, and you commit the switch."
                )
                actions_html = _render_post_action_button(
                    action=target_provider_action,
                    label=f"Start {target_provider_label} switch",
                )
        else:
            body = (
                f"{body} Provider switching is blocked right now because this workspace still has "
                f"{_billing_provider_switch_blockers_copy(switch_clean_state=switch_clean_state)}. "
                f"Clear those items before starting a {target_provider_label} switch."
            )
        return {
            "label": "Connected",
            "body": body,
            "badge_class": "connected",
            "actions_html": actions_html,
        }

    if normalized_status == "disconnected":
        provider_action = _billing_provider_connect_action(
            provider_name=readiness.billing_provider,
            reconnect=True,
            paypal_available_to_creator=paypal_available_to_creator,
        )
        body = (
            f"This workspace is not currently connected to {html.escape(_billing_provider_label(readiness.billing_provider))} "
            "for invoicing. You can reconnect it here when you are ready."
        )
        actions_html = _render_post_action_button(action=provider_action) if provider_action is not None else ""
        if provider_action is None:
            body = (
                f"This workspace is not currently connected to {html.escape(_billing_provider_label(readiness.billing_provider))} "
                f"for invoicing. {_PAYPAL_UNAVAILABLE_CREATOR_COPY}"
            )
        return {
            "label": "Disconnected",
            "body": body,
            "badge_class": "disconnected",
            "actions_html": actions_html,
        }

    if show_provider_choice:
        body = (
            "This workspace is not currently connected to a billing provider for invoicing. "
            "Choose Stripe or PayPal here when you are ready. No billing provider is preselected "
            "for this workspace."
            if paypal_available_to_creator
            else "This workspace is not currently connected to a billing provider for invoicing. "
            "Choose Stripe here when you are ready. PayPal setup is not yet available for general creators."
        )
        actions_html = _render_billing_provider_choice_actions(
            paypal_available_to_creator=paypal_available_to_creator
        )
    else:
        provider_action = _billing_provider_connect_action(
            provider_name=readiness.billing_provider,
            reconnect=False,
            paypal_available_to_creator=paypal_available_to_creator,
        )
        if provider_action is None:
            body = (
                "This workspace is not currently connected to a billing provider for invoicing. "
                f"{_PAYPAL_UNAVAILABLE_CREATOR_COPY}"
            )
            actions_html = ""
        else:
            body = (
                "This workspace is not currently connected to a billing provider for invoicing. "
                "You can continue the current setup here when you are ready."
            )
            actions_html = _render_post_action_button(action=provider_action)
    return {
        "label": "Pending",
        "body": body,
        "badge_class": "pending",
        "actions_html": actions_html,
    }


def _connected_account_billing_body(readiness: CreatorWorkspaceReadiness) -> str:
    if _billing_provider_is_connected_but_blocked(readiness):
        return _billing_provider_blocked_copy(provider_name=readiness.billing_provider)
    if _billing_provider_is_connected_but_not_ready(readiness):
        return _billing_provider_not_ready_copy(readiness)
    if readiness.billable_now:
        return (
            "This workspace has a connected billing provider and is billable now for future "
            "invoicing."
        )
    return (
        "This workspace has a connected billing provider, but it is not billable now yet. Save "
        "amount and currency on at least one booking link before new bookings can move into invoicing."
    )


def _billing_provider_switch_attempt_body(
    *,
    current_provider_label: str,
    switch_attempt: BillingProviderSwitchAttempt,
    switch_clean_state: BillingProviderSwitchCleanState,
    switch_target_guidance: _BillingProviderSetupGuidance,
) -> str:
    target_provider_label = _billing_provider_label(switch_attempt.target_billing_provider)
    if (
        switch_attempt.target_billing_connect_status != "connected"
        or switch_attempt.target_billing_account_id is None
    ):
        return (
            f"A {target_provider_label} switch is in progress. {current_provider_label} stays active "
            f"until {target_provider_label} is connected, ready, and you commit the switch."
        )
    if switch_target_guidance.state == _BILLING_PROVIDER_SETUP_STATE_BLOCKED:
        return (
            f"{target_provider_label} is connected for the pending switch, but its invoice readiness "
            "could not be verified right now. "
            f"{current_provider_label} stays active until the readiness check succeeds and you commit "
            "the switch."
        )
    if switch_target_guidance.state == _BILLING_PROVIDER_SETUP_STATE_NOT_READY:
        return (
            f"{target_provider_label} is connected for the pending switch, but it still needs this "
            f"setup work before it can create invoices: "
            f"{_billing_provider_actionable_issue_copy(switch_attempt.target_billing_provider, switch_target_guidance.actionable_issue_codes)}. "
            f"{current_provider_label} stays active until {target_provider_label} is ready and you commit the switch."
        )
    if not switch_clean_state.is_clean:
        return (
            f"{target_provider_label} is connected for the pending switch, but finishing the switch is "
            f"blocked because this workspace still has "
            f"{_billing_provider_switch_blockers_copy(switch_clean_state=switch_clean_state)}. "
            f"{current_provider_label} stays active until those items are cleared."
        )
    return (
        f"{target_provider_label} is connected and ready for the pending switch. "
        f"{current_provider_label} stays active until you commit the switch."
    )


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
      <p><strong>Choose billing provider</strong></p>
      <p>No billing provider is preselected for this workspace. Choose one provider to continue setup.</p>
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
        return "Paid result recorded"
    if row.funnel_status == "blocked_before_invoicing":
        return "Blocked before invoicing"
    if row.funnel_status == "waiting_for_first_paid_result":
        return "Waiting for first paid result"
    return "No bookings yet"


def _reports_funnel_status_summary(row: ReportsSummaryRow) -> str:
    if row.funnel_status == "paid_result_recorded":
        if row.open_blocked_billing_case_count > 0:
            return (
                "This content already has counted paid results, but some newer booking activity "
                "is still blocked before invoicing."
            )
        return "This content already has invoice-backed paid results in canonical reporting."
    if row.funnel_status == "blocked_before_invoicing":
        return (
            "Tracked bookings reached billing, but at least one open blocked billing case still "
            "keeps that booking activity outside paid totals."
        )
    if row.funnel_status == "waiting_for_first_paid_result":
        return (
            "Canonical bookings are recorded for this content, but no invoice-backed paid result "
            "is counted yet."
        )
    return "This content is tracked, but no canonical booking has been recorded for it yet."


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
        f"{_count_copy(event_count, 'event')} diagnostic only and still outside paid totals "
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
        width: min(960px, calc(100% - 32px));
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

        .status-row,
        .checklist-item,
        .section-heading,
        .booking-link-header,
        .content-card-header,
        .activity-card-header {{
          flex-direction: column;
          align-items: flex-start;
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
