@echo off
REM Silent periodic invoke. start-streams.ps1 rebuilds on every run, re-sampling a
REM fresh random clip subset into each playlist (even with no new clips), so firing
REM this every 30 min keeps the streams varied. The staggered per-port restart in
REM the script keeps each screen dark only ~3 s at a time.
powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0start-streams.ps1"
