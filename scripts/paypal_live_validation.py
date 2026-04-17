import argparse
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from jose import jwt
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.core.config import PAYPAL_ENVIRONMENT_LIVE, get_settings
from app.db.session import SessionLocal
from app.models.auth_user import AuthUser
from app.models.blocked_billing_case import BlockedBillingCase
from app.models.booking import Booking
from app.models.booking_link import BookingLink
from app.models.content import Content
from app.models.creator import Creator
from app.models.invoice import Invoice
from app.models.invoice_payment_event import InvoicePaymentEvent
from app.services.billing import BillingOrchestrator
from app.services.billing_provider import build_billing_provider_registry
from app.services.paypal_provider import build_default_paypal_provider
from app.services.stripe_provider import build_default_stripe_provider


DEFAULT_APP_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_BOOKING_LINK_URL = "https://calendly.com/example/paypal-live-validation"
DEFAULT_CONTENT_SOURCE_URL = "https://example.com/paypal-live-validation"


class PayPalLiveValidationError(RuntimeError):
    pass


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "show-config":
        return _run_show_config(args)
    if args.command == "prepare-proof":
        return _run_prepare_proof(args)
    if args.command == "start-order":
        return _run_start_order(args)
    if args.command == "create-invoice":
        return _run_create_invoice(args)
    if args.command == "void-invoice":
        return _run_void_invoice(args)
    if args.command == "show-trace":
        return _run_show_trace(args)
    if args.command == "show-state":
        return _run_show_state(args)

    parser.error(f"unsupported command: {args.command}")
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare and inspect operator-gated live PayPal proof state through the app's real billing seams."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    show_config = subparsers.add_parser(
        "show-config",
        help="Report the selected PayPal runtime environment and whether live-proof prerequisites are configured.",
    )
    show_config.add_argument(
        "--require-live",
        action="store_true",
        help="Exit non-zero unless the selected environment is live and the required live settings are present.",
    )
    show_config.add_argument(
        "--creator-email",
        help="Optional creator email to check against the operator allowlist.",
    )
    show_config.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of key=value lines.",
    )

    prepare = subparsers.add_parser(
        "prepare-proof",
        help="Create or reuse one operator-owned creator plus one fresh booking graph for PP-14 live validation.",
    )
    prepare.add_argument(
        "--creator-email",
        required=True,
        help="Email for the operator-owned creator/auth user.",
    )
    prepare.add_argument(
        "--creator-name",
        default="PayPal Live Validation",
        help="Creator name to use when inserting a new creator.",
    )
    prepare.add_argument(
        "--booking-email",
        help="Email that should receive the live invoice. Defaults to the creator email.",
    )
    prepare.add_argument(
        "--booking-label",
        default="proof",
        help="Short label to distinguish multiple PP-14 live-proof bookings for the same creator.",
    )
    prepare.add_argument(
        "--booking-link-name",
        default="PayPal Live Validation Link",
        help="Booking link name for the seeded booking graph.",
    )
    prepare.add_argument(
        "--booking-link-url",
        default=DEFAULT_BOOKING_LINK_URL,
        help=f"Destination scheduling URL to store on the booking link. Default: {DEFAULT_BOOKING_LINK_URL}",
    )
    prepare.add_argument(
        "--content-source-url",
        default=DEFAULT_CONTENT_SOURCE_URL,
        help=f"Source URL to store on the content row. Default: {DEFAULT_CONTENT_SOURCE_URL}",
    )
    prepare.add_argument(
        "--billing-amount-cents",
        type=int,
        default=15000,
        help="Billing amount for the seeded booking link. Default: 15000",
    )
    prepare.add_argument(
        "--billing-currency",
        default="USD",
        help="Billing currency for the seeded booking link. Default: USD",
    )
    prepare.add_argument(
        "--base-url",
        default=DEFAULT_APP_BASE_URL,
        help=f"Base URL for local API calls such as /paypal/connect/start. Default: {DEFAULT_APP_BASE_URL}",
    )
    prepare.add_argument(
        "--reuse-creator",
        action="store_true",
        help="Reuse the existing creator/auth user if the email already exists.",
    )
    prepare.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of key=value lines.",
    )

    create_invoice = subparsers.add_parser(
        "create-invoice",
        help="Run BillingOrchestrator.create_invoice_for_booking for one seeded booking.",
    )
    create_invoice.add_argument("--booking-id", required=True, help="Booking UUID to invoice.")
    create_invoice.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of key=value lines.",
    )

    start_order = subparsers.add_parser(
        "start-order",
        help="Call the auth-protected /paypal/orders/start route for one seeded booking.",
    )
    start_order.add_argument("--booking-id", required=True, help="Booking UUID to start.")
    start_order.add_argument(
        "--base-url",
        default=DEFAULT_APP_BASE_URL,
        help=f"Base URL for the local API route. Default: {DEFAULT_APP_BASE_URL}",
    )
    start_order.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of key=value lines.",
    )

    void_invoice = subparsers.add_parser(
        "void-invoice",
        help="Run BillingOrchestrator.void_open_invoice_for_booking for one seeded booking.",
    )
    void_invoice.add_argument("--booking-id", required=True, help="Booking UUID to void.")
    void_invoice.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of key=value lines.",
    )

    show_trace = subparsers.add_parser(
        "show-trace",
        help="Print the currently configured PayPal API trace records used for sandbox or live packet evidence.",
    )
    show_trace.add_argument(
        "--trace-path",
        help="Optional explicit trace path. Defaults to PAYPAL_API_TRACE_PATH from settings.",
    )
    show_trace.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of key=value lines.",
    )

    show_state = subparsers.add_parser(
        "show-state",
        help="Print the current creator, booking, invoice, payment-event, and blocked-case state for live validation.",
    )
    show_state.add_argument(
        "--creator-email",
        required=True,
        help="Creator email to inspect.",
    )
    show_state.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of key=value lines.",
    )

    return parser


