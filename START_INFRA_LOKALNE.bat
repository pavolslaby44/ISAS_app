@echo off
chcp 65001 >nul
mode con: cols=85 lines=25
color 0D
title ISAS CORE - Lokalna Infrastruktura

cd /d "%~dp0"

echo =====================================================================================
echo               [ DOCKER INFRAŠTRUKTÚRA PRE LOKÁLNY VÝVOJ ]
echo =====================================================================================
echo.
echo [1/2] Vypínam všetky bežiace Docker kontajnery (aby neukradli tasky)...
docker-compose down

echo.
echo [2/2] Štartujem IBA Redis a Flower (bez databázy)...
:: Parameter --no-deps zabezpečí, že sa nespustí databáza ani iné závislosti
docker-compose up -d --no-deps redis flower

echo.
echo -------------------------------------------------------------------------------------
echo [OK] Infraštruktúra beží!
echo [+] Redis:    Port 6379
echo [+] Flower:   http://127.0.0.1:5555
echo -------------------------------------------------------------------------------------
echo.
echo Teraz môžeš spustiť svoje lokálne skripty:
echo 1. START_ISAS.bat (pre web)
echo 2. START_CELERY_LOKALNE.bat (pre PDF a tasky)
echo.
echo 🚀 Infraštruktúra beží na pozadí. Okno sa automaticky zatvorí...
timeout /t 3 >nul
exit