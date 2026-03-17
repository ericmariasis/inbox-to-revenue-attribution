import argparse
import sys

from app.evals.helper_quality import (
    DEFAULT_STORY96_DATASET_PATH,
    load_story96_helper_eval_dataset,
    run_story96_helper_quality_eval,
    write_story96_helper_eval_output,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Story 96 helper-quality comparison eval harness.",
    )
    parser.add_argument(
        "--dataset",
        default=str(DEFAULT_STORY96_DATASET_PATH),
        help="Path to the Story 96 helper-quality eval dataset JSON file.",
    )
    parser.add_argument(
        "--run-label",
        default="story96-helper-quality",
        help="Short label included in the structured output.",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Optional file path for structured JSON output.",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    dataset = load_story96_helper_eval_dataset(args.dataset)
    result = run_story96_helper_quality_eval(
        dataset=dataset,
        run_label=args.run_label,
    )

    if args.output:
        write_story96_helper_eval_output(output_path=args.output, result=result)

    comparison = result["comparison"]
    print(
        "Story 96 helper eval "
        f"cases={len(dataset.cases)} "
        f"winner={comparison['winner_candidate_id']}"
    )
    for candidate_result in result["candidates"]:
        summary = candidate_result["summary"]
        candidate = candidate_result["candidate"]
        print(
            f"{candidate['candidate_id']} "
            f"passed={summary['cases_passed']}/{summary['case_count']} "
            f"avg_overall={summary['average_overall_score']} "
            f"avg_groundedness={summary['average_groundedness_score']} "
            f"avg_citation={summary['average_evidence_citation_correctness_score']} "
            f"avg_unsupported={summary['average_unsupported_case_honesty_score']} "
            f"avg_usefulness={summary['average_usefulness_score']}"
        )
    if args.output:
        print(f"Wrote structured output to {args.output}")

    winning_candidate = next(
        candidate_result
        for candidate_result in result["candidates"]
        if candidate_result["candidate"]["candidate_id"] == comparison["winner_candidate_id"]
    )
    return 0 if winning_candidate["summary"]["all_cases_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
