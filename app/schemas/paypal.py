from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, HttpUrl, field_validator

from app.services.paypal_order_checkout import PayPalOrderShippingAddress


class PayPalConnectStartResponse(BaseModel):
    onboarding_url: HttpUrl
    state: str


class PayPalOrderShippingAddressInput(BaseModel):
    full_name: str
    address_line_1: str
    address_line_2: str | None = None
    city: str
    state_or_region: str
    postal_code: str
    country_code: str

    @field_validator(
        "full_name",
        "address_line_1",
        "city",
        "state_or_region",
        "postal_code",
        mode="before",
    )
    @classmethod
    def _strip_required_value(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("address_line_2", mode="before")
    @classmethod
    def _strip_optional_value(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    @field_validator("full_name")
    @classmethod
    def _validate_full_name(cls, value: str) -> str:
        if not value:
            raise ValueError("Enter the buyer full name before continuing to PayPal.")
        return value

    @field_validator("address_line_1")
    @classmethod
    def _validate_address_line_1(cls, value: str) -> str:
        if not value:
            raise ValueError("Enter the first shipping address line before continuing to PayPal.")
        return value

    @field_validator("city")
    @classmethod
    def _validate_city(cls, value: str) -> str:
        if not value:
            raise ValueError("Enter the shipping city before continuing to PayPal.")
        return value

    @field_validator("state_or_region")
    @classmethod
    def _validate_state_or_region(cls, value: str) -> str:
        if not value:
            raise ValueError("Enter the shipping state or region before continuing to PayPal.")
        return value

    @field_validator("postal_code")
    @classmethod
    def _validate_postal_code(cls, value: str) -> str:
        if not value:
            raise ValueError("Enter the shipping postal code before continuing to PayPal.")
        return value

    @field_validator("country_code", mode="before")
    @classmethod
    def _normalize_country_code(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @field_validator("country_code")
    @classmethod
    def _validate_country_code(cls, value: str) -> str:
        if len(value) != 2 or not value.isalpha():
            raise ValueError("Enter a valid two-letter shipping country code before continuing to PayPal.")
        return value

    def as_shipping_address(self) -> PayPalOrderShippingAddress:
        return PayPalOrderShippingAddress(
            full_name=self.full_name,
            address_line_1=self.address_line_1,
            address_line_2=self.address_line_2,
            city=self.city,
            state_or_region=self.state_or_region,
            postal_code=self.postal_code,
            country_code=self.country_code,
        )


class PayPalOrderStartRequest(BaseModel):
    booking_id: UUID
    shipping_address: PayPalOrderShippingAddressInput


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
