# Start-MjpegStreams.ps1

# NOTE
# CPU USAGE WILL BE VERY HIGH FOR A MINUTE
# IT CALMS DOWN I PROMISE :)

param(
        [string]$ClipRoot   = $(if ($env:CLIPS_ROOT) { $env:CLIPS_ROOT } else { "C:\clips" }),
        [int]$BasePort      = 8554,
        [int]$StreamCount   = 5
)

$host.UI.RawUI.WindowTitle = "NOFUN Streams"

# kill VLC on start (-Force handles VLCs owned by a different session, e.g. a prior launch)
Get-Process vlc -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

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

# ---- build one playlist per stream and launch VLC ----
for ($i = 0; $i -lt $StreamCount; $i++) {
        $port   = $BasePort + $i
        $plist  = "$env:TEMP\pls_$port.m3u"

        # random playlist
        Get-ChildItem -Path "$ClipRoot\*\*.mp4" -Recurse |
                Get-Random -Count ([int]::MaxValue) |
                Select-Object -ExpandProperty FullName |
                Out-File -Encoding UTF8 $plist

        # VLC CLI
        $vlcArgs = @(
                '--intf', 'dummy',
                '--random',
                '--loop',
                '--playlist-autostart',
                '--file-caching', '3000',
                '--network-caching', '2000',
                $plist,
                '--sout', "#transcode{vcodec=h264,acodec=none}:http{mux=ts,dst=:$port/video}"
        )

        Start-Process vlc -ArgumentList $vlcArgs -WindowStyle Hidden

        foreach ($lip in $localIPs) {
                Write-Host "Stream $($i+1) ready at: http://$lip`:$port/video"
        }
}

Write-Host 'All streams started (running detached). VLC persists after this window closes; the next relaunch kills and restarts it (see kill-on-start above).'
