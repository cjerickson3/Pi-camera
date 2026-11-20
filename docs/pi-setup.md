# Pi setup: imaging, first boot, camera test

Target: Pi Zero 2 W, hostname `zero`, user `chris`, key `~/.ssh/pi_key`.

## 1. Image the card (PC, card reader on F:)

Raspberry Pi Imager:

- **Device:** Raspberry Pi Zero 2 W
- **OS:** Raspberry Pi OS (other) → **Raspberry Pi OS Lite (64-bit)**
- **Storage:** the ~128 GB card on F:. Check the size before writing; Imager erases it.
- **Customisation:**
  - Hostname `zero`; user `chris` with a password (needed for `sudo`)
  - WiFi SSID and password, country **US**. The Zero 2 W is **2.4 GHz only**.
  - Timezone `America/Chicago`, keyboard `us`
  - Services: enable SSH, **public-key only**, and paste the contents of
    `C:\Users\chris\.ssh\pi_key.pub`

Add to `C:\Users\chris\.ssh\config`:

```
Host zero
  HostName zero.local
  User chris
  IdentityFile C:\Users\chris\.ssh\pi_key
```

After Tailscale is up (step 4), change `HostName` to `zero` (MagicDNS name),
which works from anywhere.

## 2. Camera cable

Power off first. The Zero uses the narrow 22-pin end of the cable. At both
ends the contacts face the PCB: on the Zero they face down toward the board,
and on the camera they face the camera board. Push the latch in fully.

## 3. First boot

```sh
ssh zero
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y --no-install-recommends python3-picamera2 python3-av python3-pil git
rpicam-hello --list-cameras        # expect: imx219 [3280x2464]
vcgencmd get_throttled             # expect: throttled=0x0
```

If no camera is listed, check `camera_auto_detect=1` in
`/boot/firmware/config.txt` and reseat the cable.

**Power:** the Pi runs on the official Raspberry Pi 2.5 A supply, which meets
the recommended rating. `get_throttled` should still read `0x0` after the
video test. `0x50005` or similar means under-voltage, most likely from a
loose connector or an extension cable added for the outdoor install.

Check swap is zram, not a file on the card: `swapon --show`.

Raspberry Pi OS keeps the journal in RAM only
(`/usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf`), so a crash
or power loss erases the logs. `deploy/install.sh` (step 6) overrides that.

## 4. Tailscale

```sh
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Open the printed URL to add `zero` to your tailnet. Disable key expiry for
this machine in the Tailscale admin console so it doesn't drop off after 180
days. The review web page is served on the tailnet only (step 7).

## 5. Get the code and run the camera test

```sh
git clone https://github.com/cjerickson3/Pi-camera.git ~/Pi-camera
cd ~/Pi-camera
python3 tools/camera_test.py --video 10
```

Run it once in daylight (framing, aim) and once at night with the IR
illuminator on (exposure, IR coverage, hotspots). Copy the results back to the PC:

```sh
scp -r zero:stills .
```

What to look at:

| Check | Where |
|---|---|
| Framing and aim on the hole | `auto_full.jpg` |
| Which night exposure looks usable | `manual_*ms_g8.jpg`; `mean` ~60–120 and `clip` low is good |
| IR coverage, hotspots, reflections | night `manual_*` images |
| Motion stream is usable | `lores_y.jpg` (320x240, what the detector sees) |
| Encoder keeps up, fps, MB/min | `summary.json` → `video` |
| Power | `throttled_after` must be `0x0` |

The exposure time caps the frame rate: 100 ms means at most 10 fps. Pick the
shortest exposure that gives a usable image.

### Focus

The Camera Module 2 is focused from about 1 m to infinity. If the hole is
closer than about 1 m, or the night images look soft, turn the lens with the
focus tool, a little at a time in either direction, while watching the live score:

```sh
python3 tools/camera_test.py --focus 60
```

Keep the scene and lighting fixed. Put something with texture (a printed page)
at the hole and turn the lens slowly for the highest number.

## 6. Install the service

```sh
cd ~/Pi-camera && git pull
sudo deploy/install.sh
```

This installs and starts `picam.service` (enabled at boot, restarts on
failure; the app itself idles outside the night window) and
`picam-review.service` (step 7). It also:

- makes the journal persistent (`/etc/systemd/journald.conf.d/50-persistent.conf`,
  capped at 100 MB), so `journalctl -b -1` works after a crash;
- turns WiFi power saving off (`/etc/NetworkManager/conf.d/wifi-powersave-off.conf`,
  plus `iw` immediately). With it on, the Zero dropped off Tailscale for the
  night of 2026-11-16 while the camera kept running.

Day to day:

```sh
journalctl -u picam -f              # live log
systemctl status picam
git pull && sudo systemctl restart picam   # deploy (re-run install.sh if deploy/ changed)
```

## 7. Clip review

`picam-review.service` runs `python3 -m picam.review` on `127.0.0.1:8080`.
`install.sh` publishes it to the tailnet with `tailscale serve --bg --http=80 8080`,
so from the PC or phone on Tailscale it is **http://zero/**. Nothing listens
on the LAN. The first `tailscale serve` on a tailnet may print a link to
enable Serve in the admin console; open it, then re-run `install.sh`.

- **Nights:** event count, first and last event, and an hourly strip (17:00 to 08:00).
- **Night:** snapshot grid; "starred only" filter; link to that night's scores CSV.
- **Event:** clip player (seekable), star, note, prev/next.

Starred clips are never pruned. To archive them on the PC (Windows, stdlib only):

```
python tools/fetch_starred.py            # -> archive/<night>/ (gitignored)
```

Each clip gets its snapshot and a `.json` with the event row (times, scores,
note). Re-running fetches only new stars.

Check or undo the Tailscale side:

```sh
tailscale serve status
sudo tailscale serve --http=80 off
```

To look at a copy of the data on the PC, point a config at it and run
`python -m picam.review --config copy.toml --bind 127.0.0.1`.
