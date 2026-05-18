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
  ├── YYYYMMDD_Band.MOV  ──► _process_mov()
  │                            ├── _encode_quadrants()  → D:\videos\YYYYMMDD_Band_{UL,UR,LL,LR}.mp4
  │                            └── _export_clips()      → D:\clips\YYYYMMDD_Band\*.mp4
  │
  ├── Audio/             ──► _process_audio_dir_wavs()    [primary, going-forward]
  │   └── YY-M-D_Band_chan7.3.wav   (~32 pre-separated single-channel WAVs from hardware)
  │                                                       │
  │                                                       ├─► _group_wav_files()
  │                                                       └─► _export_audio_zips()  → D:\audio\<base>.zip
  │
  └── YYYYMMDD_Band.wav  ──► _split_multichannel_wavs()    [legacy, only old recordings]
      (single multi-ch file)    └── pan filters → YYYYMMDD_Band_ch01.wav … → ZIP
```

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
