#!/usr/bin/env python3
"""Canary A: Production Preflight — Two-stage synthetic canary.

Issue #151 · PR #153

Two real execution stages, each:
  Temporal → Kanban → bot2 real Worker → result/result_sha256 → MinIO → SG Temporal Verifier

Stage 1: canary-a-stage-1-synthetic
Stage 2: canary-a-stage-2-synthetic  (fail-closed: NOT created if Stage 1 fails)

Each stage creates a kanban task with a unique contract, then the workflow
verifies results against required gates and verifier evidence fields.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import sqlite3
from pathlib import Path
from typing import Any, Optional

HERMES_HOME = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
os.environ.setdefault("HERMES_HOME", HERMES_HOME)
KANBAN_DB = Path(HERMES_HOME) / "kanban" / "boards" / "fleet" / "kanban.db"

sys.path.insert(0, str(Path(HERMES_HOME) / "hermes-agent"))
from hermes_cli import kanban_db as kb

from hermes_cli.canary.canary_multistage_workflow import (
    MultiStageWorkflow,
    REQUIRED_GATES,
    GateResult,
    Verdict,
)

# ---------------------------------------------------------------------------
# Stage contracts
# ---------------------------------------------------------------------------

STAGE_1_CONTRACT = json.dumps(
    {
        "tests_pass": True,
        "synthetic_data_only": True,
        "marker": "canary-a-stage-1",
    }
)

STAGE_2_CONTRACT = json.dumps(
    {
        "tests_pass": True,
        "synthetic_data_only": True,
        "marker": "canary-a-stage-2",
    }
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def canonical_sha256(result_str: str) -> str:
    parsed = json.loads(result_str)
    canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def wait_for_completion(
    task_id: str, deadline: float, poll_interval: float = 5.0
) -> tuple[Optional[str], Optional[str]]:
    """Poll until task is 'done' or deadline expires.

    Returns (status, result) — status is None if not found.
    """
    while time.time() < deadline:
        with kb.connect(db_path=KANBAN_DB) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT status, result, result_sha256, worker_pid FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                return (None, None)
            if row["status"] in ("done", "blocked", "archived"):
                return (
                    row["status"],
                    json.dumps(
                        {
                            "status": row["status"],
                            "result": row["result"],
                            "result_sha256": row["result_sha256"],
                            "worker_pid": row["worker_pid"],
                        }
                    ),
                )
        time.sleep(poll_interval)
    return ("timeout", None)


def verify_gates(
    task_id: str, expected_marker: str, workflow: MultiStageWorkflow
) -> tuple[Verdict, dict[str, GateResult]]:
    """Run the full gate verification against a completed task."""
    with kb.connect(db_path=KANBAN_DB) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, status, result, result_sha256, worker_pid, "
            "completed_at, claim_lock FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()

    if row is None:
        return (Verdict.FAIL, {"_error": GateResult(False, "task not found")})

    result = row["result"]
    sha256 = row["result_sha256"]
    worker_pid = row["worker_pid"]
    status = row["status"]

    return workflow.verify(
        task_id=task_id,
        status=status,
        result_str=result,
        result_sha256=sha256,
        worker_pid=worker_pid,
        expected_marker=expected_marker,
    )


# ---------------------------------------------------------------------------
# Canary runner
# ---------------------------------------------------------------------------


class CanaryA:
    """Canary A: Two-stage production preflight launcher.

    Creates exactly two kanban tasks (Stage 1, Stage 2) and verifies
    each through the full gate pipeline.
    """

    def __init__(self):
        self.workflow = MultiStageWorkflow()
        self.stage_1_task_id: Optional[str] = None
        self.stage_2_task_id: Optional[str] = None
        self.stage_1_verdict: Optional[Verdict] = None
        self.stage_2_verdict: Optional[Verdict] = None
        self.stage_1_evidence: dict[str, Any] = {}
        self.stage_2_evidence: dict[str, Any] = {}

    @property
    def execution_stage_count(self) -> int:
        return 2

    @property
    def stage_1_marker(self) -> str:
        return "canary-a-stage-1"

    @property
    def stage_2_marker(self) -> str:
        return "canary-a-stage-2"

    def run(self) -> bool:
        """Execute both stages. Returns True if fully PASS."""
        ok = True

        # --- Stage 1 ---
        print("CANARY-A: Stage 1 starting...", flush=True)
        self.stage_1_task_id = self._create_stage(
            title="Canary A Stage 1",
            body="Synthetic canary stage 1 — marker=canary-a-stage-1",
            contract=STAGE_1_CONTRACT,
        )
        print(f"CANARY-A: Stage 1 task_id={self.stage_1_task_id}", flush=True)

        deadline = time.time() + 300
        status, _ = wait_for_completion(self.stage_1_task_id, deadline)
        print(f"CANARY-A: Stage 1 status={status}", flush=True)

        verdict, gate_results = verify_gates(
            self.stage_1_task_id, self.stage_1_marker, self.workflow
        )
        self.stage_1_verdict = verdict
        self.stage_1_evidence = {
            "task_id": self.stage_1_task_id,
            "verdict": verdict.value,
            "gates": {k: v.passed for k, v in gate_results.items()},
        }
        print(f"CANARY-A: Stage 1 verdict={verdict.value}", flush=True)

        if verdict != Verdict.PASS:
            print("CANARY-A: Stage 1 FAIL — Stage 2 NOT created (fail-closed)", flush=True)
            ok = False
            return ok

        # --- Stage 2 ---
        print("CANARY-A: Stage 2 starting...", flush=True)
        self.stage_2_task_id = self._create_stage(
            title="Canary A Stage 2",
            body="Synthetic canary stage 2 — marker=canary-a-stage-2",
            contract=STAGE_2_CONTRACT,
        )
        print(f"CANARY-A: Stage 2 task_id={self.stage_2_task_id}", flush=True)

        deadline = time.time() + 300
        status, _ = wait_for_completion(self.stage_2_task_id, deadline)
        print(f"CANARY-A: Stage 2 status={status}", flush=True)

        verdict, gate_results = verify_gates(
            self.stage_2_task_id, self.stage_2_marker, self.workflow
        )
        self.stage_2_verdict = verdict
        self.stage_2_evidence = {
            "task_id": self.stage_2_task_id,
            "verdict": verdict.value,
            "gates": {k: v.passed for k, v in gate_results.items()},
        }
        print(f"CANARY-A: Stage 2 verdict={verdict.value}", flush=True)

        if verdict != Verdict.PASS:
            ok = False

        return ok

    def _create_stage(self, title: str, body: str, contract: str) -> str:
        """Create a kanban task for a canary stage."""
        kb._INITIALIZED_PATHS.discard(str(KANBAN_DB.resolve()))
        with kb.connect(db_path=KANBAN_DB) as conn:
            tid = kb.create_task(
                conn,
                title=title,
                assignee="bot2",
                tenant="canary",
                body=body,
            )
        return tid

    def cleanup(self):
        """Remove canary tasks from DB."""
        for tid in (self.stage_1_task_id, self.stage_2_task_id):
            if tid:
                try:
                    with kb.connect(db_path=KANBAN_DB) as conn:
                        conn.execute("DELETE FROM tasks WHERE id=?", (tid,))
                        conn.commit()
                except Exception:
                    pass


def main():
    canary = CanaryA()
    ok = False
    try:
        ok = canary.run()
    finally:
        canary.cleanup()
    print(f"CANARY-A: FINAL={'PASS' if ok else 'FAIL'}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
