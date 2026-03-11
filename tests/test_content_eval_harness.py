from pathlib import Path

from app.evals.content_pipeline import (
    DEFAULT_STORY64_DATASET_PATH,
    EVAL_RUBRIC,
    load_story64_seed_dataset,
    run_story64_content_pipeline_eval,
    write_story64_eval_output,
)
from app.db.session import SessionLocal


def test_story64_seed_dataset_loads_expected_cases():
    dataset = load_story64_seed_dataset()

    assert DEFAULT_STORY64_DATASET_PATH.exists()
    assert dataset.dataset_name == "story64_phase11_content_pipeline_seed"
    assert dataset.dataset_version == "2026-03-10"
    assert [case.case_id for case in dataset.cases] == [
        "launch_pricing_breakdown",
        "student_retention_playbook",
        "micro_update_low_confidence",
    ]
    assert dataset.cases[0].expected_topics is not None
    assert dataset.cases[0].expected_topics.confirmed_topics == [
        "Launch Pricing Breakdown",
        "Discovery Call Pricing",
        "Retainer Onboarding Checklist",
    ]
    assert dataset.cases[2].expected_extraction.reason_code == "TEXT_TOO_SHORT"


def test_story64_eval_runner_returns_structured_case_results_and_writes_json(tmp_path: Path):
    dataset = load_story64_seed_dataset()

    with SessionLocal() as db:
        result = run_story64_content_pipeline_eval(
            db=db,
            dataset=dataset,
            run_label="story64-test-run",
        )

    assert result["run_label"] == "story64-test-run"
    assert result["dataset_name"] == dataset.dataset_name
    assert result["rubric"] == EVAL_RUBRIC
    assert result["summary"]["case_count"] == 3
    assert result["summary"]["cases_passed"] == 3
    assert result["summary"]["all_cases_passed"] is True
    assert result["summary"]["average_extraction_score"] == 1.0
    assert result["summary"]["average_topic_suggestion_score"] == 0.9375
    assert result["summary"]["topic_eval_case_count"] == 2

    launch_case = result["cases"][0]
    assert launch_case["case_id"] == "launch_pricing_breakdown"
    assert launch_case["passed"] is True
    assert launch_case["extraction"]["passed"] is True
    assert launch_case["extraction"]["actual"]["extraction_status"] == "succeeded"
    assert launch_case["topic_suggestion"]["passed"] is True
    launch_candidate_labels = [
        candidate["suggested_label"]
        for candidate in launch_case["topic_suggestion"]["actual"]["candidate_topics"]
    ]
    assert "Launch Pricing Breakdown" in launch_candidate_labels
    assert "Discovery Call Pricing Expectations" in launch_candidate_labels
    assert "Retainer Onboarding Checklist Keeps" in launch_candidate_labels

    low_confidence_case = result["cases"][2]
    assert low_confidence_case["case_id"] == "micro_update_low_confidence"
    assert low_confidence_case["extraction"]["actual"]["extraction_status"] == "low_confidence"
    assert low_confidence_case["extraction"]["actual"]["title"] == "Micro Update"
    assert low_confidence_case["topic_suggestion"]["skipped"] is True

    output_path = tmp_path / "story64-eval.json"
    write_story64_eval_output(output_path=output_path, result=result)

    written_text = output_path.read_text(encoding="utf-8")
    assert '"run_label": "story64-test-run"' in written_text
    assert '"all_cases_passed": true' in written_text
