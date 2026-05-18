@echo off
:: Clips live on C: (SSD) to avoid D: HDD contention with VLC streaming.
:: Unset or change CLIPS_ROOT to relocate clip outputs.
set "CLIPS_ROOT=C:\clips"
set TMUX_EXE=
where tmux >nul 2>&1
if %ERRORLEVEL% == 0 set TMUX_EXE=tmux
if "%TMUX_EXE%"=="" if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\tmux.exe" set TMUX_EXE=%LOCALAPPDATA%\Microsoft\WinGet\Links\tmux.exe
if not "%TMUX_EXE%"=="" (
    %TMUX_EXE% kill-session -t engine 2>nul
    %TMUX_EXE% new-session -d -s engine "powershell -NoExit -Command Set-Location '%~dp0'; uv run python media_engine.py"
    start "" wt.exe new-tab --title "NOFUN Engine" powershell.exe -Command "%TMUX_EXE% attach -t engine" ; new-tab --title "NOFUN Streams" powershell.exe -ExecutionPolicy Bypass -File "%~dp0start-streams.ps1"
) else (
    where wt.exe >nul 2>&1
    if %ERRORLEVEL% == 0 (
        start "" wt.exe -d "%~dp0" powershell.exe -NoExit -Command "uv run python media_engine.py"
    ) else (
        start "" powershell.exe -NoExit -Command "cd '%~dp0'; uv run python media_engine.py"
    )
)
