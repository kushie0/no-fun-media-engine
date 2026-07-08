param(
    [string]$ClipRoot = "C:\clips",
    [int]$StreamCount = 5
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$MediaMtx = Join-Path $RepoRoot "bin\mediamtx.exe"
if (-not (Test-Path $MediaMtx)) { throw "missing $MediaMtx - see bin\README.md" }

$Tmp = Join-Path $env:TEMP ("mediamtx-nofun-" + [guid]::NewGuid().ToString())
New-Item -ItemType Directory $Tmp | Out-Null

# Build mediamtx.yml
$Yml = @"
logLevel: warn
rtspAddress: :8554
hlsAddress:  :8888
webrtcAddress: :8889
paths:
"@
1..$StreamCount | ForEach-Object { $Yml += "`n  stream$_`: {}" }
Set-Content -Path "$Tmp\mediamtx.yml" -Value $Yml

# Spawn mediamtx (hidden)
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

$ffmpegs = @()
try {
    1..$StreamCount | ForEach-Object {
        $i = $_
        $pls = "$Tmp\pls_stream$i.txt"
        "ffconcat version 1.0" | Set-Content $pls
        Get-ChildItem -Path $ClipRoot -Recurse -Include *.mp4,*.mov |
            Sort-Object { Get-Random } |
            ForEach-Object { "file '$($_.FullName)'" } |
            Add-Content $pls

        $ffmpegArgs = @(
            "-hide_banner","-loglevel","warning",
            "-fflags","+genpts+igndts","-re",
            "-f","concat","-safe","0","-stream_loop","-1","-i",$pls,
            "-c:v","copy","-an","-bsf:v","h264_mp4toannexb",
            "-f","rtsp","-rtsp_transport","tcp",
            "rtsp://localhost:8554/stream$i"
        )
        $p = Start-Process -FilePath "ffmpeg" -ArgumentList $ffmpegArgs `
                -RedirectStandardOutput "$Tmp\ffmpeg_stream$i.log" `
                -RedirectStandardError  "$Tmp\ffmpeg_stream$i.err" `
                -WindowStyle Hidden -PassThru
        $ffmpegs += $p
        Write-Host "stream$i  rtsp://${LocalIP}:8554/stream$i   (HLS: http://${LocalIP}:8888/stream$i/index.m3u8)"
    }

    Write-Host ""
    Read-Host "Press Enter to stop"
} finally {
    $ffmpegs + $mtx | ForEach-Object { try { $_.Kill() } catch { } }
    Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
}