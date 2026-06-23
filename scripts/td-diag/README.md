# TD-hang diagnostics toolkit

Read-only diagnostics for the TouchDesigner (`TouchPlayer.exe`) AppHang
investigation. Background and root-cause analysis live in
[`docs/active/td-hang-investigation.md`](../../docs/active/td-hang-investigation.md).

The hang is an internal main-thread cook in `libTD.dll` we can't see into (no TD
symbols), so these tools **don't probe TD** — they capture the *external state
around the hang* so the next one documents its own trigger. Every tool here is
read-only and **safe to run during a live show** (they observe; they restart
nothing and edit no `.toe`).

| Tool | What it answers | When |
|---|---|---|
| `which-streamer.ps1` | Which producer (VLC vs Python `StreamServer`) is feeding TD right now, and who's connected. | Run first — everything else assumes you know the live producer. |
| `td-conn-watch.ps1` | When does TD connect/drop each stream port? Tests the reconnect-trigger theory. | Start detached at show start; leave running. |
| `td-timeline.py` | What else happened ±N min around a hang (engine log, WER, event log, conn-watch)? | After a hang (or to inspect 6/22). |
| `dump-ip-delta.py` | Is the stuck thread wedged on one op, or looping? | Once, against the existing `D:\tmp\td_dumps`. Offline. |

## Quick start (prod, `C:\Users\NOFUNadmin\clips`)

```powershell
# 1. Confirm the live producer + current TD clients
powershell -NoProfile -File scripts\td-diag\which-streamer.ps1

# 2. Start the connection watcher detached for the show
Start-Process powershell -ArgumentList `
  '-NoProfile','-File','scripts\td-diag\td-conn-watch.ps1' -WindowStyle Hidden
#    -> logs CONNECT/DROP to D:\tmp\td_conn_watch.log

# 3. Settle the "loop vs single stuck op" question from existing dumps
.\.venv\Scripts\python.exe scripts\td-diag\dump-ip-delta.py D:\tmp\td_dumps

# 4. After the next hang (auto-anchors on the latest TouchPlayer AppHang)
.\.venv\Scripts\python.exe scripts\td-diag\td-timeline.py
#    or inspect 6/22 explicitly:
.\.venv\Scripts\python.exe scripts\td-diag\td-timeline.py --at "2026-06-22 20:12" --window 20
```

`td-timeline.py --conn-log D:\tmp\td_conn_watch.log` folds the connection watcher
into the same merged timeline.

## Notes

- The `.ps1` tools need PowerShell 5+ and only read `Get-NetTCPConnection` /
  `Get-Process` / `Get-WinEvent`.
- `td-timeline.py` and `dump-ip-delta.py` are pure stdlib; run them with the
  engine venv python. On non-Windows the WER / event-log sources are simply
  skipped, so the timeline still works on local log copies.
- `dump-ip-delta.py` is a self-contained x64 minidump parser — it does **not**
  need `dmpscan.py` or TD symbols.
