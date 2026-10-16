from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import sys
from pathlib import Path

from patchbench.commands import (
    evaluate_patch,
    report,
    prepare_solver_prompt,
    run_agent,
    validate_case,
    verify_agent_image,
)
from patchbench.exceptions import PatchbenchError
from patchbench.catalog import read_metadata, resolve_case_reference
from patchbench.repositories import load_repository
from patchbench.local_landb_mcp_smoke import (
    run_patchbench_dev_smoke_test,
    run_patchbench_smoke_test,
)
from patchbench.terminal_output import RunDisplay, format_run_summary
from patchbench.command_progress import CommandProgress


def build_parser() -> argparse.ArgumentParser:
    class RepositoryIds(argparse.Action):
        def __call__(self, parser, namespace, values, option_string=None):
            # Preserve the historical single-repository parser/API shape.
            setattr(namespace,self.dest,values[0] if len(values)==1 else values)

    parser = argparse.ArgumentParser(prog="patchbench")
    parser.add_argument("--no-progress", action="store_true", help="Disable animated terminal activity indicators")
    subparsers = parser.add_subparsers(dest="command", required=True)
    from patchbench.batch import add_batch_parser

    add_batch_parser(subparsers)

    validate_parser = subparsers.add_parser(
        "validate-case", help="Validate a coding or answer-only case"
    )
    validate_parser.add_argument("case_dir")
    validate_parser.add_argument(
        "--container",
        action="store_true",
        help="Run validation in a container (requires Podman)",
    )
    validate_parser.add_argument(
        "--landb-mcp-repo",
        help="External landb-mcp checkout used for landb_answer validation",
    )

    evaluate_parser = subparsers.add_parser(
        "evaluate-patch",
        help="Evaluate an agent patch against a case on the host",
        description=(
            "Apply a patch and run the case's deterministic setup and tests on the host. "
            "This command does not use the container evaluator."
        ),
    )
    evaluate_parser.add_argument("case_dir")
    evaluate_parser.add_argument("--patch", required=True)

    report_parser = subparsers.add_parser(
        "report", help="Aggregate current and historical result JSON recursively"
    )
    report_parser.add_argument("path", help="Runs directory or individual run directory")

    export_parser = subparsers.add_parser(
        "export-dashboard",
        help="Export sanitized, read-only JSON for the Research Console",
    )
    export_parser.add_argument("--cases-dir", default="cases")
    export_parser.add_argument("--runs-dir", default="runs")
    export_parser.add_argument("--output", default="frontend/public/data")
    export_parser.add_argument(
        "--include-gold",
        action="store_true",
        help="Include researcher-only human reference patches; test patches are never exported",
    )
    export_parser.add_argument(
        "--watch",
        action="store_true",
        help="Refresh static dashboard JSON continuously; never runs agents or tests",
    )
    export_parser.add_argument(
        "--interval-seconds",
        type=float,
        default=2.0,
        help="Watch refresh interval in seconds (default: 2)",
    )
    export_parser.add_argument(
        "--freshness-seconds",
        type=int,
        default=60,
        help="Window used to label incomplete runs as recently active (default: 60)",
    )

    ingest_parser = subparsers.add_parser(
        "ingest-gitlab-mr", help="Create a frozen draft case from a GitLab merge request"
    )
    ingest_parser.add_argument("--project-id", required=True)
    ingest_parser.add_argument("--mr", required=True, type=int, dest="mr_iid")
    ingest_parser.add_argument("--case-id", required=True)
    ingest_parser.add_argument("--repo-path", required=True)
    ingest_parser.add_argument("--repository", help="Repository manifest ID for the draft")
    ingest_parser.add_argument("--output", required=True, dest="output_path")
    ingest_parser.add_argument(
        "--gitlab-url",
        help="GitLab base URL; defaults to GITLAB_URL or https://gitlab.example.invalid",
    )

    github_parser = subparsers.add_parser("ingest-github-pr", help="Import a merged Rucio GitHub PR as a frozen draft")
    github_parser.add_argument("--github-repo", default="rucio/rucio", choices=["rucio/rucio"])
    github_parser.add_argument("--pr", required=True, type=int, dest="pr_number")
    github_parser.add_argument("--case-id", required=True)
    github_parser.add_argument("--repo-path", required=True)
    github_parser.add_argument("--repository", default="rucio", choices=["rucio"])
    github_parser.add_argument("--output", required=True, dest="output_path")

    discover_parser = subparsers.add_parser(
        "discover-gitlab-cases",
        help="Rank merged GitLab merge requests for future benchmark cases",
    )
    discover_parser.add_argument("--project-id", required=True)
    discover_parser.add_argument("--repo", required=True)
    discover_parser.add_argument("--repo-path", required=True)
    discover_parser.add_argument("--output", required=True, dest="output_path")
    discover_parser.add_argument("--report", dest="report_path")
    discover_parser.add_argument("--limit", type=int, default=100)
    discover_parser.add_argument(
        "--gitlab-url",
        help="GitLab base URL; defaults to GITLAB_URL or https://gitlab.example.invalid",
    )

    pull_parser = subparsers.add_parser(
        "pull-cases", help="Synchronize a configured repository and stage merged GitLab MRs or GitHub PRs"
    )
    pull_parser.add_argument("repository", nargs='+', action=RepositoryIds, help="Repository manifest ID(s); multiple require --harvest")
    pull_parser.add_argument("--limit", type=int, default=100)
    pull_parser.add_argument("--label", action="append", default=[], help="Require a PR/MR label; repeat to require multiple labels")
    pull_parser.add_argument('--candidate',type=int,action='append',help='Pull a specific merged PR/MR number; repeat for a reviewed shortlist')
    pull_parser.add_argument("--dry-run", action="store_true", help="List candidates and planned drafts without creating drafts")
    pull_parser.add_argument("--no-sync", action="store_true", help="Use the existing local checkout")
    pull_parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    pull_parser.add_argument('--harvest', action='store_true', help='Greedily construct existing drafts using existing validators; pulling alone remains acquisition-only')
    pull_parser.add_argument('--session', help='Immutable harvesting session identifier')
    pull_parser.add_argument('--resume', action='store_true', help='Resume a harvesting checkpoint without repeating identical completed operations')
    pull_parser.add_argument('--time-budget-seconds', type=int, default=7200)
    pull_parser.add_argument('--model-budget-usd', type=float, default=10.0)
    pull_parser.add_argument('--profile', help='Reusable historical environment profile')
    pull_parser.add_argument('--dependency-cache', help='Frozen historical Maven cache')
    pull_parser.add_argument('--dependency-cache-map', type=Path, help='JSON mapping of Java versions to frozen historical Maven caches')
    pull_parser.add_argument('--mirror-agent-profile', help='Explicit optional semantic mirror profile')
    pull_parser.add_argument('--targeted-regression-profile', help='Explicit optional generated mirror oracle profile')
    pull_parser.add_argument('--testgen-agent-profile', help='Explicit optional Base-only testgen profile')
    pull_parser.add_argument('--model-pricing', type=Path, help='Verified provider token prices and enforceable per-call ceilings; omitted disables paid harvesting')
    pull_parser.add_argument('--agent-env-file')
    pull_parser.add_argument('--agent-ca-file')
    pull_parser.add_argument('--promote-validated', action='store_true', help='Promote only validated candidates with an input-matching behavioral review')
    pull_parser.add_argument('--cached-only', action='store_true', help='Harvest cached drafts only; make no discovery requests')

    curate_parser = subparsers.add_parser(
        "curate-drafts", help="Write advisory semantic triage evidence for existing coding drafts"
    )
    curate_parser.add_argument("repository", help="Repository manifest ID")
    curate_parser.add_argument("--agent-profile", required=True, help="Versioned, credential-free curation profile YAML")
    curate_parser.add_argument("--agent-env-file", help="External literal provider environment file")
    curate_parser.add_argument("--agent-ca-file", help="Public PEM CA bundle for a private provider gateway")
    curate_parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    curate_parser.add_argument("--dry-run", action="store_true", help="Show sanitized inputs and prompts without model calls or writes")
    curate_parser.add_argument("--json", action="store_true", dest="json_output", help="Print one structured batch report on stdout")

    prepare_parser = subparsers.add_parser("prepare-draft", help="Generate and verify hidden regression tests for one coding draft")
    prepare_parser.add_argument("repository")
    prepare_parser.add_argument("case_id")
    prepare_parser.add_argument("--agent-profile", required=True)
    prepare_parser.add_argument("--agent-env-file")
    prepare_parser.add_argument("--agent-ca-file")
    prepare_parser.add_argument("--max-attempts", type=int, default=3)
    prepare_parser.add_argument("--container", action="store_true")
    prepare_parser.add_argument("--dry-run", action="store_true")
    prepare_parser.add_argument("--json", action="store_true", dest="json_output")
    prepare_parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])

    environment_parser = subparsers.add_parser("prepare-environment", help="Verify an offline component baseline using existing tests")
    environment_parser.add_argument("repository")
    environment_parser.add_argument("component")
    environment_parser.add_argument("--reference", default="HEAD", help="Local known-good commit/ref; persisted as exact SHA")
    environment_parser.add_argument("--dependency-cache", help="Local Maven dependency cache; never source credentials")
    environment_parser.add_argument("--dry-run", action="store_true")
    environment_parser.add_argument("--json", action="store_true", dest="json_output")
    environment_parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])

    historical_parser = subparsers.add_parser('prepare-historical-pr-tests', help='Use exact developer PR tests on the historical base; no models')
    historical_parser.add_argument('draft', type=Path)
    historical_parser.add_argument('--test-node', action='append', help='Pytest node from a developer-changed file; defaults to all changed developer test files')
    historical_parser.add_argument('--image', help='Override the configured historical test image')
    historical_parser.add_argument('--profile', help='Reusable historical environment profile')
    historical_parser.add_argument('--dependency-cache', help='Prepared dependency cache; authoritative checks mount it read-only')
    historical_parser.add_argument('--regression-node', action='append', help='Unchanged, preselected historical regression node to preserve P2P coverage')
    historical_parser.add_argument('--dry-run', action='store_true', help='Inspect preparation without changing draft inputs or running tests')

    mirror_parser = subparsers.add_parser("mirror-draft", help="Reintroduce a historical bug and verify existing-test transitions")
    mirror_parser.add_argument("repository")
    mirror_parser.add_argument("case_id")
    mirror_parser.add_argument("--component", default="auto", help="Configured component or automatic Maven module detection")
    mirror_parser.add_argument("--max-attempts", type=int, default=2, help="Bounded mirror-agent attempts (1–3), reverse is always first")
    mirror_parser.add_argument("--agent-profile", help="Optional mirror-role profile, used only after reverse application fails")
    mirror_parser.add_argument("--targeted-regression-profile", help="Optional testgen-mirror-v1 fallback profile for executed NO_F2P mirrors")
    mirror_parser.add_argument("--agent-env-file")
    mirror_parser.add_argument("--agent-ca-file")
    mirror_parser.add_argument("--dry-run", action="store_true")
    mirror_parser.add_argument("--json", action="store_true", dest="json_output")
    mirror_parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])

    oracle_parser = subparsers.add_parser("prepare-mirror-oracle", help="Construct a hidden targeted test for an executed NO_F2P mirror")
    oracle_parser.add_argument("repository")
    oracle_parser.add_argument("case_id")
    oracle_parser.add_argument("--agent-profile", required=True)
    oracle_parser.add_argument("--agent-env-file")
    oracle_parser.add_argument("--agent-ca-file")
    oracle_parser.add_argument("--max-attempts", type=int, default=2)
    oracle_parser.add_argument("--container", action="store_true")
    oracle_parser.add_argument("--dry-run", action="store_true")
    oracle_parser.add_argument("--json", action="store_true", dest="json_output")
    oracle_parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])

    promote_parser = subparsers.add_parser(
        "promote-draft", help="Validate a curated draft and publish it under cases/"
    )
    promote_parser.add_argument("repository", help="Repository manifest ID")
    promote_parser.add_argument("case_id")
    promote_parser.add_argument("--container", action="store_true")
    promote_parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])

    run_parser = subparsers.add_parser("run", help="Run an agent on a case")
    run_parser.add_argument("case_dir")
    run_parser.add_argument(
        "--agent",
        required=True,
        choices=["dummy", "gold", "claude-code", "container-command"],
    )
    run_parser.add_argument("--run-id", required=True)
    run_parser.add_argument(
        "--prompt-mode",
        choices=["issue_only", "issue_with_hints"],
        default="issue_only",
        help="Prompt construction mode for the solving agent",
    )
    live_group = run_parser.add_mutually_exclusive_group()
    live_group.add_argument(
        "--live",
        dest="live",
        action="store_true",
        help="Show the interactive progress view (line logs when no TTY is available)",
    )
    live_group.add_argument(
        "--no-live",
        dest="live",
        action="store_false",
        help="Disable in-place rendering and write normal line-oriented progress logs",
    )
    run_parser.set_defaults(live=None)
    run_parser.add_argument(
        "--agent-timeout-seconds",
        type=int,
        help="Override the case timeout for agent execution only",
    )
    run_parser.add_argument(
        "--agent-model",
        help="Model requested from the Claude Code harness",
    )
    run_parser.add_argument(
        "--agent-max-turns",
        type=int,
        default=100,
        help="Maximum Claude Code interaction turns (default: 100)",
    )
    run_parser.add_argument(
        "--agent-debug",
        action="store_true",
        help="Enable sanitized Claude Code diagnostics",
    )
    run_parser.add_argument(
        "--container",
        action="store_true",
        help="Evaluate in Podman; also containerize agents that support it",
    )
    run_parser.add_argument(
        "--agent-env-file",
        metavar="PATH",
        help="External claude-env.sh sourced read-only inside the Claude Code container",
    )
    run_parser.add_argument(
        "--agent-ca-file",
        metavar="PATH",
        help="External PEM CA bundle mounted read-only for containerized Claude Code",
    )
    run_parser.add_argument(
        "--agent-network",
        default="pasta",
        help=(
            "Podman network for containerized Claude Code; defaults to rootless "
            "provider/proxy connectivity"
        ),
    )
    run_parser.add_argument(
        "--agent-image",
        help="Override the image for a container-capable solver",
    )
    run_parser.add_argument(
        "--agent-command",
        help="Override the shell command for a container-capable solver",
    )
    run_parser.add_argument(
        "--mcp-server",
        choices=["landb"],
        help="Enable one run-scoped stdio MCP server (currently: landb)",
    )
    run_parser.add_argument(
        "--mcp-command",
        help=(
            "Absolute MCP executable path; defaults to sibling landb-mcp on host "
            "or the versioned image executable in containers"
        ),
    )
    run_parser.add_argument(
        "--mcp-profile",
        choices=["curated", "broad"],
        help="Read-only LanDB MCP experimental profile (default: curated)",
    )
    run_parser.add_argument(
        "--mcp-env-file",
        metavar="PATH",
        help="External literal LanDB runtime environment file; never retained",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and print the solver prompt without executing an agent",
    )
    run_parser.add_argument(
        "--skip-evaluation",
        action="store_true",
        help="Run the solver and capture its patch without evaluating it",
    )

    build_env_parser = subparsers.add_parser(
        "build-env",
        help="Build a repository evaluation image",
    )
    build_env_parser.add_argument(
        "repo",
        help="Repository name (e.g., rucio)",
    )
    build_env_parser.add_argument('--profile', help='Build a versioned reusable environment profile')
    build_env_parser.add_argument('--timeout-seconds', type=int, default=1800)

    build_agent_parser = subparsers.add_parser(
        "build-agent-image", help="Build a dedicated coding-agent container image"
    )
    build_agent_parser.add_argument("agent", choices=["claude-code"])
    build_agent_parser.add_argument(
        "--image",
        default="patchbench-rucio-claude:tests-v2",
        help="Image tag to create",
    )
    build_agent_parser.add_argument(
        "--landb-mcp-repo",
        help="Build a clean external LanDB MCP checkout into the solver image",
    )

    verify_agent_parser = subparsers.add_parser(
        "verify-agent-image",
        help="Verify a solver image against frozen case tests and gold patches",
    )
    verify_agent_parser.add_argument(
        "case_dirs",
        nargs="+",
        help="One or more case directories to verify",
    )
    verify_agent_parser.add_argument(
        "--require-landb-mcp",
        action="store_true",
        help="Also verify the image's pinned curated LanDB MCP runtime",
    )
    verify_agent_parser.add_argument(
        "--image",
        help="Solver image tag to verify (default: repository manifest)",
    )

    smoke_parser = subparsers.add_parser(
        "smoke-test-landb-mcp",
        help="Run a synthetic PatchBench case through Claude, MCP, and fake LanDB",
    )
    smoke_parser.add_argument(
        "--landb-mcp-repo",
        help="External landb-mcp checkout (default: LANDB_MCP_REPO or ../landb-mcp)",
    )
    smoke_parser.add_argument("--model", help="Optional Claude Code model override")
    smoke_parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=180,
        help="Startup and Claude execution timeout (default: 180)",
    )
    smoke_parser.add_argument(
        "--container",
        action="store_true",
        help="Run the synthetic solver and evaluator in Podman",
    )
    smoke_parser.add_argument(
        "--agent-env-file",
        help="External Claude provider env file (required for --container)",
    )
    smoke_parser.add_argument(
        "--agent-ca-file",
        help="External PEM CA bundle for the Claude provider",
    )
    smoke_parser.add_argument(
        "--agent-image",
        help="Versioned Claude Code + LanDB MCP image (required for --container)",
    )
    smoke_parser.add_argument(
        "--agent-network",
        default="pasta",
        help="Narrow Podman network used by the solver",
    )
    smoke_parser.add_argument(
        "--artifacts-dir",
        help="Optional new directory in which to retain the synthetic run evidence",
    )
    smoke_parser.add_argument(
        "--live-dev",
        action="store_true",
        help="Explicitly use optional service instead of the fake backend (read-only)",
    )
    smoke_parser.add_argument(
        "--mcp-env-file",
        help="External literal LanDB DEV environment file (required by --live-dev)",
    )

    answer_parser = subparsers.add_parser(
        "run-landb-answer",
        help="Run one answer-only Claude/LanDB MCP retrieval task against fake REST",
    )
    answer_parser.add_argument("case_dir", help="A case_type: landb_answer directory")
    answer_parser.add_argument(
        "--condition", required=True, choices=["none", "curated", "broad"]
    )
    answer_parser.add_argument("--run-id", required=True, help="Immutable run identifier")
    answer_parser.add_argument(
        "--landb-mcp-repo",
        help="External landb-mcp checkout containing benchmark-v1",
    )
    answer_parser.add_argument(
        "--agent-env-file",
        help="External literal Claude provider environment file",
    )
    answer_parser.add_argument(
        "--agent-model", help="Model requested from the Claude Code harness"
    )
    answer_parser.add_argument("--timeout-seconds", type=int, default=300)
    answer_parser.add_argument(
        "--agent-max-turns",
        type=int,
        default=100,
        help="Fixed turn budget shared by none, curated, and broad (default: 100)",
    )
    answer_parser.add_argument(
        "--artifacts-root",
        help="Optional external artifact root (default: CASE_DIR/runs)",
    )
    answer_parser.add_argument(
        "--live-dev",
        action="store_true",
        help="Use the configured optional service read-only endpoint instead of fake REST",
    )
    answer_parser.add_argument(
        "--mcp-env-file",
        help="External literal LanDB DEV environment file (required by --live-dev)",
    )

    verify_dev_parser = subparsers.add_parser(
        "verify-landb-dev",
        help="Make one authenticated read-only DEV preflight without running an agent",
    )
    verify_dev_parser.add_argument("case_dir", help="A case_type: landb_answer directory")
    verify_dev_parser.add_argument(
        "--landb-mcp-repo",
        help="External landb-mcp checkout containing benchmark-v1",
    )
    verify_dev_parser.add_argument(
        "--mcp-env-file",
        required=True,
        help="External literal LanDB DEV environment file",
    )
    verify_dev_parser.add_argument("--timeout-seconds", type=int, default=60)

    serve_parser = subparsers.add_parser(
        "serve", help="Start the opt-in local workflow execution service"
    )
    serve_parser.add_argument("--dev", action="store_true", required=True)
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="Explicitly allow a non-loopback bind (no authentication is provided)",
    )

    for command_parser in dict.fromkeys(subparsers.choices.values()):
        command_parser.add_argument("--no-progress", action="store_true", default=argparse.SUPPRESS,
                                    help="Disable animated terminal activity indicators")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.no_progress and hasattr(args, "live"):
        args.live = False
    dedicated_display = args.command == "run" and not args.dry_run
    with CommandProgress(args.command, enabled=not args.no_progress and not dedicated_display) as progress:
        args.command_progress = progress
        with redirect_stderr(progress.log_stream), redirect_stdout(progress.output_stream):
            progress.result = _dispatch(args, parser)
        return progress.result


