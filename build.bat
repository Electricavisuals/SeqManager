@echo off
cd /d "%~dp0"
echo Building SeqManager.exe...
pyinstaller SeqManager.spec --clean
echo.
if exist dist\SeqManager.exe (
    echo BUILD OK  --  dist\SeqManager.exe
) else (
    echo BUILD FAILED
)
pause
