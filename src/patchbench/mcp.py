"""Run-scoped MCP configuration and sanitized experiment evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from patchbench.exceptions import PatchbenchError
from patchbench.runner import is_secret_key


SUPPORTED_LANDB_PROFILES = ("curated", "broad")
_SERVER_NAME = re.compile(r"[A-Za-z0-9_-]+")
_LANDB_ALLOWED_ENV = {
    "LANDB_AUTH_TYPE",
    "LANDB_TOKEN",
    "LANDB_BASIC_USERNAME",
    "LANDB_BASIC_PASSWORD",
    "LANDB_BASE_URL",
    "LANDB_V1_BASE_URL",
    "LANDB_BETA_BASE_URL",
    "LANDB_ENV",
    "LANDB_TIMEOUT_SECONDS",
    "LANDB_MCP_PROFILE",
}
_LANDB_FORCED_SAFE_ENV = {
    "LANDB_MCP_ALLOW_PRODUCTION_BENCHMARK": "false",
    "LANDB_MCP_EXPOSE_DEBUG_TOOLS": "false",
    "LANDB_ALLOW_WRITE": "false",
    "LANDB_WRITE_ENABLED": "false",
    "LANDB_PROD_WRITE_ENABLED": "false",
}
_KNOWN_PRODUCTION_HOST = "service.example.invalid"


@dataclass(frozen=True)
class McpServerConfig:
    """One stdio MCP server visible only during a solver run."""

    name: str
    command: str
    environment: dict[str, str]
    profile: str | None = None
    revision: str | None = None
    revision_dirty: bool | None = None

    def __post_init__(self) -> None:
        if _SERVER_NAME.fullmatch(self.name) is None:
            raise PatchbenchError(
                "MCP server name must contain only letters, numbers, '_' or '-'"
            )
        if not self.command or not Path(self.command).is_absolute():
            raise PatchbenchError("MCP command must be an absolute path")

    def claude_config(self) -> dict[str, Any]:
        return {
            "type": "stdio",
            "command": self.command,
            "args": [],
            "env": dict(sorted(self.environment.items())),
        }


@dataclass(frozen=True)
class McpRunConfig:
    """Validated MCP servers supplied to one Claude Code invocation."""

    servers: tuple[McpServerConfig, ...] = ()
    condition: str = "none"

    def __post_init__(self) -> None:
        names = [server.name for server in self.servers]
        if len(names) != len(set(names)):
            raise PatchbenchError("MCP server names must be unique")
        if not self.servers and self.condition != "none":
            raise PatchbenchError("MCP condition must be 'none' when MCP is disabled")
        if self.servers and self.condition == "none":
            raise PatchbenchError(
                "Enabled MCP configuration requires a named condition"
            )

    @property
    def enabled(self) -> bool:
        return bool(self.servers)

    @property
    def server_names(self) -> tuple[str, ...]:
        return tuple(server.name for server in self.servers)

    def claude_config(self) -> dict[str, Any]:
        return {
            "mcpServers": {
                server.name: server.claude_config() for server in self.servers
            }
        }

    def redaction_environment(self) -> dict[str, str]:
        return {
            key: value
            for server in self.servers
            for key, value in server.environment.items()
        }

    def metadata(self) -> dict[str, Any]:
        primary = self.servers[0] if len(self.servers) == 1 else None
        return {
            "mcp_enabled": self.enabled,
            "mcp_servers": list(self.server_names),
            "mcp_condition": self.condition,
            "mcp_server_name": primary.name if primary else None,
            "mcp_profile": primary.profile if primary else None,
            "mcp_revision": primary.revision if primary else None,
            "mcp_revision_dirty": primary.revision_dirty if primary else None,
        }


def disabled_mcp_config() -> McpRunConfig:
    return McpRunConfig()


def landb_mcp_config(
    *,
    command: str | Path,
    profile: str,
    env_file: str | Path | None = None,
    environment: dict[str, str] | None = None,
    validate_executable: bool = True,
    revision: str | None = None,
    revision_dirty: bool | None = None,
) -> McpRunConfig:
    """Create a safe read-only LanDB MCP condition for a solver run."""
    if profile not in SUPPORTED_LANDB_PROFILES:
        choices = ", ".join(SUPPORTED_LANDB_PROFILES)
        raise PatchbenchError(
            f"Invalid LanDB MCP profile {profile!r}; choose {choices}"
        )

    command_path = Path(command).expanduser()
    if not command_path.is_absolute():
        command_path = command_path.resolve()
    if validate_executable:
        _validate_executable(command_path)

    supplied: dict[str, str] = {}
    if env_file is not None:
        supplied.update(parse_literal_env_file(Path(env_file), label="MCP"))
    supplied.update(environment or {})
    unknown = sorted(set(supplied) - _LANDB_ALLOWED_ENV - set(_LANDB_FORCED_SAFE_ENV))
    if unknown:
        raise PatchbenchError(
            "Unsupported LanDB MCP environment variable(s): " + ", ".join(unknown)
        )
    for key, safe_value in _LANDB_FORCED_SAFE_ENV.items():
        configured = supplied.get(key)
        if configured is not None and configured.lower() != safe_value:
            raise PatchbenchError(f"Benchmark MCP runs require {key}={safe_value}")
        supplied[key] = safe_value
    configured_profile = supplied.get("LANDB_MCP_PROFILE")
    if configured_profile is not None and configured_profile != profile:
        raise PatchbenchError(
            "LANDB_MCP_PROFILE in --mcp-env-file does not match --mcp-profile"
        )
    supplied["LANDB_MCP_PROFILE"] = profile
    _validate_landb_runtime(supplied)

    if revision is None and revision_dirty is None and validate_executable:
        revision, revision_dirty = git_revision_for_executable(command_path)
    server = McpServerConfig(
        name="landb",
        command=str(command_path),
        environment=supplied,
        profile=profile,
        revision=revision,
        revision_dirty=revision_dirty,
    )
    return McpRunConfig((server,), condition=profile)


def parse_literal_env_file(path: Path, *, label: str = "Agent") -> dict[str, str]:
    """Parse literal ``export NAME=value`` assignments without shell execution."""
    path = path.expanduser().resolve()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise PatchbenchError(f"Could not read {label} env file: {path}") from exc
    environment: dict[str, str] = {}
    assignment = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)")
    unsafe_unquoted = re.compile(r"[\s#;&|<>`$(){}\\]")
    for number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        match = assignment.fullmatch(line)
        if match is None:
            raise PatchbenchError(
                f"Malformed {label} env file line {number}; expected export NAME=value"
            )
        key, raw_value = match.groups()
        if not raw_value:
            value = ""
        elif raw_value[0] in {"'", '"'}:
            quote = raw_value[0]
            if len(raw_value) < 2 or raw_value[-1] != quote:
                raise PatchbenchError(
                    f"Malformed quoted value on {label} env file line {number}"
                )
            inner = raw_value[1:-1]
            if (
                quote in inner
                or "\\" in inner
                or (quote == '"' and re.search(r"[`$]", inner))
            ):
                raise PatchbenchError(
                    f"Shell expansion or escaping is not allowed on {label} env file line {number}"
                )
            value = inner
        else:
            if unsafe_unquoted.search(raw_value):
                raise PatchbenchError(
                    f"Shell syntax is not allowed on {label} env file line {number}; "
                    "quote literal special characters"
                )
            value = raw_value
        environment[key] = value
    return environment


def git_revision_for_executable(command: Path) -> tuple[str | None, bool | None]:
    """Return the containing repository revision and dirty state when discoverable."""
    for candidate in (command, *command.parents):
        root = candidate if candidate.is_dir() else candidate.parent
        if not (root / ".git").exists():
            continue
        try:
            revision = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            ).stdout.strip()
            dirty = bool(
                subprocess.run(
                    ["git", "-C", str(root), "status", "--porcelain"],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=5,
                ).stdout.strip()
            )
            return revision or None, dirty
        except (OSError, subprocess.SubprocessError):
            return None, None
    return None, None


def write_mcp_config(path: Path, config: McpRunConfig) -> None:
    """Write a short-lived Claude MCP config readable only by the current user."""
    path.write_text(
        json.dumps(config.claude_config(), indent=2) + "\n", encoding="utf-8"
    )
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def summarize_mcp_arguments(value: Any) -> Any:
    """Keep argument shape and stable hashes without retaining production values."""
    if isinstance(value, dict):
        if set(value) == {"type", "length", "sha256"} and value.get("type") == "string":
            return dict(value)
        return {
            str(key): "<redacted>"
            if is_secret_key(str(key))
            else summarize_mcp_arguments(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, list):
        return {"type": "list", "length": len(value)}
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    text = str(value)
    return {
        "type": "string",
        "length": len(text),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:12],
    }


@dataclass
class McpEvidenceCollector:
    """Collect only evidence Claude's structured event stream actually exposes."""

    configured_servers: tuple[str, ...] = ()
    server_statuses: dict[str, str] = field(default_factory=dict)
    tools_discovered: bool | None = None
    interactions: list[dict[str, Any]] = field(default_factory=list)
    _calls_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    _event_index: int = 0

    def observe(self, event: dict[str, Any]) -> None:
        index = self._event_index
        self._event_index += 1
        self._observe_init(event)
        for block in _content_blocks(event):
            block_type = block.get("type")
            if block_type in {"tool_use", "tool"}:
                self._observe_tool_use(block, event, index)
            elif block_type == "tool_result":
                self._observe_tool_result(block, event, index)

    def is_mcp_tool_id(self, value: object) -> bool:
        return isinstance(value, str) and value in self._calls_by_id

    def details(self) -> dict[str, Any]:
        statuses = [self.server_statuses.get(name) for name in self.configured_servers]
        known = [status for status in statuses if status is not None]
        started: bool | None
        if any(_connected(status) for status in known):
            started = True
        elif known:
            started = False
        else:
            started = None
        successes = sum(item.get("success") is True for item in self.interactions)
        errors = sum(item.get("success") is False for item in self.interactions)
        return {
            "mcp_process_started": started,
            "mcp_server_statuses": dict(sorted(self.server_statuses.items())),
            "mcp_tools_discovered": self.tools_discovered,
            "mcp_activity_observed": bool(self.interactions),
            "mcp_tool_call_count": len(self.interactions),
            "mcp_tool_success_count": successes,
            "mcp_tool_error_count": errors,
            "mcp_telemetry_available": bool(self.server_statuses)
            or self.tools_discovered is not None
            or bool(self.interactions),
            "mcp_interactions": [dict(item) for item in self.interactions],
        }

    def _observe_init(self, event: dict[str, Any]) -> None:
        if event.get("type") != "system" or event.get("subtype") != "init":
            return
        servers = event.get("mcp_servers") or event.get("mcpServers")
        if isinstance(servers, dict):
            servers = [
                {"name": name, "status": status}
                if not isinstance(status, dict)
                else {"name": name, **status}
                for name, status in servers.items()
            ]
        if isinstance(servers, list):
            for server in servers:
                if not isinstance(server, dict):
                    continue
                name = server.get("name")
                status = server.get("status")
                if name in self.configured_servers and status is not None:
                    self.server_statuses[str(name)] = str(status)
        tools = event.get("tools")
        if isinstance(tools, list):
            names = {
                str(tool.get("name") if isinstance(tool, dict) else tool)
                for tool in tools
            }
            self.tools_discovered = any(
                any(
                    name.startswith(f"mcp__{server}__")
                    for server in self.configured_servers
                )
                for name in names
            )

    def _observe_tool_use(
        self, block: dict[str, Any], event: dict[str, Any], index: int
    ) -> None:
        full_name = str(block.get("name") or block.get("tool") or "")
        server = next(
            (
                name
                for name in self.configured_servers
                if full_name.startswith(f"mcp__{name}__")
            ),
            None,
        )
        if server is None:
            return
        call_id = str(block.get("id") or f"event-{index}-call-{len(self.interactions)}")
        prefix = f"mcp__{server}__"
        interaction = {
            "event_index": index,
            "timestamp": _event_timestamp(event),
            "server": server,
            "tool_name": full_name[len(prefix) :],
            "arguments": summarize_mcp_arguments(block.get("input") or {}),
            "success": None,
            "duration_seconds": _event_duration(event, block),
            "result_summary": None,
        }
        self.interactions.append(interaction)
        self._calls_by_id[call_id] = interaction

    def _observe_tool_result(
        self, block: dict[str, Any], event: dict[str, Any], index: int
    ) -> None:
        call_id = block.get("tool_use_id") or block.get("toolUseId")
        interaction = self._calls_by_id.get(str(call_id))
        if interaction is None:
            return
        is_error = block.get("is_error", block.get("isError"))
        interaction["success"] = None if is_error is None else not bool(is_error)
        if is_error is None:
            # Claude tool results normally omit is_error on success.
            interaction["success"] = True
        if interaction["duration_seconds"] is None:
            interaction["duration_seconds"] = _event_duration(event, block)
        if interaction["timestamp"] is None:
            interaction["timestamp"] = _event_timestamp(event)
        interaction["result_summary"] = _result_shape(block.get("content"))
        interaction["result_event_index"] = index


