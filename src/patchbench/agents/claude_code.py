"""Observable host-side integration with the Claude Code CLI harness."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from patchbench.agents.base import AgentOutput
from patchbench.catalog import read_metadata
from patchbench.case import _resolve_case_artifact
from patchbench.exceptions import PatchbenchError
from patchbench.mcp import (
    McpEvidenceCollector,
    McpRunConfig,
    disabled_mcp_config,
    parse_literal_env_file,
    write_mcp_config,
    write_mcp_evidence,
)
from patchbench.runner import (
    is_secret_key,
    redact_secrets,
    run_command,
    sanitize_command,
)


_DEFAULT_MAX_TURNS = 100
_DEFAULT_HEARTBEAT_SECONDS = 30.0
_SHUTDOWN_GRACE_SECONDS = 2.0
_URL_PATTERN = re.compile(r"https?://[^\s\"']+", re.IGNORECASE)
_LOCAL_PATH_PATTERN = re.compile(
    r"/(?:Users|home|private/(?:tmp|var)|tmp|var)/[^\s\"'<>]+"
)
_AMBIENT_AGENT_ENV_NAMES = {
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TMPDIR",
    "LANG",
    "TERM",
    "COLORTERM",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    # Claude Code/provider runtime controls used by provider-compatible gateways.
    "API_TIMEOUT_MS",
    "SANDBOX",
}
_AGENT_ENV_PREFIXES = ("ANTHROPIC_", "CLAUDE_")
_TEST_AGENT_ENV_PREFIX = "FAKE_CLAUDE_"

_COMPLETION_PROTOCOL = """\
Completion protocol

This is an execution task: use tools to inspect and fix the checkout. An
introductory sentence or a promise to investigate is not a completed task.
Do not finish while an edit, missing import, or validation step remains pending.

Do not finish immediately after the first plausible edit. Understand the reported
problem and relevant implementation; reproduce it where practical; and inspect
nearby tests and conventions. Make the smallest complete fix, then run the most
relevant available tests. If a test fails, inspect the failure, revise the fix,
and rerun it. Inspect the final `git diff` for accidental, incomplete, or
unrelated changes, and run a broader regression check when feasible.

