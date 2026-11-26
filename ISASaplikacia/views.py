import base64
import datetime
import json
import logging
import mimetypes
import os
import posixpath
import re
import secrets
import time
import urllib.parse
from zoneinfo import ZoneInfo

from google import genai
from google.genai import types
import requests

from django.apps import apps
from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist, SuspiciousFileOperation
from django.db import transaction
from django.views.decorators.http import require_http_methods
from django.db.models import Count, Exists, F, OuterRef, Prefetch, Q, Sum, Window, Avg
from django.db.models.functions import RowNumber
from django.http import FileResponse, Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.utils._os import safe_join
from django.utils.dateparse import parse_date
from django.utils.html import escape
from django.views.decorators.csrf import csrf_exempt

from ISASaplikacia.utils import (
    get_gemini_api_key,
    get_google_maps_api_key,
)
from ISASaplikacia.utils_helper import (
    vypocitaj_vzdialenost_metre,
    prepare_attachments_for_celery,
    save_attachments_to_db,
    group_emails_by_account
)
from .meniny_db import MENINY_KALENDAR
from .models import (
    EmailovaSprava, EmailovyUcet, EmailPriloha,
    FiremnaIdentita, GlobalneNastavenia, SpotifyToken,
    ISASHraLeaderboard
)
from .tasks import (
    bulk_delete_permanently_imap_task,
    bulk_restore_imap_task,
    bulk_trash_imap_task,
    delete_draft_imap_task,
    delete_permanently_imap_task,
    empty_trash_imap_task,
    forward_invoice_to_evidencia_task,
    forward_mail_to_technician_task,
    mark_imap_read_task,
    restore_imap_task,
    save_draft_imap_task,
    send_email_smtp_task,
    trash_imap_email_task
)

from Evidencia.models import (
    Dodavatel, EvidenciaJazd, GPSStopa, PlanovanyServisZariadenia,
    PrichodziaFaktura, ServisovaneZariadenie, SukromnaOsoba,
    TypServisuPrace, Uloha, Vozidlo, VystavenaFaktura,
    Zakaznik, Zamestnanec
)
from Evidencia.utils_helper import get_firemna_mena
from Evidencia.industry_loader import get_easter_egg_tiles

logger = logging.getLogger(__name__)

def home(request):
    """Presmerovanie domovskej stránky na administráciu."""
    return redirect('/admin/')