def _run_show_config(args: argparse.Namespace) -> int:
    settings = get_settings()
    payload = _config_summary(
        settings=settings,
        creator_email=args.creator_email,
        require_live=args.require_live,
    )
    _emit(payload, as_json=args.json)
    if args.require_live and not payload["ready_for_live_proof"]:
        return 2
    return 0


def _run_prepare_proof(args: argparse.Namespace) -> int:
    settings = get_settings()
    normalized_creator_email = _normalized_email(args.creator_email)
    normalized_booking_email = _normalized_email(args.booking_email or normalized_creator_email)
    now = datetime.now(UTC)
    booking_label = _slug(args.booking_label) or "proof"
    booking_suffix = uuid.uuid4().hex[:12]
    provider_booking_id = f"BOOK_PP14_{booking_label}_{booking_suffix}".upper()
    tid = uuid.uuid4().hex

    with SessionLocal() as session:
        auth_user = session.scalar(
            select(AuthUser)
            .options(joinedload(AuthUser.creator))
            .where(AuthUser.email == normalized_creator_email)
        )

        creator_created = False
        if auth_user is None:
            creator = Creator(name=args.creator_name)
            session.add(creator)
            session.flush()

            auth_user = AuthUser(
                creator_id=creator.id,
                email=normalized_creator_email,
            )
            session.add(auth_user)
            session.flush()
            creator_created = True
        else:
            creator = auth_user.creator
            if creator is None:
                raise PayPalLiveValidationError(
                    f"auth user {normalized_creator_email} is missing its creator relationship"
                )
            if not args.reuse_creator:
                raise PayPalLiveValidationError(
                    "creator email already exists; rerun with --reuse-creator or choose a new --creator-email"
                )

        booking_link = BookingLink(
            creator_id=creator.id,
            name=f"{args.booking_link_name} {booking_label}",
            calendly_url=args.booking_link_url,
            billing_amount_cents=args.billing_amount_cents,
            billing_currency=args.billing_currency.upper(),
        )
        session.add(booking_link)
        session.flush()

        content = Content(
            creator_id=creator.id,
            booking_link_id=booking_link.id,
            source_url=args.content_source_url,
            tid=tid,
        )
        session.add(content)
        session.flush()

        booking = Booking(
            creator_id=creator.id,
            booking_link_id=booking_link.id,
            tid=content.tid,
            provider="calendly",
            provider_booking_id=provider_booking_id,
            calendly_booking_uuid=provider_booking_id,
            email=normalized_booking_email,
            status="created",
            booked_at=now,
        )
        session.add(booking)
        session.commit()
        session.refresh(creator)
        session.refresh(auth_user)
        session.refresh(booking_link)
        session.refresh(content)
        session.refresh(booking)

        creator_id = str(creator.id)
        creator_name = creator.name
        creator_billing_provider = creator.resolved_billing_provider
        creator_billing_connect_status = creator.resolved_billing_connect_status
        creator_billing_account_id = creator.resolved_billing_account_id
        creator_correlation_id = creator.billing_provider_correlation_id
        creator_connected_at = _iso_or_none(creator.resolved_billing_connected_at)
        auth_user_id = str(auth_user.id)
        booking_id = str(booking.id)
        booking_provider_booking_id = booking.resolved_provider_booking_id
        booking_email = booking.email
        booking_link_id = str(booking_link.id)
        content_id = str(content.id)
        billing_amount_cents = booking_link.billing_amount_cents
        billing_currency = booking_link.billing_currency

    access_token = _create_access_token(
        user_id=auth_user_id,
        creator_id=creator_id,
        email=auth_user.email,
        settings=settings,
    )
    connect_start_url = _append_path(args.base_url, "/paypal/connect/start")
    show_state_command = (
        ".venv\\Scripts\\python.exe scripts\\paypal_live_validation.py "
        f"show-state --creator-email {normalized_creator_email} --json"
    )
    create_invoice_command = (
        ".venv\\Scripts\\python.exe scripts\\paypal_live_validation.py "
        f"create-invoice --booking-id {booking_id} --json"
    )
    start_order_command = (
        ".venv\\Scripts\\python.exe scripts\\paypal_live_validation.py "
        f"start-order --booking-id {booking_id} --base-url {args.base_url} --json"
    )
    void_invoice_command = (
        ".venv\\Scripts\\python.exe scripts\\paypal_live_validation.py "
        f"void-invoice --booking-id {booking_id} --json"
    )

    payload = {
        "creator_created": creator_created,
        "creator_id": creator_id,
        "creator_email": normalized_creator_email,
        "creator_name": creator_name,
        "creator_billing_provider": creator_billing_provider,
        "creator_billing_connect_status": creator_billing_connect_status,
        "creator_billing_account_id": creator_billing_account_id,
        "creator_correlation_id": creator_correlation_id,
        "creator_connected_at": creator_connected_at,
        "creator_email_allowlisted": settings.is_operator_email_allowed(normalized_creator_email),
        "auth_user_id": auth_user_id,
        "booking_id": booking_id,
        "booking_provider_booking_id": booking_provider_booking_id,
        "booking_email": booking_email,
        "booking_link_id": booking_link_id,
        "content_id": content_id,
        "tid": tid,
        "billing_amount_cents": billing_amount_cents,
        "billing_currency": billing_currency,
        "paypal_environment": settings.paypal_environment_value(),
        "paypal_api_trace_path": _resolved_trace_path(settings=settings),
        "paypal_connect_redirect_uri": settings.paypal_connect_redirect_uri,
        "connect_start_url": connect_start_url,
        "authorization_header": f"Bearer {access_token}",
        "access_token": access_token,
        "connect_start_curl": (
            f'curl.exe -X POST "{connect_start_url}" '
            f'-H "Authorization: Bearer {access_token}" '
            '-H "Accept: application/json"'
        ),
        "connect_start_powershell": (
            f"Invoke-RestMethod -Method Post -Uri '{connect_start_url}' "
            f"-Headers @{{ Authorization = 'Bearer {access_token}'; Accept = 'application/json' }}"
        ),
        "show_state_command": show_state_command,
        "start_order_command": start_order_command,
        "create_invoice_command": create_invoice_command,
        "void_invoice_command": void_invoice_command,
    }
    _emit(payload, as_json=args.json)
    return 0


