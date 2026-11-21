import imaplib
import email
from django.core.mail import EmailMultiAlternatives
from django.conf import settings
import time
import logging
from typing import Tuple, List, Dict, Optional
from email.header import decode_header
from email.message import EmailMessage
from email.utils import formatdate

logger = logging.getLogger(__name__)

def connect_imap(account) -> imaplib.IMAP4_SSL:
    """Vytvorí bezpečné IMAP SSL spojenie a vráti autentifikovaný objekt."""
    try:
        # 🚀 LOKÁLNY TIMEOUT: Platí len pre toto jedno spojenie, neohrozí Celery workera!
        try:
            port = int(getattr(account, 'imap_port', None) or 993)
        except (ValueError, TypeError):
            port = 993
        imap_server = getattr(account, 'imap_server', '')
        username = str(getattr(account, 'prihlasovacie_meno', None) or getattr(account, 'email_adresa', '') or '')
        password = str(getattr(account, 'heslo', '') or '')
        mail = imaplib.IMAP4_SSL(imap_server, port, timeout=30)
        mail.login(username, password)
        return mail
    except Exception as e: # Zmenené na Exception, aby sme zachytili aj socket.timeout
        logger.error(f"IMAP Login Error pre {getattr(account, 'email_adresa', 'neznámy')}: {e}")
        raise

def clean_header(header_value: str) -> str:
    """Bezpečne dekóduje hlavičky (Predmet, Odosielateľ) s extrémnou diakritikou."""
    if not header_value:
        return "Neznáme"
    try:
        decoded_fragments = decode_header(header_value)
        header_string = ""
        for fragment, encoding in decoded_fragments:
            if isinstance(fragment, bytes):
                # Fallback mechanizmus dekódovania
                try:
                    header_string += fragment.decode(encoding or 'utf-8', errors='replace')
                except LookupError:
                    header_string += fragment.decode('windows-1250', errors='replace')
            else:
                header_string += str(fragment)
        return header_string.replace('\r', '').replace('\n', '').strip()
    except Exception as e:
        logger.warning(f"Nepodarilo sa dekódovať hlavičku '{header_value}': {e}")
        return str(header_value)

def extract_email_content(msg) -> Tuple[str, str, List[Dict]]:
    """Extrahuje čistý text, HTML a pamäťové objekty príloh."""
    if not msg:
        return "", "", []
    body_text, body_html = "", ""
    attachments = []

    for part in msg.walk():
        if part.get_content_maintype() == 'multipart':
            continue
            
        content_disposition = str(part.get("Content-Disposition", ""))
        mime_type = part.get_content_type()
        filename = part.get_filename()
        
        # Ošetrenie príloh (Aj inline obrázkov)
        payload = part.get_payload(decode=True)
        if ("attachment" in content_disposition or filename) and payload:
            clean_name = clean_header(filename) if filename else f"priloha_{int(time.time())}.bin"
            attachments.append({
                'filename': clean_name,
                'data': payload,
                'mime': mime_type
            })
        else:
            # Extrakcia tela e-mailu
            charset = part.get_content_charset() or 'utf-8'
            try:
                if payload:
                    if isinstance(payload, bytes):
                        try:
                            decoded_payload = payload.decode(charset, errors='replace')
                        except LookupError:
                            decoded_payload = payload.decode('utf-8', errors='replace')
                    else:
                        decoded_payload = str(payload)
                    if mime_type == 'text/plain':
                        body_text += decoded_payload
                    elif mime_type == 'text/html':
                        body_html += decoded_payload
            except Exception as e:
                logger.debug(f"Chyba pri čítaní tela správy (charset: {charset}): {e}")

    return body_text.strip(), body_html.strip(), attachments

import base64

