import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.auth import me_router, router as auth_router
from app.api.booking_links import router as booking_links_router
from app.api.content import router as content_router
from app.api.redirects import router as redirects_router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.middleware.request_id import RequestIDMiddleware
from app.services.click_events import DEFAULT_CLICK_EVENT_PUBLISHER
from app.services.rate_limit import RedirectSoftRateLimiter

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.settings = get_settings()
    if not hasattr(app.state, "click_event_publisher"):
        app.state.click_event_publisher = DEFAULT_CLICK_EVENT_PUBLISHER
    if not hasattr(app.state, "redirect_rate_limiter"):
        app.state.redirect_rate_limiter = RedirectSoftRateLimiter()
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(RequestIDMiddleware)
app.include_router(auth_router)
app.include_router(me_router)
app.include_router(booking_links_router)
app.include_router(content_router)
app.include_router(redirects_router)


@app.get("/health")
def health():
    logger.info("health_check")
    return {"status": "ok"}
