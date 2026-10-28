"""SQL-based graph integrity checks. Read-only; nothing is removed or fixed."""

from __future__ import annotations

import duckdb

from .schema import FEATURE_COLUMNS

MAX_COMPONENT_EDGES = 5_000_000


def _one(con, sql):
    return con.execute(sql).fetchone()[0]


def missing_feature_values(con) -> dict:
    cols = ", ".join(f"count(*) - count({c})" for c in FEATURE_COLUMNS)
    row = con.execute(f"SELECT {cols} FROM tx_features").fetchone()
    per_col = {c: n for c, n in zip(FEATURE_COLUMNS, row) if n}
    return {"total_missing": sum(row), "columns_with_missing": per_col}


def degree_distribution(con) -> dict:
    """In/out degree summary plus a histogram of the most common degrees."""
    out = {}
    for name, col in (("in_degree", "dst_tx_id"), ("out_degree", "src_tx_id")):
        con.execute(
            f"""CREATE OR REPLACE TEMP TABLE _deg AS
                SELECT t.tx_id, count(e.edge_id) AS d
                FROM transactions t LEFT JOIN edges e ON e.{col} = t.tx_id
                GROUP BY t.tx_id"""
        )
        stats = con.execute(
            "SELECT min(d), max(d), avg(d), median(d), quantile_cont(d, 0.99) FROM _deg"
        ).fetchone()
        hist = con.execute(
            "SELECT d, count(*) FROM _deg GROUP BY d ORDER BY d LIMIT 10"
        ).fetchall()
        out[name] = {
            "min": stats[0], "max": stats[1], "mean": stats[2],
            "median": stats[3], "p99": stats[4],
            "histogram_lowest_10": dict(hist),
        }
        con.execute("DROP TABLE _deg")
    return out


class _UnionFind:
    def __init__(self):
        self.p: dict[int, int] = {}

    def find(self, x):
        p = self.p
        p.setdefault(x, x)
        root = x
        while p[root] != root:
            root = p[root]
        while p[x] != root:
            p[x], x = root, p[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


def component_stats(con, step: int | None = None, with_sizes: bool = False) -> dict | None:
    """Weakly connected components (edge direction ignored).

    Streams edges from DuckDB into a union-find over ints; returns None when
    the edge count exceeds MAX_COMPONENT_EDGES. If `step` is given, only
    edges inside that time step are used.
    """
    where = ""
    params: list = []
    node_where = ""
    if step is not None:
        where = (
            "JOIN transactions a ON a.tx_id = e.src_tx_id "
            "JOIN transactions b ON b.tx_id = e.dst_tx_id "
            "WHERE a.time_step = ? AND b.time_step = ?"
        )
        params = [step, step]
        node_where = "WHERE time_step = ?"
    n_edges = con.execute(f"SELECT count(*) FROM edges e {where}", params).fetchone()[0]
    if n_edges > MAX_COMPONENT_EDGES:
        return None
    n_nodes = con.execute(
        f"SELECT count(*) FROM transactions {node_where}", [step] if step is not None else []
    ).fetchone()[0]
    uf = _UnionFind()
    cur = con.execute(f"SELECT e.src_tx_id, e.dst_tx_id FROM edges e {where}", params)
    while chunk := cur.fetchmany(100_000):
        for a, b in chunk:
            uf.union(a, b)
    sizes: dict[int, int] = {}
    for x in list(uf.p):
        r = uf.find(x)
        sizes[r] = sizes.get(r, 0) + 1
    isolated = n_nodes - len(uf.p)
    comp_sizes = sorted(sizes.values(), reverse=True)
    out = {
        "nodes": n_nodes,
        "components": len(comp_sizes) + isolated,
        "isolated_nodes": isolated,
        "largest_component": comp_sizes[0] if comp_sizes else (1 if isolated else 0),
        "top5_sizes": comp_sizes[:5],
    }
    if with_sizes:
        out["sizes"] = comp_sizes + [1] * isolated
    return out


def validate(con: duckdb.DuckDBPyConnection, components: bool = True) -> dict:
    r: dict = {}
    r["total_nodes"] = _one(con, "SELECT count(*) FROM transactions")
    r["total_edges"] = _one(con, "SELECT count(*) FROM edges")
    r["duplicate_transaction_ids"] = _one(
        con, "SELECT count(*) - count(DISTINCT tx_id) FROM transactions"
    )
    r["duplicate_directed_edges"] = _one(
        con,
        "SELECT coalesce(sum(c - 1), 0) FROM "
        "(SELECT count(*) c FROM edges GROUP BY src_tx_id, dst_tx_id HAVING count(*) > 1)",
    )
    r["edges_with_missing_source_node"] = _one(
        con, "SELECT count(*) FROM edges e ANTI JOIN transactions t ON t.tx_id = e.src_tx_id"
    )
    r["edges_with_missing_target_node"] = _one(
        con, "SELECT count(*) FROM edges e ANTI JOIN transactions t ON t.tx_id = e.dst_tx_id"
    )
    r["self_loops"] = _one(con, "SELECT count(*) FROM edges WHERE src_tx_id = dst_tx_id")
    r["missing_feature_values"] = missing_feature_values(con)
    r["feature_rows_without_transaction"] = _one(
        con, "SELECT count(*) FROM tx_features f ANTI JOIN transactions t USING (tx_id)"
    )
    r["transactions_without_features"] = _one(
        con, "SELECT count(*) FROM transactions t ANTI JOIN tx_features f USING (tx_id)"
    )
    r["transactions_per_time_step"] = dict(
        con.execute(
            "SELECT time_step, count(*) FROM transactions GROUP BY 1 ORDER BY 1"
        ).fetchall()
    )
    # 'unknown' is an explicit source class; 'no_class_row' means the source
    # classes file had no entry. Neither is ever treated as licit.
    r["label_distribution"] = dict(
        con.execute(
            "SELECT coalesce(l.label, 'no_class_row'), count(*) FROM transactions t "
            "LEFT JOIN tx_labels l USING (tx_id) GROUP BY 1 ORDER BY 1"
        ).fetchall()
    )
    r["cross_time_step_edges"] = _one(
        con,
        "SELECT count(*) FROM edges e JOIN transactions a ON a.tx_id = e.src_tx_id "
        "JOIN transactions b ON b.tx_id = e.dst_tx_id WHERE a.time_step <> b.time_step",
    )
    r["degree"] = degree_distribution(con)
    if components:
        r["connected_components"] = component_stats(con)
    problems = [
        k for k in (
            "duplicate_transaction_ids", "edges_with_missing_source_node",
            "edges_with_missing_target_node", "feature_rows_without_transaction",
            "transactions_without_features",
        ) if r[k]
    ]
    # Duplicate edges, self-loops and missing values are reported, not errors.
    r["structural_problems"] = problems
    return r