@csrf_exempt
@staff_member_required
def isas_mail_api(request):
    if request.method == "POST":
        try:
            # Podpora pre JSON aj FormData (kvôli prílohám)
            if request.content_type == 'application/json':
                data = json.loads(request.body)
                files = []
            else:
                data = request.POST
                files = request.FILES.getlist('attachments')
                
            akcia = data.get('action')
            
            # ==========================================================
            # 1. NAČÍTANIE A VYHĽADÁVANIE E-MAILOV (S MULTI-ACCOUNT FILTEROM)
            # ==========================================================
            if akcia == "get_list":
                zlozka = data.get('folder', 'INBOX')
                account_id = data.get('account_id', 'ALL')
                vyhladavanie = (data.get('search') or '').strip()
                
                # 🎛️ Načítanie dynamických SLA limitov z front-endu
                try:
                    sla_zak = float(data.get('sla_zak', 4))
                    sla_dod = float(data.get('sla_dod', 24))
                except (ValueError, TypeError):
                    sla_zak, sla_dod = 4, 24
                
                # 🚀 MAXIMÁLNY VÝKON: Exists subquery cez pole 'sprava' + defer ťažkých HTML tiel (0 N+1)
                prilohy_subquery = EmailPriloha.objects.filter(sprava=OuterRef('pk'))

                maily = EmailovaSprava.objects.filter(
                    zlozka=zlozka,
                    ucet__je_aktivny=True
                ).select_related(
                    'ucet', 'zakaznik', 'dodavatel', 'zamestnanec'
                ).annotate(
                    ma_prilohu_db=Exists(prilohy_subquery)
                ).defer(
                    'telo_html', 'telo_text'
                )
                
                # 🗄️ MULTI-ACCOUNT FILTER: Konkrétna schránka vs Všetky schránky (Unified Inbox)
                if account_id and str(account_id) != 'ALL':
                    try:
                        maily = maily.filter(ucet_id=int(account_id))
                    except (ValueError, TypeError):
                        pass
                
                # 🚀 ELITNÉ VYHĽADÁVANIE (Ak užívateľ niečo zadal do lupy)
                if vyhladavanie:
                    maily = maily.filter(
                        Q(odosielatel__icontains=vyhladavanie) |
                        Q(predmet__icontains=vyhladavanie) |
                        Q(zakaznik__Nazov_spolocnosti__icontains=vyhladavanie) |
                        Q(dodavatel__Nazov_spolocnosti__icontains=vyhladavanie) |
                        Q(zamestnanec__meno__icontains=vyhladavanie) |
                        Q(zamestnanec__priezvisko__icontains=vyhladavanie)
                    )

                # Limit na 50 je skvelá poistka proti zahĺteniu RAM
                maily = maily.order_by('-datum_prijatia')[:50]
                
                now = timezone.now()
                vystup = []
                for m in maily:
                    odosielatel = m.odosielatel.split('<')[0].strip() if m.odosielatel and '<' in m.odosielatel else (m.odosielatel or "Neznámy odosielateľ")
                    
                    # 🚨 DYNAMICKÝ SLA WATCHDOG
                    sla_alert_customer = False
                    sla_alert_supplier = False
                    
                    if not m.precitane and m.datum_prijatia:
                        d_prijatia = m.datum_prijatia
                        if timezone.is_aware(now) and timezone.is_naive(d_prijatia):
                            d_prijatia = timezone.make_aware(d_prijatia, timezone.get_current_timezone())
                        elif timezone.is_naive(now) and timezone.is_aware(d_prijatia):
                            d_prijatia = timezone.make_naive(d_prijatia, timezone.get_current_timezone())
                        rozdiel_hodin = (now - d_prijatia).total_seconds() / 3600
                        if m.zakaznik and sla_zak > 0 and rozdiel_hodin > sla_zak:
                            sla_alert_customer = True
                        elif m.dodavatel and sla_dod > 0 and rozdiel_hodin > sla_dod:
                            sla_alert_supplier = True

                    vystup.append({
                        "id": m.id,
                        "odosielatel_meno": odosielatel,
                        "predmet": m.predmet or "Bez predmetu",
                        "datum": m.datum_prijatia.strftime("%d.%m.") if m.datum_prijatia else "",
                        "precitane": m.precitane,
                        "ai_kat": m.ai_kategoria,
                        "ma_prilohu": m.ma_prilohu_db,
                        "sla_alert_customer": sla_alert_customer,
                        "sla_alert_supplier": sla_alert_supplier,
                        "ucet_id": m.ucet_id,
                        "ucet_nazov": m.ucet.nazov if m.ucet else "",
                        "ucet_email": m.ucet.email_adresa if m.ucet else ""
                    })
                return JsonResponse({"status": "success", "mails": vystup})

            # ==========================================================
            # 2. NAČÍTANIE DETAILU SPRÁVY & IMAP SYNC
            # ==========================================================
            elif akcia == "get_email":
                mail = EmailovaSprava.objects.select_related(
                    'ucet', 'zakaznik', 'dodavatel', 'zamestnanec'
                ).prefetch_related('prilohy').get(id=data.get('id'))
                
                # OBOJSMERNÝ SYNC: IMAP READ (CELERY TASK) - Atomická ochrana pred Race Condition
                if not mail.precitane:
                    zmenene_riadky = EmailovaSprava.objects.filter(id=mail.id, precitane=False).update(precitane=True)
                    if zmenene_riadky > 0 and mail.ucet:
                        try:
                            mark_imap_read_task.delay(mail.ucet.id, mail.message_id)
                        except Exception as exc:
                            logger.warning(f"Zlyhalo zaradenie mark_imap_read_task do Celery: {exc}")
                
                crm_badge = ""
                if mail.zakaznik: 
                    crm_badge = f"🏢 Zákazník: {mail.zakaznik.Nazov_spolocnosti}"
                elif mail.dodavatel: 
                    crm_badge = f"🏭 Dodávateľ: {mail.dodavatel.Nazov_spolocnosti}"
                elif mail.zamestnanec: 
                    crm_badge = f"👤 Zamestnanec: {mail.zamestnanec.meno} {mail.zamestnanec.priezvisko}"

                prilohy = [{
                    "id": p.id, 
                    "nazov": p.nazov_suboru or "Neznámy súbor", 
                    "url": p.subor.url if p.subor else "", 
                    "is_pdf": (p.nazov_suboru or "").lower().endswith('.pdf')
                } for p in mail.prilohy.all()]

                return JsonResponse({
                    "status": "success",
                    "subject": mail.predmet or "Bez predmetu",
                    "sender_name": mail.odosielatel.split('<')[0].strip() if mail.odosielatel and '<' in mail.odosielatel else (mail.odosielatel or "Neznámy odosielateľ"),
                    "sender_email": mail.odosielatel,
                    "to_email": mail.prijemca,
                    "date": mail.datum_prijatia.strftime("%d.%m.%Y %H:%M") if mail.datum_prijatia else "",
                    "html_body": mail.telo_html or (escape(mail.telo_text).replace('\n', '<br>') if mail.telo_text else ""),
                    "ai_summary": mail.ai_zhrnutie,
                    "crm_badge": crm_badge,
                    "prilohy": prilohy,
                    "is_outbound": mail.odosielatel == (mail.ucet.email_adresa if mail.ucet else ""),
                    "account_id": mail.ucet_id,
                    "account_name": mail.ucet.nazov if mail.ucet else "Neznámy účet",
                    "account_email": mail.ucet.email_adresa if mail.ucet else ""
                })

            # ==========================================================
            # 3. AI ASISTENT (GENERÁTOR ODPOVEDÍ)
            # ==========================================================
            elif akcia == "generate_reply":
                # 🛡️ DEFENSIVE GRACEFUL DEGRADATION: Poistka so správnou prioritou (Cache -> DB -> .env)
                api_key = get_gemini_api_key()
                gemini_model = getattr(settings, 'GEMINI_MODEL', None)

                if not api_key or not gemini_model:
                    logger.warning("⚠️ [ISAS AI] Gemini API kľúč alebo GEMINI_MODEL nie je nakonfigurovaný.")
                    return JsonResponse({
                        "status": "error",
                        "message": "🤖 AI asistent nie je nastavený (chýba API kľúč alebo model)."
                    }, status=503)

                # 🚀 VÝKON: Pridané select_related, aby sme predišli N+1 queries pri zisťovaní kontextu
                mail = EmailovaSprava.objects.select_related('zakaznik', 'dodavatel', 'zamestnanec').get(id=data.get('id'))

                # 🏢 DYNAMICKÁ IDENTITA FIRMY A GLOBÁLNE NASTAVENIA
                identita = FiremnaIdentita.objects.first()
                nazov_firmy = identita.nazov_spolocnosti if identita and identita.nazov_spolocnosti else "Naša spoločnosť"
                
                gn = GlobalneNastavenia.objects.first()
                mena_firmy = get_firemna_mena()
                
                sadzby_list = []
                if gn and gn.hodinova_sadzba_technik is not None:
                    sadzby_list.append(f"základná sadzba technika: {gn.hodinova_sadzba_technik} {mena_firmy}/hod")
                if gn and gn.sadzba_za_km_zakaznik is not None:
                    sadzby_list.append(f"doprava: {gn.sadzba_za_km_zakaznik} {mena_firmy}/km")
                sadzby_info = f" Cenníkové sadzby ({', '.join(sadzby_list)})." if sadzby_list else ""

                email_faktury = (
                    (gn.email_pre_faktury if (gn and gn.email_pre_faktury) else None)
                    or (identita.email_uctovnicky if (identita and identita.email_uctovnicky) else None)
                    or (identita.email_fakturacia if (identita and identita.email_fakturacia) else None)
                    or (identita.email_vseobecny if (identita and identita.email_vseobecny) else None)
                )
                faktury_info = f" Faktúry prijímame výhradne na e-mail: {email_faktury}." if email_faktury else ""

                # 🧠 DYNAMICKÁ DOMÉNOVÁ EXPERTÍZA (Zero-Shot)
                typy_prac = list(TypServisuPrace.objects.values_list('nazov', flat=True))
                typy_prac_str = ", ".join(typy_prac) if typy_prac else "servis a údržba"
                
                zariadenia_kategorie = ServisovaneZariadenie.objects.values_list('kategoria', flat=True).distinct()
                kategorie_dict = dict(ServisovaneZariadenie.KATEGORIE)
                zariadenia_nazvy = [re.sub(r'[^\w\s,/()-]', '', str(kategorie_dict.get(k, k) or '')).strip() for k in zariadenia_kategorie if k is not None]
                zariadenia_str = ", ".join(zariadenia_nazvy) if zariadenia_nazvy else "technické zariadenia"
                
                domenova_expertiza = f"Firma vykonáva najmä: {typy_prac_str}. Servisuje zariadenia ako: {zariadenia_str}."

                # 👥 CRM KONTEXT ODOSIELATEĽA (Vrátane Súkromných osôb)
                crm_kontext = "Neznámy odosielateľ (Potenciálny nový klient, partner alebo spam)"
                if mail.zakaznik:
                    crm_kontext = f"Náš existujúci zákazník: {mail.zakaznik.Nazov_spolocnosti}"
                elif mail.dodavatel:
                    crm_kontext = f"Náš dodávateľ: {mail.dodavatel.Nazov_spolocnosti}"
                elif mail.zamestnanec:
                    rola = mail.zamestnanec.get_pracovna_rola_display() if hasattr(mail.zamestnanec, 'get_pracovna_rola_display') else "Zamestnanec"
                    crm_kontext = f"Náš interný zamestnanec: {mail.zamestnanec.meno} {mail.zamestnanec.priezvisko} ({rola})"
                else:
                    # Fallback pre fyzické osoby, ktoré nie sú priamo v modeli EmailovaSprava
                    email_from = mail.odosielatel.split('<')[-1].replace('>', '').strip() if mail.odosielatel else ""
                    sukromna_osoba = SukromnaOsoba.objects.filter(email__iexact=email_from).first() if email_from else None
                    if sukromna_osoba:
                        crm_kontext = f"Súkromná osoba / Fyzická osoba: {sukromna_osoba.meno_a_priezvisko}"

                # 🌍 ROZŠÍRENIE ZÁBERU SOFTVÉRU PRE AI
                rozsirena_expertiza = (
                    f"{domenova_expertiza} "
                    f"Okrem servisu firma spravuje aj vlastný vozový park, skladové zásoby, "
                    f"vystavuje cenové ponuky a rieši povolenia na vstup do fabrík (Gate pass)."
                    f"{sadzby_info}"
                    f"{faktury_info}"
                )

                prompt = (
                    f"Si elitný asistent/ka v ERP systéme spoločnosti {nazov_firmy}. "
                    f"ZAMERANIE FIRMY: {rozsirena_expertiza}\n\n"
                    f"Tu je prichádzajúci e-mail. 🧠 KONTEXT ODOSIELATEĽA Z NAŠEJ DATABÁZY: {crm_kontext}\n"
                    f"TEXT E-MAILU:\n{mail.telo_text or mail.telo_html or ''}\n\n"
                    f"Tvojou úlohou je napísať vysoko profesionálnu, zdvorilú a jasnú odpoveď v slovenčine.\n"
                    f"🛡️ ANTI-HALUCINAČNÉ PRAVIDLÁ:\n"
                    f"1. ZAKÁZANÉ: Nesľubuj termíny, ceny, kapacity ani skladové zásoby, ktoré nevieš garantovať. Ak si e-mail vyžaduje nacenenie, termín alebo kontrolu skladu, napíš, že požiadavku evidujeme a čoskoro sa ozveme.\n"
                    f"2. ZAKÁZANÉ: Nevymýšľaj si mená kolegov, čísla faktúr ani technické parametre.\n"
                    f"3. ZAKÁZANÉ: Nevytváraj e-mailový podpis (žiadne telefónne čísla, weby ani názov firmy na konci). Systém ho pripojí automaticky. Správu ukonči len zdvorilostnou frázou (napr. 'S pozdravom').\n"
                    f"4. Prispôsob tón kontextu (zamestnancovi odpovedz interne a vecne, zákazníkovi vysoko profesionálne, dodávateľovi obchodne).\n"
                    f"VÝSTUP: Odpoveď vráť IBA ako čistý text pripravený na odoslanie (bez úvodných omáčok ako 'Tu je návrh odpovede', a absolútne BEZ markdown znakov ako **, len holý text)."
                )
                
                try:
                    client = genai.Client(api_key=api_key)
                    response = client.models.generate_content(
                        model=gemini_model, 
                        contents=[prompt],
                        config=types.GenerateContentConfig(thinking_config=types.ThinkingConfig(thinking_level=getattr(settings, 'GEMINI_THINKING_LEVEL', 'MEDIUM') or 'MEDIUM'))
                    )
                    odpoved_text = getattr(response, 'text', None) or ""
                    cisty_text = odpoved_text.replace('**', '').replace('*', '').strip()
                    if not cisty_text:
                        return JsonResponse({
                            "status": "error",
                            "message": "⚠️ AI model nevrátil žiadnu odpoveď (možné bezpečnostné obmedzenie obsahu)."
                        }, status=422)
                    return JsonResponse({"status": "success", "reply_text": cisty_text})
                except Exception as e:
                    logger.error(f"❌ [ISAS AI] Generovanie AI odpovede zlyhalo: {e}")
                    return JsonResponse({
                        "status": "error", 
                        "message": "⚠️ Generovanie odpovede zlyhalo. AI služba je dočasne nedostupná."
                    }, status=503)

            # ==========================================================
            # 4. ODOSLANIE E-MAILU (SMTP + IMAP ULOŽENIE)
            # ==========================================================
            elif akcia == "send_email":
                mail = EmailovaSprava.objects.select_related('ucet').get(id=data.get('id'))
                if not mail.ucet:
                    return JsonResponse({"status": "error", "message": "K správe nie je priradený odosielací účet."}, status=400)
                html_odpovede = data.get('html', '')
                cc = data.get('cc', '')
                bcc = data.get('bcc', '')
                
                # Spracovanie príloh do pamäte pre Celery
                file_data_list = prepare_attachments_for_celery(files)
                
                # Databázová operácia musí prebehnúť ako prvá a uspieť aj pri výpadku Redis brokera
                novy_mail = EmailovaSprava.objects.create(
                    ucet=mail.ucet, message_id=f"isas-out-{timezone.now().timestamp()}",
                    odosielatel=mail.ucet.email_adresa, prijemca=mail.odosielatel, predmet=f"Re: {mail.predmet}",
                    datum_prijatia=timezone.now(), telo_html=html_odpovede, precitane=True, zlozka='SENT',
                    zakaznik_id=mail.zakaznik_id, dodavatel_id=mail.dodavatel_id, zamestnanec_id=mail.zamestnanec_id
                )
                save_attachments_to_db(files, novy_mail, EmailPriloha)

                try:
                    send_email_smtp_task.delay(mail.ucet.id, mail.odosielatel, f"Re: {mail.predmet}", html_odpovede, cc, bcc, file_data_list)
                except Exception as exc:
                    logger.warning(f"Zlyhalo zaradenie send_email_smtp_task do Celery: {exc}")
                    return JsonResponse({"status": "warning", "message": "⚠️ E-mail bol zaznamenaný do systému, ale zaradenie na odoslanie zlyhalo (výpadok úlohového brokera)."}, status=200)

                return JsonResponse({"status": "success", "message": "E-mail bol úspešne odoslaný."})

            # ==========================================================
            # 4.5 ODOSLANIE ÚPLNE NOVEJ SPRÁVY (ZO ZVOLENÉHO ÚČTU)
            # ==========================================================
            elif akcia == "send_new_email":
                account_id = data.get('account_id')
                
                # 🎯 Výber presného účtu z UI alebo fallback na prvý aktívny
                if account_id and str(account_id) != 'ALL':
                    ucet = EmailovyUcet.objects.filter(id=account_id, je_aktivny=True).first()
                else:
                    ucet = EmailovyUcet.objects.filter(je_aktivny=True).first()
                    
                if not ucet:
                    return JsonResponse({"status": "error", "message": "Zvolená odosielacia schránka neexistuje alebo je neaktívna."}, status=400)
                
                to_addr = data.get('to')
                cc = data.get('cc', '')
                bcc = data.get('bcc', '')
                subject = data.get('subject')
                html_content = data.get('html', '')
                draft_id = data.get('draft_id')
                
                file_data_list = prepare_attachments_for_celery(files)
                
                # Databázová operácia musí prebehnúť ako prvá a uspieť aj pri výpadku Redis brokera
                novy_mail = EmailovaSprava.objects.create(
                    ucet=ucet, message_id=f"isas-new-{timezone.now().timestamp()}@isas.core",
                    odosielatel=ucet.email_adresa, prijemca=to_addr, predmet=subject,
                    datum_prijatia=timezone.now(), telo_html=html_content,
                    precitane=True, zlozka='SENT'
                )
                save_attachments_to_db(files, novy_mail, EmailPriloha)

                # Zmazanie starého konceptu zo správnej zložky na IMAPe
                if draft_id:
                    try:
                        stary_koncept = EmailovaSprava.objects.select_related('ucet').get(id=int(draft_id), zlozka='DRAFTS')
                        if stary_koncept.ucet:
                            try:
                                delete_draft_imap_task.delay(stary_koncept.ucet.id, stary_koncept.message_id)
                            except Exception as exc:
                                logger.warning(f"Zlyhalo zaradenie delete_draft_imap_task do Celery: {exc}")
                        stary_koncept.delete()
                    except (EmailovaSprava.DoesNotExist, ValueError, TypeError):
                        pass

                try:
                    send_email_smtp_task.delay(ucet.id, to_addr, subject, html_content, cc, bcc, file_data_list)
                except Exception as exc:
                    logger.warning(f"Zlyhalo zaradenie send_email_smtp_task do Celery: {exc}")
                    return JsonResponse({"status": "warning", "message": "⚠️ E-mail bol zaznamenaný do systému, ale zaradenie na odoslanie zlyhalo (výpadok úlohového brokera)."}, status=200)

                return JsonResponse({"status": "success", "message": "E-mail bol úspešne odoslaný."})

            # ==========================================================
            # 4.6 ULOŽENIE KONCEPTU (DRAFTS POD SPRÁVNYM ÚČTOM)
            # ==========================================================
            elif akcia == "save_draft":
                is_reply = data.get('is_reply') == 'true'
                draft_id = data.get('draft_id')
                account_id = data.get('account_id')
                
                if is_reply:
                    povodny_mail = EmailovaSprava.objects.select_related('ucet').get(id=data.get('id'))
                    ucet = povodny_mail.ucet
                    to_addr = povodny_mail.odosielatel
                    subject = f"Re: {povodny_mail.predmet}"
                else:
                    if account_id and str(account_id) != 'ALL':
                        ucet = EmailovyUcet.objects.filter(id=account_id, je_aktivny=True).first()
                    else:
                        ucet = EmailovyUcet.objects.filter(je_aktivny=True).first()
                    to_addr = data.get('to', '')
                    subject = data.get('subject', 'Bez predmetu')

                if not ucet:
                    return JsonResponse({"status": "error", "message": "E-mailový účet pre uloženie konceptu nie je dostupný."}, status=400)
                
                cc = data.get('cc', '')
                bcc = data.get('bcc', '')
                html_content = data.get('html', '')
                
                file_data_list = prepare_attachments_for_celery(files)
                
                # Vygenerujeme ID vopred, aby sme ho mohli poslať na IMAP
                novy_msg_id = f"isas-draft-{timezone.now().timestamp()}@isas.core"
                
                # Databázová operácia musí prebehnúť ako prvá a uspieť aj pri výpadku Redis brokera
                novy_mail = EmailovaSprava.objects.create(
                    ucet=ucet, message_id=novy_msg_id,
                    odosielatel=ucet.email_adresa, prijemca=to_addr, predmet=subject,
                    datum_prijatia=timezone.now(), telo_html=html_content,
                    precitane=True, zlozka='DRAFTS'
                )
                save_attachments_to_db(files, novy_mail, EmailPriloha)

                # Zmazanie starého konceptu zo správnej zložky na IMAPe
                if draft_id:
                    try:
                        stary_koncept = EmailovaSprava.objects.select_related('ucet').get(id=int(draft_id), zlozka='DRAFTS')
                        if stary_koncept.ucet:
                            try:
                                delete_draft_imap_task.delay(stary_koncept.ucet.id, stary_koncept.message_id)
                            except Exception as exc:
                                logger.warning(f"Zlyhalo zaradenie delete_draft_imap_task do Celery: {exc}")
                        stary_koncept.delete()
                    except (EmailovaSprava.DoesNotExist, ValueError, TypeError):
                        pass

                try:
                    save_draft_imap_task.delay(ucet.id, to_addr, subject, html_content, cc, bcc, file_data_list, novy_msg_id)
                except Exception as exc:
                    logger.warning(f"Zlyhalo zaradenie save_draft_imap_task do Celery: {exc}")
                    return JsonResponse({"status": "warning", "message": "⚠️ Koncept bol uložený v databáze, ale synchronizácia na IMAP zlyhala (výpadok úlohového brokera)."}, status=200)

                return JsonResponse({"status": "success", "message": "Koncept bol úspešne uložený."})

            # ==========================================================
            # 5. PRESUN DO KOŠA
            # ==========================================================
            elif akcia == "trash_email":
                mail = EmailovaSprava.objects.select_related('ucet').get(id=data.get('id'))
                mail.zlozka = 'TRASH'
                mail.save(update_fields=['zlozka'])
                
                if mail.ucet:
                    try:
                        trash_imap_email_task.delay(mail.ucet.id, mail.message_id)
                    except Exception as exc:
                        logger.warning(f"Zlyhalo zaradenie trash_imap_email_task do Celery: {exc}")
                
                return JsonResponse({"status": "success", "message": "🗑️ Správa bola presunutá do koša."})

            # ==========================================================
            # 5.5 PRESUN DO ARCHÍVU A SPÄŤ
            # ==========================================================
            elif akcia == "archive_email":
                mail = EmailovaSprava.objects.select_related('ucet').get(id=data.get('id'))
                mail.zlozka = 'ARCHIVE'
                mail.save(update_fields=['zlozka'])
                return JsonResponse({"status": "success", "message": "Správa bola úspešne archivovaná."})

            elif akcia == "unarchive_email":
                mail = EmailovaSprava.objects.select_related('ucet').get(id=data.get('id'))
                mail.zlozka = 'SENT' if (mail.ucet and mail.odosielatel == mail.ucet.email_adresa) else 'INBOX'
                mail.save(update_fields=['zlozka'])
                return JsonResponse({"status": "success", "message": f"Správa bola presunutá do {'Odoslaných' if mail.zlozka == 'SENT' else 'Doručených'}."})

            # ==========================================================
            # 5.6 HROMADNÉ AKCIE (BULK ARCHIVE / UNARCHIVE / TRASH)
            # ==========================================================
            elif akcia == "bulk_archive":
                ids = data.get('ids', [])
                if ids: EmailovaSprava.objects.filter(id__in=ids).update(zlozka='ARCHIVE')
                return JsonResponse({"status": "success"})

            elif akcia == "bulk_unarchive":
                ids = data.get('ids', [])
                if ids:
                    maily = EmailovaSprava.objects.filter(id__in=ids).select_related('ucet')
                    for m in maily:
                        m.zlozka = 'SENT' if (m.ucet and m.odosielatel == m.ucet.email_adresa) else 'INBOX'
                    EmailovaSprava.objects.bulk_update(maily, ['zlozka'])
                return JsonResponse({"status": "success"})

            elif akcia == "bulk_trash":
                ids = data.get('ids', [])
                if ids:
                    # Použijeme .values() namiesto select_related pre bezpečný a rýchly prístup
                    maily_data = EmailovaSprava.objects.filter(id__in=ids).values('ucet_id', 'message_id')
                    
                    # Zoskupíme podľa účtov pre optimalizáciu IMAP volaní
                    ucty_dict = group_emails_by_account(maily_data)
                    
                    # Odoslanie na IMAP server (CELERY TASK)
                    for u_id, msg_ids in ucty_dict.items():
                        try:
                            bulk_trash_imap_task.delay(u_id, msg_ids)
                        except Exception as exc:
                            logger.warning(f"Zlyhalo zaradenie bulk_trash_imap_task do Celery: {exc}")
                    
                    # Bezpečná aktualizácia v DB (čistý QuerySet bez select_related)
                    EmailovaSprava.objects.filter(id__in=ids).update(zlozka='TRASH')

                return JsonResponse({"status": "success"})

            # ==========================================================
            # 5.7 OBNOVENIE A TRVALÉ ZMAZANIE (SINGLE & BULK)
            # ==========================================================
            elif akcia == "restore_email":
                mail = EmailovaSprava.objects.select_related('ucet').get(id=data.get('id'))
                mail.zlozka = 'INBOX'
                mail.save(update_fields=['zlozka'])
                if mail.ucet:
                    try:
                        restore_imap_task.delay(mail.ucet.id, mail.message_id)
                    except Exception as exc:
                        logger.warning(f"Zlyhalo zaradenie restore_imap_task do Celery: {exc}")
                return JsonResponse({"status": "success"})

            elif akcia == "delete_permanently":
                mail = EmailovaSprava.objects.select_related('ucet').get(id=data.get('id'))
                if mail.ucet:
                    try:
                        delete_permanently_imap_task.delay(mail.ucet.id, mail.message_id)
                    except Exception as exc:
                        logger.warning(f"Zlyhalo zaradenie delete_permanently_imap_task do Celery: {exc}")
                EmailovaSprava.objects.filter(id=mail.id).delete()
                return JsonResponse({"status": "success"})

            elif akcia == "bulk_restore":
                ids = data.get('ids', [])
                if ids:
                    maily_data = EmailovaSprava.objects.filter(id__in=ids).values('ucet_id', 'message_id')
                    ucty_dict = group_emails_by_account(maily_data)
                    
                    for u_id, msg_ids in ucty_dict.items():
                        try:
                            bulk_restore_imap_task.delay(u_id, msg_ids)
                        except Exception as exc:
                            logger.warning(f"Zlyhalo zaradenie bulk_restore_imap_task do Celery: {exc}")
                    
                    EmailovaSprava.objects.filter(id__in=ids).update(zlozka='INBOX')
                return JsonResponse({"status": "success"})

            elif akcia == "bulk_delete_permanently":
                ids = data.get('ids', [])
                if ids:
                    maily_data = EmailovaSprava.objects.filter(id__in=ids).values('ucet_id', 'message_id')
                    ucty_dict = group_emails_by_account(maily_data)
                    
                    for u_id, msg_ids in ucty_dict.items():
                        try:
                            bulk_delete_permanently_imap_task.delay(u_id, msg_ids)
                        except Exception as exc:
                            logger.warning(f"Zlyhalo zaradenie bulk_delete_permanently_imap_task do Celery: {exc}")
                    
                    # Fyzické zmazanie z DB (spustí signály pre prílohy)
                    EmailovaSprava.objects.filter(id__in=ids).delete()

                return JsonResponse({"status": "success"})

            # ==========================================================
            # 5.8 VYSYPANIE CELÉHO KOŠA
            # ==========================================================
            elif akcia == "empty_trash":
                maily_v_kosi = EmailovaSprava.objects.filter(zlozka='TRASH')
                
                # Zoskupíme podľa účtov pre optimalizáciu IMAP volaní
                ucty_ids = list(maily_v_kosi.values_list('ucet_id', flat=True).distinct())
                for u_id in ucty_ids:
                    if u_id:
                        try:
                            empty_trash_imap_task.delay(u_id)
                        except Exception as exc:
                            logger.warning(f"Zlyhalo zaradenie empty_trash_imap_task do Celery: {exc}")
                
                # Rýchle, bezpečné a hromadné zmazanie bez pádu na chýbajúcom get_current_user
                maily_v_kosi.delete()
                    
                return JsonResponse({"status": "success", "message": "🔥 Kôš bol úspešne vysypaný."})

            # ==========================================================
            # 5.9 PREPOSLANIE TECHNIKOVI (TELEGRAM / DISCORD)
            # ==========================================================
            elif akcia == "get_technicians":
                # 🛡️ ISAS CORE: Vytiahne VÝHRADNE zamestnancov s rolou 'TE' (Servisný technik),
                # ktorí majú nastavený aspoň jeden komunikačný kanál (Telegram, Discord alebo E-mail).
                technici = Zamestnanec.objects.filter(
                    pracovna_rola='TE',
                    je_anonymizovany=False
                ).filter(
                    Q(telegram_id__isnull=False) | 
                    Q(discord_id__isnull=False) | 
                    (Q(email__isnull=False) & ~Q(email=''))
                ).values('id', 'meno', 'priezvisko')

                zoznam = [{"id": t['id'], "meno": f"{t['meno']} {t['priezvisko']}"} for t in technici]
                return JsonResponse({"status": "success", "technici": zoznam})

            elif akcia == "forward_to_technician":
                mail_id_raw = data.get('mail_id')
                technik_id_raw = data.get('technik_id')
                poznamka = data.get('poznamka', '')
                
                try:
                    mail_id = int(mail_id_raw)
                    technik_id = int(technik_id_raw)
                except (ValueError, TypeError):
                    return JsonResponse({"status": "error", "message": "⚠️ Neplatný identifikátor správy alebo technika."}, status=400)
                
                try:
                    forward_mail_to_technician_task.delay(mail_id, technik_id, poznamka)
                except Exception as exc:
                    logger.warning(f"Zlyhalo zaradenie forward_mail_to_technician_task do Celery: {exc}")
                    return JsonResponse({"status": "error", "message": "⚠️ E-mail sa nepodarilo odoslať technikovi (výpadok úlohového servera)."}, status=503)
                
                return JsonResponse({"status": "success", "message": "✈️ E-mail bol technikovi úspešne odoslaný."})

            # ==========================================================
            # 6. ELITE FEATURE: ODOVZDANIE FAKTÚRY S VÝCHOVNÝM E-MAILOM
            # ==========================================================
            elif akcia == "forward_to_invoices":
                raw_att_id = data.get('attachment_id')
                try:
                    attachment_id = int(raw_att_id)
                except (ValueError, TypeError):
                    return JsonResponse({"status": "error", "message": "⚠️ Neplatný identifikátor prílohy."}, status=400)
                
                unikatna_poznamka = f"⚠️ Ručne presmerované z ISAS CORE (Príloha ID: {attachment_id})"
                if PrichodziaFaktura.objects.filter(poznamka=unikatna_poznamka).exists():
                    return JsonResponse({"status": "error", "message": "⚠️ Táto faktúra už bola odovzdaná do evidencie."}, status=400)

                # 🚀 100% BEZPEČNÉ A ASYNCHRÓNNE: Garantovaný int primitívny typ pre Celery
                try:
                    forward_invoice_to_evidencia_task.delay(attachment_id)
                except Exception as exc:
                    logger.warning(f"Zlyhalo zaradenie forward_invoice_to_evidencia_task do Celery: {exc}")
                    return JsonResponse({"status": "error", "message": "⚠️ Spracovanie faktúry sa nepodarilo zaradiť do fronty (výpadok brokera)."}, status=503)
                
                return JsonResponse({
                    "status": "success", 
                    "message": "✅ Faktúra bola zaradená na spracovanie a dodávateľ bude informovaný."
                })

        except EmailovaSprava.DoesNotExist:
            return JsonResponse({"status": "error", "message": "🔍 Požadovaný e-mail sa nenašiel. Možno bol medzitým vymazaný."}, status=404)
        except EmailPriloha.DoesNotExist:
            return JsonResponse({"status": "error", "message": "📎 Požadovaná príloha sa nenašla."}, status=404)
        except Exception as e:
            logger.error(f"API Error: {str(e)}")
            return JsonResponse({"status": "error", "message": "🚨 Vyskytla sa neočakávaná chyba pri spracovaní požiadavky."}, status=400)

    return JsonResponse({"status": "invalid"}, status=400)

