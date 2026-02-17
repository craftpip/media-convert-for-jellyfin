import sys
import os
import json
import subprocess
import argparse
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


def get_audio_track_channels(file: Path):
    data = ffprobe_json(file, "stream=index,codec_type,channels")
    if not data:
        return []

    channels = []
    for stream in data.get("streams", []):
        if stream.get("codec_type") != "audio":
            continue
        ch = stream.get("channels")
        try:
            channels.append(int(ch))
        except (TypeError, ValueError):
            channels.append(None)
    return channels


def should_report(file: Path, max_bitrate_kbps: int):
    ext = file.suffix.lower()

    if ext not in VIDEO_EXTS:
        return False, None, []

    if TMP_TAG in file.name:
        return False, None, []

    br = get_estimated_bitrate_kbps(file)
    audio_channels = get_audio_track_channels(file)

    high_bitrate = br is not None and br > max_bitrate_kbps
    has_multichannel_audio = any(ch is not None and ch > 2 for ch in audio_channels)

    return high_bitrate or has_multichannel_audio, br, audio_channels


def format_size(num_bytes: int):
    mib = num_bytes / (1024 * 1024)
    return f"{num_bytes}B ({mib:.2f}MiB)"


def scan(root: Path, max_bitrate_kbps: int):
    for p in root.rglob("*"):
        if not p.is_file():
            continue

        yes, bitrate_kbps, audio_channels = should_report(p, max_bitrate_kbps)
        if yes:
            tracks_text = ", ".join(f"{c}ch" if c is not None else "unknown" for c in audio_channels)
            if not tracks_text:
                tracks_text = "none"

            bitrate_text = f"{bitrate_kbps}kbps" if bitrate_kbps is not None else "unknown"
            size_text = format_size(p.stat().st_size)
            print(
                f"{p} | bitrate={bitrate_text} | audio_tracks={len(audio_channels)} ({tracks_text}) | size={size_text}"
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--bitrate",
        type=int,
        default=MAX_BR,
        help="Max allowed bitrate in kbps"
    )
    parser.add_argument("dir", nargs="?", default=".", help="Directory to scan recursively")

    args = parser.parse_args()
    root = Path(args.dir).expanduser().resolve()

    if not root.is_dir():
        print(f"Not a directory: {root}")
        sys.exit(1)

    scan(root, args.bitrate)
