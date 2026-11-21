"""Main loop: schedule -> camera -> motion -> recorder -> clips + event log.

    python3 -m picam                  # normal run (night window only)
    python3 -m picam --always-on      # ignore the schedule, e.g. daytime testing
    python3 -m picam --dry-run        # detect and log only: no clips, no database
    python3 -m picam --fake           # synthetic camera; runs on the PC
"""

from __future__ import annotations

import argparse
import logging
import signal
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Sequence

import numpy as np

from .camera import Camera, Frame, NoFrame
from .config import DEFAULT_PATH, Config, load as load_config
from .events import Event, EventLog
from .motion import MotionDetector, MotionResult
from .prune import prune
from .recorder import Action, Recorder
from .schedule import NightSchedule
from .triggers import Trigger, build_triggers

log = logging.getLogger("picam")

HEARTBEAT_S = 600


@dataclass
class _OpenEvent:
    night: str
    started_at: datetime
    clip: Path
    score_sum: float = 0.0
    peak_score: float = 0.0
    motion_frames: int = 0
    sources: set[str] = field(default_factory=set)
    exposure_ms: float | None = None
    gain: float | None = None
    snapshot: np.ndarray | None = None
    snapshot_score: float = -1.0
    snapshot_t: float = float("-inf")


class ScoreLog:
    """One CSV row per second to scores/<night>.csv (for tuning sensitivity and seeing
    when things move), plus a heartbeat log line every HEARTBEAT_S."""

    HEADER = "time,frames,max_score,motion_frames,lighting_changes,brightness,exposure_ms,gain\n"

    def __init__(self, directory: Path):
        self.dir = directory
        self._fh = None
        self._night = None
        self._sec = None
        self._reset_row()
        self._beat_t = time.monotonic()
        self._beat = {"frames": 0, "max_score": 0.0, "events": 0}

    def _reset_row(self):
        self._row = {"frames": 0, "max_score": 0.0, "motion": 0, "lighting": 0,
                     "brightness": 0.0, "exposure_ms": 0.0, "gain": 0.0}

    def add(self, wall: datetime, night: str, res: MotionResult, motion: bool, f: Frame):
        sec = wall.replace(microsecond=0)
        if self._sec is not None and sec != self._sec:
            self._flush()
        if night != self._night:
            self._open(night)
        self._sec = sec
        r = self._row
        r["frames"] += 1
        r["max_score"] = max(r["max_score"], res.score)
        r["motion"] += motion
        r["lighting"] += res.lighting_change
        r["brightness"], r["exposure_ms"], r["gain"] = res.brightness, f.exposure_ms, f.gain

        b = self._beat
        b["frames"] += 1
        b["max_score"] = max(b["max_score"], res.score)
        now = time.monotonic()
        if now - self._beat_t >= HEARTBEAT_S:
            log.info("heartbeat: %.1f fps, brightness %.0f, exp %.1f ms, gain %.2f, max score %.4f, "
                     "%d events", b["frames"] / (now - self._beat_t), res.brightness, f.exposure_ms,
                     f.gain, b["max_score"], b["events"])
            self._beat_t, self._beat = now, {"frames": 0, "max_score": 0.0, "events": 0}

    def count_event(self):
        self._beat["events"] += 1

    def _open(self, night: str):
        self._flush()
        if self._fh:
            self._fh.close()
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{night}.csv"
        new = not path.exists()
        self._fh = open(path, "a", buffering=1, encoding="utf-8")
        if new:
            self._fh.write(self.HEADER)
        self._night = night

    def _flush(self):
        r = self._row
        if self._fh and self._sec is not None and r["frames"]:
            self._fh.write(f"{self._sec.isoformat()},{r['frames']},{r['max_score']:.5f},{r['motion']},"
                           f"{r['lighting']},{r['brightness']:.1f},{r['exposure_ms']:.1f},{r['gain']:.2f}\n")
        self._reset_row()

    def close(self):
        self._flush()
        if self._fh:
            self._fh.close()
            self._fh = None


