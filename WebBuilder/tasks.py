import json
import logging
import mimetypes
import os
import time
import uuid
import urllib.parse
import requests

from datetime import datetime, timedelta
from celery import shared_task
from django.utils.html import escape

logger = logging.getLogger(__name__)

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.template.loader import render_to_string
from django.utils import timezone
from django.core.serializers.json import DjangoJSONEncoder
from email.utils import formataddr
from django.core.validators import validate_email
from django.core.exceptions import ValidationError

from Evidencia.models import AlarmLog, Kandidat, PrijataObjednavka, ServisnyZaznam
from Evidencia.utils import ISASNotificationBuilder, ISASNotifier
from Evidencia.utils_helper import generate_dynamic_greeting
from ISASaplikacia.utils_helper import (
    ISASAutoHealTask,
    isas_task_execution_guard,
    validate_safe_webhook_url,
)
from WebBuilder.models import WebhookEndpoint, WebhookLog, WebNastavenia

@shared_task(base=ISASAutoHealTask, bind=True)
def notify_web_inquiry_task(self, objednavka_id):
    """
    Asynchrónne odošle notifikáciu majiteľovi a zároveň Auto-Responder zákazníkovi.
    Chránené Redis zámkom a Auto-Heal enginom.
    """
    # 2. IMPLEMENTÁCIA: Unikátny Redis zámok pre tento konkrétny dopyt
    lock_id = f"lock_web_inquiry_{objednavka_id}"
    
    # 3. IMPLEMENTÁCIA: isas_task_execution_guard (Automaticky načíta gn a identitu)
    with isas_task_execution_guard(lock_id, timeout=60) as (is_locked, nastavenia, identita):
        if not is_locked:
            # Ochrana proti "Double-Click" spamu z frontendu
            return "Task už beží, preskakujem duplicitu."

        try:
            objednavka = PrijataObjednavka.objects.select_related('zakaznik').get(pk=objednavka_id)
            web_nastavenia = WebNastavenia.objects.first()
            
            # ====================================================
            # 1. NOTIFIKÁCIA PRE DISPEČERA (ISASNotifier)
            # ====================================================
            raw_base = (getattr(nastavenia, 'clean_base_url', None) or getattr(settings, 'SITE_URL', '')).strip()
            if raw_base and not raw_base.startswith(('http://', 'https://')):
                raw_base = f"https://{raw_base}"
            base_url = raw_base.rstrip('/')
            admin_url = f"{base_url}/admin/Evidencia/prijataobjednavka/{objednavka.id}/change/"
            
            firma_nazov = (objednavka.zakaznik.Nazov_spolocnosti.strip() if (objednavka.zakaznik and objednavka.zakaznik.Nazov_spolocnosti and objednavka.zakaznik.Nazov_spolocnosti.strip()) else "Neznámy klient")
            kontakt = ((objednavka.zakaznik.telefon or objednavka.zakaznik.email) if objednavka.zakaznik else None) or objednavka.specificky_email_pre_fakturu or "Bez kontaktu"
            
            payload = (ISASNotificationBuilder(nadpis='NOVÝ DOPYT Z VEREJNÉHO WEBU', ikona='🌐', farba_temy='#10b981')
                       .pridaj_riadok('🏢 Zákazník:', firma_nazov)
                       .pridaj_riadok('📞 Kontakt:', kontakt)
                       .pridaj_riadok('🏷️ Číslo dopytu:', objednavka.cislo_objednavky_interne)
                       .nastav_hlavny_text(f"<b>Požiadavka klienta:</b><br>{escape(objednavka.poznamka_klienta).replace(chr(10), '<br>') if objednavka.poznamka_klienta else 'Bez špecifikácie'}")
                       .nastav_tlacidlo('Otvoriť dopyt v ISAS CORE', admin_url)
                       .nastav_ai_poznamku('Dopyt bol prijatý z verejnej webstránky a čaká na spracovanie dispečerom.')
                       .build())
            
            ISASNotifier.send(
                payload=payload,
                send_tg=getattr(nastavenia, 'objednavky_notif_tg', True),
                send_mail=getattr(nastavenia, 'objednavky_notif_mail', True),
                send_discord=getattr(nastavenia, 'objednavky_notif_discord', True)
            )

            # ====================================================
            # 2. AUTO-RESPONDER PRE ZÁKAZNÍKA (Krásny HTML E-mail)
            # ====================================================
            raw_email = objednavka.specificky_email_pre_fakturu or (objednavka.zakaznik.email if objednavka.zakaznik else None)
            klient_email = raw_email.strip() if raw_email else None
            
            # 🛡️ STABILITA: Pridaná kontrola smtp_password
            if klient_email and nastavenia and getattr(nastavenia, 'smtp_host', None) and nastavenia.smtp_user and nastavenia.smtp_password:
                try:
                    try:
                        validate_email(klient_email)
                    except ValidationError:
                        logger.warning(f"Objednávka {objednavka.id} obsahuje neplatný e-mail '{klient_email}', autoresponder preskakujem.")
                        return "Neplatný e-mail klienta, autoresponder nebol odoslaný."

                    clean_farba = web_nastavenia.hlavna_farba.strip().lstrip('#') if (web_nastavenia and web_nastavenia.hlavna_farba and web_nastavenia.hlavna_farba.strip()) else ''
                    is_valid_hex = len(clean_farba) in (3, 6) and all(c in '0123456789abcdefABCDEF' for c in clean_farba)
                    farba_brandu = f"#{clean_farba}" if is_valid_hex else '#0f172a'

                    # 🛡️ DEFENSIVE: Výpočet luminancie pre 100% čitateľnosť textu na dynamickej brandovej farbe
                    hex_6 = "".join([c * 2 for c in clean_farba]) if (is_valid_hex and len(clean_farba) == 3) else (clean_farba if is_valid_hex else '0f172a')
                    r, g, b = int(hex_6[0:2], 16), int(hex_6[2:4], 16), int(hex_6[4:6], 16)
                    brightness = (r * 299 + g * 587 + b * 114) / 1000
                    text_farba_brandu = '#0f172a' if brightness > 165 else '#ffffff'
                    subtext_farba_brandu = '#334155' if brightness > 165 else '#f1f5f9'
                    # 🛡️ UI CONTRAST GUARD: Ak je brand príliš svetlý (napr. biela/žltá), tlačidlo a akcent na bielom podklade nesmú zaniknúť
                    btn_farba_brandu = '#0f172a' if brightness > 190 else farba_brandu
                    btn_text_farba = '#ffffff' if brightness > 190 else text_farba_brandu
                    akcent_farba = '#0f172a' if brightness > 190 else farba_brandu
                    raw_nazov = getattr(identita, 'nazov_spolocnosti', None) if identita else None
                    # 🛡️ Prevencia BadHeaderError: odstránenie \r a \n z názvu firmy pred odovzdaním do formataddr
                    nazov_nasej_firmy = " ".join(raw_nazov.split()) if (isinstance(raw_nazov, str) and raw_nazov.strip()) else "Naša spoločnosť"
                    logo_path = identita.logo.url if (identita and getattr(identita, 'logo', None) and identita.logo.name) else None
                    # 🛡️ STABILITA: Outlook a webmaily nepodporujú SVG v e-mailoch - použijeme bezpečný textový fallback (ošetrené voči query parametrom)
                    is_svg = urllib.parse.urlsplit(logo_path).path.lower().endswith('.svg') if logo_path else False
                    if logo_path and not is_svg:
                        logo_url = logo_path if logo_path.startswith(('http://', 'https://')) else f"{base_url}/{logo_path.lstrip('/')}"
                    else:
                        logo_url = None
                    
                    cislo_obj = objednavka.cislo_objednavky_interne
                    raw_predmet = f"Potvrdenie prijatia dopytu č. {cislo_obj} - {nazov_nasej_firmy}" if cislo_obj else f"Potvrdenie prijatia dopytu - {nazov_nasej_firmy}"
                    predmet = " ".join(raw_predmet.split())  # 🛡️ Prevencia BadHeaderError (odstráni \r a \n)
                    
                    # 4. IMPLEMENTÁCIA: Dynamické oslovenie vytvorené PRED odovzdaním do šablóny
                    raw_oslovenie = generate_dynamic_greeting(objednavka.zakaznik, fallback="Dobrý deň,", use_formal=True) if objednavka.zakaznik else "Dobrý deň,"
                    oslovenie = raw_oslovenie.strip() if (isinstance(raw_oslovenie, str) and raw_oslovenie.strip()) else "Dobrý deň,"
                    sprava_klienta_cista = objednavka.poznamka_klienta.strip() if (objednavka.poznamka_klienta and objednavka.poznamka_klienta.strip()) else None

                    priloha_subor = None
                    nazov_suboru = None
                    mime_typ = None
                    MAX_EMAIL_PRILOHA_BYTES = 9 * 1024 * 1024  # 9 MB bezpečný strop pre SMTP transport
                    if objednavka.subor_objednavky and objednavka.subor_objednavky.name:
                        try:
                            velkost = getattr(objednavka.subor_objednavky, 'size', 0)
                            if 0 < velkost <= MAX_EMAIL_PRILOHA_BYTES:
                                raw_nazov = os.path.basename(objednavka.subor_objednavky.name)
                                nazov_suboru = " ".join(raw_nazov.split()) if raw_nazov else "priloha"
                                mime_typ, _ = mimetypes.guess_type(nazov_suboru)
                                with objednavka.subor_objednavky.open('rb') as f:
                                    priloha_subor = f.read()
                            else:
                                logger.warning(f"Príloha objednávky {objednavka.id} prekročila limit pre e-mail ({velkost} B), nebude priložená.")
                        except Exception as e:
                            logger.warning(f"Nepodarilo sa načítať prílohu k dopytu {objednavka.id}: {e}")
                    ma_prilohu = bool(priloha_subor)
                    tracking_ticket_url = None
                    if cislo_obj:
                        safe_ticket = urllib.parse.quote(str(cislo_obj).strip())
                        tracking_ticket_url = f"{base_url}/?ticket={safe_ticket}#stav-zakazky"

                    context = {
                        'farba_brandu': farba_brandu,
                        'text_farba_brandu': text_farba_brandu,
                        'subtext_farba_brandu': subtext_farba_brandu,
                        'btn_farba_brandu': btn_farba_brandu,
                        'btn_text_farba': btn_text_farba,
                        'akcent_farba': akcent_farba,
                        'nazov_nasej_firmy': nazov_nasej_firmy,
                        'logo_url': logo_url,
                        'cislo_objednavky': cislo_obj,
                        'tracking_url': f"{base_url}/",
                        'tracking_ticket_url': tracking_ticket_url,
                        'oslovenie': oslovenie,
                        'ma_prilohu': ma_prilohu,
                        'nazov_suboru': nazov_suboru if ma_prilohu else None,
                        'sprava_klienta': sprava_klienta_cista
                    }
                    html_content = render_to_string('web/email_autoresponder.html', context)
                    
                    cislo_text = f" pod číslom {cislo_obj}" if cislo_obj else ""
                    priloha_text = "\n\nPožadovaný dokument / podklady nájdete v prílohe tohto e-mailu." if ma_prilohu else ""
                    sprava_text = f"\n\nZnenie Vašej požiadavky:\n\"{sprava_klienta_cista}\"" if sprava_klienta_cista else ""
                    tracking_plain_text = f"\n\nStav zákazky môžete sledovať online na adrese:\n{tracking_ticket_url}" if tracking_ticket_url else ""
                    plain_text = (
                        f"{oslovenie}\n\n"
                        f"Ďakujeme za Váš dopyt. Radi by sme Vám potvrdili, že sme ho v poriadku prijali{cislo_text}."
                        f"{priloha_text}"
                        f"{sprava_text}"
                        f"{tracking_plain_text}\n\n"
                        f"Náš technický tím si práve prechádza Vaše zadanie a čoskoro sa Vám ozveme s návrhom riešenia.\n"
                        f"Ak máte doplňujúce informácie, pokojne odpovedzte na tento e-mail.\n\n"
                        f"S pozdravom,\nTím {nazov_nasej_firmy}"
                    )

                    odosielaci_email = (getattr(nastavenia, 'smtp_from_email', None) or nastavenia.smtp_user or '').strip()
                    try:
                        validate_email(odosielaci_email)
                    except ValidationError:
                        logger.error(f"Neplatná adresa odosielateľa '{odosielaci_email}', autoresponder pre {objednavka.id} nie je možné odoslať.")
                        return "Neplatná adresa odosielateľa."

                    odosielatel = formataddr((nazov_nasej_firmy, odosielaci_email))
                    raw_kontakt = (getattr(identita, 'email_vseobecny', None) or getattr(identita, 'email_fakturacia', None) or '').strip()
                    try:
                        validate_email(raw_kontakt)
                        kontaktny_email = raw_kontakt
                    except ValidationError:
                        kontaktny_email = odosielaci_email

                    msg = EmailMultiAlternatives(
                        subject=predmet,
                        body=plain_text,
                        from_email=odosielatel,
                        to=[klient_email],
                        reply_to=[kontaktny_email],
                        using='default'
                    )
                    msg.attach_alternative(html_content, "text/html")
                    
                    # 🚀 ISAS LEAD MAGNET FIX: Bezpečne pripojíme vopred overenú prílohu
                    if priloha_subor and nazov_suboru:
                        msg.attach(nazov_suboru, priloha_subor, mime_typ or 'application/octet-stream')
                        
                    msg.send()

                except Exception as e:
                    # 🛡️ ARCHITEKTÚRA: Zlyhanie auto-respondera zalogujeme, ale nezhodíme celý task,
                    # aby Celery nespustil retry a neposielal dispečerovi duplicitné notifikácie.
                    logger.error(f"Zlyhalo odoslanie zákazníckeho autorespondera pre objednávku {objednavka.id}: {e}", exc_info=True)
                    
        except PrijataObjednavka.DoesNotExist:
            logger.warning(f"notify_web_inquiry_task: Objednávka s ID {objednavka_id} nebola nájdená v DB.")

