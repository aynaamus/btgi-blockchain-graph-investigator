import hashlib

import duckdb
import pytest

from btgi.ingest import IngestError, ingest
from btgi.schema import FEATURE_COLUMNS
from conftest import CLASSES, EDGES, NODES, feature_row, write_dataset


def count(con, t):
    return con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]


def test_correct_ingestion(loaded):
    assert count(loaded, "transactions") == len(NODES)
    assert count(loaded, "edges") == len(EDGES)
    assert count(loaded, "tx_labels") == len(CLASSES)
    assert count(loaded, "feature_map") == 166
    assert loaded.execute(
        "SELECT status FROM ingestion_runs").fetchall() == [("completed",)]


def test_features_and_time_step_preserved(loaded):
    vals = feature_row(101, 1).split(",")
    row = loaded.execute("SELECT * FROM tx_features WHERE tx_id=101").fetchone()
    assert len(row) == 1 + len(FEATURE_COLUMNS) == 166
    assert [round(x, 4) for x in row[1:]] == [round(float(v), 4) for v in vals[2:]]  # original order
    assert loaded.execute("SELECT time_step FROM transactions WHERE tx_id=101").fetchone()[0] == 1


def test_header_row_in_features_is_skipped(con, tmp_path):
    d = write_dataset(tmp_path / "h", headers=True)
    ingest(con, d)
    assert count(con, "transactions") == len(NODES)


def test_directed_edges_preserved(loaded):
    got = set(loaded.execute("SELECT src_tx_id, dst_tx_id FROM edges").fetchall())
    assert got == set(EDGES)
    assert (101, 100) not in got


def test_label_mapping_and_missing_row(loaded):
    m = dict(loaded.execute("SELECT tx_id, label FROM tx_labels").fetchall())
    assert m == {100: "illicit", 101: "licit", 102: "unknown", 200: "unknown", 201: "licit"}
    assert loaded.execute("SELECT raw_class FROM tx_labels WHERE tx_id=100").fetchone()[0] == "1"
    assert 300 not in m  # missing class row is not turned into licit


def test_unrecognized_label_rejected(con, tmp_path):
    d = write_dataset(tmp_path / "x", classes=[(100, "3")])
    with pytest.raises(IngestError, match="unrecognized"):
        ingest(con, d)


def test_missing_source_file(con, tmp_path, src):
    (src / "elliptic_txs_edgelist.csv").unlink()
    with pytest.raises(FileNotFoundError, match="elliptic_txs_edgelist.csv"):
        ingest(con, src)
    with pytest.raises(FileNotFoundError):
        ingest(con, tmp_path / "nope")


def test_incorrect_schema_column_count(con, tmp_path):
    d = write_dataset(tmp_path / "bad")
    (d / "elliptic_txs_features.csv").write_text("100,1,0.5,0.5\n")
    with pytest.raises(IngestError, match="expected 167 columns"):
        ingest(con, d)
    (d / "elliptic_txs_features.csv").write_text(
        "\n".join(feature_row(t, s) for t, s in NODES) + "\n")
    (d / "elliptic_txs_edgelist.csv").write_text("txId1,txId2,x\n1,2,3\n")
    with pytest.raises(IngestError, match="expected 2 columns"):
        ingest(con, d)


def test_non_numeric_value_rejected(con, tmp_path):
    d = write_dataset(tmp_path / "bad")
    rows = [feature_row(t, s) for t, s in NODES]
    parts = rows[1].split(",")
    parts[5] = "abc"
    rows[1] = ",".join(parts)
    (d / "elliptic_txs_features.csv").write_text("\n".join(rows) + "\n")
    with pytest.raises(IngestError):
        ingest(con, d)
    assert count(con, "transactions") == 0


def test_duplicate_identifiers_rejected(con, tmp_path):
    d = write_dataset(tmp_path / "dup", nodes=NODES + [(100, 1)])
    with pytest.raises(IngestError, match="duplicate transaction"):
        ingest(con, d)
    assert count(con, "transactions") == 0


def test_invalid_edge_reference_rejected(con, tmp_path):
    d = write_dataset(tmp_path / "inv", edges=EDGES + [(100, 999)])
    with pytest.raises(IngestError, match="nonexistent"):
        ingest(con, d)


def test_failed_ingest_rolls_back_and_is_logged(con, tmp_path, src):
    ingest(con, src)
    bad = write_dataset(tmp_path / "bad", edges=EDGES + [(100, 999)])
    with pytest.raises(IngestError):
        ingest(con, bad, replace=True)
    # original data intact
    assert count(con, "transactions") == len(NODES)
    assert count(con, "edges") == len(EDGES)
    st = [r[0] for r in con.execute("SELECT status FROM ingestion_runs ORDER BY run_id").fetchall()]
    assert st == ["completed", "failed"]


def test_repeat_import_is_idempotent(con, src):
    r1 = ingest(con, src)
    ids1 = con.execute("SELECT * FROM edges ORDER BY edge_id").fetchall()
    r2 = ingest(con, src)
    assert r1["status"] == "completed" and r2["status"] == "already_ingested"
    assert count(con, "ingestion_runs") == 1
    assert con.execute("SELECT * FROM edges ORDER BY edge_id").fetchall() == ids1
    assert count(con, "transactions") == len(NODES)


def test_different_data_requires_replace(con, src, tmp_path):
    ingest(con, src)
    other = write_dataset(tmp_path / "o", edges=EDGES[:2])
    with pytest.raises(IngestError, match="replace"):
        ingest(con, other)
    assert count(con, "edges") == len(EDGES)
    ingest(con, other, replace=True)
    assert count(con, "edges") == 2
    assert count(con, "transactions") == len(NODES)


def test_source_hash_recorded_and_files_untouched(con, src):
    before = {p.name: p.read_bytes() for p in src.iterdir()}
    ingest(con, src)
    rows = con.execute("SELECT file_name, sha256, size_bytes, row_count FROM source_files").fetchall()
    assert len(rows) == 3
    for name, sha, size, n in rows:
        assert sha == hashlib.sha256(before[name]).hexdigest()
        assert size == len(before[name])
    assert {p.name: p.read_bytes() for p in src.iterdir()} == before


def test_nested_extracted_folder_is_found(con, tmp_path):
    d = write_dataset(tmp_path / "zipdir" / "elliptic_bitcoin_dataset" / "inner")
    ingest(con, tmp_path / "zipdir")
    assert count(con, "transactions") == len(NODES)


def test_persists_to_file_db(tmp_path, src):
    db = tmp_path / "t.duckdb"
    with duckdb.connect(str(db)) as c:
        ingest(c, src, db_path=db)
    with duckdb.connect(str(db), read_only=True) as c:
        assert count(c, "edges") == len(EDGES)
