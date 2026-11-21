@echo off
chcp 65001 >nul
echo ===================================================
echo   Zistovanie dostupnych verzii kniznice google-genai
echo ===================================================
echo.

:: 1. Aktivacia virtualneho prostredia
if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
) else (
    echo [!] Upozornenie: Nenasiel sa priecinok 'venv'.
)

:: 2. Vypis vsetkych dostupnych verzii na PyPI
echo Pripajam sa na PyPI...
echo ---------------------------------------------------
python -m pip index versions google-genai
if errorlevel 1 (
    echo [-] Nastala chyba pri dotaze na PyPI ^(skontrolujte pripojenie alebo verziu pip^).
)
echo ---------------------------------------------------

:: 3. Overenie nainstalovanej verzie a podporovanych ThinkingLevel
echo.
echo Aktualne nainstalovane SDK a podporovane ThinkingLevel:
python -c "import google.genai as g; from google.genai import types; tl=getattr(types,'ThinkingLevel',None); print('Nainstalovana verzia:', getattr(g,'__version__','?')); print('ThinkingLevel:', list(tl.__members__.keys()) if tl else 'N/A')" 2>nul
if errorlevel 1 (
    echo [-] Kniznica google-genai nie je v aktivnom prostredi nainstalovana.
)

echo.
echo Hotovo.
pause