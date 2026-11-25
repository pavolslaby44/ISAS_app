import zoneinfo
import logging
import time
import email
import socket
import re
import os
import glob
import subprocess
import tarfile
import requests
import html

from django.template.loader import render_to_string
from django.utils.html import strip_tags
from email.utils import parsedate_to_datetime, parseaddr
from django.utils import timezone
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import close_old_connections, transaction
from celery import shared_task
from google import genai
from google.genai import types
from ISASaplikacia.utils import get_gemini_api_key
from ISASaplikacia.utils_helper import ISASAutoHealTask, isas_task_execution_guard
from datetime import datetime
from django.core.files.storage import default_storage

from Evidencia.utils_helper import clean_and_parse_ai_json, generate_dynamic_greeting
from Evidencia.utils import ISASNotifier, ISASNotificationBuilder
from Evidencia.industry_loader import ZARIADENIA_CHOICES
from ISASaplikacia.models import EmailovyUcet, EmailovaSprava, EmailPriloha, FiremnaIdentita, GlobalneNastavenia
from Evidencia.models import Zakaznik, Dodavatel, Zamestnanec, SukromnaOsoba, TypServisuPrace, ServisovaneZariadenie, PrichodziaFaktura
from ISASaplikacia.isas_imap_engine import (
    connect_imap, clean_header, extract_email_content, mark_imap_read, 
    trash_imap_email, send_email_smtp, save_draft_imap, delete_draft_imap, 
    empty_trash_imap, bulk_trash_imap, restore_imap, delete_permanently_imap, 
    bulk_restore_imap, bulk_delete_permanently_imap
)

logger = logging.getLogger(__name__)

