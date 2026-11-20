"""Review page against a live server on a random port, with a temp data dir."""

import json
import threading
import urllib.error
import urllib.request
from dataclasses import replace
from urllib.parse import urlencode

import pytest

from picam.config import Config, PathsConfig
from picam.events import EventLog
from picam.review import make_server
from test_prune import add_night


@pytest.fixture
def site(tmp_path):
    cfg = replace(Config(), paths=PathsConfig(data_dir=str(tmp_path)))
    events = EventLog(cfg.paths.db_path)
    ids = add_night(cfg.paths.clips_dir, events, "2026-11-16", n=3, size=5000)
    (cfg.paths.clips_dir / "2026-11-16" / "20261116-230000.mp4").write_bytes(bytes(range(256)) * 20)
    cfg.paths.scores_dir.mkdir()
    (cfg.paths.scores_dir / "2026-11-16.csv").write_text("time,frames\n")
    server = make_server(cfg, "127.0.0.1", 0)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base, ids, events, cfg
    server.shutdown()
    server.server_close()
    events.close()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def get(url, headers=None):
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {})) as r:
        return r.status, dict(r.headers), r.read()


def post(url, data):
    opener = urllib.request.build_opener(NoRedirect)
    try:
        opener.open(url, urlencode(data).encode())
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Location")
    raise AssertionError("expected a redirect")


def test_pages(site):
    base, ids, _, _ = site
    status, _, body = get(base + "/")
    assert status == 200 and b"2026-11-16" in body and b"3 events" in body
    _, _, body = get(base + "/night/2026-11-16")
    assert body.count(b'class="card"') == 3 and b"scores CSV" in body
    _, _, body = get(base + f"/event/{ids[0]}")
    assert b"<video" in body and b"20261116-230000.mp4" in body and b"next &rarr;</a>" in body


def test_missing_things_404(site):
    base, *_ = site
    for path in ("/night/2026-01-01", "/event/999", "/clips/2026-11-16/nope.mp4",
                 "/clips/../events.db", "/clips/%2e%2e/events.db", "/nope"):
        with pytest.raises(urllib.error.HTTPError) as e:
            get(base + path)
        assert e.value.code == 404, path


def test_range_requests(site):
    base, *_ = site
    url = base + "/clips/2026-11-16/20261116-230000.mp4"
    data = bytes(range(256)) * 20
    status, h, body = get(url)
    assert status == 200 and body == data and h["Accept-Ranges"] == "bytes"
    status, h, body = get(url, {"Range": "bytes=100-199"})
    assert status == 206 and body == data[100:200] and h["Content-Range"] == f"bytes 100-199/{len(data)}"
    status, _, body = get(url, {"Range": "bytes=5000-"})
    assert status == 206 and body == data[5000:]
    status, _, body = get(url, {"Range": "bytes=-10"})
    assert status == 206 and body == data[-10:]
    with pytest.raises(urllib.error.HTTPError) as e:
        get(url, {"Range": f"bytes={len(data)}-"})
    assert e.value.code == 416


def test_star_and_note(site):
    base, ids, events, _ = site
    assert post(base + f"/event/{ids[1]}", {"starred": "1"}) == (303, f"/event/{ids[1]}")
    assert post(base + f"/event/{ids[1]}", {"note": "  rat, left hole  "})[0] == 303
    row = events.db.execute("SELECT starred, note FROM events WHERE id = ?", (ids[1],)).fetchone()
    assert tuple(row) == (1, "rat, left hole")

    starred = json.loads(get(base + "/api/starred")[2])
    assert [e["id"] for e in starred] == [ids[1]] and starred[0]["clip"].endswith(".mp4")
    _, _, body = get(base + "/starred")
    assert body.count(b'class="card"') == 1

    assert post(base + f"/event/{ids[1]}", {"starred": "0", "note": ""})[0] == 303
    assert json.loads(get(base + "/api/starred")[2]) == []
    assert events.db.execute("SELECT note FROM events WHERE id = ?", (ids[1],)).fetchone()[0] is None


def test_api_events_by_night(site):
    base, *_ = site
    assert len(json.loads(get(base + "/api/events?night=2026-11-16")[2])) == 3
    assert json.loads(get(base + "/api/events?night=2026-11-17")[2]) == []


def test_empty_data_dir(tmp_path):
    cfg = replace(Config(), paths=PathsConfig(data_dir=str(tmp_path / "none")))
    server = make_server(cfg, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        _, _, body = get(f"http://127.0.0.1:{server.server_address[1]}/")
        assert b"No events yet" in body
    finally:
        server.shutdown()
        server.server_close()
