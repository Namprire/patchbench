"""Reject execution/setup errors; require real assertion failures and reference passes."""

from __future__ import annotations

from pathlib import Path
import re
import xml.etree.ElementTree as ET


def report_summary(directory: Path) -> dict:
    summary = dict(tests=0, failures=0, errors=0, skipped=0, test_ids=[], assertions=[], invalid_reports=[], inventory={}, executed=0)
    for path in sorted(directory.rglob('*.xml')):
        try:
            if path.is_symlink():
                summary['invalid_reports'].append(str(path.name))
                continue
            root = ET.parse(path).getroot()
            occurrences = {}
            for test in root.iter('testcase'):
                summary['tests'] += 1
                identity = (test.get('classname', ''), test.get('name', ''))
                summary['test_ids'].append(identity)
                occurrences[identity] = occurrences.get(identity, 0) + 1
                key = path.relative_to(directory).as_posix() + '::' + '::'.join(identity) + '#' + str(occurrences[identity])
                status = 'ERROR' if test.find('error') is not None else 'FAIL' if test.find('failure') is not None else 'SKIP' if test.find('skipped') is not None else 'PASS'
                summary['inventory'][key] = status
                summary['executed'] += status in ('PASS', 'FAIL')
                for kind in ('failure', 'error', 'skipped'):
                    for node in test.findall(kind):
                        summary[{'failure':'failures', 'error':'errors', 'skipped':'skipped'}[kind]] += 1
                        if kind == 'failure':
                            summary['assertions'].append(dict(test=test.get('name'),
                                type=node.get('type') or ('AssertionError' if (node.get('message') or '').startswith('assert ') else ''), message=(node.get('message') or node.text or '')[:2000]))
        except (ET.ParseError, OSError):
            summary['invalid_reports'].append(str(path.name))
    return summary


def rejection(validation, base: dict, gold: dict) -> str | None:
    if validation.base_test_exit_code == 124 or validation.gold_test_exit_code == 124:
        return 'timeout'
    if not validation.test_patch_applies:
        return 'generated_test_invalid'
    if validation.base_test_exit_code is None:
        return 'environment_failed'
    if validation.base_test_exit_code == 0:
        return 'base_did_not_fail'
    if not base['tests'] or base['errors'] or base['invalid_reports'] or not base['failures']:
        return 'base_failed_for_wrong_reason'
    if any(not any(marker in (item['type'] or item['message']) for marker in
                   ('AssertionError', 'AssertionFailedError', 'ComparisonFailure')) for item in base['assertions']):
        return 'base_failed_for_wrong_reason'
    if not validation.validated or not gold['tests'] or gold['failures'] or gold['errors'] or gold['invalid_reports']:
        return 'gold_did_not_pass'
    if base.get('inventory') is not None and gold.get('inventory') is not None:
        before, after = base['inventory'], gold['inventory']
        if (before.keys() != after.keys() or
                any((before[key] == 'SKIP') != (after[key] == 'SKIP') for key in before)):
            return 'generated_test_invalid'
    elif sorted(base['test_ids']) != sorted(gold['test_ids']) or base['skipped'] != gold['skipped']:
        return 'generated_test_invalid'
    return None


def classify_execution(exit_code: int | None, reports: dict, output: str) -> str:
    """Execution stage, not a model judgement or an alternative F2P gate."""
    if exit_code is None or exit_code == 124:
        return 'environment_failure'
    if re.search(r'COMPILATION ERROR|cannot find symbol|package [^\n]+ does not exist|incompatible types?|SyntaxError|ImportError|ModuleNotFoundError', output):
        return 'test_compile_failure'
    if re.search(r'Could not resolve|Failed to collect dependencies|Non-resolvable|PKIX|command not found|release version .+ not supported|PREPARE_FAILED|setup failed', output, re.I):
        return 'environment_failure'
    if reports['errors'] or reports['invalid_reports']:
        return 'test_runtime_failure'
    if not reports.get('executed', reports['tests'] - reports['skipped']):
        return 'test_discovery_failure'
    if reports['failures']:
        assertion_types = ('AssertionError', 'AssertionFailedError', 'ComparisonFailure')
        if exit_code != 0 and all(any(marker in (item['type'] or item['message'])
            for marker in assertion_types) for item in reports['assertions']):
            return 'behavioural_failure'
        return 'test_runtime_failure'
    return 'behavioural_pass' if exit_code == 0 else 'test_runtime_failure'
