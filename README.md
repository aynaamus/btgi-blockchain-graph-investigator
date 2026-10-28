# Bitcoin Transaction Graph Investigator (BTGI)

A local investigative graph-analysis tool for the **Elliptic Bitcoin Data Set**, built on
Python 3.12, DuckDB and Streamlit. It ingests the dataset into a DuckDB database
with recorded source hashes and ingestion history, validates the graph, flags structurally unusual transactions with explainable
rules, and lets an analyst explore directed neighborhoods, trace supporting evidence and
record case work.

> **This is not a validated illicit-transaction detector.** On the held-out period the
> structural rules perform *worse than the base rate* (see [Measured performance](#measured-detection-performance)).
> Risk scores measure structural prominence, not the probability of criminal activity.

## Architecture

```
data/raw/…csv ──ingest──▶ DuckDB (data/btgi.duckdb) ◀── Streamlit app (src/btgi/app.py)
                           │ transactions · tx_features · tx_labels · edges
                           │ ingestion_runs · source_files · feature_map          (Milestone 1)
                           │ detection_runs · rule_thresholds · alerts
                           │ alert_rule_hits · alert_evidence                      (Milestone 2)
                           └ cases · case_notes · case_audit                       (Milestone 3)
```

| Module (`src/btgi/`) | Role |
|---|---|
| `schema.py`, `ingest.py`, `discovery.py` | transactional, idempotent, SHA-256-recorded ingestion; dataset and disk-space discovery |
| `validate.py`, `overview.py` | SQL integrity checks, degree and connected-component statistics |
| `explore.py` | bounded lookups, neighbors, directed neighborhoods/paths, evidence retrieval |
| `detect.py`, `detect_schema.py` | topology-only rules A/B/C, scoring, evidence tables |
| `evaluate.py` | retrospective evaluation (the only module that reads labels) |
| `alert_queue.py` | database-backed alert filtering, sorting and pagination |
| `cases.py` | case workflow with an audit trail that is append-only through the application API (see [Investigator workflow](#investigator-workflow)) |
| `graph_plot.py`, `ui/`, `app.py` | Plotly figure and the five Streamlit views. Analytical logic lives in the modules above; the UI performs only limited display lookups (for example reading `feature_map` and the thresholds of a triggered rule) |
| `cli.py` | command line interface for every analytical step |

## Dataset provenance, attribution and licensing

**Data source.** The [Elliptic Data Set](https://www.kaggle.com/datasets/ellipticco/elliptic-data-set)
is provided by Elliptic on Kaggle under the
[Creative Commons Attribution-NonCommercial-NoDerivatives 4.0 International licence (CC BY-NC-ND 4.0)](https://creativecommons.org/licenses/by-nc-nd/4.0/).

**Research citation.**
Weber, M., Domeniconi, G., Chen, J., Weidele, D. K. I., Bellei, C., Robinson, T., and
Leiserson, C. E. (2019). *Anti-Money Laundering in Bitcoin: Experimenting with Graph
Convolutional Networks for Financial Forensics.* KDD '19 Workshop on Anomaly Detection in
Finance. <https://arxiv.org/abs/1908.02591>

**What this repository does and does not contain.**

- The original dataset is **not** distributed with this repository, and neither is any
  database, export or other artifact derived from it. Those files are excluded by
  `.gitignore` and must stay out of any public copy.
- The application only reads the three source CSVs; it never modifies them. Anyone running
  it must obtain the data from the authorized source above and comply with its licence,
  including attribution and the non-commercial and no-derivatives terms.
- The dataset's licence is separate from the licence of the application code. It
  applies to the data, not to the software in this repository.
- **Application code licence.** Copyright (c) 2026 Ayna Musayeva. All rights reserved.
  The source is public for viewing only; no open-source or other reuse licence is granted
  (see [Copyright and licence](#copyright-and-licence)).
- The README quotes a few aggregate results and one worked example with anonymized
  identifiers from the data set; it contains no dataset files or record excerpts.

## Graph representation

- **Nodes**: 203,769 transactions identified by their original `txId`, each with a temporal step (1–49).
- **Edges**: 234,355 directed edges `source → target` with a deterministic `edge_id` (rank of the pair in sorted order). Duplicate pairs would be kept, not removed; none exist.
- **Features**: 166 anonymized features are preserved in original order (`feature_map`): position 1 is the time step; positions 2–166 are stored as `f002…f166` with no invented names. Per the dataset documentation, positions 1–94 are local and 95–166 are aggregated from neighbouring transactions.
- **Labels**: source `1` = illicit, `2` = licit, `unknown`. Unknown is never treated as licit.
- The 49 connected components coincide with the 49 temporal steps. There are **no edges between time steps**.

## Integrity checks

`python -m btgi validate` runs SQL checks for node/edge totals, duplicate ids and edges, edges
to missing nodes, self-loops, missing feature values, per-step counts, label distribution,
degree distributions and connected components. Ingestion records file sizes and SHA-256
hashes, runs in a single transaction (rolling back on any error) and re-running on identical
files is a no-op. Source CSVs are never modified. Verified on the imported data: no duplicate
nodes or edges, no missing nodes, self-loops or feature values.

## Directed path exploration

Neighborhoods and paths are built with one bounded SQL query per hop (depth ≤ 4, caps on
nodes and edges, results flagged `truncated`), so exploration never loads the complete graph
into memory. The connected-component statistics in `validate.py` are a separate case: they
stream all edges from DuckDB into an in-memory union-find structure (integer node ids only,
a few hundred thousand entries for this data set).
Incoming, outgoing and bidirectional exploration are supported, with supporting edge ids.
Paths are observed topology only: they do not show payment flow, intent, ownership, control
or money laundering.

## Structural anomaly rules

Topology-only and label-free; thresholds come from reference steps 1–34 only.

| Rule | What it flags | Thresholds (in / out) |
|---|---|---|
| A | degree above the reference 99th percentile | 9 / 5 |
| B | concentrated fan: moderate degree whose neighbours connect only to this node | degree 4–9 & share ≥ 0.875 / degree 3–5 & share ≥ 1.0 |
| C | two-hop reach above the reference 99th percentile (not already hit by A/B) | 15 / 7 |

One alert per transaction (7,542 in total); per-rule scores combine as a noisy-or into a
structural-prominence score with fixed severity bands (low < 0.5 ≤ medium < 0.8 ≤ high).
Rule C often fires on nodes merely adjacent to a hub; see the example below.

## Evidence traceability

Every rule hit stores its observed value, threshold, exact evidence total and up to 25
supporting edge ids (or two-edge paths for rule C). The stored rows are a **sample** when the
total is larger; the application says so and can retrieve the complete set on demand with
`explore.evidence_page`, whose ordering matches the stored sample.

## Investigator workflow

In the **Structural Alerts** view an analyst filters the queue (rule, severity, step,
investigation status), opens an alert, reviews thresholds and evidence, then may open a
case. Cases are stored in `cases`, `case_notes` and `case_audit`; they support findings,
notes, status (`open`/`in_review`/`closed`), dispositions, closing and reopening. Each
change writes an audit row (timestamp, analyst, action, old and new value) in the same
transaction; the application API offers no way to edit or delete audit history. This is an
application-level guarantee only: the audit tables are ordinary DuckDB tables and are not
protected against direct modification of the database file (there is no tamper evidence).
One case per alerted transaction; cases survive restarts and detection reruns. Dispositions describe a review
outcome of a structural pattern and never assert criminal activity. There is no
authentication: the analyst identifier is a free-text label.

## Measured detection performance

Held-out steps 35–49 (67,504 transactions: 1,083 illicit, 15,587 licit, 50,834 unknown).
Unknown labels are excluded from the confusion matrix.

| TP | FP | TN | FN | Precision | Recall | F1 | Alert rate |
|---|---|---|---|---|---|---|---|
| 17 | 1,467 | 14,120 | 1,066 | 1.15 % | 1.57 % | 0.013 | 5.14 % |

Known-label illicit prevalence is 6.50 %, so the rules perform worse than the base rate.
1,986 held-out alerts are on unknown-label transactions (no correctness assigned). The
evaluation is retrospective on complete per-step snapshots; it does not demonstrate
real-time detection. Results are reported unchanged and were not tuned on held-out labels.

**Methodological disclosure.** Before the rules were fixed, graph profiling examined
structural statistics for all 49 temporal steps, including the held-out steps 35–49 (for
example degree quantiles and per-step maximum degrees). Rule thresholds are computed by the
detection code from reference steps 1–34 only, and labels were not used in profiling, rule
selection or threshold calibration. The rule definitions and quantile parameters (0.99,
0.97, 0.90) were fixed before evaluation and were not changed afterwards; the evaluation was
run once on that configuration. Because the design was not blind to the structure of the
held-out period, the evaluation should not be read as a fully untouched out-of-sample test
of the rule design.

## Limitations of the data

The data are anonymized: there are no wallet addresses, owners, amounts or locations, so
nothing here supports attributing a transaction to a real-world wallet, person or
organization. The aggregated source features may contain information from adjacent
transactions and need a leakage assessment before any causal or chronological use; they
are not used by the rules.

## Local setup and launch

Requires Python 3.12 (`python3.12 --version`). On Windows use `.venv\Scripts\` in place of `.venv/bin/`.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev,app]"
# place the three Elliptic CSVs under data/raw/ (nested folders are fine), then:
.venv/bin/python -m btgi ingest
.venv/bin/python -m btgi validate
.venv/bin/python -m btgi detect
.venv/bin/streamlit run src/btgi/app.py
.venv/bin/python -m pytest
```

The app holds the DuckDB file open; stop it before running CLI commands against the same
database. Set `BTGI_DB=/path/to.duckdb` to use another database file.

## Reproducible example (real transaction 915064)

```bash
.venv/bin/python -m btgi tx 915064
.venv/bin/python -m btgi alert 915064
.venv/bin/python -m btgi paths 915064 --direction in --hops 2 --limit 5
```

Observed values: transaction 915064 is in temporal step 40 with in-degree 1 and out-degree
0; its only incoming edge is edge 17837 from transaction 12117243. It carries a high-severity
rule C alert (`C:in`): 79 observed two-hop paths end at it, against a reference threshold of
15, of which 25 are stored (the app can retrieve all 79). The path count arises because its
single predecessor, 12117243, itself has many predecessors. This documents graph structure
only and supports no conclusion about the nature of the transaction.

## Copyright and licence

Copyright (c) 2026 Ayna Musayeva. All rights reserved.

The source code in this repository may be viewed publicly, but no licence to use, copy,
modify or redistribute it is granted beyond what GitHub's Terms of Service allow for viewing
and forking hosted repositories. No open-source licence applies.

This covers the original application code only. The Elliptic Data Set is a separate work
with its own licence (CC BY-NC-ND 4.0), is not included here, and is governed solely by that
licence; see [Dataset provenance, attribution and licensing](#dataset-provenance-attribution-and-licensing).
