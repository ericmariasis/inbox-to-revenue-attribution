from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import Settings
from app.services.growth_loop_agent import (
    LoomiDiagnosticContext,
    build_fixture_loomi_diagnostic_context,
)

MCP_PROTOCOL_VERSION = "2025-06-18"
TOOL_GET_PROJECT_OVERVIEW = "get_project_overview"
TOOL_LIST_SEGMENTATIONS = "list_segmentations"
TOOL_LIST_RECOMMENDATIONS = "list_recommendations"
LIVE_TOOL_NAMES = (
    TOOL_GET_PROJECT_OVERVIEW,
    TOOL_LIST_SEGMENTATIONS,
    TOOL_LIST_RECOMMENDATIONS,
)


class LoomiMcpError(RuntimeError):
    pass


@dataclass(frozen=True)
class LoomiMcpTool:
    name: str
    input_schema: Mapping[str, Any]


@dataclass(frozen=True)
class LoomiMcpCallResult:
    payload: Any


class LoomiMcpClient:
    def __init__(
        self,
        *,
        endpoint: str,
        access_token: str,
        timeout_seconds: float = 8.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._access_token = access_token
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._session_id: str | None = None
        self._next_request_id = 1

    def initialize(self) -> Mapping[str, Any]:
        result = self._request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": "inbox-to-revenue-growth-loop",
                    "version": "0.1.0",
                },
            },
        )
        self._notify_initialized()
        if isinstance(result, Mapping):
            return result
        return {}

    def list_tools(self) -> dict[str, LoomiMcpTool]:
        result = self._request("tools/list", {})
        if not isinstance(result, Mapping):
            raise LoomiMcpError("MCP tools/list returned a non-object result")
        raw_tools = result.get("tools")
        if not isinstance(raw_tools, list):
            raise LoomiMcpError("MCP tools/list did not include a tools list")

        tools: dict[str, LoomiMcpTool] = {}
        for raw_tool in raw_tools:
            if not isinstance(raw_tool, Mapping):
                continue
            name = raw_tool.get("name")
            if not isinstance(name, str) or not name:
                continue
            raw_schema = raw_tool.get("inputSchema")
            input_schema = raw_schema if isinstance(raw_schema, Mapping) else {}
            tools[name] = LoomiMcpTool(name=name, input_schema=input_schema)
        return tools

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> LoomiMcpCallResult:
        result = self._request(
            "tools/call",
            {
                "name": name,
                "arguments": dict(arguments),
            },
        )
        return LoomiMcpCallResult(payload=result)

    def _notify_initialized(self) -> None:
        try:
            self._post_json_rpc(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                }
            )
        except LoomiMcpError:
            return

    def _request(self, method: str, params: Mapping[str, Any]) -> Any:
        request_id = self._next_request_id
        self._next_request_id += 1
        response_payload = self._post_json_rpc(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": dict(params),
            }
        )
        if not isinstance(response_payload, Mapping):
            raise LoomiMcpError(f"MCP {method} returned a non-object response")
        if response_payload.get("error"):
            raise LoomiMcpError(f"MCP {method} returned an error")
        return response_payload.get("result")

    def _post_json_rpc(self, payload: Mapping[str, Any]) -> Any:
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        try:
            with httpx.Client(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = client.post(
                    self._endpoint,
                    headers=headers,
                    json=dict(payload),
                )
        except httpx.HTTPError as exc:
            raise LoomiMcpError("MCP request failed") from exc

        response_session_id = response.headers.get("mcp-session-id")
        if response_session_id:
            self._session_id = response_session_id

        if response.status_code == 202 and not response.content:
            return {}
        if response.status_code >= 400:
            raise LoomiMcpError(f"MCP request returned HTTP {response.status_code}")
        return _parse_mcp_response(response)


class LoomiMcpDiagnosticProvider:
    def __init__(
        self,
        *,
        settings: Settings,
        client: LoomiMcpClient | None = None,
    ) -> None:
        self._settings = settings
        self._client = client or LoomiMcpClient(
            endpoint=settings.growth_loop_loomi_mcp_endpoint,
            access_token=settings.growth_loop_loomi_mcp_access_token,
            timeout_seconds=settings.growth_loop_loomi_mcp_timeout_seconds,
        )

    def load_context(self) -> LoomiDiagnosticContext:
        self._client.initialize()
        tools = self._client.list_tools()
        missing_tools = tuple(name for name in LIVE_TOOL_NAMES if name not in tools)

        overview_payload: Any = None
        segments_payload: Any = None
        recommendations_payload: Any = None
        skipped_tools: list[str] = []

        if TOOL_GET_PROJECT_OVERVIEW in tools:
            args = _arguments_for_tool(
                tool=tools[TOOL_GET_PROJECT_OVERVIEW],
                settings=self._settings,
            )
            if args is None:
                skipped_tools.append(TOOL_GET_PROJECT_OVERVIEW)
            else:
                overview_payload = self._client.call_tool(
                    TOOL_GET_PROJECT_OVERVIEW,
                    args,
                ).payload
        if TOOL_LIST_SEGMENTATIONS in tools:
            args = _arguments_for_tool(
                tool=tools[TOOL_LIST_SEGMENTATIONS],
                settings=self._settings,
            )
            if args is None:
                skipped_tools.append(TOOL_LIST_SEGMENTATIONS)
            else:
                segments_payload = self._client.call_tool(
                    TOOL_LIST_SEGMENTATIONS,
                    args,
                ).payload
        if TOOL_LIST_RECOMMENDATIONS in tools:
            args = _arguments_for_tool(
                tool=tools[TOOL_LIST_RECOMMENDATIONS],
                settings=self._settings,
            )
            if args is None:
                skipped_tools.append(TOOL_LIST_RECOMMENDATIONS)
            else:
                recommendations_payload = self._client.call_tool(
                    TOOL_LIST_RECOMMENDATIONS,
                    args,
                ).payload

        analytics = _extract_diagnostic_lines(
            overview_payload,
            preferred_keys=("name", "title", "description", "summary", "status"),
            limit=3,
        )
        segments = _extract_diagnostic_lines(
            segments_payload,
            preferred_keys=("name", "title", "label", "description", "segment_name"),
            limit=5,
        )
        recommendations = _extract_diagnostic_lines(
            recommendations_payload,
            preferred_keys=("name", "title", "recommendation", "description", "summary"),
            limit=5,
        )
        limitations = [
            "Loomi MCP results are diagnostic context only.",
            "They do not count revenue, prove causality, or replace app-owned booking and payment records.",
        ]
        if missing_tools:
            limitations.append("Missing live Loomi MCP tools: " + ", ".join(missing_tools) + ".")
        if skipped_tools:
            limitations.append(
                "Skipped live Loomi MCP tools needing unconfigured required arguments: "
                + ", ".join(skipped_tools)
                + "."
            )
        if not analytics and not segments and not recommendations:
            limitations.append("Live Loomi MCP returned no mappable overview, segment, or recommendation text.")

        return LoomiDiagnosticContext(
            source_label="Loomi live MCP diagnostics",
            source_kind="live_mcp",
            source_status_label="Loomi live MCP",
            source_status_kind="live_mcp",
            source_status_detail=(
                "Live Loomi MCP responded through the configured runtime provider."
            ),
            segments=segments,
            predictions=(),
            recommendations=recommendations,
            analytics=analytics,
            limitations=tuple(limitations),
        )


def build_growth_loop_loomi_context(
    *,
    settings: Settings,
    provider: LoomiMcpDiagnosticProvider | None = None,
) -> LoomiDiagnosticContext:
    if not settings.growth_loop_loomi_mcp_enabled:
        return build_fixture_loomi_diagnostic_context()
    if not settings.growth_loop_loomi_mcp_endpoint or not settings.growth_loop_loomi_mcp_access_token:
        return build_fixture_loomi_diagnostic_context(
            source_status_detail=(
                "Live Loomi MCP is enabled but endpoint or bearer token config is missing, "
                "so fixture diagnostics are shown."
            )
        )

    try:
        diagnostic_provider = provider or LoomiMcpDiagnosticProvider(settings=settings)
        return diagnostic_provider.load_context()
    except Exception as exc:
        return build_fixture_loomi_diagnostic_context(
            source_status_detail=(
                f"Live Loomi MCP was unavailable ({_safe_error_reason(exc, settings=settings)}), "
                "so fixture diagnostics are shown."
            )
        )


def _parse_mcp_response(response: httpx.Response) -> Any:
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        return _parse_sse_json_response(response.text)
    try:
        return response.json()
    except ValueError as exc:
        raise LoomiMcpError("MCP response was not valid JSON") from exc


def _parse_sse_json_response(response_text: str) -> Any:
    data_lines: list[str] = []
    events: list[str] = []
    for raw_line in response_text.splitlines():
        line = raw_line.strip()
        if not line:
            if data_lines:
                events.append("\n".join(data_lines))
                data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").strip())
    if data_lines:
        events.append("\n".join(data_lines))

    for event_text in reversed(events):
        if not event_text or event_text == "[DONE]":
            continue
        try:
            return json.loads(event_text)
        except json.JSONDecodeError as exc:
            raise LoomiMcpError("MCP SSE data was not valid JSON") from exc
    raise LoomiMcpError("MCP SSE response did not contain JSON data")


def _arguments_for_tool(
    *,
    tool: LoomiMcpTool,
    settings: Settings,
) -> dict[str, Any] | None:
    schema = tool.input_schema
    raw_properties = schema.get("properties") if isinstance(schema, Mapping) else None
    properties = raw_properties if isinstance(raw_properties, Mapping) else {}
    raw_required = schema.get("required") if isinstance(schema, Mapping) else None
    required = (
        tuple(value for value in raw_required if isinstance(value, str))
        if isinstance(raw_required, list)
        else ()
    )

    args: dict[str, Any] = {}
    for name in properties:
        if not isinstance(name, str):
            continue
        value = _configured_argument_value(name=name, settings=settings)
        if value:
            args[name] = value

    missing_required = tuple(name for name in required if name not in args)
    if missing_required:
        return None
    return args


def _configured_argument_value(*, name: str, settings: Settings) -> str:
    normalized_name = name.replace("-", "_").lower()
    if "project" in normalized_name and "id" in normalized_name:
        return settings.growth_loop_loomi_mcp_project_id
    if "workspace" in normalized_name and "id" in normalized_name:
        return settings.growth_loop_loomi_mcp_workspace_id
    if (
        "organization" in normalized_name
        or "organisation" in normalized_name
        or "cloud" in normalized_name
    ) and "id" in normalized_name:
        return settings.growth_loop_loomi_mcp_organization_id
    return ""


def _extract_diagnostic_lines(
    payload: Any,
    *,
    preferred_keys: tuple[str, ...],
    limit: int,
) -> tuple[str, ...]:
    if payload is None:
        return ()

    normalized_payload = _normalize_mcp_tool_payload(payload)
    values: list[str] = []
    _collect_diagnostic_lines(
        normalized_payload,
        preferred_keys=preferred_keys,
        values=values,
    )

    unique_values: list[str] = []
    for value in values:
        cleaned = " ".join(value.split())
        if cleaned and cleaned not in unique_values:
            unique_values.append(cleaned[:240])
        if len(unique_values) >= limit:
            break
    return tuple(unique_values)


def _normalize_mcp_tool_payload(payload: Any) -> Any:
    if isinstance(payload, Mapping):
        content = payload.get("content")
        if isinstance(content, list):
            normalized_items = []
            for item in content:
                if not isinstance(item, Mapping):
                    continue
                text = item.get("text")
                if not isinstance(text, str):
                    continue
                normalized_items.append(_parse_possible_json_text(text))
            if normalized_items:
                return normalized_items
    return payload


def _parse_possible_json_text(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        return ""
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return stripped


def _collect_diagnostic_lines(
    payload: Any,
    *,
    preferred_keys: tuple[str, ...],
    values: list[str],
) -> None:
    if isinstance(payload, str):
        values.append(payload)
        return
    if isinstance(payload, Mapping):
        for key in preferred_keys:
            raw_value = payload.get(key)
            if isinstance(raw_value, str):
                values.append(raw_value)
        for raw_value in payload.values():
            if isinstance(raw_value, (Mapping, list, tuple)):
                _collect_diagnostic_lines(
                    raw_value,
                    preferred_keys=preferred_keys,
                    values=values,
                )
        return
    if isinstance(payload, (list, tuple)):
        for item in payload:
            _collect_diagnostic_lines(
                item,
                preferred_keys=preferred_keys,
                values=values,
            )


def _safe_error_reason(exc: Exception, *, settings: Settings) -> str:
    reason = str(exc).strip() or exc.__class__.__name__
    for secret_value in (
        settings.growth_loop_loomi_mcp_access_token,
        settings.growth_loop_loomi_mcp_endpoint,
    ):
        if secret_value:
            reason = reason.replace(secret_value, "[redacted]")
    return reason[:120]
