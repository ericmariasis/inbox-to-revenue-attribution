from pathlib import Path
import textwrap

import pytest

from app.services.epic_review_artifacts import EpicReviewArtifactsError, generate_epic_review_artifacts


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")


def _build_minimal_repo(tmp_path: Path, *, implementation_friction: tuple[str, ...] = ("sample friction",)) -> Path:
    repo_root = tmp_path / "repo"

    _write_text(
        repo_root / "north-star" / "ARCHITECTURE.MD",
        """
        # Sample Architecture

        ## Components
        - FastAPI app
        - Postgres

        ## Data Model
        - Sample entity
        """,
    )
    _write_text(
        repo_root / "north-star" / "ACTIVE_CONTEXT.md",
        f"""
        # Active Context

        ## Epic Review Scratchpad

        ### Active Epic
        - Phase 1 Epic — Sample Epic

        ### Architecture Pressures Observed
        - sample pressure

        ### Implementation Friction
        {"".join(f"- {item}\\n" for item in implementation_friction)}
        ### Design Decisions Changed During The Epic
        - made sample decision

        ### Interfaces Or Data Model Changed
        - added Sample entity

        ### Risks Before Next Epic
        - next epic risk
        """,
    )
    _write_text(
        repo_root / "north-star" / "JIRA_STORIES_CLOSED.md",
        """
        # Jira Stories Closed

        ## Story 1 — Sample model
        - Status: Closed
        - Scope delivered:
          - Added a sample model
        - Validation:
          - pytest -q tests/test_sample_model.py passing (`1 passed`)
        - Follow-ups:
          - Story 2 closes the epic

        ## Story 2 — Phase 1 validation scenario
        - Status: Closed
        - Scope delivered:
          - Added an epic validation scenario
          - Added `north-star/story2-manual.md`
        - Validation:
          - pytest -q tests/test_phase1_validation.py passing (`2 passed`)
        - Follow-ups:
          - Start Phase 2
        """,
    )
    _write_text(
        repo_root / "north-star" / "phase1-epic-sample.md",
        """
        # Phase 1 Epic — Sample Epic

        Goal:
        Ship sample capability.

        Scope:
        - add sample capability
        - keep the implementation narrow

        Epic closeout story: Story 2

        Recommended story order:

        1. Story 1 — Sample model
        2. Story 2 — Phase 1 validation scenario
        """,
    )
    _write_text(
        repo_root / "north-star" / "roadmap-note-2026-03-08.md",
        """
        # Roadmap Note

        Recommended execution order from here:

        1. Phase 1 Epic — Sample Epic
        2. Phase 2 Epic — Next Epic
        3. Phase 3 Epic — Later Epic
        4. Phase 4 Epic — Even Later
        """,
    )

    return repo_root


def test_generate_epic_review_artifacts_writes_packet_prompt_and_response_files(tmp_path: Path):
    repo_root = _build_minimal_repo(tmp_path)

    artifacts = generate_epic_review_artifacts(
        repo_root=repo_root,
        epic_doc_path="north-star/phase1-epic-sample.md",
        output_dir="north-star/epic-reviews",
    )

    assert artifacts.closeout_story_number == 2
    assert artifacts.packet_path.exists()
    assert artifacts.prompt_path.exists()
    assert artifacts.review_response_path.exists()
    assert artifacts.prompt_word_count > 0

    packet_text = artifacts.packet_path.read_text(encoding="utf-8")
    prompt_text = artifacts.prompt_path.read_text(encoding="utf-8")
    review_response_text = artifacts.review_response_path.read_text(encoding="utf-8")

    assert "## Current Architecture Snapshot" in packet_text
    assert "### Story 1 — Sample model" in packet_text
    assert "sample friction" in packet_text
    assert "Phase 2 Epic — Next Epic" in packet_text

    assert "Use the review packet below as the full source of truth" in prompt_text
    assert "What parts of the system design are likely to become fragile" in prompt_text
    assert "If you were the staff engineer responsible for this system" in prompt_text
    assert "Do not assume every risk should become new pre-beta scope" in prompt_text
    assert "sample friction" in prompt_text

    assert "## Raw GPT Review" in review_response_text
    assert "## Beta-Freeze Classification" in review_response_text
    assert "New Pre-Beta Blockers" in review_response_text
    assert "## Accepted Recommendations" in review_response_text


def test_generate_epic_review_artifacts_requires_closed_closeout_story(tmp_path: Path):
    repo_root = _build_minimal_repo(tmp_path)
    epic_doc_path = repo_root / "north-star" / "phase1-epic-sample.md"
    epic_doc_path.write_text(
        epic_doc_path.read_text(encoding="utf-8").replace(
            "Epic closeout story: Story 2",
            "Epic closeout story: Story 3",
        ),
        encoding="utf-8",
    )

    with pytest.raises(EpicReviewArtifactsError, match="closeout story Story 3"):
        generate_epic_review_artifacts(
            repo_root=repo_root,
            epic_doc_path="north-star/phase1-epic-sample.md",
            output_dir="north-star/epic-reviews",
        )


