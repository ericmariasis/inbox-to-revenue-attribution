from dataclasses import dataclass

from app.models.billing_provider import BILLING_PROVIDER_PAYPAL, BILLING_PROVIDER_STRIPE
from app.models.billing_provider_switch_attempt import BillingProviderSwitchAttempt
from app.services.billing_provider import (
    BILLING_ACCOUNT_READINESS_ISSUE_COMPLETE_STRIPE_SETUP,
    BILLING_ACCOUNT_READINESS_ISSUE_CONFIRM_PAYPAL_PRIMARY_EMAIL,
    BILLING_ACCOUNT_READINESS_ISSUE_ENABLE_PAYPAL_PAYMENTS_RECEIVABLE,
    BILLING_ACCOUNT_READINESS_ISSUE_GRANT_PAYPAL_THIRD_PARTY_PERMISSIONS,
)
from app.services.billing_provider_switch import (
    BillingProviderSwitchCleanState,
    replacement_billing_provider_name,
)
from app.services.creator_workspace_state import (
    CreatorWorkspaceReadiness,
)


_BILLING_PROVIDER_SETUP_STATE_BLOCKED = "blocked"
_BILLING_PROVIDER_SETUP_STATE_NOT_READY = "not_ready"

PAYPAL_UNAVAILABLE_CREATOR_COPY = (
    "PayPal setup is not yet available for general creators. "
    "Stripe remains the supported self-serve billing path for now."
)


@dataclass(frozen=True)
class CreatorSetupHomeMilestoneView:
    title: str
    badge_label: str
    badge_class: str
    question: str
    body: str
    next_title: str
    next_copy: str
    proof_title: str
    proof_copy: str
    action: dict[str, str]


@dataclass(frozen=True)
class CreatorSetupHomeAttentionSummaryView:
    title: str | None
    body: str
    action: dict[str, str] | None
    inline_prefix: str | None = None
    inline_link_label: str | None = None
    inline_suffix: str | None = None


@dataclass(frozen=True)
class CreatorAccountBillingManagementView:
    label: str
    body: str
    badge_class: str
    action_mode: str
    action: dict[str, str] | None = None
    action_label_override: str | None = None


@dataclass(frozen=True)
class CreatorAttentionOverviewView:
    blocked_heading: str
    blocked_backlog_copy: str
    blocked_explainer: str
    unmatched_heading: str
    unmatched_backlog_copy: str
    unmatched_explainer: str


def billing_provider_connect_action(
    *,
    provider_name: str | None,
    reconnect: bool,
    paypal_available_to_creator: bool = True,
) -> dict[str, str] | None:
    normalized_provider = (provider_name or BILLING_PROVIDER_STRIPE).strip().lower()
    if normalized_provider == BILLING_PROVIDER_PAYPAL:
        if not paypal_available_to_creator:
            return None
        return {
            "label": "Reconnect PayPal" if reconnect else "Start PayPal setup",
            "href": "/app/paypal/connect/start",
        }
    return {
        "label": "Reconnect Stripe" if reconnect else "Start Stripe setup",
        "href": "/app/stripe/connect/start",
    }


def billing_provider_label(raw_provider: str | None) -> str:
    normalized_provider = (raw_provider or "").strip().lower()
    if normalized_provider == BILLING_PROVIDER_STRIPE:
        return "Stripe"
    if normalized_provider == BILLING_PROVIDER_PAYPAL:
        return "PayPal"
    if normalized_provider:
        return normalized_provider.replace("_", " ").title()
    return "Not connected"


def billing_provider_is_connected_but_blocked(
    readiness: CreatorWorkspaceReadiness,
) -> bool:
    return (
        readiness.billing_connected
        and readiness.billing_provider_guidance_state == _BILLING_PROVIDER_SETUP_STATE_BLOCKED
    )


def billing_provider_is_connected_but_not_ready(
    readiness: CreatorWorkspaceReadiness,
) -> bool:
    return (
        readiness.billing_connected
        and readiness.billing_provider_guidance_state == _BILLING_PROVIDER_SETUP_STATE_NOT_READY
    )


def billing_provider_blocked_copy(*, provider_name: str | None) -> str:
    provider_label = billing_provider_label(provider_name)
    return (
        f"{provider_label} is connected, but its invoice readiness could not be verified right now. "
        "Try again later before relying on new bookings."
    )


