"""End to end with FakeCamera: frames -> detector -> recorder -> clip, snapshot, DB row."""

import threading
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from picam.camera import FakeCamera, moving_square_scene
from picam.config import Config, PathsConfig
from picam.events import EventLog
from picam.main import App
from picam.schedule import NightSchedule

TZ = ZoneInfo("America/Chicago")
FPS = 15


class FakeClock:
    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def advance(self, dt):
        self.t += timedelta(seconds=dt)


def make_app(tmp_path, dry_run=False, start=datetime(2026, 11, 16, 23, 0, 0, tzinfo=TZ)):
    cfg = replace(Config(), paths=PathsConfig(data_dir=str(tmp_path)))
    clock = FakeClock(start)
    camera = FakeCamera(cfg.camera, moving_square_scene(cfg.camera.lores_size, 10, 12),
                        on_frame=clock.advance)
    events = None if dry_run else EventLog(cfg.paths.db_path)
    app = App(cfg, camera, NightSchedule(cfg.schedule), events, now=clock.now, dry_run=dry_run)
    return app, camera, events, cfg


def test_one_crossing_makes_one_event(tmp_path):
    app, camera, events, cfg = make_app(tmp_path)
    app.run(threading.Event(), max_frames=25 * FPS)

    rows = events.events()
    assert len(rows) == 1
    e = rows[0]
    assert e["night"] == "2026-11-16"
    assert e["started_at"].startswith("2026-11-16T23:00:1")      # first motion ~10 s in
    assert 1.0 < e["duration_s"] < 2.5
    assert e["trigger"] == "camera" and e["peak_score"] > 0
    assert (cfg.paths.clips_dir / e["clip"]).read_bytes() == b"fake mp4\n"
    assert (cfg.paths.clips_dir / e["snapshot"]).stat().st_size > 1000
    assert e["clip"].endswith(".mp4") and e["clip"].startswith("2026-11-16/20261116-23001")
    assert not camera.running                                    # stopped on exit

    csv = (cfg.paths.scores_dir / "2026-11-16.csv").read_text().splitlines()
    assert csv[0].startswith("time,frames,max_score")
    assert 20 <= len(csv) - 1 <= 27


def test_dry_run_writes_no_clips(tmp_path):
    app, camera, events, cfg = make_app(tmp_path, dry_run=True)
    app.run(threading.Event(), max_frames=25 * FPS)
    assert camera.clips == []
    assert not cfg.paths.db_path.exists()
    assert (cfg.paths.scores_dir / "2026-11-16.csv").exists()


def test_daytime_keeps_camera_off(tmp_path):
    app, camera, events, cfg = make_app(tmp_path, start=datetime(2026, 11, 16, 12, 0, tzinfo=TZ))
    stop = threading.Event()
    real = app.schedule.next_change
    app.schedule.next_change = lambda now: (stop.set(), real(now))[1]   # exit after one idle pass
    app.run(stop)
    assert camera.starts == 0


def test_shutdown_mid_event_still_logs_it(tmp_path):
    app, camera, events, cfg = make_app(tmp_path)
    app.run(threading.Event(), max_frames=int(11 * FPS))          # stop while the square is moving
    rows = events.events()
    assert len(rows) == 1 and rows[0]["clip"]
    assert (cfg.paths.clips_dir / rows[0]["clip"]).exists()
