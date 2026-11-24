import os
import sys
import json
import logging
import uuid
import re
import time
import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo
from typing import Any

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ISASaplikacia.settings')
import django
django.setup()

import discord
from discord.ext import commands, tasks
from asgiref.sync import sync_to_async

from django.conf import settings
from django.core.cache import cache
from django.core.files import File
from django.db.models import Q
from django.core.files.base import ContentFile
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone

from Evidencia.industry_loader import CATEGORY_MAP, CATEGORY_CHOICES, get_industry_term
from Evidencia.utils_helper import (
    LedgerKalkulacka, 
    ocisti_nazov_od_jednotky, 
    clean_and_parse_ai_json,
    safe_discord,
    parse_int,
    parse_decimal,
    parse_bool,
    get_firemna_mena
)
from Evidencia.utils import ISASNotificationBuilder, ISASNotifier
from Evidencia.services import ask_isas_ai
from Evidencia.models import (
    StockMovement, Zamestnanec, Vozidlo, 
    Zakaznik, EvidenciaJazd, Tankovanie,
    SukromnaOsoba, ServisnyZaznam, ServisovaneZariadenie, TypServisuPrace,
    PrichodziaFaktura, Uloha, PovolenieNaVstup, Filialka, Dodavatel, AIAuditLog,
    PouzityMaterialVZaznamoch
)
from ISASaplikacia.models import FiremnaIdentita, GlobalneNastavenia
from ISASaplikacia.utils import vypocitaj_ai_cenu_usd

from telegram_bot import (
    MODEL_NAME_MAP,
    get_zoznam_filialok_pre_bot as get_zoznam_filialok_pre_bot_async,
    get_model_class_by_name as get_model_class_by_name_async,
    get_stavy_pre_bota as get_stavy_pre_bota_async,
    get_hlavny_sklad_adresa as get_hlavny_sklad_adresa_async,
    vyhladaj_zakaznika_pre_bot as vyhladaj_zakaznika_pre_bot_async,
    vyhladaj_cerpacku as vyhladaj_cerpacku_async,
    get_zoznam_cerpaciek as get_zoznam_cerpaciek_async,
    get_domaci_stat_a_mena_pre_bot as get_domaci_stat_a_mena_pre_bot_async,
    get_zariadenia_pre_zakaznika_bot,
    get_typy_servisu_bot,
    skontroluj_suvisiace_ulohy
)

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

# Trvalá pamäť pre Discord (odolnosť voči reštartu)
DISCORD_STATE_FILE = os.path.join(settings.BASE_DIR, 'data', 'discord_states.json')

