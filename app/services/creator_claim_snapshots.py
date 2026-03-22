from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.content import Content
from app.models.creator_claim_paid_evidence_reference import CreatorClaimPaidEvidenceReference
from app.models.creator_claim_snapshot import CreatorClaimSnapshotRecord
from app.services.authoritative_content_evidence import (
    AuthoritativeContentEvidence,
    get_authoritative_content_evidence_for_snapshot,
)
from app.services.settled_paid_evidence import (
    SettledPaidEvidenceReference,
    SettledPaidEvidenceRow,
    build_settled_paid_evidence_reference,
    get_creator_settled_paid_evidence_rows_for_references,
)


@dataclass(frozen=True)
class CreateCreatorClaimSnapshotInput:
    claim_kind: str
    content_id: UUID
    authoritative_extraction_artifact_id: UUID
    authoritative_fetch_snapshot_id: UUID
    settled_paid_evidence_rows: list[SettledPaidEvidenceRow]
    claim_contract_version: str
    claim_generator_type: str | None = None
    claim_model_name: str | None = None
    claim_config_version: str | None = None
    claim_reducer_version: str | None = None
    claim_prompt_version: str | None = None
    rendered_claim_text: str | None = None


@dataclass(frozen=True)
class CreatorClaimSnapshot:
    id: UUID
    creator_id: UUID
    content_id: UUID
    authoritative_extraction_artifact_id: UUID
    authoritative_fetch_snapshot_id: UUID
    claim_kind: str
    claim_generator_type: str | None
    claim_model_name: str | None
    claim_config_version: str | None
    claim_contract_version: str
    claim_reducer_version: str | None
    claim_prompt_version: str | None
    rendered_claim_text: str | None
    created_at: datetime


@dataclass(frozen=True)
class ResolvedCreatorClaimSnapshot:
    snapshot: CreatorClaimSnapshot
    authoritative_content_evidence: AuthoritativeContentEvidence
    settled_paid_evidence_rows: list[SettledPaidEvidenceRow]


def create_creator_claim_snapshot(
    *,
    creator_id: UUID,
    input: CreateCreatorClaimSnapshotInput,
    db: Session,
) -> CreatorClaimSnapshot:
    content = db.execute(
        select(Content).where(
            Content.id == input.content_id,
            Content.creator_id == creator_id,
        )
    ).scalar_one_or_none()
    if content is None:
        raise ValueError("content must belong to the creator")

    authoritative_evidence = get_authoritative_content_evidence_for_snapshot(
        creator_id=creator_id,
        content_id=input.content_id,
        extraction_artifact_id=input.authoritative_extraction_artifact_id,
        fetch_snapshot_id=input.authoritative_fetch_snapshot_id,
        db=db,
    )
    if authoritative_evidence is None:
        raise ValueError("authoritative content evidence ids must resolve canonically")

    references = _build_unique_references(
        content_id=input.content_id,
        rows=input.settled_paid_evidence_rows,
    )
    if not references:
        raise ValueError("at least one settled paid evidence row is required")

    resolved_rows = get_creator_settled_paid_evidence_rows_for_references(
        creator_id=creator_id,
        references=references,
        db=db,
    )
    if [build_settled_paid_evidence_reference(row) for row in resolved_rows] != references:
        raise ValueError("settled paid evidence ids must resolve through the canonical contract")

    snapshot_record = CreatorClaimSnapshotRecord(
        creator_id=creator_id,
        content_id=input.content_id,
        authoritative_extraction_artifact_id=input.authoritative_extraction_artifact_id,
        authoritative_fetch_snapshot_id=input.authoritative_fetch_snapshot_id,
        claim_kind=input.claim_kind,
        claim_generator_type=input.claim_generator_type,
        claim_model_name=input.claim_model_name,
        claim_config_version=input.claim_config_version,
        claim_contract_version=input.claim_contract_version,
        claim_reducer_version=input.claim_reducer_version,
        claim_prompt_version=input.claim_prompt_version,
        rendered_claim_text=input.rendered_claim_text,
    )
    snapshot_record.paid_evidence_references = [
        CreatorClaimPaidEvidenceReference(
            booking_id=reference.booking_id,
            invoice_id=reference.invoice_id,
            payment_event_id=reference.payment_event_id,
            evidence_order=index,
        )
        for index, reference in enumerate(references, start=1)
    ]

    db.add(snapshot_record)
    db.flush()
    db.refresh(snapshot_record)
    return _build_snapshot(snapshot_record)


