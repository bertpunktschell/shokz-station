import subprocess
import json
import re
import threading
import uuid
import time
import sqlite3
from pathlib import Path
from file_manager import MOUNT_PATH

DB_PATH = "/home/christoph/shokz-station/jobs.db"

def _get_db():
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.execute("""CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        kind TEXT,
        status TEXT,
        progress INTEGER,
        message TEXT,
        error TEXT,
        url TEXT,
        title TEXT,
        updated REAL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS info_jobs (
        id TEXT PRIMARY KEY,
        status TEXT,
        result TEXT,
        updated REAL
    )""")
    db.commit()
    return db

def _set_job(job_id, **kwargs):
    db = _get_db()
    fields = list(kwargs.keys())
    vals = [kwargs[f] for f in fields] + [time.time(), job_id]
    sets = ", ".join(f + "=?" for f in fields) + ", updated=?"
    db.execute(f"UPDATE jobs SET {sets} WHERE id=?", vals)
    db.commit()
    db.close()

def get_job(job_id):
    db = _get_db()
    row = db.execute("SELECT id,status,progress,message,error,url,title FROM jobs WHERE id=?", (job_id,)).fetchone()
    db.close()
    if not row:
        return {}
    return {"id": row[0], "status": row[1], "progress": row[2], "message": row[3], "error": row[4], "url": row[5], "title": row[6]}

def list_jobs():
    db = _get_db()
    rows = db.execute("SELECT id,status,progress,message,error,url,title FROM jobs ORDER BY updated DESC LIMIT 20").fetchall()
    db.close()
    return {r[0]: {"status": r[1], "progress": r[2], "message": r[3], "error": r[4], "url": r[5], "title": r[6]} for r in rows}

def start_info_fetch(url):
    job_id = str(uuid.uuid4())[:8]
    db = _get_db()
    db.execute("INSERT INTO info_jobs (id, status, result, updated) VALUES (?,?,?,?)",
               (job_id, "pending", None, time.time()))
    db.commit()
    db.close()
    thread = threading.Thread(target=_run_info_fetch, args=(job_id, url), daemon=True)
    thread.start()
    return job_id

def get_info_job(job_id):
    db = _get_db()
    row = db.execute("SELECT status, result FROM info_jobs WHERE id=?", (job_id,)).fetchone()
    db.close()
    if not row:
        return {}
    result = json.loads(row[1]) if row[1] else None
    return {"status": row[0], "result": result}

def _run_info_fetch(job_id, url):
    result = get_video_info(url)
    db = _get_db()
    db.execute("UPDATE info_jobs SET status=?, result=?, updated=? WHERE id=?",
               ("done", json.dumps(result), time.time(), job_id))
    db.commit()
    db.close()

def get_video_info(url):
    try:
        result = subprocess.run(
            ["./venv/bin/yt-dlp", "--dump-json", "--no-playlist", url],
            capture_output=True, text=True, timeout=60,
            cwd="/home/christoph/shokz-station"
        )
        if result.returncode != 0:
            return {"error": result.stderr.strip() or "Failed to fetch info"}
        data = json.loads(result.stdout)
        return {
            "title": data.get("title", "Unknown"),
            "uploader": data.get("uploader", ""),
            "duration": data.get("duration"),
            "thumbnail": data.get("thumbnail"),
        }
    except subprocess.TimeoutExpired:
        return {"error": "Timeout fetching info"}
    except Exception as e:
        return {"error": str(e)}

def start_download(url, dest_dir=""):
    job_id = str(uuid.uuid4())[:8]
    dest = str(Path(MOUNT_PATH) / dest_dir)
    db = _get_db()
    db.execute("INSERT INTO jobs (id,kind,status,progress,message,error,url,title,updated) VALUES (?,?,?,?,?,?,?,?,?)",
               (job_id, "download", "starting", 0, "Connecting...", None, url, None, time.time()))
    db.commit()
    db.close()
    thread = threading.Thread(target=_run_download, args=(job_id, url, dest), daemon=True)
    thread.start()
    return job_id

def _run_download(job_id, url, dest):
    def update(**kwargs):
        _set_job(job_id, **kwargs)

    try:
        cmd = [
            "./venv/bin/yt-dlp",
            "--no-playlist",
            # Prefer native MP3, then M4A, then best audio - NO conversion
            "--format", "bestaudio[ext=mp3]/bestaudio[ext=m4a]/bestaudio",
            "--no-post-overwrites",
            "--restrict-filenames",
            "-o", f"{dest}/%(title)s.%(ext)s",
            "--newline",
            url
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd="/home/christoph/shokz-station"
        )

        for line in proc.stdout:
            line = line.strip()

            # Extract title from destination line early in the process
            dest_match = re.search(r'\[download\] Destination: .+/(.+)\.\w+$', line)
            if dest_match:
                title = dest_match.group(1)
                update(title=title, message="Downloading...")

            # Download progress
            dl_match = re.search(r'\[download\]\s+([\d.]+)%\s+of\s+([\d.]+\w+)', line)
            if dl_match:
                pct = float(dl_match.group(1))
                size = dl_match.group(2)
                update(progress=int(pct), message=f"Downloading... {pct:.0f}% of {size}")

        proc.wait()
        if proc.returncode == 0:
            update(status="done", progress=100, message="Done!")
        else:
            update(status="error", message="Download failed", error="yt-dlp exited with error")

    except Exception as e:
        update(status="error", message="Unexpected error", error=str(e))