@staff_member_required
def crm_intel_api(request):
    """
    API pre Dashboard: Zistí dnešné meniny (SK/CZ) a narodeniny v databáze.
    100% OFFLINE ENGINE - Žiadne závislosti na cudzích API.
    """
    dnes = timezone.localdate() if getattr(settings, 'USE_TZ', False) else timezone.now().date()

    den_mesiac = dnes.strftime("%d.%m.")
    je_nepriestupny_posledny_februar = (
        dnes.month == 2 and dnes.day == 28 and (dnes.year % 4 != 0 or (dnes.year % 100 == 0 and dnes.year % 400 != 0))
    )

    # 👑 Čítame priamo z našej lokálnej databázy (0 ms latency)
    dnesne_meniny_raw = MENINY_KALENDAR.get(den_mesiac, {"sk": "", "cz": ""})
    dnesne_meniny = dict(dnesne_meniny_raw) if isinstance(dnesne_meniny_raw, dict) else {"sk": "", "cz": ""}
    if je_nepriestupny_posledny_februar:
        meniny_29 = MENINY_KALENDAR.get("29.02.", {})
        if isinstance(meniny_29, dict):
            for k, v in meniny_29.items():
                if v:
                    povodna_hodnota = dnesne_meniny.get(k, "")
                    dnesne_meniny[k] = f"{povodna_hodnota}, {v}".strip(", ") if povodna_hodnota else v
    sviatky_blacklist = {
        "nový rok", "sviatok práce", "svátek práce", "státní svátek",
        "tři králové", "upálení mistra jana husa", "pamiatka zosnulých",
        "1. sviatok vianočný", "boží hod vánoční"
    }

    def parsuj_mena(text_meniny):
        mena = []
        for cast in str(text_meniny or "").split(","):
            polozka = cast.strip()
            if not polozka or polozka.lower() in sviatky_blacklist:
                continue
            # Defenzívne rozdelenie českých zložených mien so spojkou "a" (Petr a Pavel, Adam a Eva, Cyril a Metoděj)
            if " a " in polozka and not any(s in polozka.lower() for s in ["sviatok", "svátek", "hod"]):
                for sub in polozka.split(" a "):
                    sub_clean = sub.strip()
                    if sub_clean and sub_clean.lower() not in sviatky_blacklist:
                        mena.append(sub_clean)
            else:
                mena.append(polozka)
        return mena

    dnesne_meniny_dict = dnesne_meniny if isinstance(dnesne_meniny, dict) else {}
    mena_podla_krajiny = {
        str(k).lower(): parsuj_mena(v)
        for k, v in dnesne_meniny_dict.items()
    }
    sk_mena_list = mena_podla_krajiny.get("sk", [])
    cz_mena_list = mena_podla_krajiny.get("cz", [])
    sk_meno = str(dnesne_meniny_dict.get("sk") or "").strip() or "Neznáme"
    cz_meno = str(dnesne_meniny_dict.get("cz") or "").strip() or "Neznámé"

    zhody = []

    # --- INTELIGENTNÉ POMOCNÉ FUNKCIE ---
    def get_osoba_meno(obj):
        if getattr(obj, 'kontaktna_osoba', None):
            return str(obj.kontaktna_osoba).strip()
        if getattr(obj, 'meno_a_priezvisko', None):
            return str(obj.meno_a_priezvisko).strip()
        cele_meno = f"{getattr(obj, 'meno', '') or ''} {getattr(obj, 'priezvisko', '') or ''}".strip()
        return cele_meno if cele_meno else "Neznáma osoba"

    def get_subjekt_text(obj, typ_subjektu):
        nazov = getattr(obj, 'Nazov_spolocnosti', None) or getattr(obj, 'nazov_spolocnosti', None)
        if nazov:
            return f"{typ_subjektu}: {str(nazov).strip()}"
        if getattr(obj, 'pracovna_rola', None):
            rola_display = obj.get_pracovna_rola_display() if hasattr(obj, 'get_pracovna_rola_display') else obj.pracovna_rola
            if rola_display:
                return f"{typ_subjektu} ({rola_display})"
        return typ_subjektu

    def format_roky(vek):
        try:
            v = max(0, int(vek or 0))
        except (ValueError, TypeError):
            v = 0
        if v == 1: return "1 rok"
        elif 2 <= v <= 4: return f"{v} roky"
        else: return f"{v} rokov"

    def get_flag(iso_kod):
        c = str(iso_kod or "").strip().upper()
        if len(c) == 2 and c.isalpha():
            return "".join(chr(127397 + ord(char)) for char in c)
        return "🎉"

    # --- HLAVNÝ VYHĽADÁVACÍ ENGINE ---
    # 🏛️ Natívne načítanie domáceho ISO kódu z FiremnaIdentita bez manuálneho parsovania
    domaci_iso_raw = FiremnaIdentita.get_aktivna_krajina_iso() if hasattr(FiremnaIdentita, 'get_aktivna_krajina_iso') else ""
    domaci_iso = domaci_iso_raw.lower() if domaci_iso_raw and domaci_iso_raw not in ('NEZADANE', 'INYKRAJINA') else ""
    dostupne_kalendare = set(dnesne_meniny_dict.keys())

    # Dynamické prepojenie na model StatLokalizacia
    try:
        StatLokalizaciaModel = apps.get_model('ISASaplikacia', 'StatLokalizacia')
    except Exception:
        StatLokalizaciaModel = None

    def najdi_zhody(model_class, typ_subjektu, pole_pre_meniny):
        try:
            model_fields = {f.name for f in model_class._meta.concrete_fields}
        except Exception:
            model_fields = set()

        # 1. Hľadáme narodeniny (defenzívna ochrana stĺpca a ošetrenie 29. 2. v nepriestupných rokoch)
        if "datum_narodenia" in model_fields:
            je_nepriestupny_posledny_februar = (
                dnes.month == 2 and dnes.day == 28 and (dnes.year % 4 != 0 or (dnes.year % 100 == 0 and dnes.year % 400 != 0))
            )
            q_narodeniny = (
                (Q(datum_narodenia__month=2, datum_narodenia__day=28) | Q(datum_narodenia__month=2, datum_narodenia__day=29))
                if je_nepriestupny_posledny_februar
                else Q(datum_narodenia__month=dnes.month, datum_narodenia__day=dnes.day)
            )
            for obj in model_class.objects.filter(q_narodeniny):
                dn = getattr(obj, 'datum_narodenia', None)
                if not dn or not hasattr(dn, 'year'):
                    continue
                vek = dnes.year - dn.year

                if vek < 0:
                    continue
                zhody.append({
                    "typ": "🎂 Narodeniny",
                    "subjekt": get_subjekt_text(obj, typ_subjektu),
                    "osoba": get_osoba_meno(obj),
                    "detail": format_roky(vek)
                })

        # 2. Hľadáme meniny (defenzívna ochrana existencie stĺpca pred FieldError 500)
        if pole_pre_meniny not in model_fields:
            return

        vsetky_mena = [
            m.strip()
            for zoznam in mena_podla_krajiny.values()
            for m in zoznam
            if m and str(m).strip()
        ]
        vsetky_mena = list(dict.fromkeys(vsetky_mena))
        if not vsetky_mena:
            return

        # Spojíme všetky dnešné mená do jedného SQL dopytu (ochrana pred prázdnym stringom spôsobujúcim full-table scan)
        q_mena = Q()
        for m in vsetky_mena:
            if m:
                q_mena |= Q(**{f"{pole_pre_meniny}__icontains": m})

        if not q_mena:
            return

        spracovane_osoby_ids = set()

        for obj in model_class.objects.filter(q_mena):
            if obj.pk in spracovane_osoby_ids:
                continue

            hladany_text = str(getattr(obj, pole_pre_meniny, "") or "").strip()
            if not hladany_text:
                continue

            # 🛡️ DYNAMICKÁ MEDZINÁRODNÁ POISTKA (0 riadkov hardkódu, využitie StatLokalizacia.rozpoznaj_iso)
            stat_obj_raw = str(getattr(obj, 'stat', '') or getattr(obj, 'krajina', '') or '').strip()
            if stat_obj_raw and StatLokalizaciaModel and hasattr(StatLokalizaciaModel, 'rozpoznaj_iso'):
                iso_obj = StatLokalizaciaModel.rozpoznaj_iso(stat_obj_raw)
                subjekt_iso = iso_obj.lower() if iso_obj and iso_obj not in ('NEZADANE', 'INYKRAJINA') else ""
            else:
                subjekt_iso = domaci_iso

            # Ak má subjekt štát, pre ktorý nie je v DB kalendár menín (napr. AT, DE), ignorujeme ho
            if subjekt_iso and subjekt_iso not in dostupne_kalendare:
                continue

            # Určenie relevantných mien pre daný subjekt dynamicky podľa štátu
            cielove_mena = mena_podla_krajiny.get(subjekt_iso) if (subjekt_iso and subjekt_iso in mena_podla_krajiny) else vsetky_mena

            for m in cielove_mena:
                if re.search(rf'\b{re.escape(m)}\b', hladany_text, re.IGNORECASE):
                    kalendar_kod = subjekt_iso if subjekt_iso in dostupne_kalendare else next((k for k, v in mena_podla_krajiny.items() if m in v), domaci_iso)
                    vlajka = get_flag(kalendar_kod)

                    zhody.append({
                        "typ": f"{vlajka} Meniny ({m})",
                        "subjekt": get_subjekt_text(obj, typ_subjektu),
                        "osoba": get_osoba_meno(obj),
                        "detail": ""
                    })
                    spracovane_osoby_ids.add(obj.pk)
                    break

    # Spustenie
    najdi_zhody(Zakaznik, "Zákazník", "kontaktna_osoba")
    najdi_zhody(Dodavatel, "Dodávateľ", "kontaktna_osoba")
    najdi_zhody(Zamestnanec, "Zamestnanec", "meno")
    najdi_zhody(SukromnaOsoba, "Súkromná osoba", "meno_a_priezvisko")

    # 🚀 DYNAMICKÉ ZORADENIE (Narodeniny prvé, potom domáci trh podľa sídla firmy, potom ostatné)
    domaca_vlajka = get_flag(domaci_iso) if domaci_iso else ""

    def sort_key(item):
        typ = item.get("typ", "")
        if "Narodeniny" in typ: return 0
        if domaca_vlajka and domaca_vlajka in typ: return 1
        return 2
    
    zhody.sort(key=sort_key)

    return JsonResponse({
        "dnes_sk": sk_meno,
        "dnes_cz": cz_meno,
        "zhody": zhody
    })

