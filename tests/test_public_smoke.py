"""Portable checks use only original synthetic inputs, never a research corpus."""
import json
import subprocess

import pytest

from patchbench.demo import EXAMPLE_IDS, create_demo, create_suite
from patchbench.commands import validate_case, run_agent, evaluate_patch
from patchbench.agents import AGENT_REGISTRY, AgentOutput, DummyAgent
from patchbench.dashboard_export import export_dashboard
from patchbench.catalog import case_output_dir
from patchbench.exceptions import PatchbenchError
from patchbench.gitutils import temporary_checkout, apply_patch
from patchbench.case import load_case
from patchbench.report import generate_report


@pytest.mark.parametrize("case_id", EXAMPLE_IDS)
def test_synthetic_case_evaluation_and_dashboard(tmp_path, case_id):
    case = create_demo(tmp_path / "invented", case_id)
    assert validate_case(case).validated
    gold = run_agent(case, "gold", "smoke-gold")
    assert gold.resolved
    dummy = run_agent(case, "dummy", "smoke-dummy")
    assert not dummy.resolved
    assert evaluate_patch(case, case / "gold.patch").resolved
    exported = export_dashboard(cases_dir=case.parent, runs_dir=tmp_path / "absent",
                                output=tmp_path / "viewer")
    assert exported["case_count"] == 1
    assert exported["run_count"] == 2
    generate_report(case_output_dir(case) / "runs" / "smoke-gold")
    assert (case_output_dir(case) / "runs/smoke-gold/report.md").is_file()
    detail = json.loads((tmp_path / "viewer" / "cases" / f"{case_id}.json").read_text())
    assert not detail.get("human_reference_patch")
    assert "test.patch" not in json.dumps(detail.get("artifacts", {}))


@pytest.mark.parametrize("case_id", EXAMPLE_IDS)
def test_behavioral_fail_to_pass_and_required_repairs(tmp_path, case_id):
    case = load_case(create_demo(tmp_path / "invented", case_id))
    with temporary_checkout(case.repo_path, case.base_commit) as checkout:
        assert apply_patch(checkout, case.test_patch)[0]
        base = subprocess.run(["python", "-B", "check_example.py"], cwd=checkout,
                              capture_output=True, text=True)
        assert base.returncode == 1
        assert "FAIL:" in base.stderr
        assert "ERROR:" not in base.stderr
        assert apply_patch(checkout, case.gold_patch)[0]
        fixed = subprocess.run(["python", "-B", "check_example.py"], cwd=checkout,
                               capture_output=True, text=True)
        assert fixed.returncode == 0, fixed.stderr
        if case_id == "demo-ranges":
            # Either single-file repair must remain insufficient for this task.
            for name in ("ranges.py", "coverage.py"):
                subprocess.run(["git", "checkout", case.base_commit, "--", name],
                               cwd=checkout, check=True, capture_output=True)
                partial = subprocess.run(["python", "-B", "check_example.py"],
                                         cwd=checkout, capture_output=True, text=True)
                assert partial.returncode == 1
                source = subprocess.check_output(["git", "-C", str(case.repo_path),
                          "show", f"{case.fix_commit}:{name}"], text=True)
                (checkout / name).write_text(source)


@pytest.mark.parametrize("case_id", EXAMPLE_IDS)
def test_solver_sees_only_base_and_issue(tmp_path, case_id, monkeypatch):
    path = create_demo(tmp_path / "invented", case_id)
    case = load_case(path)
    inspected = []

    class InspectingAgent(DummyAgent):
        def run(self, *, solving_worktree, prompt, **kwargs):
            inspected.append(True)
            tracked = subprocess.check_output(["git", "ls-files"], cwd=solving_worktree,
                                              text=True).splitlines()
            assert not any(name.endswith(".patch") or name.startswith("check_") for name in tracked)
            assert not (solving_worktree / "check_example.py").exists()
            assert subprocess.check_output(["git", "rev-list", "--count", "HEAD"],
                                           cwd=solving_worktree, text=True).strip() == "1"
            assert subprocess.run(["git", "cat-file", "-e", case.fix_commit],
                                  cwd=solving_worktree, capture_output=True).returncode != 0
            assert not subprocess.check_output(["git", "remote"], cwd=solving_worktree)
            assert not (solving_worktree / ".git/FETCH_HEAD").exists()
            assert prompt == (path / "issue.md").read_text()
            return AgentOutput(None, "Synthetic inspection control")

    monkeypatch.setitem(AGENT_REGISTRY, "dummy", InspectingAgent)
    assert not run_agent(path, "dummy", "inspect-solver").resolved
    assert inspected == [True]


def test_suite_reproducibility_and_no_overwrite(tmp_path):
    first = create_suite(tmp_path / "a")
    second = create_suite(tmp_path / "b")
    assert tuple(case.name for case in first) == EXAMPLE_IDS
    for a, b in zip(first, second):
        assert {p.name: p.read_bytes() for p in a.iterdir()} == {
            p.name: p.read_bytes() for p in b.iterdir()}
    with pytest.raises(PatchbenchError, match="must not exist"):
        create_suite(tmp_path / "a")
    with pytest.raises(PatchbenchError, match="must not exist"):
        create_demo(tmp_path / "a")


def test_invalid_example_creates_nothing(tmp_path):
    with pytest.raises(PatchbenchError, match="Unknown synthetic example"):
        create_demo(tmp_path / "invalid", "not-a-demo")
    assert not (tmp_path / "invalid").exists()
