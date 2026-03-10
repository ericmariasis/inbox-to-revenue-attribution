from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Iterable

from app.models.content_fetch_snapshot import ContentFetchSnapshot
from app.services.content_fetch import CONTENT_FETCH_STATUS_SUCCEEDED


CONTENT_EXTRACTION_STATUS_SUCCEEDED = "succeeded"
CONTENT_EXTRACTION_STATUS_LOW_CONFIDENCE = "low_confidence"
CONTENT_EXTRACTION_STATUS_FAILED = "failed"

CONTENT_EXTRACTION_REASON_SOURCE_FETCH_FAILED = "SOURCE_FETCH_FAILED"
CONTENT_EXTRACTION_REASON_SNAPSHOT_TEXT_MISSING = "SNAPSHOT_TEXT_MISSING"
CONTENT_EXTRACTION_REASON_NO_USABLE_TEXT = "NO_USABLE_TEXT"
CONTENT_EXTRACTION_REASON_TEXT_TOO_SHORT = "TEXT_TOO_SHORT"
CONTENT_EXTRACTION_REASON_UNSUPPORTED_SNAPSHOT_TYPE = "UNSUPPORTED_SNAPSHOT_TYPE"

LOW_CONFIDENCE_MIN_WORD_COUNT = 30
HTML_SNAPSHOT_CONTENT_TYPES = frozenset({"application/xhtml+xml", "text/html"})
PLAIN_TEXT_SNAPSHOT_CONTENT_TYPES = frozenset({"text/plain"})
_META_TITLE_KEYS = ("og:title", "twitter:title", "title")
_META_PUBLISHED_AT_KEYS = (
    "article:published_time",
    "article:published",
    "og:article:published_time",
    "publish-date",
    "publish_date",
    "published_time",
    "pubdate",
    "date",
    "dc.date",
    "parsely-pub-date",
    "datepublished",
)


@dataclass(frozen=True)
class ContentExtractionResult:
    extraction_status: str
    extraction_reason_code: str | None
    extraction_detail: str | None
    extraction_method: str | None
    title: str | None
    published_at: datetime | None
    published_at_raw: str | None
    source_text_char_count: int
    extracted_text_char_count: int
    extracted_text_word_count: int
    extracted_text: str | None


def extract_content_from_snapshot(snapshot: ContentFetchSnapshot) -> ContentExtractionResult:
    source_text = snapshot.snapshot_text or ""
    source_text_char_count = len(source_text)

    if snapshot.fetch_status != CONTENT_FETCH_STATUS_SUCCEEDED:
        reason_code = snapshot.failure_reason_code or CONTENT_EXTRACTION_REASON_SOURCE_FETCH_FAILED
        detail = snapshot.failure_detail or (
            f"Cannot extract from fetch snapshot with status {snapshot.fetch_status}."
        )
        return ContentExtractionResult(
            extraction_status=CONTENT_EXTRACTION_STATUS_FAILED,
            extraction_reason_code=reason_code,
            extraction_detail=detail,
            extraction_method=None,
            title=None,
            published_at=None,
            published_at_raw=None,
            source_text_char_count=source_text_char_count,
            extracted_text_char_count=0,
            extracted_text_word_count=0,
            extracted_text=None,
        )

    if not source_text.strip():
        return ContentExtractionResult(
            extraction_status=CONTENT_EXTRACTION_STATUS_FAILED,
            extraction_reason_code=CONTENT_EXTRACTION_REASON_SNAPSHOT_TEXT_MISSING,
            extraction_detail="Fetch snapshot text is empty.",
            extraction_method=None,
            title=None,
            published_at=None,
            published_at_raw=None,
            source_text_char_count=source_text_char_count,
            extracted_text_char_count=0,
            extracted_text_word_count=0,
            extracted_text=None,
        )

    content_type = (snapshot.response_content_type or "text/html").lower()
    if content_type in HTML_SNAPSHOT_CONTENT_TYPES:
        parsed = _extract_from_html(source_text)
        return _finalize_extraction_result(
            source_text_char_count=source_text_char_count,
            extraction_method=parsed.extraction_method,
            title=parsed.title,
            published_at_raw=parsed.published_at_raw,
            extracted_text=parsed.extracted_text,
        )
    if content_type in PLAIN_TEXT_SNAPSHOT_CONTENT_TYPES:
        extracted_text = _normalize_text_blocks([source_text])
        return _finalize_extraction_result(
            source_text_char_count=source_text_char_count,
            extraction_method="plain_text_full_body",
            title=None,
            published_at_raw=None,
            extracted_text=extracted_text,
        )

    return ContentExtractionResult(
        extraction_status=CONTENT_EXTRACTION_STATUS_FAILED,
        extraction_reason_code=CONTENT_EXTRACTION_REASON_UNSUPPORTED_SNAPSHOT_TYPE,
        extraction_detail=f"Unsupported snapshot content type {content_type}.",
        extraction_method=None,
        title=None,
        published_at=None,
        published_at_raw=None,
        source_text_char_count=source_text_char_count,
        extracted_text_char_count=0,
        extracted_text_word_count=0,
        extracted_text=None,
    )