class App:
    def __init__(self, cfg: Config, camera: Camera, schedule: NightSchedule, events: EventLog | None,
                 *, now: Callable[[], datetime], dry_run: bool = False, triggers: Sequence[Trigger] = ()):
        self.cfg = cfg
        self.camera = camera
        self.schedule = schedule
        self.events = events
        self.now = now
        self.dry_run = dry_run
        self.triggers = list(triggers)
        w, h = cfg.motion_size
        self.detector = MotionDetector(cfg.motion, (h, w))
        self.scores = ScoreLog(cfg.paths.scores_dir)
        self._new_recorder()
        self._running = False
        self._event: _OpenEvent | None = None
        self._last_t = 0.0
        self._no_frames_since: float | None = None

    def _new_recorder(self):
        c = self.cfg
        self.recorder = Recorder(c.motion.trigger_frames, c.clips.postroll_s, c.clips.max_clip_s)

    def run(self, stop: threading.Event, max_frames: int | None = None) -> int:
        frames = 0
        try:
            while not stop.is_set() and (max_frames is None or frames < max_frames):
                wall = self.now()
                if not self.schedule.is_active(wall):
                    self._go_idle(wall)
                    wait = (self.schedule.next_change(wall) - wall).total_seconds()
                    stop.wait(min(max(wait, 1.0), 60.0))
                    continue
                if not self._running:
                    self._go_active(wall)
                try:
                    with self.camera.frame() as f:
                        self._process(f, self.now())
                except NoFrame as e:
                    self._lost_frames(e)
                    continue
                if self._no_frames_since is not None:
                    log.info("frames back after %.0f s", time.monotonic() - self._no_frames_since)
                    self._no_frames_since = None
                frames += 1
        finally:
            self._go_idle(self.now())
            self.scores.close()
        return frames

    def _lost_frames(self, e: NoFrame):
        """The camera stopped delivering (WiFi camera dropped). Close any open event with what
        was recorded, and start the background over when frames return."""
        if self._no_frames_since is None:
            self._no_frames_since = time.monotonic()
            log.warning("%s; waiting for the camera to reconnect", e)
        if self._event is not None:
            self.recorder.stop()
            self._finish_event(self._last_t, self.now())
        self.detector.reset()

    def _go_active(self, wall: datetime):
        self._prune(wall)
        self.camera.start()
        self.detector.reset()
        self._new_recorder()
        self._running = True
        until = "" if self.schedule.always_on else f" until {self.schedule.next_change(wall):%H:%M}"
        log.info("camera on%s%s", until, " (dry run: no clips, no database)" if self.dry_run else "")

    def _prune(self, wall: datetime):
        if self.events is None:         # dry run: no clips, and no record of what is starred
            return
        try:
            prune(self.cfg.paths.clips_dir, self.events, self.cfg.retention,
                  self.schedule.night_label(wall))
        except Exception:
            log.exception("pruning failed; recording anyway")

    def _go_idle(self, wall: datetime):
        if self._event is not None:
            self.recorder.stop()
            self._finish_event(self._last_t, wall)
        if self._running:
            self.camera.stop()
            self._running = False
            log.info("camera off; next start %s",
                     f"{self.schedule.next_change(wall):%Y-%m-%d %H:%M}" if not self.schedule.always_on
                     else "on demand")

    def _process(self, f: Frame, wall: datetime):
        self._last_t = f.t
        res = self.detector.update(f.lores_y)
        fired = [t.name for t in self.triggers if t.active()]
        motion = res.motion or bool(fired)
        action = self.recorder.update(f.t, motion)

        if action is Action.START:
            self._start_event(f, wall)
        ev = self._event
        if ev is not None and motion:
            ev.motion_frames += 1
            ev.score_sum += res.score
            ev.sources.update(fired)
            if res.motion:
                ev.sources.add("camera")
            if res.score > ev.peak_score:
                ev.peak_score, ev.exposure_ms, ev.gain = res.score, round(f.exposure_ms, 1), round(f.gain, 2)
            # Snapshot at peak motion, at most once a second (copying the main frame isn't free).
            if res.score > ev.snapshot_score and f.t - ev.snapshot_t >= 1.0:
                ev.snapshot, ev.snapshot_score, ev.snapshot_t = f.main_y(), res.score, f.t
        if action is Action.STOP:
            self._finish_event(f.t, wall)

        self.scores.add(wall, self.schedule.night_label(wall), res, motion, f)

    def _start_event(self, f: Frame, wall: datetime):
        started = wall - timedelta(seconds=f.t - self.recorder.first_motion_t)
        night = self.schedule.night_label(started)
        stamp = started.astimezone(self.schedule.tz).strftime("%Y%m%d-%H%M%S")
        clip = _unique(self.cfg.paths.clips_dir / night / f"{stamp}.mp4")
        if not self.dry_run:
            self.camera.start_clip(clip)
        self._event = _OpenEvent(night, started, clip)
        log.info("motion: %s%s", clip.name, " (dry run)" if self.dry_run else "")

    def _finish_event(self, t: float, wall: datetime):
        ev, self._event = self._event, None
        if not self.dry_run:
            self.camera.stop_clip()
        ended = wall - timedelta(seconds=max(0.0, t - self.recorder.last_motion_t))
        ended = max(ended, ev.started_at)
        clips_dir = self.cfg.paths.clips_dir
        snapshot = None
        if not self.dry_run and ev.snapshot is not None:
            path = ev.clip.with_suffix(".jpg")
            _save_jpeg(ev.snapshot, path)
            snapshot = path.relative_to(clips_dir).as_posix()
        event = Event(
            night=ev.night, started_at=ev.started_at, ended_at=ended,
            clip=None if self.dry_run else ev.clip.relative_to(clips_dir).as_posix(),
            snapshot=snapshot, peak_score=ev.peak_score,
            mean_score=ev.score_sum / ev.motion_frames if ev.motion_frames else 0.0,
            motion_frames=ev.motion_frames, trigger="+".join(sorted(ev.sources)) or "camera",
            exposure_ms=ev.exposure_ms, gain=ev.gain)
        if self.events is not None:
            self.events.add(event)
        self.scores.count_event()
        log.info("event: %s, %.1f s of motion, peak %.4f, %s", ev.clip.name, event.duration_s,
                 event.peak_score, event.trigger)


