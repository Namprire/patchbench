"""Deterministic aggregation of PatchBench JSON run artifacts."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from patchbench.exceptions import PatchbenchError
from patchbench.gitutils import compare_patches
from patchbench.io import read_json, write_json, write_text
from patchbench.runner import redact_secrets


def generate_report(input_path: str | Path) -> tuple[dict[str, Any], str]:
    """Discover runs recursively, write summary.json/report.md, and return both."""
    report_dir = Path(input_path).expanduser().resolve()
    if not report_dir.is_dir():
        raise PatchbenchError(f"Report path is not a directory: {report_dir}")

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result_paths: dict[Path, Path] = {}
    for path in sorted(report_dir.rglob("result.json")):
        result_paths[path.parent] = path
    for path in sorted(report_dir.rglob("agent.result.json")):
        result_paths.setdefault(path.parent, path)
    runs = [normalize_run(path) for path in result_paths.values()]
    runs.sort(key=lambda run: (run["case_id"], run["run_id"], run["agent"]))
    summary = _build_summary(runs, generated_at)
    markdown = _render_markdown(summary)

    write_json(report_dir / "summary.json", summary)
    write_text(report_dir / "report.md", markdown)
    return summary, markdown


def normalize_run(result_path: str | Path) -> dict[str, Any]:
    """Normalize current and historical run fields without mutating artifacts.

    This is the shared compatibility boundary for reports and read-only export
    consumers. Callers may enrich this normalized view, but should not
    independently reinterpret legacy result fields.
    """
    result_path = Path(result_path)
    try:
        result = read_json(result_path)
        metadata_path = result_path.with_name("metadata.json")
        metadata = read_json(metadata_path) if metadata_path.is_file() else {}
    except (OSError, ValueError, TypeError) as exc:
        raise PatchbenchError(f"Failed to read run artifacts at {result_path}: {exc}") from exc

    run_dir = result_path.parent
    nested_agent_details = result.get("agent_details")
    if not isinstance(nested_agent_details, dict):
        nested_agent_details = {}

    def field(name: str, default: Any) -> Any:
        value = result.get(name)
        if value is None:
            value = metadata.get(name)
        if value is None:
            value = nested_agent_details.get(name)
        return default if value is None else value

    def aliased_field(names: tuple[str, ...], default: Any) -> Any:
        for source in (result, metadata, nested_agent_details):
            for name in names:
                value = source.get(name)
                if value is not None:
                    return value
        return default

    patch_applied = result.get("patch_applied")
    if patch_applied is None:
        patch_applied = result.get("patch_applies", metadata.get("patch_applied", False))
    patch_comparison = _patch_comparison(result.get("patch_comparison"))
    if patch_comparison is None:
        patch_comparison = _legacy_patch_comparison(run_dir)

    resolved = aliased_field(("resolved", "tests_pass", "task_success"), False)

    agent_harness = field("agent_harness", None)
    agent = _text(
        field(
            "agent",
            "claude-code" if agent_harness == "claude-code-cli" else "unknown",
        )
    )
    legacy_model = field("model", "unknown")
    actual_model = field("actual_model", legacy_model)
    requested_model = field("requested_model", None)
    return {
        "case_id": _text(field("case_id", "unknown")),
        "run_id": _text(field("run_id", run_dir.name)),
        "agent": agent,
        "agent_harness": _text(
            agent_harness or ("claude-code-cli" if agent == "claude-code" else agent)
        ),
        "agent_harness_version": _optional_text(field("agent_harness_version", None)),
        "provider": _optional_text(field("provider", None)),
        "requested_model": _optional_text(requested_model),
        "actual_model": _text(actual_model),
        "model": _text(actual_model),
        "model_source": _text(field("model_source", "unknown")),
        "agent_execution_mode": _text(field("agent_execution_mode", "unknown")),
        "evaluation_execution_mode": _text(
            field("evaluation_execution_mode", "unknown")
        ),
        "prompt_mode": _text(field("prompt_mode", "unknown")),
        "agent_num_turns": _optional_integer(field("agent_num_turns", None)),
        "agent_activity_observed": bool(field("agent_activity_observed", False)),
        "agent_termination_reason": _optional_text(
            field("agent_termination_reason", None)
        ),
        "agent_execution_detail": _optional_text(
            field("agent_execution_detail", None)
        ),
        "partial_patch_captured": bool(field("partial_patch_captured", False)),
        "evaluation_skipped": bool(field("evaluation_skipped", False)),
        "mcp_enabled": bool(field("mcp_enabled", False)),
        "mcp_servers": _string_list(field("mcp_servers", [])),
        "mcp_condition": _text(field("mcp_condition", "none")),
        "mcp_server_name": _optional_text(field("mcp_server_name", None)),
        "mcp_profile": _optional_text(field("mcp_profile", None)),
        "mcp_revision": _optional_text(field("mcp_revision", None)),
        "mcp_revision_dirty": field("mcp_revision_dirty", None),
        "mcp_process_started": field("mcp_process_started", None),
        "mcp_tools_discovered": field("mcp_tools_discovered", None),
        "mcp_activity_observed": bool(field("mcp_activity_observed", False)),
        "mcp_tool_call_count": _integer(field("mcp_tool_call_count", 0)),
        "mcp_tool_success_count": _integer(field("mcp_tool_success_count", 0)),
        "mcp_tool_error_count": _integer(field("mcp_tool_error_count", 0)),
        "mcp_telemetry_available": bool(field("mcp_telemetry_available", False)),
        "patch_applied": bool(patch_applied),
        "resolved": bool(resolved),
        "failure_mode": _text(field("failure_mode", "unknown")),
        "runtime_seconds": _number(field("runtime_seconds", 0.0)),
        "files_changed": _string_list(field("files_changed", [])),
        "lines_added": _integer(field("lines_added", 0)),
        "lines_removed": _integer(field("lines_removed", 0)),
        "patch_comparison": patch_comparison,
        "artifacts": _discover_artifacts(run_dir, result.get("artifacts", {})),
    }


def _build_summary(runs: list[dict[str, Any]], generated_at: str) -> dict[str, Any]:
    total = len(runs)
    resolved_count = sum(run["resolved"] for run in runs)
    patch_applied_count = sum(run["patch_applied"] for run in runs)
    runtimes = [run["runtime_seconds"] for run in runs]
    return {
        "generated_at": generated_at,
        "total_runs": total,
        "total_cases": len({run["case_id"] for run in runs}),
        "agents": sorted({run["agent"] for run in runs}),
        "models": sorted({run["model"] for run in runs}),
        "resolved_count": resolved_count,
        "resolved_rate": _rate(resolved_count, total),
        "patch_applied_count": patch_applied_count,
        "patch_apply_rate": _rate(patch_applied_count, total),
        "average_runtime_seconds": sum(runtimes) / total if total else 0.0,
        "failure_modes": _counts(runs, "failure_mode"),
        "agent_breakdown": _breakdown(runs, "agent"),
        "model_breakdown": _breakdown(runs, "model"),
        "execution_mode_breakdown": {
            "agent_execution_mode": _counts(runs, "agent_execution_mode"),
            "evaluation_execution_mode": _counts(
                runs, "evaluation_execution_mode"
            ),
            "combinations": dict(
                sorted(
                    Counter(
                        f"{run['agent_execution_mode']} / {run['evaluation_execution_mode']}"
                        for run in runs
                    ).items()
                )
            ),
        },
        "prompt_mode_breakdown": _counts(runs, "prompt_mode"),
        "mcp_condition_breakdown": _counts(runs, "mcp_condition"),
        "runs": runs,
    }


def _breakdown(runs: list[dict[str, Any]], field: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for value in sorted({run[field] for run in runs}):
        selected = [run for run in runs if run[field] == value]
        total = len(selected)
        resolved = sum(run["resolved"] for run in selected)
        applied = sum(run["patch_applied"] for run in selected)
        result[value] = {
            "total_runs": total,
            "resolved_count": resolved,
            "resolved_rate": _rate(resolved, total),
            "patch_applied_count": applied,
            "patch_apply_rate": _rate(applied, total),
            "average_runtime_seconds": sum(
                run["runtime_seconds"] for run in selected
            )
            / total,
        }
    return result


def _render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# PatchBench Report",
        "",
        f"Generated: {summary['generated_at']}",
        "",
        "## Overview",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Total runs | {summary['total_runs']} |",
        f"| Total cases | {summary['total_cases']} |",
        f"| Resolved | {summary['resolved_count']} ({_percent(summary['resolved_rate'])}) |",
        f"| Patch applied | {summary['patch_applied_count']} ({_percent(summary['patch_apply_rate'])}) |",
        f"| Average runtime | {summary['average_runtime_seconds']:.3f}s |",
        "",
        "## Runs",
        "",
        "| Case | Run | Harness | Model | Requested | Provider | Prompt | MCP | MCP calls/errors | Turns | Activity | Termination | Detail | Partial | Evaluation skipped | Resolved | Failure mode | Runtime |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | ---: | ---: | --- | --- | --- | --- | --- | --- | --- | ---: |",
    ]
    for run in summary["runs"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    _markdown(run["case_id"]),
                    _markdown(run["run_id"]),
                    _markdown(run["agent_harness"]),
                    _markdown(run["model"]),
                    _markdown(
                        run["requested_model"]
                        if run["requested_model"]
                        and run["requested_model"] != run["actual_model"]
                        else "—"
                    ),
                    _markdown(run["provider"] or "—"),
                    _markdown(run["prompt_mode"]),
                    _markdown(run["mcp_condition"]),
                    f"{run['mcp_tool_call_count']}/{run['mcp_tool_error_count']}",
                    str(run["agent_num_turns"] if run["agent_num_turns"] is not None else "—"),
                    _yes_no(run["agent_activity_observed"]),
                    _markdown(run["agent_termination_reason"] or "—"),
                    _markdown(run["agent_execution_detail"] or "—"),
                    _yes_no(run["partial_patch_captured"]),
                    _yes_no(run["evaluation_skipped"]),
                    _yes_no(run["resolved"]),
                    _markdown(run["failure_mode"]),
                    f"{run['runtime_seconds']:.3f}s",
                ]
            )
            + " |"
        )
    if not summary["runs"]:
        lines.append("| _No runs found_ |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |")

    lines.extend(
        [
            "",
            "## Patch Comparison",
            "",
            "| Case / Run | Resolved | Failure mode | Agent files | Gold files | Overlap | Agent patch | Gold patch | Runtime |",
            "| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for run in summary["runs"]:
        comparison = run["patch_comparison"]
        lines.append(
            "| "
            + " | ".join(
                [
                    _markdown(f"{run['case_id']} / {run['run_id']}"),
                    _yes_no(run["resolved"]),
                    _markdown(run["failure_mode"]),
                    _comparison_files(comparison, "agent_files_changed"),
                    _comparison_files(comparison, "gold_files_changed"),
                    _comparison_overlap(comparison),
                    _comparison_size(comparison, "agent"),
                    _comparison_size(comparison, "gold"),
                    f"{run['runtime_seconds']:.3f}s",
                ]
            )
            + " |"
        )
    if not summary["runs"]:
        lines.append("| _No runs found_ |  |  |  |  |  |  |  |  |")

    lines.extend(["", "## Failure Modes", "", "| Failure mode | Runs |", "| --- | ---: |"])
    for mode, count in summary["failure_modes"].items():
        lines.append(f"| {_markdown(mode)} | {count} |")
    if not summary["failure_modes"]:
        lines.append("| _None_ | 0 |")

    lines.extend(_breakdown_markdown("Agent Breakdown", summary["agent_breakdown"]))
    lines.extend(_breakdown_markdown("Model Breakdown", summary["model_breakdown"]))
    lines.extend(
        [
            "",
            "## MCP Conditions",
            "",
            _format_counts(summary["mcp_condition_breakdown"]) + ".",
        ]
    )
    agent_modes = _format_counts(
        summary["execution_mode_breakdown"]["agent_execution_mode"]
    )
    evaluation_modes = _format_counts(
        summary["execution_mode_breakdown"]["evaluation_execution_mode"]
    )
    lines.extend(
        [
            "",
            "## Execution Modes",
            "",
            f"- Solver execution: {agent_modes}.",
            f"- Evaluation execution: {evaluation_modes}.",
            "- Agent and evaluation modes are reported independently; containerized evaluation does not imply containerized solving.",
            "",
        ]
    )
    return "\n".join(lines)


def _breakdown_markdown(title: str, breakdown: dict[str, Any]) -> list[str]:
    lines = [
        "",
        f"## {title}",
        "",
        "| Name | Runs | Resolved | Patch applied | Average runtime |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for name, values in breakdown.items():
        lines.append(
            f"| {_markdown(name)} | {values['total_runs']} | "
            f"{values['resolved_count']} ({_percent(values['resolved_rate'])}) | "
            f"{values['patch_applied_count']} ({_percent(values['patch_apply_rate'])}) | "
            f"{values['average_runtime_seconds']:.3f}s |"
        )
    if not breakdown:
        lines.append("| _None_ | 0 | 0 | 0 | 0.000s |")
    return lines


def _discover_artifacts(run_dir: Path, declared: Any) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    if isinstance(declared, dict):
        for name, value in declared.items():
            if not isinstance(name, str) or not isinstance(value, str):
                continue
            path = _contained_artifact(run_dir, Path(value))
            if path is not None:
                artifacts[name] = path

    known = {
        "agent_patch": "agent.patch",
        "prompt": "prompt.txt",
        "trajectory": "trajectory.log",
        "metadata": "metadata.json",
        "test_log": "test.log",
        "agent_events": "agent_events.jsonl",
        "agent_debug": "agent_debug.log",
        "mcp_activity": "mcp_activity.jsonl",
    }
    for name, filename in known.items():
        path = _contained_artifact(run_dir, Path(filename))
        if path is not None:
            artifacts.setdefault(name, path)
    for result_name in ("result.json", "agent.result.json"):
        path = _contained_artifact(run_dir, Path(result_name))
        if path is not None:
            artifacts.setdefault("result", path)
            break
    return dict(sorted(artifacts.items()))


def _contained_artifact(run_dir: Path, path: Path) -> str | None:
    root = run_dir.resolve()
    candidate = path.resolve() if path.is_absolute() else (root / path).resolve()
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return None
    return relative.as_posix() if candidate.is_file() else None


def _counts(runs: list[dict[str, Any]], field: str) -> dict[str, int]:
    return dict(sorted(Counter(run[field] for run in runs).items()))


def _rate(count: int, total: int) -> float:
    return count / total if total else 0.0


def _text(value: Any) -> str:
    return redact_secrets(str(value))


def _optional_text(value: Any) -> str | None:
    return None if value is None else _text(value)


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _integer(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _optional_integer(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_text(item) for item in value]


def _patch_comparison(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "agent_files_changed": _string_list(value.get("agent_files_changed")),
        "gold_files_changed": _string_list(value.get("gold_files_changed")),
        "overlapping_files": _string_list(value.get("overlapping_files")),
        "file_overlap_count": _integer(value.get("file_overlap_count", 0)),
        "file_overlap_jaccard": _number(value.get("file_overlap_jaccard", 0.0)),
        "exact_file_set_match": bool(value.get("exact_file_set_match", False)),
        "agent_lines_added": _integer(value.get("agent_lines_added", 0)),
        "agent_lines_removed": _integer(value.get("agent_lines_removed", 0)),
        "gold_lines_added": _integer(value.get("gold_lines_added", 0)),
        "gold_lines_removed": _integer(value.get("gold_lines_removed", 0)),
    }


def _legacy_patch_comparison(run_dir: Path) -> dict[str, Any] | None:
    """Compute comparison metrics for historical local runs without persisting them."""
    case_dir = run_dir.parent.parent
    gold_patch = case_dir / "gold.patch"
    if not gold_patch.is_file():
        return None
    return compare_patches(run_dir / "agent.patch", gold_patch).to_dict()


def _comparison_files(comparison: dict[str, Any] | None, field: str) -> str:
    if comparison is None:
        return "—"
    files = comparison[field]
    if not files:
        return "(none)"
    rendered = ", ".join(_markdown(path) for path in files)
    if len(rendered) <= 80:
        return rendered
    return f"{_markdown(files[0])}, +{len(files) - 1} more"


def _comparison_overlap(comparison: dict[str, Any] | None) -> str:
    if comparison is None:
        return "—"
    return (
        f"{comparison['file_overlap_count']} "
        f"({comparison['file_overlap_jaccard']:.2f})"
    )


def _comparison_size(comparison: dict[str, Any] | None, prefix: str) -> str:
    if comparison is None:
        return "—"
    return (
        f"+{comparison[f'{prefix}_lines_added']}"
        f"/-{comparison[f'{prefix}_lines_removed']}"
    )


def _percent(rate: float) -> str:
    return f"{rate * 100:.1f}%"


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _markdown(value: Any) -> str:
    return _text(value).replace("|", "\\|").replace("\n", " ")


def _format_counts(values: dict[str, int]) -> str:
    if not values:
        return "none"
    return ", ".join(f"{_markdown(name)}={count}" for name, count in values.items())
