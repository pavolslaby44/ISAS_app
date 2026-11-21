"""
odvetvia/kompresory/compliance.py

⚖️ ISAS INDUSTRIAL COMPLIANCE ENGINE – ODVETVIE: KOMPRESORY A STLAČENÝ VZDUCH
Normatívny a legislatívny rámec:
- EN ISO 4126 (Poistné ventily kompresorov a nádob)
- Nariadenie (EÚ) 2024/573 o F-plynoch (chladivá sušičiek a chillerov)
- EN ISO 4414 (Pneumatika a bezpečnosť tlakových hadíc)
- Smernica PED 2014/68/EÚ a národné predpisy pre VTZ tlakové zariadenia
"""

import logging
from datetime import date
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
    ParametreKompresor,
    ParametreSusicka,
    ParametreTlakovaNadoba,
    ParametreFiltracia,
    ParametreOdlucovac,
    ParametreGeneratorPlynu,
    ParametreChladic,
    ParametreKlimatizacia,
    KompresoryNastavenia,
    KompresoryStatLokalizacia,
)

# 🧪 Koeficienty GWP (Potenciál globálneho otepľovania podľa IPCC AR6 / Nariadenia EÚ 2024/573)
GWP_MAP: Dict[str, int] = {
    'R410A': 2088,
    'R134A': 1430,
    'R404A': 3922,
    'R32': 675,
    'R407C': 1774,
    'R507': 3985,
    'R507A': 3985,
    'R513A': 631,
    'R422D': 2729,
    'R407A': 2107,
    'R407F': 1825,
    'R290': 3,
    'R600A': 3,
    'R1234YF': 4,
    'R1234ZE': 7,
    'R448A': 1387,
    'R449A': 1397,
    'R452A': 2141,
}


def enrich_config(config: Dict[str, Any], gn: Any = None, iso_clean: Optional[str] = None) -> Dict[str, Any]:
    """
    Naplní konfiguráciu o predpísané lehoty špecifické pre odvetvie kompresorov a stlačeného vzduchu.
    Jediným zdrojom pravdy sú dátové modely KompresoryNastavenia a KompresoryStatLokalizacia.
    Nula riadkov hardkódu: predvolené hodnoty sa pri absencii záznamu v DB čítajú priamo z metadát modelu.
    """
    # 1. Mapovanie konfiguračných kľúčov na polia modelu KompresoryNastavenia
    pole_map = {
        'prevadzkova_roky': 'lehota_vtz_prevadzkova_roky',
        'vnutorna_roky': 'lehota_vtz_vnutorna_roky',
        'tlakova_roky': 'lehota_vtz_tlakova_roky',
        'poistny_ventil_roky': 'lehota_vtz_poistny_ventil_roky',
        'fplyn_male_mesiace': 'lehota_fplyn_5_az_50t_mesiace',
        'fplyn_stredne_mesiace': 'lehota_fplyn_50_az_500t_mesiace',
        'fplyn_velke_mesiace': 'lehota_fplyn_nad_500t_mesiace',
        'elektro_roky': 'lehota_vtz_elektro_roky',
        'manometer_roky': 'lehota_vtz_manometer_roky',
        'desikant_roky': 'lehota_desikant_roky',
        'hadice_roky': 'lehota_tlakove_hadice_roky',
        'odlucovac_filtre_roky': 'lehota_odlucovac_filtre_roky',
        'filter_vlozky_roky': 'lehota_filter_vlozky_roky',
        'kalibracia_o2_roky': 'lehota_kalibracia_o2_roky',
    }

    kn = None
    try:
        kn = KompresoryNastavenia.objects.first()
    except Exception:
        pass

    source_obj = kn or gn

    for cfg_key, field_name in pole_map.items():
        val = getattr(source_obj, field_name, None) if source_obj else None
        if val is None:
            # Dynamický fallback z definície modelu (Model Introspection namiesto hardkódu)
            try:
                field_obj = KompresoryNastavenia._meta.get_field(field_name)
                val = getattr(field_obj, 'default', 1)
            except Exception:
                val = 1
        config[cfg_key] = max(1, int(val if val is not None else 1))

    # 2. Národné odvetvové lehoty z KompresoryStatLokalizacia
    if iso_clean:
        stat_lehoty = None
        try:
            stat_lehoty_mgr: Any = KompresoryStatLokalizacia.objects
            stat_lehoty = stat_lehoty_mgr.filter(stat__iso_kod=iso_clean).first()
        except Exception:
            pass

        stat_pole_map = {
            'prevadzkova_roky': 'lehota_vtz_prevadzkova_roky',
            'vnutorna_roky': 'lehota_vtz_vnutorna_roky',
            'tlakova_roky': 'lehota_vtz_tlakova_roky',
            'poistny_ventil_roky': 'lehota_vtz_poistny_ventil_roky',
            'elektro_roky': 'lehota_vtz_elektro_roky',
            'manometer_roky': 'lehota_vtz_manometer_roky',
        }

        if stat_lehoty:
            for cfg_key, field_name in stat_pole_map.items():
                val = getattr(stat_lehoty, field_name, None)
                if val:
                    config[cfg_key] = max(1, int(val if val is not None else 1))
        else:
            # Dočasný spätný fallback na staré polia v StatLokalizacia (kým sa v Kroku 4 neodstránia)
            try:
                from django.apps import apps
                StatLokalizaciaModel = apps.get_model('ISASaplikacia', 'StatLokalizacia')
                stat_obj = StatLokalizaciaModel.get_for_iso(iso_clean)
                if stat_obj:
                    for cfg_key, field_name in stat_pole_map.items():
                        val = getattr(stat_obj, field_name, None)
                        if val:
                            config[cfg_key] = max(1, int(val if val is not None else 1))
            except Exception:
                pass

    if 'vnutorna_roky' in config:
        config['revizia_perioda_1_roky'] = int(config['vnutorna_roky'])
    if 'tlakova_roky' in config:
        config['revizia_perioda_2_roky'] = int(config['tlakova_roky'])
    raw_idle = getattr(ParametreKompresor, 'koeficient_naprazdno', Decimal('0.30'))
    config['esg_idle_factor'] = float(str(raw_idle).replace(',', '.'))
    return config


def normalize_chladivo(chladivo: Optional[str]) -> str:
    """Normalizuje textové označenie chladiva (napr. ' R-410A ' -> 'R410A')."""
    if not chladivo:
        return ""
    return str(chladivo).upper().replace(' ', '').replace('-', '').strip()


def vypocitaj_co2_ekvivalent(chladivo: Optional[str], mnozstvo_kg: Any, zadany_co2_eq: Any = None) -> Tuple[float, bool]:
    """
    Vypočíta CO2 ekvivalent v tonách: (kg * GWP) / 1000.
    Vracia tuple: (co2_ekvivalent_t, je_validny_vypocet).
    Ak chladivo nie je v číselníku a nie je zadaný CO2 eq, vracia je_validny_vypocet=False.
    """
    if zadany_co2_eq is not None:
        val = safe_float(zadany_co2_eq, 0.0)
        if val > 0:
            return round(val, 2), True

    c_clean = normalize_chladivo(chladivo)
    kg = safe_float(mnozstvo_kg, 0.0)
    if not c_clean and kg == 0.0:
        return 0.0, True

    gwp = GWP_MAP.get(c_clean, 0)
    if gwp > 0 and kg > 0:
        return round((kg * gwp) / 1000.0, 2), True
    if (c_clean and kg <= 0) or (kg > 0 and gwp == 0):
        return 0.0, False
    return 0.0, True