def load_user_states():
    if os.path.exists(DISCORD_STATE_FILE):
        try:
            with open(DISCORD_STATE_FILE, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
                # 🛡️ DEFENSIVE: Prevod len platných číselných ID, aby jedna neplatná hodnota nezmazala celú pamäť
                states = {}
                if isinstance(loaded, dict):
                    for k, v in loaded.items():
                        try:
                            states[int(k)] = v
                        except (ValueError, TypeError):
                            continue
                return states
        except Exception as e:
            logging.error(f"Chyba pri načítaní Discord pamäte: {e}")
    return {}

def save_user_states():
    dir_name = os.path.dirname(DISCORD_STATE_FILE)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    temp_file = f"{DISCORD_STATE_FILE}.tmp_{uuid.uuid4().hex[:6]}"
    try:
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(USER_STATES, f, ensure_ascii=False, indent=2)
        os.replace(temp_file, DISCORD_STATE_FILE)
    except Exception as e:
        logging.error(f"Chyba pri ukladaní Discord pamäte: {e}")
        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except Exception:
                pass

# Pamäť pre stavy používateľov (Načítaná zo súboru)
USER_STATES = load_user_states()

# --- DISCORD SETUP ---
# Zapíname Intents, aby bot mohol čítať správy
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

@sync_to_async
def update_radar_cache_discord(discord_id, status):
    """Zápis do Redis cache pre dispečing v jednotnom ISAS formáte."""
    if not discord_id:
        return
    z = Zamestnanec.objects.filter(discord_id=discord_id).first()
    if z:
        zaznam = cache.get(f'isas_radar_tech_{z.id}') or {}
        zaznam['status'] = status
        zaznam['timestamp'] = time.time()
        cache.set(f'isas_radar_tech_{z.id}', zaznam, timeout=86400)

@sync_to_async
def get_radar_status_discord(discord_id):
    """Overí, či technik v posledných 12 hodinách už interagoval s radarom."""
    if not discord_id:
        return False
    z = Zamestnanec.objects.filter(discord_id=discord_id).first()
    if z:
        zaznam = cache.get(f'isas_radar_tech_{z.id}')
        if zaznam and (time.time() - zaznam.get('timestamp', 0)) < 43200:
            return True
    return False

@sync_to_async
def identifikuj_pouzivatela_discord(discord_id):
    """Zistí, či už je Discord ID spárované s nejakým profilom."""
    if not discord_id:
        return None, None
    z = Zamestnanec.objects.select_related('user').filter(discord_id=discord_id, je_anonymizovany=False).first()
    if z: return z, "ZAMESTNANEC"
    s = SukromnaOsoba.objects.filter(discord_id=discord_id, je_anonymizovany=False).first()
    if s: return s, "SUKROMNA"
    return None, None

@sync_to_async
def sparuj_discord_ucet(telefon, discord_id):
    """Spáruje účet na základe telefónneho čísla (náhrada za Zdieľať kontakt)."""
    if not discord_id:
        return None, None
    ciste_cislo = re.sub(r'\D', '', str(telefon))
    if len(ciste_cislo) < 9:
        return None, None
        
    ciste_cislo = ciste_cislo[-9:]
    
    with transaction.atomic():
        z = Zamestnanec.objects.filter(je_anonymizovany=False, telefon__icontains=ciste_cislo).first()
        if not z:
            for cand in Zamestnanec.objects.filter(je_anonymizovany=False, telefon__isnull=False):
                if re.sub(r'\D', '', str(cand.telefon or '')).endswith(ciste_cislo):
                    z = cand
                    break

        if z:
            Zamestnanec.objects.filter(discord_id=discord_id).exclude(pk=z.pk).update(discord_id=None)
            SukromnaOsoba.objects.filter(discord_id=discord_id).update(discord_id=None)
            z.discord_id = discord_id
            z.save(update_fields=['discord_id'])
            return z, "ZAMESTNANEC"
            
        s = SukromnaOsoba.objects.filter(je_anonymizovany=False, telefon__icontains=ciste_cislo).first()
        if not s:
            for cand in SukromnaOsoba.objects.filter(je_anonymizovany=False, telefon__isnull=False):
                if re.sub(r'\D', '', str(cand.telefon or '')).endswith(ciste_cislo):
                    s = cand
                    break

        if s:
            Zamestnanec.objects.filter(discord_id=discord_id).update(discord_id=None)
            SukromnaOsoba.objects.filter(discord_id=discord_id).exclude(pk=s.pk).update(discord_id=None)
            s.discord_id = discord_id
            s.save(update_fields=['discord_id'])
            return s, "SUKROMNA"
        
    return None, None

@sync_to_async
def ziskaj_otvorene_ulohy_pre_technika_discord(discord_id):
    """ISAS CORE: Vytiahne živé úlohy pre konkrétneho zamestnanca (Discord verzia)."""
    if not discord_id:
        return None, "Neznámy technik"
    technik = Zamestnanec.objects.filter(discord_id=discord_id).first()
    if not technik: return None, "Neznámy technik"

    ulohy = Uloha.objects.filter(
        technici=technik, 
        dokoncene=False
    ).select_related('zakaznik').prefetch_related('typ_servisu', 'zariadenia').order_by('zaciatok')[:10]
    
    vysledok = []
    for u in ulohy:
        typy = ", ".join([str(t) for t in u.typ_servisu.all()]) or "Všeobecná práca"
        zariadenia_text = ", ".join([str(z) for z in u.zariadenia.all()]) or "Neuvedené"
        zakaznik = u.zakaznik.Nazov_spolocnosti if u.zakaznik else "Bez zákazníka"
        # 🛡️ DEFENSIVE DUAL-MODE: Bezpečné ošetrenie pre USE_TZ=True aj USE_TZ=False
        if u.zaciatok:
            c_cas = timezone.localtime(u.zaciatok) if (u.zaciatok and timezone.is_aware(u.zaciatok)) else u.zaciatok
            zaciatok = c_cas.strftime('%d.%m.%Y o %H:%M')
        else:
            zaciatok = "Nezadaný čas"
        info = u.blizsie_informacie if u.blizsie_informacie else "Bez popisu."
        
        vysledok.append({
            'id': u.id, 'zakaznik': zakaznik, 'zaciatok': zaciatok,
            'typy': typy, 'zariadenia': zariadenia_text, 'info': info
        })
        
    return vysledok, technik.meno

@sync_to_async
def ziskaj_zajtrajsie_ulohy_pre_discord():
    zajtra = timezone.now().date() + datetime.timedelta(days=1)
    
    filter_kwargs = {'discord_id__isnull': False}
    if hasattr(Zamestnanec, 'discord_upozornenia_ulohy'):
        filter_kwargs['discord_upozornenia_ulohy'] = True
    else:
        filter_kwargs['tg_upozornenia_ulohy'] = True
    technici = Zamestnanec.objects.filter(**filter_kwargs)
    
    data_na_odoslanie = []
    for t in technici:
        ulohy = Uloha.objects.filter(technici=t, dokoncene=False, zaciatok__date=zajtra).select_related('zakaznik').prefetch_related('typ_servisu').order_by('zaciatok')
        if ulohy.exists():
            zoznam = []
            for u in ulohy:
                c_cas = timezone.localtime(u.zaciatok) if (u.zaciatok and timezone.is_aware(u.zaciatok)) else u.zaciatok
                cas_str = c_cas.strftime('%H:%M') if c_cas else "Nezadaný čas"
                zoznam.append({
                    'cas': cas_str,
                    'zakaznik': u.zakaznik.Nazov_spolocnosti if u.zakaznik else "Interná",
                    'praca': ", ".join([str(tp) for tp in u.typ_servisu.all()]) or "Všeobecná práca"
                })
            data_na_odoslanie.append({'discord_id': t.discord_id, 'meno': t.meno, 'ulohy': zoznam})
    return data_na_odoslanie

ask_isas_ai_async = sync_to_async(ask_isas_ai)

@sync_to_async
def get_predvolena_cerpacka_discord(discord_id):
    if not discord_id:
        return None, None
    zamestnanec = Zamestnanec.objects.filter(discord_id=discord_id).first()
    if zamestnanec and zamestnanec.predvolena_cerpacka:
        return zamestnanec.predvolena_cerpacka.id, zamestnanec.predvolena_cerpacka.Nazov_spolocnosti
    osoba = SukromnaOsoba.objects.filter(discord_id=discord_id).first()
    if osoba and osoba.predvolena_cerpacka:
        return osoba.predvolena_cerpacka.id, osoba.predvolena_cerpacka.Nazov_spolocnosti
    return None, None

@sync_to_async
def vymaz_predvolenu_cerpacku_discord(discord_id):
    if not discord_id:
        return
    zamestnanec = Zamestnanec.objects.filter(discord_id=discord_id).first()
    if zamestnanec:
        zamestnanec.predvolena_cerpacka = None
        zamestnanec.save(update_fields=['predvolena_cerpacka'])
    osoba = SukromnaOsoba.objects.filter(discord_id=discord_id).first()
    if osoba:
        osoba.predvolena_cerpacka = None
        osoba.save(update_fields=['predvolena_cerpacka'])

@sync_to_async
def get_stavy_notifikacii_discord(discord_id):
    if not discord_id:
        return None, None
    technik = Zamestnanec.objects.filter(discord_id=discord_id).first()
    if technik:
        stav_discord = getattr(technik, 'discord_upozornenia_ulohy', technik.tg_upozornenia_ulohy)
        return stav_discord, technik.mail_upozornenia_ulohy
    return None, None

@sync_to_async
def prepni_nastavenie_notifikacii_discord(discord_id, platforma):
    if not discord_id:
        return False
    technik = Zamestnanec.objects.filter(discord_id=discord_id).first()
    if technik:
        if platforma in ['discord', 'tg']:
            field_name = 'discord_upozornenia_ulohy' if hasattr(technik, 'discord_upozornenia_ulohy') else 'tg_upozornenia_ulohy'
            setattr(technik, field_name, not getattr(technik, field_name, False))
            technik.save(update_fields=[field_name])
        elif platforma == 'mail':
            technik.mail_upozornenia_ulohy = not technik.mail_upozornenia_ulohy
            technik.save(update_fields=['mail_upozornenia_ulohy'])
    return True

@sync_to_async
def get_dnesne_vstupy_discord(discord_id):
    if not discord_id:
        return []
    dnes = timezone.now().date()
    technik = Zamestnanec.objects.filter(discord_id=discord_id).first()
    if not technik: return []
    
    vstupy = PovolenieNaVstup.objects.filter(
        zamestnanci=technik,
        datum_vstupu__gte=dnes
    ).select_related('zakaznik').prefetch_related('zamestnanci', 'vozidla').order_by('datum_vstupu')
    
    vysledok = []
    for v in vstupy:
        pdf_cesta = None
        if v.vygenerovany_dokument and v.vygenerovany_dokument.name:
            try:
                pdf_cesta = v.vygenerovany_dokument.path
            except (ValueError, NotImplementedError, AttributeError):
                pdf_cesta = None

        fabrika_nazov = v.zakaznik.Nazov_spolocnosti if v.zakaznik else "Neznámy zákazník"
        datum_str = v.datum_vstupu.strftime('%d.%m.%Y') if v.datum_vstupu else "Nedatované"
        instrukcie_text = v.zakaznik.instrukcie_vstupu if v.zakaznik else ""

        vysledok.append({
            'id': v.id, 'fabrika': fabrika_nazov,
            'datum': datum_str,
            'stav': v.stav, 'stav_display': v.get_stav_display(),
            'kolegovia': ", ".join([f"{z.meno or ''} {z.priezvisko or ''}".strip() for z in v.zamestnanci.all() if z.meno or z.priezvisko]) or "Bez posádky",
            'auta': ", ".join([a.spz for a in v.vozidla.all() if getattr(a, 'spz', None)]) or "Bez vozidla",
            'instrukcie': instrukcie_text,
            'pdf_cesta': pdf_cesta,
        })
    return vysledok

@sync_to_async
def get_vozidla_list_discord():
    return list(Vozidlo.objects.all()[:25])

@sync_to_async
def zmenit_auto_vstup_db_discord(povolenie_id, auto_id, discord_id):
    try:
        povolenie = PovolenieNaVstup.objects.get(id=povolenie_id, zamestnanci__discord_id=discord_id)
        nove_auto = Vozidlo.objects.get(id=auto_id)
        povolenie.vozidla.clear()
        povolenie.vozidla.add(nove_auto)
        ma_email_vratnice = bool(povolenie.zakaznik and getattr(povolenie.zakaznik, 'email_vratnice', None))
        return True, nove_auto.spz, ma_email_vratnice
    except ObjectDoesNotExist:
        return False, None, False

@sync_to_async
def ulozit_fakturu_z_discordu(discord_id, file_bytes, filename):
    user = Zamestnanec.objects.filter(discord_id=discord_id).first() or SukromnaOsoba.objects.filter(discord_id=discord_id).first()
    meno = getattr(user, 'meno', None) or getattr(user, 'meno_a_priezvisko', 'Neznámy používateľ')
    faktura = PrichodziaFaktura(
        zdroj='DISCORD',
        poznamka=f"Nahraté cez Discord (Meno: {meno})"
    )
    # 🛡️ DEFENSIVE: Uloženie súboru bez predčasného save(), aby AI task neodpálil na prázdny doklad
    faktura.subor.save(filename, ContentFile(file_bytes), save=False)
    faktura.save()
    return faktura.id

@sync_to_async
def skontroluj_fakturu_po_ai(faktura_id):
    f = PrichodziaFaktura.objects.filter(id=faktura_id).first()
    return f.poznamka if f else None

@sync_to_async
def vytvor_zaznam_cez_ai_discord(discord_id, ai_json_data, zakaznik_id, zariadenie_id, typ_servisu_id, cart_data=None, foto_pred_path=None, foto_po_path=None, video_path=None):
    technik = Zamestnanec.objects.filter(discord_id=discord_id).first()
    if not technik: return False, "Nenašiel som tvoj profil.", None

    zakaznik = Zakaznik.objects.filter(id=zakaznik_id).first()
    zariadenie = ServisovaneZariadenie.objects.filter(id=zariadenie_id).first()
    typ_servisu = TypServisuPrace.objects.filter(id=typ_servisu_id).first()

    poruchy_text = ai_json_data.get('poruchy_a_poznamky') or ""
    if str(poruchy_text).strip().lower() == "null": poruchy_text = ""
    
    material_text = ai_json_data.get('pouzity_material') or ""
    if str(material_text).strip().lower() == "null": material_text = ""
    
    finalna_poznamka = f"{poruchy_text}\n\n📦 Nahlásený materiál (mimo skladu):\n{material_text}".strip() if material_text else poruchy_text.strip()

    namerane_hodnoty = ai_json_data.get('namerane_hodnoty') or ""
    if str(namerane_hodnoty).strip().lower() == "null": namerane_hodnoty = ""

    stav_mh = parse_int(ai_json_data.get('motohodiny'), default=None)
    stav_mh_zataz = parse_int(ai_json_data.get('motohodiny_v_zatazi'), default=None)
    zmena_hardveru = parse_bool(ai_json_data.get('zmena_hardveru', False))

    ulozene_django_subory = []

    try:
        with transaction.atomic():
            zaznam = ServisnyZaznam.objects.create(
                zakaznik=zakaznik, zariadenie=zariadenie,
                stav_motohodin=stav_mh, motohodiny_v_zatazi=stav_mh_zataz,
                poruchy=finalna_poznamka, namerane_hodnoty=namerane_hodnoty, 
                zmena_hardveru=zmena_hardveru, dokoncene=True
            )
            zaznam.technici.add(technik)

            if typ_servisu:
                zaznam.typ_servisu.add(typ_servisu)
                zaznam.vygeneruj_prediktivne_plany()

            if cart_data:
                materialy_na_vytvorenie = []
                for key, item in cart_data.items():
                    model_class, _, _ = MODEL_NAME_MAP.get(item['model_name'], (None, None, None))
                    
                    if model_class:
                        ct = ContentType.objects.get_for_model(model_class)
                        raw_pk = item.get('pk')
                        clean_pk = int(raw_pk) if raw_pk is not None and str(raw_pk).isdigit() else None
                        diel_obj = model_class.objects.filter(pk=clean_pk).first() if clean_pk else None
                        materialy_na_vytvorenie.append(
                            PouzityMaterialVZaznamoch(
                                zaznam=zaznam,
                                content_type=ct,
                                object_id=clean_pk,
                                seriove_cislo_text=str(item.get('nazov', ''))[:100],
                                pocet=int(item.get('count', 1)),
                                obchodny_nazov=getattr(diel_obj, 'obchodny_nazov', None) if diel_obj else None,
                                skryt_vyrobcu=getattr(diel_obj, 'skryt_vyrobcu', False) if diel_obj else False
                            )
                        )
                
                if materialy_na_vytvorenie:
                    PouzityMaterialVZaznamoch.objects.bulk_create(materialy_na_vytvorenie)

            if foto_pred_path and os.path.exists(foto_pred_path):
                with open(foto_pred_path, 'rb') as f:
                    zaznam.foto_pred.save(os.path.basename(foto_pred_path), File(f), save=False)
                    ulozene_django_subory.append(zaznam.foto_pred)
            if foto_po_path and os.path.exists(foto_po_path):
                with open(foto_po_path, 'rb') as f:
                    zaznam.foto_po.save(os.path.basename(foto_po_path), File(f), save=False)
                    ulozene_django_subory.append(zaznam.foto_po)
            if video_path and os.path.exists(video_path):
                with open(video_path, 'rb') as f:
                    zaznam.video.save(name=os.path.basename(video_path), content=File(f), save=False)
                    ulozene_django_subory.append(zaznam.video)

            zaznam.save()
            
    except Exception as e:
        for django_file in ulozene_django_subory:
            if django_file and django_file.name:
                try:
                    django_file.storage.delete(django_file.name)
                except Exception:
                    pass
        return False, f"Kritická chyba pri zápise do databázy: {str(e)}", None

    zaznam.refresh_from_db()
    zaznam.odoslat_notifikacie_adminovi(poslat_tg=True, poslat_mail=True)   
    
    zaznam_data = {
        'id': zaznam.id,
        'bezpecnostny_kluc': str(getattr(zaznam, 'bezpecnostny_kluc', zaznam.id)),
        'zakaznik_id': zaznam.zakaznik_id,
        'zariadenie_id': zaznam.zariadenie_id,
        'zakaznik_nazov': zaznam.zakaznik.Nazov_spolocnosti if zaznam.zakaznik else "Nezistený",
        'zariadenie_nazov': str(zaznam.zariadenie) if zaznam.zariadenie else "Nezistené",
        'typ_servisu_nazov': typ_servisu.nazov if typ_servisu else "Nezistený"
    }
    return True, f"Záznam ID #{zaznam.id} úspešne uložený.", zaznam_data

@sync_to_async
def uzavri_ulohu_v_db_discord(uloha_id, discord_id):
    
    technik = Zamestnanec.objects.filter(discord_id=discord_id).first()
    if not technik: return False, None

    notifikovat = False
    uzavreta_id = None

    with transaction.atomic():
        uloha = Uloha.objects.select_for_update().filter(id=uloha_id).first()
        if uloha and not uloha.dokoncene:
            uloha.dokoncene = True
            uloha.save(update_fields=['dokoncene'])
            notifikovat = True
            uzavreta_id = uloha.id

    # Notifikácie voláme VŽDY až po úspešnom commite transakcie!
    if notifikovat:
        uloha_notif = Uloha.objects.get(id=uzavreta_id)
        uloha_notif.odoslat_notifikacie_adminovi()
        return True, uzavreta_id
        
    return False, None

@sync_to_async
def get_item_stav_pre_bota_discord(model_class, pk, wh_id):
    """Zistí stav konkrétnej položky na konkrétnom sklade (Ledger)."""
    try:
        item = model_class.objects.get(pk=pk)
        if wh_id != 0:
            filialka = Filialka.objects.filter(id=wh_id).first()
            if filialka:
                cisty = ocisti_nazov_od_jednotky(str(item))
                stav = LedgerKalkulacka.zisti_stav_polozky(filialka, cisty)
                return item, stav
        return item, item.pocet_na_sklade
    except Exception:
        return None, 0

@sync_to_async
def uloz_zmenu_skladu_db_discord(discord_id, model_class, pk, change, cat_name, sub_name, wh_id, typ_dokladu_erp):
    """ISAS CORE: Bezpečný zápis do skladu pre Discord (s Ledger ochranou)."""
    zamestnanec = Zamestnanec.objects.filter(discord_id=discord_id).first()
    django_user = zamestnanec.user if zamestnanec else None

    with transaction.atomic():
        try:
            item = model_class.objects.select_for_update().get(pk=pk)
        except model_class.DoesNotExist:
            return None, None, None, 0, None, 0
        
        cisty_nazov = ocisti_nazov_od_jednotky(str(item))
        jednotka = getattr(item, 'merna_jednotka', 'ks')

        if wh_id != 0:
            filialka = Filialka.objects.filter(id=wh_id).first()
            if not filialka: return None, None, None, 0, None, 0
            
            povodny_stav = LedgerKalkulacka.zisti_stav_polozky(filialka, cisty_nazov)
            skutocna_zmena = change
            if change < 0 and abs(change) > povodny_stav:
                skutocna_zmena = -povodny_stav

            if skutocna_zmena != 0:
                StockMovement.objects.create(
                    filialka=filialka, user=django_user, category=cat_name, subcategory=sub_name,
                    product_name=cisty_nazov, action=StockMovement.ADD if skutocna_zmena > 0 else StockMovement.REMOVE, 
                    count=abs(skutocna_zmena), merna_jednotka=jednotka, typ_dokladu_erp=typ_dokladu_erp
                )
            konecny_stav = povodny_stav + skutocna_zmena
            wh_name = filialka.nazov
        else:
            povodny_stav = item.pocet_na_sklade
            skutocna_zmena = change
            if change < 0 and abs(change) > povodny_stav:
                skutocna_zmena = -povodny_stav

            item.pocet_na_sklade += skutocna_zmena
            item.save(update_fields=['pocet_na_sklade'])

            if skutocna_zmena != 0:
                StockMovement.objects.create(
                    filialka=None, user=django_user, category=cat_name, subcategory=sub_name,
                    product_name=cisty_nazov, action=StockMovement.ADD if skutocna_zmena > 0 else StockMovement.REMOVE, 
                    count=abs(skutocna_zmena), merna_jednotka=jednotka, typ_dokladu_erp=typ_dokladu_erp
                )
            konecny_stav = item.pocet_na_sklade
            wh_name = "Hlavný sklad"

    meno_technika = zamestnanec.meno if zamestnanec else "Neznámy"
    nazov_polozky = getattr(item, 'obchodny_nazov', None) or getattr(item, 'seriove_cislo', None) or str(item)
    return nazov_polozky, jednotka, meno_technika, skutocna_zmena, wh_name, konecny_stav

@sync_to_async
def ulozit_tankovanie_db_discord(discord_id, auto_id, km_str, litre_str, cena_str, platba, stanica_id, foto_path):
    try: vozidlo = Vozidlo.objects.get(id=auto_id)
    except ObjectDoesNotExist: return False, "⚠️ **Chyba:** Zvolené vozidlo už neexistuje."

    zamestnanec = Zamestnanec.objects.filter(discord_id=discord_id).first()
    osoba = SukromnaOsoba.objects.filter(discord_id=discord_id).first()
    meno_technika = zamestnanec.meno if zamestnanec else (osoba.meno_a_priezvisko if osoba else "Neznámy vodič")

    km_hodnota = parse_int(km_str, default=0)
    litre_decimal = parse_decimal(litre_str, default='0.00')
    cena_decimal = parse_decimal(cena_str, default='0.00')

    if litre_decimal <= Decimal('0.00') or cena_decimal <= Decimal('0.00'):
        return False, "⚠️ **Chyba:** Množstvo litrov a cena musia byť väčšie ako 0."

    # Účtovná logika
    if platba == 'PALIVOVA': erp_zauctovanie = 'FAKTURA'
    elif platba == 'PLATOBNA': erp_zauctovanie = 'FIREMNY_UCET'
    else: erp_zauctovanie = 'SUKROMNE_PENIAZE'

    t = None
    try:
        with transaction.atomic():
            mena_firmy = get_firemna_mena()
            t = Tankovanie(
                vozidlo=vozidlo, vodič=zamestnanec, osoba=osoba,
                stav_tachometra=km_hodnota if km_hodnota > 0 else None,
                litre=litre_decimal, cena_celkom=cena_decimal,
                mena=mena_firmy,
                sposob_platby=platba, cerpacia_stanica_id=stanica_id, sposob_zauctovania_erp=erp_zauctovanie
            )
            
            if os.path.exists(foto_path):
                with open(foto_path, 'rb') as f:
                    t.foto_blocku.save(f"blocek_dc_{discord_id}_{timezone.now().strftime('%Y%m%d%H%M')}.jpg", File(f), save=False)
            t.full_clean()
            t.save()
    except ValidationError as e:
        if t and getattr(t, 'foto_blocku', None) and t.foto_blocku.name:
            try:
                t.foto_blocku.storage.delete(t.foto_blocku.name)
            except Exception:
                pass
        if hasattr(e, 'message_dict'):
            err_msg = "\n".join([f"- {m}" for f, errs in e.message_dict.items() for m in errs])
        elif hasattr(e, 'messages'):
            err_msg = "\n".join([f"- {m}" for m in e.messages])
        else:
            err_msg = str(e)
        return False, f"⚠️ **Účtovná chyba dokladu:**\n{err_msg}"
    except Exception as e:
        if t and getattr(t, 'foto_blocku', None) and t.foto_blocku.name:
            try:
                t.foto_blocku.storage.delete(t.foto_blocku.name)
            except Exception:
                pass
        return False, f"⚠️ **Systémová chyba pri zápise tankovania:**\n{str(e)}"

    alert_pre_majitela, upozornenie_pre_technika = [], []

    # 🛡️ 100% BULLETPROOF FLEET AUDITOR
    # 1. Kontrola objemu nádrže (Čisto typové porovnanie Decimal vs Decimal)
    max_litre = getattr(vozidlo, 'varovanie_max_litre', None)
    if max_litre and max_litre > 0:
        if litre_decimal > Decimal(str(max_litre)):
            alert_pre_majitela.append(
                f"🚨 **Podozrivé množstvo paliva:** Natankovaných až **{litre_decimal:.2f} L** "
                f"(limit vozidla: {max_litre} L)."
            )

    # 2. Kontrola sumy s ochranou voči krížovým menám (Cross-Currency Shield)
    max_suma = getattr(vozidlo, 'varovanie_max_suma', None)
    max_suma_dec = parse_decimal(max_suma, default='0.00')
    if max_suma_dec > Decimal('0.00'):
        mena_firmy = get_firemna_mena()
        mena_dokladu = getattr(t, 'mena', '') or mena_firmy

        # Porovnanie vykonáme LEN vtedy, ak je bloček v rovnakej mene ako nastavený limit vozidla
        if mena_dokladu.upper() == mena_firmy.upper():
            if cena_decimal > max_suma_dec:
                alert_pre_majitela.append(
                    f"🚨 **Podozrivá suma:** Tankovanie stálo **{cena_decimal:.2f} {mena_dokladu}** "
                    f"(limit vozidla: {max_suma_dec:.2f} {mena_firmy})."
                )

    aktualny_km_auta = vozidlo.aktualny_stav_km or 0
    
    if km_hodnota > 0 and km_hodnota < aktualny_km_auta:
        alert_pre_majitela.append(f"🚨 **Anomália tachometra:** Zadaný stav ({km_hodnota} km) je NIŽŠÍ ako posledný evidovaný ({aktualny_km_auta} km).")

    if km_hodnota > aktualny_km_auta:
        posledny_olej = vozidlo.posledny_olej_km or 0
        posledny_servis = vozidlo.posledny_servis_km or 0
        olej_interval = vozidlo.olej_interval_km or 15000
        servis_interval = vozidlo.servis_interval_km or 30000
        
        stare_km_do_oleja = olej_interval - (aktualny_km_auta - posledny_olej)
        stare_km_do_servisu = servis_interval - (aktualny_km_auta - posledny_servis)

        nove_km_do_oleja = olej_interval - (km_hodnota - posledny_olej)
        nove_km_do_servisu = servis_interval - (km_hodnota - posledny_servis)

        if nove_km_do_oleja <= 1000: upozornenie_pre_technika.append(f"🛢️ **Olej:** Zostáva len {nove_km_do_oleja} km!")
        if nove_km_do_servisu <= 1500: upozornenie_pre_technika.append(f"⚙️ **Veľký servis:** Zostáva len {nove_km_do_servisu} km!")

        if stare_km_do_oleja > 1000 and nove_km_do_oleja <= 1000: alert_pre_majitela.append(f"🛢️ **Nutná výmena oleja:** Vozidlu zostáva už len {nove_km_do_oleja} km.")
        if stare_km_do_servisu > 1500 and nove_km_do_servisu <= 1500: alert_pre_majitela.append(f"⚙️ **Veľký servis:** Vozidlu zostáva {nove_km_do_servisu} km.")

    dnes = timezone.now().date()
    limit_dni = datetime.timedelta(days=14)
    if vozidlo.stk_plati_do and (vozidlo.stk_plati_do - dnes) <= limit_dni: upozornenie_pre_technika.append(f"📑 **STK/EK:** Platí do {vozidlo.stk_plati_do.strftime('%d.%m.%Y')}!")
    if vozidlo.dialnicna_znamka_do and (vozidlo.dialnicna_znamka_do - dnes) <= limit_dni: upozornenie_pre_technika.append(f"🎫 **Známka:** Končí {vozidlo.dialnicna_znamka_do.strftime('%d.%m.%Y')}!")

    if alert_pre_majitela:
        gn = GlobalneNastavenia.objects.first()
        base_url = gn.clean_base_url if gn else getattr(settings, 'SITE_URL', '')
        admin_url = f"{base_url}/admin/Evidencia/vozidlo/{vozidlo.id}/change/"
        udalosti_text = "<br>".join([f"• {safe_discord(msg)}" for msg in alert_pre_majitela])
        majitel_discord = getattr(gn, 'discord_id_majitel', None)

        payload = (
            ISASNotificationBuilder(
                nadpis=f"FLEET ALERT ({safe_discord(vozidlo.spz)})",
                ikona="🚨",
                farba_temy="#dc2626"
            )
            .pridaj_riadok("Vozidlo:", f"{safe_discord(vozidlo.spz)} ({safe_discord(vozidlo.znacka_a_model)})")
            .pridaj_riadok("Vodič:", safe_discord(meno_technika))
            .nastav_hlavny_text(f"<b>Udalosti detegované pri tankovaní:</b><br>{udalosti_text}")
            .nastav_tlacidlo("Otvoriť kartu vozidla", admin_url)
            .nastav_ai_poznamku("ISAS AI Fleet Guardian: Detegovaná anomália alebo dosiahnutý servisný interval.")
            .build()
        )
        ISASNotifier.send(
            payload=payload, 
            discord_id=majitel_discord, 
            send_tg=True, 
            send_mail=True, 
            send_discord=True
        )

    vystupne_upozornenie = "\n\n⚠️ **ISAS INFO PRE TEBA:**\n" + "\n".join(upozornenie_pre_technika) if upozornenie_pre_technika else ""
    return True, vystupne_upozornenie

@sync_to_async
def ulozit_jazdu_db_discord(discord_id, auto_id, typ_jazdy, km_start, km_koniec, ciel_text, ucel, zakaznik_id=None, spolujazdec_id=None, stat_ciela=None, noclah=False, strava=False):    
    try:
        try: auto = Vozidlo.objects.get(id=auto_id)
        except ObjectDoesNotExist: return False, "⚠️ **Chyba:** Vozidlo už neexistuje."
            
        z = Zamestnanec.objects.filter(discord_id=discord_id).first()
        s = SukromnaOsoba.objects.filter(discord_id=discord_id).first()

        km_s = parse_int(km_start, default=None)
        km_k = parse_int(km_koniec, default=None)
        if km_s is None or km_k is None:
            return False, "⚠️ **Chyba:** Tachometer musí obsahovať platné číslo."
            
        if km_k <= km_s: return False, f"⚠️ **Logická chyba:** Konečný stav ({km_k} km) nemôže byť menší alebo rovný počiatočnému ({km_s} km)!"

        prejdene = km_k - km_s
        if prejdene > 1500: return False, f"⚠️ **Nereálny prejazd!** Zadal si trasu **{prejdene} km** na jeden záťah."

        posledna_jazda = EvidenciaJazd.objects.filter(vozidlo=auto, datum_cas_koniec__isnull=False).order_by('-datum_cas_koniec').first()
        if posledna_jazda and posledna_jazda.miesto_ciela:
            miesto_startu = posledna_jazda.miesto_ciela
            stat_startu = posledna_jazda.stat_ciela or ""
        else:
            identita = FiremnaIdentita.objects.first()
            mesto = getattr(identita, 'sidlo_mesto', '') if identita else ''
            miesto_startu = f"Sídlo firmy ({mesto})" if mesto else "Sídlo firmy"
            stat_startu = getattr(identita, 'sidlo_stat', '') or ""
        
        poznamka_k_jazde = ""
        aktualny_km_auta = auto.aktualny_stav_km or 0
        rozdiel_od_minulosti = km_s - aktualny_km_auta
        fleet_anomalia = None
        if rozdiel_od_minulosti > 20: 
            poznamka_k_jazde = f"⚠️ ALERT: Medzi poslednou evidenciou a touto jazdou chýba {rozdiel_od_minulosti} km!"
            fleet_anomalia = f"Medzi poslednou evidenciou a touto jazdou chýba nezaevidovaných <b>{rozdiel_od_minulosti} km</b>!"
        elif rozdiel_od_minulosti < -20:
            poznamka_k_jazde = f"⚠️ ALERT: Technik zadal na štarte o {abs(rozdiel_od_minulosti)} km menej, ako mal byť posledný stav!"
            fleet_anomalia = f"Vodič zadal na štarte o <b>{abs(rozdiel_od_minulosti)} km menej</b> než posledný evidovaný stav!"

        zakaznik_obj = Zakaznik.objects.filter(id=zakaznik_id).first() if zakaznik_id else None
        uloha_obj = Uloha.objects.filter(technici=z, zakaznik=zakaznik_obj, dokoncene=False, zaciatok__date=timezone.now().date()).first() if zakaznik_obj and z else None

        odhad_minut = max(5, min(480, int(prejdene * 1.2)))
        cas_koniec = timezone.now()
        
        # 🛡️ DEFENSIVE: Striktná harmonizácia naive vs. aware datetime rešpektujúca settings.USE_TZ
        use_tz = getattr(settings, 'USE_TZ', False)
        if not use_tz:
            if timezone.is_aware(cas_koniec):
                try:
                    cas_koniec = timezone.make_naive(cas_koniec)
                except Exception:
                    cas_koniec = cas_koniec.replace(tzinfo=None)
        else:
            if not timezone.is_aware(cas_koniec):
                try:
                    cas_koniec = timezone.make_aware(cas_koniec)
                except Exception:
                    pass

        p_koniec = posledna_jazda.datum_cas_koniec if posledna_jazda else None
        if p_koniec:
            if not use_tz:
                if timezone.is_aware(p_koniec):
                    try:
                        p_koniec = timezone.make_naive(p_koniec)
                    except Exception:
                        p_koniec = p_koniec.replace(tzinfo=None)
            else:
                if not timezone.is_aware(p_koniec):
                    try:
                        p_koniec = timezone.make_aware(p_koniec)
                    except Exception:
                        # Fail-safe pre prípad zlyhania make_aware: zjednotenie na naive
                        cas_koniec = cas_koniec.replace(tzinfo=None)
                        p_koniec = p_koniec.replace(tzinfo=None)

            min_mozny_start = p_koniec + datetime.timedelta(minutes=1)
            cas_start = cas_koniec - datetime.timedelta(minutes=odhad_minut)
            
            # 🛡️ DEFENSIVE: Garantovaná eliminácia TypeError naive vs. aware pred porovnávaním
            if timezone.is_aware(cas_start) != timezone.is_aware(min_mozny_start):
                cas_start = cas_start.replace(tzinfo=None)
                cas_koniec = cas_koniec.replace(tzinfo=None)
                min_mozny_start = min_mozny_start.replace(tzinfo=None)

            if cas_start <= min_mozny_start:
                cas_start = min_mozny_start
                cas_koniec = cas_start + datetime.timedelta(minutes=odhad_minut)
        else:
            cas_start = cas_koniec - datetime.timedelta(minutes=odhad_minut)
            if not use_tz and timezone.is_aware(cas_start):
                cas_start = cas_start.replace(tzinfo=None)

        with transaction.atomic():
            cielovy_stat_final = stat_startu if stat_ciela in ["Tuzemsko", None, ""] else (stat_ciela or stat_startu or "")
            jazda = EvidenciaJazd(
                vozidlo=auto, typ_jazdy=typ_jazdy, zamestnanec=z, sukromna_osoba=s,
                miesto_startu=miesto_startu, miesto_ciela=ciel_text,
                zakaznik=zakaznik_obj, uloha=uloha_obj, ucel_cesty=ucel,
                stav_km_start=km_s, stav_km_koniec=km_k,
                datum_cas_start=cas_start, datum_cas_koniec=cas_koniec,
                poznamka=poznamka_k_jazde,
                stat_startu=stat_startu or "",
                stat_ciela=cielovy_stat_final or "",
                noclah_zabezpeceny=noclah, bezplatne_stravovanie=strava
            )

            jazda.full_clean()
            jazda.save()
            
            if spolujazdec_id and z:
                kolega = Zamestnanec.objects.filter(id=spolujazdec_id).first()
                if kolega:
                    jazda.spolujazdci.add(kolega)
        
        sprava = f"📍 Trasa: **{miesto_startu} ➔ {ciel_text}**\nEvidovaných: **{prejdene} km**"
        if uloha_obj: sprava += f"\n🔗 *Spojené s Úlohou #{uloha_obj.id}*"
        if poznamka_k_jazde: sprava += f"\n\n{poznamka_k_jazde}"

        if fleet_anomalia:
            gn = GlobalneNastavenia.objects.first()
            base_url = gn.clean_base_url if gn else getattr(settings, 'SITE_URL', '')
            admin_url = f"{base_url}/admin/Evidencia/evidenciajazd/{jazda.id}/change/"
            meno_vodica = z.meno if z else (s.meno_a_priezvisko if s else "Neznámy vodič")
            majitel_discord = getattr(gn, 'discord_id_majitel', None)

            payload = (
                ISASNotificationBuilder(
                    nadpis=f"FLEET ANOMÁLIA ({safe_discord(auto.spz)})",
                    ikona="🚨",
                    farba_temy="#dc2626"
                )
                .pridaj_riadok("Vozidlo:", f"{safe_discord(auto.spz)} ({safe_discord(auto.znacka_a_model)})")
                .pridaj_riadok("Vodič:", safe_discord(meno_vodica))
                .pridaj_riadok("Trasa:", f"{safe_discord(miesto_startu)} ➔ {safe_discord(ciel_text)}")
                .nastav_hlavny_text(f"<b>Detegovaná nezrovnalosť v tachometri:</b><br>• {fleet_anomalia}")
                .nastav_tlacidlo("Skontrolovať jazdu v ISAS", admin_url)
                .nastav_ai_poznamku("ISAS AI Fleet Auditor: Automatická detekcia prerušenia nadväznosti jázd.")
                .build()
            )
            ISASNotifier.send(payload=payload, discord_id=majitel_discord, send_tg=True, send_mail=True, send_discord=True)

        return True, sprava
    except ValidationError as e:
        if hasattr(e, 'message_dict'):
            err_msg = "\n".join([f"- {m}" for f, errs in e.message_dict.items() for m in errs])
        elif hasattr(e, 'messages'):
            err_msg = "\n".join([f"- {m}" for m in e.messages])
        else:
            err_msg = str(e)
        return False, f"⚠️ **Účtovná chyba:**\n{err_msg}"
    except Exception as e:
        return False, f"⚠️ **Systémová chyba:** {str(e)}"

@sync_to_async
def set_predvolena_cerpacka_discord(discord_id, dodavatel_id):
    dodavatel = Dodavatel.objects.filter(id=dodavatel_id).first()
    if not dodavatel:
        return "Neznáma čerpacia stanica"
    zamestnanec = Zamestnanec.objects.filter(discord_id=discord_id).first()
    if zamestnanec:
        zamestnanec.predvolena_cerpacka = dodavatel
        zamestnanec.save(update_fields=['predvolena_cerpacka'])
    osoba = SukromnaOsoba.objects.filter(discord_id=discord_id).first()
    if osoba:
        osoba.predvolena_cerpacka = dodavatel
        osoba.save(update_fields=['predvolena_cerpacka'])
    return dodavatel.Nazov_spolocnosti

@sync_to_async
def get_zoznam_filialok_pre_bot_discord(discord_id):
    """Vráti zoznam aktívnych filiálok vyfiltrovaný podľa prístupu technika (Discord verzia)."""
    if not discord_id:
        return []
    qs = Filialka.objects.filter(je_aktivna=True)
    technik = Zamestnanec.objects.select_related('user').filter(discord_id=discord_id).first()
    if not technik:
        return []
    is_admin = (
        technik.typ_pomeru == 'KO'
        or technik.pracovna_rola in ['AD', 'DI']
        or bool(technik.user and technik.user.is_superuser)
    )
    if not is_admin:
        qs = qs.filter(priradeni_zamestnanci=technik)
    return list(qs.values('id', 'nazov'))

@sync_to_async
def uloz_medziskladovy_presun_db_discord(discord_id, zdroj_id, ciel_id, model_name, pk, count):
    """ISAS CORE: Logistika (Medziskladové presuny cez Discord)."""
    if zdroj_id == ciel_id:
        return False, "⚠️ Zdrojový a cieľový sklad nemôžu byť zhodné."

    zamestnanec = Zamestnanec.objects.filter(discord_id=discord_id).first()
    django_user = zamestnanec.user if zamestnanec else None
    
    zdroj_sklad = Filialka.objects.filter(id=zdroj_id).first() if zdroj_id != 0 else None
    ciel_sklad = Filialka.objects.filter(id=ciel_id).first() if ciel_id != 0 else None

    model_class, cat_name, subcat_name = MODEL_NAME_MAP.get(model_name, (None, None, None))
    if not model_class: return False, "⚠️ Neznámy model položky."

    with transaction.atomic():
        try:
            product = model_class.objects.select_for_update().get(pk=pk)
        except model_class.DoesNotExist:
            return False, "🔍 Produkt neexistuje alebo bol vymazaný."

        cisty_nazov_produktu = ocisti_nazov_od_jednotky(str(product))
        jednotka = getattr(product, 'merna_jednotka', 'ks')
        
        # 1. KONTROLA ZDROJA
        if zdroj_sklad is None:
            if product.pocet_na_sklade < count:
                return False, f"📦 Na Hlavnom sklade nie je dostatok kusov! (Dostupné: {product.pocet_na_sklade} {jednotka})"
            product.pocet_na_sklade -= count
        else:
            povodny_stav_zdroj = LedgerKalkulacka.zisti_stav_polozky(zdroj_sklad, cisty_nazov_produktu)
            if povodny_stav_zdroj < count:
                return False, f"📦 Na sklade {zdroj_sklad.nazov} nie je dostatok kusov! (Dostupné: {povodny_stav_zdroj} {jednotka})"

        StockMovement.objects.create(
            filialka=zdroj_sklad, user=django_user, category=cat_name, subcategory=subcat_name,
            product_name=cisty_nazov_produktu, count=count, action=StockMovement.REMOVE, 
            merna_jednotka=jednotka, typ_dokladu_erp='PREVODKA'
        )

        # 2. PRIPÍSANIE DO CIEĽA
        if ciel_sklad is None:
            product.pocet_na_sklade += count
        
        StockMovement.objects.create(
            filialka=ciel_sklad, user=django_user, category=cat_name, subcategory=subcat_name,
            product_name=cisty_nazov_produktu, count=count, action=StockMovement.ADD, 
            merna_jednotka=jednotka, typ_dokladu_erp='PREVODKA'
        )

        if zdroj_sklad is None or ciel_sklad is None:
            product.save(update_fields=['pocet_na_sklade'])

        nazov_zdroja = zdroj_sklad.nazov if zdroj_sklad else "Hlavný sklad"
        nazov_ciela = ciel_sklad.nazov if ciel_sklad else "Hlavný sklad"
        
        choices_dict = dict(CATEGORY_CHOICES)
        kategoria_s_emoji = choices_dict.get(cat_name, cat_name)

        hlavna_sprava = (
            f"🗂️ **Kategória:** {kategoria_s_emoji} / {subcat_name}\n"
            f"📦 **Položka:** {cisty_nazov_produktu}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📤 **ZDROJ:** {nazov_zdroja}\n"
            f"📥 **CIEĽ:** {nazov_ciela}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔢 **Presunuté množstvo:** {count} {jednotka}"
        )

        try:
            from Evidencia.tasks import task_odosli_notifikaciu_presun
            user_name = f"{zamestnanec.meno} {zamestnanec.priezvisko}" if zamestnanec else "Neznámy technik"

            def safe_odosli_notifikaciu():
                try:
                    task_odosli_notifikaciu_presun.delay(
                        user_name, nazov_zdroja, nazov_ciela, cisty_nazov_produktu, count, jednotka
                    )
                except Exception as exc:
                    logging.warning(f"Zlyhalo odoslanie notifikácie presunu do Celery (výpadok brokera): {exc}")

            # 🛡️ DEFENSIVE: Odoslanie notifikácie výhradne po úspešnom commite DB transakcie s fail-safe ochranou
            transaction.on_commit(safe_odosli_notifikaciu)
        except Exception as exc:
            logging.warning(f"Zlyhalo zaregistrovanie on_commit notifikácie presunu: {exc}")

    return True, hlavna_sprava

async def send_clean_menu_discord(channel, user_id, text, view):
    """Pomocná funkcia na odoslanie menu a zmazanie toho predchádzajúceho."""
    
    # 🧹 ISAS GARBAGE COLLECTOR: Zmazanie opustených médií pri návrate do menu
    if user_id not in USER_STATES or not isinstance(USER_STATES[user_id], dict):
        USER_STATES[user_id] = {}
        save_user_states()
    else:
        for key in ['sh_foto_pred', 'sh_foto_po', 'sh_video']:
            temp_path = USER_STATES[user_id].get(key)
            if temp_path and os.path.exists(temp_path):
                try: os.remove(temp_path)
                except Exception: pass
        
    # 1. Zmazanie starého menu, priepustiek a ZAMKNUTEJ OBRAZOVKY (Garbage Collector)
    old_msg_id = USER_STATES[user_id].get('last_menu_msg_id')
    lock_msg_id = USER_STATES[user_id].get('lock_msg_id')
    vstupy_ids = USER_STATES[user_id].get('vstupy_msg_ids')
    if not isinstance(vstupy_ids, list):
        vstupy_ids = []
    
    ids_na_zmazanie = [m for m in [old_msg_id, lock_msg_id] + vstupy_ids if m]
    for msg_id in ids_na_zmazanie:
        try:
            old_msg = await channel.fetch_message(msg_id)
            await old_msg.delete()
        except Exception:
            pass
            
    USER_STATES[user_id]['vstupy_msg_ids'] = []
    USER_STATES[user_id]['lock_msg_id'] = None

    # 2. Odoslanie nového menu
    new_msg = await channel.send(content=text, view=view)
    
    # 3. Uloženie ID nového menu
    USER_STATES[user_id]['last_menu_msg_id'] = new_msg.id
    save_user_states()

class BtnRozumiemDiscord(discord.ui.Button):
    def __init__(self, typ_uzivatela):
        super().__init__(label="Rozumiem, pokračovať do menu", style=discord.ButtonStyle.primary)
        self.typ_uzivatela = typ_uzivatela

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        # Zapíšeme ho do RAM ako REFUSED (lebo Discord nemá GPS)
        await update_radar_cache_discord(interaction.user.id, 'REFUSED')
        
        text = "**ISAS CORE TERMINÁL** 🟢\n━━━━━━━━━━━━━━━━━━━━\nVyber požadovanú akciu z menu:"
        try:
            await interaction.delete_original_response()
        except Exception:
            pass
        _, typ = await identifikuj_pouzivatela_discord(interaction.user.id)
        user_type = typ or self.typ_uzivatela or "ZAMESTNANEC"
        await send_clean_menu_discord(interaction.channel, interaction.user.id, text, HlavneMenu(user_type))

class BtnSpatMenu(discord.ui.Button):
    """Univerzálne tlačidlo pre návrat do Hlavného Menu"""
    def __init__(self, typ_uzivatela):
        super().__init__(label="🔙 Hlavné menu", style=discord.ButtonStyle.secondary)
        self.typ_uzivatela = typ_uzivatela

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        # 🛡️ EDGE CASE: Resetujeme stav, aby bot nečakal na ďalšie správy ako na AI prompt
        if interaction.user.id in USER_STATES and isinstance(USER_STATES[interaction.user.id], dict):
            USER_STATES[interaction.user.id]['state'] = None
            save_user_states()

        text = "**ISAS CORE TERMINÁL (ZÁLOHA)** 🟢\n━━━━━━━━━━━━━━━━━━━━\nVyber požadovanú akciu z menu:"
        try:
            await interaction.delete_original_response()
        except Exception:
            pass
        _, typ = await identifikuj_pouzivatela_discord(interaction.user.id)
        user_type = typ or self.typ_uzivatela or "ZAMESTNANEC"
        await send_clean_menu_discord(interaction.channel, interaction.user.id, text, HlavneMenu(user_type))

class BtnOdomknut(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        btn = discord.ui.Button(label="🔓 OTVORIŤ TERMINÁL", style=discord.ButtonStyle.success, custom_id="menu_unlock")
        self.add_item(btn)

# --- MOJE VSTUPY ---
class SelectAutoVstup(discord.ui.Select):
    def __init__(self, povolenie_id, vozidla):
        self.povolenie_id = povolenie_id
        # 🛡️ DEFENSIVE: Discord limituje label na max 100 znakov a max 25 položiek
        options = [
            discord.SelectOption(
                label=(f"{v.spz or ''} ({v.znacka_a_model or ''})".replace("()", "").strip() or f"Vozidlo #{v.id}")[:100], 
                value=str(v.id), 
                emoji="🚗"
            ) for v in vozidla[:25]
        ]
        super().__init__(placeholder="🔄 Zmeniť vozidlo pre výjazd...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        auto_id = int(self.values[0])
        
        await interaction.edit_original_response(content="🔄 *Spracovávam zmenu vozidla v systéme...*", view=None)

        from Evidencia.tasks import vykonaj_zmenu_vozidla_vstupu
        try:
            success, text = await sync_to_async(vykonaj_zmenu_vozidla_vstupu)(
                povolenie_id=self.povolenie_id,
                auto_id=auto_id,
                user_identifier={'discord_id': interaction.user.id}
            )
        except Exception as e:
            logging.error(f"🚨 Chyba pri zmene vozidla cez Discord bota: {e}")
            text = "❌ **Chyba systému:** Zmenu vozidla sa nepodarilo spracovať kvôli chybe spojenia. Skús to znova."

        # Prevod HTML tagov na Discord markdown formát
        text_discord = text.replace("<b>", "**").replace("</b>", "**")
        await interaction.edit_original_response(content=text_discord, view=discord.ui.View().add_item(BtnSpatMenu("ZAMESTNANEC")))

# --- UZAVRETIE ÚLOHY PO HLASOVKE ---
class BtnUzavrietUlohu(discord.ui.Button):
    def __init__(self, uloha_id, label):
        super().__init__(label=label, style=discord.ButtonStyle.success)
        self.uloha_id = uloha_id

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        success, uzavreta_uloha_id = await uzavri_ulohu_v_db_discord(self.uloha_id, interaction.user.id)
        if success:
            text = f"✅ **ÚLOHA #{uzavreta_uloha_id} BOLA UZAVRETÁ!**\n━━━━━━━━━━━━━━━━━━━━\nSystém automaticky spároval tvoj servisný záznam a uzavrel administratívu."
        else:
            text = "⚠️ **Chyba:** Úlohu sa nepodarilo uzavrieť."
        await interaction.edit_original_response(content=text, view=discord.ui.View().add_item(BtnSpatMenu("ZAMESTNANEC")))

# --- TANKOVANIE ---
class TankovanieModal(discord.ui.Modal, title='Údaje o tankovaní'):
    km: discord.ui.TextInput = discord.ui.TextInput(label='Stav tachometra (km)', placeholder='Napr. 145000', required=True)
    litre: discord.ui.TextInput = discord.ui.TextInput(label='Natankované litre', placeholder='Napr. 52.4', required=True)
    cena: discord.ui.TextInput = discord.ui.TextInput(label='Celková cena s DPH', placeholder='Napr. 65.20', required=True)

    def __init__(self, auto_id):
        super().__init__()
        self.auto_id = auto_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        # Uložíme dáta do dočasnej pamäte
        USER_STATES[interaction.user.id] = {
            'state': 'TANKOVANIE_PLATBA',
            'auto_id': self.auto_id,
            'km': self.km.value,
            'litre': self.litre.value,
            'cena': self.cena.value
        }
        save_user_states()
        
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="💳 Palivová karta", style=discord.ButtonStyle.primary, custom_id="tpay_PALIVOVA"))
        view.add_item(discord.ui.Button(label="📱 Banková karta", style=discord.ButtonStyle.primary, custom_id="tpay_PLATOBNA"))
        view.add_item(discord.ui.Button(label="💵 Hotovosť", style=discord.ButtonStyle.success, custom_id="tpay_HOTOVOST"))
        view.add_item(BtnSpatMenu("ZAMESTNANEC"))
        
        await interaction.edit_original_response(content="💳 **Vyber spôsob platby:**", view=view)

class CerpackaSearchModal(discord.ui.Modal, title='Hľadať čerpaciu stanicu'):
    hladany_text: discord.ui.TextInput = discord.ui.TextInput(label='Názov (napr. Shell, Tesco)', required=True)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        stanice = await vyhladaj_cerpacku_async(self.hladany_text.value)
        
        if not stanice:
            await interaction.followup.send(f"⚠️ Nenašiel som žiadnu stanicu obsahujúcu '{self.hladany_text.value}'.", ephemeral=True)
            return
            
        # 🛡️ DEFENSIVE: Striktný limit Discord API (max 25 možností v Select Menu)
        options = [discord.SelectOption(label=s['nazov'][:100], value=str(s['id']), emoji="⛽") for s in stanice[:25]]
        select: discord.ui.Select[Any] = discord.ui.Select(placeholder="Vyber nájdenú stanicu...", options=options)
        
        async def cb(i: discord.Interaction):
            await i.response.defer()
            s_id = int(select.values[0])
            USER_STATES[i.user.id]['stanica_id'] = s_id
            USER_STATES[i.user.id]['state'] = 'CAKAM_BLOCEK'
            save_user_states()
            
            await set_predvolena_cerpacka_discord(i.user.id, s_id)
            
            await i.edit_original_response(content="✅ *Stanica uložená.*\n\n📸 **Teraz odfotografuj bloček a pošli mi ho sem:**", view=discord.ui.View().add_item(BtnSpatMenu("ZAMESTNANEC")))
            
        setattr(select, 'callback', cb)
        view = discord.ui.View(timeout=None).add_item(select).add_item(BtnSpatMenu("ZAMESTNANEC"))
        await interaction.edit_original_response(content="🔍 **Výsledky hľadania:**", view=view)

async def render_tankovanie_stanica(interaction: discord.Interaction):
    discord_id = interaction.user.id
    stanica_id, stanica_nazov = await get_predvolena_cerpacka_discord(discord_id)
    
    if stanica_id:
        USER_STATES[discord_id]['stanica_id'] = stanica_id
        USER_STATES[discord_id]['state'] = 'CAKAM_BLOCEK'
        save_user_states()
        text = f"⛽ *Použitá tvoja predvolená ČS: **{stanica_nazov}***\n\n📸 **Teraz odfotografuj bloček a pošli mi ho sem:**"
        await interaction.edit_original_response(content=text, view=discord.ui.View().add_item(BtnSpatMenu("ZAMESTNANEC")))
    else:
        stanice = await get_zoznam_cerpaciek_async()
        if not stanice:
            view = discord.ui.View(timeout=None)
            btn_search: discord.ui.Button[Any] = discord.ui.Button(label="🔍 Hľadať čerpaciu stanicu", style=discord.ButtonStyle.primary)
            async def search_empty_cb(i: discord.Interaction):
                await i.response.send_modal(CerpackaSearchModal())
            setattr(btn_search, 'callback', search_empty_cb)
            view.add_item(btn_search)
            view.add_item(BtnSpatMenu("ZAMESTNANEC"))
            await interaction.edit_original_response(content="⚠️ V systéme zatiaľ nie sú evidované predvolené čerpacie stanice. Použi vyhľadávanie:", view=view)
            return

        options = [discord.SelectOption(label=s['nazov'][:100], value=str(s['id']), emoji="⛽") for s in stanice[:25]]
        
        select: discord.ui.Select[Any] = discord.ui.Select(placeholder="Kde si tankoval? (Uloží sa ako predvolená)", options=options)
        async def cb(i: discord.Interaction):
            await i.response.defer()
            s_id = int(select.values[0])
            USER_STATES[i.user.id]['stanica_id'] = s_id
            USER_STATES[i.user.id]['state'] = 'CAKAM_BLOCEK'
            save_user_states()
            
            # Uložíme ako predvolenú
            await set_predvolena_cerpacka_discord(i.user.id, s_id)
            
            await i.edit_original_response(content="✅ *Stanica uložená.*\n\n📸 **Teraz odfotografuj bloček a pošli mi ho sem:**", view=discord.ui.View().add_item(BtnSpatMenu("ZAMESTNANEC")))
        setattr(select, 'callback', cb)
        
        view = discord.ui.View(timeout=None).add_item(select).add_item(BtnSpatMenu("ZAMESTNANEC"))
        # PRIDANÉ TLAČIDLO NA HĽADANIE
        btn_other_search: discord.ui.Button[Any] = discord.ui.Button(label="🔍 Iná čerpacia stanica", style=discord.ButtonStyle.secondary)
        async def search_cb(i: discord.Interaction):
            await i.response.send_modal(CerpackaSearchModal())
        setattr(btn_other_search, 'callback', search_cb)
        view.add_item(btn_other_search)
        
        view.add_item(BtnSpatMenu("ZAMESTNANEC"))
        await interaction.edit_original_response(content="🏢 **Kde si tankoval?**\n*(Zvolená možnosť sa uloží ako predvolená)*", view=view)

class ZakaznikSearchModal(discord.ui.Modal, title='Hľadať zákazníka'):
    hladany_text: discord.ui.TextInput = discord.ui.TextInput(label='Názov firmy', required=True)
    def __init__(self, auto_id, typ_jazdy):
        super().__init__()
        self.auto_id = auto_id
        self.typ_jazdy = typ_jazdy

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        zakaznici = await vyhladaj_zakaznika_pre_bot_async(self.hladany_text.value)
        if not zakaznici:
            await interaction.followup.send("⚠️ Nenašiel som žiadneho zákazníka.", ephemeral=True)
            return
            
        # 🛡️ DEFENSIVE: Striktný limit Discord API (max 25 možností v Select Menu)
        options = [discord.SelectOption(label=z['Nazov_spolocnosti'][:100], value=str(z['id']), emoji="🏢") for z in zakaznici[:25]]
        select: discord.ui.Select[Any] = discord.ui.Select(placeholder="Vyber zákazníka...", options=options)
        
        async def cb(i: discord.Interaction):
            # Tu konečne otvárame okno na KM!
            z_id = int(select.values[0])
            z_nazov = next((z['Nazov_spolocnosti'] for z in zakaznici if z['id'] == z_id), "Zákazník")
            await i.response.send_modal(JazdaModal(self.auto_id, self.typ_jazdy, f"Zákazník: {z_nazov}", z_id))
            
        setattr(select, 'callback', cb)
        view = discord.ui.View(timeout=None).add_item(select).add_item(BtnSpatMenu("ZAMESTNANEC"))
        await interaction.edit_original_response(content="🤝 **Výsledky hľadania:**", view=view)

class IneMiestoModal(discord.ui.Modal, title='Iné miesto cieľa'):
    miesto: discord.ui.TextInput = discord.ui.TextInput(label='Mesto, Ulica', required=True)
    def __init__(self, auto_id, typ_jazdy):
        super().__init__()
        self.auto_id = auto_id
        self.typ_jazdy = typ_jazdy

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        miesto_text = self.miesto.value.strip()
        view = discord.ui.View(timeout=None)
        
        btn_km: discord.ui.Button[Any] = discord.ui.Button(label="✍️ Zadať kilometre a účel", style=discord.ButtonStyle.success)
        async def cb_km(i: discord.Interaction):
            await i.response.send_modal(JazdaModal(self.auto_id, self.typ_jazdy, miesto_text, None))
        setattr(btn_km, 'callback', cb_km)
        view.add_item(btn_km)
        view.add_item(BtnSpatMenu("ZAMESTNANEC"))

        await interaction.edit_original_response(
            content=f"📍 **Cieľ nastavený:** {miesto_text}\n\nPre pokračovanie klikni na tlačidlo nižšie:",
            view=view
        )

async def render_jazda_ciel(interaction: discord.Interaction, auto_id, typ_jazdy):
    view = discord.ui.View(timeout=None)
    
    btn_hlavny: discord.ui.Button[Any] = discord.ui.Button(label="🏢 Návrat na Hlavnú firmu", style=discord.ButtonStyle.primary)
    async def cb_hlavny(i: discord.Interaction):
        adresa = (await get_hlavny_sklad_adresa_async()) or "Hlavný sklad"
        await i.response.send_modal(JazdaModal(auto_id, typ_jazdy, adresa, None))
    setattr(btn_hlavny, 'callback', cb_hlavny)
    view.add_item(btn_hlavny)

    btn_filialka: discord.ui.Button[Any] = discord.ui.Button(label="🏭 Presun na našu Filiálku", style=discord.ButtonStyle.primary)
    async def cb_filialka(i: discord.Interaction):
        await i.response.defer()
        filialky = await get_zoznam_filialok_pre_bot_discord(i.user.id)
        if not filialky:
            await i.followup.send("⚠️ Nemáš priradené žiadne aktívne filiálky.", ephemeral=True)
            return
        options = [discord.SelectOption(label=f['nazov'][:100], value=str(f['id']), emoji="🏭") for f in filialky[:25]]
        select: discord.ui.Select[Any] = discord.ui.Select(placeholder="Vyber filiálku...", options=options)
        async def cb_sel(i2: discord.Interaction):
            f_id = int(select.values[0])
            f_nazov = next(f['nazov'] for f in filialky if f['id'] == f_id)
            await i2.response.send_modal(JazdaModal(auto_id, typ_jazdy, f"Filiálka: {f_nazov}", None))
        setattr(select, 'callback', cb_sel)
        view_f = discord.ui.View(timeout=None).add_item(select).add_item(BtnSpatMenu("ZAMESTNANEC"))
        await i.edit_original_response(content="🏭 **Vyber si konkrétnu filiálku:**", view=view_f)
    setattr(btn_filialka, 'callback', cb_filialka)
    view.add_item(btn_filialka)

    btn_zakaznik: discord.ui.Button[Any] = discord.ui.Button(label="🤝 Cesta k Zákazníkovi", style=discord.ButtonStyle.success)
    async def cb_zakaznik(i: discord.Interaction):
        await i.response.send_modal(ZakaznikSearchModal(auto_id, typ_jazdy))
    setattr(btn_zakaznik, 'callback', cb_zakaznik)
    view.add_item(btn_zakaznik)
    
    btn_ine: discord.ui.Button[Any] = discord.ui.Button(label="📍 Iné miesto", style=discord.ButtonStyle.secondary)
    async def cb_ine(i: discord.Interaction):
        await i.response.send_modal(IneMiestoModal(auto_id, typ_jazdy))
    setattr(btn_ine, 'callback', cb_ine)

    view.add_item(BtnSpatMenu("ZAMESTNANEC"))
    await interaction.edit_original_response(content="🗺️ **CIEĽ CESTY (Kam si dorazil?):**", view=view)

# --- KNIHA JÁZD ---
class JazdaModal(discord.ui.Modal, title='Údaje o jazde'):
    km_start: discord.ui.TextInput = discord.ui.TextInput(label='Tachometer ŠTART', required=True)
    km_koniec: discord.ui.TextInput = discord.ui.TextInput(label='Tachometer CIEĽ', required=True)
    ucel: discord.ui.TextInput = discord.ui.TextInput(label='Účel cesty', placeholder='Napr. Oprava, Nákup', required=True)

    def __init__(self, auto_id, typ_jazdy, ciel_text, zakaznik_id=None):
        super().__init__()
        self.auto_id = auto_id
        self.typ_jazdy = typ_jazdy
        self.ciel_text = ciel_text
        self.zakaznik_id = zakaznik_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        USER_STATES[interaction.user.id] = {
            'state': 'JAZDA_SPOLUJAZDEC',
            'auto_id': self.auto_id, 'typ_jazdy': self.typ_jazdy,
            'ciel_text': self.ciel_text, 'zakaznik_id': self.zakaznik_id,
            'km_start': self.km_start.value, 'km_koniec': self.km_koniec.value,
            'ucel': self.ucel.value
        }
        save_user_states()
        
        if self.typ_jazdy == "SUKROMNA":
            # Súkromná jazda preskakuje diéty a spolujazdcov
            await dokonci_jazdu_discord(interaction)
            return

        # Výber spolujazdca (zahrňuje interných zamestnancov, konateľov aj technikov)
        kolegovia: list[Zamestnanec] = await sync_to_async(
            lambda: list(
                Zamestnanec.objects.exclude(discord_id=interaction.user.id).filter(
                    Q(typ_pomeru__in=['IN', 'KO']) | Q(pracovna_rola='TE')
                )[:20]
            )
        )()
        
        options = [discord.SelectOption(label="Išiel som SÁM", value="0", emoji="👤")]
        for k in kolegovia:
            options.append(discord.SelectOption(label=f"{k.meno} {k.priezvisko}"[:100], value=str(k.id), emoji="👥"))
            
        select: discord.ui.Select[Any] = discord.ui.Select(placeholder="Bol s tebou kolega?", options=options)
        async def cb(i: discord.Interaction):
            await i.response.defer()
            spolu_id = int(select.values[0])
            USER_STATES[i.user.id]['spolujazdec_id'] = spolu_id if spolu_id != 0 else None
            save_user_states()
            await render_jazda_stat(i)
        setattr(select, 'callback', cb)
        
        view = discord.ui.View(timeout=None).add_item(select).add_item(BtnSpatMenu("ZAMESTNANEC"))
        await interaction.edit_original_response(content="👥 **SPOLUJAZDCI (Diéty)**\nBol s tebou v aute kolega?", view=view)

async def render_jazda_stat(interaction: discord.Interaction):
    domaci_stat, _ = await get_domaci_stat_a_mena_pre_bot_async()
    if not domaci_stat:
        identita = await sync_to_async(FiremnaIdentita.objects.first)()
        domaci_stat = getattr(identita, 'sidlo_stat', '') or 'Tuzemsko'
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label=f"🏠 {domaci_stat} (Tuzemsko)"[:80], style=discord.ButtonStyle.primary, custom_id=f"jstat_{domaci_stat}"[:100]))
    view.add_item(discord.ui.Button(label="🌍 Zahraničie", style=discord.ButtonStyle.secondary, custom_id="jstat_Zahraničie"))
    view.add_item(BtnSpatMenu("ZAMESTNANEC"))
    await interaction.edit_original_response(content="🌍 **ŠTÁT CIEĽA (Diéty)**\nV akom štáte sa nachádzal cieľ cesty?", view=view)

