"""Aggregate statistics for the application overview (plain data, no UI)."""

from __future__ import annotations

from .validate import component_stats, degree_distribution


def overview(con) -> dict:
    one = lambda sql: con.execute(sql).fetchone()[0]
    steps = con.execute(
        "SELECT time_step, count(*) FROM transactions GROUP BY 1 ORDER BY 1").fetchall()
    out = {
        "nodes": one("SELECT count(*) FROM transactions"),
        "edges": one("SELECT count(*) FROM edges"),
        "steps": steps,
        "cross_step_edges": one(
            "SELECT count(*) FROM edges e JOIN transactions a ON a.tx_id = e.src_tx_id "
            "JOIN transactions b ON b.tx_id = e.dst_tx_id WHERE a.time_step <> b.time_step"),
        "degree": degree_distribution(con),
    }
    cc = component_stats(con, with_sizes=True)
    out["components"] = cc
    has_alerts = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = 'alerts'").fetchone()[0]
    if has_alerts:
        out["alerts_total"] = one("SELECT count(*) FROM alerts")
        out["alerts_by_severity"] = dict(con.execute(
            "SELECT severity, count(*) FROM alerts GROUP BY 1").fetchall())
        out["alert_hits_by_rule"] = dict(con.execute(
            "SELECT rule_id, count(*) FROM alert_rule_hits GROUP BY 1 ORDER BY 1").fetchall())
        out["alert_tx_by_rule"] = dict(con.execute(
            "SELECT rule_id, count(DISTINCT tx_id) FROM alert_rule_hits GROUP BY 1 ORDER BY 1").fetchall())
        out["alerts_by_step"] = con.execute(
            "SELECT time_step, count(*) FROM alerts GROUP BY 1 ORDER BY 1").fetchall()
    else:
        out["alerts_total"] = None
    return out