@shared_task(base=ISASAutoHealTask, bind=True)
def notify_new_candidate_task(self, kandidat_id):
    """
    Asynchrónne odošle notifikáciu vedeniu o novom uchádzačovi o zamestnanie.
    Chránené Redis zámkom a Auto-Heal enginom.
    """
    lock_id = f"lock_candidate_notif_{kandidat_id}"
    
    with isas_task_execution_guard(lock_id, timeout=60) as (is_locked, nastavenia, identita):
        if not is_locked:
            return "Task už beží, preskakujem duplicitu."
            
        send_tg = getattr(nastavenia, 'kandidati_notif_tg', True) if nastavenia else True
        send_mail = getattr(nastavenia, 'kandidati_notif_mail', True) if nastavenia else True
        send_discord = getattr(nastavenia, 'kandidati_notif_discord', True) if nastavenia else True

        # VÝKON: Ak sú všetky kanály vypnuté, okamžite ukončíme task bez zaťaženia DB
        if not (send_tg or send_mail or send_discord):
            return "Notifikácie pre nových uchádzačov sú v globálnych nastaveniach vypnuté."

        try:
            kandidat = Kandidat.objects.select_related('pozicia').get(pk=kandidat_id)
            raw_base = (getattr(nastavenia, 'clean_base_url', None) or getattr(settings, 'SITE_URL', '')).strip()
            if raw_base and not raw_base.startswith(('http://', 'https://')):
                raw_base = f"https://{raw_base}"
            base_url = raw_base.rstrip('/')
            admin_url = f"{base_url}/admin/Evidencia/kandidat/{kandidat.id}/change/"
            
            pozicia_nazov = kandidat.pozicia.nazov if kandidat.pozicia else "Všeobecná žiadosť (Bez pozície)"
            motivacna_sprava_formatovana = escape(kandidat.motivacna_sprava).replace(chr(10), '<br>') if kandidat.motivacna_sprava else 'Bez sprievodnej správy.'
            
            payload = (ISASNotificationBuilder(nadpis='NOVÝ UCHÁDZAČ O ZAMESTNANIE', ikona='🎓', farba_temy='#3b82f6')
                       .pridaj_riadok('👤 Meno:', kandidat.meno or 'Neuvedené')
                       .pridaj_riadok('📞 Telefón:', kandidat.telefon or 'Neuvedený')
                       .pridaj_riadok('📧 E-mail:', kandidat.email or 'Neuvedený')
                       .pridaj_riadok('🎯 Pozícia:', pozicia_nazov)
                       .nastav_hlavny_text(f"<b>Motivačná správa:</b><br>{motivacna_sprava_formatovana}")
                       .nastav_tlacidlo('Zobraziť životopis & Kartu', admin_url)
                       .nastav_ai_poznamku('Nový životopis prijatý z modulu Kariéra na verejnom webe.')
                       .build())
            
            ISASNotifier.send(
                payload=payload,
                send_tg=send_tg,
                send_mail=send_mail,
                send_discord=send_discord
            )
        except Kandidat.DoesNotExist:
            pass

