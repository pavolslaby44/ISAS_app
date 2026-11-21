@echo off
chcp 65001 >nul
mode con: cols=90 lines=30
color 0B
title ISAS CORE - Celery Worker ^& Beat

cd /d "%~dp0"

echo =====================================================================================
echo.
echo       ██████╗ ███████╗██╗     ███████╗██████╗ ██╗   ██╗
echo      ██╔════╝ ██╔════╝██║     ██╔════╝██╔══██╗╚██╗ ██╔╝
echo      ██║      █████╗  ██║     █████╗  ██████╔╝ ╚████╔╝ 
echo      ██║      ██╔══╝  ██║     ██╔══╝  ██╔══██╗  ╚██╔╝  
echo      ╚██████╗ ███████╗███████╗███████╗██║  ██║   ██║   
echo       ╚═════╝ ╚══════╝╚══════╝╚══════╝╚═╝  ╚═╝   ╚═╝   
echo.
echo               [ CELERY ASYNC ENGINE - LOKÁLNY VÝVOJ ]
echo =====================================================================================
echo.

if exist venv\Scripts\activate.bat (
    call venv\Scripts\activate.bat
    echo [OK] Virtuálne prostredie aktivované.
) else (
    color 0C
    echo [!] KRITICKÁ CHYBA: venv nenájdený!
    pause
    exit /b
)

echo.
echo [!] UPOZORNENIE: Pre lokálny beh musíš mať spustený REDIS (napr. cez Docker Desktop).
echo.
echo Štartujem Celery Worker a Celery Beat (Plánovač)...
echo -------------------------------------------------------------------------------------

:: Spustenie workera a beata v oddelených procesoch (pre Windows lokálny vývoj)
echo [START] Spúšťam Celery Worker v novom okne...
start "Celery Worker" /min cmd /k "venv\Scripts\activate.bat & celery -A ISASaplikacia worker -l info --pool=solo -Q default,heavy,notifications,iot"

echo [START] Spúšťam Celery Beat v novom okne...
start "Celery Beat" /min cmd /k "venv\Scripts\activate.bat & celery -A ISASaplikacia beat -l info"

echo.
echo [OK] Služby boli spustené v samostatných oknách.
echo 🚀 Hlavné okno sa automaticky zatvorí...
timeout /t 2 >nul
exit