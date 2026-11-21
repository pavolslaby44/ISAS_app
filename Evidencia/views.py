import calendar
import decimal
import hashlib
import shutil
import json
import logging
import os
import re
import traceback
import uuid
from datetime import date, datetime, timedelta
from io import BytesIO

from google import genai
import openpyxl
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
import requests

from celery.result import AsyncResult
from decimal import Decimal, InvalidOperation
from django.apps import apps
from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.mail import EmailMultiAlternatives, get_connection
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.db import transaction
from django.db.models import Avg, Count, ExpressionWrapper, F, FloatField, Max, Prefetch, Q, Sum, fields
from django.db.models.functions import Coalesce
from django.http import FileResponse, Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from google.genai import types

from ISASaplikacia.models import FiremnaIdentita, GlobalneNastavenia, StatLokalizacia
from ISASaplikacia.utils import get_gemini_api_key

from .forms import CATEGORY_CHOICES, CATEGORY_MAP, ERPAccountantForm, PARENT_MODELS
from .models import (
    AIAuditLog, AlarmLog, CenovaPonuka, CenovyKatalog, ERPExportNastavenia,
    Filialka, ImportDatTask, KosikSPolozkamiNaDoobjednanie, Nepritomnost,
    PlanovanyServisZariadenia, PolozkaCenovejPonuky, PolozkaPredpisu,
    PolozkaVystavenejFaktury, PovolenieNaVstup, PrijataObjednavka,
    ServisnyPredpis, ServisnyZaznam, ServisovaneZariadenie, SignalDictionary, Sklad,
    StockMovement, TelemetryHistory, TypServisuPrace, Uloha, Vozidlo,
    VystavenaFaktura, Zakaznik, Zamestnanec
)
from .tasks import (
    execute_modbus_write_task,
    generuj_ponuku_pdf_task,
    generuj_protokol_pdf_task,
    odosli_sbs_notifikacie_task,
    process_iot_payload_task,
    spracuj_ai_chat_task,
    task_odoslat_notifikaciu_o_nastaveni_erp,
    task_odosli_notifikaciu_presun,
    vygeneruj_pdf_a_notifikuj_fakturu_task,
    task_notifikuj_schvalenie_ponuky
)
from .industry_loader import (
    get_active_parameter_reverse_names,
    get_zariadenie_parametre,
    SAMPLE_IMPORT_DATA,
    ZARIADENIA_CHOICES,
    CATEGORY_CHOICES,
    FLAT_CATEGORY_MAP,
    INDUSTRY_TERMS,
    HAS_ENERGY_ESG_AUDIT
)
from .utils import generuj_ai_sepkar_analyzu, ISASProductMetadataResolver
from .utils_helper import (
    LedgerKalkulacka, d_round, get_client_ip, get_firemna_mena, isas_ai_logistika_trasy,
    ocisti_nazov_od_jednotky, send_isas_stock_notifications,
    send_telegram_message
)
from .utils_compliance import get_zariadenie_compliance_audit, get_zariadenie_live_diagnostika, get_iso_pre_objekt, to_date
from .industry_loader import get_all_industry_terms, get_industry_term

logger = logging.getLogger(__name__)

PERIOD_MAP = {"7": 7, "30": 30, "90": 90, "180": 180, "365": 365}

@staff_member_required
def dashboard_reklamacie(request):
    now = timezone.now()

    # --- 1. SLA MONITOR (Dynamické sledovanie lehôt bez N+1) ---
    aktivne_reklamacie = Uloha.objects.filter(
        je_reklamacia=True, 
        dokoncene=False
    ).select_related('zakaznik').prefetch_related('technici', 'zariadenia', 'typ_servisu', 'zdrojove_objednavky_ulohy')

    gn_obj = GlobalneNastavenia.objects.first()
    globalna_lehota = getattr(gn_obj, 'standardna_lehota_reklamacie_dni', None) if gn_obj else None

    sla_data = []
    kritickych_sla = 0
    dnes = now.date()

    for u in aktivne_reklamacie:
        # 🛡️ 1. DÁTUM ŠTARTU: Objednávka -> Úloha prijatie -> Úloha začiatok
        nadradena_obj = u.zdrojove_objednavky_ulohy.first() if hasattr(u, 'zdrojove_objednavky_ulohy') else None
        start_raw = u.datum_prijatia_reklamacie or (nadradena_obj.datum_prijatia if nadradena_obj else None) or u.zaciatok
        d_start = to_date(start_raw)
        dni_bezi = max(0, (dnes - d_start).days) if d_start else 0

        # 🛡️ 2. PRIORITA TERMÍNU: Záväzný termín z objednávky prebíja orientačné výpočty
        zavazny_termin = to_date(nadradena_obj.termin_dodania) if (nadradena_obj and nadradena_obj.termin_dodania) else None

        zak = u.zakaznik
        iso_krajiny = get_iso_pre_objekt(zak) if zak else None
        stat_lehota = None
        if iso_krajiny:
            stat_obj = StatLokalizacia.get_for_iso(iso_krajiny)
            stat_lehota = getattr(stat_obj, 'lehota_reklamacie_dni', None) if stat_obj else None

        platna_lehota = getattr(zak, 'reklamacna_lehota_dni', None) or stat_lehota or globalna_lehota

        # 🛡️ 3. KALKULÁCIA LEHOTY A EXSPIRÁCIE
        cielovy_termin = zavazny_termin
        if not cielovy_termin and platna_lehota is not None and platna_lehota > 0 and d_start:
            kandidat_termin = d_start + timedelta(days=platna_lehota)
            cielovy_termin = FiremnaIdentita.posun_na_pracovny_den(kandidat_termin, iso=iso_krajiny) if hasattr(FiremnaIdentita, 'posun_na_pracovny_den') else kandidat_termin

        if cielovy_termin:
            dni_do_konca = (cielovy_termin - dnes).days
            je_po_lehote = dni_do_konca < 0
            dni_po_lehote = abs(dni_do_konca) if je_po_lehote else 0
            zostava_dni = dni_do_konca if not je_po_lehote else 0

            # Percentuálny progress voči reálnemu rozpätiu dní
            celkovy_rozsah = max(1, (cielovy_termin - d_start).days) if d_start else (platna_lehota or 1)
            percento = max(0, min(int((dni_bezi / celkovy_rozsah) * 100), 100))

            if je_po_lehote or percento > 80:
                status_color = "#ef4444"
                status_ikona = "🔴"
                kritickych_sla += 1
            elif percento >= 50:
                status_color = "#f59e0b"
                status_ikona = "🟠"
            else:
                status_color = "#10b981"
                status_ikona = "🟢"
        else:
            je_po_lehote = False
            zostava_dni = None
            dni_po_lehote = 0
            percento = None
            status_color = "#64748b"
            status_ikona = "⚪"

        priorita_raw = 'STREDNA'
        typy_prac = u.typ_servisu.all()
        if any(t.priorita == 'VYSOKA' for t in typy_prac):
            priorita_raw = 'VYSOKA'
        elif any(t.priorita == 'NIZKA' for t in typy_prac) and not any(t.priorita == 'STREDNA' for t in typy_prac):
            priorita_raw = 'NIZKA'

        priorita_dict = {'NIZKA': '🟢 Nízka', 'STREDNA': '🟡 Stredná', 'VYSOKA': '🔴 Vysoká'}

        sla_data.append({
            'id': u.id,
            'zakaznik': u.zakaznik.Nazov_spolocnosti if u.zakaznik else "Neznámy",
            'zariadenie': ", ".join([str(z.model_zariadenia or z.nazov) for z in u.zariadenia.all() if (z.model_zariadenia or z.nazov)]) or "Nezaradené",
            'technici': ", ".join([str(t.meno) for t in u.technici.all() if t.meno]),
            'priorita': priorita_dict.get(priorita_raw, priorita_raw),
            'dni': dni_bezi,
            'platna_lehota': platna_lehota,
            'zostava_dni': zostava_dni,
            'dni_po_lehote': dni_po_lehote,
            'je_po_lehote': je_po_lehote,
            'percento': percento,
            'color': status_color,
            'ikona': status_ikona
        })
    
    sla_data.sort(key=lambda x: x['dni'], reverse=True)

    # --- 2. STROJ ČASU: ISO 9001 HALL OF SHAME ---
    chybne_zaznamy = ServisnyZaznam.objects.filter(
        koren_problemu='CHYBA_TECHNIKA'
    ).select_related('zariadenie')

    # ELIMINÁCIA N+1: Prednačítanie celej potrebnej histórie a technikov naraz
    zariadenia_ids = [z.zariadenie_id for z in chybne_zaznamy if z.zariadenie_id]
    historicke_zaznamy = ServisnyZaznam.objects.filter(
        zariadenie_id__in=zariadenia_ids
    ).prefetch_related('technici').order_by('zariadenie_id', '-vytvorene')

    historia_mapa = {}
    for hz in historicke_zaznamy:
        historia_mapa.setdefault(hz.zariadenie_id, []).append(hz)

    technik_chyby = {}
    for zaznam in chybne_zaznamy:
        if not zaznam.zariadenie_id:
            continue
            
        # Hľadanie predošlého záznamu v prednačítanej mape (O(1) / O(N) v pamäti, 0 DB dopytov)
        predosly_zaznam = None
        z_vytvorene = timezone.localtime(zaznam.vytvorene) if (zaznam.vytvorene and timezone.is_aware(zaznam.vytvorene)) else zaznam.vytvorene
        z_cmp = z_vytvorene.replace(tzinfo=None) if (z_vytvorene and getattr(z_vytvorene, 'tzinfo', None)) else z_vytvorene
        for hz in historia_mapa.get(zaznam.zariadenie_id, []):
            hz_vytvorene = timezone.localtime(hz.vytvorene) if (hz.vytvorene and timezone.is_aware(hz.vytvorene)) else hz.vytvorene
            hz_cmp = hz_vytvorene.replace(tzinfo=None) if (hz_vytvorene and getattr(hz_vytvorene, 'tzinfo', None)) else hz_vytvorene
            if hz_cmp and z_cmp and hz_cmp < z_cmp:
                predosly_zaznam = hz
                break

        technici_chyby_list = predosly_zaznam.technici.all() if predosly_zaznam else zaznam.technici.all()
        for technik in technici_chyby_list:
            tid = technik.id
            if tid not in technik_chyby:
                t_meno_str = f"{(technik.meno or '').strip()} {(technik.priezvisko or '').strip()}".strip()
                technik_chyby[tid] = {
                    'meno': t_meno_str or "Neznámy technik", 
                    'pocet': 0
                }
            technik_chyby[tid]['pocet'] += 1

    rebricek_technikov = sorted(technik_chyby.values(), key=lambda x: x['pocet'], reverse=True)

    # --- 3. ŠTATISTIKA KOREŇOV PROBLÉMOV ---
    statistika_korenov = ServisnyZaznam.objects.exclude(
        koren_problemu='NEHODNOTENE'
    ).values('koren_problemu').annotate(pocet=Count('id')).order_by('-pocet')
    
    korene_dict = dict(ServisnyZaznam.KOREN_PROBLEMU_CHOICES)
    korene_data = []
    celkom_reklamacii = 0
    opravnene_pocet = 0
    neopravnene_pocet = 0
    
    opravnene_kody = ['CHYBA_TECHNIKA', 'VADNY_DIEL', 'SOFTWARE']
    neopravnene_kody = ['CHYBA_OBSLUHY', 'ZANEDBANA_UDRZBA', 'EXTERNY_VPLYV', 'OPOTREBENIE']
    
    for s in statistika_korenov:
        celkom_reklamacii += s['pocet']
        if s['koren_problemu'] in opravnene_kody:
            opravnene_pocet += s['pocet']
        elif s['koren_problemu'] in neopravnene_kody:
            neopravnene_pocet += s['pocet']
            
        korene_data.append({
            'nazov': korene_dict.get(s['koren_problemu'], s['koren_problemu']), 
            'pocet': s['pocet']
        })
        
    opravnene_percento = 0
    neopravnene_percento = 0
    if celkom_reklamacii > 0:
        opravnene_percento = round((opravnene_pocet / celkom_reklamacii) * 100, 1)
        neopravnene_percento = round((neopravnene_pocet / celkom_reklamacii) * 100, 1)
        for k in korene_data:
            k['percento'] = round((k['pocet'] / celkom_reklamacii) * 100, 1)

    # --- 4. HALL OF FAME (NPS Skóre) ---
    nps_zaznamy = ServisnyZaznam.objects.filter(hodnotenie_zakaznika__isnull=False).prefetch_related('technici')
    technik_nps = {}
    for z in nps_zaznamy:
        for t in z.technici.all():
            tid = t.id
            if tid not in technik_nps:
                t_meno_str = f"{(t.meno or '').strip()} {(t.priezvisko or '').strip()}".strip()
                technik_nps[tid] = {'meno': t_meno_str or "Neznámy technik", 'suma': 0, 'pocet': 0}
            technik_nps[tid]['suma'] += z.hodnotenie_zakaznika
            technik_nps[tid]['pocet'] += 1
    
    rebricek_nps = []
    for tid, data in technik_nps.items():
        rebricek_nps.append({
            'meno': data['meno'],
            'priemer': round(data['suma'] / data['pocet'], 1) if data.get('pocet', 0) > 0 else 0.0
        })
    rebricek_nps = sorted(rebricek_nps, key=lambda x: x['priemer'], reverse=True)[:5]

    priemerne_nps_db = ServisnyZaznam.objects.filter(hodnotenie_zakaznika__isnull=False).aggregate(Avg('hodnotenie_zakaznika'))['hodnotenie_zakaznika__avg']
    priemerne_nps = round(priemerne_nps_db, 1) if priemerne_nps_db else 0.0

    # --- 5. NAJKAZOVEJŠIE ZNAČKY A KATEGÓRIE ---
    kazove_znacky_db = ServisnyZaznam.objects.filter(
        koren_problemu__in=['VADNY_DIEL', 'SOFTWARE'],
        zariadenie__vyrobca__isnull=False
    ).values('zariadenie__vyrobca__nazov_vyrobcu').annotate(pocet=Count('id')).order_by('-pocet')[:5]
    
    kazove_znacky = [{'nazov': z['zariadenie__vyrobca__nazov_vyrobcu'], 'pocet': z['pocet']} for z in kazove_znacky_db]

    kazove_kategorie_db = ServisnyZaznam.objects.filter(
        koren_problemu__in=['VADNY_DIEL', 'SOFTWARE'],
        zariadenie__kategoria__isnull=False
    ).exclude(zariadenie__kategoria='').values('zariadenie__kategoria').annotate(pocet=Count('id')).order_by('-pocet')[:5]
    
    kategorie_dict = dict(ZARIADENIA_CHOICES)
    kazove_kategorie = [{'nazov': kategorie_dict.get(k['zariadenie__kategoria'], k['zariadenie__kategoria'] or 'Nezaradené'), 'pocet': k['pocet']} for k in kazove_kategorie_db]

    # --- 6. PRIEMERNÁ DOBA VYBAVENIA REKLAMÁCIE (MTTR) ---
    vybavene_reklamacie = list(
        Uloha.objects.filter(je_reklamacia=True, dokoncene=True, koniec__isnull=False)
        .only('zaciatok', 'koniec', 'datum_prijatia_reklamacie')
    )
    priemerny_cas = 0.0
    if vybavene_reklamacie:
        celkovy_cas_sec = 0.0
        platne_pocty = 0
        for u in vybavene_reklamacie:
            t_start = u.datum_prijatia_reklamacie or u.zaciatok
            if t_start and u.koniec and u.koniec >= t_start:
                celkovy_cas_sec += (u.koniec - t_start).total_seconds()
                platne_pocty += 1
        if platne_pocty > 0:
            priemerny_cas = round((celkovy_cas_sec / platne_pocty) / 86400.0, 1)

    # --- 7. CHRONICKÉ PORUCHY (Opakované reklamácie) ---
    # NEPRIESTRELNÉ RIEŠENIE CEZ PYTHON (Obchádza zložité M2M ORM dotazy, ktoré padajú)
    vsetky_reklamacie = Uloha.objects.filter(je_reklamacia=True).prefetch_related('zariadenia')
    
    mapa_poctov = {}
    for uloha in vsetky_reklamacie:
        for zar in uloha.zariadenia.all():
            if zar.id not in mapa_poctov:
                mapa_poctov[zar.id] = 0
            mapa_poctov[zar.id] += 1
            
    chronicke_ids = [zid for zid, pocet in mapa_poctov.items() if pocet > 1]
    
    chronicke_stroje = []
    if chronicke_ids:
        stroje_db = ServisovaneZariadenie.objects.filter(id__in=chronicke_ids).select_related('zakaznik', 'vyrobca')
        for stroj in stroje_db:
            chronicke_stroje.append({
                'id': stroj.id,
                'zakaznik': stroj.zakaznik.Nazov_spolocnosti if stroj.zakaznik else "Neznámy",
                'nazov': stroj.model_zariadenia or stroj.nazov,
                'sn': stroj.vyrobne_cislo,
                'vyrobca': stroj.vyrobca.nazov_vyrobcu if stroj.vyrobca else "Neznámy",
                'pocet': mapa_poctov[stroj.id]
            })
        chronicke_stroje.sort(key=lambda x: x['pocet'], reverse=True)

    context = {
        'sla_data': sla_data,
        'rebricek_technikov': rebricek_technikov,
        'korene_data': korene_data,
        'celkom_reklamacii': celkom_reklamacii,
        'opravnene_percento': opravnene_percento,
        'neopravnene_percento': neopravnene_percento,
        'aktivne_reklamacie_pocet': len(sla_data),
        'kritickych_sla': kritickych_sla,
        'rebricek_nps': rebricek_nps,
        'priemerne_nps': priemerne_nps,
        'kazove_znacky': kazove_znacky,
        'kazove_kategorie': kazove_kategorie,
        'priemerny_cas_dni': priemerny_cas,
        'chronicke_stroje': chronicke_stroje,
    }
    
    return render(request, 'admin/dashboard_reklamacie.html', context)

@staff_member_required
def get_categories(request):    
    cats_db = list(StockMovement.objects.order_by().values_list("category", flat=True).distinct())
    cats_db = list(set([c.strip() for c in cats_db if c]))
    choices_dict = dict(CATEGORY_CHOICES)
    
    result = []
    for c in cats_db:
        if c in choices_dict:
            label = choices_dict[c]
            result.append({"id": c, "text": label})
        
    return JsonResponse({"categories": sorted(result, key=lambda x: x['text'])})

@staff_member_required
def get_subcategories(request):
    category = request.GET.get("category", "")
    
    qs = StockMovement.objects.order_by()
    
    if category:
        qs = qs.filter(category=category)
        
    subs_db = list(qs.values_list("subcategory", flat=True).distinct())
    subs_db = list(set([s.strip() for s in subs_db if s]))
    
    result_keys = set()
    cat_map = CATEGORY_MAP.get(category, {})
    
    for s in subs_db:
        for emoji_key in cat_map.keys():
            # Odstránime emoji pre porovnanie historických dát (ponecháme písmená, čísla aj bežnú interpunkciu)
            clean_key = ''.join(c for c in emoji_key if c.isalnum() or c in " ()-,./").strip()
            clean_s = ''.join(c for c in s if c.isalnum() or c in " ()-,./").strip()
                    
            # Ak sa záznam z DB zhoduje s novým kľúčom (s emoji) alebo so starým (bez emoji)
            if s == emoji_key or clean_s == clean_key:
                # Uložíme VÝHRADNE presný kľúč z CATEGORY_MAP!
                result_keys.add(emoji_key)
                break
                
    # Vytvoríme zoznam, kde ID aj TEXT je presný názov s emoji
    result = [{"id": k, "text": k} for k in result_keys]
        
    return JsonResponse({"subcategories": sorted(result, key=lambda x: x['text'])})

@staff_member_required
def get_products(request):
    category = request.GET.get("category", "")
    subcategory = request.GET.get("subcategory", "")
    filialka_id = request.GET.get("filialka", "")
    only_with_movements = request.GET.get("only_with_movements") == "true"

    if category not in CATEGORY_MAP or subcategory not in CATEGORY_MAP[category]:
        return JsonResponse({"products": []})

    ModelClass = CATEGORY_MAP[category][subcategory]

    povolene_nazvy = []
    if only_with_movements:
        # Hľadáme pohyby pod aktuálnym názvom (s emoji) aj historickým (bez emoji)
        clean_sub = ''.join(c for c in subcategory if c.isalpha() or c.isspace()).strip()
        povolene_nazvy = list(StockMovement.objects.filter(
            category=category, 
            subcategory__in=[subcategory, clean_sub]
        ).values_list('product_name', flat=True).distinct())

    filialka = None
    stavy_na_filialke = {}
    if filialka_id and str(filialka_id).isdigit():
        filialka = Filialka.objects.filter(id=filialka_id).first()
        if filialka:
            stavy_na_filialke = LedgerKalkulacka.zisti_stav_vsetkych_poloziek(filialka)

    result = []
    for obj in ModelClass.objects.all().iterator():
        cisty_nazov = ocisti_nazov_od_jednotky(str(obj))
        
        # 🛑 Ak je zapnutý flag, preskočíme produkty bez pohybu
        if only_with_movements and cisty_nazov not in povolene_nazvy:
            continue
            
        mj = getattr(obj, 'merna_jednotka', 'ks')
        
        if filialka:
            pocet = stavy_na_filialke.get(cisty_nazov, 0)
        else:
            pocet = getattr(obj, 'pocet_na_sklade', 0)
            if pocet is None:
                pocet = 0
            
        # 🛑 ODSTRÁNENIE ZÁTVORIEK: Pre Obratovosť pošleme len čistý názov
        if only_with_movements:
            display_text = cisty_nazov
        else:
            display_text = f"{cisty_nazov} ({pocet} {mj})"
        
        result.append({
            "id": obj.pk,
            "name": display_text,
            "clean_name": cisty_nazov, # Posielame čistý názov pre grafy
            "unit": mj
        })
        
    return JsonResponse({"products": sorted(result, key=lambda x: x['name'])})

@staff_member_required
def get_totals(request):
    category = request.GET.get("category", "")
    subcategory = request.GET.get("subcategory", "")
    product_name = request.GET.get("product_name", "")
    filialka_id = request.GET.get("filialka", "")
    
    period_days = PERIOD_MAP.get(request.GET.get("period", "7"), 7)
    start_ts = timezone.now() - timedelta(days=period_days)

    qs = StockMovement.objects.filter(timestamp__gte=start_ts)

    if filialka_id == "main":
        qs = qs.filter(filialka__isnull=True)
    elif filialka_id.isdigit():
        qs = qs.filter(filialka_id=int(filialka_id))

    if category:
        qs = qs.filter(category=category)
    if subcategory:
        # Pre správne výpočty berieme do úvahy staré aj nové záznamy!
        clean_sub = ''.join(c for c in subcategory if c.isalpha() or c.isspace()).strip()
        qs = qs.filter(subcategory__in=[subcategory, clean_sub])
    if product_name:
        qs = qs.filter(product_name=product_name)

    incoming = qs.filter(action=StockMovement.ADD).aggregate(s=Sum("count"))["s"] or 0
    outgoing = qs.filter(action=StockMovement.REMOVE).aggregate(s=Sum("count"))["s"] or 0

    return JsonResponse({
        "incoming": incoming, 
        "outgoing": outgoing
    })

@staff_member_required
def zariadenie_autocomplete(request):
    term = request.GET.get("term", "")
    customer_id = request.GET.get("customer", "")

    # select_related pre vyrobca zabráni N+1 pri volaní str(z)
    qs = ServisovaneZariadenie.objects.select_related('vyrobca').all()
    if customer_id and str(customer_id).isdigit():
        qs = qs.filter(zakaznik_id=customer_id).order_by("kategoria", "nazov")

    if term:
        qs = qs.filter(
            Q(nazov__icontains=term) |
            Q(vyrobne_cislo__icontains=term) |
            Q(model_zariadenia__icontains=term)
        )
        results = [{"id": z.id, "text": str(z)} for z in qs[:50]]
    elif customer_id and str(customer_id).isdigit():
        # Pre konkrétneho zákazníka vrátime všetky jeho zariadenia do checkboxového zoznamu bez orezania
        results = [{"id": z.id, "text": str(z)} for z in qs]
    else:
        results = [{"id": z.id, "text": str(z)} for z in qs[:20]]

    return JsonResponse({"results": results})

@staff_member_required
def get_zariadenia_for_zakaznik(request):
    zakaznik_id = request.GET.get("zakaznik_id")
    if not zakaznik_id or not str(zakaznik_id).isdigit():
        return JsonResponse({"zariadenia": []})

    # Vytiahneme stroje zákazníka
    zariadenia_qs = ServisovaneZariadenie.objects.filter(zakaznik_id=zakaznik_id)
    
    # Vyskladáme zoznam s pekným názvom (Typ + Sériové číslo) a záručnými parametrami pre odznak
    dnes = timezone.now().date()
    zariadenia = [
        {
            "id": z.id, 
            "text": f"{z.model_zariadenia} (S/N: {z.vyrobne_cislo})",
            "kategoria_display": z.get_kategoria_display(), # 🪄 TOTO ZABEZPEČÍ EMOJI V DROPDOWNE
            "zaruka_do": z.zaruka_do.strftime('%d.%m.%Y') if z.zaruka_do else "",
            "predlzena_zaruka_do": z.predlzena_zaruka_do.strftime('%d.%m.%Y') if z.predlzena_zaruka_do else "",
            "je_v_zaruke": getattr(z, 'je_v_zaruke', False),
            "je_predlzena": bool(z.predlzena_zaruka_do and z.predlzena_zaruka_do >= dnes)
        } for z in zariadenia_qs
    ]
    
    return JsonResponse({"zariadenia": zariadenia})

@staff_member_required
def get_sklad_items(request):
    ct_id = request.GET.get('ct_id')
    if not ct_id or not str(ct_id).isdigit():
        return JsonResponse({'results': []})
    
    try:
        ct = ContentType.objects.get(id=ct_id)
    except ContentType.DoesNotExist:
        return JsonResponse({'results': []})
        
    model_cls = ct.model_class()
    if not model_cls:
        return JsonResponse({'results': []})
        
    # Limit na 50 záznamov chráni RAM pred pretečením
    results = [{'id': obj.id, 'text': str(obj)} for obj in model_cls.objects.all()[:50]]
    return JsonResponse({'results': results})


@staff_member_required
def delete_item_ajax(request):
    if request.method == "POST":
        try:            
            data = json.loads(request.body)
            prefix = data.get('model')
            pk = data.get('pk')
            
            if not pk or not str(pk).isdigit():
                return JsonResponse({'status': 'error', 'message': 'Neplatné ID pre vymazanie'}, status=400)

            # Dynamické poskladanie mapy z meta-dát modelov
            mapping = {}
            for main_cat, subcats in CATEGORY_MAP.items():
                parent_cls = PARENT_MODELS.get(main_cat)
                if not parent_cls: continue
                
                for subcat_name, model_cls in subcats.items():
                    # Django introspekcia: Nájdeme ForeignKey smerujúci na Rodiča a vezmeme si jeho related_name
                    for field in model_cls._meta.get_fields():
                        if field.is_relation and field.many_to_one and field.related_model == parent_cls:
                            # prefix, ktorý posiela JavaScript, je presne toto related_query_name()
                            rel_name = field.related_query_name()
                            mapping[rel_name] = model_cls.__name__
                            break

            if prefix not in mapping:
                # BEZPEČNOSTNÝ ZÁMOK: Zabránenie zmazaniu arbitrary modelov!
                return JsonResponse({'status': 'error', 'message': 'Nepovolený model pre vymazanie'}, status=403)
            
            model_name = mapping[prefix]
            skladove_modely_map = {m.__name__: m for subcats in CATEGORY_MAP.values() for m in subcats.values()}
            model = skladove_modely_map.get(model_name)

            if not model:
                return JsonResponse({'status': 'error', 'message': 'Model nebol nájdený v aktívnom odvetví'}, status=404)
            model.objects.filter(pk=pk).delete() # filter + delete je bezpečnejší ako get()

            return JsonResponse({'status': 'ok'})

        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=400)

    return JsonResponse({'status': 'invalid method'}, status=405)

@staff_member_required
def bleskove_ulozit(request):
    """
    ISAS CORE: Jadro ukladania pohybov.
    🚀 LEDGER UPDATE: Izoluje transakcie Filiálky od fyzického modelu (Hlavného skladu).
    """
    if request.method == "POST":
        try:
            filialka_id = request.POST.get('filialka')
            typ_dokladu_erp = request.POST.get('typ_dokladu_erp') # 🟢 NOVÉ: Načítanie typu dokladu od skladníka
            cat_name = request.POST.get('category')
            subcat_name = request.POST.get('subcategory')
            prod_id = request.POST.get('product')
            
            # 🛡️ ISAS SECURITY FIX: Ochrana pred podvrhnutým payloadom
            try:
                count = int(request.POST.get('count', 1))
                if count <= 0: raise ValueError
            except ValueError:
                return JsonResponse({'status': 'error', 'message': '⚠️ Množstvo musí byť platné celé číslo väčšie ako nula!'})

            referer = request.META.get('HTTP_REFERER', '')
            action_post = request.POST.get('action', '').upper()
            is_remove = (action_post == 'REMOVE') or ('remove-stock' in referer)

            subcat_dict = CATEGORY_MAP.get(cat_name)
            if not subcat_dict:
                return JsonResponse({'status': 'error', 'message': f'❓ Neznáma kategória: {cat_name}'})

            ProductModel = subcat_dict.get(subcat_name)
            if not ProductModel:
                return JsonResponse({'status': 'error', 'message': f'❓ Neznáma podkategória: {subcat_name}'})

            # Skúsime nájsť filiálku
            filialka = Filialka.objects.filter(id=filialka_id).first() if filialka_id and str(filialka_id).isdigit() else None

            # =========================================================================
            # 🚀 ISAS PERFORMANCE FIX: Príprava dát PRED uzamknutím databázy
            # =========================================================================
            try:
                # Vytiahneme produkt bez zámku, aby sme zistili jeho názov a jednotku.
                # Týmto zabránime N+1 dopytom (str(product)) vo vnútri kritickej sekcie!
                product_unlocked = ProductModel.objects.get(pk=prod_id)
                cely_nazov_produktu = str(product_unlocked)
                cisty_nazov_produktu = ocisti_nazov_od_jednotky(cely_nazov_produktu)
                jednotka = getattr(product_unlocked, 'merna_jednotka', 'ks')
            except (ValueError, ProductModel.DoesNotExist):
                return JsonResponse({'status': 'error', 'message': '🔍 Produkt neexistuje alebo bol vymazaný.'})

            # =========================================================================
            # 🔒 ATOMICKÁ TRANSAKCIA (Všetko alebo nič) - Trvá len mikrosekundy!
            # =========================================================================
            with transaction.atomic():
                # 🛡️ ISAS MUTEX: Zamkneme produkt.
                product = ProductModel.objects.select_for_update().get(pk=prod_id)

                # 🟢 MAGICKÁ VÝHYBKA PRE ULOŽENIE
                if filialka:
                    # ---- SME NA FILIÁLKE ----
                    # Nedotýkame sa databázy product! Len zistíme virtuálny stav pre notifikácie
                    povodny_stav = LedgerKalkulacka.zisti_stav_polozky(filialka, cisty_nazov_produktu)
                    
                    if is_remove:
                        if povodny_stav < count:
                             return JsonResponse({'status': 'error', 'message': f'📦 Na filiálke {filialka.nazov} nie je dostatok kusov!'})
                        action_type = StockMovement.REMOVE
                        novy_stav = povodny_stav - count
                    else:
                        action_type = StockMovement.ADD
                        novy_stav = povodny_stav + count
                        
                else:
                    # ---- SME NA HLAVNOM SKLADE ----
                    povodny_stav = product.pocet_na_sklade if product.pocet_na_sklade is not None else 0
                    
                    if is_remove:
                        if povodny_stav < count:
                             hlavny_sklad_obj = Sklad.objects.first()
                             dyn_nazov_hlavneho = f"Hlavný sklad ({hlavny_sklad_obj.nazov})" if hlavny_sklad_obj and hlavny_sklad_obj.nazov and hlavny_sklad_obj.nazov.lower() != "hlavný sklad" else "Hlavný sklad"
                             return JsonResponse({'status': 'error', 'message': f'📦 V sklade {dyn_nazov_hlavneho} nie je dostatok kusov!'})
                        product.pocet_na_sklade = povodny_stav - count
                        action_type = StockMovement.REMOVE
                    else:
                        product.pocet_na_sklade = povodny_stav + count
                        action_type = StockMovement.ADD
                        
                    novy_stav = product.pocet_na_sklade

                    # Ukladáme zmenu priamo do fyzického modelu (Hlavný sklad)
                    product.save(update_fields=['pocet_na_sklade'])

                # 3. Vytvoríme záznam o pohybe (Tu sa "Ledger" zapisuje do knihy)
                povolene_typy = [choice[0] for choice in StockMovement.TYP_DOKLADU_CHOICES]
                if typ_dokladu_erp not in povolene_typy:
                    typ_dokladu_erp = 'SPOTREBA' if is_remove else 'PRIJEMKA'

                movement = StockMovement.objects.create(
                    filialka=filialka, 
                    user=request.user,  
                    category=cat_name,
                    subcategory=subcat_name,
                    product_name=cisty_nazov_produktu, 
                    count=count,
                    action=action_type,
                    timestamp=timezone.now(),
                    merna_jednotka=getattr(product, 'merna_jednotka', 'ks'),
                    typ_dokladu_erp=typ_dokladu_erp
                )

            # Dynamicky prilepíme presné čísla do objektu pohybu!
            movement._povodny_stav = povodny_stav
            movement._novy_stav = novy_stav

            url_detail = f"/admin/Evidencia/stockmovement/?q={movement.id}"
            send_isas_stock_notifications(movement, url_detail)

            return JsonResponse({
                'status': 'ok',
                'cely_nazov': cely_nazov_produktu
            })
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)})
            
    return JsonResponse({'status': 'invalid method'}, status=405)

