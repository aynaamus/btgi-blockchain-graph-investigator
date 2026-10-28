import duckdb
import pytest

from btgi import cases
from btgi.cases import CaseError
from btgi.detect import DetectionConfig, run_detection
from graphs import build

CFG = DetectionConfig(ref_last_step=2)


@pytest.fixture
def env(con):
    _, n = build(con)
    run_detection(con, CFG)
    cases.create_case_schema(con)
    return con, n


def actions(con, cid):
    return [(a["action"], a["field"], a["old_value"], a["new_value"]) for a in cases.get_audit(con, cid)]


def test_open_case_snapshots_alert_and_audits(env):
    con, n = env
    cid = cases.open_case(con, n["a_in_4"], "ana")
    c = cases.get_case(con, cid)
    assert c["status"] == "open" and c["tx_id"] == n["a_in_4"] and c["opened_by"] == "ana"
    assert c["alert_severity"] == "low" and c["alert_rules"] == "A:in"
    assert actions(con, cid) == [("open_case", "status", None, "open")]
    a = cases.get_audit(con, cid)[0]
    assert a["analyst"] == "ana" and a["ts"] is not None


def test_one_case_per_alert(env):
    con, n = env
    cases.open_case(con, n["a_in_4"], "ana")
    with pytest.raises(CaseError, match="already exists"):
        cases.open_case(con, n["a_in_4"], "bob")
    assert con.execute("SELECT count(*) FROM cases").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM case_audit").fetchone()[0] == 1   # failed attempt left no trace


def test_case_requires_an_alert_and_analyst(env):
    con, n = env
    with pytest.raises(CaseError, match="No structural alert"):
        cases.open_case(con, n["isolated"], "ana")
    with pytest.raises(CaseError, match="Analyst"):
        cases.open_case(con, n["a_in_4"], "  ")
    with pytest.raises(CaseError, match="Analyst"):
        cases.open_case(con, n["a_in_4"], "x" * 65)
    assert con.execute("SELECT count(*) FROM cases").fetchone()[0] == 0


def test_status_disposition_notes_and_close_reopen(env):
    con, n = env
    cid = cases.open_case(con, n["distributor"], "ana")
    cases.set_status(con, cid, "ana", "in_review")
    cases.add_note(con, cid, "ana", "High out-degree to leaf targets", kind="finding")
    cases.add_note(con, cid, "bob", "Second look pending")
    cases.set_disposition(con, cid, "bob", "structural_pattern_only")
    cases.close_case(con, cid, "bob")
    c = cases.get_case(con, cid)
    assert c["status"] == "closed" and c["disposition"] == "structural_pattern_only" and c["closed_at"]
    cases.reopen_case(con, cid, "ana", "new evidence")
    c = cases.get_case(con, cid)
    assert c["status"] == "open" and c["closed_at"] is None
    assert c["disposition"] == "structural_pattern_only"            # history kept, not silently erased
    notes = cases.list_notes(con, cid)
    assert [x["kind"] for x in notes] == ["finding", "note", "note"] and "Reopened: new evidence" in notes[-1]["body"]
    assert actions(con, cid) == [
        ("open_case", "status", None, "open"),
        ("set_status", "status", "open", "in_review"),
        ("add_finding", "note_id", None, "1"),
        ("add_note", "note_id", None, "2"),
        ("set_disposition", "disposition", None, "structural_pattern_only"),
        ("close_case", "status", "in_review", "closed"),
        ("reopen_case", "status", "closed", "open"),
    ]


def test_close_with_disposition_argument_audits_both_changes(env):
    con, n = env
    cid = cases.open_case(con, n["distributor"], "ana")
    cases.close_case(con, cid, "ana", "inconclusive")
    acts = [a[0] for a in actions(con, cid)]
    assert acts[-2:] == ["set_disposition", "close_case"]


