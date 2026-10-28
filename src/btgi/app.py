"""BTGI Streamlit application: structural investigation of the Elliptic transaction graph.

Launch: .venv/bin/streamlit run src/btgi/app.py
"""

import sys
from pathlib import Path

# Allow `streamlit run src/btgi/app.py` without an editable install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import streamlit as st  # noqa: E402

from btgi.ui import alerts_view, common, explorer_view, graph_view, overview_view, performance_view  # noqa: E402

VIEWS = {
    "Overview": overview_view.render,
    "Transaction Explorer": explorer_view.render,
    "Graph Investigation": graph_view.render,
    "Structural Alerts": alerts_view.render,
    "Detection Performance": performance_view.render,
}

st.set_page_config(page_title="BTGI – Transaction Graph Investigator", layout="wide")
st.session_state.setdefault("nav", "Overview")

with st.sidebar:
    st.title("BTGI")
    st.caption("Bitcoin Transaction Graph Investigator · Elliptic data set (CC BY-NC-ND 4.0)")
    st.radio("View", list(VIEWS), key="nav")
    st.text_input("Analyst identifier", value="analyst", key="analyst",
                  help="Recorded in the case audit trail.")
    st.caption("Structural analysis of anonymized data. Not a validated illicit-activity detector.")

con = common.get_con()
if con is not None:
    VIEWS[st.session_state["nav"]](con)
