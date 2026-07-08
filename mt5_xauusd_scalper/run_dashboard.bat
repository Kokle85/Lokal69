@echo off
call .venv\Scripts\activate
start http://127.0.0.1:8765
python src\dashboard.py
pause