def billing_provider_not_ready_copy(
    readiness: CreatorWorkspaceReadiness,
) -> str:
    provider_label = billing_provider_label(readiness.billing_provider)
    return (
        f"{provider_label} is connected, but it still needs this setup work before it can create "
        f"invoices: "
        f"{billing_provider_actionable_issue_copy(readiness.billing_provider, readiness.billing_provider_actionable_issue_codes)}."
    )


def billing_provider_actionable_issue_copy(
    provider_name: str | None,
    issue_codes: tuple[str, ...],
) -> str:
    provider_label = billing_provider_label(provider_name)
    ordered_issue_codes = tuple(dict.fromkeys(issue_codes))
    actions = [
        action
        for issue_code, action in (
            (
                BILLING_ACCOUNT_READINESS_ISSUE_COMPLETE_STRIPE_SETUP,
                "finish the remaining Stripe account setup in Stripe",
            ),
            (
                BILLING_ACCOUNT_READINESS_ISSUE_CONFIRM_PAYPAL_PRIMARY_EMAIL,
                "confirm the primary email on the connected PayPal business account",
            ),
            (
                BILLING_ACCOUNT_READINESS_ISSUE_ENABLE_PAYPAL_PAYMENTS_RECEIVABLE,
                "finish the PayPal payments-receivable setup",
            ),
            (
                BILLING_ACCOUNT_READINESS_ISSUE_GRANT_PAYPAL_THIRD_PARTY_PERMISSIONS,
                "reconnect the PayPal business account and grant this platform the required PayPal permissions",
            ),
        )
        if issue_code in ordered_issue_codes
    ]
    if not actions:
        return f"finish the remaining {provider_label} account setup"
    return _human_join(actions)


def billing_provider_switch_blockers_copy(
    *,
    switch_clean_state: BillingProviderSwitchCleanState,
) -> str:
    blockers: list[str] = []
    if switch_clean_state.open_invoice_count > 0:
        blockers.append(_count_copy(switch_clean_state.open_invoice_count, "open invoice"))
    if switch_clean_state.blocked_billing_count > 0:
        blockers.append(
            _count_copy(
                switch_clean_state.blocked_billing_count,
                "billing issue that still needs review",
                "billing issues that still need review",
            )
        )
    return _human_join(blockers)


def has_limited_tracking_only_booking_links(
    readiness: CreatorWorkspaceReadiness,
) -> bool:
    return (
        readiness.booking_links_count > 0
        and readiness.trackable_booking_links_count == 0
        and readiness.limited_tracking_booking_links_count > 0
    )


def has_inactive_creator_booking_links(
    readiness: CreatorWorkspaceReadiness,
) -> bool:
    return (
        readiness.booking_links_count > 0
        and readiness.trackable_booking_links_count == 0
        and readiness.limited_tracking_booking_links_count == 0
    )


def blocked_billing_backlog_copy(blocked_billing_count: int) -> str:
    if blocked_billing_count == 0:
        return "No tracked bookings are blocked before invoicing right now."
    return (
        f"{_count_copy(blocked_billing_count, 'booking')} still blocked before invoicing "
        "and outside paid totals."
    )


def unmatched_payment_backlog_copy(event_count: int) -> str:
    if event_count == 0:
        return "No unmatched payment events are waiting right now."
    return (
        f"{_count_copy(event_count, 'payment event')} still diagnostic only and outside paid totals "
        "while the attribution chain is incomplete."
    )


def _human_join(parts: list[str]) -> str:
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return f"{', '.join(parts[:-1])}, and {parts[-1]}"


def _count_copy(count: int, singular: str, plural: str | None = None) -> str:
    label = singular if count == 1 else plural or f"{singular}s"
    return f"{count} {label}"


def _connected_account_billing_body(readiness: CreatorWorkspaceReadiness) -> str:
    if billing_provider_is_connected_but_blocked(readiness):
        return billing_provider_blocked_copy(provider_name=readiness.billing_provider)
    if billing_provider_is_connected_but_not_ready(readiness):
        return billing_provider_not_ready_copy(readiness)
    if readiness.billable_now:
        return (
            "This workspace has a connected billing provider and is billable now for future "
            "invoicing."
        )
    return (
        "This workspace has a connected billing provider, but it is not billable now yet. Save "
        "amount and currency on at least one booking link before new bookings can move into invoicing."
    )


