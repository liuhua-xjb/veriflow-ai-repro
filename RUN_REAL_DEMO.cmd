@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" "RUN_REAL_DEMO.py"
) else (
  py -3 "RUN_REAL_DEMO.py"
)
