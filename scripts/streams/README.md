# scripts/streams — Google TV clip-wall (gtv)

Direct-from-`C:\clips` 2×2 quad RTSP feeds for the venue TV sticks. **TouchDesigner-independent.**

- `google_tv_run.ps1` — supervisor + worker; composites 4 clips → 640×360 quad (libx264/CPU),
  publishes `/gtv1../gtvN` on its **own** mediamtx `:8656` (isolated from the NDI bridge on `:8654`).
- `gtv_heal.ps1` — async-discovers sticks on the subnet, assigns each a feed (optional fixed
  `-StickFeedMap`, otherwise round-robin), and keeps VLC receiving via reception-based
  (established-TCP) self-heal with timeout-bounded adb.
- `-Root` = runtime dir (`mediamtx.exe`, logs, `mtx-*.yml`, `*.ffconcat`). Defaults to the prod runtime
  location so the scripts run identically now that they live in git.
- Clip selection is weighted per worker launch: 22% from the last 14 days, 33% from the prior 60-day
  window, and the remainder from the entire library.

Scheduled tasks (Interactive/console): **`GoogleTVStreams`** (`-RtspPort 8656 -FeedCount 4 -QuadOnly`),
**`GoogleTVHeal`**. Deploy via `git pull`; repoint tasks at this path.

Full design, deployed state, and hard-won gotchas: `docs/active/td-ndi-rtsp-tv-wall-runbook.md` §9.
Migration roadmap: `docs/active/venue-av-target-architecture.md`.
