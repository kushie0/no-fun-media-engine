# Start-MjpegStreams.ps1

# NOTE
# CPU USAGE WILL BE VERY HIGH FOR A MINUTE
# IT CALMS DOWN I PROMISE :)

param(
        [string]$ClipRoot   = $(if ($env:CLIPS_ROOT) { $env:CLIPS_ROOT } else { "D:\clips" }),
        [int]$BasePort      = 8554,
        [int]$StreamCount   = 5
)

$host.UI.RawUI.WindowTitle = "NOFUN Streams"

# kill VLC on start (-Force handles VLCs owned by a different session, e.g. a prior launch)
Get-Process vlc -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

# ---- helper: find local IP that faces the default route ----
function Get-LocalIP {
        $adapter = (Get-NetRoute -DestinationPrefix 0.0.0.0/0 | Where-Object -Property RouteMetric -EQ 0).InterfaceAlias
        (Get-NetIPAddress -InterfaceAlias $adapter -AddressFamily IPv4).IPAddress
}

$localIP = Get-LocalIP

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

        Write-Host "Stream $($i+1) ready at: http://$localIP`:$port/video"
}

Write-Host 'All streams started. Press <Enter> to terminate.'
Read-Host

# kill VLC on exit
Get-Process vlc -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
