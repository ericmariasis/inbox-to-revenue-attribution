from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from app.api.content import (
    create_content_extraction_artifact_response_for_creator,
    create_content_topic_candidates_response_for_creator,
)
from app.models.booking_link import BookingLink
from app.models.content import Content
from app.models.content_confirmed_topic import ContentConfirmedTopic
from app.models.content_fetch_snapshot import ContentFetchSnapshot
from app.models.creator import Creator
from app.services.content_topics import normalize_topic_label, normalize_topic_label_display

DEFAULT_STORY64_DATASET_PATH = (
    Path(__file__).resolve().parent / "datasets" / "story64_seed_dataset.json"
)

EXTRACTION_PASS_THRESHOLD = 0.85
TOPIC_SUGGESTION_PASS_THRESHOLD = 0.75
PARTIAL_TOPIC_CONTAINMENT_SCORE = 0.75
PARTIAL_TOPIC_TOKEN_OVERLAP_SCORE = 0.5

EVAL_RUBRIC = {
    "version": "story64-v1",
    "extraction_quality": {
        "pass_threshold": EXTRACTION_PASS_THRESHOLD,
        "checks": [
            "status must match the expected canonical extraction outcome",
            "reason code must match when the case expects a non-success reason",
            "title and published_at_raw must match when explicitly expected",
            "required phrases must remain in extracted_text",
            "forbidden phrases should stay out of extracted_text",
            "word count must clear the case minimum when one is specified",
        ],
    },
    "topic_suggestion_quality": {
        "pass_threshold": TOPIC_SUGGESTION_PASS_THRESHOLD,
        "checks": [
            "candidate coverage is scored against canonical confirmed topics",
            "exact normalized topic match scores 1.0",
            "normalized containment scores 0.75",
            "token overlap on at least half of the expected topic tokens scores 0.5",
            "known boilerplate or junk labels should stay out of the candidate set",
        ],
    },
}


class Story64SnapshotSeed(BaseModel):
    requested_url: str
    fetched_url: str | None
    fetch_status: str
    http_status: int | None
    response_content_type: str | None = "text/html"
    response_content_charset: str | None = "utf-8"
    snapshot_text: str | None = None
    failure_reason_code: str | None = None
    failure_detail: str | None = None


class Story64ExtractionExpectation(BaseModel):
    status: str
    reason_code: str | None = None
    title: str | None = None
    published_at_raw: str | None = None
    min_word_count: int | None = None
    required_phrases: list[str] = Field(default_factory=list)
    forbidden_phrases: list[str] = Field(default_factory=list)


class Story64TopicExpectation(BaseModel):
    confirmed_topics: list[str] = Field(default_factory=list)
    forbidden_suggestions: list[str] = Field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return bool(self.confirmed_topics or self.forbidden_suggestions)


class Story64EvalCase(BaseModel):
    case_id: str
    title: str
    source_url: str
    snapshot: Story64SnapshotSeed
    expected_extraction: Story64ExtractionExpectation
    expected_topics: Story64TopicExpectation | None = None


class Story64EvalDataset(BaseModel):
    dataset_name: str
    dataset_version: str
    description: str
    cases: list[Story64EvalCase]

    @model_validator(mode="after")
    def _validate_unique_case_ids(self) -> "Story64EvalDataset":
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("Story 64 eval dataset case_id values must be unique")
        return self


def load_story64_seed_dataset(path: str | Path | None = None) -> Story64EvalDataset:
    dataset_path = Path(path) if path is not None else DEFAULT_STORY64_DATASET_PATH
    return Story64EvalDataset.model_validate_json(dataset_path.read_text(encoding="utf-8"))


def run_story64_content_pipeline_eval(
    *,
    db: Session,
    dataset: Story64EvalDataset | None = None,
    run_label: str = "story64-baseline",
) -> dict[str, Any]:
    dataset = dataset or load_story64_seed_dataset()
    started_at = datetime.now(timezone.utc)
    case_results = [_run_story64_case_eval(db=db, case=case) for case in dataset.cases]
    completed_at = datetime.now(timezone.utc)

    extraction_scores = [
        case_result["extraction"]["score"] for case_result in case_results if case_result["extraction"]
    ]
    topic_scores = [
        case_result["topic_suggestion"]["score"]
        for case_result in case_results
        if case_result["topic_suggestion"]["score"] is not None
    ]

    return {
        "run_label": run_label,
        "dataset_name": dataset.dataset_name,
        "dataset_version": dataset.dataset_version,
        "rubric": EVAL_RUBRIC,
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "summary": {
            "case_count": len(case_results),
            "cases_passed": sum(1 for case_result in case_results if case_result["passed"]),
            "all_cases_passed": all(case_result["passed"] for case_result in case_results),
            "average_extraction_score": _rounded_average(extraction_scores),
            "average_topic_suggestion_score": _rounded_average(topic_scores),
            "topic_eval_case_count": len(topic_scores),
        },
        "cases": case_results,
    }


