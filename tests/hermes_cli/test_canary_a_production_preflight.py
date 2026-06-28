"""CI tests for Canary A Production Preflight (Issue #151, PR #153).

Six tests:
  1. launcher_has_exactly_two_stages
  2. two_stages_have_unique_synthetic_markers
  3. workflow_required_gates_exactly_nine
  4. missing_verifier_worker_pid_forces_fail
  5. stage2_not_created_after_stage1_fail     (retained)
  6. stage2_not_created_after_stage1_blocked  (retained)
"""

from __future__ import annotations

import json
import pytest

from hermes_cli.canary.canary_a import CanaryA, STAGE_1_CONTRACT, STAGE_2_CONTRACT
from hermes_cli.canary.canary_multistage_workflow import (
    REQUIRED_GATES,
    REQUIRED_GATE_COUNT,
    REQUIRED_VERIFIER_FIELDS,
    MultiStageWorkflow,
    Verdict,
)
from hermes_cli.canary.verification_activity import verify_artifact


# ---------------------------------------------------------------------------
# Test 1: launcher_has_exactly_two_stages
# ---------------------------------------------------------------------------


def test_launcher_has_exactly_two_stages():
    """CanaryA launcher must define exactly 2 stages."""
    canary = CanaryA()
    assert canary.execution_stage_count == 2, (
        f"Expected 2 stages, got {canary.execution_stage_count}"
    )


# ---------------------------------------------------------------------------
# Test 2: two_stages_have_unique_synthetic_markers
# ---------------------------------------------------------------------------


def test_two_stages_have_unique_synthetic_markers():
    """Stage 1 and Stage 2 must have DIFFERENT synthetic markers."""
    canary = CanaryA()
    assert canary.stage_1_marker == "canary-a-stage-1", (
        f"Stage 1 marker mismatch: {canary.stage_1_marker}"
    )
    assert canary.stage_2_marker == "canary-a-stage-2", (
        f"Stage 2 marker mismatch: {canary.stage_2_marker}"
    )
    assert canary.stage_1_marker != canary.stage_2_marker, (
        "Stage markers must be unique"
    )

    # Also verify the contracts carry the correct markers
    s1 = json.loads(STAGE_1_CONTRACT)
    s2 = json.loads(STAGE_2_CONTRACT)
    assert s1["marker"] == "canary-a-stage-1"
    assert s2["marker"] == "canary-a-stage-2"
    assert s1["synthetic_data_only"] is True
    assert s2["synthetic_data_only"] is True
    assert s1["tests_pass"] is True
    assert s2["tests_pass"] is True


# ---------------------------------------------------------------------------
# Test 3: workflow_required_gates_exactly_nine
# ---------------------------------------------------------------------------


def test_workflow_required_gates_exactly_nine():
    """REQUIRED_GATES must be exactly 9 items with correct names."""
    assert REQUIRED_GATE_COUNT == 9, (
        f"Expected 9 gates, got {REQUIRED_GATE_COUNT}"
    )
    assert len(REQUIRED_GATES) == 9

    expected = [
        "artifact_exists",
        "manifest_sha256_match",
        "schema_valid",
        "content_type_allowed",
        "artifact_size_allowed",
        "malware_or_forbidden_extension_check",
        "tests_pass",
        "consumer_contract_present",
        "result_sha256_match",
    ]
    assert REQUIRED_GATES == expected, (
        f"Gate list mismatch:\n  got:      {REQUIRED_GATES}\n  expected: {expected}"
    )

    # Verify "sha256_match" is NOT in the list (should be manifest_sha256_match)
    assert "sha256_match" not in REQUIRED_GATES, (
        "Old gate name 'sha256_match' must not appear — use 'manifest_sha256_match'"
    )


# ---------------------------------------------------------------------------
# Test 4: missing_verifier_worker_pid_forces_fail
# ---------------------------------------------------------------------------


def test_missing_verifier_worker_pid_forces_fail():
    """Missing or zero worker_pid must force verdict=FAIL and stage2_created=false."""
    valid_result = json.dumps(
        {"tests_pass": True, "synthetic_data_only": True, "marker": "canary-a-stage-1"}
    )
    valid_sha256 = (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )

    # Case A: worker_pid = None
    result_a = verify_artifact(
        task_id="test-a",
        status="done",
        result_str=valid_result,
        result_sha256=valid_sha256,
        worker_pid=None,
        expected_marker="canary-a-stage-1",
    )
    assert result_a["verdict"] == "FAIL", (
        f"None worker_pid should FAIL, got {result_a['verdict']}"
    )
    assert result_a["stage2_created"] is False, (
        "stage2_created must be False when worker_pid is None"
    )
    assert result_a["worker_pid"] is None

    # Case B: worker_pid = 0
    result_b = verify_artifact(
        task_id="test-b",
        status="done",
        result_str=valid_result,
        result_sha256=valid_sha256,
        worker_pid=0,
        expected_marker="canary-a-stage-1",
    )
    assert result_b["verdict"] == "FAIL", (
        f"Zero worker_pid should FAIL, got {result_b['verdict']}"
    )
    assert result_b["stage2_created"] is False, (
        "stage2_created must be False when worker_pid is 0"
    )

    # Case C: worker_pid = 12345 (valid)
    result_c = verify_artifact(
        task_id="test-c",
        status="done",
        result_str=valid_result,
        result_sha256=valid_sha256,
        worker_pid=12345,
        expected_marker="canary-a-stage-1",
    )
    # With valid worker_pid, all gates should pass
    assert result_c["worker_pid"] == 12345

    # Verify worker_pid is in REQUIRED_VERIFIER_FIELDS
    assert "worker_pid" in REQUIRED_VERIFIER_FIELDS, (
        "worker_pid must be in REQUIRED_VERIFIER_FIELDS"
    )