@shared_task(bind=True, base=ISASAutoHealTask)
def send_ping_webhook_task(self, endpoint_id):
    """🏓 ISAS PING: Odošle testovací payload pre overenie spojenia a HMAC podpisu."""    
    try:
        endpoint = WebhookEndpoint.objects.get(pk=endpoint_id, je_aktivny=True)
    except WebhookEndpoint.DoesNotExist:
        return "Endpoint neexistuje alebo je deaktivovaný."

    event_id = str(uuid.uuid4())
    payload = {
        "event_id": event_id,
        "event_type": "PING",
        "api_version": "v1",
        "timestamp": timezone.now().isoformat(),
        "data": {
            "message": "ISAS CORE Test Ping",
            "status": "OK"
        }
    }
    send_webhook_task.delay(endpoint.id, payload, event_id)
    return "Ping odoslaný do fronty."

@shared_task(bind=True, base=ISASAutoHealTask)
def process_objednavka_webhooks_task(self, objednavka_id):
    """🧠 ISAS PAYLOAD BUILDER: Webhook pre zmenu stavu objednávky."""    
    try:
        obj = PrijataObjednavka.objects.select_related('zakaznik').get(pk=objednavka_id)
    except PrijataObjednavka.DoesNotExist:
        return "Objednávka neexistuje."

    if not obj.zakaznik:
        return "Objednávka nemá priradeného zákazníka, webhooky sa neodosielajú."

    webhooky = WebhookEndpoint.objects.filter(
        zakaznik=obj.zakaznik,
        udalost='OBJEDNAVKA_ZMENA',
        je_aktivny=True
    )

    if not webhooky.exists():
        return "Zákazník nemá aktívne webhooky pre túto udalosť."

    event_id = str(uuid.uuid4())
    payload = {
        "event_id": event_id,
        "event_type": "OBJEDNAVKA_ZMENA",
        "api_version": "v1",
        "timestamp": timezone.now().isoformat(),
        "data": {
            "objednavka_id": obj.id,
            "cislo_objednavky_interne": obj.cislo_objednavky_interne,
            "cislo_objednavky_klienta": obj.cislo_objednavky_klienta,
            "stav": obj.stav,
            "stav_display": obj.get_stav_display(),
            "priorita": obj.priorita,
            "poznamka": obj.poznamka_klienta
        }
    }

    for wh in webhooky:
        send_webhook_task.delay(wh.id, payload, event_id)
        
    return f"Vygenerované webhooky pre {webhooky.count()} endpointov."

