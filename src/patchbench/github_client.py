"""Read-only public GitHub acquisition for the Rucio pilot."""
from __future__ import annotations
import json
import os
import re
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from patchbench.exceptions import PatchbenchError


class GitHubClient:
    def __init__(self, token=None, opener=None):
        self.token = token or os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN')
        self.opener = opener or urlopen

    def redact(self, value):
        text = str(value)
        return text.replace(self.token, '[REDACTED]') if self.token else text

    def get(self, path):
        request = Request('https://api.github.com/' + path, headers={
            'Accept': 'application/vnd.github+json', 'User-Agent': 'PatchBench',
            'X-GitHub-Api-Version': '2022-11-28',
            **({'Authorization': 'Bearer ' + self.token} if self.token else {})})
        try:
            with self.opener(request, timeout=30) as response:
                return json.load(response)
        except (HTTPError, URLError, ValueError) as exc:
            raise PatchbenchError('GitHub read failed: ' + self.redact(str(exc))) from exc

    def fetch_pull_request(self, repository, number):
        if repository != 'rucio/rucio' or type(number) is not int or number <= 0:
            raise PatchbenchError('This pilot accepts only positive rucio/rucio PR numbers')
        pr = self.get(f'repos/{repository}/pulls/{number}')
        if not isinstance(pr, dict) or pr.get('number') != number or not pr.get('merged_at') or pr.get('state') != 'closed':
            raise PatchbenchError('Only merged pull requests can be imported')
        if pr.get('base', {}).get('repo', {}).get('full_name') != repository:
            raise PatchbenchError('Pull request repository does not match requested repository')
        commit = self.get(f"repos/{repository}/commits/{pr['merge_commit_sha']}")
        parents = commit.get('parents', [])
        if not parents:
            raise PatchbenchError('Merged commit has no exact parent')
        # Squash and merge commits have a well-defined first-parent delta. Rebase
        # merges with multiple PR commits need another boundary; never guess it.
        if len(parents) == 1 and pr.get('commits', 0) > 1:
            files = []
            page = 1
            while True:
                batch = self.get(f'repos/{repository}/pulls/{number}/files?per_page=100&page={page}')
                files.extend(batch)
                if len(batch) < 100:
                    break
                page += 1
            merged_files = commit.get('files', [])
            def delta(items):
                # Squash cherry-picks may relocate hunks; retain exact content
                # and require every file/hunk, ignoring only line-number headers.
                return {f['filename']: '\n'.join(line for line in f.get('patch', '').splitlines() if not line.startswith('@@'))
                        if f.get('patch') is not None else None for f in items}
            if len(merged_files) >= 300 or any(f.get('patch') is None for f in files) or delta(files) != delta(merged_files):
                comparison = self.get(f"repos/{repository}/compare/{pr['base']['sha']}...{pr['head']['sha']}")
                exact_base = comparison.get('merge_base_commit', {}).get('sha')
                compared_files = comparison.get('files', [])
                if (not isinstance(exact_base, str) or not re.fullmatch('[0-9a-f]{40}', exact_base)
                        or exact_base == pr['head']['sha'] or delta(files) != delta(compared_files)):
                    raise PatchbenchError('Ambiguous rebase/squash boundary; no commits were guessed')
                # A rebase's merge_commit_sha names only the final rebased
                # commit. Retain the full original PR range, just like GitLab
                # diff_refs, with the separately recorded merged SHA.
                pr['historical_fix_sha'] = pr['head']['sha']
                pr['historical_base_sha'] = exact_base
        pr['patch_base_sha'] = pr.get('historical_base_sha', parents[0]['sha'])
        pr['related_issues'] = []
        text = (pr.get('title') or '') + '\n' + (pr.get('body') or '')
        references = set(re.findall(r'(?<![\w/])#(\d+)\b', text))
        references.update(re.findall(r'https://github\.com/rucio/rucio/issues/(\d+)\b', text))
        for issue_number in sorted(map(int, references)):
            if issue_number == number:
                continue
            issue = self.get(f'repos/{repository}/issues/{issue_number}')
            if 'pull_request' not in issue:
                pr['related_issues'].append(dict(iid=issue_number, title=issue.get('title'),
                    description=issue.get('body'), web_url=issue.get('html_url')))
        return pr

    def list_merged_pull_requests(self, repository, limit, labels=None):
        if repository != 'rucio/rucio' or type(limit) is not int or not 1 <= limit <= 1000:
            raise PatchbenchError('GitHub discovery supports rucio/rucio and limits 1–1000')
        query = f'repo:{repository} is:pr is:merged'
        for label in labels or []:
            query += ' label:' + json.dumps(label)
        rows = []
        for page in range(1, 11):
            parameters = urlencode(dict(q=query,sort='updated',order='desc',per_page=min(limit,100),page=page))
            result = self.get('search/issues?' + parameters)
            if result.get('incomplete_results'):
                raise PatchbenchError('GitHub returned incomplete discovery results')
            batch = result.get('items', [])
            rows.extend(batch)
            if len(rows) >= limit or len(batch) < min(limit,100):
                break
        return rows[:limit]
