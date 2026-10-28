import inspect
import random

import duckdb
import pytest

from btgi import detect, explore
from btgi.detect import DetectionConfig, DetectionError, run_detection
from graphs import HELD, G, build, held_out, load, reference

CFG = DetectionConfig(ref_last_step=2)


@pytest.fixture
def env(con):
    g, names = build(con)
    run_detection(con, CFG)
    return con, g, names


def rules_of(con, tx):
    r = con.execute("SELECT rules FROM alerts WHERE tx_id=?", [tx]).fetchone()
    return r[0] if r else None


def thr(con, rule, direction, param):
    return con.execute(
        "SELECT value FROM rule_thresholds WHERE rule_id=? AND direction=? AND parameter=?",
        [rule, direction, param],
    ).fetchone()[0]


def test_thresholds_come_from_reference_period(env):
    con, _, _ = env
    assert thr(con, "A", "in", "degree_gt") == 3 and thr(con, "A", "out", "degree_gt") == 1
    assert thr(con, "B", "in", "exclusive_share_ge") == 1.0
    assert thr(con, "B", "out", "exclusive_share_ge") is None  # no reference population


def test_rule_a_direction_and_boundary(env):
    con, _, n = env
    assert rules_of(con, n["a_in_4"]) == "A:in"            # 4 > 3
    assert rules_of(con, n["at_thr_3"]) != "A:in"          # == threshold: not strictly greater
    assert rules_of(con, n["distributor"]) == "A:out"      # direction preserved
    assert rules_of(con, n["both"]) == "A:in,A:out"
    h = con.execute(
        "SELECT observed_value, threshold_value, time_step FROM alert_rule_hits "
        "WHERE tx_id=? AND rule_id='A' AND direction='in'", [n["a_in_4"]]).fetchone()
    assert h == (4, 3.0, HELD)


def test_rule_b_concentrated_fan(env):
    con, _, n = env
    assert rules_of(con, n["at_thr_3"]) == "B:in"
    assert rules_of(con, n["b_in_2"]) == "B:in"             # lower boundary: degree == min
    assert rules_of(con, n["nonexcl_hub"]) is None          # feeder also feeds elsewhere
    # A-sized hubs are not also reported as B (no duplicate evidence)
    assert "B" not in rules_of(con, n["a_in_4"])
    d = con.execute("SELECT detail FROM alert_rule_hits WHERE tx_id=? AND rule_id='B'",
                    [n["at_thr_3"]]).fetchone()[0]
    assert d == "exclusive_neighbours=3/3"


def test_rule_c_two_hop_paths_are_observed_edges(env):
    con, _, n = env
    assert rules_of(con, n["c_start"]) == "C:out"
    paths = explore.get_alert(con, n["c_start"])["hits"][0]["evidence"]
    assert len(paths) == 2
    for e1, e2 in paths:
        a, m = con.execute("SELECT src_tx_id, dst_tx_id FROM edges WHERE edge_id=?", [e1]).fetchone()
        m2, c = con.execute("SELECT src_tx_id, dst_tx_id FROM edges WHERE edge_id=?", [e2]).fetchone()
        assert (a, m) == (n["c_start"], n["c_mid"]) and m2 == m and c != a
    # a node already hit by A in that direction is not duplicated by C
    assert rules_of(con, n["c_mid"]) == "A:out"
    # directional: end nodes see the path from the other side only
    assert rules_of(con, n["c_end1"]) is None


def test_overlapping_hits_make_one_alert_with_noisy_or_score(env):
    con, _, n = env
    assert con.execute("SELECT count(*) FROM alerts WHERE tx_id=?", [n["both"]]).fetchone()[0] == 1
    assert con.execute("SELECT count(*) - count(DISTINCT tx_id) FROM alerts").fetchone()[0] == 0
    a, b = 0.25 * 4 / 3, 0.25 * 3 / 1
    risk, sev, nh = con.execute(
        "SELECT risk_score, severity, n_hits FROM alerts WHERE tx_id=?", [n["both"]]).fetchone()
    assert risk == pytest.approx(round(1 - (1 - a) * (1 - b), 4)) and nh == 2 and sev == "high"


def test_risk_score_and_severity_bands(env):
    con, _, n = env
    assert con.execute("SELECT risk_score, severity FROM alerts WHERE tx_id=?",
                       [n["big"]]).fetchone() == (1.0, "high")        # capped at 1
    assert con.execute("SELECT severity FROM alerts WHERE tx_id=?", [n["a_in_4"]]).fetchone()[0] == "low"
    bad = con.execute("""SELECT count(*) FROM alerts WHERE severity <>
        CASE WHEN risk_score >= 0.8 THEN 'high' WHEN risk_score >= 0.5 THEN 'medium' ELSE 'low' END
    """).fetchone()[0]
    assert bad == 0


