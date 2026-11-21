"""Software motion detection on the low-res Y (luma) stream. Pure NumPy, no camera access."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import MotionConfig, Rect


@dataclass(frozen=True)
class MotionResult:
    score: float            # fraction of the detection area that changed (after speck filter)
    motion: bool            # score >= min_area and past warm-up
    lighting_change: bool   # most of the frame changed at once; background was reset
    brightness: float       # mean luma of the input frame


def build_mask(shape: tuple[int, int], include: tuple[Rect, ...], exclude: tuple[Rect, ...]) -> np.ndarray:
    """Boolean mask of `shape` (h, w) from fractional rectangles."""
    h, w = shape
    mask = np.zeros(shape, dtype=bool)

    def region(r: Rect):
        x0, y0, x1, y1 = r
        return slice(round(y0 * h), round(y1 * h)), slice(round(x0 * w), round(x1 * w))

    for r in include:
        mask[region(r)] = True
    for r in exclude:
        mask[region(r)] = False
    return mask


def _downscale(y: np.ndarray) -> np.ndarray:
    """2x2 average: halves the sensor noise and the work."""
    h, w = y.shape[0] // 2, y.shape[1] // 2
    return y[: h * 2, : w * 2].reshape(h, 2, w, 2).mean(axis=(1, 3), dtype=np.float32)


def _neighbour_count(b: np.ndarray) -> np.ndarray:
    """Number of True cells in each 3x3 neighbourhood (including the centre)."""
    h, w = b.shape
    p = np.pad(b.astype(np.uint8), 1)
    return sum(p[dy:dy + h, dx:dx + w] for dy in range(3) for dx in range(3))


class MotionDetector:
    def __init__(self, cfg: MotionConfig, lores_shape: tuple[int, int]):
        self.cfg = cfg
        h, w = lores_shape
        self.mask = build_mask((h // 2, w // 2), cfg.include, cfg.exclude)
        self._mask_px = int(self.mask.sum())
        if self._mask_px == 0:
            raise ValueError("motion detection area is empty (check [motion] include/exclude)")
        self.reset()

    def reset(self):
        self._bg: np.ndarray | None = None
        self._bg_mean = 0.0
        self._seen = 0

    def _relearn(self, frame: np.ndarray, brightness: float):
        self._bg, self._bg_mean, self._seen = frame, brightness, 1

    def update(self, y: np.ndarray) -> MotionResult:
        cfg = self.cfg
        frame = _downscale(y)
        brightness = float(frame.mean())
        if self._bg is None:
            self._relearn(frame, brightness)
            return MotionResult(0.0, False, False, brightness)

        jump = abs(brightness - self._bg_mean) / max(self._bg_mean, 1.0)
        if self._seen <= cfg.warmup_frames:
            # Warming up (start, or after a lighting change): track the scene frame by frame
            # so nothing from before is left in the background, and keep warming up while
            # the brightness is still moving >3%/frame (AE settling, a lamp fading). Only
            # here: after warm-up, a close animal can move the mean that much (the
            # 2026-11-20 06:14 rabbit did, 3.4% in one frame).
            # (The background is the previous frame here, so `jump` is the per-frame change.)
            self._bg, self._bg_mean = frame, brightness
            self._seen = 1 if jump > cfg.brightness_jump / 5 else self._seen + 1
            return MotionResult(0.0, False, False, brightness)

        changed = np.abs(frame - self._bg) > cfg.pixel_threshold
        if changed.mean() > cfg.lighting_change or jump > cfg.brightness_jump:
            # Headlights, an illuminator switching, AE jumps: relearn instead of triggering.
            # The overall-brightness test catches lamps that light only part of the scene
            # (the grass brightens, the dark sidewalk doesn't).
            self._relearn(frame, brightness)
            return MotionResult(0.0, False, True, brightness)

        solid = changed & (_neighbour_count(changed) >= cfg.min_neighbours)
        score = float(np.count_nonzero(solid & self.mask)) / self._mask_px

        # Learn slowly where things changed so a moving animal doesn't smear into the background.
        a = cfg.background_alpha
        self._bg += np.where(changed, a * 0.1, a).astype(np.float32) * (frame - self._bg)
        self._bg_mean = float(self._bg.mean())
        self._seen += 1
        return MotionResult(score, score >= cfg.min_area, False, brightness)
