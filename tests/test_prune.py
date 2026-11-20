from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from picam.config import RetentionConfig
from picam.events import Event, EventLog
from picam.prune import GB, prune

TZ = ZoneInfo("America/Chicago")


def add_night(clips, events, night: str, n=2, size=100):
    """n events in clips/<night>/, each an MP4 + JPEG of `size` bytes."""
    d = clips / night
    d.mkdir(parents=True)
    ids = []
    for i in range(n):
        stem = f"{night.replace('-', '')}-23{i:02d}00"
        (d / f"{stem}.mp4").write_bytes(b"v" * size)
        (d / f"{stem}.jpg").write_bytes(b"j" * size)
        start = datetime.fromisoformat(night).replace(hour=23, minute=i, tzinfo=TZ)
        ids.append(events.add(Event(night=night, started_at=start, ended_at=start + timedelta(seconds=3),
                                    clip=f"{night}/{stem}.mp4", snapshot=f"{night}/{stem}.jpg",
                                    peak_score=0.02, mean_score=0.01, motion_frames=30, trigger="camera")))
    return ids


def star(events, event_id):
    with events.db:
        events.db.execute("UPDATE events SET starred = 1 WHERE id = ?", (event_id,))


def plenty(_):
    return 100 * GB


def nights_on_disk(clips):
    return sorted(p.name for p in clips.iterdir())


def test_age_rule_keeps_recent_and_starred(tmp_path):
    clips, events = tmp_path / "clips", EventLog(tmp_path / "events.db")
    old = add_night(clips, events, "2026-10-01")
    add_night(clips, events, "2026-10-02")
    add_night(clips, events, "2026-11-10")
    star(events, old[1])

    res = prune(clips, events, RetentionConfig(keep_nights=30, min_free_gb=10), "2026-11-16",
                free_bytes=plenty)

    assert res.files == 2 + 4 and res.nights == ["2026-10-01", "2026-10-02"]
    assert nights_on_disk(clips) == ["2026-10-01", "2026-11-10"]           # empty dir removed
    assert sorted(p.name for p in (clips / "2026-10-01").iterdir()) == ["20261001-230100.jpg",
                                                                       "20261001-230100.mp4"]
    rows = {r["id"]: r for r in events.events()}
    assert len(rows) == 6                                                  # rows are kept
    assert rows[old[0]]["clip"] is None and rows[old[0]]["snapshot"] is None
    assert rows[old[1]]["clip"] == "2026-10-01/20261001-230100.mp4"        # starred untouched


def test_space_rule_never_touches_tonight(tmp_path):
    clips, events = tmp_path / "clips", EventLog(tmp_path / "events.db")
    for night in ("2026-11-14", "2026-11-15", "2026-11-16"):
        add_night(clips, events, night)
    res = prune(clips, events, RetentionConfig(keep_nights=30, min_free_gb=10), "2026-11-16",
                free_bytes=lambda _: 0)                                    # disk full, stays full
    assert res.nights == ["2026-11-14", "2026-11-15"]
    assert nights_on_disk(clips) == ["2026-11-16"]
    assert len(list((clips / "2026-11-16").iterdir())) == 4


def test_space_rule_stops_once_enough_is_free(tmp_path):
    clips, events = tmp_path / "clips", EventLog(tmp_path / "events.db")
    for night in ("2026-11-13", "2026-11-14", "2026-11-15"):
        add_night(clips, events, night, size=1000)
    used = lambda: sum(f.stat().st_size for f in clips.rglob("*") if f.is_file())
    # 12000 bytes used; want 5000 of 14000 free -> one night (4000 bytes) is enough.
    res = prune(clips, events, RetentionConfig(keep_nights=30, min_free_gb=5000 / GB), "2026-11-16",
                free_bytes=lambda _: 14000 - used())
    assert res.nights == ["2026-11-13"]
    assert nights_on_disk(clips) == ["2026-11-14", "2026-11-15"]


def test_ignores_non_night_entries(tmp_path):
    clips, events = tmp_path / "clips", EventLog(tmp_path / "events.db")
    (clips / "notes").mkdir(parents=True)
    (clips / "notes" / "keep.txt").write_text("x")
    (clips / "README").write_text("x")
    add_night(clips, events, "2020-01-01")
    res = prune(clips, events, RetentionConfig(keep_nights=1, min_free_gb=10), "2026-11-16",
                free_bytes=lambda _: 0)
    assert res.nights == ["2020-01-01"]
    assert (clips / "notes" / "keep.txt").exists() and (clips / "README").exists()


def test_app_prunes_when_the_window_opens(tmp_path):
    from test_app import make_app
    app, camera, events, cfg = make_app(tmp_path)
    old = date(2026, 11, 16) - timedelta(days=cfg.retention.keep_nights + 1)
    add_night(cfg.paths.clips_dir, events, old.isoformat())
    import threading
    app.run(threading.Event(), max_frames=1)
    assert not (cfg.paths.clips_dir / old.isoformat()).exists()
