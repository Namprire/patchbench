"""Discovery-only ranking of GitLab merge requests for future benchmark cases."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from patchbench.exceptions import PatchbenchError
from patchbench.gitlab_client import GitLabClient
from patchbench.gitutils import ensure_commit_exists, ensure_git_repo
from patchbench.io import write_json
from patchbench.runner import redact_secrets, run_command


_SCORE_MANY_FILES = 25
_SCORE_HUGE_LINES = 1_000
_REJECT_MANY_FILES = 60
_REJECT_HUGE_LINES = 5_000


def discover_github_cases(*, github_repo, repo, repo_path, output_path, limit=100,
                          client=None, labels=None, candidate_numbers=None):
    """Use existing local-diff scoring and the exact merged-PR acquisition boundary."""
    from patchbench.github_client import GitHubClient
    github = client or GitHubClient()
    repository = Path(repo_path).expanduser().resolve()
    ensure_git_repo(repository)
    summaries = ([dict(number=n) for n in candidate_numbers[:limit]] if candidate_numbers
                 else github.list_merged_pull_requests(github_repo,limit,labels=labels))
    candidates = []
    for summary in summaries:
        number = summary['number']
        try:
            pr = github.fetch_pull_request(github_repo,number)
            if labels and not set(labels).issubset({v['name'] for v in pr.get('labels',[])}):
                continue
            base = pr['patch_base_sha']
            fix = pr.get('historical_fix_sha',pr['merge_commit_sha'])
            # Internal scoring adapter only; persisted provenance keeps original provider fields.
            normalized = dict(title=pr['title'],description=pr.get('body'),
                author=dict(username=pr.get('user',{}).get('login','unknown')),
                labels=[v['name'] for v in pr.get('labels',[])],merged_at=pr['merged_at'],
                target_branch=pr['base'].get('ref',''),source_branch=pr['head'].get('ref',''),
                related_issues=pr['related_issues'],diff_refs=dict(base_sha=base,head_sha=fix,start_sha=base))
            candidate = _build_candidate(project_id=github_repo,repo=repo,repository=repository,
                mr_iid=number,merge_request=normalized,client=github)
            candidate.pop('merge_request_iid')
            candidate.update(source='github',github_repo=github_repo,pull_request_number=number,
                web_url=pr.get('html_url'),merge_commit_sha=pr['merge_commit_sha'])
        except PatchbenchError as exc:
            candidate = dict(source='github',github_repo=github_repo,pull_request_number=number,
                has_code_changes=False,has_test_changes=False,base_commit=None,human_commit=None,
                candidate_score=0,rejection_reason=github.redact(str(exc)))
        candidates.append(candidate)
    candidates.sort(key=lambda c:(-c['candidate_score'],c['pull_request_number']))
    write_json(Path(output_path),candidates)
    return candidates


def discover_gitlab_cases(
    *,
    project_id: str | int,
    repo: str,
    repo_path: str | Path,
    output_path: str | Path,
    limit: int = 100,
    report_path: str | Path | None = None,
    gitlab_url: str | None = None,
    client: GitLabClient | None = None,
    labels: list[str] | None = None,
    candidate_numbers: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Rank merged MRs without creating benchmark cases or fetching diff content."""
    if limit <= 0:
        raise PatchbenchError("Candidate discovery limit must be greater than zero")

    repository = Path(repo_path).expanduser().resolve()
    ensure_git_repo(repository)
    output = Path(output_path).expanduser().resolve()
    gitlab = client or GitLabClient.from_environment(gitlab_url)

    summaries = ([dict(iid=n) for n in candidate_numbers[:limit]] if candidate_numbers else (
        gitlab.list_merged_merge_requests(project_id, limit, labels=labels)
        if labels else gitlab.list_merged_merge_requests(project_id, limit)
    ))
    candidates: list[dict[str, Any]] = []
    for summary in summaries:
        mr_iid = summary.get("iid")
        if not isinstance(mr_iid, int):
            raise PatchbenchError("GitLab merge request list contained an MR without an iid")
        merge_request = gitlab.fetch_merge_request_details(project_id, mr_iid)
        if labels and not set(labels).issubset(merge_request.get("labels") or []):
            continue
        candidates.append(
            _build_candidate(
                project_id=project_id,
                repo=repo,
                repository=repository,
                mr_iid=mr_iid,
                merge_request=merge_request,
                client=gitlab,
            )
        )
        if candidate_numbers and merge_request.get('state') != 'merged':
            candidates[-1].update(has_code_changes=False,selected=False,
                rejection_reason='Only merged merge requests can be pulled')

    candidates.sort(
        key=lambda candidate: (
            -candidate["candidate_score"],
            candidate["merge_request_iid"],
        )
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json(output, candidates)

    if report_path is not None:
        report = Path(report_path).expanduser().resolve()
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(
            _render_markdown(candidates, project_id=project_id, repo=repo),
            encoding="utf-8",
        )
    return candidates


def _build_candidate(
    *,
    project_id: str | int,
    repo: str,
    repository: Path,
    mr_iid: int,
    merge_request: dict[str, Any],
    client: GitLabClient,
) -> dict[str, Any]:
    diff_refs = merge_request.get("diff_refs")
    if not isinstance(diff_refs, dict):
        diff_refs = {}
    base_commit = _optional_text(diff_refs.get("base_sha"), client)
    head_commit = _optional_text(
        diff_refs.get("head_sha") or merge_request.get("sha"), client
    )
    start_commit = _optional_text(diff_refs.get("start_sha"), client)
    title = _text(merge_request.get("title") or "Untitled merge request", client)
    description = _text(merge_request.get("description") or "", client)
    labels = _text_list(merge_request.get("labels"), client)
    author = _author(merge_request.get("author"), client)
    source_branch = _text(merge_request.get("source_branch") or "", client)
    target_branch = _text(merge_request.get("target_branch") or "", client)
    related_issues = merge_request.get("related_issues")
    linked_issue_count = (
        len([issue for issue in related_issues if isinstance(issue, dict)])
        if isinstance(related_issues, list)
        else 0
    )

    paths: list[str] = []
    lines_added = 0
    lines_removed = 0
    local_diff_error: str | None = None
    if base_commit and head_commit:
        try:
            paths, lines_added, lines_removed = _local_diff_stats(
                repository, base_commit, head_commit
            )
        except PatchbenchError as exc:
            local_diff_error = _text(str(exc), client)

    code_paths, test_paths, docs_paths = _classify_paths(paths)
    has_code_changes = bool(code_paths)
    has_test_changes = bool(test_paths)
    likely_bugfix = _likely_bugfix(title, description, labels)
    likely_refactor = _likely_refactor(title, labels)
    is_release_target = _is_release_target(target_branch)
    complete_diff_refs = bool(base_commit and head_commit and start_commit)
    total_lines = lines_added + lines_removed
    docs_only = bool(paths) and not has_code_changes and not has_test_changes and bool(docs_paths)

    rejection_reasons: list[str] = []
    if not base_commit or not head_commit:
        missing = []
        if not base_commit:
            missing.append("diff_refs.base_sha")
        if not head_commit:
            missing.append("diff_refs.head_sha (or sha)")
        rejection_reasons.append(f"missing {' and '.join(missing)}")
    elif local_diff_error:
        rejection_reasons.append(f"local Git comparison unavailable: {local_diff_error}")
    elif not paths:
        rejection_reasons.append("local diff has no changed files")
    else:
        if docs_only:
            rejection_reasons.append("docs-only change")
        elif not has_code_changes:
            rejection_reasons.append("no non-test implementation changes")
        if not has_test_changes:
            rejection_reasons.append("no test changes")
        if len(paths) > _REJECT_MANY_FILES:
            rejection_reasons.append(f"too many changed files ({len(paths)})")
        if total_lines > _REJECT_HUGE_LINES:
            rejection_reasons.append(f"diff is too large ({total_lines} changed lines)")
        if likely_refactor and not likely_bugfix:
            rejection_reasons.append("appears to be refactor or formatting work")

    score = _score_candidate(
        has_code_changes=has_code_changes,
        has_test_changes=has_test_changes,
        likely_bugfix=likely_bugfix,
        linked_issue_count=linked_issue_count,
        changed_files_total=len(paths),
        total_lines=total_lines,
        docs_only=docs_only,
        complete_diff_refs=complete_diff_refs,
        release_target=is_release_target,
        likely_refactor=likely_refactor,
    )
    notes = _notes(
        complete_diff_refs=complete_diff_refs,
        linked_issue_count=linked_issue_count,
        is_release_target=is_release_target,
        docs_paths=docs_paths,
        local_diff_error=local_diff_error,
    )

    return {
        "project_id": project_id,
        "repo": _text(repo, client),
        "merge_request_iid": mr_iid,
        "title": title,
        "description_preview": _preview(description),
        "author": author,
        "labels": labels,
        "source_branch": source_branch,
        "target_branch": target_branch,
        "merged_at": _optional_text(merge_request.get("merged_at"), client),
        "base_commit": base_commit,
        "human_commit": head_commit,
        "head_commit": head_commit,
        "start_commit": start_commit,
        "changed_files_total": len(paths),
        "changed_file_paths": [_text(path, client) for path in paths],
        "changed_files_code": len(code_paths),
        "changed_files_tests": len(test_paths),
        "changed_files_docs": len(docs_paths),
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "has_test_changes": has_test_changes,
        "has_code_changes": has_code_changes,
        "likely_bugfix": likely_bugfix,
        "linked_issue_count": linked_issue_count,
        "candidate_score": score,
        "selected": not rejection_reasons,
        "rejection_reason": "; ".join(rejection_reasons) or None,
        "notes": notes,
    }


def _local_diff_stats(
    repository: Path, base_commit: str, human_commit: str
) -> tuple[list[str], int, int]:
    ensure_commit_exists(repository, base_commit)
    ensure_commit_exists(repository, human_commit)
    ancestor = run_command(
        ["git", "merge-base", "--is-ancestor", base_commit, human_commit],
        cwd=repository,
    )
    if ancestor.exit_code != 0:
        raise PatchbenchError("base commit is not an ancestor of the human commit")

    names = run_command(
        ["git", "diff", "--name-status", "--no-renames", base_commit, human_commit, "--"],
        cwd=repository,
    )
    if names.exit_code != 0:
        raise PatchbenchError(names.stderr.strip() or "failed to list changed files")
    paths = _changed_paths(names.stdout)

    numstat = run_command(
        ["git", "diff", "--numstat", "--no-renames", base_commit, human_commit, "--"],
        cwd=repository,
    )
    if numstat.exit_code != 0:
        raise PatchbenchError(numstat.stderr.strip() or "failed to collect diff statistics")
    lines_added, lines_removed = _line_stats(numstat.stdout)
    return paths, lines_added, lines_removed


def _changed_paths(output: str) -> list[str]:
    paths: list[str] = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1]:
            paths.append(parts[-1])
    return sorted(set(paths))


