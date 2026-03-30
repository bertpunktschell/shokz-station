#!/bin/bash
# Shokz Station - Install Script
# Tested on: Raspberry Pi Zero 1W, Pi 3, Raspbian/Raspberry Pi OS (Bookworm)
# Usage: bash install.sh

set -e

INSTALL_DIR="/home/$USER/shokz-station"
MOUNT_POINT="/media/shokz"
PORT=8080

echo "======================================"
echo " Shokz Station Installer"
echo "======================================"
echo "User:        $USER"
echo "Install dir: $INSTALL_DIR"
echo "Mount point: $MOUNT_POINT"
echo "Port:        $PORT"
echo ""

# 1. System dependencies
echo "[1/6] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y python3-pip python3-venv ffmpeg

# 2. Mount point
echo "[2/6] Creating mount point..."
sudo mkdir -p $MOUNT_POINT
sudo chown $USER:$USER $MOUNT_POINT

# 3. Copy app files
echo "[3/6] Installing app files..."
mkdir -p $INSTALL_DIR/static

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp $SCRIPT_DIR/app.py $INSTALL_DIR/
cp $SCRIPT_DIR/downloader.py $INSTALL_DIR/
cp $SCRIPT_DIR/file_manager.py $INSTALL_DIR/
cp $SCRIPT_DIR/requirements.txt $INSTALL_DIR/
cp -r $SCRIPT_DIR/static/ $INSTALL_DIR/

# 4. Python venv + dependencies
echo "[4/6] Setting up Python venv..."
python3 -m venv $INSTALL_DIR/venv
$INSTALL_DIR/venv/bin/pip install --quiet -r $INSTALL_DIR/requirements.txt

# 5. Mount watcher service
echo "[5/6] Installing mount watcher..."
sudo cp $SCRIPT_DIR/shokz-mount.sh /usr/local/bin/shokz-mount.sh
sudo chmod +x /usr/local/bin/shokz-mount.sh

sudo tee /etc/systemd/system/shokz-watch.service > /dev/null << EOF
[Unit]
Description=Shokz USB Mount Watcher
After=local-fs.target

[Service]
ExecStart=/usr/local/bin/shokz-mount.sh
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

# Allow user to unmount without password
echo "$USER ALL=(ALL) NOPASSWD: /bin/umount" | sudo tee /etc/sudoers.d/shokz-umount
sudo chmod 440 /etc/sudoers.d/shokz-umount

# 6. App service
echo "[6/6] Installing app service..."
# Detect number of CPUs for worker count
WORKERS=$(( $(nproc) * 2 + 1 ))
# Cap at 4 workers (Pi Zero has only 1 core)
[ $WORKERS -gt 4 ] && WORKERS=4

sudo tee /etc/systemd/system/shokz-station.service > /dev/null << EOF
[Unit]
Description=Shokz Station Web App
After=network.target shokz-watch.service

[Service]
User=$USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/gunicorn --workers $WORKERS --threads 2 --bind 0.0.0.0:$PORT --timeout 120 app:app
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable shokz-watch shokz-station
sudo systemctl start shokz-watch shokz-station

sleep 4

echo ""
echo "======================================"
echo " Installation complete!"
echo "======================================"

# Get local IP
IP=$(hostname -I | awk '{print $1}')
echo " Web UI: http://$IP:$PORT"
echo ""
echo " Service status:"
sudo systemctl is-active shokz-station && echo " ✅ shokz-station: running" || echo " ❌ shokz-station: failed"
sudo systemctl is-active shokz-watch   && echo " ✅ shokz-watch:   running" || echo " ❌ shokz-watch:   failed"
echo ""
echo " Plug in your Shokz via USB and open the URL above!"
