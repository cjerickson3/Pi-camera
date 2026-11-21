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


def _longest_run(results):
    best = cur = 0
    for r in results:
        cur = cur + 1 if r.motion else 0
        best = max(best, cur)
    return best


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


def _lamp_scene(level):
    """Textured ground whose left half (the 'grass') is lit by `level(t)` luma; the right
    half (the dark sidewalk) stays put, so well under half the frame changes."""
    base = noise_scene(SIZE)

    def scene(t):
        f = base(t).astype(int)
        f[:, : SHAPE[1] // 2] += int(level(t))
        return np.clip(f, 0, 255).astype(np.uint8)

    return scene


def test_partial_lamp_flicker_is_lighting_not_motion():
    # A lamp lighting only the grass, switching unevenly (on/off/on) as it comes on.
    steps = [(4, 0), (4.5, 25), (5, 50), (5.5, 0), (6, 30), (6.5, 70), (7, 85)]
    level = lambda t: next((v for s, v in reversed(steps) if t >= s), 0)
    cfg = MotionConfig()
    results = run(MotionDetector(cfg, SHAPE), _lamp_scene(level), 12)
    assert any(r.lighting_change for r in results)
    assert not any(r.motion for r in results)


def test_exposure_ramp_at_start_is_not_motion():
    # 2026-11-19 18:22: the first frames come before AE settles (brightness 19 -> 25 in ~1 s).
    # The old detector kept the dark first frame and scored ~0.7 for 17 s.
    base = noise_scene(SIZE)
    scene = lambda t: np.clip(base(t) * min(1.0, 0.6 + 0.4 * t), 0, 255).astype(np.uint8)
    results = run(MotionDetector(MotionConfig(), SHAPE), scene, 20)
    assert not any(r.motion for r in results)


def test_lamp_fading_out_is_not_motion():
    # 2026-11-20 06:43: the illuminator went off at dawn, 96 -> 57 within a second. The old
    # detector relearned from a mid-fade frame and scored ~0.6 for 12 s afterwards.
    # A frame or two of motion can slip through before the mean has moved 15%; what matters
    # is that it never reaches trigger_frames in a row. (A fade much slower than this one
    # would still trigger; none has been seen.)
    level = lambda t: 60 if t < 5 else max(0.0, 60 - 100 * (t - 5))
    cfg = MotionConfig()
    results = run(MotionDetector(cfg, SHAPE), _lamp_scene(level), 15)
    assert any(r.lighting_change for r in results)
    assert _longest_run(results) < cfg.trigger_frames


def test_motion_detected_again_after_lighting_change():
    lamp = _lamp_scene(lambda t: 80 if t >= 3 else 0)
    square = moving_square_scene(SIZE, 6, 8)
    scene = lambda t: np.maximum(lamp(t), square(t))
    results = run(MotionDetector(MotionConfig(), SHAPE), scene, 10)
    assert any(r.lighting_change for r in results[: int(4 / DT)])
    assert not any(r.motion for r in results[: int(5.9 / DT)])
    assert any(r.motion for r in results[int(6.2 / DT):int(7.8 / DT)])


def test_close_animal_dimming_the_frame_is_still_motion():
    # 2026-11-20 06:14: the rabbit moved the frame mean 3.4% in one frame. That must stay
    # motion, not a lighting change that relearns it into the background.
    base = noise_scene(SIZE)

    def scene(t):
        f = base(t).astype(int)
        if t >= 5:
            x = int(min(t - 5, 1.0) * 100)
            f[60:180, x:x + 100] -= 25          # big dark body, ~16% of the frame
        return np.clip(f, 0, 255).astype(np.uint8)

    results = run(MotionDetector(MotionConfig(), SHAPE), scene, 8)
    assert not any(r.lighting_change for r in results)
    assert any(r.motion for r in results[int(5.2 / DT):int(6 / DT)])
