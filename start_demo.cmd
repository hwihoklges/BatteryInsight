@echo off
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set OPENBLAS_NUM_THREADS=1
set OMP_NUM_THREADS=1
python examples\make_workbench_demo.py --out "%~dp0demo_workspace\input"
if errorlevel 1 goto failed
python -m battery_insight workbench --config "%~dp0demo_workspace\input\relations.json" --out "%~dp0demo_workspace\results" --open
if errorlevel 1 goto failed
exit /b 0
:failed
echo Check Python 3.12 and install dependencies: python -m pip install -e .
pause
exit /b 1
