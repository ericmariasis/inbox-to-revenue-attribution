from pydantic import BaseModel, EmailStr


class MagicLinkStartRequest(BaseModel):
    email: EmailStr


class GenericOkResponse(BaseModel):
    status: str = "ok"
