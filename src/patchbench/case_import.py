"""Repeatable provider-aware intake into the existing draft and promotion workflow."""

from __future__ import annotations

import json
import shutil
import tempfile
import hashlib
import math
import os
import sys
import time
from datetime import datetime, timezone
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from patchbench.catalog import discover_cases, read_metadata, safe_component
from patchbench.commands import validate_case
from patchbench.discovery import discover_gitlab_cases, discover_github_cases
from patchbench.exceptions import PatchbenchError
from patchbench.gitlab_client import GitLabClient
from patchbench.gitutils import ensure_git_repo
from patchbench.ingest import ingest_gitlab_mr
from patchbench.io import write_json
from patchbench.repositories import RepositoryManifest, load_repository
from patchbench.runner import run_command


@dataclass(frozen=True)
class PullReport:
    repository: str
    candidates: int
    staged: tuple[str, ...]
    existing: tuple[str, ...]
    skipped: tuple[str, ...]
    failed: dict[str, str]
    candidates_path: str
    planned: tuple[str, ...] = ()
    dry_run: bool = False
    skip_reasons: dict[str, str] = field(default_factory=dict)
    construction_options: dict[str, tuple[str, ...]] = field(default_factory=dict)


def sync_repository(manifest: RepositoryManifest) -> Path:
    """Update an existing checkout or clone the configured Git remote once."""
    source = _repository_source(manifest)
    checkout = manifest.checkout()
    if checkout.is_dir():
        ensure_git_repo(checkout)
        result = run_command(["git", "fetch", "--prune", "origin"], cwd=checkout)
    else:
        if checkout.exists():
            raise PatchbenchError(f"Checkout path is not a directory: {checkout}")
        checkout.parent.mkdir(parents=True, exist_ok=True)
        result = run_command(["git", "clone", source["git_url"], str(checkout)], cwd=checkout.parent)
    if result.exit_code != 0:
        raise PatchbenchError(f"Could not synchronize {manifest.id}: {result.stderr.strip()}")
    return checkout


def pull_drafts(
    repository_id: str,
    *,
    root: str | Path,
    limit: int = 100,
    sync: bool = True,
    client: GitLabClient | None = None,
    labels: list[str] | None = None,
    dry_run: bool = False,
    candidate_numbers: list[int] | None = None,
) -> PullReport:
    """Stage merged coding PRs/MRs; construction and promotion stay explicit."""
    project = Path(root).expanduser().resolve()
    manifest = load_repository(repository_id, project / "cases")
    source = _repository_source(manifest)
    github_provider = source['type'] == 'github'
    if manifest.config["evaluator"] != "patch_tests":
        raise PatchbenchError("Merged-change intake currently supports coding cases")
    if limit <= 0:
        raise PatchbenchError("Pull limit must be positive")
    if candidate_numbers is not None:
        if not candidate_numbers or any(type(n) is not int or n <= 0 for n in candidate_numbers):
            raise PatchbenchError('Candidate numbers must be positive integers')
        candidate_numbers = list(dict.fromkeys(candidate_numbers))
    repository = sync_repository(manifest) if sync else manifest.checkout()
    ensure_git_repo(repository)
    if github_provider:
        from patchbench.github_client import GitHubClient
        from patchbench.github_ingest import ingest_github_pr
        gitlab = client or GitHubClient()
    else:
        gitlab = client or GitLabClient.from_environment(source["gitlab_url"])
    evidence = project / "artifacts" / "imports" / repository_id
    evidence.mkdir(parents=True, exist_ok=True)
    candidates_path = evidence / ("candidates-preview.json" if dry_run else "candidates.json")
    if github_provider:
        candidates = discover_github_cases(github_repo=source['github_repo'],repo=repository_id,
            repo_path=repository,output_path=candidates_path,limit=limit,client=gitlab,
            labels=labels,candidate_numbers=candidate_numbers)
    else:
        candidates = discover_gitlab_cases(
        project_id=source["project_id"],
        repo=repository_id,
        repo_path=repository,
        output_path=candidates_path,
        limit=limit,
        gitlab_url=source["gitlab_url"],
        client=gitlab,
        labels=labels,
        **({'candidate_numbers':candidate_numbers} if candidate_numbers else {}),
        )
    existing_sources = _existing_sources(project)
    staged: list[str] = []
    existing: list[str] = []
    skipped: list[str] = []
    failed: dict[str, str] = {}
    planned: list[str] = []
    skip_reasons: dict[str, str] = {}
    construction_options = {}
    drafts = project / "drafts" / repository_id
    if not dry_run:
        drafts.mkdir(parents=True, exist_ok=True)
    active = discover_cases(project / "cases")
    for candidate in candidates:
        iid = candidate['pull_request_number' if github_provider else 'merge_request_iid']
        case_id = f"{repository_id}-{iid}"
        key = (_source_key('https://github.com',source['github_repo'],iid) if github_provider
               else _source_key(source['gitlab_url'],source['project_id'],iid))
        if key in existing_sources:
            existing.append(case_id)
            continue
        if not candidate["has_code_changes"] or not candidate["base_commit"] or not candidate["human_commit"]:
            skipped.append(case_id)
            skip_reasons[case_id] = candidate["rejection_reason"] or "Missing code changes or exact diff refs"
            continue
        if candidate["rejection_reason"] and "local Git comparison unavailable" in candidate["rejection_reason"]:
            skipped.append(case_id)
            skip_reasons[case_id] = candidate["rejection_reason"]
            continue
        if not safe_component(case_id) or case_id in active or (drafts / case_id).exists():
            failed[case_id] = "Case ID collides with another source"
            continue
        if dry_run:
            planned.append(case_id)
            construction_options[case_id] = _construction_options(manifest,candidate)
            continue
        try:
            if github_provider:
                ingest_github_pr(github_repo=source['github_repo'],pr_number=iid,case_id=case_id,
                    repo_path=repository,output_path=drafts/case_id,client=gitlab,
                    candidate_features=candidate)
            else:
                ingest_gitlab_mr(
                project_id=source["project_id"],
                mr_iid=iid,
                case_id=case_id,
                repo_path=repository,
                output_path=drafts / case_id,
                gitlab_url=source["gitlab_url"],
                client=gitlab,
                repository_id=repository_id,
                candidate_features=candidate,
                )
        except PatchbenchError as exc:
            failed[case_id] = gitlab.redact(str(exc))
            continue
        existing_sources.add(key)
        staged.append(case_id)
        construction_options[case_id] = _construction_options(manifest,candidate)
    report = PullReport(
        repository=repository_id,
        candidates=len(candidates),
        staged=tuple(staged), existing=tuple(existing), skipped=tuple(skipped),
        failed=failed, candidates_path=str(candidates_path),
        planned=tuple(planned), dry_run=dry_run, skip_reasons=skip_reasons,
        construction_options=construction_options,
    )
    write_json(evidence / ("last-pull-preview.json" if dry_run else "last-pull.json"), asdict(report))
    return report


