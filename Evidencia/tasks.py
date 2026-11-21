import logging
import json
import requests
import celery
import redis
import re
import math
import os
import uuid
import hashlib
import base64
import mimetypes
import urllib.parse
import csv
import traceback
import html
import unicodedata
import zipfile
import codecs
import itertools
import gzip
import tempfile

from email.utils import formataddr, parseaddr
from io import BytesIO
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from django.template.loader import render_to_string
from django.db.models import Max, Prefetch, F, Q
from django.utils import timezone
from django.conf import settings
from django.core.files.base import ContentFile, File
from django.db import close_old_connections, transaction, IntegrityError
from django.core.cache import cache
from django.apps import apps
from django.core.signing import TimestampSigner
from django.urls import reverse
from django.utils.html import escape, strip_tags
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.files.storage import default_storage
from django.core.mail import get_connection, EmailMultiAlternatives
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from celery import shared_task
from google import genai
from google.genai import types
import qrcode
import openpyxl
from openpyxl.styles import Font, Alignment
from openpyxl.utils import get_column_letter
from contextlib import contextmanager

from ISASaplikacia.models import GlobalneNastavenia, FiremnaIdentita, ISASAuditLog
from Evidencia.models import (
    PrichodziaFaktura, Dodavatel, VystavenaFaktura, EvidenciaUhrad, Vozidlo, 
    PlanovanyServisZariadenia, ServisovaneZariadenie, ImportDatTask, Zakaznik, 
    SukromnaOsoba, Zamestnanec, TypServisuPrace, DatabazaVyrobcov, Filialka, 
    Sklad, GPSStopa, MegaZaloha, 
    SignalDictionary, TelemetryHistory, AlarmLog, PrijataObjednavka, 
    ServisnyZaznam, CenovaPonuka, PolozkaCenovejPonuky, PolozkaVystavenejFaktury,
    PovolenieNaVstup, Uloha, KosikSPolozkamiNaDoobjednanie, OdoslanaObjednavka,
    LogNaskladnenia, StockMovement, EvidenciaJazd, Nepritomnost,
    CenovyKatalog, AIAuditLog, GlobalneNastavenieSkladu, ERPExportNastavenia, PracovnaPozicia, Kandidat,
    MERNE_JEDNOTKY_CHOICES
)
from Evidencia.utils import (
    ISASNotifier, generate_pdf_from_html, 
    format_material_list_for_notification, ziskaj_aktualny_stav_skladu,
    process_imap_mailbox, ISASExcelBuilder, ISASPDFBuilder, ISASImportStrategy,
    ISASItemMatcher, ISASProductMetadataResolver, ISASNotificationBuilder
)
from Evidencia.utils_helper import (
    LedgerKalkulacka, ocisti_nazov_od_jednotky,
    clean_and_parse_ai_json, get_safe_employee_name, safe_tg,
    get_firemna_mena
)
from .utils_compliance import (
    get_global_compliance_queue,
    DEFAULT_LEGAL_DISCLAIMER,
    get_compliance_config,
    vyhodnot_zariadenie_iot_telemetriu,
)
from Evidencia.export_engine import ISASExportEngine
from Evidencia.utils_peppol import PeppolXMLGenerator
from .utils_peppol_inbox import PeppolXMLParser, PeppolInboxEngine
from ISASaplikacia.utils import get_gemini_api_key, get_google_maps_api_key, vypocitaj_ai_cenu_usd
from ISASaplikacia.utils_helper import ISASAutoHealTask, isas_task_execution_guard
from .erp_factory import ERPFactory
from .services import ask_isas_ai

from .industry_loader import (
    CATEGORY_MAP,
    CATEGORY_CHOICES,
    get_zariadenie_parametre,
    get_default_stock_model_name,
    COMPLIANCE_ALARM_PREFIXES,
    SAMPLE_IMPORT_DATA,
    get_industry_term,
    get_industry_operativa_prompt,
    get_all_industry_terms
)

logger = logging.getLogger(__name__)

@shared_task(base=ISASAutoHealTask)
def fetch_reklamacie_emails_task():
    from .utils import process_imap_mailbox
    
    def processor(msg, nastavenia):
        api_key = get_gemini_api_key()
        gemini_model = getattr(settings, 'GEMINI_MODEL', None)
        if not api_key or not gemini_model:
            logger.warning("⚠️ [fetch_reklamacie_emails_task] Chýba GEMINI_API_KEY alebo GEMINI_MODEL v konfigurácii. AI analýza reklamácie preskočená.")
            return

        client = genai.Client(api_key=api_key)
        Zakaznik = apps.get_model('Evidencia', 'Zakaznik')
        
        predmet = msg.subject if msg.subject else "Bez predmetu"
        text_emailu = msg.text or msg.html
        
        logger.warning(f"🚨 Analyzujem e-mail s REKLAMÁCIOU: {predmet}")
        
        raw_from = str(getattr(msg, 'from_', '') or '')
        email_match = re.search(r'<([^>]+)>', raw_from)
        cisty_email = email_match.group(1).strip().lower() if email_match else raw_from.strip().lower()

        prompt = f"""
        Si elitný technický dispečer ISAS CORE. Čítaš e-mail zo schránky určenej pre REKLAMÁCIE a SŤAŽNOSTI.
        Tvojou úlohou je zistiť, kto píše a čo presne reklamuje.
        
        E-mail odosielateľa: {cisty_email}
        Predmet: {predmet}
        Text e-mailu: {text_emailu}
        
        Vráť VÝHRADNE validný JSON:
        {{
            "nazov_firmy": "Názov firmy ak je uvedený, inak null",
            "ico": "IČO ak je uvedené, inak null",
            "zhrnutie_problemu": "Zákazník hlási, že po servisnom zásahu zariadenie vykazuje chybový stav.",
            "stupen_nastvanosti": "VYSOKY" // NIZKY, STREDNY, VYSOKY (podľa tónu e-mailu)
        }}
        """
        
        response = client.models.generate_content(
            model=gemini_model,
            contents=[prompt],
            config=types.GenerateContentConfig(thinking_config=types.ThinkingConfig(thinking_level=getattr(settings, 'GEMINI_THINKING_LEVEL', 'MEDIUM') or 'MEDIUM'), response_mime_type="application/json")
        )
        
        ai_data_raw = clean_and_parse_ai_json(response.text)
        ai_data = ai_data_raw[0] if (isinstance(ai_data_raw, list) and ai_data_raw and isinstance(ai_data_raw[0], dict)) else (ai_data_raw if isinstance(ai_data_raw, dict) else {})
        
        nazov_firmy_raw = str(ai_data.get('nazov_firmy') or '').strip()
        zakazane_nazvy = {'s.r.o.', 's. r. o.', 'spol. s r.o.', 'a.s.', 'a. s.', 'sro', 'as', 'ltd', 'gmbh', 'n/a', 'none', 'null', 'firma', 'spolocnost'}
        nazov_firmy = nazov_firmy_raw if (len(nazov_firmy_raw) >= 3 and nazov_firmy_raw.lower() not in zakazane_nazvy) else None
        
        ico_raw = str(ai_data.get('ico') or '').strip()
        ico = ico_raw[:20] if (ico_raw and ico_raw.lower() not in ['none', 'null', 'n/a']) else None
        
        zakaznik = None
        if ico:
            zakaznik = Zakaznik.objects.filter(ico=ico).first()
        if not zakaznik and nazov_firmy:
            zakaznik = Zakaznik.objects.filter(Nazov_spolocnosti__icontains=nazov_firmy).first()
        if not zakaznik:
            zakaznik = Zakaznik.objects.filter(email__iexact=cisty_email).first()
            
        nazov_zakaznika = zakaznik.Nazov_spolocnosti if zakaznik else f"Neznámy odosielateľ ({cisty_email})"
        zhrnutie = ai_data.get('zhrnutie_problemu', 'Nepodarilo sa zhrnúť problém.')
        nastvanost = ai_data.get('stupen_nastvanosti', 'STREDNY')
        
        ikona_nastvanosti = "🤬" if nastvanost == "VYSOKY" else ("😠" if nastvanost == "STREDNY" else "😐")

        # 🛡️ 100% NAIVE CASOVÝ REŽIM (settings.USE_TZ = False)
        teraz = timezone.now()
        if not getattr(settings, 'USE_TZ', False) and timezone.is_aware(teraz):
            teraz = timezone.localtime(teraz).replace(tzinfo=None)

        UlohaModel = apps.get_model('Evidencia', 'Uloha')
        PrijataObjednavkaModel = apps.get_model('Evidencia', 'PrijataObjednavka')

        # 🛡️ 100% DEFENZÍVNY VSTUPNÝ LIEVIK: Robustná validácia a garancia NOT NULL integrity
        platny_email = cisty_email if (cisty_email and re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", cisty_email)) else None
        ciste_ico = re.sub(r'\s+', '', ico).strip()[:20] if ico else None

        cisty_predmet = str(predmet or '').strip()[:50] or "Bez predmetu"
        predvoleny_nazov = nazov_firmy or (f"Odosielateľ ({platny_email})" if platny_email else f"Reklamácia e-mailom ({cisty_predmet})")

        with transaction.atomic():
            # 1. Postupná identifikácia zákazníka: IČO -> E-mail -> Názov
            if not zakaznik and ciste_ico:
                zakaznik = Zakaznik.objects.filter(ico=ciste_ico).first()
            if not zakaznik and platny_email:
                zakaznik = Zakaznik.objects.filter(email__iexact=platny_email).first()
            if not zakaznik and nazov_firmy:
                zakaznik = Zakaznik.objects.filter(Nazov_spolocnosti__iexact=nazov_firmy).first()

            # 2. Bezpečné založenie s deterministickým riešením kolízií (Savepoint)
            if not zakaznik:
                try:
                    with transaction.atomic():
                        zakaznik = Zakaznik.objects.create(
                            Nazov_spolocnosti=predvoleny_nazov[:100],
                            email=platny_email,
                            ico=ciste_ico
                        )
                except IntegrityError:
                    # Kolízia mohla nastať IBA na poli ico (jediné unikátne pole)
                    if ciste_ico:
                        zakaznik = Zakaznik.objects.filter(ico=ciste_ico).first()
                    # Ak IČO zlyhalo, založíme zákazníka bez IČO (nemôže nastať ďalší IntegrityError)
                    if not zakaznik:
                        zakaznik = Zakaznik.objects.create(
                            Nazov_spolocnosti=predvoleny_nazov[:100],
                            email=platny_email,
                            ico=None
                        )

            # 3. Absolútny fail-safe: zakaznik je GARANTOVANE inštancia modelu (nikdy None)
            objednavka = PrijataObjednavkaModel.objects.create(
                zakaznik=zakaznik,
                stav='NOVA',
                priorita='VYSOKA',
                forma_prijatia='EMAIL',
                text_emailu=f"Predmet: {predmet}\n\n{text_emailu}",
                specificky_email_pre_fakturu=platny_email,
                poznamka_klienta=f"REKLAMÁCIA Z E-MAILU: {predmet}\n\n{zhrnutie}",
                interna_poznamka=f"🚨 Automaticky prijaté AI IMAP robotom.\nTón e-mailu: {nastvanost}\nAI zhrnutie: {zhrnutie}"
            )

            uloha = UlohaModel.objects.create(
                zakaznik=zakaznik,
                zaciatok=teraz,
                dokoncene=False,
                je_reklamacia=True,
                datum_prijatia_reklamacie=teraz,
                blizsie_informacie=f"🚨 REKLAMÁCIA Z E-MAILU (#{objednavka.cislo_objednavky_interne})\nPredmet: {predmet}\nAI Zhrnutie: {zhrnutie}\nTón: {nastvanost}\n\nPôvodný e-mail:\n{text_emailu[:1500] if text_emailu else ''}",
                farba='#ef4444'
            )
            objednavka.vytvorene_ulohy.add(uloha)

        base_url = (getattr(nastavenia, 'clean_base_url', None) or getattr(settings, 'SITE_URL', '')).strip().rstrip('/')
        admin_url = f"{base_url}/admin/Evidencia/uloha/{uloha.id}/change/"
        
        payload = (ISASNotificationBuilder(nadpis='NOVÁ REKLAMÁCIA / SŤAŽNOSŤ', ikona='🚨', farba_temy='#ef4444')
                   .pridaj_riadok('🏢 Zákazník:', nazov_zakaznika)
                   .pridaj_riadok(f'{ikona_nastvanosti} Tón e-mailu:', nastvanost)
                   .pridaj_riadok('📋 Číslo konania:', objednavka.cislo_objednavky_interne)
                   .nastav_hlavny_text(zhrnutie)
                   .nastav_tlacidlo('Otvoriť reklamačnú úlohu', admin_url)
                   .nastav_ai_poznamku('Úloha bola automaticky zaradená do 30-dňového SLA monitora.')
                   .build())
        
        ISASNotifier.send(
            payload=payload,
            send_tg=getattr(nastavenia, 'reklamacie_notif_tg', True),
            send_mail=getattr(nastavenia, 'reklamacie_notif_mail', True),
            send_discord=getattr(nastavenia, 'reklamacie_notif_discord', True)
        )

    config = {
        'server': 'imap_server_reklamacie',
        'user': 'email_pre_reklamacie',
        'password': 'heslo_pre_reklamacie',
        'name': 'REKLAMÁCIE'
    }
    process_imap_mailbox('lock_imap_reklamacie', config, processor)

@shared_task(base=ISASAutoHealTask)
def fetch_orders_emails_task():
    from .utils import process_imap_mailbox
    
    def processor(msg, nastavenia):
        api_key = get_gemini_api_key()
        gemini_model = getattr(settings, 'GEMINI_MODEL', None)
        if not api_key or not gemini_model:
            logger.warning("⚠️ [fetch_orders_emails_task] Chýba GEMINI_API_KEY alebo GEMINI_MODEL v konfigurácii. AI analýza objednávky preskočená.")
            return

        client = genai.Client(api_key=api_key)
        Zakaznik = apps.get_model('Evidencia', 'Zakaznik')
        PrijataObjednavka = apps.get_model('Evidencia', 'PrijataObjednavka')
        ServisovaneZariadenie = apps.get_model('Evidencia', 'ServisovaneZariadenie')
        TypServisuPrace = apps.get_model('Evidencia', 'TypServisuPrace')

        predmet = msg.subject if msg.subject else "Bez predmetu"
        text_emailu = msg.text or msg.html
        
        logger.info(f"🔎 Analyzujem e-mail s objednávkou: {predmet}")
        
        raw_from = str(getattr(msg, 'from_', '') or '')
        email_match = re.search(r'<([^>]+)>', raw_from)
        cisty_email = email_match.group(1).strip().lower() if email_match else raw_from.strip().lower()

        pdf_data = None
        pdf_filename = None
        for att in msg.attachments:
            if getattr(att, 'filename', None) and att.filename.lower().endswith('.pdf'):
                pdf_data = att.payload
                pdf_filename = att.filename
                break

        prompt = f"""
        Si elitný technický dispečer a ERP architekt ISAS CORE. Tvojou úlohou je s absolútnou presnosťou extrahovať štruktúrované dáta z prijatej objednávky (e-mail/PDF).
        Tieto dáta sa neskôr preklopia priamo do ÚLOHY pre servisného technika na výjazde.
        
        ⚠️ KRITICKÉ PRAVIDLO: ZAKAZUJEM TI HALUCINOVAŤ. Ak údaj v texte neexistuje, vráť null. Nevymýšľaj si dáta, dátumy ani sumy.
        
        E-mail odosielateľa: {cisty_email}
        Predmet: {predmet}
        Text e-mailu: {text_emailu}
        
        PRAVIDLÁ EXTRAKCIE:
        1. Zákazník: Extrahuj presný názov firmy a IČO (ak sú uvedené).
        2. Zariadenia: Ak zákazník spomína konkrétne zariadenie, technológiu či hardvér, extrahuj model a sériové číslo (SN) do polí.
        3. Typy prác: Ak text naznačuje konkrétny úkon (napr. "oprava", "údržba", "diagnostika"), extrahuj tieto kľúčové slová do poľa.
        4. PO Number: Nájdi Číslo objednávky klienta (napr. "Objednávka č. 123", "PO: 987", "Zakázka 456").
        5. Termín dodania: Ak zákazník požaduje termín (napr. "do 15.4.", "ASAP"), preveď to na formát YYYY-MM-DD. Ak nie je uvedený, vráť null.
        6. Priorita: Ak text obsahuje slová ako "havária", "urgent", "výpadok", "stojí prevádzka", vráť "VYSOKA". Ak "bežná údržba", vráť "NIZKA". Inak "STREDNA".
        7. Fakturačné údaje: Nájdi meno nákupcu (vystavil_za_klienta), špecifický e-mail pre faktúru (ak žiadajú poslať inam) a vyžadovanú splatnosť v dňoch (ak je uvedená).
        8. 🚨 POZNÁMKA KLIENTA (PRE TECHNIKA): Toto je najdôležitejšie pole! Vytvor detailné zadanie. Zhrň problém, ale BEZPODMIENEČNE ZACHOVAJ VŠETKY technické detaily (chybové kódy, presné prejavy poruchy), kontaktné osoby (mená, telefóny) a špecifické inštrukcie k miestu (napr. "hala B", "volajte p. Nováka").
        9. 🌐 B2B PORTÁL (SAP Ariba, Coupa, SupplyOn): Ak ide o notifikáciu z nákupného portálu alebo e-mail obsahuje odkaz na schválenie/otvorenie objednávky v externom portáli, extrahuj celú URL adresu do poľa "odkaz_na_portal".
        
        Vráť VÝHRADNE validný JSON. Najprv vyplň "_myslienkove_pochody" (krátka analýza), až potom samotné dáta.
        
        {{
            "_myslienkove_pochody": "Zákazník identifikovaný podľa IČO. Spomína model MODEL-100. Žiada urgentnú opravu. Termín neuvedený. V texte je kontakt na zodpovednú osobu.",
            "nazov_firmy": "Firma s.r.o.",
            "ico": "12345678",
            "zariadenia_modely": ["MODEL-100"],
            "zariadenia_sn": ["SN12345"],
            "typy_prac_klucove_slova": ["oprava", "diagnostika"],
            "cislo_objednavky_klienta": "PO-123456",
            "odkaz_na_portal": "https://service.ariba.com/service/order/123456",
            "celkova_suma_bez_dph": 0.00,
            "termin_dodania": "YYYY-MM-DD",
            "priorita": "VYSOKA",
            "vystavil_za_klienta": "Jozef Mrkvička",
            "specificky_email_pre_fakturu": "invoices@firma.sk",
            "vyzadovana_splatnost_dni": 30,
            "poznamka_klienta": "Hlásená porucha na zariadení MODEL-100. Na displeji svieti chyba E-404. Po príchode na vrátnicu volať p. Nováka (0900 123 456), ktorý vás odvedie do Haly B."
        }}
        """
        
        contents = [prompt]
        if pdf_data:
            contents.append(types.Part.from_bytes(data=pdf_data, mime_type="application/pdf"))
            
        response = client.models.generate_content(
            model=gemini_model,
            contents=contents,
            config=types.GenerateContentConfig(thinking_config=types.ThinkingConfig(thinking_level=getattr(settings, 'GEMINI_THINKING_LEVEL', 'MEDIUM') or 'MEDIUM'), response_mime_type="application/json")
        )
        
        ai_data_raw = clean_and_parse_ai_json(response.text)
        ai_data = ai_data_raw[0] if (isinstance(ai_data_raw, list) and ai_data_raw and isinstance(ai_data_raw[0], dict)) else (ai_data_raw if isinstance(ai_data_raw, dict) else {})
        
        nazov_firmy_raw = str(ai_data.get('nazov_firmy') or '').strip()
        zakazane_nazvy = {'s.r.o.', 's. r. o.', 'spol. s r.o.', 'a.s.', 'a. s.', 'sro', 'as', 'ltd', 'gmbh', 'n/a', 'none', 'null', 'firma', 'spolocnost'}
        nazov_firmy = nazov_firmy_raw if (len(nazov_firmy_raw) >= 3 and nazov_firmy_raw.lower() not in zakazane_nazvy) else None
        ico_raw = str(ai_data.get('ico') or '').strip()
        ico = ico_raw[:20] if (ico_raw and ico_raw.lower() not in ['none', 'null', 'n/a']) else None
        
        zakaznik = None
        if ico:
            zakaznik = Zakaznik.objects.filter(ico=ico).first()
        if not zakaznik and nazov_firmy:
            zakaznik = Zakaznik.objects.filter(Nazov_spolocnosti__icontains=nazov_firmy).first()
        if not zakaznik:
            zakaznik = Zakaznik.objects.filter(email__iexact=cisty_email).first()
            
        if not zakaznik:
            logger.warning(f"⚠️ Objednávka z {cisty_email} preskočená - Zákazník nebol nájdený v databáze.")
            return

        termin_dodania = None
        if ai_data.get('termin_dodania'):
            try:
                termin_dodania = datetime.strptime(ai_data['termin_dodania'], '%Y-%m-%d').date()
            except ValueError:
                pass

        # 🔗 ORM Párovanie Zariadení
        zariadenia_qs = ServisovaneZariadenie.objects.none()
        raw_sn = ai_data.get('zariadenia_sn', [])
        sn_list = [raw_sn] if isinstance(raw_sn, str) else (raw_sn if isinstance(raw_sn, list) else [])
        raw_modely = ai_data.get('zariadenia_modely', [])
        modely_list = [raw_modely] if isinstance(raw_modely, str) else (raw_modely if isinstance(raw_modely, list) else [])
        if sn_list or modely_list:
            q_zariadenia = Q()
            for sn in sn_list:
                sn_clean = str(sn).strip() if sn else ""
                if sn_clean: q_zariadenia |= Q(vyrobne_cislo__icontains=sn_clean)
            for model in modely_list:
                model_clean = str(model).strip() if model else ""
                if model_clean: q_zariadenia |= Q(model_zariadenia__icontains=model_clean)
            if q_zariadenia:
                zariadenia_qs = ServisovaneZariadenie.objects.filter(Q(zakaznik=zakaznik) & q_zariadenia)

        # 🔗 ORM Párovanie Typov prác
        typy_qs = TypServisuPrace.objects.none()
        raw_prac = ai_data.get('typy_prac_klucove_slova', [])
        prac_slova = [raw_prac] if isinstance(raw_prac, str) else (raw_prac if isinstance(raw_prac, list) else [])
        if prac_slova:
            q_prace = Q()
            for slovo in prac_slova:
                slovo_clean = str(slovo).strip() if slovo else ""
                if slovo_clean: q_prace |= Q(nazov__icontains=slovo_clean)
            if q_prace:
                typy_qs = TypServisuPrace.objects.filter(q_prace)

        # 🧠 ISAS INTELLIGENCE: Detekcia zdroja (Portál vs PDF Súbor vs Čistý Mail)
        odkaz_portal = ai_data.get('odkaz_na_portal')
        if odkaz_portal:
            forma_zdroj = 'PORTAL'
        elif pdf_data:
            forma_zdroj = 'SUBOR'
        else:
            forma_zdroj = 'EMAIL'

        raw_suma = str(ai_data.get('celkova_suma_bez_dph') or '0.00').replace(' ', '').replace(',', '.')
        try:
            suma_obj = Decimal(raw_suma).quantize(Decimal('0.01'))
        except Exception:
            suma_obj = Decimal('0.00')

        splatnost_ai = ai_data.get('vyzadovana_splatnost_dni')
        try:
            splatnost_dni_val = int(splatnost_ai) if splatnost_ai is not None else None
        except (ValueError, TypeError):
            splatnost_dni_val = None

        with transaction.atomic():
            objednavka = PrijataObjednavka.objects.create(
                zakaznik=zakaznik,
                cislo_objednavky_klienta=ai_data.get('cislo_objednavky_klienta', '')[:100] if ai_data.get('cislo_objednavky_klienta') else '',
                celkova_suma_bez_dph=suma_obj,
                poznamka_klienta=ai_data.get('poznamka_klienta', ''),
                text_emailu=text_emailu,
                odkaz_na_portal=odkaz_portal[:500] if odkaz_portal else None,
                forma_prijatia=forma_zdroj,
                stav='NOVA',
                priorita=ai_data.get('priorita', 'STREDNA'),
                termin_dodania=termin_dodania,
                vystavil_za_klienta=ai_data.get('vystavil_za_klienta', '')[:150] if ai_data.get('vystavil_za_klienta') else '',
                specificky_email_pre_fakturu=ai_data.get('specificky_email_pre_fakturu'),
                vyzadovana_splatnost_dni=splatnost_dni_val
            )
            
            if zariadenia_qs.exists():
                objednavka.zariadenia.set(zariadenia_qs)
                
            if typy_qs.exists():
                objednavka.typ_servisu.set(typy_qs)
            
            if pdf_data and pdf_filename:
                safe_pdf_name = os.path.basename(pdf_filename.replace('\\', '/'))
                objednavka.subor_objednavky.save(safe_pdf_name, ContentFile(pdf_data))
                
            logger.info(f"✨ BINGO! Nová objednávka {objednavka.cislo_objednavky_interne} od {zakaznik.Nazov_spolocnosti} úspešne vytvorená.")
            
            base_url = (getattr(nastavenia, 'clean_base_url', None) or getattr(settings, 'SITE_URL', '')).strip().rstrip('/')
            admin_url = f"{base_url}/admin/Evidencia/prijataobjednavka/{objednavka.id}/change/"
            
            poznamka_safe = html.escape(str(objednavka.poznamka_klienta or '').strip())
            hlavny_text_notif = f"{poznamka_safe[:250]}..." if poznamka_safe else "Bez sprievodného textu."
            payload = (ISASNotificationBuilder(nadpis='NOVÁ OBJEDNÁVKA Z E-MAILU', ikona='📥', farba_temy='#3b82f6')
                       .pridaj_riadok('🏢 Zákazník:', zakaznik.Nazov_spolocnosti)
                       .pridaj_riadok('🏷️ PO Number:', objednavka.cislo_objednavky_klienta or 'Neuvedené')
                       .nastav_hlavny_text(hlavny_text_notif)
                       .nastav_tlacidlo('Otvoriť objednávku v systéme ISAS CORE', admin_url)
                       .nastav_ai_poznamku('Objednávka bola automaticky spracovaná a čaká na spárovanie/konverziu.')
                       .build())
            
            ISASNotifier.send(
                payload=payload,
                send_tg=getattr(nastavenia, 'objednavky_notif_tg', True),
                send_mail=getattr(nastavenia, 'objednavky_notif_mail', True),
                send_discord=getattr(nastavenia, 'objednavky_notif_discord', True)
            )

    config = {
        'server': 'imap_server_objednavky',
        'user': 'email_pre_objednavky',
        'password': 'heslo_pre_objednavky',
        'name': 'OBJEDNÁVKY'
    }
    process_imap_mailbox('lock_imap_objednavky', config, processor)

@shared_task(base=ISASAutoHealTask)
def fetch_emails_task():
    from .utils import process_imap_mailbox
    
    def processor(msg, nastavenia):
        predmet = msg.subject if msg.subject else "E-mail bez predmetu"
        if not msg.attachments:
            logger.info(f"⏭️ Preskakujem mail '{predmet}' - neobsahuje žiadnu prílohu.")
            return
            
        logger.info(f"🔎 Detekovaný e-mail s prílohou: {predmet}")
        for att in msg.attachments:
            if not getattr(att, 'filename', None):
                continue
            filename = att.filename.lower()
            # Ignorujeme obrázky menšie ako 25 KB (podpisové ikony, sociálne siete)
            payload_len = len(att.payload) if getattr(att, 'payload', None) else 0
            is_image = filename.endswith(('.jpg', '.jpeg', '.png'))
            if filename.endswith('.pdf') or (is_image and payload_len >= 25600):
                try:
                    with transaction.atomic():
                        raw_from = str(getattr(msg, 'from_', '') or '')
                        email_match = re.search(r'<([^>]+)>', raw_from)
                        cisty_email = email_match.group(1).strip().lower() if email_match else raw_from.strip().lower()
                        
                        dodavatel = Dodavatel.objects.filter(email__iexact=cisty_email).first()
                        
                        faktura = PrichodziaFaktura.objects.create(
                            zdroj='EMAIL', dodavatel=dodavatel,
                            poznamka=f"Automatický zber ISAS Poštár | E-mail: {cisty_email} | Predmet: {predmet}"
                        )
                        cisty_nazov_prilohy = os.path.basename(str(att.filename).replace('\\', '/'))
                        bezpecny_nazov_prilohy = re.sub(r'[\x00\r\n]', '', cisty_nazov_prilohy).strip() or f"faktura_{faktura.id}.pdf"
                        faktura.subor.save(bezpecny_nazov_prilohy, ContentFile(att.payload))
                    logger.info(f'✨ Súbor "{att.filename}" bol prijatý a odovzdaný AI.')
                    
                    base_url = (getattr(nastavenia, 'clean_base_url', None) or getattr(settings, 'SITE_URL', '')).strip().rstrip('/')
                    admin_url = f"{base_url}/admin/Evidencia/prichodziafaktura/{faktura.id}/change/"
                    dodavatel_nazov = dodavatel.Nazov_spolocnosti if dodavatel else f"Neznámy ({cisty_email})"

                    payload = (ISASNotificationBuilder(nadpis='NOVÁ FAKTÚRA Z E-MAILU', ikona='📥', farba_temy='#3b82f6')
                               .pridaj_riadok('🏢 Odosielateľ:', dodavatel_nazov)
                               .pridaj_riadok('📎 Súbor:', att.filename)
                               .nastav_tlacidlo('Otvoriť faktúru v systéme ISAS CORE', admin_url)
                               .nastav_ai_poznamku('Faktúra bola prijatá a odovzdaná na vyťaženie dát.')
                               .build())
                    
                    ISASNotifier.send(
                        payload=payload,
                        send_tg=getattr(nastavenia, 'faktury_notif_tg', True),
                        send_mail=getattr(nastavenia, 'faktury_notif_mail', True),
                        send_discord=getattr(nastavenia, 'faktury_notif_discord', True)
                    )

                except Exception as db_err:
                    logger.error(f'❌ Databázová chyba pri ukladaní súboru {att.filename}: {str(db_err)}')
            else:
                logger.warning(f"⚠️ Ignorujem nepodporovaný typ súboru: {att.filename}")

    config = {
        'server': 'imap_server_faktury',
        'user': 'email_pre_faktury',
        'password': 'heslo_pre_faktury',
        'name': 'FAKTÚRY'
    }
    process_imap_mailbox('lock_imap_faktury', config, processor)

@shared_task(base=ISASAutoHealTask)
def isas_bankar_task():
    from .utils import process_imap_mailbox
    
    def processor(msg, nastavenia):
        api_key = get_gemini_api_key()
        gemini_model = getattr(settings, 'GEMINI_MODEL', None)
        if not api_key or not gemini_model:
            logger.warning("⚠️ [isas_bankar_task] Chýba GEMINI_API_KEY alebo GEMINI_MODEL v konfigurácii. Spracovanie bankového e-mailu preskočené.")
            return

        client = genai.Client(api_key=api_key)
        predmet = msg.subject if msg.subject else "Bez predmetu"
        text_emailu = msg.text or msg.html
        logger.info(f"🔎 Analyzujem bankový e-mail: {predmet}")
        
        FiremnaIdentita = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
        identita = FiremnaIdentita.objects.first()
        nazov_firmy = identita.nazov_spolocnosti if identita else "Naša spoločnosť"
        mena = identita.predvolena_mena if (identita and identita.predvolena_mena) else get_firemna_mena()

        system_instruction = f"""
        Si elitný finančný riaditeľ spoločnosti {nazov_firmy}. Čítaš notifikačný e-mail z banky.
        Zisti, o aký typ pohybu na účte ide.
        Pravidlá:
        1. Ak peniaze ODIŠLI z nášho účtu (platíme my dodávateľovi), typ_pohybu je "ODOSLANA".
        2. Ak peniaze PRIŠLI na náš účet (platí nám zákazník), typ_pohybu je "PRIJATA".
        3. Ak to nie je ani jedno (napr. reklama z banky), typ_pohybu je "NEZNAMY".
        Vytiahni presnú Sumu (v mene {mena}) a Variabilný symbol (VS). VS extrahuj PRESNE tak, ako je uvedený v e-maile (vrátane prípadných núl na začiatku, nič neodstraňuj).
        Vráť striktne iba JSON: {{"typ_pohybu": "PRIJATA/ODOSLANA/NEZNAMY", "suma": 0.00, "variabilny_symbol": "123456"}}
        """
        
        response = client.models.generate_content(
            model=gemini_model,
            contents=[system_instruction, f"PREDMET: {predmet}\n\nTEXT:\n{text_emailu}"],
            config=types.GenerateContentConfig(response_mime_type="application/json", thinking_config=types.ThinkingConfig(thinking_level=getattr(settings, 'GEMINI_THINKING_LEVEL', 'MEDIUM') or 'MEDIUM'))

        )
        
        ai_data_raw = clean_and_parse_ai_json(response.text)
        ai_data = ai_data_raw[0] if (isinstance(ai_data_raw, list) and ai_data_raw and isinstance(ai_data_raw[0], dict)) else (ai_data_raw if isinstance(ai_data_raw, dict) else {})

        typ_pohybu = ai_data.get('typ_pohybu', 'NEZNAMY')
        
        if typ_pohybu == 'NEZNAMY':
            logger.info("⏭️ Z e-mailu sa nedá určiť pohyb. Preskakujem.")
            return
            
        vs_raw = ai_data.get('variabilny_symbol')
        vs = str(vs_raw).strip() if vs_raw is not None else ""
        
        raw_suma = str(ai_data.get('suma') or '0.00').replace(' ', '').replace(',', '.')
        try:
            suma = Decimal(raw_suma).quantize(Decimal('0.01'))
        except Exception:
            suma = Decimal('0.00')
        
        if not vs or suma <= 0:
            logger.warning("⚠️ AI nenašlo VS alebo sumu. Preskakujem.")
            return

        with transaction.atomic():
            if typ_pohybu == 'PRIJATA':
                logger.info(f"📥 Hľadám Vystavenú faktúru pre VS: {vs}")
                faktury = VystavenaFaktura.objects.select_for_update().select_related('zakaznik').filter(variabilny_symbol=vs).exclude(stav__in=['ZAPLATENA', 'STORNO'])
                if faktury.exists():
                    faktura = faktury.first()
                    if faktura.celkom_s_dph and abs(faktura.celkom_s_dph - suma) <= Decimal('1.00'):
                        EvidenciaUhrad.objects.create(typ_uhrady='BV', suma_uhrady=suma, vystavena_faktura=faktura, poznamka="Zachytené automaticky cez ISAS BANKÁR")
                        faktura.ai_poznamka = f"{faktura.ai_poznamka or ''}\n\n✅ [ISAS BANKÁR]: Platba {suma} {mena} prijatá na účet dňa {timezone.now().strftime('%d.%m.%Y')}."
                        
                        if faktura.typ_dokladu == 'ZALOHA':
                            faktura.ai_poznamka += "\n⚠️ [LEGISLATÍVA]: Záloha bola uhradená. Nezabudnite do 15 dní vystaviť Ostrú faktúru (Daňový doklad) a odoslať ju do siete eFaktúra / Peppol!"
                            
                        faktura.stav = 'ZAPLATENA'
                        faktura.save(update_fields=['stav', 'ai_poznamka'])
                        logger.info(f"✨ BINGO! Vystavená faktúra {faktura.cislo_faktury} je ZAPLATENÁ!")
                        odosli_notifikaciu_majitelovi(typ_pohybu, faktura, suma, nastavenia)
                    else:
                        logger.warning(f"⚠️ VS sedí, ale suma ({faktura.celkom_s_dph} {mena}) nesúhlasí s bankou ({suma} {mena}).")
                else:
                    logger.info(f"⏭️ Platba prijatá, ale Vystavená faktúra pre VS {vs} nebola nájdená/čaká.")

            elif typ_pohybu == 'ODOSLANA':
                logger.info(f"📤 Hľadám Prijatú faktúru alebo Vystavený Dobropis pre VS: {vs}")
                
                dobropisy = VystavenaFaktura.objects.select_for_update().select_related('zakaznik').filter(variabilny_symbol=vs, typ_dokladu='DOBROPIS').exclude(stav__in=['ZAPLATENA', 'STORNO'])
                
                if dobropisy.exists():
                    faktura = dobropisy.first()
                    if faktura.celkom_s_dph and abs(abs(faktura.celkom_s_dph) - suma) <= Decimal('1.00'):
                        EvidenciaUhrad.objects.create(typ_uhrady='BV', suma_uhrady=suma, vystavena_faktura=faktura, poznamka="Zachytené automaticky cez ISAS BANKÁR (Úhrada Dobropisu)")
                        faktura.ai_poznamka = f"{faktura.ai_poznamka or ''}\n\n✅ [ISAS BANKÁR]: Dobropis vo výške {suma} {mena} vrátený klientovi dňa {timezone.now().strftime('%d.%m.%Y')}."
                        faktura.stav = 'ZAPLATENA'
                        faktura.save(update_fields=['stav', 'ai_poznamka'])
                        logger.info(f"✨ BINGO! Vystavený Dobropis {faktura.cislo_faktury} je VYROVNANÝ!")
                        odosli_notifikaciu_majitelovi('ODOSLANA', faktura, suma, nastavenia)
                        return
                    else:
                        faktura_suma_disp = abs(faktura.celkom_s_dph) if faktura.celkom_s_dph is not None else Decimal('0.00')
                        logger.warning(f"⚠️ VS Dobropisu sedí, ale suma ({faktura_suma_disp} {mena}) nesúhlasí s bankou ({suma} {mena}).")

                faktury = PrichodziaFaktura.objects.select_for_update().select_related('dodavatel').filter(variabilny_symbol=vs, stav_uhrady='NEZAPLATENA')
                if faktury.exists():
                    faktura = faktury.first()
                    if faktura.suma_na_uhradu and abs(faktura.suma_na_uhradu - suma) <= Decimal('1.00'):
                        EvidenciaUhrad.objects.create(typ_uhrady='BV', suma_uhrady=suma, prijata_faktura=faktura, poznamka="Zachytené automaticky cez ISAS BANKÁR")
                        faktura.stav_uhrady = 'ZAPLATENA'
                        faktura.poznamka = f"{faktura.poznamka or ''}\n\n✅ [ISAS BANKÁR]: Úhrada odoslaná z účtu dňa {timezone.now().strftime('%d.%m.%Y')}. Suma: {suma} {mena}."
                        faktura.save(update_fields=['stav_uhrady', 'poznamka'])
                        logger.info(f"✨ BINGO! Prijatá faktúra #{faktura.id} je UHRADENÁ!")
                        odosli_notifikaciu_majitelovi(typ_pohybu, faktura, suma, nastavenia)
                    else:
                        logger.warning(f"⚠️ VS sedí, ale suma ({faktura.suma_na_uhradu} {mena}) nesúhlasí s bankou ({suma} {mena}).")
                else:
                    logger.info(f"⏭️ Platba odišla, ale Prijatá faktúra pre VS {vs} nebola nájdená.")

    config = {
        'server': 'imap_server_banka',
        'user': 'email_pre_banku',
        'password': 'heslo_pre_banku',
        'name': 'BANKA'
    }
    process_imap_mailbox('lock_imap_bankar', config, processor)

def odosli_notifikaciu_majitelovi(typ_pohybu, faktura, suma, nastavenia):
    base_url = (getattr(nastavenia, 'clean_base_url', None) or getattr(settings, 'SITE_URL', '')).strip().rstrip('/')
    mena = getattr(faktura, 'mena', '') or get_firemna_mena()
    if typ_pohybu == 'PRIJATA':
        ikona, smer = "🟢", "PRIJATÁ PLATBA (Tržba)"
        partner_nazov = faktura.zakaznik.Nazov_spolocnosti if faktura.zakaznik else faktura.odberatel_nazov
        cislo_dokladu = faktura.cislo_faktury
        isas_link = f"{base_url}/admin/Evidencia/vystavenafaktura/{faktura.id}/change/"
        vysvetlivka = "Zákazník práve uhradil faktúru. Systém ju automaticky označil ako ZAPLATENÚ."
        
        # Varovanie pri Peppol chybe
        if getattr(faktura, 'peppol_status', '') == 'ERROR':
            vysvetlivka += "\n\n⚠️ POZOR: Peniaze prišli, ale eFaktúra bola štátom ZAMIETNUTÁ! Skontrolujte Peppol log."
    
    elif getattr(faktura, 'typ_dokladu', None) == 'DOBROPIS':
        ikona, smer = "💸", "ODOSLANÁ PLATBA (Úhrada Dobropisu)"
        partner_nazov = (faktura.zakaznik.Nazov_spolocnosti if getattr(faktura, 'zakaznik', None) else getattr(faktura, 'odberatel_nazov', 'Zákazník')) or "Zákazník"
        cislo_dokladu = getattr(faktura, 'cislo_faktury', f"ID #{faktura.id}")
        isas_link = f"{base_url}/admin/Evidencia/vystavenafaktura/{faktura.id}/change/"
        vysvetlivka = "Banka potvrdila odchod platby (vrátenie prostriedkov za vystavený dobropis). Systém ho automaticky vyrovnal."
    else:
        ikona, smer = "💸", "ODOSLANÁ ÚHRADA (Náklad)"
        dod = getattr(faktura, 'dodavatel', None)
        partner_nazov = dod.Nazov_spolocnosti if dod else "Neznámy dodávateľ"
        cislo_dokladu = f"Interné ID #{faktura.id}"
        isas_link = f"{base_url}/admin/Evidencia/prichodziafaktura/{faktura.id}/change/"
        vysvetlivka = "Banka potvrdila odchod platby dodávateľovi. Systém ju automaticky odškrtol z vašich záväzkov."

    payload = (ISASNotificationBuilder(nadpis=smer, ikona=ikona, farba_temy='#3b82f6')
               .pridaj_riadok('🏢 Partner:', partner_nazov)
               .pridaj_riadok('🔢 Variabilný symbol:', faktura.variabilny_symbol)
               .pridaj_riadok('📄 Doklad:', cislo_dokladu)
               .pridaj_riadok('💰 Suma:', f"{suma} {mena}")
               .nastav_tlacidlo('Otvoriť detail v systéme ISAS CORE', isas_link)
               .nastav_ai_poznamku(vysvetlivka)
               .build())
    
    ISASNotifier.send(
        payload=payload,
        send_tg=getattr(nastavenia, 'bankar_notif_tg', True),
        send_mail=getattr(nastavenia, 'bankar_notif_mail', True),
        send_discord=getattr(nastavenia, 'bankar_notif_discord', True)
    )

@shared_task(base=ISASAutoHealTask)
def strazca_fleet_task():
    with isas_task_execution_guard('lock_strazca_fleet', timeout=300) as (acquired, gn, identita):
        if not acquired:
            return

        close_old_connections()
        dnes = timezone.now().date()
        limit_dni = dnes + timedelta(days=14)
        vozidla = Vozidlo.objects.select_related('priradeny_zamestnanec').all()
        pocet_upozorneni = 0

        for v in vozidla:
            alert_spravy = []
            kluce_na_uzamknutie = []
            
            def prever_dokument(datum, nazov, cache_identifikator):
                if datum and datum <= limit_dni:
                    jedinecny_kluc = f"isas_fleet_{cache_identifikator}_{v.id}_{dnes.year}_{dnes.month}"
                    if not cache.get(jedinecny_kluc):
                        dni_do_konca = (datum - dnes).days
                        if dni_do_konca > 0: stav = f"vyprší o {dni_do_konca} dní"
                        elif dni_do_konca == 0: stav = "VYPRŠÍ DNES!"
                        else: stav = f"JE PREPADNUTÁ o {abs(dni_do_konca)} dní!"
                        alert_spravy.append(f"📅 <b>{nazov}:</b> {stav} ({datum.strftime('%d.%m.%Y')})")
                        kluce_na_uzamknutie.append(jedinecny_kluc)

            prever_dokument(v.stk_plati_do, "STK / Emisná kontrola", "stk")
            prever_dokument(v.dialnicna_znamka_do, "Diaľničná známka", "znamka")
            prever_dokument(v.pzp_plati_do, "Zákonné poistenie (PZP)", "pzp")
            prever_dokument(v.kasko_plati_do, "Havarijné poistenie (KASKO)", "kasko")
            prever_dokument(v.lekarnicka_plati_do, "Lekárnička", "lekarnicka")

            if alert_spravy:
                pocet_upozorneni += 1
                
                # Načítanie dynamických nastavení z databázy
                GlobalneNastavenia = apps.get_model('ISASaplikacia', 'GlobalneNastavenia')
                nastavenia = GlobalneNastavenia.objects.first()
                
                base_url = (getattr(nastavenia, 'clean_base_url', None) or getattr(settings, 'SITE_URL', '')).strip().rstrip('/')
                admin_url = f"{base_url}/admin/Evidencia/vozidlo/{v.id}/change/"
                
                if v.priradeny_zamestnanec:
                    vodic = f"{v.priradeny_zamestnanec.meno} {v.priradeny_zamestnanec.priezvisko}"
                elif v.sukromna_osoba:
                    vodic = f"{v.sukromna_osoba.meno_a_priezvisko} (Súkromná osoba)"
                else:
                    vodic = "Nepriradený"
                zoznam_dokladov = "\n".join([f"• {msg}" for msg in alert_spravy])

                payload = (ISASNotificationBuilder(nadpis='EXPIRÁCIA DOKLADOV', ikona='🚨', farba_temy='#ef4444')
                           .pridaj_riadok('🚗 Vozidlo:', f"{v.spz} ({v.znacka_a_model})")
                           .pridaj_riadok('👤 Vodič:', vodic)
                           .nastav_hlavny_text(f"⚠️ <b>Končiace doklady:</b><br>{'<br>'.join([f'• {msg}' for msg in alert_spravy])}")
                           .nastav_tlacidlo('Otvoriť detail v systéme ISAS CORE', admin_url)
                           .nastav_ai_poznamku('ISAS Fleet: Zabezpečte obnovu dokladov, inak hrozia pokuty.')
                           .build())
                
                ISASNotifier.send(
                    payload=payload,
                    send_tg=True,
                    send_mail=True,
                    send_discord=True
                )
                
                for kluc in kluce_na_uzamknutie:
                    cache.set(kluc, True, timeout=2592000)
                    
        if pocet_upozorneni > 0:
            logger.info(f"🚗 Fleet Check ukončený. Odoslané alerty: {pocet_upozorneni}")
        else:
            logger.info("✓ Všetky doklady flotily sú v poriadku.")

def odoslat_upozornenie_servis(plan, zariadenie, aktualne_mh, aktualne_mh_zataz, limit_mh, send_tg=True, send_mail=True, send_discord=True):
    posledny_servis = "Neznáme"
    posledny_mh = "Neznáme"
    if plan.posledny_zaznam:
        posledny_servis = plan.posledny_zaznam.vytvorene.strftime('%d.%m.%Y') if getattr(plan.posledny_zaznam, 'vytvorene', None) else "Neznáme"
        mh_celkove = plan.posledny_zaznam.meter_display if plan.posledny_zaznam.stav_motohodin is not None else "Nezadané"
        mh_zataz = f" ({plan.posledny_zaznam.meter_load_label}: {plan.posledny_zaznam.meter_load_display})" if getattr(plan.posledny_zaznam, 'motohodiny_v_zatazi', None) is not None else ""
        posledny_mh = f"{mh_celkove}{mh_zataz}"

    dovod = ""
    m_unit = getattr(zariadenie, 'meter_unit', 'hod.')
    m_load_unit = getattr(zariadenie, 'meter_load_unit', 'hod.')
    m_label = getattr(zariadenie, 'meter_label', 'Počítadlo')
    m_load_label = getattr(zariadenie, 'meter_load_label', 'Počítadlo v záťaži')
    planovane_mh_zataz = getattr(plan, 'planovane_motohodiny_zataz', None)

    if planovane_mh_zataz and aktualne_mh_zataz >= (planovane_mh_zataz - limit_mh):
        dovod = f"Dosiahnutý limit ({m_load_label}) (Aktuálny stav: {aktualne_mh_zataz} {m_load_unit} / Plánované pri: {planovane_mh_zataz} {m_load_unit})"
    elif plan.planovane_motohodiny and aktualne_mh >= (plan.planovane_motohodiny - limit_mh):
        dovod = f"Dosiahnutý limit ({m_label}) (Aktuálny stav: {aktualne_mh} {m_unit} / Plánované pri: {plan.planovane_motohodiny} {m_unit})"
    elif plan.planovany_datum:
        dovod = f"Dosiahnutý časový limit (Plánované na: {plan.planovany_datum.strftime('%d.%m.%Y')})"

    zakaznik_nazov = zariadenie.zakaznik.Nazov_spolocnosti if zariadenie.zakaznik else "Neznámy zákazník"
    mesto = zariadenie.zakaznik.mesto if zariadenie.zakaznik and zariadenie.zakaznik.mesto else "Neznáma lokalita"
    zariadenie_info = f"{zariadenie.model_zariadenia} (SN: {zariadenie.vyrobne_cislo})"

    GlobalneNastavenia = apps.get_model('ISASaplikacia', 'GlobalneNastavenia')
    nastavenia = GlobalneNastavenia.objects.first()
    base_url = (getattr(nastavenia, 'clean_base_url', None) or getattr(settings, 'SITE_URL', '')).strip().rstrip('/')
    isas_link = f"{base_url}/admin/Evidencia/servisovanezariadenie/{zariadenie.id}/change/"

    payload = (ISASNotificationBuilder(nadpis='PREDICTIVE: BLÍŽI SA SERVIS', ikona='🔧', farba_temy='#f59e0b')
               .pridaj_riadok('🏢 Zákazník:', f"{zakaznik_nazov} ({mesto})")
               .pridaj_riadok('⚙️ Zariadenie:', zariadenie_info)
               .pridaj_riadok('🛠️ Typ úkonu:', plan.typ_servisu.nazov)
               .pridaj_riadok('🎯 Dôvod:', dovod)
               .pridaj_riadok('⏪ Predošlý servis:', f"{posledny_servis} (Stav: {posledny_mh})")
               .nastav_tlacidlo('Otvoriť detail v systéme ISAS CORE', isas_link)
               .nastav_ai_poznamku('ISAS Predictive: Menšie úkony prekrývajúce sa s týmto servisom boli zlúčené.')
               .build())
    
    ISASNotifier.send(
        payload=payload,
        send_tg=send_tg,
        send_mail=send_mail,
        send_discord=send_discord
    )
    
    return True

@shared_task(base=ISASAutoHealTask)
def strazca_servisov_task():
    with isas_task_execution_guard('lock_strazca_servisov', timeout=300) as (acquired, gn, identita):
        if not acquired:
            return

        close_old_connections()
        dnes = timezone.now().date()
        cfg = get_compliance_config()
        predstih_dni = cfg['predstih_dni']
        limit_dni = dnes + timedelta(days=predstih_dni)
        limit_mh = cfg['predstih_motohodiny']

        # Zistenie globálnych predvolieb pre notifikácie a legislatívu
        sledovat_leg = cfg['sledovat_legislativu']
        send_tg = getattr(gn, 'servisy_notif_tg', True) if gn else True
        send_mail = getattr(gn, 'servisy_notif_mail', True) if gn else True
        send_dc = getattr(gn, 'servisy_notif_discord', True) if gn else True

        # =========================================================================
        # 1. KONTROLA PLÁNOVANÝCH SERVISOV PODĽA MOTOHODÍN A DÁTUMU
        # =========================================================================
        zariadenia_s_planom = ServisovaneZariadenie.objects.select_related('zakaznik').filter(
            planovane_servisy__upozornenie_odoslane=False
        ).distinct()
        pocet_upozorneni = 0

        for zariadenie in zariadenia_s_planom:
            # 🔇 Ochrana: Ak je stroj v servisnom režime, neposielame alarmy
            if getattr(zariadenie, 'v_servisnom_rezime', False):
                continue

            aktualne_mh = zariadenie.aktualne_motohodiny or 0
            aktualne_mh_zataz = getattr(zariadenie, 'motohodiny_v_zatazi', 0) or 0
            
            podmienky = Q(planovany_datum__isnull=False, planovany_datum__lte=limit_dni)

            if zariadenie.aktualne_motohodiny is not None:
                podmienky |= Q(planovane_motohodiny__isnull=False, planovane_motohodiny__lte=aktualne_mh + limit_mh)

            if getattr(zariadenie, 'motohodiny_v_zatazi', None) is not None and hasattr(PlanovanyServisZariadenia, 'planovane_motohodiny_zataz'):
                podmienky |= Q(planovane_motohodiny_zataz__isnull=False, planovane_motohodiny_zataz__lte=aktualne_mh_zataz + limit_mh)

            plany_na_upozornenie = PlanovanyServisZariadenia.objects.select_related(
                'typ_servisu', 'posledny_zaznam'
            ).filter(
                zariadenie=zariadenie, upozornenie_odoslane=False
            ).filter(podmienky).order_by('-vaha_hierarchie')

            if not plany_na_upozornenie.exists():
                continue

            top_plan = plany_na_upozornenie.first()
            uspesne_odoslane = odoslat_upozornenie_servis(top_plan, zariadenie, aktualne_mh, aktualne_mh_zataz, limit_mh, send_tg=send_tg, send_mail=send_mail, send_discord=send_dc)
            
            if uspesne_odoslane:
                pocet_upozorneni += 1
                plany_na_upozornenie.update(upozornenie_odoslane=True)
            else:
                logger.warning(f"⚠️ Notifikácia pre {zariadenie} zlyhala, neoznačujem v databáze.")

        # ===================================================================================
        # 2. ⚖️ LEGISLATÍVNY STRÁŽCA: ZÁKONNÉ A BEZPEČNOSTNÉ LEHOTY ZARIADENÍ PODĽA ODVETVIA
        # ===================================================================================
        base_url = (getattr(gn, 'clean_base_url', None) or getattr(settings, 'SITE_URL', '')).strip().rstrip('/')
        pocet_leg_upozorneni = 0

        # 🔇 Ochrana: Ak administrátor v UI globálne vypol sledovanie legislatívy
        if not sledovat_leg:
            logger.info("ℹ️ Sledovanie legislatívnych kontrol je v globálnych nastaveniach vypnuté.")
            return

        def spracuj_leg_notifikaciu(zar, p_item, stroj_popis, farba_temy='#dc2626'):
            nonlocal pocet_leg_upozorneni
            if not p_item.get('vyzaduje_alert'):
                return

            cache_kod = p_item.get('cache_kod', 'audit')
            stav_kod = p_item.get('stav', 'alert')
            # 🛡️ ISAS ARCHITECT FIX: Kľúč naviazaný na stav a 30-dňové TTL, bez prelomu mesiacov
            cache_key = f"isas_leg_{cache_kod}_{zar.id}_{stav_kod}"
            if cache.get(cache_key):
                return

            # 🛡️ ISAS FIX: Exaktné uzavretie predchádzajúcich stavov bez kolízie prefixov (napr. VTZ-PV vs VTZ-PV-KOMP)
            AlarmLog = apps.get_model('Evidencia', 'AlarmLog')
            if p_item.get('je_prepadnuta'):
                kod_base = p_item['kod'].rsplit('-', 1)[0]
                AlarmLog.objects.filter(
                    zariadenie=zar,
                    kod_chyby__in=[f"{kod_base}-WARN", f"{kod_base}-CHYBA"],
                    vyriesene=False
                ).update(vyriesene=True)

            # Bezpečné zaznamenanie do Čiernej skrinky (AlarmLog) s ochranou voči pretečeniu VARCHAR(255)
            zavaznost = 'CRITICAL' if (p_item.get('je_prepadnuta') or p_item.get('je_chybajuca')) else 'WARNING'
            popis_clean = f"Legislatíva: {p_item['nazov']} - {p_item['stav_str']}"[:250]
            if not AlarmLog.objects.filter(zariadenie=zar, kod_chyby=p_item['kod'], vyriesene=False).exists():
                AlarmLog.objects.create(
                    zariadenie=zar,
                    zakaznik=zar.zakaznik,
                    zavaznost=zavaznost,
                    kod_chyby=p_item['kod'],
                    popis=popis_clean
                )

            zak = zar.zakaznik
            zak_nazov = zak.Nazov_spolocnosti if zak else "Neznámy zákazník"
            mesto = zak.mesto if zak and zak.mesto else "Neznáma lokalita"
            zar_nazov = getattr(zar, 'nazov', None) or getattr(zar, 'model_zariadenia', None) or "Zariadenie"
            zar_info = f"{zar_nazov} (SN: {zar.vyrobne_cislo})"
            admin_url = f"{base_url}/admin/Evidencia/servisovanezariadenie/{zar.id}/change/"

            builder = (ISASNotificationBuilder(nadpis=f"LEGISLATÍVA: {p_item['nazov'].upper()}", ikona=p_item.get('ikona', '⚖️'), farba_temy=farba_temy)
                       .pridaj_riadok('🏢 Zákazník:', f"{zak_nazov} ({mesto})")
                       .pridaj_riadok('⚙️ Zariadenie:', f"{zar_info} ({stroj_popis})"))

            # 🛡️ ISAS ARCHITECT: Neutrálny odvetvový rendering bez straty dát pre dispečing
            for stitok, hodnota in p_item.get('extra_detaily', []):
                builder.pridaj_riadok(stitok, str(hodnota))

            builder.pridaj_riadok('📅 Stav lehoty:', str(p_item['stav_str']))
            farba_upozornenia = 'cervene' if p_item.get('je_prepadnuta') or p_item.get('je_chybajuca') else 'oranzove'
            
            # Pridanie legislatívneho disclaimeru a povinnej autorskej doložky (ochrana vývojára)
            disclaimer_text = p_item.get('disclaimer') or DEFAULT_LEGAL_DISCLAIMER
            upozornenie_full = f"{p_item['upozornenie_text']}<br><small style='color:#6b7280;'>⚖️ <em>{disclaimer_text}</em></small>"
            builder.nastav_upozornenie(upozornenie_full, farba_upozornenia)
            builder.nastav_tlacidlo('Otvoriť kartu zariadenia', admin_url)
            builder.nastav_ai_poznamku(p_item.get('ai_poznamka', 'ISAS Compliance: Zabezpečte predpísanú kontrolu.'))

            try:
                ISASNotifier.send(payload=builder.build(), send_tg=send_tg, send_mail=send_mail, send_discord=send_dc)
                cache.set(cache_key, True, timeout=2592000)
                pocet_leg_upozorneni += 1
            except Exception as e_send:
                logger.error(f"❌ Zlyhalo odoslanie legislatívneho alertu pre {zar} ({p_item['nazov']}): {e_send}", exc_info=True)

        # ⚖️ Centrálne spracovanie legislatívnej fronty cez Compliance Engine
        queue_items = get_global_compliance_queue(referencny_datum=dnes, limit_dni_alert=predstih_dni, filter_alerts_only=True)
        
        # 🛡️ ISAS FIX: Auto-resolve pre vykonané revízie (Likvidácia uviaznutých Zombie alarmov)
        aktivne_alarm_pary = {(it['zariadenie'].id, it['kod']) for it in queue_items if it.get('zariadenie')}
        nevyriesene_leg = AlarmLog.objects.filter(
            vyriesene=False,
            zariadenie__v_servisnom_rezime=False
        )
        if COMPLIANCE_ALARM_PREFIXES:
            prefix_filter = Q()
            for pfx in COMPLIANCE_ALARM_PREFIXES:
                prefix_filter |= Q(kod_chyby__startswith=pfx)
            nevyriesene_leg = nevyriesene_leg.filter(prefix_filter)
        else:
            nevyriesene_leg = nevyriesene_leg.none()

        zombie_ids = [a.id for a in nevyriesene_leg if (a.zariadenie_id, a.kod_chyby) not in aktivne_alarm_pary]
        if zombie_ids:
            # 🛡️ ISAS ARCHITECT: Dávkové uzatváranie po 500 ID proti pretečeniu SQL query parametrov
            for batch_start in range(0, len(zombie_ids), 500):
                batch_ids = zombie_ids[batch_start:batch_start + 500]
                AlarmLog.objects.filter(id__in=batch_ids).update(vyriesene=True)

        for item in queue_items:
            zar = item.get('zariadenie')
            if not zar:
                continue
            farba = '#dc2626' if item.get('farba') == 'red' else ('#ea580c' if item.get('farba') == 'orange' else '#2563eb')
            spracuj_leg_notifikaciu(zar, item, item.get('stroj_kategoria', 'zariadenie'), farba_temy=farba)

        logger.info(f"✅ Strážca servisov dokončil kontrolu. Odoslané servisné plány: {pocet_upozorneni}, Legislatívne kontroly: {pocet_leg_upozorneni}")

@shared_task(base=ISASAutoHealTask, bind=True, max_retries=3)
def process_iot_payload_task(self, payload, config_id):
    """
    🧠 ISAS CORE: Asynchrónne spracovanie IoT dát.
    Rozlišuje ROUTINE a ALARM režim. Využíva pesimistické zámky.
    """
    # config_id je teraz zariadenie_id
    try:
        zariadenie = ServisovaneZariadenie.objects.select_related('zakaznik').get(id=config_id)
    except ServisovaneZariadenie.DoesNotExist:
        logger.error(f"Zariadenie {config_id} neexistuje.")
        return

    mode = str(payload.get('mode') or 'ROUTINE').strip().upper()
    raw_data = payload.get('data', {})
    if not isinstance(raw_data, dict):
        raw_data = {}
        
    # 1. PREKLAD REGISTROV (Čítame priamo zo zariadenia)
    slovnik = {sig.register_adresa: sig for sig in SignalDictionary.objects.filter(zariadenie=zariadenie)}
    
    analogove_hodnoty = {}
    digitalne_stavy = {}
    nove_motohodiny_celkove = None
    nove_motohodiny_zataz = None

    for adresa, hodnota in raw_data.items():
        signal = slovnik.get(str(adresa))
        if not signal:
            continue # Ignorujeme neznáme registre
            
        if signal.typ_signalu == 'DIGITAL':
            # Digitálne signály parsujeme bezpečne a obchádzame float() konverziu
            digitalne_stavy[signal.nazov_signalu] = str(hodnota).lower() in ['true', '1', '1.0', 'on', 'yes']
            continue
            
        # Aplikovanie násobiča (napr. 75 * 0.1 = 7.5)
        try:
            # Ak senzor pošle "NaN" alebo "Error", preskočíme tento register
            vypocitana_hodnota = float(str(hodnota).replace(',', '.')) * float(str(signal.nasobic).replace(',', '.'))
            
            # Ochrana proti JSON serializačnej chybe v databáze
            if math.isnan(vypocitana_hodnota) or math.isinf(vypocitana_hodnota):
                logger.warning(f"Hardvér poslal NaN/Inf pre register {adresa}: {hodnota}")
                continue
        except (ValueError, TypeError):
            logger.warning(f"Neplatná hodnota pre register {adresa}: {hodnota}")
            continue

        analogove_hodnoty[signal.nazov_signalu] = vypocitana_hodnota
        
        # Detekcia motohodín podľa názvu signálu
        nazov_lower = signal.nazov_signalu.lower()
        if any(kw in nazov_lower for kw in ['motohodin', 'pocitadl', 'counter', 'operating_hour']):
            if 'zataz' in nazov_lower or 'záťaž' in nazov_lower or 'load' in nazov_lower:
                nove_motohodiny_zataz = int(vypocitana_hodnota)
            else:
                nove_motohodiny_celkove = int(vypocitana_hodnota)

    # ==========================================
    # 🔄 REŽIM: ROUTINE (Zápis histórie a aktualizácia stroja)
    # ==========================================
    if mode == 'ROUTINE':
        # A. Zápis do Time-Series databázy
        TelemetryHistory.objects.create(
            zariadenie=zariadenie,
            zakaznik=zariadenie.zakaznik,
            analogove_hodnoty=analogove_hodnoty,
            digitalne_stavy=digitalne_stavy
        )

        # Univerzálna automatická diagnostika telemetrie (100% dynamicky delegovaná na odvetvový modul)
        iot_alarm = vyhodnot_zariadenie_iot_telemetriu(zariadenie, analogove_hodnoty, digitalne_stavy=digitalne_stavy)
        if iot_alarm:
            # Ak je stroj v servise, alarmy ignorujeme
            if not zariadenie.v_servisnom_rezime:
                alarm = AlarmLog.objects.create(
                    zariadenie=zariadenie,
                    zakaznik=zariadenie.zakaznik,
                    zavaznost=iot_alarm.get('zavaznost', 'CRITICAL'),
                    kod_chyby=iot_alarm.get('kod_chyby', 'IOT_FAULT'),
                    popis=iot_alarm.get('popis', ''),
                    snapshot_dat={'analog': analogove_hodnoty, 'digital': digitalne_stavy}
                )
                odoslat_iot_alarm_notifikaciu(alarm, zariadenie)
            else:
                logger.info(f"🔇 Alarm ignorovaný (Servisný režim): {zariadenie.vyrobne_cislo}")

        # B. Bezpečná aktualizácia živých dát na zariadení (Pessimistic Lock)
        try:
            with transaction.atomic():
                zariadenie_locked = ServisovaneZariadenie.objects.select_for_update().get(id=zariadenie.id)
                
                # Posledná aktualizácia sa MUSÍ aktualizovať pri KAŽDOM príchode telemetrie, 
                # aby Dashboard a Konfigurátor neukazovali starý čas aj keď telemetria reálne prúdi.
                zariadenie_locked.posledna_aktualizacia_stavu = timezone.now()
                update_fields = ['posledna_aktualizacia_stavu']
                
                zmena_mh = False
                aktualne_mh = zariadenie_locked.aktualne_motohodiny or 0
                if nove_motohodiny_celkove is not None and nove_motohodiny_celkove > aktualne_mh:
                    zariadenie_locked.aktualne_motohodiny = nove_motohodiny_celkove
                    update_fields.append('aktualne_motohodiny')
                    zmena_mh = True
                    
                aktualne_zataz = getattr(zariadenie_locked, 'motohodiny_v_zatazi', 0) or 0
                if nove_motohodiny_zataz is not None and nove_motohodiny_zataz > aktualne_zataz:
                    zariadenie_locked.motohodiny_v_zatazi = nove_motohodiny_zataz
                    update_fields.append('motohodiny_v_zatazi')
                    zmena_mh = True

                zariadenie_locked.save(update_fields=update_fields)
                
                # 🚀 PREBUDENIE STRÁŽCU: Bezpečné spustenie až po commite transakcie do PostgreSQL
                if zmena_mh:
                    from .tasks import strazca_servisov_task
                    if not cache.get('lock_strazca_servisov'):
                        transaction.on_commit(lambda: strazca_servisov_task.delay())
                    
        except Exception as e:
            logger.error(f"Chyba pri aktualizácii stavu zariadenia (Zariadenie ID: {zariadenie.id}): {e}")
            raise self.retry(exc=e, countdown=5) # 🛡️ Vynútené prerušenie a skutočný Celery Retry

    # ==========================================
    # 🚨 REŽIM: ALARM (Čierna skrinka a Notifikácie)
    # ==========================================
    elif mode == 'ALARM':
        # Ak je stroj v servise, hardvérové alarmy ignorujeme
        if zariadenie.v_servisnom_rezime:
            logger.info(f"🔇 Hardvérový alarm ignorovaný (Servisný režim): {zariadenie.vyrobne_cislo}")
            return

        kod_chyby = str(payload.get('alarm_code') or 'NEZNÁMA CHYBA').strip()[:50]
        zavaznost_raw = str(payload.get('alarm_severity') or 'WARNING').strip().upper()
        zavaznost = zavaznost_raw if zavaznost_raw in ('WARNING', 'CRITICAL') else 'WARNING'
        popis = str(payload.get('alarm_description') or 'Automaticky detegovaný alarm z telemetrie.').strip()

        # Skontrolujeme, či už takýto nevyriešený alarm pre tento stroj existuje
        existujuci_alarm = AlarmLog.objects.filter(
            zariadenie=zariadenie, kod_chyby=kod_chyby, vyriesene=False
        ).first()

        if existujuci_alarm:
            # Ak existuje, len mu aktualizujeme čas a najnovšie dáta (nevytvárame duplikát a neposielame znova mail)
            existujuci_alarm.casova_peciatka = timezone.now()
            existujuci_alarm.snapshot_dat = {'analog': analogove_hodnoty, 'digital': digitalne_stavy}
            existujuci_alarm.save(update_fields=['casova_peciatka', 'snapshot_dat'])
        else:
            # A. Zápis do čiernej skrinky (Nový alarm)
            alarm = AlarmLog.objects.create(
                zariadenie=zariadenie,
                zakaznik=zariadenie.zakaznik,
                zavaznost=zavaznost,
                kod_chyby=kod_chyby,
                popis=popis,
                snapshot_dat={'analog': analogove_hodnoty, 'digital': digitalne_stavy}
            )
            # B. Odoslanie okamžitej notifikácie dispečerovi (Iba pri vzniku nového alarmu)
            odoslat_iot_alarm_notifikaciu(alarm, zariadenie)

def odoslat_iot_alarm_notifikaciu(alarm, zariadenie):
    """
    🧠 ISAS CORE: Helper funkcia pre odoslanie kritického alarmu.
    Využíva existujúcu infraštruktúru (Telegram + E-mail).
    """
    GlobalneNastavenia = apps.get_model('ISASaplikacia', 'GlobalneNastavenia')
    nastavenia = GlobalneNastavenia.objects.first()
    
    if not nastavenia:
        return

    zakaznik_nazov = zariadenie.zakaznik.Nazov_spolocnosti if zariadenie.zakaznik else "Neznámy zákazník"
    mesto = zariadenie.zakaznik.mesto if zariadenie.zakaznik and zariadenie.zakaznik.mesto else "Neznáma lokalita"
    zariadenie_info = f"{zariadenie.model_zariadenia} (SN: {zariadenie.vyrobne_cislo})"
    
    # Preklad závažnosti do slovenčiny
    zavaznost_sk = "KRITICKÁ PORUCHA" if alarm.zavaznost == 'CRITICAL' else "VAROVANIE"
    ikona = "🚨" if alarm.zavaznost == 'CRITICAL' else "⚠️"

    # Dynamický link na zariadenie
    base_url = (getattr(nastavenia, 'clean_base_url', None) or getattr(settings, 'SITE_URL', '')).strip().rstrip('/')
    isas_link = f"{base_url}/admin/Evidencia/servisovanezariadenie/{zariadenie.id}/change/"

    farba = '#ef4444' if alarm.zavaznost == 'CRITICAL' else '#f59e0b'
    
    # Formátovanie snapshotu pre hlavný text (bezpečné pre všetky typy senzorov bez prázdneho nadpisu)
    hlavny_text = ""
    if alarm.snapshot_dat and isinstance(alarm.snapshot_dat, dict):
        analog_data = alarm.snapshot_dat.get('analog', {})
        if isinstance(analog_data, dict) and analog_data:
            riadky_snapshotu = []
            for key, val in analog_data.items():
                zobrazena_hodnota = round(val, 2) if isinstance(val, (int, float)) else val
                riadky_snapshotu.append(f"├ {key.replace('_', ' ')}: <b>{zobrazena_hodnota}</b>")
            
            if riadky_snapshotu:
                hlavny_text = "📊 <b>Namerané hodnoty pri poruche:</b><br>" + "<br>".join(riadky_snapshotu[:8]) + "<br>"

    payload = (ISASNotificationBuilder(nadpis=f"IOT TELEMETRIA: {zavaznost_sk}", ikona=ikona, farba_temy=farba)
               .pridaj_riadok('🏢 Zákazník:', f"{zakaznik_nazov} ({mesto})")
               .pridaj_riadok('⚙️ Zariadenie:', zariadenie_info)
               .pridaj_riadok('🔢 Kód chyby:', alarm.kod_chyby)
               .pridaj_riadok('📝 Popis:', alarm.popis)
               .pridaj_riadok('⏱️ Čas vzniku:', alarm.casova_peciatka.strftime('%d.%m.%Y %H:%M:%S'))
               .nastav_hlavny_text(hlavny_text)
               .nastav_tlacidlo('Otvoriť detail v systéme ISAS CORE', isas_link)
               .nastav_ai_poznamku('ISAS IoT: Zariadenie nahlásilo poruchu. Vyžaduje sa okamžitý zásah dispečera.')
               .build())
    
    ISASNotifier.send(
        payload=payload,
        send_tg=nastavenia.iot_notif_tg,
        send_mail=nastavenia.iot_notif_mail,
        send_discord=nastavenia.iot_notif_discord
    )

@shared_task(base=ISASAutoHealTask, bind=True, max_retries=3)
def execute_modbus_write_task(self, zariadenie_id, adresa, nova_hodnota, user_id):
    """
    🧠 ISAS CORE: Reálny zápis príkazu do Redis fronty pre Edge Gateway / Simulátor.
    """
    try:
        zariadenie = ServisovaneZariadenie.objects.get(id=zariadenie_id)
        if not zariadenie.iot_je_aktivny or not zariadenie.iot_api_kluc:
            return {'status': 'error', 'message': 'Zariadenie nemá aktívny IoT prenos alebo API kľúč.'}
        signal = SignalDictionary.objects.get(zariadenie=zariadenie, register_adresa=adresa)
        
        if signal.prava_pristupu != 'RW':
            logger.error(f"Pokus o zápis do Read-Only registra {adresa}!")
            return {'status': 'error', 'message': 'Register je iba na čítanie.'}

        try:
            if signal.typ_signalu == 'DIGITAL':
                if isinstance(nova_hodnota, bool):
                    hodnota_pre_plc = 1 if nova_hodnota else 0
                elif str(nova_hodnota).lower() in ['true', '1', '1.0', 'on']:
                    hodnota_pre_plc = 1
                else:
                    hodnota_pre_plc = 0
            else:
                hodnota_pre_plc = float(str(nova_hodnota).replace(',', '.')) / float(str(signal.nasobic).replace(',', '.'))
                hodnota_pre_plc = int(round(hodnota_pre_plc))
        except (ValueError, TypeError, ZeroDivisionError):
            return {'status': 'error', 'message': 'Chyba pri prepočte hodnoty.'}

        payload = {
            "command": "WRITE_REGISTER",
            "register": adresa,
            "value": hodnota_pre_plc,
            "user_id": user_id
        }
        
        queue_name = f"iot_cmd_{zariadenie.iot_api_kluc}"
        with redis.Redis.from_url(settings.CELERY_BROKER_URL) as r:
            r.lpush(queue_name, json.dumps(payload))
            r.expire(queue_name, 3600)

        logger.info(f"✅ Príkaz odoslaný do Redisu [{queue_name}]: {payload}")
        return {'status': 'success', 'sent_value': hodnota_pre_plc}

    except Exception as e:
        logger.error(f"Chyba pri zápise Modbus: {e}")
        raise self.retry(exc=e, countdown=2)

@shared_task(base=ISASAutoHealTask, bind=True, max_retries=1)
def task_vycisti_staru_telemetriu(self, retencia_dni=365):
    """
    🧹 ISAS TIME-SERIES HYGIENA: Dávková archivácia a čistenie telemetrických dát
    starších ako 365 dní (prevencia nárastu tabuľky TelemetryHistory a table lockov).
    """
    with isas_task_execution_guard('lock_cistenie_telemetrie', timeout=300) as (acquired, gn, identita):
        if not acquired:
            logger.warning("🔒 Čistenie telemetrie už beží v inom procese. Preskakujem.")
            return 0

        hranica = datetime.now() - timedelta(days=retencia_dni)
        celkovo_zmazane = 0

        while True:
            stare_ids = list(TelemetryHistory.objects.filter(
                casova_peciatka__lt=hranica
            ).values_list('id', flat=True)[:5000])

            if not stare_ids:
                break

            zmazane, _ = TelemetryHistory.objects.filter(id__in=stare_ids).delete()
            celkovo_zmazane += zmazane

        if celkovo_zmazane > 0:
            logger.info(f"Úspešne odstránených {celkovo_zmazane} starých telemetrických záznamov.")
        return celkovo_zmazane


@shared_task(base=ISASAutoHealTask, bind=True, max_retries=1)
def drain_fallback_iot_spool_task(self):
    """
    🛡️ ISAS RESILIENCE: Atomické odčerpanie núdzového Redis spoolu s ochranou proti súbehu.
    """
    # 🔒 Mutex zámok: zaručí, že spool neodčerpávajú dva procesy súčasne
    with isas_task_execution_guard('lock_drain_iot_spool', timeout=55) as (acquired, gn, identita):
        if not acquired:
            logger.warning("🔒 Odčerpávanie IoT spoolu už beží v inom procese. Preskakujem.")
            return 0

        try:
            client = cache._cache.get_client()
        except Exception as e:
            logger.error(f"Nedostupný Redis klient pre odčerpanie spoolu: {e}")
            return 0

        processed_count = 0
        # Spracujeme maximálne 500 položiek na jeden beh úlohy, aby sme nezahltili Celery
        for _ in range(500):
            raw_item = client.lpop('iot_fallback_spool_queue')
            if not raw_item:
                break
            try:
                item = json.loads(raw_item.decode('utf-8') if isinstance(raw_item, bytes) else raw_item)
                payload = item.get('payload')
                config_id = item.get('config_id')
                if payload and config_id:
                    process_iot_payload_task.delay(payload, config_id)
                    processed_count += 1
            except Exception as err:
                logger.error(f"Chyba pri spracovaní položky z núdzového IoT spoolu: {err}")

        if processed_count > 0:
            logger.info(f"Úspešne odčerpaných {processed_count} položiek z núdzového IoT spoolu.")
        return processed_count

@shared_task(base=ISASAutoHealTask)
def generuj_protokol_pdf_task(zaznam_id, base_url, user_id=None, odoslat_mail=False):
    lock_id = f"lock_pdf_protokol_{zaznam_id}"
    
    with isas_task_execution_guard(lock_id, timeout=300, fallback_model=ServisnyZaznam, fallback_id=zaznam_id, fallback_field='protokol_uzavrety', fallback_value=False, fallback_note_field=None) as (acquired, gn, identita):
        if not acquired:
            return

        # 🛡️ DEFENSIVE: Bezpečné načítanie bez pádu DoesNotExist
        zaznam = ServisnyZaznam.objects.select_related('zakaznik', 'zariadenie', 'zariadenie__vyrobca').prefetch_related(
            'pouzite_polozky_zaznamu__content_object', 'typ_servisu', 'technici'
        ).filter(id=zaznam_id).first()
        
        if not zaznam:
            logger.error(f"Kritická chyba: ServisnyZaznam ID #{zaznam_id} nebol nájdený pri generovaní PDF.")
            return

        identita = FiremnaIdentita.objects.first()
        gn = GlobalneNastavenia.objects.first()

        poradove_cislo = zaznam.id
        if zaznam.zakaznik:
            poradove_cislo = ServisnyZaznam.objects.filter(zakaznik=zaznam.zakaznik, id__lte=zaznam.id).count()

        # Kaskáda pre Meno, Telefón a E-mail technika
        technik_meno = ""
        technik_telefon = ""
        technik_email = ""
        
        if zaznam.technici.exists():
            technik_meno = ", ".join([f"{t.meno or ''} {t.priezvisko or ''}".strip() for t in zaznam.technici.all() if (t.meno or t.priezvisko)])
            prvy_technik = zaznam.technici.first()
            technik_telefon = prvy_technik.telefon or ""
            technik_email = prvy_technik.email or ""
        elif user_id:
            User = get_user_model()
            try:
                u = User.objects.get(id=user_id)
                technik_meno = get_safe_employee_name(u, fallback_text=u.username)
                if hasattr(u, 'zamestnanec') and u.zamestnanec:
                    technik_telefon = u.zamestnanec.telefon or ""
                    technik_email = u.zamestnanec.email or ""
                else:
                    technik_email = u.email or ""
            except User.DoesNotExist:
                pass
        
        if not technik_meno:
            technik_meno = getattr(identita, 'vystavuje_servis_vykazy', '') or "Servisný technik"
        if not technik_telefon:
            technik_telefon = getattr(identita, 'telefon', '') or "-"
        if not technik_email:
            technik_email = getattr(identita, 'email_vseobecny', '') or "-"

        # Dynamické dohľadanie výrobcu z content_object
        materialy_list = list(zaznam.pouzite_polozky_zaznamu.all())
        for mat in materialy_list:
            mat.vyrobca_nazov = ""
            if hasattr(mat, 'content_object') and mat.content_object:
                obj = mat.content_object
                for field in obj._meta.fields:
                    if field.is_relation and field.related_model and field.related_model.__name__ == 'DatabazaVyrobcov':
                        val = getattr(obj, field.name)
                        if val:
                            mat.vyrobca_nazov = str(val).strip()
                            break
                if not mat.vyrobca_nazov:
                    for kandidat in ['vyrobca_dielu', 'vyrobca', 'znacka', 'brand']:
                        if hasattr(obj, kandidat):
                            val = getattr(obj, kandidat)
                            if val:
                                mat.vyrobca_nazov = str(val).strip()
                                break

        context = {
            'zaznam': zaznam,
            'identita': identita,
            'materialy': materialy_list,
            'poradove_cislo': poradove_cislo,
            'technik_meno': technik_meno,
            'technik_telefon': technik_telefon,
            'technik_email': technik_email
        }
        
        base_url = (getattr(gn, 'clean_base_url', None) or base_url or getattr(settings, 'SITE_URL', '')).strip().rstrip('/')
        kluc_hex = zaznam.bezpecnostny_kluc.hex[:6] if zaznam.bezpecnostny_kluc else uuid.uuid4().hex[:6]
        file_name = f"Servisny_protokol_{poradove_cislo}_{kluc_hex}.pdf"
        
        pdf_data, _ = ISASPDFBuilder.generate_and_save(
            template_name='admin/protokol_tlac.html',
            context=context,
            base_url=base_url,
            file_name=file_name,
            instance=zaznam,
            file_field_name='uzavrety_pdf_dokument',
            save_instance=True
        )

        # 🛡️ DEFENSIVE: E-mail zákazníkovi (Iba ak bol explicitne zaškrtnutý checkbox a e-mail je validný)
        raw_email = getattr(zaznam.zakaznik, 'email', '') or ''
        clean_email = str(raw_email).strip()
        
        if odoslat_mail and clean_email and '@' in clean_email and gn and gn.smtp_user:
            try:
                nazov_firmy = identita.nazov_spolocnosti if identita and identita.nazov_spolocnosti else "Naša spoločnosť"
                zamestnanci_mail_str = html.escape(technik_meno)
                zariadenie_mail_str = html.escape(str(zaznam.zariadenie)) if zaznam.zariadenie else "Neurčené"
                
                oslovenie = zaznam.zakaznik.get_email_greeting() if zaznam.zakaznik else "Vážený zákazník,"
                hlavny_text = (
                    f"{oslovenie}<br><br>"
                    "ďakujeme Vám za dôveru a využitie našich služieb. Servisné práce na Vašom zariadení boli úspešne ukončené. ✅ "
                    "V prílohe tohto e-mailu Vám zasielame elektronicky uzamknutý a obojstranne podpísaný servisný výkaz vo formáte PDF."
                )
                
                tlacidlo = None
                if identita and identita.web:
                    tlacidlo = {'text': 'Navštíviť našu stránku', 'url': identita.web}
                    
                builder = (ISASNotificationBuilder(nadpis=f"SERVISNÝ PROTOKOL #{poradove_cislo}", ikona='📄', farba_temy='#2563eb')
                           .pridaj_riadok('Dodávateľ:', nazov_firmy)
                           .pridaj_riadok('Vystavil (Technik):', zamestnanci_mail_str)
                           .pridaj_riadok('Evidenčné číslo:', f"#{poradove_cislo}")
                           .pridaj_riadok('Zariadenie:', zariadenie_mail_str)
                           .pridaj_riadok('Čas uzavretia:', zaznam.cas_podpisu.strftime('%d.%m.%Y o %H:%M') if zaznam.cas_podpisu else "-")
                           .nastav_hlavny_text(hlavny_text))
                if tlacidlo:
                    builder.nastav_tlacidlo(tlacidlo['text'], tlacidlo['url'])
                
                ISASNotifier.send(
                    payload=builder.build(),
                    to_email=clean_email,
                    send_tg=False,
                    send_mail=True,
                    send_discord=False,
                    attachments=[{
                        'filename': file_name,
                        'content': pdf_data,
                        'mimetype': 'application/pdf'
                    }]
                )
            except Exception as mail_err:
                # 🛡️ Zlyhanie mailu zákazníkovi NESMIE zhodiť task ani odvolať vygenerované PDF
                logger.error(f"Zlyhalo odoslanie e-mailu zákazníkovi pre protokol #{zaznam.id}: {mail_err}")

        # =======================
        # 🧠 Notifikácie majitel
        # =======================
        je_konatel = False
        if user_id:
            User = get_user_model()
            try:
                u = User.objects.get(id=user_id)
                if hasattr(u, 'zamestnanec') and u.zamestnanec and u.zamestnanec.typ_pomeru == 'KO':
                    je_konatel = True
            except User.DoesNotExist:
                pass

        # Ak to uzamkol konateľ osobne, nebudeme ho spamovať jeho vlastnou prácou
        if not je_konatel:
            bezpecny_zakaznik = html.escape(str(zaznam.zakaznik)) if zaznam.zakaznik else "Neuvedený zákazník"
            zariadenie_str = html.escape(str(zaznam.zariadenie)) if zaznam.zariadenie else "Neurčené"
            zamestnanci_str = html.escape(", ".join([f"{t.meno or ''} {t.priezvisko or ''}".strip() for t in zaznam.technici.all() if (t.meno or t.priezvisko)]))
            podpisujuci_str = html.escape(str(zaznam.meno_podpisujuceho)) if zaznam.meno_podpisujuceho else "Neuvedené"
            gps_str = html.escape(str(zaznam.gps_suradnice)) if zaznam.gps_suradnice else "Neznáma poloha"
            hviezdy = "⭐" * int(zaznam.hodnotenie_zakaznika) if zaznam.hodnotenie_zakaznika else "Nehodnotené"
            
            # Dynamický nadpis podľa toho, či bol použitý Smart Lock (chýba podpis zákazníka)
            if not zaznam.podpis_zakaznika:
                nadpis_notif = "PROTOKOL UZAVRETÝ TECHNIKOM (SMART LOCK)"
                ikona_notif = "🏢"
            else:
                nadpis_notif = "PROTOKOL UZAVRETÝ ZÁKAZNÍKOM"
                ikona_notif = "🟢"
            
            pdf_url = f"{base_url}{zaznam.uzavrety_pdf_dokument.url}" if zaznam.uzavrety_pdf_dokument else "#"
            
            try:
                payload = (ISASNotificationBuilder(nadpis=nadpis_notif, ikona=ikona_notif, farba_temy='#10b981')
                           .pridaj_riadok('🏢 Zákazník:', bezpecny_zakaznik)
                           .pridaj_riadok('⚙️ Zariadenie:', zariadenie_str)
                           .pridaj_riadok('👨‍🔧 Zamestnanci:', zamestnanci_str)
                           .pridaj_riadok('✍️ Podpísal(a):', podpisujuci_str)
                           .pridaj_riadok('🌟 Spokojnosť:', hviezdy)
                           .pridaj_riadok('📍 GPS Poloha:', gps_str)
                           .nastav_tlacidlo('Stiahnuť PDF Protokol', pdf_url)
                           .nastav_ai_poznamku('Protokol bol úspešne uzamknutý a vygenerovaný.')
                           .build())
                
                ISASNotifier.send(
                    payload=payload,
                    send_tg=zaznam.upozornit_majitela_protokol_tg,
                    send_mail=zaznam.upozornit_majitela_protokol_mail,
                    send_discord=getattr(zaznam, 'upozornit_majitela_protokol_discord', True)
                )
            except Exception as notif_err:
                # 🛡️ DEFENSIVE: Výpadok Telegramu/Discordu NESMIE spustiť rollback a odomknúť podpísaný protokol!
                logger.error(f"Zlyhalo odoslanie notifikácie vedeniu pre protokol #{zaznam.id}: {notif_err}")


@shared_task(base=ISASAutoHealTask)
def odoslat_protokol_zakaznikovi_manualne_task(zaznam_id):
    """
    🛡️ ENTERPRISE SAFE: Manuálne odoslanie skontrolovaného PDF zákazníkovi.
    Zabezpečené proti súbehu (Anti-Race Condition Lock) a výpadkom disku/siete.
    """
    # 1. Distribuovaný zámok v Cache proti duplicitnému klikaniu v administrácii
    lock_key = f"lock_manual_send_pdf_{zaznam_id}"
    if not cache.add(lock_key, True, timeout=60):
        logger.warning(f"Odoslanie protokolu #{zaznam_id} zákazníkovi už prebieha v inom vlákne.")
        return False

    try:
        zaznam = ServisnyZaznam.objects.select_related('zakaznik', 'zariadenie').filter(id=zaznam_id).first()
        if not zaznam or not zaznam.uzavrety_pdf_dokument or not zaznam.zakaznik:
            return False

        raw_email = getattr(zaznam.zakaznik, 'email', '') or ''
        clean_email = str(raw_email).strip()
        if not clean_email or '@' not in clean_email:
            logger.warning(f"Zákazník pri zázname #{zaznam_id} nemá platnú e-mailovú adresu: '{raw_email}'")
            return False
            
        identita = FiremnaIdentita.objects.first()
        gn = GlobalneNastavenia.objects.first()
        if not gn or not gn.smtp_user:
            logger.error("Odoslanie zlyhalo: SMTP nie je nakonfigurované v Globálnych nastaveniach.")
            return False

        # 2. Bezpečné čítanie PDF z disku s ochranou pred FileNotFoundError
        try:
            with zaznam.uzavrety_pdf_dokument.open('rb') as f:
                pdf_bytes = f.read()
        except (FileNotFoundError, OSError, ValueError) as file_err:
            logger.error(f"Fyzický PDF súbor protokolu #{zaznam_id} nebol na disku nájdený: {file_err}")
            return False

        nazov_firmy = identita.nazov_spolocnosti if identita and identita.nazov_spolocnosti else "Naša spoločnosť"
        poradove_cislo = zaznam.poradove_cislo
        zariadenie_str = html.escape(str(zaznam.zariadenie)) if zaznam.zariadenie else "Neurčené"
        oslovenie = zaznam.zakaznik.get_email_greeting() if zaznam.zakaznik else "Vážený zákazník,"
        
        hlavny_text = (
            f"{oslovenie}<br><br>"
            "ďakujeme Vám za dôveru. V nadväznosti na vykonaný servis Vám v prílohe "
            "zasielame autorizovaný a obojstranne potvrdený servisný protokol vo formáte PDF."
        )
        
        builder = (ISASNotificationBuilder(nadpis=f"SERVISNÝ PROTOKOL #{poradove_cislo}", ikona='📄', farba_temy='#2563eb')
                   .pridaj_riadok('Dodávateľ:', nazov_firmy)
                   .pridaj_riadok('Evidenčné číslo:', f"#{poradove_cislo}")
                   .pridaj_riadok('Zariadenie:', zariadenie_str)
                   .pridaj_riadok('Čas uzavretia:', zaznam.cas_podpisu.strftime('%d.%m.%Y o %H:%M') if zaznam.cas_podpisu else "-")
                   .nastav_hlavny_text(hlavny_text))

        if identita and identita.web:
            builder.nastav_tlacidlo('Navštíviť našu stránku', identita.web)

        file_name = os.path.basename(zaznam.uzavrety_pdf_dokument.name)

        # 3. Bezpečné odoslanie cez ISASNotifier
        ISASNotifier.send(
            payload=builder.build(),
            to_email=clean_email,
            send_tg=False,
            send_mail=True,
            send_discord=False,
            attachments=[{
                'filename': file_name,
                'content': pdf_bytes,
                'mimetype': 'application/pdf'
            }]
        )
        return True

    except Exception as e:
        logger.error(f"Neočakávaná chyba pri manuálnom odosielaní protokolu #{zaznam_id}: {e}")
        return False
    finally:
        # Vždy uvoľníme zámok
        cache.delete(lock_key)

@shared_task(base=ISASAutoHealTask)
def generuj_ponuku_pdf_task(ponuka_id, base_url, notif_tg, notif_mail, notif_discord=True, send_notif=True):
    lock_id = f"lock_pdf_ponuka_{ponuka_id}"
    
    with isas_task_execution_guard(lock_id, timeout=300) as (acquired, gn, identita):
        if not acquired:
            return

        PolozkaCenovejPonuky = apps.get_model('Evidencia', 'PolozkaCenovejPonuky')

        ponuka = CenovaPonuka.objects.select_related(
            'zakaznik', 'vystavil', 'vystavil__zamestnanec', 'zariadenie'
        ).prefetch_related(
            Prefetch('polozky', queryset=PolozkaCenovejPonuky.objects.select_related('zariadenie', 'content_type')),
            'typ_servisu'
        ).filter(id=ponuka_id).first()

        if not ponuka:
            logger.error(f"❌ Cenová ponuka ID {ponuka_id} neexistuje alebo bola zmazaná. Task sa ukončuje.")
            return
        
        identita = FiremnaIdentita.objects.first()
        gn = GlobalneNastavenia.objects.first()

        fallback_meno = getattr(identita, 'vystavuje_ponuky', '') or getattr(identita, 'konatel', '') or "Obchodné oddelenie"
        vystavil_meno = get_safe_employee_name(ponuka.vystavil, fallback_text=fallback_meno)

        platnost_ponuky_dni = getattr(identita, 'predvolena_platnost_ponuk', 14) or 14
        garancie_prace = [{'nazov': praca.nazov, 'mesiace': praca.zaruka_mesiace} for praca in ponuka.typ_servisu.all() if praca.zaruka_mesiace is not None and praca.zaruka_mesiace > 0]

        polozky_ponuky = list(ponuka.polozky.all())
        for p in polozky_ponuky:
            # Delegovanie komplexnej logiky na doménovú utilitu
            p.kategoria_nazov, p.vyrobca_nazov = ISASProductMetadataResolver.resolve(p)

        context = {
            'ponuka': ponuka,
            'identita': identita,
            'polozky': polozky_ponuky,
            'garancie': garancie_prace,
            'terms': get_all_industry_terms()
        }
        
        base_url = (getattr(gn, 'clean_base_url', None) or base_url or getattr(settings, 'SITE_URL', '')).strip().rstrip('/')
        cislo_ponuky_clean = re.sub(r'[^a-zA-Z0-9_\-]', '_', str(ponuka.cislo_ponuky or f"ponuka_{ponuka.id}"))
        file_name = f"{cislo_ponuky_clean}.pdf"

        # 🗑️ OCHRANA PRED DUPLIKÁTOM: Fyzické zmazanie starého PDF z disku pred vygenerovaním nového
        if ponuka.pdf_dokument and ponuka.pdf_dokument.name:
            try:
                if default_storage.exists(ponuka.pdf_dokument.name):
                    default_storage.delete(ponuka.pdf_dokument.name)
            except Exception as del_e:
                logger.warning(f"⚠️ Nepodarilo sa odstrániť starý súbor ponuky {ponuka.cislo_ponuky}: {del_e}")
        
        pdf_data, _ = ISASPDFBuilder.generate_and_save(
            template_name='admin/cenova_ponuka_tlac.html',
            context=context,
            base_url=base_url,
            file_name=file_name,
            instance=ponuka,
            file_field_name='pdf_dokument',
            save_instance=True,
            append_slash=True
        )

        # 🛑 OCHRANA DRAFTU: Ak ide o koncept alebo send_notif=False, PDF je bezpečne uložené a task končí bez posielania mailov
        if not send_notif or ponuka.stav == 'DRAFT':
            return

        # ⚙️ Agregácia VŠETKÝCH unikátnych zariadení (Definovaná GLOBÁLNE, aby nezlyhala notifikácia vedeniu pri zákazníkovi bez mailu)
        zariadenia_set = set()
        if ponuka.zariadenie:
            zariadenia_set.add(f"{ponuka.zariadenie.model_zariadenia} (S/N: {ponuka.zariadenie.vyrobne_cislo or 'N/A'})")
            
        for p in polozky_ponuky:
            if p.zariadenie:
                zariadenia_set.add(f"{p.zariadenie.model_zariadenia} (S/N: {p.zariadenie.vyrobne_cislo or 'N/A'})")

        if zariadenia_set:
            zariadenie_text_plain = ", ".join(zariadenia_set)
            zariadenie_text_html = "\n".join([f"⚙️ {z}" for z in zariadenia_set])
        else:
            zariadenie_text_plain = "Všeobecná dodávka / Služby"
            zariadenie_text_html = "Všeobecná dodávka / Služby"

        # E-mail zákazníkovi
        if ponuka.zakaznik and ponuka.zakaznik.email and pdf_data and gn and gn.smtp_user:
            nazov_firmy = identita.nazov_spolocnosti if identita and identita.nazov_spolocnosti else "Naša spoločnosť"
            
            # Dynamické oslovenie podľa databázy
            oslovenie = ponuka.zakaznik.get_email_greeting()

            # Inteligentné rozpoznanie DPH pre e-mail
            if identita and not identita.je_platca_dph:
                label_suma = "Celková suma:"
            elif ponuka.prenos_danovej_povinnosti:
                label_suma = "Celková suma (bez DPH):"
            else:
                label_suma = "Celková suma s DPH:"

            # Bezpečné vytvorenie odkazu (magic link) pre schválenie ponuky zákazníkom (s očisteným base_url)
            clean_base = (gn.clean_base_url if gn and gn.clean_base_url else base_url).rstrip('/')
            magic_link = f"{clean_base}/evidencia/ponuka/{ponuka.bezpecnostny_kluc}/"
            
            ponuka_mena = getattr(ponuka, 'mena', '') or get_firemna_mena()
            payload_zakaznik = (ISASNotificationBuilder(nadpis='CENOVÁ PONUKA', ikona='📄', farba_temy='#2563eb')
                                .pridaj_riadok('Dodávateľ:', nazov_firmy)
                                .pridaj_riadok('Vystavil:', vystavil_meno)
                                .pridaj_riadok('Číslo ponuky:', ponuka.cislo_ponuky)
                                .pridaj_riadok('Zariadenia:', zariadenie_text_html)
                                .pridaj_riadok('Platnosť ponuky:', f"{platnost_ponuky_dni} dní")
                                .pridaj_riadok(label_suma, f"{ponuka.celkom_s_dph} {ponuka_mena}")
                                .nastav_hlavny_text(f"{oslovenie}<br><br>na základe Vašej požiadavky Vám zasielame vypracovanú cenovú ponuku. Kompletný dokument s detailným rozpisom položiek a cien nájdete v priloženom PDF súbore.")
                                .nastav_tlacidlo('Zobraziť a schváliť ponuku', magic_link)
                                .build())
            
            try:
                ISASNotifier.send(
                    payload=payload_zakaznik,
                    to_email=ponuka.zakaznik.email,
                    send_tg=False,
                    send_mail=True,
                    send_discord=False,
                    attachments=[{
                        'filename': file_name,
                        'content': pdf_data,
                        'mimetype': 'application/pdf'
                    }]
                )
            except Exception as mail_err:
                logger.error(f"⚠️ Chyba pri odosielaní e-mailu zákazníkovi pre ponuku {ponuka.cislo_ponuky}: {mail_err}")

        # Notifikácie pre vedenie
        if (notif_tg or notif_mail or notif_discord) and gn:
            url_ponuky = f"{base_url}/admin/Evidencia/cenovaponuka/{ponuka.id}/change/"
            
            label_zar = get_industry_term('zariadenie_fallback_nazov', 'Zariadenie')
            ponuka_mena = getattr(ponuka, 'mena', '') or get_firemna_mena()
            payload = (ISASNotificationBuilder(nadpis='NOVÁ CENOVÁ PONUKA', ikona='💰', farba_temy='#3b82f6')
                       .pridaj_riadok('🏢 Zákazník:', ponuka.zakaznik.Nazov_spolocnosti)
                       .pridaj_riadok('🔢 Číslo ponuky:', ponuka.cislo_ponuky)
                       .pridaj_riadok(f'⚙️ {label_zar}:', zariadenie_text_html)
                       .pridaj_riadok('💰 Celková suma:', f"{ponuka.celkom_s_dph} {ponuka_mena}")
                       .pridaj_riadok('👤 Vystavil:', vystavil_meno)
                       .nastav_tlacidlo('Otvoriť ponuku v systéme ISAS CORE', url_ponuky)
                       .nastav_ai_poznamku('Cenová ponuka bola vygenerovaná a uložená.')
                       .build())
            
            ISASNotifier.send(
                payload=payload,
                send_tg=notif_tg,
                send_mail=notif_mail,
                send_discord=notif_discord
            )

@shared_task(base=ISASAutoHealTask, bind=True, max_retries=3, default_retry_delay=30)
def task_notifikuj_schvalenie_ponuky(self, ponuka_id):
    """
    🧠 ISAS CORE: Enterprise asynchrónna notifikácia o schválení ponuky klientom.
    Zabezpečuje idempotenciu (Redis lock), sanitizáciu vstupov a auto-retry pri sieťovom výpadku.
    """
    lock_id = f"lock_notif_ponuka_schvalena_{ponuka_id}"
    
    with isas_task_execution_guard(lock_id, timeout=120) as (acquired, gn, identita):
        if not acquired:
            logger.warning(f"⏭️ Notifikácia pre ponuku ID {ponuka_id} už bola odoslaná alebo práve prebieha. Preskakujem duplicitu.")
            return

        try:
            ponuka = CenovaPonuka.objects.select_related('zakaznik', 'zariadenie').filter(id=ponuka_id).first()
            if not ponuka:
                logger.error(f"❌ Cenová ponuka ID {ponuka_id} neexistuje.")
                return

            if ponuka.stav not in ['ACCEPTED', 'CONVERTED']:
                logger.warning(f"⚠️ Ponuka ID {ponuka_id} nie je v schválenom stave ({ponuka.stav}). Notifikácia zrušená.")
                return

            base_url = (getattr(gn, 'clean_base_url', None) or getattr(settings, 'SITE_URL', '')).strip().rstrip('/')
            admin_url = f"{base_url}/admin/Evidencia/cenovaponuka/{ponuka.id}/change/"

            # 🔄 1. AUTOMATICKÉ PREGENEROVANIE PDF S PODPISOM ZÁKAZNÍKA NA DISKU
            try:
                PolozkaCenovejPonuky = apps.get_model('Evidencia', 'PolozkaCenovejPonuky')
                ponuka_full = CenovaPonuka.objects.select_related(
                    'zakaznik', 'vystavil', 'vystavil__zamestnanec', 'zariadenie'
                ).prefetch_related(
                    Prefetch('polozky', queryset=PolozkaCenovejPonuky.objects.select_related('zariadenie', 'content_type')),
                    'typ_servisu'
                ).filter(id=ponuka_id).first()

                if ponuka_full:
                    polozky_ponuky = list(ponuka_full.polozky.all())
                    for p in polozky_ponuky:
                        p.kategoria_nazov, p.vyrobca_nazov = ISASProductMetadataResolver.resolve(p)

                    garancie_prace = [{'nazov': praca.nazov, 'mesiace': praca.zaruka_mesiace} for praca in ponuka_full.typ_servisu.all() if praca.zaruka_mesiace is not None and praca.zaruka_mesiace > 0]

                    context_pdf = {
                        'ponuka': ponuka_full,
                        'identita': identita,
                        'polozky': polozky_ponuky,
                        'garancie': garancie_prace,
                        'terms': get_all_industry_terms()
                    }
                    file_name = f"{ponuka_full.cislo_ponuky}.pdf"

                    # 🗑️ OCHRANA PRED DUPLIKÁTOM: Fyzické zmazanie starého nepodpísaného PDF z disku
                    if ponuka_full.pdf_dokument and ponuka_full.pdf_dokument.name:
                        try:
                            if default_storage.exists(ponuka_full.pdf_dokument.name):
                                default_storage.delete(ponuka_full.pdf_dokument.name)
                        except Exception as del_e:
                            logger.warning(f"⚠️ Nepodarilo sa odstrániť starý súbor ponuky: {del_e}")

                    ISASPDFBuilder.generate_and_save(
                        template_name='admin/cenova_ponuka_tlac.html',
                        context=context_pdf,
                        base_url=base_url,
                        file_name=file_name,
                        instance=ponuka_full,
                        file_field_name='pdf_dokument',
                        save_instance=True,
                        append_slash=True
                    )
                    logger.info(f"✅ PDF cenovej ponuky {ponuka_full.cislo_ponuky} bolo úspešne pregenerované s podpisom.")
            except Exception as pdf_err:
                logger.error(f"❌ Chyba pri pregenerovaní PDF ponuky s podpisom: {pdf_err}")

            cas_lokalny = timezone.localtime(ponuka.cas_schvalenia) if (ponuka.cas_schvalenia and timezone.is_aware(ponuka.cas_schvalenia)) else (ponuka.cas_schvalenia or timezone.now())
            cas_formatovany = cas_lokalny.strftime('%d.%m.%Y %H:%M:%S')

            bezpecne_meno = html.escape(str(ponuka.meno_schvalitela or 'Neuvedené'))
            bezpecny_zakaznik = html.escape(str(ponuka.zakaznik.Nazov_spolocnosti if ponuka.zakaznik else 'Neznámy'))
            bezpecna_ip = html.escape(str(ponuka.ip_adresa_schvalitela or 'Neznáma'))

            ponuka_mena = getattr(ponuka, 'mena', '') or get_firemna_mena()
            builder = (ISASNotificationBuilder(nadpis='ZÁKAZNÍK SCHVÁLIL PONUKU', ikona='✅', farba_temy='#10b981')
                       .pridaj_riadok('🏢 Zákazník:', bezpecny_zakaznik)
                       .pridaj_riadok('👤 Schválil:', bezpecne_meno)
                       .pridaj_riadok('📄 Číslo ponuky:', ponuka.cislo_ponuky))

            if ponuka.zariadenie:
                label_zar = get_industry_term('zariadenie_fallback_nazov', 'Zariadenie')
                builder.pridaj_riadok(f'⚙️ {label_zar}:', html.escape(str(ponuka.zariadenie)))

            dph_text = "(Neplatca DPH)" if (identita and not identita.je_platca_dph) else ("(Prenos daň. pov.)" if ponuka.prenos_danovej_povinnosti else "s DPH")
            builder.pridaj_riadok('💰 Suma celkom:', f"{ponuka.celkom_s_dph} {ponuka_mena} {dph_text}".strip())
            builder.pridaj_riadok('🕒 Čas schválenia:', cas_formatovany)
            builder.pridaj_riadok('🌐 IP adresa:', bezpecna_ip)
            builder.nastav_tlacidlo('Otvoriť ponuku v systéme ISAS CORE', admin_url)
            builder.nastav_ai_poznamku('Ponuka bola prepnutá do stavu ACCEPTED a je pripravená na konverziu do kalendára.')

            ISASNotifier.send(
                payload=builder.build(),
                send_tg=True,
                send_mail=True,
                send_discord=True
            )
            logger.info(f"✅ Notifikácia o schválení ponuky {ponuka.cislo_ponuky} úspešne odoslaná.")

        except Exception as exc:
            logger.error(f"❌ Chyba pri spracovaní notifikácie pre ponuku ID {ponuka_id}: {exc}")
            # Pri sieťovej chybe Celery zopakuje task s exponenciálnym oneskorením
            raise self.retry(exc=exc)

@shared_task(base=ISASAutoHealTask)
def vygeneruj_pdf_a_notifikuj_fakturu_task(faktura_id, base_url, send_notif=True):
    lock_id = f"lock_pdf_faktura_{faktura_id}"
    
    VystavenaFaktura = apps.get_model('Evidencia', 'VystavenaFaktura')
    faktura = None

    with isas_task_execution_guard(lock_id, timeout=300) as (acquired, gn, identita):
        if not acquired:
            return

        PolozkaVystavenejFaktury = apps.get_model('Evidencia', 'PolozkaVystavenejFaktury')

        faktura = VystavenaFaktura.objects.select_related(
            'zakaznik', 'vystavil', 'vystavil__zamestnanec', 'zariadenie'
        ).prefetch_related(
            Prefetch('polozky', queryset=PolozkaVystavenejFaktury.objects.select_related('zariadenie', 'content_type')),
            'typ_servisu'
        ).filter(id=faktura_id).first()

        if not faktura:
            logger.error(f"❌ Vystavená faktúra ID {faktura_id} neexistuje alebo bola zmazaná. Task sa ukončuje.")
            return

        identita = FiremnaIdentita.objects.first()
        gn = GlobalneNastavenia.objects.first()

        # Kaskádový výpočet dátumu splatnosti
        if not faktura.datum_splatnosti:
            
            # Základom je dátum vystavenia, ak chýba, použijeme dnešok
            zakladny_datum = faktura.datum_vystavenia or timezone.now().date()
            
            # KASKÁDA
            if faktura.zakaznik and getattr(faktura.zakaznik, 'predvolena_splatnost_dni', None) is not None:
                splatnost_dni = faktura.zakaznik.predvolena_splatnost_dni
            elif identita and getattr(identita, 'splatnost_faktur', None) is not None:
                splatnost_dni = identita.splatnost_faktur
            else:
                splatnost_dni = 14
            
            # Bezpečnostná poistka, ak by v DB bol nejaký nezmysel (napr. string)
            try:
                splatnost_dni = int(splatnost_dni)
            except (ValueError, TypeError):
                splatnost_dni = 14
                
            faktura.datum_splatnosti = zakladny_datum + timedelta(days=splatnost_dni)
            # Fyzicky to uložíme do DB, aby to sedelo s PDFkom
            faktura.save(update_fields=['datum_splatnosti'])

        fallback_meno = getattr(identita, 'vystavuje_faktury', '') or getattr(identita, 'konatel', '') or "Oprávnená osoba"
        bezpecne_meno_vystavovatela = get_safe_employee_name(faktura.vystavil, fallback_text=fallback_meno)

        polozky_faktury = list(faktura.polozky.all())
        for p in polozky_faktury:
            # Delegovanie komplexnej logiky na doménovú utilitu
            p.kategoria_nazov, p.vyrobca_nazov = ISASProductMetadataResolver.resolve(p)

        # Ak sú snapshot údaje odberateľa "None" (napr. pri zrýchlenom uložení), natiahneme ich priamo z karty Zákazníka
        if str(faktura.odberatel_nazov) == 'None' or not faktura.odberatel_nazov:
            if faktura.zakaznik:
                faktura.odberatel_nazov = getattr(faktura.zakaznik, 'Nazov_spolocnosti', '') or getattr(faktura.zakaznik, 'nazov_spolocnosti', '')
                
                ulica = getattr(faktura.zakaznik, 'ulica_a_cislo', '') or getattr(faktura.zakaznik, 'Ulica_a_cislo', '') or getattr(faktura.zakaznik, 'ulica', '')
                psc = getattr(faktura.zakaznik, 'psc', '') or getattr(faktura.zakaznik, 'PSC', '')
                mesto = getattr(faktura.zakaznik, 'mesto', '') or getattr(faktura.zakaznik, 'Mesto', '')
                stat = getattr(faktura.zakaznik, 'stat', '') or getattr(faktura.zakaznik, 'Stat', '')
                
                faktura.odberatel_ulica = ulica
                faktura.odberatel_psc = psc
                faktura.odberatel_mesto = mesto
                faktura.odberatel_stat = stat
                
                faktura.odberatel_ico = getattr(faktura.zakaznik, 'Ico', '') or getattr(faktura.zakaznik, 'ico', '')
                faktura.odberatel_dic = getattr(faktura.zakaznik, 'Dic', '') or getattr(faktura.zakaznik, 'dic', '')
                faktura.odberatel_ic_dph = getattr(faktura.zakaznik, 'Ic_dph', '') or getattr(faktura.zakaznik, 'ic_dph', '')

                adresa_diely = [p for p in [faktura.odberatel_ulica, f"{faktura.odberatel_psc} {faktura.odberatel_mesto}".strip(), faktura.odberatel_stat] if p]
                faktura.odberatel_sidlo = ", ".join(adresa_diely)

                # 🛡️ ISAS ARCHITECT FIX: Ukladáme aj skompletizované sídlo odberateľa
                faktura.save(update_fields=[
                    'odberatel_nazov', 'odberatel_ulica', 'odberatel_psc', 
                    'odberatel_mesto', 'odberatel_stat', 'odberatel_sidlo',
                    'odberatel_ico', 'odberatel_dic', 'odberatel_ic_dph'
                ])

        # ⚙️ GLOBÁLNA AGREGÁCIA VŠETKÝCH UNIKÁTNYCH ZARIADENÍ
        zariadenia_dict = {}
        if faktura.zariadenie:
            zariadenia_dict[faktura.zariadenie.id] = faktura.zariadenie
            
        for p in polozky_faktury:
            if p.zariadenie:
                zariadenia_dict[p.zariadenie.id] = p.zariadenie

        # 3. Posielame obohatený list do kontextu
        context = {
            'faktura': faktura, 
            'identita': identita, 
            'gn': gn,
            'polozky': polozky_faktury,
            'unikatne_zariadenia': list(zariadenia_dict.values())
        }
        
        # 🛡️ ISAS ARCHITECT FIX: Ochrana pred prepísaním na prázdny string, ak clean_base_url nie je vyplnená
        base_url = (getattr(gn, 'clean_base_url', None) or base_url or getattr(settings, 'SITE_URL', '')).strip().rstrip('/')
        cislo_fa_clean = re.sub(r'[^a-zA-Z0-9_\-]', '_', str(faktura.cislo_faktury or f"faktura_{faktura.id}"))
        file_name = f"{cislo_fa_clean}.pdf"

        # 🗑️ OCHRANA PRED DUPLIKÁTOM: Fyzické zmazanie predchádzajúceho PDF z disku pred vygenerovaním nového
        if faktura.pdf_dokument and faktura.pdf_dokument.name:
            try:
                if default_storage.exists(faktura.pdf_dokument.name):
                    default_storage.delete(faktura.pdf_dokument.name)
            except Exception as del_e:
                logger.warning(f"⚠️ Nepodarilo sa odstrániť starý súbor faktúry {faktura.cislo_faktury}: {del_e}")
        
        pdf_data, _ = ISASPDFBuilder.generate_and_save(
            template_name='admin/faktura_tlac.html',
            context=context,
            base_url=base_url,
            file_name=file_name,
            instance=faktura,
            file_field_name='pdf_dokument',
            save_instance=True
        )

        # 🇪🇺 PREDBEŽNÉ GENEROVANIE eFAKTÚRY (XML) PRE NÁHĽAD V ADMINE (Stav: NACHYSTANA)
        je_b2b = bool(faktura.odberatel_ico or faktura.odberatel_dic or faktura.odberatel_ic_dph)

        if je_b2b and faktura.typ_dokladu != 'ZALOHA' and faktura.cislo_faktury:
            # Generujeme XML len ak ešte nebolo odoslané štátu (ochrana pred prepísaním uzamknutej eFaktúry)
            if faktura.peppol_status not in ['SENT', 'PROCESSING']:
                try:
                    # 🗑️ OCHRANA PRED DUPLIKÁTOM: Zmazanie starého XML z disku pred vytvorením nového
                    if faktura.peppol_xml_dokument and faktura.peppol_xml_dokument.name:
                        try:
                            if default_storage.exists(faktura.peppol_xml_dokument.name):
                                default_storage.delete(faktura.peppol_xml_dokument.name)
                        except Exception as del_e:
                            logger.warning(f"⚠️ Nepodarilo sa odstrániť starý XML súbor faktúry {faktura.cislo_faktury}: {del_e}")

                    generator = PeppolXMLGenerator()
                    xml_data = generator.vygeneruj_peppol_xml(faktura)
                    xml_bytes = xml_data if isinstance(xml_data, bytes) else xml_data.encode('utf-8')
                    faktura.peppol_xml_hash = hashlib.sha256(xml_bytes).hexdigest()

                    xml_file_name = f"peppol_{faktura.cislo_faktury}.xml"
                    faktura.peppol_xml_dokument.save(name=xml_file_name, content=ContentFile(xml_bytes), save=False)
                    faktura.save(update_fields=['peppol_xml_dokument', 'peppol_xml_hash'])
                    logger.info(f"✅ XML eFaktúra {faktura.cislo_faktury} bola úspešne vygenerovaná na kontrolu pred odoslaním.")
                except Exception as xml_err:
                    logger.error(f"⚠️ Chyba pri predbežnom generovaní Peppol XML pre FA {faktura.cislo_faktury}: {xml_err}")

        nazov_firmy = identita.nazov_spolocnosti if identita and identita.nazov_spolocnosti else "Naša spoločnosť"
        aktualny_rok = timezone.now().year

        # Vytvorenie textu zo slovníka zariadenia_dict, ktorý sme definovali vyššie
        if zariadenia_dict:
            zariadenia_texty = [f"{z.model_zariadenia} (S/N: {z.vyrobne_cislo})" for z in zariadenia_dict.values()]
            zariadenie_text_plain = ", ".join(zariadenia_texty)
            zariadenie_text_html = "\n".join([f"⚙️ {t}" for t in zariadenia_texty])
        else:
            zariadenie_text_plain = "Všeobecná dodávka / Služby"
            zariadenie_text_html = "Všeobecná dodávka / Služby"

        # 🖼️ LOGO HANDLING
        logo_html = ""
        if identita and identita.logo and base_url:
            logo_url = f"{base_url}{identita.logo.url}"
            logo_html = f'<img src="{logo_url}" alt="{nazov_firmy}" style="max-height: 55px; margin-bottom: 15px;">'

        # Ak je to len DRAFT (send_notif=False), tu funkciu bezpečne ukončíme!
        if not send_notif:
            return

        # =======================
        # 1. E-MAIL PRE ZÁKAZNÍKA
        # =======================
        # Dynamické gramatické tvary podľa typu dokladu (skloňovanie)
        tvary = {
            'nom': "Faktúra",
            'gen': "faktúry",
            'aku': "faktúru",
            'novy_nom': "Nová faktúra",
            'novy_aku': "novú faktúru",
            'novy_nom_cap': "NOVÁ FAKTÚRA",
            'vystavena_nom': "Vystavená faktúra",
            'vystavena_nom_cap': "NOVÁ FAKTÚRA VYSTAVENÁ",
            'elektronicky_aku': "elektronickú faktúru",
            'bola_vygenerovana': "bola vygenerovaná a zaevidovaná"
        }
        if faktura.typ_dokladu == 'DOBROPIS':
            tvary = {
                'nom': "Dobropis",
                'gen': "dobropisu",
                'aku': "dobropis",
                'novy_nom': "Nový dobropis",
                'novy_aku': "nový dobropis",
                'novy_nom_cap': "NOVÝ DOBROPIS",
                'vystavena_nom': "Vystavený dobropis",
                'vystavena_nom_cap': "NOVÝ DOBROPIS VYSTAVENÝ",
                'elektronicky_aku': "elektronický dobropis",
                'bola_vygenerovana': "bol vygenerovaný a zaevidovaný"
            }
        elif faktura.typ_dokladu == 'ZALOHA':
            tvary = {
                'nom': "Zálohová faktúra",
                'gen': "zálohovej faktúry",
                'aku': "zálohovú faktúru",
                'novy_nom': "Nová zálohová faktúra",
                'novy_aku': "novú zálohovú faktúru",
                'novy_nom_cap': "NOVÁ ZÁLOHOVÁ FAKTÚRA",
                'vystavena_nom': "Vystavená zálohová faktúra",
                'vystavena_nom_cap': "NOVÁ ZÁLOHOVÁ FAKTÚRA VYSTAVENÁ",
                'elektronicky_aku': "elektronickú zálohovú faktúru",
                'bola_vygenerovana': "bola vygenerovaná a zaevidovaná"
            }

        faktura_mena = getattr(faktura, 'mena', '') or get_firemna_mena()

        if faktura.zakaznik and faktura.zakaznik.email and pdf_data and gn and gn.smtp_user:
            splatnost_str = faktura.datum_splatnosti.strftime('%d.%m.%Y') if faktura.datum_splatnosti else "Podľa zmluvy"
            
            # Dynamické oslovenie podľa databázy
            oslovenie = faktura.zakaznik.get_email_greeting()

            tlacidlo = None
            if identita and identita.web:
                tlacidlo = {'text': 'Navštíviť našu stránku', 'url': identita.web}
            
            # Príprava prác a objednávok pre e-mail
            typy_prac_zoznam = [t.nazov for t in faktura.typ_servisu.all()]
            typy_prac_html = "\n".join([f"🔧 {t}" for t in typy_prac_zoznam]) if typy_prac_zoznam else "Nešpecifikované / Všeobecný servis"
            objednavky_html = faktura.cislo_objednavky_klienta if faktura.cislo_objednavky_klienta else "Žiadne"

            hlavny_text = f"{oslovenie}<br><br>v prílohe tohto e-mailu Vám zasielame vizuálnu kópiu pre {tvary['elektronicky_aku']} vo formáte PDF."
            
            # 🇪🇺 MEDZINÁRODNÁ eFAKTÚRA (EÚ Štandard EN 16931): Upozornenie na originál
            if faktura.typ_dokladu != 'ZALOHA':
                hlavny_text += "<br><br><b>Elektronická faktúra (EÚ Štandard):</b> Originál tohto daňového dokladu v štruktúrovanom formáte bol zaevidovaný prostredníctvom elektronickej siete (Peppol). Priložené PDF slúži ako čitateľná vizuálna kópia."

            builder = (ISASNotificationBuilder(nadpis=tvary['nom'].upper(), ikona='🧾', farba_temy='#2563eb')
                       .pridaj_riadok('Číslo dokladu:', faktura.cislo_faktury)
                       .pridaj_riadok('Týka sa zariadení:', zariadenie_text_html)
                       .pridaj_riadok('Vykonané práce a úkony:', typy_prac_html)
                       .pridaj_riadok('Súvisiace s objednávkami:', objednavky_html)
                       .pridaj_riadok('Dátum splatnosti:', splatnost_str)
                       .pridaj_riadok('IBAN:', faktura.dodavatel_iban or (identita.iban if identita and identita.iban else '-'))
                       .pridaj_riadok('Variabilný symbol:', faktura.variabilny_symbol or '-')
                       .pridaj_riadok('Vystavil:', bezpecne_meno_vystavovatela)
                       .pridaj_riadok('Suma na úhradu:', f"{faktura.celkom_s_dph} {faktura_mena}")
                       .nastav_hlavny_text(hlavny_text))
            if tlacidlo:
                builder.nastav_tlacidlo(tlacidlo['text'], tlacidlo['url'])

            try:
                ISASNotifier.send(
                    payload=builder.build(),
                    to_email=faktura.zakaznik.email,
                    send_tg=False,
                    send_mail=True,
                    send_discord=False,
                    attachments=[{
                        'filename': file_name,
                        'content': pdf_data,
                        'mimetype': 'application/pdf'
                    }]
                )
                
                # Ak sa e-mail úspešne odošle a faktúra bola len nachystaná, prepneme ju na odoslanú
                VystavenaFaktura.objects.filter(
                    id=faktura_id, 
                    stav__in=['DRAFT', 'NACHYSTANA']
                ).update(stav='ODOSLANA')
                faktura.stav = 'ODOSLANA'  # 🛡️ ISAS FIX: Synchronizácia in-memory inštancie pre nasledujúce podmienky
                    
            except Exception as e:
                logger.error(f"Chyba pri odosielaní {tvary['gen']} zákazníkovi ({faktura.cislo_faktury}): {e}")
                # 🛡️ ISAS ARCHITECT FIX: Pri zlyhaní doručenia e-mailu zákazníkovi vrátime stav na NACHYSTANÁ pre možnosť opakovania
                VystavenaFaktura.objects.filter(id=faktura_id, stav='ODOSLANA').update(stav='NACHYSTANA')
                faktura.stav = 'NACHYSTANA'

        # ========================
        # 2. E-MAIL PRE ÚČTOVNÍČKU
        # ========================
        if identita and identita.email_uctovnicky and pdf_data and gn and gn.smtp_user:
            objednavky_ucto = faktura.cislo_objednavky_klienta if faktura.cislo_objednavky_klienta else "Žiadne"
            faktura_url_ucto = f"{base_url}/admin/Evidencia/vystavenafaktura/{faktura.id}/change/"
            
            payload = (ISASNotificationBuilder(nadpis=f"{tvary['novy_nom_cap']} DO EVIDENCIE", ikona='🗂️', farba_temy='#3b82f6')
                       .pridaj_riadok('Číslo dokladu:', faktura.cislo_faktury)
                       .pridaj_riadok('Odberateľ:', faktura.odberatel_nazov)
                       .pridaj_riadok('Súvisiace s objednávkami:', objednavky_ucto)
                       .pridaj_riadok('Suma s DPH:', f"{faktura.celkom_s_dph} {faktura_mena}")
                       .pridaj_riadok('Vystavil:', bezpecne_meno_vystavovatela)
                       .nastav_hlavny_text(f"Dobrý deň,<br>systém práve vystavil a odoslal {tvary['novy_aku']}. V prílohe nájdete PDF kópiu pre Vašu účtovnú evidenciu.")
                       .nastav_tlacidlo(f"Otvoriť {tvary['aku']} v systéme ISAS CORE", faktura_url_ucto)
                       .build())
            
            try:
                ISASNotifier.send(
                    payload=payload,
                    to_email=identita.email_uctovnicky,
                    send_tg=False,
                    send_mail=True,
                    send_discord=False,
                    attachments=[{
                        'filename': file_name,
                        'content': pdf_data,
                        'mimetype': 'application/pdf'
                    }]
                )
            except Exception as ucto_mail_err:
                logger.error(f"⚠️ Nepodarilo sa odoslať kópiu faktúry {faktura.cislo_faktury} účtovníčke: {ucto_mail_err}")

        faktura_url = f"{base_url}/admin/Evidencia/vystavenafaktura/{faktura.id}/change/"

        if faktura.notif_majitel_tg or faktura.notif_majitel_mail or getattr(faktura, 'notif_majitel_discord', True):
            splatnost_majitel_str = faktura.datum_splatnosti.strftime('%d.%m.%Y') if faktura.datum_splatnosti else "Neznámy"
            
            payload_majitel = (ISASNotificationBuilder(nadpis=tvary['vystavena_nom_cap'], ikona='🧾', farba_temy='#3b82f6')
                               .pridaj_riadok('🏢 Odberateľ:', faktura.odberatel_nazov)
                               .pridaj_riadok('🔢 Číslo dokladu:', faktura.cislo_faktury)
                               .pridaj_riadok('💰 Suma na úhradu:', f"{faktura.celkom_s_dph} {faktura_mena}")
                               .pridaj_riadok('⏳ Dátum splatnosti:', splatnost_majitel_str)
                               .pridaj_riadok('👤 Vystavil:', bezpecne_meno_vystavovatela)
                               .nastav_tlacidlo(f"Otvoriť {tvary['aku']} v systéme ISAS CORE", faktura_url)
                               .nastav_ai_poznamku(f"{tvary['nom']} {tvary['bola_vygenerovana']}.")
                               .build())
            
            attachments = []
            if pdf_data:
                attachments.append({
                    'filename': file_name,
                    'content': pdf_data,
                    'mimetype': 'application/pdf'
                })

            ISASNotifier.send(
                payload=payload_majitel,
                send_tg=faktura.notif_majitel_tg,
                send_mail=faktura.notif_majitel_mail,
                send_discord=getattr(faktura, 'notif_majitel_discord', True),
                attachments=attachments
            )
        
    # 🇪🇺 MEDZINÁRODNÁ eFAKTÚRA: Asynchrónne odoslanie do siete Peppol pre celú EÚ
    if faktura and faktura.stav in ['NACHYSTANA', 'ODOSLANA'] and faktura.typ_dokladu != 'ZALOHA':
        
        je_b2b = bool(faktura.odberatel_ico or faktura.odberatel_dic or faktura.odberatel_ic_dph)
        
        # 💡 ISAS ARCHITECT: Dynamický "Feature Toggle" pre e-invoicing podľa poskytovateľa
        provider = getattr(gn, 'efaktura_provider', 'DIRECT_AP') or 'DIRECT_AP'
        if provider == 'BROKER_STORECOVE':
            peppol_zapnuty = bool(gn and (gn.peppol_client_secret or gn.peppol_client_id) and gn.peppol_participant_id)
        else:
            peppol_zapnuty = bool(gn and gn.peppol_api_url and gn.peppol_client_id and gn.peppol_client_secret and gn.peppol_participant_id)
        
        if je_b2b and peppol_zapnuty:
            if faktura.peppol_status != 'SENT':
                faktura.peppol_status = 'PROCESSING'
                faktura.peppol_error_log = None
                faktura.save(update_fields=['peppol_status', 'peppol_error_log'])
                odosli_efakturu_peppol_task.delay(faktura.id)
        else:
            if not peppol_zapnuty:
                dovod = "Služba eFaktúra (Peppol) nie je v systéme nakonfigurovaná."
            else:
                dovod = "B2C odberateľ (Koncový spotrebiteľ bez IČO/DIČ)"
                
            VystavenaFaktura.objects.filter(id=faktura.id).update(
                peppol_status='NEW',
                peppol_error_log=f"ℹ️ Výnimka: Faktúra nebola odoslaná do siete Peppol. Dôvod: {dovod}"
            ) 

@shared_task(base=ISASAutoHealTask, bind=True, max_retries=3)
def odosli_efakturu_peppol_task(self, faktura_id):
    """
    Elitný task pre odoslanie eFaktúry do siete Peppol cez SAPI-SK API.
    Využíva unifikovaný ISAS Guard pre distribuované zamykanie a auto-recovery.
    """
    lock_id = f"lock_peppol_send_{faktura_id}"
    VystavenaFaktura = apps.get_model('Evidencia', 'VystavenaFaktura')

    with isas_task_execution_guard(lock_id, timeout=180) as (acquired, gn, identita):
        if not acquired:
            if self.request.retries > 0:
                raise self.retry(countdown=30, exc=Exception("ISAS zámok sa nepodarilo získať pri opakovanom pokuse."))
            return "Faktúra sa práve odosiela iným procesom (aktívny ISAS zámok)."

        provider = getattr(gn, 'efaktura_provider', 'DIRECT_AP') or 'DIRECT_AP'

        with transaction.atomic():
            faktura = VystavenaFaktura.objects.select_for_update().filter(id=faktura_id).first()
            if not faktura:
                logger.error(f"❌ Vystavená faktúra ID {faktura_id} neexistuje pri odosielaní do siete Peppol.")
                return f"Faktúra ID {faktura_id} neexistuje."

            if faktura.peppol_status == 'SENT':
                return "Faktúra už bola do siete eFaktúra úspešne odoslaná."

            # 1. Kontrola konfigurácie podľa typu poskytovateľa
            if provider == 'BROKER_STORECOVE':
                api_key = str(gn.peppol_client_secret or gn.peppol_client_id or '').strip()
                if not api_key or not gn.peppol_participant_id:
                    err = "Chýba API kľúč alebo Participant ID pre Storecove brokera."
                    logger.warning(f"⚠️ eFaktúra {faktura.cislo_faktury} neodoslaná: {err}")
                    faktura.peppol_status = 'ERROR'
                    faktura.peppol_error_log = err
                    faktura.save(update_fields=['peppol_status', 'peppol_error_log'])
                    return err
                base_url = (gn.peppol_api_url or 'https://api.storecove.com').rstrip('/')
            else:
                if not gn or not gn.peppol_api_url or not gn.peppol_client_id or not gn.peppol_client_secret or not gn.peppol_participant_id:
                    err = "Chýba konfigurácia Peppol Access Pointu (URL, Client ID, Client Secret alebo Participant ID)."
                    logger.warning(f"⚠️ eFaktúra {faktura.cislo_faktury} neodoslaná: {err}")
                    faktura.peppol_status = 'ERROR'
                    faktura.peppol_error_log = err
                    faktura.save(update_fields=['peppol_status', 'peppol_error_log'])
                    return err
                base_url = gn.peppol_api_url.rstrip('/')

            # 2. Zákonné výnimky (B2C, Zálohy)
            je_b2b = bool(faktura.odberatel_ico or faktura.odberatel_dic or faktura.odberatel_ic_dph)

            if faktura.typ_dokladu == 'ZALOHA' or not je_b2b:
                dovod = "Zálohová faktúra (Nedaňový doklad)" if faktura.typ_dokladu == 'ZALOHA' else "B2C odberateľ (Koncový spotrebiteľ)"
                faktura.peppol_status = 'ERROR'
                faktura.peppol_error_log = f"ℹ️ eFaktúra export nie je podporovaný pre: {dovod}."
                faktura.save(update_fields=['peppol_status', 'peppol_error_log'])
                return f"Odmietnutie odoslania: {dovod}"

            # 3. 🏛️ DYNAMICKÁ KONTROLA REŽIMU PODĽA ŠTÁTU (Zero Hardcode cez StatLokalizacia)
            StatLokalizaciaModel = apps.get_model('ISASaplikacia', 'StatLokalizacia')
            lokalizacia = StatLokalizaciaModel.get_for_iso(faktura.odberatel_stat) if StatLokalizaciaModel else None
            rezim_statu = getattr(lokalizacia, 'efaktura_rezim', 'PEPPOL_DIRECT') or 'PEPPOL_DIRECT'

            if rezim_statu == 'VYPNUTE':
                faktura.peppol_status = 'NEW'
                faktura.peppol_error_log = "ℹ️ Štát odberateľa nepodporuje elektronickú výmenu daňových dokladov."
                faktura.save(update_fields=['peppol_status', 'peppol_error_log'])
                return faktura.peppol_error_log

            if provider == 'DIRECT_AP' and rezim_statu == 'CTC_CLEARANCE':
                nazov_statu = getattr(lokalizacia, 'nazov', None) or (faktura.odberatel_stat or 'zahraničie')
                faktura.peppol_status = 'NEW'
                faktura.peppol_error_log = f"ℹ️ Štát {nazov_statu} vyžaduje národný Clearance portál (CTC). Pre odoslanie aktivujte v nastaveniach brokera."
                faktura.save(update_fields=['peppol_status', 'peppol_error_log'])
                return faktura.peppol_error_log

            faktura.peppol_status = 'PROCESSING'
            faktura.peppol_error_log = None
            faktura.save(update_fields=['peppol_status', 'peppol_error_log'])

        # 4. Hybridná autentifikácia (OAuth 2.0 vs Statický Broker Bearer Key)
        if provider == 'BROKER_STORECOVE':
            access_token = api_key
        else:
            cache_key = 'peppol_access_token'
            access_token = cache.get(cache_key)

            if not access_token:
                auth_url = f"{base_url}/auth/token"
                auth_data = {
                    'grant_type': 'client_credentials',
                    'client_id': gn.peppol_client_id,
                    'client_secret': gn.peppol_client_secret,
                    'scope': 'document:send document:receive'
                }
                
                verify_ssl = not (getattr(settings, 'DEBUG', False) and any(loc in base_url for loc in ['localhost', '127.0.0.1', 'testserver']))
                try:
                    # 🛡️ ISAS ARCHITECT: Modelom riadená voľba autentifikačného protokolu (Nula hardkódu v URL)
                    # Rešpektuje voľbu v GlobalneNastavenia (DIRECT_SAPI_SK) a národnú EAS schému (0245: SK)
                    je_sapi_protokol = (
                        provider in ['DIRECT_SAPI_SK', 'SAPI_SK'] or 
                        str(getattr(gn, 'peppol_participant_id', '') or '').strip().startswith('0245:') or
                        'sapi' in base_url.lower()
                    )
                    auth_payload = {'json': auth_data} if je_sapi_protokol else {'data': auth_data}
                    auth_response = requests.post(auth_url, **auth_payload, timeout=10, verify=verify_ssl)
                except requests.exceptions.RequestException as auth_err:
                    logger.warning(f"⚠️ Výpadok siete pri získavaní Peppol tokenu pre FA {faktura.cislo_faktury}: {auth_err}")
                    raise self.retry(exc=auth_err, countdown=30)

                if auth_response.status_code == 200:
                    token_data = auth_response.json()
                    access_token = token_data.get('access_token')
                    if not access_token:
                        faktura.peppol_status = 'ERROR'
                        faktura.peppol_error_log = "Peppol API vrátilo 200 OK, ale chýba 'access_token' v odpovedi."
                        faktura.save(update_fields=['peppol_status', 'peppol_error_log'])
                        return faktura.peppol_error_log
                    cache.set(cache_key, access_token, timeout=3000)
                else:
                    error_msg = f"Zlyhala autentifikácia na Access Point: HTTP {auth_response.status_code} - {auth_response.text}"
                    faktura.peppol_status = 'ERROR'
                    faktura.peppol_error_log = error_msg[:2000]
                    faktura.save(update_fields=['peppol_status', 'peppol_error_log'])
                    return error_msg

        # 3. Načítanie skontrolovaného XML z disku alebo jeho vygenerovanie (ak chýba)
        xml_bytes = None
        if faktura.peppol_xml_dokument and faktura.peppol_xml_dokument.name:
            try:
                if default_storage.exists(faktura.peppol_xml_dokument.name):
                    with default_storage.open(faktura.peppol_xml_dokument.name, 'rb') as f:
                        xml_bytes = f.read()
            except Exception as read_err:
                logger.warning(f"⚠️ Nepodarilo sa načítať existujúci XML súbor faktúry {faktura.cislo_faktury}: {read_err}")
                xml_bytes = None

        if not xml_bytes:
            try:
                generator = PeppolXMLGenerator()
                xml_data = generator.vygeneruj_peppol_xml(faktura)
                xml_bytes = xml_data if isinstance(xml_data, bytes) else xml_data.encode('utf-8')
                faktura.peppol_xml_hash = hashlib.sha256(xml_bytes).hexdigest()
                file_name = f"peppol_{faktura.cislo_faktury}.xml"
                faktura.peppol_xml_dokument.save(name=file_name, content=ContentFile(xml_bytes), save=False)
                faktura.save(update_fields=['peppol_xml_dokument', 'peppol_xml_hash'])
            except Exception as xml_err:
                err_msg = f"Kritická chyba pri generovaní Peppol XML: {str(xml_err)}"
                logger.error(f"❌ {err_msg} pre FA {faktura.cislo_faktury}")
                faktura.peppol_status = 'ERROR'
                faktura.peppol_error_log = err_msg[:2000]
                faktura.save(update_fields=['peppol_status', 'peppol_error_log'])
                return err_msg

        xml_data = xml_bytes
        aktualny_sha256 = hashlib.sha256(xml_bytes).hexdigest()
        if faktura.peppol_xml_hash != aktualny_sha256:
            faktura.peppol_xml_hash = aktualny_sha256
            faktura.save(update_fields=['peppol_xml_hash'])

        # 5. Určenie cieľového odberateľa cez EAS register
        generator_instance = PeppolXMLGenerator()
        eas_scheme, eas_id = generator_instance._get_eas_endpoint(faktura.odberatel_ico, faktura.odberatel_dic, faktura.odberatel_ic_dph, faktura.odberatel_stat)
        
        if not eas_scheme or not eas_id:
            error_msg = f"Odberateľ {faktura.odberatel_nazov} nemá platné daňové ani registračné číslo pre Peppol EAS identifikáciu."
            faktura.peppol_status = 'ERROR'
            faktura.peppol_error_log = error_msg
            faktura.save(update_fields=['peppol_status', 'peppol_error_log'])
            return f"❌ {error_msg}"
            
        receiver_participant_id = f"{eas_scheme}:{eas_id}"
        
        try:
            idem_key = str(uuid.UUID(str(faktura.bezpecnostny_kluc)))
        except (ValueError, TypeError):
            idem_key = str(uuid.uuid5(uuid.NAMESPACE_OID, f"faktura_peppol_{faktura.id}"))

        xml_string = xml_data.decode('utf-8') if isinstance(xml_data, bytes) else str(xml_data)

        # 6. Pluggable Transport: Adaptívne zostavenie Endpointu, Hlavičiek a Payloadu (Storecove v2 API Spec)
        if provider == 'BROKER_STORECOVE':
            send_url = f"{base_url}/api/v2/document_submissions"
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json',
                'Accept': 'application/json'
            }
            storecove_doc_type = "ubl_credit_note" if faktura.typ_dokladu == 'DOBROPIS' else "ubl_invoice"
            peppol_payload = {
                "document": base64.b64encode(xml_string.encode('utf-8')).decode('utf-8'),
                "document_type": storecove_doc_type,
                "idempotency_key": idem_key
            }
        else:
            send_url = f"{base_url}/document/send"
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'X-Peppol-Participant-Id': str(gn.peppol_participant_id),
                'Idempotency-Key': idem_key 
            }
            
            if faktura.typ_dokladu == 'DOBROPIS':
                doc_type_id = "urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2::CreditNote##urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:poacc:billing:3.0::2.1"
            else:
                doc_type_id = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2::Invoice##urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:poacc:billing:3.0::2.1"

            peppol_payload = {
                "metadata": {
                    "documentId": str(faktura.cislo_faktury),
                    "documentTypeId": doc_type_id,
                    "processId": "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0",
                    "senderParticipantId": str(gn.peppol_participant_id),
                    "receiverParticipantId": receiver_participant_id,
                    "creationDateTime": timezone.now().strftime("%Y-%m-%dT%H:%M:%SZ")
                },
                "payload": xml_string,
                "payloadFormat": "XML",
                "checksum": faktura.peppol_xml_hash
            }

        verify_ssl = not (getattr(settings, 'DEBUG', False) and any(loc in base_url for loc in ['localhost', '127.0.0.1', 'testserver']))
        
        try:
            response = requests.post(send_url, json=peppol_payload, headers=headers, timeout=15, verify=verify_ssl)

            # 7. Univerzálne spracovanie odpovede (OpenPeppol AP vs Broker)
            if response.status_code in [200, 201, 202]:
                response_data = response.json() if response.text else {}
                faktura.peppol_status = 'SENT'
                # Bezpečný extrakt transakčného ID: Storecove (guid/id) vs OpenPeppol AP (providerDocumentId)
                faktura.peppol_transaction_id = str(
                    response_data.get('guid') or 
                    response_data.get('providerDocumentId') or 
                    response_data.get('id') or 
                    'SENT_OK'
                )
                faktura.peppol_odoslane_dna = timezone.now()
                
                update_polia = ['peppol_status', 'peppol_transaction_id', 'peppol_odoslane_dna']
                if faktura.stav == 'NACHYSTANA':
                    faktura.stav = 'ODOSLANA'
                    update_polia.append('stav')

                faktura.save(update_fields=update_polia)
                return f"✅ eFaktúra {faktura.cislo_faktury} úspešne odoslaná."
                
            elif response.status_code == 401:
                if provider == 'DIRECT_AP':
                    cache.delete('peppol_access_token')
                raise requests.exceptions.RequestException(f"eFaktúra Provider ({provider}) vrátil 401 Unauthorized. Vynucujem Celery Retry.")
                
            else:
                # 8. Univerzálne parsovanie chybovej obálky (OpenPeppol Error Envelope vs Storecove Errors list)
                try:
                    error_json = response.json()
                    if 'error' in error_json:
                        peppol_err = error_json['error']
                        error_msg = f"[{peppol_err.get('code', 'ERR')}] {peppol_err.get('message', 'Neznáma chyba')}"
                        if 'details' in peppol_err and peppol_err['details']:
                            error_msg += f" | Detaily: {peppol_err['details']}"
                    elif 'errors' in error_json and isinstance(error_json['errors'], list):
                        err_items = [e.get('message') or str(e) for e in error_json['errors'] if isinstance(e, dict)]
                        error_msg = " | ".join(err_items) or str(error_json)
                    elif 'message' in error_json:
                        error_msg = str(error_json['message'])
                    else:
                        error_msg = str(error_json)
                except Exception:
                    error_msg = f"HTTP {response.status_code}: {response.text}"
                    
                faktura.peppol_status = 'ERROR'
                faktura.peppol_error_log = error_msg[:2000]
                faktura.save(update_fields=['peppol_status', 'peppol_error_log'])
                logger.error(f"❌ Peppol chyba pre FA {faktura.cislo_faktury}: {error_msg}")
                
                # ================================================
                # 🚨 ZÁKONNÁ POVINNOSŤ: AKTÍVNY ALARM PRI ZLYHANÍ
                # ================================================
                if gn and (gn.telegram_bot_token or gn.email_majitel_notifikacie or getattr(gn, 'discord_bot_token', None)):
                    payload_alarm = (ISASNotificationBuilder(nadpis='ZLYHANIE eFAKTÚRY (PEPPOL)', ikona='🚨', farba_temy='#ef4444')
                                     .pridaj_riadok('🏢 Odberateľ:', faktura.odberatel_nazov)
                                     .pridaj_riadok('🔢 Číslo dokladu:', faktura.cislo_faktury)
                                     .pridaj_riadok('❌ Dôvod zlyhania:', error_msg)
                                     .nastav_upozornenie("<strong>⚠️ Zlyhanie doručenia do siete Peppol</strong><br>Prístupový bod (Access Point) odmietol prijať túto faktúru. Faktúra sa považuje za nedoručenú prostredníctvom siete eFaktúra. Skontrolujte registráciu odberateľa v Peppol Directory alebo správnosť identifikačných čísiel (IČO/DIČ/VAT).", 'cervene')
                                     .nastav_ai_poznamku('Prihláste sa do ISAS Command Center a skontrolujte chybový log faktúry.')
                                     .build())
                    
                    ISASNotifier.send(
                        payload=payload_alarm,
                        send_tg=bool(gn.telegram_bot_token and gn.telegram_id_majitel),
                        send_mail=bool(gn.email_majitel_notifikacie and gn.smtp_user),
                        send_discord=bool(getattr(gn, 'discord_bot_token', None))
                    )

                return f"❌ Chyba pri odosielaní: {error_msg}"

        except requests.exceptions.RequestException as e:
            logger.warning(f"⚠️ Sieťová chyba pri Peppol exporte FA {faktura_id}. Pokus {self.request.retries + 1}/3. ({e})")
            try:
                raise self.retry(exc=e, countdown=60)
            except self.MaxRetriesExceededError:
                # 🛡️ ISAS ARCHITECT FIX: Stav ERROR zapíšeme len vtedy, keď Celery skutočne vyčerpá max_retries
                err_text = f"Vyčerpané všetky pokusy o pripojenie k Peppol Access Pointu: {str(e)}"
                VystavenaFaktura.objects.filter(id=faktura_id).update(
                    peppol_status='ERROR',
                    peppol_error_log=err_text[:2000]
                )
                logger.error(f"❌ Peppol definitívne zlyhal po 3 pokusoch pre FA {faktura_id}: {err_text}")
                return f"❌ Definitívne zlyhanie siete: {err_text}"
            
        except Exception as e:
            logger.error(f"❌ Kritická chyba v Peppol tasku pre FA {faktura_id}: {str(e)}")
            VystavenaFaktura.objects.filter(id=faktura_id).update(
                peppol_status='ERROR', 
                peppol_error_log=str(e)[:2000]
            )
            return f"Kritická chyba: {str(e)}"

@shared_task(base=ISASAutoHealTask)
def vygeneruj_vstupku_ai_task(povolenie_id: int):
    lock_id = f"lock_ai_vstupka_{povolenie_id}"
    
    with isas_task_execution_guard(lock_id, timeout=300) as (acquired, gn, identita):
        if not acquired:
            logger.warning(f"Task zamietnutý: Zámok pre povolenie {povolenie_id} je už aktívny.")
            return
        
        PovolenieNaVstup = apps.get_model('Evidencia', 'PovolenieNaVstup')

        with transaction.atomic():
            # 🛡️ DEFENSIVE: of=('self',) chráni Postgres a odstránená @property 'vytvoril_zamestnanec' bráni pádu na FieldError
            try:
                povolenie = PovolenieNaVstup.objects.select_for_update(of=('self',)).select_related(
                    'zakaznik', 'uloha'
                ).prefetch_related(
                    'zamestnanci', 'vozidla', 'uloha__typ_servisu', 'uloha__zariadenia'
                ).get(id=povolenie_id)
            except PovolenieNaVstup.DoesNotExist:
                logger.warning(f"🛑 Generovanie zrušené: Povolenie ID {povolenie_id} už v DB neexistuje.")
                return

            # Idempotencia tasku
            if povolenie.stav in ['ODOSLANE', 'SCHVALENE', 'ZAMIETNUTE']:
                logger.warning(f"Task zrušený: Povolenie {povolenie_id} už je v stave {povolenie.stav}.")
                return
            
        zakaznik = povolenie.zakaznik
        
        if not zakaznik:
            return
            
        identita = FiremnaIdentita.objects.first()
        gn = GlobalneNastavenia.objects.first()

        # Vytiahnutie dynamických dát pre maximálny kontext
        nasa_firma = identita.nazov_spolocnosti if identita and identita.nazov_spolocnosti else "Naša spoločnosť"
        nase_ico = identita.ico if identita and identita.ico else "Nezadané"
        # Vytiahnutie konkrétneho autora pre AI kontext
        vytvoril_zam = povolenie.vytvoril_zamestnanec
        vystavil_meno = f"{vytvoril_zam.meno} {vytvoril_zam.priezvisko}" if vytvoril_zam else (identita.vystavuje_vstupky if identita and identita.vystavuje_vstupky else "Dispečing")
        nas_telefon = vytvoril_zam.telefon if (vytvoril_zam and vytvoril_zam.telefon) else (identita.telefon if identita and identita.telefon else "Nezadané")
        nas_email = vytvoril_zam.email if (vytvoril_zam and vytvoril_zam.email) else (identita.email_vseobecny if identita and identita.email_vseobecny else "Nezadané")
        nas_web = identita.web if identita and identita.web else "Nezadané"
        nase_mesto = identita.sidlo_mesto if identita and identita.sidlo_mesto else "Nezadané"
        
        ucel_vstupu = "Servisný zásah / Údržba"
        cas_prichodu = "Nešpecifikovaný"
        predpokladany_odchod = "Nešpecifikovaný"
        
        if povolenie.uloha:
            typy_prac = ", ".join([str(t.nazov) for t in povolenie.uloha.typ_servisu.all() if t.nazov]) or "Servisný zásah"
            zariadenia = ", ".join([str(getattr(z, 'nazov', None) or getattr(z, 'model_zariadenia', None) or 'Špecifikované na mieste') for z in povolenie.uloha.zariadenia.all()])
            ucel_vstupu = f"Výkon prác: {typy_prac} na zariadeniach: {zariadenia or 'Neurčené'}"
            
            # Kompletný dátum a čas pre viacdňové výjazdy
            if povolenie.uloha.zaciatok:
                local_zaciatok = timezone.localtime(povolenie.uloha.zaciatok) if timezone.is_aware(povolenie.uloha.zaciatok) else povolenie.uloha.zaciatok
                cas_prichodu = local_zaciatok.strftime('%d.%m.%Y %H:%M')
            if povolenie.uloha.koniec:
                local_koniec = timezone.localtime(povolenie.uloha.koniec) if timezone.is_aware(povolenie.uloha.koniec) else povolenie.uloha.koniec
                predpokladany_odchod = local_koniec.strftime('%d.%m.%Y %H:%M')

        interny_kontakt = "Nezadaný"
        if zakaznik.kontaktna_osoba:
            interny_kontakt = f"{zakaznik.kontaktna_osoba}"
            if zakaznik.telefon:
                interny_kontakt += f" ({zakaznik.telefon})"

        # Fyzická kontrola existencie súborov na disku/S3
        zoznam_technikov_data = []
        for z in povolenie.zamestnanci.all():
            prilohy = []
            dnes_vstup = povolenie.datum_vstupu
            platne_cert = z.certifikaty.filter(Q(je_dozivotny=True) | Q(datum_platnosti__gte=dnes_vstup))
            if z.bozp_subor and z.bozp_subor.name and default_storage.exists(z.bozp_subor.name): prilohy.append("BOZP")
            if platne_cert.filter(subor__isnull=False).exclude(subor='').exists(): prilohy.append("Certifikáty")
            if z.lekarska_prehliadka_subor and z.lekarska_prehliadka_subor.name and default_storage.exists(z.lekarska_prehliadka_subor.name): prilohy.append("Lekárska prehliadka")
            
            meno_cele = f"{z.meno or ''} {z.priezvisko or ''}".strip()
            zoznam_technikov_data.append({
                "meno_a_priezvisko": meno_cele if meno_cele else "Nezadané",
                "datum_narodenia": z.datum_narodenia.strftime('%d.%m.%Y') if z.datum_narodenia else "Nezadané",
                "pracovna_rola": z.get_pracovna_rola_display() if z.pracovna_rola else "Technik",
                "statna_prislusnost": z.stat or "Nezadané",
                "cislo_dokladu_totoznosti": z.cislo_op or "Nezadané",
                "telefonny_kontakt": z.telefon or "Nezadané",
                "odborna_sposobilost": ", ".join([c.nazov for c in platne_cert]) or "Bez špec. osvedčenia",
                "platnost_bozp_do": z.bozp_platne_do.strftime('%d.%m.%Y') if z.bozp_platne_do else "Nezadané",
                "prilozene_certifikaty_v_maile": prilohy if prilohy else "Žiadne"
            })

        data_kontext = {
            "nasa_spolocnost": {
                "nazov": nasa_firma,
                "vystavil": vystavil_meno,
                "ico": nase_ico,
                "dic": identita.dic if identita and identita.dic else "Nezadané",
                "ic_dph": identita.ic_dph if identita and identita.ic_dph else "Nezadané",
                "telefon_dispecing": nas_telefon,
                "email_dispecing": nas_email,
                "webova_stranka": nas_web,
                "sidlo_spolocnosti": f"{identita.sidlo_ulica or ''} {identita.sidlo_cislo or ''}, {identita.sidlo_psc or ''} {nase_mesto}".replace(" ,", ",").strip(", ") if identita else nase_mesto
            },
            "cielova_fabrika": {
                "nazov": zakaznik.Nazov_spolocnosti,
                "adresa": f"{zakaznik.ulica_a_cislo or ''}, {zakaznik.psc or ''} {zakaznik.mesto or ''}".strip(", "),
                "ico": zakaznik.ico or "Nezadané",
                "dic": zakaznik.dic or "Nezadané",
                "ic_dph": zakaznik.ic_dph or "Nezadané"
            },
            "interna_kontaktna_osoba_vo_fabrike": interny_kontakt,
            "ucel_vstupu": ucel_vstupu,
            "datum_vstupu": povolenie.datum_vstupu.strftime('%d.%m.%Y'),
            "predpokladany_cas_prichodu": cas_prichodu,
            "predpokladany_cas_odchodu": predpokladany_odchod,
            "zakaznik_ma_vlastnu_sablonu_vstupu": bool(getattr(zakaznik, 'sablona_vstupu', None) and getattr(zakaznik.sablona_vstupu, 'name', None)),
            "zakaznik_vyzaduje_externy_portal": zakaznik.url_vstupneho_portalu if hasattr(zakaznik, 'url_vstupneho_portalu') and zakaznik.url_vstupneho_portalu else False,
            "zoznam_technikov": zoznam_technikov_data,
            "zoznam_vozidiel": [
                {
                    "spz": getattr(v, 'spz', 'Nezadané'), 
                    "typ_a_model": getattr(v, 'znacka_a_model', 'Nezadané'),
                    "kategoria_vozidla": v.get_typ_vozidla_erp_display() if hasattr(v, 'get_typ_vozidla_erp_display') else str(getattr(v, 'typ_vozidla_erp', 'Vozidlo')),
                    "typ_paliva": v.get_typ_paliva_display() if hasattr(v, 'get_typ_paliva_display') else str(getattr(v, 'typ_paliva', 'Štandard'))
                } for v in povolenie.vozidla.all()
            ]
        }

        instrukcie_zakaznika = zakaznik.instrukcie_vstupu or "Žiadne špecifické inštrukcie."

        prompt = f"""
        Si elitný administratívny dispečer spoločnosti '{nasa_firma}'. Tvojou úlohou je vygenerovať 
        vysoko profesionálne telo e-mailu pre vrátnicu/SBS do objektu zákazníka '{zakaznik.Nazov_spolocnosti}'.
        
        VŠETKY POTREBNÉ DÁTA SÚ V TOMTO JSON OBJEKTE (NIKDY SI NEVYMÝŠĽAJ INÉ):
        {json.dumps(data_kontext, ensure_ascii=False, indent=2)}
        
        --- ZAČIATOK INŠTRUKCIÍ OD ZÁKAZNÍKA ---
        {instrukcie_zakaznika}
        --- KONIEC INŠTRUKCIÍ OD ZÁKAZNÍKA ---
        
        PRAVIDLÁ A OCHRANA PROTI HALUCINÁCIÁM (STRIKTNE DODRŽ):
        1. HIERARCHIA: Inštrukcie od zákazníka (vyššie) majú absolútnu prioritu. Ak zákazník vyžaduje špecifický formát alebo zakazuje posielať určité údaje (napr. čísla dokladov), MUSÍŠ sa tým riadiť, aj keby boli v JSONe uvedené.
        2. ZAČIATOK A KONIEC: Začni priamo oslovením (napr. "Dobrý deň," alebo "Vážená SBS,"). Žiadne úvodné frázy ako "Tu je návrh e-mailu:" alebo "Predmet:". Na konci vytvor vysoko profesionálny korporátny podpis dispečingu. Použi VÝHRADNE údaje z uzla "nasa_spolocnost" (Názov, IČO, Telefón, E-mail, Web, Sídlo). ZÁKAZ vymýšľania si akýchkoľvek iných kontaktných údajov!
        3. IGNOROVANIE PRÁZDNYCH DÁT: Ak má akýkoľvek kľúč v JSONe hodnotu "Nezadané", "Nešpecifikovaný" alebo "Žiadne", TENTO ÚDAJ V TEXTE ÚPLNE VYNECHAJ. Máš prísny zákaz použiť v texte slová ako "Nezadané", "Neuvedené", "N/A" alebo "Bez špec. osvedčenia". Ak údaj chýba, tvár sa, že táto kategória vôbec neexistuje. ZÁKAZ halucinovania čísel dokladov, mien, časov alebo ŠPZ!
        4. VOZIDLÁ A BEZPEČNOSŤ: Ak je zoznam vozidiel prázdny, vozidlá v e-maile vôbec nespomínaj. Ak vozidlo obsahuje typ paliva LPG, explicitne na to upozorni (kvôli prísnym bezpečnostným predpisom vo fabrikách).
        5. Jasne uveď účel vstupu a internú kontaktnú osobu (ak je zadaná), aby SBS vedela, za kým technici idú.
        6. NEPOUŽÍVAJ žiadny Markdown (*, **, #). Použi klasické textové odrážky s pomlčkou (-).
        7. PRÍLOHY: Na záver spomeň, že v prílohe e-mailu sa nachádza naša oficiálna elektronická priepustka v PDF. Ďalšie certifikáty (BOZP, Certifikáty atď.) spomeň v texte LEN VTEDY, ak sú pri konkrétnom technikovi reálne uvedené v kľúči "prilozene_certifikaty_v_maile". Nesľubuj prílohy, ktoré neexistujú!
        8. EXTERNÉ POŽIADAVKY FABRIKY: 
           - Ak je kľúč "zakaznik_vyzaduje_externy_portal" vyplnený (obsahuje URL), zdvorilo informuj SBS, že pracovníci boli/budú zaevidovaní aj priamo cez ich externý portál.
           - Ak je kľúč "zakaznik_ma_vlastnu_sablonu_vstupu" true, zdvorilo dodaj, že ak naša elektronická priepustka nepostačuje, sme pripravení obratom vyplniť ich špecifický interný formulár.
        9. Tón musí byť úradný, zdvorilý, jasný a maximálne stručný.
        """
        
        api_key = get_gemini_api_key()
        gemini_model = getattr(settings, 'GEMINI_MODEL', None)
        
        if not api_key or not gemini_model:
            logger.warning(f"⚠️ [vygeneruj_vstupku_ai_task] Chýba GEMINI_API_KEY alebo GEMINI_MODEL pre povolenie ID {povolenie_id}. Používam štandardný fallback text.")
            ai_text = f"Dobrý deň,\n\nohlasujeme vstup našich pracovníkov na servisný zásah v objekte {zakaznik.Nazov_spolocnosti} dňa {povolenie.datum_vstupu.strftime('%d.%m.%Y')}.\nÚčel: {ucel_vstupu}.\n\nS pozdravom,\n{nasa_firma}"
        else:
            try:
                client = genai.Client(api_key=api_key)
                response = client.models.generate_content(
                    model=gemini_model,
                    contents=prompt,
                    config=types.GenerateContentConfig(thinking_config=types.ThinkingConfig(thinking_level=getattr(settings, 'GEMINI_THINKING_LEVEL', types.ThinkingLevel.MEDIUM) or types.ThinkingLevel.MEDIUM))
                )
                ai_text = (response.text or "").replace('**', '').replace('*', '•').strip()
            except Exception as ai_err:
                logger.error(f"❌ [vygeneruj_vstupku_ai_task] Zlyhalo volanie Gemini AI: {ai_err}")
                ai_text = f"Dobrý deň,\n\nohlasujeme vstup našich pracovníkov na servisný zásah v objekte {zakaznik.Nazov_spolocnosti} dňa {povolenie.datum_vstupu.strftime('%d.%m.%Y')}.\nÚčel: {ucel_vstupu}.\n\nS pozdravom,\n{nasa_firma}"

        raw_base_url = (getattr(gn, 'clean_base_url', None) if gn else None) or getattr(settings, 'SITE_URL', '') or ''
        base_url = raw_base_url.rstrip('/')

        cesta_k_portalu = reverse('evidencia:schvalit_vstup_sbs', args=[povolenie.bezpecnostny_kluc])
        magic_link = f"{base_url}{cesta_k_portalu}"

        qr = qrcode.QRCode(version=1, box_size=10, border=0)
        qr.add_data(magic_link)
        qr.make(fit=True)
        img_qr = qr.make_image(fill_color="#0f172a", back_color="white")
        
        buffer = BytesIO()
        img_qr.save(buffer, format="PNG")
        qr_b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")

        context = {
            'povolenie': povolenie,
            'identita': identita,
            'qr_kod_b64': qr_b64,
        }
        
        # 🛡️ DEFENSIVE: Bezpečné ošetrenie názvov aj pri hodnote None
        nasa_firma_cista = re.sub(r'[^a-zA-Z0-9_]', '', str(nasa_firma or 'Firma').replace(' ', '_'))[:30] or "Firma"
        zak_nazov = getattr(povolenie.zakaznik, 'Nazov_spolocnosti', '') or "Zakaznik"
        zakaznik_cisty = re.sub(r'[^a-zA-Z0-9_]', '', str(zak_nazov).replace(' ', '_'))[:30] or "Zakaznik"
        file_name = f"Priepustka_{nasa_firma_cista}_{zakaznik_cisty}_{povolenie.datum_vstupu}.pdf"
        
        # 🗑️ OCHRANA PRED SIROTSKÝMI SÚBORMI: Zmazanie starého PDF z disku pred vygenerovaním nového
        if povolenie.vygenerovany_dokument and povolenie.vygenerovany_dokument.name:
            try:
                if default_storage.exists(povolenie.vygenerovany_dokument.name):
                    default_storage.delete(povolenie.vygenerovany_dokument.name)
            except Exception as del_err:
                logger.warning(f"⚠️ Nepodarilo sa odstrániť staré PDF priepustky: {del_err}")

        pdf_data, _ = ISASPDFBuilder.generate_and_save(
            template_name='admin/pdf_vstupka.html',
            context=context,
            base_url=base_url,
            file_name=file_name,
            instance=povolenie,
            file_field_name='vygenerovany_dokument',
            save_instance=False
        )

        # 🛡️ DVOJFÁZOVÝ WORKFLOW: Chránime stav, ak dispečer medzitým záznam vybavil alebo odoslal
        aktualny_stav = PovolenieNaVstup.objects.filter(id=povolenie.id).values_list('stav', flat=True).first()
        if not aktualny_stav:
            logger.warning(f"🛑 Generovanie zrušené: Povolenie ID {povolenie_id} bolo počas behu AI zmazané z DB.")
            return

        if aktualny_stav in ['ODOSLANE', 'SCHVALENE', 'ZAMIETNUTE']:
            logger.warning(f"🛑 Generovanie neaplikované: Povolenie ID {povolenie_id} je už v stave {aktualny_stav}.")
            return

        povolenie.ai_text_emailu = ai_text
        povolenie.stav = 'GENEROVANE'
        povolenie.save(update_fields=['ai_text_emailu', 'vygenerovany_dokument', 'stav'])
        logger.info(f"✅ [vygeneruj_vstupku_ai_task] PDF a text úspešne pripravené na kontrolu pre povolenie ID {povolenie_id}.")

@shared_task(base=ISASAutoHealTask)
def odosli_vstupku_na_vratnicu_task(povolenie_id: int):
    """
    🚀 FÁZA 2: Bezpečné asynchrónne odoslanie skontrolovaného povolenia a príloh na vrátnicu.
    Garantuje idempotenciu pomocou Redis zámku a DB uzamknutia riadku.
    """
    lock_id = f"lock_send_vstupka_{povolenie_id}"
    
    with isas_task_execution_guard(lock_id, timeout=180) as (acquired, gn, identita):
        if not acquired:
            logger.warning(f"Odoslanie zamietnuté: Zámok pre odoslanie povolenia {povolenie_id} je už aktívny.")
            return

        PovolenieNaVstup = apps.get_model('Evidencia', 'PovolenieNaVstup')

        with transaction.atomic():
            # 🛡️ DEFENSIVE: of=('self',) chráni PostgreSQL a odstránenie @property 'vytvoril_zamestnanec' bráni pádu na FieldError
            try:
                povolenie = PovolenieNaVstup.objects.select_for_update(of=('self',)).select_related(
                    'zakaznik', 'uloha'
                ).prefetch_related(
                    'zamestnanci', 'vozidla', 'uloha__typ_servisu', 'uloha__zariadenia'
                ).get(id=povolenie_id)
            except PovolenieNaVstup.DoesNotExist:
                logger.warning(f"🛑 Odoslanie zrušené: Povolenie ID {povolenie_id} už v DB neexistuje.")
                return

            # 🛡️ IDEMPOTENCIA: Ak už bol vstup odoslaný alebo vybavený, prerušíme beh
            if povolenie.stav in ['ODOSLANE', 'SCHVALENE', 'ZAMIETNUTE']:
                logger.warning(f"Odoslanie zrušené: Povolenie {povolenie_id} je už v stave {povolenie.stav}.")
                return

            # 🛡️ INTEGRITA DÁT: Overenie existencie vygenerovaného súboru na úložisku
            if not povolenie.vygenerovany_dokument or not povolenie.vygenerovany_dokument.name or not default_storage.exists(povolenie.vygenerovany_dokument.name):
                logger.error(f"🛑 Zlyhalo odoslanie povolenia {povolenie_id}: Fyzické PDF na disku neexistuje!")
                return

            zakaznik = povolenie.zakaznik
            if not zakaznik or not zakaznik.email_vratnice or not gn or not gn.smtp_user:
                logger.error(f"🛑 Zlyhalo odoslanie povolenia {povolenie_id}: Chýba e-mail vrátnice alebo konfigurácia SMTP.")
                return

            nasa_firma = identita.nazov_spolocnosti if identita and identita.nazov_spolocnosti else "Naša spoločnosť"
            vytvoril_zam = povolenie.vytvoril_zamestnanec
            vystavil_meno = f"{vytvoril_zam.meno} {vytvoril_zam.priezvisko}" if vytvoril_zam else (identita.vystavuje_vstupky if identita and identita.vystavuje_vstupky else "Dispečing")

            ucel_vstupu = "Servisný zásah / Údržba"
            cas_prichodu = "Nešpecifikovaný"
            
            if povolenie.uloha:
                typy_prac = ", ".join([str(t.nazov) for t in povolenie.uloha.typ_servisu.all() if t.nazov]) or "Servisný zásah"
                zariadenia = ", ".join([str(getattr(z, 'nazov', None) or getattr(z, 'model_zariadenia', None) or 'Špecifikované na mieste') for z in povolenie.uloha.zariadenia.all()])
                ucel_vstupu = f"Výkon prác: {typy_prac} na zariadeniach: {zariadenia or 'Neurčené'}"

                if povolenie.uloha.zaciatok:
                    local_zaciatok = timezone.localtime(povolenie.uloha.zaciatok) if timezone.is_aware(povolenie.uloha.zaciatok) else povolenie.uloha.zaciatok
                    cas_prichodu = local_zaciatok.strftime('%d.%m.%Y %H:%M')

            raw_base_url = (getattr(gn, 'clean_base_url', None) if gn else None) or getattr(settings, 'SITE_URL', '') or ''
            base_url = raw_base_url.rstrip('/')

            cesta_k_portalu = reverse('evidencia:schvalit_vstup_sbs', args=[povolenie.bezpecnostny_kluc])
            magic_link = f"{base_url}{cesta_k_portalu}"

            # 1. Príprava príloh – bezpečné čítanie cez kontextový manažér (ochrana pred resource leakom)
            try:
                with povolenie.vygenerovany_dokument.open('rb') as f:
                    pdf_content = f.read()
            except Exception as read_err:
                logger.error(f"🛑 Zlyhalo čítanie PDF súboru povolenia {povolenie_id}: {read_err}")
                return

            attachments = [{
                'filename': os.path.basename(povolenie.vygenerovany_dokument.name),
                'content': pdf_content,
                'mimetype': 'application/pdf'
            }]

            for technik in povolenie.zamestnanci.all():
                dok_mapa = {
                    'BOZP': technik.bozp_subor,
                    'Lekarska_prehliadka': technik.lekarska_prehliadka_subor
                }
                dnes_v = povolenie.datum_vstupu
                for cert in technik.certifikaty.filter(Q(je_dozivotny=True) | Q(datum_platnosti__gte=dnes_v)):
                    if cert.subor and getattr(cert.subor, 'name', None):
                        dok_mapa[f"Certifikat_{cert.id}"] = cert.subor
                for typ_doku, doc in dok_mapa.items():
                    if doc and doc.name and default_storage.exists(doc.name):
                        try:
                            with doc.open('rb') as df:
                                file_content = df.read()
                            _, ext = os.path.splitext(doc.name)
                            ciste_meno = f"{technik.priezvisko or ''}_{technik.meno or ''}".strip('_').replace(" ", "") or f"technik_{technik.id}"
                            ciste_meno_ascii = ''.join(c for c in unicodedata.normalize('NFD', ciste_meno) if unicodedata.category(c) != 'Mn')
                            # 🛡️ DEFENSIVE: Striktné obmedzenie názvu prílohy na alfanumerické znaky (RFC 2045 compliance)
                            ciste_meno_bezpecne = re.sub(r'[^a-zA-Z0-9_]', '', ciste_meno_ascii) or f"technik_{technik.id}"
                            attachments.append({
                                'filename': f"{typ_doku}_{ciste_meno_bezpecne}_ID{technik.id}{ext}",
                                'content': file_content
                            })
                        except Exception as file_e:
                            logger.warning(f"⚠️ Nepodarilo sa načítať certifikát {doc.name}: {file_e}")

            # 2. Odoslanie e-mailu cez ISASNotifier s prísnym ošetrením chýb
            ai_text_raw = povolenie.ai_text_emailu or f"Dobrý deň,\nohlasujeme vstup našich pracovníkov dňa {povolenie.datum_vstupu.strftime('%d.%m.%Y')}."
            # 🛡️ DEFENSIVE: Konverzia plain-text nových riadkov na HTML tagy <br> pre zachovanie odsekov a podpisu
            ai_text_html = escape(ai_text_raw).replace('\n', '<br>')

            payload = (ISASNotificationBuilder(nadpis="OHLÁSENIE VSTUPU DO OBJEKTU", ikona='🏢', farba_temy='#2563eb')
                       .pridaj_riadok('Cieľový objekt:', zakaznik.Nazov_spolocnosti)
                       .pridaj_riadok('Dodávateľ:', nasa_firma)
                       .pridaj_riadok('Účel vstupu:', ucel_vstupu)
                       .pridaj_riadok('Dátum vstupu:', povolenie.datum_vstupu.strftime('%d.%m.%Y'))
                       .pridaj_riadok('Čas príchodu:', cas_prichodu)
                       .pridaj_riadok('Vystavil:', vystavil_meno)
                       .nastav_hlavny_text(f"{ai_text_html}<br><br><b>Vybavenie ohlásenia (Schválenie / Zamietnutie)</b><br>Kliknutím na tlačidlo nižšie prejdete na portál, kde môžete vstup schváliť alebo zamietnuť.")
                       .nastav_tlacidlo('Otvoriť portál vstupu', magic_link)
                       .build())

            # 🛡️ OCHRANA PRED FAKTOVÝM NESÚLADOM: Stav prepneme LEN VTEDY, ak mail reálne odišiel
            try:
                ISASNotifier.send(
                    payload=payload,
                    to_email=zakaznik.email_vratnice,
                    send_tg=False,
                    send_mail=True,
                    send_discord=False,
                    attachments=attachments
                )
                povolenie.stav = 'ODOSLANE'
                povolenie.save(update_fields=['stav'])
                logger.info(f"✅ Povolenie ID {povolenie_id} bolo úspešne doručené na vrátnicu ({zakaznik.email_vratnice}).")
            except Exception as mail_err:
                logger.error(f"🚨 KRITICKÁ CHYBA: Zlyhalo odoslanie e-mailu pre povolenie {povolenie_id}: {mail_err}")
                # Stav ostáva GENEROVANE, aby dispečer vedel akciu zopakovať

@shared_task(base=ISASAutoHealTask)
def pregeneruj_a_odosli_aktualizaciu_vstupu_task(povolenie_id: int):
    """
    🔄 AKTUALIZÁCIA ODOSLANEJ PRIEPUSTKY:
    Pregeneruje PDF s novým vozidlom a odošle na vrátnicu aktualizačný e-mail s novým PDF.
    """
    lock_id = f"lock_ai_vstupka_{povolenie_id}"
    
    with isas_task_execution_guard(lock_id, timeout=180) as (acquired, gn, identita):
        if not acquired:
            logger.warning(f"Aktualizácia vstupu zamietnutá: Zámok pre povolenie {povolenie_id} je aktívny.")
            return

        PovolenieNaVstup = apps.get_model('Evidencia', 'PovolenieNaVstup')

        with transaction.atomic():
            # 🛡️ DEFENSIVE: of=('self',) chráni PostgreSQL a odstránenie @property 'vytvoril_zamestnanec' bráni pádu na FieldError
            try:
                povolenie = PovolenieNaVstup.objects.select_for_update(of=('self',)).select_related(
                    'zakaznik', 'uloha'
                ).prefetch_related(
                    'zamestnanci', 'vozidla', 'uloha__typ_servisu', 'uloha__zariadenia'
                ).get(id=povolenie_id)
            except PovolenieNaVstup.DoesNotExist:
                logger.error(f"🛑 Aktualizácia vstupu zlyhala: Povolenie ID {povolenie_id} v databáze neexistuje.")
                return

            if povolenie.stav != 'ODOSLANE':
                logger.warning(f"Aktualizácia zrušená: Povolenie {povolenie_id} nie je v stave ODOSLANE.")
                return

            # 🛡️ DEFENSIVE: Inicializácia zoznamu vozidiel s filtrom na neprázdne ŠPZ (ochrana pred TypeError)
            auta_zoznam = ", ".join([a.spz for a in povolenie.vozidla.all() if getattr(a, 'spz', None)]) or "Bez vozidla"

            raw_base_url = (getattr(gn, 'clean_base_url', None) if gn else None) or getattr(settings, 'SITE_URL', '') or ''
            base_url = raw_base_url.rstrip('/')

            cesta_k_portalu = reverse('evidencia:schvalit_vstup_sbs', args=[povolenie.bezpecnostny_kluc])
            magic_link = f"{base_url}{cesta_k_portalu}"

            # Generovanie nového QR kódu
            qr = qrcode.QRCode(version=1, box_size=10, border=0)
            qr.add_data(magic_link)
            qr.make(fit=True)
            img_qr = qr.make_image(fill_color="#0f172a", back_color="white")
            buffer = BytesIO()
            img_qr.save(buffer, format="PNG")
            qr_b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")

            context = {'povolenie': povolenie, 'identita': identita, 'qr_kod_b64': qr_b64}
            nasa_firma = identita.nazov_spolocnosti if identita and identita.nazov_spolocnosti else "Naša spoločnosť"
            nasa_firma_cista = re.sub(r'[^a-zA-Z0-9_]', '', str(nasa_firma or 'Firma').replace(' ', '_'))[:30] or "Firma"
            zak_nazov = getattr(povolenie.zakaznik, 'Nazov_spolocnosti', '') or "Zakaznik"
            zakaznik_cisty = re.sub(r'[^a-zA-Z0-9_]', '', str(zak_nazov).replace(' ', '_'))[:30] or "Zakaznik"
            file_name = f"Priepustka_{nasa_firma_cista}_{zakaznik_cisty}_{povolenie.datum_vstupu}.pdf"

            # Vymazanie starého PDF
            if povolenie.vygenerovany_dokument and povolenie.vygenerovany_dokument.name:
                try:
                    if default_storage.exists(povolenie.vygenerovany_dokument.name):
                        default_storage.delete(povolenie.vygenerovany_dokument.name)
                except Exception as del_e:
                    logger.warning(f"Nepodarilo sa zmazať staré PDF pri zmene auta: {del_e}")

            pdf_data, _ = ISASPDFBuilder.generate_and_save(
                template_name='admin/pdf_vstupka.html',
                context=context,
                base_url=base_url,
                file_name=file_name,
                instance=povolenie,
                file_field_name='vygenerovany_dokument',
                save_instance=False
            )

            povolenie.save(update_fields=['vygenerovany_dokument'])

            # Ak má zákazník e-mail vrátnice, odošleme aktualizáciu
            if povolenie.zakaznik.email_vratnice and gn and gn.smtp_user:
                attachments = [{'filename': file_name, 'content': pdf_data, 'mimetype': 'application/pdf'}]

                for technik in povolenie.zamestnanci.all():
                    dok_mapa = {'BOZP': technik.bozp_subor, 'Lekarska_prehliadka': technik.lekarska_prehliadka_subor}
                    dnes_v = povolenie.datum_vstupu
                    for cert in technik.certifikaty.filter(Q(je_dozivotny=True) | Q(datum_platnosti__gte=dnes_v)):
                        if cert.subor and getattr(cert.subor, 'name', None):
                            dok_mapa[f"Certifikat_{cert.id}"] = cert.subor
                    for typ_doku, doc in dok_mapa.items():
                        if doc and doc.name and default_storage.exists(doc.name):
                            try:
                                with doc.open('rb') as df:
                                    f_content = df.read()
                                _, ext = os.path.splitext(doc.name)
                                ciste_m_raw = f"{technik.priezvisko or ''}_{technik.meno or ''}".strip('_').replace(" ", "") or f"technik_{technik.id}"
                                ciste_m = ''.join(c for c in unicodedata.normalize('NFD', ciste_m_raw) if unicodedata.category(c) != 'Mn')
                                attachments.append({'filename': f"{typ_doku}_{ciste_m}_ID{technik.id}{ext}", 'content': f_content})
                            except Exception as fe:
                                logger.warning(f"Chyba prílohy certifikátu: {fe}")

                payload = (ISASNotificationBuilder(nadpis="AKTUALIZÁCIA VOZIDLA: OHLÁSENIE VSTUPU", ikona='🚗', farba_temy='#f59e0b')
                           .pridaj_riadok('Cieľový objekt:', povolenie.zakaznik.Nazov_spolocnosti)
                           .pridaj_riadok('Dodávateľ:', nasa_firma)
                           .pridaj_riadok('Nové vozidlá výjazdu:', auta_zoznam)
                           .pridaj_riadok('Dátum vstupu:', povolenie.datum_vstupu.strftime('%d.%m.%Y'))
                           .nastav_hlavny_text(f"Dobrý deň,<br><br>pri servisnom výjazde došlo k operatívnej zmene servisného vozidla. Aktuálne pridelené vozidlo/vozidlá: <b>{auta_zoznam}</b>.<br>V prílohe zasielame aktualizovanú priepustku s novým QR kódom.")
                           .nastav_tlacidlo('Otvoriť portál vstupu', magic_link)
                           .build())

                try:
                    ISASNotifier.send(payload=payload, to_email=povolenie.zakaznik.email_vratnice, send_tg=False, send_mail=True, send_discord=False, attachments=attachments)
                    logger.info(f"✅ Aktualizácia vozidla pre povolenie {povolenie_id} bola úspešne odoslaná vrátnici.")
                except Exception as mail_err:
                    logger.error(f"🚨 Zlyhalo odoslanie aktualizácie vozidla na vrátnicu: {mail_err}")

            # 🛡️ DISPEČERSKÝ AUDIT: Notifikovanie vedenia/dispečingu o operatívnej zmene auta v teréne
            if povolenie.notif_admin_tg or povolenie.notif_admin_mail or getattr(povolenie, 'notif_admin_discord', True):
                sprava_pre_admina = (
                    "Technik v teréne operatívne zmenil vozidlo na už odoslanom vstupe. Vrátnici fabriky bol automaticky odoslaný aktualizovaný e-mail s novou priepustkou."
                    if (povolenie.zakaznik.email_vratnice and gn and gn.smtp_user) else
                    "Technik v teréne operatívne zmenil vozidlo na už odoslanom vstupe. Nové PDF priepustky bolo vygenerované v systéme (zákazník nemá evidovaný e-mail vrátnice)."
                )
                admin_payload = (ISASNotificationBuilder(nadpis="OPERATÍVNA ZMENA AUTA V TERÉNE", ikona='⚠️', farba_temy='#f59e0b')
                                 .pridaj_riadok('🏢 Zákazník:', povolenie.zakaznik.Nazov_spolocnosti)
                                 .pridaj_riadok('🚗 Nové vozidlo:', auta_zoznam)
                                 .pridaj_riadok('📅 Dátum vstupu:', povolenie.datum_vstupu.strftime('%d.%m.%Y'))
                                 .nastav_hlavny_text(sprava_pre_admina)
                                 .build())
                admin_email = (gn.email_majitel_notifikacie or gn.smtp_user) if gn else None
                try:
                    ISASNotifier.send(
                        payload=admin_payload,
                        to_email=admin_email,
                        send_tg=povolenie.notif_admin_tg,
                        send_mail=povolenie.notif_admin_mail and bool(admin_email),
                        send_discord=getattr(povolenie, 'notif_admin_discord', True)
                    )
                except Exception as admin_err:
                    logger.warning(f"Nepodarilo sa odoslať dispečerský alert o zmene auta: {admin_err}")


def vykonaj_zmenu_vozidla_vstupu(povolenie_id: int, auto_id: int, user_identifier: dict) -> tuple[bool, str]:
    """
    🧠 CENTRÁLNA BIZNIS LOGIKA PRE BOTOV (DRY):
    user_identifier: {'telegram_id': int} alebo {'discord_id': int}
    """

    with transaction.atomic():
        try:
            tg_id = user_identifier.get('telegram_id') if isinstance(user_identifier, dict) else None
            dc_id = user_identifier.get('discord_id') if isinstance(user_identifier, dict) else None

            if not tg_id and not dc_id:
                return False, "⚠️ <b>Chyba:</b> Neplatná alebo chýbajúca identifikácia používateľa."

            filter_kwargs = {'id': povolenie_id}
            if tg_id:
                filter_kwargs['zamestnanci__telegram_id'] = tg_id
            elif dc_id:
                filter_kwargs['zamestnanci__discord_id'] = dc_id

            povolenie = PovolenieNaVstup.objects.select_for_update(of=('self',)).select_related('zakaznik').prefetch_related('vozidla').get(**filter_kwargs)
            nove_auto = Vozidlo.objects.get(id=auto_id)
        except (PovolenieNaVstup.DoesNotExist, Vozidlo.DoesNotExist):
            return False, "⚠️ <b>Chyba:</b> Nemáš oprávnenie upravovať túto priepustku alebo neexistuje."

        # 🛑 SITUÁCIA C (MOŽNOSŤ 1): Striktný zákaz zmeny po rozhodnutí SBS
        if povolenie.stav in ['SCHVALENE', 'ZAMIETNUTE']:
            return False, f"🛑 <b>Zmena zamietnutá:</b> Vstup bol vrátnicou už <b>{povolenie.get_stav_display().upper()}</b>. Z bezpečnostných dôvodov nie je možné auto zmeniť cez bota. Kontaktuj dispečing."

        # 🛡️ DEFENSIVE: Ak je auto už priradené, nepregenerovávame PDF ani nespamujeme vrátnicu e-mailom
        aktualne_auta_ids = set(povolenie.vozidla.values_list('id', flat=True))
        if aktualne_auta_ids == {nove_auto.id}:
            return True, f"ℹ️ <b>Vozidlo {nove_auto.spz} už je k tomuto vstupu priradené.</b> Žiadna zmena nebola potrebná."

        # Uloženie nového auta
        povolenie.vozidla.clear()
        povolenie.vozidla.add(nove_auto)

        # SITUÁCIA A: Vstup ešte neodišiel na vrátnicu (GENEROVANE)
        if povolenie.stav == 'GENEROVANE':
            if povolenie.vygenerovany_dokument and povolenie.vygenerovany_dokument.name:
                try:
                    if default_storage.exists(povolenie.vygenerovany_dokument.name):
                        default_storage.delete(povolenie.vygenerovany_dokument.name)
                except Exception:
                    pass
                povolenie.vygenerovany_dokument = None
                povolenie.save(update_fields=['vygenerovany_dokument'])

            pid_vstup = povolenie.id
            transaction.on_commit(lambda: vygeneruj_vstupku_ai_task.delay(pid_vstup))
            return True, f"✅ <b>VOZIDLO ZMENENÉ NA {nove_auto.spz}</b>\n━━━━━━━━━━━━━━━━━━━━\nPriepustka sa pregenerováva. Vstup čaká na kontrolu a odoslanie dispečingom."

        # SITUÁCIA B: Vstup už bol odoslaný na vrátnicu (ODOSLANE)
        elif povolenie.stav == 'ODOSLANE':
            pid_akt = povolenie.id
            transaction.on_commit(lambda: pregeneruj_a_odosli_aktualizaciu_vstupu_task.delay(pid_akt))
            if povolenie.zakaznik.email_vratnice:
                return True, f"✅ <b>VOZIDLO ZMENENÉ NA {nove_auto.spz}</b>\n━━━━━━━━━━━━━━━━━━━━\nVrátnici fabriky bol automaticky odoslaný <b>aktualizačný e-mail</b> s novým PDF a novou ŠPZ."
            else:
                return True, f"✅ <b>VOZIDLO ZMENENÉ NA {nove_auto.spz}</b>\n━━━━━━━━━━━━━━━━━━━━\n(Firma nemá e-mail vrátnice, nové PDF priepustky sa vygenerovalo v systéme)."

        return False, "⚠️ Neznámy stav povolenia."

@shared_task(base=ISASAutoHealTask)
def odosli_sbs_notifikacie_task(povolenie_id, akcia):
    try:
        # Pridané prefetch_related('vozidla') pre správne načítanie áut
        povolenie = PovolenieNaVstup.objects.select_related('zakaznik').prefetch_related('zamestnanci', 'vozidla').get(id=povolenie_id)
        gn = GlobalneNastavenia.objects.first()
        if not gn: return

        # Príprava dát priamo v tasku
        technici_zoznam = ", ".join([f"{t.meno} {t.priezvisko}" for t in povolenie.zamestnanci.all()])
        auta_zoznam = ", ".join([a.spz for a in povolenie.vozidla.all() if getattr(a, 'spz', None)])
        auta_str = auta_zoznam if auta_zoznam else 'Peší vstup / Nešpecifikované'
        
        farba_temy = '#16a34a' if akcia != 'zamietnut' else '#dc2626'
        ikona = '✅' if akcia != 'zamietnut' else '❌'
        nadpis = f"VSTUP {'SCHVÁLENÝ' if akcia != 'zamietnut' else 'ZAMIETNUTÝ'}"
        ai_poznamka = 'Môžete bez problémov vyraziť na pracovný/servisný výjazd.' if akcia != 'zamietnut' else 'Odporúča sa okamžite kontaktovať vrátnicu/zákazníka pre riešenie situácie.'
        
        hlavny_text = ""
        if povolenie.poznamka_sbs:
            hlavny_text = f"💬 <b>Poznámka od SBS:</b><br><i>{html.escape(str(povolenie.poznamka_sbs))}</i>"

        payload = (ISASNotificationBuilder(nadpis=nadpis, ikona=ikona, farba_temy=farba_temy)
                   .pridaj_riadok('🏢 Zákazník:', povolenie.zakaznik.Nazov_spolocnosti)
                   .pridaj_riadok('📅 Dátum vstupu:', povolenie.datum_vstupu.strftime('%d.%m.%Y'))
                   .pridaj_riadok('👨‍🔧 Pracovníci:', technici_zoznam)
                   .pridaj_riadok('🚗 Vozidlá:', auta_str)
                   .nastav_hlavny_text(hlavny_text)
                   .nastav_ai_poznamku(ai_poznamka)
                   .build())

        # 1. Notifikácia pre Admina (Majiteľa)
        if povolenie.notif_admin_tg or povolenie.notif_admin_mail or getattr(povolenie, 'notif_admin_discord', True):
            admin_email = gn.email_majitel_notifikacie or gn.smtp_user
            ISASNotifier.send(
                payload=payload,
                to_email=admin_email,
                send_tg=povolenie.notif_admin_tg,
                send_mail=povolenie.notif_admin_mail,
                send_discord=getattr(povolenie, 'notif_admin_discord', True)
            )

        # 2. Notifikácia pre Technikov (Doručenie výsledku schvaľovania: Schválené aj Zamietnuté)
        if povolenie.notif_technik_tg or povolenie.notif_technik_mail or getattr(povolenie, 'notif_technik_discord', False):
            for technik in povolenie.zamestnanci.all():
                ISASNotifier.send(
                    payload=payload,
                    to_email=technik.email if hasattr(technik, 'email') else None,
                    tg_chat_id=technik.telegram_id if hasattr(technik, 'telegram_id') else None,
                    discord_id=getattr(technik, 'discord_id', None),
                    send_tg=povolenie.notif_technik_tg and bool(getattr(technik, 'telegram_id', None)),
                    send_mail=povolenie.notif_technik_mail and bool(getattr(technik, 'email', None)),
                    send_discord=getattr(povolenie, 'notif_technik_discord', False) and bool(getattr(technik, 'discord_id', None))
                )

        # 3. 🔄 Automatické pregenerovanie PDF priepustky s autorizačnou pečaťou SBS
        try:
            identita = FiremnaIdentita.objects.first()
            nasa_firma = identita.nazov_spolocnosti if identita and identita.nazov_spolocnosti else "Naša spoločnosť"
            raw_base_url = (getattr(gn, 'clean_base_url', None) if gn else None) or getattr(settings, 'SITE_URL', '') or ''
            base_url = raw_base_url.rstrip('/')

            cesta_k_portalu = reverse('evidencia:schvalit_vstup_sbs', args=[povolenie.bezpecnostny_kluc])
            magic_link = f"{base_url}{cesta_k_portalu}"

            qr = qrcode.QRCode(version=1, box_size=10, border=0)
            qr.add_data(magic_link)
            qr.make(fit=True)
            img_qr = qr.make_image(fill_color="#0f172a", back_color="white")
            buffer = BytesIO()
            img_qr.save(buffer, format="PNG")
            qr_b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")

            context = {
                'povolenie': povolenie,
                'identita': identita,
                'qr_kod_b64': qr_b64,
            }
            nasa_firma_cista = re.sub(r'[^a-zA-Z0-9_]', '', str(nasa_firma or 'Firma').replace(' ', '_'))[:30] or "Firma"
            zak_nazov = getattr(povolenie.zakaznik, 'Nazov_spolocnosti', '') or "Zakaznik"
            zakaznik_cisty = re.sub(r'[^a-zA-Z0-9_]', '', str(zak_nazov).replace(' ', '_'))[:30] or "Zakaznik"
            file_name = f"Priepustka_{nasa_firma_cista}_{zakaznik_cisty}_{povolenie.datum_vstupu}.pdf"

            # 🗑️ OCHRANA PRED DUPLIKÁTOM: Fyzické zmazanie starého predbežného PDF z disku
            if povolenie.vygenerovany_dokument and povolenie.vygenerovany_dokument.name:
                try:
                    if default_storage.exists(povolenie.vygenerovany_dokument.name):
                        default_storage.delete(povolenie.vygenerovany_dokument.name)
                except Exception as del_e:
                    logger.warning(f"⚠️ Nepodarilo sa odstrániť starý súbor priepustky: {del_e}")

            ISASPDFBuilder.generate_and_save(
                template_name='admin/pdf_vstupka.html',
                context=context,
                base_url=base_url,
                file_name=file_name,
                instance=povolenie,
                file_field_name='vygenerovany_dokument',
                save_instance=True
            )
        except Exception as pdf_err:
            logger.error(f"Chyba pri pregenerovaní PDF priepustky: {pdf_err}")

    except Exception as e:
        logger.error(f"Chyba pri odosielaní SBS notifikácií: {e}")

@shared_task(base=ISASAutoHealTask)
def spustit_hromadny_export_task(mega_zaloha_id):
    """
    Spustí ťažký exportný engine na pozadí (Celery Worker).
    Zabraňuje HTTP Timeoutom pri generovaní veľkých ZIP archívov a DB dumpov.
    """
    lock_id = f"lock_mega_export_{mega_zaloha_id}"
    
    with isas_task_execution_guard(
        lock_id, 
        timeout=3600, 
        fallback_model=MegaZaloha, 
        fallback_id=mega_zaloha_id, 
        fallback_field='stav_spracovania', 
        fallback_value='chyba'
    ) as (acquired, gn, identita):
        if not acquired:
            return

        logger.info(f"📦 [ISAS DATA NEXUS] Spúšťam generovanie exportu ID: {mega_zaloha_id}")
        
        try:
            zaloha = MegaZaloha.objects.filter(pk=mega_zaloha_id).first()
            if zaloha and zaloha.typ_zalohy == 'audit_logy':
                task_archivovat_a_vycistit_audit_logy(mega_zaloha_id=mega_zaloha_id, zmazat_po_exporte=False)
            else:
                engine = ISASExportEngine(mega_zaloha_id)
                engine.spustit_export()
            logger.info(f"✅ [ISAS DATA NEXUS] Export ID: {mega_zaloha_id} bol úspešne dokončený.")
        except BaseException as be:
            if not isinstance(be, Exception):
                logger.error(f"🚨 [ISAS DATA NEXUS] Worker bol násilne ukončený (Timeout) pri ID {mega_zaloha_id}: {str(be)}")
                MegaZaloha.objects.filter(id=mega_zaloha_id).update(stav_spracovania='chyba')
            raise be

@shared_task(base=ISASAutoHealTask)
def task_archivovat_a_vycistit_audit_logy(mega_zaloha_id=None, zmazat_po_exporte=False):
    """
    🛡️ ISAS ARCHITECT: Dávkový zero-downtime export a čistenie auditných logov.
    - Zápis do .json.gz (NDJSON komprimovaný cez gzip) s pamäťovou náročnosťou do 100 MB.
    - Uloženie do MegaZaloha s politikou 'navzdy'.
    - Bezpečné PostgreSQL mazanie v dávkach po 2 000 PKs (iba pri zmazat_po_exporte=True).
    - Redis Mutex zámok s TTL 7200s proti súbežnému behu.
    """
    lock_key = "lock_task_archivovat_audit_logy"
    acquired = cache.add(lock_key, "locked", timeout=7200)
    if not acquired:
        if mega_zaloha_id:
            MegaZaloha.objects.filter(pk=mega_zaloha_id).update(
                stav_spracovania='chyba',
                poznamka="⚠️ Úloha archivácie audit logov už beží v inom procese. Počkajte na dokončenie."
            )
        logger.warning("⚠️ [ISAS AUDIT ARCHIV] Úloha archivácie audit logov už beží v inom procese. Preskakujem.")
        return

    zaloha = None
    tmp_path = None
    try:
        gn = GlobalneNastavenia.objects.first()
        retencia_dni = getattr(gn, 'audit_log_retencia_dni', 730) or 730
        cutoff_date = timezone.now() - timedelta(days=retencia_dni)

        if mega_zaloha_id:
            zaloha = MegaZaloha.objects.filter(pk=mega_zaloha_id).first()

        qs = ISASAuditLog.objects.select_related(None).order_by('cas_vykonania')
        if zaloha:
            if zaloha.obsahuje_data_od:
                qs = qs.filter(cas_vykonania__date__gte=zaloha.obsahuje_data_od)
            if zaloha.obsahuje_data_do:
                qs = qs.filter(cas_vykonania__date__lte=zaloha.obsahuje_data_do)
            if not zaloha.obsahuje_data_od and not zaloha.obsahuje_data_do and zmazat_po_exporte:
                qs = qs.filter(cas_vykonania__lt=cutoff_date)
        else:
            qs = qs.filter(cas_vykonania__lt=cutoff_date)

        pocet_zaznamov = qs.count()

        # Krok A: Overenie počtu záznamov
        if pocet_zaznamov == 0:
            if zaloha:
                zaloha.stav_spracovania = 'hotova'
                zaloha.poznamka = (zaloha.poznamka or "") + "\nℹ️ Žiadne staré logy na spracovanie."
                zaloha.save(update_fields=['stav_spracovania', 'poznamka'])
            logger.info("ℹ️ [ISAS AUDIT ARCHIV] Žiadne auditné logy na spracovanie. Task sa korektne ukončil.")
            return

        if not zaloha:
            zaloha = MegaZaloha.objects.create(
                nazov_zalohy=f"Automatický archív audit logov ({timezone.now().strftime('%d.%m.%Y')})",
                typ_zalohy='audit_logy',
                politika_uchovania='navzdy',
                stav_spracovania='spracovava_sa',
                obsahuje_data_do=cutoff_date.date(),
                poznamka=f"Automatická Celery Beat archivácia logov starších ako {retencia_dni} dní."
            )

        with tempfile.NamedTemporaryFile(suffix='.json.gz', delete=False) as tmp_file:
            tmp_path = tmp_file.name

        # Krok B: Zero-Downtime chunked streaming do .json.gz
        with gzip.open(tmp_path, 'wt', encoding='utf-8') as gz_out:
            log_iterator = qs.values(
                'id', 'cas_vykonania', 'vykonal_id', 'content_type_id',
                'object_id', 'akcia', 'pocet_zmien', 'isas_metadata_json_zmeny'
            ).iterator(chunk_size=5000)
            
            for item in log_iterator:
                if item.get('cas_vykonania') and hasattr(item['cas_vykonania'], 'isoformat'):
                    item['cas_vykonania'] = item['cas_vykonania'].isoformat()
                gz_out.write(json.dumps(item, ensure_ascii=False, default=str) + '\n')

        # Kontrola integrity: overenie existencie a nenulovej veľkosti
        file_size = os.path.getsize(tmp_path)
        if file_size <= 0:
            raise ValueError("Kritická chyba: Vygenerovaný archív má neplatnú veľkosť 0 bajtov!")

        filename = f"audit_logy_{timezone.now().strftime('%Y%m%d_%H%M%S')}.json.gz"
        with open(tmp_path, 'rb') as f_in:
            zaloha.subor_zalohy.save(filename, File(f_in), save=False)

        zaloha.velkost_v_mb = max(Decimal('0.01'), round(Decimal(file_size) / Decimal(1024 * 1024), 2))
        zaloha.stav_spracovania = 'hotova'
        zaloha.politika_uchovania = 'navzdy'
        zaloha.poznamka = (zaloha.poznamka or "") + f"\n✅ Export úspešne dokončený ({pocet_zaznamov} záznamov, {file_size} B)."
        zaloha.save(update_fields=['subor_zalohy', 'velkost_v_mb', 'stav_spracovania', 'politika_uchovania', 'datum_exspiracie', 'poznamka'])

        # Bezpečné PostgreSQL dávkové mazanie (iba pre plánovač Beat)
        if zmazat_po_exporte:
            if not zaloha.subor_zalohy or not zaloha.subor_zalohy.storage.exists(zaloha.subor_zalohy.name) or zaloha.subor_zalohy.size <= 0:
                raise IOError("Kritická chyba integrity: Súbor archívu sa nepodarilo overiť na cieľovom úložisku! Mazanie audit logov bolo zastavené.")

            logger.info("🗑️ [ISAS AUDIT ARCHIV] Spúšťam dávkové mazanie starých logov...")
            zmazanych_celkovo = 0
            while True:
                pks = list(ISASAuditLog.objects.select_related(None).filter(cas_vykonania__lt=cutoff_date).order_by('pk').values_list('pk', flat=True)[:2000])
                if not pks:
                    break
                with transaction.atomic():
                    ISASAuditLog.objects.select_related(None).filter(pk__in=pks).delete()
                zmazanych_celkovo += len(pks)
            
            zaloha.poznamka = (zaloha.poznamka or "") + f"\n🗑️ Z databázy bolo bezpečne zmazaných {zmazanych_celkovo} starých záznamov."
            zaloha.save(update_fields=['poznamka'])
            logger.info(f"✨ [ISAS AUDIT ARCHIV] Úspešne zmazaných {zmazanych_celkovo} starých auditných logov.")

    except Exception as e:
        logger.exception(f"🚨 [ISAS AUDIT ARCHIV] Zlyhala archivácia auditných logov: {e}")
        if zaloha:
            zaloha.stav_spracovania = 'chyba'
            zaloha.poznamka = (zaloha.poznamka or "") + f"\n❌ Kritická chyba: {str(e)}"
            zaloha.save(update_fields=['stav_spracovania', 'poznamka'])
        raise e
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        cache.delete(lock_key)

@shared_task(base=ISASAutoHealTask)
def spracuj_fakturu_ai_task(faktura_id):
    # 🔒 MUTEX: Zabráni súbežnému AI spracovaniu tej istej faktúry
    lock_id = f"lock_ai_faktura_{faktura_id}"
    
    with isas_task_execution_guard(lock_id, timeout=300) as (acquired, gn, identita):
        if not acquired:
            return "AI spracovanie tejto faktúry už prebieha."

        try:
            with transaction.atomic():
                # FÁZA 3: Tvrdý DB zámok ako poistka, ak by Redis zlyhal
                faktura = PrichodziaFaktura.objects.select_for_update().get(pk=faktura_id)
                
                if faktura.spracovane:
                    return "Faktúra už bola úspešne spracovaná umelou inteligenciou."
        except PrichodziaFaktura.DoesNotExist:
            return

        # Načítame globálne nastavenia ERP, aby sme vedeli, akú predkontáciu dať službám
        ERPExportNastavenia = apps.get_model('Evidencia', 'ERPExportNastavenia')
        erp_nastavenia = ERPExportNastavenia.objects.first()
        predkontacia_sluzby = getattr(erp_nastavenia, 'pohoda_predkontacia_sluzby', '2') if erp_nastavenia else '2'

        # Inicializácia klienta priamo v tasku
        api_key = get_gemini_api_key()
        gemini_model = getattr(settings, 'GEMINI_MODEL', None)
        if not api_key or not gemini_model:
            err_msg = "❌ AI vyťaženie zlyhalo: Chýba GEMINI_API_KEY alebo GEMINI_MODEL v konfigurácii systému."
            logger.warning(f"⚠️ [spracuj_fakturu_ai_task] FA ID {faktura_id}: {err_msg}")
            PrichodziaFaktura.objects.filter(pk=faktura_id).update(poznamka=err_msg)
            return err_msg

        client = genai.Client(api_key=api_key)

        povolene_mj_str = ", ".join(kod for kod, _ in MERNE_JEDNOTKY_CHOICES)
        default_stock_kat = get_default_stock_model_name()
        aktivna_mena = (identita.predvolena_mena if identita and identita.predvolena_mena else None) or get_firemna_mena()

        AI_CATEGORY_PROMPT_DATA = {}
        for top_category, subcategories in CATEGORY_MAP.items():
            for verbose_name, model_class in subcategories.items():
                AI_CATEGORY_PROMPT_DATA[model_class.__name__] = verbose_name
        AI_CATEGORY_PROMPT_DATA["Sluzba"] = "🛠️ Doprava, balné, clo, práca, inštalácia alebo iná nehmotná služba (nenaskladňuje sa)."

        try:
            if not faktura.subor:
                raise ValueError("📂 Súbor faktúry neexistuje.")
            file_data = faktura.subor.read()
            mime_type = "application/pdf" if faktura.subor.name.lower().endswith(".pdf") else "image/jpeg"
        except Exception as e:
            PrichodziaFaktura.objects.filter(pk=faktura_id).update(poznamka=f"❌ Chyba pri čítaní súboru: {str(e)}")
            return
        
        CenovyKatalog_model = apps.get_model('Evidencia', 'CenovyKatalog')
        # Použitie prefetch_related eliminuje N+1 queries pri GenericForeignKey!
        katalog_dat = [f"Kód: {getattr(obj.polozka_zo_skladu, 'seriove_cislo', obj.search_name)} | Popis: {getattr(obj.polozka_zo_skladu, 'kto_som', 'Bez popisu')} -> Kategória: {obj.polozka_zo_skladu.__class__.__name__}" for obj in CenovyKatalog_model.objects.prefetch_related('polozka_zo_skladu').all()[:200] if obj.polozka_zo_skladu]

        Dodavatel_model = apps.get_model('Evidencia', 'Dodavatel')
        dodavatelia_mapa = []
        for d in Dodavatel_model.objects.prefetch_related('ai_zameranie', 'zastupene_znacky').all():
            kategorie = [ct.model_class().__name__ for ct in d.ai_zameranie.all() if ct.model_class()]
            znacky = [v.nazov_vyrobcu for v in d.zastupene_znacky.all()]
            # Posielame IČO, DIČ a IBAN. Odstránená podmienka 'if kategorie', chceme VŠETKÝCH dodávateľov!
            dodavatelia_mapa.append(f"Názov: '{d.Nazov_spolocnosti}' | IČO: {d.ico or 'N/A'} | DIČ: {d.dic or 'N/A'} | IBAN: {d.iban or 'N/A'} | Kategórie: {', '.join(kategorie) if kategorie else 'Všetko'} | Značky: {', '.join(znacky) if znacky else 'Všetky'}")

        Vyrobca_model = apps.get_model('Evidencia', 'DatabazaVyrobcov')
        vyrobcovia_db = [f"ID: {v.id} | Názov: {v.nazov_vyrobcu}" for v in Vyrobca_model.objects.all()]

        prompt_detektiv = f"""
        Si elitný dátový inžinier pre ISAS CORE. Tvojou absolútnou prioritou je PREČÍTAŤ VŠETKY DÁTA z faktúry s 100% presnosťou.
        
        Úlohy a prísne pravidlá:
        1. Identifikuj dodávateľa (Názov, IČO, DIČ, IČ DPH vrátane kódu štátu, Adresa vrátane štátu/krajiny). Krížovo to over s DATABÁZOU DODÁVATEĽOV nižšie. Ak nájdeš zhodu v IČO/DIČ, použi presný názov z našej databázy.
        2. Identifikuj platobné údaje (IBAN, Variabilný symbol, Suma na úhradu, Mena dokladu (kód ISO 4217, napr. {aktivna_mena}), Dátum splatnosti).
        3. DÔKLADNE PREČÍTAJ TABUĽKU POLOŽIEK. Vypíš KAŽDÝ JEDEN RIADOK. Pre každý riadok extrahuj: Surový celý názov (vrátane kódov v názve), Množstvo, Mernú jednotku (ks, l, m, bal, atď.), Jednotkovú cenu, Sadzbu DPH (%) a EAN kód (ak je uvedený, zvyčajne 13-miestne číslo). Ak má dodávateľ explicitne oddelený svoj vlastný kód (SKU) od názvu, poznač ho.
        4. Pri položkách sa snaž nájsť zhodu s ŤAHÁKOM NAŠICH KÓDOV a DATABÁZOU VÝROBCOV. Ak zistíš, že položka na faktúre je zjavne to isté ako položka v našom ťaháku, poznač si to.
        
        --- INTERNÉ DATABÁZY ISAS CORE ---
        ŤAHÁK NAŠICH KÓDOV PRE PÁROVANIE: {katalog_dat}
        DATABÁZA DODÁVATEĽOV: {dodavatelia_mapa}
        DATABÁZA VÝROBCOV: {vyrobcovia_db}
        ----------------------------------
        
        Vypíš detailný TEXTOVÝ REPORT. NEVYNECHAJ ANI JEDNU POLOŽKU. ZÁKAZ HALUCINOVAŤ ÚDAJE, KTORÉ NA FAKTÚRE NIE SÚ. Neformátuj do JSON.
        """

        try:
            gn_ai = GlobalneNastavenia.objects.first()
            dph_val = getattr(gn_ai, 'zakladna_sadzba_dph', None)
            def_dph_str = str(dph_val) if dph_val is not None else str(GlobalneNastavenia.get_zakladna_dph())

            detektiv_response = client.models.generate_content(
                model=gemini_model,
                contents=[prompt_detektiv, types.Part.from_bytes(data=file_data, mime_type=mime_type)],
                config=types.GenerateContentConfig(thinking_config=types.ThinkingConfig(thinking_level=getattr(settings, 'GEMINI_THINKING_LEVEL', 'MEDIUM') or 'MEDIUM'))
            )
            zistenia_z_faktury_a_webu = detektiv_response.text

            prompt_formatovac = f"""
            Prekonvertuj analyzovaný report do striktného JSON formátu pre databázu ISAS CORE.
            
            --- ANALÝZA FAKTÚRY ---
            {zistenia_z_faktury_a_webu}
            ------------------------
            
            POVOLENÉ KATEGÓRIE (Musíš použiť PRESNE kľúč z tohto zoznamu): 
            {json.dumps(AI_CATEGORY_PROMPT_DATA, ensure_ascii=False)}
            
            DATABÁZA VÝROBCOV (Pre priradenie vyrobca_id):
            {vyrobcovia_db}
            
            PRAVIDLÁ PRE JSON POLIA:
            - "seriove_cislo": ZÁKAZ ROZDEĽOVAŤ! Vlož sem presný, surový a celý reťazec z faktúry (napr. "Názov položky / model").
            - "katalogove_cislo": Ak mal dodávateľ na faktúre explicitne oddelený svoj vlastný kód (SKU) v samostatnom stĺpci, daj ho sem. Inak null.
            - "ean": Ak si našiel EAN kód (13 čísel), daj ho sem. Inak null.
            - "merna_jednotka": MUSÍŠ vybrať výhradne jednu z týchto povolených jednotiek: {povolene_mj_str}. Preveď jednotku z faktúry na najbližšiu povolenú.
            - "sadzba_dph": Číslo (napr. {def_dph_str}). Ak chýba, daj null.
            - "kat": Musí byť presný kľúč z POVOLENÉ KATEGÓRIE. Ak nevieš, použi "{default_stock_kat}".
            - "vyrobca_id": Použi ID z DATABÁZY VÝROBCOV z predchádzajúceho kroku. Ak výrobcu nevieš s istotou určiť, vráť null (ZÁKAZ HALUCINOVAŤ ID!).
            - "typ_on": Ak ide o originálny diel, daj "O". Ak ide o náhradu (aftermarket), daj "N". Ak nevieš, daj null.
            - "cena_status": Ak je cena podozrivo vysoká oproti bežným, daj "DRAHSIE". Inak daj "OK".
            - "mena": VÝHRADNE 3-písmenový medzinárodný kód ISO 4217 meny faktúry (predvolená systémová mena je {aktivna_mena}). Všetky grafické symboly a národné skratky mien VŽDY preveď na ich oficiálny ISO 4217 kód.
            - "datum_splatnosti": Formát YYYY-MM-DD. Ak chýba, vráť null.
            - "cisla_nasich_objednavok": Hľadaj na faktúre referencie na naše objednávky (napr. "Vaša objednávka", "PO Number", "Objednávka č."). Vráť pole nájdených reťazcov. Ak nenájdeš nič, vráť prázdne pole [].
            
            Vygeneruj čistý JSON vo formáte:
            {{
                "dodavatel": {{"nazov": "...", "ico": "...", "dic": "...", "ic_dph": "...", "ulica": "...", "mesto": "...", "psc": "...", "stat": "..."}},
                "platba": {{"iban": "...", "variabilny_symbol": "...", "suma_na_uhradu": 0.0, "mena": "{aktivna_mena}", "datum_splatnosti": "YYYY-MM-DD", "cisla_nasich_objednavok": []}},
                "polozky": [
                    {{
                        "seriove_cislo": "Surový celý názov z faktúry", 
                        "katalogove_cislo": null,
                        "ean": null,
                        "nazov": "",
                        "kto_som": "Stručný technický popis (na čo to slúži)", 
                        "mnozstvo": 1.0, 
                        "merna_jednotka": "{MERNE_JEDNOTKY_CHOICES[0][0]}",
                        "unit_price": 0.0, 
                        "sadzba_dph": {def_dph_str},
                        "kat": "{default_stock_kat}",
                        "vyrobca_id": null, 
                        "typ_on": null, 
                        "cena_status": "OK"
                    }}
                ]
            }}
            Vráť VÝHRADNE validný JSON. Nepoužívaj markdown bloky (```json).
            """

            formatovac_response = client.models.generate_content(
                model=gemini_model,
                contents=[prompt_formatovac],
                config=types.GenerateContentConfig(thinking_config=types.ThinkingConfig(thinking_level=getattr(settings, 'GEMINI_THINKING_LEVEL', 'MEDIUM') or 'MEDIUM'), response_mime_type="application/json")
            )
            
            try:
                ai_data_raw = clean_and_parse_ai_json(formatovac_response.text)
                data = ai_data_raw[0] if (isinstance(ai_data_raw, list) and ai_data_raw and isinstance(ai_data_raw[0], dict)) else (ai_data_raw if isinstance(ai_data_raw, dict) else {})
            except Exception as e:
                PrichodziaFaktura.objects.filter(pk=faktura_id).update(poznamka=f"❌ AI vrátila poškodený JSON formát: {str(e)}")
                return
            
            d_info = data.get('dodavatel') if isinstance(data.get('dodavatel'), dict) else {}
            raw_ico = str(d_info.get('ico') or '').replace(" ", "").strip()
            ico_ai = raw_ico[:20] if raw_ico and raw_ico.lower() not in ['none', 'null', 'n/a'] else None
            dod = Dodavatel_model.objects.filter(ico=ico_ai).first() if ico_ai else faktura.dodavatel
            
            if not dod and ico_ai:
                raw_nazov = str(d_info.get('nazov') or '').strip()
                nazov_spol = raw_nazov[:100] if raw_nazov and raw_nazov.lower() not in ['none', 'null', 'n/a'] else 'Neznámy dodávateľ'
                
                def _cisti_str(val, max_len):
                    if not val: return None
                    s = str(val).strip()[:max_len]
                    return s if s and s.lower() not in ['none', 'null', 'n/a'] else None

                dod = Dodavatel_model.objects.create(
                    Nazov_spolocnosti=nazov_spol, 
                    ico=ico_ai, 
                    dic=_cisti_str(d_info.get('dic'), 20),
                    ic_dph=_cisti_str(d_info.get('ic_dph'), 20),
                    ulica_a_cislo=_cisti_str(d_info.get('ulica'), 150),
                    mesto=_cisti_str(d_info.get('mesto'), 100),
                    psc=_cisti_str(d_info.get('psc'), 20),
                    stat=_cisti_str(d_info.get('stat'), 50),
                    ostatne="🤖 Automaticky vytvorené cez AI Modul"
                )

            platba_info = data.get('platba') if isinstance(data.get('platba'), dict) else {}
            raw_iban = str(platba_info.get('iban') or '').replace(" ", "").strip()
            faktura.iban = raw_iban[:34] if raw_iban and raw_iban.lower() not in ['none', 'null', 'n/a'] else ""
            
            raw_vs = str(platba_info.get('variabilny_symbol') or '').strip()
            faktura.variabilny_symbol = raw_vs[:20] if raw_vs and raw_vs.lower() not in ['none', 'null', 'n/a'] else ""
            
            # 🛡️ ISAS ARCHITECT: Bezpečné načítanie meny s fallbackom na predvolenú menu firmy
            mena_ai = str(platba_info.get('mena', '')).upper().strip()[:10]
            fallback_mena = (identita.predvolena_mena if identita and identita.predvolena_mena else None) or get_firemna_mena()
            faktura.mena = mena_ai if len(mena_ai) == 3 and mena_ai.isalpha() else fallback_mena

            suma_raw = str(platba_info.get('suma_na_uhradu', '')).replace(' ', '').replace(',', '.')
            try:
                if suma_raw:
                    dec_suma = Decimal(suma_raw).quantize(Decimal('0.01'))
                    if dec_suma > Decimal('0.00'):
                        faktura.suma_na_uhradu = dec_suma
            except (ValueError, TypeError, ArithmeticError):
                pass

            if platba_info.get('datum_splatnosti') and platba_info.get('datum_splatnosti') != "YYYY-MM-DD":
                try: faktura.datum_splatnosti = datetime.strptime(platba_info.get('datum_splatnosti'), "%Y-%m-%d").date()
                except: pass
                
            cisla_objednavok_ai = platba_info.get('cisla_nasich_objednavok', [])
            if not isinstance(cisla_objednavok_ai, list):
                cisla_objednavok_ai = [str(cisla_objednavok_ai)]

            LogNaskladnenia_model = apps.get_model('Evidencia', 'LogNaskladnenia')
            videne = set()
            varovania = []

            # Inicializácia optimalizovaného Matchera (načíta DB kódy len raz pred transakciou)
            from .utils import ISASItemMatcher
            matcher = ISASItemMatcher()

            with transaction.atomic():
                faktura.polozky_log.all().delete()

                for p in data.get('polozky', []):
                    # Extrakcia nových polí
                    kod = str(p.get('seriove_cislo') or '').strip()
                    katalogove_cislo_ai = str(p.get('katalogove_cislo') or '').strip()
                    ean_ai = str(p.get('ean') or '').strip()
                    raw_mj = str(p.get('merna_jednotka') or '').strip()
                    merna_jednotka_ai = raw_mj if raw_mj and raw_mj.lower() not in ['none', 'null', 'n/a'] else 'ks'
                    
                    try: 
                        val_dph = p.get('sadzba_dph')
                        sadzba_dph_ai = Decimal(str(val_dph if val_dph not in [None, ''] else def_dph_str))
                    except: 
                        sadzba_dph_ai = None

                    raw_nazov = str(p.get('nazov') or '').strip()
                    nazov_z_faktury = raw_nazov[:250] if raw_nazov and raw_nazov.lower() not in ['none', 'null'] else ''
                    raw_popis = str(p.get('kto_som') or '').strip()
                    nazov_popis = raw_popis if raw_popis and raw_popis.lower() not in ['none', 'null'] else ''
                    
                    cena_raw = str(p.get('unit_price', '0')).replace(' ', '').replace(',', '.')
                    try: cena = Decimal(cena_raw).quantize(Decimal('0.01'))
                    except: cena = Decimal('0.00')

                    mnozstvo_raw = str(p.get('mnozstvo', '1')).replace(' ', '').replace(',', '.')
                    try: mnozstvo_ciste = Decimal(mnozstvo_raw).quantize(Decimal('0.001'))
                    except: mnozstvo_ciste = Decimal('1.000')

                    if kod.upper() in ['N/A', 'NULL', 'NONE', '']: kod = ""
                    if katalogove_cislo_ai.upper() in ['N/A', 'NULL', 'NONE', '']: katalogove_cislo_ai = None
                    if ean_ai.upper() in ['N/A', 'NULL', 'NONE', '']: ean_ai = None
                    
                    zobrazeny_kod = kod if kod else nazov_z_faktury
                    ident = f"{zobrazeny_kod}".lower()
                    
                    if ident not in videne and len(zobrazeny_kod) > 1:
                        # 🌊 ISAS WATERFALL MATCHING ENGINE (Delegované na dedikovanú triedu)
                        najdena_polozka, skutocna_kategoria, ct_id, obj_id = matcher.find_match(ean_ai, zobrazeny_kod)

                        kat_ai = skutocna_kategoria if skutocna_kategoria else p.get('kat')
                        if not kat_ai or kat_ai not in AI_CATEGORY_PROMPT_DATA:
                            kat_ai = default_stock_kat
                        
                        # Zistenie hlavnej kategórie z podkategórie
                        hlavna_kat_ai = None
                        if kat_ai == "Sluzba":
                            hlavna_kat_ai = "Služby a Práce"
                        else:
                            for main_cat, subcats in CATEGORY_MAP.items():
                                for verbose_name, model_class in subcats.items():
                                    if model_class.__name__ == kat_ai:
                                        hlavna_kat_ai = main_cat
                                        break
                                if hlavna_kat_ai: break
                        
                        if najdena_polozka:
                            ct_id = ContentType.objects.get_for_model(najdena_polozka).id
                            obj_id = najdena_polozka.id

                        vyrobca_instancia = None
                        if not najdena_polozka and p.get('vyrobca_id'):
                            try:
                                vyrobca_instancia = Vyrobca_model.objects.filter(id=int(p.get('vyrobca_id'))).first()
                            except (ValueError, TypeError):
                                vyrobca_instancia = None

                        # Ak AI vyhodnotila, že ide o službu (Doprava, Práca...), 
                        # automaticky priradíme ERP predkontáciu pre služby (napr. '2').
                        auto_predkontacia = predkontacia_sluzby if kat_ai == "Sluzba" else None

                        LogNaskladnenia_model.objects.create(
                            faktura=faktura, najdeny_kod_faktura=zobrazeny_kod, navrhovany_nazov=nazov_z_faktury, 
                            hlavna_kategoria=hlavna_kat_ai, navrhovana_kategoria=kat_ai, mnozstvo=mnozstvo_ciste,
                            nakupna_cena=cena if cena > Decimal('0.00') else None,
                            navrhovany_vyrobca=vyrobca_instancia,
                            navrhovany_typ=p.get('typ_on') if not najdena_polozka else None,
                            navrhovany_popis=nazov_popis if not najdena_polozka else None,
                            navrhovany_ean=ean_ai,
                            navrhovana_merna_jednotka=merna_jednotka_ai,
                            navrhovana_sadzba_dph=sadzba_dph_ai,
                            navrhovane_katalogove_cislo=katalogove_cislo_ai,
                            erp_predkontacia=auto_predkontacia,
                            content_type_id=ct_id, object_id=obj_id
                        )
                        
                        if p.get('cena_status') == "DRAHSIE": varovania.append(f"⚠️ {zobrazeny_kod} má vyššiu cenu!")
                        videne.add(ident)

                pocet_poloziek = len(videne)
                PrichodziaFaktura.objects.filter(pk=faktura_id).update(
                    dodavatel=dod, spracovane=True, iban=faktura.iban, variabilny_symbol=faktura.variabilny_symbol,
                    suma_na_uhradu=faktura.suma_na_uhradu, mena=faktura.mena, datum_splatnosti=faktura.datum_splatnosti, 
                    poznamka=f"✅ Spracované AI a ISAS Match. Položiek: {pocet_poloziek}. " + " ".join(varovania)
                )
            
                transaction.on_commit(lambda: task_vyhodnot_smart_matcher_faktura_objednavka.delay(faktura_id, cisla_objednavok_ai, zistenia_z_faktury_a_webu))

        except Exception as e:
            PrichodziaFaktura.objects.filter(pk=faktura_id).update(poznamka=f"❌ AI Error: {str(e)}")

@shared_task(base=ISASAutoHealTask)
def posli_email_uctovnicke_task(faktura_id):
    try:
        faktura = PrichodziaFaktura.objects.select_related('dodavatel').get(pk=faktura_id)
        dodavatel_nazov = faktura.dodavatel.Nazov_spolocnosti if faktura.dodavatel else 'Neznámy dodávateľ'
        pocet_poloziek = faktura.polozky_log.count()
        
        ai_status = "✅ Pripravené na kontrolu a zaúčtovanie"
        ai_color = "#10b981" 
        if faktura.poznamka and "⚠️" in faktura.poznamka:
            ai_status = "⚠️ AI detegovala zmenu nákupných cien (Drahšie!)"
            ai_color = "#f59e0b" 
            
        # 🧠 ISAS 3-WAY MATCHING INFO PRE E-MAIL
        sparovane_objednavky = list(faktura.suvisiace_objednavky.values_list('cislo_objednavky', flat=True))
        navrhnute_objednavky = list(faktura.navrhnute_objednavky.values_list('cislo_objednavky', flat=True))
        
        sparovanie_text = "Nespárované s objednávkou"
        if sparovane_objednavky:
            sparovanie_text = f"🔗 Spárované s: {', '.join(sparovane_objednavky)}"
        elif navrhnute_objednavky:
            sparovanie_text = f"🤖 AI navrhuje: {', '.join(navrhnute_objednavky)}"
            
        predmet = f"🧾 ISAS CORE | Nová faktúra na zaúčtovanie: {dodavatel_nazov}"
        gn = GlobalneNastavenia.objects.first()
        if not gn: return
        
        base_url = (getattr(gn, 'clean_base_url', None) or getattr(settings, 'SITE_URL', '')).strip().rstrip('/')
        cesta = reverse('admin:Evidencia_prichodziafaktura_change', args=[faktura.id])
        isas_link = f"{base_url}{cesta}"
        
        mena_symbol = getattr(faktura, 'mena', '') or get_firemna_mena()
        suma_str = f"{faktura.suma_na_uhradu:.2f} {mena_symbol}" if faktura.suma_na_uhradu else "Nezistená"
        vs_str = faktura.variabilny_symbol if faktura.variabilny_symbol else "Nezistený"
        iban_str = faktura.iban if faktura.iban else "Nezistený"

        # ⚖️ Dynamický text podľa toho, či ide o Peppol alebo bežné PDF
        je_peppol = bool(faktura.xml_subor) or faktura.zdroj == 'PEPPOL'
        
        upozornenie_cervene = None
        if je_peppol:
            uvod_text_plain = "firemný informačný systém ISAS CORE úspešne prijal novú elektronickú faktúru (eFaktúru) zo siete Peppol."
            prilohy_text_plain = "📎 V prílohe nájdete originálny XML súbor (zákonný daňový doklad) a vizuálnu PDF kópiu (ak ju dodávateľ pribalil)."
            uvod_text_html = "firemný informačný systém <b>ISAS CORE</b> úspešne prijal novú elektronickú faktúru (eFaktúru) zo siete Peppol."
            prilohy_text_html = "📎 V prílohe tohto e-mailu nájdete <b>originálny XML súbor</b> (zákonný daňový doklad) a vizuálnu PDF kópiu (ak ju dodávateľ pribalil)."
            upozornenie_cervene = "<strong>⚖️ Právna informácia (Elektronická fakturácia):</strong> Táto faktúra bola doručená cez sieť Peppol. <b>Priložený XML súbor je jediný platný originál daňového dokladu.</b> Prípadné PDF slúži výhradne ako ľudsky čitateľná vizuálna kópia."
        else:
            uvod_text_plain = "firemný informačný systém ISAS CORE úspešne zachytil, prečítal a vyťažil novú dodávateľskú faktúru prostredníctvom zabudovanej umelej inteligencie."
            prilohy_text_plain = "📎 Fyzický dokument (PDF/sken) je bezpečne archivovaný a nachádza sa aj v prílohe tohto e-mailu pre Vaše účtovné potreby."
            uvod_text_html = "firemný informačný systém <b>ISAS CORE</b> úspešne zachytil, prečítal a vyťažil novú dodávateľskú faktúru prostredníctvom zabudovanej umelej inteligencie."
            prilohy_text_html = "📎 Fyzický dokument (PDF/sken) je bezpečne archivovaný a nachádza sa aj v prílohe tohto e-mailu pre Vaše účtovné potreby."

        cielovy_email = None
        identita = FiremnaIdentita.objects.first()
        if identita and identita.email_uctovnicky:
            cielovy_email = identita.email_uctovnicky
        
        if not cielovy_email or not gn.smtp_user: return

        attachments = []
        if faktura.subor:
            try:
                faktura.subor.seek(0)
                mime_type, _ = mimetypes.guess_type(faktura.subor.name)
                attachments.append({
                    'filename': faktura.subor.name.split('/')[-1],
                    'content': faktura.subor.read(),
                    'mimetype': mime_type or 'application/octet-stream'
                })
            except Exception as read_err:
                logger.warning(f"⚠️ Nepodarilo sa prečítať súbor faktúry #{faktura_id} pre prílohu e-mailu: {read_err}")
            
        if faktura.xml_subor:
            try:
                faktura.xml_subor.seek(0)
                attachments.append({
                    'filename': faktura.xml_subor.name.split('/')[-1],
                    'content': faktura.xml_subor.read(),
                    'mimetype': 'application/xml'
                })
            except Exception as xml_read_err:
                logger.warning(f"⚠️ Nepodarilo sa prečítať XML faktúry #{faktura_id} pre prílohu e-mailu: {xml_read_err}")

        builder = (ISASNotificationBuilder(nadpis='NOVÁ FAKTÚRA NA ZAÚČTOVANIE', ikona='🧾', farba_temy=ai_color)
                   .pridaj_riadok('🏢 Dodávateľ:', dodavatel_nazov)
                   .pridaj_riadok('💰 Suma na úhradu:', suma_str)
                   .pridaj_riadok('🔢 Var. symbol:', vs_str)
                   .pridaj_riadok('🏦 IBAN:', iban_str)
                   .pridaj_riadok('📅 Dátum prijatia:', faktura.datum_prijatia.strftime('%d.%m.%Y o %H:%M'))
                   .pridaj_riadok('📦 Počet položiek:', f"{pocet_poloziek} ks")
                   .pridaj_riadok('🛒 Objednávka:', sparovanie_text)
                   .pridaj_riadok('🧠 AI Analýza:', ai_status)
                   .nastav_hlavny_text(f"👋 Dobrý deň,<br><br>{uvod_text_html}<br><br>{prilohy_text_html}")
                   .nastav_tlacidlo('Otvoriť faktúru v systéme ISAS CORE', isas_link))
        if upozornenie_cervene:
            builder.nastav_upozornenie(upozornenie_cervene, 'cervene')

        ISASNotifier.send(
            payload=builder.build(),
            to_email=cielovy_email,
            send_tg=False,
            send_mail=True,
            send_discord=False,
            attachments=attachments
        )

    except Exception as e:
        logger.error(f"Chyba pri odosielaní emailu účtovníčke: {e}")

@shared_task(base=ISASAutoHealTask)
def odosli_notifikaciu_o_prijme_task(faktura_id, log_ids):
    try:
        faktura = PrichodziaFaktura.objects.select_related('dodavatel', 'cielovy_sklad').get(pk=faktura_id)
        gn = GlobalneNastavenia.objects.first()
        if not gn: return

        aktualne_logy = LogNaskladnenia.objects.filter(id__in=log_ids).prefetch_related('polozka')
        if not aktualne_logy.exists(): return

        celkova_hodnota = Decimal('0.00')
        polozky_telegram = []
        mena_dokladu = getattr(faktura, 'mena', '') or get_firemna_mena()

        for p in aktualne_logy:
            mnozstvo = p.mnozstvo or Decimal('1.000')
            cena_ks = p.nakupna_cena or Decimal('0.00')
            spolu = (Decimal(str(cena_ks)) * Decimal(str(mnozstvo))).quantize(Decimal('0.01'))
            celkova_hodnota += spolu
            
            jednotka = getattr(p.polozka, 'merna_jednotka', 'ks') if p.polozka else 'ks'
            cisty_nazov = safe_tg(p.navrhovany_nazov or p.najdeny_kod_faktura)
            
            polozky_telegram.append(f"🔸 {mnozstvo} {jednotka} | {cisty_nazov} <i>({spolu} {mena_dokladu})</i>")

        dodavatel_nazov = faktura.dodavatel.Nazov_spolocnosti if faktura.dodavatel else "Neznámy dodávateľ"
        hlavny_sklad = Sklad.objects.first()
        nazov_hlavneho = f"Hlavný sklad ({hlavny_sklad.nazov})" if hlavny_sklad and hlavny_sklad.nazov and hlavny_sklad.nazov.lower() != "hlavný sklad" else "Hlavný sklad"
        sklad_nazov = faktura.cielovy_sklad.nazov if faktura.cielovy_sklad else nazov_hlavneho
        
        base_url = (getattr(gn, 'clean_base_url', None) or getattr(settings, 'SITE_URL', '')).strip().rstrip('/')
        isas_link = f"{base_url}{reverse('admin:Evidencia_prichodziafaktura_change', args=[faktura.id])}"

        # Namiesto zložitej HTML tabuľky použijeme čistý zoznam, ktorý ISASNotifier
        # bezpečne preloží pre Telegram aj E-mail.
        polozky_html = "<b>Naskladnené položky (Dávka):</b><br>"
        for p in aktualne_logy:
            mnozstvo = p.mnozstvo or Decimal('1.000')
            cena_ks = p.nakupna_cena or Decimal('0.00')
            spolu = (Decimal(str(cena_ks)) * Decimal(str(mnozstvo))).quantize(Decimal('0.01'))
            jednotka = getattr(p.polozka, 'merna_jednotka', 'ks') if p.polozka else 'ks'
            cisty_nazov = safe_tg(p.navrhovany_nazov or p.najdeny_kod_faktura)
            polozky_html += f"🔸 {mnozstvo} {jednotka} | {cisty_nazov} <i>({spolu} {mena_dokladu})</i><br>"

        payload = (ISASNotificationBuilder(nadpis='PRÍJEM NA SKLAD', ikona='📦', farba_temy='#10b981')
                   .pridaj_riadok('🏢 Dodávateľ:', safe_tg(dodavatel_nazov))
                   .pridaj_riadok('🏭 Cieľový sklad:', safe_tg(sklad_nazov))
                   .pridaj_riadok('💰 Hodnota príjmu:', f"{celkova_hodnota} {mena_dokladu}")
                   .nastav_hlavny_text(polozky_html)
                   .nastav_tlacidlo('Otvoriť faktúru v systéme ISAS CORE', isas_link)
                   .build())

        ISASNotifier.send(
            payload=payload,
            send_tg=faktura.notif_prijem_tg,
            send_mail=faktura.notif_prijem_mail,
            send_discord=getattr(faktura, 'notif_prijem_discord', True)
        )

    except Exception as e:
        logger.error(f"Chyba pri odosielaní notifikácie o príjme: {e}")

@shared_task(base=ISASAutoHealTask)
def fetch_peppol_invoices_task():
    """
    ISAS CORE: PULL architektúra pre sťahovanie eFaktúr zo siete Peppol.
    Pravidelne kontroluje Inbox u Digitálneho poštára (SAPI-SK / Access Point).
    """
    # 🔒 MUTEX: Zabráni súbežnému sťahovaniu
    with isas_task_execution_guard('lock_peppol_inbox', timeout=600) as (acquired, gn, identita):
        if not acquired:
            return "Sťahovanie Peppol faktúr už prebieha."

        if not gn or not gn.peppol_api_url or not gn.peppol_client_id:
            logger.warning("⚠️ Peppol Inbox: Chýba konfigurácia API.")
            return

        base_url = gn.peppol_api_url.rstrip('/')
        
        # 1. Získanie OAuth Tokenu
        cache_key = 'peppol_access_token'
        access_token = cache.get(cache_key)
        
        if not access_token:
            auth_data = {
                'grant_type': 'client_credentials', 
                'client_id': gn.peppol_client_id, 
                'client_secret': gn.peppol_client_secret,
                'scope': 'document:send document:receive'
            }

            provider = getattr(gn, 'efaktura_provider', 'DIRECT_AP') or 'DIRECT_AP'
            je_sapi_protokol = (
                provider in ['DIRECT_SAPI_SK', 'SAPI_SK'] or 
                str(getattr(gn, 'peppol_participant_id', '') or '').strip().startswith('0245:') or
                'sapi' in base_url.lower()
            )
            auth_payload = {'json': auth_data} if je_sapi_protokol else {'data': auth_data}
            verify_ssl = not (getattr(settings, 'DEBUG', False) and any(loc in base_url for loc in ['localhost', '127.0.0.1', 'testserver']))
            auth_response = requests.post(f"{base_url}/auth/token", **auth_payload, timeout=10, verify=verify_ssl)
            
            if auth_response.status_code == 200:
                access_token = auth_response.json().get('access_token')
                if access_token:
                    cache.set(cache_key, access_token, timeout=3000)
                else:
                    logger.error("❌ Peppol Inbox: API nevrátilo access_token.")
                    return
            else:
                logger.error(f"❌ Peppol Inbox: Zlyhala autentifikácia. HTTP {auth_response.status_code} - {auth_response.text}")
                return

        # Štandardné Peppol AP hlavičky pre inbox
        headers = {
            'Authorization': f'Bearer {access_token}', 
            'Content-Type': 'application/json',
            'X-Peppol-Participant-Id': str(gn.peppol_participant_id)
        }

        # 2. Dopyt na Inbox (Získanie zoznamu čakajúcich správ podľa OpenPeppol / SAPI-SK štandardu)
        verify_ssl = not (getattr(settings, 'DEBUG', False) and any(loc in base_url for loc in ['localhost', '127.0.0.1', 'testserver']))
        inbox_response = requests.get(f"{base_url}/document/receive", headers=headers, timeout=15, verify=verify_ssl)
        
        # Ak token expiroval alebo je neplatný, musíme ho zmazať z cache!
        if inbox_response.status_code == 401:
            logger.warning("⚠️ Peppol Inbox: Token expiroval alebo je neplatný (HTTP 401). Mažem z cache.")
            cache.delete(cache_key)
            return
            
        if inbox_response.status_code != 200:
            logger.error(f"❌ Peppol Inbox: Chyba pri čítaní schránky HTTP {inbox_response.status_code}")
            return

        spravy = inbox_response.json().get('documents', [])
        if not spravy:
            logger.info("✓ Peppol Inbox: Žiadne nové eFaktúry.")
            return

        logger.info(f"📥 Peppol Inbox: Nájdených {len(spravy)} nových správ. Začínam sťahovanie.")

        # 3. Sťahovanie a spracovanie každej správy
        for doc in spravy:
            raw_msg_id = doc.get('documentId')
            if not raw_msg_id:
                logger.error(f"⚠️ Peppol Inbox: Nenašlo sa 'documentId' v odpovedi. Dostupné dáta: {doc}")
                continue

            msg_id = raw_msg_id
                
            try:
                # A. Stiahnutie správy (Peppol AP REST obálka)
                verify_ssl = not (getattr(settings, 'DEBUG', False) and any(loc in base_url for loc in ['localhost', '127.0.0.1', 'testserver']))
                
                safe_msg_id = urllib.parse.quote(str(msg_id))
                
                download_response = requests.get(f"{base_url}/document/receive/{safe_msg_id}", headers=headers, timeout=15, verify=verify_ssl)
                
                if download_response.status_code != 200:
                    logger.error(f"⚠️ Peppol Inbox: Zlyhalo stiahnutie správy {msg_id}. HTTP {download_response.status_code} - Odpoveď: {download_response.text}")
                    continue
                
                response_json = download_response.json()
                
                # SAPI-SK vracia payload ako čistý UTF-8 string, NIE Base64!
                string_payload = response_json.get('payload')
                if not string_payload:
                    logger.error(f"⚠️ Peppol Inbox: Správa {msg_id} neobsahuje payload.")
                    continue
                    
                # Prevedieme string na bytes pre náš XML parser
                xml_content = string_payload.encode('utf-8')

                # B. Parsovanie tvrdých dát
                parsed_data = PeppolXMLParser.parse(xml_content)
                if not (parsed_data.get('ico') or parsed_data.get('ic_dph') or parsed_data.get('dic')):
                    logger.warning(f"⚠️ Peppol Inbox: Správa {msg_id} neobsahuje žiadny identifikátor dodávateľa (IČO/DIČ/IČ DPH). Preskakujem.")
                    continue

                # C. Deduplikácia a Uloženie (Náš Engine z Fázy 1)
                faktura = PeppolInboxEngine.spracuj_prijate_xml(xml_content, msg_id, parsed_data)

                # D. Potvrdenie o prijatí (Acknowledge)
                if faktura:
                    # 1. Acknowledge (Technické potvrdenie pre poštára, že sme to stiahli)
                    # Správny endpoint je /document/receive/{documentId}/acknowledge
                    ack_response = requests.post(f"{base_url}/document/receive/{safe_msg_id}/acknowledge", headers=headers, timeout=10, verify=verify_ssl)
                    
                    # SAPI-SK v1.0 NEMÁ endpoint /response pre MLR. 
                    # Acknowledge slúži ako potvrdenie doručenia na koncový bod.
                    if ack_response.status_code in [200, 204]:
                        logger.info(f"✅ Peppol Inbox: Správa {msg_id} úspešne potvrdená (ACKNOWLEDGED).")
                    else:
                        logger.warning(f"⚠️ Peppol Inbox: Správa {msg_id} stiahnutá, ale zlyhalo potvrdenie (HTTP {ack_response.status_code}).")
                    
                    # E. Spustenie AI Sémantiky pre položky (Ak je to nová faktúra)
                    if not faktura.spracovane:
                        from .tasks import task_peppol_ai_semantika
                        task_peppol_ai_semantika.delay(faktura.id, parsed_data['polozky'])

            except Exception as msg_err:
                logger.error(f"⚠️ Peppol Inbox: Chyba pri spracovaní správy {msg_id}: {str(msg_err)}")
                continue

@shared_task(base=ISASAutoHealTask)
def task_peppol_ai_semantika(faktura_id, polozky_data):
    """
    ISAS CORE: Sémantická analýza položiek z Peppol XML.
    Spája 100% presné tvrdé dáta z XML s inteligenciou Gemini AI.
    """
    lock_id = f"lock_peppol_ai_{faktura_id}"
    
    with isas_task_execution_guard(lock_id, timeout=300) as (acquired, gn, identita):
        if not acquired:
            return "AI spracovanie tejto Peppol faktúry už prebieha."

        PrichodziaFaktura = apps.get_model('Evidencia', 'PrichodziaFaktura')
        LogNaskladnenia = apps.get_model('Evidencia', 'LogNaskladnenia')
        Vyrobca_model = apps.get_model('Evidencia', 'DatabazaVyrobcov')
        ERPExportNastavenia = apps.get_model('Evidencia', 'ERPExportNastavenia')

        try:
            faktura = PrichodziaFaktura.objects.get(pk=faktura_id)
        except PrichodziaFaktura.DoesNotExist:
            logger.error(f"❌ Peppol AI: Faktúra {faktura_id} neexistuje.")
            return

        # 1. Príprava dát pre AI (Rovnaké ako pri PDF)
        erp_nastavenia = ERPExportNastavenia.objects.first()
        predkontacia_sluzby = getattr(erp_nastavenia, 'pohoda_predkontacia_sluzby', '2') if erp_nastavenia else '2'

        AI_CATEGORY_PROMPT_DATA = {}
        for top_category, subcategories in CATEGORY_MAP.items():
            for verbose_name, model_class in subcategories.items():
                AI_CATEGORY_PROMPT_DATA[model_class.__name__] = verbose_name
        AI_CATEGORY_PROMPT_DATA["Sluzba"] = "🛠️ Doprava, balné, clo, práca, inštalácia alebo iná nehmotná služba (nenaskladňuje sa)."

        default_stock_kat = get_default_stock_model_name()

        vyrobcovia_db = [f"ID: {v.id} | Názov: {v.nazov_vyrobcu}" for v in Vyrobca_model.objects.all()]
        # Posielame AI aj kód položky (ak existuje), aby mala rovnaký kontext ako pri PDF
        nazvy_na_analyzu = [f"{p.get('kod', p.get('seriove_cislo', ''))} {p.get('nazov', 'Neznáma položka')}".strip() for p in polozky_data]

        # 2. Volanie Gemini AI (Odľahčený, bleskový prompt)
        api_key = get_gemini_api_key()
        gemini_model = getattr(settings, 'GEMINI_MODEL', None)
        
        if not api_key or not gemini_model:
            logger.warning(f"⚠️ [task_peppol_ai_semantika] FA ID {faktura_id}: Chýba GEMINI_API_KEY alebo GEMINI_MODEL. Položky budú spracované bez sémantickej AI kategorizácie.")
            ai_results = [{} for _ in polozky_data]
        else:
            # 2. Volanie Gemini AI (Odľahčený, bleskový prompt)
            client = genai.Client(api_key=api_key)
            
            prompt = f"""
            Si elitný dátový inžinier pre ISAS CORE. Dostal si zoznam položiek z elektronickej faktúry (XML).
            Tvojou jedinou úlohou je SÉMANTICKÁ KATEGORIZÁCIA. Množstvá a ceny už máme, tie nerieš.
        
            POVOLENÉ KATEGÓRIE (Musíš použiť PRESNE kľúč z tohto zoznamu): 
            {json.dumps(AI_CATEGORY_PROMPT_DATA, ensure_ascii=False)}
        
            DATABÁZA VÝROBCOV (Pre priradenie vyrobca_id):
            {vyrobcovia_db}
        
            POLOŽKY NA ANALÝZU:
            {json.dumps(nazvy_na_analyzu, ensure_ascii=False)}
        
            PRAVIDLÁ:
            Vráť VÝHRADNE validný JSON pole (array) objektov v PRESNE ROVNAKOM PORADÍ, ako sú položky na analýzu.
            Formát jedného objektu:
            {{
                "kat": "Musí byť presný kľúč z POVOLENÉ KATEGÓRIE. Ak nevieš, použi '{default_stock_kat}'.",
                "vyrobca_id": "Použi ID z DATABÁZY VÝROBCOV. Ak výrobcu nevieš s istotou určiť, vráť null (ZÁKAZ HALUCINOVAŤ ID!).",
                "typ_on": "Ak ide o originálny diel, daj 'O'. Ak ide o náhradu (aftermarket), daj 'N'. Ak nevieš, daj null.",
                "kto_som": "Stručný technický popis (na čo to slúži)"
            }}
            Vráť VÝHRADNE validný JSON. Nepoužívaj markdown bloky (```json).
            """

            try:
                response = client.models.generate_content(
                    model=gemini_model,
                    contents=[prompt],
                    config=types.GenerateContentConfig(thinking_config=types.ThinkingConfig(thinking_level=getattr(settings, 'GEMINI_THINKING_LEVEL', 'MEDIUM') or 'MEDIUM'), response_mime_type="application/json")
                )
                ai_results = clean_and_parse_ai_json(response.text)
            except Exception as e:
                logger.error(f"❌ Peppol AI: Zlyhalo parsovanie JSON od Gemini: {e}")
                ai_results = []

        # 🛡️ Ochrana pred halucináciou a formátovou anomáliou (Ak AI vráti iný počet položiek alebo neplatný typ)
        if isinstance(ai_results, dict):
            ai_results = ai_results.get('polozky') or ai_results.get('items') or []
        if not isinstance(ai_results, list) or len(ai_results) != len(polozky_data):
            logger.warning("⚠️ Peppol AI: Počet AI výsledkov nesedí s počtom XML položiek. Použijem bezpečný fallback.")
            ai_results = [{} for _ in polozky_data]
        else:
            ai_results = [item if isinstance(item, dict) else {} for item in ai_results]

        # 4. Zápis do databázy (Spojenie XML dát a AI dát)
        with transaction.atomic():
            # Vymažeme staré logy, ak nejaké boli (napr. pri opakovanom spracovaní)
            faktura.polozky_log.all().delete()
            
            # Inicializácia optimalizovaného Matchera
            from .utils import ISASItemMatcher
            matcher = ISASItemMatcher()
            
            gn_peppol = GlobalneNastavenia.objects.first()
            dph_peppol_val = getattr(gn_peppol, 'zakladna_sadzba_dph', None)
            def_dph_peppol = str(dph_peppol_val) if dph_peppol_val is not None else str(GlobalneNastavenia.get_zakladna_dph())

            for xml_data, ai_data in zip(polozky_data, ai_results):
                raw_nazov_xml = str(xml_data.get('nazov') or '').strip()
                cisty_nazov_xml = raw_nazov_xml[:250] if raw_nazov_xml and raw_nazov_xml.lower() not in ['none', 'null'] else 'Neznáma položka'
                raw_kod_xml = str(xml_data.get('kod') or xml_data.get('seriove_cislo') or '').strip()
                kod_xml = raw_kod_xml[:100] if raw_kod_xml and raw_kod_xml.lower() not in ['none', 'null', 'n/a'] else ''
                
                # EXTRAKCIA TVRDÝCH DÁT Z XML PARSERA
                raw_ean = str(xml_data.get('ean') or '').strip()
                ean_xml = raw_ean[:50] if raw_ean and raw_ean.lower() not in ['none', 'null', 'n/a'] else None
                raw_mj = str(xml_data.get('merna_jednotka') or '').strip()
                merna_jednotka_xml = raw_mj[:20] if raw_mj and raw_mj.lower() not in ['none', 'null', 'n/a'] else 'ks'

                try: 
                    val_xml_dph = xml_data.get('sadzba_dph')
                    sadzba_dph_xml = Decimal(str(val_xml_dph if val_xml_dph not in [None, ''] else def_dph_peppol))
                except: 
                    sadzba_dph_xml = None
                
                try: mnozstvo = Decimal(str(xml_data.get('mnozstvo', '1.0'))).quantize(Decimal('0.001'))
                except: mnozstvo = Decimal('1.000')
                
                try: cena = Decimal(str(xml_data.get('cena_bez_dph', '0.0'))).quantize(Decimal('0.01'))
                except: cena = Decimal('0.00')

                kat_ai = ai_data.get('kat') or default_stock_kat
                if kat_ai not in AI_CATEGORY_PROMPT_DATA:
                    kat_ai = default_stock_kat

                vyrobca_id = ai_data.get('vyrobca_id')
                typ_on = ai_data.get('typ_on')
                popis = ai_data.get('kto_som', 'Importované z Peppol XML')

                vyrobca_instancia = None
                if vyrobca_id:
                    try: vyrobca_instancia = Vyrobca_model.objects.filter(id=int(vyrobca_id)).first()
                    except: pass

                auto_predkontacia = predkontacia_sluzby if kat_ai == "Sluzba" else None

                # 🌊 ISAS WATERFALL MATCHING ENGINE (Delegované na dedikovanú triedu)
                zobrazeny_kod = kod_xml if kod_xml else cisty_nazov_xml
                najdena_polozka, skutocna_kategoria, ct_id, obj_id = matcher.find_match(ean_xml, zobrazeny_kod)

                if skutocna_kategoria:
                    kat_ai = skutocna_kategoria

                # Zistenie hlavnej kategórie z podkategórie
                hlavna_kat_ai = None
                if kat_ai == "Sluzba":
                    hlavna_kat_ai = "Služby a Práce"
                else:
                    for main_cat, subcats in CATEGORY_MAP.items():
                        for verbose_name, model_class in subcats.items():
                            if model_class.__name__ == kat_ai:
                                hlavna_kat_ai = main_cat
                                break
                        if hlavna_kat_ai: break

                LogNaskladnenia.objects.create(
                    faktura=faktura,
                    najdeny_kod_faktura=zobrazeny_kod,
                    navrhovany_nazov=cisty_nazov_xml,
                    hlavna_kategoria=hlavna_kat_ai,
                    navrhovana_kategoria=kat_ai,
                    mnozstvo=mnozstvo,
                    nakupna_cena=cena if cena > Decimal('0.00') else None,
                    navrhovany_vyrobca=vyrobca_instancia,
                    navrhovany_typ=typ_on if not najdena_polozka else None,
                    navrhovany_popis=popis if not najdena_polozka else None,
                    navrhovany_ean=ean_xml,
                    navrhovana_merna_jednotka=merna_jednotka_xml,
                    navrhovana_sadzba_dph=sadzba_dph_xml,
                    navrhovane_katalogove_cislo=kod_xml if kod_xml else None,
                    erp_predkontacia=auto_predkontacia,
                    content_type_id=ct_id,
                    object_id=obj_id
                )

            # 5. Finalizácia faktúry
            faktura.spracovane = True
            faktura.poznamka = f"{faktura.poznamka or ''}\n\n✅ [PEPPOL AI]: Sémantika položiek úspešne dokončená."
            faktura.save(update_fields=['spracovane', 'poznamka'])

        # 6. Reťazenie taskov (Rovnako ako pri PDF)
        from .tasks import posli_email_uctovnicke_task, task_vyhodnot_smart_matcher_faktura_objednavka
        
        # Prečítame surový text priamo z XML súboru, aby v ňom Smart Matcher mohol nájsť číslo objednávky!
        xml_surovy_text = "Dáta extrahované z Peppol XML."
        if faktura.xml_subor:
            try:
                faktura.xml_subor.seek(0)
                xml_surovy_text = faktura.xml_subor.read().decode('utf-8', errors='ignore')
            except Exception as e:
                logger.warning(f"Nepodarilo sa prečítať XML pre Smart Matcher: {e}")

        transaction.on_commit(lambda: task_vyhodnot_smart_matcher_faktura_objednavka.delay(faktura.id, [], xml_surovy_text))

@shared_task(base=ISASAutoHealTask)
def posli_peppol_vizualizaciu_uctovnicke_task(faktura_id):
    try:
        faktura = PrichodziaFaktura.objects.select_related('dodavatel').get(pk=faktura_id)
        gn = GlobalneNastavenia.objects.first()
        identita = FiremnaIdentita.objects.first()
        
        if not gn or not identita or not identita.email_uctovnicky or not gn.smtp_user:
            logger.warning("Chýba konfigurácia pre odoslanie e-mailu účtovníčke.")
            return

        if not faktura.vizualizacia_subor:
            logger.warning(f"Faktúra {faktura_id} nemá vygenerovanú PDF vizualizáciu.")
            return

        dodavatel_nazov = faktura.dodavatel.Nazov_spolocnosti if faktura.dodavatel else 'Neznámy dodávateľ'
        vs_str = faktura.variabilny_symbol or 'Nezistený'
        peppol_id = faktura.peppol_document_id or 'Nezistené'
        mena_symbol = getattr(faktura, 'mena', '') or get_firemna_mena()
        suma_str = f"{faktura.suma_na_uhradu:.2f} {mena_symbol}" if faktura.suma_na_uhradu else "Nezistená"
        iban_str = faktura.iban if faktura.iban else "Nezistený"
        
        base_url = (getattr(gn, 'clean_base_url', None) or getattr(settings, 'SITE_URL', '')).strip().rstrip('/')
        cesta = reverse('admin:Evidencia_prichodziafaktura_change', args=[faktura.id])
        isas_link = f"{base_url}{cesta}"
        
        predmet = f"📄 ISAS CORE | PDF Vizualizácia Peppol eFaktúry: {dodavatel_nazov} (VS: {vs_str})"
        
        uvod_text_plain = "na základe manuálneho vyžiadania v systéme ISAS CORE Vám zasielame dodatočne vygenerovanú PDF vizualizáciu k elektronickej faktúre (Peppol)."
        prilohy_text_plain = "📎 V prílohe tohto e-mailu nájdete vygenerovaný PDF dokument pre Vašu vizuálnu kontrolu."
        
        uvod_text_html = "na základe manuálneho vyžiadania v systéme <b>ISAS CORE</b> Vám zasielame dodatočne vygenerovanú PDF vizualizáciu k elektronickej faktúre (Peppol)."
        prilohy_text_html = "📎 V prílohe tohto e-mailu nájdete <b>vygenerovaný PDF dokument</b> pre Vašu vizuálnu kontrolu."
        
        payload = (ISASNotificationBuilder(nadpis='PDF VIZUALIZÁCIA eFAKTÚRY', ikona='📄', farba_temy='#3b82f6')
                   .pridaj_riadok('🏢 Dodávateľ:', dodavatel_nazov)
                   .pridaj_riadok('💰 Suma na úhradu:', suma_str)
                   .pridaj_riadok('🔢 Var. symbol:', vs_str)
                   .pridaj_riadok('🏦 IBAN:', iban_str)
                   .pridaj_riadok('🆔 Peppol ID:', peppol_id)
                   .nastav_hlavny_text(f"👋 Dobrý deň,<br><br>{uvod_text_html}<br><br>{prilohy_text_html}")
                   .nastav_upozornenie("<strong>ℹ️ UPOZORNENIE:</strong> Tento PDF dokument slúži výhradne pre ľudské čítanie. Zákonným daňovým dokladom zostáva <b>originálny XML súbor</b>, ktorý Vám už bol systémom automaticky odoslaný pri prijatí faktúry.", 'oranzove')
                   .nastav_tlacidlo('Otvoriť faktúru v systéme ISAS CORE', isas_link)
                   .build())

        faktura.vizualizacia_subor.seek(0)

        attachments = [{
            'filename': faktura.vizualizacia_subor.name.split('/')[-1],
            'content': faktura.vizualizacia_subor.read(),
            'mimetype': 'application/pdf'
        }]

        ISASNotifier.send(
            payload=payload,
            to_email=identita.email_uctovnicky,
            send_tg=False,
            send_mail=True,
            send_discord=False,
            attachments=attachments
        )

        logger.info(f"✅ Peppol vizualizácia (iba PDF) pre FA {faktura_id} úspešne odoslaná.")
    except Exception as e:
        logger.error(f"❌ Chyba pri odosielaní Peppol vizualizácie: {e}")

@shared_task(base=ISASAutoHealTask)
def task_odosli_notifikaciu_nizky_stav(nazov, kategoria_s_emoji, podkategoria, novy_pocet, jednotka, limit, typ_notifikacie):
    """
    📦 ISAS CORE: Formátuje a odosiela notifikáciu o nízkom stave zásob.
    """
    # Kontrola, či sú notifikácie globálne zapnuté
    nastavenie, _ = GlobalneNastavenieSkladu.objects.get_or_create(pk=1)
    if not nastavenie.zapnute_notifikacie:
        return
        
    hlavny_sklad = Sklad.objects.first()
    nazov_hlavneho = f"Hlavný sklad ({hlavny_sklad.nazov})" if hlavny_sklad and hlavny_sklad.nazov and hlavny_sklad.nazov.lower() != "hlavný sklad" else "Hlavný sklad"
    
    if typ_notifikacie == 'VYPREDANE':
        payload = (ISASNotificationBuilder(nadpis='SKLAD VYPRÁZDNENÝ', ikona='🚨', farba_temy='#ef4444')
                   .pridaj_riadok('🏢 Sklad:', nazov_hlavneho)
                   .pridaj_riadok('🗂️ Kategória:', f"{kategoria_s_emoji} / {podkategoria}")
                   .pridaj_riadok('⚙️ Položka:', nazov)
                   .pridaj_riadok('📉 Aktuálny stav:', f"0 {jednotka}")
                   .nastav_ai_poznamku('Položka bola automaticky zaradená do košíka na okamžité doobjednanie.')
                   .build())
    else:
        payload = (ISASNotificationBuilder(nadpis='KRITICKÝ LIMIT ZÁSOB', ikona='⚠️', farba_temy='#f59e0b')
                   .pridaj_riadok('🏢 Sklad:', nazov_hlavneho)
                   .pridaj_riadok('🗂️ Kategória:', f"{kategoria_s_emoji} / {podkategoria}")
                   .pridaj_riadok('⚙️ Položka:', nazov)
                   .pridaj_riadok('📊 Aktuálny stav:', f"{novy_pocet} {jednotka} (Limit: {limit} {jednotka})")
                   .nastav_ai_poznamku('Položka aktuálne čaká v košíku na potvrdenie doobjednávky.')
                   .build())

    ISASNotifier.send(
        payload=payload,
        send_tg=True,
        send_mail=True,
        send_discord=True
    )

@shared_task(base=ISASAutoHealTask)
def uloha_odoslat_notifikacie_adminovi_task(ids, poslat_tg=True, poslat_mail=True, poslat_discord=True):
    """Univerzálny task pre odoslanie notifikácií pre jednu alebo viacero úloh."""    
    if not isinstance(ids, (list, tuple)):
        ids = [ids]
        
    try:
        gn = GlobalneNastavenia.objects.first()
        if not gn: return
        
        base_url = (getattr(gn, 'clean_base_url', None) or getattr(settings, 'SITE_URL', '')).strip().rstrip('/')

        ulohy = Uloha.objects.prefetch_related('technici', 'zariadenia', 'typ_servisu', 'pouzite_polozky__content_type', 'pouzite_polozky__content_object').select_related('zakaznik').filter(id__in=ids)
        
        for uloha in ulohy:
            cesta = reverse('admin:Evidencia_uloha_change', args=[uloha.id])
            url_detail = f"{base_url}{cesta}"
            
            zamestnanci_list = ", ".join([html.escape(str(t)) for t in uloha.technici.all()]) or "Neuvedení"
            zariadenia_list = ", ".join([html.escape(str(z)) for z in uloha.zariadenia.all()]) or "Neuvedené"
            typy_ciste = [str(tp).replace('[Fix]', '').replace('(Fix)', '').strip() for tp in uloha.typ_servisu.all()]
            typy_list = ", ".join([html.escape(tp) for tp in typy_ciste]) or "Neuvedené"
            
            zakaznik_nazov = str(uloha.zakaznik) if uloha.zakaznik else "Neuvedený"
            bezpecny_zakaznik = html.escape(zakaznik_nazov)
            
            polozky = uloha.pouzite_polozky.all()
            material_sekcia = format_material_list_for_notification(polozky)

            info_text = uloha.blizsie_informacie if uloha.blizsie_informacie else 'ℹ️ Neuvedené'

            hlavny_text = f"<b>📝 Detaily k úlohe:</b><br>{html.escape(info_text)}<br><br>"
            if material_sekcia:
                hlavny_text += material_sekcia

            payload = (ISASNotificationBuilder(nadpis='ÚLOHA ÚSPEŠNE DOKONČENÁ', ikona='🟢', farba_temy='#10b981')
                       .pridaj_riadok('🏢 Zákazník:', bezpecny_zakaznik)
                       .pridaj_riadok('🛠 Typ práce:', typy_list)
                       .pridaj_riadok('⚙️ Zariadenia:', zariadenia_list)
                       .pridaj_riadok('👨‍🔧 Zamestnanci:', zamestnanci_list)
                       .nastav_hlavny_text(hlavny_text)
                       .nastav_tlacidlo('Otvoriť detail v systéme ISAS CORE', url_detail)
                       .nastav_ai_poznamku('Úloha bola uzatvorená a dáta boli synchronizované.')
                       .build())

            ISASNotifier.send(
                payload=payload,
                send_tg=poslat_tg,
                send_mail=poslat_mail,
                send_discord=poslat_discord
            )

    except Exception as e:
        logger.error(f"Chyba pri odosielaní notifikácie o úlohe: {e}")

@shared_task(base=ISASAutoHealTask)
def vygeneruj_odborny_text_ai_task(zaznam_id):
    lock_id = f"lock_ai_text_{zaznam_id}"
    
    with isas_task_execution_guard(lock_id, timeout=120) as (acquired, gn, identita):
        if not acquired:
            return

        try:
            zaznam = ServisnyZaznam.objects.select_related('zakaznik', 'zariadenie__vyrobca').prefetch_related('pouzite_polozky_zaznamu').get(pk=zaznam_id)
        except ServisnyZaznam.DoesNotExist:
            return

        api_key = get_gemini_api_key()
        gemini_model = getattr(settings, 'GEMINI_MODEL', None)
    
        if not api_key or not gemini_model:
            logger.warning(f"⚠️ [vygeneruj_odborny_text_ai_task] Záznam ID {zaznam_id}: Chýba GEMINI_API_KEY alebo GEMINI_MODEL. Používam pôvodný text technika.")
            zaznam.formalne_zhodnotenie_ai = zaznam.poruchy
            zaznam.save(update_fields=['formalne_zhodnotenie_ai'])
            return

        client = genai.Client(api_key=api_key)
        historia_text = zaznam.get_historia_stroja()
    
        zakaznik_meno = zaznam.zakaznik.Nazov_spolocnosti if zaznam.zakaznik else "Neznámy zákazník"
        zariadenie_meno = "Neznáme zariadenie"
        if zaznam.zariadenie:
            vyrobca = zaznam.zariadenie.vyrobca.nazov_vyrobcu if zaznam.zariadenie.vyrobca else ""
            zariadenie_meno = f"{vyrobca} {zaznam.zariadenie.model_zariadenia} (Sériové číslo: {zaznam.zariadenie.vyrobne_cislo})".strip()
        
        motohodiny = zaznam.meter_display if zaznam.stav_motohodin is not None else "Nezistené"
        now = timezone.now()
        dnesny_datum = timezone.localtime(now).strftime('%d.%m.%Y') if timezone.is_aware(now) else now.strftime('%d.%m.%Y')
    
        materialy = zaznam.pouzite_polozky_zaznamu.all()
        if materialy.exists():
            pouzite_diely = ", ".join([f"{m.pocet} {m.merna_jednotka_zobrazenie} - {m.cisty_nazov}" for m in materialy])
        else:
            pouzite_diely = "🚫 Žiadny materiál nebol spotrebovaný."

        nazov_poskytovatela = "naša servisná spoločnosť"
        try:
            if apps.ready:
                FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
                fi = FiremnaIdentitaModel.objects.first() if FiremnaIdentitaModel else None
                if fi and fi.nazov_spolocnosti:
                    nazov_poskytovatela = fi.nazov_spolocnosti.strip()
        except Exception as err:
            logger.warning("vygeneruj_odborny_text_ai_task: Nepodarilo sa načítať identitu firmy: %s", err)

        meter_genitiv = getattr(zaznam, 'meter_label_genitiv', None) or get_industry_term('meter_label_genitiv', 'prevádzkových hodín')
        term_zariadenie = get_industry_term('zariadenie_fallback_nazov', 'zariadenie').lower()

        prompt = (
            f"Si špičkový technický riaditeľ a servisný inžinier v spoločnosti {nazov_poskytovatela}. "
            f"Tvojou úlohou je napísať formálny 'Výkaz o servisnom zásahu' určený pre zákazníka.\n\n"
            f"🚨 KRITICKÉ PRAVIDLO: MÁŠ PRÍSNY ZÁKAZ POUŽÍVAŤ ZÁTVORKY AKO [Typ zariadenia] ALEBO SI VYMÝŠĽAŤ DÁTA. "
            f"TU SÚ SKUTOČNÉ DÁTA ZO SYSTÉMU, KTORÉ MUSÍŠ DOSLOVA POUŽIŤ V TEXTE:\n"
            f"- Zákazník: {zakaznik_meno}\n"
            f"- Zariadenie: {zariadenie_meno}\n"
            f"- {zaznam.meter_label}: {motohodiny}\n"
            f"- Dnešný dátum: {dnesny_datum}\n"
            f"- Použitý materiál / diely: {pouzite_diely}\n\n"
            f"Tu sú rýchle, možno gramaticky nesprávne poznámky technika z dnešného výjazdu: '{zaznam.poruchy}'.\n"
            f"Tu je stručná história posledných servisov zariadenia: '{historia_text}'.\n\n"
            f"ŠTRUKTÚRA TEXTU (striktne dodržuj v tomto poradí):\n"
            f"1. Formálny úvod: Uveď, že bol dňa {dnesny_datum} vykonaný zásah na zariadení {zariadenie_meno} pre zákazníka {zakaznik_meno} a napíš aktuálny stav pre {meter_genitiv}.\n"
            f"2. Zoznam úkonov: Z poznámok technika urob profesionálny zoznam krokov pomocou odrážok (použi znak pomlčky '- '). "
            f"Ak bol použitý materiál ({pouzite_diely}), logicky ho do úkonov včleň (napr. že bol aplikovaný/vymenený).\n"
            f"3. Analýza systémom ISAS CORE: Zhodnoť históriu. Ak naznačuje opakujúcu sa poruchu alebo anomáliu, zákazníka upozorni na možné riziká a navrhni riešenie. Ak je história bez závad, len odborne skonštatuj, že {term_zariadenie} nevykazuje žiadne anomálie.\n"
            f"4. Záver: Krátka veta o tom, že zariadenie bolo odskúšané a odovzdané do prevádzky.\n\n"
            f"PRAVIDLÁ FORMÁTOVANIA:\n"
            f"- NEPOUŽÍVAJ markdown hviezdičky (**). Zákazník dostane PDF, text musí byť čistý plain-text bez formátovacích znakov.\n"
            f"- Nepíš žiadne vysvetlivky pre mňa, vráť PRIAMO iba finálny text výkazu."
    )
    
        try:
            response = client.models.generate_content(
                model=gemini_model, 
                contents=[prompt],
                config=types.GenerateContentConfig(thinking_config=types.ThinkingConfig(thinking_level=getattr(settings, 'GEMINI_THINKING_LEVEL', 'MEDIUM') or 'MEDIUM'))
            )
            vysledok = (response.text or "").replace('**', '').replace('*', '').strip()
            zaznam.formalne_zhodnotenie_ai = vysledok
            zaznam.save(update_fields=['formalne_zhodnotenie_ai'])
        except Exception as e:
            logger.error(f"🤖 AI Error: {e}")
            zaznam.formalne_zhodnotenie_ai = zaznam.poruchy
            zaznam.save(update_fields=['formalne_zhodnotenie_ai'])

@shared_task(base=ISASAutoHealTask)
def zaznam_odoslat_notifikacie_adminovi_task(ids, poslat_tg=True, poslat_mail=True, poslat_discord=True):
    """Univerzálny task pre odoslanie notifikácií pre jeden alebo viacero záznamov."""
    # Podpora pre single ID aj list IDčok
    if not isinstance(ids, (list, tuple)):
        ids = [ids]
        
    try:
        gn = GlobalneNastavenia.objects.first()
        if not gn: return
        
        base_url = (getattr(gn, 'clean_base_url', None) or getattr(settings, 'SITE_URL', '')).strip().rstrip('/')
        zaznamy = ServisnyZaznam.objects.prefetch_related('technici', 'typ_servisu', 'pouzite_polozky_zaznamu__content_type', 'pouzite_polozky_zaznamu__content_object').select_related('zakaznik', 'zariadenie').filter(id__in=ids)
        
        for zaznam in zaznamy:
            cesta = reverse('admin:Evidencia_servisnyzaznam_change', args=[zaznam.id])
            url_detail = f"{base_url}{cesta}"
            
            zamestnanci_list = ", ".join([html.escape(str(t)) for t in zaznam.technici.all()]) or "Neuvedení"
            zariadenie_text = html.escape(str(zaznam.zariadenie)) if zaznam.zariadenie else "Neuvedené"
            typy_ciste = [str(tp).replace('[Fix]', '').replace('(Fix)', '').strip() for tp in zaznam.typ_servisu.all()]
            typy_list = ", ".join([html.escape(tp) for tp in typy_ciste]) or "Neuvedené"
            
            zakaznik_nazov = str(zaznam.zakaznik) if zaznam.zakaznik else "Neuvedený"
            bezpecny_zakaznik = html.escape(zakaznik_nazov)
            
            mh_celkove = zaznam.meter_display if zaznam.stav_motohodin is not None else "Nezadané"
            mh_zataz = f" ({zaznam.meter_load_label}: {zaznam.meter_load_display})" if getattr(zaznam, 'motohodiny_v_zatazi', None) is not None else ""
            mh_text = f"{mh_celkove}{mh_zataz}"
            
            polozky = zaznam.pouzite_polozky_zaznamu.all()
            material_sekcia = format_material_list_for_notification(polozky)

            info_text = zaznam.poruchy if zaznam.poruchy else 'ℹ️ Neuvedené'
            hodnoty_text = zaznam.namerane_hodnoty if zaznam.namerane_hodnoty else "ℹ️ Žiadne merania"
            
            safe_info_html = html.escape(info_text).replace('\n', '<br>')
            safe_hodnoty_html = html.escape(hodnoty_text).replace('\n', '<br>')
            
            upozornenie_hw_mail = "🚨 <b>POZOR:</b> Zamestnanec hlási zmenu hardvérových parametrov! Skontrolujte kartu zariadenia." if zaznam.zmena_hardveru else ""

            hlavny_text = ""
            hlavny_text += f"<b>📝 Poznámky/Poruchy:</b><br>{safe_info_html}<br><br>"
            hlavny_text += f"<b>📊 Hodnoty po servise:</b><br>{safe_hodnoty_html}<br><br>"
            
            if material_sekcia:
                hlavny_text += material_sekcia

            builder = (ISASNotificationBuilder(nadpis='SERVISNÝ ZÁZNAM DOKONČENÝ', ikona='🟢', farba_temy='#10b981')
                       .pridaj_riadok('🏢 Zákazník:', bezpecny_zakaznik)
                       .pridaj_riadok('🛠 Typ práce:', typy_list)
                       .pridaj_riadok('⚙️ Zariadenia:', zariadenie_text)
                       .pridaj_riadok(f'⏱ {zaznam.meter_label}:', mh_text)
                       .pridaj_riadok('👨‍🔧 Zamestnanci:', zamestnanci_list)
                       .nastav_hlavny_text(hlavny_text)
                       .nastav_tlacidlo('Otvoriť detail v systéme ISAS CORE', url_detail)
                       .nastav_ai_poznamku('Servisný záznam bol uzatvorený a synchronizovaný.'))
            if upozornenie_hw_mail:
                builder.nastav_upozornenie(upozornenie_hw_mail, 'cervene')

            ISASNotifier.send(
                payload=builder.build(),
                send_tg=poslat_tg,
                send_mail=poslat_mail,
                send_discord=poslat_discord
            )

    except Exception as e:
        logger.error(f"Chyba pri odosielaní notifikácie o zázname: {e}")

@shared_task(base=ISASAutoHealTask)
def task_vygeneruj_a_odosli_objednavky(ids, user_id, base_url):
    """Celery task pre Playwright PDF a SMTP odosielanie objednávok"""
    User = get_user_model()
    user = User.objects.get(id=user_id)
    gn = GlobalneNastavenia.objects.first()
    identita = FiremnaIdentita.objects.first()
    
    if not gn or not gn.smtp_user:
        logger.error("SMTP nie je nastavené. Ruším task.")
        return

    # 🔒 1. APLIKAČNÝ ZÁMOK (MUTEX): Ochrana pre dlhotrvajúce Celery tasky
    # Zamykáme KAŽDÚ položku samostatne, aby sme zabránili prieniku množín (napr. [1,2] a [2,3])
    aquired_locks = []
    for item_id in ids:
        lock_key = f"lock_objednavka_polozka_{item_id}"
        if cache.add(lock_key, 'locked', timeout=300):
            aquired_locks.append(lock_key)
            
    if len(aquired_locks) != len(ids):
        # Niekto iný už spracováva aspoň jednu z týchto položiek. Uvoľníme naše zámky a zrušíme task.
        for lock in aquired_locks:
            cache.delete(lock)
        logger.warning(f"Konflikt súbehu: Niektoré položky z {ids} sa už spracovávajú. Zabraňujem duplicite.")
        return

    try:
        # Tu už nepotrebujeme select_for_update(), Redis/Cache nás chráni
        pripravene_polozky = KosikSPolozkamiNaDoobjednanie.objects.filter(
            pk__in=ids, objednane=False
        ).select_related('dodavatel', 'filialka')
        
        if not pripravene_polozky:
            return

        objednavky = {}

        # Hromadné prednačítanie kategórií a výrobcov (0 dopytov v cykle!)
        kody_na_hladanie = set()
        for p in pripravene_polozky:
            cisty = ocisti_nazov_od_jednotky(str(p.nazov_polozky or '').strip())
            if cisty: kody_na_hladanie.add(cisty)

        meta_cache = {}
        if kody_na_hladanie:
            for cat_group in CATEGORY_MAP.values():
                for model_class in cat_group.values():
                    dostupne_stlpce = [f.name for f in model_class._meta.fields]
                    q_obj = Q()
                    for stlpec in ['seriove_cislo', 'nazov', 'nazov_polozky', 'model_zariadenia', 'katalogove_cislo', 'sku']:
                        if stlpec in dostupne_stlpce:
                            q_obj |= Q(**{f"{stlpec}__in": list(kody_na_hladanie)})
                    
                    if q_obj:
                        relacne_polia = [f.name for f in model_class._meta.fields if f.is_relation and f.related_model and f.related_model.__name__ == 'DatabazaVyrobcov']
                        qs = model_class.objects.filter(q_obj)
                        if relacne_polia:
                            qs = qs.select_related(relacne_polia[0])
                            
                        for obj in qs:
                            najdeny_kod = None
                            for stlpec in ['seriove_cislo', 'nazov', 'nazov_polozky', 'model_zariadenia', 'katalogove_cislo', 'sku']:
                                if stlpec in dostupne_stlpce and getattr(obj, stlpec) in kody_na_hladanie:
                                    najdeny_kod = getattr(obj, stlpec)
                                    break
                            
                            if najdeny_kod and najdeny_kod not in meta_cache:
                                cat_name = getattr(model_class._meta, 'verbose_name', 'Položka')
                                vyrobca_nazov = ""
                                if relacne_polia and getattr(obj, relacne_polia[0]):
                                    vyrobca_nazov = str(getattr(obj, relacne_polia[0])).strip()
                                if not vyrobca_nazov:
                                    for kandidat in ['vyrobca_dielu', 'vyrobca', 'znacka', 'brand']:
                                        if kandidat in dostupne_stlpce and getattr(obj, kandidat):
                                            vyrobca_nazov = str(getattr(obj, kandidat)).strip()
                                            break
                                meta_cache[najdeny_kod] = {'cat': cat_name, 'vyrobca': vyrobca_nazov}

        for polozka in pripravene_polozky:
            if polozka.dodavatel and polozka.dodavatel.email:
                mail = polozka.dodavatel.email
                sklad_id = polozka.filialka.id if polozka.filialka else 0
                group_key = f"{mail}_WH{sklad_id}"
                
                if group_key not in objednavky:
                    full_oslovenie = polozka.dodavatel.get_email_greeting()
                    
                    objednavky[group_key] = {
                        'dodavatel_obj': polozka.dodavatel,
                        'filialka_obj': polozka.filialka,
                        'mail': mail,
                        'oslovenie': full_oslovenie,
                        'polozky_pre_pdf': [],
                        'id_poloziek': []
                    }

                cely_nazov = str(polozka.nazov_polozky or '').strip()
                cisty_kod = ocisti_nazov_od_jednotky(cely_nazov)
                
                # Čítame z pamäte, žiadne DB dopyty v cykle!
                cached_data = meta_cache.get(cisty_kod, {})
                kategoria_nazov = cached_data.get('cat', 'Položka')
                vyrobca_nazov = cached_data.get('vyrobca', '')

                objednavky[group_key]['polozky_pre_pdf'].append({
                    'kategoria': kategoria_nazov,
                    'nazov': cisty_kod,
                    'vyrobca': vyrobca_nazov,
                    'mnozstvo': polozka.mnozstvo_na_objednanie,
                    'merna_jednotka': getattr(polozka, 'merna_jednotka', None) or 'ks'
                })
                objednavky[group_key]['id_poloziek'].append(polozka.id)

        if not objednavky:
            return

        nazov_firmy = identita.nazov_spolocnosti if identita and identita.nazov_spolocnosti else "Naša spoločnosť"
        
        fallback_meno = getattr(identita, 'vystavuje_objednavky', '') or getattr(identita, 'konatel', '') or "Nákupné oddelenie"
        odosielatel_meno = get_safe_employee_name(user, fallback_text=fallback_meno)

        uspesne_id_poloziek = []
        
        for group_key, data_objednavky in objednavky.items():
            email_dodavatela = data_objednavky['mail']
            filialka_obj = data_objednavky['filialka_obj']
            dodavatel_obj = data_objednavky['dodavatel_obj']

            try:
                # 🔒 2. ZÁMOK NA DODÁVATEĽA: Atomická rezervácia sekvencie, PDF a prepojenie košíka
                with transaction.atomic():
                    # Uzamkneme záznam dodávateľa po celú dobu tvorby dokladu (prevencia race conditions)
                    dodavatel_locked = type(dodavatel_obj).objects.select_for_update().get(pk=dodavatel_obj.pk)
                    
                    max_poradie = OdoslanaObjednavka.objects.filter(dodavatel=dodavatel_locked).aggregate(Max('poradove_cislo_dodavatela'))['poradove_cislo_dodavatela__max']
                    nove_poradie = (max_poradie or 0) + 1
                    rok = timezone.now().strftime('%Y')
                    ref_cislo = f"OBJ-{rok}-{dodavatel_locked.id}-{nove_poradie:03d}" 
                    datum_dnes = timezone.now().strftime('%d.%m.%Y')

                    nova_objednavka = OdoslanaObjednavka(
                        dodavatel=dodavatel_locked,
                        cislo_objednavky=ref_cislo,
                        poradove_cislo_dodavatela=nove_poradie,
                        celkovy_pocet_poloziek=len(data_objednavky['polozky_pre_pdf'])
                    )
                    nazov_suboru = f"{ref_cislo}.pdf"
                    
                    context = {
                        'identita': identita,
                        'dodavatel': dodavatel_obj,
                        'filialka': filialka_obj,
                        'polozky': data_objednavky['polozky_pre_pdf'],
                        'cislo_objednavky': ref_cislo,
                        'datum_vystavenia': datum_dnes,
                        'vystavil_meno': odosielatel_meno,
                        'vystavil_user': user,
                    }

                    base_url = (getattr(gn, 'clean_base_url', None) or base_url or getattr(settings, 'SITE_URL', '')).strip().rstrip('/')

                    pdf_bytes, _ = ISASPDFBuilder.generate_and_save(
                        template_name='admin/pdf_objednavka_dodavatelovi.html',
                        context=context,
                        base_url=base_url,
                        file_name=nazov_suboru,
                        instance=nova_objednavka,
                        file_field_name='pdf_dokument',
                        save_instance=False
                    )
                    nova_objednavka.save()

                    # 🟢 Prepojenie košíka prebieha vnútri tej istej atomickej transakcie
                    KosikSPolozkamiNaDoobjednanie.objects.filter(id__in=data_objednavky['id_poloziek']).update(suvisiaca_objednavka=nova_objednavka)

                # 🧠 ISAS DYNAMIC DATA PREPARATION & ADDRESS FORMATTING
                hlavny_sklad = Sklad.objects.first()
                nazov_hlavneho = f"Hlavný sklad ({hlavny_sklad.nazov})" if hlavny_sklad and hlavny_sklad.nazov and hlavny_sklad.nazov.lower() != "hlavný sklad" else "Hlavný sklad"
                sklad_nazov = filialka_obj.nazov if filialka_obj else nazov_hlavneho
                sklad_text = f" ({sklad_nazov})"
                pocet_poloziek = len(data_objednavky['polozky_pre_pdf'])
                
                # Čistenie a formátovanie adresy skladu do jedného riadku bez prázdnych riadkov
                if filialka_obj and getattr(filialka_obj, 'adresa', None):
                    sklad_adresa = ", ".join([line.strip() for line in filialka_obj.adresa.splitlines() if line.strip()])
                elif identita and getattr(identita, 'adresa_prevadzky', None):
                    sklad_adresa = ", ".join([line.strip() for line in identita.adresa_prevadzky.splitlines() if line.strip()])
                elif identita:
                    ulica_part = f"{identita.sidlo_ulica or ''} {identita.sidlo_cislo or ''}".strip()
                    mesto_part = f"{identita.sidlo_psc or ''} {identita.sidlo_mesto or ''}".strip()
                    sklad_adresa = f"{ulica_part}, {mesto_part}".strip(", ")
                else:
                    sklad_adresa = ""

                miesto_dodania_komplet = f"{sklad_nazov} - {sklad_adresa}" if sklad_adresa else sklad_nazov
                aktualny_rok = timezone.now().year
                logo_url = f"{base_url}{identita.logo.url}" if identita and identita.logo and base_url else None

                predmet = f"📦 Záväzná objednávka č. {ref_cislo}{sklad_text} | {nazov_firmy}"
            
                email_na_faktury = (identita.email_fakturacia or identita.email_vseobecny or 'Nezadaný') if identita else 'Nezadaný'
            
                oslovenie_text = data_objednavky['oslovenie']
                
                tlacidlo = None
                if identita and identita.web:
                    tlacidlo = {'text': 'Navštíviť našu stránku', 'url': identita.web}

                builder = (ISASNotificationBuilder(nadpis=f'ZÁVÄZNÁ OBJEDNÁVKA Č. {ref_cislo}', ikona='📦', farba_temy='#2563eb')
                           .nastav_logo(logo_url)
                           .pridaj_riadok('Objednávateľ:', nazov_firmy)
                           .pridaj_riadok('Vystavil:', odosielatel_meno)
                           .pridaj_riadok('Miesto dodania:', miesto_dodania_komplet)
                           .pridaj_riadok('Počet položiek:', f"{pocet_poloziek} ks")
                           .pridaj_riadok('Faktúry zasielajte na:', email_na_faktury)
                           .nastav_hlavny_text(f"{oslovenie_text}<br><br>v prílohe tohto e-mailu Vám zasielame záväznú objednávku vo formáte PDF.<br><br>Prosíme o potvrdenie prijatia tejto objednávky a zaslanie predpokladaného termínu dodania."))
                if tlacidlo:
                    builder.nastav_tlacidlo(tlacidlo['text'], tlacidlo['url'])
                payload = builder.build()

                attachments = [{
                    'filename': nazov_suboru,
                    'content': pdf_bytes,
                    'mimetype': 'application/pdf'
                }]

                ISASNotifier.send(
                    payload=payload,
                    to_email=email_dodavatela,
                    send_tg=False,
                    send_mail=True,
                    send_discord=False,
                    attachments=attachments
                )
            
                uspesne_id_poloziek.extend(data_objednavky['id_poloziek'])
            except Exception as e:
                logger.error(f"Chyba pri objednavke pre {email_dodavatela}: {e}")
    finally:
        # 🔓 UVOĽNENIE ZÁMKU: Vždy uvoľníme Mutex, aj keby task spadol na chybe
        for lock in aquired_locks:
            cache.delete(lock)

    if uspesne_id_poloziek:
        KosikSPolozkamiNaDoobjednanie.objects.filter(id__in=uspesne_id_poloziek).update(objednane=True, datum_odoslania=timezone.now())

@shared_task(base=ISASAutoHealTask)
def task_vygeneruj_a_odosli_erp_export(model_name, ids, user_id):
    """Univerzálny Celery task pre asynchrónny export do ERP (XML + ZIP + SMTP)"""    
    lock_id = f"lock_erp_export_{model_name}_{hashlib.md5(str(sorted(ids)).encode()).hexdigest()}"
    if not cache.add(lock_id, 'locked', timeout=300):
        return
    
    Model = apps.get_model('Evidencia', model_name)   
    GlobalneNastavenia = apps.get_model('ISASaplikacia', 'GlobalneNastavenia')
    FiremnaIdentita = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
    ERPExportNastavenia = apps.get_model('Evidencia', 'ERPExportNastavenia')
    
    has_erp_status = False
    try:
        # 🛡️ DEFENSIVE: Overenie dostupnosti ERP generátora EŠTE PRED zamknutím databázových riadkov
        generator = ERPFactory.get_generator()
        file_prefix = generator.get_file_prefix()

        model_fields = {f.name for f in Model._meta.get_fields()}
        has_erp_status = 'erp_export_status' in model_fields

        with transaction.atomic():
            # 1. ZAMKNUTIE DÁT: select_for_update(of=('self',)) garantuje uzamknutie len hlavnej tabuľky
            zaznamy_qs = Model.objects.select_for_update(of=('self',)).filter(id__in=ids)
            if has_erp_status:
                zaznamy_qs = zaznamy_qs.exclude(erp_export_status='EXPORTED')
            
            # Optimalizácia N+1 dopytov
            if model_name == 'VystavenaFaktura':
                zaznamy_qs = zaznamy_qs.select_related('zakaznik').prefetch_related('polozky')
            elif model_name == 'PrichodziaFaktura':
                zaznamy_qs = zaznamy_qs.select_related('dodavatel').prefetch_related('polozky_log')
            elif model_name == 'Tankovanie':
                zaznamy_qs = zaznamy_qs.select_related('vozidlo', 'cerpacia_stanica', 'vodič', 'osoba')
            elif model_name == 'EvidenciaUhrad':
                zaznamy_qs = zaznamy_qs.select_related('vystavena_faktura', 'prijata_faktura')

            zaznamy = list(zaznamy_qs)
            pocet_zaznamov = len(zaznamy)

            if pocet_zaznamov == 0:
                logger.info(f"Žiadne nové záznamy na export pre model {model_name}.")
                # 🛡️ DEFENSIVE: Vrátenie stavu späť na NEW, aby záznamy neostali navždy visieť v PROCESSING
                if has_erp_status:
                    Model.objects.filter(id__in=ids, erp_export_status='PROCESSING').update(erp_export_status='NEW')
                return
                
            gn = GlobalneNastavenia.objects.first()
            identita = FiremnaIdentita.objects.first()
            erp_nastavenia = ERPExportNastavenia.objects.first()
            
            if model_name in ['Zamestnanec', 'SukromnaOsoba', 'Dodavatel', 'Zakaznik']:
                xml_string = generator.vygeneruj_adresare_xml(zaznamy)
                typ_exportu, subor_nazov = "Adresár", f"{file_prefix}_adresar"
            elif model_name == 'PrichodziaFaktura':
                xml_string = generator.vygeneruj_prijate_faktury_xml(zaznamy)
                typ_exportu, subor_nazov = "Prijaté faktúry", f"{file_prefix}_prijate"
            elif model_name == 'VystavenaFaktura':
                xml_string = generator.vygeneruj_faktury_xml(zaznamy)
                typ_exportu, subor_nazov = "Vystavené faktúry", f"{file_prefix}_vystavene"
            elif model_name == 'EvidenciaUhrad':
                xml_string = generator.vygeneruj_uhrady_xml(zaznamy)
                typ_exportu, subor_nazov = "Úhrady faktúr", f"{file_prefix}_uhrady"
            elif model_name == 'Tankovanie':
                xml_string = generator.vygeneruj_tankovania_xml(zaznamy)
                typ_exportu, subor_nazov = "Tankovania", f"{file_prefix}_tankovania"
            else:
                logger.error(f"Neznámy model pre ERP export: {model_name}")
                return

            # 3. VYTVORENIE ZIP ARCHÍVU
            export_dir = os.path.join(settings.MEDIA_ROOT, 'erp_exporty')
            os.makedirs(export_dir, exist_ok=True)
            now = timezone.now()
            cas_suboru = timezone.localtime(now).strftime('%Y%m%d_%H%M%S') if timezone.is_aware(now) else now.strftime('%Y%m%d_%H%M%S')
            zip_filename = f"ISAS_Export_{model_name}_{cas_suboru}.zip"
            zip_filepath = os.path.join(export_dir, zip_filename)
            
            # Dynamicky zistíme príponu podľa zvoleného systému (Pohoda = xml, Omega = txt)
            file_ext = getattr(generator, 'get_file_extension', lambda: 'xml')()
            # 🛡️ DEFENSIVE: Ak generátor vrátil str, bezpečne enkódujeme do UTF-8; bajty zapisujeme napriamo
            data_na_zapis = xml_string if isinstance(xml_string, bytes) else xml_string.encode('utf-8')
            with zipfile.ZipFile(zip_filepath, 'w', zipfile.ZIP_DEFLATED) as zipf:
                zipf.writestr(f"{subor_nazov}_{cas_suboru}.{file_ext}", data_na_zapis)
                
            # 4. ODOSLANIE E-MAILU
            if identita and getattr(identita, 'email_uctovnicky', None) and gn and gn.smtp_user and erp_nastavenia and erp_nastavenia.auto_odosielat_mailom:
                nazov_firmy = identita.nazov_spolocnosti if identita.nazov_spolocnosti else "Naša spoločnosť"
                base_url = (getattr(gn, 'clean_base_url', None) or getattr(settings, 'SITE_URL', '')).strip().rstrip('/')
                erp_nazov = erp_nastavenia.get_erp_system_display()
                aktualny_cas = timezone.localtime(now).strftime('%d.%m.%Y %H:%M') if timezone.is_aware(now) else now.strftime('%d.%m.%Y %H:%M')
                
                builder = (ISASNotificationBuilder(nadpis='EXPORT ÚČTOVNÝCH DÁT', ikona='📦', farba_temy='#3b82f6')
                           .pridaj_riadok('Spoločnosť:', nazov_firmy)
                           .pridaj_riadok('Cieľový ERP systém:', erp_nazov)
                           .pridaj_riadok('Typ dát:', typ_exportu)
                           .pridaj_riadok('Dátum exportu:', aktualny_cas)
                           .pridaj_riadok('Počet záznamov v súbore:', f"{pocet_zaznamov}")
                           .nastav_hlavny_text("Dobrý deň,<br><br>v prílohe tohto e-mailu Vám zasielame vygenerovaný dátový ZIP archív pre import do Vášho účtovného programu.<br><br>V prípade akýchkoľvek technických otázok nás neváhajte kontaktovať."))
                if base_url:
                    builder.nastav_tlacidlo('Otvoriť systém ISAS CORE', base_url)
                payload = builder.build()

                with open(zip_filepath, 'rb') as f:
                    zip_content = f.read()

                attachments = [{
                    'filename': zip_filename,
                    'content': zip_content,
                    'mimetype': 'application/zip'
                }]

                ISASNotifier.send(
                    payload=payload,
                    to_email=identita.email_uctovnicky,
                    send_tg=False,
                    send_mail=True,
                    send_discord=False,
                    attachments=attachments
                )

            # 5. ZÁPIS DO DB: Dynamické priradenie polí podľa toho, či ich model fyzicky obsahuje
            update_data = {}
            if has_erp_status:
                update_data['erp_export_status'] = 'EXPORTED'
            if 'erp_last_exported_at' in model_fields:
                update_data['erp_last_exported_at'] = timezone.now()
            if update_data:
                Model.objects.filter(id__in=ids).update(**update_data)

    except Exception as e:
        logger.error(f"Kritická chyba v Celery tasku pre {model_name}: {e}")
        # Ak task zlyhá, transakcia sa už rollbackla. 
        # Musíme ale manuálne vrátiť status z 'PROCESSING' späť na 'NEW', 
        # inak zostanú záznamy navždy zablokované v admine!
        try:
            if has_erp_status:
                Model.objects.filter(id__in=ids, erp_export_status='PROCESSING').update(erp_export_status='NEW')
        except Exception as unlock_error:
            logger.error(f"Nepodarilo sa odomknúť záznamy po chybe: {unlock_error}")
            
        raise e
    finally:
        cache.delete(lock_id)

@shared_task(base=ISASAutoHealTask)
def task_hromadne_ai_spracovanie_faktur(ids):
    """Celery task pre hromadné vyťaženie dát z faktúr cez AI"""  
    # Voláme priamo existujúci task pre každé ID.
    # Nepotrebujeme tu zámky, pretože `spracuj_fakturu_ai_task` už má svoj vlastný Mutex zámok!

    for f_id in ids:
        try:
            spracuj_fakturu_ai_task.delay(f_id)
        except Exception as e:
            logger.error(f"ISAS AI Error pri delegovaní faktúry ID {f_id}: {e}")

@shared_task(base=ISASAutoHealTask)
def task_vygeneruj_knihu_jazd_excel(vozidla_ids, user_id, zaloha_id=None, datum_od_str=None, datum_do_str=None):
    """Celery task pre vygenerovanie Excelu a jeho uloženie do MegaZálohy (S podporou dynamického obdobia)"""    
    lock_id = f"lock_excel_kniha_{hashlib.md5(str(sorted(vozidla_ids)).encode()).hexdigest()}"
    
    with isas_task_execution_guard(lock_id, timeout=300, fallback_model=MegaZaloha, fallback_id=zaloha_id) as (acquired, gn, identita):
        if not acquired:
            return

        User = get_user_model()
        user = User.objects.filter(id=user_id).first()
        vozidla = Vozidlo.objects.filter(id__in=vozidla_ids)
        
        if not vozidla.exists() or not user:
            return

        datum_od = None
        datum_do = None
        nadpis_obdobie = "Za posledných 12 mesiacov"
        
        try:
            if datum_od_str:
                datum_od = datum_od_str if isinstance(datum_od_str, date) else datetime.strptime(str(datum_od_str), '%Y-%m-%d').date()
            if datum_do_str:
                datum_do = datum_do_str if isinstance(datum_do_str, date) else datetime.strptime(str(datum_do_str), '%Y-%m-%d').date()
                
            if datum_od and datum_do:
                nadpis_obdobie = f"Za obdobie: {datum_od.strftime('%d.%m.%Y')} - {datum_do.strftime('%d.%m.%Y')}"
        except (ValueError, TypeError):
            logger.warning("Neplatný formát dátumu pri generovaní knihy jázd. Použije sa fallback.")
            
        builder = ISASExcelBuilder()
        wb = builder.wb
        ws = wb.active
        
        for index, vozidlo in enumerate(vozidla):
            surovy_nazov = str(vozidlo.spz).strip() if vozidlo.spz else "Vozidlo"
            cisty_nazov = re.sub(r'[\\/*?:\[\]]', '_', surovy_nazov)
            
            id_suffix = f"_{vozidlo.id}"
            max_dlzka_nazvu = 31 - len(id_suffix)
            bezpecny_nazov_harku = f"{cisty_nazov[:max_dlzka_nazvu]}{id_suffix}"
            
            if index == 0: 
                ws.title = bezpecny_nazov_harku
            else: 
                ws = wb.create_sheet(title=bezpecny_nazov_harku)

            ws.merge_cells('A1:K1')
            ws['A1'] = f"KNIHA JÁZD - VOZIDLO: {vozidlo.znacka_a_model} ({vozidlo.spz}) | {nadpis_obdobie}"
            ws['A1'].font = Font(size=16, bold=True, color="10b981")
            ws['A1'].alignment = builder.center_align
            ws.row_dimensions[1].height = 30

            headers = [
                "Dátum", "Čas odchodu", "Čas príchodu", "Vodič", 
                "Odkiaľ", "Kam (Cieľ)", "Účel cesty / Zákazník", 
                "Typ jazdy", "KM Štart", "KM Cieľ", "Najazdené KM"
            ]
            for col_num, header in enumerate(headers, 1):
                ws.cell(row=3, column=col_num, value=header)
                if col_num in [4, 5, 6, 7]: ws.column_dimensions[get_column_letter(col_num)].width = 25 
                else: ws.column_dimensions[get_column_letter(col_num)].width = 15
                
            builder.apply_header_style(ws, 3)

            jazdy_qs = vozidlo.jazdy.all()
            
            if datum_od:
                jazdy_qs = jazdy_qs.filter(datum_cas_start__date__gte=datum_od)
            if datum_do:
                jazdy_qs = jazdy_qs.filter(datum_cas_start__date__lte=datum_do)
            if not datum_od and not datum_do:
                jeden_rok_dozadu = timezone.now() - timedelta(days=365)
                jazdy_qs = jazdy_qs.filter(datum_cas_start__gte=jeden_rok_dozadu)

            jazdy = jazdy_qs.select_related('zamestnanec', 'sukromna_osoba').order_by('datum_cas_start').iterator(chunk_size=1000)
            
            sum_firemne = 0
            sum_sukromne = 0
            row_num = 4
            
            for jazda in jazdy:
                if jazda.zamestnanec:
                    vodic = str(jazda.zamestnanec)
                elif jazda.sukromna_osoba:
                    vodic = str(jazda.sukromna_osoba)
                else:
                    vodic = "Neznámy vodič"
                    
                najazd = jazda.prejdene_km or 0
                
                if jazda.typ_jazdy == 'FIREMNA': sum_firemne += najazd
                else: sum_sukromne += najazd
                
                if jazda.datum_cas_start:
                    local_start = timezone.localtime(jazda.datum_cas_start) if timezone.is_aware(jazda.datum_cas_start) else jazda.datum_cas_start
                    datum_str = local_start.strftime("%d.%m.%Y")
                    cas_start_str = local_start.strftime("%H:%M")
                else:
                    datum_str = "Neznáme"
                    cas_start_str = "—"

                if jazda.datum_cas_koniec:
                    local_koniec = timezone.localtime(jazda.datum_cas_koniec) if timezone.is_aware(jazda.datum_cas_koniec) else jazda.datum_cas_koniec
                    cas_koniec_str = local_koniec.strftime("%H:%M")
                else:
                    cas_koniec_str = "Prebieha"

                data_row = [
                    datum_str, cas_start_str, cas_koniec_str, vodic,
                    jazda.miesto_startu, jazda.miesto_ciela, jazda.ucel_cesty,
                    jazda.get_typ_jazdy_display(), jazda.stav_km_start,
                    jazda.stav_km_koniec if jazda.stav_km_koniec else "—", najazd
                ]
                
                for col_num, value in enumerate(data_row, 1):
                    c = ws.cell(row=row_num, column=col_num, value=value)
                    c.border = builder.thin_border
                    if col_num in [1, 2, 3, 8, 9, 10, 11]: c.alignment = builder.center_align
                
                row_num += 1

            sumar_row = row_num + 2
            ws.merge_cells(f'G{sumar_row}:H{sumar_row}')
            c_label1 = ws.cell(row=sumar_row, column=7, value="CELKOM FIREMNÉ JAZDY:")
            c_label1.font = Font(bold=True); c_label1.alignment = Alignment(horizontal="right")
            c_val1 = ws.cell(row=sumar_row, column=9, value=f"{sum_firemne} km")
            c_val1.font = Font(bold=True, color="059669"); c_val1.fill = builder.summary_fill; c_val1.alignment = builder.center_align; c_val1.border = builder.thin_border
            
            sumar_row += 1
            ws.merge_cells(f'G{sumar_row}:H{sumar_row}')
            c_label2 = ws.cell(row=sumar_row, column=7, value="CELKOM SÚKROMNÉ JAZDY:")
            c_label2.font = Font(bold=True); c_label2.alignment = Alignment(horizontal="right")
            c_val2 = ws.cell(row=sumar_row, column=9, value=f"{sum_sukromne} km")
            c_val2.font = Font(bold=True, color="7e22ce"); c_val2.fill = builder.summary_fill; c_val2.alignment = builder.center_align; c_val2.border = builder.thin_border
            
            sumar_row += 1
            ws.merge_cells(f'G{sumar_row}:H{sumar_row}')
            c_label3 = ws.cell(row=sumar_row, column=7, value="NÁJAZD SPOLU:")
            c_label3.font = Font(bold=True); c_label3.alignment = Alignment(horizontal="right")
            c_val3 = ws.cell(row=sumar_row, column=9, value=f"{sum_firemne + sum_sukromne} km")
            c_val3.font = Font(bold=True); c_val3.fill = builder.summary_fill; c_val3.alignment = builder.center_align; c_val3.border = builder.thin_border

        return builder.save_to_megazaloha(
            zaloha_id=zaloha_id, 
            filename_prefix="Kniha_jazd_ISAS_CORE", 
            user=user, 
            default_name=f"Kniha jázd ({vozidla.count()} vozidiel)", 
            default_note="Vygenerované z modulu Vozidlá",
            typ_zalohy='kniha_jazd'
        )

@shared_task(base=ISASAutoHealTask)
def task_odoslat_knihu_jazd_uctovnicke(zaloha_id):
    """Nezávislý Celery task pre odoslanie už vygenerovanej knihy jázd účtovníčke"""
    try:
        FiremnaIdentita = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
        GlobalneNastavenia = apps.get_model('ISASaplikacia', 'GlobalneNastavenia')
        
        zaloha = MegaZaloha.objects.get(id=zaloha_id)
        if not zaloha.subor_zalohy:
            return "Chyba: Súbor neexistuje."

        identita = FiremnaIdentita.objects.first()
        gn = GlobalneNastavenia.objects.first()
        
        if identita and identita.email_uctovnicky and gn and gn.smtp_user:
            vygeneroval = get_safe_employee_name(zaloha.vyziadal_uzivatel, "Systém")
            
            payload = (ISASNotificationBuilder(nadpis='NOVÁ KNIHA JÁZD DO EVIDENCIE', ikona='🚗', farba_temy='#3b82f6')
                       .pridaj_riadok('Názov súboru:', zaloha.subor_zalohy.name.split('/')[-1])
                       .pridaj_riadok('Vygeneroval:', vygeneroval)
                       .nastav_hlavny_text("Dobrý deň,<br><br>systém práve vygeneroval novú knihu jázd. V prílohe nájdete Excel súbor pre Vašu účtovnú evidenciu.")
                       .build())

            zaloha.subor_zalohy.seek(0)
            attachments = [{
                'filename': zaloha.subor_zalohy.name.split('/')[-1],
                'content': zaloha.subor_zalohy.read(),
                'mimetype': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            }]

            try:
                ISASNotifier.send(
                    payload=payload,
                    to_email=identita.email_uctovnicky,
                    send_tg=False,
                    send_mail=True,
                    send_discord=False,
                    attachments=attachments
                )
                return "Úspešne odoslané"
            except Exception as e:
                logger.error(f"❌ Chyba pri odosielaní knihy jázd účtovníčke: {e}")
                return f"Chyba: {str(e)}"

        else:
            return "Chyba: Chýbajú nastavenia SMTP alebo email účtovníčky."
    except Exception as e:
        return f"Kritická chyba: {str(e)}"

@shared_task(base=ISASAutoHealTask)
def task_vygeneruj_ocenenie_skladu_excel(user_id, zaloha_id):
    """Celery task pre vygenerovanie finančného ocenenia skladov (Hlavný + Filiálky)"""
    lock_id = f"lock_excel_sklad_{zaloha_id}"
    
    with isas_task_execution_guard(lock_id, timeout=300, fallback_model=MegaZaloha, fallback_id=zaloha_id) as (acquired, gn, identita):
        if not acquired:
            return

        User = get_user_model()
        user = User.objects.filter(id=user_id).first()
        
        if not user: return
            
        builder = ISASExcelBuilder()
        wb = builder.wb

        # O(1) In-Memory Cache pre Ceny a Kategórie
        cennik_cache = {item.search_name: item.nakupna_cena for item in CenovyKatalog.objects.only('search_name', 'nakupna_cena')}
        product_info_cache = {}
        
        hlavny_sklad = Sklad.objects.first()
        nazov_hlavneho = f"Hlavný sklad ({hlavny_sklad.nazov})" if hlavny_sklad and hlavny_sklad.nazov and hlavny_sklad.nazov.lower() != "hlavný sklad" else "Hlavný sklad"
        cisty_nazov_hlavny = re.sub(r'[\\/*?:\[\]]', '_', nazov_hlavneho)
        ws_hlavny = wb.active
        ws_hlavny.title = f"🏭 {cisty_nazov_hlavny}"[:31]

        firemna_mena = get_firemna_mena()
        headers = ["Sériové číslo / Názov", "Kategória", "Podkategória", "Množstvo", "MJ", f"Jedn. Nákupná Cena ({firemna_mena})", f"Celková Hodnota ({firemna_mena})"]
        
        for col_num, header in enumerate(headers, 1):
            ws_hlavny.cell(row=1, column=col_num, value=header)
            ws_hlavny.column_dimensions[get_column_letter(col_num)].width = 25 if col_num in [1, 2, 3] else 15
            
        builder.apply_header_style(ws_hlavny, 1)

        row_num_hlavny = 2
        celkova_hodnota_hlavny = Decimal('0.00')
        pocet_poloziek_hlavny = 0

        for main_cat, subcats in CATEGORY_MAP.items():
            for subcat, model_cls in subcats.items():
                dostupne_polia = {f.name for f in model_cls._meta.fields}
                if 'pocet_na_sklade' not in dostupne_polia:
                    continue
                name_field = next((f for f in ['seriove_cislo', 'nazov', 'nazov_polozky', 'model_zariadenia'] if f in dostupne_polia), None)
                stlpce_only = ['pocet_na_sklade']
                if name_field:
                    stlpce_only.append(name_field)
                if 'merna_jednotka' in dostupne_polia:
                    stlpce_only.append('merna_jednotka')
                polozky = model_cls.objects.filter(pocet_na_sklade__gt=0).only(*stlpce_only)
                
                for obj in polozky:
                    nazov = getattr(obj, name_field, None) if name_field else str(obj)
                    mj = getattr(obj, 'merna_jednotka', 'ks')
                    
                    product_info_cache[nazov] = {'cat': main_cat, 'subcat': subcat, 'mj': mj}
                    
                    cena_jedn = cennik_cache.get(nazov) or Decimal('0.00')
                    hodnota_spolu = (cena_jedn * Decimal(str(obj.pocet_na_sklade))).quantize(Decimal('0.01'))
                    
                    celkova_hodnota_hlavny += hodnota_spolu
                    pocet_poloziek_hlavny += 1
                    
                    data_row = [nazov, main_cat, subcat, obj.pocet_na_sklade, mj, float(cena_jedn), float(hodnota_spolu)]
                    for col_num, value in enumerate(data_row, 1):
                        c = ws_hlavny.cell(row=row_num_hlavny, column=col_num, value=value)
                        c.border = builder.thin_border
                        if col_num in [4, 5, 6, 7]: c.alignment = builder.center_align
                    row_num_hlavny += 1

        # --- SPRACOVANIE FILIÁLOK ---
        filialky_data = []
        filialky = Filialka.objects.filter(je_aktivna=True)
        
        for filialka in filialky:
            cisty_nazov_fil = re.sub(r'[\\/*?:\[\]]', '_', str(filialka.nazov or 'Pobocka'))
            ws_filialka = wb.create_sheet(title=f"🏪 {cisty_nazov_fil}"[:31])
            
            for col_num, header in enumerate(headers, 1):
                ws_filialka.cell(row=1, column=col_num, value=header)
                ws_filialka.column_dimensions[get_column_letter(col_num)].width = 25 if col_num in [1, 2, 3] else 15
                
            builder.apply_header_style(ws_filialka, 1)

            stavy = LedgerKalkulacka.zisti_stav_vsetkych_poloziek(filialka)
            
            row_num_fil = 2
            celkova_hodnota_fil = Decimal('0.00')
            pocet_poloziek_fil = 0
            
            for nazov, pocet in stavy.items():
                info = product_info_cache.get(nazov, {'cat': 'Neznáme', 'subcat': 'Neznáme', 'mj': 'ks'})
                cena_jedn = cennik_cache.get(nazov) or Decimal('0.00')
                hodnota_spolu = (cena_jedn * Decimal(str(pocet or '0'))).quantize(Decimal('0.01'))
                
                celkova_hodnota_fil += hodnota_spolu
                pocet_poloziek_fil += 1
                
                data_row = [nazov, info['cat'], info['subcat'], pocet, info['mj'], float(cena_jedn), float(hodnota_spolu)]
                for col_num, value in enumerate(data_row, 1):
                    c = ws_filialka.cell(row=row_num_fil, column=col_num, value=value)
                    c.border = builder.thin_border
                    if col_num in [4, 5, 6, 7]: c.alignment = builder.center_align
                row_num_fil += 1
                
            filialky_data.append({
                'nazov': filialka.nazov,
                'pocet': pocet_poloziek_fil,
                'hodnota': celkova_hodnota_fil
            })

        # --- VYTVORENIE SÚHRNNÉHO HÁRKU (DASHBOARD) ---
        ws_suhrn = wb.create_sheet(title="📊 SÚHRN PRE ÚČTOVNÍCTVO", index=0)
        ws_suhrn.column_dimensions['A'].width = 35
        ws_suhrn.column_dimensions['B'].width = 20
        ws_suhrn.column_dimensions['C'].width = 25
        
        ws_suhrn.merge_cells('A1:C1')
        ws_suhrn['A1'] = f"FINANČNÉ OCENENIE ZÁSOB K DÁTUMU: {timezone.now().strftime('%d.%m.%Y %H:%M')}"
        ws_suhrn['A1'].font = Font(size=14, bold=True, color="10b981")
        ws_suhrn['A1'].alignment = builder.center_align
        ws_suhrn.row_dimensions[1].height = 30
        
        headers_suhrn = ["Lokalita / Sklad", "Počet unikátnych položiek", f"Celková hodnota zásob ({firemna_mena})"]
        for col_num, header in enumerate(headers_suhrn, 1):
            ws_suhrn.cell(row=3, column=col_num, value=header)
            
        builder.apply_header_style(ws_suhrn, 3)

        ws_suhrn.cell(row=4, column=1, value=f"🏭 {nazov_hlavneho} (Centrála)").border = builder.thin_border
        c_pocet = ws_suhrn.cell(row=4, column=2, value=pocet_poloziek_hlavny); c_pocet.alignment = builder.center_align; c_pocet.border = builder.thin_border
        c_hodnota = ws_suhrn.cell(row=4, column=3, value=float(celkova_hodnota_hlavny)); c_hodnota.alignment = builder.center_align; c_hodnota.border = builder.thin_border; c_hodnota.font = Font(bold=True)
        
        row_suhrn = 5
        celkova_hodnota_firma = celkova_hodnota_hlavny
        
        for f_data in filialky_data:
            ws_suhrn.cell(row=row_suhrn, column=1, value=f"🏪 Filiálka: {f_data['nazov']}").border = builder.thin_border
            c_pocet = ws_suhrn.cell(row=row_suhrn, column=2, value=f_data['pocet']); c_pocet.alignment = builder.center_align; c_pocet.border = builder.thin_border
            c_hodnota = ws_suhrn.cell(row=row_suhrn, column=3, value=float(f_data['hodnota'])); c_hodnota.alignment = builder.center_align; c_hodnota.border = builder.thin_border; c_hodnota.font = Font(bold=True)
            celkova_hodnota_firma += f_data['hodnota']
            row_suhrn += 1
            
        row_suhrn += 1
        ws_suhrn.merge_cells(f'A{row_suhrn}:B{row_suhrn}')
        c_total_label = ws_suhrn.cell(row=row_suhrn, column=1, value="💰 CELKOVÁ HODNOTA ZÁSOB FIRMY:")
        c_total_label.font = Font(bold=True, size=12); c_total_label.alignment = Alignment(horizontal="right"); c_total_label.border = builder.thin_border
        ws_suhrn.cell(row=row_suhrn, column=2).border = builder.thin_border
        
        c_total_val = ws_suhrn.cell(row=row_suhrn, column=3, value=float(celkova_hodnota_firma))
        c_total_val.font = Font(bold=True, size=14, color="059669"); c_total_val.fill = builder.summary_fill; c_total_val.alignment = builder.center_align; c_total_val.border = builder.thin_border

        return builder.save_to_megazaloha(
            zaloha_id=zaloha_id, 
            filename_prefix="Ocenenie_Skladov_ISAS", 
            user=user, 
            default_name="Ocenenie Skladov", 
            default_note="Vygenerované z modulu Sklad"
        )

@shared_task(base=ISASAutoHealTask)
def task_odoslat_ocenenie_skladu_uctovnicke(zaloha_id):
    """Nezávislý Celery task pre odoslanie ocenenia skladu účtovníčke"""
    try:
        FiremnaIdentita = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
        GlobalneNastavenia = apps.get_model('ISASaplikacia', 'GlobalneNastavenia')
        
        zaloha = MegaZaloha.objects.get(id=zaloha_id)
        if not zaloha.subor_zalohy: return "Chyba: Súbor neexistuje."

        identita = FiremnaIdentita.objects.first()
        gn = GlobalneNastavenia.objects.first()
        
        if identita and identita.email_uctovnicky and gn and gn.smtp_user:
            vygeneroval = get_safe_employee_name(zaloha.vyziadal_uzivatel, "Systém")
            
            payload = (ISASNotificationBuilder(nadpis='FINANČNÉ OCENENIE ZÁSOB', ikona='📊', farba_temy='#f59e0b')
                       .pridaj_riadok('Názov súboru:', zaloha.subor_zalohy.name.split('/')[-1])
                       .pridaj_riadok('Vygeneroval:', vygeneroval)
                       .nastav_hlavny_text("Dobrý deň,<br><br>systém práve vygeneroval aktuálny report o stave a hodnote zásob. V prílohe nájdete Excel súbor pre Vašu účtovnú evidenciu.")
                       .build())

            zaloha.subor_zalohy.seek(0)
            attachments = [{
                'filename': zaloha.subor_zalohy.name.split('/')[-1],
                'content': zaloha.subor_zalohy.read(),
                'mimetype': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            }]

            try:
                ISASNotifier.send(
                    payload=payload,
                    to_email=identita.email_uctovnicky,
                    send_tg=False,
                    send_mail=True,
                    send_discord=False,
                    attachments=attachments
                )
                return "Úspešne odoslané"
            except Exception as e:
                logger.error(f"❌ Chyba pri odosielaní ocenenia skladu: {e}")
                return f"Chyba: {str(e)}"

        else: return "Chyba: Chýbajú nastavenia SMTP alebo email účtovníčky."
    except Exception as e: return f"Kritická chyba: {str(e)}"

@shared_task(base=ISASAutoHealTask)
def task_skontroluj_sklad_jit(uloha_id, poslat_tg=True, poslat_mail=True, poslat_discord=True, **kwargs):
    """Nezávislý Celery task výhradne pre JIT kontrolu skladu"""
    try:
        # Prefetch pre GenericForeignKey eliminuje N+1 dopyty v cykle
        uloha = Uloha.objects.prefetch_related('pouzite_polozky__content_object', 'pouzite_polozky__content_type').get(id=uloha_id)
        polozky = uloha.pouzite_polozky.all()
        kontrolovat = getattr(uloha, 'kontrolovat_sklad_pri_ulozeni', True)

        if not kontrolovat or not polozky.exists():
            return

        gn = GlobalneNastavenia.objects.first()
        nedostatok_dielov = 0
        chybajuce_polozky_zoznam = []
        choices_dict = dict(CATEGORY_CHOICES)

        check_main = True
        filialky_to_check = []

        if gn:
            if getattr(gn, 'jit_kontrolovat_vsetky_sklady', True):
                check_main = True
                filialky_to_check = list(Filialka.objects.filter(je_aktivna=True))
            else:
                check_main = getattr(gn, 'jit_kontrolovat_hlavny_sklad', False)
                if hasattr(gn, 'jit_kontrolovane_filialky'):
                    filialky_to_check = list(gn.jit_kontrolovane_filialky.all())
        else:
            filialky_to_check = list(Filialka.objects.filter(je_aktivna=True))

        sklady_nazvy = []
        if check_main:
            hlavny_sklad = Sklad.objects.first()
            nazov_hlavneho = f"Hlavný sklad ({hlavny_sklad.nazov})" if hlavny_sklad and hlavny_sklad.nazov and hlavny_sklad.nazov.lower() != "hlavný sklad" else "Hlavný sklad"
            sklady_nazvy.append(nazov_hlavneho)
        sklady_nazvy.extend([f.nazov for f in filialky_to_check])
        sklady_text = ", ".join(sklady_nazvy) if sklady_nazvy else "Žiadny sklad"

        # Prednačítanie stavov všetkých filiálok do pamäte (Eliminácia N*M dopytov)
        prednacitane_stavy_filialok = {}
        for f in filialky_to_check:
            surove_stavy = LedgerKalkulacka.zisti_stav_vsetkych_poloziek(f)
            prednacitane_stavy_filialok[f.id] = {ocisti_nazov_od_jednotky(k): v for k, v in surove_stavy.items()}

        for p in polozky:
            diel = p.content_object
            if diel:
                cisty_nazov = ocisti_nazov_od_jednotky(diel)
                dostupne_spolu = Decimal('0.00')
                if check_main:
                    dostupne_spolu += Decimal(str(getattr(diel, 'pocet_na_sklade', 0) or '0'))

                for f in filialky_to_check:
                    # ⚡ Bleskové čítanie z pamäte (RAM) namiesto SQL dopytu
                    dostupne_spolu += Decimal(str(prednacitane_stavy_filialok[f.id].get(cisty_nazov, 0) or '0'))

                potrebne_ks = Decimal(str(p.pocet or '0'))

                if potrebne_ks > dostupne_spolu:
                    chyba_kusov = potrebne_ks - dostupne_spolu
                    nedostatok_dielov += 1
                    merna_jednotka = getattr(diel, 'merna_jednotka', 'ks')

                    model_cls = p.content_type.model_class() if p.content_type else None
                    hlavna_kat = "📦 Ostatné"
                    podkategoria_nazov = p.content_type.model.title() if p.content_type else "Neznáme"

                    for main_cat, subcats in CATEGORY_MAP.items():
                        for subcat_name, subcat_model in subcats.items():
                            if model_cls == subcat_model:
                                hlavna_kat = choices_dict.get(main_cat, main_cat)
                                podkategoria_nazov = subcat_name
                                break
                        if hlavna_kat != "📦 Ostatné":
                            break

                    chybajuce_polozky_zoznam.append(f"🔸 {hlavna_kat} / {podkategoria_nazov}\n   ↳ {cisty_nazov} <b>(+{chyba_kusov} {merna_jednotka})</b>")
                    
                    with transaction.atomic():
                        chyba_kusov_dec = Decimal(str(chyba_kusov))
                        dostupne_spolu_dec = Decimal(str(dostupne_spolu))
                        existujuca_polozka = KosikSPolozkamiNaDoobjednanie.objects.select_for_update().filter(
                            nazov_polozky=cisty_nazov,
                            objednane=False
                        ).first()

                        if existujuca_polozka:
                            # Typová bezpečnosť voči PostgreSQL 18 (numeric + numeric)
                            existujuca_polozka.mnozstvo_na_objednanie = F('mnozstvo_na_objednanie') + chyba_kusov_dec
                            existujuca_polozka.aktualny_stav = dostupne_spolu_dec
                            existujuca_polozka.save(update_fields=['mnozstvo_na_objednanie', 'aktualny_stav'])
                        else:
                            # Ak nie je, vytvoríme novú
                            KosikSPolozkamiNaDoobjednanie.objects.create(
                                nazov_polozky=cisty_nazov,
                                aktualny_stav=dostupne_spolu_dec,
                                dodavatel=getattr(diel, 'dodavatel', None),
                                mnozstvo_na_objednanie=chyba_kusov_dec,
                                merna_jednotka=merna_jednotka,
                                datum_zistenia=timezone.now(),
                                objednane=False
                            )

        if nedostatok_dielov > 0:
            base_url = (getattr(gn, 'clean_base_url', None) or getattr(settings, 'SITE_URL', '')).strip().rstrip('/')

            zoznam_text = "<br>".join(chybajuce_polozky_zoznam)
            slovo_polozka = "položku" if nedostatok_dielov == 1 else ("položky" if 2 <= nedostatok_dielov <= 4 else "položiek")

            payload = (ISASNotificationBuilder(nadpis=f'CHÝBA MATERIÁL (Úloha #{uloha.id})', ikona='🚨', farba_temy='#ef4444')
                       .pridaj_riadok('📋 Zdroj:', f"Úloha #{uloha.id} ({uloha.zakaznik})")
                       .pridaj_riadok('🏢 Kontrolované sklady:', sklady_text)
                       .pridaj_riadok('⚠️ Problém:', f"Nedostatok pre {nedostatok_dielov} {slovo_polozka}")
                       .nastav_hlavny_text(f"🛒 <b>Vložené do košíka:</b><br>{zoznam_text}")
                       .nastav_tlacidlo('Otvoriť Nákupný košík', f"{base_url}/admin/Evidencia/kosikspolozkaminadoobjednanie/")
                       .nastav_ai_poznamku('Odporúča sa doobjednať u dodávateľa, inak je výjazd ohrozený.')
                       .build())

            ISASNotifier.send(
                payload=payload,
                send_tg=poslat_tg,
                send_mail=poslat_mail,
                send_discord=poslat_discord
            )
            
    except Exception as e:
        logger.error(f"JIT Logistika zlyhala pre ulohu {uloha_id}: {e}")

@shared_task(base=ISASAutoHealTask)
def task_notifikuj_technikov_o_ulohe(uloha_id, change, stary_material, poslat_tg, poslat_mail, poslat_discord=True):
    """Nezávislý Celery task výhradne pre generovanie a odoslanie správ technikom"""
    try:
        uloha = Uloha.objects.select_related('zakaznik').prefetch_related(
            'typ_servisu', 'zariadenia', 'technici', 
            'pouzite_polozky__content_object', 'pouzite_polozky__content_type'
        ).get(id=uloha_id)
        
        gn = GlobalneNastavenia.objects.first()
        identita = FiremnaIdentita.objects.first()

        if not gn: return

        # --- 1. GENERÁCIA TEXTU ---
        typy_ciste = [str(tp).replace('[Fix]', '').replace('(Fix)', '').strip() for tp in uloha.typ_servisu.all()]
        typy = ", ".join([escape(tp) for tp in typy_ciste])
        stroje = ", ".join([escape(str(z)) for z in uloha.zariadenia.all()])
        polozky = uloha.pouzite_polozky.all()

        vsetky_postupy = []
        for typ_prace in uloha.typ_servisu.all():
            if typ_prace.pracovny_postup:
                cisty_nazov = str(typ_prace).replace('[Fix]', '').replace('(Fix)', '').strip()
                vsetky_postupy.append(f"<b>SOP pre '{escape(cisty_nazov)}':</b>\n<i>{escape(typ_prace.pracovny_postup)}</i>")

        postup_sekcia = ""
        if vsetky_postupy:
            obsah_postupov = "\n\n".join(vsetky_postupy)
            postup_sekcia = f"\n📋 <b>ŠTANDARDNÉ PRACOVNÉ POSTUPY (SOP):</b>\n<hr>\n{obsah_postupov}\n"

        material_sekcia = format_material_list_for_notification(polozky, stary_material=stary_material, is_change=change)

        uvodny_pozdrav = "👋 Dobrý deň <b>[MENO]</b>, bola Vám pridelená nová servisná úloha. 📋" if not change else "👋 Dobrý deň <b>[MENO]</b>, vo Vašej pridelenej úlohe nastali zmeny. 🔄"
        predmet = "🔵 ISAS CORE: NOVÁ ÚLOHA" if not change else "🟠 ISAS CORE: AKTUALIZÁCIA ÚLOHY"
        bezpecne_info = escape(uloha.blizsie_informacie) if uloha.blizsie_informacie else 'ℹ️ Žiadne doplňujúce informácie neboli poskytnuté.'

        zakaznik_nazov = str(uloha.zakaznik) if uloha.zakaznik else "Neuvedený"
        bezpecny_zakaznik = escape(zakaznik_nazov)

        if uloha.zaciatok:
            lokalny_zaciatok = timezone.localtime(uloha.zaciatok) if timezone.is_aware(uloha.zaciatok) else uloha.zaciatok
            cas_text = f"{lokalny_zaciatok.strftime('%H:%M')} ({lokalny_zaciatok.strftime('%d.%m.%Y')})"
        else:
            cas_text = "⏳ Čas nebol uvedený."

        # ==================
        # 🤖 ISAS AI ŠEPKÁR
        # ==================
        signer = TimestampSigner(salt='isas-ai-sepkar')
        base_url = (getattr(gn, 'clean_base_url', None) or getattr(settings, 'SITE_URL', '')).strip().rstrip('/')
        
        # Skontrolujeme, či úloha má aspoň jedno zariadenie
        zariadenia_v_ulohe = uloha.zariadenia.all()

        # --- 2. ODOSLANIE ---
        nazov_firmy = identita.nazov_spolocnosti if identita and identita.nazov_spolocnosti else "Naša spoločnosť"
        aktualny_rok = timezone.now().year

        logo_html = ""
        if identita and identita.logo and base_url:
            logo_url = f"{base_url}{identita.logo.url}"
            logo_html = f'<img src="{logo_url}" alt="{nazov_firmy}" style="max-height: 55px; margin-bottom: 15px;">'

        upozornenie_cervene = None
        if uloha.zakaznik and getattr(uloha.zakaznik, 'vyzaduje_povolenie_vstupu', False):
            upozornenie_cervene = f"<b>🚧 POZOR: PRÍSNY REŽIM VSTUPU</b><br>Zákazník <b>{bezpecny_zakaznik}</b> vyžaduje povolenku na vrátnici! Pred odjazdom si skontroluj inštrukcie v systéme alebo v Telegrame."

        for technik in uloha.technici.all():
            ai_links_html = ""
            
            if bool(zariadenia_v_ulohe):
                for zar in zariadenia_v_ulohe:
                    payload_token = {"z": zar.id, "t": technik.id}
                    token = signer.sign_object(payload_token)
                    sepkar_url = f"{base_url}/evidencia/zariadenie/sepkar/{token}/"
                    
                    casti_nazvu = []
                    if zar.vyrobne_cislo: casti_nazvu.append(f"SN: {zar.vyrobne_cislo}")
                    if getattr(zar, 'typ_zariadenia', None): casti_nazvu.append(f"TYP: {zar.typ_zariadenia}")
                    nazov_stroja = " | ".join(casti_nazvu) if casti_nazvu else "Neznáme zariadenie"
                    if len(nazov_stroja) > 35: nazov_stroja = nazov_stroja[:32] + "..."
                    
                    # ISASNotifier._clean_html_for_telegram podporuje <a> tagy, takže sa to správne zobrazí aj v TG
                    ai_links_html += f"• <a href='{sepkar_url}'>🤖 AI Diagnostika: {nazov_stroja}</a><br>"

            hlavny_text = f"Ahoj <b>{escape(technik.meno)}</b>,<br><br>{'bola ti pridelená nová pracovná úloha' if not change else 'vo tvojej pridelenej úlohe nastali zmeny'}.<br><br>"
            hlavny_text += f"<b>📖 Bližšie informácie k úlohe:</b><br><i>{bezpecne_info}</i><br>"
            
            if postup_sekcia:
                hlavny_text += postup_sekcia
            if material_sekcia:
                hlavny_text += material_sekcia

            if ai_links_html:
                hlavny_text += f"<br><br><b>🧠 ISAS AI PREDIKTÍVNA ANALÝZA:</b><br>{ai_links_html}"

            builder = (ISASNotificationBuilder(
                           nadpis='NOVÁ ÚLOHA' if not change else 'AKTUALIZÁCIA ÚLOHY', 
                           ikona='🔵' if not change else '🟠', 
                           farba_temy='#3b82f6' if not change else '#f59e0b')
                       .pridaj_riadok('🏢 Zákazník:', bezpecny_zakaznik)
                       .pridaj_riadok('⏰ Čas začiatku:', cas_text)
                       .pridaj_riadok('🛠 Typ práce:', typy if typy else 'Neuvedené')
                       .pridaj_riadok('⚙️ Zariadenia:', stroje if stroje else 'Neuvedené')
                       .nastav_hlavny_text(hlavny_text)
                       .nastav_ai_poznamku('Prajeme Vám bezpečný a úspešný výjazd.'))
            if base_url:
                builder.nastav_tlacidlo('Otvoriť systém ISAS CORE', base_url)
            if upozornenie_cervene:
                builder.nastav_upozornenie(upozornenie_cervene, 'cervene')
            payload = builder.build()

            try:
                ISASNotifier.send(
                    payload=payload,
                    to_email=technik.email if hasattr(technik, 'email') else None,
                    tg_chat_id=technik.telegram_id if hasattr(technik, 'telegram_id') else None,
                    discord_id=getattr(technik, 'discord_id', None),
                    send_tg=poslat_tg and bool(getattr(technik, 'telegram_id', None)),
                    send_mail=poslat_mail and bool(getattr(technik, 'email', None)),
                    send_discord=poslat_discord and bool(getattr(technik, 'discord_id', None))
                )
            except Exception as e:
                logger.error(f"Chyba pri notifikácii technika {technik.meno}: {e}")

    except Exception as e:
        logger.error(f"Kriticka chyba v task_notifikuj_technikov_o_ulohe: {e}")

@shared_task(base=ISASAutoHealTask)
def send_isas_stock_notifications_task(movement_id, detail_url_path, povodny_stav=None, novy_stav=None):
    """Univerzálny task pre odoslanie TG aj E-mail notifikácií o pohybe na sklade."""
    try:
        # OPTIMALIZÁCIA: Prednačítanie usera, jeho zamestnaneckého profilu a filiálky
        stock_movement_obj = StockMovement.objects.select_related('user', 'user__zamestnanec', 'filialka').get(id=movement_id)
    except StockMovement.DoesNotExist:
        return

    gn = GlobalneNastavenia.objects.first()
    if not gn: return

    user_name = get_safe_employee_name(stock_movement_obj.user, "🤖 AUTO-SYSTÉM")
    action_str = str(stock_movement_obj.action).upper()
    
    if action_str == "ADD" or "PRID" in action_str:
        action_ikona, action_text, znamenko = "📥", "PRIDANIE NA SKLAD", "+"
    else:
        action_ikona, action_text, znamenko = "📤", "VÝDAJ ZO SKLADU", "-"

    jednotka = getattr(stock_movement_obj, 'merna_jednotka', 'ks')
    if jednotka == 'ks':
        match = re.search(r'\([\d\.\,\s]+([a-zA-Z2]+)\)$', str(stock_movement_obj.product_name))
        if match: jednotka = match.group(1).lower()

    mnozstvo_str = f"{znamenko}{stock_movement_obj.count} {jednotka}"
    base_url = (getattr(gn, 'clean_base_url', None) or getattr(settings, 'SITE_URL', '')).strip().rstrip('/')
    full_url = f"{base_url}{detail_url_path}"

    choices_dict = dict(CATEGORY_CHOICES)
    kategoria_s_emoji = choices_dict.get(stock_movement_obj.category, stock_movement_obj.category)

    safe_user = safe_tg(user_name)
    safe_cat = safe_tg(kategoria_s_emoji)
    safe_subcat = safe_tg(stock_movement_obj.subcategory)
    hlavny_sklad = Sklad.objects.first()
    nazov_hlavneho = f"Hlavný sklad ({hlavny_sklad.nazov})" if hlavny_sklad and hlavny_sklad.nazov and hlavny_sklad.nazov.lower() != "hlavný sklad" else "Hlavný sklad"
    sklad_nazov = stock_movement_obj.filialka.nazov if stock_movement_obj.filialka else nazov_hlavneho
    safe_sklad = safe_tg(sklad_nazov)
    
    if novy_stav is not None and povodny_stav is not None:
        aktualny_stav, predchadzajuci_stav = novy_stav, povodny_stav
    else:
        aktualny_stav = ziskaj_aktualny_stav_skladu(stock_movement_obj)
        try:
            akt_stav_int = int(aktualny_stav)
            predchadzajuci_stav = akt_stav_int - stock_movement_obj.count if (action_str == "ADD" or "PRID" in action_str) else akt_stav_int + stock_movement_obj.count
        except (ValueError, TypeError):
            predchadzajuci_stav = "?"
    
    cisty_nazov_do_spravy = ocisti_nazov_od_jednotky(stock_movement_obj.product_name)
    safe_prod_base = safe_tg(cisty_nazov_do_spravy)
    safe_prod = f"{safe_prod_base} (Stav: {predchadzajuci_stav} {jednotka} ➔ {aktualny_stav} {jednotka})"

    payload = (ISASNotificationBuilder(nadpis='POHYB NA SKLADE', ikona=action_ikona, farba_temy='#10b981' if (action_str == "ADD" or "PRID" in action_str) else '#ef4444')
               .pridaj_riadok('👤 Vykonal(a):', safe_user)
               .pridaj_riadok('🏢 Sklad:', safe_sklad)
               .pridaj_riadok('🗂️ Kategória:', f"{safe_cat} / {safe_subcat}")
               .pridaj_riadok('⚙️ Položka:', safe_prod)
               .pridaj_riadok(f'{action_ikona} Akcia:', f"{action_text} ({mnozstvo_str})")
               .nastav_tlacidlo('Otvoriť detail v systéme ISAS CORE', full_url)
               .nastav_ai_poznamku('Skladová evidencia bola úspešne synchronizovaná.')
               .build())

    try:
        ISASNotifier.send(
            payload=payload,
            send_tg=True,
            send_mail=True,
            send_discord=True
        )
    except Exception as e:
        logger.warning(f"Notifikácia skladu zlyhala: {e}")

@shared_task(base=ISASAutoHealTask)
def send_isas_bulk_stock_notifications_task(movement_ids):
    """
    Odošle JEDNU zbernú notifikáciu pri hromadnej úprave skladu v databáze.
    """
    # Kontrola globálneho prepínača notifikácií
    if not cache.get('isas_stock_notifications', True):
        return  # Ak sú notifikácie vypnuté (🔕), task sa ihneď ukončí!

    # Pridané user__zamestnanec pre get_safe_employee_name
    pohyby = list(StockMovement.objects.filter(id__in=movement_ids).select_related('user', 'user__zamestnanec', 'filialka'))
    if not pohyby:
        return

    gn = GlobalneNastavenia.objects.first()
    if not gn:
        return

    prvy_pohyb = pohyby[0]
    user_name = get_safe_employee_name(prvy_pohyb.user, "🤖 AUTO-SYSTÉM")
    safe_user = safe_tg(user_name)
    hlavny_sklad = Sklad.objects.first()
    nazov_hlavneho = f"Hlavný sklad ({hlavny_sklad.nazov})" if hlavny_sklad and hlavny_sklad.nazov and hlavny_sklad.nazov.lower() != "hlavný sklad" else "Hlavný sklad"
    sklad_nazov = prvy_pohyb.filialka.nazov if prvy_pohyb.filialka else nazov_hlavneho
    safe_sklad = safe_tg(sklad_nazov)
    base_url = (getattr(gn, 'clean_base_url', None) or getattr(settings, 'SITE_URL', '')).strip().rstrip('/')
    full_url = f"{base_url}/admin/Evidencia/stockmovement/"

    pocet = len(pohyby)
    if pocet == 1:
        pocet_str = "1 položka"
    elif 2 <= pocet <= 4:
        pocet_str = f"{pocet} položky"
    else:
        pocet_str = f"{pocet} položiek"

    # Zostavíme zoznam položiek
    polozky_tg = []

    for p in pohyby:
        action_str = str(p.action).upper()
        if action_str == "ADD" or "PRID" in action_str:
            action_ikona, znamenko = "📥", "+"
        else:
            action_ikona, znamenko = "📤", "-"

        jednotka = getattr(p, 'merna_jednotka', 'ks')
        cisty_nazov_raw = ocisti_nazov_od_jednotky(p.product_name)
        cisty_nazov_safe = safe_tg(cisty_nazov_raw)
        mnozstvo_str = f"{znamenko}{p.count} {jednotka}"

        polozky_tg.append(f"• {action_ikona} <b>{cisty_nazov_safe}</b> ({mnozstvo_str})")

    zoznam_tg_text = "\n".join(polozky_tg)

    payload = (ISASNotificationBuilder(nadpis='HROMADNÁ ÚPRAVA SKLADU', ikona='📋', farba_temy='#10b981')
               .pridaj_riadok('👤 Vykonal(a):', safe_user)
               .pridaj_riadok('🏢 Sklad:', safe_sklad)
               .pridaj_riadok('📊 Upravené položky:', pocet_str)
               .pridaj_riadok('📑 Typ pohybu:', 'INVENTÚRA (Priamy zápis v DB)')
               .nastav_hlavny_text(f"<b>Zoznam zmien:</b><br>{zoznam_tg_text}")
               .nastav_tlacidlo('Otvoriť zoznam pohybov v ISAS CORE', full_url)
               .nastav_ai_poznamku('Skladová evidencia bola úspešne synchronizovaná.')
               .build())

    try:
        ISASNotifier.send(
            payload=payload,
            send_tg=True,
            send_mail=True,
            send_discord=True
        )
    except Exception as e:
        logger.warning(f"Hromadná notifikácia skladu zlyhala: {e}")

@shared_task(base=ISASAutoHealTask)
def task_vyhodnot_smart_matcher(objednavka_id):
    """🤖 SMER A: Uložila sa Objednávka -> Hľadáme Ponuku"""    
    try:
        # Prednačítanie zákazníka pre filter kandidátov
        obj = PrijataObjednavka.objects.select_related('zakaznik').get(id=objednavka_id)
    except PrijataObjednavka.DoesNotExist:
        return "Objednávka neexistuje."
        
    # Hľadáme všetky vhodné ponuky, ktoré ešte nie sú spárované, navrhnuté alebo ignorované.
    kandidati = CenovaPonuka.objects.filter(
        zakaznik=obj.zakaznik, 
        stav__in=['SENT', 'ACCEPTED']
    ).exclude(
        id__in=obj.ignorovane_ponuky.values_list('id', flat=True)
    ).exclude(
        id__in=obj.zdrojove_ponuky.values_list('id', flat=True)
    ).exclude(
        id__in=obj.navrhnute_ponuky.values_list('id', flat=True)
    )
    
    text_na_prehladanie = f"{obj.cislo_objednavky_klienta or ''} {obj.text_emailu or ''} {obj.poznamka_klienta or ''}"
    najdene_zhody_ids = []
    
    for ponuka in kandidati:
        skore = 0
        
        if ponuka.cislo_ponuky and ponuka.cislo_ponuky in text_na_prehladanie:
            skore += 100
            
        # Tolerancia 1.00 pri sume (častá chyba pri zaokrúhľovaní DPH klientom v B2B)
        obj_suma = obj.celkova_suma_bez_dph or Decimal('0.00')
        if obj_suma > Decimal('0.00'):
            p_bez_dph = ponuka.celkom_bez_dph or Decimal('0.00')
            p_s_dph = ponuka.celkom_s_dph or Decimal('0.00')
            rozdiel_bez_dph = abs(obj_suma - p_bez_dph)
            rozdiel_s_dph = abs(obj_suma - p_s_dph)
            if rozdiel_bez_dph <= Decimal('1.00') or rozdiel_s_dph <= Decimal('1.00'):
                skore += 50
            
        d_obj = obj.datum_prijatia.date() if (obj.datum_prijatia and hasattr(obj.datum_prijatia, 'date')) else obj.datum_prijatia
        d_ponuka = ponuka.datum_vystavenia.date() if (ponuka.datum_vystavenia and hasattr(ponuka.datum_vystavenia, 'date')) else ponuka.datum_vystavenia
        if d_obj and d_ponuka:
            casovy_rozdiel = abs((d_obj - d_ponuka).days)
            if casovy_rozdiel <= 14:
                skore += 20
                
        # Ak ponuka dosiahla aspoň 50 bodov (zhoda čísla alebo sumy), pridáme ju do návrhov.
        # Nepoužívame tie-breaker, pretože pri Zbernej objednávke môže byť správnych viacero ponúk!
        if skore >= 50:
            najdene_zhody_ids.append(ponuka.id)
            
    if najdene_zhody_ids:
        obj.navrhnute_ponuky.add(*najdene_zhody_ids)
        return f"✅ ÚSPECH: Navrhnuté ponuky: {najdene_zhody_ids}."
    return "❌ Nenašla sa zhoda."

@shared_task(base=ISASAutoHealTask)
def task_vyhodnot_smart_matcher_pre_ponuku(ponuka_id):
    """🤖 SMER B: Uložila sa Ponuka -> Hľadáme Objednávku"""
    try:
        # OPTIMALIZÁCIA: Prednačítanie zákazníka pre filter objednávok
        ponuka = CenovaPonuka.objects.select_related('zakaznik').get(id=ponuka_id)
    except CenovaPonuka.DoesNotExist:
        return "Ponuka neexistuje."
        
    # Hľadáme objednávky, ktoré túto konkrétnu ponuku ešte nemajú spárovanú, navrhnutú ani ignorovanú.
    # Nezaujíma nás, či už majú iné ponuky (Zberná objednávka).
    objednavky = PrijataObjednavka.objects.filter(
        zakaznik=ponuka.zakaznik
    ).exclude(
        zdrojove_ponuky=ponuka
    ).exclude(
        navrhnute_ponuky=ponuka
    ).exclude(
        ignorovane_ponuky=ponuka
    )
    
    for obj in objednavky:
        # Zavoláme pôvodný task pre každú relevantnú objednávku
        task_vyhodnot_smart_matcher.delay(obj.id)
        
    return f"Spustené prehodnotenie pre {objednavky.count()} objednávok."

@shared_task(base=ISASAutoHealTask)
def spracuj_import_dat_task(task_id):
    try: task = ImportDatTask.objects.get(pk=task_id)
    except ImportDatTask.DoesNotExist: return "Task neexistuje"

    # 🔒 1. APLIKAČNÝ ZÁMOK (MUTEX)
    lock_id = f"lock_import_{task.typ_importu}"
    aquired = cache.add(lock_id, 'locked', timeout=1800)

    if not aquired:
        task.stav = 'CHYBA'
        task.chybovy_log = "⚠️ Import tohto typu dát už práve prebieha. Zabránené duplicite a kolízii."
        task.save(update_fields=['stav', 'chybovy_log'])
        return "Locked"

    try:
        task.stav = 'SPRACOVAVA_SA'
        task.save(update_fields=['stav'])

        uspesne, duplicity, chyby = 0, 0, 0
        logy = []
        filename = task.subor.name.lower()
    
        # 🧠 1. ZÍSKANIE STRATÉGIE PRE DANÝ TYP IMPORTU
        strategy_func = ISASImportStrategy.get_strategy(task.typ_importu)
        if not strategy_func:
            raise ValueError(f"Nepodporovaný typ importu: {task.typ_importu}")

        # 🛡️ 2. GENERÁTORY PRE OOM OCHRANU (Lazy Loading)
        def get_row_generator():
            if filename.endswith('.csv'):
                task.subor.open('rb')
                # Načítame len malú vzorku pre detekciu separátora
                sample = task.subor.read(2048).decode('utf-8-sig', errors='replace')
                task.subor.seek(0)
                separator = ';' if ';' in sample else ','
            
                # 🛡️ ISAS ARCHITECT: Dynamická detekcia kódovania zamedzujúca poškodeniu diakritiky v Celery workerovi
                encoding_to_use = 'utf-8-sig'
                try:
                    task.subor.seek(0)
                    task.subor.read(4096).decode('utf-8-sig')
                except UnicodeDecodeError:
                    encoding_to_use = 'windows-1250'
                task.subor.seek(0)

                decoded_file = codecs.iterdecode(task.subor, encoding_to_use, errors='replace')
                reader = csv.DictReader(decoded_file, delimiter=separator)
                for row in reader:
                    yield row
                
            elif filename.endswith('.xlsx'):
                task.subor.open('rb')
                # read_only=True zabezpečí, že openpyxl nevytvára v pamäti celý DOM strom
                wb = openpyxl.load_workbook(task.subor, read_only=True, data_only=True)
                sheet = wb.active
                row_iterator = sheet.iter_rows(values_only=True)
            
                try:
                    first_row = next(row_iterator)
                    hlavicky = [str(c).strip() if c else "" for c in first_row]
                except StopIteration:
                    hlavicky = []
                
                for row in row_iterator:
                    row_dict = {hlavicky[i]: str(cell).strip() if cell is not None else "" for i, cell in enumerate(row) if i < len(hlavicky)}
                    if any(row_dict.values()):
                        yield row_dict
                wb.close()

        def filter_rows(generator):
            # 🛡️ ISAS ARCHITECT: Cielené filtrovanie vzorov na konkrétnom stĺpci (Zero False Positives pre všetkých 10 typov)
            VZOROVE_STLPCE_A_HODNOTY = {
                'ZAKAZNICI': ('Nazov_spolocnosti', {'Ukážková Firma s.r.o.'}),
                'DODAVATELIA': ('Nazov_spolocnosti', {'Dodávateľ s.r.o.'}),
                'SUKROMNE_OSOBY': ('email', {'jan.novak@email.sk'}),
                'ZAMESTNANCI': ('email', {'peter@firma.sk'}),
                'VOZIDLA': ('SPZ', {'BA123XY'}),
                'TYPY_SERVISU': ('Nazov', {'Pravidelná kontrola a diagnostika'}),
                'VYROBCOVIA': ('Nazov_vyrobcu', {'Hlavný Dodávateľ / Výrobca'}),
                'FILIALKY': ('Kod_pobocky', {'WH-BA-01'}),
                'SERVISOVANE_ZARIADENIA': ('Vyrobne_cislo', {'SN-001'}),
                'SKLAD_KARTY': ('Seriove_cislo', {'SN-SKLAD-001'}),
            }

            kluc_stlpec, predvolene_vzory = VZOROVE_STLPCE_A_HODNOTY.get(task.typ_importu, (None, set()))
            vzory_pre_typ = set(predvolene_vzory)

            # Dynamické doplnenie odvetvového identifikátora zo SAMPLE_IMPORT_DATA (ak existuje)
            if isinstance(SAMPLE_IMPORT_DATA, dict) and task.typ_importu in SAMPLE_IMPORT_DATA:
                v = SAMPLE_IMPORT_DATA[task.typ_importu]
                if isinstance(v, (list, tuple)) and kluc_stlpec:
                    stlpce = ImportDatTask.STLPCE_MAPA.get(task.typ_importu, [])
                    if kluc_stlpec in stlpce:
                        idx = stlpce.index(kluc_stlpec)
                        if idx < len(v) and str(v[idx]).strip():
                            vzory_pre_typ.add(str(v[idx]).strip())

            for row in generator:
                if not row or not isinstance(row, dict):
                    continue
                hodnoty = [str(val or '').strip() for val in row.values()]
                if not hodnoty or not any(hodnoty):
                    continue
                # 1. Preskočenie riadku s inštrukciami šablóny
                if any(h.startswith('ℹ️ INFO:') for h in hodnoty):
                    continue
                # 2. Presné preskočenie vzorového riadku iba v určenom identifikačnom stĺpci
                if kluc_stlpec and str(row.get(kluc_stlpec) or '').strip() in vzory_pre_typ:
                    continue
                yield row

        # 🚀 3. UNIVERZÁLNY CYKLUS IMPORTU S CHUNKINGOM A SAVEPOINTAMI
        CHUNK_SIZE = 500
        row_gen = filter_rows(get_row_generator())
        global_index = 2
        celkovy_spracovany_pocet = 0

        while True:
            # Vytiahne z generátora presne CHUNK_SIZE riadkov
            chunk = list(itertools.islice(row_gen, CHUNK_SIZE))
            if not chunk:
                break
        
            # Vonkajšia transakcia: Združuje disk I/O pre celý chunk (Extrémne zrýchlenie DB)
            with transaction.atomic():
                for row in chunk:
                    celkovy_spracovany_pocet += 1
                    try:
                        # Vnútorná transakcia (Savepoint): Izoluje chyby jednotlivých riadkov
                        # Ak jeden riadok zlyhá (napr. IntegrityError), nezruší sa celý chunk!
                        with transaction.atomic():
                            ModelClass, query, defaults = strategy_func(row, global_index, logy)
                        
                            existuje = ModelClass.objects.select_for_update().filter(query).first()
                        
                            if existuje:
                                if task.rezim_importu == 'UPDATE':
                                    for key, val in defaults.items(): 
                                        setattr(existuje, key, val)
                                    existuje.save()
                                    uspesne += 1
                                else: 
                                    duplicity += 1
                            else:
                                ModelClass.objects.create(**defaults)
                                uspesne += 1
                            
                    except Exception as e:
                        chyby += 1
                        logy.append(f"Riadok {global_index}: {str(e)}")
                
                    global_index += 1
        
            # Priebežná aktualizácia stavu v DB (Progress tracking pre Frontend)
            task.celkovy_pocet_riadkov = celkovy_spracovany_pocet
            task.uspesne_importovane = uspesne
            task.preskocene_duplicity = duplicity
            task.chybne_riadky = chyby
            task.save(update_fields=['celkovy_pocet_riadkov', 'uspesne_importovane', 'preskocene_duplicity', 'chybne_riadky'])

        # 4. ULOŽENIE FINÁLNYCH ŠTATISTÍK (ISAS CORE: Strict Naive Datetime)
        task.dokoncene = datetime.now()

        if logy: task.chybovy_log = "\n".join(logy)
        if chyby > 0 and uspesne == 0: task.stav = 'CHYBA'
        else: task.stav = 'HOTOVO'

        task.save(update_fields=['dokoncene', 'chybovy_log', 'stav'])
    
        # Bezpečné uvoľnenie file descriptora
        try: task.subor.close()
        except: pass
    
        return f"Dokončené: {uspesne} OK, {duplicity} Duplicity, {chyby} Chýb"

    except Exception as fatal_error:
        task.stav = 'CHYBA'
        task.chybovy_log = f"KRITICKÁ CHYBA PRI ČÍTANÍ SÚBORU:\n{traceback.format_exc()}"
        task.save()
        return "Fatal Error"
    
    finally:
        cache.delete(lock_id)

@shared_task(base=ISASAutoHealTask)
def task_vyhodnot_smart_matcher_faktura_objednavka(faktura_id, cisla_objednavok_ai, surovy_text_faktury):
    """🤖 SMER C: Prijatá Faktúra -> Hľadáme Odoslanú Objednávku (3-Way Matching)"""   
    try:
        faktura = PrichodziaFaktura.objects.select_related('dodavatel').get(id=faktura_id)
        
        if not faktura.dodavatel:
            return "Chýba dodávateľ, párovanie preskočené."

        # Vylúčime tie, ktoré už sú spárované alebo ignorované
        objednavky_dodavatela = OdoslanaObjednavka.objects.filter(
            dodavatel=faktura.dodavatel
        ).exclude(
            id__in=faktura.suvisiace_objednavky.values_list('id', flat=True)
        ).exclude(
            id__in=faktura.ignorovane_objednavky.values_list('id', flat=True)
        )
        
        pevne_zhody = []
        navrhnute_zhody = []
        
        # 1. TVRDÉ PÁROVANIE
        if cisla_objednavok_ai:
            for cislo_ai in cisla_objednavok_ai:
                cislo_clean = str(cislo_ai).strip()
                zhoda = objednavky_dodavatela.filter(cislo_objednavky__iexact=cislo_clean).first()
                if zhoda and zhoda not in pevne_zhody:
                    pevne_zhody.append(zhoda)

        # 2. MÄKKÉ PÁROVANIE (Hľadáme vo zvyšných)
        # Ak chýba PDF text, hľadáme aspoň v manuálnej poznámke faktúry!
        text_na_analyzu = f"{surovy_text_faktury or ''} {faktura.poznamka or ''}"
        
        if text_na_analyzu.strip():
            for obj in objednavky_dodavatela:
                if obj not in pevne_zhody and obj.cislo_objednavky in text_na_analyzu:
                    navrhnute_zhody.append(obj)

        # 3. ZÁPIS DO DATABÁZY
        poznamka_update = ""
        if pevne_zhody:
            faktura.suvisiace_objednavky.add(*pevne_zhody)
            # Odstránime z návrhov, ak tam náhodou boli z minula
            faktura.navrhnute_objednavky.remove(*pevne_zhody)
            cisla_str = ", ".join([o.cislo_objednavky for o in pevne_zhody])
            poznamka_update = f"\n🔗 AI úspešne spárovalo faktúru s objednávkami: {cisla_str}."
            
        if navrhnute_zhody:
            faktura.navrhnute_objednavky.add(*navrhnute_zhody)
            cisla_str = ", ".join([o.cislo_objednavky for o in navrhnute_zhody])
            poznamka_update += f"\n🤖 AI navrhuje prepojenie s objednávkami: {cisla_str} (Vyžaduje potvrdenie)."

        # Zabránenie duplicite textu v poznámke pri opakovanom spustení
        if poznamka_update:
            with transaction.atomic():
                # 🔒 ROW-LEVEL LOCK: Zamkneme riadok, aby sme neprepísali poznámku, ktorú práve píše účtovníčka
                aktualna_faktura_db = PrichodziaFaktura.objects.select_for_update().only('poznamka').get(id=faktura_id)
                aktualna_poznamka = aktualna_faktura_db.poznamka or ''
                if poznamka_update.strip() not in aktualna_poznamka:
                    aktualna_faktura_db.poznamka = f"{aktualna_poznamka}{poznamka_update}"
                    aktualna_faktura_db.save(update_fields=['poznamka'])

        return f"Párovanie ukončené. Pevné: {len(pevne_zhody)}, Návrhy: {len(navrhnute_zhody)}."

    except Exception as e:
        logger.error(f"Chyba pri párovaní faktúry {faktura_id}: {e}")
        return f"Chyba: {e}"
        
    finally:
        # Toto sa vykoná VŽDY, či už párovanie prešlo, alebo padlo na chybe.
        # Účtovníčka tak nikdy nepríde o notifikáciu o novej faktúre.
        posli_email_uctovnicke_task.delay(faktura_id)

@shared_task(base=ISASAutoHealTask)
def task_odosli_notifikaciu_presun(user_name, zdroj_nazov, ciel_nazov, product_name, count, jednotka):
    """Špeciálna Telegram a E-mail notifikácia pre Medziskladové presuny"""
    try:
        gn = GlobalneNastavenia.objects.first()
        if not gn:
            return

        # Zistenie kategórie a podkategórie podľa názvu produktu
        cat_name = "Neznáma kategória"
        subcat_name = "Neznáma podkategória"
        
        # Prehľadáme všetky modely v CATEGORY_MAP, aby sme našli, kam produkt patrí
        cisty_hladany_nazov = str(product_name or '').strip()
        if cisty_hladany_nazov:
            for main_cat, subcats in CATEGORY_MAP.items():
                for sub_name, model_cls in subcats.items():
                    dostupne_stlpce = {f.name for f in model_cls._meta.fields}
                    hladany_stlpec = next((s for s in ['seriove_cislo', 'nazov', 'nazov_polozky', 'model_zariadenia'] if s in dostupne_stlpce), None)
                    if hladany_stlpec and model_cls.objects.filter(**{f"{hladany_stlpec}__icontains": cisty_hladany_nazov}).exists():
                        cat_name = main_cat
                        subcat_name = sub_name
                        break
                if cat_name != "Neznáma kategória":
                    break

        # Priradenie Emoji k hlavnej kategórii
        choices_dict = dict(CATEGORY_CHOICES)
        kategoria_s_emoji = choices_dict.get(cat_name, cat_name)

        safe_user = safe_tg(user_name)
        safe_cat = safe_tg(kategoria_s_emoji)
        safe_subcat = safe_tg(subcat_name)
        safe_prod = safe_tg(product_name)
        safe_zdroj = safe_tg(zdroj_nazov)
        safe_ciel = safe_tg(ciel_nazov)
        mnozstvo_str = f"{count} {jednotka}"
            
        payload = (ISASNotificationBuilder(nadpis='MEDZISKLADOVÝ PRESUN', ikona='🚛', farba_temy='#3b82f6')
                   .pridaj_riadok('👤 Vykonal(a):', safe_user)
                   .pridaj_riadok('📤 ZDROJ:', safe_zdroj)
                   .pridaj_riadok('📥 CIEĽ:', safe_ciel)
                   .pridaj_riadok('🗂️ Kategória:', f"{safe_cat} / {safe_subcat}")
                   .pridaj_riadok('⚙️ Položka:', safe_prod)
                   .pridaj_riadok('🔄 Akcia:', f"PRESUN ({mnozstvo_str})")
                   .nastav_ai_poznamku('Presun bol úspešne zaevidovaný v systéme.')
                   .build())

        try:
            ISASNotifier.send(
                payload=payload,
                send_tg=gn.presun_notif_tg,
                send_mail=gn.presun_notif_mail,
                send_discord=getattr(gn, 'presun_notif_discord', True)
            )
        except Exception as e:
            logger.warning(f"Notifikácia o presune zlyhala: {e}")

    except Exception as e:
        logger.error(f"Kritická chyba v task_odosli_notifikaciu_presun: {e}")

@shared_task(base=ISASAutoHealTask)
def task_vycisti_stare_gps_stopy():
    """
    🧹 ISAS GRAVEDIGGER: Automatické čistenie starých vizuálnych GPS dát.
    Udržuje databázu štíhlu a bleskovo rýchlu.
    Spúšťať cez Celery Beat každý deň o 03:00 ráno.
    """
    # Vypočítame dátum spred 14 dní
    hranica_exspiracie = timezone.now() - timedelta(days=14)
    
    # Zmažeme všetko staršie ako 14 dní
    vymazane_zaznamy, _ = GPSStopa.objects.filter(timestamp__lt=hranica_exspiracie).delete()
    
    logger.info(f"🧹 ISAS Cleanup: Úspešne vymazaných {vymazane_zaznamy} starých GPS bodov.")
    return f"Vymazaných {vymazane_zaznamy} záznamov."

@shared_task(base=ISASAutoHealTask, bind=True, rate_limit='30/m', max_retries=3)
def task_ziskaj_gps_zakaznika(self, zakaznik_id):
    """
    🌍 ISAS ASYNC GEOCODING: Získava GPS súradnice na pozadí.
    Obsahuje Rate Limit (max 30/minútu), aby nás OpenStreetMap nezablokoval pri hromadnom importe.
    """
    try:
        zakaznik = Zakaznik.objects.get(id=zakaznik_id)
    except Zakaznik.DoesNotExist:
        return "Zákazník neexistuje"

    if not zakaznik.ulica_a_cislo or not zakaznik.mesto:
        return "Chýba adresa"

    diely_adresy = [str(p).strip() for p in [zakaznik.ulica_a_cislo, zakaznik.mesto, zakaznik.stat] if p and str(p).strip() and str(p).strip().lower() not in ['none', 'null', 'n/a']]
    adresa = ", ".join(diely_adresy)

    # 1. PRIMÁRNA VOĽBA: Google Maps (Použije dynamický kľúč z DB -> Cache -> .env)
    # Voláme správnu funkciu pre Google Maps
    api_key = get_google_maps_api_key()
    if api_key:
        try:
            url = "https://maps.googleapis.com/maps/api/geocode/json"
            response = requests.get(url, params={'address': adresa, 'key': api_key}, timeout=5)
            if response.status_code == 200:
                data = response.json()
                if data['status'] == 'OK':
                    location = data['results'][0]['geometry']['location']
                    zakaznik.lat = location['lat']
                    zakaznik.lon = location['lng']
                    zakaznik.save(update_fields=['lat', 'lon'])
                    return "Google Maps OK"
        except Exception as e:
            logger.warning(f"Google Maps Geocoding zlyhal: {e}")

    # 2. ZÁLOŽNÁ VOĽBA: Bezplatný OpenStreetMap (Nominatim)
    try:
        headers = {'User-Agent': 'ISAS-Core-App/1.0'}
        url = "https://nominatim.openstreetmap.org/search"
        response = requests.get(url, headers=headers, params={'q': adresa, 'format': 'json', 'limit': 1}, timeout=5)
        
        if response.status_code == 200:
            data = response.json()
            if data:
                zakaznik.lat = float(data[0]['lat'])
                zakaznik.lon = float(data[0]['lon'])
                zakaznik.save(update_fields=['lat', 'lon'])
                return "OSM OK"
            return "OSM Nenašiel adresu"
        elif response.status_code == 429:
            # Ak nás predsa len zablokujú (napr. iný systém na rovnakej IP), skúsime to znova o 60 sekúnd
            raise Exception("OSM Rate Limit Exceeded (429)")
            
    except Exception as e:
        logger.error(f"OSM Geocoding zlyhal pre zákazníka {zakaznik_id}: {e}")
        # Vyvoláme výnimku, aby Celery vedelo, že má task zopakovať (retry)
        raise self.retry(exc=e, countdown=60)

@shared_task(base=ISASAutoHealTask)
def task_odoslat_ziadost_uctovnicke(base_url, notify_tg=True, notify_mail=True, notify_discord=True):
    """Nezávislý Celery task pre odoslanie žiadosti o nastavenie ERP účtovníčke"""
    try:
        FiremnaIdentita = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
        GlobalneNastavenia = apps.get_model('ISASaplikacia', 'GlobalneNastavenia')
        
        identita = FiremnaIdentita.objects.first()
        gn = GlobalneNastavenia.objects.first()
        
        if identita and identita.email_uctovnicky and gn and gn.smtp_user:
            nazov_firmy = identita.nazov_spolocnosti if identita.nazov_spolocnosti else "Naša spoločnosť"
            
            # 🔐 Vygenerovanie bezpečného odkazu s vloženými preferenciami (Stateless)
            signer = TimestampSigner()
            token_data = {
                "action": "erp_setup",
                "tg": notify_tg,
                "mail": notify_mail,
                "discord": notify_discord
            }
            token = signer.sign_object(token_data)
            magic_link = f"{base_url}{reverse('evidencia:accountant-erp-setup', args=[token])}"

            # 📊 Kompletná príprava všetkých relevantných údajov pre účtovníctvo
            nazov_firmy_html = html.escape(nazov_firmy)
            sidlo_html = html.escape(f"{identita.sidlo_ulica or ''} {identita.sidlo_cislo or ''}, {identita.sidlo_psc or ''} {identita.sidlo_mesto or ''}, {identita.sidlo_stat or ''}".strip(", "))
            ico_dic_html = html.escape(f"IČO: {identita.ico or '-'} | DIČ: {identita.dic or '-'}")
            registracie_html = html.escape(identita.zapis_v_registri or 'Nezadané')
            konatel_html = html.escape(identita.konatel or 'Nezadané')
            banka_html = html.escape(f"{identita.nazov_banky or 'Banka'} (IBAN: {identita.iban or '-'}, SWIFT: {identita.swift or '-'})")
            kontakt_html = html.escape(f"{identita.telefon or '-'} | {identita.email_vseobecny or '-'}")
            
            if identita.je_platca_dph:
                dph_text = f"{html.escape(identita.ic_dph)} (Platca DPH)" if identita.ic_dph else "Platca DPH (IČ DPH neuvedené)"
                dph_tuple = ('IČ DPH:', dph_text)
            else:
                dph_tuple = ('DPH Status:', "Nie je platcom DPH")

            payload = (ISASNotificationBuilder(nadpis='NASTAVENIE ÚČTOVNEJ OSNOVY', ikona='⚙️', farba_temy='#10b981')
                       .pridaj_riadok('Firma:', nazov_firmy_html)
                       .pridaj_riadok('Sídlo:', sidlo_html)
                       .pridaj_riadok('Identifikácia:', ico_dic_html)
                       .pridaj_riadok(dph_tuple[0], dph_tuple[1])
                       .pridaj_riadok('Štatutár:', konatel_html)
                       .pridaj_riadok('Banka:', banka_html)
                       .pridaj_riadok('Kontakt:', kontakt_html)
                       .pridaj_riadok('Registrácia:', registracie_html)
                       .nastav_hlavny_text("Dobrý deň,<br><br>prosíme o vyplnenie účtovnej osnovy a predkontácií pre správne prepojenie nášho systému s Vaším účtovným programom.<br><br>Tento odkaz je kryptograficky zabezpečený a platí 7 dní. Dáta sa po potvrdení automaticky implementujú do nášho jadra.")
                       .nastav_tlacidlo('Otvoriť zabezpečený formulár', magic_link)
                       .build())

            try:
                ISASNotifier.send(
                    payload=payload,
                    to_email=identita.email_uctovnicky,
                    send_tg=False,
                    send_mail=True,
                    send_discord=False
                )
                return "Úspešne odoslané"

            except Exception as e:
                logger.error(f"❌ Chyba pri odosielaní žiadosti účtovníčke: {e}")
                return f"Chyba: {str(e)}"

        else:
            return "Chyba: Chýbajú nastavenia SMTP alebo email účtovníčky."
    except Exception as e:
        return f"Kritická chyba: {str(e)}"

@shared_task(base=ISASAutoHealTask)
def task_odoslat_notifikaciu_o_nastaveni_erp(ip_adresa, notify_tg=True, notify_mail=True, notify_discord=True):
    """Nezávislý Celery task pre notifikáciu majiteľa o úspešnom nastavení ERP účtovníčkou."""
    try:
        GlobalneNastavenia = apps.get_model('ISASaplikacia', 'GlobalneNastavenia')
        ERPExportNastavenia = apps.get_model('Evidencia', 'ERPExportNastavenia')
        
        gn = GlobalneNastavenia.objects.first()
        erp = ERPExportNastavenia.objects.first()
        
        if not gn or not erp: 
            return "Chyba: Chýbajú globálne alebo ERP nastavenia."
            
        base_url = (getattr(gn, 'clean_base_url', None) or getattr(settings, 'SITE_URL', '')).strip().rstrip('/')
        
        # Cesta do administrácie k nastaveniam ERP
        cesta = reverse('admin:Evidencia_erpexportnastavenia_change', args=[erp.id])
        url_detail = f"{base_url}{cesta}"
        
        # Príprava dát pre notifikáciu
        erp_nazov = html.escape(erp.get_erp_system_display())
        ucet_pohladavky = html.escape(erp.ucet_pohladavky or 'Nenastavené')
        ucet_zavazky = html.escape(erp.ucet_zavazky or 'Nenastavené')
        ucet_banka = html.escape(erp.ucet_banka) if erp.ucet_banka else "Nenastavené"
        ucet_pokladna = html.escape(erp.ucet_pokladna) if erp.ucet_pokladna else "Nenastavené"
        ucet_dph_val = getattr(erp, 'ucet_dph_zakladna', getattr(erp, 'ucet_dph_20', None))
        ucet_dph = html.escape(str(ucet_dph_val)) if ucet_dph_val else "Nenastavené"
        
        builder = (ISASNotificationBuilder(nadpis='ÚČTOVNÁ OSNOVA NASTAVENÁ', ikona='⚙️', farba_temy='#10b981')
                   .pridaj_riadok('🎯 Zvolený systém:', erp_nazov)
                   .pridaj_riadok('📈 Pohľadávky:', ucet_pohladavky)
                   .pridaj_riadok('📉 Záväzky:', ucet_zavazky)
                   .pridaj_riadok('🏦 Banka:', ucet_banka)
                   .pridaj_riadok('💵 Pokladňa:', ucet_pokladna)
                   .pridaj_riadok('⚖️ Základná DPH:', ucet_dph))
        
        if erp.erp_system == 'POHODA':
            pohoda_fa = html.escape(erp.pohoda_predkontacia_fa) if erp.pohoda_predkontacia_fa else "Nenastavené"
            pohoda_sluzby = html.escape(erp.pohoda_predkontacia_sluzby) if erp.pohoda_predkontacia_sluzby else "Nenastavené"
            pohoda_tovar = html.escape(erp.pohoda_predkontacia_tovar) if erp.pohoda_predkontacia_tovar else "Nenastavené"
            builder.pridaj_riadok('🧾 Predkontácia FA:', pohoda_fa)
            builder.pridaj_riadok('👨‍🔧 Služby / 📦 Tovar:', f"{pohoda_sluzby} / {pohoda_tovar}")
            
        builder.pridaj_riadok('🌐 IP adresa:', ip_adresa)
        builder.nastav_tlacidlo('Skontrolovať nastavenia v ISAS CORE', url_detail)
        builder.nastav_ai_poznamku('Účtovníčka úspešne vyplnila a uložila formulár. Dáta boli automaticky validované a implementované do jadra.')
        
        payload = builder.build()

        try:
            ISASNotifier.send(
                payload=payload,
                send_tg=notify_tg,
                send_mail=notify_mail,
                send_discord=notify_discord
            )
        except Exception as e:
            logger.error(f"Chyba pri odosielaní notifikácie majiteľovi: {e}")
                
    except Exception as e:
        logger.error(f"Kritická chyba v tasku notifikácie ERP: {e}")

@shared_task(base=ISASAutoHealTask, bind=True)
def spracuj_ai_chat_task(self, user_question, history_text, aktualny_pouzivatel, rola_pouzivatela, typ_pomeru, is_superuser, audit_id=None, ma_pristup_k_financiam=False, audio_path=None, file_paths=None, subjekt_id=None):
    """
    Asynchrónny task pre spracovanie AI chatu vrátane multimodálnych príloh.
    """
    task_id = self.request.id
    cache_key = f"ai_chat_result_{task_id}"

    try:
        ai_data = ask_isas_ai(
            user_question=user_question,
            history_text=history_text,
            audio_path=audio_path,
            file_paths=file_paths,
            aktualny_pouzivatel=aktualny_pouzivatel,
            rola_pouzivatela=rola_pouzivatela,
            typ_pomeru=typ_pomeru,
            is_superuser=is_superuser,
            ma_pristup_k_financiam=ma_pristup_k_financiam,
            subjekt_id=subjekt_id
        )
        
        ai_text = ai_data.get("text", "")
        in_tokens = ai_data.get("in_tokens", 0)
        out_tokens = ai_data.get("out_tokens", 0)
        cached_tokens = ai_data.get("cached_tokens", 0)
        
        # 🤖 Zistenie aktívneho modelu z .env
        aktivny_model = getattr(settings, 'GEMINI_MODEL', 'gemini-4-hydrogen')

        # 💰 VÝPOČET PRESNEJ CENY ZA AI MODEL
        cena_usd = vypocitaj_ai_cenu_usd(in_tokens, out_tokens, cached_tokens=cached_tokens, model_name=aktivny_model)
        
        if audit_id:
            AIAuditLog.objects.filter(id=audit_id).update(
                uspesne_dokoncene=True,
                vstupne_tokeny=in_tokens,
                vystupne_tokeny=out_tokens,
                presna_cena_usd=cena_usd,
                odpoved=ai_text,
                pouzity_model=aktivny_model
            )
        
        cache.set(cache_key, {"status": "success", "response": ai_text}, timeout=600)
        
    except Exception as e:
        error_details = str(e) or "Neznáma systémová chyba v AI engine."
        logger.error(f"Kritická chyba v AI Celery Tasku: {error_details}", exc_info=True)
        if audit_id:
            try:
                AIAuditLog.objects.filter(id=audit_id).update(
                    uspesne_dokoncene=False,
                    odpoved=f"❌ Chyba pri spracovaní dopytu: {error_details}"
                )
            except Exception as db_err:
                logger.error(f"Nepodarilo sa zapísať chybu do AIAuditLog: {str(db_err)}")
        cache.set(cache_key, {"status": "error", "message": f"Asistent zlyhal pri spracovaní požiadavky: {error_details}"}, timeout=600)
        
    finally:
        # 🧹 ROBUSTNÉ UPRATANIE DOČASNÉHO PRIEČINKA A AUDIO SÚBOROV Z DISKU SERVERA
        if file_paths and len(file_paths) > 0:
            import shutil
            try:
                parent_temp_dir = os.path.dirname(file_paths[0])
                if os.path.exists(parent_temp_dir) and 'ai_temp_uploads' in parent_temp_dir:
                    shutil.rmtree(parent_temp_dir, ignore_errors=True)
            except Exception as cleanup_err:
                logger.warning(f"Chyba pri čistení dočasného adresára {file_paths[0]}: {cleanup_err}")
        if audio_path and os.path.exists(audio_path) and 'ai_temp_uploads' in audio_path:
            try:
                os.remove(audio_path)
            except Exception as audio_err:
                logger.warning(f"Chyba pri mazaní audio súboru {audio_path}: {audio_err}")

@shared_task(base=ISASAutoHealTask, bind=True, max_retries=3)
def task_vygeneruj_mzdy_excel(self, user_id, zaloha_id, zamestnanci_ids=None):
    """
    🧠 ISAS ELITE TASK 1: Generuje inteligentný Excel pre účtovníčku.
    Vracia zaloha_id pre reťazenie (chain) do ďalšieho tasku.
    """
    lock_id = f"lock_excel_mzdy_{zaloha_id}"
    
    with isas_task_execution_guard(lock_id, timeout=300, fallback_model=MegaZaloha, fallback_id=zaloha_id) as (acquired, gn, identita):
        if not acquired:
            return
         
        zaloha = MegaZaloha.objects.filter(pk=zaloha_id).first()
        if not zaloha:
            logger.error(f"MegaZáloha ID #{zaloha_id} nebola nájdená pri generovaní miezd.")
            return
        nastavenia = GlobalneNastavenia.objects.first() or GlobalneNastavenia()
        
        datum_od = zaloha.obsahuje_data_od
        datum_do = zaloha.obsahuje_data_do

        # 🛡️ USE_TZ=False kompatibilita: Pracujeme striktne s čistým naivným lokálnym časom (zamedzí RuntimeError)
        dt_od = datetime.combine(datum_od, time.min) if datum_od else None
        dt_do = datetime.combine(datum_do, time.max) if datum_do else None

        builder = ISASExcelBuilder()
        wb = builder.wb

        # =====================================
        # 🟢 LIST 1: INTERNÍ ZAMESTNANCI (TPP)
        # =====================================
        User = get_user_model()
        user = User.objects.filter(id=user_id).first()

        ws_tpp = wb.active
        ws_tpp.title = "TPP - Mzdy a Diéty"
        
        firemna_mena = get_firemna_mena()
        headers_tpp = ["Meno a Priezvisko", "Rola", "Spôsob odmeny", f"Sadzba ({firemna_mena})", "Hodiny (Cesty)", "Hodiny (Servis)", "Spolu Hodiny", "Dovolenka (Dni)", "PN (Dni)", "Paragraf (Dni)", f"Hrubá Mzda ({firemna_mena})", f"Diéty ({firemna_mena})", f"Zrážky ({firemna_mena})", "Dôvod zrážky"]
        ws_tpp.append(headers_tpp)
        
        for col_num in range(1, len(headers_tpp) + 1):
            ws_tpp.column_dimensions[get_column_letter(col_num)].width = 20
            
        builder.apply_header_style(ws_tpp, 1, "0F172A")

        interni = Zamestnanec.objects.filter(typ_pomeru='IN')
        if zamestnanci_ids:
            interni = interni.filter(id__in=zamestnanci_ids)
        
        for zam in interni:
            jazdy_qs = EvidenciaJazd.objects.filter(Q(zamestnanec=zam) | Q(spolujazdci=zam), datum_cas_koniec__isnull=False).distinct()
            if datum_od and datum_do:
                jazdy_qs = jazdy_qs.filter(datum_cas_start__date__lte=datum_do, datum_cas_koniec__date__gte=datum_od)

            hodiny_cesty = Decimal('0.00')
            jazdy_podla_dni = defaultdict(lambda: {'hodiny': Decimal('0.00'), 'bezplatna_strava': False})

            for jazda in jazdy_qs:
                # 🛡️ USE_TZ=False kompatibilita: Dáta v DB sú naivné, odstraňujeme tzinfo pre bezpečné porovnávanie
                j_start = jazda.datum_cas_start.replace(tzinfo=None) if getattr(jazda.datum_cas_start, 'tzinfo', None) else jazda.datum_cas_start
                j_koniec = jazda.datum_cas_koniec.replace(tzinfo=None) if getattr(jazda.datum_cas_koniec, 'tzinfo', None) else jazda.datum_cas_koniec
                if not j_start or not j_koniec:
                    continue
                start = max(j_start, dt_od) if dt_od else j_start
                koniec = min(j_koniec, dt_do) if dt_do else j_koniec
                
                if start < koniec:
                    trvanie_h = Decimal(str((koniec - start).total_seconds() / 3600))
                    hodiny_cesty += trvanie_h
                    
                    den = timezone.localtime(start).date() if timezone.is_aware(start) else start.date()
                    jazdy_podla_dni[den]['hodiny'] += trvanie_h
                    if jazda.bezplatne_stravovanie:
                        jazdy_podla_dni[den]['bezplatna_strava'] = True

            celkove_diety = Decimal('0.00')
            for den, data in jazdy_podla_dni.items():
                h = data['hodiny']
                zakladna_dieta = Decimal('0.00')
                
                def _bezpecny_decimal(val):
                    try:
                        return Decimal(str(val).replace(',', '.')) if (val is not None and str(val).strip() != '') else Decimal('0.00')
                    except Exception:
                        return Decimal('0.00')

                s_5_12 = _bezpecny_decimal(getattr(nastavenia, 'stravne_5_12h', None))
                s_12_18 = _bezpecny_decimal(getattr(nastavenia, 'stravne_12_18h', None))
                s_nad_18 = _bezpecny_decimal(getattr(nastavenia, 'stravne_nad_18h', None))

                if 5 <= h < 12: zakladna_dieta = s_5_12
                elif 12 <= h < 18: zakladna_dieta = s_12_18
                elif h >= 18: zakladna_dieta = s_nad_18

                if data['bezplatna_strava'] and zakladna_dieta > 0:
                    kratenie = s_nad_18 * Decimal('0.40')
                    zakladna_dieta = max(Decimal('0.00'), zakladna_dieta - kratenie)
                celkove_diety += zakladna_dieta

            ulohy_qs = Uloha.objects.filter(technici=zam, dokoncene=True, zaciatok__isnull=False).prefetch_related('typ_servisu')
            if datum_od and datum_do:
                ulohy_qs = ulohy_qs.filter(Q(zaciatok__date__lte=datum_do) & (Q(koniec__date__gte=datum_od) | Q(koniec__isnull=True, zaciatok__date__gte=datum_od)))

            hodiny_servis = Decimal('0.00')
            for u in ulohy_qs:
                if u.koniec:
                    u_zac = u.zaciatok.replace(tzinfo=None) if getattr(u.zaciatok, 'tzinfo', None) else u.zaciatok
                    u_kon = u.koniec.replace(tzinfo=None) if getattr(u.koniec, 'tzinfo', None) else u.koniec
                    start = max(u_zac, dt_od) if dt_od else u_zac
                    koniec = min(u_kon, dt_do) if dt_do else u_kon
                    
                    if start < koniec:
                        rozdiel = koniec - start
                        if rozdiel.days == 0:
                            hodiny_servis += Decimal(str(rozdiel.total_seconds() / 3600))
                        else:
                            hodiny_servis += Decimal(str((rozdiel.days + 1) * 8))
                else:
                    if (not datum_od or u.zaciatok.date() >= datum_od) and (not datum_do or u.zaciatok.date() <= datum_do):
                        for praca in u.typ_servisu.all():
                            hodiny_servis += Decimal(str(praca.normohodiny or '1.00'))

            spolu_hodiny = hodiny_cesty + hodiny_servis

            absencie_qs = Nepritomnost.objects.filter(zamestnanec=zam, stav='SCHVALENE')
            if datum_od and datum_do:
                absencie_qs = absencie_qs.filter(datum_od__lte=datum_do, datum_do__gte=datum_od)
            
            dni_dovolenka, dni_pn, dni_paragraf = 0.0, 0.0, 0.0
            
            for absencia in absencie_qs:
                if not absencia.datum_od or not absencia.datum_do:
                    continue
                start_date = max(absencia.datum_od, datum_od) if datum_od else absencia.datum_od
                end_date = min(absencia.datum_do, datum_do) if datum_do else absencia.datum_do
                
                if absencia.pol_dna:
                    if start_date <= absencia.datum_od <= end_date:
                        if absencia.typ == 'DOVOLENKA': dni_dovolenka += 0.5
                        elif absencia.typ == 'PARAGRAF': dni_paragraf += 0.5
                else:
                    curr = start_date
                    while curr <= end_date:
                        if absencia.typ == 'PN':
                            dni_pn += 1.0
                        elif curr.weekday() < 5:
                            if absencia.typ == 'DOVOLENKA': dni_dovolenka += 1.0
                            elif absencia.typ == 'PARAGRAF': dni_paragraf += 1.0
                        curr += timedelta(days=1)

            hruba_mzda = Decimal('0.00')
            sadzba_text = ""
            if zam.sposob_odmenovania == 'HODINA':
                hruba_mzda = spolu_hodiny * (zam.mzda_brutto_hodina or Decimal('0.00'))
                sadzba_text = zam.mzda_brutto_hodina or "0.00"
            elif zam.sposob_odmenovania == 'MESIAC':
                hruba_mzda = zam.mzda_brutto_mesiac or Decimal('0.00')
                sadzba_text = f"{zam.mzda_brutto_mesiac or '0.00'} (Paušál)"

            zavazok_zam = zam.zavazky or Decimal('0.00')
            row = [
                f"{zam.meno} {zam.priezvisko}", zam.get_pracovna_rola_display(), zam.get_sposob_odmenovania_display(),
                sadzba_text, round(hodiny_cesty, 2), round(hodiny_servis, 2), round(spolu_hodiny, 2),
                dni_dovolenka, dni_pn, dni_paragraf,
                round(hruba_mzda, 2), celkove_diety, zavazok_zam, zam.poznamka_zavazky if zam.poznamka_zavazky else "—"
            ]
            ws_tpp.append(row)
            
            last_row = ws_tpp.max_row
            for col in [4, 11, 12, 13]:
                if isinstance(ws_tpp.cell(row=last_row, column=col).value, (int, float, Decimal)):
                    ws_tpp.cell(row=last_row, column=col).number_format = builder.currency_format
            
            if zam.zavazky and zam.zavazky > 0: ws_tpp.cell(row=last_row, column=13).font = Font(color="DC2626", bold=True)
            for col in range(1, 15): ws_tpp.cell(row=last_row, column=col).border = builder.thin_border

        # ====================================
        # 🔵 LIST 2: EXTERNISTI (ŽIVNOSTNÍCI)
        # ====================================
        ws_ex = wb.create_sheet(title="Živnostníci - Fakturácia")
        headers_ex = ["Meno a Priezvisko", "Spôsob odmeny", f"Sadzba ({firemna_mena}/hod)", "Hodiny (Cesty)", "Hodiny (Servis)", "Spolu Hodiny", "Hrubá suma na fakturáciu", f"Zrážky / Zápočet ({firemna_mena})", "Dôvod zrážky", "Čistá suma na úhradu"]
        ws_ex.append(headers_ex)
        
        for col_num in range(1, len(headers_ex) + 1):
            ws_ex.column_dimensions[get_column_letter(col_num)].width = 20
            
        builder.apply_header_style(ws_ex, 1, "1E3A8A")

        externisti = Zamestnanec.objects.filter(typ_pomeru='EX')
        if zamestnanci_ids:
            externisti = externisti.filter(id__in=zamestnanci_ids)
        
        for zam in externisti:
            jazdy_qs = EvidenciaJazd.objects.filter(Q(zamestnanec=zam) | Q(spolujazdci=zam), datum_cas_koniec__isnull=False).distinct()
            if datum_od and datum_do:
                jazdy_qs = jazdy_qs.filter(datum_cas_start__date__lte=datum_do, datum_cas_koniec__date__gte=datum_od)

            hodiny_cesty = Decimal('0.00')
            for jazda in jazdy_qs:
                j_start = jazda.datum_cas_start.replace(tzinfo=None) if getattr(jazda.datum_cas_start, 'tzinfo', None) else jazda.datum_cas_start
                j_koniec = jazda.datum_cas_koniec.replace(tzinfo=None) if getattr(jazda.datum_cas_koniec, 'tzinfo', None) else jazda.datum_cas_koniec
                if not j_start or not j_koniec:
                    continue
                start = max(j_start, dt_od) if dt_od else j_start
                koniec = min(j_koniec, dt_do) if dt_do else j_koniec

                if start < koniec:
                    hodiny_cesty += Decimal(str((koniec - start).total_seconds() / 3600))

            ulohy_qs = Uloha.objects.filter(technici=zam, dokoncene=True, zaciatok__isnull=False).prefetch_related('typ_servisu')
            if datum_od and datum_do:
                ulohy_qs = ulohy_qs.filter(Q(zaciatok__date__lte=datum_do) & (Q(koniec__date__gte=datum_od) | Q(koniec__isnull=True, zaciatok__date__gte=datum_od)))

            hodiny_servis = Decimal('0.00')
            for u in ulohy_qs:
                if u.koniec:
                    u_zac = u.zaciatok.replace(tzinfo=None) if getattr(u.zaciatok, 'tzinfo', None) else u.zaciatok
                    u_kon = u.koniec.replace(tzinfo=None) if getattr(u.koniec, 'tzinfo', None) else u.koniec
                    start = max(u_zac, dt_od) if dt_od else u_zac
                    koniec = min(u_kon, dt_do) if dt_do else u_kon
                    if start < koniec:
                        rozdiel = koniec - start
                        if rozdiel.days == 0:
                            hodiny_servis += Decimal(str(rozdiel.total_seconds() / 3600))
                        else:
                            hodiny_servis += Decimal(str((rozdiel.days + 1) * 8))
                else:
                    if (not datum_od or u.zaciatok.date() >= datum_od) and (not datum_do or u.zaciatok.date() <= datum_do):
                        for praca in u.typ_servisu.all():
                            hodiny_servis += Decimal(str(praca.normohodiny or '1.00'))

            spolu_hodiny = hodiny_cesty + hodiny_servis
            zavazok = zam.zavazky or Decimal('0.00')

            if zam.sposob_odmenovania == 'UKOL':
                suma_faktura = "ÚKOL (Doplniť)"
                cista_suma = "Doplniť ručne"
                sadzba_text = "ÚKOL"
            else:
                suma_faktura = round(spolu_hodiny * (zam.mzda_zivnostnik_hodina or Decimal('0.00')), 2)
                cista_suma = round(suma_faktura - zavazok, 2)
                sadzba_text = zam.mzda_zivnostnik_hodina or "0.00"

            row = [
                f"{zam.meno} {zam.priezvisko}", zam.get_sposob_odmenovania_display(), sadzba_text,
                round(hodiny_cesty, 2), round(hodiny_servis, 2), round(spolu_hodiny, 2),
                suma_faktura, zavazok, zam.poznamka_zavazky if zam.poznamka_zavazky else "—", cista_suma
            ]
            ws_ex.append(row)
            
            last_row = ws_ex.max_row
            for col in [3, 7, 8, 10]:
                if isinstance(ws_ex.cell(row=last_row, column=col).value, (int, float, Decimal)):
                    ws_ex.cell(row=last_row, column=col).number_format = builder.currency_format
            
            if zavazok > 0: ws_ex.cell(row=last_row, column=8).font = Font(color="DC2626", bold=True)
            if isinstance(cista_suma, Decimal): ws_ex.cell(row=last_row, column=10).font = Font(color="059669", bold=True)
            for col in range(1, 11): ws_ex.cell(row=last_row, column=col).border = builder.thin_border

        # ===========================================
        # 🟠 LIST 3: OSTATNÍ (BRIGÁDNICI A KONATELIA)
        # ===========================================
        ws_ost = wb.create_sheet(title="Ostatní (Dohodári a Konatelia)")
        headers_ost = ["Meno a Priezvisko", "Typ pomeru", "Spôsob odmeny", f"Sadzba ({firemna_mena})", "Hodiny (Cesty)", "Hodiny (Servis)", "Spolu Hodiny", f"Hrubá Mzda ({firemna_mena})", f"Zrážky ({firemna_mena})", "Dôvod zrážky"]
        ws_ost.append(headers_ost)
        
        for col_num in range(1, len(headers_ost) + 1):
            ws_ost.column_dimensions[get_column_letter(col_num)].width = 20
            
        builder.apply_header_style(ws_ost, 1, "D97706")

        ostatni = Zamestnanec.objects.filter(typ_pomeru__in=['BR', 'KO'])
        if zamestnanci_ids:
            ostatni = ostatni.filter(id__in=zamestnanci_ids)
        
        for zam in ostatni:
            jazdy_qs = EvidenciaJazd.objects.filter(Q(zamestnanec=zam) | Q(spolujazdci=zam), datum_cas_koniec__isnull=False).distinct()
            if datum_od and datum_do:
                jazdy_qs = jazdy_qs.filter(datum_cas_start__date__lte=datum_do, datum_cas_koniec__date__gte=datum_od)

            hodiny_cesty = Decimal('0.00')
            for jazda in jazdy_qs:
                j_start = jazda.datum_cas_start.replace(tzinfo=None) if getattr(jazda.datum_cas_start, 'tzinfo', None) else jazda.datum_cas_start
                j_koniec = jazda.datum_cas_koniec.replace(tzinfo=None) if getattr(jazda.datum_cas_koniec, 'tzinfo', None) else jazda.datum_cas_koniec
                if not j_start or not j_koniec:
                    continue
                start = max(j_start, dt_od) if dt_od else j_start
                koniec = min(j_koniec, dt_do) if dt_do else j_koniec

                if start < koniec:
                    hodiny_cesty += Decimal(str((koniec - start).total_seconds() / 3600))

            ulohy_qs = Uloha.objects.filter(technici=zam, dokoncene=True, zaciatok__isnull=False).prefetch_related('typ_servisu')
            if datum_od and datum_do:
                ulohy_qs = ulohy_qs.filter(Q(zaciatok__date__lte=datum_do) & (Q(koniec__date__gte=datum_od) | Q(koniec__isnull=True, zaciatok__date__gte=datum_od)))

            hodiny_servis = Decimal('0.00')
            for u in ulohy_qs:
                if u.koniec:
                    u_zac = u.zaciatok.replace(tzinfo=None) if getattr(u.zaciatok, 'tzinfo', None) else u.zaciatok
                    u_kon = u.koniec.replace(tzinfo=None) if getattr(u.koniec, 'tzinfo', None) else u.koniec
                    start = max(u_zac, dt_od) if dt_od else u_zac
                    koniec = min(u_kon, dt_do) if dt_do else u_kon
                    if start < koniec:
                        rozdiel = koniec - start
                        if rozdiel.days == 0:
                            hodiny_servis += Decimal(str(rozdiel.total_seconds() / 3600))
                        else:
                            hodiny_servis += Decimal(str((rozdiel.days + 1) * 8))
                else:
                    if (not datum_od or u.zaciatok.date() >= datum_od) and (not datum_do or u.zaciatok.date() <= datum_do):
                        for praca in u.typ_servisu.all():
                            hodiny_servis += Decimal(str(praca.normohodiny or '1.00'))

            spolu_hodiny = hodiny_cesty + hodiny_servis
            
            hruba_mzda = Decimal('0.00')
            sadzba_text = ""
            if zam.sposob_odmenovania == 'HODINA':
                hruba_mzda = spolu_hodiny * (zam.mzda_brutto_hodina or Decimal('0.00'))
                sadzba_text = zam.mzda_brutto_hodina or "0.00"
            elif zam.sposob_odmenovania == 'MESIAC':
                hruba_mzda = zam.mzda_brutto_mesiac or Decimal('0.00')
                sadzba_text = f"{zam.mzda_brutto_mesiac or '0.00'} (Paušál)"
            else:
                hruba_mzda = "ÚKOL (Doplniť)"
                sadzba_text = "ÚKOL"

            zavazok_ost = zam.zavazky or Decimal('0.00')
            row = [
                f"{zam.meno} {zam.priezvisko}", zam.get_typ_pomeru_display(), zam.get_sposob_odmenovania_display(),
                sadzba_text, round(hodiny_cesty, 2), round(hodiny_servis, 2), round(spolu_hodiny, 2),
                hruba_mzda if isinstance(hruba_mzda, str) else round(hruba_mzda, 2), 
                zavazok_ost, zam.poznamka_zavazky if zam.poznamka_zavazky else "—"
            ]
            ws_ost.append(row)
            
            last_row = ws_ost.max_row
            for col in [4, 8, 9]:
                if isinstance(ws_ost.cell(row=last_row, column=col).value, (int, float, Decimal)):
                    ws_ost.cell(row=last_row, column=col).number_format = builder.currency_format
            
            if zam.zavazky and zam.zavazky > 0: ws_ost.cell(row=last_row, column=9).font = Font(color="DC2626", bold=True)
            for col in range(1, 11): ws_ost.cell(row=last_row, column=col).border = builder.thin_border

        return builder.save_to_megazaloha(
            zaloha_id=zaloha_id, 
            filename_prefix="Mzdy_a_Fakturacia_ISAS_CORE", 
            user=user,
            default_name="Mzdy a Fakturácia", 
            default_note="Vygenerované z modulu Mzdy",
            typ_zalohy='mzdy'
        )

@shared_task(base=ISASAutoHealTask)
def task_odoslat_mzdy_uctovnicke(zaloha_id):
    """
    🧠 ISAS ELITE TASK 2: Nezávislý Celery task pre odoslanie už vygenerovaných miezd účtovníčke.
    Dizajn e-mailu je na pixel zhodný s Knihou jázd.
    """
    try:
        FiremnaIdentita = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
        GlobalneNastavenia = apps.get_model('ISASaplikacia', 'GlobalneNastavenia')
        
        # Ak predchádzajúci task zlyhal, zaloha_id môže byť Exception objekt (v Celery chaine).
        if isinstance(zaloha_id, Exception):
            logger.error("❌ Predchádzajúci task zlyhal, e-mail sa neodošle.")
            return "Zrušené kvôli chybe v predchádzajúcom kroku."

        zaloha = MegaZaloha.objects.get(id=zaloha_id)
        if not zaloha.subor_zalohy:
            return "Chyba: Súbor neexistuje."

        identita = FiremnaIdentita.objects.first()
        gn = GlobalneNastavenia.objects.first()
        
        if identita and identita.email_uctovnicky and gn and gn.smtp_user:
            vygeneroval = get_safe_employee_name(zaloha.vyziadal_uzivatel, "Systém")
            
            payload = (ISASNotificationBuilder(nadpis='PODKLADY PRE MZDY A FAKTURÁCIU', ikona='💰', farba_temy='#10b981')
                       .pridaj_riadok('Názov súboru:', zaloha.subor_zalohy.name.split('/')[-1])
                       .pridaj_riadok('Vygeneroval:', vygeneroval)
                       .nastav_hlavny_text("Dobrý deň,<br><br>systém práve vygeneroval nové podklady pre mzdy a fakturáciu. V prílohe nájdete Excel súbor pre Vašu účtovnú evidenciu.")
                       .build())

            zaloha.subor_zalohy.seek(0)
            attachments = [{
                'filename': zaloha.subor_zalohy.name.split('/')[-1],
                'content': zaloha.subor_zalohy.read(),
                'mimetype': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            }]

            try:
                ISASNotifier.send(
                    payload=payload,
                    to_email=identita.email_uctovnicky,
                    send_tg=False,
                    send_mail=True,
                    send_discord=False,
                    attachments=attachments
                )
                return "Úspešne odoslané"
            except Exception as e:
                logger.error(f"❌ Chyba pri odosielaní miezd účtovníčke: {e}")
                return f"Chyba: {str(e)}"

        else:
            return "Chyba: Chýbajú nastavenia SMTP alebo email účtovníčky."
    except Exception as e:
        return f"Kritická chyba: {str(e)}"

@shared_task(base=ISASAutoHealTask)
def task_tyzdenna_predikcia_nakupu():
    """
    🛒 ISAS CONFIDENCE-BASED BUYER: Týždenný report predikcie nákupu.
    Spúšťať cez Celery Beat každý pondelok o 06:00.
    """
    gn = GlobalneNastavenia.objects.first()
    if not gn:
        return "Chýbajú globálne nastavenia."
        
    # 🛑 KONTROLA PREPÍNAČA: Ak je predikcia vypnutá, task sa okamžite ukončí
    if not getattr(gn, 'zapnut_predikciu_nakupu', True):
        logger.info("🛒 Predikcia nákupu je globálne vypnutá. Task ukončený.")
        return "Predikcia nákupu je vypnutá."
        
    # DYNAMICKÉ NAČÍTANIE Z DATABÁZY (Bez hardkódu, fallback na 14 dní ak pole neexistuje)
    dni_predikcie = getattr(gn, 'dni_predikcie_nakupu', None) or 14
    dnes = timezone.now().date()
    limit_dni = dnes + timedelta(days=dni_predikcie)
    
    # 1. Nájdeme všetky plánované servisy v tomto okne
    plany = PlanovanyServisZariadenia.objects.filter(
        planovany_datum__gte=dnes,
        planovany_datum__lte=limit_dni
    ).select_related('zariadenie', 'typ_servisu')
    
    # 1.B Nájdeme aj všetky reálne Úlohy (napr. zo Smart Bookingu), ktoré majú stroj a typ servisu
    Uloha = apps.get_model('Evidencia', 'Uloha')
    ulohy = Uloha.objects.filter(
        dokoncene=False,
        zaciatok__date__gte=dnes,
        zaciatok__date__lte=limit_dni,
        zariadenia__isnull=False,
        typ_servisu__isnull=False
    ).prefetch_related('zariadenia', 'typ_servisu').distinct()
    
    if not plany.exists() and not ulohy.exists():
        return "Žiadne plánované servisy ani úlohy na najbližšie obdobie."
        
    # 2. Agregujeme potrebný materiál podľa servisných predpisov (kusovníkov)
    potrebny_material = {} 
    ServisnyPredpis = apps.get_model('Evidencia', 'ServisnyPredpis')

    # Hromadné prednačítanie predpisov pre plány aj úlohy (O(1) In-Memory Lookup)
    vsetky_zar_ids = set(p.zariadenie_id for p in plany)
    vsetky_typy_ids = set(p.typ_servisu_id for p in plany)
    for u in ulohy:
        vsetky_zar_ids.update(z.id for z in u.zariadenia.all())
        vsetky_typy_ids.update(t.id for t in u.typ_servisu.all())

    predpisy_mapa = {}
    if vsetky_zar_ids and vsetky_typy_ids:
        predpisy_qs = ServisnyPredpis.objects.filter(
            zariadenie_id__in=vsetky_zar_ids,
            typ_prace_id__in=vsetky_typy_ids
        ).prefetch_related('polozky__content_object')
        for pred in predpisy_qs:
            predpisy_mapa[(pred.zariadenie_id, pred.typ_prace_id)] = pred
    
    # A) Spracovanie dlhodobých plánov
    for plan in plany:
        predpis = predpisy_mapa.get((plan.zariadenie_id, plan.typ_servisu_id))
        
        if predpis:
            for polozka in predpis.polozky.all():
                if polozka.content_object:
                    key = (polozka.content_type_id, polozka.object_id)
                    if key not in potrebny_material:
                        potrebny_material[key] = {
                            'nazov': ocisti_nazov_od_jednotky(str(polozka.content_object)),
                            'potrebne': Decimal('0.00'),
                            'obj': polozka.content_object,
                            'jednotka': getattr(polozka.content_object, 'merna_jednotka', 'ks')
                        }
                    potrebny_material[key]['potrebne'] += Decimal(str(polozka.mnozstvo or '0.00'))

    # B) Spracovanie ad-hoc úloh (využíva už načítanú O(1) predpisy_mapa z kroku vyššie)
    for uloha in ulohy:
        for zariadenie in uloha.zariadenia.all():
            for typ in uloha.typ_servisu.all():
                predpis = predpisy_mapa.get((zariadenie.id, typ.id))
                
                if predpis:
                    for polozka in predpis.polozky.all():
                        if polozka.content_object:
                            key = (polozka.content_type_id, polozka.object_id)
                            if key not in potrebny_material:
                                potrebny_material[key] = {
                                    'nazov': ocisti_nazov_od_jednotky(str(polozka.content_object)),
                                    'potrebne': Decimal('0.00'),
                                    'obj': polozka.content_object,
                                    'jednotka': getattr(polozka.content_object, 'merna_jednotka', 'ks')
                                }
                            potrebny_material[key]['potrebne'] += Decimal(str(polozka.mnozstvo or '0.00'))
                    
    # 3. Porovnáme so skutočným stavom na skladoch
    na_doobjednanie = []
    filialky = Filialka.objects.filter(je_aktivna=True)
    
    # 🛡️ Normalizácia kľúčov z LedgerKalkulacky pre 100% zhodu (odstránenie jednotiek)
    stavy_filialok = {}
    for f in filialky:
        surove_stavy = LedgerKalkulacka.zisti_stav_vsetkych_poloziek(f)
        stavy_filialok[f.id] = {ocisti_nazov_od_jednotky(k): v for k, v in surove_stavy.items()}
    
    for key, data in potrebny_material.items():
        dostupne_spolu = Decimal(str(getattr(data['obj'], 'pocet_na_sklade', 0) or '0.00'))
        for f_id, stavy in stavy_filialok.items():
            dostupne_spolu += Decimal(str(stavy.get(data['nazov'], 0) or '0.00'))
            
        if dostupne_spolu < data['potrebne']:
            chyba = data['potrebne'] - dostupne_spolu
            na_doobjednanie.append(f"🔸 <b>{data['nazov']}</b><br>Potrebné: {data['potrebne']} {data['jednotka']} | Skladom: {dostupne_spolu} {data['jednotka']} | <b>Chýba: {chyba} {data['jednotka']}</b>")
            
    # 4. Odoslanie reportu
    if na_doobjednanie:
        zoznam_text = "<br>".join(na_doobjednanie)
        payload = (ISASNotificationBuilder(nadpis=f'PREDIKCIA NÁKUPU ({dni_predikcie} DNÍ)', ikona='🛒', farba_temy='#f59e0b')
                   .pridaj_riadok('Obdobie:', f"{dnes.strftime('%d.%m.%Y')} - {limit_dni.strftime('%d.%m.%Y')}")
                   .pridaj_riadok('Plánované servisy:', f"{plany.count()} výjazdov")
                   .nastav_hlavny_text(f"<b>Na základe servisných predpisov bude chýbať tento materiál:</b><br><br>{zoznam_text}")
                   .nastav_ai_poznamku('Odporúčame preveriť dodacie lehoty a materiál včas doobjednať.')
                   .build())
                   
        ISASNotifier.send(
            payload=payload,
            send_tg=True,
            send_mail=True,
            send_discord=True
        )
        return f"Odoslaný report pre {len(na_doobjednanie)} chýbajúcich položiek."
    else:
        return f"Materiál na najbližších {dni_predikcie} dní je plne pokrytý skladom."

@shared_task(base=ISASAutoHealTask)
def task_ai_audit_ulohy(uloha_id):
    """
    🚨 AI DISPATCH AUDITOR: Tichý strážny pes dispečera.
    Kontroluje logiku, BOZP a prejazdy po uložení úlohy.
    """
    lock_id = f"lock_ai_audit_ulohy_{uloha_id}"
    
    with isas_task_execution_guard(lock_id, timeout=120) as (acquired, gn, identita):
        if not acquired:
            return "Audit už prebieha."

        try:
            uloha = Uloha.objects.select_related('zakaznik').prefetch_related('technici', 'typ_servisu').get(id=uloha_id)
        except Uloha.DoesNotExist:
            return "Úloha neexistuje."

        # Ak má dispečer vypnuté AI varovania, task potichu skončí
        if not getattr(gn, 'zapnut_ai_upozornenia_dispecingu', True):
            return "AI upozornenia sú globálne vypnuté."

        api_key = get_gemini_api_key()
        gemini_model = getattr(settings, 'GEMINI_MODEL', None)
        if not api_key or not gemini_model:
            return "Chýba API kľúč alebo nie je definovaný GEMINI_MODEL v nastaveniach."

        if not uloha.zaciatok:
            return "Úloha nemá nastavený dátum a čas začiatku, audit logistiky nie je možný."

        # 1. Zber dát pre AI
        dnes = timezone.now().date()
        technici_data = []
        for t in uloha.technici.all():
            platne_osvedcenia = [
                c.nazov for c in t.certifikaty.filter(Q(je_dozivotny=True) | Q(datum_platnosti__gte=dnes))
            ]
            technici_data.append({
                "meno": f"{t.meno} {t.priezvisko}",
                "bozp_platne_do": t.bozp_platne_do.strftime('%Y-%m-%d') if hasattr(t.bozp_platne_do, 'strftime') else str(t.bozp_platne_do or "Nezadané"),
                "platne_certifikaty_a_sposobilosti": ", ".join(platne_osvedcenia) if platne_osvedcenia else "Žiadne evidované"
            })

        typy_prac = [tp.nazov for tp in uloha.typ_servisu.all()]
        odvetvova_operativa = get_industry_operativa_prompt()

        # Zistenie predchádzajúcej a nasledujúcej úlohy pre 360° kontrolu prejazdu
        predchadzajuca_uloha = None
        nasledujuca_uloha = None
        den_ulohy = uloha.zaciatok.date() if hasattr(uloha.zaciatok, 'date') else uloha.zaciatok
        spolocne_ulohy = Uloha.objects.filter(technici__in=uloha.technici.all(), zaciatok__date=den_ulohy).exclude(id=uloha.id)
        
        predchadzajuca_uloha = spolocne_ulohy.filter(koniec__lte=uloha.zaciatok).order_by('-koniec').first()
        nasledujuca_uloha = spolocne_ulohy.filter(zaciatok__gte=uloha.zaciatok).order_by('zaciatok').first()

        lokalny_zaciatok = timezone.localtime(uloha.zaciatok) if timezone.is_aware(uloha.zaciatok) else uloha.zaciatok
        logistika_data_list = [f"Nová úloha začína o {lokalny_zaciatok.strftime('%H:%M')} v meste {uloha.zakaznik.mesto if uloha.zakaznik else 'Neznáme'}."]
        if predchadzajuca_uloha and predchadzajuca_uloha.zakaznik and predchadzajuca_uloha.koniec:
            k_dt = timezone.localtime(predchadzajuca_uloha.koniec) if timezone.is_aware(predchadzajuca_uloha.koniec) else predchadzajuca_uloha.koniec
            mesto_pred = predchadzajuca_uloha.zakaznik.mesto or 'Neznáme'
            logistika_data_list.append(f"Predchádzajúca úloha končí o {k_dt.strftime('%H:%M')} v meste {mesto_pred}.")
        if nasledujuca_uloha and nasledujuca_uloha.zakaznik and nasledujuca_uloha.zaciatok:
            z_dt = timezone.localtime(nasledujuca_uloha.zaciatok) if timezone.is_aware(nasledujuca_uloha.zaciatok) else nasledujuca_uloha.zaciatok
            mesto_nasl = nasledujuca_uloha.zakaznik.mesto or 'Neznáme'
            logistika_data_list.append(f"Nasledujúca úloha začína o {z_dt.strftime('%H:%M')} v meste {mesto_nasl}.")
            
        logistika_data = " ".join(logistika_data_list)

        nazov_firmy = "ISAS CORE Dispatch"
        try:
            if apps.ready:
                FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
                fi = FiremnaIdentitaModel.objects.first() if FiremnaIdentitaModel else None
                if fi and fi.nazov_spolocnosti:
                    nazov_firmy = fi.nazov_spolocnosti.strip()
        except Exception as err:
            logger.warning("task_ai_audit_ulohy: Nepodarilo sa načítať identitu firmy: %s", err)

        prompt = f"""
        Si elitný AI Dispatch Auditor riadenia servisných a technických operácií pre spoločnosť {nazov_firmy}. Tvojou úlohou je skontrolovať, či dispečer neurobil logickú, kvalifikačnú alebo bezpečnostnú chybu pri plánovaní výjazdu.
        Dnešný dátum: {dnes.strftime('%Y-%m-%d')} (formát YYYY-MM-DD)
        
        DÁTA ÚLOHY:
        - Zákazník: {uloha.zakaznik.Nazov_spolocnosti if uloha.zakaznik else 'Neznámy'}
        - Typy plánovaných prác: {', '.join(typy_prac) if typy_prac else 'Bežný servis / Nešpecifikované'}
        - Technici a ich spôsobilosť: {json.dumps(technici_data, ensure_ascii=False)}
        - Logistika a časový harmonogram: {logistika_data}

        ODVETVOVÉ PREDPISY, CERTIFIKÁCIE A COMPLIANCE ŠTANDARDY:
        {odvetvova_operativa if odvetvova_operativa else 'Aplikujú sa všeobecné BOZP predpisy a odborná spôsobilosť na vykonávanie technických prác.'}
        
        PRAVIDLÁ KONTROLY (Vyhodnoť všetky a ak nájdeš viac chýb, zhrň ich do jedného popisu):
        1. BOZP (Kritické): Porovnaj dátum 'bozp_platne_do' každého technika s dnešným dátumom. Ak je dátum BOZP menší ako dnešný (alebo je 'Nezadané'), technik nesmie byť zaradený na zásah.
        2. Odborná spôsobilosť a certifikácie (Kritické): Skontroluj, či plánované typy prác vyžadujú osobitnú spôsobilosť podľa zadaných odvetvových štandardov a predpisov. Aspoň jeden priradený technik musí mať v položke 'platne_certifikaty_a_sposobilosti' zodpovedajúce platné osvedčenie.
        3. Logistika (Varovanie): Zhodnoť reálnosť prejazdu medzi mestami na základe bežných vzdialeností a časového okna. Ak je čas na presun zjavne nereálny, upozorni na to.
        
        POŽIADAVKA NA VÝSTUP:
        Vráť VÝHRADNE validný JSON bez akéhokoľvek iného textu.
        Štruktúra JSONu musí byť presne takáto (hodnota 'stupen' môže byť len "CRITICAL", "WARNING" alebo null):
        {{
            "najdena_chyba": true,
            "stupen": "CRITICAL",
            "popis_chyby": "Stručné, profesionálne a úderné vysvetlenie pre dispečera."
        }}
        
        Ak nenájdeš absolútne žiadnu chybu, vráť presne tento JSON:
        {{
            "najdena_chyba": false,
            "stupen": null,
            "popis_chyby": null
        }}
        """

        try:
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model=gemini_model,
                contents=[prompt],
                config=types.GenerateContentConfig(thinking_config=types.ThinkingConfig(thinking_level=getattr(settings, 'GEMINI_THINKING_LEVEL', 'MEDIUM') or 'MEDIUM'), response_mime_type="application/json")
            )
            
            # 🛡️ Ochrana pred zablokovaním odpovede (Safety Filters)
            if not response.text:
                return "AI nevrátila žiadny text (možný blok bezpečnostným filtrom)."
                
            ai_data_raw = clean_and_parse_ai_json(response.text)
            ai_data = ai_data_raw[0] if (isinstance(ai_data_raw, list) and ai_data_raw and isinstance(ai_data_raw[0], dict)) else (ai_data_raw if isinstance(ai_data_raw, dict) else {})
            
        except Exception as e:
            logger.error(f"🚨 AI Dispatch Auditor zlyhal pre úlohu #{uloha_id}: {e}")
            return "Chyba pri komunikácii s AI API alebo pri parsovaní JSONu."
        
        if ai_data.get('najdena_chyba') and ai_data.get('popis_chyby'):
            # 1. Uloženie do AI Asistenta (aby to dispečer videl v chate)
            AIAuditLog.objects.create(
                zdroj_poziadavky='SYSTEM',
                otazka=f"Automatický audit úlohy #{uloha.id}",
                odpoved=f"🚨 UPOZORNENIE DISPEČINGU:\n{ai_data['popis_chyby']}",
                uspesne_dokoncene=True
            )
            
            # 2. Odoslanie notifikácie dispečerovi
            ikona = "🚨" if ai_data.get('stupen') == 'CRITICAL' else "⚠️"
            farba = "#ef4444" if ai_data.get('stupen') == 'CRITICAL' else "#f59e0b"
            
            base_url = (getattr(gn, 'clean_base_url', None) or getattr(settings, 'SITE_URL', '')).strip().rstrip('/')
            admin_url = f"{base_url}/admin/Evidencia/uloha/{uloha.id}/change/"
            
            payload = (ISASNotificationBuilder(nadpis='AI DISPATCH AUDITOR', ikona=ikona, farba_temy=farba)
                       .pridaj_riadok('Úloha:', f"#{uloha.id} ({uloha.zakaznik.Nazov_spolocnosti if uloha.zakaznik else 'Neznámy'})")
                       .nastav_hlavny_text(f"<b>Detegované riziko pri plánovaní:</b><br>{html.escape(str(ai_data['popis_chyby']))}")
                       .nastav_tlacidlo('Otvoriť a opraviť úlohu', admin_url)
                       .nastav_ai_poznamku('Toto je automatické varovanie systému. Ak ide o vedomú výnimku, môžete ho ignorovať.')
                       .build())
                       
            ISASNotifier.send(
                payload=payload,
                send_tg=True,
                send_mail=True,
                send_discord=True
            )
            return f"Nájdená chyba: {ai_data['popis_chyby']}"
            
        return "Úloha je bez logických chýb."

@shared_task(base=ISASAutoHealTask)
def task_notifikuj_kandidata_o_stave(kandidat_id, stary_stav, novy_stav):
    """
    🎓 ISAS ATS ENGINE: Automatické e-mailové notifikácie kandidátom cez čistú Django šablónu.
    Plne zosúladené s isas_task_execution_guard, render_to_string a SMTP spojením.
    """
    if stary_stav == novy_stav:
        return "Stav sa nezmenil, notifikácia preskočená."

    farby_stavu = {
        'POHOVOR': '#2563eb',
        'PRIJATY': '#10b981',
        'ZAMIETNUTY': '#64748b',
        'KONTAKTOVANY': '#b45309',
    }
    
    if novy_stav not in farby_stavu:
        return f"Pre stav '{novy_stav}' nie je definovaná notifikácia."

    lock_id = f"lock_notif_kandidat_{kandidat_id}_{novy_stav}"

    with isas_task_execution_guard(lock_id, timeout=120) as (acquired, gn, identita):
        if not acquired:
            logger.warning(f"⏭️ [task_notifikuj_kandidata_o_stave] Notifikácia pre kandidáta ID {kandidat_id} (stav: {novy_stav}) už prebieha. Preskakujem duplicitu.")
            return "Zámok aktívny."

        if not gn or not gn.smtp_user or not getattr(gn, 'smtp_host', None):
            logger.warning("⚠️ [task_notifikuj_kandidata_o_stave] Chýba konfigurácia SMTP v Globálnych nastaveniach.")
            return "Chýba SMTP konfigurácia."

        kandidat = Kandidat.objects.select_related('pozicia').filter(id=kandidat_id).first()
        if not kandidat or not kandidat.email or not kandidat.email.strip():
            logger.warning(f"⚠️ [task_notifikuj_kandidata_o_stave] Kandidát ID {kandidat_id} neexistuje alebo nemá vyplnený e-mail.")
            return "Kandidát neexistuje alebo chýba e-mail."

        _, cisty_email = parseaddr(str(kandidat.email).strip())
        cisty_email = cisty_email or str(kandidat.email).strip()
        try:
            validate_email(cisty_email)
        except ValidationError:
            logger.warning(f"⚠️ [task_notifikuj_kandidata_o_stave] Neplatná e-mailová adresa '{cisty_email}' kandidáta ID {kandidat_id}.")
            return "Neplatný e-mail."

        raw_firma = identita.nazov_spolocnosti if identita and identita.nazov_spolocnosti else "Naša spoločnosť"
        nasa_firma = " ".join(str(raw_firma).split())
        raw_pozicia = kandidat.pozicia.nazov if (kandidat.pozicia and kandidat.pozicia.nazov) else None
        pozicia_nazov = " ".join(str(raw_pozicia).split()) if (raw_pozicia and str(raw_pozicia).strip()) else ""

        raw_base = getattr(gn, 'clean_base_url', None) or getattr(settings, 'SITE_URL', '')
        base_str = str(raw_base).strip().rstrip('/')
        base_url = base_str if base_str.startswith(('http://', 'https://')) else f"https://{base_str}"

        logo_url = None
        if identita and getattr(identita, 'logo', None) and identita.logo.name:
            try:
                raw_logo_url = identita.logo.url
                if raw_logo_url and not raw_logo_url.split('?')[0].lower().endswith('.svg'):
                    if raw_logo_url.startswith(('http://', 'https://')):
                        logo_url = raw_logo_url
                    elif raw_logo_url.startswith('//'):
                        logo_url = f"https:{raw_logo_url}"
                    else:
                        logo_url = f"{base_url}/{raw_logo_url.lstrip('/')}"
            except Exception as logo_err:
                logger.warning(f"⚠️ [task_notifikuj_kandidata_o_stave] Nepodarilo sa získať URL loga: {logo_err}")

        podnadpisy = {
            'POHOVOR': 'Pozvánka na osobný pohovor',
            'PRIJATY': 'Prijatie do tímu',
            'ZAMIETNUTY': 'Vyjadrenie k výberovému konaniu',
            'KONTAKTOVANY': 'Aktualizácia výberového konania',
        }

        popis_pozicie = f" – {pozicia_nazov}" if pozicia_nazov else ""
        predmety = {
            'POHOVOR': f"🤝 Pozvánka na osobný pohovor{popis_pozicie} | {nasa_firma}",
            'PRIJATY': f"🎉 Prijatie do tímu{popis_pozicie} | {nasa_firma}",
            'ZAMIETNUTY': f"Vyjadrenie k výberovému konaniu{popis_pozicie} | {nasa_firma}",
            'KONTAKTOVANY': f"Aktualizácia výberového konania{popis_pozicie} | {nasa_firma}",
        }

        safe_web_url = None
        if identita and identita.web and identita.web.strip():
            raw_web = identita.web.strip()
            safe_web_url = raw_web if raw_web.startswith(('http://', 'https://')) else f"https://{raw_web}"

        context = {
            'farba_brandu': farby_stavu[novy_stav],
            'nazov_nasej_firmy': nasa_firma,
            'podnadpis_hlavicky': podnadpisy.get(novy_stav, 'Informácia o výberovom konaní'),
            'logo_url': logo_url,
            'meno_kandidata': kandidat.meno.strip() if (kandidat.meno and kandidat.meno.strip()) else '',
            'pozicia_nazov': pozicia_nazov,
            'stav': novy_stav,
            'web_url': safe_web_url,
        }

        html_text = render_to_string('emails/kandidat_status_notifikacia.html', context)
        # 1. Orezanie <head> zamedzí presakovaniu a zdvojovaniu textu z <title> do plain-textu
        text_bez_head = re.sub(r'<head\b[^>]*>.*?</head>', '', html_text, flags=re.DOTALL | re.IGNORECASE)
        # 2. Odstránenie skrytého preheaderu odolné voči variáciám medzier v CSS
        text_faza1 = re.sub(r'<div\b[^>]*style=["\'][^"\']*display:\s*none[^"\']*["\'][^>]*>.*?</div>', '', text_bez_head, flags=re.DOTALL | re.IGNORECASE)

        def _extrahuj_odkaz(match):
            url = match.group(1).strip()
            if not url or url.startswith('#'):
                return match.group(2)
            # Normalizácia bielych znakov; pri grafických odkazoch (logo bez textu) nevkladáme holú URL
            text_linku = " ".join(strip_tags(match.group(2)).split())
            return f"{text_linku} ({url})" if text_linku else ""

        text_faza1 = re.sub(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', _extrahuj_odkaz, text_faza1, flags=re.IGNORECASE | re.DOTALL)
        html_s_odriadkovanim = text_faza1.replace('</p>', '\n\n</p>').replace('</h3>', '\n\n</h3>').replace('</h2>', '\n\n</h2>').replace('<br>', '\n').replace('<br/>', '\n').replace('<br />', '\n')
        ciste_riadky = [html.unescape(line.strip()) for line in strip_tags(html_s_odriadkovanim).splitlines() if line.strip()]
        plain_text = "\n\n".join(ciste_riadky)

        raw_predmet = predmety.get(novy_stav, f"Výberové konanie – {nasa_firma}")
        cisty_predmet = " ".join(str(raw_predmet).split())

        kandidati_na_odosielatela = [
            getattr(identita, 'email_vseobecny', None),
            getattr(settings, 'DEFAULT_FROM_EMAIL', None),
            getattr(gn, 'smtp_user', None),
        ]

        odosielaci_email = None
        for raw_kandidat in kandidati_na_odosielatela:
            if not raw_kandidat:
                continue
            _, extrahovana_adresa = parseaddr(str(raw_kandidat).strip())
            if extrahovana_adresa:
                try:
                    validate_email(extrahovana_adresa)
                    odosielaci_email = extrahovana_adresa
                    break
                except ValidationError:
                    continue

        if not odosielaci_email:
            logger.error(f"❌ [task_notifikuj_kandidata_o_stave] Konfigurácia zlyhala: Nie je k dispozícii žiadna platná odosielacia e-mailová adresa pre kandidáta ID {kandidat_id}.")
            return "Chýba platná odosielacia adresa."

        odosielatel_formatted = formataddr((nasa_firma, odosielaci_email))

        reply_to_email = odosielaci_email
        raw_reply = getattr(identita, 'email_vseobecny', None)
        if raw_reply:
            _, extrahovany_reply = parseaddr(str(raw_reply).strip())
            if extrahovany_reply:
                try:
                    validate_email(extrahovany_reply)
                    reply_to_email = extrahovany_reply
                except ValidationError:
                    pass

        email_msg = EmailMultiAlternatives(
            subject=cisty_predmet,
            body=plain_text,
            from_email=odosielatel_formatted,
            to=[cisty_email],
            reply_to=[reply_to_email],
            using='default'
        )
        email_msg.attach_alternative(html_text, "text/html")

        try:
            email_msg.send()
        except Exception as err:
            logger.error(f"❌ [task_notifikuj_kandidata_o_stave] SMTP zlyhanie pri odosielaní kandidátovi ID {kandidat_id}: {err}", exc_info=True)
            raise

        logger.info(f"✅ [task_notifikuj_kandidata_o_stave] E-mail o zmene stavu na '{novy_stav}' úspešne odoslaný kandidátovi {kandidat.meno} ({cisty_email}).")
        return f"Notifikácia '{novy_stav}' úspešne odoslaná na {cisty_email}."

@shared_task(base=ISASAutoHealTask)
def skontrolovat_zamrznute_exporty_task():
    """
    🛡️ SELF-HEALING MECHANIZMUS (OOM Kill Ochrana):
    Nájde exporty, ktoré sú v stave 'Generuje sa' dlhšie ako 2 hodiny.
    Ak také existujú, znamená to, že worker bol natvrdo zabitý operačným systémom.
    """
    # Hľadáme všetko, čo je staršie ako 2 hodiny
    limit_casu = timezone.now() - timedelta(hours=2)
    
    zamrznute_zalohy = MegaZaloha.objects.filter(
        stav_spracovania='spracovava_sa',
        datum_vytvorenia__lt=limit_casu
    )
    
    pocet = zamrznute_zalohy.count()
    if pocet > 0:
        for zaloha in zamrznute_zalohy:
            aktualna_poznamka = zaloha.poznamka or ""
            zaloha.poznamka = aktualna_poznamka + "\n🚨 [SYSTÉM] Export bol prerušený z dôvodu preťaženia servera (Nedostatok RAM / Timeout)."
            zaloha.stav_spracovania = 'chyba'
            zaloha.save(update_fields=['stav_spracovania', 'poznamka'])
            
        logger.warning(f"🧹 [ISAS DATA NEXUS] Upratané {pocet} zamrznutých exportov.")