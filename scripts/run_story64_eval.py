import argparse
import sys

from app.db.session import SessionLocal
from app.evals.content_pipeline import (
    DEFAULT_STORY64_DATASET_PATH,
    load_story64_seed_dataset,
    run_story64_content_pipeline_eval,
    write_story64_eval_output,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Story 64 Phase 11 content eval harness.",
    )
    parser.add_argument(
        "--dataset",
        default=str(DEFAULT_STORY64_DATASET_PATH),
        help="Path to the Story 64 eval dataset JSON file.",
    )
    parser.add_argument(
        "--run-label",
        default="story64-baseline",
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
    dataset = load_story64_seed_dataset(args.dataset)

    with SessionLocal() as db:
        result = run_story64_content_pipeline_eval(
            db=db,
            dataset=dataset,
            run_label=args.run_label,
        )

    if args.output:
        write_story64_eval_output(output_path=args.output, result=result)

    summary = result["summary"]
    print(
        "Story 64 eval "
        f"cases={summary['case_count']} "
        f"passed={summary['cases_passed']} "
        f"all_passed={summary['all_cases_passed']} "
        f"avg_extraction={summary['average_extraction_score']} "
        f"avg_topic={summary['average_topic_suggestion_score']}"
    )
    if args.output:
        print(f"Wrote structured output to {args.output}")

    return 0 if summary["all_cases_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
