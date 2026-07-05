param(
  [string]$ClipRoot = 'D:\clips',
  # Runtime dir: where mediamtx.exe lives and where logs / mtx-*.yml / *.ffconcat / cmd files are written.
  # Defaults to the current prod location so behavior is unchanged now that the SCRIPT lives in git.
  [string]$Root = 'C:\Users\NOFUNadmin\clips\scratch\ndi',
  # gtv runs its OWN mediamtx on :8656 (its own config file), fully isolated from the NDI bridge's
  # mediamtx on :8654 — so starting/stopping the gtv stack can never disturb NDI/TD (the pause lesson).
  [int]$RtspPort = 8656,
  # Software x264 on the Ryzen keeps the AMD GPU 100% free for TouchDesigner (fragile + GPU-sensitive).
  # A 640x360 libx264 stream is ~0.1-0.2 core. Switch to 'h264_amf' for GPU encode if CPU ever bottlenecks.
  [string]$Encoder = 'libx264',
  [string]$Preset = 'veryfast',
  # Clips are H.264 320x180. A native 2x2 quad is 640x360 (each cell 320x180 — no upscaling).
  # Encoding larger just upscales the same source: more bitrate/encode load, zero added detail.
  [string]$Bitrate = '2M',
  [int]$SwitchMinutes = 20,
  [int]$QuadMinutes = 15,
  [int]$Width = 640,
  [int]$Height = 360,
  [int]$PlaylistSize = 120,
  # Clips are a fixed length (STEP_SECONDS=40). We phase-shift the 4 quad cells by fractions of this
  # so they don't all cut at the same instant. Keep in sync with the real clip length.
  [int]$ClipSeconds = 40,
  # Clip selection mirrors start-streams.ps1 Build-Picks: bias the wall toward tonight + recent
  # footage. tonight (filename date == today) / recent (<= RecentDays, by mtime) / library, filled
  # ~TodayShare / RecentShare / remainder, re-sampled each cycle for variety.
  [double]$TodayShare = 0.33,
  [int]$RecentDays = 21,
  [double]$RecentShare = 0.33,
  # Re-scanning the ~87k-clip tree takes seconds; only do it this often. On a crash-relaunch we reuse
  # the cached list so the publisher returns in ~1-2s instead of ~30s.
  [int]$RescanMinutes = 60,
  # One RTSP feed per stream stick. Set this to how many sticks you're driving.
  [int]$FeedCount = 1,
  # Permanent 2x2: skip the single (full-frame) phase and run the quad continuously (refreshing the
  # playlist every SwitchMinutes). Drop this switch to restore the quad<->single rotation.
  [switch]$QuadOnly,
  [switch]$Worker,
  [string]$Path = '',
  [int]$OffsetSeconds = 0
)

$ErrorActionPreference = 'Stop'
$root = $Root
$mtx = Join-Path $root 'mediamtx.exe'
$logDir = $root

# One RTSP path per stick: /gtv1../gtvN, each phase-offset by cycle/N so no two sticks show the
# same quad/single phase at the same moment. /tv1,/tv2 stay reserved for the (parked) NDI bridge.
$feeds = @(0..($FeedCount - 1) | ForEach-Object {
  @{ path = ('gtv' + ($_ + 1)); offsetSeconds = [int]($_ * $SwitchMinutes * 60 / $FeedCount) }
})

function Test-PortOpen($port) {
  $client = $null
  try {
    $client = [System.Net.Sockets.TcpClient]::new('127.0.0.1', $port)
    return $true
  } catch {
    return $false
  } finally {
    if ($client) { $client.Close() }
  }
}

