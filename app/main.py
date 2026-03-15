import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.auth import me_router, router as auth_router
from app.api.booking_links import router as booking_links_router
from app.api.content import router as content_router
from app.api.reports import router as reports_router
from app.api.redirects import router as redirects_router
from app.api.stripe import router as stripe_router
from app.api.ui import router as ui_router
from app.api.webhooks import router as webhooks_router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.middleware.request_id import RequestIDMiddleware
from app.services.click_events import DEFAULT_CLICK_EVENT_PUBLISHER
from app.services.calendly_webhooks import build_default_calendly_webhook_router
from app.services.content_fetch import build_default_content_fetch_provider
from app.services.email_provider import build_default_email_provider
from app.services.stripe_provider import build_default_stripe_provider
from app.services.stripe_webhooks import DEFAULT_STRIPE_WEBHOOK_ROUTER

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.validate_runtime()
    app.state.settings = settings
    if not hasattr(app.state, "click_event_publisher"):
        app.state.click_event_publisher = DEFAULT_CLICK_EVENT_PUBLISHER
    if not getattr(app.state, "_email_provider_overridden", False):
        app.state.email_provider = build_default_email_provider(settings=app.state.settings)
    if not hasattr(app.state, "content_fetch_provider"):
        app.state.content_fetch_provider = build_default_content_fetch_provider()
    if not hasattr(app.state, "stripe_provider"):
        app.state.stripe_provider = build_default_stripe_provider(settings=app.state.settings)
    if not hasattr(app.state, "stripe_webhook_router"):
        app.state.stripe_webhook_router = DEFAULT_STRIPE_WEBHOOK_ROUTER
    if not hasattr(app.state, "calendly_webhook_router"):
        app.state.calendly_webhook_router = build_default_calendly_webhook_router(
            provider=app.state.stripe_provider
        )
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(RequestIDMiddleware)
app.include_router(ui_router)
app.include_router(auth_router)
app.include_router(me_router)
app.include_router(booking_links_router)
app.include_router(content_router)
app.include_router(reports_router)
app.include_router(redirects_router)
app.include_router(stripe_router)
app.include_router(webhooks_router)


@app.get("/health")
def health():
    logger.info("health_check")
    return {"status": "ok"}
