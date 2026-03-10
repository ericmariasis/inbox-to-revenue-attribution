from dataclasses import dataclass
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


CONTENT_FETCH_STATUS_SUCCEEDED = "succeeded"
CONTENT_FETCH_STATUS_FAILED = "failed"
CONTENT_FETCH_FAILURE_HTTP_ERROR = "HTTP_ERROR"
CONTENT_FETCH_FAILURE_NETWORK_ERROR = "NETWORK_ERROR"
CONTENT_FETCH_FAILURE_UNSUPPORTED_CONTENT_TYPE = "UNSUPPORTED_CONTENT_TYPE"
CONTENT_FETCH_FAILURE_RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
CONTENT_FETCH_FAILURE_EMPTY_BODY = "EMPTY_BODY"
SUPPORTED_FETCH_CONTENT_TYPES = frozenset({"application/xhtml+xml", "text/html", "text/plain"})


@dataclass(frozen=True)
class ContentFetchSuccess:
    fetched_url: str
    http_status: int
    response_content_type: str | None
    response_content_charset: str | None
    snapshot_text: str
    fetch_status: str = CONTENT_FETCH_STATUS_SUCCEEDED
    failure_reason_code: str | None = None
    failure_detail: str | None = None


@dataclass(frozen=True)
class ContentFetchFailure:
    reason_code: str
    detail: str
    fetched_url: str | None = None
    http_status: int | None = None
    response_content_type: str | None = None
    response_content_charset: str | None = None
    fetch_status: str = CONTENT_FETCH_STATUS_FAILED
    snapshot_text: str | None = None


ContentFetchResult = ContentFetchSuccess | ContentFetchFailure


class ContentFetchProvider(Protocol):
    def fetch_public_url(self, *, source_url: str) -> ContentFetchResult: ...


class UrllibContentFetchProvider:
    def __init__(
        self,
        *,
        timeout_seconds: int = 10,
        max_bytes: int = 1_000_000,
        user_agent: str = "Creator Compass Content Fetcher/1.0",
    ):
        self._timeout_seconds = timeout_seconds
        self._max_bytes = max_bytes
        self._user_agent = user_agent

    def fetch_public_url(self, *, source_url: str) -> ContentFetchResult:
        request = Request(
            source_url,
            headers={
                "Accept": "text/html,text/plain;q=0.9,*/*;q=0.1",
                "User-Agent": self._user_agent,
            },
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                raw_body = response.read(self._max_bytes + 1)
                content_type = _content_type_from_headers(response.headers)
                content_charset = _content_charset_from_headers(response.headers)
                fetched_url = response.geturl()
                http_status = response.getcode()
        except HTTPError as exc:
            return ContentFetchFailure(
                fetched_url=exc.url,
                http_status=exc.code,
                reason_code=CONTENT_FETCH_FAILURE_HTTP_ERROR,
                detail=f"Fetch returned HTTP {exc.code}.",
                response_content_type=_content_type_from_headers(exc.headers),
                response_content_charset=_content_charset_from_headers(exc.headers),
            )
        except URLError as exc:
            return ContentFetchFailure(
                reason_code=CONTENT_FETCH_FAILURE_NETWORK_ERROR,
                detail=_network_error_detail(exc),
            )

        if content_type and content_type not in SUPPORTED_FETCH_CONTENT_TYPES:
            return ContentFetchFailure(
                fetched_url=fetched_url,
                http_status=http_status,
                reason_code=CONTENT_FETCH_FAILURE_UNSUPPORTED_CONTENT_TYPE,
                detail=f"Expected HTML or text content, got {content_type}.",
                response_content_type=content_type,
                response_content_charset=content_charset,
            )

        if len(raw_body) > self._max_bytes:
            return ContentFetchFailure(
                fetched_url=fetched_url,
                http_status=http_status,
                reason_code=CONTENT_FETCH_FAILURE_RESPONSE_TOO_LARGE,
                detail=f"Response exceeded the {self._max_bytes}-byte fetch limit.",
                response_content_type=content_type,
                response_content_charset=content_charset,
            )

        resolved_charset = content_charset or "utf-8"
        snapshot_text = raw_body.decode(resolved_charset, errors="replace")
        if not snapshot_text.strip():
            return ContentFetchFailure(
                fetched_url=fetched_url,
                http_status=http_status,
                reason_code=CONTENT_FETCH_FAILURE_EMPTY_BODY,
                detail="Response body was empty after decoding.",
                response_content_type=content_type,
                response_content_charset=content_charset,
            )

        return ContentFetchSuccess(
            fetched_url=fetched_url,
            http_status=http_status,
            response_content_type=content_type,
            response_content_charset=content_charset,
            snapshot_text=snapshot_text,
        )


def build_default_content_fetch_provider() -> ContentFetchProvider:
    return UrllibContentFetchProvider()


def _content_type_from_headers(headers) -> str | None:
    if headers is None or not hasattr(headers, "get_content_type"):
        return None
    value = headers.get_content_type()
    if not isinstance(value, str) or not value:
        return None
    return value.lower()


def _content_charset_from_headers(headers) -> str | None:
    if headers is None or not hasattr(headers, "get_content_charset"):
        return None
    value = headers.get_content_charset()
    if not isinstance(value, str) or not value:
        return None
    return value.lower()


def _network_error_detail(exc: URLError) -> str:
    reason = getattr(exc, "reason", None)
    if reason:
        return f"Network error: {reason}"
    return "Network error prevented the fetch."