async def render_jazda_diety(interaction: discord.Interaction):
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="❌ Nie, nič z toho", style=discord.ButtonStyle.secondary, custom_id="jdiety_NIC"))
    view.add_item(discord.ui.Button(label="🛏️ Iba Nocľah", style=discord.ButtonStyle.primary, custom_id="jdiety_NOCLAH"))
    view.add_item(discord.ui.Button(label="🍽️ Iba Stravu", style=discord.ButtonStyle.primary, custom_id="jdiety_STRAVA"))
    view.add_item(discord.ui.Button(label="🛏️ + 🍽️ Oboje", style=discord.ButtonStyle.success, custom_id="jdiety_OBOJE"))
    view.add_item(BtnSpatMenu("ZAMESTNANEC"))
    await interaction.edit_original_response(content="🏨 **NOCĽAH A STRAVA**\nMal si zabezpečený bezplatný nocľah alebo stravu?", view=view)

async def dokonci_jazdu_discord(interaction: discord.Interaction):
    data = USER_STATES.pop(interaction.user.id, {})
    save_user_states()
    if not data or not isinstance(data, dict) or 'auto_id' not in data:
        await interaction.edit_original_response(
            content="⚠️ **Relácia vypršala.** Začni prosím zadávanie jazdy znova z hlavného menu.",
            view=discord.ui.View().add_item(BtnSpatMenu("ZAMESTNANEC"))
        )
        return
    
    success, msg = await ulozit_jazdu_db_discord(
        interaction.user.id, data['auto_id'], data.get('typ_jazdy', 'FIREMNA'),
        data.get('km_start'), data.get('km_koniec'), data.get('ciel_text', ''), data.get('ucel', ''),
        data.get('zakaznik_id'), data.get('spolujazdec_id'),
        data.get('stat_ciela', None), data.get('noclah', False), data.get('strava', False)
    )
    
    text = f"✅ **JAZDA ULOŽENÁ!**\n━━━━━━━━━━━━━━━━━━━━\n{msg}" if success else f"{msg}\n\n*Začni proces znova.*"
    spat_typ = "SUKROMNA" if data.get('typ_jazdy') == "SUKROMNA" else "ZAMESTNANEC"
    await interaction.edit_original_response(content=text, view=discord.ui.View().add_item(BtnSpatMenu(spat_typ)))

