# Prod Processes And Scheduled Tasks

Snapshot source: `DESKTOP-FVRSFIV` (`prod`) as `NOFUNadmin`, checked 2026-07-04.

This is the operational map for "what is supposed to be running" around the media engine, venue streams,
TouchDesigner diagnostics, storage jobs, and one-off maintenance tasks. Treat it as a starting point when
prod looks odd: first identify the task/process family, then inspect the listed logs or checks.

## Quick Health Checks

```powershell
# Google TV wall: publishers + central heal + active stick reader.
schtasks /query /tn GoogleTVStreams /fo LIST /v
schtasks /query /tn GoogleTVHeal /fo LIST /v
Get-NetTCPConnection -LocalPort 8656 -State Established |
  ? RemoteAddress -like '192.168.*'
Get-Content C:\Users\NOFUNadmin\clips\scratch\ndi\gtv_heal.log -Tail 20

# Engine.
tasklist /FI "IMAGENAME eq python.exe"
Get-Content C:\Users\NOFUNadmin\clips\convert_recent.log -Tail 40

# TouchDesigner hang diagnostics.
schtasks /query /tn TDHangLog /fo LIST /v
Get-ChildItem C:\Users\NOFUNadmin\wer_touchplayer -ErrorAction SilentlyContinue
```

Task Scheduler result codes seen often:

| Code | Meaning in this repo context |
|---:|---|
| `0` | Finished successfully. |
| `267009` | Currently running. |
| `267011` | Task has not run / no current run record. |
| `267014` | Task was terminated. |
| `3221225477` | Process crashed/access violation. |

## Prod Default Services

These are the tasks that should normally be running for the current venue stream setup.

| Task | Expected | Action | Notes / logs |
|---|---|---|---|
| `GoogleTVStreams` | Running | `powershell ... C:\Users\NOFUNadmin\clips\scripts\streams\google_tv_run.ps1 -RtspPort 8656 -FeedCount 4 -QuadOnly` | Publishes `/gtv1..gtv4` on RTSP `:8656`. Playlists are weighted 22% last 14 days, 33% prior 60-day window, remainder whole library. Runtime files/logs stay in `C:\Users\NOFUNadmin\clips\scratch\ndi\`. |
| `GoogleTVHeal` | Running | `powershell ... C:\Users\NOFUNadmin\clips\scripts\streams\gtv_heal.ps1` | Central heal: discovers adb-reachable sticks, assigns `/gtvN`, force-stops/relaunches VLC if not receiving. Add `-StickFeedMap '192.168.0.242=gtv1,192.168.0.174=gtv2'` to pin individual TVs to specific feeds. Log: `scratch\ndi\gtv_heal.log`. |
| `TDHangLog` | Running | `powershell ... C:\Users\NOFUNadmin\tools\td-hangwatch.ps1 -DumpOnHang ...` | TouchDesigner hang watcher and dump capture. This is diagnostic, but intentionally always on while TD stability is under investigation. |

As of 2026-07-04, `GoogleTVStreams` and `GoogleTVHeal` are configured as logon tasks with no execution
time limit (`PT0S`), restart on failure (`RestartCount=999`, `RestartInterval=PT1M`), and duplicate
starts ignored. This prevents another silent 72-hour timeout or "left stopped after test" state.

## Healing Model

Stick-side healing and prod-side healing solve different problems.

| Layer | Mechanism | Covers | Does not cover |
|---|---|---|---|
| Stick boot APK | Android app launches VLC at boot to baked URL, currently `/gtv1` on `:8656` for `.0.242`. | Power-cycle / reboot recovery without prod intervention. | Mid-run VLC freeze, wrong foreground app, or lost playback after boot. |
| `GoogleTVHeal` on prod | Uses adb plus TCP reception checks. If a stick has no established connection to `:8656`, it force-stops VLC and relaunches the assigned feed. | Mid-run drops, home-screen exits, many VLC stuck states, dynamic stick reassignment. | Brand-new unpaired sticks; adb unreachable Wi-Fi state; cases where the stick is physically offline. |

Current live proof command:

```powershell
Get-NetTCPConnection -LocalPort 8656 -State Established |
  ? RemoteAddress -like '192.168.*'
