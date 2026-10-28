"""Bounded graph exploration queries.

Neighborhoods are expanded breadth-first with one SQL query per hop; only
the frontier and result set are held in memory, and hard caps apply to depth,
nodes and edges. Anonymized fields carry no wallet owners or payment amounts.
"""

from __future__ import annotations

import duckdb

MAX_DEPTH = 4
MAX_LIMIT = 10_000
DIRECTIONS = ("in", "out", "both")


def _check(direction: str, limit: int) -> int:
    if direction not in DIRECTIONS:
        raise ValueError(f"direction must be one of {DIRECTIONS}")
    if limit < 1:
        raise ValueError("limit must be >= 1")
    return min(limit, MAX_LIMIT)


def get_transaction(con, tx_id: int) -> dict | None:
    row = con.execute(
        """SELECT t.tx_id, t.time_step, l.raw_class, l.label,
                  (SELECT count(*) FROM edges WHERE dst_tx_id = t.tx_id),
                  (SELECT count(*) FROM edges WHERE src_tx_id = t.tx_id)
           FROM transactions t LEFT JOIN tx_labels l USING (tx_id) WHERE t.tx_id = ?""",
        [tx_id],
    ).fetchone()
    if row is None:
        return None
    return {
        "tx_id": row[0], "time_step": row[1], "raw_class": row[2],
        # None means the classes file had no row; it is not "licit".
        "label": row[3] if row[3] is not None else "no_class_row",
        "in_degree": row[4], "out_degree": row[5],
    }


def get_features(con, tx_id: int) -> dict | None:
    cur = con.execute("SELECT * FROM tx_features WHERE tx_id = ?", [tx_id])
    row = cur.fetchone()
    if row is None:
        return None
    return dict(zip([d[0] for d in cur.description], row))


def neighbors(con, tx_id: int, direction: str = "both", limit: int = 100) -> list[dict]:
    """Direct neighbors with the supporting edge id and edge direction."""
    limit = _check(direction, limit)
    parts = []
    if direction in ("out", "both"):
        parts.append(
            "SELECT edge_id, src_tx_id, dst_tx_id, dst_tx_id AS neighbor, 'out' AS dir "
            "FROM edges WHERE src_tx_id = $1"
        )
    if direction in ("in", "both"):
        parts.append(
            "SELECT edge_id, src_tx_id, dst_tx_id, src_tx_id AS neighbor, 'in' AS dir "
            "FROM edges WHERE dst_tx_id = $1"
        )
    sql = " UNION ALL ".join(parts) + f" ORDER BY edge_id LIMIT {limit}"
    return [
        {"edge_id": e, "src_tx_id": s, "dst_tx_id": d, "neighbor": n, "direction": dr}
        for e, s, d, n, dr in con.execute(sql, [tx_id]).fetchall()
    ]


def neighborhood(
    con, tx_id: int, depth: int = 2, direction: str = "both",
    max_nodes: int = 500, max_edges: int = 1000,
) -> dict:
    """Limited-depth neighborhood. `truncated` is True if a cap was hit."""
    max_edges = _check(direction, max_edges)
    max_nodes = min(max(max_nodes, 1), MAX_LIMIT)
    depth = max(0, min(depth, MAX_DEPTH))
    if get_transaction(con, tx_id) is None:
        return {"center": tx_id, "nodes": {}, "edges": [], "truncated": False}

    nodes = {tx_id: 0}  # tx_id -> hop distance
    edges: dict[int, tuple[int, int]] = {}
    frontier = [tx_id]
    truncated = False
    for hop in range(1, depth + 1):
        if not frontier or truncated:
            break
        cond = {
            "out": "src_tx_id IN (SELECT unnest($1))",
            "in": "dst_tx_id IN (SELECT unnest($1))",
            "both": "src_tx_id IN (SELECT unnest($1)) OR dst_tx_id IN (SELECT unnest($1))",
        }[direction]
        rows = con.execute(
            f"SELECT edge_id, src_tx_id, dst_tx_id FROM edges WHERE {cond} "
            f"ORDER BY edge_id LIMIT {max_edges + 1}",
            [frontier],
        ).fetchall()
        if len(rows) > max_edges:
            truncated = True
            rows = rows[:max_edges]
        nxt = []
        for eid, s, d in rows:
            if eid not in edges and len(edges) >= max_edges:
                truncated = True
                continue
            # only keep an edge if it stays within the node cap
            new = [n for n in (s, d) if n not in nodes]
            if len(nodes) + len(set(new)) > max_nodes:
                truncated = True
                continue
            edges[eid] = (s, d)
            for n in new:
                if n not in nodes:
                    nodes[n] = hop
                    nxt.append(n)
        frontier = nxt
    return {
        "center": tx_id, "depth": depth, "direction": direction,
        "nodes": nodes,
        "edges": [{"edge_id": e, "src_tx_id": s, "dst_tx_id": d} for e, (s, d) in sorted(edges.items())],
        "truncated": truncated,
    }


