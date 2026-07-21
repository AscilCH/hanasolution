@echo off
chcp 65001 >nul
title B2B Phone Finder
echo.
echo   =======================================
echo     B2B Phone Finder - Starting Server
echo   =======================================
echo.
echo   Opening http://localhost:8080 ...
echo   Press Ctrl+C to stop the server.
echo.
python "%~dp0server.py"
pause