@shared_task(base=ISASAutoHealTask)
def sync_isas_mail_task():
    # 🔒 Pridaný Mutex zámok (Zabráni duplicite e-mailov pri súbehu)
    with isas_task_execution_guard('lock_sync_isas_mail', timeout=600) as (acquired, gn, identita):
        if not acquired:
            logger.warning("⏭️ [ISAS NEXUS] Synchronizácia e-mailov už prebieha. Preskakujem.")
            return

        close_old_connections()
        logger.warning("📡 [ISAS NEXUS] Pripájam sa na IMAP servery...")
        
        # 🚀 OPTIMALIZÁCIA: 'identita' už máme z isas_task_execution_guard, neťaháme ju znova z DB!
        nazov_firmy = identita.nazov_spolocnosti if identita and identita.nazov_spolocnosti else "Naša spoločnosť"
        
        # 🧠 DYNAMICKÁ DOMÉNOVÁ EXPERTÍZA (Ohraničená na max. 40 položiek pre úsporu tokenov a rýchlosť AI)
        typy_prac = list(TypServisuPrace.objects.values_list('nazov', flat=True)[:40])
        typy_prac_str = ", ".join(typy_prac) if typy_prac else "servis a údržba"
    
        zariadenia_kategorie = ServisovaneZariadenie.objects.values_list('kategoria', flat=True).distinct()[:40]
        kategorie_dict = dict(getattr(ServisovaneZariadenie, 'KATEGORIE', None) or ZARIADENIA_CHOICES)
        zariadenia_nazvy = []
        for k in zariadenia_kategorie:
            if k is not None:
                nazov = kategorie_dict.get(k, k)
                cisty_nazov = re.sub(r'[^\w\s,/()-]', '', str(nazov or '')).strip()
                if cisty_nazov:
                    zariadenia_nazvy.append(cisty_nazov)
        
        zariadenia_str = ", ".join(zariadenia_nazvy) if zariadenia_nazvy else "technické zariadenia"
        domenova_expertiza = f"Firma vykonáva najmä: {typy_prac_str}. Servisuje zariadenia ako: {zariadenia_str}."
    
        rozsirena_expertiza = (
            f"{domenova_expertiza} "
            f"Okrem servisu firma spravuje skladové zásoby, "
            f"vystavuje cenové ponuky a rieši požiadavky klientov."
        )
    
        ucty = EmailovyUcet.objects.filter(je_aktivny=True)
        if not ucty:
            logger.warning("⚠️ Nie je nastavený žiadny aktívny e-mailový účet.")
            return

        # 🛡️ DEFENSIVE CHECK: Overenie konfigurácie Gemini AI pred inicializáciou (DB/Cache -> Fallback .env)
        client = None
        gemini_api_key = get_gemini_api_key()
        gemini_model = getattr(settings, 'GEMINI_MODEL', None)

        if not gemini_api_key or not gemini_model:
            logger.warning("⚠️ [ISAS AI] Gemini API kľúč alebo GEMINI_MODEL nie je dostupný. E-maily sa stiahnu bez AI analýzy.")
        else:
            try:
                client = genai.Client(api_key=gemini_api_key)
            except Exception as e:
                logger.warning(f"⚠️ [ISAS AI] Nepodarilo sa inicializovať Gemini Client ({e}). AI analýza bude preskočená.")

        for ucet in ucty:
            imap = None
            try:
                imap = connect_imap(ucet)
                if not imap:
                    logger.warning(f"⚠️ Nepodarilo sa nadviazať IMAP spojenie pre účet {ucet.email_adresa}.")
                    continue
            
                # 🚀 ELITNÁ ARCHITEKTÚRA: Mapovanie IMAP zložiek na ISAS zložky (dynamická detekcia jedinej zložky SENT)
                zlozky_na_sync = [('INBOX', 'INBOX')]
                sent_candidates = ['"[Gmail]/Odoslan&AOk-"', '"[Gmail]/Sent Mail"', 'Sent', 'Odoslane', 'INBOX.Sent']
                for sf in sent_candidates:
                    st_sent, _ = imap.select(sf)
                    if st_sent == 'OK':
                        zlozky_na_sync.append((sf, 'SENT'))
                        break
            
                for imap_folder, db_folder in zlozky_na_sync:
                    status, _ = imap.select(imap_folder)
                    if status != 'OK':
                        continue
                
                    status, uid_data = imap.uid('SEARCH', None, 'ALL')
                    if status != 'OK' or not uid_data or not uid_data[0]:
                        continue

                    server_uids = uid_data[0].decode('ascii', errors='ignore').split() if uid_data[0] else []
                    db_uids = set(EmailovaSprava.objects.filter(ucet=ucet, zlozka=db_folder).values_list('message_id', flat=True))
                    chybajuce_uids = [uid for uid in server_uids if uid not in db_uids]
                
                    if not chybajuce_uids:
                        logger.info(f"✓ Účet {ucet.email_adresa} | Zložka {db_folder} je plne synchronizovaná.")
                        continue
                
                    logger.info(f"📥 Našlo sa {len(chybajuce_uids)} chýbajúcich správ v zložke {db_folder}. Sťahujem dávku...")

                    for uid in chybajuce_uids[-50:]:
                        try:
                            status, msg_data = imap.uid('FETCH', uid, '(RFC822)')
                            if status != 'OK' or not msg_data:
                                continue
                        
                            raw_email = None
                            for response_part in msg_data:
                                if isinstance(response_part, tuple):
                                    raw_email = response_part[1]
                                    break
                                
                            if not raw_email:
                                logger.warning(f"⚠️ Nepodarilo sa extrahovať raw dáta pre UID {uid}")
                                continue
                            
                            msg = email.message_from_bytes(raw_email)

                            # 🛡️ DEFENSIVE SLICING: Ochrana pred TypeError v prípade, že clean_header vráti None
                            subject = (clean_header(msg.get('Subject')) or "Bez predmetu")[:490]
                            sender = (clean_header(msg.get('From')) or "")[:250]
                            to_addr = clean_header(msg.get('To', '')) or ""
                        
                            try:
                                raw_date = parsedate_to_datetime(msg.get('Date'))
                                # 🛡️ Konverzia prichádzajúceho e-mailového času na naivný lokálny čas podľa konfigurácie pre USE_TZ=False
                                if raw_date:
                                    lokalna_tz = zoneinfo.ZoneInfo(getattr(settings, 'TIME_ZONE', 'UTC'))
                                    if raw_date.tzinfo:
                                        date = raw_date.astimezone(lokalna_tz).replace(tzinfo=None)
                                    else:
                                        date = raw_date
                                else:
                                    date = timezone.now()
                            except Exception:
                                date = timezone.now()

                            body_text, body_html, attachments = extract_email_content(msg)

                            bezpecny_sender = sender or ""
                            parsed_email = parseaddr(bezpecny_sender)[1].strip()
                            email_from = parsed_email if parsed_email else bezpecny_sender.split('<')[-1].replace('>', '').strip()
                        
                            # 1. Primárne párovanie podľa e-mailovej adresy
                            zakaznik, dodavatel, zamestnanec, sukromna_osoba = None, None, None, None
                            if email_from:
                                zakaznik = Zakaznik.objects.filter(email__iexact=email_from).first()
                                dodavatel = Dodavatel.objects.filter(email__iexact=email_from).first()
                                zamestnanec = Zamestnanec.objects.filter(email__iexact=email_from).first()
                                sukromna_osoba = SukromnaOsoba.objects.filter(email__iexact=email_from).first()

                            telo = body_text or body_html
                            ai_zhrnutie, ai_kat = "", "Nezaradené"
                        
                            if client: 
                                ciste_telo = (telo or "").replace('<br>', '\n').replace('<p>', '\n')[:3000]
                            
                                crm_kontext = "Neznámy odosielateľ (Potenciálny nový klient, partner alebo spam)"
                                if zakaznik:
                                    crm_kontext = f"Náš existujúci zákazník: {zakaznik.Nazov_spolocnosti}"
                                elif dodavatel:
                                    crm_kontext = f"Náš dodávateľ: {dodavatel.Nazov_spolocnosti}"
                                elif zamestnanec:
                                    rola = zamestnanec.get_pracovna_rola_display() if hasattr(zamestnanec, 'get_pracovna_rola_display') else "Zamestnanec"
                                    crm_kontext = f"Náš interný zamestnanec: {zamestnanec.meno} {zamestnanec.priezvisko} ({rola})"
                                elif sukromna_osoba:
                                    crm_kontext = f"Súkromná osoba / Fyzická osoba: {sukromna_osoba.meno_a_priezvisko}"

                                system_instruction = f"""
                                Si elitný AI asistent a systémový analytik v ERP systéme spoločnosti {nazov_firmy}.
                                ZAMERANIE FIRMY: {rozsirena_expertiza}
                                Tvojou úlohou je precízne analyzovať prichádzajúci e-mail, extrahovať podstatu a striktne ho zaradiť do jednej z povolených kategórií.
                                Zároveň extrahuj kľúčové identifikátory pre párovanie v databáze.
                            
                                🧠 KONTEXT ODOSIELATEĽA Z NAŠEJ DATABÁZY: {crm_kontext}
                            
                                🛡️ ANTI-HALUCINAČNÉ PRAVIDLÁ:
                                1. ZAKÁZANÉ vymýšľať si údaje. Extrahuj IČO, názov firmy alebo model zariadenia IBA ak sú explicitne uvedené v texte.
                                2. Ak údaj v texte nie je, vráť null.
                                3. Neodpovedaj na e-mail, iba ho analyzuj.

                                POVOLENÉ KATEGÓRIE (vyber presne jednu):
                                "Dopyt / Objednávka", "Servis / Porucha", "Účtovníctvo / Faktúry", "HR / Interné", "Reklama / Spam", "Všeobecné / Iné"

                                PRAVIDLÁ PRE ZHRNUTIE:
                                Napíš maximálne 2 až 3 vety. Zameraj sa výhradne na fakty.

                                VÝSTUP:
                                Vráť ABSOLÚTNE ČISTÝ JSON formát bez Markdown tagov:
                                {
                                    "kategoria": "...",
                                    "zhrnutie": "...",
                                    "extrahovane_ico": null,
                                    "extrahovany_nazov_firmy": null,
                                    "extrahovany_model_zariadenia": null
                                }
                                """
                                try:
                                    res = client.models.generate_content(
                                        model=gemini_model,
                                        contents=[system_instruction, f"TEXT E-MAILU:\n{ciste_telo}"],
                                        config=types.GenerateContentConfig(response_mime_type="application/json", thinking_config=types.ThinkingConfig(thinking_level=getattr(settings, 'GEMINI_THINKING_LEVEL', 'MEDIUM') or 'MEDIUM'))
                                    )
                                    ai_data = clean_and_parse_ai_json(res.text)
                                    if not isinstance(ai_data, dict):
                                        ai_data = {}
                                
                                    ai_kat = str(ai_data.get("kategoria") or "Nezaradené")[:45]
                                    ai_zhrnutie = str(ai_data.get("zhrnutie") or "")
                                
                                    # 🚀 2. Sekundárne párovanie (RAG Fallback s medzinárodnou validáciou IČO / reg. čísla)
                                    ext_ico_raw = str(ai_data.get("extrahovane_ico") or '').strip()
                                    ciste_ico = re.sub(r'[^\w-]', '', ext_ico_raw)
                                    ext_ico = ciste_ico if (ciste_ico and len(ciste_ico) >= 3 and ciste_ico.lower() not in {'none', 'null', 'n/a'}) else None

                                    ext_nazov_raw = str(ai_data.get("extrahovany_nazov_firmy") or '').strip()
                                    zakazane_nazvy = {'s.r.o.', 's. r. o.', 'spol. s r.o.', 'a.s.', 'a. s.', 'sro', 'as', 'ltd', 'gmbh', 'n/a', 'none', 'null', 'firma', 'spolocnost'}
                                    ext_nazov = ext_nazov_raw if (len(ext_nazov_raw) >= 3 and ext_nazov_raw.lower() not in zakazane_nazvy) else None
                                
                                    if not zakaznik:
                                        if ext_ico:
                                            zakaznik = Zakaznik.objects.filter(ico=ext_ico).first()
                                        if not zakaznik and ext_nazov:
                                            zakaznik = Zakaznik.objects.filter(Nazov_spolocnosti__icontains=ext_nazov).first()
                                        
                                    if not dodavatel:
                                        if ext_ico:
                                            dodavatel = Dodavatel.objects.filter(ico=ext_ico).first()
                                        if not dodavatel and ext_nazov:
                                            dodavatel = Dodavatel.objects.filter(Nazov_spolocnosti__icontains=ext_nazov).first()

                                    time.sleep(1)
                                except Exception as e: 
                                    logger.warning(f"⚠️ AI Analýza zlyhala pre '{subject}': {e}")

                            with transaction.atomic():
                                novy_mail, created = EmailovaSprava.objects.get_or_create(
                                    ucet=ucet, message_id=uid,
                                    defaults={
                                        'odosielatel': sender, 'prijemca': to_addr, 'predmet': subject,
                                        'datum_prijatia': date, 'telo_text': body_text, 'telo_html': body_html,
                                        'ai_zhrnutie': ai_zhrnutie, 'ai_kategoria': ai_kat,
                                        'zakaznik': zakaznik, 'dodavatel': dodavatel, 'zamestnanec': zamestnanec,
                                        'zlozka': db_folder,
                                        'precitane': True if db_folder == 'SENT' else False
                                    }
                                )

                                if created:
                                    for idx, att in enumerate(attachments):
                                        if att.get('data'):
                                            raw_nazov = att.get('filename') or f"priloha_{novy_mail.id}_{idx}_{int(time.time())}"
                                            # 🛡️ DB DEFENSE & PATH TRAVERSAL: Odstránenie adresárových ciest, NUL bajtov a riadiacich znakov
                                            cisty_nazov = os.path.basename(str(raw_nazov).replace('\\', '/'))
                                            subor_nazov = re.sub(r'[\x00\r\n]', '', cisty_nazov).strip() or f"priloha_{novy_mail.id}_{idx}"
                                            priloha = EmailPriloha(
                                                sprava=novy_mail,
                                                nazov_suboru=subor_nazov[:250],
                                                mime_type=att.get('mime') or 'application/octet-stream'
                                            )
                                            priloha.subor.save(subor_nazov, ContentFile(att['data']))

                            logger.info(f"✨ Spracované: {subject} | Kat: {ai_kat}")

                        except Exception as e:
                            logger.error(f"❌ Chyba pri spracovaní e-mailu (UID: {uid}): {e}")                     
 
                # --- 🔄 OBOJSMERNÁ SYNCHRONIZÁCIA (Zrkadlenie Koša) ---
                try:
                    for imap_folder, db_folder in zlozky_na_sync:
                        status, _ = imap.select(imap_folder)
                        if status == 'OK':
                            status, uid_data = imap.uid('SEARCH', None, 'ALL')
                            # 🛡️ DEFENSIVE: Ak SEARCH zlyhal, nesmieme synchronizovať zmazanie (ochrana pred vyprázdnením DB)
                            if status != 'OK' or not uid_data:
                                logger.warning(f"⚠️ IMAP SEARCH zlyhal pre {imap_folder}. Preskakujem zrkadlenie zmazania.")
                                continue

                            server_uids = set(uid_data[0].decode('ascii', errors='ignore').split()) if uid_data[0] else set()
                        
                            # 🛡️ OOM OCHRANA: Načítavame len potrebné stĺpce (vynechá ťažké telo_text a telo_html)
                            db_maily = EmailovaSprava.objects.filter(ucet=ucet, zlozka=db_folder).only('id', 'message_id', 'predmet')
                            for db_mail in db_maily:
                                if db_mail.message_id and db_mail.message_id.isdigit() and db_mail.message_id not in server_uids:
                                    zmenene = EmailovaSprava.objects.filter(
                                        id=db_mail.id, zlozka=db_folder
                                    ).update(zlozka='TRASH')
                                
                                    if zmenene > 0:
                                        logger.info(f"🗑️ Sync: E-mail '{db_mail.predmet}' zmizol z {imap_folder}. Presúvam do Koša.")

                    trash_folders = ['"[Gmail]/K&APQ-&AWE-"', '"[Gmail]/Trash"', 'Trash', 'Deleted', 'INBOX.Trash']
                    trash_selected = False
                    for tf in trash_folders:
                        status, _ = imap.select(tf)
                        if status == 'OK':
                            trash_selected = True
                            break
                
                    if trash_selected:
                        status, uid_data = imap.uid('SEARCH', None, 'ALL')
                        if status == 'OK' and uid_data:
                            raw_trash_uids = uid_data[0].decode('ascii', errors='ignore').split() if uid_data[0] else []
                            db_trash_maily = EmailovaSprava.objects.filter(ucet=ucet, zlozka='TRASH').only('id', 'message_id', 'predmet')
                            
                            # 🎯 SCENÁR 1: Celý kôš na Gmaile bol vysypaný (0 správ na serveri)
                            if not raw_trash_uids:
                                if db_trash_maily.exists():
                                    pocet_zmazanych, _ = db_trash_maily.delete()
                                    if pocet_zmazanych > 0:
                                        logger.info(f"🔥 Sync: Kôš na Gmaile bol kompletne vysypaný. Prečistených {pocet_zmazanych} správ v ISAS CORE.")
                            
                            # 🎯 SCENÁR 2: Čiastočné trvalé mazanie z Koša (Zrkadlenie jednotlivých zmazaní)
                            elif db_trash_maily.exists():
                                # Bleskový batch fetch hlavičiek v Koši (stiahne len predmety za 50ms, žiadne telá správ)
                                status_fetch, fetch_data = imap.fetch('1:*', '(BODY.PEEK[HEADER.FIELDS (SUBJECT)])')
                                if status_fetch == 'OK' and fetch_data:
                                    server_trash_predmety = set()
                                    for item in fetch_data:
                                        if isinstance(item, tuple) and len(item) > 1:
                                            hdr_msg = email.message_from_bytes(item[1])
                                            clean_subj = clean_header(hdr_msg.get('Subject', '')).strip().lower()
                                            if clean_subj:
                                                server_trash_predmety.add(clean_subj)

                                    for db_mail in db_trash_maily:
                                        subj_lokal = (db_mail.predmet or "Bez predmetu").strip().lower()
                                        # Ak predmet správy už v Koši na Gmaile preukázateľne nie je, bola trvalo zmazaná
                                        if server_trash_predmety and subj_lokal not in server_trash_predmety:
                                            EmailovaSprava.objects.filter(id=db_mail.id, zlozka='TRASH').delete()
                                            logger.info(f"🔥 Sync: E-mail '{db_mail.predmet}' bol trvalo odstránený z Koša na Gmaile. Mažem z ISAS CORE.")
                            
                except Exception as e:
                    logger.error(f"❌ Chyba pri obojsmernej synchronizácii Koša: {e}")

            except Exception as e:
                logger.error(f"❌ Chyba spojenia pre účet {ucet.email_adresa}: {e}")
            finally:
                # 🛡️ LEAK PROTECTION: Spojenie sa korektne uzavrie aj pri páde spracovania e-mailu
                if imap:
                    try:
                        imap.logout()
                    except Exception:
                        pass