@staff_member_required
def api_medziskladovy_presun(request):
    """
    ISAS CORE: Logistika (Medziskladové presuny).
    Podporuje presuny: Hlavný <-> Filiálka, ale aj Filiálka <-> Filiálka.
    Zabezpečené atomickou transakciou a M2M prístupovými právami.
    """
    if request.method == "POST":
        try:            
            zdroj_id = request.POST.get('zdroj_id')
            ciel_id = request.POST.get('ciel_id')
            
            cat_name = request.POST.get('category')
            subcat_name = request.POST.get('subcategory')
            prod_id = request.POST.get('product')
            
            try:
                count = int(request.POST.get('count', 1))
                if count <= 0: raise ValueError
            except ValueError:
                return JsonResponse({'status': 'error', 'message': '⚠️ Množstvo musí byť platné celé číslo väčšie ako nula!'})

            is_admin = request.user.is_superuser
            zamestnanec = getattr(request.user, 'zamestnanec', None)

            def check_access(filialka_obj):
                if is_admin: return True
                if not zamestnanec: return False
                return filialka_obj.priradeni_zamestnanci.filter(id=zamestnanec.id).exists()

            # Načítanie skladov (None znamená Hlavný sklad)
            zdroj_sklad = Filialka.objects.filter(id=zdroj_id).first() if zdroj_id and str(zdroj_id).isdigit() else None
            ciel_sklad = Filialka.objects.filter(id=ciel_id).first() if ciel_id and str(ciel_id).isdigit() else None

            if zdroj_sklad == ciel_sklad:
                return JsonResponse({'status': 'error', 'message': '⚠️ Zdrojový a cieľový sklad nemôžu byť rovnaké!'})

            if zdroj_sklad and not check_access(zdroj_sklad):
                return JsonResponse({'status': 'error', 'message': f'🔒 Nemáte prístup k zdrojovému skladu: {zdroj_sklad.nazov}'})
            if ciel_sklad and not check_access(ciel_sklad):
                return JsonResponse({'status': 'error', 'message': f'🔒 Nemáte prístup k cieľovému skladu: {ciel_sklad.nazov}'})

            subcat_dict = CATEGORY_MAP.get(cat_name)
            if not subcat_dict:
                return JsonResponse({'status': 'error', 'message': f'❓ Neznáma kategória: {cat_name}'})

            ProductModel = subcat_dict.get(subcat_name)
            if not ProductModel:
                return JsonResponse({'status': 'error', 'message': f'❓ Neznáma podkategória: {subcat_name}'})

            try:
                # Vytiahneme produkt bez zámku, aby sme zistili jeho názov a jednotku.
                # Týmto zabránime N+1 dopytom (str(product)) vo vnútri kritickej sekcie!
                product_unlocked = ProductModel.objects.get(pk=prod_id)
                cisty_nazov_produktu = ocisti_nazov_od_jednotky(str(product_unlocked))
                jednotka = getattr(product_unlocked, 'merna_jednotka', 'ks')
            except (ValueError, ProductModel.DoesNotExist):
                return JsonResponse({'status': 'error', 'message': '🔍 Produkt neexistuje alebo bol vymazaný.'})

            with transaction.atomic():
                product = ProductModel.objects.select_for_update().get(pk=prod_id)
                
                # 1. KONTROLA A ODPOČÍTANIE ZO ZDROJA
                if zdroj_sklad is None:
                    # Zdroj je Hlavný sklad
                    povodny_stav_zdroj = product.pocet_na_sklade if product.pocet_na_sklade is not None else 0
                    if povodny_stav_zdroj < count:
                        hlavny_sklad_obj = Sklad.objects.first()
                        dyn_nazov_hlavneho = f"Hlavný sklad ({hlavny_sklad_obj.nazov})" if hlavny_sklad_obj and hlavny_sklad_obj.nazov and hlavny_sklad_obj.nazov.lower() != "hlavný sklad" else "Hlavný sklad"
                        return JsonResponse({'status': 'error', 'message': f'📦 V sklade {dyn_nazov_hlavneho} nie je dostatok kusov! (Dostupné: {povodny_stav_zdroj} {jednotka})'})
                    product.pocet_na_sklade = povodny_stav_zdroj - count
                else:
                    # Zdroj je Filiálka
                    povodny_stav_zdroj = LedgerKalkulacka.zisti_stav_polozky(zdroj_sklad, cisty_nazov_produktu)
                    if povodny_stav_zdroj < count:
                        return JsonResponse({'status': 'error', 'message': f'📦 Na sklade {zdroj_sklad.nazov} nie je dostatok kusov! (Dostupné: {povodny_stav_zdroj} {jednotka})'})

                # Zápis výdaja (REMOVE)
                mov_out = StockMovement.objects.create(
                    filialka=zdroj_sklad, user=request.user, category=cat_name, subcategory=subcat_name,
                    product_name=cisty_nazov_produktu, count=count, action=StockMovement.REMOVE, 
                    timestamp=timezone.now(), merna_jednotka=jednotka, typ_dokladu_erp='PREVODKA'
                )
                mov_out._povodny_stav = povodny_stav_zdroj
                mov_out._novy_stav = povodny_stav_zdroj - count

                # 2. PRIPÍSANIE DO CIEĽA
                if ciel_sklad is None:
                    # Cieľ je Hlavný sklad
                    povodny_stav_ciel = product.pocet_na_sklade if product.pocet_na_sklade is not None else 0
                    product.pocet_na_sklade = povodny_stav_ciel + count
                else:
                    # Cieľ je Filiálka
                    povodny_stav_ciel = LedgerKalkulacka.zisti_stav_polozky(ciel_sklad, cisty_nazov_produktu)

                # Zápis príjmu (ADD)
                mov_in = StockMovement.objects.create(
                    filialka=ciel_sklad, user=request.user, category=cat_name, subcategory=subcat_name,
                    product_name=cisty_nazov_produktu, count=count, action=StockMovement.ADD, 
                    timestamp=timezone.now(), merna_jednotka=jednotka, typ_dokladu_erp='PREVODKA'
                )
                mov_in._povodny_stav = povodny_stav_ciel
                mov_in._novy_stav = povodny_stav_ciel + count

                # Uložíme fyzický model IBA ak sa menil Hlavný sklad
                if zdroj_sklad is None or ciel_sklad is None:
                    product.save(update_fields=['pocet_na_sklade'])

                hlavny_sklad_obj = Sklad.objects.first()
                dyn_nazov_hlavneho = f"Hlavný sklad ({hlavny_sklad_obj.nazov})" if hlavny_sklad_obj and hlavny_sklad_obj.nazov and hlavny_sklad_obj.nazov.lower() != "hlavný sklad" else "Hlavný sklad"

                nazov_zdroja = zdroj_sklad.nazov if zdroj_sklad else dyn_nazov_hlavneho
                nazov_ciela = ciel_sklad.nazov if ciel_sklad else dyn_nazov_hlavneho
                hlavna_sprava = f"🚛 Úspešný presun {count} {jednotka}\nZ: {nazov_zdroja}\nDO: {nazov_ciela}"

            # Notifikácia pre presun
            try:
                if zamestnanec:
                    user_name = f"{zamestnanec.meno} {zamestnanec.priezvisko}".strip()
                else:
                    # Fallback, ak by presun robil napr. SuperAdmin, ktorý nemá kartu zamestnanca
                    user_name = request.user.get_full_name() or request.user.username
                    
                task_odosli_notifikaciu_presun.delay(
                    user_name, nazov_zdroja, nazov_ciela, cisty_nazov_produktu, count, jednotka
                )
            except Exception as exc:
                logger.warning(f"Zlyhalo zaradenie task_odosli_notifikaciu_presun do Celery: {exc}")

            return JsonResponse({'status': 'success', 'message': hlavna_sprava, 'cely_nazov': str(product)})
            
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': f"Kritická chyba presunu: {str(e)}"}, status=500)
            
    return JsonResponse({'status': 'invalid method'}, status=405)

@staff_member_required
def ai_chat_view(request):
    # 🛡️ FAST-FAIL BEZPEČNOSTNÁ POISTKA
    api_key = get_gemini_api_key()
    gemini_model = getattr(settings, 'GEMINI_MODEL', None)
    if not api_key or not gemini_model:
        logger.warning("AI Chat View: GEMINI API kľúč alebo GEMINI_MODEL nie je nakonfigurovaný.")
        return JsonResponse({
            "status": "error", 
            "message": "AI asistent je momentálne nedostupný (chýba konfigurácia API kľúča alebo modelu v systéme)."
        })

    if request.method == "POST":
        user_message = request.POST.get("message", "").strip()
        uploaded_files_list = request.FILES.getlist('files') or request.FILES.getlist('attachments')
        
        # 🛡️ Povolíme odoslanie, ak je prítomný text ALEBO aspoň jeden súbor
        if not user_message and not uploaded_files_list:
            return JsonResponse({"status": "error", "message": "Prázdna správa alebo chýbajúca príloha."})
            
        # 🛡️ BACKEND SECURITY LIMITS: Ochrana proti DoS a zaplneniu disku
        if len(uploaded_files_list) > 5:
            return JsonResponse({"status": "error", "message": "🛑 Maximálny povolený počet príloh na jednu správu je 5."})

        MAX_FILE_SIZE_BYTES = 25 * 1024 * 1024 # 25 MB
        for up_file in uploaded_files_list:
            if up_file.size > MAX_FILE_SIZE_BYTES:
                return JsonResponse({"status": "error", "message": f"🛑 Súbor '{up_file.name}' prekračuje povolenú veľkosť 25 MB."})
            
        if not user_message and uploaded_files_list:
            user_message = "Analyzuj priložené súbory a poskytni k nim podrobný prehľad."
            
        # --- 🛡️ OCHRANA 0: PAYLOAD SIZE LIMIT (Ochrana pred Token Bloat útokom) ---
        if len(user_message) > 10000:
            return JsonResponse({
                "status": "error", 
                "message": "🛑 Vaša správa je príliš dlhá (maximum je 10 000 znakov). Ak analyzujete dlhý text, rozdeľte ho na menšie časti."
            })

        user_id = request.user.id
        dnes_datum = timezone.now().date()
        
        # --- 🛡️ OCHRANA 1: ZÁMOK SÚBEŽNOSTI (Concurrency Lock) ---
        lock_key = f"ai_lock_user_{user_id}"
        if cache.get(lock_key):
            return JsonResponse({"status": "error", "message": "⏳ Stále premýšľam nad predchádzajúcou otázkou. Prosím, počkaj na odpoveď."})
        
        # --- 🛡️ OCHRANA 2: DENNÉ KVÓTY Z DATABÁZY (Presná štatistika) ---        
        pouzite_dnes = AIAuditLog.objects.filter(
            pouzivatel=request.user, 
            cas_dopytu__date=dnes_datum
        ).count()
        
        # --- 🛡️ DYNAMICKÉ LIMITY Z GLOBÁLNYCH NASTAVENÍ (Presné mapovanie na DB model) ---
        nastavenia = GlobalneNastavenia.objects.first()
        
        # Zistíme, či je to majiteľ (buď cez Django práva, alebo cez typ pomeru 'KO')
        is_majitel = request.user.is_superuser or (hasattr(request.user, 'zamestnanec') and request.user.zamestnanec.typ_pomeru == 'KO')
        
        if is_majitel:
            denny_limit = getattr(nastavenia, 'ai_limit_superadmin', None) or 500
        elif hasattr(request.user, 'zamestnanec'):
            rola = request.user.zamestnanec.pracovna_rola
            # 'DI' = Dispečer, 'AD' = Administratíva
            if rola in ['DI', 'AD']: 
                denny_limit = getattr(nastavenia, 'ai_limit_management', None) or 100
            # 'TE' = Technik, 'SK' = Skladník
            else: 
                denny_limit = getattr(nastavenia, 'ai_limit_technik', None) or 25
        else:
            denny_limit = getattr(nastavenia, 'ai_limit_default', None) or 10
            
        if pouzite_dnes >= denny_limit:
            return JsonResponse({"status": "error", "message": f"🛑 Dosiahli ste svoj denný limit ({denny_limit} správ) pre komunikáciu s AI. Skúste to znova zajtra."})
        
        try:
            # 1. Defenzívne získame históriu zo session
            raw_history = request.session.get('chat_history', [])
            if not isinstance(raw_history, list):
                raw_history = []
                
            history = []
            for msg in raw_history:
                if isinstance(msg, dict) and 'role' in msg and 'text' in msg:
                    history.append(msg)
            
            history_text = "\n".join([f"{msg.get('role', 'Používateľ')}: {msg.get('text', '')}" for msg in history])
            
            # --- 🛡️ ISAS RBAC: BEZPEČNOSTNÁ IDENTIFIKÁCIA POUŽÍVATEĽA ---
            aktualny_pouzivatel = request.user.get_full_name() or request.user.username
            rola_pouzivatela = "Neznáma"
            typ_pomeru = "Neznámy"
            
            if request.user.is_superuser:
                typ_pomeru = "👑 Konateľ / Majiteľ"
                rola_pouzivatela = "Superadmin"
            elif hasattr(request.user, 'zamestnanec'):
                rola_pouzivatela = request.user.zamestnanec.get_pracovna_rola_display()
                typ_pomeru = request.user.zamestnanec.get_typ_pomeru_display()
            
            # 2. Uložíme otázku do histórie IHNEĎ (odpoveď pridáme až v status view)
            meno_pouzivatela = request.user.first_name if request.user.first_name else "Používateľ"
            history.append({"role": meno_pouzivatela, "text": user_message})
            request.session['chat_history'] = history[-10:]
            request.session.modified = True
            
            # 3. ZAMKNEME POUŽÍVATEĽA V REDISE (Proti double-clicku)
            cache.set(lock_key, True, timeout=120)

            # Generujeme unikátne ID úlohy vopred pre dokonalé a spoľahlivé spárovanie logu
            custom_task_id = str(uuid.uuid4())

            # 4. ZAPÍŠEME AUDIT DO DATABÁZY (Ukladáme aj otázku a ID úlohy do nových stĺpcov)
            audit_zaznam = AIAuditLog.objects.create(
                pouzivatel=request.user,
                otazka=user_message,
                celery_task_id=custom_task_id
            )
            
            # --- 🛡️ HARD-SECURITY: Prístup k financiám ---
            # Prístup má len Majiteľ (Superadmin alebo 'KO') a Administratíva ('AD')
            ma_pristup_k_financiam = is_majitel
            if not ma_pristup_k_financiam and hasattr(request.user, 'zamestnanec') and request.user.zamestnanec:
                if request.user.zamestnanec.pracovna_rola == 'AD':
                    ma_pristup_k_financiam = True

            # 🛡️ DEFENSIVE: Presné ID pre B2B klienta alebo externú osobu (prevencia IDOR a neunikátnych mien)
            subjekt_id = None
            b2b_obj = getattr(request.user, 'zakaznik_profil', None) or getattr(request.user, 'zakaznik', None)
            if b2b_obj and not getattr(b2b_obj, 'je_interny_profil', False):
                subjekt_id = b2b_obj.id
            else:
                so_obj = getattr(request.user, 'sukromna_osoba', None)
                if so_obj:
                    subjekt_id = so_obj.id

            # 📁 BEZPEČNÉ DOČASNÉ ULOŽENIE PRÍLOH PRE CELERY WORKER
            saved_file_paths = []
            if uploaded_files_list:
                temp_upload_dir = os.path.join(settings.MEDIA_ROOT, 'ai_temp_uploads', custom_task_id)
                os.makedirs(temp_upload_dir, exist_ok=True)
                for f in uploaded_files_list:
                    safe_filename = f"{uuid.uuid4().hex}_{os.path.basename(f.name)}"
                    full_disk_path = os.path.join(temp_upload_dir, safe_filename)
                    with open(full_disk_path, 'wb+') as destination:
                        for chunk in f.chunks():
                            destination.write(chunk)
                    saved_file_paths.append(full_disk_path)

            # 5. DELEGUJEME PRÁCU NA CELERY TASK (Asynchrónne cez apply_async s vnúteným ID)
            try:
                task = spracuj_ai_chat_task.apply_async(
                    kwargs={
                        'user_question': user_message,
                        'history_text': history_text,
                        'aktualny_pouzivatel': aktualny_pouzivatel,
                        'rola_pouzivatela': rola_pouzivatela,
                        'typ_pomeru': typ_pomeru,
                        'is_superuser': request.user.is_superuser,
                        'audit_id': audit_zaznam.id,
                        'ma_pristup_k_financiam': ma_pristup_k_financiam,
                        'file_paths': saved_file_paths,
                        'subjekt_id': subjekt_id
                    },
                    task_id=custom_task_id # Vnútime Celery naše vygenerované ID
                )
            except Exception as exc:
                cache.delete(lock_key)
                if 'temp_upload_dir' in locals() and os.path.exists(temp_upload_dir):
                    shutil.rmtree(temp_upload_dir, ignore_errors=True)
                logger.warning(f"Zlyhalo zaradenie spracuj_ai_chat_task do Celery: {exc}")
                return JsonResponse({"status": "error", "message": "🤖 AI služba je momentálne nedostupná (výpadok úlohového servera)."}, status=503)            
            return JsonResponse({"status": "processing", "task_id": custom_task_id})
            
        except Exception as e:
            cache.delete(lock_key) # V prípade chyby odomkneme
            if 'temp_upload_dir' in locals() and os.path.exists(temp_upload_dir):
                import shutil
                shutil.rmtree(temp_upload_dir, ignore_errors=True)
            return JsonResponse({"status": "error", "message": f"Kritická chyba pri spúšťaní AI: {str(e)}"})
            
    return JsonResponse({"status": "error", "message": "Invalid request"}, status=400)

@staff_member_required
def ai_chat_reset_view(request):
    """
    Vymaže históriu AI chatu zo session (Nová konverzácia).
    """
    if request.method == "POST":
        
        # 1. Vymažeme históriu zo session
        if 'chat_history' in request.session:
            del request.session['chat_history']
            request.session.modified = True
            
        # 2. 🛡️ Odomkneme používateľa (Ak resetol chat počas toho, ako AI premýšľalo)
        cache.delete(f"ai_lock_user_{request.user.id}")
        
        # 3. 🧹 Ochrana proti Redis Memory Leak (Zmažeme osirelý výsledok Celery tasku)
        try:
            data = json.loads(request.body)
            task_id = data.get('task_id')
            if task_id:
                uuid.UUID(str(task_id))
                if AIAuditLog.objects.filter(celery_task_id=str(task_id), pouzivatel=request.user).exists():
                    cache.delete(f"ai_chat_result_{task_id}")
        except Exception:
            pass
        
        return JsonResponse({"status": "success", "message": "História vymazaná"})
    return JsonResponse({"status": "error", "message": "Invalid request"}, status=400)

@staff_member_required
def ai_chat_status_view(request, task_id):
    """
    Ultra-rýchly endpoint pre polling s nepriestrelným odchytávaním chýb.
    """
    try:
        try:
            uuid.UUID(str(task_id))
        except (ValueError, TypeError, AttributeError):
            return JsonResponse({"status": "error", "message": "Neplatný identifikátor AI úlohy."}, status=400)

        if not AIAuditLog.objects.filter(celery_task_id=str(task_id), pouzivatel=request.user).exists():
            return JsonResponse({"status": "error", "message": "Prístup k tejto AI úlohe bol zamietnutý."}, status=403)

        cache_key = f"ai_chat_result_{task_id}"
        result = cache.get(cache_key)
        user_id = request.user.id
        
        if result:
            cache.delete(f"ai_lock_user_{user_id}")
            
            if result.get("status") == "success":
                history = request.session.get('chat_history', [])
                if isinstance(history, list):
                    history.append({"role": "AI", "text": str(result.get("response", ""))})
                    request.session['chat_history'] = history[-10:]
                    request.session.modified = True
                
            cache.delete(cache_key)
            return JsonResponse(result)

        # 🛡️ OCHRANA: Bezpečné overenie stavu Celery tasku
        try:
            from celery.result import AsyncResult
            async_res = AsyncResult(task_id)
            if async_res.ready() and async_res.failed():
                cache.delete(f"ai_lock_user_{user_id}")
                return JsonResponse({
                    "status": "error", 
                    "message": f"Úloha na serveri zlyhala: {str(async_res.result)}"
                })
        except Exception as celery_err:
            logger.warning(f"Nepodarilo sa overiť stav Celery cez AsyncResult: {celery_err}")

        return JsonResponse({"status": "processing"})

    except Exception as e:
        logger.error(f"Kritická chyba v ai_chat_status_view: {e}", exc_info=True)
        cache.delete(f"ai_lock_user_{request.user.id}")
        return JsonResponse({
            "status": "error",
            "message": f"Chyba pri overovaní stavu odpovede: {str(e)}"
        })


@staff_member_required
def ai_pdf_cache_image_view(request, filename):
    if request.method not in ('GET', 'HEAD'):
        return HttpResponse(status=405)

    match = re.fullmatch(
        r'pdf_render_([A-Za-z0-9_]+)_(\d+)_strana_(\d+)_(\d+)_([0-9a-f]{16})\.png',
        str(filename or '')
    )
    if not match:
        raise Http404()

    safe_model_lower = match.group(1).lower()
    zam = getattr(request.user, 'zamestnanec', None)
    is_vedenie = request.user.is_superuser or (zam is not None and zam.typ_pomeru == 'KO')
    is_financie = is_vedenie or (zam is not None and zam.pracovna_rola == 'AD')

    if not is_vedenie:
        if is_financie:
            if safe_model_lower in {'kandidat', 'megazaloha', 'importdattask'}:
                return HttpResponseForbidden("Prístup zamietnutý.")
        else:
            if not zam:
                return HttpResponseForbidden("Prístup zamietnutý.")
            povolene_modely = {
                'technickaspecifikacia', 'servisovanezariadenie', 'servisnyzaznam',
                'povolenienavstup', 'zakaznik', 'databazavyrobcov',
            }
            from .industry_loader import CATEGORY_MAP
            for subcats in CATEGORY_MAP.values():
                for m_cls in subcats.values():
                    if hasattr(m_cls, '_meta'):
                        povolene_modely.add(m_cls._meta.object_name.lower())
            if safe_model_lower not in povolene_modely:
                return HttpResponseForbidden("Prístup zamietnutý.")

    cache_dir = os.path.abspath(os.path.join(settings.MEDIA_ROOT, 'ai_pdf_cache'))
    full_path = os.path.abspath(os.path.join(cache_dir, filename))
    if not full_path.startswith(cache_dir + os.sep):
        raise Http404()
    if os.path.islink(full_path) or not os.path.isfile(full_path):
        raise Http404()

    response = FileResponse(open(full_path, 'rb'), content_type='image/png')
    response['Cache-Control'] = 'private, no-store, max-age=0'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


@staff_member_required
def konfigurator_dielov_view(request, zariadenie_id):
    zariadenie = get_object_or_404(ServisovaneZariadenie, id=zariadenie_id)
    typy_prac = TypServisuPrace.objects.all()

    # Spracovanie pridania nového dielu
    if request.method == "POST":

        # --- OPRAVENÉ MAZANIE: VYMAŽE AJ PRÁZDNU KARTU ---
        delete_id = request.POST.get('delete_id')
        if delete_id and str(delete_id).isdigit():
            # ISAS SECURITY: Overenie vlastníctva (IDOR ochrana)
            polozka = PolozkaPredpisu.objects.filter(id=delete_id, predpis__zariadenie=zariadenie).first()
            if polozka:
                predpis = polozka.predpis # Zapamätáme si, do ktorej karty patrí
                polozka.delete()          # Zmažeme konkrétny diel
                
                # Ak v karte už neostal ani jeden diel, zmažeme celú kartu
                if predpis.polozky.count() == 0:
                    predpis.delete()
                
                messages.success(request, "🗑️ Položka bola úspešne vymazaná.")
                    
            return redirect('evidencia:konfigurator_dielov', zariadenie_id=zariadenie.id)

        typ_prace_id = request.POST.get('typ_prace')
        model_name = request.POST.get('model_name')
        diel_id = request.POST.get('diel_id')
        
        try:
            raw_mnozstvo = float(str(request.POST.get('mnozstvo', 1)).replace(',', '.'))
            if raw_mnozstvo <= 0:
                raise ValueError
            mnozstvo = max(1, int(round(raw_mnozstvo)))
        except (ValueError, TypeError):
            messages.error(request, "🔢 Neplatné množstvo položky (musí byť kladné celé číslo).")
            return redirect('evidencia:konfigurator_dielov', zariadenie_id=zariadenie.id)

        if typ_prace_id and str(typ_prace_id).isdigit() and model_name and diel_id and str(diel_id).isdigit():
            typ_prace = get_object_or_404(TypServisuPrace, id=typ_prace_id)
            
            # Vytvoríme alebo nájdeme hlavičku predpisu
            predpis, created = ServisnyPredpis.objects.get_or_create(
                zariadenie=zariadenie, 
                typ_prace=typ_prace
            )
            
            # Overenie povolených modelov priamo z aktívneho odvetvia
            skladove_modely_map = {m.__name__: m for subcats in CATEGORY_MAP.values() for m in subcats.values()}
            ModelClass = skladove_modely_map.get(model_name)
            if not ModelClass:
                messages.error(request, "🛡️ Bezpečnostná výnimka: Model neexistuje v aktívnom odvetví.")
                return redirect('evidencia:konfigurator_dielov', zariadenie_id=zariadenie.id)
                
            # Necháme Django, nech si ContentType vytiahne samo priamo z triedy
            content_type = ContentType.objects.get_for_model(ModelClass)
            
            # Uložíme riadok (položku)
            PolozkaPredpisu.objects.create(
                predpis=predpis,
                content_type=content_type,
                object_id=diel_id,
                mnozstvo=mnozstvo
            )
            messages.success(request, "⚙️ Položka bola úspešne pridaná do predpisu.")
            return redirect('evidencia:konfigurator_dielov', zariadenie_id=zariadenie.id)

    # 1. Vytvoríme si pomocnú mapu pre rýchle hľadanie názvov kategórií
    # Výsledok bude: {'Olej': ('🗂️ Spotrebný materiál', '💧 Oleje'), ...}
    rev_map = {}
    for main_cat, subcats in CATEGORY_MAP.items():
        for sub_cat_name, model_class in subcats.items():
            rev_map[model_class.__name__] = (main_cat, sub_cat_name)

    existujuce_predpisy = list(ServisnyPredpis.objects.filter(zariadenie=zariadenie).prefetch_related('polozky'))

    for predpis in existujuce_predpisy:
        polozky_list = []
        for p in predpis.polozky.all():
            # Bezpečné zistenie triedy modelu s ochranou proti poškodeným ContentType
            model_cls = p.content_type.model_class() if p.content_type else None
            m_name = model_cls.__name__ if model_cls else 'NeznamyModel'
            
            # Vytiahneme kategórie z našej mapy
            kat_info = rev_map.get(m_name, ("📂 Nešpecifikované", "📦 Všeobecné"))
            
            # RUČNE PRIRADÍME ATRIBÚTY (tieto potom použijeme v template)
            p.temp_kategoria = kat_info[0]
            p.temp_podkategoria = kat_info[1]
            polozky_list.append(p)
        
        # Zoradíme podľa našich nových "vstreknutých" atribútov
        predpis.zoradene_polozky = sorted(
            polozky_list, 
            key=lambda x: (x.temp_kategoria, x.temp_podkategoria)
        )

    # Pretransformujeme tvoju CATEGORY_MAP pre JavaScript (vytiahneme len názvy modelov ako stringy)
    js_category_map = {}
    for main_cat, subcats in CATEGORY_MAP.items():
        js_category_map[main_cat] = {name: model.__name__ for name, model in subcats.items()}

    context = {
        'zariadenie': zariadenie,
        'typy_prac': typy_prac,
        'existujuce_predpisy': existujuce_predpisy,
        'js_category_map': js_category_map,
        # Dynamicky zistíme URL Zákazníka, aby fungovalo tlačidlo "Späť" (bezpečný fallback ak zariadenie nemá zákazníka)
        'zakaznik_url': reverse('admin:Evidencia_zakaznik_change', args=[zariadenie.zakaznik.id]) if zariadenie.zakaznik else reverse('admin:Evidencia_servisovanezariadenie_change', args=[zariadenie.id]),
        'kategorie_moznosti': CATEGORY_CHOICES,  # 🚀 Posielame premennú s emoji do HTML šablóny!
    }
    return render(request, 'admin/konfigurator_dielov.html', context)


@staff_member_required
def ajax_get_diely(request):
    model_name = request.GET.get('model_name')
    if not model_name:
        return JsonResponse({'error': 'Nebol zadaný model'}, status=400)
    
    try:
        skladove_modely_map = {m.__name__: m for subcats in CATEGORY_MAP.values() for m in subcats.values()}
        ModelClass = skladove_modely_map.get(model_name)
        if not ModelClass:
            return JsonResponse({'error': 'Nepovolený prístup k systémovému modelu'}, status=403)

        diely = ModelClass.objects.all()[:50]  # Limit pre autocomplete
        
        data = []
        for d in diely:
            text_objektu = str(d)
            seriove = getattr(d, 'seriove_cislo', '')
            
            # --- ISAS CORE: 100% Dynamické stiahnutie mernej jednotky z modelu ---
            merna_jednotka = getattr(d, 'merna_jednotka', 'ks')
            
            if "object (" in text_objektu and seriove:
                text_objektu = str(seriove)
            elif seriove and str(seriove) not in text_objektu:
                text_objektu = ocisti_nazov_od_jednotky(text_objektu)
                text_objektu = f"{seriove} - {text_objektu}"
            else:
                text_objektu = ocisti_nazov_od_jednotky(text_objektu)
                
            # Prilepíme jednotku do JSON odpovede
            data.append({'id': d.id, 'text': text_objektu, 'unit': merna_jednotka})
            
        return JsonResponse({'results': data})
    except Exception as e:
        print(f"--- DIAGNOSTIKA API CHYBY: {e} ---") 
        return JsonResponse({'error': str(e)}, status=400)


# --- Tvoja existujúca funkcia pre zobrazenie zostáva ---
def servisny_protokol_view(request, kluc):
    try:
        uuid.UUID(str(kluc))
    except (ValueError, TypeError, AttributeError):
        raise Http404("Tento protokol neexistuje alebo bol vymazaný.")
        
    zaznam = ServisnyZaznam.objects.select_related('zariadenie', 'zariadenie__vyrobca', 'zakaznik').prefetch_related(
        'pouzite_polozky_zaznamu__content_object', 'typ_servisu', 'technici'
    ).filter(bezpecnostny_kluc=kluc).first()
    if not zaznam:
        raise Http404("Tento protokol neexistuje alebo bol vymazaný.")
        
    identita = FiremnaIdentita.objects.first()
    
    # Ak záznam nemá AI zhodnotenie, necháme AI ho rýchlo vygenerovať z poznámok (Fallback)
    if not zaznam.formalne_zhodnotenie_ai and zaznam.poruchy:
        try:
            zaznam.vygeneruj_odborny_text_ai()
        except Exception as e:
            logger.warning(f"Zlyhalo automatické AI zhodnotenie protokolu ID {zaznam.id} (Graceful Degradation): {e}")

    poradove_cislo = zaznam.id

    if zaznam.zakaznik:
        poradove_cislo = ServisnyZaznam.objects.filter(zakaznik=zaznam.zakaznik, id__lte=zaznam.id).count()
        
    context = {
        'zaznam': zaznam,
        'identita': identita,
        'materialy': zaznam.pouzite_polozky_zaznamu.all(),
        'typy_prac': zaznam.typ_servisu.all(),
        'poradove_cislo': poradove_cislo
    }
    
    if zaznam.protokol_uzavrety:
        return render(request, 'admin/protokol_tlac.html', context)
        
    return render(request, 'admin/protokol_podpis.html', context)