function Ensure-MediaMtx {
  if (Test-PortOpen $RtspPort) { return }
  if (-not (Test-Path $mtx)) { throw "missing mediamtx at $mtx" }
  # Port-specific config/logs so gtv's mediamtx (:8656) never clobbers the NDI bridge's mtx.yml (:8654).
  # rtspTransports:[tcp] refuses UDP so every reader (sticks, any decoder) is forced onto TCP —
  # kills Wi-Fi UDP packet-loss glitches. TCP is the mandatory RTSP transport, so it's universal.
  $cfg = Join-Path $root "mtx-$RtspPort.yml"
  # Disable every protocol except RTSP: a 2nd mediamtx instance otherwise collides with the NDI one on
  # the DEFAULT ports (RTMP 1935, HLS 8888, WebRTC 8889, API 9997, SRT 8890...) and exits on bind error.
  @"
rtspAddress: :$RtspPort
rtspTransports: [tcp]
rtmp: no
hls: no
webrtc: no
srt: no
moq: no
api: no
metrics: no
pprof: no
playback: no
paths:
  all_others:
"@ | Set-Content -Encoding ASCII $cfg
  Start-Process -FilePath $mtx -ArgumentList $cfg `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $root "mtx-$RtspPort.out.log") `
    -RedirectStandardError (Join-Path $root "mtx-$RtspPort.err.log")
  for ($i = 0; $i -lt 10; $i++) {
    Start-Sleep -Seconds 1
    if (Test-PortOpen $RtspPort) { return }
  }
  throw "mediamtx did not open :$RtspPort"
}

function Get-Clips {
  if (-not (Test-Path $ClipRoot)) { throw "clip root does not exist: $ClipRoot" }
  $clips = @(Get-ChildItem -Path $ClipRoot -Recurse -Filter *.mp4 |
    Where-Object { $_.DirectoryName -ne $ClipRoot })
  if ($clips.Count -lt 4) { throw "need at least 4 clips under $ClipRoot, found $($clips.Count)" }
  return $clips
}