@staff_member_required
def api_live_radar(request):
    """
    ISAS CORE: Live Dispatch Radar API (Enriched with Fleet & Task Data)
    """
    dnes = timezone.now().date()
    aktualny_cas = time.time()

    TIMEOUT_SEKUNDY = 900  # 15 minút

    # 1. EXTRÉMNA OPTIMALIZÁCIA (N+1 Query Protection)
    zaciatok_dna = datetime.datetime.combine(dnes, datetime.time.min)
    koniec_dna = zaciatok_dna + datetime.timedelta(days=1)

    technici_db = Zamestnanec.objects.filter(pracovna_rola='TE').prefetch_related(
        Prefetch('vozidlo_set', queryset=Vozidlo.objects.all(), to_attr='priradene_auta'),
        Prefetch(
            'uloha_set', 
            # Použitie __gte a __lt namiesto __date. 
            # Toto donúti PostgreSQL použiť B-Tree index na poli 'zaciatok'. Rýchlosť stúpne o 1000%.
            queryset=Uloha.objects.filter(zaciatok__gte=zaciatok_dna, zaciatok__lt=koniec_dna).select_related('zakaznik').prefetch_related(
                'typ_servisu', 'zariadenia', 'povolenienavstup_set', 'servisne_zaznamy', 'pouzite_polozky__content_object'
            ).order_by('zaciatok'), 
            to_attr='dnesne_ulohy'
        )
    )

    # Hromadný výpočet dlhov a upsellu pre dnešných zákazníkov (0 extra queries v cykle)
    dnesne_ulohy_zakaznici_ids = [u.zakaznik_id for t in technici_db for u in t.dnesne_ulohy if u.zakaznik_id]
    
    # A. Finančné riziko (Dlhy po splatnosti)
    dlhy_db = VystavenaFaktura.objects.filter(
        zakaznik_id__in=dnesne_ulohy_zakaznici_ids, stav='PO_SPLATNOSTI'
    ).values('zakaznik_id').annotate(celkovy_dlh=Sum('celkom_s_dph'))
    # Ochrana pred TypeError, ak Sum() vráti None (napr. pri poškodených dátach faktúry)
    mapa_dlhov = {d['zakaznik_id']: float(d['celkovy_dlh'] or 0.0) for d in dlhy_db}

    # B. Upsell potenciál (Plánované servisy na najbližších 30 dní)
    buduci_mesiac = dnes + datetime.timedelta(days=30)
    upsell_db = PlanovanyServisZariadenia.objects.filter(
        zariadenie__zakaznik_id__in=dnesne_ulohy_zakaznici_ids,
        planovany_datum__lte=buduci_mesiac,
        planovany_datum__gte=dnes,
        upozornenie_odoslane=False
    ).values('zariadenie__zakaznik_id').annotate(pocet=Count('id'))
    mapa_upsellu = {u['zariadenie__zakaznik_id']: u['pocet'] for u in upsell_db}

    response_data = {
        'active': [],
        'refused': [],
        'offline': []
    }

    # Bulk Cache & Bulk DB fetch pred cyklom (Ochrana pred N+1)
    cache_keys = [f'isas_radar_tech_{t.id}' for t in technici_db]
    
    # Ochrana pred pádom Redis servera. 
    # Ak Redis zlyhá, systém nespadne (Error 500), ale plynule pokračuje ďalej.
    try:
        bulk_cache = cache.get_many(cache_keys)
    except Exception as e:
        logging.getLogger(__name__).error(f"Kritická chyba: Redis Cache neodpovedá! {e}")
        bulk_cache = {}  # Fallback: Dispečer uvidí úlohy a technikov, len bez Live GPS polohy
    
    # Window funkcia stiahne z DB presne len 100 najnovších stôp pre každého technika.
    # Zabraňuje stiahnutiu desiatok tisíc záznamov do RAM vo večerných hodinách.
    vsetky_stopy = GPSStopa.objects.filter(
        zamestnanec__in=technici_db, timestamp__gte=zaciatok_dna
    ).annotate(
        row_number=Window(
            expression=RowNumber(),
            partition_by=[F('zamestnanec_id')],
            order_by=F('timestamp').desc()
        )
    ).values('zamestnanec_id', 'lat', 'lon', 'row_number')
    
    stopy_mapa = {}
    for stopa in vsetky_stopy:
        if stopa['row_number'] <= 100:
            stopy_mapa.setdefault(stopa['zamestnanec_id'], []).append((stopa['lat'], stopa['lon']))

    for technik in technici_db:
        tg_id = str(technik.id)
        cele_meno = f"{technik.meno} {technik.priezvisko}"
        telefon = technik.telefon if technik.telefon else ""

        # --- A. SPRACOVANIE VOZIDLA A ALERTOV ---
        auto_data = None
        if technik.priradene_auta:
            auto = technik.priradene_auta[0] # Berieme prvé priradené auto
            
            # 🟢 UPRIMNÝ FIX: Ochrana pred TypeError, ak má auto nevyplnené KM (None)
            akt_km = auto.aktualny_stav_km or 0
            posl_olej = auto.posledny_olej_km or 0
            int_olej = auto.olej_interval_km or 15000 # Fallback na štandardný interval
            
            # Výpočet oleja
            km_do_oleja = int_olej - (akt_km - posl_olej)
            olej_alert = km_do_oleja <= 1000

            # Výpočet STK, Diaľničnej známky a PZP
            stk_alert = False
            if auto.stk_plati_do:
                stk_alert = (auto.stk_plati_do - dnes).days <= 14
                
            dialnica_alert = False
            if auto.dialnicna_znamka_do:
                dialnica_alert = (auto.dialnicna_znamka_do - dnes).days <= 7
                
            pzp_alert = False
            if auto.pzp_plati_do:
                pzp_alert = (auto.pzp_plati_do - dnes).days <= 14
                
            kasko_alert = False
            if auto.kasko_plati_do:
                kasko_alert = (auto.kasko_plati_do - dnes).days <= 14
                
            lekarnicka_alert = False
            if auto.lekarnicka_plati_do:
                lekarnicka_alert = (auto.lekarnicka_plati_do - dnes).days <= 30

            auto_data = {
                'spz': auto.spz,
                'model': auto.znacka_a_model,
                'aktualny_stav_km': auto.aktualny_stav_km,
                'olej_alert': olej_alert,
                'km_do_oleja': km_do_oleja,
                'stk_alert': stk_alert,
                'stk_datum': auto.stk_plati_do.strftime('%d.%m.%Y') if auto.stk_plati_do else "",
                'dialnica_alert': dialnica_alert,
                'pzp_alert': pzp_alert,
                'kasko_alert': kasko_alert,
                'lekarnicka_alert': lekarnicka_alert,
                'pneumatiky': auto.aktualne_pneumatiky
            }

        # --- B. SPRACOVANIE ÚLOH A EFEKTIVITY ---
        ulohy = technik.dnesne_ulohy
        celkom_uloh = len(ulohy)
        hotove_ulohy = sum(1 for u in ulohy if u.dokoncene)
        
        aktualna_uloha_data = None
        # Nájdeme prvú nedokončenú úlohu
        aktualna_uloha = next((u for u in ulohy if not u.dokoncene), None)
        
        if aktualna_uloha:
            # Extrakcia typov prác s ohľadom na prioritu a normohodiny
            typy_prac_list = list(aktualna_uloha.typ_servisu.all())
            typy_prac = ", ".join([t.nazov for t in typy_prac_list if t.nazov])
            
            # ISAS ARCHITECT FIX: Zariadenie musí obsahovať aj model a S/N pre presnú identifikáciu
            zariadenia_prac_list = []
            for z in aktualna_uloha.zariadenia.all():
                nazov_z = z.nazov or z.model_zariadenia or "Neznáme zariadenie"
                sn_z = f" (SN: {z.vyrobne_cislo})" if z.vyrobne_cislo and z.vyrobne_cislo != "Nezadané" else ""
                loc_z = f" [📍 {z.umiestnenie}]" if z.umiestnenie else ""
                zariadenia_prac_list.append(f"{nazov_z}{sn_z}{loc_z}")
            zariadenia_prac = ", ".join(zariadenia_prac_list)
            
            # Biznis logika: Výpočet celkového odhadovaného času a detekcia kritickej priority
            # Ochrana pred TypeError, ak má typ práce nevyplnené normohodiny (None)
            celkove_normohodiny = sum((t.normohodiny or 0.0) for t in typy_prac_list)
            je_kriticka = any(t.priorita == 'VYSOKA' for t in typy_prac_list)
            
            # Výpočet dní do konca reklamácie (30 dňová lehota)
            dni_do_konca_reklamacie = None
            if aktualna_uloha.je_reklamacia and aktualna_uloha.datum_prijatia_reklamacie:
                d_reklamacie = (
                    aktualna_uloha.datum_prijatia_reklamacie.date()
                    if hasattr(aktualna_uloha.datum_prijatia_reklamacie, 'date')
                    else aktualna_uloha.datum_prijatia_reklamacie
                )
                koniec_lehoty = d_reklamacie + datetime.timedelta(days=30)
                dni_do_konca_reklamacie = (koniec_lehoty - dnes).days

            # Detailné dáta o zákazníkovi pre dispečera
            zakaznik_data = None
            if aktualna_uloha.zakaznik:
                z = aktualna_uloha.zakaznik
                adresa = f"{z.ulica_a_cislo or ''}, {z.mesto or ''}".strip(', ')
                
                # Zistenie stavu povolenia na vstup pre danú úlohu (bez N+1 vďaka prefetch)
                povolenia = aktualna_uloha.povolenienavstup_set.all()
                stav_povolenia = povolenia[0].stav if povolenia else None

                zakaznik_data = {
                    'nazov': z.Nazov_spolocnosti,
                    'adresa': adresa if adresa else "Adresa neznáma",
                    'kontakt_osoba': z.kontaktna_osoba,
                    'telefon': z.telefon,
                    'gate_pass': z.vyzaduje_povolenie_vstupu,
                    'telefon_vratnica': z.telefon_vratnica,
                    'stav_povolenia': stav_povolenia,
                    'instrukcie_vstupu': z.instrukcie_vstupu
                }

            # Zistenie stavu servisného protokolu (Real-time sledovanie práce)
            zaznamy = aktualna_uloha.servisne_zaznamy.all()
            stav_protokolu = None
            if zaznamy:
                # Ak existuje aspoň jeden neuzavretý protokol, technik ho práve vypisuje
                if any(not z.protokol_uzavrety for z in zaznamy):
                    stav_protokolu = 'VYPISUJE'
                else:
                    stav_protokolu = 'UZAVRETY'

            # Kontrola záruky a prenájmu
            je_v_zaruke = False
            je_prenajom = False
            for zar in aktualna_uloha.zariadenia.all():
                if zar.v_prenajme_od_isas:
                    je_prenajom = True
                # 🛡️ ISAS INDUSTRIAL: Rešpektuje štandardnú aj predĺženú záruku cez modelovú property
                if getattr(zar, 'je_v_zaruke', False):
                    je_v_zaruke = True

            # Kontrola chýbajúceho materiálu (JIT Sklad)
            chyba_material = False
            chybajuce_polozky_text = []
            for mat in aktualna_uloha.pouzite_polozky.all():
                if mat.content_object and hasattr(mat.content_object, 'pocet_na_sklade'):
                    skladom = mat.content_object.pocet_na_sklade or 0
                    potrebne = mat.pocet or 0
                    if skladom < potrebne:
                        chyba_material = True
                        chybajuce_polozky_text.append(f"{mat.content_object} (Chýba: {potrebne - skladom} ks)")

            # Detekcia meškania (Ak je aktuálny čas väčší ako plánovaný začiatok a protokol nie je uzavretý)
            meska = False
            if aktualna_uloha.zaciatok and stav_protokolu != 'UZAVRETY':
                teraz = timezone.now()
                cas_zaciatku = aktualna_uloha.zaciatok

                # 🛡️ Zjednotenie naive/aware formátov (ochrana pred TypeError pri akejkoľvek konfigurácii USE_TZ)
                if timezone.is_aware(teraz) and timezone.is_naive(cas_zaciatku):
                    cas_zaciatku = timezone.make_aware(cas_zaciatku, timezone.get_current_timezone())
                elif timezone.is_naive(teraz) and timezone.is_aware(cas_zaciatku):
                    cas_zaciatku = timezone.make_naive(cas_zaciatku, timezone.get_current_timezone())

                if cas_zaciatku < teraz:
                    meska = True

            aktualna_uloha_data = {
                'id': aktualna_uloha.id,
                'zakaznik_id': aktualna_uloha.zakaznik_id,
                'zakaznik': zakaznik_data,
                'dlh_po_splatnosti': mapa_dlhov.get(aktualna_uloha.zakaznik_id, 0.0),
                'upsell_pocet': mapa_upsellu.get(aktualna_uloha.zakaznik_id, 0),
                'stav_protokolu': stav_protokolu,
                'je_v_zaruke': je_v_zaruke,
                'je_prenajom': je_prenajom,
                'chyba_material': chyba_material,
                'chybajuce_polozky': ", ".join(chybajuce_polozky_text),
                'typ': typy_prac if typy_prac else "Všeobecný servis",
                'zariadenia': zariadenia_prac if zariadenia_prac else "Nešpecifikované zariadenie",
                'blizsie_informacie': aktualna_uloha.blizsie_informacie,
                'je_reklamacia': aktualna_uloha.je_reklamacia,
                'dni_reklamacie': dni_do_konca_reklamacie,
                'cas': aktualna_uloha.zaciatok.strftime('%H:%M'),
                'meska': meska,
                'normohodiny': float(celkove_normohodiny),
                'je_kriticka': je_kriticka
            }

        # --- C. ZOSTAVENIE FINÁLNEHO OBJEKTU ---
        bozp_alert = False
        if technik.bozp_platne_do:
            bozp_alert = (technik.bozp_platne_do - dnes).days <= 14

        technik_info = {
            'id': technik.id,
            'meno': cele_meno,
            'telefon': telefon,
            'vyhlaska': getattr(technik.certifikaty.first(), 'nazov', '') or '',
            'bozp_alert': bozp_alert,
            'ma_lekarsku': bool(technik.lekarska_prehliadka_subor),
            'typ_pomeru': technik.typ_pomeru, # Dôležité pre nákladovosť
            'auto': auto_data,
            'ulohy_celkom': celkom_uloh,
            'ulohy_hotovo': hotove_ulohy,
            'aktualna_uloha': aktualna_uloha_data,
            'lat': None,
            'lon': None,
            'trasa': []
        }

        # --- D. SPÁROVANIE S GPS CACHE ---
        cache_zaznam = bulk_cache.get(f'isas_radar_tech_{tg_id}')

        # Zistíme, či technik nehlási núdzu
        is_sos_active = cache_zaznam and cache_zaznam.get('status') == 'SOS'

        # Ak má vypnuté zdieľanie a NIE JE v SOS režime, hodíme ho do OFFLINE
        if not getattr(technik, 'zdiela_polohu', True) and not is_sos_active:
            technik_info['dovod'] = 'Sledovanie vypnuté v nastaveniach'
            response_data['offline'].append(technik_info)
            continue # Preskočíme ďalšie spracovanie pre tohto technika

        if cache_zaznam:
            try:
                posledny_ping = float(cache_zaznam.get('timestamp') or 0)
            except (ValueError, TypeError):
                posledny_ping = 0.0
            vek_zaznamu = max(0.0, aktualny_cas - posledny_ping)

            status_cache = cache_zaznam.get('status')
            is_sos = (status_cache == 'SOS')

            if status_cache == 'REFUSED':
                technik_info['dovod'] = 'Sledovanie vypnuté'
                response_data['refused'].append(technik_info)
                
            # Ak je SOS, ignorujeme timeout. Technik v núdzi musí svietiť na mape navždy, kým to nezruší!
            elif (status_cache == 'ACTIVE' and vek_zaznamu <= TIMEOUT_SEKUNDY) or is_sos:
                technik_info['is_sos'] = is_sos
                
                # Dynamické získanie núdzových kontaktov
                if is_sos:
                    nudzove_kontakty = {
                        'technik': {'meno': cele_meno, 'telefon': telefon},
                        'kolegovia': [],
                        'zakaznik': None
                    }
                    kolegovia_ids = set()

                    # 1. Kolegovia na rovnakej úlohe
                    if aktualna_uloha:
                        for kol in aktualna_uloha.technici.all():
                            if kol.id != technik.id and kol.telefon:
                                nudzove_kontakty['kolegovia'].append({
                                    'meno': f"{kol.meno} {kol.priezvisko}",
                                    'telefon': kol.telefon,
                                    'dovod': 'Spolu na úlohe'
                                })
                                kolegovia_ids.add(kol.id)
                        
                        # Zákazník a vrátnica
                        if aktualna_uloha.zakaznik:
                            z = aktualna_uloha.zakaznik
                            nudzove_kontakty['zakaznik'] = {
                                'nazov': z.Nazov_spolocnosti,
                                'osoba': z.kontaktna_osoba or 'Zákazník',
                                'telefon': z.telefon,
                                'vratnica': z.telefon_vratnica
                            }

                    # 2. Kolegovia v rovnakom aute (Evidencia Jázd)
                    # Hľadáme aktívnu jazdu práve v tejto sekunde, kde je technik vodič alebo spolujazdec
                    teraz_aware = timezone.now()
                    aktivna_jazda = EvidenciaJazd.objects.filter(
                        Q(zamestnanec=technik) | Q(spolujazdci=technik),
                        datum_cas_start__lte=teraz_aware
                    ).filter(
                        Q(datum_cas_koniec__isnull=True) | Q(datum_cas_koniec__gte=teraz_aware)
                    ).prefetch_related('spolujazdci', 'zamestnanec').first()

                    if aktivna_jazda:
                        # Ak je vodič niekto iný
                        if aktivna_jazda.zamestnanec and aktivna_jazda.zamestnanec.id != technik.id:
                            if aktivna_jazda.zamestnanec.id not in kolegovia_ids and aktivna_jazda.zamestnanec.telefon:
                                nudzove_kontakty['kolegovia'].append({
                                    'meno': f"{aktivna_jazda.zamestnanec.meno} {aktivna_jazda.zamestnanec.priezvisko}",
                                    'telefon': aktivna_jazda.zamestnanec.telefon,
                                    'dovod': 'Vodič vozidla'
                                })
                                kolegovia_ids.add(aktivna_jazda.zamestnanec.id)
                        
                        # Ak sú tam iní spolujazdci
                        for spol in aktivna_jazda.spolujazdci.all():
                            if spol.id != technik.id and spol.id not in kolegovia_ids and spol.telefon:
                                nudzove_kontakty['kolegovia'].append({
                                    'meno': f"{spol.meno} {spol.priezvisko}",
                                    'telefon': spol.telefon,
                                    'dovod': 'Spolujazdec'
                                })
                                kolegovia_ids.add(spol.id)

                    technik_info['nudzove_kontakty'] = nudzove_kontakty

                lat = cache_zaznam.get('lat')
                lon = cache_zaznam.get('lon')
                
                # Ak chýba GPS v RAM (napr. po zrušení SOS alebo reštarte), VŽDY vytiahneme poslednú známu z DB!
                if not lat or not lon:
                    posledna_stopa = GPSStopa.objects.filter(zamestnanec=technik).order_by('-timestamp').first()
                    if posledna_stopa:
                        lat = posledna_stopa.lat
                        lon = posledna_stopa.lon
                        if is_sos: technik_info['dovod'] = '⚠️ SOS: Posledná známa poloha z DB!'
                    else:
                        # Ak v živote neposlal polohu, použijeme polohu dnešnej úlohy alebo bezpečný None fallback
                        if aktualna_uloha and aktualna_uloha.zakaznik and aktualna_uloha.zakaznik.lat and aktualna_uloha.zakaznik.lon:
                            lat, lon = aktualna_uloha.zakaznik.lat, aktualna_uloha.zakaznik.lon
                        else:
                            lat, lon = None, None
                        if is_sos: technik_info['dovod'] = '⚠️ SOS: Poloha absolútne neznáma!'

                technik_info['lat'] = lat
                technik_info['lon'] = lon
                technik_info['vek_minut'] = int(vek_zaznamu / 60)
                
                # 📍Čítanie z prednačítanej RAM mapy (0 extra DB queries)
                posledne_stopy = stopy_mapa.get(technik.id, [])[:100]
                
                trasa = list(reversed(posledne_stopy))
                # Ochrana pred pádom Leaflet mapy (ak by lat/lon boli náhodou None)
                if technik_info.get('lat') and technik_info.get('lon'):
                    trasa.append((technik_info['lat'], technik_info['lon']))
                technik_info['trasa'] = trasa
                
                # Výpočet vzdialenosti k zákazníkovi
                if technik_info.get('lat') and technik_info.get('lon') and aktualna_uloha and aktualna_uloha.zakaznik and aktualna_uloha.zakaznik.lat and aktualna_uloha.zakaznik.lon:
                    vzdialenost = vypocitaj_vzdialenost_metre(
                        technik_info['lat'], technik_info['lon'], 
                        aktualna_uloha.zakaznik.lat, aktualna_uloha.zakaznik.lon
                    )
                    if vzdialenost is not None:
                        technik_info['aktualna_uloha']['vzdialenost_metre'] = int(vzdialenost)
                        technik_info['aktualna_uloha']['je_na_mieste'] = vzdialenost <= 200 # Okruh 200 metrov
                        
                        # Heuristický výpočet času príchodu (bez externých API)
                        # Počítame len ak je vzdialenosť medzi 200m a 800km (Ochrana pred GPS anomáliami)
                        if 200 < vzdialenost < 800000:
                            # Vzdušná vzdialenosť * 1.3 (priemerná kľukatosť SK/CZ) / 12.5 m/s (priemerná rýchlosť 45 km/h)
                            eta_sekundy = (vzdialenost * 1.3) / 12.5
                            technik_info['aktualna_uloha']['eta_minuty'] = int(eta_sekundy / 60)
                
                response_data['active'].append(technik_info)
                
            else:
                # Technik stratil signál (OFFLINE)
                technik_info['dovod'] = 'Strata GPS signálu'
                if cache_zaznam.get('lat') and cache_zaznam.get('lon'):
                    technik_info['lat'] = cache_zaznam.get('lat')
                    technik_info['lon'] = cache_zaznam.get('lon')
                    technik_info['vek_minut'] = int(vek_zaznamu / 60)
                    
                    # Extrémne rýchly dopyt (OFFLINE)
                    posledne_stopy = GPSStopa.objects.filter(
                        zamestnanec=technik, 
                        timestamp__gte=zaciatok_dna
                    ).order_by('-timestamp').values_list('lat', 'lon')[:100]
                    
                    trasa = list(reversed(posledne_stopy))
                    if technik_info.get('lat') and technik_info.get('lon'):
                        trasa.append((technik_info['lat'], technik_info['lon']))
                    technik_info['trasa'] = trasa
                    
                response_data['offline'].append(technik_info)
        else:
            technik_info['dovod'] = 'Neaktívny v systéme'
            response_data['offline'].append(technik_info)

    # Pridanie lokácií dnešných úloh (zákazníkov) na mapu
    response_data['tasks_locations'] = []
    # Použijeme set, aby sme jedného zákazníka nepridali 5x, ak tam má dnes 5 úloh
    pridani_zakaznici = set() 
    
    for technik in technici_db:
        for uloha in technik.dnesne_ulohy:
            if uloha.zakaznik and uloha.zakaznik.id not in pridani_zakaznici:
                if uloha.zakaznik.lat and uloha.zakaznik.lon:
                    response_data['tasks_locations'].append({
                        'id': uloha.zakaznik.id,
                        'nazov': uloha.zakaznik.Nazov_spolocnosti,
                        'lat': uloha.zakaznik.lat,
                        'lon': uloha.zakaznik.lon,
                        'mesto': uloha.zakaznik.mesto,
                        'dokoncene': uloha.dokoncene,
                        'domena': getattr(uloha.zakaznik, 'domena', None)
                    })
                    pridani_zakaznici.add(uloha.zakaznik.id)

    return JsonResponse(response_data)

