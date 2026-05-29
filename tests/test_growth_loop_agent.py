from app.services.growth_loop_agent import (
    GROWTH_LOOP_STAGE_BILLABLE_NO_TRACKED_CONTENT,
    GROWTH_LOOP_STAGE_BOOKINGS_NO_PAID_RESULTS,
    GROWTH_LOOP_STAGE_PAID_RESULT_EXISTS,
    GROWTH_LOOP_STAGE_SETUP_INCOMPLETE,
    GROWTH_LOOP_STAGE_TRACKED_NO_BOOKINGS,
    GrowthLoopWorkspaceEvidence,
    build_sleepy_goose_schema_opportunity,
    build_fixture_loomi_diagnostic_context,
    build_growth_loop_action_brief,
)


def _evidence(**overrides: object) -> GrowthLoopWorkspaceEvidence:
    values = {
        "billing_connected": False,
        "billable_now": False,
        "booking_links_count": 0,
        "billing_ready_count": 0,
        "tracked_content_count": 0,
        "booking_count": 0,
        "paid_invoice_count": 0,
        "paid_revenue_cents": 0,
        "billing_provider": None,
    }
    values.update(overrides)
    return GrowthLoopWorkspaceEvidence(**values)


def test_growth_loop_stage_setup_incomplete():
    brief = build_growth_loop_action_brief(evidence=_evidence())

    assert brief.stage == GROWTH_LOOP_STAGE_SETUP_INCOMPLETE
    assert "Finish billable tracking setup" == brief.next_action_title
    assert "paid-result evidence is not available" in brief.confidence_summary


def test_growth_loop_stage_billable_but_no_tracked_content():
    brief = build_growth_loop_action_brief(
        evidence=_evidence(
            billing_connected=True,
            billable_now=True,
            booking_links_count=1,
            billing_ready_count=1,
        )
    )

    assert brief.stage == GROWTH_LOOP_STAGE_BILLABLE_NO_TRACKED_CONTENT
    assert brief.next_action_title == "Add one tracked content item"
    assert "no app-owned content path" in brief.confidence_summary


def test_growth_loop_stage_tracked_but_no_bookings():
    brief = build_growth_loop_action_brief(
        evidence=_evidence(
            billing_connected=True,
            billable_now=True,
            booking_links_count=1,
            billing_ready_count=1,
            tracked_content_count=1,
        )
    )

    assert brief.stage == GROWTH_LOOP_STAGE_TRACKED_NO_BOOKINGS
    assert brief.next_action_title == "Promote one tracked booking path"
    assert "no booking evidence" in brief.confidence_summary


def test_growth_loop_stage_bookings_but_no_paid_results():
    brief = build_growth_loop_action_brief(
        evidence=_evidence(
            billing_connected=True,
            billable_now=True,
            booking_links_count=1,
            billing_ready_count=1,
            tracked_content_count=1,
            booking_count=2,
        )
    )

    assert brief.stage == GROWTH_LOOP_STAGE_BOOKINGS_NO_PAID_RESULTS
    assert brief.next_action_title == "Review booking-to-paid follow-up"
    assert "revenue is not counted yet" in brief.confidence_summary


