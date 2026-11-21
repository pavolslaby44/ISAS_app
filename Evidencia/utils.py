import unicodedata
import requests
import traceback
import re
import hashlib
import logging
import math
import json
import socket
import io
import openpyxl

from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from django.core.files.base import ContentFile
from django.conf import settings
from django.core.cache import cache
from django.core.mail import send_mail
from django.utils import timezone
from datetime import datetime, timedelta
from django.utils.html import escape
from decimal import Decimal
from django.http import JsonResponse
from django.db import transaction
from django.apps import apps
from django.db.models import Q
from django.core.exceptions import ObjectDoesNotExist
from django.core.mail import get_connection, EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.utils.dateparse import parse_datetime
from django.urls import reverse
from django.db import close_old_connections
from google import genai
from google.genai import types
from django.contrib.contenttypes.models import ContentType
from playwright.sync_api import sync_playwright
from imap_tools import MailBox, AND

from Evidencia.models import (
    Zakaznik, Dodavatel, SukromnaOsoba, Zamestnanec, Vozidlo, 
    TypServisuPrace, DatabazaVyrobcov, Filialka, ServisovaneZariadenie, 
    Sklad, ServisnyZaznam, TelemetryHistory,
    AlarmLog, SignalDictionary, PlanovanyServisZariadenia
)

from ISASaplikacia.utils import get_gemini_api_key
from .utils_helper import (
    ocisti_nazov_od_jednotky, get_client_ip, safe_tg, 
    clean_and_parse_ai_json, get_safe_employee_name, generate_dynamic_greeting,
    parse_decimal, parse_float, parse_int, parse_bool, parse_date, parse_bigint,
    get_firemna_mena
)
from .utils_compliance import get_zariadenie_compliance_audit

from .industry_loader import (
    get_active_parameter_reverse_names, get_zariadenie_parametre, get_industry_compliance,
    get_industry_term, INDUSTRY_TERMS, CATEGORY_MAP, CATEGORY_CHOICES, PARENT_MODELS
)

logger = logging.getLogger(__name__)

def generuj_ai_sepkar_analyzu(zariadenie, technik_id=None):
    """
    🧠 ISAS CORE: Zdieľaná biznis logika pre AI Šepkára.
    Získava kontext, rieši cache a komunikuje s Gemini API.
    """
    # 1. Načítanie histórie (Hlboký prefetch)
    historia_qs = ServisnyZaznam.objects.filter(
        zariadenie=zariadenie, dokoncene=True
    ).prefetch_related(
        'pouzite_polozky_zaznamu__content_type', 
        'pouzite_polozky_zaznamu__content_object'
    ).order_by('-vytvorene')[:15]
    historia = list(historia_qs)

    posledny_alarm = AlarmLog.objects.filter(zariadenie=zariadenie).order_by('-casova_peciatka').first()
    posledna_telemetria = TelemetryHistory.objects.filter(zariadenie=zariadenie).order_by('-casova_peciatka').first()
    najnovsi_zaznam = historia[0] if historia else None
    
    zariadenie_ts = int(zariadenie.upravene.timestamp()) if (getattr(zariadenie, 'upravene', None) and hasattr(zariadenie.upravene, 'timestamp')) else 0
    servis_ts = int(najnovsi_zaznam.upravene.timestamp()) if (najnovsi_zaznam and getattr(najnovsi_zaznam, 'upravene', None) and hasattr(najnovsi_zaznam.upravene, 'timestamp')) else 0
    alarm_ts = int(posledny_alarm.casova_peciatka.timestamp()) if (posledny_alarm and getattr(posledny_alarm, 'casova_peciatka', None) and hasattr(posledny_alarm.casova_peciatka, 'timestamp')) else 0
    telemetria_ts = int(posledna_telemetria.casova_peciatka.timestamp()) if (posledna_telemetria and getattr(posledna_telemetria, 'casova_peciatka', None) and hasattr(posledna_telemetria.casova_peciatka, 'timestamp')) else 0

    # 2. Načítanie draftu technika
    zive_poznamky_technika = ""
    draft_hash = "0"
    if technik_id:
        draft = cache.get(f"isas_quicknote_draft_{technik_id}")
        if draft:
            zive_poznamky_technika = f"\n\nŽIVÉ POZNÁMKY TECHNIKA Z OBHLIADKY:\n{draft}"
            draft_hash = hashlib.md5(draft.encode('utf-8')).hexdigest()

    # 3. Cache logika (zahrnutý podpis compliance alertov a zmeny stroja pre okamžitú invalidáciu)
    compliance_polozky = get_zariadenie_compliance_audit(zariadenie) or []
    compliance_alerty = [c for c in compliance_polozky if isinstance(c, dict) and c.get('vyzaduje_alert')]
    compliance_sig = "_".join(c.get('kod', '') for c in compliance_alerty) or "ok"

    cache_key = f"isas_ai_sepkar_{zariadenie.id}_{zariadenie_ts}_{servis_ts}_{alarm_ts}_{telemetria_ts}_{compliance_sig}_{draft_hash}"
    cache_timeout = 900 if getattr(zariadenie, 'iot_je_aktivny', False) else 2592000

    ai_text = cache.get(cache_key)

    if not ai_text:
        # 🛡️ ISAS FIX: Ak stroj nemá históriu ani IoT, ale má legislatívny alert (napr. expirované VTZ), AI Šepkár MUSÍ analyzovať
        if not historia and not getattr(zariadenie, 'iot_je_aktivny', False) and not compliance_alerty:
            return "EMPTY_HISTORY_FLAG"

        vek = zariadenie.rok_vyroby if zariadenie.rok_vyroby else "Neznámy"
        vyrobca = zariadenie.vyrobca.nazov_vyrobcu if zariadenie.vyrobca else "Neznámy"
        kategoria = zariadenie.get_kategoria_display()
        umiestnenie = zariadenie.umiestnenie if zariadenie.umiestnenie else "Neznáme"
        poznamka_stroja = zariadenie.poznamka if zariadenie.poznamka else "Žiadna"

        # 4. Extrakcia histórie
        text_historie = ""
        for z in historia:
            diely_qs = z.pouzite_polozky_zaznamu.all()
            diely_text = ", ".join([f"{d.pocet}x {d.cisty_nazov}" for d in diely_qs]) if diely_qs else "Žiadne diely"
            koren = z.get_koren_problemu_display() if (z.koren_problemu and z.koren_problemu != 'NEHODNOTENE') else "Bežný servis"
            namerane = f" (Namerané po servise: {z.namerane_hodnoty})" if z.namerane_hodnoty else ""
            datum_str = z.vytvorene.strftime('%d.%m.%Y') if getattr(z, 'vytvorene', None) else 'Neznámy'
            mh_disp = z.meter_display if z.stav_motohodin is not None else "Nezadané"
            load_disp = f" ({z.meter_load_label}: {z.meter_load_display})" if getattr(z, 'motohodiny_v_zatazi', None) is not None else ""
            text_historie += (
                f"- Dátum: {datum_str}, Stav: {mh_disp}{load_disp}. "
                f"Príčina: {koren}. Úkony: {z.poruchy}. Vymenené diely: {diely_text}.{namerane}\n"
            )

        # 5. IoT Telemetria
        iot_dossier = "IoT telemetria neaktívna."
        if getattr(zariadenie, 'iot_je_aktivny', False):
            slovnik = {s.nazov_signalu: s for s in SignalDictionary.objects.filter(zariadenie=zariadenie)}
            now = timezone.now()
            
            tel_15m = TelemetryHistory.objects.filter(zariadenie=zariadenie, casova_peciatka__lte=now - timedelta(minutes=15)).order_by('-casova_peciatka').first()
            tel_1h = TelemetryHistory.objects.filter(zariadenie=zariadenie, casova_peciatka__lte=now - timedelta(hours=1)).order_by('-casova_peciatka').first()
            tel_24h = TelemetryHistory.objects.filter(zariadenie=zariadenie, casova_peciatka__lte=now - timedelta(days=1)).order_by('-casova_peciatka').first()
            tel_7d = TelemetryHistory.objects.filter(zariadenie=zariadenie, casova_peciatka__lte=now - timedelta(days=7)).order_by('-casova_peciatka').first()
            
            nevyriesene = AlarmLog.objects.filter(zariadenie=zariadenie, vyriesene=False).order_by('-casova_peciatka')[:5]
            
            # Bezpečné načítanie plánovaného servisu
            try:
                dalsi_servis = PlanovanyServisZariadenia.objects.select_related('typ_servisu').filter(zariadenie=zariadenie, upozornenie_odoslane=False).order_by('planovany_datum').first()
            except Exception:
                dalsi_servis = None
            
            tel_list = []
            if posledna_telemetria:
                analog_dict = posledna_telemetria.analogove_hodnoty if isinstance(posledna_telemetria.analogove_hodnoty, dict) else {}
                tel_15m_dict = tel_15m.analogove_hodnoty if (tel_15m and isinstance(tel_15m.analogove_hodnoty, dict)) else {}
                tel_1h_dict = tel_1h.analogove_hodnoty if (tel_1h and isinstance(tel_1h.analogove_hodnoty, dict)) else {}
                tel_24h_dict = tel_24h.analogove_hodnoty if (tel_24h and isinstance(tel_24h.analogove_hodnoty, dict)) else {}
                tel_7d_dict = tel_7d.analogove_hodnoty if (tel_7d and isinstance(tel_7d.analogove_hodnoty, dict)) else {}

                # 🛡️ ISAS ARCHITECT: Neutrálna evaluácia telemetrie cez compliance modul odvetvia (Zero Hardcode)
                compliance_mod = get_industry_compliance()
                telemetria_alarm = None
                if compliance_mod and hasattr(compliance_mod, 'vyhodnot_iot_telemetriu'):
                    try:
                        telemetria_alarm = compliance_mod.vyhodnot_iot_telemetriu(
                            zariadenie, analog_dict, digitalne_stavy=posledna_telemetria.digitalne_stavy
                        )
                    except Exception as err:
                        logger.warning("ISAS AI Šepkár: Odvetvová telemetria vyvolala výnimku: %s", err)

                # 🛡️ ISAS ARCHITECT: 100% polymorfné načítanie medzných IoT limitov cez zmluvný GaugeLimitsMixin
                param_obj = get_zariadenie_parametre(zariadenie)
                gauge_limity = {}
                if param_obj and hasattr(param_obj, 'get_gauge_limits'):
                    try:
                        gauge_limity = param_obj.get_gauge_limits() or {}
                    except Exception as err:
                        logger.warning("ISAS AI Šepkár: get_gauge_limits zlyhalo: %s", err)

                # Štandardizovaná mapa normalizovaných aliasov telemetrických signálov
                alias_map = {
                    'cpu': ('cpu', 'cpu_usage', 'procesor'),
                    'ram': ('ram', 'ram_usage', 'pamat', 'memory'),
                    'latency': ('latency', 'network_latency_ms', 'ping', 'odozva'),
                    'error_rate': ('error_rate', 'http_error_rate', 'errors', 'chyby'),
                    'teplota': ('teplot', 'temp'),
                    'tlak': ('tlak', 'press', 'bar'),
                    'vibracie_rms': ('vibracie_rms', 'vibracie', 'rms'),
                    'vibracie_bpfo': ('vibracie_bpfo', 'bpfo', 'bsf'),
                }

                for k, v in analog_dict.items():
                    sig = slovnik.get(k)
                    nazov = sig.nazov_signalu if sig else k
                    jed = sig.merna_jednotka if (sig and sig.merna_jednotka) else ""
                    
                    hodnoty_trendu = []
                    if k in tel_15m_dict: hodnoty_trendu.append(f"-15m: {tel_15m_dict[k]}")
                    if k in tel_1h_dict: hodnoty_trendu.append(f"-1h: {tel_1h_dict[k]}")
                    if k in tel_24h_dict: hodnoty_trendu.append(f"-24h: {tel_24h_dict[k]}")
                    if k in tel_7d_dict: hodnoty_trendu.append(f"-7d: {tel_7d_dict[k]}")
                        
                    trend_text = f" [Časový vývoj: {', '.join(hodnoty_trendu)}]" if hodnoty_trendu else ""
                    
                    varovanie = ""
                    if v is not None and gauge_limity:
                        k_norm = str(k).lower().replace('-', '_').replace(' ', '_')
                        val_num = None
                        try:
                            val_num = float(str(v).replace(',', '.'))
                        except (ValueError, TypeError):
                            pass

                        if val_num is not None:
                            for lim_key, limit_val in gauge_limity.items():
                                try:
                                    lim_num = float(limit_val)
                                except (ValueError, TypeError):
                                    continue

                                aliases = alias_map.get(lim_key, (lim_key,))
                                if any(a in k_norm for a in aliases):
                                    if val_num > lim_num:
                                        varovanie = f" ⚠️ [PREKROČENÝ LIMIT {limit_val} {jed}!]"
                                        break
                    
                    tel_list.append(f"{nazov}: {v} {jed}{trend_text}{varovanie}".strip())

                if telemetria_alarm and isinstance(telemetria_alarm, dict) and telemetria_alarm.get('popis'):
                    tel_list.append(f"🚨 SRE/IoT KRITICKÉ VAROVANIE: {telemetria_alarm['popis']}")
                    
                digital_dict = posledna_telemetria.digitalne_stavy if isinstance(posledna_telemetria.digitalne_stavy, dict) else {}
                for k, v in digital_dict.items():
                    sig = slovnik.get(k)
                    nazov = sig.nazov_signalu if sig else k
                    tel_list.append(f"{nazov}: {'ZAP' if v else 'VYP'}")

            alarm_list = [f"[{a.zavaznost}] {a.kod_chyby} - {a.popis} (Snapshot: {a.snapshot_dat or 'Žiadny'})" for a in nevyriesene]
            plan_text = f"\nPlánovaná údržba: {dalsi_servis.typ_servisu.nazov if dalsi_servis and dalsi_servis.typ_servisu else 'Žiadna'}" if dalsi_servis else ""
            iot_dossier = f"Živé senzory: {', '.join(tel_list) if tel_list else 'Bez dát'}\nAktívne alarmy (Čierna skrinka): {'; '.join(alarm_list) if alarm_list else 'Žiadne'}{plan_text}"
            
        # 6. Špecifické parametre (Dynamicky načítané priamo pre kategóriu tohto zariadenia)
        specificke_parametre = ""
        obj_param = get_zariadenie_parametre(zariadenie)
        if obj_param:
            hodnoty = []
            for field in obj_param._meta.fields:
                # 🛡️ ISAS ARCHITECT: Vylúčenie revíznych a dátumových polí (rieši ich Compliance Dossier nižšie)
                if field.name not in ['id', 'zariadenie', 'vytvorene', 'upravene', 'vytvoril', 'upravil'] and not field.name.startswith(('datum_', 'ekvivalent_co2')):
                    val = getattr(obj_param, field.name)
                    if val is not None and val != '':
                        display_func = getattr(obj_param, f'get_{field.name}_display', None)
                        zobrazenie = display_func() if display_func else val
                        cisty_nazov = field.verbose_name.split(' ', 1)[-1] if ' ' in field.verbose_name else field.verbose_name
                        hodnoty.append(f"{cisty_nazov}: {zobrazenie}")
            if hodnoty:
                specificke_parametre = " | ".join(hodnoty)

        # 7. Compliance Dossier
        if compliance_alerty:
            compliance_riadky = [f"- {c['nazov']}: {c['stav_str']} | Inštrukcia: {c.get('ai_poznamka', 'Skontrolovať')} (Typ: {c['typ_lehoty']})" for c in compliance_alerty]
            compliance_dossier = "⚠️ EXPIROVANÉ / KONČIACE ZÁKONNÉ LEHOTY (VYŽADUJE ZÁSAH):\n" + "\n".join(compliance_riadky)
        else:
            compliance_dossier = "Všetky zákonné revízie a kontroly sú v norme a platné."

        # 8. AI Prompt a volanie
        term_zariadenie = get_industry_term('polozka_fallback_nazov', 'Zariadenie')
        term_zariadenie_gen = get_industry_term('zariadenie_genitiv', 'zariadenia')

        prompt = (
            f"Si hlavný inžinier prediktívnej údržby a diagnostiky (Predictive Maintenance & SRE Expert). Tvoj technik práve stojí pri zariadení ({term_zariadenie}):\n"
            f"Kategória: {kategoria}, Výrobca: {vyrobca}, Rok výroby/nasadenia: {vek}, {zariadenie.meter_label}: {zariadenie.meter_display if zariadenie.aktualne_motohodiny is not None else 'Neznáme'}.\n"
            f"Umiestnenie/Prostredie: {umiestnenie}. Interná poznámka k položke: {poznamka_stroja}.\n\n"
            f"Zákonný, bezpečnostný a revízny stav (Compliance):\n{compliance_dossier}\n\n"
            f"IoT Telemetria, Alarmy a Časový vývoj (-15m, -1h, -24h, -7d):\n{iot_dossier}{zive_poznamky_technika}\n\n"
            f"Technické parametre {term_zariadenie_gen}: {specificke_parametre if specificke_parametre else 'Nezadané'}.\n\n"
            f"História zásahov (vrátane vymenených dielov, nameraných hodnôt a príčin):\n{text_historie}\n\n"
            f"TVOJA ÚLOHA:\n"
            f"Zanalyzuj časový vývoj telemetrie, živé alarmy, servisnú históriu, vymenené diely a zákonné/bezpečnostné lehoty. "
            f"Tieto vstupné dáta NEOPAKUJ. Odhoď omáčku a odhaľ skrytú príčinu poruchy alebo predikuj najbližšie zlyhanie.\n\n"
            f"STRIKTNÉ PRAVIDLÁ PRE ODPOVEĎ:\n"
            f"1. Maximálne 3 stručné odrážky (použi LEN znak '- '). Žiadny úvod, záver ani nadpisy.\n"
            f"2. ANALÝZA TRENDU: Porovnaj hodnoty v Časovom vývoji. Ak parameter prudko rastie za posledných 15 minút, ide o akútnu anomáliu (napr. preťaženie, náhly skok teploty/odozvy). Ak rastie plynule v horizonte dní, ide o opotrebovanie, degradáciu alebo zanášanie kapacity.\n"
            f"3. KORELÁCIA DIELOV: Ak bol v histórii nedávno vymenený komponent či diel, ale súvisiaci prevádzkový parameter stále degraduje, upozorni technika na možnú chybu konfigurácie/montáže alebo vadný diel.\n"
            f"4. AKČNÝ PRÍKAZ A PREDPISY: Daj technikovi presný fyzický či diagnostický úkon, čo má na mieste okamžite overiť. Ak sú v zadaní expirované alebo končiace lehoty (predpísané revízie, bezpečnostné audity, kontroly únikov, inšpekcie), prikáž technikovi ich riešenie priamo pri tomto zásahu.\n"
            f"5. FORMÁT: Píš výhradne v slovenčine. NEPOUŽÍVAJ žiadne markdown formátovanie (žiadne hviezdičky **, tučné písmo ani kurzívu)."
        )
        
        api_key = get_gemini_api_key()
        gemini_model = getattr(settings, 'GEMINI_MODEL', None)
        if not api_key or not gemini_model:
            logger.warning("ISAS AI Šepkár: Chýba Gemini API kľúč alebo model v konfigurácii.")
            return None

        try:
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model=gemini_model, 
                contents=[prompt],
                config=types.GenerateContentConfig(thinking_config=types.ThinkingConfig(thinking_level=getattr(settings, 'GEMINI_THINKING_LEVEL', 'MEDIUM') or 'MEDIUM'))
            )
            
            raw_text = response.text
            if not raw_text:
                logger.warning("ISAS AI Šepkár: AI vrátilo prázdnu odpoveď.")
                return None
                
            ai_text = escape(raw_text.replace('**', ''))
            cache.set(cache_key, ai_text, timeout=cache_timeout)
        except Exception as e:
            logger.error(f"ISAS AI Šepkár zlyhal pri volaní Gemini API: {e}", exc_info=True)
            return None

    return ai_text

