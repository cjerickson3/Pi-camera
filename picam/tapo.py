"""Tapo C120 (any RTSP camera, really) as a picam Camera.

Two connections to the camera, each in its own thread and each reconnecting on its own:

- Motion: PyAV decodes the low-res stream (stream2, 640x360), and every 1/detect_fps s a
  luma frame scaled to motion_size goes to the detector through `frame()`.
- Recording: an ffmpeg process copies the high-res stream (stream1) without re-encoding into
  segment_s pieces, `seg-<epoch>.ts`, in a RAM directory (the ring). The ring always holds
  the last few seconds; that is the pre-roll. A clip is the pieces from preroll_s before
  `start_clip()` to `stop_clip()`, joined (still without re-encoding) into an MP4 once the
  piece covering the end has closed. Nothing touches the SD card except finished clips.

Pieces can only start on a keyframe, so a clip's edges are rounded out to the camera's
keyframe interval. MPEG-TS pieces are used because a piece cut short (ffmpeg killed, WiFi
gone) is still readable, and TS pieces join with the concat protocol.

`frame()` raises NoFrame when the motion stream has been silent for stall_s, so the main
loop can close an open event and keep checking the schedule while this reconnects.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Sequence
from urllib.parse import quote

import numpy as np

from .camera import NoFrame
from .config import TapoConfig, TapoLogin

log = logging.getLogger("picam.tapo")

BACKOFF_S = (1, 2, 5, 10, 30, 60)
END_MARGIN_S = 1.0       # stream latency: the piece holding wall time T starts after T
TICK_S = 0.5


def rtsp_url(login: TapoLogin, port: int, stream: str) -> str:
    return f"rtsp://{quote(login.user, safe='')}:{quote(login.password, safe='')}@{login.host}:{port}/{stream}"


def redact(url: str) -> str:
    """URL without the login, for logs."""
    if "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    return f"{scheme}://***@{rest.rsplit('@', 1)[1]}"


# --- segment ring: pure file bookkeeping, no ffmpeg ------------------------------

@dataclass(frozen=True)
class Segment:
    start: float          # wall time (epoch s) ffmpeg opened the piece
    end: float | None     # start of the next piece; None while it is still being written
    path: Path


def list_segments(ring: Path) -> list[Segment]:
    """Pieces in start order. The newest has no successor and is taken to be in progress."""
    found = []
    for p in ring.glob("seg-*.ts"):
        try:
            found.append((int(p.stem[4:]), p))
        except ValueError:
            continue
    found.sort()
    return [Segment(float(s), float(found[i + 1][0]) if i + 1 < len(found) else None, p)
            for i, (s, p) in enumerate(found)]


def pick(segments: Sequence[Segment], start: float, end: float) -> list[Segment]:
    """Pieces overlapping [start, end]."""
    return [s for s in segments if (s.end is None or s.end > start) and s.start <= end]


@dataclass
class _Clip:
    path: Path
    start: float                  # wall time the clip should begin (pre-roll included)
    end: float | None = None      # set by stop_clip
    stopped_at: float = 0.0       # monotonic, for the give-up timeout


@dataclass
class SegmentRing:
    """Keeps the ring trimmed and turns finished clips into MP4s.

    `tick()` is called a couple of times a second. Pieces are deleted once they are older
    than `keep_s` and not part of any open or pending clip.
    """

    ring: Path
    keep_s: float
    give_up_s: float                                  # stop waiting for the end piece after this
    join: Callable[[list[Path], Path], None]
    clips: list[_Clip] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def open(self, path: Path, start: float):
        with self.lock:
            self.clips.append(_Clip(path, start))

    def close(self, path: Path, end: float):
        with self.lock:
            for c in self.clips:
                if c.path == path and c.end is None:
                    c.end, c.stopped_at = end, time.monotonic()

    def tick(self, now: float, *, flush: bool = False) -> list[Path]:
        """Write the clips whose pieces are all in (or all clips if `flush`), then trim.
        Returns the clips written."""
        segs = list_segments(self.ring)
        with self.lock:
            done = [c for c in self.clips if c.end is not None and (
                flush or self._complete(segs, c.end) or time.monotonic() - c.stopped_at > self.give_up_s)]
            if flush:
                for c in self.clips:
                    if c.end is None:
                        c.end = now
                        done.append(c)
        written = [c.path for c in done if self._write(segs, c)]
        with self.lock:
            self.clips = [c for c in self.clips if c not in done]
            oldest_needed = min([now - self.keep_s, *(c.start for c in self.clips)])
        for s in segs:
            if s.end is not None and s.end < oldest_needed:
                s.path.unlink(missing_ok=True)
        return written

    @staticmethod
    def _complete(segs: Sequence[Segment], end: float) -> bool:
        return bool(segs) and segs[-1].start > end + END_MARGIN_S

    def _write(self, segs: Sequence[Segment], c: _Clip) -> bool:
        parts = pick(segs, c.start, c.end + END_MARGIN_S)
        if not parts:
            log.warning("no video for %s (recording stream down?)", c.path.name)
            return False
        try:
            self.join([s.path for s in parts], c.path)
            return True
        except Exception:
            log.exception("joining %d pieces into %s failed", len(parts), c.path.name)
            return False


def ffmpeg_segment_cmd(ffmpeg: str, url: str, ring: Path, segment_s: float) -> list[str]:
    return [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
            "-rtsp_transport", "tcp", "-timeout", "5000000",       # socket timeout, microseconds
            "-i", url, "-map", "0:v:0", "-c", "copy",              # video only, no re-encode
            "-f", "segment", "-segment_time", f"{segment_s:g}", "-segment_format", "mpegts",
            "-strftime", "1", str(ring / "seg-%s.ts")]


def ffmpeg_join_cmd(ffmpeg: str, parts: Sequence[Path], out: Path) -> list[str]:
    return [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-i", "concat:" + "|".join(str(p) for p in parts),
            "-c", "copy", "-movflags", "+faststart", str(out)]


# --- the camera ----------------------------------------------------------------

@dataclass
class _TapoFrame:
    t: float
    lores_y: np.ndarray
    _frame: object = field(repr=False)        # av.VideoFrame, converted only for a snapshot
    exposure_ms: float = 0.0                  # not reported over RTSP
    gain: float = 0.0

    def main_y(self) -> np.ndarray:
        return self._frame.to_ndarray(format="gray")


class TapoCamera:
    def __init__(self, cfg: TapoConfig, login: TapoLogin, preroll_s: float, max_clip_s: float):
        import av  # noqa: F401  (fail at startup, not at dusk, if PyAV is missing)

        self.cfg = cfg
        self._motion_url = rtsp_url(login, cfg.port, cfg.motion_stream)
        self._record_url = rtsp_url(login, cfg.port, cfg.record_stream)
        self._preroll_s = preroll_s
        self.ring = SegmentRing(
            Path(cfg.ring_dir), keep_s=preroll_s + 2 * cfg.segment_s,
            give_up_s=3 * cfg.segment_s + cfg.stall_s, join=self._join)
        self._frames: deque[_TapoFrame] = deque(maxlen=max(2, int(cfg.detect_fps * 2)))
        self._have_frame = threading.Condition()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._ffmpeg: subprocess.Popen | None = None
        self._open_clip: Path | None = None

    # Camera protocol

    def start(self) -> None:
        if self._threads:
            return
        ring = Path(self.cfg.ring_dir)
        ring.mkdir(parents=True, exist_ok=True)
        for old in ring.glob("seg-*.ts"):
            old.unlink(missing_ok=True)
        self._stop.clear()
        self._frames.clear()
        self._threads = [threading.Thread(target=self._motion_loop, name="tapo-motion", daemon=True),
                         threading.Thread(target=self._record_loop, name="tapo-record", daemon=True)]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        if not self._threads:
            return
        self._stop.set()
        with self._have_frame:
            self._have_frame.notify_all()
        for t in self._threads:
            t.join(timeout=15)
        self._threads = []
        self._open_clip = None

    def close(self) -> None:
        self.stop()

    @contextmanager
    def frame(self) -> Iterator[_TapoFrame]:
        deadline = time.monotonic() + self.cfg.stall_s
        with self._have_frame:
            while not self._frames:
                left = deadline - time.monotonic()
                if left <= 0 or self._stop.is_set():
                    raise NoFrame(f"no frames from {self.cfg.motion_stream} for {self.cfg.stall_s:g} s")
                self._have_frame.wait(left)
            f = self._frames.popleft()
        yield f

    def start_clip(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if self._open_clip is not None:          # shouldn't happen; keep what we have
            self.stop_clip()
        self.ring.open(path, time.time() - self._preroll_s)
        self._open_clip = path

    def stop_clip(self) -> None:
        if self._open_clip is not None:
            self.ring.close(self._open_clip, time.time())
            self._open_clip = None

    # motion stream

    def _motion_loop(self):
        import av
        w, h = self.cfg.motion_size
        period = 1.0 / self.cfg.detect_fps
        failures = 0
        while not self._stop.is_set():
            try:
                with av.open(self._motion_url, options={"rtsp_transport": "tcp"}, timeout=(10.0, 5.0)) as c:
                    stream = c.streams.video[0]
                    stream.thread_type = "AUTO"
                    log.info("motion stream connected: %s %dx%d", redact(self._motion_url),
                             stream.codec_context.width, stream.codec_context.height)
                    last = 0.0
                    for fr in c.decode(stream):
                        if self._stop.is_set():
                            return
                        failures = 0
                        now = time.monotonic()
                        if now - last < period * 0.9:
                            continue
                        last = now
                        y = fr.reformat(width=w, height=h, format="gray").to_ndarray()
                        with self._have_frame:
                            self._frames.append(_TapoFrame(now, y, fr))
                            self._have_frame.notify()
                log.warning("motion stream ended")
            except Exception as e:
                log.warning("motion stream: %s", e)
            failures += 1
            self._stop.wait(BACKOFF_S[min(failures, len(BACKOFF_S)) - 1])

    # recording stream

    def _record_loop(self):
        ring = Path(self.cfg.ring_dir)
        failures = 0
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                errors: deque[str] = deque(maxlen=5)
                try:
                    self._ffmpeg = subprocess.Popen(
                        ffmpeg_segment_cmd(self.cfg.ffmpeg, self._record_url, ring, self.cfg.segment_s),
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                except OSError as e:          # e.g. ffmpeg not installed: no clips, motion still logged
                    reason = f"could not start {self.cfg.ffmpeg} ({e})"
                else:
                    threading.Thread(target=_drain, args=(self._ffmpeg.stderr, errors),
                                     name="tapo-ffmpeg-stderr", daemon=True).start()
                    log.info("recording stream: ffmpeg started (%s)", redact(self._record_url))
                    reason = self._watch_ffmpeg(started)
                if self._stop.is_set():
                    break
                if errors:
                    reason += ": " + redact(errors[-1])
                # A run that worked for a minute was a dropout, not a broken setup: retry fast.
                failures = 1 if time.monotonic() - started > 60 else failures + 1
                wait = BACKOFF_S[min(failures, len(BACKOFF_S)) - 1]
                log.warning("recording stream %s; restarting in %d s", reason, wait)
                self._stop.wait(wait)
        finally:
            self._kill_ffmpeg()
            try:
                self.ring.tick(time.time(), flush=True)
            except Exception:
                log.exception("writing clips at shutdown failed")

    def _watch_ffmpeg(self, started: float) -> str:
        """Tick the ring until ffmpeg exits or stops producing pieces. Returns why it ended."""
        p = self._ffmpeg
        while not self._stop.is_set():
            self.ring.tick(time.time())
            if p.poll() is not None:
                return f"ffmpeg exited ({p.returncode})"
            segs = list_segments(Path(self.cfg.ring_dir))
            newest = segs[-1].path.stat().st_mtime if segs else None
            idle = time.time() - newest if newest else time.monotonic() - started
            if idle > self.cfg.stall_s:
                self._kill_ffmpeg()
                return f"stalled ({idle:.0f} s without data)"
            self._stop.wait(TICK_S)
        return "stopped"

    def _kill_ffmpeg(self):
        p, self._ffmpeg = self._ffmpeg, None
        if p is None or p.poll() is not None:
            return
        p.terminate()                   # ffmpeg closes the current piece cleanly on SIGTERM
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()

    def _join(self, parts: list[Path], out: Path):
        tmp = out.with_name(out.stem + ".part.mp4")
        r = subprocess.run(ffmpeg_join_cmd(self.cfg.ffmpeg, parts, tmp),
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
        if r.returncode != 0:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(r.stderr.decode(errors="replace").strip()[-500:])
        tmp.replace(out)
        log.info("clip written: %s (%d pieces)", out.name, len(parts))


def _drain(pipe, keep: deque):
    """Read ffmpeg's stderr so it can never fill the pipe and block ffmpeg; keep the last lines."""
    for raw in pipe:
        line = raw.decode(errors="replace").strip()
        if line:
            keep.append(line)
            log.debug("ffmpeg: %s", redact(line))
