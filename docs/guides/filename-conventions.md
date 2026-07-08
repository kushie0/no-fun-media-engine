# Filename Conventions

All filenames encode the recording date and band name so the pipeline can group
related files without relying on filesystem metadata (which drifts during copies).

## Source video

| Format | Example | Notes |
|--------|---------|-------|
| Short-date | `26-3-11_DaisyChain.MOV` | `YY-M-D_BandName` — month/day not zero-padded |
| Long-date  | `20260311_DaisyChain.MOV` | `YYYYMMDD_BandName` — zero-padded |

Both formats are accepted. `extract_date_band()` in `nofun/inventory.py` normalises them.

## Output quadrant files

```
26-03-11_DaisyChain_CAM1.mp4   ← upper-left  crop
26-03-11_DaisyChain_CAM2.mp4   ← upper-right crop
26-03-11_DaisyChain_CAM3.mp4   ← lower-left  crop
26-03-11_DaisyChain_CAM4.mp4   ← lower-right crop
```

Cloud labels map to quad position: **UL→CAM1, UR→CAM2, LL→CAM3, LR→CAM4**
(`CAM_LABELS` / `QUAD_FILTER` in `nofun/video.py`).

> **Legacy naming (pre-migration):** older outputs used `_UL/_UR/_LL/_LR.mp4`. The crop
> geometry is identical, so these were reconciled to `_CAM1-4.mp4` by an in-place rename
> (no re-encode). If you find old `_UL`-style names, they predate the migration.

## Instagram reel

```
26-03-11_DaisyChain_INSTAGRAM.mp4   ← 9:16 vertical reel (generate_reel(), nofun/reel.py)
```

Legacy name was `_reel.mp4`; reconciled in place to `_INSTAGRAM.mp4`. The reel generator
sources from the `_CAM1.mp4` quad — old `_UL`-named quads will not be found.

## Audio master

```
26-03-11_DaisyChain_AUDIO.mp3   (or .wav)   ← mixed-down master (nofun/mastering.py)
```

> **Legacy naming (pre-migration):** `_FULLSET.mp3`/`.wav` was the **old name for the board master**,
> superseded by the 6/1 audio-pipeline re-master that writes `_AUDIO.*`. Reconciled 2026-06-02: a
> `_FULLSET` with no `_AUDIO` sibling was the sole master → renamed in place to `_AUDIO.*`; a `_FULLSET`
> with a newer `_AUDIO.mp3` sibling was a superseded duplicate → deleted. No `_FULLSET` files remain.

## Proxy clips

Clips are exported to `C:\clips\<base>\` with an index suffix:

```
C:\clips\20260311_DaisyChain\
    20260311_DaisyChain_UL_1.mp4
    20260311_DaisyChain_UL_2.mp4
    ...
```

Each clip is `STEP_SECONDS` (40 s) long.

## Audio — `Audio/` subfolder (going-forward primary path)

The recording hardware now writes ~32 pre-separated single-channel WAVs
per performance directly into `VenueLighting/Audio/`:

```
26-3-11_DAISY_CHAIN_chan7.3.wav
```

Format: `YY-M-D_BandName_chan<number>.<sub>.wav`

The `_chan[\d.]+` suffix is stripped by `JUNK_SUFFIX` before `extract_date_band()` runs.
Handled by `_process_audio_dir_wavs()` in `nofun/audio.py`.

## Audio — multichannel WAV (legacy, dormant)

Older performances produced a single multi-channel WAV that the pipeline
splits into per-channel files:

```
20260311_DaisyChain.wav          ← original multichannel file
20260311_DaisyChain_ch01.wav     ← split channel 1
20260311_DaisyChain_ch02.wav     ← split channel 2
```

`_CH_WAV` regex: `_ch\d+\.wav$` — distinguishes split files from originals.
Handled by `_split_multichannel_wavs()` in `nofun/audio.py`. **This path
is dormant for new recordings**; kept so we can still reprocess archived
multichannel files.

## Audio ZIP archives

```
<media_root>\audio\26-03-11_DaisyChain_MULTITRACK.zip
```

All channel WAVs for a given `(date, band)` group are zipped together. Legacy name was a
bare `{base}.zip`; reconciled in place to `_MULTITRACK.zip`. `<media_root>` is the NAS
(`N:`) when reachable, else the local `D:` fallback (`detect_media_root()`, `nofun/paths.py`).

## Audio recorder files

```
R_20260311-103000am.wav
```

Format: `R_YYYYMMDD-HHMMSSam|pm.wav` — produced by some recording hardware. The `R_`
prefix and timestamp are stripped before band-name extraction.

## `_NoFun` placeholder

If a source file has no recognisable band name, the pipeline writes `_NoFun` into the
output filename and triggers an interactive rename prompt in the TUI (the `RENAME`
command flow). The user supplies the correct band name; the pipeline renames all four
quadrant files and the clip directory.

## SharePoint / cloud paths

Shared clips are uploaded to `sharepoint_dest` (OneDrive Multitracks folder)
by the scheduled `SYNC PERFORMANCES` task. The `EXPIRE CLOUD SHARES`
scheduled task (hourly) deletes files past `EXPIRE_AGE` (28 days; defined
in `nofun/inventory.py`).
