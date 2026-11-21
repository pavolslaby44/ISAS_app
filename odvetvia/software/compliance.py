"""
odvetvia/software/compliance.py

⚖️ ISAS IT & CLOUD COMPLIANCE ENGINE – ODVETVIE: SOFTWARE & IT INFRAŠTRUKTÚRA
Normatívny, bezpečnostný a legislatívny rámec:
- Smernica NIS2 (EÚ) 2022/2555 o opatreniach na zabezpečenie vysokej úrovne kybernetickej bezpečnosti
- ISO/IEC 27001 (Systémy riadenia informačnej bezpečnosti - ISMS)
- BCP & Disaster Recovery (Kontinuita podnikania, periodické testovanie obnovy záloh)
- RFC 5280 / TLS štandardy (Platnosť a expirácia SSL certifikátov a doménových mien)
- Google SRE Golden Signals (Latency, Traffic, Errors, Saturation pre IoT/Telemetriu)
"""

import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional, Dict, Any, List, Tuple
from django.utils import timezone

logger = logging.getLogger(__name__)

from Evidencia.utils_compliance import (
    to_date,
    safe_get_param,
    add_calendar_interval,
    safe_float,
    get_iso_pre_objekt,
    get_compliance_config,
    get_legal_disclaimer,
    MANDATORY_LEGAL_DISCLAIMER,
    DEFAULT_LEGAL_DISCLAIMER,
)
from .models import (
    ParametreServer,
    ParametreAplikacia,
    ParametreDatabaza,
    SoftwareNastavenia,
    SoftwareStatLokalizacia,
)


# ==============================================================================
# ⚙️ 1. ODBOHATENIE ODVETVOVEJ KONFIGURÁCIE (ENRICH CONFIG)
# ==============================================================================

def enrich_config(config: Dict[str, Any], gn: Any = None, iso_clean: Optional[str] = None) -> Dict[str, Any]:
    """
    Naplní konfiguráciu o predpísané lehoty špecifické pre odvetvie IT a softvérových systémov.
    Jediným zdrojom pravdy sú dátové modely SoftwareNastavenia a SoftwareStatLokalizacia.
    Nula riadkov hardkódu: predvolené hodnoty sa pri absencii záznamu v DB čítajú priamo z metadát modelu.
    """
    pole_map = {
        'ssl_dni': 'lehota_ssl_upozornenie_dni',
        'domena_dni': 'lehota_domena_upozornenie_dni',
        'zalohovanie_dr_test_mesiace': 'lehota_zalohovanie_dr_test_mesiace',
        'audit_nis2_roky': 'lehota_audit_nis2_roky',
        'audit_iso27001_roky': 'lehota_audit_iso27001_roky',
        'penetracny_test_roky': 'lehota_penetracny_test_roky',
        'rotacia_pristupov_dni': 'lehota_rotacia_pristupov_dni',
    }

    sn = None
    try:
        sn = SoftwareNastavenia.objects.first()
    except Exception:
        pass

    source_obj = sn or gn

    for cfg_key, field_name in pole_map.items():
        val = getattr(source_obj, field_name, None) if source_obj else None
        if val is None:
            try:
                field_obj = SoftwareNastavenia._meta.get_field(field_name)
                val = getattr(field_obj, 'default', 1)
            except Exception:
                val = 1
        config[cfg_key] = max(1, int(val if val is not None else 1))

    if iso_clean:
        stat_lehoty = None
        try:
            stat_lehoty = SoftwareStatLokalizacia.objects.filter(stat__iso_kod=iso_clean).first()
        except Exception:
            pass

        stat_pole_map = {
            'audit_nis2_roky': 'lehota_audit_nis2_roky',
            'zalohovanie_dr_test_mesiace': 'lehota_zalohovanie_dr_test_mesiace',
            'penetracny_test_roky': 'lehota_penetracny_test_roky',
            'rotacia_pristupov_dni': 'lehota_rotacia_pristupov_dni',
        }

        if stat_lehoty:
            for cfg_key, field_name in stat_pole_map.items():
                val = getattr(stat_lehoty, field_name, None)
                if val:
                    config[cfg_key] = max(1, int(val if val is not None else 1))

    if 'audit_nis2_roky' in config:
        config['revizia_perioda_1_roky'] = int(config['audit_nis2_roky'])
    if 'penetracny_test_roky' in config:
        config['revizia_perioda_2_roky'] = int(config['penetracny_test_roky'])

    return config


# ==============================================================================
# 📱 2. COMPLIANCE AUDIT APLIKÁCIÍ (SSL, DOMÉNA, PENTEST)
# ==============================================================================

