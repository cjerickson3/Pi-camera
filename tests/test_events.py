from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from picam.events import SCHEMA_VERSION, Event, EventLog

TZ = ZoneInfo("America/Chicago")


def make_event(minute=0):
    start = datetime(2026, 11, 16, 23, minute, 5, tzinfo=TZ)
    return Event(night="2026-11-16", started_at=start, ended_at=start + timedelta(seconds=4.5),
                 clip="2026-11-16/20261116-230005.mp4", snapshot="2026-11-16/20261116-230005.jpg",
                 peak_score=0.0312, mean_score=0.012, motion_frames=60, trigger="camera",
                 exposure_ms=66.6, gain=9.85)


def test_add_and_read(tmp_path):
    log = EventLog(tmp_path / "events.db")
    log.add(make_event(1))
    log.add(make_event(0))
    rows = log.events("2026-11-16")
    assert [r["started_at"] for r in rows] == ["2026-11-16T23:00:05-06:00", "2026-11-16T23:01:05-06:00"]
    assert rows[0]["duration_s"] == 4.5 and rows[0]["starred"] == 0
    assert log.events("2026-11-17") == []


def test_wal_and_schema_version(tmp_path):
    log = EventLog(tmp_path / "events.db")
    assert log.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert log.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    log.close()
    EventLog(tmp_path / "events.db").close()   # reopening is fine


def test_newer_schema_refused(tmp_path):
    log = EventLog(tmp_path / "events.db")
    log.db.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    log.close()
    with pytest.raises(RuntimeError, match="schema"):
        EventLog(tmp_path / "events.db")
