param([string]$Mode = "list")
Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class WinEnum {
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr h, uint msg, IntPtr w, IntPtr l);
    public delegate bool EnumProc(IntPtr h, IntPtr l);
}
"@
$wtPids = (Get-Process WindowsTerminal -ErrorAction SilentlyContinue).Id
$cb = [WinEnum+EnumProc]{
    param($h, $l)
    $pidOut = 0
    [void][WinEnum]::GetWindowThreadProcessId($h, [ref]$pidOut)
    if ($wtPids -contains $pidOut -and [WinEnum]::IsWindowVisible($h)) {
        $sb = New-Object Text.StringBuilder 256
        [void][WinEnum]::GetWindowText($h, $sb, 256)
        $title = $sb.ToString()
        if ($title) {
            if ($Mode -eq "list") {
                "{0} pid={1} title={2}" -f $h, $pidOut, $title
            } elseif ($title -match 'Douyin|MITM|_svc|saver|Forwarder|Console|Scroll') {
                [void][WinEnum]::SendMessage($h, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero)  # WM_CLOSE
                "CLOSED: $h $title"
            }
        }
    }
    return $true
}
[void][WinEnum]::EnumWindows($cb, [IntPtr]::Zero)