def vyhodnot_fplyny_compliance(
    chladivo: Optional[str],
    mnozstvo_kg: Any,
    datum_poslednej: Any,
    zadany_co2_eq: Any = None,
    referencny_datum: Optional[date] = None,
    limit_dni_alert: Optional[int] = None,
    cfg: Optional[Dict[str, Any]] = None,
    ma_detekcny_system: bool = False
) -> Optional[Dict[str, Any]]:
    """
    Vyhodnotí povinnosť a lehotu kontroly tesnosti chladiaceho okruhu
    podľa Nariadenia Európskeho parlamentu a Rady (EÚ) 2024/573 o F-plynoch.
    """
    dnes = to_date(referencny_datum) or timezone.now().date()
    co2_val, je_validne = vypocitaj_co2_ekvivalent(chladivo, mnozstvo_kg, zadany_co2_eq)
    d_posledna = to_date(datum_poslednej)
    kg = safe_float(mnozstvo_kg, 0.0)

    # Ak zariadenie nemá chladivo, ani hmotnosť, ani dátum kontroly a ani zadaný ekvivalent CO2, nejde o F-plyn okruh
    if not chladivo and kg == 0.0 and d_posledna is None and safe_float(zadany_co2_eq, 0.0) <= 0.0:
        return None

    active_cfg = get_compliance_config()
    if cfg and isinstance(cfg, dict):
        active_cfg.update(cfg)
    limit_dni = limit_dni_alert if limit_dni_alert is not None else active_cfg.get('predstih_dni', 14)

    # Záchranná brzda: chýbajúce vstupné údaje pre výpočet CO2 eq
    if not je_validne:
        c_clean = normalize_chladivo(chladivo)
        if not c_clean:
            kod_chyby = 'FPLYN-CHYBA-CHLADIVO'
            nazov_chyby = 'F-Plyny (Chýba typ chladiva)'
            hodnota_ui = 'Chýba typ chladiva'
            stav_str = 'Chýba typ použitého chladiva. Doplňte označenie média (napr. R410A)!'
            upozornenie_text = "<strong>⚠️ CHÝBA TYP CHLADIVA:</strong><br>Pre výpočet zákonných revízií F-plynov zadajte označenie chladiaceho média."
        elif kg <= 0:
            kod_chyby = 'FPLYN-CHYBA-HMOTNOST'
            nazov_chyby = 'F-Plyny (Chýba hmotnosť)'
            hodnota_ui = f'Chýba hmotnosť ({chladivo})'
            stav_str = f'Chýba hmotnosť chladiacej náplne pre "{chladivo}". Doplňte množstvo v kg!'
            upozornenie_text = f"<strong>⚠️ CHÝBA HMOTNOSŤ NÁPLNE ({chladivo}):</strong><br>Pre výpočet zákonných revízií F-plynov zadajte množstvo chladiva v kg."
        else:
            kod_chyby = 'FPLYN-CHYBA-GWP'
            nazov_chyby = 'F-Plyny (Neznáme GWP)'
            hodnota_ui = f'Neznáme GWP ({chladivo})'
            stav_str = f'Neznámy GWP koeficient chladiva "{chladivo}". Zadajte CO₂ ekvivalent ručne!'
            upozornenie_text = f"<strong>⚠️ NEZNÁME CHLADIVO V ČÍSELNÍKU ({chladivo}):</strong><br>Pre chladivo {chladivo} chýba hodnota GWP. Doplňte ekvivalent CO₂ priamo v karte zariadenia."
        return {
            'kod': kod_chyby,
            'cache_kod': 'fplyn',
            'nazov': nazov_chyby,
            'typ_lehoty': '❄️ Kontrola únikov F-plynov',
            'termin': dnes,
            'termin_str': 'Neuvedený',
            'dni_do': -99999,
            'stav': 'CHYBA_KONFIGURACIE',
            'farba': 'red',
            'ikona': '⚠️',
            'hodnota_ui': hodnota_ui,
            'stav_str': stav_str,
            'co2_ekvivalent_t': 0.0,
            'chladivo': chladivo or 'Nezadané',
            'vyzaduje_alert': True,
            'je_prepadnuta': False,
            'je_bliziaca_sa': False,
            'je_chybajuca': True,
            'upozornenie_text': upozornenie_text,
            'ai_poznamka': 'ISAS Compliance: Skontrolujte štítok chladiaceho okruhu a doplňte parametre.',
            'disclaimer': active_cfg.get('disclaimer', DEFAULT_LEGAL_DISCLAIMER)
        }

    # Zákonná povinnosť vzniká až od 5.0 ton CO2 eq
    if co2_val < 5.0:
        return {
            'kod': 'FPLYN-BEZ-POVINNOSTI',
            'cache_kod': 'fplyn',
            'nazov': 'F-Plyny (Úniky)',
            'typ_lehoty': '❄️ Kontrola únikov F-plynov',
            'termin': None,
            'termin_str': 'Bez povinnej kontroly',
            'dni_do': 99999,
            'stav': 'BEZ_POVINNOSTI',
            'farba': 'gray',
            'ikona': '❄️',
            'hodnota_ui': f'Bez povinnej kontroly ({co2_val:.1f}t CO₂)',
            'stav_str': f'Bez povinnej kontroly (náplň {co2_val:.1f} t CO₂ eq < 5 t)',
            'co2_ekvivalent_t': co2_val,
            'chladivo': chladivo or 'Nezadané',
            'vyzaduje_alert': False,
            'je_prepadnuta': False,
            'je_bliziaca_sa': False,
            'je_chybajuca': False,
            'disclaimer': active_cfg.get('disclaimer', DEFAULT_LEGAL_DISCLAIMER),
        }

    # Kalendárne intervaly načítané z konfigurácie (s uplatnením 2x predĺženia pri systéme detekcie únikov LDS)
    nasobyc_lds = 2 if ma_detekcny_system else 1
    lds_popis = " (s LDS detekciou)" if ma_detekcny_system else ""

    if co2_val >= 500.0:
        mesiace = int(active_cfg['fplyn_velke_mesiace']) * nasobyc_lds
        lehota_nazov = f"každých {mesiace} mes.{lds_popis}"
    elif co2_val >= 50.0:
        mesiace = int(active_cfg['fplyn_stredne_mesiace']) * nasobyc_lds
        lehota_nazov = f"každých {mesiace} mes.{lds_popis}"
    else:
        mesiace = int(active_cfg['fplyn_male_mesiace']) * nasobyc_lds
        lehota_nazov = f"každých {mesiace} mes.{lds_popis}"

    termin = add_calendar_interval(d_posledna, months=mesiace) if d_posledna else None
    if termin:
        dni_do = (termin - dnes).days
        je_chybajuca = False
        termin_str = termin.strftime('%d.%m.%Y')

        if dni_do < 0:
            stav = 'PREPADNUTA'
            farba = 'red'
            hodnota_ui = f'Prepadnutá ({abs(dni_do)} dní, {co2_val:.1f}t CO₂)'
            stav_str = f"JE PREPADNUTÁ o {abs(dni_do)} dní! ({termin_str})"
            vyzaduje_alert = True
        elif dni_do <= limit_dni:
            stav = 'BLIZI_SA'
            farba = 'orange'
            hodnota_ui = f'Končí o {dni_do} dní ({termin_str})'
            stav_str = f"vyprší o {dni_do} dní ({termin_str})"
            vyzaduje_alert = True
        else:
            stav = 'PLATNA'
            farba = 'emerald'
            hodnota_ui = f'Platná do {termin_str} ({co2_val:.1f}t CO₂)'
            stav_str = f"Platná do {termin_str}"
            vyzaduje_alert = False
    else:
        termin = dnes
        dni_do = -99999
        je_chybajuca = True
        stav = 'CHYBA_KONTROLA'
        farba = 'red'
        hodnota_ui = f'Chýba kontrola (Povinná {lehota_nazov}, {co2_val:.1f}t CO₂)'
        stav_str = f"CHÝBA KONTROLA! (Zariadenie má {co2_val:.1f} t CO₂ eq, povinná lehota: {lehota_nazov})"
        termin_str = "Neuvedený"
        vyzaduje_alert = True

    kod_fplyn = 'FPLYN-EXP' if stav == 'PREPADNUTA' else ('FPLYN-WARN' if stav == 'BLIZI_SA' else ('FPLYN-CHYBA' if stav == 'CHYBA_KONTROLA' else 'FPLYN-OK'))
    return {
        'kod': kod_fplyn,
        'cache_kod': 'fplyn',
        'nazov': 'F-Plyny (Úniky)',
        'typ_lehoty': f"❄️ Kontrola únikov F-plynov (Povinná {lehota_nazov})",
        'termin': termin,
        'termin_str': termin_str,
        'dni_do': dni_do,
        'stav': stav,
        'farba': farba,
        'ikona': '❄️',
        'hodnota_ui': hodnota_ui,
        'stav_str': stav_str,
        'co2_ekvivalent_t': co2_val,
        'chladivo': chladivo or 'F-plyn',
        'lehota_nazov': lehota_nazov,
        'vyzaduje_alert': vyzaduje_alert,
        'je_prepadnuta': (stav == 'PREPADNUTA'),
        'je_bliziaca_sa': (stav == 'BLIZI_SA'),
        'je_chybajuca': je_chybajuca,
        'upozornenie_text': (
            f"<strong>🌍 ENVIRO LEGISLATÍVA (Nariadenie EÚ č. 2024/573 o F-plynoch):</strong><br>"
            f"Pri náplni {co2_val:.1f} t CO₂ eq je zo zákona povinná kontrola tesnosti v periodicite {lehota_nazov} "
            f"certifikovanou osobou s riadnym zápisom do prevádzkovej knihy zariadenia."
        ),
        'ai_poznamka': 'ISAS Eco Guardian: Zabezpečte kontrolu úniku certifikovaným chladiarom.',
        'disclaimer': active_cfg.get('disclaimer', DEFAULT_LEGAL_DISCLAIMER)
    }


