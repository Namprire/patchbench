from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import ssl
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from patchbench.agents import AGENT_REGISTRY
from patchbench.agents.base import AgentOutput, FailureMode
from patchbench.agents.claude_code import add_completion_protocol, claude_result_error, incomplete_execution_error
from patchbench.catalog import read_metadata, case_type, validation_path
from patchbench.repositories import load_repository
from patchbench.case import (
    ensure_case_output_dirs,
    is_safe_path_component,
    load_case,
    result_path_for_patch,
)
from patchbench.containers import (
    ContainerResult,
    ensure_podman_available,
    image_name_for_repo,
    inspect_landb_mcp_image,
    run_claude_in_container,
    run_in_container,
    run_solver_in_container,
)
from patchbench.exceptions import PatchbenchError
from patchbench.gitutils import (
    apply_patch,
    compare_patches,
    diff_stats,
    ensure_commit_exists,
    ensure_git_repo,
    temporary_checkout,
    temporary_worktree,
)
from patchbench.io import write_json
from patchbench.mcp import McpRunConfig, disabled_mcp_config, landb_mcp_config
from patchbench.mcp import write_mcp_config, write_mcp_evidence
from patchbench.models import PatchComparison, EvaluationResult, RunResult, ValidationResult
from patchbench.report import generate_report
from patchbench.runner import redact_secrets, run_command, sanitize_command, write_log


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CLAUDE_NETWORK = "pasta"
_PEM_CERTIFICATE = re.compile(
    r"-----BEGIN CERTIFICATE-----\s+.+?\s+-----END CERTIFICATE-----",
    re.DOTALL,
)
ProgressCallback = Callable[..., None]


def _progress(
    callback: ProgressCallback | None, phase: str, **fields: Any
) -> None:
    if callback is not None:
        try:
            callback(phase, **fields)
        except (BrokenPipeError, OSError, UnicodeError):
            # Progress is best-effort and must never change benchmark behavior.
            pass


def _validate_provider_ca_file(path: str | Path) -> tuple[Path, str, int]:
    """Validate a public PEM-only CA bundle without retaining its host path."""
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise PatchbenchError(f"Provider CA file does not exist: {resolved}")
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        raise PatchbenchError(f"Could not read provider CA file: {resolved}") from exc
    if len(data) > 2 * 1024 * 1024:
        raise PatchbenchError("Provider CA bundle exceeds the 2 MiB safety limit")
    if b"PRIVATE KEY" in data:
        raise PatchbenchError("Provider CA file must not contain a private key")
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise PatchbenchError("Provider CA file must be an ASCII PEM bundle") from exc
    certificates = _PEM_CERTIFICATE.findall(text)
    remainder = _PEM_CERTIFICATE.sub("", text).strip()
    if not certificates or remainder:
        raise PatchbenchError(
            "Provider CA file must contain only PEM CERTIFICATE blocks"
        )
    try:
        context = ssl.create_default_context()
        context.load_verify_locations(cadata="\n".join(certificates))
    except ssl.SSLError as exc:
        raise PatchbenchError("Provider CA file contains an invalid certificate") from exc
    return resolved, hashlib.sha256(data).hexdigest(), len(certificates)


def validate_case(
    case_dir: str | Path,
    container: bool = False,
    landb_mcp_repo: str | Path | None = None,
) -> ValidationResult:
    """
    Validate a coding case, or a deterministic answer-only LanDB case.

    Args:
        case_dir: Path to the case directory
        container: If True, run validation inside a container using Podman

    Returns:
        Case-type-specific validation result with a ``validated`` outcome
    """
    resolved_case_dir = Path(case_dir).expanduser().resolve()
    raw_meta = read_metadata(resolved_case_dir)
    if case_type(raw_meta) == "landb_answer":
        if container:
            raise PatchbenchError(
                "landb_answer validation is already local and credential-free; "
                "do not pass --container"
            )
        from patchbench.landb_answer_benchmark import validate_answer_case

        repository = landb_mcp_repo or load_repository(raw_meta.get("repository", "landb-mcp"), resolved_case_dir).checkout()
        return validate_answer_case(resolved_case_dir, repository)  # type: ignore[return-value]

    case = load_case(case_dir)
    ensure_case_output_dirs(case)
    ensure_git_repo(case.repo_path)
    ensure_commit_exists(case.repo_path, case.base_commit)

    output_path = validation_path(case.case_dir)

    try:
        if raw_meta.get('construction_mode') == 'historical_pr_tests_v1':
            from patchbench.historical_pr_tests import validate_historical_case
            result = validate_historical_case(case, container=container)
        elif case.oracle_mode in {"existing_tests", "existing_tests_with_regression"}:
            from patchbench.mirroring.validation import validate_existing_case
            result = validate_existing_case(case, container=container)
        elif raw_meta.get('validation_policy') == 'strict_f2p_v1':
            if not container:
                raise PatchbenchError('strict_f2p_v1 validation requires --container')
            if raw_meta.get('validation_image_identity'):
                from patchbench.historical_pr_tests import image_identity
                identity = raw_meta['validation_image_identity']
                if image_identity(case.image) != identity:
                    raise PatchbenchError('Evaluator image identity mismatch')
            else:
                identity = None
            result = _validate_case_container(case, collect_test_reports=True, image_name=identity)
        elif container:
            result = _validate_case_container(case)
        else:
            result = _validate_case_local(case)
    except Exception as exc:
        result = ValidationResult(
            case_id=case.id,
            base_commit=case.base_commit,
            test_patch_applies=False,
            base_test_exit_code=None,
            base_test_passes=False,
            gold_patch_applies=False,
            gold_test_exit_code=None,
            gold_test_passes=False,
            validated=False,
            error=f"Validation failed before test completion: {exc}",
        )

    return _write_validation(output_path, result)


def verify_agent_image(
    case_dirs: list[str | Path],
    *,
    image_name: str | None = None,
    require_landb_mcp: bool = False,
) -> list[ValidationResult]:
    """Verify a solver image against frozen cases without running an agent.

    The verification applies each case's test and gold patches only inside a
    temporary containerized checkout, then runs its declared setup and test
    commands. This is intentionally separate from solving: gold/test patches
    are never exposed to an agent run or persisted as run evidence.
    """
    if not case_dirs:
        raise PatchbenchError("verify-agent-image requires at least one case directory")

    ensure_podman_available()
    results: list[ValidationResult] = []
    for case_dir in case_dirs:
        case = load_case(case_dir)
        ensure_case_output_dirs(case)
        ensure_git_repo(case.repo_path)
        ensure_commit_exists(case.repo_path, case.base_commit)
        try:
            results.append(
                _validate_case_container(
                    case,
                    image_name=image_name or _solver_image(case, "claude-code"),
                    log_prefix="agent_image_",
                    verify_solver_tools=True,
                    verify_mcp_tools=require_landb_mcp,
                )
            )
        except Exception as exc:
            results.append(
                ValidationResult(
                    case_id=case.id,
                    base_commit=case.base_commit,
                    test_patch_applies=False,
                    base_test_exit_code=None,
                    base_test_passes=False,
                    gold_patch_applies=False,
                    gold_test_exit_code=None,
                    gold_test_passes=False,
                    validated=False,
                    error=f"Agent-image verification failed before test completion: {exc}",
                )
            )
    return results


def _copy_test_reports(checkout: Path, destination: Path) -> None:
    for report in checkout.rglob("*.xml"):
        if report.name == "patchbench-junit.xml" or (report.parent.name == "surefire-reports" and report.name.startswith("TEST-")):
            target = destination / report.relative_to(checkout)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(report, target)
            report.unlink()


def _validate_case_local(case, *, collect_test_reports: bool = False) -> ValidationResult:
    """Local validation (non-container)."""
    with temporary_worktree(case.repo_path, case.base_commit) as worktree_path:
        execution_cwd = _execution_cwd(worktree_path, case.workdir)

        test_patch_applies, test_patch_error = apply_patch(worktree_path, case.test_patch)
        if not test_patch_applies:
            return ValidationResult(
                case_id=case.id,
                base_commit=case.base_commit,
                test_patch_applies=False,
                base_test_exit_code=None,
                base_test_passes=False,
                gold_patch_applies=False,
                gold_test_exit_code=None,
                gold_test_passes=False,
                validated=False,
                error=f"Failed to apply test.patch: {test_patch_error}",
            )

        setup_error = _run_setup_commands(case, execution_cwd, case.logs_dir, prefix="base_setup")
        if setup_error is not None:
            return ValidationResult(
                case_id=case.id,
                base_commit=case.base_commit,
                test_patch_applies=True,
                base_test_exit_code=None,
                base_test_passes=False,
                gold_patch_applies=False,
                gold_test_exit_code=None,
                gold_test_passes=False,
                validated=False,
                error=f"Pre-gold setup failed: {setup_error}",
            )

        base_test = run_command(
            case.test_command,
            cwd=execution_cwd,
            env_overrides=case.env,
            timeout_seconds=case.timeout_seconds,
        )
        write_log(case.logs_dir / "base_with_test.log", base_test)
        if collect_test_reports:
            _copy_test_reports(worktree_path, case.logs_dir / "base_reports")
            from patchbench.preparation.evidence import classify_execution, report_summary
            stage = classify_execution(base_test.exit_code, report_summary(case.logs_dir / "base_reports"), base_test.stdout + base_test.stderr)
            if stage != "behavioural_failure":
                return ValidationResult(case.id, case.base_commit, True, base_test.exit_code,
                    base_test.exit_code == 0, False, None, False, False,
                    f"Preparation stopped at {stage} before Gold")

        gold_patch_applies, gold_patch_error = apply_patch(worktree_path, case.gold_patch)
        if not gold_patch_applies:
            return ValidationResult(
                case_id=case.id,
                base_commit=case.base_commit,
                test_patch_applies=True,
                base_test_exit_code=base_test.exit_code,
                base_test_passes=base_test.exit_code == 0,
                gold_patch_applies=False,
                gold_test_exit_code=None,
                gold_test_passes=False,
                validated=False,
                error=f"Failed to apply gold.patch: {gold_patch_error}",
            )

        setup_error = _run_setup_commands(case, execution_cwd, case.logs_dir, prefix="gold_setup")
        if setup_error is not None:
            return ValidationResult(
                case_id=case.id,
                base_commit=case.base_commit,
                test_patch_applies=True,
                base_test_exit_code=base_test.exit_code,
                base_test_passes=base_test.exit_code == 0,
                gold_patch_applies=True,
                gold_test_exit_code=None,
                gold_test_passes=False,
                validated=False,
                error=f"Post-gold setup failed: {setup_error}",
            )

        gold_test = run_command(
            case.test_command,
            cwd=execution_cwd,
            env_overrides=case.env,
            timeout_seconds=case.timeout_seconds,
        )
        write_log(case.logs_dir / "gold_with_test.log", gold_test)
        if collect_test_reports:
            _copy_test_reports(worktree_path, case.logs_dir / "gold_reports")

        return ValidationResult(
            case_id=case.id,
            base_commit=case.base_commit,
            test_patch_applies=True,
            base_test_exit_code=base_test.exit_code,
            base_test_passes=base_test.exit_code == 0,
            gold_patch_applies=True,
            gold_test_exit_code=gold_test.exit_code,
            gold_test_passes=gold_test.exit_code == 0,
            validated=(
                not base_test.timed_out
                and base_test.exit_code != 0
                and not gold_test.timed_out
                and gold_test.exit_code == 0
            ),
            error=_validation_outcome_error(
                base_test.exit_code,
                gold_test.exit_code,
                case.logs_dir,
            ),
        )