@shared_task(bind=True, base=ISASAutoHealTask)
def process_alarm_webhooks_task(self, alarm_id):
    """🧠 ISAS PAYLOAD BUILDER: Webhook pre IoT alarmy zariadení."""    
    try:
        alarm = AlarmLog.objects.select_related('zariadenie', 'zakaznik').get(pk=alarm_id)
    except AlarmLog.DoesNotExist:
        return "Alarm neexistuje."

    if not alarm.zakaznik:
        return "Alarm nemá priradeného zákazníka, webhooky sa neodosielajú."

    webhooky = WebhookEndpoint.objects.filter(
        zakaznik=alarm.zakaznik,
        udalost='ZARIADENIE_PORUCHA',
        je_aktivny=True
    )

    if not webhooky.exists():
        return "Zákazník nemá aktívne webhooky pre túto udalosť."

    event_id = str(uuid.uuid4())
    payload = {
        "event_id": event_id,
        "event_type": "ZARIADENIE_PORUCHA",
        "api_version": "v1",
        "timestamp": timezone.now().isoformat(),
        "data": {
            "alarm_id": alarm.id,
            "zariadenie_sn": alarm.zariadenie.vyrobne_cislo if alarm.zariadenie else None,
            "model_zariadenia": alarm.zariadenie.model_zariadenia if alarm.zariadenie else None,
            "zavaznost": alarm.zavaznost,
            "kod_chyby": alarm.kod_chyby,
            "popis": alarm.popis,
            "vyriesene": alarm.vyriesene
        }
    }

    for wh in webhooky:
        send_webhook_task.delay(wh.id, payload, event_id)
        
    return f"Vygenerované webhooky pre {webhooky.count()} endpointov."

