import html
import uuid
from datetime import date, timezone
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
    create_content_response_for_creator,
    get_content_response_for_creator_by_tid,
    list_content_responses_for_creator,
)
from app.api.deps import get_optional_browser_auth_user
from app.api.stripe import (
    STRIPE_CONNECT_FAILED_STATUS,
    STRIPE_CONNECT_INTERRUPTED_STATUS,
    build_stripe_connect_start_response,
)
from app.core.config import get_settings
from app.db.session import SessionLocal, get_db
from app.models.auth_user import AuthUser
from app.schemas.booking_link import BookingLinkCreateRequest, BookingLinkResponse
from app.schemas.auth import MagicLinkStartRequest
from app.schemas.content import ContentCreateRequest, ContentResponse
from app.services.auth_magic_link import start_magic_link
from app.services.blocked_billing import (
    BLOCKED_BILLING_REASON_CREATOR_NOT_BILLABLE,
    BLOCKED_BILLING_REASON_PROVIDER_ERROR,
    BlockedBillingCaseSummary,
    BlockedBillingRetryService,
    count_open_blocked_billing_cases,
    list_open_blocked_billing_cases,
)
from app.services.browser_session import (
    clear_browser_session_cookie,
    get_browser_session_token,
)
from app.services.email_provider import MagicLinkEmailDeliveryError
from app.services.invoice_payment_events import (
    UNATTRIBUTED_REASON_MISSING_TID,
    UNATTRIBUTED_REASON_UNKNOWN_BOOKING_UUID,
    UNATTRIBUTED_REASON_UNKNOWN_STRIPE_INVOICE_ID,
    UnmatchedPaymentEventSummary,
    list_current_unmatched_payment_events,
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
from app.services.stripe_provider import build_default_stripe_provider

router = APIRouter(include_in_schema=False)

STATUS_MESSAGES = {
    "sent": {
        "title": "Check your inbox",
        "body": "If the address is valid, we sent a fresh sign-in link. If it expires, request another one here.",
        "notice_class": "notice success",
    },
    "invalid-email": {
        "title": "Enter a valid email",
        "body": "Use a real email address so we can send a secure sign-in link.",
        "notice_class": "notice error",
    },
    "invalid-link": {
        "title": "That sign-in link is invalid or expired",
        "body": "Enter your email below and we will send a fresh link so you can keep going.",
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
    blocked_billing_count = count_open_blocked_billing_cases(
        creator_id=current_user.creator_id,
        db=db,
    )
    unmatched_payment_count = len(
        list_current_unmatched_payment_events(
            creator_id=current_user.creator_id,
            db=db,
        )
    )

    return _html_response(
        _render_app_shell(
            current_user=current_user,
            booking_links=booking_links,
            content_items=content_items,
            blocked_billing_count=blocked_billing_count,
            unmatched_payment_count=unmatched_payment_count,
            status_value=status_value,
        )
    )


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
    filter_values = _reports_filter_values(dict(request.query_params))
    start_date, end_date, field_errors = _reports_date_filters_from_values(filter_values)

    summary = get_creator_reports_summary(
        creator_id=current_user.creator_id,
        db=db,
    )
    blocked_billing_count = count_open_blocked_billing_cases(
        creator_id=current_user.creator_id,
        db=db,
    )
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

    return _html_response(
        _render_reports_page(
            current_user=current_user,
            content_items=content_items,
            summary=summary,
            blocked_billing_count=blocked_billing_count,
            filter_values=filter_values,
            field_errors=field_errors,
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
      <p class="lede">Use your email to get a secure sign-in link, then finish Stripe, Calendly, and tracked-link setup inside the app.</p>
      {message_block}
      <form action="/sign-in" method="post" class="card">
        <label for="email">Email</label>
        <input id="email" name="email" type="email" autocomplete="email" placeholder="creator@example.com" required />
        <button type="submit">Send magic link</button>
      </form>
      <p class="footnote">If the last link expired or the setup tab was closed, request another email here and continue from setup home.</p>
    </section>
    """
    return _page_layout(title="Creator sign in", body=body)


def _render_app_shell(
    *,
    current_user: AuthUser,
    booking_links: list[BookingLinkResponse],
    content_items: list[ContentResponse],
    blocked_billing_count: int,
    unmatched_payment_count: int,
    status_value: str | None,
) -> str:
    creator_name = html.escape(current_user.creator.name)
    creator_email = html.escape(current_user.email)
    stripe_status = _stripe_setup_home_state(current_user.creator.stripe_connect_status)
    setup_progress = _build_setup_home_progress(
        raw_stripe_status=current_user.creator.stripe_connect_status,
        booking_links=booking_links,
        content_items=content_items,
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


def _build_setup_home_progress(
    *,
    raw_stripe_status: str,
    booking_links: list[BookingLinkResponse],
    content_items: list[ContentResponse],
    blocked_billing_count: int,
    unmatched_payment_count: int,
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
    attention_count = blocked_billing_count + unmatched_payment_count

    if normalized_stripe_status == "connected":
        stripe_step = _setup_step(
            title="Connect Stripe",
            copy_html="Stripe is connected. New bookings can use this workspace once the rest of setup is finished.",
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
            copy_html=f"{html.escape(_count_copy(billing_ready_count, 'billing-ready link'))} already has amount and currency saved for invoicing.",
            label="Done",
            badge_class="connected",
            item_class="done",
            is_complete=True,
        )
    elif booking_links_count > 0:
        billing_defaults_step = _setup_step(
            title="Add billing defaults",
            copy_html='At least one saved booking link still needs both amount and currency before invoicing can run safely. <a href="/app/booking-links" class="inline-link">Add billing defaults</a>.',
            label="Blocked",
            badge_class="disconnected",
            item_class="todo",
            is_complete=False,
        )
        if next_action is None:
            next_action = {
                "title": "Add billing defaults",
                "copy_html": "This setup is blocked until at least one booking link has both amount and currency saved.",
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
            copy_html=f"{html.escape(_count_copy(tracked_content_count, 'tracked link'))} ready to copy into the content you share.",
            label="Done",
            badge_class="connected",
            item_class="done",
            is_complete=True,
        )
    elif booking_links_count > 0:
        tracked_link_step = _setup_step(
            title="Create a tracked link",
            copy_html='Create one tracked link so bookings can be tied back to the content that sent them. <a href="/app/content" class="inline-link">Open content</a>.',
            label="Needs action",
            badge_class="pending",
            item_class="todo",
            is_complete=False,
        )
        if next_action is None:
            next_action = {
                "title": "Create your first tracked link",
                "copy_html": "Save a source URL and copy the generated tracked link into the post, page, or CTA you share.",
                "action_label": "Open content",
                "action_href": "/app/content",
                "action_method": "get",
            }
    else:
        tracked_link_step = _setup_step(
            title="Create a tracked link",
            copy_html="Booking links come first. After that, create a tracked link from a real source URL you plan to share.",
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
        else:
            next_action = {
                "title": "Start using your tracked link",
                "copy_html": "Core setup is ready. Copy the tracked link you want to share, then watch bookings and reports as real activity arrives.",
                "action_label": "Open content",
                "action_href": "/app/content",
                "action_method": "get",
            }

    progress_copy = "Finish the next highlighted step to move this workspace forward."
    if completed_count == len(steps):
        progress_copy = "Core setup is ready for real activity."

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
        ("/app/attention", "Attention"),
        ("/app/reports", "Reports"),
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
        <p>This first visibility slice shows only the basics: booking status, timestamps, and the tracked content plus booking-link context that the verified webhook resolved from stored app data.</p>
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


def _render_reports_page(
    *,
    current_user: AuthUser,
    content_items: list[ContentResponse],
    summary: CreatorReportsSummary,
    blocked_billing_count: int,
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
          <h2>Keep pending fixes separate from paid totals</h2>
        </div>
        <p>Paid totals on this page come only from invoices that are marked paid and matched back to your tracked content through the stored booking chain.</p>
        <p><strong>Current unmatched backlog</strong>: {html.escape(_count_copy(summary.unattributed_current_backlog.event_count, "event"))} waiting on more attribution context.</p>
        {_render_reports_unmatched_explainer(summary)}
        {_render_reports_unmatched_reasons(summary)}
        {_render_reports_unmatched_explanation_link(
            summary=summary,
            filter_values=filter_values,
        )}
        <p><strong>Blocked billing cases</strong>: {html.escape(_reports_blocked_case_copy(blocked_billing_count))}</p>
        <p><a href="/app/attention" class="inline-link">Review blocked billing and unresolved payment details</a></p>
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
        <p class="lede">Review bookings that are blocked before invoicing and payment events still waiting on attribution repair.</p>
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
          <h2>Bookings waiting for invoice recovery</h2>
          <p>Signed in as <strong class="wrap-anywhere">{creator_email}</strong> for <strong class="wrap-anywhere">{creator_name}</strong>.</p>
        </div>
        <p>{html.escape(_count_copy(blocked_count, "booking"))} currently waiting on invoice creation or retry.</p>
      </article>
      <article class="card accent stack">
        <div>
          <p class="eyebrow">Unresolved payments</p>
          <h2>Payment events still outside paid totals</h2>
        </div>
        <p>{html.escape(_count_copy(unmatched_count, "event"))} still waiting on canonical attribution links before they can be counted.</p>
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

    return f"""
    <article class="activity-card stack">
      <div class="activity-card-header">
        <div>
          <p class="eyebrow">Booking activity</p>
          <h2>{html.escape(_content_card_title(booking.source_url))}</h2>
        </div>
        <span class="status-pill {html.escape(status["badge_class"])}">{html.escape(status["label"])}</span>
      </div>
      <p><strong>Booked at</strong>: {_format_timestamp_in_utc(booking.booked_at)}</p>
      {canceled_at_line}
      <p><strong>Booking link</strong>: {html.escape(booking.booking_link_name)}</p>
      <p><strong>Source URL</strong>: <a href="{html.escape(booking.source_url)}" class="inline-link">{html.escape(booking.source_url)}</a></p>
      <p><strong>Tracking ID</strong>: <code>{html.escape(booking.tid)}</code></p>
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
    summary: CreatorReportsSummary,
    filters_active: bool,
    filter_values: dict[str, str],
) -> str:
    if not summary.rows:
        return _render_reports_empty_state(
            has_tracked_content=bool(content_items),
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


def _render_reports_empty_state(*, has_tracked_content: bool, filters_active: bool) -> str:
    if not has_tracked_content:
        return """
        <section class="empty-state">
          <p class="eyebrow">No tracked content yet</p>
          <h2>Create tracked content first</h2>
          <p>This reporting page fills in only after you save a tracked link and a paid invoice is matched back to it.</p>
          <a href="/app/content" class="inline-link">Create tracked content</a>
        </section>
        """

    if filters_active:
        return """
        <section class="empty-state">
          <p class="eyebrow">No results in this window</p>
          <h2>No paid results match this paid-date filter</h2>
          <p>Try widening the paid-date range or clear the filters to see all invoice-backed paid results for this creator.</p>
          <a href="/app/reports" class="inline-link">Clear filters</a>
        </section>
        """

    return """
    <section class="empty-state">
      <p class="eyebrow">No paid results yet</p>
      <h2>No paid results yet</h2>
      <p>You already have tracked content, but nothing is counted here until a matching invoice is marked paid.</p>
    <a href="/app/content" class="inline-link">Review tracked content</a>
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
          <p>When invoice creation is deferred for a tracked booking, it will appear here with the frozen billing inputs and the latest retry-safe reason.</p>
        </section>
        """

    items = "".join(
        _render_blocked_billing_case_card(blocked_case=blocked_case)
        for blocked_case in blocked_cases
    )
    return f'<div class="content-list">{items}</div>'


def _render_blocked_billing_case_card(*, blocked_case: BlockedBillingCaseSummary) -> str:
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
      <p><strong>Reason</strong>: {html.escape(_blocked_billing_reason_label(blocked_case.reason_code))} (<code>{html.escape(blocked_case.reason_code)}</code>)</p>
      <p>{html.escape(_blocked_billing_reason_explanation(blocked_case.reason_code))}</p>
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
          <p>When a paid Stripe event cannot be linked back to canonical local booking or invoice state yet, it will appear here with the current reason and lifecycle timestamps.</p>
        </section>
        """

    items = "".join(
        _render_unmatched_payment_event_card(payment_event=payment_event)
        for payment_event in unmatched_events
    )
    return f'<div class="content-list">{items}</div>'


def _render_unmatched_payment_event_card(*, payment_event: UnmatchedPaymentEventSummary) -> str:
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
          <h2>{html.escape(_reports_reason_label(payment_event.unattributed_reason))}</h2>
        </div>
        <span class="status-pill pending">{html.escape(_reports_payment_event_status_label(payment_event.status))}</span>
      </div>
      <p>{html.escape(_reports_reason_explanation(payment_event.unattributed_reason))}</p>
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
        <p>This row stays in paid totals only when the stored content, booking, invoice, and payment-event records all point back to the same creator-scoped tracking ID.</p>
        <p>If any part of that chain is missing, the payment is explained separately and kept out of paid totals until the missing link is repaired.</p>
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
    payment_event_line = (
        f"<p><strong>Payment event</strong>: "
        f"<code>{html.escape(evidence.stripe_event_id or '')}</code> "
        f"stored as {html.escape(_reports_payment_event_status_label(evidence.payment_event_status))} "
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
        <p class="pill-note">{html.escape(_reports_payment_event_status_label(evidence.payment_event_status))}</p>
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
        <p>This page shows counts and reasons only. It does not estimate revenue for unmatched events because the missing content, booking, or invoice link has not been repaired yet.</p>
        <p><strong>Current unmatched backlog</strong>: {html.escape(_count_copy(backlog.event_count, "event"))} waiting on more attribution context.</p>
        <a href="{back_href}" class="inline-link">Back to reports</a>
      </article>
      <article class="card accent stack">
        <div>
          <p class="eyebrow">What happens next</p>
          <h2>Only repaired chains move into paid totals</h2>
        </div>
        <p>Once the missing tracking, booking, or invoice link is restored in canonical local data, the payment can be reconciled and counted through the same reporting path as the paid content rows.</p>
        <p>Until then, this backlog remains explanatory only and does not change the paid totals or CSV export.</p>
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
        f"""
        <article class="content-card stack">
          <div class="content-card-header">
            <div>
              <p class="eyebrow">Unmatched reason</p>
              <h2>{html.escape(_reports_reason_label(reason.reason))}</h2>
            </div>
            <p class="pill-note">{html.escape(_count_copy(reason.event_count, "event"))}</p>
          </div>
          <p>{html.escape(_reports_reason_explanation(reason.reason))}</p>
        </article>
        """
        for reason in backlog.reasons
    )
    return f'<div class="content-list">{items}</div>'


def _render_reports_unmatched_reasons(summary: CreatorReportsSummary) -> str:
    backlog = summary.unattributed_current_backlog
    if backlog.event_count == 0:
        return "<p>No current unmatched payment backlog is waiting to be repaired.</p>"

    items = "".join(
        (
            f"<li><strong>{html.escape(_reports_reason_label(reason.reason))}</strong>: "
            f"{html.escape(_count_copy(reason.event_count, 'event'))}</li>"
        )
        for reason in backlog.reasons
    )
    return f'<ul class="reason-list">{items}</ul>'


def _render_reports_unmatched_explainer(summary: CreatorReportsSummary) -> str:
    backlog = summary.unattributed_current_backlog
    if backlog.event_count == 0:
        return ""

    return (
        "<p>These backlog events are separate from the paid content rows below. "
        "You can still see an attributed paid result while a different payment is "
        "waiting for its tracking details to be repaired.</p>"
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


def _stripe_setup_home_state(raw_status: str) -> dict[str, str]:
    normalized_status = raw_status.strip().lower()
    if normalized_status == "connected":
        return {
            "label": "Connected",
            "heading": "Stripe is connected",
            "description": "This workspace already has a connected Stripe account. Keep going with booking links, billing defaults, and tracked links.",
            "button_label": "",
            "badge_class": "connected",
            "item_class": "done",
            "checklist_label": "Done",
            "checklist_copy": "Your Stripe account is connected. The next setup work is booking links, billing defaults, and tracked links.",
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


def _reports_reason_label(reason: str | None) -> str:
    if reason == UNATTRIBUTED_REASON_MISSING_TID:
        return "Missing tracking ID"
    if reason == UNATTRIBUTED_REASON_UNKNOWN_BOOKING_UUID:
        return "Unknown booking"
    if reason == UNATTRIBUTED_REASON_UNKNOWN_STRIPE_INVOICE_ID:
        return "Unknown invoice"
    return (reason or "Unknown reason").replace("_", " ").title()


def _reports_reason_explanation(reason: str | None) -> str:
    if reason == UNATTRIBUTED_REASON_MISSING_TID:
        return (
            "A verified payment event arrived, but the tracking ID needed to connect it back "
            "to a tracked content row was missing. The payment stays out of paid totals until "
            "that creator-scoped link can be repaired."
        )
    if reason == UNATTRIBUTED_REASON_UNKNOWN_BOOKING_UUID:
        return (
            "The payment event carried invoice context, but the current creator-scoped chain "
            "could not find the matching booking yet. Until the booking is linked, the payment "
            "cannot be trusted as paid content revenue."
        )
    if reason == UNATTRIBUTED_REASON_UNKNOWN_STRIPE_INVOICE_ID:
        return (
            "The payment event could not be matched to a canonical stored invoice yet. Until "
            "that invoice link exists, the event stays explanatory instead of changing paid totals."
        )
    return "The payment event is missing canonical attribution context, so it stays out of paid totals for now."


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


def _reports_blocked_case_copy(blocked_billing_count: int) -> str:
    if blocked_billing_count == 0:
        return "no tracked bookings are blocked before invoicing right now."
    return f"{_count_copy(blocked_billing_count, 'booking')} waiting on invoice recovery or retry."


def _blocked_billing_reason_label(reason_code: str) -> str:
    if reason_code == BLOCKED_BILLING_REASON_CREATOR_NOT_BILLABLE:
        return "Creator not billable"
    if reason_code == BLOCKED_BILLING_REASON_PROVIDER_ERROR:
        return "Provider error"
    return reason_code.replace("_", " ").title()


def _blocked_billing_reason_explanation(reason_code: str) -> str:
    if reason_code == BLOCKED_BILLING_REASON_CREATOR_NOT_BILLABLE:
        return (
            "Stripe was not ready to create an invoice for this creator account yet, "
            "so the booking was kept without guessing or dropping the invoice inputs."
        )
    if reason_code == BLOCKED_BILLING_REASON_PROVIDER_ERROR:
        return (
            "The provider failed during readiness or invoice creation, so the booking "
            "stayed blocked with the latest provider context and frozen billing inputs."
        )
    return "This booking is blocked until the stored billing condition is repaired."


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
