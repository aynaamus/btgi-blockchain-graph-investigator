"""Locate the Elliptic CSVs and check disk space. Read-only."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

FILE_NAMES = {
    "features": "elliptic_txs_features.csv",
    "classes": "elliptic_txs_classes.csv",
    "edgelist": "elliptic_txs_edgelist.csv",
}
_SKIP_DIRS = {".venv", ".git", "__pycache__", "node_modules", ".cache"}


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_search_roots() -> list[Path]:
    roots = [project_root(), Path.home() / "Downloads"]
    return [r for r in roots if r.is_dir()]


def _find_in(root: Path, max_depth: int) -> dict[str, Path]:
    found: dict[str, Path] = {}
    base_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        if len(Path(dirpath).parts) - base_depth >= max_depth:
            dirnames[:] = []
        for role, name in FILE_NAMES.items():
            if name in filenames and role not in found:
                found[role] = Path(dirpath) / name
    return found


def find_dataset(
    roots: list[Path] | None = None, max_depth: int = 6
) -> dict[str, Path] | None:
    """Return {role: path} for the first root holding all three files.

    Files may sit in nested folders (e.g. an auto-extracted archive); the
    three files are accepted only if they share one directory.
    """
    for root in roots if roots is not None else default_search_roots():
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            if len(Path(dirpath).relative_to(root).parts) >= max_depth:
                dirnames[:] = []
            if all(n in filenames for n in FILE_NAMES.values()):
                return {r: Path(dirpath) / n for r, n in FILE_NAMES.items()}
    return None


def resolve_source(source: Path) -> dict[str, Path]:
    """Resolve an explicit directory (searched recursively) to the three files."""
    source = Path(source)
    if not source.is_dir():
        raise FileNotFoundError(f"Source directory not found: {source}")
    found = find_dataset([source], max_depth=8)
    if found is None:
        partial = _find_in(source, 8)
        missing = [FILE_NAMES[r] for r in FILE_NAMES if r not in partial]
        raise FileNotFoundError(
            f"Missing source file(s) under {source}: {', '.join(missing)}"
        )
    return found


def disk_report(path: Path, source_bytes: int = 0) -> dict:
    """Free-space assessment. Conservative need: 4x source size (DB + temp)."""
    target = path if path.exists() else path.parent
    while not target.exists():
        target = target.parent
    usage = shutil.disk_usage(target)
    required = 4 * source_bytes
    return {
        "free_bytes": usage.free,
        "total_bytes": usage.total,
        "required_bytes": required,
        "sufficient": usage.free >= required + 2 * 1024**3,  # keep 2 GiB headroom
    }


def require_disk_space(path: Path, source_bytes: int) -> dict:
    rep = disk_report(path, source_bytes)
    if not rep["sufficient"]:
        raise OSError(
            f"Insufficient disk space: {rep['free_bytes']/1e9:.1f} GB free, "
            f"need ~{rep['required_bytes']/1e9:.1f} GB plus 2 GB headroom"
        )
    return rep
