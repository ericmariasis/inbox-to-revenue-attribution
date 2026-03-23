from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field, model_validator

from app.services.next_content_experiments import (
    EXPERIMENT_RUN_CONFIG_VERSION,
    EXPERIMENT_RUN_CONTRACT_VERSION,
    EXPERIMENT_RUN_REDUCER_VERSION,
    UNSUPPORTED_EXPERIMENTS_SUMMARY,
    build_next_content_experiment_ranking_rationale,
)

DEFAULT_STORY96_DATASET_PATH = (
    Path(__file__).resolve().parent / "datasets" / "story96_helper_quality_dataset.json"
)

HELPER_EVAL_DIMENSION_PASS_THRESHOLD = 0.75
HELPER_EVAL_CASE_PASS_THRESHOLD = 0.8
MAX_HELPER_EVAL_CARDS = 3

STORY96_BASELINE_RUN_CONFIG_VERSION = "next_content_experiments.helper_config.v1"

DEFAULT_STORY96_CANDIDATE_IDS = [
    "current_evidence_backed_rules_v1",
    "ready_ranking_clarity_v1",
]

HELPER_QUALITY_EVAL_RUBRIC = {
    "version": "story96-v2",
    "case_pass_threshold": HELPER_EVAL_CASE_PASS_THRESHOLD,
    "candidate_ranking": {
        "primary_metric": "average_overall_score",
        "tie_breakers": [
            "cases_passed",
            "average_groundedness_score",
            "average_evidence_citation_correctness_score",
            "average_usefulness_score",
            "average_unsupported_case_honesty_score",
            "candidate_id",
        ],
    },
    "dimensions": {
        "groundedness": {
            "pass_threshold": HELPER_EVAL_DIMENSION_PASS_THRESHOLD,
            "checks": [
                "reported status must match the expected ready or unsupported state",
                "ready outputs must stay inside the case's supported content evidence",
                "unsupported outputs must not fabricate ready cards",
                "forbidden diagnostic or internal fragments must stay out of the output text",
            ],
        },
        "evidence_citation_correctness": {
            "pass_threshold": HELPER_EVAL_DIMENSION_PASS_THRESHOLD,
            "checks": [
                "ready cards are scored positionally against the expected supported content ranking",
                "content_tid, topic_label, paid booking count, paid invoice count, and paid revenue must match the expected evidence citation",
                "missing or vague citations score lower than exact structured citations",
            ],
        },
        "unsupported_case_honesty": {
            "pass_threshold": HELPER_EVAL_DIMENSION_PASS_THRESHOLD,
            "checks": [
                "unsupported cases must stay unsupported",
                "expected unsupported reasons must be stated plainly",
                "unsupported outputs must not hide behind generic advice or fabricate experiment cards",
            ],
        },
        "usefulness": {
            "pass_threshold": HELPER_EVAL_DIMENSION_PASS_THRESHOLD,
            "checks": [
                "ready outputs must include 1 to 3 concrete cards",
                "topic-specific titles or hypotheses should stay actionable rather than generic",
                "modest framing should stay explicit",
                "evidence summaries should include concrete counts or totals when support exists",
                "ready outputs should explain why each card is ranked where it is",
                "unsupported outputs should still explain why the helper is blocked",
            ],
        },
    },
}


class Story96PaidResultSeed(BaseModel):
    booking_uuid: str
    stripe_invoice_id: str
    paid_at: datetime
    amount_cents: int
    currency: str


class Story96ContentPatternSeed(BaseModel):
    content_tid: str
    source_url: str
    authoritative_title: str | None = None
    authoritative_topics: list[str] = Field(default_factory=list)
    settled_paid_results: list[Story96PaidResultSeed] = Field(default_factory=list)

    @property
    def primary_topic(self) -> str | None:
        if not self.authoritative_topics:
            return None
        return self.authoritative_topics[0]

    @property
    def paid_booking_count(self) -> int:
        return len({result.booking_uuid for result in self.settled_paid_results})

    @property
    def paid_invoice_count(self) -> int:
        return len({result.stripe_invoice_id for result in self.settled_paid_results})

    @property
    def paid_revenue_cents(self) -> int:
        return sum(result.amount_cents for result in self.settled_paid_results)

    @property
    def last_paid_at(self) -> datetime | None:
        if not self.settled_paid_results:
            return None
        return max(result.paid_at for result in self.settled_paid_results)


class Story96DiagnosticBacklogSeed(BaseModel):
    unmatched_payment_count: int = 0
    blocked_billing_count: int = 0
    unattributed_booking_count: int = 0

    @property
    def total_count(self) -> int:
        return (
            self.unmatched_payment_count
            + self.blocked_billing_count
            + self.unattributed_booking_count
        )


class Story96ExpectedReadyCard(BaseModel):
    content_tid: str
    topic_label: str
    paid_booking_count: int
    paid_invoice_count: int
    paid_revenue_cents: int
    currency: str


class Story96HelperExpectation(BaseModel):
    status: Literal["ready", "unsupported"]
    expected_ready_cards: list[Story96ExpectedReadyCard] = Field(default_factory=list)
    expected_unsupported_reasons: list[str] = Field(default_factory=list)
    forbidden_text_fragments: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_expected_shape(self) -> "Story96HelperExpectation":
        if self.status == "ready":
            if not self.expected_ready_cards:
                raise ValueError("ready helper eval cases must define expected_ready_cards")
            if self.expected_unsupported_reasons:
                raise ValueError(
                    "ready helper eval cases must not define expected_unsupported_reasons"
                )
        else:
            if self.expected_ready_cards:
                raise ValueError("unsupported helper eval cases must not define expected_ready_cards")
            if not self.expected_unsupported_reasons:
                raise ValueError(
                    "unsupported helper eval cases must define expected_unsupported_reasons"
                )
        return self


