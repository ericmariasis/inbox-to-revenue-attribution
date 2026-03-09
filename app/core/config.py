from functools import lru_cache
from ipaddress import ip_address
from urllib.parse import urlsplit

from pydantic import EmailStr
from pydantic_settings import BaseSettings, SettingsConfigDict

LOCAL_APP_ENVS = frozenset({"local", "test", "manual_test"})
DEFAULT_DATABASE_URL = "postgresql://localhost/attribution"
DEFAULT_JWT_SECRET = "replace_me"
DEFAULT_STRIPE_CONNECT_CLIENT_ID = "ca_test_example"
DEFAULT_STRIPE_SECRET_KEY = "sk_test_example"
DEFAULT_STRIPE_CONNECT_AUTHORIZE_URL = "https://connect.stripe.com/oauth/authorize"
DEFAULT_STRIPE_CONNECT_REDIRECT_URI = "http://localhost:8000/stripe/connect/callback"
DEFAULT_STRIPE_WEBHOOK_SECRET = "whsec_test_example"
DEFAULT_CALENDLY_WEBHOOK_SIGNING_KEY = "whsec_calendly_test_example"
DEFAULT_TRACKED_LINK_BASE_URL = "https://trk.example.com"
DEFAULT_MAGIC_LINK_EMAIL_PROVIDER = "stub"
DEFAULT_MAGIC_LINK_BASE_URL = "http://localhost:8000"
DEFAULT_MAGIC_LINK_EMAIL_FROM_EMAIL = "no-reply@example.com"
DEFAULT_MAGIC_LINK_EMAIL_FROM_NAME = "Creator Compass"
SUPPORTED_MAGIC_LINK_EMAIL_PROVIDERS = frozenset({"stub", "smtp"})


class SettingsValidationError(ValueError):
    pass


def is_local_app_env(app_env: str) -> bool:
    return app_env.strip().lower() in LOCAL_APP_ENVS


def _cleaned_value(value: str) -> str:
    return value.strip()


def _normalized_host(value: str) -> str | None:
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return None
    return parsed.hostname


def _host_is_local(host: str | None) -> bool:
    if host is None:
        return True

    normalized = host.strip().lower()
    if normalized in {"localhost"} or normalized.endswith(".localhost"):
        return True

    try:
        parsed_ip = ip_address(normalized)
    except ValueError:
        return False

    return parsed_ip.is_loopback or parsed_ip.is_unspecified


def _host_is_example(host: str | None) -> bool:
    if host is None:
        return False

    normalized = host.strip().lower()
    return normalized in {
        "example.com",
        "example.org",
        "example.net",
    } or normalized.endswith(
        (
            ".example.com",
            ".example.org",
            ".example.net",
        )
    )


def _email_domain_is_example(value: str) -> bool:
    _, _, domain = value.strip().rpartition("@")
    if not domain:
        return False
    return _host_is_example(domain)


def _require_non_placeholder(
    errors: list[str],
    *,
    field_name: str,
    value: str,
    placeholders: set[str],
    min_length: int | None = None,
) -> None:
    cleaned_value = _cleaned_value(value)
    lowered_value = cleaned_value.lower()
    if not cleaned_value or lowered_value in placeholders:
        errors.append(f"{field_name} must be set to a non-placeholder value")
        return

    if min_length is not None and len(cleaned_value) < min_length:
        errors.append(f"{field_name} must be at least {min_length} characters in non-local environments")


def _require_https_url(
    errors: list[str],
    *,
    field_name: str,
    value: str,
    forbid_local_host: bool = False,
    forbid_example_host: bool = False,
) -> None:
    cleaned_value = _cleaned_value(value)
    parsed = urlsplit(cleaned_value)
    if parsed.scheme != "https" or not parsed.netloc:
        errors.append(f"{field_name} must be an absolute https URL in non-local environments")
        return

    host = parsed.hostname
    if forbid_local_host and _host_is_local(host):
        errors.append(f"{field_name} must not point to localhost or loopback hosts in non-local environments")
    if forbid_example_host and _host_is_example(host):
        errors.append(f"{field_name} must not use example placeholder hosts in non-local environments")


