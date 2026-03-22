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
TRANSCODER = "h264_nvenc"
TRANSCODER_PROFILE = "nvidia"
FFMPEG_CMD = "ffmpeg"
FFPROBE_CMD = "ffprobe"
AVAILABLE_ENCODERS: set[str] | None = None

DONE_FILE = Path(__file__).parent / "done.txt"
SIZE_REPORT_FILE = Path(__file__).parent / "size-report.csv"

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".webm", ".ts", ".flv", ".wmv"}
TMP_TAG = ".__transcoding__"  # temp name keeps same container (ext at end)

H264_OK_CONTAINERS = {".mkv", ".mp4", ".mov", ".ts", ".flv", ".avi"}
TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text"}
ALLOWED_AUDIO_LANGS = {
    "eng", "en", "english",
    "jpn", "ja", "japanese",
    "hin", "hi", "hindi",
}

log = logging.getLogger("transcode")

TRANSCODER_ALIASES = {
    "nvidia": "nvidia",
    "nvidiagpu": "nvidia",
    "nvenc": "nvidia",
    "h264_nvenc": "nvidia",
    "amdgpu": "amdgpu",
    "amd": "amdgpu",
    "amf": "amdgpu",
    "h264_amf": "amdgpu",
    "intel": "intel",
    "intelgpu": "intel",
    "qsv": "intel",
    "h264_qsv": "intel",
    "hevc_qsv": "intel",
    "cpu": "cpu",
    "software": "cpu",
    "x264": "cpu",
    "libx264": "cpu",
    "x265": "cpu",
    "libx265": "cpu",
    "vaapi": "vaapi",
    "h264_vaapi": "vaapi",
}

TRANSCODER_ENCODERS = {
    "nvidia": "h264_nvenc",
    "amdgpu": "h264_amf",
    "intel": "h264_qsv",
    "cpu": "libx264",
    "vaapi": "h264_vaapi",
}


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _discover_binary(prefix: str, env_var: str, exact_names: tuple[str, ...]) -> str | None:
    override = os.getenv(env_var, "").strip()
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_absolute():
            return str(candidate) if _is_executable(candidate) else None
        resolved = shutil.which(override)
        if resolved:
            return resolved

    for name in exact_names:
        resolved = shutil.which(name)
        if resolved:
            return resolved

    path_dirs = [Path(p) for p in os.getenv("PATH", "").split(os.pathsep) if p]
    discovered = []
    for d in path_dirs:
        if not d.is_dir():
            continue
        try:
            for item in d.iterdir():
                low = item.name.lower()
                if not low.startswith(prefix):
                    continue
                if _is_executable(item):
                    discovered.append(item)
        except Exception:
            continue

    if not discovered:
        return None

    discovered.sort(key=lambda p: (len(p.name), p.name))
    return str(discovered[0])


def resolve_ffmpeg_tools() -> bool:
    global FFMPEG_CMD, FFPROBE_CMD

    ffmpeg = _discover_binary("ffmpeg", "FFMPEG_BIN", ("ffmpeg", "ffmpeg.exe"))
    ffprobe = _discover_binary("ffprobe", "FFPROBE_BIN", ("ffprobe", "ffprobe.exe"))
    if not ffmpeg or not ffprobe:
        return False

    FFMPEG_CMD = ffmpeg
    FFPROBE_CMD = ffprobe
    return True


