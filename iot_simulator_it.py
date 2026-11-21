import os
import sys
import time
import json
import random
import requests
from datetime import datetime

# ==========================================
# ⚙️ KONFIGURÁCIA (ISAS CORE - IT ODVETVIE)
# ==========================================
API_URL = os.environ.get("ISAS_IOT_API_URL", "http://127.0.0.1:8000/evidencia/api/iot/telemetry/")
DEFAULT_API_KEY = "SEM_VLOZ_IOT_API_KLUC"
API_KEY = (
    os.environ.get("ISAS_IOT_API_KEY", "").strip()
    or (sys.argv[1].strip() if len(sys.argv) > 1 else DEFAULT_API_KEY)
)
REDIS_URL = os.environ.get("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
INTERVAL_SEKUND = 5

# ==========================================
# 🧠 STAV SERVERA (IT Infraštruktúra)
# ==========================================
stav = {
    "web_api_online": True,
    "db_cluster_ok": True,
    "cpu_raw": 425,         # 42.5 % (násobič 0.1)
    "ram_raw": 612,         # 61.2 % (násobič 0.1)
    "latency_ms": 24,       # 24 ms (násobič 1.0)
    "error_rate_raw": 25,   # 0.25 % (násobič 0.01)
    "uptime_h": 4320,
    "peak_load_h": 1280,
    "cyklus": 0,
    "porucha_aktivna": False,
    "cykly_poruchy": 0,
}


def generuj_data():
    """Generuje realistické SRE metriky servera a aktualizáciu meradla Uptime/Peak."""
    stav["cyklus"] += 1

    # 🚨 Náhodná simulácia SRE incidentu (cca 1 z 10 cyklov)
    if not stav["porucha_aktivna"] and random.randint(1, 10) == 1:
        stav["porucha_aktivna"] = True
        stav["cykly_poruchy"] = 6
        print("\n🚨 [SIMULÁTOR] Generujem SRE incident (Vysoké CPU/RAM a HTTP 5xx chyby)!\n")

    if stav["porucha_aktivna"]:
        stav["cpu_raw"] = random.randint(910, 990)        # 91.0 - 99.0 %
        stav["ram_raw"] = random.randint(910, 970)        # 91.0 - 97.0 %
        stav["latency_ms"] = random.randint(320, 650)     # 320 - 650 ms
        stav["error_rate_raw"] = random.randint(520, 900) # 5.20 - 9.00 %

        stav["cykly_poruchy"] -= 1
        if stav["cykly_poruchy"] <= 0:
            stav["porucha_aktivna"] = False
            stav["cpu_raw"] = 425
            stav["ram_raw"] = 612
            stav["latency_ms"] = 24
            stav["error_rate_raw"] = 25
            print("\n✅ [SIMULÁTOR] Záťaž pominula, server je opäť v SLA norme.\n")
    else:
        if stav["web_api_online"]:
            stav["cpu_raw"] = max(180, min(680, stav["cpu_raw"] + random.randint(-25, 25)))
            stav["ram_raw"] = max(480, min(760, stav["ram_raw"] + random.randint(-12, 12)))
            stav["latency_ms"] = max(12, min(65, stav["latency_ms"] + random.randint(-5, 5)))
            stav["error_rate_raw"] = max(0, min(55, stav["error_rate_raw"] + random.randint(-5, 5)))
        else:
            stav["cpu_raw"] = random.randint(20, 60)
            stav["ram_raw"] = random.randint(180, 240)
            stav["latency_ms"] = 0
            stav["error_rate_raw"] = 0

    # Simulácia pribúdania meradla Uptime a Peak záťaže
    if stav["cyklus"] % 12 == 0:
        stav["uptime_h"] += 1
        if stav["cpu_raw"] >= 550:
            stav["peak_load_h"] += 1

    return {
        "40001": stav["cpu_raw"],
        "40002": stav["ram_raw"],
        "40003": stav["uptime_h"],
        "40004": stav["peak_load_h"],
        "40005": stav["latency_ms"],
        "40006": stav["error_rate_raw"],
        "10001": 1 if stav["web_api_online"] else 0,
        "10002": 1 if stav["db_cluster_ok"] else 0,
    }


def spracuj_vzdialeny_prikaz(teraz):
    """Spracuje príkazy zápisu odoslané z IoT Dashboardu cez Redis frontu."""
    try:
        import redis
        r = redis.Redis.from_url(REDIS_URL)
        raw_cmd = r.rpop(f"iot_cmd_{API_KEY}")
        if not raw_cmd:
            return
        cmd_text = raw_cmd.decode("utf-8") if isinstance(raw_cmd, bytes) else raw_cmd
        cmd = json.loads(cmd_text)
        print(f"[{teraz}] ⚡ [PRÍKAZ Z ISAS CORE] 🚀 Server prijal príkaz: {cmd}")
        if isinstance(cmd, dict) and str(cmd.get("register")) == "10001":
            stav["web_api_online"] = bool(int(cmd.get("value", 1)))
    except Exception:
        pass


def posli_telemetriu():
    aktualny_rezim = "ALARM" if stav["porucha_aktivna"] else "ROUTINE"

    payload = {
        "api_key": API_KEY,
        "mode": aktualny_rezim,
        "data": generuj_data(),
    }

    if aktualny_rezim == "ALARM":
        payload["alarm_code"] = "IT-PERF-CPU-HIGH"
        payload["alarm_severity"] = "CRITICAL"
        payload["alarm_description"] = "Kritické vyťaženie CPU/RAM a zvýšená chybovosť HTTP 5xx!"

    headers = {"Content-Type": "application/json"}
    teraz = datetime.now().strftime("%H:%M:%S")

    try:
        response = requests.post(API_URL, json=payload, headers=headers, timeout=5)
        if response.status_code == 200:
            print(f"[{teraz}] ✅ [{aktualny_rezim}] Dáta odoslané: {response.json()}")
        elif response.status_code == 404:
            print(f"[{teraz}] ❌ Chyba 404: Skontroluj URL adresu {API_URL}.")
        elif response.status_code == 403:
            print(f"[{teraz}] ❌ Chyba 403: Neplatný API kľúč alebo zariadenie nemá zapnuté iot_je_aktivny.")
        elif response.status_code == 429:
            print(f"[{teraz}] ⚠️ Chyba 429: Prekročený Rate Limit zariadenia.")
        else:
            print(f"[{teraz}] ⚠️ Server vrátil kód {response.status_code}: {response.text}")
    except requests.exceptions.ConnectionError:
        print(f"[{teraz}] ❌ Nepodarilo sa pripojiť na {API_URL}. Beží runserver a Celery worker?")

    spracuj_vzdialeny_prikaz(teraz)


if __name__ == "__main__":
    if not API_KEY or API_KEY == "SEM_VLOZ_IOT_API_KLUC":
        print("❌ Chýba API kľúč. Spusti skript ako: python iotsimulator_it.py <API_KLUC>")
        sys.exit(1)

    print("==================================================")
    print(" 📡 START IT IOT SIMULÁTORA (ISAS CORE)")
    print("==================================================")
    print("Odvetvie: IT / Serverová infraštruktúra")
    print(f"API Kľúč: {API_KEY}")
    print(f"Cieľová URL: {API_URL}\n")

    try:
        while True:
            posli_telemetriu()
            time.sleep(INTERVAL_SEKUND)
    except KeyboardInterrupt:
        print("\n🛑 Simulátor ukončený používateľom.")