function Escape-ConcatPath($path) {
  ([string]$path).Replace('\', '/').Replace("'", "'\''")
}

function Write-Playlist($name, $clips, $firstInpoint = 0) {
  $file = Join-Path $root "$name.ffconcat"
  $lines = New-Object System.Collections.Generic.List[string]
  $lines.Add('ffconcat version 1.0')
  $first = $true
  foreach ($clip in $clips) {
    $lines.Add("file '$(Escape-ConcatPath $clip.FullName)'")
    # inpoint on the FIRST clip only: start it partway in, so this cell's clip boundaries are
    # phase-shifted vs the others (concat-native — no external input seek, which the demuxer rejects).
    if ($first -and $firstInpoint -gt 0) { $lines.Add("inpoint $firstInpoint") }
    $first = $false
  }
  Set-Content -Encoding ASCII -Path $file -Value $lines
  return $file
}

# Split the clip list into recency buckets (ported from start-streams.ps1). Recomputed each cycle so
# "tonight" tracks newly-landed clips.
function Split-Buckets($allClips) {
  $tonightPrefix = (Get-Date).ToString('yy-MM-dd')
  $today = @($allClips | Where-Object { $_.Name -like "$tonightPrefix*" })
  $recentCut = (Get-Date).AddDays(-$RecentDays)
  $recent = @($allClips | Where-Object {
    $_.LastWriteTime -gt $recentCut -and -not ($_.Name -like "$tonightPrefix*") })
  return @{ today = $today; recent = $recent }
}

# Bucketed sampling, ported from start-streams.ps1 Build-Picks: fill from tonight first, then recent,
# then the whole library for the remainder. Re-sampled every call so the wall stays varied.
function Build-Picks($allClips, $todayClips, $recentClips) {
  $picks = @()
  $todayTarget = [int]($PlaylistSize * $TodayShare)
  while ($todayClips.Count -gt 0 -and $picks.Count -lt $todayTarget) {
    $picks += $todayClips | Get-Random -Count ([Math]::Min($todayClips.Count, $todayTarget - $picks.Count))
  }
  $recentTarget = $picks.Count + [int]($PlaylistSize * $RecentShare)
  while ($recentClips.Count -gt 0 -and $picks.Count -lt $recentTarget) {
    $picks += $recentClips | Get-Random -Count ([Math]::Min($recentClips.Count, $recentTarget - $picks.Count))
  }
  $rest = [Math]::Min($allClips.Count, $PlaylistSize - $picks.Count)
  if ($rest -gt 0) { $picks += $allClips | Get-Random -Count $rest }
  ,$picks
}

function New-BucketedPlaylist($name, $allClips, $todayClips, $recentClips, $firstInpoint = 0) {
  Write-Playlist $name (Build-Picks $allClips $todayClips $recentClips) $firstInpoint
}

function Invoke-Phase($phase, $seconds, $allClips, $todayClips, $recentClips, $loop = $false) {
  $rtspUrl = "rtsp://127.0.0.1:$RtspPort/$Path"
  $halfW = [int]($Width / 2)
  $halfH = [int]($Height / 2)
  # In loop mode each input carries -stream_loop -1 so ffmpeg NEVER hits EOF and NEVER exits — the RTSP
  # publisher (and thus the stick's stream) stays up indefinitely, no per-cycle teardown/reconnect.
  $lp = if ($loop) { @('-stream_loop', '-1') } else { @() }

  if ($phase -eq 'quad') {
    # Stagger the 4 cells so they don't all cut on the same 40s grid: each cell's first clip starts a
    # different fraction of ClipSeconds in (via ffconcat `inpoint`), phase-shifting its boundaries.
    # Result: one cell flips every ~ClipSeconds/4 s (10s) instead of all four every 40s.
    $sk = 0..3 | ForEach-Object { [int]($_ * $ClipSeconds / 4) }
    $p1 = New-BucketedPlaylist "$Path-quad-a" $allClips $todayClips $recentClips $sk[0]
    $p2 = New-BucketedPlaylist "$Path-quad-b" $allClips $todayClips $recentClips $sk[1]
    $p3 = New-BucketedPlaylist "$Path-quad-c" $allClips $todayClips $recentClips $sk[2]
    $p4 = New-BucketedPlaylist "$Path-quad-d" $allClips $todayClips $recentClips $sk[3]
    $filter = (
      "[0:v]fps=30,scale=${halfW}:${halfH},setpts=PTS-STARTPTS[q1];" +
      "[1:v]fps=30,scale=${halfW}:${halfH},setpts=PTS-STARTPTS[q2];" +
      "[2:v]fps=30,scale=${halfW}:${halfH},setpts=PTS-STARTPTS[q3];" +
      "[3:v]fps=30,scale=${halfW}:${halfH},setpts=PTS-STARTPTS[q4];" +
      "[q1][q2][q3][q4]xstack=inputs=4:layout=0_0|${halfW}_0|0_${halfH}|${halfW}_${halfH},format=yuv420p[out]"
    )
    $args = @('-hide_banner', '-loglevel', 'warning') +
      $lp + @('-re', '-f', 'concat', '-safe', '0', '-i', $p1) +
      $lp + @('-re', '-f', 'concat', '-safe', '0', '-i', $p2) +
      $lp + @('-re', '-f', 'concat', '-safe', '0', '-i', $p3) +
      $lp + @('-re', '-f', 'concat', '-safe', '0', '-i', $p4) +
      @('-filter_complex', $filter, '-map', '[out]')
  } else {
    $p1 = New-BucketedPlaylist "$Path-single" $allClips $todayClips $recentClips
    $args = @('-hide_banner', '-loglevel', 'warning') +
      $lp + @('-re', '-f', 'concat', '-safe', '0', '-i', $p1,
      '-vf', "fps=30,scale=${Width}:${Height},setpts=PTS-STARTPTS,format=yuv420p")
  }

  $venc = @('-c:v', $Encoder)
  if ($Encoder -match 'x26[45]') { $venc += @('-preset', $Preset) }   # preset only applies to software x26x
  $tail = @('-metadata', "comment=nofun_google_tv_$Path") + $venc + @(
    '-b:v', $Bitrate, '-pix_fmt', 'yuv420p', '-g', '60', '-an',
    '-f', 'rtsp', '-rtsp_transport', 'tcp', $rtspUrl
  )
  # -t caps a phase (rotation mode); loop mode has no cap so ffmpeg runs forever.
  if (-not $loop) { $tail = @('-t', [string]$seconds) + $tail }
  $args += $tail

  $cmdFile = Join-Path $logDir "$Path-$phase.cmd.txt"
  Set-Content -Encoding ASCII -Path $cmdFile -Value ("ffmpeg " + (($args | ForEach-Object { "`"$_`"" }) -join ' '))
  Write-Host "$(Get-Date -Format s) $Path $phase -> $rtspUrl ($(if ($loop) { 'loop' } else { "${seconds}s" }))"

  $proc = Start-Process -FilePath 'ffmpeg' -ArgumentList $args `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logDir "$Path-$phase.out.log") `
    -RedirectStandardError (Join-Path $logDir "$Path-$phase.err.log") `
    -PassThru
  $proc.WaitForExit()
  Write-Host "$(Get-Date -Format s) $Path $phase exited code $($proc.ExitCode)"
}

function Invoke-Worker {
  if (-not $Path) { throw '-Path is required with -Worker' }
  Ensure-MediaMtx
  $allClips = Get-Clips
  $buckets = Split-Buckets $allClips
  $lastScan = Get-Date
  $cycleSeconds = $SwitchMinutes * 60
  $quadSeconds = [Math]::Min($QuadMinutes * 60, $cycleSeconds)
  $singleSeconds = [Math]::Max(1, $cycleSeconds - $quadSeconds)

  # Offset only staggers the quad<->single rotation across feeds; in QuadOnly (permanent loop) it would
  # just black out feeds 2..N for minutes at startup, so skip it.
  if (-not $QuadOnly -and $OffsetSeconds -gt 0) {
    $delay = $OffsetSeconds % $cycleSeconds
    Write-Host "$(Get-Date -Format s) $Path initial offset sleep ${delay}s"
    Start-Sleep -Seconds $delay
  }

  while ($true) {
    if ($QuadOnly) {
      # Loop mode: ffmpeg runs forever (never tears down the publisher). If it ever exits (crash), the
      # loop re-enters and relaunches; only then do we rescan/rebuild for a fresh selection.
      Invoke-Phase 'quad' 0 $allClips $buckets.today $buckets.recent $true
    } else {
      Invoke-Phase 'quad' $quadSeconds $allClips $buckets.today $buckets.recent
      Invoke-Phase 'single' $singleSeconds $allClips $buckets.today $buckets.recent
    }
    # Reuse the cached clip list across relaunches (fast recovery); only re-scan periodically.
    if (((Get-Date) - $lastScan).TotalMinutes -ge $RescanMinutes) {
      $allClips = Get-Clips
      $buckets = Split-Buckets $allClips
      $lastScan = Get-Date
    }
  }
}

function Stop-PriorGoogleProcesses {
  Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
    Where-Object {
      $_.ProcessId -ne $PID -and
      $_.CommandLine -like '*google_tv_run.ps1*' -and
      $_.CommandLine -like '*-Worker*'
    } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

  Get-CimInstance Win32_Process -Filter "Name='ffmpeg.exe'" |
    Where-Object { $_.CommandLine -like '*nofun_google_tv_gtv*' -or $_.CommandLine -like '*:8656/gtv*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Invoke-Supervisor {
  Ensure-MediaMtx
  Stop-PriorGoogleProcesses
  $script = $PSCommandPath
  Write-Host "Google TV direct clip supervisor started. Existing /tv1 and /tv2 NDI paths are unchanged."
  foreach ($feed in $feeds) {
    $args = @(
      '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $script,
      '-Worker',
      '-Path', $feed.path,
      '-OffsetSeconds', [string]$feed.offsetSeconds,
      '-ClipRoot', $ClipRoot,
      '-Root', $Root,
      '-RtspPort', [string]$RtspPort,
      '-Encoder', $Encoder,
      '-Preset', $Preset,
      '-Bitrate', $Bitrate,
      '-SwitchMinutes', [string]$SwitchMinutes,
      '-QuadMinutes', [string]$QuadMinutes,
      '-Width', [string]$Width,
      '-Height', [string]$Height,
      '-PlaylistSize', [string]$PlaylistSize,
      '-ClipSeconds', [string]$ClipSeconds,
      '-TodayShare', [string]$TodayShare,
      '-RecentDays', [string]$RecentDays,
      '-RecentShare', [string]$RecentShare,
      '-RescanMinutes', [string]$RescanMinutes
    )
    if ($QuadOnly) { $args += '-QuadOnly' }
    Start-Process -FilePath 'powershell' -ArgumentList $args `
      -WindowStyle Hidden `
      -RedirectStandardOutput (Join-Path $logDir "$($feed.path)-worker.out.log") `
      -RedirectStandardError (Join-Path $logDir "$($feed.path)-worker.err.log")
    Write-Host "$($feed.path) -> rtsp://192.168.0.137:$RtspPort/$($feed.path)"
  }

  while ($true) { Start-Sleep -Seconds 60 }
}

if ($Worker) {
  Invoke-Worker
} else {
  Invoke-Supervisor
}