def _dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        if args.command in {"run-all", "runall"}:
            from patchbench.batch import run_all

            return run_all(args)
        if hasattr(args, "case_dir"):
            args.case_dir = resolve_case_reference(args.case_dir, Path.cwd())
        if hasattr(args, "case_dirs"):
            args.case_dirs = [resolve_case_reference(value, Path.cwd()) for value in args.case_dirs]
        if args.command == "validate-case":
            result = validate_case(
                args.case_dir,
                container=args.container,
                landb_mcp_repo=args.landb_mcp_repo,
            )
            print(f"validated={result.validated}")
            if result.error:
                print(f"error: {result.error}", file=sys.stderr)
            return 0 if result.validated else 1
        if args.command == "verify-agent-image":
            results = verify_agent_image(
                args.case_dirs,
                image_name=args.image,
                require_landb_mcp=args.require_landb_mcp,
            )
            for result in results:
                print(
                    f"case={result.case_id} validated={result.validated} "
                    f"base_exit={result.base_test_exit_code} "
                    f"gold_exit={result.gold_test_exit_code}"
                )
                if result.error:
                    print(f"error: {result.error}", file=sys.stderr)
            return 0 if all(result.validated for result in results) else 1
        if args.command == "evaluate-patch":
            result = evaluate_patch(args.case_dir, args.patch)
            print(
                f"patch_applied={result.patch_applied} "
                f"resolved={result.resolved} failure_mode={result.failure_mode}"
            )
            return 0 if result.resolved else 1
        if args.command == "report":
            print(report(args.path))
            return 0
        if args.command == "export-dashboard":
            from patchbench.dashboard_export import export_dashboard, watch_dashboard

            options = {
                "cases_dir": args.cases_dir,
                "runs_dir": args.runs_dir,
                "output": args.output,
                "include_gold": args.include_gold,
                "freshness_seconds": args.freshness_seconds,
            }
            if args.watch:
                print(
                    f"Watching dashboard artifacts every {args.interval_seconds:g}s; "
                    "press Ctrl+C to stop",
                    flush=True,
                )
                manifest = watch_dashboard(
                    **options,
                    interval_seconds=args.interval_seconds,
                )
            else:
                manifest = export_dashboard(**options)
            print(
                f"Exported {manifest['case_count']} cases and "
                f"{manifest['run_count']} runs to {args.output}"
            )
            return 0
        if args.command == "ingest-gitlab-mr":
            from patchbench.ingest import ingest_gitlab_mr

            output = ingest_gitlab_mr(
                project_id=args.project_id,
                mr_iid=args.mr_iid,
                case_id=args.case_id,
                repo_path=args.repo_path,
                output_path=args.output_path,
                gitlab_url=args.gitlab_url,
                repository_id=args.repository,
            )
            print(f"Created draft case: {output}")
            return 0
        if args.command == 'prepare-historical-pr-tests':
            import json
            from patchbench.historical_pr_tests import prepare_historical_pr_tests
            result = prepare_historical_pr_tests(args.draft, test_nodes=args.test_node, image=args.image,
                profile=args.profile, dependency_cache=args.dependency_cache,
                regression_nodes=args.regression_node, dry_run=args.dry_run)
            print(json.dumps(result, indent=2))
            return 0
        if args.command == "ingest-github-pr":
            from patchbench.github_ingest import ingest_github_pr
            output = ingest_github_pr(github_repo=args.github_repo, pr_number=args.pr_number,
                case_id=args.case_id, repo_path=args.repo_path, output_path=args.output_path,
                repository_id=args.repository)
            print(f"Created draft case: {output}")
            return 0
        if args.command == "discover-gitlab-cases":
            from patchbench.discovery import discover_gitlab_cases

            candidates = discover_gitlab_cases(
                project_id=args.project_id,
                repo=args.repo,
                repo_path=args.repo_path,
                output_path=args.output_path,
                report_path=args.report_path,
                limit=args.limit,
                gitlab_url=args.gitlab_url,
            )
            selected = sum(candidate["selected"] for candidate in candidates)
            print(f"Discovered {len(candidates)} merged MRs; {selected} selected for review")
            return 0
        if args.command == "pull-cases":
            from patchbench.case_import import pull_drafts
            repositories = [args.repository] if isinstance(args.repository,str) else args.repository

            if args.harvest:
                import json
                from datetime import datetime, timezone
                from patchbench.case_import import harvest_drafts
                if args.resume and not args.session:
                    raise PatchbenchError('--resume requires --session')
                if args.candidate and not args.cached_only and not args.dry_run and not args.resume:
                    for repository in repositories:
                        pull_drafts(repository, root=args.root, limit=args.limit, sync=not args.no_sync,
                                    labels=args.label, candidate_numbers=args.candidate)
                profiles = {k:v for k,v in {'semantic':args.mirror_agent_profile,
                    'oracle':args.targeted_regression_profile, 'testgen':args.testgen_agent_profile}.items() if v}
                cache = args.dependency_cache
                if args.dependency_cache_map:
                    if cache:
                        raise PatchbenchError('Choose --dependency-cache or --dependency-cache-map')
                    cache = json.loads(args.dependency_cache_map.read_text())
                    if not isinstance(cache,dict) or not all(isinstance(k,str) and isinstance(v,str) for k,v in cache.items()):
                        raise PatchbenchError('Dependency cache map must map Java versions to filesystem paths')
                # Reuse prior isolated acquisition rather than rediscovering the same MRs.
                directories = [p.parent for r in repositories for p in (args.root/'drafts'/r).glob('*/meta.yaml')]
                directories += [p.parent for r in repositories for p in
                    (args.root/'artifacts').glob(f'*/*/isolated-root/drafts/{r}/*/meta.yaml')]
                if args.candidate:
                    identifiers = {f'{repository}-{number}' for repository in repositories for number in args.candidate}
                    directories = [directory for directory in directories if directory.name in identifiers]
                result = harvest_drafts(repositories, root=args.root,
                    session_id=args.session or datetime.now(timezone.utc).strftime('harvest-%Y%m%dT%H%M%S%f'),
                    resume=args.resume, dry_run=args.dry_run, time_budget_seconds=args.time_budget_seconds,
                    model_budget_usd=args.model_budget_usd, profile=args.profile,
                    dependency_cache=cache, model_profiles=profiles,
                    model_pricing=args.model_pricing, agent_env_file=args.agent_env_file,
                    agent_ca_file=args.agent_ca_file, promote=args.promote_validated, candidate_dirs=directories,
                    discover=not args.cached_only, sync=not args.no_sync,
                    progress=lambda message: print(message,file=sys.stderr,flush=True))
                print(json.dumps(result, sort_keys=True, allow_nan=False))
                return 0
            if len(repositories) != 1:
                raise PatchbenchError('Multiple repositories require --harvest')

            result = pull_drafts(
                repositories[0], root=args.root, limit=args.limit, sync=not args.no_sync,
                labels=args.label, dry_run=args.dry_run,
                candidate_numbers=args.candidate,
            )
            print(
                f"repository={result.repository} candidates={result.candidates} "
                f"staged={len(result.staged)} existing={len(result.existing)} "
                f"skipped={len(result.skipped)} failed={len(result.failed)}"
            )
            if args.dry_run:
                print(f"planned={len(result.planned)} dry_run=true")
                for case_id in result.planned:
                    print(f"would_import={case_id}")
            for case_id, reason in result.skip_reasons.items():
                print(f"skipped={case_id} reason={reason}")
            for case_id, reason in result.failed.items():
                print(f"failed={case_id} reason={reason}")
            for case_id, options in result.construction_options.items():
                print(f"case={case_id} construction_options={','.join(options)}")
            report_name = "last-pull-preview.json" if args.dry_run else "last-pull.json"
            print(f"report={args.root / 'artifacts' / 'imports' / args.repository / report_name}")
            return 1 if result.failed else 0
        if args.command == "curate-drafts":
            import json
            from dataclasses import asdict
            from patchbench.curation.workflow import curate_drafts

            result = curate_drafts(
                args.repository, root=args.root, agent_profile=args.agent_profile,
                agent_env_file=args.agent_env_file, dry_run=args.dry_run,
                agent_ca_file=args.agent_ca_file,
                progress=args.command_progress.update_case,
            )
            if args.json_output:
                print(json.dumps(result.to_dict(), sort_keys=True, allow_nan=False))
            elif args.dry_run:
                for preview in result.previews:
                    print(f"candidate={preview['candidate_id']} input_digest={preview['input_digest']}")
                    print(preview["prompt"])
            else:
                for record in result.results:
                    print(f"candidate={record.candidate_id} recommendation={record.recommendation} status={record.status}")
            for candidate_id, message in result.failures.items():
                print(f"candidate={candidate_id} error={message}", file=sys.stderr)
            for record in result.results:
                for diagnostic in record.validation_diagnostics:
                    print(f"candidate={record.candidate_id} diagnostic="
                          + json.dumps(asdict(diagnostic), sort_keys=True, ensure_ascii=True), file=sys.stderr)
            return 1 if result.failures else 0
        if args.command == "prepare-draft":
            import json
            from patchbench.preparation.workflow import prepare_draft
            result = prepare_draft(args.repository, args.case_id, root=args.root,
                agent_profile=args.agent_profile, max_attempts=args.max_attempts,
                container=args.container, dry_run=args.dry_run,
                agent_env_file=args.agent_env_file, agent_ca_file=args.agent_ca_file,
                progress=args.command_progress.update_case)
            if args.json_output:
                print(json.dumps(result, sort_keys=True, allow_nan=False))
            else:
                print(f"case={args.case_id} status={result['status']} failure={result['failure_category']}")
            return 0 if result['status'] in {"prepared", "planned"} else 1
        if args.command == "prepare-mirror-oracle":
            import json
            from patchbench.mirroring.regression import prepare_mirror_oracle
            if not args.container and not args.dry_run:
                raise PatchbenchError('Targeted regression verification requires --container')
            result = prepare_mirror_oracle(args.repository, args.case_id, root=args.root,
                agent_profile=args.agent_profile, agent_env_file=args.agent_env_file,
                agent_ca_file=args.agent_ca_file,max_attempts=args.max_attempts,dry_run=args.dry_run)
            print(json.dumps(result,sort_keys=True,allow_nan=False) if args.json_output else
                  f"case={args.case_id} status={result['status']} reason={result['reason']}")
            return 0 if result['status'] in {'prepared','planned'} else 1
        if args.command in {"prepare-environment", "mirror-draft"}:
            import json
            if args.command == "prepare-environment":
                from patchbench.mirroring.environment import prepare_environment
                result = prepare_environment(args.repository, args.component, root=args.root,
                    reference=args.reference, dry_run=args.dry_run, dependency_cache=args.dependency_cache)
                success = args.dry_run or result['environment_eligible']
            else:
                from patchbench.mirroring.workflow import mirror_draft
                result = mirror_draft(args.repository, args.case_id, args.component, root=args.root, max_attempts=args.max_attempts,
                    agent_profile=args.agent_profile, agent_env_file=args.agent_env_file,
                    agent_ca_file=args.agent_ca_file, dry_run=args.dry_run,
                    progress=args.command_progress.update_case)
                if result.get('reason') == 'no_f2p_tests' and args.targeted_regression_profile and not args.dry_run:
                    from patchbench.mirroring.regression import prepare_mirror_oracle
                    result = prepare_mirror_oracle(args.repository,args.case_id,root=args.root,
                        agent_profile=args.targeted_regression_profile,agent_env_file=args.agent_env_file,
                        agent_ca_file=args.agent_ca_file,max_attempts=args.max_attempts)
                success = result['status'] in {'prepared', 'planned'}
            print(json.dumps(result, sort_keys=True, allow_nan=False) if args.json_output
                  else f"status={result.get('status', result.get('environment_eligible'))} reason={result['reason']}")
            return 0 if success else 1
        if args.command == "promote-draft":
            from patchbench.case_import import promote_draft_case

            published = promote_draft_case(
                args.repository, args.case_id, root=args.root, container=args.container
            )
            print(f"Published validated case: {published}")
            return 0
        if args.command == "run":
            if args.dry_run:
                print(prepare_solver_prompt(args.case_dir, args.prompt_mode, args.agent))
                return 0
            display = RunDisplay(
                case_id=read_metadata(args.case_dir)["id"],
                run_id=args.run_id,
                agent=args.agent,
                max_turns=(args.agent_max_turns if args.agent == "claude-code" else None),
                live=args.live,
            )
            display.start()
            try:
                result = run_agent(
                    args.case_dir,
                    args.agent,
                    args.run_id,
                    container=args.container,
                    agent_env_file=args.agent_env_file,
                    agent_ca_file=args.agent_ca_file,
                    agent_image=args.agent_image,
                    agent_command=args.agent_command,
                    agent_timeout_seconds=args.agent_timeout_seconds,
                    agent_model=args.agent_model,
                    agent_max_turns=args.agent_max_turns,
                    agent_debug=args.agent_debug,
                    agent_network=args.agent_network,
                    # Default to live observability in interactive use.  The display
                    # automatically falls back to line logs for CI/non-TTY streams.
                    live=args.live is not False,
                    prompt_mode=args.prompt_mode,
                    progress=display.update,
                    mcp_server=args.mcp_server,
                    mcp_command=args.mcp_command,
                    mcp_profile=args.mcp_profile,
                    mcp_env_file=args.mcp_env_file,
                    skip_evaluation=args.skip_evaluation,
                )
                display.complete(result)
            except Exception:
                display.update(
                    "completed",
                    status="failed",
                    evaluation_status="Aborted",
                )
                raise
            finally:
                display.close()
            print(
                format_run_summary(
                    result,
                    color=display.color_enabled,
                    unicode_enabled=display.unicode_enabled,
                ),
                file=sys.stderr,
            )
            print(f"resolved={result.resolved} failure_mode={result.failure_mode}")
            return 0 if result.resolved else 1
        if args.command == "build-env":
            from patchbench.containers import build_image

            image_name = build_image(args.repo, profile=args.profile, timeout_seconds=args.timeout_seconds)
            print(f"Built image: {image_name}")
            return 0
        if args.command == "build-agent-image":
            from patchbench.containers import build_claude_code_image

            image_name = build_claude_code_image(
                args.image,
                landb_mcp_repo=args.landb_mcp_repo,
            )
            print(f"Built agent image: {image_name}")
            return 0
        if args.command == "smoke-test-landb-mcp":
            smoke_function = (
                run_patchbench_dev_smoke_test
                if args.live_dev
                else run_patchbench_smoke_test
            )
            smoke_arguments = {
                "landb_mcp_repo": args.landb_mcp_repo or str(load_repository("landb-mcp").checkout()),
                "model": args.model,
                "timeout_seconds": args.timeout_seconds,
                "container": args.container,
                "agent_env_file": args.agent_env_file,
                "agent_ca_file": args.agent_ca_file,
                "agent_image": args.agent_image,
                "agent_network": args.agent_network,
                "artifacts_dir": args.artifacts_dir,
                "progress": lambda message: print(
                    message, file=sys.stderr, flush=True
                ),
            }
            if args.live_dev:
                if not args.mcp_env_file:
                    raise PatchbenchError("--live-dev requires --mcp-env-file")
                smoke_arguments["mcp_env_file"] = args.mcp_env_file
            elif args.mcp_env_file:
                raise PatchbenchError("--mcp-env-file requires --live-dev")
            evidence = smoke_function(**smoke_arguments)
            if args.live_dev:
                print(
                    "smoke_test=passed synthetic_run=true benchmark_performance=false "
                    "backend=cern-dev read_only=true "
                    f"mode={evidence.run.agent_execution_mode} "
                    f"mcp_calls={evidence.run.mcp_tool_call_count} "
                    f"resolved={str(evidence.run.resolved).lower()}"
                )
                return 0
            print(
                "smoke_test=passed synthetic_run=true benchmark_performance=false "
                f"mode={evidence.run.agent_execution_mode} "
                f"mcp_calls={evidence.run.mcp_tool_call_count} "
                f"http={evidence.request_method} "
                f"path={evidence.request_path} "
                f"status={evidence.response_status} "
                f"resolved={str(evidence.run.resolved).lower()}"
            )
            return 0
        if args.command == "run-landb-answer":
            from patchbench.landb_answer_benchmark import run_answer_benchmark

            result = run_answer_benchmark(
                args.case_dir,
                landb_mcp_repo=args.landb_mcp_repo,
                condition=args.condition,
                run_id=args.run_id,
                agent_env_file=args.agent_env_file,
                agent_model=args.agent_model,
                timeout_seconds=args.timeout_seconds,
                max_turns=args.agent_max_turns,
                artifacts_root=args.artifacts_root,
                live_dev=args.live_dev,
                mcp_env_file=args.mcp_env_file,
                progress=lambda message: print(message, file=sys.stderr, flush=True),
            )
            print(result.final_answer)
            print(
                f"task={result.task_id} condition={result.condition} "
                f"answer_correct={str(result.answer_correct).lower()} "
                f"answer_format_correct={str(result.answer_format_correct).lower()} "
                f"tool_selection_correct={str(result.tool_selection_correct).lower()} "
                f"http_request_correct={str(result.http_request_correct).lower()} "
                f"redundant_read_only_calls={result.redundant_read_only_call_count} "
                f"read_only={str(result.read_only).lower()} "
                f"task_success={str(result.task_success).lower()}",
                file=sys.stderr,
            )
            if result.agent_error:
                print(f"error: {result.agent_error}", file=sys.stderr)
                return 1
            return 0 if result.read_only else 1
        if args.command == "verify-landb-dev":
            from patchbench.landb_answer_benchmark import verify_live_dev_case

            status = verify_live_dev_case(
                args.case_dir,
                landb_mcp_repo=args.landb_mcp_repo,
                mcp_env_file=args.mcp_env_file,
                timeout_seconds=args.timeout_seconds,
            )
            print(f"dev_preflight=passed status={status}")
            return 0
        if args.command == "serve":
            from patchbench.server import serve

            serve(root=Path.cwd(), host=args.host, port=args.port, allow_remote=args.allow_remote)
            return 0
    except PatchbenchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
