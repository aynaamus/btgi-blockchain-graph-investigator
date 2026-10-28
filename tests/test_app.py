import json
import os
from pathlib import Path

import duckdb
import pytest
from streamlit.testing.v1 import AppTest

from btgi import cases
from btgi.detect import DetectionConfig, run_detection
from btgi.evaluate import evaluate
from graphs import HELD, build

APP = str(Path(__file__).resolve().parents[1] / "src" / "btgi" / "app.py")
CORE = ("transactions", "edges", "tx_labels", "tx_features", "ingestion_runs", "detection_runs",
        "rule_thresholds", "alerts", "alert_rule_hits", "alert_evidence")


@pytest.fixture
def appdb(tmp_path, monkeypatch):
    db = tmp_path / "app.duckdb"
    con = duckdb.connect(str(db))

    def labels(g, n):
        return {t: ("illicit" if t == n["a_in_4"] else "licit") for t, s in g.nodes.items() if s == HELD}
    _, names = build(con, labels)
    run_detection(con, DetectionConfig(ref_last_step=2))
    con.close()
    monkeypatch.setenv("BTGI_DB", str(db))
    return db, names


def fingerprint(db):
    con = duckdb.connect(str(db))
    out = {t: con.execute(f"SELECT count(*), sum(hash(x)::HUGEINT)::VARCHAR FROM {t} x").fetchone() for t in CORE}
    out["eval"] = json.dumps(evaluate(con, HELD, HELD), sort_keys=True)
    con.close()
    return out


# NB: the app keeps one cached read-write connection in this process, so inspection
# connections must use the same (default) configuration.


def run_view(view, **kw):
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    at.radio(key="nav").set_value(view).run()
    return at


def texts(at):
    parts = [m.value for m in at.markdown] + [m.value for m in at.caption] + [m.value for m in at.info] \
        + [m.value for m in at.warning] + [m.value for m in at.error] + [m.value for m in at.success]
    return "\n".join(str(p) for p in parts)


def test_overview_renders_with_notice(appdb):
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    assert "structural prominence" in texts(at) and "not** the probability of criminal activity" in texts(at)
    labels = {m.label: m.value for m in at.metric}
    assert labels["Transaction nodes"].replace(",", "").isdigit()
    assert "Connected components" in labels and "Unique alerted transactions" in labels


def test_explorer_lookup_missing_and_found(appdb):
    db, n = appdb
    at = run_view("Transaction Explorer")
    assert not at.exception
    at.text_input(key="tx_explorer").set_value("424242").run()
    assert any("not found" in w.value for w in at.warning)
    at.text_input(key="tx_explorer").set_value("abc").run()
    assert any("positive whole number" in e.value for e in at.error)
    at.text_input(key="tx_explorer").set_value(str(n["both"])).run()
    assert not at.exception
    m = {x.label: x.value for x in at.metric}
    assert m["Transaction ID"] == str(n["both"]) and m["In-degree"] == "4" and m["Out-degree"] == "3"
    assert "Dataset annotation" in " ".join(e.label for e in at.expander)


def test_graph_view_bounds_and_missing(appdb):
    db, n = appdb
    at = run_view("Graph Investigation")
    at.text_input(key="tx_graph").set_value("424242").run()
    assert any("not found" in w.value for w in at.warning) and not at.exception
    at.text_input(key="tx_graph").set_value(str(n["big"])).run()
    assert not at.exception
    at.slider[0].set_value(10).run()                      # max nodes
    assert any("truncated" in w.value.lower() for w in at.warning)
    at.text_input(key="tx_graph").set_value(str(n["isolated"])).run()
    assert any("no edges" in i.value for i in at.info) and not at.exception


def test_alert_view_filters_evidence_and_case_workflow(appdb):
    db, n = appdb
    at = run_view("Structural Alerts")
    assert not at.exception
    assert any("Find alert" in t.label for t in at.text_input)
    at.text_input(key="tx_alert_lookup").set_value(str(n["big"])).run()
    assert not at.exception
    t = texts(at)
    assert "Evidence complete" in t or "stored sample" in t
    at.sidebar.text_input(key="analyst").set_value("tester").run()
    next(b for b in at.button if b.label.startswith("Open case")).click().run()
    assert any("Case opened" in s.value for s in at.success)
    # the new case is persisted and visible in the queue's case-status column
    con = duckdb.connect(str(db))
    assert con.execute("SELECT tx_id, opened_by FROM cases").fetchall() == [(n["big"], "tester")]
    con.close()
    # filtering to an empty set shows a message rather than failing
    at.selectbox[2].set_value(1).run()                    # step 1 has no alert for this tx lookup
    assert any("No alerts match" in i.value for i in at.info) and not at.exception


def test_performance_view_shows_actual_numbers(appdb):
    db, n = appdb
    at = run_view("Detection Performance")
    assert not at.exception
    assert any("not a" in w.value and "validated" in w.value for w in at.warning)
    labels = {m.label: m.value for m in at.metric}
    assert set(labels) >= {"Precision", "Recall", "F1", "Known-label illicit prevalence"}
    body = texts(at)
    assert "worse than the base rate" in body or "not below the base rate" in body
    assert "unknown-label" in body.lower() and "retrospective" in body.lower()


def test_missing_database_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("BTGI_DB", str(tmp_path / "nope.duckdb"))
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception and any("Database not found" in e.value for e in at.error)


def test_app_leaves_analytic_tables_and_evaluation_unchanged(appdb):
    db, n = appdb
    before = fingerprint(db)
    for view in ("Overview", "Transaction Explorer", "Graph Investigation", "Structural Alerts",
                 "Detection Performance"):
        at = run_view(view)
        assert not at.exception, view
    # opening a case writes only to case tables
    at = run_view("Structural Alerts")
    at.text_input(key="tx_alert_lookup").set_value(str(n["distributor"])).run()
    next(b for b in at.button if b.label.startswith("Open case")).click().run()
    assert fingerprint(db) == before
    con = duckdb.connect(str(db))
    assert con.execute("SELECT count(*) FROM cases").fetchone()[0] == 1
    con.close()


@pytest.mark.skipif(not os.environ.get("BTGI_REAL_DB"), reason="set BTGI_REAL_DB to check the real database")
def test_real_database_baseline():
    con = duckdb.connect(os.environ["BTGI_REAL_DB"], read_only=True)
    one = lambda s: con.execute(s).fetchone()[0]
    assert (one("SELECT count(*) FROM transactions"), one("SELECT count(*) FROM edges"),
            one("SELECT count(*) FROM alerts")) == (203769, 234355, 7542)
    c = evaluate(con, 35, 49)["scopes"]["combined"]
    assert (c["tp"], c["fp"], c["tn"], c["fn"], c["unknown_label_flagged"]) == (17, 1467, 14120, 1066, 1986)
