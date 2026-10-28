"""DuckDB schema for the Elliptic transaction graph.

Feature positions follow the original `elliptic_txs_features.csv` column order
after the transaction id: position 1 is the time step (stored in
`transactions.time_step`), positions 2..166 are stored in `tx_features` as
`f002`..`f166`. No semantic names are invented for anonymized features.
"""

from __future__ import annotations

import duckdb

N_FEATURE_POSITIONS = 166  # time step + 165 anonymized features
FEATURE_COLUMNS = [f"f{p:03d}" for p in range(2, N_FEATURE_POSITIONS + 1)]
EXPECTED_FEATURE_FILE_COLUMNS = 1 + N_FEATURE_POSITIONS  # txId + 166

# Raw class codes in elliptic_txs_classes.csv -> label category.
# Per the dataset description: 1 = illicit, 2 = licit, "unknown" = unlabelled.
LABEL_MAP = {"1": "illicit", "2": "licit", "unknown": "unknown"}

# Dataset documentation: first 94 positions (incl. time step) are local
# transaction features, the remaining 72 are aggregated from neighbours.
LOCAL_LAST_POSITION = 94


def _ddl() -> list[str]:
    feat_cols = ",\n    ".join(f"{c} DOUBLE" for c in FEATURE_COLUMNS)
    return [
        """CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id        BIGINT PRIMARY KEY,
    started_at    TIMESTAMP NOT NULL,
    finished_at   TIMESTAMP,
    status        VARCHAR NOT NULL CHECK (status IN ('running','completed','failed')),
    n_transactions BIGINT,
    n_edges       BIGINT,
    n_labels      BIGINT,
    dataset_fingerprint VARCHAR,
    message       VARCHAR
)""",
        """CREATE TABLE IF NOT EXISTS source_files (
    run_id     BIGINT NOT NULL,
    role       VARCHAR NOT NULL CHECK (role IN ('features','classes','edgelist')),
    file_name  VARCHAR NOT NULL,
    size_bytes BIGINT NOT NULL,
    sha256     VARCHAR NOT NULL,
    has_header BOOLEAN NOT NULL,
    row_count  BIGINT NOT NULL,
    PRIMARY KEY (run_id, role)
)""",
        """CREATE TABLE IF NOT EXISTS transactions (
    tx_id     BIGINT PRIMARY KEY,   -- original Elliptic txId
    time_step INTEGER NOT NULL,
    run_id    BIGINT NOT NULL
)""",
        f"""CREATE TABLE IF NOT EXISTS tx_features (
    tx_id BIGINT PRIMARY KEY,
    {feat_cols}
)""",
        """CREATE TABLE IF NOT EXISTS tx_labels (
    tx_id     BIGINT PRIMARY KEY,
    raw_class VARCHAR NOT NULL,      -- verbatim source value
    label     VARCHAR NOT NULL CHECK (label IN ('illicit','licit','unknown'))
)""",
        """CREATE TABLE IF NOT EXISTS edges (
    edge_id  BIGINT PRIMARY KEY,     -- rank of (src,dst) in sorted order; deterministic across reruns
    src_tx_id BIGINT NOT NULL,       -- direction: src -> dst as in the source
    dst_tx_id BIGINT NOT NULL,
    run_id   BIGINT NOT NULL
)""",
        "CREATE INDEX IF NOT EXISTS idx_edges_src ON edges (src_tx_id)",
        "CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges (dst_tx_id)",
        """CREATE TABLE IF NOT EXISTS feature_map (
    position    INTEGER PRIMARY KEY, -- 1-based position after txId in source
    column_name VARCHAR NOT NULL,    -- column in tx_features / transactions
    storage_table VARCHAR NOT NULL,
    documented_group VARCHAR NOT NULL -- 'time_step' | 'local' | 'aggregated' (per dataset docs)
)""",
    ]


def create_schema(con: duckdb.DuckDBPyConnection) -> None:
    for stmt in _ddl():
        con.execute(stmt)
    if con.execute("SELECT count(*) FROM feature_map").fetchone()[0] == 0:
        rows = [(1, "time_step", "transactions", "time_step")]
        for p in range(2, N_FEATURE_POSITIONS + 1):
            grp = "local" if p <= LOCAL_LAST_POSITION else "aggregated"
            rows.append((p, f"f{p:03d}", "tx_features", grp))
        con.executemany("INSERT INTO feature_map VALUES (?,?,?,?)", rows)