def _run_start_order(args: argparse.Namespace) -> int:
    booking_id = _parse_uuid(args.booking_id, field_name="booking-id")
    with SessionLocal() as session:
        booking = session.get(Booking, booking_id)
        if booking is None:
            raise PayPalLiveValidationError(f"booking {booking_id} was not found")
        auth_user = session.scalar(
            select(AuthUser).where(AuthUser.creator_id == booking.creator_id)
        )
        if auth_user is None:
            raise PayPalLiveValidationError(
                f"creator {booking.creator_id} is missing an auth user for route auth"
            )
        auth_user_id = str(auth_user.id)
        creator_id = str(auth_user.creator_id)
        auth_user_email = auth_user.email

    settings = get_settings()
    access_token = _create_access_token(
        user_id=auth_user_id,
        creator_id=creator_id,
        email=auth_user_email,
        settings=settings,
    )
    start_url = _append_path(args.base_url, "/paypal/orders/start")
    payload = _post_json(
        url=start_url,
        access_token=access_token,
        body={"booking_id": str(booking_id)},
    )
    _emit(payload, as_json=args.json)
    return 0


def _run_create_invoice(args: argparse.Namespace) -> int:
    booking_id = _parse_uuid(args.booking_id, field_name="booking-id")
    providers = build_billing_provider_registry(
        providers=[
            build_default_stripe_provider(),
            build_default_paypal_provider(),
        ]
    )
    orchestrator = BillingOrchestrator(
        session_factory=SessionLocal,
        providers=providers,
    )
    result = orchestrator.create_invoice_for_booking(booking_id=booking_id)
    payload = {
        "booking_id": str(booking_id),
        "outcome": result.outcome,
        "reason": result.reason,
        "invoice_id": _string_or_none(result.invoice_id),
        "provider_account_id": result.provider_account_id,
        "provider_invoice_id": result.provider_invoice_id,
        "invoice_status": result.invoice_status,
    }
    _emit(payload, as_json=args.json)
    return 0


