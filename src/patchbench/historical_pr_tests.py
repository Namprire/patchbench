"""Exact historical developer-test construction; no models or source rewriting."""
from pathlib import Path, PurePosixPath
from datetime import datetime, timezone
import hashlib
import json
import os
import shlex
import configparser
import re
import stat
import xml.etree.ElementTree as ET
import yaml

from patchbench.catalog import read_metadata
from patchbench.case import load_case
from patchbench.exceptions import PatchbenchError
from patchbench.gitutils import temporary_checkout, apply_patch
from patchbench.io import write_json
from patchbench.models import ValidationResult
from patchbench.runner import run_command
from patchbench.mirroring.execution import execute
from patchbench.mirroring.patches import production_file
from patchbench.mirroring.reports import green, differential
from patchbench.repositories import load_repository

MODE = 'historical_pr_tests_v1'


def resolve_dependency_cache(case_dir, settings):
    """Resolve a portable cache reference without changing its pinned contents."""
    reference = settings.get('dependency_cache')
    if not reference:
        return None
    if reference.startswith('$'):
        configured = os.environ.get(reference[1:])
        if configured:
            return str(Path(configured).expanduser().resolve())
        manifest = load_repository(read_metadata(case_dir)['repository'], case_dir)
        return str((manifest.path.parent.parent / settings['dependency_cache_default']).resolve())
    return str(Path(reference).expanduser().resolve())


def historical_baseline(case):
    settings = read_metadata(case.case_dir)['historical_pr_tests']
    name = settings.get('baseline_record')
    if not name or not safe_path(name):
        raise PatchbenchError('Historical solver evaluation requires a frozen validation baseline; promote the validated draft first')
    path = case.case_dir / name
    if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != settings.get('baseline_sha256'):
        raise PatchbenchError('Historical validation baseline identity changed')
    return json.loads(path.read_text())


def git(source, *args):
    result = run_command(['git', *args], cwd=source)
    if result.exit_code:
        raise PatchbenchError(result.stderr.strip() or 'Historical Git operation failed')
    return result.stdout


def historical_policy(repository, draft, profile=None):
    manifest = load_repository(repository, draft)
    policy = manifest.config.get('historical_pr_tests', {})
    if repository != 'rucio' and not policy:
        raise PatchbenchError('Repository has no configured historical developer-test policy')
    if profile:
        selected = manifest.config.get('environment_profiles', {}).get(profile)
        if not isinstance(selected, dict):
            raise PatchbenchError('Unknown historical environment profile')
        policy = {**policy, **selected.get('historical_pr_tests', {})}
        policy['env'] = {**manifest.config.get('historical_pr_tests', {}).get('env', {}),
                         **selected.get('historical_pr_tests', {}).get('env', {})}
        if selected.get('image'):
            policy['image'] = selected['image']
    return manifest, policy


def check_provenance(manifest, provenance):
    source = manifest.config.get('source', {})
    if manifest.id == 'rucio':
        matched = provenance.get('source') == 'github' and provenance.get('github_repo') == source.get('github_repo', 'rucio/rucio')
    else:
        matched = (provenance.get('source') == 'gitlab'
                   and provenance.get('gitlab_url') == source.get('gitlab_url')
                   and str(provenance.get('project_id')) == str(source.get('project_id'))
                   and isinstance(provenance.get('merge_request_iid'), int))
    if not matched or not provenance.get('merged_at'):
        raise PatchbenchError('Historical tests require matching merged repository provenance')


def developer_test(name, policy=None):
    if (policy or {}).get('framework') == 'maven':
        path = PurePosixPath(name)
        return (safe_path(name) and '/src/test/java/' in '/' + name
                and name.endswith('.java') and bool(re.match(r'(?:Test.*|.*Tests?|.*TestCase)\.java$', path.name)))
    roots = (policy or {}).get('test_roots', ['tests/', 'lib/rucio/tests/'])
    path = PurePosixPath(name)
    return (not path.is_absolute() and '..' not in path.parts and '\\' not in name
            and name.startswith(tuple(roots))
            and path.name.startswith('test_') and name.endswith('.py'))


