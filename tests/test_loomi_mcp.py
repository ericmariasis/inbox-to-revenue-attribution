import json

import httpx

from app.core.config import Settings
from app.services.growth_loop_agent import LoomiDiagnosticContext
from app.services.loomi_mcp import (
    LoomiMcpClient,
    LoomiMcpDiagnosticProvider,
    LoomiMcpError,
    build_growth_loop_loomi_context,
)


def _settings(**overrides: object) -> Settings:
    values = {
        "app_env": "local",
        "growth_loop_loomi_mcp_enabled": True,
        "growth_loop_loomi_mcp_endpoint": "https://loomi.example.test/mcp",
        "growth_loop_loomi_mcp_access_token": "test-token",
        "growth_loop_loomi_mcp_project_id": "project_123",
        "growth_loop_loomi_mcp_workspace_id": "workspace_123",
        "growth_loop_loomi_mcp_organization_id": "org_123",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _json_rpc_response(request_id: int, result: object, *, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "result": result,
        },
        headers=headers,
    )


def _sse_response(request_id: int, result: object, *, headers: dict[str, str] | None = None) -> httpx.Response:
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": result,
        }
    )
    response_headers = {"content-type": "text/event-stream"}
    if headers:
        response_headers.update(headers)
    return httpx.Response(200, text=f"event: message\ndata: {payload}\n\n", headers=response_headers)


def test_loomi_mcp_client_parses_sse_and_reuses_session_header():
    seen_methods: list[str] = []
    session_headers: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        method = payload["method"]
        seen_methods.append(method)
        session_headers.append(request.headers.get("mcp-session-id"))
        if method == "initialize":
            return _sse_response(
                payload["id"],
                {"serverInfo": {"name": "loomi-mcp"}},
                headers={"mcp-session-id": "session_123"},
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return _json_rpc_response(payload["id"], {"tools": []})
        raise AssertionError(f"unexpected method {method}")

    client = LoomiMcpClient(
        endpoint="https://loomi.example.test/mcp",
        access_token="secret-token",
        transport=httpx.MockTransport(handler),
    )

    assert client.initialize()["serverInfo"]["name"] == "loomi-mcp"
    assert client.list_tools() == {}
    assert seen_methods == ["initialize", "notifications/initialized", "tools/list"]
    assert session_headers[0] is None
    assert session_headers[2] == "session_123"


def test_loomi_mcp_provider_maps_live_overview_segments_and_recommendations():
    settings = _settings()
    calls: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        method = payload["method"]
        if method == "initialize":
            return _json_rpc_response(payload["id"], {"serverInfo": {"name": "loomi-mcp"}})
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return _json_rpc_response(
                payload["id"],
                {
                    "tools": [
                        {
                            "name": "get_project_overview",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"project_id": {"type": "string"}},
                                "required": ["project_id"],
                            },
                        },
                        {
                            "name": "list_segmentations",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"workspace_id": {"type": "string"}},
                                "required": ["workspace_id"],
                            },
                        },
                        {
                            "name": "list_recommendations",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"organization_id": {"type": "string"}},
                                "required": ["organization_id"],
                            },
                        },
                    ]
                },
            )
        if method == "tools/call":
            name = payload["params"]["name"]
            arguments = payload["params"]["arguments"]
            calls.append((name, arguments))
            if name == "get_project_overview":
                result = {"content": [{"type": "text", "text": '{"name": "Pacific Tutors", "summary": "Live overview loaded"}'}]}
            elif name == "list_segmentations":
                result = {"content": [{"type": "text", "text": '{"items": [{"name": "High intent tutors"}, {"name": "Booked no paid"}]}'}]}
            elif name == "list_recommendations":
                result = {"content": [{"type": "text", "text": '{"items": [{"title": "Review a booking follow-up", "description": "Focus on booked prospects"}]}'}]}
            else:
                raise AssertionError(f"unexpected tool {name}")
            return _json_rpc_response(payload["id"], result)
        raise AssertionError(f"unexpected method {method}")

    client = LoomiMcpClient(
        endpoint=settings.growth_loop_loomi_mcp_endpoint,
        access_token=settings.growth_loop_loomi_mcp_access_token,
        transport=httpx.MockTransport(handler),
    )
    provider = LoomiMcpDiagnosticProvider(settings=settings, client=client)

    context = provider.load_context()

    assert context.source_kind == "live_mcp"
    assert context.source_status_label == "Loomi live MCP"
    assert "Pacific Tutors" in context.analytics
    assert "Live overview loaded" in context.analytics
    assert "High intent tutors" in context.segments
    assert "Booked no paid" in context.segments
    assert "Review a booking follow-up" in context.recommendations
    assert "Focus on booked prospects" in context.recommendations
    assert ("get_project_overview", {"project_id": "project_123"}) in calls
    assert ("list_segmentations", {"workspace_id": "workspace_123"}) in calls
    assert ("list_recommendations", {"organization_id": "org_123"}) in calls
    assert any("diagnostic context only" in limitation for limitation in context.limitations)