def _line_stats(output: str) -> tuple[int, int]:
    lines_added = 0
    lines_removed = 0
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, removed, _ = parts
        if added.isdigit():
            lines_added += int(added)
        if removed.isdigit():
            lines_removed += int(removed)
    return lines_added, lines_removed


def _classify_paths(paths: list[str]) -> tuple[list[str], list[str], list[str]]:
    code_paths: list[str] = []
    test_paths: list[str] = []
    docs_paths: list[str] = []
    for path in paths:
        if _is_test_path(path):
            test_paths.append(path)
        elif _is_docs_path(path):
            docs_paths.append(path)
        else:
            code_paths.append(path)
    return code_paths, test_paths, docs_paths


def _is_test_path(path: str) -> bool:
    pure_path = PurePosixPath(path.lower())
    parts = pure_path.parts
    filename = pure_path.name
    return (
        "test" in parts
        or "tests" in parts
        or filename.startswith("test_")
        or filename.startswith("tests_")
        or "_test" in filename
    )


def _is_docs_path(path: str) -> bool:
    pure_path = PurePosixPath(path.lower())
    filename = pure_path.name
    stem = pure_path.stem
    return (
        "doc" in pure_path.parts
        or "docs" in pure_path.parts
        or pure_path.suffix in {".md", ".rst", ".txt"}
        or stem in {"readme", "changelog", "license", "copying"}
        or filename.startswith("readme")
    )