@shared_task(base=ISASAutoHealTask, bind=True, max_retries=3, default_retry_delay=10)
def mark_imap_read_task(self, ucet_id, message_id):
    try:
        ucet = EmailovyUcet.objects.get(id=ucet_id)
        mark_imap_read(ucet, message_id)
    except EmailovyUcet.DoesNotExist:
        logger.error(f"Účet ID {ucet_id} neexistuje.")
    except Exception as e:
        logger.warning(f"Sieťová chyba pri mark_imap_read_task: {e}. Skúšam znova...")
        raise self.retry(exc=e)

@shared_task(base=ISASAutoHealTask, bind=True, max_retries=3, default_retry_delay=10)
def trash_imap_email_task(self, ucet_id, message_id):
    try:
        ucet = EmailovyUcet.objects.get(id=ucet_id)
        trash_imap_email(ucet, message_id)
    except EmailovyUcet.DoesNotExist:
        logger.error(f"Účet ID {ucet_id} neexistuje.")
    except Exception as e:
        logger.warning(f"Sieťová chyba pri trash_imap_email_task: {e}. Skúšam znova...")
        raise self.retry(exc=e)

@shared_task(base=ISASAutoHealTask, bind=True, max_retries=3, default_retry_delay=10)
def send_email_smtp_task(self, ucet_id, to_addr, subject, html_content, cc="", bcc="", attachments=None):
    try:
        ucet = EmailovyUcet.objects.get(id=ucet_id)
        send_email_smtp(ucet, to_addr, subject, html_content, cc, bcc, attachments)
    except EmailovyUcet.DoesNotExist:
        logger.error(f"Účet ID {ucet_id} neexistuje.")
    except Exception as e:
        logger.warning(f"Sieťová chyba pri send_email_smtp_task: {e}. Skúšam znova...")
        raise self.retry(exc=e)

