"""Read-only, sanitized static-data export for the Research Console."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from textwrap import shorten
from typing import Any
from urllib.parse import urlsplit

from patchbench.agents.claude_code import claude_result_error
from patchbench.catalog import discover_cases, read_metadata, case_type as get_case_type, case_runs_dir, validation_path
from patchbench.exceptions import PatchbenchError
from patchbench.gitutils import patch_stats
from patchbench.io import write_json
from patchbench.report import normalize_run
from patchbench.runner import redact_secrets


SCHEMA_VERSION = 1
WORKFLOW_SCHEMA_VERSION = 1
DEFAULT_FRESHNESS_SECONDS = 60
_FORBIDDEN_KEYS = re.compile(
    r"(?:authorization|authentication|auth|token|api[_-]?key|private[_-]?key|password|secret|"
    r"credential|environment|env(?:iron)?|headers?)",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_ABSOLUTE_PATH = re.compile(r"/(?:home|tmp|var|opt|Users)/[^\s\"'<>]+")
_AUTH_VALUE = re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;}]+")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:TOKEN|API_KEY|PRIVATE_KEY|PASSWORD|SECRET|"
    r"CREDENTIAL|AUTHORIZATION|JWT)[A-Z0-9_]*)\s*[:=]\s*([^\s,;}]+)"
)
_SECRET_IDENTIFIER = re.compile(
    r"(?i)\b[A-Z0-9_]*(?:TOKEN|API_KEY|PRIVATE_KEY|PASSWORD|SECRET|"
    r"CREDENTIAL|AUTHORIZATION|JWT)[A-Z0-9_]*\b"
)


def export_dashboard(
    *,
    cases_dir: str | Path,
    runs_dir: str | Path,
    output: str | Path,
    include_gold: bool = False,
    freshness_seconds: int = DEFAULT_FRESHNESS_SECONDS,
) -> dict[str, Any]:
    """Export case and run evidence into deterministic, browser-safe JSON."""
    if freshness_seconds <= 0:
        raise PatchbenchError("Dashboard freshness window must be greater than zero")
    cases_root = Path(cases_dir).expanduser().resolve()
    historical_root = Path(runs_dir).expanduser().resolve()
    output_root = Path(output).expanduser().resolve()
    if not cases_root.is_dir():
        raise PatchbenchError(f"Cases directory does not exist: {cases_root}")

    catalog = discover_cases(cases_root)
    cases = [_read_case(path / "meta.yaml", include_gold) for path in catalog.values()]
    case_by_id = {case["case_id"]: case for case in cases}
    runs: list[dict[str, Any]] = []
    seen_runs: set[tuple[str, str]] = set()

    for case_id, directory in catalog.items():
        # New evidence takes precedence; historical case-local evidence is read-only.
        for runs_path in (case_runs_dir(directory), directory / "runs"):
            for run_dir in sorted(path for path in runs_path.glob("*") if path.is_dir()):
                run = _read_current_run(run_dir, case_by_id.get(case_id), freshness_seconds=freshness_seconds)
                key = (run["case_id"], run["run_id"])
                if key not in seen_runs:
                    runs.append(run)
                    seen_runs.add(key)

    if historical_root.is_dir():
        for run_dir in sorted(path for path in historical_root.glob("*/*") if path.is_dir()):
            run = _read_historical_run(
                run_dir,
                case_by_id.get(run_dir.parent.name),
                freshness_seconds=freshness_seconds,
            )
            key = (run["case_id"], run["run_id"])
            if key not in seen_runs:
                runs.append(run)
                seen_runs.add(key)

    runs.sort(key=_run_sort_key)
    for case in cases:
        case_runs = [run for run in runs if run["case_id"] == case["case_id"]]
        case["run_count"] = len(case_runs)
        case["run_ids"] = [run["run_id"] for run in case_runs]
        case["last_run_at"] = next(
            (run["created_at"] for run in reversed(case_runs) if run["created_at"]),
            None,
        )

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "case_count": len(cases),
        "run_count": len(runs),
        "includes_gold": include_gold,
    }
    dashboard = {
        **manifest,
        "cases": [_case_summary(case) for case in cases],
        "runs": [_run_summary(run) for run in runs],
    }

    payloads: dict[Path, Any] = {
        Path("manifest.json"): manifest,
        Path("dashboard.json"): dashboard,
    }
    for case in cases:
        payloads[Path("cases") / f"{_safe_segment(case['case_id'])}.json"] = case
    for run in runs:
        payloads[
            Path("runs")
            / _safe_segment(run["case_id"])
            / f"{_safe_segment(run['run_id'])}.json"
        ] = run

    _write_payloads(output_root, payloads)
    return manifest


def watch_dashboard(
    *,
    cases_dir: str | Path,
    runs_dir: str | Path,
    output: str | Path,
    include_gold: bool = False,
    interval_seconds: float = 2,
    freshness_seconds: int = DEFAULT_FRESHNESS_SECONDS,
) -> dict[str, Any]:
    """Continuously refresh the static export without executing benchmark work."""
    if interval_seconds <= 0:
        raise PatchbenchError("Dashboard watch interval must be greater than zero")
    if freshness_seconds <= 0:
        raise PatchbenchError("Dashboard freshness window must be greater than zero")
    manifest: dict[str, Any] = {}
    try:
        while True:
            manifest = export_dashboard(
                cases_dir=cases_dir,
                runs_dir=runs_dir,
                output=output,
                include_gold=include_gold,
                freshness_seconds=freshness_seconds,
            )
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        return manifest


def _read_case(meta_path: Path, include_gold: bool) -> dict[str, Any]:
    raw = read_metadata(meta_path.parent)

    case_dir = meta_path.parent
    case_id = _safe_segment(str(raw.get("id") or raw.get("case_id") or case_dir.name))
    hidden = {raw.get("test_patch"), raw.get("oracle")} - {None}
    issue_path = _safe_case_artifact(case_dir, raw.get("problem_statement"), hidden)
    issue_text = _read_text(issue_path)
    title = _issue_title(issue_text, case_id)
    validation = _read_optional_json(validation_path(case_dir))
    if not validation:
        validation = _read_optional_json(case_dir / "validation.json")
    provenance = _read_optional_json(case_dir / "provenance.json")
    gold_path = _safe_case_artifact(case_dir, raw.get("gold_patch"), hidden)
    gold_files, _, _ = patch_stats(gold_path) if gold_path else ([], 0, 0)
    repository = raw.get("repository") or raw.get("repo")
    if not repository:
        repo_path = str(raw.get("repo_path") or "")
        repository = Path(repo_path).name if repo_path else None

    case_type = get_case_type(raw)
    allowed_operations = (
        ["validate-case", "run-landb-answer", "verify-landb-dev"]
        if case_type == "landb_answer"
        else ["validate-case", "run", "evaluate-patch"]
    )

    human_reference = None
    if include_gold and gold_path and gold_path.is_file():
        human_reference = _sanitize_text(_read_text(gold_path))

    return {
        "schema_version": SCHEMA_VERSION,
        "case_id": case_id,
        "case_type": case_type,
        "allowed_operations": allowed_operations,
        "repository": _optional_text(repository),
        "issue_title": title,
        "issue_text": _sanitize_text(issue_text),
        "base_commit": _optional_text(raw.get("base_commit")),
        "fix_commit": _optional_text(raw.get("fix_commit") or raw.get("human_commit")),
        "validation_status": (
            bool(validation.get("validated")) if isinstance(validation, dict) else None
        ),
        "draft": bool(raw.get("draft", False)),
        "evaluation": {
            "test_command": _sanitize_text(str(raw.get("test_command") or "")) or None,
            "setup_command_count": len(raw.get("setup_commands") or []),
            "workdir": _safe_relative_value(raw.get("workdir")),
            "timeout_seconds": _optional_int(raw.get("timeout_seconds")),
        },
        "gold_files_changed": [_sanitize_text(path) for path in gold_files],
        "safe_provenance": _sanitize_value(provenance),
        "human_reference_patch": human_reference,
        "run_count": 0,
        "run_ids": [],
        "last_run_at": None,
    }


def _read_current_run(
    run_dir: Path,
    case: dict[str, Any] | None,
    *,
    freshness_seconds: int,
) -> dict[str, Any]:
    result_path = run_dir / "result.json"
    result = _read_optional_json(result_path)
    metadata = _read_optional_json(result_path.with_name("metadata.json"))
    try:
        normalized = normalize_run(result_path) if result else _minimal_normalized(result, metadata, run_dir)
    except PatchbenchError:
        normalized = _minimal_normalized(result, metadata, run_dir)
    nested_agent_details = result.get("agent_details")
    if not isinstance(nested_agent_details, dict):
        nested_agent_details = {}
    run_config = _read_optional_json(run_dir / "run.config.json")
    # GUI-launched runs persist their requested configuration separately from
    # observed result data. Use it as a lowest-precedence compatibility layer so
    # answer runs retain their condition and budgets after dashboard indexing.
    # Answer-benchmark results retain observed harness/model/token metadata under
    # agent_details; canonical result fields remain authoritative.
    merged = {**run_config, **metadata, **nested_agent_details, **result}
    artifacts = _read_artifacts(run_dir, normalized.get("artifacts", merged.get("artifacts", {})))
    view = _normalized_run_view(
        normalized=normalized,
        merged=merged,
        artifacts=artifacts,
        result_path=result_path,
        case=case,
        historical=False,
        freshness_seconds=freshness_seconds,
    )
    view["run_config"] = _sanitize_value(run_config) if run_config else None
    return view


def _read_historical_run(
    run_dir: Path,
    case: dict[str, Any] | None,
    *,
    freshness_seconds: int,
) -> dict[str, Any]:
    result_path = run_dir / "agent.result.json"
    raw = _read_optional_json(result_path)
    patch_applied = raw.get("patch_applied", raw.get("patch_applies"))
    normalized = {
        "case_id": raw.get("case_id") or result_path.parents[1].name,
        "run_id": run_dir.name,
        "agent": raw.get("agent"),
        "agent_harness": raw.get("agent_harness"),
        "agent_harness_version": raw.get("agent_harness_version"),
        "provider": raw.get("provider"),
        "requested_model": raw.get("requested_model"),
        "actual_model": raw.get("actual_model") or raw.get("model"),
        "model_source": raw.get("model_source"),
        "prompt_mode": raw.get("prompt_mode"),
        "agent_num_turns": raw.get("agent_num_turns"),
        "agent_activity_observed": raw.get("agent_activity_observed"),
        "agent_termination_reason": raw.get("agent_termination_reason"),
        "agent_execution_detail": raw.get("agent_execution_detail"),
        "partial_patch_captured": raw.get("partial_patch_captured"),
        "evaluation_skipped": raw.get("evaluation_skipped"),
        "patch_applied": patch_applied,
        "resolved": raw.get("resolved", raw.get("tests_pass")),
        "failure_mode": raw.get("failure_mode") or (
            ("tests_pass" if raw.get("tests_pass") else "tests_failed")
            if "tests_pass" in raw
            else None
        ),
        "runtime_seconds": raw.get("runtime_seconds", raw.get("duration_seconds")),
        "files_changed": raw.get("files_changed", raw.get("changed_files", [])),
        "lines_added": raw.get("lines_added", raw.get("added_lines")),
        "lines_removed": raw.get("lines_removed", raw.get("removed_lines")),
        "patch_comparison": raw.get("patch_comparison"),
    }
    artifacts = {
        "prompt": None,
        "answer": None,
        "agent_patch": _sanitize_text(_read_text(run_dir / "agent.patch")) or None,
        "trajectory": None,
        "test_log": _sanitize_text(_read_text(run_dir / "eval.log")) or None,
        "agent_events": None,
        "agent_debug": None,
        "mcp_activity": None,
        "human_reference_patch": case.get("human_reference_patch") if case else None,
    }
    return _normalized_run_view(
        normalized=normalized,
        merged=raw,
        artifacts=artifacts,
        result_path=result_path,
        case=case,
        historical=True,
        freshness_seconds=freshness_seconds,
    )


def _normalized_run_view(
    *,
    normalized: dict[str, Any],
    merged: dict[str, Any],
    artifacts: dict[str, Any],
    result_path: Path,
    case: dict[str, Any] | None,
    historical: bool,
    freshness_seconds: int,
) -> dict[str, Any]:
    patch_text = artifacts.get("agent_patch") or ""
    if case and case.get("human_reference_patch") is not None:
        artifacts["human_reference_patch"] = case["human_reference_patch"]
    patch_files, patch_added, patch_removed = _patch_stats_text(patch_text)
    files_changed = normalized.get("files_changed") or patch_files
    lines_added = normalized.get("lines_added")
    lines_removed = normalized.get("lines_removed")
    if patch_text and not lines_added and not lines_removed:
        lines_added, lines_removed = patch_added, patch_removed

    actual_model = _nullable_model(normalized.get("actual_model"))
    model_source = _optional_text(normalized.get("model_source"))
    patch_applied = _optional_bool(
        merged.get("patch_applied", merged.get("patch_applies", normalized.get("patch_applied")))
    )
    resolved = _first_bool(
        normalized.get("resolved"),
        merged.get("resolved"),
        merged.get("tests_pass"),
        merged.get("task_success"),
    )
    evaluation_skipped = _optional_bool(normalized.get("evaluation_skipped"))
    if evaluation_skipped:
        # A skipped evaluator did not establish either outcome. Persisted
        # RunResult booleans are legacy transport defaults, not evidence.
        patch_applied = None
        resolved = None
    partial = bool(normalized.get("partial_patch_captured", False))
    created_at = _first_text(
        merged.get("created_at"),
        merged.get("timestamp"),
        merged.get("started_at"),
        _path_modified_at(result_path),
    )

    view = {
        "schema_version": SCHEMA_VERSION,
        "case_id": _safe_segment(str(normalized.get("case_id") or result_path.parents[1].name)),
        "case_type": case.get("case_type", "coding") if case else "coding",
        "run_id": _safe_segment(str(normalized.get("run_id") or result_path.parent.name)),
        "created_at": _optional_text(created_at),
        "historical": historical,
        "agent": _optional_text(normalized.get("agent") or merged.get("agent")),
        "agent_harness": _optional_text(normalized.get("agent_harness")),
        "agent_harness_version": _optional_text(normalized.get("agent_harness_version")),
        "requested_model": _optional_text(
            normalized.get("requested_model") or merged.get("requested_model")
        ),
        "actual_model": actual_model,
        "model_source": model_source,
        "provider": _optional_text(normalized.get("provider")),
        "provider_host": _host_only(merged.get("provider_base_url_host")),
        "prompt_mode": _optional_text(
            normalized.get("prompt_mode") or merged.get("prompt_mode")
        ),
        "prompt_hash": _optional_text(merged.get("prompt_hash")),
        "available_relevant_files_count": _optional_int(
            merged.get("available_relevant_files_count", merged.get("relevant_files_count"))
        ),
        "included_relevant_files_count": _optional_int(
            merged.get("included_relevant_files_count")
        ),
        "configured_max_turns": _optional_int(merged.get("agent_max_turns")),
        "reported_turns": _optional_int(normalized.get("agent_num_turns")),
        "timeout_seconds": _optional_int(merged.get("agent_timeout_seconds")),
        "runtime_seconds": _optional_float(normalized.get("runtime_seconds")),
        "activity_observed": _optional_bool(
            normalized.get("agent_activity_observed", merged.get("agent_activity_observed"))
        ),
        "event_count": _optional_int(merged.get("agent_event_count")),
        "tool_event_count": _optional_int(merged.get("agent_tool_event_count")),
        "first_event_seconds": _optional_float(merged.get("agent_first_event_seconds")),
        "last_event_seconds": _optional_float(merged.get("agent_last_event_seconds")),
        "termination_reason": _optional_text(normalized.get("agent_termination_reason")),
        "execution_detail": _optional_text(normalized.get("agent_execution_detail")),
        "stop_reason": _optional_text(merged.get("agent_stop_reason")),
        "patch_captured": bool(patch_text.strip()),
        "partial_patch_captured": partial,
        "patch_applied": patch_applied,
        "evaluation_skipped": evaluation_skipped,
        "evaluation_skip_reason": _optional_text(merged.get("evaluation_skip_reason")),
        "mcp_enabled": bool(
            merged.get("mcp_enabled", merged.get("condition", "none") != "none")
        ),
        "mcp_servers": _sanitize_value(merged.get("mcp_servers", [])),
        "mcp_condition": _optional_text(
            merged.get("mcp_condition")
            or merged.get("condition")
            or merged.get("answer_condition")
        )
        or "none",
        "mcp_server_name": _optional_text(
            merged.get("mcp_server_name") or merged.get("mcp_server")
        ),
        "mcp_profile": _optional_text(
            merged.get("mcp_profile") or merged.get("condition")
        ),
        "mcp_revision": _optional_text(merged.get("mcp_revision")),
        "mcp_revision_dirty": _optional_bool(merged.get("mcp_revision_dirty")),
        "mcp_process_started": _optional_bool(merged.get("mcp_process_started")),
        "mcp_tools_discovered": _optional_bool(merged.get("mcp_tools_discovered")),
        "mcp_activity_observed": bool(
            merged.get(
                "mcp_activity_observed",
                (_optional_int(merged.get("mcp_tool_call_count")) or 0) > 0,
            )
        ),
        "mcp_tool_call_count": _optional_int(merged.get("mcp_tool_call_count")) or 0,
        "mcp_tool_success_count": _optional_int(merged.get("mcp_tool_success_count")) or 0,
        "mcp_tool_error_count": _optional_int(merged.get("mcp_tool_error_count")) or 0,
        "mcp_telemetry_available": bool(merged.get("mcp_telemetry_available", False)),
        "resolved": resolved,
        "failure_mode": _optional_text(normalized.get("failure_mode")),
        "files_changed": [_sanitize_text(str(path)) for path in files_changed],
        "lines_added": _optional_int(lines_added),
        "lines_removed": _optional_int(lines_removed),
        "patch_comparison": _sanitize_value(normalized.get("patch_comparison")),
        "input_tokens": _optional_int(merged.get("input_tokens")),
        "output_tokens": _optional_int(merged.get("output_tokens")),
        "cache_read_tokens": _optional_int(merged.get("cache_read_tokens")),
        "reported_cost_usd": _optional_float(merged.get("reported_cost_usd")),
        "artifacts": artifacts,
        "raw_sanitized_metadata": _sanitize_value(merged),
        "case_title": case.get("issue_title") if case else None,
    }
    view["workflow"] = _derive_workflow(
        run=view,
        merged=merged,
        artifacts=artifacts,
        result_path=result_path,
        case=case,
        historical=historical,
        freshness_seconds=freshness_seconds,
    )
    return view


def _derive_workflow(
    *,
    run: dict[str, Any],
    merged: dict[str, Any],
    artifacts: dict[str, Any],
    result_path: Path,
    case: dict[str, Any] | None,
    historical: bool,
    freshness_seconds: int,
) -> dict[str, Any]:
    """Build the stable workflow view once, at the compatibility boundary."""
    answer_run = run.get("case_type") == "landb_answer"
    run_dir = result_path.parent
    result_exists = result_path.is_file()
    timestamps = _run_timestamps(run_dir, result_path, result_exists)
    age_seconds = timestamps["last_activity_age_seconds"]
    incomplete = not result_exists
    reliably_timed = timestamps["last_activity_at"] is not None
    inferred_running = bool(
        incomplete
        and reliably_timed
        and age_seconds is not None
        and age_seconds <= freshness_seconds
    )
    inferred_stalled = bool(
        incomplete
        and reliably_timed
        and age_seconds is not None
        and age_seconds > freshness_seconds
    )

    diagnostics: list[dict[str, Any]] = []

    def diagnostic(
        severity: str,
        code: str,
        message: str,
        node_id: str,
        field: str,
        value: Any,
        inspection_target: str,
    ) -> None:
        diagnostics.append(
            {
                "severity": severity,
                "code": code,
                "message": message,
                "node_id": node_id,
                "evidence": {"field": field, "value": _sanitize_value(value)},
                "inspection_target": inspection_target,
            }
        )

    declared_missing = _declared_missing_artifacts(run_dir, merged.get("artifacts"))
    for artifact_name in declared_missing:
        diagnostic(
            "error" if artifact_name in {"prompt", "result"} else "warning",
            "ARTIFACT_MISSING",
            f"The declared {artifact_name} artifact is absent from this run.",
            "prompt" if artifact_name == "prompt" else "result",
            "artifacts",
            artifact_name,
            "Run artifacts",
        )

    raw_prompt = _read_declared_artifact(run_dir, merged.get("artifacts"), "prompt", "prompt.txt")
    prompt_hash = run.get("prompt_hash")
    prompt_hash_mismatch = bool(
        raw_prompt
        and prompt_hash
        and sha256(raw_prompt.encode("utf-8")).hexdigest() != prompt_hash
    )
    forbidden_prompt_reference = bool(
        re.search(r"(?i)\b(?:gold|test)\.patch\b|hidden[_ -]?tests?", raw_prompt)
    )
    if prompt_hash_mismatch:
        diagnostic(
            "error",
            "PROMPT_HASH_MISMATCH",
            "The persisted prompt hash does not match prompt.txt.",
            "prompt",
            "prompt_hash",
            prompt_hash,
            "Prompt artifact and metadata",
        )
    if raw_prompt and not prompt_hash:
        diagnostic(
            "warning",
            "PROMPT_HASH_MISSING",
            "A prompt artifact exists, but this result schema does not record its integrity hash.",
            "prompt",
            "prompt_hash",
            None,
            "Prompt artifact and raw normalized metadata",
        )
    if forbidden_prompt_reference:
        diagnostic(
            "error",
            "FORBIDDEN_ARTIFACT_LEAKAGE",
            "The prompt references an evaluation-only or hidden artifact.",
            "prompt",
            "prompt.txt",
            "forbidden reference detected",
            "Prompt artifact",
        )

    events = artifacts.get("agent_events")
    event_list = events if isinstance(events, list) else []
    malformed_events = sum(
        1 for event in event_list if isinstance(event, dict) and event.get("type") == "unparsed"
    )
    if malformed_events:
        diagnostic(
            "error",
            "MALFORMED_EVENT_DATA",
            f"{malformed_events} structured event line(s) could not be parsed.",
            "tools",
            "agent_events",
            malformed_events,
            "Agent Events tab",
        )

    tool_summary = _summarize_tool_events(event_list)
    if not event_list:
        if answer_run:
            tool_summary["event_count"] = run.get("mcp_tool_call_count")
            tool_summary["tool_event_count"] = run.get("mcp_tool_call_count")
            tool_summary["summary"] = (
                f"{run.get('mcp_tool_call_count') or 0} MCP retrieval call(s)"
            )
            tool_summary["observed_mcp_tools"] = _sanitize_value(
                merged.get("observed_mcp_tools", [])
            )
        else:
            tool_summary["event_count"] = run.get("event_count")
            tool_summary["tool_event_count"] = run.get("tool_event_count")
    tool_summary["first_event_seconds"] = run.get("first_event_seconds")
    tool_summary["last_event_seconds"] = run.get("last_event_seconds")
    termination = (run.get("termination_reason") or "").lower()
    execution_detail = (run.get("execution_detail") or "").lower()
    api_errors = _optional_int(merged.get("agent_api_error_count")) or 0
    permission_denials = _optional_int(merged.get("agent_permission_denial_count")) or 0
    if termination == "max_turns":
        diagnostic(
            "warning",
            "AGENT_MAX_TURNS",
            "The agent stopped at its configured interaction budget.",
            "termination",
            "agent_termination_reason",
            termination,
            "Trajectory and Agent Events tabs",
        )
    if "timeout" in termination or "timeout" in execution_detail:
        if run.get("activity_observed"):
            diagnostic(
                "warning",
                "AGENT_TIMEOUT_AFTER_ACTIVITY",
                "The wall-clock limit expired after agent activity was observed.",
                "termination",
                "agent_execution_detail",
                execution_detail or termination,
                "Trajectory and Agent Events tabs",
            )
        else:
            diagnostic(
                "error",
                "AGENT_TIMEOUT_NO_ACTIVITY",
                "The wall-clock limit expired without observable agent activity.",
                "agent",
                "agent_activity_observed",
                run.get("activity_observed"),
                "Agent debug and trajectory artifacts",
            )
    if permission_denials and "permission" in termination:
        diagnostic(
            "error",
            "AGENT_PERMISSION_FAILURE",
            "Permission denial terminated the agent process.",
            "agent",
            "agent_permission_denial_count",
            permission_denials,
            "Agent Events and Debug tabs",
        )
    if api_errors:
        recovered = termination not in {"api_error", "cli_error"} and "api" not in termination
        recorded_error = str(merged.get("error") or "")
        provider_error = claude_result_error(recorded_error)
        if provider_error is None and "API Error:" in recorded_error:
            provider_error = recorded_error.split("API Error:", 1)[1].splitlines()[0]
        provider_error = (
            shorten(_sanitize_text(provider_error), width=320, placeholder="…")
            if provider_error
            else None
        )
        diagnostic(
            "warning" if recovered else "error",
            "AGENT_API_ERROR_RECOVERED" if recovered else "AGENT_API_ERROR",
            (
                "API errors were observed, but the run continued to another terminal state."
                if recovered
                else (
                    f"Model provider request failed: {provider_error}"
                    if provider_error
                    else "An API error terminated the agent process."
                )
            ),
            "agent",
            "agent_api_error_count",
            api_errors,
            "Agent Events and Debug tabs",
        )

    if run.get("partial_patch_captured"):
        diagnostic(
            "warning",
            "PARTIAL_PATCH_CAPTURED",
            "A partial patch was preserved when agent execution stopped.",
            "patch",
            "partial_patch_captured",
            True,
            "Agent Patch tab",
        )
        if run.get("evaluation_skipped"):
            diagnostic(
                "warning",
                "PARTIAL_PATCH_NOT_EVALUATED",
                "The captured partial patch was not evaluated automatically.",
                "evaluation",
                "evaluation_skip_reason",
                run.get("evaluation_skip_reason"),
                "Agent Patch tab or explicit evaluate-patch",
            )
    if run.get("evaluation_skipped"):
        diagnostic(
            "warning",
            "EVALUATION_SKIPPED",
            "Deterministic evaluation was explicitly skipped for this run.",
            "evaluation",
            "evaluation_skip_reason",
            run.get("evaluation_skip_reason"),
            "Test Log tab",
        )
    if run.get("configured_max_turns") is not None and run.get("reported_turns") is not None:
        if run["configured_max_turns"] != run["reported_turns"]:
            diagnostic(
                "warning",
                "TURN_COUNT_DISCREPANCY",
                "Configured and upstream-reported turn counts differ; both values are retained.",
                "agent",
                "agent_num_turns",
                run.get("reported_turns"),
                "Raw normalized metadata",
            )

    patch_text = artifacts.get("agent_patch") or ""
    patch_files, patch_added, patch_removed = _patch_stats_text(patch_text)
    persisted_files = merged.get("files_changed")
    persisted_added = _optional_int(merged.get("lines_added"))
    persisted_removed = _optional_int(merged.get("lines_removed"))
    stats_inconsistent = bool(
        patch_text
        and (
            (isinstance(persisted_files, list) and sorted(map(str, persisted_files)) != sorted(patch_files))
            or (persisted_added is not None and persisted_added != patch_added)
            or (persisted_removed is not None and persisted_removed != patch_removed)
        )
    )
    if stats_inconsistent:
        diagnostic(
            "warning",
            "PATCH_STATS_INCONSISTENT",
            "Persisted top-level patch statistics differ from the captured patch; exported statistics use the patch.",
            "patch",
            "files_changed/lines_added/lines_removed",
            "inconsistent",
            "Agent Patch and Raw Metadata tabs",
        )

    evaluation_known = any(
        key in merged
        for key in (
            "patch_applied",
            "patch_applies",
            "resolved",
            "tests_pass",
            "failure_mode",
            "evaluation_skipped",
            "task_success",
            "answer_correct",
        )
    )
    tests_failed = bool(
        evaluation_known
        and not run.get("evaluation_skipped")
        and run.get("resolved") is False
        and run.get("patch_applied") is not False
    )
    if tests_failed:
        diagnostic(
            "info",
            "ANSWER_EVALUATION_FAILED" if answer_run else "EVALUATION_TESTS_FAILED",
            (
                "Answer evaluation completed, but one or more scoring checks did not pass."
                if answer_run
                else "Deterministic evaluation completed, but the task tests did not pass."
            ),
            "evaluation",
            "task_success" if answer_run else "failure_mode",
            merged.get("task_success") if answer_run else run.get("failure_mode"),
            "Final Answer and Raw tabs" if answer_run else "Test Log tab",
        )
    patch_rejected = bool(
        evaluation_known
        and run.get("patch_captured")
        and run.get("patch_applied") is False
        and not run.get("evaluation_skipped")
    )
    if patch_rejected:
        diagnostic(
            "error",
            "PATCH_APPLICATION_FAILED",
            "The candidate patch could not be applied for deterministic evaluation.",
            "evaluation",
            "patch_applied",
            False,
            "Test Log tab",
        )
    failure_mode = str(run.get("failure_mode") or "").lower()
    if failure_mode in {
        "environment_error", "environment_failure", "container_error", "setup_error",
        "evaluation_error", "evaluation_crash",
    }:
        diagnostic(
            "error",
            "EVALUATION_ENVIRONMENT_FAILURE",
            "Deterministic evaluation did not complete because its execution environment failed.",
            "evaluation",
            "failure_mode",
            failure_mode,
            "Test Log tab",
        )
    run_error = str(merged.get("error") or "").lower()
    if incomplete and any(term in run_error for term in ("worktree", "checkout", "clone")):
        diagnostic(
            "error",
            "WORKTREE_SETUP_FAILED",
            "The isolated solver checkout could not be prepared.",
            "worktree",
            "error",
            "checkout/setup failure",
            "Trajectory and raw normalized metadata",
        )

    if historical and (not termination or not evaluation_known):
        diagnostic(
            "warning",
            "HISTORICAL_STATE_UNKNOWN",
            "This historical run does not contain enough lifecycle data to prove every stage.",
            "result",
            "historical",
            True,
            "Raw normalized metadata",
        )
    if historical and str(merged.get("model") or "").lower() == "claude-code-default":
        diagnostic(
            "warning",
            "HISTORICAL_MODEL_AMBIGUOUS",
            "The historical model label identifies a harness default, not a verified underlying model.",
            "agent",
            "model",
            "claude-code-default",
            "Raw normalized metadata",
        )
    if inferred_stalled:
        diagnostic(
            "warning",
            "STALE_INCOMPLETE_RUN",
            "The run has no final result and its exported artifacts have not changed within the freshness window.",
            "agent",
            "last_activity_age_seconds",
            round(age_seconds or 0, 3),
            "Refresh the static export and inspect trajectory/events",
        )

    agent_error = bool(
        termination in {"api_error", "cli_error", "process_error", "permission_failure"}
        or "permission" in termination
        or "api_error" in execution_detail
    )
    agent_warning = bool(
        termination in {"max_turns", "interrupted", "wall_clock_timeout", "timeout"}
        or "timeout" in termination
        or "timeout" in execution_detail
    )

    source_status = "success" if case and case.get("safe_provenance") else "unavailable"
    case_status = "success" if case else "unavailable"
    worktree_failed = any(item["code"] == "WORKTREE_SETUP_FAILED" for item in diagnostics)
    worktree_status = (
        "error"
        if worktree_failed
        else "running"
        if inferred_running
        else "success"
        if result_exists or raw_prompt or merged.get("agent")
        else "unavailable"
    )
    prompt_status = (
        "error"
        if prompt_hash_mismatch or forbidden_prompt_reference or "prompt" in declared_missing
        else "success"
        if raw_prompt and prompt_hash
        else "warning"
        if raw_prompt
        else "warning"
        if historical and prompt_hash
        else "unavailable"
    )
    agent_status = (
        "running"
        if inferred_running
        else "error"
        if agent_error or ("timeout" in (termination + execution_detail) and not run.get("activity_observed"))
        else "warning"
        if agent_warning or inferred_stalled
        else "success"
        if result_exists and termination in {"completed", ""}
        else "unavailable"
    )
    tools_status = (
        "error"
        if malformed_events
        else "running"
        if inferred_running and (run.get("activity_observed") or event_list)
        else "warning"
        if event_list and agent_status in {"warning", "error"}
        else "success"
        if event_list or run.get("activity_observed")
        else "skipped"
        if result_exists
        else "unavailable"
    )
    patch_status = (
        "success"
        if answer_run and artifacts.get("answer")
        else "error"
        if answer_run and result_exists
        else "warning"
        if run.get("partial_patch_captured")
        else "success"
        if run.get("patch_captured")
        else "skipped"
        if result_exists
        else "unavailable"
    )
    termination_status = (
        "running"
        if inferred_running
        else "error"
        if agent_error or ("timeout" in (termination + execution_detail) and not run.get("activity_observed"))
        else "warning"
        if agent_warning or inferred_stalled
        else "success"
        if termination == "completed" or (result_exists and not termination)
        else "unavailable"
    )
    evaluation_status = (
        "skipped"
        if run.get("evaluation_skipped")
        else "error"
        if patch_rejected or tests_failed or run.get("resolved") is False
        else "success"
        if run.get("resolved") is True
        else "unavailable"
    )
    result_status = (
        "success"
        if run.get("resolved") is True
        else "error"
        if evaluation_known and run.get("resolved") is False and not run.get("evaluation_skipped")
        else "skipped"
        if run.get("evaluation_skipped")
        else "unavailable"
    )

    task_outcome = (
        "resolved"
        if run.get("resolved") is True
        else "not_evaluated"
        if run.get("evaluation_skipped")
        else "unknown"
        if historical and not evaluation_known
        else "diagnostic"
        if incomplete
        else "unresolved"
        if run.get("resolved") is False
        else "not_evaluated"
        if not evaluation_known
        else "unknown"
    )
    if inferred_running:
        health, health_source = "running", "derived"
    elif historical and (not termination or not evaluation_known):
        health, health_source = "unknown", "historical_fallback"
    elif inferred_stalled:
        health, health_source = "stalled", "derived"
    elif any(item["severity"] == "error" for item in diagnostics):
        health, health_source = "error", (
            "historical_fallback" if historical else "authoritative" if result_exists else "derived"
        )
    elif (
        agent_warning
        or run.get("evaluation_skipped")
        or run.get("partial_patch_captured")
        or any(item["severity"] == "warning" for item in diagnostics)
    ):
        health, health_source = "degraded", (
            "historical_fallback" if historical else "authoritative" if result_exists else "derived"
        )
    else:
        # A deterministic tests_failed outcome is scientifically unsuccessful but infrastructurally healthy.
        health, health_source = "healthy", (
            "historical_fallback" if historical else "authoritative" if result_exists else "derived"
        )

    nodes = [
        _workflow_node(
            "source", "source", "Case Source / Provenance", source_status,
            "Safe case provenance is available." if source_status == "success" else "No reliable provenance is available.",
            "Issue and merge-request provenance used before the case was frozen.",
            {"repository": case.get("repository") if case else None},
            [], [],
        ),
        _workflow_node(
            "case", "case", "Frozen Case", case_status,
            "Frozen metadata loaded." if case_status == "success" else "Case metadata is unavailable.",
            f"{run['case_id']} · {(case or {}).get('repository') or 'repository unknown'}",
            {"base_commit": ((case or {}).get("base_commit") or "")[:12] or None},
            ["case_metadata"], _codes_for_node(diagnostics, "case"),
        ),
        _workflow_node(
            "worktree", "worktree", "Retrieval Environment" if answer_run else "Solver Worktree", worktree_status,
            (
                "The read-only retrieval environment was prepared."
                if answer_run and worktree_status in {"success", "running"}
                else "Retrieval environment evidence is unavailable."
                if answer_run
                else "Solving checkout evidence is present."
                if worktree_status in {"success", "running"}
                else "Historical data cannot prove worktree creation."
            ),
            (
                f"{run.get('mcp_condition') or 'none'} profile · {'live DEV' if merged.get('live_dev') else 'deterministic fake backend'}"
                if answer_run
                else "Isolated checkout at the case base commit."
            ),
            {
                "execution_mode": _optional_text(merged.get("agent_execution_mode")),
                **(
                    {
                        "condition": run.get("mcp_condition"),
                        "backend": merged.get("backend"),
                        "read_only": merged.get("read_only"),
                    }
                    if answer_run
                    else {}
                ),
            },
            [], _codes_for_node(diagnostics, "worktree"),
        ),
        _workflow_node(
            "prompt", "prompt", "Prompt Context", prompt_status,
            "Prompt artifact and hash are consistent." if prompt_status == "success" else "Prompt evidence is incomplete or inconsistent.",
            (
                f"answer task · {run.get('mcp_condition') or 'none'} retrieval condition"
                if answer_run
                else f"{run.get('prompt_mode') or 'mode unknown'} · {run.get('included_relevant_files_count') or 0} hint(s) included"
            ),
            {
                "prompt_mode": run.get("prompt_mode"),
                "available_hints": run.get("available_relevant_files_count"),
                "included_hints": run.get("included_relevant_files_count"),
            },
            ["prompt"] if raw_prompt else [], _codes_for_node(diagnostics, "prompt"),
        ),
        _workflow_node(
            "agent", "agent", "Agent Harness", agent_status,
            _agent_status_reason(agent_status, termination, inferred_running),
            f"{run.get('actual_model') or run.get('agent') or 'agent unknown'} through {run.get('agent_harness') or run.get('agent') or 'harness unknown'}",
            {
                "agent": run.get("agent"),
                "harness": run.get("agent_harness"),
                "harness_version": run.get("agent_harness_version"),
                "requested_model": run.get("requested_model"),
                "actual_model": run.get("actual_model"),
                "provider": run.get("provider"),
                "provider_host": run.get("provider_host"),
                "configured_max_turns": run.get("configured_max_turns"),
                "reported_turns": run.get("reported_turns"),
                "timeout_seconds": run.get("timeout_seconds"),
                "input_tokens": run.get("input_tokens"),
                "output_tokens": run.get("output_tokens"),
                "reported_cost_usd": run.get("reported_cost_usd"),
                "first_event_seconds": run.get("first_event_seconds"),
                "last_event_seconds": run.get("last_event_seconds"),
                "api_error_count": api_errors,
                "permission_denial_count": permission_denials,
            },
            [name for name in ("trajectory", "agent_events", "agent_debug", "mcp_activity") if artifacts.get(name) is not None],
            _codes_for_node(diagnostics, "agent"),
        ),
        _workflow_node(
            "tools", "tools", "LanDB MCP Retrieval" if answer_run else "Agent Tools", tools_status,
            (
                "Read-only MCP retrieval activity summarized."
                if answer_run and run.get("mcp_tool_call_count")
                else "No MCP retrieval calls were observed."
                if answer_run
                else "Structured agent activity summarized."
                if event_list
                else "No structured tool activity is available."
            ),
            tool_summary["summary"],
            tool_summary,
            ["agent_events"] if event_list else [], _codes_for_node(diagnostics, "tools"),
        ),
        _workflow_node(
            "patch", "patch", "Final Answer" if answer_run else "Patch Capture", patch_status,
            (
                "The final answer artifact was captured."
                if answer_run and artifacts.get("answer")
                else "The run completed without a final answer artifact."
                if answer_run
                else "Partial patch preserved."
                if run.get("partial_patch_captured")
                else "Patch captured."
                if run.get("patch_captured")
                else "No patch was produced."
            ),
            (
                f"format {'valid' if merged.get('answer_format_correct') else 'invalid'} · read-only {'yes' if merged.get('read_only') else 'no'}"
                if answer_run
                else f"{len(run.get('files_changed') or [])} file(s) · +{run.get('lines_added') or 0} / -{run.get('lines_removed') or 0}"
            ),
            (
                {
                    "answer_format_correct": merged.get("answer_format_correct"),
                    "read_only": merged.get("read_only"),
                }
                if answer_run
                else {
                    "patch_captured": run.get("patch_captured"),
                    "partial_patch_captured": run.get("partial_patch_captured"),
                    "files_changed": run.get("files_changed"),
                    "lines_added": run.get("lines_added"),
                    "lines_removed": run.get("lines_removed"),
                }
            ),
            ["answer"] if answer_run and artifacts.get("answer") is not None else ["agent_patch"] if artifacts.get("agent_patch") is not None else [], _codes_for_node(diagnostics, "patch"),
        ),
        _workflow_node(
            "termination", "termination", "Termination", termination_status,
            termination or "No authoritative termination reason is available.",
            f"{termination or 'unknown'} · {run.get('reported_turns') if run.get('reported_turns') is not None else '?'} reported turn(s)",
            {
                "termination_reason": run.get("termination_reason"),
                "execution_detail": run.get("execution_detail"),
                "stop_reason": run.get("stop_reason"),
                "configured_max_turns": run.get("configured_max_turns"),
                "reported_turns": run.get("reported_turns"),
            },
            ["trajectory"] if artifacts.get("trajectory") is not None else [], _codes_for_node(diagnostics, "termination"),
        ),
        _workflow_node(
            "evaluation", "evaluation", "Answer Evaluation" if answer_run else "Deterministic Evaluation", evaluation_status,
            (
                "Answer content, JSON format, tool selection, HTTP request, and read-only behavior were scored."
                if answer_run
                else _evaluation_status_reason(run, evaluation_known)
            ),
            (
                f"answer {'correct' if merged.get('answer_correct') else 'incorrect'} · task {'passed' if merged.get('task_success') else 'failed'}"
                if answer_run
                else f"{merged.get('evaluation_execution_mode') or 'mode unknown'} · {run.get('failure_mode') or 'outcome unknown'}"
            ),
            {
                "execution_mode": _optional_text(merged.get("evaluation_execution_mode")),
                "evaluation_skipped": run.get("evaluation_skipped"),
                "evaluation_skip_reason": run.get("evaluation_skip_reason"),
                "patch_applied": run.get("patch_applied"),
                "f2p_passed": _optional_int(merged.get("f2p_passed")),
                "f2p_total": _optional_int(merged.get("f2p_total")),
                "p2p_passed": _optional_int(merged.get("p2p_passed")),
                "p2p_total": _optional_int(merged.get("p2p_total")),
                "resolved": run.get("resolved"),
                "failure_mode": run.get("failure_mode"),
                **(
                    {
                        "answer_correct": merged.get("answer_correct"),
                        "answer_format_correct": merged.get("answer_format_correct"),
                        "tool_selection_correct": merged.get("tool_selection_correct"),
                        "http_request_correct": merged.get("http_request_correct"),
                        "read_only": merged.get("read_only"),
                        "condition_valid": merged.get("condition_valid"),
                    }
                    if answer_run
                    else {}
                ),
            },
            ["answer"] if answer_run and artifacts.get("answer") is not None else ["test_log"] if artifacts.get("test_log") is not None else [], _codes_for_node(diagnostics, "evaluation"),
        ),
        _workflow_node(
            "result", "result", "Result Artifacts", result_status,
            f"Scientific outcome: {task_outcome}.",
            f"{task_outcome.replace('_', ' ')} · {len([value for value in artifacts.values() if value is not None])} safe artifact(s)",
            {"task_outcome": task_outcome, "resolved": run.get("resolved"), "failure_mode": run.get("failure_mode")},
            sorted(name for name, value in artifacts.items() if value is not None), _codes_for_node(diagnostics, "result"),
        ),
    ]

    posthoc = merged.get("posthoc_evaluation") or merged.get("post_hoc_evaluation")
    if isinstance(posthoc, dict):
        posthoc_resolved = _optional_bool(posthoc.get("resolved"))
        nodes.append(
            _workflow_node(
                "posthoc", "posthoc", "Post-hoc Evaluation",
                "success" if posthoc_resolved else "error" if posthoc_resolved is False else "unavailable",
                "Persisted explicit post-hoc evaluation outcome.",
                "Resolved" if posthoc_resolved else "Unresolved" if posthoc_resolved is False else "Outcome unknown",
                _sanitize_value(posthoc), [], _codes_for_node(diagnostics, "posthoc"),
            )
        )

    for node in nodes:
        node["started_at"] = timestamps["started_at"] if node["id"] in {"worktree", "prompt", "agent"} else None
        node["completed_at"] = timestamps["completed_at"] if node["status"] not in {"running", "queued", "unavailable"} else None
        node["duration_seconds"] = run.get("runtime_seconds") if node["id"] == "agent" else None

    edges = _workflow_edges(nodes)
    diagnostics.sort(key=lambda item: (item["severity"], item["code"], item["node_id"]))
    return {
        "schema_version": WORKFLOW_SCHEMA_VERSION,
        "health": health,
        "health_source": health_source,
        "task_outcome": task_outcome,
        "started_at": timestamps["started_at"],
        "last_activity_at": timestamps["last_activity_at"],
        "last_activity_age_seconds": timestamps["last_activity_age_seconds"],
        "completed_at": timestamps["completed_at"],
        "freshness_seconds": freshness_seconds,
        "nodes": nodes,
        "edges": edges,
        "diagnostics": diagnostics,
    }


def _workflow_node(
    node_id: str,
    kind: str,
    label: str,
    status: str,
    status_reason: str,
    summary: str,
    metrics: dict[str, Any],
    artifact_refs: list[str],
    diagnostic_codes: list[str],
) -> dict[str, Any]:
    return {
        "id": node_id,
        "kind": kind,
        "label": label,
        "status": status,
        "status_reason": status_reason,
        "started_at": None,
        "completed_at": None,
        "duration_seconds": None,
        "summary": _sanitize_text(summary),
        "metrics": _sanitize_value(metrics),
        "artifact_refs": artifact_refs,
        "diagnostic_codes": diagnostic_codes,
    }


def _workflow_edges(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    for source, target in zip(nodes, nodes[1:]):
        target_status = target["status"]
        status = {
            "running": "active",
            "success": "completed",
            "warning": "warning",
            "error": "error",
            "skipped": "skipped",
            "unavailable": "inactive",
            "queued": "inactive",
            "initial": "inactive",
        }.get(target_status, "inactive")
        edges.append(
            {
                "id": f"{source['id']}-to-{target['id']}",
                "source": source["id"],
                "target": target["id"],
                "status": status,
                "label": None,
                "animated": status == "active",
            }
        )
    return edges


def _codes_for_node(diagnostics: list[dict[str, Any]], node_id: str) -> list[str]:
    return [item["code"] for item in diagnostics if item["node_id"] == node_id]


def _run_timestamps(run_dir: Path, result_path: Path, result_exists: bool) -> dict[str, Any]:
    candidates: list[float] = []
    for name in (
        "metadata.json",
        "prompt.txt",
        "trajectory.log",
        "agent_events.jsonl",
        "mcp_activity.jsonl",
        "agent.patch",
        "test.log",
        result_path.name,
    ):
        path = run_dir / name
        try:
            candidates.append(path.stat().st_mtime)
        except OSError:
            continue
    if not candidates:
        return {
            "started_at": None,
            "last_activity_at": None,
            "last_activity_age_seconds": None,
            "completed_at": None,
        }
    earliest, latest = min(candidates), max(candidates)
    completed = None
    if result_exists:
        try:
            completed = result_path.stat().st_mtime
        except OSError:
            completed = latest
    return {
        "started_at": _timestamp_iso(earliest),
        "last_activity_at": _timestamp_iso(latest),
        "last_activity_age_seconds": max(0.0, time.time() - latest),
        "completed_at": _timestamp_iso(completed) if completed is not None else None,
    }


def _timestamp_iso(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")


def _declared_missing_artifacts(run_dir: Path, declared: Any) -> list[str]:
    if not isinstance(declared, dict):
        return []
    missing: list[str] = []
    for name, value in sorted(declared.items()):
        if not isinstance(value, str) or name not in {
            "prompt", "agent_patch", "trajectory", "test_log", "agent_events", "agent_debug", "mcp_activity", "result"
        }:
            continue
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts or not (run_dir / relative).is_file():
            missing.append(name)
    return missing


def _read_declared_artifact(
    run_dir: Path,
    declared: Any,
    name: str,
    fallback: str,
) -> str:
    value = declared.get(name, fallback) if isinstance(declared, dict) else fallback
    if not isinstance(value, str):
        return ""
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        relative = Path(fallback)
    return _read_text(run_dir / relative)


def _summarize_tool_events(events: list[Any]) -> dict[str, Any]:
    counts = {"Read": 0, "Grep": 0, "Bash": 0, "Edit": 0, "Write": 0, "other": 0}
    significant: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "unknown")
        if event_type == "system" and event.get("subtype") == "init":
            significant.append({"index": index, "category": "session_started", "label": "Session started"})
        if event_type == "result":
            significant.append(
                {
                    "index": index,
                    "category": str(event.get("terminal_reason") or event.get("subtype") or "completion"),
                    "label": _sanitize_text(str(event.get("terminal_reason") or event.get("subtype") or "Completed")),
                }
            )
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "tool_use":
                continue
            name = str(item.get("name") or "other")
            bucket = name if name in counts else "other"
            counts[bucket] += 1
            category = {
                "Read": "file_read",
                "Grep": "search",
                "Bash": "command",
                "Edit": "edit",
                "Write": "write",
            }.get(name, "tool")
            significant.append(
                {"index": index, "category": category, "label": _sanitize_text(name)}
            )
    latest = significant[-1] if significant else None
    total = sum(counts.values())
    active = ", ".join(f"{name} {count}" for name, count in counts.items() if count)
    return {
        "event_count": len(events),
        "tool_event_count": total,
        "counts": counts,
        "read_search_count": counts["Read"] + counts["Grep"],
        "command_count": counts["Bash"],
        "edit_count": counts["Edit"] + counts["Write"],
        "latest_event": latest,
        "recent_significant_events": significant[-20:],
        "summary": active or (f"{len(events)} event(s), no tool calls identified" if events else "No structured events"),
    }


def _agent_status_reason(status: str, termination: str, inferred_running: bool) -> str:
    if inferred_running:
        return "Recent artifact activity suggests the run may still be active; process liveness is not proven."
    if termination:
        return f"Agent termination: {termination}."
    if status == "success":
        return "Agent execution completed."
    return "Agent lifecycle state is unavailable."


def _evaluation_status_reason(run: dict[str, Any], evaluation_known: bool) -> str:
    if run.get("evaluation_skipped"):
        return f"Evaluation skipped: {run.get('evaluation_skip_reason') or 'reason unavailable'}."
    if run.get("resolved") is True:
        return "The candidate patch applied and deterministic tests passed."
    if run.get("patch_applied") is False and not run.get("patch_captured"):
        return "No candidate patch was produced; this is a scientific no-code-change outcome."
    if run.get("patch_applied") is False:
        return "The candidate patch did not apply for deterministic evaluation."
    if run.get("resolved") is False and evaluation_known:
        return "Deterministic evaluation completed without resolving the task."
    return "Evaluation state is unavailable in this result schema."


def _read_artifacts(run_dir: Path, declared: Any) -> dict[str, Any]:
    names = declared if isinstance(declared, dict) else {}

    def artifact(name: str, fallback: str) -> str | None:
        value = names.get(name, fallback)
        if not isinstance(value, str):
            return None
        candidate = Path(value)
        if candidate.is_absolute() or ".." in candidate.parts:
            candidate = Path(fallback)
        path = run_dir / candidate
        return _sanitize_text(_read_text(path)) or None

    events_text = artifact("agent_events", "agent_events.jsonl")
    events = _parse_events(events_text) if events_text is not None else None
    return {
        "prompt": artifact("prompt", "prompt.txt"),
        "answer": artifact("answer", "answer.txt"),
        "agent_patch": artifact("agent_patch", "agent.patch"),
        "trajectory": artifact("trajectory", "trajectory.log"),
        "test_log": artifact("test_log", "test.log"),
        "agent_events": events,
        "agent_debug": artifact("agent_debug", "agent_debug.log"),
        "mcp_activity": artifact("mcp_activity", "mcp_activity.jsonl"),
        "human_reference_patch": None,
    }


def _parse_events(text: str) -> list[Any]:
    events: list[Any] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            events.append(_sanitize_value(json.loads(line)))
        except (json.JSONDecodeError, TypeError):
            events.append({"type": "unparsed", "raw": _sanitize_text(line)})
    return events


def _run_summary(run: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in run.items() if key not in {"artifacts", "raw_sanitized_metadata"}}


def _case_summary(case: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in case.items()
        if key not in {"issue_text", "safe_provenance", "human_reference_patch", "run_ids"}
    }


def _write_payloads(output_root: Path, payloads: dict[Path, Any]) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    # The manifest is the commit marker and is always replaced last.
    ordered = sorted((path, value) for path, value in payloads.items() if path.name != "manifest.json")
    for relative, payload in ordered:
        write_json(output_root / relative, payload)
    _prune_stale_payloads(output_root, set(payloads))
    write_json(output_root / "manifest.json", payloads[Path("manifest.json")])


def _prune_stale_payloads(output_root: Path, expected: set[Path]) -> None:
    """Remove stale generated detail JSON without touching other public assets."""
    for directory in (Path("cases"), Path("runs")):
        root = output_root / directory
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.json")):
            relative = path.relative_to(output_root)
            if relative not in expected:
                path.unlink()


def _sanitize_value(value: Any, key: str | None = None) -> Any:
    if key and _FORBIDDEN_KEYS.search(key):
        return "<redacted>"
    if isinstance(value, dict):
        return {
            str(name): _sanitize_value(item, str(name))
            for name, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(name).lower() not in {"test_patch", "hidden_tests", "hidden_test"}
            and not _FORBIDDEN_KEYS.search(str(name))
        }
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, str):
        return _sanitize_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _sanitize_text(str(value))


def _sanitize_text(value: str) -> str:
    text = redact_secrets(value)
    text = _AUTH_VALUE.sub(r"\1<redacted>", text)
    text = _SECRET_ASSIGNMENT.sub(r"\1=<redacted>", text)
    text = _SECRET_IDENTIFIER.sub("<redacted-secret-name>", text)

    def url_host(match: re.Match[str]) -> str:
        host = urlsplit(match.group(0).rstrip(".,;)")).netloc
        return f"<url-host:{host}>" if host else "<redacted-url>"

    text = _URL.sub(url_host, text)
    text = _ABSOLUTE_PATH.sub("<local-path>", text)
    text = re.sub(r"(?i)\btest\.patch\b", "<excluded-evaluation-artifact>", text)
    return re.sub(r"(?i)\bgold\.patch\b", "<human-reference-artifact>", text)


def _host_only(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if "://" in text:
        return urlsplit(text).netloc or None
    return text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0] or None


def _safe_case_artifact(case_dir: Path, value: Any, hidden: set[str] | None = None) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or value in (hidden or set()):
        return None
    candidate = (case_dir / relative).resolve()
    try:
        candidate.relative_to(case_dir.resolve())
    except ValueError:
        return None
    if any(candidate == (case_dir / name).resolve() for name in (hidden or set())):
        return None
    return candidate


def _safe_segment(value: str) -> str:
    segment = value.strip()
    if not segment or segment in {".", ".."} or "/" in segment or "\\" in segment:
        raise PatchbenchError(f"Unsafe dashboard identifier: {value!r}")
    return segment


def _safe_relative_value(value: Any) -> str | None:
    if value is None:
        return None
    path = Path(str(value))
    return None if path.is_absolute() or ".." in path.parts else path.as_posix()


def _read_text(path: Path | None) -> str:
    if path is None or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _read_optional_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def _issue_title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        candidate = line.lstrip("# ").strip()
        if candidate:
            return _sanitize_text(candidate)
    return fallback


def _patch_stats_text(text: str) -> tuple[list[str], int, int]:
    if not text:
        return [], 0, 0
    handle, temporary = tempfile.mkstemp(prefix="patchbench-dashboard-patch-", suffix=".diff")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        return patch_stats(Path(temporary))
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _nullable_model(value: Any) -> str | None:
    if value is None or str(value).strip().lower() in {"", "unknown", "none", "null"}:
        return None
    return _sanitize_text(str(value))


def _optional_text(value: Any) -> str | None:
    return None if value is None or value == "" else _sanitize_text(str(value))


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _first_bool(*values: Any) -> bool | None:
    return next((value for value in values if isinstance(value, bool)), None)


def _first_text(*values: Any) -> str | None:
    return next((str(value) for value in values if value not in (None, "")), None)


def _path_modified_at(path: Path) -> str | None:
    try:
        modified_at = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(modified_at, timezone.utc).isoformat(timespec="seconds")


def _run_sort_key(run: dict[str, Any]) -> tuple[str, str, str]:
    return (run["case_id"], run.get("created_at") or "", run["run_id"])


def _minimal_normalized(
    result: dict[str, Any], metadata: dict[str, Any], run_dir: Path
) -> dict[str, Any]:
    merged = {**metadata, **result}
    agent = merged.get("agent")
    legacy_model = merged.get("model")
    return {
        "case_id": merged.get("case_id") or run_dir.parents[1].name,
        "run_id": merged.get("run_id") or run_dir.name,
        "agent": agent,
        "agent_harness": merged.get("agent_harness") or (
            "claude-code-cli" if agent == "claude-code" else agent
        ),
        "agent_harness_version": merged.get("agent_harness_version"),
        "provider": merged.get("provider"),
        "requested_model": merged.get("requested_model"),
        "actual_model": merged.get("actual_model", legacy_model),
        "model_source": merged.get("model_source"),
        "prompt_mode": merged.get("prompt_mode"),
        "agent_num_turns": merged.get("agent_num_turns"),
        "agent_activity_observed": merged.get("agent_activity_observed"),
        "agent_termination_reason": merged.get("agent_termination_reason"),
        "agent_execution_detail": merged.get("agent_execution_detail"),
        "partial_patch_captured": merged.get("partial_patch_captured"),
        "evaluation_skipped": merged.get("evaluation_skipped"),
        "patch_applied": merged.get("patch_applied", merged.get("patch_applies")),
        "resolved": _first_bool(
            merged.get("resolved"), merged.get("tests_pass"), merged.get("task_success")
        ),
        "failure_mode": merged.get("failure_mode"),
        "runtime_seconds": merged.get("runtime_seconds", merged.get("duration_seconds")),
        "files_changed": merged.get("files_changed", merged.get("changed_files", [])),
        "lines_added": merged.get("lines_added", merged.get("added_lines")),
        "lines_removed": merged.get("lines_removed", merged.get("removed_lines")),
        "patch_comparison": merged.get("patch_comparison"),
        "artifacts": merged.get("artifacts", {}),
    }