def _run_preparation_verification(case, image_name: str, script: str, destination: Path) -> ContainerResult:
    """Reuse the evaluator script in fresh Base, repeated Base, and Gold containers.

    Generated test code is untrusted too: BASE must not mount Gold or full Git history.
    GOLD runs separately and its outputs are never fed back to the test generator.
    """
    gold_start = script.index("if ! git apply --check --verbose --whitespace=nowarn " + shlex.quote(f"/patches/{case.gold_patch.name}"))
    base_start = script.index("(\n  cd ")
    scripts = {"base": script[:gold_start], "gold": script[:base_start] + script[gold_start:]}
    statuses = []
    original_base = None
    with temporary_checkout(case.repo_path, case.base_commit) as source:
        for stage in ("base", "base_repeat", "gold"):
            script_stage = "base" if stage == "base_repeat" else stage
            with tempfile.TemporaryDirectory(prefix=f"patchbench-verify-{stage}-") as directory:
                workspace = Path(directory)
                shutil.copyfile(case.test_patch, workspace / case.test_patch.name)
                if stage == "gold":
                    shutil.copyfile(case.gold_patch, workspace / case.gold_patch.name)
                result = run_in_container(
                    image_name, workspace, scripts[script_stage], env_overrides=case.env,
                    network="none",
                    additional_mounts=[(source, "/repo:ro"), (workspace, "/patches:ro")],
                    insecure_mounts=True,
                    timeout_seconds=(len(case.setup_commands) + 1) * case.timeout_seconds + 30,
                )
                for name in ("prepare.log", f"{script_stage}_with_test.log"):
                    path = workspace / name
                    if path.is_file() and not path.is_symlink():
                        shutil.copyfile(path, destination / (f"{stage}_with_test.log" if name.endswith("_with_test.log") else name))
                reports = workspace / f"{script_stage}_reports"
                if reports.is_dir() and not reports.is_symlink():
                    shutil.copytree(reports, destination / f"{stage}_reports", dirs_exist_ok=True)
                markers = workspace / "test_results.txt"
                if markers.is_file() and not markers.is_symlink():
                    marker_text = markers.read_text()
                    if stage == "base_repeat":
                        marker_text = marker_text.replace("BASE_", "BASE_REPEAT_")
                    statuses.append(marker_text)
                if result.returncode or result.timed_out or result.interrupted:
                    break
                if script_stage == "base":
                    from patchbench.preparation.evidence import classify_execution, report_summary
                    base_code = next((line.split("=", 1)[1] for line in statuses[-1].splitlines()
                                      if line.startswith("BASE_REPEAT_EXIT_CODE=" if stage == "base_repeat" else "BASE_EXIT_CODE=")), None) if statuses else None
                    log = workspace / "base_with_test.log"
                    base_stage = classify_execution(int(base_code) if base_code is not None else None,
                        report_summary(workspace / "base_reports"), log.read_text(errors="replace") if log.is_file() else "")
                    if base_stage != "behavioural_failure":
                        statuses.append(f"PREPARATION_BASE_STAGE={base_stage}\n")
                        break
                    inventory = report_summary(workspace / "base_reports")
                    if stage == "base":
                        original_base = inventory
                    elif original_base['inventory'] != inventory['inventory']:
                        statuses.append("PREPARATION_BASE_STAGE=unstable_oracle\n")
                        break
    (destination / "test_results.txt").write_text("\n".join(statuses))
    return result


def _validate_case_container(
    case,
    *,
    image_name: str | None = None,
    log_prefix: str = "",
    verify_solver_tools: bool = False,
    verify_mcp_tools: bool = False,
    collect_test_reports: bool = False,
) -> ValidationResult:
    """Container-based validation using Podman."""
    ensure_podman_available()
    if collect_test_reports:
        # Reports from an earlier validation cannot supply missing tests this time.
        for stage in ('base', 'base_repeat', 'gold'):
            reports = case.logs_dir / (stage + '_reports')
            if reports.is_symlink():
                reports.unlink()
            elif reports.exists():
                shutil.rmtree(reports)

    image_name = image_name or _evaluation_image(case)
    clone_dir = "/host/repo-clone"
    workdir_path = f"{clone_dir}/{case.workdir}" if case.workdir else clone_dir
    container_setup_commands = [shlex.join(command) for command in case.setup_commands]
    base_setup_script = _container_setup_script(
        container_setup_commands, "BASE_SETUP_EXIT_CODE", case.timeout_seconds
    )
    gold_setup_script = _container_setup_script(
        container_setup_commands, "GOLD_SETUP_EXIT_CODE", case.timeout_seconds
    )
    test_command = _bounded_shell_command(
        shlex.join(case.test_command), case.timeout_seconds
    )

    quoted_clone = shlex.quote(clone_dir)
    quoted_workdir = shlex.quote(workdir_path)
    quoted_base = shlex.quote(case.base_commit)
    quoted_test_patch = shlex.quote(f"/patches/{case.test_patch.name}")
    quoted_gold_patch = shlex.quote(f"/patches/{case.gold_patch.name}")
    smoke_check = ""
    if verify_solver_tools:
        smoke_check = """
if ! command -v python >/dev/null \\
  || ! command -v python3 >/dev/null \\
  || ! command -v pytest >/dev/null \\
  || ! command -v claude >/dev/null \\
  || ! python --version \\
  || ! python3 -m pip --version \\
  || ! pytest --version \\
  || ! claude --version; then
  echo AGENT_IMAGE_SMOKE_FAILED=1 >> /host/test_results.txt
  exit 0
fi
"""
    if verify_mcp_tools:
        smoke_check += """
if ! test -x /opt/landb-mcp/.venv/bin/landb-mcp \
  || ! /opt/landb-mcp/.venv/bin/python /opt/patchbench-verify-landb-mcp.py curated \
  || ! /opt/landb-mcp/.venv/bin/python /opt/patchbench-verify-landb-mcp.py broad; then
  echo AGENT_IMAGE_SMOKE_FAILED=1 >> /host/test_results.txt
  exit 0
fi
"""
    def report_script(stage):
        if not collect_test_reports:
            return ""
        # Snapshot reports before Gold, remove stale reports between executions.
        return fr"""
mkdir -p /host/{stage}_reports
find {quoted_clone} -type f \( -path '*/surefire-reports/TEST-*.xml' -o -name 'patchbench-junit.xml' \) -exec cp --parents {{}} /host/{stage}_reports/ \;
find {quoted_clone} -type f \( -path '*/surefire-reports/TEST-*.xml' -o -name 'patchbench-junit.xml' \) -delete
"""

    script = f"""
: > /host/test_results.txt
{smoke_check}
git config --global --add safe.directory /repo
git config --global --add safe.directory /repo/.git
if ! git clone /repo {quoted_clone} > /host/prepare.log 2>&1; then
  echo PREPARE_FAILED=1 >> /host/test_results.txt
  exit 0
fi
cd {quoted_clone}
if ! git checkout --detach {quoted_base}; then
  echo PREPARE_FAILED=1 >> /host/test_results.txt
  exit 0
fi
if ! git apply --check --verbose --whitespace=nowarn {quoted_test_patch} > /host/test-patch-check.log 2>&1 \
   || grep -Eiq 'offset[[:space:]]+[0-9]+[[:space:]]+lines?|with[[:space:]]+fuzz' /host/test-patch-check.log \
   || ! git apply --whitespace=nowarn {quoted_test_patch}; then
  echo TEST_PATCH_APPLIED=0 >> /host/test_results.txt
  exit 0
fi
echo TEST_PATCH_APPLIED=1 >> /host/test_results.txt
(
  cd {quoted_workdir}
  {base_setup_script}
  {test_command}
  echo "BASE_EXIT_CODE=$?" >> /host/test_results.txt
) > /host/base_with_test.log 2>&1
{report_script("base")}
if ! git apply --check --verbose --whitespace=nowarn {quoted_gold_patch} > /host/gold-patch-check.log 2>&1 \
   || grep -Eiq 'offset[[:space:]]+[0-9]+[[:space:]]+lines?|with[[:space:]]+fuzz' /host/gold-patch-check.log \
   || ! git apply --whitespace=nowarn {quoted_gold_patch}; then
  echo GOLD_PATCH_APPLIED=0 >> /host/test_results.txt
  exit 0
fi
echo GOLD_PATCH_APPLIED=1 >> /host/test_results.txt
(
  cd {quoted_workdir}
  {gold_setup_script}
  {test_command}
  echo "GOLD_EXIT_CODE=$?" >> /host/test_results.txt
) > /host/gold_with_test.log 2>&1
{report_script("gold")}
"""

    with tempfile.TemporaryDirectory() as tmpdir:
        if verify_solver_tools:
            # The Claude solver image runs as its non-root `node` user.
            # Temporary directories can be owner-only when PatchBench is
            # invoked through an automation wrapper, so make this short-lived
            # verification mount writable before asking the solver image to
            # clone into it.
            os.chmod(tmpdir, 0o777)
        test_patch_dest = Path(tmpdir) / case.test_patch.name
        gold_patch_dest = Path(tmpdir) / case.gold_patch.name
        shutil.copy(case.test_patch, test_patch_dest)
        shutil.copy(case.gold_patch, gold_patch_dest)

        if collect_test_reports:
            result = _run_preparation_verification(case, image_name, script, Path(tmpdir))
        else:
            result = run_in_container(
                image_name,
                Path(tmpdir),
                script,
                env_overrides=case.env,
                additional_mounts=[
                    (case.repo_path, "/repo:ro"),
                    (Path(tmpdir), "/patches:ro"),
                ],
                insecure_mounts=True,
                # This is a test-only verification container, not an agent run.
                # Running it as root avoids leaving host-user-namespace files that
                # Python cannot remove from its temporary directory afterwards.
                run_as_user="0" if verify_solver_tools else None,
                login_shell=not verify_solver_tools,
                timeout_seconds=(len(case.setup_commands) + 1)
                * case.timeout_seconds
                * 2
                + 30,
            )

        for log_name in ("prepare.log", "base_with_test.log", "base_repeat_with_test.log", "gold_with_test.log"):
            container_log = Path(tmpdir) / log_name
            if container_log.is_file():
                shutil.copy(container_log, case.logs_dir / f"{log_prefix}{log_name}")

        if collect_test_reports:
            for stage in ("base", "base_repeat", "gold"):
                reports = Path(tmpdir) / f"{stage}_reports"
                if reports.is_dir():
                    shutil.copytree(reports, case.logs_dir / f"{stage}_reports", dirs_exist_ok=True)

        if result.returncode != 0 or result.timed_out or result.interrupted:
            error_msg = (
                "Container validation was interrupted"
                if result.interrupted
                else result.stderr or result.stdout or "Container execution failed"
            )
            return ValidationResult(
                case_id=case.id,
                base_commit=case.base_commit,
                test_patch_applies=True,
                base_test_exit_code=None,
                base_test_passes=False,
                gold_patch_applies=False,
                gold_test_exit_code=None,
                gold_test_passes=False,
                validated=False,
                error=f"Container execution failed: {error_msg}",
            )

        results_file = Path(tmpdir) / "test_results.txt"
        if results_file.exists():
            results_content = results_file.read_text()
            parsed = {}
            for line in results_content.splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    parsed[key] = value
            base_exit = _optional_exit_code(parsed.get("BASE_EXIT_CODE"))
            gold_exit = _optional_exit_code(parsed.get("GOLD_EXIT_CODE"))

            if parsed.get("AGENT_IMAGE_SMOKE_FAILED") == "1":
                error = (
                    "Solver image is missing a required executable or Python toolchain "
                    "(python, python3, pip, pytest, or claude)"
                )
            elif parsed.get("PREPARE_FAILED") == "1":
                error = "Container validation could not clone or check out the base commit"
            elif parsed.get("TEST_PATCH_APPLIED") != "1":
                error = "Failed to apply test.patch"
            elif "BASE_SETUP_EXIT_CODE" in parsed:
                error = f"Pre-gold setup failed with exit code {parsed['BASE_SETUP_EXIT_CODE']}"
            elif "PREPARATION_BASE_STAGE" in parsed:
                error = f"Preparation stopped at {parsed['PREPARATION_BASE_STAGE']} before Gold"
            elif parsed.get("GOLD_PATCH_APPLIED") != "1":
                error = "Failed to apply gold.patch"
            elif "GOLD_SETUP_EXIT_CODE" in parsed:
                error = f"Post-gold setup failed with exit code {parsed['GOLD_SETUP_EXIT_CODE']}"
            else:
                error = _validation_outcome_error(base_exit, gold_exit, case.logs_dir)

            outcome = ValidationResult(
                case_id=case.id,
                base_commit=case.base_commit,
                test_patch_applies=parsed.get("TEST_PATCH_APPLIED") == "1",
                base_test_exit_code=base_exit,
                base_test_passes=base_exit == 0,
                gold_patch_applies=parsed.get("GOLD_PATCH_APPLIED") == "1",
                gold_test_exit_code=gold_exit,
                gold_test_passes=gold_exit == 0,
                validated=(
                    base_exit is not None
                    and base_exit not in {0, 124}
                    and gold_exit == 0
                ),
                error=error,
            )
            if collect_test_reports:
                from dataclasses import replace
                from patchbench.preparation.evidence import report_summary, rejection
                reason = rejection(outcome, report_summary(case.logs_dir / "base_reports"),
                                   report_summary(case.logs_dir / "gold_reports"))
                if reason is not None:
                    outcome = replace(outcome, validated=False, error=error or reason)
            return outcome

    detail = (result.stderr or result.stdout).strip()
    suffix = f": {detail}" if detail else ""
    return ValidationResult(
        case_id=case.id,
        base_commit=case.base_commit,
        test_patch_applies=False,
        base_test_exit_code=None,
        base_test_passes=False,
        gold_patch_applies=False,
        gold_test_exit_code=None,
        gold_test_passes=False,
        validated=False,
        error=f"Container validation failed to produce test exit codes{suffix}",
    )


