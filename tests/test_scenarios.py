import pytest

from resolve.evals.harness import run_all
from resolve.evals.synth import run_synthetic


@pytest.mark.asyncio
async def test_scenario_suite_all_pass():
    r = await run_all()
    failed = [x["key"] for x in r["rows"] if not x["passed"]]
    assert not failed, failed
    assert r["unsafe_actions"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("noise", [0.0, 0.5])
async def test_synthetic_invariants(noise):
    r = await run_synthetic(150, seed=3, noise=noise)
    assert r["invariant_violations"] == 0 and r["overreach_cases"] == 0
    assert r["decision_accuracy"] > 0.9