def _run_story64_case_eval(*, db: Session, case: Story64EvalCase) -> dict[str, Any]:
    seed = _seed_story64_case(db=db, case=case)

    extraction_response = create_content_extraction_artifact_response_for_creator(
        tid=seed["content"].tid,
        creator_id=seed["creator"].id,
        db=db,
        response=Response(),
    )
    extraction_result = _evaluate_extraction(
        case=case,
        extraction_response=extraction_response.model_dump(mode="json"),
    )

    topic_result = _skipped_topic_result()
    expected_topics = case.expected_topics
    if expected_topics is not None and expected_topics.enabled:
        _seed_confirmed_topics(
            db=db,
            content=seed["content"],
            creator=seed["creator"],
            topic_labels=expected_topics.confirmed_topics,
        )
        topic_response = create_content_topic_candidates_response_for_creator(
            tid=seed["content"].tid,
            creator_id=seed["creator"].id,
            db=db,
            response=Response(),
        )
        topic_result = _evaluate_topic_suggestions(
            case=case,
            topic_response=topic_response.model_dump(mode="json"),
        )

    case_passed = extraction_result["passed"] and (
        topic_result["skipped"] or topic_result["passed"]
    )

    return {
        "case_id": case.case_id,
        "title": case.title,
        "content_tid": seed["content"].tid,
        "passed": case_passed,
        "extraction": extraction_result,
        "topic_suggestion": topic_result,
    }


def _seed_story64_case(*, db: Session, case: Story64EvalCase) -> dict[str, Any]:
    creator = Creator(
        name=f"Story 64 Eval {case.case_id}",
        stripe_connect_status="pending",
    )
    db.add(creator)
    db.flush()

    booking_link = BookingLink(
        creator_id=creator.id,
        name=f"Story 64 {case.title}",
        calendly_url=f"https://calendly.com/example/{case.case_id}",
    )
    db.add(booking_link)
    db.flush()

    content = Content(
        creator_id=creator.id,
        booking_link_id=booking_link.id,
        source_url=case.source_url,
        tid=uuid.uuid4().hex,
    )
    db.add(content)
    db.flush()

    snapshot = ContentFetchSnapshot(
        content_id=content.id,
        creator_id=creator.id,
        requested_url=case.snapshot.requested_url,
        fetched_url=case.snapshot.fetched_url,
        fetch_status=case.snapshot.fetch_status,
        http_status=case.snapshot.http_status,
        failure_reason_code=case.snapshot.failure_reason_code,
        failure_detail=case.snapshot.failure_detail,
        response_content_type=case.snapshot.response_content_type,
        response_content_charset=case.snapshot.response_content_charset,
        snapshot_text=case.snapshot.snapshot_text,
    )
    db.add(snapshot)
    db.commit()

    return {
        "creator": creator,
        "booking_link": booking_link,
        "content": content,
        "snapshot": snapshot,
    }


def _seed_confirmed_topics(
    *,
    db: Session,
    content: Content,
    creator: Creator,
    topic_labels: list[str],
) -> None:
    for topic_label in topic_labels:
        db.add(
            ContentConfirmedTopic(
                content_id=content.id,
                creator_id=creator.id,
                canonical_label=normalize_topic_label_display(topic_label),
                normalized_label=normalize_topic_label(topic_label),
            )
        )
    db.commit()