@csrf_exempt
def ulozit_podpis_ajax(request, kluc):
    if request.method == "POST":
        try:
            uuid.UUID(str(kluc))
        except (ValueError, TypeError, AttributeError):
            return JsonResponse({'status': 'error', 'message': 'Neplatný bezpečnostný kľúč.'}, status=400)
            
        try:
            with transaction.atomic():
                # Uzamknutie záznamu proti double-clicku na tablete
                zaznam = ServisnyZaznam.objects.select_for_update().prefetch_related(
                    'pouzite_polozky_zaznamu', 'technici'
                ).filter(bezpecnostny_kluc=kluc).first()
                
                if not zaznam:
                    return JsonResponse({'status': 'error', 'message': 'Záznam nebol nájdený.'})
                
                if zaznam.protokol_uzavrety:
                    # Ak už je uzavretý, vrátime 'success', aby si to frontend (auto-sync) 
                    # úspešne vymazal z offline pamäte a nerobil nekonečnú slučku.
                    return JsonResponse({'status': 'success', 'message': 'Protokol už bol spracovaný.'})
                    
                data = json.loads(request.body)
                if not isinstance(data, dict):
                    return JsonResponse({'status': 'error', 'message': 'Neplatný dátový formát požiadavky.'}, status=400)
                
                # --- NOVÉ ENTERPRISE DÁTA ---
                # Priradenie podpisov a mena z frontendu do modelu!
                zaznam.meno_podpisujuceho = data.get('meno_zakaznika') or 'Neuvedené'
                zaznam.podpis_zakaznika = data.get('podpis_zakaznika') or ''
                zaznam.podpis_technika = data.get('podpis_technika') or ''
                # -----------------------------------------------------------------------

                hodnotenie_str = data.get('hodnotenie_zakaznika')
                if hodnotenie_str and str(hodnotenie_str).isdigit():
                    zaznam.hodnotenie_zakaznika = int(hodnotenie_str)
                
                zaznam.gps_suradnice = data.get('gps_suradnice', 'Nezískané')
                
                upraveny_text = data.get('upraveny_text_ai')
                if upraveny_text:
                    zaznam.formalne_zhodnotenie_ai = upraveny_text
                
                zaznam.protokol_uzavrety = True
                zaznam.cas_podpisu = timezone.now()
                
                # VÝPOČET KRYPTOGRAFICKEJ PEČATE (SHA-256)
                # Spájame kľúčové nezmeniteľné dáta do jedného surového reťazca
                raw_data = f"{zaznam.id}|{zaznam.bezpecnostny_kluc}|{zaznam.cas_podpisu.timestamp()}|"
                raw_data += f"{zaznam.meno_podpisujuceho}|{zaznam.gps_suradnice}|{zaznam.formalne_zhodnotenie_ai}|"
                # ISAS SECURITY: Pridanie prevádzkových dát stroja do pečate
                raw_data += f"MH:{zaznam.stav_motohodin}|LOAD:{zaznam.motohodiny_v_zatazi}|"
                # ISAS SECURITY: Pridanie diagnostiky a meraní (Ochrana pred falšovaním reklamácií)
                raw_data += f"VAL:{zaznam.namerane_hodnoty}|HW:{zaznam.zmena_hardveru}|KOREN:{zaznam.koren_problemu}|"
                
                # ISAS SECURITY: Uzamknutie samotných podpisov do hashu (Ochrana pred prenesením podpisu)
                bezpecny_podpis_zak = zaznam.podpis_zakaznika if zaznam.podpis_zakaznika else "SMART_LOCK_BEZ_PODPISU"
                raw_data += f"SIG_ZAK:{bezpecny_podpis_zak}|SIG_TECH:{zaznam.podpis_technika}|"
                
                # Pridáme do pečate aj všetok materiál (ID a Počet)
                for mat in zaznam.pouzite_polozky_zaznamu.all().order_by('id'):
                    raw_data += f"M{mat.object_id}x{mat.pocet}|"
                    
                # Preženieme to algoritmom SHA-256 a získame 64-znakový hexadecimálny kód
                zaznam.bezpecnostny_hash = hashlib.sha256(raw_data.encode('utf-8')).hexdigest()

                # Teraz môžeme všetko naraz uložiť do DB
                zaznam.save()

                # ⚙️ Oživenie mŕtveho kódu
                # Po úspešnom uzatvorení protokolu vygenerujeme plány pre ďalší servis
                zaznam.vygeneruj_prediktivne_plany()

                base_url = request.build_absolute_uri('/')
                
                # Zistenie ID používateľa pre potlačenie notifikácií konateľovi
                user_id = request.user.id if request.user.is_authenticated else None
                poslat_mail = bool(data.get('odoslat_mail_zakaznikovi', False))
                
                # Task sa spustí až po úspešnom commite transakcie
                def _safe_generuj_protokol_pdf(z_id, b_url, u_id, p_mail):
                    try:
                        generuj_protokol_pdf_task.delay(z_id, b_url, u_id, p_mail)
                    except Exception as exc:
                        logger.warning(f"Zlyhalo zaradenie generuj_protokol_pdf_task do Celery: {exc}")

                transaction.on_commit(lambda p_mail=poslat_mail: _safe_generuj_protokol_pdf(zaznam.id, base_url, user_id, p_mail))

            return JsonResponse({'status': 'success', 'message': 'Protokol bol úspešne uzamknutý. PDF sa generuje na pozadí a bude odoslané zákazníkovi.'})
            
        except Exception:
            logger.exception("ISAS PDF ERROR pri uložení podpisu")
            return JsonResponse({'status': 'error', 'message': 'Nastala chyba pri ukladaní podpisu a generovaní protokolu.'}, status=400)
            
    return JsonResponse({'status': 'invalid'}, status=400)

@staff_member_required
def generuj_protokol_ai_ajax(request):
    """
    Zoberie text, ktorý technik rýchlo naťukal do mobilu, 
    a pomocou Gemini ho prepíše na vysoko profesionálny servisný protokol.
    """
    # 🛡️ OCHRANA PRED DoS ÚTOKOM A VYČERPANÍM KREDITU GOOGLE API
    klient_ip = get_client_ip(request)
    cache_kluc = f"ai_rate_limit_{klient_ip}"
    
    # Atomická inkrementácia v Cache (Ochrana pred paralelným DoS útokom)
    try:
        pocet_pokusov = cache.incr(cache_kluc)
    except ValueError:
        # Ak kľúč ešte neexistuje, cache.incr zlyhá. Vytvoríme ho s platnosťou 60 sekúnd.
        cache.set(cache_kluc, 1, 60)
        pocet_pokusov = 1
    
    if pocet_pokusov > 5:
        return JsonResponse({'status': 'error', 'message': 'Prekročený limit požiadaviek. Skúste to o minútu.'}, status=429)

    if request.method == "POST":
        try:
            data = json.loads(request.body)
            surovy_text = data.get('text', '')
            
            # 🏢 Vytiahnutie identity firmy pre kontext AI
            identita = FiremnaIdentita.objects.first()
            nazov_firmy = identita.nazov_spolocnosti if identita and identita.nazov_spolocnosti else "našej spoločnosti"
            
            if not surovy_text:
                return JsonResponse({
                    'status': 'success', 
                    'vysledok': f'Zariadenie bolo vizuálne aj technicky skontrolované technikom {nazov_firmy}. Neboli zistené žiadne závady brániace plynulej a bezpečnej prevádzke.'
                })

            # 🛡️ DEFENZÍVNA POISTKA (Graceful Degradation)
            api_key = get_gemini_api_key()
            gemini_model = getattr(settings, 'GEMINI_MODEL', None)
            if not api_key or not gemini_model:
                logger.warning("AI Protokol Generátor: Chýba GEMINI API kľúč alebo GEMINI_MODEL v konfigurácii.")
                return JsonResponse({
                    'status': 'error', 
                    'message': 'AI služba je momentálne nedostupná (chýba konfigurácia API kľúča alebo modelu).'
                }, status=503)

            client = genai.Client(api_key=api_key)
            
            # 🧠 ELITNÝ PROMPT PRE ISAS CORE (Enterprise Level)
            prompt = (
                f"Si hlavný technický audítor a servisný inžinier spoločnosti '{nazov_firmy}'. "
                f"Tvoj technik z terénu ti poslal tieto rýchle, neformálne a možno gramaticky nesprávne poznámky z výjazdu: '{surovy_text}'.\n\n"
                f"Tvojou úlohou je premeniť tieto poznámky na vysoko profesionálny, objektívny a formálny 'Výkaz o servisnom zásahu', ktorý bude slúžiť ako právny a fakturačný podklad pre zákazníka.\n\n"
                f"ŠTRUKTÚRA TEXTU (striktne dodržuj):\n"
                f"1. Úvodná deklarácia: Začni formálne (napr. 'Servisný tím spoločnosti {nazov_firmy} vykonal odborný zásah...').\n"
                f"2. Technická špecifikácia prác: Použi štandardné odrážky (pomocou znaku pomlčky '- '). "
                f"Preštylizuj hovorové výrazy do odbornej terminológie (napr. namiesto 'vymenil som diel' napíš 'Demontáž poškodeného komponentu a odborná montáž nového dielu'). "
                f"Používaj trpný rod (pasívum), znie to profesionálnejšie (napr. 'bola vykonaná', 'bol vymenený').\n"
                f"3. Záverečné stanovisko: Krátke zhodnotenie stavu zariadenia po servise a potvrdenie jeho prevádzkyschopnosti.\n\n"
                f"STRIKTNÉ PRAVIDLÁ:\n"
                f"- NEVYMÝŠĽAJ SI žiadne poruchy, diely ani úkony, ktoré technik nespomenul. Pracuj LEN s poskytnutým textom, iba ho odborne preštylizuj.\n"
                f"- Ak technik použil vulgarizmy alebo neprofesionálny slang (napr. 'zariadenie je na šrot'), prelož to do objektívneho technického jazyka (napr. 'Zariadenie vykazuje kritické opotrebenie nezlučiteľné s ďalšou prevádzkou').\n"
                f"- NEPOUŽÍVAJ markdown formátovanie (žiadne hviezdičky **text**). Text musí byť čistý (Plain Text).\n"
                f"- Nepíš žiadne vysvetlivky pre mňa, vráť PRIAMO finálny text pripravený na vloženie do protokolu."
            )
            
            response = client.models.generate_content(
                model=gemini_model,
                contents=[prompt],
                config=types.GenerateContentConfig(thinking_config=types.ThinkingConfig(thinking_level=getattr(settings, 'GEMINI_THINKING_LEVEL', 'MEDIUM') or 'MEDIUM'))
            )

            # Bezpečnostné očistenie od prípadného markdownu, ak by ho AI predsa len použilo
            vysledok = (response.text or "").replace('**', '').replace('*', '')
            
            return JsonResponse({'status': 'success', 'vysledok': vysledok})
        except Exception as e:
            logger.error(f"Chyba pri generovaní protokolu cez AI: {str(e)}", exc_info=True)
            return JsonResponse({'status': 'error', 'message': str(e)}, status=500)
    return JsonResponse({'status': 'invalid'}, status=400)

@staff_member_required
def api_kalkulacia_predpisu(request):
    """
    ISAS AUTOPILOT: Prepočíta predpis a vygeneruje položky.
    [ISAS TRUE-MARGIN ENGINE INTEGROVANÝ]
    """
    
    zariadenie_id = request.GET.get('zariadenie_id')
    typ_prace_ids = request.GET.getlist('typ_prace_ids[]') or request.GET.getlist('typ_prace_ids')
    vozidlo_id = request.GET.get('vozidlo_id')
    
    rezim_prace = request.GET.get('rezim_prace', 'hodinovka') 
    rezim_dopravy = request.GET.get('rezim_dopravy', 'kalkulacka') 
    
    # --- NOVÉ PARAMETRE ---
    havarijny_rezim = request.GET.get('havarijny_rezim') == 'true'
    technici_ids = request.GET.getlist('technici_ids[]') or request.GET.getlist('technici_ids')

    if not zariadenie_id or not str(zariadenie_id).isdigit() or not typ_prace_ids:
        return JsonResponse({'status': 'error', 'message': 'Chýbajú alebo sú neplatné vstupné parametre.'}, status=400)

    zariadenie = ServisovaneZariadenie.objects.filter(id=zariadenie_id).first()
    if not zariadenie:
        return JsonResponse({'status': 'error', 'message': 'Zariadenie nebolo nájdené.'}, status=404)

    zariadenie_nazov_formatovany = f"{zariadenie.model_zariadenia} (S/N: {zariadenie.vyrobne_cislo})"
    nastavenia = GlobalneNastavenia.objects.first()
    polozky_ponuky = []
    
    # ==========================================================
    # 🧠 [ISAS TRUE-MARGIN]: Výpočet reálnych mzdových nákladov
    # ==========================================================
    priemerna_mzda_naklad = decimal.Decimal('0.00')
    if technici_ids:
        ciste_technici_ids = [t for t in technici_ids if str(t).isdigit()]
        zvoleni_technici = Zamestnanec.objects.filter(id__in=ciste_technici_ids)
        if zvoleni_technici.exists():
            # 🧠 ISAS TRUE-MARGIN FIX: Zohľadnenie živnostníkov a mesačných platov
            total_mzda = decimal.Decimal('0.00')
            for t in zvoleni_technici:
                if t.typ_pomeru == 'EX':
                    total_mzda += (t.mzda_zivnostnik_hodina or decimal.Decimal('0.00'))
                else:
                    total_mzda += (t.interny_hodinovy_naklad or decimal.Decimal('0.00'))
            
            # Náklad na 1 normohodinu je súčet miezd chlapov, ktorých tam posielame (ak idú dvaja, platíme oboch)
            priemerna_mzda_naklad = decimal.Decimal(str(total_mzda))
            
    # 🧠 ISAS TRUE-MARGIN FALLBACK: Ak nie sú vybraní technici, použije sa firemný priemer technikov
    if priemerna_mzda_naklad == decimal.Decimal('0.00'):
        vsetci_technici = Zamestnanec.objects.filter(pracovna_rola=Zamestnanec.TECHNIK)
        pocet_tech = vsetci_technici.count()
        if pocet_tech > 0:
            total = sum(
                [(t.mzda_zivnostnik_hodina if t.typ_pomeru == 'EX' else t.interny_hodinovy_naklad) or decimal.Decimal('0.00') for t in vsetci_technici],
                decimal.Decimal('0.00')
            )
            priemerna_mzda_naklad = (total / decimal.Decimal(str(pocet_tech))).quantize(decimal.Decimal('0.01'))

    # =========================================================================
    # 🚨 VÝPOČET HAVARIJNEJ PRIRÁŽKY
    # =========================================================================
    # 🛡️ Ochrana pred NoneType a zlyhaním konverzie na Decimal
    priplatok_val = getattr(nastavenia, 'pohotovostny_priplatok', None) if nastavenia else None
    priplatok_percento = decimal.Decimal(str(priplatok_val)) if priplatok_val is not None else decimal.Decimal('0.00')
    
    koeficient_priplatku = decimal.Decimal('1.00')
    prefix_nazvu = ""
    
    if havarijny_rezim:
        koeficient_priplatku = decimal.Decimal('1.00') + (priplatok_percento / decimal.Decimal('100.00'))
        prefix_nazvu = "🚨 EXPRES/HAVÁRIA: "

    hodinova_val = getattr(nastavenia, 'hodinova_sadzba_technik', None) if nastavenia else None
    hodinova_sadzba = decimal.Decimal(str(hodinova_val)) if hodinova_val is not None else decimal.Decimal('0.00')

    # =========================================================================
    # 🚗 1. DOPRAVA A LOGISTIKA
    # =========================================================================
    celkove_km = decimal.Decimal('0.00')
    celkove_hodiny_cesta = decimal.Decimal('0.00')
    naklad_na_cestu = decimal.Decimal('0.00')

    label_vyjazd = get_industry_term('vyjazd_label', 'Servisný výjazd')
    jednotka_vyjazd = get_industry_term('vyjazd_jednotka', 'výjazd')

    if rezim_dopravy == 'pausal':
        vyjazd_val = getattr(nastavenia, 'cena_za_vyjazd', None) if nastavenia else None
        zakladna_cena_dopravy = decimal.Decimal(str(vyjazd_val)) if vyjazd_val is not None else decimal.Decimal('0.00')
        cena_dopravy = zakladna_cena_dopravy * koeficient_priplatku 
        text_dopravy = f"{prefix_nazvu}{label_vyjazd} (Paušálna sadzba)"
    else:
        # 🗺️ Ťaháme presnú adresu (Centrála, Filiálka, Domov), ktorá už bola poskladaná vo View
        start_adresa = request.GET.get('start_adresa')
        if not start_adresa or start_adresa.strip() == "":
            identita_obj = FiremnaIdentita.objects.first()
            start_adresa = identita_obj.adresa_prevadzky.strip() if (identita_obj and getattr(identita_obj, 'adresa_prevadzky', None)) else ""
            
        # Poskladanie cieľovej adresy u Zákazníka (PRESNÁ LOGIKA S PSČ)
        zakaznik = getattr(zariadenie, 'zakaznik', None)
        mesto_ciel = "Neznáme mesto"
        
        if zakaznik:
            parts_zakaznik = []
            ulica = getattr(zakaznik, 'ulica_a_cislo', '') or ''
            mesto = getattr(zakaznik, 'mesto', '') or ''
            psc = getattr(zakaznik, 'psc', '') or ''
            
            if ulica: parts_zakaznik.append(ulica.strip())
            
            # Formát: 914 42 Horné Srnie
            mesto_s_psc = f"{psc.strip()} {mesto.strip()}".strip()
            if mesto_s_psc: parts_zakaznik.append(mesto_s_psc)
            
            # 🪄 VIVID FIX: Extrémne čistý formát pre presné Geocoding API ("Ulica, PSČ Mesto")
            if parts_zakaznik:
                ciel_adresa = ", ".join(parts_zakaznik)
            else:
                ciel_adresa = getattr(zakaznik, 'Nazov_spolocnosti', '') or ""
                
            # Pre pekný vizuál v tabuľke ponuky vyextrahujeme iba holé Mesto
            mesto_ciel = mesto.strip() if mesto else getattr(zakaznik, 'Nazov_spolocnosti', 'Zákazník')
        else:
            ciel_adresa = ""
            
        # 🤖 Volanie AI Logistickej funkcie s defenzívnym fallbackom
        try:
            trasa_res = isas_ai_logistika_trasy(start_adresa, ciel_adresa)
            if trasa_res and isinstance(trasa_res, (tuple, list)) and len(trasa_res) == 2:
                km_raw, min_raw = trasa_res
                km_jedna_cesta = decimal.Decimal(str(km_raw).replace(',', '.').strip()) if km_raw is not None else decimal.Decimal('0.00')
                minuty_jedna_cesta = decimal.Decimal(str(min_raw).replace(',', '.').strip()) if min_raw is not None else decimal.Decimal('0.00')
            else:
                km_jedna_cesta, minuty_jedna_cesta = decimal.Decimal('0.00'), decimal.Decimal('0.00')
        except Exception as e:
            logger.warning(f"AI výpočet logistiky trasy zlyhal, aplikovaný fallback 0 km: {e}")
            km_jedna_cesta, minuty_jedna_cesta = decimal.Decimal('0.00'), decimal.Decimal('0.00')
        
        celkove_km = (km_jedna_cesta * decimal.Decimal('2.00')).quantize(decimal.Decimal('0.01'))
        celkove_hodiny_cesta = ((minuty_jedna_cesta * decimal.Decimal('2.00')) / decimal.Decimal('60.00')).quantize(decimal.Decimal('0.01'))

        # Bezpečné natiahnutie sadzieb z globálnych nastavení (odolné voči None v DB)
        km_val = getattr(nastavenia, 'sadzba_za_km_zakaznik', None) if nastavenia else None
        sadzba_za_km = decimal.Decimal(str(km_val)) if km_val is not None else decimal.Decimal('0.00')
        
        cas_val = getattr(nastavenia, 'sadzba_za_cas_na_ceste', None) if nastavenia else None
        sadzba_za_cas_na_ceste = decimal.Decimal(str(cas_val)) if cas_val is not None else decimal.Decimal('0.00')
        
        nazov_auta = label_vyjazd
        if vozidlo_id and str(vozidlo_id).isdigit():
            vozidlo = Vozidlo.objects.filter(id=vozidlo_id).first()
            if vozidlo:
                nazov_auta = f"{label_vyjazd} ({vozidlo.znacka_a_model})"
                if vozidlo.vlastna_sadzba_za_km is not None:
                    sadzba_za_km = decimal.Decimal(str(vozidlo.vlastna_sadzba_za_km))

        zakladna_cena_dopravy = (celkove_km * sadzba_za_km) + (celkove_hodiny_cesta * sadzba_za_cas_na_ceste)
        cena_dopravy = zakladna_cena_dopravy * koeficient_priplatku
        
        text_dopravy = f"{prefix_nazvu}{nazov_auta} do: {mesto_ciel} (Obojsmerne: {celkove_km.quantize(decimal.Decimal('0.1'))} km, Čas na ceste: {celkove_hodiny_cesta.quantize(decimal.Decimal('0.1'))} h)"

    # Vypočítame mzdový náklad na cestu (iba ak nejde o paušál)
    if rezim_dopravy != 'pausal':
        naklad_na_cestu = priemerna_mzda_naklad * celkove_hodiny_cesta

    polozky_ponuky.append({
        'typ': 'DOPRAVA',
        'nazov': text_dopravy,
        'mnozstvo': '1.00',
        'jednotka': jednotka_vyjazd,
        'cena_bez_dph': str(cena_dopravy.quantize(decimal.Decimal('0.01'))),
        'nakupna_cena': str(naklad_na_cestu.quantize(decimal.Decimal('0.01'))),
        'ct_id': None, 'obj_id': None,
        'zariadenie_id': zariadenie.id,
        'zariadenie_nazov': zariadenie_nazov_formatovany
    })

    # =========================================================================
    # 👨‍🔧 2. PRÁCA
    # =========================================================================
    ciste_prace_ids = [p for p in typ_prace_ids if str(p).isdigit()]
    vybrane_prace = TypServisuPrace.objects.filter(id__in=ciste_prace_ids)
    
    # ISAS PERFORMANCE: Hromadné načítanie predpisov pred cyklom (Ochrana pred N+1)
    predpisy_qs = ServisnyPredpis.objects.prefetch_related('polozky').filter(zariadenie=zariadenie, typ_prace__in=vybrane_prace)
    predpisy_map = {p.typ_prace_id: p for p in predpisy_qs}
    
    for praca in vybrane_prace:
        if praca.je_fakturovatelna:
            
            # 🧠 BIZNIS LOGIKA: Zisťujeme režim (Z UI alebo Predvolený z DB)
            je_fixny_rezim = (rezim_prace == 'fix') or (praca.sposob_uctovania == 'FIX')
            normohodiny = decimal.Decimal(str(praca.normohodiny or '1.00'))

            if je_fixny_rezim:
                # --- REŽIM: FIXNÁ CENA (ÚKON) ---
                # Ak máme fixnú cenu zadanú, použijeme ju. Inak dopočítame ako (Hodinovka x Normohodiny).
                if praca.fixna_cena_predaj and praca.fixna_cena_predaj > 0:
                    zaklad_cena = praca.fixna_cena_predaj
                else:
                    sadzba_p = praca.hodinova_sadzba_predaj if (praca.hodinova_sadzba_predaj and praca.hodinova_sadzba_predaj > 0) else hodinova_sadzba
                    zaklad_cena = sadzba_p * normohodiny

                # Náklad: Fixný náklad ak existuje, inak časová mzda x hodiny
                if praca.fixny_naklad and praca.fixny_naklad > 0:
                    zaklad_naklad = praca.fixny_naklad
                else:
                    sadzba_n = praca.hodinova_sadzba_naklad if (praca.hodinova_sadzba_naklad and praca.hodinova_sadzba_naklad > 0) else priemerna_mzda_naklad
                    zaklad_naklad = sadzba_n * normohodiny

                cena_prace = zaklad_cena * koeficient_priplatku
                mnozstvo_prace = decimal.Decimal('1.00')
                jednotka_prace = 'ks'
                naklad_prace = zaklad_naklad # [TRUE-MARGIN] Celý náklad úkonu
                
            else:
                # --- REŽIM: HODINOVKA ---
                sadzba_p = praca.hodinova_sadzba_predaj if (praca.hodinova_sadzba_predaj and praca.hodinova_sadzba_predaj > 0) else hodinova_sadzba
                cena_prace = sadzba_p * koeficient_priplatku
                mnozstvo_prace = normohodiny
                jednotka_prace = 'hod'
                
                # Náklad na 1 normohodinu tohto špecifického úkonu (napr. špeci zváranie vs údržba)
                naklad_prace = praca.hodinova_sadzba_naklad if (praca.hodinova_sadzba_naklad and praca.hodinova_sadzba_naklad > 0) else priemerna_mzda_naklad
                
            polozky_ponuky.append({
                'typ': 'PRACA',
                'nazov': f"{prefix_nazvu}Odborná servisná práca: {praca.nazov}",
                'mnozstvo': str(d_round(mnozstvo_prace)),
                'jednotka': jednotka_prace,
                'cena_bez_dph': str(d_round(cena_prace)),
                'nakupna_cena': str(d_round(naklad_prace)), # 🛡️ Zapíše sa ticho na pozadie pre P&L Reporty
                'ct_id': None, 'obj_id': None,
                'zariadenie_id': zariadenie.id,
                'zariadenie_nazov': zariadenie_nazov_formatovany
            })

        # =========================================================================
        # 📦 3. MATERIÁL
        # =========================================================================
        predpis = predpisy_map.get(praca.id)
        if predpis:
            # ISAS PERFORMANCE: Hromadné načítanie katalógu (Ochrana pred N+1)
            polozky_list = list(predpis.polozky.all())
            obj_ids = [p.object_id for p in polozky_list]
            katalog_map = {f"{k.content_type_id}_{k.object_id}": k for k in CenovyKatalog.objects.filter(object_id__in=obj_ids)}

            for polozka_predpisu in polozky_list:
                try:
                    diel = polozka_predpisu.diel
                except ObjectDoesNotExist:
                    diel = None
                if not diel:
                    continue
                    
                ct_id = polozka_predpisu.content_type_id
                katalog = katalog_map.get(f"{ct_id}_{diel.id}")
                cena_dielu = katalog.predajna_cena if (katalog and katalog.predajna_cena is not None) else decimal.Decimal('0.00')
                nakup_dielu = katalog.nakupna_cena if (katalog and katalog.nakupna_cena is not None) else decimal.Decimal('0.00')
                
                cisty_nazov = ocisti_nazov_od_jednotky(str(diel))

                polozky_ponuky.append({
                    'typ': 'MAT',
                    'nazov': cisty_nazov,
                    'mnozstvo': str(d_round(polozka_predpisu.mnozstvo)),
                    'jednotka': getattr(diel, 'merna_jednotka', 'ks'),
                    'cena_bez_dph': str(d_round(cena_dielu)),
                    'nakupna_cena': str(d_round(nakup_dielu)),
                    'ct_id': ct_id,
                    'obj_id': diel.id,
                    'zariadenie_id': zariadenie.id,
                    'zariadenie_nazov': zariadenie_nazov_formatovany
                })

    return JsonResponse({
        'status': 'success',
        'polozky': polozky_ponuky,
        'dph': str(nastavenia.zakladna_sadzba_dph) if nastavenia and nastavenia.zakladna_sadzba_dph is not None else str(GlobalneNastavenia.get_zakladna_dph())
    })


