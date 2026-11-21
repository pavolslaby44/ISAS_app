import ipaddress
import json
import re
import requests
import logging

from datetime import datetime, timedelta, time as dt_time, date
from django.apps import apps
from django.core.cache import cache
from django.db import models
from django.utils import timezone
from django.conf import settings
from decimal import Decimal, ROUND_HALF_UP
from django.db.models import Sum, Q, IntegerField
from django.db.models.functions import Coalesce

logger = logging.getLogger(__name__)

def get_firemna_mena() -> str:
    """
    🛡️ ISAS MASTER RESILIENCE: 100% defenzívny resolver meny materskej spoločnosti.
    - Chránený voči výpadku spojenia na Redis/Memcached (nezhodí aplikáciu).
    - Chránený voči prázdnej DB pri prvotných migráciách a inštalácii.
    - Sanitizuje výstup na normalizovaný ISO kód (strip + upper).
    - Garantuje návrat platného reťazca (fail-safe fallback z nastavení).
    """
    try:
        mena = cache.get('isas_firemna_mena')
        if mena and isinstance(mena, str) and mena.strip():
            return mena.strip().upper()
    except Exception:
        pass

    mena = None
    try:
        FiremnaIdentita = None
        for app_label in ('ISASaplikacia', 'Evidencia'):
            try:
                FiremnaIdentita = apps.get_model(app_label, 'FiremnaIdentita')
                if FiremnaIdentita:
                    break
            except LookupError:
                continue

        if FiremnaIdentita:
            identita = FiremnaIdentita.objects.first()
            mena_val = getattr(identita, 'predvolena_mena', None) or getattr(identita, 'mena', None)
            if identita and mena_val:
                mena = str(mena_val).strip().upper()
            elif identita and hasattr(FiremnaIdentita, 'get_country_defaults'):
                sidlo = getattr(identita, 'sidlo_stat', None)
                iso = FiremnaIdentita.get_aktivna_krajina_iso(sidlo) if hasattr(FiremnaIdentita, 'get_aktivna_krajina_iso') else sidlo
                try:
                    defaults = FiremnaIdentita.get_country_defaults(iso) if iso else FiremnaIdentita.get_country_defaults()
                except TypeError:
                    defaults = FiremnaIdentita.get_country_defaults()
                if defaults and defaults.get('mena'):
                    mena = str(defaults['mena']).strip().upper()
    except Exception:
        pass

    final_mena = mena if (mena and len(mena) >= 2) else ""

    try:
        cache.set('isas_firemna_mena', final_mena, 3600)
    except Exception:
        pass

    return final_mena

def get_b2b_client_and_discount(request):
    """🛡️ ISAS BEZPEČNOSTNÝ HELPER: Vráti B2B klienta a jeho zľavu (Ochrana pred AttributeError)."""
    user = getattr(request, 'user', None)
    is_auth = user.is_authenticated() if callable(getattr(user, 'is_authenticated', None)) else getattr(user, 'is_authenticated', False)
    if not is_auth or not getattr(user, 'is_active', False):
        return None, Decimal('0.00')

    profil = None
    for attr in ('zakaznik_profil', 'zakaznik'):
        try:
            kandidat = getattr(user, attr, None)
            if kandidat and not getattr(kandidat, 'je_interny_profil', False) and hasattr(kandidat, 'b2b_zlava_percenta'):
                profil = kandidat
                break
        except Exception:
            continue

    tenant = getattr(request, 'tenant', None)
    if tenant and (getattr(tenant, 'je_interny_profil', False) or not hasattr(tenant, 'b2b_zlava_percenta')):
        tenant = None

    if tenant:
        is_staff = bool(getattr(user, 'is_staff', False))
        b2b_klient = tenant if (profil == tenant or is_staff) else None
    else:
        b2b_klient = profil

    raw_zlava = getattr(b2b_klient, 'b2b_zlava_percenta', None) if b2b_klient else None
    zlava = parse_decimal(raw_zlava, default='0.00')
    if zlava < Decimal('0.00'):
        zlava = Decimal('0.00')
    elif zlava > Decimal('100.00'):
        zlava = Decimal('100.00')
    return b2b_klient, zlava.quantize(Decimal('0.01'))

