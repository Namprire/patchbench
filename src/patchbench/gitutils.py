from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from unidiff import PatchSet

from patchbench.exceptions import PatchbenchError
from patchbench.models import PatchComparison
from patchbench.runner import ensure_disk_space, redact_secrets, run_command


def extract_solver_patch(
    checkout: Path,
    base_commit: str = "HEAD",
    *,
    exclude_paths: tuple[str, ...] = (),
    max_bytes: int = 128 * 1024 * 1024,
) -> str:
    """Extract a complete diff against the solver's immutable starting commit.

    Callers running an agent must supply its base commit: the agent may commit
    its changes and move HEAD. The default preserves existing utility callers.

    The root Maven dependency cache is runtime data. Resetting its index
    entries first also handles agent-staged files and earlier intent-to-add
    entries. Files already tracked in HEAD retain their working-tree changes.
    """
    cache = ".m2/repository"
    reset = run_command(["git", "reset", "--quiet", "HEAD", "--", cache], cwd=checkout)
    if reset.exit_code:
        diagnostic = redact_secrets(reset.stderr.strip() or reset.stdout.strip())
        raise PatchbenchError(f"Could not reset Maven cache index entries during patch extraction: {diagnostic}")
    added = run_command(
        ["git", "add", "-N", "--all", "--", ".", f":(top,exclude){cache}"],
        cwd=checkout,
    )
    if added.exit_code:
        raise PatchbenchError("Could not index new files during patch extraction")
    command = ["git", "diff", base_commit, "--binary", "--", "."]
    command.extend(f":(exclude){path}" for path in exclude_paths)
    # Do not use communicate(): it retains the entire binary diff, then makes
    # additional copies while decoding. Never return a truncated submission.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, cwd=checkout, stdout=subprocess.PIPE, stderr=errors)
        try:
            assert process.stdout is not None
            size = 0
            while chunk := process.stdout.read(64 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise PatchbenchError(
                        f"Patch extraction exceeded {max_bytes} bytes; "
                        "no partial patch will be evaluated"
                    )
                output.write(chunk)
            if process.wait() != 0:
                raise PatchbenchError("Git diff failed during patch extraction")
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            if process.stdout is not None:
                process.stdout.close()
        output.seek(0)
        return output.read().decode("utf-8", errors="replace")


@contextmanager
def temporary_worktree(repo_path: Path, commit: str) -> Iterator[Path]:
    original_cwd = Path.cwd()
    temp_dir = Path(tempfile.mkdtemp(prefix="patchbench-worktree-"))
    worktree_path = temp_dir / "repo"
    add_result = run_command(
        ["git", "-C", str(repo_path), "worktree", "add", "--detach", str(worktree_path), commit],
        cwd=repo_path,
    )
    if add_result.exit_code != 0:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise PatchbenchError(
            f"Failed to create worktree at commit {commit}: {add_result.stderr.strip() or add_result.stdout.strip()}"
        )

    try:
        yield worktree_path
    finally:
        try:
            Path.cwd()
        except FileNotFoundError:
            os.chdir(original_cwd)
        remove_result = run_command(
            ["git", "-C", str(repo_path), "worktree", "remove", "--force", str(worktree_path)],
            cwd=repo_path,
        )
        if remove_result.exit_code != 0 and worktree_path.exists():
            shutil.rmtree(worktree_path, ignore_errors=True)
        shutil.rmtree(temp_dir, ignore_errors=True)


@contextmanager
def temporary_checkout(repo_path: Path, commit: str) -> Iterator[Path]:
    """Create a self-contained, depth-1 checkout containing only the base commit."""
    ensure_disk_space(Path(tempfile.gettempdir()))
    temp_dir = Path(tempfile.mkdtemp(prefix="patchbench-checkout-"))
    checkout_path = temp_dir / "repo"
    checkout_path.mkdir()
    init_result = run_command(["git", "init"], cwd=checkout_path)
    if init_result.exit_code != 0:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise PatchbenchError(
            f"Failed to initialize solving checkout: {init_result.stderr.strip() or init_result.stdout.strip()}"
        )

    fetch_result = run_command(
        ["git", "fetch", "--depth=1", "--no-tags", str(repo_path), commit],
        cwd=checkout_path,
    )
    if fetch_result.exit_code != 0:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise PatchbenchError(
            f"Failed to fetch base commit {commit}: {fetch_result.stderr.strip() or fetch_result.stdout.strip()}"
        )

    checkout_result = run_command(
        ["git", "checkout", "--detach", "FETCH_HEAD"], cwd=checkout_path
    )
    if checkout_result.exit_code != 0:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise PatchbenchError(
            f"Failed to check out commit {commit}: {checkout_result.stderr.strip() or checkout_result.stdout.strip()}"
        )

    # FETCH_HEAD records the source path and is not needed after checkout.
    (checkout_path / ".git" / "FETCH_HEAD").unlink(missing_ok=True)
    try:
        yield checkout_path
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def ensure_git_repo(repo_path: Path) -> None:
    result = run_command(["git", "rev-parse", "--is-inside-work-tree"], cwd=repo_path)
    if result.exit_code != 0 or result.stdout.strip() != "true":
        raise PatchbenchError(f"Not a git repository: {repo_path}")


def ensure_commit_exists(repo_path: Path, commit: str) -> None:
    result = run_command(["git", "cat-file", "-e", f"{commit}^{{commit}}"], cwd=repo_path)
    if result.exit_code != 0:
        raise PatchbenchError(f"Commit does not exist in repository {repo_path}: {commit}")


def apply_patch(worktree_path: Path, patch_path: Path) -> tuple[bool, str | None]:
    check = run_command(
        ["git", "apply", "--check", "--verbose", "--whitespace=nowarn", str(patch_path)],
        cwd=worktree_path,
    )
    diagnostic = "\n".join((check.stdout, check.stderr)).strip()
    if check.exit_code != 0:
        message = diagnostic or "git apply failed"
        return False, message
    if re.search(
        r"\boffset\s+\d+\s+lines?\b|\bwith\s+fuzz\b",
        diagnostic,
        re.IGNORECASE,
    ):
        return False, f"Patch requires a non-exact application: {diagnostic}"
    result = run_command(
        ["git", "apply", "--whitespace=nowarn", str(patch_path)],
        cwd=worktree_path,
    )
    if result.exit_code != 0:
        return False, result.stderr.strip() or result.stdout.strip() or "git apply failed"
    return True, None


@contextmanager
def evaluation_test_patch(
    checkout: Path, base_commit: str, test_patch: Path | None,
    *, reset_files: tuple[str, ...] = (),
) -> Iterator[tuple[bool, str | None]]:
    """Apply official tests between targeted base resets, as in SWE-bench PR #539.

    Only paths from the official test patch or explicit frozen oracle inputs
    are reset. Unrelated model tests remain in place for evaluation.
    """
    patch = PatchSet(test_patch.read_text(encoding="utf-8")) if test_patch else PatchSet("")
    modified_files = [
        file.source_file[2:]
        for file in patch
        if file.source_file != "/dev/null" and file.source_file.startswith("a/")
    ]
    # Existing-tests mirrors have no developer test patch. Their original,
    # tracked test inputs are the frozen oracle and must be restored explicitly.
    modified_files = sorted(set(modified_files) | set(reset_files))
    new_files = [
        file.target_file[2:] if file.target_file.startswith("b/") else file.target_file
        for file in patch
        if file.source_file == "/dev/null"
    ]

    def reset() -> None:
        commands = []
        # An empty checkout path list would reset the entire model patch.
        if modified_files:
            commands.append(["git", "checkout", base_commit, "--", *modified_files])
        if new_files:
            commands.append(["rm", "-f", "--", *new_files])
        for command in commands:
            result = run_command(command, cwd=checkout)
            if result.exit_code != 0:
                raise PatchbenchError(
                    "Failed to reset official test patch files: "
                    + (result.stderr.strip() or result.stdout.strip())
                )

    try:
        reset()
        yield apply_patch(checkout, test_patch) if test_patch else (True, None)
    finally:
        reset()


def diff_stats(worktree_path: Path) -> tuple[list[str], int, int]:
    intent = run_command(["git", "add", "-N", "--all"], cwd=worktree_path)
    if intent.exit_code != 0:
        raise PatchbenchError(
            intent.stderr.strip() or intent.stdout.strip() or "git add -N failed"
        )
    result = run_command(["git", "diff", "HEAD", "--numstat", "--", "."], cwd=worktree_path)
    if result.exit_code != 0:
        raise PatchbenchError(result.stderr.strip() or result.stdout.strip() or "git diff failed")

    changed_files: list[str] = []
    added_lines = 0
    removed_lines = 0
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, removed, file_path = parts
        changed_files.append(file_path)
        if added.isdigit():
            added_lines += int(added)
        if removed.isdigit():
            removed_lines += int(removed)

    return sorted(changed_files), added_lines, removed_lines


def compare_patches(agent_patch: Path, gold_patch: Path) -> PatchComparison:
    """Return deterministic file and line overlap metrics for two unified diffs."""
    agent_files, agent_added, agent_removed = patch_stats(agent_patch)
    gold_files, gold_added, gold_removed = patch_stats(gold_patch)
    agent_set = set(agent_files)
    gold_set = set(gold_files)
    overlapping_files = sorted(agent_set & gold_set)
    union = agent_set | gold_set
    jaccard = len(overlapping_files) / len(union) if union else 1.0
    return PatchComparison(
        agent_files_changed=agent_files,
        gold_files_changed=gold_files,
        overlapping_files=overlapping_files,
        file_overlap_count=len(overlapping_files),
        file_overlap_jaccard=jaccard,
        exact_file_set_match=agent_set == gold_set,
        agent_lines_added=agent_added,
        agent_lines_removed=agent_removed,
        gold_lines_added=gold_added,
        gold_lines_removed=gold_removed,
    )


def patch_stats(patch_path: Path) -> tuple[list[str], int, int]:
    """Parse changed paths and line counts from a unified diff without raising."""
    try:
        lines = patch_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return [], 0, 0

    changed_files: set[str] = set()
    added_lines = 0
    removed_lines = 0
    in_file_diff = False
    for line in lines:
        if line.startswith("diff --git "):
            path = _diff_destination_path(line)
            in_file_diff = path is not None
            if path is not None:
                changed_files.add(path)
            continue
        if not in_file_diff:
            continue
        if line.startswith("+++ ") or line.startswith("--- "):
            continue
        if line.startswith("+"):
            added_lines += 1
        elif line.startswith("-"):
            removed_lines += 1

    return sorted(changed_files), added_lines, removed_lines


def _diff_destination_path(line: str) -> str | None:
    try:
        parts = shlex.split(line)
    except ValueError:
        return None
    if len(parts) != 4 or parts[:2] != ["diff", "--git"]:
        return None
    destination = parts[3]
    if not destination.startswith("b/") or len(destination) == 2:
        return None
    return destination[2:]