def test_growth_loop_stage_paid_result_exists_keeps_paid_truth_app_owned():
    brief = build_growth_loop_action_brief(
        evidence=_evidence(
            billing_connected=True,
            billable_now=True,
            booking_links_count=1,
            billing_ready_count=1,
            tracked_content_count=1,
            booking_count=2,
            paid_invoice_count=1,
            paid_revenue_cents=19500,
        )
    )

    assert brief.stage == GROWTH_LOOP_STAGE_PAID_RESULT_EXISTS
    assert brief.next_action_title == "Prepare one follow-up brief from the proven path"
    assert any(item.label == "Canonical invoices" and item.value == "1 paid invoice" for item in brief.app_evidence)
    assert any(item.label == "Canonical payment truth" and item.value == "$195.00" for item in brief.app_evidence)
    assert "stored invoices and payment records" in brief.diagnosis_summary
    assert "Loomi diagnostics are context for review, not a second paid-result ledger." in brief.limitations
    assert brief.schema_opportunity.opportunity_title == "Cart-abandon recover & convert"
    assert brief.schema_opportunity.source_status_label == "Verified via Cursor MCP"
    assert brief.reviewable_action is not None
    assert brief.reviewable_action.title == "Reviewable recovery brief"
    assert "app-owned paid conversion lift" in " ".join(brief.reviewable_action.success_evidence)
    assert brief.decision_trace is not None
    assert brief.decision_trace.title == "Decision trace"
    assert "$195.00 canonical payment truth" in " ".join(brief.decision_trace.evidence_chain)
    assert brief.segment_recipe is not None
    assert brief.segment_recipe.title == "Bloomreach-ready segment recipe"
    assert brief.measurement_plan is not None
    assert brief.measurement_plan.title == "Measurement plan"
    assert brief.sandbox_proof is not None
    assert brief.sandbox_proof.title == "Sandbox proof"
    assert brief.sandbox_proof.status_label == "Story 137 passed"
    assert "Pacific Apparel Storefront" in brief.sandbox_proof.summary
    assert "sleepy-goose Engagement" in brief.sandbox_proof.summary
    assert [card.label for card in brief.sandbox_proof.cards] == [
        "Storefront",
        "Engagement",
        "App proof",
    ]
    assert "Pacific Apparel" in brief.sandbox_proof.cards[0].title
    assert "sleepy-goose" in brief.sandbox_proof.cards[1].title
    assert "$195.00" in brief.sandbox_proof.cards[2].detail
    assert "No live Engagement or Storefront call is made by this page." in brief.sandbox_proof.boundaries
    assert "No lift, causality, or new paid-truth source is claimed." in brief.sandbox_proof.boundaries
    assert brief.agent_console is not None
    assert brief.agent_console.title == "Agent console"
    assert brief.agent_console.guided_run.title == "Run agent"
    assert [step.title for step in brief.agent_console.guided_run.steps] == [
        "Inspect paid proof",
        "Read Loomi schema evidence",
        "Score candidate actions",
        "Prepare recovery brief",
        "Generate segment recipe",
        "Attach measurement plan",
    ]
    assert "No campaign is sent." in brief.agent_console.guided_run.boundaries
    assert "App-owned invoice and payment records remain paid truth." in brief.agent_console.guided_run.boundaries
    assert "#growth-loop-review-packet" in brief.agent_console.primary_action_label or brief.agent_console.primary_action_label == "View review packet"


def test_growth_loop_guided_run_is_paid_result_only_and_review_bounded():
    brief = build_growth_loop_action_brief(
        evidence=_evidence(
            billing_connected=True,
            billable_now=True,
            booking_links_count=1,
            billing_ready_count=1,
            tracked_content_count=1,
            booking_count=1,
            paid_invoice_count=1,
            paid_revenue_cents=19500,
        )
    )

    assert brief.agent_console is not None
    run = brief.agent_console.guided_run
    combined_text = " ".join(
        [run.summary, run.completion_summary]
        + [step.title for step in run.steps]
        + [step.summary for step in run.steps]
        + [step.evidence_detail for step in run.steps]
        + list(run.boundaries)
    ).lower()

    assert len(run.steps) == 6
    assert run.steps[0].target_anchor == "#growth-loop-boundaries"
    assert run.steps[1].target_anchor == "#growth-loop-proof"
    assert run.steps[2].target_anchor == "#growth-loop-decision"
    assert run.steps[3].target_anchor == "#growth-loop-action"
    assert run.steps[4].target_anchor == "#growth-loop-segment"
    assert run.steps[5].target_anchor == "#growth-loop-measure"
    assert "$195.00 canonical payment truth" in combined_text
    assert "mcp-derived schema" in " ".join(step.evidence_label.lower() for step in run.steps)
    assert "does not create a saved segment" in combined_text
    assert "paid revenue is primary" in combined_text
    assert "not for automatic send, export, or mutation" in combined_text
    assert "no campaign is sent" in combined_text
    assert "no bloomreach object is mutated" in combined_text
    assert "no lift or causality is claimed" in combined_text
    assert "caused revenue" not in combined_text


