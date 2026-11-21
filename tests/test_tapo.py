"""Tapo pieces that run without a camera or ffmpeg: the segment ring, URLs, ffmpeg command
lines, and the app's handling of a camera that stops delivering frames."""

import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from picam.camera import FakeCamera, NoFrame, moving_square_scene
from picam.config import Config, PathsConfig, TapoLogin
from picam.events import EventLog
from picam.main import App
from picam.schedule import NightSchedule
from picam.tapo import (SegmentRing, ffmpeg_join_cmd, ffmpeg_segment_cmd, list_segments, pick,
                        redact, rtsp_url)


def seg(ring, start):
    p = ring / f"seg-{start}.ts"
    p.write_bytes(b"x")
    return p


def make_ring(tmp_path, keep_s=9.0, give_up_s=30.0):
    joined = []
    ring = SegmentRing(tmp_path, keep_s=keep_s, give_up_s=give_up_s,
                       join=lambda parts, out: joined.append(([p.name for p in parts], out.name)))
    return ring, joined


def test_url_quotes_login_and_redacts():
    url = rtsp_url(TapoLogin("10.0.0.3", "cam user", "p@ss:w/rd"), 554, "stream1")
    assert url == "rtsp://cam%20user:p%40ss%3Aw%2Frd@10.0.0.3:554/stream1"
    assert redact(url) == "rtsp://***@10.0.0.3:554/stream1"
    assert redact("connection to rtsp://a:b@h:554/s refused").count("***") == 1
    assert "p%40ss" not in redact(url)


def test_list_segments_orders_and_marks_newest_open(tmp_path):
    for s in (1010, 1002, 1006):
        seg(tmp_path, s)
    (tmp_path / "seg-junk.ts").write_bytes(b"")
    segs = list_segments(tmp_path)
    assert [(s.start, s.end) for s in segs] == [(1002, 1006), (1006, 1010), (1010, None)]


def test_pick_overlapping(tmp_path):
    for s in (100, 102, 104, 106, 108):
        seg(tmp_path, s)
    names = [s.path.name for s in pick(list_segments(tmp_path), 103.0, 106.5)]
    assert names == ["seg-102.ts", "seg-104.ts", "seg-106.ts"]


def test_clip_waits_for_end_piece_then_joins_preroll_to_end(tmp_path):
    ring, joined = make_ring(tmp_path)
    for s in range(1000, 1012, 2):          # 1000..1010, the last still being written
        seg(tmp_path, s)
    ring.open(tmp_path / "clip.mp4", start=1003.0)       # motion at 1008, pre-roll 5 s
    ring.close(tmp_path / "clip.mp4", end=1011.0)
    assert ring.tick(1011.5) == [] and joined == []      # 1011 (+1 s margin) not closed yet
    seg(tmp_path, 1012)
    assert ring.tick(1012.5) == []                       # newest starts before end + margin
    seg(tmp_path, 1014)
    assert ring.tick(1014.5) == [tmp_path / "clip.mp4"]
    assert joined == [(["seg-1002.ts", "seg-1004.ts", "seg-1006.ts", "seg-1008.ts",
                        "seg-1010.ts", "seg-1012.ts"], "clip.mp4")]
    assert ring.clips == []


def test_trim_keeps_preroll_and_open_clips(tmp_path):
    ring, _ = make_ring(tmp_path, keep_s=9.0)
    for s in range(1000, 1032, 2):
        seg(tmp_path, s)
    ring.tick(1031.0)                       # keep pieces ending after 1022
    assert list_segments(tmp_path)[0].start == 1020
    ring.open(tmp_path / "clip.mp4", start=1021.0)
    for s in range(1032, 1060, 2):
        seg(tmp_path, s)
    ring.tick(1059.0)                       # an open clip holds everything from its start
    assert list_segments(tmp_path)[0].start == 1020