def _run_void_invoice(args: argparse.Namespace) -> int:
    booking_id = _parse_uuid(args.booking_id, field_name="booking-id")
    providers = build_billing_provider_registry(
        providers=[
            build_default_stripe_provider(),
            build_default_paypal_provider(),
        ]
    )
    orchestrator = BillingOrchestrator(
        session_factory=SessionLocal,
        providers=providers,
    )
    result = orchestrator.void_open_invoice_for_booking(booking_id=booking_id)
    payload = {
        "booking_id": str(booking_id),
        "outcome": result.outcome,
        "reason": result.reason,
        "invoice_id": _string_or_none(result.invoice_id),
        "provider_account_id": result.provider_account_id,
        "provider_invoice_id": result.provider_invoice_id,
        "invoice_status": result.invoice_status,
    }
    _emit(payload, as_json=args.json)
    return 0


def _run_show_trace(args: argparse.Namespace) -> int:
    settings = get_settings()
    trace_path = _resolved_trace_path(explicit_path=args.trace_path, settings=settings)
    if trace_path is None:
        raise PayPalLiveValidationError(
            "PAYPAL_API_TRACE_PATH is not configured; set it first or pass --trace-path"
        )

    resolved_path = Path(trace_path)
    if not resolved_path.exists():
        payload = {
            "trace_path": str(resolved_path),
            "record_count": 0,
            "records": [],
        }
        _emit(payload, as_json=args.json)
        return 0

    records: list[dict[str, Any]] = []
    for raw_line in resolved_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        records.append(json.loads(line))

    payload = {
        "trace_path": str(resolved_path),
        "record_count": len(records),
        "records": records,
    }
    _emit(payload, as_json=args.json)
    return 0


