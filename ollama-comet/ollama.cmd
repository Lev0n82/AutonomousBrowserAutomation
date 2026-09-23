@echo off
setlocal
set "OLLAMA_COMET_SCRIPT=%~dp0Launch-OllamaComet.ps1"
if /I "%~1"=="launch" if /I "%~2"=="comet" goto comet
"%LOCALAPPDATA%\Programs\Ollama\ollama.exe" %*
exit /b %ERRORLEVEL%

:comet
shift
shift
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%OLLAMA_COMET_SCRIPT%" %1 %2 %3 %4 %5 %6 %7 %8 %9
exit /b %ERRORLEVEL%