def ziskaj_aktualny_stav_skladu(stock_movement_obj):
    """ISAS CORE MÁGIA: Keďže StockMovement nemá ForeignKey, nájdeme reálny stav cez text a CATEGORY_MAP."""
    try:
        from .industry_loader import CATEGORY_MAP

        cat = str(stock_movement_obj.category).strip()
        subcat = str(stock_movement_obj.subcategory).strip()
        
        # 1. Vyčistíme názov od starých prívlastkov
        cisty_nazov = ocisti_nazov_od_jednotky(stock_movement_obj.product_name)
        
        # 2. Nájdeme správny model v databáze
        model_class = CATEGORY_MAP.get(cat, {}).get(subcat)
        
        if model_class and cisty_nazov:
            # ISAS CORE FIX: Dynamické hľadanie cez indexované polia (O(1) DB Query namiesto Full Table Scan)
            from django.db.models import Q
            q_obj = Q()
                    
            for field in ['seriove_cislo', 'nazov', 'oznacenie', 'model', 'name']:
                if hasattr(model_class, field):
                    q_obj |= Q(**{f"{field}__icontains": cisty_nazov})
                    
            if q_obj:
                prod = model_class.objects.filter(q_obj).first()
                if prod:
                    return getattr(prod, 'pocet_na_sklade', '?')

            # 3. Fallback: Záchranná sieť pre atypické modely (ak zlyhá DB filter)
            if cisty_nazov:
                for prod in model_class.objects.all().iterator():
                    prod_str = ocisti_nazov_od_jednotky(prod)
                    
                    if cisty_nazov == prod_str or cisty_nazov in prod_str:
                        return getattr(prod, 'pocet_na_sklade', '?')
    except Exception:
        pass
    
    return '?'

