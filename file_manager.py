import os
import shutil
from pathlib import Path

MOUNT_PATH = "/media/shokz"

def get_mount_path():
    return MOUNT_PATH

def is_mounted():
    # Check if mount point is actually accessible (device really present)
    if not os.path.ismount(MOUNT_PATH):
        return False
    try:
        os.listdir(MOUNT_PATH)
        return True
    except OSError:
        # Mount point exists but device is gone (stale mount)
        try:
            import subprocess
            subprocess.run(['umount', '-l', MOUNT_PATH], capture_output=True)
        except Exception:
            pass
        return False

def list_directory(rel_path=""):
    base = Path(MOUNT_PATH)
    target = (base / rel_path).resolve()
    # Security: ensure we stay within mount
    if not str(target).startswith(str(base)):
        raise PermissionError("Access denied")
    if not target.exists():
        raise FileNotFoundError(f"Path not found: {rel_path}")

    HIDDEN = {'SYSTEM', 'System Volume Information'}
    entries = []
    for item in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        if item.name in HIDDEN:
            continue
        entries.append({
            "name": item.name,
            "type": "dir" if item.is_dir() else "file",
            "size": item.stat().st_size if item.is_file() else None,
            "path": str(item.relative_to(base))
        })
    return entries

def make_directory(rel_path):
    base = Path(MOUNT_PATH)
    target = (base / rel_path).resolve()
    if not str(target).startswith(str(base)):
        raise PermissionError("Access denied")
    target.mkdir(parents=True, exist_ok=True)

def delete_item(rel_path):
    base = Path(MOUNT_PATH)
    target = (base / rel_path).resolve()
    if not str(target).startswith(str(base)):
        raise PermissionError("Access denied")
    if not target.exists():
        raise FileNotFoundError(f"Not found: {rel_path}")
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()

def rename_item(rel_path, new_name):
    base = Path(MOUNT_PATH)
    target = (base / rel_path).resolve()
    if not str(target).startswith(str(base)):
        raise PermissionError("Access denied")
    if not target.exists():
        raise FileNotFoundError(f"Not found: {rel_path}")
    # Sanitize new_name
    new_name = Path(new_name).name
    new_path = target.parent / new_name
    target.rename(new_path)

def move_item(rel_src, rel_dst_dir):
    base = Path(MOUNT_PATH)
    src = (base / rel_src).resolve()
    dst_dir = (base / rel_dst_dir).resolve()
    if not str(src).startswith(str(base)) or not str(dst_dir).startswith(str(base)):
        raise PermissionError("Access denied")
    if not src.exists():
        raise FileNotFoundError(f"Source not found: {rel_src}")
    dst = dst_dir / src.name
    shutil.move(str(src), str(dst))
