import pandas as pd
import streamlit as st

from .. import explore
from ..graph_plot import build_figure
from . import common

MAX_NODES_CAP = 400
MAX_EDGES_CAP = 800


def render(con) -> None:
    st.header("Graph Investigation")
    common.risk_notice()
    st.caption(common.ANON_NOTICE + " Only edges stored in the database are drawn; "
               "the graph has no links between temporal steps.")
    text = st.text_input("Starting transaction ID", key="tx_graph")
    start = common.parse_tx_id(text)
    c1, c2, c3 = st.columns(3)
    direction = c1.radio("Direction", ["both", "out", "in"], horizontal=True,
                         format_func={"both": "Both", "out": "Outgoing →", "in": "← Incoming"}.get)
    hops = c2.radio("Hops", [1, 2, 3], horizontal=True)
    max_nodes = c3.slider("Max nodes", 10, MAX_NODES_CAP, 120)
    max_edges = st.slider("Max edges", 10, MAX_EDGES_CAP, 240)
    if start is None:
        st.write("Enter a transaction ID to explore its directed neighborhood.")
        return
    if explore.get_transaction(con, start) is None:
        st.warning(f"Transaction {start} was not found in the database.")
        return

    res = explore.neighborhood(con, start, depth=hops, direction=direction,
                               max_nodes=max_nodes, max_edges=max_edges)
    if len(res["nodes"]) == 1:
        st.info("This transaction has no edges in the selected direction.")
    if res["truncated"]:
        st.warning(f"Result truncated at the limits ({max_nodes} nodes / {max_edges} edges). "
                   "This is a partial neighborhood; reduce hops or raise the caps to see more.")
    meta = explore.node_summaries(con, list(res["nodes"]))
    steps = {m["time_step"] for m in meta.values()}
    st.caption(f"{len(res['nodes'])} nodes · {len(res['edges'])} edges · temporal step(s) {sorted(steps)}")

    fig = build_figure(res, meta)
    event = st.plotly_chart(fig, on_select="rerun", selection_mode="points", key="graph_chart",
                            width="stretch")
    st.caption("★ start node · ◆ node with a structural alert (outline = severity) · node colour = hop distance · "
               "arrows follow edge direction (source → target).")

    picked = None
    try:
        pts = event.selection.points if event is not None else []
        if pts and pts[0].get("customdata") is not None:
            cd = pts[0]["customdata"]
            picked = int(cd[0] if isinstance(cd, (list, tuple)) else cd)
    except (AttributeError, KeyError, TypeError, ValueError):
        picked = None
    options = sorted(res["nodes"])
    default = options.index(picked) if picked in options else 0
    node = st.selectbox("Inspect node", options, index=default,
                        format_func=lambda t: f"{t}  (hop {res['nodes'][t]}, step {meta.get(t, {}).get('time_step', '?')})")
    m = meta.get(node, {})
    d1, d2, d3, d4 = st.columns(4)
    d1.metric("Selected transaction", node)
    d2.metric("Step", m.get("time_step", "?"))
    d3.metric("In-degree", m.get("in_degree", "?"))
    d4.metric("Out-degree", m.get("out_degree", "?"))
    b1, b2 = st.columns(2)
    b1.button("Re-centre graph on this node", on_click=common.goto, args=("Graph Investigation", node),
              disabled=node == start)
    b2.button("Open in Transaction Explorer", on_click=common.goto, args=("Transaction Explorer", node))

    with st.expander("Supporting edges (edge ID, source → target)", expanded=True):
        st.dataframe(pd.DataFrame(res["edges"]).rename(columns={"edge_id": "edge id", "src_tx_id": "from",
                                                                "dst_tx_id": "to"}),
                     hide_index=True, width="stretch", height=240)

    st.subheader("Observed directed paths")
    st.caption("Paths are sequences of existing directed edges. They describe topology only and do not "
               "indicate payment flow, intent, ownership, control or money laundering.")
    path_hops = st.radio("Path length (edges)", [2, 3], horizontal=True)
    for d in (["out", "in"] if direction == "both" else [direction]):
        rows = explore.paths(con, start, d, path_hops, limit=100)
        label = "starting at" if d == "out" else "ending at"
        st.markdown(f"**{len(rows)}{'+' if len(rows) == 100 else ''} paths {label} {start}** "
                    f"({'outgoing' if d == 'out' else 'incoming'}, up to 100 shown)")
        if rows:
            st.dataframe(pd.DataFrame({"nodes": [" → ".join(map(str, r["nodes"])) if d == "out"
                                                  else " → ".join(map(str, reversed(r["nodes"]))) for r in rows],
                                       "edge IDs": [", ".join(map(str, r["edge_ids"])) for r in rows]}),
                         hide_index=True, width="stretch", height=240)