@staff_member_required
def api_history_radar(request):
    """⏱️ ISAS TIME MACHINE: API pre prehrávanie histórie pohybu technikov"""
    date_str = request.GET.get('date')
    if not date_str:
        return JsonResponse({'error': 'Chýba dátum.'}, status=400)
    
    try:
        target_date = parse_date(date_str)
        if not target_date:
            raise ValueError
    except (ValueError, TypeError):
        return JsonResponse({'error': 'Neplatný formát dátumu.'}, status=400)

    # Ochrana pred dopytovaním zmazaných dát
    dnes = timezone.now().date()
    if (dnes - target_date).days > 14:
        return JsonResponse({'error': 'Z dôvodu GDPR a optimalizácie sú dáta dostupné len 14 dní dozadu.'}, status=400)
    if target_date > dnes:
        return JsonResponse({'error': 'Nemôžete sledovať budúcnosť.'}, status=400)

    zaciatok_dna = datetime.datetime.combine(target_date, datetime.time.min)
    koniec_dna = zaciatok_dna + datetime.timedelta(days=1)

    # Stiahneme len potrebné polia, zoradené podľa času
    stopy = GPSStopa.objects.filter(
        timestamp__gte=zaciatok_dna, timestamp__lt=koniec_dna
    ).select_related('zamestnanec').order_by('timestamp').values(
        'zamestnanec_id', 'zamestnanec__meno', 'zamestnanec__priezvisko', 'lat', 'lon', 'timestamp'
    )

    history_data = {}
    for s in stopy:
        tid = s['zamestnanec_id']
        if tid not in history_data:
            history_data[tid] = {
                'meno': f"{s['zamestnanec__meno']} {s['zamestnanec__priezvisko']}",
                'stopy': []
            }
        # Prevedieme čas na sekundy od polnoci (0 až 86400), aby s tým vedel pracovať HTML Slider
        ts = s['timestamp']
        z_dna = zaciatok_dna
        if timezone.is_aware(ts) and timezone.is_naive(z_dna):
            z_dna = timezone.make_aware(z_dna, timezone.get_current_timezone())
        elif timezone.is_naive(ts) and timezone.is_aware(z_dna):
            z_dna = timezone.make_naive(z_dna, timezone.get_current_timezone())
        sekundy_od_polnoci = int((ts - z_dna).total_seconds())
        history_data[tid]['stopy'].append([s['lat'], s['lon'], sekundy_od_polnoci])

    if not history_data:
        return JsonResponse({'error': 'Pre tento deň neexistujú žiadne GPS záznamy.'}, status=404)

    return JsonResponse({'status': 'ok', 'data': history_data})

