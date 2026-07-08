# Architecture

## Mixin inheritance

```
Pipeline(VideoMixin, AudioMixin, CleanupMixin,
        InventoryMenuMixin, StreamsMenuMixin,
        JobsMenuMixin, ReprocessMenuMixin)
    │
    ├── VideoMixin          (nofun/video.py)           — quadrant encode, clip export, rename
    ├── AudioMixin          (nofun/audio.py)           — channel split, ZIP archive
    ├── CleanupMixin        (nofun/cleanup.py)         — audit findings, delete queue management
    ├── InventoryMenuMixin  (nofun/menu_inventory.py)  — INVENTORY/STATUS menu commands (SCAN, RENAME, REMASTER, etc.)
    ├── StreamsMenuMixin    (nofun/menu_streams.py)    — STREAMS menu commands
    ├── JobsMenuMixin       (nofun/menu_jobs.py)       — JOBS menu commands (CANCEL, etc.)
    └── ReprocessMenuMixin  (nofun/menu_reprocess.py)  — REPROCESS menu commands
```

`Pipeline.__init__` sets shared attributes (`search_dir`, `mount_d`, `enc`, `trial_run`,
`force`, etc.) that all the mixins read via `self`. The mixins have no `__init__` and
do not call `super().__init__`.

## Data flow

```
VenueLighting/
  ├── YY-MM-DD_Band.N.mov  ──► _process_mov()
  │                             ├── _encode_quadrants()  → D:\videos\YY-MM-DD_Band_CAM1..CAM4.mp4
  │                             └── _export_clips()      → C:\clips\YY-MM-DD_Band\*.mp4  (SSD streaming primary)
  │
  ├── YY-MM-DD_Band_chanNN.0.wav   ──► _collect_chan_candidates(root) → _process_audio_group()   [PRIMARY]
  │     (~32 pre-separated single-channel WAVs from hardware, in the VenueLighting root)
  │        ├─► silence-drop — channels < MIN_ACTIVE_SECONDS are dropped from the ZIP
  │        ├─► _zip_wav_group()  → D:\audio\YY-MM-DD_Band_MULTITRACK.zip   (active channels)
  │        └─► all 32 channels retained in D:\audio_archive\ (silent AND active — on_success='archive')
  │
  ├── Audio/  subfolder  ──► _collect_chan_candidates(Audio/)   [DEPRECATED — still scanned, no longer fed]
  │
  └── YY-MM-DD_Band.wav  ──► _split_multichannel_wavs()    [LEGACY — one multi-ch file, old recordings only]
      (single multi-ch file)    └── pan filters → YY-MM-DD_Band_ch01.wav … → ZIP
```

Note the `_chanNN` per-channel inputs are **not** the `_chNN` split outputs: they do not match the
`_ch\d+\.wav$` split regex (`nofun/audio.py`), so the primary path treats them as ordinary
single-channel WAVs and never invokes `_split_multichannel_wavs`.

## Drives at a glance (prod `DESKTOP-FVRSFIV`, mapped 2026-07-02)

