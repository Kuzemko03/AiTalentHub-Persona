@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if errorlevel 1 goto end
".venv\Scripts\python.exe" -X utf8 -m joker serve --telegram
:end
pause