def _billing_provider_switch_attempt_body(
    *,
    current_provider_label: str,
    switch_attempt: BillingProviderSwitchAttempt,
    switch_clean_state: BillingProviderSwitchCleanState,
    switch_target_guidance_state: str,
    switch_target_actionable_issue_codes: tuple[str, ...],
) -> str:
    target_provider_label = billing_provider_label(switch_attempt.target_billing_provider)
    if (
        switch_attempt.target_billing_connect_status != "connected"
        or switch_attempt.target_billing_account_id is None
    ):
        return (
            f"A {target_provider_label} switch is in progress. {current_provider_label} stays active "
            f"until {target_provider_label} is connected, ready, and you commit the switch."
        )
    if switch_target_guidance_state == _BILLING_PROVIDER_SETUP_STATE_BLOCKED:
        return (
            f"{target_provider_label} is connected for the pending switch, but its invoice readiness "
            "could not be verified right now. "
            f"{current_provider_label} stays active until the readiness check succeeds and you commit "
            "the switch."
        )
    if switch_target_guidance_state == _BILLING_PROVIDER_SETUP_STATE_NOT_READY:
        return (
            f"{target_provider_label} is connected for the pending switch, but it still needs this "
            f"setup work before it can create invoices: "
            f"{billing_provider_actionable_issue_copy(switch_attempt.target_billing_provider, switch_target_actionable_issue_codes)}. "
            f"{current_provider_label} stays active until {target_provider_label} is ready and you commit the switch."
        )
    if not switch_clean_state.is_clean:
        return (
            f"{target_provider_label} is connected for the pending switch, but finishing the switch is "
            f"blocked because this workspace still has "
            f"{billing_provider_switch_blockers_copy(switch_clean_state=switch_clean_state)}. "
            f"{current_provider_label} stays active until those items are cleared."
        )
    return (
        f"{target_provider_label} is connected and ready for the pending switch. "
        f"{current_provider_label} stays active until you commit the switch."
    )


