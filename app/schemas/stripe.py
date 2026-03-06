from pydantic import BaseModel, HttpUrl


class StripeConnectStartResponse(BaseModel):
    onboarding_url: HttpUrl
    state: str
