#!/usr/bin/env python3
"""One-shot camera bring-up test for the Pi Zero 2 W + Camera Module 2 NoIR.

Run on the Pi:
    python3 tools/camera_test.py                 # auto still + night bracket + lores frame
    python3 tools/camera_test.py --video 10      # ...plus a 10 s test clip at planned settings
    python3 tools/camera_test.py --focus 30      # live sharpness readout while turning the lens

Writes everything to ~/stills/<timestamp>/ : JPEGs, a JSON of metadata per
image, and summary.json. Pull it back with:
    scp -r zero:stills/<timestamp> .

Standalone on purpose: it talks to picamera2 directly and is not part of the
app's hardware layer.
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from picamera2 import Picamera2

# Planned video settings (2x2 binned sensor mode keeps full FOV and is better in low light).
SENSOR_MODE = (1640, 1232)
MAIN_SIZE = (1280, 960)
LORES_SIZE = (320, 240)
BITRATE = 3_000_000

DEFAULT_EXPOSURES_MS = [33, 66, 100, 250, 500]
DEFAULT_GAIN = 8.0

META_KEYS = ("ExposureTime", "AnalogueGain", "DigitalGain", "Lux", "FrameDuration",
             "SensorTemperature", "ColourTemperature")


def sharpness(gray: np.ndarray) -> float:
    """Variance of a 4-neighbour Laplacian over the centre third. Higher = sharper.
    Only meaningful when comparing frames of the same scene and exposure."""
    h, w = gray.shape
    c = gray[h // 3: 2 * h // 3, w // 3: 2 * w // 3].astype(np.float32)
    lap = (c[1:-1, :-2] + c[1:-1, 2:] + c[:-2, 1:-1] + c[2:, 1:-1] - 4 * c[1:-1, 1:-1])
    return float(lap.var())


def brightness(gray: np.ndarray) -> dict:
    return {"mean": round(float(gray.mean()), 1),
            "p99": int(np.percentile(gray, 99)),
            "clipped_pct": round(float((gray >= 250).mean() * 100), 2)}


def pick_meta(md: dict) -> dict:
    return {k: md[k] for k in META_KEYS if k in md}


def throttled() -> str:
    try:
        return subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                              text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "vcgencmd unavailable"


def capture(picam2: Picamera2, out: Path, name: str, target_us: int | None = None) -> dict:
    """Capture one main-stream frame, save JPEG + metadata. If target_us is set,
    wait until the sensor reports that exposure (manual changes take a few frames)."""
    for attempt in range(15):
        req = picam2.capture_request()
        md = req.get_metadata()
        if (target_us is None or attempt == 14
                or abs(md.get("ExposureTime", 0) - target_us) <= target_us * 0.1):
            break
        req.release()
    try:
        path = out / f"{name}.jpg"
        req.save("main", str(path))
        rgb = req.make_array("main")
    finally:
        req.release()
    gray = rgb.mean(axis=2)
    info = {"file": path.name, "metadata": pick_meta(md), "brightness": brightness(gray),
            "sharpness": round(sharpness(gray), 1)}
    (out / f"{name}.json").write_text(json.dumps(info, indent=2))
    m = info["metadata"]
    print(f"  {path.name:28s} exp={m.get('ExposureTime', 0) / 1000:7.1f} ms  "
          f"gain={m.get('AnalogueGain', 0):5.2f}  mean={info['brightness']['mean']:5.1f}  "
          f"clip={info['brightness']['clipped_pct']:5.2f}%  sharp={info['sharpness']:8.1f}")
    return info


def stills(picam2: Picamera2, out: Path, exposures_ms: list[int], gain: float) -> list[dict]:
    picam2.configure(picam2.create_still_configuration(main={"format": "RGB888"}))
    picam2.start()
    results = []
    print("Full-res auto exposure:")
    time.sleep(2)  # let AE/AWB converge
    results.append(capture(picam2, out, "auto_full"))

    print(f"Full-res manual bracket (gain {gain}):")
    for ms in exposures_ms:
        us = ms * 1000
        picam2.set_controls({"AeEnable": False, "AnalogueGain": gain, "ExposureTime": us,
                             "FrameDurationLimits": (us, us + 20_000)})
        results.append(capture(picam2, out, f"manual_{ms:04d}ms_g{gain:g}", target_us=us))
    picam2.stop()
    return results


def video_config(picam2: Picamera2, fps: float):
    return picam2.create_video_configuration(
        main={"size": MAIN_SIZE, "format": "YUV420"},
        lores={"size": LORES_SIZE, "format": "YUV420"},
        raw={"size": SENSOR_MODE},
        controls={"FrameRate": fps},
    )


def lores_frame(picam2: Picamera2, out: Path, fps: float) -> dict:
    from PIL import Image
    picam2.configure(video_config(picam2, fps))
    picam2.start()
    time.sleep(2)
    y = picam2.capture_array("lores")[: LORES_SIZE[1], : LORES_SIZE[0]]  # Y plane
    picam2.stop()
    Image.fromarray(y, "L").save(out / "lores_y.jpg")
    info = {"file": "lores_y.jpg", "size": LORES_SIZE, "brightness": brightness(y)}
    print(f"Lores Y frame: {LORES_SIZE[0]}x{LORES_SIZE[1]} mean={info['brightness']['mean']}")
    return info


def test_video(picam2: Picamera2, out: Path, seconds: int, fps: float) -> dict:
    from picamera2.encoders import H264Encoder
    try:
        from picamera2.outputs import PyavOutput
        path = out / "test.mp4"
        output = PyavOutput(str(path))
    except ImportError:
        from picamera2.outputs import FileOutput
        path = out / "test.h264"
        output = FileOutput(str(path))

    frames = []
    picam2.configure(video_config(picam2, fps))
    picam2.post_callback = lambda req: frames.append(req.get_metadata().get("SensorTimestamp", 0))
    picam2.start_recording(H264Encoder(bitrate=BITRATE), output)
    time.sleep(seconds)
    picam2.stop_recording()
    picam2.post_callback = None

    gaps = np.diff(frames) / 1e6 if len(frames) > 1 else np.array([0.0])  # ms
    span = (frames[-1] - frames[0]) / 1e9 if len(frames) > 1 else 0
    info = {"file": path.name, "requested_fps": fps,
            "achieved_fps": round((len(frames) - 1) / span, 2) if span else 0,
            "frames": len(frames), "max_gap_ms": round(float(gaps.max()), 1),
            "size_mb": round(path.stat().st_size / 1e6, 2),
            "mb_per_min": round(path.stat().st_size / 1e6 / seconds * 60, 1)}
    print(f"Video: {path.name} {info['frames']} frames, {info['achieved_fps']} fps achieved "
          f"(asked {fps}), max gap {info['max_gap_ms']} ms, {info['mb_per_min']} MB/min")
    return info


def focus_loop(picam2: Picamera2, seconds: int):
    """Print a sharpness score twice a second; turn the lens for the highest number."""
    picam2.configure(video_config(picam2, 15))
    picam2.start()
    time.sleep(1)
    best = 0.0
    end = time.monotonic() + seconds
    print("Turn the lens slowly; Ctrl-C to stop. Keep the scene and lighting fixed.")
    try:
        while time.monotonic() < end:
            y = picam2.capture_array("main")[: MAIN_SIZE[1], : MAIN_SIZE[0]]
            s = sharpness(y)
            best = max(best, s)
            print(f"\r  sharpness {s:9.1f}   best {best:9.1f}  ", end="", flush=True)
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    print()
    picam2.stop()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=Path.home() / "stills")
    ap.add_argument("--exposures", type=int, nargs="+", default=DEFAULT_EXPOSURES_MS, metavar="MS")
    ap.add_argument("--gain", type=float, default=DEFAULT_GAIN)
    ap.add_argument("--fps", type=float, default=15)
    ap.add_argument("--video", type=int, default=0, metavar="SECONDS", help="also record a test clip")
    ap.add_argument("--focus", type=int, default=0, metavar="SECONDS",
                    help="only run the live sharpness readout")
    args = ap.parse_args()

    picam2 = Picamera2()
    print(f"Camera: {picam2.camera_properties.get('Model')}  "
          f"pixel array {picam2.camera_properties.get('PixelArraySize')}")
    print("Sensor modes:")
    for m in picam2.sensor_modes:
        print(f"  {m['size']}  {m['fps']:.1f} fps  {m['format']}")
    print(f"Throttled: {throttled()}  (0x0 = power OK)")

    if args.focus:
        focus_loop(picam2, args.focus)
        return 0

    out = args.out / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    summary = {"time": datetime.now().isoformat(timespec="seconds"),
               "camera": {k: str(v) for k, v in picam2.camera_properties.items()},
               "stills": stills(picam2, out, args.exposures, args.gain),
               "lores": lores_frame(picam2, out, args.fps)}
    if args.video:
        summary["video"] = test_video(picam2, out, args.video, args.fps)
    summary["throttled_after"] = throttled()
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    print(f"Throttled after: {summary['throttled_after']}")
    print(f"Saved to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
