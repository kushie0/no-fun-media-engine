# Sync D:\clips → C:\clips so stream scripts keep reading from C:.
# Intended to run as a Windows scheduled task (every 15 minutes).
# D: is primary; this is a one-way copy only.

$logDir = "C:\logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

robocopy "D:\clips" "C:\clips" /MIR /Z /MT:4 /NP /R:2 /W:5 /LOG+:"$logDir\clips-to-c.log"
