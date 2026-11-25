# Code Monitor helper (Windows only).
# Started by the VS Code extension while a student is connected.
#  - Draws a thin click-through border around every screen:
#      orange = student is in an allowed app, red = in a forbidden app.
#  - Prints "APP:<process name>" to stdout whenever the foreground app changes.
#    Only the process name is reported, never window titles.
#  - Exits by itself when the VS Code process that started it is gone.
param(
    [int]$ParentPid = 0,
    [string]$Forbidden = ''
)

Add-Type -AssemblyName System.Windows.Forms, System.Drawing

Add-Type -ReferencedAssemblies System.Windows.Forms, System.Drawing -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class CmWin {
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
}
// Border strip: never takes focus, never receives clicks, hidden from Alt+Tab.
public class CmStrip : System.Windows.Forms.Form {
    protected override bool ShowWithoutActivation { get { return true; } }
    protected override System.Windows.Forms.CreateParams CreateParams {
        get {
            System.Windows.Forms.CreateParams cp = base.CreateParams;
            // TOOLWINDOW | NOACTIVATE | TRANSPARENT | TOPMOST
            cp.ExStyle |= 0x80 | 0x08000000 | 0x20 | 0x8;
            return cp;
        }
    }
}
"@

$forbiddenSet = @{}
foreach ($n in ($Forbidden -split ',')) { if ($n.Trim()) { $forbiddenSet[$n.Trim().ToLower()] = $true } }

$orange = [System.Drawing.Color]::FromArgb(255, 140, 0)
$red    = [System.Drawing.Color]::FromArgb(220, 38, 38)
# Real pixels (not scaled up on high-DPI screens): a 1px line, like a thin
# green/red frame line.
[void][CmWin]::SetProcessDPIAware()
$thick  = 2   # px (real pixels)
$strips = New-Object System.Collections.ArrayList

# Windows will not make a window thinner than ~14px, so each strip is $S px
# wide and only a $thick px line of it is kept visible (window Region).
$S = 16
foreach ($screen in [System.Windows.Forms.Screen]::AllScreens) {
    $b = $screen.Bounds
    # x, y, w, h of the strip, then the visible slice inside it
    $defs = @(
        @($b.X, $b.Y, $b.Width, $S,            0, 0, $b.Width, $thick),                       # top
        @($b.X, ($b.Y + $b.Height - $S), $b.Width, $S,  0, ($S - $thick), $b.Width, $thick), # bottom
        @($b.X, $b.Y, $S, $b.Height,           0, 0, $thick, $b.Height),                      # left
        @(($b.X + $b.Width - $S), $b.Y, $S, $b.Height, ($S - $thick), 0, $thick, $b.Height)  # right
    )
    foreach ($d in $defs) {
        $f = New-Object CmStrip
        $f.FormBorderStyle = 'None'
        $f.ShowInTaskbar   = $false
        $f.TopMost         = $true
        $f.StartPosition   = 'Manual'
        $f.BackColor       = $orange
        $f.Opacity         = 1.0
        $f.SetBounds($d[0], $d[1], $d[2], $d[3])
        $f.Region = New-Object System.Drawing.Region((New-Object System.Drawing.Rectangle($d[4], $d[5], $d[6], $d[7])))
        $f.Show()
        [void]$strips.Add($f)
    }
}

function Set-BorderColor($color) {
    foreach ($s in $strips) { $s.BackColor = $color }
}

$script:lastApp = ''
$ownPid = $PID

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 400
$timer.Add_Tick({
    if ($ParentPid -gt 0 -and -not (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue)) {
        [System.Windows.Forms.Application]::Exit()
        return
    }
    $h = [CmWin]::GetForegroundWindow()
    if ($h -eq [IntPtr]::Zero) { return }
    $fgPid = [uint32]0
    [void][CmWin]::GetWindowThreadProcessId($h, [ref]$fgPid)
    if ($fgPid -eq 0 -or $fgPid -eq $ownPid) { return }
    $p = Get-Process -Id $fgPid -ErrorAction SilentlyContinue
    if (-not $p) { return }
    $name = $p.ProcessName.ToLower()
    if ($name -ne $script:lastApp) {
        $script:lastApp = $name
        if ($forbiddenSet.ContainsKey($name)) { Set-BorderColor $red } else { Set-BorderColor $orange }
        [Console]::Out.WriteLine("APP:$name")
        [Console]::Out.Flush()
    }
})
$timer.Start()

[Console]::Out.WriteLine("READY")
[Console]::Out.Flush()
[System.Windows.Forms.Application]::Run()

foreach ($s in $strips) { $s.Close() }
