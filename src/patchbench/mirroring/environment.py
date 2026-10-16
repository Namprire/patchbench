"""Per-component environment eligibility, independent of MR candidates."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
from patchbench.catalog import safe_component
from patchbench.exceptions import PatchbenchError
from patchbench.gitutils import temporary_checkout
from patchbench.repositories import load_repository
from patchbench.runner import run_command
from patchbench.io import write_json
from .execution import execute
from .reports import green, execution_reason


def environment_digest(record):
    # Local cache locations are operational, not curation policy.
    return hashlib.sha256(json.dumps({k:v for k,v in record.items() if k not in
        ('dependency_cache', 'environment_digest')}, sort_keys=True).encode()).hexdigest()


def prepare_environment(repository, component, *, root, reference='HEAD', dry_run=False,
                        dependency_cache=None, executor=execute, policy=None):
    if not safe_component(repository) or not safe_component(component):
        raise PatchbenchError('Repository and component must be safe identifiers')
    root = Path(root).resolve()
    manifest = load_repository(repository, root / 'drafts')
    policy = policy or manifest.config.get('benchmark_components', {}).get(component)
    if not isinstance(policy, dict):
        raise PatchbenchError('Unknown benchmark component; configure benchmark_components in the repository manifest')
    source = manifest.checkout()
    revision = run_command(['git', 'rev-parse', '--verify', reference + '^{commit}'], cwd=source)
    if revision.exit_code:
        raise PatchbenchError('Reference revision does not exist')
    if type(policy.get('external_services_required')) is not bool:
        raise PatchbenchError('Component policy must explicitly declare external_services_required')
    record = dict(schema_version=1, repository=repository, component=component,
        reference_sha=revision.stdout.strip(), component_path=policy['path'],
        component_paths=policy.get('paths', [policy['path']]),
        java_version=policy.get('java_version'), image=policy['image'],
        workdir=policy.get('workdir'), setup_commands=policy.get('setup_commands', []),
        test_command=policy['test_command'], test_policy=policy['test_policy'],
        timeout_seconds=policy.get('timeout_seconds', 900), dependency_cache=str(Path(dependency_cache).resolve()) if dependency_cache else None,
        verification_network='none', cern_dev_services_required=policy['external_services_required'], environment_eligible=False,
        timestamp=datetime.now(timezone.utc).isoformat(), reason='planned', baseline_tests=0)
    if dry_run:
        return record
    destination = root / 'benchmark-environments' / repository / component
    destination.mkdir(parents=True, exist_ok=True)
    if policy['external_services_required']:
        record.update(reason='external_dependency', runtime_seconds=0, network_required=False)
        record['environment_digest'] = environment_digest(record)
        write_json(destination / 'environment.json', record)
        write_json(destination / 'baseline-validation.json', dict(executed=False, reason='external_dependency'))
        return record
    with temporary_checkout(source, record['reference_sha']) as checkout:
        if record['setup_commands']:
            setup = executor(checkout, record, destination / 'setup', setup=True)
            record['setup_result'] = {k:v for k,v in setup.items() if k != 'output'}
        baseline = executor(checkout, record, destination / 'baseline')
    record.update(baseline_tests=baseline['report']['executed'],
        runtime_seconds=baseline['runtime_seconds'], network_required=False,
        setup_network_required=bool(record['setup_commands']),
        environment_eligible=green(baseline), reason='eligible' if green(baseline) else execution_reason(baseline) or 'baseline_not_green')
    record['environment_digest'] = environment_digest(record)
    write_json(destination / 'baseline-validation.json', baseline)
    write_json(destination / 'environment.json', record)
    return record