def degree_stats(con, tx_id: int | None = None) -> dict:
    if tx_id is not None:
        t = get_transaction(con, tx_id)
        return t and {"tx_id": tx_id, "in_degree": t["in_degree"], "out_degree": t["out_degree"]}
    row = con.execute(
        """SELECT max(i), avg(i), max(o), avg(o) FROM (
             SELECT t.tx_id,
                    (SELECT count(*) FROM edges WHERE dst_tx_id = t.tx_id) i,
                    (SELECT count(*) FROM edges WHERE src_tx_id = t.tx_id) o
             FROM transactions t)"""
    ).fetchone()
    return {"max_in": row[0], "mean_in": row[1], "max_out": row[2], "mean_out": row[3]}


def step_connectivity(con, time_step: int) -> dict:
    """Edge and component statistics for one temporal step."""
    from .validate import component_stats

    n = con.execute(
        "SELECT count(*) FROM transactions WHERE time_step = ?", [time_step]
    ).fetchone()[0]
    if n == 0:
        return {"time_step": time_step, "nodes": 0, "internal_edges": 0,
                "edges_leaving_step": 0, "edges_entering_step": 0, "components": None}
    internal = con.execute(
        "SELECT count(*) FROM edges e JOIN transactions a ON a.tx_id=e.src_tx_id "
        "JOIN transactions b ON b.tx_id=e.dst_tx_id WHERE a.time_step=? AND b.time_step=?",
        [time_step, time_step],
    ).fetchone()[0]
    leaving = con.execute(
        "SELECT count(*) FROM edges e JOIN transactions a ON a.tx_id=e.src_tx_id "
        "JOIN transactions b ON b.tx_id=e.dst_tx_id WHERE a.time_step=? AND b.time_step<>?",
        [time_step, time_step],
    ).fetchone()[0]
    entering = con.execute(
        "SELECT count(*) FROM edges e JOIN transactions a ON a.tx_id=e.src_tx_id "
        "JOIN transactions b ON b.tx_id=e.dst_tx_id WHERE b.time_step=? AND a.time_step<>?",
        [time_step, time_step],
    ).fetchone()[0]
    return {
        "time_step": time_step, "nodes": n, "internal_edges": internal,
        "edges_leaving_step": leaving, "edges_entering_step": entering,
        "components": component_stats(con, step=time_step),
    }


# ---- Milestone 2 additions -------------------------------------------------


def top_nodes(con, direction: str = "in", time_step: int | None = None, limit: int = 20) -> list[dict]:
    """Structurally significant nodes ranked by in- or out-degree (ties by tx_id)."""
    if direction not in ("in", "out"):
        raise ValueError("direction must be 'in' or 'out'")
    limit = _check("both", limit)
    col, other = ("dst_tx_id", "src_tx_id") if direction == "in" else ("src_tx_id", "dst_tx_id")
    where, params = ("WHERE t.time_step = ?", [time_step]) if time_step is not None else ("", [])
    rows = con.execute(
        f"""SELECT t.tx_id, t.time_step, count(e.edge_id) AS deg FROM transactions t
            JOIN edges e ON e.{col} = t.tx_id {where}
            GROUP BY t.tx_id, t.time_step ORDER BY deg DESC, t.tx_id LIMIT {limit}""",
        params,
    ).fetchall()
    return [{"tx_id": a, "time_step": b, f"{direction}_degree": c} for a, b, c in rows]