@staff_member_required
def api_ulozit_ponuku(request):
    """
    Prijme JSON z Konfigurátora, uloží ponuku, vygeneruje PDF a odošle ju zákazníkovi.
    """
    if request.method != "POST":
        return JsonResponse({'status': 'invalid'}, status=405)

    try:
        data = json.loads(request.body)
        if not isinstance(data, dict):
            return JsonResponse({'status': 'error', 'message': 'Neplatný dátový formát požiadavky (očakáva sa JSON objekt).'}, status=400)
            
        zakaznik_id = data.get('zakaznik_id')
        if not zakaznik_id or not str(zakaznik_id).isdigit():
            return JsonResponse({'status': 'error', 'message': 'Neplatný zákazník.'}, status=400)
            
        zakaznik = Zakaznik.objects.filter(id=zakaznik_id).first()
        if not zakaznik:
            return JsonResponse({'status': 'error', 'message': 'Zákazník nebol nájdený.'}, status=404)

        zariadenie_id = data.get('zariadenie_id')

        zariadenie = ServisovaneZariadenie.objects.filter(id=zariadenie_id, zakaznik=zakaznik).first() if zariadenie_id and str(zariadenie_id).isdigit() else None
        
        typ_prace_ids = data.get('typ_prace_ids', [])
        ciste_prace_ids = [p for p in typ_prace_ids if str(p).isdigit()]
        polozky_data = data.get('polozky', [])
        
        prenos_dph = data.get('prenos_danovej_povinnosti', False)
        
        zlava_raw = data.get('globalna_zlava_percento', '0.00')
        try:
            cista_zlava_str = str(zlava_raw if zlava_raw not in [None, ""] else '0.00').replace(',', '.').strip()
            globalna_zlava = decimal.Decimal(cista_zlava_str)
        except (decimal.InvalidOperation, TypeError):
            globalna_zlava = decimal.Decimal('0.00')
        globalna_zlava = max(decimal.Decimal('0.00'), min(decimal.Decimal('100.00'), globalna_zlava))
        
        # Vystavovateľ je priamo prihlásený užívateľ (User)!
        aktualny_uzivatel = request.user if request.user.is_authenticated else None
        nastavenia = GlobalneNastavenia.objects.first()
        predvolena_dph = nastavenia.zakladna_sadzba_dph if nastavenia and nastavenia.zakladna_sadzba_dph is not None else GlobalneNastavenia.get_zakladna_dph()

        # 1. 🛡️ ATOMICKÉ ULOŽENIE ALEBO AKTUALIZÁCIA DO DATABÁZY (S META-ÚDAJMI PRE BUILDER)
        as_draft = data.get('as_draft', False)
        ponuka_id = data.get('ponuka_id')

        metadata = {
            'technici_ids': data.get('technici_ids', []),
            'strategia_prace': data.get('strategia_prace', 'hodinovka'),
            'vozidlo_id': data.get('vozidlo_id', ''),
            'start_trasy': data.get('start_trasy', 'HQ_CENTRAL'),
            'strategia_dopravy': data.get('strategia_dopravy', 'kalkulacka'),
            'havarijny_rezim': data.get('havarijny_rezim', False),
            'notif_tg': data.get('notif_admin_tg', True),
            'notif_mail': data.get('notif_admin_mail', True),
            'notif_discord': data.get('notif_admin_discord', True),
        }
        metadata_serialized = "[ISAS_METADATA_JSON]" + json.dumps(metadata)

        with transaction.atomic():
            mena_ponuky = (str(data.get('mena') or '').strip().upper()[:10]) or get_firemna_mena()
            if ponuka_id and str(ponuka_id).isdigit():
                # 🔒 ISAS SECURITY: Uzamknutie ponuky počas prepisovania položiek
                ponuka = CenovaPonuka.objects.select_for_update().filter(id=ponuka_id).first()
                if not ponuka:
                    return JsonResponse({'status': 'error', 'message': 'Cenová ponuka nebola nájdená v databáze.'}, status=400)
                ponuka.zakaznik = zakaznik
                ponuka.zariadenie = zariadenie
                ponuka.prenos_danovej_povinnosti = prenos_dph
                ponuka.globalna_zlava_percento = globalna_zlava
                ponuka.ai_uvodny_text = data.get('ai_uvodny_text', '')
                ponuka.interne_poznamky = metadata_serialized
                ponuka.stav = 'DRAFT' if as_draft else 'SENT'
                if hasattr(ponuka, 'mena'):
                    ponuka.mena = mena_ponuky
                # Vymažeme staré priradené položky pred vygenerovaním nových, aby nevznikli duplicity
                ponuka.polozky.all().delete()
            else:
                ponuka_kwargs = {
                    'zakaznik': zakaznik,
                    'zariadenie': zariadenie,
                    'vystavil': aktualny_uzivatel, 
                    'prenos_danovej_povinnosti': prenos_dph,
                    'globalna_zlava_percento': globalna_zlava,
                    'ai_uvodny_text': data.get('ai_uvodny_text', ''),
                    'interne_poznamky': metadata_serialized,
                    'stav': 'DRAFT' if as_draft else 'SENT'
                }
                if hasattr(CenovaPonuka, 'mena'):
                    ponuka_kwargs['mena'] = mena_ponuky
                ponuka = CenovaPonuka(**ponuka_kwargs)
            ponuka.save()
            
            if ciste_prace_ids:
                ponuka.typ_servisu.set(TypServisuPrace.objects.filter(id__in=ciste_prace_ids))

            sum_bez_dph_pred_zlavou = decimal.Decimal('0.00')
            sum_dph = decimal.Decimal('0.00')
            zakaznik_zariadenia_ids = set(ServisovaneZariadenie.objects.filter(zakaznik=zakaznik).values_list('id', flat=True))

            for p in (polozky_data if isinstance(polozky_data, list) else []):
                if not isinstance(p, dict):
                    continue
                mnozstvo_raw = str(p.get('mnozstvo') or '1.00').replace(',', '.').strip()
                cena_bez_raw = str(p.get('cena_bez_dph') or '0.00').replace(',', '.').strip()
                zlava_riadok_raw = str(p.get('zlava_percento') or '0.00').replace(',', '.').strip()
                sadzba_dph_raw = str(p.get('sadzba_dph') or '').replace(',', '.').strip()
                
                try:
                    mnozstvo = decimal.Decimal(mnozstvo_raw) if mnozstvo_raw else decimal.Decimal('1.00')
                except decimal.InvalidOperation:
                    mnozstvo = decimal.Decimal('1.00')
                try:
                    cena_bez = decimal.Decimal(cena_bez_raw) if cena_bez_raw else decimal.Decimal('0.00')
                except decimal.InvalidOperation:
                    cena_bez = decimal.Decimal('0.00')
                try:
                    zlava_riadok = decimal.Decimal(zlava_riadok_raw) if zlava_riadok_raw else decimal.Decimal('0.00')
                except decimal.InvalidOperation:
                    zlava_riadok = decimal.Decimal('0.00')
                try:
                    sadzba_dph = decimal.Decimal(sadzba_dph_raw) if sadzba_dph_raw else predvolena_dph
                except decimal.InvalidOperation:
                    sadzba_dph = predvolena_dph
                
                ct_id = p.get('ct_id')
                obj_id = p.get('obj_id')
                
                # ISAS PERFORMANCE: get_for_id() využíva internú RAM cache Djanga (0 DB queries)
                try:
                    ct = ContentType.objects.get_for_id(int(ct_id)) if ct_id and str(ct_id).isdigit() else None
                except ContentType.DoesNotExist:
                    ct = None

                zar_id_riadok = p.get('zariadenie_id')
                zar_id_valid = int(zar_id_riadok) if (zar_id_riadok and str(zar_id_riadok).isdigit() and int(zar_id_riadok) in zakaznik_zariadenia_ids) else None

                polozka = PolozkaCenovejPonuky(
                    ponuka=ponuka,
                    typ_polozky=p.get('typ', 'MAT'),
                    nazov=p.get('nazov', 'Neznáma položka'),
                    mnozstvo=mnozstvo,
                    merna_jednotka=p.get('jednotka', 'ks'),
                    jednotkova_cena_bez_dph=cena_bez,
                    zlava_percento=zlava_riadok,
                    sadzba_dph=sadzba_dph,
                    content_type=ct,
                    object_id=obj_id if ct and str(obj_id).isdigit() else None,
                    zariadenie_id=zar_id_valid
                )
                polozka.save()

                # Fix: Zohľadňujeme záporné sumy zľavy v backende pre správny súčet
                riadok_zaklad = mnozstvo * cena_bez
                sum_bez_dph_pred_zlavou += riadok_zaklad
                sum_dph += (polozka.hodnota_dph or decimal.Decimal('0.00'))

            # Aplikovanie Globálnej zľavy
            koeficient_global_zlavy = decimal.Decimal('1.00') - (globalna_zlava / decimal.Decimal('100.00'))
            
            ponuka.celkom_bez_dph_pred_zlavou = d_round(sum_bez_dph_pred_zlavou)
            celkom_bez_dph_po_riadkovych = sum((decimal.Decimal(str(p.cena_po_zlave_bez_dph or '0.00')) for p in ponuka.polozky.all()), decimal.Decimal('0.00'))
            
            ponuka.celkom_bez_dph = d_round(celkom_bez_dph_po_riadkovych * koeficient_global_zlavy)
            ponuka.hodnota_zlavy = d_round(ponuka.celkom_bez_dph_pred_zlavou - ponuka.celkom_bez_dph)
            ponuka.celkom_dph = d_round(sum_dph * koeficient_global_zlavy)
            ponuka.celkom_s_dph = ponuka.celkom_bez_dph + ponuka.celkom_dph
            ponuka.save()

        base_url = request.build_absolute_uri('/')
        def _safe_generuj_ponuku_pdf(p_id, b_url, n_tg, n_m, n_d, s_notif):
            try:
                generuj_ponuku_pdf_task.delay(p_id, b_url, n_tg, n_m, n_d, send_notif=s_notif)
            except Exception as exc:
                logger.warning(f"Zlyhalo zaradenie generuj_ponuku_pdf_task do Celery pre ponuku {p_id}: {exc}")

        if as_draft:
            # 📄 Vygenerovanie PDF aj pre DRAFT (tichý režim bez odosielania klientovi a vedeniu)
            transaction.on_commit(lambda p_id=ponuka.id, b_url=base_url: _safe_generuj_ponuku_pdf(
                p_id, b_url, False, False, False, False
            ))
            success_message = '📝 Koncept cenovej ponuky bol úspešne uložený. PDF sa generuje na pozadí.'
        else:
            notif_tg = data.get('notif_admin_tg', True)
            notif_mail = data.get('notif_admin_mail', True)
            notif_discord = data.get('notif_admin_discord', True)
            transaction.on_commit(lambda p_id=ponuka.id, b_url=base_url, n_tg=notif_tg, n_m=notif_mail, n_d=notif_discord: _safe_generuj_ponuku_pdf(
                p_id, b_url, n_tg, n_m, n_d, True
            ))
            success_message = '✅ Ponuka bola uložená. PDF sa generuje na pozadí a bude odoslané zákazníkovi.'

        return JsonResponse({'status': 'success', 'ponuka_id': ponuka.id, 'message': success_message})

    except Exception as e:
        traceback.print_exc() # Vynúti výpis celej chyby do tvojho terminálu
        return JsonResponse({'status': 'error', 'message': f'Nastala chyba pri ukladaní: {str(e)}'}, status=400)

@staff_member_required
def api_ai_generuj_uvod(request):
    """
    AI vygeneruje profesionálny, obchodný úvod ponuky.
    [ISAS AI TECHNOLÓG INTEGROVANÝ - MULTI-DEVICE FLEET ENGINE]
    """
    if request.method != "POST": return JsonResponse({'status': 'invalid'}, status=405)
    try:
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'status': 'error', 'message': '⚠️ Neplatný formát dát (JSON).'}, status=400)
            
        zakaznik_id = data.get('zakaznik_id')
        
        identita = FiremnaIdentita.objects.first()
        nazov_nasej_firmy = identita.nazov_spolocnosti if identita else "našej spoločnosti"
        
        if not zakaznik_id or not str(zakaznik_id).isdigit():
            return JsonResponse({'status': 'error', 'message': 'Neplatný zákazník.'}, status=400)
            
        zakaznik = Zakaznik.objects.filter(id=zakaznik_id).first()
        if not zakaznik:
            return JsonResponse({'status': 'error', 'message': 'Zákazník nebol nájdený.'}, status=404)
        
        # Prijímame pole zariadení a typov prác
        zariadenia_ids = data.get('zariadenia_ids', [])
        typ_servisu_ids = data.get('typ_servisu_ids', [])
        
        kontext_strojov = ""
        kontext_servisu = ""
        
        # 🛠️ 1. KONTEXT PLÁNOVANÉHO SERVISU
        if typ_servisu_ids and isinstance(typ_servisu_ids, list):
            ciste_typ_servisu_ids = [t for t in typ_servisu_ids if str(t).isdigit()]
            typy_prac = TypServisuPrace.objects.filter(id__in=ciste_typ_servisu_ids)
            if typy_prac.exists():
                nazvy_prac = [p.nazov for p in typy_prac]
                najvyssia_priorita = "VYSOKA" if typy_prac.filter(priorita="VYSOKA").exists() else "STREDNA"
                max_zaruka = typy_prac.aggregate(Max('zaruka_mesiace'))['zaruka_mesiace__max']
                celkove_normohodiny = typy_prac.aggregate(Sum('normohodiny'))['normohodiny__sum'] or decimal.Decimal('0.00')
                
                vsetky_zadarmo = not any(p.je_fakturovatelna for p in typy_prac)
                obsahuje_fix = any(p.sposob_uctovania == 'FIX' for p in typy_prac)
                
                if vsetky_zadarmo:
                    fakturacia_text = "Ide o BEZPLATNÝ / ZÁRUČNÝ servis."
                elif obsahuje_fix:
                    fakturacia_text = "Ide o platený servis (obsahuje úkony s garantovanou FIXNOU cenou)."
                else:
                    fakturacia_text = "Ide o platený servis (účtovaný podľa reálne odpracovaných hodín)."

                zaruka_riadok = f"Poskytovaná záruka na prácu: {max_zaruka} mesiacov.\n" if max_zaruka else ""
                kontext_servisu = (
                    f"Plánované úkony: {', '.join(nazvy_prac)}.\n"
                    f"Odhadovaná časová náročnosť: {celkove_normohodiny} hodín.\n"
                    f"Priorita zásahu: {najvyssia_priorita}.\n"
                    f"{zaruka_riadok}"
                    f"Finančný charakter: {fakturacia_text}\n"
                )

        # 🛂 2. BEZPEČNOSTNÝ A INTERNÝ KONTEXT ZÁKAZNÍKA
        bezpecnost_text = ""
        if zakaznik.vyzaduje_povolenie_vstupu:
            bezpecnost_text = "Zákazník má prísne bezpečnostné predpisy (vyžaduje povolenie na vstup/Gate Pass)."
            if zakaznik.instrukcie_vstupu:
                bezpecnost_text += f" Špecifické inštrukcie pre vstup: {zakaznik.instrukcie_vstupu}"
            bezpecnost_text += "\n"
            
        poznamka_zakaznik_text = f"Interná poznámka k zákazníkovi: {zakaznik.Ostatne}\n" if zakaznik.Ostatne else ""

        # 🚀 3. SPRACOVANIE FLOTILY ZARIADENÍ (SMART FLEET SUMMARIZATION & COMPLIANCE SCAN)
        kontext_compliance = ""
        if zariadenia_ids:
            ciste_zar_ids = [z for z in zariadenia_ids if str(z).isdigit()]
            zariadenia = ServisovaneZariadenie.objects.select_related('vyrobca').filter(id__in=ciste_zar_ids, zakaznik=zakaznik)
            pocet_zariadeni = len(zariadenia)
            
            # ⚖️ ISAS COMPLIANCE AUDIT PRE PONUKU (Regulatory Upselling)
            compliance_alerty_ponuka = []
            for zar_item in zariadenia:
                for c_item in get_zariadenie_compliance_audit(zar_item):
                    if c_item.get('vyzaduje_alert'):
                        stroj_popis = zar_item.model_zariadenia or zar_item.nazov or "Zariadenie"
                        sn_popis = f" (SN: {zar_item.vyrobne_cislo})" if zar_item.vyrobne_cislo else ""
                        prenajom_tag = " [V NÁŠHO PRENÁJME]" if zar_item.v_prenajme_od_isas else ""
                        compliance_alerty_ponuka.append(
                            f"- {stroj_popis}{sn_popis}{prenajom_tag}: {c_item['nazov']} -> {c_item['stav_str']} (Odborný dôvod: {c_item.get('ai_poznamka', 'Nutná revízna kontrola')})"
                        )
            if compliance_alerty_ponuka:
                kontext_compliance = (
                    "ZÁKONNÉ A REVÍZNE LEHOTY VYŽADUJÚCE POZORNOSŤ (OPRETIE PONUKY O LEGISLATÍVU):\n"
                    + "\n".join(compliance_alerty_ponuka) + "\n"
                )
            
            if pocet_zariadeni > 0:
                # 🧠 BIZNIS LOGIKA: Ak je strojov veľa (4 a viac), robíme len agregáciu, aby AI nehalucinovalo z prebytku dát
                if pocet_zariadeni > 3:
                    kontext_strojov = f"Predmet ponuky zahŕňa plošný servis/údržbu flotily {pocet_zariadeni} zariadení.\nZoznam položiek vo flotile:\n"
                    for zar in zariadenia:
                        kategoria_nazov = zar.get_kategoria_display() if hasattr(zar, 'get_kategoria_display') else "Zariadenie"
                        vyrobca = str(zar.vyrobca) if zar.vyrobca else "Neznámy"
                        mh_text = f" ({zar.meter_display})" if zar.aktualne_motohodiny and zar.aktualne_motohodiny > 0 else ""
                        kontext_strojov += f"- {kategoria_nazov} {vyrobca} {zar.model_zariadenia}{mh_text}\n"
                    kontext_strojov += "Inštrukcia pre AI: Ide o hromadný servis flotily. Nespomínaj konkrétne položky, zhrň to ako 'komplexnú údržbu vašej technológie a prevádzkových systémov'.\n"

                # 🧠 BIZNIS LOGIKA: Ak sú zariadenia 1 až 3, ideme do hĺbky (Deep DNA Extraction)
                else:
                    kontext_strojov = "Predmet ponuky zahŕňa detailný servis nasledujúcich zariadení:\n"
                    for zar in zariadenia:
                        vyrobca = str(zar.vyrobca) if zar.vyrobca else "Neznámy výrobca"
                        interny_nazov_text = f" (Interný názov: '{zar.nazov}')" if zar.nazov else ""
                        label_umiestnenie = get_industry_term('umiestnenie', 'Umiestnenie')
                        umiestnenie_text = f" [{label_umiestnenie}: {zar.umiestnenie}]" if zar.umiestnenie else ""
                        poznamka_stroj_text = f" - Špecifická poznámka: {zar.poznamka}" if zar.poznamka else ""

                        kategoria_nazov = zar.get_kategoria_display() if hasattr(zar, 'get_kategoria_display') else "Zariadenie"
                        prenajom_text = " [⚠️ V PRENÁJME OD NÁS]" if zar.v_prenajme_od_isas else ""
                        
                        label_rok = get_industry_term('rok_vyroby', 'Rok výroby')
                        label_zaruka = get_industry_term('zaruka', 'Záruka')
                        zive_data = []
                        if zar.rok_vyroby: zive_data.append(f"{label_rok}: {zar.rok_vyroby}")
                        if zar.datum_instalacie: zive_data.append(f"Inštalácia: {zar.datum_instalacie.strftime('%d.%m.%Y')}")
                        if zar.aktualne_motohodiny and zar.aktualne_motohodiny > 0: zive_data.append(f"{zar.meter_label}: {zar.meter_display}")
                        if zar.motohodiny_v_zatazi and zar.motohodiny_v_zatazi > 0: zive_data.append(f"{zar.meter_load_label}: {zar.meter_load_display}")
                        if zar.iot_mac_adresa or zar.iot_protokol: zive_data.append("Aktívne IoT monitorovanie")
                        
                        # 🛡️ Záručný kontext pre AI Technológa
                        dnes_datum = timezone.now().date()
                        if zar.predlzena_zaruka_do and zar.predlzena_zaruka_do >= dnes_datum:
                            zive_data.append(f"{label_zaruka}: 💎 Predĺžená platná do {zar.predlzena_zaruka_do.strftime('%d.%m.%Y')}")
                        elif zar.zaruka_do and zar.zaruka_do >= dnes_datum:
                            zive_data.append(f"{label_zaruka}: 🛡️ Štandardná platná do {zar.zaruka_do.strftime('%d.%m.%Y')}")
                        elif zar.zaruka_do:
                            zive_data.append(f"{label_zaruka}: ⚪ Po záruke (skončila {zar.zaruka_do.strftime('%d.%m.%Y')})")
                        
                        zive_data_text = f" | Prevádzkové dáta: {', '.join(zive_data)}" if zive_data else ""
                    
                        technicke_detaily = []
                        ignorovane_polia_ai = ['id', 'zariadenie', 'vytvorene', 'upravene', 'vytvoril', 'upravil']
                        param_obj = get_zariadenie_parametre(zar)

                        if param_obj and hasattr(param_obj, '_meta'):
                            try:
                                for field in param_obj._meta.fields:
                                    if field.name not in ignorovane_polia_ai and not field.is_relation:
                                        display_method = getattr(param_obj, f"get_{field.name}_display", None)
                                        hodnota = display_method() if callable(display_method) else getattr(param_obj, field.name, None)
                                        if hodnota is not None and str(hodnota).strip() != "":
                                            technicke_detaily.append(f"{field.verbose_name}: {hodnota}")
                            except Exception:
                                pass
                        
                        dna_text = f" | Parametre: {', '.join(technicke_detaily)}" if technicke_detaily else ""
                        kontext_strojov += f"- {kategoria_nazov} {vyrobca} {zar.model_zariadenia} (S/N: {zar.vyrobne_cislo}){interny_nazov_text}{umiestnenie_text}{prenajom_text}{zive_data_text}{dna_text}{poznamka_stroj_text}\n"

        # 🛡️ DEFENZÍVNA POISTKA (Graceful Degradation)
        api_key = get_gemini_api_key()
        gemini_model = getattr(settings, 'GEMINI_MODEL', None)
        if not api_key or not gemini_model:
            logger.warning("AI Generátor Úvodu Ponuky: Chýba GEMINI API kľúč alebo GEMINI_MODEL v konfigurácii.")
            return JsonResponse({
                'status': 'error', 
                'message': 'AI služba je momentálne nedostupná (chýba konfigurácia API kľúča alebo modelu).'
            }, status=503)

        client = genai.Client(api_key=api_key)
        
        titul = f"{zakaznik.oslovenie_rod} " if zakaznik.oslovenie_rod else ""
        osoba_text = f" (Kontaktná osoba: {titul}{zakaznik.kontaktna_osoba})" if zakaznik.kontaktna_osoba else ""

        # 🧠 4. ULTIMÁTNY PROMPT
        prompt = f"""Si senior servisný inžinier a B2B manažér spoločnosti '{nazov_nasej_firmy}'.
Tvojou úlohou je napísať vysoko profesionálny, vecný a technicky presný úvodný odsek (max 3-4 vety) do cenovej ponuky pre zákazníka '{zakaznik.Nazov_spolocnosti}'{osoba_text}.

<DATA_Z_DATABAZY>
[ZARIADENIA]:
{kontext_strojov}
[PLANOVANY_SERVIS]:
{kontext_servisu}
[LEGISLATÍVA_A_REVIZIE]:
{kontext_compliance if kontext_compliance else f"Všetky dotknuté {get_industry_term('zariadenia_mnozno', 'zariadenia')} majú platné revízie."}
[BEZPECNOST_A_VSTUP]:
{bezpecnost_text}
[INTERNE_POZNAMKY]:
{poznamka_zakaznik_text}
</DATA_Z_DATABAZY>

<STRIKTNE_PRAVIDLA_A_ZAKAZY>
1. ZAKÁZANÉ HALUCINOVAŤ: Nespomínaj ŽIADNE vymyslené technické úkony, diely ani služby, ktoré nie sú explicitne uvedené v blokoch [PLANOVANY_SERVIS] alebo [LEGISLATÍVA_A_REVIZIE].
2. ZAKÁZANÉ SĽUBOVAŤ: Nevymýšľaj si termíny dodania, časy príchodu, ani služby navyše (napr. náhradné zariadenie).
3. ZAKÁZANÉ CENY: Nespomínaj žiadne konkrétne sumy cien ani sadzieb v akejkoľvek mene.
4. OCHRANA INTERNÝCH DÁT: Blok [INTERNE_POZNAMKY] slúži len pre tvoj kontext (aby si vedel, ako so zákazníkom jednať). NIKDY tieto poznámky necituj a nespomínaj ich v texte!
5. ZAKÁZANÁ VATA: Žiadne marketingové klišé ("sme lídri na trhu"). Píš ako inžinier - odborne, k veci, s rešpektom.
6. ZAKÁZANÉ FORMÁTOVANIE: Nevracaj žiadne oslovenie (žiadne "Vážený zákazník") a žiadnu pätičku (žiadne "S pozdravom"). Vráť IBA samotný text odseku. Žiadny markdown (žiadne **tučné** písmo).
7. ZÁKAZ PRÁVNYCH ZÁRUK: Lehoty revízií a enviro-kontrol uvádzaj výhradne ako technicko-odborné odporúčanie z prevádzkovej evidencie na zabezpečenie kontinuity prevádzky, nikdy nedávaj záväzné právne záruky.
</STRIKTNE_PRAVIDLA_A_ZAKAZY>

<INSTRUKCIE_PRE_TVORBU_TEXTU>
- Začni priamo k veci.
- Ak ide o všeobecnú dodávku (bez konkrétnych zariadení), naformuluj to elegantne ako dodávku produktov a služieb pre ich prevádzku.
- Ak je zariadení viac, zhrň to elegantne (napr. "komplexný servis a údržba vašej technológie a systémov").
- Ak blok [LEGISLATÍVA_A_REVIZIE] obsahuje expirované lehoty alebo blížiace sa revízie, odborne to vyzdvihni ako hlavný technický a bezpečnostný dôvod realizácie tohto servisu (Regulatory Compliance a prevádzková bezpečnosť). Pri položkách označených [V NÁŠHO PRENÁJME] to prezentuj ako plnenie našej garancie bezpečnej prevádzky technológie.
- Ak sú zariadenia monitorované cez IoT/telemetriu, vyzdvihni to ako našu technologickú výhodu (proaktívny servis na základe presných dát).
- Ak zákazník vyžaduje povolenie na vstup, ubezpeč ho jednou vetou, že naši technici sú na to pripravení a plne rešpektujú ich interné predpisy.
- Ak ide o BEZPLATNÝ/ZÁRUČNÝ servis, zdôrazni to ako garanciu našej kvality.
- Ak je niektoré zariadenie v PRENÁJME, formuluj to ako proaktívnu starostlivosť o našu prenajatú technológiu.
- Ak ponuka obsahuje FIXNÚ cenu, spomeň to ako garanciu neprekročenia rozpočtu.
</INSTRUKCIE_PRE_TVORBU_TEXTU>

Vygeneruj text:"""
        
        # Volanie Gemini API s dynamickým modelom zo settings
        response = client.models.generate_content(
            model=gemini_model,
            contents=[prompt],
            config=types.GenerateContentConfig(thinking_config=types.ThinkingConfig(thinking_level=getattr(settings, 'GEMINI_THINKING_LEVEL', 'MEDIUM') or 'MEDIUM'))
        )

        # Posledná vrstva ochrany v Pythone (odstránenie markdownu a prípadných nechcených medzier)
        final_text = (response.text or "").replace('**', '').replace('*', '').strip()
        
        return JsonResponse({'status': 'success', 'text': final_text})
    except Exception as e:
        logger.error(f"ISAS AI ERROR v api_ai_generuj_uvod: {e}", exc_info=True)
        return JsonResponse({'status': 'error', 'message': 'Nepodarilo sa vygenerovať text. Skontrolujte pripojenie k AI službe.'}, status=500)

@staff_member_required
def cenova_ponuka_builder_view(request):
    """
    VIVID LIGHT SPA: Konfigurátor cenových ponúk (Optimalizované pre Command Center).
    """
    edit_id = request.GET.get('edit_id')
    existing_ponuka = None
    items_list = []
    metadata_dict = {}
    
    if edit_id and str(edit_id).isdigit():
        existing_ponuka = get_object_or_404(CenovaPonuka, id=edit_id)
        zakaznik_id = str(existing_ponuka.zakaznik_id)
        zariadenie_id = str(existing_ponuka.zariadenie_id) if existing_ponuka.zariadenie_id else None
        
        # Dešifrovanie a extrakcia UI konfigurácie z poľa interne_poznamky
        if existing_ponuka.interne_poznamky and existing_ponuka.interne_poznamky.startswith("[ISAS_METADATA_JSON]"):
            try:
                metadata_raw = existing_ponuka.interne_poznamky.replace("[ISAS_METADATA_JSON]", "", 1)
                parsed_meta = json.loads(metadata_raw) # Overenie validity JSON štruktúry
                if isinstance(parsed_meta, dict):
                    metadata_dict = parsed_meta
            except Exception:
                pass
        
        for pol in existing_ponuka.polozky.select_related('zariadenie').all():
            items_list.append({
                'typ': pol.typ_polozky,
                'nazov': pol.nazov,
                'mnozstvo': str(pol.mnozstvo),
                'jednotka': pol.merna_jednotka,
                'cena_bez_dph': str(pol.jednotkova_cena_bez_dph),
                'nakupna_cena': "0.00",
                'zlava_percento': str(pol.zlava_percento),
                'ct_id': pol.content_type_id or '',
                'obj_id': pol.object_id or '',
                'zariadenie_id': str(pol.zariadenie_id) if pol.zariadenie_id else '',
                'zariadenie_nazov': f"{pol.zariadenie.model_zariadenia} (S/N: {pol.zariadenie.vyrobne_cislo})" if pol.zariadenie else ''
            })
    else:
        zakaznik_id = request.GET.get('zakaznik_id')
        zariadenie_id = request.GET.get('zariadenie_id')
    
    zakaznici = Zakaznik.objects.all().order_by('Nazov_spolocnosti')
    typy_prac = TypServisuPrace.objects.all().order_by('nazov')
    vozidla = Vozidlo.objects.all().order_by('znacka_a_model')
    
    # Vytiahneme len reálnych technikov
    technici = Zamestnanec.objects.filter(
        pracovna_rola=Zamestnanec.TECHNIK
    ).order_by('meno', 'priezvisko')
    
    nastavenia = GlobalneNastavenia.objects.first()
    
    zariadenia_zakaznika = []
    if zakaznik_id and str(zakaznik_id).isdigit():
        zariadenia_zakaznika = ServisovaneZariadenie.objects.filter(zakaznik_id=zakaznik_id)

    back_url = reverse('admin:Evidencia_cenovaponuka_changelist')
    if zariadenie_id:
        back_url = reverse('admin:Evidencia_servisovanezariadenie_change', args=[zariadenie_id])
    elif zakaznik_id:
        back_url = reverse('admin:Evidencia_zakaznik_change', args=[zakaznik_id])

    js_category_map = {}
    for main_cat, subcats in CATEGORY_MAP.items():
        js_category_map[main_cat] = {name: model.__name__ for name, model in subcats.items()}
        
    # Vytvorenie dynamickej mapy pre Emojis z tvojich Choices
    js_category_emojis = {k: v for k, v in CATEGORY_CHOICES}

    sadzba_praca = getattr(nastavenia, 'hodinova_sadzba_technik', None) if nastavenia and getattr(nastavenia, 'hodinova_sadzba_technik', None) is not None else decimal.Decimal('0.00')
    sadzba_doprava_pausal = getattr(nastavenia, 'cena_za_vyjazd', None) if nastavenia and getattr(nastavenia, 'cena_za_vyjazd', None) is not None else decimal.Decimal('0.00')
    pohotovostny_priplatok = getattr(nastavenia, 'pohotovostny_priplatok', None) if nastavenia and getattr(nastavenia, 'pohotovostny_priplatok', None) is not None else decimal.Decimal('0.00')

    # 🛡️ ISAS TRUE-MARGIN: Výpočet priemerného nákladu na mzdu pre manuálne riadky práce
    priemerna_mzda_naklad = decimal.Decimal('0.00')
    if technici.exists():
        total_mzda = sum(
            [(t.mzda_zivnostnik_hodina if t.typ_pomeru == 'EX' else t.interny_hodinovy_naklad) or decimal.Decimal('0.00') for t in technici],
            decimal.Decimal('0.00')
        )
        pocet_t = technici.count()
        if pocet_t > 0:
            priemerna_mzda_naklad = (total_mzda / decimal.Decimal(str(pocet_t))).quantize(decimal.Decimal('0.01'))

    # 🧠 ISAS SMART MANUAL: Exportujeme špecifické dáta do JSON pre Javascript
    vozidla_map = {}
    for v in vozidla:
        vozidla_map[str(v.id)] = {
            'nazov': v.znacka_a_model,
            'sadzba': float(v.vlastna_sadzba_za_km) if v.vlastna_sadzba_za_km else None
        }

    # 🚀 NOVÉ: Transport celej True-Margin matice na Frontend
    prace_map = {}
    for p in typy_prac:
        prace_map[str(p.id)] = {
            'nazov': p.nazov,
            'sposob_uctovania': p.sposob_uctovania,
            'hodiny': float(p.normohodiny) if p.normohodiny else 1.0,
            'hod_predaj': float(p.hodinova_sadzba_predaj) if p.hodinova_sadzba_predaj is not None else 0.0,
            'hod_naklad': float(p.hodinova_sadzba_naklad) if p.hodinova_sadzba_naklad is not None else 0.0,
            'fix_predaj': float(p.fixna_cena_predaj) if p.fixna_cena_predaj is not None else 0.0,
            'fix_naklad': float(p.fixny_naklad) if p.fixny_naklad is not None else 0.0
        }

    technici_map = {}
    for t in technici:
        naklad = t.mzda_zivnostnik_hodina if t.typ_pomeru == 'EX' else t.interny_hodinovy_naklad
        technici_map[str(t.id)] = float(naklad) if naklad else 0.0

    # 🏭 1. Vytiahnutie adresy Skladu / Prevádzky z Firemnej Identity
    identita = FiremnaIdentita.objects.first()
    adresa_centraly = ""
    if identita:
        if getattr(identita, 'adresa_prevadzky', None):
            adresa_centraly = identita.adresa_prevadzky.strip()
        else:
            # Fallback z fakturačných údajov, ak chýba prevádzka
            parts_hq = []
            if getattr(identita, 'sidlo_ulica', None): parts_hq.append(f"{identita.sidlo_ulica} {getattr(identita, 'sidlo_cislo', '')}".strip())
            mesto_psc_hq = f"{getattr(identita, 'sidlo_psc', '')} {getattr(identita, 'sidlo_mesto', '')}".strip()
            if mesto_psc_hq: parts_hq.append(mesto_psc_hq)
            if getattr(identita, 'sidlo_stat', None): parts_hq.append(str(identita.sidlo_stat).strip())
            if parts_hq: adresa_centraly = ", ".join(parts_hq)

    # 🏪 2. Vytiahnutie aktívnych pobočiek (Model Filialka využíva TextField 'adresa')
    aktivne_filialky = Filialka.objects.filter(je_aktivna=True).order_by('nazov')

    # 👨‍🔧 3. Inteligentné mapovanie adries technikov (Z domova)
    technici_adresy_map = {}
    for t in technici:
        parts_tech = []
        if t.ulica_a_cislo: parts_tech.append(t.ulica_a_cislo.strip())
        
        mesto_psc = f"{t.psc or ''} {t.mesto or ''}".strip()
        if mesto_psc: parts_tech.append(mesto_psc)
        
        technici_adresy_map[str(t.id)] = ", ".join(parts_tech) if parts_tech else ""

    dph_predvolena_val = float(nastavenia.zakladna_sadzba_dph) if nastavenia and nastavenia.zakladna_sadzba_dph is not None else float(GlobalneNastavenia.get_zakladna_dph())
    mena_val = (getattr(existing_ponuka, 'mena', '') or '').strip() or get_firemna_mena()
    vyjazd_label = get_industry_term('vyjazd_label', 'Servisný výjazd')
    vyjazd_jednotka = get_industry_term('vyjazd_jednotka', 'výjazd')

    builder_data = {
        'category_map': js_category_map,
        'category_emojis': js_category_emojis,
        'vozidla_map': vozidla_map,
        'prace_map': prace_map,
        'technici_map': technici_map,
        'technici_adresy': technici_adresy_map,
        'existing_items': items_list,
        'metadata': metadata_dict,
        'dph_predvolena': dph_predvolena_val,
        'prenos_dph': bool(existing_ponuka.prenos_danovej_povinnosti) if existing_ponuka else False,
        'globalna_zlava': float(existing_ponuka.globalna_zlava_percento or 0.0) if existing_ponuka else 0.0,
        'ponuka_id': str(existing_ponuka.id) if existing_ponuka else '',
        'mena': mena_val,
        'sadzba_praca': float(sadzba_praca),
        'sadzba_doprava': float(sadzba_doprava_pausal),
        'sadzba_doprava_pausal': float(sadzba_doprava_pausal),
        'pohotovostny_priplatok': float(pohotovostny_priplatok),
        'naklad_praca': float(priemerna_mzda_naklad),
        'vyjazd_label': vyjazd_label,
        'vyjazd_jednotka': vyjazd_jednotka,
        'back_url': back_url,
    }

    context = {
        'zakaznici': zakaznici,
        'typy_prac': typy_prac,
        'vozidla': vozidla,
        'technici': technici,
        'aktivne_filialky': aktivne_filialky,
        'adresa_centraly': adresa_centraly,
        'existing_ponuka': existing_ponuka,
        'mena': (getattr(existing_ponuka, 'mena', '') or '').strip() or get_firemna_mena(),
        'val_mena': (getattr(existing_ponuka, 'mena', '') or '').strip() or get_firemna_mena(),
    
        'zakaznik_id': int(zakaznik_id) if zakaznik_id and str(zakaznik_id).isdigit() else None,
        'zariadenie_id': int(zariadenie_id) if zariadenie_id and str(zariadenie_id).isdigit() else None,
        'zariadenia_zakaznika': zariadenia_zakaznika,
        'dph_predvolena': float(nastavenia.zakladna_sadzba_dph) if nastavenia and nastavenia.zakladna_sadzba_dph is not None else float(GlobalneNastavenia.get_zakladna_dph()),

        'sadzba_praca': str(sadzba_praca),
        'sadzba_doprava': str(sadzba_doprava_pausal), 
        'sadzba_doprava_pausal': str(sadzba_doprava_pausal),
        'pohotovostny_priplatok': str(pohotovostny_priplatok),
        'priemerny_naklad_technik': str(priemerna_mzda_naklad),
        
        'builder_data': builder_data,
        'back_url': back_url,
    }
    
    return render(request, 'admin/cenova_ponuka_builder.html', context)


