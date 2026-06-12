# Hourly mirror of the clips primary (C:\clips) to the NAS. Copy-only (/E) — never deletes.
# Run as an Interactive scheduled task: the console logon session already has the NAS SMB session.
param(
        [string]$Src = 'C:\clips',
        [string]$Dst = '\\192.168.0.232\nofun-archive\clips',
        [string]$Log = "$env:USERPROFILE\clips-nas-mirror.log"
)
if (-not (Test-Path $Dst -PathType Container)) {
    Add-Content $Log "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  SKIP - NAS unreachable"
    exit 1
}
robocopy $Src $Dst /E /COPY:DAT /Z /R:2 /W:5 /NP /NDL /NFL /LOG+:$Log
