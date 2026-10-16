"""Single-draft mirroring. Models construct candidates; deterministic tests decide."""
from pathlib import Path
from datetime import datetime, timezone
import json
import shutil
import hashlib
import time
import yaml
from patchbench.catalog import read_metadata, safe_component
from patchbench.exceptions import PatchbenchError
from patchbench.gitutils import temporary_checkout, apply_patch
from patchbench.io import write_json
from patchbench.runner import run_command, redact_secrets
from patchbench.repositories import load_repository
from patchbench.curation.profiles import load_agent_profile
from patchbench.curation.runtime import create_curation_agent
from patchbench.curation.snapshot import private_metadata_dir
from .execution import execute, first_diagnostics
from .patches import historical_source_patch, check_changes, write_patches, exact_reverse
from .components import detect_component, map_historical_patch, reactor_modules
from .environment import prepare_environment, environment_digest
from .agent import mirror_request, apply_agent_edits
from .validation import verify_states


def mirror_draft(repository, case_id, component, *, root, agent_profile=None, agent_env_file=None,
                 agent_ca_file=None, dry_run=False, agent=None, executor=execute, progress=None, max_attempts=2):
    if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
        raise PatchbenchError('Mirror max_attempts must be between 1 and 3')
    if not all(safe_component(v) for v in (repository, case_id, component)):
        raise PatchbenchError('Repository, case and component must be safe identifiers')
    started = time.monotonic()
    root = Path(root).resolve()
    draft = root / 'drafts' / repository / case_id
    meta = read_metadata(draft)
    if draft.is_symlink() or (draft / 'meta.yaml').is_symlink() or meta.get('draft') is not True or meta.get('repository') != repository or meta.get('id') != case_id:
        raise PatchbenchError('Mirroring accepts only matching drafts')
    if meta.get('construction_mode') == 'mr_mirror_v1':
        raise PatchbenchError('Draft is already mirrored; validate it rather than overwriting it')
    manifest = load_repository(repository, draft)
    source = manifest.checkout()
    historical = draft / meta['gold_patch']
    patch_text = historical.read_text()
    mapping_record = {}
    if component == 'auto':
        policy_config = manifest.config.get('benchmark_maven_policy', {})
        reference = policy_config.get('reference', 'HEAD')
        with temporary_checkout(source, reference) as checkout:
            component, policy, mapping_record = detect_component(manifest, checkout, patch_text)
        if not component:
            record = dict(schema_version=1, case_id=case_id, status='rejected', reason='unsupported_scope',
                          attempts=[], model_usage=[], component_detection=mapping_record)
            if not dry_run:
                private = private_metadata_dir(draft); private.mkdir(exist_ok=True)
                write_json(private / 'mirror.json', record)
            return record
        directory = root / 'benchmark-environments' / repository / component
        environment_path = directory / 'environment.json'
        environment = json.loads(environment_path.read_text()) if environment_path.exists() else None
        revision = run_command(['git', 'rev-parse', reference], cwd=source).stdout.strip()
        if environment and (environment.get('reference_sha') != revision or
                environment.get('environment_digest') != environment_digest(environment)):
            raise PatchbenchError('Existing environment has changed revision or invalid digest')
        if not environment:
            # Operational cache comes from a prepared local environment, never committed paths.
            dependency_cache = None
            for cached in sorted((root / 'benchmark-environments' / repository).glob('*/environment.json')):
                existing = json.loads(cached.read_text())
                if (existing.get('reference_sha') == revision and existing.get('image') == policy['image']
                        and existing.get('environment_eligible')
                        and existing.get('environment_digest') == environment_digest(existing)):
                    dependency_cache = existing.get('dependency_cache')
                    if dependency_cache:
                        break
            environment = prepare_environment(repository, component, root=root, reference=reference,
                dry_run=dry_run, dependency_cache=dependency_cache, policy=policy, executor=executor)
    else:
        directory = root / 'benchmark-environments' / repository / component
        environment = json.loads((directory / 'environment.json').read_text())
    record = dict(schema_version=1, case_id=case_id, component=component,
        reference_sha=environment['reference_sha'], status='planned', reason=None,
        component_detection=mapping_record, mirror_method=None, attempts=[], model_usage=[], timestamp=datetime.now(timezone.utc).isoformat())
    if dry_run:
        return record
    private = private_metadata_dir(draft); private.mkdir(exist_ok=True)
    session = private / ('mirror-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')); session.mkdir()
    def finish(reason):
        record['runtime_seconds'] = time.monotonic() - started
        record.update(status='prepared' if reason == 'valid_f2p' else 'rejected', reason=reason)
        write_json(session / 'mirror.json', record); write_json(private / 'mirror.json', record)
        return record
    if not environment.get('environment_eligible'):
        return finish(environment.get('reason') or 'baseline_not_green')
    # Bind new scientific evidence to actual image/cache bytes rather than tags.
    # Environment eligibility is still rechecked by one authoritative suite.
    if executor is execute:
        try:
            environment = bind_environment(source, environment)
        except PatchbenchError as exc:
            record['error'] = str(exc)
            return finish('environment_identity_invalid')
        record['image_identity'] = environment['image_identity']
        record['dependency_cache_digest'] = environment['dependency_cache_digest']
    with temporary_checkout(source, environment['reference_sha']) as checkout:
        mapped, mapping, rejected = map_historical_patch(patch_text, checkout, sorted(set(reactor_modules(checkout)) | set(environment.get('component_paths', [environment['component_path']]))))
    record['file_mapping'] = mapping
    record['unmapped_files'] = rejected
    selected, allowed = historical_source_patch(mapped, environment.get('component_paths', [environment['component_path']]))
    if not allowed:
        return finish('forbidden_file_change')
    source = manifest.checkout()
    mirror, gold = session / 'mirror.patch', session / 'gold.patch'
    try:
        verification = None
        feedback = None
        # Reverse first, then a bounded repair loop. Every candidate starts from reference.
        for index, method in enumerate(['deterministic_reverse'] + ['agent'] * max_attempts):
            if method == 'agent' and not (agent_profile or agent):
                break
            attempt = dict(method=method, reason=None, number=index)
            label = method if index <= 1 else method + '-' + str(index)
            record['attempts'].append(attempt)
            with temporary_checkout(source, environment['reference_sha']) as checkout:
                if progress: progress(case_id, 1, 1)
                if method == 'deterministic_reverse':
                    fix = session / 'historical-source.patch'; fix.write_text(selected)
                    reverse_ok, diagnostic = exact_reverse(checkout, selected)
                    attempt.update(applicable=reverse_ok, diagnostic=diagnostic)
                    if not reverse_ok:
                        attempt['reason'] = 'reverse_patch_not_applicable'
                        feedback = dict(reason=attempt['reason'], diagnostic=diagnostic)
                        continue
                else:
                    profile = load_agent_profile(agent_profile)
                    if 'mirror' not in profile.roles:
                        raise PatchbenchError('Profile requires mirror role')
                    request, supplied = mirror_request(profile, (draft / meta['problem_statement']).read_text(), selected, checkout, allowed, feedback=feedback)
                    runtime = agent or create_curation_agent(profile, agent_env_file, root, agent_ca_file)
                    write_json(session / (label + '-request.json'), dict(prompt=redact_secrets(request.prompt), output_schema=request.output_schema))
                    response = runtime.run(request)
                    record['model_usage'].append(dict({**response.details, **dict(profile=profile.name, profile_digest=profile.digest,
                        harness=profile.harness, requested_model=profile.model, prompt_version=profile.roles['mirror'].prompt_version,
                        prompt_digest=hashlib.sha256(request.prompt.encode()).hexdigest(),
                        output_schema_digest=hashlib.sha256(json.dumps(request.output_schema, sort_keys=True).encode()).hexdigest(),
                        runtime_budget=vars(profile.roles['mirror']), supplied_files=supplied)}))
                    write_json(session / (label + '-agent-response.json'), dict(text=redact_secrets(response.text), error=response.error, details=response.details))
                    try:
                        apply_agent_edits(response, checkout, allowed)
                    except PatchbenchError as exc:
                        attempt.update(reason='forbidden_file_change' if 'forbidden_file_change' in str(exc) else 'mirror_agent_failed',
                                       diagnostic=redact_secrets(str(exc)))
                        feedback = dict(reason=attempt['reason'], diagnostic=attempt['diagnostic'],
                                        previous_output=redact_secrets(response.text)[-8000:])
                        if attempt['reason'] == 'forbidden_file_change':
                            break
                        continue
                try:
                    record['mirror_files'] = check_changes(checkout, allowed)
                except PatchbenchError as exc:
                    attempt.update(reason='no_f2p_tests' if 'no_f2p_tests' in str(exc) else 'forbidden_file_change', diagnostic=redact_secrets(str(exc)))
                    feedback = dict(reason=attempt['reason'], diagnostic=attempt['diagnostic'])
                    continue
                attempt_dir = session / label; attempt_dir.mkdir()
                mirror, gold = attempt_dir / 'mirror.patch', attempt_dir / 'gold.patch'
                write_patches(checkout, mirror, gold)
            verification = verify_states(source, environment, mirror, gold, attempt_dir / 'validation',
                                         allowed_files=allowed, executor=executor,
                                         relevant_test_classes=mapping_record.get('relevant_test_classes'))
            attempt['verification'] = verification
            attempt['reason'] = verification['reason']
            bug = verification.get('mirrored_bug') or {}
            output = bug.get('output', '')
            diagnostics = first_diagnostics(output)
            feedback = dict(reason=verification['reason'], diagnostic=diagnostics,
                            previous_mirror=mirror.read_text()[:10000],
                            tests_executed=(bug.get('report') or {}).get('executed'))
            if verification['validated']:
                record.update(mirror_method=method, verification=verification)
                break
        if not verification or not verification['validated'] or record['attempts'][-1]['reason'] != 'valid_f2p':
            final_reason = record['attempts'][-1]['reason'] or 'mirror_agent_failed'
            # A failed optional repair must not erase an executed, well-formed NO_F2P result.
            # Retain all agent errors independently; this is diagnosis, never acceptance.
            had_executed_no_f2p = any((a.get('verification') or {}).get('reason') == 'no_f2p_tests' for a in record['attempts'])
            if final_reason in ('mirror_agent_failed', 'compilation_failure', 'unrelated_test_failure', 'timeout') and had_executed_no_f2p:
                record['repair_failure'] = final_reason
                final_reason = 'no_f2p_tests'
            return finish(final_reason)
        record['benchmark_base_sha'] = retain_prepared_mirror(source, environment, draft, meta, historical,
            mirror, gold, verification, component, record['mirror_method'], record['mirror_files'])
        return finish('valid_f2p')
    except Exception as exc:
        record['error'] = redact_secrets(str(exc))
        reason = 'forbidden_file_change' if 'forbidden_file_change' in str(exc) else 'mirror_agent_failed'
        return finish(reason)


