"""Opt-in loopback execution service for the workflow builder."""

from __future__ import annotations

import ipaddress
import json
import re
import shlex
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from patchbench import __version__
from patchbench.catalog import read_metadata, case_type as get_case_type, resolve_case, case_runs_dir
from patchbench.repositories import load_repository
from patchbench.case import is_safe_path_component
from patchbench.commands import prepare_solver_prompt, run_agent, validate_case
from patchbench.dashboard_export import _sanitize_value, export_dashboard
from patchbench.exceptions import PatchbenchError
from patchbench.io import write_json
from patchbench.landb_answer_benchmark import run_answer_benchmark
from patchbench.operations import OPERATION_KINDS, execute_operation, normalize_operation


SERVICE_SCHEMA_VERSION = 1
_ALLOWED_AGENTS = {"dummy", "gold", "claude-code", "container-command"}
_ALLOWED_PROMPTS = {"issue_only", "issue_with_hints"}
_ALLOWED_STOPS = {"prompt", "patch", "evaluation"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class RunRecord:
    run_id: str
    case_id: str
    config: dict[str, Any]
    status: str = "queued"
    events: list[dict[str, Any]] = field(default_factory=list)
    finished: bool = False
    cancel_requested: bool = False
    next_seq: int = 0
    condition: threading.Condition = field(default_factory=threading.Condition)

    def emit(self, event_type: str, **fields: Any) -> None:
        with self.condition:
            fields.setdefault("emitted_at", _now())
            if isinstance(fields.get("message"), str) and len(fields["message"]) > 20_000:
                fields["message"] = fields["message"][:20_000] + "\n<truncated>"
            event = _sanitize_value(
                {"seq": self.next_seq, "type": event_type, **fields}
            )
            if len(json.dumps(event, ensure_ascii=False).encode("utf-8")) > 2_000_000:
                event = _sanitize_value(
                    {
                        "seq": self.next_seq,
                        "type": event_type,
                        "status": fields.get("status"),
                        "emitted_at": fields["emitted_at"],
                        "result": {
                            "truncated": True,
                            "message": "Event payload exceeded the 2 MB limit.",
                        },
                    }
                )
            self.next_seq += 1
            self.events.append(event)
            if len(self.events) > 1000:
                # Preserve monotonic sequence values even when old events roll off.
                self.events = self.events[-1000:]
            self.condition.notify_all()

    def close(self, status: str) -> None:
        with self.condition:
            self.status = status
            self.finished = True
            self.condition.notify_all()


@dataclass
class OperationRecord:
    operation_id: str
    kind: str
    config: dict[str, Any]
    status: str = "queued"
    events: list[dict[str, Any]] = field(default_factory=list)
    finished: bool = False
    next_seq: int = 0
    condition: threading.Condition = field(default_factory=threading.Condition)

    def emit(self, event_type: str, **fields: Any) -> None:
        with self.condition:
            if isinstance(fields.get("message"), str) and len(fields["message"]) > 20_000:
                fields["message"] = fields["message"][:20_000] + "\n<truncated>"
            event = _sanitize_value(
                {"seq": self.next_seq, "type": event_type, **fields}
            )
            if len(json.dumps(event, ensure_ascii=False).encode("utf-8")) > 2_000_000:
                event = _sanitize_value(
                    {
                        "seq": self.next_seq,
                        "type": event_type,
                        "status": fields.get("status"),
                        "result": {
                            "truncated": True,
                            "message": "Event payload exceeded the 2 MB limit.",
                        },
                    }
                )
            self.next_seq += 1
            self.events.append(event)
            if len(self.events) > 1000:
                self.events = self.events[-1000:]
            self.condition.notify_all()

    def close(self, status: str) -> None:
        with self.condition:
            self.status = status
            self.finished = True
            self.condition.notify_all()


class RunRegistry:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.records: dict[str, RunRecord] = {}
        self.operation_records: dict[str, OperationRecord] = {}
        self.lock = threading.Lock()
        self.export_lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="patchbench-run")
        self.operation_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="patchbench-operation"
        )

    def register(self, payload: dict[str, Any]) -> RunRecord:
        config = _validate_payload(payload, self.root)
        run_id = config["run_id"]
        case_id = config["case_id"]
        case_dir = resolve_case(self.root, case_id)
        run_dir = case_runs_dir(case_dir) / run_id
        legacy_run = case_dir / "runs" / run_id
        with self.lock:
            previous = self.records.get(run_id)
            if previous and not previous.finished:
                raise PatchbenchError(f"Run ID is already active: {run_id}")
            if run_dir.exists() or run_dir.is_symlink() or legacy_run.exists() or legacy_run.is_symlink():
                raise PatchbenchError(f"Run ID already exists for {case_id}: {run_id}")
            record = RunRecord(run_id=run_id, case_id=case_id, config=config)
            record.emit(
                "run_start",
                run_id=run_id,
                case_id=case_id,
                status="queued",
                started_at=_now(),
                config=_public_config(config),
            )
            self.records[run_id] = record
            self.executor.submit(self._worker, record)
            return record

    def prompt_for(self, record: RunRecord) -> str | None:
        """Return only the solver prompt belonging to a registered run."""
        with record.condition:
            for event in reversed(record.events):
                prompt = event.get("prompt")
                if isinstance(prompt, str):
                    return prompt
        prompt_path = (
            case_runs_dir(resolve_case(self.root, record.case_id))
            / record.run_id
            / "prompt.txt"
        )
        try:
            if prompt_path.is_file() and prompt_path.stat().st_size <= 2_000_000:
                return prompt_path.read_text(encoding="utf-8")
        except OSError:
            return None
        return None

    def _worker(self, record: RunRecord) -> None:
        config = record.config
        record.status = "running"
        final_status = "complete"
        terminal_event = "run_complete"
        terminal_fields: dict[str, Any] = {}
        try:
            if record.cancel_requested:
                raise KeyboardInterrupt
            if config["stop_at"] == "prompt":
                record.emit("phase", phase="environment", status="success")
                record.emit("phase", phase="prompt", status="running")
                prompt = prepare_solver_prompt(
                    resolve_case(self.root, record.case_id),
                    config["prompt_mode"],
                    config["agent"],
                )
                if record.cancel_requested:
                    raise KeyboardInterrupt
                record.emit(
                    "phase",
                    phase="prompt",
                    status="success",
                    prompt=prompt,
                    last_activity="Prompt prepared; no solver executed",
                )
                terminal_event = "stopped"
                terminal_fields = {"phase": "prompt", "message": "Stopped at prompt"}
            else:
                if config["case_type"] == "landb_answer":
                    result_payload, resolved = self._run_answer(record)
                else:
                    evidence = _evidence_config(config)

                    def progress(phase: str, **fields: Any) -> None:
                        if record.cancel_requested:
                            raise KeyboardInterrupt
                        record.emit("phase", phase=phase, **fields)

                    result = run_agent(
                        resolve_case(self.root, record.case_id),
                        config["agent"],
                        record.run_id,
                        container=config["container"],
                        agent_env_file=config.get("agent_env_file"),
                        agent_ca_file=config.get("agent_ca_file"),
                        agent_image=config.get("agent_image"),
                        agent_command=config.get("agent_command"),
                        agent_timeout_seconds=config["agent_timeout_seconds"],
                        agent_model=config.get("agent_model"),
                        agent_max_turns=config["agent_max_turns"],
                        agent_debug=config["agent_debug"],
                        agent_network=config["agent_network"],
                        live=False,
                        prompt_mode=config["prompt_mode"],
                        progress=progress,
                        mcp_server=config.get("mcp_server"),
                        mcp_command=config.get("mcp_command"),
                        mcp_profile=config.get("mcp_profile"),
                        mcp_env_file=config.get("mcp_env_file"),
                        skip_evaluation=config["stop_at"] == "patch",
                        run_config=evidence,
                    )
                    result_payload = result.to_dict()
                    resolved = result.resolved
                # Agent adapters may translate the callback's KeyboardInterrupt
                # into an interrupted result after terminating their child process.
                # Keep the user's cancellation authoritative at this boundary.
                if record.cancel_requested:
                    raise KeyboardInterrupt
                record.emit("phase", phase="result", status="success")
                terminal_fields = {
                    "result": result_payload,
                    "resolved": resolved,
                    "stop_at": config["stop_at"],
                }
                if config["stop_at"] == "patch":
                    terminal_fields["message"] = (
                        "Patch captured; evaluation skipped"
                    )
        except BaseException as exc:
            final_status = "error"
            message = "Run cancelled" if isinstance(exc, KeyboardInterrupt) else str(exc)
            terminal_event = "error"
            terminal_fields = {"message": message}
        finally:
            output_root = self.root / "frontend" / "public" / "data"
            with self.export_lock:
                try:
                    manifest = export_dashboard(
                        cases_dir=self.root / "cases",
                        runs_dir=self.root / "runs",
                        output=output_root,
                    )
                    run_indexed = (
                        output_root
                        / "runs"
                        / record.case_id
                        / f"{record.run_id}.json"
                    ).is_file()
                    record.emit(
                        "dashboard_updated",
                        phase="result",
                        status="success",
                        case_id=record.case_id,
                        run_id=record.run_id,
                        generated_at=manifest["generated_at"],
                        case_count=manifest["case_count"],
                        run_count=manifest["run_count"],
                        run_indexed=run_indexed,
                        detail_path=(
                            f"/data/runs/{record.case_id}/{record.run_id}.json"
                            if run_indexed
                            else None
                        ),
                    )
                except Exception as exc:
                    record.emit(
                        "indexing_error",
                        phase="result",
                        status="failed",
                        message=f"Dashboard export failed: {exc}",
                    )
            # Terminal completion is emitted only after dashboard indexing, so
            # clients can refresh without racing the export commit marker.
            record.emit(terminal_event, **terminal_fields)
            record.close(final_status)

    def _run_answer(self, record: RunRecord) -> tuple[dict[str, Any], bool]:
        config = record.config
        record.emit("phase", phase="environment", status="success")
        record.emit(
            "phase",
            phase="prompt",
            status="success",
            last_activity="Answer prompt prepared from the frozen case",
        )
        record.emit("phase", phase="worktree", status="skipped")
        record.emit(
            "phase",
            phase="agent",
            status="running",
            last_activity="Starting the Claude Code answer harness",
        )
        record.emit("phase", phase="tools", status="running")

        def progress(message: str) -> None:
            if record.cancel_requested:
                raise KeyboardInterrupt
            record.emit(
                "phase",
                phase="tools",
                status="running",
                last_activity=message,
            )

        result = run_answer_benchmark(
            resolve_case(self.root, record.case_id),
            landb_mcp_repo=config["landb_mcp_repo"],
            condition=config["answer_condition"],
            run_id=record.run_id,
            agent_env_file=config.get("agent_env_file"),
            agent_model=config.get("agent_model"),
            timeout_seconds=config["agent_timeout_seconds"],
            max_turns=config["agent_max_turns"],
            artifacts_root=config.get("artifacts_root"),
            live_dev=config["live_dev"],
            mcp_env_file=config.get("mcp_env_file"),
            progress=progress,
        )
        if record.cancel_requested:
            raise KeyboardInterrupt
        write_json(
            case_runs_dir(resolve_case(self.root, record.case_id))
            / record.run_id
            / "run.config.json",
            _evidence_config(config),
        )
        record.emit("phase", phase="agent", status="success")
        record.emit(
            "phase",
            phase="tools",
            status="success",
            tool_calls=result.mcp_tool_call_count,
        )
        record.emit("phase", phase="patch", status="skipped")
        record.emit("phase", phase="termination", status="success")
        record.emit(
            "phase",
            phase="evaluation",
            status="success",
            task_success=result.task_success,
            answer_correct=result.answer_correct,
            read_only=result.read_only,
        )
        return asdict(result), result.task_success

    def active(self) -> list[dict[str, str]]:
        with self.lock:
            return [
                {"run_id": item.run_id, "case_id": item.case_id, "status": item.status}
                for item in self.records.values()
                if not item.finished
            ]

    def register_operation(self, payload: dict[str, Any]) -> OperationRecord:
        config = normalize_operation(payload, self.root)
        operation_id = f"{config['kind']}-{uuid.uuid4().hex[:12]}"
        record = OperationRecord(operation_id, config["kind"], config)
        record.emit(
            "operation_start",
            operation_id=operation_id,
            kind=config["kind"],
            status="queued",
            started_at=_now(),
            config=_public_operation_config(config),
        )
        with self.lock:
            self.operation_records[operation_id] = record
        self.operation_executor.submit(self._operation_worker, record)
        return record

    def _operation_worker(self, record: OperationRecord) -> None:
        record.status = "running"
        try:
            record.emit("operation_status", status="running")
            outcome = execute_operation(
                record.config,
                self.root,
                record.operation_id,
                lambda message: record.emit("log", status="running", message=message),
            )
            if outcome.refresh_dashboard:
                with self.export_lock:
                    try:
                        manifest = export_dashboard(
                            cases_dir=self.root / "cases",
                            runs_dir=self.root / "runs",
                            output=self.root / "frontend" / "public" / "data",
                        )
                        record.emit(
                            "dashboard_updated",
                            status="success",
                            generated_at=manifest["generated_at"],
                            case_count=manifest["case_count"],
                            run_count=manifest["run_count"],
                        )
                    except Exception as exc:
                        record.emit(
                            "indexing_error",
                            status="failed",
                            message=f"Dashboard export failed: {exc}",
                        )
            elif outcome.dashboard_manifest is not None:
                manifest = outcome.dashboard_manifest
                record.emit(
                    "dashboard_updated",
                    status="success",
                    generated_at=manifest["generated_at"],
                    case_count=manifest["case_count"],
                    run_count=manifest["run_count"],
                )
            record.emit("operation_complete", status="success", result=outcome.result)
            record.close("complete")
        except BaseException as exc:
            record.emit("operation_error", status="failed", message=str(exc))
            record.close("error")

    def active_operations(self) -> list[dict[str, str]]:
        with self.lock:
            return [
                {
                    "operation_id": item.operation_id,
                    "kind": item.kind,
                    "status": item.status,
                }
                for item in self.operation_records.values()
                if not item.finished
            ]

    def cancel_active(self) -> None:
        """Request cancellation for every queued or running record."""
        with self.lock:
            for record in self.records.values():
                if not record.finished:
                    record.cancel_requested = True

    def validate_case(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Validate one frozen case and refresh the static dashboard export."""
        config = _validate_case_payload(payload, self.root)
        result = validate_case(
            resolve_case(self.root, config["case_id"]),
            container=config["container"],
            landb_mcp_repo=config["landb_mcp_repo"],
        )
        response: dict[str, Any] = {"result": asdict(result)}
        with self.export_lock:
            try:
                manifest = export_dashboard(
                    cases_dir=self.root / "cases",
                    runs_dir=self.root / "runs",
                    output=self.root / "frontend" / "public" / "data",
                )
                response["dashboard_updated"] = True
                response["manifest"] = manifest
            except Exception as exc:
                response["dashboard_updated"] = False
                response["indexing_error"] = f"Dashboard export failed: {exc}"
        return response


def _validate_case_payload(payload: dict[str, Any], root: Path) -> dict[str, Any]:
    case_id = payload.get("case_id")
    if not isinstance(case_id, str) or not is_safe_path_component(case_id):
        raise PatchbenchError("Case ID must be one safe path component")
    case_dir = resolve_case(root, case_id)
    meta = read_metadata(case_dir)
    case_type = get_case_type(meta)
    container = payload.get("container", False)
    if not isinstance(container, bool):
        raise PatchbenchError("container must be a boolean")
    if case_type == "landb_answer" and container:
        raise PatchbenchError("landb_answer validation uses the host runtime")
    landb_mcp_repo = payload.get("landb_mcp_repo")
    if landb_mcp_repo is None and case_type == "landb_answer":
        landb_mcp_repo = str(load_repository(meta.get("repository", "landb-mcp"), case_dir).checkout())
    if landb_mcp_repo is not None and (not isinstance(landb_mcp_repo, str) or not landb_mcp_repo.strip()):
        raise PatchbenchError("landb_mcp_repo must be a non-empty path")
    return {
        "case_id": case_id,
        "case_type": case_type,
        "container": container,
        "landb_mcp_repo": landb_mcp_repo,
    }


def _validate_payload(payload: dict[str, Any], root: Path) -> dict[str, Any]:
    case_id = payload.get("case_id")
    run_id = payload.get("run_id")
    agent = payload.get("agent")
    prompt_mode = payload.get("prompt_mode", "issue_only")
    stop_at = payload.get("stop_at", "evaluation")
    case_dir = resolve_case(root, case_id)
    case_metadata = read_metadata(case_dir)
    if not is_safe_path_component(run_id):
        raise PatchbenchError("Run ID must be one safe path component")
    case_type = get_case_type(case_metadata)
    if case_type == "landb_answer":
        return _validate_answer_payload(
            payload,
            root=root,
            case_id=str(case_id),
            run_id=str(run_id),
        )
    if agent not in _ALLOWED_AGENTS:
        raise PatchbenchError("Unsupported agent")
    if prompt_mode not in _ALLOWED_PROMPTS:
        raise PatchbenchError("Unsupported prompt mode")
    if stop_at not in _ALLOWED_STOPS:
        raise PatchbenchError("Unsupported stop target")
    max_turns = payload.get("agent_max_turns", 100)
    timeout = payload.get("agent_timeout_seconds", 900)
    if not isinstance(max_turns, int) or isinstance(max_turns, bool) or max_turns <= 0:
        raise PatchbenchError("agent_max_turns must be a positive integer")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise PatchbenchError("agent_timeout_seconds must be a positive integer")
    text_keys = (
        "agent_model", "agent_env_file", "agent_ca_file", "agent_image",
        "agent_network", "agent_command", "mcp_server", "mcp_command", "mcp_profile", "mcp_env_file",
    )
    for key in text_keys:
        if payload.get(key) is not None and not isinstance(payload[key], str):
            raise PatchbenchError(f"{key} must be a string or null")
    for key in ("agent_debug", "container"):
        if key in payload and not isinstance(payload[key], bool):
            raise PatchbenchError(f"{key} must be a boolean")
    container = bool(payload.get("container", False))
    agent_command = payload.get("agent_command") or None
    if agent_command and agent != "container-command":
        raise PatchbenchError("agent_command is supported only by container-command")
    if agent == "container-command" and not container:
        raise PatchbenchError("container-command requires the container runtime")
    return {
        "case_type": "coding",
        "case_id": case_id,
        "run_id": run_id,
        "agent": agent,
        "prompt_mode": prompt_mode,
        "stop_at": stop_at,
        "agent_timeout_seconds": timeout,
        "agent_model": payload.get("agent_model") or None,
        "agent_max_turns": max_turns,
        "agent_debug": bool(payload.get("agent_debug", False)),
        "container": container,
        "agent_env_file": payload.get("agent_env_file") or None,
        "agent_ca_file": payload.get("agent_ca_file") or None,
        "agent_image": payload.get("agent_image") or None,
        "agent_command": agent_command,
        "agent_network": payload.get("agent_network") or "pasta",
        "mcp_server": payload.get("mcp_server") or None,
        "mcp_command": payload.get("mcp_command") or None,
        "mcp_profile": payload.get("mcp_profile") or None,
        "mcp_env_file": payload.get("mcp_env_file") or None,
    }


def _validate_answer_payload(
    payload: dict[str, Any], *, root: Path, case_id: str, run_id: str
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id):
        raise PatchbenchError(
            "Answer run ID may contain only letters, numbers, '.', '_' and '-'"
        )
    if payload.get("agent") != "claude-code":
        raise PatchbenchError("LanDB answer cases require the claude-code agent")
    if payload.get("stop_at", "evaluation") != "evaluation":
        raise PatchbenchError("LanDB answer runs always include answer evaluation")
    if payload.get("container", False) is not False:
        raise PatchbenchError("LanDB answer runs currently use the host runtime")
    condition = payload.get("answer_condition", "curated")
    if condition not in {"none", "curated", "broad"}:
        raise PatchbenchError("Unsupported LanDB answer condition")
    max_turns = payload.get("agent_max_turns", 100)
    timeout = payload.get("agent_timeout_seconds", 300)
    if not isinstance(max_turns, int) or isinstance(max_turns, bool) or max_turns <= 0:
        raise PatchbenchError("agent_max_turns must be a positive integer")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise PatchbenchError("agent_timeout_seconds must be a positive integer")
    live_dev = payload.get("live_dev", False)
    if not isinstance(live_dev, bool):
        raise PatchbenchError("live_dev must be a boolean")
    text_keys = (
        "agent_model",
        "agent_env_file",
        "landb_mcp_repo",
        "artifacts_root",
        "mcp_env_file",
    )
    for key in text_keys:
        if payload.get(key) is not None and not isinstance(payload[key], str):
            raise PatchbenchError(f"{key} must be a string or null")
    artifacts_root = payload.get("artifacts_root") or None
    mcp_env_file = payload.get("mcp_env_file") or None
    if live_dev and not artifacts_root:
        raise PatchbenchError("Live DEV answer runs require an external artifacts root")
    if live_dev and not mcp_env_file:
        raise PatchbenchError("Live DEV answer runs require an MCP environment file")
    if not live_dev and (artifacts_root or mcp_env_file):
        raise PatchbenchError(
            "External artifacts and MCP environment files require live DEV mode"
        )
    case_dir = resolve_case(root, case_id)
    repository_id = read_metadata(case_dir).get("repository", "landb-mcp")
    return {
        "case_type": "landb_answer",
        "case_id": case_id,
        "run_id": run_id,
        "agent": "claude-code",
        "prompt_mode": "answer_task",
        "stop_at": "evaluation",
        "answer_condition": condition,
        "landb_mcp_repo": payload.get("landb_mcp_repo")
        or str(load_repository(repository_id, case_dir).checkout()),
        "agent_timeout_seconds": timeout,
        "agent_model": payload.get("agent_model") or None,
        "agent_max_turns": max_turns,
        "agent_debug": False,
        "container": False,
        "agent_env_file": payload.get("agent_env_file") or None,
        "agent_ca_file": None,
        "agent_image": None,
        "agent_network": "pasta",
        "mcp_server": "landb" if condition != "none" else None,
        "mcp_command": None,
        "mcp_profile": condition if condition != "none" else None,
        "mcp_env_file": mcp_env_file,
        "live_dev": live_dev,
        "artifacts_root": artifacts_root,
    }


def _public_config(config: dict[str, Any]) -> dict[str, Any]:
    return _evidence_config(config)


def _public_operation_config(config: dict[str, Any]) -> dict[str, Any]:
    """Expose operation shape without patch bodies or external file paths."""
    external_path_keys = {
        "agent_env_file",
        "agent_ca_file",
        "mcp_env_file",
        "landb_mcp_repo",
        "repo_path",
        "artifacts_dir",
    }
    public: dict[str, Any] = {}
    for key, value in config.items():
        if key == "patch_text":
            public["patch_bytes"] = len(value.encode("utf-8"))
        elif key in external_path_keys:
            public[f"{key}_configured"] = bool(value)
        else:
            public[key] = value
    return public


def _evidence_config(config: dict[str, Any]) -> dict[str, Any]:
    if config["case_type"] == "landb_answer":
        return {
            "schema_version": 1,
            "case_type": "landb_answer",
            "case_id": config["case_id"],
            "agent": "claude-code",
            "requested_model": config.get("agent_model"),
            "prompt_mode": "answer_task",
            "agent_max_turns": config["agent_max_turns"],
            "agent_timeout_seconds": config["agent_timeout_seconds"],
            "answer_condition": config["answer_condition"],
            "mcp_server": config.get("mcp_server"),
            "mcp_profile": config.get("mcp_profile"),
            "agent_env_configured": bool(config.get("agent_env_file")),
            "mcp_env_configured": bool(config.get("mcp_env_file")),
            "live_dev": config["live_dev"],
            "stop_at": "evaluation",
            "launched_from": "workflow-canvas",
            "cli_equivalent": _cli_equivalent(config),
        }
    return {
        "schema_version": 1,
        "case_id": config["case_id"],
        "agent": config["agent"],
        "requested_model": config.get("agent_model"),
        "prompt_mode": config["prompt_mode"],
        "agent_max_turns": config["agent_max_turns"],
        "agent_timeout_seconds": config["agent_timeout_seconds"],
        "agent_debug": config["agent_debug"],
        "container": config["container"],
        "agent_image": config.get("agent_image"),
        "agent_command_configured": bool(config.get("agent_command")),
        "agent_network": config.get("agent_network") if config["container"] else None,
        "agent_env_configured": bool(config.get("agent_env_file")),
        "agent_ca_configured": bool(config.get("agent_ca_file")),
        "mcp_server": config.get("mcp_server"),
        "mcp_profile": config.get("mcp_profile"),
        "mcp_command_configured": bool(config.get("mcp_command")),
        "mcp_env_configured": bool(config.get("mcp_env_file")),
        "stop_at": config["stop_at"],
        "launched_from": "workflow-canvas",
        "cli_equivalent": _cli_equivalent(config),
    }


def _cli_equivalent(config: dict[str, Any]) -> str:
    if config["case_type"] == "landb_answer":
        args = [
            "patchbench",
            "run-landb-answer",
            config["case_id"],
            "--condition",
            config["answer_condition"],
            "--run-id",
            config["run_id"],
            "--landb-mcp-repo",
            config["landb_mcp_repo"],
            "--timeout-seconds",
            str(config["agent_timeout_seconds"]),
            "--agent-max-turns",
            str(config["agent_max_turns"]),
        ]
        if config.get("agent_env_file"):
            args.extend(["--agent-env-file", "<external-file>"])
        if config.get("agent_model"):
            args.extend(["--agent-model", config["agent_model"]])
        if config["live_dev"]:
            args.extend(
                [
                    "--live-dev",
                    "--mcp-env-file",
                    "<external-file>",
                    "--artifacts-root",
                    config["artifacts_root"],
                ]
            )
        return shlex.join(args)
    args = [
        "patchbench", "run", config["case_id"],
        "--agent", config["agent"], "--run-id", config["run_id"],
        "--prompt-mode", config["prompt_mode"],
        "--agent-timeout-seconds", str(config["agent_timeout_seconds"]),
    ]
    if config["agent"] == "claude-code":
        args.extend(["--agent-max-turns", str(config["agent_max_turns"])])
        if config.get("agent_model"):
            args.extend(["--agent-model", config["agent_model"]])
        if config["agent_debug"]:
            args.append("--agent-debug")
    if config["container"]:
        args.append("--container")
    if config.get("agent_image"):
        args.extend(["--agent-image", config["agent_image"]])
    if config.get("agent_command"):
        args.extend(["--agent-command", "<custom-command>"])
    if config.get("agent_env_file"):
        args.extend(["--agent-env-file", "<external-file>"])
    if config.get("agent_ca_file"):
        args.extend(["--agent-ca-file", "<external-file>"])
    if config.get("mcp_server"):
        args.extend(["--mcp-server", config["mcp_server"]])
        if config.get("mcp_profile"):
            args.extend(["--mcp-profile", config["mcp_profile"]])
        if config.get("mcp_command"):
            args.extend(["--mcp-command", "<external-executable>"])
        if config.get("mcp_env_file"):
            args.extend(["--mcp-env-file", "<external-file>"])
    if config["stop_at"] == "prompt":
        args.append("--dry-run")
    elif config["stop_at"] == "patch":
        args.append("--skip-evaluation")
    return shlex.join(args)


class WorkflowRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    registry: RunRegistry
    allow_remote_origins = False

    def log_message(self, format: str, *args: Any) -> None:
        print(f"workflow-service: {format % args}")

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        if self.allow_remote_origins:
            return True
        try:
            host = urlsplit(origin).hostname
            return host == "localhost" or bool(host and ipaddress.ip_address(host).is_loopback)
        except ValueError:
            return False

    def _headers(self, status: int, content_type: str = "application/json", length: int | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        origin = self.headers.get("Origin")
        if origin and self._origin_allowed():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        data = (json.dumps(_sanitize_value(payload), ensure_ascii=False) + "\n").encode()
        self._headers(status, length=len(data))
        self.wfile.write(data)

    def _reject_origin(self) -> bool:
        if self._origin_allowed():
            return False
        self._json(HTTPStatus.FORBIDDEN, {"error": "Only loopback browser origins are allowed"})
        return True

    def _read_json_body(self, max_length: int = 65536) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > max_length:
            raise PatchbenchError(f"Request body must be 1–{max_length} bytes")
        payload = json.loads(self.rfile.read(length))
        if not isinstance(payload, dict):
            raise PatchbenchError("Request body must be a JSON object")
        return payload

    def do_OPTIONS(self) -> None:
        if self._reject_origin():
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", "http://localhost"))
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        if self._reject_origin():
            return
        if self.path == "/api/health":
            self._json(HTTPStatus.OK, {"version": __version__, "schema_version": SERVICE_SCHEMA_VERSION, "capabilities": ["run", "run-landb-answer", "validate-case", *sorted(OPERATION_KINDS), "operations", "dry-run", "skip-evaluation", "cancel", "reconnect", "prompt-inspection", "dashboard-indexing"], "active_runs": self.registry.active(), "active_operations": self.registry.active_operations()})
            return
        parts = self.path.split("?")[0].strip("/").split("/")
        if len(parts) in {3, 4} and parts[:2] == ["api", "operations"]:
            operation_id = unquote(parts[2])
            operation = self.registry.operation_records.get(operation_id)
            if not operation:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Unknown operation"})
                return
            if len(parts) == 3:
                self._json(HTTPStatus.OK, {"operation_id": operation.operation_id, "kind": operation.kind, "status": operation.status, "finished": operation.finished, "last_seq": operation.events[-1]["seq"] if operation.events else -1})
                return
            if parts[3] == "events":
                self._stream(operation)
                return
        if len(parts) in {3, 4} and parts[:2] == ["api", "runs"]:
            run_id = unquote(parts[2])
            record = self.registry.records.get(run_id)
            if not record:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Unknown run"})
                return
            if len(parts) == 3:
                self._json(HTTPStatus.OK, {"run_id": record.run_id, "case_id": record.case_id, "status": record.status, "finished": record.finished, "last_seq": record.events[-1]["seq"] if record.events else -1})
                return
            if parts[3] == "prompt":
                prompt = self.registry.prompt_for(record)
                if prompt is None:
                    self._json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "Prompt is not available yet"},
                    )
                else:
                    self._json(
                        HTTPStatus.OK,
                        {"run_id": record.run_id, "prompt": prompt},
                    )
                return
            if parts[3] == "events":
                self._stream(record)
                return
        self._json(HTTPStatus.NOT_FOUND, {"error": "Unknown endpoint"})

    def _stream(self, record: RunRecord | OperationRecord) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        origin = self.headers.get("Origin")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.end_headers()
        next_seq = 0
        try:
            while True:
                with record.condition:
                    pending = [
                        event for event in record.events if event["seq"] >= next_seq
                    ]
                    while not pending and not record.finished:
                        record.condition.wait(timeout=15)
                        pending = [
                            event for event in record.events if event["seq"] >= next_seq
                        ]
                    finished = record.finished
                for event in pending:
                    self.wfile.write((json.dumps(event, ensure_ascii=False) + "\n").encode())
                    self.wfile.flush()
                if pending:
                    next_seq = pending[-1]["seq"] + 1
                if finished:
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.close_connection = True

    def do_POST(self) -> None:
        if self._reject_origin():
            return
        parts = self.path.split("?")[0].strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["api", "cases"] and parts[3] == "validate":
            try:
                payload = self._read_json_body()
                case_id = unquote(parts[2])
                if payload.get("case_id") not in {None, case_id}:
                    raise PatchbenchError("Case ID in request body does not match URL")
                payload["case_id"] = case_id
                self._json(HTTPStatus.OK, self.registry.validate_case(payload))
            except (PatchbenchError, json.JSONDecodeError, ValueError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:
                self._json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": f"Case validation failed: {exc}"},
                )
            return
        if self.path == "/api/operations":
            try:
                payload = self._read_json_body(max_length=2_000_000)
                operation = self.registry.register_operation(payload)
                self._json(
                    HTTPStatus.ACCEPTED,
                    {
                        "operation_id": operation.operation_id,
                        "kind": operation.kind,
                    },
                )
            except (PatchbenchError, json.JSONDecodeError, ValueError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if self.path == "/api/runs":
            try:
                payload = self._read_json_body()
                record = self.registry.register(payload)
                self._json(HTTPStatus.ACCEPTED, {"run_id": record.run_id})
            except (PatchbenchError, json.JSONDecodeError, ValueError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "cancel":
            record = self.registry.records.get(unquote(parts[2]))
            if not record:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Unknown run"})
            elif record.finished:
                self._json(HTTPStatus.CONFLICT, {"error": "Run is already finished"})
            else:
                record.cancel_requested = True
                self._json(HTTPStatus.ACCEPTED, {"run_id": record.run_id, "status": "cancelling"})
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "Unknown endpoint"})


def serve(*, root: str | Path = ".", host: str = "127.0.0.1", port: int = 8765, allow_remote: bool = False) -> None:
    """Run the explicitly opted-in workflow service."""
    if not allow_remote:
        try:
            loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise PatchbenchError("Refusing a non-loopback host without --allow-remote")
    project_root = Path(root).expanduser().resolve()
    if not (project_root / "cases").is_dir():
        raise PatchbenchError(f"PatchBench cases directory not found under {project_root}")
    registry = RunRegistry(project_root)
    handler = type(
        "BoundWorkflowRequestHandler",
        (WorkflowRequestHandler,),
        {"registry": registry, "allow_remote_origins": allow_remote},
    )
    server = ThreadingHTTPServer((host, port), handler)
    print(f"PatchBench workflow service listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        registry.cancel_active()
        # Wait for adapters to terminate their solver process groups so Ctrl-C
        # cannot leave an agent running after the service exits.
        registry.executor.shutdown(wait=True, cancel_futures=True)
        registry.operation_executor.shutdown(wait=True, cancel_futures=True)