@staff_member_required
def api_cena_dielu(request):
    """Bleskový endpoint pre Konfigurátor: Vráti predajnú aj nákupnú cenu a CT_ID."""
    model_name = request.GET.get('model_name')
    diel_id = request.GET.get('diel_id')
    
    if not model_name or not diel_id or not str(diel_id).isdigit():
        return JsonResponse({'status': 'error', 'message': 'Chýbajúce alebo neplatné parametre.'}, status=400)
    
    try:
        skladove_modely_map = {m.__name__: m for subcats in CATEGORY_MAP.values() for m in subcats.values()}
        ModelClass = skladove_modely_map.get(model_name)
        if not ModelClass:
            return JsonResponse({'status': 'error', 'message': 'Nepovolený prístup k modelu.'}, status=403)
        
        diel = ModelClass.objects.filter(id=diel_id).first()

        if not diel:
            return JsonResponse({'status': 'error', 'message': 'Položka nebola nájdená.'}, status=404)
            
        ct = ContentType.objects.get_for_model(ModelClass)
        
        katalog = CenovyKatalog.objects.filter(content_type=ct, object_id=diel.id).first()
        cena = katalog.predajna_cena if (katalog and katalog.predajna_cena is not None) else decimal.Decimal('0.00')
        nakup = katalog.nakupna_cena if (katalog and katalog.nakupna_cena is not None) else decimal.Decimal('0.00')
        
        return JsonResponse({
            'status': 'success',
            'cena': str(cena),
            'nakupna_cena': str(nakup),
            'ct_id': ct.id
        })
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)})

def verejny_portal_ponuky_view(request, kluc):
    """Zobrazenie ponuky zákazníkovi cez Magic Link."""
    try:
        uuid.UUID(str(kluc))
    except (ValueError, TypeError, AttributeError):
        raise Http404("Cenová ponuka neexistuje.")
        
    PolozkaCenovejPonuky = apps.get_model('Evidencia', 'PolozkaCenovejPonuky')
    ponuka = get_object_or_404(
        CenovaPonuka.objects.select_related(
            'zakaznik', 'vystavil', 'vystavil__zamestnanec', 'zariadenie'
        ).prefetch_related(
            Prefetch('polozky', queryset=PolozkaCenovejPonuky.objects.select_related('zariadenie', 'content_type')),
            'typ_servisu'
        ), 
        bezpecnostny_kluc=kluc
    )
    identita = FiremnaIdentita.objects.first()

    polozky_ponuky = list(ponuka.polozky.all())
    for p in polozky_ponuky:
        p.kategoria_nazov, p.vyrobca_nazov = ISASProductMetadataResolver.resolve(p)

    # 1. Získanie IP adresy (zobrazíme ju klientovi pre pocit bezpečia/záväznosti)
    ip = get_client_ip(request)

    dph_popis = "(Neplatca DPH)" if not getattr(identita, 'je_platca_dph', False) else ("(Prenos daň. pov.)" if ponuka.prenos_danovej_povinnosti else "s DPH")
    js_data = {
        'celkom_s_dph': f"{ponuka.celkom_s_dph:.2f}",
        'mena': getattr(ponuka, 'mena', '') or get_firemna_mena(),
        'dph_popis': dph_popis,
        'klient_ip': ip,
        'schvalit_url': f"/evidencia/api/ponuka/{ponuka.bezpecnostny_kluc}/schvalit/",
    }

    context = {
        'ponuka': ponuka,
        'identita': identita,
        'polozky': polozky_ponuky,
        'klient_ip': ip,
        'terms': get_all_industry_terms(),
        'js_data': js_data,
    }

    return render(request, 'klientsky_portal/schvalenie_ponuky.html', context)

@csrf_exempt
def api_schvalit_ponuku_klientom(request, kluc):
    """AJAX endpoint: Spracuje tlačidlo 'Schvaľujem' od klienta."""
    if request.method != "POST":
        return JsonResponse({'status': 'invalid'}, status=405)
        
    try:
        uuid.UUID(str(kluc))
    except (ValueError, TypeError, AttributeError):
        return JsonResponse({'status': 'error', 'message': 'Neplatný bezpečnostný kľúč ponuky.'}, status=400)
        
    try:
        data = json.loads(request.body)
        if not isinstance(data, dict):
            return JsonResponse({'status': 'error', 'message': 'Neplatný dátový formát požiadavky.'}, status=400)
        meno_podpisu = str(data.get('meno') or '').strip()
        
        if not meno_podpisu:
            return JsonResponse({'status': 'error', 'message': '✍️ Prosím, zadajte svoje meno pre právnu platnosť.'})

        with transaction.atomic():
            # Uzamknutie riadku proti paralelnému schváleniu
            ponuka = get_object_or_404(CenovaPonuka.objects.select_for_update(), bezpecnostny_kluc=kluc)
            
            if ponuka.stav in ['ACCEPTED', 'CONVERTED']:
                return JsonResponse({'status': 'error', 'message': '✅ Táto ponuka už bola v minulosti schválená.'})
                
            ponuka.stav = 'ACCEPTED'
            ponuka.cas_schvalenia = timezone.now()
            ponuka.meno_schvalitela = meno_podpisu
            ponuka.ip_adresa_schvalitela = get_client_ip(request)
            ponuka.save()

            def _safe_notifikuj_schvalenie(p_id):
                try:
                    task_notifikuj_schvalenie_ponuky.delay(p_id)
                except Exception as exc:
                    logger.warning(f"Zlyhalo zaradenie task_notifikuj_schvalenie_ponuky do Celery: {exc}")

            transaction.on_commit(lambda p_id=ponuka.id: _safe_notifikuj_schvalenie(p_id))

        return JsonResponse({'status': 'success', 'message': '🎉 Ponuka bola úspešne schválená a zaznamenaná.'})
    except Exception:
        logger.exception("Chyba pri schvaľovaní ponuky klientom")
        return JsonResponse({'status': 'error', 'message': 'Ponuku sa nepodarilo schváliť. Skontrolujte zadané údaje.'}, status=400)

@csrf_exempt
def schvalit_vstup_sbs_view(request, kluc):
    """
    ISAS ENTRY PORTAL: Webové rozhranie pre vrátnicu (SBS).
    Zobrazuje detaily osôb a áut, poskytuje PDF a rieši schvaľovanie cez AJAX.
    """
    try:
        uuid.UUID(str(kluc))
    except (ValueError, TypeError, AttributeError):
        raise Http404("Povolenie na vstup neexistuje.")

    povolenie = get_object_or_404(
        PovolenieNaVstup.objects.select_related(
            'zakaznik', 'uloha'
        ).prefetch_related(
            'vozidla', 'zamestnanci', 'uloha__typ_servisu', 'uloha__zariadenia'
        ), 
        bezpecnostny_kluc=kluc
    )

    
    # 1. AK IDE O SCHVÁLENIE ALEBO ZAMIETNUTIE (AJAX POST REQUEST z portálu)
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            if not isinstance(data, dict):
                return JsonResponse({'status': 'error', 'message': 'Neplatný dátový formát požiadavky.'}, status=400)
            akcia = data.get('action')
            poznamka = str(data.get('poznamka', '')).strip()
        except (json.JSONDecodeError, AttributeError, ValueError):
            return JsonResponse({'status': 'error', 'message': 'Poškodené JSON telo požiadavky.'}, status=400)

        with transaction.atomic():
            # 🛡️ DEFENSIVE: of=('self',) zamyká výhradne riadok povolenia a chráni Postgres pred outer join chybou
            povolenie_locked = PovolenieNaVstup.objects.select_for_update(of=('self',)).select_related('zakaznik').prefetch_related('vozidla', 'zamestnanci').get(id=povolenie.id)

            # Ak už bolo rozhodnuté, nedovolíme žiadnu zmenu!
            if povolenie_locked.stav in ['SCHVALENE', 'ZAMIETNUTE']:
                return JsonResponse({'status': 'error', 'message': '🛑 Rozhodnutie už bolo v minulosti zaznamenané. Z bezpečnostných dôvodov ho nie je možné zmeniť.'})
                
            raw_ip = str(get_client_ip(request) or '').strip()
            # 🛡️ DEFENSIVE: Extrakcia prvej klientskej IP z proxy reťazca a orezanie na dĺžku poľa (max 50)
            client_ip = raw_ip.split(',')[0].strip()[:50]

            povolenie_locked.cas_schvalenia_sbs = timezone.now()
            povolenie_locked.ip_schvalitela_sbs = client_ip
            povolenie_locked.poznamka_sbs = poznamka
            
            # 🎯 ROZHODOVACÍ STROM (MOZOG) - Striktná validácia akcie
            if akcia == 'zamietnut':
                povolenie_locked.stav = 'ZAMIETNUTE'
                povolenie_locked.save(update_fields=['cas_schvalenia_sbs', 'ip_schvalitela_sbs', 'poznamka_sbs', 'stav'])
                msg_response = '🔴 Vstup bol zamietnutý.'
            elif akcia == 'schvalit':
                povolenie_locked.stav = 'SCHVALENE'
                povolenie_locked.save(update_fields=['cas_schvalenia_sbs', 'ip_schvalitela_sbs', 'poznamka_sbs', 'stav'])
                msg_response = '🟢 Vstup bol úspešne povolený.'
            else:
                return JsonResponse({'status': 'error', 'message': '🛑 Neplatná bezpečnostná akcia.'}, status=400)

            def _safe_odosli_sbs(p_id, act):
                try:
                    odosli_sbs_notifikacie_task.delay(p_id, act)
                except Exception as exc:
                    logger.warning(f"Zlyhalo zaradenie odosli_sbs_notifikacie_task do Celery: {exc}")

            transaction.on_commit(lambda p_id=povolenie_locked.id, act=akcia: _safe_odosli_sbs(p_id, act))

            # ÚSPEŠNÝ NÁVRAT PRE JAVASCRIPT
            return JsonResponse({'status': 'success', 'message': msg_response})

    # 2. AK ČLOVEK (ALEBO EMAIL SCANNER) IBA OTVORIL ODKAZ (GET REQUEST)
    identita = FiremnaIdentita.objects.first()

    # Normálne vykreslíme krásny HTML Portál
    context = {
        'povolenie': povolenie,
        'identita': identita,
        'klient_ip': get_client_ip(request)
    }
    return render(request, 'klientsky_portal/portal_vstupu.html', context)

@staff_member_required
def faktura_builder_nova_view(request):
    """Smerovač pre úplne novú faktúru (Presmerovanie z tlačidla Pridať)."""
    return _faktura_builder_core(request, faktura_id=None)

@staff_member_required
def faktura_builder_edit_view(request, faktura_id):
    """Smerovač pre úpravu už existujúceho DRAFTu."""
    return _faktura_builder_core(request, faktura_id=faktura_id)

def _faktura_builder_core(request, faktura_id=None):
    """
    Spoločné jadro pre vykreslenie Buildera Faktúry.
    Zachováva 100% kontextu z Cenovej ponuky (pre kalkulačku), ale pridáva importy.
    """

    faktura = None
    existujuce_polozky = []
    metadata_dict = {}
    
    if faktura_id:
        faktura = get_object_or_404(VystavenaFaktura, id=faktura_id)
        if faktura.stav != 'DRAFT':
            messages.error(request, "Táto faktúra je už uzamknutá a nedá sa upravovať v Builderi.")
            return redirect('admin:Evidencia_vystavenafaktura_changelist')
            
        # Dešifrovanie a extrakcia UI konfigurácie z poľa ai_poznamka (HACK pre Faktúru)
        if getattr(faktura, 'ai_poznamka', None) and faktura.ai_poznamka.startswith("[ISAS_METADATA_JSON]"):
            try:
                metadata_raw = faktura.ai_poznamka.replace("[ISAS_METADATA_JSON]", "", 1).split('\n')[0].strip()
                parsed_meta = json.loads(metadata_raw)
                if isinstance(parsed_meta, dict):
                    metadata_dict = parsed_meta
            except Exception:
                metadata_dict = {}
                
        zdroje_raw = metadata_dict.get('zdroje_poloziek')
        zdroje_zoznam = zdroje_raw if isinstance(zdroje_raw, list) else []
        
        povodna_zlava_raw = metadata_dict.get('povodna_zlava_poloziek')
        povodna_zlava_zoznam = povodna_zlava_raw if isinstance(povodna_zlava_raw, list) else []
            
        # Pripravíme položky pre JS (Obnova čistej riadkovej zľavy pred aplikáciou globálnej)
        for idx, p in enumerate(faktura.polozky.select_related('zariadenie').all()):
            cista_riadkova_zlava = povodna_zlava_zoznam[idx] if idx < len(povodna_zlava_zoznam) else str(p.zlava_percento)
            zdroj_polozky = zdroje_zoznam[idx] if (idx < len(zdroje_zoznam) and isinstance(zdroje_zoznam[idx], dict)) else {}
            existujuce_polozky.append({
                'typ': p.typ_polozky,
                'nazov': p.nazov,
                'mnozstvo': str(p.mnozstvo),
                'jednotka': p.merna_jednotka,
                'cena_bez_dph': str(p.jednotkova_cena_bez_dph),
                'zlava_percento': str(cista_riadkova_zlava),
                'sadzba_dph': str(p.sadzba_dph),
                # Priame čítanie ID stĺpca bez N+1 SQL dotazov
                'ct_id': str(p.content_type_id) if p.content_type_id else '',
                'obj_id': str(p.object_id) if p.object_id else '',
                'praca_naviac': p.praca_naviac,
                'zariadenie_id': str(p.zariadenie_id) if p.zariadenie_id else '',
                'zariadenie_nazov': f"{p.zariadenie.model_zariadenia} (S/N: {p.zariadenie.vyrobne_cislo})" if p.zariadenie else '',
                'zdroje': zdroj_polozky # Obnova pamäte
            })

    # --- PÔVODNÝ KONTEXT PRE KALKULAČKU (ZACHOVANÉ 1:1) ---
    zakaznici = Zakaznik.objects.all().order_by('Nazov_spolocnosti')
    typy_prac = TypServisuPrace.objects.all().order_by('nazov')
    vozidla = Vozidlo.objects.all().order_by('znacka_a_model')
    technici = Zamestnanec.objects.filter(pracovna_rola=Zamestnanec.TECHNIK).order_by('meno', 'priezvisko')
    nastavenia = GlobalneNastavenia.objects.first()
    
    sadzba_praca = getattr(nastavenia, 'hodinova_sadzba_technik', None) if nastavenia and getattr(nastavenia, 'hodinova_sadzba_technik', None) is not None else decimal.Decimal('0.00')
    sadzba_doprava_pausal = getattr(nastavenia, 'cena_za_vyjazd', None) if nastavenia and getattr(nastavenia, 'cena_za_vyjazd', None) is not None else decimal.Decimal('0.00')
    pohotovostny_priplatok = getattr(nastavenia, 'pohotovostny_priplatok', None) if nastavenia and getattr(nastavenia, 'pohotovostny_priplatok', None) is not None else decimal.Decimal('0.00')

    priemerna_mzda_naklad = decimal.Decimal('0.00')
    if technici.exists():
        total_mzda = sum(
            [(t.mzda_zivnostnik_hodina if t.typ_pomeru == 'EX' else t.interny_hodinovy_naklad) or decimal.Decimal('0.00') for t in technici],
            decimal.Decimal('0.00')
        )
        pocet_t = technici.count()
        if pocet_t > 0:
            priemerna_mzda_naklad = (total_mzda / decimal.Decimal(str(pocet_t))).quantize(decimal.Decimal('0.01'))

    vozidla_map = {str(v.id): {'nazov': v.znacka_a_model, 'sadzba': float(v.vlastna_sadzba_za_km) if v.vlastna_sadzba_za_km else None} for v in vozidla}
    prace_map = {
        str(p.id): {
            'nazov': p.nazov,
            'sposob_uctovania': p.sposob_uctovania,
            'hodiny': float(p.normohodiny or 1.0),
            'hod_predaj': float(p.hodinova_sadzba_predaj) if p.hodinova_sadzba_predaj is not None else 0.0,
            'hod_naklad': float(p.hodinova_sadzba_naklad) if p.hodinova_sadzba_naklad is not None else 0.0,
            'fix_predaj': float(p.fixna_cena_predaj) if p.fixna_cena_predaj is not None else 0.0,
            'fix_naklad': float(p.fixny_naklad) if p.fixny_naklad is not None else 0.0
        } for p in typy_prac
    }
    technici_map = {str(t.id): float((t.mzda_zivnostnik_hodina if t.typ_pomeru == 'EX' else t.interny_hodinovy_naklad) or 0.0) for t in technici}

    identita = FiremnaIdentita.objects.first()
    adresa_centraly = ""
    if identita:
        if getattr(identita, 'adresa_prevadzky', None):
            adresa_centraly = identita.adresa_prevadzky.strip()
        else:
            parts_hq = []
            if getattr(identita, 'sidlo_ulica', None): parts_hq.append(f"{identita.sidlo_ulica} {getattr(identita, 'sidlo_cislo', '')}".strip())
            mesto_psc_hq = f"{getattr(identita, 'sidlo_psc', '')} {getattr(identita, 'sidlo_mesto', '')}".strip()
            if mesto_psc_hq: parts_hq.append(mesto_psc_hq)
            if getattr(identita, 'sidlo_stat', None): parts_hq.append(str(identita.sidlo_stat).strip())
            if parts_hq: adresa_centraly = ", ".join(parts_hq)

    aktivne_filialky = Filialka.objects.filter(je_aktivna=True).order_by('nazov')
    technici_adresy_map = {str(t.id): f"{t.ulica_a_cislo or ''} {t.psc or ''} {t.mesto or ''}".strip() for t in technici}

    js_category_map = {}
    for main_cat, subcats in CATEGORY_MAP.items():
        js_category_map[main_cat] = {name: model.__name__ for name, model in subcats.items()}
    js_category_emojis = {k: v for k, v in CATEGORY_CHOICES}

    # --- NOVÉ PRE FAKTÚRU: ZDROJE PRE IMPORT ---
    dostupne_ponuky = CenovaPonuka.objects.filter(stav__in=['SENT', 'ACCEPTED', 'CONVERTED']).order_by('-id')[:100]
    
    # Filtrovanie úloh a záznamov pre VŠETKY zariadenia vo faktúre (Hlavička + Položky)
    zariadenia_ids = set()
    if faktura and faktura.zariadenie_id:
        zariadenia_ids.add(faktura.zariadenie_id)
    if faktura:
        # Vytiahneme aj všetky zariadenia priradené k jednotlivým riadkom
        zariadenia_ids.update(faktura.polozky.exclude(zariadenie_id__isnull=True).values_list('zariadenie_id', flat=True))
    
    if zariadenia_ids:
        # Úloha má ManyToMany pole 'zariadenia', Záznam má ForeignKey 'zariadenie'
        dostupne_ulohy = Uloha.objects.filter(zariadenia__id__in=zariadenia_ids).distinct().order_by('-id')[:100]
        dostupne_zaznamy = ServisnyZaznam.objects.filter(zariadenie_id__in=zariadenia_ids).order_by('-id')[:100]
    elif faktura and faktura.zakaznik_id:
        # Fallback: Ak zariadenie nie je vybraté, ukážeme aspoň doklady daného zákazníka
        dostupne_ulohy = Uloha.objects.filter(zakaznik_id=faktura.zakaznik_id).order_by('-id')[:100]
        dostupne_zaznamy = ServisnyZaznam.objects.filter(zakaznik_id=faktura.zakaznik_id).order_by('-id')[:100]
    else:
        # Ak nie je ani zákazník, neukážeme nič (ochrana pred načítaním celej DB)
        dostupne_ulohy = Uloha.objects.none()
        dostupne_zaznamy = ServisnyZaznam.objects.none()

    # Pridanie Prijatých objednávok a mapovania pre JS mágiu (M:N Zberné objednávky)
    dostupne_objednavky = PrijataObjednavka.objects.filter(stav__in=['NOVA', 'SCHVALENA', 'VYBAVENA']).prefetch_related('zdrojove_ponuky').order_by('-id')[:100]

    # Načítanie hotových faktúr pre tvorbu Dobropisov
    dostupne_faktury = VystavenaFaktura.objects.exclude(stav='DRAFT').order_by('-id')[:150]
    
    # Vytvoríme slovníky pre JS: { "id_objednavky": ["id_ponuky1", "id_ponuky2"] } a naopak
    mapa_ponuka_objednavka = {}
    mapa_objednavka_ponuka = {}
    
    for o in dostupne_objednavky:
        ponuky_ids = list(o.zdrojove_ponuky.values_list('id', flat=True))
        if ponuky_ids:
            mapa_objednavka_ponuka[str(o.id)] = [str(pid) for pid in ponuky_ids]
            for pid in ponuky_ids:
                if str(pid) not in mapa_ponuka_objednavka:
                    mapa_ponuka_objednavka[str(pid)] = []
                mapa_ponuka_objednavka[str(pid)].append(str(o.id))

    hlavny_sklad_obj = Sklad.objects.first()
    nazov_hlavneho_skladu = f"Hlavný sklad ({hlavny_sklad_obj.nazov})" if hlavny_sklad_obj and hlavny_sklad_obj.nazov and hlavny_sklad_obj.nazov.lower() != "hlavný sklad" else "Hlavný sklad"

    # 🗓️ Výpočet bezpečných predvolených dátumov a hodnôt
    dnes = timezone.now().date()
    splatnost_dni = getattr(identita, 'splatnost_faktur', 14) or 14
    try:
        splatnost_dni = int(splatnost_dni)
    except (ValueError, TypeError):
        splatnost_dni = 14
    predvolena_splatnost = dnes + timedelta(days=splatnost_dni)

    datum_vystavenia_val = (faktura.datum_vystavenia.strftime('%Y-%m-%d') if (faktura and faktura.datum_vystavenia) else dnes.strftime('%Y-%m-%d'))
    datum_dodania_val = (faktura.datum_dodania.strftime('%Y-%m-%d') if (faktura and faktura.datum_dodania) else dnes.strftime('%Y-%m-%d'))
    datum_splatnosti_val = (faktura.datum_splatnosti.strftime('%Y-%m-%d') if (faktura and faktura.datum_splatnosti) else predvolena_splatnost.strftime('%Y-%m-%d'))
    sposob_uhrady_val = faktura.sposob_uhrady if faktura else 'PREVOD'
    mena_val = (faktura.mena.strip() if (faktura and faktura.mena) else '') or get_firemna_mena()
    poznamka_pre_klienta_val = faktura.poznamka_pre_klienta if faktura else ''

    dph_predvolena_val = float(nastavenia.zakladna_sadzba_dph) if nastavenia and nastavenia.zakladna_sadzba_dph is not None else float(GlobalneNastavenia.get_zakladna_dph())
    try:
        globalna_zlava_val = float(str(metadata_dict.get('globalna_zlava_percento') or '0.0').replace(',', '.'))
    except (ValueError, TypeError):
        globalna_zlava_val = 0.0
    back_url_val = reverse('admin:Evidencia_vystavenafaktura_changelist')
    vyjazd_label = get_industry_term('vyjazd_label', 'Servisný výjazd')
    vyjazd_jednotka = get_industry_term('vyjazd_jednotka', 'výjazd')

    ulozene_prace = [str(tp.id) for tp in faktura.typ_servisu.all()] if faktura and hasattr(faktura, 'typ_servisu') else []
    builder_data = {
        'category_map': js_category_map,
        'category_emojis': js_category_emojis,
        'vozidla_map': vozidla_map,
        'prace_map': prace_map,
        'technici_map': technici_map,
        'technici_adresy': technici_adresy_map,
        'mapa_ponuka_objednavka': mapa_ponuka_objednavka,
        'mapa_objednavka_ponuka': mapa_objednavka_ponuka,
        'existujuce_polozky': existujuce_polozky,
        'metadata': metadata_dict,
        'ulozene_prace': ulozene_prace,
        'dph_predvolena': dph_predvolena_val,
        'prenos_dph': bool(faktura.prenos_danovej_povinnosti) if faktura else False,
        'globalna_zlava': globalna_zlava_val,
        'mena': mena_val,
        'faktura_id': str(faktura.id) if faktura else '',
        'sadzba_praca': float(sadzba_praca),
        'sadzba_doprava': float(sadzba_doprava_pausal),
        'sadzba_doprava_pausal': float(sadzba_doprava_pausal),
        'pohotovostny_priplatok': float(pohotovostny_priplatok),
        'naklad_praca': float(priemerna_mzda_naklad),
        'zdrojovy_sklad': str(getattr(faktura, 'zdrojovy_sklad_id', '') or '') if faktura else '',
        'splatnost_dni': splatnost_dni,
        'vyjazd_label': vyjazd_label,
        'vyjazd_jednotka': vyjazd_jednotka,
        'back_url': back_url_val,
    }

    context = {
        'faktura': faktura,
        'faktura_id': faktura.id if faktura else '',
        'sposob_uhrady_choices': VystavenaFaktura.SPOSOB_UHRADY_CHOICES,
        'val_sposob_uhrady': sposob_uhrady_val,
        'val_mena': mena_val,
        'val_datum_vystavenia': datum_vystavenia_val,
        'val_datum_dodania': datum_dodania_val,
        'val_datum_splatnosti': datum_splatnosti_val,
        'val_poznamka_pre_klienta': poznamka_pre_klienta_val,
        
        'zakaznik_id': faktura.zakaznik_id if faktura else None,
        'zariadenie_id': getattr(faktura, 'zariadenie_id', None) if faktura else None,
        'zariadenia_zakaznika': ServisovaneZariadenie.objects.filter(zakaznik_id=faktura.zakaznik_id) if faktura else [],
        
        'zakaznici': zakaznici, 'typy_prac': typy_prac, 'vozidla': vozidla, 'technici': technici,
        'aktivne_filialky': aktivne_filialky, 'adresa_centraly': adresa_centraly,
        'dph_predvolena': float(nastavenia.zakladna_sadzba_dph) if nastavenia and nastavenia.zakladna_sadzba_dph is not None else float(GlobalneNastavenia.get_zakladna_dph()),

        'sadzba_praca': str(sadzba_praca), 'sadzba_doprava': str(sadzba_doprava_pausal), 'sadzba_doprava_pausal': str(sadzba_doprava_pausal),
        'pohotovostny_priplatok': str(pohotovostny_priplatok), 'priemerny_naklad_technik': str(priemerna_mzda_naklad),
        
        'builder_data': builder_data,
        'back_url': reverse('admin:Evidencia_vystavenafaktura_changelist'),
        
        # Import dát
        'dostupne_ponuky': dostupne_ponuky,
        'dostupne_ulohy': dostupne_ulohy,
        'dostupne_zaznamy': dostupne_zaznamy,
        'dostupne_objednavky': dostupne_objednavky,
        'dostupne_faktury': dostupne_faktury,
        'nazov_hlavneho_skladu': nazov_hlavneho_skladu,
    }
    return render(request, 'admin/faktura_builder.html', context)

