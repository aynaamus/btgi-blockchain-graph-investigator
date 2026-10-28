import pandas as pd
import streamlit as st

from ..evaluate import evaluate
from . import common


@st.cache_data(show_spinner=False)
def _eval(_con, path: str, first: int, last: int):
    return evaluate(_con, first, last)


def _pct(v):
    return "n/a" if v is None else f"{v * 100:.2f}%"


def render(con) -> None:
    st.header("Detection Performance")
    st.warning("Retrospective evaluation only. These rules are structural investigation aids, not a "
               "validated real-time illicit-transaction detection control.")
    if not common.has_alerts(con):
        st.warning("No alerts found. Run `python -m btgi detect`.")
        return
    r = _eval(con, str(common.db_path()), 35, 49)
    c = r["scopes"]["combined"]
    prev = r["known_label_prevalence_illicit"]
    st.caption(f"Held-out period: temporal steps {r['period']['first_step']}–{r['period']['last_step']} · "
               f"{r['transactions']:,} transactions · thresholds fitted on steps 1–34 without labels.")

    m = st.columns(5)
    m[0].metric("Precision", _pct(c["precision"]))
    m[1].metric("Recall", _pct(c["recall"]))
    m[2].metric("F1", "n/a" if c["f1"] is None else f"{c['f1']:.3f}")
    m[3].metric("Alert rate (all transactions)", _pct(c["alert_rate_all"]))
    m[4].metric("Known-label illicit prevalence", _pct(prev))

    st.subheader("Confusion matrix (known labels only)")
    st.dataframe(pd.DataFrame(
        {"Flagged": [c["tp"], c["fp"]], "Not flagged": [c["fn"], c["tn"]]},
        index=["Annotated illicit", "Annotated licit"]), width="stretch")
    lc = r["label_counts"]
    st.caption(f"Denominators: {lc.get('illicit', 0):,} illicit + {lc.get('licit', 0):,} licit = "
               f"{r['known_labelled']:,} known-label transactions; {lc.get('unknown', 0):,} unknown-label "
               "transactions are excluded from the matrix.")

    st.subheader("By rule")
    rows = []
    for name, s in r["scopes"].items():
        rows.append({"scope": "Combined (any rule)" if name == "combined" else f"Rule {name}",
                     "TP": s["tp"], "FP": s["fp"], "TN": s["tn"], "FN": s["fn"],
                     "precision": _pct(s["precision"]), "recall": _pct(s["recall"]),
                     "F1": None if s["f1"] is None else round(s["f1"], 4),
                     "alert rate": _pct(s["alert_rate_all"]),
                     "unknown-label transactions flagged": s["unknown_label_flagged"]})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.caption("Unknown-label flagged counts are reported separately; no correctness is assigned to them.")

    st.subheader("How to read these results")
    lift = c["lift_vs_prevalence"]
    verdict = ("The rules perform **worse than the base rate**" if lift is not None and lift < 1
               else "Precision is not below the base rate here")
    st.markdown(f"""
1. Combined held-out precision is **{_pct(c['precision'])}**.
2. Known-label illicit prevalence is **{_pct(prev)}**.
3. {verdict} (precision / prevalence = {('n/a' if lift is None else f'{lift:.2f}')}): flagged transactions
   are less often annotated illicit than a random known-label transaction.
4. Unknown labels are excluded from the confusion matrix and are never treated as licit.
5. The evaluation is retrospective and uses complete temporal-step graph snapshots, so it does not
   reflect what would have been visible at decision time.
6. The rules are not validated real-time illicit-transaction detection controls.
7. The data are anonymized; nothing here supports attributing transactions to real-world wallets,
   people or organizations, or confirms criminal activity.
""")
