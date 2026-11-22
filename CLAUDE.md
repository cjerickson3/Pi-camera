# Pi-camera

Nighttime wildlife observation camera built on a Raspberry Pi Zero 2 W.
Owner: Chris (GitHub: cjerickson3). Repo name: `Pi-camera`.

## Why this exists

Something is digging burrows at the house in Plano, TX: under the front porch
slab, beside the sidewalk, and along the foundation (three active holes).
Rats are suspected; a neighbor's bird feeder and corn are the likely food
source. A pest control company will be hired for trapping and exclusion.

The camera's job:

1. Confirm what animal is digging.
2. Show when it is active and which holes it uses.
3. Provide evidence to brief the pest control company.
4. Verify afterward that the fix worked (no more activity).

## Hardware

| Part | Notes |
|---|---|
| Raspberry Pi Zero 2 W, in a case | Quad-core, 512 MB RAM. Keep the software light. |
| 128 GB high-endurance microSD | Holds OS, clips, and the event database |
| TP-Link Tapo C120 (since 2026-11-21) | **The camera in use.** WiFi, own IR. On a pole where the old IR light stood: 1 ft from the hole horizontally, 2 ft up, looking straight down at it. RTSP: `stream1` 2560x1440 H.264 20 fps (~0.8 Mbps by day), `stream2` 640x360 H.264 20 fps; keyframes every ~2 s; audio pcm_alaw (not recorded). ONVIF on port 2020. Login in `.env`. On-screen clock overlay turned off (2026-11-21). Its own zones/motion alerts are not used. |
| Camera Module 2 NoIR (IMX219, 8 MP) | Retired 2026-11-21 (the Zero is moving out of the front yard). Still attached; code path kept (`[camera] source = "picam"`). No IR filter. Fixed focus. Pi Zero camera cable. |
| 12 V IR illuminator | **850 nm** (faint red glow visible on the LEDs). Small grey dome. Has its own 12 V supply and a built-in photocell (on at dusk). Not switched or controlled by the Pi. Must sit beside the camera aiming at the scene, never facing the lens. |
| Second IR illuminator (added 2026-11-19) | Separate unit, much brighter. On a stake up and left of the hole, in frame at top left. Raised the scene from frame mean ~11 to ~75 at the same 66.6 ms / gain 9.85. |
| 5 V USB power supply for the Pi | Official Raspberry Pi 2.5 A supply (replaced an earlier 2.2 A one). Still check `vcgencmd get_throttled` = `0x0` under load. |
| CanaKit USB microSD reader | For imaging the card from the PC |

Enclosure: bench setup first. Later, a weatherproof outdoor case with 120 V
access holding both power supplies, plus an acrylic housing for the Pi and
camera that Chris will laser-cut on his Glowforge. Note that standard acrylic
and glass pass near-IR, but IR light reflecting off a window in front of the
lens causes glare, so the lens and illuminator need separate openings or a
baffle between them.

No PIR sensor is installed. Motion detection is done in software from the
camera. Leave a clean hook for adding a PIR on a GPIO pin later.

## Development setup

- Project root: `Documents\Pi-camera` on Chris's Windows PC. Code is written
  here with Claude Code and managed in git / GitHub.
- GitHub repo: `cjerickson3/Pi-camera`, **public** (so the Pi can `git pull`
  over HTTPS with no deploy key). Never commit secrets, WiFi credentials, or
  clips. `.gitattributes` forces LF for everything that runs on the Pi.