@staff_member_required
def api_ulozit_fakturu_builder(request):
    """
    Prijme JSON z Faktúra Buildera.
    ULOŽÍ IBA DÁTA DO DATABÁZY. Nastaví stav na DRAFT.
    (Nerobí PDF, neposiela maily, neprideľuje ostré číslo).
    """
    if request.method != "POST":
        return JsonResponse({'status': 'invalid'}, status=405)

    try:
        data = json.loads(request.body)
        if not isinstance(data, dict):
            return JsonResponse({'status': 'error', 'message': 'Neplatný dátový formát požiadavky (očakáva sa JSON objekt).'}, status=400)
        faktura_id = data.get('faktura_id')
        zakaznik_id = data.get('zakaznik_id')
        akcia = data.get('akcia_faktury') or ('draft' if data.get('as_draft', True) else 'odoslat')
        as_draft = (akcia == 'draft')
        
        if not zakaznik_id or not str(zakaznik_id).isdigit():
            return JsonResponse({'status': 'error', 'message': 'Zákazník je povinný.'}, status=400)
            
        zakaznik = Zakaznik.objects.filter(id=zakaznik_id).first()
        if not zakaznik:
            return JsonResponse({'status': 'error', 'message': 'Zákazník nebol nájdený.'}, status=404)
        polozky_data = data.get('polozky', [])

        with transaction.atomic():
            # 1. ZÍSKANIE ALEBO VYTVORENIE FAKTÚRY
            if faktura_id and str(faktura_id).isdigit():
                # Uzamknutie faktúry počas prepisovania položiek
                faktura = VystavenaFaktura.objects.select_for_update().filter(id=faktura_id).first()
                if not faktura:
                    return JsonResponse({'status': 'error', 'message': 'Faktúra nebola nájdená.'}, status=404)
                if faktura.stav != 'DRAFT':
                    return JsonResponse({'status': 'error', 'message': 'Faktúru mimo DRAFTu už nie je možné upravovať.'}, status=400)
                # Vymažeme staré položky, lebo ideme vložiť nové z Buildera
                faktura.polozky.all().delete()
                # Stav nastavíme na DRAFT. Ak to nie je draft, zmení sa to neskôr cez schvalit_a_vystavit()
                faktura.stav = 'DRAFT'
            else:
                faktura = VystavenaFaktura(stav='DRAFT')

            # 2. NAPLNENIE ZÁKLADNÝCH ÚDAJOV
            faktura.zakaznik = zakaznik
            
            faktura.vystavil = request.user if request.user.is_authenticated else None
            
            # Fyzické uloženie zariadenia do DB s IDOR kontrolou voči zákazníkovi
            zar_id_input = data.get('zariadenie_id')
            if zar_id_input and str(zar_id_input).isdigit():
                faktura.zariadenie = ServisovaneZariadenie.objects.filter(id=zar_id_input, zakaznik=zakaznik).first()
            else:
                faktura.zariadenie = None
                
            # 🛡️ ISAS ARCHITECT FIX: Bezpečné načítanie a ohraničenie globálnej zľavy (0 až 100 %)
            zlava_fa_raw = str(data.get('globalna_zlava_percento') or '0.00').replace(',', '.').strip()
            try:
                globalna_zlava = decimal.Decimal(zlava_fa_raw)
            except (decimal.InvalidOperation, TypeError):
                globalna_zlava = decimal.Decimal('0.00')
            globalna_zlava = max(decimal.Decimal('0.00'), min(decimal.Decimal('100.00'), globalna_zlava))

            ciste_polozky_data = [p for p in polozky_data if isinstance(p, dict)] if isinstance(polozky_data, list) else []

            # Serializácia meta-údajov s uchovaním PÔVODNÝCH riadkových zliav (ochrana pred zacykleným zľavňovaním)
            metadata = {
                'technici_ids': data.get('technici_ids', []) if isinstance(data.get('technici_ids'), list) else [],
                'strategia_prace': data.get('strategia_prace', 'hodinovka'),
                'vozidlo_id': data.get('vozidlo_id', ''),
                'start_trasy': data.get('start_trasy', 'HQ_CENTRAL'),
                'strategia_dopravy': data.get('strategia_dopravy', 'kalkulacka'),
                'havarijny_rezim': bool(data.get('havarijny_rezim', False)),
                'zdrojove_ponuky_ids': data.get('zdrojove_ponuky_ids', []) if isinstance(data.get('zdrojove_ponuky_ids'), list) else [],
                'zdrojove_objednavky_ids': data.get('zdrojove_objednavky_ids', []) if isinstance(data.get('zdrojove_objednavky_ids'), list) else [],
                'zdrojova_uloha_id': data.get('zdrojova_uloha_id', ''),
                'zdrojove_zaznamy_ids': data.get('zdrojove_zaznamy_ids', []) if isinstance(data.get('zdrojove_zaznamy_ids'), list) else [],
                'globalna_zlava_percento': str(globalna_zlava),
                'zdroje_poloziek': [p.get('zdroje', {}) if isinstance(p.get('zdroje'), dict) else {} for p in ciste_polozky_data],
                'povodna_zlava_poloziek': [str(p.get('zlava_percento') or '0.00').replace(',', '.').strip() for p in ciste_polozky_data]
            }

            # VystavenaFaktura nemá interne_poznamky, použijeme existujúce pole ai_poznamka
            faktura.ai_poznamka = "[ISAS_METADATA_JSON]" + json.dumps(metadata)
                
            # Spracovanie prenosu daňovej povinnosti a voľby kódu DPH
            prenos_dph = data.get('prenos_danovej_povinnosti', False)
            faktura.prenos_danovej_povinnosti = prenos_dph
            kod_dph = data.get('kod_clenenia_dph')
            if kod_dph:
                faktura.kod_clenenia_dph = kod_dph
            elif not faktura.kod_clenenia_dph or faktura.kod_clenenia_dph in ['VAT_OUT', 'VAT_REVERSE']:
                faktura.kod_clenenia_dph = 'VAT_REVERSE' if prenos_dph else 'VAT_OUT'

            sklad_id_input = str(data.get('zdrojovy_sklad_id') or '').strip()
            faktura.zdrojovy_sklad_id = sklad_id_input if sklad_id_input else None
            faktura.odpisat_zo_skladu = bool(data.get('odpisat_zo_skladu', False))
            faktura.notif_odpis_tg = data.get('notif_odpis_tg', True)
            faktura.notif_odpis_mail = data.get('notif_odpis_mail', True)
            faktura.notif_odpis_discord = data.get('notif_odpis_discord', True)
            faktura.notif_majitel_tg = data.get('notif_majitel_tg', True)
            faktura.notif_majitel_mail = data.get('notif_majitel_mail', True)
            faktura.notif_majitel_discord = data.get('notif_majitel_discord', True)
            
            # Analytické prepojenia s IDOR ochranou voči zákazníkovi dokladu
            uloha_id_input = data.get('zdrojova_uloha_id')
            if uloha_id_input and str(uloha_id_input).isdigit():
                faktura.zdrojova_uloha = Uloha.objects.filter(id=int(uloha_id_input), zakaznik=zakaznik).first()
            else:
                faktura.zdrojova_uloha = None
            
            # Uloženie Typu dokladu a Pôvodnej faktúry
            faktura.typ_dokladu = data.get('typ_dokladu', 'FAKTURA')
            
            povodna_fa_id = data.get('povodna_faktura_id')
            if povodna_fa_id and str(povodna_fa_id).isdigit():
                faktura.povodna_faktura = VystavenaFaktura.objects.filter(id=povodna_fa_id, zakaznik=zakaznik).first()
            else:
                faktura.povodna_faktura = None

            # 💶 Uloženie platobných podmienok, dátumov a poznámky priamo z Buildera
            faktura.sposob_uhrady = data.get('sposob_uhrady') or 'PREVOD'
            faktura.mena = (str(data.get('mena') or '').strip().upper()[:10]) or get_firemna_mena()
            faktura.poznamka_pre_klienta = data.get('poznamka_pre_klienta') or None

            # Bezpečné parsovanie dátumov (ochrana pred neplatnými formátmi)
            def _parse_iso_date(d_val):
                if not d_val:
                    return None
                try:
                    return datetime.strptime(str(d_val).strip(), '%Y-%m-%d').date()
                except (ValueError, TypeError):
                    return None

            datum_vyst = _parse_iso_date(data.get('datum_vystavenia')) or timezone.now().date()
            datum_dod = _parse_iso_date(data.get('datum_dodania')) or timezone.now().date()
            datum_splat = _parse_iso_date(data.get('datum_splatnosti')) or (datum_vyst + timedelta(days=14))

            if datum_splat and datum_splat < datum_vyst:
                return JsonResponse({'status': 'error', 'message': 'Dátum splatnosti nemôže byť skorší ako dátum vystavenia.'}, status=400)

            faktura.datum_vystavenia = datum_vyst
            faktura.datum_dodania = datum_dod
            faktura.datum_splatnosti = datum_splat

            faktura.save() # Najprv uložíme, aby sme mohli priraďovať M2M väzby

            # Spracovanie viacerých Prijatých objednávok s IDOR kontrolou a vytvorenie Snapshotu pre PDF
            obj_ids = data.get('zdrojove_objednavky_ids', [])
            if obj_ids:
                ciste_obj_ids = [int(oid) for oid in obj_ids if str(oid).isdigit()]
                objednavky = PrijataObjednavka.objects.filter(id__in=ciste_obj_ids, zakaznik=zakaznik)
                faktura.zdrojove_objednavky.set(objednavky)
                
                po_cisla = []
                for o in objednavky:
                    # Prioritne berieme PO Number od klienta, ak nie je, tak naše interné
                    po_num = str(o.cislo_objednavky_klienta or o.cislo_objednavky_interne or '').strip()
                    if po_num:
                        po_cisla.append(po_num)
                
                # Spojíme všetky čísla objednávok čiarkou pre PDF
                faktura.cislo_objednavky_klienta = ", ".join(po_cisla) if po_cisla else None
            else:
                faktura.zdrojove_objednavky.clear()
                faktura.cislo_objednavky_klienta = None
                
            faktura.save(update_fields=['cislo_objednavky_klienta'])
            
            # 🚀 ISAS FIX: Uloženie viacerých záznamov a ponúk (Zberná faktúra s IDOR kontrolou)
            zaznamy_ids = data.get('zdrojove_zaznamy_ids', [])
            if zaznamy_ids:
                ciste_zaznamy_ids = [int(zid) for zid in zaznamy_ids if str(zid).isdigit()]
                faktura.zdrojove_zaznamy.set(ServisnyZaznam.objects.filter(id__in=ciste_zaznamy_ids, zakaznik=zakaznik))
            else:
                faktura.zdrojove_zaznamy.clear()
                
            ponuky_ids = data.get('zdrojove_ponuky_ids', [])
            if ponuky_ids:
                ciste_ponuky_ids = [int(pid) for pid in ponuky_ids if str(pid).isdigit()]
                faktura.zdrojove_ponuky.set(CenovaPonuka.objects.filter(id__in=ciste_ponuky_ids, zakaznik=zakaznik))
            else:
                faktura.zdrojove_ponuky.clear()

            # 🚀 ISAS FIX: Fyzické uloženie Typov Práce do DB
            tp_ids = data.get('typ_prace_ids')
            ciste_tp_ids = [int(p) for p in tp_ids if str(p).isdigit()] if isinstance(tp_ids, list) else []
            if ciste_tp_ids:
                faktura.typ_servisu.set(TypServisuPrace.objects.filter(id__in=ciste_tp_ids))
            else:
                faktura.typ_servisu.clear()

            # 3. SPRACOVANIE POLOŽIEK
            sum_dph = decimal.Decimal('0.00')
            nastavenia_fa = GlobalneNastavenia.objects.first()
            predvolena_dph_fa = nastavenia_fa.zakladna_sadzba_dph if nastavenia_fa and nastavenia_fa.zakladna_sadzba_dph is not None else GlobalneNastavenia.get_zakladna_dph()
            zakaznik_zariadenia_ids = set(ServisovaneZariadenie.objects.filter(zakaznik=zakaznik).values_list('id', flat=True))

            for p in (polozky_data if isinstance(polozky_data, list) else []):
                if not isinstance(p, dict):
                    continue
                mnozstvo_str = str(p.get('mnozstvo') or '1.00').replace(',', '.').strip()
                cena_bez_str = str(p.get('cena_bez_dph') or '0.00').replace(',', '.').strip()
                zlava_str = str(p.get('zlava_percento') or '0.00').replace(',', '.').strip()
                p_dph = str(p.get('sadzba_dph') or '').replace(',', '.').strip()
                
                try:
                    mnozstvo = decimal.Decimal(mnozstvo_str) if mnozstvo_str else decimal.Decimal('1.00')
                except decimal.InvalidOperation:
                    mnozstvo = decimal.Decimal('1.00')
                try:
                    cena_bez = decimal.Decimal(cena_bez_str) if cena_bez_str else decimal.Decimal('0.00')
                except decimal.InvalidOperation:
                    cena_bez = decimal.Decimal('0.00')
                try:
                    zlava_riadok_cista = decimal.Decimal(zlava_str) if zlava_str else decimal.Decimal('0.00')
                except decimal.InvalidOperation:
                    zlava_riadok_cista = decimal.Decimal('0.00')
                try:
                    sadzba_dph = decimal.Decimal(p_dph) if p_dph else predvolena_dph_fa
                except decimal.InvalidOperation:
                    sadzba_dph = predvolena_dph_fa
                
                # 🛡️ ISAS ARCHITECT: Výpočet efektívnej zľavy pre DB zápis (Peppol compliance)
                # Zľavu neaplikujeme na fixné zľavové riadky (typ ZLA)
                zlava_riadok_db = zlava_riadok_cista
                if globalna_zlava > decimal.Decimal('0.00') and p.get('typ') != 'ZLA':
                    koef_riadok = decimal.Decimal('1.00') - (zlava_riadok_cista / decimal.Decimal('100.00'))
                    koef_global = decimal.Decimal('1.00') - (globalna_zlava / decimal.Decimal('100.00'))
                    efektivna_zlava = (decimal.Decimal('1.00') - (koef_riadok * koef_global)) * decimal.Decimal('100.00')
                    zlava_riadok_db = efektivna_zlava.quantize(decimal.Decimal('0.01'), rounding=decimal.ROUND_HALF_UP)

                ct_id = p.get('ct_id')
                obj_id = p.get('obj_id')
                
                try:
                    ct = ContentType.objects.get_for_id(int(ct_id)) if ct_id and str(ct_id).isdigit() else None
                except ContentType.DoesNotExist:
                    ct = None

                zar_id_riadok = p.get('zariadenie_id')
                zar_id_valid = int(zar_id_riadok) if (zar_id_riadok and str(zar_id_riadok).isdigit() and int(zar_id_riadok) in zakaznik_zariadenia_ids) else None

                polozka = PolozkaVystavenejFaktury(
                    faktura=faktura,
                    typ_polozky=p.get('typ', 'MAT'),
                    nazov=p.get('nazov', 'Neznáma položka'),
                    mnozstvo=mnozstvo,
                    merna_jednotka=p.get('jednotka', 'ks'),
                    jednotkova_cena_bez_dph=cena_bez,
                    zlava_percento=zlava_riadok_db,
                    sadzba_dph=sadzba_dph,
                    content_type=ct,
                    object_id=obj_id if ct and str(obj_id).isdigit() else None,
                    zariadenie_id=zar_id_valid,
                    praca_naviac=p.get('praca_naviac', False)
                )
                polozka.save()

                sum_dph += (polozka.hodnota_dph or decimal.Decimal('0.00'))

            # 4. FINÁLNA MATEMATIKA HLAVIČKY (Striktný súčet riadkov: Peppol BR-CO-10)
            celkom_bez_dph_po_riadkovych = sum((decimal.Decimal(str(p.cena_po_zlave_bez_dph or '0.00')) for p in faktura.polozky.all()), decimal.Decimal('0.00'))
            
            faktura.celkom_bez_dph = celkom_bez_dph_po_riadkovych.quantize(decimal.Decimal('0.01'), rounding=decimal.ROUND_HALF_UP)
            faktura.celkom_dph = sum_dph.quantize(decimal.Decimal('0.01'), rounding=decimal.ROUND_HALF_UP)
            faktura.celkom_s_dph = faktura.celkom_bez_dph + faktura.celkom_dph
            faktura.save()

            # 🛡️ ISAS ARCHITECT FLOW: Spracovanie podľa zvolenej akcie
            if akcia in ['nachystat', 'odoslat']:
                # schvalit_a_vystavit() pridelí číslo dokladu, zafixuje snapshot, odpíše sklad a nastaví stav 'NACHYSTANA'
                faktura.schvalit_a_vystavit(schvalil_user=request.user if request.user.is_authenticated else None)
                
                if akcia == 'odoslat':
                    faktura.stav = 'ODOSLANA'
                    faktura.save(update_fields=['stav'])

        # 🚀 ASYNCHRÓNNA CELERY OBSLUHA
        from .tasks import vygeneruj_pdf_a_notifikuj_fakturu_task

        def _safe_vygeneruj_fakturu(f_id, url, s_notif):
            try:
                vygeneruj_pdf_a_notifikuj_fakturu_task.delay(f_id, url, send_notif=s_notif)
            except Exception as exc:
                logger.warning(f"Zlyhalo zaradenie vygeneruj_pdf_a_notifikuj_fakturu_task do Celery pre faktúru {f_id}: {exc}")

        if akcia == 'draft':
            success_message = '📝 Koncept faktúry bol úspešne uložený do systému. Doklad nemá číslo a tovar zostáva na sklade.'
        elif akcia == 'nachystat':
            base_url = request.build_absolute_uri('/')[:-1]
            # send_notif=False: Vygeneruje PDF, zmaže staré osirotené súbory, pripraví Peppol XML, ale NEPOSIELA maily ani SAPI-SK!
            transaction.on_commit(lambda f_id=faktura.id, url=base_url: _safe_vygeneruj_fakturu(f_id, url, False))
            success_message = f'📦 Faktúra {faktura.cislo_faktury} bola úspešne vystavená, tovar odpísaný a PDF sa generuje na pozadí (neodosiela sa).'
        else:
            base_url = request.build_absolute_uri('/')[:-1]
            # send_notif=True: Vygeneruje PDF, odošle maily zákazníkovi aj účtovníčke a odošle doklad do Peppol (SAPI-SK)
            transaction.on_commit(lambda f_id=faktura.id, url=base_url: _safe_vygeneruj_fakturu(f_id, url, True))
            success_message = f'🚀 Faktúra {faktura.cislo_faktury} bola úspešne vystavená a odosiela sa zákazníkovi, účtovníčke aj do siete Peppol.'

        return JsonResponse({'status': 'success', 'faktura_id': faktura.id, 'message': success_message})

    except ValidationError as val_err:
        traceback.print_exc()
        if hasattr(val_err, 'message_dict'):
            zoznam_sprav = [msg for polia in val_err.message_dict.values() for msg in (polia if isinstance(polia, list) else [polia])]
            cista_sprava = " ".join(zoznam_sprav)
        elif hasattr(val_err, 'messages'):
            cista_sprava = " ".join(val_err.messages)
        else:
            cista_sprava = str(val_err)
        return JsonResponse({'status': 'error', 'message': cista_sprava}, status=400)
    except Exception as e:
        traceback.print_exc() # Vynúti výpis chyby do terminálu pre okamžité ladenie
        return JsonResponse({'status': 'error', 'message': f'Nastala chyba pri ukladaní draftu: {str(e)}'}, status=400)

@staff_member_required
def api_get_ponuka_polozky(request):
    """
    Rýchly endpoint: Vráti všetky položky a zľavy z Cenovej Ponuky 
    vo formáte kompatibilnom s Javascript Builderom faktúry.
    """
    ponuka_id = request.GET.get('ponuka_id')
    if not ponuka_id:
        return JsonResponse({'status': 'error', 'message': 'ID ponuky chýba.'})
        
    try:
        ponuka = CenovaPonuka.objects.get(id=ponuka_id)
        
        polozky_data = []
        # select_related pre rýchle načítanie zariadení z riadkov ponuky
        for p in ponuka.polozky.select_related('zariadenie').all():
            polozky_data.append({
                'typ': p.typ_polozky,
                'nazov': p.nazov,
                'mnozstvo': str(p.mnozstvo),
                'jednotka': p.merna_jednotka,
                'cena_bez_dph': str(p.jednotkova_cena_bez_dph),
                'zlava_percento': str(p.zlava_percento),
                'sadzba_dph': str(p.sadzba_dph),
                # 🛡️ ISAS ARCHITECT FIX: Priame čítanie ID eliminuje N+1 SQL dopyty
                'ct_id': str(p.content_type_id) if p.content_type_id else '',
                'obj_id': str(p.object_id) if p.object_id else '',
                'zariadenie_id': str(p.zariadenie_id) if p.zariadenie_id else '',
                'zariadenie_nazov': f"{p.zariadenie.model_zariadenia} (S/N: {p.zariadenie.vyrobne_cislo})" if p.zariadenie else '',
                'praca_naviac': False
            })
            
        # Extrakcia a dešifrovanie meta-údajov z importovanej ponuky
        metadata = {}
        if ponuka.interne_poznamky and ponuka.interne_poznamky.startswith("[ISAS_METADATA_JSON]"):
            try:
                metadata_raw = ponuka.interne_poznamky.replace("[ISAS_METADATA_JSON]", "", 1)
                parsed_meta = json.loads(metadata_raw)
                if isinstance(parsed_meta, dict):
                    metadata = parsed_meta
            except Exception:
                pass

        return JsonResponse({
            'status': 'success',
            'mena': (getattr(ponuka, 'mena', '') or '').strip() or get_firemna_mena(),
            'globalna_zlava': str(ponuka.globalna_zlava_percento),
            'prenos_dph': ponuka.prenos_danovej_povinnosti,
            'zakaznik_id': str(ponuka.zakaznik.id) if ponuka.zakaznik else None,
            'zariadenie_id': str(ponuka.zariadenie.id) if ponuka.zariadenie else None,
            'typ_prace_ids': [str(tp.id) for tp in ponuka.typ_servisu.all()],
            'polozky': polozky_data,
            'metadata': metadata
        })
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)})

@staff_member_required
def api_get_doklady_pre_fakturu(request):
    """
    ISAS CORE AJAX: Dynamické načítanie úloh a záznamov pre Faktúra Builder
    na základe VŠETKÝCH vybratých zariadení (hlavička + riadky) alebo zákazníka v reálnom čase.
    """
    zakaznik_id = request.GET.get('zakaznik_id')
    zariadenia_ids = request.GET.getlist('zariadenia_ids[]') or request.GET.getlist('zariadenia_ids')

    ulohy_qs = Uloha.objects.none()
    zaznamy_qs = ServisnyZaznam.objects.none()

    ciste_zar_ids = [int(zid) for zid in zariadenia_ids if str(zid).isdigit()]

    if ciste_zar_ids:
        ulohy_qs = Uloha.objects.filter(zariadenia__id__in=ciste_zar_ids)
        zaznamy_qs = ServisnyZaznam.objects.filter(zariadenie_id__in=ciste_zar_ids)
        if zakaznik_id and str(zakaznik_id).isdigit():
            ulohy_qs = ulohy_qs.filter(zakaznik_id=int(zakaznik_id))
            zaznamy_qs = zaznamy_qs.filter(zakaznik_id=int(zakaznik_id))
        ulohy_qs = ulohy_qs.distinct().select_related('zakaznik').prefetch_related('typ_servisu', 'zariadenia', 'technici').order_by('-id')[:100]
        zaznamy_qs = zaznamy_qs.select_related('zakaznik', 'zariadenie').prefetch_related('typ_servisu', 'technici').order_by('-id')[:100]

    elif zakaznik_id and str(zakaznik_id).isdigit():
        ulohy_qs = Uloha.objects.filter(zakaznik_id=zakaznik_id).select_related('zakaznik').prefetch_related('typ_servisu', 'zariadenia', 'technici').order_by('-id')[:100]
        zaznamy_qs = ServisnyZaznam.objects.filter(zakaznik_id=zakaznik_id).select_related('zakaznik', 'zariadenie').prefetch_related('typ_servisu', 'technici').order_by('-id')[:100]

    ulohy_data = [{'id': u.id, 'text': str(u)} for u in ulohy_qs]
    zaznamy_data = [{'id': z.id, 'text': str(z)} for z in zaznamy_qs]

    return JsonResponse({
        'status': 'success',
        'ulohy': ulohy_data,
        'zaznamy': zaznamy_data
    })

