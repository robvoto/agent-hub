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
class HumanMCPConfig:
    enabled: bool
    command: str
    args: tuple[str, ...]
    allowed_tools: frozenset[str]
    connect_timeout_seconds: float
    call_timeout_seconds: float
    max_result_chars: int


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

    return HumanMCPConfig(
        enabled=enabled,
        command=command,
        args=tuple(args),
        allowed_tools=frozenset(v.strip() for v in allowed if v.strip()),
        connect_timeout_seconds=float(raw.get("connect_timeout_seconds", 30)),
        call_timeout_seconds=float(raw.get("call_timeout_seconds", 45)),
        max_result_chars=int(raw.get("max_result_chars", 30000)),
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

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
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


def _truncate_result(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n...<truncated {len(value) - limit} chars>"


_gateway: HumanMCPGateway | None = None
_gateway_lock = threading.Lock()


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
