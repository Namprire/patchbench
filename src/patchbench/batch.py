"""Sequential execution and durable summaries for mixed case batches."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import time
import tempfile
from uuid import uuid4

from patchbench.catalog import case_runs_dir, case_type, discover_cases, read_metadata
from patchbench.commands import run_agent
from patchbench.exceptions import PatchbenchError
from patchbench.landb_answer_benchmark import run_answer_benchmark
from patchbench.terminal_output import RunDisplay
from patchbench.batch_lock import batch_lock
from patchbench.runner import ensure_disk_space


def add_batch_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "run-all", aliases=["runall"], help="Run all coding and answer cases and summarize results"
    )
    parser.add_argument("--cases-dir", default="cases")
    parser.add_argument("--case-id", action="append", help="Select a case ID (repeatable; default: all)")
    parser.add_argument("--case-type", choices=["all", "coding", "landb_answer"], default="all")
    parser.add_argument("--run-id", help="Batch identifier (default: generated timestamp and suffix)")
    parser.add_argument("--agent", choices=["dummy", "gold", "claude-code", "container-command"], default="claude-code")
    parser.add_argument("--agent-model")
    parser.add_argument("--agent-env-file")
    parser.add_argument("--agent-login-dir", help="Dedicated external Claude SSO login directory")
    parser.add_argument("--agent-ca-file", help="CA bundle for containerized coding runs")
    parser.add_argument("--agent-image", help="Override solver image for coding runs")
    parser.add_argument("--agent-command", help="Override solver command for coding runs")
    parser.add_argument("--agent-network", default="pasta")
    parser.add_argument("--agent-debug", action="store_true")
    parser.add_argument("--agent-max-turns", type=int, default=100, help="Coding turn budget (default: 100)")
    parser.add_argument("--agent-timeout-seconds", type=int, help="Coding solver timeout override")
    parser.add_argument("--container", action="store_true", help="Containerize coding runs; answer runs use the host")
    parser.add_argument("--prompt-mode", choices=["issue_only", "issue_with_hints"], default="issue_only")
    parser.add_argument("--condition", choices=["none", "curated", "broad"], default="curated", help="Answer-case MCP condition (default: curated)")
    parser.add_argument("--landb-mcp-repo")
    parser.add_argument("--answer-max-turns", type=int, default=100)
    parser.add_argument("--answer-timeout-seconds", type=int, default=300)
    parser.add_argument("--progress-file", type=Path, help="Publish current-case status for the shared live viewer")
    parser.add_argument("--live", action="store_true", help="Show the existing per-case live display")


def run_all(args: argparse.Namespace) -> int:
    with batch_lock(args.cases_dir):
        return _run_all(args)


def _run_all(args: argparse.Namespace) -> int:
    progress_file = getattr(args, "progress_file", None)
    live = getattr(args, "live", False)
    cases = []
    selected = set(getattr(args, "case_id", None) or [])
    available = discover_cases(args.cases_dir)
    if selected - available.keys():
        raise PatchbenchError(f"Unknown case IDs: {', '.join(sorted(selected - available.keys()))}")
    for identifier, directory in sorted(available.items()):
        if selected and identifier not in selected:
            continue
        kind = case_type(read_metadata(directory))
        if args.case_type in ("all", kind):
            cases.append((identifier, directory, kind))
    if not cases:
        raise PatchbenchError("No matching cases found")
    if any(kind == "landb_answer" for _, _, kind in cases) and args.agent != "claude-code":
        raise PatchbenchError("Answer cases require --agent claude-code; use --case-type coding for other agents")
    login_dir = getattr(args, "agent_login_dir", None)
    if login_dir is not None:
        if args.agent_env_file is not None or args.agent != "claude-code":
            raise PatchbenchError("--agent-login-dir requires claude-code and cannot be combined with --agent-env-file")
        if any(kind == "coding" for _, _, kind in cases) and not args.container:
            raise PatchbenchError("SSO coding runs require --container")
    run_id = args.run_id or f"all-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid4().hex[:8]}"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id):
        raise PatchbenchError("Run ID may contain only letters, numbers, '.', '_' and '-'")
    batch_dir = Path(args.cases_dir).expanduser().resolve().parent / "artifacts" / "batches" / run_id
    # Check the whole batch before executing anything, to avoid partial reruns.
    if batch_dir.exists():
        raise PatchbenchError(f"Batch already exists: {batch_dir}")
    for _, directory, _ in cases:
        for existing in (case_runs_dir(directory) / run_id, directory / "runs" / run_id):
            if existing.exists() or existing.is_symlink():
                raise PatchbenchError(f"Run already exists: {existing}")
    batch_dir.mkdir(parents=True)
    summary = {"run_id": run_id, "total": len(cases), "passed": 0, "failed": 0, "errors": 0, "results": []}
    summary_path = batch_dir / "summary.json"

    def save():
        temporary = batch_dir / "summary.json.tmp"
        temporary.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        temporary.replace(summary_path)

    save()
    started = time.monotonic()
    for index, (identifier, directory, kind) in enumerate(cases, 1):
        # Resource exhaustion is batch-wide, not a failure of the next case.
        ensure_disk_space(batch_dir)
        ensure_disk_space(Path(tempfile.gettempdir()))
        print(f"[{index}/{len(cases)}] {identifier} ({kind})", file=sys.stderr, flush=True)
        record = {"case_id": identifier, "case_type": kind, "result_path": str(case_runs_dir(directory) / run_id / "result.json")}
        display = None
        if progress_file or live:
            display = RunDisplay(case_id=f"{identifier} [{index}/{len(cases)}]", run_id=run_id,
                agent=args.agent, max_turns=args.agent_max_turns if kind == "coding" else args.answer_max_turns,
                live=live, status_path=progress_file)
            display.start()
            if kind == "landb_answer":
                display.update("agent", container_status="Not used", last_activity="Preparing answer task")

        def coding_progress(phase, **fields):
            if not live:
                print(f"{identifier}: {phase} {fields.get('status', '')}", file=sys.stderr, flush=True)
            if display:
                display.update(phase, **fields)

        def answer_progress(message):
            if not live:
                print(message, file=sys.stderr, flush=True)
            if display:
                display.update("analysis", status="running", last_activity=message)

        try:
            if kind == "coding":
                result = run_agent(
                    directory, args.agent, run_id,
                    container=args.container, agent_env_file=args.agent_env_file,
                    agent_ca_file=args.agent_ca_file, agent_image=args.agent_image,
                    agent_command=args.agent_command, agent_network=args.agent_network,
                    agent_debug=args.agent_debug, agent_model=args.agent_model,
                    agent_max_turns=args.agent_max_turns,
                    agent_timeout_seconds=args.agent_timeout_seconds,
                    prompt_mode=args.prompt_mode,
                    progress=coding_progress,
                    **({"agent_login_dir": login_dir} if login_dir is not None else {}),
                )
                if getattr(result, "agent_termination_reason", None) == "interrupted":
                    raise KeyboardInterrupt
                success = result.resolved
                record.update(resolved=result.resolved, failure_mode=result.failure_mode)
            else:
                result = run_answer_benchmark(
                    directory, landb_mcp_repo=args.landb_mcp_repo,
                    condition=args.condition, run_id=run_id,
                    agent_env_file=args.agent_env_file, agent_model=args.agent_model,
                    timeout_seconds=args.answer_timeout_seconds, max_turns=args.answer_max_turns,
                    progress=answer_progress,
                    **({"progress_callback": display.update} if display else {}),
                    **({"agent_login_dir": login_dir} if login_dir is not None else {}),
                )
                if getattr(result, "agent_details", {}).get("agent_termination_reason") == "interrupted":
                    raise KeyboardInterrupt
                success = result.task_success and not result.agent_error
                record.update(task_success=result.task_success, answer_correct=result.answer_correct,
                              read_only=result.read_only, agent_error=result.agent_error)
            record["status"] = "passed" if success else "failed"
            if display:
                display.update("completed", status="success" if success else "failed",
                    tests_status=("All tests passed" if success else display.tests_status) if kind == "coding"
                                 else ("Answer correct" if success else "Answer failed"),
                    evaluation_status="Successful" if success else "Failed")
        except Exception as exc:
            # Keep errors case-local; interrupts still stop the batch.
            record.update(status="error", error=str(exc))
            if display:
                display.update("completed", status="failed", evaluation_status="Failed", last_activity=str(exc))
        finally:
            if display:
                display.close()
        summary[{"passed": "passed", "failed": "failed", "error": "errors"}[record["status"]]] += 1
        summary["results"].append(record)
        save()

    if progress_file:
        display = RunDisplay(case_id="All cases", run_id=run_id, agent=args.agent,
                             max_turns=None, live=False, status_path=progress_file)
        display.started = started
        display.update("completed", status="success" if summary['passed'] == len(cases) else "failed",
                       tests_status=f"{summary['passed']}/{len(cases)} cases passed",
                       evaluation_status="Completed",
                       last_activity=f"{summary['failed']} failed, {summary['errors']} errors")
        display.close()

    print(f"\nBatch {run_id}: {summary['passed']}/{summary['total']} passed, "
          f"{summary['failed']} failed, {summary['errors']} errors")
    for record in summary["results"]:
        detail = record.get("error") or record.get("agent_error") or record.get("failure_mode") or ""
        print(f"{record['case_id']}: {record['status']} {detail}".rstrip())
    print(f"Summary: {summary_path}")
    return 0 if summary["passed"] == summary["total"] else 1