def _score_candidate(
    *,
    has_code_changes: bool,
    has_test_changes: bool,
    likely_bugfix: bool,
    linked_issue_count: int,
    changed_files_total: int,
    total_lines: int,
    docs_only: bool,
    complete_diff_refs: bool,
    release_target: bool,
    likely_refactor: bool,
) -> int:
    score = 0
    score += 3 if has_code_changes else -3
    score += 3 if has_test_changes else -3
    score += 2 if likely_bugfix else 0
    score += 1 if linked_issue_count else 0
    score -= 2 if changed_files_total > _SCORE_MANY_FILES else 0
    score -= 2 if total_lines > _SCORE_HUGE_LINES else 0
    score -= 3 if docs_only else 0
    score -= 1 if not complete_diff_refs else 0
    score -= 2 if release_target else 0
    score -= 2 if likely_refactor and not likely_bugfix else 0
    return score


def _likely_bugfix(title: str, description: str, labels: list[str]) -> bool:
    text = " ".join([title, description, *labels]).lower()
    markers = (
        "fix",
        "bug",
        "regression",
        "broken",
        "error",
        "fail",
        "incorrect",
        "wrong",
        "crash",
        "exception",
    )
    return any(marker in text for marker in markers)


def _likely_refactor(title: str, labels: list[str]) -> bool:
    text = " ".join([title, *labels]).lower()
    markers = ("refactor", "format", "style", "lint", "cleanup", "clean-up", "rename")
    return any(marker in text for marker in markers)


