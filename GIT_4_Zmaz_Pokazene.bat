@echo off
chcp 65001 >nul
mode con: cols=85 lines=25
color 0C
title GIT - ZÁCHRANNÁ BRZDA (Návrat v čase)

cd /d "%~dp0"

echo =====================================================================================
echo                      [ !!! GIT - ZÁCHRANNÁ BRZDA !!! ]
echo =====================================================================================
echo.
echo POZOR: Tento skript VYMAŽE všetky tvoje neuložené zmeny v kóde!
echo Vráti všetky súbory presne do stavu, v akom boli pri poslednom Commite.
echo.
echo Použi to len vtedy, ak si kód pokazil a chceš začať odznova od poslednej zálohy.
echo.
set /p volba="Si si ISTÝ, že chceš vymazať dnešné zmeny? (A/N): "

if /I "%volba%"=="A" (
    echo.
    echo [!] Vraciam čas...
    :: Vrati zmenene subory do povodneho stavu
    git restore .
    :: Vymaze nove subory, ktore este neboli v Gite
    git clean -fd
    echo.
    echo [OK] Kód bol úspešne vrátený do funkčného stavu!
) else (
    echo.
    echo [ZRUŠENÉ] Nič sa nevymazalo.
)

echo.
pause