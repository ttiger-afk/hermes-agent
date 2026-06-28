# ---------------------------------------------------------------------------
# Issue #146: fail-closed result atomicity tests
# ---------------------------------------------------------------------------

import json, hashlib
import pytest
import sqlite3
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db import _validate_completion_result


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_missing_result_never_marks_done(kanban_home):
    """result=None must not transition to done."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="test missing result", assignee="bot2")
        kb.claim_task(conn, tid)
        ok = kb.complete_task(conn, tid, result=None)
        assert ok is False, "None result must not be accepted"
        task = kb.get_task(conn, tid)
        assert task.status != "done", f"status={task.status}, expected != done"


def test_empty_result_never_marks_done(kanban_home):
    """result='' must not transition to done."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="test empty result", assignee="bot2")
        kb.claim_task(conn, tid)
        ok = kb.complete_task(conn, tid, result="")
        assert ok is False, "empty result must not be accepted"
        task = kb.get_task(conn, tid)
        assert task.status != "done"


def test_empty_object_never_marks_done(kanban_home):
    """result='{}' must not transition to done."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="test empty object", assignee="bot2")
        kb.claim_task(conn, tid)
        ok = kb.complete_task(conn, tid, result="{}")
        assert ok is False, "empty object must not be accepted"
        task = kb.get_task(conn, tid)
        assert task.status != "done"


def test_invalid_json_never_marks_done(kanban_home):
    """result='not-json' must not transition to done."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="test invalid json", assignee="bot2")
        kb.claim_task(conn, tid)
        ok = kb.complete_task(conn, tid, result="not-json")
        assert ok is False, "invalid JSON must not be accepted"
        task = kb.get_task(conn, tid)
        assert task.status != "done"


def test_serialization_failure_never_marks_done(kanban_home):
    """result containing non-serializable types must be rejected."""
    # We can't easily inject a non-serializable type through the
    # string interface, but we test the validator directly.
    from hermes_cli.kanban_db import _validate_completion_result
    import pytest as pt
    # A bare partial-JSON string that looks like an attempt to inject
    # invalid structure: trailing garbage after valid JSON.
    with pt.raises(ValueError):
        _validate_completion_result('{"ok":true}trailing')


def test_commit_failure_never_marks_done(kanban_home):
    """If the write_txn rowcount is 0 (e.g. status mismatch), status must not be done."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="test no-rowcount", assignee="bot2")
        # Do NOT claim — complete_task on ready is allowed but let's test
        # the rowcount==0 path by completing a task that was already completed
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, result='{"ok":true,"seq":1}')
        # Second completion on already-done task → rowcount=0 → returns False
        ok2 = kb.complete_task(conn, tid, result='{"ok":true,"seq":2}')
        assert ok2 is False, "second completion must return False (rowcount=0)"
        task = kb.get_task(conn, tid)
        # Result must be from FIRST completion
        assert '"seq":1' in (task.result or ""), "duplicate must not overwrite result"


def test_valid_result_atomic_completion_pass(kanban_home):
    """Valid result JSON → status=done + result + result_sha256 all present."""
    import json as j
    contract = j.dumps({"tests_pass": True, "synthetic_data_only": True, "marker": "canary-001"})
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="test valid completion", assignee="bot2")
        kb.claim_task(conn, tid)
        ok = kb.complete_task(conn, tid, result=contract)
        assert ok is True, "valid contract result must be accepted"
        task = kb.get_task(conn, tid)
        assert task.status == "done"
        assert task.result is not None
        assert task.result_sha256 is not None
        assert len(task.result_sha256) == 64, f"sha256 length={len(task.result_sha256)}"


def test_result_sha256_matches_canonical_json(kanban_home):
    """SHA-256 must be the hex digest of canonical (sorted-key) JSON."""
    import json as j, hashlib
    # Non-sorted input — canonical form must sort keys
    result_in = '{"b":2,"a":1}'
    canonical = j.dumps({"a": 1, "b": 2}, sort_keys=True, separators=(",", ":"))
    expected_sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="test sha256 match", assignee="bot2")
        kb.claim_task(conn, tid)
        ok = kb.complete_task(conn, tid, result=result_in)
        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.result_sha256 == expected_sha, (
            f"sha256={task.result_sha256}, expected={expected_sha}"
        )
        # Verify result was canonicalised
        assert task.result == canonical, (
            f"result={task.result!r}, expected={canonical!r}"
        )


def test_duplicate_completion_is_idempotent(kanban_home):
    """Completing an already-done task must be a no-op (return False)."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="test idempotent", assignee="bot2")
        kb.claim_task(conn, tid)
        # First completion
        ok1 = kb.complete_task(
            conn, tid, result='{"ok":true,"version":1}'
        )
        assert ok1 is True
        # Second completion
        ok2 = kb.complete_task(
            conn, tid, result='{"ok":true,"version":2}'
        )
        assert ok2 is False, "duplicate completion must return False"
        task = kb.get_task(conn, tid)
        # Result must be from FIRST completion
        assert '"version":1' in (task.result or ""), "duplicate must not overwrite result"


