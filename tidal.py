"""
Tidal download integration for Shokz Station.
Uses tiddl Python API directly (not CLI) for downloads with real progress tracking.
"""

import json
import re
import sys
import threading
import time
import uuid
import shutil
from pathlib import Path
from tempfile import NamedTemporaryFile

import sqlite3

# ── tiddl path setup ─────────────────────────────────────────────────────────
TIDDL_VENV = "/home/christoph/shokz-station/tiddl-venv"
TIDDL_SITE = f"{TIDDL_VENV}/lib/python3.13/site-packages"
TIDDL_PATH = "/home/christoph/.tiddl"
AUTH_FILE = Path(TIDDL_PATH) / "auth.json"

# Make tiddl importable
if TIDDL_SITE not in sys.path:
    sys.path.insert(0, TIDDL_SITE)

from file_manager import MOUNT_PATH

DB_PATH = "/home/christoph/shokz-station/jobs.db"

# ── Database ──────────────────────────────────────────────────────────────────

def _get_db():
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.execute("""CREATE TABLE IF NOT EXISTS tidal_jobs (
        id TEXT PRIMARY KEY,
        status TEXT,
        progress INTEGER,
        message TEXT,
        error TEXT,
        url TEXT,
        title TEXT,
        track_num TEXT,
        updated REAL
    )""")
    db.commit()
    return db


def _set_job(job_id, **kwargs):
    db = _get_db()
    fields = list(kwargs.keys())
    vals = [kwargs[f] for f in fields] + [time.time(), job_id]
    sets = ", ".join(f + "=?" for f in fields) + ", updated=?"
    db.execute(f"UPDATE tidal_jobs SET {sets} WHERE id=?", vals)
    db.commit()
    db.close()


def get_job(job_id):
    db = _get_db()
    row = db.execute(
        "SELECT id,status,progress,message,error,url,title,track_num FROM tidal_jobs WHERE id=?",
        (job_id,)
    ).fetchone()
    db.close()
    if not row:
        return {}
    return {
        "id": row[0], "status": row[1], "progress": row[2],
        "message": row[3], "error": row[4], "url": row[5], "title": row[6],
        "track_num": row[7]
    }


# ── Auth ─────────────────────────────────────────────────────────────────────

def get_auth_status():
    """Check if tiddl is authenticated and token is valid."""
    try:
        if not AUTH_FILE.exists():
            return {"authenticated": False, "reason": "not_logged_in"}

        data = json.loads(AUTH_FILE.read_text())
        token = data.get("token")
        expires_at = data.get("expires_at", 0)

        if not token:
            return {"authenticated": False, "reason": "no_token"}

        if expires_at and time.time() > expires_at:
            refresh = data.get("refresh_token")
            if refresh:
                return {"authenticated": False, "reason": "token_expired_refreshable"}
            return {"authenticated": False, "reason": "token_expired"}

        return {
            "authenticated": True,
            "user_id": data.get("user_id"),
            "country_code": data.get("country_code"),
            "expires_at": expires_at,
        }
    except Exception as e:
        return {"authenticated": False, "reason": str(e)}


def start_device_auth():
    from tiddl.core.auth import AuthAPI

    auth_api = AuthAPI()
    device_auth = auth_api.get_device_auth()

    return {
        "device_code": device_auth.deviceCode,
        "user_code": device_auth.userCode,
        "verification_uri": f"https://{device_auth.verificationUriComplete}",
        "expires_in": device_auth.expiresIn,
        "interval": device_auth.interval,
    }


def poll_device_auth(device_code):
    from tiddl.core.auth import AuthAPI, AuthClientError

    auth_api = AuthAPI()
    try:
        auth = auth_api.get_auth(device_code)

        auth_data = {
            "token": auth.access_token,
            "refresh_token": auth.refresh_token,
            "expires_at": auth.expires_in + int(time.time()),
            "user_id": str(auth.user_id),
            "country_code": auth.user.countryCode,
        }
        AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        AUTH_FILE.write_text(json.dumps(auth_data))

        return True, {"user_id": str(auth.user_id), "country_code": auth.user.countryCode}

    except AuthClientError as e:
        if e.error == "authorization_pending":
            return False, {"status": "pending"}
        if e.error == "expired_token":
            return False, {"status": "expired", "error": "Authorization timed out"}
        return False, {"status": "error", "error": str(e)}
    except Exception as e:
        return False, {"status": "error", "error": str(e)}


