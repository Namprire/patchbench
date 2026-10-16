"""Bounded repository-local test conventions, collected exclusively from BASE."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
import xml.etree.ElementTree as ET

from patchbench.runner import redact_secrets

MAX_FILES = 10
MAX_SOURCE_CHARS = 24000


def discover_test_patterns(checkout: Path, environment: dict) -> dict:
    checkout = checkout.resolve()
    relevant = environment.get('relevant_base_files', [])
    module = next((p.split('/src/')[0] for p in relevant if '/src/' in p), '.')
    root = checkout / module
    result = dict(schema_version=1, module=module, files=[], junit=[], surefire=[],
                  naming_examples=[], max_files=MAX_FILES, max_source_chars=MAX_SOURCE_CHARS)
    if environment['build_system'] != 'maven':
        component = environment.get('python_component') or '.'
        test_roots = [checkout / component / name for name in ('tests', 'test')]
        tests = sorted({p for directory in test_roots for p in directory.rglob('test_*.py')},
            key=lambda p:(-sum(Path(f).stem.removeprefix('test_') in p.stem for f in relevant), str(p)))[:3]
        fixtures = [p for directory in test_roots for p in directory.rglob('conftest.py')]
        fixtures += [p for directory in test_roots for p in directory.glob('base_test*.py')]
        remaining = MAX_SOURCE_CHARS
        for path in [*tests, *sorted(set(fixtures))][:MAX_FILES]:
            if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(checkout):
                excerpt = redact_secrets(path.read_text(errors='replace')[:min(4000, remaining)])
                result['files'].append(dict(path=path.relative_to(checkout).as_posix(),
                    kind='example' if path in tests else 'fixture', source=excerpt))
                remaining -= len(excerpt)
                if remaining <= 0:
                    break
        result['supplied_files'] = [f['path'] for f in result['files']]
        return result
    test_root = root / 'src/test/java'
    index = {}
    for path in sorted(test_root.rglob('*.java')):
        if path.is_symlink() or not path.resolve().is_relative_to(checkout):
            continue
        text = path.read_text(encoding='utf-8', errors='replace')
        package = re.search(r'(?m)^\s*package\s+([\w.]+)\s*;', text)
        index[path.stem] = (path, text, package.group(1) if package else '')
    selected = [p for p in relevant if '/src/test/' in p and p.endswith('Test.java') and 'IntegrationTest' not in p][:3]
    if not selected:
        selected = [value[0].relative_to(checkout).as_posix() for key, value in index.items()
                    if key.endswith('Test')][:3]
    queue = [(p, 'example') for p in selected]
    seen, remaining = set(), MAX_SOURCE_CHARS
    while queue and len(result['files']) < MAX_FILES and remaining > 0:
        name, kind = queue.pop(0)
        path = checkout / name
        if name in seen or path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(checkout):
            continue
        seen.add(name)
        text = path.read_text(encoding='utf-8', errors='replace')
        package = re.search(r'(?m)^\s*package\s+([\w.]+)\s*;', text)
        imports = re.findall(r'(?m)^\s*import\s+(?:static\s+)?([\w.*]+)\s*;', text)
        superclass = re.search(r'\bclass\s+\w+(?:\s*<[\s\S]*?>)?\s+extends\s+(\w+)', text)
        limit = min(remaining, 5000 if kind == 'example' else 3000)
        excerpt = redact_secrets(text[:limit])
        result['files'].append(dict(path=name, kind=kind, package=package.group(1) if package else '',
            class_name=path.stem, superclass=superclass.group(1) if superclass else None,
            imports=imports, source=excerpt, truncated=len(text) > limit,
            sha256=hashlib.sha256(text.encode()).hexdigest()))
        remaining -= len(excerpt)
        if kind == 'example':
            result['naming_examples'].append(path.stem)
        # Resolve base classes by declarations in this same module, never by guessing packages.
        references = ([superclass.group(1)] if superclass else [])
        references += [i.rsplit('.', 1)[-1] for i in imports if not i.endswith('*')]
        for ref in references:
            if ref in index:
                entry = (index[ref][0].relative_to(checkout).as_posix(),
                         'base_class' if superclass and ref == superclass.group(1) else 'fixture')
                if entry[1] == 'base_class':
                    queue.insert(0, entry)
                else:
                    queue.append(entry)
    for pom in (checkout / 'pom.xml', root / 'pom.xml'):
        if not pom.is_file() or pom.is_symlink() or not pom.resolve().is_relative_to(checkout):
            continue
        xml = ET.parse(pom)
        for dependency in xml.findall('.//{*}dependency'):
            artifact = dependency.findtext('{*}artifactId') or ''
            if 'junit' in artifact:
                result['junit'].append(dict(path=pom.relative_to(checkout).as_posix(), artifact=artifact,
                    version=dependency.findtext('{*}version'), scope=dependency.findtext('{*}scope')))
        for plugin in xml.findall('.//{*}plugin'):
            if plugin.findtext('{*}artifactId') == 'maven-surefire-plugin':
                result['surefire'].append(dict(path=pom.relative_to(checkout).as_posix(),
                    version=plugin.findtext('{*}version'), configuration=redact_secrets(
                    ET.tostring(plugin, encoding='unicode')[:3000])))
    result['junit_annotations'] = sorted({imp for item in result['files'] for imp in item.get('imports', []) if imp.startswith('org.junit.')})
    result['supplied_files'] = [f['path'] for f in result['files']]
    return result
