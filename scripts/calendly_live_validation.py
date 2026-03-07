import argparse
import json
import os
import secrets
import sys
from dataclasses import dataclass
from typing import Any
from urllib import error, parse, request


CALENDLY_API_BASE_URL = "https://api.calendly.com"
DEFAULT_EVENTS = ("invitee.created", "invitee.canceled")
DEFAULT_SCOPE = "organization"
DEFAULT_WEBHOOK_PATH = "/webhooks/calendly"
DEFAULT_SIGNING_KEY_PREFIX = "whsec_"
DEFAULT_HTTP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/133.0.0.0 Safari/537.36"
)


class CalendlyApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class CalendlyIdentity:
    user_uri: str
    organization_uri: str


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "show-users-me":
        token = _required_env("CALENDLY_PERSONAL_ACCESS_TOKEN")
        identity = get_current_identity(token=token)
        print(f"user_uri={identity.user_uri}")
        print(f"organization_uri={identity.organization_uri}")
        return 0

    if args.command == "create-subscription":
        token = _required_env("CALENDLY_PERSONAL_ACCESS_TOKEN")
        public_base_url = args.public_base_url or _required_env("CALENDLY_WEBHOOK_PUBLIC_BASE_URL")
        scope = args.scope
        events = tuple(args.events.split(",")) if args.events else DEFAULT_EVENTS
        requested_signing_key = (
            args.signing_key
            or os.getenv("CALENDLY_WEBHOOK_SIGNING_KEY")
            or _generate_signing_key()
        )

        identity = get_current_identity(token=token)
        organization_uri = args.organization_uri or os.getenv(
            "CALENDLY_WEBHOOK_ORGANIZATION_URI",
            identity.organization_uri,
        )
        user_uri = args.user_uri or os.getenv("CALENDLY_WEBHOOK_USER_URI", identity.user_uri)
        webhook_url = build_webhook_url(
            public_base_url=public_base_url,
            webhook_path=args.webhook_path,
        )
        subscription = create_webhook_subscription(
            token=token,
            webhook_url=webhook_url,
            events=events,
            scope=scope,
            organization_uri=organization_uri,
            user_uri=user_uri,
            signing_key=requested_signing_key,
        )

        print(f"webhook_url={webhook_url}")
        print(f"scope={scope}")
        print(f"events={','.join(events)}")
        print(f"organization_uri={organization_uri}")
        if scope == "user":
            print(f"user_uri={user_uri}")
        subscription_uri = subscription.get("uri")
        if isinstance(subscription_uri, str) and subscription_uri:
            print(f"subscription_uri={subscription_uri}")
        signing_key = subscription.get("signing_key")
        if isinstance(signing_key, str) and signing_key:
            print(f"CALENDLY_WEBHOOK_SIGNING_KEY={signing_key}")
            print("next_step=export CALENDLY_WEBHOOK_SIGNING_KEY into the app shell before receiving live deliveries")
        else:
            print(f"CALENDLY_WEBHOOK_SIGNING_KEY={requested_signing_key}")
            print("warning=response did not include signing_key; using the explicit key supplied in the create request")
            print("next_step=export CALENDLY_WEBHOOK_SIGNING_KEY into the app shell before receiving live deliveries")
        return 0

    if args.command == "delete-subscription":
        token = _required_env("CALENDLY_PERSONAL_ACCESS_TOKEN")
        subscription_uri = args.subscription_uri or _required_env("CALENDLY_WEBHOOK_SUBSCRIPTION_URI")
        delete_webhook_subscription(
            token=token,
            subscription_uri=subscription_uri,
        )
        print(f"deleted_subscription_uri={subscription_uri}")
        return 0

    parser.error(f"unsupported command: {args.command}")
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create and clean up real Calendly webhook subscriptions for provider-backed validation."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "show-users-me",
        help="Fetch the current Calendly user and organization URIs for the configured personal access token.",
    )

    create_parser = subparsers.add_parser(
        "create-subscription",
        help="Create a webhook subscription pointing at the app's public Calendly webhook URL.",
    )
    create_parser.add_argument(
        "--public-base-url",
        help="Public base URL for the running app, for example https://abc123.ngrok.app",
    )
    create_parser.add_argument(
        "--webhook-path",
        default=DEFAULT_WEBHOOK_PATH,
        help=f"Webhook path to append to the public base URL. Default: {DEFAULT_WEBHOOK_PATH}",
    )
    create_parser.add_argument(
        "--scope",
        choices=("organization", "user"),
        default=os.getenv("CALENDLY_WEBHOOK_SCOPE", DEFAULT_SCOPE),
        help=f"Webhook scope. Default: {DEFAULT_SCOPE}",
    )
    create_parser.add_argument(
        "--events",
        default=",".join(DEFAULT_EVENTS),
        help=f"Comma-separated Calendly events. Default: {','.join(DEFAULT_EVENTS)}",
    )
    create_parser.add_argument(
        "--organization-uri",
        help="Calendly organization URI. If omitted, the script derives it from /users/me.",
    )
    create_parser.add_argument(
        "--user-uri",
        help="Calendly user URI. If omitted, the script derives it from /users/me.",
    )
    create_parser.add_argument(
        "--signing-key",
        help="Webhook signing key to send in the create request. Defaults to CALENDLY_WEBHOOK_SIGNING_KEY or a generated whsec_ value.",
    )

    delete_parser = subparsers.add_parser(
        "delete-subscription",
        help="Delete a webhook subscription created for live validation.",
    )
    delete_parser.add_argument(
        "--subscription-uri",
        help="Calendly webhook subscription URI. If omitted, uses CALENDLY_WEBHOOK_SUBSCRIPTION_URI.",
    )

    return parser