@shared_task(base=ISASAutoHealTask, bind=True, max_retries=3, default_retry_delay=10)
def save_draft_imap_task(self, ucet_id, to_addr, subject, html_content, cc="", bcc="", attachments=None, message_id=None):
    try:
        ucet = EmailovyUcet.objects.get(id=ucet_id)
        save_draft_imap(ucet, to_addr, subject, html_content, cc, bcc, attachments, message_id)
    except EmailovyUcet.DoesNotExist:
        logger.error(f"Účet ID {ucet_id} neexistuje.")
    except Exception as e:
        logger.warning(f"Sieťová chyba pri save_draft_imap_task: {e}. Skúšam znova...")
        raise self.retry(exc=e)

@shared_task(base=ISASAutoHealTask, bind=True, max_retries=3, default_retry_delay=10)
def delete_draft_imap_task(self, ucet_id, message_id):
    try:
        ucet = EmailovyUcet.objects.get(id=ucet_id)
        delete_draft_imap(ucet, message_id)
    except EmailovyUcet.DoesNotExist:
        pass
    except Exception as e:
        logger.warning(f"Sieťová chyba pri delete_draft_imap_task: {e}. Skúšam znova...")
        raise self.retry(exc=e)

@shared_task(base=ISASAutoHealTask, bind=True, max_retries=3, default_retry_delay=10)
def empty_trash_imap_task(self, ucet_id):
    try:
        ucet = EmailovyUcet.objects.get(id=ucet_id)
        empty_trash_imap(ucet)
    except EmailovyUcet.DoesNotExist:
        pass
    except Exception as e:
        logger.warning(f"Sieťová chyba pri empty_trash_imap_task: {e}. Skúšam znova...")
        raise self.retry(exc=e)

