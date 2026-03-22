from pathlib import Path

from app.evals.helper_quality import (
    DEFAULT_STORY96_CANDIDATE_IDS,
    DEFAULT_STORY96_DATASET_PATH,
    HELPER_QUALITY_EVAL_RUBRIC,
    load_story96_helper_eval_dataset,
    run_story96_helper_quality_eval,
    write_story96_helper_eval_output,
)


def test_story96_helper_dataset_loads_expected_cases():
    dataset = load_story96_helper_eval_dataset()

    assert DEFAULT_STORY96_DATASET_PATH.exists()
    assert dataset.dataset_name == "story96_helper_quality_seed"
    assert dataset.dataset_version == "2026-03-17"
    assert [case.case_id for case in dataset.cases] == [
        "ready_single_supported_pattern",
        "ready_ranked_supported_patterns",
        "ready_supported_pattern_with_diagnostics_noise",
        "unsupported_no_paid_results",
        "unsupported_no_overlap_with_diagnostics",
        "unsupported_paid_without_authority",
    ]
    assert dataset.cases[1].expected.expected_ready_cards[0].content_tid == "pricing_breakdown"
    assert dataset.cases[4].expected.expected_unsupported_reasons == [
        "Your reviewed topics and settled paid results do not overlap on the same tracked content yet."
    ]


def test_story96_helper_eval_runner_compares_named_candidates_and_writes_json(
    tmp_path: Path,
):
    dataset = load_story96_helper_eval_dataset()

    result = run_story96_helper_quality_eval(
        dataset=dataset,
        run_label="story96-test-run",
    )

    assert result["run_label"] == "story96-test-run"
    assert result["dataset_name"] == dataset.dataset_name
    assert result["dataset_version"] == dataset.dataset_version
    assert result["rubric"] == HELPER_QUALITY_EVAL_RUBRIC
    assert [candidate["candidate"]["candidate_id"] for candidate in result["candidates"]] == (
        DEFAULT_STORY96_CANDIDATE_IDS
    )

    winning_candidate = result["candidates"][0]
    generic_candidate = result["candidates"][1]

    assert result["comparison"]["winner_candidate_id"] == "current_evidence_backed_rules_v1"
    assert result["comparison"]["ranked_candidates"][0]["candidate_id"] == (
        "current_evidence_backed_rules_v1"
    )
    assert winning_candidate["candidate"]["config_version"] == "next_content_experiments.helper_config.v1"
    assert generic_candidate["candidate"]["config_version"] == "story96.generic_revenue_first.config.v1"

    assert winning_candidate["summary"]["case_count"] == 6
    assert winning_candidate["summary"]["cases_passed"] == 6
    assert winning_candidate["summary"]["all_cases_passed"] is True
    assert winning_candidate["summary"]["average_overall_score"] == 1.0
    assert winning_candidate["summary"]["average_groundedness_score"] == 1.0
    assert winning_candidate["summary"]["average_evidence_citation_correctness_score"] == 1.0
    assert winning_candidate["summary"]["average_unsupported_case_honesty_score"] == 1.0
    assert winning_candidate["summary"]["average_usefulness_score"] == 1.0
    assert generic_candidate["summary"]["cases_passed"] < winning_candidate["summary"]["cases_passed"]
    assert generic_candidate["summary"]["average_overall_score"] < winning_candidate["summary"][
        "average_overall_score"
    ]

    ranked_case = next(
        case
        for case in generic_candidate["cases"]
        if case["case_id"] == "ready_ranked_supported_patterns"
    )
    assert ranked_case["passed"] is False
    assert ranked_case["dimensions"]["evidence_citation_correctness"]["score"] < 1.0
    assert ranked_case["output"]["cards"][0]["content_tids"] == ["launch_offer_faq"]

    unsupported_case = next(
        case
        for case in generic_candidate["cases"]
        if case["case_id"] == "unsupported_paid_without_authority"
    )
    assert unsupported_case["passed"] is False
    assert unsupported_case["output"]["status"] == "ready"
    assert unsupported_case["dimensions"]["unsupported_case_honesty"]["score"] < 0.75

    output_path = tmp_path / "story96-helper-eval.json"
    write_story96_helper_eval_output(output_path=output_path, result=result)

    written_text = output_path.read_text(encoding="utf-8")
    assert '"run_label": "story96-test-run"' in written_text
    assert '"winner_candidate_id": "current_evidence_backed_rules_v1"' in written_text
