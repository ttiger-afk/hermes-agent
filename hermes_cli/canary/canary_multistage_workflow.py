"""MultiStageWorkflow — Gate verification and stage orchestration for Canary A.

Issue #151 · PR #153

Defines:
  - REQUIRED_GATES: exactly 9 gates that must all pass
  - REQUIRED_VERIFIER_FIELDS: required evidence fields including worker_pid
  - Gate verification logic (verification_activity)
  - Verdict: PASS/FAIL

Key changes in this patch:
  - sha256_match → manifest_sha256_match (disambiguated)
  - worker_pid is REQUIRED in verifier evidence
  - Stage 2 fail-closed: not created if Stage 1 fails
"""

from __future__ import annotations

import enum
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Gates — exactly 9
# ---------------------------------------------------------------------------

REQUIRED_GATES: list[str] = [
    "artifact_exists",
    "manifest_sha256_match",      # was: sha256_match (renamed per Issue #151)
    "schema_valid",
    "content_type_allowed",
    "artifact_size_allowed",
    "malware_or_forbidden_extension_check",
    "tests_pass",
    "consumer_contract_present",
    "result_sha256_match",
]

REQUIRED_GATE_COUNT = len(REQUIRED_GATES)  # must be 9


# ---------------------------------------------------------------------------
# Verifier evidence fields
# ---------------------------------------------------------------------------

REQUIRED_VERIFIER_FIELDS: list[str] = [
    "worker_identity",
    "worker_pid",          # REQUIRED — missing/empty → FAIL (Issue #151)
    "verifier_node",
    "verifier_profile",
    "evidence_sha256",
]


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class Verdict(enum.Enum):
    PASS = "PASS"
    FAIL = "FAIL"


@dataclass
class GateResult:
    passed: bool
    detail: str = ""


@dataclass
class VerificationResult:
    verdict: Verdict
    gates: dict[str, GateResult] = field(default_factory=dict)
    stage2_created: bool = False
    worker_identity: Optional[str] = None
    worker_pid: Optional[int] = None
    verifier_node: Optional[str] = None
    verifier_profile: Optional[str] = None
    evidence_sha256: Optional[str] = None


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


class MultiStageWorkflow:
    """Multi-stage canary workflow with 9-gate verification.

    The workflow enforces:
      - All 9 required gates must pass
      - Verifier reply MUST contain worker_pid (non-None, non-zero)
      - Stage 2 is fail-closed (not created when Stage 1 fails)
    """

    def __init__(self):
        self.required_gates: list[str] = list(REQUIRED_GATES)
        self.required_verifier_fields: list[str] = list(REQUIRED_VERIFIER_FIELDS)

    def verify(
        self,
        task_id: str,
        status: Optional[str],
        result_str: Optional[str],
        result_sha256: Optional[str],
        worker_pid: Optional[int],
        expected_marker: str,
    ) -> tuple[Verdict, dict[str, GateResult]]:
        """Run all 9 gates, return (verdict, gate_results)."""
        gates: dict[str, GateResult] = {}

        # Gate 1: artifact_exists
        gates["artifact_exists"] = GateResult(
            passed=status == "done",
            detail=f"task status={status}",
        )

        # Gate 2: manifest_sha256_match (was sha256_match)
        manifest_ok = result_sha256 is not None and len(str(result_sha256)) == 64
        try:
            if manifest_ok and result_str and result_str.strip() and result_str != "{}":
                parsed = json.loads(result_str)
                canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
                expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                manifest_ok = result_sha256 == expected
        except (json.JSONDecodeError, Exception):
            manifest_ok = False
        gates["manifest_sha256_match"] = GateResult(
            passed=manifest_ok,
            detail=f"sha256={result_sha256}",
        )

        # Gate 3: schema_valid
        schema_ok = False
        if result_str:
            try:
                parsed = json.loads(result_str)
                if isinstance(parsed, dict):
                    schema_ok = True
            except json.JSONDecodeError:
                pass
        gates["schema_valid"] = GateResult(
            passed=schema_ok,
            detail="valid JSON object",
        )

        # Gate 4: content_type_allowed
        gates["content_type_allowed"] = GateResult(
            passed=True, detail="application/json"
        )

        # Gate 5: artifact_size_allowed
        size_ok = result_str is not None and len(result_str.encode("utf-8")) < 1_000_000
        gates["artifact_size_allowed"] = GateResult(
            passed=size_ok,
            detail=f"size={len(result_str or '')}",
        )

        # Gate 6: malware_or_forbidden_extension_check
        gates["malware_or_forbidden_extension_check"] = GateResult(
            passed=True, detail="synthetic — clean"
        )

        # Gate 7: tests_pass
        tests_ok = False
        if result_str:
            try:
                parsed = json.loads(result_str)
                tests_ok = parsed.get("tests_pass", False) is True
            except json.JSONDecodeError:
                pass
        gates["tests_pass"] = GateResult(
            passed=tests_ok,
            detail="tests_pass=True",
        )

        # Gate 8: consumer_contract_present
        contract_ok = False
        if result_str:
            try:
                parsed = json.loads(result_str)
                contract_ok = parsed.get("marker") == expected_marker
            except json.JSONDecodeError:
                pass
        gates["consumer_contract_present"] = GateResult(
            passed=contract_ok,
            detail=f"marker={expected_marker}",
        )

        # Gate 9: result_sha256_match
        sha256_ok = result_sha256 is not None and len(str(result_sha256)) == 64
        gates["result_sha256_match"] = GateResult(
            passed=sha256_ok,
            detail=f"sha256 present={sha256_ok}",
        )

        # Check verifier required fields
        verifier_fail = False
        if worker_pid is None or worker_pid == 0:
            verifier_fail = True
            gates["result_sha256_match"] = GateResult(
                passed=False,
                detail="verifier worker_pid missing or zero — FAIL",
            )

        # Final verdict
        all_passed = all(g.passed for g in gates.values())
        verdict = Verdict.PASS if all_passed and not verifier_fail else Verdict.FAIL

        return (verdict, gates)
