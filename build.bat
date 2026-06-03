@echo off
cd /d "%~dp0"
echo Building SeqManager.exe...
pyinstaller SeqManager.spec --clean
echo.
if exist dist\SeqManager.exe (
    echo BUILD OK  --  dist\SeqManager.exe
    powershell -command "Add-Type -AssemblyName System.Windows.Forms; $n=New-Object System.Windows.Forms.NotifyIcon; $n.Icon=[System.Drawing.SystemIcons]::Application; $n.Visible=$true; $n.ShowBalloonTip(6000,'SeqManager','BUILD OK','Info'); Start-Sleep -s 3; $n.Dispose()" 2>nul
) else (
    echo BUILD FAILED
    powershell -command "Add-Type -AssemblyName System.Windows.Forms; $n=New-Object System.Windows.Forms.NotifyIcon; $n.Icon=[System.Drawing.SystemIcons]::Error; $n.Visible=$true; $n.ShowBalloonTip(6000,'SeqManager','BUILD FAILED','Error'); Start-Sleep -s 3; $n.Dispose()" 2>nul
)
pause
