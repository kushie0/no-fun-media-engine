# Start-MjpegStreams.ps1

# NOTE
# CPU USAGE WILL BE VERY HIGH FOR A MINUTE
# IT CALMS DOWN I PROMISE :)

param(
        [string]$ClipRoot   = $(if ($env:CLIPS_ROOT) { $env:CLIPS_ROOT } else { "C:\clips" }),
        [int]$BasePort      = 8554,
        [int]$StreamCount   = 5,
        [int]$PlaylistSize  = 500,
        # Playlist composition (VLC --random picks one slot per item; clips are
        # STEP_SECONDS=40 s long, so picks/min = 1.5).
        # Tonight: clips whose filename date prefix matches TODAY's calendar date
        #   — sized for ~0.5 tonight pick/min/stream (0.33 × 1.5 ≈ 0.5). Matching
        #   today's date (not the newest prefix in the tree) keeps last night's gig
        #   out of the bucket; before tonight's clips exist it's empty and falls
        #   through to recent/library.
        # Recent: clips with mtime in the last RecentDays, excluding tonight.
        # Library: everything else; fills slack so playlists never under-fill.
        # Shares are an even split: tonight / recent / library ≈ 33 / 33 / 34
        # (library is the implicit remainder = 1 − TodayShare − RecentShare).
        [double]$TodayShare  = 0.33,
        [int]$RecentDays    = 21,
        [double]$RecentShare = 0.33
)

$host.UI.RawUI.WindowTitle = "NOFUN Streams"

# Singleton guard: two concurrent runs race the kill-on-start below and strand
# duplicate VLCs fighting over the same ports (seen 2026-06-12, streams unusable).
$mutex = New-Object System.Threading.Mutex($false, 'Global\NofunStartStreams')
if (-not $mutex.WaitOne(0)) {
        Write-Host 'Another start-streams run is already in progress - exiting. Let it finish.'
        exit 1
}

# Always rebuild on every invocation. The RefreshStreams task fires every 30 min
# and each playlist is only a random PlaylistSize-clip subset of the library, so we
# re-sample a fresh subset each run for variety — even when no new clips were added.
# (The old skip-guard exited early when the clip tree was unchanged, which froze the
# same subset on screen for hours.) The Mutex above still blocks overlapping runs,
# and the staggered per-port restart below keeps each screen dark only ~3 s at a time.

# Each port's VLC is killed and replaced inside the loop below (staggered
# refresh) — a rebuild blanks one venue screen for a few seconds at a time
# instead of all five at once.

# ---- helper: list the LAN IPs to advertise ----
# VLC binds to all interfaces (dst=:$port below), so the stream is reachable on
# every IP regardless. This is only for the printed URL. On a multi-homed host
# we print one line per real IPv4 address so it's unambiguous which to use.
# Set $env:STREAM_IP (machine-local, e.g. via setx) to advertise just one.
function Get-LocalIPs {
        if ($env:STREAM_IP) { return @($env:STREAM_IP) }
        @(Get-NetIPAddress -AddressFamily IPv4 |
                Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
                Select-Object -ExpandProperty IPAddress)
}

$localIPs = Get-LocalIPs

# ---- scan the clip tree ONCE (86k+ files; per-stream rescans took minutes each) ----
$allClips = @(Get-ChildItem -Path $ClipRoot -Recurse -Filter *.mp4 |
        Where-Object { $_.DirectoryName -ne $ClipRoot })

# Tonight = clips whose filename date prefix matches TODAY's calendar date.
# Filenames are yy-MM-dd_… so we match the current date directly rather than the
# newest prefix in the tree — that older "max prefix" logic grabbed last night's
# gig whenever tonight's clips didn't exist yet. Before tonight's show is
# recorded/encoded the bucket is simply empty and falls through to recent/library;
# it never falls back to last night. A set running past midnight (filed under its
# start date) stops counting once the date rolls over.
$tonightPrefix = (Get-Date).ToString('yy-MM-dd')
$todayClips = @($allClips | Where-Object { $_.Name -like "$tonightPrefix*" })

# Recent: by mtime, last RecentDays. Exclude tonight to avoid double-counting.
$recentCut   = (Get-Date).AddDays(-$RecentDays)
$recentClips = @($allClips | Where-Object {
        $_.LastWriteTime -gt $recentCut -and -not ($_.Name -like "$tonightPrefix*")
})
Write-Host ("Found {0} clips under {1} (tonight={2}: {3}, recent week: {4})" -f $allClips.Count, $ClipRoot, $tonightPrefix, $todayClips.Count, $recentClips.Count)

# ---- build one playlist per stream and launch VLC ----
for ($i = 0; $i -lt $StreamCount; $i++) {
        $port   = $BasePort + $i
        $plist  = "$env:TEMP\pls_$port.m3u"

        # VLC --random picks playlist entries uniformly, so play frequency =
        # slots occupied. Fill today first (clips repeat to fill the share when
        # the today pool is small), then recent, then any library to top up.
        # Empty today pool falls through naturally to recent/library.
        $picks = @()
        $todayTarget = [int]($PlaylistSize * $TodayShare)
        while ($todayClips.Count -gt 0 -and $picks.Count -lt $todayTarget) {
                $picks += $todayClips |
                        Get-Random -Count ([Math]::Min($todayClips.Count, $todayTarget - $picks.Count))
        }
        $recentTarget = $picks.Count + [int]($PlaylistSize * $RecentShare)
        while ($recentClips.Count -gt 0 -and $picks.Count -lt $recentTarget) {
                $picks += $recentClips |
                        Get-Random -Count ([Math]::Min($recentClips.Count, $recentTarget - $picks.Count))
        }
        $rest = [Math]::Min($allClips.Count, $PlaylistSize - $picks.Count)
        if ($rest -gt 0) { $picks += $allClips | Get-Random -Count $rest }
        $picks | Select-Object -ExpandProperty FullName | Out-File -Encoding UTF8 $plist

        # Replace this port's VLC only (kill → immediate restart below keeps
        # the gap to one VLC boot, ~3 s)
        $old = @(Get-CimInstance Win32_Process -Filter "Name = 'vlc.exe'" |
                Where-Object { $_.CommandLine -like "*dst=:$port/video*" })
        $old | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

        # VLC CLI
        $vlcArgs = @(
                '--intf', 'dummy',
                '--random',
                '--loop',
                '--playlist-autostart',
                # clips are on local SSD; 300ms buffer vs the old 3000 trims ~3s
                # of black between playlist items
                '--file-caching', '300',
                '--network-caching', '2000',
                $plist,
                '--sout', "#transcode{vcodec=h264,acodec=none}:http{mux=ts,dst=:$port/video}",
                # keep the encoder+mux alive across item changes instead of
                # tearing down and rebuilding the chain (the main gap source)
                '--sout-keep'
        )

        Start-Process vlc -ArgumentList $vlcArgs -WindowStyle Hidden

        foreach ($lip in $localIPs) {
                Write-Host "Stream $($i+1) ready at: http://$lip`:$port/video"
        }

        # Stagger: let this stream come back before touching the next port.
        # Cold start kills nothing, so boot skips the wait and stays fast.
        if ($old.Count -gt 0 -and $i -lt $StreamCount - 1) {
                Start-Sleep -Seconds 20
        }
}

Write-Host 'All streams started (running detached). VLC persists after this window closes; the next run replaces each port''s VLC one at a time (staggered refresh).'