def write_mcp_evidence(path: Path, interactions: Iterable[dict[str, Any]]) -> None:
    lines = [json.dumps(item, sort_keys=True) for item in interactions]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _validate_executable(path: Path) -> None:
    if not path.is_file():
        raise PatchbenchError(f"MCP executable does not exist: {path}")
    if not os.access(path, os.X_OK):
        raise PatchbenchError(f"MCP command is not executable: {path}")


def _validate_landb_runtime(environment: dict[str, str]) -> None:
    if environment.get("LANDB_ENV", "").lower() == "prod":
        raise PatchbenchError("Benchmark MCP runs refuse LANDB_ENV=prod")
    urls = [
        environment.get(name)
        for name in ("LANDB_BASE_URL", "LANDB_V1_BASE_URL", "LANDB_BETA_BASE_URL")
        if environment.get(name)
    ]
    if not urls:
        raise PatchbenchError(
            "LanDB curated/broad MCP runs require an explicit base URL in --mcp-env-file"
        )
    for url in urls:
        parsed = urlsplit(str(url))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise PatchbenchError(f"Invalid LanDB MCP base URL: {url!r}")
        if parsed.hostname.lower() == _KNOWN_PRODUCTION_HOST:
            raise PatchbenchError("Benchmark MCP runs refuse the production LanDB host")
    auth_type = environment.get("LANDB_AUTH_TYPE")
    if auth_type not in {None, "none", "bearer", "basic"}:
        raise PatchbenchError("LANDB_AUTH_TYPE must be none, bearer, or basic")
    if auth_type == "bearer" and not environment.get("LANDB_TOKEN"):
        raise PatchbenchError("LANDB_TOKEN is required for bearer MCP authentication")
    if auth_type == "basic" and not all(
        environment.get(name)
        for name in ("LANDB_BASIC_USERNAME", "LANDB_BASIC_PASSWORD")
    ):
        raise PatchbenchError(
            "LANDB_BASIC_USERNAME and LANDB_BASIC_PASSWORD are required for basic MCP authentication"
        )


