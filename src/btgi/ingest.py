"""Transactional, idempotent ingestion of the Elliptic CSVs into DuckDB.

Source files are only ever opened read-only.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from .discovery import FILE_NAMES, require_disk_space, resolve_source
from .schema import (
    EXPECTED_FEATURE_FILE_COLUMNS,
    FEATURE_COLUMNS,
    LABEL_MAP,
    create_schema,
)


class IngestError(Exception):
    """Source data failed validation; the database was left unchanged."""


_EXPECTED_COLS = {
    "features": EXPECTED_FEATURE_FILE_COLUMNS,
    "classes": 2,
    "edgelist": 2,
}


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def _inspect(role: str, path: Path) -> bool:
    """Check column count from the first line; return whether it is a header."""
    with open(path, "r", encoding="utf-8", newline="") as f:
        first = f.readline().rstrip("\r\n")
    if not first:
        raise IngestError(f"{path.name}: file is empty")
    fields = first.split(",")
    if len(fields) != _EXPECTED_COLS[role]:
        raise IngestError(
            f"{path.name}: expected {_EXPECTED_COLS[role]} columns, found {len(fields)}"
        )
    try:
        int(fields[0].strip().strip('"'))
        return False
    except ValueError:
        return True


def _q(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _read_csv(path: Path, columns: dict[str, str], header: bool) -> str:
    cols = ", ".join(f"'{k}': '{v}'" for k, v in columns.items())
    return (
        f"read_csv({_q(path)}, header=false, skip={1 if header else 0}, "
        f"columns={{{cols}}}, nullstr='', delim=',')"
    )


def _stage(con, paths: dict[str, Path], headers: dict[str, bool]) -> None:
    fcols = {"tx_id": "BIGINT", "time_step": "INTEGER"}
    fcols.update({c: "DOUBLE" for c in FEATURE_COLUMNS})
    con.execute(
        f"CREATE TEMP TABLE stg_features AS SELECT * FROM "
        f"{_read_csv(paths['features'], fcols, headers['features'])}"
    )
    con.execute(
        f"CREATE TEMP TABLE stg_classes AS SELECT * FROM "
        f"{_read_csv(paths['classes'], {'tx_id': 'BIGINT', 'raw_class': 'VARCHAR'}, headers['classes'])}"
    )
    con.execute(
        f"CREATE TEMP TABLE stg_edges AS SELECT * FROM "
        f"{_read_csv(paths['edgelist'], {'src_tx_id': 'BIGINT', 'dst_tx_id': 'BIGINT'}, headers['edgelist'])}"
    )


def _check_staged(con) -> None:
    def scalar(sql):
        return con.execute(sql).fetchone()[0]

    if scalar("SELECT count(*) FROM stg_features WHERE tx_id IS NULL OR time_step IS NULL"):
        raise IngestError("features: null transaction id or time step")
    if scalar("SELECT count(*) - count(DISTINCT tx_id) FROM stg_features"):
        raise IngestError("features: duplicate transaction identifiers")
    if scalar("SELECT count(*) FROM stg_classes WHERE tx_id IS NULL OR raw_class IS NULL"):
        raise IngestError("classes: null transaction id or class")
    if scalar("SELECT count(*) - count(DISTINCT tx_id) FROM stg_classes"):
        raise IngestError("classes: duplicate transaction identifiers")
    allowed = ", ".join(f"'{k}'" for k in LABEL_MAP)
    bad = con.execute(
        f"SELECT DISTINCT raw_class FROM stg_classes WHERE raw_class NOT IN ({allowed}) LIMIT 5"
    ).fetchall()
    if bad:
        raise IngestError(f"classes: unrecognized class values {[b[0] for b in bad]}")
    if scalar(
        "SELECT count(*) FROM stg_classes c ANTI JOIN stg_features f USING (tx_id)"
    ):
        raise IngestError("classes: reference transactions absent from features")
    if scalar("SELECT count(*) FROM stg_edges WHERE src_tx_id IS NULL OR dst_tx_id IS NULL"):
        raise IngestError("edgelist: null endpoint")
    if scalar(
        """SELECT count(*) FROM stg_edges e
           WHERE NOT EXISTS (SELECT 1 FROM stg_features f WHERE f.tx_id = e.src_tx_id)
              OR NOT EXISTS (SELECT 1 FROM stg_features f WHERE f.tx_id = e.dst_tx_id)"""
    ):
        raise IngestError("edgelist: edges reference nonexistent transactions")


def ingest(
    con: duckdb.DuckDBPyConnection,
    source: Path | dict[str, Path],
    *,
    db_path: Path | None = None,
    replace: bool = False,
    check_disk: bool = True,
) -> dict:
    """Load the dataset. Returns a summary dict with `status`.

    status is 'completed' for a new load or 'already_ingested' when the same
    source hashes were previously loaded (no changes made). Loading different
    data over existing data requires replace=True.
    """
    paths = source if isinstance(source, dict) else resolve_source(Path(source))
    for role in FILE_NAMES:
        if role not in paths or not Path(paths[role]).is_file():
            raise FileNotFoundError(f"Missing source file for '{role}'")
    paths = {r: Path(p) for r, p in paths.items()}

    sizes = {r: p.stat().st_size for r, p in paths.items()}
    if check_disk and db_path is not None:
        require_disk_space(Path(db_path), sum(sizes.values()))

    headers = {r: _inspect(r, p) for r, p in paths.items()}
    hashes = {r: sha256_file(p) for r, p in paths.items()}
    fingerprint = hashlib.sha256(
        "|".join(f"{r}:{hashes[r]}" for r in sorted(hashes)).encode()
    ).hexdigest()

    create_schema(con)
    prev = con.execute(
        "SELECT dataset_fingerprint FROM ingestion_runs WHERE status='completed' "
        "ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    has_data = con.execute("SELECT count(*) FROM transactions").fetchone()[0] > 0
    if has_data and prev and prev[0] == fingerprint:
        return {"status": "already_ingested", "fingerprint": fingerprint}
    if has_data and not replace:
        raise IngestError(
            "Database already holds different data; pass replace=True to reload"
        )

    run_id = con.execute("SELECT coalesce(max(run_id),0)+1 FROM ingestion_runs").fetchone()[0]
    started = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        con.execute("BEGIN")
        con.execute(
            "INSERT INTO ingestion_runs (run_id, started_at, status, dataset_fingerprint) "
            "VALUES (?,?,'running',?)",
            [run_id, started, fingerprint],
        )
        _stage(con, paths, headers)
        _check_staged(con)

        for t in ("edges", "tx_labels", "tx_features", "transactions"):
            con.execute(f"DELETE FROM {t}")
        con.execute(
            "INSERT INTO transactions SELECT tx_id, time_step, ? FROM stg_features", [run_id]
        )
        feat = ", ".join(FEATURE_COLUMNS)
        con.execute(f"INSERT INTO tx_features SELECT tx_id, {feat} FROM stg_features")
        mapping = " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in LABEL_MAP.items())
        con.execute(
            f"INSERT INTO tx_labels SELECT tx_id, raw_class, CASE raw_class {mapping} END "
            "FROM stg_classes"
        )
        # Deterministic id: rank in (src, dst) order, independent of file layout.
        con.execute(
            "INSERT INTO edges SELECT row_number() OVER (ORDER BY src_tx_id, dst_tx_id), "
            "src_tx_id, dst_tx_id, ? FROM stg_edges",
            [run_id],
        )
        counts = {
            "features": con.execute("SELECT count(*) FROM stg_features").fetchone()[0],
            "classes": con.execute("SELECT count(*) FROM stg_classes").fetchone()[0],
            "edgelist": con.execute("SELECT count(*) FROM stg_edges").fetchone()[0],
        }
        # Source must not have changed while we were reading it.
        for r, p in paths.items():
            if sha256_file(p) != hashes[r]:
                raise IngestError(f"{p.name}: file changed during ingestion")
        for r, p in paths.items():
            con.execute(
                "INSERT INTO source_files VALUES (?,?,?,?,?,?,?)",
                [run_id, r, p.name, sizes[r], hashes[r], headers[r], counts[r]],
            )
        con.execute(
            "UPDATE ingestion_runs SET finished_at=?, status='completed', "
            "n_transactions=?, n_edges=?, n_labels=? WHERE run_id=?",
            [
                datetime.now(timezone.utc).replace(tzinfo=None),
                counts["features"], counts["edgelist"], counts["classes"], run_id,
            ],
        )
        con.execute("COMMIT")
    except Exception as exc:
        try:
            con.execute("ROLLBACK")
        except duckdb.Error:
            pass
        con.execute(
            "INSERT INTO ingestion_runs (run_id, started_at, finished_at, status, "
            "dataset_fingerprint, message) VALUES (?,?,?,'failed',?,?)",
            [run_id, started, datetime.now(timezone.utc).replace(tzinfo=None),
             fingerprint, str(exc)[:500]],
        )
        if isinstance(exc, (IngestError, OSError)):
            raise
        raise IngestError(f"ingestion failed: {exc}") from exc
    finally:
        for t in ("stg_features", "stg_classes", "stg_edges"):
            con.execute(f"DROP TABLE IF EXISTS {t}")

    return {
        "status": "completed", "run_id": run_id, "fingerprint": fingerprint,
        "n_transactions": counts["features"], "n_edges": counts["edgelist"],
        "n_labels": counts["classes"], "sha256": hashes,
    }
