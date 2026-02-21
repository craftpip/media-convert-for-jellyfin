# H.264 Bitrate Scanner & Converter

This project contains two Python scripts:

1. **Scanner (dry-run)**
   Recursively scans a directory and prints only video files that match conversion conditions.

2. **Converter (active)**
   Recursively scans a directory and converts high-bitrate videos to H.264 while preserving compatible subtitle streams.

Both scripts use the same conversion decision logic, so scan output matches converter eligibility.

## Usage

```bash
python scan.py /path/to/media
python convert.py /path/to/media
```

Useful converter options:

```bash
python convert.py /path/to/media --use nvidia
python convert.py /path/to/media --use cpu --crf 23
python convert.py /path/to/media --bitrate 2800 --verbose
python convert.py /path/to/media --log-file convert.log
```

## Features

- Recursive directory scan
- Bitrate detection with `ffprobe`
- Container validation before conversion
- Multiple transcoder profiles (`nvidia`, `amdgpu`, `intel`, `cpu`, `vaapi`)
- Conversion tracking via `done.txt`
- Conversion size report in `size-report.csv`
- Temp file safety and automatic cleanup

## Requirements

- Python 3.10+
- `ffmpeg` and `ffprobe` in `PATH`
- GPU encoder support if using hardware profiles

Verify tools:

```bash
ffmpeg -version
ffprobe -version
```

## Environment Configuration

Create a `.env` file in the project root:

```dotenv
TARGET_BR=3000
MAX_BR=3300
BITRATE_TOLERANCE=300
BUFSIZE=6000
DEFAULT_CRF=23
```

Optional binary overrides:

```dotenv
FFMPEG_BIN=/usr/bin/ffmpeg
FFPROBE_BIN=/usr/bin/ffprobe
```

## LLM Usage

If you are using this repository from an LLM agent (Copilot, ChatGPT, Claude, etc.), use this minimal workflow:

1. **Read-only preview first**

```bash
python scan.py /path/to/media
```

2. **Run conversion with explicit encoder**

```bash
python convert.py /path/to/media --use nvidia
```

3. **Fallback to CPU when GPU encoder is unavailable**

```bash
python convert.py /path/to/media --use cpu --crf 23
```

4. **Use logs for audit/debug**

```bash
python convert.py /path/to/media --log-file convert.log --verbose
```

LLM-safe notes:

- Prefer `scan.py` before any write operation.
- Do not delete `done.txt`; it prevents repeated conversions.
- Check `size-report.csv` after runs to verify expected size reduction.
- Keep `.env` values explicit in prompts to avoid accidental quality/bitrate changes.
