import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


DEFAULT_OUTPUT_SUBDIR = Path("north-star") / "epic-reviews"
DEFAULT_TARGET_WORD_BUDGET = 3000
DEFAULT_MAX_WORD_BUDGET = 4000
ROADMAP_NOTE_GLOB = "roadmap-note-*.md"


class EpicReviewArtifactsError(ValueError):
    pass


@dataclass(frozen=True)
class ClosedStorySummary:
    number: int
    title: str
    scope_delivered: tuple[str, ...]
    validation: tuple[str, ...]
    follow_ups: tuple[str, ...]
    manual_guides: tuple[str, ...]


@dataclass(frozen=True)
class EpicReviewArtifacts:
    epic_title: str
    closeout_story_number: int
    packet_path: Path
    prompt_path: Path
    review_response_path: Path
    prompt_word_count: int
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class EpicReviewScratchpad:
    active_epic: str
    architecture_pressures: tuple[str, ...]
    implementation_friction: tuple[str, ...]
    design_decisions_changed: tuple[str, ...]
    interfaces_or_data_model_changed: tuple[str, ...]
    risks_before_next_epic: tuple[str, ...]


EMPTY_SCRATCHPAD = EpicReviewScratchpad(
    active_epic="",
    architecture_pressures=(),
    implementation_friction=(),
    design_decisions_changed=(),
    interfaces_or_data_model_changed=(),
    risks_before_next_epic=(),
)


