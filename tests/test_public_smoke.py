"""Public checks create invented inputs; they do not load any research corpus."""
from pathlib import Path

import pytest

from patchbench.demo import create_demo
from patchbench.commands import validate_case, run_agent, evaluate_patch
from patchbench.dashboard_export import export_dashboard
from patchbench.catalog import case_output_dir
from patchbench.report import generate_report


def test_invented_case_evaluation_and_dashboard(tmp_path):
    case = create_demo(tmp_path / "invented")
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


def test_demo_does_not_overwrite(tmp_path):
    create_demo(tmp_path / "invented")
    with pytest.raises(Exception, match="must not exist"):
        create_demo(tmp_path / "invented")


def test_case_inputs_are_repeatable(tmp_path):
    first = create_demo(tmp_path / "a")
    second = create_demo(tmp_path / "b")
    assert {p.name: p.read_bytes() for p in first.iterdir()} == {
        p.name: p.read_bytes() for p in second.iterdir()
    }
