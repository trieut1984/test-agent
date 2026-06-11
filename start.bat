@echo off
chcp 65001 >nul
echo ========================================
echo   Tro ly Phap ly AI - Dang khoi dong...
echo ========================================
echo.

cd /d "%~dp0"

echo [1/2] Kiem tra dependencies...
pip install -r requirements.txt -q

echo [2/2] Khoi dong web server...
echo.
echo >> Mo trinh duyet: http://localhost:8000
echo >> Nhan Ctrl+C de dung
echo.
python app.py
pause