def _evaluate_extraction(
    *,
    case: Story64EvalCase,
    extraction_response: dict[str, Any],
) -> dict[str, Any]:
    expected = case.expected_extraction
    extracted_text = extraction_response.get("extracted_text") or ""
    checks: list[dict[str, Any]] = []

    checks.append(
        _check_result(
            name="status",
            passed=extraction_response["extraction_status"] == expected.status,
            score=1.0 if extraction_response["extraction_status"] == expected.status else 0.0,
            detail=(
                f"expected {expected.status!r}, got "
                f"{extraction_response['extraction_status']!r}"
            ),
        )
    )

    if expected.reason_code is not None or extraction_response.get("extraction_reason_code") is not None:
        actual_reason = extraction_response.get("extraction_reason_code")
        checks.append(
            _check_result(
                name="reason_code",
                passed=actual_reason == expected.reason_code,
                score=1.0 if actual_reason == expected.reason_code else 0.0,
                detail=f"expected {expected.reason_code!r}, got {actual_reason!r}",
            )
        )

    if expected.title is not None:
        actual_title = extraction_response.get("title")
        checks.append(
            _check_result(
                name="title",
                passed=_normalized_text(actual_title) == _normalized_text(expected.title),
                score=1.0
                if _normalized_text(actual_title) == _normalized_text(expected.title)
                else 0.0,
                detail=f"expected {expected.title!r}, got {actual_title!r}",
            )
        )

    if expected.published_at_raw is not None:
        actual_published_at_raw = extraction_response.get("published_at_raw")
        checks.append(
            _check_result(
                name="published_at_raw",
                passed=_normalized_text(actual_published_at_raw)
                == _normalized_text(expected.published_at_raw),
                score=1.0
                if _normalized_text(actual_published_at_raw)
                == _normalized_text(expected.published_at_raw)
                else 0.0,
                detail=(
                    f"expected {expected.published_at_raw!r}, got "
                    f"{actual_published_at_raw!r}"
                ),
            )
        )

    if expected.min_word_count is not None:
        actual_word_count = int(extraction_response.get("extracted_text_word_count") or 0)
        checks.append(
            _check_result(
                name="min_word_count",
                passed=actual_word_count >= expected.min_word_count,
                score=1.0 if actual_word_count >= expected.min_word_count else 0.0,
                detail=(
                    f"expected at least {expected.min_word_count} words, got "
                    f"{actual_word_count}"
                ),
            )
        )

    if expected.required_phrases:
        matched_phrases = [
            phrase
            for phrase in expected.required_phrases
            if _contains_normalized_phrase(extracted_text, phrase)
        ]
        checks.append(
            _check_result(
                name="required_phrases",
                passed=len(matched_phrases) == len(expected.required_phrases),
                score=len(matched_phrases) / len(expected.required_phrases),
                detail=(
                    f"matched {len(matched_phrases)} of {len(expected.required_phrases)} "
                    f"required phrases"
                ),
                matched=matched_phrases,
                missing=[
                    phrase for phrase in expected.required_phrases if phrase not in matched_phrases
                ],
            )
        )

    if expected.forbidden_phrases:
        leaked_phrases = [
            phrase
            for phrase in expected.forbidden_phrases
            if _contains_normalized_phrase(extracted_text, phrase)
        ]
        checks.append(
            _check_result(
                name="forbidden_phrases",
                passed=not leaked_phrases,
                score=(
                    (len(expected.forbidden_phrases) - len(leaked_phrases))
                    / len(expected.forbidden_phrases)
                ),
                detail=(
                    f"found {len(leaked_phrases)} forbidden phrases in extracted_text"
                ),
                leaked=leaked_phrases,
            )
        )

    score = _rounded_average([check["score"] for check in checks]) or 0.0

    return {
        "passed": (
            extraction_response["extraction_status"] == expected.status
            and score >= EXTRACTION_PASS_THRESHOLD
        ),
        "score": score,
        "threshold": EXTRACTION_PASS_THRESHOLD,
        "expected": expected.model_dump(mode="json"),
        "actual": extraction_response,
        "checks": checks,
    }