def _validation_outcome_error(
    base_exit: int | None, gold_exit: int | None, logs_dir: Path
) -> str | None:
    if base_exit is None:
        return "Pre-gold test did not produce an exit code"
    if base_exit == 124:
        return "Pre-gold test timed out; the benchmark failure was not reproduced"
    if base_exit == 0:
        return (
            "Pre-gold test unexpectedly passed; test.patch does not reproduce the bug. "
            f"See {logs_dir / 'base_with_test.log'}"
        )
    if gold_exit is None:
        return "Post-gold test did not produce an exit code"
    if gold_exit == 124:
        return "Post-gold test timed out; gold.patch was not validated"
    if gold_exit != 0:
        return (
            f"Post-gold test failed with exit code {gold_exit}; gold.patch did not produce "
            f"a passing evaluation. See {logs_dir / 'gold_with_test.log'}"
        )
    return None


def evaluate_patch(case_dir: str | Path, patch: str | Path) -> EvaluationResult:
    case = load_case(case_dir)
    patch_path = Path(patch).expanduser().resolve()
    ensure_case_output_dirs(case)
    ensure_git_repo(case.repo_path)
    ensure_commit_exists(case.repo_path, case.base_commit)
    if not patch_path.is_file():
        raise FileNotFoundError(f"Patch file does not exist: {patch_path}")

    result_path = result_path_for_patch(case, patch_path)
    if case.oracle_mode in {"existing_tests", "existing_tests_with_regression"}:
        from patchbench.mirroring.validation import evaluate_existing_patch
        started = time.monotonic()
        evaluated = evaluate_existing_patch(case, patch_path, case.runs_dir / patch_path.stem)
        raw = read_metadata(case.case_dir)['benchmark']
        result = EvaluationResult(case.id, "", "", "", evaluated.patch_applied, evaluated.tests_pass,
            len(raw['f2p_tests']) if evaluated.tests_pass else 0, len(raw['f2p_tests']),
            len(raw['p2p_tests']) if evaluated.tests_pass else 0, len(raw['p2p_tests']),
            time.monotonic() - started, evaluated.files_changed, evaluated.lines_added, evaluated.lines_removed,
            "tests_pass" if evaluated.tests_pass else "tests_failed", evaluated.error, {})
        return _write_evaluation(result_path, result)

    result = EvaluationResult(
        case_id=case.id,
        agent="",
        run_id="",
        prompt_mode="",
        patch_applied=False,
        resolved=False,
        f2p_passed=0,
        f2p_total=0,
        p2p_passed=0,
        p2p_total=0,
        runtime_seconds=0.0,
        files_changed=[],
        lines_added=0,
        lines_removed=0,
        failure_mode="",
        error=None,
        artifacts={},
    )

    try:
        with temporary_worktree(case.repo_path, case.base_commit) as worktree_path:
            execution_cwd = _execution_cwd(worktree_path, case.workdir)

            patch_applied, patch_error = apply_patch(worktree_path, patch_path)
            if not patch_applied:
                result = EvaluationResult(
                    case_id=case.id,
                    agent="",
                    run_id="",
                    prompt_mode="",
                    patch_applied=False,
                    resolved=False,
                    f2p_passed=0,
                    f2p_total=0,
                    p2p_passed=0,
                    p2p_total=0,
                    runtime_seconds=0.0,
                    files_changed=[],
                    lines_added=0,
                    lines_removed=0,
                    failure_mode="patch_failed",
                    error=patch_error,
                    artifacts={},
                )
                return _write_evaluation(result_path, result)

            agent_changed_files, agent_added_lines, agent_removed_lines = diff_stats(
                worktree_path
            )
            test_patch_applies, test_patch_error = apply_patch(
                worktree_path, case.test_patch
            )
            if not test_patch_applies:
                result = EvaluationResult(
                    case_id=case.id,
                    agent="",
                    run_id="",
                    prompt_mode="",
                    patch_applied=True,
                    resolved=False,
                    f2p_passed=0,
                    f2p_total=0,
                    p2p_passed=0,
                    p2p_total=0,
                    runtime_seconds=0.0,
                    files_changed=agent_changed_files,
                    lines_added=agent_added_lines,
                    lines_removed=agent_removed_lines,
                    failure_mode="patch_failed",
                    error=f"Failed to apply test patch: {test_patch_error}",
                    artifacts={},
                )
                return _write_evaluation(result_path, result)

            setup_error = _run_setup_commands(case, execution_cwd, case.logs_dir, prefix="eval_setup")
            if setup_error is not None:
                result = EvaluationResult(
                    case_id=case.id,
                    agent="",
                    run_id="",
                    prompt_mode="",
                    patch_applied=True,
                    resolved=False,
                    f2p_passed=0,
                    f2p_total=0,
                    p2p_passed=0,
                    p2p_total=0,
                    runtime_seconds=0.0,
                    files_changed=[],
                    lines_added=0,
                    lines_removed=0,
                    failure_mode="other",
                    error=setup_error,
                    artifacts={},
                )
                return _write_evaluation(result_path, result)

            test_result = run_command(
                case.test_command,
                cwd=execution_cwd,
                env_overrides=case.env,
                timeout_seconds=case.timeout_seconds,
            )

            result = EvaluationResult(
                case_id=case.id,
                agent="",
                run_id="",
                prompt_mode="",
                patch_applied=True,
                resolved=test_result.exit_code == 0,
                f2p_passed=0,
                f2p_total=0,
                p2p_passed=0,
                p2p_total=0,
                runtime_seconds=test_result.duration_seconds,
                files_changed=agent_changed_files,
                lines_added=agent_added_lines,
                lines_removed=agent_removed_lines,
                failure_mode=(
                    FailureMode.TIMEOUT
                    if test_result.timed_out
                    else "tests_pass" if test_result.exit_code == 0 else "tests_failed"
                ),
                error=(
                    f"Tests timed out after {case.timeout_seconds} seconds"
                    if test_result.timed_out
                    else None
                ),
                artifacts={},
            )
    except Exception as exc:
        result = EvaluationResult(
            case_id=case.id,
            agent="",
            run_id="",
            prompt_mode="",
            patch_applied=False,
            resolved=False,
            f2p_passed=0,
            f2p_total=0,
            p2p_passed=0,
            p2p_total=0,
            runtime_seconds=0.0,
            files_changed=[],
            lines_added=0,
            lines_removed=0,
            failure_mode="other",
            error=str(exc),
            artifacts={},
        )

    return _write_evaluation(result_path, result)


def report(input_path: str | Path) -> str:
    """Write structured report artifacts and return the Markdown report."""
    _, markdown = generate_report(input_path)
    return markdown


@dataclass(frozen=True)
class _PatchEvaluation:
    patch_applied: bool
    tests_pass: bool
    error: str | None
    files_changed: list[str]
    lines_added: int
    lines_removed: int
    failure_mode: str | None = None


