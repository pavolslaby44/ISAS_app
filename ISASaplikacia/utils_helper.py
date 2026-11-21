import base64
import math
import sys
import logging
import celery
import ipaddress
import socket
from typing import Any

from urllib.parse import urlparse
from django.core.exceptions import ValidationError
from contextlib import contextmanager
from cryptography.fernet import Fernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.cache import cache
from django.apps import apps

logger = logging.getLogger(__name__)

def validate_safe_webhook_url(value):
    """🛡️ SSRF & DNS REBINDING OCHRANA: Zablokuje lokálne adresy aj po preklade DNS."""
    if not value or not isinstance(value, str):
        raise ValidationError("Webhook URL musí byť zadaná ako platný textový reťazec.")
    parsed = urlparse(value.strip())
    if parsed.scheme != 'https' or not parsed.hostname:
        raise ValidationError("Webhook URL musí byť platná a používať bezpečný protokol HTTPS.")
    
    try:
        # Preklad všetkých IPv4 aj IPv6 záznamov domény (SSRF ochrana)
        addr_infos = socket.getaddrinfo(parsed.hostname, None)
        if not addr_infos:
            raise ValidationError("Nepodarilo sa získať IP adresu cieľovej domény.")
        for info in addr_infos:
            ip_str = str(info[4][0]).split('%')[0]
            ip_obj = ipaddress.ip_address(ip_str)
            mapped_v4 = getattr(ip_obj, 'ipv4_mapped', None)
            for kontrolovana_ip in ([ip_obj, mapped_v4] if mapped_v4 is not None else [ip_obj]):
                if (
                    not kontrolovana_ip.is_global
                    or kontrolovana_ip.is_private
                    or kontrolovana_ip.is_loopback
                    or kontrolovana_ip.is_link_local
                    or kontrolovana_ip.is_multicast
                    or kontrolovana_ip.is_reserved
                    or kontrolovana_ip.is_unspecified
                ):
                    raise ValidationError(f"Zadaná doména smeruje na internú alebo nevyhraditeľnú IP adresu ({ip_str}). Toto je z bezpečnostných dôvodov zakázané (SSRF Ochrana).")
    except (socket.gaierror, ValueError, TypeError):
        raise ValidationError("Nepodarilo sa overiť doménu (DNS preklad zlyhal alebo je neplatná adresa). Skontrolujte preklepy.")

def prepare_attachments_for_celery(files):
    """Pripraví súbory do Base64 formátu pre bezpečný prenos cez Celery."""
    if not files:
        return []
    attachments = []
    for f in files:
        if hasattr(f, 'seek'):
            f.seek(0)
        content = base64.b64encode(f.read()).decode('utf-8')
        name = getattr(f, 'name', 'priloha')
        c_type = getattr(f, 'content_type', 'application/octet-stream')
        attachments.append({'name': name, 'content': content, 'type': c_type})
    return attachments

def save_attachments_to_db(files, mail_obj, priloha_model):
    """Uloží fyzické súbory ako prílohy k e-mailovej správe v databáze."""
    if not files:
        return
    for f in files:
        if hasattr(f, 'seek'):
            f.seek(0)
        name = getattr(f, 'name', 'priloha')
        mime = getattr(f, 'content_type', 'application/octet-stream')
        priloha = priloha_model(sprava=mail_obj, nazov_suboru=name, mime_type=mime)
        priloha.subor.save(name, f)

def group_emails_by_account(maily_data):
    """Zoskupí e-maily podľa ucet_id pre optimalizáciu hromadných IMAP volaní."""
    if not maily_data:
        return {}
    ucty_dict = {}
    for m in maily_data:
        if not isinstance(m, dict):
            continue
        u_id = m.get('ucet_id')
        if u_id not in ucty_dict:
            ucty_dict[u_id] = []
        ucty_dict[u_id].append(m.get('message_id'))
    return ucty_dict

def get_cipher() -> Fernet:
    """Bezpečne inicializuje a vracia symetrickú šifru Fernet podľa nastavení aplikácie."""
    key = getattr(settings, "CRYPTOGRAPHY_KEY", None)
    if not key:
        argv = getattr(sys, "argv", [])
        if "makemigrations" in argv or "migrate" in argv:
            key = Fernet.generate_key()
        else:
            raise ImproperlyConfigured(
                "Kritická bezpečnostná chyba: Chýba settings.CRYPTOGRAPHY_KEY! "
                "Hrozí trvalá strata zašifrovaných dát."
            )
    return Fernet(key)

def vypocitaj_vzdialenost_metre(lat1, lon1, lat2, lon2):
    """📍 ISAS GEOFENCING: Výpočet vzdušnej vzdialenosti v metroch (Haversine formula)."""
    if any(v is None for v in (lat1, lon1, lat2, lon2)):
        return None
    try:
        lat1, lon1, lat2, lon2 = float(lat1), float(lon1), float(lat2), float(lon2)
        R = 6371000  # Polomer Zeme v metroch
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        delta_phi = math.radians(lat2 - lat1)
        delta_lambda = math.radians(lon2 - lon1)
        a = (
            math.sin(delta_phi / 2.0) ** 2
            + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
        )
        # Ochrana pred chybou zaokrúhľovania floatov (a > 1.0 spôsobí v math.sqrt math domain error)
        a = min(1.0, max(0.0, a))
        return R * (2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))
    except (ValueError, TypeError):
        return None

