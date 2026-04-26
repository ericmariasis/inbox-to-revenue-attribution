import json
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from urllib import error as urllib_error
from urllib import request as urllib_request
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, object_session, selectinload

from app.core.config import Settings, get_settings
from app.models.creator_operator_experiment_draft_run import (
    CreatorOperatorExperimentDraftRunRecord,
)
from app.models.creator_operator_experiment_draft_run_card import (
    CreatorOperatorExperimentDraftRunCardRecord,
)
from app.services.creator_claim_snapshots import (
    CreateCreatorClaimSnapshotInput,
    create_creator_claim_snapshot,
    resolve_creator_claim_snapshot,
)
from app.services.next_content_experiments import (
    CURRENT_NEXT_CONTENT_EXPERIMENTS_FRESHNESS_POLICY,
    EXPERIMENT_EVIDENCE_INPUT_VERSION,
    EXPERIMENT_RUN_STATUS_READY,
    HelperFreshnessPolicy,
    HelperGenerationLineage,
    HelperVersionSemantics,
    NextContentExperimentPaidEvidenceDetail,
    _build_experiment_candidates,
    _candidate_sort_key,
    _format_money_from_cents,
    _format_revenue_summary,
)
from app.services.settled_paid_evidence import get_creator_settled_paid_evidence

OPERATOR_EXPERIMENT_DRAFT_GENERATOR_TYPE = "llm_assisted"
OPERATOR_EXPERIMENT_DRAFT_RUN_CONTRACT_VERSION = (
    "operator_draft_next_content_experiments.v1"
)
OPERATOR_EXPERIMENT_DRAFT_RUN_REDUCER_VERSION = (
    "operator_draft_next_content_experiments.reducer.v1"
)
OPERATOR_EXPERIMENT_DRAFT_RUN_CONFIG_VERSION = (
    "operator_draft_next_content_experiments.openai_responses.v1"
)
OPERATOR_EXPERIMENT_DRAFT_RESULT_SCHEMA_VERSION = (
    "operator_draft_next_content_experiments.result.v1"
)
OPERATOR_EXPERIMENT_DRAFT_CARD_CLAIM_KIND = (
    "operator_draft_next_content_experiment_card"
)
OPERATOR_EXPERIMENT_DRAFT_CARD_CLAIM_CONTRACT_VERSION = (
    "operator_draft_next_content_experiment_card.v1"
)
OPERATOR_EXPERIMENT_DRAFT_CARD_CLAIM_CONFIG_VERSION = (
    "operator_draft_next_content_experiment_card.openai_responses.v1"
)
MAX_OPERATOR_EXPERIMENT_DRAFT_CARDS = 5
OPERATOR_EXPERIMENT_DRAFT_CARD_CAUTION = (
    "Treat this as a draft hypothesis pending operator review. It is grounded "
    "in one authoritative content pattern and this creator's settled paid "
    "results for one tracked post."
)


class OperatorExperimentDraftUnavailableError(RuntimeError):
    pass


class OperatorExperimentDraftNotReadyError(RuntimeError):
    pass


class OperatorExperimentDraftProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class OperatorExperimentDraftPromptCandidate:
    content_id: UUID
    content_tid: str
    source_url: str
    authoritative_title: str | None
    authoritative_topics: list[str]
    authoritative_excerpt: str | None
    paid_booking_count: int
    paid_invoice_count: int
    paid_revenue_cents: int
    paid_revenue_summary: str
    last_paid_at: datetime


@dataclass(frozen=True)
class OperatorExperimentDraftPromptInput:
    creator_id: UUID
    creator_name: str
    candidates: list[OperatorExperimentDraftPromptCandidate]


class _OperatorExperimentDraftCardPayload(BaseModel):
    content_tid: str
    title: str
    hypothesis: str
    why_this_might_work: str
    evidence_summary: str
    ranking_rationale: str


class _OperatorExperimentDraftResponsePayload(BaseModel):
    cards: list[_OperatorExperimentDraftCardPayload] = Field(
        min_length=1,
        max_length=MAX_OPERATOR_EXPERIMENT_DRAFT_CARDS,
    )


@dataclass(frozen=True)
class OperatorExperimentDraftProviderOutput:
    model_name: str
    prompt_version: str
    cards: list[_OperatorExperimentDraftCardPayload]


class OperatorExperimentDraftProvider(Protocol):
    def is_configured(self) -> bool: ...

    def generate_draft(
        self,
        *,
        prompt_input: OperatorExperimentDraftPromptInput,
    ) -> OperatorExperimentDraftProviderOutput: ...