def run_agent(
    case_dir: str | Path,
    agent_name: str,
    run_id: str,
    container: bool = False,
    agent_env_file: str | Path | None = None,
    agent_ca_file: str | Path | None = None,
    agent_image: str | None = None,
    agent_command: str | None = None,
    agent_timeout_seconds: int | None = None,
    agent_model: str | None = None,
    agent_max_turns: int = 100,
    agent_debug: bool = False,
    agent_network: str = _DEFAULT_CLAUDE_NETWORK,
    live: bool = False,
    prompt_mode: str = "issue_only",
    progress: ProgressCallback | None = None,
    mcp_server: str | None = None,
    mcp_command: str | Path | None = None,
    mcp_profile: str | None = None,
    mcp_env_file: str | Path | None = None,
    mcp_environment: dict[str, str] | None = None,
    mcp_revision: str | None = None,
    mcp_revision_dirty: bool | None = None,
    skip_evaluation: bool = False,
    run_config: dict[str, Any] | None = None,
) -> RunResult:
    """Run a solver, then evaluate its patch in a fresh checkout."""
    if not is_safe_path_component(run_id):
        raise PatchbenchError("run ID must be a single non-empty path component")
    case = load_case(case_dir)
    _progress(progress, "environment", status="running")
    if agent_name not in AGENT_REGISTRY:
        raise PatchbenchError(f"Unknown agent: {agent_name}")
    metadata = read_metadata(case.case_dir)
    historical = metadata.get('historical_pr_tests', {})
    modern_historical = historical.get('command_policy_version', 1) >= 2
    if (modern_historical or metadata.get('validation_policy') == 'strict_f2p_v1'
            or case.oracle_mode in {'existing_tests', 'existing_tests_with_regression'}) and not container:
        raise PatchbenchError('Strict coding solver execution requires --container to isolate hidden inputs')
    if modern_historical:
        from patchbench.historical_pr_tests import historical_baseline
        historical_baseline(case)
    if agent_env_file and agent_name != "claude-code":
        raise PatchbenchError("--agent-env-file is only supported by claude-code")
    if agent_ca_file is not None and agent_name != "claude-code":
        raise PatchbenchError("--agent-ca-file is only supported by claude-code")
    if agent_ca_file is not None and not container:
        raise PatchbenchError("--agent-ca-file requires containerized Claude Code")
    if agent_network != _DEFAULT_CLAUDE_NETWORK and agent_name != "claude-code":
        raise PatchbenchError("--agent-network is only supported by claude-code")
    if agent_network != _DEFAULT_CLAUDE_NETWORK and not container:
        raise PatchbenchError("--agent-network only affects containerized Claude Code")
    if container and agent_name == "claude-code" and not agent_network.strip():
        raise PatchbenchError("--agent-network must be non-empty")
    if agent_image is not None and agent_name not in {
        "claude-code",
        "container-command",
    }:
        raise PatchbenchError(
            "--agent-image is only supported by claude-code and container-command"
        )
    if agent_image is not None and agent_name == "claude-code" and not container:
        raise PatchbenchError("--agent-image only affects containerized Claude Code")
    if agent_command is not None and agent_name != "container-command":
        raise PatchbenchError("--agent-command is only supported by container-command")
    if agent_timeout_seconds is not None and agent_timeout_seconds <= 0:
        raise PatchbenchError("agent timeout must be greater than zero")
    if agent_max_turns <= 0:
        raise PatchbenchError("agent max turns must be greater than zero")
    if (agent_model is not None or agent_debug or agent_max_turns != 100) and agent_name != "claude-code":
        raise PatchbenchError(
            "--agent-model, --agent-max-turns, and --agent-debug are only supported by claude-code"
        )
    if prompt_mode not in {"issue_only", "issue_with_hints"}:
        raise PatchbenchError(f"Unknown prompt mode: {prompt_mode}")
    mcp_options_present = any(
        value is not None
        for value in (mcp_command, mcp_profile, mcp_env_file, mcp_environment)
    )
    if mcp_server is None and mcp_options_present:
        raise PatchbenchError("MCP options require --mcp-server")
    if mcp_server is not None and agent_name != "claude-code":
        raise PatchbenchError("MCP servers are currently supported only by claude-code")
    if mcp_server not in {None, "landb"}:
        raise PatchbenchError(f"Unsupported MCP server: {mcp_server}")
    mcp_config: McpRunConfig = disabled_mcp_config()
    if mcp_server == "landb":
        if mcp_env_file is not None:
            mcp_env_path = Path(mcp_env_file).expanduser().resolve()
            if not mcp_env_path.is_file():
                raise PatchbenchError(f"MCP env file does not exist: {mcp_env_path}")
            try:
                mcp_env_path.relative_to(_PROJECT_ROOT)
            except ValueError:
                pass
            else:
                raise PatchbenchError(
                    "MCP credentials must be provided from outside the PatchBench project"
                )
        effective_mcp_command = mcp_command or (
            "/opt/landb-mcp/.venv/bin/landb-mcp"
            if container
            else load_repository("landb-mcp").checkout() / ".venv/bin/landb-mcp"
        )
        if container and mcp_revision is None:
            image_for_mcp = agent_image or _solver_image(case, "claude-code")
            if not image_for_mcp:
                raise PatchbenchError("Containerized MCP requires a solver image")
            mcp_revision, mcp_revision_dirty = inspect_landb_mcp_image(
                image_for_mcp
            )
        mcp_config = landb_mcp_config(
            command=effective_mcp_command,
            profile=mcp_profile or "curated",
            env_file=mcp_env_file,
            environment=mcp_environment,
            validate_executable=not container,
            revision=mcp_revision,
            revision_dirty=mcp_revision_dirty,
        )
    if agent_name == "claude-code" and agent_env_file is not None:
        env_path = Path(agent_env_file).expanduser().resolve()
        if not env_path.is_file():
            raise PatchbenchError(f"Claude env file does not exist: {env_path}")
        try:
            env_path.relative_to(_PROJECT_ROOT)
        except ValueError:
            pass
        else:
            raise PatchbenchError(
                "Claude Code requires claude-env.sh outside the project directory"
            )
    if container and agent_name == "claude-code":
        if agent_env_file is None:
            raise PatchbenchError(
                "Containerized Claude Code requires --agent-env-file PATH to an external claude-env.sh"
            )
        env_path = Path(agent_env_file).expanduser().resolve()
    provider_ca_path: Path | None = None
    provider_ca_sha256: str | None = None
    provider_ca_cert_count = 0
    if agent_ca_file is not None:
        (
            provider_ca_path,
            provider_ca_sha256,
            provider_ca_cert_count,
        ) = _validate_provider_ca_file(agent_ca_file)
    ensure_git_repo(case.repo_path)
    ensure_commit_exists(case.repo_path, case.base_commit)
    _progress(
        progress,
        "environment",
        status="success",
        last_activity="Runtime, credentials, repository, and base commit validated",
    )

    effective_agent_timeout = agent_timeout_seconds or case.timeout_seconds

    _progress(progress, "prompt", status="running")
    prompt = _read_solver_prompt(case, prompt_mode)
    if agent_name == "claude-code":
        prompt = add_completion_protocol(prompt)
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    _progress(
        progress,
        "prompt",
        status="success",
        prompt=prompt,
        prompt_hash=prompt_hash,
        last_activity="Exact solver-safe prompt prepared",
    )
    agent_class = AGENT_REGISTRY[agent_name]
    if agent_name == "claude-code":
        agent_kwargs: dict[str, Any] = {
            "env_file": agent_env_file,
            "timeout_seconds": effective_agent_timeout,
        }
        if mcp_config.enabled:
            agent_kwargs["mcp_config"] = mcp_config
        if provider_ca_path is not None:
            agent_kwargs["ca_file"] = provider_ca_path
        agent = agent_class(**agent_kwargs)
        # Test doubles and older third-party adapters may not accept the new
        # optional constructor keyword while still supporting disabled MCP.
        agent.mcp_config = mcp_config
        if not hasattr(agent, "mcp_interactions"):
            agent.mcp_interactions = []
    else:
        agent = agent_class()
    if agent_name == "claude-code":
        # The central display owns terminal rendering when a callback is present.
        agent.live_output = live and progress is None
        agent.progress_callback = (
            (lambda phase, **fields: _progress(progress, phase, **fields))
            if progress is not None
            else None
        )
        agent.requested_model = agent_model
        agent.max_turns = agent_max_turns
        agent.debug = agent_debug
        if container:
            # Read only enough configuration for metadata and log redaction.
            # The container itself sources the mounted script at runtime.
            agent.container_environment()
    use_container_solver = bool(
        agent.supports_container_execution
        and (container or not agent.supports_host_execution)
    )
    if not use_container_solver and not agent.supports_host_execution:
        raise PatchbenchError(f"Agent {agent_name} does not support host execution")

    agent_mode = "container" if use_container_solver else "host"
    evaluation_mode = "container" if container else "host"
    selected_image = agent_image or _solver_image(case, agent_name)
    selected_command = (
        agent.container_command()
        if use_container_solver and agent_name == "claude-code"
        else agent_command or getattr(agent, "command", None)
    )
    metadata: dict[str, Any] = {
        "agent": agent_name,
        "run_id": run_id,
        "case_id": case.id,
        "base_commit": case.base_commit,
        "prompt_mode": prompt_mode,
        "available_relevant_files_count": len(case.relevant_files),
        "included_relevant_files_count": (
            len(case.relevant_files) if prompt_mode == "issue_with_hints" else 0
        ),
        # Backward-compatible alias, now defined as the number actually included.
        "relevant_files_count": (
            len(case.relevant_files) if prompt_mode == "issue_with_hints" else 0
        ),
        "prompt_hash": prompt_hash,
        "agent_execution_mode": agent_mode,
        "evaluation_execution_mode": evaluation_mode,
        "model": getattr(agent, "model", None),
        "model_source": getattr(agent, "model_source", None),
        "agent_timeout_seconds": effective_agent_timeout,
        "agent_max_turns": agent_max_turns if agent_name == "claude-code" else None,
        "agent_debug": agent_debug,
        "live_output": live,
        "agent_ca_configured": provider_ca_path is not None,
        "agent_ca_sha256": provider_ca_sha256,
        "agent_ca_certificate_count": provider_ca_cert_count,
        **mcp_config.metadata(),
    }
    if use_container_solver and agent_name == "claude-code":
        metadata["agent_network"] = agent_network
        metadata["agent_env_source"] = "external read-only claude-env.sh mount"
    if use_container_solver:
        if not selected_image or not selected_command:
            raise PatchbenchError(f"Container agent {agent_name} requires an image and command")
        metadata["agent_image"] = selected_image
        metadata["agent_command_sanitized"] = sanitize_command(selected_command)

    ensure_case_output_dirs(case)
    run_dir = case.runs_dir / run_id
    legacy_run = case.case_dir / "runs" / run_id
    if legacy_run.exists() or legacy_run.is_symlink():
        raise PatchbenchError(f"Run directory already exists: {legacy_run}. Choose a unique --run-id.")
    try:
        run_dir.mkdir()
    except FileExistsError as exc:
        raise PatchbenchError(
            f"Run directory already exists: {run_dir}. Choose a unique --run-id."
        ) from exc
    patch_path = run_dir / "agent.patch"
    prompt_path = run_dir / "prompt.txt"
    test_log_path = run_dir / "test.log"
    metadata_path = run_dir / "metadata.json"
    patch_path.write_text("", encoding="utf-8")
    prompt_path.write_text(prompt, encoding="utf-8")
    test_log_path.write_text("", encoding="utf-8")
    if run_config is not None:
        write_json(run_dir / "run.config.json", run_config)

    started = time.monotonic()
    agent_output = AgentOutput(None, "Agent did not run")
    solver_completed = False
    score_stopped_agent = False
    evaluation = _PatchEvaluation(False, False, None, [], 0, 0)
    try:
        _progress(
            progress,
            "worktree",
            status="running",
            last_activity="Creating the isolated solver checkout",
        )
        if use_container_solver:
            _progress(
                progress,
                "container",
                status="running",
                container_status="Starting",
            )
            if agent_name == "claude-code":
                agent_output = _run_claude_container_solver(
                    case,
                    prompt,
                    prompt_hash,
                    run_dir,
                    agent,
                    selected_image,
                    selected_command,
                    Path(agent_env_file).expanduser().resolve(),
                    effective_agent_timeout,
                    agent_network,
                    progress,
                )
            else:
                _progress(
                    progress,
                    "agent",
                    status="running",
                    container_status="Running",
                )
                agent_output = _run_container_solver(
                    case,
                    prompt,
                    prompt_hash,
                    run_dir,
                    selected_image,
                    selected_command,
                    effective_agent_timeout,
                    progress,
                )
        else:
            _progress(
                progress,
                "agent",
                status="running",
                container_status="Not used",
            )
            with temporary_checkout(case.repo_path, case.base_commit) as solving_worktree:
                _progress(
                    progress,
                    "worktree",
                    status="success",
                    last_activity="Isolated solver checkout ready",
                )
                agent_output = agent.run(
                    case_dir=case.case_dir,
                    solving_worktree=solving_worktree,
                    base_commit=case.base_commit,
                    prompt_mode=prompt_mode,
                    prompt=prompt,
                )
                if agent_output.patch_path is not None:
                    shutil.copy(agent_output.patch_path, patch_path)
                for artifact_name in (
                    "trajectory.log",
                    "agent_events.jsonl",
                    "agent_debug.log",
                    "mcp_activity.jsonl",
                ):
                    artifact = solving_worktree / artifact_name
                    if artifact.exists():
                        shutil.copy(artifact, run_dir / artifact_name)

        solver_completed = True
        metadata["model"] = getattr(agent, "model", None)
        # Score the submission available when a coding agent exhausts its
        # budget or ends prematurely. Execution diagnostics remain separate
        # from the deterministic patch outcome. User interrupts still stop.
        score_stopped_agent = agent_output.details.get("agent_termination_reason") in {
            "wall_clock_timeout", "max_turns", "incomplete_response", "no_tool_activity",
        }
        if score_stopped_agent:
            details = dict(agent_output.details)
            details.update(evaluation_skipped=False, evaluation_skip_reason=None)
            agent_output = AgentOutput(
                agent_output.patch_path, agent_output.error,
                timed_out=agent_output.timed_out, details=details,
            )
        metadata["model_source"] = getattr(agent, "model_source", None)
        metadata.update(agent_output.details)
        if skip_evaluation:
            details = dict(agent_output.details)
            details.update(
                {
                    "evaluation_skipped": True,
                    "evaluation_skip_reason": "requested_stop_after_patch",
                }
            )
            agent_output = AgentOutput(
                agent_output.patch_path,
                agent_output.error,
                timed_out=agent_output.timed_out,
                details=details,
            )
            metadata.update(details)
        if mcp_config.enabled and (
            agent_output.details.get("mcp_process_started") is False
            or agent_output.details.get("mcp_tools_discovered") is False
        ):
            details = dict(agent_output.details)
            reason = (
                "mcp_startup_failure"
                if details.get("mcp_process_started") is False
                else "mcp_discovery_failure"
            )
            details.update(
                {
                    "agent_termination_reason": reason,
                    "agent_execution_detail": reason,
                    "evaluation_skipped": True,
                    "evaluation_skip_reason": reason,
                }
            )
            agent_output = AgentOutput(
                agent_output.patch_path,
                "Configured MCP server was unavailable to Claude Code",
                timed_out=agent_output.timed_out,
                details=details,
            )
            metadata.update(details)
        agent_status = "warning" if agent_output.error else "success"
        agent_activity = (
            redact_secrets(agent_output.error)[:500]
            if agent_output.error
            else "Agent execution completed"
        )
        _progress(
            progress,
            "agent",
            status=agent_status,
            turns=agent_output.details.get("agent_num_turns"),
            last_activity=agent_activity,
        )
        _progress(
            progress,
            "tools",
            status="success",
            tool_calls=agent_output.details.get("agent_tool_event_count", 0),
            last_activity=(
                "Agent tool activity captured"
                if agent_output.details.get("agent_tool_event_count")
                else "Agent completed without recorded tool calls"
            ),
        )
        patch_created = bool(patch_path.read_text(encoding="utf-8").strip())
        _progress(
            progress,
            "patch",
            status="success" if patch_created else "warning",
            turns=agent_output.details.get("agent_num_turns"),
            tool_calls=agent_output.details.get("agent_tool_event_count"),
            last_activity=(
                "Patch saved" if patch_created else "No patch produced"
            ),
            container_status="Completed" if use_container_solver else "Not used",
        )
        termination_reason = agent_output.details.get("agent_termination_reason")
        _progress(
            progress,
            "termination",
            status=(
                "success"
                if termination_reason in {None, "completed"} and not agent_output.error
                else "warning"
            ),
            last_activity=termination_reason or (
                "completed" if not agent_output.error else agent_activity
            ),
        )
        if agent_output.timed_out and not agent_output.details:
            _progress(
                progress,
                "evaluation",
                status="skipped",
                tests_status="Not run",
                evaluation_status="Skipped after agent timeout",
            )
            test_log_path.write_text(
                "Evaluation skipped because agent execution timed out.\n",
                encoding="utf-8",
            )
        elif agent_output.details.get("evaluation_skipped"):
            _progress(
                progress,
                "evaluation",
                status="skipped",
                tests_status="Not run",
                evaluation_status="Skipped",
            )
            test_log_path.write_text(
                "Evaluation skipped: "
                f"{agent_output.details.get('evaluation_skip_reason', 'agent execution did not complete')}.\n",
                encoding="utf-8",
            )
        elif patch_path.read_text(encoding="utf-8").strip():
            _progress(
                progress,
                "evaluation",
                status="running",
                tests_status="Running",
                evaluation_status="Applying patch and running deterministic tests",
            )
            evaluation = (
                _evaluate_patch_container(case, patch_path, run_dir)
                if container
                else _evaluate_patch_host(case, patch_path, run_dir)
            )
            _progress(
                progress,
                "evaluation",
                status="success" if evaluation.tests_pass else "error",
                tests_status=(
                    "All tests passed" if evaluation.tests_pass else "Failed"
                ),
                evaluation_status=(
                    "Completed successfully"
                    if evaluation.tests_pass
                    else "Completed with failures"
                ),
            )
        else:
            _progress(
                progress,
                "evaluation",
                status="error" if score_stopped_agent else "skipped",
                tests_status="Not run",
                evaluation_status="Failed: no code changes" if score_stopped_agent else "Skipped",
            )
            test_log_path.write_text(
                "No code changes submitted; case scored unresolved.\n"
                if score_stopped_agent else
                "Evaluation skipped because the agent produced no code changes.\n",
                encoding="utf-8",
            )
    except KeyboardInterrupt:
        partial_patch = bool(patch_path.read_text(encoding="utf-8").strip())
        details = dict(agent_output.details)
        details.update(
            {
                "evaluation_skipped": True,
                "evaluation_skip_reason": "run_interrupted",
                "partial_patch_captured": partial_patch,
            }
        )
        if not solver_completed:
            details.update(
                {
                    "agent_termination_reason": "interrupted",
                    "agent_execution_detail": "interrupted_by_user",
                }
            )
        else:
            evaluation = _PatchEvaluation(
                False,
                False,
                "Evaluation interrupted by user",
                [],
                0,
                0,
                FailureMode.OTHER,
            )
        agent_output = AgentOutput(
            patch_path if partial_patch else None,
            "Run interrupted by user",
            details=details,
        )
        if not solver_completed:
            _progress(
                progress,
                "agent",
                status="failed",
                last_activity="Run interrupted by user",
            )
            _progress(progress, "tools", status="skipped", tool_calls=0)
            _progress(
                progress,
                "patch",
                status="success" if partial_patch else "warning",
                last_activity="Partial patch preserved" if partial_patch else "No patch produced",
            )
            _progress(
                progress,
                "termination",
                status="warning",
                last_activity="interrupted",
            )
        _progress(
            progress,
            "evaluation",
            status="failed",
            last_activity="Run interrupted by user",
            tests_status="Not run",
            evaluation_status="Interrupted",
        )
        test_log_path.write_text(
            "Evaluation skipped because the run was interrupted.\n",
            encoding="utf-8",
        )
    except Exception as exc:
        error_text = redact_secrets(str(exc))
        partial_patch = bool(patch_path.read_text(encoding="utf-8").strip())
        termination_reason = (
            "evaluation_error"
            if solver_completed
            else "container_error" if use_container_solver else "agent_error"
        )
        details = dict(agent_output.details)
        details.update(
            {
                "evaluation_skipped": True,
                "evaluation_skip_reason": termination_reason,
                "partial_patch_captured": partial_patch,
            }
        )
        if solver_completed:
            evaluation = _PatchEvaluation(
                False,
                False,
                error_text,
                [],
                0,
                0,
                FailureMode.OTHER,
            )
        else:
            details["agent_termination_reason"] = termination_reason
        agent_output = AgentOutput(
            patch_path if partial_patch else None,
            error_text,
            details=details,
        )
        if not solver_completed:
            _progress(
                progress,
                "agent",
                status="failed",
                last_activity=error_text,
            )
            _progress(progress, "tools", status="skipped", tool_calls=0)
            _progress(
                progress,
                "patch",
                status="success" if partial_patch else "warning",
                last_activity="Partial patch preserved" if partial_patch else "No patch produced",
            )
            _progress(
                progress,
                "termination",
                status="error",
                last_activity=termination_reason,
            )
        _progress(
            progress,
            "evaluation",
            status="failed",
            last_activity=f"Error: {error_text}",
            tests_status="Not run",
            evaluation_status="Aborted",
            container_status=(
                "Failed" if termination_reason == "container_error" else "Completed"
            ),
        )
        test_log_path.write_text(
            (
                f"Evaluation failed: {error_text}\n"
                if solver_completed
                else f"Run failed before evaluation: {error_text}\n"
            ),
            encoding="utf-8",
        )

    if agent_name == "claude-code":
        (run_dir / "agent_events.jsonl").touch(exist_ok=True)
        if agent_debug:
            (run_dir / "agent_debug.log").touch(exist_ok=True)

    if agent_name in {"claude-code", "container-command"}:
        trajectory_path = run_dir / "trajectory.log"
        if not trajectory_path.exists():
            trajectory_path.write_text(
                f"agent_execution_mode: {agent_mode}\n"
                f"prompt_hash: {prompt_hash}\n"
                f"agent_timeout_seconds: {effective_agent_timeout}\n"
                f"timed_out: {str(agent_output.timed_out).lower()}\n"
                f"error: {agent_output.error or 'agent did not complete'}\n",
                encoding="utf-8",
            )

    score_stopped_agent = score_stopped_agent and not agent_output.details.get("evaluation_skipped")
    termination_reason = agent_output.details.get("agent_termination_reason")
    if agent_output.timed_out and not score_stopped_agent:
        failure_mode = FailureMode.TIMEOUT
        error = agent_output.error or f"Agent execution timed out after {effective_agent_timeout} seconds"
    elif not score_stopped_agent and termination_reason in {
        "max_turns",
        "interrupted",
        "agent_error",
        "container_error",
        "mcp_startup_failure",
        "mcp_discovery_failure",
    }:
        failure_mode = FailureMode.OTHER
        error = agent_output.error
    elif not patch_path.read_text(encoding="utf-8").strip():
        failure_mode = (
            FailureMode.NO_CODE_CHANGE
            if solver_completed
            and (
                agent_name in {"dummy", "container-command"}
                or score_stopped_agent
                or "no code changes" in (agent_output.error or "").lower()
            )
            else FailureMode.OTHER
        )
        error = agent_output.error
    elif agent_output.details.get("evaluation_skipped"):
        failure_mode = FailureMode.OTHER
        error = agent_output.error
    elif evaluation.failure_mode is not None:
        failure_mode = evaluation.failure_mode
        error = evaluation.error
    elif not evaluation.patch_applied:
        failure_mode = FailureMode.PATCH_FAILED
        error = evaluation.error
    elif evaluation.tests_pass:
        failure_mode = FailureMode.TESTS_PASS
        error = None
    else:
        failure_mode = FailureMode.TESTS_FAILED
        error = evaluation.error

    patch_comparison = compare_patches(patch_path, case.gold_patch)
    result_files_changed = evaluation.files_changed
    result_lines_added = evaluation.lines_added
    result_lines_removed = evaluation.lines_removed
    if agent_output.details.get("partial_patch_captured") or (
        agent_output.details.get("evaluation_skipped")
        and patch_path.read_text(encoding="utf-8").strip()
    ):
        result_files_changed = patch_comparison.agent_files_changed
        result_lines_added = patch_comparison.agent_lines_added
        result_lines_removed = patch_comparison.agent_lines_removed

    result = _create_run_result(
        case=case,
        agent_name=agent_name,
        run_id=run_id,
        prompt_mode=prompt_mode,
        patch_applied=evaluation.patch_applied,
        resolved=evaluation.tests_pass,
        failure_mode=failure_mode,
        error=error,
        files_changed=result_files_changed,
        added_lines=result_lines_added,
        removed_lines=result_lines_removed,
        runtime_seconds=time.monotonic() - started,
        agent_patch_path=patch_path,
        prompt_path=prompt_path,
        test_log_path=test_log_path,
        metadata_path=metadata_path,
        agent_execution_mode=agent_mode,
        evaluation_execution_mode=evaluation_mode,
        agent_image=selected_image if use_container_solver else None,
        agent_command_sanitized=(
            sanitize_command(selected_command)
            if use_container_solver
            else agent_output.details.get("agent_command_sanitized")
        ),
        model=getattr(agent, "model", None),
        model_source=getattr(agent, "model_source", None),
        prompt_hash=prompt_hash,
        agent_timeout_seconds=effective_agent_timeout,
        agent_details=agent_output.details,
        patch_comparison=patch_comparison,
    )
    metadata.update(
        patch_applied=result.patch_applied,
        resolved=result.resolved,
        failure_mode=result.failure_mode,
    )
    write_json(metadata_path, metadata)
    _write_run_result(run_dir / "result.json", result)
    return result


