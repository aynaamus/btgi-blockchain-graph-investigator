"""Retrospective evaluation of graph-anomaly alerts against known labels.

This is the only Milestone 2 module that reads `tx_labels`. Labels are used
solely to score alerts after the fact; they never feed detection, thresholds
or risk scores. Unknown-label transactions are excluded from the confusion
matrix and counted separately without any correctness assignment.

The result is a retrospective, full-snapshot analysis of which structurally
unusual transactions coincide with annotated illicit ones. It is not evidence
of real-time detection capability.
"""

from __future__ import annotations

import duckdb


def _div(a, b):
    return None if not b else a / b


def metrics(tp: int, fp: int, tn: int, fn: int) -> dict:
    p, r = _div(tp, tp + fp), _div(tp, tp + fn)
    f1 = None if p is None or r is None or (p + r) == 0 else 2 * p * r / (p + r)
    return {"tp": tp, "fp": fp, "tn": tn, "fn": fn, "precision": p, "recall": r, "f1": f1}


def label_availability(con, first_step: int, last_step: int) -> dict:
    rows = con.execute(
        """SELECT coalesce(l.label, 'no_class_row'), count(*) FROM transactions t
           LEFT JOIN tx_labels l USING (tx_id) WHERE t.time_step BETWEEN ? AND ? GROUP BY 1""",
        [first_step, last_step],
    ).fetchall()
    return dict(sorted(rows))


def evaluate(con: duckdb.DuckDBPyConnection, first_step: int = 35, last_step: int = 49) -> dict:
    avail = label_availability(con, first_step, last_step)
    total = sum(avail.values())
    illicit, licit = avail.get("illicit", 0), avail.get("licit", 0)
    known = illicit + licit
    out = {
        "period": {"first_step": first_step, "last_step": last_step},
        "label_counts": avail,
        "transactions": total,
        "known_labelled": known,
        "known_label_prevalence_illicit": _div(illicit, known),
        "scopes": {},
    }
    scopes = {"combined": "TRUE", "A": "rule_id = 'A'", "B": "rule_id = 'B'", "C": "rule_id = 'C'"}
    for name, cond in scopes.items():
        rows = con.execute(
            f"""WITH flagged AS (
                  SELECT DISTINCT tx_id FROM alert_rule_hits WHERE {cond}),
                pop AS (
                  SELECT t.tx_id, l.label, f.tx_id IS NOT NULL AS flagged
                  FROM transactions t
                  LEFT JOIN tx_labels l USING (tx_id)
                  LEFT JOIN flagged f USING (tx_id)
                  WHERE t.time_step BETWEEN ? AND ?)
                SELECT coalesce(label, 'no_class_row'), flagged, count(*) FROM pop GROUP BY 1, 2""",
            [first_step, last_step],
        ).fetchall()
        c = {(lab, fl): n for lab, fl, n in rows}
        tp, fn = c.get(("illicit", True), 0), c.get(("illicit", False), 0)
        fp, tn = c.get(("licit", True), 0), c.get(("licit", False), 0)
        m = metrics(tp, fp, tn, fn)
        flagged_all = sum(n for (lab, fl), n in c.items() if fl)
        flagged_unknown = sum(n for (lab, fl), n in c.items() if fl and lab not in ("illicit", "licit"))
        m.update({
            "flagged_total": flagged_all,
            "alert_rate_all": _div(flagged_all, total),
            "flagged_known": tp + fp,
            "alert_rate_known": _div(tp + fp, known),
            "lift_vs_prevalence": _div(m["precision"], _div(illicit, known))
            if m["precision"] is not None else None,
            "unknown_label_flagged": flagged_unknown,
            "unknown_label_total": total - known,
        })
        out["scopes"][name] = m
    return out
