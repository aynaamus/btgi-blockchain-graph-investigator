import pandas as pd
import streamlit as st

from .. import overview as ov
from . import common


@st.cache_data(show_spinner="Computing graph statistics…")
def _stats(_con, path: str):
    return ov.overview(_con)


def render(con) -> None:
    st.header("Overview")
    st.caption("Graph structure and structural alerts. Structure is not an illicit-activity classification.")
    common.risk_notice()
    o = _stats(con, str(common.db_path()))

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Transaction nodes", f"{o['nodes']:,}")
    c2.metric("Directed edges", f"{o['edges']:,}")
    c3.metric("Temporal steps", len(o["steps"]))
    cc = o["components"]
    c4.metric("Connected components", f"{cc['components']:,}")

    left, right = st.columns(2)
    with left:
        st.subheader("Transactions per temporal step")
        st.bar_chart(pd.DataFrame(o["steps"], columns=["step", "transactions"]).set_index("step"))
    with right:
        st.subheader("Connected components")
        sizes = cc["sizes"]
        st.write(
            f"{cc['components']:,} weakly connected components · largest {cc['largest_component']:,} nodes · "
            f"smallest {min(sizes):,} · isolated nodes {cc['isolated_nodes']:,}"
        )
        st.write(f"Edges between different temporal steps: **{o['cross_step_edges']:,}**."
                 + (" The components therefore correspond to the temporal steps; no links exist between steps."
                    if o["cross_step_edges"] == 0 and cc["components"] == len(o["steps"]) else ""))
        st.dataframe(pd.DataFrame({"component size": sorted(sizes, reverse=True)[:10]}),
                     hide_index=True, width="stretch")

    st.subheader("Degree statistics")
    rows = []
    for name in ("in_degree", "out_degree"):
        d = o["degree"][name]
        rows.append({"direction": name.replace("_", " "), "min": d["min"], "median": d["median"],
                     "mean": round(d["mean"], 3), "p99": d["p99"], "max": d["max"]})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

    st.subheader("Structural alerts")
    if o["alerts_total"] is None:
        st.warning("No alerts found. Run `python -m btgi detect`.")
        return
    a1, a2, a3 = st.columns(3)
    a1.metric("Unique alerted transactions", f"{o['alerts_total']:,}")
    sev = o["alerts_by_severity"]
    with a2:
        st.caption("Severity (structural prominence band)")
        st.bar_chart(pd.DataFrame({"alerts": [sev.get(s, 0) for s in ("low", "medium", "high")]},
                                  index=["low", "medium", "high"]))
    with a3:
        st.caption("Transactions by rule (a transaction can match several rules)")
        st.bar_chart(pd.DataFrame({"transactions": o["alert_tx_by_rule"]}))
