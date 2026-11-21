@echo off
chcp 65001 >nul
mode con: cols=85 lines=25
color 0B
title GIT - Stav Projektu

cd /d "%~dp0"

echo =====================================================================================
echo                           [ GIT - AKTUÁLNY STAV KÓDU ]
echo =====================================================================================
echo.
echo Červené súbory = Zmenené, ale ešte neuložené v Gite.
echo Zelené súbory  = Pripravené na uloženie (Commit).
echo.
git status
echo.
echo =====================================================================================
pause