class ISASTaskConversionEngine:
    """
    🚀 ISAS CORE: Ultimátny konverzný engine.
    Pretaví Objednávku alebo Ponuku na realizačnú Úlohu.
    Rieši SOP, absencie, klonovanie materiálu a spúšťa JIT logistiku.
    """
    @staticmethod
    def convert_to_task(request, source_type, source_id, payload):
        from Evidencia.models import CenovaPonuka, PrijataObjednavka, Uloha, PouzityMaterial, Nepritomnost, PovolenieNaVstup
        
        payload = payload or {} if isinstance(payload, dict) else {}
        datum_cas_raw = payload.get('datum_cas')
        raw_tech_ids = payload.get('technici_ids') or []
        if not isinstance(raw_tech_ids, (list, tuple, set)):
            raw_tech_ids = [raw_tech_ids]

        # Striktná číselná sanitizácia zamedzujúca ValueError v ORM lookupe
        technici_ids = [parsed for t in raw_tech_ids if (parsed := parse_int(t, None)) is not None]
        
        raw_typ_ids = payload.get('typ_servisu_ids') or []
        if not isinstance(raw_typ_ids, (list, tuple, set)):
            raw_typ_ids = [raw_typ_ids]
        typ_servisu_ids = [parsed for t in raw_typ_ids if (parsed := parse_int(t, None)) is not None]
        
        poznamka_z_modalu = str(payload.get('poznamka') or '').strip()
        notif_tech_tg = payload.get('notif_tech_tg', True)
        notif_tech_mail = payload.get('notif_tech_mail', True)
        notif_tech_discord = payload.get('notif_tech_discord', True)
        notif_jit_tg = payload.get('notif_jit_tg', True)
        notif_jit_mail = payload.get('notif_jit_mail', True)
        notif_jit_discord = payload.get('notif_jit_discord', True)

        if isinstance(datum_cas_raw, datetime):
            zaciatok_ulohy = datum_cas_raw
        elif hasattr(datum_cas_raw, 'year') and hasattr(datum_cas_raw, 'month') and hasattr(datum_cas_raw, 'day') and not hasattr(datum_cas_raw, 'hour'):
            zaciatok_ulohy = datetime.combine(datum_cas_raw, datetime.min.time())
        else:
            parsed_dt = parse_datetime(str(datum_cas_raw)) if datum_cas_raw else None
            if not parsed_dt and datum_cas_raw:
                parsed_d = parse_date(datum_cas_raw)
                if parsed_d:
                    parsed_dt = datetime.combine(parsed_d, datetime.min.time())
            zaciatok_ulohy = parsed_dt or timezone.now()
        if timezone.is_aware(zaciatok_ulohy):
            zaciatok_ulohy = timezone.make_naive(zaciatok_ulohy)

        with transaction.atomic():
            # 1. NAČÍTANIE A UZAMKNUTIE ZDROJA
            if source_type == 'ponuka':
                try:
                    zdroj = CenovaPonuka.objects.select_for_update().prefetch_related('typ_servisu', 'polozky__zariadenie', 'objednavky').get(id=source_id)
                except (CenovaPonuka.DoesNotExist, ValueError, TypeError):
                    return JsonResponse({"status": "error", "message": "Ponuka neexistuje alebo bolo zadané neplatné ID."})
                if zdroj.stav == 'CONVERTED':
                    return JsonResponse({"status": "error", "message": "Táto ponuka už bola konvertovaná na úlohu."})
            elif source_type == 'objednavka':
                try:
                    zdroj = PrijataObjednavka.objects.select_for_update().prefetch_related('typ_servisu', 'zdrojove_ponuky__polozky__zariadenie', 'zdrojove_ponuky__typ_servisu').get(id=source_id)
                except (PrijataObjednavka.DoesNotExist, ValueError, TypeError):
                    return JsonResponse({"status": "error", "message": "Objednávka neexistuje alebo bolo zadané neplatné ID."})
                if zdroj.vytvorene_ulohy.exists():
                    return JsonResponse({"status": "error", "message": "Úloha pre túto objednávku už bola vygenerovaná."})
            else:
                return JsonResponse({"status": "error", "message": f"Neplatný typ zdroja pre konverziu: '{source_type}'."})

            # 2. KONTROLA ABSENCIÍ
            if technici_ids:
                datum_ulohy = zaciatok_ulohy.date()
                absenti = Nepritomnost.objects.filter(
                    zamestnanec_id__in=technici_ids, stav='SCHVALENE',
                    datum_od__lte=datum_ulohy, datum_do__gte=datum_ulohy
                ).select_related('zamestnanec')
                
                if absenti.exists():
                    mena = ", ".join([f"{a.zamestnanec.meno} {a.zamestnanec.priezvisko} ({a.get_typ_display()})" for a in absenti])
                    return JsonResponse({"status": "error", "message": f"🚫 KONVERZIA ZAMIETNUTÁ: Nasledovní technici majú absenciu: {mena}"})

            # 3. GENERÁTOR SOP (Pracovných postupov)
            from Evidencia.models import TypServisuPrace
            
            # Zistíme finálny zoznam typov prác (buď z modálu, alebo zo zdroja ako fallback)
            if typ_servisu_ids:
                aktivne_typy_prac = list(TypServisuPrace.objects.filter(id__in=typ_servisu_ids))
            else:
                aktivne_typy_prac = list(zdroj.typ_servisu.all())
                if source_type == 'objednavka':
                    for p in zdroj.zdrojove_ponuky.all():
                        for pt in p.typ_servisu.all():
                            if pt not in aktivne_typy_prac:
                                aktivne_typy_prac.append(pt)

            popis = ""
            if source_type == 'ponuka':
                popis = f"🚨 VYGENEROVANÉ ZO SCHVÁLENEJ PONUKY: {zdroj.cislo_ponuky}\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            else:
                popis = f"🚨 VÝJAZD Z OBJEDNÁVKY: {zdroj.cislo_objednavky_interne}\n"
                if zdroj.cislo_objednavky_klienta: popis += f"PO Number: {zdroj.cislo_objednavky_klienta}\n"
                if zdroj.text_emailu: popis += f"\nText od klienta:\n{zdroj.text_emailu}"
                elif zdroj.poznamka_klienta: popis += f"\nPoznámka klienta:\n{zdroj.poznamka_klienta}"
                
                sparovane_ponuky = list(zdroj.zdrojove_ponuky.all())
                if sparovane_ponuky:
                    popis += f"\n\n🔗 SPÁROVANÉ S PONUKAMI (Zberná objednávka):"
                    for p in sparovane_ponuky:
                        p_mena = getattr(p, 'mena', None) or get_firemna_mena()
                        popis += f"\n- {p.cislo_ponuky} (Rozpočet: {p.celkom_s_dph} {p_mena} s DPH)"

            if aktivne_typy_prac:
                popis += "\n\n📋 PRACOVNÉ POSTUPY A ÚKONY (SOP):\n"
                for praca in aktivne_typy_prac:
                    postup = praca.pracovny_postup if praca.pracovny_postup else "Štandardný rutinný postup pre tento úkon."
                    popis += f"▪️ {praca.nazov}:\n   {postup}\n\n"

            if zdroj.zakaznik and getattr(zdroj.zakaznik, 'vyzaduje_povolenie_vstupu', False):
                popis += f"\n\n🚧 POZOR: TENTO ZÁKAZNÍK VYŽADUJE POVOLENIE NA VSTUP (GATE PASS)!"
                if getattr(zdroj.zakaznik, 'instrukcie_vstupu', None):
                    popis += f"\n📋 Inštrukcie pre vrátnicu: {zdroj.zakaznik.instrukcie_vstupu}"

            if poznamka_z_modalu:
                popis += f"\n\n📝 BLIŽŠIE INFORMÁCIE Z DISPEČINGU:\n{poznamka_z_modalu}"

            # 🧠 ISAS AI: Automatická detekcia reklamácie (RMA)
            is_warranty = False
            warranty_date = None
            if source_type == 'objednavka':
                text_na_analyzu = f"{zdroj.interna_poznamka or ''} {zdroj.poznamka_klienta or ''}".upper()
                if 'REKLAMÁCIA' in text_na_analyzu or 'REKLAMACIA' in text_na_analyzu:
                    is_warranty = True
                    warranty_date = zdroj.datum_prijatia

            # 4. VYTVORENIE ÚLOHY
            farba = "#10b981" if source_type == 'ponuka' or (source_type == 'objednavka' and zdroj.zdrojove_ponuky.exists()) else "#f59e0b"
            
            # Ak ide o reklamáciu, prebijeme farbu na výstražnú červenú
            if is_warranty:
                farba = "#ef4444"
                
            nova_uloha = Uloha.objects.create(
                zakaznik=zdroj.zakaznik, 
                zaciatok=zaciatok_ulohy,
                blizsie_informacie=popis, 
                farba=farba,
                je_reklamacia=is_warranty,
                datum_prijatia_reklamacie=warranty_date
            )
            
            if technici_ids: nova_uloha.technici.set(technici_ids)

            if zdroj.zakaznik and getattr(zdroj.zakaznik, 'vyzaduje_povolenie_vstupu', False):
                PovolenieNaVstup.objects.get_or_create(
                    zakaznik=zdroj.zakaznik, uloha=nova_uloha,
                    datum_vstupu=nova_uloha.zaciatok.date(), defaults={'stav': 'GENEROVANE'}
                )

            # 5. KLONOVANIE MATERIÁLU, ZARIADENÍ A TYPOV PRÁC
            vsetky_zariadenia = set()
            
            # Nastavenie finálnych typov servisu (z modálu / zjednotených dát)
            if aktivne_typy_prac:
                nova_uloha.typ_servisu.set(aktivne_typy_prac)

            if source_type == 'ponuka':
                if zdroj.zariadenie_id: vsetky_zariadenia.add(zdroj.zariadenie_id)
                ponuky_na_spracovanie = [zdroj]
            else:
                vsetky_zariadenia.update(zdroj.zariadenia.values_list('id', flat=True))
                ponuky_na_spracovanie = list(zdroj.zdrojove_ponuky.all())

            for p in ponuky_na_spracovanie:
                if p.zariadenie_id: vsetky_zariadenia.add(p.zariadenie_id)
                
                for polozka in p.polozky.all():
                    if polozka.zariadenie_id: vsetky_zariadenia.add(polozka.zariadenie_id)
                    if polozka.typ_polozky == 'MAT' and polozka.content_type and polozka.object_id:
                        pocet_ks = math.ceil(parse_decimal(polozka.mnozstvo, Decimal('0.00')))
                        if pocet_ks > 0:
                            PouzityMaterial.objects.create(
                                uloha=nova_uloha, content_type=polozka.content_type,
                                object_id=polozka.object_id, pocet=pocet_ks
                            )

            if vsetky_zariadenia:
                nova_uloha.zariadenia.add(*list(vsetky_zariadenia))

            # 6. SYNCHRONIZÁCIA STAVOV (M:N)
            if source_type == 'ponuka':
                zdroj.stav = 'CONVERTED'
                zdroj.save(update_fields=['stav'])
                zdroj.vytvorene_ulohy.add(nova_uloha)
                for obj in zdroj.objednavky.all():
                    obj.vytvorene_ulohy.add(nova_uloha)
                    if obj.stav == 'NOVA':
                        obj.stav = 'SCHVALENA'
                        obj.save(update_fields=['stav'])
            else:
                zdroj.vytvorene_ulohy.add(nova_uloha)
                if zdroj.stav == 'NOVA':
                    zdroj.stav = 'SCHVALENA'
                    zdroj.save(update_fields=['stav'])
                for ponuka in ponuky_na_spracovanie:
                    ponuka.vytvorene_ulohy.add(nova_uloha)
                    ponuka.stav = 'CONVERTED'
                    ponuka.save(update_fields=['stav'])

            # 7. ASYNC ROUTING (Notifikácie a JIT Sklad)
            if notif_tech_tg or notif_tech_mail or notif_tech_discord:
                from Evidencia.tasks import task_notifikuj_technikov_o_ulohe
                def _odosli_notifikacie_technikov(u_id=nova_uloha.id):
                    try:
                        task_notifikuj_technikov_o_ulohe.delay(
                            uloha_id=u_id, change=False, stary_material={}, 
                            poslat_tg=notif_tech_tg, poslat_mail=notif_tech_mail, poslat_discord=notif_tech_discord
                        )
                    except Exception as exc:
                        logger.error(f"Zlyhalo odoslanie úlohy notifikácie technikov do Celery brokera: {exc}", exc_info=True)
                transaction.on_commit(_odosli_notifikacie_technikov)
                
            if notif_jit_tg or notif_jit_mail or notif_jit_discord:
                from Evidencia.tasks import task_skontroluj_sklad_jit
                def _odosli_jit_kontrolu(u_id=nova_uloha.id):
                    try:
                        task_skontroluj_sklad_jit.delay(
                            uloha_id=u_id, poslat_tg=notif_jit_tg, poslat_mail=notif_jit_mail, poslat_discord=notif_jit_discord
                        )
                    except Exception as exc:
                        logger.error(f"Zlyhalo odoslanie JIT skladovej úlohy do Celery brokera: {exc}", exc_info=True)
                transaction.on_commit(_odosli_jit_kontrolu)

            redirect_url = reverse('admin:Evidencia_uloha_change', args=[nova_uloha.id])
            return JsonResponse({
                "status": "success", 
                "message": "⚙️ Úloha bola úspešne vygenerovaná! Presmerovávam...",
                "redirect_url": redirect_url
            })

