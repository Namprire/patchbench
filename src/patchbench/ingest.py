"""GitLab-assisted creation of frozen draft benchmark cases."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
from typing import Any

import yaml

from patchbench import __version__
from patchbench.case import is_safe_path_component
from patchbench.repositories import load_repository, DEFAULT_MANIFEST_ROOT
from patchbench.exceptions import PatchbenchError
from patchbench.gitlab_client import GitLabClient
from patchbench.gitutils import ensure_commit_exists, ensure_git_repo
from patchbench.io import write_json
from patchbench.runner import run_command


def ingest_gitlab_mr(
    *,
    project_id: str | int,
    mr_iid: int,
    case_id: str,
    repo_path: str | Path,
    output_path: str | Path,
    gitlab_url: str | None = None,
    client: GitLabClient | None = None,
    repository_id: str | None = None,
    candidate_features: dict[str, Any] | None = None,
) -> Path:
    """Create a draft case from GitLab metadata and a local Git repository."""
    if not is_safe_path_component(case_id):
        raise PatchbenchError("case ID must be a single non-empty path component")
    gitlab = client or GitLabClient.from_environment(gitlab_url)
    repository = Path(repo_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    if output.exists():
        if not output.is_dir():
            raise PatchbenchError(f"Output path is not a directory: {output}")
        if any(output.iterdir()):
            raise PatchbenchError(f"Output directory is not empty: {output}")

    merge_request = gitlab.fetch_merge_request(project_id, mr_iid)
    base_commit, human_commit = _resolve_commits(merge_request)
    gold_patch = _generate_gold_patch(repository, base_commit, human_commit)
    imported_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    repo_name = repository_id or repository.name

    issue_text = _build_issue(merge_request, gitlab)
    provenance = _build_provenance(
        merge_request=merge_request,
        client=gitlab,
        project_id=project_id,
        mr_iid=mr_iid,
        base_commit=base_commit,
        human_commit=human_commit,
        imported_at=imported_at,
    )
    metadata = _build_metadata(
        case_id=case_id,
        repo_name=repo_name,
        repo_path=repository,
        project_id=project_id,
        mr_iid=mr_iid,
        gitlab_url=gitlab.gitlab_url,
        base_commit=base_commit,
        human_commit=human_commit,
        repository_id=repository_id,
        output_path=output,
    )

    safe_mr = {
        "title": gitlab.redact(merge_request.get("title") or ""),
        "description": gitlab.redact(merge_request.get("description") or ""),
        "labels": provenance["labels"], "iid": mr_iid,
        "merged_at": provenance["merged_at"], "target_branch": provenance["target_branch"],
        "related_issues": [{"title": gitlab.redact(item.get("title") or ""),
                            "description": gitlab.redact(item.get("description") or "")}
                           for item in (merge_request.get("related_issues") or []) if isinstance(item, dict)],
    }
    return _write_draft(output, issue_text, gold_patch, metadata, provenance,
                        repo_name, case_id, safe_mr, candidate_features)


def _write_draft(output, issue_text, gold_patch, metadata, provenance, repo_name, case_id, snapshot, candidate_features=None):
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".patchbench-ingest-", dir=output.parent) as raw:
        prepared = Path(raw) / "case"
        prepared.mkdir()
        (prepared / "issue.md").write_text(issue_text, encoding="utf-8")
        (prepared / "gold.patch").write_text(gold_patch, encoding="utf-8")
        (prepared / "test.patch").write_text(
            "# DRAFT: add hidden regression tests before validating this case.\n",
            encoding="utf-8",
        )
        write_json(prepared / "provenance.json", provenance)
        # Curators receive allowlisted metadata, never the human patch. Keep this
        # local evidence out of the solver prompt and the published case inputs.
        from patchbench.curation.snapshot import snapshot_from_acquisition

        private = prepared / ".patchbench"
        private.mkdir(mode=0o700)
        write_json(private / "candidate.json", snapshot_from_acquisition(
            repo_name, case_id, snapshot, issue_text, candidate_features or {},
        ))
        (prepared / "meta.yaml").write_text(
            yaml.safe_dump(metadata, sort_keys=False), encoding="utf-8"
        )
        if output.exists():
            if any(output.iterdir()):
                raise PatchbenchError(f"Output directory is not empty: {output}")
            output.rmdir()
        prepared.rename(output)
    return output


def _resolve_commits(merge_request: dict[str, Any]) -> tuple[str, str]:
    diff_refs = merge_request.get("diff_refs")
    if not isinstance(diff_refs, dict):
        diff_refs = {}
    base_commit = diff_refs.get("base_sha")
    human_commit = diff_refs.get("head_sha") or merge_request.get("sha")
    missing: list[str] = []
    if not base_commit:
        missing.append("diff_refs.base_sha")
    if not human_commit:
        missing.append("diff_refs.head_sha (or sha)")
    if missing:
        raise PatchbenchError(
            "Cannot determine exact benchmark commits from GitLab metadata. "
            f"Missing fields: {', '.join(missing)}. No commits were guessed."
        )
    return str(base_commit), str(human_commit)


def _generate_gold_patch(repository: Path, base_commit: str, human_commit: str) -> str:
    ensure_git_repo(repository)
    ensure_commit_exists(repository, base_commit)
    ensure_commit_exists(repository, human_commit)
    ancestor = run_command(
        ["git", "merge-base", "--is-ancestor", base_commit, human_commit],
        cwd=repository,
    )
    if ancestor.exit_code != 0:
        raise PatchbenchError(
            "Base commit is not an ancestor of the human commit in the local repository"
        )
    diff = run_command(
        ["git", "diff", "--binary", base_commit, human_commit, "--"], cwd=repository
    )
    if diff.exit_code != 0:
        raise PatchbenchError(f"Failed to generate local gold patch: {diff.stderr.strip()}")
    if not diff.stdout.strip():
        raise PatchbenchError("Local base and human commits produce an empty gold patch")
    return diff.stdout


def _build_issue(merge_request: dict[str, Any], client: GitLabClient) -> str:
    title = client.redact(merge_request.get("title") or "Untitled merge request")
    description = client.redact(
        merge_request.get("description") or "No merge request description was provided."
    ).strip()
    lines = [f"# {title}", "", description]
    related = merge_request.get("related_issues", [])
    if isinstance(related, list) and related:
        lines.extend(["", "## Related Issues"])
        for issue in related:
            if not isinstance(issue, dict):
                continue
            reference = _issue_reference(issue)
            issue_title = client.redact(issue.get("title") or "Untitled issue")
            issue_body = client.redact(
                issue.get("description") or "No issue description was provided."
            ).strip()
            heading = f"### {reference}: {issue_title}" if reference else f"### {issue_title}"
            lines.extend(["", heading, "", issue_body])
    return "\n".join(lines).rstrip() + "\n"


def _build_provenance(
    *,
    merge_request: dict[str, Any],
    client: GitLabClient,
    project_id: str | int,
    mr_iid: int,
    base_commit: str,
    human_commit: str,
    imported_at: str,
) -> dict[str, Any]:
    author = merge_request.get("author")
    author_name = "unknown"
    if isinstance(author, dict):
        author_name = author.get("username") or author.get("name") or "unknown"
    labels = merge_request.get("labels")
    return {
        "source": "gitlab",
        "gitlab_url": client.gitlab_url,
        "project_id": project_id,
        "merge_request_iid": mr_iid,
        "mr_title": client.redact(merge_request.get("title") or ""),
        "mr_author": client.redact(author_name),
        "source_branch": client.redact(merge_request.get("source_branch") or ""),
        "target_branch": client.redact(merge_request.get("target_branch") or ""),
        "labels": [client.redact(label) for label in labels]
        if isinstance(labels, list)
        else [],
        "created_at": _optional_redacted(merge_request.get("created_at"), client),
        "merged_at": _optional_redacted(merge_request.get("merged_at"), client),
        "base_commit": base_commit,
        "human_commit": human_commit,
        "related_issues": _related_issue_provenance(
            merge_request.get("related_issues"), client
        ),
        "imported_at": imported_at,
        "import_tool_version": __version__,
        "notes": [
            "gold.patch was generated from the local Git repository, not GitLab diff content.",
            "Draft case: test.patch and evaluation commands require manual completion.",
        ],
    }


def _build_metadata(
    *,
    case_id: str,
    repo_name: str,
    repo_path: Path,
    project_id: str | int,
    mr_iid: int,
    gitlab_url: str,
    base_commit: str,
    human_commit: str,
    repository_id: str | None = None,
    output_path: Path | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "id": case_id,
        "case_id": case_id,
        "repo": repo_name,
        "repo_path": str(repo_path),
        "base_commit": base_commit,
        "fix_commit": human_commit,
        "human_commit": human_commit,
        "problem_statement": "issue.md",
        "gold_patch": "gold.patch",
        "test_patch": "test.patch",
        "test_command": "false",
        "setup_commands": [],
        "env": {},
        "source": {
            "type": "gitlab",
            "gitlab_url": gitlab_url,
            "project_id": project_id,
            "merge_request_iid": mr_iid,
        },
        "artifacts": {
            "issue": "issue.md",
            "provenance": "provenance.json",
            "gold_patch": "gold.patch",
            "test_patch": "test.patch",
        },
        "timeout_seconds": 900,
        "draft": True,
        "draft_todos": [
            "Replace test.patch with hidden regression tests.",
            "Replace test_command=false with the deterministic evaluation command.",
            "Add setup_commands, workdir, and env only if required.",
        ],
    }
    if repository_id or (DEFAULT_MANIFEST_ROOT / f"{repo_name}.yaml").is_file():
        manifest = load_repository(repo_name, output_path if repository_id else None)
        metadata["repository"] = manifest.id
        if repo_path.resolve() == manifest.checkout():
            metadata.pop("repo_path")
        metadata.pop("repo")
    metadata["case_type"] = "coding"
    return metadata


def _issue_reference(issue: dict[str, Any]) -> str:
    references = issue.get("references")
    if isinstance(references, dict):
        return str(references.get("full") or references.get("relative") or "")
    iid = issue.get("iid")
    return f"#{iid}" if iid is not None else ""


def _related_issue_provenance(value: Any, client: GitLabClient) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for issue in value:
        if not isinstance(issue, dict):
            continue
        result.append(
            {
                "project_id": issue.get("project_id"),
                "iid": issue.get("iid"),
                "reference": client.redact(_issue_reference(issue)),
                "title": client.redact(issue.get("title") or ""),
                "web_url": client.redact(issue.get("web_url") or ""),
            }
        )
    return result


def _optional_redacted(value: Any, client: GitLabClient) -> str | None:
    return None if value is None else client.redact(value)
