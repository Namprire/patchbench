"""Allowlisted candidate facts; never open a patch, oracle, or source checkout."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from patchbench.catalog import read_metadata, safe_component
from patchbench.exceptions import PatchbenchError
from patchbench.runner import redact_secrets, sanitize_command


TEXT_FIELDS = ("mr_title", "mr_description", "problem_description", "merged_at", "target_branch")
LIST_FIELDS = ("labels", "linked_issues", "changed_file_paths", "heuristic_reasons")
COUNT_FIELDS = ("changed_files_total", "changed_files_code", "changed_files_tests", "changed_files_docs",
                "lines_added", "lines_removed", "linked_issue_count", "heuristic_score", "merge_request_iid")
BOOL_FIELDS = ("has_test_changes", "has_code_changes", "likely_bugfix", "heuristic_selected")


def sanitize_text(value: str) -> str:
    """Omit embedded code/diffs and redact credentials, including credentialed URLs."""
    text = redact_secrets(value)
    text = re.sub(r"(?ms)^\s*(`{3,}|~{3,})[^\n]*\n.*?(?:^\s*\1\s*$|\Z)", "[code omitted]", text)
    text = re.sub(r"(?is)<(pre|code|script)\b[^>]*>.*?(?:</\1>|\Z)", "[code omitted]", text)
    text = re.sub(r"`[^`\n]*`", "[inline code omitted]", text)
    # Pasted diffs also contain unchanged source context: omit the remainder.
    text = re.sub(r"(?ms)^\s*(?:diff --git|Index:|@@ |--- a/|\+\+\+ b/).*\Z", "[diff omitted]", text)
    text = re.sub(r"(?m)^(?: {4}|\t).*$", "[code omitted]", text)
    text = sanitize_command(text)
    text = re.sub(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+", "<redacted-auth>", text)
    text = re.sub(r"(?i)\b(?:private-token|authorization|api[-_ ]?key|password|secret|token)\s*[:=]\s*[^\s,;]+",
                  "<redacted-secret>", text)

    def url(match: re.Match[str]) -> str:
        try:
            parsed = urlsplit(match.group())
            # Text descriptions need no URL paths or query credentials.
            return f"{parsed.scheme}://{parsed.hostname}" if parsed.hostname else "<redacted-url>"
        except ValueError:
            return "<redacted-url>"

    return re.sub(r"https?://[^\s<>\"']+", url, text)[:8000]


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def input_digest(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(snapshot).encode()).hexdigest()


def normalize_snapshot(raw: dict[str, Any], repository: str, candidate_id: str) -> dict[str, Any]:
    """Reapply the allowlist even to previously saved candidate metadata."""
    result: dict[str, Any] = {"schema_version": 1, "repository": repository, "candidate_id": candidate_id}
    for key in TEXT_FIELDS:
        result[key] = sanitize_text(raw[key]) if isinstance(raw.get(key), str) else ""
    for key in LIST_FIELDS:
        values = raw.get(key)
        result[key] = [sanitize_text(item) for item in values[:500] if isinstance(item, str)] if isinstance(values, list) else []
    for key in COUNT_FIELDS:
        result[key] = raw.get(key) if type(raw.get(key)) is int else None
    for key in BOOL_FIELDS:
        result[key] = raw.get(key) if type(raw.get(key)) is bool else None
    return result


def snapshot_from_acquisition(
    repository: str, candidate_id: str, merge_request: dict[str, Any],
    problem_description: str, features: dict[str, Any],
) -> dict[str, Any]:
    """Use fetched MR/issue metadata and discovery facts without another API call."""
    related = merge_request.get("related_issues")
    linked = [f"{item.get('title', '')}\n{item.get('description', '')}" for item in related if isinstance(item, dict)] if isinstance(related, list) else []
    raw = {
        "mr_title": merge_request.get("title"), "mr_description": merge_request.get("description"),
        "problem_description": problem_description, "linked_issues": linked,
        "labels": merge_request.get("labels"),
        "merged_at": merge_request.get("merged_at"), "target_branch": merge_request.get("target_branch"),
        **{key: features.get(key) for key in (*COUNT_FIELDS, *BOOL_FIELDS)},
        "merge_request_iid": merge_request.get("iid", features.get("merge_request_iid")),
        "changed_file_paths": features.get("changed_file_paths"),
        "heuristic_score": features.get("candidate_score"),
        "heuristic_selected": features.get("selected"),
        "heuristic_reasons": [*(features.get("notes") or []), *([features["rejection_reason"]] if features.get("rejection_reason") else [])],
    }
    return normalize_snapshot(raw, repository, candidate_id)


def private_metadata_dir(draft: Path) -> Path:
    path = draft / ".patchbench"
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise PatchbenchError("Draft private metadata must be a local directory")
    if (path / "triage.json").is_symlink() or (path / "candidate.json").is_symlink():
        raise PatchbenchError("Draft private evidence must not be a symlink")
    return path


def build_candidate_snapshot(draft: Path, repository: str, discovery_features: dict[int, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Read imported safe facts, or fall back to the declared issue and provenance."""
    if draft.is_symlink() or (draft / "meta.yaml").is_symlink():
        raise PatchbenchError("Draft inputs must not be symlinks")
    metadata = read_metadata(draft)
    candidate_id = metadata.get("id")
    if not safe_component(candidate_id) or metadata.get("repository") != repository or metadata.get("draft") is not True:
        raise PatchbenchError("Draft identity, repository, or state does not match")
    if metadata.get("case_type", "coding") != "coding":
        raise PatchbenchError("Triage version 1 supports coding drafts")
    cached = private_metadata_dir(draft) / "candidate.json"
    for hidden in ("gold_patch", "test_patch", "oracle"):
        name = metadata.get(hidden)
        if isinstance(name, str) and cached.is_file() and (draft / name).is_file() and cached.samefile(draft / name):
            raise PatchbenchError("Candidate snapshot aliases evaluator-only material")
    if cached.is_file():
        raw = json.loads(cached.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or type(raw.get("schema_version")) is not int or raw.get("schema_version") != 1:
            raise PatchbenchError("Invalid candidate snapshot schema")
        if raw.get("repository") != repository or raw.get("candidate_id") != candidate_id:
            raise PatchbenchError("Candidate snapshot identity does not match draft")
        return normalize_snapshot(raw, repository, candidate_id)
    role = metadata.get("problem_statement")
    if not isinstance(role, str):
        raise PatchbenchError("Draft requires a declared problem statement")
    issue = draft / role
    if not issue.resolve().is_relative_to(draft.resolve()) or issue.is_symlink():
        raise PatchbenchError("Problem statement must stay inside the draft")
    for hidden in ("gold_patch", "test_patch", "oracle"):
        name = metadata.get(hidden)
        if isinstance(name, str):
            private = draft / name
            if issue.resolve() == private.resolve() or (issue.is_file() and private.is_file() and issue.samefile(private)):
                raise PatchbenchError("Problem statement aliases evaluator-only material")
    provenance_path = draft / "provenance.json"
    if provenance_path.is_symlink():
        raise PatchbenchError("Draft provenance must not be a symlink")
    provenance = json.loads(provenance_path.read_text(encoding="utf-8")) if provenance_path.is_file() else {}
    if not isinstance(provenance, dict):
        raise PatchbenchError("Draft provenance must be a mapping")
    mr = {"title": provenance.get("mr_title"), "labels": provenance.get("labels"),
          "iid": provenance.get("merge_request_iid"), "merged_at": provenance.get("merged_at"),
          "target_branch": provenance.get("target_branch")}
    iid = provenance.get("merge_request_iid")
    features = (discovery_features or {}).get(iid, {}) if type(iid) is int else {}
    return snapshot_from_acquisition(repository, candidate_id, mr, issue.read_text(encoding="utf-8"), features)