@dataclass(frozen=True)
class _ParsedHtmlExtraction:
    extraction_method: str | None
    title: str | None
    published_at_raw: str | None
    extracted_text: str | None


def _extract_from_html(snapshot_text: str) -> _ParsedHtmlExtraction:
    parser = _ContentHTMLParser()
    parser.feed(snapshot_text)
    parser.close()

    article_text = _normalize_text_blocks(parser.article_blocks)
    main_text = _normalize_text_blocks(parser.main_blocks)
    body_text = _normalize_text_blocks(parser.body_blocks)

    if article_text:
        extraction_method = "html_article"
        extracted_text = article_text
    elif main_text:
        extraction_method = "html_main"
        extracted_text = main_text
    else:
        extraction_method = "html_body"
        extracted_text = body_text

    return _ParsedHtmlExtraction(
        extraction_method=extraction_method if extracted_text else None,
        title=_first_non_empty(parser.meta_value(key) for key in _META_TITLE_KEYS) or parser.title_text,
        published_at_raw=_first_non_empty(
            parser.meta_value(key) for key in _META_PUBLISHED_AT_KEYS
        )
        or _first_non_empty(parser.time_datetimes),
        extracted_text=extracted_text,
    )


def _finalize_extraction_result(
    *,
    source_text_char_count: int,
    extraction_method: str | None,
    title: str | None,
    published_at_raw: str | None,
    extracted_text: str | None,
) -> ContentExtractionResult:
    normalized_title = _normalize_optional_text(title)
    normalized_published_at_raw = _normalize_optional_text(published_at_raw)
    normalized_extracted_text = _normalize_optional_multiline_text(extracted_text)

    if normalized_extracted_text is None:
        return ContentExtractionResult(
            extraction_status=CONTENT_EXTRACTION_STATUS_FAILED,
            extraction_reason_code=CONTENT_EXTRACTION_REASON_NO_USABLE_TEXT,
            extraction_detail="No usable text could be extracted from the fetch snapshot.",
            extraction_method=extraction_method,
            title=normalized_title,
            published_at=None,
            published_at_raw=normalized_published_at_raw,
            source_text_char_count=source_text_char_count,
            extracted_text_char_count=0,
            extracted_text_word_count=0,
            extracted_text=None,
        )

    extracted_text_char_count = len(normalized_extracted_text)
    extracted_text_word_count = _word_count(normalized_extracted_text)
    published_at = (
        _parse_datetime_like_value(normalized_published_at_raw)
        if normalized_published_at_raw is not None
        else None
    )

    if extracted_text_word_count < LOW_CONFIDENCE_MIN_WORD_COUNT:
        return ContentExtractionResult(
            extraction_status=CONTENT_EXTRACTION_STATUS_LOW_CONFIDENCE,
            extraction_reason_code=CONTENT_EXTRACTION_REASON_TEXT_TOO_SHORT,
            extraction_detail=(
                "Extracted text was too short to trust as a clean content artifact "
                f"({extracted_text_word_count} words)."
            ),
            extraction_method=extraction_method,
            title=normalized_title,
            published_at=published_at,
            published_at_raw=normalized_published_at_raw,
            source_text_char_count=source_text_char_count,
            extracted_text_char_count=extracted_text_char_count,
            extracted_text_word_count=extracted_text_word_count,
            extracted_text=normalized_extracted_text,
        )

    return ContentExtractionResult(
        extraction_status=CONTENT_EXTRACTION_STATUS_SUCCEEDED,
        extraction_reason_code=None,
        extraction_detail=None,
        extraction_method=extraction_method,
        title=normalized_title,
        published_at=published_at,
        published_at_raw=normalized_published_at_raw,
        source_text_char_count=source_text_char_count,
        extracted_text_char_count=extracted_text_char_count,
        extracted_text_word_count=extracted_text_word_count,
        extracted_text=normalized_extracted_text,
    )


