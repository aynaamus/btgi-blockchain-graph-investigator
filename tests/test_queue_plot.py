import pytest

from btgi import alert_queue, cases, explore
from btgi.detect import DetectionConfig, run_detection
from btgi.graph_plot import build_figure, layout
from btgi.overview import overview
from graphs import build

CFG = DetectionConfig(ref_last_step=2)


@pytest.fixture
def env(con):
    _, n = build(con)
    run_detection(con, CFG)
    return con, n


def test_pagination_covers_every_alert_once(env):
    con, _ = env
    total = con.execute("SELECT count(*) FROM alerts").fetchone()[0]
    seen = []
    first = alert_queue.list_alerts(con, page_size=4)
    assert first["total"] == total and first["pages"] == -(-total // 4)
    for p in range(1, first["pages"] + 1):
        seen += [r["tx_id"] for r in alert_queue.list_alerts(con, page=p, page_size=4)["rows"]]
    assert len(seen) == len(set(seen)) == total
    # out-of-range pages clamp rather than fail or return junk
    assert alert_queue.list_alerts(con, page=999, page_size=4)["page"] == first["pages"]
    assert alert_queue.list_alerts(con, page=-3, page_size=4)["page"] == 1
    assert alert_queue.list_alerts(con, page_size=10**6)["page_size"] == alert_queue.MAX_PAGE_SIZE


def test_filters(env):
    con, n = env
    q = alert_queue.list_alerts
    assert {r["tx_id"] for r in q(con, rule="B", time_step=3)["rows"]} == {n["at_thr_3"], n["b_in_2"]}
    assert all("C" in r["rules"] for r in q(con, rule="C")["rows"])
    assert all(r["severity"] == "high" for r in q(con, severity="high")["rows"])
    assert all(r["time_step"] == 1 for r in q(con, time_step=1)["rows"])
    assert q(con, tx_id=n["both"])["total"] == 1
    assert q(con, rule="A", severity="low", time_step=3)["rows"][0]["tx_id"] == n["a_in_4"]
    assert q(con, time_step=49)["rows"] == [] and q(con, time_step=49)["pages"] == 1
    for bad in ({"rule": "Z"}, {"severity": "urgent"}, {"status": "weird"}, {"sort": "label"}):
        with pytest.raises(ValueError):
            q(con, **bad)


def test_status_filter_and_case_status_column(env):
    con, n = env
    assert alert_queue.list_alerts(con, status="no_case")["total"] == con.execute(
        "SELECT count(*) FROM alerts").fetchone()[0]          # before cases exist at all
    cases.create_case_schema(con)
    cid = cases.open_case(con, n["distributor"], "ana")
    assert [r["tx_id"] for r in alert_queue.list_alerts(con, status="open")["rows"]] == [n["distributor"]]
    cases.set_status(con, cid, "ana", "in_review")
    assert alert_queue.list_alerts(con, status="open")["rows"] == []
    assert alert_queue.list_alerts(con, status="in_review")["total"] == 1
    cases.close_case(con, cid, "ana", "inconclusive")
    row = alert_queue.list_alerts(con, tx_id=n["distributor"])["rows"][0]
    assert row["case_status"] == "closed"
    assert alert_queue.list_alerts(con, status="no_case")["total"] == alert_queue.list_alerts(con)["total"] - 1


def test_sorting_is_structural_and_deterministic(env):
    con, _ = env
    rows = alert_queue.list_alerts(con, sort="risk_desc", page_size=200)["rows"]
    scores = [r["risk_score"] for r in rows]
    assert scores == sorted(scores, reverse=True)
    assert alert_queue.list_alerts(con, sort="risk_desc", page_size=200)["rows"] == rows
    asc = [r["risk_score"] for r in alert_queue.list_alerts(con, sort="risk_asc", page_size=200)["rows"]]
    assert asc == sorted(asc)
    steps = [r["time_step"] for r in alert_queue.list_alerts(con, sort="step_desc", page_size=200)["rows"]]
    assert steps == sorted(steps, reverse=True)


def test_queue_never_reads_labels():
    import inspect
    assert "tx_labels" not in inspect.getsource(alert_queue)


def test_observed_vs_threshold_column(env):
    con, n = env
    r = alert_queue.list_alerts(con, tx_id=n["both"])["rows"][0]
    assert r["observed_vs_threshold"] == "A:in 4 > 3.0; A:out 3 > 1.0"


def test_evidence_page_complete_and_bounded(env):
    con, n = env
    p = explore.evidence_page(con, n["big"], "A", "in", 0, 5)
    assert p["total"] == 12 and len(p["items"]) == 5 and p["has_more"]
    p2 = explore.evidence_page(con, n["big"], "A", "in", 10, 5)
    assert len(p2["items"]) == 2 and not p2["has_more"]
    ids = [i["edge_ids"][0] for i in explore.evidence_page(con, n["big"], "A", "in", 0, 500)["items"]]
    assert ids == sorted(set(ids)) and len(ids) == 12
    assert all(i["nodes"][1] == n["big"] for i in p["items"])               # incoming edges end at the node
    assert explore.evidence_page(con, n["big"], "A", "in", 0, 10**6)["limit"] == explore.MAX_EVIDENCE_PAGE
    assert explore.evidence_page(con, n["isolated"], "A", "out")["items"] == []


def test_evidence_page_extends_stored_sample(con):
    build(con)
    run_detection(con, DetectionConfig(ref_last_step=2, evidence_cap=2))
    big = con.execute("SELECT tx_id FROM alerts ORDER BY risk_score DESC, tx_id LIMIT 1").fetchone()[0]
    hit = explore.get_alert(con, big)["hits"][0]
    assert hit["evidence_total"] > hit["evidence_stored"] == 2
    full = explore.evidence_page(con, big, hit["rule_id"], hit["direction"], 0, 500)
    assert full["total"] == hit["evidence_total"]
    assert [i["edge_ids"] for i in full["items"]][:2] == hit["evidence"]    # stored sample is a prefix


def test_evidence_page_rule_c_paths_match_alert(env):
    con, n = env
    hit = explore.get_alert(con, n["c_start"])["hits"][0]
    page = explore.evidence_page(con, n["c_start"], "C", "out", 0, 50)
    assert page["total"] == hit["evidence_total"] == 2
    assert [i["edge_ids"] for i in page["items"]] == hit["evidence"]
    assert all(len(i["nodes"]) == 3 and i["nodes"][0] == n["c_start"] for i in page["items"])
    inc = explore.evidence_page(con, n["c_end1"], "C", "in")
    assert inc["total"] == 1 and inc["items"][0]["nodes"][2] == n["c_end1"]
    for bad in (("A", "up"), ("Z", "in")):
        with pytest.raises(ValueError):
            explore.evidence_page(con, n["c_start"], *bad)


def test_node_summaries_bounded_and_missing(env):
    con, n = env
    s = explore.node_summaries(con, [n["both"], n["isolated"], 424242])
    assert set(s) == {n["both"], n["isolated"]}
    assert s[n["both"]]["in_degree"] == 4 and s[n["both"]]["out_degree"] == 3
    assert s[n["both"]]["alert_severity"] == "high" and s[n["isolated"]]["alert_severity"] is None
    assert explore.node_summaries(con, []) == {}


def test_figure_matches_neighborhood_and_direction(env):
    con, n = env
    res = explore.neighborhood(con, n["c_start"], depth=2, direction="out")
    meta = explore.node_summaries(con, list(res["nodes"]))
    fig = build_figure(res, meta)
    nodes_trace = fig.data[2]
    assert sorted(nodes_trace.customdata) == sorted(res["nodes"])
    assert len(fig.layout.annotations) == len(res["edges"]) == 3            # one arrow per real edge
    # arrow tips are at the target end of each edge
    pos = layout(res["nodes"])
    for e, ann in zip(res["edges"], fig.layout.annotations):
        tx, ty = pos[e["dst_tx_id"]]
        sx, sy = pos[e["src_tx_id"]]
        assert abs(ann.x - (0.88 * tx + 0.12 * sx)) < 1e-9
    assert "label" not in " ".join(nodes_trace.text).lower()
    empty = build_figure(explore.neighborhood(con, n["isolated"], 2), explore.node_summaries(con, [n["isolated"]]))
    assert len(empty.layout.annotations) == 0


def test_overview_stats(env):
    con, n = env
    o = overview(con)
    assert o["nodes"] == con.execute("SELECT count(*) FROM transactions").fetchone()[0]
    assert o["cross_step_edges"] == 0
    assert sum(c for _, c in o["steps"]) == o["nodes"]
    assert len(o["components"]["sizes"]) == o["components"]["components"]
    assert sum(o["components"]["sizes"]) == o["nodes"]
    assert o["alerts_total"] == sum(o["alerts_by_severity"].values())
    assert set(o["alert_tx_by_rule"]) == {"A", "B", "C"}
