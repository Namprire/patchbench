from __future__ import annotations

import os
import re
import shlex
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from patchbench.exceptions import PatchbenchError
from patchbench.models import PatchComparison
from patchbench.runner import run_command


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


def diff_stats(worktree_path: Path) -> tuple[list[str], int, int]:
    intent = run_command(["git", "add", "-N", "--all"], cwd=worktree_path)
    if intent.exit_code != 0:
        raise PatchbenchError(
            intent.stderr.strip() or intent.stdout.strip() or "git add -N failed"
        )
    result = run_command(["git", "diff", "--numstat", "--", "."], cwd=worktree_path)
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
