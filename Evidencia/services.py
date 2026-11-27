import base64
import calendar
import hashlib
import json
import logging
import mimetypes
import os
import re
import secrets
import time
import unicodedata
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Optional
from urllib.parse import quote_plus
from django.utils.html import escape

import fitz  # PyMuPDF
import requests
from google import genai
from google.genai import types

from django.apps import apps
from django.conf import settings
from django.urls import reverse
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist
from django.core.files.base import ContentFile
from django.db import transaction
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import (
    Avg,
    Count,
    DecimalField,
    ExpressionWrapper,
    F,
    FloatField,
    IntegerField,
    Prefetch,
    Q,
    Sum,
    fields,
)
from django.db.models.functions import Coalesce
from django.utils import timezone

from .utils_compliance import (
    get_global_compliance_queue,
    get_zariadenie_compliance_audit,
    get_compliance_config,
    vyhodnot_zariadenie_iot_telemetriu,
    get_zariadenie_live_diagnostika,
    to_date,
    safe_float,
    add_calendar_interval,
    get_iso_pre_objekt,
)

from Evidencia.industry_loader import (
    CATEGORY_MAP,
    CATEGORY_CHOICES,
    ZARIADENIA_MAP,
    ZARIADENIA_CHOICES,
    FLAT_CATEGORY_MAP,
    PARENT_MODELS,
    COMPLIANCE_ALARM_PREFIXES,
    ZARIADENIA_COLOR_MAP,
    SAMPLE_IMPORT_DATA,
    get_active_parameter_reverse_names,
    get_zariadenie_parametre,
    get_all_industry_terms,
    get_industry_term,
    get_default_stock_model_name,
    get_easter_egg_tiles,
    HAS_ENERGY_ESG_AUDIT,
    HAS_FPLYNY_AUDIT,
    HAS_HEAT_RECOVERY,
    HAS_VTZ_INSPECTION,
    HAS_VIBRO_DIAGNOSTICS,
    HAS_CAPEX_CALCULATOR,
    FPLYNY_CHLADIVA,
    HUB_CONFIG,
    TCO_PROFILES,
    MEDIA_QUALITY_APPS,
)
from Evidencia.utils import generate_dynamic_greeting, ocisti_nazov_od_jednotky
from Evidencia.utils_helper import LedgerKalkulacka, get_firemna_mena, isas_ai_logistika_trasy
from Evidencia.models import CenovaPonuka, Zamestnanec
from ISASaplikacia.models import FiremnaIdentita
from ISASaplikacia.tasks import save_draft_imap_task
from ISASaplikacia.utils import get_gemini_api_key
from .ai_prompts import zostav_staticky_a_dynamicky_prompt, _je_vedenie_firmy, _klasifikuj_externy_subjekt

logger = logging.getLogger(__name__)

def _spravuj_pamat_historie(history_text: str, max_chars: int = 20000) -> str:
    """
    Bezpečne orezáva históriu po celých riadkoch bez poškodenia HTML značiek a syntaxe.
    """
    if not history_text or len(history_text) <= max_chars:
        return history_text

    riadky = history_text.splitlines()
    vybrane_riadky = []
    aktualna_dlzka = 0

    # Čítame od najnovších správ dozadu
    for riadok in reversed(riadky):
        if aktualna_dlzka + len(riadok) > max_chars:
            break
        vybrane_riadky.append(riadok)
        aktualna_dlzka += len(riadok) + 1

    if not vybrane_riadky and riadky:
        # Fallback pre extrémne dlhý jednoriadkový text bez nových riadkov
        vybrane_riadky = [riadky[-1][-max_chars:]]
    else:
        vybrane_riadky.reverse()
    return f"[... Staršia história bola zmazaná pre optimalizáciu pamäte ...]\n" + "\n".join(vybrane_riadky)

def tool_sklad_a_katalog(hladany_vyraz: Optional[str] = None, kategoria_dielu: Optional[str] = None, limit: int = 10, offset: int = 0) -> str:
    """
    Použi na prehľad SKLADU a ODOSLANÝCH OBJEDNÁVOK. Zobrazuje stav na HLAVNOM SKLADE aj na všetkých FILIÁLKACH! 
    Môžeš použiť 'limit' a 'offset' na listovanie vo veľkých výsledkoch.
    """
    # 🛡️ DEFENSIVE TYPECASTING: Ochrana pred zlyhaním Django Slicingu
    try:
        limit = max(1, min(int(limit), 100)) if limit is not None else 10
        offset = max(0, int(offset)) if offset is not None else 0
    except (ValueError, TypeError):
        limit, offset = 10, 0

    CenovyKatalog = apps.get_model('Evidencia', 'CenovyKatalog')
    TechnickaSpecifikacia = apps.get_model('Evidencia', 'TechnickaSpecifikacia')
    OdoslanaObjednavka = apps.get_model('Evidencia', 'OdoslanaObjednavka')
    StockMovement = apps.get_model('Evidencia', 'StockMovement')
    Sklad = apps.get_model('Evidencia', 'Sklad')
    Filialka = apps.get_model('Evidencia', 'Filialka')
    hlavny_sklad = Sklad.objects.first()
    nazov_hlavneho = f"Hlavný sklad ({hlavny_sklad.nazov})" if hlavny_sklad and hlavny_sklad.nazov and hlavny_sklad.nazov.lower() != "hlavný sklad" else "Hlavný sklad"
    
    # Prednačítame všetky filiálky do pamäte (aby sme ich nenačítavali v cykle)
    filialky = list(Filialka.objects.all())
    stavy_filialok = {f.id: LedgerKalkulacka.zisti_stav_vsetkych_poloziek(f) for f in filialky}
    nazvy_na_filialkach = {
        nazov
        for stavy in stavy_filialok.values()
        for nazov, qty in stavy.items()
        if qty and qty > 0
    }
    
    vysledky: list[dict[str, Any]] = []
    
    try:
        preskocenych = 0
        for main_cat, subcats in CATEGORY_MAP.items():
            if kategoria_dielu and kategoria_dielu.lower() not in main_cat.lower():
                continue
                
            for subcat_name, model_cls in subcats.items():
                qs = model_cls.objects.select_related('dodavatel', 'vyrobca')
                 
                if hladany_vyraz:
                    qs = qs.filter(Q(seriove_cislo__icontains=hladany_vyraz) | Q(kto_som__icontains=hladany_vyraz) | Q(katalogove_cislo__icontains=hladany_vyraz) | Q(ean_kod__icontains=hladany_vyraz))
                elif not nazvy_na_filialkach:
                    qs = qs.filter(pocet_na_sklade__gt=0)

                potrebne_kusy = limit - len(vysledky)
                if hladany_vyraz or not nazvy_na_filialkach:
                    # 🛡️ GLOBAL ACCUMULATIVE SLICING: Presné stránkovanie naprieč heterogénnymi modelmi skladu
                    pocet_v_kategorii = qs.count()
                    if preskocenych + pocet_v_kategorii <= offset:
                        preskocenych += pocet_v_kategorii
                        continue

                    start_index = max(0, offset - preskocenych)
                    ziskane_polozky = list(qs[start_index:start_index + potrebne_kusy])
                    preskocenych += pocet_v_kategorii
                else:
                    ziskane_polozky = []
                    for item in qs:
                        hlavny_pocet = getattr(item, 'pocet_na_sklade', 0) or 0
                        if hlavny_pocet <= 0 and ocisti_nazov_od_jednotky(str(item)) not in nazvy_na_filialkach:
                            continue
                        if preskocenych < offset:
                            preskocenych += 1
                            continue
                        ziskane_polozky.append(item)
                        if len(ziskane_polozky) >= potrebne_kusy:
                            break

                if not ziskane_polozky:
                    continue
                    
                # Získame ceny a manuály naraz (BULK FETCH)
                ct = ContentType.objects.get_for_model(model_cls)
                item_ids = [item.id for item in ziskane_polozky]
                
                katalogy = CenovyKatalog.objects.filter(content_type=ct, object_id__in=item_ids)
                katalog_mapa = {k.object_id: k for k in katalogy}
                
                manualy_qs = TechnickaSpecifikacia.objects.filter(content_type=ct, object_id__in=item_ids)
                manualy_mapa = {m.object_id: m for m in manualy_qs}
                
                for item in ziskane_polozky:
                    katalog = katalog_mapa.get(item.id)
                    jednotka = getattr(item, 'merna_jednotka', 'ks')
                    cisty_nazov = ocisti_nazov_od_jednotky(str(item))
                    
                    # 🟢 Zostavenie dynamického reťazca zásob (Ledger)
                    hlavny_pocet = getattr(item, 'pocet_na_sklade', 0) or 0
                    skladove_stavy = [f"{nazov_hlavneho}: {hlavny_pocet} {jednotka}"]
                    celkovy_pocet = hlavny_pocet
                    
                    for f in filialky:
                        stav_f = stavy_filialok[f.id].get(cisty_nazov, 0)
                        if stav_f > 0:
                            skladove_stavy.append(f"{f.nazov}: {stav_f} {jednotka}")
                            celkovy_pocet += stav_f
                    
                    # Ak nemáme vôbec nič ani na jednom sklade a nehľadáme konkrétny výraz, položku preskočíme
                    if not hladany_vyraz and celkovy_pocet == 0:
                        continue
                    
                    vysledky.append({
                        "kategoria": f"{main_cat} -> {subcat_name}",
                        "nazov_seriove_cislo": item.seriove_cislo,
                        "katalogove_cislo": getattr(item, 'katalogove_cislo', 'Neuvedené'),
                        "ean_kod": getattr(item, 'ean_kod', 'Neuvedené'),
                        "technicky_popis": getattr(item, 'kto_som', 'Bez popisu'),
                        "skladom_rozpis": ", ".join(skladove_stavy),
                        "nakupna_cena_eur": float(katalog.nakupna_cena) if katalog and katalog.nakupna_cena is not None else 0.0,
                        "predajna_cena_eur": float(katalog.predajna_cena) if katalog and katalog.predajna_cena is not None else 0.0,
                        "marza_percento": float(katalog.marza_percento) if katalog and katalog.marza_percento is not None else 0.0,
                        "cena_aktualizovana": katalog.posledna_aktualizacia.strftime('%d.%m.%Y %H:%M') if katalog and katalog.posledna_aktualizacia else "Neznáme",
                        "sadzba_dph_percento": float(item.sadzba_dph) if getattr(item, 'sadzba_dph', None) is not None else float(apps.get_model('ISASaplikacia', 'GlobalneNastavenia').get_zakladna_dph()),
                        "dodavatel": item.dodavatel.Nazov_spolocnosti if getattr(item, 'dodavatel', None) else 'Neznámy',
                        "vyrobca": item.vyrobca.nazov_vyrobcu if getattr(item, 'vyrobca', None) else 'Neznámy',
                        "vyrobca_katalog_url": item.vyrobca.katalog_dielov_url if getattr(item, 'vyrobca', None) else "Neznámy",
                        "vyrobca_technicka_poznamka": item.vyrobca.technicka_poznamka if getattr(item, 'vyrobca', None) else "Žiadna",
                        "typ_zasoby_erp": getattr(item, 'typ_zasoby_erp', 'Neznáme'),
                        "typ_original_nahrada": item.get_typ_display() if hasattr(item, 'typ') and item.typ else "Nešpecifikované",
                        "existuje_manual": item.id in manualy_mapa,
                        "interna_poznamka_k_dielu": manualy_mapa[item.id].interna_poznamka if item.id in manualy_mapa else "Žiadna",
                        "odkaz_na_web": manualy_mapa[item.id].web_stranka if item.id in manualy_mapa else "Žiadny",
                        "ma_pdf_manual": bool(manualy_mapa[item.id].subor_manual) if item.id in manualy_mapa else False,
                        "ma_schemu": bool(manualy_mapa[item.id].subor_schema) if item.id in manualy_mapa else False,
                        "ma_certifikat": bool(manualy_mapa[item.id].subor_certifikat) if item.id in manualy_mapa else False,
                        "obchodny_nazov_pdf": getattr(item, 'obchodny_nazov', None) or "Zhodný so sériovým číslom",
                        "skryt_vyrobcu_na_pdf": getattr(item, 'skryt_vyrobcu', False),
                        "zobrazit_v_eshope_web": getattr(item, 'zobrazit_na_webe', False),
                        "ma_obrazok_na_webe": bool(getattr(item, 'obrazok_na_webe', False)),
                        "marketingovy_popis_web": getattr(item, 'popis_na_webe', None) or "Žiadny",
                        "slug": getattr(item, 'slug', None)
                    })
                
                if len(vysledky) >= limit: break
            if len(vysledky) >= limit: break
                
        # 🟢 Pridanie odoslaných objednávok do kontextu skladu
        objednavky_qs = OdoslanaObjednavka.objects.select_related('dodavatel').prefetch_related('prijate_faktury').order_by('-vytvorene')
        if hladany_vyraz:
            objednavky_qs = objednavky_qs.filter(Q(cislo_objednavky__icontains=hladany_vyraz) | Q(dodavatel__Nazov_spolocnosti__icontains=hladany_vyraz))
            
        nedavne_objednavky = []
        for obj in objednavky_qs[offset:offset+limit]:
            nedavne_objednavky.append({
                "cislo_objednavky": obj.cislo_objednavky,
                "poradove_cislo_dodavatela": obj.poradove_cislo_dodavatela,
                "dodavatel": obj.dodavatel.Nazov_spolocnosti if obj.dodavatel else "Neznámy",
                "pocet_poloziek": obj.celkovy_pocet_poloziek,
                "ma_vygenerovane_pdf": bool(obj.pdf_dokument),
                "sparovane_prijate_faktury": [f.id for f in getattr(obj, 'prijate_faktury').all()] if hasattr(obj, 'prijate_faktury') else [],
                "vytvorene": obj.vytvorene.strftime('%d.%m.%Y') if obj.vytvorene else "Neznáme"
            })

        # 🟢 Pridanie histórie pohybov (Kto čo kedy zobral/pridal)
        pohyby_qs = StockMovement.objects.select_related('user', 'filialka', 'faktura').order_by('-timestamp')
        if hladany_vyraz:
            pohyby_qs = pohyby_qs.filter(product_name__icontains=hladany_vyraz)
            
        historia_pohybov = []
        for pohyb in pohyby_qs[offset:offset+limit]:
            historia_pohybov.append({
                "akcia": pohyb.get_action_display(),
                "typ_dokladu_erp": pohyb.get_typ_dokladu_erp_display(),
                "kategoria": pohyb.category,
                "podkategoria": pohyb.subcategory,
                "polozka": pohyb.product_name,
                "mnozstvo": f"{pohyb.count} {pohyb.merna_jednotka}",
                "vykonal": pohyb.user.get_full_name() if pohyb.user else "Systém",
                "sklad": pohyb.filialka.nazov if pohyb.filialka else nazov_hlavneho,
                "zdrojova_faktura_id": pohyb.faktura.id if pohyb.faktura else None,
                "datum": pohyb.timestamp.strftime('%d.%m.%Y %H:%M') if pohyb.timestamp else "Neznáme"
            })

        # 🟢 Pridanie informácie o tovare, ktorý fyzicky prišiel, ale čaká v Naskladňovacom dashboarde
        LogNaskladnenia = apps.get_model('Evidencia', 'LogNaskladnenia')
        cakajuce_qs = LogNaskladnenia.objects.filter(uspesne=False).exclude(navrhovana_kategoria='Sluzba').select_related('faktura__dodavatel', 'navrhovany_vyrobca', 'filialka')
        if hladany_vyraz:
            cakajuce_qs = cakajuce_qs.filter(Q(najdeny_kod_faktura__icontains=hladany_vyraz) | Q(navrhovany_nazov__icontains=hladany_vyraz))
        
        cakajuce_v_dashboarde = []
        for log in cakajuce_qs[offset:offset+limit]:
            cakajuce_v_dashboarde.append({
                "faktura_id": log.faktura.id if log.faktura else None,
                "dodavatel": log.faktura.dodavatel.Nazov_spolocnosti if (log.faktura and log.faktura.dodavatel) else "Neznámy",
                "kod": log.najdeny_kod_faktura,
                "nazov": log.navrhovany_nazov or "Bez názvu",
                "navrhovana_kategoria": log.navrhovana_kategoria or "Nezadaná",
                "navrhovany_vyrobca": log.navrhovany_vyrobca.nazov_vyrobcu if log.navrhovany_vyrobca else "Neznámy",
                "nakupna_cena_eur": float(log.nakupna_cena) if log.nakupna_cena else 0.0,
                "cielovy_sklad": log.filialka.nazov if log.filialka else nazov_hlavneho,
                "mnozstvo": float(log.mnozstvo) if log.mnozstvo is not None else 0.0,
                "upozornenie": "Tento tovar už fyzicky prišiel, ale ešte nebol potvrdený v Naskladňovacom dashboarde!"
            })

        return json.dumps({
            "data": vysledky, 
            "nedavne_odoslane_objednavky": nedavne_objednavky, 
            "historia_pohybov": historia_pohybov,
            "cakajuce_v_dashboarde": cakajuce_v_dashboarde,
            "offset": offset, 
            "limit": limit
        }) if (vysledky or nedavne_objednavky or historia_pohybov or cakajuce_v_dashboarde) else json.dumps({"informacia": "Položka, objednávka ani pohyb nenájdený."})
    except Exception as e:
        return json.dumps({"error": str(e)})


def tool_zariadenia_a_technicke_parametre(nazov_zakaznika: Optional[str] = None, vyrobne_cislo: Optional[str] = None, limit: int = 10, offset: int = 0) -> str:
    """
    Použi na analýzu STROJOV u zákazníkov, ich PARAMETROV. Podporuje stránkovanie cez limit a offset.
    """
    return _vykonaj_zariadenia_dotaz(
        nazov_zakaznika=nazov_zakaznika,
        vyrobne_cislo=vyrobne_cislo,
        limit=limit,
        offset=offset,
    )


def _vykonaj_zariadenia_dotaz(nazov_zakaznika: Optional[str] = None, vyrobne_cislo: Optional[str] = None, limit: int = 10, offset: int = 0, exact_zakaznik_nazov: Optional[str] = None, exact_zakaznik_id: Optional[int] = None) -> str:
    # 🛡️ DEFENSIVE TYPECASTING
    try:
        limit = max(1, min(int(limit), 100)) if limit is not None else 10
        offset = max(0, int(offset)) if offset is not None else 0
    except (ValueError, TypeError):
        limit, offset = 10, 0

    ServisovaneZariadenie = apps.get_model('Evidencia', 'ServisovaneZariadenie')
    KorekciaMotohodinLog = apps.get_model('Evidencia', 'KorekciaMotohodinLog')
    HistoriaPrenajmuLog = apps.get_model('Evidencia', 'HistoriaPrenajmuLog')
    AlarmLog = apps.get_model('Evidencia', 'AlarmLog')
    SignalDictionary = apps.get_model('Evidencia', 'SignalDictionary')
    TelemetryHistory = apps.get_model('Evidencia', 'TelemetryHistory')
    
    vysledky: list[dict[str, Any]] = []
    try:
        reverse_one_to_one = get_active_parameter_reverse_names() or [str(f.get_accessor_name()) for f in ServisovaneZariadenie._meta.get_fields() if f.is_relation and f.one_to_one and not f.concrete and hasattr(f, 'get_accessor_name') and f.get_accessor_name()]
        select_rels = ['zakaznik', 'vyrobca'] + reverse_one_to_one
        prefetch_korekcie = Prefetch('korekcie_motohodin', queryset=KorekciaMotohodinLog.objects.select_related('vykonal').order_by('-datum'))
        prefetch_alarmy = Prefetch('alarmy', queryset=AlarmLog.objects.filter(vyriesene=False).order_by('-casova_peciatka'))
        prefetch_slovnik = Prefetch('slovnik_signalov', queryset=SignalDictionary.objects.all())
        prefetch_prenajmy = Prefetch('historia_prenajmov', queryset=HistoriaPrenajmuLog.objects.select_related('povodny_zakaznik', 'novy_zakaznik', 'vykonal').order_by('-datum_presunu', '-id'))

        qs = ServisovaneZariadenie.objects.select_related(*select_rels).prefetch_related('predpisy__typ_prace', 'predpisy__polozky__diel', prefetch_korekcie, prefetch_alarmy, prefetch_slovnik, prefetch_prenajmy)
        if exact_zakaznik_id is not None:
            qs = qs.filter(zakaznik_id=exact_zakaznik_id, zakaznik__je_interny_profil=False)
        elif exact_zakaznik_nazov:
            qs = qs.filter(zakaznik__Nazov_spolocnosti__iexact=exact_zakaznik_nazov, zakaznik__je_interny_profil=False)
        elif nazov_zakaznika:
            qs = qs.filter(zakaznik__Nazov_spolocnosti__icontains=nazov_zakaznika)
        if vyrobne_cislo: qs = qs.filter(vyrobne_cislo__icontains=vyrobne_cislo)

        ziskane_stroje = list(qs[offset:offset+limit])
        stroj_ids = [s.id for s in ziskane_stroje]
        
        # BULK FETCH: Načítanie telemetrie pre všetky stroje na stránke v 1 SQL dopyte
        vsetka_telemetria = TelemetryHistory.objects.filter(zariadenie_id__in=stroj_ids).order_by('-casova_peciatka')
        telemetria_mapa: dict[int, list[Any]] = {}
        for t in vsetka_telemetria:
            telemetria_mapa.setdefault(t.zariadenie_id, []).append(t)

        for stroj in ziskane_stroje:
            stroj_je_interny = bool(stroj.zakaznik and getattr(stroj.zakaznik, 'je_interny_profil', False))
            technicke_parametre: dict[str, Any] = {}
            stroj_data: dict[str, Any] = {
                "id": stroj.id,
                "zakaznik": (
                    f"{stroj.zakaznik.Nazov_spolocnosti} (Naša dielňa / Centrála)"
                    if stroj_je_interny
                    else (stroj.zakaznik.Nazov_spolocnosti if stroj.zakaznik else "Interný stroj")
                ),
                "je_nas_vlastny_stroj_na_dielni": stroj_je_interny,
                "stav_prenajmu": "U ZÁKAZNÍKA (V prenájme)" if stroj.v_prenajme_od_isas else ("VOĽNÉ NA PRENÁJOM (Na centrále / v sklade)" if stroj.je_urcene_na_prenajom else "BEŽNÉ ZARIADENIE (Mimo rental flotily)"),
                "typ_stroja": stroj.get_kategoria_display(),
                "vyrobca_model": (
                    stroj.model_zariadenia
                    if (exact_zakaznik_nazov or exact_zakaznik_id is not None)
                    else f"{stroj.vyrobca.nazov_vyrobcu if stroj.vyrobca else ''} {stroj.model_zariadenia}".strip()
                ),
                "interny_nazov": stroj.nazov or "Bez názvu",
                "vyrobne_cislo": stroj.vyrobne_cislo,
                "rok_vyroby": stroj.rok_vyroby,
                "datum_instalacie": stroj.datum_instalacie.strftime('%d.%m.%Y') if stroj.datum_instalacie else "Neznáme",
                "zaruka_do": stroj.zaruka_do.strftime('%d.%m.%Y') if stroj.zaruka_do else "Nezadaná",
                "predlzena_zaruka_do": stroj.predlzena_zaruka_do.strftime('%d.%m.%Y') if stroj.predlzena_zaruka_do else "Nezadaná",
                "je_aktualne_v_zaruke": getattr(stroj, 'je_v_zaruke', False),
                "zarucny_status": "💎 PREDĹŽENÁ ZÁRUKA" if (stroj.predlzena_zaruka_do and stroj.predlzena_zaruka_do >= timezone.now().date()) else ("🛡️ V ZÁRUKE" if getattr(stroj, 'je_v_zaruke', False) else "⚪ PO ZÁRUKE"),
                "motohodiny": stroj.aktualne_motohodiny,
                "motohodiny_v_zatazi": stroj.motohodiny_v_zatazi,
                "meter_display": getattr(stroj, 'meter_display', f"{stroj.aktualne_motohodiny} {get_industry_term('meter_unit', 'hod.')}"),
                "meter_load_display": getattr(stroj, 'meter_load_display', f"{stroj.motohodiny_v_zatazi} {get_industry_term('meter_load_unit', 'hod.')}"),
                "meter_unit": getattr(stroj, 'meter_unit', None) or get_industry_term('meter_unit', 'hod.'),
                "meter_load_unit": getattr(stroj, 'meter_load_unit', None) or get_industry_term('meter_load_unit', 'hod.'),
                "motohodiny_aktualizovane": stroj.posledna_aktualizacia_stavu.strftime('%d.%m.%Y %H:%M') if stroj.posledna_aktualizacia_stavu else "Neznáme",
                "hmotnost_kg": stroj.hmotnost_kg if stroj.hmotnost_kg else "Nezadaná",
                "presna_adresa_stroja": (
                    "Centrála spoločnosti (Naša prevádzka)"
                    if stroj_je_interny
                    else (f"{stroj.zakaznik.ulica_a_cislo}, {stroj.zakaznik.psc} {stroj.zakaznik.mesto}" if stroj.zakaznik else "Interný sklad")
                ),
                "umiestnenie_v_objekte": stroj.umiestnenie or "Nešpecifikované",
                "poznamka": stroj.poznamka or "Žiadna",
                "v_prenajme": stroj.v_prenajme_od_isas,
                "ma_vygenerovany_qr_kod": bool(stroj.qr_kod),
                "iot_mac_adresa": stroj.iot_mac_adresa or "Nepripojené",
                "iot_ip_adresa": stroj.iot_ip_adresa or "Neznáma",
                "iot_protokol": stroj.get_iot_protokol_display() if stroj.iot_protokol else "Žiadny",
                "iot_api_kluc": "Nastavený (Skrytý)" if stroj.iot_api_kluc else "Nenastavený",
                "iot_je_aktivny": stroj.iot_je_aktivny,
                "iot_limit_zapisov_za_hodinu": stroj.iot_limit_zapisov_za_hodinu,
                "v_servisnom_rezime_mute_alarmov": stroj.v_servisnom_rezime,
                "bezpecnostny_kluc_pas_stroja": str(stroj.bezpecnostny_kluc) if stroj.bezpecnostny_kluc else "Žiadny",
                "aktivne_iot_alarmy": [f"[{a.get_zavaznost_display()}] {a.kod_chyby}: {a.popis} (Snapshot: {a.snapshot_dat}) ({a.casova_peciatka.strftime('%d.%m.%Y %H:%M') if a.casova_peciatka else 'Neznámy čas'})" for a in stroj.alarmy.all()],
                "iot_slovnik_signalov": [f"{s.register_adresa} -> {s.nazov_signalu} ({s.get_typ_signalu_display()}, {s.get_prava_pristupu_display()})" for s in stroj.slovnik_signalov.all()],
                "nedavna_telemetria": [{"cas": t.casova_peciatka.strftime('%d.%m.%Y %H:%M:%S') if t.casova_peciatka else "Neznámy čas", "analog": t.analogove_hodnoty, "digital": t.digitalne_stavy} for t in telemetria_mapa.get(stroj.id, [])[:5]],
                "live_diagnostika_iot": get_zariadenie_live_diagnostika(stroj, posledny_zaznam=(telemetria_mapa.get(stroj.id) or [None])[0]),
                "iot_vyhodnotenie_stavu": vyhodnot_zariadenie_iot_telemetriu(
                    stroj,
                    (telemetria_mapa.get(stroj.id) or [None])[0].analogove_hodnoty if telemetria_mapa.get(stroj.id) else {},
                    digitalne_stavy=(telemetria_mapa.get(stroj.id) or [None])[0].digitalne_stavy if telemetria_mapa.get(stroj.id) else None
                ) if telemetria_mapa.get(stroj.id) else None,

                "je_urcene_na_prenajom_rental": stroj.je_urcene_na_prenajom,
                "zobrazit_v_pozicovni_web": stroj.zobrazit_na_webe_prenajom,
                "ma_obrazok_na_webe": bool(stroj.obrazok_na_webe),
                "slug": stroj.slug,
                "cena_prenajmu_na_den_eur": float(stroj.cena_prenajmu_na_den) if stroj.cena_prenajmu_na_den else "Na vyžiadanie",
                "technicke_parametre": technicke_parametre
            }
            
            param_obj = get_zariadenie_parametre(stroj)
            if param_obj is not None:
                ignorovane_p_polia = {'id', 'zariadenie', 'vytvorene', 'upravene', 'vytvoril', 'upravil'}
                for p_field in param_obj._meta.fields:
                    if p_field.name not in ignorovane_p_polia:
                        val = getattr(param_obj, p_field.name, None)
                        if val is not None and val != '':
                            if isinstance(val, Decimal): val = float(val)
                            elif isinstance(val, (datetime, date)): val = val.isoformat()
                            elif hasattr(p_field, 'choices') and p_field.choices: val = dict(p_field.choices).get(val, str(val))
                            technicke_parametre[str(p_field.verbose_name)] = val
                            
            # 🟢 Pridanie Servisných predpisov (Kusovníkov)
            servisne_predpisy: list[dict[str, Any]] = []
            for predpis in stroj.predpisy.all():
                diely = []
                for p in predpis.polozky.all():
                    try:
                        if p.diel:
                            diely.append(f"{p.mnozstvo}x {p.diel}")
                    except ObjectDoesNotExist:
                        diely.append(f"{p.mnozstvo}x (Zmazaná skladová položka #{p.object_id})")
                servisne_predpisy.append({
                    "typ_prace": predpis.typ_prace.nazov,
                    "potrebne_diely": diely
                })
            stroj_data["servisne_predpisy"] = servisne_predpisy

            korekcie = list(stroj.korekcie_motohodin.all())[:5]
            stroj_data["logy_korekcii_motohodin"] = [
                {
                    "datum": k.datum.strftime('%d.%m.%Y %H:%M') if k.datum else "Neznáme",
                    "vykonal": k.vykonal.get_full_name() if k.vykonal else "Neznámy",
                    "zmena": f"{k.povodna_hodnota_display} -> {k.nova_hodnota_display}",
                    "zmena_zataz": f"{k.povodna_hodnota_zataz_display} -> {k.nova_hodnota_zataz_display}" if k.povodna_hodnota_zataz is not None else "Nemenené",
                    "dovod": k.dovod
                } for k in korekcie
            ]

            prenajmy = list(stroj.historia_prenajmov.all())[:5]
            stroj_data["historia_prenajmov"] = [
                {
                    "datum": p.datum_presunu.strftime('%d.%m.%Y') if p.datum_presunu else "Neznáme",
                    "typ_akcie": p.get_typ_akcie_display(),
                    "od_partnera": p.povodny_zakaznik.Nazov_spolocnosti if p.povodny_zakaznik else "Centrála / Sklad",
                    "k_partnerovi": p.novy_zakaznik.Nazov_spolocnosti if p.novy_zakaznik else "Centrála / Sklad",
                    "stav_pocitadla": f"{p.stav_pocitadla} {getattr(stroj, 'meter_unit', None) or get_industry_term('meter_unit', 'hod.')}" if p.stav_pocitadla is not None else "Nezadaný",
                    "cislo_protokolu": p.cislo_protokolu or "Neuvedené",
                    "umiestnenie": p.nove_umiestnenie or "Centrálny sklad",
                    "vykonal": p.vykonal.get_full_name() if p.vykonal else "Systém",
                    "poznamka": p.poznamka or "Bez poznámky"
                } for p in prenajmy
            ]

            # ⚖️ ISAS COMPLIANCE AUDIT: 100% presný a defenzívne ošetrený legislatívny audit stroja k dnešnému dňu
            try:
                compliance_audit = get_zariadenie_compliance_audit(stroj)
                stroj_data["zakonny_revizny_status"] = [
                    f"{c['nazov']}: {c['stav_str']} ({c['typ_lehoty']})" for c in compliance_audit
                ] if compliance_audit else ["Všetky revízie a kontroly sú v norme / Bez evidovaných povinností"]
            except Exception as comp_err:
                logger.warning(f"Chyba pri compliance audite stroja {stroj.id}: {comp_err}")
                stroj_data["zakonny_revizny_status"] = ["Status revízií sa nepodarilo overiť (Chyba dát)"]
                
            vysledky.append(stroj_data)
        return json.dumps({"data": vysledky}) if vysledky else json.dumps({"informacia": "Zariadenie nenájdené."})
    except Exception as e:
        return json.dumps({"error": str(e)})


def tool_operativa_servis_a_ulohy(technik_meno: Optional[str] = None, zakaznik_nazov: Optional[str] = None, datum_od: Optional[str] = None, datum_do: Optional[str] = None, limit: int = 5, offset: int = 0) -> str:
    """
    Prehľad OTVORENÝCH ÚLOH, história SERVISOV, PREDIKCIA údržby a POVOLENIA NA VSTUP do fabrík.
    Dátumy zadávaj vo formáte YYYY-MM-DD.
    """
    return _vykonaj_operativa_dotaz(
        technik_meno=technik_meno,
        zakaznik_nazov=zakaznik_nazov,
        datum_od=datum_od,
        datum_do=datum_do,
        limit=limit,
        offset=offset,
    )


def _vykonaj_operativa_dotaz(technik_meno: Optional[str] = None, zakaznik_nazov: Optional[str] = None, datum_od: Optional[str] = None, datum_do: Optional[str] = None, limit: int = 5, offset: int = 0, exact_zakaznik_nazov: Optional[str] = None, exact_zakaznik_id: Optional[int] = None) -> str:
    # 🛡️ DEFENSIVE TYPECASTING
    try:
        limit = max(1, min(int(limit), 50)) if limit is not None else 5
        offset = max(0, int(offset)) if offset is not None else 0
    except (ValueError, TypeError):
        limit, offset = 5, 0

    Uloha = apps.get_model('Evidencia', 'Uloha')
    ServisnyZaznam = apps.get_model('Evidencia', 'ServisnyZaznam')
    PlanovanyServis = apps.get_model('Evidencia', 'PlanovanyServisZariadenia')
    PovolenieNaVstup = apps.get_model('Evidencia', 'PovolenieNaVstup')
    
    je_b2b_rezim = bool(exact_zakaznik_nazov or exact_zakaznik_id is not None)

    def _format_zariadenie_op(zar: Any) -> str:
        if not zar:
            return "Neznáme"
        if je_b2b_rezim:
            model_txt = (getattr(zar, 'model_zariadenia', '') or '').strip() or get_industry_term('zariadenie_fallback_nazov', 'Zariadenie')
            sn_txt = (getattr(zar, 'vyrobne_cislo', '') or '').strip()
            return f"{model_txt} (SN: {sn_txt})" if sn_txt else model_txt
        return str(zar)

    data: dict[str, list[dict[str, Any]]] = {"otvorene_ulohy": [], "aktivne_reklamacie": [], "nedavna_historia_servisov": [], "bliziace_sa_servisy_predikcia": [], "bliziace_sa_zakonne_revizie": [], "povolenia_na_vstup": []}
    
    try:
        d_od = to_date(datum_od)
        d_do = to_date(datum_do)

        # 1. Bežné otvorené úlohy (Mimo reklamácií)
        ulohy_qs = Uloha.objects.filter(dokoncene=False, je_reklamacia=False).select_related('zakaznik').prefetch_related('technici', 'typ_servisu', 'zariadenia__vyrobca', 'zdrojove_ponuky_ulohy', 'zdrojove_objednavky_ulohy', 'pouzite_polozky')
        if technik_meno: ulohy_qs = ulohy_qs.filter(Q(technici__meno__icontains=technik_meno) | Q(technici__priezvisko__icontains=technik_meno))
        if exact_zakaznik_id is not None:
            ulohy_qs = ulohy_qs.filter(zakaznik_id=exact_zakaznik_id, zakaznik__je_interny_profil=False)
        elif exact_zakaznik_nazov:
            ulohy_qs = ulohy_qs.filter(zakaznik__Nazov_spolocnosti__iexact=exact_zakaznik_nazov, zakaznik__je_interny_profil=False)
        elif zakaznik_nazov:
            ulohy_qs = ulohy_qs.filter(zakaznik__Nazov_spolocnosti__icontains=zakaznik_nazov)
        if d_od: ulohy_qs = ulohy_qs.filter(zaciatok__date__gte=d_od)
        if d_do: ulohy_qs = ulohy_qs.filter(zaciatok__date__lte=d_do)
            
        for u in ulohy_qs[offset:offset+limit]:
            data["otvorene_ulohy"].append({
                "id": u.id, "zakaznik": u.zakaznik.Nazov_spolocnosti if u.zakaznik else "Interné",
                "dokoncene": u.dokoncene,
                "farba_v_kalendari": u.farba,
                "zaciatok": u.zaciatok.strftime('%d.%m.%Y %H:%M') if u.zaciatok else "Nezadané",
                "koniec": u.koniec.strftime('%d.%m.%Y %H:%M') if u.koniec else "Nezadané",
                "zariadenia": [_format_zariadenie_op(z) for z in u.zariadenia.all()],
                "priradeni_technici": [f"{t.meno} {t.priezvisko}" for t in u.technici.all()],
                "typy_prac": [t.nazov for t in u.typ_servisu.all()],
                "zdrojove_ponuky": [p.cislo_ponuky for p in getattr(u, 'zdrojove_ponuky_ulohy').all()],
                "zdrojove_objednavky": [o.cislo_objednavky_interne for o in getattr(u, 'zdrojove_objednavky_ulohy').all()],
                "kontrolovat_sklad_jit": u.kontrolovat_sklad_pri_ulozeni,
                "pouzity_material_zatial": [f"{p.pocet}x {p.seriove_cislo_text or 'Diel'}" for p in u.pouzite_polozky.all()],
                "miesto_vykonania_adresa": getattr(u, 'miesto_vykonania', None) or (f"{u.zakaznik.ulica_a_cislo}, {u.zakaznik.mesto}" if u.zakaznik else "Neznáma adresa"),
                "detail_ulohy": u.blizsie_informacie or "Bez popisu"
            })

        # 2. 🚨 Aktívne reklamácie (SLA Monitor a Náklady s kaskádovým určením lehoty)
        GlobalneNastaveniaModel = apps.get_model('ISASaplikacia', 'GlobalneNastavenia')
        gn_obj = GlobalneNastaveniaModel.objects.first() if GlobalneNastaveniaModel else None
        globalna_lehota = getattr(gn_obj, 'standardna_lehota_reklamacie_dni', None) if gn_obj else None

        reklamacie_qs = Uloha.objects.filter(dokoncene=False, je_reklamacia=True).select_related('zakaznik').prefetch_related('technici', 'zariadenia__vyrobca', 'pouzite_polozky', 'zdrojove_ponuky_ulohy', 'zdrojove_objednavky_ulohy')
        if technik_meno: reklamacie_qs = reklamacie_qs.filter(Q(technici__meno__icontains=technik_meno) | Q(technici__priezvisko__icontains=technik_meno))
        if exact_zakaznik_id is not None:
            reklamacie_qs = reklamacie_qs.filter(zakaznik_id=exact_zakaznik_id, zakaznik__je_interny_profil=False)
        elif exact_zakaznik_nazov:
            reklamacie_qs = reklamacie_qs.filter(zakaznik__Nazov_spolocnosti__iexact=exact_zakaznik_nazov, zakaznik__je_interny_profil=False)
        elif zakaznik_nazov:
            reklamacie_qs = reklamacie_qs.filter(zakaznik__Nazov_spolocnosti__icontains=zakaznik_nazov)
        if d_od: reklamacie_qs = reklamacie_qs.filter(zaciatok__date__gte=d_od)
        if d_do: reklamacie_qs = reklamacie_qs.filter(zaciatok__date__lte=d_do)
        
        for r in reklamacie_qs[offset:offset+limit]:
            start_date = r.datum_prijatia_reklamacie or r.zaciatok
            dni_bezi = (timezone.now().date() - (start_date.date() if hasattr(start_date, 'date') else start_date)).days if start_date else 0
            
            # 🟢 Kaskádové určenie lehoty: Zakaznik -> StatLokalizacia -> GlobalneNastavenia -> None
            zak = r.zakaznik
            stat_lehota = None
            iso_zak = get_iso_pre_objekt(zak) if zak else None
            if iso_zak:
                StatLokalizaciaModel = apps.get_model('ISASaplikacia', 'StatLokalizacia')
                stat_obj = StatLokalizaciaModel.get_for_iso(iso_zak) if StatLokalizaciaModel else None
                stat_lehota = getattr(stat_obj, 'lehota_reklamacie_dni', None) if stat_obj else None

            platna_lehota = getattr(zak, 'reklamacna_lehota_dni', None) or stat_lehota or globalna_lehota
            zostava_dni = (platna_lehota - dni_bezi) if (platna_lehota is not None and start_date) else None

            # 🟢 Zistíme, aký materiál už technici na túto reklamáciu odpísali
            pouzity_material = [f"{p.pocet}x {p.seriove_cislo_text or 'Diel'}" for p in r.pouzite_polozky.all()]
            
            data["aktivne_reklamacie"].append({
                "id": r.id, 
                "zakaznik": r.zakaznik.Nazov_spolocnosti if r.zakaznik else "Neznámy",
                "zariadenia": [_format_zariadenie_op(z) for z in r.zariadenia.all()],
                "datum_prijatia_reklamacie": r.datum_prijatia_reklamacie.strftime('%d.%m.%Y %H:%M') if r.datum_prijatia_reklamacie else "Nezadané",
                "dni_v_rieseni": dni_bezi,
                "platna_lehota_dni": platna_lehota,
                "zostava_dni_lehoty": zostava_dni,
                "popis_problemu": r.blizsie_informacie,
                "zdrojove_ponuky": [p.cislo_ponuky for p in getattr(r, 'zdrojove_ponuky_ulohy').all()],
                "zdrojove_objednavky": [o.cislo_objednavky_interne for o in getattr(r, 'zdrojove_objednavky_ulohy').all()],
                "pouzity_material_zatial": pouzity_material if pouzity_material else ["Zatiaľ žiadny materiál"]
            })

        # 3. História servisov (Pridaný koreň problému pre ISO 9001)
        zaznamy_qs = ServisnyZaznam.objects.select_related('zariadenie__vyrobca', 'zakaznik', 'uloha').prefetch_related('pouzite_polozky_zaznamu', 'technici', 'vystavenafaktura_set', 'typ_servisu').order_by('-vytvorene')
        if exact_zakaznik_id is not None:
            zaznamy_qs = zaznamy_qs.filter(zakaznik_id=exact_zakaznik_id, zakaznik__je_interny_profil=False, dokoncene=True)
        elif exact_zakaznik_nazov:
            zaznamy_qs = zaznamy_qs.filter(zakaznik__Nazov_spolocnosti__iexact=exact_zakaznik_nazov, zakaznik__je_interny_profil=False, dokoncene=True)
        elif zakaznik_nazov:
            zaznamy_qs = zaznamy_qs.filter(zakaznik__Nazov_spolocnosti__icontains=zakaznik_nazov)
        if technik_meno: zaznamy_qs = zaznamy_qs.filter(Q(technici__meno__icontains=technik_meno) | Q(technici__priezvisko__icontains=technik_meno))
        if d_od: zaznamy_qs = zaznamy_qs.filter(vytvorene__date__gte=d_od)
        if d_do: zaznamy_qs = zaznamy_qs.filter(vytvorene__date__lte=d_do)
        
        for z in zaznamy_qs[offset:offset+limit]:
            pouzity_mat = [f"{m.pocet}x {m.seriove_cislo_text}" for m in z.pouzite_polozky_zaznamu.all()]
            data["nedavna_historia_servisov"].append({
                "datum": z.vytvorene.strftime('%d.%m.%Y') if z.vytvorene else "Neznáme",
                "upravene": z.upravene.strftime('%d.%m.%Y %H:%M') if z.upravene else "Neznáme",
                "dokoncene": z.dokoncene,
                "zariadenie": _format_zariadenie_op(z.zariadenie),
                "stav_motohodin": z.stav_motohodin if z.stav_motohodin else "Nezadané",
                "motohodiny_v_zatazi": z.motohodiny_v_zatazi if z.motohodiny_v_zatazi else "Nezadané",
                "meter_display": getattr(z, 'meter_display', None) or (f"{z.stav_motohodin} {get_industry_term('meter_unit', 'hod.')}" if z.stav_motohodin else "Nezadané"),
                "meter_load_display": getattr(z, 'meter_load_display', None) or (f"{z.motohodiny_v_zatazi} {get_industry_term('meter_load_unit', 'hod.')}" if z.motohodiny_v_zatazi else "Nezadané"),
                "poruchy_a_poznamky_technika": z.poruchy,
                "oficialne_vyjadrenie_pre_zakaznika": z.formalne_zhodnotenie_ai or "Nevygenerované",
                "skryta_ai_analyza_tco": z.ai_analyza_historie or "Žiadna",
                "namerane_hodnoty": z.namerane_hodnoty or "Žiadne merania",
                "bola_to_reklamacia": z.uloha.je_reklamacia if z.uloha else False,
                "koren_problemu_ak_reklamacia": z.get_koren_problemu_display() if z.koren_problemu != 'NEHODNOTENE' else "Bežný servis",
                "koren_problemu_b2b_verejny": getattr(z, 'verejny_koren_problemu_display', z.get_koren_problemu_display()) if z.koren_problemu != 'NEHODNOTENE' else "Bežný servis",
                "je_reklamacia_opravnena": getattr(z, 'je_reklamacia_opravnena', False),
                "zmena_hardveru": z.zmena_hardveru,
                "hodnotenie_zakaznika_nps": z.hodnotenie_zakaznika if z.hodnotenie_zakaznika else "Nehodnotené",
                "protokol_uzavrety": z.protokol_uzavrety,
                "kryptograficka_pecat_hash": z.bezpecnostny_hash or "Nezapečatené",
                "ma_bezpecnostny_kluc": bool(z.bezpecnostny_kluc),
                "upozornit_majitela_protokol_tg": z.upozornit_majitela_protokol_tg,
                "upozornit_majitela_protokol_mail": z.upozornit_majitela_protokol_mail,
                "upozornit_majitela_protokol_discord": z.upozornit_majitela_protokol_discord,
                "ma_vygenerovane_pdf": bool(z.uzavrety_pdf_dokument),
                "podpisal": z.meno_podpisujuceho or "Nepodpísané",
                "ma_fyzicky_podpis_zakaznika": bool(z.podpis_zakaznika),
                "ma_fyzicky_podpis_technika": bool(z.podpis_technika),
                "cas_podpisu": z.cas_podpisu.strftime('%d.%m.%Y %H:%M') if z.cas_podpisu else "Nepodpísané",
                "gps_poloha_podpisu": z.gps_suradnice or "Neznáma",
                "obsahuje_foto_pred": bool(z.foto_pred),
                "obsahuje_foto_po": bool(z.foto_po),
                "obsahuje_video": bool(z.video),
                "zobrazit_ako_referenciu_web": z.zobrazit_na_webe,
                "verejny_popis_referencie": z.verejny_popis_referencie or "Žiadny",
                "vykonal": [f"{t.meno} {t.priezvisko}" for t in z.technici.all()],
                "vykonane_typy_prac": [t.nazov for t in z.typ_servisu.all()],
                "vyfakturovane_vo_fakturach": (
                    [f.cislo_faktury for f in getattr(z, 'vystavenafaktura_set').all() if f.stav not in ('DRAFT', 'STORNO') and f.cislo_faktury]
                    if je_b2b_rezim
                    else [f.cislo_faktury or "DRAFT" for f in getattr(z, 'vystavenafaktura_set').all()]
                ),
                "spotrebovany_material": pouzity_mat
            })

        plany = PlanovanyServis.objects.filter(upozornenie_odoslane=False).select_related('zariadenie__zakaznik', 'zariadenie__vyrobca', 'typ_servisu', 'posledny_zaznam')
        if exact_zakaznik_id is not None:
            plany = plany.filter(zariadenie__zakaznik_id=exact_zakaznik_id, zariadenie__zakaznik__je_interny_profil=False)
        elif exact_zakaznik_nazov:
            plany = plany.filter(zariadenie__zakaznik__Nazov_spolocnosti__iexact=exact_zakaznik_nazov, zariadenie__zakaznik__je_interny_profil=False)
        elif zakaznik_nazov:
            plany = plany.filter(zariadenie__zakaznik__Nazov_spolocnosti__icontains=zakaznik_nazov)
        if d_od: plany = plany.filter(planovany_datum__gte=d_od)
        if d_do: plany = plany.filter(planovany_datum__lte=d_do)
        
        for p in plany.order_by('planovany_datum')[offset:offset+limit]:
            data["bliziace_sa_servisy_predikcia"].append({
                "stroj": _format_zariadenie_op(p.zariadenie), "typ_potrebneho_servisu": p.typ_servisu.nazov,
                "dosiahnutie_dna": p.planovany_datum.strftime('%d.%m.%Y') if p.planovany_datum else None,
                "dosiahnutie_motohodin": p.planovane_motohodiny if p.planovane_motohodiny else "Nezadané",
                "dosiahnutie_motohodin_zataz": p.planovane_motohodiny_zataz if p.planovane_motohodiny_zataz else "Nezadané",
                "meter_display": getattr(p, 'meter_display', None) or (f"{p.planovane_motohodiny} {get_industry_term('meter_unit', 'hod.')}" if p.planovane_motohodiny else "Nezadané"),
                "vaha_hierarchie": p.vaha_hierarchie,
                "upozornenie_odoslane": p.upozornenie_odoslane,
                "posledny_zaznam_id": p.posledny_zaznam.id if p.posledny_zaznam else None,
                "vytvorene": p.vytvorene.strftime('%d.%m.%Y %H:%M') if p.vytvorene else "Neznáme"
            })

        # ⚖️ ISAS OPERATÍVNY AUDIT: 100% defenzívne prepojenie dispečingu so zákonnými revíziami
        try:
            revizie_queue = get_global_compliance_queue(referencny_datum=timezone.now().date(), filter_alerts_only=True)
            
            # 🛡️ Ochrana: Bezpečné filtrovanie podľa zákazníka bez pádu na AttributeError
            if exact_zakaznik_id is not None:
                revizie_queue = [
                    r for r in revizie_queue
                    if r.get('zariadenie') and getattr(r['zariadenie'], 'zakaznik', None)
                    and not getattr(r['zariadenie'].zakaznik, 'je_interny_profil', False)
                    and getattr(r['zariadenie'], 'zakaznik_id', None) == exact_zakaznik_id
                ]
            elif exact_zakaznik_nazov:
                hladany_zak = str(exact_zakaznik_nazov).strip().lower()
                revizie_queue = [
                    r for r in revizie_queue
                    if r.get('zariadenie') and getattr(r['zariadenie'], 'zakaznik', None)
                    and not getattr(r['zariadenie'].zakaznik, 'je_interny_profil', False)
                    and str(getattr(r['zariadenie'].zakaznik, 'Nazov_spolocnosti', '') or '').strip().lower() == hladany_zak
                ]
            elif zakaznik_nazov:
                hladany_zak = str(zakaznik_nazov).strip().lower()
                revizie_queue = [
                    r for r in revizie_queue 
                    if r.get('zariadenie') and getattr(r['zariadenie'], 'zakaznik', None) 
                    and hladany_zak in str(getattr(r['zariadenie'].zakaznik, 'Nazov_spolocnosti', '') or '').lower()
                ]

            # 🛡️ Ochrana: Bezpečné filtrovanie dátumov (porovnávame len date objekty)
            if d_od:
                revizie_queue = [r for r in revizie_queue if r.get('termin') and (r['termin'].date() if isinstance(r['termin'], datetime) else r['termin']) >= d_od]
            if d_do:
                revizie_queue = [r for r in revizie_queue if r.get('termin') and (r['termin'].date() if isinstance(r['termin'], datetime) else r['termin']) <= d_do]

            cielovy_zoznam = data.setdefault("bliziace_sa_zakonne_revizie", [])
            for rev in revizie_queue[offset:offset+limit]:
                zar_obj = rev.get('zariadenie')
                zak_obj = getattr(zar_obj, 'zakaznik', None) if zar_obj else None
                zak_meno = getattr(zak_obj, 'Nazov_spolocnosti', 'Neznámy zákazník') if zak_obj else 'Neznámy zákazník'
                
                stroj_nazov = (
                    (getattr(zar_obj, 'model_zariadenia', None) or get_industry_term('zariadenie_fallback_nazov', 'Zariadenie'))
                    if je_b2b_rezim
                    else (getattr(zar_obj, 'model_zariadenia', None) or getattr(zar_obj, 'nazov', None) or 'Zariadenie')
                )
                stroj_sn = getattr(zar_obj, 'vyrobne_cislo', 'Neuvedené') or 'Neuvedené'

                cielovy_zoznam.append({
                    "zakaznik": zak_meno,
                    "stroj": f"{stroj_nazov} (SN: {stroj_sn})",
                    "typ_revizie": rev.get('nazov', 'Zákonná revízia'),
                    "stav": rev.get('stav_str', 'Vyžaduje kontrolu'),
                    "termin": rev.get('termin_str', 'Neuvedený'),
                    "dni_do_expiracie": rev.get('dni_do', 0)
                })
        except Exception as e_rev:
            logger.warning(f"Chyba pri načítaní operatívnych revízií: {e_rev}", exc_info=True)

        vstupy_qs = PovolenieNaVstup.objects.select_related('zakaznik', 'uloha').prefetch_related('zamestnanci', 'vozidla').order_by('-datum_vstupu')
        if exact_zakaznik_id is not None:
            vstupy_qs = vstupy_qs.filter(zakaznik_id=exact_zakaznik_id, zakaznik__je_interny_profil=False)
        elif exact_zakaznik_nazov:
            vstupy_qs = vstupy_qs.filter(zakaznik__Nazov_spolocnosti__iexact=exact_zakaznik_nazov, zakaznik__je_interny_profil=False)
        elif zakaznik_nazov:
            vstupy_qs = vstupy_qs.filter(zakaznik__Nazov_spolocnosti__icontains=zakaznik_nazov)
        if technik_meno: vstupy_qs = vstupy_qs.filter(Q(zamestnanci__meno__icontains=technik_meno) | Q(zamestnanci__priezvisko__icontains=technik_meno))
        if d_od: vstupy_qs = vstupy_qs.filter(datum_vstupu__gte=d_od)
        if d_do: vstupy_qs = vstupy_qs.filter(datum_vstupu__lte=d_do)
        
        for v in vstupy_qs[offset:offset+limit]:
            data["povolenia_na_vstup"].append({
                "zakaznik": v.zakaznik.Nazov_spolocnosti if v.zakaznik else "Neznámy",
                "datum_vstupu": v.datum_vstupu.strftime('%d.%m.%Y') if v.datum_vstupu else "Neznáme",
                "cas_schvalenia_sbs": v.cas_schvalenia_sbs.strftime('%d.%m.%Y %H:%M') if v.cas_schvalenia_sbs else "Zatiaľ neschválené",
                "ip_schvalitela_sbs": v.ip_schvalitela_sbs or "Neznáma",
                "notif_technik_tg": v.notif_technik_tg,
                "notif_technik_mail": v.notif_technik_mail,
                "notif_technik_discord": v.notif_technik_discord,
                "notif_admin_tg": v.notif_admin_tg,
                "notif_admin_mail": v.notif_admin_mail,
                "notif_admin_discord": v.notif_admin_discord,
                "stav_kod": v.stav,
                "stav": v.get_stav_display(),
                "je_fyzicky_odoslane_na_vratnicu": v.stav in ['ODOSLANE', 'SCHVALENE', 'ZAMIETNUTE'],
                "text_emailu_pre_vratnicu": v.ai_text_emailu or "Bez textu",
                "poznamka_od_vratnice_sbs": v.poznamka_sbs or "Bez poznámky",
                "ma_bezpecnostny_kluc": bool(v.bezpecnostny_kluc),
                "ma_vygenerovane_pdf": bool(v.vygenerovany_dokument),
                "suvisiaca_uloha_id": v.uloha.id if v.uloha else None,
                "technici": [f"{t.meno} {t.priezvisko}" for t in v.zamestnanci.all()],
                "vozidla": [a.spz for a in v.vozidla.all()]
            })
            
        return json.dumps(data)
    except Exception as e:
        return json.dumps({"error": str(e)})

def tool_financie_faktury_dodavatelia(hladany_vyraz: Optional[str] = None, datum_od: Optional[str] = None, datum_do: Optional[str] = None, suma_od: Optional[float] = None, suma_do: Optional[float] = None, stav_dokladu: Optional[str] = None, erp_status: Optional[str] = None, peppol_status: Optional[str] = None, limit: int = 10, offset: int = 0) -> str:
    """
    Použi na zisťovanie FINANCIÍ: nezaplatené prijaté/vystavené faktúry, cenové ponuky, nedávne úhrady, dodávatelia a nákupný košík.
    'hladany_vyraz' filtruje podľa názvu zákazníka alebo dodávateľa.
    'stav_dokladu' filtruje stavy (napr. DRAFT, NACHYSTANA, ODOSLANA, ZAPLATENA, NEZAPLATENA, STORNO).
    'erp_status' filtruje stav účtovníctva (napr. ERROR, EXPORTED, NEW, PROCESSING).
    'peppol_status' filtruje stav eFaktúry v sieti SAPI-SK (napr. ERROR, SENT, PROCESSING, NEW).
    Dátumy zadávaj vo formáte YYYY-MM-DD.
    """
    return _vykonaj_financie_crm_dotaz(
        hladany_vyraz=hladany_vyraz,
        datum_od=datum_od,
        datum_do=datum_do,
        suma_od=suma_od,
        suma_do=suma_do,
        stav_dokladu=stav_dokladu,
        erp_status=erp_status,
        peppol_status=peppol_status,
        limit=limit,
        offset=offset,
        povolene_sekcie=None,
    )


def _vykonaj_financie_crm_dotaz(hladany_vyraz: Optional[str] = None, datum_od: Optional[str] = None, datum_do: Optional[str] = None, suma_od: Optional[float] = None, suma_do: Optional[float] = None, stav_dokladu: Optional[str] = None, erp_status: Optional[str] = None, peppol_status: Optional[str] = None, limit: int = 10, offset: int = 0, povolene_sekcie: Optional[set[str]] = None, exact_zakaznik_nazov: Optional[str] = None, exact_zakaznik_id: Optional[int] = None) -> str:
    # 🛡️ DEFENSIVE TYPECASTING
    try:
        limit = max(1, min(int(limit), 100)) if limit is not None else 10
        offset = max(0, int(offset)) if offset is not None else 0
        if suma_od is not None: suma_od = safe_float(suma_od, 0.0)
        if suma_do is not None: suma_do = safe_float(suma_do, 0.0)
    except (ValueError, TypeError):
        limit, offset = 10, 0

    PrichodziaFaktura = apps.get_model('Evidencia', 'PrichodziaFaktura')
    VystavenaFaktura = apps.get_model('Evidencia', 'VystavenaFaktura')
    CenovaPonuka = apps.get_model('Evidencia', 'CenovaPonuka')
    PrijataObjednavka = apps.get_model('Evidencia', 'PrijataObjednavka')
    EvidenciaUhrad = apps.get_model('Evidencia', 'EvidenciaUhrad')
    Dodavatel = apps.get_model('Evidencia', 'Dodavatel')
    Zakaznik = apps.get_model('Evidencia', 'Zakaznik')
    Kosik = apps.get_model('Evidencia', 'KosikSPolozkamiNaDoobjednanie')
    Sklad = apps.get_model('Evidencia', 'Sklad')
    hlavny_sklad = Sklad.objects.first()
    nazov_hlavneho = f"Hlavný sklad ({hlavny_sklad.nazov})" if hlavny_sklad and hlavny_sklad.nazov and hlavny_sklad.nazov.lower() != "hlavný sklad" else "Hlavný sklad"
    
    data: dict[str, list[dict[str, Any]]] = {
        "prijate_faktury_zavazky": [],
        "vystavene_faktury_pohladavky": [],
        "cenove_ponuky": [],
        "prijate_objednavky_od_zakaznikov": [],
        "nedavne_uhrady": [],
        "zakaznici_crm_info": [],
        "dodavatelia_info": [], 
        "caka_v_kosiku_na_objednanie": []
    }
    
    try:
        d_od = to_date(datum_od)
        d_do = to_date(datum_do)

        faktury_in = PrichodziaFaktura.objects.select_related('dodavatel', 'cielovy_sklad').prefetch_related('polozky_log__navrhovany_vyrobca', 'polozky_log__filialka', 'suvisiace_objednavky', 'navrhnute_objednavky', 'ignorovane_objednavky').order_by('-datum_prijatia')
        faktury_out = VystavenaFaktura.objects.select_related('zakaznik', 'zdrojova_uloha', 'vystavil', 'zariadenie__vyrobca', 'povodna_faktura').prefetch_related('polozky__zariadenie', 'zdrojove_ponuky', 'zdrojove_objednavky', 'zdrojove_zaznamy', 'typ_servisu', 'uhrady').order_by('-datum_vystavenia')
        ponuky = CenovaPonuka.objects.select_related('zakaznik', 'vystavil', 'zariadenie__vyrobca').prefetch_related('polozky__zariadenie', 'vytvorene_ulohy', 'vyfakturovane', 'typ_servisu').order_by('-datum_vystavenia')
        objednavky = PrijataObjednavka.objects.select_related('zakaznik', 'zodpovedny_zamestnanec').prefetch_related('zariadenia__vyrobca', 'typ_servisu', 'zdrojove_ponuky', 'vytvorene_ulohy', 'navrhnute_ponuky', 'ignorovane_ponuky', 'faktury').order_by('-datum_prijatia')
        uhrady = EvidenciaUhrad.objects.select_related('vystavena_faktura__zakaznik', 'prijata_faktura__dodavatel').order_by('-datum_uhrady')
        dodavatelia = Dodavatel.objects.filter(je_anonymizovany=False).prefetch_related('zastupene_znacky', 'ai_zameranie', 'maily', 'email_optouts')
        zakaznici = Zakaznik.objects.filter(je_anonymizovany=False).select_related('user').prefetch_related('webhooks', 'maily', 'email_optouts').all()
        kosik = Kosik.objects.filter(objednane=False).select_related('dodavatel', 'filialka', 'suvisiaca_objednavka')
        
        if exact_zakaznik_id is not None:
            faktury_out = faktury_out.filter(zakaznik_id=exact_zakaznik_id, zakaznik__je_interny_profil=False).exclude(stav__in=['DRAFT', 'STORNO'])
            ponuky = ponuky.filter(zakaznik_id=exact_zakaznik_id, zakaznik__je_interny_profil=False).exclude(stav='DRAFT')
            objednavky = objednavky.filter(zakaznik_id=exact_zakaznik_id, zakaznik__je_interny_profil=False)
        elif exact_zakaznik_nazov:
            faktury_out = faktury_out.filter(zakaznik__Nazov_spolocnosti__iexact=exact_zakaznik_nazov, zakaznik__je_interny_profil=False).exclude(stav__in=['DRAFT', 'STORNO'])
            ponuky = ponuky.filter(zakaznik__Nazov_spolocnosti__iexact=exact_zakaznik_nazov, zakaznik__je_interny_profil=False).exclude(stav='DRAFT')
            objednavky = objednavky.filter(zakaznik__Nazov_spolocnosti__iexact=exact_zakaznik_nazov, zakaznik__je_interny_profil=False)

        if hladany_vyraz:
            faktury_in = faktury_in.filter(Q(dodavatel__Nazov_spolocnosti__icontains=hladany_vyraz) | Q(variabilny_symbol__icontains=hladany_vyraz))
            faktury_out = faktury_out.filter(Q(zakaznik__Nazov_spolocnosti__icontains=hladany_vyraz) | Q(cislo_faktury__icontains=hladany_vyraz) | Q(variabilny_symbol__icontains=hladany_vyraz))
            ponuky = ponuky.filter(Q(zakaznik__Nazov_spolocnosti__icontains=hladany_vyraz) | Q(cislo_ponuky__icontains=hladany_vyraz))
            objednavky = objednavky.filter(Q(zakaznik__Nazov_spolocnosti__icontains=hladany_vyraz) | Q(cislo_objednavky_klienta__icontains=hladany_vyraz) | Q(cislo_objednavky_interne__icontains=hladany_vyraz))
            uhrady = uhrady.filter(Q(vystavena_faktura__zakaznik__Nazov_spolocnosti__icontains=hladany_vyraz) | Q(prijata_faktura__dodavatel__Nazov_spolocnosti__icontains=hladany_vyraz) | Q(cislo_dokladu__icontains=hladany_vyraz) | Q(vystavena_faktura__cislo_faktury__icontains=hladany_vyraz))
            dodavatelia = dodavatelia.filter(Nazov_spolocnosti__icontains=hladany_vyraz)
            zakaznici = zakaznici.filter(Nazov_spolocnosti__icontains=hladany_vyraz)
            kosik = kosik.filter(Q(dodavatel__Nazov_spolocnosti__icontains=hladany_vyraz) | Q(nazov_polozky__icontains=hladany_vyraz))

        if d_od:
            faktury_in = faktury_in.filter(datum_prijatia__date__gte=d_od)
            faktury_out = faktury_out.filter(datum_vystavenia__gte=d_od)
            ponuky = ponuky.filter(datum_vystavenia__gte=d_od)
            objednavky = objednavky.filter(datum_prijatia__gte=d_od)
            uhrady = uhrady.filter(datum_uhrady__gte=d_od)
        if d_do:
            faktury_in = faktury_in.filter(datum_prijatia__date__lte=d_do)
            faktury_out = faktury_out.filter(datum_vystavenia__lte=d_do)
            ponuky = ponuky.filter(datum_vystavenia__lte=d_do)
            objednavky = objednavky.filter(datum_prijatia__lte=d_do)
            uhrady = uhrady.filter(datum_uhrady__lte=d_do)
        if suma_od is not None:
            faktury_in = faktury_in.filter(suma_na_uhradu__gte=suma_od)
            faktury_out = faktury_out.filter(celkom_s_dph__gte=suma_od)
            ponuky = ponuky.filter(celkom_s_dph__gte=suma_od)
            objednavky = objednavky.filter(celkova_suma_bez_dph__gte=suma_od)
            uhrady = uhrady.filter(suma_uhrady__gte=suma_od)
        if suma_do is not None:
            faktury_in = faktury_in.filter(suma_na_uhradu__lte=suma_do)
            faktury_out = faktury_out.filter(celkom_s_dph__lte=suma_do)
            ponuky = ponuky.filter(celkom_s_dph__lte=suma_do)
            objednavky = objednavky.filter(celkova_suma_bez_dph__lte=suma_do)
            uhrady = uhrady.filter(suma_uhrady__lte=suma_do)
            
        if stav_dokladu:
            stav_upper = stav_dokladu.upper()
            
            # Bezpečné filtrovanie pre PrichodziaFaktura (vstup)
            if stav_upper in ['ZAPLATENA', 'NEZAPLATENA']:
                faktury_in = faktury_in.filter(stav_uhrady__iexact=stav_upper)
            
            # Bezpečné mapovanie pre VystavenaFaktura (výstup)
            if stav_upper in ['DRAFT', 'NACHYSTANA', 'ODOSLANA', 'ZAPLATENA', 'PO_SPLATNOSTI', 'STORNO']:
                faktury_out = faktury_out.filter(stav__iexact=stav_upper)
            elif stav_upper == 'NEZAPLATENA':
                faktury_out = faktury_out.filter(stav__in=['NACHYSTANA', 'ODOSLANA', 'PO_SPLATNOSTI'])
                
            # Filtrovanie ponúk a objednávok iba ak stav sémanticky sedí
            if stav_upper in ['DRAFT', 'SENT', 'ACCEPTED', 'CONVERTED', 'REJECTED']:
                ponuky = ponuky.filter(stav__iexact=stav_upper)
            if stav_upper in ['NOVA', 'SCHVALENA', 'VYBAVENA', 'VYFAKTUROVANA', 'ZAMIETNUTA']:
                objednavky = objednavky.filter(stav__iexact=stav_upper)
            
        if erp_status:
            faktury_in = faktury_in.filter(erp_export_status__iexact=erp_status)
            faktury_out = faktury_out.filter(erp_export_status__iexact=erp_status)
            uhrady = uhrady.filter(erp_export_status__iexact=erp_status)
            
        if peppol_status:
            faktury_out = faktury_out.filter(peppol_status__iexact=peppol_status)
            
        if povolene_sekcie is None or "prijate_faktury_zavazky" in povolene_sekcie:
            for f in faktury_in[offset:offset+limit]:
                data["prijate_faktury_zavazky"].append({
                    "faktura_id": f.id, 
                    "stav_uhrady": f.get_stav_uhrady_display(),
                    "zdroj_prijatia": f.get_zdroj_display(), 
                    "dodavatel": f.dodavatel.Nazov_spolocnosti if f.dodavatel else "Neznámy",
                    "cielovy_sklad": f.cielovy_sklad.nazov if f.cielovy_sklad else nazov_hlavneho,
                    "datum_prijatia": f.datum_prijatia.strftime('%d.%m.%Y') if f.datum_prijatia else "Neznáme",
                    "suma_eur": float(f.suma_na_uhradu) if f.suma_na_uhradu else 0.0,
                    "variabilny_symbol": f.variabilny_symbol,
                    "kod_clenenia_dph": f.get_kod_clenenia_dph_display(),
                    "iban": f.iban or "Neuvedený",
                    "splatnost": f.datum_splatnosti.strftime('%d.%m.%Y') if f.datum_splatnosti else "Neznáme",
                    "povolit_fyzicky_prijem": f.prijat_na_sklad,
                    "notif_prijem_tg": f.notif_prijem_tg,
                    "notif_prijem_mail": f.notif_prijem_mail,
                    "notif_prijem_discord": f.notif_prijem_discord,
                    "ma_fyzicky_subor": bool(f.subor),
                    "ma_peppol_xml": bool(f.xml_subor),
                    "ma_peppol_pdf_vizualizaciu": bool(f.vizualizacia_subor),
                    "peppol_document_id": f.peppol_document_id or "Nepriradené",
                    "xml_sha256_hash": f.xml_sha256_hash or "Nezapečatené",
                    "spracovane_ai": f.spracovane,
                    "erp_status": f.get_erp_export_status_display(),
                    "erp_id": f.erp_id or "Nepriradené",
                    "erp_last_exported_at": f.erp_last_exported_at.strftime('%d.%m.%Y %H:%M') if f.erp_last_exported_at else "Nikdy",
                    "poznamka_ai": f.poznamka or "Bez poznámky",
                    "sparovane_objednavky": [o.cislo_objednavky for o in f.suvisiace_objednavky.all()],
                    "ai_navrhnute_objednavky": [o.cislo_objednavky for o in f.navrhnute_objednavky.all()],
                    "ignorovane_objednavky": [o.cislo_objednavky for o in f.ignorovane_objednavky.all()],
                    "erp_predkontacia_hlavicka": f.erp_predkontacia or "Podľa globálnych nastavení / dodávateľa",
                    "erp_chybovy_log": f.erp_error_log or "Bez chýb",
                    "obsah_faktury": [f"{p.mnozstvo} {p.navrhovana_merna_jednotka or 'ks'} {p.najdeny_kod_faktura} (AI Návrh: {p.navrhovany_nazov or 'Žiadny'}, SKU: {p.navrhovane_katalogove_cislo or 'Žiadne'}, EAN: {p.navrhovany_ean or 'Žiadny'}, Hlavná kategória: {p.hlavna_kategoria or 'Neznáma'}, Podkategória: {p.navrhovana_kategoria or 'Neznáma'}, Typ: {p.get_navrhovany_typ_display() if p.navrhovany_typ else 'Neznámy'}, Výrobca: {p.navrhovany_vyrobca.nazov_vyrobcu if p.navrhovany_vyrobca else 'Neznámy'}, Popis: {p.navrhovany_popis or 'Žiadny'}, Sklad: {p.filialka.nazov if p.filialka else nazov_hlavneho}, Predkontácia: {p.erp_predkontacia or 'Z hlavičky'}) ({float(p.nakupna_cena) if p.nakupna_cena else 0.0} {getattr(f, 'mena', None) or get_firemna_mena()}/ks, DPH: {float(p.navrhovana_sadzba_dph) if p.navrhovana_sadzba_dph is not None else float(apps.get_model('ISASaplikacia', 'GlobalneNastavenia').get_zakladna_dph())}%) - {'✅ Fyzicky naskladnené' if p.uspesne else '⏳ Čaká na potvrdenie v Naskladňovacom dashbaorde'}" for p in f.polozky_log.all()]
                })

        if povolene_sekcie is None or "vystavene_faktury_pohladavky" in povolene_sekcie:
            for f_out in faktury_out[offset:offset+limit]:
                mena_dokladu = getattr(f_out, 'mena', None) or get_firemna_mena()
                if exact_zakaznik_nazov or exact_zakaznik_id is not None:
                    polozky = [f"{p.mnozstvo} {p.merna_jednotka} {getattr(p, 'obchodny_nazov', None) or (p.get_typ_polozky_display() if (getattr(p, 'skryt_vyrobcu', False) or getattr(p, 'object_id', None)) else p.nazov)} (Typ: {p.get_typ_polozky_display()}, {p.jednotkova_cena_bez_dph} {mena_dokladu}/{p.merna_jednotka}, Zľava: {p.zlava_percento}%, DPH: {p.sadzba_dph}%, Po zľave bez DPH: {p.cena_po_zlave_bez_dph} {mena_dokladu}, Hodnota DPH: {p.hodnota_dph} {mena_dokladu}, Spolu s DPH: {p.spolu_s_dph} {mena_dokladu})" + (f" [{get_industry_term('zariadenie_fallback_nazov', 'Zariadenie')}: {p.zariadenie.vyrobne_cislo}]" if p.zariadenie else "") for p in f_out.polozky.all()]
                else:
                    polozky = [f"{p.mnozstvo} {p.merna_jednotka} {p.nazov} (Typ: {p.get_typ_polozky_display()}, {p.jednotkova_cena_bez_dph} {mena_dokladu}/{p.merna_jednotka}, Zľava: {p.zlava_percento}%, DPH: {p.sadzba_dph}%, Účet: {p.ucet_vynosov or 'Nevyplnený'}, Po zľave bez DPH: {p.cena_po_zlave_bez_dph} {mena_dokladu}, Hodnota DPH: {p.hodnota_dph} {mena_dokladu}, Spolu s DPH: {p.spolu_s_dph} {mena_dokladu})" + (" [➕ PRÁCA NAVIAC]" if p.praca_naviac else "") + (f" [{get_industry_term('zariadenie_fallback_nazov', 'Zariadenie')}: {p.zariadenie.vyrobne_cislo}]" if p.zariadenie else "") + (" [Skladová položka]" if p.object_id else "") for p in f_out.polozky.all()]
                data["vystavene_faktury_pohladavky"].append({
                    "cislo_faktury": f_out.cislo_faktury,
                    "zakaznik": f_out.zakaznik.Nazov_spolocnosti if f_out.zakaznik else "Neznámy",
                    "datum_vystavenia": f_out.datum_vystavenia.strftime('%d.%m.%Y') if f_out.datum_vystavenia else "Neznáme",
                    "datum_dodania": f_out.datum_dodania.strftime('%d.%m.%Y') if f_out.datum_dodania else "Neznáme",
                    "suma_bez_dph_eur": float(f_out.celkom_bez_dph) if f_out.celkom_bez_dph else 0.0,
                    "suma_dph_eur": float(f_out.celkom_dph) if f_out.celkom_dph else 0.0,
                    "suma_eur": float(f_out.celkom_s_dph) if f_out.celkom_s_dph else 0.0,
                    "uhradene_doteraz_eur": sum(float(u.suma_uhrady) for u in f_out.uhrady.all() if u.suma_uhrady is not None) if hasattr(f_out, 'uhrady') else 0.0,
                    "mena": f_out.mena,
                    "vystavil_meno": f_out.vystavil_meno or ((f_out.vystavil.get_full_name() or ("Oprávnená osoba" if (exact_zakaznik_nazov or exact_zakaznik_id is not None) else f_out.vystavil.username)) if f_out.vystavil else "Neznáme"),
                    "typ_dokladu": f_out.get_typ_dokladu_display(),
                    "kod_clenenia_dph": f_out.get_kod_clenenia_dph_display(),
                    "stav": f_out.get_stav_display(),
                    "sposob_uhrady": f_out.get_sposob_uhrady_display(),
                    "prenos_danovej_povinnosti": f_out.prenos_danovej_povinnosti,
                    "variabilny_symbol": f_out.variabilny_symbol or "Neuvedený",
                    "konstantny_symbol": f_out.konstantny_symbol or "Neuvedený",
                    "splatnost": f_out.datum_splatnosti.strftime('%d.%m.%Y') if f_out.datum_splatnosti else "Neznáme",
                    "datum_uhrady": f_out.datum_uhrady.strftime('%d.%m.%Y') if f_out.datum_uhrady else "Nezaplatené",
                    "po_number_klienta": f_out.cislo_objednavky_klienta or "Neuvedené",
                    "dodavatel_snapshot_nazov": f_out.dodavatel_nazov or "Neuvedené",
                    "dodavatel_snapshot_sidlo": f_out.dodavatel_sidlo or "Neuvedené",
                    "dodavatel_snapshot_ico": f_out.dodavatel_ico or "Neuvedené",
                    "dodavatel_snapshot_dic": f_out.dodavatel_dic or "Neuvedené",
                    "dodavatel_snapshot_ic_dph": f_out.dodavatel_ic_dph or "Neuvedené",
                    "dodavatel_snapshot_iban": f_out.dodavatel_iban or "Neuvedené",
                    "dodavatel_snapshot_swift": f_out.dodavatel_swift or "Neuvedené",
                    "dodavatel_snapshot_banka": f_out.dodavatel_banka or "Neuvedené",
                    "dodavatel_snapshot_zapis": f_out.dodavatel_zapis or "Neuvedené",
                    "odberatel_snapshot_nazov": f_out.odberatel_nazov or "Neuvedené",
                    "odberatel_snapshot_ulica": f_out.odberatel_ulica or "Neuvedené",
                    "odberatel_snapshot_mesto": f_out.odberatel_mesto or "Neuvedené",
                    "odberatel_snapshot_psc": f_out.odberatel_psc or "Neuvedené",
                    "odberatel_snapshot_stat": f_out.odberatel_stat or "Neuvedené",
                    "odberatel_snapshot_sidlo": f_out.odberatel_sidlo or "Neuvedené",
                    "odberatel_snapshot_ico": f_out.odberatel_ico or "Neuvedené",
                    "odberatel_snapshot_dic": f_out.odberatel_dic or "Neuvedené",
                    "odberatel_snapshot_ic_dph": f_out.odberatel_ic_dph or "Neuvedené",
                    "ma_bezpecnostny_kluc": bool(f_out.bezpecnostny_kluc),
                    "zdrojove_ponuky": [p.cislo_ponuky for p in f_out.zdrojove_ponuky.all()],
                    "zdrojova_uloha_id": f_out.zdrojova_uloha.id if f_out.zdrojova_uloha else "Bez úlohy",
                    "zdrojove_objednavky": [o.cislo_objednavky_interne for o in f_out.zdrojove_objednavky.all()],
                    "zdrojove_zaznamy_ids": [z.id for z in f_out.zdrojove_zaznamy.all()],
                    "zariadenie": (
                        f"{(getattr(f_out.zariadenie, 'model_zariadenia', '') or '').strip() or get_industry_term('zariadenie_fallback_nazov', 'Zariadenie')} (SN: {getattr(f_out.zariadenie, 'vyrobne_cislo', '')})"
                        if (f_out.zariadenie and (exact_zakaznik_nazov or exact_zakaznik_id is not None))
                        else (str(f_out.zariadenie) if f_out.zariadenie else "Žiadne")
                    ),
                    "typy_prac": [t.nazov for t in f_out.typ_servisu.all()],
                    "zdrojovy_sklad_id": f_out.zdrojovy_sklad_id or "Neuvedený",
                    "odpisane_zo_skladu": f_out.odpisat_zo_skladu,
                    "notif_odpis_tg": f_out.notif_odpis_tg,
                    "notif_odpis_mail": f_out.notif_odpis_mail,
                    "notif_odpis_discord": f_out.notif_odpis_discord,
                    "notif_majitel_tg": f_out.notif_majitel_tg,
                    "notif_majitel_mail": f_out.notif_majitel_mail,
                    "notif_majitel_discord": f_out.notif_majitel_discord,
                    "erp_status": f_out.get_erp_export_status_display(),
                    "erp_id": f_out.erp_id or "Nepriradené",
                    "erp_last_exported_at": f_out.erp_last_exported_at.strftime('%d.%m.%Y %H:%M') if f_out.erp_last_exported_at else "Nikdy",
                    "erp_chybovy_log": f_out.erp_error_log or "Bez chýb",
                    "ma_vygenerovane_pdf": bool(f_out.pdf_dokument),
                    "ai_auditor_poznamka": f_out.ai_poznamka or "Bez poznámky",
                    "poznamka_na_fakture": f_out.poznamka_pre_klienta or "Žiadna",
                    "povodna_faktura_cislo": f_out.povodna_faktura.cislo_faktury if f_out.povodna_faktura else None,
                    "peppol_status": f_out.get_peppol_status_display(),
                    "peppol_transaction_id": f_out.peppol_transaction_id or "Nepriradené",
                    "peppol_odoslane_dna": f_out.peppol_odoslane_dna.strftime('%d.%m.%Y %H:%M:%S') if f_out.peppol_odoslane_dna else "Neodoslané",
                    "peppol_xml_hash": f_out.peppol_xml_hash or "Nezapečatené",
                    "peppol_chybovy_log": f_out.peppol_error_log or "Bez chýb",
                    "ma_peppol_xml": bool(f_out.peppol_xml_dokument),
                    "obsah_faktury": polozky
                })
            
        # 🟢 Pridaný loop pre Zákazníkov (CRM)
        if povolene_sekcie is None or "zakaznici_crm_info" in povolene_sekcie:
            for z in zakaznici[offset:offset+limit]:
                data["zakaznici_crm_info"].append({
                    "nazov": f"{z.Nazov_spolocnosti} (Naša firma / Centrála)" if getattr(z, 'je_interny_profil', False) else z.Nazov_spolocnosti,
                    "je_interny_profil_nasej_firmy": getattr(z, 'je_interny_profil', False),
                    "ico": z.ico,
                    "kontaktna_osoba": z.kontaktna_osoba or "Neuvedená",
                    "oslovenie": z.get_oslovenie_rod_display() if z.oslovenie_rod else "Neuvedené",
                    "datum_narodenia": z.datum_narodenia.strftime('%d.%m.%Y') if z.datum_narodenia else "Nezadané",
                    "telefon": z.telefon or "Neuvedený",
                    "email": z.email or "Neuvedený",
                    "email_vratnice_sbs": z.email_vratnice or "Neuvedený",
                    "telefon_vratnica": z.telefon_vratnica or "Neuvedený",
                    "web": z.domena or "Neuvedený",
                    "subdomena_klientskeho_portalu": z.subdomena or "Nenastavená",
                    "vyzaduje_povolenie_vstupu": z.vyzaduje_povolenie_vstupu,
                    "instrukcie_vstupu": z.instrukcie_vstupu or "Žiadne",
                    "ma_vlastnu_sablonu_vstupu": bool(z.sablona_vstupu),
                    "url_vstupneho_portalu": z.url_vstupneho_portalu or "Žiadna",
                    "cena_elektriny_za_kwh_eur": float(z.cena_za_kwh_eur) if z.cena_za_kwh_eur is not None else 0.0,
                    "emisny_faktor_co2": float(z.emisny_faktor_co2) if z.emisny_faktor_co2 is not None else 0.0,
                    "zobrazit_ako_referenciu_web": z.zobrazit_ako_referenciu,
                    "b2b_zlava_percenta": float(z.b2b_zlava_percenta or 0.0),
                    "ma_b2b_ucet_na_webe": bool(z.user),
                    "predvolena_splatnost_dni": z.predvolena_splatnost_dni,
                    "reklamacna_lehota_dni": z.reklamacna_lehota_dni,
                    "fakturacne_udaje": f"IČO: {z.ico}, DIČ: {z.dic}, IČ DPH: {z.ic_dph}",
                    "adresa": f"{z.ulica_a_cislo or ''}, {z.psc or ''} {z.mesto or ''}, {z.stat or ''}".strip(" ,"),
                    "iban": f"****{z.iban[-4:]}" if z.iban and len(z.iban) >= 4 else ("Zadané (Skryté)" if z.iban else "Neuvedený"),
                    "interne_poznamky": z.Ostatne or "Žiadne",
                    "pocet_emailov_v_crm": z.maily.count() if hasattr(z, 'maily') else 0,
                    "aktivne_api_webhooky": [f"{w.nazov} ({w.get_udalost_display()}) - {('AKTÍVNY' if w.je_aktivny else f'DEAKTIVOVANÝ ({w.dovod_deaktivacie})' if w.dovod_deaktivacie else 'DEAKTIVOVANÝ (Chyba)')}" for w in z.webhooks.all()] if hasattr(z, 'webhooks') else [],
                    "erp_status": z.get_erp_export_status_display(),
                    "erp_id": z.erp_id or "Nepriradené",
                    "erp_last_exported_at": z.erp_last_exported_at.strftime('%d.%m.%Y %H:%M') if z.erp_last_exported_at else "Nikdy",
                    "erp_chybovy_log": z.erp_error_log or "Bez chýb",
                    "gps_suradnice": f"{z.lat}, {z.lon}" if z.lat and z.lon else "Neznáme",
                    "ma_aktivny_email_opt_out": any(o.aktivny for o in z.email_optouts.all()) if hasattr(z, 'email_optouts') else False,
                    "email_optout_zaznamy": [
                        {
                            "id": o.id,
                            "email": o.email,
                            "aktivny": o.aktivny,
                            "zdroj": o.get_zdroj_display(),
                            "rozsah": o.get_rozsah_display(),
                            "dovod": o.dovod,
                            "poznamka": o.poznamka or "Žiadna",
                            "vytvorene": o.vytvorene.strftime('%d.%m.%Y %H:%M') if o.vytvorene else "Neznáme",
                            "aktualizovane": o.aktualizovane.strftime('%d.%m.%Y %H:%M') if o.aktualizovane else "Neznáme",
                        }
                        for o in z.email_optouts.all()
                    ] if hasattr(z, 'email_optouts') else []
                })

        if povolene_sekcie is None or "cenove_ponuky" in povolene_sekcie:
            for p in ponuky[offset:offset+limit]:
                mena_ponuky = getattr(p, 'mena', None) or get_firemna_mena()
                if exact_zakaznik_nazov or exact_zakaznik_id is not None:
                    polozky = [f"{pol.mnozstvo} {pol.merna_jednotka} {getattr(pol, 'obchodny_nazov', None) or (pol.get_typ_polozky_display() if (getattr(pol, 'skryt_vyrobcu', False) or getattr(pol, 'object_id', None)) else pol.nazov)} (Typ: {pol.get_typ_polozky_display()}, {pol.jednotkova_cena_bez_dph} {mena_ponuky}/{pol.merna_jednotka}, Zľava: {pol.zlava_percento}%, DPH: {pol.sadzba_dph}%)" + (f" [{get_industry_term('zariadenie_fallback_nazov', 'Zariadenie')}: {pol.zariadenie.vyrobne_cislo}]" if pol.zariadenie else "") for pol in p.polozky.all()]
                else:
                    polozky = [f"{pol.mnozstvo} {pol.merna_jednotka} {pol.nazov} (Typ: {pol.get_typ_polozky_display()}, {pol.jednotkova_cena_bez_dph} {mena_ponuky}/{pol.merna_jednotka}, Zľava: {pol.zlava_percento}%, DPH: {pol.sadzba_dph}%)" + (f" [{get_industry_term('zariadenie_fallback_nazov', 'Zariadenie')}: {pol.zariadenie.vyrobne_cislo}]" if pol.zariadenie else "") + (" [Skladová položka]" if pol.object_id else "") for pol in p.polozky.all()]
                data["cenove_ponuky"].append({
                    "cislo_ponuky": p.cislo_ponuky,
                    "zakaznik": p.zakaznik.Nazov_spolocnosti if p.zakaznik else "Neznámy",
                    "zariadenie": (
                        f"{(getattr(p.zariadenie, 'model_zariadenia', '') or '').strip() or get_industry_term('zariadenie_fallback_nazov', 'Zariadenie')} (SN: {getattr(p.zariadenie, 'vyrobne_cislo', '')})"
                        if (p.zariadenie and (exact_zakaznik_nazov or exact_zakaznik_id is not None))
                        else (str(p.zariadenie) if p.zariadenie else "Žiadne")
                    ),
                    "typy_prac": [t.nazov for t in p.typ_servisu.all()],
                    "suma_bez_dph_pred_zlavou_eur": float(p.celkom_bez_dph_pred_zlavou) if p.celkom_bez_dph_pred_zlavou else 0.0,
                    "suma_bez_dph_eur": float(p.celkom_bez_dph) if p.celkom_bez_dph else 0.0,
                    "suma_dph_eur": float(p.celkom_dph) if p.celkom_dph else 0.0,
                    "suma_s_dph_eur": float(p.celkom_s_dph) if p.celkom_s_dph else 0.0,
                    "hodnota_zlavy_eur": float(p.hodnota_zlavy) if p.hodnota_zlavy else 0.0,
                    "stav": p.get_stav_display(),
                    "vystavil_meno": p.vystavil.get_full_name() if p.vystavil else "Neznáme",
                    "vystavene": p.datum_vystavenia.strftime('%d.%m.%Y') if p.datum_vystavenia else "Neznáme",
                    "prenos_danovej_povinnosti": p.prenos_danovej_povinnosti,
                    "cas_schvalenia_klientom": p.cas_schvalenia.strftime('%d.%m.%Y %H:%M') if p.cas_schvalenia else "Neschválené",
                    "meno_schvalitela": p.meno_schvalitela or "Nepodpísané",
                    "ip_adresa_schvalitela": p.ip_adresa_schvalitela or "Neznáma",
                    "ma_bezpecnostny_kluc": bool(p.bezpecnostny_kluc),
                    "ma_vygenerovane_pdf": bool(p.pdf_dokument),
                    "globalna_zlava_percento": float(p.globalna_zlava_percento or 0.0),
                    "vygenerovane_ulohy_ids": [u.id for u in p.vytvorene_ulohy.all()],
                    "vyfakturovane_vo_fakturach": (
                        [f.cislo_faktury for f in getattr(p, 'vyfakturovane').all() if f.stav not in ('DRAFT', 'STORNO') and f.cislo_faktury]
                        if (exact_zakaznik_nazov or exact_zakaznik_id is not None)
                        else [f.cislo_faktury or "DRAFT" for f in getattr(p, 'vyfakturovane').all()]
                    ),
                    "interne_poznamky": p.interne_poznamky or "Žiadne",
                    "uvodny_text": p.ai_uvodny_text or "Žiadny",
                    "zaverecne_podmienky": p.zavrecny_text or "Žiadne",
                    "obsah_ponuky": polozky
                })

        # 🟢 Pridaný loop pre Prijaté objednávky
        if povolene_sekcie is None or "prijate_objednavky_od_zakaznikov" in povolene_sekcie:
            for o in objednavky[offset:offset+limit]:
                data["prijate_objednavky_od_zakaznikov"].append({
                    "cislo_interne": o.cislo_objednavky_interne,
                    "po_number_klienta": o.cislo_objednavky_klienta or "Neuvedené",
                    "zakaznik": o.zakaznik.Nazov_spolocnosti if o.zakaznik else "Neznámy",
                    "stav": o.get_stav_display(),
                    "priorita": o.get_priorita_display(),
                    "forma_prijatia": o.get_forma_prijatia_display(),
                    "suma_bez_dph_eur": float(o.celkova_suma_bez_dph) if o.celkova_suma_bez_dph else 0.0,
                    "datum_prijatia": o.datum_prijatia.strftime('%d.%m.%Y') if o.datum_prijatia else "Neznáme",
                    "termin_dodania": o.termin_dodania.strftime('%d.%m.%Y') if o.termin_dodania else "Nezadaný",
                    "vystavil_za_klienta": o.vystavil_za_klienta or "Neuvedené",
                    "specificky_email_pre_fakturu": o.specificky_email_pre_fakturu or "Neuvedený",
                    "odkaz_na_portal": o.odkaz_na_portal or "Žiadny",
                    "vyzadovana_splatnost_dni": o.vyzadovana_splatnost_dni or "Štandardná",
                    "zdrojove_ponuky": [p.cislo_ponuky for p in o.zdrojove_ponuky.all()],
                    "ai_navrhnute_ponuky": [p.cislo_ponuky for p in o.navrhnute_ponuky.all()],
                    "ignorovane_ponuky_ai": [p.cislo_ponuky for p in o.ignorovane_ponuky.all()],
                    "vygenerovane_ulohy_ids": [u.id for u in o.vytvorene_ulohy.all()],
                    "vyfakturovane_vo_fakturach": (
                        [f.cislo_faktury for f in getattr(o, 'faktury').all() if f.stav not in ('DRAFT', 'STORNO') and f.cislo_faktury]
                        if (exact_zakaznik_nazov or exact_zakaznik_id is not None)
                        else [f.cislo_faktury or "DRAFT" for f in getattr(o, 'faktury').all()]
                    ),
                    "zodpovedny_dispecer": o.zodpovedny_zamestnanec.get_full_name() if o.zodpovedny_zamestnanec else "Nepridelené",
                    "poziadavka_klienta_text": o.poznamka_klienta or o.text_emailu or "Bez textu",
                    "interna_poznamka": o.interna_poznamka or "Žiadna",
                    "typy_poziadovanych_prac": [t.nazov for t in o.typ_servisu.all()],
                    "tyka_sa_zariadeni": [
                        (
                            f"{(getattr(z, 'model_zariadenia', '') or '').strip() or get_industry_term('zariadenie_fallback_nazov', 'Zariadenie')} (SN: {getattr(z, 'vyrobne_cislo', '')})"
                            if (exact_zakaznik_nazov or exact_zakaznik_id is not None)
                            else str(z)
                        )
                        for z in o.zariadenia.all()
                    ],
                    "obsahuje_prilohu_subor": bool(o.subor_objednavky)
                })

        if povolene_sekcie is None or "nedavne_uhrady" in povolene_sekcie:
            for u in uhrady[offset:offset+limit]:
                partner = u.vystavena_faktura.zakaznik.Nazov_spolocnosti if u.vystavena_faktura and u.vystavena_faktura.zakaznik else (u.prijata_faktura.dodavatel.Nazov_spolocnosti if u.prijata_faktura and u.prijata_faktura.dodavatel else "Neznámy")
                doklad = u.vystavena_faktura.cislo_faktury if u.vystavena_faktura else (f"Prijatá FA ID:{u.prijata_faktura.id}" if u.prijata_faktura else "Neznámy doklad")
                data["nedavne_uhrady"].append({
                    "typ": u.get_typ_uhrady_display(),
                    "partner": partner,
                    "doklad": doklad,
                    "cislo_bankoveho_vypisu": u.cislo_dokladu or "Neuvedené",
                    "protiucet_iban": u.protiucet_iban or "Neuvedený",
                    "poznamka": u.poznamka or "Bez poznámky",
                    "suma_eur": float(u.suma_uhrady) if u.suma_uhrady else 0.0,
                    "mena": u.mena,
                    "datum": u.datum_uhrady.strftime('%d.%m.%Y') if u.datum_uhrady else "Neznáme",
                    "erp_status": u.get_erp_export_status_display(),
                    "erp_id": u.erp_id or "Nepriradené",
                    "erp_last_exported_at": u.erp_last_exported_at.strftime('%d.%m.%Y %H:%M') if u.erp_last_exported_at else "Nikdy",
                    "erp_chybovy_log": u.erp_error_log or "Bez chýb"
                })
            
        if povolene_sekcie is None or "dodavatelia_info" in povolene_sekcie:
            for d in dodavatelia[offset:offset+limit]:
                znacky = [z.nazov_vyrobcu for z in d.zastupene_znacky.all()]
                data["dodavatelia_info"].append({
                    "nazov": d.Nazov_spolocnosti, "kontakt_osoba": d.kontaktna_osoba,
                    "oslovenie": d.get_oslovenie_rod_display() if d.oslovenie_rod else "Neuvedené",
                    "datum_narodenia": d.datum_narodenia.strftime('%d.%m.%Y') if d.datum_narodenia else "Nezadané",
                    "telefon": d.telefon or "Neuvedený", "email": d.email or "Neuvedený",
                    "fakturacne_udaje": f"IČO: {d.ico}, DIČ: {d.dic}, IČ DPH: {d.ic_dph}",
                    "adresa": f"{d.ulica_a_cislo}, {d.psc} {d.mesto}, {d.stat}",
                    "iban": f"****{d.iban[-4:]}" if d.iban and len(d.iban) >= 4 else ("Zadané (Skryté)" if d.iban else "Neuvedený"),
                    "predvolena_splatnost_dni": d.predvolena_splatnost_dni, 
                    "zastupuje_znacky": znacky, "web": d.domena,
                    "ai_zameranie": [mc._meta.verbose_name for ct in d.ai_zameranie.all() if (mc := ct.model_class()) is not None],
                    "interne_poznamky": d.ostatne or "Žiadne",
                    "pocet_emailov_v_crm": d.maily.count() if hasattr(d, 'maily') else 0,
                    "erp_predkontacia": d.erp_predkontacia or "Podľa globálnych nastavení",
                    "erp_status": d.get_erp_export_status_display(),
                    "erp_id": d.erp_id or "Nepriradené",
                    "erp_last_exported_at": d.erp_last_exported_at.strftime('%d.%m.%Y %H:%M') if d.erp_last_exported_at else "Nikdy",
                    "erp_chybovy_log": d.erp_error_log or "Bez chýb",
                    "ma_aktivny_email_opt_out": any(o.aktivny for o in d.email_optouts.all()) if hasattr(d, 'email_optouts') else False,
                    "email_optout_zaznamy": [
                        {
                            "id": o.id,
                            "email": o.email,
                            "aktivny": o.aktivny,
                            "zdroj": o.get_zdroj_display(),
                            "rozsah": o.get_rozsah_display(),
                            "dovod": o.dovod,
                            "poznamka": o.poznamka or "Žiadna",
                            "vytvorene": o.vytvorene.strftime('%d.%m.%Y %H:%M') if o.vytvorene else "Neznáme",
                            "aktualizovane": o.aktualizovane.strftime('%d.%m.%Y %H:%M') if o.aktualizovane else "Neznáme",
                        }
                        for o in d.email_optouts.all()
                    ] if hasattr(d, 'email_optouts') else []
                })
            
        if povolene_sekcie is None or "caka_v_kosiku_na_objednanie" in povolene_sekcie:
            for k in kosik[offset:offset+limit]:
                data["caka_v_kosiku_na_objednanie"].append({
                    "polozka": k.nazov_polozky,
                    "stav_skladu_pri_pridani": f"{k.aktualny_stav} {k.merna_jednotka}",
                    "potrebne_mnozstvo": f"{k.mnozstvo_na_objednanie} {k.merna_jednotka}",
                    "preferovany_dodavatel": k.dodavatel.Nazov_spolocnosti if k.dodavatel else "Nezaradený",
                    "cielovy_sklad": k.filialka.nazov if k.filialka else nazov_hlavneho,
                    "datum_pridania_do_kosika": k.datum_zistenia.strftime('%d.%m.%Y %H:%M') if k.datum_zistenia else "Neznáme",
                    "datum_odoslania": k.datum_odoslania.strftime('%d.%m.%Y %H:%M') if k.datum_odoslania else "Zatiaľ neodoslané",
                    "suvisiaca_objednavka_pdf": k.suvisiaca_objednavka.cislo_objednavky if k.suvisiaca_objednavka else "Zatiaľ neobjednané"
                })
            
        if povolene_sekcie is not None:
            data = {k: v for k, v in data.items() if k in povolene_sekcie}

        return json.dumps(data)

    except Exception as e:
        return json.dumps({"error": str(e)})


def tool_fleet_vozidla_jazdy(spz_alebo_vodic: Optional[str] = None, limit: int = 10, offset: int = 0) -> str:
    """
    Použi na získanie informácií o VOZOVOM PARKU, platnosti STK, EVIDENCII JÁZD a TANKOVANIACH.
    """
    return _vykonaj_fleet_dotaz(spz_alebo_vodic=spz_alebo_vodic, limit=limit, offset=offset)


def _vykonaj_fleet_dotaz(spz_alebo_vodic: Optional[str] = None, limit: int = 10, offset: int = 0, exact_sukromna_osoba: Optional[str] = None, exact_sukromna_osoba_id: Optional[int] = None) -> str:
    # 🛡️ DEFENSIVE TYPECASTING
    try:
        limit = max(1, min(int(limit), 100)) if limit is not None else 10
        offset = max(0, int(offset)) if offset is not None else 0
    except (ValueError, TypeError):
        limit, offset = 10, 0

    Vozidlo = apps.get_model('Evidencia', 'Vozidlo')
    Jazdy = apps.get_model('Evidencia', 'EvidenciaJazd')
    Tankovanie = apps.get_model('Evidencia', 'Tankovanie')
    
    data: dict[str, list[dict[str, Any]]] = {"vozidla_stav": [], "posledne_jazdy": [], "nedavne_tankovania": []}
    
    try:
        vozidla = Vozidlo.objects.select_related('priradeny_zamestnanec', 'sukromna_osoba').prefetch_related('tankovania')
        jazdy_qs = Jazdy.objects.select_related('vozidlo', 'zamestnanec', 'sukromna_osoba', 'zakaznik', 'uloha', 'zakaznik_start', 'filialka_start', 'dodavatel_start', 'zamestnanec_start', 'sukromna_osoba_start', 'filialka_ciel', 'dodavatel_ciel', 'zamestnanec_ciel', 'sukromna_osoba_ciel').prefetch_related('spolujazdci').order_by('-datum_cas_start')
        tankovania_qs = Tankovanie.objects.select_related('vozidlo', 'vodič', 'osoba', 'cerpacia_stanica').order_by('-datum')

        if exact_sukromna_osoba_id is not None:
            vozidla = vozidla.filter(sukromna_osoba_id=exact_sukromna_osoba_id, priradeny_zamestnanec__isnull=True)
            jazdy_qs = jazdy_qs.filter(sukromna_osoba_id=exact_sukromna_osoba_id, zamestnanec__isnull=True)
            tankovania_qs = tankovania_qs.filter(osoba_id=exact_sukromna_osoba_id, vodič__isnull=True)
        elif exact_sukromna_osoba:
            vozidla = vozidla.filter(sukromna_osoba__meno_a_priezvisko__iexact=exact_sukromna_osoba, priradeny_zamestnanec__isnull=True)
            jazdy_qs = jazdy_qs.filter(sukromna_osoba__meno_a_priezvisko__iexact=exact_sukromna_osoba, zamestnanec__isnull=True)
            tankovania_qs = tankovania_qs.filter(osoba__meno_a_priezvisko__iexact=exact_sukromna_osoba, vodič__isnull=True)
        
        if spz_alebo_vodic and not exact_sukromna_osoba and exact_sukromna_osoba_id is None:
            vozidla = vozidla.filter(
                Q(spz__icontains=spz_alebo_vodic) | 
                Q(priradeny_zamestnanec__meno__icontains=spz_alebo_vodic) | 
                Q(priradeny_zamestnanec__priezvisko__icontains=spz_alebo_vodic) |
                Q(sukromna_osoba__meno_a_priezvisko__icontains=spz_alebo_vodic)
            )
            jazdy_qs = jazdy_qs.filter(
                Q(vozidlo__spz__icontains=spz_alebo_vodic) | 
                Q(zamestnanec__meno__icontains=spz_alebo_vodic) |
                Q(zamestnanec__priezvisko__icontains=spz_alebo_vodic) |
                Q(sukromna_osoba__meno_a_priezvisko__icontains=spz_alebo_vodic)
            )
            tankovania_qs = tankovania_qs.filter(
                Q(vozidlo__spz__icontains=spz_alebo_vodic) | 
                Q(vodič__meno__icontains=spz_alebo_vodic) |
                Q(vodič__priezvisko__icontains=spz_alebo_vodic) |
                Q(osoba__meno_a_priezvisko__icontains=spz_alebo_vodic)
            )
            
        for v in vozidla[offset:offset+limit]:
            data["vozidla_stav"].append({
                "spz": v.spz, "model": v.znacka_a_model,
                "typ_vozidla_erp": v.get_typ_vozidla_erp_display(),
                "erp_stredisko": v.erp_stredisko or "Nepriradené",
                "erp_kod_paliva": v.get_erp_kod_paliva_display(),
                "typ_paliva": v.get_typ_paliva_display(),
                "cislo_palivovej_karty": f"**** {v.cislo_palivovej_karty[-4:]}" if v.cislo_palivovej_karty and len(v.cislo_palivovej_karty) >= 4 else ("Zadané (Skryté)" if v.cislo_palivovej_karty else "Neuvedené"),
                "je_firemne": v.je_firemne,
                "vlastna_sadzba_za_km_eur": float(v.vlastna_sadzba_za_km) if v.vlastna_sadzba_za_km else 0.0,
                "aktualny_vodic": str(v.priradeny_zamestnanec) if v.priradeny_zamestnanec else (str(v.sukromna_osoba) if v.sukromna_osoba else "Voľné"),
                "aktualne_km": v.aktualny_stav_km,
                "posledny_servis_km": v.posledny_servis_km,
                "servis_interval_km": v.servis_interval_km,
                "posledny_olej_km": v.posledny_olej_km,
                "olej_interval_km": v.olej_interval_km,
                "aktualne_pneumatiky": v.get_aktualne_pneumatiky_display(),
                "priemerna_spotreba_litrov": float(v.priemerna_spotreba) if v.priemerna_spotreba is not None else 0.0,
                "celkove_naklady_palivo_eur": float(v.celkove_naklady_palivo) if v.celkove_naklady_palivo else 0.0,
                "stk_platna_do": v.stk_plati_do.strftime('%d.%m.%Y') if v.stk_plati_do else "Neznáme",
                "dialnicna_znamka_do": v.dialnicna_znamka_do.strftime('%d.%m.%Y') if v.dialnicna_znamka_do else "Neznáme",
                "lekarnicka_plati_do": v.lekarnicka_plati_do.strftime('%d.%m.%Y') if v.lekarnicka_plati_do else "Neznáme",
                "pzp_plati_do": v.pzp_plati_do.strftime('%d.%m.%Y') if v.pzp_plati_do else "Neznáme",
                "kasko_plati_do": v.kasko_plati_do.strftime('%d.%m.%Y') if v.kasko_plati_do else "Neznáme",
                "erp_status": v.get_erp_export_status_display(),
                "erp_id": v.erp_id or "Nepriradené",
                "erp_last_exported_at": v.erp_last_exported_at.strftime('%d.%m.%Y %H:%M') if v.erp_last_exported_at else "Nikdy",
                "erp_chybovy_log": v.erp_error_log or "Bez chýb"
            })
            
        for j in jazdy_qs[offset:offset+limit]:
            vodic = str(j.zamestnanec) if j.zamestnanec else (str(j.sukromna_osoba) if j.sukromna_osoba else "Neuvedený")
            data["posledne_jazdy"].append({
                "vozidlo": j.vozidlo.spz if j.vozidlo else "Neznáme",
                "vodic": vodic,
                "spolujazdci": [f"{s.meno} {s.priezvisko}" for s in j.spolujazdci.all()],
                "typ_jazdy": j.get_typ_jazdy_display(),
                "trasa_start": j.zakaznik_start.Nazov_spolocnosti if j.zakaznik_start else (j.filialka_start.nazov if j.filialka_start else (j.dodavatel_start.Nazov_spolocnosti if j.dodavatel_start else (f"{j.zamestnanec_start.meno} {j.zamestnanec_start.priezvisko} (Bydlisko)" if j.zamestnanec_start else (j.sukromna_osoba_start.meno_a_priezvisko + " (Bydlisko)" if j.sukromna_osoba_start else j.miesto_startu)))),
                "trasa_ciel": j.zakaznik.Nazov_spolocnosti if j.zakaznik else (j.filialka_ciel.nazov if j.filialka_ciel else (j.dodavatel_ciel.Nazov_spolocnosti if j.dodavatel_ciel else (f"{j.zamestnanec_ciel.meno} {j.zamestnanec_ciel.priezvisko} (Bydlisko)" if j.zamestnanec_ciel else (j.sukromna_osoba_ciel.meno_a_priezvisko + " (Bydlisko)" if j.sukromna_osoba_ciel else j.miesto_ciela)))),
                "stat_startu": j.stat_startu,
                "stat_ciela": j.stat_ciela,
                "ucel_cesty": j.ucel_cesty or "Neuvedený",
                "zakaznik": j.zakaznik.Nazov_spolocnosti if j.zakaznik else "Žiadny",
                "suvisiaca_uloha_id": j.uloha.id if j.uloha else None,
                "erp_zakazka": j.erp_zakazka or "Nepriradené",
                "prejdene_km": j.prejdene_km,
                "stav_km_start": j.stav_km_start if j.stav_km_start else "Nezadané",
                "stav_km_koniec": j.stav_km_koniec if j.stav_km_koniec else "Nezadané",
                "stav_nadrze_start_litre": float(j.stav_nadrze_start) if j.stav_nadrze_start else 0.0,
                "noclah_zabezpeceny": j.noclah_zabezpeceny,
                "bezplatne_stravovanie": j.bezplatne_stravovanie,
                "poznamka": j.poznamka or "Bez poznámky",
                "vyfakturovane": j.vyfakturovane,
                "datum_odjazdu": j.datum_cas_start.strftime('%d.%m.%Y %H:%M') if j.datum_cas_start else "Neznáme",
                "datum_prichodu": j.datum_cas_koniec.strftime('%d.%m.%Y %H:%M') if j.datum_cas_koniec else "Prebieha"
            })

        for t in tankovania_qs[offset:offset+limit]:
            vodic = str(t.vodič) if t.vodič else (str(t.osoba) if t.osoba else "Neuvedený")
            data["nedavne_tankovania"].append({
                "vozidlo": t.vozidlo.spz if t.vozidlo else "Neznáme",
                "vodic": vodic,
                "cerpacia_stanica": t.cerpacia_stanica.Nazov_spolocnosti if t.cerpacia_stanica else "Neznáma",
                "sposob_zauctovania_erp": t.get_sposob_zauctovania_erp_display(),
                "kod_clenenia_dph": t.get_kod_clenenia_dph_display(),
                "litre": float(t.litre) if t.litre is not None else 0.0,
                "cena_celkom_eur": float(t.cena_celkom) if t.cena_celkom is not None else 0.0,
                "sadzba_dph_percento": float(t.sadzba_dph) if t.sadzba_dph is not None else 0.0,
                "suma_dph_eur": float(t.suma_dph) if t.suma_dph is not None else 0.0,
                "cena_bez_dph_eur": float(t.cena_bez_dph) if t.cena_bez_dph is not None else 0.0,
                "sposob_platby": t.get_sposob_platby_display(),
                "cislo_dokladu_ekasa": t.cislo_dokladu or "Neuvedené",
                "stav_tachometra": t.stav_tachometra if t.stav_tachometra else "Nezadaný",
                "ma_foto_blocku": bool(t.foto_blocku),
                "erp_status": t.get_erp_export_status_display(),
                "erp_id": t.erp_id or "Nepriradené",
                "erp_last_exported_at": t.erp_last_exported_at.strftime('%d.%m.%Y %H:%M') if t.erp_last_exported_at else "Nikdy",
                "erp_chybovy_log": t.erp_error_log or "Bez chýb",
                "datum": t.datum.strftime('%d.%m.%Y') if t.datum else "Neznáme"
            })
            
        return json.dumps(data)
    except Exception as e:
        return json.dumps({"error": str(e)})


def tool_hr_a_mzdy(meno_osoby: Optional[str] = None, limit: int = 10, offset: int = 0) -> str:
    """
    Použi na zistenie údajov o ZAMESTNANCOCH a SÚKROMNÝCH OSOBÁCH (mzdy, dovolenky).
    """
    # 🛡️ DEFENSIVE TYPECASTING
    try:
        limit = max(1, min(int(limit), 100)) if limit is not None else 10
        offset = max(0, int(offset)) if offset is not None else 0
    except (ValueError, TypeError):
        limit, offset = 10, 0

    Zamestnanec = apps.get_model('Evidencia', 'Zamestnanec')
    SukromnaOsoba = apps.get_model('Evidencia', 'SukromnaOsoba')
    GPSStopa = apps.get_model('Evidencia', 'GPSStopa')
    PracovnaPozicia = apps.get_model('Evidencia', 'PracovnaPozicia')
    Kandidat = apps.get_model('Evidencia', 'Kandidat')
    
    data: dict[str, list[dict[str, Any]]] = {"zamestnanci": [], "sukromne_osoby": [], "pracovne_pozicie": [], "kandidati": []}
    
    try:
        Nepritomnost = apps.get_model('Evidencia', 'Nepritomnost')
        qs_zam = Zamestnanec.objects.select_related('predvolena_cerpacka', 'user', 'user__easter_egg_skore').annotate(
            vycerpane_z_absencii_db=Coalesce(
                Sum('nepritomnosti__pocet_pracovnych_dni', filter=Q(nepritomnosti__typ='DOVOLENKA', nepritomnosti__stav='SCHVALENE')),
                0.0,
                output_field=FloatField()
            )
        ).all()
        qs_suk = SukromnaOsoba.objects.select_related('predvolena_cerpacka').all()
        
        if meno_osoby:
            qs_zam = qs_zam.filter(Q(meno__icontains=meno_osoby) | Q(priezvisko__icontains=meno_osoby))
            qs_suk = qs_suk.filter(meno_a_priezvisko__icontains=meno_osoby)
            
        # 1. Vytiahneme si zamestnancov pre aktuálnu stranu do pamäte
        ziskane_osoby = list(qs_zam[offset:offset+limit])
        zam_ids = [z.id for z in ziskane_osoby]
        
        # 2. BULK FETCH: Získame všetky GPS stopy pre týchto zamestnancov naraz (1 SQL dopyt)
        vsetky_stopy = GPSStopa.objects.filter(zamestnanec_id__in=zam_ids).order_by('-timestamp')
        mapa_stop = {}
        for stopa in vsetky_stopy:
            if stopa.zamestnanec_id not in mapa_stop:
                mapa_stop[stopa.zamestnanec_id] = stopa

        # 3. BULK FETCH: Získame všetky aktívne absencie pre týchto zamestnancov naraz (1 SQL dopyt)
        dnes = timezone.now().date()
        vsetky_absencie = Nepritomnost.objects.filter(zamestnanec_id__in=zam_ids, stav='SCHVALENE', datum_do__gte=dnes)
        mapa_absencii: dict[int, list[Any]] = {}
        for a in vsetky_absencie:
            mapa_absencii.setdefault(a.zamestnanec_id, []).append(a)

        for z in ziskane_osoby:
            # Vytiahnutie Live dát z Redis Cache
            radar_data = cache.get(f'isas_radar_tech_{z.id}')
            if not radar_data:
                discord_cache = cache.get('isas_live_radar_data') or {}
                radar_data = discord_cache.get(str(z.id))
                
            live_status = radar_data.get('status') if radar_data else "OFFLINE"
            live_lat = radar_data.get('lat') if radar_data else None
            live_lon = radar_data.get('lon') if radar_data else None
            
            # Vytiahnutie poslednej známej polohy z prednačítanej mapy (ŽIADEN SQL DOPYT!)
            posledna_stopa = mapa_stop.get(z.id)
            posledna_poloha_db = f"{posledna_stopa.lat}, {posledna_stopa.lon} ({posledna_stopa.timestamp.strftime('%d.%m.%Y %H:%M')})" if posledna_stopa else "Neznáma"

            absencie = mapa_absencii.get(z.id, [])
            zoznam_absencii = [f"{a.get_typ_display()} {'(Pol dňa)' if a.pol_dna else ''} ({a.datum_od.strftime('%d.%m.%Y')} - {a.datum_do.strftime('%d.%m.%Y')}) - Schválil: {a.meno_schvalujuceho}" for a in absencie]
            user_obj = getattr(z, 'user', None)
            skore_obj = getattr(user_obj, 'easter_egg_skore', None) if user_obj is not None else None

            data["zamestnanci"].append({
                "meno_a_priezvisko": f"{z.meno} {z.priezvisko}",
                "typ_pomeru": z.get_typ_pomeru_display(),
                "erp_status": z.get_erp_export_status_display(),
                "rola": z.get_pracovna_rola_display(),
                "datum_narodenia": z.datum_narodenia.strftime('%d.%m.%Y') if z.datum_narodenia else "Nezadané",
                "datum_nastupu": z.datum_nastupu.strftime('%d.%m.%Y') if z.datum_nastupu else "Nezadané",
                "adresa": f"{z.ulica_a_cislo or ''}, {z.psc or ''} {z.mesto or ''}, {z.stat or ''}".strip(" ,"),
                "kontakt": z.telefon,
                "email": z.email or "Neuvedený",
                "cislo_op": f"****{z.cislo_op[-3:]}" if z.cislo_op and len(z.cislo_op) >= 3 else ("Zadané (Skryté)" if z.cislo_op else "Nezadané"),
                "iban": f"****{z.iban[-4:]}" if z.iban and len(z.iban) >= 4 else ("Zadané (Skryté)" if z.iban else "Neuvedený"),
                "predvolena_cerpacka": z.predvolena_cerpacka.Nazov_spolocnosti if z.predvolena_cerpacka else "Nenastavená",
                "sposob_odmenovania": z.get_sposob_odmenovania_display(),
                "mzda_brutto_hodina_eur": float(z.mzda_brutto_hodina) if z.mzda_brutto_hodina else 0.0,
                "mzda_brutto_mesiac_eur": float(z.mzda_brutto_mesiac) if z.mzda_brutto_mesiac else 0.0,
                "mzda_zivnostnik_hodina_eur": float(z.mzda_zivnostnik_hodina) if z.mzda_zivnostnik_hodina else 0.0,
                "interny_hodinovy_naklad_eur": float(z.interny_hodinovy_naklad or 0.0),
                "dlhy_a_zavazky_voci_firme_eur": float(z.zavazky) if z.zavazky is not None else 0.0,
                "poznamka_k_zavazkom": z.poznamka_zavazky or "Žiadna",
                "narok_na_dovolenku_dni": z.narok_na_dovolenku,
                "vycerpana_dovolenka_dni": z.vycerpana_dovolenka,
                "zostatok_dovolenky_dni": z.zostatok_dovolenky,
                "schvalene_buduce_absencie": zoznam_absencii,
                "bozp_platne_do": z.bozp_platne_do.strftime('%d.%m.%Y') if z.bozp_platne_do else "Nezadané/Neplatné",
                "certifikaty_a_vyhlasky": [c.nazov for c in z.certifikaty.all()],
                "ma_lekarsku_prehliadku": bool(z.lekarska_prehliadka_subor),
                "ma_sken_zmluvy": bool(z.zmluva_subor),
                "ma_sken_bozp": bool(z.bozp_subor),
                "ma_certifikaty": z.certifikaty.exists(),
                "ma_osobny_podpis": bool(z.osobny_podpis),
                "ma_priradeny_user_ucet": bool(z.user),
                "ma_prepojeny_telegram": bool(z.telegram_id),
                "ma_prepojeny_discord": bool(z.discord_id),
                "zdiela_polohu": getattr(z, 'zdiela_polohu', True),
                "live_radar_status": live_status,
                "live_gps_suradnice": f"{live_lat}, {live_lon}" if live_lat and live_lon else "Neznáme",
                "posledna_znama_poloha_db": posledna_poloha_db,
                "tg_upozornenia_ulohy": z.tg_upozornenia_ulohy,
                "mail_upozornenia_ulohy": z.mail_upozornenia_ulohy,
                "discord_upozornenia_ulohy": z.discord_upozornenia_ulohy,
                "zobrazit_v_time_web": z.zobrazit_na_webe,
                "ma_profilovu_fotku_web": bool(z.profilova_fotka),
                "verejna_rola_web": z.verejna_rola or "Nenastavená",
                "erp_id": z.erp_id or "Nepriradené",
                "erp_last_exported_at": z.erp_last_exported_at.strftime('%d.%m.%Y %H:%M') if z.erp_last_exported_at else "Nikdy",
                "erp_chybovy_log": z.erp_error_log or "Bez chýb",
                "je_anonymizovany": getattr(z, 'je_anonymizovany', False),
                "hra_2048_profil": {
                    "max_skore": skore_obj.max_skore,
                    "max_dlazdica": skore_obj.max_dlazdica,
                    "nazov_dlazdice": skore_obj.get_nazov_dlazdice(),
                    "dosiahnute_dna": skore_obj.dosiahnute_dna.strftime('%d.%m.%Y %H:%M') if skore_obj.dosiahnute_dna else "Neznáme",
                    "sezona_kod": skore_obj.sezona_kod,
                    "sezona_skore": skore_obj.sezona_skore,
                    "sezona_dlazdica": skore_obj.sezona_dlazdica,
                    "sezona_dosiahnute_dna": skore_obj.sezona_dosiahnute_dna.strftime('%d.%m.%Y %H:%M') if skore_obj.sezona_dosiahnute_dna else "Neznáme",
                    "den_kod": skore_obj.den_kod,
                    "den_skore": skore_obj.den_skore,
                    "den_dlazdica": skore_obj.den_dlazdica,
                    "den_dosiahnute_dna": skore_obj.den_dosiahnute_dna.strftime('%d.%m.%Y %H:%M') if skore_obj.den_dosiahnute_dna else "Neznáme",
                    "den_bounty_splnena": skore_obj.den_bounty_splnena
                } if skore_obj is not None else None
            })
            
        for s in qs_suk[offset:offset+limit]:
            data["sukromne_osoby"].append({
                "meno_a_priezvisko": s.meno_a_priezvisko,
                "datum_narodenia": s.datum_narodenia.strftime('%d.%m.%Y') if s.datum_narodenia else "Nezadané",
                "adresa": f"{s.ulica_a_cislo or ''}, {s.psc or ''} {s.mesto or ''}, {s.stat or ''}".strip(" ,"),
                "predvolena_cerpacka": s.predvolena_cerpacka.Nazov_spolocnosti if s.predvolena_cerpacka else "Nenastavená",
                "kontakt": s.telefon,
                "email": s.email or "Neuvedený",
                "iban": f"****{s.iban[-4:]}" if s.iban and len(s.iban) >= 4 else ("Zadané (Skryté)" if s.iban else "Neuvedený"),
                "erp_status": s.get_erp_export_status_display(),
                "erp_id": s.erp_id or "Nepriradené",
                "erp_last_exported_at": s.erp_last_exported_at.strftime('%d.%m.%Y %H:%M') if s.erp_last_exported_at else "Nikdy",
                "erp_chybovy_log": s.erp_error_log or "Bez chýb",
                "ma_prepojeny_telegram": bool(s.telegram_id),
                "ma_prepojeny_discord": bool(s.discord_id),
                "je_anonymizovany": getattr(s, 'je_anonymizovany', False)
            })

        for p in PracovnaPozicia.objects.all()[offset:offset+limit]:
            data["pracovne_pozicie"].append({
                "id": p.id, "nazov": p.nazov, "slug": p.slug, "typ_uvazku": p.get_typ_uvazku_display(),
                "cielova_rola_erp": p.get_cielova_rola_display(),
                "cielova_rola_kod": getattr(p, 'cielova_rola', 'TE'),
                "mzda": p.mzda, "lokalita": p.lokalita, "nastup": p.nastup,
                "poziadavky_skratka": p.poziadavky_skratka, "benefity_skratka": p.benefity_skratka,
                "popis_naplne": p.popis_naplne, "poziadavky_detail": p.poziadavky_detail, "ponukame_detail": p.ponukame_detail,
                "platnost_do": p.platnost_do.strftime('%d.%m.%Y') if p.platnost_do else "Neurčito",
                "vytvorene": p.vytvorene.strftime('%d.%m.%Y %H:%M') if getattr(p, 'vytvorene', None) else "Neznáme",
                "je_aktivna": p.je_aktivna, "poradie": p.poradie
            })

        for k in Kandidat.objects.select_related('pozicia', 'vytvoreny_zamestnanec').all()[offset:offset+limit]:
            data["kandidati"].append({
                "id": k.id, "meno": k.meno, "email": k.email, "telefon": k.telefon,
                "pozicia": k.pozicia.nazov if k.pozicia else "Všeobecný záujem",
                "cielova_rola_pozicie": k.pozicia.get_cielova_rola_display() if k.pozicia else "🛠️ Servisný technik",
                "stav": k.get_stav_display(), "hodnotenie": k.hodnotenie or "Nehodnotené",
                "motivacna_sprava": k.motivacna_sprava or "Žiadna",
                "datum_pohovoru": k.datum_pohovoru.strftime('%d.%m.%Y %H:%M') if k.datum_pohovoru else "Nenaplánovaný",
                "interna_poznamka": k.interna_poznamka or "Žiadna",
                "vytvoreny_zamestnanec_id": k.vytvoreny_zamestnanec_id,
                "vytvoreny_zamestnanec_meno": f"{k.vytvoreny_zamestnanec.meno} {k.vytvoreny_zamestnanec.priezvisko}" if k.vytvoreny_zamestnanec else None,
                "stav_preklopenia_erp": "✅ Preklopený do ERP" if k.vytvoreny_zamestnanec else ("⏳ Prijatý (Čaká na kartu)" if k.stav == 'PRIJATY' else "👤 V procese náboru"),
                "ma_zivotopis_subor": bool(k.zivotopis),
                "gdpr_suhlas": k.gdpr_suhlas, "suhlas_do": k.suhlas_do.strftime('%d.%m.%Y') if k.suhlas_do else "Neznáme",
                "vytvorene": k.vytvorene.strftime('%d.%m.%Y %H:%M') if k.vytvorene else "Neznáme"
            })
            
        if not data["zamestnanci"] and not data["sukromne_osoby"] and not data["pracovne_pozicie"] and not data["kandidati"]:
            return json.dumps({"informacia": "Osoba ani HR záznam nenájdený v systéme."})
            
        return json.dumps(data)
    except Exception as e:
        return json.dumps({"error": str(e)})


def tool_systemove_informacie_a_maily(limit: int = 10, offset: int = 0) -> str:
    """
    Použi na získanie FIREMNEJ IDENTITY, CENNÍKA ÚKONOV, E-MAILOV a OPT-OUT ZOZNAMU (NEZASIELAŤ).
    """
    FiremnaIdentita = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
    GlobalneNastavenia = apps.get_model('ISASaplikacia', 'GlobalneNastavenia')
    EmailovaSprava = apps.get_model('ISASaplikacia', 'EmailovaSprava')
    EmailovyUcet = apps.get_model('ISASaplikacia', 'EmailovyUcet')
    EmailOptOut = apps.get_model('ISASaplikacia', 'EmailOptOut')
    TypServisuPrace = apps.get_model('Evidencia', 'TypServisuPrace')
    MegaZaloha = apps.get_model('Evidencia', 'MegaZaloha')
    ERPExportNastavenia = apps.get_model('Evidencia', 'ERPExportNastavenia')
    ImportDatTask = apps.get_model('Evidencia', 'ImportDatTask')
    Filialka = apps.get_model('Evidencia', 'Filialka')
    GlobalneNastavenieSkladu = apps.get_model('Evidencia', 'GlobalneNastavenieSkladu')
    PravidloServisnehoIntervalu = apps.get_model('Evidencia', 'PravidloServisnehoIntervalu')
    LimitKategorieSkladu = apps.get_model('Evidencia', 'LimitKategorieSkladu')
    DatabazaVyrobcov = apps.get_model('Evidencia', 'DatabazaVyrobcov')
    StatLokalizacia = apps.get_model('ISASaplikacia', 'StatLokalizacia')
    WebNastavenia: Optional[type[Any]] = None
    WebBlok: Optional[type[Any]] = None
    WebFAQ: Optional[type[Any]] = None
    Clanok: Optional[type[Any]] = None
    WebhookEndpoint: Optional[type[Any]] = None
    WebhookLog: Optional[type[Any]] = None
    try:
        WebNastavenia = apps.get_model('WebBuilder', 'WebNastavenia')
        WebBlok = apps.get_model('WebBuilder', 'WebBlok')
        WebFAQ = apps.get_model('WebBuilder', 'WebFAQ')
        Clanok = apps.get_model('WebBuilder', 'Clanok')
        WebhookEndpoint = apps.get_model('WebBuilder', 'WebhookEndpoint')
        WebhookLog = apps.get_model('WebBuilder', 'WebhookLog')
    except LookupError:
        pass
    
    data: dict[str, Any] = {"firemne_udaje": {}, "ekonomicke_nastavenia": {}, "statne_lokalizacie_dph_a_efaktury": [], "erp_uctovne_nastavenia": {}, "cennik_pracovnych_ukonov": [], "pravidla_servisnych_intervalov": [], "pripojene_emailove_schranky": [], "neprecitane_maily": [], "opt_out_zoznam_nezasilat": [], "nedavne_exporty_a_zalohy": [], "moznosti_exportov_a_zaloh": {}, "nedavne_importy_dat": [], "struktura_excel_importov": {}, "zoznam_filialok": [], "skladove_limity": [], "globalne_nastavenie_skladu": {}, "databaza_vyrobcov_znaciek": [], "verejny_web_nastavenia": {}, "verejny_web_bloky": [], "verejny_web_faq": [], "verejny_web_clanky": [], "webhook_integracie": [], "webhook_nedavne_logy": [], "infrastruktura_a_pozadie": {}, "firemna_hra_2048": {}, "aktivne_odvetvie_a_konfiguracia": {}}
    
    try:
        identita = FiremnaIdentita.objects.select_related('zodpovedny_zamestnanec_sklad').first()
        if identita:
            data["firemne_udaje"] = {
                "nazov": identita.nazov_spolocnosti,
                "podtitul": identita.podtitul or "Nenastavený",
                "ico": identita.ico, "dic": identita.dic,
                "ic_dph": identita.ic_dph or "Nie je", "je_platca_dph": identita.je_platca_dph,
                "sidlo": f"{identita.sidlo_ulica} {identita.sidlo_cislo}, {identita.sidlo_psc} {identita.sidlo_mesto}, {identita.sidlo_stat}",
                "adresa_prevadzky": identita.adresa_prevadzky or "Zhodná so sídlom",
                "koresp_adresa": identita.koresp_adresa or "Zhodná so sídlom",
                "konatel": identita.konatel, "zapis_v_registri": identita.zapis_v_registri,
                "telefon": identita.telefon, "email": identita.email_vseobecny,
                "email_fakturacia": identita.email_fakturacia or "Nenastavený",
                "email_uctovnicky": identita.email_uctovnicky or "Nenastavený",
                "web": identita.web or "Nenastavený",
                "vystavuje_faktury": identita.vystavuje_faktury or "Nenastavené",
                "vystavuje_ponuky": identita.vystavuje_ponuky or "Nenastavené",
                "vystavuje_objednavky": identita.vystavuje_objednavky or "Nenastavené",
                "vystavuje_servis_vykazy": identita.vystavuje_servis_vykazy or "Nenastavené",
                "vystavuje_vstupky": identita.vystavuje_vstupky or "Nenastavené",
                "zodpovedny_zamestnanec_sklad": (f"{identita.zodpovedny_zamestnanec_sklad.meno} {identita.zodpovedny_zamestnanec_sklad.priezvisko}".strip() if hasattr(identita.zodpovedny_zamestnanec_sklad, 'meno') else (identita.zodpovedny_zamestnanec_sklad.get_full_name() if hasattr(identita.zodpovedny_zamestnanec_sklad, 'get_full_name') else str(identita.zodpovedny_zamestnanec_sklad))) if identita.zodpovedny_zamestnanec_sklad else "Nenastavený",
                "veduci_skladu": identita.veduci_skladu or "Nenastavený",
                "telefon_sklad": identita.telefon_sklad or "Nenastavený",
                "email_sklad": identita.email_sklad or "Nenastavený",
                "splatnost_faktur_dni": identita.splatnost_faktur,
                "predvolena_platnost_ponuk_dni": identita.predvolena_platnost_ponuk,
                "predvolena_mena": identita.predvolena_mena,
                "iban": identita.iban, "swift": identita.swift, "nazov_banky": identita.nazov_banky,
                "facebook": identita.facebook or "Nenastavený",
                "instagram": identita.instagram or "Nenastavený",
                "linkedin": identita.linkedin or "Nenastavený",
                "ma_logo": bool(identita.logo),
                "ma_peciatku": bool(identita.peciatka_podpis)
            }
            
        nastavenia = GlobalneNastavenia.objects.prefetch_related('jit_kontrolovane_filialky').first()
        if nastavenia:
            data["ekonomicke_nastavenia"] = {
                "dph_zakladna_percento": float(nastavenia.zakladna_sadzba_dph) if nastavenia.zakladna_sadzba_dph is not None else 0.0,
                "znizena_sadzba_dph_percento": float(nastavenia.znizena_sadzba_dph) if nastavenia.znizena_sadzba_dph is not None else 0.0,
                "sadzba_uroku_omeskanie_percento": float(nastavenia.sadzba_uroku_omeskanie) if nastavenia.sadzba_uroku_omeskanie is not None else 0.0,
                "nahrada_za_km_pre_zamestnanca_eur": float(nastavenia.nahrada_za_1km) if nastavenia.nahrada_za_1km is not None else 0.0,
                "stravne_5_12h_eur": float(nastavenia.stravne_5_12h) if nastavenia.stravne_5_12h is not None else 0.0,
                "stravne_12_18h_eur": float(nastavenia.stravne_12_18h) if nastavenia.stravne_12_18h is not None else 0.0,
                "stravne_nad_18h_eur": float(nastavenia.stravne_nad_18h) if nastavenia.stravne_nad_18h is not None else 0.0,
                "hodinova_sadzba_technik_eur": float(nastavenia.hodinova_sadzba_technik) if nastavenia.hodinova_sadzba_technik is not None else 0.0,
                "min_uctovacia_jednotka_minuty": nastavenia.min_uctovacia_jednotka if nastavenia.min_uctovacia_jednotka else "Presný čas",
                "pohotovostny_priplatok_percento": float(nastavenia.pohotovostny_priplatok) if nastavenia.pohotovostny_priplatok is not None else 0.0,
                "pohotovostny_priplatok_podmienky": nastavenia.pohotovostny_priplatok_popis or "Podľa pracovného času",
                "fakturacia_zakaznikovi_za_km_doprava_eur": float(nastavenia.sadzba_za_km_zakaznik) if nastavenia.sadzba_za_km_zakaznik is not None else 0.0,
                "fakturacia_zakaznikovi_za_cas_na_ceste_eur": float(nastavenia.sadzba_za_cas_na_ceste) if nastavenia.sadzba_za_cas_na_ceste is not None else 0.0,
                "email_majitel_notifikacie": nastavenia.email_majitel_notifikacie or "Nenastavený",
                "url_systemu": nastavenia.url_systemu or "Nenastavená",
                "rezim_udrzby_zapnuty": nastavenia.rezim_udrzby,
                "globalny_banner": nastavenia.globalny_banner or "Vypnutý",
                "jit_kontrolovat_vsetky_sklady": nastavenia.jit_kontrolovat_vsetky_sklady,
                "jit_kontrolovat_hlavny_sklad": nastavenia.jit_kontrolovat_hlavny_sklad,
                "jit_kontrolovane_filialky": [f.nazov for f in nastavenia.jit_kontrolovane_filialky.all()] if nastavenia.pk else [],
                "telegram_id_majitel": nastavenia.telegram_id_majitel or "Nenastavené",
                "discord_id_majitel": nastavenia.discord_id_majitel or "Nenastavené",
                "email_pre_faktury": nastavenia.email_pre_faktury or "Nenastavené",
                "email_pre_objednavky": nastavenia.email_pre_objednavky or "Nenastavené",
                "email_pre_reklamacie": nastavenia.email_pre_reklamacie or "Nenastavené",
                "email_pre_banku": nastavenia.email_pre_banku or "Nenastavené",
                "notifikacie_nastavenia": {
                    "faktury_tg": nastavenia.faktury_notif_tg, "faktury_discord": nastavenia.faktury_notif_discord, "faktury_mail": nastavenia.faktury_notif_mail,
                    "objednavky_tg": nastavenia.objednavky_notif_tg, "objednavky_discord": nastavenia.objednavky_notif_discord, "objednavky_mail": nastavenia.objednavky_notif_mail,
                    "bankar_tg": nastavenia.bankar_notif_tg, "bankar_discord": nastavenia.bankar_notif_discord, "bankar_mail": nastavenia.bankar_notif_mail,
                    "reklamacie_tg": nastavenia.reklamacie_notif_tg, "reklamacie_discord": nastavenia.reklamacie_notif_discord, "reklamacie_mail": nastavenia.reklamacie_notif_mail,
                    "presun_sklad_tg": nastavenia.presun_notif_tg, "presun_sklad_discord": nastavenia.presun_notif_discord, "presun_sklad_mail": nastavenia.presun_notif_mail,
                    "kandidati_tg": nastavenia.kandidati_notif_tg, "kandidati_discord": nastavenia.kandidati_notif_discord, "kandidati_mail": nastavenia.kandidati_notif_mail,
                    "servisy_a_revizie_tg": getattr(nastavenia, 'servisy_notif_tg', True),
                    "servisy_a_revizie_discord": getattr(nastavenia, 'servisy_notif_discord', True),
                    "servisy_a_revizie_mail": getattr(nastavenia, 'servisy_notif_mail', True),
                    "iot_telemetria_tg": getattr(nastavenia, 'iot_notif_tg', True),
                    "iot_telemetria_discord": getattr(nastavenia, 'iot_notif_discord', True),
                    "iot_telemetria_mail": getattr(nastavenia, 'iot_notif_mail', True),
                },
                "ai_denne_limity_sprav": {
                    "superadmin_majitel": nastavenia.ai_limit_superadmin,
                    "management_dispecing": nastavenia.ai_limit_management,
                    "technik_skladnik": nastavenia.ai_limit_technik,
                    "ostatni": nastavenia.ai_limit_default
                },
                "hr_kapacitne_limity_sla": {
                    "zvysena_zataz_uloh": nastavenia.limit_uloh_zvysena,
                    "kriticka_zataz_uloh_sla": nastavenia.limit_uloh_kriticka,
                    "pracovny_cas_od": nastavenia.pracovny_cas_od.strftime('%H:%M') if nastavenia.pracovny_cas_od else "Nezadané",
                    "pracovny_cas_do": nastavenia.pracovny_cas_do.strftime('%H:%M') if nastavenia.pracovny_cas_do else "Nezadané",
                    "pracovat_vikendy": nastavenia.pracovat_vikendy,
                    "pracovat_sviatky": nastavenia.pracovat_sviatky
                },
                "ai_automatizacia_a_predikcia": {
                    "zapnut_predikciu_nakupu": nastavenia.zapnut_predikciu_nakupu,
                    "dni_predikcie_nakupu": nastavenia.dni_predikcie_nakupu,
                    "zapnut_ai_upozornenia_dispecingu": nastavenia.zapnut_ai_upozornenia_dispecingu
                },
                "legislativa_a_revizie": {
                    "strazit_zakonne_revizie": getattr(nastavenia, 'servisy_sledovat_legislativu', True),
                    "predstih_upozornenia_dni": getattr(nastavenia, 'servisy_predstih_upozornenia_dni', 14),
                    "predstih_servisu_motohodiny": getattr(nastavenia, 'servisy_predstih_motohodiny', 100),
                    "standardna_lehota_reklamacie_dni": getattr(nastavenia, 'standardna_lehota_reklamacie_dni', None),
                    "odvetvova_konfiguracia_a_lehoty": get_compliance_config()
                },
                "audit_log_retencia_dni": nastavenia.audit_log_retencia_dni,
                "stav_integracii": {
                    "telegram_bot_aktivny": bool(nastavenia.telegram_bot_token),
                    "discord_bot_aktivny": bool(nastavenia.discord_bot_token),
                    "smtp_nastavene": bool(nastavenia.smtp_host and nastavenia.smtp_user),
                    "smtp_host": nastavenia.smtp_host or "Nenastavený",
                    "smtp_user": nastavenia.smtp_user or "Nenastavený",
                    "smtp_port": nastavenia.smtp_port,
                    "smtp_use_tls": nastavenia.smtp_use_tls,
                    "imap_server_faktury": nastavenia.imap_server_faktury,
                    "imap_server_objednavky": nastavenia.imap_server_objednavky,
                    "imap_server_banka": nastavenia.imap_server_banka,
                    "imap_server_reklamacie": nastavenia.imap_server_reklamacie,
                    "spotify_jukebox_aktivny": bool(nastavenia.spotify_client_id),
                    "efaktura_provider": nastavenia.get_efaktura_provider_display(),
                    "efaktura_brana_nastavena": bool(nastavenia.peppol_api_url and nastavenia.peppol_client_id),
                    "peppol_participant_id": nastavenia.peppol_participant_id or "Nenastavené",
                    "sentry_monitoring_aktivny": getattr(settings, 'SENTRY_ACTIVE', False),
                    "gemini_api_nastavene": bool(nastavenia.gemini_api_key or getattr(settings, 'GEMINI_API_KEY', None) or get_gemini_api_key()),
                    "google_maps_api_nastavene": bool(nastavenia.google_maps_api_key or getattr(settings, 'GOOGLE_MAPS_API_KEY', None))
                }
            }

        data["aktivne_odvetvie_a_konfiguracia"] = {
            "modul_odvetvia": getattr(settings, 'INDUSTRY_MODULE', None) or "Neutrálne ERP jadro",
            "podporovane_kategorie_zariadeni": list(ZARIADENIA_MAP.keys()),
            "zariadenia_volby": ZARIADENIA_CHOICES,
            "kategorie_volby": CATEGORY_CHOICES,
            "plochy_katalog_kategorii": list(FLAT_CATEGORY_MAP.keys()),
            "rodicovske_modely": list(PARENT_MODELS.keys()),
            "alarmove_prefixy": list(COMPLIANCE_ALARM_PREFIXES),
            "farby_zariadeni": ZARIADENIA_COLOR_MAP,
            "slovnik_odvetvovych_pojmov": get_all_industry_terms(),
            "predvoleny_skladovy_model": get_default_stock_model_name(),
            "odvetvove_dlazdice_hry_2048": get_easter_egg_tiles(),
            "technologicke_nastroje_a_audity": {
                "energeticky_esg_audit": HAS_ENERGY_ESG_AUDIT,
                "fplyny_audit": HAS_FPLYNY_AUDIT,
                "rekuperacia_odpadoveho_tepla": HAS_HEAT_RECOVERY,
                "vtz_tlakove_inspekcie": HAS_VTZ_INSPECTION,
                "vibrodiagnostika": HAS_VIBRO_DIAGNOSTICS,
                "capex_opex_kalkulacka": HAS_CAPEX_CALCULATOR,
            },
            "chladiva_fplyny": FPLYNY_CHLADIVA,
            "tco_profily": TCO_PROFILES,
            "normy_cistoty_medii": MEDIA_QUALITY_APPS,
            "hub_config": HUB_CONFIG,
            "ukazkove_importne_data": SAMPLE_IMPORT_DATA,
            "aktivne_reverzne_relacie_parametrov": get_active_parameter_reverse_names()
        }

        staty_qs = StatLokalizacia.objects.all()
        for st in staty_qs:
            data["statne_lokalizacie_dph_a_efaktury"].append({
                "iso_kod": st.iso_kod,
                "nazov": st.nazov,
                "mena": st.mena,
                "dph_zakladna": float(st.dph_zakladna),
                "dph_znizena": float(st.dph_znizena),
                "je_clen_eu": st.je_clen_eu,
                "peppol_eas_kod": st.peppol_eas_kod or "Neuvedený",
                "efaktura_rezim": st.get_efaktura_rezim_display(),
                "lehota_faktury_dni": st.lehota_faktury_dni,
                "lehota_faktury_typ": st.get_lehota_faktury_typ_display(),
                "lehota_reklamacie_dni": st.lehota_reklamacie_dni,
                "narodne_odvetvove_lehoty": get_compliance_config(iso=st.iso_kod),
                "aliasy": st.aliasy or "Žiadne",
                "konfiguracia_regionov": st.subdiv_konfiguracia or {}
            })
            
        erp = ERPExportNastavenia.get_settings() if hasattr(ERPExportNastavenia, 'get_settings') else ERPExportNastavenia.objects.first()
        if erp:
            raw_krajina = FiremnaIdentita.get_aktivna_krajina_iso() if hasattr(FiremnaIdentita, 'get_aktivna_krajina_iso') else (identita.get_aktivna_krajina_iso() if identita and hasattr(identita, 'get_aktivna_krajina_iso') else 'NEZADANE')
            krajina_firmy = str(raw_krajina).strip().upper() if raw_krajina else 'NEZADANE'
            raw_system = getattr(erp, 'erp_system', None)
            kod_erp_systemu = str(raw_system).strip().upper() if raw_system else 'POHODA'

            if not krajina_firmy or krajina_firmy == 'NEZADANE':
                podporovane = False
                status_podpory = "Nevyplnená firemná identita (V systéme chýba IČO a štát sídla firmy pre ERP export)"
            elif kod_erp_systemu == 'OMEGA':
                podporovane = (krajina_firmy == 'SK')
                status_podpory = "Plne podporované (KROS Omega pre SK legislatívu)" if podporovane else f"Nepodporované pre sídlo v '{krajina_firmy}' (KROS Omega je legislatívne určená výhradne pre Slovenskú republiku)"
            elif kod_erp_systemu == 'POHODA':
                podporovane = (krajina_firmy in ['SK', 'CZ'])
                status_podpory = "Plne podporované (Stormware Pohoda pre SK/CZ legislatívu)" if podporovane else f"Nepodporované pre sídlo v '{krajina_firmy}' (Stormware Pohoda XML podporuje iba SK a CZ)"
            else:
                podporovane = False
                status_podpory = f"Systém '{kod_erp_systemu}' nie je podporovaný ISAS CORE"

            data["erp_uctovne_nastavenia"] = {
                "krajina_firmy_iso": krajina_firmy,
                "export_aktivny_pre_krajinu": podporovane,
                "status_podpory": status_podpory,
                "cielovy_erp_system": erp.get_erp_system_display(),
                "kod_erp_systemu": kod_erp_systemu,
                "ucet_pohladavky": erp.ucet_pohladavky,
                "ucet_zavazky": erp.ucet_zavazky,
                "ucet_banka": erp.ucet_banka,
                "ucet_pokladna": erp.ucet_pokladna,
                "ucet_dph_20": erp.ucet_dph_20,
                "ucet_dph_prenos": erp.ucet_dph_prenos,
                "pohoda_skratka_banky": erp.pohoda_skratka_banky or "Nenastavená",
                "pohoda_predkontacia_fa": erp.pohoda_predkontacia_fa or "Nenastavená",
                "pohoda_predkontacia_prijata_fa": erp.pohoda_predkontacia_prijata_fa or "Nenastavená",
                "pohoda_predkontacia_banka_prijem": erp.pohoda_predkontacia_banka_prijem or "Nenastavená",
                "pohoda_predkontacia_banka_vydaj": erp.pohoda_predkontacia_banka_vydaj or "Nenastavená",
                "pohoda_predkontacia_sluzby": erp.pohoda_predkontacia_sluzby or "Nenastavená",
                "pohoda_predkontacia_tovar": erp.pohoda_predkontacia_tovar or "Nenastavená",
                "omega_evidencia_banka": getattr(erp, 'omega_evidencia_banka', 'BV'),
                "omega_ciselny_rad_banka": getattr(erp, 'omega_ciselny_rad_banka', 'B'),
                "omega_evidencia_pokladna": getattr(erp, 'omega_evidencia_pokladna', 'PD'),
                "omega_ciselny_rad_pokladna_prijem": getattr(erp, 'omega_ciselny_rad_pokladna_prijem', 'P'),
                "omega_ciselny_rad_pokladna_vydaj": getattr(erp, 'omega_ciselny_rad_pokladna_vydaj', 'V'),
                "omega_evidencia_id": getattr(erp, 'omega_evidencia_id', 'ID'),
                "omega_ciselny_rad_id": getattr(erp, 'omega_ciselny_rad_id', 'ID'),
                "ucet_naklady_palivo": erp.ucet_naklady_palivo,
                "ucet_zavazky_zamestnanci": erp.ucet_zavazky_zamestnanci,
                "auto_odosielat_mailom": erp.auto_odosielat_mailom
            }
            
        ukony = TypServisuPrace.objects.all().order_by('nazov', 'id')[offset:offset+limit]
        for u in ukony:
            data["cennik_pracovnych_ukonov"].append({
                "id": u.id,
                "nazov_prace": u.nazov,
                "je_fakturovatelna": u.je_fakturovatelna,
                "priorita": u.get_priorita_display(),
                "priorita_kod": u.priorita,
                "sposob_uctovania": u.get_sposob_uctovania_display(),
                "sposob_uctovania_kod": u.sposob_uctovania,
                "hodinova_sadzba_predaj_eur": float(u.hodinova_sadzba_predaj or 0.0),
                "fixna_cena_predaj_eur": float(u.fixna_cena_predaj or 0.0),
                "hodinova_sadzba_naklad_eur": float(u.hodinova_sadzba_naklad or 0.0),
                "fixny_naklad_eur": float(u.fixny_naklad or 0.0),
                "normohodiny": float(u.normohodiny) if u.normohodiny is not None else 1.0,
                "zaruka_mesiace": u.zaruka_mesiace,
                "pracovny_postup_sop": u.pracovny_postup or "Žiadny",
                "zverejnit_postup_na_webe": getattr(u, 'zverejnit_postup_na_webe', False),
                "zobrazit_na_webe": u.zobrazit_na_webe,
                "povolit_online_booking": getattr(u, 'povolit_online_booking', False),
                "booking_trvanie_min": getattr(u, 'booking_trvanie_min', None),
                "marketingovy_popis_web": u.popis_na_webe or "Žiadny",
                "slug": u.slug
            })

        pravidla = PravidloServisnehoIntervalu.objects.select_related('typ_servisu').all()[offset:offset+limit]
        for pr in pravidla:
            data["pravidla_servisnych_intervalov"].append({
                "typ_servisu": pr.typ_servisu.nazov,
                "kategoria_zariadenia": pr.get_kategoria_zariadenia_display(),
                "interval_motohodin": pr.interval_motohodin or "Nenastavené",
                "interval_motohodin_zataz": pr.interval_motohodin_zataz or "Nenastavené",
                "interval_dni": pr.interval_dni or "Nenastavené",
                "vaha_hierarchie": pr.vaha_hierarchie
            })
            
        neprecitane = EmailovaSprava.objects.filter(precitane=False, zlozka='INBOX').prefetch_related('prilohy').order_by('-datum_prijatia')[offset:offset+limit]
        for m in neprecitane:
            data["neprecitane_maily"].append({
                "email_id": m.id,
                "od": m.odosielatel, "predmet": m.predmet,
                "datum_prijatia": m.datum_prijatia.strftime('%d.%m.%Y %H:%M') if m.datum_prijatia else "Neznáme",
                "ai_kategoria": m.ai_kategoria or "Nezaradené",
                "ai_zhrnutie": m.ai_zhrnutie,
                "obsahuje_prilohy": bool(m.prilohy.all())
            })

        try:
            safe_lim = max(1, min(int(limit), 100)) if limit is not None else 10
            safe_off = max(0, int(offset)) if offset is not None else 0
        except (ValueError, TypeError):
            safe_lim, safe_off = 10, 0

        opt_out_zaznamy = EmailOptOut.objects.select_related('zakaznik', 'dodavatel', 'sukromna_osoba', 'zdrojova_sprava').order_by('-vytvorene')[safe_off:safe_off+safe_lim]
        for o in opt_out_zaznamy:
            data["opt_out_zoznam_nezasilat"].append({
                "id": o.id,
                "email": o.email,
                "aktivny": o.aktivny,
                "zdroj_kod": o.zdroj,
                "zdroj": o.get_zdroj_display(),
                "rozsah_kod": o.rozsah,
                "rozsah": o.get_rozsah_display(),
                "dovod": o.dovod or "Bez dôvodu",
                "poznamka": o.poznamka or "Žiadna",
                "zdrojova_sprava_id": o.zdrojova_sprava_id,
                "zdrojova_sprava_predmet": o.zdrojova_sprava.predmet if o.zdrojova_sprava else None,
                "zakaznik_id": o.zakaznik_id,
                "zakaznik": o.zakaznik.Nazov_spolocnosti if o.zakaznik else None,
                "dodavatel_id": o.dodavatel_id,
                "dodavatel": o.dodavatel.Nazov_spolocnosti if o.dodavatel else None,
                "sukromna_osoba_id": o.sukromna_osoba_id,
                "sukromna_osoba": o.sukromna_osoba.meno_a_priezvisko if o.sukromna_osoba else None,
                "vytvorene": o.vytvorene.strftime('%d.%m.%Y %H:%M') if o.vytvorene else "Neznáme",
                "aktualizovane": o.aktualizovane.strftime('%d.%m.%Y %H:%M') if o.aktualizovane else "Neznáme",
            })
            
        # 🟢 Pridaný loop pre Exporty a Zálohy
        exporty = MegaZaloha.objects.select_related('vyziadal_uzivatel').order_by('-datum_vytvorenia')[offset:offset+limit]
        for e in exporty:
            data["nedavne_exporty_a_zalohy"].append({
                "nazov": e.nazov_zalohy,
                "typ_dat": e.get_typ_zalohy_display(),
                "stav": e.get_stav_spracovania_display(),
                "velkost_mb": float(e.velkost_v_mb) if e.velkost_v_mb else 0.0,
                "politika_uchovania": e.get_politika_uchovania_display(),
                "vytvorene": e.datum_vytvorenia.strftime('%d.%m.%Y %H:%M') if e.datum_vytvorenia else "Neznáme",
                "datum_exspiracie": e.datum_exspiracie.strftime('%d.%m.%Y %H:%M') if e.datum_exspiracie else "Nikdy",
                "obsahuje_data_od": e.obsahuje_data_od.strftime('%d.%m.%Y') if e.obsahuje_data_od else "Bez limitu",
                "obsahuje_data_do": e.obsahuje_data_do.strftime('%d.%m.%Y') if e.obsahuje_data_do else "Bez limitu",
                "poznamka": e.poznamka or "Bez poznámky",
                "vyziadal": e.vyziadal_uzivatel.get_full_name() if e.vyziadal_uzivatel else "Systém",
                "ma_fyzicky_subor": bool(e.subor_zalohy)
            })

        # 🟢 ISAS EXPORT ENGINE SPECIFIKÁCIA: AI musí poznať podporované typy exportov, formáty a štruktúru stĺpcov z ISASExportEngine
        data["moznosti_exportov_a_zaloh"] = {
            "formaty_a_routing": {
                "databazovy_dump": {"kod": "kompletny_dump", "vystup": "ZIP archív obsahujúci kompletna_databaza_dump.json"},
                "asynchronne_ulohy": {
                    "audit_logy": "Celery task_archivovat_a_vycistit_audit_logy",
                    "podklady_mzdy": "Celery task_vygeneruj_mzdy_excel (Excel súbor)"
                },
                "csv_reporty_windows_utf8": {
                    "format": "Windows UTF-8-SIG, oddeľovač ';'",
                    "typy": ["kniha_jazd", "evidencia_uhrad", "skladove_pohyby", "cenovy_katalog", "ulohy_a_vykazy"]
                },
                "zip_fyzickych_suborov": [
                    "vystavene_faktury", "prichodzie_faktury", "cenove_ponuky", "servisne_protokoly",
                    "odoslane_objednavky", "prijate_objednavky", "tankovania", "hr_dokumenty",
                    "technicke_specifikacie", "povolenia_vstupov", "qr_kody_zariadeni"
                ]
            }
        }

        # 🟢 Pridaný loop pre Importy dát
        importy = ImportDatTask.objects.select_related('vytvoril').order_by('-vytvorene')[offset:offset+limit]
        
        # 🟢 ISAS CORE: AI musí vedieť, aké stĺpce systém vyžaduje pre Excel importy
        if hasattr(ImportDatTask, 'STLPCE_MAPA'):
            data["struktura_excel_importov"] = ImportDatTask.STLPCE_MAPA

        for i in importy:
            data["nedavne_importy_dat"].append({
                "typ_importu": i.get_typ_importu_display(),
                "stav": i.get_stav_display(),
                "vytvorene": i.vytvorene.strftime('%d.%m.%Y %H:%M') if i.vytvorene else "Neznáme",
                "dokoncene": i.dokoncene.strftime('%d.%m.%Y %H:%M') if i.dokoncene else "Zatiaľ nedokončené",
                "celkovy_pocet_riadkov": i.celkovy_pocet_riadkov,
                "uspesne_importovane": i.uspesne_importovane,
                "preskocene_duplicity": i.preskocene_duplicity,
                "chybne_riadky": i.chybne_riadky,
                "rezim_importu": i.get_rezim_importu_display(),
                "chybovy_log": i.chybovy_log or "Bez chýb",
                "vytvoril": i.vytvoril.get_full_name() if i.vytvoril else "Systém",
                "ma_fyzicky_subor": bool(i.subor),
                "velkost_suboru_kb": i.velkost_suboru_kb
            })

        ucty = EmailovyUcet.objects.all()[offset:offset+limit]
        for ucet in ucty:
            data["pripojene_emailove_schranky"].append({
                "nazov": ucet.nazov,
                "email": ucet.email_adresa,
                "prihlasovacie_meno": ucet.prihlasovacie_meno or "Zhodné s e-mailom",
                "je_aktivny": ucet.je_aktivny,
                "imap_server": ucet.imap_server,
                "imap_port": ucet.imap_port,
                "smtp_server": ucet.smtp_server,
                "smtp_port": ucet.smtp_port,
                "ma_nastaveny_html_podpis": bool(ucet.podpis_html)
            })

        filialky = Filialka.objects.select_related('zodpovedny_zamestnanec').prefetch_related('priradeni_zamestnanci').all()[offset:offset+limit]
        for f in filialky:
            data["zoznam_filialok"].append({
                "nazov": f.nazov,
                "kod_pobocky": f.kod_pobocky or "Nemá",
                "typ_skladu": f.get_typ_skladu_display(),
                "adresa": f.adresa or "Neuvedená",
                "veduci_skladu": f"{f.zodpovedny_zamestnanec.meno} {f.zodpovedny_zamestnanec.priezvisko}" if f.zodpovedny_zamestnanec else (f.veduci_skladu or "Neuvedený"),
                "telefon": f.telefon or "Neuvedený",
                "email": f.email or "Neuvedený",
                "priradeni_zamestnanci_pristup": [f"{z.meno} {z.priezvisko}" for z in f.priradeni_zamestnanci.all()],
                "interna_poznamka": f.poznamka or "Žiadna",
                "je_aktivna": f.je_aktivna
            })

        limity = LimitKategorieSkladu.objects.all()[offset:offset+limit]
        for l in limity:
            data["skladove_limity"].append({
                "kategoria": l.kategoria_nazov,
                "kriticky_limit_ks": l.kriticky_limit
            })

        nastavenie_skladu = GlobalneNastavenieSkladu.objects.first()
        if nastavenie_skladu:
            data["globalne_nastavenie_skladu"] = {
                "zapnute_notifikacie": nastavenie_skladu.zapnute_notifikacie
            }

        vyrobcovia = DatabazaVyrobcov.objects.all()[offset:offset+limit]
        for v in vyrobcovia:
            data["databaza_vyrobcov_znaciek"].append({
                "nazov": v.nazov_vyrobcu,
                "krajina_povodu": v.krajina_povodu or "Neznáma",
                "web": v.domena or "Neuvedený",
                "katalog_dielov": v.katalog_dielov_url or "Neuvedený",
                "typ_vyroby": v.get_typ_vyroby_display(),
                "technicka_poznamka": v.technicka_poznamka or "Žiadna",
                "zobrazit_znacku_na_webe": v.zobrazit_na_webe,
                "ma_logo_na_webe": bool(v.logo_na_webe)
            })
            
        if WebNastavenia:
            live_nast = WebNastavenia.objects.filter(pk=1).first()
            draft_nast = WebNastavenia.objects.filter(pk=2).first()
            data["verejny_web_nastavenia"] = {
                "live": {
                    "domena": live_nast.domena if live_nast and live_nast.domena else "Nenastavená",
                    "titulok_webu": live_nast.titulok_webu if live_nast else "ISAS Web",
                    "hlavna_farba": live_nast.hlavna_farba if live_nast else "#2563eb",
                    "meta_popis": live_nast.meta_popis if live_nast else "Žiadny",
                    "zobrazit_live_sla": live_nast.zobrazit_live_sla if live_nast else False,
                    "sla_text_optimalna": getattr(live_nast, 'sla_text_optimalna', None) or "Systémový fallback",
                    "sla_text_zvysena": getattr(live_nast, 'sla_text_zvysena', None) or "Systémový fallback",
                    "sla_text_kriticka": getattr(live_nast, 'sla_text_kriticka', None) or "Systémový fallback",
                    "ma_logo": bool(live_nast.logo) if live_nast else False
                } if live_nast else "Nenakonfigurované",
                "draft": {
                    "domena": draft_nast.domena or "Zhodná s LIVE",
                    "titulok_webu": draft_nast.titulok_webu,
                    "hlavna_farba": draft_nast.hlavna_farba,
                    "meta_popis": draft_nast.meta_popis or "Žiadny",
                    "zobrazit_live_sla": draft_nast.zobrazit_live_sla,
                    "sla_text_optimalna": getattr(draft_nast, 'sla_text_optimalna', None) or "Zhodné s LIVE",
                    "sla_text_zvysena": getattr(draft_nast, 'sla_text_zvysena', None) or "Zhodné s LIVE",
                    "sla_text_kriticka": getattr(draft_nast, 'sla_text_kriticka', None) or "Zhodné s LIVE",
                    "ma_logo": bool(draft_nast.logo)
                } if draft_nast else "Žiadny rozpracovaný draft (Nastavenia zhodné s LIVE)"
            }
                
        if WebBlok:
            bloky = WebBlok.objects.all().order_by('verzia', 'poradie')[offset:offset+limit]
            data["verejny_web_bloky_info"] = {
                "pocet_live_blokov": WebBlok.objects.filter(verzia='LIVE', je_aktivny=True).count(),
                "pocet_draft_blokov": WebBlok.objects.filter(verzia='DRAFT').count(),
                "existuje_rozpracovany_draft": WebBlok.objects.filter(verzia='DRAFT').exists(),
                "preview_odkaz": "/?preview=ai_draft"
            }
            for b in bloky:
                data["verejny_web_bloky"].append({
                    "id": b.id,
                    "typ_bloku": b.get_typ_display(),
                    "typ_kod": b.typ,
                    "verzia": b.verzia,
                    "poradie": b.poradie,
                    "je_aktivny": b.je_aktivny,
                    "nadpis": b.nadpis or "Bez nadpisu",
                    "podnadpis": b.podnadpis or "Bez textu",
                    "tlacidlo_text": b.tlacidlo_text or "Žiadne",
                    "ma_obrazok": bool(b.obrazok)
                })

        if WebFAQ:
            faqs = WebFAQ.objects.all().order_by('kategoria', 'poradie')[offset:offset+limit]
            for faq in faqs:
                data["verejny_web_faq"].append({
                    "kategoria": faq.get_kategoria_display(),
                    "otazka": faq.otazka,
                    "odpoved": faq.odpoved,
                    "slug": faq.slug,
                    "poradie": faq.poradie,
                    "je_aktivna": faq.je_aktivna
                })

        if Clanok:
            clanky = Clanok.objects.select_related('autor').all().order_by('-datum_vydania')[offset:offset+limit]
            for c in clanky:
                data["verejny_web_clanky"].append({
                    "nadpis": c.nadpis,
                    "slug": c.slug,
                    "kategoria": c.get_kategoria_display(),
                    "autor": (f"{c.autor.meno} {c.autor.priezvisko}".strip() if hasattr(c.autor, 'meno') else (c.autor.get_full_name() if hasattr(c.autor, 'get_full_name') else str(c.autor))) if c.autor else "Neznámy",
                    "perex": c.perex,
                    "obsah": c.obsah,
                    "ma_obrazok": bool(c.obrazok),
                    "obrazok_alt": c.obrazok_alt or "Žiadny",
                    "seo_titulok": c.seo_titulok or "Žiadny",
                    "meta_popis": c.meta_popis or "Žiadny",
                    "datum_vydania": c.datum_vydania.strftime('%d.%m.%Y') if c.datum_vydania else "Neznáme",
                    "je_aktivny": c.je_aktivny,
                    "cas_citania_minuty": c.cas_citania
                })

        if WebhookEndpoint:
            webhooky = WebhookEndpoint.objects.select_related('zakaznik').all()[offset:offset+limit]
            for w in webhooky:
                data["webhook_integracie"].append({
                    "zakaznik": w.zakaznik.Nazov_spolocnosti if w.zakaznik else "Neznámy",
                    "nazov": w.nazov,
                    "url": w.url,
                    "tajny_kluc": "Nastavený (Skrytý)" if w.tajny_kluc else "Nenastavený",
                    "vlastne_hlavicky": w.vlastne_hlavicky,
                    "udalost": w.get_udalost_display(),
                    "je_aktivny": w.je_aktivny,
                    "max_zlyhani": w.max_zlyhani,
                    "zlyhania_po_sebe": w.zlyhania_po_sebe,
                    "dovod_deaktivacie": w.dovod_deaktivacie or "Žiadny"
                })

        if WebhookLog:
            logy = WebhookLog.objects.select_related('endpoint__zakaznik').order_by('-cas_odoslania')[offset:offset+limit]
            for wh_log in logy:
                data["webhook_nedavne_logy"].append({
                    "endpoint_nazov": wh_log.endpoint.nazov if wh_log.endpoint else "Neznámy",
                    "zakaznik": wh_log.endpoint.zakaznik.Nazov_spolocnosti if wh_log.endpoint and wh_log.endpoint.zakaznik else "Neznámy",
                    "event_id": str(wh_log.event_id),
                    "pokus": getattr(wh_log, 'pokus', 1),
                    "cas_odoslania": wh_log.cas_odoslania.strftime('%d.%m.%Y %H:%M:%S') if wh_log.cas_odoslania else "Neznáme",
                    "payload": wh_log.payload,
                    "request_headers": wh_log.request_headers or "Žiadne",
                    "http_status": wh_log.http_status,
                    "odpoved_servera": wh_log.odpoved_servera or "Žiadna",
                    "response_headers": wh_log.response_headers or "Žiadne",
                    "trvanie_ms": wh_log.trvanie_ms,
                    "uspesne": wh_log.uspesne
                })

        beat_schedule = getattr(settings, 'CELERY_BEAT_SCHEDULE', {})
        celery_tasks_info = [
            f"{k}: {v.get('task')} ({v.get('schedule')})" if isinstance(v, dict) else str(k)
            for k, v in beat_schedule.items()
        ] if isinstance(beat_schedule, dict) and beat_schedule else []

        data["infrastruktura_a_pozadie"] = {
            "bezi_v_docker_kontajneri": getattr(settings, 'IS_DOCKER', False),
            "debug_rezim_zapnuty": getattr(settings, 'DEBUG', False),
            "casova_zona": getattr(settings, 'TIME_ZONE', 'UTC'),
            "use_tz_nastavenie": getattr(settings, 'USE_TZ', False),
            "use_tz_naive_rezim": not getattr(settings, 'USE_TZ', False),
            "celery_enable_utc": getattr(settings, 'CELERY_ENABLE_UTC', False),
            "celery_broker_url": re.sub(r'://[^/@]+@', '://***@', str(getattr(settings, 'CELERY_BROKER_URL', '') or '')),
            "celery_result_backend": re.sub(r'://[^/@]+@', '://***@', str(getattr(settings, 'CELERY_RESULT_BACKEND', '') or '')),
            "celery_timezone": getattr(settings, 'CELERY_TIMEZONE', ''),
            "redis_base_url": re.sub(r'://[^/@]+@', '://***@', str(getattr(settings, 'REDIS_BASE_URL', '') or '')),
            "secure_proxy_ssl_header": getattr(settings, 'SECURE_PROXY_SSL_HEADER', None),
            "use_x_forwarded_host": getattr(settings, 'USE_X_FORWARDED_HOST', False),
            "session_cookie_secure": getattr(settings, 'SESSION_COOKIE_SECURE', False),
            "csrf_cookie_secure": getattr(settings, 'CSRF_COOKIE_SECURE', False),
            "databaza_engine": getattr(settings, 'DATABASES', {}).get('default', {}).get('ENGINE', ''),
            "sentry_monitoring_aktivny": getattr(settings, 'SENTRY_ACTIVE', False),
            "sentry_prostredie": getattr(settings, 'SENTRY_ENVIRONMENT', 'development' if getattr(settings, 'DEBUG', False) else 'production'),
            "sifrovanie_databazy_aktivne": bool(getattr(settings, 'CRYPTOGRAPHY_KEY', False)),
            "site_url": getattr(settings, 'SITE_URL', 'http://127.0.0.1:8000'),
            "public_web_domain": getattr(settings, 'PUBLIC_WEB_DOMAIN', ''),
            "main_telemetry_domain": getattr(settings, 'MAIN_TELEMETRY_DOMAIN', 'localhost'),
            "allowed_hosts": list(getattr(settings, 'ALLOWED_HOSTS', [])),
            "csrf_trusted_origins": list(getattr(settings, 'CSRF_TRUSTED_ORIGINS', [])),
            "django_email_backend": getattr(settings, 'EMAIL_BACKEND', ''),
            "django_email_host": getattr(settings, 'EMAIL_HOST', ''),
            "django_email_port": getattr(settings, 'EMAIL_PORT', 587),
            "django_email_use_tls": getattr(settings, 'EMAIL_USE_TLS', True),
            "default_from_email": getattr(settings, 'DEFAULT_FROM_EMAIL', ''),
            "data_upload_max_number_fields": getattr(settings, 'DATA_UPLOAD_MAX_NUMBER_FIELDS', 20000),
            "cache_backend": getattr(settings, 'CACHES', {}).get('default', {}).get('BACKEND', ''),
            "celery_predvolena_fronta": getattr(settings, 'CELERY_TASK_DEFAULT_QUEUE', 'default'),
            "automatizovane_ulohy_celery_beat": celery_tasks_info
        }
        
        # Pridanie stavu Spotify Tokenov
        try:
            SpotifyToken = apps.get_model('ISASaplikacia', 'SpotifyToken')
            aktivna_hranica = timezone.now() + timedelta(minutes=5)
            aktivne_qs = SpotifyToken.objects.filter(expires_at__gt=aktivna_hranica).select_related('user')
            expirovane_qs = SpotifyToken.objects.filter(expires_at__lte=aktivna_hranica)
            
            aktivne_ucty = [t.spotify_account_name or (t.user.get_username() if t.user else "Neznámy") for t in aktivne_qs]
            
            data["infrastruktura_a_pozadie"]["spotify_jukebox_status"] = {
                "aktivne_tokeny_pocet": aktivne_qs.count(),
                "expirovane_tokeny_pocet": expirovane_qs.count(),
                "pripojene_ucty": aktivne_ucty
            }
        except LookupError:
            pass

        ISASHraLeaderboard: Optional[type[Any]] = None
        try:
            ISASHraLeaderboard = apps.get_model('ISASaplikacia', 'ISASHraLeaderboard')
        except LookupError:
            ISASHraLeaderboard = None

        if ISASHraLeaderboard:
            dnesny_den_kod = timezone.now().strftime("%Y-%m-%d")
            aktualny_kvartal_kod, kvartal_nazov = ISASHraLeaderboard.get_aktualny_kvartal_info()
            denna_bounty = ISASHraLeaderboard.get_denna_bounty(dnesny_den_kod)

            top_all_time = ISASHraLeaderboard.objects.select_related('user').order_by('-max_skore', '-dosiahnute_dna')[:3]
            top_sezona = ISASHraLeaderboard.objects.filter(sezona_kod=aktualny_kvartal_kod).select_related('user').order_by('-sezona_skore', '-sezona_dosiahnute_dna')[:3]
            top_den = ISASHraLeaderboard.objects.filter(den_kod=dnesny_den_kod).select_related('user').order_by('-den_skore', '-den_dosiahnute_dna')[:3]

            data["firemna_hra_2048"] = {
                "aktualna_sezona": {
                    "kod": aktualny_kvartal_kod,
                    "nazov": kvartal_nazov,
                    "top_hraci": [
                        {
                            "hrac": r.user.get_full_name() or r.user.username,
                            "skore": r.sezona_skore,
                            "dlazdica": r.sezona_dlazdica,
                            "nazov_dlazdice": r.get_nazov_dlazdice(),
                            "dosiahnute_dna": r.sezona_dosiahnute_dna.strftime('%d.%m.%Y %H:%M') if r.sezona_dosiahnute_dna else "Neznáme"
                        } for r in top_sezona
                    ]
                },
                "denna_vyzva": {
                    "den_kod": dnesny_den_kod,
                    "bounty_uloha": denna_bounty,
                    "top_hraci_dnes": [
                        {
                            "hrac": r.user.get_full_name() or r.user.username,
                            "skore": r.den_skore,
                            "dlazdica": r.den_dlazdica,
                            "nazov_dlazdice": r.get_nazov_dlazdice(),
                            "bounty_splnena": r.den_bounty_splnena,
                            "dosiahnute_dna": r.den_dosiahnute_dna.strftime('%d.%m.%Y %H:%M') if r.den_dosiahnute_dna else "Neznáme"
                        } for r in top_den
                    ]
                },
                "sien_slavy_all_time": [
                    {
                        "hrac": r.user.get_full_name() or r.user.username,
                        "max_skore": r.max_skore,
                        "max_dlazdica": r.max_dlazdica,
                        "nazov_dlazdice": r.get_nazov_dlazdice(),
                        "dosiahnute_dna": r.dosiahnute_dna.strftime('%d.%m.%Y %H:%M') if r.dosiahnute_dna else "Neznáme"
                    } for r in top_all_time
                ]
            }
            
        return json.dumps(data)

    except Exception as e:
        return json.dumps({"error": str(e)})

def tool_manazerske_statistiky(typ_reportu: Optional[str] = None, datum_od: Optional[str] = None, datum_do: Optional[str] = None) -> str:
    """
    Použi na získanie KOMPLEXNÝCH MANAŽÉRSKYCH ŠTATISTÍK (P&L, Cash-flow, Pohľadávky, Záväzky, Pipeline, Fleet, Operatíva, Sklad, HR, CRM, Kvalita, IT).
    Dátumy musia byť vo formáte YYYY-MM-DD. Nástroj vracia agregované dáta priamo z databázy v O(1) dopytoch.
    """

    VystavenaFaktura = apps.get_model('Evidencia', 'VystavenaFaktura')
    PrichodziaFaktura = apps.get_model('Evidencia', 'PrichodziaFaktura')
    EvidenciaUhrad = apps.get_model('Evidencia', 'EvidenciaUhrad')
    CenovaPonuka = apps.get_model('Evidencia', 'CenovaPonuka')
    PrijataObjednavka = apps.get_model('Evidencia', 'PrijataObjednavka')
    OdoslanaObjednavka = apps.get_model('Evidencia', 'OdoslanaObjednavka')
    Tankovanie = apps.get_model('Evidencia', 'Tankovanie')
    EvidenciaJazd = apps.get_model('Evidencia', 'EvidenciaJazd')
    ServisnyZaznam = apps.get_model('Evidencia', 'ServisnyZaznam')
    Uloha = apps.get_model('Evidencia', 'Uloha')
    PouzityMaterialVZaznamoch = apps.get_model('Evidencia', 'PouzityMaterialVZaznamoch')
    KosikSPolozkamiNaDoobjednanie = apps.get_model('Evidencia', 'KosikSPolozkamiNaDoobjednanie')
    PlanovanyServisZariadenia = apps.get_model('Evidencia', 'PlanovanyServisZariadenia')
    Zamestnanec = apps.get_model('Evidencia', 'Zamestnanec')
    Zakaznik = apps.get_model('Evidencia', 'Zakaznik')
    Dodavatel = apps.get_model('Evidencia', 'Dodavatel')
    Vozidlo = apps.get_model('Evidencia', 'Vozidlo')
    ServisovaneZariadenie = apps.get_model('Evidencia', 'ServisovaneZariadenie')
    StockMovement = apps.get_model('Evidencia', 'StockMovement')
    PovolenieNaVstup = apps.get_model('Evidencia', 'PovolenieNaVstup')
    MegaZaloha = apps.get_model('Evidencia', 'MegaZaloha')
    EmailovaSprava = apps.get_model('ISASaplikacia', 'EmailovaSprava')
    EmailOptOut = apps.get_model('ISASaplikacia', 'EmailOptOut')
    ImportDatTask = apps.get_model('Evidencia', 'ImportDatTask')
    AIAuditLog = apps.get_model('Evidencia', 'AIAuditLog')
    
    # Príprava dynamických filtrov (Q objekty pre časové ohraničenie)
    q_vystavene = Q()
    q_prijate = Q()
    q_uhrady = Q()
    q_ponuky = Q()
    q_objednavky_in = Q()
    q_objednavky_out = Q()
    q_tankovanie = Q()
    q_jazdy = Q()
    q_zaznamy = Q()
    q_ulohy = Q()
    q_pohyby = Q()
    q_ai_audit = Q()
    
    try:
        d_od = to_date(datum_od)
        d_do = to_date(datum_do)

        if d_od:
            q_vystavene &= Q(datum_vystavenia__gte=d_od)
            q_prijate &= Q(datum_prijatia__date__gte=d_od)
            q_uhrady &= Q(datum_uhrady__gte=d_od)
            q_ponuky &= Q(datum_vystavenia__gte=d_od)
            q_objednavky_in &= Q(datum_prijatia__gte=d_od)
            q_objednavky_out &= Q(vytvorene__date__gte=d_od)
            q_tankovanie &= Q(datum__date__gte=d_od)
            q_jazdy &= Q(datum_cas_start__date__gte=d_od)
            q_zaznamy &= Q(vytvorene__date__gte=d_od)
            q_ulohy &= Q(zaciatok__date__gte=d_od)
            q_pohyby &= Q(timestamp__date__gte=d_od)
            q_ai_audit &= Q(cas_dopytu__date__gte=d_od)
            
        if d_do:
            q_vystavene &= Q(datum_vystavenia__lte=d_do)
            q_prijate &= Q(datum_prijatia__date__lte=d_do)
            q_uhrady &= Q(datum_uhrady__lte=d_do)
            q_ponuky &= Q(datum_vystavenia__lte=d_do)
            q_objednavky_in &= Q(datum_prijatia__lte=d_do)
            q_objednavky_out &= Q(vytvorene__date__lte=d_do)
            q_tankovanie &= Q(datum__date__lte=d_do)
            q_jazdy &= Q(datum_cas_start__date__lte=d_do)
            q_zaznamy &= Q(vytvorene__date__lte=d_do)
            q_ulohy &= Q(zaciatok__date__lte=d_do)
            q_pohyby &= Q(timestamp__date__lte=d_do)
            q_ai_audit &= Q(cas_dopytu__date__lte=d_do)

        # RÝCHLE SQL AGREGÁCIE (O(1) DB Queries s Coalesce pre bezpečnosť)
        
        # --- ÚČTOVNÍCTVO A FINANCIE (P&L, DPH) ---
        zero_dec = Decimal('0.00')
        # 🛡️ OCHRANA P&L OBRATU: Vylúčenie nedaňových zálohových faktúr (ZALOHA) pred zdvojením tržieb
        vystavene_agg = VystavenaFaktura.objects.filter(q_vystavene).exclude(stav__in=['DRAFT', 'STORNO']).exclude(typ_dokladu='ZALOHA').aggregate(
            obrat_bez_dph=Coalesce(Sum('celkom_bez_dph'), zero_dec, output_field=DecimalField(max_digits=12, decimal_places=2)), 
            obrat_s_dph=Coalesce(Sum('celkom_s_dph'), zero_dec, output_field=DecimalField(max_digits=12, decimal_places=2)), 
            dph_vystup=Coalesce(Sum('celkom_dph'), zero_dec, output_field=DecimalField(max_digits=12, decimal_places=2)),
            pocet=Count('id')
        )
        prijate_agg = PrichodziaFaktura.objects.filter(q_prijate).aggregate(
            naklady_s_dph=Coalesce(Sum('suma_na_uhradu'), zero_dec, output_field=DecimalField(max_digits=12, decimal_places=2)), 
            pocet=Count('id')
        )
        
        # --- CASH-FLOW ---
        # 🛡️ PEŇAŽNÝ TOK: Vzájomné zápočty (VZ) sú nepeňažné operácie a nesmú skresľovať reálny cash-flow
        cash_in = EvidenciaUhrad.objects.filter(q_uhrady, vystavena_faktura__isnull=False).exclude(typ_uhrady='VZ').aggregate(suma=Coalesce(Sum('suma_uhrady'), zero_dec, output_field=DecimalField(max_digits=12, decimal_places=2)))
        cash_out = EvidenciaUhrad.objects.filter(q_uhrady, prijata_faktura__isnull=False).exclude(typ_uhrady='VZ').aggregate(suma=Coalesce(Sum('suma_uhrady'), zero_dec, output_field=DecimalField(max_digits=12, decimal_places=2)))

        # --- POHĽADÁVKY A ZÁVÄZKY (Aktuálny stav, nezávislý od dátumu!) ---
        dnes = timezone.now().date()
        pohladavky_agg = VystavenaFaktura.objects.filter(stav__in=['NACHYSTANA', 'ODOSLANA', 'PO_SPLATNOSTI']).aggregate(
            suma_s_dph=Coalesce(Sum('celkom_s_dph'), zero_dec, output_field=DecimalField(max_digits=12, decimal_places=2)), 
            po_splatnosti_suma=Coalesce(Sum('celkom_s_dph', filter=Q(stav='PO_SPLATNOSTI') | (Q(stav__in=['NACHYSTANA', 'ODOSLANA']) & Q(datum_splatnosti__lt=dnes))), zero_dec, output_field=DecimalField(max_digits=12, decimal_places=2)),
            pocet=Count('id')
        )
        zavazky_agg = PrichodziaFaktura.objects.filter(stav_uhrady='NEZAPLATENA').aggregate(
            suma_s_dph=Coalesce(Sum('suma_na_uhradu'), zero_dec, output_field=DecimalField(max_digits=12, decimal_places=2)), 
            po_splatnosti_suma=Coalesce(Sum('suma_na_uhradu', filter=Q(datum_splatnosti__lt=dnes)), zero_dec, output_field=DecimalField(max_digits=12, decimal_places=2)),
            pocet=Count('id')
        )

        # --- OBCHOD: PIPELINE A ZĽAVY ---
        ponuky_agg = CenovaPonuka.objects.filter(q_ponuky).aggregate(
            suma_bez_dph_aktivne=Coalesce(Sum('celkom_bez_dph', filter=Q(stav__in=['SENT', 'ACCEPTED', 'CONVERTED'])), zero_dec, output_field=DecimalField(max_digits=12, decimal_places=2)), 
            poskytnute_zlavy=Coalesce(Sum('hodnota_zlavy', filter=Q(stav__in=['ACCEPTED', 'CONVERTED'])), zero_dec, output_field=DecimalField(max_digits=12, decimal_places=2)),
            pocet_aktivnych=Count('id', filter=Q(stav__in=['SENT', 'ACCEPTED', 'CONVERTED']))
        )
        objednavky_in_agg = PrijataObjednavka.objects.filter(q_objednavky_in).exclude(stav='ZAMIETNUTA').aggregate(
            suma_bez_dph=Coalesce(Sum('celkova_suma_bez_dph'), zero_dec, output_field=DecimalField(max_digits=12, decimal_places=2)), pocet=Count('id')
        )

        # --- FLEET MANAGEMENT ---
        tankovanie_agg = Tankovanie.objects.filter(q_tankovanie).aggregate(
            naklady_palivo_s_dph=Coalesce(Sum('cena_celkom'), zero_dec, output_field=DecimalField(max_digits=12, decimal_places=2)), 
            vypite_litre=Coalesce(Sum('litre'), zero_dec, output_field=DecimalField(max_digits=12, decimal_places=2))
        )
        # OPRAVA 1: Presný výpočet KM na úrovni SQL riadkov (ignoruje prebiehajúce jazdy a chýbajúce štarty)
        jazdy_agg = EvidenciaJazd.objects.filter(q_jazdy, stav_km_koniec__isnull=False, stav_km_start__isnull=False).aggregate(
            km_spolu=Coalesce(Sum(F('stav_km_koniec') - F('stav_km_start')), 0, output_field=IntegerField())
        )
        najazdene_km_spolu = jazdy_agg['km_spolu'] or 0
        
        o_30_dni = add_calendar_interval(dnes, months=1) or (dnes + timedelta(days=30))
        stk_konci_pocet = Vozidlo.objects.filter(stk_plati_do__lte=o_30_dni).count()
        pzp_konci_pocet = Vozidlo.objects.filter(pzp_plati_do__lte=o_30_dni).count()
        kasko_konci_pocet = Vozidlo.objects.filter(kasko_plati_do__lte=o_30_dni).count()
        dialnicna_konci_pocet = Vozidlo.objects.filter(dialnicna_znamka_do__lte=o_30_dni).count()

        # --- CRM A AKTÍVA ---
        # 🛡️ EXAKTNOSŤ: Nepočítame vlastnú firmu medzi externých klientov
        crm_zakaznici = Zakaznik.objects.filter(je_anonymizovany=False, je_interny_profil=False).count()
        crm_dodavatelia = Dodavatel.objects.filter(je_anonymizovany=False).count()
        stroje_v_prenajme = ServisovaneZariadenie.objects.filter(v_prenajme_od_isas=True).count()
        stroje_volne_na_prenajom = ServisovaneZariadenie.objects.filter(je_urcene_na_prenajom=True, v_prenajme_od_isas=False).count()
        
        # --- IOT A TELEMETRIA ---
        AlarmLog = apps.get_model('Evidencia', 'AlarmLog')
        iot_aktivne_stroje = ServisovaneZariadenie.objects.filter(iot_je_aktivny=True).count()
        iot_kriticke_alarmy = AlarmLog.objects.filter(vyriesene=False, zavaznost='CRITICAL').count()
        iot_varovania = AlarmLog.objects.filter(vyriesene=False, zavaznost='WARNING').count()

        # --- OPERATÍVA A KVALITA ---
        ulohy_agg = Uloha.objects.filter(q_ulohy).aggregate(
            celkom=Count('id'),
            dokoncene=Count('id', filter=Q(dokoncene=True)),
            reklamacie=Count('id', filter=Q(je_reklamacia=True))
        )
        
        # MTTR (Mean Time To Resolution) pre reklamácie
        vybavene_reklamacie = Uloha.objects.filter(q_ulohy, je_reklamacia=True, dokoncene=True, koniec__isnull=False, zaciatok__isnull=False)
        mttr_agg = vybavene_reklamacie.annotate(
            rozdiel=ExpressionWrapper(F('koniec') - F('zaciatok'), output_field=fields.DurationField())
        ).aggregate(priemer=Avg('rozdiel'))
        mttr_raw = mttr_agg['priemer']
        mttr_dni = mttr_raw.days if hasattr(mttr_raw, 'days') else (int(mttr_raw) if mttr_raw else 0)
        zaznamy_agg = ServisnyZaznam.objects.filter(q_zaznamy).aggregate(
            pocet_vyjazdov=Count('id'), 
            priemerne_nps=Coalesce(Avg('hodnotenie_zakaznika'), 0.0, output_field=FloatField())
        )
        
        chyby_technika = ServisnyZaznam.objects.filter(q_zaznamy, koren_problemu='CHYBA_TECHNIKA').count()
        vadne_diely = ServisnyZaznam.objects.filter(q_zaznamy, koren_problemu='VADNY_DIEL').count()

        # --- SKLADOVÁ AKTIVITA ---
        LogNaskladnenia = apps.get_model('Evidencia', 'LogNaskladnenia')
        pohyby_in = StockMovement.objects.filter(q_pohyby, action='ADD').aggregate(kusy=Coalesce(Sum('count'), 0))
        pohyby_out = StockMovement.objects.filter(q_pohyby, action='REMOVE').aggregate(kusy=Coalesce(Sum('count'), 0))
        
        # OPRAVA: Počítame REÁLNE spotrebovaný materiál z podpísaných servisných záznamov, nie z plánovaných úloh!
        spotrebovany_material = PouzityMaterialVZaznamoch.objects.filter(zaznam__in=ServisnyZaznam.objects.filter(q_zaznamy)).aggregate(kusy=Coalesce(Sum('pocet'), 0))
        
        odoslane_objednavky_agg = OdoslanaObjednavka.objects.filter(q_objednavky_out).aggregate(pocet=Count('id'))
        cakajuce_na_naskladnenie_pocet = LogNaskladnenia.objects.filter(uspesne=False).exclude(navrhovana_kategoria='Sluzba').count()

        # --- HR A MZDY (Aktuálny stav) ---
        # 🛡️ ISAS ABSENCE ENGINE: Oddelenie kmeňových dát zamestnancov od absencií zamedzuje skresleniu súčtov (SQL Join Fan-out)
        hr_base = Zamestnanec.objects.filter(je_anonymizovany=False)
        hr_agg = hr_base.aggregate(
            mzdy_brutto_mesiac=Coalesce(Sum('mzda_brutto_mesiac'), zero_dec, output_field=DecimalField(max_digits=12, decimal_places=2)),
            zavazky_voci_firme=Coalesce(Sum('zavazky'), zero_dec, output_field=DecimalField(max_digits=12, decimal_places=2)),
            pocet=Count('id')
        )
        dovolenky_agg = hr_base.annotate(
            schvalena_dovolenka_dni=Coalesce(
                Sum('nepritomnosti__pocet_pracovnych_dni', filter=Q(nepritomnosti__typ='DOVOLENKA', nepritomnosti__stav='SCHVALENE')),
                0.0,
                output_field=FloatField()
            )
        ).aggregate(
            nevycerpane_dovolenky_dni=Coalesce(Sum(F('narok_na_dovolenku') - F('vycerpana_dovolenka') - F('schvalena_dovolenka_dni')), 0.0, output_field=FloatField())
        )
        hr_agg['nevycerpane_dovolenky_dni'] = dovolenky_agg['nevycerpane_dovolenky_dni']
        bozp_konci_pocet = Zamestnanec.objects.filter(je_anonymizovany=False, bozp_platne_do__lte=o_30_dni).count()

        Certifikat = apps.get_model('Evidencia', 'Certifikat')

        certifikaty_koncia_pocet = Certifikat.objects.filter(
            zamestnanec__je_anonymizovany=False,
            je_dozivotny=False,
            datum_platnosti__lte=o_30_dni
        ).count()
        
        PracovnaPozicia = apps.get_model('Evidencia', 'PracovnaPozicia')
        Kandidat = apps.get_model('Evidencia', 'Kandidat')
        SukromnaOsoba = apps.get_model('Evidencia', 'SukromnaOsoba')
        
        aktivne_pozicie_pocet = PracovnaPozicia.objects.filter(je_aktivna=True).count()
        novi_kandidati_pocet = Kandidat.objects.filter(stav='NOVY').count()
        pohovory_kandidati_pocet = Kandidat.objects.filter(stav='POHOVOR').count()
        crm_sukromne_osoby = SukromnaOsoba.objects.filter(je_anonymizovany=False).count()

        User = get_user_model()
        systemove_ucty_pocet = User.objects.filter(is_active=True).count()
        superadmini_pocet = User.objects.filter(is_active=True, is_superuser=True).count()

        ISASHraLeaderboardModel: Optional[type[Any]] = None
        try:
            ISASHraLeaderboardModel = apps.get_model('ISASaplikacia', 'ISASHraLeaderboard')
        except LookupError:
            ISASHraLeaderboardModel = None
        pocet_hracov_2048 = ISASHraLeaderboardModel.objects.count() if ISASHraLeaderboardModel else 0

        # --- DISPEČING A ADMINISTRATÍVA (Aktuálny stav - "Čo horí") ---
        otvorene_ulohy = Uloha.objects.filter(dokoncene=False, je_reklamacia=False).count()
        aktivne_reklamacie = Uloha.objects.filter(dokoncene=False, je_reklamacia=True).count()
        nove_objednavky_klientov = PrijataObjednavka.objects.filter(stav='NOVA').count()
        nepodpisane_protokoly = ServisnyZaznam.objects.filter(protokol_uzavrety=False).count()
        # 🛡️ 2-FÁZOVÝ AUDIT: Presné rozdelenie konceptov čakajúcich na dispečing vs reálne odoslaných čakajúcich na SBS
        vstupy_caka_dispecing = PovolenieNaVstup.objects.filter(stav='GENEROVANE').count()
        vstupy_caka_sbs_schvalenie = PovolenieNaVstup.objects.filter(stav='ODOSLANE').count()
        nespracovane_prijate_faktury = PrichodziaFaktura.objects.filter(spracovane=False).count()
        draft_vystavene_faktury = VystavenaFaktura.objects.filter(stav='DRAFT').count()
        nachystane_vystavene_faktury = VystavenaFaktura.objects.filter(stav='NACHYSTANA').count()
        draft_cenove_ponuky = CenovaPonuka.objects.filter(stav='DRAFT').count()
        zlyhane_peppol_faktury = VystavenaFaktura.objects.filter(peppol_status='ERROR').count()
        uspesne_peppol_faktury = VystavenaFaktura.objects.filter(peppol_status='SENT').count()
        zlyhane_zalohy = MegaZaloha.objects.filter(stav_spracovania='chyba').count()
        zlyhane_importy = ImportDatTask.objects.filter(stav='CHYBA').count()
        neprecitane_maily = EmailovaSprava.objects.filter(precitane=False, zlozka='INBOX').count()
        kosik_pocet = KosikSPolozkamiNaDoobjednanie.objects.filter(objednane=False).count()
        try:
            WebhookLog = apps.get_model('WebBuilder', 'WebhookLog')
            WebhookEndpoint = apps.get_model('WebBuilder', 'WebhookEndpoint')
            zlyhane_webhooky_pocet = WebhookLog.objects.filter(uspesne=False).count()
            deaktivovane_webhooky_pocet = WebhookEndpoint.objects.filter(je_aktivny=False).exclude(dovod_deaktivacie__isnull=True).exclude(dovod_deaktivacie='').count()
        except LookupError:
            zlyhane_webhooky_pocet = 0
            deaktivovane_webhooky_pocet = 0

        ai_naklady_agg = AIAuditLog.objects.filter(q_ai_audit).aggregate(
            celkove_naklady_usd=Coalesce(Sum('presna_cena_usd'), zero_dec, output_field=DecimalField(max_digits=12, decimal_places=2)),
            pocet_dopytov=Count('id')
        )
        
        # Manažérsky predstih riadený priamo z GlobalneNastavenia
        cfg_stat = get_compliance_config()
        stat_predstih_dni = int(cfg_stat.get('predstih_dni') or 14)
        stat_predstih_mh = int(cfg_stat.get('predstih_motohodiny') or 100)
        limit_stat_datum = dnes + timedelta(days=stat_predstih_dni)

        podmienky_stat_servis = (
            Q(planovany_datum__isnull=False, planovany_datum__lte=limit_stat_datum) |
            Q(planovane_motohodiny__isnull=False, planovane_motohodiny__lte=F('zariadenie__aktualne_motohodiny') + stat_predstih_mh)
        )
        if hasattr(PlanovanyServisZariadenia, 'planovane_motohodiny_zataz'):
            podmienky_stat_servis |= Q(planovane_motohodiny_zataz__isnull=False, planovane_motohodiny_zataz__lte=F('zariadenie__motohodiny_v_zatazi') + stat_predstih_mh)

        planovane_servisy_pocet = PlanovanyServisZariadenia.objects.filter(
            Q(upozornenie_odoslane=False) & 
            Q(zariadenie__v_servisnom_rezime=False) &
            podmienky_stat_servis
        ).count()

        expirujuce_revizie_pocet = len(get_global_compliance_queue(referencny_datum=dnes, limit_dni_alert=stat_predstih_dni, filter_alerts_only=True))

        # 4. ZOSTAVENIE JSON VÝSTUPU
        data = {
            "report_info": {
                "typ_reportu": typ_reportu or "Komplexný ERP Prehľad",
                "filtrovane_od": datum_od or "Od začiatku",
                "filtrovane_do": datum_do or "Do dnes"
            },
            "financie_a_uctovnictvo": {
                "vystavene_faktury_obrat_bez_dph_eur": float(vystavene_agg['obrat_bez_dph']),
                "vystavene_faktury_obrat_s_dph_eur": float(vystavene_agg['obrat_s_dph']),
                "dph_na_vystupe_eur": float(vystavene_agg['dph_vystup']),
                "pocet_vystavenych_faktur": vystavene_agg['pocet'],
                "prijate_faktury_naklady_s_dph_eur": float(prijate_agg['naklady_s_dph']),
                "pocet_prijatych_faktur": prijate_agg['pocet'],
                "realne_prijate_uhrady_od_klientov_eur": float(cash_in['suma']),
                "realne_odoslane_uhrady_dodavatelom_eur": float(cash_out['suma']),
                "nase_pohladavky_nezaplatene_faktury_klientmi_eur": float(pohladavky_agg['suma_s_dph']),
                "z_toho_po_splatnosti_eur": float(pohladavky_agg['po_splatnosti_suma']),
                "nase_zavazky_nezaplatene_faktury_dodavatelom_eur": float(zavazky_agg['suma_s_dph']),
                "z_toho_nase_zavazky_po_splatnosti_eur": float(zavazky_agg['po_splatnosti_suma'])
            },
            "obchod_a_cenotvorba": {
                "hodnota_aktivnych_ponuk_bez_dph_eur": float(ponuky_agg['suma_bez_dph_aktivne']),
                "poskytnute_zlavy_klientom_eur": float(ponuky_agg['poskytnute_zlavy']),
                "pocet_aktivnych_ponuk": ponuky_agg['pocet_aktivnych'],
                "hodnota_prijatych_objednavok_bez_dph_eur": float(objednavky_in_agg['suma_bez_dph']),
                "pocet_prijatych_objednavok": objednavky_in_agg['pocet']
            },
            "operativa_a_dispecing": {
                "ulohy_celkom_v_obdobi": ulohy_agg['celkom'],
                "ulohy_dokoncene": ulohy_agg['dokoncene'],
                "ulohy_reklamacie": ulohy_agg['reklamacie'],
                "aktualne_otvorene_ulohy_v_rieseni": otvorene_ulohy,
                "aktualne_aktivne_reklamacie_v_rieseni": aktivne_reklamacie,
                "nove_nespracovane_objednavky_klientov": nove_objednavky_klientov,
                "nepodpisane_servisne_protokoly_aktualne": nepodpisane_protokoly,
                "povolenia_na_vstup_cakajuce_na_odoslanie_dispecingom": vstupy_caka_dispecing,
                "povolenia_na_vstup_odoslane_cakajuce_na_sbs_schvalenie": vstupy_caka_sbs_schvalenie,
                "rozpracovane_vystavene_faktury_draft_aktualne": draft_vystavene_faktury,
                "vystavene_faktury_nachystane_na_kontrolu_a_odoslanie": nachystane_vystavene_faktury,
                "rozpracovane_cenove_ponuky_draft_aktualne": draft_cenove_ponuky,
                "zlyhane_efaktury_peppol_aktualne": zlyhane_peppol_faktury,
                "uspesne_odoslane_efaktury_peppol_aktualne": uspesne_peppol_faktury,
                "prijate_faktury_cakajuce_na_ai_vytazenie_aktualne": nespracovane_prijate_faktury,
                "expirujuce_zakonne_revizie": expirujuce_revizie_pocet
            },
            "ludske_zdroje_hr": {
                "celkovy_pocet_zamestnancov": hr_agg['pocet'],
                "mesacne_mzdove_naklady_brutto_eur": float(hr_agg['mzdy_brutto_mesiac']),
                "zavazky_zamestnancov_voci_firme_eur": float(hr_agg['zavazky_voci_firme']),
                "nevycerpane_dovolenky_zamestnancov_dni_spolu": float(hr_agg['nevycerpane_dovolenky_dni']),
                "zamestnanci_s_konciacim_bozp_do_30_dni": bozp_konci_pocet,
                "zamestnanci_s_konciacim_certifikatom_do_30_dni": certifikaty_koncia_pocet,
                "aktivne_pracovne_pozicie_nabor": aktivne_pozicie_pocet,
                "novi_uchadzaci_o_pracu": novi_kandidati_pocet,
                "uchadzaci_v_stave_pohovor": pohovory_kandidati_pocet,
                "aktivne_systemove_ucty_pocet": systemove_ucty_pocet,
                "z_toho_superadmini": superadmini_pocet,
                "aktivni_hraci_2048_pocet": pocet_hracov_2048
            },
            "fleet_a_logistika": {
                "naklady_na_palivo_s_dph_eur": float(tankovanie_agg['naklady_palivo_s_dph']),
                "spotrebovane_palivo_litre": float(tankovanie_agg['vypite_litre']),
                "najazdene_kilometre_spolu": najazdene_km_spolu,
                "vozidla_s_konciacou_stk_do_30_dni": stk_konci_pocet,
                "vozidla_s_konciacim_pzp_do_30_dni": pzp_konci_pocet,
                "vozidla_s_konciacim_kasko_do_30_dni": kasko_konci_pocet,
                "vozidla_s_konciacou_dialnicnou_do_30_dni": dialnicna_konci_pocet
            },
            "sklad_a_nakup": {
                "prijate_na_sklad_kusy_vybrane_obdobie": float(pohyby_in['kusy']),
                "vydane_zo_skladu_kusy_vybrane_obdobie": float(pohyby_out['kusy']),
                "spotrebovany_material_na_vyjazdoch_kusy": float(spotrebovany_material['kusy']),
                "pocet_odoslanych_objednavok_dodavatelom_vybrane_obdobie": odoslane_objednavky_agg['pocet'],
                "pocet_poloziek_cakajucich_v_kosiku_aktualne": kosik_pocet,
                "pocet_poloziek_cakajucich_na_naskladnenie_v_dashboarde": cakajuce_na_naskladnenie_pocet
            },
            "kvalita_a_iso9001": {
                "pocet_vykonanych_servisov_vybrane_obdobie": zaznamy_agg['pocet_vyjazdov'],
                "priemerne_hodnotenie_nps_vybrane_obdobie": round(float(zaznamy_agg['priemerne_nps']), 2),
                "iso9001_reklamacie_chyba_technika": chyby_technika,
                "iso9001_reklamacie_vadny_diel": vadne_diely,
                "priemerny_cas_vybavenia_reklamacie_mttr_dni": mttr_dni
            },
            "it_a_system": {
                "zlyhane_systemove_zalohy": zlyhane_zalohy,
                "zlyhane_importy_dat": zlyhane_importy,
                "pocet_neprecitanych_emailov": neprecitane_maily,
                "pocet_aktivnych_opt_out_emailov": EmailOptOut.objects.filter(aktivny=True).count(),
                "pocet_opt_out_kompletny_zakaz": EmailOptOut.objects.filter(aktivny=True, rozsah='KOMPLETNY').count(),
                "zlyhane_webhook_odoslania": zlyhane_webhooky_pocet,
                "deaktivovane_webhook_endpointy_circuit_breaker": deaktivovane_webhooky_pocet,
                "pocet_bliziacich_sa_planovanych_servisov": planovane_servisy_pocet,
                "ai_asistent_pocet_dopytov": ai_naklady_agg['pocet_dopytov'],
                "ai_asistent_naklady_usd": float(ai_naklady_agg['celkove_naklady_usd']),
                "ai_asistent_aktivny_model": getattr(settings, 'GEMINI_MODEL', 'gemini-4-hydrogen')
            },
            "crm_a_aktiva": {
                "pocet_zakaznikov": crm_zakaznici,
                "pocet_dodavatelov": crm_dodavatelia,
                "pocet_sukromnych_osob": crm_sukromne_osoby,
                "pocet_strojov_v_prenajme": stroje_v_prenajme,
                "pocet_volnych_strojov_na_prenajom_sklad": stroje_volne_na_prenajom
            },
            "iot_a_telemetria": {
                "pocet_strojov_s_aktivnym_iot": iot_aktivne_stroje,
                "aktualne_nevyriesene_kriticke_alarmy": iot_kriticke_alarmy,
                "aktualne_nevyriesene_varovania": iot_varovania
            }
        }
        
        return json.dumps(data)
        
    except ValueError:
        return json.dumps({"error": "Neplatný formát dátumu. Použite formát YYYY-MM-DD."})
    except Exception as e:
        return json.dumps({"error": f"Chyba pri výpočte štatistík: {str(e)}"})

def tool_erp_hlbkova_analyza_a_vykazy(typ_analyzy: str, identifikator: str, datum_od: Optional[str] = None, datum_do: Optional[str] = None, limit: int = 20, offset: int = 0) -> str:
    """
    Použi na HĹBKOVÝ DRILL-DOWN a VÝKAZY.
    'typ_analyzy' MUSÍ byť presne jeden z týchto kľúčov. 'identifikator' MUSÍ byť vyplnený podľa týchto prísnych pravidiel:
    1. 'vykaz_technika' -> identifikator = "IBA krstné meno ALEBO IBA priezvisko technika" (Nikdy nie obe naraz!)
    2. 'ziskovost_zakaznika' -> identifikator = "Názov zákazníka"
    3. 'analyza_dodavatela' -> identifikator = "Názov dodávateľa"
    4. 'analyza_vozidla' -> identifikator = "ŠPZ vozidla"
    5. Pre všetky ostatné kľúče ('uctovny_audit', 'nevyfakturovana_praca', 'analyza_skladu', 'analyza_prenajmov', 'hr_audit', 'saldo_pohladavky', 'saldo_zavazky', 'backlog_objednavok', 'analyza_kvality', 'obchodna_pipeline', 'hr_expiracie', 'fleet_expiracie', 'planovane_servisy', 'systemovy_audit', 'ai_audit', 'crm_udalosti', 'nenaskladneny_tovar', 'crm_audit', 'leaderboard_2048') -> identifikator = "ALL" (alebo hľadané meno hráča / kód/názov dielu/dodávateľa)
    
    Dátumy zadávaj VÝHRADNE vo formáte YYYY-MM-DD. Ak ide o aktuálny stav, dátumy vynechaj (null).
    """
    # 🛡️ DEFENSIVE TYPECASTING
    try:
        limit = max(1, min(int(limit), 100)) if limit is not None else 20
        offset = max(0, int(offset)) if offset is not None else 0
    except (ValueError, TypeError):
        limit, offset = 20, 0
    
    Zamestnanec = apps.get_model('Evidencia', 'Zamestnanec')
    Zakaznik = apps.get_model('Evidencia', 'Zakaznik')
    Dodavatel = apps.get_model('Evidencia', 'Dodavatel')
    Uloha = apps.get_model('Evidencia', 'Uloha')
    EvidenciaJazd = apps.get_model('Evidencia', 'EvidenciaJazd')
    VystavenaFaktura = apps.get_model('Evidencia', 'VystavenaFaktura')
    PrichodziaFaktura = apps.get_model('Evidencia', 'PrichodziaFaktura')
    EvidenciaUhrad = apps.get_model('Evidencia', 'EvidenciaUhrad')
    Vozidlo = apps.get_model('Evidencia', 'Vozidlo')
    Tankovanie = apps.get_model('Evidencia', 'Tankovanie')
    StockMovement = apps.get_model('Evidencia', 'StockMovement')
    OdoslanaObjednavka = apps.get_model('Evidencia', 'OdoslanaObjednavka')
    ServisovaneZariadenie = apps.get_model('Evidencia', 'ServisovaneZariadenie')
    HistoriaPrenajmuLog = apps.get_model('Evidencia', 'HistoriaPrenajmuLog')
    PrijataObjednavka = apps.get_model('Evidencia', 'PrijataObjednavka')
    PlanovanyServisZariadenia = apps.get_model('Evidencia', 'PlanovanyServisZariadenia')
    SukromnaOsoba = apps.get_model('Evidencia', 'SukromnaOsoba')
    Sklad = apps.get_model('Evidencia', 'Sklad')
    hlavny_sklad = Sklad.objects.first()
    nazov_hlavneho = f"Hlavný sklad ({hlavny_sklad.nazov})" if hlavny_sklad and hlavny_sklad.nazov and hlavny_sklad.nazov.lower() != "hlavný sklad" else "Hlavný sklad"
    
    try:
        d_od = to_date(datum_od)
        d_do = to_date(datum_do)
        
        # 👨‍🔧 1. MODUL PRE TECHNIKOV (Výkazy práce)
        if typ_analyzy == 'vykaz_technika':
            clean_id = str(identifikator).strip()
            if clean_id.upper() in ['ALL', '', 'NONE']:
                technik = Zamestnanec.objects.filter(je_anonymizovany=False, pracovna_rola='TE').first()
            else:
                q_technik = Q(meno__icontains=clean_id) | Q(priezvisko__icontains=clean_id)
                casti_mena = clean_id.split()
                if len(casti_mena) >= 2:
                    q_technik |= (Q(meno__icontains=casti_mena[0]) & Q(priezvisko__icontains=casti_mena[-1])) | (Q(meno__icontains=casti_mena[-1]) & Q(priezvisko__icontains=casti_mena[0]))
                technik = Zamestnanec.objects.filter(je_anonymizovany=False).filter(q_technik).first()
                
            if not technik: return json.dumps({"error": f"Technik '{identifikator}' sa nenašiel v aktívnych zamestnancoch."})
            
            q_ulohy = Q(technici=technik, dokoncene=True)
            # 🛡️ SQL NULL GUARD: Počítame iba dokončené jazdy s platným tachometrom
            q_jazdy = Q(zamestnanec=technik, stav_km_koniec__isnull=False, stav_km_start__isnull=False)
            if d_od: 
                q_ulohy &= Q(zaciatok__date__gte=d_od)
                q_jazdy &= Q(datum_cas_start__date__gte=d_od)
            if d_do: 
                q_ulohy &= Q(zaciatok__date__lte=d_do)
                q_jazdy &= Q(datum_cas_start__date__lte=d_do)
                
            ulohy_agg = Uloha.objects.filter(q_ulohy).aggregate(pocet=Count('id'))
            jazdy_agg = EvidenciaJazd.objects.filter(q_jazdy).aggregate(
                km_spolu=Coalesce(Sum(F('stav_km_koniec') - F('stav_km_start')), 0, output_field=IntegerField()),
                pocet_jazd=Count('id')
            )
            
            return json.dumps({
                "technik": f"{technik.meno} {technik.priezvisko}",
                "rola": technik.get_pracovna_rola_display(),
                "hodinovy_naklad_eur": float(technik.interny_hodinovy_naklad or 0.0),
                "dokoncene_ulohy_pocet": ulohy_agg['pocet'],
                "najazdene_km_spolu": jazdy_agg['km_spolu'],
                "pocet_jazd": jazdy_agg['pocet_jazd']
            })
            
        # 👑 2. MODUL PRE MAJITEĽA (Ziskovosť zákazníka)
        elif typ_analyzy == 'ziskovost_zakaznika':
            clean_id = str(identifikator).strip() if identifikator is not None else ""
            if not clean_id or clean_id.upper() in ['ALL', 'NONE', '']:
                return json.dumps({"error": "Pre analýzu ziskovosti zákazníka musíte zadať konkrétny názov zákazníka."})
            zakaznik = Zakaznik.objects.filter(je_anonymizovany=False, Nazov_spolocnosti__icontains=clean_id).first()
            if not zakaznik: return json.dumps({"error": f"Zákazník {clean_id} nenájdený."})
            
            q_faktury = Q(zakaznik=zakaznik, stav__in=['NACHYSTANA', 'ODOSLANA', 'ZAPLATENA', 'PO_SPLATNOSTI']) & ~Q(typ_dokladu='ZALOHA')
            q_ulohy = Q(zakaznik=zakaznik, dokoncene=True)
            if d_od: 
                q_faktury &= Q(datum_vystavenia__gte=d_od)
                q_ulohy &= Q(zaciatok__date__gte=d_od)
            if d_do: 
                q_faktury &= Q(datum_vystavenia__lte=d_do)
                q_ulohy &= Q(zaciatok__date__lte=d_do)
                
            faktury_agg = VystavenaFaktura.objects.filter(q_faktury).aggregate(
                obrat_bez_dph=Coalesce(Sum('celkom_bez_dph'), Decimal('0.00'), output_field=DecimalField(max_digits=12, decimal_places=2)),
                pocet_faktur=Count('id')
            )
            ulohy_agg = Uloha.objects.filter(q_ulohy).aggregate(
                pocet_vyjazdov=Count('id'),
                pocet_reklamacii=Count('id', filter=Q(je_reklamacia=True))
            )
            
            return json.dumps({
                "zakaznik": zakaznik.Nazov_spolocnosti,
                "vygenerovany_obrat_bez_dph_eur": float(faktury_agg['obrat_bez_dph']),
                "pocet_vystavenych_faktur": faktury_agg['pocet_faktur'],
                "pocet_zrealizovanych_vyjazdov": ulohy_agg['pocet_vyjazdov'],
                "z_toho_reklamacii": ulohy_agg['pocet_reklamacii']
            })

        # 🛒 3. MODUL PRE NÁKUP (Analýza dodávateľa)
        elif typ_analyzy == 'analyza_dodavatela':
            clean_id = str(identifikator).strip() if identifikator is not None else ""
            if not clean_id or clean_id.upper() in ['ALL', 'NONE', '']:
                return json.dumps({"error": "Pre analýzu dodávateľa musíte zadať konkrétny názov dodávateľa."})
            dodavatel = Dodavatel.objects.filter(je_anonymizovany=False, Nazov_spolocnosti__icontains=clean_id).first()
            if not dodavatel: return json.dumps({"error": f"Dodávateľ {clean_id} nenájdený."})

            q_faktury = Q(dodavatel=dodavatel)
            q_objednavky = Q(dodavatel=dodavatel)
            if d_od:
                q_faktury &= Q(datum_prijatia__date__gte=d_od)
                q_objednavky &= Q(vytvorene__date__gte=d_od)
            if d_do:
                q_faktury &= Q(datum_prijatia__date__lte=d_do)
                q_objednavky &= Q(vytvorene__date__lte=d_do)

            faktury_agg = PrichodziaFaktura.objects.filter(q_faktury).aggregate(
                naklady_s_dph=Coalesce(Sum('suma_na_uhradu'), Decimal('0.00'), output_field=DecimalField(max_digits=12, decimal_places=2)),
                pocet=Count('id')
            )
            objednavky_agg = OdoslanaObjednavka.objects.filter(q_objednavky).aggregate(pocet=Count('id'))

            return json.dumps({
                "dodavatel": dodavatel.Nazov_spolocnosti,
                "celkove_naklady_s_dph_eur": float(faktury_agg['naklady_s_dph']),
                "pocet_prijatych_faktur": faktury_agg['pocet'],
                "pocet_odoslanych_objednavok": objednavky_agg['pocet']
            })
            
        # 👩‍💻 4. MODUL PRE ÚČTOVNÍČKU (Hľadanie anomálií)
        elif typ_analyzy == 'uctovny_audit':
            q_uhrady = Q(vystavena_faktura__isnull=True, prijata_faktura__isnull=True)
            if d_od: q_uhrady &= Q(datum_uhrady__gte=d_od)
            if d_do: q_uhrady &= Q(datum_uhrady__lte=d_do)
            
            nesparovane_uhrady = EvidenciaUhrad.objects.filter(q_uhrady).count()
            faktury_bez_vs = VystavenaFaktura.objects.filter(Q(variabilny_symbol__isnull=True) | Q(variabilny_symbol=''), stav__in=['ODOSLANA', 'ZAPLATENA', 'PO_SPLATNOSTI']).count()
            prijate_bez_vs = PrichodziaFaktura.objects.filter(Q(variabilny_symbol__isnull=True) | Q(variabilny_symbol='')).count()
            
            return json.dumps({
                "audit_typ": "Účtovné anomálie a chýbajúce dáta",
                "nesparovane_platby_v_banke_pocet": nesparovane_uhrady,
                "vystavene_faktury_bez_variabilneho_symbolu": faktury_bez_vs,
                "prijate_faktury_bez_variabilneho_symbolu": prijate_bez_vs
            })

        # 💸 5. MODUL PRE ÚČTOVNÍČKU/DISPEČING (Nevyfakturovaná práca)
        elif typ_analyzy == 'nevyfakturovana_praca':
            q_ulohy = Q(dokoncene=True, faktury__isnull=True, je_reklamacia=False)
            if d_od: q_ulohy &= Q(zaciatok__date__gte=d_od)
            if d_do: q_ulohy &= Q(zaciatok__date__lte=d_do)

            nevyfakturovane = Uloha.objects.filter(q_ulohy).select_related('zakaznik').prefetch_related('technici', 'zariadenia')
            zoznam = []
            for u in nevyfakturovane[offset:offset+limit]:
                zoznam.append({
                    "uloha_id": u.id,
                    "zakaznik": u.zakaznik.Nazov_spolocnosti if u.zakaznik else "Neznámy",
                    "datum_vykonania": u.zaciatok.strftime('%d.%m.%Y') if u.zaciatok else "Neznáme",
                    "technici": ", ".join([t.priezvisko for t in u.technici.all()]),
                    "zariadenia": ", ".join([z.model_zariadenia for z in u.zariadenia.all()])
                })

            return json.dumps({
                "audit_typ": "Dokončená, ale nevyfakturovaná práca (WIP)",
                "celkovy_pocet_nevyfakturovanych_uloh": nevyfakturovane.count(),
                "ukazka_najstarsich": zoznam
            })

        # 🎧 6. MODUL PRE DISPEČERA / FLEET (Analýza konkrétneho auta)
        elif typ_analyzy == 'analyza_vozidla':
            clean_id = str(identifikator).strip() if identifikator is not None else ""
            if not clean_id or clean_id.upper() in ['ALL', 'NONE', '']:
                return json.dumps({"error": "Pre analýzu vozidla musíte zadať konkrétnu ŠPZ vozidla."})
            vozidlo = Vozidlo.objects.filter(spz__icontains=clean_id).first()
            if not vozidlo: return json.dumps({"error": f"Vozidlo so ŠPZ {clean_id} nenájdené."})

            q_tank = Q(vozidlo=vozidlo)
            q_jazd = Q(vozidlo=vozidlo, stav_km_koniec__isnull=False, stav_km_start__isnull=False)
            if d_od:
                q_tank &= Q(datum__date__gte=d_od)
                q_jazd &= Q(datum_cas_start__date__gte=d_od)
            if d_do:
                q_tank &= Q(datum__date__lte=d_do)
                q_jazd &= Q(datum_cas_start__date__lte=d_do)

            tank_agg = Tankovanie.objects.filter(q_tank).aggregate(
                naklady=Coalesce(Sum('cena_celkom'), Decimal('0.00'), output_field=DecimalField(max_digits=12, decimal_places=2)),
                litre=Coalesce(Sum('litre'), Decimal('0.00'), output_field=DecimalField(max_digits=12, decimal_places=2))
            )
            jazd_agg = EvidenciaJazd.objects.filter(q_jazd).aggregate(
                km=Coalesce(Sum(F('stav_km_koniec') - F('stav_km_start')), 0, output_field=IntegerField())
            )

            return json.dumps({
                "vozidlo": f"{vozidlo.znacka_a_model} ({vozidlo.spz})",
                "naklady_na_palivo_eur": float(tank_agg['naklady']),
                "natankovane_litre": float(tank_agg['litre']),
                "najazdene_km": jazd_agg['km']
            })

        # 📦 7. MODUL PRE SKLADNÍKA (Obratovosť skladu)
        elif typ_analyzy == 'analyza_skladu':
            q_pohyby = Q()
            if d_od: q_pohyby &= Q(timestamp__date__gte=d_od)
            if d_do: q_pohyby &= Q(timestamp__date__lte=d_do)

            najviac_vydavane = list(StockMovement.objects.filter(q_pohyby, action='REMOVE')
                                    .values('product_name', 'merna_jednotka', 'typ_dokladu_erp')
                                    .annotate(spolu_vydane=Sum('count'))
                                    .order_by('-spolu_vydane')[:5])

            najviac_prijimane = list(StockMovement.objects.filter(q_pohyby, action='ADD')
                                     .values('product_name', 'merna_jednotka', 'typ_dokladu_erp')
                                     .annotate(spolu_prijate=Sum('count'))
                                     .order_by('-spolu_prijate')[:5])

            for item_vyd in najviac_vydavane:
                item_vyd['spolu_vydane'] = float(item_vyd['spolu_vydane']) if item_vyd.get('spolu_vydane') is not None else 0.0
            for item_prij in najviac_prijimane:
                item_prij['spolu_prijate'] = float(item_prij['spolu_prijate']) if item_prij.get('spolu_prijate') is not None else 0.0

            return json.dumps({
                "audit_typ": "Obratovosť skladu (Top 5 položiek)",
                "najviac_vydavane_polozky": najviac_vydavane,
                "najviac_prijimane_polozky": najviac_prijimane
            }, cls=DjangoJSONEncoder)

        # 🤝 8. MODUL PRE OBCHOD (Prenájmy a Rental flotila)
        elif typ_analyzy == 'analyza_prenajmov':
            clean_id = str(identifikator).strip() if identifikator is not None else ""
            
            qs_prenajate = ServisovaneZariadenie.objects.filter(v_prenajme_od_isas=True).select_related('zakaznik', 'vyrobca')
            qs_volne = ServisovaneZariadenie.objects.filter(je_urcene_na_prenajom=True, v_prenajme_od_isas=False).select_related('zakaznik', 'vyrobca')
            qs_historia = HistoriaPrenajmuLog.objects.select_related('zariadenie__vyrobca', 'povodny_zakaznik', 'novy_zakaznik', 'vykonal').order_by('-datum_presunu', '-id')

            if d_od:
                qs_historia = qs_historia.filter(datum_presunu__gte=d_od)
            if d_do:
                qs_historia = qs_historia.filter(datum_presunu__lte=d_do)

            if clean_id and clean_id.upper() not in ['ALL', 'NONE', '']:
                q_filter = Q(zakaznik__Nazov_spolocnosti__icontains=clean_id) | Q(vyrobne_cislo__icontains=clean_id) | Q(model_zariadenia__icontains=clean_id)
                qs_prenajate = qs_prenajate.filter(q_filter)
                qs_volne = qs_volne.filter(q_filter)
                qs_historia = qs_historia.filter(
                    Q(zariadenie__vyrobne_cislo__icontains=clean_id) |
                    Q(zariadenie__model_zariadenia__icontains=clean_id) |
                    Q(povodny_zakaznik__Nazov_spolocnosti__icontains=clean_id) |
                    Q(novy_zakaznik__Nazov_spolocnosti__icontains=clean_id)
                )

            zoznam_prenajatych = []
            for p in qs_prenajate[offset:offset+limit]:
                zoznam_prenajatych.append({
                    "id": p.id,
                    "zariadenie": f"{p.vyrobca.nazov_vyrobcu if p.vyrobca else ''} {p.model_zariadenia or p.nazov}".strip() + f" (SN: {p.vyrobne_cislo})",
                    "aktualny_najomca": p.zakaznik.Nazov_spolocnosti if p.zakaznik else "Neznámy",
                    "umiestnenie_u_klienta": p.umiestnenie or "Neuvedené",
                    "denna_sadzba_eur": float(p.cena_prenajmu_na_den) if p.cena_prenajmu_na_den else "Na vyžiadanie",
                    "stav_pocitadla": getattr(p, 'meter_display', None) or f"{p.aktualne_motohodiny} {get_industry_term('meter_unit', 'hod.')}",
                    "historia_a_poznamka": p.poznamka or "Bez poznámky"
                })

            zoznam_volnych = []
            for v in qs_volne[offset:offset+limit]:
                zoznam_volnych.append({
                    "id": v.id,
                    "zariadenie": f"{v.vyrobca.nazov_vyrobcu if v.vyrobca else ''} {v.model_zariadenia or v.nazov}".strip() + f" (SN: {v.vyrobne_cislo})",
                    "stav": "🟢 Voľné na prenájom (Na centrále / v sklade)",
                    "lokalita": v.umiestnenie or "Centrálny sklad / Dielňa",
                    "denna_sadzba_eur": float(v.cena_prenajmu_na_den) if v.cena_prenajmu_na_den else "Na vyžiadanie",
                    "stav_pocitadla": getattr(v, 'meter_display', None) or f"{v.aktualne_motohodiny} {get_industry_term('meter_unit', 'hod.')}",
                    "historia_a_poznamka": v.poznamka or "Bez predchádzajúcej histórie"
                })

            zoznam_historie = []
            for h in qs_historia[offset:offset+limit]:
                zoznam_historie.append({
                    "datum": h.datum_presunu.strftime('%d.%m.%Y'),
                    "zariadenie": f"{h.zariadenie.vyrobca.nazov_vyrobcu if h.zariadenie.vyrobca else ''} {h.zariadenie.model_zariadenia or h.zariadenie.nazov}".strip() + f" (SN: {h.zariadenie.vyrobne_cislo})",
                    "typ_akcie": h.get_typ_akcie_display(),
                    "odovzdal": h.povodny_zakaznik.Nazov_spolocnosti if h.povodny_zakaznik else "Centrála / Sklad",
                    "prevzal": h.novy_zakaznik.Nazov_spolocnosti if h.novy_zakaznik else "Centrála / Sklad",
                    "stav_pocitadla": f"{h.stav_pocitadla} {getattr(h.zariadenie, 'meter_unit', None) or get_industry_term('meter_unit', 'hod.')}" if h.stav_pocitadla is not None else "Nezadaný",
                    "protokol": h.cislo_protokolu or "Neuvedený",
                    "nove_umiestnenie": h.nove_umiestnenie or "Centrálny sklad",
                    "vykonal": h.vykonal.get_full_name() if h.vykonal else "Systém",
                    "poznamka": h.poznamka or "Bez poznámky"
                })

            return json.dumps({
                "audit_typ": "Prehľad prenájmov a rental flotily",
                "hladany_vyraz": clean_id if clean_id and clean_id.upper() not in ['ALL', 'NONE'] else "Všetky stroje",
                "celkovy_pocet_aktualne_prenajatych": qs_prenajate.count(),
                "celkovy_pocet_volnych_na_sklade": qs_volne.count(),
                "celkovy_pocet_pohybov_v_obdobi": qs_historia.count(),
                "prenajate_stroje_u_klientov": zoznam_prenajatych,
                "volne_stroje_pripravene_na_prenajom": zoznam_volnych,
                "historia_pohybov_a_presunov": zoznam_historie
            })

        # 👥 9. MODUL PRE HR (Záväzky, Dovolenky, Absencie a Nábor) - ROZŠÍRENÉ
        elif typ_analyzy == 'hr_audit':
            zamestnanci = Zamestnanec.objects.filter(je_anonymizovany=False)
            Nepritomnost = apps.get_model('Evidencia', 'Nepritomnost')
            PracovnaPozicia = apps.get_model('Evidencia', 'PracovnaPozicia')
            Kandidat = apps.get_model('Evidencia', 'Kandidat')
            
            dlznici: list[dict[str, Any]] = [dict(d) for d in zamestnanci.filter(zavazky__gt=0).values('meno', 'priezvisko', 'zavazky', 'poznamka_zavazky')]
            for d in dlznici: d['zavazky'] = float(d['zavazky'])
                
            dovolenky = list(zamestnanci.annotate(
                vycerpane_absencie=Coalesce(
                    Sum('nepritomnosti__pocet_pracovnych_dni', filter=Q(nepritomnosti__typ='DOVOLENKA', nepritomnosti__stav='SCHVALENE')),
                    0.0,
                    output_field=FloatField()
                ),
                zostatok=ExpressionWrapper(
                    F('narok_na_dovolenku') - F('vycerpana_dovolenka') - F('vycerpane_absencie'),
                    output_field=FloatField()
                )
            ).filter(zostatok__gt=0).values('meno', 'priezvisko', 'zostatok'))
            
            dnes = timezone.now().date()
            absencie_db = Nepritomnost.objects.filter(stav='SCHVALENE', datum_od__lte=dnes, datum_do__gte=dnes).select_related('zamestnanec')
            aktualne_absencie = [f"{a.zamestnanec.meno} {a.zamestnanec.priezvisko} - {a.get_typ_display()} (do {a.datum_do.strftime('%d.%m.%Y')})" for a in absencie_db]

            nabor_uchadzaci = []
            for kand in Kandidat.objects.select_related('pozicia', 'vytvoreny_zamestnanec').filter(stav__in=['NOVY', 'KONTAKTOVANY', 'POHOVOR', 'PRIJATY']):
                nabor_uchadzaci.append({
                    "meno": kand.meno,
                    "pozicia": kand.pozicia.nazov if kand.pozicia else "Všeobecný záujem",
                    "cielova_rola": kand.pozicia.get_cielova_rola_display() if kand.pozicia else "🛠️ Servisný technik",
                    "stav": kand.get_stav_display(),
                    "hodnotenie": kand.hodnotenie or "Nehodnotené",
                    "stav_preklopenia_erp": "✅ Preklopený" if kand.vytvoreny_zamestnanec else ("⏳ Čaká na kartu" if kand.stav == 'PRIJATY' else "V procese"),
                    "datum_pohovoru": kand.datum_pohovoru.strftime('%d.%m.%Y %H:%M') if kand.datum_pohovoru else "Nenaplánovaný"
                })
            
            return json.dumps({
                "audit_typ": "HR Audit (Záväzky, Dovolenky, Absencie a Nábor)",
                "aktualne_absencie_dnes": aktualne_absencie,
                "zamestnanci_so_zavazkami_voci_firme": dlznici,
                "zostatky_nevycerpanych_dovoleniek": dovolenky,
                "uchadzaci_v_nabore": nabor_uchadzaci
            })

        # 🔴 10. MODUL PRE ÚČTOVNÍČKU (Kto nám dlhuje)
        elif typ_analyzy == 'saldo_pohladavky':
            dnes = timezone.now().date()
            # 🛡️ CASH-FLOW AUDIT: Zahrnutie stavu PO_SPLATNOSTI, ako aj expirovaných faktúr v stave ODOSLANA a NACHYSTANA
            q_faktury = Q(stav='PO_SPLATNOSTI') | (Q(stav__in=['NACHYSTANA', 'ODOSLANA']) & Q(datum_splatnosti__lt=dnes))
            if d_od: q_faktury &= Q(datum_splatnosti__gte=d_od)
            if d_do: q_faktury &= Q(datum_splatnosti__lte=d_do)
            
            faktury_po_splatnosti: list[dict[str, Any]] = [
                dict(f) for f in VystavenaFaktura.objects.filter(q_faktury)
                .annotate(uhradene=Coalesce(Sum('uhrady__suma_uhrady'), Decimal('0.00'), output_field=DecimalField(max_digits=12, decimal_places=2)))
                .annotate(zostava_doplatit=ExpressionWrapper(F('celkom_s_dph') - F('uhradene'), output_field=DecimalField(max_digits=12, decimal_places=2)))
                .filter(zostava_doplatit__gt=0)
                .values('cislo_faktury', 'zakaznik__Nazov_spolocnosti', 'celkom_s_dph', 'zostava_doplatit', 'datum_splatnosti')
                .order_by('datum_splatnosti')[:20]
            ]
                                         
            for f in faktury_po_splatnosti: 
                f['celkom_s_dph'] = float(f['celkom_s_dph']) if f['celkom_s_dph'] is not None else 0.0
                f['zostava_doplatit'] = float(f['zostava_doplatit']) if f['zostava_doplatit'] is not None else 0.0
                f['datum_splatnosti'] = f['datum_splatnosti'].strftime('%d.%m.%Y') if f['datum_splatnosti'] else "Neznáme"
            
            return json.dumps({
                "audit_typ": "Saldo - Pohľadávky po splatnosti (Konkrétne nezaplatené faktúry)",
                "nezaplatene_faktury": faktury_po_splatnosti
            })

        # 🟢 11. MODUL PRE ÚČTOVNÍČKU (Komu dlhujeme my)
        elif typ_analyzy == 'saldo_zavazky':
            dnes = timezone.now().date()
            q_faktury = Q(stav_uhrady='NEZAPLATENA', datum_splatnosti__lt=dnes)
            if d_od: q_faktury &= Q(datum_splatnosti__gte=d_od)
            if d_do: q_faktury &= Q(datum_splatnosti__lte=d_do)
            
            nase_dlhy: list[dict[str, Any]] = [
                dict(dlh) for dlh in PrichodziaFaktura.objects.filter(q_faktury)
                .annotate(uhradene=Coalesce(Sum('uhrady__suma_uhrady'), Decimal('0.00'), output_field=DecimalField(max_digits=12, decimal_places=2)))
                .annotate(zostava_doplatit=ExpressionWrapper(F('suma_na_uhradu') - F('uhradene'), output_field=DecimalField(max_digits=12, decimal_places=2)))
                .filter(zostava_doplatit__gt=0)
                .values('id', 'dodavatel__Nazov_spolocnosti', 'suma_na_uhradu', 'zostava_doplatit', 'datum_splatnosti', 'variabilny_symbol')
                .order_by('datum_splatnosti')[:20]
            ]
                             
            for dlh in nase_dlhy: 
                dlh['suma_na_uhradu'] = float(dlh['suma_na_uhradu']) if dlh['suma_na_uhradu'] else 0.0
                dlh['zostava_doplatit'] = float(dlh['zostava_doplatit']) if dlh['zostava_doplatit'] else 0.0
                dlh['datum_splatnosti'] = dlh['datum_splatnosti'].strftime('%d.%m.%Y') if dlh['datum_splatnosti'] else "Neznáme"
            
            return json.dumps({
                "audit_typ": "Saldo - Naše záväzky po splatnosti (Konkrétne faktúry, ktoré musíme uhradiť)",
                "nezaplatene_faktury_dodavatelom": nase_dlhy
            })

        # 📈 12. MODUL PRE OBCHOD (Backlog objednávok) - NOVÉ
        elif typ_analyzy == 'backlog_objednavok':
            q_obj = Q(stav__in=['NOVA', 'SCHVALENA', 'VYBAVENA'])
            if d_od: q_obj &= Q(datum_prijatia__gte=d_od)
            if d_do: q_obj &= Q(datum_prijatia__lte=d_do)
            
            backlog = PrijataObjednavka.objects.filter(q_obj).aggregate(
                pocet=Count('id'),
                hodnota_bez_dph=Coalesce(Sum('celkova_suma_bez_dph'), Decimal('0.00'), output_field=DecimalField(max_digits=12, decimal_places=2))
            )
            
            return json.dumps({
                "audit_typ": "Backlog prijatých objednávok (Čo musíme dodať/vyfakturovať)",
                "pocet_otvorenych_objednavok": backlog['pocet'],
                "hodnota_bez_dph_eur": float(backlog['hodnota_bez_dph'])
            })

        # 🏭 13. MODUL PRE KVALITU A ISO 9001 (Korene problémov a kazovosť)
        elif typ_analyzy == 'analyza_kvality':
            ServisnyZaznam = apps.get_model('Evidencia', 'ServisnyZaznam')
            
            # --- 1. ŠTATISTIKA KOREŇOV PROBLÉMOV A OPRÁVNENOSŤ ---
            q_korene = Q(~Q(koren_problemu='NEHODNOTENE'))
            if d_od: q_korene &= Q(vytvorene__date__gte=d_od)
            if d_do: q_korene &= Q(vytvorene__date__lte=d_do)
            
            statistika_korenov = ServisnyZaznam.objects.filter(q_korene).values('koren_problemu').annotate(pocet=Count('id')).order_by('-pocet')
            korene_choices: Any = ServisnyZaznam._meta.get_field('koren_problemu').choices
            korene_dict = dict(korene_choices or ())
            
            korene_data = []
            celkom_reklamacii = 0
            opravnene_pocet = 0
            neopravnene_pocet = 0
            opravnene_kody = getattr(ServisnyZaznam, 'KORENE_OPRAVNENE', {'CHYBA_TECHNIKA', 'VADNY_DIEL', 'SOFTWARE'})
            neopravnene_kody = getattr(ServisnyZaznam, 'KORENE_NEOPRAVNENE', {'CHYBA_OBSLUHY', 'ZANEDBANA_UDRZBA', 'EXTERNY_VPLYV', 'OPOTREBENIE'})
            
            for s in statistika_korenov:
                celkom_reklamacii += s['pocet']
                if s['koren_problemu'] in opravnene_kody: opravnene_pocet += s['pocet']
                elif s['koren_problemu'] in neopravnene_kody: neopravnene_pocet += s['pocet']
                korene_data.append({'nazov': korene_dict.get(s['koren_problemu'], s['koren_problemu']), 'pocet': s['pocet']})
                
            opravnene_percento = round((opravnene_pocet / celkom_reklamacii) * 100, 1) if celkom_reklamacii > 0 else 0
            neopravnene_percento = round((neopravnene_pocet / celkom_reklamacii) * 100, 1) if celkom_reklamacii > 0 else 0

            # --- 2. HALL OF SHAME (Pochybenia technikov) ---
            q_chyby = Q(koren_problemu='CHYBA_TECHNIKA')
            if d_od: q_chyby &= Q(vytvorene__date__gte=d_od)
            if d_do: q_chyby &= Q(vytvorene__date__lte=d_do)
            
            chybne_zaznamy = ServisnyZaznam.objects.filter(q_chyby).select_related('zariadenie')
            zariadenia_ids = [z.zariadenie_id for z in chybne_zaznamy if z.zariadenie_id]
            historicke_zaznamy = ServisnyZaznam.objects.filter(zariadenie_id__in=zariadenia_ids).prefetch_related('technici').order_by('zariadenie_id', '-vytvorene')
            
            historia_mapa: dict[int, list[Any]] = {}
            for hz in historicke_zaznamy:
                if hz.zariadenie_id is not None:
                    historia_mapa.setdefault(hz.zariadenie_id, []).append(hz)
            
            technik_chyby: dict[int, dict[str, Any]] = {}
            for zaznam in chybne_zaznamy:
                if not zaznam.zariadenie_id: continue
                predosly_zaznam = next((hz for hz in historia_mapa.get(zaznam.zariadenie_id, []) if hz.vytvorene < zaznam.vytvorene), None)
                if predosly_zaznam:
                    for technik in predosly_zaznam.technici.all():
                        tid = technik.id
                        if tid not in technik_chyby: technik_chyby[tid] = {'meno': f"{technik.meno} {technik.priezvisko}", 'pocet': 0}
                        technik_chyby[tid]['pocet'] += 1
            rebricek_technikov = sorted(technik_chyby.values(), key=lambda x: x['pocet'], reverse=True)[:5]

            # --- 3. HALL OF FAME (NPS Skóre) ---
            q_nps = Q(hodnotenie_zakaznika__isnull=False)
            if d_od: q_nps &= Q(vytvorene__date__gte=d_od)
            if d_do: q_nps &= Q(vytvorene__date__lte=d_do)
            
            nps_zaznamy = ServisnyZaznam.objects.filter(q_nps).prefetch_related('technici')
            technik_nps: dict[int, dict[str, Any]] = {}
            for z in nps_zaznamy:
                if z.hodnotenie_zakaznika is None:
                    continue
                for t in z.technici.all():
                    tid = t.id
                    if tid not in technik_nps: technik_nps[tid] = {'meno': f"{t.meno} {t.priezvisko}", 'suma': 0, 'pocet': 0}
                    technik_nps[tid]['suma'] += z.hodnotenie_zakaznika
                    technik_nps[tid]['pocet'] += 1
            nps_list: list[dict[str, Any]] = [{'meno': d['meno'], 'priemer': round(d['suma'] / d['pocet'], 1) if d['pocet'] > 0 else 0.0} for d in technik_nps.values()]
            rebricek_nps = sorted(nps_list, key=lambda x: x['priemer'], reverse=True)[:5]

            # --- 4. NAJKAZOVEJŠIE ZNAČKY A KATEGÓRIE ---
            q_vady = Q(koren_problemu__in=['VADNY_DIEL', 'SOFTWARE'])
            if d_od: q_vady &= Q(vytvorene__date__gte=d_od)
            if d_do: q_vady &= Q(vytvorene__date__lte=d_do)
            
            kazove_znacky = list(ServisnyZaznam.objects.filter(q_vady, zariadenie__vyrobca__isnull=False)
                                 .values(nazov=F('zariadenie__vyrobca__nazov_vyrobcu'))
                                 .annotate(pocet=Count('id')).order_by('-pocet')[:5])
                                 
            kategorie_choices: Any = ServisovaneZariadenie._meta.get_field('kategoria').choices
            kategorie_dict = dict(kategorie_choices or ())
            kazove_kategorie_db = ServisnyZaznam.objects.filter(q_vady, zariadenie__kategoria__isnull=False).exclude(zariadenie__kategoria='').values('zariadenie__kategoria').annotate(pocet=Count('id')).order_by('-pocet')[:5]
            kazove_kategorie = [{'nazov': kategorie_dict.get(k['zariadenie__kategoria'], k['zariadenie__kategoria']), 'pocet': k['pocet']} for k in kazove_kategorie_db]

            # --- 5. CHRONICKÉ PORUCHY (Opakované reklamácie) ---
            q_ulohy = Q(je_reklamacia=True)
            if d_od: q_ulohy &= Q(zaciatok__date__gte=d_od)
            if d_do: q_ulohy &= Q(zaciatok__date__lte=d_do)
            
            vsetky_reklamacie = Uloha.objects.filter(q_ulohy).prefetch_related('zariadenia')
            mapa_poctov: dict[int, int] = {}
            for uloha in vsetky_reklamacie:
                for zar in uloha.zariadenia.all():
                    mapa_poctov[zar.id] = mapa_poctov.get(zar.id, 0) + 1
                    
            chronicke_ids = [zid for zid, pocet in mapa_poctov.items() if pocet > 1]
            chronicke_stroje: list[dict[str, Any]] = []
            if chronicke_ids:
                stroje_db = ServisovaneZariadenie.objects.filter(id__in=chronicke_ids).select_related('zakaznik', 'vyrobca')
                for stroj in stroje_db:
                    chronicke_stroje.append({
                        'zakaznik': stroj.zakaznik.Nazov_spolocnosti if stroj.zakaznik else "Neznámy",
                        'zariadenie': stroj.model_zariadenia or stroj.nazov,
                        'sn': stroj.vyrobne_cislo,
                        'pocet_reklamacii': mapa_poctov[stroj.id]
                    })
                chronicke_stroje.sort(key=lambda x: x['pocet_reklamacii'], reverse=True)
                             
            return json.dumps({
                "audit_typ": "Analýza kvality a reklamácií (ISO 9001)",
                "pomer_opravnenosti": {
                    "opravnene_nase_naklady_percento": opravnene_percento,
                    "neopravnene_fakturujeme_percento": neopravnene_percento
                },
                "najcastejsie_priciny_reklamacii": korene_data,
                "rebricek_technikov_pochybenia": rebricek_technikov,
                "rebricek_technikov_najlepsie_nps": rebricek_nps,
                "najkazovejsie_znacky_vyrobcovia": kazove_znacky,
                "najkazovejsie_kategorie_strojov": kazove_kategorie,
                "chronicky_kazove_stroje": chronicke_stroje
            })

        # 💼 14. MODUL PRE OBCHOD (Otvorená Pipeline)
        elif typ_analyzy == 'obchodna_pipeline':
            q_ponuky = Q(stav='SENT')
            if d_od: q_ponuky &= Q(datum_vystavenia__gte=d_od)
            if d_do: q_ponuky &= Q(datum_vystavenia__lte=d_do)
            
            pipeline_agg = CenovaPonuka.objects.filter(q_ponuky).aggregate(
                hodnota_spolu=Coalesce(Sum('celkom_bez_dph'), Decimal('0.00'), output_field=DecimalField(max_digits=12, decimal_places=2)),
                pocet=Count('id')
            )
            
            top_ponuky: list[dict[str, Any]] = [
                dict(ponuka_row) for ponuka_row in CenovaPonuka.objects.filter(q_ponuky)
                .values('cislo_ponuky', 'zakaznik__Nazov_spolocnosti', 'celkom_bez_dph')
                .order_by('-celkom_bez_dph')[:10]
            ]
                              
            for ponuka_row in top_ponuky: ponuka_row['celkom_bez_dph'] = float(ponuka_row['celkom_bez_dph']) if ponuka_row['celkom_bez_dph'] is not None else 0.0
            
            return json.dumps({
                "audit_typ": "Otvorená obchodná Pipeline (Čaká na schválenie klientom)",
                "celkova_hodnota_bez_dph_eur": float(pipeline_agg['hodnota_spolu']),
                "pocet_otvorenych_ponuk": pipeline_agg['pocet'],
                "top_10_najvacsich_ponuk": top_ponuky
            })

        # ⏰ 15. MODUL PRE HR (Expirácie certifikátov a prehliadok)
        elif typ_analyzy == 'hr_expiracie':
            dnes = timezone.now().date()
            o_30_dni = add_calendar_interval(dnes, months=1) or (dnes + timedelta(days=30))
            
            zamestnanci = Zamestnanec.objects.filter(je_anonymizovany=False)
            bozp = list(zamestnanci.filter(bozp_platne_do__lte=o_30_dni).values('meno', 'priezvisko', 'bozp_platne_do'))

            Certifikat = apps.get_model('Evidencia', 'Certifikat')
            konciace_certifikaty = list(
                Certifikat.objects.filter(
                    zamestnanec__je_anonymizovany=False,
                    je_dozivotny=False,
                    datum_platnosti__lte=o_30_dni
                ).select_related('zamestnanec').order_by('datum_platnosti')
            )

            return json.dumps({
                "audit_typ": "HR Expirácie (BOZP a odborné certifikácie do 30 dní alebo prepadnuté)",
                "konciace_bozp": [{"meno": f"{z['meno']} {z['priezvisko']}", "plati_do": z['bozp_platne_do'].strftime('%d.%m.%Y') if z['bozp_platne_do'] else "Nezadané"} for z in bozp],
                "konciace_certifikaty": [{
                    "meno": f"{c.zamestnanec.meno} {c.zamestnanec.priezvisko}",
                    "certifikat": c.nazov,
                    "kategoria": c.get_kategoria_display(),
                    "plati_do": c.datum_platnosti.strftime('%d.%m.%Y') if c.datum_platnosti else "Nezadané",
                    "dni_do_expiracie": (c.datum_platnosti - dnes).days if c.datum_platnosti else None
                } for c in konciace_certifikaty]
            })

        # 🚗 16. MODUL PRE FLEET (Expirácie STK, PZP, KASKO, Diaľničná, Lekárnička, Olej a Servis <= 1000 km)
        elif typ_analyzy == 'fleet_expiracie':
            dnes = timezone.now().date()
            o_30_dni = add_calendar_interval(dnes, months=1) or (dnes + timedelta(days=30))
            
            stk = list(Vozidlo.objects.filter(stk_plati_do__lte=o_30_dni).values('spz', 'znacka_a_model', 'stk_plati_do'))
            pzp = list(Vozidlo.objects.filter(pzp_plati_do__lte=o_30_dni).values('spz', 'znacka_a_model', 'pzp_plati_do'))
            kasko = list(Vozidlo.objects.filter(kasko_plati_do__lte=o_30_dni).values('spz', 'znacka_a_model', 'kasko_plati_do'))
            dialnicna = list(Vozidlo.objects.filter(dialnicna_znamka_do__lte=o_30_dni).values('spz', 'znacka_a_model', 'dialnicna_znamka_do'))
            lekarnicka = list(Vozidlo.objects.filter(lekarnicka_plati_do__lte=o_30_dni).values('spz', 'znacka_a_model', 'lekarnicka_plati_do'))
            olej_servis = list(
                Vozidlo.objects.filter(
                    olej_interval_km__gt=0,
                    aktualny_stav_km__gte=F('posledny_olej_km') + F('olej_interval_km') - 1000
                ).values('spz', 'znacka_a_model', 'aktualny_stav_km', 'posledny_olej_km', 'olej_interval_km')
            )
            velky_servis = list(
                Vozidlo.objects.filter(
                    servis_interval_km__gt=0,
                    aktualny_stav_km__gte=F('posledny_servis_km') + F('servis_interval_km') - 1000
                ).values('spz', 'znacka_a_model', 'aktualny_stav_km', 'posledny_servis_km', 'servis_interval_km')
            )
            
            return json.dumps({
                "audit_typ": "Fleet Expirácie (STK, PZP, KASKO, Diaľničná, Lekárnička do 30 dní alebo prepadnuté; Olej a servis do 1000 km)",
                "konciaca_stk": [{"vozidlo": f"{v['znacka_a_model']} ({v['spz']})", "plati_do": v['stk_plati_do'].strftime('%d.%m.%Y') if v['stk_plati_do'] else "Nezadané"} for v in stk],
                "konciace_pzp": [{"vozidlo": f"{v['znacka_a_model']} ({v['spz']})", "plati_do": v['pzp_plati_do'].strftime('%d.%m.%Y') if v['pzp_plati_do'] else "Nezadané"} for v in pzp],
                "konciace_kasko": [{"vozidlo": f"{v['znacka_a_model']} ({v['spz']})", "plati_do": v['kasko_plati_do'].strftime('%d.%m.%Y') if v['kasko_plati_do'] else "Nezadané"} for v in kasko],
                "konciaca_dialnicna": [{"vozidlo": f"{v['znacka_a_model']} ({v['spz']})", "plati_do": v['dialnicna_znamka_do'].strftime('%d.%m.%Y') if v['dialnicna_znamka_do'] else "Nezadané"} for v in dialnicna],
                "konciaca_lekarnicka": [{"vozidlo": f"{v['znacka_a_model']} ({v['spz']})", "plati_do": v['lekarnicka_plati_do'].strftime('%d.%m.%Y') if v['lekarnicka_plati_do'] else "Nezadané"} for v in lekarnicka],
                "konciaci_olej_km": [
                    {
                        "vozidlo": f"{v['znacka_a_model']} ({v['spz']})",
                        "aktualne_km": v['aktualny_stav_km'],
                        "zostava_km": (v['posledny_olej_km'] + v['olej_interval_km']) - v['aktualny_stav_km']
                    } for v in olej_servis
                ],
                "konciaci_velky_servis_km": [
                    {
                        "vozidlo": f"{v['znacka_a_model']} ({v['spz']})",
                        "aktualne_km": v['aktualny_stav_km'],
                        "zostava_km": (v['posledny_servis_km'] + v['servis_interval_km']) - v['aktualny_stav_km']
                    } for v in velky_servis
                ],
            })


        # 🔧 17. MODUL PRE DISPEČING (Plánované servisy a Zákonné revízie VTZ / F-plyny)
        elif typ_analyzy == 'planovane_servisy':
            dnes = timezone.now().date()
            iso_kontext = None
            clean_ident = str(identifikator).strip() if identifikator is not None else ""
            if clean_ident and clean_ident.upper() not in ['ALL', 'NONE', '']:
                target_zak = Zakaznik.objects.filter(je_anonymizovany=False, Nazov_spolocnosti__icontains=clean_ident).first()
                if target_zak:
                    iso_kontext = get_iso_pre_objekt(target_zak)
            cfg = get_compliance_config(iso=iso_kontext)
            # 🛡️ ISAS DYNAMIC: Predstih dní a MH je 100% riadený z GlobalneNastavenia (žiadny hardkod)
            predstih_dni = int(cfg.get('predstih_dni') or 14)
            predstih_mh = int(cfg.get('predstih_motohodiny') or 100)
            limit_datum = dnes + timedelta(days=predstih_dni)
            
            # 1. Bežné plánované servisy (S ochranou servisného režimu a defenzívnym vyhodnotením polí)
            podmienky_servis = (
                Q(planovany_datum__isnull=False, planovany_datum__lte=limit_datum) |
                Q(planovane_motohodiny__isnull=False, planovane_motohodiny__lte=F('zariadenie__aktualne_motohodiny') + predstih_mh)
            )
            if hasattr(PlanovanyServisZariadenia, 'planovane_motohodiny_zataz'):
                podmienky_servis |= Q(planovane_motohodiny_zataz__isnull=False, planovane_motohodiny_zataz__lte=F('zariadenie__motohodiny_v_zatazi') + predstih_mh)

            plany = PlanovanyServisZariadenia.objects.filter(
                Q(upozornenie_odoslane=False) &
                Q(zariadenie__v_servisnom_rezime=False) &
                podmienky_servis
            ).select_related('zariadenie__zakaznik', 'typ_servisu').order_by('planovany_datum')[offset:offset+limit]
            
            zoznam = []
            for plan in plany:
                zoznam.append({
                    "zakaznik": plan.zariadenie.zakaznik.Nazov_spolocnosti if plan.zariadenie.zakaznik else "Neznámy",
                    "zariadenie": f"{plan.zariadenie.model_zariadenia} (SN: {plan.zariadenie.vyrobne_cislo})",
                    "typ_servisu": plan.typ_servisu.nazov if plan.typ_servisu else "Všeobecný servis",
                    "planovany_datum": plan.planovany_datum.strftime('%d.%m.%Y') if plan.planovany_datum else "Nezadaný",
                    "planovane_motohodiny": plan.planovane_motohodiny,
                    "aktualne_motohodiny": plan.zariadenie.aktualne_motohodiny,
                    "planovane_motohodiny_zataz": getattr(plan, 'planovane_motohodiny_zataz', None),
                    "aktualne_motohodiny_zataz": getattr(plan.zariadenie, 'motohodiny_v_zatazi', 0)
                })

            # 2. ⚖️ Zákonné revízie VTZ a F-plyny cez centrálnu funkciu Compliance Engine (Dynamický predstih)
            zakonne_lehoty = []
            queue_items = get_global_compliance_queue(referencny_datum=dnes, limit_dni_alert=predstih_dni, filter_alerts_only=True)

            for rev_item in queue_items:
                zar = rev_item.get('zariadenie')
                zak = getattr(zar, 'zakaznik', None) if zar else None
                zak_meno = getattr(zak, 'Nazov_spolocnosti', 'Neznámy') if zak else "Neznámy"
                stroj_nazov = (getattr(zar, 'model_zariadenia', None) or getattr(zar, 'nazov', None) or "Neznáme zariadenie") if zar else "Neznáme zariadenie"
                stroj_sn = getattr(zar, 'vyrobne_cislo', 'Nezadané') if zar else "Nezadané"
                stroj_popis = f"{stroj_nazov} ({rev_item.get('stroj_kategoria', 'zariadenie')}, SN: {stroj_sn})"
                dni = rev_item.get('dni_do', 0)

                if rev_item.get('je_chybajuca'):
                    stav_text = rev_item.get('stav_str')
                elif dni >= 0:
                    stav_text = f"Expiruje o {dni} dní ({rev_item.get('termin_str')})"
                else:
                    stav_text = f"PREPADNUTÁ o {abs(dni)} dní! ({rev_item.get('termin_str')})"

                polozka_data = {
                    "zakaznik": zak_meno,
                    "stroj": stroj_popis,
                    "typ_lehoty": rev_item.get('typ_lehoty'),
                    "stav": stav_text,
                    "dni_do": dni
                }

                # 🛡️ ISAS ARCHITECT: Neutrálny dynamický prenos všetkých odvetvových vlastností (Zero Hardcode + Zero Data Loss)
                ignorovane_interne_kluce = {'zariadenie', 'disclaimer', 'vyzaduje_alert', 'stav_str', 'termin_str'}
                for kluc, hodnota in rev_item.items():
                    if kluc not in polozka_data and kluc not in ignorovane_interne_kluce:
                        polozka_data[kluc] = hodnota

                zakonne_lehoty.append(polozka_data)

            # 🚀 ISAS SENIOR GUARD: Plné stránkovanie naprieč celým rozsahom revízií cez offset a limit
            celkovy_pocet_revizii = len(zakonne_lehoty)
            strankovane_lehoty = zakonne_lehoty[offset:offset+limit]

            stav_leg_sprava: list[dict[str, Any]] | str
            if not cfg.get('sledovat_legislativu', True):
                stav_leg_sprava = "ℹ️ Sledovanie zákonných revízií a auditov je v globálnych nastaveniach vypnuté."
            else:
                stav_leg_sprava = strankovane_lehoty if strankovane_lehoty else (
                    f"Všetky zákonné revízie sú platné a bez expirácií v horizonte {predstih_dni} dní." if offset == 0 else "Žiadne ďalšie revízie na tejto strane."
                )

            return json.dumps({
                "audit_typ": f"Plánované servisy a Zákonné revízie (Predstih {predstih_dni} dní)",
                "pocet_kritickych_servisov_celkom": PlanovanyServisZariadenia.objects.filter(Q(upozornenie_odoslane=False) & Q(zariadenie__v_servisnom_rezime=False) & podmienky_servis).count(),
                "pocet_zakonnych_revizii_celkom": celkovy_pocet_revizii,
                "offset": offset,
                "limit": limit,
                "zoznam_servisov": zoznam,
                "zakonne_revizie": stav_leg_sprava
            }, cls=DjangoJSONEncoder)

        # 🕵️ 18. MODUL PRE BEZPEČNOSŤ (Systémový Audit Log)
        elif typ_analyzy == 'systemovy_audit':
            ISASAuditLog = apps.get_model('ISASaplikacia', 'ISASAuditLog')
            q_log = Q()
            if d_od: q_log &= Q(cas_vykonania__date__gte=d_od)
            if d_do: q_log &= Q(cas_vykonania__date__lte=d_do)
            if identifikator and str(identifikator).strip().upper() not in ['ALL', 'NONE', '']:
                cisty_id = str(identifikator).strip()
                q_log &= (Q(object_id=cisty_id) | Q(content_type__model__icontains=cisty_id))

            logy = ISASAuditLog.objects.filter(q_log).select_related('vykonal', 'content_type').order_by('-cas_vykonania')[offset:offset+limit]
            zoznam = []
            for l in logy:
                try:
                    zmeny_dict = json.loads(l.isas_metadata_json_zmeny) if l.isas_metadata_json_zmeny else {}
                except Exception:
                    zmeny_dict = l.isas_metadata_json_zmeny or "Bez detailov"

                zoznam.append({
                    "cas": l.cas_vykonania.strftime('%d.%m.%Y %H:%M') if l.cas_vykonania else "Neznámy čas",
                    "vykonal": l.vykonal.get_full_name() if l.vykonal else "Systém / AI",
                    "akcia": l.get_akcia_display(),
                    "model": l.content_type.model if l.content_type else "Neznámy model",
                    "zaznam_id": l.object_id,
                    "zmeny": zmeny_dict,
                    "pocet_zmien": l.pocet_zmien
                })
                
            return json.dumps({
                "audit_typ": "Systémový Audit Log (Kto čo zmenil/zmazal)",
                "posledne_zmeny": zoznam
            })

        # 🎂 19. MODUL PRE CRM INTEL (Dnešné narodeniny a meniny)
        elif typ_analyzy == 'crm_udalosti':
            dnes = timezone.now().date()
            je_nepriestupny_posledny_februar = (dnes.month == 2 and dnes.day == 28 and not calendar.isleap(dnes.year))
            q_datum_narodenia = Q(datum_narodenia__day=dnes.day, datum_narodenia__month=dnes.month)
            if je_nepriestupny_posledny_februar:
                q_datum_narodenia |= Q(datum_narodenia__day=29, datum_narodenia__month=2)

            # Získame narodeniny (s rešpektovaním GDPR anonymizácie a vynechaním vlastnej firmy)
            narodeniny = []
            
            # 1. Zákazníci
            for zak in Zakaznik.objects.filter(q_datum_narodenia, je_anonymizovany=False, je_interny_profil=False):
                if zak.datum_narodenia:
                    vek = dnes.year - zak.datum_narodenia.year
                    meno = zak.kontaktna_osoba or zak.Nazov_spolocnosti
                    narodeniny.append({"typ": "Zákazník", "osoba": str(meno).strip(), "firma": zak.Nazov_spolocnosti, "vek": vek})
                    
            # 2. Dodávatelia
            for dod in Dodavatel.objects.filter(q_datum_narodenia, je_anonymizovany=False):
                if dod.datum_narodenia:
                    vek = dnes.year - dod.datum_narodenia.year
                    meno = dod.kontaktna_osoba or dod.Nazov_spolocnosti
                    narodeniny.append({"typ": "Dodávateľ", "osoba": str(meno).strip(), "firma": dod.Nazov_spolocnosti, "vek": vek})
                    
            # 3. Zamestnanci
            for zam in Zamestnanec.objects.filter(q_datum_narodenia, je_anonymizovany=False):
                if zam.datum_narodenia:
                    vek = dnes.year - zam.datum_narodenia.year
                    narodeniny.append({"typ": "Zamestnanec", "osoba": f"{zam.meno} {zam.priezvisko}".strip(), "firma": "", "vek": vek})
                    
            # 4. Súkromné osoby
            for suk in SukromnaOsoba.objects.filter(q_datum_narodenia, je_anonymizovany=False):
                if suk.datum_narodenia:
                    vek = dnes.year - suk.datum_narodenia.year
                    narodeniny.append({"typ": "Súkromná osoba", "osoba": suk.meno_a_priezvisko, "firma": "", "vek": vek})

            # Získame meniny (SK a CZ z offline MENINY_KALENDAR)
            def _norm_meno(val: str) -> str:
                nfkd = unicodedata.normalize('NFKD', str(val or ''))
                return "".join(c for c in nfkd if not unicodedata.combining(c) and c.isalnum()).lower()

            try:
                from ISASaplikacia.meniny_db import MENINY_KALENDAR
            except ImportError:
                MENINY_KALENDAR = {}

            den_mesiac = dnes.strftime('%d.%m.')
            dnesne_meniny_raw = MENINY_KALENDAR.get(den_mesiac, {"sk": "", "cz": ""})
            dnesne_meniny = dict(dnesne_meniny_raw) if isinstance(dnesne_meniny_raw, dict) else {"sk": "", "cz": ""}
            if je_nepriestupny_posledny_februar:
                meniny_29 = MENINY_KALENDAR.get("29.02.", {})
                if isinstance(meniny_29, dict):
                    for lang_key, meno_29 in meniny_29.items():
                        if meno_29:
                            povodna = dnesne_meniny.get(lang_key, "")
                            dnesne_meniny[lang_key] = f"{povodna}, {meno_29}".strip(", ") if povodna else meno_29

            sviatky_blacklist = {
                "nový rok", "sviatok práce", "svátek práce", "státní svátek",
                "tři králové", "upálení mistra jana husa", "pamiatka zosnulých",
                "1. sviatok vianočný", "boží hod vánoční"
            }

            def _parsuj_mena(text_meniny: str) -> set[str]:
                vysledok = set()
                for cast in str(text_meniny or "").split(","):
                    polozka = cast.strip()
                    if polozka and polozka.lower() not in sviatky_blacklist:
                        for sub in polozka.split(" a "):
                            norm_sub = _norm_meno(sub)
                            if norm_sub:
                                vysledok.add(norm_sub)
                return vysledok

            sk_mena = _parsuj_mena(dnesne_meniny.get("sk", ""))
            cz_mena = _parsuj_mena(dnesne_meniny.get("cz", ""))
            meniny_zhody = []

            def _skontroluj_meniny(celne_meno: Optional[str], typ_subjektu: str, firma_nazov: str = "") -> None:
                if not celne_meno:
                    return
                tokeny = [_norm_meno(t) for t in str(celne_meno).replace(',', ' ').split() if _norm_meno(t)]
                trhy = []
                if any(t in sk_mena for t in tokeny):
                    trhy.append("SK")
                if any(t in cz_mena for t in tokeny):
                    trhy.append("CZ")
                if trhy:
                    meniny_zhody.append({
                        "typ": typ_subjektu,
                        "osoba": str(celne_meno).strip(),
                        "firma": firma_nazov,
                        "trhy": trhy,
                    })

            for zak in Zakaznik.objects.filter(je_anonymizovany=False, je_interny_profil=False).exclude(kontaktna_osoba__isnull=True).exclude(kontaktna_osoba='').only('kontaktna_osoba', 'Nazov_spolocnosti'):
                _skontroluj_meniny(zak.kontaktna_osoba, "Zákazník", zak.Nazov_spolocnosti)
            for dod in Dodavatel.objects.filter(je_anonymizovany=False).exclude(kontaktna_osoba__isnull=True).exclude(kontaktna_osoba='').only('kontaktna_osoba', 'Nazov_spolocnosti'):
                _skontroluj_meniny(dod.kontaktna_osoba, "Dodávateľ", dod.Nazov_spolocnosti)
            for zam in Zamestnanec.objects.filter(je_anonymizovany=False).only('meno', 'priezvisko'):
                _skontroluj_meniny(f"{zam.meno} {zam.priezvisko}".strip(), "Zamestnanec")
            for suk in SukromnaOsoba.objects.filter(je_anonymizovany=False).only('meno_a_priezvisko'):
                _skontroluj_meniny(suk.meno_a_priezvisko, "Súkromná osoba")

            return json.dumps({
                "audit_typ": "Dnešné CRM udalosti (Narodeniny a Meniny)",
                "dnesny_datum": dnes.strftime('%d.%m.%Y'),
                "dnesne_meniny_kalendar": {"sk": dnesne_meniny.get("sk", ""), "cz": dnesne_meniny.get("cz", "")},
                "narodeniny_dnes": narodeniny if narodeniny else "Dnes nemá nikto narodeniny.",
                "meniny_dnes": meniny_zhody if meniny_zhody else "Dnes nemá nikto zo známych kontaktov meniny."
            })

        # 📦 20. MODUL PRE NASKLADŇOVANIE (Vyťaženo AI, čaká na fyzický príjem)
        elif typ_analyzy == 'nenaskladneny_tovar':
            LogNaskladnenia = apps.get_model('Evidencia', 'LogNaskladnenia')
            q_log = Q(uspesne=False) & ~Q(navrhovana_kategoria='Sluzba')
            if d_od: q_log &= Q(faktura__datum_prijatia__date__gte=d_od)
            if d_do: q_log &= Q(faktura__datum_prijatia__date__lte=d_do)
            if identifikator and identifikator != 'ALL':
                q_log &= (Q(najdeny_kod_faktura__icontains=identifikator) | Q(navrhovany_nazov__icontains=identifikator) | Q(faktura__dodavatel__Nazov_spolocnosti__icontains=identifikator))
            
            cakajuce = LogNaskladnenia.objects.filter(q_log).select_related('faktura__dodavatel', 'faktura__cielovy_sklad', 'filialka', 'navrhovany_vyrobca')[offset:offset+limit]
            zoznam = []
            for log_item in cakajuce:
                zoznam.append({
                    "faktura_id": log_item.faktura.id if log_item.faktura else None,
                    "dodavatel": log_item.faktura.dodavatel.Nazov_spolocnosti if (log_item.faktura and log_item.faktura.dodavatel) else "Neznámy",
                    "kod_faktura": log_item.najdeny_kod_faktura,
                    "katalogove_cislo_sku": log_item.navrhovane_katalogove_cislo or "Neuvedené",
                    "ean_kod": log_item.navrhovany_ean or "Neuvedený",
                    "nazov": log_item.navrhovany_nazov or log_item.najdeny_kod_faktura,
                    "kategoria": log_item.navrhovana_kategoria or "Nezadaná",
                    "navrhovany_vyrobca": log_item.navrhovany_vyrobca.nazov_vyrobcu if log_item.navrhovany_vyrobca else "Neznámy",
                    "navrhovany_typ_on": log_item.get_navrhovany_typ_display() if log_item.navrhovany_typ else "Neznámy",
                    "mnozstvo": f"{log_item.mnozstvo} {log_item.navrhovana_merna_jednotka or 'ks'}",
                    "cena_za_ks_eur": float(log_item.nakupna_cena) if log_item.nakupna_cena else 0.0,
                    "sadzba_dph_percento": float(log_item.navrhovana_sadzba_dph) if log_item.navrhovana_sadzba_dph is not None else float(apps.get_model('ISASaplikacia', 'GlobalneNastavenia').get_zakladna_dph()),
                    "cieľovy_sklad": log_item.filialka.nazov if log_item.filialka else (log_item.faktura.cielovy_sklad.nazov if (log_item.faktura and log_item.faktura.cielovy_sklad) else nazov_hlavneho),
                    "datum_prijatia_faktury": log_item.faktura.datum_prijatia.strftime('%d.%m.%Y') if (log_item.faktura and log_item.faktura.datum_prijatia) else "Neznáme"
                })
            return json.dumps({
                "audit_typ": "Položky vyťažené z faktúr čakajúce na naskladnenie v dashboarde",
                "celkovy_pocet_cakajucich": LogNaskladnenia.objects.filter(q_log).count(),
                "zoznam_poloziek": zoznam
            })

        # 🤖 21. MODUL PRE MAJITEĽA (AI Audit Log - História konverzácií)
        elif typ_analyzy == 'ai_audit':
            AIAuditLog = apps.get_model('Evidencia', 'AIAuditLog')
            q_ai = Q()
            if d_od: q_ai &= Q(cas_dopytu__date__gte=d_od)
            if d_do: q_ai &= Q(cas_dopytu__date__lte=d_do)
            if identifikator and str(identifikator).strip().upper() not in ['ALL', 'NONE', '']:
                cisty_id = str(identifikator).strip()
                q_ai &= (
                    Q(pouzity_model__icontains=cisty_id) |
                    Q(pouzivatel__first_name__icontains=cisty_id) |
                    Q(pouzivatel__last_name__icontains=cisty_id) |
                    Q(sukromna_osoba__meno_a_priezvisko__icontains=cisty_id) |
                    Q(otazka__icontains=cisty_id) |
                    Q(celery_task_id__icontains=cisty_id)
                )
            
            ai_logy = AIAuditLog.objects.filter(q_ai).select_related('pouzivatel', 'sukromna_osoba').order_by('-cas_dopytu')[offset:offset+limit]
            zoznam = []
            for ai_l in ai_logy:
                osoba = ai_l.pouzivatel.get_full_name() if ai_l.pouzivatel else (ai_l.sukromna_osoba.meno_a_priezvisko if ai_l.sukromna_osoba else "Neznámy")
                zoznam.append({
                    "cas": ai_l.cas_dopytu.strftime('%d.%m.%Y %H:%M'),
                    "osoba": osoba,
                    "zdroj": ai_l.get_zdroj_poziadavky_display(),
                    "pouzity_model": getattr(ai_l, 'pouzity_model', None) or "gemini-4-hydrogen",
                    "otazka": ai_l.otazka or "Bez textu",
                    "odpoved": ai_l.odpoved or "Bez odpovede",
                    "celery_task_id": ai_l.celery_task_id or "Žiadne",
                    "cena_usd": float(ai_l.presna_cena_usd or 0.0)
                })
            return json.dumps({
                "audit_typ": "AI Audit Log (História konverzácií s AI)",
                "posledne_konverzacie": zoznam
            })

        # 🧹 22. MODUL PRE ADMINISTRATÍVU (Data Quality Enforcer / CRM Audit)
        elif typ_analyzy == 'crm_audit':
            zakaznici = list(Zakaznik.objects.filter(je_anonymizovany=False, je_interny_profil=False).values('id', 'Nazov_spolocnosti', 'ico', 'email', 'telefon', 'mesto')[:100])

            dodavatelia = list(Dodavatel.objects.filter(je_anonymizovany=False).values('id', 'Nazov_spolocnosti', 'ico', 'email', 'telefon', 'mesto')[:100])
            sukromne_osoby = list(SukromnaOsoba.objects.filter(je_anonymizovany=False).values('id', 'meno_a_priezvisko', 'email', 'telefon', 'mesto')[:100])
            
            return json.dumps({
                "audit_typ": "CRM Data Quality Audit (Hľadanie duplicít a anomálií)",
                "instrukcia_pre_ai": "Analyzuj tieto dáta. Hľadaj subjekty s rovnakým IČO, duplicitné kontakty, podobné názvy alebo anomálie v telefónnych číslach a e-mailoch. Vypíš prehľadný zoznam.",
                "vzorka_zakaznikov": zakaznici,
                "vzorka_dodavatelov": dodavatelia,
                "vzorka_sukromnych_osob": sukromne_osoby
            })

        # 🎮 23. MODUL PRE HR & TÍM (Kompletný rebríček hry ISAS 2048, Sezóny a Denné výzvy)
        elif typ_analyzy == 'leaderboard_2048':
            ISASHraLeaderboard = apps.get_model('ISASaplikacia', 'ISASHraLeaderboard')

            dnesny_den_kod = timezone.now().strftime("%Y-%m-%d")
            aktualny_kvartal_kod, kvartal_nazov = ISASHraLeaderboard.get_aktualny_kvartal_info()
            denna_bounty = ISASHraLeaderboard.get_denna_bounty(dnesny_den_kod)

            clean_id = str(identifikator).strip() if identifikator is not None else ""
            qs = ISASHraLeaderboard.objects.select_related('user')

            if clean_id and clean_id.upper() not in ['ALL', 'NONE', '']:
                q_user = Q(user__username__icontains=clean_id) | Q(user__first_name__icontains=clean_id) | Q(user__last_name__icontains=clean_id)
                casti_mena = clean_id.split()
                if len(casti_mena) >= 2:
                    q_user |= (Q(user__first_name__icontains=casti_mena[0]) & Q(user__last_name__icontains=casti_mena[-1]))
                qs = qs.filter(q_user)

            celkovy_pocet = qs.count()
            zaznamy = qs.order_by('-max_skore', '-dosiahnute_dna')[offset:offset+limit]

            rebricek_data = []
            for r in zaznamy:
                rebricek_data.append({
                    "hrac": r.user.get_full_name() or r.user.username,
                    "user_id": r.user_id,
                    "max_skore": r.max_skore,
                    "max_dlazdica": r.max_dlazdica,
                    "nazov_max_dlazdice": r.get_nazov_dlazdice(),
                    "dosiahnute_dna": r.dosiahnute_dna.strftime('%d.%m.%Y %H:%M') if r.dosiahnute_dna else "Neznáme",
                    "sezona_kod": r.sezona_kod,
                    "sezona_skore": r.sezona_skore,
                    "sezona_dlazdica": r.sezona_dlazdica,
                    "sezona_dosiahnute_dna": r.sezona_dosiahnute_dna.strftime('%d.%m.%Y %H:%M') if r.sezona_dosiahnute_dna else "Neznáme",
                    "den_kod": r.den_kod,
                    "den_skore": r.den_skore,
                    "den_dlazdica": r.den_dlazdica,
                    "den_dosiahnute_dna": r.den_dosiahnute_dna.strftime('%d.%m.%Y %H:%M') if r.den_dosiahnute_dna else "Neznáme",
                    "den_bounty_splnena": r.den_bounty_splnena
                })

            return json.dumps({
                "audit_typ": "Firemný rebríček a turnaj ISAS 2048",
                "aktualna_sezona": {
                    "kod": aktualny_kvartal_kod,
                    "nazov": kvartal_nazov
                },
                "dnesna_smenova_bounty": denna_bounty,
                "celkovy_pocet_hracov": celkovy_pocet,
                "offset": offset,
                "limit": limit,
                "rebricek": rebricek_data
            }, cls=DjangoJSONEncoder)
            
        else:
            return json.dumps({"error": f"Neznámy typ analýzy: '{typ_analyzy}'. Skontroluj povolené kľúče v popise nástroja."})
            
    except ValueError:
        return json.dumps({"error": f"Neplatný formát dátumu. Musíš použiť striktne formát YYYY-MM-DD (napr. {date.today().year}-05-01). Oprav to a zavolaj nástroj znova."})
    except Exception as e:
        return json.dumps({"error": f"Databázová chyba: {str(e)}"})

def tool_precitaj_dokument(model_name: str, object_id: int, nazov_pola: str) -> str:
    """
    Použi na fyzické otvorenie a prečítanie textu z PDF dokumentu alebo obrázka (skenu).
    """    
    try:
        # 🛡️ TYPE SAFE GUARD: Pretypovanie ID na int
        try:
            safe_obj_id = int(object_id)
        except (ValueError, TypeError):
            return json.dumps({"error": f"Neplatné ID objektu: '{object_id}'. Musí to byť celé číslo."})

        # 🔍 MULTI-APP INTROSPEKCIA: Prehľadanie všetkých aplikácií projektu vrátane aktívneho odvetvia
        Model = None
        industry_mod = (getattr(settings, 'INDUSTRY_MODULE', None) or '').strip().lower()
        search_apps = ['Evidencia', 'ISASaplikacia', 'WebBuilder', 'auth']
        if industry_mod:
            search_apps.extend([industry_mod, f"odvetvia.{industry_mod}"])
        for app_label in search_apps:
            try:
                Model = apps.get_model(app_label, model_name)
                if Model: break
            except LookupError:
                continue

        if not Model:
            vsetky_odvetvove_modely = list(ZARIADENIA_MAP.values()) + list(PARENT_MODELS.values()) + [m for subcats in CATEGORY_MAP.values() for m in subcats.values()]
            for m in vsetky_odvetvove_modely:
                if hasattr(m, '_meta') and m._meta.object_name.lower() == str(model_name).lower():
                    Model = m
                    break

        if not Model:
            return json.dumps({"error": f"Model '{model_name}' sa nenašiel v žiadnej systémovej aplikácii ani v registroch aktívneho odvetvia."})
                
        try:
            obj = Model.objects.get(pk=safe_obj_id)
        except (Model.DoesNotExist, ObjectDoesNotExist):
            return json.dumps({"error": f"Záznam s ID {safe_obj_id} v modeli {model_name} neexistuje."})
        
        if not hasattr(obj, nazov_pola):
            return json.dumps({"error": f"Pole '{nazov_pola}' v modeli '{model_name}' neexistuje."})
            
        file_field = getattr(obj, nazov_pola)

        if not file_field:
            return json.dumps({"error": "Súbor v tomto zázname nie je nahratý."})

        if not hasattr(file_field, 'path') and not hasattr(file_field, 'url'):
            return json.dumps({"error": f"Pole '{nazov_pola}' nie je súborové pole."})

        try:
            file_path = file_field.path
        except (ValueError, NotImplementedError):
            return json.dumps({"error": "Súbor nie je fyzicky dostupný na lokálnom disku."})

        if not os.path.exists(file_path):
            return json.dumps({"error": "Fyzický súbor sa nenašiel na disku servera."})

        ext = os.path.splitext(file_path)[1].lower()
        extrahovany_text = ""
        pocet_stran = 1 # Predvolená hodnota pre bezpečný return na konci

        # Čítanie PDF cez PyMuPDF (Pridáva čísla strán!)
        if ext == '.pdf':
            try:
                # 🛡️ MEMORY LEAK OCHRANA: 'with' garantuje zatvorenie súboru aj pri chybe
                with fitz.open(file_path) as doc:
                    pocet_stran = len(doc) # Uložíme si počet strán, kým je súbor otvorený
                    for i, page in enumerate(doc):
                        page_text = page.get_text()
                        if page_text.strip():
                            # TOTO JE KRITICKÉ: AI vďaka tomuto vie, na akej je strane
                            extrahovany_text += f"\n\n--- STRANA {i + 1} ---\n"
                            extrahovany_text += page_text
            except Exception as pdf_err:
                return json.dumps({"error": f"Súbor PDF je poškodený: {str(pdf_err)}"})
            
            if not extrahovany_text.strip():
                # 🛡️ MULTIMODAL FALLBACK: Spracovanie skenov bez textovej vrstvy priamo cez Gemini Vision
                api_key = get_gemini_api_key()
                gemini_model = getattr(settings, 'GEMINI_MODEL', None)
                if api_key and gemini_model:
                    try:
                        client = genai.Client(api_key=api_key)
                        uploaded_pdf = client.files.upload(file=file_path)
                        try:
                            prompt = "Prepíš a vyťaž všetok text z tohto naskenovaného PDF dokumentu. Zachovaj štruktúru tabuliek, položiek a číselných údajov."
                            contents_pdf: list[Any] = [uploaded_pdf, prompt]
                            response = client.models.generate_content(
                                model=gemini_model,
                                contents=contents_pdf,
                                config=types.GenerateContentConfig(thinking_config=types.ThinkingConfig(thinking_level=getattr(settings, 'GEMINI_THINKING_LEVEL', types.ThinkingLevel.MEDIUM) or types.ThinkingLevel.MEDIUM))
                            )
                            extrahovany_text = response.text or "Dokument je prázdny sken."
                        finally:
                            try:
                                if uploaded_pdf.name:
                                    client.files.delete(name=uploaded_pdf.name)
                            except Exception: pass
                    except Exception as ocr_err:
                        extrahovany_text = f"PDF neobsahuje textovú vrstvu a automatické OCR zlyhalo: {ocr_err}"
                else:
                    extrahovany_text = "PDF neobsahuje textovú vrstvu (čistý sken bez OCR konfigurácie)."

        # Čítanie Obrázkov (Cez Gemini Vision)
        elif ext in ['.jpg', '.jpeg', '.png', '.webp']:
            api_key = get_gemini_api_key()
            gemini_model = getattr(settings, 'GEMINI_MODEL', None)

            if not api_key or not gemini_model:
                logger.warning("tool_precitaj_dokument: GEMINI API kľúč alebo GEMINI_MODEL nie je nakonfigurovaný.")
                return json.dumps({"error": "AI služba je momentálne nedostupná (chýba konfigurácia API kľúča alebo modelu)."})

            client = genai.Client(api_key=api_key)
            uploaded_file = client.files.upload(file=file_path)
            try:
                prompt = "Prečítaj a prepíš všetok text z tohto obrázka. Zachovaj logickú štruktúru dát."
                contents_img: list[Any] = [uploaded_file, prompt]
                response = client.models.generate_content(
                    model=gemini_model,
                    contents=contents_img,
                    config=types.GenerateContentConfig(thinking_config=types.ThinkingConfig(thinking_level=getattr(settings, 'GEMINI_THINKING_LEVEL', types.ThinkingLevel.MEDIUM) or types.ThinkingLevel.MEDIUM))
                )
                extrahovany_text = response.text or ""
            finally:
                try:
                    if uploaded_file.name:
                        client.files.delete(name=uploaded_file.name)
                except Exception: pass
                
        # Čítanie XML a textových súborov (Peppol eFaktúra)
        elif ext in ['.xml', '.txt', '.csv']:
            MAX_BYTES = 5000000 
            try:
                with open(file_path, 'r', encoding='utf-8-sig') as f:
                    extrahovany_text = f.read(MAX_BYTES)
            except UnicodeDecodeError:
                try:
                    with open(file_path, 'r', encoding='windows-1250') as f:
                        extrahovany_text = f.read(MAX_BYTES)
                except Exception:
                    with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                        extrahovany_text = f.read(MAX_BYTES)

        # 📊 EXCEL SPREADSHEET PARSER (openpyxl)
        elif ext in ['.xlsx', '.xlsm']:
            try:
                import openpyxl
                wb = openpyxl.load_workbook(file_path, data_only=True, read_only=True)
                try:
                    excel_lines = []
                    for sheetname in wb.sheetnames[:5]: # Max 5 listov pre ochranu RAM
                        ws = wb[sheetname]
                        excel_lines.append(f"\n--- LIST: {sheetname} ---")
                        for row in ws.iter_rows(values_only=True):
                            row_vals = [str(cell) if cell is not None else "" for cell in row]
                            if any(row_vals):
                                excel_lines.append(" | ".join(row_vals))
                            if len(excel_lines) > 2000: # Limit 2000 riadkov
                                excel_lines.append("[... Ďalšie riadky boli skrátené ...]")
                                break
                finally:
                    wb.close()
                extrahovany_text = "\n".join(excel_lines)
            except Exception as xl_err:
                return json.dumps({"error": f"Nepodarilo sa načítať Excel súbor: {str(xl_err)}"})
                    
        else:
            return json.dumps({"error": f"Nepodporovaný formát súboru: {ext}"})

        # 🧠 ISAS FORMAT-AWARE TRUNCATION: Rozdielna stratégia podľa typu súboru
        # 1 000 000 znakov pokryje cca 300-stranový PDF manuál alebo obrovskú zbernú eFaktúru.
        MAX_ZNAKOV = 1000000
        if len(extrahovany_text) > MAX_ZNAKOV:
            if ext in ['.xml', '.csv']:
                # Pre štruktúrované dáta (eFaktúry, exporty) zachováme hlavičku a sumáre na konci
                polovica = MAX_ZNAKOV // 2
                extrahovany_text = extrahovany_text[:polovica] + "\n\n[... STREDNÁ ČASŤ DOKUMENTU BOLA VYNECHANÁ PRE ÚSPORU TOKENOV ...]\n\n" + extrahovany_text[-polovica:]
            else:
                # Pre PDF manuály, TXT a obrázky zachováme súvislý text od začiatku.
                # (Vyrezanie stredu by zničilo nadväznosť kapitol v manuáli pre technikov!)
                extrahovany_text = extrahovany_text[:MAX_ZNAKOV] + "\n\n[... ZVYŠOK DOKUMENTU BOL SKRÁTENÝ PRE OCHRANU PAMÄTE A TOKENOV ...]"

        return json.dumps({
            "status": "success",
            "nazov_suboru": os.path.basename(file_path),
            "pocet_stran": pocet_stran,
            "obsah_dokumentu": extrahovany_text
        })

    except Exception as e:
        return json.dumps({"error": f"Kritická chyba pri čítaní: {str(e)}"})


def tool_vygeneruj_obrazok_strany(model_name: str, object_id: int, nazov_pola: str, cislo_strany: int) -> str:
    """
    Použi, ak chceš používateľovi vizuálne zobraziť konkrétnu stranu z PDF (napr. schému, nákres, podpis).
    Vráti ti HTML kód obrázka, ktorý MUSÍŠ vložiť do svojej odpovede.
    """    
    try:
        # 🛡️ TYPE SAFE GUARD: Ochrana pred stringovou injektážou z LLM
        try:
            safe_obj_id = int(object_id)
            safe_page_num = int(cislo_strany)
        except (ValueError, TypeError):
            return json.dumps({"error": "object_id aj cislo_strany musia byť celé čísla."})

        Model = None
        industry_mod = (getattr(settings, 'INDUSTRY_MODULE', None) or '').strip().lower()
        search_apps = ['Evidencia', 'ISASaplikacia', 'WebBuilder']
        if industry_mod:
            search_apps.extend([industry_mod, f"odvetvia.{industry_mod}"])
        for app_label in search_apps:
            try:
                Model = apps.get_model(app_label, model_name)
                if Model: break
            except LookupError:
                continue

        if not Model:
            vsetky_odvetvove_modely = list(ZARIADENIA_MAP.values()) + list(PARENT_MODELS.values()) + [m for subcats in CATEGORY_MAP.values() for m in subcats.values()]
            for m in vsetky_odvetvove_modely:
                if hasattr(m, '_meta') and m._meta.object_name.lower() == str(model_name).lower():
                    Model = m
                    break

        if not Model:
            return json.dumps({"error": f"Model '{model_name}' neexistuje v systéme ani v registroch aktívneho odvetvia."})

        obj = Model.objects.filter(pk=safe_obj_id).first()
        if not obj:
            return json.dumps({"error": f"Záznam s ID {safe_obj_id} v modeli {model_name} neexistuje."})

        if not hasattr(obj, nazov_pola):
            return json.dumps({"error": f"Pole '{nazov_pola}' v modeli '{model_name}' neexistuje."})

        file_field = getattr(obj, nazov_pola)
        if not file_field:
            return json.dumps({"error": "Súbor v tomto zázname nie je nahratý."})

        try:
            file_path = file_field.path
        except (ValueError, NotImplementedError):
            return json.dumps({"error": "Súbor nie je fyzicky dostupný na lokálnom disku (pravdepodobne je v Cloud úložisku)."})

        if not os.path.exists(file_path):
            return json.dumps({"error": "Fyzický súbor sa nenašiel na disku servera."})

        ext = os.path.splitext(file_path)[1].lower()

        # 1. Príprava cache priečinka v MEDIA_ROOT
        cache_dir = os.path.join(settings.MEDIA_ROOT, 'ai_pdf_cache')
        os.makedirs(cache_dir, exist_ok=True)

        aktualny_cas = time.time()

        safe_model_name = re.sub(r'[^a-zA-Z0-9_]', '', model_name)
        if not safe_model_name:
            return json.dumps({"error": "Neplatný názov modelu."})

        if ext == '.pdf':
            with fitz.open(file_path) as doc:
                if safe_page_num < 1 or safe_page_num > len(doc):
                    return json.dumps({"error": f"Neplatné číslo strany. Dokument má {len(doc)} strán."})

                page = doc.load_page(safe_page_num - 1)
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))

                filename = f"pdf_render_{safe_model_name}_{safe_obj_id}_strana_{safe_page_num}_{int(aktualny_cas)}_{secrets.token_hex(8)}.png"
                save_path = os.path.join(cache_dir, filename)
                pix.save(save_path)

            img_url = reverse('evidencia:ai_pdf_cache_image', args=[filename])

        elif ext in ['.jpg', '.jpeg', '.png', '.webp']:
            from PIL import Image, ImageOps
            with Image.open(file_path) as img:
                processed_img = ImageOps.exif_transpose(img)
                if processed_img.mode not in ('RGB', 'RGBA'):
                    processed_img = processed_img.convert('RGBA' if 'A' in processed_img.getbands() else 'RGB')
                filename = f"pdf_render_{safe_model_name}_{safe_obj_id}_strana_1_{int(aktualny_cas)}_{secrets.token_hex(8)}.png"
                save_path = os.path.join(cache_dir, filename)
                processed_img.save(save_path, format='PNG')

            img_url = reverse('evidencia:ai_pdf_cache_image', args=[filename])
        else:
            return json.dumps({"error": "Tento formát sa nedá vizuálne zobraziť."})

        html_tag = f'\n<img src="{escape(img_url)}" alt="Strana {safe_page_num}" style="max-width: 100%; border: 1px solid #cbd5e1; border-radius: 8px; margin-top: 15px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">\n'

        return json.dumps({
            "status": "success",
            "html_kod": html_tag,
            "instrukcia_pre_ai": "TOTO JE DÔLEŽITÉ: Skopíruj hodnotu z 'html_kod' a vlož ju PRESNE TAKTO do svojej textovej odpovede používateľovi. Nijako ten HTML kód neupravuj!"
        })

    except Exception as e:
        return json.dumps({"error": f"Chyba pri generovaní obrázka: {str(e)}"})

def tool_historicky_stav_skladu(datum: str, hladany_vyraz: str) -> str:
    """
    Použi na zistenie historického stavu skladu konkrétnej položky k zadanému dátumu.
    Dátum zadávaj vo formáte YYYY-MM-DD (akceptuje aj DD.MM.YYYY).
    """
    try:
        parsed_target = to_date(datum)
        if datum and str(datum).strip() and str(datum).strip().lower() not in ['null', 'none'] and not parsed_target:
            return json.dumps({"error": f"Neplatný formát dátumu: '{datum}'. Použite formát YYYY-MM-DD alebo DD.MM.YYYY."})
        target_date = parsed_target or timezone.now().date()

        if not hladany_vyraz or str(hladany_vyraz).strip().lower() in ['null', 'none', '']:
            return json.dumps({"error": "Musíte zadať hľadaný výraz (názov, katalógové číslo alebo sériové číslo dielu)."})

        clean_vyraz = str(hladany_vyraz).strip()

        StockMovement = apps.get_model('Evidencia', 'StockMovement')
        Filialka = apps.get_model('Evidencia', 'Filialka')

        najdeny_produkt = None
        skutocna_kategoria = None

        # 1. Nájdenie produktu v databáze
        for main_cat, subcats in CATEGORY_MAP.items():
            if najdeny_produkt: break
            for subcat_name, model_class in subcats.items():
                zhoda = model_class.objects.filter(
                    Q(seriove_cislo__icontains=clean_vyraz) | 
                    Q(kto_som__icontains=clean_vyraz) |
                    Q(katalogove_cislo__icontains=clean_vyraz) |
                    Q(ean_kod__icontains=clean_vyraz)
                ).first()
                
                if zhoda:
                    najdeny_produkt = zhoda
                    skutocna_kategoria = f"{main_cat} -> {subcat_name}"
                    break

        if not najdeny_produkt:
            return json.dumps({"error": f"Položka pre výraz '{hladany_vyraz}' nebola v sklade nájdená."})

        cisty_nazov = ocisti_nazov_od_jednotky(str(najdeny_produkt))
        merna_jednotka = getattr(najdeny_produkt, 'merna_jednotka', 'ks')

        # 2. Získanie aktívnych filiálok (1 rýchly dopyt)
        filialky_map = {f.id: f.nazov for f in Filialka.objects.filter(je_aktivna=True)}

        # 3. Získanie aktuálnych stavov na filiálkach (1 dopyt, eliminuje N+1 z LedgerKalkulacka)
        aktualne_stavy_db = StockMovement.objects.filter(
            product_name=cisty_nazov,
            filialka__isnull=False
        ).values('filialka').annotate(
            in_qty=Coalesce(Sum('count', filter=Q(action='ADD')), 0, output_field=IntegerField()),
            out_qty=Coalesce(Sum('count', filter=Q(action='REMOVE')), 0, output_field=IntegerField())
        )
        aktualne_stavy_mapa = {item['filialka']: item['in_qty'] - item['out_qty'] for item in aktualne_stavy_db}

        # 4. Získanie pohybov PO zadanom dátume (1 dopyt)
        pohyby_po_datume = StockMovement.objects.filter(
            product_name=cisty_nazov,
            timestamp__date__gt=target_date
        ).values('filialka').annotate(
            soucet_add=Coalesce(Sum('count', filter=Q(action='ADD')), 0, output_field=IntegerField()),
            soucet_remove=Coalesce(Sum('count', filter=Q(action='REMOVE')), 0, output_field=IntegerField())
        )
        pohyby_mapa: dict[Optional[int], dict[str, int]] = {p['filialka']: {'add': p['soucet_add'], 'remove': p['soucet_remove']} for p in pohyby_po_datume}

        vysledok_sklady = []
        celkovy_historicky_stav = 0
        celkovy_aktualny_stav = 0

        # --- A. VÝPOČET PRE HLAVNÝ SKLAD ---
        aktualny_hlavny = getattr(najdeny_produkt, 'pocet_na_sklade', 0) or 0
        pohyby_hlavny = pohyby_mapa.get(None, {'add': 0, 'remove': 0})
        historicky_hlavny = aktualny_hlavny - pohyby_hlavny['add'] + pohyby_hlavny['remove']
        
        celkovy_aktualny_stav += aktualny_hlavny
        celkovy_historicky_stav += historicky_hlavny

        Sklad = apps.get_model('Evidencia', 'Sklad')
        hlavny_sklad = Sklad.objects.first()
        nazov_hlavneho = f"Hlavný sklad ({hlavny_sklad.nazov})" if hlavny_sklad and hlavny_sklad.nazov and hlavny_sklad.nazov.lower() != "hlavný sklad" else "Hlavný sklad"

        vysledok_sklady.append({
            "sklad": nazov_hlavneho,
            "aktualny_stav": aktualny_hlavny,
            "historicky_stav": historicky_hlavny
        })

        # --- B. VÝPOČET PRE VŠETKY FILIÁLKY / DODÁVKY ---
        for f_id, f_nazov in filialky_map.items():
            aktualny_f = aktualne_stavy_mapa.get(f_id, 0)
            pohyby_f = pohyby_mapa.get(f_id, {'add': 0, 'remove': 0})
            historicky_f = aktualny_f - pohyby_f['add'] + pohyby_f['remove']
            
            celkovy_aktualny_stav += aktualny_f
            celkovy_historicky_stav += historicky_f

            # Pridáme do reportu len tie dodávky, kde sa diel reálne nachádzal alebo nachádza
            if aktualny_f > 0 or historicky_f > 0 or pohyby_f['add'] > 0 or pohyby_f['remove'] > 0:
                vysledok_sklady.append({
                    "sklad": f_nazov,
                    "aktualny_stav": aktualny_f,
                    "historicky_stav": historicky_f
                })

        return json.dumps({
            "polozka": cisty_nazov,
            "kategoria": skutocna_kategoria,
            "hladany_datum": datum,
            "merna_jednotka": merna_jednotka,
            "celkovy_aktualny_stav_firma": celkovy_aktualny_stav,
            "celkovy_historicky_stav_firma": celkovy_historicky_stav,
            "rozpis_podla_skladov": vysledok_sklady,
            "vysvetlenie": "Zobrazuje stav na konci zadaného dňa. Vypočítané reverznou matematikou z aktuálneho stavu a histórie pohybov."
        })

    except Exception as e:
        return json.dumps({"error": f"Kritická chyba pri výpočte historického stavu: {str(e)}"})

def tool_vyhladaj_a_precitaj_email(hladany_vyraz: Optional[str] = None, email_id: Optional[int] = None, limit: int = 10) -> str:
    """
    Použi na VYHĽADÁVANIE v e-mailoch (prijatých aj odoslaných) alebo na prečítanie KONKRÉTNEHO e-mailu podľa ID.
    Vráti ti aj zoznam príloh (s ich ID), aby si ich mohol následne prečítať cez tool_precitaj_dokument.
    """
    return _vykonaj_email_dotaz(hladany_vyraz=hladany_vyraz, email_id=email_id, limit=limit)


def _vykonaj_email_dotaz(hladany_vyraz: Optional[str] = None, email_id: Optional[int] = None, limit: int = 10, vylucit_kategorie: Optional[list[str]] = None) -> str:
    # 🛡️ DEFENSIVE TYPECASTING
    try:
        limit = max(1, min(int(limit), 50)) if limit is not None else 10
    except (ValueError, TypeError):
        limit = 10

    EmailovaSprava = apps.get_model('ISASaplikacia', 'EmailovaSprava')
    
    try:
        if email_id is not None and str(email_id).strip() and str(email_id).strip().lower() not in ['null', 'none']:
            try:
                safe_mail_id = int(str(email_id).strip())
                maily = EmailovaSprava.objects.filter(id=safe_mail_id).select_related('zakaznik', 'dodavatel', 'zamestnanec', 'ucet').prefetch_related('prilohy', 'optout_zaznamy')
            except ValueError:
                return json.dumps({"error": f"Neplatné ID e-mailu: '{email_id}'. ID musí byť celé číslo."})
        else:
            maily = EmailovaSprava.objects.select_related('zakaznik', 'dodavatel', 'zamestnanec', 'ucet').prefetch_related('prilohy', 'optout_zaznamy').order_by('-datum_prijatia')
            if hladany_vyraz:
                maily = maily.filter(
                    Q(odosielatel__icontains=hladany_vyraz) |
                    Q(prijemca__icontains=hladany_vyraz) |
                    Q(predmet__icontains=hladany_vyraz) |
                    Q(telo_text__icontains=hladany_vyraz) |
                    Q(prilohy__nazov_suboru__icontains=hladany_vyraz) |
                    Q(zakaznik__Nazov_spolocnosti__icontains=hladany_vyraz) |
                    Q(dodavatel__Nazov_spolocnosti__icontains=hladany_vyraz)
                ).distinct()

        if vylucit_kategorie:
            maily = maily.exclude(
                Q(ai_kategoria__in=vylucit_kategorie) | Q(ai_kategoria__isnull=True) | Q(ai_kategoria='')
            )
        
        vysledky: list[dict[str, Any]] = []
        for m in maily[:limit]:
            prilohy = [{"id_prilohy": p.id, "nazov": p.nazov_suboru, "mime_type": p.mime_type or "Neznámy", "model_pre_citanie": "EmailPriloha", "pole_pre_citanie": "subor"} for p in m.prilohy.all()]
            
            vysledky.append({
                "email_id": m.id,
                "message_id": m.message_id,
                "prijate_do_schranky": m.ucet.email_adresa if m.ucet else "Neznáma schránka",
                "zlozka": m.zlozka,
                "datum": m.datum_prijatia.strftime('%d.%m.%Y %H:%M') if m.datum_prijatia else "Neznáme",
                "odosielatel": m.odosielatel,
                "prijemca": m.prijemca,
                "predmet": m.predmet or "Bez predmetu",
                "precitane": m.precitane,
                "ai_kategoria": m.ai_kategoria,
                "ai_zhrnutie": m.ai_zhrnutie,
                "text_spravy": m.telo_text[:1500] + ("..." if m.telo_text and len(m.telo_text) > 1500 else "") if m.telo_text else "Iba HTML obsah",
                "obsahuje_html_telo": bool(m.telo_html),
                "prilohy": prilohy if prilohy else "Žiadne prílohy",
                "spojene_s_crm_zakaznik": m.zakaznik.Nazov_spolocnosti if m.zakaznik else None,
                "spojene_s_crm_dodavatel": m.dodavatel.Nazov_spolocnosti if m.dodavatel else None,
                "spojene_s_crm_zamestnanec": f"{m.zamestnanec.meno} {m.zamestnanec.priezvisko}" if m.zamestnanec else None,
                "opt_out_zaznamy": [
                    {
                        "id": o.id,
                        "email": o.email,
                        "aktivny": o.aktivny,
                        "zdroj": o.get_zdroj_display(),
                        "rozsah": o.get_rozsah_display(),
                        "dovod": o.dovod,
                        "poznamka": o.poznamka or "Žiadna",
                        "vytvorene": o.vytvorene.strftime('%d.%m.%Y %H:%M') if o.vytvorene else "Neznáme",
                        "aktualizovane": o.aktualizovane.strftime('%d.%m.%Y %H:%M') if o.aktualizovane else "Neznáme",
                    }
                    for o in m.optout_zaznamy.all()
                ]
            })
            
        return json.dumps({"najdene_emaily": vysledky}) if vysledky else json.dumps({"informacia": "Žiadne e-maily nezodpovedajú hľadaniu."})
    except Exception as e:
        return json.dumps({"error": str(e)})

def tool_vypocitaj_trasu_a_logistiku(start_adresa: str, ciel_adresa: str) -> str:
    """
    Použi na presný výpočet vzdialenosti (km) a času jazdy (minúty) medzi dvoma adresami.
    Využíva interný ISAS logistický engine (Google Maps / OSRM).
    """
    try:
        if not start_adresa or not ciel_adresa or str(start_adresa).strip().lower() in ['none', 'null', ''] or str(ciel_adresa).strip().lower() in ['none', 'null', '']:
            return json.dumps({"error": "Štartovacia aj cieľová adresa musia byť zadané a nesmú byť prázdne."})

        clean_start = str(start_adresa).strip()
        clean_ciel = str(ciel_adresa).strip()

        # 🛡️ DEFENSIVE LOGISTICS: Zamedzenie chybovej hlášky pri presune na rovnakej adrese
        if clean_start.lower() == clean_ciel.lower():
            return json.dumps({
                "start_adresa": clean_start,
                "ciel_adresa": clean_ciel,
                "vzdialenost_km": 0.0,
                "odhadovany_cas_jazdy_minuty": 0,
                "informacia": "Štartovacia a cieľová adresa sú totožné."
            })

        km, minuty = isas_ai_logistika_trasy(clean_start, clean_ciel)
        
        if km is None or minuty is None or (km <= 0 and minuty <= 0):
            return json.dumps({
                "chyba": f"Nepodarilo sa nájsť cestnú trasu medzi '{clean_start}' a '{clean_ciel}'. Skontrolujte presnosť adries.",
                "instrukcia_pre_ai": "ZÁKAZ vymýšľať si alebo odhadovať kilometre! Priznaj používateľovi, že navigácia nenašla trasu pre zadané adresy."
            })

        return json.dumps({
            "start_adresa": clean_start,
            "ciel_adresa": clean_ciel,
            "vzdialenost_km": km,
            "odhadovany_cas_jazdy_minuty": minuty,
            "informacia": "Výpočet je presný, založený na reálnej cestnej sieti."
        })
    except Exception as e:
        return json.dumps({"error": f"Chyba logistického enginu: {str(e)}"})

def tool_spravuj_verejny_web(akcia: str, ulozit_ako_draft: bool = True, domena: Optional[str] = None, titulok_webu: Optional[str] = None, hlavna_farba: Optional[str] = None, meta_popis: Optional[str] = None, zobrazit_live_sla: Optional[bool] = None, sla_text_optimalna: Optional[str] = None, sla_text_zvysena: Optional[str] = None, sla_text_kriticka: Optional[str] = None, bloky_json: Optional[str] = None) -> str:
    """
    Použi na AUTOMATICKÉ VYTVORENIE alebo ÚPRAVU verejného webu.
    'ulozit_ako_draft' nechaj VŽDY True, pokiaľ ťa šéf vyslovene nepožiada o okamžitý prepis ostrej verzie.
    'akcia' musí byť: 'UPRAVIT_NASTAVENIA' (mení len farby/SEO), 'UPRAVIT_ALEBO_PRIDAT_BLOK' (upraví texty/pridá konkrétne bloky bez zmazania ostatných), 'DEAKTIVOVAT_BLOK' (skryje blok) alebo 'VYGENEROVAT_CELY_WEB' (zmaže staré bloky a vytvorí nové podľa bloky_json).
    'bloky_json' musí byť validný JSON string poľa objektov: [{"typ": "HERO", "nadpis": "...", "podnadpis": "...", "tlacidlo_text": "...", "poradie": 10}, ...]
    Povolené typy blokov: HERO, ABOUT, SERVICES, RENTAL, PARTS, BRANDS, REFERENCES, TEAM, TICKET, ENGINEERING_HUB, CONTACT, IOT_DEMO, CAREER, SLA_CALCULATOR, BUYBACK, WARRANTY_CLAIM, BOOKING, FAQ, BLOG_TEASER.
    """

    try:
        WebNastavenia = apps.get_model('WebBuilder', 'WebNastavenia')
        WebBlok = apps.get_model('WebBuilder', 'WebBlok')
    except LookupError:
        return json.dumps({"error": "Modely pre web nie sú v systéme dostupné."})

    POVOLENE_BLOKY = [
        'HERO', 'ABOUT', 'SERVICES', 'RENTAL', 'PARTS', 'BRANDS', 'REFERENCES',
        'TEAM', 'TICKET', 'ENGINEERING_HUB', 'CONTACT', 'IOT_DEMO',
        'CAREER', 'SLA_CALCULATOR', 'BUYBACK', 'WARRANTY_CLAIM', 'BOOKING', 'FAQ', 'BLOG_TEASER'
    ]

    try:
        with transaction.atomic():
            verzia_zaznamu = 'DRAFT' if ulozit_ako_draft else 'LIVE'
            pk_nastaveni = 2 if ulozit_ako_draft else 1
            
            # 1. Správa nastavení
            nastavenia = WebNastavenia.objects.filter(pk=pk_nastaveni).first()
            if not nastavenia:
                live_nastavenia = WebNastavenia.objects.filter(pk=1).first()
                if live_nastavenia:
                    nastavenia = WebNastavenia(
                        pk=pk_nastaveni, domena=live_nastavenia.domena, titulok_webu=live_nastavenia.titulok_webu,
                        hlavna_farba=live_nastavenia.hlavna_farba, logo=live_nastavenia.logo,
                        meta_popis=live_nastavenia.meta_popis, zobrazit_live_sla=live_nastavenia.zobrazit_live_sla,
                        sla_text_optimalna=getattr(live_nastavenia, 'sla_text_optimalna', None),
                        sla_text_zvysena=getattr(live_nastavenia, 'sla_text_zvysena', None),
                        sla_text_kriticka=getattr(live_nastavenia, 'sla_text_kriticka', None)
                    )
                    nastavenia.save()
                else:
                    identita = FiremnaIdentita.objects.first()
                    gn = apps.get_model('ISASaplikacia', 'GlobalneNastavenia').objects.first()
                    cisty_domena_arg = domena.strip() if domena and domena.strip() else None
                    env_public_domain = getattr(settings, 'PUBLIC_WEB_DOMAIN', '').strip()
                    vychodzia = (
                        cisty_domena_arg or
                        env_public_domain or
                        (identita.web.strip() if identita and identita.web else '') or
                        (gn.url_systemu.strip() if gn and gn.url_systemu else '')
                    )
                    if not vychodzia and getattr(settings, 'DEBUG', False):
                        vychodzia = '127.0.0.1'
                    if not vychodzia:
                        return json.dumps({"error": "Nemožno inicializovať web: V .env chýba PUBLIC_WEB_DOMAIN a v systéme nie je evidovaný web firmy."})

                    # 🛡️ 100% Očistenie od protokolu, www, ciest, query parametrov a portu (:8000)
                    cista_init_domena = re.sub(r'^https?://', '', str(vychodzia).strip().lower())
                    cista_init_domena = re.sub(r'^www\.', '', cista_init_domena).split('/')[0].split('?')[0].split('#')[0]
                    cista_init_domena = cista_init_domena.split(':')[0].strip()

                    # 🛡️ ARCHITEKTÚRA: Ak v DB neexistuje LIVE (PK 1), musíme VŽDY prioritne vytvoriť PK 1
                    live_nast = WebNastavenia.objects.filter(pk=1).first()
                    if not live_nast:
                        live_nast = WebNastavenia.objects.create(pk=1, domena=cista_init_domena)

                    if pk_nastaveni == 2:
                        nastavenia = WebNastavenia.objects.create(
                            pk=2, domena=cista_init_domena,
                            titulok_webu=live_nast.titulok_webu,
                            hlavna_farba=live_nast.hlavna_farba,
                            meta_popis=live_nast.meta_popis,
                            zobrazit_live_sla=live_nast.zobrazit_live_sla,
                            sla_text_optimalna=getattr(live_nast, 'sla_text_optimalna', None),
                            sla_text_zvysena=getattr(live_nast, 'sla_text_zvysena', None),
                            sla_text_kriticka=getattr(live_nast, 'sla_text_kriticka', None),
                            logo=live_nast.logo
                        )
                    else:
                        nastavenia = live_nast
                    
            zmeny_nastaveni = []
            
            if domena is not None: nastavenia.domena = domena; zmeny_nastaveni.append("doména")
            if titulok_webu is not None: nastavenia.titulok_webu = titulok_webu; zmeny_nastaveni.append("titulok")
            if hlavna_farba is not None: nastavenia.hlavna_farba = hlavna_farba; zmeny_nastaveni.append("farba")
            if meta_popis is not None: nastavenia.meta_popis = meta_popis; zmeny_nastaveni.append("SEO popis")
            if zobrazit_live_sla is not None: nastavenia.zobrazit_live_sla = zobrazit_live_sla; zmeny_nastaveni.append("Live SLA")
            if sla_text_optimalna is not None: nastavenia.sla_text_optimalna = sla_text_optimalna; zmeny_nastaveni.append("SLA text optimálna")
            if sla_text_zvysena is not None: nastavenia.sla_text_zvysena = sla_text_zvysena; zmeny_nastaveni.append("SLA text zvýšená")
            if sla_text_kriticka is not None: nastavenia.sla_text_kriticka = sla_text_kriticka; zmeny_nastaveni.append("SLA text kritická")
            
            if zmeny_nastaveni:
                nastavenia.clean() # 🛡️ Očistenie domény pred uložením (save nevolá clean automaticky)
                nastavenia.save()

            # 2. Generovanie blokov
            sprava_blokov = "Bloky neboli menené."
            if akcia in ['VYGENEROVAT_CELY_WEB', 'VYGENEROVAT_WEB', 'UPRAVIT_ALEBO_PRIDAT_BLOK', 'DEAKTIVOVAT_BLOK'] and bloky_json:
                try:
                    # 🛡️ POLYMORFNÁ OCHRANA: Ošetrenie prípadu, kedy Gemini odovzdá priamo Python list/dict
                    if isinstance(bloky_json, list):
                        nove_bloky = bloky_json
                    elif isinstance(bloky_json, dict):
                        nove_bloky = [bloky_json]
                    else:
                        cisty_json = str(bloky_json).strip()
                        if cisty_json.startswith('```json'): cisty_json = cisty_json[7:]
                        elif cisty_json.startswith('```'): cisty_json = cisty_json[3:]
                        if cisty_json.endswith('```'): cisty_json = cisty_json[:-3]
                        nove_bloky = json.loads(cisty_json.strip())
                        if isinstance(nove_bloky, dict):
                            nove_bloky = [nove_bloky]
                except Exception as json_err:
                    return json.dumps({"error": f"Parameter bloky_json nie je validný JSON: {str(json_err)}"})
                
                vytvorene_bloky = []
                
                if akcia in ['VYGENEROVAT_CELY_WEB', 'VYGENEROVAT_WEB']:
                    # 🛡️ ZÁCHRANA OBRÁZKOV: Uložíme si referencie na existujúce obrázky pred zmazaním
                    existujuce_obrazky = {b.typ: b.obrazok for b in WebBlok.objects.filter(verzia=verzia_zaznamu).exclude(obrazok='')}
                    if not existujuce_obrazky and ulozit_ako_draft:
                        # Ak draft nemá obrázky, skúsime zachrániť z LIVE verzie
                        existujuce_obrazky = {b.typ: b.obrazok for b in WebBlok.objects.filter(verzia='LIVE').exclude(obrazok='')}
                        
                    WebBlok.objects.filter(verzia=verzia_zaznamu).delete()
                    
                    poradie = 10
                    for b in nove_bloky[:25]:
                        if not isinstance(b, dict): continue
                        typ_bloku = str(b.get('typ') or '').strip().upper()
                        if typ_bloku not in POVOLENE_BLOKY or typ_bloku in vytvorene_bloky: continue
                            
                        WebBlok.objects.create(
                            typ=typ_bloku, verzia=verzia_zaznamu, poradie=poradie, je_aktivny=True,
                            nadpis=str(b.get('nadpis', ''))[:255], podnadpis=str(b.get('podnadpis', '')),
                            tlacidlo_text=str(b.get('tlacidlo_text', ''))[:50],
                            obrazok=existujuce_obrazky.get(typ_bloku)
                        )
                        vytvorene_bloky.append(typ_bloku)
                        poradie += 10
                    sprava_blokov = f"Staré bloky zmazané. Úspešne vygenerovaných {len(vytvorene_bloky)} nových blokov: {', '.join(vytvorene_bloky)}."
                
                elif akcia == 'UPRAVIT_ALEBO_PRIDAT_BLOK':
                    # Ak upravujeme DRAFT a zatiaľ neexistuje, najprv prekopírujeme LIVE bloky do DRAFTU
                    if ulozit_ako_draft and not WebBlok.objects.filter(verzia='DRAFT').exists():
                        for live_blok in WebBlok.objects.filter(verzia='LIVE'):
                            live_blok.pk = None
                            live_blok.verzia = 'DRAFT'
                            live_blok.save()
                            
                    for b in nove_bloky[:25]:
                        if not isinstance(b, dict): continue
                        typ_bloku = str(b.get('typ') or '').strip().upper()
                        if typ_bloku not in POVOLENE_BLOKY or typ_bloku in vytvorene_bloky: continue
                        
                        blok = WebBlok.objects.filter(typ=typ_bloku, verzia=verzia_zaznamu).first()
                        if not blok:
                            blok = WebBlok(typ=typ_bloku, verzia=verzia_zaznamu, poradie=b.get('poradie', 99) if isinstance(b.get('poradie'), int) else 99)
                        elif 'poradie' in b and isinstance(b['poradie'], int):
                            blok.poradie = b['poradie']
                            
                        if 'nadpis' in b: blok.nadpis = str(b['nadpis'])[:255]
                        if 'podnadpis' in b: blok.podnadpis = str(b['podnadpis'])
                        if 'tlacidlo_text' in b: blok.tlacidlo_text = str(b['tlacidlo_text'])[:50]
                        blok.je_aktivny = True
                        blok.save()
                        vytvorene_bloky.append(typ_bloku)
                    sprava_blokov = f"Úspešne upravené/pridané bloky: {', '.join(vytvorene_bloky)}."
                    
                elif akcia == 'DEAKTIVOVAT_BLOK':
                    if ulozit_ako_draft and not WebBlok.objects.filter(verzia='DRAFT').exists():
                        for live_blok in WebBlok.objects.filter(verzia='LIVE'):
                            live_blok.pk = None
                            live_blok.verzia = 'DRAFT'
                            live_blok.save()
                            
                    for b in nove_bloky[:25]:
                        if not isinstance(b, dict): continue
                        typ_bloku = str(b.get('typ') or '').strip().upper()
                        if typ_bloku not in POVOLENE_BLOKY: continue
                        WebBlok.objects.filter(typ=typ_bloku, verzia=verzia_zaznamu).update(je_aktivny=False)
                        vytvorene_bloky.append(typ_bloku)
                    sprava_blokov = f"Úspešne deaktivované bloky: {', '.join(vytvorene_bloky)}."

            # 3. Okamžitá invalidácia cache
            cache.add('web_cache_version', 1, None)
            try: cache.incr('web_cache_version')
            except ValueError: cache.set('web_cache_version', 2, None)
            
            if ulozit_ako_draft:
                info_msg = "Návrh webu (DRAFT) bol úspešne vytvorený. Zákazníci ho zatiaľ nevidia. Povedz šéfovi, nech si ho pozrie na adrese: /?preview=ai_draft"
            else:
                info_msg = "Web bol úspešne aktualizovaný a zmeny sú okamžite online."
                
            return json.dumps({
                "status": "success",
                "informacia": info_msg,
                "upravene_nastavenia": zmeny_nastaveni if zmeny_nastaveni else "Žiadne",
                "stav_blokov": sprava_blokov
            })

    except Exception as e:
        return json.dumps({"error": f"Kritická chyba pri zápise webu: {str(e)}"})

def tool_spravuj_viditelnost_entit(typ_entity: str, id_entity: int, zobrazit: bool, nazov_skladoveho_modelu: Optional[str] = None) -> str:
    """
    Použi na zapnutie alebo vypnutie zobrazenia konkrétneho záznamu na verejnom webe.
    'typ_entity' musí byť jedno z: 'ZAMESTNANEC', 'SLUZBA', 'SLUZBA_POSTUP', 'SLUZBA_BOOKING', 'ZNACKA', 'REFERENCIA', 'PRENAJOM', 'ZAKAZNIK_REFERENCIA', 'POZICIA', 'PRODUKT_ESHOP'.
    'id_entity' je ID daného záznamu v databáze.
    'nazov_skladoveho_modelu' zadaj výhradne pri 'PRODUKT_ESHOP' (napr. názov konkrétneho skladového modelu produktu).
    """    
    mapovanie = {
        'ZAMESTNANEC': ('Evidencia', 'Zamestnanec', 'zobrazit_na_webe'),
        'SLUZBA': ('Evidencia', 'TypServisuPrace', 'zobrazit_na_webe'),
        'SLUZBA_POSTUP': ('Evidencia', 'TypServisuPrace', 'zverejnit_postup_na_webe'),
        'SLUZBA_BOOKING': ('Evidencia', 'TypServisuPrace', 'povolit_online_booking'),
        'ZNACKA': ('Evidencia', 'DatabazaVyrobcov', 'zobrazit_na_webe'),
        'REFERENCIA': ('Evidencia', 'ServisnyZaznam', 'zobrazit_na_webe'),
        'PRENAJOM': ('Evidencia', 'ServisovaneZariadenie', 'zobrazit_na_webe_prenajom'),
        'ZAKAZNIK_REFERENCIA': ('Evidencia', 'Zakaznik', 'zobrazit_ako_referenciu'),
        'POZICIA': ('Evidencia', 'PracovnaPozicia', 'je_aktivna'),
    }
    
    typ_upper = (typ_entity or '').upper().strip()
    
    # 🛒 FULL E-SHOP INTEGRÁCIA: Dynamické vyhľadanie skladového modelu
    if typ_upper in ['PRODUKT_ESHOP', 'SKLAD_KARTA', 'DIEL']:
        if not nazov_skladoveho_modelu:
            # Automatické vyhľadanie naprieč celým CATEGORY_MAP
            najdeny_obj = None
            for main_cat, subcats in CATEGORY_MAP.items():
                if najdeny_obj: break
                for subcat_name, m_cls in subcats.items():
                    if hasattr(m_cls, 'zobrazit_na_webe'):
                        item = m_cls.objects.filter(pk=id_entity).first()
                        if item:
                            najdeny_obj = item
                            break
            if not najdeny_obj:
                return json.dumps({"error": f"Skladový diel s ID {id_entity} sa nenašiel v žiadnej kategórii skladu."})
            
            najdeny_obj.zobrazit_na_webe = zobrazit
            najdeny_obj.save(update_fields=['zobrazit_na_webe'])
            cache.add('web_cache_version', 1, None)
            try: cache.incr('web_cache_version')
            except ValueError: cache.set('web_cache_version', 2, None)
            stav = "ZOBRAZENÝ V E-SHOPE" if zobrazit else "SKRYTÝ Z E-SHOPU"
            return json.dumps({"status": "success", "informacia": f"Produkt '{najdeny_obj}' (ID: {id_entity}) bol úspešne nastavený ako {stav}."})
            
        try:
            skladove_modely_map = {m.__name__: m for subcats in CATEGORY_MAP.values() for m in subcats.values()}
            Model = skladove_modely_map.get(nazov_skladoveho_modelu)
            if not Model:
                return json.dumps({"error": f"Skladový model '{nazov_skladoveho_modelu}' nebol nájdený v aktívnom odvetví."})
            obj = Model.objects.get(pk=id_entity)
            obj.zobrazit_na_webe = zobrazit
            obj.save(update_fields=['zobrazit_na_webe'])
            cache.add('web_cache_version', 1, None)
            try: cache.incr('web_cache_version')
            except ValueError: cache.set('web_cache_version', 2, None)
            stav = "ZOBRAZENÝ V E-SHOPE" if zobrazit else "SKRYTÝ Z E-SHOPU"
            return json.dumps({"status": "success", "informacia": f"Produkt '{obj}' bol úspešne nastavený ako {stav}."})
        except Exception as e_mod:
            return json.dumps({"error": f"Chyba pri aktualizácii skladového produktu: {str(e_mod)}"})

    if typ_upper not in mapovanie:
        return json.dumps({"error": f"Neznámy typ entity: {typ_entity}. Povolené sú: {', '.join(mapovanie.keys())}, PRODUKT_ESHOP"})
        
    app_label, model_name, field_name = mapovanie[typ_upper]
    
    try:
        Model = apps.get_model(app_label, model_name)
        obj = Model.objects.get(pk=id_entity)
        
        # Zmena hodnoty
        setattr(obj, field_name, zobrazit)
        obj.save(update_fields=[field_name])
        
        # 🧹 Okamžitá invalidácia cache pre web
        cache.add('web_cache_version', 1, None)
        try: cache.incr('web_cache_version')
        except ValueError: cache.set('web_cache_version', 2, None)
        
        stav = "ZOBRAZENÉ" if zobrazit else "SKRYTÉ"
        return json.dumps({"status": "success", "informacia": f"Záznam {obj} bol úspešne nastavený ako {stav} na verejnom webe."})
        
    except ObjectDoesNotExist:
        return json.dumps({"error": f"Záznam s ID {id_entity} sa nenašiel."})
    except Exception as e:
        return json.dumps({"error": f"Chyba pri úprave: {str(e)}"})


def tool_najdi_a_nastav_ilustracny_obrazok(typ_bloku: str, hladany_vyraz: str, cielova_verzia: Optional[str] = 'DRAFT') -> str:
    """
    Použi na automatické stiahnutie a nastavenie ilustračného obrázka pre konkrétny webový blok.
    'typ_bloku' je napr. 'HERO', 'ABOUT', 'SERVICES'.
    'hladany_vyraz' je anglické kľúčové slovo pre fotobanku (napr. 'technology', 'business', 'service').
    'cielova_verzia' je 'DRAFT' (predvolené pre bezpečný návrh) alebo 'LIVE'.
    """
    try:
        WebBlok = apps.get_model('WebBuilder', 'WebBlok')
        POVOLENE_BLOKY = {
            'HERO', 'ABOUT', 'SERVICES', 'RENTAL', 'PARTS', 'BRANDS', 'REFERENCES',
            'TEAM', 'TICKET', 'ENGINEERING_HUB', 'CONTACT', 'IOT_DEMO',
            'CAREER', 'SLA_CALCULATOR', 'BUYBACK', 'WARRANTY_CLAIM', 'BOOKING', 'FAQ', 'BLOG_TEASER'
        }
        typ_bloku_upper = (typ_bloku or '').strip().upper()
        if typ_bloku_upper not in POVOLENE_BLOKY:
            return json.dumps({"error": f"Nepovolený typ bloku '{typ_bloku}'."})
        verzia_filter = (cielova_verzia or 'DRAFT').upper()
        
        # 🛡️ DRAFT-FIRST STRATÉGIA: Primárne hľadáme v DRAFTE, s bezpečným fallbackom na LIVE
        blok = WebBlok.objects.filter(typ=typ_bloku_upper, verzia=verzia_filter).first()
        if not blok and verzia_filter == 'DRAFT':
            # Ak draft blok ešte neexistuje, vytvoríme ho klonovaním z LIVE alebo nanovo
            live_blok = WebBlok.objects.filter(typ=typ_bloku_upper, verzia='LIVE').first()
            if live_blok:
                blok = WebBlok.objects.create(
                    typ=live_blok.typ, verzia='DRAFT', poradie=live_blok.poradie,
                    je_aktivny=live_blok.je_aktivny, nadpis=live_blok.nadpis,
                    podnadpis=live_blok.podnadpis, tlacidlo_text=live_blok.tlacidlo_text
                )
            else:
                blok = WebBlok.objects.create(typ=typ_bloku_upper, verzia='DRAFT', poradie=10, je_aktivny=True)
        elif not blok:
            return json.dumps({"error": f"Blok typu {typ_bloku} vo verzii {verzia_filter} na webe neexistuje. Najprv ho vytvorte pomocou tool_spravuj_verejny_web."})
            
        # 🛡️ POUŽITIE UNSPLASH API (Vyžaduje kľúč v settings alebo .env, inak použije fallback)
        unsplash_key = getattr(settings, 'UNSPLASH_API_KEY', None) or os.environ.get('UNSPLASH_API_KEY')
        
        safe_query = quote_plus(str(hladany_vyraz).strip())

        if unsplash_key:
            url = f"https://api.unsplash.com/photos/random?query={safe_query}&orientation=landscape&client_id={unsplash_key}"
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                data = response.json()
                img_url = data['urls']['regular']
            else:
                return json.dumps({"error": "Nepodarilo sa nájsť obrázok cez Unsplash API. Skontroluj kľúč alebo limit."})
        else:
            # Fallback na free službu bez API kľúča (Pexels/Unsplash proxy)
            img_url = f"https://image.pollinations.ai/prompt/{safe_query}%20realistic%20photography?width=1920&height=1080&nologo=true"
            
        # Stiahnutie samotného obrázka (s ochranou proti redirectom na internú sieť, SVG/ne-obrázkom a OOM)
        MAX_IMG_BYTES = 10 * 1024 * 1024
        with requests.get(img_url, timeout=10, stream=True, allow_redirects=False) as img_response:
            if img_response.status_code == 200:
                content_type = (img_response.headers.get('Content-Type') or '').split(';')[0].strip().lower()
                if content_type not in {'image/jpeg', 'image/jpg', 'image/png', 'image/webp'}:
                    return json.dumps({"error": f"Vzdialený server nevrátil podporovaný rastrový obrázok (Content-Type: {content_type or 'neznámy'})."})

                chunks = []
                total_bytes = 0
                for chunk in img_response.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    total_bytes += len(chunk)
                    if total_bytes > MAX_IMG_BYTES:
                        return json.dumps({"error": "Sťahovaný obrázok prekračuje maximálny limit 10 MB."})
                    chunks.append(chunk)

                raw_img_bytes = b"".join(chunks)
                try:
                    from io import BytesIO
                    from PIL import Image
                    with Image.open(BytesIO(raw_img_bytes)) as verified_img:
                        verified_img.verify()
                except Exception:
                    return json.dumps({"error": "Stiahnutý súbor nie je validný rastrový obrázok."})

                # Uloženie do modelu (Django ContentFile sa postará o fyzické uloženie)
                cisty_vyraz = re.sub(r'[^a-zA-Z0-9_]', '', str(hladany_vyraz).strip().replace(' ', '_'))[:40]
                nazov_suboru = f"ai_generated_{typ_bloku_upper.lower()}_{cisty_vyraz}.jpg"
                blok.obrazok.save(nazov_suboru, ContentFile(raw_img_bytes), save=True)
            else:
                return json.dumps({"error": f"Nepodarilo sa stiahnuť fyzický súbor obrázka (HTTP {img_response.status_code})."})

        # 🧹 Okamžitá invalidácia cache
        cache.add('web_cache_version', 1, None)
        try: cache.incr('web_cache_version')
        except ValueError: cache.set('web_cache_version', 2, None)

        return json.dumps({"status": "success", "informacia": f"Obrázok pre blok {typ_bloku} bol úspešne stiahnutý a nastavený."})
            
    except Exception as e:
        return json.dumps({"error": f"Kritická chyba pri sťahovaní obrázka: {str(e)}"})

def tool_publikuj_draft_webu() -> str:
    """
    Použi VÝHRADNE vtedy, keď šéf povie, že schvaľuje návrh webu (Draft) a chce ho zverejniť.
    Tento nástroj preklopí DRAFT verziu na LIVE verziu.
    """
    try:
        WebNastavenia = apps.get_model('WebBuilder', 'WebNastavenia')
        WebBlok = apps.get_model('WebBuilder', 'WebBlok')
    except LookupError:
        return json.dumps({"error": "Modely pre web nie sú dostupné."})
        
    try:
        with transaction.atomic():
            draft_nastavenia = WebNastavenia.objects.filter(pk=2).first()
            draft_bloky = WebBlok.objects.filter(verzia='DRAFT')
            
            if not draft_nastavenia and not draft_bloky.exists():
                return json.dumps({"error": "Neexistuje žiadny Draft na publikovanie."})
                
            # 1. Prepis nastavení
            if draft_nastavenia:
                live_nastavenia = WebNastavenia.objects.filter(pk=1).first()
                if not live_nastavenia:
                    live_nastavenia = WebNastavenia(pk=1)
                    
                live_nastavenia.domena = draft_nastavenia.domena
                live_nastavenia.titulok_webu = draft_nastavenia.titulok_webu
                live_nastavenia.hlavna_farba = draft_nastavenia.hlavna_farba
                live_nastavenia.meta_popis = draft_nastavenia.meta_popis
                live_nastavenia.zobrazit_live_sla = draft_nastavenia.zobrazit_live_sla
                live_nastavenia.sla_text_optimalna = getattr(draft_nastavenia, 'sla_text_optimalna', None)
                live_nastavenia.sla_text_zvysena = getattr(draft_nastavenia, 'sla_text_zvysena', None)
                live_nastavenia.sla_text_kriticka = getattr(draft_nastavenia, 'sla_text_kriticka', None)
                if draft_nastavenia.logo:
                    live_nastavenia.logo = draft_nastavenia.logo
                live_nastavenia.save()
                
            # 2. Prepis blokov
            if draft_bloky.exists():
                # Zmažeme staré LIVE bloky
                WebBlok.objects.filter(verzia='LIVE').delete()
                # Zmeníme DRAFT na LIVE
                draft_bloky.update(verzia='LIVE')
                
            cache.add('web_cache_version', 1, None)
            try: cache.incr('web_cache_version')
            except ValueError: cache.set('web_cache_version', 2, None)
            return json.dumps({"status": "success", "informacia": "Draft bol úspešne publikovaný! Zákazníci teraz vidia novú verziu webu."})
            
    except Exception as e:
        return json.dumps({"error": f"Chyba pri publikovaní: {str(e)}"})

def tool_zahod_draft_webu() -> str:
    """
    Použi VÝHRADNE vtedy, keď šéf povie, že sa mu návrh webu (Draft) nepáči a chce ho zmazať.
    Tento nástroj bezpečne odstráni DRAFT bloky a zosynchronizuje draft nastavenia späť s LIVE verziou.
    """    
    try:
        WebNastavenia = apps.get_model('WebBuilder', 'WebNastavenia')
        WebBlok = apps.get_model('WebBuilder', 'WebBlok')
    except LookupError:
        return json.dumps({"error": "Modely pre web nie sú dostupné."})
        
    try:
        with transaction.atomic():
            zmazane_bloky, _ = WebBlok.objects.filter(verzia='DRAFT').delete()
            
            # 🛡️ SINGLETON FAIL-SAFE: Namiesto .delete() na WebNastavenia synchronizujeme PK=2 s PK=1
            live_nast = WebNastavenia.objects.filter(pk=1).first()
            draft_nast = WebNastavenia.objects.filter(pk=2).first()
            
            if draft_nast and live_nast:
                draft_nast.domena = live_nast.domena
                draft_nast.titulok_webu = live_nast.titulok_webu
                draft_nast.hlavna_farba = live_nast.hlavna_farba
                draft_nast.meta_popis = live_nast.meta_popis
                draft_nast.zobrazit_live_sla = live_nast.zobrazit_live_sla
                draft_nast.sla_text_optimalna = getattr(live_nast, 'sla_text_optimalna', None)
                draft_nast.sla_text_zvysena = getattr(live_nast, 'sla_text_zvysena', None)
                draft_nast.sla_text_kriticka = getattr(live_nast, 'sla_text_kriticka', None)
                draft_nast.logo = live_nast.logo
                draft_nast.save()
            
            if zmazane_bloky == 0 and not draft_nast:
                return json.dumps({"informacia": "Žiadny rozpracovaný Draft momentálne neexistuje. Nie je čo zmazať."})
                
            cache.add('web_cache_version', 1, None)
            try: cache.incr('web_cache_version')
            except ValueError: cache.set('web_cache_version', 2, None)
            return json.dumps({"status": "success", "informacia": f"Návrh webu (Draft) bol úspešne zmazaný ({zmazane_bloky} blokov odstránených). Web sa vrátil do LIVE stavu."})
            
    except Exception as e:
        return json.dumps({"error": f"Chyba pri resetovaní draftu: {str(e)}"})


def tool_vylepsi_texty_v_erp(typ_entity: str, id_entity: int, novy_marketingovy_text: str, nazov_skladoveho_modelu: Optional[str] = None) -> str:
    """
    Použi ako AI Copywriter. Slúži na prepísanie suchých textov z ERP na pútavé marketingové texty pre web.
    'typ_entity' musí byť 'SLUZBA', 'REFERENCIA' alebo 'PRODUKT_ESHOP'.
    'id_entity' je ID danej služby, servisného záznamu alebo skladového produktu.
    'novy_marketingovy_text' je profesionálny predajný text.
    'nazov_skladoveho_modelu' je voliteľný názov modelu pre e-shop (napr. názov konkrétneho skladového modelu produktu).
    """
    typ_clean = (typ_entity or '').upper().strip()
    
    if typ_clean in ['SLUZBA', 'SLUŽBA']:
        app_label, model_name, field_name = 'Evidencia', 'TypServisuPrace', 'popis_na_webe'
    elif typ_clean in ['REFERENCIA', 'SERVISNY_ZAZNAM']:
        app_label, model_name, field_name = 'Evidencia', 'ServisnyZaznam', 'verejny_popis_referencie'
    elif typ_clean in ['PRODUKT_ESHOP', 'PRODUKT', 'DIEL', 'SKLAD_KARTA']:
        # 🛒 E-SHOP COPYWRITING: Vyhľadanie skladového dielu
        najdeny_obj = None
        if nazov_skladoveho_modelu:
            skladove_modely_map = {m.__name__: m for subcats in CATEGORY_MAP.values() for m in subcats.values()}
            Model = skladove_modely_map.get(nazov_skladoveho_modelu)
            if Model:
                najdeny_obj = Model.objects.filter(pk=id_entity).first()
        
        if not najdeny_obj:
            for main_cat, subcats in CATEGORY_MAP.items():
                if najdeny_obj: break
                for subcat_name, m_cls in subcats.items():
                    if hasattr(m_cls, 'popis_na_webe'):
                        item = m_cls.objects.filter(pk=id_entity).first()
                        if item:
                            najdeny_obj = item
                            break
                            
        if not najdeny_obj:
            return json.dumps({"error": f"Skladový produkt s ID {id_entity} sa nenašiel pre úpravu popisu."})
            
        najdeny_obj.popis_na_webe = str(novy_marketingovy_text).strip()[:4000]
        najdeny_obj.save(update_fields=['popis_na_webe'])
        cache.add('web_cache_version', 1, None)
        try: cache.incr('web_cache_version')
        except ValueError: cache.set('web_cache_version', 2, None)
        return json.dumps({"status": "success", "informacia": f"E-shopový popis pre produkt '{najdeny_obj}' (ID: {id_entity}) bol úspešne aktualizovaný."})
    else:
        return json.dumps({"error": f"Neznámy typ_entity '{typ_entity}'. Povolené sú: 'SLUZBA', 'REFERENCIA', 'PRODUKT_ESHOP'."})
        
    try:
        Model = apps.get_model(app_label, model_name)
        obj = Model.objects.get(pk=id_entity)
        
        # 🛡️ Ochrana dĺžky textu (databázové limity: verejny_popis_referencie je CharField(max_length=255))
        max_limit = 255 if field_name == 'verejny_popis_referencie' else 2000
        bezpecny_text = str(novy_marketingovy_text).strip()[:max_limit] 
        
        setattr(obj, field_name, bezpecny_text)
        obj.save(update_fields=[field_name])
        
        # 🧹 Okamžitá invalidácia cache pre web
        cache.add('web_cache_version', 1, None)
        try: cache.incr('web_cache_version')
        except ValueError: cache.set('web_cache_version', 2, None)
        
        return json.dumps({"status": "success", "informacia": f"Marketingový text pre {typ_entity} (ID: {id_entity}) bol úspešne aktualizovaný."})
        
    except ObjectDoesNotExist:
        return json.dumps({"error": f"Záznam s ID {id_entity} sa nenašiel."})
    except Exception as e:
        return json.dumps({"error": f"Chyba pri ukladaní textu: {str(e)}"})

def tool_spravuj_clanky_a_blog(akcia: str, clanok_id: Optional[int] = None, nadpis: Optional[str] = None, kategoria: Optional[str] = None, autor_id: Optional[int] = None, perex: Optional[str] = None, obsah_html: Optional[str] = None, seo_titulok: Optional[str] = None, meta_popis: Optional[str] = None, obrazok_alt: Optional[str] = None, datum_vydania: Optional[str] = None, je_aktivny: Optional[bool] = True) -> str:
    """
    Použi na VYTVÁRANIE, ÚPRAVU alebo ZMAZANIE odborných článkov a blogov na verejnom webe.
    'akcia' musí byť jedna z: 'VYTVORIT', 'UPRAVIT', 'ZMAZAT', 'DEAKTIVOVAT'.
    'kategoria' musí byť jedna z: 'NOVINKY', 'NAVODY', 'TECHNOLOGIE', 'PRIPADOVE_STUDIE'.
    'autor_id' je voliteľné ID zamestnanca (technika/konateľa) z ERP.
    'datum_vydania' je dátum publikácie vo formáte YYYY-MM-DD (ak nezadáte, použije sa dnešok). Budúci dátum článok naplánuje.
    """
    try:
        Clanok = apps.get_model('WebBuilder', 'Clanok')
        Zamestnanec = apps.get_model('Evidencia', 'Zamestnanec')
        from WebBuilder.utils_helper import sanitizuj_verejne_html
    except LookupError:
        return json.dumps({"error": "Model pre články nie je v systéme dostupný."})

    POVOLENE_KATEGORIE = ['NOVINKY', 'NAVODY', 'TECHNOLOGIE', 'PRIPADOVE_STUDIE']

    try:
        with transaction.atomic():
            if akcia == 'VYTVORIT':
                if not nadpis or not perex or not obsah_html:
                    return json.dumps({"error": "Pre vytvorenie článku musíš vyplniť: nadpis, perex a obsah_html."})

                kat = kategoria.upper() if kategoria and kategoria.upper() in POVOLENE_KATEGORIE else 'NAVODY'
                autor = None
                if autor_id:
                    autor = Zamestnanec.objects.filter(pk=autor_id).first()

                # 📅 SPRÁVA DÁTUMU VYDANIA (Live vs Plánované publikovanie)
                pub_date = to_date(datum_vydania) or timezone.now().date()

                novy_clanok = Clanok.objects.create(
                    nadpis=str(nadpis).strip()[:255],
                    kategoria=kat,
                    autor=autor,
                    perex=str(perex).strip(),
                    obsah=sanitizuj_verejne_html(str(obsah_html).strip()),
                    seo_titulok=str(seo_titulok).strip()[:60] if seo_titulok else None,
                    meta_popis=str(meta_popis).strip()[:160] if meta_popis else None,
                    obrazok_alt=str(obrazok_alt).strip()[:150] if obrazok_alt else None,
                    datum_vydania=pub_date,
                    je_aktivny=je_aktivny if je_aktivny is not None else True
                )
                cache.add('web_cache_version', 1, None)
                try: cache.incr('web_cache_version')
                except ValueError: cache.set('web_cache_version', 2, None)
                
                stav_str = "naplánovaný na " + pub_date.strftime('%d.%m.%Y') if pub_date > timezone.now().date() else "publikovaný na blogu"
                return json.dumps({
                    "status": "success",
                    "clanok_id": novy_clanok.id,
                    "slug": novy_clanok.slug,
                    "datum_vydania": pub_date.strftime('%d.%m.%Y'),
                    "informacia": f"Článok '{novy_clanok.nadpis}' bol úspešne {stav_str}."
                })

            elif akcia == 'UPRAVIT':
                if not clanok_id:
                    return json.dumps({"error": "Pre úpravu článku musíš zadať clanok_id."})

                clanok = Clanok.objects.select_related('autor').filter(pk=clanok_id).first()
                if not clanok:
                    return json.dumps({"error": f"Článok s ID {clanok_id} sa nenašiel."})

                if nadpis is not None: clanok.nadpis = str(nadpis).strip()[:255]
                if kategoria is not None and kategoria.upper() in POVOLENE_KATEGORIE: clanok.kategoria = kategoria.upper()
                if perex is not None: clanok.perex = str(perex).strip()
                if obsah_html is not None: clanok.obsah = sanitizuj_verejne_html(str(obsah_html).strip())
                if seo_titulok is not None: clanok.seo_titulok = str(seo_titulok).strip()[:60]
                if meta_popis is not None: clanok.meta_popis = str(meta_popis).strip()[:160]
                if obrazok_alt is not None: clanok.obrazok_alt = str(obrazok_alt).strip()[:150]
                if je_aktivny is not None: clanok.je_aktivny = je_aktivny
                if autor_id is not None:
                    clanok.autor = Zamestnanec.objects.filter(pk=autor_id).first()
                if datum_vydania is not None and str(datum_vydania).strip() and str(datum_vydania).strip().lower() not in ['null', 'none']:
                    parsed_pub_date = to_date(datum_vydania)
                    if parsed_pub_date:
                        clanok.datum_vydania = parsed_pub_date

                clanok.save()
                cache.add('web_cache_version', 1, None)
                try: cache.incr('web_cache_version')
                except ValueError: cache.set('web_cache_version', 2, None)
                return json.dumps({
                    "status": "success",
                    "clanok_id": clanok.id,
                    "slug": clanok.slug,
                    "informacia": f"Článok ID {clanok_id} bol úspešne upravený."
                })

            elif akcia in ['ZMAZAT', 'DEAKTIVOVAT']:
                if not clanok_id:
                    return json.dumps({"error": "Pre zmazanie alebo deaktiváciu musíš zadať clanok_id."})

                clanok = Clanok.objects.filter(pk=clanok_id).first()
                if not clanok:
                    return json.dumps({"error": f"Článok s ID {clanok_id} sa nenašiel."})

                if akcia == 'ZMAZAT':
                    clanok.delete()
                    sprava = f"Článok ID {clanok_id} bol trvalo vymazaný."
                else:
                    clanok.je_aktivny = False
                    clanok.save(update_fields=['je_aktivny'])
                    sprava = f"Článok ID {clanok_id} bol deaktivovaný (skrytý z webu)."

                cache.add('web_cache_version', 1, None)
                try: cache.incr('web_cache_version')
                except ValueError: cache.set('web_cache_version', 2, None)
                return json.dumps({"status": "success", "informacia": sprava})

            else:
                return json.dumps({"error": f"Neznáma akcia: {akcia}. Povolené: VYTVORIT, UPRAVIT, ZMAZAT, DEAKTIVOVAT."})

    except Exception as e:
        return json.dumps({"error": f"Kritická chyba pri správe článku: {str(e)}"})


def tool_spravuj_faq(akcia: str, faq_id: Optional[int] = None, kategoria: Optional[str] = None, otazka: Optional[str] = None, odpoved_html: Optional[str] = None, poradie: Optional[int] = 10, je_aktivna: Optional[bool] = True) -> str:
    """
    Použi na PRIDÁVANIE, ÚPRAVU alebo MAZANIE Často kladených otázok (FAQ) na verejnom webe.
    'akcia' musí byť jedna z: 'VYTVORIT', 'UPRAVIT', 'ZMAZAT', 'DEAKTIVOVAT'.
    'kategoria' musí byť jedna z: 'VSEOBECNE', 'SERVIS', 'OBCHOD', 'ZARUKA', 'TECHNOLOGIA', 'KARIERA'.
    """
    try:
        WebFAQ = apps.get_model('WebBuilder', 'WebFAQ')
        from WebBuilder.utils_helper import sanitizuj_verejne_html
    except LookupError:
        return json.dumps({"error": "Model WebFAQ nie je v systéme dostupný."})

    POVOLENE_KATEGORIE = ['VSEOBECNE', 'SERVIS', 'OBCHOD', 'ZARUKA', 'TECHNOLOGIA', 'KARIERA']

    try:
        with transaction.atomic():
            if akcia == 'VYTVORIT':
                if not otazka or not odpoved_html:
                    return json.dumps({"error": "Pre vytvorenie FAQ musíš zadať: otazka a odpoved_html."})

                kat = kategoria.upper() if kategoria and kategoria.upper() in POVOLENE_KATEGORIE else 'VSEOBECNE'
                try: safe_poradie = int(poradie) if poradie is not None else 10
                except (ValueError, TypeError): safe_poradie = 10

                nova_faq = WebFAQ.objects.create(
                    kategoria=kat,
                    otazka=str(otazka).strip()[:255],
                    odpoved=sanitizuj_verejne_html(str(odpoved_html).strip()),
                    poradie=safe_poradie,
                    je_aktivna=je_aktivna if je_aktivna is not None else True
                )
                cache.add('web_cache_version', 1, None)
                try: cache.incr('web_cache_version')
                except ValueError: cache.set('web_cache_version', 2, None)
                return json.dumps({
                    "status": "success",
                    "faq_id": nova_faq.id,
                    "kategoria": nova_faq.get_kategoria_display(),
                    "informacia": f"FAQ položka '{nova_faq.otazka}' bola úspešne pridaná na web."
                })

            elif akcia == 'UPRAVIT':
                if not faq_id:
                    return json.dumps({"error": "Pre úpravu FAQ musíš zadať faq_id."})

                faq = WebFAQ.objects.filter(pk=faq_id).first()
                if not faq:
                    return json.dumps({"error": f"FAQ položka s ID {faq_id} sa nenašla."})

                if kategoria is not None and kategoria.upper() in POVOLENE_KATEGORIE: faq.kategoria = kategoria.upper()
                if otazka is not None: faq.otazka = str(otazka).strip()[:255]
                if odpoved_html is not None: faq.odpoved = sanitizuj_verejne_html(str(odpoved_html).strip())
                if poradie is not None:
                    try: faq.poradie = int(poradie)
                    except (ValueError, TypeError): pass
                if je_aktivna is not None: faq.je_aktivna = je_aktivna

                faq.save()
                cache.add('web_cache_version', 1, None)
                try: cache.incr('web_cache_version')
                except ValueError: cache.set('web_cache_version', 2, None)
                return json.dumps({"status": "success", "faq_id": faq.id, "informacia": f"FAQ položka ID {faq_id} bola upravená."})

            elif akcia in ['ZMAZAT', 'DEAKTIVOVAT']:
                if not faq_id:
                    return json.dumps({"error": "Pre zmazanie alebo deaktiváciu musíš zadať faq_id."})

                faq = WebFAQ.objects.filter(pk=faq_id).first()
                if not faq:
                    return json.dumps({"error": f"FAQ položka s ID {faq_id} sa nenašla."})

                if akcia == 'ZMAZAT':
                    faq.delete()
                    sprava = f"FAQ položka ID {faq_id} bola trvalo vymazaná."
                else:
                    faq.je_aktivna = False
                    faq.save(update_fields=['je_aktivna'])
                    sprava = f"FAQ položka ID {faq_id} bola deaktivovaná."

                cache.add('web_cache_version', 1, None)
                try: cache.incr('web_cache_version')
                except ValueError: cache.set('web_cache_version', 2, None)
                return json.dumps({"status": "success", "informacia": sprava})

            else:
                return json.dumps({"error": f"Neznáma akcia: {akcia}. Povolené: VYTVORIT, UPRAVIT, ZMAZAT, DEAKTIVOVAT."})

    except Exception as e:
        return json.dumps({"error": f"Kritická chyba pri správe FAQ: {str(e)}"})


def tool_vytvor_pripadovu_studiu_z_vyjazdu(zaznam_id: int, verejny_marketingovy_titulok: str, verejny_popis_pribehu: str, vytvorit_aj_blog_clanok: bool = False, anonymizovat_klienta: bool = True) -> str:
    """
    Použi na PREMENU ukončeného servisného zásahu (ServisnyZaznam) na špičkovú marketingovú referenciu pre web.
    'zaznam_id' je ID dokončeného servisného záznamu.
    'verejny_marketingovy_titulok' je pútavý nadpis pre klientov (napr. 'Komplexný servis a optimalizácia prevádzky kľúčového zariadenia').
    'verejny_popis_pribehu' je B2B príbeh v štýle: Problém klienta -> Naša diagnostika -> Rýchle riešenie -> Výsledná úspora.
    'vytvorit_aj_blog_clanok' ak je True, automaticky vytvorí aj hĺbkový článok do sekcie Blog.
    """
    try:
        ServisnyZaznam = apps.get_model('Evidencia', 'ServisnyZaznam')
        Clanok = apps.get_model('WebBuilder', 'Clanok')
    except LookupError:
        return json.dumps({"error": "Potrebné modely nie sú v systéme dostupné."})

    try:
        with transaction.atomic():
            zaznam = ServisnyZaznam.objects.select_related('zakaznik', 'zariadenie__vyrobca').prefetch_related('pouzite_polozky_zaznamu', 'typ_servisu', 'technici').filter(pk=zaznam_id).first()
            if not zaznam:
                return json.dumps({"error": f"Servisný záznam s ID {zaznam_id} neexistuje."})

            # 🛡️ VALIDÁCIA MÉDIÍ PRE VEREJNÝ WEB (Vyžaduje aspoň jedno: foto PO, foto PRED alebo video)
            if not zaznam.foto_po and not zaznam.foto_pred and not zaznam.video:
                return json.dumps({
                    "error": "Servisnému záznamu chýba akékoľvek vizuálne médium (foto PO, foto PRED alebo video). Verejný web vyžaduje aspoň jedno médium pre zaradenie do referencií a sitemapy."
                })

            cisty_popis = str(verejny_popis_pribehu).strip()[:2000]
            zaznam.verejny_popis_referencie = str(verejny_marketingovy_titulok).strip()[:255]
            zaznam.zobrazit_na_webe = True

            zaznam.save(update_fields=['verejny_popis_referencie', 'zobrazit_na_webe'])
            
            # 🛡️ E-E-A-T KONTROLA AUTORA BLOGU (Zamestnanec musí mať zobrazit_na_webe=True)
            vytvoreny_clanok_id = None
            if vytvorit_aj_blog_clanok:
                prvy_technik = zaznam.technici.filter(zobrazit_na_webe=True).first() or zaznam.technici.first()
                ma_suhlas_klienta = bool(zaznam.zakaznik and getattr(zaznam.zakaznik, 'zobrazit_ako_referenciu', False))
                klient_str = (
                    zaznam.zakaznik.Nazov_spolocnosti
                    if (zaznam.zakaznik is not None and not anonymizovat_klienta and ma_suhlas_klienta)
                    else "Významný obchodný partner"
                )
                stroj_str = str(zaznam.zariadenie) if zaznam.zariadenie else get_industry_term('zariadenie_fallback_nazov', 'Zariadenie')
                diely = ", ".join([f"{p.pocet}x {p.seriove_cislo_text}" for p in zaznam.pouzite_polozky_zaznamu.all()]) or f"Originálne {get_industry_term('polozka_fallback_nazov', 'položky')}"

                blog_obsah = f"""
                <p class="lead">Úspešná servisná realizácia pre partnera: <strong>{escape(klient_str)}</strong> na zariadení <strong>{escape(stroj_str)}</strong>.</p>
                <h3>Popis technického stavu a výzvy</h3>
                <p>{escape(zaznam.poruchy or 'Zariadenie vykazovalo prevádzkové anomálie vyžadujúce odborný zásah.')}</p>
                <h3>Realizované servisné práce</h3>
                <p>{escape(cisty_popis)}</p>
                <h3>Použitý materiál a komponenty</h3>
                <p>{escape(diely)}</p>
                <h3>Výsledok a prínos pre prevádzku</h3>
                <p>{escape(zaznam.formalne_zhodnotenie_ai or 'Zariadenie bolo po dôkladnom otestovaní uvedené do plnej prevádzky v nominálnych parametroch.')}</p>
                """

                novy_clanok = Clanok.objects.create(
                    nadpis=str(verejny_marketingovy_titulok).strip()[:255],
                    kategoria='PRIPADOVE_STUDIE',
                    autor=prvy_technik,
                    perex=cisty_popis[:250] + "...",
                    obsah=blog_obsah.strip(),
                    seo_titulok=str(verejny_marketingovy_titulok)[:60],
                    meta_popis=cisty_popis[:160],
                    je_aktivny=True
                )
                vytvoreny_clanok_id = novy_clanok.id

            cache.add('web_cache_version', 1, None)
            try: cache.incr('web_cache_version')
            except ValueError: cache.set('web_cache_version', 2, None)
            return json.dumps({
                "status": "success",
                "zaznam_id": zaznam.id,
                "zobrazene_v_sekcii_referencie": True,
                "vytvoreny_blog_clanok_id": vytvoreny_clanok_id,
                "informacia": f"Servisný výjazd bol úspešne premenený na verejnú referenciu{' a publikovaný ako blogový článok' if vytvoreny_clanok_id else ''}."
            })

    except Exception as e:
        return json.dumps({"error": f"Kritická chyba pri generovaní prípadovej štúdie: {str(e)}"})


def tool_spravuj_pracovne_pozicie(akcia: str, pozicia_id: Optional[int] = None, nazov: Optional[str] = None, typ_uvazku: Optional[str] = 'TPP', cielova_rola: Optional[str] = 'TE', mzda: Optional[str] = None, lokalita: Optional[str] = None, nastup: Optional[str] = 'Ihneď', poziadavky_skratka: Optional[str] = None, benefity_skratka: Optional[str] = None, popis_naplne: Optional[str] = None, poziadavky_detail: Optional[str] = None, ponukame_detail: Optional[str] = None, platnost_do: Optional[str] = None, je_aktivna: Optional[bool] = True) -> str:
    """
    Použi na VYTVÁRANIE, ÚPRAVU alebo SPRÁVU pracovných inzerátov a náboru (Kariéra na webe).
    'akcia' musí byť jedna z: 'VYTVORIT', 'UPRAVIT', 'DEAKTIVOVAT', 'ZMAZAT'.
    'typ_uvazku' musí byť jedno z: 'TPP', 'ZIVNOST', 'DOHODA', 'DOHODOU'.
    'cielova_rola' je systémová rola v ERP: 'TE' (Technik), 'DI' (Dispečer), 'SK' (Skladník), 'AD' (Administratíva), 'OB' (Obchodník).
    'platnost_do' zadávaj vo formáte YYYY-MM-DD.
    """
    try:
        PracovnaPozicia = apps.get_model('Evidencia', 'PracovnaPozicia')
    except LookupError:
        return json.dumps({"error": "Model PracovnaPozicia nie je v systéme dostupný."})

    POVOLENE_UVAZKY = ['TPP', 'ZIVNOST', 'DOHODA', 'DOHODOU']
    POVOLENE_ROLY = ['TE', 'DI', 'SK', 'AD', 'OB']

    try:
        with transaction.atomic():
            if akcia == 'VYTVORIT':
                if not nazov or not mzda or not lokalita:
                    return json.dumps({"error": "Pre vytvorenie inzerátu musíš zadať: nazov, mzda, lokalita."})

                uvazok = typ_uvazku.upper() if typ_uvazku and typ_uvazku.upper() in POVOLENE_UVAZKY else 'TPP'
                rola = cielova_rola.upper() if cielova_rola and cielova_rola.upper() in POVOLENE_ROLY else 'TE'

                d_platnost = to_date(platnost_do)

                FALLBACK_TEXTY_PODLA_ROLY = {
                    'TE': {
                        'poziadavky_skratka': "Technické vzdelanie, VP sk. B",
                        'benefity_skratka': "Služobné vozidlo, moderné náradie, bonusy",
                        'popis_naplne': "Servis a diagnostika zariadení u zákazníkov.",
                        'poziadavky_detail': "Samostatnosť, zodpovednosť, chuť učiť sa novým technológiám.",
                        'ponukame_detail': "Stabilné zázemie, férové odmeňovanie, odborné školenia.",
                    },
                    'DI': {
                        'poziadavky_skratka': "Organizačné schopnosti, komunikácia s klientmi, práca s PC",
                        'benefity_skratka': "Moderné dispečerské zázemie, stravné príspevky, výkonnostné bonusy",
                        'popis_naplne': "Plánovanie a koordinácia servisných výjazdov, komunikácia so zákazníkmi a podpora tímu v teréne.",
                        'poziadavky_detail': "Logické myslenie, odolnosť voči stresu, samostatnosť a profesionálna komunikácia.",
                        'ponukame_detail': "Stabilné zázemie, férové odmeňovanie, odborné zaškolenie do systémov a priateľský kolektív.",
                    },
                    'SK': {
                        'poziadavky_skratka': "Precíznosť, zodpovednosť, skúsenosti so skladovým hospodárstvom",
                        'benefity_skratka': "Stabilný pracovný čas, moderný digitálny sklad, výkonnostné bonusy",
                        'popis_naplne': "Príjem, výdaj a evidencia náhradných dielov a technológií, príprava materiálu pre servisných technikov a inventúry.",
                        'poziadavky_detail': "Spoľahlivosť, zmysel pre poriadok, základná práca s PC a skladovým systémom.",
                        'ponukame_detail': "Stabilné zázemie servisno-technologickej firmy, férové odmeňovanie, pracovné pomôcky a kompletné zaškolenie.",
                    },
                    'AD': {
                        'poziadavky_skratka': "Skúsenosti s administratívou, fakturáciou, práca s PC",
                        'benefity_skratka': "Stabilná kancelárska práca, moderné ERP nástroje, príspevky a bonusy",
                        'popis_naplne': "Správa firemnej agendy, podpora fakturácie a účtovníctva, komunikácia s klientmi a dodávateľmi.",
                        'poziadavky_detail': "Dôslednosť, samostatnosť, výborná znalosť kancelárskych nástrojov a organizačné zručnosti.",
                        'ponukame_detail': "Stabilné zázemie, férové odmeňovanie, flexibilita a podpora profesijného rastu.",
                    },
                    'OB': {
                        'poziadavky_skratka': "Prax v B2B obchode, technické myslenie, komunikačné zručnosti, VP sk. B",
                        'benefity_skratka': "Služobné vozidlo, výkonnostné odmeny, telefón a notebook",
                        'popis_naplne': "Akvizícia nových B2B klientov, starostlivosť o existujúcich zákazníkov, obhliadky u klientov, príprava cenových ponúk a uzatváranie servisných a dodávateľských zmlúv.",
                        'poziadavky_detail': "Výborné prezentačné a vyjednávacie schopnosti, samostatnosť, orientácia v technických riešeniach a proklientsky prístup.",
                        'ponukame_detail': "Stabilné zázemie technologicko-servisnej firmy, férové finančné ohodnotenie s bonusmi za výsledky, moderné CRM/ERP nástroje a odbornú podporu technického tímu.",
                    },
                }
                fb = FALLBACK_TEXTY_PODLA_ROLY.get(rola, FALLBACK_TEXTY_PODLA_ROLY['TE'])

                nova_pozicia = PracovnaPozicia.objects.create(
                    nazov=str(nazov).strip()[:150],
                    typ_uvazku=uvazok,
                    cielova_rola=rola,
                    mzda=str(mzda).strip()[:100],
                    lokalita=str(lokalita).strip()[:100],
                    nastup=str(nastup or '').strip()[:100] or "Ihneď",
                    poziadavky_skratka=str(poziadavky_skratka or '').strip()[:255] or fb['poziadavky_skratka'],
                    benefity_skratka=str(benefity_skratka or '').strip()[:255] or fb['benefity_skratka'],
                    popis_naplne=str(popis_naplne or '').strip() or fb['popis_naplne'],
                    poziadavky_detail=str(poziadavky_detail or '').strip() or fb['poziadavky_detail'],
                    ponukame_detail=str(ponukame_detail or '').strip() or fb['ponukame_detail'],
                    platnost_do=d_platnost,
                    je_aktivna=je_aktivna if je_aktivna is not None else True
                )
                cache.add('web_cache_version', 1, None)
                try: cache.incr('web_cache_version')
                except ValueError: cache.set('web_cache_version', 2, None)
                return json.dumps({
                    "status": "success",
                    "pozicia_id": nova_pozicia.id,
                    "slug": nova_pozicia.slug,
                    "cielova_rola_erp": nova_pozicia.get_cielova_rola_display(),
                    "vytvorene": nova_pozicia.vytvorene.strftime('%d.%m.%Y %H:%M') if getattr(nova_pozicia, 'vytvorene', None) else "Neznáme",
                    "informacia": f"Pracovná pozícia '{nova_pozicia.nazov}' ({nova_pozicia.get_cielova_rola_display()}) bola úspešne publikovaná na webe."
                })

            elif akcia == 'UPRAVIT':
                if not pozicia_id:
                    return json.dumps({"error": "Pre úpravu pozície musíš zadať pozicia_id."})

                pozicia = PracovnaPozicia.objects.filter(pk=pozicia_id).first()
                if not pozicia:
                    return json.dumps({"error": f"Pozícia s ID {pozicia_id} sa nenašla."})

                if nazov is not None: pozicia.nazov = str(nazov).strip()[:150]
                if typ_uvazku is not None and typ_uvazku.upper() in POVOLENE_UVAZKY: pozicia.typ_uvazku = typ_uvazku.upper()
                if cielova_rola is not None and cielova_rola.upper() in POVOLENE_ROLY: pozicia.cielova_rola = cielova_rola.upper()
                if mzda is not None: pozicia.mzda = str(mzda).strip()[:100]
                if lokalita is not None: pozicia.lokalita = str(lokalita).strip()[:100]
                if nastup is not None: pozicia.nastup = str(nastup).strip()[:100]
                if poziadavky_skratka is not None: pozicia.poziadavky_skratka = str(poziadavky_skratka).strip()[:255]
                if benefity_skratka is not None: pozicia.benefity_skratka = str(benefity_skratka).strip()[:255]
                if popis_naplne is not None: pozicia.popis_naplne = str(popis_naplne).strip()
                if poziadavky_detail is not None: pozicia.poziadavky_detail = str(poziadavky_detail).strip()
                if ponukame_detail is not None: pozicia.ponukame_detail = str(ponukame_detail).strip()
                if je_aktivna is not None: pozicia.je_aktivna = je_aktivna
                if platnost_do is not None:
                    pozicia.platnost_do = to_date(platnost_do)

                pozicia.save()
                cache.add('web_cache_version', 1, None)
                try: cache.incr('web_cache_version')
                except ValueError: cache.set('web_cache_version', 2, None)
                return json.dumps({"status": "success", "pozicia_id": pozicia.id, "informacia": f"Pracovná pozícia ID {pozicia_id} bola upravená."})

            elif akcia in ['DEAKTIVOVAT', 'ZMAZAT']:
                if not pozicia_id:
                    return json.dumps({"error": "Pre deaktiváciu alebo zmazanie pozície musíš zadať pozicia_id."})

                pozicia = PracovnaPozicia.objects.filter(pk=pozicia_id).first()
                if not pozicia:
                    return json.dumps({"error": f"Pozícia s ID {pozicia_id} sa nenašla."})

                if akcia == 'ZMAZAT':
                    nazov_p = pozicia.nazov
                    pozicia.delete()
                    sprava = f"Pracovná pozícia '{nazov_p}' (ID {pozicia_id}) bola trvalo vymazaná z databázy."
                else:
                    pozicia.je_aktivna = False
                    pozicia.save(update_fields=['je_aktivna'])
                    sprava = f"Pracovná pozícia ID {pozicia_id} bola stiahnutá z webu (deaktivovaná)."

                cache.add('web_cache_version', 1, None)
                try: cache.incr('web_cache_version')
                except ValueError: cache.set('web_cache_version', 2, None)
                return json.dumps({"status": "success", "informacia": sprava})

            else:
                return json.dumps({"error": f"Neznáma akcia: {akcia}. Povolené: VYTVORIT, UPRAVIT, DEAKTIVOVAT, ZMAZAT."})

    except Exception as e:
        return json.dumps({"error": f"Kritická chyba pri správe pracovnej pozície: {str(e)}"})


def tool_priprav_social_media_post(platforma: str, tema_alebo_id_clanku: str, cielova_skupina: Optional[str] = "B2B_KLIENTI") -> str:
    """
    Použi ako OMNICHANNEL AI Copywriter na generovanie pútavých príspevkov na sociálne siete (LinkedIn, Facebook, Instagram).
    'platforma' musí byť: 'LINKEDIN', 'FACEBOOK', 'INSTAGRAM' alebo 'VSETKY'.
    'tema_alebo_id_clanku' môže byť ID existujúceho článku na blogu (napr. '12') alebo voľná téma (napr. 'Nábor nového technika', 'Prečo nezanedbať pravidelný servis zariadení').
    'cielova_skupina' môže byť: 'B2B_KLIENTI', 'UCHADZACI_O_PRACU', 'EXISTUJUCI_ZAKAZNICI'.
    Vráti pripravený formátovaný text vrátane emoji, hashtagov a odkazu na firemný web.
    """
    try:
        Clanok = apps.get_model('WebBuilder', 'Clanok')
        FiremnaIdentita = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
        WebNastavenia = apps.get_model('WebBuilder', 'WebNastavenia')
    except LookupError:
        return json.dumps({"error": "Systémové modely nie sú dostupné."})

    identita = FiremnaIdentita.objects.first()
    web = WebNastavenia.objects.filter(pk=1).first()
    surova_domena = web.domena if web and web.domena else (identita.web if identita and identita.web else getattr(settings, 'PUBLIC_WEB_DOMAIN', ''))
    cista_domena = surova_domena.replace('https://', '').replace('http://', '').strip('/')

    suvisiaci_clanok = None
    if str(tema_alebo_id_clanku).isdigit():
        suvisiaci_clanok = Clanok.objects.filter(pk=int(tema_alebo_id_clanku)).first()

    nazov_firmy = identita.nazov_spolocnosti if identita else "Naša spoločnosť"
    podtitul_firmy = identita.podtitul if identita and identita.podtitul else None
    fb_link = identita.facebook if identita and identita.facebook else None
    li_link = identita.linkedin if identita and identita.linkedin else None
    ig_link = identita.instagram if identita and identita.instagram else None

    return json.dumps({
        "status": "success",
        "nazov_firmy": nazov_firmy,
        "podtitul_firmy": podtitul_firmy or "Nenastavený",
        "firemna_domena": cista_domena,
        "prepojene_profily": {
            "facebook_nastaveny": bool(fb_link),
            "linkedin_nastaveny": bool(li_link),
            "instagram_nastaveny": bool(ig_link)
        },
        "podklad_pre_ai": {
            "platforma": platforma.upper(),
            "cielova_skupina": cielova_skupina,
            "zameranie_firmy": podtitul_firmy or "Všeobecné",
            "tema": suvisiaci_clanok.nadpis if suvisiaci_clanok else tema_alebo_id_clanku,
            "perex_clanku": suvisiaci_clanok.perex if suvisiaci_clanok else None,
            "url_clanku": f"https://{cista_domena}/blog/{suvisiaci_clanok.slug}/" if suvisiaci_clanok and suvisiaci_clanok.slug else f"https://{cista_domena}"
        },
        "instrukcia_pre_ai": "Zostav profesionálny príspevok na zadané sociálne siete. Použi úderný B2B hook v prvej vete, formátovanie odrážkami, relevantné priemyselné hashtagy a priamu výzvu k akcii (CTA) s odkazom na web."
    })

def tool_priprav_koncept_emailu(prijemca_email_alebo_nazov: str, pokyny_alebo_text_spravy: str, predmet: Optional[str] = None, povodny_email_id: Optional[int] = None, vyrobne_cislo_stroja: Optional[str] = None, prilozit_dokument_model: Optional[str] = None, prilozit_dokument_id: Optional[int] = None, prilozit_dokument_pole: Optional[str] = None, je_obchodna_komunikacia: Optional[bool] = False) -> str:
    """
    🧠 ISAS SMART MAIL DRAFTER: Pripraví profesionálny koncept (DRAFT) e-mailu priamo do poštovej schránky majiteľa/dispečera.
    E-mail sa NEODOŠLE klientovi, ale bezpečne uloží do priečinka Koncepty (Drafts) na IMAP serveri cez Celery.
    'prijemca_email_alebo_nazov' je e-mailová adresa alebo názov firmy/osoby z CRM (napr. 'Partner s.r.o.', 'Peter Kováč', 'riaditel@firma.sk').
    'pokyny_alebo_text_spravy' je obsah správy, ktorý chce šéf oznámiť (napr. 'Prídeme v stredu o 8:00 na diagnostiku zariadenia').
    'predmet' je voliteľný predmet e-mailu (ak nezadáte, vygeneruje sa automaticky).
    'povodny_email_id' zadajte, ak ide o odpoveď na existujúci prijatý e-mail z databázy (zachová Re:, In-Reply-To a e-mailové vlákno).
    'vyrobne_cislo_stroja' je voliteľné SN zariadenia, ktorého technický stav, stav počítadla a aktívne alarmy sa majú presne zakomponovať.
    'prilozit_dokument_model' je voliteľný názov modelu pre prílohu (napr. 'CenovaPonuka', 'VystavenaFaktura', 'ServisnyZaznam', 'TechnickaSpecifikacia', 'ServisovaneZariadenie', 'MegaZaloha').
    'prilozit_dokument_id' je voliteľné ID záznamu v databáze.
    'prilozit_dokument_pole' je voliteľný názov poľa súboru/fotografie (napr. 'obrazok_na_webe', 'foto_pred', 'foto_po', 'subor_schema', 'qr_kod'). Podporuje aj viacero polí oddelených čiarkou: 'foto_pred,foto_po'.
    'je_obchodna_komunikacia' nastavte na True vždy, ak ide o obchodnú ponuku, akvizíciu, novinku, zľavu alebo marketingové/promo oslovenie (bez ohľadu na jazyk správy), aby sa automaticky pridala možnosť odhlásenia.
    """
    try:
        EmailovyUcet = apps.get_model('ISASaplikacia', 'EmailovyUcet')
        EmailovaSprava = apps.get_model('ISASaplikacia', 'EmailovaSprava')
        EmailPriloha = apps.get_model('ISASaplikacia', 'EmailPriloha')
        EmailOptOut = apps.get_model('ISASaplikacia', 'EmailOptOut')
        Zakaznik = apps.get_model('Evidencia', 'Zakaznik')
        Dodavatel = apps.get_model('Evidencia', 'Dodavatel')
        SukromnaOsoba = apps.get_model('Evidencia', 'SukromnaOsoba')
        Zamestnanec = apps.get_model('Evidencia', 'Zamestnanec')
        ServisovaneZariadenie = apps.get_model('Evidencia', 'ServisovaneZariadenie')
        ServisnyZaznam = apps.get_model('Evidencia', 'ServisnyZaznam')
        AlarmLog = apps.get_model('Evidencia', 'AlarmLog')
        PlanovanyServisZariadenia = apps.get_model('Evidencia', 'PlanovanyServisZariadenia')
        FiremnaIdentita = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
        GlobalneNastavenia = apps.get_model('ISASaplikacia', 'GlobalneNastavenia')
    except LookupError as err:
        return json.dumps({"error": f"Systémové modely nie sú dostupné: {str(err)}"})

    try:
        to_addr = None
        final_subject = predmet
        crm_entity: Optional[Any] = None
        zak_id = dod_id = zam_id = None
        ucet = None
        in_reply_to_msg_id = ""

        # 🛡️ DEFENSIVE ID PARSING: Bezpečné pretypovanie voliteľných ID
        safe_povodny_mail_id = None
        if povodny_email_id is not None and str(povodny_email_id).strip().lower() not in ['null', 'none', '']:
            try: safe_povodny_mail_id = int(str(povodny_email_id).strip())
            except ValueError: safe_povodny_mail_id = None

        safe_priloha_doc_id = None
        if prilozit_dokument_id is not None and str(prilozit_dokument_id).strip().lower() not in ['null', 'none', '']:
            try: safe_priloha_doc_id = int(str(prilozit_dokument_id).strip())
            except ValueError: safe_priloha_doc_id = None

        je_odpoved_na_email = False

        # 1. Režim A: Odpoveď na existujúci e-mail (100% THREADING INTEGRITA)
        if safe_povodny_mail_id:
            povodny_mail = EmailovaSprava.objects.select_related('ucet', 'zakaznik', 'dodavatel', 'zamestnanec').filter(id=safe_povodny_mail_id).first()
            if povodny_mail:
                je_odpoved_na_email = True
                ucet = povodny_mail.ucet
                in_reply_to_msg_id = povodny_mail.message_id or ""
                to_addr = povodny_mail.odosielatel.split('<')[-1].replace('>', '').strip() if povodny_mail.odosielatel else None

                if not final_subject:
                    cisty_predmet = re.sub(r'(?i)^re:\s*', '', povodny_mail.predmet or '').strip()
                    final_subject = f"Re: {cisty_predmet}" if cisty_predmet else "Re: Servisná požiadavka"
                crm_entity = povodny_mail.zakaznik or povodny_mail.dodavatel or povodny_mail.zamestnanec
                zak_id = povodny_mail.zakaznik_id
                dod_id = povodny_mail.dodavatel_id
                zam_id = povodny_mail.zamestnanec_id

        # 2. Inteligentný výber odosielacej schránky podľa GlobalneNastavenia
        gn = GlobalneNastavenia.objects.first()

        if not ucet:
            aktivne_ucty = list(EmailovyUcet.objects.filter(je_aktivny=True))
            if not aktivne_ucty:
                return json.dumps({"error": "V systéme nie je nakonfigurovaný žiadny aktívny e-mailový účet."})
            
            predmet_text = f"{final_subject or ''} {pokyny_alebo_text_spravy or ''}".lower()
            
            if any(k in predmet_text for k in ['faktúr', 'faktur', 'platb', 'uhrad', 'splatn', 'záloh']):
                cielovy_mail = (gn.email_pre_faktury or '').lower().strip() if gn else ''
                ucet = next((u for u in aktivne_ucty if cielovy_mail and u.email_adresa.lower().strip() == cielovy_mail), None)
                if not ucet:
                    ucet = next((u for u in aktivne_ucty if any(k in u.email_adresa.lower() for k in ['faktur', 'ucto'])), aktivne_ucty[0])

            elif any(k in predmet_text for k in ['reklamác', 'reklamac', 'sťažnos', 'staznos', 'vadn']):
                cielovy_mail = (gn.email_pre_reklamacie or '').lower().strip() if gn else ''
                ucet = next((u for u in aktivne_ucty if cielovy_mail and u.email_adresa.lower().strip() == cielovy_mail), None)
                if not ucet:
                    ucet = next((u for u in aktivne_ucty if 'reklamac' in u.email_adresa.lower()), aktivne_ucty[0])

            else:
                cielovy_mail = (gn.email_pre_objednavky or '').lower().strip() if gn else ''
                ucet = next((u for u in aktivne_ucty if cielovy_mail and u.email_adresa.lower().strip() == cielovy_mail), None)
                if not ucet:
                    ucet = next((u for u in aktivne_ucty if any(k in u.email_adresa.lower() for k in ['servis', 'dispecing', 'objednavk'])), aktivne_ucty[0])

        # 3. Režim B: Nová správa (Cold outreach / CRM partner) s precíznou extrakciou e-mailu
        if not to_addr:
            target_str = (prijemca_email_alebo_nazov or '').strip()
            # Extrakcia čistého e-mailu z formátov ako 'Meno Priezvisko <meno@firma.sk>' alebo priameho e-mailu
            email_match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', target_str)
            if email_match:
                to_addr = email_match.group(0).lower().strip()
                crm_entity = (
                    Zakaznik.objects.filter(je_anonymizovany=False, email__iexact=to_addr).first() or
                    Dodavatel.objects.filter(je_anonymizovany=False, email__iexact=to_addr).first() or
                    SukromnaOsoba.objects.filter(je_anonymizovany=False, email__iexact=to_addr).first() or
                    Zamestnanec.objects.filter(je_anonymizovany=False, email__iexact=to_addr).first()
                )
                if isinstance(crm_entity, Zakaznik): zak_id = crm_entity.id
                elif isinstance(crm_entity, Dodavatel): dod_id = crm_entity.id
                elif isinstance(crm_entity, Zamestnanec): zam_id = crm_entity.id
            else:
                zak = Zakaznik.objects.filter(je_anonymizovany=False).filter(Q(Nazov_spolocnosti__icontains=target_str) | Q(kontaktna_osoba__icontains=target_str)).first()
                if zak and zak.email:
                    to_addr = zak.email
                    crm_entity = zak
                    zak_id = zak.id
                else:
                    dod = Dodavatel.objects.filter(je_anonymizovany=False).filter(Q(Nazov_spolocnosti__icontains=target_str) | Q(kontaktna_osoba__icontains=target_str)).first()
                    if dod and dod.email:
                        to_addr = dod.email
                        crm_entity = dod
                        dod_id = dod.id
                    else:
                        so = SukromnaOsoba.objects.filter(je_anonymizovany=False, meno_a_priezvisko__icontains=target_str).first()
                        if so and so.email:
                            to_addr = so.email
                            crm_entity = so
                        else:
                            zam = Zamestnanec.objects.filter(je_anonymizovany=False).filter(Q(meno__icontains=target_str) | Q(priezvisko__icontains=target_str)).first()
                            if zam and zam.email:
                                to_addr = zam.email
                                crm_entity = zam
                                zam_id = zam.id

        if not to_addr:
            return json.dumps({"error": f"Pre subjekt '{prijemca_email_alebo_nazov}' sa v databáze nenašla žiadna platná e-mailová adresa (alebo je subjekt anonymizovaný)." })

        to_addr = re.sub(r'[\r\n]+', '', str(to_addr)).strip()

        identita = FiremnaIdentita.objects.first()
        firma_nazov = identita.nazov_spolocnosti if identita and identita.nazov_spolocnosti else "Servisné stredisko"

        if not final_subject:
            final_subject = f"Informácia k servisným službám – {firma_nazov}"
        final_subject = re.sub(r'[\r\n]+', ' ', str(final_subject)).strip()[:900]

        # 4. 🧠 DEFENZÍVNA TECHNICKÁ DNA STROJA (3-STUPŇOVÝ MODEL + IOT ALARMY)
        stroj_kontext_html = ""
        stroj = None
        if vyrobne_cislo_stroja:
            stroj = ServisovaneZariadenie.objects.select_related('vyrobca', 'zakaznik').filter(vyrobne_cislo__icontains=vyrobne_cislo_stroja.strip()).first()
        elif zak_id:
            stroje_klienta = list(ServisovaneZariadenie.objects.select_related('vyrobca').filter(zakaznik_id=zak_id))
            if len(stroje_klienta) == 1:
                stroj = stroje_klienta[0]
            elif len(stroje_klienta) > 1:
                hladany_text = f"{pokyny_alebo_text_spravy or ''} {predmet or ''}".lower()
                for s in stroje_klienta:
                    if (s.vyrobne_cislo and s.vyrobne_cislo.lower() in hladany_text) or (s.model_zariadenia and s.model_zariadenia.lower() in hladany_text):
                        stroj = s
                        break

        if stroj:
            now = timezone.now()
            vyrobca_str = stroj.vyrobca.nazov_vyrobcu if stroj.vyrobca else ""
            stroj_nazov = escape(f"{vyrobca_str} {stroj.model_zariadenia} (v.č. {stroj.vyrobne_cislo})".strip())
            lokalita_str = escape(f" [Umiestnenie: {stroj.umiestnenie}]") if stroj.umiestnenie else ""
            
            najblizsi_plan = PlanovanyServisZariadenia.objects.filter(zariadenie=stroj, upozornenie_odoslane=False).select_related('typ_servisu').order_by('planovany_datum').first()
            plan_info = ""
            if najblizsi_plan:
                meter_plan_disp = getattr(najblizsi_plan, 'meter_display', None) or (f"{najblizsi_plan.planovane_motohodiny} {get_industry_term('meter_unit', 'hod.')}" if getattr(najblizsi_plan, 'planovane_motohodiny', None) else "Nezadané")
                termin_str = najblizsi_plan.planovany_datum.strftime('%d.%m.%Y') if najblizsi_plan.planovany_datum else f"pri {meter_plan_disp}"
                plan_info = f"<br>📅 <i>Najbližší plánovaný servis: <b>{escape(najblizsi_plan.typ_servisu.nazov)}</b> (predpoklad: {escape(termin_str)})</i>"

            # Kontrola aktívnych nevyriešených alarmov
            aktivne_alarmy = list(AlarmLog.objects.filter(zariadenie=stroj, vyriesene=False).order_by('-casova_peciatka')[:2])
            alarm_info = ""
            if aktivne_alarmy:
                alarm_popisy = ", ".join([f"<b>{escape(a.kod_chyby)}</b> ({escape(a.popis)})" for a in aktivne_alarmy])
                alarm_info = f"<br>⚠️ <span style='color:#dc2626;'><b>Aktívny telemetrický alarm:</b> {alarm_popisy}</span>"

            # ⚖️ Kontrola zákonných revízií stroja (VTZ a F-plyny)
            compliance_info = ""
            try:
                c_alerty = [c for c in get_zariadenie_compliance_audit(stroj) if c.get('vyzaduje_alert')]
                if c_alerty:
                    c_popisy = ", ".join([f"<b>{escape(c['nazov'])}</b> ({escape(c['stav_str'])})" for c in c_alerty])
                    compliance_info = f"<br>⚖️ <span style='color:#dc2626;'><b>Zákonné revízie a lehoty:</b> {c_popisy}</span>"
            except Exception as e_comp:
                logger.warning(f"Chyba pri načítaní compliance pre mail: {e_comp}")

            last_servis = ServisnyZaznam.objects.filter(zariadenie=stroj, dokoncene=True).order_by('-vytvorene').first()
            datum_str = last_servis.vytvorene.strftime('%d.%m.%Y') if (last_servis and getattr(last_servis, 'vytvorene', None)) else None

            cas_aktualizacie = stroj.posledna_aktualizacia_stavu.replace(tzinfo=None) if (stroj.posledna_aktualizacia_stavu and timezone.is_aware(stroj.posledna_aktualizacia_stavu)) else stroj.posledna_aktualizacia_stavu
            now_naive = now.replace(tzinfo=None) if (now and timezone.is_aware(now)) else now
            stroj_meter_disp = getattr(stroj, 'meter_display', None) or f"{stroj.aktualne_motohodiny} {get_industry_term('meter_unit', 'hod.')}"
            stroj_load_lbl = getattr(stroj, 'meter_load_label', None) or get_industry_term('meter_load_label', 'V záťaži')
            stroj_load_disp = getattr(stroj, 'meter_load_display', None) or f"{stroj.motohodiny_v_zatazi} {get_industry_term('meter_load_unit', 'hod.')}"
            if stroj.iot_je_aktivny and cas_aktualizacie and (now_naive - cas_aktualizacie) < timedelta(hours=24):
                stroj_kontext_html = f"<div style='color:#1e293b; background:#f1f5f9; padding:10px 14px; border-left:4px solid #10b981; border-radius:6px; margin: 10px 0;'>📊 <b>Technický stav (Živé IoT k {cas_aktualizacie.strftime('%d.%m.%Y %H:%M')}):</b><br>Zariadenie <b>{stroj_nazov}</b>{lokalita_str} eviduje stav <b>{stroj_meter_disp}</b> ({stroj_load_lbl}: {stroj_load_disp}).{plan_info}{alarm_info}{compliance_info}</div>"
            elif datum_str:
                stroj_kontext_html = f"<div style='color:#1e293b; background:#f1f5f9; padding:10px 14px; border-left:4px solid #f59e0b; border-radius:6px; margin: 10px 0;'>📊 <b>Technický stav (Posledný telemetrický záznam z {datum_str}):</b><br>Zariadenie <b>{stroj_nazov}</b>{lokalita_str} malo zaevidovaný stav <b>{stroj_meter_disp}</b>.{plan_info}{alarm_info}{compliance_info}</div>"
            else:
                if last_servis and last_servis.stav_motohodin:
                    last_servis_meter_disp = getattr(last_servis, 'meter_display', None) or f"{last_servis.stav_motohodin} {get_industry_term('meter_unit', 'hod.')}"
                    stroj_kontext_html = f"<div style='color:#1e293b; background:#f1f5f9; padding:10px 14px; border-left:4px solid #64748b; border-radius:6px; margin: 10px 0;'>📊 <b>Technický stav (Fyzický servis zo dňa {datum_str}):</b><br>Zariadenie <b>{stroj_nazov}</b>{lokalita_str} malo zaevidovaný stav <b>{last_servis_meter_disp}</b>.{plan_info}{compliance_info}</div>"
                else:
                    stroj_kontext_html = f"<div style='color:#1e293b; background:#f1f5f9; padding:10px 14px; border-left:4px solid #64748b; border-radius:6px; margin: 10px 0;'>📊 <b>Technický stav:</b><br>Zariadenie <b>{stroj_nazov}</b>{lokalita_str} (evidovaný stav: {stroj_meter_disp}).{plan_info}{compliance_info}</div>"

        # 5. Formátovanie oslovenia, XSS sanitizácia a HTML telo
        oslovenie = escape(generate_dynamic_greeting(crm_entity, fallback="Dobrý deň,", use_formal=True) if crm_entity else "Dobrý deň,")
        
        sbs_upozornenie_html = ""
        if isinstance(crm_entity, Zakaznik) and getattr(crm_entity, 'vyzaduje_povolenie_vstupu', False):
            predmet_a_telo = f"{final_subject or ''} {pokyny_alebo_text_spravy or ''}".lower()
            if any(k in predmet_a_telo for k in ['výjazd', 'vyjazd', 'príde', 'pride', 'návštev', 'navstev', 'servis', 'oprav', 'technik']):
                sbs_upozornenie_html = "<p style='color:#0369a1; background:#f0f9ff; padding:8px 12px; border-left:3px solid #0284c7; border-radius:4px; font-size:13px; margin: 10px 0;'>🛡️ <i>Poznámka k vstupu: V zmysle Vašich bezpečnostných predpisov majú naši technici pripravené vstupné formality / povolenie na vstup.</i></p>"

        cisty_text = str(pokyny_alebo_text_spravy).replace('**', '').replace('###', '').replace('##', '').strip()
        safe_cisty_text = escape(cisty_text)
        odseky = [f"<p>{p.strip().replace('\n', '<br>')}</p>" for p in safe_cisty_text.split('\n\n') if p.strip()]
        obsah_html = "".join(odseky) if odseky else f"<p>{safe_cisty_text.replace('\n', '<br>')}</p>"
        if sbs_upozornenie_html:
            obsah_html += sbs_upozornenie_html

        # Dynamické zistenie webovej adresy firmy (Kaskáda: WebNastavenia LIVE -> FiremnaIdentita -> GlobalneNastavenia)
        dynamicky_web = None
        try:
            WebNastavenia = apps.get_model('WebBuilder', 'WebNastavenia')
            web_live = WebNastavenia.objects.filter(pk=1).first()
            if web_live and web_live.domena:
                dynamicky_web = web_live.domena
        except LookupError:
            pass

        if not dynamicky_web and identita and identita.web:
            dynamicky_web = identita.web
        elif not dynamicky_web and gn and gn.url_systemu:
            dynamicky_web = gn.url_systemu
        elif not dynamicky_web and getattr(settings, 'PUBLIC_WEB_DOMAIN', None):
            dynamicky_web = getattr(settings, 'PUBLIC_WEB_DOMAIN', None)

        if ucet.podpis_html and ucet.podpis_html.strip():
            podpis_html = ucet.podpis_html.strip()
        elif identita:
            kontakty_riadok = []
            if identita.telefon: kontakty_riadok.append(f"📞 {escape(identita.telefon)}")
            if identita.email_vseobecny: kontakty_riadok.append(f"✉️ {escape(identita.email_vseobecny)}")
            if dynamicky_web:
                cisty_web = str(dynamicky_web).strip().replace("https://", "").replace("http://", "").strip("/")
                safe_web = escape(cisty_web)
                kontakty_riadok.append(f"🌐 <a href='https://{safe_web}' style='color:#2563eb;text-decoration:none;'>{safe_web}</a>")
            kontakty_html = " &nbsp;|&nbsp; ".join(kontakty_riadok)
            cislo_sidla = f" {identita.sidlo_cislo}".strip() if getattr(identita, 'sidlo_cislo', None) else ""
            ulica_cislo = f"{identita.sidlo_ulica or ''} {cislo_sidla}".strip()
            adresa_str = identita.adresa_prevadzky or f"{ulica_cislo}, {identita.sidlo_psc or ''} {identita.sidlo_mesto or ''}".strip(' ,')
            
            podtitul_line = f"<p style='margin: 0 0 6px 0; font-size: 12px; color: #64748b; font-weight: 500;'>{escape(identita.podtitul)}</p>" if (identita and getattr(identita, 'podtitul', None)) else ""
            podpis_html = (
                f"<div style='margin-top: 16px;'>"
                f"<p style='margin: 0 0 4px 0; font-weight: 700; color: #0f172a;'>S pozdravom,</p>"
                f"<p style='margin: 0 0 4px 0; font-weight: 800; font-size: 15px; color: #1e293b;'>Tím servisu &amp; dispečingu | {escape(firma_nazov)}</p>"
                f"{podtitul_line}"
                f"<p style='margin: 0 0 6px 0; font-size: 13px; color: #475569;'>{kontakty_html}</p>"
                f"<p style='margin: 0 0 4px 0; font-size: 12px; color: #64748b;'>📍 {escape(adresa_str)}</p>"
                f"<p style='margin: 0; font-size: 11px; color: #94a3b8;'>{f'IČO: {escape(identita.ico)} ' if identita.ico else ''}{f'| IČ DPH: {escape(identita.ic_dph)} ' if identita.ic_dph else ''}{f'| {escape(identita.zapis_v_registri)}' if identita.zapis_v_registri else ''}</p>"
                f"</div>"
            )

        else:
            podpis_html = "<p style='margin-top:16px;'>S pozdravom,<br><b>Tím servisu</b></p>"

        safe_je_obchodna = (
            je_obchodna_komunikacia is True
            or str(je_obchodna_komunikacia).strip().lower() in ['true', '1', 'ano', 'áno', 'yes']
        )
        je_interny_zamestnanec = isinstance(crm_entity, Zamestnanec)
        je_novy_externy_kontakt = (not je_odpoved_na_email) and (crm_entity is None)
        text_na_detekciu = f"{final_subject or ''} {pokyny_alebo_text_spravy or ''}"
        ma_obchodny_charakter = bool(re.search(
            r'(?i)(?<!\w)('
            r'ponuk\w*|nab[íi]d\w*|'
            r'akviz\w*|spolupr[áa]c\w*|'
            r'z[ľl]av\w*|slev\w*|'
            r'akci[íiaeouy]\w*|akciov\w*|ak[čc]n[íiy]\w*|'
            r'katal[óo]g\w*|cenn[íi]k\w*|cen[íi]k\w*|'
            r'predstavujeme|p[řr]edstavujeme|'
            r'sp[úu][šs][ťt]a\w*|spou[šs]t[íi]\w*|spusten\w*|spu[šs]t[eě]n\w*|'
            r'uv[áa]dz\w*|uv[áa]d[íi]\w*|'
            r'novink\w*|promo\w*|newsletter\w*|'
            r'v[ýy]hodn\w*|zv[ýy]hodn\w*|'
            r'zdarma|zadarmo|bezplatn\w*|'
            r'poz[ýy]vame|zveme|pozv[áa]nk\w*|'
            r'prezent[áa]c\w*|prezentac\w*|'
            r'sk[úu][šs]obn\w*|zku[šs]ebn\w*|'
            r'obchodn[éeyýi]\w*|'
            r'objednaj\w*|objednej\w*'
            r')(?!\w)',
            text_na_detekciu
        ))
        je_obchodny_alebo_studeny = (not je_interny_zamestnanec) and (
            safe_je_obchodna or ((not je_odpoved_na_email) and (je_novy_externy_kontakt or ma_obchodny_charakter))
        )

        opt_out_zaznam = EmailOptOut.objects.filter(email__iexact=to_addr, aktivny=True).first()
        je_opt_out = bool(opt_out_zaznam)
        if opt_out_zaznam and (je_obchodny_alebo_studeny or (opt_out_zaznam.rozsah == 'KOMPLETNY' and not je_odpoved_na_email)):
            return json.dumps({
                "error": (
                    f"Príjemca '{to_addr}' je evidovaný v opt-out zozname ({opt_out_zaznam.get_rozsah_display()}). "
                    "Vytvorenie tohto e-mailového konceptu bolo automaticky zablokované."
                )
            })

        obchodna_paticka_html = ""
        obchodna_paticka_text = ""
        if je_obchodny_alebo_studeny:
            obchodna_paticka_html = (
                "<p style='margin: 12px 0 0 0; font-size: 11px; color: #94a3b8; line-height: 1.4;'>"
                "Ak ide o obchodnú alebo akvizičnú komunikáciu a neprajete si dostávať podobné správy, "
                "odpovedzte prosím na tento e-mail so slovom <b>NEZASIELAŤ</b> a Vašu adresu okamžite vyradíme z ďalšej komunikácie."
                "</p>"
            )
            obchodna_paticka_text = (
                "\n\n---\nAk si neprajete dostávať obchodné ponuky, odpovedzte prosím na tento e-mail so slovom NEZASIELAŤ."
            )

        final_html = (
            f"<div style=\"font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; "
            f"font-size: 14.5px; line-height: 1.6; color: #1e293b; max-width: 680px;\">"
            f"<p style='margin: 0 0 12px 0;'>{oslovenie}</p>"
            f"{stroj_kontext_html}"
            f"<div style='margin: 12px 0;'>{obsah_html}</div>"
            f"<div style='margin-top: 16px; border-top: 1px solid #e2e8f0; padding-top: 12px;'>{podpis_html}{obchodna_paticka_html}</div>"
            f"</div>"
        )

        # 6. Univerzálne spracovanie fyzických príloh z databázy (PDF, fotografie dielov/strojov, schémy, QR kódy, dokumenty)
        celery_attachments = []
        pripravene_prilohy = []

        if prilozit_dokument_model and safe_priloha_doc_id:
            field_defaults = {
                'CenovaPonuka': ['pdf_dokument'],
                'VystavenaFaktura': ['pdf_dokument', 'peppol_xml_dokument'],
                'ServisnyZaznam': ['uzavrety_pdf_dokument', 'foto_po', 'foto_pred', 'video'],
                'PovolenieNaVstup': ['vygenerovany_dokument'],
                'OdoslanaObjednavka': ['pdf_dokument'],
                'PrijataObjednavka': ['subor_objednavky'],
                'PrichodziaFaktura': ['subor', 'vizualizacia_subor', 'xml_subor'],
                'TechnickaSpecifikacia': ['subor_manual', 'subor_schema', 'subor_certifikat'],
                'ServisovaneZariadenie': ['obrazok_na_webe', 'qr_kod'],
                'Tankovanie': ['foto_blocku'],
                'Zamestnanec': ['profilova_fotka', 'zmluva_subor', 'bozp_subor', 'lekarska_prehliadka_subor'],
                'Certifikat': ['subor'],
                'Zakaznik': ['sablona_vstupu'],
                'WebBlok': ['obrazok'],
                'Clanok': ['obrazok'],
                'Kandidat': ['zivotopis'],
                'MegaZaloha': ['subor_zalohy']
            }
            try:
                DocModel = None
                industry_mod = (getattr(settings, 'INDUSTRY_MODULE', None) or '').strip().lower()
                doc_search_apps = ['Evidencia', 'ISASaplikacia', 'WebBuilder']
                if industry_mod:
                    doc_search_apps.extend([industry_mod, f"odvetvia.{industry_mod}"])
                for app_label in doc_search_apps:
                    try:
                        DocModel = apps.get_model(app_label, prilozit_dokument_model)
                        break
                    except LookupError:
                        continue

                if not DocModel:
                    vsetky_odvetvove_modely = list(ZARIADENIA_MAP.values()) + list(PARENT_MODELS.values()) + [m for subcats in CATEGORY_MAP.values() for m in subcats.values()]
                    for m in vsetky_odvetvove_modely:
                        if hasattr(m, '_meta') and m._meta.object_name.lower() == str(prilozit_dokument_model).lower():
                            DocModel = m
                            break

                if DocModel:
                    doc_obj = DocModel.objects.filter(id=safe_priloha_doc_id).first()
                    if doc_obj:
                        polia_na_spracovanie = []
                        if prilozit_dokument_pole:
                            polia_na_spracovanie = [p.strip() for p in str(prilozit_dokument_pole).split(',') if p.strip()]
                        elif prilozit_dokument_model in field_defaults:
                            for candidate in field_defaults[prilozit_dokument_model]:
                                if hasattr(doc_obj, candidate) and getattr(doc_obj, candidate):
                                    polia_na_spracovanie = [candidate]
                                    break
                        else:
                            for f in doc_obj._meta.get_fields():
                                if hasattr(f, 'upload_to') and hasattr(doc_obj, f.name):
                                    file_val = getattr(doc_obj, f.name)
                                    if file_val:
                                        polia_na_spracovanie.append(f.name)
                                        break

                        for pole_nazov in polia_na_spracovanie:
                            if hasattr(doc_obj, pole_nazov):
                                subor_field = getattr(doc_obj, pole_nazov)
                                if subor_field and hasattr(subor_field, 'path') and os.path.exists(subor_field.path):
                                    with open(subor_field.path, 'rb') as f_read:
                                        raw_bytes = f_read.read()
                                    
                                    priloha_nazov = os.path.basename(subor_field.path)
                                    guessed_mime, _ = mimetypes.guess_type(priloha_nazov)
                                    priloha_mime = guessed_mime or 'application/octet-stream'
                                    
                                    b64_content = base64.b64encode(raw_bytes).decode('utf-8')
                                    celery_attachments.append({
                                        'name': priloha_nazov,
                                        'content': b64_content,
                                        'type': priloha_mime
                                    })
                                    pripravene_prilohy.append({
                                        'file': ContentFile(raw_bytes, name=priloha_nazov),
                                        'nazov': priloha_nazov,
                                        'mime': priloha_mime,
                                        'size': len(raw_bytes)
                                    })
            except Exception as att_err:
                logger.warning(f"Nepodarilo sa načítať prílohu {prilozit_dokument_model} ID {prilozit_dokument_id}: {att_err}")

        # 7. Asynchrónny IMAP zápis a atomické uloženie konceptu do DB
        novy_msg_id = f"isas-draft-{timezone.now().timestamp()}@isas.core"

        with transaction.atomic():
            novy_koncept = EmailovaSprava.objects.create(
                ucet=ucet,
                message_id=novy_msg_id,
                odosielatel=ucet.email_adresa,
                prijemca=to_addr,
                predmet=final_subject,
                datum_prijatia=timezone.now(),
                telo_html=final_html,
                telo_text=f"{cisty_text}{obchodna_paticka_text}",
                precitane=True,
                zlozka='DRAFTS',
                zakaznik_id=zak_id,
                dodavatel_id=dod_id,
                zamestnanec_id=zam_id
            )

            # Fyzické vytvorenie záznamov EmailPriloha v DB pre všetky priložené súbory
            model_fields = {f.name for f in EmailPriloha._meta.get_fields()}
            for att_item in pripravene_prilohy:
                priloha_kwargs = {
                    'sprava': novy_koncept,
                    'nazov_suboru': att_item['nazov'][:250],
                    'subor': att_item['file'],
                    'mime_type': att_item['mime'][:100]
                }
                if 'velkost_bajtov' in model_fields:
                    priloha_kwargs['velkost_bajtov'] = att_item['size']
                elif 'velkost_suboru_bajtov' in model_fields:
                    priloha_kwargs['velkost_suboru_bajtov'] = att_item['size']

                EmailPriloha.objects.create(**priloha_kwargs)

            def _dispatch_save_draft_imap():
                try:
                    save_draft_imap_task.delay(
                        ucet.id,
                        to_addr,
                        final_subject,
                        final_html,
                        "",
                        "",
                        celery_attachments,
                        novy_msg_id
                    )
                except Exception as exc:
                    logger.error(
                        f"Zlyhalo zaradenie save_draft_imap_task do Celery (výpadok Redis brokera): {exc}",
                        exc_info=True
                    )

            transaction.on_commit(_dispatch_save_draft_imap)

        odkaz_na_koncept = f"/admin/ISASaplikacia/emailovasprava/{novy_koncept.id}/change/"
        prilohy_popis = ", ".join([a['nazov'] for a in pripravene_prilohy]) if pripravene_prilohy else "Žiadna"
        
        spam_upozornenie = (
            " ⚠️ PRÁVNA POZNÁMKA: Príjemca nie je evidovaný v CRM a nejde o odpoveď na prijatý e-mail. "
            "Pred ručným odoslaním konceptu overte, že máte predchádzajúci súhlas príjemcu alebo odpovedáte na jeho vlastný dopyt (zákaz nevyžiadanej pošty)."
            if je_novy_externy_kontakt else ""
        )
        opt_out_upozornenie = (
            " 🚫 POZOR: Príjemca je evidovaný v opt-out zozname (NEZASIELAŤ) pre obchodnú komunikáciu. "
            "Koncept bol vytvorený iba ako neobchodná / servisná správa — pred odoslaním overte, že neobsahuje marketingový obsah."
            if je_opt_out else ""
        )

        return json.dumps({
            "status": "success",
            "koncept_id": novy_koncept.id,
            "odkaz_na_koncept": odkaz_na_koncept,
            "prijemca_email": to_addr,
            "predmet": final_subject,
            "subjekt_crm": str(crm_entity) if crm_entity else "Neuvedený",
            "stav_pocitadla_zahrnuty": bool(stroj_kontext_html),
            "prilohy_zahrnute": prilohy_popis,
            "pocet_priloh": len(pripravene_prilohy),
            "vyzaduje_kontrolu_suhlasu": je_novy_externy_kontakt,
            "je_v_opt_out_zozname": je_opt_out,
            "informacia": f"Koncept e-mailu pre '{to_addr}' s predmetom '{final_subject}' bol úspešne pripravený a uložený{' s prílohami: ' + prilohy_popis if pripravene_prilohy else ''}.{spam_upozornenie}{opt_out_upozornenie} Pre zobrazenie vlož používateľovi odkaz: [📧 Otvoriť koncept e-mailu]({odkaz_na_koncept})"
        })

    except Exception as e:
        logger.error(f"tool_priprav_koncept_emailu zlyhal: {e}", exc_info=True)
        return json.dumps({"error": f"Kritická chyba pri vytváraní konceptu e-mailu: {str(e)}"})

# =========================================================================
# 🧠 AI COMMAND CENTER - THE OMEGA PROMPT V22 (EXECUTION LOOP & SOTA)
# =========================================================================
def ask_isas_ai(user_question: Optional[str] = None, history_text: str = "", audio_path: Optional[str] = None, file_paths: Optional[list] = None, force_json: bool = False, aktualny_pouzivatel: str = "Neznámy", rola_pouzivatela: str = "Neznáma", typ_pomeru: str = "Neznámy", is_superuser: bool = False, ma_pristup_k_financiam: bool = False, subjekt_id: Optional[int] = None) -> dict:
    """
    Hlavná brána pre komunikáciu s Gemini 4 HYDROGEN.
    Plne spravuje Tool Calling, Pagináciu, a Memory Window Management.
    """
    
    # 🛡️ HARD SECURITY RBAC: Jednotná klasifikácia subjektu (Súkromná osoba / B2B / Zamestnanec / Vedenie)
    is_sukromna_osoba, is_b2b = _klasifikuj_externy_subjekt(typ_pomeru, rola_pouzivatela)
    je_web_admin = (not is_sukromna_osoba and not is_b2b) and _je_vedenie_firmy(typ_pomeru, rola_pouzivatela, is_superuser)
    ma_financie = (not is_sukromna_osoba and not is_b2b) and bool(ma_pristup_k_financiam or je_web_admin)

    def secure_web_manager(**kwargs):
        if not je_web_admin:
            return json.dumps({"error": "Kritické zamietnutie: Nemáte bezpečnostné oprávnenie na úpravu webu. Požiadavku môže vykonať iba Konateľ alebo Superadmin."})
        return tool_spravuj_verejny_web(**kwargs)

    def secure_visibility_manager(**kwargs):
        if not je_web_admin:
            return json.dumps({"error": "Kritické zamietnutie: Nemáte oprávnenie na zmenu viditeľnosti entít na webe."})
        return tool_spravuj_viditelnost_entit(**kwargs)

    def secure_image_manager(**kwargs):
        if not je_web_admin:
            return json.dumps({"error": "Kritické zamietnutie: Nemáte oprávnenie na zmenu obrázkov na webe."})
        return tool_najdi_a_nastav_ilustracny_obrazok(**kwargs)

    def secure_publish_manager(**kwargs):
        if not je_web_admin:
            return json.dumps({"error": "Kritické zamietnutie: Nemáte oprávnenie na publikovanie webu."})
        return tool_publikuj_draft_webu(**kwargs)

    def secure_discard_manager(**kwargs):
        if not je_web_admin:
            return json.dumps({"error": "Kritické zamietnutie: Nemáte oprávnenie na zmazanie návrhu webu."})
        return tool_zahod_draft_webu(**kwargs)

    def secure_copywriter_manager(**kwargs):
        if not je_web_admin:
            return json.dumps({"error": "Kritické zamietnutie: Nemáte oprávnenie na úpravu marketingových textov v ERP."})
        return tool_vylepsi_texty_v_erp(**kwargs)

    def secure_blog_manager(**kwargs):
        if not je_web_admin:
            return json.dumps({"error": "Kritické zamietnutie: Nemáte oprávnenie na správu článkov a blogu."})
        return tool_spravuj_clanky_a_blog(**kwargs)

    def secure_faq_manager(**kwargs):
        if not je_web_admin:
            return json.dumps({"error": "Kritické zamietnutie: Nemáte oprávnenie na správu FAQ položiek."})
        return tool_spravuj_faq(**kwargs)

    def secure_case_study_manager(**kwargs):
        if not je_web_admin:
            return json.dumps({"error": "Kritické zamietnutie: Nemáte oprávnenie na vytváranie verejných referencií z výjazdov."})
        return tool_vytvor_pripadovu_studiu_z_vyjazdu(**kwargs)

    def secure_career_manager(**kwargs):
        if not je_web_admin:
            return json.dumps({"error": "Kritické zamietnutie: Nemáte bezpečnostné oprávnenie na správu pracovných ponúk."})
        return tool_spravuj_pracovne_pozicie(**kwargs)

    def secure_social_media_manager(**kwargs):
        if not je_web_admin:
            return json.dumps({"error": "Kritické zamietnutie: Nemáte bezpečnostné oprávnenie na generovanie príspevkov na sociálne siete."})
        return tool_priprav_social_media_post(**kwargs)

    def _over_povolene_doc_pole(model_name: Optional[str], nazov_pola: Optional[str]) -> Optional[str]:
        if je_web_admin:
            return None
        if ma_financie:
            m_lower = str(model_name or '').strip().lower()
            f_lower = str(nazov_pola or '').strip().lower()
            if m_lower in {'kandidat', 'megazaloha', 'importdattask'}:
                return json.dumps({"error": "Kritické zamietnutie: Nemáte oprávnenie na čítanie náborových a systémových súborov."})
            if m_lower == 'zamestnanec' and f_lower in {'osobny_podpis', 'lekarska_prehliadka_subor', 'zmluva_subor'}:
                return json.dumps({"error": "Kritické zamietnutie: Nemáte oprávnenie na prístup k osobným podpisom, pracovným zmluvám a lekárskym posudkom zamestnancov."})
            if m_lower == 'firemnaidentita' and f_lower == 'peciatka_podpis':
                return json.dumps({"error": "Kritické zamietnutie: Pečiatka a podpis štatutára sú prístupné výhradne vedeniu spoločnosti."})
            return None
        povolene_doc_polia: dict[str, set[str]] = {
            'technickaspecifikacia': {'subor_manual', 'subor_schema', 'subor_certifikat'},
            'servisovanezariadenie': {'qr_kod', 'obrazok_na_webe'},
            'servisnyzaznam': {'foto_pred', 'foto_po', 'video', 'uzavrety_pdf_dokument'},
            'povolenienavstup': {'vygenerovany_dokument'},
            'zakaznik': {'sablona_vstupu'},
            'databazavyrobcov': {'logo_na_webe'},
        }
        for subcats in CATEGORY_MAP.values():
            for m_cls in subcats.values():
                if hasattr(m_cls, '_meta'):
                    povolene_doc_polia[m_cls._meta.object_name.lower()] = {'obrazok_na_webe'}

        m_key = str(model_name or '').strip().lower()
        f_key = str(nazov_pola or '').strip()
        if m_key not in povolene_doc_polia or (f_key and f_key not in povolene_doc_polia[m_key]):
            return json.dumps({"error": "Kritické zamietnutie: Nemáte oprávnenie na čítanie finančných, HR alebo interných dokumentov."})
        return None

    def secure_mail_draft_manager(**kwargs):
        # 🔒 Hard-Security: Prístup majú výhradne Superadmin, Konateľ ('KO'), Dispečer ('DI') a Administratíva ('AD')
        if is_sukromna_osoba or is_b2b:
            return json.dumps({"error": "Kritické zamietnutie: Nemáte bezpečnostné oprávnenie na prípravu e-mailovej korešpondencie firmy."})
        povolene_roly_db = {Zamestnanec.DISPECER, Zamestnanec.ADMINISTRATIVA}
        rola_val = (rola_pouzivatela or '').strip()
        rola_choices_dict = dict(Zamestnanec.ROLA_CHOICES)

        je_opravnena_rola = (
            rola_val in povolene_roly_db or
            any(rola_val == rola_choices_dict.get(r) for r in povolene_roly_db)
        )

        if not (je_web_admin or je_opravnena_rola):
            return json.dumps({"error": "Kritické zamietnutie: Nemáte bezpečnostné oprávnenie na prípravu e-mailovej korešpondencie firmy."})

        if not (je_web_admin or ma_financie):
            povol_id = kwargs.get('povodny_email_id')
            if povol_id is not None and str(povol_id).strip().lower() not in {'null', 'none', ''}:
                try:
                    EmailovaSprava = apps.get_model('ISASaplikacia', 'EmailovaSprava')
                    if EmailovaSprava.objects.filter(
                        id=int(str(povol_id).strip())
                    ).filter(
                        Q(ai_kategoria__in=['Účtovníctvo / Faktúry', 'HR / Interné', 'Nezaradené']) | Q(ai_kategoria__isnull=True) | Q(ai_kategoria='')
                    ).exists():
                        return json.dumps({"error": "Kritické zamietnutie: Nemáte bezpečnostné oprávnenie odpovedať na účtovný, HR alebo nezaradený e-mail."})
                except (ValueError, TypeError):
                    return json.dumps({"error": "Neplatné ID pôvodného e-mailu."})

            att_model = kwargs.get('prilozit_dokument_model')
            att_id = kwargs.get('prilozit_dokument_id')
            att_pole = kwargs.get('prilozit_dokument_pole')
            if att_model or att_id or att_pole:
                polia = [p.strip() for p in str(att_pole).split(',') if p.strip()] if att_pole else ['']
                for p_nazov in polia:
                    deny = _over_povolene_doc_pole(att_model, p_nazov)
                    if deny:
                        return deny

        return tool_priprav_koncept_emailu(**kwargs)

    def _over_identitu_subjektu() -> Optional[str]:
        ciste_meno = str(aktualny_pouzivatel or '').strip()
        if not ciste_meno or ciste_meno.lower() in {'neznámy', 'neznamy', 'none', 'null'}:
            return None
        return ciste_meno

    def secure_driver_fleet(**kwargs):
        vodic = _over_identitu_subjektu()
        if not vodic and subjekt_id is None:
            return json.dumps({"error": "Kritické zamietnutie: Chýba overená identita vodiča."})
        kwargs.pop('exact_sukromna_osoba', None)
        kwargs.pop('exact_sukromna_osoba_id', None)
        if subjekt_id is None:
            kwargs['spz_alebo_vodic'] = vodic
        raw_res = _vykonaj_fleet_dotaz(**kwargs, exact_sukromna_osoba=vodic, exact_sukromna_osoba_id=subjekt_id)
        try:
            parsed = json.loads(raw_res)
            if isinstance(parsed, dict):
                for v in parsed.get("vozidla_stav", []):
                    for k in ("erp_stredisko", "erp_status", "erp_id", "erp_last_exported_at", "erp_chybovy_log", "cislo_palivovej_karty", "typ_vozidla_erp", "erp_kod_paliva"):
                        v.pop(k, None)
                    if v.get("je_firemne"):
                        v.pop("celkove_naklady_palivo_eur", None)
                        v.pop("vlastna_sadzba_za_km_eur", None)
                for j in parsed.get("posledne_jazdy", []):
                    j.pop("erp_zakazka", None)
                    j.pop("vyfakturovane", None)
                    j.pop("suvisiaca_uloha_id", None)
                for t in parsed.get("nedavne_tankovania", []):
                    for k in ("sposob_zauctovania_erp", "kod_clenenia_dph", "erp_status", "erp_id", "erp_last_exported_at", "erp_chybovy_log"):
                        t.pop(k, None)
            return json.dumps(parsed)
        except Exception:
            return json.dumps({"error": "Chyba bezpečnostného filtra pri spracovaní dát."})

    def secure_b2b_zariadenia(**kwargs):
        firma = _over_identitu_subjektu()
        if not firma and subjekt_id is None:
            return json.dumps({"error": "Kritické zamietnutie: Chýba overená identita B2B zákazníka."})
        kwargs.pop('exact_zakaznik_nazov', None)
        kwargs.pop('exact_zakaznik_id', None)
        if subjekt_id is None:
            kwargs['nazov_zakaznika'] = firma
        raw_res = _vykonaj_zariadenia_dotaz(**kwargs, exact_zakaznik_nazov=firma, exact_zakaznik_id=subjekt_id)
        try:
            parsed = json.loads(raw_res)
            if isinstance(parsed, dict) and isinstance(parsed.get("data"), list):
                firma_l = (firma or "").lower()
                filtered = []
                for s in parsed["data"]:
                    if subjekt_id is None and str(s.get("zakaznik", "")).strip().lower() != firma_l:
                        continue
                    for k in ("je_nas_vlastny_stroj_na_dielni", "interny_nazov", "iot_mac_adresa", "iot_ip_adresa", "iot_api_kluc", "iot_limit_zapisov_za_hodinu", "iot_slovnik_signalov", "bezpecnostny_kluc_pas_stroja", "servisne_predpisy", "logy_korekcii_motohodin", "poznamka", "historia_prenajmov"):
                        s.pop(k, None)
                    filtered.append(s)
                parsed["data"] = filtered
            return json.dumps(parsed)
        except Exception:
            return json.dumps({"error": "Chyba bezpečnostného filtra pri spracovaní dát."})

    def secure_b2b_operativa(**kwargs):
        firma = _over_identitu_subjektu()
        if not firma and subjekt_id is None:
            return json.dumps({"error": "Kritické zamietnutie: Chýba overená identita B2B zákazníka."})
        kwargs.pop('exact_zakaznik_nazov', None)
        kwargs.pop('exact_zakaznik_id', None)
        if subjekt_id is None:
            kwargs['zakaznik_nazov'] = firma
        raw_res = _vykonaj_operativa_dotaz(**kwargs, exact_zakaznik_nazov=firma, exact_zakaznik_id=subjekt_id)
        try:
            parsed = json.loads(raw_res)
            if isinstance(parsed, dict):
                for z in parsed.get("nedavna_historia_servisov", []):
                    for k in ("skryta_ai_analyza_tco", "poruchy_a_poznamky_technika", "koren_problemu_ak_reklamacia", "upozornit_majitela_protokol_tg", "upozornit_majitela_protokol_mail", "upozornit_majitela_protokol_discord", "spotrebovany_material"):
                        z.pop(k, None)
                for r in parsed.get("aktivne_reklamacie", []):
                    r.pop("pouzity_material_zatial", None)
                    r.pop("popis_problemu", None)
                for u in parsed.get("otvorene_ulohy", []):
                    u.pop("pouzity_material_zatial", None)
                    u.pop("kontrolovat_sklad_jit", None)
                    u.pop("detail_ulohy", None)
                for p in parsed.get("povolenia_na_vstup", []):
                    for k in ("ip_schvalitela_sbs", "text_emailu_pre_vratnicu", "notif_technik_tg", "notif_technik_mail", "notif_technik_discord", "notif_admin_tg", "notif_admin_mail", "notif_admin_discord"):
                        p.pop(k, None)
            return json.dumps(parsed)
        except Exception:
            return json.dumps({"error": "Chyba bezpečnostného filtra pri spracovaní dát."})

    def _filtruj_polia(zoznam: Any, povolene_polia: set[str]) -> list[dict[str, Any]]:
        if not isinstance(zoznam, list):
            return []
        return [
            {k: v for k, v in polozka.items() if k in povolene_polia}
            for polozka in zoznam
            if isinstance(polozka, dict)
        ]

    def _zisti_rolu_kod() -> Optional[str]:
        rola_val = (rola_pouzivatela or '').strip()
        if not rola_val:
            return None
        rola_choices_dict = dict(Zamestnanec.ROLA_CHOICES)
        if rola_val in rola_choices_dict:
            return rola_val
        for kod, label in Zamestnanec.ROLA_CHOICES:
            if rola_val == label:
                return kod
        return None

    def secure_b2b_financie(**kwargs):
        firma = _over_identitu_subjektu()
        if not firma and subjekt_id is None:
            return json.dumps({"error": "Kritické zamietnutie: Chýba overená identita B2B zákazníka."})
        kwargs.pop('povolene_sekcie', None)
        kwargs.pop('exact_zakaznik_nazov', None)
        kwargs.pop('exact_zakaznik_id', None)
        if subjekt_id is None:
            kwargs['hladany_vyraz'] = firma
        raw_res = _vykonaj_financie_crm_dotaz(
            **kwargs,
            povolene_sekcie={"vystavene_faktury_pohladavky", "cenove_ponuky", "prijate_objednavky_od_zakaznikov"},
            exact_zakaznik_nazov=firma,
            exact_zakaznik_id=subjekt_id,
        )
        try:
            parsed = json.loads(raw_res)
            if isinstance(parsed, dict):
                firma_l = (firma or "").lower()
                if "vystavene_faktury_pohladavky" in parsed:
                    vlastne_fa = [fa for fa in parsed["vystavene_faktury_pohladavky"] if (subjekt_id is not None or str(fa.get("zakaznik", "")).strip().lower() == firma_l)]
                    for fa in vlastne_fa:
                        for k in ("ai_auditor_poznamka", "erp_status", "erp_id", "erp_last_exported_at", "erp_chybovy_log", "peppol_chybovy_log", "zdrojovy_sklad_id", "odpisane_zo_skladu", "notif_odpis_tg", "notif_odpis_mail", "notif_odpis_discord", "notif_majitel_tg", "notif_majitel_mail", "notif_majitel_discord"):
                            fa.pop(k, None)
                    parsed["vystavene_faktury_pohladavky"] = vlastne_fa
                if "cenove_ponuky" in parsed:
                    vlastne_cp = [cp for cp in parsed["cenove_ponuky"] if (subjekt_id is not None or str(cp.get("zakaznik", "")).strip().lower() == firma_l)]
                    for cp in vlastne_cp:
                        cp.pop("interne_poznamky", None)
                        cp.pop("ip_adresa_schvalitela", None)
                    parsed["cenove_ponuky"] = vlastne_cp
                if "prijate_objednavky_od_zakaznikov" in parsed:
                    vlastne_obj = [obj for obj in parsed["prijate_objednavky_od_zakaznikov"] if (subjekt_id is not None or str(obj.get("zakaznik", "")).strip().lower() == firma_l)]
                    for obj in vlastne_obj:
                        obj.pop("interna_poznamka", None)
                        obj.pop("ai_navrhnute_ponuky", None)
                        obj.pop("ignorovane_ponuky_ai", None)
                    parsed["prijate_objednavky_od_zakaznikov"] = vlastne_obj
            return json.dumps(parsed)
        except Exception:
            return json.dumps({"error": "Chyba bezpečnostného filtra pri spracovaní dát."})

    def secure_sys_info(**kwargs):
        raw_res = tool_systemove_informacie_a_maily(**kwargs)
        if je_web_admin:
            return raw_res
        try:
            parsed = json.loads(raw_res)
            if isinstance(parsed, dict):
                for k in ("infrastruktura_a_pozadie", "webhook_integracie", "webhook_nedavne_logy", "nedavne_exporty_a_zalohy", "nedavne_importy_dat", "pripojene_emailove_schranky"):
                    parsed.pop(k, None)
                eko = parsed.get("ekonomicke_nastavenia")
                if isinstance(eko, dict):
                    for k in ("telegram_id_majitel", "discord_id_majitel", "url_systemu", "stav_integracii", "email_majitel_notifikacie"):
                        eko.pop(k, None)
                if not ma_financie:
                    parsed.pop("erp_uctovne_nastavenia", None)
                    parsed.pop("neprecitane_maily", None)
                    parsed.pop("opt_out_zoznam_nezasilat", None)
                    f_udaje = parsed.get("firemne_udaje")
                    if isinstance(f_udaje, dict):
                        for k in ("iban", "swift", "nazov_banky", "email_fakturacia", "email_uctovnicky"):
                            f_udaje.pop(k, None)
                    if isinstance(eko, dict):
                        for k in ("email_pre_faktury", "email_pre_banku", "hodinova_sadzba_technik_eur", "nahrada_za_km_pre_zamestnanca_eur", "stravne_5_12h_eur", "stravne_12_18h_eur", "stravne_nad_18h_eur", "pohotovostny_priplatok_percento", "fakturacia_zakaznikovi_za_km_doprava_eur", "fakturacia_zakaznikovi_za_cas_na_ceste_eur", "ai_denne_limity_sprav"):
                            eko.pop(k, None)
                    for u in parsed.get("cennik_pracovnych_ukonov", []):
                        u.pop("hodinova_sadzba_naklad_eur", None)
                        u.pop("fixny_naklad_eur", None)
            return json.dumps(parsed)
        except Exception:
            return json.dumps({"error": "Chyba bezpečnostného filtra pri spracovaní dát."})

    def secure_sklad_internal(**kwargs):
        raw_res = tool_sklad_a_katalog(**kwargs)
        if ma_financie:
            return raw_res
        try:
            parsed = json.loads(raw_res)
            if isinstance(parsed, dict):
                for item in parsed.get("data", []):
                    item.pop("nakupna_cena_eur", None)
                    item.pop("marza_percento", None)
                for item in parsed.get("cakajuce_v_dashboarde", []):
                    item.pop("nakupna_cena_eur", None)
            return json.dumps(parsed)
        except Exception:
            return json.dumps({"error": "Chyba bezpečnostného filtra pri spracovaní dát."})

    def secure_hr_internal(**kwargs):
        raw_res = tool_hr_a_mzdy(**kwargs)
        if ma_financie:
            return raw_res
        try:
            parsed = json.loads(raw_res)
            if isinstance(parsed, dict):
                parsed.pop("kandidati", None)
                parsed.pop("sukromne_osoby", None)
                rola_kod_hr = _zisti_rolu_kod()
                extra_skryte = (
                    ("schvalene_buduce_absencie", "live_gps_suradnice", "posledna_znama_poloha_db")
                    if rola_kod_hr != Zamestnanec.DISPECER
                    else ()
                )
                for z in parsed.get("zamestnanci", []):
                    for k in (
                        "mzda_brutto_hodina_eur", "mzda_brutto_mesiac_eur", "mzda_zivnostnik_hodina_eur",
                        "interny_hodinovy_naklad_eur", "dlhy_a_zavazky_voci_firme_eur", "poznamka_k_zavazkom",
                        "cislo_op", "iban", "adresa", "datum_narodenia",
                        "narok_na_dovolenku_dni", "vycerpana_dovolenka_dni", "zostatok_dovolenky_dni",
                        "typ_pomeru", "sposob_odmenovania", "datum_nastupu", "ma_sken_zmluvy", "ma_lekarsku_prehliadku",
                        "erp_status", "erp_id", "erp_last_exported_at", "erp_chybovy_log",
                        *extra_skryte,
                    ):
                        z.pop(k, None)
            return json.dumps(parsed)
        except Exception:
            return json.dumps({"error": "Chyba bezpečnostného filtra pri spracovaní dát."})

    def secure_stats_internal(**kwargs):
        raw_res = tool_manazerske_statistiky(**kwargs)
        if ma_financie:
            return raw_res
        try:
            parsed = json.loads(raw_res)
            if isinstance(parsed, dict):
                parsed.pop("financie_a_uctovnictvo", None)
                parsed.pop("obchod_a_cenotvorba", None)
                hr_blk = parsed.get("ludske_zdroje_hr")
                if isinstance(hr_blk, dict):
                    hr_blk.pop("mesacne_mzdove_naklady_brutto_eur", None)
                    hr_blk.pop("zavazky_zamestnancov_voci_firme_eur", None)
                it_blk = parsed.get("it_a_system")
                if isinstance(it_blk, dict):
                    it_blk.pop("ai_asistent_naklady_usd", None)
            return json.dumps(parsed)
        except Exception:
            return json.dumps({"error": "Chyba bezpečnostného filtra pri spracovaní dát."})

    def secure_hlbkova_internal(**kwargs):
        typ_an = str(kwargs.get("typ_analyzy", "")).strip()
        if not ma_financie and typ_an in {
            "ziskovost_zakaznika", "analyza_dodavatela", "uctovny_audit",
            "saldo_pohladavky", "saldo_zavazky", "obchodna_pipeline", "ai_audit",
            "backlog_objednavok", "crm_audit", "nevyfakturovana_praca", "crm_udalosti",
        }:
            return json.dumps({"error": "Kritické zamietnutie: Nemáte oprávnenie na finančné, nákladové a CRM audity."})
        if not ma_financie and _zisti_rolu_kod() != Zamestnanec.DISPECER:
            if typ_an == "hr_expiracie":
                return json.dumps({"error": "Kritické zamietnutie: Prehľad HR expirácií kolegov je prístupný iba dispečingu, financiám a vedeniu."})
            if typ_an == "analyza_kvality":
                return json.dumps({"error": "Kritické zamietnutie: Analýza kvality (ISO 9001) a hodnotenia technikov sú prístupné iba dispečingu, financiám a vedeniu."})
            if typ_an == "vykaz_technika":
                vlastne_meno = _over_identitu_subjektu()
                if not vlastne_meno:
                    return json.dumps({"error": "Kritické zamietnutie: Chýba overená identita technika."})
                kwargs['identifikator'] = vlastne_meno
        if not je_web_admin and typ_an == "systemovy_audit":
            return json.dumps({"error": "Kritické zamietnutie: Systémový audit log je prístupný iba vedeniu."})
        raw_res = tool_erp_hlbkova_analyza_a_vykazy(**kwargs)
        if ma_financie:
            return raw_res
        try:
            parsed = json.loads(raw_res)
            if isinstance(parsed, dict):
                parsed.pop("hodinovy_naklad_eur", None)
                parsed.pop("naklady_na_palivo_eur", None)
                parsed.pop("zamestnanci_so_zavazkami_voci_firme", None)
                parsed.pop("zostatky_nevycerpanych_dovoleniek", None)
                parsed.pop("aktualne_absencie_dnes", None)
                parsed.pop("uchadzaci_v_nabore", None)
                for grp in ("prenajate_stroje_u_klientov", "volne_stroje_pripravene_na_prenajom"):
                    for s_item in parsed.get(grp, []):
                        if isinstance(s_item, dict):
                            s_item.pop("denna_sadzba_eur", None)
                for pol in parsed.get("zoznam_poloziek", []):
                    if isinstance(pol, dict):
                        pol.pop("cena_za_ks_eur", None)
            return json.dumps(parsed)
        except Exception:
            return json.dumps({"error": "Chyba bezpečnostného filtra pri spracovaní dát."})

    def secure_financie_internal(**kwargs):
        kwargs.pop('povolene_sekcie', None)
        if ma_financie:
            return tool_financie_faktury_dodavatelia(**kwargs)

        rola_kod = _zisti_rolu_kod()
        povolene_dodavatel_polia = {
            "nazov", "kontakt_osoba", "oslovenie", "telefon", "email",
            "adresa", "zastupuje_znacky", "web",
        }
        povolene_kosik_polia = {
            "polozka", "stav_skladu_pri_pridani", "potrebne_mnozstvo",
            "preferovany_dodavatel", "cielovy_sklad",
            "datum_pridania_do_kosika", "datum_odoslania", "suvisiaca_objednavka_pdf",
        }

        if rola_kod == Zamestnanec.DISPECER:
            raw_res = _vykonaj_financie_crm_dotaz(
                **kwargs,
                povolene_sekcie={
                    "zakaznici_crm_info",
                    "prijate_objednavky_od_zakaznikov",
                    "dodavatelia_info",
                    "caka_v_kosiku_na_objednanie",
                },
            )
            try:
                parsed = json.loads(raw_res)
                if isinstance(parsed, dict):
                    povolene_zakaznik_polia = {
                        "nazov", "je_interny_profil_nasej_firmy", "kontaktna_osoba",
                        "oslovenie", "telefon", "email", "email_vratnice_sbs",
                        "telefon_vratnica", "web", "vyzaduje_povolenie_vstupu",
                        "instrukcie_vstupu", "ma_vlastnu_sablonu_vstupu",
                        "url_vstupneho_portalu", "adresa", "gps_suradnice",
                        "ma_aktivny_email_opt_out",
                    }
                    povolene_objednavka_polia = {
                        "cislo_interne", "po_number_klienta", "zakaznik", "stav",
                        "priorita", "forma_prijatia", "datum_prijatia", "termin_dodania",
                        "vystavil_za_klienta", "odkaz_na_portal", "zdrojove_ponuky",
                        "vygenerovane_ulohy_ids", "zodpovedny_dispecer",
                        "poziadavka_klienta_text", "typy_poziadovanych_prac",
                        "tyka_sa_zariadeni", "obsahuje_prilohu_subor",
                    }
                    if "zakaznici_crm_info" in parsed:
                        parsed["zakaznici_crm_info"] = _filtruj_polia(parsed["zakaznici_crm_info"], povolene_zakaznik_polia)
                    if "prijate_objednavky_od_zakaznikov" in parsed:
                        parsed["prijate_objednavky_od_zakaznikov"] = _filtruj_polia(parsed["prijate_objednavky_od_zakaznikov"], povolene_objednavka_polia)
                    if "dodavatelia_info" in parsed:
                        parsed["dodavatelia_info"] = _filtruj_polia(parsed["dodavatelia_info"], povolene_dodavatel_polia)
                    if "caka_v_kosiku_na_objednanie" in parsed:
                        parsed["caka_v_kosiku_na_objednanie"] = _filtruj_polia(parsed["caka_v_kosiku_na_objednanie"], povolene_kosik_polia)
                return json.dumps(parsed)
            except Exception:
                return json.dumps({"error": "Chyba bezpečnostného filtra pri spracovaní dát."})

        if rola_kod == Zamestnanec.SKLADNIK:
            raw_res = _vykonaj_financie_crm_dotaz(
                **kwargs,
                povolene_sekcie={"dodavatelia_info", "caka_v_kosiku_na_objednanie"},
            )
            try:
                parsed = json.loads(raw_res)
                if isinstance(parsed, dict):
                    if "dodavatelia_info" in parsed:
                        parsed["dodavatelia_info"] = _filtruj_polia(parsed["dodavatelia_info"], povolene_dodavatel_polia)
                    if "caka_v_kosiku_na_objednanie" in parsed:
                        parsed["caka_v_kosiku_na_objednanie"] = _filtruj_polia(parsed["caka_v_kosiku_na_objednanie"], povolene_kosik_polia)
                return json.dumps(parsed)
            except Exception:
                return json.dumps({"error": "Chyba bezpečnostného filtra pri spracovaní dát."})

        if rola_kod == Zamestnanec.OBCHODNIK:
            raw_res = _vykonaj_financie_crm_dotaz(
                **kwargs,
                povolene_sekcie={
                    "zakaznici_crm_info",
                    "cenove_ponuky",
                    "prijate_objednavky_od_zakaznikov",
                },
            )
            try:
                parsed = json.loads(raw_res)
                if isinstance(parsed, dict):
                    povolene_zakaznik_polia_ob = {
                        "nazov", "je_interny_profil_nasej_firmy", "ico", "kontaktna_osoba",
                        "oslovenie", "telefon", "email", "web", "b2b_zlava_percenta",
                        "predvolena_splatnost_dni", "reklamacna_lehota_dni",
                        "fakturacne_udaje", "adresa", "gps_suradnice",
                        "ma_aktivny_email_opt_out",
                    }
                    povolene_ponuka_polia_ob = {
                        "cislo_ponuky", "zakaznik", "zariadenie", "typy_prac",
                        "suma_bez_dph_pred_zlavou_eur", "suma_bez_dph_eur",
                        "suma_dph_eur", "suma_s_dph_eur", "hodnota_zlavy_eur",
                        "stav", "vystavil_meno", "vystavene", "prenos_danovej_povinnosti",
                        "cas_schvalenia_klientom", "meno_schvalitela",
                        "ma_vygenerovane_pdf", "globalna_zlava_percento",
                        "uvodny_text", "zaverecne_podmienky", "obsah_ponuky",
                    }
                    povolene_objednavka_polia_ob = {
                        "cislo_interne", "po_number_klienta", "zakaznik", "stav",
                        "priorita", "forma_prijatia", "suma_bez_dph_eur",
                        "datum_prijatia", "termin_dodania", "vystavil_za_klienta",
                        "zdrojove_ponuky", "zodpovedny_dispecer",
                        "poziadavka_klienta_text", "typy_poziadovanych_prac",
                        "tyka_sa_zariadeni",
                    }
                    if "zakaznici_crm_info" in parsed:
                        parsed["zakaznici_crm_info"] = _filtruj_polia(parsed["zakaznici_crm_info"], povolene_zakaznik_polia_ob)
                    if "cenove_ponuky" in parsed:
                        parsed["cenove_ponuky"] = _filtruj_polia(parsed["cenove_ponuky"], povolene_ponuka_polia_ob)
                    if "prijate_objednavky_od_zakaznikov" in parsed:
                        parsed["prijate_objednavky_od_zakaznikov"] = _filtruj_polia(parsed["prijate_objednavky_od_zakaznikov"], povolene_objednavka_polia_ob)
                return json.dumps(parsed)
            except Exception:
                return json.dumps({"error": "Chyba bezpečnostného filtra pri spracovaní dát."})

        # 🔒 FAIL-CLOSED: Technici ('TE') aj neznáme roly majú prístup zamietnutý pred DB dopytom
        return json.dumps({"error": "Kritické zamietnutie: Nemáte bezpečnostné oprávnenie na finančný a obchodný modul."})

    # Tool Routing Máp + deklarácie do Gemini API podľa role používateľa
    if is_sukromna_osoba:
        tool_map: dict[str, Any] = {
            "tool_fleet_vozidla_jazdy": secure_driver_fleet,
        }
        tools_list: list[Any] = [tool_fleet_vozidla_jazdy]
    elif is_b2b:
        tool_map = {
            "tool_zariadenia_a_technicke_parametre": secure_b2b_zariadenia,
            "tool_operativa_servis_a_ulohy": secure_b2b_operativa,
            "tool_financie_faktury_dodavatelia": secure_b2b_financie,
        }
        tools_list = [
            tool_zariadenia_a_technicke_parametre,
            tool_operativa_servis_a_ulohy,
            tool_financie_faktury_dodavatelia,
        ]
    else:
        je_validny_interny = bool(
            is_superuser
            or _je_vedenie_firmy(typ_pomeru, rola_pouzivatela, is_superuser)
            or _zisti_rolu_kod() is not None
        )
        if not force_json and not je_validny_interny:
            return {
                "text": "<b>🔒 Prístup zamietnutý:</b> Chýba overená pracovná rola alebo typ používateľa v systéme.",
                "in_tokens": 0,
                "out_tokens": 0,
                "cached_tokens": 0,
            }

        def secure_zariadenia_internal(**kwargs):
            raw_res = tool_zariadenia_a_technicke_parametre(**kwargs)
            if ma_financie:
                return raw_res
            try:
                parsed = json.loads(raw_res)
                if isinstance(parsed, dict):
                    for s in parsed.get("data", []):
                        if isinstance(s, dict):
                            s.pop("cena_prenajmu_na_den_eur", None)
                return json.dumps(parsed)
            except Exception:
                return json.dumps({"error": "Chyba bezpečnostného filtra pri spracovaní dát."})

        def secure_operativa_internal(**kwargs):
            raw_res = tool_operativa_servis_a_ulohy(**kwargs)
            if je_web_admin:
                return raw_res
            try:
                parsed = json.loads(raw_res)
                if isinstance(parsed, dict):
                    for z in parsed.get("nedavna_historia_servisov", []):
                        if isinstance(z, dict):
                            z.pop("skryta_ai_analyza_tco", None)
                    for p in parsed.get("povolenia_na_vstup", []):
                        if isinstance(p, dict):
                            p.pop("ip_schvalitela_sbs", None)
                return json.dumps(parsed)
            except Exception:
                return json.dumps({"error": "Chyba bezpečnostného filtra pri spracovaní dát."})

        def secure_fleet_internal(**kwargs):
            raw_res = tool_fleet_vozidla_jazdy(**kwargs)
            if ma_financie:
                return raw_res
            try:
                parsed = json.loads(raw_res)
                if isinstance(parsed, dict):
                    for v in parsed.get("vozidla_stav", []):
                        if isinstance(v, dict):
                            v.pop("celkove_naklady_palivo_eur", None)
                            v.pop("vlastna_sadzba_za_km_eur", None)
                    for t in parsed.get("nedavne_tankovania", []):
                        if isinstance(t, dict):
                            t.pop("cena_celkom_eur", None)
                            t.pop("suma_dph_eur", None)
                            t.pop("cena_bez_dph_eur", None)
                return json.dumps(parsed)
            except Exception:
                return json.dumps({"error": "Chyba bezpečnostného filtra pri spracovaní dát."})

        def secure_read_document(**kwargs):
            if not kwargs.get('nazov_pola'):
                return json.dumps({"error": "Musíte zadať názov poľa dokumentu."})
            deny = _over_povolene_doc_pole(kwargs.get('model_name'), kwargs.get('nazov_pola'))
            if deny:
                return deny
            return tool_precitaj_dokument(**kwargs)

        def secure_render_page(**kwargs):
            if not kwargs.get('nazov_pola'):
                return json.dumps({"error": "Musíte zadať názov poľa dokumentu."})
            deny = _over_povolene_doc_pole(kwargs.get('model_name'), kwargs.get('nazov_pola'))
            if deny:
                return deny
            return tool_vygeneruj_obrazok_strany(**kwargs)

        def secure_search_email(**kwargs):
            if je_web_admin or ma_financie:
                return _vykonaj_email_dotaz(**kwargs)
            if _zisti_rolu_kod() == Zamestnanec.DISPECER:
                return _vykonaj_email_dotaz(**kwargs, vylucit_kategorie=['Účtovníctvo / Faktúry', 'HR / Interné', 'Nezaradené'])
            return json.dumps({"error": "Kritické zamietnutie: Nemáte bezpečnostné oprávnenie na čítanie firemných e-mailov."})

        tool_map = {
            "tool_sklad_a_katalog": secure_sklad_internal,
            "tool_zariadenia_a_technicke_parametre": secure_zariadenia_internal,
            "tool_operativa_servis_a_ulohy": secure_operativa_internal,
            "tool_financie_faktury_dodavatelia": secure_financie_internal,
            "tool_fleet_vozidla_jazdy": secure_fleet_internal,
            "tool_hr_a_mzdy": secure_hr_internal,
            "tool_systemove_informacie_a_maily": secure_sys_info,
            "tool_manazerske_statistiky": secure_stats_internal,
            "tool_erp_hlbkova_analyza_a_vykazy": secure_hlbkova_internal,
            "tool_precitaj_dokument": secure_read_document,
            "tool_vygeneruj_obrazok_strany": secure_render_page,
            "tool_historicky_stav_skladu": tool_historicky_stav_skladu,
            "tool_vyhladaj_a_precitaj_email": secure_search_email,
            "tool_vypocitaj_trasu_a_logistiku": tool_vypocitaj_trasu_a_logistiku,
            "tool_priprav_koncept_emailu": secure_mail_draft_manager,
        }
        rola_kod_tools = _zisti_rolu_kod()
        tools_list = [
            tool_sklad_a_katalog,
            tool_zariadenia_a_technicke_parametre,
            tool_operativa_servis_a_ulohy,
            tool_fleet_vozidla_jazdy,
            tool_hr_a_mzdy,
            tool_systemove_informacie_a_maily,
            tool_manazerske_statistiky,
            tool_erp_hlbkova_analyza_a_vykazy,
            tool_precitaj_dokument,
            tool_vygeneruj_obrazok_strany,
            tool_historicky_stav_skladu,
            tool_vypocitaj_trasu_a_logistiku,
        ]
        if ma_financie or rola_kod_tools in {Zamestnanec.DISPECER, Zamestnanec.SKLADNIK, Zamestnanec.OBCHODNIK}:
            tools_list.append(tool_financie_faktury_dodavatelia)
        if je_web_admin or ma_financie or rola_kod_tools == Zamestnanec.DISPECER:
            tools_list.extend([tool_vyhladaj_a_precitaj_email, tool_priprav_koncept_emailu])
        if je_web_admin:
            tool_map.update({
                "tool_spravuj_verejny_web": secure_web_manager,
                "tool_spravuj_viditelnost_entit": secure_visibility_manager,
                "tool_najdi_a_nastav_ilustracny_obrazok": secure_image_manager,
                "tool_publikuj_draft_webu": secure_publish_manager,
                "tool_zahod_draft_webu": secure_discard_manager,
                "tool_vylepsi_texty_v_erp": secure_copywriter_manager,
                "tool_spravuj_clanky_a_blog": secure_blog_manager,
                "tool_spravuj_faq": secure_faq_manager,
                "tool_vytvor_pripadovu_studiu_z_vyjazdu": secure_case_study_manager,
                "tool_spravuj_pracovne_pozicie": secure_career_manager,
                "tool_priprav_social_media_post": secure_social_media_manager,
            })
            tools_list.extend([
                tool_spravuj_verejny_web,
                tool_spravuj_viditelnost_entit,
                tool_najdi_a_nastav_ilustracny_obrazok,
                tool_publikuj_draft_webu,
                tool_zahod_draft_webu,
                tool_vylepsi_texty_v_erp,
                tool_spravuj_clanky_a_blog,
                tool_spravuj_faq,
                tool_vytvor_pripadovu_studiu_z_vyjazdu,
                tool_spravuj_pracovne_pozicie,
                tool_priprav_social_media_post,
            ])
    
    # Pamäťový manažment
    cista_historia = _spravuj_pamat_historie(history_text)

    def get_or_create_gemini_context_cache(client, model: str, cache_tier: str, static_prompt: str, tools_list: list) -> Optional[str]:
        """
        🛡️ ISAS CONTEXT CACHE MANAGER:
        Spravuje perzistentnú vyrovnávaciu pamäť v Google Cloud s prepojením na lokálny Redis.
        Znižuje vstupné náklady o 90 % ($0.075 namiesto $0.75 / 1M) a zrýchľuje odozvu pod 1 sekundu.
        """
        # 🛡️ EDGE CASE OCHRANA: B2B, externé osoby a čistý JSON majú prompt hlboko pod 32 768 tokenov.
        # Google API by vytvorenie cache odmietlo (400 InvalidArgument). Tieto role bežia výhradne bez cache.
        # Modely bez podpory explicitnej cache (napr. gemini-4-neon) používajú výhradne implicitné cachovanie.
        normalized_model = str(model or "").lower().strip().removeprefix("models/")
        if cache_tier in ["b2b", "externa_osoba", "json"] or normalized_model == "gemini-4-neon":
            return None

        tool_names = ",".join(sorted(getattr(t, '__name__', str(t)) for t in (tools_list or [])))
        prompt_sig = hashlib.sha256(f"{static_prompt}|{tool_names}".encode('utf-8')).hexdigest()[:12]
        redis_key = f"gemini_context_cache_name_{cache_tier}_{model.replace('/', '_')}_{prompt_sig}"
        cached_name = cache.get(redis_key)

        if cached_name:
            try:
                # Overíme v Google Cloud, či cache stále žije
                existing_cache = client.caches.get(name=cached_name)
                if existing_cache:
                    return cached_name
            except Exception:
                cache.delete(redis_key)

        # Pokus o vytvorenie novej Google Context Cache
        try:
            cache_tools = None
            if tools_list:
                api_client = getattr(client, '_api_client', client)
                function_declarations = []
                explicit_tools = []
                for t in tools_list:
                    if isinstance(t, types.Tool):
                        explicit_tools.append(t)
                    elif isinstance(t, types.FunctionDeclaration):
                        function_declarations.append(t)
                    else:
                        function_declarations.append(
                            types.FunctionDeclaration.from_callable(client=api_client, callable=t, use_json_schema=True)
                        )
                cache_tools = explicit_tools
                if function_declarations:
                    cache_tools.append(types.Tool(function_declarations=function_declarations))

            new_cache = client.caches.create(
                model=model,
                config=types.CreateCachedContentConfig(
                    system_instruction=static_prompt,
                    tools=cache_tools,
                    ttl="3600s"  # 1 hodina retencie v Google Cloud
                )
            )

            if new_cache and hasattr(new_cache, 'name'):
                cache.set(redis_key, new_cache.name, timeout=3500)
                logger.info(f"🚀 Vytvorená nová Gemini Context Cache: {new_cache.name} (Tier: {cache_tier})")
                return new_cache.name
        except Exception as cache_create_err:
            # Fail-Safe: Ak nastane API obmedzenie alebo je tokenov menej ako 32 768, ticho prepneme na fallback
            logger.warning(f"Context Caching nedostupný (Fallback na štandardné volanie): {cache_create_err}")

        return None

    # Dynamické načítanie identity klienta pre AI kontext
    identita = FiremnaIdentita.objects.first()
    aktualna_firma = identita.nazov_spolocnosti if identita else "našu spoločnosť"

    # Rozdelenie promptu na statickú časť (pre Cache) a dynamickú (čas, identita, história)
    if force_json:
        system_instruction = (
            "Si backendový dátový extraktor. Tvojou jedinou úlohou je vrátiť validný JSON podľa zadania používateľa. "
            "ZÁKAZ POUŽÍVAŤ HTML TAGY. Zákaz markdown blokov (```json). Vráť VÝLUČNE text začínajúci na { a končiaci na }."
        )
        staticky_prompt = system_instruction
        dynamicky_kontext = ""
        cache_tier = "json"
        response_type = "application/json"
    else:
        staticky_prompt, dynamicky_kontext, cache_tier = zostav_staticky_a_dynamicky_prompt(
            aktualna_firma=aktualna_firma,
            aktualny_pouzivatel=aktualny_pouzivatel,
            rola_pouzivatela=rola_pouzivatela,
            typ_pomeru=typ_pomeru,
            is_superuser=is_superuser,
            ma_pristup_k_financiam=ma_pristup_k_financiam,
            cista_historia=cista_historia
        )
        system_instruction = staticky_prompt
        response_type = "text/plain"

    uploaded_files_cloud = []
    client = None  # 🛡️ Ochrana pred UnboundLocalError v bloku finally

    try:
        api_key = get_gemini_api_key()
        gemini_model = getattr(settings, 'GEMINI_MODEL', None)

        if not api_key or not gemini_model:
            logger.warning("ask_isas_ai: GEMINI API kľúč alebo GEMINI_MODEL nie je nakonfigurovaný.")
            if force_json:
                return {"text": "{}", "in_tokens": 0, "out_tokens": 0, "cached_tokens": 0}
            return {
                "text": "<b>🚨 CHYBA:</b> AI služba je momentálne nedostupná (chýba konfigurácia API kľúča alebo modelu).",
                "in_tokens": 0,
                "out_tokens": 0,
                "cached_tokens": 0
            }
            
        client = genai.Client(api_key=api_key)

        # ⚡ POKUS O ZÍSKANIE ALEBO VYTVORENIE GEMINI CONTEXT CACHE
        cached_content_name = None
        if not force_json:
            cached_content_name = get_or_create_gemini_context_cache(
                client=client,
                model=gemini_model,
                cache_tier=cache_tier,
                static_prompt=staticky_prompt,
                tools_list=tools_list
            )

        # 🔒 ARCHITEKTÚRA KONFIGURÁCIE: Ak máme Cache, NESMIEME duplicitne posielať tools ani system_instruction!
        if cached_content_name:
            config = types.GenerateContentConfig(
                cached_content=cached_content_name,
                thinking_config=types.ThinkingConfig(thinking_level=getattr(settings, 'GEMINI_THINKING_LEVEL', types.ThinkingLevel.MEDIUM) or types.ThinkingLevel.MEDIUM),
                response_mime_type=response_type
            )

        else:
            config = types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_level=getattr(settings, 'GEMINI_THINKING_LEVEL', types.ThinkingLevel.MEDIUM) or types.ThinkingLevel.MEDIUM),
                system_instruction=system_instruction,
                tools=None if force_json else tools_list,
                response_mime_type=response_type
            )

        chat = client.chats.create(
            model=gemini_model,
            config=config
        )

        contents_to_send: list[Any] = []
        
        # 🛡️ 100% GARANCIA KONTEXTU: Dynamický kontext (čas, identita, história) sa posiela VŽDY!
        # Či už Context Cache beží, alebo zlyhala a bežíme cez fallback.
        if dynamicky_kontext:
            contents_to_send.append(f"[DYNAMICKÝ KONTEXT SYSTÉMU]\n{dynamicky_kontext}")

        # 🎙️ Spracovanie audio správy (s ochranou proti sieťovému zlyhaniu)
        if audio_path and os.path.exists(audio_path):
            try:
                up_audio = client.files.upload(file=audio_path)
                uploaded_files_cloud.append(up_audio)
                if not force_json: contents_to_send.append("Používateľ poslal túto zvukovú správu:")
                contents_to_send.append(up_audio)
            except Exception as audio_err:
                logger.error(f"Chyba pri nahrávaní audio správy do Gemini: {audio_err}")
                if not force_json: contents_to_send.append("⚠️ [Zvukovú správu sa nepodarilo nahrať kvôli sieťovej chybe]")

        # 📁 Multimodálne spracovanie priložených súborov (Obrázky, PDF, Videá, Dokumenty)
        if file_paths:
            for f_path in file_paths:
                if os.path.exists(f_path):
                    try:
                        up_file = client.files.upload(file=f_path)
                        
                        # ⏳ OCHRANA PRE VIDEÁ A VEĽKÉ PDF: Čakanie na stav ACTIVE v Google Cloud
                        max_wait_seconds = 30
                        waited = 0
                        while up_file.state is not None and up_file.state.name == "PROCESSING" and waited < max_wait_seconds:
                            time.sleep(1.5)
                            waited += 2
                            if up_file.name:
                                up_file = client.files.get(name=up_file.name)
                            else:
                                break
                            
                        if up_file.state is not None and up_file.state.name == "FAILED":
                            logger.error(f"Google Gemini odmietol spracovať súbor {f_path}")
                            continue

                        uploaded_files_cloud.append(up_file)
                        contents_to_send.append(up_file)
                    except Exception as upload_err:
                        logger.error(f"Chyba pri uploade súboru {f_path} do Gemini: {upload_err}")

        if user_question:
            contents_to_send.append(user_question)

        # Ak nemáme žiadny obsah, vrátime chybu
        if not contents_to_send:
            msg = "{}" if force_json else "Nebol zadaný žiadny text ani platný súbor."
            return {"text": msg, "in_tokens": 0, "out_tokens": 0, "cached_tokens": 0}

        response = chat.send_message(contents_to_send)
        
        # =====================================================================
        # 🔄 THE PING-PONG EXECUTION LOOP (Tool Routing Trap Fix + Auto Pagination)
        # =====================================================================
        MAX_ROUNDS = 5
        rounds = 0
        
        # 💰 100% CUMULATIVE TOKEN AUDIT: Akumulácia nákladov naprieč všetkými kolami vrátane CACHED tokenov
        total_in_tokens = 0
        total_out_tokens = 0
        total_cached_tokens = 0

        if hasattr(response, 'usage_metadata') and response.usage_metadata:
            total_in_tokens += getattr(response.usage_metadata, 'prompt_token_count', 0) or 0
            total_out_tokens += getattr(response.usage_metadata, 'candidates_token_count', 0) or 0
            total_cached_tokens += getattr(response.usage_metadata, 'cached_content_token_count', 0) or 0
        
        while response.function_calls and rounds < MAX_ROUNDS:
            function_responses: list[Any] = []
            
            for fc in response.function_calls:
                func_name = fc.name or ""
                func_args = fc.args if fc.args else {}
                if hasattr(func_args, 'to_dict'): 
                    func_args = func_args.to_dict()
                elif isinstance(func_args, str): 
                    try: func_args = json.loads(func_args)
                    except Exception: func_args = {}
                elif not isinstance(func_args, dict):
                    func_args = {}
                
                func = tool_map.get(func_name)
                if func:
                    try:
                        result_str = func(**func_args)
                    except Exception as exec_error:
                        logger.error(f"Error v nástroji {func_name}: {exec_error}", exc_info=True)
                        result_str = json.dumps({"error": f"Chyba pri vykonávaní nástroja: {str(exec_error)}"})
                else:
                    result_str = json.dumps({"error": f"Nástroj {func_name} neexistuje."})

                # 🛡️ PREDÍDENIE DVOJITÉMU JSON ESCAPOVANIU PRE GEMINI API
                try:
                    parsed_result = json.loads(result_str) if isinstance(result_str, str) else result_str
                except Exception:
                    parsed_result = {"output": str(result_str)}

                function_responses.append(
                    types.Part.from_function_response(
                        name=func_name, 
                        response={"response": parsed_result}
                    )
                )

            try:
                response = chat.send_message(function_responses)
                
                # 💰 Priebežné sčítanie tokenov za každé kolo volania nástrojov
                if hasattr(response, 'usage_metadata') and response.usage_metadata:
                    total_in_tokens += getattr(response.usage_metadata, 'prompt_token_count', 0) or 0
                    total_out_tokens += getattr(response.usage_metadata, 'candidates_token_count', 0) or 0
                    total_cached_tokens += getattr(response.usage_metadata, 'cached_content_token_count', 0) or 0
            except Exception as loop_err:
                logger.error(f"Chyba počas odosielania odpovedí nástrojov do Gemini: {loop_err}", exc_info=True)
                break

            rounds += 1

        if rounds >= MAX_ROUNDS:
            logger.warning("AI dosiahla MAX_ROUNDS (5). Zastavená proti zacykleniu.")
        # =====================================================================
        
        # 🛡️ BEZPEČNÁ EXTRAKCIA TEXTU Z CANDIDATES (Zabráni pádu na response.text)
        res = ""
        try:
            if hasattr(response, 'candidates') and response.candidates:
                first_candidate = response.candidates[0]
                if hasattr(first_candidate, 'content') and first_candidate.content and first_candidate.content.parts:
                    for part in first_candidate.content.parts:
                        if getattr(part, 'thought', False):
                            continue  # Preskočíme interné thinking tokeny modelu
                        if hasattr(part, 'text') and part.text:
                            res += part.text
            
            if not res and hasattr(response, 'text'):
                res = response.text or ""
        except Exception as text_extract_err:
            logger.warning(f"Fallback pri extrakcii textu: {text_extract_err}")
            res = getattr(response, 'text', '') or ""
        
        # 💰 Presné agregované tokeny pre AIAuditLog
        in_tokens = total_in_tokens
        out_tokens = total_out_tokens
        cached_tokens = total_cached_tokens

        if force_json:
            if not res: return {"text": "{}", "in_tokens": in_tokens, "out_tokens": out_tokens, "cached_tokens": cached_tokens}
            res = res.strip()
            if res.startswith("```json"): res = res[7:]
            if res.startswith("```"): res = res[3:]
            if res.endswith("```"): res = res[:-3]
            return {"text": res.strip(), "in_tokens": in_tokens, "out_tokens": out_tokens, "cached_tokens": cached_tokens}

        if res:
            res = re.sub(r'(?m)^\s{0,3}#{1,6}\s+', '', res)
            res = re.sub(r'(?m)^(\s*)\*\s+', r'\1• ', res)
            res = res.replace('**', '')
            return {"text": res.strip(), "in_tokens": in_tokens, "out_tokens": out_tokens, "cached_tokens": cached_tokens}
            
        return {"text": "Požiadavka bola spracovaná, ale asistent nevrátil textovú odpoveď.", "in_tokens": in_tokens, "out_tokens": out_tokens, "cached_tokens": cached_tokens}

    except Exception as e:
        logger.critical(f"NEURAL BRAIN FAILURE: {e}", exc_info=True)
        err_msg = "{}" if force_json else "<b>🚨 CHYBA AI SLUŽBY:</b>\nPožiadavku sa nepodarilo spracovať. Skúste to znova alebo kontaktujte administrátora."
        return {"text": err_msg, "in_tokens": 0, "out_tokens": 0, "cached_tokens": 0}

    finally:
        # 🧹 ZMAZANIE VŠETKÝCH DOČASNÝCH SÚBOROV Z GOOGLE CLOUD STORAGE
        if client and uploaded_files_cloud:
            for cloud_file in uploaded_files_cloud:
                try: 
                    if cloud_file.name:
                        client.files.delete(name=cloud_file.name)
                except Exception as cleanup_error: 
                    logger.error(f"Nepodarilo sa zmazať Google Cloud súbor {cloud_file.name}: {cleanup_error}")