def pull_gitlab_drafts(repository_id, **kwargs):
    """Compatibility entry point retained for existing callers."""
    return pull_drafts(repository_id,**kwargs)


def _construction_options(manifest, candidate):
    options = []
    if manifest.config.get('historical_pr_tests') and candidate.get('has_test_changes'):
        options.append('prepare-historical-pr-tests')
    if manifest.config.get('benchmark_components'):
        options.extend(['mirror-draft','prepare-mirror-oracle'])
    else:
        options.append('prepare-draft')
    return tuple([*options,'validate-case'])


def _repository_source(manifest):
    source = manifest.config.get('source',{})
    if not isinstance(source,dict) or not source.get('git_url'):
        raise PatchbenchError('Repository requires source.git_url')
    provider = source.get('type')
    if not provider:
        provider = ('github' if source.get('github_repo') or 'github.com' in source['git_url']
                    else 'gitlab' if source.get('gitlab_url') or 'gitlab' in source['git_url'] else None)
    if provider == 'gitlab':
        return {**_gitlab_source(manifest),'type':'gitlab'}
    if provider == 'github':
        repository = source.get('github_repo')
        if not repository:
            url = source['git_url']
            repository = (url.split('github.com:',1)[1] if 'github.com:' in url else urlparse(url).path.lstrip('/')).removesuffix('.git')
        if manifest.id != 'rucio' or repository != 'rucio/rucio':
            raise PatchbenchError('Only rucio/rucio GitHub intake is configured')
        return {**source,'type':'github','github_repo':repository}
    raise PatchbenchError('Repository source provider must be GitLab or GitHub')


def promote_draft_case(
    repository_id: str,
    case_id: str,
    *,
    root: str | Path,
    container: bool = False,
) -> Path:
    """Publish a reviewed draft only after the normal fail-before/pass-after gate."""
    if not safe_component(case_id):
        raise PatchbenchError("Case ID must be one safe path component")
    project = Path(root).expanduser().resolve()
    manifest = load_repository(repository_id, project / "cases")
    if manifest.config["evaluator"] != "patch_tests":
        raise PatchbenchError("Draft promotion currently supports coding cases")
    draft = project / "drafts" / repository_id / case_id
    metadata = read_metadata(draft)
    if metadata.get("id") != case_id or metadata.get("repository") != repository_id or metadata.get("draft") is not True:
        raise PatchbenchError("Draft identity, repository, or state does not match")
    active = project / "cases"
    active.mkdir(parents=True, exist_ok=True)
    if case_id in discover_cases(active) or (active / case_id).exists():
        raise PatchbenchError(f"Case already exists: {case_id}")
    result = validate_case(draft, container=container)
    if not result.validated:
        raise PatchbenchError(f"Draft validation failed: {result.error or 'fail-before/pass-after check did not pass'}")
    with tempfile.TemporaryDirectory(prefix=".patchbench-promote-", dir=project) as raw:
        prepared = Path(raw) / case_id
        shutil.copytree(draft, prepared, symlinks=False, ignore=shutil.ignore_patterns(".patchbench"))
        published_meta = {**metadata, "draft": False}
        published_meta.pop("draft_todos", None)
        if metadata.get('construction_mode') == 'historical_pr_tests_v1':
            from patchbench.historical_pr_tests import cache_identity, resolve_dependency_cache
            from patchbench.gitutils import temporary_checkout
            settings = dict(metadata['historical_pr_tests'])
            evidence = json.loads((draft/'.patchbench/historical-pr-tests/latest.json').read_text())
            baseline = {k:evidence[k] for k in ('f2p_tests', 'p2p_tests')}
            baseline['reference_tests'] = evidence['reference']['report']['tests']
            baseline['source_evidence_sha256'] = hashlib.sha256((draft/'.patchbench/historical-pr-tests/latest.json').read_bytes()).hexdigest()
            write_json(prepared/'historical-baseline.json', baseline)
            settings.update(baseline_record='historical-baseline.json',
                baseline_sha256=hashlib.sha256((prepared/'historical-baseline.json').read_bytes()).hexdigest())
            cache = resolve_dependency_cache(draft, settings)
            if cache:
                java = settings.get('selection', {}).get('java_version', 'unknown')
                relative = f'artifacts/dependency-caches/{repository_id}-java{java}'
                shared = project / relative
                with temporary_checkout(manifest.checkout(), metadata['fix_commit']) as reference:
                    if shared.exists():
                        if cache_identity(shared, reference) != settings['dependency_cache_digest']:
                            relative += '-' + settings['dependency_cache_digest'][:12]
                            shared = project / relative
                    if not shared.exists():
                        shared.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copytree(cache, shared)
                    if cache_identity(shared, reference) != settings['dependency_cache_digest']:
                        raise PatchbenchError('Published dependency cache differs from validated cache')
                settings['dependency_cache'] = f'$PATCHBENCH_{repository_id.upper()}_CACHE_JAVA{java}'
                settings['dependency_cache_default'] = relative
                published_meta['dependency_cache'] = settings['dependency_cache']
            published_meta['historical_pr_tests'] = settings
            published_meta.setdefault('artifacts', {})['historical_baseline'] = 'historical-baseline.json'
        if not (prepared / 'construction-provenance.json').exists():
            write_json(prepared / 'construction-provenance.json', _construction_provenance(draft, metadata))
        published_meta.setdefault('artifacts', {})['construction_provenance'] = 'construction-provenance.json'
        (prepared / "meta.yaml").write_text(yaml.safe_dump(published_meta, sort_keys=False), encoding="utf-8")
        prepared.rename(active / case_id)
    return active / case_id


