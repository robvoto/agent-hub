"""Bounded Human MCP client used by Agent Hub runtime tools."""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters

from .config import CONFIG_DIR
from .task_runs import get_current_task_run_id

logger = logging.getLogger(__name__)

HUMAN_MCP_CONFIG_FILE = CONFIG_DIR / "human_mcp.json"


class HumanMCPError(RuntimeError):
    """Raised when the configured Human MCP runtime is unavailable or invalid."""


@dataclass(frozen=True)
class HumanMCPTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool
    destructive: bool
    idempotent: bool
    open_world: bool


@dataclass(frozen=True)
class BrowserContextConfig:
    """Explicit contract for preparing an isolated, agent-owned browser context."""

    enabled: bool
    max_calls_per_task: int
    auto_prepare_tools: frozenset[str]
    session_tool: str
    tab_tool: str
    session_open_arguments: dict[str, Any]
    tab_open_arguments: dict[str, Any]
    session_id_argument: str
    tab_id_argument: str
    session_id_response_field: str
    tab_id_response_field: str
    tab_session_id_response_field: str
    isolation_response_field: str
    ownership_response_field: str
    ownership_response_value: str
    isolation_request_argument: str
    isolation_request_value: Any
    ownership_request_argument: str
    ownership_request_value: str


@dataclass(frozen=True)
class HumanMCPConfig:
    enabled: bool
    command: str
    args: tuple[str, ...]
    allowed_tools: frozenset[str]
    connect_timeout_seconds: float
    call_timeout_seconds: float
    max_result_chars: int
    browser_context: BrowserContextConfig


def _disabled_browser_context_config() -> BrowserContextConfig:
    return BrowserContextConfig(
        enabled=False,
        max_calls_per_task=0,
        auto_prepare_tools=frozenset(),
        session_tool="browser_open_session",
        tab_tool="browser_open_tab",
        session_open_arguments={},
        tab_open_arguments={},
        session_id_argument="session_id",
        tab_id_argument="tab_id",
        session_id_response_field="session_id",
        tab_id_response_field="tab_id",
        tab_session_id_response_field="session_id",
        isolation_response_field="isolated",
        ownership_response_field="owner",
        ownership_response_value="agent",
        isolation_request_argument="isolation",
        isolation_request_value="isolated",
        ownership_request_argument="owner",
        ownership_request_value="agent",
    )


