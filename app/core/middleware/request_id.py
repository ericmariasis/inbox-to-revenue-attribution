import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.request_context import creator_id_ctx, request_id_ctx
from app.services.auth_jwt import decode_access_token_or_none
from app.services.browser_session import get_browser_session_token


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        incoming = request.headers.get("X-Request-Id")
        request_id = incoming.strip() if incoming and incoming.strip() else str(uuid.uuid4())

        request.state.request_id = request_id
        request_token = request_id_ctx.set(request_id)
        creator_id = _creator_id_from_auth_header(request)
        creator_token = creator_id_ctx.set(creator_id)

        try:
            response = await call_next(request)
        finally:
            creator_id_ctx.reset(creator_token)
            request_id_ctx.reset(request_token)

        response.headers["X-Request-Id"] = request_id
        return response


def _creator_id_from_auth_header(request: Request) -> str | None:
    token = _access_token_from_request(request)
    if token is None:
        return None

    payload = decode_access_token_or_none(token)
    if payload is None:
        return None

    creator_id = payload.get("creator_id")
    return str(creator_id) if creator_id else None


def _access_token_from_request(request: Request) -> str | None:
    auth_header = request.headers.get("Authorization")
    if auth_header:
        scheme, _, token = auth_header.partition(" ")
        if scheme.lower() == "bearer" and token:
            return token

    return get_browser_session_token(request)
