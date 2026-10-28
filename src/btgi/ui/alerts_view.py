import pandas as pd
import streamlit as st

from .. import alert_queue, cases, explore
from . import common

SORT_LABELS = {"risk_desc": "Structural score (high → low)", "risk_asc": "Structural score (low → high)",
               "step_asc": "Temporal step (asc)", "step_desc": "Temporal step (desc)",
               "hits_desc": "Number of rule hits", "tx_id": "Transaction ID"}
PAGE = 100  # evidence rows fetched per on-demand request


def _queue(con) -> int | None:
    opts = alert_queue.filter_options(con)
    f1, f2, f3, f4, f5 = st.columns(5)
    rule = f1.selectbox("Rule", [None] + opts["rules"], format_func=lambda v: "All" if v is None else v)
    sev = f2.selectbox("Severity", [None] + opts["severities"], format_func=lambda v: "All" if v is None else v)
    step = f3.selectbox("Temporal step", [None] + opts["steps"], format_func=lambda v: "All" if v is None else str(v))
    status = f4.selectbox("Investigation status", [None] + opts["statuses"],
                          format_func=lambda v: "All" if v is None else v.replace("_", " "))
    sort = f5.selectbox("Sort", list(SORT_LABELS), format_func=SORT_LABELS.get)
    lookup = common.parse_tx_id(st.text_input("Find alert by transaction ID", key="tx_alert_lookup"))
    size = st.select_slider("Rows per page", [10, 25, 50, 100], value=25)
    sig = (rule, sev, step, status, sort, size, lookup)
    if st.session_state.get("queue_sig") != sig:      # filters changed → back to page 1
        st.session_state["queue_sig"], st.session_state["queue_page"] = sig, 1
    page = st.session_state.get("queue_page", 1)
    res = alert_queue.list_alerts(con, rule=rule, severity=sev, time_step=step, status=status,
                                  tx_id=lookup, sort=sort, page=page, page_size=size)
    st.session_state["queue_page"] = res["page"]
    p1, p2, p3 = st.columns([1, 2, 1])
    p1.button("◀ Previous", disabled=res["page"] <= 1,
              on_click=lambda: st.session_state.update(queue_page=res["page"] - 1))
    p2.write(f"**{res['total']:,}** alerts · page {res['page']} of {res['pages']}")
    p3.button("Next ▶", disabled=res["page"] >= res["pages"],
              on_click=lambda: st.session_state.update(queue_page=res["page"] + 1))
    if not res["rows"]:
        st.info("No alerts match these filters.")
        return None
    df = pd.DataFrame(res["rows"]).rename(columns={
        "tx_id": "transaction", "time_step": "step", "risk_score": "structural score",
        "rules": "rules", "n_hits": "hits", "case_status": "case",
        "observed_vs_threshold": "observed vs threshold"})
    st.dataframe(df, hide_index=True, width="stretch")
    return st.selectbox("Open alert", [r["tx_id"] for r in res["rows"]], key="open_alert")


def _evidence(con, tx: int, hit: dict) -> None:
    unit = "two-hop paths" if hit["rule_id"] == "C" else "edges"
    key = f"ev_{tx}_{hit['rule_id']}_{hit['direction']}"
    total, stored = hit["evidence_total"], hit["evidence_stored"]
    complete = stored >= total
    st.markdown(
        f"**Rule {hit['rule_id']} · {hit['direction']}** — observed {hit['observed']} vs threshold "
        f"{hit['threshold']:g} · score {hit['rule_score']} · {hit['detail']}")
    if complete:
        st.caption(f"Evidence complete: all {total} supporting {unit} are stored.")
    else:
        st.warning(f"Only a stored sample of {stored} of {total} supporting {unit} is shown. "
                   "It is not the complete evidence set; load more below.")
    shown = st.session_state.get(key, 0)
    rows = [{"#": i + 1, "edge IDs": " → ".join(map(str, e)), "source": "stored"}
            for i, e in enumerate(hit["evidence"])]
    if shown:
        page = explore.evidence_page(con, tx, hit["rule_id"], hit["direction"], 0, shown)
        rows = [{"#": i + 1, "edge IDs": " → ".join(map(str, it["edge_ids"])),
                 "nodes": " → ".join(map(str, it["nodes"])), "source": "retrieved"}
                for i, it in enumerate(page["items"])]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", height=min(320, 60 + 35 * len(rows)))
    if shown:
        st.caption(f"Retrieved {len(rows)} of {total} {unit} from the graph tables.")
    if (shown and shown < total) or (not shown and not complete):
        st.button(f"Load more {unit} (+{PAGE})", key=f"more_{key}",
                  on_click=lambda: st.session_state.update({key: min(total, max(shown, stored) + PAGE)}))