def _build_email_message(account, to_addr: str, subject: str, html_content: str, cc: str = "", bcc: str = "", attachments: Optional[list] = None, message_id: Optional[str] = None) -> EmailMessage:
    """Pomocná funkcia na zostavenie EmailMessage objektu s prílohami a kópiami."""
    msg = EmailMessage()
    full_html = html_content or ""
    if getattr(account, 'podpis_html', None):
        full_html = f"{full_html}<br><br>{account.podpis_html}" if full_html else account.podpis_html

    msg.set_content("Tento e-mail vyžaduje HTML klienta.")
    msg.add_alternative(full_html, subtype='html')
    
    msg['Subject'] = str(subject or "").replace('\r', '').replace('\n', '').strip()
    msg['From'] = str(getattr(account, 'email_adresa', '') or "").replace('\r', '').replace('\n', '').strip()
    msg['To'] = str(to_addr or "").replace('\r', '').replace('\n', '').strip()
    if cc:
        msg['Cc'] = str(cc).replace('\r', '').replace('\n', '').strip()
    if bcc:
        msg['Bcc'] = str(bcc).replace('\r', '').replace('\n', '').strip()
    msg['Date'] = formatdate(localtime=True)
    
    # 🚀 PRIDANÉ: Vlastné Message-ID pre neskoršiu identifikáciu konceptu
    if message_id:
        clean_mid = str(message_id).strip('<> \t\r\n').replace('"', '').replace('\\', '').replace('\r', '').replace('\n', '')
        if clean_mid:
            msg['Message-ID'] = f"<{clean_mid}>"

    if attachments:
        for att in attachments:
            if not isinstance(att, dict):
                logger.warning(f"Neplatná príloha, očakávaný dict, prijaté: {type(att)}")
                continue
            att_name = str(att.get('name') or att.get('filename') or 'priloha.bin').replace('\r', '').replace('\n', '').strip()
            try:
                raw_content = att.get('content') if att.get('content') is not None else att.get('data')
                if isinstance(raw_content, bytes):
                    file_data = raw_content
                elif isinstance(raw_content, str):
                    try:
                        file_data = base64.b64decode(raw_content)
                    except Exception:
                        file_data = raw_content.encode('utf-8', errors='replace')
                else:
                    file_data = bytes(raw_content or b'')

                att_type = str(att.get('type') or att.get('mime') or 'application/octet-stream').strip()
                maintype, subtype = att_type.split('/', 1) if '/' in att_type else ('application', 'octet-stream')
                filename = att_name or 'priloha.bin'
                msg.add_attachment(file_data, maintype=maintype, subtype=subtype, filename=filename)
            except Exception as e:
                logger.error(f"Chyba pri pripájaní súboru {att_name}: {e}")
                
    return msg

def send_email_smtp(account, to_addr: str, subject: str, html_content: str, cc: str = "", bcc: str = "", attachments: Optional[list] = None) -> bool:
    """Odošle email cez ISAS Dynamic Email Backend a uloží kópiu do IMAP Odoslaných."""
    if not any([to_addr, cc, bcc]):
        raise ValueError("Nie je zadaný žiadny príjemca (To, Cc ani Bcc).")

    # 1. Príprava adresátov
    to_list = [addr.strip() for addr in str(to_addr).split(',') if addr.strip()] if to_addr else []
    cc_list = [addr.strip() for addr in str(cc).split(',') if addr.strip()] if cc else []
    bcc_list = [addr.strip() for addr in str(bcc).split(',') if addr.strip()] if bcc else []
    from_email = str(getattr(account, 'email_adresa', '') or '').strip() or None

    # 2. Zostavenie HTML obsahu vrátane podpisu účtu
    full_html = html_content or ""
    if getattr(account, 'podpis_html', None):
        full_html = f"{full_html}<br><br>{account.podpis_html}" if full_html else account.podpis_html

    clean_subject = str(subject or "").replace('\r', '').replace('\n', '').strip()

    # 3. Odoslanie cez polymorfné rozhranie Django 6.1 (ISASDynamicEmailBackend)
    django_msg = EmailMultiAlternatives(
        subject=clean_subject,
        body="Tento e-mail vyžaduje HTML klienta.",
        from_email=from_email,
        to=to_list,
        cc=cc_list,
        bcc=bcc_list,
    )
    django_msg.attach_alternative(full_html, "text/html")

    # Spracovanie a priloženie príloh
    if attachments:
        for att in attachments:
            if not isinstance(att, dict):
                continue
            att_name = str(att.get('name') or att.get('filename') or 'priloha.bin').replace('\r', '').replace('\n', '').strip()
            raw_content = att.get('content') if att.get('content') is not None else att.get('data')
            if isinstance(raw_content, str):
                try:
                    file_data = base64.b64decode(raw_content)
                except Exception:
                    file_data = raw_content.encode('utf-8', errors='replace')
            elif isinstance(raw_content, bytes):
                file_data = raw_content
            else:
                file_data = bytes(raw_content or b'')

            att_type = str(att.get('type') or att.get('mime') or 'application/octet-stream').strip()
            django_msg.attach(att_name, file_data, att_type)

    try:
        django_msg.send()
    except Exception as e:
        logger.error(f"Odoslanie správy zlyhalo pre {from_email}: {e}")
        raise

    # 4. Uloženie odoslanej správy priamo do IMAP Odoslaných (čisté MIME bajty z Django správy)
    imap = None
    try:
        imap = connect_imap(account)
        sent_folders = ['"[Gmail]/Odoslan&AOk-"', '"[Gmail]/Sent Mail"', 'Sent', 'Odoslane', 'INBOX.Sent']
        raw_bytes = django_msg.message().as_bytes()
        for sf in sent_folders:
            status, _ = imap.append(sf, '\\Seen', imaplib.Time2Internaldate(time.time()), raw_bytes)
            if status == 'OK':
                break
    except Exception as e:
        logger.error(f"Nepodarilo sa uložiť kópiu do IMAP: {e}")
    finally:
        try:
            if imap:
                imap.logout()
        except Exception:
            pass

    return True

