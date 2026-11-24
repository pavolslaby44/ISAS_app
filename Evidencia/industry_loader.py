"""
Evidencia/industry_loader.py

Architektonický modul pre dynamické načítanie odvetvovej konfigurácie (Single-Tenant).
Zabezpečuje úplnú izoláciu univerzálneho ERP jadra od doménovej logiky odvetvia.
"""
import importlib
import logging
import re
from datetime import date
from typing import Optional, Tuple, Dict, Any, List
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)


def _load_industry_config():
    """
    Dynamicky načíta konfiguračný modul aktívneho odvetvia podľa settings.INDUSTRY_MODULE.
    
    DEFENZÍVNE OŠETRENIE:
    - Ak odvetvie nie je nastavené (None alebo prázdny reťazec), vracia None (jadro funguje neutrálne).
    - Ak je modul zadaný, ale fyzicky neexistuje balík odvetvia -> vyvolá ImproperlyConfigured.
    - Ak modul existuje, ale zlyhal na vnútornom importe / syntaxi -> vyvolá výnimku s presnou chybou.
    """
    raw_name = getattr(settings, 'INDUSTRY_MODULE', None)
    if not raw_name:
        logger.info(
            "ISAS ARCHITECT: Premenná INDUSTRY_MODULE nie je definovaná. "
            "ERP beží v neutrálnom režime bez špecifického odvetvia."
        )
        return None

    industry_module_name = str(raw_name).strip().lower()
    if not industry_module_name:
        return None

    module_path = f"odvetvia.{industry_module_name}.config"
    try:
        return importlib.import_module(module_path)
    except ModuleNotFoundError as e:
        # Overenie, či chýba samotný odvetvový modul alebo niečo vnútri neho
        if e.name == module_path or e.name == f"odvetvia.{industry_module_name}":
            msg = (
                f"Kritická chyba konfigurácie: INDUSTRY_MODULE je nastavený na '{industry_module_name}', "
                f"ale modul '{module_path}' neexistuje v projekte!"
            )
            logger.critical(f"ISAS ARCHITECT: {msg}")
            raise ImproperlyConfigured(msg) from e
        # Ak chyba nastala vo vnútri modulu (napr. import vnútri config.py)
        logger.exception(f"ISAS ARCHITECT: Chyba importu vnútri odvetvového modulu '{module_path}': {e}")
        raise
    except Exception as e:
        logger.exception(f"ISAS ARCHITECT: Fatálna chyba pri načítavaní odvetvového modulu '{module_path}': {e}")
        raise


# ------------------------------------------------------------------------------
# 🛡️ LAZY SINGLETON PRE COMPLIANCE (Prevencia AppRegistryNotReady a edge cases)
# ------------------------------------------------------------------------------
_compliance_instance = None
_compliance_loaded = False


def _load_industry_compliance():
    """
    Dynamicky a bezpečne načíta compliance modul aktívneho odvetvia.
    Ošetruje sanitizáciu vstupu, absenciu súboru aj chyby vnútri modulu.
    """
    raw_name = getattr(settings, 'INDUSTRY_MODULE', None)
    if not raw_name:
        return None

    # 🛡️ DEFENSIVE: Sanitizácia stringu z .env proti medzerám a veľkým písmenám
    industry_module_name = str(raw_name).strip().lower()
    if not industry_module_name:
        return None

    module_path = f"odvetvia.{industry_module_name}.compliance"
    try:
        return importlib.import_module(module_path)
    except ModuleNotFoundError as e:
        # Ak chýba samotný súbor compliance.py v odvetví, ERP beží neutrálne bez revízií
        if e.name == module_path or e.name == f"odvetvia.{industry_module_name}":
            logger.info(f"ISAS ARCHITECT: Odvetvie '{industry_module_name}' nedefinuje compliance modul.")
            return None
        # Ak zlyhal import vnútri compliance.py (napr. preklep v závislosti), zalogujeme a vyvoláme
        logger.exception(f"ISAS ARCHITECT: Chyba importu vnútri compliance modulu '{module_path}': {e}")
        raise
    except Exception as e:
        logger.exception(f"ISAS ARCHITECT: Fatálna chyba pri načítavaní compliance modulu '{module_path}': {e}")
        raise


def get_industry_compliance():
    """
    🚀 ISAS ARCHITECT: Lazy getter s ochranou pred predčasným načítaním modelov.
    Zaručuje, že moduly odvetvia sa importujú až po inicializácii Django aplikácií.
    """
    global _compliance_instance, _compliance_loaded
    if not _compliance_loaded:
        _compliance_instance = _load_industry_compliance()
        _compliance_loaded = True
    return _compliance_instance


