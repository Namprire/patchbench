"""Additional strict metadata checks for mirrored coding cases."""
from pathlib import Path
import re
import json
from patchbench.exceptions import PatchbenchError


def check_metadata(raw, case_dir):
    if raw.get('construction_mode') != 'mr_mirror_v1' or 'test_patch' in raw:
        raise PatchbenchError('existing_tests requires mr_mirror_v1 and no test_patch')
    historical, benchmark = raw.get('historical'), raw.get('benchmark')
    if not isinstance(historical, dict) or not isinstance(benchmark, dict):
        raise PatchbenchError('Mirror case requires historical and benchmark provenance')
    for key in ('original_base_sha', 'original_fix_sha'):
        if not isinstance(historical.get(key), str) or not re.fullmatch('[0-9a-f]{40}', historical[key]):
            raise PatchbenchError('Historical provenance requires exact commit SHAs')
    number_key = 'pr_number' if raw.get('source', {}).get('type') == 'github' else 'mr_iid'
    if type(historical.get(number_key)) is not int or historical[number_key] <= 0:
        raise PatchbenchError('Historical provenance requires MR IID')
    if not isinstance(benchmark.get('reference_sha'), str) or not re.fullmatch('[0-9a-f]{40}', benchmark['reference_sha']):
        raise PatchbenchError('Benchmark requires exact reference SHA')
    if benchmark.get('oracle_mode') != raw.get('oracle_mode') or raw.get('oracle_mode') not in {'existing_tests', 'existing_tests_with_regression'} or benchmark.get('mirror_method') not in ('deterministic_reverse', 'agent'):
        raise PatchbenchError('Invalid mirror oracle/method')
    for key in ('mirror_files', 'f2p_tests', 'p2p_tests'):
        values = benchmark.get(key)
        if not isinstance(values, list) or not all(isinstance(v, str) and v for v in values) or len(values) != len(set(values)):
            raise PatchbenchError('Invalid benchmark ' + key)
    if not benchmark['f2p_tests'] or not benchmark['mirror_files']:
        raise PatchbenchError('Mirror case requires executed F2P evidence')
    if raw.get('oracle_mode') == 'existing_tests_with_regression':
        if benchmark.get('oracle_patch') != 'hidden-regression.patch':
            raise PatchbenchError('Missing hidden regression oracle')
    elif benchmark.get('oracle_patch'):
        raise PatchbenchError('Unexpected oracle in existing-tests-only case')
    for section, keys in ((historical, ('mr_patch', 'provenance')), (benchmark, ('mirror_patch', 'environment_record', 'baseline_record') + (('oracle_patch',) if benchmark.get('oracle_patch') else ()))):
        for key in keys:
            value = section.get(key)
            if not isinstance(value, str): raise PatchbenchError('Missing mirror artifact')
            path = Path(value)
            if path.is_absolute() or '..' in path.parts or '\\' in value:
                raise PatchbenchError('Unsafe mirror artifact path')
            resolved = (case_dir / path).resolve()
            if not resolved.is_relative_to(case_dir) or (case_dir / path).is_symlink() or not resolved.is_file():
                raise PatchbenchError('Missing or unsafe mirror artifact')

    source = raw.get('source', {})
    provenance = json.loads((case_dir / historical['provenance']).read_text())
    github = (source.get('type') == 'github' and source.get('github_repo') == 'rucio/rucio'
              and provenance.get('source') == 'github' and provenance.get('github_repo') == source['github_repo']
              and source.get('pull_request_number') == historical.get('pr_number')
              and provenance.get('pull_request_number') == historical.get('pr_number') and provenance.get('merged_at'))
    gitlab = (source.get('type') == 'gitlab' and source.get('project_id') and source.get('gitlab_url')
              and source.get('merge_request_iid') == historical.get('mr_iid')
              and provenance.get('merge_request_iid') == historical.get('mr_iid'))
    if (not (github or gitlab)        or provenance.get('base_commit') != historical['original_base_sha']
        or provenance.get('human_commit') != historical['original_fix_sha']
        or raw.get('fix_commit') != benchmark['reference_sha']):
        raise PatchbenchError('Incomplete or inconsistent historical/benchmark provenance')
