@echo off
cd /d "%~dp0"
python project_manager.py
if errorlevel 1 pause
