@echo off
chcp 65001 > nul
echo ========================================================
echo   ระบบตารางเวรคู่รัก (LINE Notification + Typhoon OCR)
echo ========================================================
echo.
if not exist .venv (
    echo กำลังสร้าง Virtual Environment...
    python -m venv .venv
    call .venv\Scripts\activate
    pip install -r requirements.txt
) else (
    call .venv\Scripts\activate
)

python run.py
pause