def _unique(path: Path) -> Path:
    n = 2
    candidate = path
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}-{n}{path.suffix}")
        n += 1
    return candidate


def _save_jpeg(y: np.ndarray, path: Path):
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.ascontiguousarray(y), "L").save(path, quality=85)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="picam", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=DEFAULT_PATH)
    ap.add_argument("--dry-run", action="store_true", help="detect and log only: no clips, no database")
    ap.add_argument("--always-on", action="store_true", help="ignore the night schedule")
    ap.add_argument("--fake", action="store_true", help="synthetic camera (runs on the PC)")
    ap.add_argument("--duration", type=float, metavar="SECONDS", help="stop after this long")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("picamera2", "libcamera"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    cfg = load_config(args.config)
    schedule = NightSchedule(cfg.schedule, always_on=args.always_on)
    if args.fake:
        from .camera import FakeCamera, moving_square_scene
        camera = FakeCamera(cfg.camera, moving_square_scene(cfg.motion_size, 10, 12, period=30),
                            realtime=True)
    elif cfg.camera.source == "tapo":
        from .config import tapo_login
        from .tapo import TapoCamera
        camera = TapoCamera(cfg.tapo, tapo_login(), cfg.clips.preroll_s, cfg.clips.max_clip_s)
    else:
        from .camera import PiCamera
        camera = PiCamera(cfg.camera, cfg.clips.preroll_s)
    events = None if args.dry_run else EventLog(cfg.paths.db_path)
    triggers = build_triggers(cfg)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    if args.duration:
        timer = threading.Timer(args.duration, stop.set)
        timer.daemon = True
        timer.start()

    log.info("picam starting: %s camera, data in %s", "fake" if args.fake else cfg.camera.source,
             cfg.paths.root)
    app = App(cfg, camera, schedule, events, now=schedule.now, dry_run=args.dry_run, triggers=triggers)
    try:
        app.run(stop)
    finally:
        camera.close()
        if events is not None:
            events.close()
        for t in triggers:
            t.close()
    log.info("picam stopped")
    return 0
