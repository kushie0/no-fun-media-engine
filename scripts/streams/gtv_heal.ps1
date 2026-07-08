# gtv_heal.ps1 — auto-discover Google TV sticks, assign each a DIFFERENT quad (/gtv1../gtvN,
# fixed by IP when configured, otherwise round-robin/first-come), and keep each one actually
# RECEIVING its feed (kiosk self-heal).
#
# Robust design (2026-07-03):
#  - Every adb call is timeout-bounded, so a wedged stick/adb can never hang the loop.
#  - Health = ACTUAL RECEPTION: is there an ESTABLISHED TCP connection from the stick to the stream
#    port? (Not "is VLC the foreground app" — a frozen VLC is foreground but receives nothing.)
#  - Recovery = a CLEAN VLC restart (force-stop + start), not a re-fired intent (which a frozen VLC
#    ignores), rate-limited so it can't churn while VLC boots + reconnects.
#
# Separate from tv_heal.ps1 (NDI /tv1,/tv2) so the two paths never interfere. Detection auto-manages
# any stick PAIRED once; brand-new hardware still needs a one-time on-screen pairing (Android).
param(
  [string]$RtspHost = '192.168.0.137',
  [int]$RtspPort = 8656,
  [int]$FeedCount = 4,
  [string]$Subnet = '192.168.0',          # /24 to scan for sticks
  [int]$AdbPort = 5555,
  [int]$HealSeconds = 10,                  # how often to check reception
  [int]$DiscoverEverySeconds = 60,         # how often to rescan the subnet for new sticks
  [int]$RestartCooldown = 30,              # min seconds between clean-restarts of the same stick
  [int]$ResetMinutes = 60,                 # periodic force-restart even while 'receiving' — clears a FROZEN VLC
  [int]$AdbTimeout = 8,                    # per-adb-call timeout (seconds)
  [int]$TelemetrySeconds = 300,            # how often to log per-stick wifi telemetry (RSSI/link speed)
  # Optional stable per-TV assignment, e.g. '192.168.0.242=gtv1,192.168.0.174=gtv2'.
  # Unlisted sticks still get round-robin feeds.
  [string]$StickFeedMap = '',
  # Runtime dir (for the heal log). Defaults to the current prod location; the streamer passes the same.
  [string]$Root = 'C:\Users\NOFUNadmin\clips\scratch\ndi',
  # adb lives one level up (shared platform-tools) — keep its own absolute default; do NOT move it.
  [string]$Adb = 'C:\Users\NOFUNadmin\clips\scratch\platform-tools\adb.exe',
  # Local fallback screen packaged in the tv-boot APK. This is intentionally on-stick, not streamed.
  [string]$LogoPackage = 'com.nofun.tvboot',
  [string]$LogoActivity = 'com.nofun.tvboot/.LogoActivity',
  [int]$PostRestartWaitSeconds = 30,
  [int]$PostRestartRetrySeconds = 5,
  [string]$Log = (Join-Path $Root 'gtv_heal.log')
)

