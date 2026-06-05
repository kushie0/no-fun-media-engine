# Delete-and-rebuild verify pass

A reusable **final verification** for substantial changes to the media pipeline (storage routing,
naming, encode/audio paths, expiry). Instead of trusting green unit tests, you delete one real
performance's outputs on prod and watch the deployed engine rebuild them from the raw originals,
then prove the rebuilt outputs equal a safe backup. It exercises detection → encode → audio → zip →
master → archive end-to-end on the actual deployed binary and the actual filesystem.

This is the generalized form of the NAS-cutover smoke test (archived bundle
`2026-06-01_nas-primary-storage.md`), which is the worked example to copy from.

**Ready-made fixture.** A full raw+derived set for one real show is parked on the NAS at
`\\192.168.0.232\nofun-archive\_smoke_test_fixture\26-05-23_ONE_THRU_TEN\` (41 files / 24.156 GB: the
`.mov`, 32 `_chanNN.0.wav`, quads, reel, ZIP, master). It lives outside the engine-managed tree so it
is never scanned or expired — drop it into VenueLighting to re-run this procedure without sourcing a
new show.

## When to harness it

Use it as the Stage-5 verify when a change could plausibly break *where files land* or *what gets
written*, and a unit test can't see the real disk:

- storage routing / drive or mount changes (the NAS cutover)
- output naming or path conventions
- encode skip-gate or audio ingestion logic
- expiry / retention thresholds

Skip it for changes with no filesystem-write surface (TUI layout, logging, pure refactors).

## Engine facts that make it work (verify these still hold before relying on them)

- **Encode skip is disk-presence, not DB-driven.** A `.mov` re-encodes whenever its four quad
  `.mp4`s are absent from `vids_dest` (`nofun/video.py` `_process_mov`). Delete the quads → rebuild.
  No `encoding_db.json` surgery.
- **Raw video is retained**, not consumed: the `.mov` is *moved* to `video_archive/` after encode,
  so it round-trips byte-identical and is recoverable for a re-run.
- **Audio re-triggers on WAV presence** — no zip-presence skip gate. The per-channel path processes
  any stable single-channel WAV in the engine's `search_dir`. Each show is one multicam `.mov` plus
  **32 discrete mono WAVs** `{date}_{band}_chanNN.0.wav` (not one multichannel file). The full 32
  survive in `audio_archive/` for a completed show (silent channels archived directly; active ones
  zipped into `_MULTITRACK.zip` **and** archived), so raw audio is recoverable from `audio_archive/`
  without unzipping.
- **Expiry interacts with show age.** Raw `.mov` + loose `audio_archive` WAVs are deleted once
  outputs exist and the show is older than `RAW_EXPIRE_AGE` (`nofun/inventory.py`). Picking a show
  **older** than that threshold means the engine will re-expire the raws minutes after rebuild —
  expected, not a fault (durable audio lives on in `_MULTITRACK.zip`). Capture equivalence hashes
  *before* that expiry fires, or pick a show younger than the threshold to keep the loose raws.

## The procedure

**Pick (read-only).** A completed performance whose full raw set is recoverable: `.mov` present in
`video_archive/` (or still in `search_dir`) **and** all 32 `_chanNN.0.wav` present in
`audio_archive/` (count them — must be 32), plus its derived outputs as the equivalence baseline.
Prefer a representative real show over a tiny fragment. Note its age vs `RAW_EXPIRE_AGE`.

**Back up ×2 — gate everything on this.** Copy to two distinct safe locations (e.g.
`C:\smoke_backup_A\` + a second drive): the raw `.mov`, all 32 raw WAVs, and every derived output
(quads, reel, ZIP, master). Verify each backup — file count, sizes, and **sha256** of the `.mov` +
all 32 WAVs match the source. **Do not proceed until both verify**: after the delete, the backup is
the only copy of the raw inputs.

**Stop the engine.** Quit cleanly (or kill the pane); confirm no `ffmpeg` children remain. Nothing
may re-touch files mid-delete.

**Delete the test performance's outputs — ⚠️ PROD-WRITE, explicit go-ahead in the moment.** Delete
*only* this performance's quads, reel, ZIP, master, archived `.mov`, and 32 archived WAVs. Per
`CLAUDE.md`, present the exact file list and get an explicit OK before deleting. Leave every other
performance and `C:\clips\` untouched. Never run during a live-show window.

**Re-stage the raw inputs.** Copy the `.mov` + 32 `_chanNN.0.wav` **from a backup** into the
engine's `search_dir` root (not an `Audio/` subfolder) — that is where the per-channel ingestion
scans.

**Reprocess.** Start the engine. Heavy lanes are gated to 00:00–16:00; if testing outside that
window, send `NOPROBLEM` **exactly once** to bypass the gate (a *second* `NOPROBLEM` flips to
force-re-encode-all — don't). Watch for, in order: detection → silence probes → quad `CREATE` lines
→ `.mov` MOVE to `video_archive/` → ZIP/master/reel → 32 WAVs re-archived. **The core proof is one
`CREATE` line whose path is the expected target** (e.g. the NAS UNC / `N:\videos\…`).

**Verify equivalence (not byte-identity — GPU AMF encodes are non-deterministic).** Against the
backup baseline: full file set present with exact names; each output size within ~±5%; `ffprobe`
each quad/reel for duration (±1 s), resolution, codec, stream counts; the archived `.mov` and 32
WAVs **sha256-match** the backup (they are moved/copied inputs, not re-encoded); everything resolved
to the intended location, not a fallback. INVENTORY returns to COMPLETE / SHARE_ELIGIBLE. Any
mismatch beyond encode non-determinism = **fail** → investigate before declaring the deploy verified.

**Close out.** Spot-check INVENTORY is clean. Keep both backups until fully satisfied, then discard
(also a prod-write — confirm first). Record the captured `CREATE` line + a one-line pass/fail into
the effort's plan doc's Verify section, then archive the bundle.

## Rollback (if a run fails mid-test)

The only guaranteed-good copy during the delete→rebuild window is the backup. To restore without the
engine: copy the raw originals back to `video_archive/` + `audio_archive/` and the derived outputs
back to `videos/` + `audio/` from a backup.