def _get_validated_compliance_fieldset(config_obj) -> Optional[tuple]:
    """🛡️ DEFENSIVE: Validuje formát Django Admin fieldsetu (musí byť tuple/list o dĺžke 2)."""
    fieldset = getattr(config_obj, 'COMPLIANCE_ADMIN_FIELDSET', None)
    if not fieldset:
        return None
    if isinstance(fieldset, (tuple, list)) and len(fieldset) == 2 and isinstance(fieldset[1], dict):
        return tuple(fieldset)
    logger.warning("ISAS ARCHITECT: COMPLIANCE_ADMIN_FIELDSET v odvetví má neplatný formát. Očakáva sa (názov, dict).")
    return None


# Načítanie základnej konfigurácie odvetvia
_config = _load_industry_config()

# ------------------------------------------------------------------------------
# 🔄 SPÄTNÁ KOMPATIBILITA PRE FORMS.PY, APPS.PY A ADMIN.PY
# Všetky atribúty majú bezpečný fallback na prázdne štruktúry zodpovedajúceho typu
# ------------------------------------------------------------------------------
PARENT_MODELS: dict = getattr(_config, 'PARENT_MODELS', {}) if _config else {}
ZARIADENIA_MAP: dict = getattr(_config, 'ZARIADENIA_MAP', {}) if _config else {}
ZARIADENIA_CHOICES: list = getattr(_config, 'ZARIADENIA_CHOICES', None) or [('stroj', '⚙️ Stroj / Zariadenie')]
DEFAULT_CERTIFIKAT_CHOICES: list = [
    ('BOZP_PO', '🦺 BOZP, PO a Prvá pomoc'),
    ('VODIC_REFERENT', '🚗 Školenie vodičov referentských vozidiel (Zákon 124/2006 Z.z.)'),
    ('ELEKTRO', '⚡ Elektrotechnická spôsobilosť (§20 - §24)'),
    ('VYSKY_POSTROJ', '🧗 Práce vo výškach a nad voľnou hĺbkou (OOPP postroje)'),
    ('STROJE_VZV', '🚜 VZV a mobilné zdvíhacie pracovné plošiny'),
    ('AUDIT_ISO', '📋 Manažérstvo kvality a audity (ISO 9001 / ISO normy)'),
    ('PRIVACY_GDPR', '⚖️ Ochrana osobných údajov (GDPR) a informačná bezpečnosť'),
    ('INE', '📜 Iné odborné osvedčenie'),
]
CERTIFIKAT_CHOICES: list = getattr(_config, 'CERTIFIKAT_CHOICES', None) or DEFAULT_CERTIFIKAT_CHOICES
CATEGORY_MAP: dict = getattr(_config, 'CATEGORY_MAP', {}) if _config else {}
CATEGORY_CHOICES: list = getattr(_config, 'CATEGORY_CHOICES', []) if _config else []
FLAT_CATEGORY_MAP: dict = getattr(_config, 'FLAT_CATEGORY_MAP', {}) if _config else {}
COMPLIANCE_ADMIN_FIELDSET: Optional[tuple] = _get_validated_compliance_fieldset(_config)
COMPLIANCE_ADMIN_MEDIA: Optional[dict] = getattr(_config, 'COMPLIANCE_ADMIN_MEDIA', None) if _config else None
INDUSTRY_TERMS: dict = getattr(_config, 'INDUSTRY_TERMS', {}) if _config else {}
COMPLIANCE_ALARM_PREFIXES: tuple = tuple(getattr(_config, 'COMPLIANCE_ALARM_PREFIXES', ())) if _config else ()
ZARIADENIA_COLOR_MAP: dict = getattr(_config, 'ZARIADENIA_COLOR_MAP', {}) if _config else {}
SAMPLE_IMPORT_DATA: dict = getattr(_config, 'SAMPLE_IMPORT_DATA', {}) if _config else {}


def is_esg_audit_supported() -> bool:
    """
    🚀 ISAS ARCHITECT: Defenzívne overí, či aktívne odvetvie podporuje energetický ESG audit.
    Ošetruje bool, stringové ekvivalenty ('False', '0'), chýbajúci config aj neutrálny režim ERP.
    """
    if not _config:
        return False
    val = getattr(_config, 'HAS_ENERGY_ESG_AUDIT', False)
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val > 0
    if isinstance(val, str):
        return val.strip().lower() in ('true', '1', 'yes', 't', 'on', 'enabled')
    return False