def _read_solver_prompt(case, prompt_mode: str) -> str:
    issue = case.problem_statement.read_text(encoding="utf-8")
    if prompt_mode == "issue_only":
        return issue
    if prompt_mode == "issue_with_hints":
        hints = ["Additional benchmark-provided hints", "", "Relevant files:"]
        hints.extend(f"- {path}" for path in case.relevant_files)
        if not case.relevant_files:
            hints.append("- (none provided)")
        return f"{issue.rstrip()}\n\n---\n\n" + "\n".join(hints) + "\n"
    raise PatchbenchError(f"Unknown prompt mode: {prompt_mode}")


def prepare_solver_prompt(
    case_dir: str | Path,
    prompt_mode: str = "issue_only",
    agent_name: str = "dummy",
) -> str:
    """Build the exact solver prompt without creating a worktree or run evidence."""
    if prompt_mode not in {"issue_only", "issue_with_hints"}:
        raise PatchbenchError(f"Unknown prompt mode: {prompt_mode}")
    if agent_name not in AGENT_REGISTRY:
        raise PatchbenchError(f"Unknown agent: {agent_name}")
    resolved = Path(case_dir).expanduser().resolve()
    meta_path = resolved / "meta.yaml"
    if not resolved.is_dir() or not meta_path.is_file():
        raise PatchbenchError(f"Case metadata file does not exist: {meta_path}")
    raw = read_metadata(resolved)
    issue_value = raw.get("problem_statement")
    if not isinstance(issue_value, str) or not issue_value.strip():
        raise PatchbenchError("problem_statement must be a non-empty string")
    issue_relative = Path(issue_value)
    if issue_relative.is_absolute() or "\\" in issue_value or ".." in issue_relative.parts:
        raise PatchbenchError("problem_statement must stay within the case directory")
    issue_path = (resolved / issue_relative).resolve()
    try:
        issue_path.relative_to(resolved)
    except ValueError as exc:
        raise PatchbenchError("problem_statement escapes the case directory") from exc
    if not issue_path.is_file():
        raise PatchbenchError(f"Problem statement does not exist: {issue_path}")
    issue = issue_path.read_text(encoding="utf-8")
    prompt = issue
    if prompt_mode == "issue_with_hints":
        hints_raw = raw.get("hints") or {}
        if not isinstance(hints_raw, dict):
            raise PatchbenchError("hints must be a mapping")
        relevant = hints_raw.get("relevant_files", [])
        if not isinstance(relevant, list) or not all(isinstance(item, str) for item in relevant):
            raise PatchbenchError("hints.relevant_files must be a list of strings")
        if any(
            Path(item).is_absolute() or "\\" in item or ".." in Path(item).parts
            for item in relevant
        ):
            raise PatchbenchError(
                "hints.relevant_files entries must stay within the repository"
            )
        hints = ["Additional benchmark-provided hints", "", "Relevant files:"]
        hints.extend(f"- {path}" for path in relevant)
        if not relevant:
            hints.append("- (none provided)")
        prompt = f"{issue.rstrip()}\n\n---\n\n" + "\n".join(hints) + "\n"
    return add_completion_protocol(prompt) if agent_name == "claude-code" else prompt


