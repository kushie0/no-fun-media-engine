# Mirror D:\clips to NAS. Intended to run as a Windows scheduled task (hourly).
# NAS credentials are in cmdkey for NOFUNadmin (set 2026-05-24).

$logDir = "C:\logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

robocopy "D:\clips" "\\192.168.0.232\nofun-archive\clips" /MIR /Z /MT:4 /NP /R:2 /W:5 /LOG+:"$logDir\nas-clips-mirror.log"