@staff_member_required
def stiahni_vzorovy_subor_importu(request, typ_importu):
    """
    Dynamicky vygeneruje nádherný XLSX (Excel) súbor so samorozťahovacími stĺpcami
    a prémiovým vizuálnym štýlom ISAS CORE.
    """
    if typ_importu not in ImportDatTask.STLPCE_MAPA:
        raise Http404("Tento typ importu neexistuje.")

    hlavicky = list(ImportDatTask.STLPCE_MAPA[typ_importu])
    if typ_importu == 'SERVISOVANE_ZARIADENIA':
        # 🛡️ ISAS ARCHITECT: 100% dynamické odvetvové pomenovanie stĺpcov podľa aktívneho modulu
        label_meter = get_industry_term('meter_label_short') or get_industry_term('meter_label', 'Aktualne_motohodiny')
        label_load = get_industry_term('meter_load_label', 'Motohodiny_v_zatazi')
        col_meter = re.sub(r'[^\w]', '_', str(label_meter)).strip('_')
        col_load = re.sub(r'[^\w]', '_', str(label_load)).strip('_')
        aliasy_hlaviciek = {
            'Aktualne_motohodiny': col_meter if col_meter else 'Aktualne_motohodiny',
            'Motohodiny_v_zatazi': col_load if col_load else 'Motohodiny_v_zatazi',
        }
        hlavicky = [aliasy_hlaviciek.get(h, h) for h in hlavicky]

    # 1. Vytvorenie Excel zošita
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "ISAS Import Template"

    # Zapnutie mriežky (GridLines) v Exceli
    ws.views.sheetView[0].showGridLines = True

    font_hlavicka = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    fill_hlavicka = PatternFill(start_color="0F172A", end_color="0F172A", fill_type="solid")

    font_info = Font(name="Calibri", size=10, italic=True, color="475569")
    fill_info = PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type="solid")

    font_data = Font(name="Calibri", size=11)

    # 2. RIADOK 1: Hlavičky (Názvy stĺpcov pre systém)
    ws.append(hlavicky)
    for col_idx in range(1, len(hlavicky) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = font_hlavicka
        cell.fill = fill_hlavicka

    # 3. RIADOK 2: Inštrukcie (Nápoveda pre človeka)
    instrukcie_data = {
        'ZAKAZNICI': ['Povinné', 'Povinné (Unikátne)', 'Nepovinné', 'Nepovinné', 'Ulica a číslo', 'Mesto', 'PSČ bez medzier', 'Napr. Slovenská republika', 'E-mail', 'Telefón', 'IBAN v platnom formáte', 'Webová stránka', 'Subdoména portálu', 'Meno osoby', 'Číslo (napr. 14)', 'Číslo (dni reklamácie)', 'pán / pani', 'Formát: DD.MM.RRRR', 'ANO / NIE', 'E-mail na SBS', 'Telefón na SBS', 'Text inštrukcie', 'URL portálu vstupu', 'Číslo (Cena za kWh)', 'Číslo (Emisný faktor)', 'Číslo (B2B zľava v %)', 'ANO / NIE', 'Poznámka'],
        
        'DODAVATELIA': ['Povinné', 'Povinné (Unikátne)', 'Nepovinné', 'Nepovinné', 'Ulica a číslo', 'Mesto', 'PSČ bez medzier', 'Napr. Slovenská republika', 'E-mail', 'Telefón', 'IBAN v platnom formáte', 'Meno osoby', 'Webová stránka', 'Číslo (napr. 14)', 'pán / pani', 'Formát: DD.MM.RRRR', 'ERP predkontácia', 'Poznámka'],
        
        'SUKROMNE_OSOBY': ['Povinné', 'Povinné (Unikátne)', 'Telefón', 'Ulica a číslo', 'Mesto', 'PSČ', 'Štát', 'IBAN', 'Formát: DD.MM.RRRR', 'Číselné ID', 'Číselné ID', 'IČO predvolenej čerpačky'],
        
        'ZAMESTNANCI': ['Povinné', 'Povinné', 'Povinné (Unikátne)', 'Telefón', 'INTERNÝ / EXTERNÝ / BRIGÁDNIK / KONATEĽ', 'TECHNIK / DISPEČER / SKLADNÍK / ADMINISTRATÍVA', 'IBAN', 'Ulica a číslo', 'Mesto', 'PSČ', 'Štát', 'Formát: DD.MM.RRRR', 'Formát: DD.MM.RRRR', 'Číslo (napr. 20)', 'Číslo (napr. 2.5)', 'HODINA / MESIAC / ÚKOL', 'Číslo (napr. 8.50)', 'Číslo (napr. 1500.00)', 'Číslo (sadzba živnosť)', 'Číslo (Dlh voči firme)', 'Poznámka k dlhu', 'Číslo OP', 'Formát: DD.MM.RRRR', 'ANO / NIE', 'ANO / NIE', 'ANO / NIE', 'ANO / NIE', 'Číselné ID', 'Číselné ID', 'IČO predvolenej čerpačky', 'ANO / NIE', 'Verejný titul / rola'],
        
        'VOZIDLA': ['Povinné', 'Povinné (Unikátne bez medzier)', 'NAFTA / BENZÍN 95 / BENZÍN 98 / LPG / ELEKTRINA', 'Iba číslo', 'ANO / NIE', 'OSOBNÉ / NÁKLADNÉ', 'Kód z účtovníctva', 'NM / BA95 / BA98 / LPG / EL', 'Číslo (napr. 0.60)', 'Iba celé číslo (litre)', 'Číslo (max suma v mene)', 'LETNÉ / ZIMNÉ / CELOROČNÉ', 'Iba číslo (napr. 30000)', 'Iba číslo', 'Iba číslo (napr. 15000)', 'Iba číslo', 'Číslo karty', 'Formát: DD.MM.RRRR', 'Formát: DD.MM.RRRR', 'Formát: DD.MM.RRRR', 'Formát: DD.MM.RRRR', 'Formát: DD.MM.RRRR', 'E-mail zamestnanca', 'E-mail súkromnej osoby'],
        
        'TYPY_SERVISU': ['Povinné (Unikátne)', 'Číslo (napr. 35.00)', 'Číslo (napr. 150.00)', 'HODINA / FIX', 'ANO / NIE', 'Číslo (napr. 1.5)', 'Číslo (napr. 15.00)', 'Číslo', 'NÍZKA / STREDNÁ / VYSOKÁ', 'Počet mesiacov (napr. 6)', 'Text postupu', 'ANO / NIE', 'ANO / NIE', 'Verejný popis na web'],
        
        'VYROBCOVIA': ['Povinné (Unikátne)', 'Webová stránka', 'Štát', 'ORIGINÁL / NÁHRADA / ZMIEŠANÉ', 'URL katalógu', 'Poznámka', 'ANO / NIE'],
        
        'FILIALKY': ['Povinné', 'Kód (napr. WH-01)', 'BUDOVA / AUTO / KONTAJNER / INÉ', 'Adresa', 'Meno vedúceho', 'Telefón', 'E-mail', 'ANO / NIE', 'Poznámka', 'E-mail zodpovedného zamestnanca'],
        
        'SERVISOVANE_ZARIADENIA': [
            'Povinné (IČO zákazníka z DB)', f"{' / '.join([c[0] for c in ZARIADENIA_CHOICES[:4]]) if ZARIADENIA_CHOICES else 'zariadenie'}...",
            'Názov výrobcu z DB', 'Model', 'Povinné (Unikátne sériové číslo)', 'Interný názov zariadenia',
            f'Rok (napr. {date.today().year})', 'Formát: DD.MM.RRRR', 'Záruka do: DD.MM.RRRR', 'Predĺžená záruka do: DD.MM.RRRR',
            f"Iba číslo ({get_industry_term('hmotnost_jednotka', 'kg')})", get_industry_term('stanoviste', 'Stanovište / Pozícia'), f"Iba číslo ({get_industry_term('meter_label', 'Počítadlo')})", f"Iba číslo ({get_industry_term('meter_load_label', 'Záťaž')})",
            'MAC adresa (napr. 00:1A:2B:3C:4D:5E)', 'IP adresa', 'SNMP / MODBUS / MQTT', 'ANO / NIE',
            'Číslo (zápisov za hodinu)', 'ANO / NIE', 'ANO / NIE', 'Číslo (Cena prenájmu na deň)',
            'ANO / NIE', 'ANO / NIE', 'Interná poznámka k zariadeniu'
        ],
        
        'SKLAD_KARTY': [
            f"{' / '.join([c[0] for c in CATEGORY_CHOICES]) if CATEGORY_CHOICES else 'Hlavná kategória'}",
            'Presný názov podkategórie z ISAS (vrátane emoji)', 'Povinné (Unikátne sériové číslo)', 'Katalógové číslo (SKU)',
            'EAN kód', 'Obchodný názov položky', 'ANO / NIE', 'Interné pomenovanie (Kto som)',
            'Iba celé číslo (napr. 5)', 'ks, bal, l, m, kg...', 'TOVAR / MATERIAL / SLUZBA',
            f'Napr. {GlobalneNastavenia.get_zakladna_dph()}' if GlobalneNastavenia.get_zakladna_dph() > 0 else 'Zákonná sadzba DPH v % (alebo prázdne)',
            'ORIGINÁL / NÁHRADA', 'IČO dodávateľa (ak existuje)', 'Názov značky výrobcu', 'ANO / NIE', 'Verejný popis na web'
        ],
    }
    
    if typ_importu in instrukcie_data:
        instrukcie = [f"ℹ️ INFO: {text}" for text in instrukcie_data[typ_importu]]
        ws.append(instrukcie)
        for col_idx in range(1, len(instrukcie) + 1):
            cell = ws.cell(row=2, column=col_idx)
            cell.font = font_info
            cell.fill = fill_info

    # 4. RIADOK 3: Ukážkové dáta
    ukazkove_data = {
        'ZAKAZNICI': ['Ukážková Firma s.r.o.', '12345678', '2021234567', 'SK2021234567', 'Hlavná 15', 'Bratislava', '81101', 'Slovenská republika', 'info@firma.sk', '+421900111222', 'SK1209000000001234567890', 'www.firma.sk', 'klient-portal', 'Ján Novák', '14', '30', 'pán', '15.05.1980', 'ANO', 'sbs@firma.sk', '+421900999888', 'Volať 10 min vopred', 'https://vstup.firma.sk', '0.190', '0.1500', '5.00', 'NIE', 'VIP Klient'],
        'DODAVATELIA': ['Dodávateľ s.r.o.', '87654321', '2029876543', 'SK2029876543', 'Priemyselná 10', 'Košice', '04001', 'Slovenská republika', 'obchod@dodavatel.sk', '+421900333444', 'SK9809000000009876543210', 'Jozef Mrkvička', 'www.dodavatel.sk', '30', 'pán', '20.10.1975', '321000', 'Spoľahlivý'],
        'SUKROMNE_OSOBY': ['Ján Novák', 'jan.novak@email.sk', '+421905123456', 'Kvetná 5', 'Trnava', '91701', 'Slovenská republika', 'SK1209000000001234567890', '15.05.1980', '123456789', '987654321', '31322832'],
        'ZAMESTNANCI': ['Peter', 'Technický', 'peter@firma.sk', '+421911222333', 'INTERNÝ', 'TECHNIK', 'SK1109000000001111222233', 'Lesná 1', 'Nitra', '91701', 'Slovenská republika', '10.01.1990', '01.05.2020', '25', '4.5', 'HODINA', '12.50', '0.00', '0.00', '150.00', 'Záloha na materiál', 'AB123456', f'31.12.{date.today().year + 1}', 'ANO', 'ANO', 'ANO', 'ANO', '11223344', '55667788', '31322832', 'ÁNO', 'Vedúci technického úseku'],
        'VOZIDLA': ['Škoda Octavia', 'BA123XY', 'NAFTA', '150000', 'ANO', 'OSOBNÉ', 'AUTO-01', 'NM', '0.00', '60', '100.00', 'LETNÉ', '30000', '145000', '15000', '145000', 'CCS-12345', f'15.05.{date.today().year + 1}', f'31.01.{date.today().year + 1}', f'31.12.{date.today().year}', f'31.12.{date.today().year}', f'01.01.{date.today().year + 2}', 'peter@firma.sk', ''],
        'TYPY_SERVISU': ['Pravidelná kontrola a diagnostika', '50.00', '0.00', 'HODINA', 'ANO', '1.5', '20.00', '0.00', 'STREDNÁ', '6', '1. Vizuálna kontrola, 2. Diagnostický test, 3. Protokol', 'NIE', 'ÁNO', 'Štandardný servisný úkon a komplexná technická diagnostika'],
        'VYROBCOVIA': ['Hlavný Dodávateľ / Výrobca', 'https://www.vyrobca.sk', 'Slovenská republika', 'ORIGINÁL', 'https://www.vyrobca.sk/katalog', 'Certifikovaný partner a autorizovaný distribútor', 'ÁNO'],
        'FILIALKY': ['Hlavný Sklad Bratislava', 'WH-BA-01', 'BUDOVA', 'Skladová 1, Bratislava', 'Jozef Skladník', '+421900111222', 'sklad@firma.sk', 'ANO', 'Otvorené 8:00 - 16:00', 'peter@firma.sk'],
    }

    if 'SERVISOVANE_ZARIADENIA' in SAMPLE_IMPORT_DATA:
        ukazkove_data['SERVISOVANE_ZARIADENIA'] = SAMPLE_IMPORT_DATA['SERVISOVANE_ZARIADENIA']
    else:
        def_kat = ZARIADENIA_CHOICES[0][0] if ZARIADENIA_CHOICES else 'zariadenie'
        ukazkove_data['SERVISOVANE_ZARIADENIA'] = [
            '12345678', def_kat, 'Ukážkový Výrobca', 'Model X1', 'SN-001',
            'Zariadenie 1', str(date.today().year), date.today().strftime('%d.%m.%Y'), date.today().strftime('%d.%m.%Y'), '', '100',
            'Prevádzka / Stanovište', '1000', '250', '', '', '', 'NIE',
            '60', 'NIE', 'NIE', '0.00', 'NIE', 'NIE', 'Vzorová poznámka'
        ]

    # 🛡️ ISAS ARCHITECT: Runtime zistenie DPH pri sťahovaní (ak bol config načítaný pred inicializáciou DB)
    aktivna_dph_str = ""
    try:
        gn_dph = GlobalneNastavenia.get_zakladna_dph()
        if gn_dph and gn_dph > 0:
            val = str(gn_dph)
            aktivna_dph_str = val.rstrip('0').rstrip('.') if '.' in val else val
    except Exception:
        aktivna_dph_str = ""

    if 'SKLAD_KARTY' in SAMPLE_IMPORT_DATA:
        ukazkove_data['SKLAD_KARTY'] = list(SAMPLE_IMPORT_DATA['SKLAD_KARTY'])
        # 🛡️ ISAS ARCHITECT: Dynamické zistenie indexu stĺpca Sadzba_dph bez hardkódu indexu
        sklad_stlpce = ImportDatTask.STLPCE_MAPA.get('SKLAD_KARTY', [])
        if 'Sadzba_dph' in sklad_stlpce:
            dph_idx = sklad_stlpce.index('Sadzba_dph')
            if len(ukazkove_data['SKLAD_KARTY']) > dph_idx and not ukazkove_data['SKLAD_KARTY'][dph_idx]:
                ukazkove_data['SKLAD_KARTY'][dph_idx] = aktivna_dph_str
    else:
        def_hlavna = next(iter(CATEGORY_CHOICES))[0] if CATEGORY_CHOICES else 'Materiál'
        def_pod = next(iter(FLAT_CATEGORY_MAP.keys())) if FLAT_CATEGORY_MAP else 'Všeobecné'
        ukazkove_data['SKLAD_KARTY'] = [
            def_hlavna, def_pod, 'SN-SKLAD-001', 'SKU-001', '1234567890123',
            'Vzorová položka', 'NIE', 'Základný komponent', '10', 'ks',
            'TOVAR', aktivna_dph_str, 'ORIGINÁL',
            '87654321', 'Ukážkový Výrobca', 'ÁNO', 'Popis vzorovej položky'
        ]

    if typ_importu in ukazkove_data:
        ws.append(ukazkove_data[typ_importu])
        for col_idx in range(1, len(ukazkove_data[typ_importu]) + 1):
            cell = ws.cell(row=3, column=col_idx)
            cell.font = font_data

    # 🚀 5. ULTRA-ROBUSTNÉ AUTOMATICKÉ SAMOROZTIAHNUTIE (Auto-Fit)
    for col in ws.columns:
        max_len = 0
        for cell in col:
            val_str = str(cell.value or '')
            # Ak je text dlhý, započítame ho
            if len(val_str) > max_len:
                max_len = len(val_str)
        
        # Získame písmeno stĺpca (A, B, C...)
        col_letter = get_column_letter(col[0].column)
        # Nastavíme šírku s rezervou 3 znakov (minimálna šírka 12 pre pekný vzhľad)
        ws.column_dimensions[col_letter].width = max(max_len + 3, 12)

    # 6. Streamovanie prenosu priamo do prehliadača ako pravý XLSX súbor
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="ISAS_Vzor_{typ_importu}.xlsx"'
    
    buffer = BytesIO()
    wb.save(buffer)
    response.write(buffer.getvalue())
    
    return response

def ai_sepkar_mobilny_view(request, token):
    """
    🔒 ISAS CORE: Zabezpečený mobilný endpoint pre AI Diagnostiku.
    Prístupný iba cez kryptograficky podpísaný token z notifikácie.
    """
    signer = TimestampSigner(salt='isas-ai-sepkar')
    terms = get_all_industry_terms()
    
    # 1. BEZPEČNOSTNÁ VERIFIKÁCIA TOKENU
    try:
        # Token platí presne 7 dní (604800 sekúnd)
        payload = signer.unsign_object(token, max_age=604800)
        if not isinstance(payload, dict):
            raise BadSignature("Neplatná štruktúra tokenu.")
        zariadenie_id = payload.get("z")
        technik_id = payload.get("t") # Extrakcia ID technika pre načítanie draftu
        if not zariadenie_id:
            raise BadSignature("V tokene chýba identifikátor zariadenia.")
    except SignatureExpired:
        return render(request, 'admin/ai_sepkar_mobile.html', {
            'chyba': 'Platnosť tohto bezpečnostného odkazu vypršala (viac ako 7 dní). Vyžiadajte si nový z administrácie.',
            'zariadenie_nazov': 'Neznáme zariadenie',
            'terms': terms,
        })
    except (BadSignature, AttributeError):
        return render(request, 'admin/ai_sepkar_mobile.html', {
            'chyba': 'Neplatný alebo poškodený bezpečnostný kľúč. Prístup zamietnutý.',
            'zariadenie_nazov': 'Neznáme zariadenie',
            'terms': terms,
        })

    # 2. NAČÍTANIE ZARIADENIA (Dynamický prefetch podľa aktívneho odvetvia)
    from .industry_loader import get_active_parameter_reverse_names
    vsetky_vztahy = ['vyrobca'] + get_active_parameter_reverse_names()
    # 🛡️ DEFENSIVE: Overenie existencie relácií na modeli (prevencia FieldError)
    vztahy_na_prefetch = [rel for rel in vsetky_vztahy if hasattr(ServisovaneZariadenie, rel)]
    zariadenie = get_object_or_404(ServisovaneZariadenie.objects.select_related(*vztahy_na_prefetch), id=zariadenie_id)
    
    casti = []
    if zariadenie.vyrobne_cislo:
        casti.append(f"SN: {zariadenie.vyrobne_cislo}")
    if zariadenie.model_zariadenia:
        casti.append(f"TYP: {zariadenie.model_zariadenia}")
    if zariadenie.vyrobca:
        casti.append(str(zariadenie.vyrobca))
        
    zariadenie_nazov = " | ".join(casti) if casti else "Detaily zariadenia neuvedené"

    # 3. AI LOGIKA (Delegovaná do utils pre DRY architektúru)
    try:
        ai_text = generuj_ai_sepkar_analyzu(zariadenie, technik_id=technik_id)
    except ValueError as e:
        if str(e) == "AI_EMPTY_RESPONSE":
            return render(request, 'admin/ai_sepkar_mobile.html', {
                'chyba': 'AI zablokovala odpoveď kvôli prísnym bezpečnostným filtrom alebo vrátila prázdnu odpoveď.',
                'zariadenie_nazov': zariadenie_nazov,
                'terms': terms,
            })
        raise e
    except Exception as e:
        logging.getLogger(__name__).error(f"Kritická chyba v AI Šepkárovi (Mobil): {e}")
        return render(request, 'admin/ai_sepkar_mobile.html', {
            'chyba': 'Umelá inteligencia je momentálne preťažená alebo nedostupná. Skúste to neskôr.',
            'zariadenie_nazov': zariadenie_nazov,
            'terms': terms,
        })

    # 4. FORMÁTOVANIE TEXTU DO HTML ZOZNAMU (Rovnako ako v JS)
    if not ai_text:
        return render(request, 'admin/ai_sepkar_mobile.html', {
            'chyba': 'AI asistent je momentálne nedostupný (chýba konfigurácia alebo spojenie so službou).',
            'zariadenie_nazov': zariadenie_nazov,
            'terms': terms,
        })
    elif ai_text == "EMPTY_HISTORY_FLAG":
        list_html = (
            '<div style="text-align: center; padding: 40px 20px; background: rgba(240, 253, 244, 0.95); border-radius: 24px; border: 1px solid #bbf7d0; box-shadow: 0 10px 25px rgba(34, 197, 94, 0.15); max-width: 800px; margin: 0 auto;">'
            '<div style="font-size: 48px; margin-bottom: 15px;">📝</div>'
            '<h3 style="color: #166534; margin: 0 0 10px 0; font-size: 22px;">Žiadna servisná história</h3>'
            '<p style="color: #15803d; margin: 0; font-weight: 500; font-size: 16px; line-height: 1.5;">Na tomto zariadení zatiaľ nebol evidovaný žiadny servisný zásah.</p>'
            '</div>'
        )
    else:
        riadky = [r for r in ai_text.split('\n') if r.strip()]
        list_html = '<ul class="isas-ai-list">'
        ma_odrazky = False
        for riadok in riadky:
            if riadok.strip().startswith('-'):
                ma_odrazky = True
                cisty_text = riadok.lstrip('- ').strip()
                list_html += f'<li>{cisty_text}</li>'
        list_html += '</ul>'

        if not ma_odrazky:
            list_html = f'<p style="margin:0; font-weight:500; padding: 0 20px;">{ai_text.replace(chr(10), "<br>")}</p>'

    # --- 5. DYNAMICKÁ EXTRAKCIA DÁT PRE TECHNICKÝ PREUKAZ (Skutočne 100% dynamické bez hardkódu) ---
    ui_parametre = []
    vztahy = get_active_parameter_reverse_names()
    
    # Ignorujeme len základné systémové polia (ISASAuditMixin nepridáva vlastné stĺpce)
    ignorovane_polia = ['id', 'zariadenie', 'vytvorene', 'upravene']

    for vztah in vztahy:
        try:
            if hasattr(zariadenie, vztah):
                obj_param = getattr(zariadenie, vztah)
                if obj_param:
                    for field in obj_param._meta.fields:
                        if field.name not in ignorovane_polia:
                            val = getattr(obj_param, field.name)
                            
                            # Zobrazíme len vyplnené hodnoty (0 a False sú platné hodnoty, preto kontrolujeme None a '')
                            if val is not None and val != '':
                                display_func = getattr(obj_param, f'get_{field.name}_display', None)
                                zobrazenie = display_func() if display_func else val
                                
                                # Formátovanie Boolean hodnôt (napr. integrovane_cerpadlo)
                                if isinstance(val, bool):
                                    zobrazenie = "✅ Áno" if val else "❌ Nie"
                                    
                                ui_parametre.append({
                                    'nazov': field.verbose_name, 
                                    'hodnota': zobrazenie
                                })
        except Exception:
            # Ak by OneToOneField vyhodil výnimku pri chýbajúcej relácii, bezpečne pokračujeme
            continue

    # 6. VYKRESLENIE ŠABLÓNY
    # Použijeme list(), aby sme to vyhodnotili hneď a predišli zbytočným dopytom v šablóne.
    historia_ui = list(ServisnyZaznam.objects.filter(zariadenie=zariadenie, dokoncene=True).order_by('-vytvorene')[:3])

    return render(request, 'admin/ai_sepkar_mobile.html', {
        'ai_html_obsah': list_html,
        'zariadenie_nazov': zariadenie_nazov,
        'zariadenie': zariadenie,
        'ui_parametre': ui_parametre,
        'historia': historia_ui if historia_ui else None,
        'terms': terms,
    })

@csrf_exempt
def ai_sepkar_uloz_draft(request, token):
    """🔒 ISAS CORE: Prijme poznámky z webu a uloží ich do RAM pre Bota."""
    if request.method == 'POST':
        
        signer = TimestampSigner(salt='isas-ai-sepkar')
        try:
            payload = signer.unsign_object(token, max_age=604800)
            if not isinstance(payload, dict):
                return JsonResponse({"status": "error", "msg": "Neplatný bezpečnostný token."})
            technik_id = payload.get("t")
            
            if not technik_id:
                return JsonResponse({"status": "error", "msg": "Token neobsahuje ID technika. Vyžiadaj si novú úlohu."})
            
            data = json.loads(request.body)

            # Ochrana pred JSON poľom (zabraňuje AttributeError)
            if not isinstance(data, dict):
                return JsonResponse({"status": "error", "msg": "Payload musí byť JSON objekt (dictionary)."})

            text = str(data.get('text') or '').strip()
            
            if text:
                # Uložíme do cache na 4 hodiny (14400 sekúnd)
                cache.set(f"isas_quicknote_draft_{technik_id}", text, timeout=14400)
                return JsonResponse({"status": "ok"})
            return JsonResponse({"status": "empty"})
        except Exception:
            logger.exception("Chyba pri ukladaní draftu v AI Šepkárovi")
            return JsonResponse({"status": "error", "msg": "Nepodarilo sa uložiť poznámku."}, status=400)
    return JsonResponse({"status": "invalid_method"})

def accountant_erp_setup_view(request, token):
    signer = TimestampSigner()
    try:
        token_data = signer.unsign_object(token, max_age=604800)
        if not isinstance(token_data, dict):
            raise BadSignature("Neplatná štruktúra tokenu.")
        notify_tg = token_data.get("tg", True)
        notify_mail = token_data.get("mail", True)
        notify_discord = token_data.get("discord", True)
    except (BadSignature, SignatureExpired, AttributeError):
        return HttpResponseForbidden("❌ Tento odkaz je neplatný alebo jeho platnosť vypršala (7 dní). Požiadajte o nový odkaz.")

    erp_settings = ERPExportNastavenia.get_settings()

    if request.method == 'POST':
        form = ERPAccountantForm(request.POST, instance=erp_settings)
        if form.is_valid():
            form.save()          
            ip_adresa = get_client_ip(request) or 'Neznáma'

            try:
                task_odoslat_notifikaciu_o_nastaveni_erp.delay(ip_adresa, notify_tg, notify_mail, notify_discord)
            except Exception as exc:
                logger.warning(f"Zlyhalo zaradenie task_odoslat_notifikaciu_o_nastaveni_erp do Celery: {exc}")
            
            return render(request, 'klientsky_portal/erp_accountant_form.html', {'success': True})
    else:
        form = ERPAccountantForm(instance=erp_settings)

    try:
        FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
        iso_krajiny = FiremnaIdentitaModel.get_aktivna_krajina_iso() if hasattr(FiremnaIdentitaModel, 'get_aktivna_krajina_iso') else ''
        iso_krajiny = str(iso_krajiny).strip().upper() if iso_krajiny else ''
    except Exception:
        iso_krajiny = ''

    context = {
        'form': form,
        'erp_system_value': erp_settings.erp_system,
        'iso_krajiny': iso_krajiny
    }

    return render(request, 'klientsky_portal/erp_accountant_form.html', context)

@staff_member_required
def hr_dashboard_view(request):
    """🧠 ISAS HR DASHBOARD PRO: Vizuálny kalendár absencií a kapacít."""
    
    dnes = date.today()
    try:
        mesiac = int(request.GET.get('mesiac', dnes.month))
        if mesiac < 1 or mesiac > 12:
            mesiac = dnes.month
    except (ValueError, TypeError):
        mesiac = dnes.month

    try:
        rok = int(request.GET.get('rok', dnes.year))
        if rok < 2000 or rok > 2100:
            rok = dnes.year
    except (ValueError, TypeError):
        rok = dnes.year

    rola_filter = request.GET.get('rola', None)
    
    # 🚀 ISAS HYBRID LOGIC: Načítanie limitov
    gn = GlobalneNastavenia.objects.first()
    db_busy = getattr(gn, 'limit_uloh_zvysena', None) or 3
    db_crit = getattr(gn, 'limit_uloh_kriticka', None) or 5
    
    # Ak dispečer zadal vlastné limity do URL, použijú sa tie. Inak sa použijú firemné z DB.
    try:
        limit_busy = int(request.GET.get('busy', db_busy))
    except (ValueError, TypeError):
        limit_busy = db_busy

    try:
        limit_crit = int(request.GET.get('crit', db_crit))
    except (ValueError, TypeError):
        limit_crit = db_crit
    
    prev_mesiac = mesiac - 1 if mesiac > 1 else 12
    prev_rok = rok if mesiac > 1 else rok - 1
    next_mesiac = mesiac + 1 if mesiac < 12 else 1
    next_rok = rok if mesiac < 12 else rok + 1

    pocet_dni = calendar.monthrange(rok, mesiac)[1]
    dni_v_mesiaci = [date(rok, mesiac, den) for den in range(1, pocet_dni + 1)]

    # 1. Načítanie zamestnancov (s možnosťou filtra) a 🛡️ OCHRANA PRED N+1
    zamestnanci_qs = Zamestnanec.objects.order_by('priezvisko', 'meno')
    
    if rola_filter:
        zamestnanci_qs = zamestnanci_qs.filter(pracovna_rola=rola_filter)
    zamestnanci = list(zamestnanci_qs)

    # 2. Načítanie absencií (Ignorujeme zamietnuté žiadosti)
    absencie = Nepritomnost.objects.filter(
        datum_od__lte=date(rok, mesiac, pocet_dni),
        datum_do__gte=date(rok, mesiac, 1)
    ).exclude(stav='ZAMIETNUTE').select_related('zamestnanec', 'schvalil')

    # 3. 🚀 NAČÍTANIE ÚLOH (Ochrana pred prekrývaním mesiacov)
    start_mesiaca = date(rok, mesiac, 1)
    koniec_mesiaca = date(rok, mesiac, pocet_dni)

    # Vytiahneme všetky úlohy, ktoré sa akokoľvek dotýkajú aktuálneho mesiaca
    ulohy = Uloha.objects.filter(
        Q(zaciatok__date__lte=koniec_mesiaca) & 
        (Q(koniec__date__gte=start_mesiaca) | Q(koniec__isnull=True, zaciatok__date__gte=start_mesiaca))
    ).select_related(
        'zakaznik' # 🚀 ISAS FIX: Rieši N+1 pre ForeignKey v __str__
    ).prefetch_related(
        'technici', 'typ_servisu', 'zariadenia' # 🚀 ISAS FIX: Rieši N+1 pre ManyToMany v __str__
    )

    # 4. Mapovanie dát do mriežky
    mapa_absencii = {z.id: {} for z in zamestnanci}
    mapa_uloh = {z.id: {} for z in zamestnanci}
    
    for absencia in absencie:
        if not absencia.datum_od or not absencia.datum_do:
            continue
        aktualny_den = max(absencia.datum_od, start_mesiaca)
        konecny_den = min(absencia.datum_do, koniec_mesiaca)
        while aktualny_den <= konecny_den:
            mapa_absencii.setdefault(absencia.zamestnanec_id, {})[aktualny_den] = absencia
            aktualny_den += timedelta(days=1)

    # 🚀 Naplnenie mapy úloh (Orezané na hranice mesiaca - prevencia CPU DoS)
    for uloha in ulohy:
        if not uloha.zaciatok:
            continue
        start_ulohy = uloha.zaciatok.date() if hasattr(uloha.zaciatok, 'date') else uloha.zaciatok
        end_ulohy = uloha.koniec.date() if (uloha.koniec and hasattr(uloha.koniec, 'date')) else start_ulohy
        
        # Orezanie iterácie výhradne na dni aktuálne zobrazeného mesiaca
        aktualny_den_ulohy = max(start_ulohy, start_mesiaca)
        konecny_den_ulohy = min(end_ulohy, koniec_mesiaca)
        
        while aktualny_den_ulohy <= konecny_den_ulohy:
            for technik in uloha.technici.all():
                if technik.id in mapa_uloh:
                    if aktualny_den_ulohy not in mapa_uloh[technik.id]:
                        mapa_uloh[technik.id][aktualny_den_ulohy] = []
                    mapa_uloh[technik.id][aktualny_den_ulohy].append(uloha)
            aktualny_den_ulohy += timedelta(days=1)

    # 5. 🚨 KPI: Čakajúce schvaľovania (Celofiremné, nielen pre tento mesiac)
    cakajuce_absencie_pocet = Nepritomnost.objects.filter(stav='CAKA').count()
    
    dnes_plus_30 = dnes + timedelta(days=30)

    context = {
        'title': 'HR Dashboard | ISAS CORE',
        'rok': rok, 'mesiac': mesiac,
        'prev_mesiac': prev_mesiac, 'prev_rok': prev_rok,
        'next_mesiac': next_mesiac, 'next_rok': next_rok,
        'dni_v_mesiaci': dni_v_mesiaci,
        'zamestnanci': zamestnanci,
        'mapa_absencii': mapa_absencii,
        'mapa_uloh': mapa_uloh,
        'cakajuce_absencie_pocet': cakajuce_absencie_pocet,
        'aktualna_rola': rola_filter,
        'dnes': dnes,
        'dnes_plus_30': dnes_plus_30,
        'dnes_minus_90': dnes - timedelta(days=90), # Hranica pre skúšobnú dobu
        'limit_busy': limit_busy,
        'limit_crit': limit_crit,
        'mena': get_firemna_mena(),
    }
    
    return render(request, 'admin/hr_dashboard.html', context)

@csrf_exempt
@require_POST
def api_iot_telemetry_ingest(request):
    """
    🚀 ISAS CORE: Ultra-rýchly IoT Ingest Endpoint.
    Vykonáva len Redis autentifikáciu a Rate Limiting. Spracovanie deleguje na Celery.
    """
    try:
        payload = json.loads(request.body)
        # 🛡️ DEFENZÍVA: Ochrana pred JSON poľom (napr. [1, 2, 3])
        if not isinstance(payload, dict):
            return JsonResponse({'error': 'Payload musí byť JSON objekt (dictionary)'}, status=400)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Neplatný JSON formát'}, status=400)

    api_key = payload.get('api_key')
    if not api_key or not isinstance(api_key, str) or len(api_key) > 64 or any(c.isspace() or ord(c) < 32 for c in api_key):
        return JsonResponse({'error': 'Chýba alebo je neplatný api_key'}, status=401)

    # 🧠 ISAS CACHE ENGINE: Overenie kľúča cez Redis (Zabraňuje N+1 a DB hitom)
    cache_key_config = f"iot_auth_{api_key}"
    config_data = cache.get(cache_key_config)

    if not config_data or not isinstance(config_data, dict):
        # Ak nie je v RAM, overíme priamo na ServisovaneZariadenie
        zariadenie = ServisovaneZariadenie.objects.filter(iot_api_kluc=api_key, iot_je_aktivny=True).first()
        if not zariadenie:
            return JsonResponse({'error': 'Neplatný alebo neaktívny API kľúč'}, status=403)
        
        # Uložíme do Redisu na 1 hodinu
        config_data = {
            'zariadenie_id': zariadenie.id,
            'limit': zariadenie.iot_limit_zapisov_za_hodinu or 1000
        }
        cache.set(cache_key_config, config_data, timeout=3600)

    # Ochrana pred DDoS a chybným hardvérom (Atomicky s hodinovým bucketom)
    hodina_bucket = datetime.now().strftime('%Y%m%d%H')
    cache_key_rate = f"iot_rate_{config_data['zariadenie_id']}_{hodina_bucket}"
    
    try:
        # Atomické zvýšenie priamo v Redise (zabraňuje Race Condition)
        current_requests = cache.incr(cache_key_rate)
    except ValueError:
        # Ak kľúč pre danú hodinu ešte neexistuje, vytvoríme ho s expiráciou 3700s (hodina + rezerva)
        cache.set(cache_key_rate, 1, timeout=3700)
        current_requests = 1
    
    # Fallback na 1000 zápisov, ak technik nevyplnil limit v databáze (Ochrana pred TypeError)
    limit_raw = config_data.get('limit')
    try:
        limit = int(limit_raw) if limit_raw is not None else 1000
    except (ValueError, TypeError):
        limit = 1000
        
    if current_requests > limit:
        return JsonResponse({'error': 'Prekročený limit zápisov (Rate Limit)'}, status=429)

    # ⚡ OFFLOADING: Odoslanie do Celery fronty s núdzovým Redis spoolingom
    try:
        process_iot_payload_task.delay(payload, config_data['zariadenie_id'])
    except Exception as exc:
        logger.warning(f"Zlyhalo zaradenie process_iot_payload_task do Celery: {exc}. Ukladám do záložného Redis spoolu.")
        try:
            # 🛡️ ATOMICKÝ SPOOL: Využívame natívny redis klient pre bezpečný push bez race condition
            client = cache._cache.get_client()
            spool_payload = json.dumps({'payload': payload, 'config_id': config_data['zariadenie_id']})
            if client.llen('iot_fallback_spool_queue') < 20000:
                client.rpush('iot_fallback_spool_queue', spool_payload)
                return JsonResponse({'status': 'spooled', 'message': 'Dáta prijaté do núdzovej pamäte'}, status=202)
        except Exception as spool_err:
            logger.critical(f"Kritické zlyhanie núdzového spoolingu telemetrie: {spool_err}")
        return JsonResponse({'error': 'Telemetrická fronta je dočasne nedostupná'}, status=503)

    return JsonResponse({'status': 'queued', 'message': 'Dáta prijaté na spracovanie'}, status=200)

def iot_dashboard_spa(request):
    """
    🎨 ISAS CORE: VIVID LIGHT SPA Dashboard (HTML Shell).
    """
    identita = FiremnaIdentita.objects.first()
    firemna_mena = getattr(identita, 'predvolena_mena', None) or get_firemna_mena()

    # 1. Klient pristupujúci cez vyhradenú firemnú subdoménu
    if getattr(request, 'is_subdomain', False) and request.tenant:
        if not request.user.is_authenticated:
            raise Http404("Prístup odopretý. Musíte sa prihlásiť.")
            
        # 🛡️ TENANT ISOLATION: Cudzia firma na subdoméne nesmie vidieť dáta tohto tenanta
        user_tenant = getattr(request.user, 'zakaznik_profil', None)
        if not request.user.is_staff and user_tenant != request.tenant:
            raise Http404("Prístup odopretý. Nemáte oprávnenie pre tento klientsky portál.")

        context = {
            'tenant': request.tenant, 
            'is_client_portal': True,
            'servis_firma': identita,
            'mena': firemna_mena,
            'meter_label': get_industry_term('meter_label', 'Počítadlo'),
            'meter_unit': get_industry_term('meter_unit', 'hod.'),
            'meter_load_label': get_industry_term('meter_load_label', 'Záťaž'),
            'meter_load_unit': get_industry_term('meter_load_unit', 'hod.'),
        }
        return render(request, 'iot/iot_dashboard_spa.html', context)

    # 2. 🚀 ISAS FIX: Autorizovaný B2B klient prihlásený cez hlavný web
    if request.user.is_authenticated and getattr(request.user, 'zakaznik_profil', None):
        context = {
            'tenant': request.user.zakaznik_profil,
            'is_client_portal': True,
            'servis_firma': identita,
            'mena': firemna_mena,
            'meter_label': get_industry_term('meter_label', 'Počítadlo'),
            'meter_unit': get_industry_term('meter_unit', 'hod.'),
            'meter_load_label': get_industry_term('meter_load_label', 'Záťaž'),
            'meter_load_unit': get_industry_term('meter_load_unit', 'hod.'),
        }
        return render(request, 'iot/iot_dashboard_spa.html', context)
    
    # 3. Interný personál (Technik / Dispečer)
    if not request.user.is_authenticated or not request.user.is_staff:
        raise Http404("Prístup odopretý.")
        
    context = {
        'is_client_portal': False,
        'servis_firma': identita,
        'mena': firemna_mena,
        'meter_label': get_industry_term('meter_label', 'Počítadlo'),
        'meter_unit': get_industry_term('meter_unit', 'hod.'),
        'meter_load_label': get_industry_term('meter_load_label', 'Záťaž'),
        'meter_load_unit': get_industry_term('meter_load_unit', 'hod.'),
    }

    return render(request, 'iot/iot_dashboard_spa.html', context)

def api_iot_get_devices(request):
    """
    🧠 ISAS CORE: API pre kaskádový filter a Globálne Alarmy.
    """
    # 🔒 TENANT ISOLATION (🛡️ ISAS SECURITY: Vyžaduje sa prihlásený používateľ aj na subdoméne)
    b2b_klient = getattr(request.user, 'zakaznik_profil', None) if request.user.is_authenticated else None

    if getattr(request, 'is_subdomain', False) and request.tenant and request.user.is_authenticated:
        # 🛡️ BOLA OCHRANA: Overenie, či používateľ skutočne patrí k tenantovi na subdoméne
        if not request.user.is_staff and b2b_klient != request.tenant:
            return JsonResponse({'error': 'Forbidden'}, status=403)
        zariadenia = ServisovaneZariadenie.objects.select_related('zakaznik').filter(
            Q(zakaznik=request.tenant) | Q(prenajmy__zakaznik=request.tenant),
            iot_je_aktivny=True
        ).distinct()
        alarmy_qs = AlarmLog.objects.select_related('zariadenie').filter(
            Q(zakaznik=request.tenant) | Q(zariadenie__prenajmy__zakaznik=request.tenant),
            vyriesene=False
        ).distinct().order_by('-casova_peciatka', '-id')[:10]
    elif b2b_klient:
        zariadenia = ServisovaneZariadenie.objects.select_related('zakaznik').filter(
            Q(zakaznik=b2b_klient) | Q(prenajmy__zakaznik=b2b_klient),
            iot_je_aktivny=True
        ).distinct()
        alarmy_qs = AlarmLog.objects.select_related('zariadenie').filter(
            Q(zakaznik=b2b_klient) | Q(zariadenie__prenajmy__zakaznik=b2b_klient),
            vyriesene=False
        ).distinct().order_by('-casova_peciatka', '-id')[:10]
    elif request.user.is_authenticated and request.user.is_staff:
        zariadenia = ServisovaneZariadenie.objects.select_related('zakaznik').filter(iot_je_aktivny=True)
        alarmy_qs = AlarmLog.objects.select_related('zariadenie').filter(vyriesene=False).order_by('-casova_peciatka', '-id')[:10]
    else:
        return JsonResponse({'error': 'Unauthorized'}, status=401)

    data = []
    for z in zariadenia:
        data.append({
            'id': z.id,
            'nazov': z.nazov or z.model_zariadenia,
            'sn': z.vyrobne_cislo,
            'kategoria': z.get_kategoria_display(),
            'zakaznik': z.zakaznik.Nazov_spolocnosti if z.zakaznik else 'Neznámy'
        })
        
    # 🚨 Globálne alarmy pre úvodnú obrazovku (🛡️ Ochrana pred AttributeError ak alarm nemá priradené zariadenie)
    globalne_alarmy = [{
        'id': a.id, # 👈 PRIDANÉ PRE JS
        'cas': a.casova_peciatka.strftime('%d.%m.%Y %H:%M'),
        'zariadenie': f"{a.zariadenie.model_zariadenia or a.zariadenie.nazov or 'Zariadenie'} (SN: {a.zariadenie.vyrobne_cislo})" if a.zariadenie else "Systémový alarm",
        'kod': a.kod_chyby,
        'popis': a.popis,
        'zavaznost': a.zavaznost,
        'vyriesene': a.vyriesene
    } for a in alarmy_qs]
    
    return JsonResponse({'zariadenia': data, 'globalne_alarmy': globalne_alarmy})

def api_iot_get_live_data(request, zariadenie_id):
    """
    🧠 ISAS CORE: API pre živé budíky (Gauges) a čiernu skrinku.
    """
    # 🔒 TENANT ISOLATION & ZERO N+1 OPTIMALIZÁCIA PRE 5S POLLING
    b2b_klient = getattr(request.user, 'zakaznik_profil', None) if request.user.is_authenticated else None
    param_relations = get_active_parameter_reverse_names()
    zar_qs = ServisovaneZariadenie.objects.select_related(
        'zakaznik', 'vyrobca', *param_relations
    )

    if getattr(request, 'is_subdomain', False) and request.tenant and request.user.is_authenticated:
        # 🛡️ BOLA OCHRANA: Blokovanie neoprávneného prístupu k telemetrii cudzieho stroja (vrátane prenájmov)
        if not request.user.is_staff and b2b_klient != request.tenant:
            return JsonResponse({'error': 'Forbidden'}, status=403)
        zariadenie = zar_qs.filter(
            Q(zakaznik=request.tenant) | Q(prenajmy__zakaznik=request.tenant),
            id=zariadenie_id
        ).first()
    elif b2b_klient:
        zariadenie = zar_qs.filter(
            Q(zakaznik=b2b_klient) | Q(prenajmy__zakaznik=b2b_klient),
            id=zariadenie_id
        ).first()
    elif request.user.is_authenticated and request.user.is_staff:
        zariadenie = zar_qs.filter(id=zariadenie_id).first()
    else:
        return JsonResponse({'error': 'Unauthorized'}, status=401)

    if not zariadenie:
        return JsonResponse({'error': 'Zariadenie nenájdené'}, status=404)

    # Dynamický časový rozsah pre graf (z URL parametra)
    time_range = request.GET.get('range', '1h')
    now = datetime.now()
    
    if time_range == '1h':
        start_time = now - timedelta(hours=1)
        format_casu = '%H:%M:%S'
    elif time_range == '24h':
        start_time = now - timedelta(hours=24)
        format_casu = '%H:%M'
    elif time_range == '3d':
        start_time = now - timedelta(days=3)
        format_casu = '%d.%m. %H:%M'
    elif time_range == '7d':
        start_time = now - timedelta(days=7)
        format_casu = '%d.%m. %H:%M'
    elif time_range == '14d':
        start_time = now - timedelta(days=14)
        format_casu = '%d.%m. %H:%M'
    elif time_range == '30d':
        start_time = now - timedelta(days=30)
        format_casu = '%d.%m.'
    elif time_range == '90d':
        start_time = now - timedelta(days=90)
        format_casu = '%d.%m.%Y'
    elif time_range == '180d':
        start_time = now - timedelta(days=180)
        format_casu = '%d.%m.%Y'
    elif time_range == '365d':
        start_time = now - timedelta(days=365)
        format_casu = '%m/%Y'
    else:
        start_time = now - timedelta(hours=1)
        format_casu = '%H:%M:%S'

    # 🛡️ DEFENZÍVA: Podpora Delta Pollingu (ak klient pošle last_id, ťaháme len prírastok)
    last_id_param = request.GET.get('last_id')
    is_delta = False

    if last_id_param and last_id_param.isdigit():
        historia_qs = TelemetryHistory.objects.filter(
            zariadenie=zariadenie,
            id__gt=int(last_id_param)
        ).only('id', 'casova_peciatka', 'analogove_hodnoty').order_by('id')[:200]
        historia = list(historia_qs)
        is_delta = True
    else:
        historia_qs = TelemetryHistory.objects.filter(
            zariadenie=zariadenie, 
            casova_peciatka__gte=start_time
        ).only('id', 'casova_peciatka', 'analogove_hodnoty').order_by('-casova_peciatka', '-id')[:1000]
        historia = list(reversed(list(historia_qs)))
    
    # Posledný záznam ťaháme absolútne, aby budíky a status fungovali aj keď v danom časovom okne grafu nie sú dáta
    posledny_zaznam = TelemetryHistory.objects.filter(zariadenie=zariadenie).order_by('-casova_peciatka', '-id').first()
    
    # 🚀 Reálny výpočet ONLINE stavu (Dáta nesmú byť staršie ako 2 minúty)
    is_online = False
    if posledny_zaznam and posledny_zaznam.casova_peciatka:
        casovy_rozdiel = datetime.now() - posledny_zaznam.casova_peciatka
        # Ochrana pred časovým posunom do budúcnosti (tolerancia max 30s dopredu a max 120s dozadu)
        if -30 <= casovy_rozdiel.total_seconds() < 120:
            is_online = True
            
    # Pripravíme dáta pre Chart.js (s prírastkovým streamingom bez sieťového preťaženia)
    current_last_id = historia[-1].id if historia else (int(last_id_param) if is_delta else (posledny_zaznam.id if posledny_zaznam else None))
    graf_data = {
        'casy': [z.casova_peciatka.strftime(format_casu) for z in historia],
        'hodnoty': [z.analogove_hodnoty or {} for z in historia],
        'is_delta': is_delta,
        'last_id': current_last_id
    }
    
    # Získame posledných 5 nevyriešených alarmov s deterministickým radením
    alarmy = AlarmLog.objects.filter(zariadenie=zariadenie, vyriesene=False).order_by('-casova_peciatka', '-id')[:5]
    alarmy_data = [{
        'id': a.id,
        'cas': a.casova_peciatka.strftime('%d.%m.%Y %H:%M:%S'),
        'kod': a.kod_chyby,
        'popis': a.popis,
        'zavaznost': a.zavaznost,
        'vyriesene': a.vyriesene
    } for a in alarmy]

    # 🧠 ISAS CORE: Výpočet energetickej efektivity, strát a ESG (CO2)
    mh_celkove = zariadenie.aktualne_motohodiny or 0
    mh_zataz = getattr(zariadenie, 'motohodiny_v_zatazi', 0) or 0
    efektivita = 0
    strata_eur = Decimal('0.00')
    co2_tony = 0
    
    if mh_celkove > 0:
        vypocitana_efekt = (mh_zataz / mh_celkove) * 100
        efektivita = round(min(100.0, max(0.0, vypocitana_efekt)), 1)
        
        if HAS_ENERGY_ESG_AUDIT:
            param_obj = get_zariadenie_parametre(zariadenie)
            vykon_raw = getattr(param_obj, 'vykon_kw', None) if param_obj else None

            if vykon_raw is not None:
                try:
                    vykon_kw = float(str(vykon_raw).replace(',', '.'))
                except (ValueError, TypeError):
                    vykon_kw = None
                        
                if vykon_kw:
                    # 🧠 UNIVERZÁLNY KOEFICIENT: Delegovaný na model odvetvia (pre kompresor 0.30, inak 0.0)
                    koef_naprazdno_raw = getattr(param_obj, 'koeficient_naprazdno', 0.0)
                    try:
                        koef_naprazdno = float(koef_naprazdno_raw)
                    except (ValueError, TypeError):
                        koef_naprazdno = 0.0

                    # 1. Výpočet strát (Chod naprázdno)
                    mh_naprazdno = max(0, mh_celkove - mh_zataz)
                    spotreba_naprazdno_kwh = 0.0
                    if mh_naprazdno > 0 and koef_naprazdno > 0:
                        spotreba_naprazdno_kwh = mh_naprazdno * (vykon_kw * koef_naprazdno)
                        try:
                            cena_kwh = Decimal(str(zariadenie.zakaznik.cena_za_kwh_eur).replace(',', '.')) if (zariadenie.zakaznik and zariadenie.zakaznik.cena_za_kwh_eur is not None) else Decimal('0.000')
                        except (InvalidOperation, TypeError, ValueError):
                            cena_kwh = Decimal('0.000')
                        strata_eur = (Decimal(str(spotreba_naprazdno_kwh)) * cena_kwh).quantize(Decimal('0.01'))

                    # 2. 🍃 ISAS ESG CO2 Tracker
                    celkova_spotreba_kwh = (mh_zataz * vykon_kw) + spotreba_naprazdno_kwh
                    
                    # Vytiahnutie certifikovaného faktora priamo z karty zákazníka
                    emisny_faktor = 0.0
                    if zariadenie.zakaznik and zariadenie.zakaznik.emisny_faktor_co2 is not None:
                        try:
                            emisny_faktor = float(str(zariadenie.zakaznik.emisny_faktor_co2).replace(',', '.'))
                        except (ValueError, TypeError):
                            emisny_faktor = 0.0
                    
                    # Výpočet: Spotreba (kWh) * Faktor (kg/kWh) = kg CO2
                    co2_kg = celkova_spotreba_kwh * emisny_faktor
                    co2_tony = round(co2_kg / 1000, 2) # Prevod na tony pre prehľadnosť na Dashboarde

    # História servisov (VŠETKY záznamy s extrémnou optimalizáciou RAM a N+1)
    servisy_qs = ServisnyZaznam.objects.filter(
        zariadenie=zariadenie, dokoncene=True
    ).prefetch_related(
        'typ_servisu'
    ).only(
        'id', 'vytvorene', 'poruchy', 'formalne_zhodnotenie_ai', 'protokol_uzavrety', 
        'bezpecnostny_kluc', 'uzavrety_pdf_dokument', 'stav_motohodin'
    ).order_by('-vytvorene', '-id')
    
    servisy_data = []
    for s in servisy_qs:
        # Voláme priamo t.nazov, čím obchádzame __str__ a garantujeme 0 extra SQL dopytov
        typy = ", ".join([t.nazov for t in s.typ_servisu.all()]) or "Všeobecný servis"
        
        # Prioritne ťaháme fyzické PDF. Ak neexistuje, použijeme bezpečný dynamický reverz
        pdf_url = None
        if s.uzavrety_pdf_dokument and s.uzavrety_pdf_dokument.name:
            try:
                pdf_url = s.uzavrety_pdf_dokument.url
            except ValueError:
                pdf_url = None
        if not pdf_url and s.protokol_uzavrety and s.bezpecnostny_kluc:
            try:
                pdf_url = reverse('evidencia:protokol_detail', kwargs={'kluc': s.bezpecnostny_kluc})
            except Exception:
                pdf_url = None
            
        # Dynamické pridanie stavu počítadla cez odvetvový industry_loader
        mh_text = f"[{s.meter_display}] " if s.stav_motohodin is not None else ""
        text_zaznamu = s.formalne_zhodnotenie_ai or s.poruchy or ""
        popis = text_zaznamu.strip() if text_zaznamu else "Bez popisu"
        popis = f"{mh_text}{popis}"
        if len(popis) > 60:
            popis = popis[:57] + "..."
            
        servisy_data.append({
            'datum': s.vytvorene.strftime('%d.%m.%Y'),
            'typ': typy,
            'popis': popis,
            'pdf_url': pdf_url
        })

    # Najbližší plánovaný servis
    dalsi_servis = PlanovanyServisZariadenia.objects.filter(
        zariadenie=zariadenie, upozornenie_odoslane=False
    ).order_by('planovany_datum', 'planovane_motohodiny').first()
    
    predikcia_data = None
    if dalsi_servis:
        limit_text = []
        if dalsi_servis.planovany_datum:
            limit_text.append(f"Dátum: {dalsi_servis.planovany_datum.strftime('%d.%m.%Y')}")
            
        percento_opotrebenia = 0
        if dalsi_servis.planovane_motohodiny is not None:
            limit_text.append(f"Pri: {dalsi_servis.planovane_motohodiny} {zariadenie.meter_unit}")
            
            # 🧠 ISAS CORE: Výpočet reálneho opotrebenia do ďalšieho servisu
            posledne_mh = 0
            if dalsi_servis.posledny_zaznam and dalsi_servis.posledny_zaznam.stav_motohodin:
                posledne_mh = dalsi_servis.posledny_zaznam.stav_motohodin
                
            rozsah_intervalu = dalsi_servis.planovane_motohodiny - posledne_mh
            odpracovane_od_servisu = (zariadenie.aktualne_motohodiny or 0) - posledne_mh
            
            if rozsah_intervalu > 0:
                percento_opotrebenia = max(0, min(int((odpracovane_od_servisu / rozsah_intervalu) * 100), 100))
        elif dalsi_servis.planovane_motohodiny_zataz is not None:
            posledne_mh_z = 0
            if dalsi_servis.posledny_zaznam and dalsi_servis.posledny_zaznam.motohodiny_v_zatazi:
                posledne_mh_z = dalsi_servis.posledny_zaznam.motohodiny_v_zatazi
            rozsah_intervalu = dalsi_servis.planovane_motohodiny_zataz - posledne_mh_z
            odpracovane_od_servisu = (getattr(zariadenie, 'motohodiny_v_zatazi', 0) or 0) - posledne_mh_z
            if rozsah_intervalu > 0:
                percento_opotrebenia = max(0, min(int((odpracovane_od_servisu / rozsah_intervalu) * 100), 100))

        if dalsi_servis.planovane_motohodiny_zataz is not None:
            m_load_unit = get_industry_term('meter_load_unit', zariadenie.meter_unit)
            limit_text.append(f"{zariadenie.meter_load_label}: {dalsi_servis.planovane_motohodiny_zataz} {m_load_unit}")
            
        predikcia_data = {
            'typ': dalsi_servis.typ_servisu.nazov if dalsi_servis.typ_servisu else 'Údržba',
            'limit': " | ".join(limit_text) if limit_text else "Podľa potreby",
            'percento': percento_opotrebenia
        }

    # 🧠 ISAS CORE: Vytiahnutie merných jednotiek zo slovníka
    slovnik_qs = SignalDictionary.objects.filter(zariadenie=zariadenie)
    jednotky_mapa = {sig.nazov_signalu: sig.merna_jednotka for sig in slovnik_qs if sig.merna_jednotka}
    
    ovladanie_mapa = {
        sig.nazov_signalu: {
            'adresa': sig.register_adresa,
            'prava': sig.prava_pristupu,
            'typ': sig.typ_signalu
        } for sig in slovnik_qs
    }

    # 🆔 ISAS IDENTITY: Dáta stroja (Defenzívne ošetrenie voči chýbajúcemu súboru na úložisku)
    qr_url = None
    if zariadenie.qr_kod:
        try:
            qr_url = zariadenie.qr_kod.url
        except (ValueError, AttributeError):
            qr_url = None

    je_personal = request.user.is_authenticated and (request.user.is_staff or request.user.is_superuser) and not hasattr(request.user, 'zakaznik_profil')
    identita_data = {
        'vyrobca': (str(zariadenie.vyrobca) if zariadenie.vyrobca else '') if je_personal else '',
        'model': zariadenie.model_zariadenia or zariadenie.nazov or 'Zariadenie',
        'sn': zariadenie.vyrobne_cislo or 'Nezadané',
        'rok': zariadenie.rok_vyroby or 'Nezadaný',
        'qr_kod_url': qr_url,
        'passport_url': f"/zariadenie/{zariadenie.bezpecnostny_kluc}/" if zariadenie.bezpecnostny_kluc else None,
        'zaruka_do': zariadenie.zaruka_do.strftime('%d.%m.%Y') if zariadenie.zaruka_do else None,
        'predlzena_zaruka_do': zariadenie.predlzena_zaruka_do.strftime('%d.%m.%Y') if zariadenie.predlzena_zaruka_do else None,
        'je_v_zaruke': getattr(zariadenie, 'je_v_zaruke', False),
    }
            
    # 🧠 ISAS CORE: Neutrálne načítanie limitov budíkov od aktívneho odvetvia (Polymorfizmus)
    parametre = get_zariadenie_parametre(zariadenie)
    limity = parametre.get_gauge_limits() if (parametre and hasattr(parametre, 'get_gauge_limits')) else {}

    # =====================================================================
    # 🔬 ISAS DIAGNOSTIC ENGINE: Automatizovaný audit (F-Plyny, VTZ, ISO)
    # =====================================================================
    legislativa = []
    diagnostika = []
    dnes = date.today()

    # 🛡️ ISAS CORE: Bezpečný zápis do Čiernej skrinky (Zero-Spam ochrana s ochranou voči duplicitám)
    def auto_alarm(kod, popis, zavaznost):
        try:
            # Ak už existuje otvorený (nevyriešený) alarm tohto typu, nezapisujeme duplikát
            if not AlarmLog.objects.filter(zariadenie=zariadenie, kod_chyby=kod, vyriesene=False).exists():
                AlarmLog.objects.create(
                    zariadenie=zariadenie, 
                    zakaznik=zariadenie.zakaznik,
                    zavaznost=zavaznost, 
                    kod_chyby=kod, 
                    popis=popis
                )
        except Exception as e_alarm:
            pass # Tichý fail: Zlyhanie zápisu do logu nesmie zhodiť live monitoring dashboard

    try:
        # 1. & 2. ZÁKONNÁ LEGISLATÍVA (VTZ A F-PLYNY) cez unifikovaný Compliance Engine s bezpečným mapovaním kľúčov
        compliance_polozky = get_zariadenie_compliance_audit(zariadenie, referencny_datum=dnes)
        for c_item in compliance_polozky:
            c_farba = c_item.get('farba')
            if not c_farba:
                c_stav = c_item.get('stav', '')
                if c_stav in ['PREPADNUTA', 'CHYBA_KONTROLA', 'CRITICAL']:
                    c_farba = 'red'
                elif c_stav in ['BLIZI_SA', 'WARNING']:
                    c_farba = 'orange'
                else:
                    c_farba = 'emerald'

            c_hodnota = c_item.get('hodnota_ui') or c_item.get('hodnota')
            if not c_hodnota and c_item.get('stav') == 'CHYBA_KONTROLA':
                c_hodnota = 'Chýba záznam'

            legislativa.append({
                'nazov': c_item.get('nazov', 'Odborná prehliadka'),
                'hodnota': c_hodnota or 'Bez záznamu',
                'farba': c_farba,
                'ikona': c_item.get('ikona', '⚖️'),
                'popis': c_item.get('upozornenie_text') or c_item.get('popis', ''),
                'termin': c_item.get('termin_str') or (c_item.get('termin').strftime('%d.%m.%Y') if c_item.get('termin') else '')
            })

        # 3. Live diagnostika telemetrie (100% dynamicky delegovaná na odvetvový modul)
        live_diag = get_zariadenie_live_diagnostika(zariadenie, posledny_zaznam=posledny_zaznam)
        for d_item in live_diag:
            diagnostika.append({
                'nazov': d_item['nazov'],
                'hodnota': d_item['hodnota'],
                'farba': d_item['farba'],
                'ikona': d_item['ikona']
            })
            if d_item.get('alarm_kod') and d_item.get('alarm_popis') and d_item.get('alarm_zavaznost'):
                auto_alarm(d_item['alarm_kod'], d_item['alarm_popis'], d_item['alarm_zavaznost'])

    except Exception as e:
        pass # Tichý fail, aby nezamrzol dashboard kvôli chybe vo výpočte

    response_data = {
        'status': 'online' if is_online else 'offline',
        'servisny_rezim': zariadenie.v_servisnom_rezime,
        'strata_eur': strata_eur,
        'mena': getattr(FiremnaIdentita.objects.first(), 'predvolena_mena', None) or get_firemna_mena(),
        'co2_tony': co2_tony,
        'posledna_aktualizacia': posledny_zaznam.casova_peciatka.strftime('%d.%m.%Y %H:%M:%S') if posledny_zaznam else None,
        'analog': posledny_zaznam.analogove_hodnoty if posledny_zaznam else {},
        'digital': posledny_zaznam.digitalne_stavy if posledny_zaznam else {},
        'alarmy': alarmy_data,
        'servisy': servisy_data,
        'predikcia': predikcia_data,
        'identita': identita_data,
        'efektivita': efektivita,
        'motohodiny': zariadenie.aktualne_motohodiny or 0,
        'meter_label': zariadenie.meter_label,
        'meter_unit': zariadenie.meter_unit,
        'meter_load_label': zariadenie.meter_load_label,
        'meter_load_unit': get_industry_term('meter_load_unit', zariadenie.meter_unit),
        'graf_data': graf_data,
        'limity': limity,
        'jednotky': jednotky_mapa,
        'ovladanie': ovladanie_mapa if je_personal else {},
        'legislativa': legislativa,
        'diagnostika': diagnostika,
        'kpi_strata_label': get_industry_term('kpi_strata_label', 'Odhadovaná ročná finančná strata'),
        'kpi_strata_tooltip': get_industry_term('kpi_strata_tooltip', 'Odhadovaná finančná strata z neefektívnej prevádzky.'),
    }

    return JsonResponse(response_data)

@staff_member_required
def modbus_konfigurator_view(request):
    """🎨 ISAS CORE: VIVID SPA pre správu Modbus registrov."""
    # 1. Zákazníci s aktívnym IoT
    zakaznici = Zakaznik.objects.filter(servisovane_zariadenia__iot_je_aktivny=True).distinct()
    
    # 2. Prednačítanie LEN aktívnych IoT zariadení pre bleskový JS kaskádový filter
    iot_zariadenia = ServisovaneZariadenie.objects.select_related('vyrobca').filter(iot_je_aktivny=True)
    zariadenia_map = {}
    
    for z in iot_zariadenia:
        # 🚀 AUTOMATICKÁ POISTKA: Ak kompresor nemá API kľúč, vygenerujeme ho bezpečne cez model
        if not z.iot_api_kluc:
            z.save() # Model ServisovaneZariadenie.save() sa postará o bezpečné vygenerovanie token_urlsafe(32)

        zid = str(z.zakaznik_id)
        if zid not in zariadenia_map:
            zariadenia_map[zid] = []
                
        # 🧠 ISAS CORE: Precízne vyskladanie názvu pre technika (Výrobca + Model + Pozícia + SN)
        vyrobca_str = str(z.vyrobca) if z.vyrobca else "Neznámy výrobca"
        model_str = z.model_zariadenia if z.model_zariadenia else "Neznámy model"
        pozicia_str = f" [{z.nazov}]" if z.nazov else ""
        
        plny_nazov = f"⚙️ {vyrobca_str} {model_str}{pozicia_str} (SN: {z.vyrobne_cislo})"
        
        zariadenia_map[zid].append({
            'id': z.id,
            'text': plny_nazov,
            'kategoria': z.get_kategoria_display() if z.kategoria else 'Nezaradené',
            'api_kluc': z.iot_api_kluc,
            'protokol': z.get_iot_protokol_display() if z.iot_protokol else 'Nenastavený',
            'ip_adresa': z.iot_ip_adresa if z.iot_ip_adresa else 'Neznáma IP',
            'posledny_zapis': z.posledna_aktualizacia_stavu.strftime('%d.%m.%Y %H:%M:%S') if z.posledna_aktualizacia_stavu else 'Nikdy'
        })

    context = {
        'zakaznici': zakaznici,
        'zariadenia_map': zariadenia_map
    }

    return render(request, 'iot/modbus_konfigurator.html', context)

@staff_member_required
def api_iot_get_registers(request, zariadenie_id):
    """Vráti registre pre vybrané zariadenie."""
    registre = SignalDictionary.objects.filter(zariadenie_id=zariadenie_id).values(
        'id', 'register_adresa', 'nazov_signalu', 'typ_signalu', 'prava_pristupu', 'merna_jednotka', 'nasobic'
    )
    return JsonResponse({'registre': list(registre)})

@staff_member_required
@require_POST
def api_iot_save_register(request):
    """Vytvorí alebo upraví register cez AJAX (Blbuvzdorné)."""
    try:
        data = json.loads(request.body)
        if not isinstance(data, dict):
            return JsonResponse({'error': 'Payload musí byť JSON objekt.'}, status=400)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Neplatný JSON formát.'}, status=400)
        
    try:
        zariadenie_id = data.get('zariadenie_id')
        register_adresa = data.get('register_adresa')
        
        if not zariadenie_id or not register_adresa:
            return JsonResponse({'error': 'Zariadenie a Adresa sú povinné.'}, status=400)

        # Ak existuje ID záznamu, aktualizujeme podľa ID (umožňuje zmenu adresy). Inak hľadáme/vytvárame podľa adresy.
        reg_id = data.get('id')
        raw_nasobic = data.get('nasobic')
        try:
            nasobic_dec = Decimal(str(raw_nasobic).replace(',', '.')).quantize(Decimal('0.0001')) if raw_nasobic not in [None, ''] else Decimal('1.0000')
        except (InvalidOperation, TypeError, ValueError):
            nasobic_dec = Decimal('1.0000')
            
        prava_raw = str(data.get('prava_pristupu', 'R')).strip().upper()
        prava_valid = prava_raw if prava_raw in ['R', 'RW'] else 'R'

        defaults_data = {
            'register_adresa': register_adresa,
            'nazov_signalu': data.get('nazov_signalu'),
            'typ_signalu': data.get('typ_signalu', 'ANALOG') if data.get('typ_signalu') in ['ANALOG', 'DIGITAL'] else 'ANALOG',
            'prava_pristupu': prava_valid,
            'merna_jednotka': data.get('merna_jednotka', ''),
            'nasobic': nasobic_dec
        }
        
        if reg_id:
            reg = SignalDictionary.objects.get(id=reg_id, zariadenie_id=zariadenie_id)
            for key, val in defaults_data.items():
                setattr(reg, key, val)
            reg.save()
            created = False
        else:
            reg, created = SignalDictionary.objects.update_or_create(
                zariadenie_id=zariadenie_id,
                register_adresa=register_adresa,
                defaults=defaults_data
            )

        return JsonResponse({'status': 'success', 'id': reg.id, 'created': created})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)