def vyhodnot_aplikacia_compliance(
    aplikacia: Any,
    referencny_datum: Optional[date] = None,
    limit_dni_alert: Optional[int] = None,
    cfg: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """
    Vyhodnotí expirácie digitálnej identity webovej aplikácie / API (SSL/TLS certifikát, doména)
    a zákonné lehoty penetračného testovania bezpečnosti.
    """
    dnes = to_date(referencny_datum) or timezone.now().date()
    active_cfg = get_compliance_config(iso=get_iso_pre_objekt(aplikacia))
    if cfg and isinstance(cfg, dict):
        active_cfg.update(cfg)
    cfg = active_cfg

    limit_dni_global = limit_dni_alert if limit_dni_alert is not None else cfg.get('predstih_dni', 14)
    vysledky: List[Dict[str, Any]] = []

    # 1. Expirácia SSL / TLS certifikátu
    d_ssl = to_date(getattr(aplikacia, 'ssl_certifikat_vyprsanie', None))
    limit_ssl = int(cfg.get('ssl_dni', 30))
    auto_obnova = getattr(aplikacia, 'ssl_auto_obnova', False)

    if d_ssl:
        dni_ssl = (d_ssl - dnes).days
        t_str_ssl = d_ssl.strftime('%d.%m.%Y')

        if dni_ssl < 0:
            stav = 'PREPADNUTA'
            farba = 'red'
            hodnota_ui = f'Expirovaný ({abs(dni_ssl)} dní)'
            stav_str = f"SSL CERTIFIKÁT VYPRŠAL pred {abs(dni_ssl)} dňami! ({t_str_ssl})"
            vyzaduje_alert = True
        elif dni_ssl <= limit_ssl:
            stav = 'BLIZI_SA'
            farba = 'orange'
            hodnota_ui = f'Expiruje o {dni_ssl} dní ({t_str_ssl})'
            stav_str = f"SSL certifikát vyprší o {dni_ssl} dní ({t_str_ssl})"
            vyzaduje_alert = True
        else:
            stav = 'PLATNA'
            farba = 'emerald'
            hodnota_ui = f'Platný do {t_str_ssl}'
            stav_str = f"Platný do {t_str_ssl}"
            vyzaduje_alert = False

        auto_info = " (Detegovaná automatická obnova certifikátu)" if auto_obnova else " (Manuálna správa certifikátu)"
        stav_suffix = '-EXP' if stav == 'PREPADNUTA' else ('-WARN' if stav == 'BLIZI_SA' else '-OK')

        vysledky.append({
            'kod': f'IT-SSL{stav_suffix}',
            'cache_kod': 'ssl_cert',
            'nazov': 'SSL / TLS certifikát',
            'typ_lehoty': f'🔒 Platnosť SSL certifikátu{auto_info}',
            'termin': d_ssl,
            'termin_str': t_str_ssl,
            'dni_do': dni_ssl,
            'stav': stav,
            'farba': farba,
            'ikona': '🔒',
            'hodnota_ui': hodnota_ui,
            'stav_str': stav_str,
            'vyzaduje_alert': vyzaduje_alert,
            'je_prepadnuta': (stav == 'PREPADNUTA'),
            'je_bliziaca_sa': (stav == 'BLIZI_SA'),
            'je_chybajuca': False,
            'upozornenie_text': (
                f"<strong>🔒 BEZPEČNOSŤ KOMUNIKÁCIE (RFC 5280 / TLS):</strong><br>"
                f"Expirácia produkčného TLS certifikátu spôsobí zablokovanie webu v prehliadačoch "
                f"a výpadok HTTPS/WSS šifrovaného spojenia.{auto_info}"
            ),
            'ai_poznamka': 'ISAS SecOps: Zabezpečte overenie funkčnosti obnovy certifikátu (Let\'s Encrypt / Certbot).',
            'disclaimer': cfg.get('disclaimer', DEFAULT_LEGAL_DISCLAIMER)
        })
    elif getattr(aplikacia, 'url_adresa', None):
        vysledky.append({
            'kod': 'IT-SSL-CHYBA',
            'cache_kod': 'ssl_cert',
            'nazov': 'SSL / TLS certifikát',
            'typ_lehoty': '🔒 Platnosť SSL certifikátu',
            'termin': dnes,
            'termin_str': 'Neuvedený',
            'dni_do': -99999,
            'stav': 'CHYBA_KONTROLA',
            'farba': 'red',
            'ikona': '🔒',
            'hodnota_ui': 'Chýba dátum expirácie SSL',
            'stav_str': 'CHÝBA DÁTUM EXPIRÁCIE SSL CERTIFIKÁTU!',
            'vyzaduje_alert': True,
            'je_prepadnuta': False,
            'je_bliziaca_sa': False,
            'je_chybajuca': True,
            'upozornenie_text': "<strong>🔒 BEZPEČNOSŤ:</strong><br>Aplikácia má priradenú URL adresu, no chýba evidencia platnosti SSL certifikátu.",
            'ai_poznamka': 'ISAS SecOps: Doplňte dátum vypršania TLS certifikátu.',
            'disclaimer': cfg.get('disclaimer', DEFAULT_LEGAL_DISCLAIMER)
        })

    # 2. Expirácia internetovej domény
    d_dom = to_date(getattr(aplikacia, 'domena_vyprsanie', None))
    limit_dom = int(cfg.get('domena_dni', 60))

    if d_dom:
        dni_dom = (d_dom - dnes).days
        t_str_dom = d_dom.strftime('%d.%m.%Y')

        if dni_dom < 0:
            stav_d = 'PREPADNUTA'
            farba_d = 'red'
            hodnota_ui_d = f'Expirovaná ({abs(dni_dom)} dní)'
            stav_str_d = f"DOMÉNA EXPIROVALA pred {abs(dni_dom)} dňami! ({t_str_dom})"
            alert_d = True
        elif dni_dom <= limit_dom:
            stav_d = 'BLIZI_SA'
            farba_d = 'orange'
            hodnota_ui_d = f'Expiruje o {dni_dom} dní ({t_str_dom})'
            stav_str_d = f"Registrácia domény vyprší o {dni_dom} dní ({t_str_dom})"
            alert_d = True
        else:
            stav_d = 'PLATNA'
            farba_d = 'emerald'
            hodnota_ui_d = f'Platná do {t_str_dom}'
            stav_str_d = f"Platná do {t_str_dom}"
            alert_d = False

        dom_suffix = '-EXP' if stav_d == 'PREPADNUTA' else ('-WARN' if stav_d == 'BLIZI_SA' else '-OK')

        vysledky.append({
            'kod': f'IT-DOM{dom_suffix}',
            'cache_kod': 'domena',
            'nazov': 'Registrácia domény (DNS)',
            'typ_lehoty': '🌐 Expirácia internetovej domény',
            'termin': d_dom,
            'termin_str': t_str_dom,
            'dni_do': dni_dom,
            'stav': stav_d,
            'farba': farba_d,
            'ikona': '🌐',
            'hodnota_ui': hodnota_ui_d,
            'stav_str': stav_str_d,
            'vyzaduje_alert': alert_d,
            'je_prepadnuta': (stav_d == 'PREPADNUTA'),
            'je_bliziaca_sa': (stav_d == 'BLIZI_SA'),
            'je_chybajuca': False,
            'upozornenie_text': (
                "<strong>🌐 KONTINUITA SLUŽBY (DNS Infraštruktúra):</strong><br>"
                "Neobnovenie registrácie domény včas spôsobí okamžité vyradenie DNS zón, "
                "nedostupnosť webu a riziko registrácie domény treťou stranou (domain squatting)."
            ),
            'ai_poznamka': 'ISAS DNS Guard: Skontrolujte kredit alebo predĺžte registráciu domény u registrátora.',
            'disclaimer': cfg.get('disclaimer', DEFAULT_LEGAL_DISCLAIMER)
        })

    # 3. Penetračný test systémov (NIS2 / ISO 27001)
    lehota_pent_roky = int(cfg.get('penetracny_test_roky', 1))
    d_pent = to_date(getattr(aplikacia, 'datum_dalsieho_pentestu', None))
    if not d_pent and getattr(aplikacia, 'datum_posledneho_pentestu', None):
        p_pent = to_date(aplikacia.datum_posledneho_pentestu)
        if p_pent:
            d_pent = add_calendar_interval(p_pent, years=lehota_pent_roky)

    if d_pent:
        dni_p = (d_pent - dnes).days
        t_str_p = d_pent.strftime('%d.%m.%Y')

        if dni_p < 0:
            stav_p = 'PREPADNUTA'
            farba_p = 'red'
            hodnota_ui_p = f'Prepadnutý ({abs(dni_p)} dní)'
            stav_str_p = f"PENETRAČNÝ TEST JE PREPADNUTÝ o {abs(dni_p)} dní! ({t_str_p})"
            alert_p = True
        elif dni_p <= limit_dni_global:
            stav_p = 'BLIZI_SA'
            farba_p = 'orange'
            hodnota_ui_p = f'Termín o {dni_p} dní ({t_str_p})'
            stav_str_p = f"Lehota penetračného testu vyprší o {dni_p} dní ({t_str_p})"
            alert_p = True
        else:
            stav_p = 'PLATNA'
            farba_p = 'emerald'
            hodnota_ui_p = f'Platný do {t_str_p}'
            stav_str_p = f"Platný do {t_str_p}"
            alert_p = False

        p_suffix = '-EXP' if stav_p == 'PREPADNUTA' else ('-WARN' if stav_p == 'BLIZI_SA' else '-OK')

        vysledky.append({
            'kod': f'IT-SEC-PENT{p_suffix}',
            'cache_kod': 'pentest',
            'nazov': 'Penetračný test aplikácie',
            'typ_lehoty': f'🛡️ Bezpečnostný penetračný test ({lehota_pent_roky} r. lehota)',
            'termin': d_pent,
            'termin_str': t_str_p,
            'dni_do': dni_p,
            'stav': stav_p,
            'farba': farba_p,
            'ikona': '🎯',
            'hodnota_ui': hodnota_ui_p,
            'stav_str': stav_str_p,
            'vyzaduje_alert': alert_p,
            'je_prepadnuta': (stav_p == 'PREPADNUTA'),
            'je_bliziaca_sa': (stav_p == 'BLIZI_SA'),
            'je_chybajuca': False,
            'upozornenie_text': (
                f"<strong>🛡️ AUDIT BEZPEČNOSTI (NIS2 / ISO 27001):</strong><br>"
                f"Vypršala odporúčaná lehota periodického etického hackingu a penetračného testovania aplikácie. "
                f"Systém je potrebné podrobiť overeniu zraniteľností podľa štandardu OWASP Top 10."
            ),
            'ai_poznamka': 'ISAS CISO: Naplánujte externý penetračný test aplikácie a API rozhraní.',
            'disclaimer': cfg.get('disclaimer', DEFAULT_LEGAL_DISCLAIMER)
        })

    return vysledky


# ==============================================================================
# 🗄️ 3. COMPLIANCE AUDIT DATABÁZ (DR TESTY, ZÁLOHY, RPO/RTO)
# ==============================================================================

def vyhodnot_databaza_compliance(
    databaza: Any,
    referencny_datum: Optional[date] = None,
    limit_dni_alert: Optional[int] = None,
    cfg: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """
    Vyhodnotí kontinuitu prevádzky databázového klastra, Disaster Recovery (DR) cvičenia
    a aktuálnosť overených záloh podľa požiadaviek RPO a RTO.
    """
    dnes = to_date(referencny_datum) or timezone.now().date()
    active_cfg = get_compliance_config(iso=get_iso_pre_objekt(databaza))
    if cfg and isinstance(cfg, dict):
        active_cfg.update(cfg)
    cfg = active_cfg

    limit_dni = limit_dni_alert if limit_dni_alert is not None else cfg.get('predstih_dni', 14)
    vysledky: List[Dict[str, Any]] = []

    # 1. Periodicita Disaster Recovery (DR) testu obnovy
    lehota_dr_mesiace = int(cfg.get('zalohovanie_dr_test_mesiace', 6))
    d_dr = to_date(getattr(databaza, 'datum_dalsieho_dr_testu', None))
    if not d_dr and getattr(databaza, 'datum_posledneho_dr_testu', None):
        p_dr = to_date(databaza.datum_posledneho_dr_testu)
        if p_dr:
            d_dr = add_calendar_interval(p_dr, months=lehota_dr_mesiace)

    if d_dr:
        dni_dr = (d_dr - dnes).days
        t_str_dr = d_dr.strftime('%d.%m.%Y')

        if dni_dr < 0:
            stav = 'PREPADNUTA'
            farba = 'red'
            hodnota_ui = f'Prepadnutý ({abs(dni_dr)} dní)'
            stav_str = f"DR TEST OBNOVY DÁT JE PREPADNUTÝ o {abs(dni_dr)} dní! ({t_str_dr})"
            alert = True
        elif dni_dr <= limit_dni:
            stav = 'BLIZI_SA'
            farba = 'orange'
            hodnota_ui = f'Test o {dni_dr} dní ({t_str_dr})'
            stav_str = f"Lehota DR testu obnovy vyprší o {dni_dr} dní ({t_str_dr})"
            alert = True
        else:
            stav = 'PLATNA'
            farba = 'emerald'
            hodnota_ui = f'Platný do {t_str_dr}'
            stav_str = f"Platný do {t_str_dr}"
            alert = False

        dr_suffix = '-EXP' if stav == 'PREPADNUTA' else ('-WARN' if stav == 'BLIZI_SA' else '-OK')

        vysledky.append({
            'kod': f'IT-BCK-DR{dr_suffix}',
            'cache_kod': 'dr_test',
            'nazov': 'Disaster Recovery test (DR)',
            'typ_lehoty': f'💾 DR test obnovy dát zo zálohy ({lehota_dr_mesiace} mes. lehota)',
            'termin': d_dr,
            'termin_str': t_str_dr,
            'dni_do': dni_dr,
            'stav': stav,
            'farba': farba,
            'ikona': '🧪',
            'hodnota_ui': hodnota_ui,
            'stav_str': stav_str,
            'vyzaduje_alert': alert,
            'je_prepadnuta': (stav == 'PREPADNUTA'),
            'je_bliziaca_sa': (stav == 'BLIZI_SA'),
            'je_chybajuca': False,
            'upozornenie_text': (
                f"<strong>💾 KONTINUITA DÁT (NIS2 & Disaster Recovery Plan):</strong><br>"
                f"Podľa predpisov kontinuity prevádzky je povinné vykonať praktické cvičenie obnovy databázy "
                f"zo zálohy v predpísanej lehote (každých {lehota_dr_mesiace} mesiacov). Neotestovaná záloha nie je záloha."
            ),
            'ai_poznamka': 'ISAS DBA Guardian: Spustite cvičnú obnovu zálohy do izolovaného testovacieho prostredia.',
            'disclaimer': cfg.get('disclaimer', DEFAULT_LEGAL_DISCLAIMER)
        })
    else:
        vysledky.append({
            'kod': 'IT-BCK-DR-CHYBA',
            'cache_kod': 'dr_test',
            'nazov': 'Disaster Recovery test (DR)',
            'typ_lehoty': f'💾 DR test obnovy dát zo zálohy ({lehota_dr_mesiace} mes. lehota)',
            'termin': dnes,
            'termin_str': 'Neuvedený',
            'dni_do': -99999,
            'stav': 'CHYBA_KONTROLA',
            'farba': 'red',
            'ikona': '🧪',
            'hodnota_ui': 'Chýba záznam o DR teste',
            'stav_str': 'CHÝBA ZÁZNAM O DISASTER RECOVERY TESTE OBNOVY!',
            'vyzaduje_alert': True,
            'je_prepadnuta': False,
            'je_bliziaca_sa': False,
            'je_chybajuca': True,
            'upozornenie_text': "<strong>💾 RIZIKO STRATY DÁT:</strong><br>Databáza nemá zaevidovaný žiadny úspešný Disaster Recovery test.",
            'ai_poznamka': 'ISAS DBA Guardian: Naplánujte a zaevidujte overenie funkčnosti zálohy.',
            'disclaimer': cfg.get('disclaimer', DEFAULT_LEGAL_DISCLAIMER)
        })

    # 2. Kontrola dátumu poslednej overenej zálohy
    d_zaloha = to_date(getattr(databaza, 'datum_poslednej_zalohy', None))
    rpo_h = int(getattr(databaza, 'rpo_hodiny', 24))

    if d_zaloha:
        dni_zal = (dnes - d_zaloha).days
        t_str_zal = d_zaloha.strftime('%d.%m.%Y')
        max_dni_zalohy = max(1, rpo_h // 24)

        if dni_zal > max_dni_zalohy:
            vysledky.append({
                'kod': 'IT-BCK-STALE',
                'cache_kod': 'zaloha_stav',
                'nazov': 'Zálohovanie databázy',
                'typ_lehoty': f'💿 Posledná overená záloha (RPO: {rpo_h} h)',
                'termin': d_zaloha,
                'termin_str': t_str_zal,
                'dni_do': -dni_zal,
                'stav': 'PREPADNUTA',
                'farba': 'red',
                'ikona': '💾',
                'hodnota_ui': f'Neaktuálna ({dni_zal} dní stará)',
                'stav_str': f"ZÁLOHA DÁT JE NEAKTUÁLNA! Posledná overená: {t_str_zal} (Prekročené RPO {rpo_h} h)",
                'vyzaduje_alert': True,
                'je_prepadnuta': True,
                'je_bliziaca_sa': False,
                'je_chybajuca': False,
                'upozornenie_text': (
                    f"<strong>⚠️ PORUŠENIE RPO (Recovery Point Objective):</strong><br>"
                    f"Posledná overená záloha je staršia než tolerovaný limit RPO ({rpo_h} hodín). "
                    f"Hrozí nevratná strata produkčných transakcií."
                ),
                'ai_poznamka': 'ISAS DBA: Skontrolujte nočný snapshot skript a úložisko záloh.',
                'disclaimer': cfg.get('disclaimer', DEFAULT_LEGAL_DISCLAIMER)
            })

    return vysledky


# ==============================================================================
# 🖥️ 4. COMPLIANCE AUDIT SERVEROV (PATCHING, ROTÁCIA KĽÚČOV, NIS2)
# ==============================================================================

def vyhodnot_server_compliance(
    server: Any,
    referencny_datum: Optional[date] = None,
    limit_dni_alert: Optional[int] = None,
    cfg: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """
    Vyhodnotí stav údržbového okna, rotáciu poverení (SSH/API kľúčov)
    a zákonné audity kybernetickej bezpečnosti serverového uzla (NIS2, ISO 27001).
    """
    dnes = to_date(referencny_datum) or timezone.now().date()
    active_cfg = get_compliance_config(iso=get_iso_pre_objekt(server))
    if cfg and isinstance(cfg, dict):
        active_cfg.update(cfg)
    cfg = active_cfg

    limit_dni = limit_dni_alert if limit_dni_alert is not None else cfg.get('predstih_dni', 14)
    vysledky: List[Dict[str, Any]] = []

    # 1. Servisné okno a rotácia prístupov (SSH kľúče, certifikáty)
    lehota_rotacie_dni = int(cfg.get('rotacia_pristupov_dni', 90))
    d_udrzba = to_date(getattr(server, 'datum_dalsej_udrzby', None))
    if not d_udrzba and getattr(server, 'datum_posledneho_patchu', None):
        p_patch = to_date(server.datum_posledneho_patchu)
        if p_patch:
            d_udrzba = p_patch + timedelta(days=lehota_rotacie_dni)

    if d_udrzba:
        dni_u = (d_udrzba - dnes).days
        t_str_u = d_udrzba.strftime('%d.%m.%Y')

        if dni_u < 0:
            stav = 'PREPADNUTA'
            farba = 'red'
            hodnota_ui = f'Prepadnutá ({abs(dni_u)} dní)'
            stav_str = f"ÚDRŽBA A ROTÁCIA PRÍSTUPOV JE PREPADNUTÁ o {abs(dni_u)} dní! ({t_str_u})"
            alert = True
        elif dni_u <= limit_dni:
            stav = 'BLIZI_SA'
            farba = 'orange'
            hodnota_ui = f'Údržba o {dni_u} dní ({t_str_u})'
            stav_str = f"Servisné okno a rotácia kľúčov vyprší o {dni_u} dní ({t_str_u})"
            alert = True
        else:
            stav = 'PLATNA'
            farba = 'emerald'
            hodnota_ui = f'Plánované na {t_str_u}'
            stav_str = f"Plánované na {t_str_u}"
            alert = False

        u_suffix = '-EXP' if stav == 'PREPADNUTA' else ('-WARN' if stav == 'BLIZI_SA' else '-OK')

        vysledky.append({
            'kod': f'IT-SEC-ROT{u_suffix}',
            'cache_kod': 'rotacia_kľucov',
            'nazov': 'Údržbové okno a rotácia poverení',
            'typ_lehoty': f'🔑 Rotácia prístupov a OS patch ({lehota_rotacie_dni} dňová lehota)',
            'termin': d_udrzba,
            'termin_str': t_str_u,
            'dni_do': dni_u,
            'stav': stav,
            'farba': farba,
            'ikona': '🔑',
            'hodnota_ui': hodnota_ui,
            'stav_str': stav_str,
            'vyzaduje_alert': alert,
            'je_prepadnuta': (stav == 'PREPADNUTA'),
            'je_bliziaca_sa': (stav == 'BLIZI_SA'),
            'je_chybajuca': False,
            'upozornenie_text': (
                f"<strong>🔑 HYGIENA PRÍSTUPOV (NIS2 Článok 21):</strong><br>"
                f"Vypršala lehota bezpečnostnej rotácie administrátorských SSH kľúčov a servisných hesiel "
                f"spolu s kontrolou bezpečnostných záplat operačného systému (lehota: každých {lehota_rotacie_dni} dní)."
            ),
            'ai_poznamka': 'ISAS SecOps: Vykonajte rotáciu SSH keys a aplikujte security kernel updates.',
            'disclaimer': cfg.get('disclaimer', DEFAULT_LEGAL_DISCLAIMER)
        })

    # 2. Bezpečnostný audit NIS2
    lehota_nis2_roky = int(cfg.get('audit_nis2_roky', 2))
    if getattr(server, 'typ_servera', '') in ('baremetal', 'node', 'k8s'):
        vysledky.append({
            'kod': 'IT-SEC-NIS2-INFO',
            'cache_kod': 'nis2_audit',
            'nazov': 'Regulačný audit NIS2',
            'typ_lehoty': f'🛡️ Audit kybernetickej bezpečnosti NIS2 ({lehota_nis2_roky} r. periodicita)',
            'termin': add_calendar_interval(dnes, years=lehota_nis2_roky),
            'termin_str': 'Platný štandard',
            'dni_do': 365,
            'stav': 'PLATNA',
            'farba': 'emerald',
            'ikona': '🛡️',
            'hodnota_ui': f'Zhoda NIS2 ({lehota_nis2_roky} r.)',
            'stav_str': f'Spadá pod požiadavky NIS2 ({lehota_nis2_roky} r. interval auditu)',
            'vyzaduje_alert': False,
            'je_prepadnuta': False,
            'je_bliziaca_sa': False,
            'je_chybajuca': False,
            'upozornenie_text': "Kritický uzol podliehajúci povinnému auditu riadenia kybernetických rizík.",
            'ai_poznamka': 'ISAS Compliance: Zaevidujte technickú dokumentáciu v súlade so Smernicou NIS2.',
            'disclaimer': cfg.get('disclaimer', DEFAULT_LEGAL_DISCLAIMER)
        })

    return vysledky


# ==============================================================================
# 🎯 5. CENTRÁLNY AUDIT PRE JEDNOTLIVÉ ZARIADENIE (DISPATCHER)
# ==============================================================================

def get_zariadenie_compliance_audit(
    zariadenie: Any,
    referencny_datum: Optional[date] = None,
    limit_dni_alert: Optional[int] = None,
    cfg: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """
    Kompletný audit vyhradených, kybernetických a digitálnych kontrol pre IT zariadenie.
    Polymorfne spracováva servery, webové/API aplikácie aj databázové klastre.
    Plne rešpektuje globálne vypnutie sledovania legislatívy v nastaveniach jadra.
    """
    active_cfg = get_compliance_config(iso=get_iso_pre_objekt(zariadenie))
    if cfg and isinstance(cfg, dict):
        active_cfg.update(cfg)
    cfg = active_cfg

    if not cfg.get('sledovat_legislativu', True):
        return []

    polozky: List[Dict[str, Any]] = []
    dnes = to_date(referencny_datum) or timezone.now().date()
    limit_dni = limit_dni_alert if limit_dni_alert is not None else cfg.get('predstih_dni', 14)
    kat = getattr(zariadenie, 'kategoria', None)

    # 1. Server a výpočtová infraštruktúra
    if kat == 'server':
        p_server = safe_get_param(zariadenie, 'parametre_server')
        if p_server:
            polozky.extend(vyhodnot_server_compliance(p_server, dnes, limit_dni, cfg=cfg))

    # 2. Webová aplikácia / API mikroslužba
    elif kat == 'aplikacia':
        p_app = safe_get_param(zariadenie, 'parametre_aplikacia')
        if p_app:
            polozky.extend(vyhodnot_aplikacia_compliance(p_app, dnes, limit_dni, cfg=cfg))

    # 3. Databázový cluster a úložisko
    elif kat == 'databaza':
        p_db = safe_get_param(zariadenie, 'parametre_databaza')
        if p_db:
            polozky.extend(vyhodnot_databaza_compliance(p_db, dnes, limit_dni, cfg=cfg))

    return polozky


# ==============================================================================
# 🚀 6. GLOBÁLNY DISPEČING IT AUDITOV (GLOBAL COMPLIANCE QUEUE)
# ==============================================================================

def get_global_compliance_queue(
    referencny_datum: Optional[date] = None,
    limit_dni_alert: Optional[int] = None,
    filter_alerts_only: bool = True
) -> List[Dict[str, Any]]:
    """
    🚀 ISAS ENTERPRISE COMPLIANCE QUEUE – ODVETVIE: SOFTWARE & IT
    Jednotné a vysoko optimalizované systémové rozhranie pre zber všetkých IT a kybernetických lehôt.
    Garantuje 0 N+1 dopytov a rešpektuje globálne nastavenie 'servisy_sledovat_legislativu'.
    """
    cfg = get_compliance_config()
    if not cfg.get('sledovat_legislativu', True):
        return []

    dnes = to_date(referencny_datum) or timezone.now().date()
    limit_dni = limit_dni_alert if limit_dni_alert is not None else cfg.get('predstih_dni', 14)
    queue: List[Dict[str, Any]] = []

    # 1. Aplikácie (SSL certifikáty, domény, pentesty) - 0 N+1
    try:
        app_qs = ParametreAplikacia.objects.select_related(
            'zariadenie', 'zariadenie__zakaznik'
        ).filter(zariadenie__v_servisnom_rezime=False, zariadenie__kategoria='aplikacia')

        for app_param in app_qs:
            zar = app_param.zariadenie
            if not zar:
                continue
            stroj_cfg = get_compliance_config(iso=get_iso_pre_objekt(zar))
            for item in vyhodnot_aplikacia_compliance(app_param, referencny_datum=dnes, limit_dni_alert=limit_dni, cfg=stroj_cfg):
                if not filter_alerts_only or item.get('vyzaduje_alert'):
                    item['zariadenie'] = zar
                    item['stroj_kategoria'] = 'webová aplikácia / API'
                    queue.append(item)
    except Exception as exc:
        logger.warning("ISAS IT COMPLIANCE: Chyba pri aplikáciách: %s", exc)

    # 2. Databázy (Disaster Recovery cvičenia a zálohy) - 0 N+1
    try:
        db_qs = ParametreDatabaza.objects.select_related(
            'zariadenie', 'zariadenie__zakaznik'
        ).filter(zariadenie__v_servisnom_rezime=False, zariadenie__kategoria='databaza')

        for db_param in db_qs:
            zar = db_param.zariadenie
            if not zar:
                continue
            stroj_cfg = get_compliance_config(iso=get_iso_pre_objekt(zar))
            for item in vyhodnot_databaza_compliance(db_param, referencny_datum=dnes, limit_dni_alert=limit_dni, cfg=stroj_cfg):
                if not filter_alerts_only or item.get('vyzaduje_alert'):
                    item['zariadenie'] = zar
                    item['stroj_kategoria'] = 'databázový cluster'
                    queue.append(item)
    except Exception as exc:
        logger.warning("ISAS IT COMPLIANCE: Chyba pri databázach: %s", exc)

    # 3. Servery (Patching, rotácia kľúčov, NIS2) - 0 N+1
    try:
        srv_qs = ParametreServer.objects.select_related(
            'zariadenie', 'zariadenie__zakaznik'
        ).filter(zariadenie__v_servisnom_rezime=False, zariadenie__kategoria='server')

        for srv_param in srv_qs:
            zar = srv_param.zariadenie
            if not zar:
                continue
            stroj_cfg = get_compliance_config(iso=get_iso_pre_objekt(zar))
            for item in vyhodnot_server_compliance(srv_param, referencny_datum=dnes, limit_dni_alert=limit_dni, cfg=stroj_cfg):
                if not filter_alerts_only or item.get('vyzaduje_alert'):
                    item['zariadenie'] = zar
                    item['stroj_kategoria'] = 'serverový uzol'
                    queue.append(item)
    except Exception as exc:
        logger.warning("ISAS IT COMPLIANCE: Chyba pri serveroch: %s", exc)

    queue.sort(key=lambda x: int(x['dni_do']) if isinstance(x.get('dni_do'), (int, float)) else 99999)
    return queue


# ==============================================================================
# 🎛️ 7. IOT TELEMETRIA & SRE ALARMY (GOOGLE SRE GOLDEN SIGNALS)
# ==============================================================================

def vyhodnot_iot_telemetriu(
    zariadenie: Any,
    analogove_hodnoty: dict,
    digitalne_stavy: Optional[dict] = None
) -> Optional[Dict[str, Any]]:
    """
    Vyhodnocuje prichádzajúci IT IoT / Telemetrický paket (CPU, RAM, Sieťová odozva, HTTP Error Rate)
    voči definovaným limitom servera alebo aplikácie.
    Vracia štruktúru pre AlarmLog, alebo None ak je stav v norme.
    """
    from Evidencia.industry_loader import get_zariadenie_parametre
    parametre = get_zariadenie_parametre(zariadenie)

    if not analogove_hodnoty:
        return None

    cpu_val = safe_float(analogove_hodnoty.get('cpu_usage', analogove_hodnoty.get('CPU', analogove_hodnoty.get('cpu'))))
    ram_val = safe_float(analogove_hodnoty.get('ram_usage', analogove_hodnoty.get('RAM', analogove_hodnoty.get('ram'))))
    lat_val = safe_float(analogove_hodnoty.get('network_latency_ms', analogove_hodnoty.get('latency', analogove_hodnoty.get('ping'))))
    err_val = safe_float(analogove_hodnoty.get('http_error_rate', analogove_hodnoty.get('error_rate', analogove_hodnoty.get('errors'))))

    alarm_dovod = None
    kod_chyby = 'IT-PERF-CRIT'

    # 1. Vyťaženie CPU (Warning >= 80%, Critical >= 95%)
    max_cpu = safe_float(getattr(parametre, 'max_vykon_cpu_percent', 95.0), 95.0)
    if cpu_val and cpu_val >= max_cpu:
        alarm_dovod = f"Kritické vyťaženie CPU servera: {cpu_val:.1f} % (Limit: {max_cpu:.1f} %)"
        kod_chyby = 'IT-PERF-CPU-HIGH'

    # 2. Využitie pamäte RAM (Kritické >= 95% alebo parameter)
    max_ram = safe_float(getattr(parametre, 'max_vyuzitie_ram_percent', 95.0), 95.0)
    if not alarm_dovod and ram_val and ram_val >= max_ram:
        alarm_dovod = f"Kritické zaplnenie RAM (Hrozí OOM crash): {ram_val:.1f} % (Limit: {max_ram:.1f} %)"
        kod_chyby = 'IT-PERF-RAM-OOM'

    # 3. Sieťová latencia (Kritické >= 500 ms)
    max_lat = safe_float(getattr(parametre, 'max_latencia_ms', 500.0), 500.0)
    if not alarm_dovod and lat_val and lat_val >= max_lat:
        alarm_dovod = f"Kritická sieťová latencia endpointu: {lat_val:.0f} ms (Limit: {max_lat:.0f} ms)"
        kod_chyby = 'IT-PERF-LAT-SPIKE'

    # 4. HTTP 5xx Error Rate (Kritické >= 5.0%)
    max_err = safe_float(getattr(parametre, 'max_error_rate_percent', 5.0), 5.0)
    if not alarm_dovod and err_val and err_val >= max_err:
        alarm_dovod = f"Vysoká miera chybových odpovedí HTTP 5xx: {err_val:.2f} % (Limit: {max_err:.2f} %)"
        kod_chyby = 'IT-PERF-ERR-RATE'

    if alarm_dovod:
        return {
            'zavaznost': 'CRITICAL',
            'kod_chyby': kod_chyby,
            'popis': alarm_dovod,
        }
    return None


# ==============================================================================
# 📊 8. LIVE DIAGNOSTIKA TELEMETRIE PRE DASHBOARD
# ==============================================================================

def get_zariadenie_live_diagnostika(zariadenie: Any, posledny_zaznam: Any = None) -> List[Dict[str, Any]]:
    """
    Vyhodnocuje live diagnostiku telemetrie pre IT dashboard (Google SRE Golden Signals).
    Vracia zoznam diagnostických kariet pre CPU, RAM, Latenciu a Error rate.
    """
    if not posledny_zaznam or not posledny_zaznam.analogove_hodnoty:
        return []

    d = posledny_zaznam.analogove_hodnoty
    karty: List[Dict[str, Any]] = []

    # 1. Vyťaženie CPU
    cpu = safe_float(d.get('cpu_usage', d.get('CPU', d.get('cpu'))))
    if cpu is not None:
        if cpu >= 90.0:
            karty.append({
                'nazov': 'Vyťaženie CPU',
                'hodnota': f'{cpu:.1f} % (Kritické zaťaženie)',
                'farba': 'red',
                'ikona': '🧠',
                'alarm_kod': 'IT-PERF-CPU',
                'alarm_popis': f'CPU beží na {cpu:.1f} %. Hrozí vyčerpanie výpočtových jadier.',
                'alarm_zavaznost': 'CRITICAL'
            })
        elif cpu >= 75.0:
            karty.append({
                'nazov': 'Vyťaženie CPU',
                'hodnota': f'{cpu:.1f} % (Zvýšené)',
                'farba': 'orange',
                'ikona': '🧠'
            })
        else:
            karty.append({
                'nazov': 'Vyťaženie CPU',
                'hodnota': f'{cpu:.1f} % (Optimálne)',
                'farba': 'emerald',
                'ikona': '🧠'
            })

    # 2. Využitie pamäte RAM
    ram = safe_float(d.get('ram_usage', d.get('RAM', d.get('ram'))))
    if ram is not None:
        if ram >= 90.0:
            karty.append({
                'nazov': 'Využitie RAM',
                'hodnota': f'{ram:.1f} % (Hrozí OOM)',
                'farba': 'red',
                'ikona': '💾',
                'alarm_kod': 'IT-PERF-RAM',
                'alarm_popis': f'Pamäť RAM je zaplnená na {ram:.1f} %. Hrozí pád procesov.',
                'alarm_zavaznost': 'CRITICAL'
            })
        elif ram >= 80.0:
            karty.append({
                'nazov': 'Využitie RAM',
                'hodnota': f'{ram:.1f} % (Vysoké)',
                'farba': 'orange',
                'ikona': '💾'
            })
        else:
            karty.append({
                'nazov': 'Využitie RAM',
                'hodnota': f'{ram:.1f} % (Stabilné)',
                'farba': 'emerald',
                'ikona': '💾'
            })

    # 3. Sieťová odozva a latencia
    lat = safe_float(d.get('network_latency_ms', d.get('latency', d.get('ping'))))
    if lat is not None:
        if lat >= 300.0:
            karty.append({
                'nazov': 'Sieťová odozva (Ping)',
                'hodnota': f'{lat:.0f} ms (Vysoká latencia)',
                'farba': 'red',
                'ikona': '⏱️',
                'alarm_kod': 'IT-PERF-LAT',
                'alarm_popis': f'Odozva dosahuje {lat:.0f} ms. Aplikácia môže reagovať spomalene.',
                'alarm_zavaznost': 'WARNING'
            })
        else:
            karty.append({
                'nazov': 'Sieťová odozva (Ping)',
                'hodnota': f'{lat:.0f} ms (Blesková)',
                'farba': 'emerald',
                'ikona': '⏱️'
            })

    # 4. Miera chybovosti HTTP 5xx (Google SRE Error Rate)
    err = safe_float(d.get('http_error_rate', d.get('error_rate', d.get('errors'))))
    if err is not None:
        if err >= 5.0:
            karty.append({
                'nazov': 'Chybovosť (Error Rate)',
                'hodnota': f'{err:.2f} % (Kritický výpadok)',
                'farba': 'red',
                'ikona': '📉',
                'alarm_kod': 'IT-PERF-ERR',
                'alarm_popis': f'Miera chybových požiadaviek dosiahla {err:.2f} %. Prebieha incident.',
                'alarm_zavaznost': 'CRITICAL'
            })
        elif err >= 1.0:
            karty.append({
                'nazov': 'Chybovosť (Error Rate)',
                'hodnota': f'{err:.2f} % (Zvýšená)',
                'farba': 'orange',
                'ikona': '📉',
                'alarm_kod': 'IT-PERF-ERR',
                'alarm_popis': f'Zvýšená chybovosť požiadaviek {err:.2f} %.',
                'alarm_zavaznost': 'WARNING'
            })
        else:
            karty.append({
                'nazov': 'Chybovosť (Error Rate)',
                'hodnota': f'{err:.2f} % (V norme)',
                'farba': 'emerald',
                'ikona': '📉'
            })

    return karty