@shared_task(base=ISASAutoHealTask, bind=True, max_retries=3, default_retry_delay=10)
def bulk_trash_imap_task(self, ucet_id, message_ids):
    try:
        ucet = EmailovyUcet.objects.get(id=ucet_id)
        bulk_trash_imap(ucet, message_ids)
    except EmailovyUcet.DoesNotExist:
        pass
    except Exception as e:
        logger.warning(f"Sieťová chyba pri bulk_trash_imap_task: {e}. Skúšam znova...")
        raise self.retry(exc=e)

@shared_task(base=ISASAutoHealTask, bind=True, max_retries=3, default_retry_delay=10)
def restore_imap_task(self, ucet_id, message_id):
    try:
        ucet = EmailovyUcet.objects.get(id=ucet_id)
        restore_imap(ucet, message_id)
    except EmailovyUcet.DoesNotExist:
        pass
    except Exception as e:
        logger.warning(f"Sieťová chyba pri restore_imap_task: {e}. Skúšam znova...")
        raise self.retry(exc=e)

@shared_task(base=ISASAutoHealTask, bind=True, max_retries=3, default_retry_delay=10)
def delete_permanently_imap_task(self, ucet_id, message_id):
    try:
        ucet = EmailovyUcet.objects.get(id=ucet_id)
        delete_permanently_imap(ucet, message_id)
    except EmailovyUcet.DoesNotExist:
        pass
    except Exception as e:
        logger.warning(f"Sieťová chyba pri delete_permanently_imap_task: {e}. Skúšam znova...")
        raise self.retry(exc=e)

@shared_task(base=ISASAutoHealTask, bind=True, max_retries=3, default_retry_delay=10)
def bulk_restore_imap_task(self, ucet_id, message_ids):
    try:
        ucet = EmailovyUcet.objects.get(id=ucet_id)
        bulk_restore_imap(ucet, message_ids)
    except EmailovyUcet.DoesNotExist:
        pass
    except Exception as e:
        logger.warning(f"Sieťová chyba pri bulk_restore_imap_task: {e}. Skúšam znova...")
        raise self.retry(exc=e)

@shared_task(base=ISASAutoHealTask, bind=True, max_retries=3, default_retry_delay=10)
def bulk_delete_permanently_imap_task(self, ucet_id, message_ids):
    try:
        ucet = EmailovyUcet.objects.get(id=ucet_id)
        bulk_delete_permanently_imap(ucet, message_ids)
    except EmailovyUcet.DoesNotExist:
        pass
    except Exception as e:
        logger.warning(f"Sieťová chyba pri bulk_delete_permanently_imap_task: {e}. Skúšam znova...")
        raise self.retry(exc=e)

@shared_task(base=ISASAutoHealTask)
def delete_physical_file_task(file_name):
    """
    Asynchrónne vymaže fyzický súbor z disku/úložiska.
    Chráni Gunicorn workery pred I/O blokovaním pri hromadnom mazaní záznamov.
    """
    try:
        if file_name and default_storage.exists(file_name):
            default_storage.delete(file_name)
            logger.info(f"🗑️ [GARBAGE COLLECTOR] Súbor úspešne vymazaný: {file_name}")
    except Exception as e:
        logger.error(f"❌ [GARBAGE COLLECTOR] Chyba pri mazaní súboru {file_name}: {e}")

@shared_task(base=ISASAutoHealTask, bind=True, max_retries=3, default_retry_delay=10)
def forward_mail_to_technician_task(self, mail_id, technik_id, poznamka):
    """Prepošle dôležitý e-mail technikovi priamo na Telegram alebo Discord."""    
    
    # 1. FÁZA: Databáza (Ak záznam neexistuje, nemá zmysel task opakovať)
    try:
        mail = EmailovaSprava.objects.get(id=mail_id)
        technik = Zamestnanec.objects.get(id=technik_id)
    except (EmailovaSprava.DoesNotExist, Zamestnanec.DoesNotExist) as db_err:
        logger.error(f"❌ Záznam neexistuje, ruším preposielanie: {db_err}")
        return

    # 2. FÁZA: Sieťová komunikácia (Tu chytáme výpadky a opakujeme task)
    try:
        bezpecny_odosielatel = html.escape(mail.odosielatel or "Neznámy")
        bezpecny_predmet = html.escape(mail.predmet or "Bez predmetu")
        # 🛡️ SMART FALLBACK: Ak chýba plain text, extrahujeme čistý text z HTML tela správy
        raw_obsah = mail.telo_text or strip_tags(mail.telo_html or "") or "Správa neobsahuje textový obsah."
        bezpecny_text = html.escape(raw_obsah.strip())
        skrateny_text = bezpecny_text[:3500] + ("<br><i>... (text správy bol skrátený)</i>" if len(bezpecny_text) > 3500 else "")
        cisty_text_mailu = skrateny_text.replace('\n', '<br>')
        bezpecne_meno = html.escape(str(getattr(technik, 'meno', '') or 'kolega').strip())

        # --- Odoslanie na Telegram a E-mail (Cez ISASNotifier) ---
        builder = (ISASNotificationBuilder(nadpis='PREPOSLANÝ E-MAIL Z DISPEČINGU', ikona='📧', farba_temy='#8b5cf6')
                   .pridaj_riadok('👤 Od:', bezpecny_odosielatel)
                   .pridaj_riadok('🏷️ Predmet:', bezpecny_predmet)
                   .nastav_hlavny_text(f"👋 Ahoj <b>{bezpecne_meno}</b>,<br><br>Dispečing ti preposiela dôležitý e-mail z centrály. Prosím, venuj mu pozornosť.<br><br><b>📝 Pôvodný text správy:</b><br><i>{cisty_text_mailu}</i>")
                   .nastav_ai_poznamku(mail.ai_zhrnutie if mail.ai_zhrnutie else None))
        
        if poznamka:
            builder.nastav_upozornenie(f"💬 <b>Odkaz od dispečera:</b> {html.escape(poznamka)}", 'oranzove')

        payload = builder.build()

        # 🛡️ Sanitizácia kontaktov zamedzí aktivácii fallbacku na majiteľa pri prázdnych znakoch
        t_email = str(getattr(technik, 'email', '') or '').strip()
        t_tg = str(getattr(technik, 'telegram_id', '') or '').strip()
        t_discord = str(getattr(technik, 'discord_id', '') or '').strip()

        if not any([t_email, t_tg, t_discord]):
            logger.warning(f"Technik {technik_id} ({technik.meno}) nemá definovaný žiadny notifikačný kanál. Preposielanie bolo vynechané.")
            return

        ISASNotifier.send(
            payload=payload,
            to_email=t_email or None,
            tg_chat_id=t_tg or None,
            discord_id=t_discord or None,
            send_tg=bool(t_tg),
            send_mail=bool(t_email),
            send_discord=bool(t_discord)
        )
            
    except Exception as e:
        logger.warning(f"⚠️ Sieťová chyba pri preposielaní mailu technikovi {technik_id}: {e}. Skúšam znova...")
        raise self.retry(exc=e)

