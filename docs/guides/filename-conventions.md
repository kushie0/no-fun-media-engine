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
20260311_DaisyChain_UL.mp4   ← upper-left
20260311_DaisyChain_UR.mp4   ← upper-right
20260311_DaisyChain_LL.mp4   ← lower-left
20260311_DaisyChain_LR.mp4   ← lower-right
```

## Proxy clips

Clips are exported to `D:\clips\<base>\` with an index suffix:

```
D:\clips\20260311_DaisyChain\
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
D:\audio\20260311_DaisyChain.zip
```

All channel WAVs for a given `(date, band)` group are zipped together. The ZIP uses
`ZIP_STORED` (no re-compression — WAV is already uncompressed PCM).

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