class Story96HelperEvalInput(BaseModel):
    content_patterns: list[Story96ContentPatternSeed]
    diagnostic_backlog: Story96DiagnosticBacklogSeed = Field(
        default_factory=Story96DiagnosticBacklogSeed
    )

    @model_validator(mode="after")
    def _validate_unique_content_tids(self) -> "Story96HelperEvalInput":
        content_tids = [pattern.content_tid for pattern in self.content_patterns]
        if len(content_tids) != len(set(content_tids)):
            raise ValueError("Story 96 helper eval content_tid values must be unique per case")
        return self


class Story96HelperEvalCase(BaseModel):
    case_id: str
    title: str
    input: Story96HelperEvalInput
    expected: Story96HelperExpectation


class Story96HelperEvalDataset(BaseModel):
    dataset_name: str
    dataset_version: str
    description: str
    cases: list[Story96HelperEvalCase]

    @model_validator(mode="after")
    def _validate_unique_case_ids(self) -> "Story96HelperEvalDataset":
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("Story 96 helper eval dataset case_id values must be unique")
        return self


class Story96HelperEvidenceCitation(BaseModel):
    content_tid: str
    topic_label: str | None = None
    paid_booking_count: int | None = None
    paid_invoice_count: int | None = None
    paid_revenue_cents: int | None = None
    currency: str | None = None


class Story96HelperCardOutput(BaseModel):
    title: str
    hypothesis: str
    why_this_might_work: str
    evidence_summary: str
    caution: str
    ranking_rationale: str | None = None
    content_tids: list[str] = Field(default_factory=list)
    evidence_citations: list[Story96HelperEvidenceCitation] = Field(default_factory=list)


class Story96HelperCandidateOutput(BaseModel):
    status: Literal["ready", "unsupported"]
    summary: str
    cards: list[Story96HelperCardOutput] = Field(default_factory=list)
    unsupported_reasons: list[str] = Field(default_factory=list)


class Story96HelperCandidateIdentity(BaseModel):
    candidate_id: str
    display_name: str
    generator_type: str
    model_name: str | None = None
    prompt_version: str | None = None
    config_version: str | None = None
    contract_version: str | None = None
    reducer_version: str | None = None
    notes: str | None = None


def load_story96_helper_eval_dataset(
    path: str | Path | None = None,
) -> Story96HelperEvalDataset:
    dataset_path = Path(path) if path is not None else DEFAULT_STORY96_DATASET_PATH
    return Story96HelperEvalDataset.model_validate_json(
        dataset_path.read_text(encoding="utf-8")
    )


def run_story96_helper_quality_eval(
    *,
    dataset: Story96HelperEvalDataset | None = None,
    run_label: str = "story96-helper-quality",
) -> dict[str, Any]:
    dataset = dataset or load_story96_helper_eval_dataset()
    started_at = datetime.now(UTC).isoformat()
    candidate_results = [
        _run_story96_candidate_eval(dataset=dataset, candidate_id=candidate_id)
        for candidate_id in DEFAULT_STORY96_CANDIDATE_IDS
    ]
    completed_at = datetime.now(UTC).isoformat()
    comparison = _build_story96_candidate_comparison(candidate_results=candidate_results)
    return {
        "run_label": run_label,
        "dataset_name": dataset.dataset_name,
        "dataset_version": dataset.dataset_version,
        "rubric": HELPER_QUALITY_EVAL_RUBRIC,
        "started_at": started_at,
        "completed_at": completed_at,
        "candidates": candidate_results,
        "comparison": comparison,
    }


def write_story96_helper_eval_output(
    *,
    output_path: str | Path,
    result: dict[str, Any],
) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")


def _run_story96_candidate_eval(
    *,
    dataset: Story96HelperEvalDataset,
    candidate_id: str,
) -> dict[str, Any]:
    candidate_identity, generator = _story96_candidate_registry()[candidate_id]
    case_results = [
        _evaluate_story96_case(
            case=case,
            candidate_id=candidate_id,
            output=generator(case),
        )
        for case in dataset.cases
    ]

    groundedness_scores = [
        case_result["dimensions"]["groundedness"]["score"] for case_result in case_results
    ]
    citation_scores = [
        case_result["dimensions"]["evidence_citation_correctness"]["score"]
        for case_result in case_results
        if case_result["dimensions"]["evidence_citation_correctness"]["score"] is not None
    ]
    unsupported_scores = [
        case_result["dimensions"]["unsupported_case_honesty"]["score"]
        for case_result in case_results
        if case_result["dimensions"]["unsupported_case_honesty"]["score"] is not None
    ]
    usefulness_scores = [
        case_result["dimensions"]["usefulness"]["score"] for case_result in case_results
    ]
    overall_scores = [case_result["overall_score"] for case_result in case_results]

    return {
        "candidate": candidate_identity.model_dump(mode="json"),
        "summary": {
            "case_count": len(case_results),
            "cases_passed": sum(1 for case_result in case_results if case_result["passed"]),
            "all_cases_passed": all(case_result["passed"] for case_result in case_results),
            "average_overall_score": _rounded_average(overall_scores),
            "average_groundedness_score": _rounded_average(groundedness_scores),
            "average_evidence_citation_correctness_score": _rounded_average(citation_scores),
            "evidence_citation_eval_case_count": len(citation_scores),
            "average_unsupported_case_honesty_score": _rounded_average(unsupported_scores),
            "unsupported_case_honesty_eval_case_count": len(unsupported_scores),
            "average_usefulness_score": _rounded_average(usefulness_scores),
        },
        "cases": case_results,
    }


