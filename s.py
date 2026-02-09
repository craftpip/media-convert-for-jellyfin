import os
import json
import subprocess
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()  # loads .env from current directory

BITRATE_TOLERANCE = int(os.getenv("BITRATE_TOLERANCE", 300))
TARGET_BR = int(os.getenv("TARGET_BR", 3000))
MAX_BR = int(os.getenv("MAX_BR", 3300))

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".webm", ".ts", ".flv", ".wmv"}
H264_OK_CONTAINERS = {".mkv", ".mp4", ".mov", ".ts", ".flv", ".avi"}
TMP_TAG = ".__transcoding__"


def ffprobe_json(file, entries):
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", entries,
        "-of", "json",
        str(file)
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def get_estimated_bitrate_kbps(file: Path):
    data = ffprobe_json(file, "format=duration,size,bit_rate")
    if not data or "format" not in data:
        return None

    fmt = data["format"]

    if fmt.get("bit_rate"):
        try:
            return int(fmt["bit_rate"]) // 1000
        except Exception:
            pass

    try:
        duration = float(fmt.get("duration", 0))
        size = int(fmt.get("size", 0))
        if duration <= 0 or size <= 0:
            return None
        return int((size * 8) / duration / 1000)
    except Exception:
        return None


def should_convert(file: Path):
    ext = file.suffix.lower()

    if ext not in VIDEO_EXTS:
        return False, "not_video"

    if TMP_TAG in file.name:
        return False, "temp_file"

    if ext not in H264_OK_CONTAINERS:
        return False, f"container_not_ok({ext})"

    br = get_estimated_bitrate_kbps(file)
    if br is None:
        return False, "bitrate_unknown"

    if br <= MAX_BR + BITRATE_TOLERANCE:
        return False, f"ok({br}kbps)"

    return True, f"convert({br}kbps)"


def scan(root: Path):
    for p in root.rglob("*"):
        if not p.is_file():
            continue

        yes, reason = should_convert(p)
        if yes:
            print(f"{p}  ->  {reason}")


if __name__ == "__main__":
    import sys

    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").expanduser().resolve()

    if not root.is_dir():
        print(f"Not a directory: {root}")
        sys.exit(1)

    scan(root)