@shared_task(bind=True, base=ISASAutoHealTask)
def process_servis_webhooks_task(self, servis_id):
    """
    🧠 ISAS PAYLOAD BUILDER: Vytiahne dáta z DB a pripraví Strict Contract.
    Tento task beží na pozadí, takže neblokuje ukladanie v systéme.
    """
    try:
        zaznam = ServisnyZaznam.objects.select_related('zariadenie', 'zakaznik').prefetch_related('technici', 'typ_servisu').get(pk=servis_id)
    except ServisnyZaznam.DoesNotExist:
        return "Záznam neexistuje."

    if not zaznam.zakaznik:
        return "Servisný záznam nemá priradeného zákazníka, webhooky sa neodosielajú."

    # Zistíme, či má zákazník aktívne webhooky
    webhooky = WebhookEndpoint.objects.filter(
        zakaznik=zaznam.zakaznik,
        udalost='SERVIS_DOKONCENY',
        je_aktivny=True
    )

    if not webhooky.exists():
        return "Zákazník nemá aktívne webhooky pre túto udalosť."

    # 🛡️ STRICT CONTRACT (Zero Data Leak): Presne definujeme, čo klient uvidí
    event_id = str(uuid.uuid4())
    payload = {
        "event_id": event_id,
        "event_type": "SERVIS_DOKONCENY",
        "api_version": "v1",
        "timestamp": timezone.now().isoformat(),
        "data": {
            "servis_id": zaznam.id,
            "cislo_zaznamu": zaznam.cislo_zaznamu if hasattr(zaznam, 'cislo_zaznamu') else str(zaznam.id),
            "zariadenie": {
                "seriove_cislo": zaznam.zariadenie.vyrobne_cislo if zaznam.zariadenie else None,
                "model": zaznam.zariadenie.model_zariadenia if zaznam.zariadenie else None,
            },
            "motohodiny": {
                "celkove": zaznam.stav_motohodin,
                "v_zatazi": zaznam.motohodiny_v_zatazi
            },
            "namerane_hodnoty": zaznam.namerane_hodnoty,
            "reklamacia": {
                "koren_problemu": zaznam.koren_problemu,
                "koren_problemu_display": zaznam.get_koren_problemu_display()
            },
            "vykonane_prace": zaznam.poruchy,
            "technici": [f"{t.meno} {t.priezvisko}" for t in zaznam.technici.all()],
            "typy_servisu": [t.nazov for t in zaznam.typ_servisu.all()]
        }
    }

    # Odpálime samotné odoslanie pre každý endpoint
    for wh in webhooky:
        send_webhook_task.delay(wh.id, payload, event_id)
        
    return f"Vygenerované webhooky pre {webhooky.count()} endpointov."


