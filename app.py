import os
import json
import time
import queue
import threading
import subprocess
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context

import file_manager as fm
import downloader as dl
import tidal

app = Flask(__name__, static_folder="static", static_url_path="")

# ── LED control ───────────────────────────────────────────────────────────────
def led(state):
    """Set LED state: boot | ready | busy | off"""
    try:
        subprocess.run(['sudo', '/usr/local/bin/shokz-led.sh', state],
                       capture_output=True, timeout=2)
    except Exception:
        pass

def led_busy_then_ready():
    """Set LED to busy, then back to ready in background."""
    def _run():
        led('busy')
        time.sleep(1.5)
        led('ready')
    threading.Thread(target=_run, daemon=True).start()

# Signal app is ready
led('ready')

# ── SSE Progress ─────────────────────────────────────────────────────────────
# Per-client queues for upload/job progress
_sse_queues = {}
_sse_lock = threading.Lock()

def _push_event(client_id, event_type, data):
    with _sse_lock:
        q = _sse_queues.get(client_id)
        if q:
            q.put({"event": event_type, "data": data})

# ── Static ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/favicon.ico")
def favicon():
    return send_from_directory("static", "favicon.svg", mimetype="image/svg+xml")

# ── Status ────────────────────────────────────────────────────────────────────
@app.route("/api/status")
def status():
    return jsonify({"mounted": fm.is_mounted()})

# ── File Manager ──────────────────────────────────────────────────────────────
@app.route("/api/files", strict_slashes=False)
@app.route("/api/files/<path:rel_path>", strict_slashes=False)
def list_files(rel_path=""):
    try:
        entries = fm.list_directory(rel_path)
        return jsonify({"path": rel_path, "entries": entries})
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403

@app.route("/api/mkdir", methods=["POST"])
def mkdir():
    data = request.json
    try:
        fm.make_directory(data.get("path", ""))
        led_busy_then_ready()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/delete", methods=["POST"])
def delete():
    data = request.json
    try:
        fm.delete_item(data.get("path", ""))
        led_busy_then_ready()
        return jsonify({"ok": True})
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/rename", methods=["POST"])
def rename():
    data = request.json
    try:
        fm.rename_item(data.get("path", ""), data.get("new_name", ""))
        led_busy_then_ready()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/move", methods=["POST"])
def move():
    data = request.json
    try:
        fm.move_item(data.get("src", ""), data.get("dst_dir", ""))
        led_busy_then_ready()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# ── Upload ────────────────────────────────────────────────────────────────────
CHUNK_SIZE = 256 * 1024  # 256KB chunks

@app.route("/api/upload", methods=["POST"])
def upload():
    dest_dir = request.form.get("dest_dir", "")
    base = Path(fm.MOUNT_PATH)
    dest = (base / dest_dir).resolve()
    if not str(dest).startswith(str(base)):
        return jsonify({"error": "Access denied"}), 403

    results = []
    led('busy')
    for file in request.files.getlist("files"):
        filename = Path(file.filename).name
        out_path = dest / filename
        try:
            file.save(str(out_path))
            results.append({"name": filename, "ok": True})
        except Exception as e:
            results.append({"name": filename, "ok": False, "error": str(e)})
    led('ready')
    return jsonify({"results": results})

# ── Download (yt-dlp) ─────────────────────────────────────────────────────────
@app.route("/api/dl/info", methods=["POST"])
def dl_info():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    job_id = dl.start_info_fetch(url)
    return jsonify({"job_id": job_id})

@app.route("/api/dl/info/status/<job_id>")
def dl_info_status(job_id):
    job = dl.get_info_job(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    return jsonify(job)

@app.route("/api/dl/start", methods=["POST"])
def dl_start():
    data = request.json
    url = data.get("url", "").strip()
    dest_dir = data.get("dest_dir", "")
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    job_id = dl.start_download(url, dest_dir)
    return jsonify({"job_id": job_id})

@app.route("/api/dl/status/<job_id>")
def dl_status(job_id):
    job = dl.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)

@app.route("/api/dl/stream/<job_id>")
def dl_stream(job_id):
    """SSE stream for download progress."""
    def generate():
        last = {}
        for _ in range(600):  # max 5 minutes polling
            job = dl.get_job(job_id)
            if not job:
                yield "data: {}\n\n"
                break
            if job != last:
                last = dict(job)
                yield f"data: {json.dumps(job)}\n\n"
            if job.get("status") in ("done", "error"):
                break
            time.sleep(0.5)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no"
        }
    )