def build_webhook_url(*, public_base_url: str, webhook_path: str) -> str:
    normalized_base_url = public_base_url.rstrip("/")
    normalized_path = webhook_path if webhook_path.startswith("/") else f"/{webhook_path}"
    return f"{normalized_base_url}{normalized_path}"


def get_current_identity(*, token: str) -> CalendlyIdentity:
    payload = _api_request_json(
        method="GET",
        path="/users/me",
        token=token,
    )
    resource = _unwrap_resource(payload)
    user_uri = resource.get("uri")
    organization_uri = resource.get("current_organization")

    if not isinstance(user_uri, str) or not user_uri:
        raise CalendlyApiError("Calendly /users/me response did not include a user uri")
    if not isinstance(organization_uri, str) or not organization_uri:
        raise CalendlyApiError("Calendly /users/me response did not include an organization uri")

    return CalendlyIdentity(
        user_uri=user_uri,
        organization_uri=organization_uri,
    )


def create_webhook_subscription(
    *,
    token: str,
    webhook_url: str,
    events: tuple[str, ...],
    scope: str,
    organization_uri: str,
    user_uri: str,
    signing_key: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "url": webhook_url,
        "events": list(events),
        "organization": organization_uri,
        "scope": scope,
        "signing_key": signing_key,
    }
    if scope == "user":
        payload["user"] = user_uri

    response = _api_request_json(
        method="POST",
        path="/webhook_subscriptions",
        token=token,
        payload=payload,
    )
    resource = _unwrap_resource(response)
    if not isinstance(resource, dict):
        raise CalendlyApiError("Calendly create webhook subscription response was missing a resource object")
    return resource


def delete_webhook_subscription(*, token: str, subscription_uri: str) -> None:
    parsed_uri = parse.urlsplit(subscription_uri)
    if parsed_uri.scheme and parsed_uri.netloc:
        if parsed_uri.netloc != "api.calendly.com":
            raise CalendlyApiError(
                f"Unsupported subscription host {parsed_uri.netloc!r}; expected api.calendly.com"
            )
        path = parsed_uri.path
    else:
        path = subscription_uri

    _api_request_json(
        method="DELETE",
        path=path,
        token=token,
        payload=None,
        allow_empty_response=True,
    )


def _generate_signing_key() -> str:
    return f"{DEFAULT_SIGNING_KEY_PREFIX}{secrets.token_urlsafe(32)}"


def _api_request_json(
    *,
    method: str,
    path: str,
    token: str,
    payload: dict[str, Any] | None = None,
    allow_empty_response: bool = False,
) -> dict[str, Any]:
    url = path if path.startswith("https://") else f"{CALENDLY_API_BASE_URL}{path}"
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/plain, */*",
        "User-Agent": os.getenv("CALENDLY_API_USER_AGENT", DEFAULT_HTTP_USER_AGENT),
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = request.Request(
        url,
        data=body,
        method=method,
        headers=headers,
    )

    try:
        with request.urlopen(req) as response:
            raw_body = response.read()
    except error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise CalendlyApiError(
            f"Calendly API {method} {url} failed with {exc.code}: {error_body}"
        ) from exc
    except error.URLError as exc:
        raise CalendlyApiError(f"Calendly API {method} {url} failed: {exc.reason}") from exc

    if not raw_body:
        if allow_empty_response:
            return {}
        raise CalendlyApiError(f"Calendly API {method} {url} returned an empty response body")

    try:
        parsed_payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise CalendlyApiError(
            f"Calendly API {method} {url} returned invalid JSON: {raw_body.decode('utf-8', errors='replace')}"
        ) from exc

    if not isinstance(parsed_payload, dict):
        raise CalendlyApiError(
            f"Calendly API {method} {url} returned unexpected JSON shape: {type(parsed_payload).__name__}"
        )
    return parsed_payload


def _unwrap_resource(payload: dict[str, Any]) -> dict[str, Any]:
    resource = payload.get("resource")
    if isinstance(resource, dict):
        return resource
    return payload


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if value:
        return value

    raise CalendlyApiError(
        f"Missing required environment variable {name}. "
        "Set it in the shell for live Calendly validation and rerun the script."
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CalendlyApiError as exc:
        print(f"error={exc}", file=sys.stderr)
        raise SystemExit(2)