class ISASNotifier:
    """
    Centrálna trieda pre odosielanie unifikovaných notifikácií (Telegram + E-mail + Discord).
    Aplikuje striktný DRY princíp a fail-safe mechanizmy. Podporuje prílohy, dynamických príjemcov a Discord Embedy.
    """
    
    @staticmethod
    def _clean_html_for_telegram(html_text):
        """
        100% Defenzívny HTML parser pre Telegram (Enterprise Grade).
        - Escapuje nebezpečné znaky v texte.
        - Odstraňuje nepovolené tagy.
        - Automaticky uzatvára nespárované tagy (Tag Balancer).
        """
        if html_text is None or html_text == "":
            return ""

        text = str(html_text)
        text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'<hr\s*/?>', '\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n', text, flags=re.IGNORECASE)
        
        import html
        # Unescape na začiatku zabráni double-escapingu, ak už text bol čiastočne escapovaný
        text = html.unescape(text)

        allowed_tags = {'b', 'strong', 'i', 'em', 'u', 's', 'a', 'code', 'pre'}
        result = []
        open_tags = []
        current_len = 0
        
        # Regex rozdelí string na tagy a textové uzly
        tag_pattern = re.compile(r'(</?[a-zA-Z0-9]+(?:[^>]*?)>)')
        parts = tag_pattern.split(text)
        
        for part in parts:
            if not part:
                continue
                
            if tag_pattern.match(part):
                is_closing = part.startswith('</')
                tag_name_match = re.match(r'</?([a-zA-Z0-9]+)', part)
                
                if tag_name_match:
                    tag_name = tag_name_match.group(1).lower()
                    
                    if tag_name in allowed_tags:
                        if not is_closing:
                            open_tags.append(tag_name)
                            # Telegram nepovoľuje atribúty na štandardných formátovacích tagoch (spôsobuje 400 Bad Request)
                            if tag_name in {'b', 'strong', 'i', 'em', 'u', 's', 'code', 'pre'}:
                                safe_tag = f"<{tag_name}>"
                                result.append(safe_tag)
                                current_len += len(safe_tag)
                            elif tag_name == 'a':
                                href_match = re.search(r'href=["\']([^"\']+)["\']', part, flags=re.IGNORECASE)
                                # Telegram striktne vyžaduje schémy http://, https:// alebo tg:// (iné vrátia HTTP 400)
                                if href_match and href_match.group(1).strip().startswith(('http://', 'https://', 'tg://')):
                                    raw_url = href_match.group(1).strip().replace('"', '%22')
                                    # Enkódovanie rezervovaných HTML znakov vo vnútri href parametra pre Telegram API
                                    clean_url = raw_url.replace('&amp;', '&').replace('&', '&amp;').replace('<', '%3C').replace('>', '%3E')
                                    safe_tag = f'<a href="{clean_url}">'
                                    result.append(safe_tag)
                                    current_len += len(safe_tag)
                                else:
                                    # Vyradí tag bez validnej URL, balancer neuzavrie neexistujúci tag
                                    open_tags.pop()
                        else:
                            if tag_name in open_tags:
                                # Uzatvára tagy v správnom poradí (LIFO), kým nenájde ten správny
                                while open_tags:
                                    popped = open_tags.pop()
                                    closing_str = f"</{popped}>"
                                    result.append(closing_str)
                                    current_len += len(closing_str)
                                    if popped == tag_name:
                                        break
                    else:
                        # Ignoruje nepovolené tagy (tým ich potichu odstráni)
                        pass
            else:
                escaped_text = html.escape(part)
                # 🛡️ ISAS ARCHITECT: Limit Telegramu je 4096 znakov. Nastavíme bezpečný strop 3800 znakov,
                # aby zostala rezerva na uzatváracie tagy a systémovú pätku.
                if current_len + len(escaped_text) > 3800:
                    allowed_len = max(0, 3800 - current_len - 60)
                    raw_slice = escaped_text[:allowed_len]
                    # Ochrana pred rozseknutím entity (napr. '&am' -> '')
                    clean_slice = re.sub(r'&[a-zA-Z0-9#]*$', '', raw_slice)
                    truncated_part = clean_slice + "\n\n... [Text skrátený systémom ISAS CORE]"
                    result.append(truncated_part)
                    break # Zastavíme spracovanie a necháme Tag Balancer uzavrieť tagy
                else:
                    result.append(escaped_text)
                    current_len += len(escaped_text)
                
        # Uzatvorí všetky tagy, ktoré zostali otvorené na konci textu (aj po orezaní!)
        while open_tags:
            result.append(f"</{open_tags.pop()}>")
            
        final_text = "".join(result)
        
        # Telegram špecifiká (nepodporuje niektoré entity a zlyháva na &nbsp;)
        final_text = final_text.replace('&nbsp;', ' ').replace('&#x27;', "'").replace('&#39;', "'").replace('&quot;', '"')
        
        return final_text.strip()

    @staticmethod
    def _clean_html_for_discord(html_text):
        """
        Prevedie HTML značky z ISAS do plne formátovaného Discord Markdownu.
        """
        if html_text is None or html_text == "":
            return ""

        text = str(html_text)
        text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'<hr\s*/?>', '\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n', text, flags=re.IGNORECASE)
        text = re.sub(r'<(?:b|strong)(?:\s+[^>]*)?>(.*?)</(?:b|strong)>', r'**\1**', text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r'<(?:i|em)(?:\s+[^>]*)?>(.*?)</(?:i|em)>', r'*\1*', text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', r'[\2](\1)', text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r'<code>(.*?)</code>', r'`\1`', text, flags=re.IGNORECASE | re.DOTALL)
        text = strip_tags(text)
        import html
        return html.unescape(text).strip()

    @staticmethod
    def send(payload, to_email=None, tg_chat_id=None, discord_id=None, send_tg=True, send_mail=True, send_discord=True, attachments=None):
        """
        Odošle unifikovanú notifikáciu na základe štandardizovaného ISASNotificationBuilder payloadu.
        
        :param payload: Dict s dátami (ikona, nadpis, farba_temy, polozky_tabulky, hlavny_text, tlacidlo, ai_poznamka)
        :param to_email: String alebo List e-mailov.
        :param tg_chat_id: String/Int Telegram ID.
        :param discord_id: String/Int Discord User ID.
        :param send_tg: Bool, či sa má poslať Telegram.
        :param send_mail: Bool, či sa má poslať E-mail.
        :param send_discord: Bool, či sa má poslať Discord.
        :param attachments: List slovníkov: [{'filename': 'subor.pdf', 'content': bytes_data, 'mimetype': 'application/pdf'}]
        """
        GlobalneNastavenia = apps.get_model('ISASaplikacia', 'GlobalneNastavenia')
        nastavenia = cache.get('isas_globalne_nastavenia')
        if nastavenia is None:
            nastavenia = GlobalneNastavenia.objects.first()
            if nastavenia:
                cache.set('isas_globalne_nastavenia', nastavenia, timeout=300)
        
        if not nastavenia:
            logger.error("ISASNotifier: Chýbajú Globálne nastavenia v databáze.")
            return

        if not isinstance(payload, dict):
            logger.error("ISASNotifier: Payload musí byť slovník (dict).")
            return

        # Defenzívna normalizácia a regex validácia farby pre striktný CSS hex formát (RGB 3 alebo 6 hex znakov)
        farba_raw = str(payload.get('farba_temy') or '').strip().lstrip('#')
        if re.fullmatch(r'[0-9a-fA-F]{3}', farba_raw):
            farba_raw = ''.join([c * 2 for c in farba_raw])
        payload['farba_temy'] = f"#{farba_raw}" if re.fullmatch(r'[0-9a-fA-F]{6}', farba_raw) else '#3b82f6'

        # Defenzívna normalizácia: garantuje zoznam 2-prvkových tuplov (kluc, hodnota) voči ValueError
        polozky_raw = payload.get('polozky_tabulky')
        if isinstance(polozky_raw, dict):
            polozky_raw = list(polozky_raw.items())
        elif not isinstance(polozky_raw, (list, tuple, set)):
            polozky_raw = []

        ciste_polozky = []
        for p in polozky_raw:
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                # Striktná normalizácia kľúča aj hodnoty (None a prázdne reťazce na '—', zachovanie 0 a False)
                kluc_cisty = '—' if (p[0] is None or str(p[0]).strip() == '') else (p[0] if isinstance(p[0], (int, float, bool)) else str(p[0]).strip())
                hodnota_cista = '—' if (p[1] is None or str(p[1]).strip() == '') else p[1]
                ciste_polozky.append((kluc_cisty, hodnota_cista))
            elif isinstance(p, (list, tuple)) and len(p) == 1:
                kluc_cisty = '—' if (p[0] is None or str(p[0]).strip() == '') else (p[0] if isinstance(p[0], (int, float, bool)) else str(p[0]).strip())
                ciste_polozky.append((kluc_cisty, '—'))
            elif p is not None:
                val_cista = '—' if str(p).strip() == '' else (p if isinstance(p, (int, float, bool)) else str(p).strip())
                ciste_polozky.append((val_cista, '—'))
        payload['polozky_tabulky'] = ciste_polozky

        # ==========================================
        # 1. DISCORD NOTIFIKÁCIA (REST API DM / EMBED)
        # ==========================================
        if send_discord and nastavenia.discord_bot_token:
            target_id = discord_id if (discord_id and str(discord_id).strip()) else getattr(nastavenia, 'discord_id_majitel', None)
            if target_id and str(target_id).strip():
                # Defenzívne odstránenie akýchkoľvek bielych znakov voči Memcached/Redis InvalidCacheKey chybám
                target_discord_id = re.sub(r'\s+', '', str(target_id))
                try:
                    headers = {
                        "Authorization": f"Bot {nastavenia.discord_bot_token}",
                        "Content-Type": "application/json"
                    }
                    
                    # 1.1 Získanie DM kanála z cache alebo jeho bezpečné otvorenie
                    cache_key_dm = f"discord_dm_channel_{target_discord_id}"
                    channel_id = cache.get(cache_key_dm)

                    if not channel_id:
                        dm_channel_res = requests.post(
                            "https://discord.com/api/v10/users/@me/channels",
                            json={"recipient_id": target_discord_id},
                            headers=headers,
                            timeout=5
                        )
                        if dm_channel_res.status_code in [200, 201]:
                            channel_id = dm_channel_res.json().get("id")
                            if channel_id:
                                cache.set(cache_key_dm, channel_id, timeout=86400) # Cache na 24 hodín
                        else:
                            logger.error(f"ISASNotifier (Discord DM Kanál) zlyhal pre discord_id {target_discord_id}: HTTP {dm_channel_res.status_code} - {dm_channel_res.text}")

                    if channel_id:
                        # 1.2 Parsovanie farby témy (Discord API striktne vyžaduje celočíselný rozsah 0 až 16777215)
                        farba_hex = str(payload.get('farba_temy', '#3b82f6')).replace('#', '').strip()
                        try:
                            farba_int = min(max(0, int(farba_hex[:6], 16)), 0xFFFFFF)
                        except ValueError:
                            farba_int = 0x3B82F6
                            
                        # 1.3 Inicializácia Embedu
                        cisty_nadpis = strip_tags(str(payload.get('nadpis', ''))).strip()
                        prefix_ikona = f"{str(payload.get('ikona')).strip()} " if payload.get('ikona') else ""
                        embed_title = f"{prefix_ikona}ISAS CORE: {cisty_nadpis}"[:256].strip()
                        
                        embed = {
                            "title": embed_title,
                            "color": farba_int
                        }
                        total_embed_chars = len(embed_title)

                        # 1.4 Hlavný text a upozornenia (MAJÚ PRIORITU pred tabuľkou)
                        popis_casti = []
                        if payload.get('upozornenie_cervene'):
                            popis_casti.append(f"🚨 **UPOZORNENIE:** {ISASNotifier._clean_html_for_discord(payload['upozornenie_cervene'])}")
                        if payload.get('upozornenie_oranzove'):
                            popis_casti.append(f"⚠️ **VÝSTRAHA:** {ISASNotifier._clean_html_for_discord(payload['upozornenie_oranzove'])}")
                        if payload.get('hlavny_text'):
                            popis_casti.append(ISASNotifier._clean_html_for_discord(payload['hlavny_text']))
                        
                        full_desc = "\n\n".join(popis_casti)[:3500]
                        if payload.get('tlacidlo') and isinstance(payload['tlacidlo'], dict) and payload['tlacidlo'].get('url'):
                            btn_url = str(payload['tlacidlo']['url']).strip()
                            btn_txt = strip_tags(str(payload['tlacidlo'].get('text') or 'Otvoriť odkaz'))
                            if btn_url.startswith(('http://', 'https://')):
                                embed["url"] = btn_url
                                odkaz_md = f"\n\n🔗 [{btn_txt}]({btn_url})" if full_desc else f"🔗 [{btn_txt}]({btn_url})"
                                if len(full_desc) + len(odkaz_md) <= 4000:
                                    full_desc += odkaz_md
                        
                        if full_desc:
                            embed["description"] = full_desc
                            total_embed_chars += len(full_desc)

                        # 1.5 Pätka
                        if payload.get('ai_poznamka'):
                            footer_txt = f"🤖 {strip_tags(str(payload['ai_poznamka']))}"[:500]
                            embed["footer"] = {"text": footer_txt}
                            total_embed_chars += len(footer_txt)

                        # 1.6 Polia tabuľky (dopĺňajú sa dynamicky do bezpečného stropu 5800 znakov)
                        embed_fields = []
                        for kluc, hodnota in payload.get('polozky_tabulky', [])[:25]:
                            cisty_kluc = strip_tags(str(kluc)).strip()[:256]
                            cista_val = ISASNotifier._clean_html_for_discord(hodnota)[:1024]
                            
                            if cista_val and cisty_kluc:
                                if total_embed_chars + len(cisty_kluc) + len(cista_val) > 5800:
                                    break
                                embed_fields.append({
                                    "name": cisty_kluc,
                                    "value": cista_val,
                                    "inline": len(cista_val) < 35
                                })
                                total_embed_chars += len(cisty_kluc) + len(cista_val)

                        if embed_fields:
                            embed["fields"] = embed_fields
                            
                        # 1.7 Odoslanie správy
                        response = requests.post(
                            f"https://discord.com/api/v10/channels/{channel_id}/messages",
                            json={"embeds": [embed]},
                            headers=headers,
                            timeout=5
                        )
                        if response.status_code not in [200, 201]:
                            # Ak Discord vráti 404 (kanál zanikol), okamžite mažeme z cache pre obnovu pri ďalšom pokuse
                            if response.status_code == 404:
                                cache.delete(cache_key_dm)
                            logger.error(f"ISASNotifier (Discord) zlyhal pre discord_id {target_discord_id}: HTTP {response.status_code} - {response.text}")
                except Exception as e:
                    logger.error(f"ISASNotifier (Discord) zlyhal pre discord_id {target_discord_id}: {e}")

        # ==========================================
        # 2. TELEGRAM NOTIFIKÁCIA
        # ==========================================
        if send_tg and nastavenia.telegram_bot_token:
            target_tg = tg_chat_id if (tg_chat_id and str(tg_chat_id).strip()) else getattr(nastavenia, 'telegram_id_majitel', None)
            
            if target_tg and str(target_tg).strip():
                chat_id = str(target_tg).strip()
                try:
                    # 1. Poskladáme surové HTML (bez obáv o nebezpečné znaky)
                    prefix_ikona = f"{str(payload.get('ikona')).strip()} " if payload.get('ikona') else ""
                    nadpis_tg = str(payload.get('nadpis') or '').strip()
                    tg_lines = [f"{prefix_ikona}<b>ISAS CORE: {nadpis_tg}</b>\n".strip()]
                    
                    for kluc, hodnota in payload.get('polozky_tabulky', []):
                        tg_lines.append(f"<b>{kluc}</b> {hodnota}")
                    
                    if payload.get('upozornenie_cervene'):
                        tg_lines.append(f"\n🚨 <b>UPOZORNENIE:</b> {payload['upozornenie_cervene']}")
                    if payload.get('upozornenie_oranzove'):
                        tg_lines.append(f"\n⚠️ <b>VÝSTRAHA:</b> {payload['upozornenie_oranzove']}")
                    if payload.get('hlavny_text'):
                        tg_lines.append(f"\n{payload['hlavny_text']}")
                        
                    if payload.get('tlacidlo') and isinstance(payload['tlacidlo'], dict) and payload['tlacidlo'].get('url'):
                        btn_url = str(payload['tlacidlo']['url']).strip()
                        btn_txt = strip_tags(str(payload['tlacidlo'].get('text') or 'Zobraziť detail'))
                        if btn_url.startswith(('http://', 'https://', 'tg://')):
                            tg_lines.append(f"\n🔗 <a href='{btn_url}'>{btn_txt}</a>")
                        
                    if payload.get('ai_poznamka'):
                        tg_lines.append(f"\n🤖 <i>{payload['ai_poznamka']}</i>")
                        
                    raw_tg_text = "\n".join(tg_lines)
                    
                    # 2. Celý string preženieme cez náš 100% defenzívny parser
                    tg_text = ISASNotifier._clean_html_for_telegram(raw_tg_text)
                    
                    response = requests.post(
                        f"https://api.telegram.org/bot{nastavenia.telegram_bot_token}/sendMessage",
                        json={
                            'chat_id': chat_id, 
                            'text': tg_text, 
                            'parse_mode': 'HTML', 
                            'disable_web_page_preview': True
                        },
                        timeout=5
                    )
                    if response.status_code != 200:
                        logger.error(f"ISASNotifier (Telegram) zlyhal pre chat_id {chat_id}: HTTP {response.status_code} - {response.text}")
                except Exception as e:
                    logger.error(f"ISASNotifier (Telegram) zlyhal pre chat_id {chat_id}: {e}")

        # ==========================================
        # 3. E-MAIL NOTIFIKÁCIA
        # ==========================================
        if send_mail and nastavenia.smtp_user:
            # Spracovanie príjemcov do listu s elimináciou whitespace-only hodnôt a spoľahlivým fallbackom
            has_email = bool(str(to_email).strip()) if (to_email is not None and not isinstance(to_email, (list, tuple, set))) else bool(to_email)
            surovy_zoznam = list(to_email) if isinstance(to_email, (list, tuple, set)) else ([to_email] if has_email else ([nastavenia.email_majitel_notifikacie] if getattr(nastavenia, 'email_majitel_notifikacie', None) else []))
            prijimatelia = []
            for polozka in surovy_zoznam:
                if polozka:
                    # Striktné odstránenie riadiacich znakov \r a \n pred splitom voči BadHeaderError
                    sanitizovana_polozka = re.sub(r'[\r\n]+', '', str(polozka))
                    for adresa in sanitizovana_polozka.split(','):
                        cista = adresa.strip()
                        if cista and cista not in prijimatelia:
                            prijimatelia.append(cista)
            if not prijimatelia and getattr(nastavenia, 'email_majitel_notifikacie', None):
                majitel_email = re.sub(r'[\r\n]+', '', str(nastavenia.email_majitel_notifikacie)).strip()
                if majitel_email:
                    prijimatelia.append(majitel_email)

            if prijimatelia:
                try:
                    prefix_ikona = f"{str(payload.get('ikona')).strip()} " if payload.get('ikona') else ""
                    nadpis_mail = str(payload.get('nadpis') or '').strip()
                    raw_predmet = f"{prefix_ikona}ISAS CORE: {nadpis_mail}".strip()
                    predmet = " ".join(str(raw_predmet).split())
                    
                    # Plain-text fallback s unescape entít a konverziou odriadkovania
                    import html as py_html
                    plain_lines = [f"ISAS CORE: {payload.get('nadpis', '')}\n"]
                    for kluc, hodnota in payload.get('polozky_tabulky', []):
                        hodnota_txt = py_html.unescape(strip_tags(re.sub(r'<br\s*/?>', ' ', str(hodnota), flags=re.I)))
                        plain_lines.append(f"{kluc} {hodnota_txt}")
                    if payload.get('upozornenie_cervene'):
                        plain_lines.append(f"\n🚨 UPOZORNENIE:\n{py_html.unescape(strip_tags(re.sub(r'<br\s*/?>', chr(10), str(payload['upozornenie_cervene']), flags=re.I)))}")
                    if payload.get('upozornenie_oranzove'):
                        plain_lines.append(f"\n⚠️ VÝSTRAHA:\n{py_html.unescape(strip_tags(re.sub(r'<br\s*/?>', chr(10), str(payload['upozornenie_oranzove']), flags=re.I)))}")
                    if payload.get('hlavny_text'):
                        cisty_text = py_html.unescape(strip_tags(re.sub(r'<br\s*/?>', chr(10), str(payload['hlavny_text']), flags=re.I)))
                        plain_lines.append(f"\nDetaily:\n{cisty_text}")
                    if payload.get('tlacidlo') and isinstance(payload['tlacidlo'], dict) and payload['tlacidlo'].get('url'):
                        btn_u = str(payload['tlacidlo']['url']).strip()
                        if btn_u.startswith(('http://', 'https://')):
                            plain_lines.append(f"\nOdkaz: {btn_u}")
                    if payload.get('ai_poznamka'):
                        plain_lines.append(f"\nISAS AI: {py_html.unescape(strip_tags(str(payload['ai_poznamka'])))}")
                        
                    plain_text = "\n".join(plain_lines)
                    
                    # HTML verzia – dôsledná normalizácia existujúcich <br> tagov na \n, orezanie bielych znakov z URL a položiek
                    payload_mail = dict(payload)
                    if isinstance(payload_mail.get('tlacidlo'), dict) and payload_mail['tlacidlo'].get('url'):
                        payload_mail['tlacidlo'] = dict(payload_mail['tlacidlo'])
                        payload_mail['tlacidlo']['url'] = str(payload_mail['tlacidlo']['url']).strip()
                    if payload_mail.get('logo_url'):
                        payload_mail['logo_url'] = str(payload_mail['logo_url']).strip()
                    for pole in ['hlavny_text', 'upozornenie_cervene', 'upozornenie_oranzove', 'ai_poznamka']:
                        if payload_mail.get(pole):
                            payload_mail[pole] = re.sub(r'<br\s*/?>', '\n', str(payload_mail[pole]), flags=re.I)

                    if 'polozky_tabulky' in payload_mail:
                        payload_mail['polozky_tabulky'] = [
                            (
                                py_html.unescape(str(k)) if k not in (None, '—') else k,
                                py_html.unescape(re.sub(r'<br\s*/?>', '\n', str(v), flags=re.I)) if v not in (None, '—', 0, False) else v
                            )
                            for k, v in payload_mail['polozky_tabulky']
                        ]
                    
                    html_text = render_to_string('filters_and_notifications/univerzalna_notifikacia.html', {'payload': payload_mail})
                    
                    # Defenzívne overenie prítomnosti reálnej adresy s elimináciou whitespace a CRLF znakov
                    raw_firemny = getattr(nastavenia, 'firemny_email', None)
                    firemny_cisty = re.sub(r'[\r\n]+', '', str(raw_firemny)).strip() if raw_firemny and str(raw_firemny).strip() else None
                    odosielatel = firemny_cisty or re.sub(r'[\r\n]+', '', str(nastavenia.smtp_user)).strip()
                    
                    email_msg = EmailMultiAlternatives(
                        subject=predmet, 
                        body=plain_text, 
                        from_email=odosielatel, 
                        to=prijimatelia, 
                        using='default'
                    )

                    email_msg.attach_alternative(html_text, "text/html")
                    
                    # 📎 PRIDANIE PRÍLOH (s defenzívnou extrakciou .read() a validáciou prázdneho obsahu)
                    if attachments:
                        att_list = [attachments] if isinstance(attachments, dict) else (attachments if isinstance(attachments, (list, tuple, set)) else [])
                        for att in att_list:
                            fname, fcontent, fmim = None, None, 'application/octet-stream'
                            if isinstance(att, dict) and att.get('filename') and att.get('content') is not None:
                                fname = str(att['filename']).strip()
                                fcontent = att['content']
                                fmim = att.get('mimetype') or 'application/octet-stream'
                            elif isinstance(att, (list, tuple)) and len(att) >= 2 and att[0] and att[1] is not None:
                                fname = str(att[0]).strip()
                                fcontent = att[1]
                                fmim = att[2] if (len(att) >= 3 and att[2]) else 'application/octet-stream'
                            
                            if fname and fcontent is not None:
                                if hasattr(fcontent, 'seek') and callable(fcontent.seek):
                                    try:
                                        fcontent.seek(0)
                                    except Exception:
                                        pass
                                try:
                                    raw_bytes = fcontent.read() if (hasattr(fcontent, 'read') and callable(fcontent.read)) else fcontent
                                except Exception as err_read:
                                    logger.error(f"ISASNotifier: Zlyhalo načítanie prílohy {fname}: {err_read}")
                                    raw_bytes = None
                                
                                if raw_bytes is not None and isinstance(raw_bytes, (bytes, str)):
                                    email_msg.attach(fname, raw_bytes, fmim)
                    
                    email_msg.send()
                except Exception as e:
                    logger.error(f"ISASNotifier (E-mail) zlyhal pre {prijimatelia}: {e}")

class ISASNotificationBuilder:
    """
    ISAS CORE: Enterprise Builder Pattern pre unifikovanú tvorbu notifikačných payloadov.
    Znižuje cyklomatickú komplexitu a zabezpečuje striktný formát dát pre ISASNotifier.
    """
    def __init__(self, nadpis, ikona='', farba_temy='#3b82f6'):
        self.payload = {
            'nadpis': nadpis,
            'ikona': ikona,
            'farba_temy': farba_temy,
            'polozky_tabulky': []
        }

    def pridaj_riadok(self, kluc, hodnota):
        if hodnota is not None:
            self.payload['polozky_tabulky'].append((kluc, hodnota))
        return self

    def nastav_hlavny_text(self, text):
        if text:
            self.payload['hlavny_text'] = text
        return self

    def nastav_tlacidlo(self, text, url):
        if text and url:
            self.payload['tlacidlo'] = {'text': text, 'url': url}
        return self

    def nastav_ai_poznamku(self, text):
        if text:
            self.payload['ai_poznamka'] = text
        return self

    def nastav_upozornenie(self, text, typ='cervene'):
        if text:
            if typ == 'cervene':
                self.payload['upozornenie_cervene'] = text
            elif typ == 'oranzove':
                self.payload['upozornenie_oranzove'] = text
        return self

    def nastav_logo(self, url):
        if url:
            self.payload['logo_url'] = url
        return self

    def nastav_hlavicku(self, text):
        if text:
            self.payload['hlavicka_text'] = str(text).strip()
        return self

    def nastav_paticku(self, text):
        if text:
            self.payload['paticka_text'] = str(text).strip()
        return self

    def build(self):
        return self.payload

def generate_pdf_from_html(html_string):
    """
    Unifikovaný generátor PDF cez Playwright.
    Bezpečne izolovaný pre Celery workre.
    """
    ISAS_PDF_FOOTER = """
    <div style="width: 100%; text-align: center; font-size: 9px; font-family: 'Inter', Helvetica, sans-serif; color: #94a3b8; padding-bottom: 5px;">
        Strana <span class="pageNumber"></span> z <span class="totalPages"></span>
    </div>
    """
    
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, 
            args=['--disable-dev-shm-usage', '--no-sandbox', '--disable-setuid-sandbox', '--disable-gpu']
        )
        page = None
        try:
            page = browser.new_page()
            # wait_until="load" zamedzí Timeout chybám pri externých fontoch alebo pomalých sieťových assetoch
            try:
                page.set_content(html_string, wait_until="load", timeout=15000)
            except Exception:
                page.set_content(html_string, wait_until="domcontentloaded", timeout=10000)
                
            pdf_data = page.pdf(
                format="A4", 
                print_background=True, 
                margin={"top": "15mm", "bottom": "15mm", "left": "15mm", "right": "15mm"},
                display_header_footer=True,
                header_template="<div></div>",
                footer_template=ISAS_PDF_FOOTER
            )
            return pdf_data
        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass
            browser.close()

