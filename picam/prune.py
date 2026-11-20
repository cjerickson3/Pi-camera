"""Storage cleanup: delete old, unstarred clips so the card never fills.

Clips live in clips/<night>/ (night = evening date, YYYY-MM-DD). Two rules, in order:

1. Age: nights more than keep_nights before the current one lose their unstarred files.
2. Space: while free space is under min_free_gb, the oldest remaining night loses its
   unstarred files. The current night is never touched.

Starred clips and snapshots are kept. Deleted files are cleared from their event rows,
which stay in the database as the activity record. Score CSVs are tiny and kept.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from .config import RetentionConfig
from .events import EventLog

log = logging.getLogger("picam.prune")

GB = 1024 ** 3


@dataclass
class PruneResult:
    files: int = 0
    bytes: int = 0
    nights: list[str] = field(default_factory=list)
    free_bytes: int = 0


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def _night_dirs(clips_dir: Path) -> list[tuple[date, Path]]:
    out = []
    for d in clips_dir.iterdir():
        if not d.is_dir():
            continue
        try:
            out.append((date.fromisoformat(d.name), d))
        except ValueError:
            continue                    # not a night folder; leave it alone
    return sorted(out)


def prune(clips_dir: Path, events: EventLog, cfg: RetentionConfig, current_night: str,
          free_bytes: Callable[[Path], int] = _free_bytes) -> PruneResult:
    clips_dir.mkdir(parents=True, exist_ok=True)
    keep = events.starred_files()
    current = date.fromisoformat(current_night)
    cutoff = current - timedelta(days=cfg.keep_nights)
    need = int(cfg.min_free_gb * GB)
    res = PruneResult()
    deleted: set[str] = set()

    def clear(night_dir: Path):
        n = 0
        for f in sorted(night_dir.iterdir()):
            rel = f"{night_dir.name}/{f.name}"
            if not f.is_file() or rel in keep:
                continue
            size = f.stat().st_size
            f.unlink()
            deleted.add(rel)
            res.files += 1
            res.bytes += size
            n += 1
        if not any(night_dir.iterdir()):
            night_dir.rmdir()
        if n:
            res.nights.append(night_dir.name)

    nights = [(d, p) for d, p in _night_dirs(clips_dir) if d < current]
    for d, p in nights:
        if d < cutoff:
            clear(p)
    for d, p in nights:
        if d >= cutoff and p.exists() and free_bytes(clips_dir) < need:
            clear(p)

    if deleted:
        events.forget_files(deleted)
    res.free_bytes = free_bytes(clips_dir)
    if res.files:
        log.info("pruned %d files (%.1f GB) from %s; %.1f GB free", res.files, res.bytes / GB,
                 ", ".join(res.nights), res.free_bytes / GB)
    if res.free_bytes < need:
        log.warning("only %.1f GB free after pruning (want %.1f); the rest is starred, tonight's, "
                    "or not clips", res.free_bytes / GB, cfg.min_free_gb)
    return res