# --- NASTAVENIA ---
async def render_nastavenia(interaction: discord.Interaction, typ_uzivatela):
    discord_id = interaction.user.id
    stanica_id, stanica_nazov = await get_predvolena_cerpacka_discord(discord_id)
    txt_stanica = stanica_nazov if stanica_nazov else "Nenastavená"
    
    view = discord.ui.View(timeout=None)
    
    if typ_uzivatela == "ZAMESTNANEC":
        stav_discord, stav_mail = await get_stavy_notifikacii_discord(discord_id)
        txt_discord = "🟢 ZAPNUTÉ" if stav_discord else "🔴 VYPNUTÉ"
        txt_mail = "🟢 ZAPNUTÉ" if stav_mail else "🔴 VYPNUTÉ"
        
        text = (
            f"⚙️ **NASTAVENIA PROFILU**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"**Upozornenia na výjazdy:**\n"
            f"🎮 **Discord:** {txt_discord}\n"
            f"✉️ **E-mail:** {txt_mail}\n\n"
            f"**Tankovanie do firmy:**"
        )
        
        # Dynamické tlačidlá pre prepínanie
        btn_discord: discord.ui.Button[Any] = discord.ui.Button(label="🔕 Vypnúť Discord" if stav_discord else "🔔 Zapnúť Discord", style=discord.ButtonStyle.secondary)
        async def tog_discord(i: discord.Interaction):
            await i.response.defer()
            await prepni_nastavenie_notifikacii_discord(i.user.id, 'discord')
            await render_nastavenia(i, typ_uzivatela)
        setattr(btn_discord, 'callback', tog_discord)
        view.add_item(btn_discord)
        
        btn_mail: discord.ui.Button[Any] = discord.ui.Button(label="🔕 Vypnúť E-mail" if stav_mail else "🔔 Zapnúť E-mail", style=discord.ButtonStyle.secondary)
        async def tog_mail(i: discord.Interaction):
            await i.response.defer()
            await prepni_nastavenie_notifikacii_discord(i.user.id, 'mail')
            await render_nastavenia(i, typ_uzivatela)
        setattr(btn_mail, 'callback', tog_mail)
        view.add_item(btn_mail)
    else:
        text = "⚙️ **NASTAVENIA PROFILU**\n━━━━━━━━━━━━━━━━━━━━\n**Tankovanie:**"

    # Dynamické tlačidlo pre reset čerpačky
    if stanica_id:
        btn_cs_text = f"❌ Vymazať predvolenú ČS ({txt_stanica})"
        btn_cs_style = discord.ButtonStyle.danger
    else:
        btn_cs_text = "ℹ️ Ako nastaviť predvolenú ČS?"
        btn_cs_style = discord.ButtonStyle.secondary

    btn_cs: discord.ui.Button[Any] = discord.ui.Button(label=btn_cs_text[:80], style=btn_cs_style)
    
    async def reset_cs(i: discord.Interaction):
        s_id, _ = await get_predvolena_cerpacka_discord(i.user.id)
        
        if not s_id:
            # Ak ju nemá, pošleme mu "tajnú" správu (ephemeral), ktorú vidí len on
            await i.response.send_message("ℹ️ **Stanicu zatiaľ nemáš nastavenú.**\nNastaví sa automaticky, keď pôjdeš najbližšie tankovať a vyberieš si ju zo zoznamu.", ephemeral=True)
            return
            
        # Ak ju má, vymažeme ju
        await i.response.defer()
        await vymaz_predvolenu_cerpacku_discord(i.user.id)
        await i.followup.send("✅ **Stanica vymazaná.**\nPri najbližšom tankovaní sa ťa bot opýta na novú a tú si zapamätá.", ephemeral=True)
        await render_nastavenia(i, typ_uzivatela)
        
    setattr(btn_cs, 'callback', reset_cs)
    view.add_item(btn_cs)
    
    view.add_item(BtnSpatMenu(typ_uzivatela))

    try:
        await interaction.edit_original_response(content=text, view=view)
    except discord.HTTPException:
        pass

class ServisZakaznikSearchModal(discord.ui.Modal, title='Hľadať zákazníka pre servis'):
    hladany_text: discord.ui.TextInput = discord.ui.TextInput(label='Názov firmy', required=True)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        zakaznici = await vyhladaj_zakaznika_pre_bot_async(self.hladany_text.value)
        if not zakaznici:
            await interaction.followup.send("⚠️ Nenašiel som žiadneho zákazníka.", ephemeral=True)
            return
            
        # 🛡️ DEFENSIVE: Striktný limit Discord API (max 25 možností v Select Menu)
        options = [discord.SelectOption(label=z['Nazov_spolocnosti'][:100], value=str(z['id']), emoji="🏢") for z in zakaznici[:25]]
        select: discord.ui.Select[Any] = discord.ui.Select(placeholder="Vyber zákazníka...", options=options)
        
        async def cb_zakaznik(i: discord.Interaction):
            await i.response.defer()
            z_id = int(select.values[0])
            USER_STATES[i.user.id] = {'state': 'SERVIS_FLOW', 'zakaznik_id': z_id}
            save_user_states()
            
            zariadenia = await get_zariadenia_pre_zakaznika_bot(z_id)
            if not zariadenia:
                dev_mnozno_zapor = get_industry_term('zariadenia_mnozno_zapor', 'zariadenia, technologické celky ani položky')
                await i.edit_original_response(content=f"⚠️ Tento zákazník nemá evidované žiadne {dev_mnozno_zapor}.", view=discord.ui.View().add_item(BtnSpatMenu("ZAMESTNANEC")))
                return
                
            opt_zar = [
                discord.SelectOption(
                    label=(f"{z['kategoria_display']} | {z['nazov'] or z['model_zariadenia']} ({z['vyrobne_cislo']})"[:78] + f" [{z.get('zaruka_badge', '')}]")[:100],
                    value=str(z['id']),
                    description=f"Výrobné číslo: {z['vyrobne_cislo']} | Status: {z.get('zaruka_badge', '')}"[:100]
                ) for z in zariadenia[:25]
            ]
            dev_title = get_industry_term('zariadenie_fallback_nazov', 'Zariadenie')
            sel_zar: discord.ui.Select[Any] = discord.ui.Select(placeholder=f"Vyber {dev_title.lower()}..."[:100], options=opt_zar)
            
            async def cb_zariadenie(i2: discord.Interaction):
                await i2.response.defer()
                zar_id = int(sel_zar.values[0])
                USER_STATES[i2.user.id]['zariadenie_id'] = zar_id
                save_user_states()
                
                typy = await get_typy_servisu_bot()
                opt_typ = [discord.SelectOption(label=t['nazov'][:100], value=str(t['id']), emoji="🛠️") for t in typy[:25]]
                sel_typ: discord.ui.Select[Any] = discord.ui.Select(placeholder="Vyber typ práce...", options=opt_typ)
                
                async def cb_typ(i3: discord.Interaction):
                    await i3.response.defer()
                    typ_id = int(sel_typ.values[0])
                    USER_STATES[i3.user.id]['typ_id'] = typ_id
                    USER_STATES[i3.user.id]['sh_cart'] = {}
                    save_user_states()
                    await render_sh_kosik_discord(i3)
                
                setattr(sel_typ, 'callback', cb_typ)
                await i2.edit_original_response(content="🛠️ **Vyber typ vykonanej práce:**", view=discord.ui.View().add_item(sel_typ).add_item(BtnSpatMenu("ZAMESTNANEC")))

            setattr(sel_zar, 'callback', cb_zariadenie)
            await i.edit_original_response(content=f"⚙️ **Vyber {dev_title.lower()}:**", view=discord.ui.View().add_item(sel_zar).add_item(BtnSpatMenu("ZAMESTNANEC")))
            
        setattr(select, 'callback', cb_zakaznik)
        view = discord.ui.View(timeout=None).add_item(select).add_item(BtnSpatMenu("ZAMESTNANEC"))
        await interaction.edit_original_response(content="🤝 **Výsledky hľadania:**", view=view)

async def render_sh_kosik_discord(interaction: discord.Interaction):
    cart = USER_STATES.get(interaction.user.id, {}).get('sh_cart', {}) if isinstance(USER_STATES.get(interaction.user.id), dict) else {}
    polozka_label = get_industry_term('polozka_fallback_nazov', 'Položka')
    text = f"🛒 **POUŽITÝ MATERIÁL A {polozka_label.upper()}**\n━━━━━━━━━━━━━━━━━━━━\n"
    
    if not cart:
        text += "*Zatiaľ neboli pridané žiadne položky.*\n\n"
    else:
        for key, item in cart.items():
            text += f"▪️ **{item['count']} {item['jednotka']}** - {item['nazov']} (Sklad: {item['wh_name']})\n"
        text += "\n"
        
    view = discord.ui.View(timeout=None)
    
    btn_add: discord.ui.Button[Any] = discord.ui.Button(label=f"➕ Pridať ({polozka_label})"[:80], style=discord.ButtonStyle.primary)
    async def cb_add(i: discord.Interaction):
        await i.response.defer()
        USER_STATES[i.user.id]['sh_wh_id'] = 0 # Natvrdo Hlavný sklad (Katalóg)
        save_user_states()
        await render_sh_sklad_cat_discord(i)
    setattr(btn_add, 'callback', cb_add)
    view.add_item(btn_add)
    
    if cart:
        polozka_label_lower = polozka_label.lower()
        btn_del: discord.ui.Button[Any] = discord.ui.Button(label=f"❌ Odstrániť ({polozka_label_lower})"[:80], style=discord.ButtonStyle.danger)
        async def cb_del(i: discord.Interaction):
            await i.response.defer()
            view_del = discord.ui.View(timeout=None)
            
            # Použijeme Select Menu pre čisté UI a ochranu pred limitom 25 tlačidiel
            cart_items = list(cart.items())[:25]
            del_options = [
                discord.SelectOption(
                    label=f"{item['nazov']} ({item['count']} {item['jednotka']})"[:100],
                    value=k,
                    emoji="🗑️"
                ) for k, item in cart_items
            ]
            
            del_select: discord.ui.Select[Any] = discord.ui.Select(placeholder=f"Zvoľ {polozka_label_lower} na vymazanie..."[:100], options=del_options)
            
            async def cb_select_del(i2: discord.Interaction):
                await i2.response.defer()
                key_to_del = del_select.values[0]
                if key_to_del in USER_STATES.get(i2.user.id, {}).get('sh_cart', {}):
                    del USER_STATES[i2.user.id]['sh_cart'][key_to_del]
                    save_user_states()
                await render_sh_kosik_discord(i2)
                
            setattr(del_select, 'callback', cb_select_del)
            view_del.add_item(del_select)
            
            btn_back: discord.ui.Button[Any] = discord.ui.Button(label="🔙 Späť do košíka", style=discord.ButtonStyle.secondary)
            async def cb_back_del(i2: discord.Interaction):
                await i2.response.defer()
                await render_sh_kosik_discord(i2)
            setattr(btn_back, 'callback', cb_back_del)
            view_del.add_item(btn_back)
            
            await i.edit_original_response(content=f"🗑️ **Vyber {polozka_label_lower}, ktorú chceš z košíka odstrániť:**", view=view_del)
        setattr(btn_del, 'callback', cb_del)
        view.add_item(btn_del)
        
    btn_go: discord.ui.Button[Any] = discord.ui.Button(label="➡️ Pokračovať", style=discord.ButtonStyle.success)
    async def cb_go(i: discord.Interaction):
        await i.response.defer()
        await render_sh_media_discord(i)
    setattr(btn_go, 'callback', cb_go)
    view.add_item(btn_go)
    
    view.add_item(BtnSpatMenu("ZAMESTNANEC"))
    await interaction.edit_original_response(content=text, view=view)

async def render_sh_media_discord(interaction: discord.Interaction):
    text = "📸 **FOTOGRAFIE A VIDEO (Voliteľné)**\n━━━━━━━━━━━━━━━━━━━━\nChceš k záznamu priložiť fotky alebo video z opravy?"
    
    data = USER_STATES.get(interaction.user.id, {})
    f_pred = "✅ Nahraté" if data.get('sh_foto_pred') else "Pridať"
    f_po = "✅ Nahraté" if data.get('sh_foto_po') else "Pridať"
    v_vid = "✅ Nahraté" if data.get('sh_video') else "Pridať"

    view = discord.ui.View(timeout=None)
    
    btn_pred: discord.ui.Button[Any] = discord.ui.Button(label=f"📸 Foto PRED ({f_pred})", style=discord.ButtonStyle.secondary)
    async def cb_pred(i: discord.Interaction):
        await i.response.defer()
        USER_STATES[i.user.id]['state'] = 'CAKAM_FOTO_PRED'
        save_user_states()
        await i.edit_original_response(content="📸 **Pošli fotku PRED servisom.**", view=discord.ui.View().add_item(BtnSpatMenu("ZAMESTNANEC")))
    setattr(btn_pred, 'callback', cb_pred)
    view.add_item(btn_pred)

    btn_po: discord.ui.Button[Any] = discord.ui.Button(label=f"📸 Foto PO ({f_po})", style=discord.ButtonStyle.secondary)
    async def cb_po(i: discord.Interaction):
        await i.response.defer()
        USER_STATES[i.user.id]['state'] = 'CAKAM_FOTO_PO'
        save_user_states()
        await i.edit_original_response(content="📸 **Pošli fotku PO servise.**", view=discord.ui.View().add_item(BtnSpatMenu("ZAMESTNANEC")))
    setattr(btn_po, 'callback', cb_po)
    view.add_item(btn_po)

    btn_vid: discord.ui.Button[Any] = discord.ui.Button(label=f"🎥 Video ({v_vid})", style=discord.ButtonStyle.secondary)
    async def cb_vid(i: discord.Interaction):
        await i.response.defer()
        USER_STATES[i.user.id]['state'] = 'CAKAM_VIDEO'
        save_user_states()
        await i.edit_original_response(content="🎥 **Pošli krátke video z opravy.**", view=discord.ui.View().add_item(BtnSpatMenu("ZAMESTNANEC")))
    setattr(btn_vid, 'callback', cb_vid)
    view.add_item(btn_vid)

    btn_go: discord.ui.Button[Any] = discord.ui.Button(label="➡️ Pokračovať na hlasovku", style=discord.ButtonStyle.success)
    async def cb_go(i: discord.Interaction):
        await i.response.defer()
        
        # 🚀 ISAS DRAFT SYNC (Presunuté z košíka)
        technik = await sync_to_async(lambda: Zamestnanec.objects.filter(discord_id=i.user.id).first())()
        if not technik:
            await i.edit_original_response(content="⚠️ **Chyba:** Nenašiel sa profil technika pre tvoj účet.", view=discord.ui.View().add_item(BtnSpatMenu("ZAMESTNANEC")))
            return
            
        if i.user.id not in USER_STATES or not isinstance(USER_STATES[i.user.id], dict):
            USER_STATES[i.user.id] = {}
        USER_STATES[i.user.id]['technik_db_id'] = technik.id
        save_user_states()
        draft_text = cache.get(f"isas_quicknote_draft_{technik.id}")
        
        if draft_text:
            text_hlas = (
                "📝 **NAŠIEL SOM TVOJE POZNÁMKY Z DIAGNOSTIKY:**\n"
                f"*{draft_text}*\n\n"
                "**Ako ich chceš použiť pre servisný záznam?**"
            )
            view_draft = discord.ui.View(timeout=None)
            
            btn_only: discord.ui.Button[Any] = discord.ui.Button(label="📥 Importovať a rovno uložiť", style=discord.ButtonStyle.success)
            async def cb_only(i2: discord.Interaction):
                await i2.response.defer()
                USER_STATES[i2.user.id]['use_draft'] = True
                save_user_states()
                msg = await i2.edit_original_response(content="🔄 *Spracovávam importované poznámky...*", view=None)
                
                class FakeMessage:
                    def __init__(self, interaction, original_msg):
                        self.author = interaction.user
                        self.channel = interaction.channel
                        self.guild = interaction.guild
                        self.attachments = []
                        self._msg = original_msg
                        self.content = ""

                    async def reply(self, content=None, view=None, **kwargs):
                        return await self._msg.edit(content=content, view=view)

                    async def delete(self):
                        pass

                fake_msg = FakeMessage(i2, msg)
                await spracuj_servis_hlasovka_discord(fake_msg, is_text_only=True)
            setattr(btn_only, 'callback', cb_only)
            view_draft.add_item(btn_only)
            
            btn_voice: discord.ui.Button[Any] = discord.ui.Button(label="🎤 Importovať a dopovedať hlasom", style=discord.ButtonStyle.primary)
            async def cb_voice(i2: discord.Interaction):
                await i2.response.defer()
                USER_STATES[i2.user.id]['state'] = 'CAKAM_HLASOVKU_SERVIS'
                USER_STATES[i2.user.id]['use_draft'] = True
                save_user_states()
                await i2.edit_original_response(content="🎤 **POZNÁMKY PRIPRAVENÉ NA IMPORT**\n━━━━━━━━━━━━━━━━━━━━\nTeraz mi pošli hlasovku s detailmi, ktoré chceš k poznámkam doplniť.", view=discord.ui.View().add_item(BtnSpatMenu("ZAMESTNANEC")))
            setattr(btn_voice, 'callback', cb_voice)
            view_draft.add_item(btn_voice)
            
            btn_ignore: discord.ui.Button[Any] = discord.ui.Button(label="🚫 Ignorovať (Nahrať iba hlasovku)", style=discord.ButtonStyle.secondary)
            async def cb_ignore(i2: discord.Interaction):
                await i2.response.defer()
                USER_STATES[i2.user.id]['state'] = 'CAKAM_HLASOVKU_SERVIS'
                USER_STATES[i2.user.id]['use_draft'] = False
                save_user_states()
                await i2.edit_original_response(content="🎤 **POZNÁMKY ZAHODENÉ**\n━━━━━━━━━━━━━━━━━━━━\nPošli mi hlasovku s kompletnými detailmi servisu.", view=discord.ui.View().add_item(BtnSpatMenu("ZAMESTNANEC")))
            setattr(btn_ignore, 'callback', cb_ignore)
            view_draft.add_item(btn_ignore)
            
            view_draft.add_item(BtnSpatMenu("ZAMESTNANEC"))
            await i.edit_original_response(content=text_hlas, view=view_draft)
            
        else:
            USER_STATES[i.user.id]['state'] = 'CAKAM_HLASOVKU_SERVIS'
            USER_STATES[i.user.id]['use_draft'] = False
            save_user_states()
            text_hlas = (
                "🎤 **VŠETKO PRIPRAVENÉ**\n━━━━━━━━━━━━━━━━━━━━\n\n"
                "Pošli mi sem hlasovú správu (Voice Message) a povedz detaily.\n\n"
                "🎤 **Čakám na tvoju hlasovku...**"
            )
            await i.edit_original_response(content=text_hlas, view=discord.ui.View().add_item(BtnSpatMenu("ZAMESTNANEC")))
            
    setattr(btn_go, 'callback', cb_go)
    view.add_item(btn_go)
    view.add_item(BtnSpatMenu("ZAMESTNANEC"))
    
    await interaction.edit_original_response(content=text, view=view)

async def render_sh_sklad_cat_discord(interaction: discord.Interaction):
    view = discord.ui.View(timeout=None)
    for val, name in CATEGORY_CHOICES:
        btn: discord.ui.Button[Any] = discord.ui.Button(label=f"📂 {name}", style=discord.ButtonStyle.primary)
        async def cb(i: discord.Interaction, cat_val=val):
            await i.response.defer()
            await render_sh_sklad_sub_discord(i, cat_val)
        setattr(btn, 'callback', cb)
        view.add_item(btn)
        
    btn_back: discord.ui.Button[Any] = discord.ui.Button(label="🔙 Späť do košíka", style=discord.ButtonStyle.secondary)
    async def cb_back_cat(i: discord.Interaction):
        await i.response.defer()
        await render_sh_kosik_discord(i)
    setattr(btn_back, 'callback', cb_back_cat)
    view.add_item(btn_back)
    await interaction.edit_original_response(content="🗂️ **Vyber kategóriu položky:**", view=view)

async def render_sh_sklad_sub_discord(interaction: discord.Interaction, cat_name):
    subcats = CATEGORY_MAP.get(cat_name, {})
    view = discord.ui.View(timeout=None)
    for sub_name, model_class in subcats.items():
        btn: discord.ui.Button[Any] = discord.ui.Button(label=f"📁 {sub_name}", style=discord.ButtonStyle.primary)
        async def cb(i: discord.Interaction, m_name=model_class.__name__):
            await i.response.defer()
            await render_sh_sklad_item_discord(i, m_name, 0)
        setattr(btn, 'callback', cb)
        view.add_item(btn)
        
    btn_back: discord.ui.Button[Any] = discord.ui.Button(label="🔙 Späť do košíka", style=discord.ButtonStyle.secondary)
    async def cb_back_sub(i: discord.Interaction):
        await i.response.defer()
        await render_sh_kosik_discord(i)
    setattr(btn_back, 'callback', cb_back_sub)
    view.add_item(btn_back)
    await interaction.edit_original_response(content=f"📂 **{cat_name.upper()}**\nZvoľ podkategóriu:", view=view)

class ShSkladCountModalDiscord(discord.ui.Modal, title='Množstvo použitej položky'):
    count: discord.ui.TextInput = discord.ui.TextInput(label='Zadaj presný počet kusov (číslo)', required=True)
    def __init__(self, model_name, pk):
        super().__init__()
        self.model_name = model_name
        self.pk = pk
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        pocet_str = self.count.value.strip()
        if not pocet_str.isdigit() or int(pocet_str) <= 0:
            await interaction.followup.send("⚠️ Musíš zadať platné celé číslo väčšie ako 0.", ephemeral=True)
            return
            
        count = int(pocet_str)
        # 🛡️ DEFENSIVE: Bezpečné načítanie bez rizika KeyError pri reštarte pamäte
        wh_id = USER_STATES.get(interaction.user.id, {}).get('sh_wh_id', 0) if isinstance(USER_STATES.get(interaction.user.id), dict) else 0
        model_class, cat_name, sub_name = await get_model_class_by_name_async(self.model_name)
        
        try:
            item = await sync_to_async(model_class.objects.get)(pk=self.pk)
            wh_name = "📕 Katalóg položiek"
                
            key = f"{self.model_name}_{self.pk}_{wh_id}"
            if interaction.user.id not in USER_STATES or not isinstance(USER_STATES[interaction.user.id], dict):
                USER_STATES[interaction.user.id] = {}
            cisty_nazov_polozky = getattr(item, 'obchodny_nazov', None) or getattr(item, 'seriove_cislo', None) or str(item)
            USER_STATES[interaction.user.id].setdefault('sh_cart', {})[key] = {
                'model_name': self.model_name, 'pk': self.pk, 'count': count,
                'nazov': cisty_nazov_polozky, 'jednotka': getattr(item, 'merna_jednotka', 'ks'),
                'wh_id': wh_id, 'wh_name': wh_name, 'cat_name': cat_name, 'sub_name': sub_name
            }
            save_user_states()
        except Exception as e:
            logging.error(f"Chyba pri ukladaní položky do košíka Discord: {e}")
            
        await render_sh_kosik_discord(interaction)