def get_eshop_models():
    """🛒 ISAS E-COMMERCE: Dynamicky vráti výhradne skladové modely v Evidencii."""
    try:
        app_config = apps.get_app_config('Evidencia')
        return [
            model for model in app_config.get_models() 
            if not model._meta.abstract and 'zobrazit_na_webe' in [f.name for f in model._meta.fields] and 'pocet_na_sklade' in [f.name for f in model._meta.fields]
        ]
    except Exception:
        return []

def get_work_shifts(gn, target_date):
    """📅 ISAS SMART BOOKING: Vypočíta začiatok, stred a koniec pracovnej doby pre daný deň."""
    t_start = getattr(gn, 'pracovny_cas_od', None) or dt_time(7, 30)
    t_end = getattr(gn, 'pracovny_cas_do', None) or dt_time(16, 0)
    
    if not isinstance(t_start, dt_time):
        try:
            t_start = datetime.strptime(str(t_start).strip(), '%H:%M:%S').time() if len(str(t_start).strip()) > 5 else datetime.strptime(str(t_start).strip(), '%H:%M').time()
        except (ValueError, TypeError):
            t_start = dt_time(7, 30)
            
    if not isinstance(t_end, dt_time):
        try:
            t_end = datetime.strptime(str(t_end).strip(), '%H:%M:%S').time() if len(str(t_end).strip()) > 5 else datetime.strptime(str(t_end).strip(), '%H:%M').time()
        except (ValueError, TypeError):
            t_end = dt_time(16, 0)

    # Ochrana pred chybným nastavením v ERP (Koniec pred začiatkom)
    if t_end <= t_start:
        t_start, t_end = dt_time(7, 30), dt_time(16, 0)
        
    c_target_date = target_date.date() if isinstance(target_date, datetime) else (parse_date(target_date) or date.today() if not isinstance(target_date, date) else target_date)
    work_start = datetime.combine(c_target_date, t_start)
    work_end = datetime.combine(c_target_date, t_end)

    # ISAS CORE: Striktný naive režim (settings.USE_TZ = False) bez miešania aware/naive datetime
    
    pol_dna_hodin = (work_end - work_start).total_seconds() / 3600 / 2
    midpoint = work_start + timedelta(hours=pol_dna_hodin)
    
    return work_start, midpoint, work_end

class SmartDateTime(datetime):
    """
    Defenzívny Python DateTime wrapper.
    Zabezpečuje, že polnoc (00:00:00) sa navonok maskuje ako 'čas neuvedený'.
    """
    @classmethod
    def from_datetime(cls, dt):
        if not dt:
            return None
        if isinstance(dt, cls):
            return dt
        if not isinstance(dt, datetime):
            if isinstance(dt, date):
                dt = datetime.combine(dt, dt_time.min)
            else:
                return None
        return cls(
            dt.year, dt.month, dt.day,
            dt.hour, dt.minute, dt.second, dt.microsecond,
            tzinfo=dt.tzinfo,
            fold=getattr(dt, 'fold', 0)
        )

    @property
    def je_cely_den(self):
        return self.hour == 0 and self.minute == 0 and self.second == 0

    def strftime(self, fmt):
        if self.je_cely_den:
            # Ak formát žiada časové zložky
            if any(x in fmt for x in ('%H', '%M', '%I', '%X', '%T')):
                if any(x in fmt for x in ('%d', '%m', '%Y', '%y')):
                    datum = super().strftime('%d.%m.%Y')
                    return f"{datum} (čas neuvedený)"
                return "čas neuvedený"
        return super().strftime(fmt)

    def replace(self, *args, **kwargs):
        """Zabezpečí, že po replace() zostane objekt typu SmartDateTime."""
        res = super().replace(*args, **kwargs)
        return SmartDateTime.from_datetime(res)

    def astimezone(self, tz=None):
        """Zabezpečí, že po zmene časovej zóny zostane objekt typu SmartDateTime."""
        res = super().astimezone(tz)
        return SmartDateTime.from_datetime(res)

    def __str__(self):
        if self.je_cely_den:
            return f"{super().strftime('%d.%m.%Y')} (čas neuvedený)"
        return super().strftime('%d.%m.%Y %H:%M')

    def __format__(self, format_spec):
        if not format_spec:
            return str(self)
        return self.strftime(format_spec)


