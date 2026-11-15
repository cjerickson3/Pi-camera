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

**Power:** the supply is 2.2 A against an official 2.5 A recommendation. The
Zero 2 W with camera and WiFi usually draws well under 1 A, so it should be
fine, but `get_throttled` must stay `0x0` after the video test. `0x50005`
or similar means under-voltage: change the supply or use a shorter, thicker
USB cable.

Optional SD-wear reduction:

```sh
sudo sed -i 's/^#\?Storage=.*/Storage=volatile/' /etc/systemd/journald.conf
sudo systemctl restart systemd-journald
swapon --show                       # want zram, not /var/swap
```

## 4. Tailscale

```sh
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Open the printed URL to add `zero` to your tailnet. Disable key expiry for
this machine in the Tailscale admin console so it doesn't drop off after 180
days. The review web page will be served on the tailnet only (Phase 5).

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
