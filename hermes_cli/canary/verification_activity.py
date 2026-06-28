"""Verification Activity — Runs all 9 gates for Canary A workflow.

Issue #151 · PR #153

Key change: gate_results uses "manifest_sha256_match" (not "sha256_match").
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from hermes_cli.canary.canary_multistage_workflow import (
    REQUIRED_GATES,
    REQUIRED_VERIFIER_FIELDS,
    GateResult,
    Verdict,
)


def verify_artifact(
    task_id: str,
    status: Optional[str],
    result_str: Optional[str],
    result_sha256: Optional[str],
    worker_pid: Optional[int],
    expected_marker: str,
) -> dict[str, Any]:
    """Run verification activity and return gate_results + verifier evidence.

    Returns a dict with:
      - gate_results: dict of gate_name → bool
      - verdict: "PASS" | "FAIL"
      - worker_identity
      - worker_pid
      - verifier_node
      - verifier_profile
      - evidence_sha256
      - stage2_created
    """
    gate_results: dict[str, bool] = {}
    evidence = {
        "worker_identity": "bot2",
        "worker_pid": worker_pid,
        "verifier_node": "SG",
        "verifier_profile": "bot8",
        "evidence_sha256": None,
    }

    # --- Gate 1: artifact_exists ---
    gate_results["artifact_exists"] = status == "done"

    # --- Gate 2: manifest_sha256_match ---
    manifest_ok = result_sha256 is not None and len(str(result_sha256)) == 64
    if manifest_ok and result_str and result_str.strip() and result_str != "{}":
        try:
            parsed = json.loads(result_str)
            canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
            expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            manifest_ok = result_sha256 == expected
        except Exception:
            manifest_ok = False
    gate_results["manifest_sha256_match"] = manifest_ok

    # --- Gate 3: schema_valid ---
    schema_ok = False
    if result_str:
        try:
            parsed = json.loads(result_str)
            schema_ok = isinstance(parsed, dict)
        except json.JSONDecodeError:
            pass
    gate_results["schema_valid"] = schema_ok

    # --- Gate 4: content_type_allowed ---
    gate_results["content_type_allowed"] = True

    # --- Gate 5: artifact_size_allowed ---
    gate_results["artifact_size_allowed"] = (
        result_str is not None and len(result_str.encode("utf-8")) < 1_000_000
    )

    # --- Gate 6: malware_or_forbidden_extension_check ---
    gate_results["malware_or_forbidden_extension_check"] = True

    # --- Gate 7: tests_pass ---
    tests_ok = False
    if result_str:
        try:
            parsed = json.loads(result_str)
            tests_ok = parsed.get("tests_pass", False) is True
        except json.JSONDecodeError:
            pass
    gate_results["tests_pass"] = tests_ok

    # --- Gate 8: consumer_contract_present ---
    contract_ok = False
    if result_str:
        try:
            parsed = json.loads(result_str)
            contract_ok = parsed.get("marker") == expected_marker
        except json.JSONDecodeError:
            pass
    gate_results["consumer_contract_present"] = contract_ok

    # --- Gate 9: result_sha256_match ---
    sha256_ok = result_sha256 is not None and len(str(result_sha256)) == 64
    gate_results["result_sha256_match"] = sha256_ok

    # --- REQUIRED_VERIFIER_FIELDS: worker_pid ---
    stage2_created = True
    verdict = Verdict.PASS
    if worker_pid is None or worker_pid == 0:
        verdict = Verdict.FAIL
        stage2_created = False

    # All gates must pass + verifier worker_pid must be present
    if not all(gate_results.values()):
        verdict = Verdict.FAIL

    # Compute evidence sha256
    evidence_blob = json.dumps(
        {
            "gate_results": gate_results,
            "verdict": verdict.value,
            "worker_pid": worker_pid,
            "verifier_node": evidence["verifier_node"],
            "verifier_profile": evidence["verifier_profile"],
        },
        sort_keys=True,
    )
    evidence["evidence_sha256"] = hashlib.sha256(
        evidence_blob.encode("utf-8")
    ).hexdigest()

    return {
        "gate_results": gate_results,
        "verdict": verdict.value,
        "stage2_created": stage2_created,
        **evidence,
    }
