import html
from datetime import timezone
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.api.booking_links import (
    create_booking_link_response_for_creator,
    list_booking_link_responses_for_creator,
)
from app.api.deps import get_optional_browser_auth_user
from app.api.stripe import build_stripe_connect_start_response
from app.core.config import get_settings
from app.db.session import get_db
from app.models.auth_user import AuthUser
from app.schemas.booking_link import BookingLinkCreateRequest, BookingLinkResponse
from app.schemas.auth import MagicLinkStartRequest
from app.services.auth_magic_link import start_magic_link
from app.services.browser_session import (
    clear_browser_session_cookie,
    get_browser_session_token,
)

router = APIRouter(include_in_schema=False)

STATUS_MESSAGES = {
    "sent": (
        "Check your inbox",
        "If the address is valid, a fresh magic link is on the way.",
    ),
    "invalid-email": (
        "Enter a valid email",
        "Use a real email address so the sign-in link can be issued safely.",
    ),
    "invalid-link": (
        "That link no longer works",
        "Start again to request a new sign-in link.",
    ),
}

BOOKING_LINK_FORM_FIELDS = (
    "name",
    "calendly_url",
    "billing_amount_cents",
    "billing_currency",
)


@router.get("/")
def root(
    request: Request,
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
) -> RedirectResponse:
    if current_user is not None:
        return _redirect("/app")

    should_clear_cookie = get_browser_session_token(request) is not None
    return _redirect("/sign-in", clear_session=should_clear_cookie)


@router.get("/sign-in")
def sign_in_page(
    request: Request,
    status_value: str | None = Query(default=None, alias="status"),
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
) -> Response:
    if current_user is not None:
        return _redirect("/app")

    response = _html_response(_render_sign_in_page(status_value))
    if get_browser_session_token(request) is not None:
        clear_browser_session_cookie(response, settings=get_settings())
    return response


@router.post("/sign-in")
async def sign_in_start(
    request: Request,
    db: Session = Depends(get_db),
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
) -> Response:
    if current_user is not None:
        return _redirect("/app")

    form_values = await _parse_form_values(request)
    email = form_values.get("email", "")

    try:
        payload = MagicLinkStartRequest(email=email)
    except ValidationError:
        return _redirect("/sign-in?status=invalid-email")

    start_magic_link(db, payload.email)
    return _redirect("/sign-in?status=sent")


