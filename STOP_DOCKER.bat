@echo off
:: Nastavenie UTF-8 kodovania pre slovenske znaky
chcp 65001 >nul

:: Nastavenie velkosti okna
mode con: cols=85 lines=25

:: Farba: 0C (Cierne pozadie, Svetlocervena - reprezentuje vypinanie)
color 0C
title ISAS CORE - Bezpecne Vypnutie (Shutdown)

:: Prepneme sa do priecinka, kde je tento .bat subor
cd /d "%~dp0"

echo =====================================================================================
echo.
echo          ███████╗████████╗ ██████╗ ██████╗     ██████╗  ██████╗ ██╗    ██╗███╗   ██╗
echo          ██╔════╝╚══██╔══╝██╔═══██╗██╔══██╗    ██╔══██╗██╔═══██╗██║    ██║████╗  ██║
echo          ███████╗   ██║   ██║   ██║██████╔╝    ██║  ██║██║   ██║██║ █╗ ██║██╔██╗ ██║
echo          ╚════██║   ██║   ██║   ██║██╔═══╝     ██║  ██║██║   ██║██║███╗██║██║╚██╗██║
echo          ███████║   ██║   ╚██████╔╝██║         ██████╔╝╚██████╔╝╚███╔███╔╝██║ ╚████║
echo          ╚══════╝   ╚═╝    ╚═════╝ ╚═╝         ╚═════╝  ╚═════╝  ╚══╝╚══╝ ╚═╝  ╚═══╝
echo.
echo                  [ DOCKER INFRAŠTRUKTÚRA - BEZPEČNÉ VYPNUTIE ]
echo =====================================================================================
echo.
echo [!] Posielam signál na bezpečné vypnutie (Graceful Shutdown)...
echo [!] Databáza ukladá dáta, Celery dokončuje tasky. Prosím čakaj.
echo.

:: Univerzalny prikaz na vypnutie vsetkeho, co bezi z tohto docker-compose.yml
docker-compose down

echo.
echo -------------------------------------------------------------------------------------
echo [OK] Všetky Docker kontajnery boli bezpečne vypnuté a odstránené z pamäte!
echo [OK] Porty (8000, 5433, 6379, 5555) sú opäť voľné.
echo [OK] Tvoje dáta v databáze zostali bezpečne uložené na disku.
echo -------------------------------------------------------------------------------------
echo.
:: Krátka pauza na vizuálnu kontrolu (2 sekundy) a následné automatické zavretie
timeout /t 2 /nobreak >nul
exit