# Self-serve teardown for the synthetic smoke-test fixture.
# Deletes every artifact a smoke run can leave across prod folders, so a run
# (including a failed/partial one) can be reset to a clean state and re-run.
# Does NOT touch the fixture source build dir (D:\smoke_build) or the source
# channels in audio_archive belonging to real performances.
#
# Run on prod (needs NAS auth in the same session):
#   net use \\192.168.0.232\nofun-archive <pw> /user:alex & \
#     powershell -ExecutionPolicy Bypass -File D:\smoke_build\smoke_cleanup.ps1
param(
    [string]$Perf = '01-01-01_SMOKETEST'   # date_band stem prefix
)
$nas = '\\192.168.0.232\nofun-archive'
$od  = Join-Path $env:USERPROFILE 'OneDrive - No Fun Troy LLC\Multitracks'
$vl  = Join-Path $env:USERPROFILE 'VenueLighting'

$globs = @(
    "$nas\videos\$Perf*"
    "$nas\audio\$Perf*"
    "$nas\video_archive\$Perf*"
    "$nas\audio_archive\$Perf*"
    "$vl\$Perf*"
    "$vl\Audio\$Perf*"
    "C:\clips\$Perf*"
    "$od\$Perf*"
)

$deleted = 0
foreach ($g in $globs) {
    Get-ChildItem -Path $g -Force -ErrorAction SilentlyContinue | ForEach-Object {
        Write-Host ("DEL  " + $_.FullName)
        Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
        $deleted++
    }
}
Write-Host ("smoke cleanup complete for {0}: {1} item(s) removed" -f $Perf, $deleted)
