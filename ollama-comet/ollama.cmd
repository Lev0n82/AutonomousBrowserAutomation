@echo off
setlocal
set "OLLAMA_BROWSER_SCRIPT=%~dp0Launch-AutonomousBrowser.ps1"
if /I "%~1"=="launch" if /I "%~2"=="comet" goto comet
if /I "%~1"=="launch" if /I "%~2"=="chrome" goto chrome
if /I "%~1"=="launch" if /I "%~2"=="edge" goto edge
"%LOCALAPPDATA%\Programs\Ollama\ollama.exe" %*
exit /b %ERRORLEVEL%

:comet
shift
shift
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%OLLAMA_BROWSER_SCRIPT%" -Browser comet %1 %2 %3 %4 %5 %6 %7 %8 %9
exit /b %ERRORLEVEL%

:chrome
shift
shift
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%OLLAMA_BROWSER_SCRIPT%" -Browser chrome %1 %2 %3 %4 %5 %6 %7 %8 %9
exit /b %ERRORLEVEL%

:edge
shift
shift
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%OLLAMA_BROWSER_SCRIPT%" -Browser edge %1 %2 %3 %4 %5 %6 %7 %8 %9
exit /b %ERRORLEVEL%