def generate_epic_review_artifacts(
    *,
    repo_root: Path,
    epic_doc_path: str | Path,
    output_dir: str | Path | None = None,
    allow_empty_friction: bool = False,
    allow_oversize: bool = False,
    target_word_budget: int = DEFAULT_TARGET_WORD_BUDGET,
    max_word_budget: int = DEFAULT_MAX_WORD_BUDGET,
) -> EpicReviewArtifacts:
    resolved_repo_root = repo_root.resolve()
    epic_path = (resolved_repo_root / Path(epic_doc_path)).resolve()
    if not epic_path.exists():
        raise EpicReviewArtifactsError(f"Epic doc not found: {epic_path}")

    architecture_path = resolved_repo_root / "north-star" / "ARCHITECTURE.MD"
    active_context_path = resolved_repo_root / "north-star" / "ACTIVE_CONTEXT.md"
    jira_closed_path = resolved_repo_root / "north-star" / "JIRA_STORIES_CLOSED.md"
    for required_path in (architecture_path, active_context_path, jira_closed_path):
        if not required_path.exists():
            raise EpicReviewArtifactsError(f"Required doc not found: {required_path}")

    epic_text = epic_path.read_text(encoding="utf-8")
    architecture_text = architecture_path.read_text(encoding="utf-8").strip()
    active_context_text = active_context_path.read_text(encoding="utf-8")
    jira_closed_text = jira_closed_path.read_text(encoding="utf-8")

    epic_title = _extract_markdown_title(epic_text)
    epic_goal = _extract_labeled_value(epic_text, "Goal")
    epic_scope = tuple(_extract_bullets(_extract_labeled_block(epic_text, "Scope")))
    closeout_story_number = _extract_closeout_story_number(epic_text)
    story_numbers = _extract_story_numbers(epic_text)
    if closeout_story_number not in story_numbers:
        raise EpicReviewArtifactsError(
            f"Epic closeout story Story {closeout_story_number} is not listed in recommended story order for {epic_title!r}"
        )

    closed_stories = _parse_closed_story_summaries(
        jira_closed_text=jira_closed_text,
        story_numbers=story_numbers,
    )
    if closeout_story_number not in {story.number for story in closed_stories}:
        raise EpicReviewArtifactsError(
            f"Epic closeout story Story {closeout_story_number} is not closed in north-star/JIRA_STORIES_CLOSED.md"
        )

    scratchpad = _parse_epic_review_scratchpad(active_context_text=active_context_text)
    if scratchpad.active_epic and _normalize_space(scratchpad.active_epic) != _normalize_space(epic_title):
        if allow_empty_friction:
            warnings_for_mismatch = (
                f"Scratchpad active epic {scratchpad.active_epic!r} does not match requested epic {epic_title!r}; "
                "continuing with empty scratchpad because --allow-empty-friction was used."
            )
            scratchpad = EMPTY_SCRATCHPAD
        else:
            raise EpicReviewArtifactsError(
                f"ACTIVE_CONTEXT epic review scratchpad is for {scratchpad.active_epic!r}, not {epic_title!r}. "
                "Update the scratchpad or rerun with --allow-empty-friction."
            )
    else:
        warnings_for_mismatch = None
    if not allow_empty_friction and not scratchpad.implementation_friction:
        raise EpicReviewArtifactsError(
            "ACTIVE_CONTEXT epic review scratchpad is missing implementation friction bullets. "
            "Update the scratchpad or rerun with --allow-empty-friction."
        )

    closeout_story = next(story for story in closed_stories if story.number == closeout_story_number)
    upcoming_epics = tuple(
        _find_upcoming_epics(
            repo_root=resolved_repo_root,
            current_epic_title=epic_title,
        )
    )

    packet_text = _render_packet(
        generated_at=datetime.now(UTC),
        epic_title=epic_title,
        epic_goal=epic_goal,
        epic_doc_path=epic_path,
        closeout_story_number=closeout_story_number,
        epic_scope=epic_scope,
        architecture_text=architecture_text,
        closed_stories=closed_stories,
        closeout_story=closeout_story,
        scratchpad=scratchpad,
        upcoming_epics=upcoming_epics,
    )
    prompt_text = _render_prompt(packet_text=packet_text)
    prompt_word_count = count_words(prompt_text)
    warnings: list[str] = []
    if warnings_for_mismatch is not None:
        warnings.append(warnings_for_mismatch)
    if prompt_word_count > target_word_budget:
        warnings.append(
            f"Prompt word count {prompt_word_count} exceeds target budget {target_word_budget}"
        )
    if prompt_word_count > max_word_budget and not allow_oversize:
        raise EpicReviewArtifactsError(
            f"Prompt word count {prompt_word_count} exceeds max budget {max_word_budget}. "
            "Summarize the source docs further or rerun with --allow-oversize."
        )

    output_root = (
        (resolved_repo_root / DEFAULT_OUTPUT_SUBDIR)
        if output_dir is None
        else (resolved_repo_root / Path(output_dir))
    ).resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    file_stem = _build_output_stem(epic_title=epic_title, epic_doc_path=epic_path)
    packet_path = output_root / f"{file_stem}.packet.md"
    prompt_path = output_root / f"{file_stem}.gpt54pro.md"
    review_response_path = output_root / f"{file_stem}.review-response.md"

    packet_path.write_text(packet_text, encoding="utf-8")
    prompt_path.write_text(prompt_text, encoding="utf-8")
    review_response_path.write_text(
        _render_review_response_template(epic_title=epic_title),
        encoding="utf-8",
    )

    return EpicReviewArtifacts(
        epic_title=epic_title,
        closeout_story_number=closeout_story_number,
        packet_path=packet_path,
        prompt_path=prompt_path,
        review_response_path=review_response_path,
        prompt_word_count=prompt_word_count,
        warnings=tuple(warnings),
    )


def count_words(text: str) -> int:
    return len(re.findall(r"\S+", text))


def _extract_markdown_title(text: str) -> str:
    match = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    if match is None:
        raise EpicReviewArtifactsError("Epic doc is missing a top-level markdown title")
    return match.group(1).strip()


def _extract_labeled_value(text: str, label: str) -> str:
    inline_match = re.search(rf"^{re.escape(label)}:\s*(.+)$", text, re.MULTILINE)
    if inline_match is not None and inline_match.group(1).strip():
        return inline_match.group(1).strip()

    block_text = _extract_labeled_block(text, label)
    if not block_text:
        raise EpicReviewArtifactsError(f"Epic doc is missing {label}:")

    for line in block_text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped

    raise EpicReviewArtifactsError(f"Epic doc is missing {label}:")


def _extract_labeled_block(text: str, label: str) -> str:
    label_match = re.search(rf"^{re.escape(label)}:\s*$", text, re.MULTILINE)
    if label_match is None:
        return ""

    block_start = label_match.end()
    next_label_match = re.search(r"^[A-Z][A-Za-z0-9 .'/()\-]+:\s*$", text[block_start:], re.MULTILINE)
    if next_label_match is None:
        return text[block_start:].strip()

    return text[block_start : block_start + next_label_match.start()].strip()


def _extract_bullets(block_text: str) -> list[str]:
    bullets: list[str] = []
    for line in block_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            bullets.append(stripped[2:].strip())
    return bullets


