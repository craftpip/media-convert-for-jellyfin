# H.264 Bitrate Scanner & Converter

This project contains two Python scripts:

1. **Scanner (dry-run)**  
   Recursively scans a directory and **prints only the video files that would be converted**, based on bitrate and container rules.

2. **Converter (active)**  
   Recursively scans a directory and **actually converts high-bitrate videos to H.264 (NVENC)** while preserving audio and supported subtitles.

Both scripts share the **same decision logic**, so the scanner output exactly matches what the converter would process.

## Usage

```bash 
python r.py path_to_dir # scan and convert the files.
python s.py path_to_dir # scan for files only
```

---

## Features

- Recursive directory scan
- Accurate bitrate detection using `ffprobe`
- H.264 container validation
- NVIDIA NVENC encoding
- Audio stream copy (no re-encode)
- Subtitle handling (container-aware)
- Safe in-place replacement
- Temporary file protection
- Dry-run mode via scanner script

---

## Requirements

- Python 3.10
- `ffmpeg` and `ffprobe` available in `PATH`
- NVIDIA GPU with NVENC support (for converter)

Verify:
```bash
ffmpeg -version
ffprobe -version
```

Configure this in the files.
```python
TARGET_BR = 3000      # kbps
MAX_BR = 3300         # kbps
BITRATE_TOLERANCE = 300
BUFSIZE = 6000        # kbps
```