@csrf_exempt
@staff_member_required
def api_optimize_route(request):
    """🧠 ISAS AI ROUTING: Návrh optimálnej trasy (Bez zápisu do DB)"""
    if request.method != 'POST':
        return JsonResponse({'error': 'Povolený len POST request.'}, status=405)

    tech_id_raw = request.POST.get('tech_id')
    if not tech_id_raw:
        return JsonResponse({'error': 'Chýba ID technika.'}, status=400)
    try:
        tech_id = int(tech_id_raw)
    except (ValueError, TypeError):
        return JsonResponse({'error': 'Neplatné ID technika.'}, status=400)

    dnes = timezone.now().date()
    zaciatok_dna = datetime.datetime.combine(dnes, datetime.time.min)
    koniec_dna = zaciatok_dna + datetime.timedelta(days=1)

    # 1. Vytiahneme dnešné nedokončené úlohy technika
    ulohy = list(Uloha.objects.filter(
        technici__id=tech_id, 
        dokoncene=False, 
        zaciatok__gte=zaciatok_dna, 
        zaciatok__lt=koniec_dna
    ).select_related('zakaznik').order_by('zaciatok'))

    # Na zmysluplnú optimalizáciu treba aspoň 2 úlohy
    if len(ulohy) < 2:
        return JsonResponse({'error': 'Na optimalizáciu sú potrebné aspoň 2 zostávajúce úlohy.'}, status=400)

    # 2. Kontrola, či majú všetci zákazníci GPS
    for u in ulohy:
        if not u.zakaznik or not u.zakaznik.lat or not u.zakaznik.lon:
            return JsonResponse({'error': f'Zákazník {u.zakaznik} nemá vyplnené GPS súradnice. AI nevie vypočítať trasu.'}, status=400)

    # 3. Získanie aktuálnej polohy technika (Štartovací bod)
    try:
        cache_zaznam = cache.get(f'isas_radar_tech_{tech_id}')
    except Exception as e:
        logger.warning(f"Cache nedostupná pri optimalizácii trasy: {e}")
        cache_zaznam = None
    tech_lat, tech_lon = None, None
    if cache_zaznam and cache_zaznam.get('lat') and cache_zaznam.get('lon'):
        tech_lat, tech_lon = cache_zaznam['lat'], cache_zaznam['lon']
    else:
        # Ak technik nemá Live polohu, použijeme ako štart prvú úlohu v jeho aktuálnom pláne
        tech_lat, tech_lon = ulohy[0].zakaznik.lat, ulohy[0].zakaznik.lon

    # 4. 🧠 ISAS AI ROUTING ENGINE (Google Traffic + OSRM Fallback)
    optimalne_poradie = []
    pouzity_engine = "OSRM (Statická trasa)"
    
    google_api_key = get_google_maps_api_key()

    google_uspesny = False

    if google_api_key:
        try:
            # 🚦 GOOGLE DIRECTIONS API (S Live Traffic)
            origin = f"{tech_lat},{tech_lon}"
            destination = f"{ulohy[-1].zakaznik.lat},{ulohy[-1].zakaznik.lon}"
            
            # Waypoints (všetky úlohy okrem poslednej)
            waypoints_list = [f"{u.zakaznik.lat},{u.zakaznik.lon}" for u in ulohy[:-1]]
            waypoints_str = "optimize:true|" + "|".join(waypoints_list) if waypoints_list else ""
            
            # departure_time=now je KRITICKÉ pre započítanie aktuálnych zápch!
            google_url = f"https://maps.googleapis.com/maps/api/directions/json?origin={origin}&destination={destination}&waypoints={waypoints_str}&departure_time=now&key={google_api_key}"
            
            # 🛡️ ISAS TANK ARMOR: Timeout 5 sekúnd chráni workery pred zamrznutím
            response = requests.get(google_url, timeout=5)
            response.raise_for_status()
            data = response.json()
            
            if data.get('status') == 'OK':
                waypoint_order = data['routes'][0]['waypoint_order']
                
                # Google vráti poradie prejazdových bodov (waypoints).
                for i, original_index in enumerate(waypoint_order):
                    optimalne_poradie.append({'uloha': ulohy[original_index], 'optimal_index': i})
                
                # Pridáme poslednú úlohu (cieľ) na koniec
                optimalne_poradie.append({'uloha': ulohy[-1], 'optimal_index': len(waypoint_order)})
                
                pouzity_engine = "Google Maps (Live Traffic 🚦)"
                google_uspesny = True
            else:
                raise Exception(f"Google API zamietlo požiadavku: {data.get('status')}")
                
        except Exception as e:
            logging.getLogger(__name__).warning(f"Google Routing zlyhal, prepínam na OSRM: {e}")
            optimalne_poradie.clear()
            google_uspesny = False

    # 🛡️ FALLBACK NA OSRM (Ak Google zlyhal, alebo nie je kľúč)
    if not google_uspesny:
        coords = f"{tech_lon},{tech_lat}"
        for u in ulohy:
            coords += f";{u.zakaznik.lon},{u.zakaznik.lat}"

        osrm_url = f"http://router.project-osrm.org/trip/v1/driving/{coords}?source=first&roundtrip=false"

        try:
            response = requests.get(osrm_url, timeout=10)
            if response.status_code != 200:
                raise Exception("OSRM API neodpovedá.")
            data = response.json()
            if data.get('code') != 'Ok':
                raise Exception("OSRM nedokázal vypočítať trasu.")
                
            waypoints = data['waypoints']
            for i, u in enumerate(ulohy):
                if i + 1 < len(waypoints):
                    wp_index = waypoints[i + 1]['waypoint_index']
                    optimalne_poradie.append({'uloha': u, 'optimal_index': wp_index})
                else:
                    optimalne_poradie.append({'uloha': u, 'optimal_index': 999 + i})
                    
        except Exception as e:
            return JsonResponse({'error': f'Kritická chyba routovacích serverov (Google aj OSRM zlyhali): {str(e)}'}, status=503)

    optimalne_poradie.sort(key=lambda x: x['optimal_index'])

    # 6. 🚀 ISAS SAFE MODE: Vytvorenie textového návrhu (ŽIADEN ZÁPIS DO DB!)
    navrh_text = f"🧠 <b>AI navrhuje toto poradie výjazdov</b><br><span style='font-size:10px; color:#64748b;'>Zdroj dát: {pouzity_engine}</span>\n\n"
    
    for poradie, item in enumerate(optimalne_poradie, 1):
        u = item['uloha']
        cas_str = u.zaciatok.strftime('%H:%M')
        zakaznik_nazov = u.zakaznik.Nazov_spolocnosti if u.zakaznik else "Neznámy zákazník"
        navrh_text += f"<b>{poradie}. {zakaznik_nazov}</b> (Pôvodný plán: {cas_str})\n"

    navrh_text += "\n⚠️ <i>Upozornenie: Časy v systéme neboli zmenené. Ak chcete zmeniť poradie, kontaktujte zákazníkov a upravte časy manuálne.</i>"

    return JsonResponse({'status': 'ok', 'message': navrh_text})

@staff_member_required
def spotify_login(request):
    """
    KROK 1: Presmerovanie dispečera na prihlasovaciu bránu Spotify.
    """
    gn = GlobalneNastavenia.objects.first()
    if not gn or not gn.spotify_client_id or not gn.spotify_client_secret:
        messages.error(request, "⚠️ Spotify nie je nakonfigurované. Prejdite do 'Globálne nastavenia' -> sekcia 'ISAS JUKEBOX' a zadajte Client ID a Secret.")
        return redirect('/admin/')

    redirect_uri = request.build_absolute_uri('/spotify/callback/')
    # 🚀 ISAS ENGINE: Pridané práva na čítanie a úpravu playlistov
    scope = 'streaming user-read-email user-read-private user-modify-playback-state user-read-playback-state playlist-read-private playlist-read-collaborative playlist-modify-private playlist-modify-public'

    # 🛡️ BEZPEČNOSŤ: Generovanie kryptografického 'state' proti CSRF útokom
    state = secrets.token_urlsafe(32)
    request.session['spotify_oauth_state'] = state

    params = {
        'response_type': 'code',
        'client_id': gn.spotify_client_id,
        'scope': scope,
        'redirect_uri': redirect_uri,
        'state': state, # Pridaný bezpečnostný parameter
    }
    
    url = f"https://accounts.spotify.com/authorize?{urllib.parse.urlencode(params)}"
    return redirect(url)

