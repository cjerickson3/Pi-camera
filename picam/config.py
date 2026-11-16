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
    tuning: str = "imx219_noir.json"
    sensor_mode: tuple[int, int] = (1640, 1232)
    main_size: tuple[int, int] = (1280, 960)
    lores_size: tuple[int, int] = (320, 240)
    fps: float = 15.0
    max_exposure_ms: float = 66.7
    bitrate: int = 3_000_000
    greyscale: bool = True


@dataclass(frozen=True)
class MotionConfig:
    pixel_threshold: float = 12.0
    min_area: float = 0.004
    min_neighbours: int = 4
    background_alpha: float = 0.05
    lighting_change: float = 0.5
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
    stop_after_sunrise_min: float = 0.0


@dataclass(frozen=True)
class PirConfig:
    enabled: bool = False
    gpio_pin: int = 17


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
    motion: MotionConfig = MotionConfig()
    clips: ClipConfig = ClipConfig()
    schedule: ScheduleConfig = ScheduleConfig()
    pir: PirConfig = PirConfig()
    paths: PathsConfig = PathsConfig()


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
    c, m, k = cfg.camera, cfg.motion, cfg.clips
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
    _check(1 <= m.min_neighbours <= 9, "[motion] min_neighbours must be 1..9")
    _check(m.trigger_frames >= 1, "[motion] trigger_frames must be >= 1")
    _check(len(m.include) > 0, "[motion] include needs at least one rectangle")
    for r in (*m.include, *m.exclude):
        _check(len(r) == 4 and all(0 <= v <= 1 for v in r) and r[0] < r[2] and r[1] < r[3],
               f"[motion] bad rectangle {list(r)}: want [x0, y0, x1, y1] fractions, x0<x1, y0<y1")
    _check(0 <= k.preroll_s <= 30, "[clips] preroll_s must be 0..30")
    _check(k.postroll_s > 0, "[clips] postroll_s must be > 0")
    _check(k.max_clip_s > k.postroll_s, "[clips] max_clip_s must exceed postroll_s")