def format_material_list_for_notification(polozky, stary_material=None, is_change=False):
    """
    Unifikuje logiku zoskupovania materiálu pre e-maily a Telegram.
    Podporuje Django QuerySety aj bežné Python zoznamy.
    """
    # Bezpečná kontrola pre QuerySet aj bežný List
    if not polozky or (hasattr(polozky, 'exists') and not polozky.exists()):
        return ""
        
    choices_dict = dict(CATEGORY_CHOICES)
    zoskupeny_material = {display_name: [] for display_name in choices_dict.values()}
    zoskupeny_material["📦 Ostatné (Nezaradené)"] = []
    
    for p in polozky:
        if not getattr(p, 'content_object', None):
            continue
            
        model_cls = p.content_type.model_class() if p.content_type else None
        hlavna_kat = "📦 Ostatné (Nezaradené)"
        podkategoria_nazov = escape(p.content_type.model.title() if p.content_type else "Neznáme")
        
        for main_cat, subcats in CATEGORY_MAP.items():
            for subcat_name, subcat_model in subcats.items():
                if model_cls == subcat_model:
                    hlavna_kat = choices_dict.get(main_cat, main_cat)
                    podkategoria_nazov = escape(subcat_name)
                    break
            if hlavna_kat != "📦 Ostatné (Nezaradené)":
                break
                
        nazov_cisty = escape(ocisti_nazov_od_jednotky(p.content_object))
        jednotka = escape(getattr(p.content_object, 'merna_jednotka', 'ks') or 'ks')
        pocet_val = getattr(p, 'pocet', 0) or 0
        
        zmena_text = ""
        if is_change:
            stary_material_safe = stary_material if isinstance(stary_material, dict) else {}
            p_id_str = str(getattr(p, 'id', ''))
            if p_id_str and p_id_str in stary_material_safe:
                try:
                    stary_val = stary_material_safe.get(p_id_str) or 0
                    rozdiel = float(pocet_val) - float(stary_val)
                    if rozdiel > 0: zmena_text = f" (📈 +{rozdiel:g} {jednotka})"
                    elif rozdiel < 0: zmena_text = f" (📉 {rozdiel:g} {jednotka})"
                except (ValueError, TypeError):
                    pass
            else:
                zmena_text = " (✨ NOVÁ POLOŽKA)"
        
        zoskupeny_material.setdefault(hlavna_kat, []).append(f"  • [{podkategoria_nazov}] {nazov_cisty}: {pocet_val} {jednotka}{zmena_text}")
    
    zoznam_riadky = []
    for kat, polozky_v_kat in zoskupeny_material.items():
        if polozky_v_kat: 
            zoznam_riadky.append(f"<b>{kat}</b>")
            zoznam_riadky.extend(polozky_v_kat)
            zoznam_riadky.append("") 
    
    zoznam_dielov = "\n".join(zoznam_riadky).strip()
    if not zoznam_dielov:
        return ""
        
    return (
        f"\n🛒 <b>MATERIÁL:</b>\n"
        f"<hr>\n"
        f"{zoznam_dielov}\n"
    )

def process_imap_mailbox(lock_key, config_keys, processor_func):
    """
    Univerzálny IMAP Worker Factory pre ISAS CORE.
    Zapuzdruje pripojenie, zamykanie cache, iteráciu správ a error handling.
    """
    if not cache.add(lock_key, 'locked', timeout=600):
        return

    socket.setdefaulttimeout(30)
    close_old_connections()
    
    name = config_keys.get('name', 'Neznámy')
    logger.info(f"🚨 [ISAS CORE] Inicializácia IMAP spojenia pre {name}...")
    
    GlobalneNastavenia_model = apps.get_model('ISASaplikacia', 'GlobalneNastavenia')
    nastavenia = GlobalneNastavenia_model.objects.first()
    
    if not nastavenia:
        logger.error(f"❌ ISAS Poštár ({name}): Chýbajú globálne nastavenia.")
        cache.delete(lock_key)
        return
        
    imap_user = getattr(nastavenia, config_keys.get('user', ''), None)
    imap_password = getattr(nastavenia, config_keys.get('password', ''), None)
    imap_server = getattr(nastavenia, config_keys.get('server', ''), 'imap.gmail.com') or 'imap.gmail.com'
    
    if not imap_user or not imap_password:
        logger.error(f"❌ ISAS Poštár ({name}): Klient nemá nastavené prihlasovacie údaje.")
        cache.delete(lock_key)
        return

    try:
        with MailBox(imap_server).login(imap_user, imap_password) as mailbox:
            neprecitane_spravy = list(mailbox.fetch(AND(seen=False)))
            if not neprecitane_spravy:
                logger.info(f"✓ Žiadne nové správy ({name}).")
                return

            for msg in neprecitane_spravy:
                try:
                    processor_func(msg, nastavenia)
                except Exception as msg_err:
                    logger.error(f"⚠️ Chyba pri spracovaní e-mailu ({name}): {str(msg_err)}")
                    continue
    except Exception as e:
        logger.error(f"❌ Kritická chyba IMAP spojenia ({name}): {str(e)}")
    finally:
        cache.delete(lock_key)