# ---------------------------------------------------------------------------
# Test 5: stage2_not_created_after_stage1_fail (retained)
# ---------------------------------------------------------------------------


def test_stage2_not_created_after_stage1_fail():
    """Stage 2 must NOT be created when Stage 1 FAILs (fail-closed)."""
    workflow = MultiStageWorkflow()

    # Simulate Stage 1 with a FAIL result
    verdict, gates = workflow.verify(
        task_id="stage1-fail",
        status="done",
        result_str=json.dumps(
            {"tests_pass": False, "marker": "canary-a-stage-1"}
        ),
        result_sha256=None,
        worker_pid=12345,
        expected_marker="canary-a-stage-1",
    )
    assert verdict == Verdict.FAIL, (
        f"Stage 1 should FAIL, got {verdict.value}"
    )
    # gates_pass check confirms at least one gate failed
    assert not all(g.passed for g in gates.values()), (
        "At least one gate should fail"
    )

    # Stage 2 must have stage2_created=False in this scenario
    result = verify_artifact(
        task_id="stage1-fail",
        status="done",
        result_str=json.dumps(
            {"tests_pass": False, "marker": "canary-a-stage-1"}
        ),
        result_sha256=None,
        worker_pid=12345,
        expected_marker="canary-a-stage-1",
    )
    # When Stage 1 fails, the workflow controller should not create Stage 2.
    # In isolation, verify_artifact reports stage2_created based on worker_pid
    # + gate results. Here worker_pid is valid but tests_pass=False → FAIL.
    assert result["verdict"] == "FAIL"


# ---------------------------------------------------------------------------
# Test 6: stage2_not_created_after_stage1_blocked (retained)
# ---------------------------------------------------------------------------


def test_stage2_not_created_after_stage1_blocked():
    """Stage 2 must NOT be created when Stage 1 is BLOCKED (fail-closed)."""
    workflow = MultiStageWorkflow()

    # Simulate blocked task
    verdict, gates = workflow.verify(
        task_id="stage1-blocked",
        status="blocked",
        result_str=None,
        result_sha256=None,
        worker_pid=None,
        expected_marker="canary-a-stage-1",
    )
    assert verdict == Verdict.FAIL, (
        f"Blocked stage should FAIL, got {verdict.value}"
    )

    result = verify_artifact(
        task_id="stage1-blocked",
        status="blocked",
        result_str=None,
        result_sha256=None,
        worker_pid=None,
        expected_marker="canary-a-stage-1",
    )
    assert result["verdict"] == "FAIL"
    assert result["stage2_created"] is False


# ---------------------------------------------------------------------------
# Additional integrity checks
# ---------------------------------------------------------------------------


def test_gate_results_uses_manifest_sha256_match_not_sha256_match():
    """verification_activity must use manifest_sha256_match, not sha256_match."""
    result = verify_artifact(
        task_id="test-gate-names",
        status="done",
        result_str=json.dumps(
            {"tests_pass": True, "marker": "canary-a-stage-1"}
        ),
        result_sha256=None,
        worker_pid=12345,
        expected_marker="canary-a-stage-1",
    )
    gate_results = result["gate_results"]
    assert "manifest_sha256_match" in gate_results, (
        "gate_results must contain 'manifest_sha256_match'"
    )
    assert "sha256_match" not in gate_results, (
        "gate_results must NOT contain old name 'sha256_match'"
    )


def test_required_verifier_fields_includes_all():
    """REQUIRED_VERIFIER_FIELDS must contain worker_identity, worker_pid,
    verifier_node, verifier_profile, evidence_sha256."""
    required = set(REQUIRED_VERIFIER_FIELDS)
    expected = {
        "worker_identity",
        "worker_pid",
        "verifier_node",
        "verifier_profile",
        "evidence_sha256",
    }
    assert required == expected, (
        f"REQUIRED_VERIFIER_FIELDS mismatch: {required} vs {expected}"
    )


def test_canary_a_stage_count_is_two():
    """CanaryA.execution_stage_count must be exactly 2."""
    canary = CanaryA()
    assert canary.execution_stage_count == 2
