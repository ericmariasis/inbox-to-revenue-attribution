import os
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.config import get_settings
from app.core.startup_smoke import (
    StartupSmokeError,
    StartupSmokeResult,
    _resolve_repo_root,
    run_startup_smoke,
)


SAFE_NON_LOCAL_ENV = {
    "APP_ENV": "production",
    "DATABASE_URL": "",
    "JWT_SECRET": "story73-production-jwt-secret-0123456789abcdef",
    "STRIPE_CONNECT_CLIENT_ID": "ca_story73_beta_live",
    "STRIPE_SECRET_KEY": "sk_test_story73_beta_live",
    "STRIPE_CONNECT_AUTHORIZE_URL": "https://connect.stripe.com/oauth/authorize",
    "STRIPE_CONNECT_REDIRECT_URI": "https://creatorbeta.co/stripe/connect/callback",
    "STRIPE_WEBHOOK_SECRET": "whsec_story73_beta_live",
    "CALENDLY_WEBHOOK_SIGNING_KEY": "cal_story73_beta_live",
    "TRACKED_LINK_BASE_URL": "https://creatorbeta.co",
    "MAGIC_LINK_EMAIL_PROVIDER": "smtp",
    "MAGIC_LINK_BASE_URL": "https://creatorbeta.co",
    "MAGIC_LINK_EMAIL_FROM_EMAIL": "auth@creatorbeta.co",
    "MAGIC_LINK_EMAIL_SMTP_HOST": "smtp.creatorbeta.co",
    "MAGIC_LINK_EMAIL_SMTP_USERNAME": "smtp-user",
    "MAGIC_LINK_EMAIL_SMTP_PASSWORD": "smtp-password",
    "OPERATOR_EMAIL_ALLOWLIST": "ops1@creatortrust.co,ops2@creatortrust.co",
}


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _set_safe_non_local_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    env_values = dict(SAFE_NON_LOCAL_ENV)
    env_values["DATABASE_URL"] = os.getenv("TEST_DATABASE_URL") or get_settings().database_url
    env_values.update(overrides)
    for name, value in env_values.items():
        monkeypatch.setenv(name, value)


def test_run_startup_smoke_validates_runtime_and_connects_to_database(
    monkeypatch: pytest.MonkeyPatch,
):
    _set_safe_non_local_env(monkeypatch)

    result = run_startup_smoke(require_schema=True)

    assert result.schema_ready is True
    assert result.current_revision is not None
    assert result.current_revision == result.head_revision


def test_run_startup_smoke_wraps_database_connection_failures(
    monkeypatch: pytest.MonkeyPatch,
):
    _set_safe_non_local_env(monkeypatch)

    class _BrokenEngine:
        def connect(self):
            raise RuntimeError("boom")

        def dispose(self):
            return None

    with patch("app.core.startup_smoke.create_engine", return_value=_BrokenEngine()):
        with pytest.raises(StartupSmokeError, match="startup smoke failed"):
            run_startup_smoke()


def test_run_startup_smoke_reports_schema_not_ready_when_head_is_missing(
    monkeypatch: pytest.MonkeyPatch,
):
    _set_safe_non_local_env(monkeypatch)

    class _StubConnection:
        def execute(self, _statement):
            return None

    class _StubEngine:
        def connect(self):
            class _ContextManager:
                def __enter__(self):
                    return _StubConnection()

                def __exit__(self, exc_type, exc, tb):
                    return False

            return _ContextManager()

        def dispose(self):
            return None

    not_ready_result = StartupSmokeResult(
        current_revision=None,
        head_revision="365dd98_schema_head",
        schema_ready=False,
    )

    with patch("app.core.startup_smoke.create_engine", return_value=_StubEngine()):
        with patch("app.core.startup_smoke._load_schema_state", return_value=not_ready_result):
            result = run_startup_smoke()

    assert result == not_ready_result


def test_run_startup_smoke_can_require_schema_to_be_ready(
    monkeypatch: pytest.MonkeyPatch,
):
    _set_safe_non_local_env(monkeypatch)

    class _StubConnection:
        def execute(self, _statement):
            return None

    class _StubEngine:
        def connect(self):
            class _ContextManager:
                def __enter__(self):
                    return _StubConnection()

                def __exit__(self, exc_type, exc, tb):
                    return False

            return _ContextManager()

        def dispose(self):
            return None

    not_ready_result = StartupSmokeResult(
        current_revision=None,
        head_revision="365dd98_schema_head",
        schema_ready=False,
    )

    with patch("app.core.startup_smoke.create_engine", return_value=_StubEngine()):
        with patch("app.core.startup_smoke._load_schema_state", return_value=not_ready_result):
            with pytest.raises(StartupSmokeError, match="schema is not at the current migration head"):
                run_startup_smoke(require_schema=True)


def test_resolve_repo_root_prefers_current_working_directory_when_imported_from_site_packages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "alembic.ini").write_text("[alembic]\nscript_location = migrations\n", encoding="utf-8")
    (repo_root / "migrations").mkdir()

    fake_module_path = (
        tmp_path
        / "venv"
        / "lib"
        / "python3.12"
        / "site-packages"
        / "app"
        / "core"
        / "startup_smoke.py"
    )
    fake_module_path.parent.mkdir(parents=True)
    fake_module_path.write_text("# stub", encoding="utf-8")

    monkeypatch.chdir(repo_root)

    with patch("app.core.startup_smoke.__file__", str(fake_module_path)):
        assert _resolve_repo_root() == repo_root.resolve()


def test_resolve_repo_root_raises_when_alembic_files_are_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    missing_root = tmp_path / "missing"
    missing_root.mkdir()
    fake_module_path = missing_root / "app" / "core" / "startup_smoke.py"
    fake_module_path.parent.mkdir(parents=True)
    fake_module_path.write_text("# stub", encoding="utf-8")

    monkeypatch.chdir(missing_root)

    with patch("app.core.startup_smoke.__file__", str(fake_module_path)):
        with pytest.raises(
            StartupSmokeError,
            match="could not locate alembic.ini and migrations",
        ):
            _resolve_repo_root()
