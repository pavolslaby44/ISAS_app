@echo off
echo =======================================
echo     Generovanie kontextu pre AI
echo =======================================
echo.

REM 1. Krok: Aktivacia virtualneho prostredia
REM Upravte 'venv' na nazov vasej zlozky s prostredim (napr. env, .venv), ak sa vola inak.
REM Ak virtualne prostredie nepouzivate, tento riadok vymazte.
if exist venv\Scripts\activate.bat (
    echo Aktivujem virtualne prostredie 'venv'...
    call venv\Scripts\activate.bat
) else (
    echo UPOZORNENIE: Zlozka s virtualnym prostredim 'venv' sa nenasla.
    echo Pokracujem s globalnym Pythonom...
)

echo.
REM 2. Krok: Spustenie skriptu na zlucenie suborov
echo Spustam: python merge_isas.py
python merge_isas.py

echo.
echo =======================================
echo              HOTOVO!
echo =======================================
pause