def test_evidence_traceability_and_cap(con):
    build(con)
    run_detection(con, DetectionConfig(ref_last_step=2, evidence_cap=2))
    # every evidence edge exists and touches the alerted node in the stated direction
    bad = con.execute("""
        SELECT count(*) FROM alert_evidence v JOIN edges e ON e.edge_id = v.edge_id_1
        WHERE v.rule_id IN ('A','B')
          AND NOT ((v.direction='in' AND e.dst_tx_id = v.tx_id) OR (v.direction='out' AND e.src_tx_id = v.tx_id))
    """).fetchone()[0]
    assert bad == 0
    assert con.execute("SELECT count(*) FROM alert_evidence v ANTI JOIN edges e ON e.edge_id=v.edge_id_1"
                       ).fetchone()[0] == 0
    assert con.execute("SELECT max(evidence_stored) FROM alert_rule_hits").fetchone()[0] == 2
    assert con.execute("SELECT count(*) FROM alert_rule_hits WHERE evidence_stored > evidence_total"
                       ).fetchone()[0] == 0
    # exact totals preserved when evidence is truncated
    assert con.execute("SELECT max(evidence_total) FROM alert_rule_hits WHERE rule_id='A'").fetchone()[0] == 12


def test_thresholds_ignore_held_out_period(con):
    g = G(); reference(g); held_out(g)
    load(con, g)
    run_detection(con, CFG)
    base = con.execute("SELECT rule_id,direction,parameter,value FROM rule_thresholds ORDER BY ALL").fetchall()
    con2 = duckdb.connect(":memory:")
    g2 = G(); reference(g2); held_out(g2)
    for _ in range(50):                       # extreme held-out hubs must not move thresholds
        h, _f = g2.fan_in(HELD, 40)
    load(con2, g2)
    run_detection(con2, CFG)
    assert con2.execute("SELECT rule_id,direction,parameter,value FROM rule_thresholds ORDER BY ALL"
                        ).fetchall() == base


def test_empty_reference_period_raises(con):
    build(con)
    with pytest.raises(DetectionError, match="reference"):
        run_detection(con, DetectionConfig(ref_last_step=0))
    assert con.execute("SELECT count(*) FROM alerts").fetchone()[0] == 0   # rolled back


def test_requires_completed_ingestion(con):
    from btgi.schema import create_schema
    create_schema(con)
    with pytest.raises(DetectionError, match="ingestion"):
        run_detection(con)


def test_detection_is_label_independent(con):
    def labels_a(g, n):
        return {t: "illicit" for t in g.nodes}
    g, n = build(con, labels_a)
    run_detection(con, CFG)
    a1 = con.execute("SELECT * FROM alerts ORDER BY tx_id").fetchall()
    h1 = con.execute("SELECT * FROM alert_rule_hits ORDER BY ALL").fetchall()

    con2 = duckdb.connect(":memory:")
    rnd = random.Random(0)
    build(con2, lambda g, n: {t: rnd.choice(["illicit", "licit", "unknown"]) for t in g.nodes})
    run_detection(con2, CFG)
    assert con2.execute("SELECT * FROM alerts ORDER BY tx_id").fetchall() == a1
    assert con2.execute("SELECT * FROM alert_rule_hits ORDER BY ALL").fetchall() == h1

    con3 = duckdb.connect(":memory:")
    build(con3)                                    # no labels at all
    run_detection(con3, CFG)
    assert con3.execute("SELECT * FROM alerts ORDER BY tx_id").fetchall() == a1


def test_detection_source_never_touches_labels_or_features():
    # skip the module docstring (which names the tables to say they are off limits)
    body = inspect.getsource(detect).split("from __future__", 1)[1]
    assert "tx_labels" not in body and "tx_features" not in body


def test_rerun_is_idempotent_and_does_not_touch_core_tables(con):
    build(con)
    core = lambda: [con.execute(f"SELECT count(*), sum(hash(t)::HUGEINT) FROM {t} t").fetchone()
                    for t in ("transactions", "edges", "tx_labels", "tx_features", "ingestion_runs")]
    before = core()
    r1 = run_detection(con, CFG)
    snap = [con.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall()
            for t in ("alerts", "alert_rule_hits", "alert_evidence", "rule_thresholds")]
    r2 = run_detection(con, CFG)
    assert r1["status"] == "completed" and r2["status"] == "already_computed"
    assert snap == [con.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall()
                    for t in ("alerts", "alert_rule_hits", "alert_evidence", "rule_thresholds")]
    # a changed config replaces the output rather than appending
    r3 = run_detection(con, DetectionConfig(ref_last_step=2, a_quantile=0.9))
    assert r3["status"] == "completed"
    assert con.execute("SELECT count(*) FROM detection_runs").fetchone()[0] == 1
    assert con.execute("SELECT count(*) - count(DISTINCT tx_id) FROM alerts").fetchone()[0] == 0
    assert core() == before


def test_deterministic_across_connections():
    out = []
    for _ in range(2):
        c = duckdb.connect(":memory:")
        build(c)
        run_detection(c, CFG)
        out.append([c.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall()
                    for t in ("alerts", "alert_rule_hits", "alert_evidence")])
    assert out[0] == out[1]
