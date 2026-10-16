"""Merged GitHub PRs written through the existing frozen-draft writer."""
from datetime import datetime, timezone
from pathlib import Path
import re
from patchbench import __version__
from patchbench.case import is_safe_path_component
from patchbench.exceptions import PatchbenchError
from patchbench.github_client import GitHubClient
from patchbench.ingest import _build_issue, _build_metadata, _generate_gold_patch, _write_draft


def ingest_github_pr(*, github_repo, pr_number, case_id, repo_path, output_path,
                     repository_id='rucio', client=None, candidate_features=None):
    if github_repo != 'rucio/rucio' or repository_id != 'rucio':
        raise PatchbenchError('Only rucio/rucio is configured for this pilot')
    if not is_safe_path_component(case_id):
        raise PatchbenchError('case ID must be a single non-empty path component')
    output = Path(output_path).expanduser().resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise PatchbenchError('Output must be absent or an empty directory')
    repository = Path(repo_path).expanduser().resolve()
    client = client or GitHubClient()
    pr = client.fetch_pull_request(github_repo, pr_number)
    if pr.get('state') != 'closed' or not pr.get('merged_at'):
        raise PatchbenchError('Only merged pull requests can be imported')
    base, fix = pr.get('patch_base_sha'), pr.get('historical_fix_sha', pr.get('merge_commit_sha'))
    if not all(isinstance(sha, str) and re.fullmatch('[0-9a-f]{40}', sha) for sha in (base, fix)):
        raise PatchbenchError('GitHub import requires exact base/fix commit SHAs')
    patch = _generate_gold_patch(repository, base, fix)
    acquired = {**pr, 'description': pr.get('body'), 'iid': pr_number,
        'labels': [item['name'] for item in pr.get('labels', [])]}
    issue = _build_issue(acquired, client)
    metadata = _build_metadata(case_id=case_id, repo_name='rucio', repo_path=repository,
        project_id=github_repo, mr_iid=pr_number, gitlab_url='', base_commit=base,
        human_commit=fix, repository_id=repository_id, output_path=output)
    metadata['source'] = dict(type='github', github_repo=github_repo, pull_request_number=pr_number)
    provenance = dict(source='github', github_repo=github_repo, pull_request_number=pr_number,
        web_url=pr.get('html_url'), pr_title=client.redact(pr.get('title') or ''),
        pr_author=client.redact(pr.get('user', {}).get('login', 'unknown')),
        merged_at=pr['merged_at'], created_at=pr.get('created_at'),
        base_commit=base, human_commit=fix, merge_commit_sha=pr['merge_commit_sha'],
        pr_base_sha=pr.get('base', {}).get('sha'), pr_head_sha=pr.get('head', {}).get('sha'),
        labels=acquired['labels'], related_issues=pr.get('related_issues', []),
        imported_at=datetime.now(timezone.utc).isoformat(), import_tool_version=__version__,
        notes=['Historical patch is the exact local merged first-parent diff or API-verified full original PR range for a rebase.'])
    safe = {key:acquired.get(key) for key in ('title','description','iid','labels','merged_at','related_issues')}
    safe['target_branch'] = pr.get('base', {}).get('ref')
    def redact(value):
        if isinstance(value, str):
            return client.redact(value)
        if isinstance(value, dict):
            return {key:redact(item) for key,item in value.items()}
        if isinstance(value, list):
            return [redact(item) for item in value]
        return value
    provenance, safe = redact(provenance), redact(safe)
    return _write_draft(output, issue, patch, metadata, provenance, 'rucio', case_id, safe, redact(candidate_features or {}))