def test_clip_given_up_on_when_stream_dies(tmp_path):
    ring, joined = make_ring(tmp_path, give_up_s=0.05)
    for s in (1000, 1002, 1004):
        seg(tmp_path, s)
    ring.open(tmp_path / "clip.mp4", start=1001.0)
    ring.close(tmp_path / "clip.mp4", end=1005.0)
    assert ring.tick(1005.5) == []
    time.sleep(0.1)
    assert ring.tick(1006.0) == [tmp_path / "clip.mp4"]  # wrote what it had
    assert joined[0][0] == ["seg-1000.ts", "seg-1002.ts", "seg-1004.ts"]


def test_flush_writes_open_and_pending_clips(tmp_path):
    ring, joined = make_ring(tmp_path)
    for s in (1000, 1002, 1004):
        seg(tmp_path, s)
    ring.open(tmp_path / "a.mp4", start=999.0)
    assert ring.tick(1005.0, flush=True) == [tmp_path / "a.mp4"]
    assert ring.clips == []


def test_no_pieces_no_clip(tmp_path):
    ring, joined = make_ring(tmp_path, give_up_s=0)
    ring.open(tmp_path / "a.mp4", start=1000.0)
    ring.close(tmp_path / "a.mp4", end=1004.0)
    assert ring.tick(1010.0) == [] and joined == [] and ring.clips == []


def test_failed_join_drops_clip_and_keeps_going(tmp_path):
    def boom(parts, out):
        raise RuntimeError("bad")
    ring = SegmentRing(tmp_path, keep_s=9, give_up_s=0, join=boom)
    seg(tmp_path, 1000)
    ring.open(tmp_path / "a.mp4", start=1000.0)
    ring.close(tmp_path / "a.mp4", end=1001.0)
    assert ring.tick(1002.0) == [] and ring.clips == []


def test_ffmpeg_commands_copy_without_reencoding(tmp_path):
    cmd = ffmpeg_segment_cmd("ffmpeg", "rtsp://u:p@h/stream1", tmp_path, 2.0)
    assert cmd[cmd.index("-c") + 1] == "copy" and "-segment_time" in cmd
    assert cmd[cmd.index("-segment_time") + 1] == "2" and cmd[-1].endswith("seg-%s.ts")
    join = ffmpeg_join_cmd("ffmpeg", [tmp_path / "a.ts", tmp_path / "b.ts"], tmp_path / "o.mp4")
    assert join[join.index("-c") + 1] == "copy"
    assert join[join.index("-i") + 1] == f"concat:{tmp_path / 'a.ts'}|{tmp_path / 'b.ts'}"


# --- the app when frames stop -------------------------------------------------------

class DroppingCamera(FakeCamera):
    """FakeCamera that goes silent (raises NoFrame) for frames in [drop_from, drop_to)."""

    def __init__(self, *a, drop_from, drop_to, **kw):
        super().__init__(*a, **kw)
        self.n = 0
        self.drop = range(drop_from, drop_to)

    @contextmanager
    def frame(self):
        self.n += 1
        if self.n in self.drop:
            raise NoFrame("no frames")
        with super().frame() as f:
            yield f


def test_dropout_mid_event_closes_it_and_recovers(tmp_path):
    TZ = ZoneInfo("America/Chicago")
    cfg = replace(Config(), paths=PathsConfig(data_dir=str(tmp_path)))
    clock = [datetime(2026, 11, 16, 23, 0, tzinfo=TZ)]
    from datetime import timedelta

    def advance(dt):
        clock[0] += timedelta(seconds=dt)

    # Square crosses at 10-12 s (frames 150-180) and again at 40-42 s; drop frames 160-200.
    scene = moving_square_scene(cfg.motion_size, 10, 12, period=30)
    camera = DroppingCamera(cfg.camera, scene, on_frame=advance, drop_from=160, drop_to=200)
    events = EventLog(cfg.paths.db_path)
    app = App(cfg, camera, NightSchedule(cfg.schedule), events, now=lambda: clock[0])
    app.run(threading.Event(), max_frames=50 * 15)
    rows = events.events()
    assert len(rows) == 2                 # the cut-off one, then the next crossing
    assert rows[0]["clip"] != rows[1]["clip"]
    assert camera._open is None
