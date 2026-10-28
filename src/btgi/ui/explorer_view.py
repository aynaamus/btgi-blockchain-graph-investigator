import pandas as pd
import streamlit as st

from .. import cases, explore
from . import common


def _neighbor_table(rows):
    return pd.DataFrame(rows).rename(columns={"neighbor": "neighbor tx", "edge_id": "edge id",
                                             "src_tx_id": "from", "dst_tx_id": "to"})


def render(con) -> None:
    st.header("Transaction Explorer")
    common.risk_notice()
    st.caption(common.ANON_NOTICE)
    text = st.text_input("Original transaction ID (txId)", key="tx_explorer")
    tx = common.parse_tx_id(text)
    if tx is None:
        st.write("Enter a transaction ID from the Elliptic data set to begin.")
        return
    t = explore.get_transaction(con, tx)
    if t is None:
        st.warning(f"Transaction {tx} was not found in the database.")
        return

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Transaction ID", t["tx_id"])
    c2.metric("Temporal step", t["time_step"])
    c3.metric("In-degree", t["in_degree"])
    c4.metric("Out-degree", t["out_degree"])
    st.button("Open in Graph Investigation", on_click=common.goto, args=("Graph Investigation", tx))

    alert = explore.get_alert(con, tx) if common.has_alerts(con) else None
    st.subheader("Structural alert")
    if alert is None:
        st.write("No structural alert for this transaction.")
    else:
        st.write(f"**{alert['severity']}** · structural prominence score {alert['risk_score']} · rules `{alert['rules']}`")
        for h in alert["hits"]:
            st.write(f"- Rule {h['rule_id']} ({h['direction']}): observed {h['observed']} > threshold "
                     f"{h['threshold']:g}; {h['evidence_total']} supporting "
                     f"{'paths' if h['rule_id'] == 'C' else 'edges'} (first {h['evidence_stored']} stored). "
                     f"Edge IDs: {', '.join('→'.join(map(str, e)) for e in h['evidence'][:10])}"
                     f"{' …' if len(h['evidence']) > 10 else ''}")
        st.caption("Open the Structural Alerts view for the full investigation and case workflow.")
    case = cases.get_case_by_tx(con, tx)
    if case:
        st.write(f"Case #{case['case_id']}: **{case['status']}**"
                 + (f" · disposition `{case['disposition']}`" if case["disposition"] else ""))

    st.subheader("Direct neighbors")
    limit = st.slider("Maximum neighbors per direction", 5, 200, 25, key="expl_limit")
    ci, co = st.columns(2)
    for col, direction, title in ((ci, "in", "Incoming (sources → this)"), (co, "out", "Outgoing (this → targets)")):
        rows = explore.neighbors(con, tx, direction, limit)
        total = t["in_degree"] if direction == "in" else t["out_degree"]
        with col:
            st.markdown(f"**{title}** — {total} total")
            if rows:
                st.dataframe(_neighbor_table(rows)[["neighbor tx", "edge id"]], hide_index=True, width="stretch")
                if total > len(rows):
                    st.caption(f"Showing {len(rows)} of {total}; raise the limit to see more.")
            else:
                st.write("None.")

    st.subheader("Bounded neighborhood")
    depth = st.radio("Depth", [1, 2], horizontal=True, key="expl_depth")
    nb = explore.neighborhood(con, tx, depth=depth, direction="both", max_nodes=200, max_edges=400)
    st.write(f"{len(nb['nodes'])} nodes and {len(nb['edges'])} edges within {depth} hop(s).")
    if nb["truncated"]:
        st.warning("Result truncated at 200 nodes / 400 edges; this is a partial neighborhood.")
    with st.expander("Edges (with supporting edge IDs)"):
        st.dataframe(pd.DataFrame(nb["edges"]), hide_index=True, width="stretch")

    st.subheader("Anonymized feature references")
    st.caption("Positions follow the original file order. Names are intentionally not assigned; "
               "'aggregated' columns may incorporate information from adjacent transactions.")
    feats = explore.get_features(con, tx)
    fmap = con.execute("SELECT position, column_name, documented_group FROM feature_map ORDER BY position").fetchall()
    rows = [{"position": 1, "column": "time_step", "documented group": "time_step", "value": t["time_step"]}]
    rows += [{"position": p, "column": c, "documented group": g, "value": feats.get(c)}
             for p, c, g in fmap if c in (feats or {})]
    with st.expander(f"All {len(rows)} feature values"):
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", height=320)

    with st.expander("Dataset annotation (not used by any rule or score)"):
        st.write(f"Original class: `{t['raw_class']}` → {t['label']}. "
                 "Unknown or missing annotations do not mean licit.")