def refresh_auth():
    try:
        if not AUTH_FILE.exists():
            return False

        data = json.loads(AUTH_FILE.read_text())
        refresh_token = data.get("refresh_token")
        if not refresh_token:
            return False

        from tiddl.core.auth import AuthAPI

        auth_api = AuthAPI()
        auth = auth_api.refresh_token(refresh_token)

        data["token"] = auth.access_token
        data["expires_at"] = auth.expires_in + int(time.time())
        AUTH_FILE.write_text(json.dumps(data))

        return True
    except Exception:
        return False


def logout():
    if AUTH_FILE.exists():
        try:
            data = json.loads(AUTH_FILE.read_text())
            if data.get("token"):
                from tiddl.core.auth import AuthAPI
                auth_api = AuthAPI()
                auth_api.logout_token(data["token"])
        except Exception:
            pass
        AUTH_FILE.unlink()
    return True


# ── TidalAPI helper ──────────────────────────────────────────────────────────

def _get_api(use_cache=True):
    """Create a TidalAPI instance from saved auth.
    
    use_cache=False for download threads to avoid SQLite locking issues.
    """
    if not AUTH_FILE.exists():
        raise RuntimeError("Not authenticated")

    data = json.loads(AUTH_FILE.read_text())
    token = data.get("token")
    user_id = str(data.get("user_id", ""))
    country_code = data.get("country_code", "")

    if not token:
        raise RuntimeError("No auth token")

    from tiddl.core.api import TidalAPI, TidalClient

    client = TidalClient(
        token=token,
        cache_name=f"{TIDDL_PATH}/api_cache",
        omit_cache=not use_cache,
        on_token_expiry=None,
    )

    return TidalAPI(client=client, user_id=user_id, country_code=country_code)


def _parse_tidal_url(url):
    """Extract resource type and ID from a Tidal URL."""
    # https://tidal.com/album/496642361
    # https://tidal.com/track/12345
    # https://tidal.com/playlist/uuid
    match = re.match(r'https?://(?:www\.)?tidal\.com/(\w+)/(\w+)', url)
    if match:
        return match.group(1), match.group(2)
    return None, None


# ── Download (direct Python API) ─────────────────────────────────────────────

CHUNK_SIZE = 1024 * 1024  # 1MB chunks for progress tracking


def _sanitize_vfat(filename):
    """Sanitize filename for VFAT/FAT32 filesystems.
    
    VFAT with iocharset=ascii cannot handle non-ASCII Unicode characters.
    Replace them with ASCII equivalents or remove them.
    Also strip characters illegal in FAT32: \\ / : * ? \" < > |
    """
    # Unicode normalization: try to decompose and keep ASCII only
    import unicodedata
    
    # First try NFKD decomposition to separate base char + combining marks
    normalized = unicodedata.normalize('NFKD', filename)
    # Keep only ASCII characters + basic safe chars (preserve path separators)
    ascii_only = ''.join(
        c for c in normalized 
        if ord(c) < 128 and c not in '\\:*?"<>|'
    )
    # If we lost too much, fall back to basic ASCII stripping
    if len(ascii_only) < len(filename) * 0.5:
        ascii_only = ''.join(
            c if ord(c) < 128 and c not in '\\:*?"<>|' else '_'
            for c in filename
        )
    return ascii_only


def start_download(url, dest_dir=""):
    job_id = str(uuid.uuid4())[:8]
    dest = str(Path(MOUNT_PATH) / dest_dir)

    db = _get_db()
    db.execute(
        "INSERT INTO tidal_jobs (id,status,progress,message,error,url,title,track_num,updated) VALUES (?,?,?,?,?,?,?,?,?)",
        (job_id, "starting", 0, "Starting Tidal download...", None, url, None, None, time.time())
    )
    db.commit()
    db.close()

    thread = threading.Thread(target=_run_download, args=(job_id, url, dest), daemon=True)
    thread.start()
    return job_id