def _run_claude_container_solver(
    case,
    prompt: str,
    prompt_hash: str,
    run_dir: Path,
    agent,
    image: str,
    command: str,
    env_file: Path,
    timeout_seconds: int,
    network: str,
    progress: ProgressCallback | None = None,
) -> AgentOutput:
    """Run the fixed Claude CLI invocation inside Podman and collect its patch."""
    environment = agent.container_environment()
    with temporary_checkout(case.repo_path, case.base_commit) as solving_checkout:
        seed = getattr(case, "test_seed_patch", None)
        if seed is not None:
            applied, error = apply_patch(solving_checkout, seed)
            if not applied:
                raise PatchbenchError(f"Could not apply prior generated tests: {error}")
        _progress(
            progress,
            "worktree",
            status="success",
            last_activity="Isolated solver checkout ready",
        )
        with tempfile.TemporaryDirectory(prefix="patchbench-prompt-") as prompt_dir:
            prompt_path = Path(prompt_dir) / "issue.md"
            prompt_path.write_text(prompt, encoding="utf-8")
            mcp_config_path: Path | None = None
            if agent.mcp_config.enabled:
                mcp_config_path = Path(prompt_dir) / "mcp.json"
                write_mcp_config(mcp_config_path, agent.mcp_config)
            _progress(
                progress,
                "agent",
                status="running",
                container_status="Running",
                last_activity="Starting Claude Code inside the container",
            )

            last_container_heartbeat = -float("inf")

            def on_container_output(
                stream_name: str, line: str, elapsed: float, heartbeat: bool
            ) -> None:
                nonlocal last_container_heartbeat
                if stream_name == "stdout" and line:
                    agent.observe_live_event(line, elapsed)
                    return
                if line:
                    _progress(
                        progress,
                        "analysis",
                        status="running",
                        container_status="Running",
                        last_activity=f"Container: {line.strip()[:160]}",
                    )
                    return
                if elapsed - last_container_heartbeat < agent.heartbeat_seconds:
                    return
                last_container_heartbeat = elapsed
                _progress(
                    progress,
                    "analysis",
                    status="running",
                    last_activity="Waiting for model response",
                    container_status="Running",
                    heartbeat=heartbeat,
                )

            arguments = (
                image,
                solving_checkout,
                prompt_path,
                env_file,
                command,
                timeout_seconds,
                environment,
                network,
            )
            extra: dict[str, Any] = {}
            if progress is not None:
                extra["progress_callback"] = on_container_output
            if mcp_config_path is not None:
                extra["mcp_config_path"] = mcp_config_path
            if getattr(agent, "ca_file", None) is not None:
                extra["provider_ca_path"] = agent.ca_file
            if getattr(case, 'case_dir', None):
                settings = read_metadata(case.case_dir).get('historical_pr_tests', {})
                if settings.get('dependency_cache'):
                    from patchbench.historical_pr_tests import resolve_dependency_cache, cache_identity
                    cache = Path(resolve_dependency_cache(case.case_dir, settings))
                    if cache_identity(cache, solving_checkout) != settings.get('dependency_cache_digest'):
                        raise PatchbenchError('Historical solver dependency cache identity changed')
                    extra['dependency_cache'] = cache
            completed = run_claude_in_container(*arguments, **extra)

        sanitized_stdout = (
            agent.sanitize_event_stream(completed.stdout, environment)
            if hasattr(agent, "sanitize_event_stream")
            else completed.stdout
        )
        completed = ContainerResult(
            completed.returncode,
            sanitized_stdout,
            completed.stderr,
            completed.duration_seconds,
            completed.timed_out,
            completed.interrupted,
        )

        run_command(["git", "add", "-N", "--all"], cwd=solving_checkout)
        diff = run_command(
            ["git", "diff", "HEAD", "--binary", "--", "."], cwd=solving_checkout
        )
        # Never persist a credential that an agent accidentally wrote into the
        # checkout. This may make such a patch invalid, which is preferable to
        # leaking a credential into benchmark artifacts.
        patch_text = redact_secrets(diff.stdout, environment)
        (run_dir / "agent.patch").write_text(patch_text, encoding="utf-8")
        events_path = run_dir / "agent_events.jsonl"
        events_path.write_text(completed.stdout, encoding="utf-8")
        details = agent.container_execution_details(
            completed.stdout,
            completed.returncode,
            completed.timed_out,
            partial_patch=bool(patch_text.strip()),
        )
        write_mcp_evidence(run_dir / "mcp_activity.jsonl", agent.mcp_interactions)
        if completed.interrupted:
            details.update(
                {
                    "agent_termination_reason": "interrupted",
                    "agent_execution_detail": "interrupted_by_user",
                    "evaluation_skipped": True,
                    "evaluation_skip_reason": "agent_interrupted",
                }
            )
        if getattr(agent, "debug", False):
            (run_dir / "agent_debug.log").write_text(
                "Claude Code ran in Podman; stdout was collected after container exit.\n",
                encoding="utf-8",
            )
        trajectory = [
            "=== Containerized Claude Code Execution ===",
            f"prompt_hash: {prompt_hash}",
            "agent_execution_mode: container",
            f"image: {image}",
            f"command: {sanitize_command(command)}",
            f"network: {network}",
            f"agent_timeout_seconds: {timeout_seconds}",
            "workspace: /workspace",
            "prompt_file: /run/patchbench/issue.md (read-only)",
            "env_file: /run/patchbench/claude-env.sh (read-only; external source)",
            (
                "provider_ca: /run/patchbench/provider-ca.pem (read-only; external source)"
                if getattr(agent, "ca_file", None) is not None
                else "provider_ca: system trust store"
            ),
            (
                "mcp_config: /run/patchbench/mcp.json (read-only; ephemeral)"
                if agent.mcp_config.enabled
                else "mcp_config: disabled"
            ),
            f"exit_code: {completed.returncode}",
            f"duration_seconds: {completed.duration_seconds:.3f}",
            f"timed_out: {str(completed.timed_out).lower()}",
            "",
            "=== STDOUT ===",
            completed.stdout or "(empty)",
            "",
            "=== STDERR ===",
            completed.stderr or "(empty)",
            "",
            "=== AGENT PATCH (git diff) ===",
            patch_text or "(no changes)",
            "",
        ]
        (run_dir / "trajectory.log").write_text("\n".join(trajectory), encoding="utf-8")
        if completed.timed_out:
            return AgentOutput(
                run_dir / "agent.patch" if patch_text.strip() else None,
                f"Claude Code execution timed out after {timeout_seconds} seconds",
                timed_out=True,
                details=details,
            )
        if completed.interrupted:
            return AgentOutput(
                run_dir / "agent.patch" if patch_text.strip() else None,
                "Claude Code execution was interrupted",
                details=details,
            )
        if details["agent_termination_reason"] == "max_turns":
            return AgentOutput(
                run_dir / "agent.patch" if patch_text.strip() else None,
                f"Claude Code reached the {agent.max_turns}-turn limit",
                details=details,
            )
        if incomplete_execution_error(details["agent_termination_reason"]):
            return AgentOutput(
                run_dir / "agent.patch" if patch_text.strip() else None,
                incomplete_execution_error(details["agent_termination_reason"]),
                details=details,
            )
        if completed.returncode != 0:
            message = (
                completed.stderr
                or claude_result_error(completed.stdout)
                or completed.stdout[-2000:]
                or "unknown error"
            )
            return AgentOutput(
                run_dir / "agent.patch" if patch_text.strip() else None,
                f"Claude Code failed with exit code {completed.returncode}: {message}",
                details=details,
            )
        if not patch_text.strip():
            return AgentOutput(
                None, "Claude Code produced no code changes", details=details
            )
        return AgentOutput(run_dir / "agent.patch", None, details=details)