class SmartDateTimeField(models.DateTimeField):
    """V DB je to natívny DateTimeField, v Pythone vždy vracia SmartDateTime."""
    def from_db_value(self, value, expression, connection):
        if value is None:
            return value
        return SmartDateTime.from_datetime(value)

    def to_python(self, value):
        val = super().to_python(value)
        if val is None or isinstance(val, SmartDateTime):
            return val
        if isinstance(val, datetime):
            return SmartDateTime.from_datetime(val)
        return val

def d_round(value):
    """Zaokrúhli číslo alebo Decimal na 2 desatinné miesta (bankové zaokrúhľovanie nahor)."""
    if value is None or str(value).strip() == "":
        return Decimal('0.00')
    try:
        return Decimal(str(value).replace('\xa0', '').replace(' ', '').replace(',', '.')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal('0.00')

def ocisti_nazov_od_jednotky(nazov):
    """Zmaže z konca reťazca '(číslo jednotka)' pre čistý výpis v notifikáciách s podporou celých aj desatinných čísel."""
    if not nazov:
        return ""
    # Očistíme vstupné biele znaky a bezpečne odstránime zátvorku s celým aj desatinným číslom
    cisty_vstup = str(nazov).strip()
    return re.sub(r'\s*\(\d+(?:[.,]\d+)?\s*[a-zA-Z2]+\)\s*$', '', cisty_vstup)

def get_client_ip(request):
    """Pomocná funkcia na bezpečné získanie reálnej IP adresy klienta (odolná voči X-Forwarded-For spoofingu)."""
    if not request or not hasattr(request, 'META') or not isinstance(request.META, dict):
        return ""
    remote_addr = str(request.META.get('REMOTE_ADDR', '') or '').strip()
    remote_ip = None
    try:
        remote_ip = ipaddress.ip_address(remote_addr)
    except ValueError:
        remote_ip = None

    # Dôverujeme X-Forwarded-For výhradne vtedy, ak spojenie prichádza z internej/loopback proxy siete
    is_trusted_proxy = bool(remote_ip and (remote_ip.is_private or remote_ip.is_loopback or remote_ip.is_link_local))
    if is_trusted_proxy or not remote_ip:
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            # Čítame sprava doľava (hodnoty pridané naším reverzným proxy) a preskakujeme interné proxy IP
            for raw_candidate in reversed(str(x_forwarded_for).split(',')):
                candidate = raw_candidate.strip()
                try:
                    cand_ip = ipaddress.ip_address(candidate)
                    if cand_ip.is_global and not (cand_ip.is_private or cand_ip.is_loopback or cand_ip.is_link_local or cand_ip.is_multicast or cand_ip.is_reserved or cand_ip.is_unspecified):
                        return str(cand_ip)
                except ValueError:
                    continue

    return str(remote_ip) if remote_ip else ""

def safe_tg(text):
    """Čistič pre Telegram a e-maily. Nepoužívame html.escape, lebo ten rozbíja úvodzovky."""
    if not text:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def safe_discord(text):
    """Čistič a konvertor textu pre Discord Markdown (odstraňuje problematické formátovanie)."""
    if not text:
        return ""
    # Bezpečné ošetrenie špeciálnych markdown znakov mimo formátovacích blokov
    return str(text).replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")

def clean_and_parse_ai_json(response_text):
    """
    Unifikuje čistenie markdownu a parsovanie JSONu z AI odpovedí.
    Bezpečne deteguje, či AI vrátila JSON Object {...} alebo JSON Array [...].
    """
    if isinstance(response_text, (dict, list)):
        return response_text
        
    if not response_text:
        raise ValueError("AI JSON parser obdržal prázdny vstup.")

    raw_text = str(response_text).strip()
    
    # Odstránenie markdown blokov
    if raw_text.startswith("```json"):
        raw_text = raw_text[7:]
    elif raw_text.startswith("```"):
        raw_text = raw_text[3:]
    if raw_text.endswith("```"):
        raw_text = raw_text[:-3]
        
    raw_text = raw_text.strip()
    
    # Detekcia vonkajšieho obalu (Objekt vs Pole)
    zaciatok_obj = raw_text.find('{')
    koniec_obj = raw_text.rfind('}')
    zaciatok_arr = raw_text.find('[')
    koniec_arr = raw_text.rfind(']')
    
    # Vyberieme ten formát, ktorý začína skôr (je vonkajším obalom) a je správne uzavretý
    if zaciatok_obj != -1 and koniec_obj > zaciatok_obj and (zaciatok_arr == -1 or zaciatok_obj < zaciatok_arr or koniec_arr <= zaciatok_arr):
        raw_text = raw_text[zaciatok_obj:koniec_obj+1]
    elif zaciatok_arr != -1 and koniec_arr > zaciatok_arr:
        raw_text = raw_text[zaciatok_arr:koniec_arr+1]
        
    return json.loads(raw_text.strip())

def get_safe_employee_name(user, fallback_text="Oprávnená osoba"):
    """
    Unifikuje bezpečné získanie mena zamestnanca z User modelu.
    """
    if not user:
        return fallback_text
    if hasattr(user, 'zamestnanec') and user.zamestnanec:
        meno = getattr(user.zamestnanec, 'meno', '') or ''
        priezvisko = getattr(user.zamestnanec, 'priezvisko', '') or ''
        cele_meno = f"{meno} {priezvisko}".strip()
        if cele_meno:
            return cele_meno
    full_name_fn = getattr(user, 'get_full_name', None)
    if callable(full_name_fn):
        try:
            full_name = str(full_name_fn()).strip()
            if full_name:
                return full_name
        except Exception:
            pass
    return str(getattr(user, 'username', '') or '').strip() or fallback_text

def generate_dynamic_greeting(obj, fallback="Dobrý deň,", use_formal=False):
    """
    Polymorfné zostavenie oslovenia pre Zákazníkov, Dodávateľov, Zamestnancov a Súkromné osoby.
    """
    if not obj:
        return fallback

    rod = str(getattr(obj, 'oslovenie_rod', '') or '').strip()
    
    # 1. Zákazník / Dodávateľ (kontaktna_osoba)
    osoba = str(getattr(obj, 'kontaktna_osoba', '') or '').strip()
    
    # 2. Zamestnanec (meno + priezvisko)
    if not osoba and hasattr(obj, 'meno') and hasattr(obj, 'priezvisko'):
        m = str(getattr(obj, 'meno', '') or '').strip()
        p = str(getattr(obj, 'priezvisko', '') or '').strip()
        osoba = f"{m} {p}".strip()
        
    # 3. Súkromná osoba (meno_a_priezvisko)
    elif not osoba and hasattr(obj, 'meno_a_priezvisko'):
        osoba = str(getattr(obj, 'meno_a_priezvisko', '') or '').strip()

    if rod and osoba:
        prefix = "Vážený" if use_formal else "Dobrý deň"
        return f"{prefix} {rod} {osoba},"
    elif osoba:
        return f"Dobrý deň, {osoba},"
    
    return fallback

# --- BEZPEČNÉ PARSERY DÁT ---
def parse_decimal(value, default='0.00'):
    if value is None or str(value).strip() == '':
        return Decimal(str(default)) if default is not None else None
    try:
        d = Decimal(str(value).replace('\xa0', '').replace(' ', '').replace(',', '.'))
        if not d.is_finite():
            return Decimal(str(default)) if default is not None else None
        return d
    except Exception:
        return Decimal(str(default)) if default is not None else None

def parse_float(value, default=0.0):
    if value is None or str(value).strip() == '':
        return default
    try:
        val = float(str(value).replace('\xa0', '').replace(' ', '').replace(',', '.'))
        if val != val or val in (float('inf'), float('-inf')):
            return default
        return val
    except Exception:
        return default

def parse_int(value, default=0):
    if value is None or str(value).strip() == '':
        return default
    try:
        return int(float(str(value).replace('\xa0', '').replace(' ', '').replace(',', '.')))
    except Exception:
        return default

def parse_bool(value, default=False):
    if value is None or str(value).strip() == '':
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().upper() in ['ANO', 'ÁNO', 'TRUE', '1', 'Y', 'YES']

def parse_date(value):
    if not value: return None
    if isinstance(value, datetime): return value.date()
    if isinstance(value, date): return value
    val_str = str(value).strip().split('T')[0].split(' ')[0]
    formaty = ['%d.%m.%Y', '%Y-%m-%d', '%d/%m/%Y', '%d.%m.%y']
    for fmt in formaty:
        try: return datetime.strptime(val_str, fmt).date()
        except ValueError: continue
    return None

def parse_bigint(value):
    if not value: return None
    try: return int(str(value).replace('\xa0', '').replace(' ', ''))
    except (ValueError, TypeError): return None

def send_telegram_message(text):
    try:
        from ISASaplikacia.models import GlobalneNastavenia
        gn = GlobalneNastavenia.objects.first()
        
        if not gn or not gn.telegram_bot_token or not gn.telegram_id_majitel:
            return
            
        url = f"https://api.telegram.org/bot{gn.telegram_bot_token}/sendMessage"
        payload = {"chat_id": gn.telegram_id_majitel, "text": text, "parse_mode": "HTML"}
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        logger.warning(f"Zlyhanie odoslania Telegram notifikácie: {e}")

def send_isas_stock_notifications(stock_movement_obj, detail_url_path):
    if not cache.get('isas_stock_notifications', True):
        return
        
    movement_id = getattr(stock_movement_obj, 'id', None)
    if not movement_id:
        return

    povodny = getattr(stock_movement_obj, '_povodny_stav', None)
    novy = getattr(stock_movement_obj, '_novy_stav', None)
    
    try:
        from .tasks import send_isas_stock_notifications_task
        send_isas_stock_notifications_task.delay(movement_id, detail_url_path, povodny, novy)
    except Exception as e:
        logger.warning(f"Zlyhanie zaradenia skladovej notifikácie do fronty: {e}")

class LedgerKalkulacka:
    """
    🧠 ISAS CORE: Event Sourcing Engine pre Filiálky.
    Počíta aktuálny stav skladu dynamicky iba na základe IN/OUT transakcií (StockMovement).
    """
    @staticmethod
    def zisti_stav_polozky(filialka, product_name):
        """
        Vráti aktuálny počet kusov konkrétnej položky na danej filiálke.
        """
        from .models import StockMovement
        
        if not filialka:
            return 0  # Ak nie je filiálka, sme v Hlavnom sklade (neplatí pre Ledger)

        aggr = StockMovement.objects.filter(
            filialka=filialka, 
            product_name=product_name
        ).aggregate(
            in_qty=Coalesce(Sum('count', filter=Q(action='ADD')), 0, output_field=IntegerField()),
            out_qty=Coalesce(Sum('count', filter=Q(action='REMOVE')), 0, output_field=IntegerField())
        )
        
        in_qty = (aggr.get('in_qty') or 0) if aggr else 0
        out_qty = (aggr.get('out_qty') or 0) if aggr else 0
        return in_qty - out_qty

    @staticmethod
    def zisti_stav_vsetkych_poloziek(filialka):
        """
        🚀 MEGA OPTIMALIZÁCIA (ISAS PERFORMANCE): 
        Vráti slovník { 'Názov produktu': pocet_kusov } pre celú filiálku 
        naraz v jednej jedinej rýchlej databázovej (SQL) query!
        """
        from .models import StockMovement
        
        if not filialka:
            return {}

        qs = StockMovement.objects.filter(filialka=filialka).values('product_name').annotate(
            in_qty=Coalesce(Sum('count', filter=Q(action='ADD')), 0, output_field=IntegerField()),
            out_qty=Coalesce(Sum('count', filter=Q(action='REMOVE')), 0, output_field=IntegerField())
        )
        
        vysledok = {}
        for item in qs:
            stav = (item.get('in_qty') or 0) - (item.get('out_qty') or 0)
            if stav > 0:  # Zaujímajú nás len položky, ktoré reálne fyzicky ešte na filiálke sú
                vysledok[item['product_name']] = stav
                
        return vysledok

def isas_ai_logistika_trasy(start_adresa, ciel_adresa):
    """
    ISAS CORE LOGISTIKA: Vypočíta exaktnú cestnú vzdialenosť a čas.
    Využíva Google Maps Distance Matrix API (alebo OSRM fallback).
    Umelá inteligencia (LLM) nesmie počítať kilometre, pretože halucinuje!
    """    
    if not start_adresa or not ciel_adresa:
        return 0.0, 0.0
        
    try:
        # Absolútny import naprieč aplikáciami (z ISASaplikacia do Evidencia) vo vnútri funkcie
        from ISASaplikacia.utils import get_google_maps_api_key
        api_key = get_google_maps_api_key()
        
        if api_key:
            # 🗺️ 1. GOOGLE MAPS API (Absolútna zhoda s tvojou fotkou z prehliadača)
            url = "https://maps.googleapis.com/maps/api/distancematrix/json"
            params = {'origins': start_adresa, 'destinations': ciel_adresa, 'key': api_key}
            response = requests.get(url, params=params, timeout=5)
            data = response.json()
            
            rows = data.get('rows', [])
            elements = rows[0].get('elements', []) if (rows and isinstance(rows, list) and len(rows) > 0) else []
            if data.get('status') == 'OK' and elements and elements[0].get('status') == 'OK':
                element = elements[0]
                km = element['distance']['value'] / 1000.0      # Prevod z metrov na KM
                minuty = element['duration']['value'] / 60.0    # Prevod zo sekúnd na minúty
                return round(km, 1), round(minuty, 1)
                
        else:
            # 🌍 2. BEZPLATNÝ OPEN-SOURCE FALLBACK (OSRM)
            logger.info("ISAS LOGISTIKA: Počítam cez bezplatný OSRM fallback.")
            
            # Najprv preložíme adresy na GPS súradnice (Nominatim)
            headers = {'User-Agent': 'ISAS-Core-Logistics/1.0'}
            def ziskaj_gps(adresa):
                try:
                    r = requests.get("https://nominatim.openstreetmap.org/search", params={'q': adresa, 'format': 'json', 'limit': 1}, headers=headers, timeout=5)
                    data = r.json()
                    if isinstance(data, list) and len(data) > 0 and 'lon' in data[0] and 'lat' in data[0]:
                        return (data[0]['lon'], data[0]['lat'])
                except Exception:
                    pass
                return None
                
            gps_start = ziskaj_gps(start_adresa)
            gps_ciel = ziskaj_gps(ciel_adresa)
            
            if gps_start and gps_ciel:
                # Následne vypočítame reálnu trasu autom po ceste
                osrm_url = f"http://router.project-osrm.org/route/v1/driving/{gps_start[0]},{gps_start[1]};{gps_ciel[0]},{gps_ciel[1]}?overview=false"
                r_osrm = requests.get(osrm_url, timeout=5)
                data_osrm = r_osrm.json()
                
                routes = data_osrm.get('routes', [])
                if data_osrm.get('code') == 'Ok' and isinstance(routes, list) and len(routes) > 0:
                    km = routes[0]['distance'] / 1000.0
                    minuty = routes[0]['duration'] / 60.0
                    return round(km, 1), round(minuty, 1)

        return 0.0, 0.0
        
    except Exception as e:
        logger.error(f"ISAS CORE - LOGISTIKA ERROR: {e}")
        return 0.0, 0.0

def get_kategoria_nazov(model_cls):
    """Vyhľadá model v CATEGORY_MAP a vráti jeho názov. O(N) zložitosť."""
    try:
        from .industry_loader import CATEGORY_MAP
        if isinstance(CATEGORY_MAP, dict):
            for main_cat, sub_dict in CATEGORY_MAP.items():
                if isinstance(sub_dict, dict):
                    for name, cls in sub_dict.items():
                        if cls == model_cls:
                            return name
    except Exception:
        pass
    return "📦 Nezaradené"

def get_sklad_dynamic_js_config():
    """Generuje JSON konfiguráciu hierarchie skladu pre JS komponenty."""
    try:
        from .industry_loader import CATEGORY_CHOICES, CATEGORY_MAP, FLAT_CATEGORY_MAP, PARENT_MODELS
        
        model_map = {}
        hierarchy = {}
        main_cat_ui_map = dict(CATEGORY_CHOICES) if CATEGORY_CHOICES else {}
        
        for main_cat, subcats in (CATEGORY_MAP.items() if isinstance(CATEGORY_MAP, dict) else []):
            display_name = main_cat_ui_map.get(main_cat, main_cat)
            parts = str(display_name).split(" ", 1)
            main_icon = parts[0] if parts and not parts[0].isalnum() else "📦"
            cisty_nazov = parts[1] if parts and not parts[0].isalnum() and len(parts) > 1 else str(display_name)

            hierarchy[main_cat] = {
                "nazov": cisty_nazov,
                "ikona": main_icon,
                "bg": "#f8fafc",
                "txt": "#0f172a",
                "podkategorie": []
            }

            if isinstance(subcats, dict):
                for subcat_name, model_cls in subcats.items():
                    model_name = getattr(model_cls, '__name__', str(model_cls))
                    related_name = model_name.lower()
                    if hasattr(model_cls, '_meta'):
                        for field in model_cls._meta.get_fields():
                            if isinstance(field, models.ForeignKey):
                                fk_field = FLAT_CATEGORY_MAP.get(model_name, {}).get('fk_field')
                                parent_models = tuple(PARENT_MODELS.values()) if PARENT_MODELS else ()
                                target_model = getattr(field.remote_field, 'model', None) if field.remote_field else None
                                if (fk_field and field.name == fk_field) or (parent_models and target_model in parent_models):
                                    if field.remote_field and field.remote_field.related_name:
                                        related_name = field.remote_field.related_name
                                        break
                    
                    model_map[related_name] = model_name
                    sub_parts = str(subcat_name).split(" ", 1)
                    sub_icon = sub_parts[0] if sub_parts and not sub_parts[0].isalnum() else "🏷️"
                    cisty_subcat = sub_parts[1] if sub_parts and not sub_parts[0].isalnum() and len(sub_parts) > 1 else str(subcat_name)
                    
                    hierarchy[main_cat]["podkategorie"].append({
                        "nazov": cisty_subcat,
                        "kod": model_name.lower(),
                        "ikona": sub_icon,
                        "bg_color": "#ffffff",
                        "text_color": "#1e293b"
                    })

        return json.dumps({
            "models": model_map,
            "hierarchy": hierarchy
        })
    except Exception as e:
        logger.error(f"Chyba pri generovaní skladového JS configu: {e}")
        return json.dumps({"models": {}, "hierarchy": {}})

def ziskaj_povolene_modely(vrat_app_label=False):
    """
    🧠 Kanonický generátor pre ForeignKey(ContentType, limit_choices_to=...).
    Nahrádza 4 duplicitné funkcie. Dynamicky číta modely z CATEGORY_MAP bez cyklenia.
    """
    try:
        from Evidencia.industry_loader import CATEGORY_MAP
        povolene = [
            getattr(model_cls._meta, 'model_name', model_cls.__name__.lower())
            for podkategorie in (CATEGORY_MAP.values() if isinstance(CATEGORY_MAP, dict) else [])
            for model_cls in (podkategorie.values() if isinstance(podkategorie, dict) else [])
            if hasattr(model_cls, '_meta') or hasattr(model_cls, '__name__')
        ]
        if vrat_app_label:
            return {'app_label': 'Evidencia', 'model__in': povolene}
        return {'model__in': povolene}
    except Exception:
        return {'app_label': 'Evidencia', 'model__in': []} if vrat_app_label else {'model__in': []}

def get_default_miesto_startu():
    """Dynamicky načíta adresu prevádzky z globálnych nastavení firmy."""
    try:
        FiremnaIdentita = None
        for app_label in ('ISASaplikacia', 'Evidencia'):
            try:
                FiremnaIdentita = apps.get_model(app_label, 'FiremnaIdentita')
                if FiremnaIdentita:
                    break
            except LookupError:
                continue

        if FiremnaIdentita:
            identita = FiremnaIdentita.objects.first()
            if identita and getattr(identita, 'adresa_prevadzky', None):
                return str(identita.adresa_prevadzky).replace('\r\n', ', ').replace('\n', ', ')
    except Exception:
        pass
    return "Sídlo spoločnosti"

def get_skladove_kategorie_z_mapy():
    """
    Dynamicky vytiahne všetky skladové modely z CATEGORY_MAP z industry_loader.py.
    Lokálny import zabraňuje cyklickej chybe pri štarte Djanga.
    """
    try:
        from Evidencia.industry_loader import CATEGORY_MAP
        
        povolené_modely = []
        if isinstance(CATEGORY_MAP, dict):
            for hlavna_kategoria, podkategorie in CATEGORY_MAP.items():
                if isinstance(podkategorie, dict):
                    for nazov_v_menu, model_trieda in podkategorie.items():
                        # ContentType hľadá modely vždy malými písmenami (napr. názov modelu v lowercase)
                        if hasattr(model_trieda, '_meta') and getattr(model_trieda._meta, 'model_name', None):
                            povolené_modely.append(model_trieda._meta.model_name.lower())
                        elif hasattr(model_trieda, '__name__'):
                            povolené_modely.append(model_trieda.__name__.lower())
                
        return {'app_label': 'Evidencia', 'model__in': povolené_modely}
    except Exception:
        return {'app_label': 'Evidencia', 'model__in': []}