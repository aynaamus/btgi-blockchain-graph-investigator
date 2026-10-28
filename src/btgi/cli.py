"""Command line entry point: python -m btgi <command>."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb

from . import explore
from .discovery import FILE_NAMES, disk_report, find_dataset, project_root
from .ingest import IngestError, ingest
from .detect import DetectionConfig, DetectionError, run_detection
from .evaluate import evaluate
from .validate import validate

DEFAULT_DB = project_root() / "data" / "btgi.duckdb"


def _dump(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="btgi")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("discover", help="locate dataset files and report disk space")
    p = sub.add_parser("ingest", help="load the dataset into DuckDB")
    p.add_argument("--source", type=Path, help="directory holding (or containing) the CSVs")
    p.add_argument("--replace", action="store_true", help="replace previously loaded different data")
    p = sub.add_parser("validate", help="run graph integrity checks")
    p.add_argument("--no-components", action="store_true")
    p = sub.add_parser("tx", help="look up a transaction")
    p.add_argument("tx_id", type=int)
    p = sub.add_parser("neighbors", help="direct neighbors")
    p.add_argument("tx_id", type=int)
    p.add_argument("--direction", choices=explore.DIRECTIONS, default="both")
    p.add_argument("--limit", type=int, default=50)
    p = sub.add_parser("neighborhood", help="limited-depth neighborhood")
    p.add_argument("tx_id", type=int)
    p.add_argument("--depth", type=int, default=2)
    p.add_argument("--direction", choices=explore.DIRECTIONS, default="both")
    p = sub.add_parser("step", help="connectivity within a time step")
    p.add_argument("time_step", type=int)

    sub.add_parser("detect", help="run graph anomaly rules and store alerts")
    p = sub.add_parser("evaluate", help="retrospective evaluation on a chronological period")
    p.add_argument("--first-step", type=int, default=35)
    p.add_argument("--last-step", type=int, default=49)
    p = sub.add_parser("alert", help="alert and evidence for a transaction")
    p.add_argument("tx_id", type=int)
    p = sub.add_parser("top", help="highest-degree transactions")
    p.add_argument("--direction", choices=("in", "out"), default="in")
    p.add_argument("--step", type=int)
    p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("paths", help="observed 2/3-edge directed paths")
    p.add_argument("tx_id", type=int)
    p.add_argument("--direction", choices=("in", "out"), default="out")
    p.add_argument("--hops", type=int, choices=(2, 3), default=2)
    p.add_argument("--limit", type=int, default=50)

    a = ap.parse_args(argv)

    if a.cmd == "discover":
        found = find_dataset()
        size = sum(p.stat().st_size for p in found.values()) if found else 0
        _dump({
            "expected_files": list(FILE_NAMES.values()),
            "found": {r: str(p) for r, p in found.items()} if found else None,
            "source_bytes": size,
            "disk": disk_report(a.db, size),
            "note": None if found else "Dataset not found; place the three CSVs under data/raw/",
        })
        return 0

    try:
        if a.cmd == "ingest":
            source = a.source
            if source is None:
                found = find_dataset()
                if found is None:
                    print("Dataset not found. Place the Elliptic CSVs under data/raw/ "
                          "(or pass --source).", file=sys.stderr)
                    return 2
                source = found
            a.db.parent.mkdir(parents=True, exist_ok=True)
            with duckdb.connect(str(a.db)) as con:
                _dump(ingest(con, source, db_path=a.db, replace=a.replace))
            return 0

        if a.cmd == "detect":
            if not a.db.exists():
                print(f"Database not found: {a.db} (run ingest first)", file=sys.stderr)
                return 2
            with duckdb.connect(str(a.db)) as con:
                _dump(run_detection(con, DetectionConfig()))
            return 0

        if not a.db.exists():
            print(f"Database not found: {a.db} (run ingest first)", file=sys.stderr)
            return 2
        with duckdb.connect(str(a.db), read_only=True) as con:
            if a.cmd == "validate":
                _dump(validate(con, components=not a.no_components))
            elif a.cmd == "tx":
                _dump(explore.get_transaction(con, a.tx_id))
            elif a.cmd == "neighbors":
                _dump(explore.neighbors(con, a.tx_id, a.direction, a.limit))
            elif a.cmd == "neighborhood":
                _dump(explore.neighborhood(con, a.tx_id, a.depth, a.direction))
            elif a.cmd == "evaluate":
                _dump(evaluate(con, a.first_step, a.last_step))
            elif a.cmd == "alert":
                _dump(explore.get_alert(con, a.tx_id))
            elif a.cmd == "top":
                _dump(explore.top_nodes(con, a.direction, a.step, a.limit))
            elif a.cmd == "paths":
                _dump(explore.paths(con, a.tx_id, a.direction, a.hops, a.limit))
            elif a.cmd == "step":
                _dump(explore.step_connectivity(con, a.time_step))
        return 0
    except (IngestError, DetectionError, FileNotFoundError, OSError, duckdb.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
