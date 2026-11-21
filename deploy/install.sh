#!/bin/sh
# Install the picam and review services, persistent journal, WiFi power-save
# fix, and publish the review page on the tailnet.
# Run on the Pi from the repo: sudo deploy/install.sh   (safe to re-run)
set -eu
cd "$(dirname "$0")"

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

# Persistent journal
install -d /etc/systemd/journald.conf.d
install -m 644 journald-persistent.conf /etc/systemd/journald.conf.d/50-persistent.conf
install -d -g systemd-journal -m 2755 /var/log/journal
systemd-tmpfiles --create --prefix /var/log/journal
systemctl restart systemd-journald
journalctl --flush

# WiFi power save: persistent via NetworkManager, and off right now via iw
# (no reconnect, so an SSH session over Tailscale survives).
install -m 644 wifi-powersave-off.conf /etc/NetworkManager/conf.d/wifi-powersave-off.conf
command -v iw >/dev/null || apt-get install -y --no-install-recommends iw
iw dev wlan0 set power_save off
iw dev wlan0 get power_save

# ffmpeg records the Tapo camera's stream ([camera] source = "tapo")
command -v ffmpeg >/dev/null || apt-get install -y --no-install-recommends ffmpeg

# Services
install -m 644 picam.service /etc/systemd/system/picam.service
install -m 644 picam-review.service /etc/systemd/system/picam-review.service
systemctl daemon-reload
systemctl enable picam.service picam-review.service
systemctl restart picam.service picam-review.service

# Review page on the tailnet: http://zero/ -> 127.0.0.1:8080. Persists across
# reboots. Plain HTTP; tailnet traffic is already encrypted by WireGuard.
tailscale serve --bg --http=80 8080
tailscale serve status

sleep 3
systemctl --no-pager status picam.service picam-review.service | grep -E '●|Active:|Main PID' 
