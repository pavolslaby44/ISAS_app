@echo off
:: Ak skript nebol spustený minimalizovane, reštartuje sa v minimalizovanom stave a pôvodné okno zavrie
if not "%1"=="min" start /min "" "%~dpnx0" min & exit /b

:: Nastavenie UTF-8 kodovania pre slovenske znaky
chcp 65001 >nul

:: Nastavenie velkosti okna
mode con: cols=85 lines=32

:: Farba: 0B (Cierne pozadie, Svetlomodra - reprezentuje sietovy TUNEL proces)
color 0B
title ISAS CORE - Network Tunnel (Ngrok)

:: Prepneme sa do priecinka, kde je tento .bat subor
cd /d "%~dp0"

echo =====================================================================================
echo.
echo        ███╗   ██╗ ██████╗ ██████╗  ██████╗ ██╗  ██╗
echo        ████╗  ██║██╔════╝ ██╔══██╗██╔═══██╗██║ ██╔╝
echo        ██╔██╗ ██║██║  ███╗██████╔╝██║   ██║█████═╝ 
echo        ██║╚██╗██║██║   ██║██╔══██╗██║   ██║██╔═██╗ 
echo        ██║ ╚████║╚██████╔╝██║  ██║╚██████╔╝██║ ╚██╗
echo        ╚═╝  ╚═══╝ ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═╝
echo.
echo                   [ NGROK TUNNEL - SECURE CONNECTION ]
echo =====================================================================================
echo.
echo [1/2] Kontrola prítomnosti sieťového modulu (ngrok.exe)...

if exist ngrok.exe (
    echo [OK] Modul ngrok bol úspešne lokalizovaný.
) else (
    color 0C
    echo.
    echo [!] KRITICKÁ CHYBA: Súbor 'ngrok.exe' nebol nájdený v tomto priečinku!
    echo     Uisti sa, že si ngrok.exe skopíroval presne k tomuto .bat súboru.
    echo.
    pause
    exit /b
)

echo.
echo [2/2] Vytváram šifrovaný tunel do internetu...
echo -------------------------------------------------------------------------------------
echo [+] Statická doména: bradyauxetically-commissarial-josephina.ngrok-free.dev
echo [+] Smerovanie na:   http://127.0.0.1:8000
echo [!] TOTO OKNO NESMIEŠ ZATVORIŤ, kým testuješ funkcie cez mobilný telefón.
echo -------------------------------------------------------------------------------------
echo.

:: Spustenie Ngroku so statickou domenou a portom 8000
ngrok http --domain=bradyauxetically-commissarial-josephina.ngrok-free.dev 8000

:: Ak tunel spadne alebo ho vypnes
echo.
color 0C
echo [!] SIEŤOVÝ TUNEL BOL UKONČENÝ ALEBO SPADOL.
pause