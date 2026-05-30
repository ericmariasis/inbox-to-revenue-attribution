from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]


def _blueprint() -> dict:
    return yaml.safe_load((REPO_ROOT / "render.yaml").read_text(encoding="utf-8"))


def _service_env_vars() -> dict[str, dict]:
    service = _blueprint()["services"][0]
    return {entry["key"]: entry for entry in service["envVars"]}


def test_render_blueprint_defines_single_web_service_and_database():
    blueprint = _blueprint()

    assert len(blueprint["services"]) == 1
    assert len(blueprint["databases"]) == 1

    service = blueprint["services"][0]
    database = blueprint["databases"][0]

    assert service["name"] == "inbox-to-revenue-attribution-web"
    assert service["type"] == "web"
    assert service["runtime"] == "python"
    assert service["plan"] == "starter"
    assert service["region"] == "virginia"
    assert service["numInstances"] == 1
    assert service["healthCheckPath"] == "/health"

    assert database["name"] == "inbox-to-revenue-attribution-db"
    assert database["plan"] == "basic-256mb"
    assert database["region"] == "virginia"
    assert database["postgresMajorVersion"] == "17"
    assert database["databaseName"] == "attribution"
    assert database["user"] == "attribution_app"
    assert database["ipAllowList"] == []


def test_render_blueprint_pins_python_and_explicit_runtime_commands():
    python_version = (REPO_ROOT / ".python-version").read_text(encoding="utf-8").strip()
    service = _blueprint()["services"][0]

    assert python_version == "3.12"
    assert service["buildCommand"] == "pip install ."
    assert service["startCommand"] == "uvicorn app.main:app --host 0.0.0.0 --port $PORT"


def test_render_blueprint_covers_required_non_local_config():
    env_vars = _service_env_vars()

    assert env_vars["APP_ENV"]["value"] == "production"
    assert env_vars["JWT_SECRET"]["generateValue"] is True
    assert env_vars["JWT_ALGORITHM"]["value"] == "HS256"
    assert env_vars["PAYPAL_ENVIRONMENT"]["value"] == "sandbox"
    assert env_vars["PAYPAL_CREATOR_ACCESS"]["value"] == "public"
    assert env_vars["PAYPAL_PARTNER_ATTRIBUTION_ID"]["value"] == "CAREERCODE_SP_PPCP"
    assert env_vars["GROWTH_LOOP_AGENT_FEATURE_ENABLED"]["value"] == "false"
    assert env_vars["GROWTH_LOOP_LOOMI_MCP_ENABLED"]["value"] == "false"
    assert env_vars["GROWTH_LOOP_BLOOMREACH_SEGMENT_PROOF_ENABLED"]["value"] == "false"
    assert env_vars["GROWTH_LOOP_BLOOMREACH_SEGMENT_PROOF_PROJECT_NAME"]["value"] == "sleepy-goose"
    assert (
        env_vars["GROWTH_LOOP_BLOOMREACH_SEGMENT_PROOF_WORKSPACE_NAME"]["value"]
        == "Hackathon Workspace"
    )
    assert (
        env_vars["GROWTH_LOOP_BLOOMREACH_SEGMENT_PROOF_CREATED_VIA"]["value"]
        == "Bloomreach Engagement UI"
    )
    assert (
        env_vars["GROWTH_LOOP_BLOOMREACH_SEGMENT_PROOF_STATUS_LABEL"]["value"]
        == "Created in Engagement UI"
    )
    assert env_vars["DATABASE_URL"]["fromDatabase"] == {
        "name": "inbox-to-revenue-attribution-db",
        "property": "connectionString",
    }
    assert env_vars["STRIPE_CONNECT_AUTHORIZE_URL"]["value"] == "https://connect.stripe.com/oauth/authorize"
    assert env_vars["MAGIC_LINK_EMAIL_PROVIDER"]["value"] == "smtp"
    assert env_vars["MAGIC_LINK_EMAIL_FROM_NAME"]["value"] == "Creator Compass"
    assert env_vars["MAGIC_LINK_EMAIL_SMTP_PORT"]["value"] == "587"
    assert env_vars["MAGIC_LINK_EMAIL_SMTP_STARTTLS"]["value"] == "true"
    assert env_vars["MAGIC_LINK_EMAIL_SMTP_USE_SSL"]["value"] == "false"

    sync_false_keys = {
        "STRIPE_CONNECT_CLIENT_ID",
        "STRIPE_SECRET_KEY",
        "STRIPE_CONNECT_REDIRECT_URI",
        "STRIPE_WEBHOOK_SECRET",
        "PAYPAL_SANDBOX_CLIENT_ID",
        "PAYPAL_SANDBOX_CLIENT_SECRET",
        "PAYPAL_SANDBOX_PARTNER_ID",
        "PAYPAL_CONNECT_REDIRECT_URI",
        "PAYPAL_SANDBOX_WEBHOOK_ID",
        "GROWTH_LOOP_LOOMI_MCP_ENDPOINT",
        "GROWTH_LOOP_LOOMI_MCP_ACCESS_TOKEN",
        "GROWTH_LOOP_LOOMI_MCP_PROJECT_ID",
        "GROWTH_LOOP_LOOMI_MCP_WORKSPACE_ID",
        "GROWTH_LOOP_LOOMI_MCP_ORGANIZATION_ID",
        "GROWTH_LOOP_BLOOMREACH_SEGMENT_PROOF_NAME",
        "GROWTH_LOOP_BLOOMREACH_SEGMENT_PROOF_ID",
        "CALENDLY_WEBHOOK_SIGNING_KEY",
        "TRACKED_LINK_BASE_URL",
        "MAGIC_LINK_BASE_URL",
        "MAGIC_LINK_EMAIL_FROM_EMAIL",
        "MAGIC_LINK_EMAIL_SMTP_HOST",
        "MAGIC_LINK_EMAIL_SMTP_USERNAME",
        "MAGIC_LINK_EMAIL_SMTP_PASSWORD",
    }
    assert {key for key, value in env_vars.items() if value.get("sync") is False} == sync_false_keys
