#!/usr/bin/env bash
# One-command setup of the real-launch recorder on a fresh Ubuntu/Debian server (a $5/month VPS is plenty).
# Run as root:   bash install_recorder.sh
# It installs Python, fetches this project, and starts the recorder as a service that survives reboots.
# Data goes to /var/lib/sniper/ (one new file per day). Read-only: it holds no wallet and no keys.
set -euo pipefail

REPO="${REPO:-https://github.com/FL1105/Roblox-game-test}"
BRANCH="${BRANCH:-claude/loving-cori-dvrk02}"
APP=/opt/sniper
DATA=/var/lib/sniper

if [ "$(id -u)" -ne 0 ]; then echo "Please run as root (sudo bash install_recorder.sh)"; exit 1; fi

apt-get update -y
apt-get install -y python3 python3-venv git

if [ -d "$APP/.git" ]; then git -C "$APP" fetch origin "$BRANCH" && git -C "$APP" checkout "$BRANCH" && git -C "$APP" pull --ff-only origin "$BRANCH"; else git clone --branch "$BRANCH" "$REPO" "$APP"; fi
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install --quiet websockets
mkdir -p "$DATA"

cat > /etc/systemd/system/sniper-recorder.service <<UNIT
[Unit]
Description=Real pump.fun launch recorder (read-only)
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$APP
# One file per run; the run lasts 24 h, then systemd starts a fresh file.
ExecStart=/bin/bash -c '$APP/venv/bin/python -m sniper.live --out $DATA/raw_\$(date +%%Y%%m%%d_%%H%%M).jsonl --minutes 1440'
Restart=always
RestartSec=5
# It can't touch anything but its own data folder.
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=$DATA
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now sniper-recorder.service
sleep 3
systemctl --no-pager --lines=5 status sniper-recorder.service || true
echo
echo "Recorder is running. Data files: $DATA"
echo "Watch it:        journalctl -u sniper-recorder -f"
echo "Stop it:         systemctl stop sniper-recorder"
echo "Copy data home:  scp root@THIS_SERVER:$DATA/*.jsonl ."
