import duckdb
import pytest

from btgi import explore
from btgi.detect import DetectionConfig, run_detection
from btgi.evaluate import evaluate, metrics
from graphs import HELD, build

CFG = DetectionConfig(ref_last_step=2)


def labelled(con, spec):
    """spec: {name: label}; every other held-out node is licit."""
    def fn(g, n):
        lab = {t: "licit" for t, s in g.nodes.items() if s == HELD}
        lab.update({n[k]: v for k, v in spec.items()})
        return lab
    return build(con, fn)


def test_metrics_formulae_and_empty_cases():
    m = metrics(tp=2, fp=2, tn=4, fn=2)
    assert (m["precision"], m["recall"], m["f1"]) == (0.5, 0.5, 0.5)
    z = metrics(0, 0, 0, 0)
    assert z["precision"] is None and z["recall"] is None and z["f1"] is None
    assert metrics(0, 3, 5, 0)["precision"] == 0 and metrics(0, 3, 5, 0)["recall"] is None
    assert metrics(0, 3, 5, 2)["f1"] is None


def test_confusion_matrix_by_rule_and_combined(con):
    _, n = labelled(con, {"a_in_4": "illicit", "distributor": "illicit", "isolated": "illicit",
                          "b_in_2": "unknown", "big": "licit"})
    run_detection(con, CFG)
    r = evaluate(con, HELD, HELD)
    c = r["scopes"]["combined"]
    assert r["label_counts"]["unknown"] == 1
    assert c["tp"] == 2 and c["fn"] == 1                       # isolated illicit is unflagged
    known = r["known_labelled"]
    assert c["tp"] + c["fp"] + c["tn"] + c["fn"] == known       # unknown excluded from the matrix
    assert c["unknown_label_flagged"] == 1                      # b_in_2 flagged, no correctness assigned
    assert c["flagged_total"] == c["flagged_known"] + 1
    assert r["scopes"]["A"]["tp"] == 2 and r["scopes"]["B"]["tp"] == 0
    assert r["known_label_prevalence_illicit"] == pytest.approx(3 / known)
    assert c["precision"] == pytest.approx(c["tp"] / (c["tp"] + c["fp"]))


def test_unknown_is_not_licit(con):
    _, n = labelled(con, {"a_in_4": "unknown"})
    run_detection(con, CFG)
    a = evaluate(con, HELD, HELD)["scopes"]["A"]
    con2 = duckdb.connect(":memory:")
    labelled(con2, {"a_in_4": "licit"})
    run_detection(con2, CFG)
    b = evaluate(con2, HELD, HELD)["scopes"]["A"]
    assert a["unknown_label_flagged"] == 1 and b["unknown_label_flagged"] == 0
    assert b["fp"] == a["fp"] + 1            # only an explicit licit label counts as a false positive
    assert b["tn"] == a["tn"] and a["tp"] == b["tp"] == 0


def test_missing_class_row_counted_separately(con):
    build(con)                     # no labels at all
    run_detection(con, CFG)
    r = evaluate(con, HELD, HELD)
    assert r["known_labelled"] == 0 and r["label_counts"]["no_class_row"] == r["transactions"]
    c = r["scopes"]["combined"]
    assert c["precision"] is None and c["recall"] is None and c["alert_rate_known"] is None
    assert c["unknown_label_flagged"] == c["flagged_total"] > 0


def test_evaluation_empty_period_and_no_alerts(con):
    labelled(con, {})
    run_detection(con, CFG)
    r = evaluate(con, 40, 49)      # no transactions in this period
    assert r["transactions"] == 0 and r["scopes"]["combined"]["flagged_total"] == 0
    assert r["scopes"]["combined"]["alert_rate_all"] is None


def test_evaluation_restricted_to_period(con):
    labelled(con, {"a_in_4": "illicit"})
    run_detection(con, CFG)
    r = evaluate(con, 1, 2)        # reference period contains no held-out node
    assert r["scopes"]["combined"]["tp"] == 0


# ---- exploration (bounded, directional, evidence-linked) ----

def test_get_alert_and_missing_alert(con):
    _, n = build(con)
    run_detection(con, CFG)
    a = explore.get_alert(con, n["a_in_4"])
    assert a["severity"] == "low" and a["hits"][0]["evidence_total"] == 4
    assert all(len(e) == 1 for e in a["hits"][0]["evidence"])
    assert explore.get_alert(con, n["isolated"]) is None


def test_paths_directional_and_bounded(con):
    _, n = build(con)
    out2 = explore.paths(con, n["c_start"], "out", 2)
    assert {tuple(p["nodes"]) for p in out2} == {
        (n["c_start"], n["c_mid"], n["c_end1"]), (n["c_start"], n["c_mid"], n["c_end2"])}
    assert all(len(p["edge_ids"]) == 2 for p in out2)
    in2 = explore.paths(con, n["c_end1"], "in", 2)
    assert [p["nodes"] for p in in2] == [[n["c_end1"], n["c_mid"], n["c_start"]]]
    assert explore.paths(con, n["c_end1"], "out", 2) == []        # direction matters
    assert explore.paths(con, n["isolated"], "out", 3) == []
    assert len(explore.paths(con, n["c_start"], "out", 2, limit=1)) == 1
    with pytest.raises(ValueError):
        explore.paths(con, n["c_start"], "out", 4)
    with pytest.raises(ValueError):
        explore.paths(con, n["c_start"], "sideways", 2)


def test_three_hop_paths_exclude_cycles(con):
    _, n = build(con)
    # feeder -> both -> target: 2 edges; three-hop needs a further edge (none here)
    assert explore.paths(con, n["c_start"], "out", 3) == []


def test_top_nodes_and_step_stats(con):
    _, n = build(con)
    top = explore.top_nodes(con, "in", HELD, limit=2)
    assert top[0]["tx_id"] == n["big"] and top[0]["in_degree"] == 12 and len(top) == 2
    assert explore.top_nodes(con, "out", 99) == []
    assert explore.top_nodes(con, "in", limit=10**9)  # limit is capped, not unbounded
    s = explore.step_degree_stats(con, HELD)
    assert s["max_in"] == 12 and s["nodes"] > 0
    assert explore.step_degree_stats(con, 49)["nodes"] == 0


def test_neighborhood_three_hops_directional(con):
    _, n = build(con)
    r = explore.neighborhood(con, n["c_start"], depth=3, direction="out")
    assert set(r["nodes"]) == {n["c_start"], n["c_mid"], n["c_end1"], n["c_end2"]}
    r = explore.neighborhood(con, n["c_end1"], depth=3, direction="in")
    assert set(r["nodes"]) == {n["c_end1"], n["c_mid"], n["c_start"]}