def _run_download(job_id, url, dest):
    """Download from Tidal using the Python API directly with chunk-level progress."""

    def update(**kwargs):
        _set_job(job_id, **kwargs)

    try:
        # Refresh token if needed
        auth_status = get_auth_status()
        if auth_status.get("reason") == "token_expired_refreshable":
            if not refresh_auth():
                update(status="error", message="Token expired", error="Tidal token expired and refresh failed.")
                return

        update(status="connecting", progress=0, message="Connecting to Tidal...")

        # Use cached API for initial metadata, uncached for the download thread
        api = _get_api(use_cache=True)
        resource_type, resource_id = _parse_tidal_url(url)

        if not resource_type or not resource_id:
            update(status="error", message="Invalid URL", error=f"Could not parse Tidal URL: {url}")
            return

        # Create a separate uncached API client for the download thread
        # to avoid SQLite cache locking issues with requests_cache
        dl_api = _get_api(use_cache=False)

        if resource_type == "album":
            _download_album(job_id, api, dl_api, resource_id, dest, update)
        elif resource_type == "track":
            _download_track(job_id, dl_api, resource_id, dest, update, track_index=1, total_tracks=1)
        elif resource_type == "playlist":
            _download_playlist(job_id, api, dl_api, resource_id, dest, update)
        else:
            update(status="error", message="Unsupported type", error=f"Resource type '{resource_type}' is not supported")

    except Exception as e:
        update(status="error", message="Unexpected error", error=str(e))


def _download_file_chunked(urls, dest_path, update, label=""):
    """Download from URL list to file with chunk-level progress tracking.
    Returns the total bytes written."""
    import requests

    total_bytes = 0
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    with NamedTemporaryFile("wb", delete=False, dir=dest_path.parent, suffix=dest_path.suffix) as tmp:
        tmp_name = tmp.name
        for url in urls:
            with requests.get(url, stream=True) as resp:
                resp.raise_for_status()
                total_size = int(resp.headers.get('content-length', 0))
                for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                    if chunk:
                        tmp.write(chunk)
                        total_bytes += len(chunk)
        tmp.flush()

    shutil.move(tmp_name, dest_path)
    return total_bytes


def _download_track(job_id, api, track_id, dest, update, track_index=1, total_tracks=1,
                    album=None, track_num_str=None):
    """Download a single track using the Python API with chunk-level progress."""

    from tiddl.core.utils.parse import parse_track_stream
    from tiddl.core.metadata import add_track_metadata, Cover
    from tiddl.core.utils.format import format_template

    # Set album title as job title once we know it
    if album:
        album_title = f"{album.artist.name if album.artist else ''} - {album.title}" if album.artist else album.title
        update(title=album_title)

    # Get track info
    track = api.get_track(track_id)
    track_label = track.title

    if not track.allowStreaming:
        update(message=f"⚠️ Cannot stream: {track_label}")
        return False

    # Get album for template formatting if not provided
    if not album:
        try:
            album = api.get_album(track.album.id)
        except Exception:
            album = None

    # Get stream
    try:
        stream = api.get_track_stream(track_id=track.id, quality="HIGH")
    except Exception as e:
        update(message=f"❌ Stream error for {track_label}: {e}")
        return False

    urls, file_extension = parse_track_stream(stream)
    quality_str = stream.audioQuality

    # Build output path using template
    template = "{album.artist} - {album.title}/{item.number:02d} - {item.title}"
    try:
        relative_path = format_template(
            template=template,
            item=track,
            album=album,
            quality="high",
            with_asterisk_ext=False,
        )
    except Exception:
        relative_path = f"{track.title}"

    dest_path = Path(dest) / f"{relative_path}{file_extension}"
    # Sanitize each path segment (not the root/mount prefix) for VFAT compatibility
    # Build path manually so we don't break the absolute root
    safe_segments = []
    for segment in Path(f"{relative_path}{file_extension}").parts:
        safe_segments.append(_sanitize_vfat(segment))
    dest_path = Path(dest).joinpath(*safe_segments)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    # Check if file already exists
    if dest_path.exists():
        update(message=f"⏭️ Exists: {track_label}")
        return True

    # Build progress message
    if total_tracks > 1:
        progress_msg = f"Track {track_index}/{total_tracks}: {track_label}"
        overall_pct = int(((track_index - 1) / total_tracks) * 100)
        track_num = f"{track_index}/{total_tracks}"
    else:
        progress_msg = f"Downloading: {track_label}"
        overall_pct = 0
        track_num = "1/1"

    update(
        status="downloading",
        message=progress_msg,
        progress=overall_pct,
        track_num=track_num,
    )

    # Download with chunk tracking
    bytes_written = _download_file_chunked(urls, dest_path, update, label=track_label)

    # Update progress for this track completion
    if total_tracks > 1:
        done_pct = int((track_index / total_tracks) * 100)
        update(
            progress=done_pct,
            message=f"✅ Track {track_index}/{total_tracks}: {track_label}",
            track_num=f"{track_index}/{total_tracks}",
        )
    else:
        update(progress=100, message=f"✅ {track_label}")

    # Add metadata
    try:
        cover_data = None
        if track.album and track.album.cover:
            cover = Cover(track.album.cover)
            cover.fetch_data()
            cover_data = cover.data if cover.data else None

        add_track_metadata(
            path=dest_path,
            track=track,
            date=str(album.releaseDate) if album and album.releaseDate else "",
            album_artist=album.artist.name if album and album.artist else "",
            cover_data=cover_data,
        )
    except Exception:
        pass  # Metadata failure is non-fatal

    return True


