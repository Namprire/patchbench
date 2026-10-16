"""Base-only deterministic discovery. Commands come from supported build systems."""

from __future__ import annotations

from pathlib import Path
import re
import hashlib
import shlex
import xml.etree.ElementTree as ET

from patchbench.exceptions import PatchbenchError
from patchbench.runner import sanitize_command


def discover_environment(checkout: Path, manifest, case, problem_title: str = "") -> dict:
    files = sorted(p.relative_to(checkout).as_posix() for p in checkout.rglob('*')
                   if p.is_file() and '.git' not in p.relative_to(checkout).parts)
    inspected = [f for f in files if Path(f).name in {
        '.gitlab-ci.yml', 'pom.xml', 'build.gradle', 'build.gradle.kts', 'Makefile',
        'package.json', 'pyproject.toml', 'pytest.ini', 'tox.ini', 'noxfile.py',
        'README.md', 'CONTRIBUTING.md'}]
    inspected.sort(key=lambda f: next((i for i, name in enumerate(('.gitlab-ci.yml', 'pom.xml', 'build.gradle', 'build.gradle.kts', 'Makefile', 'package.json', 'pyproject.toml', 'pytest.ini', 'tox.ini', 'noxfile.py', 'README.md', 'CONTRIBUTING.md')) if Path(f).name == name), 99))
    python_packages = sorted({str(Path(f).parent) for f in files if Path(f).name in
        ('setup.py', 'pyproject.toml', 'pytest.ini')})
    system = 'maven' if 'pom.xml' in files else ('pytest' if python_packages else 'unsupported')
    if system == 'unsupported':
        system = next((name for name, markers in [('gradle', ('build.gradle', 'build.gradle.kts')), ('make', ('Makefile',)), ('npm', ('package.json',))] if any(m in files for m in markers)), 'unknown')
    java = None
    if system == 'maven':
        xml = ET.parse(checkout / 'pom.xml')
        for key in ('maven.compiler.release', 'java.version', 'maven.compiler.target'):
            value = xml.findtext(f'.//{{*}}{key}')
            if value and re.fullmatch(r'(?:1\.)?\d+', value):
                java = value.removeprefix('1.')
                break
    fingerprints = {f: hashlib.sha256((checkout / f).read_bytes()).hexdigest() for f in inspected}
    tokens = set(re.findall(r'[a-z]{3,}', problem_title.lower())) - {
        'the', 'and', 'for', 'with', 'when', 'one', 'same', 'name', 'was', 'recently',
        'prevent', 'creating', 'deleted', 'should', 'only', 'allow', 'from', 'that'}
    ranked = sorted((f for f in files if f.endswith(('.java', '.py', '.js', '.ts'))),
                    key=lambda f: (-sum(token in Path(f).stem.lower() for token in tokens),
                                   not any(x in Path(f).stem for x in ('Validator', 'ServiceTest')), f))
    relevant = [f for f in ranked if any(t in Path(f).stem.lower() for t in tokens)][:24]
    python_component = None
    if system == 'pytest':
        scores = {p:sum(f.startswith(p + '/') for f in relevant) for p in python_packages}
        declared = case.workdir or '.'
        python_component = declared if declared in scores else max(scores, key=lambda p:(scores[p], p == '.'))
        relevant = [f for f in relevant if python_component == '.' or f.startswith(python_component + '/')]
    runtime = manifest.config.get('preparation_runtimes', {}).get(java or '', {})
    environment_profile = None
    env = dict(getattr(case, 'env', {}))
    if system == 'pytest':
        for name, settings in manifest.config.get('environment_profiles', {}).items():
            if settings.get('component') != python_component:
                continue
            selectors = settings.get('source_contains', {})
            if all((checkout / path).is_file() and needle in (checkout / path).read_text()
                   for path, needle in selectors.items()):
                runtime = settings
                environment_profile = name
                env.update(manifest.config.get('historical_pr_tests', {}).get('env', {}))
                env.update(settings.get('historical_pr_tests', {}).get('env', {}))
                break
    image = runtime.get('image', case.image)
    solver_image = runtime.get('solver_image', manifest.config.get('solver_image'))
    # Candidate commands are templates; the selected command targets actual generated tests.
    return dict(schema_version=1, base_commit=case.base_commit, build_system=system,
                java_version=java, workdir=case.workdir, python_component=python_component,
                image=image, solver_image=solver_image,
                environment_profile=environment_profile, env=env,
                setup_commands=[sanitize_command(shlex.join(c)) for c in case.setup_commands], inspected_files=inspected, configuration_digests=fingerprints,
                relevant_base_files=relevant, public_test_directories=sorted({str(Path(f).parent) for f in files
                    if '/src/test/' in f or ('test' in Path(f).parts or 'tests' in Path(f).parts) and f.endswith('.py')}),
                candidate_test_commands=['mvn -B -ntp -pl <test-module> -am -Dtest=<generated-tests> test']
                if system == 'maven' else ['python -m pytest <generated-tests> --junitxml=patchbench-junit.xml']
                if system == 'pytest' else [], selected_test_command=None, attempts=[], execution_results=[])


def test_command(checkout: Path, environment: dict, files: list[str]) -> list[str]:
    if environment['build_system'] == 'maven':
        if not files or any(not re.fullmatch(r'(?:[\w.-]+/)*src/test/java/(?:[\w]+/)*[\w]+Test.java', f) for f in files):
            raise PatchbenchError('Only new *Test.java regression tests are allowed')
        modules = {f.split('/src/test/java/')[0] if '/src/test/java/' in f else '.' for f in files}
        if len(modules) != 1:
            raise PatchbenchError('Generated Maven tests must belong to one module')
        module = modules.pop()
        if not (checkout / module / 'pom.xml').is_file():
            raise PatchbenchError('Generated test module has no pom.xml')
        # Freeze the affected unit regression scope before executing the candidate.
        # Running only the newly generated class cannot demonstrate P2P preservation.
        return ['mvn', '-o', '-B', '-ntp', '-pl', module, '-am',
                '-DexcludedGroups=integration', 'test']
    if environment['build_system'] == 'pytest':
        if not files or any(not re.fullmatch(r'tests/(?:[\w]+/)*test_[\w]+.py', f) for f in files):
            raise PatchbenchError('Only new tests/test_*.py regression tests are allowed')
        component = environment.get('python_component') or '.'
        public = [str(Path(component) / name) for name in ('test', 'tests')
                  if (checkout / component / name).is_dir()]
        # New files are under tests/; avoid collecting that tree twice.
        scope = list(dict.fromkeys([*public, *[f for f in files if not any(
            f.startswith(p.rstrip('/') + '/') for p in public)]]))
        return ['python', '-B', '-m', 'pytest', *scope, '--junitxml=patchbench-junit.xml']
    raise PatchbenchError('No supported deterministic test command found')
