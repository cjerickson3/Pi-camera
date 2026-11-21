"""Load and validate config.toml. Every tunable lives there; see that file for meanings."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

Rect = tuple[float, float, float, float]   # x0, y0, x1, y1 as fractions of the frame

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = REPO_ROOT / "config.toml"


@dataclass(frozen=True)
class CameraConfig:
    source: str = "picam"           # "picam" (Camera Module on the CSI port) or "tapo" (RTSP)
    tuning: str = "imx219_noir.json"
    sensor_mode: tuple[int, int] = (1640, 1232)
    main_size: tuple[int, int] = (1280, 960)
    lores_size: tuple[int, int] = (320, 240)
    fps: float = 15.0
    max_exposure_ms: float = 66.7
    bitrate: int = 3_000_000
    greyscale: bool = True


@dataclass(frozen=True)
class TapoConfig:
    """RTSP camera (Tapo C120). Host and login come from .env, never from config.toml."""
    port: int = 554
    motion_stream: str = "stream2"          # low-res stream decoded for motion detection
    record_stream: str = "stream1"          # high-res stream copied (not re-encoded) into clips
    motion_size: tuple[int, int] = (320, 180)
    detect_fps: float = 10.0
    segment_s: float = 2.0
    ring_dir: str = "/dev/shm/picam-tapo"
    stall_s: float = 10.0
    ffmpeg: str = "ffmpeg"


@dataclass(frozen=True)
class MotionConfig:
    pixel_threshold: float = 12.0
    min_area: float = 0.004
    min_neighbours: int = 4
    background_alpha: float = 0.05
    lighting_change: float = 0.5
    brightness_jump: float = 0.15
    warmup_frames: int = 30
    trigger_frames: int = 3
    include: tuple[Rect, ...] = ((0.0, 0.0, 1.0, 1.0),)
    exclude: tuple[Rect, ...] = ()


@dataclass(frozen=True)
class ClipConfig:
    preroll_s: float = 5.0
    postroll_s: float = 5.0
    max_clip_s: float = 60.0


@dataclass(frozen=True)
class ScheduleConfig:
    latitude: float = 33.02
    longitude: float = -96.70
    timezone: str = "America/Chicago"
    start_after_sunset_min: float = 60.0
    stop_after_sunrise_min: float = -30.0


@dataclass(frozen=True)
class PirConfig:
    enabled: bool = False
    gpio_pin: int = 17


@dataclass(frozen=True)
class RetentionConfig:
    keep_nights: int = 30
    min_free_gb: float = 10.0


@dataclass(frozen=True)
class ReviewConfig:
    bind: str = "127.0.0.1"
    port: int = 8080


@dataclass(frozen=True)
class PathsConfig:
    data_dir: str = "~/picam-data"

    @property
    def root(self) -> Path:
        return Path(self.data_dir).expanduser()

    @property
    def clips_dir(self) -> Path:
        return self.root / "clips"

    @property
    def db_path(self) -> Path:
        return self.root / "events.db"

    @property
    def scores_dir(self) -> Path:
        return self.root / "scores"


@dataclass(frozen=True)
class Config:
    camera: CameraConfig = CameraConfig()
    tapo: TapoConfig = TapoConfig()
    motion: MotionConfig = MotionConfig()
    clips: ClipConfig = ClipConfig()
    schedule: ScheduleConfig = ScheduleConfig()
    pir: PirConfig = PirConfig()
    retention: RetentionConfig = RetentionConfig()
    review: ReviewConfig = ReviewConfig()
    paths: PathsConfig = PathsConfig()


    @property
    def motion_size(self) -> tuple[int, int]:
        """(width, height) of the luma frames the motion detector gets."""
        return self.tapo.motion_size if self.camera.source == "tapo" else self.camera.lores_size


_SECTIONS = {f.name: f.default.__class__ for f in fields(Config)}


def load(path: Path | str = DEFAULT_PATH) -> Config:
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    return from_dict(data)


def from_dict(data: dict) -> Config:
    unknown = set(data) - set(_SECTIONS)
    if unknown:
        raise ValueError(f"unknown config section(s): {', '.join(sorted(unknown))}")
    cfg = Config(**{name: _build(cls, data.get(name, {}), name) for name, cls in _SECTIONS.items()})
    _validate(cfg)
    return cfg


def _build(cls, raw: dict, section: str):
    names = {f.name for f in fields(cls)}
    unknown = set(raw) - names
    if unknown:
        raise ValueError(f"[{section}] unknown key(s): {', '.join(sorted(unknown))}")
    return cls(**{k: _tuplify(v) for k, v in raw.items()})


def _tuplify(v):
    return tuple(_tuplify(x) for x in v) if isinstance(v, list) else v


def _check(ok: bool, msg: str):
    if not ok:
        raise ValueError(msg)


def _validate(cfg: Config):
    c, m, k, tp = cfg.camera, cfg.motion, cfg.clips, cfg.tapo
    _check(c.source in ("picam", "tapo"), '[camera] source must be "picam" or "tapo"')
    _check(len(tp.motion_size) == 2 and all(int(v) == v and v > 0 and v % 2 == 0 for v in tp.motion_size),
           "[tapo] motion_size must be [width, height], both even")
    _check(tp.detect_fps > 0, "[tapo] detect_fps must be > 0")
    _check(tp.segment_s >= 1, "[tapo] segment_s must be >= 1")
    _check(tp.stall_s >= 2 * tp.segment_s, "[tapo] stall_s must be at least 2 x segment_s")
    _check(c.fps > 0, "[camera] fps must be > 0")
    _check(c.max_exposure_ms > 0, "[camera] max_exposure_ms must be > 0")
    for name in ("main_size", "lores_size", "sensor_mode"):
        size = getattr(c, name)
        _check(len(size) == 2 and all(int(v) == v and v > 0 for v in size),
               f"[camera] {name} must be [width, height]")
    _check(all(v % 2 == 0 for v in c.lores_size), "[camera] lores_size must be even")
    _check(0 < m.min_area < 1, "[motion] min_area must be between 0 and 1")
    _check(0 < m.background_alpha <= 1, "[motion] background_alpha must be in (0, 1]")
    _check(0 < m.lighting_change <= 1, "[motion] lighting_change must be in (0, 1]")
    _check(0 < m.brightness_jump <= 1, "[motion] brightness_jump must be in (0, 1]")
    _check(m.warmup_frames >= 1, "[motion] warmup_frames must be >= 1")
    _check(1 <= m.min_neighbours <= 9, "[motion] min_neighbours must be 1..9")
    _check(m.trigger_frames >= 1, "[motion] trigger_frames must be >= 1")
    _check(len(m.include) > 0, "[motion] include needs at least one rectangle")
    for r in (*m.include, *m.exclude):
        _check(len(r) == 4 and all(0 <= v <= 1 for v in r) and r[0] < r[2] and r[1] < r[3],
               f"[motion] bad rectangle {list(r)}: want [x0, y0, x1, y1] fractions, x0<x1, y0<y1")
    _check(0 <= k.preroll_s <= 30, "[clips] preroll_s must be 0..30")
    _check(k.postroll_s > 0, "[clips] postroll_s must be > 0")
    _check(k.max_clip_s > k.postroll_s, "[clips] max_clip_s must exceed postroll_s")
    _check(cfg.retention.keep_nights >= 1, "[retention] keep_nights must be >= 1")
    _check(cfg.retention.min_free_gb >= 0, "[retention] min_free_gb must be >= 0")
    _check(0 < cfg.review.port < 65536, "[review] port must be 1..65535")


ENV_PATH = REPO_ROOT / ".env"


def read_env(path: Path | str = ENV_PATH) -> dict[str, str]:
    """KEY=value lines from a .env file (comments and blanks skipped), overridden by
    the process environment. Missing file = environment only."""
    import os
    values: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    values.update({k: v for k, v in os.environ.items() if k.startswith("TAPO_")})
    return values


@dataclass(frozen=True)
class TapoLogin:
    host: str
    user: str
    password: str


def tapo_login(path: Path | str = ENV_PATH) -> TapoLogin:
    env = read_env(path)
    missing = [k for k in ("TAPO_HOST", "TAPO_USER", "TAPO_PASSWORD") if not env.get(k)]
    if missing:
        raise ValueError(f"{', '.join(missing)} not set: put them in {path} (see .env.example)")
    return TapoLogin(env["TAPO_HOST"], env["TAPO_USER"], env["TAPO_PASSWORD"])
