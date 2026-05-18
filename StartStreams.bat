@echo off
start "" wt.exe new-tab --title "NOFUN Streams" powershell.exe -ExecutionPolicy Bypass -File "%~dp0start-streams.ps1"