@dataclass(frozen=True)
class OperatorExperimentDraftCard:
    claim_snapshot_id: UUID
    card_order: int
    title: str
    hypothesis: str
    why_this_might_work: str
    evidence_summary: str
    caution: str
    ranking_rationale: str | None
    content_tid: str
    authoritative_source_url: str
    authoritative_artifact_title: str | None
    authoritative_topics: list[str]
    settled_paid_results: list[NextContentExperimentPaidEvidenceDetail]
    lineage: HelperGenerationLineage


@dataclass(frozen=True)
class CreatorOperatorExperimentDraftRunResult:
    draft_run_id: UUID
    status: str
    summary: str
    lineage: HelperGenerationLineage
    version_semantics: HelperVersionSemantics
    freshness_policy: HelperFreshnessPolicy
    cards: list[OperatorExperimentDraftCard]
    created_at: datetime


class OpenAIResponsesOperatorExperimentDraftProvider:
    def __init__(
        self,
        *,
        api_key: str,
        api_base_url: str,
        model_name: str,
        prompt_version: str,
        timeout_seconds: int,
    ):
        self._api_key = api_key.strip()
        self._api_base_url = api_base_url.rstrip("/")
        self._model_name = model_name.strip()
        self._prompt_version = prompt_version.strip()
        self._timeout_seconds = timeout_seconds

    def is_configured(self) -> bool:
        return bool(self._api_key and self._model_name and self._prompt_version)

    def generate_draft(
        self,
        *,
        prompt_input: OperatorExperimentDraftPromptInput,
    ) -> OperatorExperimentDraftProviderOutput:
        if not self.is_configured():
            raise OperatorExperimentDraftUnavailableError(
                "OpenAI draft provider is not configured"
            )

        body = {
            "model": self._model_name,
            "instructions": _operator_draft_instructions(
                prompt_version=self._prompt_version,
                max_cards=min(
                    MAX_OPERATOR_EXPERIMENT_DRAFT_CARDS,
                    len(prompt_input.candidates),
                ),
            ),
            "input": json.dumps(
                _operator_draft_prompt_payload(prompt_input=prompt_input),
                separators=(",", ":"),
            ),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "operator_experiment_draft_cards",
                    "strict": True,
                    "schema": _operator_draft_response_schema(
                        candidate_tids=[candidate.content_tid for candidate in prompt_input.candidates]
                    ),
                }
            },
        }
        request = urllib_request.Request(
            url=f"{self._api_base_url}/responses",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib_request.urlopen(request, timeout=self._timeout_seconds) as response:
                response_body = response.read().decode("utf-8")
        except urllib_error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise OperatorExperimentDraftProviderError(
                f"OpenAI draft generation failed with status {exc.code}: {error_body}"
            ) from exc
        except urllib_error.URLError as exc:
            raise OperatorExperimentDraftProviderError(
                f"OpenAI draft generation could not reach the API: {exc.reason}"
            ) from exc

        try:
            response_payload = json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise OperatorExperimentDraftProviderError(
                "OpenAI draft generation returned invalid JSON"
            ) from exc

        if response_payload.get("status") != "completed":
            raise OperatorExperimentDraftProviderError(
                "OpenAI draft generation did not complete successfully"
            )

        output_text = _extract_openai_output_text(response_payload)
        try:
            parsed_output = _OperatorExperimentDraftResponsePayload.model_validate_json(
                output_text
            )
        except ValidationError as exc:
            raise OperatorExperimentDraftProviderError(
                "OpenAI draft generation returned invalid structured output"
            ) from exc

        return OperatorExperimentDraftProviderOutput(
            model_name=self._model_name,
            prompt_version=self._prompt_version,
            cards=parsed_output.cards,
        )


def build_default_operator_experiment_draft_provider(
    *,
    settings: Settings | None = None,
) -> OperatorExperimentDraftProvider:
    resolved_settings = settings or get_settings()
    return OpenAIResponsesOperatorExperimentDraftProvider(
        api_key=resolved_settings.openai_api_key,
        api_base_url=resolved_settings.openai_api_base_url,
        model_name=resolved_settings.operator_experiment_draft_model,
        prompt_version=resolved_settings.operator_experiment_draft_prompt_version,
        timeout_seconds=resolved_settings.operator_experiment_draft_timeout_seconds,
    )


def create_creator_operator_experiment_draft_run(
    *,
    creator_id: UUID,
    creator_name: str,
    db: Session,
    provider: OperatorExperimentDraftProvider,
) -> CreatorOperatorExperimentDraftRunResult:
    if not provider.is_configured():
        raise OperatorExperimentDraftUnavailableError(
            "OpenAI draft provider is not configured"
        )

    settled_snapshot = get_creator_settled_paid_evidence(
        creator_id=creator_id,
        db=db,
    )
    selected_candidates = sorted(
        _build_experiment_candidates(
            creator_id=creator_id,
            settled_paid_rows=settled_snapshot.settled_rows,
            db=db,
        ),
        key=_candidate_sort_key,
    )[:MAX_OPERATOR_EXPERIMENT_DRAFT_CARDS]
    if not selected_candidates:
        raise OperatorExperimentDraftNotReadyError(
            "current evidence is not ready for operator draft generation"
        )

    prompt_input = OperatorExperimentDraftPromptInput(
        creator_id=creator_id,
        creator_name=creator_name,
        candidates=[
            _build_prompt_candidate(candidate)
            for candidate in selected_candidates
        ],
    )
    provider_output = provider.generate_draft(prompt_input=prompt_input)
    validated_cards = _validate_provider_cards(
        cards=provider_output.cards,
        allowed_tids={candidate.content.tid for candidate in selected_candidates},
    )

    generation_spec = _current_operator_experiment_generation_spec(
        model_name=provider_output.model_name,
        prompt_version=provider_output.prompt_version,
    )
    run_record = CreatorOperatorExperimentDraftRunRecord(
        creator_id=creator_id,
        status=EXPERIMENT_RUN_STATUS_READY,
        summary_text=_operator_draft_summary(card_count=len(validated_cards)),
        run_generator_type=generation_spec.run_lineage.generator_type,
        run_model_name=generation_spec.run_lineage.model_name,
        run_config_version=generation_spec.run_lineage.config_version,
        run_contract_version=generation_spec.run_lineage.contract_version,
        run_reducer_version=generation_spec.run_lineage.reducer_version,
        run_prompt_version=generation_spec.run_lineage.prompt_version,
    )

    candidate_by_tid = {candidate.content.tid: candidate for candidate in selected_candidates}
    db.add(run_record)
    db.flush()

    for index, card_payload in enumerate(validated_cards, start=1):
        candidate = candidate_by_tid[card_payload.content_tid]
        claim_snapshot = create_creator_claim_snapshot(
            creator_id=creator_id,
            input=CreateCreatorClaimSnapshotInput(
                claim_kind=OPERATOR_EXPERIMENT_DRAFT_CARD_CLAIM_KIND,
                content_id=candidate.content.id,
                authoritative_extraction_artifact_id=candidate.authoritative_evidence.artifact.id,
                authoritative_fetch_snapshot_id=candidate.authoritative_evidence.fetch_snapshot.id,
                settled_paid_evidence_rows=candidate.settled_paid_rows,
                claim_generator_type=generation_spec.card_lineage.generator_type,
                claim_model_name=generation_spec.card_lineage.model_name,
                claim_config_version=generation_spec.card_lineage.config_version,
                claim_contract_version=generation_spec.card_lineage.contract_version,
                claim_reducer_version=generation_spec.card_lineage.reducer_version,
                claim_prompt_version=generation_spec.card_lineage.prompt_version,
                rendered_claim_text=_operator_draft_rendered_claim_text(card_payload=card_payload),
            ),
            db=db,
        )
        run_record.cards.append(
            CreatorOperatorExperimentDraftRunCardRecord(
                claim_snapshot_id=claim_snapshot.id,
                content_tid=card_payload.content_tid,
                title=card_payload.title.strip(),
                hypothesis=card_payload.hypothesis.strip(),
                why_this_might_work=card_payload.why_this_might_work.strip(),
                evidence_summary=card_payload.evidence_summary.strip(),
                ranking_rationale=card_payload.ranking_rationale.strip(),
                caution=OPERATOR_EXPERIMENT_DRAFT_CARD_CAUTION,
                card_order=index,
            )
        )

    db.flush()
    db.refresh(run_record)
    hydrated_run_record = db.execute(
        _creator_operator_experiment_draft_run_query(creator_id=creator_id).where(
            CreatorOperatorExperimentDraftRunRecord.id == run_record.id
        )
    ).scalar_one()
    return _build_operator_draft_run_result(hydrated_run_record)


def get_latest_creator_operator_experiment_draft_run(
    *,
    creator_id: UUID,
    db: Session,
) -> CreatorOperatorExperimentDraftRunResult | None:
    run_record = db.execute(
        _creator_operator_experiment_draft_run_query(creator_id=creator_id)
        .order_by(CreatorOperatorExperimentDraftRunRecord.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if run_record is None:
        return None
    return _build_operator_draft_run_result(run_record)


def get_creator_operator_experiment_draft_run(
    *,
    creator_id: UUID,
    draft_run_id: UUID,
    db: Session,
) -> CreatorOperatorExperimentDraftRunResult | None:
    run_record = db.execute(
        _creator_operator_experiment_draft_run_query(creator_id=creator_id).where(
            CreatorOperatorExperimentDraftRunRecord.id == draft_run_id
        )
    ).scalar_one_or_none()
    if run_record is None:
        return None
    return _build_operator_draft_run_result(run_record)


def _creator_operator_experiment_draft_run_query(*, creator_id: UUID):
    return (
        select(CreatorOperatorExperimentDraftRunRecord)
        .options(
            selectinload(CreatorOperatorExperimentDraftRunRecord.cards).selectinload(
                CreatorOperatorExperimentDraftRunCardRecord.claim_snapshot
            )
        )
        .where(CreatorOperatorExperimentDraftRunRecord.creator_id == creator_id)
    )


def _build_operator_draft_run_result(
    run_record: CreatorOperatorExperimentDraftRunRecord,
) -> CreatorOperatorExperimentDraftRunResult:
    db = object_session(run_record)
    if db is None:
        raise ValueError("operator draft run must be attached to a session")
    cards: list[OperatorExperimentDraftCard] = []
    for card_record in run_record.cards:
        resolved_snapshot = resolve_creator_claim_snapshot(
            creator_id=run_record.creator_id,
            claim_snapshot_id=card_record.claim_snapshot_id,
            db=db,
        )
        if resolved_snapshot is None:
            raise ValueError("stored operator draft claim snapshot could not be resolved")

        authoritative_evidence = resolved_snapshot.authoritative_content_evidence
        cards.append(
            OperatorExperimentDraftCard(
                claim_snapshot_id=card_record.claim_snapshot_id,
                card_order=card_record.card_order,
                title=card_record.title,
                hypothesis=card_record.hypothesis,
                why_this_might_work=card_record.why_this_might_work,
                evidence_summary=card_record.evidence_summary,
                caution=card_record.caution,
                ranking_rationale=card_record.ranking_rationale,
                content_tid=card_record.content_tid,
                authoritative_source_url=(
                    authoritative_evidence.fetch_snapshot.fetched_url
                    or authoritative_evidence.fetch_snapshot.requested_url
                ),
                authoritative_artifact_title=authoritative_evidence.artifact.title,
                authoritative_topics=[
                    topic.canonical_label
                    for topic in authoritative_evidence.confirmed_topics
                ],
                settled_paid_results=[
                    NextContentExperimentPaidEvidenceDetail(
                        content_tid=row.tid,
                        booked_at=row.booked_at,
                        paid_at=row.invoice_paid_at,
                        amount_cents=row.invoice_amount_cents,
                        currency=row.invoice_currency,
                    )
                    for row in resolved_snapshot.settled_paid_evidence_rows
                ],
                lineage=_build_claim_lineage_from_record(card_record),
            )
        )

    run_lineage = HelperGenerationLineage(
        generator_type=run_record.run_generator_type,
        model_name=run_record.run_model_name,
        prompt_version=run_record.run_prompt_version,
        config_version=run_record.run_config_version,
        contract_version=run_record.run_contract_version,
        reducer_version=run_record.run_reducer_version,
    )
    return CreatorOperatorExperimentDraftRunResult(
        draft_run_id=run_record.id,
        status=run_record.status,
        summary=run_record.summary_text,
        lineage=run_lineage,
        version_semantics=HelperVersionSemantics(
            schema_version=OPERATOR_EXPERIMENT_DRAFT_RESULT_SCHEMA_VERSION,
            evidence_input_version=EXPERIMENT_EVIDENCE_INPUT_VERSION,
            generation_config_version=run_lineage.config_version,
        ),
        freshness_policy=CURRENT_NEXT_CONTENT_EXPERIMENTS_FRESHNESS_POLICY,
        cards=cards,
        created_at=run_record.created_at,
    )


@dataclass(frozen=True)
class _OperatorExperimentGenerationSpec:
    run_lineage: HelperGenerationLineage
    card_lineage: HelperGenerationLineage


def _current_operator_experiment_generation_spec(
    *,
    model_name: str,
    prompt_version: str,
) -> _OperatorExperimentGenerationSpec:
    return _OperatorExperimentGenerationSpec(
        run_lineage=HelperGenerationLineage(
            generator_type=OPERATOR_EXPERIMENT_DRAFT_GENERATOR_TYPE,
            model_name=model_name,
            prompt_version=prompt_version,
            config_version=OPERATOR_EXPERIMENT_DRAFT_RUN_CONFIG_VERSION,
            contract_version=OPERATOR_EXPERIMENT_DRAFT_RUN_CONTRACT_VERSION,
            reducer_version=OPERATOR_EXPERIMENT_DRAFT_RUN_REDUCER_VERSION,
        ),
        card_lineage=HelperGenerationLineage(
            generator_type=OPERATOR_EXPERIMENT_DRAFT_GENERATOR_TYPE,
            model_name=model_name,
            prompt_version=prompt_version,
            config_version=OPERATOR_EXPERIMENT_DRAFT_CARD_CLAIM_CONFIG_VERSION,
            contract_version=OPERATOR_EXPERIMENT_DRAFT_CARD_CLAIM_CONTRACT_VERSION,
            reducer_version=OPERATOR_EXPERIMENT_DRAFT_RUN_REDUCER_VERSION,
        ),
    )


def _build_prompt_candidate(candidate) -> OperatorExperimentDraftPromptCandidate:
    authoritative_excerpt = (candidate.authoritative_evidence.artifact.extracted_text or "").strip()
    if authoritative_excerpt:
        authoritative_excerpt = authoritative_excerpt[:600]
    else:
        authoritative_excerpt = None
    return OperatorExperimentDraftPromptCandidate(
        content_id=candidate.content.id,
        content_tid=candidate.content.tid,
        source_url=candidate.content.source_url,
        authoritative_title=candidate.authoritative_evidence.artifact.title,
        authoritative_topics=[
            topic.canonical_label
            for topic in candidate.authoritative_evidence.confirmed_topics
        ],
        authoritative_excerpt=authoritative_excerpt,
        paid_booking_count=candidate.paid_booking_count,
        paid_invoice_count=candidate.paid_invoice_count,
        paid_revenue_cents=candidate.paid_revenue_cents,
        paid_revenue_summary=_format_revenue_summary(candidate.settled_paid_rows),
        last_paid_at=candidate.last_paid_at,
    )


def _operator_draft_prompt_payload(
    *,
    prompt_input: OperatorExperimentDraftPromptInput,
) -> dict[str, object]:
    return {
        "creator_name": prompt_input.creator_name,
        "objective": (
            "Generate operator-only draft next-content experiments using only the "
            "provided evidence-backed candidate set."
        ),
        "constraints": [
            "Use only the provided candidate content_tids.",
            "Choose between 1 and 5 cards.",
            "Keep each card grounded in one candidate content item.",
            "Do not mention missing data, internal systems, diagnostics, or generic growth advice.",
            "Use modest hypothesis framing, preferably 'Test whether...' wording, not guaranteed outcomes.",
            "Prefer concrete counts and revenue signals already present in the candidate data.",
        ],
        "candidates": [
            {
                "content_tid": candidate.content_tid,
                "source_url": candidate.source_url,
                "authoritative_title": candidate.authoritative_title,
                "authoritative_topics": candidate.authoritative_topics,
                "authoritative_excerpt": candidate.authoritative_excerpt,
                "paid_booking_count": candidate.paid_booking_count,
                "paid_invoice_count": candidate.paid_invoice_count,
                "paid_revenue_cents": candidate.paid_revenue_cents,
                "paid_revenue_summary": candidate.paid_revenue_summary,
                "last_paid_at": candidate.last_paid_at.isoformat(),
            }
            for candidate in prompt_input.candidates
        ],
    }


def _operator_draft_instructions(*, prompt_version: str, max_cards: int) -> str:
    return (
        f"Prompt version: {prompt_version}. "
        "You are generating internal operator-only draft content experiments for a creator workflow. "
        "Return only structured JSON that matches the schema. "
        "Each card must stay inside the provided evidence, remain specific rather than generic, "
        "and use modest hypothesis framing instead of promised outcomes. "
        f"Return between 1 and {max_cards} cards."
    )


def _operator_draft_response_schema(*, candidate_tids: list[str]) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["cards"],
        "properties": {
            "cards": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_OPERATOR_EXPERIMENT_DRAFT_CARDS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "content_tid",
                        "title",
                        "hypothesis",
                        "why_this_might_work",
                        "evidence_summary",
                        "ranking_rationale",
                    ],
                    "properties": {
                        "content_tid": {
                            "type": "string",
                            "enum": candidate_tids,
                        },
                        "title": {"type": "string"},
                        "hypothesis": {"type": "string"},
                        "why_this_might_work": {"type": "string"},
                        "evidence_summary": {"type": "string"},
                        "ranking_rationale": {"type": "string"},
                    },
                },
            }
        },
    }