def build_setup_home_milestone_view(
    *,
    readiness: CreatorWorkspaceReadiness,
    attention_count: int,
    tracked_booking_count: int,
    show_provider_choice: bool,
    paypal_available_to_creator: bool,
) -> CreatorSetupHomeMilestoneView:
    if readiness.paid_invoice_count > 0:
        return CreatorSetupHomeMilestoneView(
            title="First paid result is already landing",
            badge_label="Paid",
            badge_class="connected",
            question="What is working and where should I look next?",
            body=(
                "The setup proof is already real here. At least one canonical paid result is attached to this workspace, "
                "so the next job is understanding which tracked content and bookings are producing it."
            ),
            next_title="Review paid results",
            next_copy="Open reports to review the counted paid results already attached to this workspace.",
            proof_title=f"{_count_copy(readiness.paid_invoice_count, 'paid result')} already counted",
            proof_copy=(
                "This is the first-value milestone, not just booking activity. Canonical paid invoices are already counted."
            ),
            action={
                "title": "Review paid results",
                "copy_html": "Open reports to review the counted paid results already attached to this workspace.",
                "action_label": "Open Reports",
                "action_href": "/app/reports",
                "action_method": "get",
            },
        )

    if readiness.waiting_for_first_paid_result and tracked_booking_count > 0:
        return CreatorSetupHomeMilestoneView(
            title="Bookings are landing; paid proof is next",
            badge_label="Current",
            badge_class="pending",
            question="Why do bookings show up before revenue?",
            body=(
                "Tracked bookings already prove the funnel is working. Revenue stays empty until the matching invoice path is "
                "complete enough to count as canonical paid truth."
            ),
            next_title="Review the content funnel",
            next_copy="Open reports to review the bookings already recorded and see why paid results have not landed yet.",
            proof_title=f"{_count_copy(tracked_booking_count, 'tracked booking')} already recorded",
            proof_copy="Activity is already visible. This is a waiting-on-paid-truth state, not a broken-tracking state.",
            action={
                "title": "Review the content funnel",
                "copy_html": "Open reports to review the bookings already recorded and see why paid results have not landed yet.",
                "action_label": "Open Reports",
                "action_href": "/app/reports",
                "action_method": "get",
            },
        )

    if readiness.waiting_for_first_paid_result:
        return CreatorSetupHomeMilestoneView(
            title="Ready to track",
            badge_label="Current",
            badge_class="connected",
            question="Am I set up correctly?",
            body=(
                "This workspace is ready to track. The next milestone is real activity: a tracked booking and then a matching paid invoice."
            ),
            next_title="Copy or share a tracked link",
            next_copy="Open content to copy the tracked link that is already ready to share from this billable setup.",
            proof_title=f"{_count_copy(readiness.tracked_content_count, 'tracked link')} ready to share",
            proof_copy="Tracking is ready. Reports stay quiet until real activity lands, which is different from setup failing.",
            action={
                "title": "Copy or share a tracked link",
                "copy_html": "Open content to copy the tracked link that is ready to share from this billable setup.",
                "action_label": "Open content",
                "action_href": "/app/content",
                "action_method": "get",
            },
        )

    if readiness.billable_now:
        return CreatorSetupHomeMilestoneView(
            title="Billable now",
            badge_label="Current",
            badge_class="connected",
            question="How do I start tracking real activity?",
            body=(
                "Billing is ready. The next milestone is creating tracked content so the links you share can carry attribution into bookings."
            ),
            next_title="Create tracked content",
            next_copy="Open content and create the first tracked link for this billable setup.",
            proof_title=f"{_count_copy(readiness.billing_ready_count, 'saved link')} already billable now",
            proof_copy="At least one saved booking link already has amount and currency, so invoicing can be trusted once activity arrives.",
            action={
                "title": "Create tracked content",
                "copy_html": "Open content and create the first tracked link for this billable setup.",
                "action_label": "Open content",
                "action_href": "/app/content",
                "action_method": "get",
            },
        )

    if billing_provider_is_connected_but_blocked(readiness):
        return CreatorSetupHomeMilestoneView(
            title="Billing setup needs review",
            badge_label="Blocked",
            badge_class="disconnected",
            question="Is something wrong before this workspace becomes billable now?",
            body=billing_provider_blocked_copy(provider_name=readiness.billing_provider),
            next_title="Review billing connection",
            next_copy="Open account to review the connected provider and the readiness issue before relying on new bookings.",
            proof_title="A billing provider is connected",
            proof_copy="The connection exists, but invoice readiness could not be verified cleanly for future billing.",
            action={
                "title": "Review billing connection",
                "copy_html": "Open account to review the connected provider and the readiness issue before relying on new bookings.",
                "action_label": "Open account",
                "action_href": "/app/account",
                "action_method": "get",
            },
        )

    if billing_provider_is_connected_but_not_ready(readiness):
        return CreatorSetupHomeMilestoneView(
            title="Connected, but not billable now",
            badge_label="Current",
            badge_class="pending",
            question="What is keeping this workspace from becoming billable now?",
            body=billing_provider_not_ready_copy(readiness),
            next_title="Review billing readiness",
            next_copy="Open account to review the connected provider and the setup work still needed before invoicing can start.",
            proof_title="A billing provider is already connected",
            proof_copy="The workspace is past the first connection step, but the provider still is not ready to create invoices.",
            action={
                "title": "Review billing readiness",
                "copy_html": "Open account to review the connected provider and the setup work still needed before invoicing can start.",
                "action_label": "Open account",
                "action_href": "/app/account",
                "action_method": "get",
            },
        )

    if readiness.billing_connect_status == "disconnected":
        provider_action = billing_provider_connect_action(
            provider_name=readiness.billing_provider,
            reconnect=True,
            paypal_available_to_creator=paypal_available_to_creator,
        )
        if provider_action is None:
            return CreatorSetupHomeMilestoneView(
                title="Reconnect billing setup",
                badge_label="Blocked",
                badge_class="disconnected",
                question="How do I restore billing safely?",
                body=(
                    f"This workspace was connected to {billing_provider_label(readiness.billing_provider)} before, but it is disconnected now. "
                    f"{PAYPAL_UNAVAILABLE_CREATOR_COPY}"
                ),
                next_title="Review billing connection",
                next_copy=PAYPAL_UNAVAILABLE_CREATOR_COPY,
                proof_title="Historical workspace data stays here",
                proof_copy="Reconnection affects future billing readiness. Existing local history remains attached to this workspace.",
                action={
                    "title": "Review billing connection",
                    "copy_html": PAYPAL_UNAVAILABLE_CREATOR_COPY,
                    "action_label": "Open account",
                    "action_href": "/app/account",
                    "action_method": "get",
                },
            )
        return CreatorSetupHomeMilestoneView(
            title="Reconnect billing setup",
            badge_label="Blocked",
            badge_class="disconnected",
            question="What do I need to restore before new bookings can move into invoicing?",
            body=(
                f"This workspace was connected to {billing_provider_label(readiness.billing_provider)} before, but it is disconnected now. "
                "Reconnect it before you rely on new bookings."
            ),
            next_title=provider_action["label"],
            next_copy="Reconnect billing so the workspace can return to the billable-now path.",
            proof_title="Historical workspace data stays here",
            proof_copy="Reconnection affects future billing readiness. Existing local history remains attached to this workspace.",
            action={
                "title": provider_action["label"],
                "copy_html": "Reconnect billing so the workspace can return to the billable-now path.",
                "action_label": provider_action["label"],
                "action_href": provider_action["href"],
                "action_method": "post",
            },
        )

    if readiness.billing_connected:
        if readiness.booking_links_count == 0:
            return CreatorSetupHomeMilestoneView(
                title="Connected, but not billable now",
                badge_label="Current",
                badge_class="pending",
                question="What does this workspace need before it can bill real activity?",
                body="A billing provider is connected. The next milestone is saving a booking link and adding billing defaults.",
                next_title="Add your first booking link",
                next_copy="Open booking links and save the destination this creator actually uses.",
                proof_title="The billing connection step is already done",
                proof_copy="The next blocker is configuration, not connection.",
                action={
                    "title": "Add your first booking link",
                    "copy_html": "Open booking links and save the destination this creator actually uses.",
                    "action_label": "Open booking links",
                    "action_href": "/app/booking-links",
                    "action_method": "get",
                },
            )
        if has_limited_tracking_only_booking_links(readiness):
            next_copy = "Open booking links and add a creator-visible tracked-content-ready link before relying on billable-now state."
            body = (
                "Saved booking sources can generate tracked redirects now, but billable-now readiness still waits for end-to-end provider support."
            )
        elif has_inactive_creator_booking_links(readiness):
            next_copy = "Open booking links and add a currently supported booking link for creator-tracked setup."
            body = (
                "Saved booking sources are not active for creator-tracked workflows right now, so this workspace is still not billable now."
            )
        else:
            next_copy = "Open booking links and add amount and currency so at least one saved link becomes billable now."
            body = "A billing provider is connected, but this workspace still needs amount and currency on at least one saved booking link."
        return CreatorSetupHomeMilestoneView(
            title="Connected, but not billable now",
            badge_label="Current",
            badge_class="pending",
            question="What is keeping this workspace from becoming billable now?",
            body=body,
            next_title="Become billable now",
            next_copy=next_copy,
            proof_title=f"{_count_copy(readiness.booking_links_count, 'booking link')} already saved",
            proof_copy="The workspace has moved beyond connection. The remaining gap is making one saved link usable for creator billing.",
            action={
                "title": "Become billable now",
                "copy_html": next_copy,
                "action_label": "Open booking links",
                "action_href": "/app/booking-links",
                "action_method": "get",
            },
        )

    if show_provider_choice:
        next_copy = (
            "Choose Stripe or PayPal to start billing setup. This release still keeps one active billing provider per workspace."
            if paypal_available_to_creator
            else "Choose Stripe to start billing setup. PayPal setup is not yet available for general creators."
        )
        proof_copy = (
            "Nothing is broken yet. This workspace is simply still before the first billing milestone."
            if attention_count == 0
            else "Billing has not started yet, and blocked or unresolved items will stay separate if they appear later."
        )
        return CreatorSetupHomeMilestoneView(
            title="Choose billing provider",
            badge_label="Start here",
            badge_class="pending",
            question="What do I need to do first?",
            body="A billing provider must be connected before this workspace can turn new bookings into invoices.",
            next_title="Start billing setup",
            next_copy=next_copy,
            proof_title="No provider is connected yet",
            proof_copy=proof_copy,
            action={
                "title": "Start billing setup",
                "copy_html": next_copy,
                "action_label": "",
                "action_href": "",
                "action_method": "provider-choice",
            },
        )

    provider_action = billing_provider_connect_action(
        provider_name=readiness.billing_provider,
        reconnect=False,
        paypal_available_to_creator=paypal_available_to_creator,
    )
    if provider_action is None:
        return CreatorSetupHomeMilestoneView(
            title="Review billing setup",
            badge_label="Start here",
            badge_class="pending",
            question="What do I need to do first?",
            body=PAYPAL_UNAVAILABLE_CREATOR_COPY,
            next_title="Review billing options",
            next_copy=PAYPAL_UNAVAILABLE_CREATOR_COPY,
            proof_title="No provider is connected yet",
            proof_copy="This workspace is still before the first billing milestone.",
            action={
                "title": "Review billing options",
                "copy_html": PAYPAL_UNAVAILABLE_CREATOR_COPY,
                "action_label": "Open account",
                "action_href": "/app/account",
                "action_method": "get",
            },
        )
    return CreatorSetupHomeMilestoneView(
        title="Start billing setup",
        badge_label="Start here",
        badge_class="pending",
        question="What do I need to do first?",
        body="A billing provider is required before this workspace can turn new bookings into invoices.",
        next_title=provider_action["label"],
        next_copy="Finish billing setup so the rest of the shell can move toward a billable workspace.",
        proof_title="No provider is connected yet",
        proof_copy="The workspace is still before the first billing milestone, not in a broken state.",
        action={
            "title": provider_action["label"],
            "copy_html": "Finish billing setup so the rest of the shell can move toward a billable workspace.",
            "action_label": provider_action["label"],
            "action_href": provider_action["href"],
            "action_method": "post",
        },
    )


