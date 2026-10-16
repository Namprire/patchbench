"""Authoritative existing-tests reference / bug / repaired validation."""
from pathlib import Path
import json
from patchbench.exceptions import PatchbenchError
from patchbench.gitutils import temporary_checkout, apply_patch
from patchbench.io import write_json
from patchbench.runner import run_command
from .execution import execute
from .reports import green, differential
from .environment import environment_digest
from .patches import check_changes, production_file


def verify_states(source, environment, mirror_patch, gold_patch, destination, *, allowed_files,
                  executor=execute, designated=None, bug_commit=None, oracle_patch=None,
                  relevant_test_classes=None):
    """Validate each state in a clean checkout, with one authoritative Reference run.

    Build outputs, reports and caches produced by Base must never become Gold inputs.
    Existing-test and generated-test mirrors use the same repeated behavioral gate.
    """
    if not environment.get('environment_eligible') or environment.get('verification_network') != 'none' or environment.get('cern_dev_services_required') is not False:
        raise PatchbenchError('external_dependency: environment is not locally eligible')
    if environment.get('environment_digest') != environment_digest(environment):
        raise PatchbenchError('Environment evidence digest mismatch')
    if environment.get('image_identity'):
        from patchbench.historical_pr_tests import image_identity
        if image_identity(environment['image']) != environment['image_identity']:
            raise PatchbenchError('Environment image identity mismatch')
    if environment.get('dependency_cache_digest'):
        from patchbench.historical_pr_tests import cache_identity
        with temporary_checkout(source, environment['reference_sha']) as checkout:
            if cache_identity(environment.get('dependency_cache'), checkout) != environment['dependency_cache_digest']:
                raise PatchbenchError('Environment dependency cache identity mismatch')
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    paths = environment.get('component_paths', [environment['component_path']])
    relevant_test_classes = sorted(set(relevant_test_classes or []))
    result = dict(schema_version=1, reference=None, mirrored_bug=None, restored=None,
                  f2p_tests=[], p2p_tests=[], validated=False, reason='baseline_not_green',
                  relevant_test_classes=relevant_test_classes)

    def finish():
        write_json(destination / 'differential-validation.json', result)
        return result

    def inject(checkout):
        if oracle_patch:
            from .oracle import inject_oracle
            return inject_oracle(checkout, oracle_patch, paths)
        return None

    def apply_mirror(checkout):
        applied, error = apply_patch(checkout, mirror_patch)
        if not applied:
            result.update(reason='reverse_patch_not_applicable', error=error)
            return False
        check_changes(checkout, allowed_files)
        return True

    # Check the retained solver tree before injecting evaluator-only material.
    if bug_commit:
        with temporary_checkout(source, environment['reference_sha']) as checkout:
            if not apply_mirror(checkout):
                return finish()
            staged = run_command(['git', 'add', '--all'], cwd=checkout)
            expected = run_command(['git', 'write-tree'], cwd=checkout).stdout.strip()
            actual = run_command(['git', 'rev-parse', bug_commit + '^{tree}'], cwd=source).stdout.strip()
            if staged.exit_code or not expected or expected != actual:
                raise PatchbenchError('Benchmark base does not match the verified mirror')

    with temporary_checkout(source, environment['reference_sha']) as checkout:
        oracle_file = inject(checkout)
        reference = executor(checkout, environment, destination / 'reference')
    result['reference'] = reference
    if not green(reference):
        return finish()
    tests = reference['report']['tests']
    covered = [p for p in paths if any(k.startswith(p + '::') and t['status'] == 'PASS'
               for k, t in tests.items())]
    result['tested_components'] = covered
    result['untested_components'] = [p for p in paths if p not in covered]
    # Legacy reports have no Maven module prefix; retain their original handling.
    if any(k.count('::') >= 2 for k in tests) and len(covered) != len(paths):
        result['reason'] = 'affected_module_tests_not_executed'
        return finish()
    executed_classes = {k.split('::')[-2] for k, t in tests.items() if t['status'] == 'PASS'}
    if relevant_test_classes and not any(
            actual == expected or actual.startswith(expected + '$')
            for actual in executed_classes for expected in relevant_test_classes):
        result['reason'] = 'relevant_test_classes_not_executed'
        result['missing_relevant_test_classes'] = relevant_test_classes
        return finish()

    with temporary_checkout(source, environment['reference_sha']) as checkout:
        inject(checkout)
        if not apply_mirror(checkout):
            return finish()
        bug = executor(checkout, environment, destination / 'mirrored_bug')
        delta = differential(reference, bug)
        result.update(delta, mirrored_bug=bug)
        if delta['reason'] != 'valid_f2p':
            return finish()
        if oracle_file:
            oracle_class = Path(oracle_file).stem
            if not any(t.split('::')[-2] == oracle_class or ('::' + oracle_class + '::') in t or
                       t.split('::')[-2].endswith('.' + oracle_class) or
                       (oracle_file.endswith('.py') and ('.' + oracle_class + '.') in ('.' + t.split('::')[-2] + '.'))
                       for t in delta['f2p_tests']):
                result['reason'] = 'generated_test_not_executed'
                return finish()
        # Require concrete package, source-reference or architecture-assertion evidence.
        packages = {str(Path(f.split('/src/main/java/', 1)[1]).parent).replace('/', '.') + '.'
                    for f in allowed_files if '/src/main/java/' in f}
        packages.discard('..')
        classes = {f.split('/src/main/java/', 1)[1][:-5].replace('/', '.')
                   for f in allowed_files if '/src/main/java/' in f}
        python_modules = {f[4:-3].replace('/', '.') for f in allowed_files if f.startswith('lib/rucio/') and f.endswith('.py')}
        def related(test):
            classname = test.split('::')[-2]
            if any(classname.startswith(package) for package in packages):
                return True
            if python_modules:
                for path in (checkout / 'tests').glob('test*.py'):
                    if classname == 'tests.' + path.stem or classname.startswith('tests.' + path.stem + '.') or classname == path.stem or classname.startswith(path.stem + '.'):
                        if any(m in path.read_text(errors='replace') for m in python_modules):
                            return True
            message = bug['report']['tests'][test].get('message', '')
            if any(c in message for c in classes):
                return True
            for path in checkout.glob('**/src/test/java/' + classname.replace('.', '/') + '.java'):
                text = path.read_text(errors='replace')
                if any(c in text or (' ' + c.rsplit('.', 1)[-1] + ' ') in text for c in classes):
                    return True
            return False
        unrelated = [test for test in delta['f2p_tests'] if (packages or python_modules) and not related(test)]
        result['unrelated_failures'] = unrelated
        if unrelated or (designated is not None and sorted(designated) != delta['f2p_tests']):
            result['reason'] = 'unrelated_test_failure'
            return finish()

    with temporary_checkout(source, environment['reference_sha']) as checkout:
        inject(checkout)
        if not apply_mirror(checkout):
            return finish()
        repeated = executor(checkout, environment, destination / 'mirrored_bug_repeat')
    result['mirrored_bug_repeat'] = repeated
    # Compare inventories and individual statuses as well as F2P/P2P sets. Messages
    # can legitimately contain differing assertion values or elapsed timing.
    statuses = lambda execution: {k: (t['status'], t.get('type', ''), t.get('pytest_call_failure'))
                                  for k, t in execution['report']['tests'].items()}
    if differential(reference, repeated) != delta or statuses(repeated) != statuses(bug):
        result['reason'] = 'unstable_oracle'
        return finish()

    with temporary_checkout(source, environment['reference_sha']) as checkout:
        inject(checkout)
        if not apply_mirror(checkout):
            return finish()
        applied, error = apply_patch(checkout, gold_patch)
        # Exact production and oracle restoration; a merely green alternative fix
        # is insufficient for validating the retained benchmark gold patch.
        clean = run_command(['git', 'diff', '--exit-code', 'HEAD'], cwd=checkout).exit_code == 0
        if not applied or not clean:
            result.update(reason='gold_did_not_pass', error=error or 'Gold does not restore reference')
            return finish()
        restored = executor(checkout, environment, destination / 'restored')
    result['restored'] = restored
    result['validated'] = green(restored) and restored['report']['tests'] == reference['report']['tests']
    if not result['validated']:
        result['reason'] = 'gold_did_not_pass'
    return finish()