def test_side_file(name, policy=None):
    """Developer fixtures, helpers and dedicated pytest configuration are evaluator inputs."""
    path = PurePosixPath(name)
    if (policy or {}).get('framework') == 'maven':
        return safe_path(name) and any(root in '/' + name for root in ('/src/test/java/', '/src/test/resources/'))
    return (not path.is_absolute() and '..' not in path.parts and '\\' not in name
            and (name.startswith(tuple((policy or {}).get('test_roots', ['tests/', 'lib/rucio/tests/'])))
                 or name in ('conftest.py', 'pytest.ini', '.pytest.ini')))


def pytest_configuration_only(source, base, fix, name):
    """Accept shared config files only if every non-pytest setting is unchanged."""
    if name not in ('pyproject.toml', 'setup.cfg', 'tox.ini'):
        return False
    settings = []
    for revision in (base, fix):
        text = git(source, 'show', revision + ':' + name) if git(source, 'ls-tree', revision, '--', name).strip() else ''
        if name == 'pyproject.toml':
            try:
                import tomllib
            except ImportError:
                import tomli as tomllib
            data = tomllib.loads(text)
            tools = data.get('tool', {})
            tools.pop('pytest', None)
            if not tools:
                data.pop('tool', None)
        else:
            parser = configparser.ConfigParser(interpolation=None)
            parser.read_string(text)
            data = {section:dict(parser.items(section)) for section in parser.sections()
                    if section not in ('pytest', 'tool:pytest')}
            data['DEFAULT'] = dict(parser.defaults())
        settings.append(data)
    return settings[0] == settings[1]


def historical_patches(source, base, fix, policy=None, *, require_tests=True):
    policy = policy or {}
    rows = git(source, 'diff', '--name-status', '--no-renames', base, fix, '--').splitlines()
    production, tests, omitted = [], [], []
    for row in rows:
        status, name = row.split('\t')
        if not safe_path(name):
            raise PatchbenchError('Historical patch paths must stay inside the checkout')
        for revision in (base, fix):
            entry = git(source, 'ls-tree', revision, '--', name).strip()
            if entry and entry.split()[0] not in ('100644', '100755'):
                raise PatchbenchError('Historical inputs must be regular files')
        if status in ('M', 'A', 'D') and (test_side_file(name, policy) or pytest_configuration_only(source, base, fix, name)):
            tests.append(name)
        elif status in ('M', 'A', 'D') and ((policy.get('framework') == 'maven' and
                any(root in '/' + name for root in ('/src/main/java/', '/src/main/resources/')))
                or (production_file(name, 'lib/rucio') if not policy.get('production_roots') else
                name.startswith(tuple(policy['production_roots'])) and name.endswith(tuple(policy.get('production_extensions', ['.py'])))
                and 'test' not in PurePosixPath(name).parts and 'tests' not in PurePosixPath(name).parts)):
            production.append(name)
        else:
            omitted.append(name)
    if not production or (require_tests and not any(developer_test(p, policy) and git(source, 'ls-tree', fix, '--', p).strip() for p in tests)):
        raise PatchbenchError('Historical path requires production fixes AND actual developer test changes')
    # Documentation is inert; refuse hidden configuration/dependency/helper changes.
    if any(not (p.endswith(('.md', '.rst')) or p.startswith('docs/')) for p in omitted):
        raise PatchbenchError('Historical PR has unsupported non-production/test changes')
    return (git(source, 'diff', '--binary', '--full-index', base, fix, '--', *production),
            git(source, 'diff', '--binary', '--full-index', base, fix, '--', *tests) if tests else '', production, tests, omitted)


