"""Shared Streamlit helpers: connection, notices, session defaults."""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import streamlit as st

from ..cases import create_case_schema

RISK_NOTICE = (
    "Risk scores measure **structural prominence** in the transaction graph "
    "(unusual connectivity or path structure). They are **not** the probability of "
    "criminal activity, and the rules are not validated illicit-transaction detectors."
)
ANON_NOTICE = (
    "Transactions are anonymized: no wallet addresses, owners, payment amounts or locations "
    "are available, and directed paths do not prove ownership, control or money laundering."
)


def db_path() -> Path:
    env = os.environ.get("BTGI_DB")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[3] / "data" / "btgi.duckdb"


@st.cache_resource(show_spinner=False)
def _connect(path: str):
    con = duckdb.connect(path)
    create_case_schema(con)
    return con


def get_con():
    """Return the shared connection, or None (after showing an error) if unusable."""
    path = db_path()
    if not path.exists():
        st.error(f"Database not found: {path}. Run `python -m btgi ingest` and `python -m btgi detect` first.")
        return None
    try:
        con = _connect(str(path))
    except duckdb.Error as exc:
        st.error(f"Could not open the database (is another process using it?): {exc}")
        return None
    have = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    missing = {"transactions", "edges"} - have
    if missing:
        st.error("The database has no graph data. Run `python -m btgi ingest` first.")
        return None
    return con


def has_alerts(con) -> bool:
    return con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = 'alerts'"
    ).fetchone()[0] > 0 and con.execute("SELECT count(*) FROM alerts").fetchone()[0] > 0


def risk_notice() -> None:
    st.info(RISK_NOTICE)


def parse_tx_id(text: str) -> int | None:
    text = (text or "").strip().replace(",", "")
    if not text:
        return None
    if not text.isdigit() or int(text) >= 2**63:
        st.error("Transaction ID must be a positive whole number.")
        return None
    return int(text)


def goto(view: str, tx_id: int | None = None) -> None:
    """Navigate to another view, optionally carrying a transaction id."""
    st.session_state["nav"] = view
    if tx_id is not None:
        # widget keys are dropped while their view is not rendered, so seed each view's key
        for key in ("tx_explorer", "tx_graph", "tx_alert_lookup"):
            st.session_state[key] = str(tx_id)