def _case_panel(con, tx: int, alert: dict) -> None:
    st.subheader("Investigation case")
    ss = st.session_state
    case = cases.get_case_by_tx(con, tx)

    def run(fn, getter, ok, after=None):
        """Click handler: read widget values now (not at render time), then apply."""
        try:
            fn(con, *getter())
            ss["case_msg"] = ("success", ok)
            if after:
                after()
        except cases.CaseError as exc:
            ss["case_msg"] = ("error", str(exc))

    analyst = lambda: ss.get("analyst", "")
    msg = ss.pop("case_msg", None)
    if case is None:
        st.write("No case has been opened for this alert.")
        st.button("Open case from this alert", on_click=run,
                  args=(cases.open_case, lambda: (tx, analyst()), "Case opened."))
        if msg:
            getattr(st, msg[0])(msg[1])
        return
    cid = case["case_id"]
    if msg:
        getattr(st, msg[0])(msg[1])
    st.write(f"Case **#{cid}** · status **{case['status']}** · disposition "
             f"`{case['disposition'] or 'not recorded'}` · opened by {case['opened_by']} "
             f"{case['opened_at']:%Y-%m-%d %H:%M} UTC")
    st.caption(f"Alert snapshot at opening: {case['alert_severity']} · structural score "
               f"{case['alert_risk_score']} · rules {case['alert_rules']}")
    if case["status"] == "closed":
        st.text_input("Reason for reopening", key=f"reopen_{cid}")
        st.button("Reopen case", on_click=run,
                  args=(cases.reopen_case, lambda: (cid, analyst(), ss.get(f"reopen_{cid}", "")),
                        "Case reopened.", lambda: ss.update({f"reopen_{cid}": ""})))
    else:
        c1, c2 = st.columns(2)
        c1.selectbox("Status", ["open", "in_review"],
                     index=["open", "in_review"].index(case["status"]), key=f"st_{cid}")
        c1.button("Update status", on_click=run,
                  args=(cases.set_status, lambda: (cid, analyst(), ss[f"st_{cid}"]), "Status updated."))
        disp_opts = list(cases.DISPOSITIONS)
        c2.selectbox("Disposition", disp_opts, key=f"dp_{cid}",
                     index=disp_opts.index(case["disposition"]) if case["disposition"] else 0)
        c2.button("Record disposition", on_click=run,
                  args=(cases.set_disposition, lambda: (cid, analyst(), ss[f"dp_{cid}"]), "Disposition recorded."))
        st.radio("Entry type", ["finding", "note"], horizontal=True, key=f"kind_{cid}")
        st.text_area("Finding / analyst note", key=f"body_{cid}", max_chars=cases.MAX_TEXT)
        st.button("Add entry", on_click=run,
                  args=(cases.add_note, lambda: (cid, analyst(), ss.get(f"body_{cid}", ""), ss[f"kind_{cid}"]),
                        "Entry added.", lambda: ss.update({f"body_{cid}": ""})))
        st.button("Close case with the selected disposition", on_click=run,
                  args=(cases.close_case, lambda: (cid, analyst(), ss[f"dp_{cid}"]), "Case closed."))
    notes = cases.list_notes(con, cid)
    if notes:
        st.markdown("**Findings and notes**")
        st.dataframe(pd.DataFrame(notes), hide_index=True, width="stretch")
    with st.expander("Audit history (append-only)"):
        st.dataframe(pd.DataFrame(cases.get_audit(con, cid)), hide_index=True, width="stretch")


def render(con) -> None:
    st.header("Structural Alert Investigation")
    common.risk_notice()
    st.caption("Alerts are ordered only by structural score and chosen filters; dataset labels are not used "
               "for scoring or prioritization.")
    if not common.has_alerts(con):
        st.warning("No alerts found. Run `python -m btgi detect`.")
        return
    tx = _queue(con)
    if tx is None:
        return
    st.divider()
    alert = explore.get_alert(con, tx)
    if alert is None:
        st.warning(f"No structural alert exists for transaction {tx}.")
        return
    st.subheader(f"Transaction {tx}")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Severity", alert["severity"])
    m2.metric("Structural score", alert["risk_score"])
    m3.metric("Temporal step", alert["time_step"])
    m4.metric("Rules", alert["rules"])
    thr = con.execute(
        "SELECT rule_id, direction, parameter, value, basis FROM rule_thresholds "
        "WHERE rule_id IN (SELECT rule_id FROM alert_rule_hits WHERE tx_id = ?) ORDER BY 1,2,3", [tx]).fetchall()
    with st.expander("Reference thresholds for the triggered rules (steps 1–34, label-free)"):
        st.dataframe(pd.DataFrame(thr, columns=["rule", "direction", "parameter", "value", "basis"]),
                     hide_index=True, width="stretch")
    st.subheader("Supporting graph evidence")
    for hit in alert["hits"]:
        _evidence(con, tx, hit)
    st.button("Open in Graph Investigation", on_click=common.goto, args=("Graph Investigation", tx))
    _case_panel(con, tx, alert)