class ISASExcelBuilder:
    """
    ISAS CORE: Enterprise Excel Engine.
    Zapuzdruje firemný styling a bezpečnú logiku ukladania do MegaZaloha.
    """
    def __init__(self, mena=None):
        self.wb = openpyxl.Workbook()
        self.header_font = Font(bold=True, color="FFFFFF")
        self.center_align = Alignment(horizontal="center", vertical="center")
        self.thin_border = Border(
            left=Side(style='thin'), right=Side(style='thin'), 
            top=Side(style='thin'), bottom=Side(style='thin')
        )
        self.summary_fill = PatternFill(start_color="f8fafc", end_color="f8fafc", fill_type="solid")
        aktualna_mena = mena or get_firemna_mena()
        self.currency_format = f'#,##0.00 {aktualna_mena}'

    def get_header_fill(self, color_hex="1e293b"):
        return PatternFill(start_color=color_hex, end_color=color_hex, fill_type="solid")

    def apply_header_style(self, ws, row_num, color_hex="1e293b"):
        """Aplikuje unifikovaný štýl na celý riadok hlavičky."""
        fill = self.get_header_fill(color_hex)
        for cell in ws[row_num]:
            cell.font = self.header_font
            cell.fill = fill
            cell.alignment = self.center_align
            cell.border = self.thin_border

    def save_to_megazaloha(self, zaloha_id, filename_prefix, user=None, default_name="Excel Export", default_note="", typ_zalohy="excel_export"):
        from Evidencia.models import MegaZaloha
        
        excel_buffer = io.BytesIO()
        self.wb.save(excel_buffer)
        excel_buffer.seek(0)
        
        _now = timezone.now()
        cas_suboru = (timezone.localtime(_now) if timezone.is_aware(_now) else _now).strftime('%Y%m%d_%H%M%S')
        nazov_suboru = f"{filename_prefix}_{cas_suboru}.xlsx"
        
        with transaction.atomic():
            zaloha = None
            if zaloha_id:
                # Ochrana pred Race Condition (ak by užívateľ klikol viackrát na stiahnutie)
                zaloha = MegaZaloha.objects.select_for_update().filter(id=zaloha_id).first()
                if zaloha:
                    zaloha.stav_spracovania = 'hotova'
            if not zaloha:
                safe_user = user if (user and getattr(user, 'is_authenticated', False)) else None
                zaloha = MegaZaloha.objects.create(
                    nazov_zalohy=default_name,
                    typ_zalohy=typ_zalohy,
                    stav_spracovania='hotova',
                    vyziadal_uzivatel=safe_user,
                    poznamka=default_note
                )
            
            zaloha.subor_zalohy.save(nazov_suboru, ContentFile(excel_buffer.read()), save=True)
            
        excel_buffer.close()
        return zaloha.id

class ISASPDFBuilder:
    """
    ISAS CORE: Enterprise PDF Factory (Facade/Builder).
    Centralizuje renderovanie HTML, injektovanie base_url pre Playwright a bezpečné ukladanie do modelov.
    """
    @classmethod
    def generate_and_save(cls, template_name, context, base_url, file_name, instance=None, file_field_name=None, save_instance=True, append_slash=False):
        # 1. Renderovanie HTML zo šablóny
        html_string = render_to_string(template_name, context)
        
        # 2. Bezpečná injektáž <base href> pre Playwright (načítanie statických súborov/obrázkov)
        if base_url and re.search(r'<head\b[^>]*>', html_string, re.IGNORECASE):
            cisty_base = base_url.rstrip('/') + '/'
            html_string = re.sub(r'(<head\b[^>]*>)', rf"\1<base href='{cisty_base}'>", html_string, count=1, flags=re.IGNORECASE)
            
        # 3. Generovanie PDF bajtov
        pdf_data = generate_pdf_from_html(html_string)
        
        # 4. Uloženie do inštancie modelu (ak je požadované)
        if instance and file_field_name and pdf_data:
            file_field = getattr(instance, file_field_name, None)
            if file_field and hasattr(file_field, 'save'):
                # Django FieldFile.save() automaticky volá instance.save(), ak je save=True
                file_field.save(name=file_name, content=ContentFile(pdf_data), save=save_instance)
            
        return pdf_data, file_name