def _load_browser_context_config(
    raw: Any,
    allowed_tools: frozenset[str],
) -> BrowserContextConfig:
    if raw is None:
        return _disabled_browser_context_config()
    if not isinstance(raw, dict):
        raise HumanMCPError("Human MCP browser_context must be an object.")

    enabled = bool(raw.get("enabled", False))
    defaults = _disabled_browser_context_config()
    if not enabled:
        return defaults

    try:
        max_calls = int(raw.get("max_calls_per_task", 12))
    except (TypeError, ValueError) as exc:
        raise HumanMCPError(
            "Human MCP browser_context max_calls_per_task must be an integer."
        ) from exc
    if not 1 <= max_calls <= 100:
        raise HumanMCPError(
            "Human MCP browser_context max_calls_per_task must be between 1 and 100."
        )

    auto_prepare = raw.get("auto_prepare_tools") or []
    if not isinstance(auto_prepare, list) or not auto_prepare or not all(
        isinstance(value, str) and value.strip() for value in auto_prepare
    ):
        raise HumanMCPError(
            "Enabled Human MCP browser_context requires a non-empty auto_prepare_tools list."
        )
    auto_prepare_tools = frozenset(value.strip() for value in auto_prepare)

    session_tool = str(raw.get("session_tool", defaults.session_tool)).strip()
    tab_tool = str(raw.get("tab_tool", defaults.tab_tool)).strip()
    if not session_tool or not tab_tool:
        raise HumanMCPError("Human MCP browser_context requires session_tool and tab_tool.")
    missing_tools = {session_tool, tab_tool, *auto_prepare_tools} - allowed_tools
    if missing_tools:
        raise HumanMCPError(
            "Human MCP browser_context tools must be explicitly allowlisted: "
            + ", ".join(sorted(missing_tools))
        )

    session_args = raw.get("session_open_arguments")
    tab_args = raw.get("tab_open_arguments")
    if not isinstance(session_args, dict) or not isinstance(tab_args, dict):
        raise HumanMCPError(
            "Enabled Human MCP browser_context requires session_open_arguments and "
            "tab_open_arguments objects."
        )

    session_id_argument = str(
        raw.get("session_id_argument", defaults.session_id_argument)
    ).strip()
    tab_id_argument = str(raw.get("tab_id_argument", "")).strip()
    session_id_response_field = str(
        raw.get("session_id_response_field", defaults.session_id_response_field)
    ).strip()
    tab_id_response_field = str(
        raw.get("tab_id_response_field", defaults.tab_id_response_field)
    ).strip()
    tab_session_id_response_field = str(
        raw.get("tab_session_id_response_field", defaults.tab_session_id_response_field)
    ).strip()
    isolation_response_field = str(
        raw.get("isolation_response_field", defaults.isolation_response_field)
    ).strip()
    ownership_response_field = str(
        raw.get("ownership_response_field", defaults.ownership_response_field)
    ).strip()
    isolation_request_argument = str(
        raw.get("isolation_request_argument", defaults.isolation_request_argument)
    ).strip()
    ownership_request_argument = str(
        raw.get("ownership_request_argument", defaults.ownership_request_argument)
    ).strip()
    required_fields = {
        "session_id_argument": session_id_argument,
        "tab_id_argument": tab_id_argument,
        "session_id_response_field": session_id_response_field,
        "tab_id_response_field": tab_id_response_field,
        "tab_session_id_response_field": tab_session_id_response_field,
        "isolation_response_field": isolation_response_field,
        "ownership_response_field": ownership_response_field,
        "isolation_request_argument": isolation_request_argument,
        "ownership_request_argument": ownership_request_argument,
    }
    if any(not value for value in required_fields.values()):
        raise HumanMCPError(
            "Enabled Human MCP browser_context requires explicit response and argument field names."
        )

    if session_args.get(isolation_request_argument) != raw.get(
        "isolation_request_value", defaults.isolation_request_value
    ):
        raise HumanMCPError(
            "Human MCP browser_context session_open_arguments must request the configured "
            "isolated mode."
        )
    ownership_request_value = raw.get(
        "ownership_request_value", defaults.ownership_request_value
    )
    if session_args.get(ownership_request_argument) != ownership_request_value:
        raise HumanMCPError(
            "Human MCP browser_context session_open_arguments must request the configured "
            "agent ownership."
        )
    if tab_args.get(session_id_argument) != "$session_id":
        raise HumanMCPError(
            'Human MCP browser_context tab_open_arguments must bind the new tab to "$session_id".'
        )

    return BrowserContextConfig(
        enabled=True,
        max_calls_per_task=max_calls,
        auto_prepare_tools=auto_prepare_tools,
        session_tool=session_tool,
        tab_tool=tab_tool,
        session_open_arguments=dict(session_args),
        tab_open_arguments=dict(tab_args),
        session_id_argument=session_id_argument,
        tab_id_argument=tab_id_argument,
        session_id_response_field=session_id_response_field,
        tab_id_response_field=tab_id_response_field,
        tab_session_id_response_field=tab_session_id_response_field,
        isolation_response_field=isolation_response_field,
        ownership_response_field=ownership_response_field,
        ownership_response_value=str(
            raw.get("ownership_response_value", defaults.ownership_response_value)
        ),
        isolation_request_argument=isolation_request_argument,
        isolation_request_value=raw.get(
            "isolation_request_value", defaults.isolation_request_value
        ),
        ownership_request_argument=ownership_request_argument,
        ownership_request_value=str(ownership_request_value),
    )


