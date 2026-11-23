@echo off
chcp 65001 >nul
cd /d %~dp0
if "%~1"=="" (echo Перетащите файл реестра .xlsx на этот файл & pause & exit /b)
.venv\Scripts\python xlsx_io.py "%~1"
pause