@shared_task(base=ISASAutoHealTask, bind=True, max_retries=3, default_retry_delay=15)
def forward_invoice_to_evidencia_task(self, attachment_id: int):
    """
    100% Asynchrónne spracovanie faktúry bez blokovania HTTP workerov:
    1. Vytvorí záznam PrichodziaFaktura a bezpečne prenesie súbor.
    2. Zostaví unifikovanú notifikáciu cez ISASNotificationBuilder.
    3. Odošle SMTP e-mail a zapíše transakciu do CRM histórie.
    """
    try:
        priloha = EmailPriloha.objects.select_related('sprava__dodavatel', 'sprava__ucet').get(id=attachment_id)
    except EmailPriloha.DoesNotExist:
        logger.warning(f"Príloha ID {attachment_id} už neexistuje. Úloha bola ukončená bez opakovania.")
        return

    try:
        mail = priloha.sprava
        if not mail or not mail.ucet or not mail.odosielatel:
            logger.warning(f"Príloha {attachment_id} nemá kompletnú správu, odosielateľa alebo účet. Spracovanie zrušené.")
            return

        unikatna_poznamka = f"⚠️ Ručne presmerované z ISAS CORE (Príloha ID: {priloha.id})"

        # 1. 🗃️ ATOMICKÁ EVIDENCIA FAKTÚRY (Ochrana pred Race Condition a podpora bezpečného Retry)
        crm_identifikator = f"isas-warn-att-{attachment_id}"
        if EmailovaSprava.objects.filter(message_id=crm_identifikator).exists():
            logger.info(f"Faktúra aj notifikácia pre prílohu {attachment_id} už boli kompletne spracované.")
            return

        # 🛡️ DEFENSIVE: Overenie fyzického súboru pred zásahom do DB (zamedzí vzniku sirotských záznamov)
        if not priloha.subor or not priloha.subor.name or not priloha.subor.storage.exists(priloha.subor.name):
            logger.error(f"Fyzický súbor prílohy {attachment_id} na úložisku neexistuje.")
            return

        cisty_nazov = os.path.basename(str(priloha.nazov_suboru or '').replace('\\', '/'))
        bezpecny_nazov = cisty_nazov or f"priloha_{priloha.id}.pdf"

        nova_faktura = PrichodziaFaktura.objects.filter(poznamka=unikatna_poznamka).first()
        if not nova_faktura:
            with transaction.atomic():
                nova_faktura = PrichodziaFaktura(
                    zdroj='EMAIL',
                    dodavatel=mail.dodavatel,
                    poznamka=unikatna_poznamka
                )
                with priloha.subor.open('rb') as f_stream:
                    nova_faktura.subor.save(bezpecny_nazov, ContentFile(f_stream.read()), save=False)
                nova_faktura.save()
        elif not nova_faktura.subor:
            with transaction.atomic():
                with priloha.subor.open('rb') as f_stream:
                    nova_faktura.subor.save(bezpecny_nazov, ContentFile(f_stream.read()), save=True)

        # 2. 📧 UNIFIKOVANÝ NOTIFIKAČNÝ ENGINE
        gn = GlobalneNastavenia.objects.first()
        identita = FiremnaIdentita.objects.first()
        faktury_email = (
            (gn.email_pre_faktury if (gn and gn.email_pre_faktury) else None)
            or (identita.email_uctovnicky if (identita and identita.email_uctovnicky) else None)
            or (identita.email_fakturacia if (identita and identita.email_fakturacia) else "")
        )

        if not faktury_email:
            logger.warning("V Globálnych nastaveniach chýba 'email_pre_faktury'. Upozornenie dodávateľovi nebolo odoslané.")
            return

        nazov_firmy = identita.nazov_spolocnosti if (identita and identita.nazov_spolocnosti) else "Naša spoločnosť"
        pozdrav = generate_dynamic_greeting(mail.dodavatel, fallback="Dobrý deň,")

        logo_url = None
        if identita and identita.logo and identita.logo.name:
            if identita.logo.url.startswith('http'):
                logo_url = identita.logo.url
            else:
                # 🛡️ Zabezpečí platnú bázu aj vtedy, ak je gn.clean_base_url prázdny reťazec alebo None
                raw_candidate = getattr(gn, 'clean_base_url', None) or getattr(settings, 'SITE_URL', None) or ''
                raw_base = str(raw_candidate).strip()
                if raw_base and not raw_base.startswith(('http://', 'https://')):
                    raw_base = f"https://{raw_base}"
                base_url = raw_base.rstrip('/')
                logo_url = f"{base_url}/{identita.logo.url.lstrip('/')}" if base_url else identita.logo.url

        builder = ISASNotificationBuilder(
            nadpis="Potvrdenie prijatia faktúry",
            ikona="📄",
            farba_temy="#f59e0b"
        )
        if logo_url:
            builder.nastav_logo(logo_url)

        builder.pridaj_riadok("Dodávateľ:", mail.dodavatel.Nazov_spolocnosti if mail.dodavatel else mail.odosielatel)
        builder.pridaj_riadok("Názov dokladu:", bezpecny_nazov)
        builder.pridaj_riadok("Dátum prijatia:", timezone.now().strftime("%d.%m.%Y %H:%M"))
        
        builder.nastav_hlavny_text(
            f"{pozdrav}<br><br>"
            f"Vašu faktúru pre spoločnosť <strong>{nazov_firmy}</strong> sme úspešne prijali "
            f"a zaevidovali do nášho interného systému."
        )
        
        builder.nastav_upozornenie(
            f"<strong>⚠️ DÔLEŽITÉ UPOZORNENIE K FAKTURÁCII</strong><br>"
            f"V budúcnosti Vás dôrazne žiadame, aby ste všetky faktúry zasielali <b>VÝHRADNE</b> "
            f"na našu automatizovanú linku: <a href='mailto:{faktury_email}' style='color: #b45309; font-weight: bold;'>{faktury_email}</a>.<br>"
            f"⏳ Faktúry zaslané na iné e-mailové adresy nemusia byť systémom zachytené a uhradené včas.",
            typ='oranzove'
        )

        payload = builder.build()
        payload['hlavicka_text'] = nazov_firmy
        payload['paticka_text'] = f"{nazov_firmy} | Automatizovaný systém ISAS CORE"

        html_odpovede = render_to_string('filters_and_notifications/univerzalna_notifikacia.html', {'payload': payload})
        plain_text = strip_tags(html_odpovede)

        # 3. 🚀 SMTP ODOSLANIE CEZ SCHRÁNKU ODOSIELATEĽA
        cisty_odosielatel = parseaddr(mail.odosielatel)[1].strip() or mail.odosielatel
        cisty_predmet = " ".join(str(mail.predmet or '').split())
        send_email_smtp(
            mail.ucet,
            to_addr=cisty_odosielatel,
            subject=f"Re: {cisty_predmet} (Dôležité upozornenie k fakturácii)",
            html_content=html_odpovede
        )

        # 4. 💾 ZÁPIS DO CRM HISTÓRIE (Idempotentné ošetrenie pri opakovaní tasku)
        EmailovaSprava.objects.get_or_create(
            ucet=mail.ucet,
            message_id=crm_identifikator,
            defaults={
                'odosielatel': mail.ucet.email_adresa,
                'prijemca': mail.odosielatel,
                'predmet': f"Re: {mail.predmet} (Upozornenie k fakturácii)",
                'datum_prijatia': timezone.now(),
                'telo_text': plain_text,
                'telo_html': html_odpovede,
                'precitane': True,
                'zlozka': 'SENT',
                'dodavatel': mail.dodavatel
            }
        )

    except Exception as exc:
        logger.error(f"Kritická chyba pri spracovaní faktúry {attachment_id}: {exc}")
        raise self.retry(exc=exc)

