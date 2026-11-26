import hashlib
import logging
import math
import re
import time
import uuid
import holidays

from datetime import date, datetime, time as dt_time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import requests
from django.apps import apps
from django.conf import settings
from django.urls import reverse
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.files.base import ContentFile
from django.core.paginator import Paginator
from django.core.validators import validate_email
from django.db import DataError, models, transaction
from django.db.models import Avg, Count, Exists, OuterRef, Prefetch, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.generic import DetailView, ListView, TemplateView

from Evidencia.models import (
    AlarmLog,
    TelemetryHistory, SignalDictionary,
    CenovyKatalog,
    DatabazaVyrobcov,
    Filialka,
    Kandidat,
    Nepritomnost,
    PlanovanyServisZariadenia,
    PracovnaPozicia,
    PrijataObjednavka,
    ServisnyPredpis,
    ServisnyZaznam,
    ServisovaneZariadenie,
    TypServisuPrace,
    Uloha,
    VystavenaFaktura,
    Zakaznik,
    Zamestnanec,
)
from Evidencia.industry_loader import (
    FPLYNY_CHLADIVA,
    HAS_CAPEX_CALCULATOR,
    HAS_FPLYNY_AUDIT,
    HAS_HEAT_RECOVERY,
    HAS_VIBRO_DIAGNOSTICS,
    HAS_VTZ_INSPECTION,
    HUB_CONFIG,
    INDUSTRY_TERMS,
    MEDIA_QUALITY_APPS,
    TCO_PROFILES,
    ZARIADENIA_COLOR_MAP,
    get_active_parameter_reverse_names,
    get_all_industry_terms,
    get_industry_term,
    is_esg_audit_supported,
    get_zariadenie_parametre
)
from Evidencia.utils import generate_pdf_from_html
from Evidencia.utils_compliance import (
    add_calendar_interval,
    get_compliance_config,
    get_legal_disclaimer,
    get_zariadenie_compliance_audit,
    get_zariadenie_live_diagnostika,
    to_date,
    get_iso_pre_objekt
)
from ISASaplikacia.models import FiremnaIdentita, GlobalneNastavenia, StatLokalizacia
from Evidencia.utils_helper import get_client_ip, get_firemna_mena
from WebBuilder.utils_helper import get_active_web_blocks, get_b2b_client_and_discount, get_eshop_models, get_nav_katalog_menu, get_web_nav_flags, get_work_shifts
from .models import Clanok, WebBlok, WebFAQ, WebNastavenia

logger = logging.getLogger(__name__)

def _is_staff_user(user):
    return bool(
        user
        and getattr(user, 'is_authenticated', False)
        and getattr(user, 'is_active', False)
        and getattr(user, 'is_staff', False)
    )

def _resolve_preview_mode(request):
    """Povolí DRAFT / AI náhľad výhradne autentifikovanému aktívnemu personálu (is_staff)."""
    session = getattr(request, 'session', None)
    if not _is_staff_user(getattr(request, 'user', None)):
        if session is not None and 'is_draft_preview' in session:
            session.pop('is_draft_preview', None)
        return False

    preview_param = request.GET.get('preview')
    if session is not None:
        if preview_param == 'ai_draft':
            session['is_draft_preview'] = True
        elif preview_param in ['live', 'exit']:
            session.pop('is_draft_preview', None)
        return bool(session.get('is_draft_preview', False) or (preview_param == 'ai_draft'))
    return preview_param == 'ai_draft'

def _is_valid_email_address(email):
    if not email or len(email) > 254 or any(c in email for c in ('\r', '\n', '\x00')):
        return False
    try:
        validate_email(email)
        return True
    except ValidationError:
        return False

def _rate_limit_hit(cache_key, limit, timeout):
    """Atomický rate limiter odolný voči race condition pri cache.incr."""
    if cache.add(cache_key, 1, timeout=timeout):
        return False
    try:
        return cache.incr(cache_key) > limit
    except ValueError:
        cache.set(cache_key, 1, timeout=timeout)
        return False

def _local_today():
    return timezone.localdate() if getattr(settings, 'USE_TZ', False) else timezone.now().date()