def test_growth_loop_loomi_fixture_is_diagnostic_and_no_autonomous_action():
    brief = build_growth_loop_action_brief(
        evidence=_evidence(
            billing_connected=True,
            billable_now=True,
            booking_links_count=1,
            billing_ready_count=1,
            tracked_content_count=1,
            booking_count=1,
        ),
        loomi_context=build_fixture_loomi_diagnostic_context(),
    )
    combined_text = " ".join(
        [
            brief.diagnosis_summary,
            brief.next_action_summary,
            brief.prepared_action_body,
            " ".join(brief.loomi_context.recommendations),
            " ".join(brief.limitations),
        ]
    ).lower()

    assert brief.loomi_context.source_kind == "diagnostic_fixture"
    assert "not a second paid-result ledger" in combined_text
    assert "does not execute external mutations" in combined_text
    assert "causal lift" not in combined_text
    assert "caused revenue" not in combined_text
    assert "send automatically" not in combined_text


def test_growth_loop_reviewable_action_is_paid_result_only():
    non_paid_evidence = (
        _evidence(),
        _evidence(
            billing_connected=True,
            billable_now=True,
            booking_links_count=1,
            billing_ready_count=1,
        ),
        _evidence(
            billing_connected=True,
            billable_now=True,
            booking_links_count=1,
            billing_ready_count=1,
            tracked_content_count=1,
        ),
        _evidence(
            billing_connected=True,
            billable_now=True,
            booking_links_count=1,
            billing_ready_count=1,
            tracked_content_count=1,
            booking_count=1,
        ),
    )

    for evidence in non_paid_evidence:
        brief = build_growth_loop_action_brief(evidence=evidence)

        assert brief.reviewable_action is None
        assert brief.decision_trace is None
        assert brief.segment_recipe is None
        assert brief.measurement_plan is None
        assert brief.sandbox_proof is None
        assert brief.agent_console is None


def test_growth_loop_reviewable_action_is_review_only_and_paid_evidence_grounded():
    brief = build_growth_loop_action_brief(
        evidence=_evidence(
            billing_connected=True,
            billable_now=True,
            booking_links_count=1,
            billing_ready_count=1,
            tracked_content_count=1,
            booking_count=1,
            paid_invoice_count=1,
            paid_revenue_cents=19500,
        )
    )

    assert brief.reviewable_action is not None
    action = brief.reviewable_action
    combined_text = " ".join(
        (
            action.summary,
            " ".join(action.target_segment),
            " ".join(action.message_outline),
            " ".join(action.bloomreach_next_step),
            " ".join(action.success_evidence),
            " ".join(action.diagnostic_signals),
            action.copy_ready_text,
            " ".join(action.limitations),
        )
    ).lower()

    assert action.title == "Reviewable recovery brief"
    assert "draft a segment spec" in combined_text
    assert "app-owned paid conversion lift" in combined_text
    assert "stored booking, invoice, and payment-backed records" in combined_text
    assert "campaign.status" in combined_text
    assert "retargeting.audience" in combined_text
    assert "does not mutate bloomreach" in combined_text
    assert "does not count revenue" in combined_text
    assert "prove causality" in combined_text
    assert "send the recovery message" in combined_text
    assert "caused revenue" not in combined_text
    assert "saved segment, campaign, or recommendation" in combined_text