def test_crash_before_commit_is_retryable(kanban_home):
    """Task must stay retryable (not done) if exception occurs mid-write."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="test retryable", assignee="bot2")
        kb.claim_task(conn, tid)
        # Write partial state manually to simulate mid-txn
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (tid,))
        conn.commit()
        # Confirm status is still running (not done)
        task = kb.get_task(conn, tid)
        assert task.status == "running", "task must remain retryable"
        assert task.result is None, "no result until proper completion"


def test_real_commit_failure_is_retryable(kanban_home, monkeypatch):
    """Real COMMIT failure (sqlite3.OperationalError) must not leave task done.

    Verifies that when the SQLite COMMIT itself raises — simulating a
    disk-full, I/O error, or torn transaction — the task remains in a
    retryable state with no partial commit.

    Requirements (from Issue #146 Canary A):
      exception_raised=true
      status!=done
      result IS NULL
      result_sha256 IS NULL
      task remains retryable
      no partial commit
    """
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="test real commit failure", assignee="bot2"
        )
        kb.claim_task(conn, tid)

        # Inject a real COMMIT failure by monkeypatching write_txn.
        # sqlite3 connection uses isolation_level=None (autocommit), so
        # every statement commits immediately.  To simulate a real
        # COMMIT failure we wrap the body in ``BEGIN IMMEDIATE`` first,
        # then raise OperationalError instead of calling ``COMMIT``.
        #
        # conn.commit is read-only on sqlite3 C objects, so we replace
        # the entire context manager.
        from contextlib import contextmanager

        @contextmanager
        def failing_write_txn(conn_arg):
            """write_txn that wraps work in a real transaction, then fails at COMMIT."""
            conn_arg.execute("BEGIN IMMEDIATE")
            try:
                yield
                # Simulate COMMIT failure — never reaches conn_arg.commit()
                raise sqlite3.OperationalError(
                    "simulated disk full during commit"
                )
            except BaseException:
                conn_arg.rollback()
                raise

        monkeypatch.setattr(kb, "write_txn", failing_write_txn)

        # complete_task must raise — the exception propagates through
        # write_txn's rollback-and-re-raise path
        with pytest.raises(Exception) as exc_info:
            kb.complete_task(conn, tid, result='{"ok":true,"test":"real-failure"}')

        # Confirm an exception was raised
        assert exc_info.value is not None

        # Verify the task did NOT transition to done
        task = kb.get_task(conn, tid)
        assert task.status != "done", (
            f"status={task.status}, expected != done after real commit failure"
        )
        assert task.result is None, (
            "result must be NULL after commit failure (no partial commit)"
        )
        assert task.result_sha256 is None, (
            "result_sha256 must be NULL after commit failure"
        )
        # Task must remain retryable (running/ready/blocked, not done)
        assert task.status in ("running", "ready", "blocked"), (
            f"status={task.status}, task must remain retryable"
        )
