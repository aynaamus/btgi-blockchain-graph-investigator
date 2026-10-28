"""Database-backed alert queue: filtering, sorting and pagination.

Ordering uses only structural alert fields (never labels). Case status is
joined from the `cases` table when it exists.
"""

from __future__ import annotations

import duckdb

MAX_PAGE_SIZE = 200
CASE_STATUSES = ("no_case", "open", "in_review", "closed")
SORTS = {
    "risk_desc": "a.risk_score DESC, a.tx_id",
    "risk_asc": "a.risk_score ASC, a.tx_id",
    "step_asc": "a.time_step ASC, a.risk_score DESC, a.tx_id",
    "step_desc": "a.time_step DESC, a.risk_score DESC, a.tx_id",
    "tx_id": "a.tx_id",
    "hits_desc": "a.n_hits DESC, a.risk_score DESC, a.tx_id",
}


def _has_cases(con) -> bool:
    return con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = 'cases'"
    ).fetchone()[0] > 0


def _where(con, rule, severity, time_step, status, tx_id):
    joins, conds, params = "", [], []
    if _has_cases(con):
        joins = "LEFT JOIN cases c ON c.tx_id = a.tx_id"
        status_expr = "coalesce(c.status, 'no_case')"
    else:
        status_expr = "'no_case'"
    if rule:
        if rule not in ("A", "B", "C"):
            raise ValueError("rule must be A, B or C")
        conds.append("EXISTS (SELECT 1 FROM alert_rule_hits h WHERE h.tx_id = a.tx_id AND h.rule_id = ?)")
        params.append(rule)
    if severity:
        if severity not in ("low", "medium", "high"):
            raise ValueError("severity must be low, medium or high")
        conds.append("a.severity = ?")
        params.append(severity)
    if time_step is not None:
        conds.append("a.time_step = ?")
        params.append(int(time_step))
    if status:
        if status not in CASE_STATUSES:
            raise ValueError(f"status must be one of {CASE_STATUSES}")
        conds.append(f"{status_expr} = ?")
        params.append(status)
    if tx_id is not None:
        conds.append("a.tx_id = ?")
        params.append(int(tx_id))
    return joins, status_expr, (" WHERE " + " AND ".join(conds)) if conds else "", params


def list_alerts(
    con: duckdb.DuckDBPyConnection, *, rule=None, severity=None, time_step=None,
    status=None, tx_id=None, sort="risk_desc", page=1, page_size=25,
) -> dict:
    """Return {'rows', 'total', 'page', 'pages', 'page_size'}."""
    if sort not in SORTS:
        raise ValueError(f"sort must be one of {sorted(SORTS)}")
    page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
    joins, status_expr, where, params = _where(con, rule, severity, time_step, status, tx_id)
    total = con.execute(f"SELECT count(*) FROM alerts a {joins}{where}", params).fetchone()[0]
    pages = max(1, -(-total // page_size))
    page = max(1, min(int(page), pages))
    cur = con.execute(
        f"""SELECT a.tx_id, a.time_step, a.risk_score, a.severity, a.rules, a.n_hits,
                   {status_expr} AS case_status,
                   (SELECT string_agg(h.rule_id || ':' || h.direction || ' ' || h.observed_value::VARCHAR
                                      || ' > ' || h.threshold_value::VARCHAR, '; ' ORDER BY h.rule_id, h.direction)
                    FROM alert_rule_hits h WHERE h.tx_id = a.tx_id) AS observed_vs_threshold
            FROM alerts a {joins}{where}
            ORDER BY {SORTS[sort]} LIMIT {page_size} OFFSET {(page - 1) * page_size}""",
        params,
    )
    cols = [d[0] for d in cur.description]
    return {"rows": [dict(zip(cols, r)) for r in cur.fetchall()], "total": total,
            "page": page, "pages": pages, "page_size": page_size}


def filter_options(con) -> dict:
    steps = [r[0] for r in con.execute("SELECT DISTINCT time_step FROM alerts ORDER BY 1").fetchall()]
    return {"steps": steps, "rules": ["A", "B", "C"], "severities": ["low", "medium", "high"],
            "statuses": list(CASE_STATUSES)}