def test_generate_epic_review_artifacts_requires_friction_by_default(tmp_path: Path):
    repo_root = _build_minimal_repo(tmp_path, implementation_friction=())

    with pytest.raises(EpicReviewArtifactsError, match="implementation friction"):
        generate_epic_review_artifacts(
            repo_root=repo_root,
            epic_doc_path="north-star/phase1-epic-sample.md",
            output_dir="north-star/epic-reviews",
        )


def test_generate_epic_review_artifacts_enforces_max_word_budget(tmp_path: Path):
    repo_root = _build_minimal_repo(tmp_path)

    with pytest.raises(EpicReviewArtifactsError, match="exceeds max budget"):
        generate_epic_review_artifacts(
            repo_root=repo_root,
            epic_doc_path="north-star/phase1-epic-sample.md",
            output_dir="north-star/epic-reviews",
            max_word_budget=10,
        )


def test_generate_epic_review_artifacts_warns_when_prompt_exceeds_target_word_budget(tmp_path: Path):
    repo_root = _build_minimal_repo(tmp_path)

    artifacts = generate_epic_review_artifacts(
        repo_root=repo_root,
        epic_doc_path="north-star/phase1-epic-sample.md",
        output_dir="north-star/epic-reviews",
        target_word_budget=10,
        max_word_budget=10000,
    )

    assert artifacts.warnings
    assert "exceeds target budget" in artifacts.warnings[0]


def test_generate_epic_review_artifacts_can_ignore_mismatched_scratchpad_with_allow_empty_friction(tmp_path: Path):
    repo_root = _build_minimal_repo(tmp_path)
    active_context_path = repo_root / "north-star" / "ACTIVE_CONTEXT.md"
    active_context_path.write_text(
        active_context_path.read_text(encoding="utf-8").replace(
            "Phase 1 Epic — Sample Epic",
            "Phase 9 Epic — Different Epic",
            1,
        ),
        encoding="utf-8",
    )

    artifacts = generate_epic_review_artifacts(
        repo_root=repo_root,
        epic_doc_path="north-star/phase1-epic-sample.md",
        output_dir="north-star/epic-reviews",
        allow_empty_friction=True,
    )

    assert artifacts.warnings
    assert "does not match requested epic" in artifacts.warnings[0]


def test_generate_epic_review_artifacts_ignores_follow_on_story_mentions_outside_ordered_list(tmp_path: Path):
    repo_root = _build_minimal_repo(tmp_path)
    epic_doc_path = repo_root / "north-star" / "phase1-epic-sample.md"
    epic_doc_path.write_text(
        epic_doc_path.read_text(encoding="utf-8")
        + textwrap.dedent(
            """

            Immediate follow-on after this epic:

            - Story 99 — Future helper
              - depends on Stories 1-2 landing cleanly
            """
        ),
        encoding="utf-8",
    )

    artifacts = generate_epic_review_artifacts(
        repo_root=repo_root,
        epic_doc_path="north-star/phase1-epic-sample.md",
        output_dir="north-star/epic-reviews",
    )

    assert artifacts.closeout_story_number == 2
    assert artifacts.packet_path.exists()


def test_generate_epic_review_artifacts_supports_prefixed_story_keys(tmp_path: Path):
    repo_root = _build_minimal_repo(tmp_path)
    _write_text(
        repo_root / "north-star" / "JIRA_STORIES_CLOSED.md",
        """
        # Jira Stories Closed

        ## FS-1 — FullScope contract proof spike
        - Status: Closed locally
        - Scope delivered:
          - captured the provider-backed attribution contract
        - Validation:
          - local replay notes captured
        - Follow-ups:
          - FS-2 widens creator and operator surfaces

        ## FS-2 — Reports and health closeout
        - Status: Closed locally
        - Scope delivered:
          - widened reporting and health copy
          - added `north-star/fullscope-fs2-manual.md`
        - Validation:
          - pytest -q tests/test_fullscope_validation.py passing (`3 passed`)
        - Follow-ups:
          - run the epic review gate
        """,
    )
    _write_text(
        repo_root / "north-star" / "fullscope-booking-integration-v1.md",
        """
        # FullScope Booking Integration V1

        Goal:
        Ship a narrow FullScope integration.

        Scope:
        - Personal Calendar support
        - direct Service Calendar support

        Epic closeout story: FS-2

        Recommended story order:

        1. FS-1 — FullScope contract proof spike
        2. FS-2 — Reports and health closeout
        """,
    )
    active_context_path = repo_root / "north-star" / "ACTIVE_CONTEXT.md"
    active_context_path.write_text(
        active_context_path.read_text(encoding="utf-8").replace(
            "Phase 1 Epic — Sample Epic",
            "FullScope Booking Integration V1",
            1,
        ),
        encoding="utf-8",
    )

    artifacts = generate_epic_review_artifacts(
        repo_root=repo_root,
        epic_doc_path="north-star/fullscope-booking-integration-v1.md",
        output_dir="north-star/epic-reviews",
    )

    assert artifacts.closeout_story_key == "FS-2"
    assert artifacts.closeout_story_number is None

    packet_text = artifacts.packet_path.read_text(encoding="utf-8")
    assert "Epic closeout story: FS-2" in packet_text
    assert "### FS-1 — FullScope contract proof spike" in packet_text
    assert "north-star/fullscope-fs2-manual.md" in packet_text