async def render_sh_sklad_item_discord(interaction: discord.Interaction, model_name, page=0):
    wh_id = USER_STATES.get(interaction.user.id, {}).get('sh_wh_id', 0) if isinstance(USER_STATES.get(interaction.user.id), dict) else 0
    model_class, cat, subcat_name = await get_model_class_by_name_async(model_name)
    start_idx = page * 10
    end_idx = start_idx + 10
    
    total_count, stavy = await get_stavy_pre_bota_async(model_class, wh_id, "", start_idx, end_idx)
    view = discord.ui.View(timeout=None)
    
    if total_count > 0:
        row_idx = 0
        for idx, (item, stav) in enumerate(stavy):
            if idx > 0 and idx % 3 == 0: row_idx += 1
            jednotka = getattr(item, 'merna_jednotka', 'ks')
            nazov_polozky = getattr(item, 'obchodny_nazov', None) or getattr(item, 'seriove_cislo', None) or str(item)
            btn: discord.ui.Button[Any] = discord.ui.Button(label=f"📦 {nazov_polozky} ({stav}{jednotka})"[:80], style=discord.ButtonStyle.secondary, row=row_idx)
            async def cb(i: discord.Interaction, pk=item.pk):
                await i.response.send_modal(ShSkladCountModalDiscord(model_name, pk))
            setattr(btn, 'callback', cb)
            view.add_item(btn)

    if page > 0:
        btn_prev: discord.ui.Button[Any] = discord.ui.Button(label="⬅️", style=discord.ButtonStyle.primary, row=4)
        async def cb_prev(i: discord.Interaction):
            await i.response.defer()
            await render_sh_sklad_item_discord(i, model_name, page - 1)
        setattr(btn_prev, 'callback', cb_prev)
        view.add_item(btn_prev)
        
    if end_idx < total_count:
        btn_next: discord.ui.Button[Any] = discord.ui.Button(label="➡️", style=discord.ButtonStyle.primary, row=4)
        async def cb_next(i: discord.Interaction):
            await i.response.defer()
            await render_sh_sklad_item_discord(i, model_name, page + 1)
        setattr(btn_next, 'callback', cb_next)
        view.add_item(btn_next)

    btn_back: discord.ui.Button[Any] = discord.ui.Button(label="🔙 Späť do košíka", style=discord.ButtonStyle.secondary, row=4)
    async def cb_back_item(i: discord.Interaction):
        await i.response.defer()
        await render_sh_kosik_discord(i)
    setattr(btn_back, 'callback', cb_back_item)
    view.add_item(btn_back)

    polozka_label = get_industry_term('polozka_fallback_nazov', 'položku')
    await interaction.edit_original_response(content=f"📦 **VÝBER POLOŽKY**\nZvoľ konkrétnu {polozka_label}:", view=view)

# ==================================================================
# MEDZISKLADOVÝ PRESUN (DISCORD UI)
# ==================================================================
async def render_presun_zdroj(interaction: discord.Interaction):
    filialky = await get_zoznam_filialok_pre_bot_discord(interaction.user.id)
    if not filialky:
        await interaction.edit_original_response(content="⚠️ **Prístup zamietnutý**\nNemáš prístup k žiadnej filiálke.", view=discord.ui.View().add_item(BtnSpatMenu("ZAMESTNANEC")))
        return

    options = [discord.SelectOption(label="Hlavný sklad", value="0", emoji="🏢")]
    for f in filialky[:24]: # Discord limit je 25 možností v Selecte
        options.append(discord.SelectOption(label=f['nazov'][:100], value=str(f['id']), emoji="🏭"))

    select: discord.ui.Select[Any] = discord.ui.Select(placeholder="Odkiaľ presúvaš tovar? (Zdroj)", options=options)
    
    async def cb(i: discord.Interaction):
        await i.response.defer()
        zdroj_id = int(select.values[0])
        USER_STATES[i.user.id] = {'state': 'PRESUN', 'zdroj_id': zdroj_id}
        save_user_states()
        await render_presun_ciel(i, zdroj_id, filialky)
        
    setattr(select, 'callback', cb)
    view = discord.ui.View(timeout=None).add_item(select).add_item(BtnSpatMenu("ZAMESTNANEC"))
    await interaction.edit_original_response(content="🔄 **MEDZISKLADOVÝ PRESUN**\n━━━━━━━━━━━━━━━━━━━━\n⬇️ **ODKIAĽ** presúvaš tovar? (Zdroj)", view=view)

async def render_presun_ciel(interaction: discord.Interaction, zdroj_id, filialky):
    options = []
    if zdroj_id != 0:
        options.append(discord.SelectOption(label="Hlavný sklad", value="0", emoji="🏢"))
        
    for f in filialky[:24]:
        if f['id'] != zdroj_id:
            options.append(discord.SelectOption(label=f['nazov'][:100], value=str(f['id']), emoji="🏭"))

    select: discord.ui.Select[Any] = discord.ui.Select(placeholder="Kam presúvaš tovar? (Cieľ)", options=options)
    
    async def cb(i: discord.Interaction):
        await i.response.defer()
        USER_STATES[i.user.id]['ciel_id'] = int(select.values[0])
        save_user_states()
        await render_presun_kategorie(i)
        
    setattr(select, 'callback', cb)
    
    btn_back: discord.ui.Button[Any] = discord.ui.Button(label="🔙 Späť na Zdroj", style=discord.ButtonStyle.secondary)
    async def cb_back_zdroj(i: discord.Interaction):
        await i.response.defer()
        await render_presun_zdroj(i)
    setattr(btn_back, 'callback', cb_back_zdroj)
    
    view = discord.ui.View(timeout=None).add_item(select).add_item(btn_back).add_item(BtnSpatMenu("ZAMESTNANEC"))
    await interaction.edit_original_response(content="🔄 **MEDZISKLADOVÝ PRESUN**\n━━━━━━━━━━━━━━━━━━━━\n⬆️ **KAM** presúvaš tovar? (Cieľ)", view=view)

async def render_presun_kategorie(interaction: discord.Interaction):
    view = discord.ui.View(timeout=None)
    for val, name in CATEGORY_CHOICES:
        btn: discord.ui.Button[Any] = discord.ui.Button(label=f"📂 {name}", style=discord.ButtonStyle.primary)
        async def cb(i: discord.Interaction, cat_val=val):
            await i.response.defer()
            await render_presun_podkategorie(i, cat_val)
        setattr(btn, 'callback', cb)
        view.add_item(btn)
        
    view.add_item(BtnSpatMenu("ZAMESTNANEC"))
    await interaction.edit_original_response(content="🗂️ **VÝBER KATEGÓRIE**\n━━━━━━━━━━━━━━━━━━━━\nZvoľ hlavnú kategóriu presúvaného tovaru:", view=view)

async def render_presun_podkategorie(interaction: discord.Interaction, cat_name):
    subcats = CATEGORY_MAP.get(cat_name, {})
    view = discord.ui.View(timeout=None)
    
    for sub_name, model_class in subcats.items():
        btn: discord.ui.Button[Any] = discord.ui.Button(label=f"📁 {sub_name}", style=discord.ButtonStyle.primary)
        async def cb(i: discord.Interaction, m_name=model_class.__name__):
            await i.response.defer()
            await render_presun_polozky(i, m_name, page=0, search_term="")
        setattr(btn, 'callback', cb)
        view.add_item(btn)
        
    btn_back: discord.ui.Button[Any] = discord.ui.Button(label="🔙 Späť na kategórie", style=discord.ButtonStyle.secondary)
    async def cb_back_kategorie(i: discord.Interaction):
        await i.response.defer()
        await render_presun_kategorie(i)
    setattr(btn_back, 'callback', cb_back_kategorie)
    view.add_item(btn_back)
    
    await interaction.edit_original_response(content=f"📂 **{cat_name.upper()}**\n━━━━━━━━━━━━━━━━━━━━\nZvoľ podkategóriu:", view=view)

class PresunSearchModal(discord.ui.Modal, title='Hľadať položku na presun'):
    hladany_text: discord.ui.TextInput = discord.ui.TextInput(label='Názov alebo sériové číslo', required=True)
    def __init__(self, model_name):
        super().__init__()
        self.model_name = model_name
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await render_presun_polozky(interaction, self.model_name, page=0, search_term=self.hladany_text.value)

class PresunCountModal(discord.ui.Modal, title='Množstvo na presun'):
    count: discord.ui.TextInput = discord.ui.TextInput(label='Zadaj presný počet kusov (číslo)', required=True)
    def __init__(self, model_name, pk):
        super().__init__()
        self.model_name = model_name
        self.pk = pk
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        pocet_str = self.count.value.strip()
        if not pocet_str.isdigit() or int(pocet_str) <= 0:
            await interaction.followup.send("⚠️ Musíš zadať platné celé číslo väčšie ako 0.", ephemeral=True)
            return
            
        data = USER_STATES.get(interaction.user.id, {})
        success, sprava = await uloz_medziskladovy_presun_db_discord(
            interaction.user.id, data.get('zdroj_id', 0), data.get('ciel_id', 0),
            self.model_name, self.pk, int(pocet_str)
        )
        
        if success:
            text = f"✅ **PRESUN VYKONANÝ**\n━━━━━━━━━━━━━━━━━━━━\n{sprava}"
        else:
            text = f"❌ **CHYBA PRESUNU**\n━━━━━━━━━━━━━━━━━━━━\n{sprava}"
            
        await interaction.edit_original_response(content=text, view=discord.ui.View().add_item(BtnSpatMenu("ZAMESTNANEC")))

async def render_presun_polozky(interaction: discord.Interaction, model_name, page=0, search_term=""):
    model_class, cat, subcat_name = await get_model_class_by_name_async(model_name)
    zdroj_id = USER_STATES.get(interaction.user.id, {}).get('zdroj_id', 0)
    
    start_idx = page * 10
    end_idx = start_idx + 10
    total_count, stavy = await get_stavy_pre_bota_async(model_class, zdroj_id, search_term, start_idx, end_idx)
    
    view = discord.ui.View(timeout=None)
    
    if total_count > 0:
        row_idx = 0
        for idx, (item, stav) in enumerate(stavy):
            if idx > 0 and idx % 3 == 0: row_idx += 1
            jednotka = getattr(item, 'merna_jednotka', 'ks')
            nazov_polozky = getattr(item, 'obchodny_nazov', None) or getattr(item, 'seriove_cislo', None) or str(item)
            btn_label = f"📦 {nazov_polozky} ({stav}{jednotka})"[:80]
            
            btn: discord.ui.Button[Any] = discord.ui.Button(label=btn_label, style=discord.ButtonStyle.secondary, row=row_idx)
            async def cb(i: discord.Interaction, pk=item.pk):
                await i.response.send_modal(PresunCountModal(model_name, pk))
            setattr(btn, 'callback', cb)
            view.add_item(btn)

    if page > 0:
        btn_prev: discord.ui.Button[Any] = discord.ui.Button(label="⬅️", style=discord.ButtonStyle.primary, row=4)
        async def cb_presun_prev(i: discord.Interaction):
            await i.response.defer()
            await render_presun_polozky(i, model_name, page - 1, search_term)
        setattr(btn_prev, 'callback', cb_presun_prev)
        view.add_item(btn_prev)
        
    if end_idx < total_count:
        btn_next: discord.ui.Button[Any] = discord.ui.Button(label="➡️", style=discord.ButtonStyle.primary, row=4)
        async def cb_presun_next(i: discord.Interaction):
            await i.response.defer()
            await render_presun_polozky(i, model_name, page + 1, search_term)
        setattr(btn_next, 'callback', cb_presun_next)
        view.add_item(btn_next)

    btn_search: discord.ui.Button[Any] = discord.ui.Button(label="🔍 Hľadať", style=discord.ButtonStyle.success, row=4)
    async def search_cb(i: discord.Interaction):
        await i.response.send_modal(PresunSearchModal(model_name))
    setattr(btn_search, 'callback', search_cb)
    view.add_item(btn_search)

    if search_term:
        btn_clear: discord.ui.Button[Any] = discord.ui.Button(label="❌ Zrušiť filter", style=discord.ButtonStyle.danger, row=4)
        async def cb_presun_clear(i: discord.Interaction):
            await i.response.defer()
            await render_presun_polozky(i, model_name, 0, "")
        setattr(btn_clear, 'callback', cb_presun_clear)
        view.add_item(btn_clear)

    btn_back: discord.ui.Button[Any] = discord.ui.Button(label="🔙 Späť", style=discord.ButtonStyle.secondary, row=4)
    async def cb_presun_back(i: discord.Interaction):
        await i.response.defer()
        await render_presun_kategorie(i)
    setattr(btn_back, 'callback', cb_presun_back)
    view.add_item(btn_back)

    text = f"📦 **VÝBER POLOŽKY NA PRESUN**\n━━━━━━━━━━━━━━━━━━━━\n📁 Kategória: **{subcat_name.upper()}**\n"
    if search_term: text += f"🔍 *Filter: {search_term}*\n"
    text += f"Celkový počet položiek: {total_count}" if total_count > 0 else "⚠️ *Nenašli sa žiadne položky.*"
    
    try:
        await interaction.edit_original_response(content=text, view=view)
    except discord.HTTPException:
        pass


# --- SKLAD: VYHĽADÁVANIE (MODAL) ---
# V Discorde je hľadanie lepšie ako v TG - vyskočí ti okienko, kde napíšeš text.
class SkladSearchModal(discord.ui.Modal, title='Vyhľadávanie v sklade'):
    hladany_text: discord.ui.TextInput = discord.ui.TextInput(label='Názov alebo sériové číslo', required=True)

    def __init__(self, wh_id, model_name):
        super().__init__()
        self.wh_id = wh_id
        self.model_name = model_name

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await render_sklad_polozky(interaction, self.wh_id, self.model_name, page=0, search_term=self.hladany_text.value)

# --- SKLAD: 1. VÝBER SKLADU ---
async def render_sklad_vyber(interaction: discord.Interaction):
    # 🟢 ISAS FIX: Použijeme Discord verziu funkcie pre Access Control
    filialky = await get_zoznam_filialok_pre_bot_discord(interaction.user.id)
    view = discord.ui.View(timeout=None)
    
    # Tlačidlo Hlavný sklad
    btn_main: discord.ui.Button[Any] = discord.ui.Button(label="🏢 Hlavný sklad", style=discord.ButtonStyle.primary)
    async def cb_main(i: discord.Interaction):
        await i.response.defer()
        await render_sklad_kategorie(i, 0)
    setattr(btn_main, 'callback', cb_main)
    view.add_item(btn_main)

    # Tlačidlá pre Filiálky (🛡️ DEFENSIVE: Max 23 položiek, aby sa s Hlavným skladom a Späť neprekročil limit 25 prvkov vo View)
    for f in filialky[:23]:
        btn_label = f"🏭 {f['nazov']}"[:80]
        btn_f: discord.ui.Button[Any] = discord.ui.Button(label=btn_label, style=discord.ButtonStyle.primary)
        # Hack pre zachytenie premennej v loope
        async def cb_f(i: discord.Interaction, f_id=f['id']):
            await i.response.defer()
            await render_sklad_kategorie(i, f_id)
        setattr(btn_f, 'callback', cb_f)
        view.add_item(btn_f)
        
    view.add_item(BtnSpatMenu("ZAMESTNANEC"))
    await interaction.edit_original_response(content="📦 **VÝBER SKLADU**\n━━━━━━━━━━━━━━━━━━━━\nKde sa práve nachádzaš?", view=view)

# --- SKLAD: 2. KATEGÓRIE ---
async def render_sklad_kategorie(interaction: discord.Interaction, wh_id):
    view = discord.ui.View(timeout=None)
    
    for val, name in CATEGORY_CHOICES:
        btn: discord.ui.Button[Any] = discord.ui.Button(label=f"📂 {name}", style=discord.ButtonStyle.primary)
        async def cb(i: discord.Interaction, cat_val=val):
            await i.response.defer()
            await render_sklad_podkategorie(i, wh_id, cat_val)
        setattr(btn, 'callback', cb)
        view.add_item(btn)
        
    btn_back: discord.ui.Button[Any] = discord.ui.Button(label="🔙 Zmeniť sklad", style=discord.ButtonStyle.secondary)
    async def cb_sklad_vyber_back(i: discord.Interaction):
        await i.response.defer()
        await render_sklad_vyber(i)
    setattr(btn_back, 'callback', cb_sklad_vyber_back)
    view.add_item(btn_back)
    
    await interaction.edit_original_response(content="📂 **KATEGÓRIE**\n━━━━━━━━━━━━━━━━━━━━\nVyber hlavnú kategóriu:", view=view)

# --- SKLAD: 3. PODKATEGÓRIE ---
async def render_sklad_podkategorie(interaction: discord.Interaction, wh_id, cat_name):
    subcats = CATEGORY_MAP.get(cat_name, {})
    view = discord.ui.View(timeout=None)
    
    for sub_name, model_class in subcats.items():
        btn: discord.ui.Button[Any] = discord.ui.Button(label=f"📁 {sub_name}", style=discord.ButtonStyle.primary)
        async def cb(i: discord.Interaction, m_name=model_class.__name__):
            await i.response.defer()
            await render_sklad_polozky(i, wh_id, m_name, page=0, search_term="")
        setattr(btn, 'callback', cb)
        view.add_item(btn)
        
    btn_back: discord.ui.Button[Any] = discord.ui.Button(label="🔙 Späť na kategórie", style=discord.ButtonStyle.secondary)
    async def cb_sklad_cat_back(i: discord.Interaction):
        await i.response.defer()
        await render_sklad_kategorie(i, wh_id)
        setattr(btn_back, 'callback', cb_sklad_cat_back)
        view.add_item(btn_back)
    
    await interaction.edit_original_response(content=f"📁 **{cat_name.upper()}**\n━━━━━━━━━━━━━━━━━━━━\nZvoľ si konkrétnu podkategóriu:", view=view)

# --- SKLAD: 4. POLOŽKY (10 na stranu) ---
async def render_sklad_polozky(interaction: discord.Interaction, wh_id, model_name, page=0, search_term=""):
    model_class, cat, subcat_name = await get_model_class_by_name_async(model_name)
    start_idx = page * 10
    end_idx = start_idx + 10
    
    total_count, stavy = await get_stavy_pre_bota_async(model_class, wh_id, search_term, start_idx, end_idx)
    
    view = discord.ui.View(timeout=None)
    
    # Vykreslenie položiek (Discord limit: max 5 radov. Dáme 3 položky do radu)
    if total_count > 0:
        row_idx = 0
        for idx, (item, stav) in enumerate(stavy):
            if idx > 0 and idx % 3 == 0: row_idx += 1 # Každé 3 tlačidlá nový rad
            
            jednotka = getattr(item, 'merna_jednotka', 'ks')
            nazov_polozky = getattr(item, 'obchodny_nazov', None) or getattr(item, 'seriove_cislo', None) or str(item)
            btn_label = f"📦 {nazov_polozky} ({stav}{jednotka})"[:80]
            
            btn: discord.ui.Button[Any] = discord.ui.Button(label=btn_label, style=discord.ButtonStyle.secondary, row=row_idx)
            async def cb(i: discord.Interaction, pk=item.pk):
                await i.response.defer()
                await render_sklad_uprava(i, wh_id, model_name, pk, 0)
            setattr(btn, 'callback', cb)
            view.add_item(btn)

    # Rad 4: Navigácia a Hľadanie (Posledný povolený rad v Discorde)
    if page > 0:
        btn_prev: discord.ui.Button[Any] = discord.ui.Button(label="⬅️", style=discord.ButtonStyle.primary, row=4)
        async def cb_sklad_prev(i: discord.Interaction):
            await i.response.defer()
            await render_sklad_polozky(i, wh_id, model_name, page - 1, search_term)
        setattr(btn_prev, 'callback', cb_sklad_prev)
        view.add_item(btn_prev)
        
    if end_idx < total_count:
        btn_next: discord.ui.Button[Any] = discord.ui.Button(label="➡️", style=discord.ButtonStyle.primary, row=4)
        async def cb_sklad_next(i: discord.Interaction):
            await i.response.defer()
            await render_sklad_polozky(i, wh_id, model_name, page + 1, search_term)
        setattr(btn_next, 'callback', cb_sklad_next)
        view.add_item(btn_next)

    btn_search: discord.ui.Button[Any] = discord.ui.Button(label="🔍 Hľadať", style=discord.ButtonStyle.success, row=4)
    async def search_cb(i: discord.Interaction):
        await i.response.send_modal(SkladSearchModal(wh_id, model_name))
    setattr(btn_search, 'callback', search_cb)
    view.add_item(btn_search)

    if search_term:
        btn_clear: discord.ui.Button[Any] = discord.ui.Button(label="❌ Zrušiť filter", style=discord.ButtonStyle.danger, row=4)
        async def cb_sklad_clear(i: discord.Interaction):
            await i.response.defer()
            await render_sklad_polozky(i, wh_id, model_name, 0, "")
        setattr(btn_clear, 'callback', cb_sklad_clear)
        view.add_item(btn_clear)

    btn_back: discord.ui.Button[Any] = discord.ui.Button(label="🔙 Späť", style=discord.ButtonStyle.secondary, row=4)
    async def cb_sklad_back_sub(i: discord.Interaction):
        await i.response.defer()
        await render_sklad_podkategorie(i, wh_id, cat)
    setattr(btn_back, 'callback', cb_sklad_back_sub)
    view.add_item(btn_back)


    text = f"📁 Kategória: **{subcat_name.upper()}**\n━━━━━━━━━━━━━━━━━━━━\n"
    if search_term: text += f"🔍 *Filter: {search_term}*\n"
    text += f"Celkový počet položiek: {total_count}" if total_count > 0 else "⚠️ *Nenašli sa žiadne položky.*"
    
    try:
        await interaction.edit_original_response(content=text, view=view)
    except discord.HTTPException:
        pass

