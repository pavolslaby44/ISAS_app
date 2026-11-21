import time
import webbrowser
import urllib.request
import socket
from urllib.error import URLError, HTTPError

def wait_for_server(url, max_retries=60):
    print(f"--- Čakám na plný štart ISAS CORE na {url}... ---")
    
    pokus = 0
    while pokus < max_retries:
        try:
            # 1. Pokus o spojenie (Klopanie na dvere)
            urllib.request.urlopen(url, timeout=2.0)
            
            print("--- Stránka je 100% pripravená! Otváram prehliadač... ---")
            try_open_browser(url)
            break
            
        except HTTPError:
            # Server beží, len vrátil napr. 403/500 (čo je pre launcher OK)
            print("--- Server komunikuje! Otváram prehliadač... ---")
            try_open_browser(url)
            break
            
        except (URLError, ConnectionResetError, TimeoutError, socket.timeout):
            # Štandardné sieťové chyby - server ešte nebeží
            pokus += 1
            time.sleep(1.0)
            
        except Exception as e:
            # 🛡️ PARANOID OCHRANA: Zachytí akúkoľvek bizarnú OS chybu
            print(f"⚠️ Nečakaná chyba pri kontrole servera: {e}. Skúšam znova...")
            pokus += 1
            time.sleep(1.0)
            
    if pokus >= max_retries:
        print("🚨 ISAS CORE: Server neodpovedal do 3 minút. Skontroluj konzolu Djanga!")

def try_open_browser(url):
    """ 🛡️ Ochrana pre prípad, že OS zlyhá pri otváraní prehliadača """
    try:
        webbrowser.open(url)
    except Exception as e:
        print(f"🚨 Nepodarilo sa automaticky otvoriť prehliadač ({e}).")
        print(f"👉 Klikni na tento odkaz manuálne: {url}")

if __name__ == "__main__":
    wait_for_server("http://127.0.0.1:8000/admin/login/")