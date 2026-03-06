from typing import Protocol
from urllib.parse import urlencode

from app.core.config import Settings, get_settings


class StripeProviderError(ValueError):
    pass


class StripeProvider(Protocol):
    def build_connect_onboarding_url(self, *, creator_id: str, state: str) -> str: ...

    def exchange_connect_callback(self, *, code: str, state: str) -> str: ...


class StripeOAuthProvider:
    def __init__(
        self,
        *,
        authorize_url: str,
        client_id: str,
        redirect_uri: str,
    ):
        self._authorize_url = authorize_url.rstrip("?")
        self._client_id = client_id
        self._redirect_uri = redirect_uri

    def build_connect_onboarding_url(self, *, creator_id: str, state: str) -> str:
        del creator_id
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._client_id,
                "scope": "read_write",
                "state": state,
                "redirect_uri": self._redirect_uri,
            }
        )
        return f"{self._authorize_url}?{query}"

    def exchange_connect_callback(self, *, code: str, state: str) -> str:
        del code, state
        raise StripeProviderError("default stripe callback exchange is not implemented")


def build_default_stripe_provider(*, settings: Settings | None = None) -> StripeProvider:
    resolved_settings = settings or get_settings()
    return StripeOAuthProvider(
        authorize_url=resolved_settings.stripe_connect_authorize_url,
        client_id=resolved_settings.stripe_connect_client_id,
        redirect_uri=resolved_settings.stripe_connect_redirect_uri,
    )