def _gitlab_source(manifest: RepositoryManifest) -> dict[str, str]:
    source = manifest.config.get("source")
    if not isinstance(source, dict) or not all(
        isinstance(source.get(key), str) and source[key].strip()
        for key in ("gitlab_url", "project_id", "git_url")
    ):
        raise PatchbenchError(f"Repository {manifest.id} requires source.gitlab_url, project_id, and git_url")
    return source


def _source_key(url: object, project_id: object, iid: object) -> tuple[str, str, str]:
    return (str(url).rstrip("/").lower(), str(project_id), str(iid))


def _existing_sources(project: Path) -> set[tuple[str, str, str]]:
    sources: set[tuple[str, str, str]] = set()
    paths = [*(project / "cases").glob("*/provenance.json"), *(project / "drafts").glob("*/*/provenance.json")]
    for path in paths:
        try:
            raw: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PatchbenchError(f"Cannot index case provenance: {path}") from exc
        if isinstance(raw, dict) and raw.get("source") == "gitlab" and all(
            raw.get(key) is not None for key in ("gitlab_url", "project_id", "merge_request_iid")
        ):
            sources.add(_source_key(raw["gitlab_url"], raw["project_id"], raw["merge_request_iid"]))
        elif isinstance(raw,dict) and raw.get('source') == 'github' and raw.get('github_repo') and raw.get('pull_request_number'):
            sources.add(_source_key('https://github.com',raw['github_repo'],raw['pull_request_number']))
    return sources


def _existing_gitlab_sources(project):
    return {key for key in _existing_sources(project) if key[0] != 'https://github.com'}


def construction_fingerprint(draft: Path) -> str:
    """Hash immutable construction inputs, excluding evidence and execution outputs."""
    digest = hashlib.sha256()
    for path in sorted(draft.rglob('*')):
        if any(p in {'logs', 'runs', '.patchbench'} for p in path.relative_to(draft).parts):
            continue
        if path.is_symlink():
            raise PatchbenchError('Construction inputs must not contain symlinks')
        if path.is_file():
            digest.update(path.relative_to(draft).as_posix().encode() + b'\0')
            digest.update(path.read_bytes())
    return digest.hexdigest()


def review_construction(draft: str | Path, *, reviewer: str, reason: str) -> dict:
    """Record a researcher's grounded review without allowing it to waive validation."""
    draft = Path(draft).resolve()
    if not reviewer.strip() or not reason.strip():
        raise PatchbenchError('Construction review requires reviewer and behavioral rationale')
    record = dict(reviewer=reviewer, reason=reason, reviewed_at=datetime.now(timezone.utc).isoformat(),
                  input_sha256=construction_fingerprint(draft), approved=True)
    from patchbench.runner import redact_secrets
    record['reason'] = redact_secrets(reason)
    private = draft / '.patchbench'
    private.mkdir(exist_ok=True)
    write_json(private / 'construction-review.json', record)
    return record