def _require_absolute_http_url(
    errors: list[str],
    *,
    field_name: str,
    value: str,
    allow_http: bool,
    forbid_local_host: bool = False,
    forbid_example_host: bool = False,
) -> None:
    cleaned_value = _cleaned_value(value)
    parsed = urlsplit(cleaned_value)
    allowed_schemes = {"http", "https"} if allow_http else {"https"}
    expected_scheme = "an absolute http or https URL" if allow_http else "an absolute https URL"
    if parsed.scheme not in allowed_schemes or not parsed.netloc:
        errors.append(f"{field_name} must be {expected_scheme}")
        return

    host = parsed.hostname
    if forbid_local_host and _host_is_local(host):
        errors.append(f"{field_name} must not point to localhost or loopback hosts in non-local environments")
    if forbid_example_host and _host_is_example(host):
        errors.append(f"{field_name} must not use example placeholder hosts in non-local environments")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: str = "local"
    database_url: str = DEFAULT_DATABASE_URL
    jwt_secret: str = DEFAULT_JWT_SECRET
    jwt_algorithm: str = "HS256"
    jwt_access_token_ttl_hours: int = 24
    magic_link_token_ttl_minutes: int = 15
    stripe_connect_state_ttl_minutes: int = 15
    stripe_connect_client_id: str = DEFAULT_STRIPE_CONNECT_CLIENT_ID
    stripe_secret_key: str = DEFAULT_STRIPE_SECRET_KEY
    stripe_connect_authorize_url: str = DEFAULT_STRIPE_CONNECT_AUTHORIZE_URL
    stripe_connect_redirect_uri: str = DEFAULT_STRIPE_CONNECT_REDIRECT_URI
    stripe_webhook_secret: str = DEFAULT_STRIPE_WEBHOOK_SECRET
    stripe_webhook_tolerance_seconds: int = 300
    calendly_webhook_signing_key: str = DEFAULT_CALENDLY_WEBHOOK_SIGNING_KEY
    calendly_webhook_tolerance_seconds: int = 300
    tracked_link_base_url: str = DEFAULT_TRACKED_LINK_BASE_URL
    magic_link_email_provider: str = DEFAULT_MAGIC_LINK_EMAIL_PROVIDER
    magic_link_base_url: str = DEFAULT_MAGIC_LINK_BASE_URL
    magic_link_email_from_email: EmailStr = DEFAULT_MAGIC_LINK_EMAIL_FROM_EMAIL
    magic_link_email_from_name: str = DEFAULT_MAGIC_LINK_EMAIL_FROM_NAME
    magic_link_email_smtp_host: str = ""
    magic_link_email_smtp_port: int = 587
    magic_link_email_smtp_username: str = ""
    magic_link_email_smtp_password: str = ""
    magic_link_email_smtp_starttls: bool = True
    magic_link_email_smtp_use_ssl: bool = False
    magic_link_email_smtp_timeout_seconds: int = 10

    def is_local_env(self) -> bool:
        return is_local_app_env(self.app_env)

    def validate_runtime(self) -> None:
        errors: list[str] = []
        is_local_env = self.is_local_env()
        normalized_email_provider = _cleaned_value(self.magic_link_email_provider).lower()
        if normalized_email_provider not in SUPPORTED_MAGIC_LINK_EMAIL_PROVIDERS:
            errors.append(
                "magic_link_email_provider must be one of: "
                + ", ".join(sorted(SUPPORTED_MAGIC_LINK_EMAIL_PROVIDERS))
            )
        elif normalized_email_provider == "smtp":
            _require_non_placeholder(
                errors,
                field_name="magic_link_email_smtp_host",
                value=self.magic_link_email_smtp_host,
                placeholders=set(),
            )
            _require_absolute_http_url(
                errors,
                field_name="magic_link_base_url",
                value=self.magic_link_base_url,
                allow_http=is_local_env,
                forbid_local_host=not is_local_env,
                forbid_example_host=not is_local_env,
            )
            if self.magic_link_email_smtp_starttls and self.magic_link_email_smtp_use_ssl:
                errors.append(
                    "magic_link_email_smtp_starttls and magic_link_email_smtp_use_ssl cannot both be enabled"
                )
            smtp_username = _cleaned_value(self.magic_link_email_smtp_username)
            smtp_password = _cleaned_value(self.magic_link_email_smtp_password)
            if bool(smtp_username) != bool(smtp_password):
                errors.append(
                    "magic_link_email_smtp_username and magic_link_email_smtp_password must be set together"
                )
            if not is_local_env and _email_domain_is_example(str(self.magic_link_email_from_email)):
                errors.append(
                    "magic_link_email_from_email must not use example placeholder domains in non-local environments"
                )

        if is_local_env:
            if errors:
                joined_errors = "\n- ".join(errors)
                raise SettingsValidationError(
                    f"Runtime blocked by unsafe settings for app_env={self.app_env!r}:\n- {joined_errors}"
                )
            return

        _require_non_placeholder(
            errors,
            field_name="jwt_secret",
            value=self.jwt_secret,
            placeholders={DEFAULT_JWT_SECRET},
            min_length=32,
        )
        _require_non_placeholder(
            errors,
            field_name="stripe_webhook_secret",
            value=self.stripe_webhook_secret,
            placeholders={DEFAULT_STRIPE_WEBHOOK_SECRET},
        )
        _require_non_placeholder(
            errors,
            field_name="calendly_webhook_signing_key",
            value=self.calendly_webhook_signing_key,
            placeholders={DEFAULT_CALENDLY_WEBHOOK_SIGNING_KEY},
        )
        _require_non_placeholder(
            errors,
            field_name="stripe_connect_client_id",
            value=self.stripe_connect_client_id,
            placeholders={DEFAULT_STRIPE_CONNECT_CLIENT_ID},
        )
        _require_non_placeholder(
            errors,
            field_name="stripe_secret_key",
            value=self.stripe_secret_key,
            placeholders={DEFAULT_STRIPE_SECRET_KEY},
        )
        _require_https_url(
            errors,
            field_name="stripe_connect_authorize_url",
            value=self.stripe_connect_authorize_url,
        )
        _require_https_url(
            errors,
            field_name="stripe_connect_redirect_uri",
            value=self.stripe_connect_redirect_uri,
            forbid_local_host=True,
            forbid_example_host=True,
        )
        _require_https_url(
            errors,
            field_name="tracked_link_base_url",
            value=self.tracked_link_base_url,
            forbid_local_host=True,
            forbid_example_host=True,
        )
        if normalized_email_provider == "stub":
            errors.append("magic_link_email_provider must be configured to a live provider in non-local environments")

        if errors:
            joined_errors = "\n- ".join(errors)
            raise SettingsValidationError(
                f"Non-local startup blocked by unsafe settings for app_env={self.app_env!r}:\n- {joined_errors}"
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()