def _run_show_state(args: argparse.Namespace) -> int:
    settings = get_settings()
    normalized_creator_email = _normalized_email(args.creator_email)

    with SessionLocal() as session:
        auth_user = session.scalar(
            select(AuthUser)
            .options(joinedload(AuthUser.creator))
            .where(AuthUser.email == normalized_creator_email)
        )
        if auth_user is None or auth_user.creator is None:
            raise PayPalLiveValidationError(f"creator email {normalized_creator_email!r} was not found")

        creator = auth_user.creator
        content_rows = session.scalars(
            select(Content)
            .where(Content.creator_id == creator.id)
            .order_by(Content.tid.desc())
        ).all()
        booking_rows = session.scalars(
            select(Booking)
            .where(Booking.creator_id == creator.id)
            .order_by(Booking.booked_at.desc())
        ).all()
        invoice_rows = session.scalars(
            select(Invoice)
            .where(Invoice.creator_id == creator.id)
            .order_by(Invoice.issued_at.desc())
        ).all()
        payment_event_rows = session.scalars(
            select(InvoicePaymentEvent)
            .where(InvoicePaymentEvent.creator_id == creator.id)
            .order_by(InvoicePaymentEvent.received_at.desc())
        ).all()
        blocked_case_rows = session.scalars(
            select(BlockedBillingCase)
            .where(BlockedBillingCase.creator_id == creator.id)
            .order_by(BlockedBillingCase.last_blocked_at.desc())
        ).all()

    payload = {
        "creator": {
            "id": str(creator.id),
            "email": auth_user.email,
            "name": creator.name,
            "billing_provider": creator.resolved_billing_provider,
            "billing_connect_status": creator.resolved_billing_connect_status,
            "billing_account_id": creator.resolved_billing_account_id,
            "billing_provider_correlation_id": creator.billing_provider_correlation_id,
            "billing_connected_at": _iso_or_none(creator.resolved_billing_connected_at),
            "email_allowlisted_for_operator": settings.is_operator_email_allowed(auth_user.email),
        },
        "content_items": [
            {
                "id": str(content.id),
                "booking_link_id": str(content.booking_link_id),
                "source_url": content.source_url,
                "tid": content.tid,
            }
            for content in content_rows
        ],
        "bookings": [
            {
                "id": str(booking.id),
                "booking_link_id": str(booking.booking_link_id),
                "tid": booking.tid,
                "provider": booking.provider,
                "provider_booking_id": booking.resolved_provider_booking_id,
                "email": booking.email,
                "status": booking.status,
                "booked_at": _iso_or_none(booking.booked_at),
                "canceled_at": _iso_or_none(booking.canceled_at),
                "frozen_billing_amount_cents": booking.frozen_billing_amount_cents,
                "frozen_billing_currency": booking.frozen_billing_currency,
            }
            for booking in booking_rows
        ],
        "invoices": [
            {
                "id": str(invoice.id),
                "booking_id": str(invoice.booking_id),
                "tid": invoice.tid,
                "payment_provider": invoice.resolved_payment_provider,
                "provider_account_id": invoice.resolved_provider_account_id,
                "provider_invoice_id": invoice.resolved_provider_invoice_id,
                "provider_action_url": invoice.provider_action_url,
                "status": invoice.status,
                "amount_cents": invoice.amount_cents,
                "currency": invoice.currency,
                "issued_at": _iso_or_none(invoice.issued_at),
                "paid_at": _iso_or_none(invoice.paid_at),
                "voided_at": _iso_or_none(invoice.voided_at),
            }
            for invoice in invoice_rows
        ],
        "payment_events": [
            {
                "id": str(event.id),
                "invoice_id": _string_or_none(event.invoice_id),
                "booking_id": _string_or_none(event.booking_id),
                "tid": event.tid,
                "payment_provider": event.resolved_payment_provider,
                "provider_event_id": event.resolved_provider_event_id,
                "provider_event_type": event.resolved_provider_event_type,
                "provider_account_id": event.resolved_provider_account_id,
                "provider_invoice_id": event.resolved_provider_invoice_id,
                "status": event.status,
                "unattributed_reason": event.unattributed_reason,
                "paid_at": _iso_or_none(event.paid_at),
                "received_at": _iso_or_none(event.received_at),
                "processed_at": _iso_or_none(event.processed_at),
            }
            for event in payment_event_rows
        ],
        "blocked_billing_cases": [
            {
                "id": str(case.id),
                "booking_id": str(case.booking_id),
                "invoice_id": _string_or_none(case.invoice_id),
                "tid": case.tid,
                "provider": case.resolved_provider,
                "provider_booking_id": case.resolved_provider_booking_id,
                "stripe_account_id": case.stripe_account_id,
                "reason_code": case.reason_code,
                "provider_operation": case.provider_operation,
                "provider_http_status": case.provider_http_status,
                "provider_error_code": case.provider_error_code,
                "status": case.status,
                "first_blocked_at": _iso_or_none(case.first_blocked_at),
                "last_blocked_at": _iso_or_none(case.last_blocked_at),
                "resolved_at": _iso_or_none(case.resolved_at),
                "resolution_code": case.resolution_code,
            }
            for case in blocked_case_rows
        ],
    }
    _emit(payload, as_json=args.json)
    return 0


