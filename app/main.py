import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.auth import me_router, router as auth_router
from app.api.booking_links import router as booking_links_router
from app.api.content import router as content_router
from app.api.redirects import router as redirects_router
from app.api.stripe import router as stripe_router
from app.api.webhooks import router as webhooks_router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.middleware.request_id import RequestIDMiddleware
from app.services.click_events import DEFAULT_CLICK_EVENT_PUBLISHER
from app.services.rate_limit import RedirectSoftRateLimiter
from app.services.stripe_provider import build_default_stripe_provider
from app.services.stripe_webhooks import DEFAULT_STRIPE_WEBHOOK_ROUTER

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.settings = get_settings()
    if not hasattr(app.state, "click_event_publisher"):
        app.state.click_event_publisher = DEFAULT_CLICK_EVENT_PUBLISHER
    if not hasattr(app.state, "redirect_rate_limiter"):
        app.state.redirect_rate_limiter = RedirectSoftRateLimiter()
    if not hasattr(app.state, "stripe_provider"):
        app.state.stripe_provider = build_default_stripe_provider(settings=app.state.settings)
    if not hasattr(app.state, "stripe_webhook_router"):
        app.state.stripe_webhook_router = DEFAULT_STRIPE_WEBHOOK_ROUTER
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(RequestIDMiddleware)
app.include_router(auth_router)
app.include_router(me_router)
app.include_router(booking_links_router)
app.include_router(content_router)
app.include_router(redirects_router)
app.include_router(stripe_router)
app.include_router(webhooks_router)


@app.get("/health")
def health():
    logger.info("health_check")
    return {"status": "ok"}