def get_available_encoders() -> set[str]:
    global AVAILABLE_ENCODERS
    if AVAILABLE_ENCODERS is not None:
        return AVAILABLE_ENCODERS

    cmd = [FFMPEG_CMD, "-hide_banner", "-encoders"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    encoders: set[str] = set()
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("-") or line.startswith("Encoders:"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                encoders.add(parts[1].lower())
    AVAILABLE_ENCODERS = encoders
    return AVAILABLE_ENCODERS


def resolve_transcoder(use_value: str):
    global TRANSCODER, TRANSCODER_PROFILE

    requested = (use_value or "nvidia").strip().lower()
    profile = TRANSCODER_ALIASES.get(requested)
    encoders = get_available_encoders()

    if profile:
        encoder = TRANSCODER_ENCODERS[profile]
        if encoder not in encoders:
            return False, f"encoder_not_supported({encoder})"
        TRANSCODER = encoder
        TRANSCODER_PROFILE = profile
        return True, None

    # Allow passing a raw ffmpeg encoder name directly (e.g. --use h264_nvenc).
    if requested in encoders:
        TRANSCODER = requested
        TRANSCODER_PROFILE = "custom"
        return True, None

    supported = ", ".join(sorted(TRANSCODER_ENCODERS.keys()))
    return False, f"unknown_transcoder({requested}); valid profiles: {supported}"


# ---------------------------------------------------------------------------
# Remote mode helpers
# ---------------------------------------------------------------------------

def parse_remote_target(remote: str) -> tuple[str, str]:
    """Parse 'user@host:/path' into (host, remote_dir)."""
    if ":" not in remote:
        raise ValueError("Remote must be in format user@host:/path")
    host, remote_dir = remote.split(":", 1)
    return host, remote_dir


def ssh_run(host: str, cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-o", "ConnectTimeout=10", host, cmd],
        capture_output=True, text=True, timeout=timeout,
    )


def scp_upload(local: Path, remote_dest: str, timeout: int = 600) -> bool:
    r = subprocess.run(
        ["scp", "-o", "ConnectTimeout=10", str(local), remote_dest],
        capture_output=True, text=True, timeout=timeout,
    )
    return r.returncode == 0


def scp_download(remote_src: str, local_dest: Path, timeout: int = 600) -> bool:
    r = subprocess.run(
        ["scp", "-o", "ConnectTimeout=10", remote_src, str(local_dest)],
        capture_output=True, text=True, timeout=timeout,
    )
    return r.returncode == 0


def ssh_mkdir(host: str, remote_dir: str) -> bool:
    r = ssh_run(host, f"mkdir -p '{remote_dir}'")
    return r.returncode == 0


def ssh_ls(host: str, remote_dir: str) -> bool:
    r = ssh_run(host, f"ls -1 '{remote_dir}'")
    return r.returncode == 0


def list_remote_videos(host: str, remote_dir: str) -> list[str]:
    """List video files on remote via SSH find."""
    ext_pattern = " -o ".join(f'-name "*{ext}"' for ext in VIDEO_EXTS)
    cmd = f"find '{remote_dir}' -type f \\( {ext_pattern} \\) | sort"
    r = ssh_run(host, cmd, timeout=120)
    if r.returncode != 0:
        return []
    files = [line.strip() for line in r.stdout.splitlines() if line.strip()]
    return [f for f in files if TMP_TAG not in f]


def remote_file_size(host: str, remote_path: str) -> int | None:
    r = ssh_run(host, f"stat -c%s '{remote_path}'")
    if r.returncode == 0:
        try:
            return int(r.stdout.strip())
        except ValueError:
            pass
    return None


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


def append_size_report(rows: list[tuple[str, int, int]]) -> None:
    if not rows:
        return

    need_header = True
    if SIZE_REPORT_FILE.exists() and SIZE_REPORT_FILE.stat().st_size > 0:
        need_header = False

    with SIZE_REPORT_FILE.open("a", encoding="utf-8") as f:
        if need_header:
            f.write("name,before,after\n")
        for name, before, after in rows:
            f.write(f"{name},{before},{after}\n")


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
    cmd = [FFPROBE_CMD, "-v", "error", "-show_entries", entries, "-of", "json", str(file)]
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
    data = ffprobe_json(file, "stream=index,codec_type,codec_name:stream_tags=language")
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
            tags = s.get("tags") or {}
            lang = (tags.get("language") or "und").strip().lower()
            auds.append((idx, lang))
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


def build_ffmpeg_cmd(in_file: Path, out_file: Path, crf: int | None = None, verbose: bool = False):
    ext = in_file.suffix.lower()

    streams = probe_streams(in_file)
    if not streams:
        return None, "ffprobe_failed"

    vids, auds, subs = streams

    maps = []
    for i in vids:
        maps += ["-map", f"0:{i}"]
    for i, lang in auds:
        if lang in ALLOWED_AUDIO_LANGS:
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

    loglevel = "info" if verbose else "error"
    cmd = [
        FFMPEG_CMD, "-hide_banner", "-loglevel", loglevel, "-y",
        "-i", str(in_file),
        *maps,
    ]

    if TRANSCODER_PROFILE == "nvidia":
        cmd += ["-preset", "p4", "-pix_fmt", "yuv420p"]
    elif TRANSCODER_PROFILE == "amdgpu":
        cmd += ["-usage", "transcoding", "-quality", "balanced", "-pix_fmt", "yuv420p"]
    elif TRANSCODER_PROFILE == "intel":
        cmd += ["-preset", "medium", "-pix_fmt", "yuv420p"]
    elif TRANSCODER_PROFILE == "cpu":
        cmd += ["-preset", "medium", "-pix_fmt", "yuv420p"]
    elif TRANSCODER_PROFILE == "vaapi" or TRANSCODER.endswith("_vaapi"):
        cmd += ["-vaapi_device", "/dev/dri/renderD128", "-vf", "format=nv12,hwupload"]

    if crf is not None:
        cmd += [
            "-c:v", f"{TRANSCODER}",
            "-crf", str(crf),
            "-c:a", "aac",
            "-ac:a", "2",
            "-max_muxing_queue_size", "1024",
        ]
    else:
        cmd += [
            "-c:v", f"{TRANSCODER}",
            "-b:v", f"{TARGET_BR}k",
            "-maxrate", f"{MAX_BR}k",
            "-bufsize", f"{BUFSIZE}k",
            "-c:a", "aac",
            "-ac:a", "2",
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


def convert_if_needed(file: Path, crf: int | None = None, verbose: bool = False):
    ext = file.suffix.lower()

    if ext not in H264_OK_CONTAINERS:
        return "SKIP", f"container_not_h264_ok({ext})", None

    br = get_estimated_bitrate_kbps(file)
    log.info(f"Bitrate: {br} kbps")

    if crf is None:
        if br is None:
            return "SKIP", "bitrate_unknown", None

        if br <= MAX_BR + BITRATE_TOLERANCE:
            return "SKIP", f"ok({br}kbps)", None

    tmp = tmp_name_for(file)

    if tmp.exists():
        try:
            tmp.unlink()
        except Exception:
            return "FAIL", "tmp_cleanup_failed", None

    cmd, err = build_ffmpeg_cmd(file, tmp, crf, verbose)
    if err:
        return "FAIL", err, None
    if not cmd:
        return "FAIL", "cmd_build_failed", None
    if log.isEnabledFor(logging.DEBUG):
        log.info(f"Command: {' '.join(cmd)}")

    try:
        if verbose:
            r = subprocess.run(cmd, check=True)
        else:
            r = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        if hasattr(e, 'stderr') and e.stderr:
            return "FAIL", f"ffmpeg_failed: {e.stderr}", None
        return "FAIL", f"ffmpeg_failed: exit_code={e.returncode}", None
    if not tmp.exists() or tmp.stat().st_size == 0:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return "FAIL", "ffmpeg_failed", None

    before_size = file.stat().st_size

    if not safe_replace(tmp, file):
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return "FAIL", "replace_failed", None

    after_size = file.stat().st_size

    mode = f"crf({crf})" if crf is not None else f"converted({br}kbps)"
    return "OK", mode, (file.name, before_size, after_size)


def main(scan_dir: str, crf: int | None = None, verbose: bool = False):
    if not resolve_ffmpeg_tools():
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
    size_rows: list[tuple[str, int, int]] = []
    for i, f in enumerate(files, 1):
        path = f.resolve().as_posix()
        current_opt = f"crf:{crf}" if crf else f"br:{TARGET_BR}"
        key = f"{path}|{current_opt}"

        if done.get(path) == current_opt:
            skip += 1
            log.info(f"[{i}/{total}] SKIP: {f} :: already_done")
            continue

        log.info(f"[{i}/{total}] Checking: {f}")
        status, msg, size_row = convert_if_needed(f, crf, verbose)
        if status == "OK":
            ok += 1
            done[path] = current_opt
            save_done(done)
            if size_row:
                size_rows.append(size_row)
            log.info(f"[{i}/{total}] OK: {f} :: {msg}")
        elif status == "SKIP":
            skip += 1
            log.info(f"[{i}/{total}] SKIP: {f} :: {msg}")
        else:
            fail += 1
            log.error(f"[{i}/{total}] FAIL: {f} :: {msg}")

    append_size_report(size_rows)
    log.info(f"Done. OK={ok} SKIP={skip} FAIL={fail}")


def main_remote(host: str, remote_dir: str, local_tmp: Path, crf: int | None = None, verbose: bool = False):
    """Remote mode: download each file, convert locally with GPU, upload back."""
    local_tmp.mkdir(parents=True, exist_ok=True)

    if not ssh_ls(host, remote_dir):
        log.error(f"FATAL: remote path not accessible: {host}:{remote_dir}")
        return

    done = load_done()
    files = list_remote_videos(host, remote_dir)
    total = len(files)
    log.info(f"Remote scan: {total} file(s) on {host}:{remote_dir}")

    ok = skip = fail = 0
    size_rows: list[tuple[str, int, int]] = []

    for i, remote_file in enumerate(files, 1):
        current_opt = f"crf:{crf}" if crf else f"br:{TARGET_BR}"
        key = f"{remote_file}|{current_opt}"

        if done.get(remote_file) == current_opt:
            skip += 1
            log.info(f"[{i}/{total}] SKIP: {remote_file} :: already_done")
            continue

        local_file = local_tmp / Path(remote_file).name
        local_tmp_conv = tmp_name_for(local_file)

        log.info(f"[{i}/{total}] Downloading: {remote_file}")
        if not scp_download(f"{host}:{remote_file}", local_file):
            fail += 1
            log.error(f"[{i}/{total}] FAIL: {remote_file} :: download_failed")
            continue

        log.info(f"[{i}/{total}] Checking: {local_file}")
        status, msg, size_row = convert_if_needed(local_file, crf, verbose)

        if status == "OK":
            remote_dest_dir = str(Path(remote_file).parent)
            ssh_mkdir(host, remote_dest_dir)

            log.info(f"[{i}/{total}] Uploading converted file...")
            if scp_upload(local_file, f"{host}:{remote_file}"):
                ok += 1
                done[remote_file] = current_opt
                save_done(done)
                if size_row:
                    size_rows.append((Path(remote_file).name, size_row[1], size_row[2]))
                log.info(f"[{i}/{total}] OK: {remote_file} :: {msg}")
            else:
                fail += 1
                log.error(f"[{i}/{total}] FAIL: {remote_file} :: upload_failed")

            # cleanup local temp files
            try:
                local_file.unlink()
            except Exception:
                pass
            if local_tmp_conv.exists():
                try:
                    local_tmp_conv.unlink()
                except Exception:
                    pass

        elif status == "SKIP":
            skip += 1
            log.info(f"[{i}/{total}] SKIP: {remote_file} :: {msg}")
            try:
                local_file.unlink()
            except Exception:
                pass
        else:
            fail += 1
            log.error(f"[{i}/{total}] FAIL: {remote_file} :: {msg}")
            try:
                local_file.unlink()
            except Exception:
                pass
            if local_tmp_conv.exists():
                try:
                    local_tmp_conv.unlink()
                except Exception:
                    pass

    append_size_report(size_rows)
    log.info(f"Done. OK={ok} SKIP={skip} FAIL={fail}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--bitrate",
        type=int,
        help="Set target video bitrate (kbps); max bitrate becomes target + 1000 kbps"
    )
    parser.add_argument(
        "--crf",
        type=int,
        help="Use CRF mode with this value (lower = better quality, 23 is default)"
    )
    parser.add_argument("dir", nargs="?", default=".", help="Directory to scan recursively")
    parser.add_argument("--remote", default="", help="Remote target: user@host:/path")
    parser.add_argument("--local-tmp", default="", help="Local temp dir for remote mode (default: /tmp/convert-remote)")
    parser.add_argument("--log-file", default="", help="Path to log file (optional)")
    parser.add_argument("--use", default="nvidia", help="Transcoder profile or ffmpeg encoder name")
    parser.add_argument("--verbose", action="store_true", help="More logs (debug)")
    args = parser.parse_args()

    log_path = Path(args.log_file).expanduser().resolve() if args.log_file else None
    setup_logging(log_path, args.verbose)

    if args.bitrate:
        TARGET_BR = args.bitrate
        MAX_BR = TARGET_BR + 1000
        BUFSIZE = MAX_BR * 2

    crf = args.crf if args.crf else None

    if not resolve_ffmpeg_tools():
        log.error("FATAL: ffmpeg/ffprobe not found in PATH")
        raise SystemExit(1)

    ok, err = resolve_transcoder(args.use)
    if not ok:
        log.error(f"FATAL: {err}")
        raise SystemExit(1)

    print(f"Using transcoder: {TRANSCODER} (profile: {TRANSCODER_PROFILE})")

    if args.remote:
        host, remote_dir = parse_remote_target(args.remote)
        local_tmp = Path(args.local_tmp).expanduser().resolve() if args.local_tmp else Path("/tmp/convert-remote")
        try:
            main_remote(host, remote_dir, local_tmp, crf, args.verbose)
        except KeyboardInterrupt as e:
            print(f"\nError: {str(e)}")
        except Exception as e:
            print(f"\nError: {str(e)}")
            log.exception("Unhandled exception")
    else:
        try:
            main(args.dir, crf, args.verbose)
        except KeyboardInterrupt as e:
            print(f"\nError: {str(e)}")
        except Exception as e:
            print(f"\nError: {str(e)}")
            log.exception("Unhandled exception")