def _config_summary(
    *,
    settings,
    creator_email: str | None,
    require_live: bool,
) -> dict[str, Any]:
    errors: list[str] = []
    selected_environment_errors: list[str] = []
    selected_environment = settings.paypal_environment_value()
    client_id = settings.selected_paypal_client_id().strip()
    client_secret = settings.selected_paypal_client_secret().strip()
    partner_id = settings.selected_paypal_partner_id().strip()
    webhook_id = settings.selected_paypal_webhook_id().strip()
    redirect_uri = settings.paypal_connect_redirect_uri.strip()

    if require_live and selected_environment != PAYPAL_ENVIRONMENT_LIVE:
        errors.append("paypal_environment must be live for provider-backed PP-14 proof")
    if not client_id:
        selected_environment_errors.append(
            f"selected PayPal client id is not configured for environment={selected_environment}"
        )
    if not client_secret:
        selected_environment_errors.append(
            f"selected PayPal client secret is not configured for environment={selected_environment}"
        )
    if not partner_id:
        selected_environment_errors.append(
            f"selected PayPal partner id is not configured for environment={selected_environment}"
        )
    if not webhook_id:
        selected_environment_errors.append(
            f"selected PayPal webhook id is not configured for environment={selected_environment}"
        )
    if not _is_public_https_url(redirect_uri):
        selected_environment_errors.append(
            "PAYPAL_CONNECT_REDIRECT_URI must be a public https callback URL"
        )
    errors.extend(selected_environment_errors)

    payload = {
        "paypal_environment": selected_environment,
        "selected_api_base_url": settings.selected_paypal_api_base_url(),
        "paypal_api_trace_path": _resolved_trace_path(settings=settings),
        "paypal_connect_redirect_uri": redirect_uri,
        "selected_client_id_configured": bool(client_id),
        "selected_client_secret_configured": bool(client_secret),
        "selected_partner_id_configured": bool(partner_id),
        "selected_webhook_id_configured": bool(webhook_id),
        "connect_redirect_is_public_https": _is_public_https_url(redirect_uri),
        "operator_email_allowlist_count": len(settings.operator_email_allowlist_values()),
        "creator_email_allowlisted": (
            settings.is_operator_email_allowed(_normalized_email(creator_email))
            if creator_email
            else None
        ),
        "ready_for_selected_environment": len(selected_environment_errors) == 0,
        "ready_for_live_proof": (
            selected_environment == PAYPAL_ENVIRONMENT_LIVE and len(errors) == 0
        ),
        "errors": errors,
    }
    return payload


def _create_access_token(
    *,
    user_id: str,
    creator_id: str,
    email: str,
    settings,
) -> str:
    issued_at = datetime.now(UTC)
    expires_at = issued_at + timedelta(hours=settings.jwt_access_token_ttl_hours)
    payload = {
        "sub": user_id,
        "creator_id": creator_id,
        "email": email,
        "iat": issued_at,
        "exp": expires_at,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def _append_path(base_url: str, path: str) -> str:
    normalized_base_url = base_url.rstrip("/")
    normalized_path = path if path.startswith("/") else f"/{path}"
    return f"{normalized_base_url}{normalized_path}"


def _post_json(*, url: str, access_token: str, body: dict[str, Any]) -> dict[str, Any]:
    request = Request(
        url=url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            raw_payload = response.read().decode("utf-8")
    except HTTPError as exc:
        try:
            error_payload = exc.read().decode("utf-8")
        except OSError as inner_exc:
            raise PayPalLiveValidationError(
                f"local route call failed with status {exc.code}"
            ) from inner_exc
        raise PayPalLiveValidationError(error_payload) from exc
    except URLError as exc:
        raise PayPalLiveValidationError("could not reach the local app route") from exc

    if not raw_payload:
        return {}
    try:
        parsed_payload = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise PayPalLiveValidationError("local route returned non-JSON response") from exc
    if not isinstance(parsed_payload, dict):
        raise PayPalLiveValidationError("local route returned unexpected JSON shape")
    return parsed_payload


def _resolved_trace_path(
    *,
    explicit_path: str | None = None,
    settings=None,
) -> str | None:
    if explicit_path is not None:
        cleaned_explicit_path = explicit_path.strip()
        return cleaned_explicit_path or None
    resolved_settings = settings or get_settings()
    cleaned_trace_path = resolved_settings.paypal_api_trace_path.strip()
    return cleaned_trace_path or None


def _is_public_https_url(value: str) -> bool:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc:
        return False
    host = parsed.hostname
    if host is None:
        return False
    normalized_host = host.strip().lower()
    if normalized_host in {"localhost"} or normalized_host.endswith(".localhost"):
        return False
    if normalized_host.startswith("127.") or normalized_host == "::1":
        return False
    return True


def _normalized_email(value: str) -> str:
    return value.strip().lower()


def _slug(value: str) -> str:
    return "".join(character if character.isalnum() else "-" for character in value.strip().lower()).strip("-")


def _parse_uuid(value: str, *, field_name: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise PayPalLiveValidationError(f"{field_name} must be a valid UUID") from exc


def _iso_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json or any(isinstance(value, (dict, list, tuple)) for value in payload.values()):
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    for key, value in payload.items():
        print(f"{key}={value}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PayPalLiveValidationError as exc:
        print(f"error={exc}", file=sys.stderr)
        raise SystemExit(2)
