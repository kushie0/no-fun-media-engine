@echo off
REM Silent periodic invoke. start-streams.ps1's idle guard makes this a no-op
REM when the clip tree hasn't changed since the last playlist write, so the
REM schtask can fire every N minutes without disturbing screens.
powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0start-streams.ps1"
