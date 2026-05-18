$appdir = $PSScriptRoot

if (Get-Command wt.exe -ErrorAction SilentlyContinue) {
    Start-Process wt.exe -ArgumentList @(
        'new-tab', '--title', 'NOFUN Media Engine',
        '-d', $appdir,
        'powershell.exe', '-NoExit', '-Command', "Set-Location '$appdir'; uv run python media_engine.py"
    )
} else {
    Start-Process powershell.exe -ArgumentList @(
        '-NoExit', '-WorkingDirectory', $appdir,
        '-Command', "uv run python media_engine.py"
    )
}