def historical_command(policy, nodes):
    """Run component pytest configuration from its declared working directory."""
    workdir = policy.get('workdir')
    directories = policy.get('test_workdirs', {})
    if directories:
        selected = {directory for root, directory in directories.items()
                    if any(node.split('::')[0].startswith(root) for node in nodes)}
        if len(selected) != 1 or not all(any(node.split('::')[0].startswith(root)
                for root in directories) for node in nodes):
            raise PatchbenchError('Historical tests require one configured component working directory')
        workdir = selected.pop()
    arguments = ['/host/' + node for node in nodes] if workdir else nodes
    report = '/host/patchbench-junit.xml' if workdir else 'patchbench-junit.xml'
    options = ['-o', 'addopts=' + policy['targeted_addopts']] if 'targeted_addopts' in policy else []
    return ([policy.get('python', 'python'), '-B', '-m', 'pytest', *options, *arguments,
             '--junitxml=' + report, '-q'], workdir)


def safe_path(name):
    path = PurePosixPath(name)
    return bool(name) and not path.is_absolute() and '..' not in path.parts and '\\' not in name


def policy_digest(policy):
    return hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()


def targeted_pytest_policy(policy, checkout, nodes):
    """Remove only existing positional collection paths from configured addopts."""
    _, workdir = historical_command(policy, nodes)
    directory = checkout / (workdir or '.')
    addopts = ''
    for name in ('pytest.ini', '.pytest.ini', 'pyproject.toml', 'tox.ini', 'setup.cfg'):
        path = directory / name
        if not path.is_file():
            continue
        if name == 'pyproject.toml':
            try:
                import tomllib
            except ImportError:
                import tomli as tomllib
            options = tomllib.loads(path.read_text()).get('tool', {}).get('pytest', {}).get('ini_options', {})
        else:
            parser = configparser.ConfigParser(interpolation=None)
            parser.read_string(path.read_text())
            section = 'tool:pytest' if name == 'setup.cfg' else 'pytest'
            options = dict(parser.items(section)) if parser.has_section(section) else {}
        if options or name in ('pytest.ini', '.pytest.ini'):
            addopts = options.get('addopts', '')
            if isinstance(addopts, list):
                addopts = shlex.join(addopts)
            break
    tokens = shlex.split(addopts)
    option_values = {'-k', '-m', '-p', '-c', '-o', '--override-ini', '--rootdir', '--confcutdir',
                     '--ignore', '--ignore-glob', '--deselect', '--junitxml', '--junit-prefix',
                     '--maxfail', '--tb', '--capture', '--color', '--log-level', '--basetemp'}
    kept, consumes_value = [], False
    for token in tokens:
        if consumes_value or token.startswith('-'):
            kept.append(token)
            consumes_value = token in option_values
        elif not (directory / token.split('::')[0]).exists():
            kept.append(token)
    return {**policy, 'targeted_addopts': shlex.join(kept)}


def maven_selection(checkout, nodes, production, policy):
    from patchbench.mirroring.components import reactor_modules
    modules = reactor_modules(checkout)
    if (checkout / 'src').is_dir():
        modules.append('.')
    selected = set()
    for name in [*nodes, *production]:
        matches = [m for m in modules if name.startswith(('' if m == '.' else m + '/') + 'src/')]
        if not matches:
            raise PatchbenchError('Historical Java change has no reactor module')
        selected.add(max(matches, key=len))
    classes = {}
    for module in modules:
        for path in (checkout / module / 'src/test/java').rglob('*.java'):
            if path.is_symlink():
                raise PatchbenchError('Historical Java tests must be regular files')
            content = re.sub(r'/\*.*?\*/|//[^\n]*', '', path.read_text(), flags=re.S)
            name = re.search(r'\b(?:class|interface)\s+(\w+)', content)
            if not name:
                continue
            package = re.search(r'^\s*package\s+([\w.]+)\s*;', content, re.M)
            parent = re.search(r'\bextends\s+([\w.]+)', content)
            fqcn = ((package[1] + '.') if package else '') + name[1]
            classes[path.relative_to(checkout).as_posix()] = dict(fqcn=fqcn, simple=name[1],
                parent=parent[1].split('.')[-1] if parent else None,
                abstract=bool(re.search(r'\babstract\s+class\b|\binterface\b', content)), module=module)
    required = []
    def derives(item, target, seen=None):
        seen = seen or set()
        if item['simple'] in seen:
            return False
        if item['parent'] == target:
            return True
        seen.add(item['simple'])
        parents = [c for c in classes.values() if c['simple'] == item['parent']]
        return len(parents) == 1 and derives(parents[0], target, seen)
    for node in nodes:
        item = classes.get(node)
        if not item:
            raise PatchbenchError('Developer Java test class was not found')
        concrete = [item] if not item['abstract'] else [c for c in classes.values()
                    if not c['abstract'] and derives(c, item['simple'])]
        if not concrete:
            raise PatchbenchError('Changed abstract developer test has no concrete subclass')
        for c in concrete:
            selected.add(c['module'])
            required.append(c['fqcn'])
    # Run the predeclared module unit suite, including passing regression tests.
    command = ['mvn', '-o', '-B', '-ntp', '-pl', ','.join(sorted(selected)), '-am']
    groups = policy.get('test_policy', {}).get('excluded_groups', [])
    if groups:
        command.append('-DexcludedGroups=' + ','.join(groups))
    command.append('test')
    return command, dict(modules=sorted(selected), required_test_classes=sorted(set(required)))