@shared_task(bind=True, base=ISASAutoHealTask, max_retries=5, retry_backoff=True, retry_backoff_max=3600)
def send_webhook_task(self, endpoint_id, payload_dict, event_id):
    """
    🚀 ISAS WEBHOOK DISPATCHER: Fyzické odoslanie s meraním latencie a Circuit Breakerom.
    """    
    try:
        endpoint = WebhookEndpoint.objects.get(pk=endpoint_id, je_aktivny=True)
    except WebhookEndpoint.DoesNotExist:
        return "Endpoint neexistuje alebo je deaktivovaný."

    payload_json = json.dumps(payload_dict, ensure_ascii=False, cls=DjangoJSONEncoder)
    signature = endpoint.generuj_podpis(payload_json)
    
    pokus = getattr(self.request, 'retries', 0) + 1
    max_pokusov = (self.max_retries or 5) + 1

    headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'ISAS-Core-Webhook/2.0',
        'X-ISAS-Signature': signature,
        'X-ISAS-Event': endpoint.udalost,
        'X-ISAS-Event-ID': event_id,
        'X-ISAS-Delivery': str(self.request.id),
        'X-ISAS-Attempt': f"{pokus}/{max_pokusov}"
    }
    if isinstance(endpoint.vlastne_hlavicky, dict):
        headers.update({str(k): str(v) for k, v in endpoint.vlastne_hlavicky.items()})

    safe_headers = headers.copy()
    # 🛡️ ZERO DATA LEAK: Case-insensitive maskovanie citlivých hlavičiek
    for key in list(safe_headers.keys()):
        if key.lower() in ['authorization', 'x-api-key', 'token', 'api-key']:
            safe_headers[key] = '*** MASKED ***'

    log_entry = WebhookLog(
        endpoint=endpoint, 
        event_id=event_id,
        pokus=pokus,
        payload=payload_json,
        request_headers=json.dumps(safe_headers, indent=2)
    )

    start_time = time.time()

    try:
        if not getattr(settings, 'DEBUG', False):
            validate_safe_webhook_url(endpoint.url)
        # 🛡️ ARCHITEKTÚRA: Separovaný connect (3.05s) a read (5s) timeout + zákaz HTTP presmerovaní (SSRF ochrana)
        response = requests.post(endpoint.url, data=payload_json, headers=headers, timeout=(3.05, 5.0), allow_redirects=False)
        end_time = time.time()
        
        log_entry.trvanie_ms = int((end_time - start_time) * 1000)
        log_entry.http_status = response.status_code
        log_entry.odpoved_servera = response.text[:2000]
        log_entry.response_headers = json.dumps(dict(response.headers), indent=2)
        
        if 200 <= response.status_code < 300:
            log_entry.uspesne = True
            if (endpoint.zlyhania_po_sebe or 0) > 0:
                endpoint.zlyhania_po_sebe = 0
                endpoint.save(update_fields=['zlyhania_po_sebe'])
            log_entry.save()
            return f"Webhook úspešne odoslaný. Status: {response.status_code}"
        else:
            # 🛡️ ARCHITEKTÚRA: Klientove chyby 4xx (okrem 429 Too Many Requests) nemá zmysel opakovať
            is_client_error = (400 <= response.status_code < 500 and response.status_code != 429)
            raise requests.exceptions.RequestException(
                f"HTTP {response.status_code}", 
                response=response
            )

    except ValidationError as ssrf_err:
        log_entry.trvanie_ms = int((time.time() - start_time) * 1000)
        log_entry.uspesne = False
        log_entry.odpoved_servera = f"SSRF ochrana zablokovala URL: {str(ssrf_err)}"[:2000]
        log_entry.save()
        logger.warning(f"🚫 SSRF ochrana zablokovala webhook {endpoint.id} ({endpoint.url}): {ssrf_err}")
        return "SSRF ochrana: Nepovolená cieľová URL."

    except requests.exceptions.RequestException as e:
        end_time = time.time()
        log_entry.trvanie_ms = int((end_time - start_time) * 1000)
        log_entry.uspesne = False
        if not log_entry.odpoved_servera:
            log_entry.odpoved_servera = str(e)[:2000]
        log_entry.save()
        
        aktualne_zlyhania = (endpoint.zlyhania_po_sebe or 0) + 1
        endpoint.zlyhania_po_sebe = aktualne_zlyhania
        max_povolenych = endpoint.max_zlyhani if (endpoint.max_zlyhani is not None and endpoint.max_zlyhani > 0) else 5
        if aktualne_zlyhania >= max_povolenych:
            endpoint.je_aktivny = False
            endpoint.dovod_deaktivacie = f"Automaticky vypnuté po {max_povolenych} zlyhaniach po sebe."
            endpoint.save(update_fields=['zlyhania_po_sebe', 'je_aktivny', 'dovod_deaktivacie'])
        else:
            endpoint.save(update_fields=['zlyhania_po_sebe'])

        # Neopakujeme 4xx chyby (zlé URL, neautorizované, zlý formát), iba 5xx, 429 a výpadky siete
        status_code = getattr(getattr(e, 'response', None), 'status_code', None)
        if status_code and 400 <= status_code < 500 and status_code != 429:
            logger.warning(f"🚫 Webhook {endpoint.id} zlyhal s klientskou chybou HTTP {status_code}. Retry zrušený.")
            return f"Zlyhalo s HTTP {status_code}. Retry preskočený."
            
        raise self.retry(exc=e)
    return f"Webhook odoslaný. Status: {log_entry.http_status}"


