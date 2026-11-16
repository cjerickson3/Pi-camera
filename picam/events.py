"""SQLite event log."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY,
    night         TEXT NOT NULL,      -- evening date, YYYY-MM-DD
    started_at    TEXT NOT NULL,      -- first motion, ISO 8601 local with offset
    ended_at      TEXT NOT NULL,      -- last motion
    duration_s    REAL NOT NULL,      -- ended_at - started_at
    clip          TEXT,               -- relative to the clips dir
    snapshot      TEXT,               -- JPEG at peak motion, relative to the clips dir
    peak_score    REAL NOT NULL,      -- max fraction of the detection area changed
    mean_score    REAL NOT NULL,      -- mean over motion frames
    motion_frames INTEGER NOT NULL,
    trigger       TEXT NOT NULL,      -- 'camera', 'pir' or 'camera+pir'
    exposure_ms   REAL,               -- at peak
    gain          REAL,               -- at peak
    starred       INTEGER NOT NULL DEFAULT 0,
    note          TEXT
);
CREATE INDEX IF NOT EXISTS events_started ON events(started_at);
CREATE INDEX IF NOT EXISTS events_night ON events(night);
"""


@dataclass
class Event:
    night: str
    started_at: datetime
    ended_at: datetime
    clip: str | None
    snapshot: str | None
    peak_score: float
    mean_score: float
    motion_frames: int
    trigger: str
    exposure_ms: float | None = None
    gain: float | None = None

    @property
    def duration_s(self) -> float:
        return (self.ended_at - self.started_at).total_seconds()


class EventLog:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"{path} has schema v{version}; this code knows v{SCHEMA_VERSION}")
        with self.db:
            self.db.executescript(SCHEMA)
            self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def add(self, e: Event) -> int:
        with self.db:
            cur = self.db.execute(
                """INSERT INTO events (night, started_at, ended_at, duration_s, clip, snapshot,
                       peak_score, mean_score, motion_frames, trigger, exposure_ms, gain)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (e.night, e.started_at.isoformat(timespec="seconds"),
                 e.ended_at.isoformat(timespec="seconds"), round(e.duration_s, 2), e.clip, e.snapshot,
                 round(e.peak_score, 5), round(e.mean_score, 5), e.motion_frames, e.trigger,
                 e.exposure_ms, e.gain))
        return cur.lastrowid

    def events(self, night: str | None = None) -> list[sqlite3.Row]:
        if night is None:
            return self.db.execute("SELECT * FROM events ORDER BY started_at").fetchall()
        return self.db.execute("SELECT * FROM events WHERE night = ? ORDER BY started_at",
                               (night,)).fetchall()

    def close(self):
        self.db.close()
