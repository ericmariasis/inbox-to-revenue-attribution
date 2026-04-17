from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, HttpUrl


class PayPalConnectStartResponse(BaseModel):
    onboarding_url: HttpUrl
    state: str


class PayPalOrderStartRequest(BaseModel):
    booking_id: UUID


class PayPalOrderStartResponse(BaseModel):
    invoice_id: UUID
    provider_order_id: str
    approval_url: HttpUrl
    state: str


class PayPalOrderCaptureRequest(BaseModel):
    booking_id: UUID
    provider_order_id: str


class PayPalOrderCaptureResponse(BaseModel):
    outcome: Literal["captured", "already_paid"]
    invoice_id: UUID
    provider_order_id: str
    capture_id: str | None
    paid_at: datetime | None