def test_growth_loop_decision_trace_selects_recovery_and_preserves_boundaries():
    brief = build_growth_loop_action_brief(
        evidence=_evidence(
            billing_connected=True,
            billable_now=True,
            booking_links_count=1,
            billing_ready_count=1,
            tracked_content_count=1,
            booking_count=1,
            paid_invoice_count=1,
            paid_revenue_cents=19500,
        )
    )

    assert brief.decision_trace is not None
    trace = brief.decision_trace
    selected = [candidate for candidate in trace.candidates if candidate.status_label == "Selected"]
    assert len(selected) == 1
    assert selected[0].title == "Booking-step recovery brief"
    assert selected[0].score == max(candidate.score for candidate in trace.candidates)
    assert {candidate.title for candidate in trace.candidates} == {
        "Booking-step recovery brief",
        "Broad nurture follow-up",
        "Direct Bloomreach segment or campaign mutation",
    }

    combined_text = " ".join(
        (
            trace.summary,
            trace.guardrail_summary,
            " ".join(trace.scoring_criteria),
            " ".join(trace.evidence_chain),
            " ".join(
                " ".join(
                    (
                        candidate.title,
                        candidate.status_label,
                        candidate.summary,
                        " ".join(candidate.criteria),
                        candidate.outcome,
                        candidate.boundary,
                    )
                )
                for candidate in trace.candidates
            ),
        )
    ).lower()

    assert "schema fit" in combined_text
    assert "app evidence fit" in combined_text
    assert "review safety" in combined_text
    assert "no live llm call is required" in combined_text
    assert "no campaign is sent" in combined_text
    assert "no bloomreach object is mutated" in combined_text
    assert "do not become paid truth" in combined_text
    assert "no saved segment, recommendation, campaign, or external system change" in combined_text
    assert "caused revenue" not in combined_text
    assert "causal lift" not in combined_text


def test_sleepy_goose_schema_opportunity_is_review_only_and_event_grounded():
    opportunity = build_sleepy_goose_schema_opportunity()
    combined_text = " ".join(
        (
            opportunity.source_summary,
            opportunity.opportunity_summary,
            opportunity.app_bridge_summary,
            " ".join(opportunity.required_segment),
            " ".join(opportunity.recommended_action),
            " ".join(opportunity.proof_evidence),
            " ".join(opportunity.event_properties),
            " ".join(opportunity.limitations),
        )
    ).lower()

    assert opportunity.source_label == "Live Loomi schema proof"
    assert opportunity.source_status_label == "Verified via Cursor MCP"
    assert opportunity.project_name == "sleepy-goose"
    assert opportunity.project_id == "b15c09b0-5469-11f1-b333-862b79b06b65"
    assert opportunity.opportunity_title == "Cart-abandon recover & convert"
    assert "booking-step recovery" in opportunity.app_bridge_summary
    assert "cart_update.total_quantity" in opportunity.event_properties
    assert "purchase.purchase_status" in opportunity.event_properties
    assert "campaign.status" in opportunity.event_properties
    assert "retargeting.action" in opportunity.event_properties
    assert "not a live page-load mcp call" in combined_text
    assert "does not send campaigns" in combined_text
    assert "does not count revenue" in combined_text
    assert "prove causality" in combined_text
    assert "caused revenue" not in combined_text
    assert "second paid-result ledger" not in combined_text


def test_growth_loop_segment_recipe_is_bloomreach_ready_and_review_only():
    brief = build_growth_loop_action_brief(
        evidence=_evidence(
            billing_connected=True,
            billable_now=True,
            booking_links_count=1,
            billing_ready_count=1,
            tracked_content_count=1,
            booking_count=1,
            paid_invoice_count=1,
            paid_revenue_cents=19500,
        )
    )

    assert brief.segment_recipe is not None
    recipe = brief.segment_recipe
    combined_text = " ".join(
        (
            recipe.title,
            recipe.summary,
            " ".join(recipe.include_rules),
            " ".join(recipe.exclude_rules),
            " ".join(recipe.recovery_window),
            " ".join(recipe.message_variables),
            " ".join(recipe.measurement_plan),
            recipe.conversation_mcp_note,
            " ".join(recipe.limitations),
        )
    ).lower()

    assert recipe.title == "Bloomreach-ready segment recipe"
    assert "cart_update" in combined_text
    assert "total_quantity is greater than zero" in combined_text
    assert "completed purchase" in combined_text
    assert "24-hour recovery window" in combined_text
    assert "app-owned paid invoices and payment-backed records" in combined_text
    assert "holdout or non-targeted group" in combined_text
    assert "campaign.status" in combined_text
    assert "retargeting.audience" in combined_text
    assert "conversation mcp" in combined_text
    assert "catalog-proxy signals" in combined_text
    assert "support, checkout, booking, refund, or payment-failure telemetry" in combined_text
    assert "does not create a saved bloomreach segment" in combined_text
    assert "no campaign is sent" in combined_text
    assert "no external system is mutated" in combined_text
    assert "do not count revenue" in combined_text
    assert "prove causality" in combined_text
    assert "caused revenue" not in combined_text


