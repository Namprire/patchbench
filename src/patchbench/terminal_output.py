"""Human-friendly terminal progress and run summaries.

All human-facing output is written to stderr.  The CLI keeps its compact
technical result on stdout so existing scripts can continue to parse it.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping, TextIO

from patchbench.runner import redact_secrets


_SEPARATOR = "━" * 50
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_ASCII_SPINNER = "|/-\\"
_CI_KEYS = ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "BUILD_NUMBER")

# ANSI styling is deliberately centralized here. Rendering code deals only in
# semantic style names so color choices and non-color behavior stay consistent.
_STYLES = {
    "title": "\x1b[1;36m",
    "case_id": "\x1b[1;37m",
    "separator": "\x1b[2;36m",
    "label": "\x1b[2;37m",
    "value": "\x1b[37m",
    "active": "\x1b[1;92m",
    "duration": "\x1b[33m",
    "counter": "\x1b[36m",
    "success": "\x1b[32m",
    "pending": "\x1b[33m",
    "skipped": "\x1b[2;90m",
    "warning": "\x1b[33m",
    "error": "\x1b[31m",
    "heading_success": "\x1b[1;32m",
    "heading_warning": "\x1b[1;33m",
    "heading_error": "\x1b[1;31m",
}
_STYLE_RESET = "\x1b[0m"
_CURSOR_HIDE = "\x1b[?25l"
_CURSOR_SHOW = "\x1b[?25h"
_ERASE_LINE = "\r\x1b[2K"

_PHASE_LABELS = {
    "environment": "Preparing environment",
    "container": "Starting container",
    "agent": "Starting agent",
    "analysis": "Agent is analyzing the issue",
    "editing": "Agent is editing files",
    "patch": "Patch created",
    "tests": "Running tests",
    "evaluation": "Evaluating result",
    "completed": "Run completed",
}

_STATUS_SYMBOLS = {
    "running": None,
    "success": "✓",
    "failed": "✗",
    "warning": "!",
    "skipped": "–",
    "pending": "–",
}


def format_duration(seconds: float | int | None) -> str:
    total = max(0, int(seconds or 0))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def activity_bar(index: int, *, unicode_enabled: bool = True, width: int = 16) -> str:
    """Bounce a segment while total work is unknown; this is not a percentage."""
    segment = 4
    distance = width - segment
    offset = index % (distance * 2)
    offset = offset if offset <= distance else distance * 2 - offset
    active, empty = ("█", "─") if unicode_enabled else ("#", ".")
    return "[" + empty * offset + active * segment + empty * (width - offset - segment) + "]"


def _is_ci(environment: Mapping[str, str]) -> bool:
    return any(environment.get(key) for key in _CI_KEYS)


def _supports_color(
    stream: TextIO, environment: Mapping[str, str], *, tty: bool | None = None
) -> bool:
    is_tty = (
        bool(getattr(stream, "isatty", lambda: False)()) if tty is None else tty
    )
    return (
        is_tty
        and not _is_ci(environment)
        and "NO_COLOR" not in environment
        and environment.get("TERM", "").lower() != "dumb"
    )


def _style(text: str, name: str, enabled: bool) -> str:
    if not enabled or not text:
        return text
    return f"{_STYLES[name]}{text}{_STYLE_RESET}"


def _status_style(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {
        "running",
        "passed",
        "complete",
        "completed",
        "resolved",
        "successful",
        "all tests passed",
    }:
        return "success"
    if normalized in {
        "pending",
        "starting",
        "preparing",
        "waiting",
        "evaluating",
    }:
        return "pending"
    if normalized in {"failed", "error", "unresolved", "aborted"}:
        return "error"
    if normalized in {"skipped", "not run", "unavailable", "not used"}:
        return "skipped"
    return "value"


def _field_line(
    label: str,
    value: str,
    *,
    value_style: str = "value",
    color: bool,
    column: int = 20,
) -> str:
    spacing = " " * max(1, column - len(label))
    return (
        _style(label, "label", color)
        + spacing
        + _style(value, value_style, color)
    )


def _symbol(status: str, spinner_index: int, *, unicode_enabled: bool = True) -> str:
    fixed = _STATUS_SYMBOLS.get(status)
    if fixed is not None:
        if unicode_enabled:
            return fixed
        return {"✓": "+", "✗": "x", "–": "-"}.get(fixed, fixed)
    spinner = _SPINNER if unicode_enabled else _ASCII_SPINNER
    return spinner[spinner_index % len(spinner)]


def _supports_unicode(stream: TextIO) -> bool:
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return True
    try:
        "━⠋✓✗–".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def _terminal_width(stream: TextIO) -> int:
    try:
        return max(20, os.get_terminal_size(stream.fileno()).columns)
    except (AttributeError, OSError, ValueError):
        return 80


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _clip_ansi(text: str, width: int) -> str:
    visible = 0
    output: list[str] = []
    position = 0
    for match in _ANSI_ESCAPE.finditer(text):
        plain = text[position : match.start()]
        remaining = width - visible
        if len(plain) > remaining:
            output.append(plain[:remaining])
            return "".join(output) + _STYLE_RESET
        output.extend((plain, match.group(0)))
        visible += len(plain)
        position = match.end()
    remaining = width - visible
    output.append(text[position : position + max(0, remaining)])
    return "".join(output)


def _activity_text(value: str | None) -> str:
    if not value:
        return "No agent activity yet"
    match = re.match(r"tool=([^ ]+)(?: path=(.*))?$", value)
    if match:
        tool, path = match.groups()
        normalized = tool.lower()
        if normalized in {"read", "readfile"} and path:
            return f"Read file {path}"
        if normalized in {"edit", "write", "writefile", "multiedit"} and path:
            return f"Edited file {path}"
        if path:
            return f"{tool}: {path}"
        return f"Called tool {tool}"
    command = re.match(r'tool=([^ ]+) command="(.*)"$', value)
    if command:
        return f"Ran {command.group(1)}: {command.group(2)}"
    translations = {
        "session started": "Agent session started",
        "result received": "Agent result received",
        "completed": "Agent completed its work",
        "max_turns": "Maximum agent turns reached",
        "wall_clock_timeout": "Time limit reached",
        "cli_error": "Agent reported an internal error",
    }
    return translations.get(value, value)


class RunDisplay:
    """Render one in-place TTY dashboard or concise line-oriented progress."""

    def __init__(
        self,
        *,
        case_id: str,
        run_id: str,
        agent: str,
        max_turns: int | None,
        live: bool | None,
        stream: TextIO | None = None,
        environment: Mapping[str, str] | None = None,
        clock=time.monotonic,
    ) -> None:
        self.case_id = case_id
        self.run_id = run_id
        self.agent = agent
        self.max_turns = max_turns
        self.stream = stream or sys.stderr
        self.clock = clock
        self.started = clock()
        env = environment if environment is not None else os.environ
        tty = bool(getattr(self.stream, "isatty", lambda: False)())
        terminal_capable = (
            tty and not _is_ci(env) and env.get("TERM", "").lower() != "dumb"
        )
        self.dynamic = live is not False and terminal_capable
        self.color_enabled = live is not False and _supports_color(
            self.stream, env, tty=tty
        )
        self.unicode_enabled = _supports_unicode(self.stream)
        self.terminal_width = _terminal_width(self.stream)
        self.joiner = " · " if self.unicode_enabled else " | "
        self.phase = "environment"
        self.phase_status = "running"
        self.container_status = "Preparing" if agent else "Pending"
        self.turns: int | None = None
        self.tool_calls = 0
        self.last_activity: str | None = None
        self.tests_status = "Pending"
        self.evaluation_status = "Pending"
        self._spinner_index = 0
        self._rendered_lines = 0
        self._cursor_hidden = False
        self._closed = False
        self._last_line_signature: tuple[Any, ...] | None = None
        self._last_dynamic_render = 0.0
        self._line_header_written = False
        self._last_line_heartbeat = self.started
        self._render_lock = threading.RLock()
        self._animation_stop = threading.Event()
        self._animation_thread: threading.Thread | None = None

    def __enter__(self) -> "RunDisplay":
        self.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if exc is not None:
            self.update(
                "completed",
                status="failed",
                last_activity=f"Error: {exc}",
                evaluation_status="Aborted",
            )
        self.close()

    def start(self) -> None:
        self.update("environment", status="running")
        if self.dynamic and self._animation_thread is None and not self._closed:
            self._animation_thread = threading.Thread(target=self._animate, name="patchbench-run-progress", daemon=True)
            self._animation_thread.start()

    def _animate(self) -> None:
        while not self._animation_stop.wait(0.2):
            with self._render_lock:
                self.update(self.phase, heartbeat=True)

    def update(self, phase: str, **fields: Any) -> None:
        with self._render_lock:
            self._update(phase, **fields)

    def _update(self, phase: str, **fields: Any) -> None:
        if self._closed:
            return
        now = self.clock()
        if (
            self.dynamic
            and fields.get("heartbeat")
            and now - self._last_dynamic_render < 0.2
        ):
            return
        if phase in _PHASE_LABELS:
            self.phase = phase
        self.phase_status = str(fields.get("status", self.phase_status))
        if "container_status" in fields:
            self.container_status = str(fields["container_status"])
        if fields.get("turns") is not None:
            self.turns = int(fields["turns"])
        if fields.get("tool_calls") is not None:
            self.tool_calls = int(fields["tool_calls"])
        if fields.get("last_activity"):
            self.last_activity = _activity_text(
                redact_secrets(str(fields["last_activity"]))
            )
        if "tests_status" in fields:
            self.tests_status = str(fields["tests_status"])
        if "evaluation_status" in fields:
            self.evaluation_status = str(fields["evaluation_status"])
        self._spinner_index += 1
        if self.dynamic:
            self._render_dynamic()
            self._last_dynamic_render = now
        else:
            self._render_line(heartbeat=bool(fields.get("heartbeat")))

    def complete(self, result: Any) -> None:
        if result.evaluation_skipped:
            tests_status = "Not run"
            evaluation_status = "Skipped"
        elif result.resolved:
            tests_status = "All tests passed"
            evaluation_status = "Successful"
        elif result.failure_mode == "tests_failed":
            tests_status = "Failed"
            evaluation_status = "Completed"
        else:
            tests_status = self.tests_status
            evaluation_status = "Failed"
        status = "success" if result.resolved else (
            "warning"
            if result.evaluation_skipped
            else "failed"
        )
        self.update(
            "completed",
            status=status,
            turns=result.agent_num_turns,
            tool_calls=result.agent_tool_event_count,
            tests_status=tests_status,
            evaluation_status=evaluation_status,
            container_status=(
                "Completed"
                if result.agent_execution_mode == "container"
                else "Not used"
            ),
        )
        self.close()

    def close(self) -> None:
        self._animation_stop.set()
        if self._animation_thread is not None:
            self._animation_thread.join()
        with self._render_lock:
            self._close()

    def _close(self) -> None:
        if self._closed:
            return
        if self.dynamic and self._cursor_hidden:
            self.stream.write(_CURSOR_SHOW)
            self.stream.flush()
            self._cursor_hidden = False
        self._closed = True

    def _render_dynamic(self) -> None:
        if not self._cursor_hidden:
            self.stream.write(_CURSOR_HIDE)
            self._cursor_hidden = True
        elapsed = format_duration(self.clock() - self.started)
        turns = "–" if self.unicode_enabled else "-"
        if self.max_turns is not None:
            turns = f"{self.turns or 0} / {self.max_turns}"
        elif self.turns is not None:
            turns = str(self.turns)
        phase_text = f"{_symbol(self.phase_status, self._spinner_index, unicode_enabled=self.unicode_enabled)} {_PHASE_LABELS[self.phase]}"
        phase_spacing = " " * max(1, 38 - len(phase_text))
        phase_style = {
            "success": "success",
            "failed": "error",
            "warning": "warning",
            "skipped": "skipped",
        }.get(self.phase_status, "active")
        lines = [
            _style("PatchBench", "title", self.color_enabled)
            + self.joiner
            + _style(self.case_id, "case_id", self.color_enabled),
            _style(
                ("━" if self.unicode_enabled else "-")
                * min(50, self.terminal_width),
                "separator",
                self.color_enabled,
            ),
            _field_line("Run-ID:", self.run_id, color=self.color_enabled),
            _field_line("Agent:", self.agent, color=self.color_enabled),
            _field_line(
                "Container:",
                self.container_status,
                value_style=_status_style(self.container_status),
                color=self.color_enabled,
            ),
            _style(phase_text, phase_style, self.color_enabled)
            + phase_spacing
            + _style(elapsed, "duration", self.color_enabled),
            _style(activity_bar(self._spinner_index, unicode_enabled=self.unicode_enabled), phase_style, self.color_enabled)
            if self.phase_status == "running" else _style("Status: " + self.phase_status, phase_style, self.color_enabled),
            _field_line(
                "Agent turns:", turns, value_style="counter", color=self.color_enabled
            ),
            _field_line(
                "Tool calls:",
                str(self.tool_calls),
                value_style="counter",
                color=self.color_enabled,
            ),
            _field_line(
                "Last activity:",
                self.last_activity or "No agent activity yet",
                color=self.color_enabled,
            ),
            _field_line(
                "Tests:",
                self.tests_status,
                value_style=_status_style(self.tests_status),
                color=self.color_enabled,
            ),
            _field_line(
                "Evaluation:",
                self.evaluation_status,
                value_style=_status_style(self.evaluation_status),
                color=self.color_enabled,
            ),
            "",
            _field_line(
                "Total duration:",
                elapsed,
                value_style="duration",
                color=self.color_enabled,
            ),
        ]
        if self._rendered_lines:
            self.stream.write(f"\x1b[{self._rendered_lines}F")
        for line in lines:
            self.stream.write(
                f"{_ERASE_LINE}{_clip_ansi(line, self.terminal_width)}\n"
            )
        for _ in range(max(0, self._rendered_lines - len(lines))):
            self.stream.write(f"{_ERASE_LINE}\n")
        self.stream.flush()
        self._rendered_lines = len(lines)

    def _render_line(self, *, heartbeat: bool) -> None:
        if not self._line_header_written:
            self.stream.write(
                _style("PatchBench", "title", self.color_enabled)
                + " case="
                + _style(self.case_id, "case_id", self.color_enabled)
                + f" run_id={self.run_id} agent={self.agent}\n"
            )
            self._line_header_written = True
        if heartbeat:
            now = self.clock()
            if now - self._last_line_heartbeat < 30:
                return
            self._last_line_heartbeat = now
            elapsed = format_duration(now - self.started)
            active = (
                f"{(_SPINNER if self.unicode_enabled else _ASCII_SPINNER)[self._spinner_index % len(_SPINNER if self.unicode_enabled else _ASCII_SPINNER)]} "
                f"{_PHASE_LABELS[self.phase]}{self.joiner}still running"
            )
            self.stream.write(
                _style(f"[{elapsed}]", "duration", self.color_enabled)
                + " "
                + _style(active, "active", self.color_enabled)
                + "\n"
            )
            self.stream.flush()
            return
        signature = (
            self.phase,
            self.phase_status,
            self.turns,
            self.tool_calls,
            self.last_activity,
            self.tests_status,
            self.evaluation_status,
        )
        if signature == self._last_line_signature:
            return
        self._last_line_signature = signature
        elapsed = format_duration(self.clock() - self.started)
        symbol = _symbol(
            self.phase_status,
            self._spinner_index,
            unicode_enabled=self.unicode_enabled,
        )
        details: list[str] = []
        if self.turns is not None:
            maximum = f"/{self.max_turns}" if self.max_turns is not None else ""
            details.append(f"Turn {self.turns}{maximum}")
        if self.tool_calls:
            details.append(f"{self.tool_calls} tool calls")
        if self.last_activity:
            details.append(self.last_activity)
        if self.phase == "container" or self.container_status not in {
            "Pending",
            "Preparing",
            "Not used",
        }:
            details.append(f"Container: {self.container_status}")
        if self.tests_status != "Pending":
            details.append(f"Tests: {self.tests_status}")
        if self.evaluation_status != "Pending":
            details.append(f"Evaluation: {self.evaluation_status}")
        suffix = self.joiner + self.joiner.join(details) if details else ""
        phase_text = f"{symbol} {_PHASE_LABELS[self.phase]}"
        phase_style = {
            "success": "success",
            "failed": "error",
            "warning": "warning",
            "skipped": "skipped",
        }.get(self.phase_status, "active")
        self.stream.write(
            _style(f"[{elapsed}]", "duration", self.color_enabled)
            + " "
            + _style(phase_text, phase_style, self.color_enabled)
            + suffix
            + "\n"
        )
        self.stream.flush()


@dataclass(frozen=True)
class OutcomeText:
    title: str
    symbol: str
    tests: str
    message: tuple[str, ...]
    recommendations: tuple[str, ...] = ()


def outcome_text(result: Any) -> OutcomeText:
    reason = (result.agent_termination_reason or "").lower()
    failure_mode = (result.failure_mode or "").lower()
    error = (result.error or "").lower()

    if result.resolved or failure_mode == "tests_pass":
        return OutcomeText(
            "CASE SUCCESSFULLY RESOLVED",
            "✓",
            "All tests passed",
            ("The agent created a valid patch and the case is considered solved.",),
        )
    if reason == "max_turns" and result.evaluation_skipped:
        return OutcomeText(
            "RUN STOPPED EARLY",
            "!",
            "Not run" if result.evaluation_skipped else "Not passed",
            (
                "The agent reached the maximum number of allowed turns.",
                "No fully evaluable patch was produced, so evaluation was skipped."
                if result.evaluation_skipped
                else "The available patch could not be completed successfully.",
            ),
            (
                "Make the prompt or hints more specific",
                "Reduce context usage",
                "Increase the turn limit if needed",
            ),
        )
    if failure_mode == "timeout" or (reason == "wall_clock_timeout" and result.evaluation_skipped):
        return OutcomeText(
            "RUN TIMED OUT",
            "!",
            "Not run" if result.evaluation_skipped else "Not passed",
            ("The run ended because it exceeded the time limit.",),
            ("Review or increase the time limit", "Narrow the prompt and scope"),
        )
    if failure_mode == "tests_failed":
        return OutcomeText(
            "CASE NOT RESOLVED",
            "✗",
            "Failed",
            (
                "The agent created a patch, but at least one test failed.",
                "The patch is therefore not functionally correct yet.",
            ),
        )
    if reason == "container_error" or "podman" in error or error.startswith("container execution failed"):
        return OutcomeText(
            "RUN FAILED",
            "✗",
            "Not run",
            ("The container could not be started or executed correctly.",),
        )
    if reason in {"cli_error", "agent_error"}:
        if getattr(result, "agent_api_error_count", 0) or "api error:" in error:
            return OutcomeText(
                "RUN FAILED",
                "✗",
                "Not run",
                ("The model provider returned an error before the agent could complete.",),
            )
        return OutcomeText(
            "RUN FAILED",
            "✗",
            "Not run" if result.evaluation_skipped else "Not passed",
            ("The agent could not complete successfully because of an internal error.",),
        )
    if result.evaluation_skipped:
        return OutcomeText(
            "EVALUATION SKIPPED",
            "!",
            "Not run",
            ("Tests were not run; review the recorded termination reason and error details.",),
        )
    if failure_mode == "patch_failed":
        return OutcomeText(
            "PATCH COULD NOT BE EVALUATED",
            "✗",
            "Not run",
            ("The generated patch could not be applied correctly.",),
        )
    if failure_mode == "no_code_change":
        return OutcomeText(
            "CASE NOT RESOLVED",
            "✗",
            "Not run",
            ("The agent submitted no code changes; the case is scored as failed.",),
        )
    return OutcomeText(
        "RUN FAILED",
        "✗",
        "Unknown",
        (
            "The run could not be completed successfully.",
            "The technical details below can help identify the root cause.",
        ),
    )


def format_run_summary(
    result: Any, *, color: bool = False, unicode_enabled: bool = True
) -> str:
    outcome = outcome_text(result)
    symbol = outcome.symbol
    if not unicode_enabled:
        symbol = {"✓": "+", "✗": "x", "!": "!"}.get(symbol, symbol)
    severity = "success" if outcome.symbol == "✓" else (
        "warning" if outcome.symbol == "!" else "error"
    )
    heading_style = {
        "success": "heading_success",
        "warning": "heading_warning",
        "error": "heading_error",
    }[severity]
    turns = "–" if unicode_enabled else "-"
    if result.agent_num_turns is not None:
        turns = str(result.agent_num_turns)
        if result.agent_max_turns is not None:
            turns += f" / {result.agent_max_turns}"
    lines = [
        _style(_SEPARATOR if unicode_enabled else "-" * 50, "separator", color),
        _style(f"{symbol} {outcome.title}", heading_style, color),
        "",
        _field_line("Case:", result.case_id, color=color, column=15),
        _field_line("Run-ID:", result.run_id, color=color, column=15),
        _field_line("Agent:", result.agent, color=color, column=15),
        _field_line(
            "Duration:",
            format_duration(result.runtime_seconds),
            value_style="duration",
            color=color,
            column=15,
        ),
        _field_line(
            "Agent turns:", turns, value_style="counter", color=color, column=15
        ),
        _field_line(
            "Tests:",
            outcome.tests,
            value_style=_status_style(outcome.tests),
            color=color,
            column=15,
        ),
        "",
        *(_style(message, severity, color) for message in outcome.message),
    ]
    if outcome.recommendations:
        lines.extend(["", _style("Recommendations:", "label", color)])
        lines.extend(
            _style(f"- {recommendation}", "warning", color)
            for recommendation in outcome.recommendations
        )
    if result.error:
        lines.extend(
            [
                "",
                _style("Error details:", "label", color)
                + " "
                + _style(redact_secrets(str(result.error)), "error", color),
            ]
        )
    lines.extend(
        [
            _style(_SEPARATOR if unicode_enabled else "-" * 50, "separator", color),
            "",
            "Technical result:",
            *technical_result_lines(result),
        ]
    )
    return "\n".join(lines)


def technical_result_lines(result: Any) -> list[str]:
    lines = [f"resolved={result.resolved} failure_mode={result.failure_mode}"]
    if result.agent_termination_reason:
        lines.append(f"termination_reason={result.agent_termination_reason}")
    if result.evaluation_skipped:
        lines.append("evaluation_skipped=true")
    return lines
