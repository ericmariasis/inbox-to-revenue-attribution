from datetime import datetime

from pydantic import BaseModel, EmailStr


class MagicLinkStartRequest(BaseModel):
    email: EmailStr


class GenericOkResponse(BaseModel):
    status: str = "ok"


class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class MeResponse(BaseModel):
    id: str
    email: EmailStr
    name: str
    billing_provider: str
    billing_connect_status: str
    billing_account_id: str | None = None
    billing_connected_at: datetime | None = None
    stripe_connect_status: str
    stripe_account_id: str | None = None
    stripe_connected_at: datetime | None = None