def cache_identity(cache, checkout):
    if not cache:
        return None
    directory = Path(cache).resolve() / 'repository'
    if not directory.is_dir():
        raise PatchbenchError('Dependency cache requires a repository directory')
    reactor_coordinates = set()
    for pom in checkout.rglob('pom.xml'):
        root = ET.parse(pom).getroot()
        group = root.findtext('./{*}groupId') or root.findtext('./{*}parent/{*}groupId')
        artifact = root.findtext('./{*}artifactId')
        if group and artifact:
            reactor_coordinates.add(group.replace('.', '/') + '/' + artifact)
    digest = hashlib.sha256()
    for path in sorted(directory.rglob('*')):
        if path.is_symlink():
            raise PatchbenchError('Frozen dependency cache must not contain symlinks')
        if path.is_file():
            relative = path.relative_to(directory).as_posix()
            if any(relative.startswith(c + '/') for c in reactor_coordinates):
                raise PatchbenchError('Dependency cache contains project reactor artifacts; rebuild these from checkout')
            digest.update(relative.encode() + b'\0' + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def image_identity(image):
    try:
        result = run_command(['podman', 'image', 'inspect', '--format', '{{.Id}}', image], cwd=Path.cwd(), timeout_seconds=30)
    except OSError:
        return None
    return result.stdout.strip() if not result.exit_code else None


def configuration_digests(checkout):
    return {p.relative_to(checkout).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in checkout.rglob('*') if p.is_file() and not p.is_symlink()
            and p.name in ('pom.xml', 'setup.cfg', 'tox.ini', 'pytest.ini', 'pyproject.toml')}


def file_identity(checkout, name):
    path = checkout / name
    if path.is_symlink() or not path.resolve().is_relative_to(checkout.resolve()):
        raise PatchbenchError('Historical inputs must be regular files inside the checkout')
    return (path.read_bytes(), bool(path.stat().st_mode & stat.S_IXUSR)) if path.exists() else None


def prepare_historical_pr_tests(draft, *, test_nodes=None, image=None, dependency_cache=None,
                                profile=None, regression_nodes=None, dry_run=False):
    draft = Path(draft).resolve()
    meta = read_metadata(draft)
    case = load_case(draft)
    if meta.get('draft') is not True or meta.get('construction_mode') not in (None, MODE):
        raise PatchbenchError('Historical tests require an unprepared configured draft')
    manifest, policy = historical_policy(meta['repository'], draft, profile)
    provenance = json.loads((draft/'provenance.json').read_text())
    check_provenance(manifest, provenance)
    base, fix = case.base_commit, case.fix_commit
    if (base != provenance['base_commit'] or fix != provenance['human_commit']
            or not provenance.get('merged_at')):
        raise PatchbenchError('Historical PR commit/provenance mismatch')
    production_patch, test_patch, production, test_side, omitted = historical_patches(case.repo_path, base, fix, policy)
    tests = [p for p in test_side if developer_test(p, policy) and git(case.repo_path, 'ls-tree', fix, '--', p).strip()]
    if test_nodes is None:
        # Existing pytest discovery over the unchanged developer files; no synthesized tests.
        test_nodes = tests
    if not test_nodes or any(not isinstance(n, str) or n.split('::')[0] not in tests or n.startswith('-') for n in test_nodes):
        raise PatchbenchError('Select nodes only from the developer-changed test files')
    regression_nodes = list(regression_nodes or policy.get('regression_test_nodes', []))
    check_regression_nodes(case.repo_path, base, fix, regression_nodes, policy)
    command_nodes = [*test_nodes, *regression_nodes]
    with temporary_checkout(case.repo_path, fix) as checkout:
        selection = {}
        if policy.get('framework') == 'maven':
            if any('::' in node for node in command_nodes):
                raise PatchbenchError('Maven selection requires developer test file paths')
            command, selection = maven_selection(checkout, command_nodes, production, policy)
            workdir = None
            from patchbench.preparation.environment import discover_environment
            with temporary_checkout(case.repo_path, base) as base_checkout:
                java = discover_environment(base_checkout, manifest, case)['java_version']
            runtime = manifest.config.get('preparation_runtimes', {}).get(java, {})
            if not runtime and image is None:
                raise PatchbenchError('No image configured for historical Java version')
            image = image or policy.get('image') or runtime.get('image')
            selection['java_version'] = java
        else:
            policy = targeted_pytest_policy(policy, checkout, command_nodes)
            command, workdir = historical_command(policy, command_nodes)
            image = image or policy.get('image', 'patchbench-rucio-historical:tests-v1')
        digest = cache_identity(dependency_cache, checkout)
        configuration = configuration_digests(checkout)
    settings = dict(production_files=production, developer_test_files=tests, test_side_files=test_side,
        test_nodes=list(test_nodes), omitted_documentation=omitted, image=image,
        regression_test_nodes=regression_nodes,
        image_identity=image_identity(image), profile=profile, command_policy_version=2,
        framework=policy.get('framework', 'pytest'), selection=selection, test_command=command,
        targeted_addopts=policy.get('targeted_addopts'), policy_digest=policy_digest(policy),
        configuration_digests=configuration,
        dependency_cache=str(Path(dependency_cache).resolve()) if dependency_cache else None,
        dependency_cache_digest=digest,
        gold_sha256=hashlib.sha256(production_patch.encode()).hexdigest(),
        test_sha256=hashlib.sha256(test_patch.encode()).hexdigest())
    if dry_run:
        return settings
    private = draft/'.patchbench/historical-pr-tests'
    private.mkdir(parents=True, exist_ok=True)
    (private/'historical-full.patch').write_text(git(case.repo_path, 'diff', '--binary', '--full-index', base, fix))
    (draft/'gold.patch').write_text(production_patch)
    (draft/'test.patch').write_text(test_patch)
    meta.update(construction_mode=MODE, oracle_mode='injected_test_patch', image=image,
                test_command=shlex.join(command), timeout_seconds=policy.get('timeout_seconds', 180), setup_commands=[],
                historical_pr_tests=settings,
                draft_todos=['Review deterministic historical validation evidence; do not auto-promote.'])
    if dependency_cache:
        meta['dependency_cache'] = settings['dependency_cache']
    meta['env'] = policy.get('env', {})
    if workdir:
        meta['workdir'] = workdir
    else:
        meta.pop('workdir', None)
    (draft/'meta.yaml').write_text(yaml.safe_dump(meta,sort_keys=False))
    return meta['historical_pr_tests']


def check_regression_nodes(source, base, fix, nodes, policy):
    for node in nodes:
        if not isinstance(node, str) or node.startswith('-') or not developer_test(node.split('::')[0], policy):
            raise PatchbenchError('Regression nodes must be existing developer test files')
        name = node.split('::')[0]
        entries = [git(source, 'ls-tree', revision, '--', name).strip() for revision in (base, fix)]
        if not entries[0] or entries[0] != entries[1] or entries[0].split()[0] not in ('100644', '100755'):
            raise PatchbenchError('Regression nodes must be unchanged regular developer test inputs on Base and Fix')


def validate_historical_case(case, *, container=True, executor=execute):
    if not container:
        raise PatchbenchError('Historical PR-test verification requires the offline container')
    meta = read_metadata(case.case_dir)
    settings = meta['historical_pr_tests']
    manifest, policy = historical_policy(meta['repository'], case.case_dir, settings.get('profile'))
    provenance = json.loads((case.case_dir/'provenance.json').read_text())
    check_provenance(manifest, provenance)
    if (provenance.get('base_commit') != case.base_commit or provenance.get('human_commit') != case.fix_commit):
        raise PatchbenchError('Historical provenance/commit mismatch')
    prod, tests, production, test_side, _ = historical_patches(case.repo_path, case.base_commit, case.fix_commit, policy)
    test_files = [p for p in test_side if developer_test(p, policy) and git(case.repo_path, 'ls-tree', case.fix_commit, '--', p).strip()]
    if (case.gold_patch.read_text() != prod or case.test_patch.read_text() != tests
            or settings['production_files'] != production or settings['developer_test_files'] != test_files
            or settings.get('test_side_files', settings['developer_test_files']) != test_side
            or hashlib.sha256(prod.encode()).hexdigest() != settings['gold_sha256']
            or hashlib.sha256(tests.encode()).hexdigest() != settings['test_sha256']):
        raise PatchbenchError('Historical patches must equal the exact developer Git deltas')
    nodes = settings['test_nodes']
    regressions = settings.get('regression_test_nodes', [])
    check_regression_nodes(case.repo_path, case.base_commit, case.fix_commit, regressions, policy)
    command_nodes = [*nodes, *regressions]
    modern = settings.get('command_policy_version', 1) >= 2
    if not nodes or any(n.split('::')[0] not in test_files for n in nodes):
        raise PatchbenchError('Historical evaluator policy changed')
    cache = resolve_dependency_cache(case.case_dir, settings)
    with temporary_checkout(case.repo_path, case.fix_commit) as reference:
        if modern and policy.get('framework') == 'maven':
            expected, selection = maven_selection(reference, command_nodes, production, policy)
            workdir = None
            if any(settings['selection'].get(k) != v for k, v in selection.items()):
                raise PatchbenchError('Historical Maven selection changed')
        else:
            if modern:
                policy = targeted_pytest_policy(policy, reference, command_nodes)
            expected, workdir = historical_command(policy, command_nodes)
        if modern and (settings.get('policy_digest') != policy_digest(policy)
                or settings.get('configuration_digests') != configuration_digests(reference)
                or settings.get('dependency_cache_digest') != cache_identity(cache, reference)
                or meta.get('dependency_cache') != settings.get('dependency_cache')):
            raise PatchbenchError('Historical environment fingerprint changed')
        if (case.test_command != expected or case.image != settings['image'] or case.setup_commands
                or case.env != policy.get('env', {}) or case.workdir != workdir):
            raise PatchbenchError('Historical evaluator policy changed')
        oracle_inputs = sorted(set(test_side) | {node.split('::')[0] for node in regressions})
        reference_tests = {p:file_identity(reference, p) for p in oracle_inputs}
        reference_production = {p:file_identity(reference, p) for p in production}
        if modern and settings.get('image_identity'):
            if image_identity(case.image) != settings['image_identity']:
                raise PatchbenchError('Historical container image identity changed')
        if modern and executor is execute and not settings.get('image_identity'):
            raise PatchbenchError('Historical validation requires an available pinned container image; prepare again after building the profile')
    environment = dict(image=settings.get('image_identity') or case.image, setup_commands=[],
                       test_command=expected, timeout_seconds=case.timeout_seconds,
                       dependency_cache=cache, env=case.env)
    if workdir:
        environment['workdir'] = workdir
    session = case.case_dir/'.patchbench/historical-pr-tests'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
    session.mkdir(parents=True)
    evidence = dict(construction_mode=MODE, base_sha=case.base_commit, fix_sha=case.fix_commit,
                    developer_test_files=test_files, production_files=production, test_nodes=nodes,
                    test_side_files=test_side, regression_test_nodes=regressions, selection=settings.get('selection'),
                    environment={k:settings.get(k) for k in ('image_identity', 'profile', 'policy_digest',
                        'configuration_digests', 'dependency_cache_digest')},
                    image=case.image, verification_network='none', validated=False)
    def execute_state(state, revision, gold=False):
        with temporary_checkout(case.repo_path, revision) as checkout:
            if revision == case.base_commit:
                applied, error = apply_patch(checkout, case.test_patch)
                if not applied:
                    return None, 'developer_test_patch_not_applicable: ' + str(error)
                if {p:file_identity(checkout, p) for p in oracle_inputs} != reference_tests:
                    return None, 'developer_tests_differ_from_historical_fix'
                if gold:
                    applied, error = apply_patch(checkout, case.gold_patch)
                    if not applied:
                        return None, 'historical_production_fix_not_applicable: ' + str(error)
                    if {p:file_identity(checkout, p) for p in production} != reference_production:
                        return None, 'production_fix_differs_from_historical_fix'
                    if {p:file_identity(checkout, p) for p in oracle_inputs} != reference_tests:
                        return None, 'gold_changed_developer_tests'
            inputs = {p:file_identity(checkout, p) for p in [*oracle_inputs, *production]}
            result = executor(checkout, environment, session/state)
            if {p:file_identity(checkout, p) for p in inputs} != inputs:
                result['report']['invalid_reports'].append('historical_evaluator_modified_source_or_oracle_inputs')
            return result, None
    reference_run, reason = execute_state('reference', case.fix_commit)
    evidence['reference'] = reference_run
    if settings.get('baseline_record') and reference_run is not None:
        baseline = historical_baseline(case)
        if reference_run['report']['tests'] != baseline['reference_tests']:
            raise PatchbenchError('Historical test inventory differs from the frozen validation baseline')
    from patchbench.mirroring.reports import execution_reason
    evidence['reference_failure_reason'] = execution_reason(reference_run)
    applied = gold_applied = False
    base_run = gold_run = None
    if green(reference_run):
        required = settings.get('selection', {}).get('required_test_classes', [])
        executed = [key for key, value in reference_run['report']['tests'].items() if value['status'] != 'SKIP']
        if any(not any(('::' + cls + '::') in ('::' + key) for key in executed) for cls in required):
            reason = 'developer_regression_tests_not_executed'
        else:
            base_run, reason = execute_state('base', case.base_commit)
            applied = base_run is not None
            if base_run is not None:
                evidence['base'] = base_run
                delta = differential(reference_run, base_run)
                evidence.update(delta)
                reason = delta['reason']
                if modern and reason == 'valid_f2p' and not delta['p2p_tests']:
                    reason = 'no_passing_regression_tests'
                if modern and reason != 'valid_f2p':
                    # A green fixed reference already distinguishes environment
                    # support from Base API/coverage defects. Repeats and Gold
                    # cannot turn an infrastructure or no-F2P result into a case.
                    evidence.update(reason=reason, historical_environment_works=True)
                    write_json(session/'evidence.json', evidence)
                    write_json(case.case_dir/'.patchbench/historical-pr-tests/latest.json', evidence)
                    return ValidationResult(case.id, case.base_commit, applied, base_run['exit_code'],
                        green(base_run), False, None, False, False, reason)
                repeat, repeat_error = execute_state('base_repeat', case.base_commit)
                evidence['base_repeat'] = repeat
                if repeat is None or delta != differential(reference_run, repeat) or base_run['report']['tests'] != repeat['report']['tests']:
                    reason = 'unstable_oracle'
                # Gold evidence helps distinguish environment failures, even for rejected Base runs.
                gold_run, error = execute_state('gold', case.base_commit, gold=True)
                gold_applied = gold_run is not None
                if gold_run is not None:
                    evidence['gold'] = gold_run
                if error:
                    reason = error
                elif reason == 'valid_f2p':
                    if not green(gold_run) or gold_run['report']['tests'] != reference_run['report']['tests']:
                        reason = 'gold_did_not_pass'
                    elif modern:
                        gold_repeat, error = execute_state('gold_repeat', case.base_commit, gold=True)
                        evidence['gold_repeat'] = gold_repeat
                        if error or not green(gold_repeat) or gold_repeat['report']['tests'] != gold_run['report']['tests']:
                            reason = 'unstable_gold'
                        else:
                            evidence['validated'] = True
                    else:
                        evidence['validated'] = True
    else:
        reason = 'baseline_not_green'
    evidence['reason'] = reason
    evidence['historical_environment_works'] = green(reference_run)
    write_json(session/'evidence.json', evidence)
    write_json(case.case_dir/'.patchbench/historical-pr-tests/latest.json', evidence)
    return ValidationResult(case.id, case.base_commit, applied,
        base_run['exit_code'] if base_run else None, bool(base_run and green(base_run)), gold_applied,
        gold_run['exit_code'] if gold_run else None, bool(gold_run and green(gold_run)),
        evidence['validated'], None if evidence['validated'] else reason)


def evaluate_historical_patch(case, patch_path, run_dir, *, executor=execute):
    """Use the construction environment and frozen oracle for solver scoring."""
    from patchbench.commands import _PatchEvaluation
    from patchbench.gitutils import diff_stats
    settings = read_metadata(case.case_dir)['historical_pr_tests']
    baseline = historical_baseline(case)
    _, policy = historical_policy(read_metadata(case.case_dir)['repository'], case.case_dir, settings.get('profile'))
    if image_identity(case.image) != settings.get('image_identity'):
        raise PatchbenchError('Historical container image identity changed')
    cache = resolve_dependency_cache(case.case_dir, settings)
    environment = dict(image=settings['image_identity'], dependency_cache=cache,
        test_command=case.test_command, timeout_seconds=case.timeout_seconds,
        setup_commands=[], env=case.env, workdir=case.workdir)
    with temporary_checkout(case.repo_path, case.base_commit) as checkout:
        if cache_identity(cache, checkout) != settings.get('dependency_cache_digest'):
            raise PatchbenchError('Historical dependency cache identity changed')
        applied, error = apply_patch(checkout, patch_path)
        if not applied:
            return _PatchEvaluation(False, False, 'Failed to apply agent patch: ' + str(error), [], 0, 0)
        files, added, removed = diff_stats(checkout)
        def allowed(name):
            if not safe_path(name) or test_side_file(name, policy):
                return False
            if policy.get('framework') == 'maven':
                return any(root in '/' + name for root in ('/src/main/java/', '/src/main/resources/'))
            return (name.startswith(tuple(policy.get('production_roots', ['lib/rucio/'])))
                and name.endswith(tuple(policy.get('production_extensions', ['.py'])))
                and not {'test', 'tests'} & set(PurePosixPath(name).parts))
        if any(not allowed(name) or (checkout / name).is_symlink() for name in files):
            return _PatchEvaluation(True, False, 'Agent patch changes protected test or environment inputs', files, added, removed)
        if hashlib.sha256(case.test_patch.read_bytes()).hexdigest() != settings['test_sha256']:
            raise PatchbenchError('Historical oracle identity changed')
        applied, error = apply_patch(checkout, case.test_patch)
        if not applied:
            return _PatchEvaluation(True, False, 'Failed to apply hidden developer tests: ' + str(error), files, added, removed)
        tracked = {p:file_identity(checkout, p) for p in set(files) | set(settings.get('test_side_files', []))}
        result = executor(checkout, environment, run_dir / 'historical-evaluation')
        if {p:file_identity(checkout, p) for p in tracked} != tracked:
            result['report']['invalid_reports'].append('evaluator_modified_source_or_oracle_inputs')
        resolved = green(result) and result['report']['tests'] == baseline['reference_tests']
        (run_dir / 'test.log').write_text((run_dir / 'historical-evaluation/execution.log').read_text())
        return _PatchEvaluation(True, resolved, None if resolved else 'Historical behavioral or regression tests failed', files, added, removed)
