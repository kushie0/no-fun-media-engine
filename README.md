# no-fun-media-engine

Watchdog pipeline that processes concert recordings into archived assets: 4-quadrant MP4s, 320×180 proxy clips, and per-performance audio ZIP archives.

<p align="center">
  <img src="docs/screenshots/quadrant-output-1.jpg" width="80%" alt="4-quadrant output frame" />
</p>

Audio input is ~32 pre-separated single-channel WAVs per performance in a `VenueLighting/Audio/` subfolder.

## Requirements

- Python 3.13+
- [uv](https://github.com/astral-sh/uv)
- ffmpeg / ffprobe on PATH

## Install

```
uv sync
```

## Run

```
uv run python media_engine.py
```

Launches in TUI/watchdog mode. Watches `SEARCH_DIR` and processes recordings as they arrive.

## Environment variables

All storage locations resolve through one `StorageConfig` (`nofun/storage_config.py`),
built from these variables at startup; the resolved layout is logged once on boot
(`Storage layout …`). Every default reproduces the paths below, so a standard box needs
none of them — set only what differs on another machine.

| Variable | Default | Notes |
|---|---|---|
| `SEARCH_DIR` | `C:\Users\<username>\VenueLighting` | Source directory to watch |
| `MOUNT_D` | `D:/` | Output drive root |
| `MOUNT_C` | `C:/` | Companion override (rarely needed) |
| `CLIPS_ROOT` | `<MOUNT_D>/clips` | Clip output directory (C: SSD; never follows the NAS) |
| `NAS_ROOT` | _(unset)_ | NAS media root; falls back to `MOUNT_D` when unreachable |
| `SHAREPOINT_DEST` | `…\<user>\OneDrive - No Fun Troy LLC\Multitracks` | OneDrive sync folder (cloud disabled if absent) |
| `VIDEOS_SUBDIR` | `videos` | Media subdir name under the media root |
| `AUDIO_SUBDIR` | `audio` | Media subdir name under the media root |
| `VIDEO_ARCHIVE_SUBDIR` | `video_archive` | Archive subdir name (also names the D: backup tier) |
| `AUDIO_ARCHIVE_SUBDIR` | `audio_archive` | Archive subdir name |

## Output layout

All output lands on `MOUNT_D`. Directories are created on first run.

| Path | Contents |
|------|----------|
| `D:\videos\` | Quadrant MP4s |
| `D:\clips\` | Proxy clip segments |
| `D:\audio\` | Per-performance audio ZIP archives |
| `D:\video_archive\` | Source MOVs archived after encoding (auto-deleted after 10 days) |
| `D:\audio_archive\` | Source WAVs archived after splitting (same expiry) |
| `D:\logs\` | Rotating log files |

## GPU encoding

Uses `h264_amf` (AMD AMF) on Windows when available. Pass `--no-gpu` to fall back to `libx264`.

```
ffmpeg -encoders 2>nul | findstr amf
```

## OneDrive sync

Completed performances are synced to the first `C:\Users\<username>\OneDrive - *\Multitracks\` folder found. Skipped gracefully if absent.

## Tests

```
uv run pytest
```

## Further reading

- `docs/guides/architecture.md` — mixin diagram, threading model, PAUSE state machine
- `docs/guides/filename-conventions.md` — source, quadrant, clip, audio, and ZIP naming formats

## License

[MIT](LICENSE)
