@echo off
chcp 65001 >nul
mode con: cols=85 lines=25
color 0A
title GIT - Vytvorenie Zálohy (Commit)

cd /d "%~dp0"

echo =====================================================================================
echo                           [ GIT - VYTVORENIE ZÁLOHY ]
echo =====================================================================================
echo.
echo Napíš krátky popis toho, čo si práve dokončil/opravil (bez diakritiky je to istejšie):
echo (Napríklad: Oprava PDF generatora, Pridany novy model pre faktury)
echo.
set /p commit_msg="Popis zmien: "

:: Ak používateľ stlačí len Enter bez textu, skript sa ukončí
if "%commit_msg%"=="" (
    echo.
    echo [CHYBA] Musíš zadať popis! Záloha nebola vytvorená.
    pause
    exit /b
)

echo.
echo [1/2] Pridávam všetky zmenené súbory...
git add .

echo [2/2] Vytváram historický záznam...
git commit -m "%commit_msg%"

echo.
echo -------------------------------------------------------------------------------------
echo [OK] Výborne! Tvoj kód je bezpečne uložený v histórii Gitu.
echo -------------------------------------------------------------------------------------
pause