"""Camera access. The only module that touches picamera2.

PiCamera: real hardware (imported lazily so the rest runs on the PC).
FakeCamera: synthetic frames for tests and for running the app on the PC.
"""

from __future__ import annotations

import threading
import time
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Protocol

import numpy as np

from .config import CameraConfig


class Frame(Protocol):
    t: float                 # monotonic seconds
    lores_y: np.ndarray      # (h, w) uint8 luma of the motion stream
    exposure_ms: float
    gain: float

    def main_y(self) -> np.ndarray: ...   # (H, W) uint8 luma of the recorded stream


class Camera(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...
    def frame(self) -> AbstractContextManager[Frame]: ...
    def start_clip(self, path: Path) -> None: ...
    def stop_clip(self) -> None: ...


def frame_duration_us(cfg: CameraConfig) -> tuple[int, int]:
    """Fixed frame rate; exposure can use the whole frame up to max_exposure_ms."""
    period = int(1e6 / cfg.fps)
    return period, max(period, int(cfg.max_exposure_ms * 1000))


# --- real camera ---------------------------------------------------------------

@dataclass
class _PiFrame:
    t: float
    lores_y: np.ndarray
    exposure_ms: float
    gain: float
    _request: object = field(repr=False)
    _main_size: tuple[int, int] = field(repr=False)

    def main_y(self) -> np.ndarray:
        w, h = self._main_size
        return self._request.make_array("main")[:h, :w]


class PiCamera:
    def __init__(self, cfg: CameraConfig, preroll_s: float):
        from picamera2 import Picamera2
        from picamera2.encoders import H264Encoder
        from picamera2.outputs import CircularOutput2

        self.cfg = cfg
        self._picam2 = Picamera2(tuning=Picamera2.load_tuning_file(cfg.tuning))
        controls = {"FrameDurationLimits": frame_duration_us(cfg)}
        if cfg.greyscale:
            controls["Saturation"] = 0.0
        self._picam2.configure(self._picam2.create_video_configuration(
            main={"size": tuple(cfg.main_size), "format": "YUV420"},
            lores={"size": tuple(cfg.lores_size), "format": "YUV420"},
            raw={"size": tuple(cfg.sensor_mode)},
            controls=controls,
        ))
        # A keyframe every second: a clip can only begin on one, so this bounds the lost start.
        self._encoder = H264Encoder(bitrate=cfg.bitrate, iperiod=max(1, round(cfg.fps)))
        # CircularOutput2 is a delay line: frames reach the open file only once they are
        # preroll_s old. Opening a file therefore starts it preroll_s in the past (the pre-roll),
        # and closing must wait preroll_s for the rest to drain in (see stop_clip).
        self._preroll_s = preroll_s
        self._circular = CircularOutput2(buffer_duration_ms=int(preroll_s * 1000))
        self._lock = threading.Lock()
        self._clip_open = False
        self._closer: threading.Timer | None = None
        self._running = False

    def start(self) -> None:
        if not self._running:
            self._picam2.start_recording(self._encoder, self._circular)
            self._running = True

    def stop(self) -> None:
        with self._lock:
            self._cancel_closer()
            # stop_recording() stops the CircularOutput2, which flushes everything still
            # buffered into an open clip and closes it, so its tail is kept.
            if self._running:
                self._picam2.stop_recording()
                self._running = False
            self._clip_open = False

    def close(self) -> None:
        self.stop()
        self._picam2.close()

    @contextmanager
    def frame(self) -> Iterator[_PiFrame]:
        req = self._picam2.capture_request()
        try:
            md = req.get_metadata()
            w, h = self.cfg.lores_size
            yield _PiFrame(
                t=time.monotonic(),
                lores_y=req.make_array("lores")[:h, :w],
                exposure_ms=md.get("ExposureTime", 0) / 1000,
                gain=md.get("AnalogueGain", 0.0),
                _request=req,
                _main_size=self.cfg.main_size,
            )
        finally:
            req.release()

    def start_clip(self, path: Path) -> None:
        from picamera2.outputs import PyavOutput
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if self._clip_open:
                # Previous clip still draining. Its remaining tail (post-roll) is the same
                # footage as this clip's pre-roll, so cutting it short loses nothing.
                self._cancel_closer()
                self._circular.close_output()
            self._circular.open_output(PyavOutput(str(path)))
            self._clip_open = True

    def stop_clip(self) -> None:
        """End the clip once the delay line has drained into it (preroll_s from now)."""
        with self._lock:
            if self._clip_open and self._closer is None:
                self._closer = threading.Timer(self._preroll_s, self._drained)
                self._closer.daemon = True
                self._closer.start()

    def _drained(self) -> None:
        with self._lock:
            # Runs in the Timer's thread; ignore a timer that was superseded or cancelled.
            if self._closer is not threading.current_thread():
                return
            self._closer = None
            if self._clip_open:
                self._circular.close_output()
                self._clip_open = False

    def _cancel_closer(self) -> None:
        if self._closer is not None:
            self._closer.cancel()
            self._closer = None


# --- fake camera -----------------------------------------------------------------

Scene = Callable[[float], np.ndarray]   # t (s since start) -> lores luma frame


def noise_scene(size: tuple[int, int], seed: int = 1, noise: float = 2.0) -> Scene:
    """Static textured ground plus per-frame sensor noise."""
    w, h = size
    rng = np.random.default_rng(seed)
    ground = rng.normal(40, 10, (h, w))

    def scene(t: float) -> np.ndarray:
        return np.clip(ground + rng.normal(0, noise, (h, w)), 0, 255).astype(np.uint8)

    return scene


def moving_square_scene(size: tuple[int, int], appear: float, vanish: float,
                        square: int = 24, seed: int = 1, period: float | None = None) -> Scene:
    """noise_scene plus a bright square crossing the middle between `appear` and `vanish`.
    With `period`, the crossing repeats every `period` seconds (for --fake demos)."""
    w, h = size
    base = noise_scene(size, seed)

    def scene(t: float) -> np.ndarray:
        frame = base(t)
        tt = t % period if period else t
        if appear <= tt < vanish:
            x = int((tt - appear) / (vanish - appear) * (w - square))
            y = (h - square) // 2
            frame[y:y + square, x:x + square] = np.clip(frame[y:y + square, x:x + square] + 80, 0, 255)
        return frame

    return scene


@dataclass
class _FakeFrame:
    t: float
    lores_y: np.ndarray
    exposure_ms: float
    gain: float
    _main_size: tuple[int, int]

    def main_y(self) -> np.ndarray:
        w, h = self._main_size
        ly, lx = h // self.lores_y.shape[0], w // self.lores_y.shape[1]
        return np.repeat(np.repeat(self.lores_y, ly, axis=0), lx, axis=1)


class FakeCamera:
    def __init__(self, cfg: CameraConfig, scene: Scene | None = None, realtime: bool = False,
                 on_frame: Callable[[float], None] | None = None):
        self.cfg = cfg
        self.scene = scene or noise_scene(cfg.lores_size)
        self.realtime = realtime
        self.on_frame = on_frame          # e.g. advance a fake wall clock
        self.t = 0.0
        self.running = False
        self.starts = 0
        self.clips: list[Path] = []
        self._open: Path | None = None

    def start(self) -> None:
        self.running = True
        self.starts += 1

    def stop(self) -> None:
        self.stop_clip()
        self.running = False

    def close(self) -> None:
        self.stop()

    @contextmanager
    def frame(self) -> Iterator[_FakeFrame]:
        if not self.running:
            raise RuntimeError("camera not started")
        dt = 1 / self.cfg.fps
        if self.realtime:
            time.sleep(dt)
        self.t += dt
        if self.on_frame:
            self.on_frame(dt)
        yield _FakeFrame(self.t, self.scene(self.t), 66.6, 9.85, self.cfg.main_size)

    def start_clip(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._open = path
        self.clips.append(path)

    def stop_clip(self) -> None:
        if self._open:
            self._open.write_bytes(b"fake mp4\n")
            self._open = None
