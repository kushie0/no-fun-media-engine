@echo off
REM Fast-tier NAS audio deliverable integrity audit. Read-only.
REM Run from the console session (Interactive scheduled task) so the NAS SMB
REM session is reachable; output lands in D:\tmp for SSH pickup.
REM For the weekly heavy sweep, append --deep.
cd /d C:\Users\NOFUNadmin\clips
uv run python scripts\nas_audit.py ^
  --nas-audio "\\192.168.0.232\nofun-archive\audio" ^
  --d-zip "D:\audio" ^
  --d-archive "D:\audio_archive" ^
  --out "D:\tmp\nas_audit_out.txt" ^
  --json "D:\tmp\nas_audit_out.json"
