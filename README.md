# 🎧 Shokz Station

A lightweight web-based file manager and music downloader for **Shokz bone conduction headphones** with internal storage. Runs on a Raspberry Pi (Zero 1W, Pi 3, or similar).

## Features

- 📁 **File Manager** — Browse, rename, delete, create folders, drag-to-move files
- ⬆️ **Upload** — Drag & drop or file picker with per-file progress bars
- ⬇️ **Download** — Paste a YouTube or SoundCloud URL, downloads directly to the Shokz
- ⏏️ **Safe Eject** — Properly unmounts before unplugging
- 🔄 **Auto-Mount** — Shokz is automatically mounted when plugged in
- 📱 **Responsive UI** — Works on mobile and desktop

## Requirements

- Raspberry Pi (Zero 1W, 3, 4, etc.) with Raspberry Pi OS (Bookworm)
- Python 3.x
- Internet connection (for yt-dlp downloads)
- Shokz headphones with USB storage (e.g. OpenRun Pro, OpenSwim Pro)

## Installation

```bash
git clone https://github.com/bertpunktschell/shokz-station.git
cd shokz-station
bash install.sh
```

The installer will:
1. Install `ffmpeg`, `python3-venv` via apt
2. Set up a Python virtualenv with Flask, gunicorn, yt-dlp
3. Create mount point `/media/shokz`
4. Install two systemd services: `shokz-station` (web app) and `shokz-watch` (USB automount)
5. Configure passwordless `umount` for the current user

After installation, open `http://<pi-ip>:8080` in your browser.

## Architecture

```
┌─────────────────────────────────────┐
│  Browser (mobile or desktop)        │
│  http://<pi-ip>:8080                │
└────────────┬────────────────────────┘
             │ HTTP / SSE
┌────────────▼────────────────────────┐
│  Flask + Gunicorn (port 8080)       │
│  ├── file_manager.py  (CRUD ops)    │
│  ├── downloader.py    (yt-dlp jobs) │
│  └── app.py           (routes/SSE)  │
└────────────┬────────────────────────┘
             │
┌────────────▼────────────────────────┐
│  /media/shokz  (FAT32, USB OTG)    │
│  ↑ auto-mounted by shokz-watch     │
└─────────────────────────────────────┘
```

**Key design decisions:**
- **Gunicorn** with multiple workers instead of Flask dev server (non-blocking)
- **SQLite** for job state (shared across gunicorn workers, survives worker restarts)
- **SSE** (Server-Sent Events) for real-time progress and mount status (no polling)
- **No ffmpeg conversion** for downloads — native audio format from source (MP3/M4A)
- **`--restrict-filenames`** for yt-dlp — FAT32 doesn't allow `|`, `*`, `?` etc. in filenames
- **Lock file** in `/run/user/1000/` (tmpfs) for eject state — auto-cleared on reboot

## Services

| Service | Description |
|---|---|
| `shokz-station` | Web app (gunicorn) |
| `shokz-watch` | USB automount watcher (polls every 3s) |

```bash
# Status
sudo systemctl status shokz-station shokz-watch

# Restart
sudo systemctl restart shokz-station

# Logs
sudo journalctl -u shokz-station -f
```

## Notes

- yt-dlp downloads on Pi Zero 1W are slow (~25-40s for SoundCloud info fetch, 1-3 min download)
- Pi 3/4 is significantly faster
- The Shokz shows up as a generic USB Mass Storage device (`/dev/sdX1`)
- FAT32 filesystem — filenames are restricted to ASCII-safe characters

## ⚡ Pi Zero 1W Boot Performance

By default, Raspberry Pi OS (Bookworm) on a Pi Zero 1W takes **~100s+ to boot**. With the optimizations below, you can get it down to **~55-60s**.

> ⚠️ **Caution:** Some of these disable services that may be useful in other contexts. Review before applying.

### Results

| Metric | Default | Optimized |
|--------|---------|-----------|
| Total boot time | ~100s | **~58s** |
| Userspace | ~88s | **~47s** |

### Optimizations Applied

**1. Mask/unmask unnecessary services:**

```bash
# Cloud-init (used by Raspberry Pi Imager — not needed after setup)
sudo systemctl mask cloud-init.service cloud-init-main.service \
  cloud-init-local.service cloud-init-network.service cloud-final.service

# NetworkManager-wait-online (blocks boot until network is fully up)
sudo systemctl disable NetworkManager-wait-online.service

# Unneeded on headless devices
sudo systemctl mask systemd-rfkill.service systemd-rfkill.socket \
  systemd-binfmt.service e2scrub_reap.service \
  sys-kernel-debug.mount sys-kernel-tracing.mount \
  run-lock.mount dev-mqueue.mount \
  systemd-hostnamed.service systemd-timesyncd.service

# Re-enable if needed
sudo systemctl unmask avahi-daemon.service   # for .local resolution
sudo systemctl unmask tmp.mount               # /tmp in RAM, actually useful
```

**2. Boot config tweaks** (`/boot/firmware/config.txt`, add under `[all]`):

```ini
boot_delay=0
boot_wait=0
```

**3. Optional: Disable kernel updates** (prevents unwanted kernel upgrades that may not boot on Pi Zero 1W):

```bash
sudo apt-mark hold linux-image-rpi-v6 linux-image-rpi-v7 linux-image-rpi-v8
```

**4. Optional: Use static IP instead of DHCP** (saves ~20s by removing NetworkManager):

> ⚠️ Make sure you know the IP before doing this, or have serial console access!

```bash
# Use systemd-networkd instead of NetworkManager
sudo systemctl disable --now NetworkManager
sudo systemctl enable systemd-networkd systemd-resolved
# Create: /etc/systemd/network/eth0.network
```

### What to Keep

- **`avahi-daemon`** — enables `pizero.local` mDNS resolution. Keep it.
- **`tmp.mount`** — puts `/tmp` in RAM. Keeps SD card writes low.
- **`ssh.service`** — obviously needed.
- **`systemd-logind`** — required for `sudo reboot` to work properly.

### Why This Works

The default Raspberry Pi OS image includes many desktop/server-oriented services (cloud-init, NetworkManager-wait-online, ModemManager, bluetooth, etc.) that are completely unnecessary on a headless Pi Zero 1W. Each disabled service saves 1-25s of boot time.

### Verification

```bash
# Measure boot time
systemd-analyze time

# See what's slowest
systemd-analyze blame | head -20
```

## License

MIT

