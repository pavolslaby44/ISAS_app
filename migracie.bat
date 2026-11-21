@echo off
echo =======================================
echo     Spustenie Django migracii
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
REM 2. Krok: Vytvorenie migracii
echo Spustam: python manage.py makemigrations
python manage.py makemigrations

echo.
REM 3. Krok: Aplikovanie migracii
echo Spustam: python manage.py migrate
python manage.py migrate

echo.
echo =======================================
echo              HOTOVO!
echo =======================================
pause