class ISASImportStrategy:
    """
    ISAS CORE: Strategy Pattern pre dynamický import dát.
    Každá metóda extrahuje dáta z riadku, vykoná FK lookup a vráti (ModelClass, Query, Defaults).
    """
    @staticmethod
    def _resolve_stat(hodnota):
        """Dynamické určenie štátu cez StatLokalizacia a FiremnaIdentita bez hardkódu."""
        StatLokalizaciaModel = apps.get_model('ISASaplikacia', 'StatLokalizacia')
        FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
        
        val = str(hodnota or '').strip()
        if val:
            if StatLokalizaciaModel and hasattr(StatLokalizaciaModel, 'rozpoznaj_iso'):
                iso = StatLokalizaciaModel.rozpoznaj_iso(val)
                if iso not in ('NEZADANE', 'INYKRAJINA'):
                    stat_obj = StatLokalizaciaModel.get_for_iso(iso)
                    if stat_obj and stat_obj.nazov:
                        return stat_obj.nazov
            return val

        if FiremnaIdentitaModel:
            identita = FiremnaIdentitaModel.objects.first()
            if identita and identita.sidlo_stat:
                return identita.sidlo_stat
            defaults = FiremnaIdentitaModel.get_country_defaults()
            if defaults and defaults.get('stat_nazov'):
                return defaults['stat_nazov']
        return ''

    @staticmethod
    def parse_zakaznici(row, index, logy):
        ico = str(row.get('ICO') or '').strip()
        nazov = str(row.get('Nazov_spolocnosti') or '').strip()
        if not nazov: raise ValueError("Chýba Názov spoločnosti")
        
        query = Q(ico=ico) if ico else Q(Nazov_spolocnosti=nazov)
        oslovenie = str(row.get('Oslovenie_rod') or '').strip().lower()
        
        defaults = {
            'ico': ico, 'Nazov_spolocnosti': nazov, 'dic': str(row.get('DIC') or '').strip(),
            'ic_dph': str(row.get('IC_DPH') or '').strip(), 'ulica_a_cislo': str(row.get('Ulica_a_cislo') or '').strip(),
            'mesto': str(row.get('Mesto') or '').strip(), 'psc': str(row.get('PSC') or '').strip(),
            'stat': ISASImportStrategy._resolve_stat(row.get('Stat')),
            'email': str(row.get('email') or '').strip(), 'telefon': str(row.get('telefon') or '').strip(),
            'iban': str(row.get('IBAN') or '').strip(), 'domena': str(row.get('Domena') or '').strip(),
            'subdomena': str(row.get('Subdomena') or '').strip().lower() or None,
            'kontaktna_osoba': str(row.get('kontaktna_osoba') or '').strip(),
            'predvolena_splatnost_dni': parse_int(row.get('Predvolena_splatnost_dni'), 14),
            'reklamacna_lehota_dni': parse_int(row.get('Reklamacna_lehota_dni'), None) if row.get('Reklamacna_lehota_dni') else None,
            'oslovenie_rod': oslovenie if oslovenie in ['pán', 'pani'] else '',
            'datum_narodenia': parse_date(row.get('Datum_narodenia')),
            'vyzaduje_povolenie_vstupu': parse_bool(row.get('Vyzaduje_povolenie_vstupu')),
            'email_vratnice': str(row.get('Email_vratnice') or '').strip(), 'telefon_vratnica': str(row.get('Telefon_vratnica') or '').strip(),
            'instrukcie_vstupu': str(row.get('Instrukcie_vstupu') or '').strip(),
            'url_vstupneho_portalu': str(row.get('Url_vstupneho_portalu') or '').strip(),
            'cena_za_kwh_eur': parse_decimal(row.get('Cena_za_kwh_eur'), Decimal('0.000')),
            'emisny_faktor_co2': parse_decimal(row.get('Emisny_faktor_co2'), Decimal('0.0000')),
            'b2b_zlava_percenta': parse_decimal(row.get('B2b_zlava_percenta'), Decimal('0.00')),
            'zobrazit_ako_referenciu': parse_bool(row.get('Zobrazit_ako_referenciu', 'NIE')),
            'Ostatne': str(row.get('Ostatne') or '').strip(),
        }
        return Zakaznik, query, defaults

    @staticmethod
    def parse_dodavatelia(row, index, logy):
        ico = str(row.get('ICO') or '').strip()
        nazov = str(row.get('Nazov_spolocnosti') or '').strip()
        if not nazov: raise ValueError("Chýba Názov spoločnosti")
        
        query = Q(ico=ico) if ico else Q(Nazov_spolocnosti=nazov)
        oslovenie = str(row.get('Oslovenie_rod') or '').strip().lower()
        
        defaults = {
            'ico': ico, 'Nazov_spolocnosti': nazov, 'dic': str(row.get('DIC') or '').strip(),
            'ic_dph': str(row.get('IC_DPH') or '').strip(), 'ulica_a_cislo': str(row.get('Ulica_a_cislo') or '').strip(),
            'mesto': str(row.get('Mesto') or '').strip(), 'psc': str(row.get('PSC') or '').strip(),
            'stat': ISASImportStrategy._resolve_stat(row.get('Stat')),
            'email': str(row.get('email') or '').strip(), 'telefon': str(row.get('telefon') or '').strip(),
            'iban': str(row.get('IBAN') or '').strip(), 'kontaktna_osoba': str(row.get('kontaktna_osoba') or '').strip(),
            'domena': str(row.get('Domena') or '').strip(), 'predvolena_splatnost_dni': parse_int(row.get('Predvolena_splatnost_dni'), 14),
            'oslovenie_rod': oslovenie if oslovenie in ['pán', 'pani'] else '',
            'datum_narodenia': parse_date(row.get('Datum_narodenia')),
            'erp_predkontacia': str(row.get('Erp_predkontacia') or '').strip(), 'ostatne': str(row.get('Ostatne') or '').strip(),
        }
        return Dodavatel, query, defaults

    @staticmethod
    def parse_sukromne_osoby(row, index, logy):
        meno = str(row.get('Meno_a_priezvisko') or '').strip()
        email = str(row.get('email') or '').strip()
        if not meno: raise ValueError("Chýba Meno a priezvisko")
        
        query = Q(email=email) if email else Q(meno_a_priezvisko=meno)
        
        cerpacka_ico = str(row.get('Predvolena_cerpacka_ico') or '').strip()
        cerpacka = Dodavatel.objects.filter(ico=cerpacka_ico).first() if cerpacka_ico else None
        if cerpacka_ico and not cerpacka: logy.append(f"⚠️ Riadok {index}: Čerpačka s IČO {cerpacka_ico} nebola nájdená.")

        defaults = {
            'email': email, 'meno_a_priezvisko': meno, 'telefon': str(row.get('telefon') or '').strip(),
            'ulica_a_cislo': str(row.get('Ulica_a_cislo') or '').strip(), 'mesto': str(row.get('Mesto') or '').strip(),
            'psc': str(row.get('PSC') or '').strip(), 'stat': ISASImportStrategy._resolve_stat(row.get('Stat')),
            'iban': str(row.get('IBAN') or '').strip(), 'datum_narodenia': parse_date(row.get('Datum_narodenia')),
            'telegram_id': parse_bigint(row.get('Telegram_id')), 'discord_id': parse_bigint(row.get('Discord_id')),
            'predvolena_cerpacka': cerpacka,
        }
        return SukromnaOsoba, query, defaults

    @staticmethod
    def parse_zamestnanci(row, index, logy):
        meno = str(row.get('Meno') or '').strip()
        priezvisko = str(row.get('Priezvisko') or '').strip()
        email = str(row.get('email') or '').strip()
        if not meno or not priezvisko: raise ValueError("Chýba Meno alebo Priezvisko")
        
        query = Q(email=email) if email else Q(meno=meno, priezvisko=priezvisko)
        
        cerpacka_ico = str(row.get('Predvolena_cerpacka_ico') or '').strip()
        cerpacka = Dodavatel.objects.filter(ico=cerpacka_ico).first() if cerpacka_ico else None
        if cerpacka_ico and not cerpacka: logy.append(f"⚠️ Riadok {index}: Čerpačka s IČO {cerpacka_ico} nebola nájdená.")

        defaults = {
            'email': email, 'meno': meno, 'priezvisko': priezvisko, 'telefon': str(row.get('telefon') or '').strip(),
            'typ_pomeru': {'INTERNÝ': 'IN', 'EXTERNÝ': 'EX', 'BRIGÁDNIK': 'BR', 'KONATEĽ': 'KO'}.get(str(row.get('Typ_pomeru') or '').strip().upper(), 'IN'),
            'pracovna_rola': {'TECHNIK': 'TE', 'DISPEČER': 'DI', 'SKLADNÍK': 'SK', 'ADMINISTRATÍVA': 'AD'}.get(str(row.get('Pracovna_rola') or '').strip().upper(), 'TE'),
            'iban': str(row.get('IBAN') or '').strip(), 'ulica_a_cislo': str(row.get('Ulica_a_cislo') or '').strip(),
            'mesto': str(row.get('Mesto') or '').strip(), 'psc': str(row.get('PSC') or '').strip(),
            'stat': ISASImportStrategy._resolve_stat(row.get('Stat')),
            'datum_narodenia': parse_date(row.get('Datum_narodenia')), 'datum_nastupu': parse_date(row.get('Datum_nastupu')),
            'narok_na_dovolenku': parse_int(row.get('Narok_na_dovolenku'), 20), 'vycerpana_dovolenka': parse_float(row.get('Vycerpana_dovolenka'), 0.0),
            'sposob_odmenovania': {'HODINA': 'HODINA', 'MESIAC': 'MESIAC', 'ÚKOL': 'UKOL'}.get(str(row.get('Sposob_odmenovania') or '').strip().upper(), 'HODINA'),
            'mzda_brutto_hodina': parse_decimal(row.get('Mzda_brutto_hodina')), 'mzda_brutto_mesiac': parse_decimal(row.get('Mzda_brutto_mesiac')),
            'mzda_zivnostnik_hodina': parse_decimal(row.get('Mzda_zivnostnik_hodina')), 'zavazky': parse_decimal(row.get('Zavazky')),
            'poznamka_zavazky': str(row.get('Poznamka_zavazky') or '').strip(), 'cislo_op': str(row.get('Cislo_op') or '').strip(),
            'bozp_platne_do': parse_date(row.get('Bozp_platne_do')),
            'tg_upozornenia_ulohy': parse_bool(row.get('Tg_upozornenia_ulohy', 'ANO')),
            'mail_upozornenia_ulohy': parse_bool(row.get('Mail_upozornenia_ulohy', 'ANO')),
            'discord_upozornenia_ulohy': parse_bool(row.get('Discord_upozornenia_ulohy', 'ANO')),
            'zdiela_polohu': parse_bool(row.get('Zdiela_polohu', 'ANO')),
            'telegram_id': parse_bigint(row.get('Telegram_id')), 'discord_id': parse_bigint(row.get('Discord_id')),
            'predvolena_cerpacka': cerpacka,
            'zobrazit_na_webe': parse_bool(row.get('Zobrazit_na_webe', 'NIE')),
            'verejna_rola': str(row.get('Verejna_rola') or '').strip(),
        }
        return Zamestnanec, query, defaults

    @staticmethod
    def parse_vozidla(row, index, logy):
        spz = str(row.get('SPZ') or '').strip().replace(" ", "").upper()
        znacka = str(row.get('Znacka_a_model') or '').strip()
        if not spz or not znacka: raise ValueError("Chýba ŠPZ alebo Značka vozidla")
        
        query = Q(spz=spz)
        
        zam_email = str(row.get('Priradeny_zamestnanec_email') or '').strip()
        zamestnanec = Zamestnanec.objects.filter(email=zam_email).first() if zam_email else None
        if zam_email and not zamestnanec: logy.append(f"⚠️ Riadok {index}: Zamestnanec {zam_email} nebol nájdený.")
            
        so_email = str(row.get('Sukromna_osoba_email') or '').strip()
        sukromna_osoba = SukromnaOsoba.objects.filter(email=so_email).first() if so_email else None
        if so_email and not sukromna_osoba: logy.append(f"⚠️ Riadok {index}: Súkromná osoba {so_email} nebola nájdená.")

        defaults = {
            'spz': spz, 'znacka_a_model': znacka, 'typ_paliva': str(row.get('Typ_paliva') or 'DIESEL').strip().upper(),
            'aktualny_stav_km': parse_int(row.get('Aktualny_stav_km'), 0), 'je_firemne': parse_bool(row.get('Je_firemne')),
            'typ_vozidla_erp': {'OSOBNÉ': 'OSOBNE', 'NÁKLADNÉ': 'NAKLADNE'}.get(str(row.get('Typ_vozidla_erp') or '').strip().upper(), 'OSOBNE'),
            'erp_stredisko': str(row.get('Erp_stredisko') or '').strip(),
            'erp_kod_paliva': {'NAFTA': 'NM', 'BENZÍN 95': 'BA95', 'BENZÍN 98': 'BA98', 'LPG': 'LPG', 'ELEKTRINA': 'EL'}.get(str(row.get('Erp_kod_paliva') or '').strip().upper(), 'NM'),
            'vlastna_sadzba_za_km': parse_decimal(row.get('Vlastna_sadzba_za_km'), None) if row.get('Vlastna_sadzba_za_km') else None,
            'varovanie_max_litre': parse_int(row.get('Varovanie_max_litre'), None) if row.get('Varovanie_max_litre') else None,
            'varovanie_max_suma': parse_decimal(row.get('Varovanie_max_suma'), Decimal('0.00')),
            'aktualne_pneumatiky': {'LETNÉ': 'LETNE', 'ZIMNÉ': 'ZIMNE', 'CELOROČNÉ': 'CELOROCNE'}.get(str(row.get('Aktualne_pneumatiky') or '').strip().upper(), 'LETNE'),
            'servis_interval_km': parse_int(row.get('Servis_interval_km'), 30000), 'posledny_servis_km': parse_int(row.get('Posledny_servis_km'), 0),
            'olej_interval_km': parse_int(row.get('Olej_interval_km'), 15000), 'posledny_olej_km': parse_int(row.get('Posledny_olej_km'), 0),
            'cislo_palivovej_karty': str(row.get('Cislo_palivovej_karty') or '').strip(),
            'stk_plati_do': parse_date(row.get('Stk_plati_do')), 'dialnicna_znamka_do': parse_date(row.get('Dialnicna_znamka_do')),
            'pzp_plati_do': parse_date(row.get('Pzp_plati_do')), 'kasko_plati_do': parse_date(row.get('Kasko_plati_do')),
            'lekarnicka_plati_do': parse_date(row.get('Lekarnicka_plati_do')),
            'priradeny_zamestnanec': zamestnanec, 'sukromna_osoba': sukromna_osoba,
        }
        return Vozidlo, query, defaults

    @staticmethod
    def parse_typy_servisu(row, index, logy):
        nazov = str(row.get('Nazov') or '').strip()
        if not nazov: raise ValueError("Chýba Názov práce")
        
        query = Q(nazov=nazov)
        sposob_uctovania = str(row.get('Sposob_uctovania') or 'HODINA').strip().upper()
        
        defaults = {
            'nazov': nazov,
            'hodinova_sadzba_predaj': parse_decimal(row.get('Hodinova_sadzba_predaj')),
            'fixna_cena_predaj': parse_decimal(row.get('Fixna_cena_predaj')),
            'sposob_uctovania': sposob_uctovania if sposob_uctovania in ['HODINA', 'FIX'] else 'HODINA',
            'je_fakturovatelna': parse_bool(row.get('Je_fakturovatelna', 'ANO')),
            'normohodiny': parse_decimal(row.get('Normohodiny', '1.00')),
            'hodinova_sadzba_naklad': parse_decimal(row.get('Hodinova_sadzba_naklad')),
            'fixny_naklad': parse_decimal(row.get('Fixny_naklad')),
            'priorita': {'NÍZKA': 'NIZKA', 'STREDNÁ': 'STREDNA', 'VYSOKÁ': 'VYSOKA'}.get(str(row.get('Priorita') or '').strip().upper(), 'STREDNA'),
            'zaruka_mesiace': parse_int(row.get('Zaruka_mesiace'), 6),
            'pracovny_postup': str(row.get('Pracovny_postup') or '').strip(),
            'zverejnit_postup_na_webe': parse_bool(row.get('Zverejnit_postup_na_webe', 'NIE')),
            'zobrazit_na_webe': parse_bool(row.get('Zobrazit_na_webe', 'NIE')),
            'popis_na_webe': str(row.get('Popis_na_webe') or '').strip() or None,
        }
        return TypServisuPrace, query, defaults

    @staticmethod
    def parse_vyrobcovia(row, index, logy):
        nazov = str(row.get('Nazov_vyrobcu') or '').strip()
        if not nazov: raise ValueError("Chýba Názov výrobcu")
        
        query = Q(nazov_vyrobcu=nazov)
        
        defaults = {
            'nazov_vyrobcu': nazov,
            'domena': str(row.get('Domena_webu') or '').strip(), 'krajina_povodu': str(row.get('Krajina_povodu') or '').strip(),
            'typ_vyroby': {'ORIGINÁL': 'O', 'NÁHRADA': 'N', 'ZMIEŠANÉ': 'M'}.get(str(row.get('Typ_vyroby') or '').strip().upper(), 'M'),
            'katalog_dielov_url': str(row.get('Katalog_dielov_url') or '').strip(), 'technicka_poznamka': str(row.get('Technicka_poznamka') or '').strip(),
            'zobrazit_na_webe': parse_bool(row.get('Zobrazit_na_webe', 'NIE')),
        }
        return DatabazaVyrobcov, query, defaults

    @staticmethod
    def parse_filialky(row, index, logy):
        nazov = str(row.get('Nazov') or '').strip()
        kod = str(row.get('Kod_pobocky') or '').strip()
        if not nazov: raise ValueError("Chýba Názov filiálky")
        
        query = Q(kod_pobocky=kod) if kod else Q(nazov=nazov)
        
        zam_email = str(row.get('Zodpovedny_zamestnanec_email') or '').strip()
        zamestnanec = Zamestnanec.objects.filter(email=zam_email).first() if zam_email else None
        if zam_email and not zamestnanec: logy.append(f"⚠️ Riadok {index}: Zamestnanec {zam_email} nebol nájdený.")

        defaults = {
            'kod_pobocky': kod, 'nazov': nazov,
            'typ_skladu': {'BUDOVA': 'BUDOVA', 'AUTO': 'AUTO', 'KONTAJNER': 'KONTAJNER', 'INÉ': 'INE'}.get(str(row.get('Typ_skladu') or '').strip().upper(), 'BUDOVA'),
            'adresa': str(row.get('Adresa') or '').strip(), 'veduci_skladu': str(row.get('Veduci_skladu') or '').strip(),
            'telefon': str(row.get('telefon') or '').strip(), 'email': str(row.get('email') or '').strip(),
            'je_aktivna': parse_bool(row.get('Je_aktivna', 'ANO')), 'poznamka': str(row.get('Poznamka') or '').strip(),
            'zodpovedny_zamestnanec': zamestnanec,
        }
        return Filialka, query, defaults

    @staticmethod
    def parse_servisovane_zariadenia(row, index, logy):
        vyrobne_cislo = str(row.get('Vyrobne_cislo') or '').strip()
        zakaznik_ico = str(row.get('Zakaznik_ico') or '').strip()
        vyrobca_nazov = str(row.get('Vyrobca_nazov') or '').strip()
        
        if not vyrobne_cislo: raise ValueError("Chýba Výrobné číslo")
        if not zakaznik_ico: raise ValueError("Chýba IČO zákazníka")

        zakaznik = Zakaznik.objects.filter(ico=zakaznik_ico).first()
        if not zakaznik: raise ValueError(f"Zákazník s IČO {zakaznik_ico} neexistuje.")

        vyrobca = None
        if vyrobca_nazov:
            vyrobca = DatabazaVyrobcov.objects.filter(nazov_vyrobcu__iexact=vyrobca_nazov).first()
            if not vyrobca: 
                vyrobca = DatabazaVyrobcov.objects.create(nazov_vyrobcu=vyrobca_nazov)

        query = Q(vyrobne_cislo=vyrobne_cislo)
        
        kategorie_dict = dict(ServisovaneZariadenie.KATEGORIE)
        default_kat = next(iter(kategorie_dict.keys()), '')
        raw_kategoria = str(row.get('Kategoria') or '').strip().lower()

        if raw_kategoria:
            if raw_kategoria not in kategorie_dict:
                raise ValueError(f"Neplatná kategória zariadenia: '{raw_kategoria}'. Povolené možnosti v aktívnom odvetví: {', '.join(kategorie_dict.keys())}.")
            kategoria = raw_kategoria
        else:
            kategoria = default_kat

        # 🛡️ ISAS ARCHITECT: 100% symetrická normalizácia (odstraňuje diakritiku, podčiarkovníky aj interpunkciu)
        def _norm_term(val):
            if not val: return ''
            s = unicodedata.normalize('NFKD', str(val)).encode('ASCII', 'ignore').decode('utf-8')
            return re.sub(r'[^a-zA-Z0-9]', '', s).lower()

        r_norm = {_norm_term(k): v for k, v in row.items() if k}
        m_slug = _norm_term(get_industry_term('meter_label_short') or get_industry_term('meter_label'))
        l_slug = _norm_term(get_industry_term('meter_load_label'))

        val_uptime = (r_norm.get(m_slug) if m_slug else None) or r_norm.get('aktualnemotohodiny') or r_norm.get('uptime')
        val_peak = (r_norm.get(l_slug) if l_slug else None) or r_norm.get('motohodinyvzatazi') or r_norm.get('casvspicke') or r_norm.get('peak')

        defaults = {
            'vyrobne_cislo': vyrobne_cislo, 'zakaznik': zakaznik, 'vyrobca': vyrobca,
            'kategoria': kategoria, 'model_zariadenia': str(row.get('Model_zariadenia') or '').strip(),
            'nazov': str(row.get('Nazov') or '').strip(), 'rok_vyroby': parse_int(row.get('Rok_vyroby'), None) if row.get('Rok_vyroby') else None,
            'datum_instalacie': parse_date(row.get('Datum_instalacie')), 
            'zaruka_do': parse_date(row.get('Zaruka_do')),
            'predlzena_zaruka_do': parse_date(row.get('Predlzena_zaruka_do')),
            'hmotnost_kg': parse_int(row.get('Hmotnost_kg'), None) if row.get('Hmotnost_kg') else None,
            'umiestnenie': str(row.get('Umiestnenie') or '').strip(),
            'aktualne_motohodiny': parse_int(val_uptime, 0),
            'motohodiny_v_zatazi': parse_int(val_peak, 0),
            'iot_mac_adresa': str(row.get('Iot_mac_adresa') or '').strip(),
            'iot_ip_adresa': str(row.get('Iot_ip_adresa') or '').strip() or None, 'iot_protokol': str(row.get('Iot_protokol') or '').strip(),
            'iot_je_aktivny': parse_bool(row.get('Iot_je_aktivny')), 'iot_limit_zapisov_za_hodinu': parse_int(row.get('Iot_limit_zapisov_za_hodinu'), 60),
            'v_prenajme_od_isas': parse_bool(row.get('V_prenajme_od_isas')),
            'je_urcene_na_prenajom': parse_bool(row.get('Je_urcene_na_prenajom', 'NIE')),
            'cena_prenajmu_na_den': parse_decimal(row.get('Cena_prenajmu_na_den'), Decimal('0.00')),
            'zobrazit_na_webe_prenajom': parse_bool(row.get('Zobrazit_na_webe_prenajom', 'NIE')),
            'v_servisnom_rezime': parse_bool(row.get('V_servisnom_rezime')),
            'poznamka': str(row.get('Poznamka') or '').strip(),
        }

        return ServisovaneZariadenie, query, defaults

    @staticmethod
    def parse_sklad_karty(row, index, logy):
        hlavna_kat = str(row.get('Hlavna_kategoria') or '').strip()
        podkat = str(row.get('Podkategoria') or '').strip()
        seriove_cislo = str(row.get('Seriove_cislo') or '').strip()
        
        if not hlavna_kat or not podkat or not seriove_cislo:
            raise ValueError("Chýba Hlavná kategória, Podkategória alebo Sériové číslo.")
        cat_map_ci = {k.strip().lower(): (k, {sk.strip().lower(): (sk, model) for sk, model in sub.items()}) for k, sub in CATEGORY_MAP.items()}
        h_norm, p_norm = hlavna_kat.lower(), podkat.lower()

        if h_norm not in cat_map_ci or p_norm not in cat_map_ci[h_norm][1]:
            raise ValueError(f"Neplatná kombinácia kategórií: '{hlavna_kat}' -> '{podkat}'. Povolené kategórie: {', '.join(CATEGORY_MAP.keys())}.")

        canon_hlavna, canon_pod = cat_map_ci[h_norm][0], cat_map_ci[h_norm][1][p_norm][0]
        ModelTrieda = cat_map_ci[h_norm][1][p_norm][1]
        hlavna_kat = canon_hlavna
        hlavny_sklad = Sklad.objects.first()
        if not hlavny_sklad:
            hlavny_sklad = Sklad.objects.create(nazov='Hlavný sklad')

        # 🚀 ISAS ARCHITECT: 100% dynamické prepojenie na sekciu skladu bez hardkódu modelov a polí
        parent_kwargs = {}
        ParentModel = PARENT_MODELS.get(hlavna_kat)
        if ParentModel:
            # 🛡️ DEFENSIVE: filter().first() zabráni pádu na MultipleObjectsReturned
            sekcia = ParentModel.objects.filter(sklad=hlavny_sklad).first()
            if not sekcia:
                sekcia = ParentModel.objects.create(sklad=hlavny_sklad)

            for f in ModelTrieda._meta.fields:
                if f.is_relation and f.related_model == ParentModel:
                    parent_kwargs[f.name] = sekcia
                    break

        dodavatel_ico = str(row.get('Dodavatel_ico') or '').strip()
        dodavatel = Dodavatel.objects.filter(ico=dodavatel_ico).first() if dodavatel_ico else None
        if dodavatel_ico and not dodavatel: logy.append(f"⚠️ Riadok {index}: Dodávateľ s IČO {dodavatel_ico} nebol nájdený.")

        vyrobca_nazov = str(row.get('Vyrobca_nazov') or '').strip()
        vyrobca = None
        if vyrobca_nazov:
            vyrobca = DatabazaVyrobcov.objects.filter(nazov_vyrobcu__iexact=vyrobca_nazov).first()
            if not vyrobca: 
                vyrobca = DatabazaVyrobcov.objects.create(nazov_vyrobcu=vyrobca_nazov)

        query = Q(seriove_cislo=seriove_cislo)
        kat_cislo = str(row.get('Katalogove_cislo') or '').strip()

        global_nastavenia_model = apps.get_model('ISASaplikacia', 'GlobalneNastavenia')
        raw_dph = global_nastavenia_model.get_zakladna_dph() if (global_nastavenia_model and hasattr(global_nastavenia_model, 'get_zakladna_dph')) else None
        zakladna_dph = raw_dph if (raw_dph is not None and raw_dph > 0) else Decimal('0.00')

        defaults = {
            'seriove_cislo': seriove_cislo, 'katalogove_cislo': kat_cislo if kat_cislo else None,
            'ean_kod': str(row.get('Ean_kod') or '').strip(), 'obchodny_nazov': str(row.get('Obchodny_nazov') or '').strip(),
            'skryt_vyrobcu': parse_bool(row.get('Skryt_vyrobcu')), 'kto_som': str(row.get('Nazov_kto_som') or '').strip(),
            'pocet_na_sklade': parse_int(row.get('Pocet_na_sklade'), 0), 'merna_jednotka': str(row.get('Merna_jednotka') or 'ks').strip(),
            'typ_zasoby_erp': str(row.get('Typ_zasoby_erp') or 'TOVAR').strip().upper(), 'sadzba_dph': parse_decimal(row.get('Sadzba_dph'), zakladna_dph),
            'dodavatel': dodavatel, 'vyrobca': vyrobca,
            'zobrazit_na_webe': parse_bool(row.get('Zobrazit_na_webe', 'NIE')),
            'popis_na_webe': str(row.get('Popis_na_webe') or '').strip() or None,
        }

        defaults.update(parent_kwargs)
        
        if hasattr(ModelTrieda, 'typ'):
            defaults['typ'] = {'ORIGINÁL': 'O', 'NÁHRADA': 'N'}.get(str(row.get('Typ_dielu_ON') or '').strip().upper(), None)

        return ModelTrieda, query, defaults

    @classmethod
    def get_strategy(cls, typ_importu):
        mapovanie = {
            'ZAKAZNICI': cls.parse_zakaznici, 'DODAVATELIA': cls.parse_dodavatelia,
            'SUKROMNE_OSOBY': cls.parse_sukromne_osoby, 'ZAMESTNANCI': cls.parse_zamestnanci,
            'VOZIDLA': cls.parse_vozidla, 'TYPY_SERVISU': cls.parse_typy_servisu,
            'VYROBCOVIA': cls.parse_vyrobcovia, 'FILIALKY': cls.parse_filialky,
            'SERVISOVANE_ZARIADENIA': cls.parse_servisovane_zariadenia, 'SKLAD_KARTY': cls.parse_sklad_karty,
        }
        return mapovanie.get(typ_importu)