def load_human_mcp_config(path: Path = HUMAN_MCP_CONFIG_FILE) -> HumanMCPConfig:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HumanMCPError(f"Human MCP config does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise HumanMCPError(f"Human MCP config is invalid JSON: {path}: {exc}") from exc

    transport = raw.get("transport")
    if not isinstance(transport, dict):
        raise HumanMCPError("Human MCP config requires a transport object.")
    command = str(transport.get("command") or "").strip()
    args = transport.get("args") or []
    if not command or not isinstance(args, list) or not all(isinstance(v, str) for v in args):
        raise HumanMCPError("Human MCP transport requires a command and string args.")

    allowed = raw.get("allowed_tools") or []
    if not isinstance(allowed, list) or not allowed or not all(isinstance(v, str) for v in allowed):
        raise HumanMCPError("Human MCP allowed_tools must be a non-empty list of names.")

    enabled_override = os.getenv("HUB_HUMAN_MCP_ENABLED", "").strip().casefold()
    if enabled_override:
        if enabled_override not in {"true", "false", "1", "0", "yes", "no"}:
            raise HumanMCPError(
                "HUB_HUMAN_MCP_ENABLED must be one of: true, false, 1, 0, yes, no."
            )
        enabled = enabled_override in {"true", "1", "yes"}
    else:
        enabled = bool(raw.get("enabled", False))

    allowed_tools = frozenset(v.strip() for v in allowed if v.strip())

    return HumanMCPConfig(
        enabled=enabled,
        command=command,
        args=tuple(args),
        allowed_tools=allowed_tools,
        connect_timeout_seconds=float(raw.get("connect_timeout_seconds", 30)),
        call_timeout_seconds=float(raw.get("call_timeout_seconds", 45)),
        max_result_chars=int(raw.get("max_result_chars", 30000)),
        browser_context=_load_browser_context_config(raw.get("browser_context"), allowed_tools),
    )


class HumanMCPGateway:
    """Own one persistent stdio MCP session on a private event-loop thread."""

    def __init__(self, config: HumanMCPConfig | None = None) -> None:
        self.config = config or load_human_mcp_config()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = threading.Thread(
            target=self._thread_main,
            name="agent-hub-human-mcp",
            daemon=True,
        )
        self._client: Client | None = None
        self._tools: dict[str, HumanMCPTool] = {}
        self._ready = threading.Event()
        self._stop_event: asyncio.Event | None = None
        self._startup_error: Exception | None = None
        self._closed = False
        self._browser_contexts: dict[str, tuple[str, str]] = {}
        self._browser_context_lock = threading.RLock()
        self._browser_call_counts: dict[str, int] = {}
        self._thread.start()
        if self.config.enabled:
            if not self._ready.wait(timeout=self.config.connect_timeout_seconds):
                raise HumanMCPError("Timed out starting Human MCP gateway.")
            if self._startup_error is not None:
                raise HumanMCPError(
                    f"Human MCP gateway startup failed: {self._startup_error}"
                ) from self._startup_error

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
        finally:
            loop.close()

    def _submit(self, coro: Any, *, timeout: float) -> Any:
        if self._closed:
            raise HumanMCPError("Human MCP gateway is closed.")
        if self._loop is None or self._startup_error is not None:
            raise HumanMCPError("Human MCP gateway is not available.")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except Exception as exc:
            future.cancel()
            raise HumanMCPError(f"Human MCP operation failed: {exc}") from exc

    async def _serve(self) -> None:
        params = StdioServerParameters(
            command=self.config.command,
            args=list(self.config.args),
        )
        client = Client(params, read_timeout_seconds=self.config.call_timeout_seconds)
        try:
            async with client:
                listed = await client.list_tools()
                tools: dict[str, HumanMCPTool] = {}
                for item in listed.tools:
                    if item.name not in self.config.allowed_tools:
                        continue
                    annotations = item.annotations
                    if annotations is None or annotations.read_only_hint is None:
                        raise HumanMCPError(
                            f"Allowed Human MCP tool '{item.name}' has no explicit read_only_hint."
                        )
                    tools[item.name] = HumanMCPTool(
                        name=item.name,
                        description=item.description or f"Human MCP tool: {item.name}",
                        input_schema=dict(
                            item.input_schema or {"type": "object", "properties": {}}
                        ),
                        read_only=bool(annotations.read_only_hint),
                        destructive=bool(annotations.destructive_hint),
                        idempotent=bool(annotations.idempotent_hint),
                        open_world=bool(annotations.open_world_hint),
                    )

                missing = sorted(self.config.allowed_tools - tools.keys())
                if missing:
                    logger.warning(
                        "Human MCP allowed tools unavailable from server: %s",
                        ", ".join(missing),
                    )
                self._client = client
                self._tools = tools
                self._stop_event = asyncio.Event()
                logger.info(
                    "Human MCP gateway connected with %d allowlisted tool(s).",
                    len(tools),
                )
                self._ready.set()
                await self._stop_event.wait()
        except Exception as exc:
            self._startup_error = exc
            self._ready.set()
        finally:
            self._client = None

    def list_tools(self) -> list[HumanMCPTool]:
        return sorted(self._tools.values(), key=lambda item: item.name)

    async def _call(self, name: str, arguments: dict[str, Any]) -> str:
        if self._client is None:
            raise HumanMCPError("Human MCP gateway is not connected.")
        if name not in self._tools:
            raise HumanMCPError(f"Human MCP tool '{name}' is not allowlisted or unavailable.")
        result = await self._client.call_tool(
            name,
            arguments,
            read_timeout_seconds=self.config.call_timeout_seconds,
        )
        if result.is_error:
            text = _result_text(result)
            raise HumanMCPError(f"Human MCP tool '{name}' failed: {text}")
        return _truncate_result(_result_text(result), self.config.max_result_chars)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        return self._submit(
            self._call(name, arguments),
            timeout=self.config.call_timeout_seconds + 5,
        )

    def call_browser_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        session_id: str | None = None,
    ) -> str:
        """Call a configured read-only browser tool after proving safe context."""
        policy = self.config.browser_context
        if not policy.enabled:
            raise HumanMCPError(
                "No isolated agent-owned browser context is configured; no existing browser "
                "tab was used. Configure Human MCP browser_context with explicit isolation "
                "evidence before enabling automatic browser research."
            )
        if name not in policy.auto_prepare_tools:
            raise HumanMCPError(
                f"Human MCP browser tool '{name}' is not approved for automatic read-only "
                "context use."
            )
        if not isinstance(session_id, str) or not session_id.strip():
            raise HumanMCPError(
                "Automatic browser research requires the canonical Hub session id; refusing "
                "to select an unscoped browser context."
            )
        info = self._tools.get(name)
        if info is None or not info.read_only or info.destructive:
            raise HumanMCPError(
                f"Human MCP browser tool '{name}' is not a read-only, non-destructive tool."
            )
        try:
            self._validate_browser_binding_schema(info, policy)
            context = self.ensure_browser_context(session_id)
            bound_arguments = self._bind_browser_arguments(
                info, arguments, context, policy
            )
        except HumanMCPError:
            self._audit_browser("context", name, "failed")
            raise
        return self._call_browser_tool(name, bound_arguments, phase="browser")

    def ensure_browser_context(self, hub_session_id: str) -> tuple[str, str]:
        """Create/reuse only a context whose MCP response proves isolation and ownership."""
        policy = self.config.browser_context
        if not policy.enabled:
            raise HumanMCPError(
                "No isolated agent-owned browser context is configured; refusing to use an "
                "existing browser tab."
            )
        if not isinstance(hub_session_id, str) or not hub_session_id.strip():
            raise HumanMCPError(
                "Automatic browser research requires the canonical Hub session id; refusing "
                "to select an unscoped browser context."
            )
        with self._browser_context_lock:
            existing = self._browser_contexts.get(hub_session_id)
            if existing is not None:
                self._audit_browser("reuse", policy.session_tool, "ok")
                return existing

            session_tool = self._tools.get(policy.session_tool)
            tab_tool = self._tools.get(policy.tab_tool)
            if session_tool is None or tab_tool is None:
                raise HumanMCPError(
                    "Human MCP cannot guarantee an isolated browser context because the "
                    "configured session and tab setup tools are unavailable. Existing tabs "
                    "will not be selected."
                )
            session_args = dict(policy.session_open_arguments)
            tab_args_template = dict(policy.tab_open_arguments)
            self._validate_setup_arguments(session_tool, session_args, policy.session_tool)
            self._validate_setup_arguments(
                tab_tool, tab_args_template, policy.tab_tool, allow_placeholder=True
            )

            self._audit_browser("open_session", policy.session_tool, "started")
            session_result = self._call_browser_tool(
                policy.session_tool, session_args, phase="setup"
            )
            session_payload = _parse_json_object(session_result)
            mcp_session_id = _required_response_value(
                session_payload, policy.session_id_response_field, "session id"
            )
            if _response_value(session_payload, policy.isolation_response_field) is not True:
                raise HumanMCPError(
                    "Human MCP did not prove that the new browser session is isolated; refusing "
                    "to use it or any existing tab."
                )
            if _response_value(session_payload, policy.ownership_response_field) != (
                policy.ownership_response_value
            ):
                raise HumanMCPError(
                    "Human MCP did not prove that the new browser session is agent-owned; "
                    "refusing to use it or any existing tab."
                )

            tab_args = _replace_session_id(tab_args_template, mcp_session_id)
            self._validate_setup_arguments(tab_tool, tab_args, policy.tab_tool)
            self._audit_browser("open_tab", policy.tab_tool, "started")
            tab_result = self._call_browser_tool(policy.tab_tool, tab_args, phase="setup")
            tab_payload = _parse_json_object(tab_result)
            tab_id = _required_response_value(tab_payload, policy.tab_id_response_field, "tab id")
            if _response_value(tab_payload, policy.tab_session_id_response_field) != mcp_session_id:
                raise HumanMCPError(
                    "Human MCP did not prove that the new browser tab belongs to the isolated "
                    "agent-owned session; refusing to use it."
                )
            self._browser_contexts[hub_session_id] = (mcp_session_id, tab_id)
            self._audit_browser("setup", policy.tab_tool, "ok")
            return self._browser_contexts[hub_session_id]

    def discard_browser_context(self, session_id: str) -> None:
        """Drop local autonomous-context state when a Hub session is rotated."""
        with self._browser_context_lock:
            removed = self._browser_contexts.pop(session_id, None)
            if removed is not None:
                self._audit_browser("discard", self.config.browser_context.tab_tool, "ok")

    def _bind_browser_arguments(
        self,
        info: HumanMCPTool,
        arguments: dict[str, Any],
        context: tuple[str, str],
        policy: BrowserContextConfig,
    ) -> dict[str, Any]:
        self._validate_browser_binding_schema(info, policy)
        session_id, tab_id = context
        bound = dict(arguments)
        expected = {
            policy.session_id_argument: session_id,
            policy.tab_id_argument: tab_id,
        }
        for argument, value in expected.items():
            if argument in bound and bound[argument] != value:
                raise HumanMCPError(
                    f"Human MCP browser tool '{info.name}' supplied a context argument for "
                    "a different session or tab; refusing to cross-bind browser state."
                )
            bound[argument] = value
        return bound

    def _validate_browser_binding_schema(
        self,
        info: HumanMCPTool,
        policy: BrowserContextConfig,
    ) -> None:
        properties = info.input_schema.get("properties")
        if not isinstance(properties, dict) or any(
            argument not in properties
            for argument in (policy.session_id_argument, policy.tab_id_argument)
        ):
            raise HumanMCPError(
                f"Human MCP browser tool '{info.name}' cannot prove session/tab binding; "
                "its schema does not expose both configured context arguments."
            )

    def _call_browser_tool(self, name: str, arguments: dict[str, Any], *, phase: str) -> str:
        run_id = get_current_task_run_id()
        if run_id is not None:
            used = self._browser_call_counts.get(run_id, 0)
            if used >= self.config.browser_context.max_calls_per_task:
                raise HumanMCPError(
                    "Human MCP browser call limit reached for this task; research stopped "
                    "without opening another context or tab."
                )
            self._browser_call_counts[run_id] = used + 1
        try:
            result = self.call_tool(name, arguments)
        except HumanMCPError:
            self._audit_browser(phase, name, "failed")
            raise
        self._audit_browser(phase, name, "ok")
        return result

    def _validate_setup_arguments(
        self,
        info: HumanMCPTool,
        arguments: dict[str, Any],
        tool_name: str,
        *,
        allow_placeholder: bool = False,
    ) -> None:
        schema = info.input_schema
        properties = schema.get("properties") if isinstance(schema, dict) else None
        required = schema.get("required", []) if isinstance(schema, dict) else []
        if not isinstance(properties, dict) or not isinstance(required, list):
            raise HumanMCPError(
                f"Human MCP tool '{tool_name}' has no usable input schema for isolated setup."
            )
        missing = [key for key in required if key not in arguments]
        if missing:
            raise HumanMCPError(
                f"Human MCP isolated setup for '{tool_name}' is missing required argument(s): "
                + ", ".join(sorted(str(value) for value in missing))
            )
        if schema.get("additionalProperties") is False:
            unknown = set(arguments) - set(properties)
            if unknown:
                raise HumanMCPError(
                    f"Human MCP isolated setup for '{tool_name}' contains unsupported "
                    "argument(s): "
                    + ", ".join(sorted(unknown))
                )
        if not allow_placeholder and _contains_placeholder(arguments):
            raise HumanMCPError(
                f"Human MCP isolated setup for '{tool_name}' has an unresolved placeholder."
            )

    def _audit_browser(self, phase: str, tool_name: str, outcome: str) -> None:
        logger.info(
            "Human MCP browser audit: run_id=%s phase=%s tool=%s outcome=%s",
            get_current_task_run_id() or "unscoped",
            phase,
            tool_name,
            outcome,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._browser_context_lock:
            self._browser_contexts.clear()
        if self._loop is not None and self._stop_event is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            logger.error("Human MCP gateway thread did not stop within 5 seconds.")


def _result_text(result: Any) -> str:
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict) and isinstance(structured.get("result"), str):
        return structured["result"]
    texts = [
        content.text
        for content in getattr(result, "content", [])
        if getattr(content, "type", None) == "text"
        and isinstance(getattr(content, "text", None), str)
    ]
    if texts:
        return "\n".join(texts)
    return json.dumps(result.model_dump(mode="json"), ensure_ascii=False)