def save_draft_imap(account, to_addr: str, subject: str, html_content: str, cc: str = "", bcc: str = "", attachments: Optional[list] = None, message_id: Optional[str] = None):
    """Uloží koncept priamo do zložky Koncepty na IMAP serveri."""
    msg = _build_email_message(account, to_addr, subject, html_content, cc, bcc, attachments, message_id)
    if 'Date' in msg: msg.replace_header('Date', formatdate(localtime=True))
    
    imap = None
    try:
        imap = connect_imap(account)
        draft_folders = ['"[Gmail]/Koncepty"', '"[Gmail]/Drafts"', 'Drafts', 'Koncepty', 'INBOX.Drafts']
        for df in draft_folders:
            status, _ = imap.append(df, '(\\Draft \\Seen)', imaplib.Time2Internaldate(time.time()), msg.as_bytes())
            if status == 'OK': break
    except Exception as e:
        logger.error(f"Nepodarilo sa uložiť koncept do IMAP: {e}")
    finally:
        try: 
            if imap: imap.logout()
        except Exception: pass

# 🚀 PRIDANÉ: Nová funkcia na bezpečné zmazanie starého konceptu z IMAPu
def delete_draft_imap(account, message_id: str):
    """Vyhľadá koncept podľa Message-ID v zložke Koncepty a trvalo ho vymaže."""
    if not message_id: return
    imap = None
    try:
        imap = connect_imap(account)
        draft_folders = ['"[Gmail]/Koncepty"', '"[Gmail]/Drafts"', 'Drafts', 'Koncepty', 'INBOX.Drafts']
        for df in draft_folders:
            status, _ = imap.select(df)
            if status == 'OK':
                # Vyhľadáme presne ten koncept, ktorý chceme zmazať
                clean_mid = str(message_id).strip('<> \t\r\n').replace('"', '').replace('\\', '').replace('\r', '').replace('\n', '')
                if not clean_mid:
                    break
                search_status, search_data = imap.search(None, f'(HEADER Message-ID "{clean_mid}")')
                if search_status == 'OK' and search_data and search_data[0]:
                    for num in search_data[0].split():
                        imap.store(num, '+FLAGS', '\\Deleted')
                    imap.expunge()
                    break
    except Exception as e:
        logger.error(f"IMAP Delete Draft Error: {e}")
    finally:
        try:
            if imap: imap.logout()
        except Exception: pass

def mark_imap_read(account, uid: str):
    """Označiť správu ako prečítanú na serveri (bezpečné pre UID aj Message-ID)."""
    imap = None
    try:
        imap = connect_imap(account)
        imap.select('INBOX')
        _apply_flags_safe(imap, [uid], '\\Seen')
    except Exception as e: 
        logger.error(f"IMAP Read Error ({uid}): {e}")
    finally:
        try: 
            if imap: imap.logout()
        except Exception: pass

def trash_imap_email(account, uid: str):
    """Presunúť do koša (Gmail behavior - deleted from Inbox, bezpečné pre UID aj Message-ID)."""
    imap = None
    try:
        imap = connect_imap(account)
        imap.select('INBOX')
        _apply_flags_safe(imap, [uid], '\\Deleted')
        imap.expunge()
    except Exception as e: 
        logger.error(f"IMAP Trash Error ({uid}): {e}")
    finally:
        try: 
            if imap: imap.logout()
        except Exception: pass

def _apply_flags_safe(imap, ids: list, flag: str = '\\Deleted'):
    """Univerzálna pomocná funkcia: spracuje číselné UID aj textové Message-ID bez chyby IMAP syntaxe."""
    for item in ids:
        if not item:
            continue
        item_str = str(item).strip()
        if not item_str:
            continue
        if item_str.isdigit():
            imap.uid('STORE', item_str.encode('utf-8'), '+FLAGS', flag)
        else:
            clean_mid = item_str.strip('<> \t\r\n').replace('"', '').replace('\\', '').replace('\r', '').replace('\n', '')
            if not clean_mid:
                continue
            status, data = imap.search(None, f'(HEADER Message-ID "{clean_mid}")')
            if status == 'OK' and data and data[0]:
                for num in data[0].split():
                    imap.store(num, '+FLAGS', flag)

