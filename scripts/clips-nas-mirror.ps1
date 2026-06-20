# Hourly mirror of the clips primary (C:\clips) to the NAS. Copy-only (/E) — never deletes.
# Run as an Interactive scheduled task: the console logon session already has the NAS SMB session.
param(
        [string]$Src = 'C:\clips',
        [string]$Dst = '\\192.168.0.232\nofun-archive\clips',
        [string]$Log = "$env:USERPROFILE\clips-nas-mirror.log"
)

# Hide our own console window. The ClipsNasMirror task launches powershell without
# -WindowStyle Hidden, and changing the task action requires the stored run-as
# password, so we self-hide here instead. Best-effort — a brief flash on launch is
# possible before the window is hidden; robocopy output still goes to $Log.
try {
        $win = Add-Type -PassThru -Name NfWin -Namespace NfNative -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("kernel32.dll")] public static extern System.IntPtr GetConsoleWindow();
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool ShowWindow(System.IntPtr hWnd, int nCmdShow);
'@
        [void]$win::ShowWindow($win::GetConsoleWindow(), 0)  # 0 = SW_HIDE
} catch { }

if (-not (Test-Path $Dst -PathType Container)) {
    Add-Content $Log "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  SKIP - NAS unreachable"
    exit 1
}
robocopy $Src $Dst /E /COPY:DAT /Z /R:2 /W:5 /NP /NDL /NFL /LOG+:$Log