@shared_task(base=ISASAutoHealTask, name="cleanup_ai_pdf_cache_task")
def cleanup_ai_pdf_cache_task():
    """
    🛡️ ISAS DEVOPS: Bezpečný Garbage Collector pre AI PDF Cache.
    Spúšťa sa v noci, maže staré vygenerované obrázky a chráni disk pred zaplnením.
    """
    
    logger.info("🧹 [GARBAGE COLLECTOR] Spúšťam čistenie AI PDF cache...")
    
    # 1. Bezpečná konštrukcia cesty (Ochrana pred Path Traversal)
    cache_dir = os.path.abspath(os.path.join(settings.MEDIA_ROOT, 'ai_pdf_cache'))
    
    # Overenie, či cache_dir skutočne leží vo vnútri MEDIA_ROOT (Hard security check)
    if not cache_dir.startswith(os.path.abspath(settings.MEDIA_ROOT) + os.sep):
        logger.critical("🚨 [SECURITY] Pokus o únik z MEDIA_ROOT! Task bol okamžite zastavený.")
        return

    if not os.path.exists(cache_dir) or not os.path.isdir(cache_dir):
        logger.info("ℹ️ [GARBAGE COLLECTOR] Zložka cache neexistuje, nie je čo mazať.")
        return

    # 2. Nastavenie časového limitu (2 hodiny = 7200 sekúnd)
    cutoff_time = time.time() - 7200
    zmazane_subory = 0
    chyby = 0

    # 3. VÝKON: Použitie os.scandir() namiesto os.listdir()
    # scandir() je iterátor (C-level), nenačítava všetko do RAM a rovno obsahuje atribúty súboru.
    try:
        with os.scandir(cache_dir) as entries:
            for entry in entries:
                try:
                    # 🛡️ BEZPEČNOSŤ 1: Ignorujeme symlinky (Ochrana pred zmazaním cudzích súborov)
                    # 🛡️ BEZPEČNOSŤ 2: Musí to byť súbor, nie zložka
                    if not entry.is_file(follow_symlinks=False):
                        continue
                        
                    # 🛡️ BEZPEČNOSŤ 3: Prísna kontrola názvu a prípony (Zmaže len to, čo vytvorila AI)
                    if not (entry.name.startswith('pdf_render_') and entry.name.endswith('.png')):
                        continue

                    # 4. Kontrola veku súboru
                    if entry.stat().st_mtime < cutoff_time:
                        # 🛡️ BEZPEČNOSŤ 4: TOCTOU ochrana (Try-Catch priamo na remove)
                        os.remove(entry.path)
                        zmazane_subory += 1
                        
                except FileNotFoundError:
                    # Súbor už zmazal iný proces (Race condition) - ignorujeme
                    pass
                except PermissionError:
                    logger.warning(f"⚠️ [GARBAGE COLLECTOR] Nemám práva na zmazanie: {entry.name}")
                    chyby += 1
                except Exception as e:
                    logger.error(f"❌ [GARBAGE COLLECTOR] Neočakávaná chyba pri súbore {entry.name}: {e}")
                    chyby += 1
                    
    except Exception as e:
        logger.critical(f"🚨 [GARBAGE COLLECTOR] Kritické zlyhanie pri čítaní zložky: {e}")
        return

    logger.info(f"✅ [GARBAGE COLLECTOR] Hotovo. Zmazaných súborov: {zmazane_subory}. Chýb: {chyby}.")