class ISASItemMatcher:
    """
    ISAS CORE: Enterprise Waterfall Matching Engine.
    Zapuzdruje komplexnú logiku párovania položiek z faktúr/XML na reálne skladové karty.
    Využíva In-Memory caching pre minimalizáciu databázových dopytov.
    """
    def __init__(self):
        self.vsetky_kody_db = {}
        self._load_codes()

    def _load_codes(self):
        """Prednačíta všetky kódy do pamäte pre bleskové porovnávanie."""
        for main_cat, subcats in CATEGORY_MAP.items():
            for subcat_name, model_class in subcats.items():
                dostupne_polia = {f.name for f in model_class._meta.fields}
                if 'seriove_cislo' in dostupne_polia:
                    kody = model_class.objects.exclude(seriove_cislo__isnull=True).exclude(seriove_cislo="").values_list('id', 'seriove_cislo')
                    self.vsetky_kody_db[model_class] = [(db_id, str(db_cislo).replace(" ", "").lower()) for db_id, db_cislo in kody]
                else:
                    self.vsetky_kody_db[model_class] = []

    def find_match(self, ean, zobrazeny_kod):
        """
        Vykoná 4-stupňové kaskádové vyhľadávanie (Waterfall).
        Vracia: (najdena_polozka, skutocna_kategoria, content_type_id, object_id)
        """
        kod_str = str(zobrazeny_kod or '').strip()
        if not kod_str or len(kod_str) <= 1:
            return None, None, None, None

        ident = kod_str.lower()
        cisty_hladany = ident.replace(" ", "").replace("-", "").replace("/", "").replace(".", "")
        tokeny_hladane = set([t for t in re.split(r'\W+', ident) if len(t) > 2])

        najdena_polozka = None
        skutocna_kategoria = None

        for main_cat, subcats in CATEGORY_MAP.items():
            if najdena_polozka: break
            for subcat_name, model_class in subcats.items():
                if najdena_polozka: break
                
                # SITO 1: EAN Kód (100% istota)
                ean_cisty = str(ean).strip() if ean is not None else ""
                if ean_cisty and hasattr(model_class, 'ean_kod'):
                    zhoda = model_class.objects.filter(ean_kod=ean_cisty).first()
                    if zhoda: najdena_polozka = zhoda; break
                
                # SITO 2: Presná zhoda (Sériové alebo Katalógové) defenzívne ošetrená voči absencii polí
                zhoda = None
                if hasattr(model_class, 'seriove_cislo'):
                    zhoda = model_class.objects.filter(seriove_cislo__iexact=zobrazeny_kod).first()
                if not zhoda and hasattr(model_class, 'katalogove_cislo'):
                    zhoda = model_class.objects.filter(katalogove_cislo__iexact=zobrazeny_kod).first()
                if zhoda: najdena_polozka = zhoda; break
                    
                # SITO 3: Normalizovaná zhoda (bez medzier a znakov)
                if len(cisty_hladany) >= 5:
                    for db_id, cisty_db in self.vsetky_kody_db.get(model_class, []):
                        cisty_db_norm = cisty_db.replace("-", "").replace("/", "").replace(".", "")
                        if cisty_hladany == cisty_db_norm:
                            najdena_polozka = model_class.objects.filter(id=db_id).first()
                            if najdena_polozka: break
                    if najdena_polozka:
                        break
                    
                # SITO 4: Tokenizovaná zhoda (Ťažká váha)
                if tokeny_hladane:
                    for db_id, cisty_db in self.vsetky_kody_db.get(model_class, []):
                        tokeny_db = set([t for t in re.split(r'\W+', cisty_db) if len(t) > 2])
                        if tokeny_hladane and tokeny_hladane.issubset(tokeny_db):
                            najdena_polozka = model_class.objects.filter(id=db_id).first()
                            if najdena_polozka: 
                                break
                    if najdena_polozka: 
                        break

            if najdena_polozka:
                skutocna_kategoria = model_class.__name__

        ct_id = None
        obj_id = None
        if najdena_polozka:
            ct_id = ContentType.objects.get_for_model(najdena_polozka).id
            obj_id = najdena_polozka.id

        return najdena_polozka, skutocna_kategoria, ct_id, obj_id

class ISASProductMetadataResolver:
    """
    ISAS CORE: Enterprise Product Metadata Resolver.
    Centralizuje logiku pre získavanie kategórie a výrobcu z položiek dokladov (Faktúry, Ponuky).
    Podporuje priame GFK väzby aj fallback textové vyhľadávanie.
    """
    @staticmethod
    def resolve(polozka):
        kategoria_nazov = "Materiál / Diel"
        vyrobca_nazov = ""

        # Hľadáme len pre materiály, nie pre prácu, dopravu, zľavy alebo iné služby
        if getattr(polozka, 'typ_polozky', None) in ['PRACA', 'DOPRAVA', 'ZLA', 'INE']:
            return kategoria_nazov, vyrobca_nazov

        # 🛡️ 1. PRIORITA: Priame prepojenie cez Skladový diel (GenericForeignKey)
        if getattr(polozka, 'content_type_id', None) and getattr(polozka, 'object_id', None):
            try:
                obj = getattr(polozka, 'skladovy_diel', None) or getattr(polozka, 'content_object', None)
            except ObjectDoesNotExist:
                obj = None
            if obj:
                kategoria_nazov = str(obj._meta.verbose_name).upper()
                
                try:
                    vyrobca_obj = getattr(obj, 'vyrobca', None)
                    if not vyrobca_obj and hasattr(obj, 'vyrobca_dielu'):
                        vyrobca_obj = getattr(obj, 'vyrobca_dielu', None)
                except ObjectDoesNotExist:
                    vyrobca_obj = None
                    
                if vyrobca_obj:
                    vyrobca_nazov = getattr(vyrobca_obj, 'nazov_vyrobcu', str(vyrobca_obj)).strip()
                else:
                    # Fallback cez meta polia modelu pre iné názvy relácií
                    for field in obj._meta.fields:
                        if field.is_relation and field.related_model and field.related_model.__name__ in ['DatabazaVyrobcov', 'Vyrobca']:
                            try:
                                val = getattr(obj, field.name, None)
                            except ObjectDoesNotExist:
                                val = None
                            if val:
                                vyrobca_nazov = getattr(val, 'nazov_vyrobcu', str(val)).strip()
                                break
                                
                if not vyrobca_nazov:
                    for kandidat in ['vyrobca_dielu', 'vyrobca', 'znacka', 'brand']:
                        if hasattr(obj, kandidat):
                            try:
                                val = getattr(obj, kandidat, None)
                            except ObjectDoesNotExist:
                                val = None
                            if val:
                                vyrobca_nazov = getattr(val, 'nazov_vyrobcu', str(val)).strip()
                                break

        # 🛡️ 2. PRIORITA: Ak bol diel dopísaný ručne textom (nemá väzbu na sklad), skúsime textové vyhľadávanie
        if not vyrobca_nazov:
            cisty_kod = ocisti_nazov_od_jednotky(str(getattr(polozka, 'nazov', '') or '').strip())
            found = False
            for cat_group in CATEGORY_MAP.values():
                if found: break
                for model_class in cat_group.values():
                    if cisty_kod:
                        dostp_stlpce = [f.name for f in model_class._meta.fields]
                        vyhladavacie_kritéria = Q()
                        for stlpec in ['seriove_cislo', 'nazov', 'nazov_polozky', 'model_zariadenia', 'katalogove_cislo', 'sku']:
                            if stlpec in dostp_stlpce:
                                vyhladavacie_kritéria |= Q(**{f"{stlpec}__iexact": cisty_kod})
                        
                        najdeny_objekt = model_class.objects.filter(vyhladavacie_kritéria).first() if vyhladavacie_kritéria else None
                        if najdeny_objekt:
                            kategoria_nazov = getattr(model_class._meta, 'verbose_name', 'Materiál / Diel')
                            for field in najdeny_objekt._meta.fields:
                                if field.is_relation and field.related_model and field.related_model.__name__ == 'DatabazaVyrobcov':
                                    val = getattr(najdeny_objekt, field.name)
                                    if val:
                                        vyrobca_nazov = getattr(val, 'nazov_vyrobcu', str(val)).strip()
                                        break
                            if not vyrobca_nazov:
                                for kandidat in ['vyrobca_dielu', 'vyrobca', 'znacka', 'brand']:
                                    if kandidat in dostp_stlpce:
                                        val = getattr(najdeny_objekt, kandidat)
                                        if val:
                                            vyrobca_nazov = str(val).strip()
                                            break
                            found = True
                            break

        return kategoria_nazov, vyrobca_nazov