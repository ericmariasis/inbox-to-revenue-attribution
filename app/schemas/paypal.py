from pydantic import BaseModel, HttpUrl


class PayPalConnectStartResponse(BaseModel):
    onboarding_url: HttpUrl
    state: str
