from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.core.config import Settings, get_settings
from app.db.session import get_db
from app.models.auth_user import AuthUser
from app.services.browser_session import get_browser_session_token
from app.services.auth_jwt import decode_access_token

bearer_scheme = HTTPBearer(auto_error=False)


def _get_auth_user_from_access_token(*, access_token: str, db: Session) -> AuthUser:
    try:
        payload = decode_access_token(access_token)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token",
        ) from exc

    user_id = payload.get("sub")
    creator_id = payload.get("creator_id")
    email = payload.get("email")
    if not user_id or not creator_id or not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token",
        )

    user = db.execute(
        select(AuthUser)
        .options(joinedload(AuthUser.creator))
        .where(
            AuthUser.id == user_id,
            AuthUser.creator_id == creator_id,
            AuthUser.email == email,
        )
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token",
        )

    return user


def get_current_auth_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> AuthUser:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not authenticated",
        )

    return _get_auth_user_from_access_token(access_token=credentials.credentials, db=db)


def get_optional_browser_auth_user(
    request: Request,
    db: Session = Depends(get_db),
) -> AuthUser | None:
    access_token = get_browser_session_token(request)
    if access_token is None:
        return None

    try:
        return _get_auth_user_from_access_token(access_token=access_token, db=db)
    except HTTPException:
        return None


def browser_auth_user_is_allowlisted_operator(
    user: AuthUser | None,
    *,
    settings: Settings | None = None,
) -> bool:
    if user is None:
        return False

    resolved_settings = settings or get_settings()
    return resolved_settings.is_operator_email_allowed(user.email)
