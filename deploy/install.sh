#!/bin/sh
# Install the picam service, persistent journal and WiFi power-save fix.
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

# Service
install -m 644 picam.service /etc/systemd/system/picam.service
systemctl daemon-reload
systemctl enable picam.service
systemctl restart picam.service
sleep 3
systemctl --no-pager status picam.service | head -15
