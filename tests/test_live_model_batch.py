"""Opt-in live acceptance: ordinary pytest never supplies a paid-call evaluator."""

import pytest


@pytest.mark.parametrize("index", range(50), ids=lambda index: f"synthetic-{index + 1:03}")
def test_live_model_batch_case(request, index):
    evaluator = getattr(request.config, "_sleepscape_live_batch", None)
    if evaluator is None:
        pytest.skip("Live model calls require an explicitly authorized task launcher")
    assert evaluator.case_count == 50, "Live acceptance requires exactly fifty synthetic cases"
    row = evaluator.run_case(index)
    assert row["business_pass"] is True, f"{row['case_id']}: {row['outcome']} ({row['error_code']})"