def _run_container_solver(
    case,
    prompt: str,
    prompt_hash: str,
    run_dir: Path,
    image: str,
    command: str,
    timeout_seconds: int,
    progress: ProgressCallback | None = None,
) -> AgentOutput:
    with temporary_checkout(case.repo_path, case.base_commit) as solving_checkout:
        _progress(
            progress,
            "worktree",
            status="success",
            last_activity="Isolated solver checkout ready",
        )
        _progress(
            progress,
            "agent",
            status="running",
            last_activity="Starting the container agent",
        )
        with tempfile.TemporaryDirectory(prefix="patchbench-prompt-") as prompt_dir:
            prompt_path = Path(prompt_dir) / "issue.md"
            prompt_path.write_text(prompt, encoding="utf-8")
            completed = run_solver_in_container(
                image,
                solving_checkout,
                prompt_path,
                command,
                timeout_seconds,
            )

        run_command(["git", "add", "-N", "--all"], cwd=solving_checkout)
        diff = run_command(
            ["git", "diff", "HEAD", "--binary", "--", "."], cwd=solving_checkout
        )
        (run_dir / "agent.patch").write_text(diff.stdout, encoding="utf-8")
        trajectory = [
            "=== Container Solver Execution ===",
            f"prompt_hash: {prompt_hash}",
            "agent_execution_mode: container",
            f"image: {image}",
            f"command: {sanitize_command(command)}",
            "workspace: /workspace",
            "prompt_file: /run/patchbench/issue.md (read-only)",
            f"exit_code: {completed.returncode}",
            f"timed_out: {str(completed.timed_out).lower()}",
            f"duration_seconds: {completed.duration_seconds:.3f}",
            "",
            "=== STDOUT ===",
            completed.stdout or "(empty)",
            "",
            "=== STDERR ===",
            completed.stderr or "(empty)",
            "",
            "=== AGENT PATCH (git diff) ===",
            diff.stdout or "(no changes)",
            "",
        ]
        (run_dir / "trajectory.log").write_text("\n".join(trajectory), encoding="utf-8")
        if completed.timed_out or completed.interrupted:
            partial_patch = bool(diff.stdout.strip())
            termination = "interrupted" if completed.interrupted else "timeout"
            detail = (
                "interrupted_by_user"
                if completed.interrupted
                else "container_timeout"
            )
            error = (
                "Container solver was interrupted"
                if completed.interrupted
                else f"Container solver timed out after {timeout_seconds} seconds"
            )
            return AgentOutput(
                run_dir / "agent.patch" if partial_patch else None,
                error,
                timed_out=completed.timed_out,
                details={
                    "agent_termination_reason": termination,
                    "agent_execution_detail": detail,
                    "partial_patch_captured": partial_patch,
                    "evaluation_skipped": True,
                    "evaluation_skip_reason": f"agent_{termination}",
                },
            )
        if completed.returncode != 0:
            detail = completed.stderr or completed.stdout or "unknown error"
            return AgentOutput(
                run_dir / "agent.patch" if diff.stdout.strip() else None,
                f"Container solver failed with exit code {completed.returncode}: {detail}",
                details={"partial_patch_captured": bool(diff.stdout.strip())},
            )
        if not diff.stdout.strip():
            return AgentOutput(None, "Container solver produced no code changes")
        return AgentOutput(run_dir / "agent.patch", None)


def _evaluate_patch_host(case, patch_path: Path, run_dir: Path) -> _PatchEvaluation:
    if read_metadata(case.case_dir).get('historical_pr_tests', {}).get('command_policy_version', 1) >= 2:
        raise PatchbenchError('Historical solver evaluation requires --container')
    if case.oracle_mode in {"existing_tests", "existing_tests_with_regression"}:
        raise PatchbenchError("existing_tests evaluation requires --container")
    with temporary_worktree(case.repo_path, case.base_commit) as checkout:
        patch_applied, patch_error = apply_patch(checkout, patch_path)
        if not patch_applied:
            return _PatchEvaluation(False, False, f"Failed to apply agent patch: {patch_error}", [], 0, 0)
        files, added, removed = diff_stats(checkout)
        hidden_applied, hidden_error = apply_patch(checkout, case.test_patch)
        if not hidden_applied:
            return _PatchEvaluation(True, False, f"Failed to apply test patch: {hidden_error}", files, added, removed)
        cwd = _execution_cwd(checkout, case.workdir)
        setup_error = _run_setup_commands(case, cwd, run_dir, prefix="run_setup")
        if setup_error:
            (run_dir / "test.log").write_text(setup_error + "\n", encoding="utf-8")
            return _PatchEvaluation(
                True, False, setup_error, files, added, removed, FailureMode.OTHER
            )
        test = run_command(
            case.test_command,
            cwd=cwd,
            env_overrides=case.env,
            timeout_seconds=case.timeout_seconds,
        )
        write_log(run_dir / "test.log", test)
        if test.timed_out:
            return _PatchEvaluation(
                True,
                False,
                f"Tests timed out after {case.timeout_seconds} seconds",
                files,
                added,
                removed,
                FailureMode.TIMEOUT,
            )
        error = None if test.exit_code == 0 else f"Tests failed with exit code {test.exit_code}"
        return _PatchEvaluation(True, test.exit_code == 0, error, files, added, removed)


def _evaluate_patch_container(case, patch_path: Path, run_dir: Path) -> _PatchEvaluation:
    if read_metadata(case.case_dir).get('historical_pr_tests', {}).get('command_policy_version', 1) >= 2:
        from patchbench.historical_pr_tests import evaluate_historical_patch
        return evaluate_historical_patch(case, patch_path, run_dir)
    if case.oracle_mode in {"existing_tests", "existing_tests_with_regression"}:
        from patchbench.mirroring.validation import evaluate_existing_patch
        return evaluate_existing_patch(case, patch_path, run_dir)
    with temporary_checkout(case.repo_path, case.base_commit) as checkout:
        root = checkout.parent
        # Evaluator images provide the test runtime, and need not contain Git.
        # Apply exact patches on the host before mounting the isolated checkout.
        applied, error = apply_patch(checkout, patch_path)
        if not applied:
            message = f"Failed to apply agent patch: {error}"
            (run_dir / "test.log").write_text(message + "\n", encoding="utf-8")
            return _PatchEvaluation(False, False, message, [], 0, 0)
        files, added, removed = diff_stats(checkout)
        protected = set(read_metadata(case.case_dir).get('historical_pr_tests', {}).get('test_side_files', []))
        changed_tests = sorted(protected.intersection(files))
        if changed_tests:
            message = "Agent patch changes protected test inputs: " + ", ".join(changed_tests)
            (run_dir / "test.log").write_text(message + "\n", encoding="utf-8")
            return _PatchEvaluation(True, False, message, files, added, removed)
        applied, error = apply_patch(checkout, case.test_patch)
        if not applied:
            message = f"Failed to apply test patch: {error}"
            (run_dir / "test.log").write_text(message + "\n", encoding="utf-8")
            return _PatchEvaluation(True, False, message, files, added, removed, FailureMode.OTHER)
        status_path = root / "status"
        cwd = f"repo/{case.workdir}" if case.workdir else "repo"
        setup_script = ""
        for setup in case.setup_commands:
            setup_script += (
                f"{_bounded_shell_command(shlex.join(setup), case.timeout_seconds)}\n"
                "rc=$?\n"
                "if [ $rc -ne 0 ]; then echo SETUP_EXIT_CODE=$rc >> /host/status; exit 0; fi\n"
            )
        script = f"""
: > /host/status
{setup_script}
{_bounded_shell_command(shlex.join(case.test_command), case.timeout_seconds)}
rc=$?
echo TEST_EXIT_CODE=$rc >> /host/status
exit 0
"""
        completed = run_in_container(
            _evaluation_image(case),
            root,
            script,
            workdir=cwd,
            env_overrides=case.env,
            insecure_mounts=True,
            timeout_seconds=(len(case.setup_commands) + 1)
            * case.timeout_seconds
            + 30,
        )
        log = completed.stdout
        if completed.stderr:
            log += ("\n" if log else "") + completed.stderr
        (run_dir / "test.log").write_text(log, encoding="utf-8")
        if completed.timed_out or completed.interrupted:
            return _PatchEvaluation(
                True,
                False,
                (
                    "Container evaluation was interrupted"
                    if completed.interrupted
                    else f"Container evaluation timed out after {case.timeout_seconds} seconds"
                ),
                files,
                added,
                removed,
                FailureMode.OTHER if completed.interrupted else FailureMode.TIMEOUT,
            )
        if completed.returncode != 0 or not status_path.exists():
            return _PatchEvaluation(
                True, False, "Container evaluation failed", files, added, removed, FailureMode.OTHER
            )

        status = dict(
            line.split("=", 1)
            for line in status_path.read_text(encoding="utf-8").splitlines()
            if "=" in line
        )
        if "SETUP_EXIT_CODE" in status:
            return _PatchEvaluation(
                True,
                False,
                f"Setup failed with exit code {status['SETUP_EXIT_CODE']}",
                files,
                added,
                removed,
                FailureMode.OTHER,
            )
        exit_code = int(status.get("TEST_EXIT_CODE", "1"))
        if exit_code == 124:
            return _PatchEvaluation(
                True,
                False,
                f"Tests timed out after {case.timeout_seconds} seconds",
                files,
                added,
                removed,
                FailureMode.TIMEOUT,
            )
        error = None if exit_code == 0 else f"Tests failed with exit code {exit_code}"
        return _PatchEvaluation(True, exit_code == 0, error, files, added, removed)


def _read_numstat(path: Path) -> tuple[list[str], int, int]:
    files: list[str] = []
    added = removed = 0
    if not path.exists():
        return files, added, removed
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        plus, minus, filename = parts
        files.append(filename)
        added += int(plus) if plus.isdigit() else 0
        removed += int(minus) if minus.isdigit() else 0
    return sorted(files), added, removed