def _download_album(job_id, api, dl_api, album_id, dest, update):
    """Download all tracks from an album."""
    album = api.get_album(album_id)
    total_items = None
    all_tracks = []
    offset = 0

    # First pass: collect all track items to know the total
    update(status="connecting", message=f"Fetching album info: {album.title}...", title=album.title)

    while True:
        items = api.get_album_items_credits(album_id=album_id, offset=offset)
        for item in items.items:
            all_tracks.append(item.item)
        offset += items.limit
        if offset >= items.totalNumberOfItems:
            total_items = items.totalNumberOfItems
            break

    total = len(all_tracks)
    update(message=f"Album: {album.title} ({total} tracks)", track_num=f"0/{total}")

    downloaded = 0
    for i, track in enumerate(all_tracks, 1):
        try:
            ok = _download_track(
                job_id, dl_api, track.id, dest, update,
                track_index=i, total_tracks=total, album=album,
            )
            if ok:
                downloaded += 1
        except Exception as e:
            update(message=f"❌ Track {i}/{total} failed: {e}")

    update(
        status="done",
        progress=100,
        message=f"✅ Album complete: {downloaded}/{total} tracks downloaded",
        track_num=f"{total}/{total}",
    )


def _download_playlist(job_id, api, dl_api, playlist_id, dest, update):
    """Download all tracks from a playlist."""
    playlist = api.get_playlist(playlist_uuid=playlist_id)
    all_tracks = []
    offset = 0

    update(status="connecting", message=f"Fetching playlist: {playlist.title}...", title=playlist.title)

    while True:
        items = api.get_playlist_items(playlist_uuid=playlist_id, offset=offset)
        for item in items.items:
            all_tracks.append(item.item)
        offset += items.limit
        if offset >= items.totalNumberOfItems:
            break

    total = len(all_tracks)
    update(message=f"Playlist: {playlist.title} ({total} tracks)", track_num=f"0/{total}")

    downloaded = 0
    for i, track in enumerate(all_tracks, 1):
        try:
            ok = _download_track(
                job_id, dl_api, track.id, dest, update,
                track_index=i, total_tracks=total,
            )
            if ok:
                downloaded += 1
        except Exception as e:
            update(message=f"❌ Track {i}/{total} failed: {e}")

    update(
        status="done",
        progress=100,
        message=f"✅ Playlist complete: {downloaded}/{total} tracks downloaded",
        track_num=f"{total}/{total}",
    )