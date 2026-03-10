from dataclasses import dataclass
import re
import unicodedata


CONTENT_TOPIC_REVIEW_STATUS_PENDING = "pending"
CONTENT_TOPIC_REVIEW_STATUS_CONFIRMED = "confirmed"
CONTENT_TOPIC_REVIEW_STATUS_REJECTED = "rejected"

MAX_CONTENT_TOPIC_LABEL_LENGTH = 255
MAX_CONTENT_TOPIC_CANDIDATES = 5

_TITLE_SEGMENT_PATTERN = re.compile(r"\s*(?::|\||/| - )\s*")
_TOPIC_STOPWORDS = frozenset(
    {
        "a",
        "about",
        "after",
        "again",
        "against",
        "all",
        "am",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "because",
        "been",
        "before",
        "being",
        "below",
        "between",
        "both",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "down",
        "during",
        "each",
        "few",
        "for",
        "from",
        "further",
        "had",
        "has",
        "have",
        "having",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "just",
        "more",
        "most",
        "no",
        "nor",
        "not",
        "of",
        "off",
        "on",
        "once",
        "only",
        "or",
        "other",
        "our",
        "out",
        "over",
        "same",
        "should",
        "so",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "too",
        "under",
        "until",
        "up",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "why",
        "will",
        "with",
        "without",
        "would",
        "you",
        "your",
    }
)


@dataclass(frozen=True)
class ContentTopicSuggestion:
    suggested_label: str
    normalized_label: str
    suggestion_method: str


def normalize_topic_label_display(raw_label: str) -> str:
    normalized = " ".join(unicodedata.normalize("NFKC", raw_label or "").split())
    if not normalized:
        raise ValueError("Enter a topic label before saving.")
    if len(normalized) > MAX_CONTENT_TOPIC_LABEL_LENGTH:
        normalized = normalized[:MAX_CONTENT_TOPIC_LABEL_LENGTH].rstrip()
    if not normalized:
        raise ValueError("Enter a topic label before saving.")
    return normalized


def normalize_topic_label(raw_label: str) -> str:
    display_label = normalize_topic_label_display(raw_label)
    folded = unicodedata.normalize("NFKC", display_label).casefold().replace("&", " and ")
    normalized = " ".join(
        "".join(char if char.isalnum() else " " for char in folded).split()
    )
    if not normalized:
        raise ValueError("Enter a topic label before saving.")
    if len(normalized) > MAX_CONTENT_TOPIC_LABEL_LENGTH:
        normalized = normalized[:MAX_CONTENT_TOPIC_LABEL_LENGTH].rstrip()
    if not normalized:
        raise ValueError("Enter a topic label before saving.")
    return normalized


def build_content_topic_suggestions(
    *,
    title: str | None,
    extracted_text: str | None,
) -> list[ContentTopicSuggestion]:
    suggestions: list[ContentTopicSuggestion] = []
    seen_normalized_labels: set[str] = set()

    def add_suggestion(*, label: str | None, method: str) -> None:
        if label is None or len(suggestions) >= MAX_CONTENT_TOPIC_CANDIDATES:
            return
        try:
            display_label = normalize_topic_label_display(label)
            normalized_label = normalize_topic_label(display_label)
        except ValueError:
            return
        if normalized_label in seen_normalized_labels:
            return
        suggestions.append(
            ContentTopicSuggestion(
                suggested_label=display_label,
                normalized_label=normalized_label,
                suggestion_method=method,
            )
        )
        seen_normalized_labels.add(normalized_label)

    if title:
        if len(title) <= 80:
            add_suggestion(label=title, method="title_full")
        else:
            add_suggestion(label=_keywords_topic_label(title), method="title_keywords")

        for segment in _TITLE_SEGMENT_PATTERN.split(title):
            if len(suggestions) >= MAX_CONTENT_TOPIC_CANDIDATES:
                break
            add_suggestion(
                label=segment if len(segment) <= 80 else _keywords_topic_label(segment),
                method="title_segment",
            )

    if extracted_text:
        for line in extracted_text.splitlines():
            if len(suggestions) >= MAX_CONTENT_TOPIC_CANDIDATES:
                break
            add_suggestion(label=_keywords_topic_label(line), method="text_keywords")

        if len(suggestions) < MAX_CONTENT_TOPIC_CANDIDATES:
            add_suggestion(label=_keywords_topic_label(extracted_text), method="text_keywords")

    return suggestions


def _keywords_topic_label(raw_text: str) -> str | None:
    tokens = _topic_tokens(raw_text)
    if len(tokens) < 2:
        return None
    return " ".join(token.title() for token in tokens[:4])


def _topic_tokens(raw_text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", raw_text or "")
    token_chars: list[str] = []
    tokens: list[str] = []

    def flush_current_token() -> None:
        if not token_chars:
            return
        token = "".join(token_chars).casefold()
        token_chars.clear()
        if len(token) < 2:
            return
        if token in _TOPIC_STOPWORDS:
            return
        tokens.append(token)

    for character in normalized:
        if character.isalnum():
            token_chars.append(character)
            continue
        flush_current_token()
    flush_current_token()
    return tokens