```

Healthy single-stick output includes `192.168.0.242` connected to the mediamtx process.

## Streaming And AV Tasks

| Task | Current role | Expected state | Notes |
|---|---|---|---|
| `GoogleTVStreams` | Current Google TV clip-wall publisher. | Running. | Own mediamtx on `:8656`; independent from TouchDesigner and old NDI path. |
| `GoogleTVHeal` | Current Google TV central heal. | Running. | Restart this after any test that ends it. |
| `NDIBridge` | Old/parked NDI-to-RTSP bridge. | Ready, not running. | Action: `scratch\ndi\prod_run.ps1`. Kept for fallback/research. Do not start for current Google TV wall. |
| `NDITVHeal` | Old/parked NDI TV heal. | Ready, not running. | Last result may show terminated. Do not confuse with `GoogleTVHeal`. |
| `StartStreams` | Legacy VLC venue streams. | Parked. | Next run set to `2099-01-01`; action `C:\Users\NOFUNadmin\StartStreams.bat`. |
| `StreamsLive` | Legacy VLC live stream task. | Parked. | Next run set to `2099-01-01`; action `monday-test\StreamsLive.bat`. |
| `RefreshStreams` | Legacy refresh task. | Disabled. | Old clip-stream refresh path. |
| `RefreshStreamsLive` | Legacy live refresh task. | Disabled. | Old Monday-test refresh path. |
| `StartTD` | Manual/parked TD launcher. | Parked. | Next run set to `2099-01-01`. |

## Engine And Pipeline Tasks

| Task | Current role | Expected state | Notes |
|---|---|---|---|
| `StartEngine` | Starts `NOFUNMediaEngine.bat`. | Ready unless manually started. | Last successful run often indicates the engine was restarted through the launcher. |
| `BounceEngine` | Engine bounce helper. | Ready. | One-off maintenance. |
| `DailyInventoryScan` | Inventory generator. | Ready, scheduled daily. | Last result `1` should be checked if inventory output looks stale. |
| `TDHealthCheck` | Recurring TD health check. | Ready, recurring. | Runs every few minutes; action `TDHealthCheck.bat`. |
| `StopHeavy3PM` | Stops/deconflicts heavy jobs for show window. | Ready. | Aligns with the 4pm-midnight quiet window. |
| `LifecycleWatch` | Lifecycle watchdog runner. | Ready, logon trigger. | Last result may show "not run"; verify before relying on it. |

## Storage, NAS, And Backfill Tasks

Many of these are one-shot maintenance jobs. "Ready with no next run" often means the dated trigger has
already passed, not that the task is actively scheduled for future work.

| Group | Tasks | Notes |
|---|---|---|
| NAS / mirror | `ClipsNasMirror`, `NasAudit`, `NASCopy`, `NasRecover`, `NasVideo` | `ClipsNasMirror` is recurring hourly. `NasAudit` last result was `1` in the 2026-07-04 snapshot; check before trusting audit health. |
| FLAC migration/backfill | `flac_*`, `FlacConvert2025`, `FlacBackfill`, `FlacBackfillApply` | Most `flac_*` tasks are completed one-shots. `FlacBackfill` and `FlacBackfillApply` are disabled. |
| ZIP / salvage | `Zip64Salvage`, `ZipSalvage`, `zipstruct`, `CarveExtract` | Mostly one-shot recovery tooling. |
| Probes / reports | `chanpat`, `corrcheck`, `finalsum`, `moncheck`, `pcbcheck`, `probe`, `probe2025`, `skipwhy`, `survey`, `survey_now`, `vchk` | Investigation/reporting jobs from prior cleanup and audit work. Treat as historical unless a current doc says otherwise. |
| Promotion | `PromoteStaged` | One-shot staged-file promotion helper. |

## TouchDesigner Diagnostic Tasks

| Task | Expected state | Notes |
|---|---|---|
| `TDHangLog` | Running. | Current hang watcher. No execution time limit. |
| `TDHangWatch` | Disabled. | Older watcher. Do not start unless deliberately switching tools. |
| `TDStressNight` | Disabled. | Stress-test runner. Keep disabled outside explicit stress tests. |
| `TDStressNightStop` | Disabled. | Companion stop task for stress tests. |
| `board_only` | Ready, last result `3221225477`. | Historical TD/board-only launcher; crash result means do not treat as healthy. |

## Vendor / System Tasks Seen In The Same Root

These are not project-owned, but appear in the same task inventory and can cause noise:

| Task | Notes |
|---|---|
| `Dante Update Helper` | Audinate/Dante updater. |
| `EasyTune`, `EasyTune 1`, `GraphicsCardEngine` | Gigabyte utilities. |
| `MicrosoftEdgeUpdateTaskMachine*` | Edge updater tasks. |
| `OneDrive *` | OneDrive updater/startup/reporting. |

## Updating This Index

Use this command to refresh the root scheduled-task inventory:

```powershell
Get-ScheduledTask | ? TaskPath -eq '\' | Sort TaskName | % {
  $info = Get-ScheduledTaskInfo -TaskName $_.TaskName -TaskPath $_.TaskPath
  [pscustomobject]@{
    TaskName=$_.TaskName
    State=$_.State
    LastRunTime=$info.LastRunTime
    LastTaskResult=$info.LastTaskResult
    NextRunTime=$info.NextRunTime
    Enabled=$_.Settings.Enabled
    ExecutionTimeLimit=$_.Settings.ExecutionTimeLimit
    Actions=($_.Actions | % { ($_.Execute + ' ' + $_.Arguments).Trim() }) -join ' || '
  }
} | Format-Table -AutoSize
```

When changing prod defaults, update this guide and the narrower runbook that owns the subsystem.