def validate_existing_case(case, *, container):
    from patchbench.models import ValidationResult
    from patchbench.catalog import read_metadata
    raw = read_metadata(case.case_dir)
    benchmark = raw['benchmark']
    environment = json.loads((case.case_dir / benchmark['environment_record']).read_text())
    if case.test_command != environment['test_command'] or case.workdir != environment.get('workdir') or case.image != environment['image'] or case.env or case.setup_commands:
        raise PatchbenchError('Existing-tests case execution policy differs from verified environment')
    if benchmark['reference_sha'] != environment['reference_sha']:
        raise PatchbenchError('Benchmark reference differs from environment')
    allowed = benchmark['mirror_files']
    if not allowed or any(not production_file(f, environment.get('component_paths', [environment['component_path']])) for f in allowed):
        raise PatchbenchError('forbidden_file_change')
    # No host mode: offline container is the enforceable external-service boundary.
    if not container:
        raise PatchbenchError('existing_tests validation requires --container')
    result = verify_states(case.repo_path, environment, case.case_dir / benchmark['mirror_patch'],
        case.gold_patch, case.logs_dir / 'existing-tests', allowed_files=allowed,
        designated=benchmark['f2p_tests'], bug_commit=case.base_commit,
        oracle_patch=case.case_dir / benchmark['oracle_patch'] if benchmark.get('oracle_patch') else None,
        relevant_test_classes=benchmark.get('relevant_test_classes'))
    bug, restored = result['mirrored_bug'], result['restored']
    return ValidationResult(case.id, case.base_commit, True,
        bug['exit_code'] if bug else None, bool(bug and green(bug)),
        bool(restored), restored['exit_code'] if restored else None,
        bool(restored and green(restored)), result['validated'], None if result['validated'] else result['reason'])