def build_setup_home_attention_summary_view(
    attention_count: int,
) -> CreatorSetupHomeAttentionSummaryView:
    if attention_count == 0:
        return CreatorSetupHomeAttentionSummaryView(
            title=None,
            body="Blocked billing and unresolved payments will appear on Attention if anything needs repair.",
            action=None,
            inline_prefix="Blocked billing and unresolved payments will appear on ",
            inline_link_label="Attention",
            inline_suffix=" if anything needs repair.",
        )

    review_count_copy = _count_copy(attention_count, "attention item")
    review_heading = (
        f"{review_count_copy} still needs review"
        if attention_count == 1
        else f"{review_count_copy} still need review"
    )
    return CreatorSetupHomeAttentionSummaryView(
        title=review_heading,
        body=(
            "Blocked billing and unresolved payments stay outside paid totals until the repair or attribution issue is resolved. "
            "Review them separately so diagnostic backlog does not get mistaken for revenue truth."
        ),
        action={"label": "Open Attention", "href": "/app/attention"},
    )


def build_attention_overview_view(
    *,
    blocked_count: int,
    unmatched_count: int,
) -> CreatorAttentionOverviewView:
    return CreatorAttentionOverviewView(
        blocked_heading="Tracked bookings blocked before invoicing",
        blocked_backlog_copy=blocked_billing_backlog_copy(blocked_count),
        blocked_explainer=(
            "These cases explain why a tracked booking did not become an invoice yet. Retry only after the stored setup or provider condition has actually changed."
        ),
        unmatched_heading="Verified payments still diagnostic-only",
        unmatched_backlog_copy=unmatched_payment_backlog_copy(unmatched_count),
        unmatched_explainer=(
            "These are real provider payment events, but they stay diagnostic until the attribution chain is complete enough to enter canonical paid truth."
        ),
    )


