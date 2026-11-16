import numpy as np
import pytest

from picam.camera import moving_square_scene, noise_scene
from picam.config import MotionConfig
from picam.motion import MotionDetector, build_mask

SIZE = (320, 240)          # w, h
SHAPE = (240, 320)         # h, w
DT = 1 / 15


def run(detector, scene, seconds):
    return [detector.update(scene(i * DT)) for i in range(1, int(seconds / DT) + 1)]


def test_mask_from_rectangles():
    m = build_mask((10, 20), ((0.0, 0.0, 0.5, 1.0),), ((0.0, 0.0, 0.25, 0.5),))
    assert m[:, :10].sum() == 100 - 25 and not m[:, 10:].any()


def test_empty_area_rejected():
    with pytest.raises(ValueError, match="empty"):
        MotionDetector(MotionConfig(include=((0.0, 0.0, 1.0, 1.0),), exclude=((0.0, 0.0, 1.0, 1.0),)), SHAPE)


def test_static_noisy_scene_never_triggers():
    results = run(MotionDetector(MotionConfig(), SHAPE), noise_scene(SIZE, noise=3.0), 20)
    assert not any(r.motion for r in results)


def test_no_motion_during_warmup():
    cfg = MotionConfig(warmup_frames=30)
    results = run(MotionDetector(cfg, SHAPE), moving_square_scene(SIZE, 0, 2), 2)
    assert not any(r.motion for r in results[:30])


def test_moving_square_triggers_and_stops():
    results = run(MotionDetector(MotionConfig(), SHAPE), moving_square_scene(SIZE, 5, 7), 12)
    in_motion = [r.motion for r in results]
    assert any(in_motion[int(5.2 / DT):int(6.8 / DT)])
    assert not any(in_motion[:int(4.9 / DT)])
    assert not any(in_motion[int(8 / DT):])


def test_motion_outside_detection_area_ignored():
    # The square crosses the middle band; exclude that band.
    cfg = MotionConfig(include=((0.0, 0.0, 1.0, 1.0),), exclude=((0.0, 0.3, 1.0, 0.7),))
    results = run(MotionDetector(cfg, SHAPE), moving_square_scene(SIZE, 5, 7), 10)
    assert not any(r.motion for r in results)


def test_lighting_change_is_not_motion():
    base = noise_scene(SIZE)

    def scene(t):
        f = base(t).astype(int)
        return np.clip(f + (60 if t > 5 else 0), 0, 255).astype(np.uint8)

    results = run(MotionDetector(MotionConfig(), SHAPE), scene, 8)
    assert any(r.lighting_change for r in results)
    assert not any(r.motion for r in results)


def test_isolated_specks_filtered():
    base = noise_scene(SIZE)
    rng = np.random.default_rng(7)

    def scene(t):
        f = base(t)
        ys, xs = rng.integers(0, SHAPE[0], 200), rng.integers(0, SHAPE[1], 200)
        f[ys, xs] = 255          # scattered single-pixel hits, like rain or hot pixels
        return f

    results = run(MotionDetector(MotionConfig(), SHAPE), scene, 10)
    assert not any(r.motion for r in results)