def _create_run_result(
    case,
    agent_name: str,
    run_id: str,
    prompt_mode: str,
    patch_applied: bool,
    resolved: bool,
    failure_mode: str,
    error: str | None,
    files_changed: list[str],
    added_lines: int,
    removed_lines: int,
    runtime_seconds: float,
    agent_patch_path: Path,
    prompt_path: Path,
    test_log_path: Path | None,
    metadata_path: Path | None,
    agent_execution_mode: str,
    evaluation_execution_mode: str,
    agent_image: str | None,
    agent_command_sanitized: str | None,
    model: str | None,
    model_source: str | None,
    prompt_hash: str,
    agent_timeout_seconds: int,
    agent_details: dict[str, Any],
    patch_comparison: PatchComparison,
) -> RunResult:
    """Create a RunResult with artifact paths (relative to run_dir)."""
    artifacts = {
        "agent_patch": agent_patch_path.name,
        "prompt": prompt_path.name,
    }
    if test_log_path is not None:
        artifacts["test_log"] = test_log_path.name
    if metadata_path is not None:
        artifacts["metadata"] = metadata_path.name
    trajectory_path = agent_patch_path.parent / "trajectory.log"
    if trajectory_path.exists():
        artifacts["trajectory"] = trajectory_path.name
    events_path = agent_patch_path.parent / "agent_events.jsonl"
    if events_path.exists():
        artifacts["agent_events"] = events_path.name
    debug_path = agent_patch_path.parent / "agent_debug.log"
    if debug_path.exists():
        artifacts["agent_debug"] = debug_path.name
    mcp_activity_path = agent_patch_path.parent / "mcp_activity.jsonl"
    if mcp_activity_path.exists():
        artifacts["mcp_activity"] = mcp_activity_path.name
    artifacts["result"] = "result.json"

    f2p_total = p2p_total = 0
    if read_metadata(case.case_dir).get('historical_pr_tests', {}).get('baseline_record'):
        from patchbench.historical_pr_tests import historical_baseline
        baseline = historical_baseline(case)
        f2p_total, p2p_total = len(baseline['f2p_tests']), len(baseline['p2p_tests'])
    if case.oracle_mode in {"existing_tests", "existing_tests_with_regression"}:
        benchmark = read_metadata(case.case_dir)['benchmark']
        f2p_total, p2p_total = len(benchmark['f2p_tests']), len(benchmark['p2p_tests'])

    return RunResult(
        case_id=case.id,
        repo=str(case.repo_path),
        agent=agent_name,
        run_id=run_id,
        prompt_mode=prompt_mode,
        agent_execution_mode=agent_execution_mode,
        evaluation_execution_mode=evaluation_execution_mode,
        agent_image=agent_image,
        agent_command_sanitized=agent_command_sanitized,
        model=model,
        model_source=model_source,
        prompt_hash=prompt_hash,
        agent_timeout_seconds=agent_timeout_seconds,
        agent_harness=agent_details.get("agent_harness"),
        agent_harness_version=agent_details.get("agent_harness_version"),
        provider=agent_details.get("provider"),
        provider_base_url_host=agent_details.get("provider_base_url_host"),
        requested_model=agent_details.get("requested_model"),
        actual_model=agent_details.get("actual_model"),
        agent_max_turns=agent_details.get("agent_max_turns"),
        agent_num_turns=agent_details.get("agent_num_turns"),
        agent_activity_observed=bool(agent_details.get("agent_activity_observed", False)),
        agent_event_count=int(agent_details.get("agent_event_count", 0)),
        agent_tool_event_count=int(agent_details.get("agent_tool_event_count", 0)),
        agent_first_event_seconds=agent_details.get("agent_first_event_seconds"),
        agent_last_event_seconds=agent_details.get("agent_last_event_seconds"),
        agent_seconds_since_last_event_at_exit=agent_details.get(
            "agent_seconds_since_last_event_at_exit"
        ),
        agent_termination_reason=agent_details.get("agent_termination_reason"),
        agent_execution_detail=agent_details.get("agent_execution_detail"),
        agent_session_id=agent_details.get("agent_session_id"),
        agent_stop_reason=agent_details.get("agent_stop_reason"),
        agent_permission_denial_count=int(
            agent_details.get("agent_permission_denial_count", 0)
        ),
        agent_api_error_count=int(agent_details.get("agent_api_error_count", 0)),
        input_tokens=agent_details.get("input_tokens"),
        output_tokens=agent_details.get("output_tokens"),
        cache_read_tokens=agent_details.get("cache_read_tokens"),
        reported_cost_usd=agent_details.get("reported_cost_usd"),
        mcp_enabled=bool(agent_details.get("mcp_enabled", False)),
        mcp_servers=list(agent_details.get("mcp_servers", [])),
        mcp_condition=str(agent_details.get("mcp_condition", "none")),
        mcp_server_name=agent_details.get("mcp_server_name"),
        mcp_profile=agent_details.get("mcp_profile"),
        mcp_revision=agent_details.get("mcp_revision"),
        mcp_revision_dirty=agent_details.get("mcp_revision_dirty"),
        mcp_process_started=agent_details.get("mcp_process_started"),
        mcp_server_statuses=dict(agent_details.get("mcp_server_statuses", {})),
        mcp_tools_discovered=agent_details.get("mcp_tools_discovered"),
        mcp_activity_observed=bool(
            agent_details.get("mcp_activity_observed", False)
        ),
        mcp_tool_call_count=int(agent_details.get("mcp_tool_call_count", 0)),
        mcp_tool_success_count=int(
            agent_details.get("mcp_tool_success_count", 0)
        ),
        mcp_tool_error_count=int(agent_details.get("mcp_tool_error_count", 0)),
        mcp_telemetry_available=bool(
            agent_details.get("mcp_telemetry_available", False)
        ),
        partial_patch_captured=bool(agent_details.get("partial_patch_captured", False)),
        evaluation_skipped=bool(agent_details.get("evaluation_skipped", False)),
        evaluation_skip_reason=agent_details.get("evaluation_skip_reason"),
        patch_comparison=patch_comparison,
        patch_applied=patch_applied,
        resolved=resolved,
        f2p_passed=f2p_total if resolved else 0,
        f2p_total=f2p_total,
        p2p_passed=p2p_total if resolved else 0,
        p2p_total=p2p_total,
        runtime_seconds=runtime_seconds,
        files_changed=files_changed,
        lines_added=added_lines,
        lines_removed=removed_lines,
        failure_mode=failure_mode,
        error=error,
        artifacts=artifacts,
    )


def _write_run_result(path: Path, result: RunResult) -> None:
    """Write run result to JSON file."""
    payload = {
        "case_id": result.case_id,
        "repo": result.repo,
        "agent": result.agent,
        "run_id": result.run_id,
        "prompt_mode": result.prompt_mode,
        "agent_execution_mode": result.agent_execution_mode,
        "evaluation_execution_mode": result.evaluation_execution_mode,
        "agent_image": result.agent_image,
        "agent_command_sanitized": result.agent_command_sanitized,
        "model": result.model,
        "model_source": result.model_source,
        "prompt_hash": result.prompt_hash,
        "agent_timeout_seconds": result.agent_timeout_seconds,
        "agent_harness": result.agent_harness,
        "agent_harness_version": result.agent_harness_version,
        "provider": result.provider,
        "provider_base_url_host": result.provider_base_url_host,
        "requested_model": result.requested_model,
        "actual_model": result.actual_model,
        "agent_max_turns": result.agent_max_turns,
        "agent_num_turns": result.agent_num_turns,
        "agent_activity_observed": result.agent_activity_observed,
        "agent_event_count": result.agent_event_count,
        "agent_tool_event_count": result.agent_tool_event_count,
        "agent_first_event_seconds": result.agent_first_event_seconds,
        "agent_last_event_seconds": result.agent_last_event_seconds,
        "agent_seconds_since_last_event_at_exit": result.agent_seconds_since_last_event_at_exit,
        "agent_termination_reason": result.agent_termination_reason,
        "agent_execution_detail": result.agent_execution_detail,
        "agent_session_id": result.agent_session_id,
        "agent_stop_reason": result.agent_stop_reason,
        "agent_permission_denial_count": result.agent_permission_denial_count,
        "agent_api_error_count": result.agent_api_error_count,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "cache_read_tokens": result.cache_read_tokens,
        "reported_cost_usd": result.reported_cost_usd,
        "mcp_enabled": result.mcp_enabled,
        "mcp_servers": result.mcp_servers,
        "mcp_condition": result.mcp_condition,
        "mcp_server_name": result.mcp_server_name,
        "mcp_profile": result.mcp_profile,
        "mcp_revision": result.mcp_revision,
        "mcp_revision_dirty": result.mcp_revision_dirty,
        "mcp_process_started": result.mcp_process_started,
        "mcp_server_statuses": result.mcp_server_statuses,
        "mcp_tools_discovered": result.mcp_tools_discovered,
        "mcp_activity_observed": result.mcp_activity_observed,
        "mcp_tool_call_count": result.mcp_tool_call_count,
        "mcp_tool_success_count": result.mcp_tool_success_count,
        "mcp_tool_error_count": result.mcp_tool_error_count,
        "mcp_telemetry_available": result.mcp_telemetry_available,
        "partial_patch_captured": result.partial_patch_captured,
        "evaluation_skipped": result.evaluation_skipped,
        "evaluation_skip_reason": result.evaluation_skip_reason,
        "patch_comparison": result.patch_comparison.to_dict(),
        "patch_applied": result.patch_applied,
        "resolved": result.resolved,
        "f2p_passed": result.f2p_passed,
        "f2p_total": result.f2p_total,
        "p2p_passed": result.p2p_passed,
        "p2p_total": result.p2p_total,
        "runtime_seconds": result.runtime_seconds,
        "files_changed": result.files_changed,
        "lines_added": result.lines_added,
        "lines_removed": result.lines_removed,
        "failure_mode": result.failure_mode,
        "error": result.error,
        "artifacts": result.artifacts,
    }
    write_json(path, payload)


def _write_validation(path: Path, result: ValidationResult) -> ValidationResult:
    write_json(path, result.to_dict())
    return result


def _write_evaluation(path: Path, result: EvaluationResult) -> EvaluationResult:
    write_json(path, result.to_dict())
    return result


def _execution_cwd(worktree_path: Path, workdir: str | None) -> Path:
    root = worktree_path.resolve()
    cwd = root if workdir is None else (root / workdir).resolve()
    try:
        cwd.relative_to(root)
    except ValueError as exc:
        raise PatchbenchError(f"Resolved workdir escapes the repository: {cwd}") from exc
    if not cwd.exists() or not cwd.is_dir():
        raise PatchbenchError(f"Resolved workdir does not exist or is not a directory: {cwd}")
    return cwd


def _run_setup_commands(case, cwd: Path, logs_dir: Path, prefix: str) -> str | None:
    for index, command in enumerate(case.setup_commands, start=1):
        result = run_command(
            list(command),
            cwd=cwd,
            env_overrides=case.env,
            timeout_seconds=case.timeout_seconds,
        )
        write_log(logs_dir / f"{prefix}_{index:02d}.log", result)
        if result.exit_code != 0:
            detail = (
                f"timed out after {case.timeout_seconds} seconds"
                if result.timed_out
                else f"failed with exit code {result.exit_code}"
            )
            return f"Setup command {detail} ({shlex.join(command)})"
    return None


def _container_setup_script(
    commands: list[str], marker: str, timeout_seconds: int
) -> str:
    lines: list[str] = []
    for command in commands:
        lines.extend(
            [
                _bounded_shell_command(command, timeout_seconds),
                "setup_rc=$?",
                f'if [ $setup_rc -ne 0 ]; then echo "{marker}=$setup_rc" >> /host/test_results.txt; exit 0; fi',
            ]
        )
    return "\n".join(lines)


def _bounded_shell_command(command: str, timeout_seconds: int) -> str:
    return f"timeout --signal=TERM --kill-after=5 {timeout_seconds}s {command}"


def _optional_exit_code(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _evaluation_image(case) -> str:
    # Naming fallback is confined to legacy cases without a manifest.
    return case.image or image_name_for_repo(case.repo_path.name)


def _solver_image(case, agent_name: str) -> str | None:
    if case.repository_id:
        manifest = load_repository(case.repository_id, case.case_dir)
        if agent_name == "claude-code":
            raw = read_metadata(case.case_dir)
            historical = raw.get('historical_pr_tests', {})
            if historical.get('framework') == 'maven':
                java = historical.get('selection', {}).get('java_version')
                runtime = manifest.config.get('preparation_runtimes', {}).get(java, {})
                return runtime.get('solver_image') or manifest.config.get('solver_image')
            profile = historical.get('profile')
            if profile:
                image = manifest.config.get('environment_profiles', {}).get(profile, {}).get('solver_image')
                if image:
                    return image
            if case.oracle_mode in {"existing_tests", "existing_tests_with_regression"}:
                import json
                benchmark = read_metadata(case.case_dir)['benchmark']
                environment = json.loads((case.case_dir / benchmark['environment_record']).read_text())
                runtime = manifest.config.get('preparation_runtimes', {}).get(environment.get('java_version'), {})
                return runtime.get('solver_image') or manifest.config.get('solver_image')
            return manifest.config.get("solver_image")
        if agent_name == "container-command":
            return _evaluation_image(case)
    # Existing standalone cases retain their historical agent defaults.
    return getattr(AGENT_REGISTRY[agent_name], "image", None)