def _evaluate_topic_suggestions(
    *,
    case: Story64EvalCase,
    topic_response: dict[str, Any],
) -> dict[str, Any]:
    expected = case.expected_topics
    if expected is None:
        return _skipped_topic_result()

    candidate_labels = [
        candidate_topic["suggested_label"]
        for candidate_topic in topic_response["candidate_topics"]
    ]
    checks: list[dict[str, Any]] = []

    if expected.confirmed_topics:
        coverage_matches = []
        coverage_scores = []
        for expected_topic in expected.confirmed_topics:
            best_match = {"candidate_label": None, "score": 0.0}
            for candidate_label in candidate_labels:
                match_score = _topic_match_score(expected_topic, candidate_label)
                if match_score > best_match["score"]:
                    best_match = {
                        "candidate_label": candidate_label,
                        "score": match_score,
                    }
            coverage_matches.append(
                {
                    "confirmed_topic": expected_topic,
                    "candidate_label": best_match["candidate_label"],
                    "score": best_match["score"],
                }
            )
            coverage_scores.append(best_match["score"])

        coverage_score = sum(coverage_scores) / len(coverage_scores)
        checks.append(
            _check_result(
                name="confirmed_topic_coverage",
                passed=all(score >= PARTIAL_TOPIC_CONTAINMENT_SCORE for score in coverage_scores),
                score=coverage_score,
                detail=(
                    f"matched {sum(1 for score in coverage_scores if score > 0)} of "
                    f"{len(coverage_scores)} confirmed topics"
                ),
                matches=coverage_matches,
            )
        )

    if expected.forbidden_suggestions:
        leaked_suggestions = []
        for forbidden_suggestion in expected.forbidden_suggestions:
            for candidate_label in candidate_labels:
                if _topic_match_score(forbidden_suggestion, candidate_label) >= PARTIAL_TOPIC_CONTAINMENT_SCORE:
                    leaked_suggestions.append(
                        {
                            "forbidden_suggestion": forbidden_suggestion,
                            "candidate_label": candidate_label,
                        }
                    )
                    break

        checks.append(
            _check_result(
                name="forbidden_suggestions",
                passed=not leaked_suggestions,
                score=(
                    (len(expected.forbidden_suggestions) - len(leaked_suggestions))
                    / len(expected.forbidden_suggestions)
                ),
                detail=(
                    f"found {len(leaked_suggestions)} forbidden suggestions in "
                    f"{len(candidate_labels)} generated candidates"
                ),
                leaked=leaked_suggestions,
            )
        )

    score = _rounded_average([check["score"] for check in checks]) or 0.0
    review_confirmed_topics = topic_response.get(
        "review_confirmed_topics",
        topic_response.get("confirmed_topics", []),
    )
    return {
        "skipped": False,
        "passed": score >= TOPIC_SUGGESTION_PASS_THRESHOLD,
        "score": score,
        "threshold": TOPIC_SUGGESTION_PASS_THRESHOLD,
        "expected": expected.model_dump(mode="json"),
        "actual": {
            "candidate_topics": topic_response["candidate_topics"],
            "confirmed_topics": review_confirmed_topics,
        },
        "checks": checks,
    }


def _skipped_topic_result() -> dict[str, Any]:
    return {
        "skipped": True,
        "passed": True,
        "score": None,
        "threshold": TOPIC_SUGGESTION_PASS_THRESHOLD,
        "expected": None,
        "actual": None,
        "checks": [],
        "skip_reason": "topic suggestion eval not configured for this case",
    }


def _check_result(
    *,
    name: str,
    passed: bool,
    score: float,
    detail: str,
    **extra: Any,
) -> dict[str, Any]:
    result = {
        "name": name,
        "passed": passed,
        "score": round(score, 4),
        "detail": detail,
    }
    result.update(extra)
    return result


def _contains_normalized_phrase(text: str | None, phrase: str) -> bool:
    return _normalized_text(phrase) in _normalized_text(text)


def _normalized_text(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def _topic_match_score(expected_topic: str, candidate_topic: str) -> float:
    try:
        normalized_expected = normalize_topic_label(expected_topic)
        normalized_candidate = normalize_topic_label(candidate_topic)
    except ValueError:
        return 0.0

    if normalized_expected == normalized_candidate:
        return 1.0
    if (
        normalized_expected in normalized_candidate
        or normalized_candidate in normalized_expected
    ):
        return PARTIAL_TOPIC_CONTAINMENT_SCORE

    expected_tokens = set(normalized_expected.split())
    candidate_tokens = set(normalized_candidate.split())
    if not expected_tokens:
        return 0.0

    token_overlap = len(expected_tokens & candidate_tokens) / len(expected_tokens)
    if token_overlap >= 0.5:
        return PARTIAL_TOPIC_TOKEN_OVERLAP_SCORE
    return 0.0


def _rounded_average(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 4)


def write_story64_eval_output(
    *,
    output_path: str | Path,
    result: dict[str, Any],
) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