def _extract_closeout_story_number(epic_text: str) -> int:
    match = re.search(r"^Epic closeout story:\s*Story\s+(\d+)\s*$", epic_text, re.MULTILINE)
    if match is None:
        raise EpicReviewArtifactsError(
            "Epic doc is missing 'Epic closeout story: Story N'."
        )
    return int(match.group(1))


def _extract_story_numbers(epic_text: str) -> tuple[int, ...]:
    if "Recommended story order:" not in epic_text:
        raise EpicReviewArtifactsError("Epic doc is missing 'Recommended story order:'")
    story_order_block = epic_text.split("Recommended story order:", 1)[1]
    ordered_lines: list[str] = []
    for line in story_order_block.splitlines():
        stripped = line.strip()
        if not stripped:
            if ordered_lines:
                break
            continue
        if re.match(r"^\d+\.\s+Story\s+\d+\b", stripped):
            ordered_lines.append(stripped)
            continue
        if ordered_lines:
            break

    story_numbers = [int(number) for number in re.findall(r"Story\s+(\d+)", "\n".join(ordered_lines))]
    if not story_numbers:
        raise EpicReviewArtifactsError("No story numbers found under 'Recommended story order:'")
    ordered_unique_numbers: list[int] = []
    for story_number in story_numbers:
        if story_number not in ordered_unique_numbers:
            ordered_unique_numbers.append(story_number)
    return tuple(ordered_unique_numbers)


def _parse_closed_story_summaries(
    *,
    jira_closed_text: str,
    story_numbers: tuple[int, ...],
) -> tuple[ClosedStorySummary, ...]:
    requested_story_numbers = set(story_numbers)
    story_entries = list(re.finditer(r"^## Story (\d+) — (.+)$", jira_closed_text, re.MULTILINE))
    summaries: dict[int, ClosedStorySummary] = {}

    for index, match in enumerate(story_entries):
        story_number = int(match.group(1))
        if story_number not in requested_story_numbers:
            continue

        start = match.start()
        end = story_entries[index + 1].start() if index + 1 < len(story_entries) else len(jira_closed_text)
        entry_text = jira_closed_text[start:end]
        summaries[story_number] = ClosedStorySummary(
            number=story_number,
            title=match.group(2).strip(),
            scope_delivered=tuple(_extract_entry_section_bullets(entry_text, "Scope delivered")),
            validation=tuple(_extract_entry_section_bullets(entry_text, "Validation")),
            follow_ups=tuple(_extract_entry_section_bullets(entry_text, "Follow-ups")),
            manual_guides=tuple(sorted(set(re.findall(r"north-star/story\d+-manual\.md", entry_text)))),
        )

    missing_story_numbers = [number for number in story_numbers if number not in summaries]
    if missing_story_numbers:
        missing_text = ", ".join(f"Story {number}" for number in missing_story_numbers)
        raise EpicReviewArtifactsError(
            f"Missing closed story entries in north-star/JIRA_STORIES_CLOSED.md: {missing_text}"
        )

    return tuple(summaries[number] for number in story_numbers)


def _extract_entry_section_bullets(entry_text: str, section_name: str) -> list[str]:
    lines = entry_text.splitlines()
    section_header = f"- {section_name}:"
    in_section = False
    bullets: list[str] = []

    for line in lines:
        if line == section_header:
            in_section = True
            continue

        if in_section and re.match(r"^- [^ ].*:$", line):
            break

        if in_section and line.startswith("  - "):
            bullets.append(line[4:].strip())

    return bullets