# 🔄 Modulová konštanta so 100% garanciou konzistencie (Single Source of Truth)
HAS_ENERGY_ESG_AUDIT: bool = is_esg_audit_supported()
HAS_FPLYNY_AUDIT: bool = bool(getattr(_config, 'HAS_FPLYNY_AUDIT', False)) if _config else False
HAS_HEAT_RECOVERY: bool = bool(getattr(_config, 'HAS_HEAT_RECOVERY', False)) if _config else False
HAS_VTZ_INSPECTION: bool = bool(getattr(_config, 'HAS_VTZ_INSPECTION', False)) if _config else False
HAS_VIBRO_DIAGNOSTICS: bool = bool(getattr(_config, 'HAS_VIBRO_DIAGNOSTICS', False)) if _config else False
HAS_CAPEX_CALCULATOR: bool = bool(getattr(_config, 'HAS_CAPEX_CALCULATOR', False)) if _config else False
FPLYNY_CHLADIVA: list = getattr(_config, 'FPLYNY_CHLADIVA', []) if _config else []
HUB_CONFIG: dict = getattr(_config, 'HUB_CONFIG', {}) if _config else {}
TCO_PROFILES: dict = getattr(_config, 'TCO_PROFILES', {}) if _config else {}
MEDIA_QUALITY_APPS: list = getattr(_config, 'MEDIA_QUALITY_APPS', []) if _config else []
EASTER_EGG_TILES: dict = getattr(_config, 'EASTER_EGG_TILES', {}) if _config else {}


def get_easter_egg_tiles() -> Dict[int, str]:
    """
    Vráti mapu úrovní dlaždíc pre minihru ISAS 2048.
    Ak aktívne odvetvie nedefinuje vlastné označenia, vracia neutrálne univerzálne položky a komponenty.
    """
    generic_defaults = {
        2: "🔩 Spojovací materiál",
        4: "⭕ Tesniaci prvok",
        8: "🧰 Základný diel",
        16: "🔌 Regulačný prvok",
        32: "⚡ Merací snímač",
        64: "🎛️ Riadiaci modul",
        128: "⚙️ Hlavný agregát",
        256: "🏢 Technologický uzol",
        512: "📋 Servisný kontrakt",
        1024: "💼 Komplexný projekt",
        2048: "🏆 Kľúčový partner",
        4096: "👑 Strategický líder",
        8192: "🚀 Globálny etalón",
    }
    industry_tiles = getattr(_config, 'EASTER_EGG_TILES', {}) if _config else {}
    if isinstance(industry_tiles, dict) and industry_tiles:
        cleaned = {}
        for k, v in industry_tiles.items():
            try:
                cleaned[int(k)] = str(v)
            except (ValueError, TypeError):
                continue
        if cleaned:
            return cleaned
    return generic_defaults


def get_default_stock_model_name() -> str:
    """
    🚀 ISAS ARCHITECT: Vráti názov kmeňového modelu pre nezaradené položky skladu.
    Hodnotu číta priamo z aktívneho odvetvia. Ak odvetvie model nedefinuje, vracia neutrálne 'Sluzba'.
    """
    if _config and hasattr(_config, 'DEFAULT_STOCK_MODEL'):
        return str(_config.DEFAULT_STOCK_MODEL)
    if FLAT_CATEGORY_MAP:
        return next(iter(FLAT_CATEGORY_MAP.keys()))
    return "Sluzba"


def get_industry_term(term_key: str, default: str = "") -> str:
    """
    🚀 ISAS ARCHITECT: Vráti sémantický pojem pre aktívne odvetvie (napr. 'Motohodiny', 'Počet jázd', 'Cykly').
    Ak kľúč neexistuje alebo odvetvie nie je zadané, bezpečne vráti definovaný fallback default.
    """
    if not _config:
        return default
    terms = getattr(_config, 'INDUSTRY_TERMS', {})
    if isinstance(terms, dict) and term_key in terms:
        return str(terms[term_key])
    return default