def test_growth_loop_measurement_plan_is_holdout_first_and_no_lift_yet():
    brief = build_growth_loop_action_brief(
        evidence=_evidence(
            billing_connected=True,
            billable_now=True,
            booking_links_count=1,
            billing_ready_count=1,
            tracked_content_count=1,
            booking_count=1,
            paid_invoice_count=1,
            paid_revenue_cents=19500,
        )
    )

    assert brief.measurement_plan is not None
    plan = brief.measurement_plan
    combined_text = " ".join(
        (
            plan.title,
            plan.summary,
            " ".join(card.label for card in plan.cards),
            " ".join(card.title for card in plan.cards),
            " ".join(card.detail for card in plan.cards),
            " ".join(plan.limitations),
        )
    ).lower()

    assert plan.title == "Measurement plan"
    assert "paid revenue" in combined_text
    assert "app-owned paid invoices and payment-backed records" in combined_text
    assert "paid conversion rate" in combined_text
    assert "paid invoice count" in combined_text
    assert "withheld holdout first" in combined_text
    assert "non-targeted comparison" in combined_text
    assert "within 24 hours" in combined_text
    assert "for 7 days" in combined_text
    assert "campaign.status" in combined_text
    assert "retargeting.audience" in combined_text
    assert "engagement, not to count revenue" in combined_text
    assert "no lift yet" in combined_text
    assert "do not claim lift" in combined_text
    assert "causality" in combined_text
    assert "statistical confidence" in combined_text
    assert "revenue improvement until the campaign runs" in combined_text
    assert "does not report measured lift or causal impact" in combined_text
    assert "canonical invoice and payment records remain paid truth" in combined_text


def test_growth_loop_agent_console_packages_review_packet_and_boundaries():
    brief = build_growth_loop_action_brief(
        evidence=_evidence(
            billing_connected=True,
            billable_now=True,
            booking_links_count=1,
            billing_ready_count=1,
            tracked_content_count=1,
            booking_count=1,
            paid_invoice_count=1,
            paid_revenue_cents=19500,
        )
    )

    assert brief.agent_console is not None
    console = brief.agent_console
    packet = console.review_packet
    combined_text = " ".join(
        (
            console.title,
            console.summary,
            console.primary_action_label,
            " ".join(step.label for step in console.steps),
            " ".join(step.title for step in console.steps),
            " ".join(step.detail for step in console.steps),
            " ".join(signal.label for signal in console.capability_signals),
            " ".join(signal.value for signal in console.capability_signals),
            " ".join(signal.detail for signal in console.capability_signals),
            packet.title,
            packet.summary,
            packet.selected_action,
            packet.segment_summary,
            packet.measurement_summary,
            " ".join(packet.proof_chain),
            " ".join(packet.boundaries),
        )
    ).lower()

    assert [step.label for step in console.steps] == ["Proof", "Schema", "Action", "Segment", "Measure"]
    assert console.primary_action_label == "View review packet"
    assert packet.title == "Review packet"
    assert "app-owned paid truth" in combined_text
    assert "cursor mcp schema proof" in combined_text
    assert "paypal-shaped outcome proof" in combined_text
    assert "review-only action" in combined_text
    assert "1 paid invoice" in combined_text
    assert "$195.00" in combined_text
    assert "booking-step recovery brief" in combined_text
    assert "24-hour recovery window" in combined_text
    assert "withheld holdout first" in combined_text
    assert "no campaign is sent" in combined_text
    assert "no bloomreach object is mutated" in combined_text
    assert "no lift is claimed yet" in combined_text
    assert "app-owned invoice and payment records remain paid truth" in combined_text
    assert "not a live page-load mcp call" in combined_text
    assert "causal lift" not in combined_text
