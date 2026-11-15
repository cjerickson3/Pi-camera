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
| 12 V IR illuminator | Has its own 12 V supply. Not switched or controlled by the Pi. |
| 5 V USB power supply for the Pi | Rating to be confirmed; the Zero 2 W with camera and WiFi wants 2 A or more |
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
- The Pi runs headless: Raspberry Pi OS Lite, WiFi, SSH.
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

- Pi power supply rating (see hardware table).
- Clip review method: recommended a small web page on the Pi (plus an rsync
  script for archiving starred clips); awaiting confirmation.
- Recording window (sunset/sunrise vs fixed vs 24/7), IR illuminator photocell
  or always-on, LAN-only vs remote review access, burned-in timestamp.
- Camera placement and distance to each burrow (affects focus and IR aim).
