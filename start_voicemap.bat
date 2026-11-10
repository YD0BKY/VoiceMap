@echo off
title Trunking Radio Monitoring (Pussertif BSSN)
echo ============================================================
echo          TRUNKING RADIO MONITORING (PUSSERTIF BSSN)
echo ============================================================
echo.
echo Starting all local monitoring services...
echo.

cd /d D:\VoiceMap

:: 1. Start Local Recorder Script in a new window
echo [1/2] Launching Local Audio Recorder...
start "Trunking Radio - Recorder Script" cmd /k "python local_recorder.py"
timeout /t 1 >nul

:: 2. Start Frontend Server in a new window
echo [2/2] Launching Web UI Server on port 8765...
start "Trunking Radio - Web UI (Port 8765)" cmd /k "python -m http.server 8765"

echo.
echo ============================================================
echo   All services have been launched!
echo   - Web App UI: http://localhost:8765
echo.
echo   Keep both windows open while monitoring.
echo   If the VPN drops, the recorder will queue files locally.
echo ============================================================
echo.
pause
