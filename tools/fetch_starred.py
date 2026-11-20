#!/usr/bin/env python3
"""Copy starred clips from the Pi's review page to the PC (runs on Windows, stdlib only).

    python tools/fetch_starred.py                      # http://zero/ -> archive/
    python tools/fetch_starred.py --url http://zero:8080/ --dest D:/burrow

For each starred event it writes archive/<night>/<clip>.mp4, the snapshot .jpg, and a
<clip>.json sidecar holding the event's database row (times, scores, note). Files
already present with the right size are skipped, so re-running only fetches new stars.
Unstarring on the Pi does not delete anything here.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urljoin

REPO_ROOT = Path(__file__).resolve().parents[1]


def fetch(url: str, dest: Path) -> int:
    """Download url to dest via a .part file; return bytes written (0 if up to date)."""
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=30) as r:
        size = int(r.headers["Content-Length"])
    if dest.exists() and dest.stat().st_size == size:
        return 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    with urllib.request.urlopen(url, timeout=60) as r, open(part, "wb") as fh:
        while chunk := r.read(256 * 1024):
            fh.write(chunk)
    if part.stat().st_size != size:
        part.unlink()
        raise IOError(f"{url}: got {part.stat().st_size if part.exists() else 0} of {size} bytes")
    part.replace(dest)
    return size


def safe_rel(rel: str) -> Path:
    p = PurePosixPath(rel)
    if p.is_absolute() or ".." in p.parts:
        raise ValueError(f"refusing path {rel!r}")
    return Path(*p.parts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://zero/", help="review page base URL (default %(default)s)")
    ap.add_argument("--dest", type=Path, default=REPO_ROOT / "archive", help="default: archive/ in the repo")
    args = ap.parse_args()
    base = args.url if args.url.endswith("/") else args.url + "/"

    with urllib.request.urlopen(urljoin(base, "api/starred"), timeout=30) as r:
        events = json.load(r)
    print(f"{len(events)} starred events on {base}")

    new_files = new_bytes = failed = 0
    for e in events:
        for key in ("clip", "snapshot"):
            rel = e.get(key)
            if not rel:
                if key == "clip":
                    print(f"  {e['started_at']}: clip was pruned before it was starred")
                continue
            try:
                n = fetch(urljoin(base, "clips/" + quote(rel)), args.dest / safe_rel(rel))
            except Exception as exc:
                print(f"  {rel}: FAILED ({exc})", file=sys.stderr)
                failed += 1
                continue
            if n:
                new_files += 1
                new_bytes += n
                print(f"  {rel}  {n / 1e6:.1f} MB")
        stem = e.get("clip") or e.get("snapshot")
        if stem:
            side = (args.dest / safe_rel(stem)).with_suffix(".json")
            side.parent.mkdir(parents=True, exist_ok=True)
            side.write_text(json.dumps(e, indent=2) + "\n", encoding="utf-8")

    print(f"{new_files} new files, {new_bytes / 1e6:.1f} MB -> {args.dest}"
          + (f"; {failed} failed" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