def test_invalid_operations_rejected_and_roll_back(env):
    con, n = env
    cid = cases.open_case(con, n["distributor"], "ana")
    before = actions(con, cid)
    with pytest.raises(CaseError, match="disposition is required"):
        cases.close_case(con, cid, "ana")
    with pytest.raises(CaseError, match="Disposition must"):
        cases.set_disposition(con, cid, "ana", "confirmed_criminal")
    with pytest.raises(CaseError, match="Status must"):
        cases.set_status(con, cid, "ana", "closed")
    with pytest.raises(CaseError, match="already open"):
        cases.set_status(con, cid, "ana", "open")
    with pytest.raises(CaseError, match="empty"):
        cases.add_note(con, cid, "ana", "   ")
    with pytest.raises(CaseError, match="exceeds"):
        cases.add_note(con, cid, "ana", "x" * 5001)
    with pytest.raises(CaseError, match="kind"):
        cases.add_note(con, cid, "ana", "ok", kind="verdict")
    with pytest.raises(CaseError, match="Only closed"):
        cases.reopen_case(con, cid, "ana", "why")
    with pytest.raises(CaseError, match="not found"):
        cases.set_status(con, 999, "ana", "in_review")
    assert actions(con, cid) == before
    assert cases.get_case(con, cid)["status"] == "open" and cases.list_notes(con, cid) == []
    # closed cases are frozen until reopened
    cases.close_case(con, cid, "ana", "no_further_action")
    for call in (lambda: cases.add_note(con, cid, "ana", "late"),
                 lambda: cases.set_status(con, cid, "ana", "in_review"),
                 lambda: cases.set_disposition(con, cid, "ana", "inconclusive"),
                 lambda: cases.close_case(con, cid, "ana", "inconclusive")):
        with pytest.raises(CaseError, match="closed"):
            call()


def test_mid_operation_failure_rolls_back_everything(env, monkeypatch):
    con, n = env
    cid = cases.open_case(con, n["distributor"], "ana")
    snapshot = (con.execute("SELECT * FROM cases").fetchall(), con.execute("SELECT * FROM case_audit").fetchall())

    def boom(*a, **k):
        raise duckdb.Error("simulated failure after the data change")
    monkeypatch.setattr(cases, "_audit", boom)
    with pytest.raises(duckdb.Error):
        cases.set_status(con, cid, "ana", "in_review")
    with pytest.raises(duckdb.Error):
        cases.add_note(con, cid, "ana", "will vanish")
    with pytest.raises(duckdb.Error):
        cases.open_case(con, n["both"], "ana")
    monkeypatch.undo()
    assert (con.execute("SELECT * FROM cases").fetchall(), con.execute("SELECT * FROM case_audit").fetchall()) == snapshot
    assert con.execute("SELECT count(*) FROM case_notes").fetchone()[0] == 0
    cases.set_status(con, cid, "ana", "in_review")      # connection still healthy


def test_audit_is_append_only_through_the_api(env):
    con, n = env
    public = {k for k in dir(cases) if not k.startswith("_")}
    assert not any(("delete" in k or "remove" in k or "edit" in k or "update_audit" in k) for k in public)
    cid = cases.open_case(con, n["distributor"], "ana")
    seen = []
    for step in (lambda: cases.set_status(con, cid, "ana", "in_review"),
                 lambda: cases.add_note(con, cid, "ana", "x"),
                 lambda: cases.close_case(con, cid, "ana", "inconclusive")):
        step()
        rows = cases.get_audit(con, cid)
        assert rows[:len(seen)] == seen              # earlier entries unchanged
        assert [r["audit_id"] for r in rows] == sorted(r["audit_id"] for r in rows)
        seen = rows


def test_cases_survive_restart_and_detection_rerun(tmp_path):
    db = tmp_path / "t.duckdb"
    c1 = duckdb.connect(str(db))
    _, n = build(c1)
    run_detection(c1, CFG)
    cases.create_case_schema(c1)
    cid = cases.open_case(c1, n["distributor"], "ana")
    cases.add_note(c1, cid, "ana", "kept")
    c1.close()

    c2 = duckdb.connect(str(db))                       # "restart"
    assert cases.get_case(c2, cid)["tx_id"] == n["distributor"]
    assert len(cases.get_audit(c2, cid)) == 2 and cases.list_notes(c2, cid)[0]["body"] == "kept"
    # a changed detection config rebuilds every alert table, but not the case tables
    run_detection(c2, DetectionConfig(ref_last_step=2, a_quantile=0.9))
    assert cases.get_case(c2, cid)["alert_rules"] == "A:out"
    assert len(cases.get_audit(c2, cid)) == 2 and len(cases.list_notes(c2, cid)) == 1
    with pytest.raises(CaseError, match="already exists"):
        cases.open_case(c2, n["distributor"], "bob")


def test_list_cases_filters(env):
    con, n = env
    a = cases.open_case(con, n["distributor"], "ana")
    b = cases.open_case(con, n["both"], "ana")
    cases.close_case(con, b, "ana", "inconclusive")
    assert {c["case_id"] for c in cases.list_cases(con)} == {a, b}
    assert [c["case_id"] for c in cases.list_cases(con, status="closed")] == [b]
    assert cases.get_case(con, 12345) is None and cases.get_case_by_tx(con, 1) is None