def is_float(value: Any) -> bool:
    """Overí, či je hodnota prevoditeľná na platný float (podporuje čiarku aj bodku)."""
    if value is None:
        return False
    try:
        float(str(value).replace(',', '.').strip())
        return True
    except (ValueError, TypeError, AttributeError):
        return False

# =====================================================================
# 🛡️ ISAS CELERY AUTO-HEAL ENGINE (Ochrana proti zamrznutiu taskov)
# =====================================================================

class ISASAutoHealTask(celery.Task):
    """
    🚀 ISAS ELITE ARCHITECTURE: Globálna základná trieda pre všetky Celery tasky.
    Garantuje, že pri akomkoľvek páde (Exception, Timeout) sa uvoľnia Redis zámky 
    a zresetuje sa stav v DB. Tým sa zabráni "zaseknutým" odznakom na frontende.
    """
    abstract = True

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        logger.error(f"🚨 [ISAS Celery Auto-Heal] Task {self.name} ({task_id}) zlyhal: {exc}")

        # 1. UNIVERZÁLNE UVOĽNENIE REDIS ZÁMKOV
        obj_id = args[0] if (args and len(args) > 0) else (kwargs.get('obj_id') if kwargs else None)
        zamky = ["lock_sync_isas_mail"]
        if obj_id:
            zamky.extend([
                f"lock_pdf_ponuka_{obj_id}",
                f"lock_pdf_faktura_{obj_id}",
                f"lock_ai_faktura_{obj_id}",
                f"lock_peppol_ai_{obj_id}",
                f"lock_peppol_send_{obj_id}",
                f"lock_ai_vstupka_{obj_id}",
                f"lock_mega_export_{obj_id}",
                f"lock_excel_mzdy_{obj_id}",
                f"lock_excel_sklad_{obj_id}",
                f"lock_excel_kniha_{obj_id}",
            ])
        for lock in zamky:
            try:
                cache.delete(lock)
            except Exception:
                pass

        # 2. UNIVERZÁLNY RESET DATABÁZOVÝCH STAVOV
        if obj_id:
            try:
                if 'export' in self.name or 'mzdy' in self.name or 'sklad_excel' in self.name or 'kniha_jazd' in self.name:
                    MegaZaloha = apps.get_model('Evidencia', 'MegaZaloha')
                    MegaZaloha.objects.filter(id=obj_id, stav_spracovania='spracovava_sa').update(
                        stav_spracovania='chyba',
                        poznamka=f"🚨 [SYSTÉM] Task zlyhal: {str(exc)}"
                    )
                elif 'import' in self.name:
                    ImportDatTask = apps.get_model('Evidencia', 'ImportDatTask')
                    ImportDatTask.objects.filter(id=obj_id, stav='SPRACOVAVA_SA').update(
                        stav='CHYBA',
                        chybovy_log=f"🚨 [SYSTÉM] Task zlyhal: {str(exc)}"
                    )
                elif 'peppol' in self.name and 'odosli' in self.name:
                    VystavenaFaktura = apps.get_model('Evidencia', 'VystavenaFaktura')
                    VystavenaFaktura.objects.filter(id=obj_id, peppol_status='PROCESSING').update(
                        peppol_status='ERROR',
                        peppol_error_log=f"🚨 [SYSTÉM] Task zlyhal: {str(exc)}"
                    )
            except Exception as db_err:
                logger.error(f"❌ [ISAS Auto-Heal] Zlyhal reset DB stavu: {db_err}")

        super().on_failure(exc, task_id, args, kwargs, einfo)


@contextmanager
def isas_task_execution_guard(lock_id, timeout=300, fallback_model=None, fallback_id=None, fallback_field='stav_spracovania', fallback_value='chyba', fallback_note_field='poznamka'):
    """
    ISAS CORE: Enterprise Context Manager pre Celery tasky.
    Zapuzdruje Redis locking, injektáž globálnych nastavení a fail-safe DB fallback pri páde (OOM/Timeout).
    """
    if not cache.add(lock_id, 'locked', timeout=timeout):
        yield False, None, None
        return

    try:
        GlobalneNastavenia = apps.get_model('ISASaplikacia', 'GlobalneNastavenia')
        FiremnaIdentita = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
        gn = GlobalneNastavenia.objects.first()
        identita = FiremnaIdentita.objects.first()
        
        yield True, gn, identita
        
    except BaseException as e:
        logger.error(f"❌ FATAL ERROR v tasku (Lock: {lock_id}): {str(e)}")
        if fallback_model and fallback_id:
            try:
                update_kwargs = {fallback_field: fallback_value}
                if fallback_note_field:
                    obj = fallback_model.objects.filter(id=fallback_id).first()
                    if obj and hasattr(obj, fallback_note_field):
                        old_note = getattr(obj, fallback_note_field) or ""
                        update_kwargs[fallback_note_field] = f"{old_note}\n🚨 [SYSTÉM] Chyba: {str(e)}".strip()
                fallback_model.objects.filter(id=fallback_id).update(**update_kwargs)
            except Exception as db_err:
                logger.error(f"❌ Zlyhal zápis chybového stavu do DB pre {fallback_model.__name__} ID {fallback_id}: {str(db_err)}")
        raise e
    finally:
        try:
            cache.delete(lock_id)
        except Exception as redis_err:
            logger.error(f"⚠️ Nepodarilo sa uvoľniť Redis zámok {lock_id}: {redis_err}")