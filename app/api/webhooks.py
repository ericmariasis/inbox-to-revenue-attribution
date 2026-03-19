from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status

from app.core.config import get_settings
from app.schemas.auth import GenericOkResponse
from app.services.calendly_webhooks import (
    DEFAULT_CALENDLY_WEBHOOK_ROUTER,
    CalendlyWebhookPayloadError,
    CalendlyWebhookRouter,
    CalendlyWebhookVerificationError,
    verify_and_parse_calendly_webhook,
)
from app.services.fullscope_webhooks import (
    DEFAULT_FULLSCOPE_WEBHOOK_ROUTER,
    FullScopeWebhookPayloadError,
    FullScopeWebhookRouter,
    FullScopeWebhookVerificationError,
    verify_and_parse_fullscope_webhook,
)
from app.services.stripe_webhooks import (
    DEFAULT_STRIPE_WEBHOOK_ROUTER,
    StripeWebhookPayloadError,
    StripeWebhookRouter,
    StripeWebhookVerificationError,
    verify_and_parse_stripe_webhook,
)


router = APIRouter(prefix="/webhooks", tags=["webhooks"])
INVALID_CALENDLY_WEBHOOK_SIGNATURE_DETAIL = "invalid calendly webhook signature"
INVALID_CALENDLY_WEBHOOK_PAYLOAD_DETAIL = "invalid calendly webhook payload"
INVALID_FULLSCOPE_WEBHOOK_SIGNATURE_DETAIL = "invalid fullscope webhook authorization"
INVALID_FULLSCOPE_WEBHOOK_PAYLOAD_DETAIL = "invalid fullscope webhook payload"
INVALID_STRIPE_WEBHOOK_SIGNATURE_DETAIL = "invalid stripe webhook signature"
INVALID_STRIPE_WEBHOOK_PAYLOAD_DETAIL = "invalid stripe webhook payload"


def _settings(request: Request):
    return getattr(request.app.state, "settings", get_settings())


def _stripe_webhook_router(request: Request) -> StripeWebhookRouter:
    return getattr(request.app.state, "stripe_webhook_router", DEFAULT_STRIPE_WEBHOOK_ROUTER)


def _calendly_webhook_router(request: Request) -> CalendlyWebhookRouter:
    return getattr(request.app.state, "calendly_webhook_router", DEFAULT_CALENDLY_WEBHOOK_ROUTER)


def _fullscope_webhook_router(request: Request) -> FullScopeWebhookRouter:
    return getattr(request.app.state, "fullscope_webhook_router", DEFAULT_FULLSCOPE_WEBHOOK_ROUTER)


@router.post("/calendly", response_model=GenericOkResponse)
async def calendly_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> GenericOkResponse:
    payload = await request.body()
    signature_header = request.headers.get("calendly-webhook-signature")
    settings = _settings(request)

    try:
        event = verify_and_parse_calendly_webhook(
            payload=payload,
            signature_header=signature_header,
            signing_key=settings.calendly_webhook_signing_key,
            tolerance_seconds=settings.calendly_webhook_tolerance_seconds,
        )
    except CalendlyWebhookVerificationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=INVALID_CALENDLY_WEBHOOK_SIGNATURE_DETAIL,
        ) from exc
    except CalendlyWebhookPayloadError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=INVALID_CALENDLY_WEBHOOK_PAYLOAD_DETAIL,
        ) from exc

    calendly_router = _calendly_webhook_router(request)
    journal_result = calendly_router.record_event(event=event)
    if journal_result.should_schedule_reducer:
        background_tasks.add_task(
            calendly_router.process_event,
            record_id=journal_result.record_id,
        )
    return GenericOkResponse()


@router.post("/fullscope", response_model=GenericOkResponse)
async def fullscope_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> GenericOkResponse:
    payload = await request.body()
    authorization_header = request.headers.get("authorization")
    settings = _settings(request)

    try:
        event = verify_and_parse_fullscope_webhook(
            payload=payload,
            authorization_header=authorization_header,
            shared_secret=settings.fullscope_webhook_shared_secret,
        )
    except FullScopeWebhookVerificationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=INVALID_FULLSCOPE_WEBHOOK_SIGNATURE_DETAIL,
        ) from exc
    except FullScopeWebhookPayloadError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=INVALID_FULLSCOPE_WEBHOOK_PAYLOAD_DETAIL,
        ) from exc

    fullscope_router = _fullscope_webhook_router(request)
    journal_result = fullscope_router.record_event(event=event)
    if journal_result.should_schedule_reducer:
        background_tasks.add_task(
            fullscope_router.process_event,
            record_id=journal_result.record_id,
        )
    return GenericOkResponse()


@router.post("/stripe", response_model=GenericOkResponse)
async def stripe_webhook(request: Request) -> GenericOkResponse:
    payload = await request.body()
    signature_header = request.headers.get("stripe-signature")
    settings = _settings(request)

    try:
        event = verify_and_parse_stripe_webhook(
            payload=payload,
            signature_header=signature_header,
            secret=settings.stripe_webhook_secret,
            tolerance_seconds=settings.stripe_webhook_tolerance_seconds,
        )
    except StripeWebhookVerificationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=INVALID_STRIPE_WEBHOOK_SIGNATURE_DETAIL,
        ) from exc
    except StripeWebhookPayloadError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=INVALID_STRIPE_WEBHOOK_PAYLOAD_DETAIL,
        ) from exc

    _stripe_webhook_router(request).handle_event(event=event)
    return GenericOkResponse()