class _ContentHTMLParser(HTMLParser):
    _BLOCK_TAGS = frozenset(
        {
            "article",
            "aside",
            "blockquote",
            "br",
            "dd",
            "div",
            "dl",
            "dt",
            "figcaption",
            "footer",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "header",
            "li",
            "main",
            "ol",
            "p",
            "pre",
            "section",
            "table",
            "td",
            "th",
            "tr",
            "ul",
        }
    )
    _IGNORED_TAGS = frozenset({"script", "style", "noscript", "svg", "template"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._ignore_depth = 0
        self._in_title = False
        self._in_body = False
        self._article_depth = 0
        self._main_depth = 0
        self._title_fragments: list[str] = []
        self._meta_values: dict[str, str] = {}
        self.time_datetimes: list[str] = []
        self.article_blocks: list[str] = []
        self.main_blocks: list[str] = []
        self.body_blocks: list[str] = []

    @property
    def title_text(self) -> str | None:
        return _normalize_optional_text("".join(self._title_fragments))

    def meta_value(self, key: str) -> str | None:
        return self._meta_values.get(key)

    def handle_starttag(self, tag: str, attrs) -> None:
        normalized_tag = tag.lower()
        attributes = {key.lower(): value for key, value in attrs if key}

        if normalized_tag in self._IGNORED_TAGS:
            self._ignore_depth += 1
            return

        if normalized_tag == "title":
            self._in_title = True
            return

        if normalized_tag == "body":
            self._in_body = True
        elif normalized_tag == "article":
            self._article_depth += 1
        elif normalized_tag == "main":
            self._main_depth += 1

        if normalized_tag == "meta":
            self._remember_meta_value(attributes)
        elif normalized_tag == "time":
            raw_datetime = _normalize_optional_text(attributes.get("datetime"))
            if raw_datetime is not None:
                self.time_datetimes.append(raw_datetime)

        if normalized_tag in self._BLOCK_TAGS:
            self._append_block_break()

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.lower()
        if normalized_tag in self._IGNORED_TAGS:
            if self._ignore_depth > 0:
                self._ignore_depth -= 1
            return

        if normalized_tag == "title":
            self._in_title = False
            return

        if normalized_tag == "body":
            self._in_body = False
        elif normalized_tag == "article" and self._article_depth > 0:
            self._article_depth -= 1
        elif normalized_tag == "main" and self._main_depth > 0:
            self._main_depth -= 1

        if normalized_tag in self._BLOCK_TAGS:
            self._append_block_break()

    def handle_data(self, data: str) -> None:
        if self._ignore_depth > 0:
            return

        if self._in_title:
            self._title_fragments.append(data)

        if not self._in_body:
            return

        self.body_blocks.append(data)
        if self._article_depth > 0:
            self.article_blocks.append(data)
        elif self._main_depth > 0:
            self.main_blocks.append(data)

    def _append_block_break(self) -> None:
        if self._in_body:
            self.body_blocks.append("\n")
        if self._article_depth > 0:
            self.article_blocks.append("\n")
        elif self._main_depth > 0:
            self.main_blocks.append("\n")

    def _remember_meta_value(self, attributes: dict[str, str | None]) -> None:
        content = _normalize_optional_text(attributes.get("content"))
        if content is None:
            return

        for key_name in ("property", "name", "itemprop"):
            raw_key = _normalize_optional_text(attributes.get(key_name))
            if raw_key is None:
                continue
            normalized_key = raw_key.lower()
            self._meta_values.setdefault(normalized_key, content)


def _normalize_text_blocks(blocks: Iterable[str]) -> str | None:
    joined = "".join(blocks)
    if not joined.strip():
        return None

    lines: list[str] = []
    for raw_line in joined.splitlines():
        normalized_line = " ".join(raw_line.split())
        if not normalized_line:
            continue
        if not lines or lines[-1] != normalized_line:
            lines.append(normalized_line)

    if not lines:
        return None
    return "\n\n".join(lines)


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split())
    if not normalized:
        return None
    return normalized


def _normalize_optional_multiline_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return normalized


def _parse_datetime_like_value(raw_value: str) -> datetime | None:
    normalized = raw_value.strip()
    candidates = [normalized]
    if normalized.endswith("Z"):
        candidates.append(f"{normalized[:-1]}+00:00")

    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        return _coerce_timezone(parsed)

    for fmt in (
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
        "%B %d, %Y",
        "%b %d, %Y",
    ):
        try:
            parsed = datetime.strptime(normalized, fmt)
        except ValueError:
            continue
        return _coerce_timezone(parsed)

    return None


def _coerce_timezone(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _first_non_empty(values: Iterable[str | None]) -> str | None:
    for value in values:
        normalized = _normalize_optional_text(value)
        if normalized is not None:
            return normalized
    return None


def _word_count(text: str) -> int:
    return len(text.split())
