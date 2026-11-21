@echo off
:: Bezpečné automatické minimalizovanie okna pri štarte master servera
if not "%1"=="min" start /min "" "%~dpnx0" min & exit /b

:: Nastavenie UTF-8 kodovania pre slovenske znaky
chcp 65001 >nul

:: Nastavenie velkosti okna
mode con: cols=85 lines=32

:: Farba: 0E (Cierne pozadie, Ziariva zlta - reprezentuje hlavny MASTER proces)
color 0E
title ISAS CORE - Master Server

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
echo                     [ MASTER SERVER - STARTUP SEQUENCE ]
echo =====================================================================================
echo.
echo [1/3] Pripájanie do jadra systému (venv)...

:: 1. Aktivacia virtualneho prostredia
if exist venv\Scripts\activate.bat (
    call venv\Scripts\activate.bat
    echo [OK] Virtuálne prostredie úspešne aktivované.
) else (
    color 0C
    echo.
    echo [!] KRITICKÁ CHYBA: Virtuálne prostredie 'venv' nebolo nájdené!
    echo     Uisti sa, že spúšťaš tento súbor z koreňového priečinka projektu.
    echo.
    pause
    exit /b
)

echo.
echo [2/3] Prebúdzam webový launcher...
:: 2. Spustenie web launchera (otvori prehliadac)
if exist web_launcher.py (
    start /b python web_launcher.py
    echo [OK] Prehliadač bol úspešne prebudený do pozadia.
) else (
    echo [!] UPOZORNENIE: Súbor 'web_launcher.py' nebol nájdený. Prehliadač sa neotvorí sám.
)

echo.
echo [3/3] Štartujem aplikačný server Django...
echo -------------------------------------------------------------------------------------
echo [+] ISAS CORE beží na adrese: http://127.0.0.1:8000
echo [+] Všetky moduly sú aktívne. Pripravené prijímať spojenia.
echo [!] TOTO OKNO NESMIEŠ ZATVORIŤ, kým chceš so systémom pracovať.
echo [!] Pre manuálne vypnutie servera stlač CTRL+C.
echo -------------------------------------------------------------------------------------
echo.

:: 3. Spustenie samotneho Django servera
python manage.py runserver

:: Ak server spadne alebo ho vypnes
echo.
color 0C
echo [!] SERVER BOL UKONČENÝ.
pause