def _parse_json_object(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise HumanMCPError(
            "Human MCP isolated browser setup returned no structured context evidence; refusing "
            "to select an existing tab."
        ) from exc
    if isinstance(parsed, dict) and isinstance(parsed.get("result"), str):
        return _parse_json_object(parsed["result"])
    if not isinstance(parsed, dict):
        raise HumanMCPError(
            "Human MCP isolated browser setup returned invalid context evidence; refusing to "
            "select an existing tab."
        )
    return parsed


def _response_value(payload: dict[str, Any], field: str) -> Any:
    value: Any = payload
    for part in field.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _required_response_value(payload: dict[str, Any], field: str, label: str) -> str:
    value = _response_value(payload, field)
    if not isinstance(value, str) or not value.strip():
        raise HumanMCPError(
            f"Human MCP isolated browser setup returned no {label} evidence; refusing to "
            "select an existing tab."
        )
    return value.strip()


def _replace_session_id(value: Any, session_id: str) -> Any:
    if isinstance(value, str):
        return value.replace("$session_id", session_id)
    if isinstance(value, list):
        return [_replace_session_id(item, session_id) for item in value]
    if isinstance(value, dict):
        return {key: _replace_session_id(item, session_id) for key, item in value.items()}
    return value


def _contains_placeholder(value: Any) -> bool:
    if isinstance(value, str):
        return "$session_id" in value
    if isinstance(value, list):
        return any(_contains_placeholder(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_placeholder(item) for item in value.values())
    return False


def _truncate_result(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n...<truncated {len(value) - limit} chars>"


_gateway: HumanMCPGateway | None = None
_gateway_lock = threading.Lock()


def discard_human_mcp_browser_context(session_id: str) -> None:
    """Discard context state only when an already-running gateway owns it."""
    with _gateway_lock:
        gateway = _gateway
    if gateway is not None:
        gateway.discard_browser_context(session_id)


def get_human_mcp_gateway() -> HumanMCPGateway | None:
    global _gateway
    config = load_human_mcp_config()
    if not config.enabled:
        return None
    with _gateway_lock:
        if _gateway is None:
            _gateway = HumanMCPGateway(config)
            atexit.register(_gateway.close)
        return _gateway