- The Pi runs headless: Raspberry Pi OS Lite 64-bit, WiFi, SSH.
  Hostname `zero`, user `chris`, SSH key `~/.ssh/pi_key` (same key as
  Chris's other Pi, `homepi`). Card reader on the PC is drive F:.
- Remote access via **Tailscale** (SSH and the review page). Setup steps:
  `docs/pi-setup.md`.
- Deploy to the Pi with `git pull` over SSH (or rsync).
- Camera code cannot run on the PC. Keep hardware access (camera, GPIO)
  behind a thin layer so the rest can be tested on the PC with a fake camera.

## Technical conventions

- Python 3, using `picamera2`. On the Pi, install it with apt
  (`python3-picamera2`), not pip. If a venv is used, create it with
  `--system-site-packages` so it can see the apt package.
- Record H.264 with the Pi's hardware encoder. Use a low-resolution stream
  for motion detection and a circular buffer for pre-roll.
- Log events to SQLite (timestamp, duration, clip file, motion score).
- Run as a systemd service: start on boot, restart on failure.
- Prune old clips so the card never fills.
- Config (resolution, sensitivity, clip length, retention, active hours)
  lives in one file, not scattered through the code.
- Night footage is monochrome IR. Do not spend effort on color tuning.

## Decisions

- **Camera to burrow distance:** about 2.5 ft (0.76 m). That is a little
  closer than the lens's stock near limit (~1 m), so expect to adjust focus
  with `camera_test.py --focus`.
- **Recording window:** one hour after sunset until 30 min before sunrise
  (`stop_after_sunrise_min = -30`, set 2026-11-20: the illuminator switched off
  at 06:43 on 11-20 and dawn light is not IR footage), computed for
  Plano, TX (approx. 33.02 N, 96.70 W) with `python3-astral`. The illuminator
  has its own photocell, so the Pi does not detect night itself. Outside the
  window the camera is stopped entirely (no daytime clips).
- **Lighting vs motion (2026-11-20):** a frame is a lighting change (background
  relearned, no motion) if >50% of it changed or its mean brightness moved
  >`brightness_jump` (15%) from the background's. During warm-up the background
  copies each frame, and warm-up restarts while brightness moves >3%/frame. This
  removed the nightly 18:22 start trigger (AE settling) and the dawn lamp-off
  trigger. Do not add a per-frame brightness-rate rule after warm-up: the 11-20
  rabbit moved the frame mean 3.4% in one frame. Changes to the detector are
  checked by replaying real clips through old and new code on the Pi (PyAV is
  there, not on the PC). Replay of all 88 clips (2026-11-21): 79 unchanged (rabbit
  included), headlight sweeps and the 11-21 dawn lamp-off now lighting, but on
  the dim pre-illuminator nights (mean ~10) encoder flicker exceeded 3%/frame and
  kept warm-up restarting, blind for most of each clip. Fixed with absolute
  floors (`JUMP_FLOOR` 3 luma, `SETTLE_FLOOR` 1 luma in `motion.py`) and a cap
  of 4x `warmup_frames` on warm-up.
- **Clips:** 5 s pre-roll, 5 s post-roll, 60 s cap (cap may come down later).
  Trigger needs 3 consecutive motion frames.
- **Data location on the Pi:** `~/picam-data/` holds `clips/<night>/`
  (MP4 + JPEG snapshot per event; `<night>` is the evening's date),
  `events.db`, and `scores/<night>.csv` (per-second motion scores for tuning).
- **Scope:** one burrow watched (down from three): the hole **beside the
  sidewalk**. Zones per hole are no longer needed; a single optional ROI mask
  in config is enough. During setup a 12" ruler stands just right of the hole
  for scale.
- **Camera (2026-11-21): Tapo C120 replaces the Pi camera.** The Zero stays
  as the recorder (it can now sit anywhere with WiFi to the camera).
  `TapoCamera` (`picam/tapo.py`) plugs into the same `Camera` interface, so
  detector, recorder, events, pruning and review are shared; `[camera] source`
  picks it. Motion: PyAV decodes stream2, 10 fps to the detector at 320x180.
  Clips: ffmpeg copies stream1 (no re-encode) into ~2 s MPEG-TS pieces in
  `/dev/shm/picam-tapo`; a clip is the pieces from `preroll_s` before the
  trigger to the end, joined with `-c copy` into the MP4 once the end piece has
  closed (edges round out to keyframes). Each connection reconnects on its own
  with backoff; `frame()` raises `NoFrame` after `stall_s`, and the app then
  closes any open event and resets the detector. Measured 2026-11-21: motion
  process ~17% of one core, 86 MB.
- **Aim (Pi camera, set 2026-11-16):** camera on a tripod ~1 m from the hole, looking
  down at the sidewalk edge. The hole (~2" across, against the sidewalk, ~1"
  left of the ruler) sits at about (0.54, 0.45) of the frame. Grass around it
  was trimmed so the hole is visible. Detection area in `config.toml` is a
  ~12" x 10" patch around the hole.
- **No burned-in timestamp** (dropped for now). Clips and thumbnails are
  named by local date-time, e.g. `20261115-021433.mp4`; the DB holds the
  exact times. Revisit if the pest control company needs it on-frame.
- **Video plan (to confirm with `tools/camera_test.py --video`):** 2x2 binned
  sensor mode 1640x1232, main 1280x960 YUV420 → H.264 at ~3 Mbps, lores
  320x240 YUV420 for motion, 10-15 fps at night. MP4 via `PyavOutput`.
- **Camera tuning: always load `imx219_noir.json`**
  (`Picamera2(tuning=Picamera2.load_tuning_file("imx219_noir.json"))`). With
  the default `imx219.json`, auto white balance decides IR is ~2400 K
  incandescent and its colour correction cancels the signal: measured
  2026-11-15 at 66.6 ms / gain 9.85, mean brightness 0.6 with the default
  tuning vs 12.2 with the NoIR tuning, on the same scene.
- **Retention:** at startup and when the night window opens, unstarred clips
  older than `keep_nights` (30) are deleted, then oldest nights first until
  `min_free_gb` (10) is free. Starred clips and tonight are never touched;
  event rows stay with `clip`/`snapshot` set to NULL.
- **Clip review:** `picam/review.py`, stdlib `http.server` (no Flask) on
  127.0.0.1:8080 as `picam-review.service`, published by
  `tailscale serve --http=80` as http://zero/. Star and note are written to
  `events.db` (both processes use a 10 s busy timeout). `tools/fetch_starred.py`
  (stdlib, runs on Windows) pulls starred clips plus a JSON sidecar into
  `archive/` via `/api/starred`, replacing the planned rsync script.

## Code layout and running

- `picam/`: the app. Only `camera.py` touches picamera2 (`PiCamera`) and only
  `tapo.py` touches RTSP/ffmpeg (`TapoCamera`); the rest is pure Python and
  NumPy and runs on the PC with `FakeCamera`. Tapo login: `.env` (see
  `.env.example`), read by `config.tapo_login()`.
  `config.py` + `config.toml` · `motion.py` (detector) · `recorder.py` (clip
  state machine) · `schedule.py` (astral) · `events.py` (SQLite) ·
  `triggers.py` (PIR hook) · `prune.py` (storage cleanup) · `main.py`
  (loop and CLI) · `review.py` (review web page, separate process).
- On the Pi: `python3 -m picam` (also `--always-on`, `--dry-run`,
  `--duration S`). On the PC: `python -m picam --fake --always-on`.
- Tests on the PC: `.venv\Scripts\python -m pytest` (venv from
  `requirements-dev.txt`). The Pi has no pytest.
- `deploy/`: `picam.service`, a persistent-journal drop-in (Pi OS defaults to
  a RAM-only journal), a NetworkManager drop-in turning WiFi power save off
  (it dropped the Zero off Tailscale overnight), a logind drop-in with
  `RemoveIPC=no` (otherwise logging out of SSH deletes chris's `/dev/shm`
  files, i.e. the Tapo ring; `/run` at 84 MB is too small to hold it instead),
  `picam-review.service`,
  and `install.sh` that puts them in place and runs `tailscale serve`. Run `sudo deploy/install.sh` on the Pi (sudo needs a
  password, so Chris runs it). Logs: `journalctl -u picam`.
- `tools/camera_test.py` (stills and exposure bracket) and `tools/aim.py`
  (live view on :8000 with the detection area drawn; `--tapo` for the C120)
  are standalone.
- The Pi needs `ffmpeg` from apt for the Tapo (`install.sh` installs it).
- **picamera2 gotcha:** `CircularOutput2` (picamera2 0.3.39) is a delay line,
  not a dump-on-open buffer. Frames reach the open file only once they are
  `preroll_s` old, so `PiCamera.stop_clip()` closes the file `preroll_s`
  later. Closing immediately loses the whole live part of the clip.

## Requirements

1. Detect motion at night from the camera stream.
2. Save a short clip per event with a few seconds of pre-roll.
3. Log each event to SQLite.
4. Run unattended as a service and recover from errors.
5. Simple way to review clips from the PC (small web page or sync script).
6. Automatic storage cleanup.

## Suggested phases

1. Image the card, first boot, SSH and WiFi working.
2. One-file camera test that saves a still: confirm camera, focus, IR lighting.
3. Motion detection and clip recording.
4. SQLite event log.
5. systemd service and storage pruning.
6. Clip review from the PC.
7. Outdoor deployment and tuning (aim, sensitivity, false triggers).

## Working with Chris

- Experienced with Linux, networking, systemd, SQLite, Python and Git.
  Skip the basics.
- Propose a plan and wait for a go-ahead before large changes.
- Keep this file current as decisions are made.

## Open questions

- **Tapo tuning (from 2026-11-21):** detection area `[0.40, 0.12, 0.70, 0.88]`
  is a first guess from one daytime frame (hole at about (0.50-0.55,
  0.35-0.50)); check it at night with `tools/aim.py --tapo`. `min_area` 0.01 is
  carried over from the Pi camera. At this range grass blades are large, so wind
  may trigger more: watch the scores.
- **Night of 2026-11-21: no clips.** At 19:03 the last SSH logout wiped
  `/dev/shm/picam-tapo` (logind RemoveIPC). ffmpeg's error line then crashed
  `redact()` (`[segment @ 0x…]` has an `@` and no URL), and the record thread
  died. 14 events have snapshots only (19:44 to 05:47, a cluster 05:38–05:47).
  Fixed 2026-11-22: regex `redact`, ring dir recreated on every ffmpeg start,
  watcher errors retried, logind drop-in. The Tapo clip path is still unproven
  at night.
- The Zero hard-reset at ~16:47 on 2026-11-21 (no shutdown in the journal, no
  OOM, `get_throttled` 0x0 after). Files written just before it came back as
  zeros: run `sync` after copying anything important to the Pi.
- **First sighting, 2026-11-20 06:14:** event 45 (`20261120-061359.mp4`) shows
  a rabbit (looks like a cottontail) at the hole, 38 s. Not a rat. Cottontails
  don't usually dig burrows like this; keep watching for what actually digs. The
  19:22–19:25 events on 11-19 were the second illuminator being installed, and the
  Pi rebooted uncleanly at 19:33 (power re-plugged, presumably).
- Sensitivity: `min_area` 0.01 and the 2026-11-16 detection area. The
  2026-11-16 `--dry-run` night (19:44–06:59) scored 0 every second except
  8 short bursts (10–11 PM busiest), with no lighting changes, so no wind
  false triggers so far. Confirm against clips.
- Night exposure: AE with frame duration capped at 66.7 ms (15 fps) held
  66.7 ms / gain 9.85, brightness ~24, all night on 2026-11-16. With the second
  illuminator (2026-11-19) the aim.py box reads ~123 and frame ~75, 0.01%
  clipped, still at 66.6 ms / 9.85. The hole is at about (0.54, 0.50) and sits
  in the sidewalk edge's shadow (light is high and to the left).
- Brighter grass may raise wind false triggers: re-check `min_area` with a
  `--dry-run` night against the 2026-11-16 scores.