| Drive | Label | Size | Free | Role |
|---|---|---|---|---|
| **`C:`** | *(none)* | 931 GB | **~31 GB (3%) ⚠️** | **System + RECORD drive.** TD records raw `.mov` + per-channel `.wav` to `C:\Users\NOFUNadmin\VenueLighting\` (the engine's `search_dir`); also holds the engine repo (`C:\Users\NOFUNadmin\clips`), OneDrive Multitracks cache, `tools\`, `wer_touchplayer\`. Raw recordings linger here until `RAW_EXPIRE_AGE` (14 d). |
| **`D:`** | Ralph | 3.7 TB | ~594 GB (16%) | **Pipeline output / working** — all engine output (`videos/audio/clips/*_archive`), `tmp\` (dumps, stress logs), `logs\`. |
| **`E:`** | DNNoFun5TB1 | 4.7 TB | ~980 GB (21%) | **Backup** — `2026_NoFun_AV-Backup\`. |

⚠️ **`C:` is near-full (3% free).** It is *both* the OS drive and the record target, and raw
multichannel recordings (~425 MB × ~32 ch per show) accumulate in `VenueLighting\` for up to 14 days.
A show or two could fill it → recording failure / system instability. Never run write-fill disk
stress on `C:`; disk-contention testing there must be **read-churn only** (no growth).

## Prod filesystem layout (`D:\`)

All pipeline output lands on `D:\` (`mount_d`). Code references these paths
via named attributes set in `Pipeline.__init__` (`media_engine.py`).

| Folder | Code attribute | What lives here |
|---|---|---|
| `D:\videos\` | `vids_dest` | Encoded quadrant MP4s — output of `_encode_quadrants()` |
| `D:\audio\` | `audio_dest` | Audio ZIP archives — output of `_export_audio_zips()` |
| `D:\audio_archive\` | `audio_archive` | All ~32 per-channel input WAVs after processing — both silent and zipped-active channels (primary path). For legacy multichannel recordings, the split outputs + original land here too. Cleaned up (deleted) once the corresponding ZIP exists in `D:\audio\` and age > `RAW_EXPIRE_AGE`. |
| `D:\video_archive\` | `video_archive` | Source `.mov` files archived here after encoding. Cleaned up once ≥4 quad files exist and age > `RAW_EXPIRE_AGE`. |
| `C:\clips\` | `clips_dest` (via `paths.py`) | Short clip thumbnails — output of `_export_clips()`. **Lives on C:, not D:** — SSD primary read by every stream feed (gtv + venue VLC), mirrored to NAS hourly by `ClipsNasMirror`. `D:\clips\` is deprecated/stale. See [`clip-storage.md`](clip-storage.md). |
| `D:\logs\` | — | Rolling log files (`RemoteRotatingHandler`, 800 KB rotation per file). |
| `D:\hard_paused\` | — | Partial encode outputs saved after a hard-stop PAUSE event. |

**Expiry thresholds** (`nofun/inventory.py`):
- `RAW_EXPIRE_AGE` — days before local raw files (`.mov` in `video_archive`, `.wav` in `audio_archive`) are deleted once outputs exist.
- `EXPIRE_AGE` — days before cloud share media is removed from OneDrive.

**Common confusion:** `audio_archive` is the *intermediate* WAV store, not the ZIP destination. ZIPs go to `audio_dest` (`D:\audio\`). If you see a large accumulation of `.wav` files in `D:\audio_archive\` with no corresponding ZIPs in `D:\audio\`, the zip step is stalled or failing for those performances.

## PAUSE state machine

```
RUNNING ──PAUSE──► SOFT_PENDING ──PAUSE (mid-encode)──► HARD_PENDING ──► PAUSED
                                                                              │
RUNNING ◄──RESUME──────────────────────────────────────────────────────────┘
```

- **SOFT_PENDING**: current ffmpeg job finishes, then the watchdog loop stops.
- **HARD_PENDING**: `_current_ffmpeg_proc.kill()` is called; partial output is moved
  to `mount_d/hard_paused/`. The PAUSED transition happens at the end of the watchdog
  loop iteration (not in `_handle_command`).
- **PAUSED**: watchdog loop is idle, waiting for RESUME.

## Dispatch schedule (time-of-day gate)

Heavy work is **time-gated to stay out of the live-show window.** Read the direction
carefully — it is the inverse of what the phrase "4pm–midnight gate" suggests:

- Heavy lanes — **GPU / CPU / MANUAL** (quad encodes, audio split + zip, remasters, reels)
  — dispatch **only 00:00–16:00** (midnight to 4pm). They are **blocked 16:00–24:00**
  (4pm to midnight), because that is when shows happen and the box must stay quiet.
- **SCHEDULED** housekeeping (SharePoint sync, expiry sweeps, SCAN) runs **24/7** — it is
  never gated.

Source of truth: `DEFAULT_SCHEDULE` + `_ENCODE_END_HOUR = 16` in `nofun/job_queue.py`. A
`ScheduleRule(start_hour, end_hour)` is active when `start_hour <= current_hour < end_hour`
(`is_active()`), so `end_hour=24` means "until midnight" and `end_hour=16` means "stops at 4pm".

Consequences worth internalising:
- A MANUAL job (e.g. REMASTER) triggered at, say, 18:00 **enqueues but sits `pending`**
  until midnight; the status bar reads `paused`, not an error. Nothing is broken — it is
  waiting for the window to open.
- **NOPROBLEM** (from HOME) bypasses the gate for the rest of the day and auto-resets at
  midnight. A *second* NOPROBLEM also sets `force=True` (re-encode `.mov`s whose four quads
  already exist) — so press it **exactly once** unless you intend a full re-encode.

Mnemonic: *heavy jobs run **overnight until 4pm**; the **4pm–midnight** stretch is the show
window, so the pipeline deliberately goes idle then.*

## Threading model

```
Main thread                       Worker thread (Textual worker)
─────────────────────────         ─────────────────────────────────────
MediaEngineApp.run()              Pipeline.run_with_queue(cmd_queue, app)
  └── Textual event loop            └── watchdog loop
        │                                  ├── _detect_file_events()
        │  cmd_queue.put(cmd)              ├── _process_mov() / _export_audio_zips()
        │◄─────────────── keyboard ─────   └── cmd_queue.get_nowait()
        │
        │  app.update_status(markup)
        │◄──── call_from_thread() ─────────┘
        │
        └── Widget.refresh()
```

`run_ffmpeg` accepts `progress_cb` and `proc_cb` callbacks so the worker thread can
push frame progress to the TUI and store the `Popen` handle for mid-encode kill.

## SharePoint / OneDrive lifecycle

**Prod OneDrive path:** `C:\Users\NOFUNadmin\OneDrive - No Fun Troy LLC\Multitracks\`

Files are shared with bands via a OneDrive folder (`OneDrive - No Fun Troy LLC/Multitracks/`).
Each performance gets a date subfolder (`YY-MM-DD_BAND1_BAND2/`) containing quad MP4s, the
audio ZIP, and a `_nofun_info.txt` manifest listing files and expiry date.

```
Multitracks/
  26-05-20_DEARMARYANNE_ETLY/   ← active: media present, within 28-day window
  26-03-15_PV/                  ← active: media present, expires ~Jun 6
  archived/
    26-04-30_GRATITUDE/         ← expired stub: only _nofun_info.txt remains
    26-04-29_NODIVISION/        ← expired stub
    …
  _sharepoint_permissions_YYYY-MM-DD.csv   ← access report (not for bands)
```

**Lifecycle stages:**

1. **SYNC** (`_sync_eligible_performances`, every 15 min) — copies quad MP4s and audio ZIP
   into the date folder; writes `_nofun_info.txt` with file list and 28-day expiry.
2. **EXPIRE** (`_auto_expire_cloud_shares`, hourly) — deletes media files once past expiry;
   updates `_nofun_info.txt` with a "files removed" note so bands who navigate there see an
   explanation rather than an empty folder.
3. **ARCHIVE** (`_archive_empty_cloud_folders`, called at the end of each expire run) — moves
   the now-stub folder into `archived/`. The move is a filesystem `rename()`, which OneDrive
   treats as a MOVE (item ID preserved), so any shared link given to bands still resolves to
   the folder even in its new location.
4. **RE-UPLOAD** (`_reupload_performance`, INVENTORY → REUPLOAD command) — if a band needs
   files after expiry, moves the folder back from `archived/` to the top level and re-syncs,
   resetting the 28-day clock.

**Key invariants:**
- The engine never deletes or recreates date folders — only `rename()` moves and file-level
  `unlink()` deletes. SharePoint item IDs (and therefore shared links) are always preserved.
- `archived/` is skipped by inventory scans and expiry checks — files there are considered
  permanently expired and will not be re-deleted or re-synced automatically.
- The `_nofun_info.txt` system file is never deleted, only rewritten. It is excluded from
  "has media" checks so a folder containing only an info stub is treated as empty.

## Delete queue

`DeleteQueue` accumulates files with `.add(path, reason)`. `execute()` is called
automatically each pipeline loop iteration and deletes all queued files without
requiring any confirmation command.

## StreamServer / StreamWorker

```
StreamServer
  └── [StreamWorker × N]
        ├── ffmpeg (subprocess) → HLS segments → BytesIO broadcast
        └── _ThreadingHTTPServer
              └── HTTP clients subscribe via queue.Queue
```

Each `StreamWorker` runs an ffmpeg subprocess that re-encodes clips into an HLS stream
and broadcasts chunks to all connected HTTP clients. Slow clients have chunks dropped
(not disconnected) to avoid blocking faster clients.
