"""Topology-only, label-free graph anomaly rules with explainable evidence.

Inputs are limited to `transactions` (tx_id, time_step) and `edges`. This module
must not read `tx_labels` or `tx_features`; thresholds come from the reference
period only (time_step <= ref_last_step).

Rules (each evaluated per direction; 'in' = incoming edges, 'out' = outgoing):
  A  degree outlier: degree > reference quantile of degree.
  B  concentrated fan: degree >= reference B-minimum, at or below the A
     threshold (so A and B never describe the same size outlier), and the share
     of neighbours whose only edge in that direction connects to this node is
     >= a reference quantile. ('in': feeders with out-degree 1; 'out': targets
     with in-degree 1.)
  C  two-hop reach outlier: number of distinct nodes reached by observed
     directed two-hop paths (a->m->c, c != a) exceeds a reference quantile,
     for nodes that are not already hit by A or B in that direction.

Rule hits describe observed topology only; they do not establish intent,
ownership, or illicit conduct.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import duckdb

from .detect_schema import create_detection_schema

DIRECTIONS = ("in", "out")
SEVERITY_MEDIUM = 0.5
SEVERITY_HIGH = 0.8


class DetectionError(Exception):
    pass


@dataclass(frozen=True)
class DetectionConfig:
    ref_last_step: int = 34          # reference period: steps 1..34
    a_quantile: float = 0.99
    b_min_quantile: float = 0.97
    b_excl_quantile: float = 0.90
    c_quantile: float = 0.99
    evidence_cap: int = 25
    score_scale: float = 0.25        # rule_score = min(1, scale * observed / threshold)
    version: str = "m2.1"

    def hash(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


def _scalar(con, sql, params=None):
    return con.execute(sql, params or []).fetchone()[0]


def _build_degrees(con) -> None:
    con.execute(
        """CREATE OR REPLACE TEMP TABLE d_deg AS
           SELECT t.tx_id, t.time_step,
                  coalesce(i.n, 0)::BIGINT AS indeg, coalesce(o.n, 0)::BIGINT AS outdeg
           FROM transactions t
           LEFT JOIN (SELECT dst_tx_id id, count(*) n FROM edges GROUP BY 1) i ON i.id = t.tx_id
           LEFT JOIN (SELECT src_tx_id id, count(*) n FROM edges GROUP BY 1) o ON o.id = t.tx_id"""
    )
    # exclusive neighbours: in-neighbours whose only out-edge is to this node,
    # out-neighbours whose only in-edge is from this node
    con.execute(
        """CREATE OR REPLACE TEMP TABLE d_excl_in AS
           SELECT e.dst_tx_id tx_id, count(*)::BIGINT n, count(*) FILTER (s.outdeg = 1)::BIGINT excl
           FROM edges e JOIN d_deg s ON s.tx_id = e.src_tx_id GROUP BY 1"""
    )
    con.execute(
        """CREATE OR REPLACE TEMP TABLE d_excl_out AS
           SELECT e.src_tx_id tx_id, count(*)::BIGINT n, count(*) FILTER (s.indeg = 1)::BIGINT excl
           FROM edges e JOIN d_deg s ON s.tx_id = e.dst_tx_id GROUP BY 1"""
    )
    con.execute(
        """CREATE OR REPLACE TEMP TABLE d_reach_out AS
           SELECT e1.src_tx_id tx_id, count(DISTINCT e2.dst_tx_id)::BIGINT reach, count(*)::BIGINT paths
           FROM edges e1 JOIN edges e2 ON e1.dst_tx_id = e2.src_tx_id
           WHERE e2.dst_tx_id <> e1.src_tx_id GROUP BY 1"""
    )
    con.execute(
        """CREATE OR REPLACE TEMP TABLE d_reach_in AS
           SELECT e2.dst_tx_id tx_id, count(DISTINCT e1.src_tx_id)::BIGINT reach, count(*)::BIGINT paths
           FROM edges e1 JOIN edges e2 ON e1.dst_tx_id = e2.src_tx_id
           WHERE e1.src_tx_id <> e2.dst_tx_id GROUP BY 1"""
    )


def _thresholds(con, cfg: DetectionConfig) -> list[tuple]:
    """Return (rule, direction, parameter, value, basis) from the reference period."""
    ref = "time_step <= ?"
    p = [cfg.ref_last_step]
    if _scalar(con, f"SELECT count(*) FROM d_deg WHERE {ref}", p) == 0:
        raise DetectionError(f"No transactions in reference period (steps <= {cfg.ref_last_step})")
    rows = []
    for d, col in (("in", "indeg"), ("out", "outdeg")):
        a = _scalar(con, f"SELECT quantile_disc({col}, {cfg.a_quantile}) FROM d_deg WHERE {ref}", p)
        bmin = max(2, _scalar(con, f"SELECT quantile_disc({col}, {cfg.b_min_quantile}) FROM d_deg WHERE {ref}", p))
        rho = con.execute(
            f"""SELECT quantile_disc(x.excl::DOUBLE / x.n, {cfg.b_excl_quantile}) FROM d_excl_{d} x
                JOIN d_deg g USING (tx_id)
                WHERE g.time_step <= ? AND x.n >= ? AND x.n <= ?""",
            [cfg.ref_last_step, bmin, a],
        ).fetchone()[0]
        reach = _scalar(
            con,
            f"""SELECT quantile_disc(coalesce(r.reach, 0), {cfg.c_quantile}) FROM d_deg g
                LEFT JOIN d_reach_{d} r USING (tx_id) WHERE g.time_step <= ?""",
            p,
        )
        basis = f"steps 1..{cfg.ref_last_step}, structure only"
        rows += [
            ("A", d, "degree_gt", float(a), f"quantile_disc q={cfg.a_quantile} of {d}-degree; {basis}"),
            ("B", d, "min_degree", float(bmin), f"max(2, quantile_disc q={cfg.b_min_quantile}); {basis}"),
            ("B", d, "max_degree", float(a), "equals rule A threshold (B excludes A-sized hubs)"),
            ("B", d, "exclusive_share_ge", None if rho is None else float(rho),
             f"quantile_disc q={cfg.b_excl_quantile} among reference nodes with min<=degree<=max; {basis}"),
            ("C", d, "two_hop_reach_gt", float(reach), f"quantile_disc q={cfg.c_quantile} of two-hop reach; {basis}"),
        ]
    return rows


def _find_hits(con, th: dict) -> None:
    con.execute(
        "CREATE OR REPLACE TEMP TABLE d_hits (tx_id BIGINT, rule_id VARCHAR, direction VARCHAR, "
        "observed BIGINT, threshold DOUBLE, evidence_total BIGINT, detail VARCHAR)"
    )
    for d, col in (("in", "indeg"), ("out", "outdeg")):
        a = th[("A", d, "degree_gt")]
        con.execute(
            f"INSERT INTO d_hits SELECT tx_id, 'A', '{d}', {col}, ?, {col}, "
            f"'{d}_degree=' || {col}::VARCHAR FROM d_deg WHERE {col} > ?",
            [a, a],
        )
        bmin, bmax, rho = th[("B", d, "min_degree")], th[("B", d, "max_degree")], th[("B", d, "exclusive_share_ge")]
        if rho is not None:
            con.execute(
                f"""INSERT INTO d_hits SELECT tx_id, 'B', '{d}', n, ?, n,
                           'exclusive_neighbours=' || excl::VARCHAR || '/' || n::VARCHAR
                    FROM d_excl_{d} WHERE n >= ? AND n <= ? AND excl::DOUBLE / n >= ?""",
                [bmin, bmin, bmax, rho],
            )
        c = th[("C", d, "two_hop_reach_gt")]
        con.execute(
            f"""INSERT INTO d_hits SELECT r.tx_id, 'C', '{d}', r.reach, ?, r.paths,
                       'distinct_two_hop_nodes=' || r.reach::VARCHAR || ' via ' || r.paths::VARCHAR || ' paths'
                FROM d_reach_{d} r
                WHERE r.reach > ?
                  AND NOT EXISTS (SELECT 1 FROM d_hits h WHERE h.tx_id = r.tx_id
                                  AND h.direction = '{d}' AND h.rule_id IN ('A','B'))""",
            [c, c],
        )


def _write_evidence(con, cfg: DetectionConfig) -> None:
    cap = int(cfg.evidence_cap)
    for d, col in (("in", "dst_tx_id"), ("out", "src_tx_id")):
        con.execute(
            f"""INSERT INTO alert_evidence
                SELECT tx_id, rule_id, direction, seq, edge_id, NULL FROM (
                  SELECT h.tx_id, h.rule_id, h.direction, e.edge_id,
                         row_number() OVER (PARTITION BY h.tx_id, h.rule_id ORDER BY e.edge_id)::INTEGER seq
                  FROM d_hits h JOIN edges e ON e.{col} = h.tx_id
                  WHERE h.direction = '{d}' AND h.rule_id IN ('A','B')) WHERE seq <= {cap}"""
        )
    # rule C: observed directed two-hop paths a->m->c with c != a
    con.execute(
        f"""INSERT INTO alert_evidence
            SELECT tx_id, 'C', direction, seq, e1, e2 FROM (
              SELECT h.tx_id, h.direction, e1.edge_id e1, e2.edge_id e2,
                     row_number() OVER (PARTITION BY h.tx_id ORDER BY e1.edge_id, e2.edge_id)::INTEGER seq
              FROM d_hits h
              JOIN edges e1 ON e1.src_tx_id = h.tx_id
              JOIN edges e2 ON e2.src_tx_id = e1.dst_tx_id AND e2.dst_tx_id <> e1.src_tx_id
              WHERE h.rule_id = 'C' AND h.direction = 'out') WHERE seq <= {cap}"""
    )
    con.execute(
        f"""INSERT INTO alert_evidence
            SELECT tx_id, 'C', direction, seq, e1, e2 FROM (
              SELECT h.tx_id, h.direction, e1.edge_id e1, e2.edge_id e2,
                     row_number() OVER (PARTITION BY h.tx_id ORDER BY e1.edge_id, e2.edge_id)::INTEGER seq
              FROM d_hits h
              JOIN edges e2 ON e2.dst_tx_id = h.tx_id
              JOIN edges e1 ON e1.dst_tx_id = e2.src_tx_id AND e1.src_tx_id <> e2.dst_tx_id
              WHERE h.rule_id = 'C' AND h.direction = 'in') WHERE seq <= {cap}"""
    )


def run_detection(con: duckdb.DuckDBPyConnection, cfg: DetectionConfig | None = None) -> dict:
    cfg = cfg or DetectionConfig()
    create_detection_schema(con)
    fp = con.execute(
        "SELECT dataset_fingerprint FROM ingestion_runs WHERE status='completed' "
        "ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    if fp is None:
        raise DetectionError("No completed ingestion found; run ingest first")
    fp, ch = fp[0], cfg.hash()
    prev = con.execute(
        "SELECT run_id, n_alerts FROM detection_runs WHERE dataset_fingerprint=? AND config_hash=?",
        [fp, ch],
    ).fetchone()
    if prev:
        return {"status": "already_computed", "run_id": prev[0], "n_alerts": prev[1]}

    run_id = _scalar(con, "SELECT coalesce(max(run_id), 0) + 1 FROM detection_runs")
    try:
        con.execute("BEGIN")
        for t in ("alert_evidence", "alert_rule_hits", "alerts", "rule_thresholds", "detection_runs"):
            con.execute(f"DELETE FROM {t}")
        _build_degrees(con)
        rows = _thresholds(con, cfg)
        con.executemany(
            "INSERT INTO rule_thresholds VALUES (?,?,?,?,?,?)", [(run_id, *r) for r in rows]
        )
        th = {(r, d, p): v for r, d, p, v, _ in rows}
        _find_hits(con, th)
        con.execute(
            f"""INSERT INTO alert_rule_hits
                SELECT h.tx_id, h.rule_id, h.direction, g.time_step, h.observed, h.threshold,
                       round(least(1.0, {cfg.score_scale} * h.observed / greatest(h.threshold, 1.0)), 6),
                       h.evidence_total, 0, h.detail
                FROM d_hits h JOIN d_deg g USING (tx_id)"""
        )
        _write_evidence(con, cfg)
        con.execute(
            """UPDATE alert_rule_hits h SET evidence_stored = s.n FROM
               (SELECT tx_id, rule_id, direction, count(*) n FROM alert_evidence GROUP BY ALL) s
               WHERE h.tx_id = s.tx_id AND h.rule_id = s.rule_id AND h.direction = s.direction"""
        )
        # noisy-or combination of rule scores; fixed severity bands
        con.execute(
            f"""INSERT INTO alerts
                SELECT tx_id, {run_id}, any_value(time_step), score,
                       CASE WHEN score >= {SEVERITY_HIGH} THEN 'high'
                            WHEN score >= {SEVERITY_MEDIUM} THEN 'medium' ELSE 'low' END,
                       n::INTEGER, rules
                FROM (SELECT tx_id, any_value(time_step) time_step,
                             round(1 - product(1 - rule_score), 4) score, count(*) n,
                             string_agg(rule_id || ':' || direction, ',' ORDER BY rule_id, direction) rules
                      FROM alert_rule_hits GROUP BY tx_id)
                GROUP BY ALL"""
        )
        n_alerts = _scalar(con, "SELECT count(*) FROM alerts")
        con.execute(
            "INSERT INTO detection_runs VALUES (?,?,?,?,?,?)",
            [run_id, datetime.now(timezone.utc).replace(tzinfo=None), fp, ch,
             json.dumps(asdict(cfg), sort_keys=True), n_alerts],
        )
        con.execute("COMMIT")
    except Exception:
        try:
            con.execute("ROLLBACK")
        except duckdb.Error:
            pass
        raise
    return {"status": "completed", "run_id": run_id, "n_alerts": n_alerts, "config_hash": ch}
