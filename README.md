# PatchBench

Construct executable coding tasks from repository history, benchmark agents on
frozen inputs, and inspect patches, test outcomes and execution evidence.

This public profile contains the reusable coding workflow and Research Console.
It runs independently of the private upstream research corpus. Demonstration data
is invented and clearly labeled synthetic; it does not represent model performance.
See [publication status](PUBLICATION.md) before redistribution.

## Install

Use Python 3.9+, Git, and uv. Work from a source checkout so repository manifests
and image recipes remain available.

```bash
uv sync --frozen --extra dev
. .venv/bin/activate
patchbench --help
```

Node 22.13+ on the 22.x line, or Node 24+, is required for the optional frontend:

```bash
cd frontend
npm ci
npm run dev
```

The console opens on loopback port 5180. Its default data comes exclusively from
`frontend/demo-data`; it never loads an upstream research snapshot. `npm run build`
builds the static viewer after `npm run export-data` prepares its data.

## Exercise the complete evaluator without a model

From the project root, choose a new destination:

```bash
python -m patchbench.demo /tmp/patchbench-demo
patchbench validate-case /tmp/patchbench-demo/cases/demo-label
patchbench run /tmp/patchbench-demo/cases/demo-label --agent gold --run-id gold-r1 --no-live
patchbench report /tmp/patchbench-demo/artifacts/demo-label/runs/gold-r1
```

Validation should succeed and the gold control should report `resolved=True`.
Gold supplies the synthetic reference repair and measures evaluator behavior.
A negative control intentionally exits with status 1:

```bash
patchbench run /tmp/patchbench-demo/cases/demo-label --agent dummy --run-id dummy-r1 --no-live
```

The generated test remains evaluator-only during solving. Recreate the demo at a
new destination or choose a fresh run ID for each repetition.

## Construct your own cases

The configured public GitHub acquisition source is `rucio/rucio`. Rootless Podman
is required for its offline evaluator. GitHub authentication uses `GITHUB_TOKEN`
or `GH_TOKEN` from your external environment when supplied.

```bash
export PATCHBENCH_RUCIO_REPO="$HOME/patchbench-sources/rucio"
patchbench build-env rucio
podman build -f images/rucio-historical-tests.Containerfile -t localhost/patchbench-rucio-historical:tests-v2 .
podman build -f images/rucio-claude-code.Containerfile -t localhost/patchbench-rucio-claude:tests-v2 .
patchbench pull-cases rucio --limit 10 --dry-run
patchbench pull-cases rucio --limit 10
```

Review the acquired issues and suggested construction options under
`artifacts/imports/rucio/`. For a selected change with suitable developer tests:

```bash
patchbench prepare-historical-pr-tests drafts/rucio/YOUR_CASE_ID
patchbench validate-case drafts/rucio/YOUR_CASE_ID --container
patchbench promote-draft rucio YOUR_CASE_ID --container
```

Replace the case ID. Construction splits exact developer tests from the repair;
validation must show behavioral failure before repair and passing tests afterwards.
A draft that fails validation remains excluded. Select test scope before inspecting
failures. Existing-test mirroring is also supported for configured components:
prepare a green reference with `prepare-environment`, then use `mirror-draft`.

For larger collections, `pull-cases rucio --harvest --session NAME` schedules
construction under time/cost budgets. Build runtimes first. Omit model profiles and
use `--model-budget-usd 0` for deterministic construction. Review accepted drafts
before promotion. No single candidate or yield is guaranteed.

GitLab acquisition can use an explicitly configured server. Additional repositories
need manifests, compatible runtimes and construction support; GitHub intake is
currently limited to Rucio. Service-specific answer integrations are omitted from
this public coding profile and return an explicit unavailable error.

## Benchmark an agent

After promoting your cases, configure a compatible model provider in a literal
export file outside this project. Never commit credentials.

```bash
patchbench run-all --cases-dir cases --case-type coding --agent claude-code \
  --agent-model YOUR_MODEL_ID --agent-env-file /secure/path/provider-env.sh \
  --agent-max-turns 100 --agent-timeout-seconds 900 --container
```

The adapter name identifies the harness; it does not establish the underlying model.
Use matching evaluator/solver images. Keep case selection, prompt mode, model and
budgets fixed between conditions. `issue_only` is the default prompt mode.

Each attempt retains a unique run ID and writes evidence under `artifacts/`.
Batch summaries select that batch's outcomes. Scope reports to the intended runs;
exclude controls and do not silently select successful retries.

To inspect your own data, set `PATCHBENCH_CASES_DIR` and optionally
`PATCHBENCH_RUNS_DIR` before `npm run dev`. A local execution service is opt-in:
`patchbench serve --dev`. Keep it on loopback; it is not a public authenticated
execution service. Static hosting should serve only the reviewed viewer build.

## Architecture and reproducibility

Case metadata describes frozen source revisions, commands and evaluator inputs.
Repository manifests describe runtimes and source acquisition. Construction,
solver execution, evaluation, and evidence export have separate responsibilities.
Solvers receive the approved issue and bug checkout; evaluator tests and reference
repairs remain separate. Functional scoring uses executable tests, not an LLM judge.

Retain case inputs, source objects, image identities, lockfiles, caches, commands,
model/harness versions, budgets and selected result records for replay. A fixed
patch can be re-evaluated deterministically in its frozen environment. Fresh model
runs can differ. Unit tests do not prove complete behavioral correctness.

## Checks and contributions

```bash
python -m pytest -q
cd frontend
npm run lint
npm run typecheck
npm run test
npm run export-data
npm run build
```

Public contributions are reviewed downstream, then manually integrated into the
private upstream source of truth. Do not request or include private cases, source,
credentials, logs or experimental evidence. Upstream maintainers validate the
shared change and produce a new reviewed export; repositories are never mirrored.

## License

Apache License 2.0, with the CERN privileges-and-immunities statement. See
[LICENSE.txt](LICENSE.txt) and [publication status](PUBLICATION.md).
