import os
import json
import time
import shutil
import argparse
import subprocess
import logging
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()  # loads .env from current directory

BITRATE_TOLERANCE = int(os.getenv("BITRATE_TOLERANCE", 300))
TARGET_BR = int(os.getenv("TARGET_BR", 3000))
MAX_BR = int(os.getenv("MAX_BR", 3300))
BUFSIZE = int(os.getenv("BUFSIZE", 6000))
DEFAULT_CRF = int(os.getenv("DEFAULT_CRF", 23))
TRANSCODER = 'h264_nvenc'

DONE_FILE = Path(__file__).parent / "done.txt"

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".webm", ".ts", ".flv", ".wmv"}
TMP_TAG = ".__transcoding__"  # temp name keeps same container (ext at end)

H264_OK_CONTAINERS = {".mkv", ".mp4", ".mov", ".ts", ".flv", ".avi"}
TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text"}

log = logging.getLogger("transcode")


def load_done() -> dict[str, str]:
    if not DONE_FILE.exists():
        return {}
    entries = {}
    for line in DONE_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" not in line:
            continue
        path, opt = line.split("|", 1)
        entries[path] = opt
    return entries


def save_done(done: dict[str, str]) -> None:
    DONE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with DONE_FILE.open("w", encoding="utf-8") as f:
        for path, opt in done.items():
            f.write(f"{path}|{opt}\n")


def setup_logging(log_file: Path | None, verbose: bool):
    log.setLevel(logging.DEBUG)

    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%Y-%m-%d %H:%M:%S")

    sh = logging.StreamHandler()
    sh.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh.setFormatter(fmt)
    log.addHandler(sh)

    if log_file:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(fmt)
            log.addHandler(fh)
        except Exception as e:
            log.warning(f"Could not create log file '{log_file}': {e}")


def run_quiet(cmd):
    return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def ffprobe_json(file, entries):
    cmd = ["ffprobe", "-v", "error", "-show_entries", entries, "-of", "json", str(file)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def get_estimated_bitrate_kbps(file):
    data = ffprobe_json(file, "format=duration,size,bit_rate")
    if not data or "format" not in data:
        return None

    fmt = data["format"]
    br = fmt.get("bit_rate")
    if br:
        try:
            return int(br) // 1000
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


def probe_streams(file):
    data = ffprobe_json(file, "stream=index,codec_type,codec_name")
    if not data or "streams" not in data:
        return None

    vids, auds, subs = [], [], []
    for s in data["streams"]:
        st = s.get("codec_type")
        idx = s.get("index")
        codec = (s.get("codec_name") or "").lower()
        if idx is None:
            continue
        if st == "video":
            vids.append(idx)
        elif st == "audio":
            auds.append(idx)
        elif st == "subtitle":
            subs.append((idx, codec))
    return vids, auds, subs


def tmp_name_for(file: Path) -> Path:
    return file.with_name(file.stem + TMP_TAG + file.suffix)


def cleanup_tmp_files(root: Path):
    deleted = 0
    for p in root.rglob(f"*{TMP_TAG}*"):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            try:
                p.unlink()
                deleted += 1
            except Exception:
                pass
    if deleted:
        log.info(f"Cleanup: deleted {deleted} leftover temp file(s)")


def safe_replace(src: Path, dst: Path):
    for _ in range(12):
        try:
            os.replace(str(src), str(dst))
            return True
        except PermissionError:
            time.sleep(0.25)
        except FileNotFoundError:
            return False
        except Exception:
            time.sleep(0.25)
    return False


def build_ffmpeg_cmd(in_file: Path, out_file: Path, crf: int | None = None):
    ext = in_file.suffix.lower()

    streams = probe_streams(in_file)
    if not streams:
        return None, "ffprobe_failed"

    vids, auds, subs = streams

    maps = []
    for i in vids:
        maps += ["-map", f"0:{i}"]
    for i in auds:
        maps += ["-map", f"0:{i}"]

    sub_mode = "none"
    if ext == ".mkv":
        for idx, _codec in subs:
            maps += ["-map", f"0:{idx}"]
        sub_mode = "copy"
    elif ext in {".mp4", ".mov"}:
        for idx, codec in subs:
            if codec in TEXT_SUB_CODECS:
                maps += ["-map", f"0:{idx}"]
        sub_mode = "mov_text"

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(in_file),
        *maps,
    ]

    if crf is not None:
        cmd += [
            "-c:v", f"{TRANSCODER}",
            "-preset", "p4",
            "-profile:v", "high",
            "-crf", str(crf),
            "-pix_fmt", "yuv420p",
            "-c:a", "copy",
            "-max_muxing_queue_size", "1024",
        ]
    else:
        cmd += [
            "-c:v", f"{TRANSCODER}",
            "-preset", "p4",
            "-profile:v", "high",
            "-b:v", f"{TARGET_BR}k",
            "-maxrate", f"{MAX_BR}k",
            "-bufsize", f"{BUFSIZE}k",
            "-pix_fmt", "yuv420p",
            "-c:a", "copy",
            "-max_muxing_queue_size", "1024",
        ]

    if sub_mode == "copy":
        cmd += ["-c:s", "copy"]
    elif sub_mode == "mov_text":
        cmd += ["-c:s", "mov_text"]
    else:
        cmd += ["-sn"]

    cmd += [str(out_file)]
    return cmd, None


