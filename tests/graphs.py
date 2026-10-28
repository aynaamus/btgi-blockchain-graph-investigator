"""Deterministic synthetic graphs for detection tests (no CSV round trip).

Reference period = steps 1-2, held-out period = step 3.

Reference (step 1): 200 pairs p->q, 10 chains u->v->w, 4 fan-in hubs with 3
exclusive feeders each, 1 fan-in hub with 6 exclusive feeders. Hence, with the
default quantiles, in-degree threshold (rule A) is 3 and out-degree threshold is 1.
Step 2 holds a few extra pairs.
"""

from __future__ import annotations

import duckdb

from btgi.schema import create_schema

HELD = 3


class G:
    def __init__(self):
        self.nodes: dict[int, int] = {}
        self.edges: list[tuple[int, int]] = []
        self._next = 1000

    def node(self, step: int) -> int:
        self._next += 1
        self.nodes[self._next] = step
        return self._next

    def edge(self, a: int, b: int) -> None:
        self.edges.append((a, b))

    def fan_in(self, step, k, extra_out=None):
        hub = self.node(step)
        feeders = [self.node(step) for _ in range(k)]
        for f in feeders:
            self.edge(f, hub)
        return hub, feeders


def reference(g: G) -> None:
    for _ in range(200):
        g.edge(g.node(1), g.node(1))
    for _ in range(10):
        u, v, w = g.node(1), g.node(1), g.node(1)
        g.edge(u, v), g.edge(v, w)
    for _ in range(4):
        g.fan_in(1, 3)
    g.fan_in(1, 6)
    for _ in range(5):
        g.edge(g.node(2), g.node(2))


def held_out(g: G) -> dict:
    """Held-out structures; returns named node ids."""
    n = {}
    n["a_in_4"], _ = g.fan_in(HELD, 4)            # in-degree 4 > 3  -> A:in
    n["at_thr_3"], _ = g.fan_in(HELD, 3)          # in-degree 3 == threshold, exclusive -> B:in only
    n["b_in_2"], _ = g.fan_in(HELD, 2)            # in-degree 2, exclusive -> B:in
    hub, feeders = g.fan_in(HELD, 3)              # in-degree 3, one feeder also feeds elsewhere
    other = g.node(HELD)
    g.edge(feeders[0], other)                     # feeder out-degree 2 -> A:out for the feeder
    n["nonexcl_hub"], n["nonexcl_feeder"] = hub, feeders[0]
    d = g.node(HELD)                              # distributor: out-degree 3 -> A:out (not A:in)
    for _ in range(3):
        g.edge(d, g.node(HELD))
    n["distributor"] = d
    x, y, z1, z2 = (g.node(HELD) for _ in range(4))   # x->y->{z1,z2}
    g.edge(x, y), g.edge(y, z1), g.edge(y, z2)
    n["c_start"], n["c_mid"], n["c_end1"], n["c_end2"] = x, y, z1, z2
    m, _ = g.fan_in(HELD, 4)                      # in 4, plus out 3 -> A:in + A:out
    for _ in range(3):
        g.edge(m, g.node(HELD))
    n["both"] = m
    big, _ = g.fan_in(HELD, 12)                   # 12 >= 4x threshold -> score 1.0 high
    n["big"] = big
    n["isolated"] = g.node(HELD)
    return n


def load(con: duckdb.DuckDBPyConnection, g: G, labels: dict[int, str] | None = None) -> None:
    create_schema(con)
    con.execute(
        "INSERT INTO ingestion_runs (run_id, started_at, finished_at, status, dataset_fingerprint) "
        "VALUES (1, now()::TIMESTAMP, now()::TIMESTAMP, 'completed', 'test-fp')"
    )
    con.executemany("INSERT INTO transactions VALUES (?,?,1)", list(g.nodes.items()))
    edges = sorted(g.edges)
    con.executemany(
        "INSERT INTO edges VALUES (?,?,?,1)", [(i + 1, a, b) for i, (a, b) in enumerate(edges)]
    )
    code = {"illicit": "1", "licit": "2", "unknown": "unknown"}
    rows = [(t, code[lab], lab) for t, lab in (labels or {}).items()]
    if rows:
        con.executemany("INSERT INTO tx_labels VALUES (?,?,?)", rows)


def build(con, labels_fn=None):
    g = G()
    reference(g)
    names = held_out(g)
    labels = labels_fn(g, names) if labels_fn else None
    load(con, g, labels)
    return g, names
