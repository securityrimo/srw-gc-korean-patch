@echo off
setlocal

if "%~1"=="" (
    echo [ERROR] A source Japanese ISO path is required.
    echo Usage: %~nx0 "source.iso" ["output.iso"]
    exit /b 2
)

if "%~2"=="" (
    powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0apply_patch.ps1" -SourceIso "%~1"
) else (
    powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0apply_patch.ps1" -SourceIso "%~1" -OutputIso "%~2"
)

set "PATCH_EXIT=%ERRORLEVEL%"
if not "%PATCH_EXIT%"=="0" (
    echo [ERROR] Patch application failed. ^(exit code: %PATCH_EXIT%^)
)

exit /b %PATCH_EXIT%