Finish only when relevant validation succeeds or testing is genuinely impossible
because of an environment limitation. In the latter case, state that limitation
clearly and do not claim unrun tests passed.
"""


def add_completion_protocol(task_prompt: str) -> str:
    """Append Claude-specific validation guidance to a benchmark-safe task."""
    return f"{task_prompt.rstrip()}\n\n---\n\n{_COMPLETION_PROTOCOL}"


@dataclass
class _EventState:
    session_id: str | None = None
    actual_model: str | None = None
    model_source: str | None = None
    num_turns: int | None = None
    observed_turns: int = 0
    last_assistant_message_id: str | None = None
    event_count: int = 0
    tool_event_count: int = 0
    first_event_seconds: float | None = None
    last_event_seconds: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    reported_cost_usd: float | None = None
    terminal_reason: str | None = None
    stop_reason: str | None = None
    permission_denial_count: int = 0
    api_error_count: int = 0
    last_progress_seconds: float | None = None
    summaries: list[str] = field(default_factory=list)
    mcp: McpEvidenceCollector = field(default_factory=McpEvidenceCollector)


@dataclass(frozen=True)
class _ProcessResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    termination_reason: str
    state: _EventState
    pid: int
    debug_lines: tuple[str, ...]


@dataclass(frozen=True)
class ClaudePromptOutput:
    """Answer-only Claude execution result with no repository patch lifecycle."""

    final_response: str
    events: tuple[dict[str, Any], ...]
    error: str | None
    timed_out: bool
    details: dict[str, Any]


class ClaudeCodeAgent:
    """Run Claude Code as a model-agnostic agent harness on the host or Podman."""

    name = "claude-code"
    supports_host_execution = True
    supports_container_execution = True
    agent_harness = "claude-code-cli"
    # Version solver images independently from the deterministic evaluator so
    # each benchmark result records the available developer tooling.
    image = "patchbench-rucio-claude:tests-v2"

    def __init__(
        self,
        env_file: str | Path | None = None,
        timeout_seconds: int = 300,
        live_output: bool = False,
        requested_model: str | None = None,
        max_turns: int = _DEFAULT_MAX_TURNS,
        debug: bool = False,
        heartbeat_seconds: float = _DEFAULT_HEARTBEAT_SECONDS,
        mcp_config: McpRunConfig | None = None,
        ca_file: str | Path | None = None,
        max_budget_usd: float | None = None,
    ) -> None:
        self.env_file = Path(env_file).expanduser().resolve() if env_file else None
        self.timeout_seconds = timeout_seconds
        self.live_output = live_output
        self.requested_model = requested_model
        self.max_turns = max_turns
        self.max_budget_usd = max_budget_usd
        self.debug = debug
        self.heartbeat_seconds = heartbeat_seconds
        self.mcp_config = mcp_config or disabled_mcp_config()
        self.ca_file = Path(ca_file).expanduser().resolve() if ca_file else None
        self.prompt_hash = ""
        self.model: str | None = None
        self.actual_model: str | None = None
        self.model_source: str | None = None
        self.agent_harness_version: str | None = None
        self.provider: str | None = None
        self.provider_base_url_host: str | None = None
        self.execution_details: dict[str, Any] = {}
        self.progress_callback: Callable[..., None] | None = None
        self.mcp_interactions: list[dict[str, Any]] = []
        self._container_live_state = self._new_event_state()

    @staticmethod
    def _read_issue(case_dir: Path) -> str:
        raw = read_metadata(case_dir)
        issue_path = _resolve_case_artifact(case_dir, raw["problem_statement"], "problem_statement")
        return issue_path.read_text(encoding="utf-8")

    def _agent_environment(self, *, isolated_home: Path | None = None) -> dict[str, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if key in _AMBIENT_AGENT_ENV_NAMES
            or key.startswith("LC_")
            or key.startswith(_AGENT_ENV_PREFIXES)
            or key.startswith(_TEST_AGENT_ENV_PREFIX)
        }
        if isolated_home is not None:
            isolated_home.mkdir(parents=True, exist_ok=True)
            env = {
                "PATH": os.environ.get("PATH", os.defpath),
                "HOME": str(isolated_home),
                "LANG": "C.UTF-8",
                "CLAUDE_CONFIG_DIR": str(isolated_home / ".claude"),
            }
        if self.env_file is not None:
            if not self.env_file.is_file():
                raise FileNotFoundError(f"Agent env file does not exist: {self.env_file}")
            configured = parse_literal_env_file(self.env_file, label="Claude")
            unsupported = sorted(
                key
                for key in configured
                if key not in _AMBIENT_AGENT_ENV_NAMES
                and not key.startswith("LC_")
                and not key.startswith(_AGENT_ENV_PREFIXES)
            )
            if unsupported:
                raise PatchbenchError(
                    "Unsupported Claude env variable(s): " + ", ".join(unsupported)
                )
            if isolated_home is not None:
                permitted = {
                    "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
                    "ANTHROPIC_API_URL", "API_TIMEOUT_MS", "SANDBOX",
                    "ANTHROPIC_MODEL", "ANTHROPIC_CUSTOM_MODEL_OPTION",
                    "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                    "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
                    "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "SSL_CERT_DIR",
                    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
                }
                if set(configured) - permitted:
                    raise PatchbenchError("Unsupported isolated agent environment variable")
            env.update(configured)

        if isolated_home is not None and self.ca_file is not None:
            env["NODE_EXTRA_CA_CERTS"] = str(self.ca_file)

        if self.requested_model is None:
            self.requested_model = env.get("ANTHROPIC_MODEL") or None
        self.provider_base_url_host = _sanitized_url_host(
            env.get("ANTHROPIC_BASE_URL") or env.get("ANTHROPIC_API_URL")
        )
        self.provider = _provider_name(self.provider_base_url_host)
        return env

    def container_environment(self) -> dict[str, str]:
        """Read provider configuration for redaction and container metadata.

        The resulting mapping is deliberately not passed to Podman. The
        container sources the same strictly literal assignment file so these
        values exactly match the redaction metadata.
        """
        if self.env_file is None:
            raise PatchbenchError("Containerized Claude Code requires an env file")
        values = parse_literal_env_file(self.env_file, label="Claude")
        if not any(is_secret_key(key) and value for key, value in values.items()):
            raise PatchbenchError(
                "Claude env file does not define a non-empty credential variable"
            )
        environment = self._agent_environment()
        environment.update(self.mcp_config.redaction_environment())
        return environment

    def container_command(self, *, system_prompt: str | None = None) -> str:
        """Return the fixed in-container Claude invocation.

        Credentials are loaded only by sourcing the read-only runtime mount;
        they never appear in the command line, image, or result artifacts.
        """
        command = [
            "claude",
            "-p",
            "--bare",
            "--permission-mode",
            "bypassPermissions",
            "--output-format",
            "stream-json",
            "--verbose",
            "--no-session-persistence",
            "--max-turns",
            str(self.max_turns),
        ]
        if self.requested_model:
            command.extend(["--model", self.requested_model])
        if self.max_budget_usd is not None:
            command.extend(['--max-budget-usd', str(self.max_budget_usd)])
        if self.debug:
            command.append("--debug")
        if self.mcp_config.enabled:
            command.extend(
                [
                    "--strict-mcp-config",
                    "--mcp-config",
                    "/run/patchbench/mcp.json",
                ]
            )
        if system_prompt:
            command.extend(["--append-system-prompt", system_prompt])
        rendered = " ".join(shlex.quote(part) for part in command)
        setup = "set -a; . /run/patchbench/claude-env.sh; set +a; "
        if self.ca_file is not None:
            setup += "export NODE_EXTRA_CA_CERTS=/run/patchbench/provider-ca.pem; "
        return setup + f'exec {rendered} < "$PATCHBENCH_PROMPT_FILE"'

    def container_execution_details(
        self,
        stdout: str,
        returncode: int,
        timed_out: bool,
        *,
        partial_patch: bool,
    ) -> dict[str, Any]:
        """Extract safe Claude observability fields from buffered container output."""
        state = self._new_event_state()
        for line in stdout.splitlines():
            self._observe_event(line, state, 0.0)
        termination_reason = (
            "wall_clock_timeout"
            if timed_out
            else _terminal_reason(state, returncode)
        )
        if termination_reason == "completed" and not partial_patch and state.tool_event_count == 0:
            termination_reason = "no_tool_activity"
        self.actual_model = state.actual_model
        self.model = self.actual_model
        self.model_source = state.model_source
        mcp_details = state.mcp.details()
        self.mcp_interactions = mcp_details.pop("mcp_interactions")
        return {
            "agent_harness": self.agent_harness,
            "agent_harness_version": None,
            "provider": self.provider,
            "provider_base_url_host": self.provider_base_url_host,
            "requested_model": self.requested_model,
            "actual_model": self.actual_model,
            "model": self.model,
            "model_source": self.model_source,
            "agent_max_turns": self.max_turns,
            "agent_num_turns": state.num_turns,
            "agent_activity_observed": state.event_count > 0,
            "agent_event_count": state.event_count,
            "agent_tool_event_count": state.tool_event_count,
            # Podman returns buffered output only after exit, so event timings
            # cannot be measured truthfully as they are for host execution.
            "agent_first_event_seconds": None,
            "agent_last_event_seconds": None,
            "agent_seconds_since_last_event_at_exit": None,
            "agent_termination_reason": termination_reason,
            "agent_execution_detail": _execution_detail(
                termination_reason, state.event_count > 0
            ),
            "agent_session_id": state.session_id,
            "agent_stop_reason": state.stop_reason,
            "agent_permission_denial_count": state.permission_denial_count,
            "agent_api_error_count": state.api_error_count,
            "input_tokens": state.input_tokens,
            "output_tokens": state.output_tokens,
            "cache_read_tokens": state.cache_read_tokens,
            "reported_cost_usd": state.reported_cost_usd,
            "partial_patch_captured": partial_patch
            and termination_reason in {"wall_clock_timeout", "max_turns", "incomplete_response"},
            "evaluation_skipped": termination_reason
            in {"wall_clock_timeout", "max_turns", "incomplete_response", "no_tool_activity"},
            "evaluation_skip_reason": _evaluation_skip_reason(termination_reason),
            "agent_command_sanitized": sanitize_command(self.container_command()),
            **self.mcp_config.metadata(),
            **mcp_details,
        }

    def observe_live_event(self, line: str, elapsed: float) -> None:
        """Update terminal progress from one already-redacted container event."""
        summary = self._observe_event(line, self._container_live_state, elapsed)
        self._report_progress(self._container_live_state, summary, elapsed)

    def run(
        self,
        case_dir: Path,
        solving_worktree: Path,
        base_commit: str,
        prompt_mode: str,
        prompt: str | None = None,
    ) -> AgentOutput:
        if self.max_turns <= 0:
            return AgentOutput(None, "Claude Code max turns must be greater than zero")
        try:
            prompt = prompt if prompt is not None else self._read_issue(case_dir)
            self.prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            env = self._agent_environment()
        except (OSError, UnicodeError, PatchbenchError) as exc:
            return AgentOutput(None, f"Failed to prepare Claude Code: {exc}")

        claude_path = shutil.which("claude")
        if claude_path is None:
            return AgentOutput(None, "Claude Code is not installed or not on PATH")

        self.agent_harness_version = self._detect_harness_version(claude_path, env)
        command = [
            claude_path,
            "-p",
            "--bare",
            "--permission-mode",
            "bypassPermissions",
            "--output-format",
            "stream-json",
            "--verbose",
            "--no-session-persistence",
            "--max-turns",
            str(self.max_turns),
        ]
        if self.requested_model:
            command.extend(["--model", self.requested_model])
        if self.debug:
            command.append("--debug")

        events_path = solving_worktree / "agent_events.jsonl"
        trajectory_path = solving_worktree / "trajectory.log"
        debug_path = solving_worktree / "agent_debug.log"
        events_path.write_text("", encoding="utf-8")
        if self.debug:
            debug_path.write_text("", encoding="utf-8")

        if self.mcp_config.enabled:
            with tempfile.TemporaryDirectory(prefix="patchbench-mcp-") as directory:
                config_path = Path(directory) / "mcp.json"
                write_mcp_config(config_path, self.mcp_config)
                result = self._run_claude(
                    command
                    + ["--strict-mcp-config", "--mcp-config", str(config_path)],
                    prompt,
                    solving_worktree,
                    env,
                    events_path=events_path,
                    debug_path=debug_path if self.debug else None,
                )
        else:
            result = self._run_claude(
                command,
                prompt,
                solving_worktree,
                env,
                events_path=events_path,
                debug_path=debug_path if self.debug else None,
            )
        self.actual_model = result.state.actual_model
        self.model = self.actual_model
        self.model_source = result.state.model_source

        run_command(["git", "add", "-N", "--all"], cwd=solving_worktree)
        diff = run_command(
            [
                "git",
                "diff",
                "HEAD",
                "--binary",
                "--",
                ".",
                ":(exclude)agent.patch",
                ":(exclude)trajectory.log",
                ":(exclude)agent_events.jsonl",
                ":(exclude)agent_debug.log",
            ],
            cwd=solving_worktree,
        )
        patch_text = redact_secrets(
            diff.stdout, self.mcp_config.redaction_environment()
        )
        if result.termination_reason == "completed" and not patch_text.strip() and result.state.tool_event_count == 0:
            result = replace(result, termination_reason="no_tool_activity")
        patch_path = solving_worktree / "agent.patch"
        if patch_text.strip():
            patch_path.write_text(patch_text, encoding="utf-8")
        partial = bool(patch_text.strip()) and result.termination_reason in {
            "wall_clock_timeout",
            "max_turns",
            "interrupted",
            "incomplete_response",
        }

        detail = _execution_detail(result.termination_reason, result.state.event_count > 0)
        seconds_since_last = (
            result.duration_seconds - result.state.last_event_seconds
            if result.state.last_event_seconds is not None
            else None
        )
        mcp_details = result.state.mcp.details()
        self.mcp_interactions = mcp_details.pop("mcp_interactions")
        write_mcp_evidence(
            solving_worktree / "mcp_activity.jsonl", self.mcp_interactions
        )
        self.execution_details = {
            "agent_harness": self.agent_harness,
            "agent_harness_version": self.agent_harness_version,
            "provider": self.provider,
            "provider_base_url_host": self.provider_base_url_host,
            "requested_model": self.requested_model,
            "actual_model": self.actual_model,
            "model": self.model,
            "model_source": self.model_source,
            "agent_max_turns": self.max_turns,
            "agent_num_turns": result.state.num_turns,
            "agent_activity_observed": result.state.event_count > 0,
            "agent_event_count": result.state.event_count,
            "agent_tool_event_count": result.state.tool_event_count,
            "agent_first_event_seconds": result.state.first_event_seconds,
            "agent_last_event_seconds": result.state.last_event_seconds,
            "agent_seconds_since_last_event_at_exit": seconds_since_last,
            "agent_termination_reason": result.termination_reason,
            "agent_execution_detail": detail,
            "input_tokens": result.state.input_tokens,
            "output_tokens": result.state.output_tokens,
            "cache_read_tokens": result.state.cache_read_tokens,
            "reported_cost_usd": result.state.reported_cost_usd,
            "partial_patch_captured": partial,
            "evaluation_skipped": result.termination_reason
            in {"wall_clock_timeout", "max_turns", "interrupted", "incomplete_response", "no_tool_activity"},
            "evaluation_skip_reason": _evaluation_skip_reason(
                result.termination_reason
            ),
            "agent_session_id": result.state.session_id,
            "agent_stop_reason": result.state.stop_reason,
            "agent_permission_denial_count": result.state.permission_denial_count,
            "agent_api_error_count": result.state.api_error_count,
            "agent_command_sanitized": " ".join(
                self._sanitized_command(result.command)
            ),
            **self.mcp_config.metadata(),
            **mcp_details,
        }
        self._write_trajectory(
            trajectory_path,
            result,
            env,
            patch_text,
            detail,
            partial,
        )

        if result.termination_reason == "wall_clock_timeout":
            return AgentOutput(
                patch_path if patch_text.strip() else None,
                f"Claude Code execution timed out after {self.timeout_seconds} seconds",
                timed_out=True,
                details=self.execution_details,
            )
        if result.termination_reason == "max_turns":
            return AgentOutput(
                patch_path if patch_text.strip() else None,
                f"Claude Code reached the {self.max_turns}-turn limit",
                details=self.execution_details,
            )
        if result.termination_reason == "interrupted":
            return AgentOutput(
                patch_path if patch_text.strip() else None,
                "Claude Code execution was interrupted",
                details=self.execution_details,
            )
        if incomplete_execution_error(result.termination_reason):
            return AgentOutput(
                patch_path if patch_text.strip() else None,
                incomplete_execution_error(result.termination_reason),
                details=self.execution_details,
            )
        if result.returncode != 0 or result.termination_reason == "cli_error":
            error_text = result.stderr or claude_result_error(result.stdout) or "unknown error"
            return AgentOutput(
                patch_path if patch_text.strip() else None,
                f"Claude Code failed with exit code {result.returncode}: {error_text}",
                details=self.execution_details,
            )
        if not patch_text.strip():
            return AgentOutput(
                None, "Claude Code produced no code changes", details=self.execution_details
            )
        return AgentOutput(patch_path, None, details=self.execution_details)

    def run_prompt(self, prompt: str, workspace: Path, *, isolated: bool = False,
                   output_schema: dict[str, Any] | None = None) -> ClaudePromptOutput:
        """Run one retrieval prompt while exposing only configured MCP tools.

        This path deliberately skips Git, patch capture, and evaluation. Built-in
        Claude tools are disabled, so the model can only answer directly or use
        tools supplied by the run-scoped MCP configuration.
        """
        if self.max_turns <= 0:
            return ClaudePromptOutput("", (), "max turns must be greater than zero", False, {})
        try:
            workspace = workspace.resolve()
            workspace.mkdir(parents=True, exist_ok=True)
            self.prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            if isolated and self.mcp_config.enabled:
                raise PatchbenchError("Isolated prompt execution cannot enable MCP")
            env = self._agent_environment(isolated_home=workspace / ".home") if isolated else self._agent_environment()
        except (OSError, UnicodeError, PatchbenchError) as exc:
            return ClaudePromptOutput("", (), f"Failed to prepare Claude Code: {exc}", False, {})

        claude_path = shutil.which("claude")
        if claude_path is None:
            return ClaudePromptOutput(
                "", (), "Claude Code is not installed or not on PATH", False, {}
            )
        self.agent_harness_version = self._detect_harness_version(claude_path, env)
        command = [
            claude_path,
            "-p",
            "--bare",
            "--permission-mode",
            "bypassPermissions",
            "--tools",
            "",
            "--output-format",
            "stream-json",
            "--verbose",
            "--no-session-persistence",
            "--max-turns",
            str(self.max_turns),
        ]
        if self.requested_model:
            command.extend(["--model", self.requested_model])
        if output_schema is not None:
            command.extend(["--json-schema", json.dumps(output_schema, ensure_ascii=False, allow_nan=False)])
        if self.max_budget_usd is not None:
            command.extend(['--max-budget-usd', str(self.max_budget_usd)])
        if isolated:
            command.extend(["--settings", "{}", "--setting-sources", ""])
        events_path = workspace / "agent_events.jsonl"
        if self.mcp_config.enabled or isolated:
            with tempfile.TemporaryDirectory(prefix="patchbench-mcp-") as directory:
                config_path = Path(directory) / "mcp.json"
                write_mcp_config(config_path, self.mcp_config)
                result = self._run_claude(
                    command + ["--strict-mcp-config", "--mcp-config", str(config_path)],
                    prompt,
                    workspace,
                    env,
                    events_path=events_path,
                )
        else:
            result = self._run_claude(command, prompt, workspace, env, events_path=events_path)

        events: list[dict[str, Any]] = []
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        final_event = next((event for event in reversed(events) if event.get("type") == "result"), {})
        output_format = "text"
        if output_schema is not None and "structured_output" in final_event:
            # Never replace malformed structured data with a plausible text result.
            final_response = json.dumps(final_event["structured_output"], ensure_ascii=False)
            output_format = "structured_output"
        else:
            final_response = final_event.get("result") if isinstance(final_event.get("result"), str) else ""
        mcp_details = result.state.mcp.details()
        self.mcp_interactions = mcp_details.pop("mcp_interactions")
        write_mcp_evidence(workspace / "mcp_activity.jsonl", self.mcp_interactions)
        details = {
            "agent_harness": self.agent_harness,
            "agent_harness_version": self.agent_harness_version,
            "provider": self.provider,
            "provider_base_url_host": self.provider_base_url_host,
            "requested_model": self.requested_model,
            "actual_model": result.state.actual_model,
            "agent_max_turns": self.max_turns,
            "agent_num_turns": result.state.num_turns,
            "agent_termination_reason": result.termination_reason,
            "agent_event_count": result.state.event_count,
            "agent_tool_event_count": result.state.tool_event_count,
            "input_tokens": result.state.input_tokens,
            "output_tokens": result.state.output_tokens,
            "reported_cost_usd": result.state.reported_cost_usd,
            "runtime_seconds": result.duration_seconds,
            "output_format": output_format,
            "agent_command_sanitized": " ".join(self._sanitized_command(result.command)),
            **self.mcp_config.metadata(),
            **mcp_details,
        }
        self.execution_details = details
        error: str | None = None
        if result.termination_reason == "wall_clock_timeout":
            error = f"Claude Code execution timed out after {self.timeout_seconds} seconds"
        elif result.termination_reason == "max_turns":
            error = f"Claude Code reached the {self.max_turns}-turn limit"
        elif incomplete_execution_error(result.termination_reason):
            error = incomplete_execution_error(result.termination_reason)
        elif result.returncode != 0 or result.termination_reason == "cli_error":
            error_text = result.stderr or claude_result_error(result.stdout) or "unknown error"
            error = f"Claude Code failed with exit code {result.returncode}: {error_text}"
        return ClaudePromptOutput(
            final_response=final_response,
            events=tuple(events),
            error=error,
            timed_out=result.termination_reason == "wall_clock_timeout",
            details=details,
        )

    def _detect_harness_version(
        self, claude_path: str, env: dict[str, str]
    ) -> str | None:
        if not Path(claude_path).is_file():
            return None
        try:
            completed = subprocess.run(
                [claude_path, "--version"],
                cwd=None,
                env=env,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        match = re.search(r"\b(\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?)\b", completed.stdout)
        return match.group(1) if match else None

    def _run_claude(
        self,
        command: list[str],
        prompt: str,
        cwd: Path,
        env: dict[str, str],
        *,
        events_path: Path | None = None,
        debug_path: Path | None = None,
    ) -> _ProcessResult:
        events_path = events_path or cwd / "agent_events.jsonl"
        events_path.write_text("", encoding="utf-8")
        state = self._new_event_state()
        sanitization_env = dict(env)
        sanitization_env.update(self.mcp_config.redaction_environment())
        started = time.monotonic()
        debug_lines: list[str] = []

        def debug(message: str) -> None:
            if debug_path is not None:
                debug_lines.append(f"[{time.monotonic() - started:.3f}s] {message}")

        debug(f"command start: {' '.join(self._sanitized_command(command))}")
        debug(f"prompt byte count: {len(prompt.encode('utf-8'))}")
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
        debug(f"PID: {process.pid}")
        assert process.stdin is not None

        def write_prompt() -> None:
            # A stalled CLI may never drain stdin. Keep prompt delivery off the
            # timeout/stream-observation thread so the wall-clock budget applies.
            try:
                process.stdin.write(prompt)
            except (BrokenPipeError, OSError):
                debug("stdin write failed: child closed the pipe")
            finally:
                try:
                    process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
                debug("stdin closed")

        prompt_writer = threading.Thread(target=write_prompt, daemon=True)
        prompt_writer.start()

        output_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()
        readers = [
            threading.Thread(
                target=self._read_stream,
                args=(stream, name, output_queue),
                daemon=True,
            )
            for name, stream in (("stdout", process.stdout), ("stderr", process.stderr))
            if stream is not None
        ]
        for reader in readers:
            reader.start()

        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        closed_streams = 0
        last_heartbeat = started
        termination_reason = "completed"
        interrupted = False
        with events_path.open("a", encoding="utf-8") as events_file:
            try:
                while True:
                    now = time.monotonic()
                    elapsed = now - started
                    if process.poll() is None and elapsed >= self.timeout_seconds:
                        termination_reason = "wall_clock_timeout"
                        debug("timeout signal: wall-clock limit reached")
                        self._terminate_process(process, debug)
                    try:
                        stream_name, line = output_queue.get(timeout=0.05)
                    except queue.Empty:
                        if self.progress_callback is not None and process.poll() is None:
                            self._report_progress(
                                state, "", elapsed, heartbeat=True
                            )
                        if self.live_output and process.poll() is None:
                            reference = (
                                started + state.last_event_seconds
                                if state.last_event_seconds is not None
                                else started
                            )
                            silent_for = now - reference
                            if (
                                silent_for >= self.heartbeat_seconds
                                and now - last_heartbeat >= self.heartbeat_seconds
                            ):
                                qualifier = (
                                    f"last event {silent_for:.1f}s ago"
                                    if state.last_event_seconds is not None
                                    else f"no event received for {silent_for:.1f}s"
                                )
                                self._live(elapsed, f"still running; {qualifier}")
                                last_heartbeat = now
                        if process.poll() is not None and closed_streams == len(readers):
                            break
                        continue

                    if line is None:
                        closed_streams += 1
                        if process.poll() is not None and closed_streams == len(readers):
                            break
                        continue
                    sanitized = (
                        _sanitize_event_line(line, sanitization_env, state.mcp)
                        if stream_name == "stdout"
                        else _sanitize_detailed_text(line, sanitization_env)
                    )
                    if stream_name == "stdout":
                        stdout_parts.append(sanitized)
                        events_file.write(sanitized)
                        if not sanitized.endswith("\n"):
                            events_file.write("\n")
                        events_file.flush()
                        summary = self._observe_event(sanitized, state, elapsed)
                        if state.event_count == 1:
                            debug("first event received")
                        if self.live_output and summary:
                            self._live(elapsed, summary)
                        if self.progress_callback is not None:
                            self._report_progress(state, summary, elapsed)
                    else:
                        stderr_parts.append(sanitized)
                        debug(f"stderr: {sanitized.strip()}")
                        if self.live_output and sanitized.strip():
                            self._live(elapsed, f"cli: {_truncate(sanitized.strip(), 160)}")
                        if self.progress_callback is not None and sanitized.strip():
                            self.progress_callback(
                                "analysis",
                                status="running",
                                last_activity=f"Claude CLI: {_truncate(sanitized.strip(), 160)}",
                            )
            except KeyboardInterrupt:
                interrupted = True
                termination_reason = "interrupted"
                debug("interrupt signal received")
                self._terminate_process(process, debug)
            finally:
                if process.poll() is None:
                    self._terminate_process(process, debug)
                returncode = process.wait()
                debug(f"process exit code: {returncode}")
                prompt_writer.join(timeout=1)
                for reader in readers:
                    reader.join(timeout=1)
                while not output_queue.empty():
                    stream_name, line = output_queue.get_nowait()
                    if line is None:
                        continue
                    sanitized = (
                        _sanitize_event_line(line, sanitization_env, state.mcp)
                        if stream_name == "stdout"
                        else _sanitize_detailed_text(line, sanitization_env)
                    )
                    if stream_name == "stdout":
                        stdout_parts.append(sanitized)
                        events_file.write(sanitized)
                        if not sanitized.endswith("\n"):
                            events_file.write("\n")
                        self._observe_event(
                            sanitized, state, time.monotonic() - started
                        )
                    else:
                        stderr_parts.append(sanitized)

        duration = time.monotonic() - started
        if termination_reason == "completed":
            termination_reason = _terminal_reason(state, process.returncode)
        if interrupted:
            termination_reason = "interrupted"
        if self.live_output:
            turns = f" turns={state.num_turns}" if state.num_turns is not None else ""
            self._live(duration, f"{termination_reason}{turns}")
        if debug_path is not None:
            debug_path.write_text("\n".join(debug_lines) + "\n", encoding="utf-8")
        return _ProcessResult(
            command=command,
            returncode=process.returncode,
            stdout="".join(stdout_parts),
            stderr="".join(stderr_parts),
            duration_seconds=duration,
            termination_reason=termination_reason,
            state=state,
            pid=process.pid,
            debug_lines=tuple(debug_lines),
        )

    def _new_event_state(self) -> _EventState:
        return _EventState(
            mcp=McpEvidenceCollector(self.mcp_config.server_names)
        )

    @staticmethod
    def _sanitized_command(command: list[str]) -> list[str]:
        sanitized: list[str] = []
        hide_next = False
        for index, part in enumerate(command):
            if hide_next:
                sanitized.append("<runtime-mcp-config>")
                hide_next = False
                continue
            rendered = "claude" if index == 0 and Path(part).name == "claude" else part
            sanitized.append(sanitize_command(rendered))
            hide_next = part == "--mcp-config"
        return sanitized

    def sanitize_event_stream(self, value: str, env: dict[str, str]) -> str:
        """Retain detailed events while removing credentials and secret keys."""
        return "".join(
            _sanitize_event_line(line, env)
            for line in value.splitlines(keepends=True)
        )

    def _observe_event(
        self, line: str, state: _EventState, elapsed: float
    ) -> str:
        if not line.strip():
            return ""
        state.event_count += 1
        if state.first_event_seconds is None:
            state.first_event_seconds = elapsed
        state.last_event_seconds = elapsed
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            return "unparsed output event"
        if not isinstance(event, dict):
            return "output event"
        state.mcp.observe(event)

        session_id = event.get("session_id") or event.get("sessionId")
        if session_id is not None:
            state.session_id = str(session_id)

        model_usage = event.get("modelUsage") or event.get("model_usage")
        if isinstance(model_usage, dict) and model_usage:
            models = [str(model) for model in model_usage if model]
            if models:
                state.actual_model = models[0]
                state.model_source = "claude-code-result"

        message = event.get("message")
        model = event.get("model")
        if isinstance(message, dict):
            model = message.get("model") or model
        if model and state.model_source != "claude-code-result":
            state.actual_model = str(model)
            state.model_source = "claude-code-stream"

        event_type = event.get("type")
        if event_type == "assistant":
            message_id = message.get("id") if isinstance(message, dict) else None
            if not message_id or message_id != state.last_assistant_message_id:
                state.observed_turns += 1
            state.last_assistant_message_id = message_id
            state.num_turns = state.observed_turns

        turns = event.get("num_turns") or event.get("numTurns")
        if isinstance(turns, int):
            state.num_turns = turns

        usage = event.get("usage")
        if not isinstance(usage, dict) and isinstance(message, dict):
            usage = message.get("usage")
        if isinstance(usage, dict):
            _record_usage(state, usage, authoritative=event.get("type") == "result")
        cost = event.get("total_cost_usd")
        if isinstance(cost, (int, float)):
            state.reported_cost_usd = float(cost)
        elif isinstance(model_usage, dict):
            model_costs = [
                value.get("costUSD", value.get("cost_usd"))
                for value in model_usage.values()
                if isinstance(value, dict)
            ]
            if model_costs and all(isinstance(value, (int, float)) for value in model_costs):
                state.reported_cost_usd = float(sum(model_costs))

        stop_reason = event.get("stop_reason") or event.get("stopReason")
        if stop_reason is None and isinstance(message, dict):
            stop_reason = message.get("stop_reason")
        if stop_reason is not None:
            state.stop_reason = str(stop_reason)

        denials = event.get("permission_denials") or event.get("permissionDenials")
        if isinstance(denials, list):
            state.permission_denial_count += len(denials)
            state.summaries.append(f"permission denials={len(denials)}")
        if event.get("type") in {"error", "api_error"} or event.get("is_error"):
            state.api_error_count += 1
            error = event.get("error") or event.get("message")
            if isinstance(error, str):
                state.summaries.append(f"error={_truncate(error, 160)}")

        subtype = str(event.get("subtype") or "")
        if event.get("type") == "result":
            if "max_turn" in subtype or "max_turn" in str(stop_reason or ""):
                state.terminal_reason = "max_turns"
            elif event.get("is_error") or subtype.startswith("error"):
                state.terminal_reason = "cli_error"
            elif stop_reason in {"tool_use", "length", "max_tokens"}:
                # A zero exit status does not make an unfinished model response
                # a valid completion. Keep the provider stop reason as evidence.
                state.terminal_reason = "incomplete_response"
            else:
                state.terminal_reason = "completed"

        tools = _tool_summaries(event)
        if tools:
            state.tool_event_count += len(tools)
            state.summaries.extend(tools)
            return tools[-1]
        if event_type == "system" and subtype == "init":
            return "session started"
        if event_type == "system" and subtype == "api_retry":
            attempt = event.get("attempt")
            maximum = event.get("max_retries")
            delay_ms = event.get("retry_delay_ms")
            retry = (
                f"{attempt} of {maximum}"
                if isinstance(attempt, int) and isinstance(maximum, int)
                else "scheduled"
            )
            delay = (
                f"; next attempt in {delay_ms / 1000:.1f}s"
                if isinstance(delay_ms, (int, float))
                else ""
            )
            return f"Provider connection retry {retry}{delay}"
        if state.actual_model and state.event_count <= 3:
            return f"model={state.actual_model}"
        if isinstance(turns, int):
            return f"turn={turns}"
        if event_type == "result":
            return state.terminal_reason or "result received"
        if event_type:
            return f"event={_truncate(str(event_type), 40)}"
        return "event received"

    def _report_progress(
        self,
        state: _EventState,
        summary: str,
        elapsed: float,
        *,
        heartbeat: bool = False,
    ) -> None:
        if self.progress_callback is None:
            return
        if heartbeat:
            if (
                state.last_progress_seconds is not None
                and elapsed - state.last_progress_seconds < self.heartbeat_seconds
            ):
                return
            summary = summary or "Waiting for model response"
        state.last_progress_seconds = elapsed
        editing_tools = ("tool=Edit", "tool=Write", "tool=MultiEdit", "tool=NotebookEdit")
        phase = "editing" if summary.startswith(editing_tools) else "analysis"
        self.progress_callback(
            phase,
            status="running",
            turns=state.num_turns,
            tool_calls=state.tool_event_count,
            last_activity=summary or None,
            heartbeat=heartbeat,
        )

    @staticmethod
    def _read_stream(
        stream, stream_name: str, output_queue: queue.Queue[tuple[str, str | None]]
    ) -> None:
        try:
            for line in iter(stream.readline, ""):
                output_queue.put((stream_name, line))
        finally:
            stream.close()
            output_queue.put((stream_name, None))

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str], debug) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            debug("terminate signal sent")
        except (OSError, ProcessLookupError):
            process.terminate()
            debug("terminate() called")
        try:
            process.wait(timeout=_SHUTDOWN_GRACE_SECONDS)
            debug("terminate result: exited")
            return
        except subprocess.TimeoutExpired:
            debug("terminate result: grace period expired")
        try:
            os.killpg(process.pid, signal.SIGKILL)
            debug("kill signal sent")
        except (OSError, ProcessLookupError):
            process.kill()
            debug("kill() called")
        process.wait()
        debug("kill result: exited")

    @staticmethod
    def _live(elapsed: float, message: str) -> None:
        print(f"[agent {elapsed:.1f}s] {message}", file=sys.stderr, flush=True)

    def _write_trajectory(
        self,
        path: Path,
        result: _ProcessResult,
        env: dict[str, str],
        patch: str,
        detail: str,
        partial: bool,
    ) -> None:
        state = result.state
        lines = [
            "=== Claude Code Agent Execution ===",
            f"prompt_hash: {self.prompt_hash}",
            f"agent_harness: {self.agent_harness}",
            f"agent_harness_version: {self.agent_harness_version or 'unknown'}",
            f"requested_model: {self.requested_model or 'none'}",
            f"actual_model: {self.actual_model or 'unknown'}",
            f"model_source: {self.model_source or 'unknown'}",
            f"provider: {self.provider or 'unknown'}",
            f"provider_base_url_host: {self.provider_base_url_host or 'unknown'}",
            "agent_execution_mode: host",
            "command: " + " ".join(self._sanitized_command(result.command)),
            f"exit_code: {result.returncode}",
            f"duration_seconds: {result.duration_seconds:.3f}",
            f"agent_timeout_seconds: {self.timeout_seconds}",
            f"agent_max_turns: {self.max_turns}",
            f"agent_num_turns: {state.num_turns}",
            f"agent_event_count: {state.event_count}",
            f"agent_tool_event_count: {state.tool_event_count}",
            f"agent_termination_reason: {result.termination_reason}",
            f"agent_execution_detail: {detail}",
            f"partial_patch_captured: {str(partial).lower()}",
            f"source_files_modified: {_patch_file_count(patch)}",
            "",
            "=== ACTIVITY ===",
            *(state.summaries or ["(no tool activity observed)"]),
            "",
            "=== STDOUT (STRUCTURED CLAUDE EVENTS) ===",
            result.stdout or "(empty)",
            "",
            "=== STDERR ===",
            _sanitize_detailed_text(result.stderr, env) or "(empty)",
            "",
            "=== AGENT PATCH (git diff) ===",
            patch or "(no changes)",
            "",
        ]
        if self.debug:
            lines.extend(
                [
                    "=== DEBUG LIFECYCLE ===",
                    *(result.debug_lines or ["(no lifecycle entries)"]),
                    "",
                ]
            )
        path.write_text("\n".join(lines), encoding="utf-8")


def _record_usage(state: _EventState, usage: dict[str, Any], *, authoritative: bool) -> None:
    fields = {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read_tokens": usage.get("cache_read_input_tokens", usage.get("cache_read_tokens")),
    }
    for name, value in fields.items():
        if not isinstance(value, int):
            continue
        current = getattr(state, name)
        setattr(state, name, value if authoritative or current is None else current + value)


def _tool_summaries(event: dict[str, Any]) -> list[str]:
    content: Any = event.get("content")
    message = event.get("message")
    if content is None and isinstance(message, dict):
        content = message.get("content")
    if not isinstance(content, list):
        content = [event] if event.get("type") in {"tool_use", "tool"} else []
    summaries: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") not in {"tool_use", "tool"}:
            continue
        name = str(block.get("name") or block.get("tool") or "unknown")
        inputs = block.get("input")
        target = ""
        if isinstance(inputs, dict):
            path = inputs.get("file_path") or inputs.get("path")
            command = inputs.get("command")
            pattern = inputs.get("pattern") or inputs.get("query")
            if path:
                target = (
                    " path=<redacted-after-mcp>"
                    if isinstance(path, dict)
                    else f" path={_sanitize_path(str(path))}"
                )
            elif command:
                target = (
                    " command=<redacted-after-mcp>"
                    if isinstance(command, dict)
                    else f' command="{_truncate(sanitize_command(str(command)), 120)}"'
                )
            elif pattern:
                target = f' target="{_truncate(str(pattern), 100)}"'
        summaries.append(f"tool={name}{target}")
    return summaries


def _terminal_reason(state: _EventState, returncode: int) -> str:
    if state.terminal_reason:
        return state.terminal_reason
    if state.stop_reason and "max_turn" in state.stop_reason:
        return "max_turns"
    return "completed" if returncode == 0 else "cli_error"


def _execution_detail(reason: str, activity: bool) -> str:
    if reason in {"incomplete_response", "no_tool_activity"}:
        return reason
    if reason == "wall_clock_timeout":
        return "timeout_after_activity" if activity else "timeout_no_activity"
    if reason == "max_turns":
        return "max_turns_after_activity" if activity else "max_turns_no_activity"
    if reason == "interrupted":
        return "interrupted"
    if reason == "cli_error":
        return "cli_error"
    return "completed"


def _evaluation_skip_reason(reason: str) -> str | None:
    return {
        "wall_clock_timeout": "agent_wall_clock_timeout",
        "max_turns": "agent_max_turns",
        "interrupted": "agent_interrupted",
        "incomplete_response": "agent_incomplete_response",
        "no_tool_activity": "agent_no_tool_activity",
    }.get(reason)


def incomplete_execution_error(reason: str) -> str | None:
    return {
        "incomplete_response": "Claude Code exited with an unfinished model response; preserve the partial patch and inspect the provider stop reason",
        "no_tool_activity": "Claude Code exited without using tools or making code changes",
    }.get(reason)


def _sanitized_url_host(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value if "://" in value else f"//{value}")
    if not parsed.hostname:
        return None
    try:
        port = parsed.port
    except ValueError:
        port = None
    return f"{parsed.hostname}:{port}" if port else parsed.hostname


def _provider_name(host: str | None) -> str | None:
    if host is None:
        return None
    hostname = host.split(":", 1)[0].lower()
    return "compatible-gateway" if hostname.endswith(".example.invalid") else "compatible-gateway"


def _sanitize_path(value: str) -> str:
    path = value.replace("\\", "/")
    if path.startswith("/"):
        return Path(path).name
    return _truncate(path, 140)


def _truncate(value: str, limit: int) -> str:
    value = value.replace("\n", " ").replace("\r", " ")
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _patch_file_count(patch: str) -> int:
    return sum(1 for line in patch.splitlines() if line.startswith("diff --git "))


def claude_result_error(stdout: str) -> str | None:
    """Extract the final CLI error from a stream-json transcript."""
    for line in reversed(stdout.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("is_error"):
            return str(event.get("error") or event.get("result") or "Claude Code CLI error")
    return None


def _sanitize_event_line(
    line: str,
    env: dict[str, str],
    mcp: McpEvidenceCollector | None = None,
) -> str:
    """Keep JSONL machine-readable while removing credentials and command secrets."""
    redacted = _sanitize_detailed_text(line, env)
    try:
        event = json.loads(redacted)
    except json.JSONDecodeError:
        return sanitize_command(redacted)

    def clean(value: Any, key: str = "") -> Any:
        metric_keys = {
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "inputtokens",
            "outputtokens",
            "cachereadinputtokens",
            "cachecreationinputtokens",
            "maxoutputtokens",
            "ephemeral_1h_input_tokens",
            "ephemeral_5m_input_tokens",
        }
        if key and key.lower() not in metric_keys and is_secret_key(key):
            return "<redacted>"
        if isinstance(value, dict):
            return {str(item_key): clean(item, str(item_key)) for item_key, item in value.items()}
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, str):
            value = _sanitize_detailed_text(value, env)
            return sanitize_command(value) if key.lower() in {"command", "cmd"} else value
        return value

    cleaned = clean(event)
    return json.dumps(cleaned, sort_keys=False) + "\n"


def _sanitize_detailed_text(value: str, env: dict[str, str]) -> str:
    """Retain diagnostic content while removing credentials from text and URLs."""
    redacted = redact_secrets(value, env)

    def replace_url(match: re.Match[str]) -> str:
        raw = match.group(0)
        trailing = ""
        while raw and raw[-1] in ".,;):":
            trailing = raw[-1] + trailing
            raw = raw[:-1]
        try:
            parsed = urlsplit(raw)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError:
            return "<redacted-url>" + trailing
        if hostname is None:
            return "<redacted-url>" + trailing
        host = f"[{hostname}]" if ":" in hostname else hostname
        if port is not None:
            host += f":{port}"
        query = urlencode(
            [
                (key, "<redacted>" if is_secret_key(key) else item)
                for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            ]
        )
        cleaned = urlunsplit(
            (parsed.scheme, host, parsed.path, query, parsed.fragment)
        )
        return cleaned + trailing

    return _URL_PATTERN.sub(replace_url, redacted)


def _sanitize_text(value: str, env: dict[str, str]) -> str:
    """Redact secrets and reduce URLs to a non-credentialed host."""
    redacted = redact_secrets(value, env)

    def replace_url(match: re.Match[str]) -> str:
        host = _sanitized_url_host(match.group(0))
        return f"<url-host:{host}>" if host else "<redacted-url>"

    redacted = _URL_PATTERN.sub(replace_url, redacted)
    return _LOCAL_PATH_PATTERN.sub("<local-path>", redacted)
