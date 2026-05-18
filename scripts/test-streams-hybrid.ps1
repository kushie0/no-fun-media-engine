# Hybrid A/B test: streams 1/3/5 via new mediamtx (RTSP), streams 2/4 via old VLC (HTTP MPEG-TS).
# Lets TouchDesigner compare known-good (VLC) against the new path side-by-side.

param(
    [string]$ClipRoot = "D:\clips"
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$MediaMtx = Join-Path $RepoRoot "bin\mediamtx.exe"
if (-not (Test-Path $MediaMtx)) { throw "missing $MediaMtx - see bin\README.md" }

# Clean slate
Get-Process vlc,ffmpeg,mediamtx -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1

$Tmp = Join-Path $env:TEMP ("hybrid-streams-" + [guid]::NewGuid().ToString())
New-Item -ItemType Directory $Tmp | Out-Null

# mediamtx config -- only stream1, stream3, stream5
$Yml = @"
logLevel: info
rtspAddress: :8554
rtspTransports: [tcp]
hlsAddress:  :8888
webrtcAddress: :8889
paths:
  stream1: {}
  stream3: {}
  stream5: {}
"@
Set-Content -Path "$Tmp\mediamtx.yml" -Value $Yml

$mtx = Start-Process -FilePath $MediaMtx -ArgumentList "$Tmp\mediamtx.yml" `
        -RedirectStandardOutput "$Tmp\mediamtx.log" -RedirectStandardError "$Tmp\mediamtx.err" `
        -WindowStyle Hidden -PassThru

$ready = $false
for ($i = 0; $i -lt 10; $i++) {
    try { $null = [System.Net.Sockets.TcpClient]::new('localhost', 8554); $ready = $true; break } catch { }
    Start-Sleep -Seconds 1
}
if (-not $ready) { throw "mediamtx failed to start - check $Tmp\mediamtx.log" }

$LocalIP = (Get-NetIPAddress -AddressFamily IPv4 -PrefixOrigin Manual,Dhcp |
            Where-Object { $_.IPAddress -notlike '169.*' -and $_.IPAddress -ne '127.0.0.1' } |
            Select-Object -First 1).IPAddress

$AllClips = Get-ChildItem -Path $ClipRoot -Recurse -Include *.mp4,*.mov
Write-Host "Found $($AllClips.Count) clips under $ClipRoot"
Write-Host ""

$procs = @($mtx)
try {
    # === streams 1, 3, 5: new mediamtx RTSP ===
    foreach ($i in 1,3,5) {
        $pls = "$Tmp\pls_mediamtx_$i.txt"
        "ffconcat version 1.0" | Set-Content $pls
        $AllClips | Sort-Object { Get-Random } | ForEach-Object { "file '$($_.FullName)'" } | Add-Content $pls

        $ffmpegArgs = @(
            "-hide_banner","-loglevel","warning",
            "-fflags","+genpts+igndts","-re",
            "-f","concat","-safe","0","-stream_loop","-1","-i",$pls,
            "-c:v","libx264","-preset","ultrafast","-tune","zerolatency",
            "-g","30","-keyint_min","30","-sc_threshold","0",
            "-pix_fmt","yuv420p",
            "-an",
            "-f","rtsp","-rtsp_transport","tcp",
            "rtsp://localhost:8554/stream$i"
        )
        $p = Start-Process -FilePath "ffmpeg" -ArgumentList $ffmpegArgs `
                -RedirectStandardOutput "$Tmp\ffmpeg_$i.log" `
                -RedirectStandardError  "$Tmp\ffmpeg_$i.err" `
                -WindowStyle Hidden -PassThru
        $procs += $p
        Write-Host "stream$i  [NEW mediamtx RTSP]   rtsp://${LocalIP}:8554/stream$i"
    }

    # === streams 2, 4: old VLC HTTP MPEG-TS ===
    foreach ($i in 2,4) {
        $port = 8554 + $i - 1     # stream2 -> 8555, stream4 -> 8557
        $plist = "$Tmp\pls_vlc_$port.m3u"
        $AllClips | Sort-Object { Get-Random } | Select-Object -ExpandProperty FullName | Out-File -Encoding UTF8 $plist

        $vlcArgs = @(
            '--intf','dummy','--random','--loop','--playlist-autostart',
            '--file-caching','3000','--network-caching','2000',
            $plist,
            '--sout',"#transcode{vcodec=h264,acodec=none}:http{mux=ts,dst=:$port/video}"
        )
        $p = Start-Process vlc -ArgumentList $vlcArgs -WindowStyle Hidden -PassThru
        $procs += $p
        Write-Host "stream$i  [OLD VLC HTTP-MPEGTS] http://${LocalIP}:${port}/video"
    }

    Write-Host ""
    Write-Host "1/3/5 = new mediamtx RTSP. 2/4 = old VLC HTTP MPEG-TS. Press Enter to stop."
    Read-Host
} finally {
    $procs | ForEach-Object { try { $_.Kill() } catch { } }
    Get-Process vlc -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
}
