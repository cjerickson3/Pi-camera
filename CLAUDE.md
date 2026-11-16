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
| Camera Module 2 NoIR (IMX219, 8 MP) | No IR filter, for night viewing. Fixed focus. Pi Zero camera cable. |
| 12 V IR illuminator | **850 nm** (faint red glow visible on the LEDs). Small grey dome. Has its own 12 V supply and a built-in photocell (on at dusk). Not switched or controlled by the Pi. Must sit beside the camera aiming at the scene, never facing the lens. |
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
- **Recording window:** one hour after sunset until sunrise, computed for
  Plano, TX (approx. 33.02 N, 96.70 W) with `python3-astral`. The illuminator
  has its own photocell, so the Pi does not detect night itself.
- **Scope:** one burrow watched (down from three): the hole **beside the
  sidewalk**. Zones per hole are no longer needed; a single optional ROI mask
  in config is enough. During setup a 12" ruler stands just right of the hole
  for scale.
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
- **Clip review:** small web page on the Pi, served on the tailnet only, plus
  an rsync script for archiving starred clips to the PC.

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

- Aim: night tests on 2026-11-15 had the camera and light at grass level,
  looking level across the yard (mostly trees and sky; the hole was hidden by
  grass and unlit). Both need raising and tilting down at the hole. Use
  `tools/aim.py` (live view on port 8000) while adjusting.
- Night exposure: tentatively AE with frame duration capped at 66.7 ms
  (15 fps). In the first test AE chose 66 ms at gain ~10. Confirm after re-aim.