def vyhodnot_tlakova_nadoba_compliance(nadoba: Any, referencny_datum: Optional[date] = None, limit_dni_alert: Optional[int] = None, cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    Vyhodnotí predpísané revízie a inšpekcie integrity tlakovej nádoby.
    S plnou podporou používateľských lehôt a hlásením chýbajúcich kontrol.
    """
    dnes = to_date(referencny_datum) or timezone.now().date()
    skupina = getattr(nadoba, 'skupina_vtz', 'Bb')
    active_cfg = get_compliance_config(iso=get_iso_pre_objekt(nadoba))
    if cfg and isinstance(cfg, dict):
        active_cfg.update(cfg)
    cfg = active_cfg
    limit_dni = limit_dni_alert if limit_dni_alert is not None else cfg.get('predstih_dni', 14)
    vysledky: List[Dict[str, Any]] = []

    def vyhodnot_skusku(d_dalsia, d_posledna, interval_roky, kod, cache_kod, nazov, typ_lehoty, upozornenie, ai_note):
        termin = to_date(d_dalsia)
        if not termin and d_posledna:
            p_date = to_date(d_posledna)
            if p_date:
                termin = add_calendar_interval(p_date, years=interval_roky)

        if termin:
            dni = (termin - dnes).days
            termin_str = termin.strftime('%d.%m.%Y')
            stav = 'PREPADNUTA' if dni < 0 else ('BLIZI_SA' if dni <= limit_dni else 'PLATNA')
            farba = 'red' if dni < 0 else ('orange' if dni <= limit_dni else 'emerald')
            hodnota_ui = f'Prepadnutá ({abs(dni)} dní)' if dni < 0 else (f'Končí o {dni} dní ({termin_str})' if dni <= limit_dni else f'Platná ({termin_str})')
            stav_str = f"JE PREPADNUTÁ o {abs(dni)} dní! ({termin_str})" if dni < 0 else (f"vyprší o {dni} dní ({termin_str})" if dni <= limit_dni else f"Platná do {termin_str}")
            vyzaduje_alert = (dni <= limit_dni)
            je_chybajuca = False
        else:
            termin = dnes
            dni = -99999
            termin_str = "Neuvedený"
            stav = 'CHYBA_KONTROLA'
            farba = 'red'
            hodnota_ui = f'Chýba revízia ({nazov})'
            stav_str = f"CHÝBA ZÁZNAM O REVÍZII! ({nazov})"
            vyzaduje_alert = True
            je_chybajuca = True

        stav_suffix = '-EXP' if stav == 'PREPADNUTA' else ('-WARN' if stav == 'BLIZI_SA' else ('-CHYBA' if stav == 'CHYBA_KONTROLA' else '-OK'))
        return {
            'kod': f"{kod}{stav_suffix}",
            'cache_kod': cache_kod,
            'nazov': nazov,
            'typ_lehoty': typ_lehoty,
            'termin': termin,
            'termin_str': termin_str,
            'dni_do': dni,
            'stav': stav,
            'farba': farba,
            'ikona': '⚖️',
            'skupina_vtz': skupina,
            'hodnota_ui': hodnota_ui,
            'stav_str': stav_str,
            'vyzaduje_alert': vyzaduje_alert,
            'je_prepadnuta': (stav == 'PREPADNUTA'),
            'je_bliziaca_sa': (stav == 'BLIZI_SA'),
            'je_chybajuca': je_chybajuca,
            'upozornenie_text': upozornenie,
            'ai_poznamka': ai_note,
            'disclaimer': cfg['disclaimer']
        }

    # 1. Prevádzková revízia
    lehota_prev_roky = cfg['prevadzkova_roky']
    vysledky.append(vyhodnot_skusku(
        getattr(nadoba, 'datum_dalsej_prevadzkovej_revizie', None),
        getattr(nadoba, 'datum_poslednej_prevadzkovej_revizie', None),
        lehota_prev_roky,
        'VTZ-PREV', 'revizia', 'Prevádzková revízia',
        f'⚖️ Prevádzková revízia nádoby ({lehota_prev_roky} r. lehota)',
        f"<strong>⚠️ BEZPEČNOSTNÁ INŠPEKCIA (Pravidelná revízia tlakových nádob):</strong><br>Prevádzkovateľ je povinný zabezpečiť vonkajšiu prevádzkovú inšpekciu tlakovej nádoby (stanovená lehota: každých {lehota_prev_roky} r.). Prevádzka tlakovej nádoby bez platnej revízie je zakázaná.",
        'ISAS CORE Compliance: Zabezpečte výjazd autorizovaného technika.'
    ))

    # 2. Vnútorná skúška
    lehota_vnut_roky = cfg['vnutorna_roky']
    vysledky.append(vyhodnot_skusku(
        getattr(nadoba, 'datum_dalsej_vnutornej_skusky', None),
        getattr(nadoba, 'datum_poslednej_vnutornej_skusky', None),
        lehota_vnut_roky,
        'VTZ-VNUT', 'vnutorna', 'Vnútorná skúška',
        f'🛢️ Vnútorná inšpekcia nádoby ({lehota_vnut_roky} r. lehota)',
        f"<strong>⚠️ ODBORNÁ INŠPEKCIA (Vnútorná inšpekcia tlakového priestoru):</strong><br>Skúška vyžaduje odstavenie, odtlakovanie a vnútornú inšpekciu revíznym inšpektorom (lehota: každých {lehota_vnut_roky} r.).",
        'ISAS CORE Inspector: Naplánujte odstávku pre vnútornú inšpekciu nádoby.'
    ))

    # 3. Tlaková skúška
    lehota_tlak_roky = cfg['tlakova_roky']
    vysledky.append(vyhodnot_skusku(
        getattr(nadoba, 'datum_dalsej_tlakovej_skusky', None),
        getattr(nadoba, 'datum_poslednej_tlakovej_skusky', None),
        lehota_tlak_roky,
        'VTZ-TLAK', 'tlakova', 'Tlaková skúška',
        f'🚨 Tlaková skúška pevnosti ({lehota_tlak_roky} r. lehota)',
        f"<strong>🛑 KRITICKÁ SKÚŠKA INTEGRITY (Pevnostná tlaková skúška):</strong><br>Koniec cyklu tlakovej skúšky skúšobným pretlakom PT (lehota: každých {lehota_tlak_roky} r.). Vyžaduje overenie celistvosti autorizovaným revíznym inšpektorom.",
        'ISAS CORE Inspector: Vyžaduje sa certifikovaná tlaková skúška.'
    ))

    # 4. Poistný ventil tlakovej nádoby
    d_pv = to_date(getattr(nadoba, 'datum_dalsej_kontroly_poistneho_ventilu', None))
    if not d_pv and getattr(nadoba, 'datum_kontroly_poistneho_ventilu', None):
        posledna_pv = to_date(nadoba.datum_kontroly_poistneho_ventilu)
        if posledna_pv:
            d_pv = add_calendar_interval(posledna_pv, years=cfg['poistny_ventil_roky'])

    pv_sn = getattr(nadoba, 'poistny_ventil_vyrobne_cislo', None) or "neuvedené"
    if d_pv:
        dni = (d_pv - dnes).days
        termin_str = d_pv.strftime('%d.%m.%Y')
        stav = 'PREPADNUTA' if dni < 0 else ('BLIZI_SA' if dni <= limit_dni else 'PLATNA')
        farba = 'red' if dni < 0 else ('orange' if dni <= limit_dni else 'emerald')
        pv_suffix = '-EXP' if stav == 'PREPADNUTA' else ('-WARN' if stav == 'BLIZI_SA' else '-OK')
        vysledky.append({
            'kod': f'VTZ-PV{pv_suffix}',
            'cache_kod': 'pv',
            'nazov': 'Poistný ventil nádoby',
            'typ_lehoty': '🚨 Kontrola / Výmena poistného ventilu nádoby',
            'termin': d_pv,
            'termin_str': termin_str,
            'dni_do': dni,
            'stav': stav,
            'farba': farba,
            'ikona': '🛡️',
            'poistny_ventil_sn': pv_sn,
            'hodnota_ui': f'Prepadnutá ({abs(dni)} dní)' if dni < 0 else (f'Končí o {dni} dní ({termin_str})' if dni <= limit_dni else f'Platná ({termin_str})'),
            'stav_str': f"JE PREPADNUTÁ o {abs(dni)} dní! ({termin_str})" if dni < 0 else (f"vyprší o {dni} dní ({termin_str})" if dni <= limit_dni else f"Platná do {termin_str}"),
            'vyzaduje_alert': (dni <= limit_dni),
            'je_prepadnuta': (stav == 'PREPADNUTA'),
            'je_bliziaca_sa': (stav == 'BLIZI_SA'),
            'je_chybajuca': False,
            'upozornenie_text': (
                "<strong>🛑 BEZPEČNOSTNÉ RIZIKO (EN ISO 4126):</strong><br>"
                "Vypršala lehota preskúšania poistného ventilu. Poistný ventil chráni nádobu pred deštrukciou a výbuchom."
            ),
            'ai_poznamka': 'ISAS CORE Safety: Ohrozenie bezpečnosti tlakového okruhu.',
            'disclaimer': cfg['disclaimer']
        })
    elif getattr(nadoba, 'poistny_ventil_vyrobne_cislo', None) or getattr(nadoba, 'poistny_ventil_typ', None) or getattr(nadoba, 'poistny_ventil_otvaraci_tlak_bar', None):
        vysledky.append({
            'kod': 'VTZ-PV-CHYBA',
            'cache_kod': 'pv',
            'nazov': 'Poistný ventil nádoby',
            'typ_lehoty': '🚨 Kontrola / Výmena poistného ventilu nádoby',
            'termin': dnes,
            'termin_str': 'Neuvedený',
            'dni_do': -99999,
            'stav': 'CHYBA_KONTROLA',
            'farba': 'red',
            'ikona': '🛡️',
            'poistny_ventil_sn': pv_sn,
            'hodnota_ui': 'Chýba kontrola PV nádoby',
            'stav_str': 'CHÝBA DÁTUM PRESKÚŠANIA POISTNÉHO VENTILU NÁDOBY!',
            'vyzaduje_alert': True,
            'je_prepadnuta': False,
            'je_bliziaca_sa': False,
            'je_chybajuca': True,
            'upozornenie_text': "<strong>🛑 BEZPEČNOSTNÉ RIZIKO:</strong><br>Poistný ventil nádoby nemá zaevidovaný dátum kontroly.",
            'ai_poznamka': 'ISAS CORE Safety: Zaevidujte protokol o preskúšaní poistného ventilu.',
            'disclaimer': cfg['disclaimer']
        })

    # 5. Manometer nádoby (Metrologické overenie prevádzkových meradiel tlaku)
    d_mano = to_date(getattr(nadoba, 'datum_dalsej_kontroly_manometra', None))
    if not d_mano and getattr(nadoba, 'datum_kontroly_manometra', None):
        p_mano = to_date(nadoba.datum_kontroly_manometra)
        if p_mano:
            d_mano = add_calendar_interval(p_mano, years=cfg['manometer_roky'])
    
    mano_sn = getattr(nadoba, 'manometer_vyrobne_cislo', None) or "neuvedené"
    if d_mano:
        dni_m = (d_mano - dnes).days
        t_str_m = d_mano.strftime('%d.%m.%Y')
        stav_m = 'PREPADNUTA' if dni_m < 0 else ('BLIZI_SA' if dni_m <= limit_dni else 'PLATNA')
        farba_m = 'red' if dni_m < 0 else ('orange' if dni_m <= limit_dni else 'emerald')
        vysledky.append({
            'kod': f'VTZ-MAN-{"EXP" if stav_m == "PREPADNUTA" else ("WARN" if stav_m == "BLIZI_SA" else "OK")}',
            'cache_kod': 'manometer',
            'nazov': 'Manometer nádoby',
            'typ_lehoty': f'⏱️ Overenie manometra nádoby ({cfg["manometer_roky"]} r. lehota)',
            'termin': d_mano,
            'termin_str': t_str_m,
            'dni_do': dni_m,
            'stav': stav_m,
            'farba': farba_m,
            'ikona': '⏱️',
            'manometer_sn': mano_sn,
            'hodnota_ui': f'Prepadnutý ({abs(dni_m)} dní)' if dni_m < 0 else (f'Končí o {dni_m} dní ({t_str_m})' if dni_m <= limit_dni else f'Platný do {t_str_m}'),
            'stav_str': f"JE PREPADNUTÝ o {abs(dni_m)} dní! ({t_str_m})" if dni_m < 0 else (f"vyprší o {dni_m} dní ({t_str_m})" if dni_m <= limit_dni else f"Platný do {t_str_m}"),
            'vyzaduje_alert': (dni_m <= limit_dni),
            'je_prepadnuta': (stav_m == 'PREPADNUTA'),
            'je_bliziaca_sa': (stav_m == 'BLIZI_SA'),
            'je_chybajuca': False,
            'upozornenie_text': "<strong>⏱️ METROLÓGIA (Kalibrácia a overenie meradiel tlaku):</strong><br>Vypršala lehota overenia funkčnosti tlakomera nádoby.",
            'ai_poznamka': 'ISAS CORE Metrology: Zabezpečte kontrolu alebo výmenu manometra nádoby.',
            'disclaimer': cfg['disclaimer']
        })
    elif getattr(nadoba, 'manometer_vyrobne_cislo', None):
        vysledky.append({
            'kod': 'VTZ-MAN-CHYBA',
            'cache_kod': 'manometer',
            'nazov': 'Manometer nádoby',
            'typ_lehoty': f'⏱️ Overenie manometra nádoby ({cfg["manometer_roky"]} r. lehota)',
            'termin': dnes,
            'termin_str': 'Neuvedený',
            'dni_do': -99999,
            'stav': 'CHYBA_KONTROLA',
            'farba': 'red',
            'ikona': '⏱️',
            'manometer_sn': mano_sn,
            'hodnota_ui': 'Chýba overenie manometra',
            'stav_str': 'CHÝBA DÁTUM OVERENIA MANOMETRA NÁDOBY!',
            'vyzaduje_alert': True,
            'je_prepadnuta': False,
            'je_bliziaca_sa': False,
            'je_chybajuca': True,
            'upozornenie_text': "<strong>⏱️ METROLÓGIA (Kalibrácia a overenie meradiel tlaku):</strong><br>Manometer nádoby nemá zaevidovaný dátum overenia.",
            'ai_poznamka': 'ISAS CORE Metrology: Zaevidujte overenie alebo výmenu manometra nádoby.',
            'disclaimer': cfg['disclaimer']
        })

    return vysledky


def vyhodnot_kompresor_compliance(kompresor: Any, referencny_datum: Optional[date] = None, limit_dni_alert: Optional[int] = None, cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    Vyhodnotí poistný ventil na separačnej nádobe kompresora s dynamickou lehotou z konfigurácie.
    """
    dnes = to_date(referencny_datum) or timezone.now().date()
    active_cfg = get_compliance_config(iso=get_iso_pre_objekt(kompresor))
    if cfg and isinstance(cfg, dict):
        active_cfg.update(cfg)
    cfg = active_cfg
    vysledky: List[Dict[str, Any]] = []
    d_kontroly = to_date(getattr(kompresor, 'poistny_ventil_datum_kontroly', None))

    pv_sn = getattr(kompresor, 'poistny_ventil_vyrobne_cislo', None) or "neuvedené"
    limit_dni = limit_dni_alert if limit_dni_alert is not None else cfg.get('predstih_dni', 14)
    lehota_pv_roky = int(cfg['poistny_ventil_roky'])

    termin_pv = add_calendar_interval(d_kontroly, years=lehota_pv_roky) if d_kontroly else None
    if termin_pv:
        dni = (termin_pv - dnes).days
        termin_str = termin_pv.strftime('%d.%m.%Y')
        stav = 'PREPADNUTA' if dni < 0 else ('BLIZI_SA' if dni <= limit_dni else 'PLATNA')
        farba = 'red' if dni < 0 else ('orange' if dni <= limit_dni else 'emerald')
        tlak_bar = getattr(kompresor, 'poistny_ventil_tlak_bar', None)
        pv_komp_suffix = '-EXP' if stav == 'PREPADNUTA' else ('-WARN' if stav == 'BLIZI_SA' else '-OK')

        vysledky.append({
            'kod': f'VTZ-PV-KOMP{pv_komp_suffix}',
            'cache_kod': 'pv_komp',
            'nazov': 'Poistný ventil kompresora',
            'typ_lehoty': f'🛡️ Preskúšanie poistného ventilu kompresora ({lehota_pv_roky} r. lehota)',
            'termin': termin_pv,
            'termin_str': termin_str,
            'dni_do': dni,
            'stav': stav,
            'farba': farba,
            'ikona': '🛡️',
            'poistny_ventil_sn': pv_sn,
            'nastaveny_tlak_bar': safe_float(tlak_bar) if tlak_bar is not None else "Nezadaný",
            'hodnota_ui': f'Prepadnutý ({abs(dni)} dní)' if dni < 0 else (f'Končí o {dni} dní ({termin_str})' if dni <= limit_dni else f'Platný do {termin_str}'),
            'stav_str': f"JE PREPADNUTÁ o {abs(dni)} dní! ({termin_str})" if dni < 0 else (f"vyprší o {dni} dní ({termin_str})" if dni <= limit_dni else f"Platná do {termin_str}"),
            'vyzaduje_alert': (dni <= limit_dni),
            'upozornenie_text': (
                f"<strong>🛑 BEZPEČNOSTNÁ POVINNOSŤ (EN ISO 4126):</strong><br>"
                f"Vypršala predpísaná lehota ({lehota_pv_roky} r.) preskúšania poistného ventilu kompresora. "
                f"Poistný ventil chráni separačnú nádobu pred pretlakom a deštrukciou."
            ),
            'je_prepadnuta': (stav == 'PREPADNUTA'),
            'je_bliziaca_sa': (stav == 'BLIZI_SA'),
            'je_chybajuca': False,
            'ai_poznamka': 'ISAS Safety: Naplánujte výmenu alebo preskúšanie poistného ventilu kompresora.',
            'disclaimer': cfg['disclaimer']
        })
    elif getattr(kompresor, 'poistny_ventil_vyrobne_cislo', None) or getattr(kompresor, 'poistny_ventil_tlak_bar', None):
        tlak_raw = getattr(kompresor, 'poistny_ventil_tlak_bar', None)
        vysledky.append({
            'kod': 'VTZ-PV-KOMP-CHYBA',
            'cache_kod': 'pv_komp',
            'nazov': 'Poistný ventil kompresora',
            'typ_lehoty': f'🛡️ Preskúšanie poistného ventilu kompresora ({lehota_pv_roky} r. lehota)',
            'termin': dnes,
            'termin_str': 'Neuvedený',
            'dni_do': -99999,
            'stav': 'CHYBA_KONTROLA',
            'farba': 'red',
            'ikona': '🛡️',
            'nastaveny_tlak_bar': safe_float(tlak_raw) if tlak_raw is not None else "Nezadaný",
            'hodnota_ui': 'Chýba kontrola PV kompresora',
            'stav_str': 'CHÝBA DÁTUM PRESKÚŠANIA POISTNÉHO VENTILU!',
            'vyzaduje_alert': True,
            'je_prepadnuta': False,
            'je_bliziaca_sa': False,
            'je_chybajuca': True,
            'upozornenie_text': "<strong>🛑 BEZPEČNOSTNÁ POVINNOSŤ:</strong><br>Poistný ventil kompresora nemá zaevidovaný dátum kontroly ani výmeny.",
            'ai_poznamka': 'ISAS Safety: Zaevidujte preskúšanie poistného ventilu kompresora.',
            'disclaimer': cfg['disclaimer']
        })

    # 2. Bezpečnostná elektrorevízia stroja (EN 60204-1 / HD 60364)
    d_el = to_date(getattr(kompresor, 'datum_dalsej_elektrorevizie', None))
    if not d_el and getattr(kompresor, 'datum_poslednej_elektrorevizie', None):
        p_el = to_date(kompresor.datum_poslednej_elektrorevizie)
        if p_el:
            d_el = add_calendar_interval(p_el, years=cfg['elektro_roky'])
    
    if d_el:
        dni_e = (d_el - dnes).days
        t_str_e = d_el.strftime('%d.%m.%Y')
        stav_e = 'PREPADNUTA' if dni_e < 0 else ('BLIZI_SA' if dni_e <= limit_dni else 'PLATNA')
        farba_e = 'red' if dni_e < 0 else ('orange' if dni_e <= limit_dni else 'emerald')
        vysledky.append({
            'kod': f'VTZ-ELEKTRO-{"EXP" if stav_e == "PREPADNUTA" else ("WARN" if stav_e == "BLIZI_SA" else "OK")}',
            'cache_kod': 'elektro',
            'nazov': 'Elektrorevízia stroja',
            'typ_lehoty': f'⚡ Bezpečnostná elektrorevízia ({cfg["elektro_roky"]} r. lehota)',
            'termin': d_el,
            'termin_str': t_str_e,
            'dni_do': dni_e,
            'stav': stav_e,
            'farba': farba_e,
            'ikona': '⚡',
            'hodnota_ui': f'Prepadnutá ({abs(dni_e)} dní)' if dni_e < 0 else (f'Končí o {dni_e} dní ({t_str_e})' if dni_e <= limit_dni else f'Platná do {t_str_e}'),
            'stav_str': f"JE PREPADNUTÁ o {abs(dni_e)} dní! ({t_str_e})" if dni_e < 0 else (f"vyprší o {dni_e} dní ({t_str_e})" if dni_e <= limit_dni else f"Platná do {t_str_e}"),
            'vyzaduje_alert': (dni_e <= limit_dni),
            'je_prepadnuta': (stav_e == 'PREPADNUTA'),
            'je_bliziaca_sa': (stav_e == 'BLIZI_SA'),
            'je_chybajuca': False,
            'upozornenie_text': "<strong>⚡ ELEKTROBEZPEČNOSŤ (Pravidelná inšpekcia elektrických zariadení):</strong><br>Vypršala lehota odbornej prehliadky a skúšky elektroinštalácie kompresora.",
            'ai_poznamka': 'ISAS CORE Safety: Naplánujte výkon elektrorevízie certifikovaným elektrotechnickým technikom.',
            'disclaimer': cfg['disclaimer']
        })

    # 3. Bezpečnostná výmena tlakových hadíc (EN ISO 4414)
    d_had = to_date(getattr(kompresor, 'datum_vymeny_tlakovych_hadic', None))
    termin_had = add_calendar_interval(d_had, years=cfg['hadice_roky']) if d_had else None
    if termin_had:
        dni_h = (termin_had - dnes).days
        t_str_h = termin_had.strftime('%d.%m.%Y')
        stav_h = 'PREPADNUTA' if dni_h < 0 else ('BLIZI_SA' if dni_h <= limit_dni else 'PLATNA')
        farba_h = 'red' if dni_h < 0 else ('orange' if dni_h <= limit_dni else 'emerald')
        vysledky.append({
            'kod': f'TECH-HAD-{"EXP" if stav_h == "PREPADNUTA" else ("WARN" if stav_h == "BLIZI_SA" else "OK")}',
            'cache_kod': 'hadice',
            'nazov': 'Tlakové hadice',
            'typ_lehoty': f'🛡️ Bezpečnostná výmena hadíc ({cfg["hadice_roky"]} r. lehota)',
            'termin': termin_had,
            'termin_str': t_str_h,
            'dni_do': dni_h,
            'stav': stav_h,
            'farba': farba_h,
            'ikona': '🛡️',
            'hodnota_ui': f'Expirované ({abs(dni_h)} dní)' if dni_h < 0 else (f'Výmena o {dni_h} dní ({t_str_h})' if dni_h <= limit_dni else f'Platné do {t_str_h}'),
            'stav_str': f"EXPIRÁCIA HADÍC o {abs(dni_h)} dní! ({t_str_h})" if dni_h < 0 else (f"výmena o {dni_h} dní ({t_str_h})" if dni_h <= limit_dni else f"Platné do {t_str_h}"),
            'vyzaduje_alert': (dni_h <= limit_dni),
            'je_prepadnuta': (stav_h == 'PREPADNUTA'),
            'je_bliziaca_sa': (stav_h == 'BLIZI_SA'),
            'je_chybajuca': False,
            'upozornenie_text': "<strong>🛑 BEZPEČNOSTNÁ ŽIVOTNOSŤ (EN ISO 4414):</strong><br>Vypršala odporúčaná životnosť vysokotlakových hadíc. Hrozí roztrhnutie a únik horúceho oleja.",
            'ai_poznamka': 'ISAS Maintenance: Naplánujte kompletnú výmenu sady tlakových hadíc.',
            'disclaimer': cfg['disclaimer']
        })

    return vysledky


def get_zariadenie_compliance_audit(zariadenie: Any, referencny_datum: Optional[date] = None, limit_dni_alert: Optional[int] = None, cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    Kompletný audit vyhradených a environmentálnych kontrol pre ľubovoľný stroj z odvetvia kompresorov.
    Združuje VTZ nádob, poistné ventily kompresorov aj F-plyny sušičiek, chladičov a klimatizácií.
    Plne rešpektuje globálne vypnutie sledovania legislatívy v systéme.
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

    # 1. Tlaková nádoba (iba pre kategóriu tlakova_nadoba)
    if kat == 'tlakova_nadoba':
        p_nadoba = safe_get_param(zariadenie, 'parametre_nadoba')
        if p_nadoba:
            polozky.extend(vyhodnot_tlakova_nadoba_compliance(p_nadoba, dnes, limit_dni, cfg=cfg))

    # 2. Kompresor (iba pre kategóriu kompresor)
    elif kat == 'kompresor':
        p_komp = safe_get_param(zariadenie, 'parametre_kompresor')
        if p_komp:
            polozky.extend(vyhodnot_kompresor_compliance(p_komp, dnes, limit_dni, cfg=cfg))

    # 3. Chladiace zariadenia (F-plyny, Desikant sušičiek, Elektrorevízie)
    elif kat in ('susic', 'chladic', 'klimatizacia'):
        kat_map = {'susic': 'parametre_susicka', 'chladic': 'parametre_chladic', 'klimatizacia': 'parametre_klimatizacia'}
        attr = kat_map.get(kat)
        p_chlad = safe_get_param(zariadenie, attr) if attr else None
        if p_chlad:
            res = vyhodnot_fplyny_compliance(
                getattr(p_chlad, 'chladivo', None),
                getattr(p_chlad, 'mnozstvo_chladiva_kg', None),
                getattr(p_chlad, 'datum_kontroly_fplynov', None),
                getattr(p_chlad, 'ekvivalent_co2_t', None),
                dnes, limit_dni, cfg=cfg,
                ma_detekcny_system=getattr(p_chlad, 'ma_detekcny_system_unikov', False)
            )
            if res:
                polozky.append(res)
            
            # Kontrola sušiva pre adsorpčné sušičky
            if kat == 'susic' and getattr(p_chlad, 'datum_vymeny_desikantu', None):
                d_des = add_calendar_interval(to_date(p_chlad.datum_vymeny_desikantu), years=cfg['desikant_roky'])
                if d_des:
                    dni_d = (d_des - dnes).days
                    t_str_d = d_des.strftime('%d.%m.%Y')
                    stav_d = 'PREPADNUTA' if dni_d < 0 else ('BLIZI_SA' if dni_d <= limit_dni else 'PLATNA')
                    polozky.append({
                        'kod': f'TECH-DES-{"EXP" if stav_d == "PREPADNUTA" else ("WARN" if stav_d == "BLIZI_SA" else "OK")}',
                        'cache_kod': 'desikant', 'nazov': 'Sušivo sušičky', 'typ_lehoty': f'🏜️ Výmena sušiva ({cfg["desikant_roky"]} r. lehota)',
                        'termin': d_des, 'termin_str': t_str_d, 'dni_do': dni_d, 'stav': stav_d,
                        'farba': 'red' if dni_d < 0 else ('orange' if dni_d <= limit_dni else 'emerald'),
                        'ikona': '🏜️', 'hodnota_ui': f'Expirované ({abs(dni_d)} dní)' if dni_d < 0 else (f'Výmena o {dni_d} dní ({t_str_d})' if dni_d <= limit_dni else f'Platné do {t_str_d}'),
                        'stav_str': f"VÝMENA SUŠIVA o {abs(dni_d)} dní! ({t_str_d})" if dni_d < 0 else f"Výmena o {dni_d} dní",
                        'vyzaduje_alert': (dni_d <= limit_dni), 'je_prepadnuta': (stav_d == 'PREPADNUTA'), 'je_bliziaca_sa': (stav_d == 'BLIZI_SA'),
                        'je_chybajuca': False, 'upozornenie_text': "Vypršala odporúčaná životnosť adsorpčného sušiva. Hrozí vlhkosť v sieti.",
                        'ai_poznamka': 'ISAS Maintenance: Zabezpečte presyp a výmenu desikantu.', 'disclaimer': cfg['disclaimer']
                    })

            # ⚡ Bezpečnostná elektrorevízia pre priemyselné chladiče (chillery)
            if kat == 'chladic' and (getattr(p_chlad, 'datum_dalsej_elektrorevizie', None) or getattr(p_chlad, 'datum_poslednej_elektrorevizie', None)):
                d_el = to_date(getattr(p_chlad, 'datum_dalsej_elektrorevizie', None))
                if not d_el and getattr(p_chlad, 'datum_poslednej_elektrorevizie', None):
                    p_el = to_date(p_chlad.datum_poslednej_elektrorevizie)
                    if p_el:
                        d_el = add_calendar_interval(p_el, years=cfg['elektro_roky'])
                if d_el:
                    dni_e = (d_el - dnes).days
                    t_str_e = d_el.strftime('%d.%m.%Y')
                    stav_e = 'PREPADNUTA' if dni_e < 0 else ('BLIZI_SA' if dni_e <= limit_dni else 'PLATNA')
                    farba_e = 'red' if dni_e < 0 else ('orange' if dni_e <= limit_dni else 'emerald')
                    polozky.append({
                        'kod': f'VTZ-ELEKTRO-{"EXP" if stav_e == "PREPADNUTA" else ("WARN" if stav_e == "BLIZI_SA" else "OK")}',
                        'cache_kod': 'elektro',
                        'nazov': 'Elektrorevízia chladiča',
                        'typ_lehoty': f'⚡ Bezpečnostná elektrorevízia ({cfg["elektro_roky"]} r. lehota)',
                        'termin': d_el,
                        'termin_str': t_str_e,
                        'dni_do': dni_e,
                        'stav': stav_e,
                        'farba': farba_e,
                        'ikona': '⚡',
                        'hodnota_ui': f'Prepadnutá ({abs(dni_e)} dní)' if dni_e < 0 else (f'Končí o {dni_e} dní ({t_str_e})' if dni_e <= limit_dni else f'Platná do {t_str_e}'),
                        'stav_str': f"JE PREPADNUTÁ o {abs(dni_e)} dní! ({t_str_e})" if dni_e < 0 else (f"vyprší o {dni_e} dní ({t_str_e})" if dni_e <= limit_dni else f"Platná do {t_str_e}"),
                        'vyzaduje_alert': (dni_e <= limit_dni),
                        'je_prepadnuta': (stav_e == 'PREPADNUTA'),
                        'je_bliziaca_sa': (stav_e == 'BLIZI_SA'),
                        'je_chybajuca': False,
                        'upozornenie_text': "<strong>⚡ ELEKTROBEZPEČNOSŤ (Pravidelná revízia elektrických zariadení):</strong><br>Vypršala lehota odbornej prehliadky a skúšky elektroinštalácie chladiča.",
                        'ai_poznamka': 'ISAS CORE Safety: Naplánujte výkon elektrorevízie chladiča certifikovaným elektrotechnickým technikom.',
                        'disclaimer': cfg['disclaimer']
                    })

    # 4. Odlučovače oleja a vody (Ochrana vôd a životného prostredia)
    elif kat == 'odlucovac':
        p_odl = safe_get_param(zariadenie, 'parametre_odlucovac')
        if p_odl:
            d_odl = to_date(getattr(p_odl, 'datum_dalsej_vymeny_vloziek', None))
            if not d_odl and getattr(p_odl, 'datum_poslednej_vymeny_vloziek', None):
                d_odl = add_calendar_interval(to_date(p_odl.datum_poslednej_vymeny_vloziek), years=cfg['odlucovac_filtre_roky'])
            if d_odl:
                dni_o = (d_odl - dnes).days
                t_str_o = d_odl.strftime('%d.%m.%Y')
                stav_o = 'PREPADNUTA' if dni_o < 0 else ('BLIZI_SA' if dni_o <= limit_dni else 'PLATNA')
                polozky.append({
                    'kod': f'ENV-WOS-{"EXP" if stav_o == "PREPADNUTA" else ("WARN" if stav_o == "BLIZI_SA" else "OK")}',
                    'cache_kod': 'odlucovac', 'nazov': 'Filtre odlučovača', 'typ_lehoty': f'🚰 Výmena filtrov separátora ({cfg["odlucovac_filtre_roky"]} r. lehota)',
                    'termin': d_odl, 'termin_str': t_str_o, 'dni_do': dni_o, 'stav': stav_o,
                    'farba': 'red' if dni_o < 0 else ('orange' if dni_o <= limit_dni else 'emerald'),
                    'ikona': '🚰', 'hodnota_ui': f'Expirované ({abs(dni_o)} dní)' if dni_o < 0 else (f'Výmena o {dni_o} dní' if dni_o <= limit_dni else f'Platné do {t_str_o}'),
                    'stav_str': f"VÝMENA FILTROV SEPARÁTORA o {abs(dni_o)} dní! ({t_str_o})" if dni_o < 0 else (f"výmena o {dni_o} dní ({t_str_o})" if dni_o <= limit_dni else f"Platné do {t_str_o}"),
                    'vyzaduje_alert': (dni_o <= limit_dni), 'je_prepadnuta': (stav_o == 'PREPADNUTA'), 'je_bliziaca_sa': (stav_o == 'BLIZI_SA'),
                    'je_chybajuca': False, 'upozornenie_text': "Vypršala lehota výmeny vložiek odlučovača. Hrozí únik ropných látok do kanalizácie.",
                    'ai_poznamka': 'ISAS Enviro: Vymeňte servisnú sadu filtrov separátora kondenzátu.', 'disclaimer': cfg['disclaimer']
                })

    # 5. Filtračné stanice stlačeného vzduchu
    elif kat == 'filtracia':
        p_filt = safe_get_param(zariadenie, 'parametre_filtracia')
        if p_filt:
            d_filt = to_date(getattr(p_filt, 'datum_dalsej_vymeny_vloziek', None))
            if not d_filt and getattr(p_filt, 'datum_poslednej_vymeny_vloziek', None):
                d_filt = add_calendar_interval(to_date(p_filt.datum_poslednej_vymeny_vloziek), years=cfg['filter_vlozky_roky'])
            if d_filt:
                dni_f = (d_filt - dnes).days
                t_str_f = d_filt.strftime('%d.%m.%Y')
                stav_f = 'PREPADNUTA' if dni_f < 0 else ('BLIZI_SA' if dni_f <= limit_dni else 'PLATNA')
                polozky.append({
                    'kod': f'TECH-FILT-{"EXP" if stav_f == "PREPADNUTA" else ("WARN" if stav_f == "BLIZI_SA" else "OK")}',
                    'cache_kod': 'filtracia', 'nazov': 'Filtračná vložka', 'typ_lehoty': f'🌪️ Výmena filtračnej vložky ({cfg["filter_vlozky_roky"]} r. lehota)',
                    'termin': d_filt, 'termin_str': t_str_f, 'dni_do': dni_f, 'stav': stav_f,
                    'farba': 'red' if dni_f < 0 else ('orange' if dni_f <= limit_dni else 'emerald'),
                    'ikona': '🌪️', 'hodnota_ui': f'Expirovaná ({abs(dni_f)} dní)' if dni_f < 0 else (f'Výmena o {dni_f} dní' if dni_f <= limit_dni else f'Platná do {t_str_f}'),
                    'stav_str': f"VÝMENA VLOŽKY FILTRA o {abs(dni_f)} dní! ({t_str_f})" if dni_f < 0 else (f"výmena o {dni_f} dní ({t_str_f})" if dni_f <= limit_dni else f"Platné do {t_str_f}"),
                    'vyzaduje_alert': (dni_f <= limit_dni), 'je_prepadnuta': (stav_f == 'PREPADNUTA'), 'je_bliziaca_sa': (stav_f == 'BLIZI_SA'),
                    'je_chybajuca': False, 'upozornenie_text': "Vypršala životnosť filtračného elementu stlačeného vzduchu.",
                    'ai_poznamka': 'ISAS Maintenance: Objednajte a vymeňte filtračnú vložku.', 'disclaimer': cfg['disclaimer']
                })

    # 6. Generátory plynu (O2 Senzor)
    elif kat == 'generator_plynu':
        p_gen = safe_get_param(zariadenie, 'parametre_generator')
        if p_gen:
            d_gen = to_date(getattr(p_gen, 'datum_dalsej_kalibracie_o2', None))
            if not d_gen and getattr(p_gen, 'datum_poslednej_kalibracie_o2', None):
                d_gen = add_calendar_interval(to_date(p_gen.datum_poslednej_kalibracie_o2), years=cfg['kalibracia_o2_roky'])
            if d_gen:
                dni_g = (d_gen - dnes).days
                t_str_g = d_gen.strftime('%d.%m.%Y')
                stav_g = 'PREPADNUTA' if dni_g < 0 else ('BLIZI_SA' if dni_g <= limit_dni else 'PLATNA')
                polozky.append({
                    'kod': f'TECH-O2-{"EXP" if stav_g == "PREPADNUTA" else ("WARN" if stav_g == "BLIZI_SA" else "OK")}',
                    'cache_kod': 'generator_o2', 'nazov': 'Kalibrácia O₂ sondy', 'typ_lehoty': f'✨ Kalibrácia O₂ senzora ({cfg["kalibracia_o2_roky"]} r. lehota)',
                    'termin': d_gen, 'termin_str': t_str_g, 'dni_do': dni_g, 'stav': stav_g,
                    'farba': 'red' if dni_g < 0 else ('orange' if dni_g <= limit_dni else 'emerald'),
                    'ikona': '✨', 'hodnota_ui': f'Prepadnutá ({abs(dni_g)} dní)' if dni_g < 0 else (f'Kalibrácia o {dni_g} dní' if dni_g <= limit_dni else f'Platná do {t_str_g}'),
                    'stav_str': f"KALIBRÁCIA O₂ SENZORA o {abs(dni_g)} dní! ({t_str_g})" if dni_g < 0 else (f"kalibrácia o {dni_g} dní ({t_str_g})" if dni_g <= limit_dni else f"Platné do {t_str_g}"),
                    'vyzaduje_alert': (dni_g <= limit_dni), 'je_prepadnuta': (stav_g == 'PREPADNUTA'), 'je_bliziaca_sa': (stav_g == 'BLIZI_SA'),
                    'je_chybajuca': False, 'upozornenie_text': "Vypršala platnosť kalibrácie merania čistoty plynu.",
                    'ai_poznamka': 'ISAS Metrology: Zabezpečte kalibráciu analyzátora O₂.', 'disclaimer': cfg['disclaimer']
                })

    return polozky


def get_global_compliance_queue(
    referencny_datum: Optional[date] = None,
    limit_dni_alert: Optional[int] = None,
    filter_alerts_only: bool = True
) -> List[Dict[str, Any]]:
    """
    🚀 ISAS ENTERPRISE COMPLIANCE QUEUE – ODVETVIE: KOMPRESORY
    Jednotné a optimalizované systémové rozhranie pre zber všetkých zákonných revízií.
    Garantuje 0 N+1 dopytov a rešpektuje globálne nastavenie 'servisy_sledovat_legislativu'.
    """
    cfg = get_compliance_config()
    if not cfg.get('sledovat_legislativu', True):
        return []

    dnes = to_date(referencny_datum) or timezone.now().date()
    limit_dni = limit_dni_alert if limit_dni_alert is not None else cfg.get('predstih_dni', 14)
    queue: List[Dict[str, Any]] = []

    # 1. Tlakové nádoby (S overením aktuálnej kategórie stroja a multi-country lehôt)
    try:
        nadoby_mgr: Any = ParametreTlakovaNadoba.objects
        nadoby_qs = nadoby_mgr.select_related(
            'zariadenie', 'zariadenie__zakaznik'
        ).filter(zariadenie__v_servisnom_rezime=False, zariadenie__kategoria='tlakova_nadoba')
        for n in nadoby_qs:
            zar = getattr(n, 'zariadenie', None)
            if not zar:
                continue
            stroj_cfg = get_compliance_config(iso=get_iso_pre_objekt(zar))
            for item in vyhodnot_tlakova_nadoba_compliance(n, referencny_datum=dnes, limit_dni_alert=limit_dni, cfg=stroj_cfg):
                if not filter_alerts_only or item.get('vyzaduje_alert'):
                    item['zariadenie'] = zar
                    item['stroj_kategoria'] = 'tlaková nádoba'
                    queue.append(item)
    except Exception:
        pass

    # 2. Poistné ventily kompresorov (S overením aktuálnej kategórie stroja)
    try:
        komp_mgr: Any = ParametreKompresor.objects
        komp_qs = komp_mgr.select_related(
            'zariadenie', 'zariadenie__zakaznik'
        ).filter(zariadenie__v_servisnom_rezime=False, zariadenie__kategoria='kompresor')
        for pk in komp_qs:
            zar = getattr(pk, 'zariadenie', None)
            if not zar:
                continue
            stroj_cfg = get_compliance_config(iso=get_iso_pre_objekt(zar))
            for item in vyhodnot_kompresor_compliance(pk, referencny_datum=dnes, limit_dni_alert=limit_dni, cfg=stroj_cfg):
                if not filter_alerts_only or item.get('vyzaduje_alert'):
                    item['zariadenie'] = zar
                    item['stroj_kategoria'] = 'kompresor'
                    queue.append(item)
    except Exception as exc:
        logger.warning("ISAS COMPLIANCE: Chyba pri vyhodnocovaní poistných ventilov kompresorov: %s", exc)

    # 3. Chladiace zariadenia a Sušičky (F-plyny + Desikant bez N+1)
    chladiace_konfiguracie = [
        (ParametreSusicka, 'sušička vzduchu', 'susic'),
        (ParametreChladic, 'chladič kvapaliny', 'chladic'),
        (ParametreKlimatizacia, 'klimatizácia', 'klimatizacia'),
    ]
    for PModel, label, kat_kod in chladiace_konfiguracie:
        try:
            p_chlad_mgr: Any = PModel.objects
            for p_chlad in p_chlad_mgr.select_related(
                'zariadenie', 'zariadenie__zakaznik'
            ).filter(zariadenie__v_servisnom_rezime=False, zariadenie__kategoria=kat_kod):
                zar = getattr(p_chlad, 'zariadenie', None)
                if not zar:
                    continue
                stroj_cfg = get_compliance_config(iso=get_iso_pre_objekt(zar))
                
                # F-plyny
                f_item = vyhodnot_fplyny_compliance(
                    getattr(p_chlad, 'chladivo', None),
                    getattr(p_chlad, 'mnozstvo_chladiva_kg', None),
                    getattr(p_chlad, 'datum_kontroly_fplynov', None),
                    getattr(p_chlad, 'ekvivalent_co2_t', None),
                    referencny_datum=dnes,
                    limit_dni_alert=limit_dni,
                    cfg=stroj_cfg,
                    ma_detekcny_system=getattr(p_chlad, 'ma_detekcny_system_unikov', False)
                )
                if f_item and (not filter_alerts_only or f_item.get('vyzaduje_alert')):
                    f_item['zariadenie'] = zar
                    f_item['stroj_kategoria'] = label
                    queue.append(f_item)

                # Desikant pre sušičky zaradený priamo do globálnej fronty
                d_des_raw = getattr(p_chlad, 'datum_vymeny_desikantu', None)
                if kat_kod == 'susic' and d_des_raw:
                    d_des = add_calendar_interval(to_date(d_des_raw), years=int(stroj_cfg['desikant_roky']))
                    if d_des:
                        dni_d = (d_des - dnes).days
                        if not filter_alerts_only or dni_d <= limit_dni:
                            t_str_d = d_des.strftime('%d.%m.%Y')
                            stav_d = 'PREPADNUTA' if dni_d < 0 else ('BLIZI_SA' if dni_d <= limit_dni else 'PLATNA')
                            queue.append({
                                'kod': f'TECH-DES-{"EXP" if stav_d == "PREPADNUTA" else ("WARN" if stav_d == "BLIZI_SA" else "OK")}',
                                'cache_kod': 'desikant', 'nazov': 'Sušivo sušičky', 'typ_lehoty': f'🏜️ Výmena sušiva ({stroj_cfg["desikant_roky"]} r. lehota)',
                                'termin': d_des, 'termin_str': t_str_d, 'dni_do': dni_d, 'stav': stav_d,
                                'farba': 'red' if dni_d < 0 else ('orange' if dni_d <= limit_dni else 'emerald'),
                                'ikona': '🏜️', 'hodnota_ui': f'Expirované ({abs(dni_d)} dní)' if dni_d < 0 else f'Výmena o {dni_d} dní ({t_str_d})',
                                'stav_str': f"VÝMENA SUŠIVA o {abs(dni_d)} dní! ({t_str_d})" if dni_d < 0 else f"Výmena o {dni_d} dní",
                                'vyzaduje_alert': (dni_d <= limit_dni), 'je_prepadnuta': (stav_d == 'PREPADNUTA'), 'je_bliziaca_sa': (stav_d == 'BLIZI_SA'),
                                'je_chybajuca': False, 'upozornenie_text': "Vypršala odporúčaná životnosť adsorpčného sušiva. Hrozí vlhkosť v sieti.",
                                'ai_poznamka': 'ISAS Maintenance: Zabezpečte presyp a výmenu desikantu.', 'disclaimer': stroj_cfg.get('disclaimer', DEFAULT_LEGAL_DISCLAIMER),
                                'zariadenie': zar, 'stroj_kategoria': label
                            })

                # Bezpečnostná elektrorevízia pre chladiče (chillery)
                d_el_next_raw = getattr(p_chlad, 'datum_dalsej_elektrorevizie', None)
                d_el_last_raw = getattr(p_chlad, 'datum_poslednej_elektrorevizie', None)
                if kat_kod == 'chladic' and (d_el_next_raw or d_el_last_raw):
                    d_el = to_date(d_el_next_raw)
                    if not d_el and d_el_last_raw:
                        p_el = to_date(d_el_last_raw)
                        if p_el:
                            d_el = add_calendar_interval(p_el, years=int(stroj_cfg['elektro_roky']))
                    if d_el:
                        dni_e = (d_el - dnes).days
                        if not filter_alerts_only or dni_e <= limit_dni:
                            t_str_e = d_el.strftime('%d.%m.%Y')
                            stav_e = 'PREPADNUTA' if dni_e < 0 else ('BLIZI_SA' if dni_e <= limit_dni else 'PLATNA')
                            queue.append({
                                'kod': f'VTZ-ELEKTRO-{"EXP" if stav_e == "PREPADNUTA" else ("WARN" if stav_e == "BLIZI_SA" else "OK")}',
                                'cache_kod': 'elektro',
                                'nazov': 'Elektrorevízia chladiča',
                                'typ_lehoty': f'⚡ Bezpečnostná elektrorevízia ({stroj_cfg["elektro_roky"]} r. lehota)',
                                'termin': d_el,
                                'termin_str': t_str_e,
                                'dni_do': dni_e,
                                'stav': stav_e,
                                'farba': 'red' if dni_e < 0 else ('orange' if dni_e <= limit_dni else 'emerald'),
                                'ikona': '⚡',
                                'hodnota_ui': f'Prepadnutá ({abs(dni_e)} dní)' if dni_e < 0 else (f'Končí o {dni_e} dní ({t_str_e})' if dni_e <= limit_dni else f'Platná do {t_str_e}'),
                                'stav_str': f"JE PREPADNUTÁ o {abs(dni_e)} dní! ({t_str_e})" if dni_e < 0 else (f"vyprší o {dni_e} dní ({t_str_e})" if dni_e <= limit_dni else f"Platná do {t_str_e}"),
                                'vyzaduje_alert': (dni_e <= limit_dni),
                                'je_prepadnuta': (stav_e == 'PREPADNUTA'),
                                'je_bliziaca_sa': (stav_e == 'BLIZI_SA'),
                                'je_chybajuca': False,
                                'upozornenie_text': "<strong>⚡ ELEKTROBEZPEČNOSŤ (Pravidelná revízia elektrických zariadení):</strong><br>Vypršala lehota odbornej prehliadky a skúšky elektroinštalácie priemyselného chladiča.",
                                'ai_poznamka': 'ISAS CORE Safety: Naplánujte výkon elektrorevízie chladiča certifikovaným elektrotechnickým technikom.',
                                'disclaimer': stroj_cfg.get('disclaimer', DEFAULT_LEGAL_DISCLAIMER),
                                'zariadenie': zar,
                                'stroj_kategoria': label
                            })
        except Exception as exc:
            logger.warning("ISAS COMPLIANCE: Chyba pri chladiacich zariadeniach (%s): %s", label, exc)

    # 4. Odlučovače, Filtrácie a Generátory (Zero N+1 priama evaluácia s multi-country lokalizáciou)
    dalsie_konfiguracie = [
        (ParametreOdlucovac, 'odlučovač kondenzátu', 'odlucovac', 'datum_dalsej_vymeny_vloziek', 'datum_poslednej_vymeny_vloziek', 'odlucovac_filtre_roky', 'ENV-WOS', '🚰', 'Filtre odlučovača', 'Výmena filtrov separátora', 'Vypršala lehota výmeny vložiek odlučovača. Hrozí únik ropných látok.', 'ISAS Enviro: Vymeňte servisnú sadu filtrov separátora.'),
        (ParametreFiltracia, 'filtrácia vzduchu', 'filtracia', 'datum_dalsej_vymeny_vloziek', 'datum_poslednej_vymeny_vloziek', 'filter_vlozky_roky', 'TECH-FILT', '🌪️', 'Filtračná vložka', 'Výmena filtračnej vložky', 'Vypršala životnosť filtračného elementu.', 'ISAS Maintenance: Vymeňte filtračnú vložku.'),
        (ParametreGeneratorPlynu, 'generátor plynu', 'generator_plynu', 'datum_dalsej_kalibracie_o2', 'datum_poslednej_kalibracie_o2', 'kalibracia_o2_roky', 'TECH-O2', '✨', 'Kalibrácia O₂ sondy', 'Kalibrácia O₂ senzora', 'Vypršala platnosť kalibrácie merania čistoty plynu.', 'ISAS Metrology: Zabezpečte kalibráciu analyzátora O₂.')
    ]
    for ModelK, k_label, k_slug, attr_next, attr_last, cfg_key, prefix, ikona, nazov, typ_leh, txt_warn, ai_txt in dalsie_konfiguracie:
        try:
            model_k_mgr: Any = ModelK.objects
            for p_obj in model_k_mgr.select_related('zariadenie', 'zariadenie__zakaznik').filter(
                zariadenie__v_servisnom_rezime=False, zariadenie__kategoria=k_slug
            ):
                zar = getattr(p_obj, 'zariadenie', None)
                if not zar:
                    continue
                stroj_cfg = get_compliance_config(iso=get_iso_pre_objekt(zar))
                interval_roky = int(stroj_cfg[cfg_key])
                d_term = to_date(getattr(p_obj, attr_next, None))
                if not d_term:
                    d_last = getattr(p_obj, attr_last, None)
                    if d_last:
                        d_term = add_calendar_interval(to_date(d_last), years=interval_roky)
                if not d_term:
                    continue
                dni_diff = (d_term - dnes).days
                if not filter_alerts_only or dni_diff <= limit_dni:
                    t_str = d_term.strftime('%d.%m.%Y')
                    stav_str_kod = 'PREPADNUTA' if dni_diff < 0 else ('BLIZI_SA' if dni_diff <= limit_dni else 'PLATNA')
                    queue.append({
                        'kod': f'{prefix}-{"EXP" if stav_str_kod == "PREPADNUTA" else ("WARN" if stav_str_kod == "BLIZI_SA" else "OK")}',
                        'cache_kod': k_slug,
                        'nazov': nazov,
                        'typ_lehoty': f'{ikona} {typ_leh} ({interval_roky} r. lehota)',
                        'termin': d_term,
                        'termin_str': t_str,
                        'dni_do': dni_diff,
                        'stav': stav_str_kod,
                        'farba': 'red' if dni_diff < 0 else ('orange' if dni_diff <= limit_dni else 'emerald'),
                        'ikona': ikona,
                        'hodnota_ui': f'Expirované ({abs(dni_diff)} dní)' if dni_diff < 0 else f'Termín o {dni_diff} dní ({t_str})',
                        'stav_str': f"EXPIRÁCIA o {abs(dni_diff)} dní! ({t_str})" if dni_diff < 0 else f"Termín o {dni_diff} dní",
                        'vyzaduje_alert': (dni_diff <= limit_dni),
                        'je_prepadnuta': (stav_str_kod == 'PREPADNUTA'),
                        'je_bliziaca_sa': (stav_str_kod == 'BLIZI_SA'),
                        'je_chybajuca': False,
                        'upozornenie_text': txt_warn,
                        'ai_poznamka': ai_txt,
                        'disclaimer': stroj_cfg.get('disclaimer', DEFAULT_LEGAL_DISCLAIMER),
                        'zariadenie': zar,
                        'stroj_kategoria': k_label
                    })
        except Exception as exc:
            logger.warning("ISAS COMPLIANCE: Chyba pri vyhodnocovaní %s: %s", k_label, exc)

    queue.sort(key=lambda x: int(x['dni_do']) if isinstance(x.get('dni_do'), (int, float)) else 99999)
    return queue


def vyhodnot_iot_telemetriu(zariadenie: Any, analogove_hodnoty: dict, digitalne_stavy: Optional[dict] = None) -> Optional[Dict[str, Any]]:
    """
    Vyhodnocuje prichádzajúci IoT paket (analógové a digitálne dáta) voči limitom technológie kompresorov/čerpadiel/dúchadiel/vývev.
    Vracia dict pre vytvorenie AlarmLog, alebo None ak je všetko v poriadku.
    """
    from Evidencia.industry_loader import get_zariadenie_parametre
    parametre = get_zariadenie_parametre(zariadenie)

    if not parametre or not hasattr(parametre, 'max_vibracie_rms_mm_s'):
        return None

    rms_val = analogove_hodnoty.get('Vibracie_RMS', analogove_hodnoty.get('vibracie_rms'))
    bpfo_val = analogove_hodnoty.get('Vibracie_BPFO', analogove_hodnoty.get('vibracie_bpfo'))

    alarm_dovod = None

    if rms_val is not None and getattr(parametre, 'max_vibracie_rms_mm_s', None):
        try:
            limit_rms = float(str(parametre.max_vibracie_rms_mm_s).replace(',', '.'))
            if float(str(rms_val).replace(',', '.')) > limit_rms:
                alarm_dovod = f"Kritické celkové vibrácie stroja: {round(float(str(rms_val).replace(',', '.')), 2)} mm/s (Limit: {limit_rms})"
        except (ValueError, TypeError):
            pass

    if not alarm_dovod and bpfo_val is not None and getattr(parametre, 'max_vibracie_bpfo_g', None):
        try:
            limit_bpfo = float(str(parametre.max_vibracie_bpfo_g).replace(',', '.'))
            if float(str(bpfo_val).replace(',', '.')) > limit_bpfo:
                alarm_dovod = f"Detegované poškodenie vonkajšej dráhy ložiska (BPFO): {round(float(str(bpfo_val).replace(',', '.')), 2)} g (Limit: {limit_bpfo})"
        except (ValueError, TypeError):
            pass

    if alarm_dovod:
        return {
            'zavaznost': 'CRITICAL',
            'kod_chyby': 'VIB_FAULT_10816',
            'popis': alarm_dovod,
        }
    return None


def get_zariadenie_live_diagnostika(zariadenie: Any, posledny_zaznam: Any = None) -> List[Dict[str, Any]]:
    """
    Vyhodnocuje live diagnostiku telemetrie pre dashboard (ISO 10816 vibrodiagnostika).
    Vracia zoznam diagnostických kariet vrátane prípadných kódov alarmov.
    """
    if not posledny_zaznam:
        return []

    from Evidencia.industry_loader import get_zariadenie_parametre
    parametre = get_zariadenie_parametre(zariadenie)

    if not parametre or not hasattr(parametre, 'max_vibracie_rms_mm_s') or not parametre.max_vibracie_rms_mm_s:
        return []

    try:
        limit_rms = float(str(parametre.max_vibracie_rms_mm_s).replace(',', '.'))
    except (ValueError, TypeError):
        limit_rms = 0.0

    if limit_rms <= 0:
        return []

    live_rms = None
    for k, v in (posledny_zaznam.analogove_hodnoty or {}).items():
        if 'vibracie' in k.lower() and 'rms' in k.lower():
            try:
                live_rms = float(str(v).replace(',', '.'))
                break
            except (ValueError, TypeError):
                continue

    if live_rms is None:
        return []

    if live_rms >= (limit_rms * 2.5):
        return [{
            'nazov': 'Vibrácie (ISO 10816)',
            'hodnota': f'Zóna D - Kritické ({live_rms} mm/s)',
            'farba': 'red',
            'ikona': '📉',
            'alarm_kod': 'ISO-VIB-D',
            'alarm_popis': f'Kritické vibrácie stroja! Namerané {live_rms} mm/s (Limit: {limit_rms}). Hrozí zadretie.',
            'alarm_zavaznost': 'CRITICAL'
        }]
    elif live_rms >= limit_rms:
        return [{
            'nazov': 'Vibrácie (ISO 10816)',
            'hodnota': f'Zóna C - Varovanie ({live_rms} mm/s)',
            'farba': 'orange',
            'ikona': '📉',
            'alarm_kod': 'ISO-VIB-C',
            'alarm_popis': f'Zvýšené vibrácie stroja. Namerané {live_rms} mm/s. Naplánujte diagnostiku.',
            'alarm_zavaznost': 'WARNING'
        }]
    elif live_rms >= (limit_rms * 0.6):
        return [{
            'nazov': 'Vibrácie (ISO 10816)',
            'hodnota': f'Zóna B - Prijateľné ({live_rms} mm/s)',
            'farba': 'emerald',
            'ikona': '📉'
        }]
    else:
        return [{
            'nazov': 'Vibrácie (ISO 10816)',
            'hodnota': f'Zóna A - Výborné ({live_rms} mm/s)',
            'farba': 'emerald',
            'ikona': '📉'
        }]