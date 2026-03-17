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
from app.api.stripe import (
    STRIPE_CONNECT_FAILED_STATUS,
    STRIPE_CONNECT_INTERRUPTED_STATUS,
    build_stripe_connect_start_response,
)
from app.core.config import get_settings
from app.db.session import SessionLocal, get_db
from app.models.auth_user import AuthUser
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
from app.services.email_provider import (
    MagicLinkEmailDeliveryError,
    SupportRequestEmailDeliveryError,
)
from app.services.evidence_ingress_health import (
    AUTHORITATIVE_CONTENT_LAG_REASON_MISSING_AUTHORITY,
    AUTHORITATIVE_CONTENT_LAG_REASON_STALE_AUTHORITY,
    CreatorEvidenceIngressHealthSnapshot,
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
    NextContentExperimentUnsupportedExplanation,
    create_creator_next_content_experiments_run,
    get_creator_next_content_experiment_card_drilldown,
    get_creator_next_content_experiments_run,
    get_current_creator_next_content_experiments_unsupported_explanation,
    get_latest_creator_next_content_experiments_run,
)
from app.services.rate_limit import (
    DEFAULT_SHARED_RATE_LIMITER,
    SUPPORT_REQUEST_SUBMIT_POLICY,
    build_support_request_rate_limit_bucket_key,
)
from app.services.reporting import (
    CreatorPaidAttributionExplanation,
    CreatorReportsSummary,
    PaidAttributionEvidence,
    ReportsSummaryRow,
    build_reports_summary_csv,
    get_creator_paid_attribution_explanation,
    get_creator_reports_summary,
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
    "name",
    "calendly_url",
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
    readiness = _build_creator_readiness(
        raw_stripe_status=current_user.creator.stripe_connect_status,
        booking_links=booking_links,
        content_items=content_items,
        paid_invoice_count=summary.paid_invoice_count,
    )

    return _html_response(
        _render_app_shell(
            current_user=current_user,
            readiness=readiness,
            blocked_billing_count=summary.blocked_summary.open_case_count,
            unmatched_payment_count=summary.unattributed_current_backlog.event_count,
            status_value=status_value,
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
    readiness = _build_creator_readiness(
        raw_stripe_status=current_user.creator.stripe_connect_status,
        booking_links=booking_links,
        content_items=content_items,
        paid_invoice_count=summary.paid_invoice_count,
    )
    support_requests = list_latest_support_requests_for_creator(
        db,
        creator_id=current_user.creator_id,
    )
    active_support_requests = list_active_support_requests_for_creator(
        db,
        creator_id=current_user.creator_id,
    )
    return _html_response(
        _render_account_page(
            current_user=current_user,
            booking_links=booking_links,
            readiness=readiness,
            support_requests=support_requests,
            active_support_requests=active_support_requests,
            status_value=status_value,
            confirm_value=confirm_value,
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
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    start_response = build_stripe_connect_start_response(
        request=request,
        current_user=current_user,
    )
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

    create_booking_link_response_for_creator(
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

    readiness = _build_creator_readiness(
        raw_stripe_status=current_user.creator.stripe_connect_status,
        booking_links=booking_links,
        content_items=content_items,
        paid_invoice_count=overall_summary.paid_invoice_count,
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
        provider=_ui_stripe_provider(request),
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


def _empty_booking_link_form_values() -> dict[str, str]:
    return {field_name: "" for field_name in BOOKING_LINK_FORM_FIELDS}


def _booking_link_form_values(raw_values: dict[str, str]) -> dict[str, str]:
    form_values = _empty_booking_link_form_values()
    form_values.update(
        {
            "name": raw_values.get("name", "").strip(),
            "calendly_url": raw_values.get("calendly_url", "").strip(),
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

    if form_values["billing_amount_cents"]:
        try:
            billing_amount_cents = int(form_values["billing_amount_cents"])
        except ValueError:
            field_errors["billing_amount_cents"] = "Enter a whole number of cents."

    if field_errors:
        return None, field_errors

    try:
        payload = BookingLinkCreateRequest(
            name=form_values["name"],
            calendly_url=form_values["calendly_url"],
            billing_amount_cents=billing_amount_cents,
            billing_currency=form_values["billing_currency"] or None,
        )
    except ValidationError as exc:
        return None, _booking_link_field_errors(exc)

    return payload, {}


def _booking_link_field_errors(exc: ValidationError) -> dict[str, str]:
    errors: dict[str, str] = {}
    for error in exc.errors():
        location = error.get("loc") or ()
        field_name = str(location[-1]) if location else ""
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
      <p class="lede">Use your email to get a secure sign-in link, then open it on this same device and browser to finish Stripe, Calendly, and tracked-link setup inside the app.</p>
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
    readiness: dict[str, object],
    blocked_billing_count: int,
    unmatched_payment_count: int,
    status_value: str | None,
) -> str:
    creator_name = html.escape(current_user.creator.name)
    creator_email = html.escape(current_user.email)
    stripe_status = _stripe_setup_home_state(
        raw_status=current_user.creator.stripe_connect_status,
        readiness=readiness,
    )
    setup_progress = _build_setup_home_progress(
        readiness=readiness,
        blocked_billing_count=blocked_billing_count,
        unmatched_payment_count=unmatched_payment_count,
    )

    stripe_detail_lines = []
    if current_user.creator.stripe_account_id:
        stripe_detail_lines.append(
            f"<p><strong>Connected account</strong>: "
            f"{html.escape(current_user.creator.stripe_account_id)}</p>"
        )
    if current_user.creator.stripe_connected_at:
        stripe_detail_lines.append(
            f"<p><strong>Connected on</strong>: "
            f"{_format_connected_at(current_user.creator.stripe_connected_at)}</p>"
        )

    stripe_action = ""
    if stripe_status["button_label"]:
        stripe_action = f"""
        <form action="/app/stripe/connect/start" method="post">
          <button type="submit">{html.escape(stripe_status["button_label"])}</button>
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
        <p>This workspace holds your Stripe connection, booking links, tracked content, and any blocked or unresolved items that still need review.</p>
      </article>
      <article class="card accent">
        <p class="eyebrow">Stripe status</p>
        <div class="status-row">
          <h2>{html.escape(stripe_status['heading'])}</h2>
          <span class="status-pill {html.escape(stripe_status['badge_class'])}">{html.escape(stripe_status['label'])}</span>
        </div>
        <p>{html.escape(stripe_status['description'])}</p>
        {"".join(stripe_detail_lines)}
        {_render_readiness_summary(readiness=readiness)}
        {stripe_action}
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
        {_render_setup_next_action_cta(setup_progress['next_action'])}
        <p class="footnote">{_setup_attention_copy(setup_progress['attention_count'])}</p>
      </article>
    </section>
    """
    return _page_layout(title="Creator Home", body=body)


def _render_account_page(
    *,
    current_user: AuthUser,
    booking_links: list[BookingLinkResponse],
    readiness: dict[str, object],
    support_requests: dict[str, SupportRequestRecord],
    active_support_requests: dict[str, SupportRequestRecord],
    status_value: str | None,
    confirm_value: str | None,
) -> str:
    creator_name = html.escape(current_user.creator.name)
    creator_email = html.escape(current_user.email)
    stripe_state = _account_stripe_management_state(
        raw_status=current_user.creator.stripe_connect_status,
        readiness=readiness,
    )
    booking_links_count = len(booking_links)
    billing_ready_count = sum(
        1
        for booking_link in booking_links
        if booking_link.billing_amount_cents is not None
        and booking_link.billing_currency is not None
    )

    stripe_detail_lines = []
    if current_user.creator.stripe_account_id:
        stripe_detail_lines.append(
            f"<p><strong>Connected account</strong>: "
            f"{html.escape(current_user.creator.stripe_account_id)}</p>"
        )
    if current_user.creator.stripe_connected_at:
        stripe_detail_lines.append(
            f"<p><strong>Connected on</strong>: "
            f"{_format_connected_at(current_user.creator.stripe_connected_at)}</p>"
        )

    booking_links_summary = "No booking links are saved yet for this workspace."
    if booking_links_count > 0:
        booking_links_summary = (
            f"This workspace currently has "
            f"{html.escape(_count_copy(booking_links_count, 'saved booking link'))}. "
            f"{html.escape(_account_billing_ready_summary_copy(billing_ready_count))}"
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
        <p>Signed in as <strong class="wrap-anywhere">{creator_email}</strong>. This workspace currently holds your Stripe connection, booking links, tracked content, reports, and any blocked or unresolved items still waiting on review.</p>
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
            <p class="eyebrow">Stripe connection</p>
            <h2>Stripe connection</h2>
          </div>
          <span class="status-pill {html.escape(stripe_state['badge_class'])}">{html.escape(stripe_state['label'])}</span>
        </div>
        <p>{html.escape(stripe_state['body'])}</p>
        {"".join(stripe_detail_lines)}
        {_render_readiness_summary(readiness=readiness)}
        <p><strong>What this changes</strong></p>
        <p>Changing the Stripe connection affects future billing readiness. It does not erase local history already recorded for this workspace, and it does not delete anything from Stripe automatically.</p>
        <form action="/app/stripe/connect/start" method="post">
          <button type="submit">{html.escape(stripe_state['action_label'])}</button>
        </form>
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
          <p>A reset can break tracked links, remove local setup state, and make earlier reports or history unavailable from this workspace. Reset does not delete or reverse anything in Stripe or Calendly automatically.</p>
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
          <p>Deleting this account can remove local workspace data and end access to tracked links, reports, and historical workspace views. Some detached diagnostics may remain without workspace links, and Stripe or Calendly accounts are not deleted automatically.</p>
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


def _render_setup_next_action_cta(next_action: dict[str, str]) -> str:
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


def _build_creator_readiness(
    *,
    raw_stripe_status: str,
    booking_links: list[BookingLinkResponse],
    content_items: list[ContentResponse],
    paid_invoice_count: int,
) -> dict[str, object]:
    normalized_stripe_status = raw_stripe_status.strip().lower()
    booking_links_count = len(booking_links)
    billing_ready_count = sum(
        1
        for booking_link in booking_links
        if booking_link.billing_amount_cents is not None
        and booking_link.billing_currency is not None
    )
    tracked_content_count = len(content_items)
    connected = normalized_stripe_status == "connected"
    billable_now = connected and billing_ready_count > 0
    ready_to_track = billable_now and tracked_content_count > 0
    waiting_for_first_paid_result = ready_to_track and paid_invoice_count == 0

    return {
        "stripe_status": normalized_stripe_status,
        "connected": connected,
        "billable_now": billable_now,
        "ready_to_track": ready_to_track,
        "waiting_for_first_paid_result": waiting_for_first_paid_result,
        "booking_links_count": booking_links_count,
        "billing_ready_count": billing_ready_count,
        "tracked_content_count": tracked_content_count,
        "paid_invoice_count": paid_invoice_count,
    }


def _readiness_stage_summary(readiness: dict[str, object]) -> dict[str, str]:
    if int(readiness["paid_invoice_count"]) > 0:
        return {
            "title": "Paid results are already landing",
            "copy": (
                "This workspace has moved past the first-value wait. Use Reports to review "
                "what is already counted."
            ),
        }

    if bool(readiness["waiting_for_first_paid_result"]):
        return {
            "title": "Ready to track and waiting for first paid result",
            "copy": (
                "First value lands only after tracked content leads to a booking and the "
                "matching invoice is marked paid."
            ),
        }

    if bool(readiness["billable_now"]):
        return {
            "title": "Billable now, but not ready to track",
            "copy": (
                "Create tracked content next so the shared link can carry attribution into "
                "bookings and later paid results."
            ),
        }

    if bool(readiness["connected"]):
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
            "Value does not arrive right after sign-in. Connect Stripe first, then make one "
            "booking link billable now, then create tracked content."
        ),
    }


def _readiness_line_items(readiness: dict[str, object]) -> list[tuple[str, str, str]]:
    stripe_status = str(readiness["stripe_status"])
    booking_links_count = int(readiness["booking_links_count"])

    if bool(readiness["connected"]):
        connected_line = ("Connected", "Done", "Stripe is connected to this workspace.")
    elif stripe_status == "disconnected":
        connected_line = (
            "Connected",
            "Not yet",
            "Reconnect Stripe before relying on new bookings.",
        )
    else:
        connected_line = ("Connected", "Not yet", "Finish Stripe setup first.")

    if bool(readiness["billable_now"]):
        billable_line = (
            "Billable now",
            "Done",
            "At least one booking link has amount and currency saved.",
        )
    elif bool(readiness["connected"]) and booking_links_count > 0:
        billable_line = (
            "Billable now",
            "Not yet",
            "Add amount and currency to at least one saved booking link.",
        )
    elif bool(readiness["connected"]):
        billable_line = (
            "Billable now",
            "Not yet",
            "Save a booking link, then add amount and currency.",
        )
    else:
        billable_line = (
            "Billable now",
            "Not yet",
            "Stripe must be connected before this workspace can be billable now.",
        )

    if bool(readiness["ready_to_track"]):
        ready_to_track_line = (
            "Ready to track",
            "Done",
            "At least one tracked link is ready to share on a billable setup.",
        )
    elif bool(readiness["billable_now"]):
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

    if int(readiness["paid_invoice_count"]) > 0:
        waiting_line = (
            "Waiting for first paid result",
            "Done",
            "Reports already includes counted paid results for this workspace.",
        )
    elif bool(readiness["waiting_for_first_paid_result"]):
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


def _render_readiness_summary(*, readiness: dict[str, object]) -> str:
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
    readiness: dict[str, object],
    blocked_billing_count: int,
    unmatched_payment_count: int,
) -> dict[str, object]:
    normalized_stripe_status = str(readiness["stripe_status"])
    booking_links_count = int(readiness["booking_links_count"])
    billing_ready_count = int(readiness["billing_ready_count"])
    tracked_content_count = int(readiness["tracked_content_count"])
    billable_now = bool(readiness["billable_now"])
    ready_to_track = bool(readiness["ready_to_track"])
    paid_invoice_count = int(readiness["paid_invoice_count"])
    attention_count = blocked_billing_count + unmatched_payment_count

    if normalized_stripe_status == "connected":
        stripe_step = _setup_step(
            title="Connect Stripe",
            copy_html=(
                "Stripe is connected. "
                + (
                    "This workspace is already billable now while you finish the rest of setup."
                    if billable_now
                    else "The next milestone is billable now, which needs amount and currency on at least one booking link."
                )
            ),
            label="Done",
            badge_class="connected",
            item_class="done",
            is_complete=True,
        )
        next_action = None
    elif normalized_stripe_status == "disconnected":
        stripe_step = _setup_step(
            title="Connect Stripe",
            copy_html="Stripe was connected before, but it is disconnected now. Reconnect it before new bookings can move into invoicing.",
            label="Blocked",
            badge_class="disconnected",
            item_class="todo",
            is_complete=False,
        )
        next_action = {
            "title": "Reconnect Stripe",
            "copy_html": "Stripe is the first setup blocker. Reconnect it from this page before you rely on new bookings.",
            "action_label": "Reconnect Stripe",
            "action_href": "/app/stripe/connect/start",
            "action_method": "post",
        }
    else:
        stripe_step = _setup_step(
            title="Connect Stripe",
            copy_html="Finish Stripe onboarding so this workspace has a payment account ready for invoicing.",
            label="Needs action",
            badge_class="pending",
            item_class="todo",
            is_complete=False,
        )
        next_action = {
            "title": "Finish Stripe setup",
            "copy_html": "Start Stripe first so the rest of the setup flow leads to a billable workspace.",
            "action_label": "Start Stripe setup",
            "action_href": "/app/stripe/connect/start",
            "action_method": "post",
        }

    if booking_links_count > 0:
        booking_link_step = _setup_step(
            title="Save a booking link",
            copy_html=f"{html.escape(_count_copy(booking_links_count, 'booking link'))} saved. Keep the Calendly link here aligned with what you actually share.",
            label="Done",
            badge_class="connected",
            item_class="done",
            is_complete=True,
        )
    else:
        booking_link_step = _setup_step(
            title="Save a booking link",
            copy_html='Add the Calendly link you want this workspace to track. <a href="/app/booking-links" class="inline-link">Open booking links</a>.',
            label="Needs action",
            badge_class="pending",
            item_class="todo",
            is_complete=False,
        )
        if next_action is None:
            next_action = {
                "title": "Add your first booking link",
                "copy_html": "Save the Calendly URL you actually use so tracked content has a real booking destination.",
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
                    else "already has amount and currency. Connect Stripe so this workspace becomes billable now."
                )
            ),
            label="Done",
            badge_class="connected",
            item_class="done",
            is_complete=True,
        )
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
        stripe_step,
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

    progress_copy = "Connect Stripe first, then make one booking link billable now and create tracked content."
    if bool(readiness["connected"]):
        progress_copy = "Stripe is connected. The next milestone is billable now."
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

    body = f"""
    <header class="shell-header">
      <div>
        <p class="eyebrow">Creator Home</p>
        <h1>Booking Links</h1>
        <p class="lede">Add the Calendly URLs this creator actually uses and, when available, store billing defaults that later invoice automation can trust.</p>
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

          <label for="calendly_url">Calendly URL</label>
          <input
            id="calendly_url"
            name="calendly_url"
            type="url"
            value="{html.escape(form_values["calendly_url"])}"
            placeholder="https://calendly.com/example/discovery-call"
            required
            aria-invalid="{str("calendly_url" in field_errors).lower()}"
          />
          {_render_booking_link_field_error(field_errors.get("calendly_url"))}

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
          <p>Add a Calendly URL now so the next creator workflow stories can attach tracked content and later invoice defaults to a real creator-owned link.</p>
        </section>
        """

    items = "".join(_render_booking_link_card(booking_link) for booking_link in booking_links)
    return f'<div class="booking-link-list">{items}</div>'


def _render_booking_link_card(booking_link: BookingLinkResponse) -> str:
    return f"""
    <article class="booking-link-card">
      <div class="booking-link-header">
        <div>
          <p class="eyebrow">Booking link</p>
          <h2>{html.escape(booking_link.name)}</h2>
        </div>
        <p class="pill-note">{html.escape(_billing_defaults_copy(booking_link))}</p>
      </div>
      <p><strong>Calendly URL</strong>: <a href="{html.escape(booking_link.calendly_url)}" class="inline-link">{html.escape(booking_link.calendly_url)}</a></p>
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
        <p class="lede">Turn a public source URL into a tracked link that routes through the attribution redirect before it reaches your Calendly booking flow.</p>
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
        <p>The tracked link uses the stored content `tid`, so later redirect and Calendly booking flows can attribute the booking back to the right source URL.</p>
        <p>Pick a saved booking link, paste in the public URL for the content you are publishing, then copy the generated tracked link into the content or CTA you share externally.</p>
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

    return f"""
    <article class="card stack">
      <div>
        <p class="eyebrow">Create tracked content</p>
        <h2>Add a source URL</h2>
        <p>Signed in as <strong class="wrap-anywhere">{creator_email}</strong> for <strong class="wrap-anywhere">{creator_name}</strong>.</p>
      </div>
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
        <p class="form-help">This keeps the tracked content aligned with the creator-owned Calendly link that downstream booking capture expects.</p>
        {_render_content_field_error(field_errors.get("booking_link_id"))}

        <button type="submit">Generate tracked link</button>
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
        selected_attr = " selected" if booking_link.id == selected_booking_link_id else ""
        options.append(
            f'<option value="{html.escape(booking_link.id)}"{selected_attr}>'
            f"{html.escape(booking_link.name)}"
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
        <p class="lede">See whether tracked content is turning into verified Calendly bookings, without needing raw DB checks or API tooling.</p>
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
        <p>Bookings only show up here after someone uses a tracked link and Calendly delivers the verified webhook back to this app. That handoff is not always instant.</p>
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
        <p>Deeper evidence drilldown stays for the next story. This page shows only the top-level snapshot id, inline evidence summary, and the tracked content id behind each card.</p>
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
    return f"""
    <article class="content-card stack">
      <div class="content-card-header">
        <div>
          <p class="eyebrow">Experiment {index}</p>
          <h2>{html.escape(experiment.title)}</h2>
        </div>
        <span class="status-pill confirmed">Hypothesis</span>
      </div>
      <p><strong>Hypothesis</strong>: {html.escape(experiment.hypothesis)}</p>
      <p><strong>Why this might work</strong>: {html.escape(experiment.why_this_might_work)}</p>
      <p><strong>Evidence summary</strong>: {html.escape(experiment.evidence_summary)}</p>
      <p><strong>Content tracking ID</strong>: {content_tids}</p>
      <p><strong>Caution</strong>: {html.escape(experiment.caution)}</p>
      <p><a href="/app/experiments/{html.escape(str(run_claim_snapshot_id))}/cards/{index}" class="inline-link">View evidence</a></p>
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
      <p><strong>Generated</strong>: {_format_timestamp_in_utc(drilldown.created_at)}</p>
      <p><strong>Hypothesis</strong>: {html.escape(drilldown.hypothesis)}</p>
      <p><strong>Why this might work</strong>: {html.escape(drilldown.why_this_might_work)}</p>
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
    list_heading = "Paid content results" if summary.rows else "No paid results yet"
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
        <p class="lede">Review which tracked content is producing paid results, using the date each invoice was actually paid.</p>
      </div>
      <form action="/sign-out" method="post">
        <button type="submit" class="secondary">Sign out</button>
      </form>
    </header>
    {_render_shell_nav(current_path="/app/reports")}
    {_render_reports_notice(field_errors=field_errors)}
    <section class="grid">
      <article class="card stack">
        <div>
          <p class="eyebrow">Paid-date filter</p>
          <h2>Invoice-backed paid results</h2>
          <p>Signed in as <strong class="wrap-anywhere">{creator_email}</strong> for <strong class="wrap-anywhere">{creator_name}</strong>.</p>
        </div>
        <p>Use the paid date below to narrow the summary by when the invoice payment actually landed. This page does not group results by booking date.</p>
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
          <p class="eyebrow">Content summary</p>
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
    <section class="card stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Calendly ingress</p>
          <h2>Webhook backlog and failure counts</h2>
        </div>
        <p>{html.escape(_count_copy(snapshot.calendly_ingress.backlog_event_count + snapshot.calendly_ingress.failed_event_count, "event"))}</p>
      </div>
      {_render_health_reason_list(
          items=[
              f"{_count_copy(item.event_count, 'event')} currently marked {_health_calendly_status_label(item.processing_status).lower()}."
              for item in snapshot.calendly_ingress.statuses
              if item.event_count > 0
          ],
          empty_heading="No Calendly backlog or failures are waiting right now",
          empty_body="Verified Calendly events for this creator are not currently sitting in backlog or failure states.",
      )}
      <p>Use structured webhook logs for event-level identifiers and replay context when these counts rise.</p>
    </section>
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
          <p>Bookings appear here only after someone uses one of your tracked links and the verified Calendly webhook is processed, so a brand-new booking may not appear immediately.</p>
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
    readiness: dict[str, object],
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
        )
        for row in summary.rows
    )
    return f'<div class="content-list">{items}</div>'


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


def _render_reports_empty_state(*, readiness: dict[str, object], filters_active: bool) -> str:
    if filters_active:
        return """
        <section class="empty-state">
          <p class="eyebrow">No results in this window</p>
          <h2>No paid results match this paid-date filter</h2>
          <p>Try widening the paid-date range or clear the filters to see all invoice-backed paid results for this creator.</p>
          <a href="/app/reports" class="inline-link">Clear filters</a>
        </section>
        """

    if bool(readiness["waiting_for_first_paid_result"]):
        return f"""
        <section class="empty-state stack">
          <p class="eyebrow">Waiting for first paid result</p>
          <h2>Waiting for first paid result</h2>
          <p>This workspace is ready to track. Reports stays empty until real tracked content leads to a booking and the matching invoice is marked paid.</p>
          {_render_illustrative_first_value_proof()}
          <a href="/app/content" class="inline-link">Review tracked content</a>
        </section>
        """

    if bool(readiness["billable_now"]):
        return """
        <section class="empty-state">
          <p class="eyebrow">Not ready to track yet</p>
          <h2>Ready to track is the next milestone</h2>
          <p>This workspace is billable now, but Reports stays empty until you create tracked content and that tracked link leads to a paid invoice.</p>
          <a href="/app/content" class="inline-link">Create tracked content</a>
        </section>
        """

    if bool(readiness["connected"]):
        return """
        <section class="empty-state">
          <p class="eyebrow">Not billable now</p>
          <h2>Billable now comes before paid results</h2>
          <p>Stripe is connected, but this workspace is not billable now yet. Save amount and currency on at least one booking link, then create tracked content.</p>
          <a href="/app/booking-links" class="inline-link">Add billing defaults</a>
        </section>
        """

    return """
    <section class="empty-state">
      <p class="eyebrow">Not connected yet</p>
      <h2>Connected comes before paid results</h2>
      <p>Connect Stripe first. Then make one booking link billable now and create tracked content before Reports can fill in.</p>
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
    if blocked_case.invoice_id is not None or blocked_case.stripe_invoice_id is not None:
        invoice_copy = (
            f'{html.escape(str(blocked_case.invoice_id or ""))} / '
            f'{html.escape(blocked_case.stripe_invoice_id or "missing provider id")}'
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
          <h2>{html.escape(blocked_case.calendly_booking_uuid)}</h2>
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
      <p><strong>Stripe account</strong>: <code>{html.escape(blocked_case.stripe_account_id or "not_connected")}</code></p>
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
          <p>If a paid Stripe event cannot be linked back to canonical local booking or invoice state yet, it will appear here with the reason, likely cause, and next step.</p>
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
      <p><strong>Stripe event</strong>: <code>{html.escape(payment_event.stripe_event_id)}</code></p>
      <p><strong>Stripe invoice</strong>: <code>{html.escape(payment_event.stripe_invoice_id)}</code></p>
      <p><strong>Stripe account</strong>: <code>{html.escape(payment_event.stripe_account_id or "unknown")}</code></p>
      <p><strong>Booking</strong>: {booking_copy}</p>
      <p><strong>TID</strong>: {tid_copy}</p>
      <p><strong>Reason code</strong>: <code>{html.escape(payment_event.unattributed_reason or "unknown")}</code></p>
      <p><strong>Paid at</strong>: {paid_at_copy}</p>
      <p><strong>Received at</strong>: {_format_timestamp_in_utc(payment_event.received_at)}</p>
      <p><strong>Processed at</strong>: {processed_copy}</p>
    </article>
    """


def _render_reports_row_card(*, row: ReportsSummaryRow, filter_values: dict[str, str]) -> str:
    explanation_href = html.escape(
        _reports_paid_explanation_href(
            tid=row.tid,
            filter_values=filter_values,
        ),
        quote=True,
    )
    return f"""
    <article class="content-card stack">
      <div class="content-card-header">
        <div>
          <p class="eyebrow">Paid content</p>
          <h2>{html.escape(_content_card_title(row.source_url))}</h2>
        </div>
        <p class="pill-note">{html.escape(_count_copy(row.paid_invoice_count, "paid invoice"))}</p>
      </div>
      <p><strong>Source URL</strong>: <a href="{html.escape(row.source_url)}" class="inline-link">{html.escape(row.source_url)}</a></p>
      <p><strong>Tracking ID</strong>: <code>{html.escape(row.tid)}</code></p>
      <p><strong>Paid revenue</strong>: {html.escape(_format_money_from_cents(row.paid_revenue_cents))}</p>
      <p><strong>Paid bookings</strong>: {html.escape(_count_copy(row.paid_booking_count, "paid booking"))}</p>
      <p><strong>Paid window</strong>: {html.escape(_reports_paid_window_copy(row))}</p>
      <a href="{explanation_href}" class="inline-link">Why this revenue counted</a>
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
        <p><strong>Paid window</strong>: {html.escape(_reports_paid_window_copy(row))}</p>
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
    payment_event_status_label = (
        _reports_payment_event_status_label(evidence.payment_event_status)
        if evidence.payment_event_status is not None
        else "Invoice-settled"
    )
    payment_event_line = (
        f"<p><strong>Payment event</strong>: "
        f"<code>{html.escape(evidence.stripe_event_id or '')}</code> "
        f"stored as {html.escape(payment_event_status_label)} "
        f"and received {_format_timestamp_in_utc(evidence.payment_event_received_at)}.</p>"
        if evidence.stripe_event_id is not None and evidence.payment_event_received_at is not None
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
      <p><strong>Invoice</strong>: <code>{html.escape(evidence.stripe_invoice_id)}</code> marked paid {_format_timestamp_in_utc(evidence.invoice_paid_at)}.</p>
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
          <p><strong>Still blocked.</strong> The retry was safe, but the current Stripe readiness or provider state still prevented invoice creation.</p>
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


def _stripe_setup_home_state(*, raw_status: str, readiness: dict[str, object]) -> dict[str, str]:
    normalized_status = raw_status.strip().lower()
    if normalized_status == "connected":
        description = (
            "Stripe is connected, but this workspace is not billable now yet. Save amount "
            "and currency on at least one booking link."
        )
        checklist_copy = (
            "Stripe is connected. The next milestone is billable now, which needs amount "
            "and currency on at least one booking link."
        )
        if bool(readiness["billable_now"]):
            description = (
                "Stripe is connected and this workspace is billable now. Keep going until "
                "it is also ready to track."
            )
            checklist_copy = (
                "Stripe is connected. This workspace is already billable now while you "
                "finish the rest of setup."
            )
        return {
            "label": "Connected",
            "heading": "Stripe is connected",
            "description": description,
            "button_label": "",
            "badge_class": "connected",
            "item_class": "done",
            "checklist_label": "Done",
            "checklist_copy": checklist_copy,
        }

    if normalized_status == "disconnected":
        return {
            "label": "Disconnected",
            "heading": "Stripe is disconnected",
            "description": "This workspace was connected before, but it is disconnected now. Reconnect it before new bookings can move into invoicing.",
            "button_label": "Reconnect Stripe",
            "badge_class": "disconnected",
            "item_class": "todo",
            "checklist_label": "Blocked",
            "checklist_copy": "Reconnect Stripe before new bookings can move into invoicing for this workspace.",
        }

    return {
        "label": "Pending",
        "heading": "Stripe setup is still pending",
        "description": "Stripe is required before this workspace can turn new bookings into invoices. Start or resume the connection from this page.",
        "button_label": "Start Stripe setup",
        "badge_class": "pending",
        "item_class": "todo",
        "checklist_label": "Needs action",
        "checklist_copy": "Finish Stripe onboarding so this workspace has a payment account ready for invoicing.",
    }


def _account_stripe_management_state(
    *,
    raw_status: str,
    readiness: dict[str, object],
) -> dict[str, str]:
    normalized_status = raw_status.strip().lower()
    if normalized_status == "connected":
        body = (
            "This workspace is connected to Stripe, but it is not billable now yet. Save "
            "amount and currency on at least one booking link before new bookings can move "
            "into invoicing."
        )
        if bool(readiness["billable_now"]):
            body = (
                "This workspace is connected to Stripe and billable now for future "
                "invoicing. You can reconnect or replace that connection without deleting "
                "your historical bookings, invoices, reports, or recovery history."
            )
        return {
            "label": "Connected",
            "body": body,
            "action_label": "Reconnect Stripe",
            "badge_class": "connected",
        }

    if normalized_status == "disconnected":
        return {
            "label": "Disconnected",
            "body": (
                "This workspace is not currently connected to Stripe for invoicing. "
                "You can connect or reconnect Stripe here when you are ready."
            ),
            "action_label": "Reconnect Stripe",
            "badge_class": "disconnected",
        }

    return {
        "label": "Pending",
        "body": (
            "This workspace is not currently connected to Stripe for invoicing. "
            "You can connect or reconnect Stripe here when you are ready."
        ),
        "action_label": "Start Stripe setup",
        "badge_class": "pending",
    }


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
                "It does not automatically delete Stripe or Calendly accounts, and historical "
                "workspace access may not be recoverable after deletion work begins."
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


def _reports_export_href(filter_values: dict[str, str]) -> str:
    return f"/app/reports/export.csv{_reports_query_string(filter_values)}"


def _reports_unattributed_explanation_href(filter_values: dict[str, str]) -> str:
    return f"/app/reports/explanations/unattributed{_reports_query_string(filter_values)}"


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


def _reports_paid_window_copy(row: ReportsSummaryRow) -> str:
    if row.first_paid_at == row.last_paid_at:
        return row.first_paid_at.astimezone(timezone.utc).strftime("%B %d, %Y")
    return (
        f"{row.first_paid_at.astimezone(timezone.utc).strftime('%B %d, %Y')} to "
        f"{row.last_paid_at.astimezone(timezone.utc).strftime('%B %d, %Y')}"
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


def _health_calendly_status_label(processing_status: str) -> str:
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
                "This is usually creator-fixable setup work: Stripe connection, billing "
                "readiness, or required account details were not ready yet."
            ),
            next_step=(
                "Finish the Stripe or billing setup and then retry invoice creation. Until an "
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
