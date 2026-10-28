"""Plotly figure for a bounded neighborhood (rendered locally, no CDN needed).

Layout is deterministic: the start node sits at the centre and nodes are
placed on rings by hop distance. Only edges present in the database are
drawn; nothing is drawn between different temporal steps unless the data
contains such an edge (it does not for the Elliptic graph).
"""

from __future__ import annotations

import math

import plotly.graph_objects as go

HOP_COLORS = ["#1f3a5f", "#2f7d6d", "#c28a2c", "#8a5a9e", "#7a7a7a"]
SEVERITY_LINE = {"high": "#b3261e", "medium": "#d9822b", "low": "#c9b037"}


def layout(nodes: dict[int, int]) -> dict[int, tuple[float, float]]:
    """nodes: tx_id -> hop distance. Returns tx_id -> (x, y)."""
    pos: dict[int, tuple[float, float]] = {}
    by_hop: dict[int, list[int]] = {}
    for t, h in nodes.items():
        by_hop.setdefault(h, []).append(t)
    for h, ids in by_hop.items():
        ids.sort()
        if h == 0:
            for t in ids:
                pos[t] = (0.0, 0.0)
            continue
        for i, t in enumerate(ids):
            ang = 2 * math.pi * i / len(ids) + (h * 0.37)
            pos[t] = (h * math.cos(ang), h * math.sin(ang))
    return pos


def build_figure(result: dict, meta: dict[int, dict], height: int = 620) -> go.Figure:
    nodes, edges = result["nodes"], result["edges"]
    pos = layout(nodes)
    fig = go.Figure()
    ex, ey = [], []
    mx, my, mtext = [], [], []
    annotations = []
    for e in edges:
        (x0, y0), (x1, y1) = pos[e["src_tx_id"]], pos[e["dst_tx_id"]]
        ex += [x0, x1, None]
        ey += [y0, y1, None]
        mx.append((x0 + x1) / 2)
        my.append((y0 + y1) / 2)
        mtext.append(f"edge {e['edge_id']}: {e['src_tx_id']} → {e['dst_tx_id']}")
        if (x0, y0) != (x1, y1):
            annotations.append(dict(
                x=x1 * 0.88 + x0 * 0.12, y=y1 * 0.88 + y0 * 0.12, ax=x0 * 0.5 + x1 * 0.5,
                ay=y0 * 0.5 + y1 * 0.5, xref="x", yref="y", axref="x", ayref="y",
                showarrow=True, arrowhead=3, arrowsize=1.1, arrowwidth=1, arrowcolor="#8d99a6",
                text=""))
    fig.add_trace(go.Scatter(x=ex, y=ey, mode="lines", line=dict(width=0.8, color="#b8c1cc"),
                             hoverinfo="skip", showlegend=False))
    fig.add_trace(go.Scatter(x=mx, y=my, mode="markers", marker=dict(size=7, opacity=0),
                             text=mtext, hoverinfo="text", showlegend=False))
    ids = sorted(nodes)
    fig.add_trace(go.Scatter(
        x=[pos[t][0] for t in ids], y=[pos[t][1] for t in ids], mode="markers",
        customdata=ids, showlegend=False, hoverinfo="text",
        text=[
            f"tx {t}<br>hop {nodes[t]} · step {meta.get(t, {}).get('time_step', '?')}"
            f"<br>in {meta.get(t, {}).get('in_degree', '?')} · out {meta.get(t, {}).get('out_degree', '?')}"
            + (f"<br>structural alert: {meta[t]['alert_severity']}" if meta.get(t, {}).get("alert_severity") else "")
            for t in ids],
        marker=dict(
            size=[22 if nodes[t] == 0 else 13 for t in ids],
            color=[HOP_COLORS[min(nodes[t], len(HOP_COLORS) - 1)] for t in ids],
            symbol=["star" if nodes[t] == 0 else ("diamond" if meta.get(t, {}).get("alert_severity") else "circle")
                    for t in ids],
            line=dict(width=2.5, color=[SEVERITY_LINE.get(meta.get(t, {}).get("alert_severity"), "#ffffff")
                                         for t in ids])),
    ))
    fig.update_layout(
        annotations=annotations, height=height, margin=dict(l=10, r=10, t=10, b=10),
        xaxis=dict(visible=False), yaxis=dict(visible=False, scaleanchor="x"),
        plot_bgcolor="white", hovermode="closest", dragmode="pan", clickmode="event+select")
    return fig
