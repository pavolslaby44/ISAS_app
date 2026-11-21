"""
⚖️ ISAS CORE INDUSTRIAL COMPLIANCE ENGINE
Univerzálny kalendárny, lokalizačný a inšpekčný dispečer jadra ERP.
Poskytuje kalendárnu aritmetiku chránenú proti priestupným rokom,
právne doložky a dynamické prepojenie na aktívny odvetvový modul.
"""

import calendar
import logging
from datetime import date, datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
from django.apps import apps
from django.core.cache import cache

logger = logging.getLogger(__name__)


def get_industry_compliance():
    """Deleguje načítanie compliance modulu na centrálny industry_loader."""
    from .industry_loader import get_industry_compliance as _loader_get_compliance
    return _loader_get_compliance()


def to_date(val: Any) -> Optional[date]:
    """Bezpečne prevedie datetime, date alebo textový dátum na date. Ošetruje None a edge cases."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    if isinstance(val, str):
        v_clean = val.strip().split('T')[0]
        if ':' in v_clean:
            v_clean = v_clean.split(' ')[0]
        v_clean = v_clean.replace('. ', '.').replace(' .', '.').strip()
        for fmt in ('%Y-%m-%d', '%d.%m.%Y', '%Y/%m/%d'):
            try:
                return datetime.strptime(v_clean, fmt).date()
            except ValueError:
                continue
    return None


def safe_get_param(obj: Any, attr: str) -> Any:
    """🛡️ ISAS ARCHITECT: Bezpečné získanie reverznej 1:1 relácie s ochranou pred ObjectDoesNotExist."""
    try:
        return getattr(obj, attr, None)
    except Exception:
        return None


def add_calendar_interval(base_date: Any, years: int = 0, months: int = 0) -> Optional[date]:
    """
    Exaktný posun dátumu o kalendárne roky a mesiace s plnou ochranou
    proti posunu pri priestupných rokoch (napr. 29. február -> 28. február) a koncoch mesiacov.
    Vnútorne sanitizuje vstupy typu str, datetime a date.
    """
    b_date = to_date(base_date)
    if not b_date:
        return None
    try:
        y_int = int(years or 0)
        m_int = int(months or 0)
    except (ValueError, TypeError):
        return None
    year = b_date.year + y_int
    month = b_date.month + m_int
    year += (month - 1) // 12
    month = ((month - 1) % 12) + 1
    try:
        max_days = calendar.monthrange(year, month)[1]
        day = min(b_date.day, max_days)
        return date(year, month, day)
    except (ValueError, OverflowError):
        return None

# ⚖️ ZÁKONNÁ DOLOŽKA AUTORA SOFTVÉRU - Táto doložka je povinná, nemenná a chráni vývojára pred zodpovednosťou za pokuty, škody a revízie.
MANDATORY_LEGAL_DISCLAIMER = (
    "Informatívny procesný alert systému ISAS CORE (poskytovaný v režime „as is“). "
    "Nenahrádza úkony autorizovaného revízneho technika, inšpekčnej organizácie ani certifikovanej odbornej osoby. "
    "Poskytovateľ softvéru v maximálnom rozsahu povolenom rozhodným právom nenesie zodpovednosť za nedodržanie lehôt, "
    "zlyhanie notifikácií, škody ani pokuty dozorných orgánov. "
    "Prevádzkovateľ zariadenia nesie plnú a výlučnú zodpovednosť za bezpečný stav a platnosť predpísaných revízií a kontrol."
)

DEFAULT_LEGAL_DISCLAIMER = MANDATORY_LEGAL_DISCLAIMER


def get_iso_pre_objekt(obj: Any) -> Optional[str]:
    """Bezpečne získa ISO kód štátu prevádzky stroja z väzby na zákazníka s fallbackom na sídlo firmy."""
    try:
        zar = getattr(obj, 'zariadenie', None) or obj
        zak = getattr(zar, 'zakaznik', None) or (zar if hasattr(zar, 'stat') else None)
        raw_stat = getattr(zak, 'stat', None) if zak else None
        StatLokalizaciaModel = apps.get_model('ISASaplikacia', 'StatLokalizacia')
        if raw_stat and StatLokalizaciaModel:
            iso = StatLokalizaciaModel.rozpoznaj_iso(raw_stat)
            if iso and iso not in ('NEZADANE', 'INYKRAJINA'):
                return iso
        FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
        if FiremnaIdentitaModel and hasattr(FiremnaIdentitaModel, 'get_aktivna_krajina_iso'):
            return FiremnaIdentitaModel.get_aktivna_krajina_iso()
    except Exception:
        pass
    return None


def get_compliance_config(iso: Optional[str] = None) -> Dict[str, Any]:
    """
    Dynamicky načíta používateľsky nastavené lehoty z DB s cachingom.
    Plná podpora multi-country lokalizácie: lehoty sa prednostne riadia modelom StatLokalizacia
    daného štátu, s bezpečným fallbackom na GlobalneNastavenia a medzinárodné štandardy.
    """
    StatLokalizaciaModel = apps.get_model('ISASaplikacia', 'StatLokalizacia')
    iso_clean = None
    if iso and StatLokalizaciaModel:
        iso_clean = StatLokalizaciaModel.rozpoznaj_iso(iso)
        if iso_clean in ('NEZADANE', 'INYKRAJINA'):
            iso_clean = None

    cache_key = f'isas_compliance_config_{iso_clean}' if iso_clean else 'isas_compliance_config'
    try:
        cached_cfg = cache.get(cache_key)
        if cached_cfg:
            return dict(cached_cfg)
    except Exception:
        pass

    config = {
        'predstih_dni': 14,
        'predstih_motohodiny': 100,
        'sledovat_legislativu': True,
        'disclaimer': DEFAULT_LEGAL_DISCLAIMER
    }
    try:
        GlobalneNastavenia = apps.get_model('ISASaplikacia', 'GlobalneNastavenia')
        gn = GlobalneNastavenia.objects.first()
        if gn:
            config['sledovat_legislativu'] = getattr(gn, 'servisy_sledovat_legislativu', True)
            if getattr(gn, 'servisy_predstih_upozornenia_dni', None):
                config['predstih_dni'] = max(1, int(gn.servisy_predstih_upozornenia_dni))
            if getattr(gn, 'servisy_predstih_motohodiny', None):
                config['predstih_motohodiny'] = max(1, int(gn.servisy_predstih_motohodiny))
            config['disclaimer'] = MANDATORY_LEGAL_DISCLAIMER

        # 🚀 ISAS PLUGIN ARCHITECTURE: Obohatenie o lehoty aktívneho odvetvia
        ind_compliance = get_industry_compliance()
        enrich_fn = getattr(ind_compliance, 'enrich_config', None)
        if callable(enrich_fn):
            config = enrich_fn(config, gn=gn, iso_clean=iso_clean)

    except Exception as exc:
        logger.warning("ISAS COMPLIANCE: Chyba pri načítavaní compliance konfigurácie: %s", exc)

    try:
        cache.set(cache_key, config, timeout=300)
    except Exception:
        pass
    return config


def get_legal_disclaimer() -> str:
    """Dynamicky vráti text doložky z centralizovanej cache (s fallbackom na zákonný štandard)."""
    cfg = get_compliance_config()
    return cfg.get('disclaimer') or DEFAULT_LEGAL_DISCLAIMER


def safe_float(val: Any, default: float = 0.0) -> float:
    """Bezpečne konvertuje číslo alebo reťazec s čiarkou na float."""
    if val is None:
        return default
    try:
        res = float(str(val).replace(',', '.').strip())
        if res != res or res in (float('inf'), float('-inf')):
            return default
        return res
    except (ValueError, TypeError):
        return default


def get_zariadenie_compliance_audit(zariadenie: Any, referencny_datum: Optional[date] = None, limit_dni_alert: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    🚀 ISAS UNIVERSAL COMPLIANCE AUDIT DISPATCHER
    Dynamicky deleguje audit zariadenia na aktívny odvetvový modul.
    Ak odvetvie nemá vlastný compliance modul alebo beží neutrálne ERP, bezpečne vracia [].
    """
    cfg = get_compliance_config(iso=get_iso_pre_objekt(zariadenie))
    if not cfg.get('sledovat_legislativu', True):
        return []

    mod = get_industry_compliance()
    if mod and hasattr(mod, 'get_zariadenie_compliance_audit'):
        return mod.get_zariadenie_compliance_audit(zariadenie, referencny_datum=referencny_datum, limit_dni_alert=limit_dni_alert)
    return []


