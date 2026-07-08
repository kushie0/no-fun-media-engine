# start-streams.ps1 — NOFUN venue stream launcher + in-place refresher
#
# Serves StreamCount HTTP-MPEG-TS feeds (one per venue screen) that TouchDesigner
# consumes via Video Stream In TOPs. Each feed is a VLC process that transcodes a
# shuffled clip playlist to H.264/TS on http://<lan>:<port>/video.
#
# Two modes:
#   (launch)            start the streams (kills any prior VLC on each port first)
#   start-streams.ps1 -Refresh   re-sample each playlist and swap it IN PLACE via VLC's
#                                RC interface, WITHOUT restarting the process.
#
# Why in-place refresh: the old launcher refreshed by killing+restarting each VLC,
# which dropped TouchDesigner's TCP connection every cycle. Repeated reconnects are
# the suspected aggravator of TD's AppHang (see docs/active/td-hang-investigation.md).
# `--sout-keep` holds the encoder+HTTP output alive across the playlist swap, so the
# RC refresh changes content with ZERO reconnect for TD. Validated 2026-06-26:
# TD's socket (LocalPort/CreationTime) is unchanged across a -Refresh, stream keeps
# flowing. See docs/active/streamer-evolution-and-nas.md for the full history.

param(
    [string]$ClipRoot    = $(if ($env:CLIPS_ROOT) { $env:CLIPS_ROOT } else { "C:\clips" }),
    [int]   $BasePort    = 8554,
    [int]   $StreamCount = 5,
    [int]   $PlaylistSize = 500,
    # Playlist composition. VLC --random picks one slot per item, so play frequency
    # tracks the share. tonight / recent / library ~= 33 / 33 / 34 (library = remainder).
    [double]$TodayShare  = 0.33,
    [int]   $RecentDays  = 21,
    [double]$RecentShare = 0.33,
    # RC control port per stream = RcBase + i (loopback only).
    [int]   $RcBase      = 9554,
    [switch]$Refresh
)

$ErrorActionPreference = 'Stop'
$LogFile = Join-Path $env:TEMP 'nofun-streams.log'
function Log($m) {
    $line = ('{0}  {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m)
    Write-Host $line
    try { Add-Content -Path $LogFile -Value $line } catch { }
}

# Singleton: two concurrent runs race the per-port kill/launch and strand duplicate
# VLCs fighting over a port (seen 2026-06-12). Refresh runs are also serialised.
$mutex = New-Object System.Threading.Mutex($false, 'Global\NofunStartStreams')
if (-not $mutex.WaitOne(0)) { Log 'another run in progress - exiting'; exit 0 }

function Get-LocalIPs {
    if ($env:STREAM_IP) { return @($env:STREAM_IP) }
    @(Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
        Select-Object -ExpandProperty IPAddress)
}

# ---- scan clip tree once + bucket it ----
$allClips = @(Get-ChildItem -Path $ClipRoot -Recurse -Filter *.mp4 |
    Where-Object { $_.DirectoryName -ne $ClipRoot })
if ($allClips.Count -eq 0) { Log "no clips under $ClipRoot - aborting"; exit 1 }
$tonightPrefix = (Get-Date).ToString('yy-MM-dd')
$todayClips  = @($allClips | Where-Object { $_.Name -like "$tonightPrefix*" })
$recentCut   = (Get-Date).AddDays(-$RecentDays)
$recentClips = @($allClips | Where-Object {
    $_.LastWriteTime -gt $recentCut -and -not ($_.Name -like "$tonightPrefix*") })
Log ("mode={0} clips={1} tonight={2} recent={3}" -f $(if ($Refresh) { 'refresh' } else { 'launch' }), $allClips.Count, $todayClips.Count, $recentClips.Count)

# Build one PlaylistSize-clip sample: fill tonight, then recent, then library.
function Build-Picks {
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

function Test-RcAlive([int]$rcPort) {
    try {
        $c = [System.Net.Sockets.TcpClient]::new()
        $iar = $c.BeginConnect('127.0.0.1', $rcPort, $null, $null)
        $ok = $iar.AsyncWaitHandle.WaitOne(700)
        if ($ok -and $c.Connected) { $c.Close(); return $true }
        $c.Close(); return $false
    } catch { return $false }
}

function Send-Rc([int]$rcPort, [string[]]$cmds) {
    $c = [System.Net.Sockets.TcpClient]::new('127.0.0.1', $rcPort)
    $w = [System.IO.StreamWriter]::new($c.GetStream()); $w.AutoFlush = $true
    foreach ($l in $cmds) { $w.WriteLine($l) }
    Start-Sleep -Milliseconds 300
    $w.WriteLine('logout'); $c.Close()
}

function Launch-Port([int]$port, [int]$rc, $picks) {
    $plist = Join-Path $env:TEMP "pls_$port.m3u"
    $picks | Select-Object -ExpandProperty FullName | Out-File -Encoding UTF8 $plist
    @(Get-CimInstance Win32_Process -Filter "Name = 'vlc.exe'" |
        Where-Object { $_.CommandLine -like "*dst=:$port/video*" }) |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    $vlcArgs = @(
        '--intf', 'dummy',
        '--extraintf', 'rc', '--rc-host', "127.0.0.1:$rc",
        '--random', '--loop', '--playlist-autostart',
        '--file-caching', '300', '--network-caching', '2000',
        $plist,
        '--sout', "#transcode{vcodec=h264,acodec=none}:http{mux=ts,dst=:$port/video}",
        '--sout-keep'
    )
    Start-Process vlc -ArgumentList $vlcArgs -WindowStyle Hidden
    Log "launched port $port (rc 127.0.0.1:$rc)"
}

function Refresh-Port([int]$port, [int]$rc, $picks) {
    # Self-heal: if the VLC/RC for this port is gone, relaunch it instead of refreshing.
    if (-not (Test-RcAlive $rc)) { Log "port $port rc dead - relaunching"; Launch-Port $port $rc $picks; return }
    # Live swap: clear old list, start first new clip, queue the rest. --sout-keep keeps
    # the HTTP output (and TD's socket) alive across the swap.
    $cmds = @('clear', ('add ' + $picks[0].FullName))
    if ($picks.Count -gt 1) {
        foreach ($p in $picks[1..($picks.Count - 1)]) { $cmds += ('enqueue ' + $p.FullName) }
    }
    try { Send-Rc $rc $cmds; Log "refreshed port $port ($($picks.Count) clips)" }
    catch { Log "rc refresh FAILED port $port - relaunching: $_"; Launch-Port $port $rc $picks }
}

$localIPs = Get-LocalIPs
for ($i = 0; $i -lt $StreamCount; $i++) {
    $port = $BasePort + $i
    $rc   = $RcBase + $i
    $picks = Build-Picks
    if ($Refresh) {
        Refresh-Port $port $rc $picks
    } else {
        Launch-Port $port $rc $picks
        foreach ($lip in $localIPs) { Log "stream $($i + 1): http://${lip}:$port/video" }
        Start-Sleep -Seconds 8   # stagger boots so the box isn't slammed
    }
}
$mutex.ReleaseMutex()
Log 'done'
