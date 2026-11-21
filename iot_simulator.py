import time
import json
import random
import requests
from datetime import datetime

# ==========================================
# ⚙️ PRESNÁ LOKÁLNA KONFIGURÁCIA (ISAS CORE)
# ==========================================
API_URL = "http://127.0.0.1:8000/evidencia/api/iot/telemetry/"
API_KEY = "isas_live_c20257e4771ef32f936ec01515141bc3"  # Presný kľúč z tvojho screenshotu
INTERVAL_SEKUND = 5  # Posiela dáta každých 5 sekúnd

# ==========================================
# 🧠 STAV STROJA (Kompresor ID 10)
# ==========================================
stav = {
    "bezi": True,
    "v_zatazi": True,
    "tlak_raw": 72,       # 7.2 Bar (pri násobiči 0.1)
    "teplota_raw": 780,   # 78.0 °C (pri násobiči 0.1)
    "mh_celkove": 12450,
    "mh_zataz": 10200,
    "cyklus": 0,
    "porucha_aktivna": False, # 👈 PRIDANÉ PRE SIMULÁCIU ALARMOV
    "cykly_poruchy": 0        # 👈 PRIDANÉ PRE SIMULÁCIU ALARMOV
}

def generuj_data():
    """Generuje realistické výkyvy tlaku a teploty kompresora."""
    stav["cyklus"] += 1
    
    # 🚨 ISAS ALARM SIMULATOR: Náhodné generovanie poruchy (cca 1 z 10 cyklov)
    if not stav["porucha_aktivna"] and random.randint(1, 10) == 1:
        stav["porucha_aktivna"] = True
        stav["cykly_poruchy"] = 12 # Porucha bude trvať 12 cyklov (60 sekúnd)
        print("\n🚨 [SIMULÁTOR] Generujem kritickú poruchu (Prehriatie oleja)!\n")

    if stav["porucha_aktivna"]:
        # Extrémne hodnoty pre vyvolanie alarmu na backende
        stav["teplota_raw"] = random.randint(1150, 1250) # 115.0 - 125.0 °C
        stav["tlak_raw"] = random.randint(10, 30)        # Tlak padá (1.0 - 3.0 Bar)
        stav["v_zatazi"] = False
        
        stav["cykly_poruchy"] -= 1
        if stav["cykly_poruchy"] <= 0:
            stav["porucha_aktivna"] = False
            stav["teplota_raw"] = 800  # Návrat do normálu
            stav["tlak_raw"] = 72  # 👈 Návrat tlaku späť na 7.2 Bar
            stav["v_zatazi"] = True  # 👈 Zopnutie kompresora späť do záťaže
            print("\n✅ [SIMULÁTOR] Porucha pominula, návrat do normálu.\n")
    else:
        # Mierne fyzikálne kolísanie pre pekné krivky v grafu (Normálny stav)
        if stav["v_zatazi"]:
            stav["tlak_raw"] += random.choice([-1, 0, 1, 2])
            if stav["tlak_raw"] > 85: # Max 8.5 Bar
                stav["v_zatazi"] = False
            stav["teplota_raw"] = min(880, stav["teplota_raw"] + random.randint(1, 4))
        else:
            stav["tlak_raw"] -= random.choice([1, 2, 3])
            if stav["tlak_raw"] < 65: # Min 6.5 Bar
                stav["v_zatazi"] = True
            stav["teplota_raw"] = max(720, stav["teplota_raw"] - random.randint(2, 5))

    # Simulácia pribúdania motohodín
    if stav["cyklus"] % 12 == 0:
        stav["mh_celkove"] += 1
        if stav["v_zatazi"]:
            stav["mh_zataz"] += 1

    return {
        "40001": stav["teplota_raw"],   # Teplota (Zámena registrov podľa tvojho nastavenia)
        "40002": stav["tlak_raw"],      # Tlak
        "40003": stav["mh_celkove"],    # MH celkovo
        "40004": stav["mh_zataz"],      # MH záťaž
        "40005": random.randint(12, 22),# Vibrácie RMS (1.2 - 2.2 mm/s)
        "10001": 1 if stav["bezi"] else 0,
        "10002": 1 if stav["v_zatazi"] else 0
    }

def posli_telemetriu():
    # 🧠 ISAS FIX: Dynamické prepnutie režimu podľa stavu stroja
    aktualny_rezim = "ALARM" if stav["porucha_aktivna"] else "ROUTINE"
    
    payload = {
        "api_key": API_KEY,
        "mode": aktualny_rezim,
        "data": generuj_data()
    }
    
    # 🚨 Ak je to ALARM, pribalíme detaily, ktoré backend očakáva pre čiernu skrinku
    if aktualny_rezim == "ALARM":
        payload["alarm_code"] = "ERR_TEMP_PRESS"
        payload["alarm_severity"] = "CRITICAL"
        payload["alarm_description"] = "Kritická teplota oleja a pokles tlaku!"

    headers = {"Content-Type": "application/json"}

    teraz = datetime.now().strftime("%H:%M:%S")

    try:
        response = requests.post(API_URL, json=payload, headers=headers, timeout=5)
        
        if response.status_code == 200:
            print(f"[{teraz}] ✅ Dáta úspešne odoslané! Odpoveď servera: {response.json()}")
        elif response.status_code == 404:
            print(f"[{teraz}] ❌ Chyba 404: Skontroluj URL adresu. Skús upraviť API_URL v skripte (odstráň alebo pridaj '/evidencia').")
        elif response.status_code == 403:
            print(f"[{teraz}] ❌ Chyba 403: Nezabudol si v administrácii zaškrtnúť 'IoT je aktívne'?")
        elif response.status_code == 429:
            print(f"[{teraz}] ⚠️ Chyba 429: Prekročený Rate Limit! Zvýš v admini 'Limit zápisov (hodina)' na 5000.")
        else:
            print(f"[{teraz}] ⚠️ Server vrátil kód {response.status_code}: {response.text}")
            
    except requests.exceptions.ConnectionError:
        print(f"[{teraz}] ❌ Nepodarilo sa pripojiť na {API_URL}. Beží ti `python manage.py runserver`?")

    # ⚡ ISAS CORE: Kontrola, či zo servera neprišiel nový príkaz na zápis do PLC
    try:
        import redis
        r = redis.Redis(host='127.0.0.1', port=6379, db=0)
        cmd = r.rpop(f"iot_cmd_{API_KEY}")
        if cmd:
            print(f"[{teraz}] ⚡ [PRÍKAZ Z ISAS CORE] 🚀 Hardvér prijal príkaz z Dashboardu: {cmd.decode('utf-8')}")
    except Exception:
        pass

if __name__ == "__main__":
    print("==================================================")
    print(" 📡 START SIMULÁCIE IOT TELEMETRIE (ISAS CORE)")
    print("==================================================")
    print(f"Zariadenie ID: 10")
    print(f"API Kľúč: {API_KEY}")
    print(f"Cieľová URL: {API_URL}\n")
    
    while True:
        posli_telemetriu()
        time.sleep(INTERVAL_SEKUND)