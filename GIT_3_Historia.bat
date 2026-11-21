@echo off
chcp 65001 >nul
mode con: cols=100 lines=30
color 0D
title GIT - História Projektu

cd /d "%~dp0"

echo =====================================================================================
echo                           [ GIT - HISTÓRIA ZÁLOH ]
echo =====================================================================================
echo.
:: Tento prikaz vypise historiu pekne farebne a prehladne
git log --oneline --graph --decorate --all
echo.
echo =====================================================================================
pause