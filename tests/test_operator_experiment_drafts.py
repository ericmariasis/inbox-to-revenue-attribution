from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.creator_claim_snapshot import CreatorClaimSnapshotRecord
from app.models.creator_operator_experiment_draft_run import (
    CreatorOperatorExperimentDraftRunRecord,
)
from app.models.creator_operator_experiment_draft_run_card import (
    CreatorOperatorExperimentDraftRunCardRecord,
)
from app.services.operator_experiment_drafts import (
    OPERATOR_EXPERIMENT_DRAFT_CARD_CLAIM_KIND,
    OperatorExperimentDraftProviderError,
    OperatorExperimentDraftProviderOutput,
    OperatorExperimentDraftUnavailableError,
    _OperatorExperimentDraftCardPayload,
    _validate_provider_cards,
    create_creator_operator_experiment_draft_run,
    get_latest_creator_operator_experiment_draft_run,
)
from tests.test_next_content_experiments import (
    _create_authoritative_artifact,
    _create_content,
    _create_creator_fixture,
    _create_paid_booking,
    _engine,
)


class _StubOperatorExperimentDraftProvider:
    def __init__(
        self,
        *,
        configured: bool = True,
        model_name: str = "gpt-5.4-mini",
        prompt_version: str = "operator_draft_next_content_experiments.prompt.v1",
        cards: list[_OperatorExperimentDraftCardPayload] | None = None,
    ):
        self._configured = configured
        self._model_name = model_name
        self._prompt_version = prompt_version
        self._cards = cards or []
        self.calls = []

    def is_configured(self) -> bool:
        return self._configured

    def generate_draft(self, *, prompt_input):
        self.calls.append(prompt_input)
        if not self._configured:
            raise OperatorExperimentDraftUnavailableError("provider unavailable")
        return OperatorExperimentDraftProviderOutput(
            model_name=self._model_name,
            prompt_version=self._prompt_version,
            cards=self._cards,
        )


def test_create_creator_operator_experiment_draft_run_persists_ready_cards_with_lineage():
    engine = _engine()

    with Session(engine) as session:
        creator, booking_link = _create_creator_fixture(session, suffix="operator_draft_ready")
        creator_id = creator.id
        content_a = _create_content(
            session,
            creator=creator,
            booking_link=booking_link,
            tid="operator_draft_tid_a",
            source_url="https://example.com/posts/operator-draft-a",
        )
        _create_authoritative_artifact(
            session,
            content=content_a,
            topic_labels=["Retention Reviews"],
            fetched_at=datetime(2026, 4, 20, 10, 0, tzinfo=timezone.utc),
        )
        _create_paid_booking(
            session,
            creator=creator,
            booking_link=booking_link,
            content=content_a,
            booking_uuid="BOOK_OPERATOR_DRAFT_A",
            paid_at=datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc),
            amount_cents=19500,
            stripe_invoice_id="in_operator_draft_a",
            stripe_event_id="evt_operator_draft_a",
        )
        content_b = _create_content(
            session,
            creator=creator,
            booking_link=booking_link,
            tid="operator_draft_tid_b",
            source_url="https://example.com/posts/operator-draft-b",
        )
        _create_authoritative_artifact(
            session,
            content=content_b,
            topic_labels=["Pricing Reviews"],
            fetched_at=datetime(2026, 4, 21, 10, 0, tzinfo=timezone.utc),
        )
        _create_paid_booking(
            session,
            creator=creator,
            booking_link=booking_link,
            content=content_b,
            booking_uuid="BOOK_OPERATOR_DRAFT_B",
            paid_at=datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc),
            amount_cents=22500,
            stripe_invoice_id="in_operator_draft_b",
            stripe_event_id="evt_operator_draft_b",
        )

        provider = _StubOperatorExperimentDraftProvider(
            cards=[
                _OperatorExperimentDraftCardPayload(
                    content_tid="operator_draft_tid_b",
                    title="Test another pricing-review proof angle",
                    hypothesis="Test whether a sharper pricing review post may drive more attributed paid bookings.",
                    why_this_might_work="Pricing review evidence already has one settled paid pattern and can support a cleaner comparison angle.",
                    evidence_summary="Pricing Reviews already has one paid booking and one paid invoice totaling USD 225.00.",
                    ranking_rationale="It leads this draft set because it pairs current recency with the strongest paid evidence in the candidate set.",
                ),
                _OperatorExperimentDraftCardPayload(
                    content_tid="operator_draft_tid_a",
                    title="Test a new retention-review follow-up",
                    hypothesis="Test whether a retention review follow-up post may drive another attributed paid booking.",
                    why_this_might_work="Retention review evidence already links one authoritative pattern to one settled paid result.",
                    evidence_summary="Retention Reviews already has one paid booking and one paid invoice totaling USD 195.00.",
                    ranking_rationale="It stays second because it still has direct paid support, but less attributed revenue than the card above.",
                ),
            ]
        )

        created = create_creator_operator_experiment_draft_run(
            creator_id=creator.id,
            creator_name=creator.name,
            db=session,
            provider=provider,
        )
        session.commit()

    with Session(engine) as session:
        run_count = session.execute(
            select(func.count()).select_from(CreatorOperatorExperimentDraftRunRecord)
        ).scalar_one()
        card_count = session.execute(
            select(func.count()).select_from(CreatorOperatorExperimentDraftRunCardRecord)
        ).scalar_one()
        snapshot_rows = session.execute(
            select(CreatorClaimSnapshotRecord.claim_kind).order_by(CreatorClaimSnapshotRecord.created_at.asc())
        ).scalars().all()
        loaded = get_latest_creator_operator_experiment_draft_run(
            creator_id=creator_id,
            db=session,
        )

    assert provider.calls
    assert created.status == "ready"
    assert created.lineage.generator_type == "llm_assisted"
    assert created.lineage.model_name == "gpt-5.4-mini"
    assert created.lineage.prompt_version == "operator_draft_next_content_experiments.prompt.v1"
    assert len(created.cards) == 2
    assert created.cards[0].content_tid == "operator_draft_tid_b"
    assert created.cards[0].authoritative_source_url == "https://example.com/posts/operator-draft-b"
    assert len(created.cards[0].settled_paid_results) == 1
    assert run_count == 1
    assert card_count == 2
    assert snapshot_rows.count(OPERATOR_EXPERIMENT_DRAFT_CARD_CLAIM_KIND) == 2
    assert loaded is not None
    assert loaded.draft_run_id == created.draft_run_id
    assert loaded.cards[1].content_tid == "operator_draft_tid_a"


