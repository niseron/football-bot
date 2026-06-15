@echo off
cd /d "%~dp0"

echo Starting Football Picks Bot...

start "Picks Bot"        cmd /k python main.py
start "Weekly Summary"   cmd /k python weekly_summary.py
start "Results Schedule" cmd /k python auto_results.py --schedule
start "Results Live"     cmd /k python auto_results.py --live

echo All 4 windows launched.