def _content_blocks(event: dict[str, Any]) -> list[dict[str, Any]]:
    content: Any = event.get("content")
    message = event.get("message")
    if content is None and isinstance(message, dict):
        content = message.get("content")
    if not isinstance(content, list):
        content = (
            [event] if event.get("type") in {"tool_use", "tool", "tool_result"} else []
        )
    return [block for block in content if isinstance(block, dict)]


def _event_timestamp(event: dict[str, Any]) -> str | float | int | None:
    value = event.get("timestamp") or event.get("created_at") or event.get("createdAt")
    return value if isinstance(value, (str, float, int)) else None


def _event_duration(event: dict[str, Any], block: dict[str, Any]) -> float | None:
    for source in (block, event):
        for key in ("duration_seconds", "durationSeconds"):
            value = source.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
        milliseconds = source.get("duration_ms", source.get("durationMs"))
        if isinstance(milliseconds, (int, float)) and not isinstance(
            milliseconds, bool
        ):
            return float(milliseconds) / 1000
    return None


def _result_shape(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return {"type": "text", "length": len(value)}
    if isinstance(value, list):
        return {"type": "content_blocks", "length": len(value)}
    if isinstance(value, dict):
        return {"type": "object", "keys": sorted(str(key) for key in value)[:20]}
    return {"type": type(value).__name__}


def _connected(status: str) -> bool:
    return status.lower() in {"connected", "ready", "running", "started"}