@router.get("/app")
def creator_app_shell(
    request: Request,
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    return _html_response(_render_app_shell(current_user))


@router.post("/app/stripe/connect/start")
def creator_stripe_connect_start(
    request: Request,
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    start_response = build_stripe_connect_start_response(
        request=request,
        current_user=current_user,
    )
    return _redirect(str(start_response.onboarding_url))


@router.get("/app/booking-links")
def creator_booking_links_page(
    request: Request,
    status_value: str | None = Query(default=None, alias="status"),
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    booking_links = list_booking_link_responses_for_creator(
        creator_id=current_user.creator_id,
        db=db,
    )
    return _html_response(
        _render_booking_links_page(
            current_user=current_user,
            booking_links=booking_links,
            form_values=_empty_booking_link_form_values(),
            field_errors={},
            status_value=status_value,
        )
    )


@router.post("/app/booking-links")
async def creator_booking_links_create(
    request: Request,
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> Response:
    should_clear_cookie = get_browser_session_token(request) is not None and current_user is None
    if current_user is None:
        return _redirect("/sign-in", clear_session=should_clear_cookie)

    form_values = _booking_link_form_values(await _parse_form_values(request))
    payload, field_errors = _booking_link_payload_from_form(form_values)
    if field_errors:
        booking_links = list_booking_link_responses_for_creator(
            creator_id=current_user.creator_id,
            db=db,
        )
        return _html_response(
            _render_booking_links_page(
                current_user=current_user,
                booking_links=booking_links,
                form_values=form_values,
                field_errors=field_errors,
                status_value=None,
            )
        )

    create_booking_link_response_for_creator(
        creator_id=current_user.creator_id,
        payload=payload,
        db=db,
    )
    return _redirect("/app/booking-links?status=created")


@router.post("/sign-out")
def sign_out() -> RedirectResponse:
    return _redirect("/sign-in", clear_session=True)


async def _parse_form_values(request: Request) -> dict[str, str]:
    parsed = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    return {
        key: values[-1]
        for key, values in parsed.items()
    }


def _redirect(url: str, *, clear_session: bool = False) -> RedirectResponse:
    response = RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)
    response.headers["Cache-Control"] = "no-store"
    if clear_session:
        clear_browser_session_cookie(response, settings=get_settings())
    return response


def _html_response(content: str) -> HTMLResponse:
    response = HTMLResponse(content=content)
    response.headers["Cache-Control"] = "no-store"
    return response


def _empty_booking_link_form_values() -> dict[str, str]:
    return {field_name: "" for field_name in BOOKING_LINK_FORM_FIELDS}


def _booking_link_form_values(raw_values: dict[str, str]) -> dict[str, str]:
    form_values = _empty_booking_link_form_values()
    form_values.update(
        {
            "name": raw_values.get("name", "").strip(),
            "calendly_url": raw_values.get("calendly_url", "").strip(),
            "billing_amount_cents": raw_values.get("billing_amount_cents", "").strip(),
            "billing_currency": raw_values.get("billing_currency", "").strip(),
        }
    )
    return form_values


def _booking_link_payload_from_form(
    form_values: dict[str, str],
) -> tuple[BookingLinkCreateRequest | None, dict[str, str]]:
    field_errors: dict[str, str] = {}
    billing_amount_cents: int | None = None

    if form_values["billing_amount_cents"]:
        try:
            billing_amount_cents = int(form_values["billing_amount_cents"])
        except ValueError:
            field_errors["billing_amount_cents"] = "Enter a whole number of cents."

    if field_errors:
        return None, field_errors

    try:
        payload = BookingLinkCreateRequest(
            name=form_values["name"],
            calendly_url=form_values["calendly_url"],
            billing_amount_cents=billing_amount_cents,
            billing_currency=form_values["billing_currency"] or None,
        )
    except ValidationError as exc:
        return None, _booking_link_field_errors(exc)

    return payload, {}


def _booking_link_field_errors(exc: ValidationError) -> dict[str, str]:
    errors: dict[str, str] = {}
    for error in exc.errors():
        location = error.get("loc") or ()
        field_name = str(location[-1]) if location else ""
        if field_name in BOOKING_LINK_FORM_FIELDS and field_name not in errors:
            errors[field_name] = error["msg"].removeprefix("Value error, ")
    return errors


def _render_sign_in_page(status_value: str | None) -> str:
    message_title = ""
    message_body = ""
    if status_value in STATUS_MESSAGES:
        message_title, message_body = STATUS_MESSAGES[status_value]

    message_block = ""
    if message_title and message_body:
        message_block = (
            f'<section class="notice">'
            f"<p class=\"eyebrow\">{html.escape(message_title)}</p>"
            f"<p>{html.escape(message_body)}</p>"
            f"</section>"
        )

    body = f"""
    <section class="hero">
      <p class="eyebrow">Phase 6.5</p>
      <h1>Creator sign in</h1>
      <p class="lede">Start with your email, then use the magic link to land in the authenticated app shell.</p>
      {message_block}
      <form action="/sign-in" method="post" class="card">
        <label for="email">Email</label>
        <input id="email" name="email" type="email" autocomplete="email" placeholder="creator@example.com" required />
        <button type="submit">Send magic link</button>
      </form>
      <p class="footnote">This first browser slice only covers secure sign-in and app bootstrap. Setup and workflow tools land in the next stories.</p>
    </section>
    """
    return _page_layout(title="Creator sign in", body=body)


def _render_app_shell(current_user: AuthUser) -> str:
    creator_name = html.escape(current_user.creator.name)
    creator_email = html.escape(current_user.email)
    stripe_status = _stripe_setup_home_state(current_user.creator.stripe_connect_status)

    stripe_detail_lines = []
    if current_user.creator.stripe_account_id:
        stripe_detail_lines.append(
            f"<p><strong>Connected account</strong>: "
            f"{html.escape(current_user.creator.stripe_account_id)}</p>"
        )
    if current_user.creator.stripe_connected_at:
        stripe_detail_lines.append(
            f"<p><strong>Connected on</strong>: "
            f"{_format_connected_at(current_user.creator.stripe_connected_at)}</p>"
        )

    stripe_action = ""
    if stripe_status["button_label"]:
        stripe_action = f"""
        <form action="/app/stripe/connect/start" method="post">
          <button type="submit">{html.escape(stripe_status["button_label"])}</button>
        </form>
        """

    body = f"""
    <header class="shell-header">
      <div>
        <p class="eyebrow">Creator Home</p>
        <h1>Setup Home</h1>
        <p class="lede">See where setup stands and connect Stripe before the later billing phase starts creating invoices on your account.</p>
      </div>
      <form action="/sign-out" method="post">
        <button type="submit" class="secondary">Sign out</button>
      </form>
    </header>
    {_render_shell_nav(current_path="/app")}
    <section class="grid">
      <article class="card">
        <p class="eyebrow">Account</p>
        <h2>{creator_name}</h2>
        <p>Signed in as <strong>{creator_email}</strong></p>
        <p>This is the current creator workspace for the thin Phase 6.5 setup flow.</p>
      </article>
      <article class="card accent">
        <p class="eyebrow">Stripe status</p>
        <div class="status-row">
          <h2>{html.escape(stripe_status["heading"])}</h2>
          <span class="status-pill {html.escape(stripe_status["badge_class"])}">{html.escape(stripe_status["label"])}</span>
        </div>
        <p>{html.escape(stripe_status["description"])}</p>
        {"".join(stripe_detail_lines)}
        {stripe_action}
      </article>
    </section>
    <section class="grid">
      <article class="card">
        <p class="eyebrow">Setup checklist</p>
        <h2>What still needs to happen</h2>
        <ul class="checklist">
          <li class="checklist-item {html.escape(stripe_status["item_class"])}">
            <div>
              <strong>Connect Stripe</strong>
              <p>{html.escape(stripe_status["checklist_copy"])}</p>
            </div>
            <span class="list-state">{html.escape(stripe_status["checklist_label"])}</span>
          </li>
          <li class="checklist-item next">
            <div>
              <strong>Add a booking link</strong>
              <p>Register the Calendly link and billing defaults that later invoice automation will trust. <a href="/app/booking-links" class="inline-link">Open booking-link manager</a>.</p>
            </div>
            <span class="list-state">Next story</span>
          </li>
          <li class="checklist-item next">
            <div>
              <strong>Create a tracked link</strong>
              <p>Attach a post URL to a booking link so future bookings carry the right content identifier.</p>
            </div>
            <span class="list-state">After booking links</span>
          </li>
        </ul>
      </article>
      <article class="card">
        <p class="eyebrow">Why Stripe matters</p>
        <h2>Connect payouts before billing automation lands</h2>
        <p>Connect Stripe now so later invoice automation can create invoices on your account.</p>
        <p>This setup page does not mean payment attribution is complete yet. It only exposes setup status while invoicing and reporting remain later stories.</p>
      </article>
    </section>
    """
    return _page_layout(title="Creator Home", body=body)


def _render_shell_nav(*, current_path: str) -> str:
    links = [
        ("/app", "Setup Home"),
        ("/app/booking-links", "Booking Links"),
    ]
    items = []
    for href, label in links:
        class_name = "nav-link active" if href == current_path else "nav-link"
        items.append(
            f'<a href="{href}" class="{class_name}">{html.escape(label)}</a>'
        )
    return f'<nav class="shell-nav">{"".join(items)}</nav>'


def _render_booking_links_page(
    *,
    current_user: AuthUser,
    booking_links: list[BookingLinkResponse],
    form_values: dict[str, str],
    field_errors: dict[str, str],
    status_value: str | None,
) -> str:
    creator_name = html.escape(current_user.creator.name)
    creator_email = html.escape(current_user.email)
    notice = _render_booking_link_notice(status_value=status_value, field_errors=field_errors)
    list_heading = "Your booking links" if booking_links else "No booking links yet"

    body = f"""
    <header class="shell-header">
      <div>
        <p class="eyebrow">Creator Home</p>
        <h1>Booking Links</h1>
        <p class="lede">Add the Calendly URLs this creator actually uses and, when available, store billing defaults that later invoice automation can trust.</p>
      </div>
      <form action="/sign-out" method="post">
        <button type="submit" class="secondary">Sign out</button>
      </form>
    </header>
    {_render_shell_nav(current_path="/app/booking-links")}
    {notice}
    <section class="grid">
      <article class="card stack">
        <div>
          <p class="eyebrow">Create link</p>
          <h2>Add a booking link</h2>
          <p>Signed in as <strong>{creator_email}</strong> for <strong>{creator_name}</strong>.</p>
        </div>
        <form action="/app/booking-links" method="post">
          <label for="name">Name</label>
          <input
            id="name"
            name="name"
            type="text"
            value="{html.escape(form_values["name"])}"
            placeholder="Discovery Call"
            required
            aria-invalid="{str("name" in field_errors).lower()}"
          />
          {_render_booking_link_field_error(field_errors.get("name"))}

          <label for="calendly_url">Calendly URL</label>
          <input
            id="calendly_url"
            name="calendly_url"
            type="url"
            value="{html.escape(form_values["calendly_url"])}"
            placeholder="https://calendly.com/example/discovery-call"
            required
            aria-invalid="{str("calendly_url" in field_errors).lower()}"
          />
          {_render_booking_link_field_error(field_errors.get("calendly_url"))}

          <label for="billing_amount_cents">Billing amount in cents</label>
          <input
            id="billing_amount_cents"
            name="billing_amount_cents"
            type="number"
            inputmode="numeric"
            min="1"
            step="1"
            value="{html.escape(form_values["billing_amount_cents"])}"
            placeholder="15000"
            aria-invalid="{str("billing_amount_cents" in field_errors).lower()}"
          />
          <p class="form-help">Leave blank to skip defaults for now. Example: 15000 means a USD 150.00 invoice default.</p>
          {_render_booking_link_field_error(field_errors.get("billing_amount_cents"))}

          <label for="billing_currency">Billing currency</label>
          <input
            id="billing_currency"
            name="billing_currency"
            type="text"
            value="{html.escape(form_values["billing_currency"])}"
            placeholder="USD"
            maxlength="3"
            aria-invalid="{str("billing_currency" in field_errors).lower()}"
          />
          <p class="form-help">Use a three-letter code such as USD or EUR. Leave blank if you are not ready to set currency yet.</p>
          {_render_booking_link_field_error(field_errors.get("billing_currency"))}

          <button type="submit">Save booking link</button>
        </form>
      </article>
      <article class="card accent stack">
        <div>
          <p class="eyebrow">Billing defaults</p>
          <h2>Why they matter</h2>
        </div>
        <p>These defaults are optional in Story 39, but later invoice automation will use the stored amount and currency instead of trusting webhook payload values.</p>
        <p>If you leave one or both billing fields blank, the UI will still save the booking link and show exactly what is missing.</p>
        <a href="/app" class="inline-link">Back to setup home</a>
      </article>
    </section>
    <section class="card stack">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Creator-owned links</p>
          <h2>{list_heading}</h2>
        </div>
        <p>{len(booking_links)} saved</p>
      </div>
      {_render_booking_links_list(booking_links)}
    </section>
    """
    return _page_layout(title="Booking Links", body=body)


def _render_booking_links_list(booking_links: list[BookingLinkResponse]) -> str:
    if not booking_links:
        return """
        <section class="empty-state">
          <p class="eyebrow">Empty state</p>
          <h2>Create the first booking link</h2>
          <p>Add a Calendly URL now so the next creator workflow stories can attach tracked content and later invoice defaults to a real creator-owned link.</p>
        </section>
        """

    items = "".join(_render_booking_link_card(booking_link) for booking_link in booking_links)
    return f'<div class="booking-link-list">{items}</div>'


def _render_booking_link_card(booking_link: BookingLinkResponse) -> str:
    return f"""
    <article class="booking-link-card">
      <div class="booking-link-header">
        <div>
          <p class="eyebrow">Booking link</p>
          <h2>{html.escape(booking_link.name)}</h2>
        </div>
        <p class="pill-note">{html.escape(_billing_defaults_copy(booking_link))}</p>
      </div>
      <p><strong>Calendly URL</strong>: <a href="{html.escape(booking_link.calendly_url)}" class="inline-link">{html.escape(booking_link.calendly_url)}</a></p>
      <p><strong>Stored defaults</strong>: {html.escape(_billing_defaults_copy(booking_link, long_form=True))}</p>
    </article>
    """


def _render_booking_link_field_error(message: str | None) -> str:
    if not message:
        return ""
    return f'<p class="field-error">{html.escape(message)}</p>'


def _render_booking_link_notice(
    *,
    status_value: str | None,
    field_errors: dict[str, str],
) -> str:
    if field_errors:
        return """
        <section class="notice error">
          <p class="eyebrow">Fix the highlighted fields</p>
          <p>Update the invalid values and submit the form again.</p>
        </section>
        """

    if status_value == "created":
        return """
        <section class="notice success">
          <p class="eyebrow">Booking link saved</p>
          <p>The creator-owned link is now available for later tracked-link and billing workflow steps.</p>
        </section>
        """

    return ""


def _billing_defaults_copy(
    booking_link: BookingLinkResponse,
    *,
    long_form: bool = False,
) -> str:
    amount = booking_link.billing_amount_cents
    currency = booking_link.billing_currency

    if amount is not None and currency is not None:
        prefix = "Ready for invoice defaults" if not long_form else "Amount and currency set"
        return f"{prefix}: {currency} {_format_billing_amount(amount)}"

    if amount is not None:
        prefix = "Incomplete defaults" if not long_form else "Amount set"
        return f"{prefix}: {_format_billing_amount(amount)} and currency still missing"

    if currency is not None:
        prefix = "Incomplete defaults" if not long_form else "Currency set"
        return f"{prefix}: {currency} and amount still missing"

    return "No billing defaults yet"


def _format_billing_amount(amount_cents: int) -> str:
    return f"{amount_cents / 100:,.2f}"


def _stripe_setup_home_state(raw_status: str) -> dict[str, str]:
    normalized_status = raw_status.strip().lower()
    if normalized_status == "connected":
        return {
            "label": "Connected",
            "heading": "Stripe is connected",
            "description": "This account is ready for the later billing phase to create invoices once the remaining setup steps ship.",
            "button_label": "",
            "badge_class": "connected",
            "item_class": "done",
            "checklist_label": "Done",
            "checklist_copy": "Your Stripe account is connected. The next setup work is booking links and tracked content.",
        }

    if normalized_status == "disconnected":
        return {
            "label": "Disconnected",
            "heading": "Stripe is disconnected",
            "description": "This creator account is not currently connected to Stripe. Reconnect it before later invoice automation can run.",
            "button_label": "Reconnect Stripe",
            "badge_class": "disconnected",
            "item_class": "todo",
            "checklist_label": "Needs action",
            "checklist_copy": "Reconnect Stripe before later invoice automation can create invoices for this creator.",
        }

    return {
        "label": "Pending",
        "heading": "Stripe setup is still pending",
        "description": "Stripe is required before the later billing phase can create invoices on your account. Start or resume onboarding from this page.",
        "button_label": "Start Stripe setup",
        "badge_class": "pending",
        "item_class": "todo",
        "checklist_label": "Needs action",
        "checklist_copy": "Finish Stripe onboarding so the later billing phase has an account it can invoice through.",
    }


def _format_connected_at(value) -> str:
    return html.escape(
        value.astimezone(timezone.utc).strftime("%B %d, %Y at %I:%M %p UTC")
    )


def _page_layout(*, title: str, body: str) -> str:
    escaped_title = html.escape(title)
    return f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{escaped_title}</title>
    <style>
      :root {{
        color-scheme: light;
        --page: #f5f1e8;
        --panel: rgba(255, 252, 245, 0.88);
        --panel-strong: #fff9ef;
        --ink: #1f1c1a;
        --muted: #655a4f;
        --accent: #a34a28;
        --accent-soft: #f3dfd4;
        --line: rgba(58, 38, 28, 0.12);
        --shadow: 0 24px 60px rgba(41, 29, 22, 0.12);
      }}

      * {{
        box-sizing: border-box;
      }}

      body {{
        margin: 0;
        min-height: 100vh;
        font-family: "Avenir Next", "Trebuchet MS", sans-serif;
        color: var(--ink);
        background:
          radial-gradient(circle at top left, rgba(163, 74, 40, 0.16), transparent 32%),
          linear-gradient(160deg, #f9f5ed 0%, #efe4d4 100%);
      }}

      main {{
        width: min(960px, calc(100% - 32px));
        margin: 0 auto;
        padding: 48px 0 64px;
      }}

      h1, h2 {{
        font-family: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", Georgia, serif;
        margin: 0;
        line-height: 1.1;
      }}

      h1 {{
        font-size: clamp(2.6rem, 4vw, 4.2rem);
        letter-spacing: -0.04em;
      }}

      h2 {{
        font-size: 1.4rem;
        margin-bottom: 12px;
      }}

      p {{
        margin: 0;
        color: var(--muted);
        line-height: 1.6;
      }}

      a {{
        color: var(--accent);
        text-decoration-thickness: 1.5px;
        text-underline-offset: 0.16em;
      }}

      strong {{
        color: var(--ink);
      }}

      .hero,
      .card {{
        border: 1px solid var(--line);
        border-radius: 24px;
        background: var(--panel);
        box-shadow: var(--shadow);
        backdrop-filter: blur(10px);
      }}

      .hero {{
        padding: 32px;
      }}

      .card {{
        padding: 24px;
      }}

      .grid {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
        gap: 16px;
        margin-bottom: 16px;
      }}

      .shell-header {{
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        gap: 16px;
        margin-bottom: 16px;
      }}

      .shell-nav {{
        display: flex;
        flex-wrap: wrap;
        gap: 12px;
        margin: 0 0 16px;
      }}

      .nav-link {{
        display: inline-flex;
        align-items: center;
        padding: 10px 14px;
        border-radius: 999px;
        border: 1px solid var(--line);
        background: rgba(255, 249, 239, 0.74);
        color: var(--ink);
        font-weight: 700;
        text-decoration: none;
      }}

      .nav-link.active {{
        background: var(--accent);
        border-color: var(--accent);
        color: #fff8f3;
      }}

      .status-row {{
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 16px;
        margin-bottom: 12px;
      }}

      .eyebrow {{
        text-transform: uppercase;
        letter-spacing: 0.14em;
        font-size: 0.78rem;
        font-weight: 700;
        color: var(--accent);
        margin-bottom: 12px;
      }}

      .lede {{
        max-width: 40rem;
        margin: 16px 0 24px;
        font-size: 1.08rem;
      }}

      .notice {{
        margin: 0 0 24px;
        padding: 16px 18px;
        border-radius: 18px;
        background: var(--accent-soft);
        border: 1px solid rgba(163, 74, 40, 0.16);
      }}

      .notice.success {{
        background: #dfeee7;
        border-color: rgba(31, 94, 88, 0.18);
      }}

      .notice.error {{
        background: #f7ddd6;
        border-color: rgba(151, 47, 23, 0.18);
      }}

      .status-pill {{
        display: inline-flex;
        align-items: center;
        padding: 8px 12px;
        border-radius: 999px;
        font-size: 0.82rem;
        font-weight: 700;
        letter-spacing: 0.04em;
      }}

      .status-pill.pending {{
        background: #f3dfd4;
        color: #8c3b1e;
      }}

      .status-pill.disconnected {{
        background: #f6d7d0;
        color: #972f17;
      }}

      .status-pill.connected {{
        background: #d9ede8;
        color: #1f5e58;
      }}

      .checklist {{
        list-style: none;
        padding: 0;
        margin: 20px 0 0;
        display: grid;
        gap: 12px;
      }}

      .checklist-item {{
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        gap: 16px;
        padding: 16px 18px;
        border-radius: 18px;
        border: 1px solid var(--line);
        background: var(--panel-strong);
      }}

      .checklist-item strong {{
        display: block;
        margin-bottom: 6px;
      }}

      .checklist-item.done {{
        background: #eef6f2;
      }}

      .checklist-item.todo {{
        background: #fff2ea;
      }}

      .checklist-item.next {{
        background: #faf4eb;
      }}

      .list-state {{
        white-space: nowrap;
        font-size: 0.84rem;
        font-weight: 700;
        color: var(--accent);
      }}

      .stack {{
        display: grid;
        gap: 16px;
      }}

      .section-heading {{
        display: flex;
        justify-content: space-between;
        align-items: flex-end;
        gap: 16px;
      }}

      form {{
        display: grid;
        gap: 12px;
      }}

      label {{
        font-weight: 700;
      }}

      input {{
        width: 100%;
        padding: 14px 16px;
        border-radius: 14px;
        border: 1px solid var(--line);
        background: var(--panel-strong);
        color: var(--ink);
        font: inherit;
      }}

      input[aria-invalid="true"] {{
        border-color: rgba(151, 47, 23, 0.42);
        background: #fff3ef;
      }}

      .form-help {{
        margin-top: -4px;
        font-size: 0.94rem;
      }}

      .field-error {{
        margin-top: -6px;
        color: #972f17;
        font-weight: 700;
      }}

      button {{
        width: fit-content;
        padding: 12px 18px;
        border: 0;
        border-radius: 999px;
        background: var(--accent);
        color: #fff8f3;
        font: inherit;
        font-weight: 700;
        cursor: pointer;
      }}

      button.secondary {{
        background: #2f5f5b;
      }}

      .accent {{
        background:
          linear-gradient(145deg, rgba(243, 223, 212, 0.94), rgba(255, 251, 244, 0.96));
      }}

      .footnote {{
        margin-top: 20px;
        font-size: 0.94rem;
      }}

      .inline-link {{
        font-weight: 700;
      }}

      .empty-state,
      .booking-link-card {{
        border-radius: 20px;
        border: 1px solid var(--line);
        background: var(--panel-strong);
      }}

      .empty-state {{
        padding: 24px;
        border-style: dashed;
      }}

      .booking-link-list {{
        display: grid;
        gap: 12px;
      }}

      .booking-link-card {{
        padding: 20px;
      }}

      .booking-link-header {{
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        gap: 16px;
        margin-bottom: 12px;
      }}

      .pill-note {{
        padding: 8px 12px;
        border-radius: 999px;
        background: rgba(163, 74, 40, 0.1);
        font-size: 0.88rem;
        font-weight: 700;
      }}

      @media (max-width: 720px) {{
        main {{
          width: min(100%, calc(100% - 24px));
          padding-top: 28px;
        }}

        .hero,
        .card {{
          border-radius: 20px;
        }}

        .shell-header {{
          flex-direction: column;
        }}

        .status-row,
        .checklist-item,
        .section-heading,
        .booking-link-header {{
          flex-direction: column;
          align-items: flex-start;
        }}
      }}
    </style>
  </head>
  <body>
    <main>
      {body}
    </main>
  </body>
</html>
"""