def public_web_view(request):
    is_preview = _resolve_preview_mode(request)
    is_draft = is_preview  # Alias pre 100% spätnú kompatibilitu
    verzia_blokov = 'DRAFT' if is_preview else 'LIVE'
    
    # Identifikácia klienta a jeho zľavy
    b2b_klient, zlava = get_b2b_client_and_discount(request)
    zlava_dec = Decimal(str(zlava or '0.00'))
    
    # Cachujeme podľa výšky zľavy, verzie webu a aktívneho odvetvia
    web_version = cache.get('web_cache_version', 1)
    ind_mod = getattr(settings, 'INDUSTRY_MODULE', 'generic') or 'generic'
    b2b_cache_id = b2b_klient.pk if b2b_klient else 0
    cache_key = f"public_web_context_v{web_version}_ind_{ind_mod}_zlava_{zlava}_b2b_{b2b_cache_id}"
    context = None if is_preview else cache.get(cache_key)
    
    if not context:
        bloky = list(WebBlok.objects.filter(je_aktivny=True, verzia=verzia_blokov).order_by('poradie')) # Evaluácia do RAM
        identita = FiremnaIdentita.objects.first()

        # Ak neexistuje pk=1 ani pk=2, vezmeme prvý dostupný záznam
        nastavenia = WebNastavenia.objects.filter(pk=2 if is_preview else 1).first()
        if not nastavenia:
            nastavenia = WebNastavenia.objects.first()
        if not nastavenia:
            default_titulok = identita.nazov_spolocnosti if (identita and identita.nazov_spolocnosti) else "ISAS CORE WEB"
            nastavenia = WebNastavenia(hlavna_farba="#0284c7", titulok_webu=default_titulok)
        
        if not identita:
            identita = FiremnaIdentita(nazov_spolocnosti="Naša Spoločnosť")
            
        # 🛡️ ISAS ARCHITECT: Načítanie globálnych nastavení pre sadzby a logistiku
        gn = GlobalneNastavenia.objects.first()
        standardna_sadzba = gn.hodinova_sadzba_technik if (gn and gn.hodinova_sadzba_technik and gn.hodinova_sadzba_technik > Decimal('0.00')) else Decimal('0.00')

        # 🚀 Anotované služby so sociálnym dôkazom, hodnotením a ochranou pred únikom interných nákladov (Zero Data Leak)
        sluzby_qs = TypServisuPrace.objects.filter(zobrazit_na_webe=True).exclude(slug__isnull=True).exclude(slug__exact='').annotate(
            hodnotenie_priemer=Avg(
                'servisnyzaznam__hodnotenie_zakaznika',
                filter=Q(servisnyzaznam__zobrazit_na_webe=True, servisnyzaznam__hodnotenie_zakaznika__isnull=False)
            ),
            hodnotenie_pocet=Count(
                'servisnyzaznam__id',
                filter=Q(servisnyzaznam__zobrazit_na_webe=True, servisnyzaznam__hodnotenie_zakaznika__isnull=False),
                distinct=True
            ),
            pocet_realizacii=Count(
                'servisnyzaznam__id',
                filter=Q(servisnyzaznam__dokoncene=True),
                distinct=True
            )
        ).defer('hodinova_sadzba_naklad', 'fixny_naklad').order_by('nazov')

        sluzby = list(sluzby_qs)
        for s in sluzby:
            cena = Decimal('0.00')
            if s.je_fakturovatelna:
                if s.sposob_uctovania == 'HODINA':
                    if s.hodinova_sadzba_predaj and s.hodinova_sadzba_predaj > Decimal('0.00'):
                        cena = s.hodinova_sadzba_predaj
                    elif standardna_sadzba > Decimal('0.00'):
                        cena = standardna_sadzba
                elif s.sposob_uctovania == 'FIX' and s.fixna_cena_predaj and s.fixna_cena_predaj > Decimal('0.00'):
                    cena = s.fixna_cena_predaj
            
            s.povodna_cena = None
            if zlava_dec > Decimal('0.00') and cena > Decimal('0.00'):
                s.povodna_cena = cena.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                s.cena_predaj = (cena * (Decimal('1.00') - (zlava_dec / Decimal('100.00')))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            else:
                s.cena_predaj = cena

            s.orientacna_cena_celkom = None
            if s.je_fakturovatelna and s.sposob_uctovania == 'HODINA' and s.cena_predaj > Decimal('0.00') and s.normohodiny and s.normohodiny > Decimal('0.00'):
                s.orientacna_cena_celkom = (s.cena_predaj * s.normohodiny).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            if s.hodnotenie_priemer is not None:
                s.hodnotenie_priemer_zaokruhlene = round(float(s.hodnotenie_priemer), 1)
        
        znacky = list(DatabazaVyrobcov.objects.filter(zobrazit_na_webe=True).values(
            'id', 'nazov_vyrobcu', 'logo_na_webe', 'typ_vyroby'
        ))

        # Vytiahneme najnovšie referencie vrátane modelu a značky pre B2B sociálny dôkaz (Zero Data Leak)
        referencie = list(ServisnyZaznam.objects.filter(
            zobrazit_na_webe=True,
            dokoncene=True
        ).filter(
            (Q(foto_po__isnull=False) & ~Q(foto_po='')) |
            (Q(foto_pred__isnull=False) & ~Q(foto_pred='')) |
            (Q(video__isnull=False) & ~Q(video=''))
        )
         .exclude(slug__isnull=True).exclude(slug__exact='')
         .select_related('zariadenie', 'zariadenie__vyrobca', 'zakaznik')
         .prefetch_related('typ_servisu')
         .defer(
            'koren_problemu', 'ai_analyza_historie', 'gps_suradnice',
            'podpis_zakaznika', 'podpis_technika', 'meno_podpisujuceho',
            'uzavrety_pdf_dokument'
         )
         .order_by('-vytvorene')[:6])

        # Vytiahneme zamestnancov pre web s fallbackom na rolu
        dnes_datum = timezone.now().date()
        tim_qs = Zamestnanec.objects.filter(je_anonymizovany=False)
        tim = list(tim_qs.filter(zobrazit_na_webe=True).prefetch_related('certifikaty').only(
            'meno', 'priezvisko', 'verejna_rola', 'profilova_fotka', 'datum_nastupu', 'pracovna_rola'
        ).order_by('priezvisko'))

        for clen in tim:
            if clen.datum_nastupu:
                clen.prax_roky = (
                    dnes_datum.year
                    - clen.datum_nastupu.year
                    - ((dnes_datum.month, dnes_datum.day) < (clen.datum_nastupu.month, clen.datum_nastupu.day))
                )
            else:
                clen.prax_roky = 0
        
        # Zero Data Leak: Na verejný web sa nikdy nevypisujú zamestnanci bez explicitného súhlasu zobrazit_na_webe=True

        # 🛒 ISAS E-COMMERCE LITE (Extrémne optimalizovaný DB dopyt & Zero Data Leak)
        eshop_diely = []
        
        # 1. Dynamicky nájdeme výhradne SKLADOVÉ modely v Evidencii (Ochrana pred FieldError na Zamestnancoch)
        povolene_modely = get_eshop_models()
        
        # 2. Bleskový výber položiek (Orezané na úrovni SQL, ukončíme ak máme 4 položky)
        for model in povolene_modely:
            if len(eshop_diely) >= 4:
                break

            ct = ContentType.objects.get_for_model(model)
            
            # 🚀 LIMIT NA 4 POLOŽKY z každej kategórie s defenzívnou kontrolou polí a platného slugu
            model_fields = {f.name for f in model._meta.get_fields()}
            if 'slug' not in model_fields or 'zobrazit_na_webe' not in model_fields:
                continue

            candidate_fields = [
                'id', 'obchodny_nazov', 'slug', 'seriove_cislo', 'katalogove_cislo', 
                'popis_na_webe', 'obrazok_na_webe', 'merna_jednotka', 
                'pocet_na_sklade', 'sadzba_dph'
            ]
            only_fields = [f for f in candidate_fields if f in model_fields]
            if 'vyrobca' in model_fields:
                only_fields.append('vyrobca')

            qs_diely = model.objects.filter(zobrazit_na_webe=True).exclude(slug__isnull=True).exclude(slug__exact='')
            if 'vyrobca' in model_fields:
                qs_diely = qs_diely.select_related('vyrobca')

            diely_na_webe = list(qs_diely.only(*only_fields)[:4])
            
            if not diely_na_webe:
                continue
                
            diely_ids = [d.id for d in diely_na_webe]
            ceny = CenovyKatalog.objects.filter(
                content_type=ct, 
                object_id__in=diely_ids
            ).values('object_id', 'predajna_cena')
            
            ceny_mapa = {c['object_id']: c['predajna_cena'] for c in ceny}
            
            # 🧠 ISAS FIX: Dynamická DPH z globálnych nastavení
            gn_tmp = GlobalneNastavenia.objects.first()
            def_dph = gn_tmp.zakladna_sadzba_dph if gn_tmp and gn_tmp.zakladna_sadzba_dph else GlobalneNastavenia.get_zakladna_dph()

            zlava_dec = Decimal(str(zlava or '0.00'))
            for diel in diely_na_webe:
                raw_zakladna_cena = ceny_mapa.get(diel.id)
                zakladna_cena = Decimal(str(raw_zakladna_cena)) if raw_zakladna_cena is not None else None
                if zakladna_cena is None or zakladna_cena <= Decimal('0.00'):
                    continue 
                
                predajna_cena = zakladna_cena * (Decimal('1.00') - (zlava_dec / Decimal('100.00'))) if zlava_dec > Decimal('0.00') else zakladna_cena
                diel_dph = getattr(diel, 'sadzba_dph', None)
                dph_raw = diel_dph if (diel_dph is not None and str(diel_dph).strip() != '') else (def_dph or '0.00')
                try:
                    sadzba_dph = Decimal(str(dph_raw))
                except (InvalidOperation, TypeError, ValueError):
                    sadzba_dph = Decimal('0.00')
                cena_s_dph = predajna_cena * (Decimal('1.00') + (sadzba_dph / Decimal('100.00')))
                
                img_url = None
                if getattr(diel, 'obrazok_na_webe', None) and diel.obrazok_na_webe.name:
                    img_url = diel.obrazok_na_webe.url
                    
                eshop_diely.append({
                    'nazov': getattr(diel, 'obchodny_nazov', None) or getattr(diel, 'seriove_cislo', '') or get_industry_term('polozka_fallback_nazov', 'Položka'),
                    'obchodny_nazov': getattr(diel, 'obchodny_nazov', ''),
                    'seriove_cislo': getattr(diel, 'seriove_cislo', ''),
                    'slug': getattr(diel, 'slug', ''),
                    'katalogove_cislo': getattr(diel, 'katalogove_cislo', '') or "",
                    'popis': getattr(diel, 'popis_na_webe', '') or '',
                    'popis_na_webe': getattr(diel, 'popis_na_webe', '') or '',
                    'obrazok_url': img_url,
                    'cena_bez_dph': predajna_cena.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
                    'povodna_cena_bez_dph': zakladna_cena.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if zlava_dec > Decimal('0.00') else None,
                    'cena_s_dph': cena_s_dph.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
                    'merna_jednotka': getattr(diel, 'merna_jednotka', ''),
                    'pocet_na_sklade': getattr(diel, 'pocet_na_sklade', 0) or 0,
                    'skladom': (getattr(diel, 'pocet_na_sklade', 0) or 0) > 0,
                    'typ_vyroby': getattr(diel.vyrobca, 'typ_vyroby', '') if getattr(diel, 'vyrobca', None) else ''
                })

                if len(eshop_diely) >= 4:
                    break
        
        eshop_diely = sorted(eshop_diely, key=lambda x: str(x['nazov'] or ''))[:4]

        # 🚜 ISAS RENTAL ENGINE: Agregovaná flotila zhodne s RentalListView (vrátane PK a servisného režimu)
        active_param_rels = get_active_parameter_reverse_names()
        rental_qs = ServisovaneZariadenie.objects.filter(
            je_urcene_na_prenajom=True,
            zobrazit_na_webe_prenajom=True
        ).exclude(slug__isnull=True).exclude(slug__exact='').select_related('vyrobca', 'zakaznik', *active_param_rels).only(
            'nazov', 'model_zariadenia', 'slug', 'kategoria', 'obrazok_na_webe', 'cena_prenajmu_na_den', 'v_prenajme_od_isas',
            'v_servisnom_rezime', 'hmotnost_kg', 'rok_vyroby', 'aktualne_motohodiny', 'motohodiny_v_zatazi', 'iot_je_aktivny', 'zaruka_do', 'predlzena_zaruka_do',
            'bezpecnostny_kluc', 'vyrobca__nazov_vyrobcu', 'zakaznik__Nazov_spolocnosti', 'zakaznik__zobrazit_ako_referenciu', *active_param_rels
        )

        rentals_grouped = {}
        for stroj in rental_qs:
            # Neutrálny kľúč: Výrobca + Model/Názov, pri virtuálnych/cloud zariadeniach bez výrobcu primárne Názov
            if stroj.vyrobca:
                item_nazov = f"{stroj.vyrobca.nazov_vyrobcu} {stroj.model_zariadenia or stroj.nazov or ''}".strip()
            else:
                item_nazov = stroj.nazov or stroj.model_zariadenia or get_industry_term('zariadenie_fallback_nazov', 'Zariadenie')
            group_key = item_nazov

            if group_key not in rentals_grouped:
                zakladna_cena = stroj.cena_prenajmu_na_den
                zlava_dec = Decimal(str(zlava or '0.00'))
                cena_po_zlave = zakladna_cena * (Decimal('1.00') - (zlava_dec / Decimal('100.00'))) if zakladna_cena and zlava_dec > Decimal('0.00') else zakladna_cena
                
                rentals_grouped[group_key] = {
                    'pk': stroj.pk,
                    'nazov': group_key,
                    'slug': getattr(stroj, 'slug', ''),
                    'kategoria': stroj.get_kategoria_display(),
                    'obrazok_url': stroj.obrazok_na_webe.url if getattr(stroj, 'obrazok_na_webe', None) and stroj.obrazok_na_webe.name else None,
                    'cena_na_den': round(cena_po_zlave, 2) if cena_po_zlave else None,
                    'povodna_cena_na_den': round(zakladna_cena, 2) if zakladna_cena and zlava_dec > Decimal('0.00') else None,
                    'celkovy_pocet': 0,
                    'dostupne_pocet': 0,
                    'v_servise_pocet': 0,
                    'hmotnost_kg': stroj.hmotnost_kg,
                    'rok_vyroby': stroj.rok_vyroby,
                    'meter_display': stroj.meter_display,
                    'meter_load_display': stroj.meter_load_display,
                    'iot_je_aktivny': stroj.iot_je_aktivny,
                    'je_v_zaruke': stroj.je_v_zaruke,
                    'zaruka_do': stroj.zaruka_do,
                    'predlzena_zaruka_do': stroj.predlzena_zaruka_do,
                    'bezpecnostny_kluc': str(stroj.bezpecnostny_kluc) if stroj.bezpecnostny_kluc else None,
                    'ui_parametre': stroj.ui_parametre[:3],
                    'referencie': set()
                }

            rentals_grouped[group_key]['celkovy_pocet'] += 1

            if not stroj.v_prenajme_od_isas and not stroj.v_servisnom_rezime:
                rentals_grouped[group_key]['dostupne_pocet'] += 1
                rentals_grouped[group_key]['slug'] = getattr(stroj, 'slug', '')
                rentals_grouped[group_key]['pk'] = stroj.pk
                if stroj.bezpecnostny_kluc:
                    rentals_grouped[group_key]['bezpecnostny_kluc'] = str(stroj.bezpecnostny_kluc)
            elif stroj.v_servisnom_rezime and not stroj.v_prenajme_od_isas:
                rentals_grouped[group_key]['v_servise_pocet'] += 1
            else:
                # 🛡️ BEZPEČNOSŤ (NDA): Referenciu ukážeme IBA ak má zákazník explicitný súhlas
                if stroj.zakaznik and getattr(stroj.zakaznik, 'zobrazit_ako_referenciu', False):
                    rentals_grouped[group_key]['referencie'].add(stroj.zakaznik.Nazov_spolocnosti)

        # Prevod do listu, zoradenie abecedne a obmedzenie na Teaser
        rental_flotila = sorted(list(rentals_grouped.values()), key=lambda x: x['nazov'])[:3] # FÁZA 3: Iba Teaser (3 položky)
        # Prevod setov na listy (Django šablóny nemajú radi sety)
        for r in rental_flotila:
            r['referencie'] = list(r['referencie'])

        stat_zakaznici = Zakaznik.objects.filter(je_interny_profil=False).count()
        stat_zariadenia = ServisovaneZariadenie.objects.count()
        stat_servisy = ServisnyZaznam.objects.filter(dokoncene=True).count()
        
        # ⭐ ISAS NPS ENGINE: Výpočet reálnej spokojnosti zákazníkov
        nps_data = ServisnyZaznam.objects.filter(
            dokoncene=True, 
            hodnotenie_zakaznika__isnull=False
        ).aggregate(
            priemer=Avg('hodnotenie_zakaznika'),
            pocet=Count('id')
        )
        nps_priemer = round(Decimal(str(nps_data['priemer'])), 1) if nps_data.get('priemer') is not None else Decimal('0.0')
        nps_pocet = nps_data.get('pocet') or 0
        
        # 🛡️ ISAS B2B PORTAL & SLA ENGINE: Globálne nastavenia, flotila klienta a strediská
        gn = GlobalneNastavenia.objects.first()

        klient_zariadenia = list(
            ServisovaneZariadenie.objects.filter(zakaznik=b2b_klient)
            .select_related('vyrobca')
            .only('id', 'model_zariadenia', 'vyrobne_cislo', 'nazov', 'vyrobca__nazov_vyrobcu')
            .order_by('nazov', 'model_zariadenia')
        ) if b2b_klient else []

        servisne_strediska = list(
            Filialka.objects.filter(je_aktivna=True, typ_skladu='BUDOVA')
            .only('nazov', 'kod_pobocky', 'adresa', 'telefon', 'email')
            .order_by('nazov')
        )
        ma_hlavnu_prevadzku = bool(identita and (getattr(identita, 'adresa_prevadzky', None) or getattr(identita, 'sidlo_mesto', None)))
        pocet_stredisk_celkom = len(servisne_strediska) + (1 if ma_hlavnu_prevadzku else 0)
        
        today = timezone.now().date()
        pozicie_qs = PracovnaPozicia.objects.all() if is_preview else PracovnaPozicia.objects.filter(
            je_aktivna=True
        ).filter(
            Q(platnost_do__isnull=True) | Q(platnost_do__gte=today)
        )
        pozicie = list(pozicie_qs.exclude(slug__isnull=True).exclude(slug__exact='').order_by('poradie', '-id'))
        for p in pozicie:
            raw_mzda = str(getattr(p, 'mzda', '') or '').strip()
            digits = re.sub(r'[^\d]', '', raw_mzda)
            p.mzda_numeric = digits if digits else None

        # ❓ ISAS FAQ ENGINE: Natiahnutie aktívnych klientskych otázok (bez náborových otázok Kariéry)
        faqs = list(WebFAQ.objects.filter(je_aktivna=True).exclude(kategoria='KARIERA').order_by('kategoria', 'poradie', '-id'))

        # 📰 ISAS BLOG ENGINE: Natiahnutie najnovších 3 článkov pre Teaser (V AI preview rešpektujeme koncepty v symbióze s MPA)
        dnes = timezone.now().date()
        clanky_qs = Clanok.objects.all() if is_preview else Clanok.objects.filter(je_aktivny=True, datum_vydania__lte=dnes)
        clanky = list(clanky_qs.exclude(slug__isnull=True).exclude(slug__exact='').order_by('-datum_vydania', '-id')[:3])

        # 🧠 DYNAMICKÝ ENGINEERING HUB: Zlúčenie technologických koeficientov z odvetvovej konfigurácie a compliance
        active_hub_config = dict(HUB_CONFIG)
        krajina_iso = FiremnaIdentita.get_aktivna_krajina_iso() if hasattr(FiremnaIdentita, 'get_aktivna_krajina_iso') else None
        compliance_cfg = get_compliance_config(iso=krajina_iso)
        if compliance_cfg:
            for k, v in compliance_cfg.items():
                if isinstance(v, (int, float, str, bool, dict, list)):
                    active_hub_config[k] = v

        if b2b_klient:
            if getattr(b2b_klient, 'cena_za_kwh_eur', None) and b2b_klient.cena_za_kwh_eur > Decimal('0.000'):
                active_hub_config['esg_default_price'] = str(b2b_klient.cena_za_kwh_eur)
            if getattr(b2b_klient, 'emisny_faktor_co2', None) and b2b_klient.emisny_faktor_co2 > Decimal('0.0000'):
                active_hub_config['esg_co2_factor'] = str(b2b_klient.emisny_faktor_co2)

        active_hub_config['mena'] = getattr(identita, 'predvolena_mena', None) or get_firemna_mena()

        has_esg_enabled = bool(is_esg_audit_supported() and active_hub_config.get('esg_idle_factor') is not None)
        has_rekuperacia_enabled = bool(HAS_HEAT_RECOVERY and active_hub_config.get('rekuperacia_factor'))
        has_tco_enabled = bool(TCO_PROFILES and isinstance(TCO_PROFILES, dict))
        has_vtz_enabled = bool((HAS_VTZ_INSPECTION or active_hub_config.get('revizia_perioda_1_roky') or active_hub_config.get('revizia_perioda_2_roky')) and (active_hub_config.get('revizia_perioda_1_roky') or active_hub_config.get('revizia_perioda_2_roky')))
        has_fplyny_enabled = bool(HAS_FPLYNY_AUDIT and FPLYNY_CHLADIVA and isinstance(FPLYNY_CHLADIVA, list))
        has_media_quality_enabled = bool(MEDIA_QUALITY_APPS and isinstance(MEDIA_QUALITY_APPS, list))
        has_vibro_enabled = bool(HAS_VIBRO_DIAGNOSTICS and active_hub_config.get('vibro_limits') and active_hub_config.get('vibro_zones'))
        has_capex_enabled = bool(HAS_CAPEX_CALCULATOR and active_hub_config.get('capex_years'))
        
        try:
            has_iot_demo_enabled = float(get_industry_term('gauge_1_base', 0) or 0) > 0
        except (ValueError, TypeError):
            has_iot_demo_enabled = False

        esg_card_count = (1 if has_esg_enabled else 0) + (1 if has_rekuperacia_enabled else 0) + (1 if has_tco_enabled else 0)
        law_card_count = (1 if has_vtz_enabled else 0) + (1 if has_fplyny_enabled else 0)
        diag_card_count = (1 if has_media_quality_enabled else 0) + (1 if has_vibro_enabled else 0) + (1 if has_capex_enabled else 0)
        active_hub_tab = 'esg' if esg_card_count > 0 else ('law' if law_card_count > 0 else ('diag' if diag_card_count > 0 else None))

        # 💰 BIZNIS VÝPOČET: Sadzby dopravy s DPH pre dopytový košík zhodne s WebBaseMixin (Defenzívne ošetrené)
        zakladna_dph = GlobalneNastavenia.get_zakladna_dph() if hasattr(GlobalneNastavenia, 'get_zakladna_dph') else Decimal('0.00')
        dph_koeficient = Decimal('1.00') + (zakladna_dph / Decimal('100.00')) if getattr(identita, 'je_platca_dph', False) else Decimal('1.00')
        
        km_sadzba = getattr(gn, 'sadzba_za_km_zakaznik', None)
        sadzba_za_km_s_dph = (km_sadzba * dph_koeficient).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if (km_sadzba is not None and km_sadzba > Decimal('0.00')) else None
        
        cas_sadzba = getattr(gn, 'sadzba_za_cas_na_ceste', None)
        sadzba_za_cas_na_ceste_s_dph = (cas_sadzba * dph_koeficient).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if (cas_sadzba is not None and cas_sadzba > Decimal('0.00')) else None

        aktivne_bloky_set = {getattr(b, 'typ', '') for b in bloky}

        context = {
            'nastavenia': nastavenia,
            'identita': identita,
            'sla_telemetria': None,
            'bloky': bloky,
            'sluzby': sluzby,
            'znacky': znacky,
            'referencie': referencie,
            'tim': tim,
            'eshop_diely': eshop_diely,
            'rental_flotila': rental_flotila,
            'stat_zakaznici': stat_zakaznici,
            'stat_zariadenia': stat_zariadenia,
            'stat_servisy': stat_servisy,
            'nps_priemer': nps_priemer,
            'nps_pocet': nps_pocet,
            'is_preview': is_preview,
            'is_b2b': zlava_dec > Decimal('0.00') or bool(b2b_klient),
            'b2b_klient': b2b_klient,
            'b2b_zlava': zlava_dec,
            'has_oem_parts': any(d.get('typ_vyroby') == 'O' for d in eshop_diely),
            'pocet_servisnych_stredisk': pocet_stredisk_celkom,
            'ma_hlavnu_prevadzku': ma_hlavnu_prevadzku,
            'pozicie': pozicie,
            'faqs': faqs,
            'clanky': clanky,
            'industry_terms': get_all_industry_terms(),
            'mena': getattr(identita, 'predvolena_mena', None) or get_firemna_mena(),
            'has_esg_audit': has_esg_enabled,
            'has_fplyny': has_fplyny_enabled,
            'has_rekuperacia': has_rekuperacia_enabled,
            'has_vtz': has_vtz_enabled,
            'has_vibro': has_vibro_enabled,
            'has_capex': has_capex_enabled,
            'has_tco': has_tco_enabled,
            'has_media_quality': has_media_quality_enabled,
            'has_iot_demo': has_iot_demo_enabled,
            'has_booking': any(getattr(b, 'typ', '') == 'BOOKING' for b in bloky) and bool(sluzby),
            'esg_card_count': esg_card_count,
            'law_card_count': law_card_count,
            'diag_card_count': diag_card_count,
            'active_hub_tab': active_hub_tab,
            'fplyn_chladiva': FPLYNY_CHLADIVA if has_fplyny_enabled else [],
            'hub_config': active_hub_config,
            'tco_profiles': TCO_PROFILES if has_tco_enabled else {},
            'media_quality_apps': MEDIA_QUALITY_APPS if has_media_quality_enabled else [],
            'MEDIA_URL': settings.MEDIA_URL,
            'klient_zariadenia': klient_zariadenia,
            'globalne_nastavenia': gn,
            'servisne_strediska': servisne_strediska,
            'nav_katalog_menu': get_nav_katalog_menu(cache_ver=web_version, is_preview=is_preview, aktivne_bloky=aktivne_bloky_set),
            'nav_flags': get_web_nav_flags(cache_ver=web_version, is_preview=is_preview, aktivne_bloky=aktivne_bloky_set),
            'sadzba_za_km_s_dph': sadzba_za_km_s_dph,
            'sadzba_za_cas_na_ceste_s_dph': sadzba_za_cas_na_ceste_s_dph,
            'cart_env': {
                'mena': getattr(identita, 'predvolena_mena', None) or get_firemna_mena(),
                'b2bZlava': float(zlava_dec),
                'vyzadujeGatePass': bool(b2b_klient and getattr(b2b_klient, 'vyzaduje_povolenie_vstupu', False)),
                'dispecingInfo': f"Náš dispečing požiadavku spracuje v prevádzkovom čase ({gn.pracovny_cas_od.strftime('%H:%M')} – {gn.pracovny_cas_do.strftime('%H:%M')}{'' if getattr(gn, 'pracovat_vikendy', False) else ', v pracovné dni'})." if (gn and getattr(gn, 'pracovny_cas_od', None) and getattr(gn, 'pracovny_cas_do', None)) else ""
            },
        }

    # Uložíme do Redisu na 15 minút (900 sekúnd), ale IBA ak to nie je draft!
    if not is_preview:
        cache.set(cache_key, context, 900)

    # 🧠 ISAS LIVE SLA: Aktuálny výpočet mimo 15-minútovú cache (časová brána beží vždy naživo)
    sla_telemetria = None
    ctx_nastavenia = context.get('nastavenia')
    ctx_gn = context.get('globalne_nastavenia')
    if ctx_nastavenia and getattr(ctx_nastavenia, 'zobrazit_live_sla', False) and ctx_gn:
        teraz = timezone.now()
        dnes = teraz.date()
        cas_teraz = teraz.time()
        je_vikend = teraz.weekday() in (5, 6)
        je_mimo_prevadzky = False

        if je_vikend and not getattr(ctx_gn, 'pracovat_vikendy', False):
            je_mimo_prevadzky = True
        elif not getattr(ctx_gn, 'pracovat_sviatky', False):
            sviatok_cache_key = f"isas_sla_sviatok_{dnes.isoformat()}"
            je_sviatok = cache.get(sviatok_cache_key)
            if je_sviatok is None:
                je_sviatok = dnes in get_holidays_for_location(dnes)
                cache.set(sviatok_cache_key, je_sviatok, 3600)
            if je_sviatok:
                je_mimo_prevadzky = True

        if not je_mimo_prevadzky and getattr(ctx_gn, 'pracovny_cas_od', None) and getattr(ctx_gn, 'pracovny_cas_do', None):
            if cas_teraz < ctx_gn.pracovny_cas_od or cas_teraz > ctx_gn.pracovny_cas_do:
                je_mimo_prevadzky = True

        if je_mimo_prevadzky:
            sla_telemetria = {
                'status': 'MIMO_PREVADZKY',
                'sprava': 'Mimo prevádzkových hodín dispečingu (príjem dopytov online 24/7)'
            }
        else:
            zataz_cache_key = f"isas_sla_zataz_{dnes.isoformat()}"
            zataz_na_technika = cache.get(zataz_cache_key)
            if zataz_na_technika is None:
                otvorene_ulohy_dnes = (
                    Uloha.objects.filter(dokoncene=False, zaciatok__date=dnes)
                    .annotate(
                        _ma_zamietnutu=Exists(PrijataObjednavka.objects.filter(vytvorene_ulohy=OuterRef('pk'), stav='ZAMIETNUTA')),
                        _ma_aktivnu=Exists(PrijataObjednavka.objects.filter(vytvorene_ulohy=OuterRef('pk')).exclude(stav='ZAMIETNUTA')),
                    )
                    .exclude(_ma_zamietnutu=True, _ma_aktivnu=False)
                    .count()
                )
                pocet_technikov = max(1, Uloha.objects.filter(zaciatok__date=dnes).values('technici').distinct().count())
                zataz_na_technika = otvorene_ulohy_dnes / pocet_technikov
                cache.set(zataz_cache_key, zataz_na_technika, 60)

            limit_zvysena = getattr(ctx_gn, 'limit_uloh_zvysena', 3) or 3
            limit_kriticka = getattr(ctx_gn, 'limit_uloh_kriticka', 5) or 5
            if zataz_na_technika >= limit_kriticka:
                sla_status_kod = 'KRITICKA'
                sla_sprava = getattr(ctx_nastavenia, 'sla_text_kriticka', None) or 'Vysoká vyťaženosť kapacít – prednostné riešenie urgentných požiadaviek'
            elif zataz_na_technika >= limit_zvysena:
                sla_status_kod = 'ZVYSENA'
                sla_sprava = getattr(ctx_nastavenia, 'sla_text_zvysena', None) or 'Štandardná vyťaženosť kapacít – bežné lehoty vybavenia'
            else:
                sla_status_kod = 'OPTIMALNA'
                sla_sprava = getattr(ctx_nastavenia, 'sla_text_optimalna', None) or 'Voľné kapacity – prijímame nové požiadavky'

            sla_telemetria = {
                'status': sla_status_kod,
                'sprava': sla_sprava
            }

    context = {**context, 'sla_telemetria': sla_telemetria}
    return render(request, 'web/sablona_weby.html', context)

@transaction.atomic
def submit_inquiry(request):
    """
    Spracovanie dopytového formulára z webu.
    Vytvorí Zákazníka (ak neexistuje) a založí Prijatú Objednávku v ERP.
    """
    if request.method == 'POST':
        # 🛡️ ANTI-BOT HONEYPOT: Ak bot vyplní toto skryté pole, ticho ho zahodíme
        if request.POST.get('website_url'):
            return JsonResponse({'status': 'success', 'message': 'Dopyt bol úspešne odoslaný.'})

        # 🛡️ DB OCHRANA: Tvrdé orezanie stringov podľa max_length v modeloch
        firma = request.POST.get('firma', '').strip()[:100]
        email = request.POST.get('email', '').strip()[:254]
        telefon = request.POST.get('telefon', '').strip()[:20]
        # 🛡️ ANTI-DoS OCHRANA: Tvrdý limit na dĺžku textu (5000 znakov bohato stačí na akýkoľvek dopyt)
        poziadavka = request.POST.get('poziadavka', '').strip()[:5000]
        gdpr_suhlas = request.POST.get('gdpr_suhlas')
        zariadenie_id = request.POST.get('zariadenie_id')
        znacka_id = request.POST.get('znacka_id')
        sluzba_id = request.POST.get('sluzba_id')
        priloha = request.FILES.get('priloha')
        cislo_objednavky_klienta = request.POST.get('cislo_objednavky_klienta', '').strip()[:100]
        priorita = 'VYSOKA' if request.POST.get('priorita') == 'VYSOKA' else 'STREDNA'
        b2b_klient, _ = get_b2b_client_and_discount(request)
        is_b2b_direct = (
            (request.POST.get('is_b2b_direct') == 'true')
            and (b2b_klient is not None)
            and request.user.is_authenticated
            and getattr(request.user, 'is_active', False)
        )

        # 🛡️ BEZPEČNOSŤ: Validácia veľkosti a typu prílohy (Ochrana pred Malware a Storage Exhaustion)
        if priloha:
            if priloha.size <= 0 or priloha.size > 10 * 1024 * 1024:
                return JsonResponse({'status': 'error', 'message': 'Príloha je prázdna alebo príliš veľká (max 10 MB).'}, status=400)
            povolene_pripony = ('.pdf', '.jpg', '.jpeg', '.png', '.webp', '.mp4', '.mov', '.txt', '.log', '.json', '.zip', '.csv')
            if not priloha.name or not priloha.name.lower().endswith(povolene_pripony):
                return JsonResponse({'status': 'error', 'message': 'Nepovolený formát prílohy. Nahrajte obrázok, video alebo PDF.'}, status=400)

        # 🛡️ VALIDÁCIA DÁT: Popis závady / práce je povinný pre B2B aj anonymné dopyty
        if not poziadavka:
            return JsonResponse({'status': 'error', 'message': 'Popis požiadavky alebo závady je povinný.'}, status=400)

        raw_termin = request.POST.get('termin_dodania')
        try:
            parsed_termin = parse_date(str(raw_termin).strip()) if raw_termin and str(raw_termin).strip() else None
        except (ValueError, TypeError):
            return JsonResponse({'status': 'error', 'message': 'Neplatný formát termínu dodania.'}, status=400)

        # 🧠 ISAS B2B ENGINE: Overenie priamej B2B objednávky voči profilu klienta
        if is_b2b_direct and b2b_klient:
            email = (b2b_klient.email or (getattr(request.user, 'email', '') or '')).strip().lower()[:254]
            firma = b2b_klient.Nazov_spolocnosti or (request.user.get_full_name() or request.user.username)
            telefon = b2b_klient.telefon or ''
            gdpr_suhlas = True
        else:
            email = email.lower()
            if not email:
                return JsonResponse({'status': 'error', 'message': 'E-mail je povinný.'}, status=400)
                
            # 🛡️ VALIDÁCIA: Ochrana pred neplatným e-mailom
            if not _is_valid_email_address(email):
                return JsonResponse({'status': 'error', 'message': 'Zadali ste neplatný formát e-mailovej adresy.'}, status=400)
                
            if not gdpr_suhlas:
                return JsonResponse({'status': 'error', 'message': 'Pre odoslanie dopytu je nutný súhlas so spracovaním osobných údajov.'}, status=400)

        # 🛡️ ANTI-SPAM & IP RATE LIMIT
        ip_adresa = get_client_ip(request)
        ip_limit_key = f"inquiry_ip_limit_{ip_adresa or 'unknown'}"
        if _rate_limit_hit(ip_limit_key, 10, 300):
            return JsonResponse({'status': 'error', 'message': 'Príliš veľa dopytov z vašej siete. Skúste to neskôr.'}, status=429)

        # 🛡️ ANTI-SPAM & DOUBLE-CLICK OCHRANA: Zámok na 30 sekúnd
        if is_b2b_direct and b2b_klient:
            lock_id = f"lock_inquiry_b2b_{b2b_klient.id}"
        else:
            lock_id = f"lock_inquiry_{email.lower()}"
            
        if not cache.add(lock_id, 'locked', timeout=30):
            return JsonResponse({'status': 'error', 'message': 'Váš dopyt sa už spracováva. Prosím, neklikajte viackrát.'}, status=429)

        try:
            # 🛡️ GDPR AUDIT: Zaznamenanie presného času a IP adresy súhlasu
            ip_adresa = get_client_ip(request)
            cas_suhlasu = timezone.now().strftime('%d.%m.%Y %H:%M:%S')
            gdpr_zaznam = f"Vytvorené z webu. GDPR súhlas udelený: {cas_suhlasu} (IP: {ip_adresa})."

            # 🛡️ RACE CONDITION & IDENTITY PROTECTION
            if is_b2b_direct and b2b_klient:
                # 🔒 BEZPEČNOSŤ: Ak je to prihlásený B2B klient, NIKDY nehľadáme podľa e-mailu.
                # Použijeme priamo jeho overený profil. Zabraňuje to Data Leaku.
                zakaznik = b2b_klient
            else:
                # Najprv skúsime nájsť existujúceho (s uzamknutím riadku pre update)
                zakaznik = Zakaznik.objects.select_for_update().filter(email__iexact=email).first()
                
                if not zakaznik:
                    try:
                        # Ak neexistuje, vytvoríme ho v savepointe (ochrana pred zneplatnením transakcie)
                        with transaction.atomic():
                            zakaznik = Zakaznik.objects.create(
                                Nazov_spolocnosti=firma or f"Klient ({email})",
                                kontaktna_osoba=firma,
                                email=email,
                                telefon=telefon,
                                Ostatne=gdpr_zaznam
                            )
                    except Exception:
                        zakaznik = Zakaznik.objects.filter(email__iexact=email).first()
                else:
                    # Ak zákazník existuje, pripojíme GDPR log k jeho existujúcim poznámkam
                    update_fields = []
                    stare_ostatne = zakaznik.Ostatne or ""
                    if "GDPR súhlas udelený" not in stare_ostatne:
                        zakaznik.Ostatne = f"{stare_ostatne}\n{gdpr_zaznam}".strip()
                        update_fields.append('Ostatne')
                    if not zakaznik.telefon and telefon:
                        zakaznik.telefon = telefon
                        update_fields.append('telefon')
                    if not zakaznik.kontaktna_osoba and firma:
                        zakaznik.kontaktna_osoba = firma
                        update_fields.append('kontaktna_osoba')
                    if update_fields:
                        zakaznik.save(update_fields=update_fields)

            if not zakaznik:
                return JsonResponse({'status': 'error', 'message': 'Nepodarilo sa overiť ani vytvoriť profil zákazníka.'}, status=500)

            # 2. Spracovanie predvalidovaného termínu zo Smart Bookingu
            preferovany_cas = request.POST.get('preferovany_cas', '').strip()
            final_poznamka = f"{poziadavka}\n\n[Rezervovaný blok: {preferovany_cas}]" if preferovany_cas else poziadavka

            # Vytvorenie Prijatej Objednávky s PO číslom, termínom a SLA prioritou
            vystavil_meno = (request.user.get_full_name() or request.user.username) if (is_b2b_direct and request.user.is_authenticated) else firma
            vystavil_string = f"{vystavil_meno} ({firma})"[:150] if (is_b2b_direct and vystavil_meno != firma) else vystavil_meno[:150]

            nova_objednavka = PrijataObjednavka.objects.create(
                zakaznik=zakaznik,
                stav='NOVA',
                priorita=priorita,
                termin_dodania=parsed_termin,
                cislo_objednavky_klienta=cislo_objednavky_klienta or None,
                forma_prijatia='PORTAL' if is_b2b_direct else 'WEB',
                vystavil_za_klienta=vystavil_string,
                specificky_email_pre_fakturu=email,
                poznamka_klienta=final_poznamka,
                interna_poznamka="Priama B2B objednávka z klientskej zóny." if is_b2b_direct else "Dopyt z verejného webu."
            )
            
            # 📸 Uloženie fotky z mobilu (Machine Passport)
            if priloha:
                nova_objednavka.subor_objednavky = priloha
                nova_objednavka.save(update_fields=['subor_objednavky'])
            
            # 🚀 ISAS BIZNIS & BEZPEČNOSŤ: Párovanie stroja s ochranou voči IDOR (anonym cez PK iba pre verejnú požičovňu)
            if zariadenie_id:
                z_raw = str(zariadenie_id).strip()
                zariadenie = None
                try:
                    with transaction.atomic():
                        z_uuid = None
                        try:
                            z_uuid = uuid.UUID(z_raw)
                        except (ValueError, TypeError, AttributeError):
                            z_uuid = None

                        if is_b2b_direct and b2b_klient:
                            if z_uuid is not None:
                                zariadenie = ServisovaneZariadenie.objects.filter(
                                    bezpecnostny_kluc=z_uuid, zakaznik=b2b_klient
                                ).first()
                            elif z_raw.isdigit():
                                zariadenie = ServisovaneZariadenie.objects.filter(
                                    pk=int(z_raw), zakaznik=b2b_klient
                                ).first()
                        else:
                            if z_uuid is not None:
                                zariadenie = ServisovaneZariadenie.objects.filter(
                                    bezpecnostny_kluc=z_uuid
                                ).first()
                            elif z_raw.isdigit():
                                zariadenie = ServisovaneZariadenie.objects.filter(
                                    pk=int(z_raw),
                                    je_urcene_na_prenajom=True,
                                    zobrazit_na_webe_prenajom=True
                                ).first()
                except (ValueError, TypeError, DataError):
                    zariadenie = None

                if zariadenie:
                    nova_objednavka.zariadenia.add(zariadenie)
                    # 🛡️ ERP INTEGRITA: Priradenie objednávky majiteľovi stroja pri QR nahlásení (NIKDY pri prenajímaných strojoch!)
                    update_obj_fields = ['interna_poznamka']
                    if (
                        not is_b2b_direct
                        and not getattr(zariadenie, 'je_urcene_na_prenajom', False)
                        and not getattr(zariadenie, 'v_prenajme_od_isas', False)
                        and zariadenie.zakaznik
                        and not getattr(zariadenie.zakaznik, 'je_interny_profil', False)
                        and nova_objednavka.zakaznik != zariadenie.zakaznik
                    ):
                        nova_objednavka.zakaznik = zariadenie.zakaznik
                        update_obj_fields.append('zakaznik')
                    prefix_zdroja = "Priama B2B požiadavka z portálu" if is_b2b_direct else "Dopyt z verejného webu (QR kód)"
                    # 🛡️ EXAKTNÁ IDENTIFIKÁCIA: Ochrana pred predvolenou hodnotou 'Nezadané' a podpora IT parametrov (Hostname / URL)
                    sn_raw = (zariadenie.vyrobne_cislo or "").strip()
                    has_real_sn = bool(sn_raw and sn_raw.lower() not in ('nezadané', 'nezadane', 'none', '-'))

                    param = get_zariadenie_parametre(zariadenie)
                    it_adresa = (
                        getattr(param, 'hostname', None)
                        or getattr(param, 'url_adresa', None)
                        or getattr(param, 'ip_adresa', None)
                    ) if param else None
                    
                    zaklad_nazov = zariadenie.nazov or zariadenie.verejny_nazov or f"ID #{zariadenie.pk}"
                    if has_real_sn and it_adresa:
                        identifikator_zar = f"{zaklad_nazov} [{it_adresa}] (SN: {sn_raw})"
                    elif has_real_sn:
                        identifikator_zar = f"{zaklad_nazov} (SN: {sn_raw})"
                    elif it_adresa:
                        identifikator_zar = f"{zaklad_nazov} [{it_adresa}]"
                    else:
                        identifikator_zar = zaklad_nazov

                    nova_objednavka.interna_poznamka = f"{prefix_zdroja} ({identifikator_zar}). Nahlásil: {firma} ({email}, {telefon})."
                    nova_objednavka.save(update_fields=update_obj_fields)

            # 🎯 ISAS BIZNIS: Inteligentné párovanie služby a značky (so savepointom proti DataError)
            if sluzba_id and str(sluzba_id).strip().isdigit():
                try:
                    with transaction.atomic():
                        sluzba_qs = TypServisuPrace.objects.filter(pk=int(str(sluzba_id).strip()))
                        if not is_b2b_direct:
                            sluzba_qs = sluzba_qs.filter(zobrazit_na_webe=True)
                        sluzba = sluzba_qs.first()
                        if sluzba:
                            nova_objednavka.typ_servisu.add(sluzba)
                except (TypServisuPrace.DoesNotExist, ValueError, TypeError, DataError):
                    pass

            if znacka_id and str(znacka_id).strip().isdigit():
                try:
                    with transaction.atomic():
                        znacka = DatabazaVyrobcov.objects.get(pk=int(str(znacka_id).strip()))
                        nova_objednavka.poznamka_klienta = f"Preferovaná značka: {znacka.nazov_vyrobcu}\n\n{nova_objednavka.poznamka_klienta}"
                        nova_objednavka.save(update_fields=['poznamka_klienta'])
                except (DatabazaVyrobcov.DoesNotExist, ValueError, TypeError, DataError):
                    pass

            # 3. Asynchrónne odpálenie notifikácie priamo z WebBuilder/tasks.py po úspešnom commite
            from .tasks import notify_web_inquiry_task
            def _safe_notify_inquiry(pk=nova_objednavka.pk):
                try:
                    notify_web_inquiry_task.delay(pk)
                except Exception as exc:
                    logger.warning(f"Výpadok Celery/Redis brokera pri notifikácii dopytu #{pk}: {exc}")
            transaction.on_commit(_safe_notify_inquiry)

            cache.delete(lock_id)
            return JsonResponse({
                'status': 'success', 
                'message': 'Dopyt bol úspešne odoslaný. Čoskoro vás budeme kontaktovať.',
                'cislo_objednavky': nova_objednavka.cislo_objednavky_interne
            })
            
        except Exception as e:
            # Ak by databáza spadla, uvoľníme zámok, aby to klient mohol skúsiť znova
            cache.delete(lock_id)
            raise e  # Necháme Django spracovať chybu (vráti 500)
    
    return JsonResponse({'status': 'error', 'message': 'Neplatná požiadavka.'}, status=400)

def check_ticket_status(request):
    """
    🔍 ISAS TICKET CHECKER: Bezpečné overenie stavu zákazky.
    Zero Data Leak: Vracia výhradne len mapovaný string stavu, žiadne iné dáta.
    """
    if request.method == 'POST':
        cislo_objednavky = request.POST.get('ticket_number', '').strip()[:100]
        
        if not cislo_objednavky:
            return JsonResponse({'status': 'error', 'message': 'Zadajte číslo objednávky.'}, status=400)

        # 🛡️ ANTI BRUTE-FORCE OCHRANA (Rate Limiting)
        # Zamedzí botom hádať čísla objednávok (max 5 pokusov za minútu na IP)
        ip_adresa = get_client_ip(request)
        cache_key = f"ticket_check_limit_{ip_adresa}"
        
        try:
            pokusy = cache.incr(cache_key)
        except ValueError:
            cache.set(cache_key, 1, 60)
            pokusy = 1
            
        if pokusy > 5:
            return JsonResponse({'status': 'error', 'message': 'Príliš veľa pokusov. Skúste to prosím o minútu.'}, status=429)

        # 🛡️ ZERO DATA LEAK: Vytiahneme len stav, nič iné!
        objednavka = PrijataObjednavka.objects.filter(cislo_objednavky_interne__iexact=cislo_objednavky).only('stav').first()
        
        if not objednavka:
            return JsonResponse({'status': 'error', 'message': 'Zákazka s týmto číslom sa nenašla. Skontrolujte preklepy.'}, status=404)

        # 🧠 BIZNIS LOGIKA: Mapovanie interných stavov na verejné (Customer-friendly)
        status_map = {
            'NOVA': {'text': 'Prijaté na diagnostiku / Čaká na spracovanie', 'color': 'text-amber-600', 'bg': 'bg-amber-50', 'icon': '⏳'},
            'SCHVALENA': {'text': 'V riešení / Pracujeme na tom', 'color': 'text-blue-600', 'bg': 'bg-blue-50', 'icon': '⚙️'},
            'VYBAVENA': {'text': get_industry_term('ticket_status_vybavena', 'Vybavené / Dokončené'), 'color': 'text-emerald-600', 'bg': 'bg-emerald-50', 'icon': '✅'},
            'VYFAKTUROVANA': {'text': 'Uzavreté a vyfakturované', 'color': 'text-gray-600', 'bg': 'bg-gray-50', 'icon': '📁'},
            'ZAMIETNUTA': {'text': 'Zamietnuté / Stornované', 'color': 'text-red-600', 'bg': 'bg-red-50', 'icon': '❌'},
        }
        
        verejny_stav = status_map.get(objednavka.stav, {'text': 'Neznámy stav', 'color': 'text-gray-600', 'bg': 'bg-gray-50', 'icon': '❓'})

        return JsonResponse({
            'status': 'success', 
            'public_status': verejny_stav['text'],
            'color_class': verejny_stav['color'],
            'bg_class': verejny_stav['bg'],
            'icon': verejny_stav['icon']
        })

    return JsonResponse({'status': 'error', 'message': 'Neplatná požiadavka.'}, status=400)

def calculate_callout_fee(request):
    """
    🧮 ISAS CALCULATOR: Výpočet orientačnej ceny výjazdu.
    Smart Routing: Vypočíta vzdialenosť z Centrály aj zo všetkých KAMENNÝCH Filiálok 
    v jednom API dopyte a vyberie tú najbližšiu!
    """
    if request.method == 'POST':
        destination = request.POST.get('destination', '').strip().replace('|', ' ')[:200]
        service_id = request.POST.get('service_id', '')

        if not destination or not service_id:
            return JsonResponse({'status': 'error', 'message': 'Zadajte miesto a vyberte službu.'}, status=400)

        # 🛡️ ANTI BRUTE-FORCE OCHRANA (Rate Limiting)
        ip_adresa = get_client_ip(request)
        cache_key = f"calc_limit_{ip_adresa}"
        
        try:
            pokusy = cache.incr(cache_key)
        except ValueError:
            cache.set(cache_key, 1, 60)
            pokusy = 1
            
        if pokusy > 10:
            return JsonResponse({'status': 'error', 'message': 'Príliš veľa pokusov. Skúste to prosím neskôr.'}, status=429)

        service_id_str = str(service_id).strip()
        if not service_id_str.isdigit():
            return JsonResponse({'status': 'error', 'message': 'Vybraná služba neexistuje alebo je neplatná.'}, status=404)

        try:            
            gn = GlobalneNastavenia.objects.first()
            identita = FiremnaIdentita.objects.first()
            sluzba = TypServisuPrace.objects.get(pk=int(service_id_str), zobrazit_na_webe=True)

            if not gn or not gn.google_maps_api_key:
                return JsonResponse({'status': 'error', 'message': 'Systém nemá nastavený Google Maps API kľúč.'}, status=500)

            origins = []
            
            # 📍 1. Pridáme Centrálu (Priorita: Adresa prevádzky -> Sídlo)
            if identita:
                if identita.adresa_prevadzky:
                    # Očistíme od enterov pre Google API
                    cista_adresa = str(identita.adresa_prevadzky).replace('\r\n', ', ').replace('\n', ', ')
                    origins.append(cista_adresa)
                elif identita.sidlo_mesto:
                    ulica_cast = f"{identita.sidlo_ulica or ''} {identita.sidlo_cislo or ''}".strip()
                    mesto_cast = f"{identita.sidlo_psc or ''} {identita.sidlo_mesto or ''}".strip()
                    sidlo_final = f"{ulica_cast}, {mesto_cast}".strip(', ')
                    if sidlo_final:
                        origins.append(sidlo_final)
            
            # 📍 2. Pridáme Filiálky (🛡️ OCHRANA: Iba KAMENNÉ BUDOVY, ignorujeme autá/dodávky!)
            filialky = Filialka.objects.filter(
                je_aktivna=True, 
                typ_skladu='BUDOVA'  # <-- TOTO JE TVOJ FIX!
            ).exclude(adresa__isnull=True).exclude(adresa__exact='')
            
            for f in filialky:
                cista_adresa_filialky = str(f.adresa).replace('\r\n', ', ').replace('\n', ', ')
                origins.append(cista_adresa_filialky)

            if not origins:
                return JsonResponse({'status': 'error', 'message': 'Systém nemá nastavené sídlo firmy ani kamenné pobočky.'}, status=500)

            # 🚀 3. Bleskový 1-to-N dopyt na Google Maps (Šetrí API kredit a čas)
            krajina_iso = FiremnaIdentita.get_aktivna_krajina_iso() if hasattr(FiremnaIdentita, 'get_aktivna_krajina_iso') else ''
            lang_kod = getattr(request, 'LANGUAGE_CODE', None) or (krajina_iso.lower() if krajina_iso else 'en')
            params = {
                'origins': "|".join(origins),
                'destinations': destination,
                'key': gn.google_maps_api_key,
                'language': lang_kod[:2]
            }
            
            response = requests.get("https://maps.googleapis.com/maps/api/distancematrix/json", params=params, timeout=5)
            if response.status_code != 200:
                return JsonResponse({'status': 'error', 'message': 'Služba Google Maps je momentálne nedostupná.'}, status=502)
                
            response = response.json()
            
            if response.get('status') != 'OK':
                return JsonResponse({'status': 'error', 'message': 'Nepodarilo sa vypočítať trasu. Skontrolujte zadané PSČ alebo Mesto.'}, status=400)

            # 🧠 4. Nájdenie najkratšej trasy (Najbližšia pobočka k zákazníkovi)
            min_distance_m = float('inf')
            min_duration_s = 0
            najdena_trasa = False

            for row in response.get('rows', []):
                if not row.get('elements'): continue
                element = row['elements'][0]
                
                if element.get('status') == 'OK' and isinstance(element.get('distance'), dict) and isinstance(element.get('duration'), dict):
                    dist = element['distance'].get('value')
                    dur = element['duration'].get('value', 0)
                    if dist is not None and dist < min_distance_m:
                        min_distance_m = dist
                        min_duration_s = dur
                        najdena_trasa = True

            if not najdena_trasa:
                return JsonResponse({'status': 'error', 'message': 'Nepodarilo sa nájsť cestu k zadanému cieľu.'}, status=400)

            # Prevod na KM a Hodiny
            distance_km = Decimal(str(min_distance_m)) / Decimal('1000')
            duration_h = Decimal(str(min_duration_s)) / Decimal('3600')

            # 💰 5. VÝPOČET DOPRAVY (Cesta sa účtuje TAM AJ SPÄŤ -> * 2)
            sadzba_km = gn.sadzba_za_km_zakaznik if (gn and gn.sadzba_za_km_zakaznik is not None) else Decimal('0.00')
            sadzba_cas = gn.sadzba_za_cas_na_ceste if (gn and gn.sadzba_za_cas_na_ceste is not None) else Decimal('0.00')

            cena_cesta = (distance_km * 2 * sadzba_km) + (duration_h * 2 * sadzba_cas)
            cena_cesta = cena_cesta.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            # 💰 6. VÝPOČET PRÁCE (Dynamická mena podľa sídla firmy)
            mena_firmy = getattr(identita, 'predvolena_mena', None) if identita else None
            mena_kod = (mena_firmy or get_firemna_mena()).strip()

            if sluzba.sposob_uctovania == 'FIX':
                cena_fix = sluzba.fixna_cena_predaj if sluzba.fixna_cena_predaj is not None else Decimal('0.00')
                praca_text = f"{cena_fix} {mena_kod} (Fixná cena)"
            else:
                cena_hod = sluzba.hodinova_sadzba_predaj if (sluzba.hodinova_sadzba_predaj and sluzba.hodinova_sadzba_predaj > Decimal('0.00')) else (gn.hodinova_sadzba_technik if (gn and gn.hodinova_sadzba_technik and gn.hodinova_sadzba_technik > Decimal('0.00')) else Decimal('0.00'))
                praca_text = f"od {cena_hod} {mena_kod} / hod" if cena_hod > Decimal('0.00') else "Na vyžiadanie"

            return JsonResponse({
                'status': 'success',
                'vzdialenost': f"{round(distance_km * 2, 1)} km (tam aj späť)",
                'cena_cesta': f"{cena_cesta} {mena_kod}",
                'cena_praca': praca_text
            })

        except (TypServisuPrace.DoesNotExist, ValueError, TypeError, DataError):
            return JsonResponse({'status': 'error', 'message': 'Vybraná služba neexistuje alebo je neplatná.'}, status=404)
        except Exception as e:
            logger.error(f"ISAS WebBuilder Calc Error: {e}")
            return JsonResponse({'status': 'error', 'message': 'Nastala chyba pri komunikácii so serverom.'}, status=500)

    return JsonResponse({'status': 'error', 'message': 'Neplatná požiadavka.'}, status=400)

def public_iot_demo(request):
    """
    📡 ISAS IOT DEMO: Simulátor reálnych dát pre verejný web.
    Žiadne SQL dopyty, chránené Redis Rate Limitingom.
    """
    ip_adresa = get_client_ip(request)
    cache_key = f"iot_demo_limit_{ip_adresa}"
    
    try:
        pokusy = cache.incr(cache_key)
    except ValueError:
        cache.set(cache_key, 1, 60)
        pokusy = 1
        
    if pokusy > 180:
        return JsonResponse({'status': 'error', 'message': 'Príliš veľa požiadaviek.'}, status=429)

    t = time.time()
    
    # Dynamické načítanie nominálnych hodnôt z odvetvia bez fiktívnych nenulových fallbackov
    try:
        base_1 = float(get_industry_term('gauge_1_base', '0.0'))
        base_2 = float(get_industry_term('gauge_2_base', '0.0'))
        base_3 = float(get_industry_term('gauge_3_base', '0.0'))
    except (ValueError, TypeError):
        base_1, base_2, base_3 = 0.0, 0.0, 0.0

    # Simulácia plynulej prevádzkovej fluktuácie výhradne pri kladnom nominále technológie
    val_1 = (base_1 + (math.sin(t / 5.0) * (base_1 * 0.02)) + ((t % 100) / 5000.0)) if base_1 > 0 else 0.0
    val_2 = (base_2 + (math.sin(t / 20.0) * (base_2 * 0.04)) + ((t % 50) / 2000.0)) if base_2 > 0 else 0.0
    val_3 = (base_3 + (math.sin(t / 2.0) * (base_3 * 0.05)) + ((t % 10) / 1000.0)) if base_3 > 0 else 0.0
    
    return JsonResponse({
        'status': 'success',
        'analog': {
            'hodnota_1': round(val_1, 2),
            'hodnota_2': round(val_2, 1),
            'hodnota_3': round(val_3, 1),
        },
        'digital': {
            'chod': base_1 > 0,
            'pomocny_okruh': (val_2 >= base_2) if base_2 > 0 else False,
            'porucha': False,
        }
    })

def machine_passport_view(request, kluc):
    """
    📱 ISAS DIGITAL PASSPORT: Verejný profil stroja po naskenovaní QR kódu.
    """
    try:
        kluc_uuid = uuid.UUID(str(kluc).strip())
    except (ValueError, TypeError, AttributeError):
        raise Http404("Neplatný bezpečnostný kľúč pasu zariadenia.")

    # 🛡️ VÝKON: select_related eliminuje N+1 dopyty vrátane odvetvových parametrov
    active_param_rels = get_active_parameter_reverse_names()
    zariadenie = get_object_or_404(
        ServisovaneZariadenie.objects.select_related('vyrobca', 'zakaznik', *active_param_rels),
        bezpecnostny_kluc=kluc_uuid
    )
    identita = FiremnaIdentita.objects.first()
    
    # 🛡️ ISAS ARCHITECT: Spracovanie preview režimu výhradne pre autentifikovaný personál
    is_preview = _resolve_preview_mode(request)

    # 🎨 DRAFT vs LIVE: Načítanie zodpovedajúcej konfigurácie (PK 2 pre DRAFT, PK 1 pre LIVE)
    nastavenia = WebNastavenia.objects.filter(pk=2 if is_preview else 1).first() or WebNastavenia.objects.first()
    
    dnes = timezone.now().date()
    b2b_klient, _ = get_b2b_client_and_discount(request)

    # 🛡️ ARCHITEKTÚRA & RBAC: Defenzívne vyhodnotenie personálu (zamestnanec / superadmin) s ochranou pred zákazníckym účtom
    je_opravneny_personal = bool(
        request.user.is_authenticated
        and request.user.is_active
        and (request.user.is_staff or request.user.is_superuser)
        and not hasattr(request.user, 'zakaznik_profil')
    )

    # 🛡️ GDPR & MULTI-TENANT FIREWALL: Overenie, či je prihlásený používateľ skutočným vlastníkom tohto stroja
    je_vlastnik_stroja = bool(
        request.user.is_authenticated
        and b2b_klient is not None
        and zariadenie.zakaznik_id is not None
        and b2b_klient.pk == zariadenie.zakaznik_id
    )

    # 🛡️ VÝKON: Deterministické zotriedenie a eliminácia N+1 dotazov
    servisne_zaznamy = list(ServisnyZaznam.objects.filter(
        zariadenie=zariadenie, dokoncene=True
    ).prefetch_related('typ_servisu', 'pouzite_polozky_zaznamu__content_object', 'pouzite_polozky_zaznamu__content_type', 'technici').order_by('-vytvorene', '-id'))

    # 🚀 VÝKON: Pre-caching poradových čísel v RAM s fallbackom na ID pre flotilu bez zákazníka
    if zariadenie.zakaznik_id:
        vsetky_ids = list(ServisnyZaznam.objects.filter(zakaznik_id=zariadenie.zakaznik_id).order_by('id').values_list('id', flat=True))
        id_to_num = {zid: idx + 1 for idx, zid in enumerate(vsetky_ids)}
        for zaznam in servisne_zaznamy:
            zaznam._poradove_cislo_cache = id_to_num.get(zaznam.id, zaznam.id)
    else:
        for zaznam in servisne_zaznamy:
            zaznam._poradove_cislo_cache = zaznam.id

    # 🛡️ ZERO DATA LEAK & AUDIT: Bezpečný prístup cez getattr s ochranou pred chýbajúcimi atribútmi
    for zaznam in servisne_zaznamy:
        zaznam.aktivni_technici = [
            t for t in zaznam.technici.all()
            if (je_opravneny_personal or getattr(t, 'zobrazit_na_webe', False)) and getattr(t, 'meno', None) and getattr(t, 'priezvisko', None)
        ]

        # 🛡️ GARANČNÝ MANAŽMENT: 100% defenzívna sanitizácia dátumu servisu cez to_date() a číselná konverzia mesiacov
        typy_prace = zaznam.typ_servisu.all()
        platne_mesiace = []
        for t in typy_prace:
            m_val = getattr(t, 'zaruka_mesiace', None)
            try:
                if m_val is not None and int(m_val) > 0:
                    platne_mesiace.append(int(m_val))
            except (ValueError, TypeError):
                continue
        max_mesiace = max(platne_mesiace, default=0)
        d_servis = to_date(getattr(zaznam, 'vytvorene', None))

        if max_mesiace > 0 and d_servis:
            zaruka_do = add_calendar_interval(d_servis, months=max_mesiace)
            zaznam.zaruka_prace_mesiace = max_mesiace
            zaznam.zaruka_prace_do = zaruka_do
            zaznam.zaruka_prace_je_platna = bool(zaruka_do and dnes and zaruka_do >= dnes)
        else:
            zaznam.zaruka_prace_mesiace = None
            zaznam.zaruka_prace_do = None
            zaznam.zaruka_prace_je_platna = False

    posledny_servis = servisne_zaznamy[0] if servisne_zaznamy else None
    predchadzajuce_servisy = servisne_zaznamy[1:] if len(servisne_zaznamy) > 1 else []

    # 📊 AUDITNÁ BILANCIA: Exaktný počet z RAM bez dodatočného SQL COUNT dopytu
    celkovy_pocet_servisov = len(servisne_zaznamy)

    # 🛡️ ARCHITEKTÚRA: Deterministický výber najurgentnejšieho plánu s plnou ochranou pred NULL dátumami aj motohodinami
    aktualne_mh = zariadenie.aktualne_motohodiny or 0
    aktualne_zataz = getattr(zariadenie, 'motohodiny_v_zatazi', 0) or 0
    PlanovanyServisModel = apps.get_model('Evidencia', 'PlanovanyServisZariadenia')
    vsetky_plany = list(PlanovanyServisModel.objects.select_related('typ_servisu').filter(zariadenie=zariadenie)) if PlanovanyServisModel else []

    def _urgencia_planu(plan):
        je_prekroceny = False
        min_rozdiel = float('inf')

        # 🛡️ DEFENZÍVNA SANITIZÁCIA: to_date garantuje date objekt a predchádza TypeErroru pri datetime/str
        p_datum = to_date(getattr(plan, 'planovany_datum', None))
        if p_datum and dnes:
            diff_dni = (p_datum - dnes).days
            if diff_dni < 0:
                je_prekroceny = True
            min_rozdiel = min(min_rozdiel, diff_dni)

        # 🛡️ DEFENZÍVNA ČÍSELNÁ KONVERZIA: Ochrana pred nevalidnými typmi v počítadlách
        try:
            p_mh = int(plan.planovane_motohodiny) if getattr(plan, 'planovane_motohodiny', None) is not None else None
        except (ValueError, TypeError):
            p_mh = None
        if p_mh is not None:
            diff_mh = p_mh - aktualne_mh
            if diff_mh <= 0:
                je_prekroceny = True
            min_rozdiel = min(min_rozdiel, diff_mh)

        try:
            p_zataz = int(plan.planovane_motohodiny_zataz) if getattr(plan, 'planovane_motohodiny_zataz', None) is not None else None
        except (ValueError, TypeError):
            p_zataz = None
        if p_zataz is not None:
            diff_zataz = p_zataz - aktualne_zataz
            if diff_zataz <= 0:
                je_prekroceny = True
            min_rozdiel = min(min_rozdiel, diff_zataz)

        try:
            vaha = int(getattr(plan, 'vaha_hierarchie', 0) or 0)
        except (ValueError, TypeError):
            vaha = 0

        return (0 if je_prekroceny else 1, min_rozdiel, -vaha)

    zoradene_plany = sorted(vsetky_plany, key=_urgencia_planu) if vsetky_plany else []
    dalsi_servis = zoradene_plany[0] if zoradene_plany else None
    dalsie_plany = zoradene_plany[1:] if len(zoradene_plany) > 1 else []

    # ⏱️ PREDIKTÍVNA ODVETVOVÁ ANALYTIKA: Plne sanitizovaný odpočet jednotiek a dní bez rizika pádu na None/Type chybách
    for plan in zoradene_plany:
        p_datum = to_date(getattr(plan, 'planovany_datum', None))
        if p_datum and dnes:
            plan.rozdiel_dni = (p_datum - dnes).days
            plan.abs_rozdiel_dni = abs(plan.rozdiel_dni)
        else:
            plan.rozdiel_dni = None
            plan.abs_rozdiel_dni = None

        try:
            p_mh = int(plan.planovane_motohodiny) if getattr(plan, 'planovane_motohodiny', None) is not None else None
            z_mh = int(zariadenie.aktualne_motohodiny) if getattr(zariadenie, 'aktualne_motohodiny', None) is not None else None
        except (ValueError, TypeError):
            p_mh, z_mh = None, None

        if p_mh is not None and z_mh is not None:
            plan.rozdiel_meter = p_mh - z_mh
            plan.abs_rozdiel_meter = abs(plan.rozdiel_meter)
        else:
            plan.rozdiel_meter = None
            plan.abs_rozdiel_meter = None

        try:
            p_zataz = int(plan.planovane_motohodiny_zataz) if getattr(plan, 'planovane_motohodiny_zataz', None) is not None else None
            z_zataz = int(zariadenie.motohodiny_v_zatazi) if getattr(zariadenie, 'motohodiny_v_zatazi', None) is not None else None
        except (ValueError, TypeError):
            p_zataz, z_zataz = None, None

        if p_zataz is not None and z_zataz is not None:
            plan.rozdiel_meter_zataz = p_zataz - z_zataz
            plan.abs_rozdiel_meter_zataz = abs(plan.rozdiel_meter_zataz)
        else:
            plan.rozdiel_meter_zataz = None
            plan.abs_rozdiel_meter_zataz = None

    sluzby = list(TypServisuPrace.objects.filter(zobrazit_na_webe=True).order_by('nazov').values('id', 'nazov'))

    # 🛡️ ZERO DATA LEAK: Servisné predpisy a normované diely pre personál vrátane GFK prefetchu (pole diel)
    servisne_predpisy = []
    if je_opravneny_personal:
        servisne_predpisy = list(ServisnyPredpis.objects.filter(
            zariadenie=zariadenie
        ).select_related('typ_prace').prefetch_related('polozky__diel', 'polozky__content_type'))

    # ⚖️ LEGISLATÍVNY COMPLIANCE: Automatický zber zákonných revízií a odborných inšpekcií bez úniku interných dát
    compliance_audity = get_zariadenie_compliance_audit(zariadenie, referencny_datum=dnes)
    legal_disclaimer = get_legal_disclaimer() if compliance_audity else ""

    # 📡 IOT TELEMETRIA: Načítanie aktívnych alarmov a posledných živých dát senzorov zo slovníka signálov
    aktivne_alarmy = []
    live_telemetria = []
    posledna_telemetria_cas = None
    posledny_paket = None
    if zariadenie.iot_je_aktivny:
        aktivne_alarmy = list(AlarmLog.objects.filter(
            zariadenie=zariadenie, vyriesene=False
        ).order_by('-casova_peciatka')[:3])

        posledny_paket = TelemetryHistory.objects.filter(zariadenie=zariadenie).order_by('-casova_peciatka').first()
        if posledny_paket:
            posledna_telemetria_cas = posledny_paket.casova_peciatka
            slovnik_list = list(SignalDictionary.objects.filter(zariadenie=zariadenie))
            slovnik = {str(s.register_adresa): s for s in slovnik_list}
            slovnik_podla_nazvu = {str(s.nazov_signalu): s for s in slovnik_list}

            if isinstance(posledny_paket.analogove_hodnoty, dict):
                for kluc, hodnota in posledny_paket.analogove_hodnoty.items():
                    sig_addr = slovnik.get(str(kluc))
                    sig = sig_addr or slovnik_podla_nazvu.get(str(kluc))
                    nazov = sig.nazov_signalu if sig else str(kluc).replace('_', ' ').capitalize()
                    jednotka = sig.merna_jednotka if sig else ''
                    nasobic = float(sig_addr.nasobic) if (sig_addr and sig_addr.nasobic is not None) else 1.0
                    try:
                        vypocitana = round(float(hodnota) * nasobic, 2)
                        if vypocitana.is_integer():
                            vypocitana = int(vypocitana)
                    except (ValueError, TypeError):
                        vypocitana = hodnota

                    live_telemetria.append({
                        'nazov': nazov,
                        'hodnota': f"{vypocitana} {jednotka}".strip(),
                        'typ': 'ANALOG',
                        'ikona': '📈'
                    })

            if isinstance(posledny_paket.digitalne_stavy, dict):
                for kluc, stav in posledny_paket.digitalne_stavy.items():
                    sig = slovnik.get(str(kluc))
                    k_str = str(kluc).lower()
                    if sig:
                        nazov = sig.nazov_signalu
                    elif k_str in ('chod', 'dig_1', 'online', 'web', 'api'):
                        nazov = get_industry_term('dig_1_title', str(kluc).replace('_', ' ').capitalize())
                    elif k_str in ('pomocny_okruh', 'dig_2', 'db', 'databaza', 'cluster'):
                        nazov = get_industry_term('dig_2_title', str(kluc).replace('_', ' ').capitalize())
                    elif k_str in ('porucha', 'error', 'chyba', 'dig_error'):
                        nazov = get_industry_term('dig_error_title', str(kluc).replace('_', ' ').capitalize())
                    else:
                        nazov = str(kluc).replace('_', ' ').capitalize()

                    je_aktivny = bool(stav)
                    if k_str in ('chod', 'dig_1', 'online', 'web', 'api'):
                        hodnota_text = get_industry_term('dig_1_on', 'ZAPNUTÝ') if je_aktivny else get_industry_term('dig_1_off', 'VYPNUTÝ')
                    elif k_str in ('pomocny_okruh', 'dig_2', 'db', 'databaza', 'cluster'):
                        hodnota_text = get_industry_term('dig_2_on', 'AKTÍVNY') if je_aktivny else get_industry_term('dig_2_off', 'NEAKTÍVNY')
                    elif k_str in ('porucha', 'error', 'chyba', 'dig_error'):
                        hodnota_text = get_industry_term('dig_error_fail', 'PORUCHA') if je_aktivny else get_industry_term('dig_error_ok', 'V NORME')
                    else:
                        hodnota_text = 'ZAPNUTÝ' if je_aktivny else 'VYPNUTÝ'

                    live_telemetria.append({
                        'nazov': nazov,
                        'hodnota': hodnota_text,
                        'je_aktivny': je_aktivny,
                        'typ': 'DIGITAL',
                        'ikona': '🟢' if je_aktivny else '⚪'
                    })

    # 🧠 ODVETVOVÝ COMPLIANCE ENGINE: Spustenie živej diagnostiky a načítanie odvetvového lexikónu s farbami
    live_diagnostika = get_zariadenie_live_diagnostika(zariadenie, posledny_zaznam=posledny_paket) if zariadenie.iot_je_aktivny else []
    kategoria_farba = ZARIADENIA_COLOR_MAP.get(zariadenie.kategoria, '') if isinstance(ZARIADENIA_COLOR_MAP, dict) else ''

    industry_terms = get_all_industry_terms({
        'meter_load_unit': getattr(zariadenie, 'meter_load_unit', None) or get_industry_term('meter_load_unit', 'h')
    })

    # 📋 OPERATÍVA & DISPEČING: Aktívne servisné úlohy a otvorené požiadavky viazané na tento stroj
    UlohaModel = apps.get_model('Evidencia', 'Uloha')
    aktivne_ulohy = list(
        UlohaModel.objects.filter(zariadenia=zariadenie, dokoncene=False)
        .prefetch_related('typ_servisu', 'technici')
        .order_by('zaciatok')
    ) if UlohaModel else []

    PrijataObjednavkaModel = apps.get_model('Evidencia', 'PrijataObjednavka')
    otvorene_objednavky = list(
        PrijataObjednavkaModel.objects.filter(
            zariadenia=zariadenie,
            stav__in=['NOVA', 'SCHVALENA']
        ).order_by('-datum_prijatia')
    ) if PrijataObjednavkaModel else []

    web_ver = cache.get('web_cache_version', 1)
    aktivne_bloky_set = get_active_web_blocks(cache_ver=web_ver, is_preview=is_preview)

    # 🌐 DYNAMICKÁ KANONICKÁ URL: Očistená od query parametrov podľa systémových pravidiel
    canonical_url = request.build_absolute_uri(request.path)

    context = {
        'sluzby': sluzby,
        'nav_katalog_menu': get_nav_katalog_menu(cache_ver=web_ver, is_preview=is_preview, aktivne_bloky=aktivne_bloky_set),
        'nav_flags': get_web_nav_flags(cache_ver=web_ver, is_preview=is_preview, aktivne_bloky=aktivne_bloky_set),
        'zariadenie': zariadenie,
        'posledny_servis': posledny_servis,
        'predchadzajuce_servisy': predchadzajuce_servisy,
        'dalsi_servis': dalsi_servis,
        'dalsie_plany': dalsie_plany,
        'celkovy_pocet_servisov': celkovy_pocet_servisov,
        'servisne_predpisy': servisne_predpisy,
        'aktivne_ulohy': aktivne_ulohy,
        'otvorene_objednavky': otvorene_objednavky,
        'aktivne_alarmy': aktivne_alarmy,
        'live_telemetria': live_telemetria,
        'live_diagnostika': live_diagnostika,
        'posledna_telemetria_cas': posledna_telemetria_cas,
        'compliance_audity': compliance_audity,
        'legal_disclaimer': legal_disclaimer,
        'kategoria_farba': kategoria_farba,
        'industry_terms': industry_terms,
        'b2b_klient': b2b_klient,
        'identita': identita,
        'nastavenia': nastavenia,
        'dnes': dnes,
        'is_preview': is_preview,
        'je_opravneny_personal': je_opravneny_personal,
        'je_vlastnik_stroja': je_vlastnik_stroja,
        'mena': getattr(identita, 'predvolena_mena', None) or get_firemna_mena(),
        'canonical_url': canonical_url,
    }

    return render(request, 'web/machine_passport.html', context)

# =====================================================================
# 🌐 FÁZA 3: MULTI-PAGE ARCHITECTURE (MPA) - ZOZNAMY A DETAILY
# =====================================================================
class WebBaseMixin:
    """🛡️ ISAS GLOBAL MIXIN: Automaticky dodá identitu firmy a nastavenia do každej podsekcie webu."""
    def dispatch(self, request, *args, **kwargs):
        _resolve_preview_mode(request)
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        is_preview = _resolve_preview_mode(self.request)
        context['is_preview'] = is_preview
        
        # 🚀 CACHE SINGLETONS: Ochrana DB pred opakovanými SELECT dopytmi na každej podstránke
        cache_ver = cache.get('web_cache_version', 1)
        identita_cache_key = f'isas_firemna_identita_singleton_v{cache_ver}'
        identita = cache.get(identita_cache_key)
        if not identita:
            identita_db = FiremnaIdentita.objects.select_related('zodpovedny_zamestnanec_sklad').first()
            if identita_db:
                identita = identita_db
                cache.set(identita_cache_key, identita, 3600)
            else:
                identita = FiremnaIdentita(nazov_spolocnosti='Naša Spoločnosť')
        context['identita'] = identita

        nastavenia_cache_key = f'isas_web_nastavenia_{2 if is_preview else 1}_v{cache_ver}'
        nastavenia = cache.get(nastavenia_cache_key)
        if not nastavenia:
            nastavenia_db = WebNastavenia.objects.filter(pk=2 if is_preview else 1).first() or WebNastavenia.objects.first()
            if nastavenia_db:
                nastavenia = nastavenia_db
                cache.set(nastavenia_cache_key, nastavenia, 3600)
            else:
                default_titulok = identita.nazov_spolocnosti if (identita and getattr(identita, 'pk', None) and identita.nazov_spolocnosti) else 'ISAS CORE WEB'
                nastavenia = WebNastavenia(hlavna_farba='#0284c7', titulok_webu=default_titulok)
        context['nastavenia'] = nastavenia
        context['mena'] = getattr(identita, 'predvolena_mena', None) or get_firemna_mena()

        gn_cache_key = f'isas_globalne_nastavenia_v{cache_ver}'
        globalne_nastavenia = cache.get(gn_cache_key)
        if not globalne_nastavenia:
            GlobalneNastaveniaModel = apps.get_model('ISASaplikacia', 'GlobalneNastavenia')
            globalne_nastavenia = GlobalneNastaveniaModel.objects.first() if GlobalneNastaveniaModel else None
            if globalne_nastavenia:
                cache.set(gn_cache_key, globalne_nastavenia, 3600)
        context['globalne_nastavenia'] = globalne_nastavenia

        # 💰 BIZNIS VÝPOČET: Sadzby dopravy s DPH pre dopytový košík a transparentnosť cien
        GlobalneNastaveniaModel = apps.get_model('ISASaplikacia', 'GlobalneNastavenia')
        zakladna_dph = GlobalneNastaveniaModel.get_zakladna_dph() if GlobalneNastaveniaModel else Decimal('0.00')
        dph_koeficient = Decimal('1.00') + (zakladna_dph / Decimal('100.00')) if getattr(identita, 'je_platca_dph', False) else Decimal('1.00')

        if globalne_nastavenia and getattr(globalne_nastavenia, 'sadzba_za_km_zakaznik', None):
            context['sadzba_za_km_s_dph'] = (globalne_nastavenia.sadzba_za_km_zakaznik * dph_koeficient).quantize(Decimal('0.01'))
        if globalne_nastavenia and getattr(globalne_nastavenia, 'sadzba_za_cas_na_ceste', None):
            context['sadzba_za_cas_na_ceste_s_dph'] = (globalne_nastavenia.sadzba_za_cas_na_ceste * dph_koeficient).quantize(Decimal('0.01'))

        context['industry_terms'] = get_all_industry_terms()

        # 🚦 DATA-DRIVEN LIVE SLA: Objektívny prepočet dennej vyťaženosti technikov bez odvetvového hardkódu
        if nastavenia.zobrazit_live_sla:
            UlohaModel = apps.get_model('Evidencia', 'Uloha')
            gn = globalne_nastavenia
            limit_zvysena = getattr(gn, 'limit_uloh_zvysena', 3) or 3
            limit_kriticka = getattr(gn, 'limit_uloh_kriticka', 5) or 5

            dnes = timezone.now().date()
            PrijataObjednavkaModel = apps.get_model('Evidencia', 'PrijataObjednavka')
            if UlohaModel and PrijataObjednavkaModel:
                otvorene_ulohy_dnes = (
                    UlohaModel.objects.filter(dokoncene=False, zaciatok__date=dnes)
                    .annotate(
                        _ma_zamietnutu=Exists(PrijataObjednavkaModel.objects.filter(vytvorene_ulohy=OuterRef('pk'), stav='ZAMIETNUTA')),
                        _ma_aktivnu=Exists(PrijataObjednavkaModel.objects.filter(vytvorene_ulohy=OuterRef('pk')).exclude(stav='ZAMIETNUTA')),
                    )
                    .exclude(_ma_zamietnutu=True, _ma_aktivnu=False)
                    .count()
                )
            else:
                otvorene_ulohy_dnes = UlohaModel.objects.filter(dokoncene=False, zaciatok__date=dnes).count() if UlohaModel else 0
            pocet_technikov = UlohaModel.objects.filter(zaciatok__date=dnes).values('technici').distinct().count() if UlohaModel else 1
            zataz_na_technika = otvorene_ulohy_dnes / max(1, pocet_technikov)

            teraz = timezone.now()
            cas_teraz = teraz.time()
            je_vikend = teraz.weekday() in (5, 6)
            je_mimo_prevadzky = False
            if gn:
                if je_vikend and not getattr(gn, 'pracovat_vikendy', False):
                    je_mimo_prevadzky = True
                elif not getattr(gn, 'pracovat_sviatky', False):
                    sviatok_cache_key = f"isas_sla_sviatok_{dnes.isoformat()}"
                    je_sviatok = cache.get(sviatok_cache_key)
                    if je_sviatok is None:
                        je_sviatok = dnes in get_holidays_for_location(dnes)
                        cache.set(sviatok_cache_key, je_sviatok, 3600)
                    if je_sviatok:
                        je_mimo_prevadzky = True
                if not je_mimo_prevadzky and getattr(gn, 'pracovny_cas_od', None) and getattr(gn, 'pracovny_cas_do', None):
                    if cas_teraz < gn.pracovny_cas_od or cas_teraz > gn.pracovny_cas_do:
                        je_mimo_prevadzky = True

            if je_mimo_prevadzky:
                sla_status = 'MIMO_PREVADZKY'
                sla_sprava = 'Mimo prevádzkových hodín dispečingu (príjem dopytov online 24/7)'
            elif zataz_na_technika >= limit_kriticka:
                sla_status = 'KRITICKA'
                sla_sprava = getattr(nastavenia, 'sla_text_kriticka', None) or 'Vysoká vyťaženosť kapacít – prednostné riešenie urgentných požiadaviek'
            elif zataz_na_technika >= limit_zvysena:
                sla_status = 'ZVYSENA'
                sla_sprava = getattr(nastavenia, 'sla_text_zvysena', None) or 'Štandardná vyťaženosť kapacít – bežné lehoty vybavenia'
            else:
                sla_status = 'OPTIMALNA'
                sla_sprava = getattr(nastavenia, 'sla_text_optimalna', None) or 'Voľné kapacity – prijímame nové požiadavky'

            context['sla_telemetria'] = {
                'status': sla_status,
                'sprava': sla_sprava
            }

        # 🧭 DATA-DRIVEN DYNAMICKÁ NAVIGÁCIA (Single Source of Truth cez get_nav_katalog_menu)
        aktivne_bloky_set = get_active_web_blocks(cache_ver=cache_ver, is_preview=is_preview)
        context['nav_katalog_menu'] = get_nav_katalog_menu(cache_ver=cache_ver, is_preview=is_preview, aktivne_bloky=aktivne_bloky_set)
        context['nav_flags'] = get_web_nav_flags(cache_ver=cache_ver, is_preview=is_preview, aktivne_bloky=aktivne_bloky_set)

        # 🛒 BEZPEČNÉ PROSTREDIE KOŠÍKA (Data-Driven JSON bez rizika XSS a syntax erroru)
        b2b_klient_base, zlava_base = get_b2b_client_and_discount(self.request)
        dispecing_text = ""
        if globalne_nastavenia and globalne_nastavenia.pracovny_cas_od and globalne_nastavenia.pracovny_cas_do:
            vikend_str = "" if globalne_nastavenia.pracovat_vikendy else ", v pracovné dni"
            dispecing_text = f"Náš dispečing požiadavku spracuje v prevádzkovom čase ({globalne_nastavenia.pracovny_cas_od.strftime('%H:%M')} – {globalne_nastavenia.pracovny_cas_do.strftime('%H:%M')}{vikend_str})."

        context['cart_env'] = {
            'mena': context['mena'],
            'b2bZlava': float(Decimal(str(zlava_base or '0.00'))),
            'vyzadujeGatePass': bool(b2b_klient_base and getattr(b2b_klient_base, 'vyzaduje_povolenie_vstupu', False)),
            'dispecingInfo': dispecing_text
        }

        # 🏭 Dielenský servis pre dopytový košík: Globálne cachované pobočky (Zero Data Leak pre interné poznámky)
        strediska_cache_key = f'isas_web_servisne_strediska_v{cache_ver}'
        servisne_strediska = cache.get(strediska_cache_key)
        if servisne_strediska is None:
            FilialkaModel = apps.get_model('Evidencia', 'Filialka')
            if FilialkaModel:
                servisne_strediska = list(
                    FilialkaModel.objects.filter(je_aktivna=True, typ_skladu='BUDOVA')
                    .select_related('zodpovedny_zamestnanec')
                    .only(
                        'nazov', 'kod_pobocky', 'adresa', 'telefon', 'email', 'veduci_skladu',
                        'zodpovedny_zamestnanec__meno', 'zodpovedny_zamestnanec__priezvisko',
                        'zodpovedny_zamestnanec__telefon', 'zodpovedny_zamestnanec__email'
                    )
                    .order_by('nazov')
                )
            else:
                servisne_strediska = []
            cache.set(strediska_cache_key, servisne_strediska, 3600)
        context['servisne_strediska'] = servisne_strediska

        return context

class SluzbyListView(WebBaseMixin, ListView):
    model = TypServisuPrace
    template_name = 'web/sluzby_list.html'
    context_object_name = 'sluzby'
    
    def get_queryset(self):
        referencie_qs = ServisnyZaznam.objects.filter(
            zobrazit_na_webe=True,
            dokoncene=True
        ).exclude(slug__isnull=True).exclude(slug__exact='').filter(
            (Q(foto_po__isnull=False) & ~Q(foto_po='')) |
            (Q(foto_pred__isnull=False) & ~Q(foto_pred='')) |
            (Q(video__isnull=False) & ~Q(video=''))
        ).select_related('zariadenie', 'zariadenie__vyrobca', 'zakaznik').only(
            'id', 'foto_po', 'foto_pred', 'video', 'slug', 
            'verejny_popis_referencie', 'formalne_zhodnotenie_ai', 'hodnotenie_zakaznika',
            'stav_motohodin', 'motohodiny_v_zatazi', 'zmena_hardveru',
            'zariadenie', 'zariadenie__vyrobca__nazov_vyrobcu', 'zariadenie__model_zariadenia', 'zariadenie__nazov',
            'zakaznik__Nazov_spolocnosti', 'zakaznik__mesto', 'zakaznik__zobrazit_ako_referenciu',
            'protokol_uzavrety', 'bezpecnostny_hash'
        ).order_by('-vytvorene')

        return (
            TypServisuPrace.objects.filter(zobrazit_na_webe=True)
            .exclude(slug__isnull=True)
            .exclude(slug__exact='')
            .annotate(
                hodnotenie_priemer=Avg(
                    'servisnyzaznam__hodnotenie_zakaznika',
                    filter=Q(servisnyzaznam__zobrazit_na_webe=True, servisnyzaznam__hodnotenie_zakaznika__isnull=False)
                ),
                hodnotenie_pocet=Count(
                    'servisnyzaznam__id',
                    filter=Q(servisnyzaznam__zobrazit_na_webe=True, servisnyzaznam__hodnotenie_zakaznika__isnull=False),
                    distinct=True
                ),
                pocet_realizacii=Count(
                    'servisnyzaznam__id',
                    filter=Q(servisnyzaznam__dokoncene=True),
                    distinct=True
                )
            )
            .prefetch_related(
                Prefetch('servisnyzaznam_set', queryset=referencie_qs, to_attr='web_referencie')
            )
            .defer('hodinova_sadzba_naklad', 'fixny_naklad', 'pracovny_postup')
            .order_by('nazov')
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        
        # 🛡️ CMS INTEGRÁCIA: Načítanie bloku servisného paušálu (SLA) z ERP
        is_preview = context.get('is_preview', False)
        verzia_blokov = 'DRAFT' if is_preview else 'LIVE'
        WebBlokModel = apps.get_model('WebBuilder', 'WebBlok')
        context['sla_blok'] = WebBlokModel.objects.filter(
            typ='SLA_CALCULATOR', je_aktivny=True, verzia=verzia_blokov
        ).first() if WebBlokModel else None

        if 'mena' not in context:
            context['mena'] = get_firemna_mena()
        sadzba_dph = GlobalneNastavenia.get_zakladna_dph()
        context['sadzba_dph'] = sadzba_dph
        identita = context.get('identita')
        je_platca = identita.je_platca_dph if identita else False
        koef_dph = (Decimal('1.00') + (sadzba_dph / Decimal('100.00'))) if (je_platca and sadzba_dph > Decimal('0.00')) else None

        gn = context.get('globalne_nastavenia')
        standardna_sadzba_technik = gn.hodinova_sadzba_technik if (gn and gn.hodinova_sadzba_technik and gn.hodinova_sadzba_technik > Decimal('0.00')) else Decimal('0.00')
        b2b_klient, zlava = get_b2b_client_and_discount(self.request)
        zlava_dec = Decimal(str(zlava or '0.00'))
        context['is_b2b'] = zlava_dec > Decimal('0.00') or bool(b2b_klient)
        context['b2b_zlava'] = zlava_dec

        for sluzba in context['sluzby']:
            cena = Decimal('0.00')
            sluzba.pouzita_standardna_sadzba = False
            if sluzba.je_fakturovatelna:
                if sluzba.sposob_uctovania == 'HODINA':
                    if sluzba.hodinova_sadzba_predaj and sluzba.hodinova_sadzba_predaj > Decimal('0.00'):
                        cena = sluzba.hodinova_sadzba_predaj
                    elif standardna_sadzba_technik > Decimal('0.00'):
                        cena = standardna_sadzba_technik
                        sluzba.pouzita_standardna_sadzba = True
                elif sluzba.sposob_uctovania == 'FIX' and sluzba.fixna_cena_predaj and sluzba.fixna_cena_predaj > Decimal('0.00'):
                    cena = sluzba.fixna_cena_predaj

            sluzba.povodna_cena = None
            if zlava_dec > Decimal('0.00') and cena > Decimal('0.00'):
                sluzba.povodna_cena = cena.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                sluzba.cena_predaj = (cena * (Decimal('1.00') - (zlava_dec / Decimal('100.00')))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            else:
                sluzba.cena_predaj = cena

            if sluzba.cena_predaj > Decimal('0.00') and koef_dph:
                sluzba.cena_s_dph = (sluzba.cena_predaj * koef_dph).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            else:
                sluzba.cena_s_dph = None

            sluzba.orientacna_cena_celkom = None
            sluzba.orientacna_cena_celkom_s_dph = None
            if sluzba.je_fakturovatelna and sluzba.sposob_uctovania == 'HODINA' and cena > Decimal('0.00') and sluzba.normohodiny and sluzba.normohodiny > Decimal('0.00'):
                sluzba.orientacna_cena_celkom = (cena * sluzba.normohodiny).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                if koef_dph:
                    sluzba.orientacna_cena_celkom_s_dph = (sluzba.orientacna_cena_celkom * koef_dph).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            # 🚨 BIZNIS: Výpočet expresnej orientačnej ceny vrátane pohotovostného príplatku pre havarijné stavy
            sluzba.expresna_cena_celkom = None
            sluzba.expresna_cena_celkom_s_dph = None
            pohotovost_pct = gn.pohotovostny_priplatok if (gn and gn.pohotovostny_priplatok and gn.pohotovostny_priplatok > Decimal('0.00')) else Decimal('0.00')
            zaklad_pre_expres = sluzba.orientacna_cena_celkom if sluzba.orientacna_cena_celkom else (cena if sluzba.sposob_uctovania == 'FIX' and cena > Decimal('0.00') else None)
            if sluzba.priorita == 'VYSOKA' and pohotovost_pct > Decimal('0.00') and zaklad_pre_expres:
                koef_expres = Decimal('1.00') + (pohotovost_pct / Decimal('100.00'))
                sluzba.expresna_cena_celkom = (zaklad_pre_expres * koef_expres).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                if koef_dph:
                    sluzba.expresna_cena_celkom_s_dph = (sluzba.expresna_cena_celkom * koef_dph).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            if sluzba.hodnotenie_priemer is not None:
                sluzba.hodnotenie_priemer_zaokruhlene = round(float(sluzba.hodnotenie_priemer), 1)

            sluzba.ukazka_referencie = sluzba.web_referencie[0] if getattr(sluzba, 'web_referencie', None) else None

        if gn and koef_dph:
            if gn.sadzba_za_km_zakaznik and gn.sadzba_za_km_zakaznik > Decimal('0.00'):
                context['sadzba_za_km_s_dph'] = (gn.sadzba_za_km_zakaznik * koef_dph).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            if gn.sadzba_za_cas_na_ceste and gn.sadzba_za_cas_na_ceste > Decimal('0.00'):
                context['sadzba_za_cas_na_ceste_s_dph'] = (gn.sadzba_za_cas_na_ceste * koef_dph).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        # 🧭 DATA-DRIVEN FILTRE: Zostavenie dostupných filtrov podľa reálnych dát bez hardkódu
        sluzby_list = list(context['sluzby'])
        katalog_filtre = [
            {'kod': 'ALL', 'nazov': 'Všetky služby', 'ikona': '📋', 'pocet': len(sluzby_list)},
        ]
        count_havarijne = sum(1 for s in sluzby_list if s.priorita == 'VYSOKA')
        if count_havarijne > 0:
            katalog_filtre.append({'kod': 'VYSOKA', 'nazov': 'Havarijné a prioritné', 'ikona': '🚨', 'pocet': count_havarijne})

        count_standard = sum(1 for s in sluzby_list if s.priorita in ('STREDNA', 'NIZKA'))
        if count_standard > 0:
            katalog_filtre.append({'kod': 'STANDARD', 'nazov': 'Plánovaná údržba a servis', 'ikona': '🛠️', 'pocet': count_standard})

        count_fix = sum(1 for s in sluzby_list if s.je_fakturovatelna and s.sposob_uctovania == 'FIX' and s.cena_predaj > Decimal('0.00'))
        if count_fix > 0:
            katalog_filtre.append({'kod': 'FIX', 'nazov': 'Paušálne balíky (Fixná cena)', 'ikona': '🎯', 'pocet': count_fix})

        count_hodina = sum(1 for s in sluzby_list if s.je_fakturovatelna and s.sposob_uctovania == 'HODINA')
        if count_hodina > 0:
            katalog_filtre.append({'kod': 'HODINA', 'nazov': 'Sadzba podľa hodín', 'ikona': '⏱️', 'pocet': count_hodina})

        count_zaruka = sum(1 for s in sluzby_list if s.zaruka_mesiace and s.zaruka_mesiace > 0)
        if count_zaruka > 0:
            katalog_filtre.append({'kod': 'ZARUKA', 'nazov': 'S firemnou zárukou', 'ikona': '🛡️', 'pocet': count_zaruka})

        count_retrofit = sum(1 for s in sluzby_list if s.ukazka_referencie and getattr(s.ukazka_referencie, 'zmena_hardveru', False))
        if count_retrofit > 0:
            katalog_filtre.append({'kod': 'RETROFIT', 'nazov': 'Modernizácie a retrofity', 'ikona': '🚀', 'pocet': count_retrofit})

        count_vcene = sum(1 for s in sluzby_list if not s.je_fakturovatelna)
        if count_vcene > 0:
            katalog_filtre.append({'kod': 'VCENE', 'nazov': 'V cene servisu / Bezplatné', 'ikona': '🎁', 'pocet': count_vcene})

        context['katalog_filtre'] = katalog_filtre

        context['faqs'] = WebFAQ.objects.filter(
            je_aktivna=True, 
            kategoria__in=['SERVIS', 'TECHNOLOGIA']
        ).order_by('poradie', '-id')[:6]

        context['meter_unit'] = get_industry_term('meter_unit', 'hod.')

        # 🏷️ BIZNIS TRUST: Značky a technológie, ktoré firma certifikovane servisuje
        VyrobcaModel = apps.get_model('Evidencia', 'DatabazaVyrobcov')
        if VyrobcaModel:
            context['vyrobcovia'] = list(
                VyrobcaModel.objects.filter(zobrazit_na_webe=True)
                .exclude(logo_na_webe='')
                .exclude(logo_na_webe__isnull=True)
                .only('nazov_vyrobcu', 'logo_na_webe', 'domena')
                .order_by('nazov_vyrobcu')
            )
        else:
            context['vyrobcovia'] = []

        # 🏭 Dielenský servis: Načítanie aktívnych kamenných stredísk vrátane kódu pobočky (Zero Data Leak pre interné poznámky)
        FilialkaModel = apps.get_model('Evidencia', 'Filialka')
        if FilialkaModel:
            context['servisne_strediska'] = list(
                FilialkaModel.objects.filter(je_aktivna=True, typ_skladu='BUDOVA')
                .select_related('zodpovedny_zamestnanec')
                .only('nazov', 'kod_pobocky', 'adresa', 'telefon', 'email', 'veduci_skladu', 'zodpovedny_zamestnanec')
                .order_by('nazov')
            )
        else:
            context['servisne_strediska'] = []

        return context

class SluzbaDetailView(WebBaseMixin, DetailView):
    model = TypServisuPrace
    template_name = 'web/sluzba_detail.html'
    context_object_name = 'sluzba'

    def get_queryset(self):
        return (
            TypServisuPrace.objects.filter(zobrazit_na_webe=True)
            .exclude(slug__isnull=True)
            .exclude(slug__exact='')
            .defer('hodinova_sadzba_naklad', 'fixny_naklad')
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if 'mena' not in context:
            context['mena'] = get_firemna_mena()

        # 📸 ServisnyZaznam: Načítanie referencií vrátane odvetvových počítadiel a zariadenia bez N+1 dopytov (Zero Data Leak pre interné polia)
        context['referencie'] = list(
            ServisnyZaznam.objects.filter(typ_servisu=self.object, zobrazit_na_webe=True, dokoncene=True)
            .exclude(slug__isnull=True).exclude(slug__exact='')
            .filter(
                (Q(foto_po__isnull=False) & ~Q(foto_po='')) |
                (Q(foto_pred__isnull=False) & ~Q(foto_pred='')) |
                (Q(video__isnull=False) & ~Q(video=''))
            )
            .select_related('zariadenie', 'zariadenie__vyrobca', 'zakaznik')
            .defer(
                'koren_problemu', 'poruchy',
                'ai_analyza_historie', 'gps_suradnice',
                'podpis_zakaznika', 'podpis_technika', 'meno_podpisujuceho',
                'uzavrety_pdf_dokument'
            )
            .order_by('-vytvorene')[:3]
        )

        # ⭐ AGREGÁCIA HODNOTENÍ A REALIZÁCIÍ: Spokojnosť zákazníkov aj celkový objem overených prác
        agregacia_zaznamov = ServisnyZaznam.objects.filter(
            typ_servisu=self.object
        ).aggregate(
            priemer=Avg('hodnotenie_zakaznika', filter=Q(zobrazit_na_webe=True, hodnotenie_zakaznika__isnull=False)),
            pocet_hodnoteni=Count('id', filter=Q(zobrazit_na_webe=True, hodnotenie_zakaznika__isnull=False)),
            pocet_realizacii=Count('id', filter=Q(dokoncene=True))
        )
        if agregacia_zaznamov['pocet_hodnoteni'] and agregacia_zaznamov['pocet_hodnoteni'] > 0:
            context['hodnotenie_priemer'] = round(float(agregacia_zaznamov['priemer']), 1)
            context['hodnotenie_pocet'] = agregacia_zaznamov['pocet_hodnoteni']
        if agregacia_zaznamov['pocet_realizacii'] and agregacia_zaznamov['pocet_realizacii'] > 0:
            context['pocet_realizacii'] = agregacia_zaznamov['pocet_realizacii']

        # ⭐ OVERENÉ RECENZIE PRE SCHEMA.ORG: Zoznam reálnych referencií s hodnotením
        context['overene_recenzie'] = [
            r for r in context['referencie']
            if getattr(r, 'hodnotenie_zakaznika', None) and r.hodnotenie_zakaznika > 0
        ]

        # ❓ WebFAQ: Otázky a odpovede viazané na servis, technológiu a garanciu
        context['faqs'] = WebFAQ.objects.filter(
            je_aktivna=True, 
            kategoria__in=['SERVIS', 'TECHNOLOGIA', 'ZARUKA']
        ).order_by('poradie', '-id')[:6]

        # ⚖️ DPH & Cena: Výpočet pre fakturovateľné položky vrátane B2B zliav
        gn = context.get('globalne_nastavenia')
        b2b_klient, zlava = get_b2b_client_and_discount(self.request)
        zlava_dec = Decimal(str(zlava or '0.00'))
        context['is_b2b'] = zlava_dec > Decimal('0.00') or bool(b2b_klient)
        context['b2b_zlava'] = zlava_dec

        cena_predaj = Decimal('0.00')
        if self.object.je_fakturovatelna:
            if self.object.sposob_uctovania == 'HODINA':
                if self.object.hodinova_sadzba_predaj and self.object.hodinova_sadzba_predaj > Decimal('0.00'):
                    cena_predaj = self.object.hodinova_sadzba_predaj
                elif gn and gn.hodinova_sadzba_technik and gn.hodinova_sadzba_technik > Decimal('0.00'):
                    cena_predaj = gn.hodinova_sadzba_technik
                    context['pouzita_standardna_sadzba'] = True
            elif self.object.sposob_uctovania == 'FIX' and self.object.fixna_cena_predaj and self.object.fixna_cena_predaj > Decimal('0.00'):
                cena_predaj = self.object.fixna_cena_predaj

        context['povodna_cena'] = None
        if zlava_dec > Decimal('0.00') and cena_predaj > Decimal('0.00'):
            context['povodna_cena'] = cena_predaj.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            cena_predaj = (cena_predaj * (Decimal('1.00') - (zlava_dec / Decimal('100.00')))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        # 📏 INFORMATÍVNE PREDPÍSANÉ INTERVALY (Návrh 1 – Transparentná matica údržby bez hádania)
        PravidloModel = apps.get_model('Evidencia', 'PravidloServisnehoIntervalu')
        if PravidloModel:
            context['servisne_intervaly'] = list(
                PravidloModel.objects.filter(typ_servisu=self.object)
                .select_related('typ_servisu')
                .order_by('kategoria_zariadenia', 'vaha_hierarchie')
            )
        else:
            context['servisne_intervaly'] = []

        sadzba_dph = GlobalneNastavenia.get_zakladna_dph()
        context['sadzba_dph'] = sadzba_dph
        identita = context.get('identita') or FiremnaIdentita.objects.first()
        je_platca = identita.je_platca_dph if identita else False
        koef_dph = (Decimal('1.00') + (sadzba_dph / Decimal('100.00'))) if (je_platca and sadzba_dph > Decimal('0.00')) else None

        if cena_predaj > Decimal('0.00'):
            context['cena_predaj'] = cena_predaj
            if koef_dph:
                context['cena_s_dph'] = (cena_predaj * koef_dph).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            if self.object.sposob_uctovania == 'HODINA' and self.object.normohodiny and self.object.normohodiny > Decimal('0.00'):
                orientacna_cena = (cena_predaj * self.object.normohodiny).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                context['orientacna_cena_celkom'] = orientacna_cena
                if koef_dph:
                    context['orientacna_cena_celkom_s_dph'] = (orientacna_cena * koef_dph).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        # 🚨 BIZNIS: Výpočet orientačnej expresnej ceny pre detail služby
        pohotovost_pct = gn.pohotovostny_priplatok if (gn and gn.pohotovostny_priplatok and gn.pohotovostny_priplatok > Decimal('0.00')) else Decimal('0.00')
        zaklad_pre_expres = context.get('orientacna_cena_celkom') or (cena_predaj if self.object.sposob_uctovania == 'FIX' and cena_predaj > Decimal('0.00') else None)
        if self.object.priorita == 'VYSOKA' and pohotovost_pct > Decimal('0.00') and zaklad_pre_expres:
            koef_expres = Decimal('1.00') + (pohotovost_pct / Decimal('100.00'))
            expres_cena = (zaklad_pre_expres * koef_expres).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            context['expresna_cena_celkom'] = expres_cena
            if koef_dph:
                context['expresna_cena_celkom_s_dph'] = (expres_cena * koef_dph).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        elif not self.object.je_fakturovatelna:
            context['cena_predaj'] = Decimal('0.00')

        # 📋 SEO: Príprava krokov postupu pre Schema.org HowTo
        if self.object.zverejnit_postup_na_webe and self.object.pracovny_postup:
            postup_kroky = [line.strip() for line in self.object.pracovny_postup.splitlines() if line.strip()]
            if postup_kroky:
                context['postup_kroky'] = postup_kroky

        gn = context.get('globalne_nastavenia')
        if gn and koef_dph:
            if gn.sadzba_za_km_zakaznik and gn.sadzba_za_km_zakaznik > Decimal('0.00'):
                context['sadzba_za_km_s_dph'] = (gn.sadzba_za_km_zakaznik * koef_dph).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            if gn.sadzba_za_cas_na_ceste and gn.sadzba_za_cas_na_ceste > Decimal('0.00'):
                context['sadzba_za_cas_na_ceste_s_dph'] = (gn.sadzba_za_cas_na_ceste * koef_dph).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        context['meter_unit'] = get_industry_term('meter_unit', 'hod.')

        # 🏷️ BIZNIS TRUST: Značky technológií reálne servisované pri tejto službe (Overený sociálny dôkaz)
        VyrobcaModel = apps.get_model('Evidencia', 'DatabazaVyrobcov')
        if VyrobcaModel:
            vyrobcovia_ids = list(
                ServisnyZaznam.objects.filter(
                    typ_servisu=self.object,
                    zariadenie__vyrobca__isnull=False
                )
                .values_list('zariadenie__vyrobca_id', flat=True)
                .distinct()
            )
            vyrobcovia_qs = VyrobcaModel.objects.filter(zobrazit_na_webe=True).exclude(logo_na_webe='').exclude(logo_na_webe__isnull=True)
            if vyrobcovia_ids:
                vyrobcovia_qs = vyrobcovia_qs.filter(id__in=vyrobcovia_ids)

            context['vyrobcovia'] = list(
                vyrobcovia_qs.only('nazov_vyrobcu', 'logo_na_webe', 'domena').order_by('nazov_vyrobcu')
            )
        else:
            context['vyrobcovia'] = []

        # 🏭 Dielenský servis: Načítanie stredísk s príjmom do servisu vrátane kódu pobočky (Zero Data Leak pre interné poznámky)
        FilialkaModel = apps.get_model('Evidencia', 'Filialka')
        if FilialkaModel:
            context['servisne_strediska'] = list(
                FilialkaModel.objects.filter(je_aktivna=True, typ_skladu='BUDOVA')
                .select_related('zodpovedny_zamestnanec')
                .only('nazov', 'kod_pobocky', 'adresa', 'telefon', 'email', 'veduci_skladu', 'zodpovedny_zamestnanec')
                .order_by('nazov')
            )
        else:
            context['servisne_strediska'] = []

        return context

class ReferencieListView(WebBaseMixin, ListView):
    model = ServisnyZaznam
    template_name = 'web/referencie_list.html'
    context_object_name = 'referencie'
    paginate_by = 12
    
    def get_queryset(self):
        qs = (
            ServisnyZaznam.objects.filter(
                zobrazit_na_webe=True, dokoncene=True
            )
            .filter(
                (Q(foto_po__isnull=False) & ~Q(foto_po='')) |
                (Q(foto_pred__isnull=False) & ~Q(foto_pred='')) |
                (Q(video__isnull=False) & ~Q(video=''))
            )
            .exclude(slug__isnull=True).exclude(slug__exact='')
            .select_related('zariadenie', 'zariadenie__vyrobca', 'zakaznik')
            .prefetch_related('typ_servisu')
            .defer(
                'koren_problemu', 'ai_analyza_historie', 'gps_suradnice',
                'podpis_zakaznika', 'podpis_technika', 'meno_podpisujuceho',
                'uzavrety_pdf_dokument'
            )
            .order_by('-vytvorene')
        )
        sluzba_slug = self.request.GET.get('sluzba', '').strip()
        if sluzba_slug:
            qs = qs.filter(typ_servisu__slug=sluzba_slug)
        kategoria_kod = self.request.GET.get('kategoria', '').strip()
        if kategoria_kod:
            qs = qs.filter(zariadenie__kategoria=kategoria_kod)
        vyrobca_id = self.request.GET.get('vyrobca', '').strip()
        if vyrobca_id and vyrobca_id.isdigit():
            qs = qs.filter(zariadenie__vyrobca_id=int(vyrobca_id))
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        media_filter = (
            (Q(foto_po__isnull=False) & ~Q(foto_po='')) |
            (Q(foto_pred__isnull=False) & ~Q(foto_pred='')) |
            (Q(video__isnull=False) & ~Q(video=''))
        )
        slug_filter = ~Q(slug__isnull=True) & ~Q(slug__exact='')

        TypServisuModel = apps.get_model('Evidencia', 'TypServisuPrace')
        if TypServisuModel:
            context['dostupne_sluzby'] = TypServisuModel.objects.filter(
                zobrazit_na_webe=True,
                servisnyzaznam__zobrazit_na_webe=True,
                servisnyzaznam__dokoncene=True
            ).annotate(
                pocet=Count('servisnyzaznam', filter=Q(
                    servisnyzaznam__zobrazit_na_webe=True,
                    servisnyzaznam__dokoncene=True
                ) & Q(
                    (Q(servisnyzaznam__foto_po__isnull=False) & ~Q(servisnyzaznam__foto_po='')) |
                    (Q(servisnyzaznam__foto_pred__isnull=False) & ~Q(servisnyzaznam__foto_pred='')) |
                    (Q(servisnyzaznam__video__isnull=False) & ~Q(servisnyzaznam__video=''))
                ) & ~Q(servisnyzaznam__slug__isnull=True) & ~Q(servisnyzaznam__slug__exact=''), distinct=True)
            ).filter(pocet__gt=0).distinct().only('nazov', 'slug')
        else:
            context['dostupne_sluzby'] = []
        context['aktivna_sluzba'] = self.request.GET.get('sluzba', '').strip()

        # ⚙️ DATA-DRIVEN KATEGÓRIE TECHNOLÓGIÍ: Dynamické načítanie kategórií z reálne publikovaných realizácií s počtami
        ServisovaneZariadenieModel = apps.get_model('Evidencia', 'ServisovaneZariadenie')
        kat_field = ServisovaneZariadenieModel._meta.get_field('kategoria') if ServisovaneZariadenieModel else None
        kategorie_choices = dict(getattr(ServisovaneZariadenieModel, 'KATEGORIE', None) or (kat_field.choices if kat_field else []))
        pouzite_kategorie = (
            ServisnyZaznam.objects.filter(
                zobrazit_na_webe=True,
                dokoncene=True,
                zariadenie__isnull=False,
                zariadenie__kategoria__isnull=False
            )
            .filter(media_filter)
            .exclude(slug__isnull=True).exclude(slug__exact='')
            .values('zariadenie__kategoria')
            .annotate(pocet=Count('id'))
            .order_by('-pocet')
        )
        context['dostupne_kategorie'] = [
            {'kod': item['zariadenie__kategoria'], 'nazov': kategorie_choices.get(item['zariadenie__kategoria'], item['zariadenie__kategoria']), 'pocet': item['pocet']}
            for item in pouzite_kategorie if item['zariadenie__kategoria']
        ]
        context['aktivna_kategoria'] = self.request.GET.get('kategoria', '').strip()

        # 🏭 DATA-DRIVEN VÝROBCOVIA: Dynamický zoznam značiek z reálne publikovaných realizácií s počtami
        VyrobcaModel = apps.get_model('Evidencia', 'DatabazaVyrobcov')
        pouziti_vyrobcovia_ids = (
            ServisnyZaznam.objects.filter(
                zobrazit_na_webe=True,
                dokoncene=True,
                zariadenie__vyrobca__isnull=False,
                zariadenie__vyrobca__zobrazit_na_webe=True
            )
            .filter(media_filter)
            .exclude(slug__isnull=True).exclude(slug__exact='')
            .values_list('zariadenie__vyrobca_id', flat=True)
            .distinct()
        )
        if VyrobcaModel and pouziti_vyrobcovia_ids:
            context['dostupni_vyrobcovia'] = list(
                VyrobcaModel.objects.filter(id__in=pouziti_vyrobcovia_ids)
                .annotate(
                    pocet=Count('servisovanezariadenie__servisnyzaznam', filter=Q(
                        servisovanezariadenie__servisnyzaznam__zobrazit_na_webe=True,
                        servisovanezariadenie__servisnyzaznam__dokoncene=True
                    ) & Q(
                        (Q(servisovanezariadenie__servisnyzaznam__foto_po__isnull=False) & ~Q(servisovanezariadenie__servisnyzaznam__foto_po='')) |
                        (Q(servisovanezariadenie__servisnyzaznam__foto_pred__isnull=False) & ~Q(servisovanezariadenie__servisnyzaznam__foto_pred='')) |
                        (Q(servisovanezariadenie__servisnyzaznam__video__isnull=False) & ~Q(servisovanezariadenie__servisnyzaznam__video=''))
                    ) & ~Q(servisovanezariadenie__servisnyzaznam__slug__isnull=True) & ~Q(servisovanezariadenie__servisnyzaznam__slug__exact=''), distinct=True)
                )
                .filter(pocet__gt=0)
                .only('id', 'nazov_vyrobcu')
                .order_by('nazov_vyrobcu')
            )
        else:
            context['dostupni_vyrobcovia'] = []
        context['aktivny_vyrobca'] = self.request.GET.get('vyrobca', '').strip()
        context['has_active_filter'] = bool(
            context['aktivna_sluzba'] or context['aktivna_kategoria'] or context['aktivny_vyrobca']
        )
        return context

class ReferenciaDetailView(WebBaseMixin, DetailView):
    model = ServisnyZaznam
    template_name = 'web/referencia_detail.html'
    context_object_name = 'referencia'

    def get_queryset(self):
        ZamestnanecModel = apps.get_model('Evidencia', 'Zamestnanec')
        technici_qs = ZamestnanecModel.objects.filter(zobrazit_na_webe=True).only(
            'id', 'meno', 'priezvisko', 'profilova_fotka', 'verejna_rola', 'zobrazit_na_webe'
        ) if ZamestnanecModel else []

        return (
            ServisnyZaznam.objects.filter(
                zobrazit_na_webe=True, dokoncene=True
            )
            .filter(
                (Q(foto_po__isnull=False) & ~Q(foto_po='')) |
                (Q(foto_pred__isnull=False) & ~Q(foto_pred='')) |
                (Q(video__isnull=False) & ~Q(video=''))
            )
            .exclude(slug__isnull=True).exclude(slug__exact='')
            .select_related('zariadenie', 'zariadenie__vyrobca', 'zakaznik')
            .prefetch_related(
                'typ_servisu',
                Prefetch('technici', queryset=technici_qs)
            )
            .defer(
                'ai_analyza_historie', 'gps_suradnice',
                'podpis_zakaznika', 'podpis_technika', 'meno_podpisujuceho',
                'uzavrety_pdf_dokument'
            )
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['verejni_technici'] = [
            t for t in self.object.technici.all() if getattr(t, 'zobrazit_na_webe', False)
        ]

        # 🤝 BIZNIS CROSS-SELL: Prepojenie prípadovej štúdie s ponukou náhradného stroja z požičovne
        zariadenie = getattr(self.object, 'zariadenie', None)
        rental_stroj = None
        if zariadenie:
            if zariadenie.je_urcene_na_prenajom and zariadenie.zobrazit_na_webe_prenajom and zariadenie.slug:
                rental_stroj = zariadenie
            else:
                stroj_cls = apps.get_model('Evidencia', 'ServisovaneZariadenie')
                if stroj_cls:
                    rental_stroj = (
                        stroj_cls.objects.filter(
                            je_urcene_na_prenajom=True,
                            zobrazit_na_webe_prenajom=True,
                            kategoria=zariadenie.kategoria
                        )
                        .exclude(slug__isnull=True)
                        .exclude(slug='')
                        .select_related('vyrobca')
                        .first()
                    )
        context['rental_stroj'] = rental_stroj

        # 📚 BIZNIS RETENCIA: Súvisiace prípadové štúdie (rovnaká kategória stroja alebo typ servisu)
        suvisiace_qs = (
            ServisnyZaznam.objects.filter(
                zobrazit_na_webe=True, dokoncene=True
            )
            .exclude(pk=self.object.pk)
            .exclude(slug__isnull=True).exclude(slug__exact='')
            .select_related('zariadenie', 'zakaznik')
            .prefetch_related('typ_servisu')
            .defer(
                'ai_analyza_historie', 'gps_suradnice',
                'podpis_zakaznika', 'podpis_technika', 'meno_podpisujuceho',
                'uzavrety_pdf_dokument'
            )
        )
        zakaznik = getattr(self.object, 'zakaznik', None)
        suvisiace_referencie = []
        if zakaznik and getattr(zakaznik, 'zobrazit_ako_referenciu', False):
            # 🤝 B2B DÔVERA: Ak má klient povolené zverejnenie referencie, uprednostníme jeho ďalšie realizácie
            suvisiace_referencie = list(suvisiace_qs.filter(zakaznik=zakaznik)[:3])

        if len(suvisiace_referencie) < 3:
            zostavajuce_sloty = 3 - len(suvisiace_referencie)
            vylucene_ids = [self.object.pk] + [r.pk for r in suvisiace_referencie]
            dalsie_qs = suvisiace_qs.exclude(pk__in=vylucene_ids)
            if zariadenie and getattr(zariadenie, 'kategoria', None):
                suvisiace_referencie += list(dalsie_qs.filter(zariadenie__kategoria=zariadenie.kategoria)[:zostavajuce_sloty])
            else:
                sluzby_ids = list(self.object.typ_servisu.values_list('pk', flat=True))
                suvisiace_referencie += list(dalsie_qs.filter(typ_servisu__in=sluzby_ids).distinct()[:zostavajuce_sloty]) if sluzby_ids else list(dalsie_qs[:zostavajuce_sloty])

        context['suvisiace_referencie'] = suvisiace_referencie
        return context

class RentalListView(WebBaseMixin, TemplateView):
    template_name = 'web/rental_list.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        
        # 🛡️ CMS INTEGRÁCIA: Načítanie bloku výkupu z ERP podľa aktívnej verzie
        is_preview = context.get('is_preview', False)
        verzia_blokov = 'DRAFT' if is_preview else 'LIVE'
        WebBlokModel = apps.get_model('WebBuilder', 'WebBlok')
        context['buyback_blok'] = WebBlokModel.objects.filter(
            typ='BUYBACK', je_aktivny=True, verzia=verzia_blokov
        ).first() if WebBlokModel else None

        b2b_klient, zlava = get_b2b_client_and_discount(self.request)
        
        active_param_rels = get_active_parameter_reverse_names()
        rental_qs = ServisovaneZariadenie.objects.filter(
            je_urcene_na_prenajom=True, zobrazit_na_webe_prenajom=True
        ).exclude(slug__isnull=True).exclude(slug__exact='').select_related('vyrobca', 'zakaznik', *active_param_rels).only(
            'nazov', 'model_zariadenia', 'slug', 'kategoria', 'obrazok_na_webe', 'cena_prenajmu_na_den', 'v_prenajme_od_isas',
            'v_servisnom_rezime', 'umiestnenie', 'hmotnost_kg', 'rok_vyroby', 'aktualne_motohodiny', 'motohodiny_v_zatazi',
            'iot_je_aktivny', 'zaruka_do', 'predlzena_zaruka_do', 'vyrobca__nazov_vyrobcu',
            'zakaznik__Nazov_spolocnosti', 'zakaznik__zobrazit_ako_referenciu', *active_param_rels
        )

        # 🧭 DATA-DRIVEN SEGMENTÁCIA: Dynamické načítanie kategórií prítomných v požičovni
        vsetky_stroje = list(rental_qs)
        kategorie_map = {}
        for s in vsetky_stroje:
            if s.kategoria and s.kategoria not in kategorie_map:
                kategorie_map[s.kategoria] = s.get_kategoria_display()

        context['kategorie_flotily'] = [{'kod': k, 'nazov': v} for k, v in sorted(kategorie_map.items(), key=lambda x: x[1])]
        zvolena_kategoria = self.request.GET.get('kategoria', '').strip()
        context['zvolena_kategoria'] = zvolena_kategoria
        zvolene_stredisko = self.request.GET.get('stredisko', '').strip()
        context['zvolene_stredisko'] = zvolene_stredisko
        iba_volne = self.request.GET.get('iba_volne', '').strip() in ('1', 'true', 'ano')
        context['iba_volne'] = iba_volne
        zoradenie = self.request.GET.get('zoradenie', '').strip()
        context['zoradenie'] = zoradenie

        spracovane_stroje = [s for s in vsetky_stroje if s.kategoria == zvolena_kategoria] if (zvolena_kategoria and zvolena_kategoria in kategorie_map) else vsetky_stroje
        if zvolene_stredisko:
            stredisko_needle = zvolene_stredisko.lower()
            spracovane_stroje = [
                s for s in spracovane_stroje
                if s.umiestnenie and (stredisko_needle in s.umiestnenie.lower())
            ]

        # 📜 SOCIÁLNY DÔKAZ & TRACK RECORD: Načítanie referencií a počtu projektov (Zero Data Leak)
        stroj_ids = [s.pk for s in vsetky_stroje]
        HistoriaPrenajmuLogModel = apps.get_model('Evidencia', 'HistoriaPrenajmuLog')
        historia_referencie_map = {}
        historia_nasadeni_map = {}
        if HistoriaPrenajmuLogModel and stroj_ids:
            logs = HistoriaPrenajmuLogModel.objects.filter(
                zariadenie_id__in=stroj_ids
            ).filter(
                models.Q(novy_zakaznik__zobrazit_ako_referenciu=True) | models.Q(povodny_zakaznik__zobrazit_ako_referenciu=True)
            ).select_related('novy_zakaznik', 'povodny_zakaznik').only(
                'zariadenie_id', 'novy_zakaznik__Nazov_spolocnosti', 'novy_zakaznik__zobrazit_ako_referenciu',
                'povodny_zakaznik__Nazov_spolocnosti', 'povodny_zakaznik__zobrazit_ako_referenciu'
            )
            for log in logs:
                z_id = log.zariadenie_id
                if z_id not in historia_referencie_map:
                    historia_referencie_map[z_id] = set()
                if log.novy_zakaznik and getattr(log.novy_zakaznik, 'zobrazit_ako_referenciu', False):
                    historia_referencie_map[z_id].add(log.novy_zakaznik.Nazov_spolocnosti)
                if log.povodny_zakaznik and getattr(log.povodny_zakaznik, 'zobrazit_ako_referenciu', False):
                    historia_referencie_map[z_id].add(log.povodny_zakaznik.Nazov_spolocnosti)

            nasadenia = HistoriaPrenajmuLogModel.objects.filter(
                zariadenie_id__in=stroj_ids, typ_akcie='NASADENIE'
            ).values('zariadenie_id').annotate(pocet=models.Count('id'))
            for n in nasadenia:
                historia_nasadeni_map[n['zariadenie_id']] = n['pocet']

        rentals_grouped = {}
        for stroj in spracovane_stroje:
            if stroj.vyrobca:
                item_nazov = f"{stroj.vyrobca.nazov_vyrobcu} {stroj.model_zariadenia or stroj.nazov or ''}".strip()
            else:
                item_nazov = stroj.nazov or stroj.model_zariadenia or get_industry_term('zariadenie_fallback_nazov', 'Zariadenie')
            group_key = item_nazov

            if group_key not in rentals_grouped:
                zakladna_cena = stroj.cena_prenajmu_na_den
                zlava_dec = Decimal(str(zlava or '0.00'))
                cena_po_zlave = zakladna_cena * (Decimal('1.00') - (zlava_dec / Decimal('100.00'))) if zakladna_cena and zlava_dec > Decimal('0.00') else zakladna_cena
                
                rentals_grouped[group_key] = {
                    'pk': stroj.pk,
                    'nazov': group_key,
                    'slug': getattr(stroj, 'slug', ''),
                    'kategoria': stroj.get_kategoria_display(),
                    'obrazok_url': stroj.obrazok_na_webe.url if getattr(stroj, 'obrazok_na_webe', None) and stroj.obrazok_na_webe.name else None,
                    'cena_na_den': round(cena_po_zlave, 2) if cena_po_zlave else None,
                    'povodna_cena_na_den': round(zakladna_cena, 2) if zakladna_cena and zlava_dec > Decimal('0.00') else None,
                    'celkovy_pocet': 0,
                    'dostupne_pocet': 0,
                    'v_servise_pocet': 0,
                    'hmotnost_kg': stroj.hmotnost_kg,
                    'rok_vyroby': stroj.rok_vyroby,
                    'umiestnenie': stroj.umiestnenie,
                    'meter_display': stroj.meter_display,
                    'meter_load_display': stroj.meter_load_display,
                    'iot_je_aktivny': stroj.iot_je_aktivny,
                    'je_v_zaruke': stroj.je_v_zaruke,
                    'ui_parametre': stroj.ui_parametre[:3],
                    'referencie': set(),
                    'pocet_projektov': 0
                }

            rentals_grouped[group_key]['celkovy_pocet'] += 1
            rentals_grouped[group_key]['pocet_projektov'] += historia_nasadeni_map.get(stroj.pk, 0)

            if stroj.pk in historia_referencie_map:
                rentals_grouped[group_key]['referencie'].update(historia_referencie_map[stroj.pk])

            if not stroj.v_prenajme_od_isas and not stroj.v_servisnom_rezime:
                rentals_grouped[group_key]['dostupne_pocet'] += 1
                rentals_grouped[group_key]['slug'] = getattr(stroj, 'slug', '')
                rentals_grouped[group_key]['pk'] = stroj.pk
                if stroj.umiestnenie:
                    rentals_grouped[group_key]['umiestnenie'] = stroj.umiestnenie
            elif stroj.v_servisnom_rezime and not stroj.v_prenajme_od_isas:
                rentals_grouped[group_key]['v_servise_pocet'] += 1
            else:
                if stroj.zakaznik and getattr(stroj.zakaznik, 'zobrazit_ako_referenciu', False):
                    rentals_grouped[group_key]['referencie'].add(stroj.zakaznik.Nazov_spolocnosti)

        rental_flotila = list(rentals_grouped.values())
        for r in rental_flotila:
            r['referencie'] = list(r['referencie'])

        if iba_volne:
            rental_flotila = [r for r in rental_flotila if r.get('dostupne_pocet', 0) > 0]

        if zoradenie == 'cena_asc':
            rental_flotila.sort(key=lambda x: (x['cena_na_den'] is None, x['cena_na_den'] or Decimal('0.00'), x['nazov']))
        elif zoradenie == 'cena_desc':
            rental_flotila.sort(key=lambda x: (x['cena_na_den'] is None, -(x['cena_na_den'] or Decimal('0.00')), x['nazov']))
        elif zoradenie == 'dostupnost':
            rental_flotila.sort(key=lambda x: (-x['dostupne_pocet'], x['nazov']))
        else:
            rental_flotila.sort(key=lambda x: x['nazov'])

        WebFAQModel = apps.get_model('WebBuilder', 'WebFAQ')
        context['rental_faqs'] = list(
            WebFAQModel.objects.filter(je_aktivna=True, kategoria__in=['OBCHOD', 'SERVIS']).order_by('poradie', '-id')[:6]
        ) if WebFAQModel else []

        paginator = Paginator(rental_flotila, 9)
        page_number = self.request.GET.get('page')
        try:
            page_obj = paginator.get_page(page_number)
        except Exception:
            page_obj = paginator.get_page(1)

        context['is_paginated'] = page_obj.has_other_pages()
        context['page_obj'] = page_obj
        context['stroje'] = page_obj.object_list
        return context

class RentalDetailView(WebBaseMixin, DetailView):
    model = ServisovaneZariadenie
    template_name = 'web/rental_detail.html'
    context_object_name = 'stroj'

    def get_queryset(self):
        # 🛡️ BEZPEČNOSŤ: Zamedzí zobrazeniu privátnych strojov zákazníkov cez prenájmovú trasu
        return ServisovaneZariadenie.objects.filter(je_urcene_na_prenajom=True, zobrazit_na_webe_prenajom=True)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        b2b_klient, zlava = get_b2b_client_and_discount(self.request)

        stroj = self.object
        zakladna_cena = stroj.cena_prenajmu_na_den
        zlava_dec = Decimal(str(zlava or '0.00'))
        if zakladna_cena and zlava_dec > Decimal('0.00'):
            context['cena_na_den'] = round(zakladna_cena * (Decimal('1.00') - (zlava_dec / Decimal('100.00'))), 2)
            context['povodna_cena_na_den'] = round(zakladna_cena, 2)
        else:
            context['cena_na_den'] = zakladna_cena
            context['povodna_cena_na_den'] = None

        context['is_b2b'] = zlava_dec > Decimal('0.00')

        # 🚜 FLOTILOVÝ POOLING: Výpočet reálnej dostupnosti celej flotily s ochranou pred servisným režimom
        flotila_filter = {
            'je_urcene_na_prenajom': True,
            'zobrazit_na_webe_prenajom': True,
        }
        if stroj.vyrobca:
            flotila_filter['vyrobca'] = stroj.vyrobca
            flotila_filter['model_zariadenia'] = stroj.model_zariadenia
        elif stroj.model_zariadenia:
            flotila_filter['model_zariadenia'] = stroj.model_zariadenia
        else:
            flotila_filter['nazov'] = stroj.nazov
        flotila_qs = ServisovaneZariadenie.objects.filter(**flotila_filter)
        context['flotila_celkovy_pocet'] = flotila_qs.count()
        context['flotila_dostupne_pocet'] = flotila_qs.filter(v_prenajme_od_isas=False, v_servisnom_rezime=False).count()
        context['flotila_v_servise_pocet'] = flotila_qs.filter(v_prenajme_od_isas=False, v_servisnom_rezime=True).count()
        context['je_dostupny_stroj'] = context['flotila_dostupne_pocet'] > 0
        context['stroj_v_servise'] = bool(stroj.v_servisnom_rezime and not stroj.v_prenajme_od_isas)

        # 🛡️ TECHNICKÁ PRIPRAVENOSŤ & COMPLIANCE: Overenie zákonných inšpekcií a servisu
        ServisnyZaznamModel = apps.get_model('Evidencia', 'ServisnyZaznam')
        posledny_servis = ServisnyZaznamModel.objects.filter(
            zariadenie=stroj, dokoncene=True
        ).order_by('-vytvorene').only('vytvorene').first() if ServisnyZaznamModel else None
        context['posledna_kontrola_datum'] = posledny_servis.vytvorene.date() if posledny_servis and posledny_servis.vytvorene else None

        audit_polozky = get_zariadenie_compliance_audit(stroj)
        ma_neplatnu_reviziu = any(p.get('stav') in ('EXPIROVANE', 'KRITICKE') for p in audit_polozky) if audit_polozky else False
        context['technicka_sposobilost_overena'] = (not ma_neplatnu_reviziu) if audit_polozky else (context['posledna_kontrola_datum'] is not None)
        context['platne_revizie'] = [
            p for p in audit_polozky
            if p.get('stav') in ('PLATNE', 'V_NORME', 'OK') and p.get('nazov')
        ] if audit_polozky else []

        # 📜 SOCIÁLNY DÔKAZ & HISTÓRIA NASADENÍ: (Zero Data Leak)
        referencie_set = set()
        if stroj.v_prenajme_od_isas and stroj.zakaznik and getattr(stroj.zakaznik, 'zobrazit_ako_referenciu', False):
            referencie_set.add(stroj.zakaznik.Nazov_spolocnosti)
        HistoriaPrenajmuLogModel = apps.get_model('Evidencia', 'HistoriaPrenajmuLog')
        if HistoriaPrenajmuLogModel:
            context['pocet_realizovanych_projektov'] = HistoriaPrenajmuLogModel.objects.filter(
                zariadenie=stroj, typ_akcie='NASADENIE'
            ).count()
            logs = HistoriaPrenajmuLogModel.objects.filter(
                zariadenie=stroj
            ).filter(
                models.Q(novy_zakaznik__zobrazit_ako_referenciu=True) | models.Q(povodny_zakaznik__zobrazit_ako_referenciu=True)
            ).select_related('novy_zakaznik', 'povodny_zakaznik').only(
                'novy_zakaznik__Nazov_spolocnosti', 'novy_zakaznik__zobrazit_ako_referenciu',
                'povodny_zakaznik__Nazov_spolocnosti', 'povodny_zakaznik__zobrazit_ako_referenciu'
            )
            for log in logs:
                if log.novy_zakaznik and getattr(log.novy_zakaznik, 'zobrazit_ako_referenciu', False):
                    referencie_set.add(log.novy_zakaznik.Nazov_spolocnosti)
                if log.povodny_zakaznik and getattr(log.povodny_zakaznik, 'zobrazit_ako_referenciu', False):
                    referencie_set.add(log.povodny_zakaznik.Nazov_spolocnosti)
        else:
            context['pocet_realizovanych_projektov'] = 0
        context['overene_referencie'] = sorted(list(referencie_set))

        # 📍 LOKALIZÁCIA: Priradenie domovského expedičného strediska podľa poľa umiestnenie
        domovske_stredisko = None
        if stroj.umiestnenie:
            um_lower = stroj.umiestnenie.lower()
            for s in context.get('servisne_strediska', []):
                if (s.nazov and s.nazov.lower() in um_lower) or (s.kod_pobocky and s.kod_pobocky.lower() in um_lower):
                    domovske_stredisko = s
                    break
        context['domovske_stredisko'] = domovske_stredisko

        # 🏋️ BIZNIS TELEMETRIA: Pomer vyťaženia v záťaži (%)
        mh_celkom = getattr(stroj, 'aktualne_motohodiny', 0) or 0
        mh_zataz = getattr(stroj, 'motohodiny_v_zatazi', 0) or 0
        if mh_celkom > 0 and mh_zataz > 0:
            is_day_meter = get_industry_term('meter_unit', '').strip().lower() in ('dní', 'dni', 'd', 'days', 'day')
            is_hour_load = get_industry_term('meter_load_unit', '').strip().lower() in ('h', 'hod.', 'hod', 'hours', 'hour')
            celkovy_cas_h = (mh_celkom * 24) if (is_day_meter and is_hour_load) else mh_celkom
            context['pomer_zataze_percenta'] = min(100.0, round((mh_zataz / celkovy_cas_h) * 100, 1)) if celkovy_cas_h > 0 else None
        else:
            context['pomer_zataze_percenta'] = None

        # 🧮 PREPOČET PRE CENOVÝ KALKULÁTOR NÁJMU S DPH
        GlobalneNastaveniaModel = apps.get_model('ISASaplikacia', 'GlobalneNastavenia')
        zakladna_dph = GlobalneNastaveniaModel.get_zakladna_dph() if GlobalneNastaveniaModel else Decimal('0.00')
        je_platca = bool(context.get('identita') and getattr(context['identita'], 'je_platca_dph', False))
        dph_koef = float(Decimal('1.00') + (zakladna_dph / Decimal('100.00'))) if je_platca else 1.0

        context['dph_sadzba_percent'] = float(zakladna_dph) if je_platca else 0.0
        context['rental_calc_config'] = {
            'cena_na_den': float(context['cena_na_den']) if context.get('cena_na_den') else 0.0,
            'mena': str(context.get('mena', '')),
            'dph_koeficient': dph_koef,
            'je_platca_dph': je_platca,
        }

        # 🛡️ DEFENZÍVNE VYLUČOVANIE: Ochrana pred nechceným vyradením všetkých cloudových inštancií bez výrobcu v SQL
        alt_exclude = models.Q(pk=stroj.pk)
        if stroj.vyrobca and stroj.model_zariadenia:
            alt_exclude |= models.Q(vyrobca=stroj.vyrobca, model_zariadenia=stroj.model_zariadenia)
        elif stroj.nazov:
            alt_exclude |= models.Q(nazov=stroj.nazov)

        alternativy_qs = ServisovaneZariadenie.objects.filter(
            kategoria=stroj.kategoria,
            je_urcene_na_prenajom=True,
            zobrazit_na_webe_prenajom=True,
            v_prenajme_od_isas=False,
            v_servisnom_rezime=False
        ).exclude(alt_exclude).select_related('vyrobca').only(
            'nazov', 'model_zariadenia', 'slug', 'kategoria', 'obrazok_na_webe', 'cena_prenajmu_na_den',
            'vyrobca__nazov_vyrobcu', 'hmotnost_kg', 'rok_vyroby'
        )
        alternativy_map = {}
        for alt in alternativy_qs:
            alt_nazov = alt.nazov or alt.model_zariadenia or get_industry_term('zariadenie_fallback_nazov', 'Zariadenie')
            alt_key = f"{alt.vyrobca.nazov_vyrobcu} {alt_nazov}".strip() if alt.vyrobca else alt_nazov
            if alt_key not in alternativy_map:
                alt_cena = alt.cena_prenajmu_na_den
                alt_cena_po_zlave = alt_cena * (Decimal('1.00') - (zlava_dec / Decimal('100.00'))) if alt_cena and zlava_dec > Decimal('0.00') else alt_cena
                alternativy_map[alt_key] = {
                    'nazov': alt_key,
                    'slug': alt.slug,
                    'cena_na_den': round(alt_cena_po_zlave, 2) if alt_cena_po_zlave else None,
                    'obrazok_na_webe': alt.obrazok_na_webe,
                    'hmotnost_kg': alt.hmotnost_kg,
                    'rok_vyroby': alt.rok_vyroby,
                    'kategoria': alt.get_kategoria_display(),
                }
            if len(alternativy_map) >= 3:
                break
        context['alternativne_stroje'] = list(alternativy_map.values())

        # 🛠️ NÁVRH 2: DOPLNKOVÉ SLUŽBY K PRENÁJMU (Inštalácia, Zaškolenie, Montáž)
        TypServisuPraceModel = apps.get_model('Evidencia', 'TypServisuPrace')
        if TypServisuPraceModel:
            sluzby_raw = TypServisuPraceModel.objects.filter(zobrazit_na_webe=True, je_fakturovatelna=True).only(
                'nazov', 'sposob_uctovania', 'normohodiny', 'hodinova_sadzba_predaj', 'fixna_cena_predaj', 'popis_na_webe'
            )[:4]
            doplnkove_sluzby = []
            for sl in sluzby_raw:
                zaklad_cena = sl.fixna_cena_predaj if sl.sposob_uctovania == 'FIX' else sl.hodinova_sadzba_predaj
                sl_cena_po_zlave = zaklad_cena * (Decimal('1.00') - (zlava_dec / Decimal('100.00'))) if zaklad_cena and zlava_dec > Decimal('0.00') else zaklad_cena
                doplnkove_sluzby.append({
                    'pk': sl.pk,
                    'nazov': sl.nazov,
                    'popis': sl.popis_na_webe,
                    'sposob_uctovania': sl.get_sposob_uctovania_display(),
                    'cena': round(sl_cena_po_zlave, 2) if sl_cena_po_zlave else None,
                })
            context['doplnkove_sluzby'] = doplnkove_sluzby
        else:
            context['doplnkove_sluzby'] = []

        # ❓ NÁVRH 5: FAQ K PRENÁJMU A PODMIENKAM ZÁRUKY
        WebFAQModel = apps.get_model('WebBuilder', 'WebFAQ')
        context['rental_faqs'] = list(
            WebFAQModel.objects.filter(je_aktivna=True, kategoria__in=['OBCHOD', 'SERVIS']).order_by('poradie', '-id')[:6]
        ) if WebFAQModel else []

        return context

class ReklamaciaView(WebBaseMixin, TemplateView):
    """🛡️ ISAS BIZNIS: Samostatná podstránka pre Garančný servis a reklamácie (RMA)."""
    template_name = 'web/reklamacia.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['titulok_stranky'] = "Garančný servis a reklamácie (RMA)"
        context['canonical_url'] = self.request.build_absolute_uri(self.request.path)

        b2b_klient, _ = get_b2b_client_and_discount(self.request)
        context['b2b_klient'] = b2b_klient

        # 🛡️ DATA-DRIVEN RMA & ZERO DATA LEAK: Sprístupnenie evidovaných strojov, faktúr, zákaziek a stavu konaní prihlásenému klientovi
        if b2b_klient:
            UlohaModel = apps.get_model('Evidencia', 'Uloha')
            otvoreny_servis_subquery = UlohaModel.objects.filter(zariadenia=models.OuterRef('pk'), dokoncene=False) if UlohaModel else None

            active_param_rels = get_active_parameter_reverse_names()
            zariadenia_qs = ServisovaneZariadenie.objects.filter(zakaznik=b2b_klient).select_related('vyrobca', *active_param_rels)
            if otvoreny_servis_subquery is not None:
                zariadenia_qs = zariadenia_qs.annotate(ma_otvoreny_servis=models.Exists(otvoreny_servis_subquery))

            context['klient_zariadenia'] = list(
                zariadenia_qs.only(
                    'id', 'kategoria', 'model_zariadenia', 'vyrobne_cislo', 'nazov', 
                    'vyrobca__nazov_vyrobcu', 'zaruka_do', 'predlzena_zaruka_do', 
                    'bezpecnostny_kluc', 'umiestnenie', 'aktualne_motohodiny', 
                    'motohodiny_v_zatazi', 'rok_vyroby', 'datum_instalacie', 'v_servisnom_rezime',
                    *active_param_rels
                )
                .order_by('nazov', 'model_zariadenia')
            )
            StatLokalizaciaModel = apps.get_model('ISASaplikacia', 'StatLokalizacia')
            stat_obj = None
            if getattr(b2b_klient, 'stat', None) and StatLokalizaciaModel:
                iso_klienta = StatLokalizaciaModel.rozpoznaj_iso(b2b_klient.stat)
                stat_obj = StatLokalizaciaModel.get_for_iso(iso_klienta)

            gn_obj = context.get('globalne_nastavenia')
            context['platna_reklamacna_lehota'] = (
                getattr(b2b_klient, 'reklamacna_lehota_dni', None) or
                (getattr(stat_obj, 'lehota_reklamacie_dni', None) if stat_obj else None) or
                (getattr(gn_obj, 'standardna_lehota_reklamacie_dni', None) if gn_obj else None)
            )

            dnes = timezone.now().date()
            context['klient_stroje_v_zaruke'] = list(
                zariadenia_qs.filter(
                    models.Q(zaruka_do__gte=dnes) | models.Q(predlzena_zaruka_do__gte=dnes)
                )
                .only(
                    'id', 'kategoria', 'model_zariadenia', 'vyrobne_cislo', 'nazov',
                    'vyrobca__nazov_vyrobcu', 'zaruka_do', 'predlzena_zaruka_do',
                    'bezpecnostny_kluc', 'umiestnenie', 'aktualne_motohodiny',
                    'motohodiny_v_zatazi', 'rok_vyroby', 'datum_instalacie', 'v_servisnom_rezime',
                    *active_param_rels
                )
                .order_by('zaruka_do', 'predlzena_zaruka_do')
            )

            context['klient_faktury'] = list(
                VystavenaFaktura.objects.filter(zakaznik=b2b_klient, typ_dokladu='FAKTURA')
                .exclude(cislo_faktury__isnull=True).exclude(cislo_faktury__exact='')
                .only('id', 'cislo_faktury', 'datum_vystavenia', 'variabilny_symbol', 'zariadenie_id')
                .order_by('-datum_vystavenia')[:20]
            )
            context['klient_objednavky'] = list(
                PrijataObjednavka.objects.filter(zakaznik=b2b_klient)
                .exclude(cislo_objednavky_interne__isnull=True).exclude(cislo_objednavky_interne__exact='')
                .only('id', 'cislo_objednavky_interne', 'cislo_objednavky_klienta', 'datum_prijatia')
                .order_by('-datum_prijatia')[:20]
            )
            reklamacie_raw = list(
                PrijataObjednavka.objects.filter(
                    models.Q(zakaznik=b2b_klient) & (models.Q(poznamka_klienta__icontains="REKLAMÁC") | models.Q(vytvorene_ulohy__je_reklamacia=True)),
                    stav__in=['NOVA', 'SCHVALENA']
                )
                .distinct()
                .prefetch_related(
                    'zariadenia__vyrobca',
                    'vytvorene_ulohy__servisne_zaznamy',
                    'vytvorene_ulohy__technici'
                )
                .only(
                    'id', 'cislo_objednavky_interne', 'cislo_objednavky_klienta', 
                    'poznamka_klienta', 'stav', 'priorita', 'datum_prijatia', 
                    'termin_dodania', 'subor_objednavky'
                )
                .order_by('-datum_prijatia')[:10]
            )
            for rek in reklamacie_raw:
                d_termin = to_date(rek.termin_dodania)
                if d_termin:
                    rozdiel = (d_termin - dnes).days
                    rek.je_po_termine = (rozdiel < 0)
                    rek.dni_do_terminu = abs(rozdiel)
                else:
                    rek.je_po_termine = False
                    rek.dni_do_terminu = None
            context['klient_reklamacie'] = reklamacie_raw

            # 🛡️ ZÁRUKY NA VYKONANÉ PRÁCE (TypServisuPrace.zaruka_mesiace):
            ServisnyZaznamModel = apps.get_model('Evidencia', 'ServisnyZaznam')
            nedavne_servisy = list(
                ServisnyZaznamModel.objects.filter(
                    zakaznik=b2b_klient,
                    dokoncene=True,
                    typ_servisu__zaruka_mesiace__gt=0
                )
                .select_related('zariadenie')
                .prefetch_related('typ_servisu')
                .distinct()
                .order_by('-vytvorene')[:15]
            ) if ServisnyZaznamModel else []

            # 🚀 O(1) ELIMINÁCIA N+1: Jednorazové očíslovanie protokolov klienta v pamäti
            if nedavne_servisy and b2b_klient:
                vsetky_id_klienta = list(
                    ServisnyZaznamModel.objects.filter(zakaznik=b2b_klient)
                    .order_by('id')
                    .values_list('id', flat=True)
                )
                id_k_poradiu = {zid: idx + 1 for idx, zid in enumerate(vsetky_id_klienta)}
                for s_obj in nedavne_servisy:
                    s_obj._poradove_cislo_cache = id_k_poradiu.get(s_obj.id, s_obj.id)

            aktivne_zaruky_prace = []
            dnes = timezone.now().date()
            for sz in nedavne_servisy:
                d_servisu = sz.vytvorene.date() if sz.vytvorene else None
                if not d_servisu:
                    continue
                for ts in sz.typ_servisu.all():
                    if ts.zaruka_mesiace > 0:
                        koniec_zaruky = add_calendar_interval(d_servisu, months=ts.zaruka_mesiace)
                        if koniec_zaruky and koniec_zaruky >= dnes:
                            aktivne_zaruky_prace.append({
                                'servisny_zaznam_id': sz.id,
                                'poradove_cislo': sz.poradove_cislo,
                                'typ_prace': ts.nazov,
                                'zariadenie': sz.zariadenie,
                                'datum_servisu': d_servisu,
                                'zaruka_do': koniec_zaruky,
                                'mesiace': ts.zaruka_mesiace,
                            })
            context['aktivne_zaruky_prace'] = aktivne_zaruky_prace

        WebFAQModel = apps.get_model('WebBuilder', 'WebFAQ') if apps.is_installed('WebBuilder') else None
        if WebFAQModel:
            context['reklamacne_faq'] = list(
                WebFAQModel.objects.filter(je_aktivna=True, kategoria='ZARUKA').only('otazka', 'odpoved', 'slug', 'poradie').order_by('poradie', '-id')
            )

        gn = context.get('globalne_nastavenia')
        identita = context.get('identita')
        context['email_reklamacie'] = getattr(gn, 'email_pre_reklamacie', None) or getattr(identita, 'email_vseobecny', None)
        context['compliance_disclaimer'] = get_legal_disclaimer()

        # 🛡️ LIVE DISPATCH TELEMETRY: Kontextové operatívne stavy dispečingu pre RMA bez duplicity zmluvných lehôt
        if context.get('sla_telemetria') and isinstance(context['sla_telemetria'], dict):
            raw_status = context['sla_telemetria'].get('status')

            if raw_status == 'KRITICKA':
                rma_sprava = "Garančný dispečing: Zvýšený počet hlásení – záručné a prioritné incidenty riešime s najvyššou prioritou."
            elif raw_status == 'ZVYSENA':
                rma_sprava = "Garančný dispečing v štandardnom režime – požiadavky priebežne priraďujeme servisným technikom."
            elif raw_status == 'OPTIMALNA':
                rma_sprava = "Garančný dispečing online – voľné technické kapacity pripravené na okamžitý servisný zásah."
            elif raw_status == 'MIMO_PREVADZKY':
                rma_sprava = "Príjem požiadaviek 24/7 – havarijný dispečing podnet spracuje v najbližšom prevádzkovom termíne."
            else:
                rma_sprava = context['sla_telemetria'].get('sprava')

            context['sla_telemetria'] = {
                'status': raw_status,
                'sprava': rma_sprava
            }

        return context

class EshopListView(WebBaseMixin, TemplateView):
    template_name = 'web/eshop_list.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        
        b2b_klient, zlava = get_b2b_client_and_discount(self.request)
        
        # 🔍 Získanie hľadaného výrazu (zachovanie pôvodného formátu pre UI)
        search_query = self.request.GET.get('q', '').strip()
        
        eshop_diely = []
        povolene_modely = get_eshop_models()

        def_dph_shop = GlobalneNastavenia.get_zakladna_dph()

        for model in povolene_modely:
            # 🚀 DB FILTER: Defenzívna kontrola polí modelu pred zostavením Q filtrov a .only()
            model_fields = {f.name for f in model._meta.get_fields()}
            is_preview = context.get('is_preview', False) or _is_staff_user(self.request.user)
            if 'slug' not in model_fields or (not is_preview and 'zobrazit_na_webe' not in model_fields):
                continue
            diel_filter = {} if is_preview else {'zobrazit_na_webe': True}
            qs = model.objects.filter(**diel_filter).exclude(slug__isnull=True).exclude(slug__exact='')
            if search_query:
                q_conditions = []
                if 'obchodny_nazov' in model_fields:
                    q_conditions.append(Q(obchodny_nazov__icontains=search_query))
                if 'seriove_cislo' in model_fields:
                    q_conditions.append(Q(seriove_cislo__icontains=search_query))
                if 'katalogove_cislo' in model_fields:
                    q_conditions.append(Q(katalogove_cislo__icontains=search_query))
                if 'ean_kod' in model_fields:
                    q_conditions.append(Q(ean_kod__icontains=search_query))
                if 'vyrobca' in model_fields:
                    if 'skryt_vyrobcu' in model_fields:
                        q_conditions.append(Q(vyrobca__nazov_vyrobcu__icontains=search_query, skryt_vyrobcu=False))
                    else:
                        q_conditions.append(Q(vyrobca__nazov_vyrobcu__icontains=search_query))
                if q_conditions:
                    final_q = q_conditions[0]
                    for cond in q_conditions[1:]:
                        final_q |= cond
                    qs = qs.filter(final_q)
                else:
                    qs = qs.none()

            candidate_fields = [
                'id', 'obchodny_nazov', 'slug', 'seriove_cislo', 'katalogove_cislo', 
                'ean_kod', 'popis_na_webe', 'obrazok_na_webe', 'merna_jednotka', 
                'pocet_na_sklade', 'sadzba_dph', 'skryt_vyrobcu', 'typ'
            ]
            only_fields = [f for f in candidate_fields if f in model_fields]
            if 'vyrobca' in model_fields:
                only_fields.append('vyrobca')

            qs_select = qs.select_related('vyrobca') if 'vyrobca' in model_fields else qs
            diely_na_webe = list(qs_select.only(*only_fields))

            if not diely_na_webe: continue
                
            ct = ContentType.objects.get_for_model(model)
            diely_ids = [d.id for d in diely_na_webe]
            ceny = CenovyKatalog.objects.filter(content_type=ct, object_id__in=diely_ids).values('object_id', 'predajna_cena')
            ceny_mapa = {c['object_id']: c['predajna_cena'] for c in ceny}
            
            for diel in diely_na_webe:
                raw_zakladna_cena = ceny_mapa.get(diel.id)
                zakladna_cena = Decimal(str(raw_zakladna_cena)) if raw_zakladna_cena is not None else None
                nazov = getattr(diel, 'obchodny_nazov', None) or getattr(diel, 'seriove_cislo', '') or get_industry_term('polozka_fallback_nazov', 'Položka')
                katalogove_cislo = getattr(diel, 'katalogove_cislo', '') or ""
                zlava_dec = Decimal(str(zlava or '0.00'))

                if zakladna_cena is not None and zakladna_cena > Decimal('0.00'):
                    predajna_cena = zakladna_cena * (Decimal('1.00') - (zlava_dec / Decimal('100.00'))) if zlava_dec > Decimal('0.00') else zakladna_cena
                    diel_dph = getattr(diel, 'sadzba_dph', None)
                    dph_raw = diel_dph if (diel_dph is not None and str(diel_dph).strip() != '') else (def_dph_shop or '0.00')
                    try:
                        sadzba_dph = Decimal(str(dph_raw))
                    except (InvalidOperation, TypeError, ValueError):
                        sadzba_dph = Decimal('0.00')
                    cena_s_dph = predajna_cena * (Decimal('1.00') + (sadzba_dph / Decimal('100.00')))
                    cena_bez_dph_val = predajna_cena.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    povodna_cena_val = zakladna_cena.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if zlava_dec > Decimal('0.00') else None
                    cena_s_dph_val = cena_s_dph.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                else:
                    cena_bez_dph_val = None
                    povodna_cena_val = None
                    cena_s_dph_val = None

                img_url = diel.obrazok_na_webe.url if getattr(diel, 'obrazok_na_webe', None) and diel.obrazok_na_webe.name else None

                eshop_diely.append({
                    'nazov': nazov,
                    'obchodny_nazov': getattr(diel, 'obchodny_nazov', ''),
                    'seriove_cislo': getattr(diel, 'seriove_cislo', ''),
                    'slug': getattr(diel, 'slug', ''),
                    'katalogove_cislo': katalogove_cislo,
                    'ean_kod': getattr(diel, 'ean_kod', '') or '',
                    'vyrobca_nazov': diel.vyrobca.nazov_vyrobcu if (getattr(diel, 'vyrobca', None) and not getattr(diel, 'skryt_vyrobcu', False)) else '',
                    'skryt_vyrobcu': getattr(diel, 'skryt_vyrobcu', False),
                    'typ': getattr(diel, 'typ', '') or '',
                    'popis': getattr(diel, 'popis_na_webe', '') or '',
                    'popis_na_webe': getattr(diel, 'popis_na_webe', '') or '',
                    'obrazok_url': img_url,
                    'cena_bez_dph': cena_bez_dph_val,
                    'povodna_cena_bez_dph': povodna_cena_val,
                    'cena_s_dph': cena_s_dph_val,
                    'merna_jednotka': getattr(diel, 'merna_jednotka', ''),
                    'pocet_na_sklade': getattr(diel, 'pocet_na_sklade', 0),
                    'skladom': (getattr(diel, 'pocet_na_sklade', 0) or 0) > 0
                })
        
        eshop_diely = sorted(eshop_diely, key=lambda x: str(x.get('nazov') or '').lower())
        
        # 📄 Stránkovanie (12 položiek na stranu)
        paginator = Paginator(eshop_diely, 12)
        page_number = self.request.GET.get('page')
        page_obj = paginator.get_page(page_number)
        
        zlava_dec = Decimal(str(zlava or '0.00'))
        context['is_b2b'] = zlava_dec > Decimal('0.00') or bool(b2b_klient)
        context['eshop_diely'] = page_obj
        context['page_obj'] = page_obj
        context['diely'] = page_obj
        context['object_list'] = page_obj
        context['search_query'] = search_query
        context['mena'] = context.get('mena') or get_firemna_mena()
        context['identita'] = context.get('identita') or FiremnaIdentita.objects.first()
        context['nastavenia'] = context.get('nastavenia') or WebNastavenia.objects.first()
        return context

class ClanokListView(WebBaseMixin, ListView):
    model = Clanok
    template_name = 'web/clanok_list.html'
    context_object_name = 'clanky'
    paginate_by = 9
    
    def get_queryset(self):
        is_preview = _resolve_preview_mode(self.request)
        if _is_staff_user(self.request.user) or is_preview:
            qs = Clanok.objects.all().exclude(slug__isnull=True).exclude(slug__exact='').select_related('autor')
        else:
            dnes = timezone.localdate() if getattr(settings, 'USE_TZ', False) else timezone.now().date()
            qs = Clanok.objects.filter(
                je_aktivny=True, 
                datum_vydania__lte=dnes
            ).exclude(slug__isnull=True).exclude(slug__exact='').select_related('autor')
        
        # 🔍 Plnohodnotný technický fulltext: nadpis, perex aj telo článku (obsah)
        q = self.request.GET.get('q', '').strip()
        if q:
            qs = qs.filter(Q(nadpis__icontains=q) | Q(perex__icontains=q) | Q(obsah__icontains=q))
            
        # 🏷️ Filter podľa kategórie
        kategoria = self.request.GET.get('kategoria', '').strip()
        if kategoria:
            qs = qs.filter(kategoria=kategoria)
            
        return qs.order_by('-datum_vydania', '-id')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        dnes = timezone.localdate() if getattr(settings, 'USE_TZ', False) else timezone.now().date()
        is_preview = context.get('is_preview', False)
        # Vytiahnutie unikátnych kategórií pre dynamické filtračné pillboxy s konzistentným časovým pásmom
        kat_filter = {} if (_is_staff_user(self.request.user) or is_preview) else {'je_aktivny': True, 'datum_vydania__lte': dnes}
        kategorie_raw = Clanok.objects.filter(
            **kat_filter
        ).exclude(kategoria__isnull=True).exclude(kategoria__exact='').values_list('kategoria', flat=True).distinct()
        
        # Mapovanie kľúčov na čitateľný názov cez choices modelu (defenzívne z triedy aj poľa modelu)
        kategorie_set = set(kategorie_raw)
        choices = getattr(Clanok, 'KATEGORIA_CHOICES', None) or getattr(Clanok._meta.get_field('kategoria'), 'choices', [])
        context['dostupne_kategorie'] = [
            {'kod': kod, 'nazov': nazov} for kod, nazov in choices if kod in kategorie_set
        ]
        aktivna_kat = self.request.GET.get('kategoria', '').strip()
        context['aktivna_kategoria'] = aktivna_kat
        choices_map = dict(choices)
        context['aktivna_kategoria_nazov'] = choices_map.get(aktivna_kat, aktivna_kat)
        context['search_query'] = self.request.GET.get('q', '').strip()
        return context

class KarieraListView(WebBaseMixin, ListView):
    model = PracovnaPozicia
    template_name = 'web/kariera_list.html'
    context_object_name = 'pozicie'

    def get_queryset(self):
        is_preview = _resolve_preview_mode(self.request)
        if _is_staff_user(self.request.user) or is_preview:
            return PracovnaPozicia.objects.all().exclude(slug__isnull=True).exclude(slug__exact='').order_by('poradie', '-id')
        dnes = timezone.localdate() if getattr(settings, 'USE_TZ', False) else timezone.now().date()
        return PracovnaPozicia.objects.filter(
            je_aktivna=True
        ).filter(
            Q(platnost_do__isnull=True) | Q(platnost_do__gte=dnes)
        ).exclude(slug__isnull=True).exclude(slug__exact='').order_by('poradie', '-id')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        is_preview = context.get('is_preview', False)
        verzia_filter = 'DRAFT' if is_preview else 'LIVE'

        # 🧱 Dynamický obsah hlavičky z CMS (WebBlok)
        WebBlokModel = apps.get_model('WebBuilder', 'WebBlok')
        context['kariera_blok'] = WebBlokModel.objects.filter(
            typ='CAREER', je_aktivny=True, verzia=verzia_filter
        ).first()

        # 👥 Reálny tím technikov vrátane odbornej spôsobilosti a dĺžky praxe (GDPR Safe)
        ZamestnanecModel = apps.get_model('Evidencia', 'Zamestnanec')
        dnes_datum = (
            timezone.localdate()
            if getattr(settings, 'USE_TZ', False)
            else timezone.now().date()
        )
        tim_qs = ZamestnanecModel.objects.filter(je_anonymizovany=False)
        tim = list(
            tim_qs.filter(zobrazit_na_webe=True)
            .prefetch_related('certifikaty')
            .only(
                'meno',
                'priezvisko',
                'verejna_rola',
                'profilova_fotka',
                'datum_nastupu',
            )
            .order_by('priezvisko')[:4]
        )

        for clen in tim:
            if clen.datum_nastupu:
                roky = (
                    dnes_datum.year
                    - clen.datum_nastupu.year
                    - (
                        (dnes_datum.month, dnes_datum.day)
                        < (clen.datum_nastupu.month, clen.datum_nastupu.day)
                    )
                )
                clen.prax_roky = max(0, roky)
            else:
                clen.prax_roky = None

        # Zero Data Leak: Na podstránke kariéry sa nezobrazujú interní zamestnanci bez povolenia na webe

        context['tim'] = tim
        context['faqs'] = list(
            WebFAQ.objects.filter(je_aktivna=True, kategoria='KARIERA').order_by('poradie', '-id')[:6]
        )
        return context

class KarieraDetailView(WebBaseMixin, DetailView):
    model = PracovnaPozicia
    template_name = 'web/kariera_detail.html'
    context_object_name = 'pozicia'

    def get_queryset(self):
        is_preview = _resolve_preview_mode(self.request)
        if _is_staff_user(self.request.user) or is_preview:
            return PracovnaPozicia.objects.all().exclude(slug__isnull=True).exclude(slug__exact='')

        return PracovnaPozicia.objects.filter(
            je_aktivna=True
        ).exclude(slug__isnull=True).exclude(slug__exact='')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        dnes = timezone.localdate() if getattr(settings, 'USE_TZ', False) else timezone.now().date()
        pozicia = self.object
        je_expirovana = bool(pozicia.platnost_do and pozicia.platnost_do < dnes)
        context['dnes'] = dnes
        context['je_expirovana'] = je_expirovana
        context['je_aktualna'] = pozicia.je_aktivna and not je_expirovana
        
        # 🤝 Zobrazenie ďalších aktívnych otvorených pozícií pre uchádzača
        dalsie_qs = PracovnaPozicia.objects.filter(
            je_aktivna=True
        ).filter(
            Q(platnost_do__isnull=True) | Q(platnost_do__gte=dnes)
        ).exclude(id=self.object.id).exclude(slug__isnull=True).exclude(slug__exact='')
        
        context['dalsie_pozicie'] = list(dalsie_qs.order_by('poradie', '-id')[:3])
        return context

class ClanokDetailView(WebBaseMixin, DetailView):
    model = Clanok
    template_name = 'web/clanok_detail.html'
    context_object_name = 'clanok'
    
    def get_queryset(self):
        # 🛡️ BEZPEČNOSŤ & PREVIEW: Staff alebo AI náhľad vidí aj neaktívne a budúce koncepty
        is_preview = _resolve_preview_mode(self.request)
        if _is_staff_user(self.request.user) or is_preview:
            return Clanok.objects.all().select_related('autor')
        dnes = timezone.localdate() if getattr(settings, 'USE_TZ', False) else timezone.now().date()
        return Clanok.objects.filter(
            je_aktivny=True,
            datum_vydania__lte=dnes
        ).exclude(slug__isnull=True).exclude(slug__exact='').select_related('autor')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        dnes = timezone.localdate() if getattr(settings, 'USE_TZ', False) else timezone.now().date()
        # 🧠 ISAS SMART RECOMMENDATION: Prioritne vyberáme články z rovnakej kategórie s platným slugom
        suvisiace_qs = Clanok.objects.filter(
            je_aktivny=True, datum_vydania__lte=dnes
        ).exclude(id=self.object.id).exclude(slug__isnull=True).exclude(slug__exact='').select_related('autor')

        rovnaka_kategoria = list(suvisiace_qs.filter(kategoria=self.object.kategoria).order_by('-datum_vydania')[:3])
        if len(rovnaka_kategoria) < 3:
            doplnok_ids = [c.id for c in rovnaka_kategoria] + [self.object.id]
            zvysok = list(suvisiace_qs.exclude(id__in=doplnok_ids).order_by('-datum_vydania')[:3 - len(rovnaka_kategoria)])
            context['suvisiace_clanky'] = rovnaka_kategoria + zvysok
        else:
            context['suvisiace_clanky'] = rovnaka_kategoria
        return context

class EshopDetailView(WebBaseMixin, TemplateView):
    template_name = 'web/eshop_detail.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        slug = self.kwargs.get('slug')
        if not slug or not str(slug).strip():
            raise Http404("Diel nebol nájdený.")
        
        povolene_modely = get_eshop_models()
        
        diel = None
        # ⚡ REDIS O(1) LOOKUP: Eliminácia sekvenčného prehľadávania 20+ skladových tabuliek
        cached_meta = cache.get(f"eshop_diel_meta_{slug}")
        if cached_meta and isinstance(cached_meta, (list, tuple)) and len(cached_meta) == 3:
            app_label, model_name, obj_id = cached_meta
            try:
                target_model = apps.get_model(app_label, model_name)
            except (LookupError, ValueError):
                target_model = None

            is_preview = _resolve_preview_mode(self.request)
            allow_draft = _is_staff_user(self.request.user) or is_preview
            diel_filter = {'pk': obj_id} if allow_draft else {'pk': obj_id, 'zobrazit_na_webe': True}

            if target_model:
                target_fields = {f.name for f in target_model._meta.get_fields()}
                if 'slug' in target_fields and (allow_draft or 'zobrazit_na_webe' in target_fields):
                    try:
                        diel = target_model.objects.select_related('vyrobca').filter(**diel_filter).first() if 'vyrobca' in target_fields else target_model.objects.filter(**diel_filter).first()
                    except Exception:
                        diel = target_model.objects.filter(**diel_filter).first()

        if not diel:
            is_preview = _resolve_preview_mode(self.request)
            allow_draft = _is_staff_user(self.request.user) or is_preview
            diel_filter = {'slug': slug} if allow_draft else {'slug': slug, 'zobrazit_na_webe': True}

            for model in povolene_modely:
                model_fields = {f.name for f in model._meta.get_fields()}
                if 'slug' not in model_fields or (not allow_draft and 'zobrazit_na_webe' not in model_fields):
                    continue
                try:
                    diel = model.objects.select_related('vyrobca').filter(**diel_filter).first() if 'vyrobca' in model_fields else model.objects.filter(**diel_filter).first()
                except Exception:
                    diel = model.objects.filter(**diel_filter).first()
                    
                if diel:
                    if not allow_draft:
                        cache.set(f"eshop_diel_meta_{slug}", (diel._meta.app_label, diel._meta.model_name, diel.id), 3600)
                    break
                
        if not diel:
            raise Http404("Diel nebol nájdený.")
            
        ct = ContentType.objects.get_for_model(diel)
        katalog = CenovyKatalog.objects.filter(content_type=ct, object_id=diel.id).first()
        
        zakladna_cena = Decimal(str(katalog.predajna_cena)) if (katalog and katalog.predajna_cena is not None) else None
        b2b_klient, zlava = get_b2b_client_and_discount(self.request)
        def_dph_detail = GlobalneNastavenia.get_zakladna_dph()

        zlava_dec = Decimal(str(zlava or '0.00'))
        if zakladna_cena is not None and zakladna_cena > Decimal('0.00'):
            cena_bez_dph = zakladna_cena * (Decimal('1.00') - (zlava_dec / Decimal('100.00'))) if zlava_dec > Decimal('0.00') else zakladna_cena
            diel_dph = getattr(diel, 'sadzba_dph', None)
            dph_raw = diel_dph if (diel_dph is not None and str(diel_dph).strip() != '') else (def_dph_detail or '0.00')
            try:
                sadzba_dph = Decimal(str(dph_raw))
            except (InvalidOperation, TypeError, ValueError):
                sadzba_dph = Decimal('0.00')

            cena_s_dph = cena_bez_dph * (Decimal('1.00') + (sadzba_dph / Decimal('100.00')))
            cena_bez_dph_val = cena_bez_dph.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            povodna_cena_val = zakladna_cena.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if zlava_dec > Decimal('0.00') else None
            cena_s_dph_val = cena_s_dph.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            cena_bez_dph_val = None
            povodna_cena_val = None
            cena_s_dph_val = None
            diel_dph = getattr(diel, 'sadzba_dph', None)
            dph_raw = diel_dph if (diel_dph is not None and str(diel_dph).strip() != '') else (def_dph_detail or '0.00')
            try:
                sadzba_dph = Decimal(str(dph_raw))
            except (InvalidOperation, TypeError, ValueError):
                sadzba_dph = Decimal('0.00')

        # 🛡️ ZERO DATA LEAK: Sanitizácia ERP modelu – interné pole 'kto_som', primárne kľúče, nákupné ceny a marže sa nesmú dostať do kontextu
        diel_clean = {
            'nazov': getattr(diel, 'obchodny_nazov', None) or getattr(diel, 'seriove_cislo', '') or get_industry_term('polozka_fallback_nazov', 'Položka'),
            'obchodny_nazov': getattr(diel, 'obchodny_nazov', ''),
            'seriove_cislo': getattr(diel, 'seriove_cislo', ''),
            'slug': getattr(diel, 'slug', ''),
            'katalogove_cislo': getattr(diel, 'katalogove_cislo', '') or "",
            'ean_kod': getattr(diel, 'ean_kod', '') or '',
            'popis_na_webe': getattr(diel, 'popis_na_webe', '') or '',
            'obrazok_na_webe': getattr(diel, 'obrazok_na_webe', None),
            'merna_jednotka': getattr(diel, 'merna_jednotka', ''),
            'pocet_na_sklade': getattr(diel, 'pocet_na_sklade', 0),
            'skladom': (getattr(diel, 'pocet_na_sklade', 0) or 0) > 0,
            'skryt_vyrobcu': getattr(diel, 'skryt_vyrobcu', False),
            'vyrobca': diel.vyrobca if (getattr(diel, 'vyrobca', None) and not getattr(diel, 'skryt_vyrobcu', False)) else None,
            'typ': getattr(diel, 'typ', '') or '',
            'zobrazit_na_webe': getattr(diel, 'zobrazit_na_webe', False),
        }
        context['diel'] = diel_clean
        context['cena_bez_dph'] = cena_bez_dph_val
        context['povodna_cena_bez_dph'] = povodna_cena_val
        context['cena_s_dph'] = cena_s_dph_val
        context['sadzba_dph'] = sadzba_dph
        context['is_b2b'] = zlava_dec > Decimal('0.00') or bool(b2b_klient)
        context['zlava_percent'] = zlava_dec
        context['mena'] = context.get('mena') or get_firemna_mena()
        context['identita'] = context.get('identita') or FiremnaIdentita.objects.first()
        context['nastavenia'] = context.get('nastavenia') or WebNastavenia.objects.first()
        return context

@transaction.atomic
def generate_esg_audit(request):
    """
    📥 ISAS LEAD GENERATOR: Vygeneruje PDF audit a založí Lead v ERP.
    """
    if not is_esg_audit_supported():
        if request.method == 'POST':
            return JsonResponse({'status': 'error', 'message': 'Tento modul nie je aktívny.'}, status=400)
        return redirect('/')

    if request.method == 'POST':
        email = request.POST.get('email', '').strip().lower()[:254]
        kw = request.POST.get('kw', '0')
        strata_eur = request.POST.get('strata_eur', '0')
        strata_co2 = request.POST.get('strata_co2', '0')
        cena_kwh_raw = request.POST.get('cena_kwh', '').strip()
        percent_raw = request.POST.get('percent', '').strip()
        
        # Honeypot
        if request.POST.get('website_url'):
            return JsonResponse({'status': 'success'})

        ip_adresa = get_client_ip(request)
        esg_limit_key = f"esg_audit_limit_{ip_adresa}"
        if not cache.add(esg_limit_key, 1, timeout=300):
            try:
                if cache.incr(esg_limit_key) > 5:
                    return JsonResponse({'status': 'error', 'message': 'Príliš veľa požiadaviek. Skúste to prosím neskôr.'}, status=429)
            except ValueError:
                cache.set(esg_limit_key, 1, timeout=300)

        if not _is_valid_email_address(email):
            return JsonResponse({'status': 'error', 'message': 'Zadajte platný e-mail.'}, status=400)

        try:
            # 1. Vytvorenie Leadu (Zákazník + Objednávka) s ochranou pred race condition
            zakaznik = Zakaznik.objects.select_for_update().filter(email__iexact=email).first()
            if not zakaznik:
                try:
                    with transaction.atomic():
                        zakaznik = Zakaznik.objects.create(
                            Nazov_spolocnosti=f"Lead ({email})",
                            email=email,
                            Ostatne='Vygenerované z webovej kalkulačky (ESG Audit).'
                        )
                except Exception:
                    zakaznik = Zakaznik.objects.filter(email__iexact=email).first()

            if not zakaznik:
                transaction.set_rollback(True)
                return JsonResponse({'status': 'error', 'message': 'Nepodarilo sa vytvoriť profil zákazníka.'}, status=500)
            
            identita = FiremnaIdentita.objects.first()
            mena_firmy = getattr(identita, 'predvolena_mena', None) or get_firemna_mena()
            stroj_label = get_industry_term('esg_audit_stroj_label', default='Menovitý príkon zariadenia')
            odporucanie_text = get_industry_term(
                'esg_audit_odporucanie',
                default='Optimalizáciou prevádzky, modernizáciou technológie alebo inštaláciou nadradeného inteligentného riadenia dokážete túto prevádzkovú stratu výrazne minimalizovať.'
            )
            metodika_label = get_industry_term('esg_audit_metodika', default='GHG Protocol (Scope 2) / EN 16247')

            # 👤 Zodpovedný garant z ERP (Dispečer / Technik / Konateľ)
            garant_zamestnanec = Zamestnanec.objects.filter(
                pracovna_rola__in=['DI', 'TE', 'KO'],
                user__isnull=False,
                user__is_staff=True,
                user__is_active=True
            ).select_related('user').first()
            zodpovedny_user = garant_zamestnanec.user if garant_zamestnanec else None
            meno_garanta = f"{garant_zamestnanec.meno or ''} {garant_zamestnanec.priezvisko or ''}".strip() if garant_zamestnanec else ""
            auditor_meno = meno_garanta or getattr(identita, 'vystavuje_ponuky', None) or getattr(identita, 'konatel', None) or "Technický audítor"
            auditor_rola = getattr(garant_zamestnanec, 'verejna_rola', None) or get_industry_term('esg_auditor_title', 'ESG & Energetický špecialista')
            vykon_jednotka = get_industry_term('esg_power_unit', 'kW')

            # 🔢 Inžinierska sanitizácia vstupov a kalkulácie v Decimal s podporou rôznych formátov čísiel
            def _parse_decimal(val_str, default=None):
                if val_str is None:
                    return default
                s = re.sub(r'[^\d.,]', '', str(val_str).strip())
                if not s:
                    return default
                if '.' in s and ',' in s:
                    s = s.replace('.', '').replace(',', '.') if s.rfind(',') > s.rfind('.') else s.replace(',', '')
                elif ',' in s:
                    s = s.replace(',', '.')
                elif s.count('.') > 1:
                    parts = s.split('.')
                    s = ''.join(parts[:-1]) + '.' + parts[-1]
                try:
                    return Decimal(s)
                except (InvalidOperation, TypeError, ValueError):
                    return default

            kw_dec = (_parse_decimal(kw, Decimal('0.00')) or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            cena_kwh_dec = _parse_decimal(cena_kwh_raw, None)

            # 🛡️ ZERO DATA LEAK: Neverejné zmluvné parametre sa sprístupnia výhradne overenému a autorizovanému klientovi
            je_autorizovany_klient = bool(
                request.user.is_authenticated and getattr(request.user, 'is_active', False) and (
                    request.user.is_staff or (request.user.email and request.user.email.strip().lower() == email)
                )
            )

            if cena_kwh_dec is None and je_autorizovany_klient and zakaznik and getattr(zakaznik, 'cena_za_kwh_eur', None):
                if zakaznik.cena_za_kwh_eur > Decimal('0.000'):
                    cena_kwh_dec = zakaznik.cena_za_kwh_eur

            podiel_naprazdno_dec = _parse_decimal(percent_raw, None)

            emisny_faktor_raw = request.POST.get('emisny_faktor', '').strip()
            emisny_faktor_dec = _parse_decimal(emisny_faktor_raw, None)

            if emisny_faktor_dec is None and je_autorizovany_klient and zakaznik and getattr(zakaznik, 'emisny_faktor_co2', None):
                if zakaznik.emisny_faktor_co2 > Decimal('0.0000'):
                    emisny_faktor_dec = zakaznik.emisny_faktor_co2

            strata_eur_dec = (_parse_decimal(strata_eur, Decimal('0.00')) or Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            strata_co2_dec = (_parse_decimal(strata_co2, Decimal('0.0')) or Decimal('0.0')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)

            krajina_iso = FiremnaIdentita.get_aktivna_krajina_iso() if hasattr(FiremnaIdentita, 'get_aktivna_krajina_iso') else None
            compliance_cfg = get_compliance_config(iso=krajina_iso) or {}
            idle_factor_dec = Decimal(str(compliance_cfg.get('esg_idle_factor') or HUB_CONFIG.get('esg_idle_factor') or '0.00'))
            hours_dec = Decimal(str(compliance_cfg.get('esg_annual_hours') or HUB_CONFIG.get('esg_annual_hours') or '0'))
            co2_factor_dec = Decimal(str(compliance_cfg.get('esg_co2_factor') or HUB_CONFIG.get('esg_co2_factor') or '0.00'))

            if strata_eur_dec <= Decimal('0.00') and kw_dec > Decimal('0.00') and cena_kwh_dec is not None and podiel_naprazdno_dec is not None:
                kwh_lost = (kw_dec * idle_factor_dec) * hours_dec * (podiel_naprazdno_dec / Decimal('100.00'))
                strata_eur_dec = (kwh_lost * cena_kwh_dec).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                if strata_co2_dec <= Decimal('0.0'):
                    strata_co2_dec = ((kwh_lost * co2_factor_dec) / Decimal('1000.00')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)

            faktor_uspory = Decimal(str(compliance_cfg.get('esg_savings_factor') or get_industry_term('esg_savings_factor') or HUB_CONFIG.get('esg_savings_factor') or '0.00'))
            uspora_eur_dec = (strata_eur_dec * faktor_uspory).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            nova_strata_eur_dec = (strata_eur_dec - uspora_eur_dec).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            uspora_co2_dec = (strata_co2_dec * faktor_uspory).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
            nova_strata_co2_dec = (strata_co2_dec - uspora_co2_dec).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
            uspora_3_roky = (uspora_eur_dec * Decimal('3.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            strata_3_roky = (strata_eur_dec * Decimal('3.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            nova_strata_3_roky = (nova_strata_eur_dec * Decimal('3.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            uspora_5_rokov = (uspora_eur_dec * Decimal('5.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            strata_5_rokov = (strata_eur_dec * Decimal('5.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            nova_strata_5_rokov = (nova_strata_eur_dec * Decimal('5.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            lead_objednavka = PrijataObjednavka.objects.create(
                zakaznik=zakaznik,
                zodpovedny_zamestnanec=zodpovedny_user,
                stav='NOVA',
                priorita='VYSOKA',
                forma_prijatia='WEB',
                specificky_email_pre_fakturu=email,
                poznamka_klienta=f"Žiadosť o ESG Audit.\n{stroj_label}: {kw_dec} {vykon_jednotka}\nVypočítaná strata: {strata_eur_dec} {mena_firmy}\nUhlíková stopa: {strata_co2_dec} t CO₂",
                interna_poznamka=f"🔥 ESG LEAD: Zodpovedný garant {auditor_meno}. Odhadovaná ročná úspora {uspora_eur_dec} {mena_firmy}."
            )

            kpi_strata_label = get_industry_term('kpi_strata_label', default='Odhadovaná ročná finančná strata')
            kpi_strata_tooltip = get_industry_term('kpi_strata_tooltip', default='Odhadované prevádzkové straty spôsobené neproduktívnym chodom zariadenia bez úžitkového výkonu.')
            disclaimer = get_legal_disclaimer()
            cas_vyhotovenia = getattr(lead_objednavka, 'vytvorene', None) or timezone.now()
            if getattr(settings, 'USE_TZ', False) and timezone.is_aware(cas_vyhotovenia):
                cas_vyhotovenia = timezone.localtime(cas_vyhotovenia)
            elif timezone.is_aware(cas_vyhotovenia):
                cas_vyhotovenia = cas_vyhotovenia.replace(tzinfo=None)
            # 🛡️ ZERO DATA LEAK: Certifikačné evidenčné číslo auditu nesmie odhaľovať interný kód objednávky ERP (OBJP-)
            d_auditu = getattr(lead_objednavka, 'datum_prijatia', None) or (cas_vyhotovenia.date() if hasattr(cas_vyhotovenia, 'date') else cas_vyhotovenia)
            cislo_auditu = f"ESG-{d_auditu.strftime('%Y')}-{lead_objednavka.pk:05d}"

            # 2. Generovanie PDF (Využívame existujúci ISAS Engine)            
            html_string = render_to_string('web/pdf_esg_audit.html', {
                'email': email,
                'zakaznik': zakaznik if je_autorizovany_klient else None,
                'cislo_auditu': cislo_auditu,
                'cas_vyhotovenia': cas_vyhotovenia,
                'auditor_meno': auditor_meno,
                'auditor_rola': auditor_rola,
                'kw': kw_dec,
                'cena_kwh': cena_kwh_dec,
                'podiel_naprazdno_percent': podiel_naprazdno_dec,
                'emisny_faktor': emisny_faktor_dec,
                'strata_eur': strata_eur_dec,
                'strata_co2': strata_co2_dec,
                'uspora_eur': uspora_eur_dec,
                'nova_strata_eur': nova_strata_eur_dec,
                'uspora_co2': uspora_co2_dec,
                'nova_strata_co2': nova_strata_co2_dec,
                'uspora_3_roky': uspora_3_roky,
                'strata_3_roky': strata_3_roky,
                'nova_strata_3_roky': nova_strata_3_roky,
                'uspora_5_rokov': uspora_5_rokov,
                'strata_5_rokov': strata_5_rokov,
                'nova_strata_5_rokov': nova_strata_5_rokov,
                'identita': identita,
                'mena': mena_firmy,
                'stroj_label': stroj_label,
                'kpi_strata_label': kpi_strata_label,
                'kpi_strata_tooltip': kpi_strata_tooltip,
                'podiel_label': get_industry_term('esg_audit_podiel_label', default='Prevádzka v odľahčení / neproduktívny čas'),
                'odporucanie_text': odporucanie_text,
                'metodika_label': metodika_label,
                'disclaimer': disclaimer,
            })
            
            pdf_file = generate_pdf_from_html(html_string)
            
            if not pdf_file:
                transaction.set_rollback(True)
                return JsonResponse({'status': 'error', 'message': 'Nepodarilo sa vygenerovať PDF dokument. Skúste to prosím neskôr.'}, status=500)
            
            # Uložíme PDF k objednávke bez redundantného save() s ochranou pred duplicitným spúšťaním Celery úloh
            lead_objednavka.subor_objednavky.save(f"ESG_Audit_{cislo_auditu}.pdf", ContentFile(pdf_file), save=False)
            PrijataObjednavka.objects.filter(pk=lead_objednavka.pk).update(subor_objednavky=lead_objednavka.subor_objednavky.name)
            
            # 3. Asynchrónne odoslanie e-mailu klientovi
            from .tasks import notify_web_inquiry_task
            def _safe_notify_esg(pk=lead_objednavka.pk):
                try:
                    notify_web_inquiry_task.delay(pk)
                except Exception as exc:
                    logger.warning(f"Výpadok Celery/Redis brokera pri notifikácii ESG auditu #{pk}: {exc}")
            transaction.on_commit(_safe_notify_esg)

            return JsonResponse({'status': 'success', 'message': 'Audit bol úspešne vygenerovaný a odoslaný na Váš e-mail.'})
            
        except Exception as e:
            transaction.set_rollback(True)
            logger.error(f"ESG Audit Error: {e}")
            return JsonResponse({'status': 'error', 'message': 'Nastala chyba pri generovaní auditu.'}, status=500)

    return JsonResponse({'status': 'error'}, status=400)

@transaction.atomic
def submit_career(request):
    """🤝 Spracovanie životopisu z modulu Kariéra s Rate Limitingom a GDPR ochranou."""
    if request.method == 'POST':
        if request.POST.get('website_url'): 
            return JsonResponse({'status': 'success'}) # Honeypot
        
        pozicia_id = request.POST.get('pozicia_id')
        meno = request.POST.get('meno', '').strip()[:150]
        email = request.POST.get('email', '').strip().lower()[:254]
        telefon = request.POST.get('telefon', '').strip()[:50]
        motivacna_sprava = request.POST.get('motivacna_sprava', '').strip()[:3000]
        gdpr_suhlas = request.POST.get('gdpr_suhlas')
        zivotopis = request.FILES.get('zivotopis')
        
        if not meno or not email or not zivotopis:
            return JsonResponse({'status': 'error', 'message': 'Meno, e-mail a životopis sú povinné.'}, status=400)

        if not _is_valid_email_address(email):
            return JsonResponse({'status': 'error', 'message': 'Zadajte platnú e-mailovú adresu.'}, status=400)

        if not gdpr_suhlas:
            return JsonResponse({'status': 'error', 'message': 'Pre odoslanie životopisu je nutný súhlas so spracovaním osobných údajov (GDPR).'}, status=400)

        # 🛡️ Ochrana pred DoS: Limit veľkosti životopisu (prázdny súbor alebo max 10MB)
        if zivotopis.size <= 0 or zivotopis.size > 10 * 1024 * 1024:
            return JsonResponse({'status': 'error', 'message': 'Súbor so životopisom je prázdny alebo príliš veľký (maximálne 10 MB).'}, status=400)

        # 🛡️ Maximálna sanitácia: Striktná kontrola typu a prípony súboru na backende
        povolene_pripony = ('.pdf', '.doc', '.docx', '.odt')
        if not zivotopis.name or not zivotopis.name.lower().endswith(povolene_pripony):
            return JsonResponse({'status': 'error', 'message': 'Nepovolený formát súboru. Nahrajte prosím PDF alebo Word dokument (.pdf, .doc, .docx).'}, status=400)

        # 🛡️ Anti-Spam Rate Limiting: Max 3 odoslania životopisu za 5 minút z jednej IP
        ip_adresa = get_client_ip(request)
        cache_key = f"career_limit_{ip_adresa}"
        
        try:
            pokusy = cache.incr(cache_key)
        except ValueError:
            cache.set(cache_key, 1, 300)
            pokusy = 1
            
        if pokusy > 3:
            return JsonResponse({'status': 'error', 'message': 'Príliš veľa odoslaných žiadostí. Skúste to prosím neskôr.'}, status=429)

        pozicia = None
        if pozicia_id and str(pozicia_id).strip():
            pozicia_raw = str(pozicia_id).strip()
            if not pozicia_raw.isdigit():
                return JsonResponse({'status': 'error', 'message': 'Neplatný identifikátor pracovnej pozície.'}, status=400)
            try:
                is_preview = _resolve_preview_mode(request) or _is_staff_user(request.user)
                pozicia_qs = PracovnaPozicia.objects.all() if is_preview else PracovnaPozicia.objects.filter(je_aktivna=True)
                pozicia = pozicia_qs.filter(pk=int(pozicia_raw)).first()
                if not pozicia:
                    return JsonResponse({'status': 'error', 'message': 'Pracovná pozícia neexistuje alebo už nie je aktívna.'}, status=400)
                dnes = timezone.localdate() if getattr(settings, 'USE_TZ', False) else timezone.now().date()
                if not is_preview and pozicia.platnost_do and pozicia.platnost_do < dnes:
                    return JsonResponse({'status': 'error', 'message': 'Výberové konanie na túto pozíciu už bolo ukončené.'}, status=400)
            except (ValueError, TypeError, DataError):
                return JsonResponse({'status': 'error', 'message': 'Neplatný identifikátor pracovnej pozície.'}, status=400)
        
        kandidat = Kandidat.objects.create(
            pozicia=pozicia,
            meno=meno,
            email=email,
            telefon=telefon,
            zivotopis=zivotopis,
            motivacna_sprava=motivacna_sprava,
            ip_adresa=ip_adresa,
            gdpr_suhlas=True
        )
        
        # 🚀 ASYNC TASK: Asynchrónny odpal notifikácie cez Celery po commite
        from .tasks import notify_new_candidate_task
        def _safe_notify_candidate(pk=kandidat.pk):
            try:
                notify_new_candidate_task.delay(pk)
            except Exception as exc:
                logger.warning(f"Výpadok Celery/Redis brokera pri notifikácii kandidáta #{pk}: {exc}")
        transaction.on_commit(_safe_notify_candidate)
        
        return JsonResponse({'status': 'success', 'message': 'Váš životopis bol úspešne odoslaný. Ďakujeme!'})
    return JsonResponse({'status': 'error'}, status=400)

@transaction.atomic
def submit_special_lead(request):
    """🛡️ Univerzálny handler pre SLA, Výkup a Reklamácie."""
    if request.method == 'POST':
        if request.POST.get('website_url'): return JsonResponse({'status': 'success'}) # Honeypot

        ip_adresa = get_client_ip(request)
        lead_limit_key = f"special_lead_limit_{ip_adresa}"
        if not cache.add(lead_limit_key, 1, timeout=300):
            try:
                if cache.incr(lead_limit_key) > 5:
                    return JsonResponse({'status': 'error', 'message': 'Príliš veľa požiadaviek. Skúste to prosím neskôr.'}, status=429)
            except ValueError:
                cache.set(lead_limit_key, 1, timeout=300)
        
        typ_leadu = request.POST.get('typ_leadu') # 'SLA', 'BUYBACK', 'WARRANTY'
        firma = request.POST.get('firma', '').strip()[:100]
        email = request.POST.get('email', '').strip().lower()[:254]
        telefon = request.POST.get('telefon', '').strip()[:50]
        priloha = request.FILES.get('priloha')
        
        # 🛡️ BEZPEČNOSŤ: Validácia prílohy pre špeciálne leady (Reklamácie, Výkup)
        if priloha:
            if priloha.size <= 0 or priloha.size > 10 * 1024 * 1024:
                return JsonResponse({'status': 'error', 'message': 'Príloha je prázdna alebo príliš veľká (max 10 MB).'}, status=400)
            if not priloha.name or not priloha.name.lower().endswith(('.pdf', '.jpg', '.jpeg', '.png', '.webp', '.mp4', '.mov')):
                return JsonResponse({'status': 'error', 'message': 'Nepovolený formát prílohy.'}, status=400)

        if not _is_valid_email_address(email):
            return JsonResponse({'status': 'error', 'message': 'Zadajte platnú e-mailovú adresu.'}, status=400)

        # 🛡️ RACE CONDITION & B2B INTEGRITA: Prednosť má autentifikovaný B2B klient pred anonymným dohľadaním
        b2b_klient, _ = get_b2b_client_and_discount(request)
        zakaznik = b2b_klient
        if not zakaznik:
            zakaznik = Zakaznik.objects.select_for_update().filter(email__iexact=email).first()
            if not zakaznik:
                try:
                    with transaction.atomic():
                        zakaznik = Zakaznik.objects.create(
                            Nazov_spolocnosti=firma or f"Lead ({email})",
                            email=email,
                            telefon=telefon
                        )
                except Exception:
                    zakaznik = Zakaznik.objects.filter(email__iexact=email).first()

        if not zakaznik:
            return JsonResponse({'status': 'error', 'message': 'Nepodarilo sa spracovať profil zákazníka.'}, status=500)

        if typ_leadu == 'SLA':
            stroje = request.POST.get('stroje', '0')
            typ_strojov = request.POST.get('typ_strojov', '')
            odozva = request.POST.get('odozva', '')
            pocet_label = get_industry_term('pocet_zariadeni_label', 'Počet zariadení')
            typ_label = get_industry_term('typ_zariadeni_label', 'Typ zariadení')
            poznamka = f"Žiadosť o SLA Zmluvu.\n{pocet_label}: {stroje}\n{typ_label}: {typ_strojov}\nPožadovaná odozva: {odozva}"
            interna = "🛡️ LEAD: Záujem o servisný paušál (SLA)."
            priorita = 'STREDNA'
            
        elif typ_leadu == 'BUYBACK':
            znacka = request.POST.get('znacka', '')
            rok = request.POST.get('rok', '')
            zar_genitiv = get_industry_term('zariadenie_genitiv', 'zariadenia')
            rok_label = get_industry_term('rok_vyroby', 'Rok výroby / nasadenia')
            poznamka = f"Ponuka na odkúpenie ({zar_genitiv}).\nZnačka/Model: {znacka}\n{rok_label}: {rok}\nStav: {request.POST.get('stav', '')}"
            interna = f"♻️ LEAD: Výkup ({zar_genitiv})."
            priorita = 'NIZKA'
            
        elif typ_leadu == 'WARRANTY':
            faktura = request.POST.get('faktura', '').strip()[:100]
            zariadenie_manual = request.POST.get('zariadenie_manual', '').strip()[:150]
            popis = request.POST.get('popis', '').strip()[:5000]
            poznamka_riadky = [f"REKLAMÁCIA (Garančný servis)", f"Doklad / Zákazka: {faktura}"]
            if zariadenie_manual:
                poznamka_riadky.append(f"Zariadenie / S/N (vstup klienta): {zariadenie_manual}")
            poznamka_riadky.append(f"Popis vady: {popis}")
            poznamka = "\n".join(poznamka_riadky)
            interna_zar = f" ({zariadenie_manual})" if zariadenie_manual else ""
            interna = f"🚨 REKLAMÁCIA Z WEBU. Doklad: {faktura}{interna_zar}. Skontrolovať a vytvoriť Úlohu s príznakom 'Ide o reklamáciu'."
            priorita = 'VYSOKA'
        else:
            return JsonResponse({'status': 'error', 'message': 'Neznámy typ požiadavky.'}, status=400)

        # 🛡️ CRM INTEGRITA: Doplnenie telefónu k zákazníkovi, ak v profile chýbal
        if telefon and not zakaznik.telefon:
            zakaznik.telefon = telefon
            zakaznik.save(update_fields=['telefon'])

        # ⏳ KASKÁDOVÉ URČENIE TERMÍNU DODANIA PRE REKLAMÁCIE (S kalendárnym posunom)
        termin_dodania = None
        if typ_leadu == 'WARRANTY':
            gn_obj = GlobalneNastavenia.objects.first()
            
            iso_krajiny = get_iso_pre_objekt(zakaznik) if zakaznik else None
            stat_obj = StatLokalizacia.get_for_iso(iso_krajiny) if iso_krajiny else None
            
            platna_lehota = (
                getattr(zakaznik, 'reklamacna_lehota_dni', None) or
                (getattr(stat_obj, 'lehota_reklamacie_dni', None) if stat_obj else None) or
                (getattr(gn_obj, 'standardna_lehota_reklamacie_dni', None) if gn_obj else None)
            )

            if platna_lehota is not None and platna_lehota > 0:
                kandidat = timezone.now().date() + timedelta(days=platna_lehota)
                iso_krajiny = stat_obj.iso_kod if stat_obj else None
                FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
                if FiremnaIdentitaModel and hasattr(FiremnaIdentitaModel, 'posun_na_pracovny_den'):
                    termin_dodania = FiremnaIdentitaModel.posun_na_pracovny_den(kandidat, iso=iso_krajiny)
                else:
                    termin_dodania = kandidat

        objednavka = PrijataObjednavka.objects.create(
            zakaznik=zakaznik, stav='NOVA', priorita=priorita, forma_prijatia='WEB',
            vystavil_za_klienta=firma[:150],
            specificky_email_pre_fakturu=email, poznamka_klienta=poznamka, interna_poznamka=interna,
            termin_dodania=termin_dodania
        )

        # 🚀 ISAS BIZNIS: Striktne IDOR-safe párovanie reklamovaného zariadenia a dokladu
        zariadenie_id = request.POST.get('zariadenie_id')
        if typ_leadu == 'WARRANTY':
            zariadenie_obj = None

            # 1. Párovanie podľa explicitne zvoleného ID zariadenia (IDOR-safe + savepoint)
            if zariadenie_id:
                z_raw = str(zariadenie_id).strip()
                try:
                    with transaction.atomic():
                        z_uuid = None
                        try:
                            z_uuid = uuid.UUID(z_raw)
                        except (ValueError, TypeError, AttributeError):
                            z_uuid = None

                        if z_uuid is not None and zakaznik:
                            zariadenie_obj = ServisovaneZariadenie.objects.filter(
                                bezpecnostny_kluc=z_uuid, zakaznik=zakaznik
                            ).first()
                        elif z_raw.isdigit() and b2b_klient is not None:
                            zariadenie_obj = ServisovaneZariadenie.objects.filter(
                                pk=int(z_raw), zakaznik=b2b_klient
                            ).first()
                except (ValueError, TypeError, DataError):
                    zariadenie_obj = None

            # 2. Relačné prepojenie faktúry alebo zákazky z evidencie (s auto-doplnením zariadenia)
            doklad_vstup = request.POST.get('faktura', '').strip()
            if doklad_vstup and zakaznik:
                faktura_obj = VystavenaFaktura.objects.filter(cislo_faktury=doklad_vstup, zakaznik=zakaznik).first()
                if faktura_obj:
                    faktura_obj.zdrojove_objednavky.add(objednavka)
                    if not zariadenie_obj and faktura_obj.zariadenie:
                        zariadenie_obj = faktura_obj.zariadenie
                else:
                    povodna_obj = PrijataObjednavka.objects.filter(
                        models.Q(cislo_objednavky_interne=doklad_vstup) | models.Q(cislo_objednavky_klienta=doklad_vstup),
                        zakaznik=zakaznik
                    ).first()
                    if povodna_obj and not zariadenie_obj and povodna_obj.zariadenia.exists():
                        zariadenie_obj = povodna_obj.zariadenia.first()

            if zariadenie_obj:
                objednavka.zariadenia.add(zariadenie_obj)

            # Doplnenie informácie o Gate Passe do internej poznámky
            if zakaznik and getattr(zakaznik, 'vyzaduje_povolenie_vstupu', False):
                objednavka.interna_poznamka = f"{objednavka.interna_poznamka}\n🛂 UPOZORNENIE: Zákazník vyžaduje Gate Pass (povolenie na vstup)."
                objednavka.save(update_fields=['interna_poznamka'])

            # 🛡️ OKAMŽITÉ ZARADENIE DO SLA MONITORA: Automatické vytvorenie servisnej Ulohy
            teraz = timezone.now()
            if not getattr(settings, 'USE_TZ', False) and timezone.is_aware(teraz):
                teraz = timezone.localtime(teraz).replace(tzinfo=None)

            UlohaModel = apps.get_model('Evidencia', 'Uloha')
            if UlohaModel:
                blizsie_info_riadky = [
                    f"🚨 REKLAMÁCIA Z WEBU (#{objednavka.cislo_objednavky_interne})",
                    f"Doklad: {faktura}"
                ]
                if zariadenie_manual:
                    blizsie_info_riadky.append(f"Špecifikácia / S/N zo štítku: {zariadenie_manual}")
                blizsie_info_riadky.append(f"Popis vady:\n{popis}")

                uloha = UlohaModel.objects.create(
                    zakaznik=zakaznik,
                    zaciatok=teraz,
                    dokoncene=False,
                    je_reklamacia=True,
                    datum_prijatia_reklamacie=teraz,
                    blizsie_informacie="\n".join(blizsie_info_riadky),
                    farba='#ef4444'
                )

                if zariadenie_obj:
                    uloha.zariadenia.add(zariadenie_obj)
                objednavka.vytvorene_ulohy.add(uloha)
        
        if priloha:
            objednavka.subor_objednavky = priloha
            objednavka.save(update_fields=['subor_objednavky'])

        from .tasks import notify_web_inquiry_task
        def _safe_notify_special_lead(pk=objednavka.pk):
            try:
                notify_web_inquiry_task.delay(pk)
            except Exception as exc:
                logger.warning(f"Výpadok Celery/Redis brokera pri notifikácii špeciálneho leadu #{pk}: {exc}")
        transaction.on_commit(_safe_notify_special_lead)

        return JsonResponse({'status': 'success', 'message': 'Vaša požiadavka bola úspešne prijatá.'})
    return JsonResponse({'status': 'error'}, status=400)

def get_holidays_for_location(target_date: date, location_str: str = "", b2b_klient=None):
    """
    🌍 ENTERPRISE SMART HOLIDAY ENGINE:
    Zisťuje štátne a REGIONÁLNE sviatky (subdiv) pre pobočky a klientov naprieč celou Európskou úniou.
    Zohľadňuje profil B2B klienta alebo zadané PSČ/mesto z verejného webu.
    """
    if not target_date:
        return {}

    raw_location = ""
    if b2b_klient:
        raw_location = f"{b2b_klient.psc or ''} {b2b_klient.mesto or ''} {b2b_klient.stat or ''}".strip()
    if not raw_location and location_str:
        raw_location = str(location_str).strip()

    # 1. Zistenie štátu
    iso = None
    if raw_location and hasattr(FiremnaIdentita, 'get_aktivna_krajina_iso'):
        iso = FiremnaIdentita.get_aktivna_krajina_iso(raw_location)
        if iso in ('NEZADANE', 'INYKRAJINA'):
            iso = FiremnaIdentita.get_aktivna_krajina_iso() # Fallback na centrálu
    elif hasattr(FiremnaIdentita, 'get_aktivna_krajina_iso'):
        iso = FiremnaIdentita.get_aktivna_krajina_iso()

    if not iso and hasattr(FiremnaIdentita, 'get_country_defaults'):
        iso = FiremnaIdentita.get_country_defaults().get('iso_kod')

    iso = (iso or "").upper()
    if not iso:
        return {}

    subdiv = None
    StatLokalizaciaModel = apps.get_model('ISASaplikacia', 'StatLokalizacia')
    if StatLokalizaciaModel and hasattr(StatLokalizaciaModel, 'rozpoznaj_subdiviziu'):
        subdiv = StatLokalizaciaModel.rozpoznaj_subdiviziu(iso, raw_location)

    # Načítanie sviatkov s regionálnym parametrom (defenzívna ochrana pred neznámymi ISO kódmi a pádmi)
    try:
        if subdiv:
            try:
                return holidays.country_holidays(iso, subdiv=subdiv, years=target_date.year)
            except (NotImplementedError, KeyError, ValueError, Exception):
                pass
        return holidays.country_holidays(iso, years=target_date.year)
    except (NotImplementedError, KeyError, ValueError, Exception):
        return {}


def _build_booking_slots(work_start, midpoint, work_end, sluzba_id=None, sposob_realizacie='VYJAZD'):
    """Vypočíta povolené časové okná pre daný deň: pre DIELNA s definovaným oknom v minútach presné bloky, inak 2 poldenné bloky."""
    trvanie_min = None
    if sposob_realizacie == 'DIELNA' and sluzba_id and str(sluzba_id).strip().isdigit():
        trvanie_min = (
            TypServisuPrace.objects.filter(pk=int(str(sluzba_id).strip()), zobrazit_na_webe=True)
            .values_list('booking_trvanie_min', flat=True)
            .first()
        )

    if trvanie_min and trvanie_min >= 15:
        slot_delta = timedelta(minutes=int(trvanie_min))
        if slot_delta <= (work_end - work_start):
            custom_slots = []
            cur = work_start
            while cur + slot_delta <= work_end:
                s_end = cur + slot_delta
                s_start_str = cur.strftime('%H:%M')
                s_end_str = s_end.strftime('%H:%M')
                custom_slots.append({
                    'id': f'{s_start_str}-{s_end_str}',
                    'label': f'{s_start_str} - {s_end_str}',
                    'start': cur,
                    'end': s_end,
                })
                cur = s_end
            if custom_slots:
                return custom_slots

    str_start = work_start.strftime('%H:%M')
    str_mid = midpoint.strftime('%H:%M')
    str_end = work_end.strftime('%H:%M')
    return [
        {'id': 'DOPOLUDNIA', 'label': f'Dopoludnia ({str_start} - {str_mid})', 'start': work_start, 'end': midpoint},
        {'id': 'POPOLUDNI', 'label': f'Popoludní ({str_mid} - {str_end})', 'start': midpoint, 'end': work_end},
    ]


def get_available_slots(request):
    """📅 ISAS SMART BOOKING: Vráti voľné termíny pre daný deň (Zero Data Leak)."""
    date_str = request.GET.get('date')
    if not date_str:
        return JsonResponse({'status': 'error', 'message': 'Dátum je povinný.'}, status=400)

    gn = GlobalneNastavenia.objects.first()
    pracovat_vikendy = getattr(gn, 'pracovat_vikendy', False)
    pracovat_sviatky = getattr(gn, 'pracovat_sviatky', False)

    try:
        target_date = parse_date(date_str)
        # 🛡️ EDGE CASE 3: Blokujeme minulosť aj "Dnes". Booking je možný najskôr na zajtra.
        if not target_date or target_date <= timezone.now().date():
            return JsonResponse({'status': 'success', 'slots': []})
            
        # 🛡️ EDGE CASE 2: Blokujeme víkendy (Ak to firma nepovolila)
        if target_date.weekday() >= 5 and not pracovat_vikendy:
            return JsonResponse({'status': 'success', 'slots': []})
            
        # 🛡️ EDGE CASE 4: Blokujeme sviatky podľa sídla firmy alebo pobočky/klienta (vrátane subdiv v DE, AT, CH, ES)
        if not pracovat_sviatky:
            location_param = request.GET.get('location', '').strip()
            try:
                b2b_klient = getattr(request.user, 'zakaznik_profil', None) if request.user.is_authenticated else None
            except (ObjectDoesNotExist, AttributeError):
                b2b_klient = None
            sviatky = get_holidays_for_location(target_date, location_str=location_param, b2b_klient=b2b_klient)

            if target_date in sviatky:
                return JsonResponse({'status': 'success', 'slots': []})
            
    except ValueError:
        return JsonResponse({'status': 'error', 'message': 'Neplatný formát dátumu.'}, status=400)

    # 🛡️ ANTI BRUTE-FORCE OCHRANA (Rate Limiting)
    ip_adresa = get_client_ip(request)
    cache_key = f"booking_slots_limit_{ip_adresa}"
    
    try:
        pokusy = cache.incr(cache_key)
    except ValueError:
        cache.set(cache_key, 1, 60)
        pokusy = 1
        
    if pokusy > 50:
        return JsonResponse({'status': 'error', 'message': 'Príliš veľa požiadaviek.'}, status=429)
    
    # 1. Zistíme, koľko máme reálne aktívnych technikov (ochrana pred započítaním prepustených/anonymizovaných)
    technici = Zamestnanec.objects.filter(pracovna_rola='TE', je_anonymizovany=False)
    total_techs = technici.count()
    if total_techs == 0:
        return JsonResponse({'status': 'success', 'slots': []})
    
    # Dynamická pracovná doba z ERP (Ochrana pred pádom a chybným nastavením)
    work_start, midpoint, work_end = get_work_shifts(gn, target_date)
    if not (work_start and midpoint and work_end):
        return JsonResponse({'status': 'success', 'slots': []})

    # 2. 🧠 ISAS ABSENCE ENGINE: Zohľadníme dovolenky, PN a paragrafy (výhradne aktívnych technikov)
    absencie_dnes = Nepritomnost.objects.filter(
        datum_od__lte=target_date,
        datum_do__gte=target_date,
        stav__in=['SCHVALENE', 'CAKA'],
        zamestnanec__pracovna_rola='TE',
        zamestnanec__je_anonymizovany=False,
    )
    
    full_absences = absencie_dnes.filter(pol_dna=False).values('zamestnanec').distinct().count()
    half_absences = absencie_dnes.filter(pol_dna=True).values('zamestnanec').distinct().count()
    
    # Základná kapacita (Počet technikov mínus celodenné absencie)
    base_capacity = total_techs - full_absences

    # 3. Vytiahneme všetky nedokončené úlohy na daný deň (okrem úloh patriacich výhradne zamietnutým objednávkam)
    ulohy_vsetky = Uloha.objects.filter(
        dokoncene=False
    ).annotate(
        _ma_zamietnutu=Exists(PrijataObjednavka.objects.filter(vytvorene_ulohy=OuterRef('pk'), stav='ZAMIETNUTA')),
        _ma_aktivnu=Exists(PrijataObjednavka.objects.filter(vytvorene_ulohy=OuterRef('pk')).exclude(stav='ZAMIETNUTA')),
    ).exclude(
        _ma_zamietnutu=True, _ma_aktivnu=False
    ).filter(
        Q(zaciatok__date__lte=target_date) & 
        (Q(koniec__date__gte=target_date) | Q(koniec__isnull=True, zaciatok__date=target_date))
    ).prefetch_related('technici')

    # 4. 🧠 ISAS FIELD SERVICE ENGINE: Výpočet obsadenosti blokov (Defenzívna normalizácia naive/aware pre USE_TZ=False)
    use_tz = getattr(settings, 'USE_TZ', False)
    if not use_tz:
        if work_start and timezone.is_aware(work_start):
            work_start = work_start.replace(tzinfo=None)
        if midpoint and timezone.is_aware(midpoint):
            midpoint = midpoint.replace(tzinfo=None)
        if work_end and timezone.is_aware(work_end):
            work_end = work_end.replace(tzinfo=None)

    sluzba_id = request.GET.get('sluzba_id')
    sposob_realizacie = request.GET.get('sposob_realizacie', 'VYJAZD')
    candidate_slots = _build_booking_slots(
        work_start, midpoint, work_end,
        sluzba_id=sluzba_id,
        sposob_realizacie=sposob_realizacie
    )

    normalized_tasks = []
    for u in ulohy_vsetky:
        u_start = u.zaciatok
        if u_start and not use_tz and timezone.is_aware(u_start):
            u_start = u_start.replace(tzinfo=None)
        if not u_start:
            continue

        u_end = u.koniec or (u_start + timedelta(hours=3))
        if u_end and not use_tz and timezone.is_aware(u_end):
            u_end = u_end.replace(tzinfo=None)

        tech_count = len(u.technici.all())
        normalized_tasks.append((u_start, u_end, tech_count if tech_count > 0 else 1))

    # 🛡️ DEFENZÍVNA LOGIKA: Poldenná absencia znižuje kapacitu všetkých blokov,
    # pretože nevieme, ktorú polovicu dňa technik chýba.
    available_slots = []
    for slot in candidate_slots:
        tasks_overlap = sum(
            t_count for u_start, u_end, t_count in normalized_tasks
            if u_start < slot['end'] and u_end > slot['start']
        )
        cap = base_capacity - half_absences - tasks_overlap
        if cap >= 1:
            available_slots.append({'id': slot['id'], 'label': slot['label']})

    return JsonResponse({'status': 'success', 'slots': available_slots})

@transaction.atomic
def submit_booking(request):
    """📅 ISAS SMART BOOKING: Vytvorenie rezervácie a úlohy v ERP."""
    if request.method == 'POST':
        if request.POST.get('website_url'): return JsonResponse({'status': 'success'}) # Honeypot
        
        datum = request.POST.get('datum')
        cas = request.POST.get('cas') # DOPOLUDNIA / POPOLUDNI
        sluzba_id = request.POST.get('sluzba_id')
        sposob_realizacie = request.POST.get('sposob_realizacie', 'VYJAZD')
        stredisko_info = request.POST.get('stredisko_info', '').strip()[:150]
        mesto = request.POST.get('mesto', '').strip()[:100]
        firma = request.POST.get('firma', '').strip()[:100]
        email = request.POST.get('email', '').strip().lower()[:254]
        telefon = request.POST.get('telefon', '').strip()[:50]
        poznamka = request.POST.get('poznamka', '').strip()[:5000]

        if sposob_realizacie == 'DIELNA':
            mesto = stredisko_info or "Servisné stredisko (príjem na pobočke)"
        
        if not datum or not cas or not email or not mesto:
            return JsonResponse({'status': 'error', 'message': 'Dátum, čas, lokalita a e-mail sú povinné.'}, status=400)

        cas = str(cas).strip()

        if not _is_valid_email_address(email):
            return JsonResponse({'status': 'error', 'message': 'Zadajte platný formát e-mailovej adresy.'}, status=400)

        b2b_klient_auth, _ = get_b2b_client_and_discount(request)
        gdpr_raw = str(request.POST.get('gdpr_suhlas') or '').strip().lower()
        gdpr_accepted = gdpr_raw in ('on', 'true', '1', 'yes', 'ano')

        if not b2b_klient_auth and not gdpr_accepted:
            return JsonResponse({'status': 'error', 'message': 'Pre odoslanie rezervácie je potrebný súhlas so spracovaním osobných údajov (GDPR).'}, status=400)

        # 🛡️ Rate Limiting (Ochrana pred spamovaním klikov)
        ip_adresa = get_client_ip(request)
        cache_key = f"booking_submit_limit_{ip_adresa}"
        if not cache.add(cache_key, 'locked', 60):
            return JsonResponse({'status': 'error', 'message': 'Rezerváciu môžete vytvoriť len raz za minútu.'}, status=429)

        try:            
            gn = GlobalneNastavenia.objects.first()
            target_date = parse_date(datum)
            if not target_date:
                cache.delete(cache_key)
                return JsonResponse({'status': 'error', 'message': 'Neplatný formát dátumu.'}, status=400)

            # 🛡️ OCHRANA PRED BYPASSOM: Zákaz rezervácie do minulosti a na dnešok
            if target_date <= timezone.now().date():
                cache.delete(cache_key)
                return JsonResponse({'status': 'error', 'message': 'Rezervácia je možná najskôr na nasledujúci pracovný deň.'}, status=400)

            # 🛡️ OCHRANA PRED BYPASSOM: Validácia víkendov
            pracovat_vikendy = getattr(gn, 'pracovat_vikendy', False)
            if target_date.weekday() >= 5 and not pracovat_vikendy:
                cache.delete(cache_key)
                return JsonResponse({'status': 'error', 'message': 'Počas víkendov rezervácie neprijímame.'}, status=400)

            # 🛡️ OCHRANA PRED BYPASSOM: Validácia sviatkov vrátane regiónov a pobočiek (DE, AT, CH, ES)
            pracovat_sviatky = getattr(gn, 'pracovat_sviatky', False)
            if not pracovat_sviatky:
                try:
                    b2b_klient_booking = getattr(request.user, 'zakaznik_profil', None) if request.user.is_authenticated else None
                except (ObjectDoesNotExist, AttributeError):
                    b2b_klient_booking = None
                sviatky = get_holidays_for_location(target_date, location_str=mesto, b2b_klient=b2b_klient_booking)

                if target_date in sviatky:
                    cache.delete(cache_key)
                    return JsonResponse({'status': 'error', 'message': 'Na vybraný deň pripadá štátny alebo regionálny sviatok. Zvoľte prosím pracovný deň.'}, status=400)
            
            # 🚀 DYNAMICKÝ ČAS ÚLOHY (Ochrana pred pádom a chybným nastavením)
            work_start, midpoint, work_end = get_work_shifts(gn, target_date)
            if not (work_start and midpoint and work_end):
                cache.delete(cache_key)
                return JsonResponse({'status': 'error', 'message': 'Pracovná doba pre vybraný deň nie je nastavená.'}, status=400)

            # Defenzívna normalizácia naive/aware pre USE_TZ=False ihneď pred priradením
            use_tz_submit = getattr(settings, 'USE_TZ', False)
            if not use_tz_submit:
                if timezone.is_aware(work_start):
                    work_start = work_start.replace(tzinfo=None)
                if timezone.is_aware(midpoint):
                    midpoint = midpoint.replace(tzinfo=None)
                if timezone.is_aware(work_end):
                    work_end = work_end.replace(tzinfo=None)

            candidate_slots = _build_booking_slots(
                work_start, midpoint, work_end,
                sluzba_id=sluzba_id,
                sposob_realizacie=sposob_realizacie
            )
            selected_slot = next((s for s in candidate_slots if s['id'] == cas), None)
            if not selected_slot:
                cache.delete(cache_key)
                return JsonResponse({'status': 'error', 'message': 'Neplatný alebo neaktuálny časový blok rezervácie.'}, status=400)

            zaciatok = selected_slot['start']
            koniec = selected_slot['end']
            slot_label = selected_slot['label']
                
        except Exception:
            return JsonResponse({'status': 'error', 'message': 'Neplatný formát dátumu alebo času.'}, status=400)

        # 🛡️ RACE CONDITION LOCK: Zámok na konkrétny deň chráni aj proti súbehu rôzne dlhých blokov
        lock_id = f"booking_lock_{target_date.strftime('%Y%m%d')}"
        if not cache.add(lock_id, 'locked', timeout=10):
            return JsonResponse({'status': 'error', 'message': 'Tento termín sa práve spracováva iným zákazníkom. Skúste to o chvíľu.'}, status=409)

        try:
            # 🧠 RE-VALIDÁCIA KAPACITY (Ochrana pred súbežným zápisom)
            technici = Zamestnanec.objects.filter(pracovna_rola='TE', je_anonymizovany=False)
            total_techs = technici.count()
            
            absencie = Nepritomnost.objects.filter(
                datum_od__lte=target_date,
                datum_do__gte=target_date,
                stav__in=['SCHVALENE', 'CAKA'],
                zamestnanec__pracovna_rola='TE',
                zamestnanec__je_anonymizovany=False,
            )
            full_absences = absencie.filter(pol_dna=False).values('zamestnanec').distinct().count()
            half_absences = absencie.filter(pol_dna=True).values('zamestnanec').distinct().count()
            
            base_capacity = total_techs - full_absences - half_absences
            
            ulohy_vsetky = Uloha.objects.filter(dokoncene=False).annotate(
                _ma_zamietnutu=Exists(PrijataObjednavka.objects.filter(vytvorene_ulohy=OuterRef('pk'), stav='ZAMIETNUTA')),
                _ma_aktivnu=Exists(PrijataObjednavka.objects.filter(vytvorene_ulohy=OuterRef('pk')).exclude(stav='ZAMIETNUTA')),
            ).exclude(
                _ma_zamietnutu=True, _ma_aktivnu=False
            ).filter(
                Q(zaciatok__date__lte=target_date) & 
                (Q(koniec__date__gte=target_date) | Q(koniec__isnull=True, zaciatok__date=target_date))
            ).prefetch_related('technici')
            
            tasks_count = 0
            for u in ulohy_vsetky:
                u_start = u.zaciatok
                if u_start and not use_tz_submit and timezone.is_aware(u_start):
                    u_start = u_start.replace(tzinfo=None)
                if not u_start:
                    continue

                u_end = u.koniec or (u_start + timedelta(hours=3))
                if u_end and not use_tz_submit and timezone.is_aware(u_end):
                    u_end = u_end.replace(tzinfo=None)
                
                if u_start < koniec and u_end > zaciatok:
                    tech_count = len(u.technici.all())
                    tasks_count += tech_count if tech_count > 0 else 1
                    
            if (base_capacity - tasks_count) < 1:
                cache.delete(cache_key)
                return JsonResponse({'status': 'error', 'message': 'Je nám ľúto, ale tento termín sa práve obsadil. Prosím, vyberte si iný.'}, status=400)

            # 💾 ZÁPIS DO DATABÁZY
            with transaction.atomic():
                # Defenzívne vytvorenie zákazníka v savepointe
                zakaznik = Zakaznik.objects.select_for_update().filter(email__iexact=email, je_interny_profil=False).first()
                if not zakaznik:
                    try:
                        with transaction.atomic():
                            zakaznik = Zakaznik.objects.create(
                                Nazov_spolocnosti=firma or f"Klient ({email})",
                                email=email,
                                telefon=telefon,
                                Ostatne="Vytvorené cez Smart Booking."
                            )
                    except Exception:
                        zakaznik = Zakaznik.objects.filter(email__iexact=email, je_interny_profil=False).first()
                else:
                    if not zakaznik.telefon and telefon:
                        zakaznik.telefon = telefon
                        zakaznik.save(update_fields=['telefon'])

                if not zakaznik:
                    return JsonResponse({'status': 'error', 'message': 'Nepodarilo sa vytvoriť zákaznícky profil pre rezerváciu.'}, status=500)

                logistika_popis = f"🏢 Príjem na stredisku: {stredisko_info} (Bez poplatku za dopravu)" if sposob_realizacie == 'DIELNA' else f"🚙 Výjazd technika k zákazníkovi (Lokalita: {mesto})"

                objednavka = PrijataObjednavka.objects.create(
                    zakaznik=zakaznik,
                    stav='NOVA',
                    priorita='STREDNA',
                    termin_dodania=target_date,
                    forma_prijatia='WEB',
                    specificky_email_pre_fakturu=email,
                    poznamka_klienta=f"ONLINE REZERVÁCIA TERMÍNU: {datum} ({slot_label})\nSpôsob realizácie: {logistika_popis}\n\n{poznamka}",
                    interna_poznamka=f"📅 SMART BOOKING: {logistika_popis}. Potvrdiť harmonogram."
                )

                uloha = Uloha.objects.create(
                    zakaznik=zakaznik,
                    zaciatok=zaciatok,
                    koniec=koniec,
                    blizsie_informacie=f"📍 Realizácia: {logistika_popis}\nOnline rezervácia z webu.\nPreferovaný blok: {slot_label}\n\n{poznamka}\n\n⚠️ DISPEČER: Skontrolovať logistiku a potvrdiť zákazníkovi čas!",
                    farba='#10b981',
                    dokoncene=False
                )
                
                if sluzba_id and str(sluzba_id).strip().isdigit():
                    try:
                        with transaction.atomic():
                            sluzba = TypServisuPrace.objects.filter(
                                pk=int(str(sluzba_id).strip()), zobrazit_na_webe=True
                            ).first()
                            if sluzba:
                                uloha.typ_servisu.add(sluzba)
                                objednavka.typ_servisu.add(sluzba)
                    except (TypServisuPrace.DoesNotExist, ValueError, TypeError, DataError):
                        pass

                objednavka.vytvorene_ulohy.add(uloha)

            # 4. Notifikácia
            from .tasks import notify_web_inquiry_task
            def _safe_notify_booking(pk=objednavka.pk):
                try:
                    notify_web_inquiry_task.delay(pk)
                except Exception as exc:
                    logger.warning(f"Výpadok Celery/Redis brokera pri notifikácii rezervácie #{pk}: {exc}")
            transaction.on_commit(_safe_notify_booking)

            return JsonResponse({'status': 'success', 'message': 'Termín bol úspešne rezervovaný!', 'cislo_objednavky': objednavka.cislo_objednavky_interne})

        finally:
            cache.delete(lock_id)
            
    return JsonResponse({'status': 'error'}, status=400)

# =====================================================================
# 🏢 FÁZA 4: B2B KLIENTSKA ZÓNA (Login & Dashboard)
# =====================================================================

def b2b_login_view(request):
    """🔒 ISAS B2B GATEWAY: Bezpečné prihlásenie pre zákazníkov s Rate Limitingom."""
    if request.user.is_authenticated:
        try:
            if getattr(request.user, 'zakaznik_profil', None):
                return redirect('webbuilder:b2b_dashboard')
        except (ObjectDoesNotExist, AttributeError):
            pass
            
        if request.user.is_staff:
            return redirect('/admin/')
        logout(request)

    is_preview = _resolve_preview_mode(request)
    identita = FiremnaIdentita.objects.first() or FiremnaIdentita(nazov_spolocnosti="Naša Spoločnosť")
    gn = GlobalneNastavenia.objects.first()
    mena_firmy = getattr(identita, 'predvolena_mena', None) or get_firemna_mena()
    default_titulok = identita.nazov_spolocnosti if (getattr(identita, 'pk', None) and identita.nazov_spolocnosti) else "ISAS CORE WEB"
    nastavenia = WebNastavenia.objects.filter(pk=2 if is_preview else 1).first() or WebNastavenia.objects.first() or WebNastavenia(hlavna_farba="#0284c7", titulok_webu=default_titulok)
    web_ver = cache.get('web_cache_version', 1)
    aktivne_bloky_set = get_active_web_blocks(cache_ver=web_ver, is_preview=is_preview)
    raw_next = (request.POST.get('next') or request.GET.get('next') or '').strip()
    safe_next_url = raw_next if (
        raw_next and url_has_allowed_host_and_scheme(raw_next, allowed_hosts={request.get_host()}, require_https=request.is_secure())
    ) else ''
    context = {
        'nastavenia': nastavenia,
        'identita': identita,
        'globalne_nastavenia': gn,
        'industry_terms': get_all_industry_terms(),
        'is_preview': is_preview,
        'mena': mena_firmy,
        'next_url': safe_next_url,
        'nav_katalog_menu': get_nav_katalog_menu(cache_ver=web_ver, is_preview=is_preview, aktivne_bloky=aktivne_bloky_set),
        'nav_flags': get_web_nav_flags(cache_ver=web_ver, is_preview=is_preview, aktivne_bloky=aktivne_bloky_set),
        'cart_env': {'mena': mena_firmy, 'b2bZlava': 0, 'vyzadujeGatePass': False, 'dispecingInfo': ''},
    }

    if request.method == 'POST':
        # 🛡️ ANTI BRUTE-FORCE OCHRANA
        ip_adresa = get_client_ip(request)
        cache_key = f"b2b_login_limit_{ip_adresa}"
        pokusy = cache.get(cache_key, 0)
        
        if pokusy >= 10:
            context['error'] = 'Príliš veľa neúspešných pokusov. Z bezpečnostných dôvodov bol prístup zablokovaný na 15 minút.'
            return render(request, 'web/b2b_login.html', context, status=429)

        u = request.POST.get('username', '').strip()
        p = request.POST.get('password', '')
        
        # 🛡️ B2B EDGE CASE: Umožnenie prihlásenia e-mailom, ak klient zadal e-mail namiesto username (podpora custom User modelu)
        if '@' in u:
            User = get_user_model()
            user_by_email = User.objects.filter(email__iexact=u, zakaznik_profil__isnull=False).first()
            if user_by_email:
                u = getattr(user_by_email, 'username', None) or getattr(user_by_email, getattr(User, 'USERNAME_FIELD', 'email'), u)

        user = authenticate(request, username=u, password=p)
        
        b2b_klient = None
        if user is not None and getattr(user, 'is_active', False):
            # 🛡️ BEZPEČNOSŤ: Pustíme len aktívnych B2B klientov (defenzívna ochrana pred ObjectDoesNotExist aj AttributeError)
            try:
                b2b_klient = getattr(user, 'zakaznik_profil', None)
            except (ObjectDoesNotExist, AttributeError):
                b2b_klient = None

        if b2b_klient:
            login(request, user)
            zapamatat = request.POST.get('remember_me') == 'on'
            if zapamatat:
                request.session.set_expiry(1209600)  # 14 dní
            else:
                request.session.set_expiry(0)  # do zatvorenia prehliadača
            cache.delete(cache_key)
            next_url = request.POST.get('next') or request.GET.get('next')
            if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
                return redirect(next_url)
            return redirect('webbuilder:b2b_dashboard')
        else:
            try:
                cache.incr(cache_key)
            except ValueError:
                cache.set(cache_key, 1, 900) # Zámok na 15 minút
            context['error'] = 'Nesprávne prihlasovacie meno alebo heslo.'

    return render(request, 'web/b2b_login.html', context)

@login_required(login_url='webbuilder:b2b_login')
def b2b_dashboard_view(request):
    """📊 ISAS B2B DASHBOARD: Klientska zóna (Zero Data Leak)."""
    try:
        b2b_klient = getattr(request.user, 'zakaznik_profil', None)
    except (ObjectDoesNotExist, AttributeError):
        b2b_klient = None
    
    # 🛡️ ISAS ARCHITECT: Ak sem vstúpi interný zamestnanec alebo deaktivovaný účet, zamedzíme prístupu
    if not getattr(request.user, 'is_active', False) or not b2b_klient:
        if getattr(request.user, 'is_active', False) and request.user.is_staff:
            return redirect('/admin/')
        logout(request)
        return redirect('webbuilder:b2b_login')

    # 🛡️ ZERO DATA LEAK + MAXIMÁLNY VÝKON: Načítanie vlastnených aj prenajatých strojov klienta (ZakaznikPrenajom)
    param_relations = get_active_parameter_reverse_names()
    PlanovanyServisModel = apps.get_model('Evidencia', 'PlanovanyServisZariadenia')
    zariadenia = ServisovaneZariadenie.objects.filter(
            Q(zakaznik=b2b_klient) | Q(prenajmy__zakaznik=b2b_klient)
        ).distinct().select_related(
            'vyrobca', *param_relations
        ).prefetch_related(
            Prefetch(
                'planovane_servisy',
                queryset=PlanovanyServisModel.objects.select_related('typ_servisu').order_by('planovany_datum', 'planovane_motohodiny')
            ) if PlanovanyServisModel else 'id'
        ).defer('poznamka', 'iot_api_kluc', 'iot_mac_adresa', 'iot_ip_adresa')

    dnes = timezone.now().date()
    zariadenia = list(zariadenia)
    for s in zariadenia:
        s.kategoria_farba = ZARIADENIA_COLOR_MAP.get(s.kategoria, '') if isinstance(ZARIADENIA_COLOR_MAP, dict) else ''
        try:
            s.compliance_audit = get_zariadenie_compliance_audit(s, referencny_datum=dnes)
        except Exception:
            s.compliance_audit = []

    industry_terms = get_all_industry_terms()

    faktury_qs = VystavenaFaktura.objects.filter(zakaznik=b2b_klient).exclude(stav__in=['DRAFT', 'STORNO'])
    pocet_faktur = faktury_qs.count()
    faktury = faktury_qs.only(
        'id', 'cislo_faktury', 'variabilny_symbol', 'datum_vystavenia', 
        'datum_splatnosti', 'celkom_s_dph', 'stav', 'pdf_dokument', 
        'typ_dokladu', 'cislo_objednavky_klienta', 'mena', 'dodavatel_iban'
    ).order_by('-datum_vystavenia', '-id')[:30]

    objednavky_qs = PrijataObjednavka.objects.filter(zakaznik=b2b_klient)
    pocet_objednavok = objednavky_qs.count()
    objednavky = objednavky_qs.prefetch_related(
        'zariadenia'
    ).only(
        'id', 'cislo_objednavky_interne', 'cislo_objednavky_klienta', 'datum_prijatia', 
        'termin_dodania', 'celkova_suma_bez_dph', 'poznamka_klienta', 'stav', 
        'priorita', 'subor_objednavky', 'mena'
    ).order_by('-datum_prijatia', '-id')[:50]

    # Zoznam uzavretých servisných protokolov a revíznych správ pre ISO audity a Inšpektorát práce
    protokoly_qs = ServisnyZaznam.objects.filter(zakaznik=b2b_klient, dokoncene=True)
    pocet_protokolov = protokoly_qs.count()
    protokoly = protokoly_qs.select_related('zariadenie').prefetch_related('typ_servisu').only(
        'id', 'vytvorene', 'cas_podpisu', 'poruchy', 'formalne_zhodnotenie_ai', 'protokol_uzavrety', 'bezpecnostny_kluc', 
        'uzavrety_pdf_dokument', 'stav_motohodin', 'meno_podpisujuceho', 'zariadenie'
    ).order_by('-vytvorene', '-id')[:30]

    # 🚨 SYMBIÓZA S IOT: Aktívne nevyriešené alarmy patriace technológiám tohto klienta
    aktivne_alarmy = list(AlarmLog.objects.filter(
        zakaznik=b2b_klient,
        vyriesene=False,
        zariadenie__isnull=False
    ).select_related('zariadenie').only(
        'id', 'kod_chyby', 'popis', 'casova_peciatka', 'zariadenie'
    ).order_by('-casova_peciatka', '-id')[:10])
    
    is_preview = _resolve_preview_mode(request)
    identita = FiremnaIdentita.objects.first() or FiremnaIdentita(nazov_spolocnosti="Naša Spoločnosť")
    gn = GlobalneNastavenia.objects.first()
    mena_firmy = getattr(identita, 'predvolena_mena', None) or get_firemna_mena()
    _, zlava_dec = get_b2b_client_and_discount(request)
    dispecing_text = f"Náš dispečing požiadavku spracuje v prevádzkovom čase ({gn.pracovny_cas_od.strftime('%H:%M')} – {gn.pracovny_cas_do.strftime('%H:%M')}{'' if getattr(gn, 'pracovat_vikendy', False) else ', v pracovné dni'})." if (gn and getattr(gn, 'pracovny_cas_od', None) and getattr(gn, 'pracovny_cas_do', None)) else ""
    default_titulok = identita.nazov_spolocnosti if (getattr(identita, 'pk', None) and identita.nazov_spolocnosti) else "ISAS CORE WEB"
    nastavenia = WebNastavenia.objects.filter(pk=2 if is_preview else 1).first() or WebNastavenia.objects.first() or WebNastavenia(hlavna_farba="#0284c7", titulok_webu=default_titulok)

    # 📡 Dynamické určenie IoT/SCADA domény podľa konfigurácie v settings.py a .env
    telemetry_base = getattr(settings, 'MAIN_TELEMETRY_DOMAIN', 'localhost')
    if settings.DEBUG and telemetry_base == 'localhost' and ':' in request.get_host():
        telemetry_domain = f"localhost:{request.get_host().split(':')[1]}"
    else:
        telemetry_domain = telemetry_base

    web_ver = cache.get('web_cache_version', 1)
    aktivne_bloky_set = get_active_web_blocks(cache_ver=web_ver, is_preview=is_preview)
    web_ver = cache.get('web_cache_version', 1)
    aktivne_bloky_set = get_active_web_blocks(cache_ver=web_ver, is_preview=is_preview)
    context = {
        'klient': b2b_klient,
        'zariadenia': zariadenia,
        'objednavky': objednavky,
        'pocet_objednavok': pocet_objednavok,
        'protokoly': protokoly,
        'pocet_protokolov': pocet_protokolov,
        'faktury': faktury,
        'pocet_faktur': pocet_faktur,
        'aktivne_alarmy': aktivne_alarmy,
        'nastavenia': nastavenia,
        'identita': identita,
        'globalne_nastavenia': gn,
        'is_preview': is_preview,
        'telemetry_domain': telemetry_domain,
        'mena': mena_firmy,
        'dnes': dnes,
        'industry_terms': industry_terms,
        'compliance_disclaimer': get_legal_disclaimer(),
        'nav_katalog_menu': get_nav_katalog_menu(cache_ver=web_ver, is_preview=is_preview, aktivne_bloky=aktivne_bloky_set),
        'nav_flags': get_web_nav_flags(cache_ver=web_ver, is_preview=is_preview, aktivne_bloky=aktivne_bloky_set),
        'cart_env': {
            'mena': mena_firmy,
            'b2bZlava': float(zlava_dec),
            'vyzadujeGatePass': bool(getattr(b2b_klient, 'vyzaduje_povolenie_vstupu', False)),
            'dispecingInfo': dispecing_text,
        },
    }

    return render(request, 'web/b2b_dashboard.html', context)

def b2b_logout_view(request):
    """🚪 Odhlásenie B2B klienta."""
    logout(request)
    return redirect('webbuilder:home')


class NastrojeView(WebBaseMixin, TemplateView):
    """🔬 ISAS ENGINEERING HUB: Samostatná MPA podstránka so všetkými diagnostickými a ESG nástrojmi."""
    template_name = 'web/nastroje.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        b2b_klient, _ = get_b2b_client_and_discount(self.request)

        active_hub_config = dict(HUB_CONFIG)
        krajina_iso = FiremnaIdentita.get_aktivna_krajina_iso() if hasattr(FiremnaIdentita, 'get_aktivna_krajina_iso') else None
        compliance_cfg = get_compliance_config(iso=krajina_iso)
        if compliance_cfg:
            for k, v in compliance_cfg.items():
                if isinstance(v, (int, float, str, bool, dict, list)):
                    active_hub_config[k] = v

        if b2b_klient:
            if getattr(b2b_klient, 'cena_za_kwh_eur', None) and b2b_klient.cena_za_kwh_eur > Decimal('0.000'):
                active_hub_config['esg_default_price'] = str(b2b_klient.cena_za_kwh_eur)
            if getattr(b2b_klient, 'emisny_faktor_co2', None) and b2b_klient.emisny_faktor_co2 > Decimal('0.0000'):
                active_hub_config['esg_co2_factor'] = str(b2b_klient.emisny_faktor_co2)

        identita = context.get('identita')
        active_hub_config['mena'] = getattr(identita, 'predvolena_mena', None) or get_firemna_mena()

        has_esg_enabled = bool(is_esg_audit_supported() and active_hub_config.get('esg_idle_factor') is not None)
        has_rekuperacia_enabled = bool(HAS_HEAT_RECOVERY and active_hub_config.get('rekuperacia_factor'))
        has_tco_enabled = bool(TCO_PROFILES and isinstance(TCO_PROFILES, dict))
        has_vtz_enabled = bool((HAS_VTZ_INSPECTION or active_hub_config.get('revizia_perioda_1_roky') or active_hub_config.get('revizia_perioda_2_roky')) and (active_hub_config.get('revizia_perioda_1_roky') or active_hub_config.get('revizia_perioda_2_roky')))
        has_fplyny_enabled = bool(HAS_FPLYNY_AUDIT and FPLYNY_CHLADIVA and isinstance(FPLYNY_CHLADIVA, list))
        has_media_quality_enabled = bool(MEDIA_QUALITY_APPS and isinstance(MEDIA_QUALITY_APPS, list))
        has_vibro_enabled = bool(HAS_VIBRO_DIAGNOSTICS and active_hub_config.get('vibro_limits') and active_hub_config.get('vibro_zones'))
        has_capex_enabled = bool(HAS_CAPEX_CALCULATOR and active_hub_config.get('capex_years'))

        esg_card_count = (1 if has_esg_enabled else 0) + (1 if has_rekuperacia_enabled else 0) + (1 if has_tco_enabled else 0)
        law_card_count = (1 if has_vtz_enabled else 0) + (1 if has_fplyny_enabled else 0)
        try:
            has_iot_demo_enabled = float(get_industry_term('gauge_1_base', 0) or 0) > 0
        except (ValueError, TypeError):
            has_iot_demo_enabled = False

        diag_card_count = (1 if has_media_quality_enabled else 0) + (1 if has_vibro_enabled else 0) + (1 if has_capex_enabled else 0)
        active_hub_tab = 'esg' if esg_card_count > 0 else ('law' if law_card_count > 0 else ('diag' if diag_card_count > 0 else None))

        context.update({
            'has_esg_audit': has_esg_enabled,
            'has_fplyny': has_fplyny_enabled,
            'has_rekuperacia': has_rekuperacia_enabled,
            'has_vtz': has_vtz_enabled,
            'has_vibro': has_vibro_enabled,
            'has_capex': has_capex_enabled,
            'has_tco': has_tco_enabled,
            'has_media_quality': has_media_quality_enabled,
            'has_iot_demo': has_iot_demo_enabled,
            'esg_card_count': esg_card_count,
            'law_card_count': law_card_count,
            'diag_card_count': diag_card_count,
            'active_hub_tab': active_hub_tab,
            'fplyn_chladiva': FPLYNY_CHLADIVA if has_fplyny_enabled else [],
            'hub_config': active_hub_config,
            'tco_profiles': TCO_PROFILES if has_tco_enabled else {},
            'first_tco': next(iter(TCO_PROFILES.values()), {}) if has_tco_enabled else {},
            'media_quality_apps': MEDIA_QUALITY_APPS if has_media_quality_enabled else [],
            'industry_terms': get_all_industry_terms(),
            'mena': getattr(identita, 'predvolena_mena', None) or get_firemna_mena(),
        })
        return context