async def render_sklad_uprava(interaction: discord.Interaction, wh_id, model_name, pk, change):
    model_class, cat_name, subcat_name = await get_model_class_by_name_async(model_name)
    item, aktualny_stav = await get_item_stav_pre_bota_discord(model_class, pk, wh_id)
    
    if not item:
        await interaction.edit_original_response(content="⚠️ **Položka už neexistuje!**", view=discord.ui.View().add_item(BtnSpatMenu("ZAMESTNANEC")))
        return

    sign = "+" if change > 0 else ""
    future_stock = aktualny_stav + change
    jednotka = getattr(item, 'merna_jednotka', 'ks')
    
    view = discord.ui.View(timeout=None)
    
    # Rad 0: Tlačidlá pre zmenu
    zmeny = [("-5", -5), ("-1", -1), (f"Zmena: {sign}{change}", 0), ("+1", 1), ("+5", 5)]
    for label, val in zmeny:
        style = discord.ButtonStyle.secondary if val == 0 else discord.ButtonStyle.primary
        btn: discord.ui.Button[Any] = discord.ui.Button(label=label, style=style, row=0, disabled=(val==0))
        if val != 0:
            async def cb(i: discord.Interaction, v=val):
                await i.response.defer()
                await render_sklad_uprava(i, wh_id, model_name, pk, change + v)
            setattr(btn, 'callback', cb)
        view.add_item(btn)

    # Rad 1: Uložiť (Presmerovanie na výber typu dokladu)
    btn_save: discord.ui.Button[Any] = discord.ui.Button(label="✅ ULOŽIŤ ZMENU DO ISAS CORE", style=discord.ButtonStyle.success, row=1)
    async def save_cb(i: discord.Interaction):
        await i.response.defer()
        if change == 0:
            await i.followup.send("⚠️ Musíš zadať nejakú zmenu (nie nulu)!", ephemeral=True)
            return
        await render_sklad_vyber_typu_dokladu_discord(i, wh_id, model_name, pk, change)
        
    setattr(btn_save, 'callback', save_cb)
    view.add_item(btn_save)

    # Rad 2: Späť
    btn_back: discord.ui.Button[Any] = discord.ui.Button(label="🔙 Späť na výber", style=discord.ButtonStyle.secondary, row=2)
    async def back_to_items(i: discord.Interaction):
        await i.response.defer()
        await render_sklad_polozky(i, wh_id, model_name, 0, "")
    setattr(btn_back, 'callback', back_to_items)
    view.add_item(btn_back)

    wh_name = "Hlavný sklad" if wh_id == 0 else f"Filiálka #{wh_id}"
    text = (
        f"📦 **ÚPRAVA SKLADOVEJ POLOŽKY**\n━━━━━━━━━━━━━━━━━━━━\n"
        f"🏢 **Sklad:** {wh_name}\n"
        f"🏷 **Položka:** `{getattr(item, 'obchodny_nazov', None) or getattr(item, 'seriove_cislo', None) or str(item)}`\n"
        f"📦 **Aktuálne na sklade:** {aktualny_stav} {jednotka}\n\n"
        f"Tlačidlami zvoľ, o aké množstvo sa sklad mení.\n"
        f"🔄 **Zmena:** {sign}{change} {jednotka}\n"
        f"🏁 **Bude na sklade:** {future_stock} {jednotka}"
    )
    try:
        await interaction.edit_original_response(content=text, view=view)
    except discord.HTTPException:
        pass


async def render_sklad_vyber_typu_dokladu_discord(interaction: discord.Interaction, wh_id, model_name, pk, change):
    model_class, cat_name, subcat_name = await get_model_class_by_name_async(model_name)
    view = discord.ui.View(timeout=None)

    def vytvor_callback(typ_dokladu):
        async def cb(i: discord.Interaction):
            await i.response.defer()
            item_cislo, jed, meno_technika, real_change, wh_name, kon_stav = await uloz_zmenu_skladu_db_discord(
                i.user.id, model_class, pk, change, cat_name, subcat_name, wh_id, typ_dokladu
            )
            
            if real_change is None or item_cislo is None:
                await i.edit_original_response(content="⚠️ **Chyba:** Položka už neexistuje.", view=discord.ui.View().add_item(BtnSpatMenu("ZAMESTNANEC")))
                return
                
            text = (
                f"✅ **ÚSPEŠNE ZAPÍSANÉ**\n━━━━━━━━━━━━━━━━━━━━\n"
                f"🏢 Sklad: **{wh_name}**\n"
                f"Položka: `{item_cislo}`\n"
                f"Zmena: **{'+' if real_change > 0 else ''}{real_change} {jed}**\n"
                f"Konečný stav: **{kon_stav} {jed}**\n\n"
                f"*ISAS Systém synchronizovaný.*"
            )
            await i.edit_original_response(content=text, view=discord.ui.View().add_item(BtnSpatMenu("ZAMESTNANEC")))

            try:
                gn = await sync_to_async(GlobalneNastavenia.objects.first)()
                pred_stav = kon_stav - real_change
                znamenko = "+" if real_change > 0 else ""
                akcia_ikona = "📥" if real_change > 0 else "📤"
                akcia_text = "PRIDANIE NA SKLAD" if real_change > 0 else "VÝDAJ ZO SKLADU"
                base_url = gn.clean_base_url if gn else getattr(settings, 'SITE_URL', '')
                admin_url = f"{base_url}/admin/Evidencia/{model_name.lower()}/{pk}/change/"
                majitel_discord = getattr(gn, 'discord_id_majitel', None)

                payload = (
                    ISASNotificationBuilder(
                        nadpis=f"POHYB NA SKLADE - {akcia_text}",
                        ikona=akcia_ikona,
                        farba_temy="#2563eb" if real_change > 0 else "#ea580c"
                    )
                    .pridaj_riadok("Vykonal:", f"{safe_discord(meno_technika)} 🎮 <i>(cez Discord bota)</i>")
                    .pridaj_riadok("Sklad:", safe_discord(wh_name))
                    .pridaj_riadok("Kategória:", f"{safe_discord(cat_name)} / {safe_discord(subcat_name)}")
                    .pridaj_riadok("Položka:", f"{safe_discord(item_cislo)} (Stav: {pred_stav} {jed} ➔ {kon_stav} {jed})")
                    .pridaj_riadok("Pohyb:", f"<b>{znamenko}{real_change} {jed}</b>")
                    .nastav_tlacidlo("Otvoriť detail položky", admin_url)
                    .nastav_ai_poznamku("Skladová evidencia bola úspešne synchronizovaná na diaľku.")
                    .build()
                )
                await sync_to_async(ISASNotifier.send)(
                    payload=payload, discord_id=majitel_discord, send_tg=True, send_mail=True, send_discord=True
                )
            except Exception as notif_err:
                logging.error(f"Chyba pri odosielaní notifikácie pohybu na sklade z Discordu: {notif_err}")
        return cb

    if change > 0:
        btn_prijem: discord.ui.Button[Any] = discord.ui.Button(label="📥 Príjemka (Nákup)", style=discord.ButtonStyle.primary)
        setattr(btn_prijem, 'callback', vytvor_callback("PRIJEMKA"))
        view.add_item(btn_prijem)

        btn_prebytok: discord.ui.Button[Any] = discord.ui.Button(label="📋 Inventúrny prebytok", style=discord.ButtonStyle.success)
        setattr(btn_prebytok, 'callback', vytvor_callback("INVENTURA"))
        view.add_item(btn_prebytok)
    else:
        btn_spotreba: discord.ui.Button[Any] = discord.ui.Button(label="🔧 Vnútropodniková spotreba", style=discord.ButtonStyle.primary)
        setattr(btn_spotreba, 'callback', vytvor_callback("SPOTREBA"))
        view.add_item(btn_spotreba)

        btn_vydaj: discord.ui.Button[Any] = discord.ui.Button(label="📤 Výdajka (Predaj)", style=discord.ButtonStyle.secondary)
        setattr(btn_vydaj, 'callback', vytvor_callback("VYDAJKA"))
        view.add_item(btn_vydaj)

        btn_manko: discord.ui.Button[Any] = discord.ui.Button(label="📋 Inventúrne manko", style=discord.ButtonStyle.danger)
        setattr(btn_manko, 'callback', vytvor_callback("INVENTURA"))
        view.add_item(btn_manko)

    btn_back: discord.ui.Button[Any] = discord.ui.Button(label="🔙 Späť na úpravu", style=discord.ButtonStyle.secondary)
    async def back_cb(i: discord.Interaction):
        await i.response.defer()
        await render_sklad_uprava(i, wh_id, model_name, pk, change)
    setattr(btn_back, 'callback', back_cb)
    view.add_item(btn_back)

    text = "📑 **TYP POHYBU / DOKLADU**\n━━━━━━━━━━━━━━━━━━━━\nVyber, o aký typ pohybu ide, aby to účtovníčke sedelo:"
    try:
        await interaction.edit_original_response(content=text, view=view)
    except discord.HTTPException:
        pass

class HlavneMenu(discord.ui.View):
    def __init__(self, typ_uzivatela):
        super().__init__(timeout=None)
        
        if typ_uzivatela == "ZAMESTNANEC":
            self.add_item(discord.ui.Button(label="📖 MOJE ÚLOHY", style=discord.ButtonStyle.primary, custom_id="menu_ulohy", row=0))
            self.add_item(discord.ui.Button(label="🛠️ NAHLÁSIŤ SERVIS", style=discord.ButtonStyle.primary, custom_id="menu_servis", row=0))
            
            self.add_item(discord.ui.Button(label="📦 SPRÁVA SKLADU", style=discord.ButtonStyle.secondary, custom_id="menu_sklad", row=1))
            self.add_item(discord.ui.Button(label="🔄 PRESUN SKLAD", style=discord.ButtonStyle.secondary, custom_id="menu_presun", row=1))
            
            self.add_item(discord.ui.Button(label="🚧 MOJE VSTUPY", style=discord.ButtonStyle.secondary, custom_id="menu_vstupy", row=2))
            self.add_item(discord.ui.Button(label="📸 NAHRAŤ FAKTÚRU", style=discord.ButtonStyle.secondary, custom_id="menu_faktura", row=2))
            
            self.add_item(discord.ui.Button(label="🚗 FIREMNÁ JAZDA", style=discord.ButtonStyle.primary, custom_id="menu_jazda_firemna", row=3))
            self.add_item(discord.ui.Button(label="⛽ TANKOVANIE", style=discord.ButtonStyle.danger, custom_id="menu_tankovanie", row=3))
            
            self.add_item(discord.ui.Button(label="🧠 ISAS AI ASISTENT", style=discord.ButtonStyle.secondary, custom_id="menu_ai", row=4))
            self.add_item(discord.ui.Button(label="⚙️ NASTAVENIA", style=discord.ButtonStyle.secondary, custom_id="menu_nastavenia", row=4))
            self.add_item(discord.ui.Button(label="🔒 UZAMKNÚŤ", style=discord.ButtonStyle.danger, custom_id="menu_lock", row=4))
            
        else:
            self.add_item(discord.ui.Button(label="🚗 SÚKROMNÁ JAZDA", style=discord.ButtonStyle.primary, custom_id="menu_jazda_sukromna", row=0))
            self.add_item(discord.ui.Button(label="⛽ TANKOVANIE", style=discord.ButtonStyle.danger, custom_id="menu_tankovanie", row=0))
            
            self.add_item(discord.ui.Button(label="🧠 ISAS AI ASISTENT", style=discord.ButtonStyle.secondary, custom_id="menu_ai", row=1))
            self.add_item(discord.ui.Button(label="⚙️ NASTAVENIA", style=discord.ButtonStyle.secondary, custom_id="menu_nastavenia", row=2))
            self.add_item(discord.ui.Button(label="🔒 UZAMKNÚŤ", style=discord.ButtonStyle.danger, custom_id="menu_lock", row=3))

# discord.ext.tasks.loop striktne vyžaduje explicitné tzinfo v objekte datetime.time
bot_tz: datetime.tzinfo
try:
    bot_tz = ZoneInfo(getattr(settings, 'TIME_ZONE', 'UTC'))
except Exception:
    try:
        bot_tz = timezone.get_current_timezone()
    except Exception:
        bot_tz = datetime.timezone.utc

cas_pripomienky = datetime.time(hour=17, minute=0, tzinfo=bot_tz)

@tasks.loop(time=cas_pripomienky)
async def cron_pripomienka_uloh():
    data = await ziskaj_zajtrajsie_ulohy_pre_discord()
    for d in data:
        builder = (
            ISASNotificationBuilder(
                nadpis=f"PLÁN VÝJAZDOV NA ZAJTRA ({safe_discord(d['meno']).upper()})",
                ikona="🔔",
                farba_temy="#3b82f6"
            )
            .nastav_hlavny_text("Systém ISAS CORE eviduje na zajtrajší deň nasledovné naplánované úlohy:")
        )

        for u in d['ulohy']:
            builder.pridaj_riadok(f"⏰ {safe_discord(u['cas'])} - {safe_discord(u['zakaznik'])}:", safe_discord(u['praca']))

        builder.nastav_ai_poznamku("ISAS Core Dispatcher: Skontroluj si pripravenosť materiálu a vozidla pred výjazdom. Prajeme úspešný a bezpečný deň! 🍀")
        payload = builder.build()

        try:
            await sync_to_async(ISASNotifier.send)(
                payload=payload,
                discord_id=d['discord_id'],
                send_tg=False,
                send_mail=False,
                send_discord=True
            )
        except Exception as e:
            logging.error(f"Chyba pri odosielaní Cron pripomienky na Discord cez ISASNotifier (User ID {d.get('discord_id')}): {e}")

@bot.event
async def on_ready():
    print(f'🟢 ISAS Discord Backup Bot je online ako {bot.user}')
    bot.add_view(BtnOdomknut())  # Zabezpečí funkčnosť tlačidla odomknutia aj po reštarte bota
    try:
        await bot.tree.sync()
        print('⚡ Slash príkazy pre Discord úspešne zosynchronizované.')
    except Exception as e:
        logging.warning(f'Zlyhala synchronizácia Slash príkazov: {e}')
    if not cron_pripomienka_uloh.is_running():
        cron_pripomienka_uloh.start()
        print('⏰ Cron Job pre pripomienky úloh (17:00) bol spustený.')

@bot.command(name="start", aliases=["menu"])
async def start_command(ctx):
    try: 
        await ctx.message.delete()
    except Exception: 
        pass # 🧹 Zmaže príkaz !start / !menu
    if ctx.author.id in USER_STATES and isinstance(USER_STATES[ctx.author.id], dict):
        USER_STATES[ctx.author.id]['state'] = None
        save_user_states()
    obj, typ = await identifikuj_pouzivatela_discord(ctx.author.id)
    if obj:
        is_technik = getattr(obj, 'pracovna_rola', '') == 'TE'
        uz_odpovedal = await get_radar_status_discord(ctx.author.id)
        
        if is_technik and not uz_odpovedal:
            text = (
                "📍 **Zdieľanie polohy (Dispečing)**\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "⚠️ **Upozornenie:** Aplikácia Discord nepodporuje zdieľanie GPS polohy.\n"
                "Na dispečerskom radare budeš zobrazený ako 'Bez polohy' a dispečer ťa bude v prípade potreby kontaktovať telefonicky."
            )
            view = discord.ui.View().add_item(BtnRozumiemDiscord(typ))
            await ctx.send(text, view=view)
            return

        meno = getattr(obj, 'meno', getattr(obj, 'meno_a_priezvisko', 'Neznámy'))
        text = f"**ISAS CORE TERMINÁL** 🟢\n━━━━━━━━━━━━━━━━━━━━\nVitaj, **{meno}**.\nVyber požadovanú akciu z menu:"
        await send_clean_menu_discord(ctx.channel, ctx.author.id, text, HlavneMenu(typ))
    else:
        await ctx.send("🔒 **Neautorizovaný prístup!**\nPre spárovanie napíš: `!login 0911222333`")

@bot.command(name="login")
async def login_command(ctx, *, telefon: str | None = None):
    """Príkaz na spárovanie účtu (Náhrada za Zdieľať kontakt)."""
    try: 
        await ctx.message.delete()
    except Exception: 
        pass # 🧹 Zmaže príkaz !login

    if not telefon:
        await ctx.send("⚠️ Musíš zadať telefónne číslo. Príklad: `!login 0911222333`")
        return

    obj, typ = await sparuj_discord_ucet(telefon, ctx.author.id)
    
    if obj:
        meno = getattr(obj, 'meno', getattr(obj, 'meno_a_priezvisko', 'Neznámy'))
        await ctx.send(f"✅ Účet úspešne spárovaný! Vitaj, **{meno}**.\nNapíš `!start` pre zobrazenie menu.")
    else:
        await ctx.send("❌ **Prístup zamietnutý!**\nToto telefónne číslo neevidujeme v systéme ISAS CORE.", delete_after=10.0) # 🧹 Zmizne po 10 sekundách

