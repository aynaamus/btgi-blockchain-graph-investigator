"""Tables holding graph-anomaly alerts and their evidence (Milestone 2).

These tables are derived output only. They never alter transactions, edges,
tx_features, tx_labels or the ingestion history.
"""

from __future__ import annotations

import duckdb

DDL = [
    """CREATE TABLE IF NOT EXISTS detection_runs (
    run_id              BIGINT PRIMARY KEY,
    created_at          TIMESTAMP NOT NULL,
    dataset_fingerprint VARCHAR NOT NULL,
    config_hash         VARCHAR NOT NULL,
    config_json         VARCHAR NOT NULL,
    n_alerts            BIGINT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS rule_thresholds (
    run_id    BIGINT NOT NULL,
    rule_id   VARCHAR NOT NULL,
    direction VARCHAR NOT NULL,        -- 'in' | 'out'
    parameter VARCHAR NOT NULL,
    value     DOUBLE,                  -- NULL: not computable on the reference period
    basis     VARCHAR NOT NULL,
    PRIMARY KEY (run_id, rule_id, direction, parameter)
)""",
    """CREATE TABLE IF NOT EXISTS alerts (
    tx_id      BIGINT PRIMARY KEY,     -- one alert per transaction
    run_id     BIGINT NOT NULL,
    time_step  INTEGER NOT NULL,
    risk_score DOUBLE NOT NULL,        -- structural prominence in [0,1], not a probability
    severity   VARCHAR NOT NULL CHECK (severity IN ('low','medium','high')),
    n_hits     INTEGER NOT NULL,
    rules      VARCHAR NOT NULL        -- sorted rule/direction codes, e.g. 'A:in,C:out'
)""",
    """CREATE TABLE IF NOT EXISTS alert_rule_hits (
    tx_id           BIGINT NOT NULL,
    rule_id         VARCHAR NOT NULL,  -- 'A' | 'B' | 'C'
    direction       VARCHAR NOT NULL,
    time_step       INTEGER NOT NULL,
    observed_value  BIGINT NOT NULL,
    threshold_value DOUBLE NOT NULL,
    rule_score      DOUBLE NOT NULL,
    evidence_total  BIGINT NOT NULL,   -- exact number of supporting edges / paths
    evidence_stored BIGINT NOT NULL,   -- stored rows (capped)
    detail          VARCHAR,
    PRIMARY KEY (tx_id, rule_id, direction)
)""",
    """CREATE TABLE IF NOT EXISTS alert_evidence (
    tx_id     BIGINT NOT NULL,
    rule_id   VARCHAR NOT NULL,
    direction VARCHAR NOT NULL,
    seq       INTEGER NOT NULL,        -- deterministic order within the hit
    edge_id_1 BIGINT NOT NULL,         -- edges.edge_id of the (first) supporting edge
    edge_id_2 BIGINT,                  -- second edge of an observed two-hop path (rule C)
    PRIMARY KEY (tx_id, rule_id, direction, seq)
)""",
]


def create_detection_schema(con: duckdb.DuckDBPyConnection) -> None:
    for stmt in DDL:
        con.execute(stmt)
