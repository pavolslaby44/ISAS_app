@echo off
echo =======================================
echo     Terminal s aktivovanym VENV
echo =======================================
echo.

if exist venv\Scripts\activate.bat (
    echo Aktivujem virtualne prostredie 'venv'...
    echo Mozes zadavat prikazy (napr. python manage.py runserver)
    echo =======================================
    REM Otvori novy terminal a priamo v nom spusti aktivaciu venv
    cmd /k "venv\Scripts\activate.bat"
) else (
    echo CHYBA: Zlozka s virtualnym prostredim 'venv' sa nenasla!
    pause
)