@shared_task(base=ISASAutoHealTask, bind=True, max_retries=3)
def task_kompletna_denna_zaloha(self):
    """
    🛡️ ENTERPRISE BACKUP TASK (DB + MEDIA)
    1. Vytvorí zálohu PostgreSQL databázy.
    2. Vytvorí komprimovaný archív zložky 'media' (kvôli signálom, ktoré mažú súbory).
    3. Overí integritu oboch záloh.
    4. Vymaže staré zálohy (udržuje posledných 30 dní).
    """
    # --- NASTAVENIA ---
    POCET_DNI_NA_UCHOVANIE = 30  # 30 dní je štandard pre ERP systémy
    BACKUP_DIR = os.path.join(settings.BASE_DIR, 'isas_backups')
    MEDIA_DIR = os.path.abspath(str(settings.MEDIA_ROOT))
    
    os.makedirs(BACKUP_DIR, exist_ok=True)

    # Databázové nastavenia
    db_settings = settings.DATABASES['default']
    db_engine = db_settings.get('ENGINE', '')
    if 'postgresql' not in db_engine and 'postgres' not in db_engine:
        logger.warning(f"⚠️ Databázový engine ({db_engine}) nepodporuje pg_dump. DB záloha bola bezpečne preskočená.")
        return

    db_name = db_settings.get('NAME') or ''
    db_user = db_settings.get('USER') or ''
    db_password = db_settings.get('PASSWORD') or ''
    db_host = db_settings.get('HOST') or 'localhost'
    db_port = db_settings.get('PORT') or '5432'

    # Generovanie spoločného timestampu pre DB aj Media (aby patrili k sebe)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    db_filename = f"backup_db_{db_name}_{timestamp}.dump"
    media_filename = f"backup_media_{timestamp}.tar.gz"
    
    db_filepath = os.path.join(BACKUP_DIR, db_filename)
    media_filepath = os.path.join(BACKUP_DIR, media_filename)

    # ==========================================
    # 1. ZÁLOHA DATABÁZY
    # ==========================================
    logger.info(f"🚀 Spúšťam zálohu DB: {db_filename}")
    env = os.environ.copy()
    env['PGPASSWORD'] = str(db_password or '')

    # Parameter -w (--no-password) zabráni interaktívnemu zamrznutiu, ak zlyhá autentifikácia
    command = [
        'pg_dump',
        '-w',
        '-h', str(db_host), '-p', str(db_port), '-U', str(db_user),
        '-F', 'c', '-f', db_filepath, str(db_name)
    ]

    try:
        # Timeout 1800s garantuje, že proces nezablokuje worker navždy
        subprocess.run(command, env=env, check=True, capture_output=True, text=True, timeout=1800)
        if not os.path.exists(db_filepath) or os.path.getsize(db_filepath) == 0:
            raise Exception("DB záloha zlyhala: Súbor je prázdny.")
        logger.info(f"✅ DB záloha úspešná ({os.path.getsize(db_filepath) / 1024 / 1024:.2f} MB)")
    except subprocess.CalledProcessError as e:
        # Odstránime nekompletný súbor, aby nezostával na disku poškodený dump
        if os.path.exists(db_filepath):
            try:
                os.remove(db_filepath)
            except OSError:
                pass
        err_detail = e.stderr.strip() if e.stderr else str(e)
        logger.error(f"❌ Kritická chyba pri DB zálohe (PostgreSQL error): {err_detail}")
        raise self.retry(exc=Exception(err_detail), countdown=300)
    except Exception as e:
        if os.path.exists(db_filepath):
            try:
                os.remove(db_filepath)
            except OSError:
                pass
        logger.error(f"❌ Kritická chyba pri DB zálohe: {str(e)}")
        raise self.retry(exc=e, countdown=300)

    # ==========================================
    # 2. ZÁLOHA SÚBOROV (MEDIA FOLDER)
    # ==========================================
    logger.info(f"📦 Spúšťam zálohu súborov: {media_filename}")
    try:
        if os.path.exists(MEDIA_DIR):
            # Filter vylúči dočasnú zložku ai_pdf_cache, čím šetrí gigabajty v zálohách
            def vyluc_ai_cache(tarinfo):
                if 'ai_pdf_cache' in tarinfo.name or not os.path.exists(os.path.join(MEDIA_DIR, os.path.relpath(tarinfo.name, os.path.basename(MEDIA_DIR)))):
                    return None
                return tarinfo

            with tarfile.open(media_filepath, "w:gz", errorlevel=0) as tar:
                tar.add(MEDIA_DIR, arcname=os.path.basename(MEDIA_DIR), filter=vyluc_ai_cache)
            
            if not os.path.exists(media_filepath) or os.path.getsize(media_filepath) == 0:
                raise Exception("Media záloha zlyhala: Archív je prázdny.")
            logger.info(f"✅ Media záloha úspešná ({os.path.getsize(media_filepath) / 1024 / 1024:.2f} MB)")
        else:
            logger.warning("⚠️ Zložka MEDIA_ROOT neexistuje, preskakujem zálohu súborov.")
    except Exception as e:
        # Odstránime neúplný archív pri prerušení
        if os.path.exists(media_filepath):
            try:
                os.remove(media_filepath)
            except OSError:
                pass
        logger.error(f"❌ Kritická chyba pri Media zálohe: {str(e)}")
        raise self.retry(exc=e, countdown=300)

    # ==========================================
    # 3. BEZPEČNÉ PREMAZANIE STARÝCH ZÁLOH
    # ==========================================
    # Vykoná sa LEN ak DB aj Media zbehli bez chyby!
    try:
        # Mazanie starých DB záloh (izolované voči pádu pri jednom súbore)
        vsetky_db = sorted(glob.glob(os.path.join(BACKUP_DIR, 'backup_db_*.dump')), key=os.path.getmtime, reverse=True)
        for stara_db in vsetky_db[POCET_DNI_NA_UCHOVANIE:]:
            try:
                os.remove(stara_db)
                logger.info(f"🗑️ Vymazaná stará DB záloha: {os.path.basename(stara_db)}")
            except OSError as e_db:
                logger.warning(f"⚠️ Nepodarilo sa zmazať starú DB zálohu {stara_db}: {e_db}")

        # Mazanie starých Media záloh
        vsetky_media = sorted(glob.glob(os.path.join(BACKUP_DIR, 'backup_media_*.tar.gz')), key=os.path.getmtime, reverse=True)
        for stare_media in vsetky_media[POCET_DNI_NA_UCHOVANIE:]:
            try:
                os.remove(stare_media)
                logger.info(f"🗑️ Vymazaná stará Media záloha: {os.path.basename(stare_media)}")
            except OSError as e_med:
                logger.warning(f"⚠️ Nepodarilo sa zmazať starú Media zálohu {stare_media}: {e_med}")
            
    except Exception as e:
        logger.warning(f"⚠️ Nepodarilo sa vymazať staré zálohy: {str(e)}")