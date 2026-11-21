@echo off
:: Nastavenie UTF-8 kodovania pre slovenske znaky
chcp 65001 >nul

:: Nastavenie velkosti okna (aby sa zmestilo logo a text)
mode con: cols=85 lines=28

:: Farba: 0D (Cierne pozadie, Svetlofialovy text - ladi s Discordom)
color 0D
title ISAS CORE - Discord Bot Gateway

:: Prepneme sa do priecinka, kde je tento .bat subor
cd /d "%~dp0"

echo =====================================================================================
echo.
echo        ██╗███████╗ █████╗ ███████╗     ██████╗ ██████╗ ██████╗ ███████╗
echo        ██║██╔════╝██╔══██╗██╔════╝    ██╔════╝██╔═══██╗██╔══██╗██╔════╝
echo        ██║███████╗███████║███████╗    ██║     ██║   ██║██████╔╝█████╗  
echo        ██║╚════██║██╔══██║╚════██║    ██║     ██║   ██║██╔══██╗██╔══╝  
echo        ██║███████║██║  ██║███████║    ╚██████╗╚██████╔╝██║  ██║███████╗
echo        ╚═╝╚══════╝╚═╝  ╚═╝╚══════╝     ╚═════╝ ╚═════╝ ╚═╝  ╚═╝╚══════╝
echo.
echo                        [ DISCORD BOT GATEWAY - ONLINE ]
echo =====================================================================================
echo.
echo [1/2] Inicializácia jadra a pripájanie do virtuálneho prostredia (venv)...

:: Aktivacia venv
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
echo [2/2] Štartujem komunikačný modul Discord...
echo -------------------------------------------------------------------------------------
echo [i] Bot je plne aktívny, pripojený na Discord API a čaká na správy.
echo [i] AI modul je pripravený prijímať príkazy.
echo [i] Pre ukončenie bota stlač CTRL+C alebo zatvor toto okno.
echo -------------------------------------------------------------------------------------
echo.

:: Spustenie samotneho bota
python discord_bot.py

:: Ak bot z nejakeho dovodu padne (chyba netu a pod.), okno zmeni farbu na cervenu
echo.
color 0C
echo [!] UPOZORNENIE: Spojenie s API bolo prerušené alebo bol bot vypnutý.
pause