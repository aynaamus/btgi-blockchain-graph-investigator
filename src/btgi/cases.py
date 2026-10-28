"""Local investigation case workflow with an append-only audit trail.

Cases are keyed by the original transaction id (one case per alerted
transaction) and snapshot the alert at opening time, so they survive
detection reruns, which rebuild the alert tables. The API exposes no
operation that edits or deletes audit rows. Every mutation runs in one
transaction together with its audit entries; failures roll back.

Dispositions describe an analyst's review outcome of a structural pattern.
None of them asserts criminal activity.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import duckdb

STATUSES = ("open", "in_review", "closed")
DISPOSITIONS = (
    "no_further_action",
    "structural_pattern_only",
    "needs_further_review",
    "inconclusive",
)
NOTE_KINDS = ("finding", "note")
MAX_TEXT = 5000


class CaseError(Exception):
    """Invalid case operation; nothing was changed."""


DDL = [
    "CREATE SEQUENCE IF NOT EXISTS seq_case_id START 1",
    "CREATE SEQUENCE IF NOT EXISTS seq_case_note_id START 1",
    "CREATE SEQUENCE IF NOT EXISTS seq_case_audit_id START 1",
    """CREATE TABLE IF NOT EXISTS cases (
    case_id         BIGINT PRIMARY KEY DEFAULT nextval('seq_case_id'),
    tx_id           BIGINT NOT NULL UNIQUE,   -- original txId; one case per alert
    status          VARCHAR NOT NULL CHECK (status IN ('open','in_review','closed')),
    disposition     VARCHAR,
    opened_by       VARCHAR NOT NULL,
    opened_at       TIMESTAMP NOT NULL,
    updated_at      TIMESTAMP NOT NULL,
    closed_at       TIMESTAMP,
    alert_run_id    BIGINT,                   -- snapshot of the alert when the case was opened
    alert_time_step INTEGER,
    alert_risk_score DOUBLE,
    alert_severity  VARCHAR,
    alert_rules     VARCHAR
)""",
    """CREATE TABLE IF NOT EXISTS case_notes (
    note_id    BIGINT PRIMARY KEY DEFAULT nextval('seq_case_note_id'),
    case_id    BIGINT NOT NULL,
    kind       VARCHAR NOT NULL CHECK (kind IN ('finding','note')),
    analyst    VARCHAR NOT NULL,
    created_at TIMESTAMP NOT NULL,
    body       VARCHAR NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS case_audit (
    audit_id  BIGINT PRIMARY KEY DEFAULT nextval('seq_case_audit_id'),
    case_id   BIGINT NOT NULL,
    ts        TIMESTAMP NOT NULL,
    analyst   VARCHAR NOT NULL,
    action    VARCHAR NOT NULL,
    field     VARCHAR,
    old_value VARCHAR,
    new_value VARCHAR
)""",
]


def create_case_schema(con: duckdb.DuckDBPyConnection) -> None:
    for stmt in DDL:
        con.execute(stmt)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@contextmanager
def _transaction(con):
    con.execute("BEGIN")
    try:
        yield
        con.execute("COMMIT")
    except BaseException:
        try:
            con.execute("ROLLBACK")
        except duckdb.Error:
            pass
        raise


def _analyst(name: str) -> str:
    name = (name or "").strip()
    if not name or len(name) > 64:
        raise CaseError("Analyst identifier is required (max 64 characters)")
    return name


def _text(body: str) -> str:
    body = (body or "").strip()
    if not body:
        raise CaseError("Text must not be empty")
    if len(body) > MAX_TEXT:
        raise CaseError(f"Text exceeds {MAX_TEXT} characters")
    return body


def _audit(con, case_id, ts, analyst, action, field=None, old=None, new=None):
    con.execute(
        "INSERT INTO case_audit (case_id, ts, analyst, action, field, old_value, new_value) "
        "VALUES (?,?,?,?,?,?,?)",
        [case_id, ts, analyst, action, field,
         None if old is None else str(old), None if new is None else str(new)],
    )


def _row(con, case_id: int) -> dict:
    cur = con.execute("SELECT * FROM cases WHERE case_id = ?", [case_id])
    row = cur.fetchone()
    if row is None:
        raise CaseError(f"Case {case_id} not found")
    return dict(zip([d[0] for d in cur.description], row))


def _require_editable(case: dict) -> None:
    if case["status"] == "closed":
        raise CaseError("Case is closed; reopen it before making changes")


def open_case(con, tx_id: int, analyst: str) -> int:
    """Open a case from the structural alert on `tx_id`. Returns the case id."""
    analyst = _analyst(analyst)
    try:
        with _transaction(con):
            alert = con.execute(
                "SELECT run_id, time_step, risk_score, severity, rules FROM alerts WHERE tx_id = ?",
                [tx_id],
            ).fetchone()
            if alert is None:
                raise CaseError(f"No structural alert exists for transaction {tx_id}")
            if con.execute("SELECT 1 FROM cases WHERE tx_id = ?", [tx_id]).fetchone():
                raise CaseError(f"A case already exists for transaction {tx_id}")
            ts = _now()
            case_id = con.execute(
                "INSERT INTO cases (tx_id, status, opened_by, opened_at, updated_at, alert_run_id, "
                "alert_time_step, alert_risk_score, alert_severity, alert_rules) "
                "VALUES (?, 'open', ?, ?, ?, ?, ?, ?, ?, ?) RETURNING case_id",
                [tx_id, analyst, ts, ts, *alert],
            ).fetchone()[0]
            _audit(con, case_id, ts, analyst, "open_case", "status", None, "open")
    except duckdb.ConstraintException as exc:
        raise CaseError(f"A case already exists for transaction {tx_id}") from exc
    return case_id


def add_note(con, case_id: int, analyst: str, body: str, kind: str = "note") -> int:
    """Add a finding or analyst note. Existing notes are never edited."""
    analyst, body = _analyst(analyst), _text(body)
    if kind not in NOTE_KINDS:
        raise CaseError(f"kind must be one of {NOTE_KINDS}")
    with _transaction(con):
        _require_editable(_row(con, case_id))
        ts = _now()
        note_id = con.execute(
            "INSERT INTO case_notes (case_id, kind, analyst, created_at, body) "
            "VALUES (?,?,?,?,?) RETURNING note_id",
            [case_id, kind, analyst, ts, body],
        ).fetchone()[0]
        con.execute("UPDATE cases SET updated_at = ? WHERE case_id = ?", [ts, case_id])
        _audit(con, case_id, ts, analyst, f"add_{kind}", "note_id", None, note_id)
    return note_id


def set_status(con, case_id: int, analyst: str, status: str) -> None:
    """Move between open and in_review. Closing and reopening have their own calls."""
    analyst = _analyst(analyst)
    if status not in ("open", "in_review"):
        raise CaseError("Status must be 'open' or 'in_review'; use close_case to close")
    with _transaction(con):
        case = _row(con, case_id)
        _require_editable(case)
        if case["status"] == status:
            raise CaseError(f"Case is already {status}")
        ts = _now()
        con.execute("UPDATE cases SET status = ?, updated_at = ? WHERE case_id = ?",
                    [status, ts, case_id])
        _audit(con, case_id, ts, analyst, "set_status", "status", case["status"], status)


def set_disposition(con, case_id: int, analyst: str, disposition: str) -> None:
    analyst = _analyst(analyst)
    if disposition not in DISPOSITIONS:
        raise CaseError(f"Disposition must be one of {DISPOSITIONS}")
    with _transaction(con):
        case = _row(con, case_id)
        _require_editable(case)
        if case["disposition"] == disposition:
            raise CaseError(f"Disposition is already {disposition}")
        ts = _now()
        con.execute("UPDATE cases SET disposition = ?, updated_at = ? WHERE case_id = ?",
                    [disposition, ts, case_id])
        _audit(con, case_id, ts, analyst, "set_disposition", "disposition",
               case["disposition"], disposition)


def close_case(con, case_id: int, analyst: str, disposition: str | None = None) -> None:
    """Close a case; a disposition must be given or already recorded."""
    analyst = _analyst(analyst)
    if disposition is not None and disposition not in DISPOSITIONS:
        raise CaseError(f"Disposition must be one of {DISPOSITIONS}")
    with _transaction(con):
        case = _row(con, case_id)
        _require_editable(case)
        final = disposition or case["disposition"]
        if final is None:
            raise CaseError("A disposition is required to close a case")
        ts = _now()
        if final != case["disposition"]:
            _audit(con, case_id, ts, analyst, "set_disposition", "disposition",
                   case["disposition"], final)
        con.execute(
            "UPDATE cases SET status='closed', disposition=?, closed_at=?, updated_at=? "
            "WHERE case_id=?", [final, ts, ts, case_id])
        _audit(con, case_id, ts, analyst, "close_case", "status", case["status"], "closed")


def reopen_case(con, case_id: int, analyst: str, reason: str) -> None:
    analyst, reason = _analyst(analyst), _text(reason)
    with _transaction(con):
        case = _row(con, case_id)
        if case["status"] != "closed":
            raise CaseError("Only closed cases can be reopened")
        ts = _now()
        con.execute(
            "UPDATE cases SET status='open', closed_at=NULL, updated_at=? WHERE case_id=?",
            [ts, case_id])
        _audit(con, case_id, ts, analyst, "reopen_case", "status", "closed", "open")
        con.execute(
            "INSERT INTO case_notes (case_id, kind, analyst, created_at, body) VALUES (?,?,?,?,?)",
            [case_id, "note", analyst, ts, f"Reopened: {reason}"])


def get_case_by_tx(con, tx_id: int) -> dict | None:
    cur = con.execute("SELECT * FROM cases WHERE tx_id = ?", [tx_id])
    row = cur.fetchone()
    return None if row is None else dict(zip([d[0] for d in cur.description], row))


def get_case(con, case_id: int) -> dict | None:
    try:
        return _row(con, case_id)
    except CaseError:
        return None


def list_notes(con, case_id: int) -> list[dict]:
    cur = con.execute(
        "SELECT note_id, kind, analyst, created_at, body FROM case_notes "
        "WHERE case_id = ? ORDER BY note_id", [case_id])
    return [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]


def get_audit(con, case_id: int) -> list[dict]:
    cur = con.execute(
        "SELECT audit_id, ts, analyst, action, field, old_value, new_value FROM case_audit "
        "WHERE case_id = ? ORDER BY audit_id", [case_id])
    return [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]


def list_cases(con, status: str | None = None, limit: int = 100) -> list[dict]:
    limit = max(1, min(int(limit), 500))
    where, params = ("WHERE status = ?", [status]) if status else ("", [])
    cur = con.execute(
        f"SELECT * FROM cases {where} ORDER BY updated_at DESC, case_id DESC LIMIT {limit}", params)
    return [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
