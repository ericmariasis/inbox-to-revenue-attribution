import logging

from fastapi import APIRouter, Depends, Request

from app.api.deps import get_current_auth_user
from app.models.auth_user import AuthUser
from app.schemas.stripe import StripeConnectStartResponse
from app.services.stripe_connect import build_stripe_connect_state
from app.services.stripe_provider import StripeProvider, build_default_stripe_provider

router = APIRouter(prefix="/stripe", tags=["stripe"])
logger = logging.getLogger(__name__)


def _stripe_provider(request: Request) -> StripeProvider:
    return getattr(request.app.state, "stripe_provider", build_default_stripe_provider())


@router.post("/connect/start", response_model=StripeConnectStartResponse)
def stripe_connect_start(
    request: Request,
    current_user: AuthUser = Depends(get_current_auth_user),
) -> StripeConnectStartResponse:
    state = build_stripe_connect_state(creator_id=str(current_user.creator_id))
    onboarding_url = _stripe_provider(request).build_connect_onboarding_url(
        creator_id=str(current_user.creator_id),
        state=state,
    )
    logger.info("stripe_connect_start_created")
    return StripeConnectStartResponse(
        onboarding_url=onboarding_url,
        state=state,
    )