def get_all_industry_terms(extra_overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    🚀 ISAS ARCHITECT: Zjednotený zdroj pravdy pre sémantiku UI.
    Vracia kompletný odvetvový slovník spojený s neutrálnymi systémovými fallbackmi ERP jadra.
    """
    core_defaults = {
        'meter_unit': 'hod.',
        'meter_load_unit': 'hod.',
        'stanoviste': 'Pozícia / Stanovište',
        'hmotnost': 'Hmotnosť',
        'hmotnost_jednotka': 'kg',
        'rok_vyroby': 'Rok výroby',
        'zaruka': 'Záruka',
        'umiestnenie': 'Umiestnenie',
        'zariadenie_genitiv': 'zariadenia / položky',
        'zariadenia_mnozno': 'zariadenia a technologické celky',
        'zariadenia_mnozno_zapor': 'zariadenia, technologické celky ani položky',
        'zataz_pomer_label': 'Podiel prevádzky v záťaži',
        'pas_title': 'Digitálny pas',
        'servisny_rezim_title': 'Položka je v servisnom režime údržby',
        'poziadavka_placeholder': 'Popíšte zistené príznaky, chybové hlásenie alebo rozsah prác...',
        'stat_zariadenia_label': 'Servisovaných zariadení',
        'pocet_zariadeni_label': 'Počet zariadení',
        'typ_zariadeni_label': 'Typ zariadení',
        'sla_placeholder': 'Napr. Výrobné linky, technologické celky, stroje',
        'rental_title': 'Požičovňa a náhradné zariadenia',
        'rental_fallback_icon': '⚙️',
        'rental_detail_label': 'Detail zariadenia',
        'rental_catalog_btn': 'Zobraziť celú ponuku',
        'buyback_title': 'Odkúpime vaše použité zariadenie',
        'buyback_device_placeholder': 'Značka a model zariadenia *',
        'buyback_photo_label': '📸 Fotografia zariadenia / štítku',
        'hub_tab_esg': '⚡ Úspora energie & ESG',
        'hub_tab_law': '⚖️ Zákony & Revízie',
        'hub_tab_diag': '🔬 Diagnostika & Dimenzovanie',
        'esg_idle_title': 'Chod bez záťaže / neproduktívny čas & CO₂',
        'esg_audit_stroj_label': 'Príkon zariadenia',
        'esg_power_unit': 'kW',
        'esg_auditor_title': 'ESG & Energetický špecialista',
        'esg_audit_podiel_label': 'Chod bez záťaže / naprázdno (%)',
        'esg_energy_price_label': 'Cena energie',
        'kpi_strata_label': 'Odhadovaná ročná finančná strata',
        'rekuperacia_title': 'Rekuperácia odpadového tepla',
        'rekuperacia_fuel_label': 'Cena nahrádzanej energie',
        'tco_title': 'TCO kalkulátor nákladov',
        'tco_desc': 'Rozpad celkových nákladov na vlastníctvo podľa profilu prevádzky a technológie.',
        'capex_title': 'CAPEX vs OPEX (Nákup vs. Prenájom)',
        'capex_desc': 'Porovnanie nákupu zariadenia vs. dlhodobý prenájom.',
        'capex_price_label': 'Cena nového zariadenia',
        'capex_rent_label': 'Mesačný prenájom zariadenia',
        'revizie_title': 'Strážca revízií a odborných prehliadok',
        'revizie_desc': 'Zistite termíny povinných periodických skúšok a inšpekcií zariadenia.',
        'revizia_perioda_1_label': 'Periodická inšpekcia',
        'revizia_perioda_2_label': 'Komplexná skúška',
        'revizie_date_label': 'Dátum uvedenia do prevádzky',
        'fplyny_title': 'F-Plyny a Chladivá',
        'fplyny_desc': 'Kontrola únikov chladiacich médií a periodicita podľa legislatívy.',
        'fplyn_type_label': 'Typ chladiva',
        'fplyn_amount_label': 'Náplň (kg)',
        'tco_profile_label': 'Typ prevádzky / Technológia',
        'cistota_title': 'Norma čistoty a úpravy média',
        'cistota_desc': 'Kvalitatívne požiadavky na úpravu pracovného média podľa aplikácie.',
        'cistota_select_label': 'Odvetvie / Prevádzka',
        'cistota_level_label': 'Požadovaná úroveň akosti',
        'cistota_tech_label': 'Doporučená technológia',
        'vibro_title': 'Vibrodiagnostika pohonu',
        'vibro_desc': 'Rýchle vyhodnotenie mechanického stavu ložísk a pohonu.',
        'vibro_input_label': 'Nameraná hodnota RMS (mm/s)',
        'tco_energy_label': 'Spotreba energie / médií',
        'tco_service_label': 'Servis a údržba',
        'tco_capex_label': 'Nákupná cena (Investícia)',
        'gauge_1_title': 'Primárne prevádzkové zaťaženie',
        'gauge_1_unit': '',
        'gauge_1_base': 0.0,
        'gauge_1_max': 100.0,
        'gauge_2_title': 'Prevádzková teplota',
        'gauge_2_unit': '°C',
        'gauge_2_base': 0.0,
        'gauge_2_max': 100.0,
        'gauge_3_title': 'Energetické vyťaženie',
        'gauge_3_unit': '%',
        'gauge_3_base': 0.0,
        'gauge_3_max': 100.0,
        'dig_1_title': 'Hlavný pohon',
        'dig_2_title': 'Pomocný okruh',
        'dig_error_title': 'Systémový stav',
        'dig_1_on': 'AKTÍVNY',
        'dig_1_off': 'NEAKTÍVNY',
        'dig_2_on': 'V PREVÁDZKE',
        'dig_2_off': 'VYPNUTÉ',
        'dig_error_ok': 'V NORME (BEZ PORÚCH)',
        'dig_error_fail': 'PORUCHA',
        'ticket_placeholder': 'Napr. ZAK-{year}-001',
        'ticket_status_vybavena': 'Vybavené / Dokončené',
        'polozka_fallback_nazov': 'Položka',
        'zariadenie_fallback_nazov': 'Zariadenie',
        'system_status_title': 'Stav Systému',
        'capex_buy_label': 'Nákup + Servis',
        'capex_rent_summary_label': 'Čistý prenájom',
        'fplyn_check_period_label': 'Kontrola 1x za {months} mes.',
        'fplyn_label_none': 'Bez povinnej kontroly',
        'nav_katalog_label': 'Katalóg & Ponuka',
        'cart_title': 'Váš dopyt',
        'cart_empty_text': 'Váš zoznam je zatiaľ prázdny.',
        'cart_order_btn': 'Záväzne objednať',
        'cart_inquiry_btn': 'Odoslať dopyt',
        'sekcia_material_label': 'Dodávka materiálu a položiek',
        'sekcia_prace_label': 'Odborné práce a služby',
        'vyjazd_label': 'Servisný výjazd',
        'vyjazd_jednotka': 'výjazd',
    }

    raw_terms = getattr(_config, 'INDUSTRY_TERMS', {}) if _config else {}
    terms = dict(core_defaults)
    if isinstance(raw_terms, dict):
        terms.update(raw_terms)
        for idx in (1, 2, 3):
            base_key = f'gauge_{idx}_base'
            max_key = f'gauge_{idx}_max'
            if base_key in raw_terms and max_key not in raw_terms:
                try:
                    base_val = float(raw_terms[base_key])
                    if base_val > 0:
                        terms[max_key] = round(base_val * 1.5, 2)
                except (ValueError, TypeError):
                    pass
        for k, v in raw_terms.items():
            if isinstance(k, str) and k.endswith('_label'):
                base_k = k[:-6]
                if base_k not in raw_terms:
                    terms[base_k] = v
    if extra_overrides and isinstance(extra_overrides, dict):
        terms.update(extra_overrides)

    # 🕒 DYNAMICKÝ ROK: 100% defenzívne nahradenie {year} bez rizika KeyError/ValueError pri iných zátvorkách
    aktualny_rok = str(date.today().year)
    for k, v in terms.items():
        if isinstance(v, str) and '{year}' in v:
            terms[k] = v.replace('{year}', aktualny_rok)

    for tab_key in ('hub_tab_esg', 'hub_tab_law', 'hub_tab_diag'):
        raw_tab = str(terms.get(tab_key, '') or '').strip()
        clean_tab = re.sub(r'^[^\w]+', '', raw_tab).strip()
        terms[f'{tab_key}_clean'] = clean_tab or raw_tab

    return terms


def get_active_parameter_reverse_names() -> List[str]:
    """
    Dynamicky vráti unikátny zoznam názvov reverzných väzieb zo ServisovaneZariadenie
    na parametrické modely aktívneho odvetvia.
    
    DEFENZÍVNE KONTROLY:
    - Overuje existenciu atribútu _meta na modeli.
    - Kontroluje, že OneToOneField skutočne smeruje na 'ServisovaneZariadenie'.
    - Výsledný zoznam deduplikuje pri zachovaní poradia.
    """
    reverse_names = []
    seen = set()

    for model_cls in ZARIADENIA_MAP.values():
        if not hasattr(model_cls, '_meta'):
            continue

        for field in model_cls._meta.get_fields():
            if field.is_relation and field.one_to_one and hasattr(field, 'remote_field'):
                # Kontrola cieľového modelu relácie
                target_model = getattr(field.remote_field, 'model', None)
                target_name = ""
                if isinstance(target_model, str):
                    target_name = target_model.split('.')[-1].lower()
                elif target_model is not None and hasattr(target_model, '_meta'):
                    target_name = target_model._meta.model_name.lower()

                if target_name == 'servisovanezariadenie':
                        remote_f = getattr(field, 'remote_field', None)
                        rel_name = getattr(remote_f, 'related_name', None) or (remote_f.get_accessor_name() if remote_f is not None and hasattr(remote_f, 'get_accessor_name') else None)
                        if rel_name and rel_name not in seen:
                            seen.add(rel_name)
                            reverse_names.append(rel_name)
                        break

    return reverse_names

def get_zariadenie_parametre(zariadenie):
    """
    🚀 ISAS ARCHITECT: Vráti inštanciu technických parametrov viazanú na konkrétne zariadenie.
    Funguje 100% dynamicky pre akékoľvek zariadenie a odvetvie bez hardkódu názvov relácií.
    """
    if not zariadenie or not getattr(zariadenie, 'kategoria', None):
        return None

    param_model = ZARIADENIA_MAP.get(zariadenie.kategoria)
    if not param_model or not hasattr(param_model, '_meta'):
        return None

    for field in param_model._meta.get_fields():
        if field.is_relation and field.one_to_one and hasattr(field, 'remote_field'):
            # 🛡️ DEFENSIVE: Overíme, že cieľom relácie je skutočne model ServisovaneZariadenie
            target_model = getattr(field.remote_field, 'model', None)
            target_name = ""
            if isinstance(target_model, str):
                target_name = target_model.split('.')[-1].lower()
            elif target_model is not None and hasattr(target_model, '_meta'):
                target_name = target_model._meta.model_name.lower()

            if target_name == 'servisovanezariadenie':
                rel_name = getattr(field.remote_field, 'related_name', None)
                if rel_name:
                    try:
                        return getattr(zariadenie, rel_name, None)
                    except Exception:
                        return None
    return None


# ------------------------------------------------------------------------------
# 🧠 DYNAMICKÉ NAČÍTANIE ODVETVOVÝCH AI PROMPTOV (Lazy Singleton)
# ------------------------------------------------------------------------------
_prompts_instance = None
_prompts_loaded = False


def _load_industry_prompts():
    """Dynamicky načíta odvetvový modul prompts.py podľa settings.INDUSTRY_MODULE."""
    raw_name = getattr(settings, 'INDUSTRY_MODULE', None)
    if not raw_name:
        return None

    industry_module_name = str(raw_name).strip().lower()
    if not industry_module_name:
        return None

    module_path = f"odvetvia.{industry_module_name}.prompts"
    try:
        return importlib.import_module(module_path)
    except ModuleNotFoundError as e:
        if e.name == module_path or e.name == f"odvetvia.{industry_module_name}":
            return None
        logger.exception(f"ISAS ARCHITECT: Chyba importu vnútri prompt modulu '{module_path}': {e}")
        raise
    except Exception as e:
        logger.exception(f"ISAS ARCHITECT: Chyba pri načítavaní prompt modulu '{module_path}': {e}")
        return None


def get_industry_prompts_module():
    """Lazy getter pre odvetvový prompt modul s ochranou pred predčasným načítaním."""
    global _prompts_instance, _prompts_loaded
    if not _prompts_loaded:
        _prompts_instance = _load_industry_prompts()
        _prompts_loaded = True
    return _prompts_instance


def get_industry_data_dictionary_prompt() -> str:
    """Vráti odvetvové modely a parametre pre dátový slovník (pre neutrálne ERP vracia prázdny reťazec)."""
    mod = get_industry_prompts_module()
    return getattr(mod, 'INDUSTRY_DATA_DICTIONARY_MODELS', '') if mod else ''


def get_industry_operativa_prompt() -> str:
    """Vráti odvetvové pravidlá revízií a inšpekcií pre operatívu (pre neutrálne ERP vracia prázdny reťazec)."""
    mod = get_industry_prompts_module()
    return getattr(mod, 'INDUSTRY_OPERATIVA_AND_COMPLIANCE', '') if mod else ''