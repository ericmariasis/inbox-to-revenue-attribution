import html
from datetime import timezone
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.api.deps import get_optional_browser_auth_user
from app.api.stripe import build_stripe_connect_start_response
from app.core.config import get_settings
from app.db.session import get_db
from app.models.auth_user import AuthUser
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
              <p>Register the Calendly link and billing defaults that later invoice automation will trust.</p>
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
        .checklist-item {{
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