def evaluate_existing_patch(case, patch_path, run_dir):
    from patchbench.commands import _PatchEvaluation
    from patchbench.gitutils import diff_stats
    from patchbench.catalog import read_metadata
    run_dir.mkdir(parents=True, exist_ok=True)
    raw = read_metadata(case.case_dir)
    env = json.loads((case.case_dir / raw['benchmark']['environment_record']).read_text())
    if environment_digest(env) != env.get('environment_digest'):
        raise PatchbenchError('Environment digest mismatch')
    if env.get('image_identity'):
        from patchbench.historical_pr_tests import image_identity
        if image_identity(env['image']) != env['image_identity']:
            raise PatchbenchError('Environment image identity mismatch')
    with temporary_checkout(case.repo_path, case.base_commit) as checkout:
        if env.get('dependency_cache_digest'):
            from patchbench.historical_pr_tests import cache_identity
            if cache_identity(env.get('dependency_cache'), checkout) != env['dependency_cache_digest']:
                raise PatchbenchError('Environment dependency cache identity mismatch')
        applied, error = apply_patch(checkout, patch_path)
        if not applied:
            return _PatchEvaluation(False, False, error, [], 0, 0)
        files, added, removed = diff_stats(checkout)
        # Solver cannot weaken the oracle or alter build configuration either.
        if any(not production_file(f, env.get('component_paths', [env['component_path']])) for f in files):
            return _PatchEvaluation(True, False, 'forbidden_file_change', files, added, removed)
        oracle = raw['benchmark'].get('oracle_patch')
        if oracle:
            # Solver patch statistics/authority are checked before evaluator injection.
            from .oracle import inject_oracle
            run_command(['git', 'add', '--all'], cwd=checkout)
            run_command(['git', '-c', 'user.name=PatchBench', '-c', 'user.email=patchbench@localhost',
                         'commit', '--allow-empty', '-m', 'Solver candidate'], cwd=checkout)
            inject_oracle(checkout, case.case_dir / oracle, env.get('component_paths', [env['component_path']]))
        result = execute(checkout, env, run_dir)
        (run_dir / 'test.log').write_text(result['output'])
        baseline_path = case.case_dir / raw['benchmark']['baseline_record']
        baseline = json.loads(baseline_path.read_text())
        resolved = green(result) and result['report']['tests'] == baseline['report']['tests']
        return _PatchEvaluation(True, resolved, None if resolved else 'existing_tests_not_restored', files, added, removed)
