import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.auth import me_router, router as auth_router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.middleware.request_id import RequestIDMiddleware

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.settings = get_settings()
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(RequestIDMiddleware)
app.include_router(auth_router)
app.include_router(me_router)


@app.get("/health")
def health():
    logger.info("health_check")
    return {"status": "ok"}
