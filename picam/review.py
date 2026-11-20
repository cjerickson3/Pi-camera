"""Clip review page: nights -> events -> clip, with star and note.

    python3 -m picam.review                 # 127.0.0.1:8080 from config.toml [review]
    python3 -m picam.review --bind 0.0.0.0  # e.g. testing on the LAN
    python -m picam.review --config x.toml  # on the PC, against a copied data dir

Standard library only (no Flask on the Zero). Listens on localhost; `tailscale serve`
publishes it on the tailnet. Stars and notes are written to the same events.db that
picam writes; both wait on each other's locks (events.BUSY_TIMEOUT_S).

JSON for tools/fetch_starred.py: GET /api/starred, GET /api/events?night=YYYY-MM-DD.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import shutil
import sqlite3
from contextlib import closing
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Sequence
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .config import DEFAULT_PATH, Config, load as load_config
from .events import connect

log = logging.getLogger("picam.review")

# Night window hours, evening first: 17:00 through 08:59.
HOURS = [*range(17, 24), *range(0, 9)]
CHUNK = 256 * 1024
TYPES = {".mp4": "video/mp4", ".jpg": "image/jpeg", ".csv": "text/csv; charset=utf-8"}

CSS = """
:root { color-scheme: dark; --bg:#111; --panel:#1c1c1c; --line:#333; --fg:#ddd; --dim:#888;
        --accent:#7fd18b; --star:#f4c542; }
* { box-sizing: border-box; }
body { margin:0; padding:12px 16px 40px; background:var(--bg); color:var(--fg);
       font:15px/1.4 system-ui, sans-serif; max-width:1200px; margin:0 auto; }
a { color:var(--accent); text-decoration:none; }
h1 { font-size:20px; margin:4px 0 12px; } h1 a { color:var(--fg); }
.dim { color:var(--dim); }
table { border-collapse:collapse; width:100%; }
td, th { padding:6px 8px; border-bottom:1px solid var(--line); text-align:left; vertical-align:middle; }
th { color:var(--dim); font-weight:normal; font-size:13px; }
.num { text-align:right; font-variant-numeric:tabular-nums; }
.hours { display:flex; gap:2px; align-items:flex-end; height:28px; min-width:160px; }
.hours.big { height:70px; margin:8px 0 2px; }
.hours div { flex:1; background:var(--accent); min-height:1px; opacity:.85; }
.hours div.zero { background:var(--line); }
.hlabels { display:flex; gap:2px; font-size:11px; color:var(--dim); margin-bottom:14px; }
.hlabels span { flex:1; text-align:center; }
.grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(220px, 1fr)); gap:10px; }
.card { background:var(--panel); border-radius:6px; overflow:hidden; color:var(--fg); display:block; }
.card img, .noimg { width:100%; aspect-ratio:4/3; object-fit:cover; display:block; background:#000; }
.noimg { display:flex; align-items:center; justify-content:center; color:var(--dim); }
.card .meta { padding:6px 8px; font-size:13px; display:flex; justify-content:space-between; }
.star { color:var(--star); }
video, .still { width:100%; max-height:72vh; object-fit:contain; background:#000; display:block; }
.bar { display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin:10px 0; }
button { font:inherit; padding:6px 14px; border-radius:6px; border:1px solid var(--line);
         background:var(--panel); color:var(--fg); cursor:pointer; }
button.on { border-color:var(--star); color:var(--star); }
textarea { width:100%; max-width:640px; font:inherit; background:var(--panel); color:var(--fg);
           border:1px solid var(--line); border-radius:6px; padding:6px; }
.details td:first-child { color:var(--dim); width:9em; }
"""


def _page(title: str, body: str) -> bytes:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{html.escape(title)}</title><style>{CSS}</style></head>'
            f'<body>{body}</body></html>').encode()


def _hours(rows, big=False) -> str:
    counts = {h: 0 for h in HOURS}
    for r in rows:
        h = int(r["started_at"][11:13])
        if h in counts:
            counts[h] += 1
    top = max(counts.values()) or 1
    bars = "".join(
        f'<div class="{"zero" if n == 0 else ""}" style="height:{max(4, 100 * n // top) if n else 4}%"'
        f' title="{h:02d}:00 - {n} event{"s" if n != 1 else ""}"></div>' for h, n in counts.items())
    out = f'<div class="hours{" big" if big else ""}">{bars}</div>'
    if big:
        out += '<div class="hlabels">' + "".join(
            f"<span>{h if h % 3 == 0 else ''}</span>" for h in HOURS) + "</div>"
    return out


def _star_count(n: int) -> str:
    return f'<span class="star">&#9733; {n}</span>' if n else ""


def _clock(iso: str) -> str:
    return iso[11:19]


def _row_dict(r: sqlite3.Row) -> dict:
    return {k: r[k] for k in r.keys()}


class Review:
    """Queries and updates; one short-lived connection per request (threads)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.clips_dir = cfg.paths.clips_dir
        self.db_path = cfg.paths.db_path

    def db(self):
        return closing(connect(self.db_path))

    def query(self, sql: str, args=()) -> list[sqlite3.Row]:
        if not self.db_path.exists():
            return []
        with self.db() as db:
            try:
                return db.execute(sql, args).fetchall()
            except sqlite3.OperationalError as e:
                if "no such table" in str(e):       # picam hasn't created the schema yet
                    return []
                raise

    def event(self, event_id: int) -> sqlite3.Row | None:
        rows = self.query("SELECT * FROM events WHERE id = ?", (event_id,))
        return rows[0] if rows else None

    def update(self, event_id: int, **cols):
        with self.db() as db, db:
            for col, val in cols.items():
                assert col in ("starred", "note")
                db.execute(f"UPDATE events SET {col} = ? WHERE id = ?", (val, event_id))

    def clip_path(self, rel: str) -> Path | None:
        root = self.clips_dir.resolve()
        p = (root / rel).resolve()
        return p if p.is_relative_to(root) and p.is_file() else None

    # Pages

    def index(self) -> bytes:
        nights = self.query(
            """SELECT night, COUNT(*) AS n, SUM(starred) AS starred, SUM(clip IS NULL) AS pruned,
                      MIN(started_at) AS first, MAX(started_at) AS last
               FROM events GROUP BY night ORDER BY night DESC""")
        by_night: dict[str, list] = {}
        for r in self.query("SELECT night, started_at FROM events"):
            by_night.setdefault(r["night"], []).append(r)
        rows = "".join(
            f'<tr><td><a href="/night/{n["night"]}">{n["night"]}</a></td>'
            f'<td class="num">{n["n"]}</td>'
            f'<td class="num">{_star_count(n["starred"])}</td>'
            f'<td class="dim">{_clock(n["first"])[:5]}&ndash;{_clock(n["last"])[:5]}</td>'
            f'<td>{_hours(by_night[n["night"]])}</td>'
            f'<td class="dim">{"pruned " + str(n["pruned"]) if n["pruned"] else ""}</td></tr>'
            for n in nights)
        total = sum(n["n"] for n in nights)
        free = ""
        if self.clips_dir.exists():
            du = shutil.disk_usage(self.clips_dir)
            free = f" &middot; {du.free / 1024**3:.0f} GB free of {du.total / 1024**3:.0f}"
        body = (f'<h1>Burrow camera</h1><p class="dim">{total} events, {len(nights)} nights{free}'
                f' &middot; <a href="/starred">starred</a></p>')
        if nights:
            body += ('<table><tr><th>Night</th><th class="num">Events</th><th></th><th>First&ndash;last</th>'
                     '<th>17:00 &rarr; 08:00</th><th></th></tr>' + rows + "</table>")
        else:
            body += "<p>No events yet.</p>"
        return _page("Burrow camera", body)

    def _grid(self, events) -> str:
        cards = []
        for e in events:
            img = (f'<img loading="lazy" src="/clips/{quote(e["snapshot"])}" alt="">' if e["snapshot"]
                   else f'<div class="noimg">{"pruned" if e["clip"] is None else "no snapshot"}</div>')
            star = '<span class="star">&#9733;</span> ' if e["starred"] else ""
            note = ' <span class="dim">&#9998;</span>' if e["note"] else ""
            cards.append(
                f'<a class="card" href="/event/{e["id"]}">{img}<div class="meta">'
                f'<span>{star}{_clock(e["started_at"])}{note}</span>'
                f'<span class="dim">{e["duration_s"]:.0f} s &middot; {e["peak_score"]:.3f}</span>'
                f'</div></a>')
        return f'<div class="grid">{"".join(cards)}</div>'

    def night(self, night: str, starred_only: bool) -> bytes | None:
        events = self.query("SELECT * FROM events WHERE night = ? ORDER BY started_at", (night,))
        if not events:
            return None
        shown = [e for e in events if e["starred"]] if starred_only else events
        toggle = (f'<a href="/night/{night}">show all</a>' if starred_only
                  else f'<a href="/night/{night}?starred=1">starred only</a>')
        scores = (self.cfg.paths.scores_dir / f"{night}.csv")
        csv = f' &middot; <a href="/scores/{night}.csv">scores CSV</a>' if scores.exists() else ""
        body = (f'<h1><a href="/">&larr;</a> Night of {night}</h1>'
                f'<p class="dim">{len(events)} events &middot; {toggle}{csv}</p>'
                f'{_hours(events, big=True)}{self._grid(shown)}')
        return _page(f"Night of {night}", body)

    def starred(self) -> bytes:
        events = self.query("SELECT * FROM events WHERE starred = 1 ORDER BY started_at")
        body = (f'<h1><a href="/">&larr;</a> Starred</h1><p class="dim">{len(events)} events</p>'
                + self._grid(events))
        return _page("Starred", body)

    def event_page(self, event_id: int) -> bytes | None:
        e = self.event(event_id)
        if e is None:
            return None
        sib = self.query("SELECT id FROM events WHERE night = ? ORDER BY started_at", (e["night"],))
        ids = [r["id"] for r in sib]
        i = ids.index(e["id"])
        prev = f'<a href="/event/{ids[i - 1]}">&larr; prev</a>' if i > 0 else '<span class="dim">&larr; prev</span>'
        nxt = (f'<a href="/event/{ids[i + 1]}">next &rarr;</a>' if i + 1 < len(ids)
               else '<span class="dim">next &rarr;</span>')

        poster = f' poster="/clips/{quote(e["snapshot"])}"' if e["snapshot"] else ""
        if e["clip"] and self.clip_path(e["clip"]):
            media = (f'<video controls playsinline preload="metadata"{poster} '
                     f'src="/clips/{quote(e["clip"])}"></video>')
            download = f'<a href="/clips/{quote(e["clip"])}" download>download MP4</a>'
        elif e["snapshot"]:
            media = f'<img class="still" src="/clips/{quote(e["snapshot"])}" alt="">'
            download = '<span class="dim">clip pruned</span>'
        else:
            media = '<div class="noimg">clip pruned</div>'
            download = ""

        star_btn = (f'<form method="post" style="display:inline">'
                    f'<input type="hidden" name="starred" value="{0 if e["starred"] else 1}">'
                    f'<button class="{"on" if e["starred"] else ""}">'
                    f'{"&#9733; Starred" if e["starred"] else "&#9734; Star"}</button></form>')
        details = [
            ("Started", e["started_at"].replace("T", " ")),
            ("Motion", f'{e["duration_s"]:.1f} s, {e["motion_frames"]} frames'),
            ("Score", f'peak {e["peak_score"]:.4f}, mean {e["mean_score"]:.4f}'),
            ("Trigger", e["trigger"]),
            ("Exposure", f'{e["exposure_ms"]} ms, gain {e["gain"]}' if e["exposure_ms"] else "-"),
            ("File", e["clip"] or "-"),
        ]
        rows = "".join(f"<tr><td>{k}</td><td>{html.escape(str(v))}</td></tr>" for k, v in details)
        body = (f'<h1><a href="/night/{e["night"]}">&larr;</a> {_clock(e["started_at"])} '
                f'<span class="dim">night of {e["night"]}</span></h1>'
                f'<div class="bar">{prev} {nxt}</div>{media}'
                f'<div class="bar">{star_btn} {download}</div>'
                f'<form method="post"><textarea name="note" rows="2" placeholder="Note (what is it?)">'
                f'{html.escape(e["note"] or "")}</textarea><br><button>Save note</button></form>'
                f'<table class="details" style="margin-top:14px">{rows}</table>')
        return _page(f"Event {_clock(e['started_at'])}", body)

    def api_events(self, night: str | None, starred_only: bool) -> bytes:
        sql, args = "SELECT * FROM events", []
        where = []
        if night:
            where.append("night = ?")
            args.append(night)
        if starred_only:
            where.append("starred = 1")
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = self.query(sql + " ORDER BY started_at", args)
        return json.dumps([_row_dict(r) for r in rows], indent=1).encode()


NIGHT_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def make_handler(review: Review):
    class Handler(BaseHTTPRequestHandler):
        server_version = "picam-review"

        def do_GET(self):
            self._route(head=False)

        def do_HEAD(self):
            self._route(head=True)

        def _route(self, head: bool):
            url = urlsplit(self.path)
            path, q = unquote(url.path), parse_qs(url.query)
            starred_only = q.get("starred", ["0"])[0] == "1"
            try:
                if path == "/":
                    return self._send(200, "text/html; charset=utf-8", review.index(), head)
                if path == "/starred":
                    return self._send(200, "text/html; charset=utf-8", review.starred(), head)
                if m := re.fullmatch(r"/night/(\d{4}-\d{2}-\d{2})", path):
                    return self._send_page(review.night(m[1], starred_only), head)
                if m := re.fullmatch(r"/event/(\d+)", path):
                    return self._send_page(review.event_page(int(m[1])), head)
                if path.startswith("/clips/"):
                    return self._send_file(review.clip_path(path[len("/clips/"):]), head)
                if m := re.fullmatch(r"/scores/(\d{4}-\d{2}-\d{2})\.csv", path):
                    p = review.cfg.paths.scores_dir / f"{m[1]}.csv"
                    return self._send_file(p if p.is_file() else None, head)
                if path == "/api/starred":
                    return self._send(200, "application/json", review.api_events(None, True), head)
                if path == "/api/events":
                    night = q.get("night", [None])[0]
                    if night and not NIGHT_RE.fullmatch(night):
                        return self._error(400, "bad night")
                    return self._send(200, "application/json", review.api_events(night, starred_only), head)
                self._error(404)
            except (BrokenPipeError, ConnectionResetError):
                pass                            # browsers drop video requests all the time
            except Exception:
                log.exception("GET %s", self.path)
                self._error(500)

        def do_POST(self):
            m = re.fullmatch(r"/event/(\d+)", urlsplit(self.path).path)
            if not m:
                return self._error(404)
            event_id = int(m[1])
            if review.event(event_id) is None:
                return self._error(404)
            n = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(min(n, 64 * 1024)).decode("utf-8", "replace"),
                            keep_blank_values=True)
            cols = {}
            if "starred" in form:
                cols["starred"] = 1 if form["starred"][0] == "1" else 0
            if "note" in form:
                cols["note"] = form["note"][0].strip() or None
            if not cols:
                return self._error(400, "nothing to change")
            try:
                review.update(event_id, **cols)
            except Exception:
                log.exception("POST %s", self.path)
                return self._error(500)
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", f"/event/{event_id}")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _send_page(self, body: bytes | None, head: bool):
            if body is None:
                return self._error(404)
            self._send(200, "text/html; charset=utf-8", body, head)

        def _send(self, code: int, ctype: str, body: bytes, head: bool = False):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def _error(self, code: int, msg: str = ""):
            body = _page(str(code), f"<h1>{code} {html.escape(msg or HTTPStatus(code).phrase)}</h1>"
                                    f'<p><a href="/">home</a></p>')
            self._send(code, "text/html; charset=utf-8", body)

        def _send_file(self, p: Path | None, head: bool):
            """Static file with single-range support (video seeking needs it)."""
            if p is None:
                return self._error(404)
            size = p.stat().st_size
            start, end = 0, size - 1
            rng = self.headers.get("Range")
            status = 200
            if rng:
                m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
                if not m or (not m[1] and not m[2]):
                    return self._range_error(size)
                if m[1]:
                    start = int(m[1])
                    end = min(int(m[2]), size - 1) if m[2] else size - 1
                else:                           # suffix: last N bytes
                    start = max(0, size - int(m[2]))
                if start >= size or start > end:
                    return self._range_error(size)
                status = 206
            length = end - start + 1
            self.send_response(status)
            self.send_header("Content-Type", TYPES.get(p.suffix.lower(), "application/octet-stream"))
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "private, max-age=3600")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if head:
                return
            with open(p, "rb") as fh:
                fh.seek(start)
                left = length
                while left > 0:
                    chunk = fh.read(min(CHUNK, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)

        def _range_error(self, size: int):
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

    return Handler


def make_server(cfg: Config, bind: str | None = None, port: int | None = None) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((bind or cfg.review.bind, cfg.review.port if port is None else port),
                                 make_handler(Review(cfg)))
    server.daemon_threads = True
    return server


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="picam.review", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=DEFAULT_PATH)
    ap.add_argument("--bind", help="address to listen on (default from config)")
    ap.add_argument("--port", type=int, help="port (default from config)")
    ap.add_argument("-v", "--verbose", action="store_true", help="log every request")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    server = make_server(cfg, args.bind, args.port)
    host, port = server.server_address[:2]
    log.info("review page on http://%s:%d/ (data in %s)", host, port, cfg.paths.root)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
