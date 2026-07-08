@echo off
call .venv\Scripts\activate
echo Running ORB across all instruments (gold London+NY, indices NY-only RR 3:1)...
python src\run_instruments.py --days 90 --validate
pause