@bot.tree.command(name="menu", description="📋 Otvoriť hlavné menu ISAS Core")
async def slash_menu(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    obj, typ = await identifikuj_pouzivatela_discord(interaction.user.id)
    if obj:
        if interaction.user.id in USER_STATES and isinstance(USER_STATES[interaction.user.id], dict):
            USER_STATES[interaction.user.id]['state'] = None
            save_user_states()
        try:
            await interaction.delete_original_response()
        except Exception:
            pass
        meno = getattr(obj, 'meno', getattr(obj, 'meno_a_priezvisko', 'Neznámy'))
        text = f"**ISAS CORE TERMINÁL** 🟢\n━━━━━━━━━━━━━━━━━━━━\nVitaj, **{meno}**.\nVyber požadovanú akciu z menu:"
        await send_clean_menu_discord(interaction.channel, interaction.user.id, text, HlavneMenu(typ))
    else:
        await interaction.followup.send("🔒 **Neautorizovaný prístup!**\nPre spárovanie napíš: `!login 0911222333`", ephemeral=True)

@bot.event
async def on_message(message):
    # Ignorujeme správy od iných botov (aj od seba samého)
    if message.author.bot:
        return

    content_clean = message.content.strip().lower()

    # Rýchle vyvolanie menu bežným textom kedykoľvek
    if content_clean in ["menu", "start", "hlavne menu", "hlavné menu"]:
        if message.author.id in USER_STATES and isinstance(USER_STATES[message.author.id], dict):
            USER_STATES[message.author.id]['state'] = None
            save_user_states()
        ctx = await bot.get_context(message)
        await start_command(ctx)
        return

    # Ak správa začína príkazu s prefixom (!start, !login, !menu), okamžite ho spracujeme
    if message.content.startswith('!'):
        await bot.process_commands(message)
        return

    user_state_data = USER_STATES.get(message.author.id, {})
    current_state = user_state_data.get('state') if isinstance(user_state_data, dict) else user_state_data
    
    # --- SPRACOVANIE FAKTÚRY ---
    if current_state == 'CAKAM_FAKTURU':
        if not message.attachments:
            try: await message.delete()
            except Exception: pass
            await message.channel.send(f"<@{message.author.id}> ⚠️ **Chyba:** Musíš nahrať súbor (Fotku alebo PDF).", delete_after=5.0)
            return
            
        USER_STATES.pop(message.author.id, None)
        save_user_states()
        status_msg = await message.reply("🔄 *Prijímam dokument a analyzujem cez Gemini AI...*")
        
        try:
            attachment = message.attachments[0]
            file_bytes = await attachment.read()
            filename = f"faktura_dc_{message.author.id}_{uuid.uuid4().hex[:8]}_{attachment.filename}"
            
            faktura_id = await ulozit_fakturu_z_discordu(message.author.id, file_bytes, filename)
            poznamka = await skontroluj_fakturu_po_ai(faktura_id)
            
            if poznamka and "Error" in poznamka:
                gn = await sync_to_async(GlobalneNastavenia.objects.first)()
                base_url = gn.clean_base_url if gn else getattr(settings, 'SITE_URL', '')
                admin_url = f"{base_url}/admin/Evidencia/prichodziafaktura/{faktura_id}/change/"
                majitel_discord = getattr(gn, 'discord_id_majitel', None)

                payload = (
                    ISASNotificationBuilder(
                        nadpis="FAKTÚRA: ZLYHANIE AI VYŤAŽENIA",
                        ikona="⚠️",
                        farba_temy="#f59e0b"
                    )
                    .pridaj_riadok("Nahral technik ID:", str(message.author.id))
                    .pridaj_riadok("Faktúra ID:", f"#{faktura_id}")
                    .nastav_hlavny_text(
                        f"Faktúra nahraná cez Discord bola archivovaná, avšak automatické AI vyťaženie zlyhalo.<br>"
                        f"<b>Chyba AI:</b> <code>{safe_discord(poznamka[:200])}</code>"
                    )
                    .nastav_tlacidlo("Otvoriť a spracovať manuálne", admin_url)
                    .nastav_ai_poznamku("ISAS Invoice Parser: Vyžaduje sa manuálne nahodenie položiek do ERP.")
                    .build()
                )
                await sync_to_async(ISASNotifier.send)(
                    payload=payload, discord_id=majitel_discord, send_tg=True, send_mail=True, send_discord=True
                )

                text = f"⚠️ **FAKTÚRA ULOŽENÁ, ALE AI ZLYHALA**\n━━━━━━━━━━━━━━━━━━━━\nSúbor bol uložený, ale vyťaženie zlyhalo:\n`{poznamka}`\n\n*Administratíva bola upozornená na manuálnu kontrolu.*"
            else:
                text = "✅ **FAKTÚRA BOLA ÚSPEŠNE PRIJATÁ!**\n━━━━━━━━━━━━━━━━━━━━\nDokument bol bezpečne archivovaný. AI modul ISAS Core ho práve vyťažuje na pozadí.\nDáta budú pripravené na kontrolu v Logu Naskladnenia."
                
            await status_msg.edit(content=text, view=discord.ui.View().add_item(BtnSpatMenu("ZAMESTNANEC")))
        except Exception as e:
            await status_msg.edit(content=f"❌ **Kritická chyba:**\n`{str(e)[:400]}`", view=discord.ui.View().add_item(BtnSpatMenu("ZAMESTNANEC")))

    # --- SPRACOVANIE BLOČKU Z TANKOVANIA ---
    elif current_state == 'CAKAM_BLOCEK':
        if not message.attachments:
            try: await message.delete()
            except Exception: pass
            await message.channel.send(f"<@{message.author.id}> ⚠️ **Chyba:** Musíš nahrať fotku bločku.", delete_after=5.0)
            return
            
        data = USER_STATES.pop(message.author.id, None)
        save_user_states()
        if not data or not isinstance(data, dict) or not all(k in data for k in ['auto_id', 'km', 'litre', 'cena']):
            await message.reply(
                "⚠️ **Relácia vypršala!** Začni prosím proces tankovania znova z hlavného menu.",
                view=discord.ui.View().add_item(BtnSpatMenu("ZAMESTNANEC"))
            )
            return

        status_msg = await message.reply("🔄 *Ukladám tankovanie do systému ISAS CORE...*")
        foto_path = f"temp_tankovanie_dc_{message.author.id}_{uuid.uuid4().hex[:8]}.jpg"
        
        try:
            attachment = message.attachments[0]
            await attachment.save(foto_path)
            
            success, upozornenie = await ulozit_tankovanie_db_discord(
                message.author.id, data['auto_id'], data.get('km'), data.get('litre'), 
                data.get('cena'), data.get('platba', 'PALIVOVA'), data.get('stanica_id'), foto_path
            )
            
            if success:
                text = "✅ **TANKOVANIE ULOŽENÉ**\n━━━━━━━━━━━━━━━━━━━━\nBloček a dáta boli bezpečne archivované."
                if upozornenie: text += f"\n{upozornenie}"
            else:
                text = f"❌ **TANKOVANIE SA NEPODARILO ULOŽIŤ**\n━━━━━━━━━━━━━━━━━━━━\n{upozornenie}\n\n*Začni proces tankovania znova z menu.*"
            
            await status_msg.edit(content=text, view=discord.ui.View().add_item(BtnSpatMenu("ZAMESTNANEC")))
        except Exception as e:
            await status_msg.edit(content=f"❌ **Chyba pri ukladaní bločku:**\n`{str(e)}`", view=discord.ui.View().add_item(BtnSpatMenu("ZAMESTNANEC")))
        finally:
            if os.path.exists(foto_path):
                try: os.remove(foto_path)
                except Exception: pass
        return

    # --- SPRACOVANIE MÉDIÍ PRE SERVIS ---
    elif current_state in ['CAKAM_FOTO_PRED', 'CAKAM_FOTO_PO', 'CAKAM_VIDEO']:
        if not message.attachments:
            try: await message.delete()
            except Exception: pass
            await message.channel.send(f"<@{message.author.id}> ⚠️ **Chyba:** Musíš nahrať súbor.", delete_after=5.0)
            return
            
        attachment = message.attachments[0]
        if attachment.size > 25000000:
            await message.channel.send(f"<@{message.author.id}> ⚠️ **Chyba:** Súbor je príliš veľký (Max 25 MB).", delete_after=5.0)
            return

        ext = os.path.splitext(attachment.filename)[1]
        path = f"temp_servis_dc_{message.author.id}_{uuid.uuid4().hex[:8]}{ext}"
        
        try:
            await attachment.save(path)
        except Exception as e:
            await message.channel.send(f"<@{message.author.id}> ⚠️ **Chyba pri sťahovaní:** {str(e)}", delete_after=5.0)
            return
        
        if message.author.id not in USER_STATES or not isinstance(USER_STATES[message.author.id], dict):
            USER_STATES[message.author.id] = {}
            
        if current_state == 'CAKAM_FOTO_PRED': USER_STATES[message.author.id]['sh_foto_pred'] = path
        elif current_state == 'CAKAM_FOTO_PO': USER_STATES[message.author.id]['sh_foto_po'] = path
        elif current_state == 'CAKAM_VIDEO': USER_STATES[message.author.id]['sh_video'] = path
        
        USER_STATES[message.author.id]['state'] = 'SERVIS_FLOW'
        save_user_states()
        
        class FakeInteraction:
            user = message.author
            async def edit_original_response(self, content, view):
                await message.reply(content=content, view=view)
        await render_sh_media_discord(FakeInteraction())
        return

    # --- SPRACOVANIE HLASOVKY PRE SERVIS ---
    elif current_state == 'CAKAM_HLASOVKU_SERVIS':
        if not message.attachments:
            try: 
                await message.delete() 
            except Exception: 
                pass # 🧹 Zmaže zlý text

            await message.channel.send(f"<@{message.author.id}> ⚠️ **Chyba:** Musíš poslať hlasovú správu (Voice Message).", delete_after=5.0)
            return
        await spracuj_servis_hlasovka_discord(message, is_text_only=False)
        return

    # --- SPRACOVANIE ISAS AI ASISTENTA (TEXT + AUDIO + PRÍLOHY) ---
    elif current_state == 'AI_CHAT':
        # 🛡️ EDGE CASE 1: Zámok súbežnosti proti spamu a double-clickom
        lock_key = f"ai_lock_dc_{message.author.id}"
        if cache.get(lock_key):
            try: await message.delete()
            except Exception: pass
            await message.channel.send(f"<@{message.author.id}> ⏳ **Stále premýšľam nad predchádzajúcou otázkou.** Počkaj prosím na odpoveď.", delete_after=5.0)
            return

        audio_temp_path = None
        files_temp_paths = []
        typ = None
        subjekt_id = None
        status_msg = await message.reply("🧠 *ISAS AI premýšľa nad odpoveďou...*")

        try:
            cache.set(lock_key, True, timeout=120)
            user_question = message.content.strip()
            obj, typ = await identifikuj_pouzivatela_discord(message.author.id)
            if not obj:
                await status_msg.edit(content="🔒 **Prístup zamietnutý:** Tvoj profil nie je spárovaný so systémom ISAS CORE.", view=None)
                return

            is_superuser = False
            ma_pristup_k_financiam = False
            aktualny_pouzivatel = "Neznámy"
            rola_pouzivatela = "Neznáma"
            typ_pomeru = "Neznámy"

            if typ == "ZAMESTNANEC" and obj:
                django_user = getattr(obj, 'user', None)
                is_superuser = getattr(django_user, 'is_superuser', False)
                aktualny_pouzivatel = f"{obj.meno} {obj.priezvisko}".strip()
                rola_pouzivatela = obj.get_pracovna_rola_display() if hasattr(obj, 'get_pracovna_rola_display') else str(obj.pracovna_rola)
                typ_pomeru = obj.get_typ_pomeru_display() if hasattr(obj, 'get_typ_pomeru_display') else str(obj.typ_pomeru)
                if is_superuser or obj.typ_pomeru == 'KO' or obj.pracovna_rola == 'AD':
                    ma_pristup_k_financiam = True
            elif typ == "SUKROMNA" and obj:
                subjekt_id = obj.id
                aktualny_pouzivatel = getattr(obj, 'meno_a_priezvisko', 'Neznámy')
                rola_pouzivatela = "Zákazník"
                typ_pomeru = "Súkromná osoba"

            # 🛡️ 1. KONTROLA DENNÉHO LIMITU SPRÁV (Overenie PRED sťahovaním akýchkoľvek príloh)
            dnes = timezone.now().date()
            gn = await sync_to_async(GlobalneNastavenia.objects.first)()

            user_kwargs = {}
            cache_limit_key = None
            if typ == "ZAMESTNANEC" and obj:
                django_user = getattr(obj, 'user', None)
                if django_user:
                    user_kwargs['pouzivatel'] = django_user
                else:
                    # 🛡️ DEFENSIVE: Izolovaný limit pre technikov bez naviazaného Django konta (zamedzí blokovaniu kolegov)
                    cache_limit_key = f"ai_limit_zam_{obj.id}_{dnes.strftime('%Y%m%d')}"

                if is_superuser or obj.typ_pomeru == 'KO':
                    denny_limit = getattr(gn, 'ai_limit_superadmin', 500)
                elif obj.pracovna_rola in ['DI', 'AD']:
                    denny_limit = getattr(gn, 'ai_limit_management', 100)
                else:
                    denny_limit = getattr(gn, 'ai_limit_technik', 25)
            else:
                user_kwargs['sukromna_osoba'] = obj
                denny_limit = getattr(gn, 'ai_limit_default', 10)

            if cache_limit_key:
                pouzite_dnes = cache.get(cache_limit_key) or 0
            else:
                pouzite_dnes = await sync_to_async(AIAuditLog.objects.filter(**user_kwargs, cas_dopytu__date=dnes).count)()

            if pouzite_dnes >= denny_limit:
                await status_msg.edit(content=f"🛑 **Dosiahol si svoj denný limit ({denny_limit} správ pre AI).**\nLimit sa obnoví o polnoci.", view=discord.ui.View().add_item(BtnSpatMenu(typ or "ZAMESTNANEC")))
                return

            # 🛡️ 2. SPRACOVANIE A SŤAHOVANIE PRÍLOH (Až po úspešnom overení kvóty)
            if message.attachments:
                att = message.attachments[0]
                if att.size > 25 * 1024 * 1024:
                    await status_msg.edit(content="🛑 **Súbor je príliš veľký.** Maximálna povolená veľkosť je 25 MB.", view=discord.ui.View().add_item(BtnSpatMenu(typ or "ZAMESTNANEC")))
                    return

                ext = os.path.splitext(att.filename)[1].lower()
                audio_extensions = ['.ogg', '.mp3', '.wav', '.m4a', '.aac', '.flac']

                if ext in audio_extensions:
                    audio_temp_path = f"ai_voice_dc_{message.author.id}_{uuid.uuid4().hex[:8]}{ext}"
                    await att.save(audio_temp_path)
                else:
                    # 🛡️ RBAC OCHRANA: Obrázky a dokumenty môžu posielať výhradne zamestnanci
                    if typ != "ZAMESTNANEC":
                        await status_msg.edit(content="⚠️ **Prístup zamietnutý:** Analýza súborov a fotografií je dostupná výhradne pre zamestnancov firmy.", view=discord.ui.View().add_item(BtnSpatMenu(typ or "SUKROMNA")))
                        return
                    file_path = f"ai_file_dc_{message.author.id}_{uuid.uuid4().hex[:8]}{ext}"
                    await att.save(file_path)
                    files_temp_paths.append(file_path)

            # 📝 2. PREDBEŽNÝ AUDIT LOG
            if files_temp_paths:
                otazka_text = f"[Príloha] {user_question}" if user_question else "[Príloha]"
            elif audio_temp_path:
                otazka_text = f"[Hlasová správa] {user_question}" if user_question else "[Hlasová správa]"
            else:
                otazka_text = user_question or "Dopyt"
                
            audit_log = await sync_to_async(AIAuditLog.objects.create)(**user_kwargs, otazka=otazka_text, zdroj_poziadavky='DISCORD')

            # 🛡️ EDGE CASE 2: Zabezpečenie zmysluplného promptu, ak používateľ pošle iba súbor bez textu
            final_prompt = user_question
            if not final_prompt and files_temp_paths:
                final_prompt = "Analyzuj priložený súbor a poskytni k nemu podrobný prehľad a relevantné fakty."

            # 🛡️ EDGE CASE 3: Načítanie konverzačnej pamäte používateľa
            history_text = USER_STATES.get(message.author.id, {}).get('history', "")

            # 🧠 3. VOLANIE AI S HISTÓRIOU A PLNÝM RBAC KONTEXTOM
            ai_response = await ask_isas_ai_async(
                user_question=final_prompt if final_prompt else None,
                history_text=history_text,
                audio_path=audio_temp_path,
                file_paths=files_temp_paths if files_temp_paths else None,
                aktualny_pouzivatel=aktualny_pouzivatel,
                rola_pouzivatela=rola_pouzivatela,
                typ_pomeru=typ_pomeru,
                is_superuser=is_superuser,
                ma_pristup_k_financiam=ma_pristup_k_financiam,
                subjekt_id=subjekt_id
            )
            
            if isinstance(ai_response, dict):
                response_text = ai_response.get("text", "Bez odpovede.")
                in_tokens = ai_response.get("in_tokens", 0)
                out_tokens = ai_response.get("out_tokens", 0)
                cached_tokens = ai_response.get("cached_tokens", 0)
            else:
                response_text = getattr(ai_response, 'text', str(ai_response))
                in_tokens = getattr(ai_response, 'in_tokens', 0)
                out_tokens = getattr(ai_response, 'out_tokens', 0)
                cached_tokens = getattr(ai_response, 'cached_tokens', 0)

            # 💰 4. AKTUALIZÁCIA TOKENOV A NÁKLADOV (so zohľadnením 90% zľavy za Cache)
            audit_log.vstupne_tokeny = in_tokens
            audit_log.vystupne_tokeny = out_tokens
            audit_log.presna_cena_usd = vypocitaj_ai_cenu_usd(in_tokens, out_tokens, cached_tokens=cached_tokens)
            audit_log.odpoved = response_text
            audit_log.uspesne_dokoncene = True
            await sync_to_async(audit_log.save)()
            if cache_limit_key:
                try:
                    cache.set(cache_limit_key, pouzite_dnes + 1, timeout=86400)
                except Exception:
                    pass

            # 🧠 5. ULOŽENIE HISTÓRIE PRE VIACKROKOVÝ DIALÓG (Multi-turn Memory)
            nova_historia = history_text + f"\n{aktualny_pouzivatel}: {otazka_text}\nAI: {response_text}"
            if message.author.id not in USER_STATES or not isinstance(USER_STATES[message.author.id], dict):
                USER_STATES[message.author.id] = {}
            USER_STATES[message.author.id]['history'] = nova_historia[-3000:]
            save_user_states()
            
            # Bezpečné rozdelenie pre Discord 2000-znakový limit
            if len(response_text) <= 1950:
                await status_msg.edit(content=response_text, view=discord.ui.View().add_item(BtnSpatMenu(typ or "ZAMESTNANEC")))
            else:
                chunks = [response_text[i:i+1900] for i in range(0, len(response_text), 1900)]
                await status_msg.edit(content=chunks[0])
                for chunk in chunks[1:-1]:
                    await message.channel.send(content=chunk)
                await message.channel.send(content=chunks[-1], view=discord.ui.View().add_item(BtnSpatMenu(typ or "ZAMESTNANEC")))
                
        except Exception as e:
            logging.error(f"Chyba pri AI chate na Discorde: {e}", exc_info=True)
            detail_txt = f"\n`{str(e)[:300]}`" if typ == "ZAMESTNANEC" else ""
            await status_msg.edit(
                content=f"❌ **Chyba AI asistenta:**{detail_txt}",
                view=discord.ui.View().add_item(BtnSpatMenu(typ or "ZAMESTNANEC"))
            )
        finally:
            cache.delete(lock_key)
            if audio_temp_path and os.path.exists(audio_temp_path):
                try: os.remove(audio_temp_path)
                except Exception: pass
            for fp in files_temp_paths:
                if fp and os.path.exists(fp):
                    try: os.remove(fp)
                    except Exception: pass

    await bot.process_commands(message)

async def spracuj_servis_hlasovka_discord(message, is_text_only=False):
    data = USER_STATES.pop(message.author.id, None)
    save_user_states()
    
    if not data or not isinstance(data, dict) or not data.get('zakaznik_id') or not data.get('zariadenie_id'):
        await message.reply(
            "⚠️ **Relácia vypršala!**\nSystém bol medzičasom reštartovaný alebo aktualizovaný. Začni prosím proces nahlásenia servisu znova z hlavného menu.", 
            view=discord.ui.View().add_item(BtnSpatMenu("ZAMESTNANEC"))
        )
        return

    if is_text_only:
        status_msg = await message.reply("🔄 *Spracovávam importované poznámky...*")
        audio_path = None
    else:
        status_msg = await message.reply("🎧 *Analyzujem hlasový záznam...*")
        attachment = message.attachments[0]
        ext = os.path.splitext(attachment.filename)[1].lower() or '.ogg'
        audio_path = f"servis_dc_{message.author.id}_{uuid.uuid4().hex[:8]}{ext}"
        await attachment.save(audio_path)
        
    try:
        raw_cart = data.get('sh_cart') if isinstance(data, dict) else None
        cart = raw_cart if isinstance(raw_cart, dict) else {}
        
        material_desc = get_industry_term('prompt_material_desc', 'Zoznam použitého materiálu, dielov alebo položiek vrátane množstva.')
        cart_items = [str(item.get('nazov', '')).replace('"', '') for item in cart.values() if isinstance(item, dict) and item.get('nazov')]
        if cart_items:
            vyklikane = ", ".join(cart_items)
            instrukcia_material = (
                f"{material_desc} POZOR: Technik už v systéme vyklikal tieto položky: [{vyklikane}]. "
                "Tieto vyklikané položky IGNORUJ a nezapisuj ich sem! Do tohto poľa zapíš IBA iný, dodatočný materiál či komponenty, ktoré technik spomenie. "
                "Ak nespomenie nič iné, vráť null."
            )
        else:
            instrukcia_material = f"{material_desc} Ak nič nemenil ani nepoužil, vráť null."

        namerane_desc = get_industry_term('prompt_namerane_hodnoty', 'Namerané prevádzkové veličiny a parametre spomenuté po servise. Sformátuj ich prehľadne. Ak nespomenul, vráť null.')
        hw_change_desc = get_industry_term('prompt_zmena_hardveru', 'true/false (Boolean. Hodnota true IBA v prípade, ak technik menil hlavný hardvér alebo kľúčové komponenty zariadenia. Bežná údržba je false.)')

        ai_prompt = (
            "Pôsobíš ako elitný technický dispečer a dátový analytik systému ISAS CORE. Tvojou úlohou je analyzovať textové/hlasové podklady od servisného technika a extrahovať z nich štruktúrované dáta pre databázu.\n\n"
            "STRIKTNÉ PRAVIDLÁ:\n"
            "1. Ak údaj v správe chýba, vráť striktne JSON hodnotu null (bez úvodzoviek). Pri poli zmena_hardveru vráť false, ak k výmene nedošlo.\n"
            "2. Vráť VÝHRADNE čistý, validný JSON formát bez markdown značiek (bez ```json).\n\n"
            "POPIS JEDNOTLIVÝCH POLÍ:\n"
            f"- motohodiny: {get_industry_term('meter_prompt_desc', 'celkový stav počítadla prevádzky (iba čisté číslo, napr. 15400)')}. Žiadne medzery ani písmená. Ak technik povie číslo slovom (napr. '15 tisíc 400'), preveď to na čisté číslo 15400. Ak nespomenie, vráť null.\n"
            f"- motohodiny_v_zatazi: {get_industry_term('meter_load_prompt_desc', 'prevádzkový stav v záťaži/špičke (iba čisté číslo, napr. 8000)')}. Žiadne medzery ani písmená. Ak nespomenie, vráť null.\n"
            "- poruchy_a_poznamky: Detailný, profesionálne preštylizovaný popis zistenej poruchy a vykonanej práce. Oprav gramatiku, odstráň slang, ale zachovaj presný technický význam a fakty.\n"
            f"- pouzity_material: {instrukcia_material}\n"
            f"- namerane_hodnoty: {namerane_desc}\n"
            f"- zmena_hardveru: {hw_change_desc}\n\n"
            "POŽADOVANÁ ŠTRUKTÚRA JSON (Vráť presne tento formát s doplnenými dátami):\n"
            "{\n"
            "  \"motohodiny\": null,\n"
            "  \"motohodiny_v_zatazi\": null,\n"
            "  \"poruchy_a_poznamky\": null,\n"
            "  \"pouzity_material\": null,\n"
            "  \"namerane_hodnoty\": null,\n"
            "  \"zmena_hardveru\": false\n"
            "}"
        )
        
        if data.get('use_draft') and data.get('sh_draft'):
            ai_prompt += f"\n\nPOZNÁMKY Z DIAGNOSTIKY (Zahrň ich do analýzy ako primárny zdroj faktov):\n{data['sh_draft']}"
            
        await status_msg.edit(content="🧠 *Umelá inteligencia ukladá dáta...*")
        
        try:
            raw_ai = await sync_to_async(ask_isas_ai)(user_question=ai_prompt, audio_path=audio_path, force_json=True)
            
            # 🛡️ 100% BEZPEČNÁ EXTRAKCIA: Získanie čistého JSON textu z návratového slovníka ask_isas_ai
            if isinstance(raw_ai, dict):
                raw_ai_text = raw_ai.get("text", "")
            else:
                raw_ai_text = getattr(raw_ai, "text", str(raw_ai))
            
            try:
                ai_data = clean_and_parse_ai_json(raw_ai_text.replace('<br>', '\n'))
            except Exception as json_err:
                logging.error(f"Zlyhalo dekódovanie AI JSON výstupu na Discorde: {json_err}. Raw: {str(raw_ai)[:300]}")
                raise ValueError(f"AI nevrátila platný formát JSON. Skutočná odpoveď: {str(raw_ai)[:300]}")
            
            cart = data.get('sh_cart', {})
            f_pred = data.get('sh_foto_pred')
            f_po = data.get('sh_foto_po')
            v_vid = data.get('sh_video')
            
            success, sprava, zaznam_data = await vytvor_zaznam_cez_ai_discord(
                message.author.id, ai_data, data['zakaznik_id'], data['zariadenie_id'], data.get('typ_id'), 
                cart_data=cart, foto_pred_path=f_pred, foto_po_path=f_po, video_path=v_vid
            )
            
        finally:
            # Garantované zmazanie bez ohľadu na to, či padla AI, DB alebo JSON
            audio_p = audio_path if 'audio_path' in locals() else None
            f_pr = data.get('sh_foto_pred') if (data and isinstance(data, dict)) else None
            f_po = data.get('sh_foto_po') if (data and isinstance(data, dict)) else None
            v_vi = data.get('sh_video') if (data and isinstance(data, dict)) else None
            
            for p in [audio_p, f_pr, f_po, v_vi]:
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass
                    
        if success:
            technik_id = data.get('technik_db_id')
            if technik_id and data.get('use_draft'):
                cache.delete(f"isas_quicknote_draft_{technik_id}")

            gn = await sync_to_async(GlobalneNastavenia.objects.first)()
            base_url = gn.clean_base_url if gn else getattr(settings, 'SITE_URL', '')
            link_na_podpis = f"{base_url}/evidencia/protokol/{zaznam_data['bezpecnostny_kluc']}/"
            
            zakaznik_text = zaznam_data.get('zakaznik_nazov', "Nezistený")
            zariadenie_text = zaznam_data.get('zariadenie_nazov', "Nezistené")
            praca_text = zaznam_data.get('typ_servisu_nazov', "Nezistený")
            
            dev_title = get_industry_term('zariadenie_fallback_nazov', 'Zariadenie')
            m_unit = get_industry_term('meter_unit', 'hod.')
            m_load_unit = get_industry_term('meter_load_unit', 'hod.')
            m_label = get_industry_term('meter_label', 'Prevádzkový čas')
            m_load_label = get_industry_term('meter_load_label', 'Záťaž / Špička')

            mh_raw = ai_data.get('motohodiny') if isinstance(ai_data, dict) else None
            is_mh_valid = mh_raw is not None and str(mh_raw).strip().lower() not in ('null', 'none', '')
            mh_celkove = f"{mh_raw} {m_unit}" if is_mh_valid else "Nezadané"

            mh_zataz_raw = ai_data.get('motohodiny_v_zatazi') if isinstance(ai_data, dict) else None
            is_zataz_valid = mh_zataz_raw is not None and str(mh_zataz_raw).strip().lower() not in ('null', 'none', '')
            mh_zataz_text = f" ({m_load_label}: {mh_zataz_raw} {m_load_unit})" if is_zataz_valid else ""

            mh_text = f"{mh_celkove}{mh_zataz_text}"

            text = (
                f"✅ **SERVIS BOL VYTVORENÝ!**\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🏢 **Zákazník:** {zakaznik_text}\n"
                f"⚙️ **{dev_title}:** {zariadenie_text}\n"
                f"⏱ **{m_label}:** {mh_text}\n"
                f"🛠 **Práca:** {praca_text}\n"
                f"📝 **SERVISNÝ PROTOKOL:**\n"
                f"Klikni na odkaz, skontroluj AI text a daj to podpísať zákazníkovi:\n"
                f"👉 [Otvoriť elektronický protokol]({link_na_podpis})"
            )

            await status_msg.edit(content=text, view=None)

            # --- ISAS SMART MATCH LOGIKA ---
            ulohy_list, match_type = await skontroluj_suvisiace_ulohy(zaznam_data['zakaznik_id'], zaznam_data['zariadenie_id'])
            view = discord.ui.View(timeout=None)
            
            if match_type == "FULL_MATCH":
                uloha = ulohy_list[0]
                smart_text = (
                    f"🤖 **ISAS CORE - Automatická detekcia**\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"Servisný záznam bol uložený. Systém našiel otvorenú úlohu, ktorá s týmto záznamom súvisí. Mám ju rovno označiť ako dokončenú?\n\n"
                    f"📌 **Úloha:** {uloha['defstr']}\n"
                )
                if uloha.get('info'):
                    smart_text += f"ℹ️ **Detail zadania:** *{uloha['info']}*\n"
                    
                view.add_item(BtnUzavrietUlohu(uloha['id'], f"✅ Uzavrieť Úlohu #{uloha['id']}"))
                view.add_item(BtnSpatMenu("ZAMESTNANEC"))
                await message.channel.send(content=smart_text, view=view)
                
            elif match_type == "ANOMALY":
                pocet_uloh = len(ulohy_list)
                if 2 <= pocet_uloh <= 4:
                    text_najdene = f"{pocet_uloh} otvorené úlohy, ktoré s týmto záznamom súvisia"
                    text_eviduje = f"{pocet_uloh} otvorené úlohy"
                else:
                    text_najdene = f"{pocet_uloh} otvorených úloh, ktoré s týmto záznamom súvisia"
                    text_eviduje = f"{pocet_uloh} otvorených úloh"
                
                smart_text = (
                    f"🤖 **ISAS CORE - Automatická detekcia**\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"Servisný záznam bol uložený. Systém našiel {text_najdene}.\n\n"
                    f"⚠️ **Systém eviduje {text_eviduje}! Ktorú si práve teraz zrealizoval?**\n"
                )
                
                zobrazene_ulohy = ulohy_list[:5]
                for u in zobrazene_ulohy:
                    ikona_ulohy = "💡" if u['is_dnes'] else "⚠️"
                    smart_text += f"\n{ikona_ulohy} **Úloha:** {u['defstr']}\n"
                    if u.get('info'):
                        smart_text += f"ℹ️ **Detail zadania:** *{u['info']}*\n"
                        
                    btn_text = f"✅ Uzavrieť #{u['id']} ({'Dnešná' if u['is_dnes'] else u['zaciatok_datum']})"[:80]
                    view.add_item(BtnUzavrietUlohu(u['id'], btn_text))
                    
                if len(ulohy_list) > 5:
                    smart_text += f"\n⚠️ *Zobrazených je len prvých 5 úloh z celkových {len(ulohy_list)}.*"
                else:
                    smart_text += "\n*(Ostatné môžeš zatiaľ nechať otvorené)*"

                view.add_item(BtnSpatMenu("ZAMESTNANEC"))
                if len(smart_text) > 1950:
                    smart_text = smart_text[:1950] + "...\n*(Skrátené kvôli limitu)*"
                await message.channel.send(content=smart_text, view=view)

            elif match_type and match_type.startswith("PARTIAL_MATCH_"):
                uloha = ulohy_list[0]
                parts = match_type.split("_")
                pocet_celkom = parse_int(parts[2], default=2) if len(parts) > 2 else 2
                pocet_zostava = max(1, pocet_celkom - 1)
                dev_label = get_industry_term('zariadenie_fallback_nazov', 'zariadenie').lower()
                dev_genitiv = get_industry_term('zariadenie_genitiv', 'zariadenia').lower()

                if pocet_zostava == 1:
                    slovo_jednotky = f"1 {dev_label}"
                else:
                    slovo_jednotky = f"{pocet_zostava} ({dev_genitiv})"
                
                smart_text = (
                    f"🤖 **ISAS CORE - Automatická detekcia**\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"Servisný záznam bol uložený. Systém našiel otvorenú úlohu, ktorá s týmto záznamom súvisí.\n\n"
                    f"⚠️ **Táto položka bola súčasťou väčšej hromadnej úlohy. Zostáva ti v nej vyriešiť ešte {slovo_jednotky}. Úloha preto zatiaľ zostáva otvorená.**\n\n"
                    f"📌 **Úloha:** {uloha['defstr']}\n"
                )
                if uloha.get('info'):
                    smart_text += f"ℹ️ **Detail zadania:** *{uloha['info']}*\n"
            
                view.add_item(BtnSpatMenu("ZAMESTNANEC"))
                if len(smart_text) > 1950:
                    smart_text = smart_text[:1950] + "...\n*(Skrátené kvôli limitu)*"
                await message.channel.send(content=smart_text, view=view)
            
            else:
                await status_msg.edit(view=discord.ui.View().add_item(BtnSpatMenu("ZAMESTNANEC")))
        else:
            await status_msg.edit(content=f"❌ **Chyba pri vytváraní záznamu:**\n{sprava}", view=discord.ui.View().add_item(BtnSpatMenu("ZAMESTNANEC")))
                
    except Exception as e:
        await status_msg.edit(content=f"❌ **Chyba pri spracovaní:**\n`{str(e)}`", view=discord.ui.View().add_item(BtnSpatMenu("ZAMESTNANEC")))

@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type == discord.InteractionType.component and interaction.data:
        raw_data: Any = interaction.data if isinstance(interaction.data, dict) else {}
        custom_id = str(raw_data.get('custom_id', '') or '')
        
        # --- TANKOVANIE: SPRACOVANIE PLATBY ---
        if custom_id.startswith('tpay_'):
            await interaction.response.defer()
            if interaction.user.id not in USER_STATES or not isinstance(USER_STATES[interaction.user.id], dict):
                USER_STATES[interaction.user.id] = {}
            if 'auto_id' not in USER_STATES[interaction.user.id]:
                await interaction.edit_original_response(
                    content="⚠️ **Relácia vypršala.** Začni prosím proces tankovania znova z hlavného menu.",
                    view=discord.ui.View().add_item(BtnSpatMenu("ZAMESTNANEC"))
                )
                return
            platba = custom_id.replace('tpay_', '')
            USER_STATES[interaction.user.id]['platba'] = platba
            save_user_states()
            await render_tankovanie_stanica(interaction)
            
        # --- JAZDA: SPRACOVANIE ŠTÁTU A DIÉT ---
        elif custom_id.startswith('jstat_'):
            await interaction.response.defer()
            if interaction.user.id not in USER_STATES or not isinstance(USER_STATES[interaction.user.id], dict):
                USER_STATES[interaction.user.id] = {}
            if 'auto_id' not in USER_STATES[interaction.user.id]:
                await interaction.edit_original_response(
                    content="⚠️ **Relácia vypršala.** Začni prosím zadávanie jazdy znova z hlavného menu.",
                    view=discord.ui.View().add_item(BtnSpatMenu("ZAMESTNANEC"))
                )
                return
            USER_STATES[interaction.user.id]['stat_ciela'] = custom_id.replace('jstat_', '')
            save_user_states()
            await render_jazda_diety(interaction)
            
        elif custom_id.startswith('jdiety_'):
            await interaction.response.defer()
            if interaction.user.id not in USER_STATES or not isinstance(USER_STATES[interaction.user.id], dict):
                USER_STATES[interaction.user.id] = {}
            if 'auto_id' not in USER_STATES[interaction.user.id]:
                await interaction.edit_original_response(
                    content="⚠️ **Relácia vypršala.** Začni prosím zadávanie jazdy znova z hlavného menu.",
                    view=discord.ui.View().add_item(BtnSpatMenu("ZAMESTNANEC"))
                )
                return
            volba = custom_id.replace('jdiety_', '')
            USER_STATES[interaction.user.id]['noclah'] = volba in ['NOCLAH', 'OBOJE']
            USER_STATES[interaction.user.id]['strava'] = volba in ['STRAVA', 'OBOJE']
            save_user_states()
            await dokonci_jazdu_discord(interaction)
            
        # --- HLAVNÉ MENU: ŠTART JAZDY A TANKOVANIA ---
        elif custom_id in ['menu_jazda_firemna', 'menu_jazda_sukromna', 'menu_tankovanie']:
            await interaction.response.defer()
            obj, typ = await identifikuj_pouzivatela_discord(interaction.user.id)
            user_type = typ or "ZAMESTNANEC"
            
            vozidla = await get_vozidla_list_discord()
            if not vozidla:
                await interaction.edit_original_response(
                    content="⚠️ **V systéme nie sú evidované žiadne aktívne vozidlá.**",
                    view=discord.ui.View().add_item(BtnSpatMenu(user_type))
                )
                return

            options = [discord.SelectOption(label=f"{v.spz} ({v.znacka_a_model})"[:100], value=str(v.id), emoji="🚗") for v in vozidla[:25]]
            
            if custom_id == 'menu_tankovanie':
                select_tank: discord.ui.Select[Any] = discord.ui.Select(placeholder="Ktoré auto si natankoval?", options=options)
                async def cb_tank(i: discord.Interaction):
                    await i.response.send_modal(TankovanieModal(int(select_tank.values[0])))
                setattr(select_tank, 'callback', cb_tank)
                text = "⛽ **TANKOVANIE**\n━━━━━━━━━━━━━━━━━━━━\nKtoré auto si natankoval?"
                view = discord.ui.View(timeout=None).add_item(select_tank).add_item(BtnSpatMenu(user_type))
            else:
                typ_jazdy = "FIREMNA" if "firemna" in custom_id else "SUKROMNA"
                select_jazda: discord.ui.Select[Any] = discord.ui.Select(placeholder="Ktoré auto si si zobral?", options=options)
                async def cb_jazda(i: discord.Interaction):
                    await i.response.defer()
                    await render_jazda_ciel(i, int(select_jazda.values[0]), typ_jazdy)
                setattr(select_jazda, 'callback', cb_jazda)
                view = discord.ui.View(timeout=None).add_item(select_jazda).add_item(BtnSpatMenu(user_type))

            await interaction.edit_original_response(content=text, view=view)

        elif custom_id == 'menu_ulohy':
            await interaction.response.defer()
            ulohy, meno_technika = await ziskaj_otvorene_ulohy_pre_technika_discord(interaction.user.id)
            if ulohy is None:
                await interaction.followup.send("⚠️ **Chyba autorizácie:** Tvoj profil nie je spárovaný.", ephemeral=True)
                return
            if len(ulohy) == 0:
                text = f"🎉 **KNIHA ÚLOH: ČISTÝ STÔL**\n━━━━━━━━━━━━━━━━━━━━\nAhoj {meno_technika}, aktuálne nemáš priradené žiadne otvorené úlohy.\nMôžeš si dať kávu! ☕"
                await interaction.edit_original_response(content=text, view=HlavneMenu("ZAMESTNANEC"))
                return
            
            header = f"📖 **TVOJA KNIHA ÚLOH**\n━━━━━━━━━━━━━━━━━━━━\nTechnik: **{meno_technika}**\nOtvorené úlohy: **{len(ulohy)}**\n\n"
            footer = "\n💡 *Pre uzavretie úlohy nahraj servisný záznam (Hlasovkou).*"
            
            dev_title = get_industry_term('zariadenie_fallback_nazov', 'Zariadenie')
            body_parts = []
            for u in ulohy:
                polozka = (
                    f"📌 **Úloha #{u['id']}** ⏳ {u['zaciatok']}\n"
                    f"🏢 **Zákazník:** {u['zakaznik']}\n"
                    f"🛠 **Zadanie:** {u['typy']}\n"
                    f"🔹 **{dev_title}:** {u.get('zariadenia', 'Neuvedené')}\n"
                    f"📝 **Info:** *{u['info']}*\n"
                    f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
                )
                body_parts.append(polozka)

            final_text = header
            for part in body_parts:
                if len(final_text) + len(part) + len(footer) > 1950:
                    final_text += "⚠️ *...ďalšie úlohy nájdeš vo webovom ISAS.*"
                    break
                final_text += part
            final_text += footer

            await interaction.edit_original_response(content=final_text, view=discord.ui.View().add_item(BtnSpatMenu("ZAMESTNANEC")))

        elif custom_id == 'menu_servis':
            await interaction.response.send_modal(ServisZakaznikSearchModal())

        elif custom_id == 'menu_sklad':
            await interaction.response.defer()
            await render_sklad_vyber(interaction)

        elif custom_id == 'menu_presun':
            await interaction.response.defer()
            await render_presun_zdroj(interaction)

        elif custom_id == 'menu_vstupy':
            await interaction.response.defer()
            channel = interaction.channel
            if not isinstance(channel, discord.abc.Messageable):
                return
            vstupy = await get_dnesne_vstupy_discord(interaction.user.id)
            if not vstupy:
                await interaction.edit_original_response(content="🚧 **KNIHA VSTUPOV**\n━━━━━━━━━━━━━━━━━━━━\nMáš čistý štít! Aktuálne nemáš evidované žiadne plánované vstupy.", view=discord.ui.View().add_item(BtnSpatMenu("ZAMESTNANEC")))
                return
            try:
                await interaction.delete_original_response()
            except Exception:
                pass
            vozidla = await get_vozidla_list_discord()
            
            # 🧹 Pripravíme zoznam na ukladanie ID správ (defenzívna inicializácia voči KeyError)
            if interaction.user.id not in USER_STATES or not isinstance(USER_STATES[interaction.user.id], dict):
                USER_STATES[interaction.user.id] = {}
            USER_STATES[interaction.user.id]['vstupy_msg_ids'] = []
            save_user_states()
            
            for v in vstupy:
                ikona = "✅" if v['stav'] == 'SCHVALENE' else "❌" if v['stav'] == 'ZAMIETNUTE' else "⏳"
                text = f"{ikona} **{v['fabrika']}** ({v['datum']})\n👨‍🔧 Zostava: {v['kolegovia']}\n🚗 Vozidlá: {v['auta']}\n📌 Status: *{v['stav_display']}*\n"
                if v['instrukcie']: text += f"\n⚠️ **Inštrukcie od firmy:**\n*{v['instrukcie']}*"
                # 🛡️ DEFENSIVE: Select pridáme LEN VTEDY, ak existujú vozidlá a vstup nie je schválený/zamietnutý (ochrana pred ValueError v discord.py)
                view_vstup = discord.ui.View(timeout=None).add_item(SelectAutoVstup(v['id'], vozidla)) if (v['stav'] not in ['SCHVALENE', 'ZAMIETNUTE'] and vozidla) else discord.utils.MISSING
                
                content_safe = text if len(text) <= 2000 else (text[:1990] + "...\n*")
                msg = None
                if v['pdf_cesta'] and os.path.exists(v['pdf_cesta']):
                    text_pdf = content_safe + "\n\n📄 **PRIEPUSTKA DO VRECKA:**\n*Predložte pri vstupe na vrátnici.*"
                    try:
                        msg = await channel.send(content=text_pdf[:2000], file=discord.File(v['pdf_cesta']), view=view_vstup)
                    except Exception as send_file_err:
                        logging.warning(f"⚠️ Nepodarilo sa odoslať PDF súbor cez Discord: {send_file_err}")
                
                # 🛡️ DEFENSIVE: Ak sa PDF nepodarilo poslať alebo neexistuje, pošleme spoľahlivo textovú kartu
                if not msg:
                    msg = await channel.send(content=content_safe, view=view_vstup)
                
                if msg:
                    USER_STATES[interaction.user.id]['vstupy_msg_ids'].append(msg.id)
                
            msg_back = await channel.send(content="🔽 **Navigácia:**", view=discord.ui.View().add_item(BtnSpatMenu("ZAMESTNANEC")))
            if msg_back:
                USER_STATES[interaction.user.id]['vstupy_msg_ids'].append(msg_back.id)
            save_user_states()

        elif custom_id == 'menu_faktura':
            # 🛡️ DEFENSIVE: Okamžitý defer zabráni pádu na 3s interaction timeout pri zápise na disk
            await interaction.response.defer()
            USER_STATES[interaction.user.id] = {'state': 'CAKAM_FAKTURU'}
            save_user_states()
            text = "📸 **NAHRÁVANIE FAKTÚRY**\n━━━━━━━━━━━━━━━━━━━━\n\nNahraj sem do chatu fotku alebo PDF faktúry.\n🤖 *AI modul ISAS Core sa postará o vyťaženie položiek.*"
            await interaction.edit_original_response(content=text, view=discord.ui.View().add_item(BtnSpatMenu("ZAMESTNANEC")))

        elif custom_id == 'menu_ai':
            await interaction.response.defer()
            USER_STATES[interaction.user.id] = {'state': 'AI_CHAT', 'history': ""}
            save_user_states()
            
            obj, typ = await identifikuj_pouzivatela_discord(interaction.user.id)
            
            if typ == "ZAMESTNANEC" and obj:
                django_user = getattr(obj, 'user', None)
                is_superuser = getattr(django_user, 'is_superuser', False)
                
                if is_superuser or obj.typ_pomeru == 'KO' or obj.pracovna_rola == 'AD':
                    text = (
                        "🧠 **ISAS CORE AI**\n━━━━━━━━━━━━━━━━━━━━\n"
                        "👋 **Ahoj šéfe!** Som tvoj elitný asistent pre riadenie firmy.\n\n"
                        "Vidím do všetkých modulov a som pripravený na hĺbkovú analýzu:\n"
                        "💰 **Financie:** Cash-flow, pohľadávky, faktúry, ERP.\n"
                        "🤝 **Obchod:** Ziskovosť, dodávatelia, ponuky, objednávky.\n"
                        f"⚙️ **Operatíva:** Úlohy, SLA reklamácie, {get_industry_term('zariadenia_mnozno', 'zariadenia')}, predikcie.\n"
                        "📦 **Sklad:** Stavy, pohyby, košík, výpočet trás.\n"
                        "👨‍🔧 **HR a Fleet:** Mzdy, dovolenky, tankovania, STK.\n"
                        "🧠 **Systém:** Čítanie PDF, schémy, maily, štatistiky.\n\n"
                        "*Čo dnes vyriešime? (Napíš text, pošli hlasovku alebo fotku)* 🚀"
                    )
                elif obj.pracovna_rola == 'DI':
                    text = (
                        "🧠 **ISAS CORE AI**\n━━━━━━━━━━━━━━━━━━━━\n"
                        "👋 **Ahoj!** Som tvoj asistent pre riadenie operatívy a dispečingu.\n\n"
                        "Mám prehľad o dianí vo firme:\n"
                        f"⚙️ **Operatíva:** Úlohy, SLA reklamácie, {get_industry_term('zariadenia_mnozno', 'zariadenia')}, vstupy.\n"
                        "📦 **Sklad:** Stavy skladu, pohyby, výpočet trás.\n"
                        "🚚 **Fleet:** Knihy jázd, tankovania, STK, PZP.\n"
                        "🧠 **Systém:** Čítanie PDF, schémy, maily.\n\n"
                        "*S čím ti môžem pomôcť? (Napíš text, pošli hlasovku alebo fotku)* 🚀"
                    )
                elif obj.pracovna_rola == 'OB':
                    text = (
                        "🧠 **ISAS CORE AI**\n━━━━━━━━━━━━━━━━━━━━\n"
                        "👋 **Ahoj!** Som tvoj obchodný asistent.\n\n"
                        "Pomôžem ti pri príprave obchodných prípadov:\n"
                        "🤝 **Obchod a CRM:** Zákazníci, cenové ponuky a stav objednávok.\n"
                        "📦 **Sklad:** Dostupnosť tovaru, predajné ceny a výpočet trás.\n"
                        f"⚙️ **Technika:** Parametre ({get_industry_term('zariadenie_genitiv', 'zariadenia')}) a katalógy.\n"
                        "🚚 **Fleet:** Moje jazdy a tankovania.\n\n"
                        "*S čím ti môžem pomôcť? (Napíš text, pošli hlasovku alebo fotku)* 🚀"
                    )
                else:
                    text = (
                        "🧠 **ISAS CORE AI**\n━━━━━━━━━━━━━━━━━━━━\n"
                        "👋 **Ahoj!** Som tvoj technický asistent.\n\n"
                        "Som tu, aby som ti uľahčil prácu:\n"
                        f"🛠️ **Moja práca:** Pracovné postupy (SOP), technické detaily ({get_industry_term('zariadenie_genitiv', 'zariadenia')}).\n"
                        "📦 **Sklad:** Dostupnosť dielov, výpočet trás.\n"
                        "🚚 **Fleet:** Moje jazdy a tankovania.\n"
                        "🧠 **Systém:** Čítanie PDF manuálov a schém.\n\n"
                        "*Čo pre teba môžem zistiť? (Napíš text, pošli hlasovku alebo fotku)* 🚀"
                    )
            elif typ == "SUKROMNA" or (obj and getattr(obj, 'typ_pomeru', '') == 'Súkromná osoba'):
                text = (
                    "🧠 **ISAS CORE AI**\n━━━━━━━━━━━━━━━━━━━━\n"
                    "👋 **Ahoj!**\n\n"
                    "Tvoj profil ťa oprávňuje výhradne na správu tvojho vozidla:\n"
                    "🚗 **Jazdy:** Evidencia súkromných a firemných jázd.\n"
                    "⛽ **Tankovanie:** Evidencia tankovania a nákladov.\n"
                    "📍 **Nastavenia:** Predvolená čerpacia stanica.\n\n"
                    "*Čo pre teba môžem zaznamenať? (Napíš text alebo pošli hlasovku)* 🚀"
                )
            else:
                text = (
                    "🧠 **ISAS CORE AI**\n━━━━━━━━━━━━━━━━━━━━\n"
                    "👋 **Ahoj!**\n\n"
                    "Tvoj používateľský účet nemá priradený profil zamestnanca alebo súkromnej osoby. Moje funkcie sú pre teba momentálne obmedzené.\n\n"
                    "*Ak potrebuješ plný prístup, kontaktuj administrátora.* 🚀"
                )

            await interaction.edit_original_response(content=text, view=discord.ui.View().add_item(BtnSpatMenu(typ if typ else "ZAMESTNANEC")))

        elif custom_id == 'menu_nastavenia':
            await interaction.response.defer()
            obj, typ = await identifikuj_pouzivatela_discord(interaction.user.id)
            if typ:
                await render_nastavenia(interaction, typ)

        # --- UZAMKNUTIE TERMINÁLU ---
        elif custom_id == 'menu_lock':
            await interaction.response.defer()
            
            try:
                await interaction.delete_original_response() # Zmažeme staré menu
            except Exception:
                pass
                
            # Zmažeme aj starú tapetu, ak tam nejaká visí
            channel = interaction.channel
            old_lock_id = USER_STATES.get(interaction.user.id, {}).get('lock_msg_id')
            if old_lock_id and isinstance(channel, discord.abc.Messageable):
                try:
                    old_msg = await channel.fetch_message(old_lock_id)
                    await old_msg.delete()
                except Exception:
                    pass

            USER_STATES[interaction.user.id] = {} # Vyčistíme pamäť

            caption_text = "🔒 **ISAS CORE TERMINÁL UZAMKNUTÝ**\n━━━━━━━━━━━━━━━━━━━━\nTvoja relácia bola bezpečne ukončená.\n\nPre opätovné prebudenie klikni na tlačidlo nižšie."
            lock_image_path = os.path.join(settings.BASE_DIR, 'Evidencia', 'static', 'img', 'isas_lockscreen.jpg')
            
            if isinstance(channel, discord.abc.Messageable):
                if os.path.exists(lock_image_path):
                    lock_msg = await channel.send(content=caption_text, file=discord.File(lock_image_path), view=BtnOdomknut())
                else:
                    lock_msg = await channel.send(content=caption_text, view=BtnOdomknut())
                    
                USER_STATES[interaction.user.id]['lock_msg_id'] = lock_msg.id # 💾 Uložíme ID tapety
                save_user_states()

        # --- ODOMKNUTIE TERMINÁLU ---
        elif custom_id == 'menu_unlock':
            await interaction.response.defer()
            try:
                if interaction.message:
                    await interaction.message.delete() # 🛡️ ISAS FIX: Natvrdo zmaže správu s tapetou aj po reštarte bota
            except Exception:
                pass
            
            # Poistka: vymažeme ID tapety z pamäte
            if interaction.user.id in USER_STATES:
                USER_STATES[interaction.user.id]['lock_msg_id'] = None
                save_user_states()
                
            obj, typ = await identifikuj_pouzivatela_discord(interaction.user.id)
            if obj:
                meno = getattr(obj, 'meno', getattr(obj, 'meno_a_priezvisko', 'Neznámy'))
                text = f"**ISAS CORE TERMINÁL** 🟢\n━━━━━━━━━━━━━━━━━━━━\nVitaj, **{meno}**.\nVyber požadovanú akciu z menu:"
                await send_clean_menu_discord(interaction.channel, interaction.user.id, text, HlavneMenu(typ))
            else:
                channel = interaction.channel
                if isinstance(channel, discord.abc.Messageable):
                    await channel.send("🔒 **Neautorizovaný prístup!**\nPre spárovanie napíš: `!login 0911222333`")

@bot.event
async def on_command_error(ctx, error):
    """Zachytáva chyby pri textových príkazoch (!start, !login)"""
    logging.error(f"Discord Command Error: {error}")
    try:
        await ctx.send(f"❌ **KRITICKÁ CHYBA SYSTÉMU**\n`{str(error)[:200]}`\nSkús to znova alebo kontaktuj dispečing.")
    except Exception: pass

async def global_interaction_error(interaction: discord.Interaction, error: Exception):
    """Zachytáva chyby pri klikaní na tlačidlá a menu (Interactions)"""
    logging.error(f"Discord Interaction Error: {error}")
    text = f"❌ **KRITICKÁ CHYBA SYSTÉMU**\n━━━━━━━━━━━━━━━━━━━━\nISAS Core narazil na problém.\n\n**Detail:** `{str(error)[:200]}`"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except Exception: pass

# Zaregistrovanie globálneho handlera pre tlačidlá
setattr(bot.tree, 'on_error', global_interaction_error)

if __name__ == '__main__':    
    max_retries = 6
    for pokus in range(max_retries):
        try:
            gn = GlobalneNastavenia.objects.first()
            if not gn or not gn.discord_bot_token:
                logging.critical("CRITICAL: Discord Bot Token nie je nastavený v Globálnych Nastaveniach! Skontrolujte Administráciu.")
                sys.exit(1)
            bot_token = gn.discord_bot_token
            break  # Úspešné pripojenie
        except Exception as e:
            logging.warning(f"Pripojenie k DB zlyhalo (Pokus {pokus+1}/{max_retries}). Čakám 5 sekúnd... ({e})")
            time.sleep(5)
    else:
        logging.critical("FATAL: Nepodarilo sa pripojiť k databáze ani po opakovaných pokusoch. Skript sa ukončuje.")
        sys.exit(1)

    # Spustenie bota s Tokenom z databázy (sanitizácia o biele znaky a nové riadky)
    bot.run(str(bot_token).strip())