@staff_member_required
@require_POST
def api_iot_save_registers_bulk(request):
    """
    🧠 ISAS CORE: Atomický hromadný zápis registrov (Eliminácia N+1 HTTP požiadaviek).
    """
    try:
        payload = json.loads(request.body)
        if not isinstance(payload, dict):
            return JsonResponse({'error': 'Payload musí byť JSON objekt.'}, status=400)
        zariadenie_id = payload.get('zariadenie_id')
        registre = payload.get('registre', [])
        if not zariadenie_id or not isinstance(registre, list):
            return JsonResponse({'error': 'Neplatné zariadenie alebo zoznam registrov.'}, status=400)

        saved_items = []
        with transaction.atomic():
            for reg_data in registre:
                reg_id = reg_data.get('id')
                reg_adresa = reg_data.get('register_adresa')
                if not reg_adresa or not reg_data.get('nazov_signalu'):
                    continue

                raw_nasobic = reg_data.get('nasobic')
                try:
                    nasobic_dec = Decimal(str(raw_nasobic).replace(',', '.')).quantize(Decimal('0.0001')) if raw_nasobic not in [None, ''] else Decimal('1.0000')
                except (InvalidOperation, TypeError, ValueError):
                    nasobic_dec = Decimal('1.0000')

                prava_raw = str(reg_data.get('prava_pristupu', 'R')).strip().upper()
                prava_valid = prava_raw if prava_raw in ['R', 'RW'] else 'R'

                defaults_data = {
                    'register_adresa': reg_adresa,
                    'nazov_signalu': reg_data.get('nazov_signalu'),
                    'typ_signalu': reg_data.get('typ_signalu', 'ANALOG') if reg_data.get('typ_signalu') in ['ANALOG', 'DIGITAL'] else 'ANALOG',
                    'prava_pristupu': prava_valid,
                    'merna_jednotka': reg_data.get('merna_jednotka', ''),
                    'nasobic': nasobic_dec
                }

                if reg_id:
                    reg = SignalDictionary.objects.filter(id=reg_id, zariadenie_id=zariadenie_id).first()
                    if reg:
                        for k, v in defaults_data.items():
                            setattr(reg, k, v)
                        reg.save()
                    else:
                        reg = SignalDictionary.objects.create(zariadenie_id=zariadenie_id, **defaults_data)
                else:
                    reg, _ = SignalDictionary.objects.update_or_create(
                        zariadenie_id=zariadenie_id,
                        register_adresa=reg_adresa,
                        defaults=defaults_data
                    )
                saved_items.append({'id': reg.id, 'register_adresa': reg.register_adresa})

        return JsonResponse({'status': 'success', 'saved': saved_items, 'count': len(saved_items)})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)

@staff_member_required
@require_POST
def api_iot_delete_register(request, register_id):
    """Zmaže register cez AJAX."""
    SignalDictionary.objects.filter(id=register_id).delete()
    return JsonResponse({'status': 'success'})

@staff_member_required
@require_POST
def api_iot_clone_registers(request):
    """🧠 ISAS CORE: Hromadné skopírovanie registrov z iného stroja."""
    try:
        data = json.loads(request.body)
        if not isinstance(data, dict):
            return JsonResponse({'error': 'Payload musí byť JSON objekt.'}, status=400)
        source_id = data.get('source_id')
        target_id = data.get('target_id')
        
        if not source_id or not target_id:
            return JsonResponse({'error': 'Chýba zdrojové alebo cieľové zariadenie.'}, status=400)
            
        if str(source_id) == str(target_id):
            return JsonResponse({'error': 'Zdrojové a cieľové zariadenie nemôžu byť rovnaké.'}, status=400)

        if not ServisovaneZariadenie.objects.filter(id=target_id).exists():
            return JsonResponse({'error': 'Cieľové zariadenie neexistuje.'}, status=404)

        source_regs = SignalDictionary.objects.filter(zariadenie_id=source_id)
        if not source_regs.exists():
            return JsonResponse({'error': 'Zdrojové zariadenie nemá žiadne registre.'}, status=400)
            
        count = 0
        with transaction.atomic():
            for reg in source_regs:
                _, created = SignalDictionary.objects.update_or_create(
                    zariadenie_id=target_id,
                    register_adresa=reg.register_adresa,
                    defaults={
                        'nazov_signalu': reg.nazov_signalu,
                        'typ_signalu': reg.typ_signalu,
                        'prava_pristupu': reg.prava_pristupu,
                        'merna_jednotka': reg.merna_jednotka,
                        'nasobic': reg.nasobic
                    }
                )
                if created: 
                    count += 1
                
        return JsonResponse({'status': 'success', 'cloned_count': count})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)

@staff_member_required
def api_iot_latest_telemetry(request, zariadenie_id):
    """🧠 ISAS CORE: Vráti posledný známy stav senzorov pre Live Test v konfigurátore."""
    latest = TelemetryHistory.objects.filter(zariadenie_id=zariadenie_id).order_by('-casova_peciatka').first()
    
    if not latest:
        return JsonResponse({'data': {}})
    
    # Spojíme analógové a digitálne dáta do jedného slovníka pre jednoduché hľadanie v JS (Ochrana pred NoneType)
    combined_data = {**(latest.analogove_hodnoty or {}), **(latest.digitalne_stavy or {})}
    
    cas_str = latest.casova_peciatka.strftime('%H:%M:%S') if latest.casova_peciatka else "—"
    return JsonResponse({
        'data': combined_data, 
        'cas': cas_str
    })

@staff_member_required
@require_POST
def api_iot_write_parameter(request, zariadenie_id):
    """
    🚀 ISAS CORE: Endpoint pre odoslanie príkazu na zápis do stroja.
    """
    try:
        data = json.loads(request.body)
        if not isinstance(data, dict):
            return JsonResponse({'error': 'Payload musí byť JSON objekt.'}, status=400)
        adresa = data.get('register_adresa')
        nova_hodnota = data.get('hodnota')

        if adresa is None or nova_hodnota is None:
            return JsonResponse({'error': 'Chýba adresa alebo hodnota.'}, status=400)

        zariadenie = ServisovaneZariadenie.objects.filter(id=zariadenie_id).first()
        if not zariadenie:
            return JsonResponse({'error': 'Zariadenie neexistuje.'}, status=404)

        signal = SignalDictionary.objects.filter(zariadenie=zariadenie, register_adresa=adresa).first()
        if not signal:
            return JsonResponse({'error': 'Register neexistuje.'}, status=404)

        if signal.prava_pristupu != 'RW':
            return JsonResponse({'error': 'Tento parameter je chránený proti zápisu (Read-Only).'}, status=403)

        try:
            execute_modbus_write_task.delay(
                zariadenie_id=zariadenie.id, 
                adresa=adresa, 
                nova_hodnota=nova_hodnota, 
                user_id=request.user.id
            )
        except Exception as exc:
            logger.warning(f"Zlyhalo zaradenie execute_modbus_write_task do Celery: {exc}")
            return JsonResponse({'error': 'Úlohový server je momentálne nedostupný. Príkaz nebol zaradený.'}, status=503)

        return JsonResponse({'status': 'queued', 'message': 'Príkaz odoslaný.'}, status=200)

    except json.JSONDecodeError:
        return JsonResponse({'error': 'Neplatný JSON formát.'}, status=400)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

@staff_member_required
@require_POST
def api_iot_toggle_service_mode(request, zariadenie_id):
    """
    🚀 ISAS BIZNIS: Rýchle prepnutie servisného režimu priamo z Dashboardu.
    Dostupné iba pre zamestnancov (technikov).
    """
    zariadenie = ServisovaneZariadenie.objects.filter(id=zariadenie_id).first()
    if not zariadenie:
        return JsonResponse({'status': 'error', 'message': 'Zariadenie nebolo nájdené.'}, status=404)
    
    # Prepnutie stavu (True -> False, False -> True)
    zariadenie.v_servisnom_rezime = not zariadenie.v_servisnom_rezime
    zariadenie.save(update_fields=['v_servisnom_rezime'])
    
    return JsonResponse({
        'status': 'success', 
        'v_servisnom_rezime': zariadenie.v_servisnom_rezime
    })

@staff_member_required
@require_POST
def api_iot_resolve_alarm(request, alarm_id):
    """
    🚀 ISAS BIZNIS: Označenie alarmu ako vyriešeného priamo z Dashboardu.
    """
    alarm = AlarmLog.objects.filter(id=alarm_id).first()
    if not alarm:
        return JsonResponse({'error': 'Alarm neexistuje.'}, status=404)
        
    alarm.vyriesene = True
    alarm.save(update_fields=['vyriesene'])
    
    return JsonResponse({'status': 'success'})