import duckdb

from btgi import explore
from btgi.schema import create_schema
from btgi.validate import validate


def test_validation_report(loaded):
    r = validate(loaded)
    assert r["total_nodes"] == 6 and r["total_edges"] == 5
    assert r["duplicate_transaction_ids"] == 0
    assert r["duplicate_directed_edges"] == 0
    assert r["self_loops"] == 0
    assert r["edges_with_missing_target_node"] == 0
    assert r["missing_feature_values"]["total_missing"] == 0
    assert r["transactions_per_time_step"] == {1: 3, 2: 2, 3: 1}
    assert r["label_distribution"] == {"illicit": 1, "licit": 2, "no_class_row": 1, "unknown": 2}
    assert r["cross_time_step_edges"] == 1
    assert r["structural_problems"] == []
    cc = r["connected_components"]
    assert cc["components"] == 2 and cc["largest_component"] == 5 and cc["isolated_nodes"] == 1
    assert r["degree"]["out_degree"]["max"] == 2


def test_validation_reports_but_keeps_anomalies(loaded):
    loaded.execute("INSERT INTO edges VALUES (50, 100, 101, 1), (51, 200, 200, 1)")
    r = validate(loaded)
    assert r["duplicate_directed_edges"] == 1
    assert r["self_loops"] == 1
    assert r["total_edges"] == 7  # nothing removed


def test_validation_detects_dangling_edges(loaded):
    loaded.execute("INSERT INTO edges VALUES (60, 100, 999, 1)")
    r = validate(loaded)
    assert r["edges_with_missing_target_node"] == 1
    assert "edges_with_missing_target_node" in r["structural_problems"]


def test_transaction_lookup(loaded):
    t = explore.get_transaction(loaded, 101)
    assert t["label"] == "licit" and t["in_degree"] == 1 and t["out_degree"] == 2
    assert explore.get_transaction(loaded, 300)["label"] == "no_class_row"
    assert explore.get_transaction(loaded, 12345) is None
    assert explore.get_features(loaded, 12345) is None


def test_direct_neighbors_with_edge_ids(loaded):
    out = explore.neighbors(loaded, 101, "out")
    assert {n["neighbor"] for n in out} == {102, 200}
    assert all(n["edge_id"] for n in out)
    assert [n["neighbor"] for n in explore.neighbors(loaded, 101, "in")] == [100]
    assert len(explore.neighbors(loaded, 101, "both")) == 3
    assert len(explore.neighbors(loaded, 101, "both", limit=1)) == 1


def test_neighborhood_depth_and_direction(loaded):
    n1 = explore.neighborhood(loaded, 100, depth=1, direction="out")
    assert set(n1["nodes"]) == {100, 101} and not n1["truncated"]
    n2 = explore.neighborhood(loaded, 100, depth=2, direction="out")
    assert set(n2["nodes"]) == {100, 101, 102, 200}
    n3 = explore.neighborhood(loaded, 100, depth=9, direction="out")  # depth capped
    assert n3["depth"] == explore.MAX_DEPTH and 201 in n3["nodes"]


def test_neighborhood_is_bounded(loaded):
    r = explore.neighborhood(loaded, 101, depth=3, max_nodes=3)
    assert len(r["nodes"]) <= 3 and r["truncated"]
    r = explore.neighborhood(loaded, 101, depth=3, max_edges=2)
    assert len(r["edges"]) <= 2 and r["truncated"]


def test_empty_results(loaded):
    assert explore.neighbors(loaded, 300) == []
    r = explore.neighborhood(loaded, 300, depth=3)
    assert r["nodes"] == {300: 0} and r["edges"] == []
    r = explore.neighborhood(loaded, 424242)
    assert r["nodes"] == {} and r["edges"] == []
    assert explore.step_connectivity(loaded, 49)["nodes"] == 0


def test_step_connectivity(loaded):
    s = explore.step_connectivity(loaded, 1)
    assert s["internal_edges"] == 3 and s["edges_leaving_step"] == 1
    assert s["components"]["components"] == 1
    assert explore.step_connectivity(loaded, 2)["edges_entering_step"] == 1


def test_empty_graph_validation():
    con = duckdb.connect(":memory:")
    create_schema(con)
    r = validate(con)
    assert r["total_nodes"] == 0 and r["total_edges"] == 0
    assert r["connected_components"]["components"] == 0
    assert r["label_distribution"] == {}