def _construction_provenance(draft: Path, metadata: dict) -> dict:
    """Portable summary of existing evidence; raw model prompts stay private."""
    from patchbench.runner import redact_secrets
    private = draft / '.patchbench'
    records = []
    paths = [private / 'mirror.json', private / 'testgen.json', private / 'historical-pr-tests/latest.json']
    paths.extend(sorted(private.glob('targeted-regression-*/testgen.json')))
    allowed = {'profile', 'profile_digest', 'harness', 'requested_model', 'actual_model',
               'prompt_version', 'prompt_digest', 'input_tokens', 'output_tokens',
               'cache_read_tokens', 'reported_cost_usd', 'runtime_seconds', 'agent_num_turns'}
    def usage(value):
        found = []
        if isinstance(value, dict):
            row = {k:v for k,v in value.items() if k in allowed and isinstance(v, (str, int, float, type(None)))}
            if any(k in row for k in ('requested_model', 'input_tokens', 'reported_cost_usd')):
                found.append(row)
            for k,v in value.items():
                if k in ('model_usage', 'attempts', 'details', 'review', 'agent'):
                    found.extend(usage(v))
        elif isinstance(value, list):
            for v in value:
                found.extend(usage(v))
        return found
    models = []
    review_path = private / 'construction-review.json'
    review = json.loads(review_path.read_text()) if review_path.is_file() else None
    for path in paths:
        if path.is_file() and not path.is_symlink():
            data = json.loads(path.read_text())
            models.extend(usage(data))
            records.append(dict(path=path.relative_to(draft).as_posix(),
                                sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    mode = metadata.get('construction_mode', 'injected_test_patch')
    benchmark = metadata.get('benchmark', {})
    oracle = ('developer_tests' if mode == 'historical_pr_tests_v1' else 'generated_regression_test'
              if metadata.get('validation_policy') == 'strict_f2p_v1' or benchmark.get('oracle_patch')
              else 'existing_test' if mode == 'mr_mirror_v1' else 'curated_test')
    return json.loads(redact_secrets(json.dumps(dict(schema_version=1, case_id=metadata['id'],
        construction_mode=mode, construction_method=benchmark.get('mirror_method', 'historical' if mode == 'historical_pr_tests_v1' else 'testgen'),
        oracle_source=oracle, reference_sha=benchmark.get('reference_sha', metadata.get('fix_commit')),
        historical=metadata.get('historical') or {k:metadata.get(k) for k in ('base_commit','fix_commit')},
        model_usage=models, evidence=records, construction_review=review,
        artifact_sha256={str(p.relative_to(draft)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in draft.iterdir() if p.is_file() and not p.is_symlink()}))))


def _harvest_candidate(draft: Path, manifest: RepositoryManifest) -> dict:
    """Cheap Git facts and supported strategies, never a semantic eligibility verdict."""
    from patchbench.historical_pr_tests import developer_test, historical_patches
    meta = read_metadata(draft)
    source = manifest.checkout()
    history = meta.get('historical', {})
    base = history.get('original_base_sha', meta['base_commit'])
    fix = history.get('original_fix_sha', meta['fix_commit'])
    result = run_command(['git', 'diff', '--name-only', '--no-renames', base, fix, '--'], cwd=source)
    if result.exit_code:
        return dict(id=meta['id'], repository=manifest.id, source=str(draft), strategies=[],
                    deferred='historical_objects_unavailable')
    paths = result.stdout.splitlines()
    policy = manifest.config.get('historical_pr_tests', {})
    tests = [p for p in paths if developer_test(p, policy)]
    strategies = []
    if policy and tests:
        try:
            historical_patches(source, base, fix, policy)
            strategies.append('historical')
        except PatchbenchError:
            pass
    if manifest.config.get('benchmark_components'):
        strategies.extend(['reverse', 'semantic', 'oracle'])
    elif manifest.id == 'cfmgr':
        strategies.append('testgen')
    retained_validation = draft/'.patchbench/historical-pr-tests/latest.json'
    mirror_validation = draft/'.patchbench/mirror.json'
    prepared_green = any(p.is_file() and (json.loads(p.read_text()).get('validated') is True or
        (json.loads(p.read_text()).get('verification') or {}).get('validated') is True)
        for p in (retained_validation, mirror_validation))
    if meta.get('construction_mode') and prepared_green:
        strategies.insert(0, 'validate')
    component = next((p.split('/src/')[0] for p in tests if '/src/' in p),
                     next((p.split('/')[0] for p in paths), 'unknown'))
    runtime_version = None
    if policy.get('framework') == 'maven':
        import xml.etree.ElementTree as ET
        pom = run_command(['git','show',base+':pom.xml'],cwd=source)
        if not pom.exit_code:
            try:
                document = ET.fromstring(pom.stdout)
                for key in ('maven.compiler.release','java.version','maven.compiler.target'):
                    value = document.findtext(f'.//{{*}}{key}')
                    if value and value.removeprefix('1.').isdigit():
                        runtime_version = value.removeprefix('1.');break
            except ET.ParseError:
                pass
    identity = hashlib.sha256(json.dumps(dict(base=base, fix=fix,
        source_inputs=construction_fingerprint(draft), manifest=manifest.config), sort_keys=True).encode()).hexdigest()
    return dict(id=meta['id'], repository=manifest.id, source=str(draft), base=base, fix=fix,
                paths=paths, developer_tests=tests, component=component, strategies=strategies,
                runtime_version=runtime_version, fingerprint=identity, deferred=None)


def rank_harvest_candidates(candidates: list[dict], attempts: list[dict], *, remaining_seconds: float,
                            remaining_usd: float, model_profiles: dict, priced_models: dict) -> list[dict]:
    """Rank feasible next operations using observed yield, time and priced reservations."""
    queue = []
    for candidate in candidates:
        for index, strategy in enumerate(candidate['strategies']):
            fingerprint = candidate.get('strategy_fingerprints',{}).get(strategy,candidate.get('fingerprint'))
            key = f"{candidate['repository']}/{candidate['id']}/{strategy}/{fingerprint}"
            prior = [a for a in attempts if a.get('key') == key]
            if prior:
                continue
            # Strategies are conditional, not an automatic A/B/C/D conveyor belt.
            previous = [a for a in attempts if a.get('case_id') == candidate['id'] and
                        a.get('repository') == candidate['repository'] and a.get('source_fingerprint', a.get('fingerprint')) == candidate.get('fingerprint')]
            if any(a.get('validated') for a in previous):
                continue
            if index and any(s in candidate['strategies'][:index] and not any(
                    a.get('strategy') == s for a in previous) for s in candidate['strategies'][:index]
                    if s not in ('semantic', 'oracle', 'testgen')):
                continue
            if strategy == 'semantic' and not any(a.get('strategy') == 'reverse' and a.get('reason') in
                    ('reverse_patch_not_applicable','compilation_failure','mirror_agent_failed') for a in previous):
                continue
            if strategy == 'oracle' and not any(a.get('reason') in ('no_f2p_tests','relevant_tests_not_executed') and a.get('work_root') for a in previous):
                continue
            paid = strategy in ('semantic','oracle','testgen')
            profile = model_profiles.get(strategy)
            rate = priced_models.get(profile['model']) if profile else None
            if paid and (not profile or not rate or rate.get('provider_enforces_budget') is not True):
                continue
            reserve = float(rate['max_call_usd']) * (2 if strategy == 'oracle' else 1) if paid else 0.0
            if reserve > remaining_usd:
                continue
            category = f"{candidate['repository']}/{candidate['component']}/{strategy}"
            if candidate.get('runtime_version') and strategy in ('historical','validate'):
                category += '/java' + candidate['runtime_version']
            observations = [a for a in attempts if a.get('category') == category and a.get('executed')]
            successes = sum(bool(a.get('validated')) for a in observations)
            probability = (1 + successes) / (4 + len(observations))
            if candidate['developer_tests']:
                probability *= 1.8 if strategy == 'historical' else 1.2
            probability = min(.95, probability)
            probability /= 1 + max(0, len(candidate['paths']) - 10) / 30
            default = 240 if strategy in ('historical','reverse','validate') and candidate['repository'] == 'monolandb' else 40
            if paid:
                default = 600
            seconds = sum(a['elapsed_seconds'] for a in observations) / len(observations) if observations else default
            completion_floor = default if strategy in ('historical','validate') else 0
            if max(seconds, completion_floor) > remaining_seconds:
                continue
            conversion = remaining_seconds / remaining_usd if remaining_usd else 0
            queue.append(dict(**{**candidate,'fingerprint':fingerprint}, source_fingerprint=candidate.get('fingerprint'), strategy=strategy, key=key, category=category,
                expected_seconds=seconds, reserved_usd=reserve, observations=len(observations),
                oracle_source=next((a['work_root'] for a in reversed(previous) if a.get('reason') == 'no_f2p_tests' and a.get('work_root')), None),
                score=probability / max(1, seconds + conversion * reserve)))
    return sorted(queue, key=lambda q:(-q['score'], q['id'], q['strategy']))


def harvest_drafts(repositories, *, root, session_id, resume=False, dry_run=False,
                   time_budget_seconds=7200, model_budget_usd=10.0, profile=None,
                   dependency_cache=None, model_profiles=None, model_pricing=None,
                   agent_env_file=None, agent_ca_file=None, promote=False,
                   executor=None, clock=time.monotonic, candidate_dirs=None,
                   discover=False, discovery_limit=20, client_factory=None, sync=False,
                   progress=None):
    """Opt-in greedy coordination of existing construction commands, with durable evidence.

    Acquisition is handled by pull_drafts. This function never builds images, relaxes
    validation, or sends a model an evaluator workspace. Each strategy has an isolated
    attempt root so rejected preparation cannot corrupt the next strategy's inputs.
    """
    import fcntl
    from patchbench.curation.profiles import load_agent_profile
    project = Path(root).resolve()
    repositories = [repositories] if isinstance(repositories, str) else list(repositories)
    if not repositories or not all(safe_component(v) for v in [session_id,*repositories]):
        raise PatchbenchError('Harvest requires safe session and repository identifiers')
    if time_budget_seconds <= 0 or not math.isfinite(model_budget_usd) or model_budget_usd < 0:
        raise PatchbenchError('Harvest budgets must be positive time and nonnegative finite cost')
    profiles = {}
    for role,path in (model_profiles or {}).items():
        loaded = load_agent_profile(path)
        expected = 'mirror' if role == 'semantic' else 'testgen'
        if expected not in loaded.roles:
            raise PatchbenchError('Harvest profile does not support its selected role')
        profiles[role] = dict(path=str(Path(path).resolve()), model=loaded.model)
    prices = json.loads(Path(model_pricing).read_text()) if model_pricing else {}
    if not isinstance(prices,dict):
        raise PatchbenchError('Model pricing must map model names to verified rates and ceilings')
    for model,price in prices.items():
        if not isinstance(price,dict) or any(type(price.get(k)) not in (int,float) or not math.isfinite(price[k])
                or price[k] < 0 for k in ('input_usd_per_million','output_usd_per_million','cache_read_usd_per_million')):
            raise PatchbenchError('Model pricing requires finite input, output and cache-read token rates')
        if type(price.get('max_call_usd')) not in (int,float) or not math.isfinite(price['max_call_usd']) or price['max_call_usd'] <= 0:
            raise PatchbenchError('Model pricing requires a positive per-call ceiling')
    manifests = {r:load_repository(r, project/'cases') for r in repositories}
    directories = candidate_dirs if candidate_dirs is not None else [p.parent for r in repositories
        for p in sorted((project/'drafts'/r).glob('*/meta.yaml'))]
    active = discover_cases(project/'cases')
    candidates = []
    seen = set()
    for directory in directories:
        directory = Path(directory).resolve()
        meta = read_metadata(directory)
        r = meta.get('repository')
        if r not in manifests or meta.get('draft') is not True or meta['id'] in active or (r,meta['id']) in seen:
            continue
        seen.add((r,meta['id']))
        candidates.append(_harvest_candidate(directory, manifests[r]))
    policy_files = [Path(__file__), Path(__file__).with_name('historical_pr_tests.py'),
        Path(__file__).with_name('preparation')/'environment.py',
        *sorted(Path(__file__).with_name('mirroring').glob('*.py'))]
    policy_digest = hashlib.sha256(b''.join(p.read_bytes() for p in policy_files)).hexdigest()
    from patchbench.historical_pr_tests import image_identity
    cache_paths = dependency_cache.values() if isinstance(dependency_cache,dict) else [dependency_cache]
    cache_fingerprints = {}
    for value in cache_paths:
        if value:
            path = Path(value).expanduser().resolve()
            digest = hashlib.sha256()
            if path.exists():
                for item in sorted(path.rglob('*')):
                    if item.is_file() and not item.is_symlink():
                        digest.update(item.relative_to(path).as_posix().encode()+b'\0')
                        digest.update(hashlib.sha256(item.read_bytes()).digest())
            cache_fingerprints[str(path)] = digest.hexdigest()
    image_fingerprints = {name:image_identity(name) for manifest in manifests.values() for name in
        set([manifest.config['image'], *[v['image'] for v in manifest.config.get('environment_profiles',{}).values() if v.get('image')],
            *[v['image'] for v in manifest.config.get('preparation_runtimes',{}).values() if v.get('image')]])}
    def bind_fingerprints(candidate):
        candidate['strategy_fingerprints'] = {strategy:hashlib.sha256(json.dumps(dict(
            inputs=candidate.get('fingerprint'), implementation=policy_digest,
            environment_profile=profile, dependency_cache=cache_fingerprints, images=image_fingerprints,
            agent_profile=(yaml.safe_load(Path(profiles[strategy]['path']).read_text()) if strategy in profiles else None)),
            sort_keys=True).encode()).hexdigest() for strategy in candidate['strategies']}
    for candidate in candidates:
        bind_fingerprints(candidate)
    destination = project/'artifacts'/'harvest'/session_id
    checkpoint = destination/'checkpoint.json'
    if dry_run:
        return dict(session_id=session_id, dry_run=True, candidates=candidates,
            ranked=rank_harvest_candidates(candidates, [], remaining_seconds=time_budget_seconds,
                remaining_usd=model_budget_usd, model_profiles=profiles, priced_models=prices),
            model_enabled=bool(prices))
    destination.mkdir(parents=True, exist_ok=True)
    with (destination/'session.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PatchbenchError('This harvest session is already running') from exc
        if checkpoint.exists() and not resume:
            raise PatchbenchError('Harvest session already exists; use --resume')
        if resume and not checkpoint.exists():
            raise PatchbenchError('Cannot resume an unknown harvest session')
        record = json.loads(checkpoint.read_text()) if checkpoint.exists() else dict(
            session_id=session_id, repositories=repositories, status='running', attempts=[],
            elapsed_seconds=0.0, cost_usd=0.0, model_cost_unknown=False, promotions=[],
            budget_spend_usd=0.0,
            time_budget_seconds=time_budget_seconds, model_budget_usd=model_budget_usd,
            discovery_pages={r:1 for r in repositories}, discovery_exhausted=[], discovery_seconds=0.0,
            discovery_events=[])
        if record['repositories'] != repositories:
            raise PatchbenchError('Resume must use the same repository list')
        record.setdefault('budget_spend_usd',sum(a.get('reserved_usd',0) for a in record['attempts'] if a.get('status') != 'running'))
        # Older checkpoints conflated temporary intake errors with an empty page.
        for repository in repositories:
            last = next((v for v in reversed(record.get('discovery_events', [])) if v.get('repository') == repository), {})
            if last.get('error'):
                record['discovery_exhausted'] = [r for r in record.get('discovery_exhausted', []) if r != repository]
        started = clock()
        previous_elapsed = record['elapsed_seconds']
        def save():
            record['elapsed_seconds'] = previous_elapsed + clock() - started
            write_json(checkpoint, record)
        # A killed attempt is incomplete, not a scientific rejection or free retry.
        for attempt in record['attempts']:
            if attempt.get('status') == 'running':
                attempt.update(status='interrupted', reason='interrupted_attempt', executed=False)
                lost_time = max(0,time.time()-attempt.get('started_at_epoch',time.time()))
                previous_elapsed += min(lost_time, record['time_budget_seconds'])
                attempt['elapsed_seconds'] = lost_time
                record['cost_usd'] += attempt.get('reserved_usd', 0)
                record['budget_spend_usd'] += attempt.get('reserved_usd', 0)
                if attempt.get('reserved_usd'):
                    record['model_cost_unknown'] = True
        save()
        discovery_failed_this_run = set()
        def acquire_next():
            if not discover or record.get('discovery_seconds',0) >= record['time_budget_seconds'] * .1:
                return False
            for repository in repositories:
                if repository in record.get('discovery_exhausted',[]) or repository in discovery_failed_this_run:
                    continue
                manifest = manifests[repository]
                source = _repository_source(manifest)
                if source['type'] != 'gitlab':
                    record.setdefault('discovery_exhausted',[]).append(repository)
                    continue
                acquisition_started = clock()
                try:
                    client = client_factory(repository) if client_factory else GitLabClient.from_environment(source['gitlab_url'])
                    page = record.setdefault('discovery_pages',{}).get(repository,1)
                    summaries = client.list_merged_merge_requests(source['project_id'], discovery_limit,
                        start_page=page, page_size=discovery_limit)
                    numbers = [s['iid'] for s in summaries]
                    if not numbers:
                        record.setdefault('discovery_exhausted',[]).append(repository); continue
                    report = pull_drafts(repository,root=project,limit=discovery_limit,
                        sync=sync and page==1,client=client,candidate_numbers=numbers)
                    record['discovery_pages'][repository] = page+1
                    record.setdefault('discovery_events',[]).append(dict(repository=repository,
                        page=page, listed=len(numbers), staged=list(report.staged), failed=report.failed,
                        skip_reasons=report.skip_reasons))
                    for identifier in report.staged:
                        draft = project/'drafts'/repository/identifier
                        item = _harvest_candidate(draft,manifest); bind_fingerprints(item)
                        candidates.append(item)
                    if len(numbers)<discovery_limit:
                        record.setdefault('discovery_exhausted',[]).append(repository)
                    return True
                except PatchbenchError as exc:
                    from patchbench.runner import redact_secrets
                    record.setdefault('discovery_events',[]).append(dict(repository=repository,error=redact_secrets(str(exc))))
                    discovery_failed_this_run.add(repository)
                finally:
                    record['discovery_seconds'] = record.get('discovery_seconds',0) + clock()-acquisition_started
                    save()
            return False
        try:
            while True:
                candidates = [c for c in candidates if not (project/'cases'/c['id']).exists()]
                remaining = record['time_budget_seconds'] - (previous_elapsed + clock() - started)
                if remaining <= 0:
                    record['status'] = 'time_budget_exhausted'; break
                feasible = rank_harvest_candidates(candidates, record['attempts'], remaining_seconds=remaining,
                    remaining_usd=max(0,record['model_budget_usd'] - record['budget_spend_usd']),
                    model_profiles={} if record['model_cost_unknown'] else profiles, priced_models=prices)
                if not feasible:
                    if acquire_next():
                        continue
                    record['status'] = 'no_feasible_next_strategy'; break
                # One in five attempts explores the least-observed feasible category.
                exploration_spent = sum(a['elapsed_seconds'] for a in record['attempts'] if a.get('exploration'))
                exploration = len(record['attempts']) % 5 == 4 and exploration_spent < record['time_budget_seconds']*.2
                selection = min(feasible, key=lambda q:(q['observations'], -q['score'])) if exploration else feasible[0]
                same = [a for a in record['attempts'] if a.get('category') == selection['category'] and a.get('executed')]
                if len(same) >= 3 and all(a.get('reason') == same[-1].get('reason') and not a.get('validated') for a in same[-3:]) and same[-1].get('reason') in ('baseline_not_green','external_dependency','historical_objects_unavailable'):
                    selection['reason'] = 'shared_capability_blocked'
                    record['attempts'].append({**selection, 'case_id':selection['id'], 'executed':False,
                        'elapsed_seconds':0, 'status':'deferred', 'validated':False}); save(); continue
                attempt = {k:selection[k] for k in ('key','category','strategy','repository','fingerprint','reserved_usd')}
                attempt.update(case_id=selection['id'], status='running', validated=False, executed=False,
                               source_fingerprint=selection.get('source_fingerprint'),
                               started_at_epoch=time.time(), exploration=exploration,
                               elapsed_seconds=0, retry_reason='new_strategy_or_changed_input_fingerprint')
                record['attempts'].append(attempt); save()
                if progress:
                    progress(f"{selection['id']}: {selection['strategy']} (estimated {selection['expected_seconds']:.0f}s)")
                attempt_started = clock()
                handler = executor or _execute_harvest_attempt
                try:
                    # Early rejects are useful scheduling observations, but must not
                    # shrink the time needed for a later five-state validation.
                    attempt_limit = 600 if selection['repository'] == 'monolandb' or selection['strategy'] in profiles else 300
                    outcome = handler(selection, destination, manifests[selection['repository']],
                        timeout_seconds=min(remaining, attempt_limit),
                        profile=profile, dependency_cache=dependency_cache, model_profiles=profiles, prices=prices,
                        agent_env_file=agent_env_file, agent_ca_file=agent_ca_file)
                except Exception as exc:
                    from patchbench.runner import redact_secrets
                    outcome = dict(validated=False, reason='construction_error', executed=False,
                                   error=redact_secrets(str(exc))[:2000])
                attempt.update(outcome, elapsed_seconds=clock()-attempt_started, status='validated' if outcome.get('validated') else 'rejected')
                cost = outcome.get('cost_usd', 0 if not selection['reserved_usd'] else None)
                if type(cost) not in (int,float) or not math.isfinite(cost) or cost < 0:
                    record['model_cost_unknown'] = True
                    # Preserve the reserved maximum; missing usage is never zero cost.
                    record['cost_usd'] += selection['reserved_usd']
                else:
                    record['cost_usd'] += cost
                # CLI/token costs can be estimates and omit cache-write billing.
                # Keep the enforceable upper bound committed for the entire session.
                record['budget_spend_usd'] += max(selection['reserved_usd'], cost if type(cost) in (int,float) and math.isfinite(cost) else 0)
                if attempt.get('validated'):
                    adopted = _adopt_harvest_case(project, Path(attempt['prepared_draft']), selection['repository'])
                    attempt['draft'] = str(adopted)
                    review = adopted/'.patchbench/construction-review.json'
                    approved = json.loads(review.read_text()) if review.exists() else {}
                    if promote and approved.get('approved') is True and approved.get('input_sha256') == construction_fingerprint(adopted):
                        promotion_remaining = record['time_budget_seconds'] - (previous_elapsed + clock() - started)
                        if promotion_remaining <= 0:
                            attempt['promotion_status'] = 'deferred_time_budget'
                        else:
                            publication = run_command([sys.executable,'-m','patchbench.cli','--no-progress',
                                'promote-draft',selection['repository'],selection['id'],'--root',str(project),'--container'],
                                cwd=project,timeout_seconds=promotion_remaining)
                            if publication.exit_code == 0:
                                record['promotions'].append(selection['id'])
                                attempt['promotion_status'] = 'promoted'
                            else:
                                attempt['promotion_status'] = 'promotion_timeout' if publication.timed_out else 'promotion_failed'
                                attempt['promotion_error'] = publication.stderr[-2000:]
                    elif promote:
                        attempt['promotion_status'] = 'behavioral_review_pending'
                save()
                if progress:
                    progress(f"{selection['id']}: {'VALIDATED' if attempt.get('validated') else attempt.get('reason')} ({attempt['elapsed_seconds']:.1f}s)")
                last_success = max((i for i,a in enumerate(record['attempts']) if a.get('validated')), default=-1)
                stalled_seconds = sum(a['elapsed_seconds'] for a in record['attempts'][last_success+1:])
                if stalled_seconds >= 1800:
                    record['status'] = 'marginal_yield_review'; break
        except KeyboardInterrupt:
            record['status'] = 'interrupted'
            raise
        finally:
            save()
        return record


def _adopt_harvest_case(project: Path, prepared: Path, repository: str) -> Path:
    meta = read_metadata(prepared)
    destination = project/'drafts'/repository/meta['id']
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Retain all previous attempts in artifacts; never overwrite an active case.
    with tempfile.TemporaryDirectory(prefix='.harvest-adopt-',dir=destination.parent) as temporary:
        staged = Path(temporary)/meta['id']
        shutil.copytree(prepared, staged)
        if destination.exists():
            archived = prepared.parent/('previous-draft-' + str(time.time_ns()))
            destination.rename(archived)
        try:
            staged.rename(destination)
        except BaseException:
            if 'archived' in locals() and archived.exists():
                archived.rename(destination)
            raise
    return destination


def _unchanged_regression_nodes(source, base, fix, nodes):
    selected = []
    for node in nodes:
        present = run_command(['git','ls-tree',base,'--',node],cwd=source)
        unchanged = run_command(['git','diff','--quiet',base,fix,'--',node],cwd=source)
        if present.stdout.strip() and unchanged.exit_code == 0:
            selected.append(node)
    return selected


def _execute_harvest_attempt(selection, destination, manifest, *, timeout_seconds, profile,
                             dependency_cache, model_profiles, prices, agent_env_file, agent_ca_file):
    """Run the established CLIs in a private catalog, preserving failed inputs."""
    from patchbench.runner import redact_secrets
    index = len(list((destination/'attempts').glob('*'))) if (destination/'attempts').exists() else 0
    work = destination/'attempts'/f'{index:04d}-{selection["repository"]}-{selection["id"]}-{selection["strategy"]}'
    (work/'repositories').mkdir(parents=True)
    (work/'cases').mkdir()
    raw = {**manifest.config, 'checkout':{**manifest.config['checkout'], 'default':str(manifest.checkout())}}
    (work/'repositories'/manifest.path.name).write_text(yaml.safe_dump(raw,sort_keys=False))
    draft = work/'drafts'/manifest.id/selection['id']
    draft.parent.mkdir(parents=True)
    source_draft = Path(selection['source'])
    if selection['strategy'] == 'oracle' and selection.get('oracle_source'):
        source_draft = Path(selection['oracle_source'])/'drafts'/manifest.id/selection['id']
    shutil.copytree(source_draft,draft)
    meta = read_metadata(draft)
    strategy = selection['strategy']
    if strategy not in ('validate', 'oracle') and (draft/'.patchbench').exists():
        shutil.rmtree(draft/'.patchbench')
    elif strategy == 'oracle':
        for previous_oracle in (draft/'.patchbench').glob('targeted-regression-*'):
            if previous_oracle.is_dir():
                shutil.rmtree(previous_oracle)
    if strategy not in ('validate','oracle') and meta.get('construction_mode'):
        # Restore original acquired history in this private attempt, never in the source draft.
        meta.update(base_commit=selection['base'],fix_commit=selection['fix'],gold_patch='gold.patch',
                    test_patch='test.patch',oracle_mode='injected_test_patch',setup_commands=[])
        for key in ('construction_mode','historical','historical_pr_tests','benchmark','validation_policy'):
            meta.pop(key,None)
        result = run_command(['git','diff','--binary','--full-index',selection['base'],selection['fix']],cwd=manifest.checkout())
        (draft/'gold.patch').write_text(result.stdout)
        (draft/'test.patch').write_text('# DRAFT: construct a regression oracle\n')
        (draft/'meta.yaml').write_text(yaml.safe_dump(meta,sort_keys=False))
    if strategy == 'testgen':
        from patchbench.historical_pr_tests import historical_patches
        try:
            production, _, _, _, _ = historical_patches(manifest.checkout(), selection['base'],selection['fix'],
                manifest.config.get('historical_pr_tests',{}), require_tests=False)
        except PatchbenchError:
            return dict(validated=False,reason='unsupported_historical_production_split',executed=False,work_root=str(work))
        (draft/'gold.patch').write_text(production)
        (draft/'test.patch').write_text('# DRAFT: independent Base-only oracle\n')
    effective_profiles = dict(model_profiles)
    if strategy in effective_profiles:
        spec = yaml.safe_load(Path(effective_profiles[strategy]['path']).read_text())
        for role in spec['roles'].values():
            role['timeout_seconds'] = min(role['timeout_seconds'],300)
            role['max_budget_usd'] = prices[spec['model']]['max_call_usd']
        bounded = work/'bounded-agent-profile.yaml'
        bounded.write_text(yaml.safe_dump(spec))
        effective_profiles[strategy] = {**effective_profiles[strategy], 'path':str(bounded)}
    reference = manifest.config.get('benchmark_maven_policy',{}).get('reference')
    if reference:
        for source in manifest.path.parent.parent.glob('benchmark-environments/' + manifest.id + '/*'):
            if source.is_dir():
                shutil.copytree(source,work/'benchmark-environments'/manifest.id/source.name)
    commands = []
    if strategy == 'historical':
        command = ['prepare-historical-pr-tests',str(draft)]
        selected_cache = dependency_cache
        if isinstance(dependency_cache,dict):
            from patchbench.preparation.environment import discover_environment
            from patchbench.case import load_case
            from patchbench.gitutils import temporary_checkout
            with temporary_checkout(manifest.checkout(),selection['base']) as base:
                java = discover_environment(base,manifest,load_case(draft))['java_version']
                selected_cache = dependency_cache.get(java)
        if selected_cache and manifest.config.get('historical_pr_tests',{}).get('framework') == 'maven':
            command += ['--dependency-cache',str(selected_cache)]
        selected_profile = profile
        if selected_profile is None:
            for name, settings in manifest.config.get('environment_profiles',{}).items():
                if settings.get('component') and settings['component'] != selection['component']:
                    continue
                selectors = settings.get('source_contains',{})
                if not selectors and not settings.get('component'):
                    continue
                matches = [run_command(['git','show',selection['base']+':'+path],cwd=manifest.checkout()) for path in selectors]
                if all(not value.exit_code and needle in value.stdout for value,needle in zip(matches,selectors.values())):
                    selected_profile = name; break
        if selected_profile:
            command += ['--profile',selected_profile]
            # The environment's local regression pool is fixed before seeing
            # Base results; use only developer files unchanged across the MR.
            pool = manifest.config.get('environment_profiles',{}).get(selected_profile,{}).get('harvest_regression_files',[])
            for node in _unchanged_regression_nodes(manifest.checkout(),selection['base'],selection['fix'],pool):
                command += ['--regression-node',node]
        commands.append(command)
    elif strategy in ('reverse','semantic','oracle'):
        command = ['mirror-draft',manifest.id,selection['id'],'--root',str(work),'--max-attempts','1','--json']
        if strategy == 'oracle':
            # Recover the actual executed NO_F2P mirror from the source's private evidence.
            command = ['prepare-mirror-oracle',manifest.id,selection['id'],'--root',str(work),'--max-attempts','1','--container','--json']
        if strategy != 'reverse':
            command += ['--agent-profile',effective_profiles[strategy]['path']]
        if agent_env_file and strategy != 'reverse': command += ['--agent-env-file',str(agent_env_file)]
        if agent_ca_file and strategy != 'reverse': command += ['--agent-ca-file',str(agent_ca_file)]
        commands.append(command)
    elif strategy == 'testgen':
        command = ['prepare-draft',manifest.id,selection['id'],'--root',str(work),'--max-attempts','1','--container','--json',
                   '--agent-profile',effective_profiles[strategy]['path']]
        if agent_env_file: command += ['--agent-env-file',str(agent_env_file)]
        if agent_ca_file: command += ['--agent-ca-file',str(agent_ca_file)]
        commands.append(command)
    commands.append(['validate-case',str(draft),'--container'])
    started = time.monotonic()
    def outcome(**values):
        if selection['reserved_usd']:
            usages = []
            mirror = draft/'.patchbench/mirror.json'
            if mirror.exists(): usages.extend(json.loads(mirror.read_text()).get('model_usage', []))
            for path in (draft/'.patchbench').glob('targeted-regression-*/attempt-*/response.json'):
                usages.append(json.loads(path.read_text()).get('details',{}))
                review = path.parent/'review.json'
                if review.exists(): usages.append(json.loads(review.read_text()).get('details',{}))
            if strategy == 'testgen':
                path = draft/'.patchbench/testgen.json'
                if path.exists():
                    usages.extend(a.get('agent',{}) for a in json.loads(path.read_text()).get('attempts',[]))
            rates = prices[model_profiles[strategy]['model']]
            cost = 0.0
            for usage in usages:
                if any(type(usage.get(k)) not in (int,float) for k in ('input_tokens','output_tokens','cache_read_tokens')):
                    cost = None; break
                # The provider ceiling remains reserved unless complete accounting is observed.
                if usage.get('reported_cost_usd') is None:
                    cost = None; break
                cost += max(float(usage['reported_cost_usd']), sum(usage[k]*rates[r]/1e6 for k,r in
                    [('input_tokens','input_usd_per_million'),('output_tokens','output_usd_per_million'),('cache_read_tokens','cache_read_usd_per_million')]))
            values.update(cost_usd=cost if usages else None, model_usage=usages)
        return dict(work_root=str(work), **values)
    for index,command in enumerate(commands):
        remaining = timeout_seconds - (time.monotonic()-started)
        if remaining <= 0:
            return outcome(validated=False, reason='construction_timeout',executed=True)
        result = run_command([sys.executable,'-m','patchbench.cli','--no-progress',*command],cwd=manifest.path.parent.parent,
                             timeout_seconds=remaining)
        (work/f'command-{index}.log').write_text(redact_secrets(result.stdout+'\n'+result.stderr))
        if result.exit_code:
            reason = 'construction_timeout' if result.timed_out else 'construction_failed'
            for record in (draft/'.patchbench/mirror.json',draft/'.patchbench/historical-pr-tests/latest.json',draft/'.patchbench/testgen.json'):
                if record.is_file():
                    evidence=json.loads(record.read_text()); reason=evidence.get('reason') or evidence.get('failure_category') or reason
            if result.timed_out:
                reason = 'construction_timeout'
            return outcome(validated=False,reason=reason,executed=True)
    from patchbench.catalog import validation_path
    record = json.loads(validation_path(draft).read_text())
    return outcome(validated=record['validated'],reason=record.get('error') or 'valid_f2p',executed=True,
                   prepared_draft=str(draft))