def _evaluate_story96_case(
    *,
    case: Story96HelperEvalCase,
    candidate_id: str,
    output: Story96HelperCandidateOutput,
) -> dict[str, Any]:
    groundedness = _evaluate_groundedness(case=case, output=output)
    evidence_citations = _evaluate_evidence_citation_correctness(case=case, output=output)
    unsupported_honesty = _evaluate_unsupported_case_honesty(case=case, output=output)
    usefulness = _evaluate_usefulness(case=case, output=output)

    applicable_scores = [
        dimension_result["score"]
        for dimension_result in (
            groundedness,
            evidence_citations,
            unsupported_honesty,
            usefulness,
        )
        if dimension_result["score"] is not None
    ]
    overall_score = _rounded_average(applicable_scores) or 0.0

    return {
        "case_id": case.case_id,
        "title": case.title,
        "candidate_id": candidate_id,
        "passed": (
            groundedness["passed"]
            and evidence_citations["passed"]
            and unsupported_honesty["passed"]
            and usefulness["passed"]
            and overall_score >= HELPER_EVAL_CASE_PASS_THRESHOLD
        ),
        "overall_score": overall_score,
        "overall_threshold": HELPER_EVAL_CASE_PASS_THRESHOLD,
        "dimensions": {
            "groundedness": groundedness,
            "evidence_citation_correctness": evidence_citations,
            "unsupported_case_honesty": unsupported_honesty,
            "usefulness": usefulness,
        },
        "expected": case.expected.model_dump(mode="json"),
        "output": output.model_dump(mode="json"),
    }