def _extract_openai_output_text(response_payload: dict[str, object]) -> str:
    output_items = response_payload.get("output") or []
    if not isinstance(output_items, list):
        raise OperatorExperimentDraftProviderError(
            "OpenAI draft generation returned an invalid output payload"
        )

    for output_item in output_items:
        if not isinstance(output_item, dict):
            continue
        if output_item.get("type") != "message":
            continue
        for content_item in output_item.get("content") or []:
            if not isinstance(content_item, dict):
                continue
            if content_item.get("type") == "refusal":
                raise OperatorExperimentDraftProviderError(
                    f"OpenAI draft generation was refused: {content_item.get('refusal', '')}"
                )
            if content_item.get("type") == "output_text":
                text_value = content_item.get("text")
                if isinstance(text_value, str) and text_value.strip():
                    return text_value

    raise OperatorExperimentDraftProviderError(
        "OpenAI draft generation returned no output text"
    )


def _validate_provider_cards(
    *,
    cards: list[_OperatorExperimentDraftCardPayload],
    allowed_tids: set[str],
) -> list[_OperatorExperimentDraftCardPayload]:
    seen_tids: set[str] = set()
    validated_cards: list[_OperatorExperimentDraftCardPayload] = []
    for card in cards:
        content_tid = card.content_tid.strip()
        if content_tid not in allowed_tids:
            raise OperatorExperimentDraftProviderError(
                f"Draft card referenced unsupported content_tid={content_tid}"
            )
        if content_tid in seen_tids:
            raise OperatorExperimentDraftProviderError(
                f"Draft card duplicated content_tid={content_tid}"
            )
        seen_tids.add(content_tid)
        title = card.title.strip()
        hypothesis = card.hypothesis.strip()
        why_this_might_work = card.why_this_might_work.strip()
        evidence_summary = card.evidence_summary.strip()
        ranking_rationale = card.ranking_rationale.strip()
        if not all(
            [
                title,
                hypothesis,
                why_this_might_work,
                evidence_summary,
                ranking_rationale,
            ]
        ):
            raise OperatorExperimentDraftProviderError(
                "Draft cards must include non-empty title, hypothesis, evidence summary, and rationale"
            )
        validated_cards.append(
            card.model_copy(
                update={
                    "content_tid": content_tid,
                    "title": title,
                    "hypothesis": hypothesis,
                    "why_this_might_work": why_this_might_work,
                    "evidence_summary": evidence_summary,
                    "ranking_rationale": ranking_rationale,
                }
            )
        )
    return validated_cards


def _operator_draft_summary(*, card_count: int) -> str:
    if card_count == 1:
        return (
            "Here is the operator-only draft content experiment most grounded in the current authoritative topics and attributed paid results."
        )
    return (
        "Here are the operator-only draft content experiments most grounded in the current authoritative topics and attributed paid results."
    )


def _operator_draft_rendered_claim_text(
    *,
    card_payload: _OperatorExperimentDraftCardPayload,
) -> str:
    return f"{card_payload.title.strip()}\n{card_payload.hypothesis.strip()}"


def _build_claim_lineage_from_record(
    card_record: CreatorOperatorExperimentDraftRunCardRecord,
) -> HelperGenerationLineage:
    claim_snapshot = card_record.claim_snapshot
    if claim_snapshot is None:
        raise ValueError("operator draft card claim snapshot is required")
    return HelperGenerationLineage(
        generator_type=claim_snapshot.claim_generator_type,
        model_name=claim_snapshot.claim_model_name,
        prompt_version=claim_snapshot.claim_prompt_version,
        config_version=claim_snapshot.claim_config_version,
        contract_version=claim_snapshot.claim_contract_version,
        reducer_version=claim_snapshot.claim_reducer_version,
    )
