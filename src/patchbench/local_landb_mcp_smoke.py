"""Optional service integration is deliberately absent from this distribution."""
from patchbench.exceptions import PatchbenchError


def unavailable(*args, **kwargs):
    raise PatchbenchError(
        "This optional service integration is not distributed in the public coding "
        "profile. Use coding cases or implement a separately reviewed adapter."
    )


run_patchbench_dev_smoke_test = unavailable
run_patchbench_smoke_test = unavailable
run_answer_benchmark = unavailable
validate_answer_case = unavailable
verify_live_dev_case = unavailable
