@echo off
:: Nastavenie UTF-8 kodovania pre slovenske znaky
chcp 65001 >nul

:: Nastavenie velkosti okna
mode con: cols=85 lines=32

:: Farba: 0B (Cierne pozadie, Tyrkysova - reprezentuje moderny Docker proces)
color 0B
title ISAS CORE - Docker Master Flotila

:: Prepneme sa do priecinka, kde je tento .bat subor
cd /d "%~dp0"

echo =====================================================================================
echo.
echo        ██╗███████╗███████╗  ██████╗ ██████╗ ██████╗ ███████╗
echo        ██║██╔════╝██╔══██╗ ██╔════╝██╔═══██╗██╔══██╗██╔════╝
echo        ██║███████╗███████║ ██║     ██║   ██║██████╔╝█████╗  
echo        ██║╚════██║██╔══██║ ██║     ██║   ██║██╔══██╗██╔══╝  
echo        ██║███████║██║  ██║ ╚██████╗╚██████╔╝██║  ██║███████╗
echo        ╚═╝╚══════╝╚═╝  ╚═╝  ╚═════╝ ╚═════╝ ╚═╝  ╚═╝╚══════╝
echo.
echo                  [ DOCKER MASTER SERVER - STARTUP SEQUENCE ]
echo =====================================================================================
echo.
echo [1/3] Prebúdzam Docker Engine a kontrolujem kontajnery...

:: 1. Spustenie Docker Compose na pozadi
docker-compose up -d

echo.
echo [2/3] Otváram riadiace panely v prehliadači...

:: 2. Pockame 3 sekundy, aby mal web cas nabehnut
timeout /t 3 /nobreak >nul

:: 3. Otvorenie ISAS CORE a FLOWER
start http://127.0.0.1:8000
start http://127.0.0.1:5555

echo.
echo [3/3] Všetky systémy sú ONLINE.
echo -------------------------------------------------------------------------------------
echo [+] ISAS CORE Web:    http://127.0.0.1:8000
echo [+] FLOWER Monitor:   http://127.0.0.1:5555
echo [+] Databáza, Redis, Celery Workery a AI Boti bežia na pozadí.
echo.
echo [!] Toto okno môžeš bezpečne zatvoriť. Systém pobeží ďalej na pozadí v Dockeri.
echo [!] Pre úplné vypnutie systému použi príkaz: docker-compose down
echo -------------------------------------------------------------------------------------
echo.
pause