def _parse_epic_review_scratchpad(*, active_context_text: str) -> EpicReviewScratchpad:
    scratchpad_body = _extract_markdown_section(
        text=active_context_text,
        heading="Epic Review Scratchpad",
        level=2,
    )
    if scratchpad_body is None:
        raise EpicReviewArtifactsError(
            "ACTIVE_CONTEXT.md is missing the '## Epic Review Scratchpad' section."
        )

    return EpicReviewScratchpad(
        active_epic=_extract_first_bullet_from_section(
            _extract_markdown_section(scratchpad_body, "Active Epic", level=3)
        ),
        architecture_pressures=tuple(
            _extract_bullets(
                _extract_markdown_section(scratchpad_body, "Architecture Pressures Observed", level=3)
                or ""
            )
        ),
        implementation_friction=tuple(
            _extract_bullets(
                _extract_markdown_section(scratchpad_body, "Implementation Friction", level=3) or ""
            )
        ),
        design_decisions_changed=tuple(
            _extract_bullets(
                _extract_markdown_section(
                    scratchpad_body,
                    "Design Decisions Changed During The Epic",
                    level=3,
                )
                or ""
            )
        ),
        interfaces_or_data_model_changed=tuple(
            _extract_bullets(
                _extract_markdown_section(
                    scratchpad_body,
                    "Interfaces Or Data Model Changed",
                    level=3,
                )
                or ""
            )
        ),
        risks_before_next_epic=tuple(
            _extract_bullets(
                _extract_markdown_section(scratchpad_body, "Risks Before Next Epic", level=3)
                or ""
            )
        ),
    )


