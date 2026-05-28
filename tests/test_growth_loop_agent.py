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
