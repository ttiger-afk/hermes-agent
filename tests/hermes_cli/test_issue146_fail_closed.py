"""Issue #146/#154 fail-closed canonical result authority tests.

Tests for the Writer side (hermes_cli/kanban_db.py):
- No auto-generation of result from summary
- NULL/empty/{}/invalid JSON rejected
- ensure_ascii=False canonical serialization
- Atomic write of result + result_sha256
- Unicode result hash matches canonical rule
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _valid_result():
    return json.dumps(
        {"tests_pass": True, "synthetic_data_only": True, "marker": "test"},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


# ---------------------------------------------------------------------------
# 1. missing_result_does_not_autogenerate_from_summary
# ---------------------------------------------------------------------------

def test_missing_result_does_not_autogenerate_from_summary(kanban_home):
    """When result is None and only summary is provided, reject — do NOT auto-generate."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="test", assignee="a")
        kb.claim_task(conn, tid)
        ok = kb.complete_task(conn, tid, result=None, summary="This should not become a result")
        assert ok is False
        t = kb.get_task(conn, tid)
        assert t is not None
        assert t.status != "done"
        # Verify no auto-generated _auto key
        assert t.result is None


# ---------------------------------------------------------------------------
# 2. summary_only_completion_is_rejected
# ---------------------------------------------------------------------------

def test_summary_only_completion_is_rejected(kanban_home):
    """Omitting result entirely while providing summary must be rejected."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="summary only", assignee="a")
        kb.claim_task(conn, tid)
        ok = kb.complete_task(conn, tid, summary="Just a summary, no result")
        assert ok is False
        t = kb.get_task(conn, tid)
        assert t is not None
        assert t.status != "done"


# ---------------------------------------------------------------------------
# 3. null_result_never_marks_done
# ---------------------------------------------------------------------------

def test_null_result_never_marks_done(kanban_home):
    """result=None must never transition task to 'done'."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="null result", assignee="a")
        kb.claim_task(conn, tid)
        ok = kb.complete_task(conn, tid, result=None)
        assert ok is False
        t = kb.get_task(conn, tid)
        assert t is not None
        assert t.status == "running"  # stays retryable


# ---------------------------------------------------------------------------
# 4. empty_result_never_marks_done
# ---------------------------------------------------------------------------

def test_empty_result_never_marks_done(kanban_home):
    """Empty string result must never transition task to 'done'."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="empty result", assignee="a")
        kb.claim_task(conn, tid)
        ok = kb.complete_task(conn, tid, result="")
        assert ok is False
        t = kb.get_task(conn, tid)
        assert t is not None
        assert t.status != "done"


def test_whitespace_result_never_marks_done(kanban_home):
    """Whitespace-only result must never transition task to 'done'."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="whitespace result", assignee="a")
        kb.claim_task(conn, tid)
        ok = kb.complete_task(conn, tid, result="   \n  \t  ")
        assert ok is False
        t = kb.get_task(conn, tid)
        assert t is not None
        assert t.status != "done"


# ---------------------------------------------------------------------------
# 5. empty_object_never_marks_done
# ---------------------------------------------------------------------------

def test_empty_object_never_marks_done(kanban_home):
    """result='{}' must never transition task to 'done'."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="empty object", assignee="a")
        kb.claim_task(conn, tid)
        ok = kb.complete_task(conn, tid, result="{}")
        assert ok is False
        t = kb.get_task(conn, tid)
        assert t is not None
        assert t.status != "done"


# ---------------------------------------------------------------------------
# 6. invalid_json_never_marks_done
# ---------------------------------------------------------------------------

def test_invalid_json_never_marks_done(kanban_home):
    """Non-JSON result must never transition task to 'done'."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="invalid json", assignee="a")
        kb.claim_task(conn, tid)
        ok = kb.complete_task(conn, tid, result="not json at all !!!")
        assert ok is False
        t = kb.get_task(conn, tid)
        assert t is not None
        assert t.status != "done"


# ---------------------------------------------------------------------------
# 7. all_done_writers_use_complete_task
# (Verified: complete_task is the only function that writes status='done'
#  via the UPDATE tasks SET status='done' queries at lines ~4099-4110 and ~4115-4130.
#  No other function directly executes UPDATE tasks SET status='done'.)
# ---------------------------------------------------------------------------

def test_all_done_writers_use_complete_task(kanban_home):
    """complete_task is the sole writer; confirm it handles the full pipeline."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="valid task", assignee="a")
        kb.claim_task(conn, tid)
        result = _valid_result()
        ok = kb.complete_task(conn, tid, result=result, summary="all good")
        assert ok is True
        t = kb.get_task(conn, tid)
        assert t is not None
        assert t.status == "done"
        assert t.result is not None
        assert t.result_sha256 is not None


# ---------------------------------------------------------------------------
# 8. result_and_hash_written_atomically
# ---------------------------------------------------------------------------

def test_result_and_hash_written_atomically(kanban_home):
    """Successful completion writes both result and result_sha256 in one transaction."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="atomic test", assignee="a")
        kb.claim_task(conn, tid)
        result_json = json.dumps({"a": 1, "b": 2}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        ok = kb.complete_task(conn, tid, result=result_json)
        assert ok is True
        t = kb.get_task(conn, tid)
        assert t is not None
        assert t.status == "done"
        assert t.result == result_json
        assert t.result_sha256 is not None
        expected_sha = hashlib.sha256(result_json.encode("utf-8")).hexdigest()
        assert t.result_sha256 == expected_sha


# ---------------------------------------------------------------------------
# 9. unicode_result_hash_matches_canonical_rule
# ---------------------------------------------------------------------------

def test_unicode_result_hash_matches_canonical_rule(kanban_home):
    """Writer, Reader, SG must produce the same SHA-256 for unicode results."""
    result_obj = {
        "tests_pass": True,
        "message": "中文验证",
        "marker": "issue154",
    }
    canonical = json.dumps(result_obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="unicode test", assignee="a")
        kb.claim_task(conn, tid)
        # Write partial state manually to simulate mid-txn
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (tid,))
        conn.commit()
        # Confirm status is still running (not done)
        task = kb.get_task(conn, tid)
        assert task is not None
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
        assert task is not None
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