def test_create_creator_operator_experiment_draft_run_rejects_unconfigured_provider():
    engine = _engine()

    with Session(engine) as session:
        creator, booking_link = _create_creator_fixture(
            session,
            suffix="operator_draft_unconfigured",
        )
        content = _create_content(
            session,
            creator=creator,
            booking_link=booking_link,
            tid="operator_draft_unconfigured_tid",
            source_url="https://example.com/posts/operator-draft-unconfigured",
        )
        _create_authoritative_artifact(
            session,
            content=content,
            topic_labels=["Retention Reviews"],
            fetched_at=datetime(2026, 4, 20, 10, 0, tzinfo=timezone.utc),
        )
        _create_paid_booking(
            session,
            creator=creator,
            booking_link=booking_link,
            content=content,
            booking_uuid="BOOK_OPERATOR_DRAFT_UNCONFIGURED",
            paid_at=datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc),
            amount_cents=19500,
            stripe_invoice_id="in_operator_draft_unconfigured",
            stripe_event_id="evt_operator_draft_unconfigured",
        )

        with pytest.raises(OperatorExperimentDraftUnavailableError):
            create_creator_operator_experiment_draft_run(
                creator_id=creator.id,
                creator_name=creator.name,
                db=session,
                provider=_StubOperatorExperimentDraftProvider(configured=False),
            )


def test_create_creator_operator_experiment_draft_run_rejects_unknown_content_tid():
    engine = _engine()

    with Session(engine) as session:
        creator, booking_link = _create_creator_fixture(
            session,
            suffix="operator_draft_bad_tid",
        )
        content = _create_content(
            session,
            creator=creator,
            booking_link=booking_link,
            tid="operator_draft_good_tid",
            source_url="https://example.com/posts/operator-draft-good",
        )
        _create_authoritative_artifact(
            session,
            content=content,
            topic_labels=["Retention Reviews"],
            fetched_at=datetime(2026, 4, 20, 10, 0, tzinfo=timezone.utc),
        )
        _create_paid_booking(
            session,
            creator=creator,
            booking_link=booking_link,
            content=content,
            booking_uuid="BOOK_OPERATOR_DRAFT_GOOD",
            paid_at=datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc),
            amount_cents=19500,
            stripe_invoice_id="in_operator_draft_good",
            stripe_event_id="evt_operator_draft_good",
        )

        with pytest.raises(OperatorExperimentDraftProviderError, match="unsupported content_tid"):
            create_creator_operator_experiment_draft_run(
                creator_id=creator.id,
                creator_name=creator.name,
                db=session,
                provider=_StubOperatorExperimentDraftProvider(
                    cards=[
                        _OperatorExperimentDraftCardPayload(
                            content_tid="totally_unknown_tid",
                            title="Bad draft",
                            hypothesis="Test whether a bad draft may appear.",
                            why_this_might_work="It should not.",
                            evidence_summary="No supported evidence exists for this card.",
                            ranking_rationale="This should fail validation.",
                        )
                    ]
                ),
            )


def test_provider_card_validation_accepts_modest_hypothesis_without_test_prefix():
    validated = _validate_provider_cards(
        cards=[
            _OperatorExperimentDraftCardPayload(
                content_tid="operator_draft_valid_tid",
                title=" Pricing-review follow-up angle ",
                hypothesis=(
                    "A sharper pricing-review post may drive more attributed paid bookings."
                ),
                why_this_might_work=(
                    "The authoritative pricing-review pattern already has a settled paid result."
                ),
                evidence_summary=(
                    "Pricing Reviews already has one paid booking and one paid invoice."
                ),
                ranking_rationale=(
                    "It is grounded in current evidence and avoids guaranteed outcome wording."
                ),
            )
        ],
        allowed_tids={"operator_draft_valid_tid"},
    )

    assert len(validated) == 1
    assert validated[0].title == "Pricing-review follow-up angle"
    assert (
        validated[0].hypothesis
        == "A sharper pricing-review post may drive more attributed paid bookings."
    )
