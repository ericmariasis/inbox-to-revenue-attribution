# Calendly Live Validation

Use this runbook after focused local tests and before closing any story that depends on the real Calendly provider contract.

## Why this exists

Focused local tests prove the app's internal contract, but they do not prove:

- the real Calendly webhook subscription API contract
- the real webhook header format and signing behavior
- the real payload field locations for event identifiers, invitee identifiers, and tracking values

This runbook makes the provider-backed step repeatable before Story 33 and Story 35 build on it.

## Current prerequisites

You need all of the following:

- a Calendly personal access token
- a public HTTPS URL that can reach the running app, such as an `ngrok` or `cloudflared` tunnel
- the app running and reachable at `<public_base_url>/webhooks/calendly`
- a shell with:
  - `CALENDLY_PERSONAL_ACCESS_TOKEN`
  - `CALENDLY_WEBHOOK_PUBLIC_BASE_URL`

Optional shell variables:

- `CALENDLY_WEBHOOK_SCOPE`
- `CALENDLY_WEBHOOK_ORGANIZATION_URI`
- `CALENDLY_WEBHOOK_USER_URI`
- `CALENDLY_WEBHOOK_SUBSCRIPTION_URI`

The app itself should also have:

- `CALENDLY_WEBHOOK_SIGNING_KEY`
- `CALENDLY_WEBHOOK_TOLERANCE_SECONDS`

## Recommended sequence

1. Run the focused local tests first.
2. Expose the running app at a public HTTPS base URL.
3. Create a real Calendly webhook subscription.
4. Export the signing key into the app shell and restart the app if needed.
5. Create or fetch one real tracked link that points at the target Calendly URL.
6. Trigger one real Calendly booking flow through that tracked link so the redirect carries `utm_content=<tid>`.
7. Confirm the app receives `POST /webhooks/calendly` successfully.
8. Record the observed provider contract in `north-star/story32-manual.md` and `north-star/ACTIVE_CONTEXT.md`.
9. Delete the temporary webhook subscription.

## Create the webhook subscription

Show the current Calendly user and organization URIs:

```powershell
.venv\Scripts\python.exe scripts\calendly_live_validation.py show-users-me
```

Create a webhook subscription using the current `organization` scope by default:

```powershell
.venv\Scripts\python.exe scripts\calendly_live_validation.py create-subscription
```

The helper now sends an explicit signing key in the create request. It will use:

- `--signing-key` if provided
- otherwise `CALENDLY_WEBHOOK_SIGNING_KEY` from the shell
- otherwise a generated `whsec_...` value printed in the command output

Example output:

```text
webhook_url=https://abc123.ngrok.app/webhooks/calendly
scope=organization
events=invitee.created,invitee.canceled
organization_uri=https://api.calendly.com/organizations/...
subscription_uri=https://api.calendly.com/webhook_subscriptions/...
CALENDLY_WEBHOOK_SIGNING_KEY=whsec_...
```

Export the printed signing key into the app shell:

```powershell
$env:CALENDLY_WEBHOOK_SIGNING_KEY = "whsec_..."
```

## Create a tracked link for the live booking

If you already have a suitable content `tracked_url`, use that.

If you need a fresh one for local validation, create it directly in the current DB:

```powershell
.venv\Scripts\python.exe scripts\create_live_validation_content.py --calendly-url "https://calendly.com/your-account/your-event"
```

The helper prints the tracked host in this order:

- `--tracked-base-url`
- `TRACKED_LINK_BASE_URL`
- `CALENDLY_WEBHOOK_PUBLIC_BASE_URL`
- app settings default

Example output:

```text
tid=84000c023e9343daae2dd184acef505a
tracked_url=https://trk.example.com/r/84000c023e9343daae2dd184acef505a
calendly_url=https://calendly.com/your-account/your-event
```

## What to observe during the live booking

Minimum observations:

- the incoming header name and overall signature format
- the provider event name, such as `invitee.created`
- the field that holds the scheduled event identifier
- the field that holds the invitee or booking identifier
- the field path where Calendly returns `utm_content`

The default webhook router now logs the verified normalized event plus the observed source paths, for example:

```text
calendly_webhook_event_verified provider_event_type=invitee.created calendly_event_id=... calendly_event_id_path=payload.event calendly_booking_uuid=... calendly_booking_uuid_path=payload.uri event_type=booking.created tid=... tid_path=payload.tracking.utm_content
```

For the current implementation, the expected internal outcome is:

- the request is accepted with `200`
- the normalized event type becomes `booking.created` or `booking.canceled`
- the normalized event includes:
  - `calendly_event_id`
  - `calendly_booking_uuid`
  - `tid` derived from `tracking.utm_content` when present

Before Story 33, there should still be no `Booking` row mutation from this live validation step alone.

## Delete the webhook subscription

```powershell
.venv\Scripts\python.exe scripts\calendly_live_validation.py delete-subscription --subscription-uri "https://api.calendly.com/webhook_subscriptions/..."
```

## Notes

- Calendly's official docs say webhook subscriptions are created with a personal access token against `/webhook_subscriptions`, scoped to either `organization` or `user`, and that UTM parameters are included in scheduled-event reporting.
- Some Calendly API responses do not echo a `signing_key` even when the create request succeeds. This helper avoids that ambiguity by sending an explicit signing key in the create request and printing the value that the app should trust.
- This repo now carries canonical `tid` values through `utm_content`, not plain `tid` query params.
- If the live payload differs from the current assumptions, stop before Story 33 and update the parser/tests/docs first.