def _is_release_target(branch: str) -> bool:
    branch = branch.lower()
    return any(marker in branch for marker in ("release", "stable", "maintenance", "lts"))


def _notes(
    *,
    complete_diff_refs: bool,
    linked_issue_count: int,
    is_release_target: bool,
    docs_paths: list[str],
    local_diff_error: str | None,
) -> list[str]:
    notes = [
        "complete base/start/head diff refs" if complete_diff_refs else "missing start SHA in diff refs",
        f"linked issues: {linked_issue_count}",
    ]
    if is_release_target:
        notes.append("targets a release-like branch")
    if docs_paths:
        notes.append(f"documentation files changed: {len(docs_paths)}")
    if local_diff_error:
        notes.append(f"local Git error: {local_diff_error}")
    return notes


def _author(value: Any, client: GitLabClient) -> str:
    if isinstance(value, dict):
        return _text(value.get("username") or value.get("name") or "unknown", client)
    return "unknown"


def _text_list(value: Any, client: GitLabClient) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_text(item, client) for item in value]


def _text(value: Any, client: GitLabClient) -> str:
    return redact_secrets(client.redact(str(value)))


def _optional_text(value: Any, client: GitLabClient) -> str | None:
    return None if value is None else _text(value, client)


def _preview(value: str, limit: int = 240) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else compact[: limit - 3].rstrip() + "..."


def _render_markdown(
    candidates: list[dict[str, Any]], *, project_id: str | int, repo: str
) -> str:
    selected_count = sum(candidate["selected"] for candidate in candidates)
    lines = [
        "# PatchBench GitLab Case Candidates",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"Repository: {_markdown(repo)} (GitLab project {project_id})",
        f"Merged MRs inspected: {len(candidates)}; selected for review: {selected_count}.",
        "",
        "## Top Candidates",
        "",
        "| MR | Score | Status | Code | Tests | Lines | Target | Title | Why |",
        "| --- | ---: | --- | ---: | ---: | ---: | --- | --- | --- |",
    ]
    for candidate in candidates[:10]:
        status = "selected" if candidate["selected"] else "rejected"
        why = candidate["rejection_reason"] or "; ".join(candidate["notes"])
        lines.append(
            "| "
            + " | ".join(
                [
                    f"!{candidate['merge_request_iid']}",
                    str(candidate["candidate_score"]),
                    status,
                    str(candidate["changed_files_code"]),
                    str(candidate["changed_files_tests"]),
                    f"+{candidate['lines_added']}/-{candidate['lines_removed']}",
                    _markdown(candidate["target_branch"]),
                    _markdown(candidate["title"]),
                    _markdown(why),
                ]
            )
            + " |"
        )
    if not candidates:
        lines.append("| _No merged merge requests found_ |  |  |  |  |  |  |  |  |")
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Scores favor implementation changes, tests, bug-fix signals, and linked issues.",
            "- GitLab diff content is not fetched. File and line statistics come from the local Git repository.",
            "- This report is discovery-only and does not create benchmark cases or patches.",
            "",
        ]
    )
    return "\n".join(lines)


def _markdown(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
