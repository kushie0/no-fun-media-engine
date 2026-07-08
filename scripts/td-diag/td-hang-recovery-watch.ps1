<#
.SYNOPSIS
  Recovery watchdog for the TouchDesigner NDI/capture wedge (see
  docs/active/2026-07_td-ndi-hang-prevention.md).

.DESCRIPTION
  The 2026-07-03 freeze was TD's main thread hard-blocked inside the NDI receive /
  DirectShow capture path for ~18 h (proven via dump-stack-scan.py). No amount of
  RAM/priority tuning prevents it, and TD's own scripts can't run while the main
  thread is wedged — so the only reliable RECOVERY is an *external* process that
  notices TD is non-responding and restarts it. This is that process.

  Detection is cheap and session-agnostic: Get-Process(...).Responding is false when
  the window message pump is stalled (the exact AppHang symptom). The watchdog tracks
  how long TD has been non-responding and, past a threshold, optionally captures a
  dump and relaunches TD with the *same* command line it was started with.

  MODES
    default        detect + log only (safe; recommended while actively developing TD)
    -AutoRestart   also kill + relaunch TD when wedged past the threshold

  IMPORTANT — session context. Relaunching a GUI app must happen in the interactive
  CONSOLE session or TD comes up with no GPU/display/device access (same failure
  class as the engine's NAS/console-session issue). Run this watchdog IN the console
  session (Task Scheduler "run only when user is logged on" / interactive), NOT from a
  bare SSH network-logon shell, when -AutoRestart is enabled. Detect-only mode works
  from any session.

.PARAMETER ProcessName      TD image name (default: TouchDesigner; use TouchPlayer for the runtime).
.PARAMETER PollSeconds      Poll cadence (default 10).
.PARAMETER WedgeSeconds     Continuous non-responding time before it's a wedge (default 45).
.PARAMETER AutoRestart      Kill + relaunch TD on wedge (default: off = detect/log only).
.PARAMETER DumpBeforeRestart  Capture one procdump before killing (needs procdump; ~3.8 GB). Default off.
.PARAMETER ProcDumpPath     Path to procdump.exe (default: search PATH + common tools dir).
.PARAMETER CooldownSeconds  Min seconds between auto-restarts, guards restart loops (default 600).
.PARAMETER LogDir           Log directory (default D:\tmp\td_hang_recovery).
.PARAMETER WhatIf           Dry run: log what it WOULD do on a wedge; take no action.

.EXAMPLE
  # Detect + log only (safe, run now):
  powershell -NoProfile -File td-hang-recovery-watch.ps1

.EXAMPLE
  # Full recovery, for show time (run in the console session):
  powershell -NoProfile -File td-hang-recovery-watch.ps1 -AutoRestart -DumpBeforeRestart
#>
[CmdletBinding()]
param(
  [string]$ProcessName = 'TouchDesigner',
  [int]$PollSeconds = 10,
  [int]$WedgeSeconds = 45,
  [switch]$AutoRestart,
  [switch]$DumpBeforeRestart,
  [string]$ProcDumpPath,
  [int]$CooldownSeconds = 600,
  [string]$LogDir = 'D:\tmp\td_hang_recovery',
  [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

function Write-Log {
  param([string]$Level, [string]$Msg)
  $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
  $line = "[$ts] $Level  $Msg"
  $file = Join-Path $LogDir ("recovery_{0}.log" -f (Get-Date -Format 'yyyyMMdd'))
  Add-Content -Path $file -Value $line
  Write-Host $line
}

function Resolve-ProcDump {
  if ($ProcDumpPath -and (Test-Path $ProcDumpPath)) { return $ProcDumpPath }
  $cmd = Get-Command procdump.exe -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  foreach ($p in @("$env:USERPROFILE\tools\procdump.exe", 'C:\Users\NOFUNadmin\tools\procdump.exe')) {
    if (Test-Path $p) { return $p }
  }
  return $null
}

# Capture the exact launch command (exe + args) so a relaunch is faithful.
function Get-TdLaunch {
  $p = Get-CimInstance Win32_Process -Filter "Name='$ProcessName.exe'" -ErrorAction SilentlyContinue | Select-Object -First 1
  if (-not $p) { return $null }
  return [pscustomobject]@{ Pid = $p.ProcessId; CommandLine = $p.CommandLine }
}

# Split a Win32 command line into exe path + argument string (handles the quoted-exe form).
function Split-CommandLine {
  param([string]$CommandLine)
  if ($CommandLine -match '^\s*"([^"]+)"\s*(.*)$') { return @($Matches[1], $Matches[2].Trim()) }
  $parts = $CommandLine -split '\s+', 2
  return @($parts[0], ($parts[1] | ForEach-Object { $_ }))
}

Write-Log 'START' ("watchdog up: proc=$ProcessName poll=${PollSeconds}s wedge=${WedgeSeconds}s " +
  "autoRestart=$($AutoRestart.IsPresent) dump=$($DumpBeforeRestart.IsPresent) whatIf=$($WhatIf.IsPresent)")

$launch = Get-TdLaunch
if ($launch) { Write-Log 'INFO' ("watching pid=$($launch.Pid): $($launch.CommandLine)") }
else { Write-Log 'WARN' "no $ProcessName process found yet; will keep polling" }

$lastCmd = if ($launch) { $launch.CommandLine } else { $null }
$notRespondingSince = $null
$episodeHandled = $false
$lastRestart = [datetime]::MinValue

while ($true) {
  try {
    $proc = Get-Process -Name $ProcessName -ErrorAction SilentlyContinue | Select-Object -First 1

    if (-not $proc) {
      if ($lastCmd) { Write-Log 'WARN' "$ProcessName not running" }
      Start-Sleep -Seconds $PollSeconds
      continue
    }

    # refresh the captured launch command while healthy
    $cur = Get-TdLaunch
    if ($cur -and $cur.CommandLine) { $lastCmd = $cur.CommandLine }

    if ($proc.Responding) {
      if ($notRespondingSince) {
        $dur = [int]((Get-Date) - $notRespondingSince).TotalSeconds
        Write-Log 'RECOVERED' "TD responsive again after ${dur}s non-responding"
      }
      $notRespondingSince = $null
      $episodeHandled = $false
    }
    else {
      if (-not $notRespondingSince) {
        $notRespondingSince = Get-Date
        Write-Log 'STALL' "TD not responding (pid=$($proc.Id)); arming ${WedgeSeconds}s wedge timer"
      }
      $dur = [int]((Get-Date) - $notRespondingSince).TotalSeconds

      if ($dur -ge $WedgeSeconds -and -not $episodeHandled) {
        $ndi = Test-Connection -ComputerName '192.168.0.113' -Count 1 -Quiet -ErrorAction SilentlyContinue
        Write-Log 'WEDGE' "TD wedged ${dur}s (NDIGO .113 reachable=$ndi)"
        $episodeHandled = $true

        if (-not $AutoRestart -or $WhatIf) {
          Write-Log 'ACTION' ("would " + $(if ($DumpBeforeRestart) { 'dump + ' } else { '' }) +
            "restart: $lastCmd  [no-op: " + $(if ($WhatIf) { 'WhatIf' } else { 'AutoRestart off' }) + "]")
        }
        elseif (((Get-Date) - $lastRestart).TotalSeconds -lt $CooldownSeconds) {
          Write-Log 'SKIP' "within ${CooldownSeconds}s cooldown of last restart; not restarting"
        }
        else {
          if ($DumpBeforeRestart) {
            $pd = Resolve-ProcDump
            if ($pd) {
              $out = Join-Path $LogDir ("wedge_{0}.dmp" -f (Get-Date -Format 'yyyyMMdd_HHmmss'))
              Write-Log 'DUMP' "capturing $out via $pd"
              & $pd -accepteula -ma $proc.Id $out 2>&1 | Out-Null
            } else { Write-Log 'WARN' 'DumpBeforeRestart set but procdump not found; skipping dump' }
          }
          Write-Log 'KILL' "stopping wedged TD pid=$($proc.Id)"
          Stop-Process -Id $proc.Id -Force
          Start-Sleep -Seconds 3
          $exe, $args = Split-CommandLine $lastCmd
          Write-Log 'RELAUNCH' "starting: $exe $args"
          if ($args) { Start-Process -FilePath $exe -ArgumentList $args }
          else { Start-Process -FilePath $exe }
          $lastRestart = Get-Date
          $notRespondingSince = $null
        }
      }
    }
  }
  catch {
    Write-Log 'ERROR' $_.Exception.Message
  }
  Start-Sleep -Seconds $PollSeconds
}