@staff_member_required
def spotify_callback(request):
    """
    KROK 2: Zachytenie návratu zo Spotify a výmena kódu za šifrované tokeny.
    """
    code = request.GET.get('code')
    error = request.GET.get('error')
    state = request.GET.get('state')
    
    # 🛡️ BEZPEČNOSŤ: Overenie 'state' parametra (Ochrana pred podvrhnutým prihlásením)
    saved_state = request.session.pop('spotify_oauth_state', None)
    if not state or state != saved_state:
        logger.warning(f"Spotify OAuth CSRF pokus zablokovaný pre usera: {request.user.username}")
        messages.error(request, "🛑 Bezpečnostná chyba: Neplatná relácia prihlásenia. Skúste to znova.")
        return redirect('/admin/')

    if error:
        messages.error(request, f"⚠️ Prihlásenie do Spotify bolo zrušené alebo zlyhalo: {error}")
        return redirect('/admin/')
        
    if not code:
        messages.error(request, "⚠️ Od Spotify nebol prijatý autorizačný kód.")
        return redirect('/admin/')

    gn = GlobalneNastavenia.objects.first()
    if not gn or not gn.spotify_client_id or not gn.spotify_client_secret:
        logger.error("Spotify OAuth Callback zlyhal: V Globálnych nastaveniach chýba Client ID alebo Secret.")
        messages.error(request, "⚠️ Spotify nie je v Globálnych nastaveniach nakonfigurované.")
        return redirect('/admin/')

    redirect_uri = request.build_absolute_uri('/spotify/callback/')

    auth_str = f"{gn.spotify_client_id}:{gn.spotify_client_secret}"
    b64_auth_str = base64.b64encode(auth_str.encode()).decode()

    headers = {
        'Authorization': f'Basic {b64_auth_str}',
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    data = {
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': redirect_uri
    }

    try:
        response = requests.post('https://accounts.spotify.com/api/token', headers=headers, data=data, timeout=10)
        response.raise_for_status()
        token_data = response.json()

        try:
            expires_in = int(token_data.get('expires_in', 3600))
        except (ValueError, TypeError):
            expires_in = 3600
        expires_at = timezone.now() + datetime.timedelta(seconds=expires_in)

        # 🧠 ISAS ENGINE: Získanie mena Spotify profilu
        spotify_account_name = "Neznámy účet"
        try:
            profile_res = requests.get(
                'https://api.spotify.com/v1/me', 
                headers={'Authorization': f"Bearer {token_data['access_token']}"},
                timeout=5
            )
            if profile_res.status_code == 200:
                profile_data = profile_res.json()
                # Zoberieme display_name, ak nie je, tak email, ak nie je, tak id
                spotify_account_name = profile_data.get('display_name') or profile_data.get('email') or profile_data.get('id')
        except Exception as e:
            logger.warning(f"Nepodarilo sa získať meno Spotify profilu: {e}")

        spotify_defaults = {
            'access_token': token_data['access_token'],
            'expires_at': expires_at,
            'spotify_account_name': spotify_account_name
        }
        if 'refresh_token' in token_data:
            spotify_defaults['refresh_token'] = token_data['refresh_token']

        SpotifyToken.objects.update_or_create(
            user=request.user,
            defaults=spotify_defaults
        )
        messages.success(request, "🎵 Spotify bolo úspešne prepojené! Jukebox je pripravený.")
    except Exception as e:
        logger.error(f"ISAS Spotify Callback Error: {e}")
        messages.error(request, "🚨 Nastala chyba pri komunikácii so servermi Spotify. Skúste to znova.")

    return redirect('/admin/')

@staff_member_required
def api_spotify_token(request):
    """
    KROK 3: Interné API pre Jukebox (Frontend). 
    Vráti platný Access Token. Ak expiroval, potichu ho obnoví.
    """
    # 🚀 ISAS ENGINE: KROK 1 - Rýchle čítanie BEZ zámku (99% prípadov).
    # Zabraňuje zbytočnému zamykaniu databázy pri každom pingnutí z frontendu.
    try:
        token = SpotifyToken.objects.get(user=request.user)
    except SpotifyToken.DoesNotExist:
        return JsonResponse({'error': 'Not authenticated'}, status=401)

    # Ak je token platný, okamžite ho vrátime (žiadne zamykanie DB)
    if not token.is_expired():
        return JsonResponse({'access_token': token.access_token})

    # 🛡️ STABILITA: Ak je token expirovaný, vykonáme sieťový dopyt bez zamykania databázy
    if token.is_expired():
        gn = GlobalneNastavenia.objects.first()
        if not gn or not gn.spotify_client_id or not gn.spotify_client_secret:
            return JsonResponse({'error': 'Spotify configuration missing'}, status=500)

        auth_str = f"{gn.spotify_client_id}:{gn.spotify_client_secret}"
        b64_auth_str = base64.b64encode(auth_str.encode()).decode()

        headers = {
            'Authorization': f'Basic {b64_auth_str}',
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        data = {
            'grant_type': 'refresh_token',
            'refresh_token': token.refresh_token
        }

        try:
            response = requests.post('https://accounts.spotify.com/api/token', headers=headers, data=data, timeout=10)
            response.raise_for_status()
            token_data = response.json()
            
            # Databázový zámok aplikujeme iba na bleskový zápis nových dát
            with transaction.atomic():
                token = SpotifyToken.objects.select_for_update().get(user=request.user)
                token.access_token = token_data['access_token']
                if 'refresh_token' in token_data:
                    token.refresh_token = token_data['refresh_token']

                try:
                    expires_in = int(token_data.get('expires_in', 3600))
                except (ValueError, TypeError):
                    expires_in = 3600
                token.expires_at = timezone.now() + datetime.timedelta(seconds=expires_in)
                token.save()
                
        except requests.exceptions.HTTPError as e:
            # 🛡️ STABILITA: Ak Spotify vráti 400/401 (napr. užívateľ odvolal prístup),
            # musíme token zmazať, inak by sme sa zasekli v nekonečnej slučke chýb.
            if e.response is not None and e.response.status_code in (400, 401):
                logger.warning(f"Spotify Refresh Token zlyhal (pravdepodobne odvolaný prístup). Mažem token pre usera: {request.user.username}")
                token.delete()
                return JsonResponse({'error': 'Token revoked, please re-login'}, status=401)
                
            logger.error(f"ISAS Spotify Refresh HTTP Error: {e}")
            return JsonResponse({'error': 'Failed to refresh token'}, status=500)
                
        except Exception as e:
            logger.error(f"ISAS Spotify Refresh General Error: {e}")
            return JsonResponse({'error': 'Failed to refresh token'}, status=500)

    return JsonResponse({'access_token': token.access_token})

def _ziskaj_anonymizovane_meno_hraca(user) -> str:
    """
    Bezpečne naformátuje meno zamestnanca pre rebríček: 'Meno P. (Rola)'.
    Prioritne čerpá z HR profilu Zamestnanec, následne z auth.User (Zero Data Leak).
    """
    zamestnanec = getattr(user, 'zamestnanec', None)
    
    # 1. Získanie mena a priezviska: priorita má model Zamestnanec, fallback auth.User
    krstne = ""
    priezvisko = ""
    if zamestnanec:
        krstne = (getattr(zamestnanec, 'meno', '') or "").strip()
        priezvisko = (getattr(zamestnanec, 'priezvisko', '') or "").strip()

    if not krstne:
        krstne = (user.first_name or "").strip()
    if not priezvisko:
        priezvisko = (user.last_name or "").strip()

    if krstne and priezvisko:
        cele_meno = f"{krstne} {priezvisko[0]}."
    elif krstne:
        cele_meno = krstne
    elif user.username:
        cele_meno = user.username
    else:
        cele_meno = "Kolega"

    # 2. Zistenie roly: bezpečné defenzívne overenie bez predpokladu existencie konkrétnych polí
    rola_nazov = ""
    if zamestnanec:
        verejna_rola = getattr(zamestnanec, 'verejna_rola', None)
        if verejna_rola:
            rola_nazov = str(verejna_rola).strip()
        else:
            get_rola = getattr(zamestnanec, 'get_pracovna_rola_display', None)
            prac_rola = getattr(zamestnanec, 'pracovna_rola', "")
            rola_nazov = get_rola() if callable(get_rola) else str(prac_rola or "")
    elif user.is_superuser:
        rola_nazov = "Vedenie"

    return f"{cele_meno} ({rola_nazov})" if rola_nazov else cele_meno

def _zostav_leaderboard_payload(aktualny_user):
    """Zostaví all-time aj kvartálny TOP 10 zoznam a seed pre dennú výzvu."""
    teraz = timezone.now()
    aktualny_kod, sezona_nazov = ISASHraLeaderboard.get_aktualny_kvartal_info(teraz)
    aktualny_den_kod = teraz.strftime("%Y-%m-%d")
    mapa_dlazdic = get_easter_egg_tiles()

    def _ziskaj_bezpecne_foto_url(user) -> str:
        """Bezpečne získa URL profilovej fotky s garanciou ochrany pred ValueError pri chýbajúcom súbore."""
        zam = getattr(user, 'zamestnanec', None)
        if zam and getattr(zam, 'profilova_fotka', None):
            try:
                return zam.profilova_fotka.url if zam.profilova_fotka.name else ""
            except (ValueError, AttributeError):
                return ""
        return ""

    # 1. All-time Sieň slávy (N+1 optimalizované cez select_related)
    top_all_time = (
        ISASHraLeaderboard.objects
        .filter(user__is_staff=True, user__is_active=True, max_skore__gt=0)
        .select_related('user', 'user__zamestnanec')
        .order_by('-max_skore', '-dosiahnute_dna')[:10]
    )
    rebricek_all_time = [
        {
            "poradie": idx,
            "meno": _ziskaj_anonymizovane_meno_hraca(z.user),
            "foto_url": _ziskaj_bezpecne_foto_url(z.user),
            "skore": z.max_skore,
            "dlazdica_nazov": mapa_dlazdic.get(z.max_dlazdica, f"Položka {z.max_dlazdica}"),
            "je_moj_profil": (z.user_id == aktualny_user.id),
        }
        for idx, z in enumerate(top_all_time, start=1)
    ]

    # 2. Kvartálna firemná sezóna (N+1 optimalizované)
    top_sezona = (
        ISASHraLeaderboard.objects
        .filter(user__is_staff=True, user__is_active=True, sezona_kod=aktualny_kod, sezona_skore__gt=0)
        .select_related('user', 'user__zamestnanec')
        .order_by('-sezona_skore', '-sezona_dosiahnute_dna')[:10]
    )
    rebricek_sezona = [
        {
            "poradie": idx,
            "meno": _ziskaj_anonymizovane_meno_hraca(z.user),
            "foto_url": _ziskaj_bezpecne_foto_url(z.user),
            "skore": z.sezona_skore,
            "dlazdica_nazov": mapa_dlazdic.get(z.sezona_dlazdica, f"Položka {z.sezona_dlazdica}"),
            "je_moj_profil": (z.user_id == aktualny_user.id),
        }
        for idx, z in enumerate(top_sezona, start=1)
    ]

    # Osobné štatistiky
    moj_zaznam = ISASHraLeaderboard.objects.filter(user=aktualny_user).first()
    moje_skore_all = moj_zaznam.max_skore if moj_zaznam else 0
    moje_skore_sezona = (moj_zaznam.sezona_skore if moj_zaznam and moj_zaznam.sezona_kod == aktualny_kod else 0)

    moje_poradie_all = None
    if moj_zaznam and moj_zaznam.max_skore > 0:
        lepsi = ISASHraLeaderboard.objects.filter(user__is_staff=True, user__is_active=True).filter(
            Q(max_skore__gt=moj_zaznam.max_skore) |
            Q(max_skore=moj_zaznam.max_skore, dosiahnute_dna__gt=moj_zaznam.dosiahnute_dna)
        ).count()
        moje_poradie_all = lepsi + 1

    moje_poradie_sezona = None
    if moj_zaznam and moj_zaznam.sezona_kod == aktualny_kod and moj_zaznam.sezona_skore > 0:
        lepsi_sez = ISASHraLeaderboard.objects.filter(user__is_staff=True, user__is_active=True, sezona_kod=aktualny_kod).filter(
            Q(sezona_skore__gt=moj_zaznam.sezona_skore) |
            Q(sezona_skore=moj_zaznam.sezona_skore, sezona_dosiahnute_dna__gt=moj_zaznam.sezona_dosiahnute_dna)
        ).count()
        moje_poradie_sezona = lepsi_sez + 1

    # 3. Firemná Denná výzva (N+1 optimalizované)
    top_den = (
        ISASHraLeaderboard.objects
        .filter(user__is_staff=True, user__is_active=True, den_kod=aktualny_den_kod, den_skore__gt=0)
        .select_related('user', 'user__zamestnanec')
        .order_by('-den_skore', '-den_dosiahnute_dna')[:10]
    )
    rebricek_den = [
        {
            "poradie": idx,
            "meno": _ziskaj_anonymizovane_meno_hraca(z.user),
            "foto_url": _ziskaj_bezpecne_foto_url(z.user),
            "skore": z.den_skore,
            "dlazdica_nazov": mapa_dlazdic.get(z.den_dlazdica, f"Položka {z.den_dlazdica}"),
            "je_moj_profil": (z.user_id == aktualny_user.id),
            "bounty_splnena": getattr(z, 'den_bounty_splnena', False),
        }
        for idx, z in enumerate(top_den, start=1)
    ]

    moje_skore_den = (moj_zaznam.den_skore if moj_zaznam and moj_zaznam.den_kod == aktualny_den_kod else 0)
    moje_poradie_den = None
    if moj_zaznam and moj_zaznam.den_kod == aktualny_den_kod and moj_zaznam.den_skore > 0:
        lepsi_den = ISASHraLeaderboard.objects.filter(user__is_staff=True, user__is_active=True, den_kod=aktualny_den_kod).filter(
            Q(den_skore__gt=moj_zaznam.den_skore) |
            Q(den_skore=moj_zaznam.den_skore, den_dosiahnute_dna__gt=moj_zaznam.den_dosiahnute_dna)
        ).count()
        moje_poradie_den = lepsi_den + 1

    # 4. Tímový rebríček divízií (defenzívne získanie volieb rolí)
    try:
        mapa_rol = dict(getattr(Zamestnanec, 'ROLA_CHOICES', ()))
    except (ImportError, AttributeError):
        mapa_rol = {}

    # Deterministické radenie: pri zhode priemeru rozhoduje počet zapojených hráčov (viazané na aktuálny kvartál)
    timy_query = (
        ISASHraLeaderboard.objects
        .filter(user__is_staff=True, user__is_active=True, sezona_kod=aktualny_kod, sezona_skore__gt=0)
        .values('user__zamestnanec__pracovna_rola')
        .annotate(
            priemer_skore=Avg('sezona_skore'),
            pocet_hracov=Count('id')
        )
        .order_by('-priemer_skore', '-pocet_hracov')
    )

    rebricek_timy = []
    moj_zamestnanec = getattr(aktualny_user, 'zamestnanec', None)
    moja_rola = getattr(moj_zamestnanec, 'pracovna_rola', None) if moj_zamestnanec else None

    for idx, t in enumerate(timy_query, start=1):
        rola_kod = t['user__zamestnanec__pracovna_rola']
        nazov_timu = mapa_rol.get(rola_kod, "👑 Vedenie / Ostatní" if not rola_kod else rola_kod)
        # Správne priradí tím aj vedeniu bez profilu Zamestnanec (rola_kod is None a moja_rola is None)
        je_moj = (rola_kod == moja_rola) if moja_rola is not None else (rola_kod is None)
        rebricek_timy.append({
            "poradie": idx,
            "nazov_timu": nazov_timu,
            "priemer_skore": int(round(t['priemer_skore'] or 0)),
            "pocet_hracov": t['pocet_hracov'],
            "je_moj_tim": je_moj
        })

    # 4b. Kooperatívny firemný míľnik (Spoločná kvartálna kapacita celého podniku)
    kvartal_suma = (
        ISASHraLeaderboard.objects
        .filter(user__is_staff=True, user__is_active=True, sezona_kod=aktualny_kod)
        .aggregate(spolu=Sum('sezona_skore'))['spolu'] or 0
    )
    milnik_krok = 50000
    kvartal_ciel = max(50000, ((kvartal_suma // milnik_krok) + 1) * milnik_krok)
    kvartal_pct = min(100, int(round((kvartal_suma / kvartal_ciel) * 100))) if kvartal_ciel > 0 else 0

    # 🛡️ Zero Data Leak: Bezpečné profesijné atribúty hráča z modelu Zamestnanec pre vizitku
    hrac_zamestnanec = getattr(aktualny_user, 'zamestnanec', None)
    ikona_pomeru = "👨‍💼"
    pracovna_pozicia = ""

    if hrac_zamestnanec:
        pomer_kod = getattr(hrac_zamestnanec, 'typ_pomeru', '')
        if pomer_kod == getattr(Zamestnanec, 'KONATEL', 'KO'):
            ikona_pomeru = "👑"
        elif pomer_kod == getattr(Zamestnanec, 'EXTERNI', 'EX'):
            ikona_pomeru = "🤝"
        elif pomer_kod == getattr(Zamestnanec, 'BRIGADNICI', 'BR'):
            ikona_pomeru = "🎒"

        if getattr(hrac_zamestnanec, 'verejna_rola', None):
            pracovna_pozicia = hrac_zamestnanec.verejna_rola.strip()
        else:
            get_rola = getattr(hrac_zamestnanec, 'get_pracovna_rola_display', None)
            pracovna_pozicia = get_rola() if callable(get_rola) else str(getattr(hrac_zamestnanec, 'pracovna_rola', ''))
    elif aktualny_user.is_superuser:
        ikona_pomeru = "👑"
        pracovna_pozicia = "Vedenie podniku"

    profil_hraca_karta = {
        "meno": _ziskaj_anonymizovane_meno_hraca(aktualny_user),
        "ikona_pomeru": ikona_pomeru,
        "pracovna_rola": pracovna_pozicia or "Interný špecialista"
    }

    return {
        "rebricek_all_time": rebricek_all_time,
        "rebricek_sezona": rebricek_sezona,
        "rebricek_den": rebricek_den,
        "rebricek_timy": rebricek_timy,
        "kvartalny_milnik": {
            "aktualne_skore": kvartal_suma,
            "cielove_skore": kvartal_ciel,
            "progres_pct": kvartal_pct,
            "sezona_nazov": sezona_nazov,
        },
        "sezona_nazov": sezona_nazov,
        "sezona_kod": aktualny_kod,
        "moje_skore": moje_skore_all,
        "moje_skore_sezona": moje_skore_sezona,
        "moje_skore_den": moje_skore_den,
        "moje_poradie_all": moje_poradie_all,
        "moje_poradie_sezona": moje_poradie_sezona,
        "moje_poradie_den": moje_poradie_den,
        "moja_den_bounty_splnena": (moj_zaznam.den_bounty_splnena if moj_zaznam and moj_zaznam.den_kod == aktualny_den_kod else False),
        "mapa_dlazdic": mapa_dlazdic,
        "denny_seed": aktualny_den_kod,
        "denna_bounty": ISASHraLeaderboard.get_denna_bounty(aktualny_den_kod),
        "profil_hraca": profil_hraca_karta,
        "herne_mechaniky": {
            "medzisklad_povoleny": True,
            "medzisklad_kapacita": 1,
            "medzisklad_nazov": "Karanténny medzisklad",
        },
    }

@require_http_methods(["GET", "POST"])
def api_easter_egg_2048(request):
    """
    Centrálny endpoint pre hru ISAS 2048.
    GET: Vráti TOP 10 rebríček a osobný rekord.
    POST: Zvaliduje a zapíše nový výsledok.
    """
    # 🔒 1. RBAC ochrana: Prísne iba pre interných zamestnancov
    if not request.user.is_authenticated or not request.user.is_staff:
        return JsonResponse({"error": "Prístup zamietnutý. Určené výhradne pre personál."}, status=403)

    if request.method == "GET":
        data = _zostav_leaderboard_payload(request.user)
        return JsonResponse({"status": "ok", **data})

    # --- POST METÓDA (Zápis výsledku) ---
    # ⏱️ 2. Anti-Flood ochrana: max 1 zápis za 2 sekundy (nezablokuje uloženie pri rýchlom Game Over)
    cache_key = f"rate_limit_2048_{request.user.id}"
    if cache.get(cache_key):
        return JsonResponse({"error": "Príliš rýchle odosielanie. Počkaj chvíľu."}, status=429)

    # 📦 3. Bezpečné parsovanie JSON tela a hernej telemetrie
    try:
        body = json.loads(request.body.decode("utf-8"))
        skore = body.get("skore")
        max_dlazdica = body.get("max_dlazdica")
        rezim = str(body.get("rezim", "standard"))
        bounty_splnena = bool(body.get("bounty_splnena", False))
        telemetria = body.get("telemetria") if isinstance(body.get("telemetria"), dict) else {}
    except (json.JSONDecodeError, AttributeError, UnicodeDecodeError):
        return JsonResponse({"error": "Neplatný formát JSON dát."}, status=400)

    # 🛡️ 4. Anti-Cheat matematická kontrola skóre a dosiahnutej dlaždice
    je_platne, dovod = ISASHraLeaderboard.validuj_vysledok(skore, max_dlazdica)
    if not je_platne:
        return JsonResponse({"error": f"Validácia zlyhala: {dovod}"}, status=400)

    # 🎯 4b. Anti-Cheat verifikácia smenovej úlohy (eliminácia klientskeho podvrhnutia)
    overena_bounty = False
    if rezim == "daily" and bounty_splnena:
        aktualny_den_kod = timezone.now().strftime("%Y-%m-%d")
        overena_bounty = ISASHraLeaderboard.validuj_dennu_bounty(
            aktualny_den_kod, skore, max_dlazdica, telemetria
        )

    # 💾 5. Atomický zápis do databázy
    with transaction.atomic():
        zaznam, _ = ISASHraLeaderboard.objects.select_for_update().get_or_create(
            user=request.user,
            defaults={"max_skore": 0, "max_dlazdica": 2}
        )
        vysledok_zaznamu = zaznam.zaznamenaj_vysledok(
            skore, max_dlazdica, rezim=rezim, bounty_splnena=overena_bounty
        )

    # Nastavíme rate-limit zámok na 2 sekundy
    cache.set(cache_key, True, timeout=2)

    data = _zostav_leaderboard_payload(request.user)
    return JsonResponse({
        "status": "ok",
        "prekonal_rekord": vysledok_zaznamu.get("all_time", False),
        "prekonal_sezonu": vysledok_zaznamu.get("sezona", False),
        "prekonal_den": vysledok_zaznamu.get("den", False),
        "bounty_uznana": overena_bounty if rezim == "daily" else False,
        **data
    })



PROTECTED_MEDIA_PREFIXES = (
    'systemove_zalohy/',
    'systemove_exporty/',
    'importy_dat/',
    'hr_zmluvy/',
    'hr_certifikaty/',
    'hr_absencie/',
    'kariera/zivotopisy/',
    'emailove_prilohy/',
    'emaile_prilohy/',
    'objednavky_dodavatelom/',
    'faktury/',
    'faktury_xml/',
    'faktury_vizualizacie/',
    'blocky/',
    'vygenerovane_vstupy/',
    'hr_podpisy/',
    'identita/peciatky/',
)

B2B_PROTECTED_MEDIA_PREFIXES = (
    'vystavene_faktury_pdf/',
    'peppol_xml/',
    'cenove_ponuky_pdf/',
    'prijate_objednavky/',
    'servisne_protokoly_pdf/',
    'sablony_vstupov/',
)


def _b2b_client_owns_media(b2b_klient, norm_path):
    """Overí v DB, či prihlásený B2B klient vlastní požadovaný súbor."""
    if norm_path.startswith('vystavene_faktury_pdf/'):
        VystavenaFaktura = apps.get_model('Evidencia', 'VystavenaFaktura')
        return bool(VystavenaFaktura and VystavenaFaktura.objects.filter(
            zakaznik=b2b_klient, pdf_dokument=norm_path
        ).exclude(stav__in=['DRAFT', 'STORNO']).exists())

    if norm_path.startswith('peppol_xml/'):
        VystavenaFaktura = apps.get_model('Evidencia', 'VystavenaFaktura')
        return bool(VystavenaFaktura and VystavenaFaktura.objects.filter(
            zakaznik=b2b_klient, peppol_xml_dokument=norm_path
        ).exclude(stav__in=['DRAFT', 'STORNO']).exists())

    if norm_path.startswith('cenove_ponuky_pdf/'):
        CenovaPonuka = apps.get_model('Evidencia', 'CenovaPonuka')
        return bool(CenovaPonuka and CenovaPonuka.objects.filter(
            zakaznik=b2b_klient, pdf_dokument=norm_path
        ).exists())

    if norm_path.startswith('prijate_objednavky/'):
        PrijataObjednavka = apps.get_model('Evidencia', 'PrijataObjednavka')
        return bool(PrijataObjednavka and PrijataObjednavka.objects.filter(
            zakaznik=b2b_klient, subor_objednavky=norm_path
        ).exists())

    if norm_path.startswith('sablony_vstupov/'):
        sablona = getattr(b2b_klient, 'sablona_vstupu', None)
        return bool(sablona and getattr(sablona, 'name', None) == norm_path)

    if norm_path.startswith('servisne_protokoly_pdf/'):
        ServisnyZaznam = apps.get_model('Evidencia', 'ServisnyZaznam')
        return bool(ServisnyZaznam and ServisnyZaznam.objects.filter(
            uzavrety_pdf_dokument=norm_path, dokoncene=True
        ).filter(
            Q(zakaznik=b2b_klient) | Q(zariadenie__zakaznik=b2b_klient) | Q(zariadenie__prenajmy__zakaznik=b2b_klient)
        ).exists())

    return False


@require_http_methods(["GET", "HEAD"])
def protected_media_view(request, rel_path):
    """🛡️ Autorizované servovanie interných a B2B médií cez Nginx X-Accel-Redirect."""
    user = getattr(request, 'user', None)
    if not user or not getattr(user, 'is_authenticated', False) or not getattr(user, 'is_active', False):
        return HttpResponseForbidden("Prístup zamietnutý.")

    if not rel_path or '\x00' in rel_path or '\\' in rel_path:
        raise Http404("Súbor sa nenašiel.")

    norm_path = posixpath.normpath(rel_path).lstrip('/')
    if not norm_path or norm_path.startswith('..'):
        raise Http404("Súbor sa nenašiel.")

    is_staff_only = norm_path.startswith(PROTECTED_MEDIA_PREFIXES)
    is_b2b_doc = norm_path.startswith(B2B_PROTECTED_MEDIA_PREFIXES)
    if not is_staff_only and not is_b2b_doc:
        raise Http404("Súbor sa nenašiel.")

    try:
        b2b_klient = getattr(user, 'zakaznik_profil', None)
    except (ObjectDoesNotExist, AttributeError):
        b2b_klient = None

    is_staff_user = bool(getattr(user, 'is_staff', False) and b2b_klient is None)

    if is_staff_only:
        if not is_staff_user:
            return HttpResponseForbidden("Prístup zamietnutý.")
        if norm_path.startswith('systemove_zalohy/') and not getattr(user, 'is_superuser', False):
            MegaZaloha = apps.get_model('Evidencia', 'MegaZaloha')
            if MegaZaloha and not MegaZaloha.objects.filter(subor_zalohy=norm_path, vyziadal_uzivatel=user).exists():
                return HttpResponseForbidden("Prístup zamietnutý.")
    else:
        if not is_staff_user and (b2b_klient is None or not _b2b_client_owns_media(b2b_klient, norm_path)):
            return HttpResponseForbidden("Prístup zamietnutý.")

    try:
        full_path = safe_join(settings.MEDIA_ROOT, norm_path)
    except (SuspiciousFileOperation, ValueError):
        raise Http404("Súbor sa nenašiel.")

    if not os.path.isfile(full_path):
        raise Http404("Súbor sa nenašiel.")

    content_type, _ = mimetypes.guess_type(full_path)
    content_type = content_type or 'application/octet-stream'

    if not settings.DEBUG:
        response = HttpResponse(content_type=content_type)
        encoded_path = urllib.parse.quote(norm_path, safe='/')
        response['X-Accel-Redirect'] = f'/protected-media/{encoded_path}'
        response['Cache-Control'] = 'private, no-store'
        return response

    response = FileResponse(open(full_path, 'rb'), content_type=content_type)
    response['Cache-Control'] = 'private, no-store'
    return response