def bind_environment(source, environment):
    """Freeze real dependency inputs for newly constructed mirrors and oracles."""
    from patchbench.historical_pr_tests import image_identity, cache_identity
    identity = image_identity(environment['image'])
    if not identity:
        raise PatchbenchError('Environment image cannot be resolved to an immutable identity')
    if environment.get('image_identity') and environment['image_identity'] != identity:
        raise PatchbenchError('Environment image identity mismatch')
    with temporary_checkout(source, environment['reference_sha']) as checkout:
        digest = cache_identity(environment.get('dependency_cache'), checkout)
    if environment.get('dependency_cache_digest') and environment['dependency_cache_digest'] != digest:
        raise PatchbenchError('Environment dependency cache identity mismatch')
    result = {**environment, 'image_identity': identity, 'dependency_cache_digest': digest}
    result['environment_digest'] = environment_digest(result)
    return result


def retain_prepared_mirror(source, environment, draft, meta, historical, mirror, gold, verification,
                           component, method, files, oracle_patch=None):
    case_id = meta['id']
    record = dict(mirror_method=method, mirror_files=files)
    # Materialize a REAL bug commit, so existing solvers start at an isolated buggy tree.
    with temporary_checkout(source, environment['reference_sha']) as checkout:
        applied, error = apply_patch(checkout, mirror)
        if not applied: raise PatchbenchError(error)
        run_command(['git', 'add', '--all'], cwd=checkout)
        committed = run_command(['git', '-c', 'user.name=PatchBench', '-c', 'user.email=patchbench@localhost',
            'commit', '-m', 'PatchBench MR mirror ' + case_id], cwd=checkout)
        if committed.exit_code: raise PatchbenchError('Cannot materialize mirror commit')
        bug_sha = run_command(['git', 'rev-parse', 'HEAD'], cwd=checkout).stdout.strip()
        fetched = run_command(['git', 'fetch', str(checkout), bug_sha + ':refs/patchbench/mirrors/' + case_id], cwd=source)
        if fetched.exit_code: raise PatchbenchError('Cannot retain benchmark base commit')
    for name in ('historical-mr.patch', 'mirror.patch', 'benchmark-gold.patch', 'benchmark-environment.json', 'benchmark-baseline.json') + (('hidden-regression.patch',) if oracle_patch else ()):
        if (draft / name).exists(): raise PatchbenchError('Refusing to overwrite existing V2 artifact: ' + name)
    shutil.copyfile(historical, draft / 'historical-mr.patch')
    shutil.copyfile(mirror, draft / 'mirror.patch')
    shutil.copyfile(gold, draft / 'benchmark-gold.patch')
    # Keep machine cache outside portable, committed metadata. The image must carry deps
    # or verification needs the local cache selected at preparation time.
    write_json(draft / 'benchmark-environment.json', environment)
    write_json(draft / 'benchmark-baseline.json', verification['reference'])
    updated = {**meta, 'construction_mode':'mr_mirror_v1', 'oracle_mode':'existing_tests',
        'base_commit':bug_sha, 'fix_commit':environment['reference_sha'], 'gold_patch':'benchmark-gold.patch',
        'test_command':__import__('shlex').join(environment['test_command']), 'image':environment['image'],
        'env':{}, 'setup_commands':[], 'workdir':environment.get('workdir'),
        'historical':dict(**({'pr_number':meta['source']['pull_request_number']} if meta['source'].get('type') == 'github' else {'mr_iid':meta['source']['merge_request_iid']}), original_base_sha=meta['base_commit'],
            original_fix_sha=meta['fix_commit'], mr_patch='historical-mr.patch', provenance='provenance.json'),
        'benchmark':dict(reference_sha=environment['reference_sha'], component=component, oracle_mode='existing_tests',
            environment_record='benchmark-environment.json', baseline_record='benchmark-baseline.json',
            mirror_patch='mirror.patch', mirror_method=record['mirror_method'], mirror_files=record['mirror_files'],
            f2p_tests=verification['f2p_tests'], p2p_tests=verification['p2p_tests'],
            relevant_test_classes=verification.get('relevant_test_classes', []))}
    updated.pop('test_patch', None); updated.pop('draft_todos', None)
    updated['artifacts'] = {k:v for k,v in updated.get('artifacts', {}).items() if k not in ('test_patch', 'gold_patch')}
    updated['artifacts'].update(historical_mr_patch='historical-mr.patch', gold_patch='benchmark-gold.patch')
    if oracle_patch:
        shutil.copyfile(oracle_patch, draft / 'hidden-regression.patch')
        updated['oracle_mode'] = 'existing_tests_with_regression'
        updated['benchmark'].update(oracle_mode='existing_tests_with_regression',
                                    oracle_patch='hidden-regression.patch')
    (draft / 'meta.yaml').write_text(yaml.safe_dump(updated, sort_keys=False))
    return bug_sha