def paths(con, tx_id: int, direction: str = "out", hops: int = 2, limit: int = 100) -> list[dict]:
    """Observed directed paths of 2 or 3 edges starting ('out') or ending ('in')
    at tx_id, with the supporting edge ids. Cycles back to earlier nodes are
    excluded. Paths are observed topology only, not inferred payment flows.
    """
    if direction not in ("in", "out"):
        raise ValueError("direction must be 'in' or 'out'")
    if hops not in (2, 3):
        raise ValueError("hops must be 2 or 3")
    limit = _check("both", limit)
    if direction == "out":
        joins = "FROM edges e1 JOIN edges e2 ON e2.src_tx_id = e1.dst_tx_id"
        nodes = ["e1.src_tx_id", "e1.dst_tx_id", "e2.dst_tx_id"]
        where = "e1.src_tx_id = ?"
        if hops == 3:
            joins += " JOIN edges e3 ON e3.src_tx_id = e2.dst_tx_id"
            nodes.append("e3.dst_tx_id")
    else:
        joins = "FROM edges e1 JOIN edges e2 ON e2.dst_tx_id = e1.src_tx_id"
        nodes = ["e1.dst_tx_id", "e1.src_tx_id", "e2.src_tx_id"]
        where = "e1.dst_tx_id = ?"
        if hops == 3:
            joins += " JOIN edges e3 ON e3.dst_tx_id = e2.src_tx_id"
            nodes.append("e3.src_tx_id")
    n = len(nodes)
    distinct = " AND ".join(f"{nodes[i]} <> {nodes[j]}" for i in range(n) for j in range(i + 1, n))
    eids = ", ".join(f"e{i}.edge_id" for i in range(1, hops + 1))
    rows = con.execute(
        f"SELECT {', '.join(nodes)}, {eids} {joins} WHERE {where} AND {distinct} "
        f"ORDER BY {eids} LIMIT {limit}",
        [tx_id],
    ).fetchall()
    return [{"nodes": list(r[:n]), "edge_ids": list(r[n:])} for r in rows]


def get_alert(con, tx_id: int) -> dict | None:
    """Alert, rule hits and stored evidence for a transaction (None if no alert)."""
    a = con.execute(
        "SELECT tx_id, time_step, risk_score, severity, rules FROM alerts WHERE tx_id = ?", [tx_id]
    ).fetchone()
    if a is None:
        return None
    hits = con.execute(
        """SELECT rule_id, direction, observed_value, threshold_value, rule_score,
                  evidence_total, evidence_stored, detail
           FROM alert_rule_hits WHERE tx_id = ? ORDER BY rule_id, direction""",
        [tx_id],
    ).fetchall()
    out = {"tx_id": a[0], "time_step": a[1], "risk_score": a[2], "severity": a[3],
           "rules": a[4], "hits": []}
    for h in hits:
        ev = con.execute(
            "SELECT edge_id_1, edge_id_2 FROM alert_evidence WHERE tx_id=? AND rule_id=? "
            "AND direction=? ORDER BY seq",
            [tx_id, h[0], h[1]],
        ).fetchall()
        out["hits"].append({
            "rule_id": h[0], "direction": h[1], "observed": h[2], "threshold": h[3],
            "rule_score": h[4], "evidence_total": h[5], "evidence_stored": h[6],
            "detail": h[7], "evidence": [list(e) if e[1] is not None else [e[0]] for e in ev],
        })
    return out


def step_degree_stats(con, time_step: int) -> dict:
    """Degree summary for one temporal step."""
    row = con.execute(
        """SELECT count(*), max(i), avg(i), max(o), avg(o) FROM (
             SELECT t.tx_id,
                    (SELECT count(*) FROM edges WHERE dst_tx_id = t.tx_id) i,
                    (SELECT count(*) FROM edges WHERE src_tx_id = t.tx_id) o
             FROM transactions t WHERE t.time_step = ?)""",
        [time_step],
    ).fetchone()
    return {"time_step": time_step, "nodes": row[0], "max_in": row[1], "mean_in": row[2],
            "max_out": row[3], "mean_out": row[4]}