def resolve_creator_claim_snapshot(
    *,
    creator_id: UUID,
    claim_snapshot_id: UUID,
    db: Session,
) -> ResolvedCreatorClaimSnapshot | None:
    snapshot_record = db.execute(
        select(CreatorClaimSnapshotRecord)
        .options(selectinload(CreatorClaimSnapshotRecord.paid_evidence_references))
        .where(
            CreatorClaimSnapshotRecord.id == claim_snapshot_id,
            CreatorClaimSnapshotRecord.creator_id == creator_id,
        )
    ).scalar_one_or_none()
    if snapshot_record is None:
        return None

    authoritative_evidence = get_authoritative_content_evidence_for_snapshot(
        creator_id=creator_id,
        content_id=snapshot_record.content_id,
        extraction_artifact_id=snapshot_record.authoritative_extraction_artifact_id,
        fetch_snapshot_id=snapshot_record.authoritative_fetch_snapshot_id,
        db=db,
    )
    if authoritative_evidence is None:
        raise ValueError("stored claim snapshot no longer resolves through authoritative content evidence")

    references = [
        SettledPaidEvidenceReference(
            content_id=snapshot_record.content_id,
            booking_id=reference.booking_id,
            invoice_id=reference.invoice_id,
            payment_event_id=reference.payment_event_id,
        )
        for reference in snapshot_record.paid_evidence_references
    ]
    settled_rows = get_creator_settled_paid_evidence_rows_for_references(
        creator_id=creator_id,
        references=references,
        db=db,
    )
    if [build_settled_paid_evidence_reference(row) for row in settled_rows] != references:
        raise ValueError("stored claim snapshot no longer resolves through settled paid evidence")

    return ResolvedCreatorClaimSnapshot(
        snapshot=_build_snapshot(snapshot_record),
        authoritative_content_evidence=authoritative_evidence,
        settled_paid_evidence_rows=settled_rows,
    )


def _build_unique_references(
    *,
    content_id: UUID,
    rows: list[SettledPaidEvidenceRow],
) -> list[SettledPaidEvidenceReference]:
    references: list[SettledPaidEvidenceReference] = []
    seen_invoice_ids: set[UUID] = set()

    for row in rows:
        if row.content_id != content_id:
            raise ValueError("settled paid evidence rows must match the claim content")
        if row.invoice_id in seen_invoice_ids:
            continue
        seen_invoice_ids.add(row.invoice_id)
        references.append(build_settled_paid_evidence_reference(row))

    return references


def _build_snapshot(snapshot_record: CreatorClaimSnapshotRecord) -> CreatorClaimSnapshot:
    return CreatorClaimSnapshot(
        id=snapshot_record.id,
        creator_id=snapshot_record.creator_id,
        content_id=snapshot_record.content_id,
        authoritative_extraction_artifact_id=snapshot_record.authoritative_extraction_artifact_id,
        authoritative_fetch_snapshot_id=snapshot_record.authoritative_fetch_snapshot_id,
        claim_kind=snapshot_record.claim_kind,
        claim_generator_type=snapshot_record.claim_generator_type,
        claim_model_name=snapshot_record.claim_model_name,
        claim_config_version=snapshot_record.claim_config_version,
        claim_contract_version=snapshot_record.claim_contract_version,
        claim_reducer_version=snapshot_record.claim_reducer_version,
        claim_prompt_version=snapshot_record.claim_prompt_version,
        rendered_claim_text=snapshot_record.rendered_claim_text,
        created_at=snapshot_record.created_at,
    )
