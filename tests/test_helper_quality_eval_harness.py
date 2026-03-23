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

    baseline_candidate = result["candidates"][0]
    ranking_candidate = result["candidates"][1]

    assert result["comparison"]["winner_candidate_id"] == "ready_ranking_clarity_v1"
    assert result["comparison"]["ranked_candidates"][0]["candidate_id"] == "ready_ranking_clarity_v1"
    assert baseline_candidate["candidate"]["config_version"] == "next_content_experiments.helper_config.v1"
    assert ranking_candidate["candidate"]["config_version"] == "next_content_experiments.helper_config.v2"

    assert ranking_candidate["summary"]["case_count"] == 6
    assert ranking_candidate["summary"]["cases_passed"] == 6
    assert ranking_candidate["summary"]["all_cases_passed"] is True
    assert ranking_candidate["summary"]["average_overall_score"] == 1.0
    assert ranking_candidate["summary"]["average_groundedness_score"] == 1.0
    assert ranking_candidate["summary"]["average_evidence_citation_correctness_score"] == 1.0
    assert ranking_candidate["summary"]["average_unsupported_case_honesty_score"] == 1.0
    assert ranking_candidate["summary"]["average_usefulness_score"] == 1.0
    assert baseline_candidate["summary"]["cases_passed"] < ranking_candidate["summary"]["cases_passed"]
    assert baseline_candidate["summary"]["average_overall_score"] < ranking_candidate["summary"][
        "average_overall_score"
    ]
    assert baseline_candidate["summary"]["average_usefulness_score"] < ranking_candidate["summary"][
        "average_usefulness_score"
    ]

    baseline_ranked_case = next(
        case
        for case in baseline_candidate["cases"]
        if case["case_id"] == "ready_ranked_supported_patterns"
    )
    assert baseline_ranked_case["passed"] is False
    assert baseline_ranked_case["dimensions"]["usefulness"]["score"] < 1.0
    assert baseline_ranked_case["output"]["cards"][0]["ranking_rationale"] is None

    ranking_case = next(
        case
        for case in ranking_candidate["cases"]
        if case["case_id"] == "ready_ranked_supported_patterns"
    )
    assert ranking_case["passed"] is True
    assert "leads your current snapshot on paid bookings" in ranking_case["output"]["cards"][0][
        "ranking_rationale"
    ]
    assert "ranks below the card above because that pattern has more paid bookings" in (
        ranking_case["output"]["cards"][1]["ranking_rationale"]
    )

    output_path = tmp_path / "story96-helper-eval.json"
    write_story96_helper_eval_output(output_path=output_path, result=result)

    written_text = output_path.read_text(encoding="utf-8")
    assert '"run_label": "story96-test-run"' in written_text
    assert '"winner_candidate_id": "ready_ranking_clarity_v1"' in written_text
