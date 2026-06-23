#requires -Version 5
<#
.SYNOPSIS
  Read-only connection-lifetime watcher for the venue stream ports.

  Logs every TCP connect/drop on the stream ports (8554-8558) with a wall-clock
  timestamp so a TD AppHang can be correlated against stream reconnects. This
  directly tests the "RefreshStreams churn / reconnect is the trigger" theory.

  It only reads TCP state (Get-NetTCPConnection) -- it restarts nothing and is
  safe to run during a live show.

.NOTES
  Run detached during a show:
    Start-Process powershell -ArgumentList `
      '-NoProfile','-File','C:\Users\NOFUNadmin\clips\scripts\td-diag\td-conn-watch.ps1' `
      -WindowStyle Hidden
  Stop: kill the powershell process (Get-Process powershell | ... ) or Ctrl-C if foreground.

  Output feeds scripts/td-diag/td-timeline.py via --conn-log.
#>
param(
    [int]$BasePort     = 8554,
    [int]$StreamCount  = 5,
    [double]$IntervalSec = 2.0,
    [string]$LogPath   = "D:\tmp\td_conn_watch.log"
)

$ports = $BasePort..($BasePort + $StreamCount - 1)
$dir = Split-Path -Parent $LogPath
if ($dir -and -not (Test-Path $dir)) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

function Write-Log([string]$msg) {
    $line = "[{0:yyyy-MM-ddTHH:mm:ss}] {1}" -f (Get-Date), $msg
    Add-Content -Path $LogPath -Value $line
    Write-Host $line
}

Write-Log "conn-watch START ports $($ports -join ',') interval ${IntervalSec}s"

# key: "port|remoteIp:remotePort" -> present while the connection is established
$known = @{}
while ($true) {
    $seen = @{}
    foreach ($p in $ports) {
        $est = Get-NetTCPConnection -State Established -LocalPort $p -ErrorAction SilentlyContinue
        foreach ($e in $est) {
            $key = "{0}|{1}:{2}" -f $p, $e.RemoteAddress, $e.RemotePort
            $seen[$key] = $true
            if (-not $known.ContainsKey($key)) {
                Write-Log "CONNECT  port $p  <- $($e.RemoteAddress):$($e.RemotePort)  (pid $($e.OwningProcess))"
            }
        }
    }
    foreach ($key in @($known.Keys)) {
        if (-not $seen.ContainsKey($key)) {
            $parts = $key -split '\|', 2
            Write-Log "DROP     port $($parts[0])  <- $($parts[1])"
        }
    }
    $known = $seen
    Start-Sleep -Seconds $IntervalSec
}
