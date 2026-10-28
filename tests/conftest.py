from pathlib import Path

import duckdb
import pytest

from btgi.schema import EXPECTED_FEATURE_FILE_COLUMNS

# tx_id, time_step
NODES = [(100, 1), (101, 1), (102, 1), (200, 2), (201, 2), (300, 3)]
# 100->101->102->100 (cycle), 101->200 (cross-step), 200->201 ; 300 isolated
EDGES = [(100, 101), (101, 102), (102, 100), (101, 200), (200, 201)]
CLASSES = [(100, "1"), (101, "2"), (102, "unknown"), (200, "unknown"), (201, "2")]  # 300 has no row


def feature_row(tx_id, step, header_offset=0):
    n = EXPECTED_FEATURE_FILE_COLUMNS - 2
    feats = [f"{(tx_id % 7) + i / 1000:.4f}" for i in range(n)]
    return ",".join([str(tx_id), str(step), *feats])


def write_dataset(d: Path, nodes=NODES, edges=EDGES, classes=CLASSES, headers=False):
    d.mkdir(parents=True, exist_ok=True)
    f = [feature_row(t, s) for t, s in nodes]
    if headers:
        f.insert(0, ",".join(["txId", "ts"] + [f"c{i}" for i in range(EXPECTED_FEATURE_FILE_COLUMNS - 2)]))
    (d / "elliptic_txs_features.csv").write_text("\n".join(f) + "\n")
    c = [f"{t},{k}" for t, k in classes]
    e = [f"{a},{b}" for a, b in edges]
    (d / "elliptic_txs_classes.csv").write_text("\n".join(["txId,class", *c]) + "\n")
    (d / "elliptic_txs_edgelist.csv").write_text("\n".join(["txId1,txId2", *e]) + "\n")
    return d


@pytest.fixture
def src(tmp_path):
    return write_dataset(tmp_path / "src")


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def loaded(con, src):
    from btgi.ingest import ingest

    ingest(con, src)
    return con