def get_global_compliance_queue(
    referencny_datum: Optional[date] = None,
    limit_dni_alert: Optional[int] = None,
    filter_alerts_only: bool = True
) -> List[Dict[str, Any]]:
    """
    🚀 ISAS UNIVERSAL COMPLIANCE QUEUE DISPATCHER
    Dynamicky deleguje zber revízií do centrálnej fronty na aktívny odvetvový modul.
    Garantuje 0 chýb LookupError a plnú izoláciu univerzálneho ERP jadra.
    """
    cfg = get_compliance_config()
    if not cfg.get('sledovat_legislativu', True):
        return []

    mod = get_industry_compliance()
    if mod and hasattr(mod, 'get_global_compliance_queue'):
        return mod.get_global_compliance_queue(
            referencny_datum=referencny_datum,
            limit_dni_alert=limit_dni_alert,
            filter_alerts_only=filter_alerts_only
        )
    return []


def vyhodnot_zariadenie_iot_telemetriu(zariadenie: Any, analogove_hodnoty: dict, digitalne_stavy: Optional[dict] = None) -> Optional[Dict[str, Any]]:
    """
    🚀 ISAS UNIVERSAL IOT TELEMETRY DISPATCHER
    Dynamicky deleguje diagnostiku telemetrie na aktívny odvetvový modul.
    """
    mod = get_industry_compliance()
    if mod and hasattr(mod, 'vyhodnot_iot_telemetriu'):
        return mod.vyhodnot_iot_telemetriu(zariadenie, analogove_hodnoty, digitalne_stavy=digitalne_stavy)
    return None


def get_zariadenie_live_diagnostika(zariadenie: Any, posledny_zaznam: Any = None) -> List[Dict[str, Any]]:
    """
    🚀 ISAS UNIVERSAL LIVE DIAGNOSTICS DISPATCHER
    Dynamicky deleguje live diagnostiku telemetrie na aktívny odvetvový modul.
    """
    mod = get_industry_compliance()
    if mod and hasattr(mod, 'get_zariadenie_live_diagnostika'):
        return mod.get_zariadenie_live_diagnostika(zariadenie, posledny_zaznam=posledny_zaznam)
    return []