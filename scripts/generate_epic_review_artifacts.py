import argparse
import sys
from pathlib import Path

from app.services.epic_review_artifacts import (
    DEFAULT_MAX_WORD_BUDGET,
    DEFAULT_TARGET_WORD_BUDGET,
    EpicReviewArtifactsError,
    generate_epic_review_artifacts,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate the single-paste GPT 5.4 Pro architecture review prompt and companion artifacts "
            "for a completed epic."
        )
    )
    parser.add_argument(
        "--epic-doc",
        required=True,
        help="Repo-relative path to the epic markdown doc, for example north-star/phase7-epic-invoice-creation.md",
    )
    parser.add_argument(
        "--output-dir",
        help="Repo-relative output directory. Defaults to north-star/epic-reviews.",
    )
    parser.add_argument(
        "--allow-empty-friction",
        action="store_true",
        help="Allow generation even if ACTIVE_CONTEXT epic scratchpad has no implementation friction bullets.",
    )
    parser.add_argument(
        "--allow-oversize",
        action="store_true",
        help="Allow generation even if the GPT prompt exceeds the max word budget.",
    )
    parser.add_argument(
        "--target-word-budget",
        type=int,
        default=DEFAULT_TARGET_WORD_BUDGET,
        help=f"Prompt word count target before warning. Default: {DEFAULT_TARGET_WORD_BUDGET}",
    )
    parser.add_argument(
        "--max-word-budget",
        type=int,
        default=DEFAULT_MAX_WORD_BUDGET,
        help=f"Prompt word count ceiling before failure. Default: {DEFAULT_MAX_WORD_BUDGET}",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    repo_root = Path(__file__).resolve().parents[1]

    try:
        artifacts = generate_epic_review_artifacts(
            repo_root=repo_root,
            epic_doc_path=args.epic_doc,
            output_dir=args.output_dir,
            allow_empty_friction=args.allow_empty_friction,
            allow_oversize=args.allow_oversize,
            target_word_budget=args.target_word_budget,
            max_word_budget=args.max_word_budget,
        )
    except EpicReviewArtifactsError as exc:
        print(f"error={exc}", file=sys.stderr)
        return 2

    print(f"epic_title={artifacts.epic_title}")
    print(f"closeout_story={artifacts.closeout_story_key}")
    print(f"packet_path={artifacts.packet_path}")
    print(f"prompt_path={artifacts.prompt_path}")
    print(f"review_response_path={artifacts.review_response_path}")
    print(f"prompt_word_count={artifacts.prompt_word_count}")
    for warning in artifacts.warnings:
        print(f"warning={warning}")
    print(
        "next_step=Paste the .gpt54pro.md file into GPT 5.4 Pro, then paste the response into the .review-response.md file and classify findings into blockers, planned-story adjustments, or post-beta follow-ups before starting the next epic"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
