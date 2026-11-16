#!/usr/bin/env python3
"""Live aiming view: serves a self-refreshing camera image over HTTP.

Run on the Pi, then open the printed URL on a phone while adjusting the
camera, light and focus:
    python3 tools/aim.py                    # auto exposure, capped at 15 fps like the app
    python3 tools/aim.py --exposure 66      # fixed 66 ms at --gain (default 8)
    python3 tools/aim.py --port 8080

Ctrl-C to stop. Holds the camera, so stop it before running camera_test.py.
Standalone, like camera_test.py; not part of the app's hardware layer.
"""

import argparse
import io
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
from picamera2 import Picamera2

SENSOR_MODE = (1640, 1232)   # 2x2 binned, full field of view (same as the app plan)
VIEW_SIZE = (1024, 768)
MAX_FRAME_US = 66_667        # 15 fps cap

PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>zero aim</title>
<style>
  body { margin: 0; background: #000; color: #ddd; font: 14px system-ui, sans-serif; }
  .wrap { position: relative; }
  img { display: block; width: 100%; height: auto; }
  .h, .v { position: absolute; background: rgba(0,255,0,.5); }
  .h { left: 0; right: 0; top: 50%; height: 1px; }
  .v { top: 0; bottom: 0; left: 50%; width: 1px; }
  #stats { padding: 8px 10px; font-variant-numeric: tabular-nums; }
  .bad { color: #f66; }
</style></head><body>
<div class="wrap"><img id="img" alt="camera"><div class="h"></div><div class="v"></div></div>
<div id="stats">connecting...</div>
<script>
const img = document.getElementById('img'), st = document.getElementById('stats');
let busy = false;
async function tick() {
  if (busy) return;
  busy = true;
  try {
    const r = await fetch('/stats.json', {cache: 'no-store'});
    const s = await r.json();
    await new Promise((ok, err) => { img.onload = ok; img.onerror = err;
                                     img.src = '/latest.jpg?n=' + s.frame; });
    const clip = s.clipped_pct > 1 ? ' class="bad"' : '';
    st.innerHTML = `exp ${s.exposure_ms} ms &middot; gain ${s.gain} &middot; ` +
      `mean ${s.mean} (aim 60-120) &middot; <span${clip}>clip ${s.clipped_pct}%</span> &middot; ` +
      `sharp ${s.sharpness} &middot; frame ${s.frame}`;
  } catch (e) {
    st.innerHTML = '<span class="bad">lost connection, retrying</span>';
  }
  busy = false;
}
setInterval(tick, 700);
tick();
</script></body></html>
"""


class Latest:
    """Most recent JPEG and its stats, shared between capture and HTTP threads."""

    def __init__(self):
        self.lock = threading.Lock()
        self.jpeg = b""
        self.stats = {"frame": 0}

    def set(self, jpeg: bytes, stats: dict):
        with self.lock:
            self.jpeg, self.stats = jpeg, stats

    def get(self):
        with self.lock:
            return self.jpeg, self.stats


def sharpness(gray: np.ndarray) -> float:
    """Variance of a 4-neighbour Laplacian over the centre third (same as camera_test.py)."""
    h, w = gray.shape
    c = gray[h // 3: 2 * h // 3, w // 3: 2 * w // 3].astype(np.float32)
    lap = c[1:-1, :-2] + c[1:-1, 2:] + c[:-2, 1:-1] + c[2:, 1:-1] - 4 * c[1:-1, 1:-1]
    return float(lap.var())


def capture_loop(picam2: Picamera2, latest: Latest, interval: float, stop: threading.Event):
    frame = 0
    while not stop.is_set():
        t0 = time.monotonic()
        req = picam2.capture_request()
        try:
            image = req.make_image("main")
            md = req.get_metadata()
        finally:
            req.release()
        gray = np.asarray(image.convert("L"))
        buf = io.BytesIO()
        image.save(buf, "JPEG", quality=80)
        frame += 1
        latest.set(buf.getvalue(), {
            "frame": frame,
            "exposure_ms": round(md.get("ExposureTime", 0) / 1000, 1),
            "gain": round(md.get("AnalogueGain", 0), 2),
            "mean": round(float(gray.mean()), 1),
            "clipped_pct": round(float((gray >= 250).mean() * 100), 2),
            "sharpness": round(sharpness(gray), 1),
        })
        stop.wait(max(0.0, interval - (time.monotonic() - t0)))


def make_handler(latest: Latest):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            jpeg, stats = latest.get()
            if path == "/":
                self.send(200, "text/html; charset=utf-8", PAGE.encode())
            elif path == "/latest.jpg" and jpeg:
                self.send(200, "image/jpeg", jpeg)
            elif path == "/stats.json":
                self.send(200, "application/json", json.dumps(stats).encode())
            else:
                self.send(404, "text/plain", b"not found")

        def send(self, code: int, ctype: str, body: bytes):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # keep the console for the URLs
            pass

    return Handler


def urls(port: int) -> list[str]:
    found = []
    for cmd in (["tailscale", "ip", "-4"], ["hostname", "-I"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        found += [ip for ip in out.split() if ip.count(".") == 3 and ip not in found]
    return [f"http://{ip}:{port}/" for ip in found]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--interval", type=float, default=0.7, help="seconds between frames")
    ap.add_argument("--exposure", type=float, metavar="MS", help="fixed exposure; default is auto")
    ap.add_argument("--gain", type=float, default=8.0, help="analogue gain with --exposure")
    args = ap.parse_args()

    picam2 = Picamera2()
    if args.exposure:
        us = int(args.exposure * 1000)
        controls = {"AeEnable": False, "ExposureTime": us, "AnalogueGain": args.gain,
                    "FrameDurationLimits": (us, max(us, MAX_FRAME_US))}
    else:
        controls = {"FrameDurationLimits": (33_333, MAX_FRAME_US)}
    controls["Saturation"] = 0.0  # NoIR colours are meaningless; greyscale like night footage
    picam2.configure(picam2.create_video_configuration(
        main={"size": VIEW_SIZE, "format": "RGB888"}, raw={"size": SENSOR_MODE},
        controls=controls, buffer_count=3))
    picam2.start()

    latest = Latest()
    stop = threading.Event()
    threading.Thread(target=capture_loop, args=(picam2, latest, args.interval, stop),
                     daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(latest))
    print("Aim view running. Open one of these on your phone:")
    for u in urls(args.port):
        print(f"  {u}")
    print(f"  http://zero:{args.port}/   (Tailscale MagicDNS)")
    print("Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        picam2.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
