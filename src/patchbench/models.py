from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


SetupCommand = tuple[str, ...]


REQUIRED_META_FIELDS = {
    "id",
    "repo_path",
    "base_commit",
    "fix_commit",
    "test_command",
    "problem_statement",
    "gold_patch",
    "test_patch",
}


@dataclass(frozen=True)
class CaseMetadata:
    id: str
    repo_path: Path
    base_commit: str
    fix_commit: str
    test_command: list[str]
    problem_statement: Path
    gold_patch: Path
    test_patch: Path | None
    workdir: str | None
    env: dict[str, str]
    setup_commands: list[SetupCommand]
    timeout_seconds: int
    relevant_files: list[str]
    case_dir: Path
    logs_dir: Path
    runs_dir: Path
    meta_path: Path
    image: str | None = None
    repository_id: str | None = None
    oracle_mode: str = "injected_test_patch"


@dataclass(frozen=True)
class CommandResult:
    command: list[str]
    cwd: str
    env_overrides: dict[str, str]
    exit_code: int
    duration_seconds: float
    stdout: str
    stderr: str
    timed_out: bool = False

    def to_log_text(self) -> str:
        env_text = " ".join(f"{key}={value}" for key, value in sorted(self.env_overrides.items()))
        return "\n".join(
            [
                f"command: {' '.join(self.command)}",
                f"cwd: {self.cwd}",
                f"env_overrides: {env_text}",
                f"exit_code: {self.exit_code}",
                f"duration_seconds: {self.duration_seconds:.6f}",
                f"timed_out: {str(self.timed_out).lower()}",
                "stdout:",
                self.stdout,
                "stderr:",
                self.stderr,
                "",
            ]
        )


@dataclass(frozen=True)
class ValidationResult:
    case_id: str
    base_commit: str
    test_patch_applies: bool
    base_test_exit_code: int | None
    base_test_passes: bool
    gold_patch_applies: bool
    gold_test_exit_code: int | None
    gold_test_passes: bool
    validated: bool
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EvaluationResult:
    case_id: str
    agent: str
    run_id: str
    prompt_mode: str
    patch_applied: bool
    resolved: bool
    f2p_passed: int
    f2p_total: int
    p2p_passed: int
    p2p_total: int
    runtime_seconds: float
    files_changed: list[str]
    lines_added: int
    lines_removed: int
    failure_mode: str
    error: str | None
    artifacts: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PatchComparison:
    agent_files_changed: list[str]
    gold_files_changed: list[str]
    overlapping_files: list[str]
    file_overlap_count: int
    file_overlap_jaccard: float
    exact_file_set_match: bool
    agent_lines_added: int
    agent_lines_removed: int
    gold_lines_added: int
    gold_lines_removed: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RunResult:
    """Result of running an agent - evaluation outcome after applying agent's patch."""

    case_id: str
    repo: str
    agent: str
    run_id: str
    prompt_mode: str
    agent_execution_mode: str
    evaluation_execution_mode: str
    agent_image: str | None
    agent_command_sanitized: str | None
    model: str | None
    model_source: str | None
    prompt_hash: str
    agent_timeout_seconds: int
    agent_harness: str | None
    agent_harness_version: str | None
    provider: str | None
    provider_base_url_host: str | None
    requested_model: str | None
    actual_model: str | None
    agent_max_turns: int | None
    agent_num_turns: int | None
    agent_activity_observed: bool
    agent_event_count: int
    agent_tool_event_count: int
    agent_first_event_seconds: float | None
    agent_last_event_seconds: float | None
    agent_seconds_since_last_event_at_exit: float | None
    agent_termination_reason: str | None
    agent_execution_detail: str | None
    agent_session_id: str | None
    agent_stop_reason: str | None
    agent_permission_denial_count: int
    agent_api_error_count: int
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    reported_cost_usd: float | None
    mcp_enabled: bool
    mcp_servers: list[str]
    mcp_condition: str
    mcp_server_name: str | None
    mcp_profile: str | None
    mcp_revision: str | None
    mcp_revision_dirty: bool | None
    mcp_process_started: bool | None
    mcp_server_statuses: dict[str, str]
    mcp_tools_discovered: bool | None
    mcp_activity_observed: bool
    mcp_tool_call_count: int
    mcp_tool_success_count: int
    mcp_tool_error_count: int
    mcp_telemetry_available: bool
    partial_patch_captured: bool
    evaluation_skipped: bool
    evaluation_skip_reason: str | None
    patch_comparison: PatchComparison
    patch_applied: bool  # Did the agent's patch apply successfully?
    resolved: bool  # Did tests pass after applying the patch?
    f2p_passed: int  # Failed tests that passed after fix (for test generation)
    f2p_total: int  # Total failed tests
    p2p_passed: int  # Passed tests that passed after fix (for test generation)
    p2p_total: int  # Total passed tests
    runtime_seconds: float
    files_changed: list[str]
    lines_added: int
    lines_removed: int
    failure_mode: str  # One of FailureMode constants
    error: str | None
    artifacts: dict[str, str]  # Paths to artifact files

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