function L($m) { ('{0}  {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) | Add-Content -Path $Log }

function Normalize-Feed([string]$feed) {
  $f = $feed.Trim()
  if ($f -match '/(gtv\d+)$') { return $Matches[1] }
  $f = $f.TrimStart('/')
  if ($f -match '^gtv\d+$') { return $f }
  throw "invalid feed '$feed' in -StickFeedMap (expected gtvN or /gtvN)"
}

function Parse-StickFeedMap([string]$map) {
  $out = @{}
  if ([string]::IsNullOrWhiteSpace($map)) { return $out }
  foreach ($entry in ($map -split '[,;]')) {
    if ([string]::IsNullOrWhiteSpace($entry)) { continue }
    $parts = $entry -split '=', 2
    if ($parts.Count -ne 2) { throw "invalid -StickFeedMap entry '$entry' (expected ip=gtvN)" }
    $ip = $parts[0].Trim()
    if ($ip -notmatch '^\d+\.\d+\.\d+\.\d+$') { throw "invalid stick IP '$ip' in -StickFeedMap" }
    $out[$ip] = Normalize-Feed $parts[1]
  }
  return $out
}

# Run adb bounded by a timeout; kill it if it exceeds. Returns stdout, or $null on timeout.
$OutTmp = Join-Path $env:TEMP ("gtvadb_o_{0}.tmp" -f $PID)
$ErrTmp = Join-Path $env:TEMP ("gtvadb_e_{0}.tmp" -f $PID)
function AdbCall([string[]]$a, [int]$timeoutSec = $AdbTimeout) {
  try {
    $p = Start-Process -FilePath $Adb -ArgumentList $a -NoNewWindow -PassThru `
      -RedirectStandardOutput $OutTmp -RedirectStandardError $ErrTmp
  } catch { return $null }
  if (-not $p.WaitForExit($timeoutSec * 1000)) {
    try { $p.Kill() } catch { }
    return $null
  }
  return (Get-Content $OutTmp -Raw -ErrorAction SilentlyContinue)
}

# Async TCP sweep of $Subnet.1-254:$AdbPort — fire all connects, wait once, collect the open ones.
function Discover-Sticks {
  $pending = New-Object System.Collections.Generic.List[object]
  foreach ($i in 1..254) {
    try {
      $c = [System.Net.Sockets.TcpClient]::new()
      $iar = $c.BeginConnect("$Subnet.$i", $AdbPort, $null, $null)
      $pending.Add([pscustomobject]@{ ip = "$Subnet.$i"; c = $c; iar = $iar })
    } catch { }
  }
  Start-Sleep -Milliseconds 700
  $found = New-Object System.Collections.Generic.List[string]
  foreach ($p in $pending) {
    if ($p.iar.IsCompleted -and $p.c.Connected) { $found.Add($p.ip) }
    try { $p.c.Close() } catch { }
  }
  return $found
}

function Get-AdbDevices {
  $out = AdbCall @('devices') 5
  $devs = New-Object System.Collections.Generic.List[string]
  if ($out) {
    foreach ($line in ($out -split "`r?`n")) {
      if ($line -match '^(\d+\.\d+\.\d+\.\d+:\d+)\s+device\s*$') { $devs.Add($Matches[1]) }
    }
  }
  return $devs
}

# Is the stick actually pulling the stream? = an ESTABLISHED TCP conn from its IP to the RTSP port.
function Stick-Receiving([string]$stickIp) {
  $r = Get-NetTCPConnection -LocalPort $RtspPort -State Established -ErrorAction SilentlyContinue |
    Where-Object { $_.RemoteAddress -eq $stickIp }
  return [bool]$r
}

function Get-FocusedApp([string]$dev) {
  $w = AdbCall @('-s', $dev, 'shell', 'dumpsys', 'window') 5
  if (-not $w) { return '' }
  foreach ($line in ($w -split "`r?`n")) {
    if ($line -match 'mCurrentFocus=.*') { return $line.Trim() }
  }
  return ''
}

function Show-LocalLogo([string]$dev) {
  # Do not force-stop this package: force-stop marks it "stopped" and blocks future boot broadcasts
  # until an activity is manually launched. Starting LogoActivity is enough to foreground the local,
  # no-network fallback and also un-stops the package after install.
  AdbCall @('-s', $dev, 'shell', 'am', 'start', '-n', $LogoActivity) 5 | Out-Null
}

function Hide-LocalLogo([string]$dev) {
  # If the logo is foreground, BACK finishes LogoActivity without marking the package stopped.
  # Avoid sending BACK blindly while VLC is foreground; that can exit playback.
  $focus = Get-FocusedApp $dev
  if ($focus -like "*$LogoPackage*") {
    AdbCall @('-s', $dev, 'shell', 'input', 'keyevent', 'BACK') 3 | Out-Null
    Start-Sleep -Seconds 1
  }
}

function Start-Vlc([string]$dev, [string]$url) {
  AdbCall @('-s', $dev, 'shell', 'am', 'start', '-n',
    'org.videolan.vlc/.gui.video.VideoPlayerActivity', '-a', 'android.intent.action.VIEW',
    '-d', $url) | Out-Null
}

# Clean VLC restart: force-stop then relaunch on the feed. Used both to recover a not-receiving stick
# and as the periodic known-good reset that clears a frozen-but-connected VLC.
function Invoke-VlcRestart([string]$dev, [string]$url) {
  AdbCall @('connect', $dev) 5 | Out-Null
  Hide-LocalLogo $dev
  AdbCall @('-s', $dev, 'shell', 'am', 'force-stop', 'org.videolan.vlc') | Out-Null
  Start-Vlc $dev $url
}

# One bounded adb call for the stick's wifi link state. Returns 'rssi=-49 tx=175Mbps rx=87Mbps'
# — the fields that reveal a weak/marginal link (a collapsed Rx rate is the tell) — or '' if the
# stick doesn't answer in time. Read-only: never touches VLC, safe to call anytime.
function Get-WifiTel([string]$dev) {
  $w = AdbCall @('-s', $dev, 'shell', 'cmd', 'wifi', 'status') 6
  if (-not $w) { return '' }
  $rssi = if ($w -match 'RSSI:\s*(-?\d+)')        { $Matches[1] } else { '?' }
  $tx   = if ($w -match 'Tx Link speed:\s*(\d+)') { $Matches[1] } else { '?' }
  $rx   = if ($w -match 'Rx Link speed:\s*(\d+)') { $Matches[1] } else { '?' }
  return "rssi=$rssi tx=${tx}Mbps rx=${rx}Mbps"
}

$staticFeedByIp = Parse-StickFeedMap $StickFeedMap
$assign = @{}          # dev 'ip:5555' -> feed path (gtv1..gtvN)
$nextFeed = 1
$lastRestart = @{}     # dev -> last clean-restart time
$lastReset = @{}       # dev -> last periodic reset time (frozen-VLC safety net)
$receiving = @{}       # dev -> last known receiving state (for recovery logging)
$lastTel = @{}         # dev -> last wifi-telemetry log time (heartbeat throttle)
$lastDiscover = (Get-Date).AddYears(-1)
L "gtv heal v2 started (feeds=1..$FeedCount on ${RtspHost}:${RtspPort}, subnet=$Subnet.0/24, fixed=$($staticFeedByIp.Count), reception-based)"

while ($true) {
  # --- discover new sticks (throttled) ---
  if (((Get-Date) - $lastDiscover).TotalSeconds -ge $DiscoverEverySeconds) {
    foreach ($ip in (Discover-Sticks)) { AdbCall @('connect', "${ip}:$AdbPort") 5 | Out-Null }
    $lastDiscover = Get-Date
  }

  # --- assign a feed to any newly-seen device (round-robin, first-come) ---
  foreach ($dev in (Get-AdbDevices)) {
    if (-not $assign.ContainsKey($dev)) {
      $stickIp = $dev.Split(':')[0]
      if ($staticFeedByIp.ContainsKey($stickIp)) {
        $assign[$dev] = $staticFeedByIp[$stickIp]
      } else {
        # Pick the lowest-numbered feed no other stick already holds (static map OR a prior
        # round-robin pick) — an unlisted stick must never collide onto a feed already on screen
        # elsewhere. Only when every feed is taken (more sticks than feeds) do we double up.
        $taken = @($assign.Values)
        $free = 1..$FeedCount | Where-Object { $taken -notcontains "gtv$_" } | Select-Object -First 1
        if ($free) {
          $assign[$dev] = "gtv$free"
        } else {
          $assign[$dev] = "gtv$nextFeed"
          $nextFeed = ($nextFeed % $FeedCount) + 1
        }
      }
      L "assigned $dev -> /$($assign[$dev])"
    }
  }

  # --- health = reception; recover a non-receiving stick with a CLEAN restart (rate-limited) ---
  foreach ($dev in @($assign.Keys)) {
    $stickIp = $dev.Split(':')[0]
    $url = "rtsp://${RtspHost}:${RtspPort}/$($assign[$dev])"
    # --- wifi telemetry heartbeat (super-light: one adb call / stick / TelemetrySeconds) ---
    if (-not $lastTel.ContainsKey($dev) -or ((Get-Date) - $lastTel[$dev]).TotalSeconds -ge $TelemetrySeconds) {
      $t = Get-WifiTel $dev
      if ($t) { L "telemetry $dev $t" }
      $lastTel[$dev] = Get-Date
    }
    if (Stick-Receiving $stickIp) {
      if (-not $receiving[$dev]) { L "receiving $dev -> $url"; $receiving[$dev] = $true; $lastReset[$dev] = Get-Date }
      # A FROZEN VLC keeps its TCP conn (reads as 'receiving') but shows black — we can't detect that,
      # so force a clean restart every ResetMinutes to guarantee it clears.
      if ($lastReset.ContainsKey($dev) -and ((Get-Date) - $lastReset[$dev]).TotalMinutes -ge $ResetMinutes) {
        Invoke-VlcRestart $dev $url
        $lastReset[$dev] = Get-Date; $lastRestart[$dev] = Get-Date
        L "periodic reset $dev -> $url (clears a possible frozen VLC)"
      }
      continue
    }
    $receiving[$dev] = $false
    $now = Get-Date
    if ($lastRestart.ContainsKey($dev) -and ($now - $lastRestart[$dev]).TotalSeconds -lt $RestartCooldown) {
      Show-LocalLogo $dev
      continue   # gave it a restart recently; let VLC finish booting + connecting
    }
    $tel = Get-WifiTel $dev
    Invoke-VlcRestart $dev $url
    $lastRestart[$dev] = $now; $lastReset[$dev] = $now; $lastTel[$dev] = $now
    $deadline = (Get-Date).AddSeconds($PostRestartWaitSeconds)
    while ((Get-Date) -lt $deadline -and -not (Stick-Receiving $stickIp)) {
      Start-Sleep -Seconds $PostRestartRetrySeconds
      if (-not (Stick-Receiving $stickIp)) { Start-Vlc $dev $url }
    }
    if (Stick-Receiving $stickIp) {
      L ("restarted + receiving $dev -> $url" + $(if ($tel) { "  [$tel]" } else { '' }))
      $receiving[$dev] = $true
    } else {
      Show-LocalLogo $dev
      L ("restarted but still not receiving; showing local logo $dev -> $url" + $(if ($tel) { "  [$tel]" } else { '' }))
    }
  }

  Start-Sleep -Seconds $HealSeconds
}