def _extract_markdown_section(text: str, heading: str, *, level: int) -> str | None:
    hashes = "#" * level
    pattern = re.compile(
        rf"^{re.escape(hashes)}\s+{re.escape(heading)}\s*$\n(?P<body>.*?)(?=^{re.escape(hashes)}\s+|^#{{1,{level-1}}}\s+|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text)
    if match is None:
        return None
    return match.group("body").strip()


def _extract_first_bullet_from_section(section_text: str | None) -> str:
    if not section_text:
        return ""
    bullets = _extract_bullets(section_text)
    return bullets[0] if bullets else ""


def _find_upcoming_epics(*, repo_root: Path, current_epic_title: str) -> list[str]:
    roadmap_notes = sorted((repo_root / "north-star").glob(ROADMAP_NOTE_GLOB))
    if roadmap_notes:
        roadmap_text = roadmap_notes[-1].read_text(encoding="utf-8")
        roadmap_entries = re.findall(r"^\d+\.\s+(Phase\s+.+)$", roadmap_text, re.MULTILINE)
        if roadmap_entries:
            normalized_current_title = _normalize_space(current_epic_title)
            for index, entry in enumerate(roadmap_entries):
                if _normalize_space(entry) == normalized_current_title:
                    return roadmap_entries[index + 1 : index + 4]
            return roadmap_entries[:3]

    return []


def _normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def _render_packet(
    *,
    generated_at: datetime,
    epic_title: str,
    epic_goal: str,
    epic_doc_path: Path,
    closeout_story_number: int,
    epic_scope: tuple[str, ...],
    architecture_text: str,
    closed_stories: tuple[ClosedStorySummary, ...],
    closeout_story: ClosedStorySummary,
    scratchpad: EpicReviewScratchpad,
    upcoming_epics: tuple[str, ...],
) -> str:
    scope_bullets = _render_bullets(epic_scope)
    story_summaries = "\n".join(_render_story_summary(story) for story in closed_stories)
    manual_guides = sorted({guide for story in closed_stories for guide in story.manual_guides})
    validation_bullets = closeout_story.validation or (
        f"Story {closeout_story.number} is closed, but no validation bullets were parsed from JIRA closeout.",
    )
    follow_up_bullets = closeout_story.follow_ups or scratchpad.risks_before_next_epic
    generated_at_text = generated_at.strftime("%Y-%m-%d %H:%M UTC")

    return (
        f"# {epic_title} Review Packet\n\n"
        f"- Generated at: {generated_at_text}\n"
        f"- Epic doc: `{epic_doc_path.as_posix()}`\n"
        f"- Epic closeout story: Story {closeout_story_number}\n"
        f"- Scratchpad active epic: {scratchpad.active_epic or 'not set'}\n\n"
        f"## Current Architecture Snapshot\n\n"
        f"{architecture_text}\n\n"
        f"## Epic Completed\n\n"
        f"- Epic: {epic_title}\n"
        f"- Goal: {epic_goal}\n"
        f"- Stories completed in this epic: {', '.join(f'Story {story.number}' for story in closed_stories)}\n"
        f"- Epic scope:\n{scope_bullets}\n\n"
        f"## Epic Stories Delivered\n\n"
        f"{story_summaries}\n\n"
        f"## Validation Completed\n\n"
        f"- Closeout story validation:\n{_render_bullets(validation_bullets)}\n"
        f"- Manual guides available:\n{_render_bullets(manual_guides or ['none found'])}\n\n"
        f"## Implementation Reality\n\n"
        f"### Architecture Pressures Observed\n"
        f"{_render_bullets(scratchpad.architecture_pressures or ('none recorded',))}\n\n"
        f"### Implementation Friction\n"
        f"{_render_bullets(scratchpad.implementation_friction or ('none recorded',))}\n\n"
        f"### Design Decisions Changed During The Epic\n"
        f"{_render_bullets(scratchpad.design_decisions_changed or ('none recorded',))}\n\n"
        f"### Interfaces Or Data Model Changed\n"
        f"{_render_bullets(scratchpad.interfaces_or_data_model_changed or ('none recorded',))}\n\n"
        f"## Known Debt / Unresolved Gaps\n\n"
        f"{_render_bullets(follow_up_bullets or ('none recorded',))}\n\n"
        f"## Upcoming Epics\n\n"
        f"{_render_bullets(upcoming_epics or ('No roadmap-based upcoming epics were derived.',))}\n"
    )


def _render_story_summary(story: ClosedStorySummary) -> str:
    scope_bullets = _render_bullets(story.scope_delivered or ("no scope-delivered bullets recorded",))
    return f"### Story {story.number} — {story.title}\n{scope_bullets}"


def _render_prompt(*, packet_text: str) -> str:
    return (
        "# GPT 5.4 Pro Architecture Review Prompt\n\n"
        "Please perform a senior/staff engineer architecture review.\n\n"
        "Use the review packet below as the full source of truth. Do not assume any missing implementation details beyond what is stated. "
        "Prefer concrete risks, trade-offs, and decisions over generic advice. "
        "Do not assume every risk should become new pre-beta scope; distinguish true beta blockers from adjustments to already-planned work and from post-beta follow-ups.\n\n"
        "## Required Answer Format\n\n"
        "Respond with these sections in this order:\n\n"
        "1. Architecture risks\n"
        "2. Scaling risks\n"
        "3. Simplifications\n"
        "4. Debt to address now\n"
        "5. Decisions before next epic\n"
        "6. Top 3 recommended actions\n\n"
        "When possible, make clear which recommendations are true pre-beta blockers versus post-beta improvements.\n\n"
        "## Review Questions\n\n"
        "1. What parts of the system design are likely to become fragile as more epics are added?\n"
        "2. What scaling risks are present if this system grows 10x-100x?\n"
        "3. Where has complexity increased more than necessary?\n"
        "4. What technical or architectural debt should be addressed now rather than later?\n"
        "5. What design decisions should be made before the next epics are implemented?\n"
        "6. Are there any event ordering or race condition risks?\n"
        "7. Are there improvements to the data model or component boundaries?\n"
        "8. If you were the staff engineer responsible for this system, what would worry you most right now?\n\n"
        "## Review Packet\n\n"
        f"{packet_text.strip()}\n"
    )


def _render_review_response_template(*, epic_title: str) -> str:
    return (
        f"# {epic_title} Review Response\n\n"
        "## Raw GPT Review\n\n"
        "_Paste the full GPT 5.4 Pro response here._\n\n"
        "## Beta-Freeze Classification\n\n"
        "- Freeze status: `pre-freeze` or `post-freeze`\n"
        "- New Pre-Beta Blockers:\n"
        "  - \n"
        "- Adjust Existing Planned Stories:\n"
        "  - \n"
        "- Post-Beta Follow-ups:\n"
        "  - \n\n"
        "## Accepted Recommendations\n\n"
        "- \n\n"
        "## Deferred Or Rejected Recommendations\n\n"
        "- \n\n"
        "## Roadmap / Next Epic Changes\n\n"
        "- \n\n"
        "## New Follow-ups\n\n"
        "- \n"
    )


def _build_output_stem(*, epic_title: str, epic_doc_path: Path) -> str:
    phase_match = re.search(r"(phase[0-9.]+)", epic_doc_path.name.lower())
    phase_prefix = phase_match.group(1) if phase_match else "epic"
    normalized_title = epic_title.lower()
    normalized_title = re.sub(r"^phase\s+[0-9.]+\s+epic\s+[—-]\s+", "", normalized_title)
    normalized_title = re.sub(r"[^a-z0-9]+", "-", normalized_title).strip("-")
    return f"{phase_prefix}-{normalized_title}"


def _render_bullets(items: tuple[str, ...] | list[str]) -> str:
    return "\n".join(f"- {item}" for item in items)
