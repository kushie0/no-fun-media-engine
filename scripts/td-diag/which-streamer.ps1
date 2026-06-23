#requires -Version 5
<#
.SYNOPSIS
  Read-only: identify which streamer is feeding TouchDesigner *right now*.

  Two streamers exist in this repo: the VLC path (start-streams.ps1) and the
  engine's Python StreamServer (nofun/streams.py). You cannot correlate a TD
  hang against stream behaviour without knowing which one is actually live.
  This script only observes -- it restarts nothing and is safe during a show.

.EXAMPLE
  powershell -NoProfile -File which-streamer.ps1
#>
param(
    [int]$BasePort    = 8554,
    [int]$StreamCount = 5
)

$ports = $BasePort..($BasePort + $StreamCount - 1)

Write-Host "=== Stream producers ($(Get-Date -Format o)) ===" -ForegroundColor Cyan

# --- VLC path (start-streams.ps1) ---
$vlc = Get-Process vlc -ErrorAction SilentlyContinue
if ($vlc) {
    Write-Host "VLC: $($vlc.Count) process(es) -- start-streams.ps1 path is LIVE" -ForegroundColor Yellow
    $vlc | Select-Object Id, CPU,
        @{n = 'WS_MB'; e = { [int]($_.WorkingSet64 / 1MB) } }, StartTime |
        Format-Table -AutoSize
}
else {
    Write-Host "VLC: none running"
}

# --- Python StreamServer path (engine) ---
$py = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'media_engine' }
if ($py) {
    Write-Host "Engine python (StreamServer path): PID(s) $($py.ProcessId -join ', ')" -ForegroundColor Yellow
}
else {
    Write-Host "Engine python: not detected via media_engine cmdline"
}

# --- Who owns the listeners + who is connected (this is the ground truth) ---
Write-Host "`n=== Port ownership + live clients ===" -ForegroundColor Cyan
foreach ($p in $ports) {
    $listen = Get-NetTCPConnection -State Listen -LocalPort $p -ErrorAction SilentlyContinue
    if ($listen) {
        foreach ($l in $listen) {
            $proc = Get-Process -Id $l.OwningProcess -ErrorAction SilentlyContinue
            Write-Host ("port {0} LISTEN  pid {1} ({2})" -f $p, $l.OwningProcess, $proc.ProcessName)
        }
    }
    else {
        Write-Host ("port {0} LISTEN  (nobody)" -f $p) -ForegroundColor Red
    }
    $est = Get-NetTCPConnection -State Established -LocalPort $p -ErrorAction SilentlyContinue
    foreach ($e in $est) {
        $proc = Get-Process -Id $e.OwningProcess -ErrorAction SilentlyContinue
        Write-Host ("   client {0}:{1}  (server pid {2} {3})" -f $e.RemoteAddress, $e.RemotePort, $e.OwningProcess, $proc.ProcessName)
    }
}

Write-Host "`nThe producer holding the listeners above is what feeds TD." -ForegroundColor Cyan