def convert_if_needed(file: Path, crf: int | None = None):
    ext = file.suffix.lower()

    if ext not in H264_OK_CONTAINERS:
        return "SKIP", f"container_not_h264_ok({ext})"

    br = get_estimated_bitrate_kbps(file)
    log.info(f"Bitrate: {br} kbps")

    if crf is None:
        if br is None:
            return "SKIP", "bitrate_unknown"

        if br <= MAX_BR + BITRATE_TOLERANCE:
            return "SKIP", f"ok({br}kbps)"

    tmp = tmp_name_for(file)

    if tmp.exists():
        try:
            tmp.unlink()
        except Exception:
            return "FAIL", "tmp_cleanup_failed"

    cmd, err = build_ffmpeg_cmd(file, tmp, crf)
    if err:
        return "FAIL", err
    if not cmd:
        return "FAIL", "cmd_build_failed"

    r = run_quiet(cmd)

    if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return "FAIL", "ffmpeg_failed"

    if not safe_replace(tmp, file):
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return "FAIL", "replace_failed"

    mode = f"crf({crf})" if crf is not None else f"converted({br}kbps)"
    return "OK", mode


def main(scan_dir: str, crf: int | None = None):
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        log.error("FATAL: ffmpeg/ffprobe not found in PATH")
        return

    root = Path(scan_dir).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        log.error(f"FATAL: not a directory: {root}")
        return

    log.info(f"Scanning: {root}")
    cleanup_tmp_files(root)

    done = load_done()

    files = []
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS and TMP_TAG not in p.name:
            files.append(p)

    total = len(files)
    log.info(f"Found {total} file(s)")

    ok = skip = fail = 0
    for i, f in enumerate(files, 1):
        path = f.resolve().as_posix()
        current_opt = f"crf:{crf}" if crf else f"br:{TARGET_BR}"
        key = f"{path}|{current_opt}"

        if done.get(path) == current_opt:
            skip += 1
            log.info(f"[{i}/{total}] SKIP: {f} :: already_done")
            continue

        log.info(f"[{i}/{total}] Checking: {f}")
        status, msg = convert_if_needed(f, crf)
        if status == "OK":
            ok += 1
            done[path] = current_opt
            save_done(done)
            log.info(f"[{i}/{total}] OK: {f} :: {msg}")
        elif status == "SKIP":
            skip += 1
            log.info(f"[{i}/{total}] SKIP: {f} :: {msg}")
        else:
            fail += 1
            log.error(f"[{i}/{total}] FAIL: {f} :: {msg}")

    log.info(f"Done. OK={ok} SKIP={skip} FAIL={fail}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--bitrate",
        type=int,
        help="Override max video bitrate (kbps)"
    )
    parser.add_argument(
        "--crf",
        type=int,
        help="Use CRF mode with this value (lower = better quality, 23 is default)"
    )
    parser.add_argument("dir", nargs="?", default=".", help="Directory to scan recursively")
    parser.add_argument("--log-file", default="", help="Path to log file (optional)")
    parser.add_argument("--use", default="nvidia", help="More logs (debug)")
    parser.add_argument("--verbose", action="store_true", help="More logs (debug)")
    args = parser.parse_args()

    log_path = Path(args.log_file).expanduser().resolve() if args.log_file else None
    setup_logging(log_path, args.verbose)

    if args.bitrate:
        MAX_BR = args.bitrate
        TARGET_BR = MAX_BR - BITRATE_TOLERANCE
        BUFSIZE = MAX_BR * 2

    crf = args.crf if args.crf else None

    if args.use:
        if args.use == 'nvidiagpu':
            TRANSCODER = 'h264_nvenc'
        if args.use == 'amdgpu':
            TRANSCODER = 'h264_amf'
        if args.use == 'intel':
            TRANSCODER = 'hevc_qsv'
        if args.use == 'cpu':
            TRANSCODER = 'libx265'

    print(f'Using transcoder: {TRANSCODER}')

    try:
        main(args.dir, crf)
    except KeyboardInterrupt:
        log.warning("Interrupted.")