MAX_EVIDENCE_PAGE = 500


def evidence_page(con, tx_id: int, rule_id: str, direction: str, offset: int = 0, limit: int = 100) -> dict:
    """On-demand retrieval of the full supporting evidence for a rule hit.

    Rules A/B: the incident edges in `direction` (ordered by edge_id).
    Rule C: observed directed two-hop paths (a->m->c, c != a), ordered by
    (first edge id, second edge id). Matches the ordering of stored evidence,
    so the first rows equal the stored sample. Bounded by MAX_EVIDENCE_PAGE.
    """
    if direction not in ("in", "out"):
        raise ValueError("direction must be 'in' or 'out'")
    if rule_id not in ("A", "B", "C"):
        raise ValueError("rule_id must be A, B or C")
    limit = max(1, min(int(limit), MAX_EVIDENCE_PAGE))
    offset = max(0, int(offset))
    if rule_id in ("A", "B"):
        col = "dst_tx_id" if direction == "in" else "src_tx_id"
        total = con.execute(f"SELECT count(*) FROM edges WHERE {col} = ?", [tx_id]).fetchone()[0]
        rows = con.execute(
            f"SELECT edge_id, src_tx_id, dst_tx_id FROM edges WHERE {col} = ? "
            f"ORDER BY edge_id LIMIT {limit} OFFSET {offset}", [tx_id]).fetchall()
        items = [{"edge_ids": [e], "nodes": [s, d]} for e, s, d in rows]
    elif direction == "out":
        base = ("FROM edges e1 JOIN edges e2 ON e2.src_tx_id = e1.dst_tx_id "
                "AND e2.dst_tx_id <> e1.src_tx_id WHERE e1.src_tx_id = ?")
        total = con.execute(f"SELECT count(*) {base}", [tx_id]).fetchone()[0]
        rows = con.execute(
            f"SELECT e1.edge_id, e2.edge_id, e1.src_tx_id, e1.dst_tx_id, e2.dst_tx_id {base} "
            f"ORDER BY e1.edge_id, e2.edge_id LIMIT {limit} OFFSET {offset}", [tx_id]).fetchall()
        items = [{"edge_ids": [a, b], "nodes": [x, y, z]} for a, b, x, y, z in rows]
    else:
        base = ("FROM edges e2 JOIN edges e1 ON e1.dst_tx_id = e2.src_tx_id "
                "AND e1.src_tx_id <> e2.dst_tx_id WHERE e2.dst_tx_id = ?")
        total = con.execute(f"SELECT count(*) {base}", [tx_id]).fetchone()[0]
        rows = con.execute(
            f"SELECT e1.edge_id, e2.edge_id, e1.src_tx_id, e1.dst_tx_id, e2.dst_tx_id {base} "
            f"ORDER BY e1.edge_id, e2.edge_id LIMIT {limit} OFFSET {offset}", [tx_id]).fetchall()
        items = [{"edge_ids": [a, b], "nodes": [x, y, z]} for a, b, x, y, z in rows]
    return {"total": total, "offset": offset, "limit": limit, "items": items,
            "has_more": offset + len(items) < total}


def node_summaries(con, tx_ids: list[int]) -> dict[int, dict]:
    """Time step, degrees and (if present) alert severity for a bounded set of nodes."""
    ids = list(dict.fromkeys(int(t) for t in tx_ids))[:MAX_LIMIT]
    if not ids:
        return {}
    has_alerts = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = 'alerts'"
    ).fetchone()[0]
    sev = "(SELECT severity FROM alerts a WHERE a.tx_id = t.tx_id)" if has_alerts else "NULL"
    rows = con.execute(
        f"""SELECT t.tx_id, t.time_step,
                   (SELECT count(*) FROM edges WHERE dst_tx_id = t.tx_id),
                   (SELECT count(*) FROM edges WHERE src_tx_id = t.tx_id), {sev}
            FROM transactions t WHERE t.tx_id IN (SELECT unnest($1))""",
        [ids],
    ).fetchall()
    return {r[0]: {"time_step": r[1], "in_degree": r[2], "out_degree": r[3], "alert_severity": r[4]}
            for r in rows}
