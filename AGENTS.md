# media-convert-for-jellyfin — LLM Guide

## Project Overview

This tool shrinks video files in a self-hosted media library (Jellyfin, etc.) by re-encoding oversized videos to a target bitrate and stripping unwanted audio/subtitle tracks.

## Key files

- `convert.py` — entry point. Run with `python convert.py /path/to/media`
- `.env` — configuration (not committed, create from readme template)
- `done.txt` — tracks completed conversions (auto-generated, do not delete)
- `size-report.csv` — output with before/after sizes (auto-generated)

## How to use

```bash
# Basic GPU conversion
python convert.py /path/to/media --use nvidia

# CPU with CRF quality
python convert.py /path/to/media --use cpu --crf 23

# With target bitrate and logging
python convert.py /path/to/media --bitrate 2800 --log-file convert.log

# Optional parallel local conversion
python convert.py /path/to/media --use nvidia --workers 2
```

### audio/subtitle Stripping

Languages kept are defined in `ALLOWED_AUDIO_LANGS` inside `convert.py` (line 34). Subtitles are copied only for supported containers and codecs — see `TEXT_SUB_CODECS` (line 33). Edit these lists to control what's kept.

### encoder Profiles

`--use` accepts: `nvidia`, `amdgpu`, `intel`, `vaapi`, `cpu`, or any raw ffmpeg encoder name (e.g. `h264_nvenc`). Aliases are defined in `TRANSCODER_ALIASES` (line 54) and their corresponding ffmpeg encoders in `TRANSCODER_ENCODERS` (line 78).

### Workers

`--workers` / `-j` controls how many local files are processed concurrently. The default is `1`, which preserves the original sequential behavior. Only suggest higher values when the user explicitly wants concurrency.

Suggested starting points: NVIDIA `2`, AMD/VAAPI `2`, Intel QSV `2-4`, CPU `2-4` depending on core count. If ffmpeg returns encoder/session errors, reduce workers.

Remote mode is intentionally sequential; do not assume `--workers` is safe for remote SCP workflows.

## Important Rules

- **Never modify or delete `done.txt`** — it prevents re-converting already-processed files.
- **Always write the temp file first**, then replace the original via `safe_replace()`.
- The `TMP_TAG = ".__transcoding__"` is used to identify in-progress temp files.
- GPU decoder support varies per profile — check `HWACCEL_DECODABLE` (line 45) before suggesting hardware acceleration.
- Keep `--workers` defaulting to `1`; parallel conversion must remain opt-in.

## .env Variables

| Variable | Default | Description |
|---|---|---|
| `TARGET_BR` | 3000 | Target video bitrate in kbps |
| `MAX_BR` | 3300 | Maximum bitrate before conversion triggers |
| `BITRATE_TOLERANCE` | 300 | Tolerance above MAX_BR |
| `BUFSIZE` | 6000 | ffmpeg bufSize |
| `DEFAULT_CRF` | 23 | Default CRF value for CPU mode |
| `FFMPEG_BIN` | — | Override ffmpeg binary path |
| `FFPROBE_BIN` | — | Override ffprobe binary path |

## Safety

- Temp files (`.__transcoding__`) are cleaned up on startup.
- `safe_replace()` retries 12 times with 250ms delay on permission errors.
- Discovery skips `.trickplay` directories automatically.
