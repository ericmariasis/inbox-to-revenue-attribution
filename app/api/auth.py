from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.auth import GenericOkResponse, MagicLinkStartRequest
from app.services.auth_magic_link import start_magic_link

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/magic-link/start", response_model=GenericOkResponse)
def magic_link_start(payload: MagicLinkStartRequest, db: Session = Depends(get_db)) -> GenericOkResponse:
    start_magic_link(db, payload.email)
    return GenericOkResponse()