def bulk_trash_imap(account, uids: list):
    """Hromadne presunie zoznam správ do koša na IMAP serveri."""
    if not uids: return
    imap = None
    try:
        imap = connect_imap(account)
        imap.select('INBOX')
        _apply_flags_safe(imap, uids, '\\Deleted')
        imap.expunge()
    except Exception as e: 
        logger.error(f"IMAP Bulk Trash Error: {e}")
    finally:
        try: 
            if imap: imap.logout()
        except Exception: pass

def bulk_delete_permanently_imap(account, uids: list):
    """Trvalo odstráni vybrané správy z koša na IMAP serveri."""
    if not uids: return
    imap = None
    try:
        imap = connect_imap(account)
        trash_folder = _find_trash_folder(imap)
        if trash_folder:
            _apply_flags_safe(imap, uids, '\\Deleted')
            imap.expunge()
    except Exception as e: 
        logger.error(f"IMAP Bulk Perm Delete Error: {e}")
    finally:
        try:
            if imap: 
                imap.logout()
        except Exception: 
            pass

def _find_trash_folder(imap):
    # 🚀 OPRAVA 4: Pridané IMAP UTF-7 kódovanie pre slovenskú diakritiku
    for tf in ['"[Gmail]/K&APQ-&AWE-"', '"[Gmail]/Trash"', 'Trash', 'Deleted', 'INBOX.Trash']:
        status, _ = imap.select(tf)
        if status == 'OK': return tf
    return None

def _restore_safe(imap, ids: list, target_folder: str = 'INBOX'):
    """Univerzálna pomocná funkcia na obnovu z koša: bezpečne kopíruje a maže UID aj Message-ID."""
    for item in ids:
        if not item:
            continue
        item_str = str(item).strip()
        if not item_str:
            continue
        if item_str.isdigit():
            imap.uid('COPY', item_str.encode('utf-8'), target_folder)
            imap.uid('STORE', item_str.encode('utf-8'), '+FLAGS', '\\Deleted')
        else:
            clean_mid = item_str.strip('<> \t\r\n').replace('"', '').replace('\\', '').replace('\r', '').replace('\n', '')
            if not clean_mid:
                continue
            status, data = imap.search(None, f'(HEADER Message-ID "{clean_mid}")')
            if status == 'OK' and data and data[0]:
                for num in data[0].split():
                    imap.copy(num, target_folder)
                    imap.store(num, '+FLAGS', '\\Deleted')

def restore_imap(account, uid: str):
    imap = None
    try:
        imap = connect_imap(account)
        if _find_trash_folder(imap):
            _restore_safe(imap, [uid], 'INBOX')
            imap.expunge()
    except Exception as e: 
        logger.error(f"IMAP Restore Error: {e}")
    finally:
        try: 
            if imap: 
                imap.logout()
        except Exception: 
            pass

def delete_permanently_imap(account, uid: str):
    imap = None
    try:
        imap = connect_imap(account)
        if _find_trash_folder(imap):
            _apply_flags_safe(imap, [uid], '\\Deleted')
            imap.expunge()
    except Exception as e: 
        logger.error(f"IMAP Perm Delete Error: {e}")
    finally:
        try:
            if imap: 
                imap.logout()
        except Exception: 
            pass

def bulk_restore_imap(account, uids: list):
    if not uids: return
    imap = None
    try:
        imap = connect_imap(account)
        if _find_trash_folder(imap):
            _restore_safe(imap, uids, 'INBOX')
            imap.expunge()
    except Exception as e: 
        logger.error(f"IMAP Bulk Restore Error: {e}")
    finally:
        try: 
            if imap: 
                imap.logout()
        except Exception: 
            pass

def empty_trash_imap(account):
    """Vysype celý kôš priamo na IMAP serveri."""
    imap = None
    try:
        imap = connect_imap(account)
        # 🚀 OPRAVA 5: Pridané IMAP UTF-7 kódovanie pre slovenskú diakritiku
        trash_folders = ['"[Gmail]/K&APQ-&AWE-"', '"[Gmail]/Trash"', 'Trash', 'Deleted', 'INBOX.Trash']
        for tf in trash_folders:
            status, select_data = imap.select(tf)
            if status == 'OK':
                try:
                    count = int(select_data[0].decode() if isinstance(select_data[0], bytes) else select_data[0])
                except (ValueError, TypeError, IndexError):
                    count = 0
                
                # Ak v koši sú správy, zmažeme ich v O(1) čase bez ťahania zoznamu ID
                if count > 0:
                    imap.store('1:*', '+FLAGS', '\\Deleted')
                    imap.expunge()
                break
    except Exception as e: 
        logger.error(f"IMAP Empty Trash Error: {e}")
    finally:
        try: 
            if imap: imap.logout()
        except Exception: pass