def test_growth_loop_loomi_context_falls_back_when_live_config_is_missing():
    settings = _settings(growth_loop_loomi_mcp_access_token="")

    context = build_growth_loop_loomi_context(settings=settings)

    assert context.source_kind == "diagnostic_fixture"
    assert context.source_status_label == "Loomi fixture fallback"
    assert "endpoint or bearer token config is missing" in context.source_status_detail


def test_growth_loop_loomi_context_falls_back_when_provider_fails():
    class FailingProvider:
        def load_context(self) -> LoomiDiagnosticContext:
            raise LoomiMcpError("HTTP 401 for test-token at https://loomi.example.test/mcp")

    context = build_growth_loop_loomi_context(
        settings=_settings(),
        provider=FailingProvider(),
    )

    assert context.source_kind == "diagnostic_fixture"
    assert context.source_status_label == "Loomi fixture fallback"
    assert "HTTP 401" in context.source_status_detail
    assert "test-token" not in context.source_status_detail
    assert "https://loomi.example.test/mcp" not in context.source_status_detail
    assert "[redacted]" in context.source_status_detail


def test_growth_loop_loomi_context_falls_back_on_malformed_mcp_response():
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json", headers={"content-type": "application/json"})

    provider = LoomiMcpDiagnosticProvider(
        settings=settings,
        client=LoomiMcpClient(
            endpoint=settings.growth_loop_loomi_mcp_endpoint,
            access_token=settings.growth_loop_loomi_mcp_access_token,
            transport=httpx.MockTransport(handler),
        ),
    )

    context = build_growth_loop_loomi_context(settings=settings, provider=provider)

    assert context.source_kind == "diagnostic_fixture"
    assert "not valid JSON" in context.source_status_detail


def test_growth_loop_loomi_context_falls_back_on_mcp_timeout():
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    provider = LoomiMcpDiagnosticProvider(
        settings=settings,
        client=LoomiMcpClient(
            endpoint=settings.growth_loop_loomi_mcp_endpoint,
            access_token=settings.growth_loop_loomi_mcp_access_token,
            transport=httpx.MockTransport(handler),
        ),
    )

    context = build_growth_loop_loomi_context(settings=settings, provider=provider)

    assert context.source_kind == "diagnostic_fixture"
    assert "MCP request failed" in context.source_status_detail


def test_loomi_mcp_provider_skips_tools_with_unconfigured_required_arguments():
    settings = _settings(growth_loop_loomi_mcp_project_id="")

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        method = payload["method"]
        if method == "initialize":
            return _json_rpc_response(payload["id"], {"serverInfo": {"name": "loomi-mcp"}})
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return _json_rpc_response(
                payload["id"],
                {
                    "tools": [
                        {
                            "name": "get_project_overview",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"project_id": {"type": "string"}},
                                "required": ["project_id"],
                            },
                        },
                    ]
                },
            )
        if method == "tools/call":
            raise AssertionError("tool with missing required args should not be called")
        raise AssertionError(f"unexpected method {method}")

    client = LoomiMcpClient(
        endpoint=settings.growth_loop_loomi_mcp_endpoint,
        access_token=settings.growth_loop_loomi_mcp_access_token,
        transport=httpx.MockTransport(handler),
    )

    context = LoomiMcpDiagnosticProvider(settings=settings, client=client).load_context()

    assert context.source_kind == "live_mcp"
    assert any("Skipped live Loomi MCP tools" in limitation for limitation in context.limitations)