@shared_task(base=ISASAutoHealTask, bind=True, max_retries=1)
def task_vycisti_stare_webhook_logy(self, retencia_uspesne_dni=30, retencia_chybne_dni=90):
    """
    🧹 ISAS DATA HYGIENA: Dávková retencia webhook logov (úspešné staršie ako 30 dní, chybné staršie ako 90 dní).
    Zabraňuje nárastu tabuľky WebhookLog a chráni pg_dump zálohy.
    """
    with isas_task_execution_guard('lock_cistenie_webhook_logov', timeout=300) as (acquired, gn, identita):
        if not acquired:
            logger.warning("🔒 Čistenie webhook logov už beží v inom procese. Preskakujem.")
            return 0

        now = datetime.now()
        hranica_ok = now - timedelta(days=retencia_uspesne_dni)
        hranica_err = now - timedelta(days=retencia_chybne_dni)

        celkovo_zmazane = 0

        # 1. Dávkové mazanie úspešných logov starších ako 30 dní
        while True:
            stare_ok_ids = list(WebhookLog.objects.filter(
                uspesne=True,
                cas_odoslania__lt=hranica_ok
            ).values_list('id', flat=True)[:5000])

            if not stare_ok_ids:
                break

            zmazane, _ = WebhookLog.objects.filter(id__in=stare_ok_ids).delete()
            celkovo_zmazane += zmazane

        # 2. Dávkové mazanie chybných logov starších ako 90 dní
        while True:
            stare_err_ids = list(WebhookLog.objects.filter(
                uspesne=False,
                cas_odoslania__lt=hranica_err
            ).values_list('id', flat=True)[:5000])

            if not stare_err_ids:
                break

            zmazane, _ = WebhookLog.objects.filter(id__in=stare_err_ids).delete()
            celkovo_zmazane += zmazane

        if celkovo_zmazane > 0:
            logger.info(f"Úspešne vyčistených {celkovo_zmazane} starých záznamov z WebhookLog.")
        return celkovo_zmazane