@app.route("/api/events")
def events():
    """SSE stream - pushes mount status changes to browser."""
    def generate():
        last = None
        while True:
            current = fm.is_mounted()
            if current != last:
                last = current
                import json
                yield f"data: {json.dumps({'mounted': current})}\n\n"
            time.sleep(2)
    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )

@app.route("/api/play/<path:rel_path>")
def play_file(rel_path):
    base = Path(fm.MOUNT_PATH)
    file_path = (base / rel_path).resolve()
    if not str(file_path).startswith(str(base)):
        return jsonify({"error": "Access denied"}), 403
    if not file_path.is_file():
        return jsonify({"error": "Not found"}), 404
    return send_from_directory(str(file_path.parent), file_path.name, conditional=True)

@app.route("/api/storage")
def storage():
    """Return disk usage info for the mounted device."""
    if not fm.is_mounted():
        return jsonify({"mounted": False})
    try:
        stat = os.statvfs(fm.MOUNT_PATH)
        total = stat.f_blocks * stat.f_frsize
        free = stat.f_bavail * stat.f_frsize
        used = total - free
        return jsonify({
            "mounted": True,
            "total": total,
            "used": used,
            "free": free,
            "percent": round(used / total * 100, 1) if total > 0 else 0
        })
    except Exception as e:
        return jsonify({"mounted": False, "error": str(e)})

@app.route("/api/eject", methods=["POST"])
def eject():
    LOCK = "/run/shokz-ejected"
    try:
        # Set lock first so watcher doesn't remount
        subprocess.run(['sudo', 'touch', LOCK], capture_output=True, timeout=3)
        # Kill any processes with open files on the mount (e.g. audio streaming)
        subprocess.run(['sudo', 'fuser', '-k', fm.MOUNT_PATH],
                       capture_output=True, timeout=3)
        time.sleep(0.3)
        # Try clean unmount first, then lazy
        for args in [['sudo', 'umount', fm.MOUNT_PATH],
                     ['sudo', 'umount', '-l', fm.MOUNT_PATH]]:
            result = subprocess.run(args, capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return jsonify({"ok": True})
        # Both failed - remove lock
        subprocess.run(['sudo', 'rm', '-f', LOCK], capture_output=True, timeout=3)
        return jsonify({"error": "Unmount failed"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Tidal Auth ────────────────────────────────────────────────────────────────
@app.route("/api/tidal/auth/status")
def tidal_auth_status():
    return jsonify(tidal.get_auth_status())

@app.route("/api/tidal/auth/login", methods=["POST"])
def tidal_auth_login():
    try:
        result = tidal.start_device_auth()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/tidal/auth/poll", methods=["POST"])
def tidal_auth_poll():
    data = request.json
    device_code = data.get("device_code", "")
    if not device_code:
        return jsonify({"error": "No device_code provided"}), 400
    success, result = tidal.poll_device_auth(device_code)
    return jsonify({"success": success, **result})

@app.route("/api/tidal/auth/logout", methods=["POST"])
def tidal_auth_logout():
    tidal.logout()
    return jsonify({"ok": True})

# ── Tidal Download ────────────────────────────────────────────────────────────
@app.route("/api/tidal/dl/start", methods=["POST"])
def tidal_dl_start():
    data = request.json
    url = data.get("url", "").strip()
    dest_dir = data.get("dest_dir", "")
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    # Check auth first
    auth = tidal.get_auth_status()
    if not auth.get("authenticated"):
        if auth.get("reason") == "token_expired_refreshable":
            if not tidal.refresh_auth():
                return jsonify({"error": "Tidal token expired. Please log in again."}), 401
        else:
            return jsonify({"error": "Not logged in to Tidal. Please authenticate first."}), 401
    job_id = tidal.start_download(url, dest_dir)
    return jsonify({"job_id": job_id})

@app.route("/api/tidal/dl/status/<job_id>")
def tidal_dl_status(job_id):
    job = tidal.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)

@app.route("/api/tidal/dl/stream/<job_id>")
def tidal_dl_stream(job_id):
    """SSE stream for tidal download progress."""
    def generate():
        last = {}
        for _ in range(1200):  # max 10 minutes polling
            job = tidal.get_job(job_id)
            if not job:
                yield "data: {}\n\n"
                break
            if job != last:
                last = dict(job)
                yield f"data: {json.dumps(job)}\n\n"
            if job.get("status") in ("done", "error"):
                break
            time.sleep(0.5)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no"
        }
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False, threaded=True)