def build_account_billing_management_view(
    *,
    current_billing_provider: str | None,
    readiness: CreatorWorkspaceReadiness,
    show_provider_choice: bool,
    switch_attempt: BillingProviderSwitchAttempt | None,
    switch_clean_state: BillingProviderSwitchCleanState,
    switch_target_guidance_state: str,
    switch_target_actionable_issue_codes: tuple[str, ...],
    paypal_available_to_creator: bool,
) -> CreatorAccountBillingManagementView:
    normalized_status = readiness.billing_connect_status
    if normalized_status == "connected":
        current_provider_label = billing_provider_label(current_billing_provider)
        target_provider_name = replacement_billing_provider_name(
            current_provider=current_billing_provider
        )
        target_provider_label = billing_provider_label(target_provider_name)
        body = _connected_account_billing_body(readiness)
        if switch_attempt is not None:
            body = _billing_provider_switch_attempt_body(
                current_provider_label=current_provider_label,
                switch_attempt=switch_attempt,
                switch_clean_state=switch_clean_state,
                switch_target_guidance_state=switch_target_guidance_state,
                switch_target_actionable_issue_codes=switch_target_actionable_issue_codes,
            )
            if (
                switch_attempt.target_billing_provider == BILLING_PROVIDER_PAYPAL
                and not paypal_available_to_creator
            ):
                body = (
                    f"{body} {PAYPAL_UNAVAILABLE_CREATOR_COPY} "
                    "Cancel the pending switch if you need to stay on the current provider."
                )
            return CreatorAccountBillingManagementView(
                label="Connected",
                body=body,
                badge_class="connected",
                action_mode="switch-attempt",
            )

        if switch_clean_state.is_clean:
            target_provider_action = billing_provider_connect_action(
                provider_name=target_provider_name,
                reconnect=False,
                paypal_available_to_creator=paypal_available_to_creator,
            )
            if target_provider_action is None:
                body = f"{body} {PAYPAL_UNAVAILABLE_CREATOR_COPY}"
                return CreatorAccountBillingManagementView(
                    label="Connected",
                    body=body,
                    badge_class="connected",
                    action_mode="none",
                )
            body = (
                f"{body} You can start a {target_provider_label} switch here. "
                f"{current_provider_label} stays active until {target_provider_label} is connected, "
                "ready, and you commit the switch."
            )
            return CreatorAccountBillingManagementView(
                label="Connected",
                body=body,
                badge_class="connected",
                action_mode="simple",
                action=target_provider_action,
                action_label_override=f"Start {target_provider_label} switch",
            )

        body = (
            f"{body} Provider switching is blocked right now because this workspace still has "
            f"{billing_provider_switch_blockers_copy(switch_clean_state=switch_clean_state)}. "
            f"Clear those items before starting a {target_provider_label} switch."
        )
        return CreatorAccountBillingManagementView(
            label="Connected",
            body=body,
            badge_class="connected",
            action_mode="none",
        )

    if normalized_status == "disconnected":
        provider_action = billing_provider_connect_action(
            provider_name=readiness.billing_provider,
            reconnect=True,
            paypal_available_to_creator=paypal_available_to_creator,
        )
        body = (
            f"This workspace is not currently connected to {billing_provider_label(readiness.billing_provider)} "
            "for invoicing. You can reconnect it here when you are ready."
        )
        if provider_action is None:
            return CreatorAccountBillingManagementView(
                label="Disconnected",
                body=(
                    f"This workspace is not currently connected to {billing_provider_label(readiness.billing_provider)} "
                    f"for invoicing. {PAYPAL_UNAVAILABLE_CREATOR_COPY}"
                ),
                badge_class="disconnected",
                action_mode="none",
            )
        return CreatorAccountBillingManagementView(
            label="Disconnected",
            body=body,
            badge_class="disconnected",
            action_mode="simple",
            action=provider_action,
        )

    if show_provider_choice:
        body = (
            "This workspace is not currently connected to a billing provider for invoicing. "
            "Choose Stripe or PayPal here when you are ready. No billing provider is preselected "
            "for this workspace."
            if paypal_available_to_creator
            else "This workspace is not currently connected to a billing provider for invoicing. "
            "Choose Stripe here when you are ready. PayPal setup is not yet available for general creators."
        )
        return CreatorAccountBillingManagementView(
            label="Pending",
            body=body,
            badge_class="pending",
            action_mode="provider-choice",
        )

    provider_action = billing_provider_connect_action(
        provider_name=readiness.billing_provider,
        reconnect=False,
        paypal_available_to_creator=paypal_available_to_creator,
    )
    if provider_action is None:
        return CreatorAccountBillingManagementView(
            label="Pending",
            body=(
                "This workspace is not currently connected to a billing provider for invoicing. "
                f"{PAYPAL_UNAVAILABLE_CREATOR_COPY}"
            ),
            badge_class="pending",
            action_mode="none",
        )
    return CreatorAccountBillingManagementView(
        label="Pending",
        body=(
            "This workspace is not currently connected to a billing provider for invoicing. "
            "You can continue the current setup here when you are ready."
        ),
        badge_class="pending",
        action_mode="simple",
        action=provider_action,
    )