def _evaluate_groundedness(
    *,
    case: Story96HelperEvalCase,
    output: Story96HelperCandidateOutput,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    expected_status = case.expected.status
    supported_tids = {card.content_tid for card in case.expected.expected_ready_cards}
    cited_tids = _cited_content_tids(output)

    checks.append(
        _check_result(
            name="status",
            passed=output.status == expected_status,
            score=1.0 if output.status == expected_status else 0.0,
            detail=f"expected {expected_status!r}, got {output.status!r}",
        )
    )

    forbidden_text = _combined_output_text(output)
    leaked_fragments = [
        fragment
        for fragment in case.expected.forbidden_text_fragments
        if _contains_normalized_fragment(forbidden_text, fragment)
    ]
    checks.append(
        _check_result(
            name="forbidden_fragments",
            passed=not leaked_fragments,
            score=(
                1.0
                if not case.expected.forbidden_text_fragments
                else (
                    len(case.expected.forbidden_text_fragments) - len(leaked_fragments)
                )
                / len(case.expected.forbidden_text_fragments)
            ),
            detail=(
                "forbidden text fragments leaked into the output"
                if leaked_fragments
                else "no forbidden fragments found"
            ),
            leaked=leaked_fragments,
        )
    )

    if output.status == "ready":
        supported_citations = [tid for tid in cited_tids if tid in supported_tids]
        checks.append(
            _check_result(
                name="supported_content_only",
                passed=bool(cited_tids) and len(supported_citations) == len(cited_tids),
                score=(
                    0.0
                    if not cited_tids
                    else len(supported_citations) / len(cited_tids)
                ),
                detail=(
                    f"{len(supported_citations)} of {len(cited_tids)} cited content IDs "
                    "match the supported expected set"
                ),
                cited_content_tids=cited_tids,
                expected_supported_content_tids=sorted(supported_tids),
            )
        )
    else:
        checks.append(
            _check_result(
                name="no_fabricated_cards",
                passed=not output.cards,
                score=1.0 if not output.cards else 0.0,
                detail=f"unsupported output returned {len(output.cards)} cards",
            )
        )

    score = _rounded_average([check["score"] for check in checks]) or 0.0
    return {
        "skipped": False,
        "passed": score >= HELPER_EVAL_DIMENSION_PASS_THRESHOLD and output.status == expected_status,
        "score": score,
        "threshold": HELPER_EVAL_DIMENSION_PASS_THRESHOLD,
        "checks": checks,
    }


def _evaluate_evidence_citation_correctness(
    *,
    case: Story96HelperEvalCase,
    output: Story96HelperCandidateOutput,
) -> dict[str, Any]:
    if case.expected.status != "ready":
        return _skipped_dimension_result("evidence citation eval only applies to ready cases")

    checks: list[dict[str, Any]] = []
    for index, expected_card in enumerate(case.expected.expected_ready_cards, start=1):
        actual_card = output.cards[index - 1] if index - 1 < len(output.cards) else None
        actual_citation = _primary_citation(actual_card)
        if actual_citation is None:
            checks.extend(
                [
                    _check_result(
                        name=f"card_{index}_content_tid",
                        passed=False,
                        score=0.0,
                        detail="missing card or structured citation",
                    ),
                    _check_result(
                        name=f"card_{index}_topic_label",
                        passed=False,
                        score=0.0,
                        detail="missing card or structured citation",
                    ),
                    _check_result(
                        name=f"card_{index}_paid_booking_count",
                        passed=False,
                        score=0.0,
                        detail="missing card or structured citation",
                    ),
                    _check_result(
                        name=f"card_{index}_paid_invoice_count",
                        passed=False,
                        score=0.0,
                        detail="missing card or structured citation",
                    ),
                    _check_result(
                        name=f"card_{index}_paid_revenue_cents",
                        passed=False,
                        score=0.0,
                        detail="missing card or structured citation",
                    ),
                ]
            )
            continue

        checks.append(
            _check_result(
                name=f"card_{index}_content_tid",
                passed=actual_citation.content_tid == expected_card.content_tid,
                score=1.0 if actual_citation.content_tid == expected_card.content_tid else 0.0,
                detail=(
                    f"expected {expected_card.content_tid!r}, got "
                    f"{actual_citation.content_tid!r}"
                ),
            )
        )
        checks.append(
            _check_result(
                name=f"card_{index}_topic_label",
                passed=actual_citation.topic_label == expected_card.topic_label,
                score=1.0 if actual_citation.topic_label == expected_card.topic_label else 0.0,
                detail=(
                    f"expected {expected_card.topic_label!r}, got "
                    f"{actual_citation.topic_label!r}"
                ),
            )
        )
        checks.append(
            _check_result(
                name=f"card_{index}_paid_booking_count",
                passed=actual_citation.paid_booking_count == expected_card.paid_booking_count,
                score=(
                    1.0
                    if actual_citation.paid_booking_count == expected_card.paid_booking_count
                    else 0.0
                ),
                detail=(
                    f"expected {expected_card.paid_booking_count}, got "
                    f"{actual_citation.paid_booking_count}"
                ),
            )
        )
        checks.append(
            _check_result(
                name=f"card_{index}_paid_invoice_count",
                passed=actual_citation.paid_invoice_count == expected_card.paid_invoice_count,
                score=(
                    1.0
                    if actual_citation.paid_invoice_count == expected_card.paid_invoice_count
                    else 0.0
                ),
                detail=(
                    f"expected {expected_card.paid_invoice_count}, got "
                    f"{actual_citation.paid_invoice_count}"
                ),
            )
        )
        checks.append(
            _check_result(
                name=f"card_{index}_paid_revenue_cents",
                passed=actual_citation.paid_revenue_cents == expected_card.paid_revenue_cents,
                score=(
                    1.0
                    if actual_citation.paid_revenue_cents == expected_card.paid_revenue_cents
                    else 0.0
                ),
                detail=(
                    f"expected {expected_card.paid_revenue_cents}, got "
                    f"{actual_citation.paid_revenue_cents}"
                ),
            )
        )

    score = _rounded_average([check["score"] for check in checks]) or 0.0
    return {
        "skipped": False,
        "passed": score >= HELPER_EVAL_DIMENSION_PASS_THRESHOLD,
        "score": score,
        "threshold": HELPER_EVAL_DIMENSION_PASS_THRESHOLD,
        "checks": checks,
    }


def _evaluate_unsupported_case_honesty(
    *,
    case: Story96HelperEvalCase,
    output: Story96HelperCandidateOutput,
) -> dict[str, Any]:
    if case.expected.status != "unsupported":
        return _skipped_dimension_result("unsupported honesty eval only applies to unsupported cases")

    checks: list[dict[str, Any]] = []
    combined_text = _combined_output_text(output)
    covered_reasons = [
        reason
        for reason in case.expected.expected_unsupported_reasons
        if _contains_normalized_fragment(combined_text, reason)
    ]

    checks.append(
        _check_result(
            name="status",
            passed=output.status == "unsupported",
            score=1.0 if output.status == "unsupported" else 0.0,
            detail=f"expected 'unsupported', got {output.status!r}",
        )
    )
    checks.append(
        _check_result(
            name="no_cards",
            passed=not output.cards,
            score=1.0 if not output.cards else 0.0,
            detail=f"unsupported output returned {len(output.cards)} cards",
        )
    )
    checks.append(
        _check_result(
            name="reason_coverage",
            passed=len(covered_reasons) == len(case.expected.expected_unsupported_reasons),
            score=len(covered_reasons) / len(case.expected.expected_unsupported_reasons),
            detail=(
                f"covered {len(covered_reasons)} of "
                f"{len(case.expected.expected_unsupported_reasons)} expected reasons"
            ),
            matched=covered_reasons,
            missing=[
                reason
                for reason in case.expected.expected_unsupported_reasons
                if reason not in covered_reasons
            ],
        )
    )
    checks.append(
        _check_result(
            name="summary_present",
            passed=bool(output.summary.strip()),
            score=1.0 if output.summary.strip() else 0.0,
            detail="unsupported summary must be non-empty",
        )
    )

    score = _rounded_average([check["score"] for check in checks]) or 0.0
    return {
        "skipped": False,
        "passed": score >= HELPER_EVAL_DIMENSION_PASS_THRESHOLD,
        "score": score,
        "threshold": HELPER_EVAL_DIMENSION_PASS_THRESHOLD,
        "checks": checks,
    }


def _evaluate_usefulness(
    *,
    case: Story96HelperEvalCase,
    output: Story96HelperCandidateOutput,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    if output.status == "ready":
        expected_topics = [card.topic_label for card in case.expected.expected_ready_cards]
        topic_specific_cards = 0
        hypothesis_style_cards = 0
        modest_caution_cards = 0
        evidence_specific_cards = 0
        ranking_rationale_cards = 0
        ranking_rationale_grounded_cards = 0
        ranking_rationale_order_cards = 0

        for index, card in enumerate(output.cards):
            expected_topic = expected_topics[index] if index < len(expected_topics) else None
            title_and_hypothesis = f"{card.title} {card.hypothesis}"
            if expected_topic and _contains_normalized_fragment(title_and_hypothesis, expected_topic):
                topic_specific_cards += 1
            if card.hypothesis.startswith("Test whether"):
                hypothesis_style_cards += 1
            if _contains_normalized_fragment(card.caution, "hypothesis") or _contains_normalized_fragment(
                card.caution,
                "guarantee",
            ):
                modest_caution_cards += 1
            if any(character.isdigit() for character in card.evidence_summary):
                evidence_specific_cards += 1
            if card.ranking_rationale and card.ranking_rationale.strip():
                ranking_rationale_cards += 1
                if _ranking_rationale_has_evidence_signal(card.ranking_rationale):
                    ranking_rationale_grounded_cards += 1
                if _ranking_rationale_has_order_signal(card.ranking_rationale):
                    ranking_rationale_order_cards += 1

        ready_card_count = len(output.cards)
        denominator = ready_card_count if ready_card_count else 1
        checks.extend(
            [
                _check_result(
                    name="summary_present",
                    passed=bool(output.summary.strip()),
                    score=1.0 if output.summary.strip() else 0.0,
                    detail="ready summary must be non-empty",
                ),
                _check_result(
                    name="card_count",
                    passed=1 <= ready_card_count <= MAX_HELPER_EVAL_CARDS,
                    score=1.0 if 1 <= ready_card_count <= MAX_HELPER_EVAL_CARDS else 0.0,
                    detail=f"ready output returned {ready_card_count} cards",
                ),
                _check_result(
                    name="topic_specificity",
                    passed=ready_card_count > 0 and topic_specific_cards == ready_card_count,
                    score=topic_specific_cards / denominator,
                    detail=(
                        f"{topic_specific_cards} of {ready_card_count} cards mention the "
                        "expected topic in the title or hypothesis"
                    ),
                ),
                _check_result(
                    name="hypothesis_style",
                    passed=ready_card_count > 0 and hypothesis_style_cards == ready_card_count,
                    score=hypothesis_style_cards / denominator,
                    detail=(
                        f"{hypothesis_style_cards} of {ready_card_count} cards keep the "
                        "expected hypothesis-style opening"
                    ),
                ),
                _check_result(
                    name="modest_caution",
                    passed=ready_card_count > 0 and modest_caution_cards == ready_card_count,
                    score=modest_caution_cards / denominator,
                    detail=(
                        f"{modest_caution_cards} of {ready_card_count} cards keep modest "
                        "caution framing"
                    ),
                ),
                _check_result(
                    name="evidence_specificity",
                    passed=ready_card_count > 0 and evidence_specific_cards == ready_card_count,
                    score=evidence_specific_cards / denominator,
                    detail=(
                        f"{evidence_specific_cards} of {ready_card_count} cards include "
                        "concrete evidence numbers in the evidence summary"
                    ),
                ),
                _check_result(
                    name="ranking_rationale_present",
                    passed=ready_card_count > 0 and ranking_rationale_cards == ready_card_count,
                    score=ranking_rationale_cards / denominator,
                    detail=(
                        f"{ranking_rationale_cards} of {ready_card_count} cards include "
                        "ranking-rationale copy"
                    ),
                ),
                _check_result(
                    name="ranking_rationale_grounded",
                    passed=ready_card_count > 0
                    and ranking_rationale_grounded_cards == ready_card_count,
                    score=ranking_rationale_grounded_cards / denominator,
                    detail=(
                        f"{ranking_rationale_grounded_cards} of {ready_card_count} cards include "
                        "concrete evidence signals in the ranking rationale"
                    ),
                ),
                _check_result(
                    name="ranking_rationale_order_signal",
                    passed=ready_card_count > 0
                    and ranking_rationale_order_cards == ready_card_count,
                    score=ranking_rationale_order_cards / denominator,
                    detail=(
                        f"{ranking_rationale_order_cards} of {ready_card_count} cards explain "
                        "placement in the current ordering"
                    ),
                ),
            ]
        )
    else:
        combined_text = _combined_output_text(output)
        covered_reasons = [
            reason
            for reason in case.expected.expected_unsupported_reasons
            if _contains_normalized_fragment(combined_text, reason)
        ]
        checks.extend(
            [
                _check_result(
                    name="summary_present",
                    passed=bool(output.summary.strip()),
                    score=1.0 if output.summary.strip() else 0.0,
                    detail="unsupported summary must be non-empty",
                ),
                _check_result(
                    name="no_cards",
                    passed=not output.cards,
                    score=1.0 if not output.cards else 0.0,
                    detail=f"unsupported output returned {len(output.cards)} cards",
                ),
                _check_result(
                    name="specific_reasons",
                    passed=len(covered_reasons) == len(case.expected.expected_unsupported_reasons),
                    score=len(covered_reasons) / len(case.expected.expected_unsupported_reasons),
                    detail=(
                        f"covered {len(covered_reasons)} of "
                        f"{len(case.expected.expected_unsupported_reasons)} expected reasons"
                    ),
                ),
            ]
        )

    score = _rounded_average([check["score"] for check in checks]) or 0.0
    return {
        "skipped": False,
        "passed": score >= HELPER_EVAL_DIMENSION_PASS_THRESHOLD,
        "score": score,
        "threshold": HELPER_EVAL_DIMENSION_PASS_THRESHOLD,
        "checks": checks,
    }


def _skipped_dimension_result(skip_reason: str) -> dict[str, Any]:
    return {
        "skipped": True,
        "passed": True,
        "score": None,
        "threshold": HELPER_EVAL_DIMENSION_PASS_THRESHOLD,
        "checks": [],
        "skip_reason": skip_reason,
    }


def _story96_candidate_registry() -> dict[
    str,
    tuple[
        Story96HelperCandidateIdentity,
        Callable[[Story96HelperEvalCase], Story96HelperCandidateOutput],
    ],
]:
    return {
        "current_evidence_backed_rules_v1": (
            Story96HelperCandidateIdentity(
                candidate_id="current_evidence_backed_rules_v1",
                display_name="Current Evidence-Backed Rules",
                generator_type="deterministic_rules",
                model_name=None,
                prompt_version=None,
                config_version=STORY96_BASELINE_RUN_CONFIG_VERSION,
                contract_version=EXPERIMENT_RUN_CONTRACT_VERSION,
                reducer_version=EXPERIMENT_RUN_REDUCER_VERSION,
                notes=(
                    "Comparison candidate that mirrors the narrow Story 65 evidence-backed "
                    "helper contract without changing creator-visible behavior."
                ),
            ),
            _generate_current_evidence_backed_rules,
        ),
        "ready_ranking_clarity_v1": (
            Story96HelperCandidateIdentity(
                candidate_id="ready_ranking_clarity_v1",
                display_name="Ready Ranking Clarity",
                generator_type="deterministic_rules",
                model_name=None,
                prompt_version=None,
                config_version=EXPERIMENT_RUN_CONFIG_VERSION,
                contract_version=EXPERIMENT_RUN_CONTRACT_VERSION,
                reducer_version=EXPERIMENT_RUN_REDUCER_VERSION,
                notes=(
                    "Comparison candidate that keeps the current evidence-backed helper logic "
                    "but adds creator-visible ranking rationale for ready cards."
                ),
            ),
            _generate_ready_ranking_clarity_rules,
        ),
        "generic_revenue_first_v1": (
            Story96HelperCandidateIdentity(
                candidate_id="generic_revenue_first_v1",
                display_name="Generic Revenue-First Comparison",
                generator_type="deterministic_rules",
                model_name=None,
                prompt_version=None,
                config_version="story96.generic_revenue_first.config.v1",
                contract_version="story96.generic_revenue_first.v1",
                reducer_version="story96.generic_revenue_first.rules.v1",
                notes=(
                    "Comparison-only candidate that optimizes for recent revenue and generic "
                    "momentum language even when authoritative support is incomplete."
                ),
            ),
            _generate_generic_revenue_first_rules,
        ),
    }


def _generate_current_evidence_backed_rules(
    case: Story96HelperEvalCase,
) -> Story96HelperCandidateOutput:
    supported_patterns = _supported_patterns(case)
    if not supported_patterns:
        return Story96HelperCandidateOutput(
            status="unsupported",
            summary=UNSUPPORTED_EXPERIMENTS_SUMMARY,
            cards=[],
            unsupported_reasons=_expected_unsupported_reasons(case),
        )

    selected_patterns = sorted(supported_patterns, key=_baseline_pattern_sort_key)[
        :MAX_HELPER_EVAL_CARDS
    ]
    cards = [_build_baseline_card(pattern=pattern) for pattern in selected_patterns]
    return Story96HelperCandidateOutput(
        status="ready",
        summary=_ready_summary(card_count=len(cards)),
        cards=cards,
        unsupported_reasons=[],
    )


def _generate_ready_ranking_clarity_rules(
    case: Story96HelperEvalCase,
) -> Story96HelperCandidateOutput:
    supported_patterns = _supported_patterns(case)
    if not supported_patterns:
        return Story96HelperCandidateOutput(
            status="unsupported",
            summary=UNSUPPORTED_EXPERIMENTS_SUMMARY,
            cards=[],
            unsupported_reasons=_expected_unsupported_reasons(case),
        )

    selected_patterns = sorted(supported_patterns, key=_baseline_pattern_sort_key)[
        :MAX_HELPER_EVAL_CARDS
    ]
    cards = [
        _build_baseline_card(
            pattern=pattern,
            ranking_rationale=build_next_content_experiment_ranking_rationale(
                rank=index,
                paid_booking_count=pattern.paid_booking_count,
                paid_invoice_count=pattern.paid_invoice_count,
                revenue_summary=_format_revenue_summary(pattern),
                reason=_baseline_ranking_reason(
                    selected_patterns=selected_patterns,
                    rank=index,
                ),
            ),
        )
        for index, pattern in enumerate(selected_patterns, start=1)
    ]
    return Story96HelperCandidateOutput(
        status="ready",
        summary=_ready_summary(card_count=len(cards)),
        cards=cards,
        unsupported_reasons=[],
    )


def _generate_generic_revenue_first_rules(
    case: Story96HelperEvalCase,
) -> Story96HelperCandidateOutput:
    paid_patterns = [pattern for pattern in case.input.content_patterns if pattern.settled_paid_results]
    if not paid_patterns:
        return Story96HelperCandidateOutput(
            status="unsupported",
            summary=(
                "Not enough data yet to suggest experiments. Keep publishing and check back "
                "after more activity."
            ),
            cards=[],
            unsupported_reasons=[],
        )

    selected_patterns = sorted(paid_patterns, key=_generic_pattern_sort_key)[:MAX_HELPER_EVAL_CARDS]
    cards = [_build_generic_card(pattern=pattern) for pattern in selected_patterns]
    summary = "Recent revenue patterns point to these next ideas."
    if case.input.diagnostic_backlog.total_count > 0:
        summary += (
            f" There are also {case.input.diagnostic_backlog.total_count} unmatched or blocked "
            "items still in flight."
        )
    return Story96HelperCandidateOutput(
        status="ready",
        summary=summary,
        cards=cards,
        unsupported_reasons=[],
    )


def _supported_patterns(case: Story96HelperEvalCase) -> list[Story96ContentPatternSeed]:
    return [
        pattern
        for pattern in case.input.content_patterns
        if pattern.authoritative_topics and pattern.settled_paid_results
    ]


def _expected_unsupported_reasons(case: Story96HelperEvalCase) -> list[str]:
    reasons: list[str] = []
    has_authoritative_topics = any(
        pattern.authoritative_topics for pattern in case.input.content_patterns
    )
    has_paid_results = any(
        pattern.settled_paid_results for pattern in case.input.content_patterns
    )
    has_overlap = any(
        pattern.authoritative_topics and pattern.settled_paid_results
        for pattern in case.input.content_patterns
    )

    if not has_authoritative_topics:
        reasons.append("No authoritative reviewed topics exist yet on your tracked content.")
    if not has_paid_results:
        reasons.append("No settled attributed paid results exist yet for this workspace.")
    if has_authoritative_topics and has_paid_results and not has_overlap:
        reasons.append(
            "Your reviewed topics and settled paid results do not overlap on the same tracked content yet."
        )
    return reasons


def _build_baseline_card(
    *,
    pattern: Story96ContentPatternSeed,
    ranking_rationale: str | None = None,
) -> Story96HelperCardOutput:
    topic_label = pattern.primary_topic or "Supported Pattern"
    title = _truncate_title(f"Test another {topic_label} angle")
    hypothesis = (
        f"Test whether another post about {topic_label} may lead to more attributed paid bookings."
    )
    why_this_might_work = (
        f"Your authoritative content at {pattern.source_url} already links the topic "
        f'"{topic_label}" to {pattern.paid_booking_count} paid '
        f'booking{"s" if pattern.paid_booking_count != 1 else ""}.'
    )
    evidence_summary = (
        f'Authoritative content pattern: "{topic_label}" on tracking ID {pattern.content_tid}. '
        f"Settled paid pattern: {pattern.paid_booking_count} paid "
        f'booking{"s" if pattern.paid_booking_count != 1 else ""} across '
        f"{pattern.paid_invoice_count} paid "
        f'invoice{"s" if pattern.paid_invoice_count != 1 else ""} totaling '
        f"{_format_revenue_summary(pattern)}."
    )
    caution = (
        "Treat this as a hypothesis, not a guarantee. This card is grounded in one "
        "authoritative content pattern and this creator's settled paid results for one tracked post."
    )
    return Story96HelperCardOutput(
        title=title,
        hypothesis=hypothesis,
        why_this_might_work=why_this_might_work,
        evidence_summary=evidence_summary,
        caution=caution,
        ranking_rationale=ranking_rationale,
        content_tids=[pattern.content_tid],
        evidence_citations=[
            Story96HelperEvidenceCitation(
                content_tid=pattern.content_tid,
                topic_label=topic_label,
                paid_booking_count=pattern.paid_booking_count,
                paid_invoice_count=pattern.paid_invoice_count,
                paid_revenue_cents=pattern.paid_revenue_cents,
                currency=_single_currency(pattern),
            )
        ],
    )


def _build_generic_card(
    *,
    pattern: Story96ContentPatternSeed,
) -> Story96HelperCardOutput:
    topic_label = pattern.primary_topic or "recent sales momentum"
    return Story96HelperCardOutput(
        title=_truncate_title(f"Lean harder into {topic_label}"),
        hypothesis="Create another post in this area to build momentum.",
        why_this_might_work=(
            f"This area has generated revenue recently for tracking ID {pattern.content_tid}."
        ),
        evidence_summary=f"Recent paid revenue totals {_format_revenue_summary(pattern)}.",
        caution="Use this as directional inspiration while more results come in.",
        content_tids=[pattern.content_tid],
        evidence_citations=[
            Story96HelperEvidenceCitation(
                content_tid=pattern.content_tid,
                topic_label=topic_label,
                paid_revenue_cents=pattern.paid_revenue_cents,
                currency=_single_currency(pattern),
            )
        ],
    )


def _baseline_pattern_sort_key(
    pattern: Story96ContentPatternSeed,
) -> tuple[int, int, int, str]:
    last_paid_at = pattern.last_paid_at or datetime.min
    return (
        -pattern.paid_booking_count,
        -pattern.paid_revenue_cents,
        -int(last_paid_at.timestamp()),
        pattern.content_tid,
    )


def _baseline_ranking_reason(
    *,
    selected_patterns: list[Story96ContentPatternSeed],
    rank: int,
) -> Literal[
    "only_supported_pattern",
    "paid_bookings",
    "paid_revenue",
    "recency",
    "deterministic_tie_breaker",
]:
    if len(selected_patterns) == 1:
        return "only_supported_pattern"

    current_index = rank - 1
    current_pattern = selected_patterns[current_index]
    reference_pattern = (
        selected_patterns[1]
        if current_index == 0
        else selected_patterns[current_index - 1]
    )

    if current_pattern.paid_booking_count != reference_pattern.paid_booking_count:
        return "paid_bookings"
    if current_pattern.paid_revenue_cents != reference_pattern.paid_revenue_cents:
        return "paid_revenue"
    if current_pattern.last_paid_at != reference_pattern.last_paid_at:
        return "recency"
    return "deterministic_tie_breaker"


def _generic_pattern_sort_key(
    pattern: Story96ContentPatternSeed,
) -> tuple[int, int, int, str]:
    last_paid_at = pattern.last_paid_at or datetime.min
    return (
        -pattern.paid_revenue_cents,
        -int(last_paid_at.timestamp()),
        -pattern.paid_booking_count,
        pattern.content_tid,
    )


def _build_story96_candidate_comparison(
    *,
    candidate_results: list[dict[str, Any]],
) -> dict[str, Any]:
    ranked_candidates = sorted(
        candidate_results,
        key=lambda candidate_result: (
            -(candidate_result["summary"]["average_overall_score"] or 0.0),
            -candidate_result["summary"]["cases_passed"],
            -(candidate_result["summary"]["average_groundedness_score"] or 0.0),
            -(candidate_result["summary"]["average_evidence_citation_correctness_score"] or 0.0),
            -(candidate_result["summary"]["average_usefulness_score"] or 0.0),
            -(candidate_result["summary"]["average_unsupported_case_honesty_score"] or 0.0),
            candidate_result["candidate"]["candidate_id"],
        ),
    )
    return {
        "winner_candidate_id": ranked_candidates[0]["candidate"]["candidate_id"],
        "ranked_candidates": [
            {
                "rank": index,
                "candidate_id": candidate_result["candidate"]["candidate_id"],
                "display_name": candidate_result["candidate"]["display_name"],
                "cases_passed": candidate_result["summary"]["cases_passed"],
                "case_count": candidate_result["summary"]["case_count"],
                "average_overall_score": candidate_result["summary"]["average_overall_score"],
                "average_groundedness_score": candidate_result["summary"]["average_groundedness_score"],
                "average_evidence_citation_correctness_score": candidate_result["summary"][
                    "average_evidence_citation_correctness_score"
                ],
                "average_unsupported_case_honesty_score": candidate_result["summary"][
                    "average_unsupported_case_honesty_score"
                ],
                "average_usefulness_score": candidate_result["summary"]["average_usefulness_score"],
            }
            for index, candidate_result in enumerate(ranked_candidates, start=1)
        ],
    }


def _single_currency(pattern: Story96ContentPatternSeed) -> str | None:
    currencies = {result.currency for result in pattern.settled_paid_results}
    if len(currencies) != 1:
        return None
    return next(iter(currencies))


def _format_revenue_summary(pattern: Story96ContentPatternSeed) -> str:
    totals_by_currency: dict[str, int] = defaultdict(int)
    for result in pattern.settled_paid_results:
        totals_by_currency[result.currency] += result.amount_cents

    parts = [
        f"{currency} {_format_money_from_cents(amount_cents)}"
        for currency, amount_cents in sorted(totals_by_currency.items())
    ]
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts)


def _ready_summary(*, card_count: int) -> str:
    if card_count == 1:
        return (
            "Here is the next content experiment most grounded in your current "
            "authoritative topics and attributed paid results."
        )
    return (
        "Here are the next content experiments most grounded in your current "
        "authoritative topics and attributed paid results."
    )


def _truncate_title(value: str) -> str:
    if len(value) <= 255:
        return value
    return value[:252].rstrip() + "..."


def _format_money_from_cents(amount_cents: int) -> str:
    return f"{amount_cents / 100:,.2f}"


def _combined_output_text(output: Story96HelperCandidateOutput) -> str:
    parts = [output.summary, *output.unsupported_reasons]
    for card in output.cards:
        parts.extend(
            [
                card.title,
                card.hypothesis,
                card.why_this_might_work,
                card.evidence_summary,
                card.ranking_rationale,
                card.caution,
            ]
        )
    return " ".join(part for part in parts if part)


def _ranking_rationale_has_evidence_signal(value: str) -> bool:
    return any(character.isdigit() for character in value) and (
        _contains_normalized_fragment(value, "booking")
        or _contains_normalized_fragment(value, "invoice")
        or _contains_normalized_fragment(value, "revenue")
        or _contains_normalized_fragment(value, "recent")
    )


def _ranking_rationale_has_order_signal(value: str) -> bool:
    return (
        _contains_normalized_fragment(value, "lead")
        or _contains_normalized_fragment(value, "first")
        or _contains_normalized_fragment(value, "only supported pattern")
        or _contains_normalized_fragment(value, "ranks below")
        or _contains_normalized_fragment(value, "tie-breaker")
    )


def _cited_content_tids(output: Story96HelperCandidateOutput) -> list[str]:
    content_tids: list[str] = []
    for card in output.cards:
        for citation in card.evidence_citations:
            if citation.content_tid:
                content_tids.append(citation.content_tid)
        for content_tid in card.content_tids:
            if content_tid not in content_tids:
                content_tids.append(content_tid)
    return content_tids


def _primary_citation(
    card: Story96HelperCardOutput | None,
) -> Story96HelperEvidenceCitation | None:
    if card is None:
        return None
    if card.evidence_citations:
        return card.evidence_citations[0]
    if card.content_tids:
        return Story96HelperEvidenceCitation(content_tid=card.content_tids[0])
    return None


def _contains_normalized_fragment(text: str, fragment: str) -> bool:
    return _normalized_text(fragment) in _normalized_text(text)


def _normalized_text(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def _rounded_average(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 4)


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
