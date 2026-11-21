@echo off
chcp 65001 >nul
echo ===================================================
echo       Zistovanie dostupnych Google AI modelov
echo ===================================================
echo.

:: 1. Aktivacia virtualneho prostredia
if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
) else (
    echo [!] Upozornenie: Nenasiel sa priecinok 'venv'.
)

:: 2. Vytvorenie docasneho Python skriptu
echo import os > temp_zisti_modely.py
echo from dotenv import load_dotenv >> temp_zisti_modely.py
echo from google import genai >> temp_zisti_modely.py
echo load_dotenv() >> temp_zisti_modely.py
echo api_key = os.getenv('GEMINI_API_KEY') >> temp_zisti_modely.py
echo if not api_key: >> temp_zisti_modely.py
echo     print("[-] CHYBA: GEMINI_API_KEY nebol najdeny v .env subore!") >> temp_zisti_modely.py
echo     exit() >> temp_zisti_modely.py
echo try: >> temp_zisti_modely.py
echo     client = genai.Client(api_key=api_key) >> temp_zisti_modely.py
echo     print("Pripajam sa na servery Googlu...") >> temp_zisti_modely.py
echo     print("---------------------------------------------------") >> temp_zisti_modely.py
echo     for model in client.models.list(): >> temp_zisti_modely.py
echo         print(model.name) >> temp_zisti_modely.py
echo     print("---------------------------------------------------") >> temp_zisti_modely.py
echo except Exception as e: >> temp_zisti_modely.py
echo     print(f"[-] Nastala chyba pri komunikacii s API: {e}") >> temp_zisti_modely.py

:: 3. Spustenie skriptu
python temp_zisti_modely.py

:: 4. Vymazanie docasneho skriptu (upratanie)
del temp_zisti_modely.py

echo.
echo Hotovo.
pause