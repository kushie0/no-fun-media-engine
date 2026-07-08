# Clip storage — where clips live (and the one rule)

**The rule: all clips live on `C:\clips`. Every feed and every script reads/writes `C:\clips`.
`D:\clips` is deprecated. NAS holds the archive copy.**

If you are adding or changing anything that touches clips — a stream publisher, a cleanup
script, a doc, an env default — point it at **`C:\clips`**. Never reintroduce `D:\clips`.

## The three tiers

| Tier | Path | Role | Written by | Read by |
|---|---|---|---|---|
| **Streaming primary** | `C:\clips` | Fast SSD copy — every stream composes from here | engine `_export_clips()` (`clips_dest`, resolved in `nofun/paths.py:detect_clips_root`, defaults to `C:\clips` on native Windows) | gtv wall (`google_tv_run.ps1`), venue VLC streams (`start-streams*.ps1`) |
| **Archive** | `\\192.168.0.232\nofun-archive\clips` (NAS) | Durable primary copy of all media | `ClipsNasMirror` task → `scripts/clips-nas-mirror.ps1` (**C: → NAS**, hourly, `/E` copy-only — never deletes) | recovery / audit only |
| **Deprecated** | `D:\clips` | **Stale — do not read or write.** Frozen since the ~June 2026 clips-to-C migration | *(nothing)* | *(nothing — was the gtv publisher until 2026-07-05)* |

## Why C: (not D:)

`C:` is the SSD; `D:` (Ralph) is the big spinning working drive. Clips are tiny and read
constantly by the stream encoders, so they live on the fast disk. The engine already writes
here and the NAS mirror already pulls from here — `C:\clips` is the live primary. `D:\clips`
is a leftover partial copy from before the migration; the gtv publisher was the last thing
still reading it (fixed 2026-07-05, which is why it had been serving a 3-week-stale pool with
no recent shows).

> Note: clips are the **exception** to the "pipeline output lives on D:/NAS" layout in
> [`architecture.md`](architecture.md). `clips_dest` never follows `media_root` — it is pinned
> to C: so streaming stays fast regardless of NAS state.

## Retired (do not resurrect)

These encoded the obsolete "D: is primary" model and were removed on 2026-07-05:

- `scripts/clips-to-c.ps1` — mirrored `D:\clips → C:\clips`. Obsolete: the engine writes C:
  directly, so there is nothing to copy up from D:.
- `scripts/nas-clips-mirror.ps1` — mirrored `D:\clips → NAS` with `/MIR` (deletes!). Replaced
  by `clips-nas-mirror.ps1` (`C: → NAS`, copy-only).

## Checklist when touching clips

- [ ] Default any `ClipRoot` / `CLIPS_ROOT` to `C:\clips`.
- [ ] Reads and writes both go to `C:\clips` — do not split across drives.
- [ ] New clip → NAS backup is handled by the existing `ClipsNasMirror` task; don't add a second mirror.
- [ ] Quarantine/glitch pools go **beside** the tree (e.g. `C:\glitch_clips\`), outside the
      recursive scan, so feeds skip them automatically without losing the files.
