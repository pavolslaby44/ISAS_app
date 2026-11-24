import datetime
from decimal import Decimal
import html
import logging
import os
import re
import time
import uuid
import warnings
from zoneinfo import ZoneInfo

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ISASaplikacia.settings')
import django
django.setup()

from asgiref.sync import sync_to_async
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    PicklePersistence,
    filters,
)
from telegram.warnings import PTBUserWarning

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.files import File
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from Evidencia.industry_loader import CATEGORY_CHOICES, CATEGORY_MAP, get_industry_term
from Evidencia.models import (
    AIAuditLog,
    Dodavatel,
    EvidenciaJazd,
    Filialka,
    GPSStopa,
    PouzityMaterialVZaznamoch,
    PovolenieNaVstup,
    PrichodziaFaktura,
    ServisnyZaznam,
    ServisovaneZariadenie,
    StockMovement,
    SukromnaOsoba,
    Tankovanie,
    TypServisuPrace,
    Uloha,
    Vozidlo,
    Zakaznik,
    Zamestnanec,
)
from Evidencia.services import ask_isas_ai
from Evidencia.utils import ISASNotificationBuilder, ISASNotifier, safe_tg
from Evidencia.utils_helper import (
    LedgerKalkulacka,
    clean_and_parse_ai_json,
    get_firemna_mena,
    ocisti_nazov_od_jednotky,
    parse_int,
    parse_decimal,
    parse_bool
)
from ISASaplikacia.models import FiremnaIdentita, GlobalneNastavenia
from ISASaplikacia.utils import vypocitaj_ai_cenu_usd
from ISASaplikacia.utils_helper import (
    is_float,
    vypocitaj_vzdialenost_metre,
)

# Umlčanie PTB varovaní pre CallbackQueryHandler
warnings.filterwarnings(action="ignore", message=r".*CallbackQueryHandler*", category=PTBUserWarning)

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

# O(1) REVERZNÝ INDEX: Vygenerovaný raz pri štarte pre bleskové vyhľadávanie modelov
MODEL_NAME_MAP = {
    model_class.__name__: (model_class, cat, subcat)
    for cat, subcats in CATEGORY_MAP.items()
    for subcat, model_class in subcats.items()
}

T_AUTO, T_KM, T_LITRE, T_CENA, T_PLATBA, T_STANICA, T_STANICA_SEARCH, T_FOTO = range(20, 28)
AI_CHAT = 30
SERVIS_HLAS = 40
SH_ZAKAZNIK_SEARCH, SH_ZAKAZNIK_SELECT, SH_ZARIADENIE_SELECT, SH_TYP_SELECT, SH_HLASOVKA = range(41, 46)
SH_MEDIA_MENU, SH_MEDIA_FOTO_PRED, SH_MEDIA_FOTO_PO, SH_MEDIA_VIDEO = range(46, 50)
SH_KOSIK, SH_SKLAD_WH, SH_SKLAD_CAT, SH_SKLAD_SUB, SH_SKLAD_ITEM, SH_SKLAD_COUNT = range(100, 106)
S_WH, S_MAIN, S_SUB, S_ITEM, S_SEARCH_WAIT = range(49, 54)
F_FOTO = 60 
J_AUTO, J_KM_START, J_KM_KONIEC, J_CIEL, J_CIEL_FILIALKA, J_CIEL_ZAKAZNIK_SEARCH, J_CIEL_ZAKAZNIK_SELECT, J_CIEL_TEXT, J_UCEL, J_SPOLUJAZDEC, J_STAT_CIELA, J_NOCLAH_STRAVA = range(70, 82)
P_ZDROJ, P_CIEL, P_CAT, P_SUB, P_ITEM, P_COUNT, P_SEARCH_WAIT = range(90, 97)

ITEMS_PER_PAGE = 10
CANCEL_MARKUP = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Hlavné menu (Zrušiť)", callback_data="cancel_action")]])
REPLY_KEYBOARD_ACTIVE = ReplyKeyboardMarkup(
    [["📋 HLAVNÉ MENU", "🔒 UZAMKNÚŤ TERMINÁL"]],
    resize_keyboard=True,
    is_persistent=True
)
REPLY_KEYBOARD_LOCKED = ReplyKeyboardMarkup(
    [["🔓 OTVORIŤ TERMINÁL"]],
    resize_keyboard=True,
    is_persistent=True
)

async def vymaz_docasne_spravy(context: ContextTypes.DEFAULT_TYPE, chat_id: int | None, keys: list[str] | None = None, direct_ids: list[int] | None = None):
    """
    🗑️ ISAS UNIFIED GC: Bezpečne zmaže správy z chatu a automaticky prečistí kľúče v user_data.
    Podporuje jednotlivé ID, zoznamy ID aj kľúče z pamäte relácie.
    """
    if not chat_id:
        return

    ids_to_delete = []

    # 1. Zber priamych ID
    if direct_ids:
        ids_to_delete.extend([mid for mid in direct_ids if mid])

    # 2. Zber ID podľa kľúčov v user_data + prečistenie pamäte
    if keys and context.user_data is not None:
        for key in keys:
            val = context.user_data.get(key)
            if isinstance(val, list):
                ids_to_delete.extend([mid for mid in val if mid])
                context.user_data[key] = []
            elif val:
                ids_to_delete.append(val)
                context.user_data[key] = None

    # 3. Zmazanie z Telegram chatu bez vyvolania výnimky
    for msg_id in ids_to_delete:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except TelegramError:
            pass

async def posli_docasnu_chybu(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None):
    """🗑️ ISAS UNIFIED GC: Bezpečne zmaže neplatný vstup a prepíše starú chybovú správu."""
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not chat_id:
        return

    # 1. Zmažeme chybnú správu od používateľa
    if update.message:
        try:
            await update.message.delete()
        except Exception:
            pass

    # 2. Zmažeme predchádzajúcu chybovú hlášku bota cez centrálny GC
    await vymaz_docasne_spravy(context, chat_id, keys=['err_msg'])

    # 3. Pošleme novú chybovú hlášku a zapamätáme si jej ID
    err = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup,
        parse_mode='HTML'
    )
    if context.user_data is not None:
        context.user_data['err_msg'] = err.message_id

@sync_to_async
def ziskaj_ai_user_kontext(tg_id):
    """ISAS AI: Jednotné načítanie používateľského profilu, oprávnení a denného limitu."""
    z = Zamestnanec.objects.select_related('user').filter(telegram_id=tg_id, je_anonymizovany=False).first()
    gn = GlobalneNastavenia.objects.first()

    if z:
        django_user = z.user
        is_superuser = django_user.is_superuser if django_user else False
        
        if is_superuser or z.typ_pomeru == 'KO':
            denny_limit = getattr(gn, 'ai_limit_superadmin', 500)
        elif z.pracovna_rola in ['DI', 'AD']:
            denny_limit = getattr(gn, 'ai_limit_management', 100)
        else:
            denny_limit = getattr(gn, 'ai_limit_technik', 25)

        return {
            'obj': z,
            'typ': "ZAMESTNANEC",
            'aktualny_pouzivatel': f"{z.meno} {z.priezvisko}",
            'rola_pouzivatela': z.get_pracovna_rola_display(),
            'typ_pomeru': z.get_typ_pomeru_display(),
            'is_superuser': is_superuser,
            'ma_pristup_k_financiam': is_superuser or z.typ_pomeru == 'KO' or z.pracovna_rola == 'AD',
            'denny_limit': denny_limit,
            'user_kwargs': {'pouzivatel': django_user}
        }

    s = SukromnaOsoba.objects.filter(telegram_id=tg_id, je_anonymizovany=False).first()
    if s:
        return {
            'obj': s,
            'subjekt_id': s.id,
            'typ': "SUKROMNA",
            'aktualny_pouzivatel': getattr(s, 'meno_a_priezvisko', 'Neznámy'),
            'rola_pouzivatela': "Zákazník",
            'typ_pomeru': "Súkromná osoba",
            'is_superuser': False,
            'ma_pristup_k_financiam': False,
            'denny_limit': getattr(gn, 'ai_limit_default', 10),
            'user_kwargs': {'sukromna_osoba': s}
        }

    return None

@sync_to_async
def trigger_sos_cache(tg_id, is_active):
    """🚨 ISAS SOS ENGINE: Aktivácia alebo deaktivácia núdzového stavu."""
    technik = Zamestnanec.objects.filter(telegram_id=tg_id).first()
    if technik:
        # Vytiahneme aktuálny záznam (aby sme nestratili poslednú GPS polohu)
        zaznam = cache.get(f'isas_radar_tech_{technik.id}') or {}
        zaznam['status'] = 'SOS' if is_active else 'ACTIVE'
        zaznam['timestamp'] = time.time()
        cache.set(f'isas_radar_tech_{technik.id}', zaznam, timeout=86400)
        return technik.meno
    return None

@sync_to_async
def vymaz_radar_cache(tg_id):
    """Vymaže cache polohy, aby sa bot znova opýtal na zdieľanie."""
    technik = Zamestnanec.objects.filter(telegram_id=tg_id).first()
    if technik:
        cache.delete(f'isas_radar_tech_{technik.id}')

@sync_to_async
def get_hlavny_sklad_adresa():
    """Zistí mesto hlavného sídla firmy z Identity."""
    identita = FiremnaIdentita.objects.first()
    mesto = (getattr(identita, 'sidlo_mesto', '') or 'Sídlo firmy') if identita else 'Sídlo firmy'
    return f"Hlavný sklad ({mesto})"

@sync_to_async
def get_domaci_stat_a_mena_pre_bot():
    """🌍 ISAS CORE: Dynamicky načíta domáci štát a menu firmy podľa profilu spoločnosti."""
    identita = FiremnaIdentita.objects.first()
    domaci_stat = getattr(identita, 'sidlo_stat', '') if identita else ''
    if not domaci_stat:
        defaults = FiremnaIdentita.get_country_defaults() if hasattr(FiremnaIdentita, 'get_country_defaults') else {}
        domaci_stat = defaults.get('stat_nazov', 'Domáci štát')
    mena = get_firemna_mena()
    return domaci_stat, mena

@sync_to_async
def get_zoznam_filialok_pre_bot(tg_id=None):
    """Vráti zoznam aktívnych filiálok. Ak je zadaný tg_id, filtruje len tie, kam má technik prístup."""
    qs = Filialka.objects.filter(je_aktivna=True)
    if tg_id:
        technik = Zamestnanec.objects.filter(telegram_id=tg_id).first()
        if technik:
            qs = qs.filter(priradeni_zamestnanci=technik)
        else:
            return []
    return list(qs.values('id', 'nazov'))

@sync_to_async
def vyhladaj_zakaznika_pre_bot(hladany_text):
    """Vyhľadá zákazníkov podľa názvu (Max 10)."""
    return list(Zakaznik.objects.filter(Nazov_spolocnosti__icontains=hladany_text)[:10].values('id', 'Nazov_spolocnosti'))

@sync_to_async
def get_zariadenia_pre_zakaznika_bot(zakaznik_id):
    zariadenia = list(ServisovaneZariadenie.objects.filter(zakaznik_id=zakaznik_id).values(
        'id', 'nazov', 'model_zariadenia', 'vyrobne_cislo', 'kategoria', 'zaruka_do', 'predlzena_zaruka_do'
    )[:25])
    kategorie_dict = dict(ServisovaneZariadenie.KATEGORIE)
    dnes = timezone.now().date()
    for z in zariadenia:
        z['kategoria_display'] = kategorie_dict.get(z['kategoria'], '⚙️ Zariadenie')
        if z.get('predlzena_zaruka_do') and z['predlzena_zaruka_do'] >= dnes:
            z['zaruka_badge'] = "💎 PREDĹŽENÁ"
        elif z.get('zaruka_do') and z['zaruka_do'] >= dnes:
            z['zaruka_badge'] = "🛡️ V ZÁRUKE"
        else:
            z['zaruka_badge'] = "⚪ PO ZÁRUKE"
    return zariadenia

@sync_to_async
def get_typy_servisu_bot():
    return list(TypServisuPrace.objects.all().values('id', 'nazov')[:25])

@sync_to_async
def get_predvolena_cerpacka(tg_id):
    zamestnanec = Zamestnanec.objects.filter(telegram_id=tg_id).first()
    if zamestnanec and zamestnanec.predvolena_cerpacka:
        return zamestnanec.predvolena_cerpacka.id, zamestnanec.predvolena_cerpacka.Nazov_spolocnosti
    osoba = SukromnaOsoba.objects.filter(telegram_id=tg_id).first()
    if osoba and osoba.predvolena_cerpacka:
        return osoba.predvolena_cerpacka.id, osoba.predvolena_cerpacka.Nazov_spolocnosti
    return None, None

@sync_to_async
def set_predvolena_cerpacka(tg_id, dodavatel_id):
    dodavatel = Dodavatel.objects.filter(id=dodavatel_id).first()
    if not dodavatel:
        return None
    zamestnanec = Zamestnanec.objects.filter(telegram_id=tg_id).first()
    if zamestnanec:
        zamestnanec.predvolena_cerpacka = dodavatel
        zamestnanec.save(update_fields=['predvolena_cerpacka'])
    osoba = SukromnaOsoba.objects.filter(telegram_id=tg_id).first()
    if osoba:
        osoba.predvolena_cerpacka = dodavatel
        osoba.save(update_fields=['predvolena_cerpacka'])
    return dodavatel.Nazov_spolocnosti

@sync_to_async
def vymaz_predvolenu_cerpacku(tg_id):
    zamestnanec = Zamestnanec.objects.filter(telegram_id=tg_id).first()
    if zamestnanec:
        zamestnanec.predvolena_cerpacka = None
        zamestnanec.save(update_fields=['predvolena_cerpacka'])
    osoba = SukromnaOsoba.objects.filter(telegram_id=tg_id).first()
    if osoba:
        osoba.predvolena_cerpacka = None
        osoba.save(update_fields=['predvolena_cerpacka'])

@sync_to_async
def vyhladaj_cerpacku(hladany_text):
    stanice = Dodavatel.objects.filter(Nazov_spolocnosti__icontains=hladany_text)[:10]
    return [{"id": s.id, "nazov": s.Nazov_spolocnosti} for s in stanice]

@sync_to_async
def get_zoznam_cerpaciek():
    """ISAS SMART QUERY: Vytiahne 10 najčastejšie používaných čerpacích staníc."""
    try:
        stanice = list(Dodavatel.objects.annotate(
            pocet_tankovani=Count('tankovanie_set')
        ).filter(pocet_tankovani__gt=0).order_by('-pocet_tankovani')[:10])
    except Exception as e:
        logging.warning(f"Zlyhala agregácia čerpacích staníc: {e}")
        stanice = []
        
    if not stanice:
        # Fallback pre prázdnu databázu alebo zlyhanie agregácie
        stanice = list(Dodavatel.objects.all()[:10])
        
    return [{"id": s.id, "nazov": s.Nazov_spolocnosti} for s in stanice]

@sync_to_async
def get_model_class_by_name(model_name):
    """Získa Django Model triedu a kategórie v O(1) čase podľa textového názvu."""
    return MODEL_NAME_MAP.get(model_name, (None, None, None))

@sync_to_async
def prepni_nastavenie_notifikacii(tg_id, platforma):
    """Prepne stav TG alebo MAIL notifikácií."""
    technik = Zamestnanec.objects.filter(telegram_id=tg_id).first()
    if technik:
        if platforma == 'tg':
            technik.tg_upozornenia_ulohy = not technik.tg_upozornenia_ulohy
            technik.save(update_fields=['tg_upozornenia_ulohy'])
        elif platforma == 'mail':
            technik.mail_upozornenia_ulohy = not technik.mail_upozornenia_ulohy
            technik.save(update_fields=['mail_upozornenia_ulohy'])
    return True

@sync_to_async
def get_stavy_notifikacii(tg_id):
    technik = Zamestnanec.objects.filter(telegram_id=tg_id).first()
    if technik:
        return technik.tg_upozornenia_ulohy, technik.mail_upozornenia_ulohy
    return None, None

@sync_to_async
def ziskaj_zajtrajsie_ulohy_pre_notifikacie():
    """Vytiahne dáta pre všetkých, ktorí chcú aspoň JEDEN typ pripomienky."""
    zajtra = timezone.now().date() + datetime.timedelta(days=1)
    
    # Berieme len tých, čo majú povolené buď TG alebo MAIL
    technici = Zamestnanec.objects.filter(
        Q(tg_upozornenia_ulohy=True) | Q(mail_upozornenia_ulohy=True)
    )
    
    data_na_odoslanie = []
    for t in technici:
        ulohy = Uloha.objects.filter(
            technici=t, 
            dokoncene=False, 
            zaciatok__date=zajtra
        ).select_related('zakaznik').prefetch_related('typ_servisu').order_by('zaciatok')
        
        if ulohy.exists():
            zoznam = []
            for u in ulohy:
                zakaznik = u.zakaznik.Nazov_spolocnosti if u.zakaznik else "Interná/Neznáma"
                praca = ", ".join([str(tp) for tp in u.typ_servisu.all()]) or "Všeobecná práca"
                # 🛡️ DEFENSIVE DUAL-MODE: Bezpečné ošetrenie pre USE_TZ=True aj USE_TZ=False
                if u.zaciatok:
                    c_cas = timezone.localtime(u.zaciatok) if (u.zaciatok and timezone.is_aware(u.zaciatok)) else u.zaciatok
                    cas_str = c_cas.strftime('%H:%M')
                else:
                    cas_str = "Nezadaný čas"

                zoznam.append({
                    'cas': cas_str,
                    'zakaznik': zakaznik,
                    'praca': praca
                })
                
            data_na_odoslanie.append({
                'tg_id': t.telegram_id,
                'email': t.email,
                'meno': t.meno,
                'chce_tg': bool(t.tg_upozornenia_ulohy and t.telegram_id),
                'chce_mail': bool(t.mail_upozornenia_ulohy and t.email),
                'ulohy': zoznam
            })
            
    return data_na_odoslanie

@sync_to_async
def odosli_mailovu_pripomienku_sync(email_prijimatela, meno_technika, ulohy_list):
    """Unifikované odoslanie štruktúrovaného e-mailu s pripomienkou denných úloh cez ISASNotifier."""
    builder = (
        ISASNotificationBuilder(
            nadpis="Pripomienka výjazdov na zajtra",
            ikona="🔔",
            farba_temy="#0d9488"
        )
        .pridaj_riadok("Technik:", safe_tg(meno_technika))
    )

    riadky_uloh = []
    for u in ulohy_list:
        riadky_uloh.append(
            f"⏰ <b>{safe_tg(u['cas'])}</b> | 🏢 {safe_tg(u['zakaznik'])}<br>"
            f"└ 🛠️ <i>{safe_tg(u['praca'])}</i>"
        )

    builder.nastav_hlavny_text("<br><br>".join(riadky_uloh))
    builder.nastav_ai_poznamku("Prajeme úspešný a bezpečný výjazd! ISAS CORE Automatik.")
    
    payload = builder.build()
    ISASNotifier.send(payload=payload, to_email=email_prijimatela, send_tg=False, send_mail=True)

@sync_to_async
def get_item_stav_pre_bota(model_class, pk, wh_id):
    """Získa inštanciu položky a jej skutočný fyzický/ledger stav na danom sklade."""
    if not model_class:
        return None, 0
    try:
        item = model_class.objects.get(pk=pk)
    except (model_class.DoesNotExist, Exception):
        return None, 0

    if wh_id != 0:
        filialka = Filialka.objects.filter(id=wh_id).first()
        if filialka:
            cisty = ocisti_nazov_od_jednotky(str(item))
            stav = LedgerKalkulacka.zisti_stav_polozky(filialka, cisty)
        else:
            stav = 0
    else:
        stav = item.pocet_na_sklade

    return item, stav

@sync_to_async
def ziskaj_otvorene_ulohy_pre_technika(tg_id):
    """ISAS CORE: Vytiahne živé úlohy pre konkrétneho zamestnanca."""
    technik = Zamestnanec.objects.filter(telegram_id=tg_id).first()
    if not technik: return None, "Neznámy technik"

    # Limit na prvých 10, aby sme nezahltili Telegram bota správou (anti-spam ochrana)
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
            'id': u.id,
            'zakaznik': zakaznik,
            'zaciatok': zaciatok,
            'typy': typy,
            'zariadenia': zariadenia_text,
            'info': info
        })
        
    return vysledok, technik.meno

@sync_to_async
def identifikuj_pouzivatela(tg_id):
    z = Zamestnanec.objects.filter(telegram_id=tg_id, je_anonymizovany=False).first()
    if z: return z, "ZAMESTNANEC"
    s = SukromnaOsoba.objects.filter(telegram_id=tg_id, je_anonymizovany=False).first()
    if s: return s, "SUKROMNA"
    return None, None

@sync_to_async
def find_and_update_pouzivatel(phone, tg_id):
    ciste_cislo = str(phone or '').strip()
    if len(ciste_cislo) < 7:
        return None, None

    kluc_vyhladania = ciste_cislo[-9:]

    with transaction.atomic():
        # Uvoľnenie prípadných predchádzajúcich väzieb na toto TG ID pred priradením
        Zamestnanec.objects.filter(telegram_id=tg_id).update(telegram_id=None)
        SukromnaOsoba.objects.filter(telegram_id=tg_id).update(telegram_id=None)

        z = Zamestnanec.objects.filter(je_anonymizovany=False, telefon__icontains=kluc_vyhladania).first()
        if not z:
            for candidate in Zamestnanec.objects.filter(je_anonymizovany=False, telefon__isnull=False).exclude(telefon=''):
                cand_clean = re.sub(r'[^0-9]', '', candidate.telefon or '')
                if cand_clean and (cand_clean.endswith(kluc_vyhladania) or kluc_vyhladania.endswith(cand_clean[-7:])):
                    z = candidate
                    break

        if z:
            z.telegram_id = tg_id
            z.save(update_fields=['telegram_id'])
            cele_meno = f"{z.meno or ''} {z.priezvisko or ''}".strip() or "Zamestnanec"
            return z, cele_meno
        
        s = SukromnaOsoba.objects.filter(je_anonymizovany=False, telefon__icontains=kluc_vyhladania).first()
        if not s:
            for candidate in SukromnaOsoba.objects.filter(je_anonymizovany=False, telefon__isnull=False).exclude(telefon=''):
                cand_clean = re.sub(r'[^0-9]', '', candidate.telefon or '')
                if cand_clean and (cand_clean.endswith(kluc_vyhladania) or kluc_vyhladania.endswith(cand_clean[-7:])):
                    s = candidate
                    break

        if s:
            s.telegram_id = tg_id
            s.save(update_fields=['telegram_id'])
            return s, s.meno_a_priezvisko
            
    return None, None

@sync_to_async
def get_item_instance(model_class, pk):
    try:
        return model_class.objects.get(pk=pk)
    except (ObjectDoesNotExist, Exception):
        return None

@sync_to_async
def get_vozidla_list():
    return list(Vozidlo.objects.all())

@sync_to_async
def get_dnesne_vstupy(tg_id):
    """Získa povolenky na vstup pre daného technika na dnešný deň (alebo budúce)."""
    dnes = timezone.now().date()
    technik = Zamestnanec.objects.filter(telegram_id=tg_id).first()
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

        vysledok.append({
            'id': v.id,
            'fabrika': v.zakaznik.Nazov_spolocnosti if v.zakaznik else "Neznámy zákazník",
            'datum': v.datum_vstupu.strftime('%d.%m.%Y') if v.datum_vstupu else "Nedatované",
            'stav': v.stav,
            'stav_display': v.get_stav_display(),
            'ai_text': v.ai_text_emailu,
            'kolegovia': ", ".join([f"{z.meno or ''} {z.priezvisko or ''}".strip() for z in v.zamestnanci.all() if z.meno or z.priezvisko]) or "Bez posádky",
            'auta': ", ".join([a.spz for a in v.vozidla.all() if getattr(a, 'spz', None)]) or "Bez vozidla",
            'instrukcie': v.zakaznik.instrukcie_vstupu if v.zakaznik else "",
            'pdf_cesta': pdf_cesta,
        })
    return vysledok

@sync_to_async
def ulozit_fakturu_z_telegramu(tg_user, file_bytes, filename):
    """Uloží faktúru priamo z Bota. Po zavolaní .save() sa automaticky naštartuje Gemini AI v models.py."""
    meno_autora = getattr(tg_user, 'meno', None) or getattr(tg_user, 'meno_a_priezvisko', 'Neznámy používateľ')
    faktura = PrichodziaFaktura(
        zdroj='TELEGRAM',
        poznamka=f"Nahraté cez TG (Meno: {meno_autora})"
    )
    faktura.subor.save(filename, ContentFile(file_bytes), save=True)
    return faktura.id

@sync_to_async
def ulozit_tankovanie_db(tg_id, auto_id, km_str, litre_str, cena_str, platba, stanica_id, foto_path):
    """
    ISAS SMART FLEET ENGINE: Záznam tankovania z Telegramu.
    """
    try:
        vozidlo = Vozidlo.objects.get(id=auto_id)
    except ObjectDoesNotExist:
        return False, "⚠️ <b>Chyba:</b> Zvolené vozidlo už v systéme neexistuje (bolo zrejme vymazané)."

    zamestnanec = Zamestnanec.objects.filter(telegram_id=tg_id).first()
    osoba = SukromnaOsoba.objects.filter(telegram_id=tg_id).first()

    meno_technika = zamestnanec.meno if zamestnanec else (osoba.meno_a_priezvisko if osoba else "Neznámy vodič")

    km_hodnota = parse_int(km_str, default=0)
    litre_decimal = parse_decimal(litre_str, default='0.00')
    cena_decimal = parse_decimal(cena_str, default='0.00')

    if litre_decimal <= Decimal('0.00') or cena_decimal <= Decimal('0.00'):
        return False, "⚠️ <b>Chyba:</b> Počet litrov aj celková suma musia byť väčšie ako nula."

    if not foto_path or not os.path.exists(foto_path):
        return False, "⚠️ <b>Chyba:</b> Fotografia pokladničného bločku sa nenašla. Zopakuj odoslanie dokladu."

    mena_firmy = get_firemna_mena()

    if platba == 'PALIVOVA':
        erp_zauctovanie = 'FAKTURA' # Zberná faktúra (CCS, Slovnaft, Dalioil)
    elif platba == 'PLATOBNA':
        erp_zauctovanie = 'FIREMNY_UCET' # Firemná banková karta
    else:
        erp_zauctovanie = 'SUKROMNE_PENIAZE' # Platil hotovosťou

    # ULOŽENIE DO DATABÁZY
    t = Tankovanie(
        vozidlo=vozidlo,
        vodič=zamestnanec,
        osoba=osoba,
        stav_tachometra=km_hodnota if km_hodnota > 0 else None,
        litre=litre_decimal,
        cena_celkom=cena_decimal,
        mena=mena_firmy,
        sposob_platby=platba,
        cerpacia_stanica_id=stanica_id,
        sposob_zauctovania_erp=erp_zauctovanie
    )
    
    with open(foto_path, 'rb') as f:
        t.foto_blocku.save(f"blocek_{tg_id}_{timezone.now().strftime('%Y%m%d%H%M')}.jpg", File(f), save=False)
            
    try:
        with transaction.atomic():
            t.full_clean()
            t.save()
    except ValidationError as e:
        if t.foto_blocku and t.foto_blocku.name:
            try:
                t.foto_blocku.storage.delete(t.foto_blocku.name)
            except Exception:
                pass
        err_msg = "\n".join([f"- {m}" for f, errs in e.message_dict.items() for m in errs]) if hasattr(e, 'message_dict') else str(e)
        return False, f"⚠️ <b>Účtovná chyba dokladu:</b>\n{err_msg}"
    except Exception as e:
        if t.foto_blocku and t.foto_blocku.name:
            try:
                t.foto_blocku.storage.delete(t.foto_blocku.name)
            except Exception:
                pass
        return False, f"⚠️ <b>Systémová chyba pri zápise dokladu:</b> {str(e)}"

    alert_pre_majitela = []
    upozornenie_pre_technika = []

    # 🛡️ 100% BULLETPROOF FLEET AUDITOR
    # 1. Kontrola objemu nádrže (Čisto typové porovnanie Decimal vs Decimal)
    max_litre = getattr(vozidlo, 'varovanie_max_litre', None)
    if max_litre and max_litre > 0:
        if litre_decimal > Decimal(str(max_litre)):
            alert_pre_majitela.append(
                f"🚨 <b>Podozrivé množstvo paliva:</b> Natankovaných až <b>{litre_decimal:.2f} L</b> "
                f"(limit vozidla: {max_litre} L)."
            )

    # 2. Kontrola sumy s ochranou voči krížovým menám (Cross-Currency Shield)
    max_suma = getattr(vozidlo, 'varovanie_max_suma', None)
    max_suma_dec = Decimal(str(max_suma)) if max_suma is not None else Decimal('0.00')
    if max_suma_dec > Decimal('0.00'):
        mena_firmy = get_firemna_mena()
        mena_dokladu = (getattr(t, 'mena', '') or mena_firmy).upper()

        # Porovnanie vykonáme LEN vtedy, ak bloček je v rovnakej mene ako nastavený limit vozidla
        if mena_dokladu == mena_firmy.upper():
            if cena_decimal > max_suma_dec:
                alert_pre_majitela.append(
                    f"🚨 <b>Podozrivá suma:</b> Tankovanie stálo <b>{cena_decimal:.2f} {mena_dokladu}</b> "
                    f"(limit vozidla: {max_suma_dec:.2f} {mena_firmy})."
                )

    aktualny_km_auta = vozidlo.aktualny_stav_km or 0
    
    if km_hodnota > 0 and km_hodnota < aktualny_km_auta:
        alert_pre_majitela.append(f"🚨 <b>Anomália tachometra:</b> Zadaný stav ({km_hodnota} km) je NIŽŠÍ ako posledný evidovaný ({aktualny_km_auta} km).")

    if km_hodnota > aktualny_km_auta:
        posledny_olej = vozidlo.posledny_olej_km or 0
        posledny_servis = vozidlo.posledny_servis_km or 0
        olej_interval = vozidlo.olej_interval_km or 15000
        servis_interval = vozidlo.servis_interval_km or 30000
        
        # Zistíme stav PRED uložením nových KM
        stare_km_do_oleja = olej_interval - (aktualny_km_auta - posledny_olej)
        stare_km_do_servisu = servis_interval - (aktualny_km_auta - posledny_servis)

        # Zistíme stav PO uložení nových KM (Model Tankovanie.save() sa postará o bezpečný DB update vozidla!)
        nove_km_do_oleja = olej_interval - (km_hodnota - posledny_olej)
        nove_km_do_servisu = servis_interval - (km_hodnota - posledny_servis)

        # Upozornenie pre technika do mobilu
        if nove_km_do_oleja <= 1000:
            upozornenie_pre_technika.append(f"🛢️ <b>Olej:</b> Zostáva len {nove_km_do_oleja} km!")
        if nove_km_do_servisu <= 1500:
            upozornenie_pre_technika.append(f"⚙️ <b>Veľký servis:</b> Zostáva len {nove_km_do_servisu} km!")

        # ANTI-SPAM PRE MAJITEĽA: Pošle mail/TG IBA v momente, keď to padne pod limit.
        if stare_km_do_oleja > 1000 and nove_km_do_oleja <= 1000:
            alert_pre_majitela.append(f"🛢️ <b>Nutná výmena oleja:</b> Vozidlu zostáva už len {nove_km_do_oleja} km.")
        if stare_km_do_servisu > 1500 and nove_km_do_servisu <= 1500:
            alert_pre_majitela.append(f"⚙️ <b>Veľký servis:</b> Vozidlu zostáva {nove_km_do_servisu} km.")

    dnes = timezone.now().date()
    limit_dni = datetime.timedelta(days=14)

    if vozidlo.stk_plati_do and (vozidlo.stk_plati_do - dnes) <= limit_dni:
        upozornenie_pre_technika.append(f"📑 <b>STK/EK:</b> Platí do {vozidlo.stk_plati_do.strftime('%d.%m.%Y')}!")
    if vozidlo.dialnicna_znamka_do and (vozidlo.dialnicna_znamka_do - dnes) <= limit_dni:
        upozornenie_pre_technika.append(f"🎫 <b>Známka:</b> Končí {vozidlo.dialnicna_znamka_do.strftime('%d.%m.%Y')}!")

    if alert_pre_majitela:
        gn = GlobalneNastavenia.objects.first()
        base_url = gn.clean_base_url if gn else getattr(settings, 'SITE_URL', '')
        admin_url = f"{base_url}/admin/Evidencia/vozidlo/{vozidlo.id}/change/"

        udalosti_text = "<br>".join([f"• {msg}" for msg in alert_pre_majitela])

        payload = (
            ISASNotificationBuilder(
                nadpis=f"FLEET ALERT ({safe_tg(vozidlo.spz)})",
                ikona="🚨",
                farba_temy="#dc2626"
            )
            .pridaj_riadok("Vozidlo:", f"{safe_tg(vozidlo.spz)} ({safe_tg(vozidlo.znacka_a_model)})")
            .pridaj_riadok("Vodič:", safe_tg(meno_technika))
            .nastav_hlavny_text(f"<b>Udalosti detegované pri tankovaní:</b><br>{udalosti_text}")
            .nastav_tlacidlo("Otvoriť kartu vozidla", admin_url)
            .nastav_ai_poznamku("ISAS AI Fleet Guardian: Detegovaná anomália alebo dosiahnutý servisný interval.")
            .build()
        )
        ISASNotifier.send(payload=payload, send_tg=True, send_mail=True, send_discord=True)

    # Finálne sformovanie správy pre technika do bota
    vystupne_upozornenie = ""
    if upozornenie_pre_technika:
        vystupne_upozornenie = "\n\n⚠️ <b>ISAS INFO PRE TEBA:</b>\n" + "\n".join(upozornenie_pre_technika)
        
    return True, vystupne_upozornenie

@sync_to_async
def ulozit_jazdu_db(tg_id, auto_id, typ_jazdy, km_start, km_koniec, ciel_text, ucel, zakaznik_id=None, spolujazdec_id=None, stat_ciela=None, noclah=False, strava=False):   
    try:
        # 1. BEZPEČNÁ IDENTIFIKÁCIA
        try:
            auto = Vozidlo.objects.get(id=auto_id)
        except ObjectDoesNotExist:
            return False, "⚠️ <b>Chyba:</b> Vozidlo už v systéme neexistuje."
            
        z = Zamestnanec.objects.filter(telegram_id=tg_id).first()
        s = SukromnaOsoba.objects.filter(telegram_id=tg_id).first()

        # 2. SANITIZÁCIA VSTUPOV (Fat-Finger ochrana proti preklepom)
        km_s = parse_int(km_start, default=None)
        km_k = parse_int(km_koniec, default=None)
        
        if km_s is None or km_k is None:
            return False, "⚠️ <b>Chyba:</b> Tachometer musí obsahovať platné číslo."

        if km_k <= km_s:
            return False, f"⚠️ <b>Logická chyba:</b> Konečný stav ({km_k} km) nemôže byť menší alebo rovný počiatočnému ({km_s} km)!"

        prejdene = km_k - km_s
        if prejdene > 1500:
            return False, f"⚠️ <b>Nereálny prejazd!</b> Zadal si trasu <b>{prejdene} km</b> na jeden záťah. Skontroluj, či si nenapísal nulu navyše."

        # 3. GAP DETECTOR A LOKALITA ŠTARTU
        identita = FiremnaIdentita.objects.first()
        defaults = FiremnaIdentita.get_country_defaults() if hasattr(FiremnaIdentita, 'get_country_defaults') else {}
        domaci_stat = (getattr(identita, 'sidlo_stat', '') if identita else '') or defaults.get('stat_nazov', '')

        posledna_jazda = EvidenciaJazd.objects.filter(vozidlo=auto, datum_cas_koniec__isnull=False).order_by('-datum_cas_koniec').first()
        if posledna_jazda and posledna_jazda.miesto_ciela:
            miesto_startu = posledna_jazda.miesto_ciela
            stat_startu = posledna_jazda.stat_ciela or domaci_stat
        else:
            mesto = getattr(identita, 'sidlo_mesto', '') if identita else ''
            miesto_startu = f"Sídlo firmy ({mesto})" if mesto else "Sídlo firmy"
            stat_startu = domaci_stat
        
        poznamka_k_jazde = ""
        aktualny_km_auta = auto.aktualny_stav_km or 0
        rozdiel_od_minulosti = km_s - aktualny_km_auta
        fleet_anomalia = None
        if rozdiel_od_minulosti > 20: 
            poznamka_k_jazde = f"⚠️ ALERT: Medzi poslednou evidenciou a touto jazdou chýba {rozdiel_od_minulosti} km!"
            fleet_anomalia = f"Medzi poslednou evidenciou a touto jazdou chýba nezaevidovaných <b>{rozdiel_od_minulosti} km</b>!"
        elif rozdiel_od_minulosti < -20:
            poznamka_k_jazde = f"⚠️ ALERT: Vodič zadal na štarte o {abs(rozdiel_od_minulosti)} km menej, ako mal byť posledný stav!"
            fleet_anomalia = f"Vodič zadal na štarte o <b>{abs(rozdiel_od_minulosti)} km menej</b> než posledný evidovaný stav!"

        zakaznik_obj = Zakaznik.objects.filter(id=zakaznik_id).first() if zakaznik_id else None

        # 4. AUTO-TASK MATCHER
        uloha_obj = None
        if zakaznik_obj and z:
            uloha_obj = Uloha.objects.filter(
                technici=z, zakaznik=zakaznik_obj, dokoncene=False, zaciatok__date=timezone.now().date()
            ).first()

        # 5. ANTI-COLLISION TIME ENGINE (Striktná ochrana pred budúcim časom aj DB overlapom)
        odhad_minut = int(prejdene * 1.2)
        if odhad_minut < 5: odhad_minut = 5
        if odhad_minut > 480: odhad_minut = 480
        teraz = timezone.now()
        cas_koniec = teraz
        
        if posledna_jazda and posledna_jazda.datum_cas_koniec:
            posledny_koniec = posledna_jazda.datum_cas_koniec
            if posledny_koniec:
                if timezone.is_aware(posledny_koniec) and not timezone.is_aware(teraz):
                    posledny_koniec = timezone.make_naive(posledny_koniec)
                elif not timezone.is_aware(posledny_koniec) and timezone.is_aware(teraz):
                    posledny_koniec = timezone.make_aware(posledny_koniec)

            if posledny_koniec > teraz:
                c_display = timezone.localtime(posledna_jazda.datum_cas_koniec) if (posledna_jazda.datum_cas_koniec and timezone.is_aware(posledna_jazda.datum_cas_koniec)) else posledna_jazda.datum_cas_koniec
                cas_str = c_display.strftime('%d.%m.%Y %H:%M') if c_display else "Neznámy čas"
                return False, f"⚠️ <b>Časová kolízia:</b> Predchádzajúca jazda končí v budúcnosti ({cas_str}). Skontroluj evidenciu jázd."

            # Štart novej jazdy nesmie nikdy kolidovať s koncom predchádzajúcej (bezpečnostný odstup 5 sekúnd)
            min_mozny_start = posledny_koniec + datetime.timedelta(seconds=5)
            cas_start = cas_koniec - datetime.timedelta(minutes=odhad_minut)

            # Ak by odhadovaný štart padol do predošlej jazdy, posunieme ho tesne za ňu
            if cas_start < min_mozny_start:
                cas_start = min_mozny_start

                # Ak minulá jazda skončila len pred chvíľou a koniec (teraz) by bol pred štartom
                if cas_koniec <= cas_start:
                    # Garantujeme platný časový interval bez generovania fiktívneho času v budúcnosti
                    cas_koniec = cas_start + datetime.timedelta(seconds=30)
        else:
            cas_start = cas_koniec - datetime.timedelta(minutes=odhad_minut)

        # 6. ATOMICKÝ ZÁPIS DO DATABÁZY
        stat_startu_final = str(stat_startu or '').strip() or domaci_stat
        stat_ciela_final = str(stat_ciela or '').strip() or stat_startu_final or domaci_stat

        with transaction.atomic():
            jazda = EvidenciaJazd(
                vozidlo=auto, typ_jazdy=typ_jazdy, zamestnanec=z, sukromna_osoba=s,
                miesto_startu=str(miesto_startu or 'Sídlo firmy')[:150], 
                miesto_ciela=str(ciel_text or 'Neznámy cieľ')[:150],
                zakaznik=zakaznik_obj, uloha=uloha_obj, 
                ucel_cesty=str(ucel or '')[:255],
                stav_km_start=km_s, stav_km_koniec=km_k,
                datum_cas_start=cas_start, datum_cas_koniec=cas_koniec,
                poznamka=poznamka_k_jazde,
                stat_startu=stat_startu_final[:50],
                stat_ciela=stat_ciela_final[:50],
                noclah_zabezpeceny=noclah,
                bezplatne_stravovanie=strava
            )
            jazda.full_clean()
            jazda.save()
            
            # PRIDANIE SPOLUJAZDCA
            if spolujazdec_id and z and spolujazdec_id != z.id:
                kolega = Zamestnanec.objects.filter(id=spolujazdec_id).first()
                if kolega:
                    jazda.spolujazdci.add(kolega)
        
        # 7. ZOSTAVENIE TEXTU PRE BOT-A
        sprava = f"📍 Trasa: <b>{miesto_startu} ➔ {ciel_text}</b>\nEvidovaných: <b>{prejdene} km</b>"
        if uloha_obj: sprava += f"\n🔗 <i>Spojené s Úlohou #{uloha_obj.id}</i>"
        if poznamka_k_jazde: sprava += f"\n\n{poznamka_k_jazde}"

        # 8. ODOSLANIE NOTIFIKÁCIE (Až po úspešnom commit-e transakcie)
        if fleet_anomalia:
            gn = GlobalneNastavenia.objects.first()
            base_url = gn.clean_base_url if gn else getattr(settings, 'SITE_URL', '')
            admin_url = f"{base_url}/admin/Evidencia/evidenciajazd/{jazda.id}/change/"
            meno_vodica = z.meno if z else (s.meno_a_priezvisko if s else "Neznámy vodič")
            
            payload = (
                ISASNotificationBuilder(
                    nadpis=f"FLEET ANOMÁLIA ({safe_tg(auto.spz)})",
                    ikona="🚨",
                    farba_temy="#dc2626"
                )
                .pridaj_riadok("Vozidlo:", f"{safe_tg(auto.spz)} ({safe_tg(auto.znacka_a_model)})")
                .pridaj_riadok("Vodič:", safe_tg(meno_vodica))
                .pridaj_riadok("Trasa:", f"{safe_tg(miesto_startu)} ➔ {safe_tg(ciel_text)}")
                .nastav_hlavny_text(f"<b>Detegovaná nezrovnalosť v tachometri:</b><br>• {fleet_anomalia}")
                .nastav_tlacidlo("Skontrolovať jazdu v ISAS", admin_url)
                .nastav_ai_poznamku("ISAS AI Fleet Auditor: Automatická detekcia prerušenia nadväznosti jázd.")
                .build()
            )
            ISASNotifier.send(payload=payload, send_tg=True, send_mail=True, send_discord=True)

        return True, sprava
    except ValidationError as e:
        return False, f"⚠️ <b>Účtovná chyba:</b>\n" + "\n".join([f"- {m}" for f, errs in e.message_dict.items() for m in errs])
    except Exception as e:
        return False, f"⚠️ <b>Systémová chyba:</b> {str(e)}"

async def zobraz_hlavne_menu(update_or_message, context, obj, typ):
    """Pomocná funkcia na vykreslenie samotného menu s auto-mazaním starého."""
    if typ == "ZAMESTNANEC":
        keyboard = [
            [InlineKeyboardButton("🚨 NÚDZOVÝ STAV (SOS)", callback_data="sos_trigger")],
            [InlineKeyboardButton("📖 MOJE ÚLOHY", callback_data="menu_moje_ulohy")],
            [InlineKeyboardButton("📦 SPRÁVA SKLADU", callback_data="sklad_main"),
            InlineKeyboardButton("🔄 PRESUN SKLAD", callback_data="presun_start")],
            [InlineKeyboardButton("🚧 MOJE VSTUPY", callback_data="menu_vstupy")],
            [InlineKeyboardButton("📸 NAHRAŤ FAKTÚRU DO SYSTÉMU", callback_data="menu_faktura")], 
            [InlineKeyboardButton("🛠️ NAHLÁSIŤ SERVIS (Hlasovka)", callback_data="menu_servis_hlas")],
            [InlineKeyboardButton("🚗 FIREMNÁ JAZDA", callback_data="menu_jazdy_firemna"), 
            InlineKeyboardButton("⛽ TANKOVANIE", callback_data="menu_tankovanie")],
            [InlineKeyboardButton("🧠 ISAS AI ASISTENT", callback_data="menu_ai_chat")],
            [InlineKeyboardButton("⚙️ NASTAVENIA", callback_data="menu_nastavenia")], 
            [InlineKeyboardButton("🔒 UZAMKNÚŤ TERMINÁL", callback_data="lock_terminal")]
        ]
        pozdrav = f"<b>ISAS CORE TERMINÁL</b> 🟢\n━━━━━━━━━━━━━━━━━━━━\nVitaj, <b>{html.escape(str(obj.meno or 'kolega'))}</b>. Prístup udelený.\nVyber požadovanú akciu z menu:"
    else:
        keyboard = [
            [InlineKeyboardButton("🚗 SÚKROMNÁ JAZDA", callback_data="menu_jazdy_sukromna"),
            InlineKeyboardButton("⛽ TANKOVANIE", callback_data="menu_tankovanie")],
            [InlineKeyboardButton("🧠 ISAS AI ASISTENT", callback_data="menu_ai_chat")],
            [InlineKeyboardButton("⚙️ NASTAVENIA", callback_data="menu_nastavenia")],
            [InlineKeyboardButton("🔒 UZAMKNÚŤ TERMINÁL", callback_data="lock_terminal")]
        ]
        pozdrav = f"<b>ISAS CORE TERMINÁL</b> 🟡\n━━━━━━━━━━━━━━━━━━━━\nVitaj, <b>{html.escape(str(obj.meno_a_priezvisko or 'používateľ'))}</b>.\nVyber požadovanú akciu z menu:"
        
    chat_id = update_or_message.chat_id if hasattr(update_or_message, 'chat_id') else update_or_message.message.chat_id

    # 1. Pokus o zmazanie starého menu (aby sa nezahlcoval chat)
    old_msg_id = context.user_data.get('last_menu_msg_id')
    if old_msg_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=old_msg_id)
        except Exception:
            pass # Správa už neexistuje alebo je príliš stará na zmazanie

    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=pozdrav,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )
    
    # 3. Zapamätáme si ID nového menu pre budúce zmazanie
    if context.user_data is not None:
        context.user_data['last_menu_msg_id'] = msg.message_id

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Zachránime ID posledného menu a zistíme, či visí tapeta
    last_menu_id = context.user_data.get('last_menu_msg_id') if context.user_data else None
    lock_msg_id = context.user_data.get('lock_msg_id') if context.user_data else None
    anchor_msg_id = context.user_data.get('anchor_msg_id') if context.user_data else None
    loc_prompt_id = context.user_data.get('loc_prompt_msg_id') if context.user_data else None
    
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not chat_id:
        return ConversationHandler.END

    # Garbage Collector - Zmažeme staré zámky a výzvy; kotvu premažeme len vtedy, ak sa vytvára nanovo
    keys_to_delete = ['lock_msg_id', 'loc_prompt_msg_id', 'err_msg', 'vstupy_msg_ids']
    if not update.callback_query or not anchor_msg_id:
        keys_to_delete.append('anchor_msg_id')
    await vymaz_docasne_spravy(context, chat_id, keys=keys_to_delete)

    anchor_msg = None
    if not update.callback_query or not anchor_msg_id:
        anchor_msg = await context.bot.send_message(
            chat_id=chat_id,
            text="✅ <i>Terminál odomknutý.</i>" if lock_msg_id else "🔄 <i>Načítavam ISAS Core...</i>",
            reply_markup=REPLY_KEYBOARD_ACTIVE,
            parse_mode='HTML'
        )

    if context.user_data: 
        # Zmazanie opustených médií, ak technik zrušil proces
        for key in ['sh_foto_pred', 'sh_foto_po', 'sh_video']:
            temp_path = context.user_data.get(key)
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError as e:
                    logging.warning(f"Nepodarilo sa zmazať dočasný súbor {temp_path}: {e}")
                
        context.user_data.clear() 
        
    current_anchor_id = anchor_msg.message_id if anchor_msg else anchor_msg_id
    if last_menu_id and context.user_data is not None:
        context.user_data['last_menu_msg_id'] = last_menu_id
    if current_anchor_id and context.user_data is not None:
        context.user_data['anchor_msg_id'] = current_anchor_id
        
    if not update.effective_user:
        return ConversationHandler.END
    user_id = update.effective_user.id
    obj, typ = await identifikuj_pouzivatela(user_id)
    
    if obj:
        if update.callback_query:
            await zobraz_hlavne_menu(update.callback_query, context, obj, typ)
        else:
            await zobraz_hlavne_menu(update.message, context, obj, typ)

        # 🧹 Okamžite zmažeme načítavaciu správu, aby nezostala visieť nad menu
        if anchor_msg:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=anchor_msg.message_id)
            except Exception:
                pass

        if context.user_data is not None:
            context.user_data['anchor_msg_id'] = None

        return ConversationHandler.END

    else:
        # 🧹 Garbage Collector pre cudzích ľudí cez centrálny GC
        await vymaz_docasne_spravy(context, chat_id, keys=['err_msg'])
            
        btn = [[KeyboardButton("📱 ZDIEĽAŤ KONTAKT", request_contact=True)]]
        markup = ReplyKeyboardMarkup(btn, resize_keyboard=True, one_time_keyboard=True)
        text_err = "🔒 <b>Neautorizovaný prístup!</b>\nPre vstup zdieľaj svoj kontakt."
        # 🛡️ DEFENSIVE: Bezpečné odoslanie bez ohľadu na to, či ide o správu alebo callback
        if update.message:
            err = await update.message.reply_text(text_err, reply_markup=markup, parse_mode='HTML')
        else:
            err = await context.bot.send_message(chat_id=chat_id, text=text_err, reply_markup=markup, parse_mode='HTML')
        if context.user_data is not None:
            context.user_data['err_msg'] = err.message_id

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        try:
            await update.callback_query.answer()
            # 🪄 MÁGIA: Odstráni inline klávesnicu zo správy, aby sa na ňu nedalo kliknúť znova
            await update.callback_query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
            
    await start(update, context)
    return ConversationHandler.END

@sync_to_async
def spracuj_a_uloz_gps(tg_id, lat, lon):
    """🛡️ ISAS INGESTION ENGINE: 3 vrstvy ochrany databázy pred GPS spamom."""

    # ŠKRTIACI VENTIL (Rate Limiting)
    cache_key_rate = f"gps_rate_limit_{tg_id}"
    if not cache.add(cache_key_rate, True, timeout=10):
        return False

    technik = Zamestnanec.objects.filter(telegram_id=tg_id).first()
    if not technik: return False

    # Zistíme, či technik nehlási núdzu
    existujuci_stav = cache.get(f'isas_radar_tech_{technik.id}')
    is_sos_active = existujuci_stav and existujuci_stav.get('status') == 'SOS'

    # Ignorujeme GPS, iba ak má vypnutú polohu a NIE JE v SOS
    if not getattr(technik, 'zdiela_polohu', True) and not is_sos_active:
        return False

    now = timezone.now()

    # 💾 Uloženie do Cache pre rýchly Live Radar
    existujuci = cache.get(f'isas_radar_tech_{technik.id}') or {}
    aktualny_status = existujuci.get('status', 'ACTIVE')

    cache.set(f'isas_radar_tech_{technik.id}', {
        'status': aktualny_status,
        'lat': lat,
        'lon': lon,
        'timestamp': now.timestamp()
    }, timeout=86400)

    # --- 🛡️ SPRACOVANIE PRE HISTÓRIU TRASY (GPSStopa) ---
    with transaction.atomic():
        posledny_bod = GPSStopa.objects.filter(zamestnanec=technik).order_by('-timestamp').first()

        if posledny_bod:
            vzdialenost_metre = vypocitaj_vzdialenost_metre(posledny_bod.lat, posledny_bod.lon, lat, lon)
            if vzdialenost_metre is None:
                return False  # Ochrana pred poškodenými GPS dátami
            pb_ts = posledny_bod.timestamp
            if pb_ts:
                if timezone.is_aware(pb_ts) and not timezone.is_aware(now):
                    pb_ts = timezone.make_naive(pb_ts)
                elif not timezone.is_aware(pb_ts) and timezone.is_aware(now):
                    pb_ts = timezone.make_aware(pb_ts)
            casovy_rozdiel_sekundy = (now - pb_ts).total_seconds() if pb_ts else 0

            # FYZIKÁLNY FILTER (Glitch Rejection) - Rýchlosť > 200 km/h (55 m/s)
            if casovy_rozdiel_sekundy > 0:
                rychlost_m_s = vzdialenost_metre / casovy_rozdiel_sekundy
                if rychlost_m_s > 55:
                    return False # Je to anomália (skok signálu), zahadzujeme
            elif vzdialenost_metre > 0:
                # Ak je časový rozdiel 0 sekúnd, ale vzdialenosť > 0, ide o teleportačný glitch. Zahadzujeme.
                return False 

            # INTELIGENTNÝ ZÁPIS (Smart Insert) - Pohyb < 20 metrov
            if vzdialenost_metre < 20:
                # Technik stojí na mieste. Redis už vie, že žije. 
                return True

        # Ak prešiel všetky štíty a reálne sa pohol, vytvoríme nový bod
        GPSStopa.objects.create(zamestnanec=technik, lat=lat, lon=lon, timestamp=now)
    return True

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Zachytáva Živú polohu (Live Location) z Telegramu."""
    message = update.message or update.edited_message
    if not message or not message.location or not update.effective_user or not update.effective_chat: return

    user_id = update.effective_user.id
    obj, typ = await identifikuj_pouzivatela(user_id)
    if not obj: return

    await spracuj_a_uloz_gps(user_id, message.location.latitude, message.location.longitude)

    # Ak je to prvé spustenie polohy (nie update na pozadí), poďakujeme a ukážeme menu
    if update.message:
        # 🧹 Garbage Collector: Zmažeme starú výzvu aj starú kotvu cez centrálny GC
        await vymaz_docasne_spravy(context, update.effective_chat.id, keys=['loc_prompt_msg_id', 'anchor_msg_id'])

        anchor_msg = await update.message.reply_text("✅ <i>Poloha aktivovaná. Dispečing ťa vidí.</i>", reply_markup=REPLY_KEYBOARD_ACTIVE, parse_mode='HTML')
        if context.user_data is not None:
            context.user_data['anchor_msg_id'] = anchor_msg.message_id
        await zobraz_hlavne_menu(update.message, context, obj, typ)

async def ukaz_moje_ulohy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query or not update.effective_user:
        return ConversationHandler.END
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("🔄 <i>Sťahujem dáta z knihy úloh ISAS Core...</i>", parse_mode='HTML')
    
    tg_id = update.effective_user.id
    ulohy, meno_technika = await ziskaj_otvorene_ulohy_pre_technika(tg_id)
    
    if ulohy is None:
        await update.callback_query.edit_message_text(
            "⚠️ <b>Chyba autorizácie:</b> Tvoj profil nie je spárovaný.", 
            reply_markup=CANCEL_MARKUP, parse_mode='HTML'
        )
        return ConversationHandler.END

    if len(ulohy) == 0:
        text = (
            f"🎉 <b>KNIHA ÚLOH: ČISTÝ STÔL</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Ahoj {meno_technika}, aktuálne nemáš priradené žiadne otvorené úlohy.\n"
            f"Môžeš si dať kávu! ☕"
        )
        await update.callback_query.edit_message_text(text, reply_markup=CANCEL_MARKUP, parse_mode='HTML')
        return ConversationHandler.END

    # Poskladáme luxusnú Telegram správu
    text = (
        f"📖 <b>TVOJA KNIHA ÚLOH</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Technik: <b>{meno_technika}</b>\n"
        f"Otvorené úlohy: <b>{len(ulohy)}</b> (zobrazené najnovšie)\n\n"
    )

    dev_title = get_industry_term('zariadenie_fallback_nazov', 'Zariadenie')
    for i, u in enumerate(ulohy, 1):
        text += (
            f"📌 <b>Úloha #{u['id']}</b> ⏳ {u['zaciatok']}\n"
            f"🏢 <b>Zákazník:</b> {u['zakaznik']}\n"
            f"🛠 <b>Zadanie:</b> {u['typy']}\n"
            f"🔹 <b>{dev_title}:</b> {u.get('zariadenia', 'Neuvedené')}\n"
            f"📝 <b>Info:</b> <i>{u['info']}</i>\n"
            f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
        )
        
    text += "\n💡 <i>Pre uzavretie úlohy môžeš nahrať servisný záznam (Hlasovkou).</i>"

    # Ochrana Telegram limitu (max 4096 znakov)
    if len(text) > 4000:
        text = text[:4000] + "...\n\n<i>[Zoznam bol skrátený, pre kompletný prehľad kontaktuj dispečing]</i>"

    await update.callback_query.edit_message_text(text, reply_markup=CANCEL_MARKUP, parse_mode='HTML')
    return ConversationHandler.END

@sync_to_async
def odosli_sos_notifikaciu_dispecingu(tg_id, is_active):
    """🚨 Okamžité odoslanie prioritného SOS alertu majiteľovi a dispečingu cez ISASNotifier."""
    technik = Zamestnanec.objects.filter(telegram_id=tg_id).first()
    if not technik:
        return
        
    cache_data = cache.get(f'isas_radar_tech_{technik.id}') or {}
    lat = cache_data.get('lat')
    lon = cache_data.get('lon')
    
    poloha_str = "Neznáma (GPS nie je dostupné)"
    maps_url = None
    if lat and lon:
        poloha_str = f"Lat: {lat:.6f}, Lon: {lon:.6f}"
        maps_url = f"https://www.google.com/maps?q={lat},{lon}"

    if is_active:
        builder = (
            ISASNotificationBuilder(
                nadpis="NÚDZOVÝ POPLACH (SOS)!",
                ikona="🚨",
                farba_temy="#dc2626"
            )
            .pridaj_riadok("Technik v núdzi:", f"<b>{safe_tg(technik.meno)} {safe_tg(technik.priezvisko)}</b>")
            .pridaj_riadok("Telefón:", f"<code>{technik.telefon or 'Neuvedený'}</code>")
            .pridaj_riadok("Posledná GPS poloha:", poloha_str)
            .nastav_hlavny_text(
                "⚠️ <b>POZOR: Technik stlačil núdzové tlačidlo SOS v teréne!</b><br>"
                "Okamžite technika telefonicky kontaktujte alebo overte situáciu na dispečerskom radare."
            )
        )
        if maps_url:
            builder.nastav_tlacidlo("📍 Zobraziť polohu na mape", maps_url)
        builder.nastav_ai_poznamku("ISAS SOS Dispatcher: Prioritný bezpečnostný alert najvyššej kategórie.")
    else:
        builder = (
            ISASNotificationBuilder(
                nadpis="SOS POPLACH BOL ODVOLANÝ",
                ikona="✅",
                farba_temy="#16a34a"
            )
            .pridaj_riadok("Technik:", f"{safe_tg(technik.meno)} {safe_tg(technik.priezvisko)}")
            .nastav_hlavny_text("Technik označil situáciu za vyriešenú a odvolal stav núdze (Som v poriadku).")
            .nastav_ai_poznamku("ISAS SOS Dispatcher: Incident uzavretý.")
        )

    payload = builder.build()
    ISASNotifier.send(payload=payload, send_tg=True, send_mail=True, send_discord=True)

async def sos_trigger_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Technik stlačil SOS."""
    if not update.callback_query or not update.effective_user:
        return
    await update.callback_query.answer()
    tg_id = update.effective_user.id
    meno = await trigger_sos_cache(tg_id, True)

    if meno:
        # 🚀 Okamžité reálne odoslanie alertu na Telegram aj E-mail majiteľa
        await odosli_sos_notifikaciu_dispecingu(tg_id, is_active=True)
        
        text = (
            "🚨 <b>SOS POPLACH ODOSLANÝ!</b> 🚨\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Dispečing a vedenie práve dostali prioritné upozornenie s tvojou polohou. Zostaň na mieste, pomoc je na ceste.\n\n"
            "<i>Ak išlo o omyl, okamžite zruš poplach tlačidlom nižšie!</i>"
        )
        keyboard = [[InlineKeyboardButton("✅ Som v poriadku (Zrušiť poplach)", callback_data="sos_cancel")]]
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


async def sos_cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Technik zrušil poplach."""
    if not update.callback_query or not update.effective_user:
        return
    await update.callback_query.answer()
    tg_id = update.effective_user.id
    await trigger_sos_cache(tg_id, False)
    
    # 🚀 Odoslanie informácie dispečingu, že poplach bol odvolaný
    await odosli_sos_notifikaciu_dispecingu(tg_id, is_active=False)
    
    obj, typ = await identifikuj_pouzivatela(tg_id)
    await update.callback_query.edit_message_text("✅ <b>Poplach bol zrušený.</b> Dispečing bol informovaný o tvojom bezpečí.", parse_mode='HTML')
    await zobraz_hlavne_menu(update.callback_query.message, context, obj, typ)

async def menu_nastavenia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Zobrazí menu nastavení a aktuálne stavy prepínačov (Dynamicky pre Zamestnanca aj Súkromnú osobu)."""
    if not update.callback_query or not update.effective_user:
        return ConversationHandler.END
    await update.callback_query.answer()
    tg_id = update.effective_user.id
    
    obj, typ_uzivatela = await identifikuj_pouzivatela(tg_id)
    
    if not obj:
        await update.callback_query.edit_message_text("⚠️ Nemáš priradený žiadny profil v systéme ISAS CORE.", reply_markup=CANCEL_MARKUP)
        return ConversationHandler.END

    stanica_id, stanica_nazov = await get_predvolena_cerpacka(tg_id)
    txt_stanica = stanica_nazov if stanica_nazov else "Nenastavená"
    
    keyboard = []

    # 1. ČASŤ: Ak je to Zamestnanec, pridáme mu možnosť nastavovať notifikácie o Úlohách
    if typ_uzivatela == "ZAMESTNANEC":
        stav_tg, stav_mail = await get_stavy_notifikacii(tg_id)
        txt_tg = "🟢 ZAPNUTÉ" if stav_tg else "🔴 VYPNUTÉ"
        txt_mail = "🟢 ZAPNUTÉ" if stav_mail else "🔴 VYPNUTÉ"
        
        btn_tg = "🔕 Vypnúť Telegram" if stav_tg else "🔔 Zapnúť Telegram"
        btn_mail = "🔕 Vypnúť E-mail" if stav_mail else "🔔 Zapnúť E-mail"

        # 100% spoľahlivé čítanie z databázy
        zdiela_polohu = getattr(obj, 'zdiela_polohu', True)
        txt_gps = "🟢 ZAPNUTÉ" if zdiela_polohu else "🔴 VYPNUTÉ"

        text = (
            f"⚙️ <b>NASTAVENIA PROFILU</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Upozornenia na výjazdy:</b>\n"
            f"✈️ <b>Telegram:</b> {txt_tg}\n"
            f"✉️ <b>E-mail:</b> {txt_mail}\n\n"
            f"<b>Dispečing a Radar:</b>\n"
            f"📍 <b>Zdieľanie polohy:</b> {txt_gps}\n\n"
            f"<b>Tankovanie do firmy:</b>"
        )
        keyboard.append([InlineKeyboardButton(btn_tg, callback_data="tog_tg"), InlineKeyboardButton(btn_mail, callback_data="tog_mail")])
        
        # Dynamické tlačidlá podľa stavu v DB
        if zdiela_polohu:
            keyboard.append([InlineKeyboardButton("🚫 Vypnúť zdieľanie polohy", callback_data="turn_off_location")])
        else:
            keyboard.append([InlineKeyboardButton("📍 Obnoviť zdieľanie polohy", callback_data="force_location")])
    
    # 2. ČASŤ: Ak je to Súkromná osoba, notifikácie neriešime
    else:
        text = (
            f"⚙️ <b>NASTAVENIA PROFILU</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Tankovanie:</b>"
        )

    # 3. SPOLOČNÁ ČASŤ: Tlačidlo na čerpaciu stanicu (Môžu klikať obaja)
    if stanica_id:
        btn_cs_text = f"❌ Vymazať predvolenú ČS ({txt_stanica})"
    else:
        btn_cs_text = "ℹ️ Ako nastaviť predvolenú ČS?"
        
    keyboard.append([InlineKeyboardButton(btn_cs_text, callback_data="zmen_cs")])
    keyboard.append([InlineKeyboardButton("🔙 Návrat do menu", callback_data="cancel_action")])
    
    try:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise e
            
    return ConversationHandler.END

async def reset_cerpacky(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler pre tlačidlo v nastaveniach."""
    if not update.callback_query or not update.effective_user:
        return ConversationHandler.END
    stanica_id, _ = await get_predvolena_cerpacka(update.effective_user.id)
    
    if not stanica_id:
        # Ak ju nemá, vysvetlíme mu, ako sa učí systém
        await update.callback_query.answer(
            "Stanicu zatiaľ nemáš nastavenú.\n\nNastaví sa automaticky, keď pôjdeš najbližšie tankovať a vyberieš si ju zo zoznamu.", 
            show_alert=True
        )
        return ConversationHandler.END
        
    # Ak ju má, vymažeme ju a povieme mu, čo sa stane
    await vymaz_predvolenu_cerpacku(update.effective_user.id)
    await update.callback_query.answer(
        "✅ Stanica vymazaná.\n\nPri najbližšom tankovaní sa ťa bot opýta na novú a tú si zapamätá.", 
        show_alert=True
    )
    return await menu_nastavenia(update, context)

async def toggle_tg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return ConversationHandler.END
    await prepni_nastavenie_notifikacii(update.effective_user.id, 'tg')
    return await menu_nastavenia(update, context)

async def toggle_mail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return ConversationHandler.END
    await prepni_nastavenie_notifikacii(update.effective_user.id, 'mail')
    return await menu_nastavenia(update, context)

async def force_location_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Zapne polohu v DB a vyžiada ju od technika cez fyzické tlačidlo."""
    if not update.callback_query or not update.effective_user or not update.effective_chat:
        return ConversationHandler.END
    await update.callback_query.answer()
    obj, typ = await identifikuj_pouzivatela(update.effective_user.id)
    if obj and hasattr(obj, 'zdiela_polohu'):
        obj.zdiela_polohu = True
        await sync_to_async(obj.save)(update_fields=['zdiela_polohu'])
    
    await vymaz_radar_cache(update.effective_user.id)
    
    keyboard = [
        [KeyboardButton("📍 Zdieľať polohu", request_location=True)],
        [KeyboardButton("🚫 Zrušiť")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
    
    chat_id = update.effective_chat.id
    # 🧹 Garbage Collector: Zmažeme starú výzvu aj starú kotvu cez centrálny GC
    await vymaz_docasne_spravy(context, chat_id, keys=['loc_prompt_msg_id', 'anchor_msg_id'])
    
    loc_msg = await context.bot.send_message(
        chat_id=chat_id,
        text="📍 <b>Zdieľanie polohy (Dispečing)</b>\n━━━━━━━━━━━━━━━━━━━━\nKlikni na tlačidlo nižšie pre aktiváciu radaru.",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )
    if context.user_data is not None:
        context.user_data['loc_prompt_msg_id'] = loc_msg.message_id
    return ConversationHandler.END

async def turn_off_location_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Natvrdo vypne sledovanie polohy v DB."""
    if not update.callback_query or not update.effective_user:
        return ConversationHandler.END
    await update.callback_query.answer("🔴 Sledovanie polohy bolo vypnuté.", show_alert=True)
    obj, typ = await identifikuj_pouzivatela(update.effective_user.id)
    if obj and hasattr(obj, 'zdiela_polohu'):
        obj.zdiela_polohu = False
        await sync_to_async(obj.save)(update_fields=['zdiela_polohu'])
        
    return await menu_nastavenia(update, context)

async def automaticka_pripomienka_uloh(context: ContextTypes.DEFAULT_TYPE):
    """Hlavný Cron Job pre TG aj Mail, bežiaci paralelne."""
    data = await ziskaj_zajtrajsie_ulohy_pre_notifikacie()
    
    for d in data:
        # Telegram správa (ponechaná v bleskovom priamom formáte pre Telegram)
        if d['chce_tg']:
            meno_pre_tg = (d['meno'] or 'kolega').upper()
            text_tg = (
                f"🔔 <b>AHOJ {safe_tg(meno_pre_tg)}, PRIPOMIENKA NA ZAJTRA!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Systém ISAS CORE eviduje, že zajtra máš naplánované tieto úlohy:\n\n"
            )
            for u in d['ulohy']:
                text_tg += f"⏰ <b>{safe_tg(u['cas'])}</b> | 🏢 {safe_tg(u['zakaznik'])}\n└ 🛠 <i>{safe_tg(u['praca'])}</i>\n\n"
            text_tg += "<i>Prajeme úspešný a bezpečný výjazd! 🍀</i>\n<i>(Pripomienky si môžeš upraviť v ⚙️ Nastaveniach)</i>"
            
            try:
                await context.bot.send_message(chat_id=d['tg_id'], text=text_tg, parse_mode='HTML')
            except Exception as e:
                logging.error(f"TG pripomienka zlyhala pre {d['tg_id']}: {e}")
                
        # E-mailová notifikácia cez ISASNotifier s plnohodnotným HTML dizajnom
        if d['chce_mail']:
            await odosli_mailovu_pripomienku_sync(d['email'], d['meno'], d['ulohy'])

async def zacat_fakturu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return ConversationHandler.END
    await update.callback_query.answer()
    text = (
        "📸 <b>NAHRÁVANIE FAKTÚRY</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Polož faktúru na stôl a <b>odfoť ju</b> (ako štandardnú fotku z galérie bota), alebo mi sem jednoducho <b>prepošli PDF súbor</b>.\n\n"
        "🤖 <i>AI modul ISAS Core sa postará o vyťaženie položiek a zaradenie do skladu.</i>"
    )
    await update.callback_query.edit_message_text(text, reply_markup=CANCEL_MARKUP, parse_mode='HTML')
    return F_FOTO

async def spracuj_fakturu_subor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user or not update.effective_chat:
        return ConversationHandler.END

    # 1. VALIDÁCIA VSTUPU (Garbage Collector pre zlé súbory)
    if not update.message.photo and not update.message.document:
        await posli_docasnu_chybu(update, context, "⚠️ <b>Chyba:</b> Pošli mi fotku alebo dokument (PDF).", reply_markup=CANCEL_MARKUP)
        return F_FOTO

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    # Ak je vstup správny, upraceme prípadnú starú chybovú hlášku cez centrálny GC
    await vymaz_docasne_spravy(context, chat_id, keys=['err_msg'])

    status_msg = await update.message.reply_text("🔄 <i>Prijímam dokument a analyzujem cez Gemini AI...</i>", parse_mode='HTML')
    
    try:
        user, _ = await identifikuj_pouzivatela(user_id)
        
        if update.message.photo:
            file_obj = await update.message.photo[-1].get_file()
            filename = f"faktura_tg_{user_id}_{uuid.uuid4().hex[:8]}.jpg"
        elif update.message.document:
            file_obj = await update.message.document.get_file()
            
            bezpecny_nazov = update.message.document.file_name or "faktura_bez_nazvu.pdf"
            ext = os.path.splitext(bezpecny_nazov)[1]
            filename = f"faktura_tg_{user_id}_{uuid.uuid4().hex[:8]}{ext}"
            
        file_bytes = await file_obj.download_as_bytearray()
        
        # 1. Uložíme faktúru (v models.py sa cez on_commit spoľahlivo naplánuje asynchrónny Celery task)
        faktura_id = await ulozit_fakturu_z_telegramu(user, file_bytes, filename)
        
        # 2. Informujeme technika o skutočnom stave (doklad archivovaný, AI vyťaženie beží v pozadí)
        text = (
            "✅ <b>FAKTÚRA BOLA ÚSPEŠNE ZAEVIDOVANÁ!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"Doklad <b>#{faktura_id}</b> bol bezpečne archivovaný v systéme ISAS CORE.\n\n"
            "🤖 <i>AI modul (Gemini) na pozadí vyťažuje dodávateľa, sumy a skladové položky. "
            "Akonáhle Celery task vyťaženie dokončí, položky nájdete v <b>Logu Naskladnenia</b>.</i>\n\n"
            "📧 <i>Účtovníctvo bolo o zaevidovaní dokladu informované.</i>"
        )
        
        await status_msg.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Návrat do menu", callback_data="cancel_action")]]), parse_mode='HTML')
                        
    except Exception as e:
        error_message = str(e)
        # Skrátime chybovú správu, aby nikdy neprekročila limit Telegramu
        if len(error_message) > 400:
            error_message = error_message[:400] + "..."
        await status_msg.edit_text(f"❌ <b>Kritická chyba pri AI spracovaní:</b>\n<code>{error_message}</code>\n\nOpakuj pokus alebo kontaktuj admina.", parse_mode='HTML')
        
    return ConversationHandler.END

async def sklad_vyber_skladu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return ConversationHandler.END
    await update.callback_query.answer()
    
    # Ponúkne len AKTÍVNE sklady, ku ktorým má technik PRÍSTUP!
    user_id = update.effective_user.id if update.effective_user else None
    filialky = await get_zoznam_filialok_pre_bot(user_id)
    
    keyboard = [[InlineKeyboardButton("🏢 Hlavný sklad", callback_data="sklad_main_0")]]
    for f in filialky:
        keyboard.append([InlineKeyboardButton(f"🏢 {f['nazov']}", callback_data=f"sklad_main_{f['id']}")])
    keyboard.append([InlineKeyboardButton("🔙 Späť na hlavné menu", callback_data="cancel_action")])
    
    await update.callback_query.edit_message_text(
        "📦 <b>VÝBER SKLADU</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Kde sa práve nachádzaš / z ktorého skladu berieš tovar?", 
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML'
    )
    return S_WH

async def sklad_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return ConversationHandler.END
    await update.callback_query.answer()
    data = update.callback_query.data or ""

    if data.startswith("sklad_main_") and data != "sklad_main_back":
        try:
            if context.user_data is not None:
                context.user_data['wh_id'] = int(data.replace("sklad_main_", ""))
        except ValueError:
            if context.user_data is not None:
                context.user_data['wh_id'] = 0
            
    wh_id = context.user_data.get('wh_id', 0) if context.user_data is not None else 0
    wh_name = "Hlavný sklad"
    
    if wh_id != 0:
        try:
            f = await sync_to_async(Filialka.objects.get)(id=wh_id)
            wh_name = f.nazov
        except ObjectDoesNotExist:
            wh_name = "Neznámy sklad"

    keyboard = []
    for val, name in CATEGORY_CHOICES:
        keyboard.append([InlineKeyboardButton(f"📂 {name}", callback_data=f"cat_{val}")])
    keyboard.append([InlineKeyboardButton("🔙 Zmeniť sklad", callback_data="sklad_start")])
    
    await update.callback_query.edit_message_text(
        f"📦 <b>{wh_name.upper()}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Vyber hlavnú kategóriu:", 
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML'
    )
    return S_MAIN

async def sklad_sub_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return ConversationHandler.END
    await update.callback_query.answer()
    cat_name = (update.callback_query.data or "").replace("cat_", "")
    if context.user_data is not None:
        context.user_data['sklad_cat'] = cat_name
    
    subcats = CATEGORY_MAP.get(cat_name, {})
    keyboard = []
    for sub_name, model_class in subcats.items():
        keyboard.append([InlineKeyboardButton(f"📁 {sub_name}", callback_data=f"sub_{model_class.__name__}")])
        
    # Vrátenie na `sklad_main_` zabezpečí, že nebudeme znova vyberať sklad, 
    # ale použijeme `wh_id` pamäť a vráti to Kategórie pre už vybraný sklad.
    keyboard.append([InlineKeyboardButton("🔙 Späť na kategórie", callback_data="sklad_main_back")])
    
    await update.callback_query.edit_message_text(
        f"📂 <b>{cat_name.upper()}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Zvoľ si konkrétnu podkategóriu:", 
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML'
    )
    return S_SUB

async def _render_sklad_list(message_or_query, context, model_name, is_message=False):
    model_class, cat, subcat_name = await get_model_class_by_name(model_name)
    if not model_class:
        text = "⚠️ <b>Chyba:</b> Kategória tovaru nebola nájdená. Otvorte sklad znova z menu."
        if is_message:
            await message_or_query.reply_text(text, reply_markup=CANCEL_MARKUP, parse_mode='HTML')
        else:
            await message_or_query.edit_message_text(text, reply_markup=CANCEL_MARKUP, parse_mode='HTML')
        return

    user_data = context.user_data or {}
    search_term = str(user_data.get('sklad_search', "") or "").strip()
    page = int(user_data.get('sklad_page', 0) or 0)
    wh_id = int(user_data.get('wh_id', 0) or 0)
    
    start_idx = page * ITEMS_PER_PAGE
    end_idx = start_idx + ITEMS_PER_PAGE

    total_count, stavy = await get_stavy_pre_bota(model_class, wh_id, search_term, start_idx, end_idx)
    
    keyboard = []
    
    for item, stav in stavy:
        jednotka = getattr(item, 'merna_jednotka', 'ks')
        nazov_polozky = getattr(item, 'obchodny_nazov', None) or getattr(item, 'seriove_cislo', None) or getattr(item, 'nazov', None) or str(item)
        btn_text = f"📦 {nazov_polozky} (Skladom: {stav} {jednotka})"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"itm_{model_name}_{item.pk}")])
    
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Predchádzajúca", callback_data=f"page_{model_name}_{page-1}"))
    if end_idx < total_count:
        nav_buttons.append(InlineKeyboardButton("Ďalšia ➡️", callback_data=f"page_{model_name}_{page+1}"))
    if nav_buttons:
        keyboard.append(nav_buttons)

    if search_term:
        keyboard.append([InlineKeyboardButton(f"❌ Zrušiť filter: '{search_term}'", callback_data=f"clrsearch_{model_name}")])
    else:
        keyboard.append([InlineKeyboardButton("🔍 Hľadať (Názov/Číslo)", callback_data=f"asksrch_{model_name}")])

    keyboard.append([InlineKeyboardButton("🔙 Späť na podkategórie", callback_data=f"cat_{cat}")])
    
    wh_name = "Hlavný sklad"
    if wh_id != 0:
        try:
            f = await sync_to_async(Filialka.objects.get)(id=wh_id)
            wh_name = f.nazov
        except ObjectDoesNotExist:
            wh_name = "Neznámy sklad"

    text = (
        f"🏢 Sklad: <b>{wh_name}</b>\n"
        f"📁 Kategória: <b>{subcat_name.upper()}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
    )
    if search_term:
        text += f"🔍 <i>Filter: {search_term}</i>\n"
    
    if total_count == 0:
        text += "⚠️ <i>Nenašli sa žiadne položky.</i>"
    else:
        text += f"Celkový počet položiek: {total_count}\n"

    markup = InlineKeyboardMarkup(keyboard)

    if is_message:
        await message_or_query.reply_text(text, reply_markup=markup, parse_mode='HTML')
    else:
        await message_or_query.edit_message_text(text, reply_markup=markup, parse_mode='HTML')

async def sklad_item_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return ConversationHandler.END
    await update.callback_query.answer()
    data = update.callback_query.data or ""
    
    if data.startswith("sub_"):
        model_name = data.replace("sub_", "")
        if context.user_data is not None:
            context.user_data['sklad_page'] = 0
            context.user_data['sklad_search'] = ""
    elif data.startswith("page_"):
        parts = data.split("_")
        model_name = parts[1]
        if context.user_data is not None:
            context.user_data['sklad_page'] = int(parts[2])
    elif data.startswith("clrsearch_"):
        model_name = data.replace("clrsearch_", "")
        if context.user_data is not None:
            context.user_data['sklad_page'] = 0
            context.user_data['sklad_search'] = ""
    elif data.startswith("back_to_list_"):
        model_name = data.replace("back_to_list_", "")
    else:
        model_name = str(context.user_data.get('current_model', '') or '') if context.user_data is not None else ""
        
    if context.user_data is not None:
        context.user_data['current_model'] = model_name

    await _render_sklad_list(update.callback_query, context, model_name, is_message=False)
    return S_ITEM

async def ask_for_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return ConversationHandler.END
    await update.callback_query.answer()
    model_name = (update.callback_query.data or "").replace("asksrch_", "")
    if context.user_data is not None:
        context.user_data['current_model'] = model_name
    
    keyboard = [[InlineKeyboardButton("🔙 Zrušiť hľadanie", callback_data=f"back_to_list_{model_name}")]]
    await update.callback_query.edit_message_text(
        "🔍 <b>VYHĽADÁVANIE V SKLADE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Napíš názov alebo časť sériového čísla a odošli ako bežnú správu.",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML'
    )
    return S_SEARCH_WAIT

async def handle_search_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return S_ITEM
    search_term = update.message.text or ""
    model_name = str(context.user_data.get('current_model', '') or '') if context.user_data is not None else ""
    
    if context.user_data is not None:
        context.user_data['sklad_search'] = search_term
        context.user_data['sklad_page'] = 0
    
    await _render_sklad_list(update.message, context, model_name, is_message=True)
    return S_ITEM

async def sklad_item_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return ConversationHandler.END
    await update.callback_query.answer()
    query_data = update.callback_query.data or ""
    
    if context.user_data is None:
        await update.callback_query.edit_message_text(
            "⚠️ <b>Relácia vypršala!</b>\n"
            "Prosím, otvor sklad znova z hlavného menu.",
            reply_markup=CANCEL_MARKUP, parse_mode='HTML'
        )
        return ConversationHandler.END

    if query_data.startswith("itm_"):
        parts = query_data.split("_")
        model_name = parts[1]
        pk = parts[2]
        context.user_data['current_model'] = model_name
        context.user_data['current_pk'] = pk
        context.user_data['change'] = 0  
    elif query_data.startswith("edit:"):
        if 'change' not in context.user_data or 'current_model' not in context.user_data:
            await update.callback_query.edit_message_text(
                "⚠️ <b>Relácia vypršala!</b>\n"
                "Systém ISAS CORE bol medzičasom reštartovaný alebo aktualizovaný.\n"
                "Prosím, otvor sklad znova z hlavného menu.",
                reply_markup=CANCEL_MARKUP, parse_mode='HTML'
            )
            return ConversationHandler.END
            
        _, val = query_data.split(":")
        context.user_data['change'] += int(val)
        
    model_name = str(context.user_data.get('current_model') or '')
    pk = str(context.user_data.get('current_pk') or '')
    change = context.user_data.get('change', 0)
    wh_id = context.user_data.get('wh_id', 0)
    
    model_class, _, subcat_name = await get_model_class_by_name(model_name)
    
    # 🟢 Využijeme funkciu pre zistenie stavu podľa Filiálky / Hlavného skladu
    item, aktualny_stav = await get_item_stav_pre_bota(model_class, pk, wh_id)
    
    if not item:
        await update.callback_query.edit_message_text(
            "⚠️ <b>Položka už neexistuje!</b>\n"
            "Pravdepodobne bola vymazaná zo systému iným používateľom.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Späť na výber", callback_data=f"back_to_list_{model_name}")]]),
            parse_mode='HTML'
        )
        return S_ITEM

    sign = "+" if change > 0 else ""
    future_stock = aktualny_stav + change
    jednotka = getattr(item, 'merna_jednotka', 'ks')
    
    keyboard = [
        [InlineKeyboardButton("➖ 5", callback_data="edit:-5"), InlineKeyboardButton("➖ 1", callback_data="edit:-1"),
         InlineKeyboardButton(f"ÚPRAVA: {sign}{change}", callback_data="none"),
         InlineKeyboardButton("➕ 1", callback_data="edit:1"), InlineKeyboardButton("➕ 5", callback_data="edit:5")],
        [InlineKeyboardButton("✅ ULOŽIŤ ZMENU DO ISAS CORE", callback_data="save_item")],
        [InlineKeyboardButton("🔙 Späť na výber", callback_data=f"back_to_list_{model_name}")]
    ]
    
    wh_name = "Hlavný sklad"
    if wh_id != 0:
        try:
            f = await sync_to_async(Filialka.objects.get)(id=wh_id)
            wh_name = f.nazov
        except ObjectDoesNotExist:
            wh_name = "Neznámy sklad"

    nazov_polozky = getattr(item, 'obchodny_nazov', None) or getattr(item, 'seriove_cislo', None) or getattr(item, 'nazov', None) or str(item)
    text = (
        f"📦 <b>ÚPRAVA SKLADOVEJ POLOŽKY</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🏢 <b>Sklad:</b> {wh_name}\n"
        f"🏷 <b>Položka:</b> <code>{nazov_polozky}</code>\n"
        f"📂 <b>Kategória:</b> {subcat_name}\n"
        f"📦 <b>Aktuálne na sklade:</b> {aktualny_stav} {jednotka}\n\n"
        f"Tlačidlami zvoľ, o aké množstvo sa sklad mení.\n"
        f"🔄 <b>Zmena:</b> {sign}{change} {jednotka}\n"
        f"🏁 <b>Bude na sklade:</b> {future_stock} {jednotka}"
    )
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return S_ITEM

@sync_to_async
def uloz_zmenu_skladu_db(tg_id, model_class, pk, change, cat_name, sub_name, wh_id, typ_dokladu_erp):
    """
    🟢 ISAS CORE: Bezpečne vykoná operácie so zreteľom na Firewall a Ledger.
    """
    zamestnanec = Zamestnanec.objects.filter(telegram_id=tg_id).first()
    django_user = zamestnanec.user if zamestnanec else None

    with transaction.atomic():
        try:
            item = model_class.objects.select_for_update().get(pk=pk)
        except model_class.DoesNotExist:
            return None, None, None, None, 0, None, 0
        
        cisty_nazov = ocisti_nazov_od_jednotky(str(item))
        jednotka = getattr(item, 'merna_jednotka', 'ks')

        if wh_id != 0:
            # Sme na Filiálke
            filialka = Filialka.objects.filter(id=wh_id).first()
            if not filialka: return None, None, None, None, 0, None, 0
            
            povodny_stav = LedgerKalkulacka.zisti_stav_polozky(filialka, cisty_nazov)
            skutocna_zmena = change
            if change < 0 and abs(change) > povodny_stav:
                skutocna_zmena = -povodny_stav

            # NEDOTÝKAME SA FYZICKÉHO MODELU! Ukladáme len Log.
            if skutocna_zmena != 0:
                mov = StockMovement.objects.create(
                    filialka=filialka,
                    user=django_user, 
                    category=str(cat_name or '')[:50], subcategory=str(sub_name or '')[:100],
                    product_name=str(cisty_nazov or '')[:200], # Cistý názov!
                    action=StockMovement.ADD if skutocna_zmena > 0 else StockMovement.REMOVE, 
                    count=abs(skutocna_zmena),
                    merna_jednotka=str(jednotka or 'ks')[:10],
                    typ_dokladu_erp=typ_dokladu_erp
                )
                
            konecny_stav = povodny_stav + skutocna_zmena
            wh_name = filialka.nazov

        else:
            # Sme na Hlavnom Sklade
            povodny_stav = item.pocet_na_sklade
            skutocna_zmena = change
            if change < 0 and abs(change) > povodny_stav:
                skutocna_zmena = -povodny_stav

            item.pocet_na_sklade += skutocna_zmena
            item.save(update_fields=['pocet_na_sklade'])

            if skutocna_zmena != 0:
                mov = StockMovement.objects.create(
                    filialka=None,
                    user=django_user, 
                    category=str(cat_name or '')[:50], subcategory=str(sub_name or '')[:100],
                    product_name=str(cisty_nazov or '')[:200], 
                    action=StockMovement.ADD if skutocna_zmena > 0 else StockMovement.REMOVE, 
                    count=abs(skutocna_zmena),
                    merna_jednotka=str(jednotka or 'ks')[:10],
                    typ_dokladu_erp=typ_dokladu_erp
                )
                
            konecny_stav = item.pocet_na_sklade
            wh_name = "Hlavný sklad"

    meno_technika = zamestnanec.meno if zamestnanec else "Neznámy"
    nazov_polozky = getattr(item, 'obchodny_nazov', None) or getattr(item, 'seriove_cislo', None) or getattr(item, 'nazov', None) or str(item)
    return nazov_polozky, item.pk, jednotka, meno_technika, skutocna_zmena, wh_name, konecny_stav

@sync_to_async
def uloz_medziskladovy_presun_db(tg_id, zdroj_id, ciel_id, model_name, pk, count):
    """
    ISAS CORE: Logistika (Medziskladové presuny cez Telegram).
    Podporuje Hlavný <-> Filiálka aj Filiálka <-> Filiálka.
    """
    
    zamestnanec = Zamestnanec.objects.filter(telegram_id=tg_id).first()
    django_user = zamestnanec.user if zamestnanec else None
    
    zdroj_sklad = Filialka.objects.filter(id=zdroj_id).first() if zdroj_id != 0 else None
    ciel_sklad = Filialka.objects.filter(id=ciel_id).first() if ciel_id != 0 else None

    # Využitie centrálneho O(1) indexu
    model_class, cat_name, subcat_name = MODEL_NAME_MAP.get(model_name, (None, None, None))
    if not model_class:
        return False, "⚠️ Neznámy model položky."

    with transaction.atomic():
        try:
            product = model_class.objects.select_for_update().get(pk=pk)
        except model_class.DoesNotExist:
            return False, "🔍 Produkt neexistuje alebo bol vymazaný."

        cisty_nazov_produktu = ocisti_nazov_od_jednotky(str(product))
        jednotka = getattr(product, 'merna_jednotka', 'ks')
        
        if zdroj_sklad is None:
            if product.pocet_na_sklade < count:
                return False, f"📦 Na Hlavnom sklade nie je dostatok kusov! (Dostupné: {product.pocet_na_sklade} {jednotka})"
            product.pocet_na_sklade -= count
        else:
            povodny_stav_zdroj = LedgerKalkulacka.zisti_stav_polozky(zdroj_sklad, cisty_nazov_produktu)
            if povodny_stav_zdroj < count:
                return False, f"📦 Na sklade {zdroj_sklad.nazov} nie je dostatok kusov! (Dostupné: {povodny_stav_zdroj} {jednotka})"

        StockMovement.objects.create(
            filialka=zdroj_sklad, user=django_user, 
            category=str(cat_name or '')[:50], 
            subcategory=str(subcat_name or '')[:100],
            product_name=str(cisty_nazov_produktu or '')[:200], 
            count=count, action=StockMovement.REMOVE, 
            merna_jednotka=str(jednotka or 'ks')[:10], 
            typ_dokladu_erp='PREVODKA'
        )

        if ciel_sklad is None:
            product.pocet_na_sklade += count
        
        StockMovement.objects.create(
            filialka=ciel_sklad, user=django_user, 
            category=str(cat_name or '')[:50], 
            subcategory=str(subcat_name or '')[:100],
            product_name=str(cisty_nazov_produktu or '')[:200], 
            count=count, action=StockMovement.ADD, 
            merna_jednotka=str(jednotka or 'ks')[:10], 
            typ_dokladu_erp='PREVODKA'
        )

        if zdroj_sklad is None or ciel_sklad is None:
            product.save(update_fields=['pocet_na_sklade'])

        nazov_zdroja = zdroj_sklad.nazov if zdroj_sklad else "Hlavný sklad"
        nazov_ciela = ciel_sklad.nazov if ciel_sklad else "Hlavný sklad"
        
        choices_dict = dict(CATEGORY_CHOICES)
        kategoria_s_emoji = choices_dict.get(cat_name, cat_name)

        hlavna_sprava = (
            f"🗂️ <b>Kategória:</b> {kategoria_s_emoji} / {subcat_name}\n"
            f"📦 <b>Položka:</b> {cisty_nazov_produktu}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📤 <b>ZDROJ:</b> {nazov_zdroja}\n"
            f"📥 <b>CIEĽ:</b> {nazov_ciela}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔢 <b>Presunuté množstvo:</b> {count} {jednotka}"
        )

        try:
            from Evidencia.tasks import task_odosli_notifikaciu_presun
            user_name = f"{zamestnanec.meno} {zamestnanec.priezvisko}" if zamestnanec else "Neznámy technik"

            def _odosli_presun_notifikaciu(u=user_name, z=nazov_zdroja, c=nazov_ciela, p=cisty_nazov_produktu, cnt=count, j=jednotka):
                try:
                    task_odosli_notifikaciu_presun.delay(u, z, c, p, cnt, j)
                except Exception as exc:
                    logging.error(f"Zlyhalo odoslanie Celery úlohy pre medziskladový presun: {exc}")

            transaction.on_commit(_odosli_presun_notifikaciu)
        except Exception as task_err:
            logging.error(f"Zlyhalo naplánovanie Celery notifikácie pre medziskladový presun: {task_err}")

    return True, hlavna_sprava

@sync_to_async
def get_stavy_pre_bota(model_class, wh_id, search_term, start_idx, end_idx):
    if not model_class:
        return 0, []
    qs = model_class.objects.all()
    if search_term:
        qs = qs.filter(
            Q(seriove_cislo__icontains=search_term) |
            Q(obchodny_nazov__icontains=search_term)
        )
    qs = qs.order_by('seriove_cislo')
    total_count = qs.count()
    page_items = list(qs[start_idx:end_idx])

    stavy = []
    if wh_id != 0:
        filialka = Filialka.objects.filter(id=wh_id).first()
        if filialka:
            ledger_stavy = LedgerKalkulacka.zisti_stav_vsetkych_poloziek(filialka)
            for item in page_items:
                cisty = ocisti_nazov_od_jednotky(str(item))
                stavy.append((item, ledger_stavy.get(cisty, 0)))
        else:
            stavy = [(item, 0) for item in page_items]
    else:
        stavy = [(item, item.pocet_na_sklade) for item in page_items]

    return total_count, stavy

@sync_to_async
def odosli_notifikaciu_sklad_telegram_bot(meno_technika, cat_name, sub_name, item_cislo, pred_stav, akt_stav, jednotka, akcia_ikona, akcia_text, znamenko, change, admin_url, wh_name):
    """Unifikované odoslanie notifikácie o pohybe na sklade cez ISASNotifier."""
    payload = (
        ISASNotificationBuilder(
            nadpis=f"POHYB NA SKLADE - {akcia_text}",
            ikona=akcia_ikona,
            farba_temy="#2563eb" if change > 0 else "#ea580c"
        )
        .pridaj_riadok("Vykonal:", f"{safe_tg(meno_technika)} 📱 <i>(cez Telegram bota)</i>")
        .pridaj_riadok("Sklad:", safe_tg(wh_name))
        .pridaj_riadok("Kategória:", f"{safe_tg(cat_name)} / {safe_tg(sub_name)}")
        .pridaj_riadok("Položka:", f"{safe_tg(item_cislo)} (Stav: {pred_stav} {jednotka} ➔ {akt_stav} {jednotka})")
        .pridaj_riadok("Pohyb:", f"<b>{znamenko}{change} {jednotka}</b>")
        .nastav_tlacidlo("Otvoriť detail položky", admin_url)
        .nastav_ai_poznamku("Skladová evidencia bola úspešne synchronizovaná na diaľku.")
        .build()
    )
    ISASNotifier.send(payload=payload, send_tg=True, send_mail=True, send_discord=True)

async def sklad_vyber_typu_dokladu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🟢 ISAS CORE: Medzikrok pre výber účtovného typu dokladu pred uložením."""
    if not update.callback_query:
        return ConversationHandler.END
    await update.callback_query.answer()
    change = context.user_data.get('change', 0) if context.user_data is not None else 0
    
    if change == 0:
        await update.callback_query.answer("⚠️ Musíš zadať nejakú zmenu (nie nulu)!", show_alert=True)
        return
        
    keyboard = []
    if change > 0:
        keyboard.append([InlineKeyboardButton("📥 Príjemka (Nákup)", callback_data="doklad_PRIJEMKA")])
        keyboard.append([InlineKeyboardButton("📋 Inventúrny prebytok", callback_data="doklad_INVENTURA")])
    else:
        keyboard.append([InlineKeyboardButton("🔧 Vnútropodniková spotreba", callback_data="doklad_SPOTREBA")])
        keyboard.append([InlineKeyboardButton("📤 Výdajka (Predaj)", callback_data="doklad_VYDAJKA")])
        keyboard.append([InlineKeyboardButton("📋 Inventúrne manko", callback_data="doklad_INVENTURA")])
        
    curr_model = context.user_data.get('current_model', '') if context.user_data else ''
    curr_pk = context.user_data.get('current_pk', '') if context.user_data else ''
    keyboard.append([InlineKeyboardButton("🔙 Späť na úpravu", callback_data=f"itm_{curr_model}_{curr_pk}")])

    await update.callback_query.edit_message_text(
        "📑 <b>TYP POHYBU / DOKLADU</b>\n━━━━━━━━━━━━━━━━━━━━\nVyber, o aký typ pohybu ide, aby to účtovníčke sedelo:",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML'
    )
    return S_ITEM

async def sklad_item_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query or not update.effective_user:
        return ConversationHandler.END
    await update.callback_query.answer()
    typ_dokladu_erp = (update.callback_query.data or "").replace("doklad_", "")
    
    if not context.user_data or 'current_model' not in context.user_data or 'current_pk' not in context.user_data:
        await update.callback_query.edit_message_text(
            "⚠️ <b>Relácia vypršala!</b>\nOtvor prosím sklad znova z hlavného menu.",
            reply_markup=CANCEL_MARKUP,
            parse_mode='HTML'
        )
        return ConversationHandler.END

    change = context.user_data.get('change', 0)
    wh_id = context.user_data.get('wh_id', 0)
    model_name = str(context.user_data.get('current_model') or '')
    pk = str(context.user_data.get('current_pk') or '')
    
    model_class, cat_name, sub_name = await get_model_class_by_name(model_name)
    
    item_cislo, item_pk, jednotka, meno_technika, real_change, wh_name, konecny_stav = await uloz_zmenu_skladu_db(
        update.effective_user.id, model_class, pk, change, cat_name, sub_name, wh_id, typ_dokladu_erp
    )

    if not item_cislo:
        await update.callback_query.edit_message_text(
            "⚠️ <b>Chyba:</b> Položka už v systéme neexistuje (bola vymazaná).",
            reply_markup=CANCEL_MARKUP, parse_mode='HTML'
        )
        return ConversationHandler.END
    
    text = (
        f"✅ <b>ÚSPEŠNE ZAPÍSANÉ</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🏢 Sklad: <b>{wh_name}</b>\n"
        f"Položka: <code>{item_cislo}</code>\n"
        f"Zmena: <b>{'+' if real_change > 0 else ''}{real_change} {jednotka}</b>\n"
        f"Konečný stav: <b>{konecny_stav} {jednotka}</b>\n\n"
        f"<i>ISAS Systém synchronizovaný.</i>"
    )
    
    keyboard = [[InlineKeyboardButton("🔙 Návrat do menu", callback_data="cancel_action")]]
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    
    try:
        gn = await sync_to_async(GlobalneNastavenia.objects.first)()
        
        # Kalkulácia pôvodného stavu podľa reálnej zmeny (aby matika v maile sedela)
        pred_stav = konecny_stav - real_change
        znamenko = "+" if real_change > 0 else ""
        akcia_ikona = "📥" if real_change > 0 else "📤"
        akcia_text = "PRIDANIE NA SKLAD" if real_change > 0 else "VÝDAJ ZO SKLADU"
        base_url = gn.clean_base_url if gn else getattr(settings, 'SITE_URL', '')
        m_name_str = str(model_name or '').lower()
        admin_url = f"{base_url}/admin/Evidencia/{m_name_str}/{item_pk}/change/"

        await odosli_notifikaciu_sklad_telegram_bot(
            meno_technika, cat_name, sub_name, item_cislo, 
            pred_stav, konecny_stav, jednotka, akcia_ikona, akcia_text, 
            znamenko, real_change, admin_url, wh_name
        )

    except Exception as e: 
        logging.error(f"Chyba pri odosielaní TG/Email notifikácie zo skladu: {e}")
    
    return ConversationHandler.END

@sync_to_async
def vytvor_zaznam_cez_ai(tg_id, ai_json_data, zakaznik_id, zariadenie_id, typ_servisu_id, cart_data=None, foto_pred_path=None, foto_po_path=None, video_path=None):
    technik = Zamestnanec.objects.filter(telegram_id=tg_id).first()
    if not technik: return False, "Nenašiel som tvoj profil zamestnanca.", None
    
    # 1. Exaktné načítanie entít z databázy
    zakaznik = Zakaznik.objects.filter(id=zakaznik_id).first()
    zariadenie = ServisovaneZariadenie.objects.filter(id=zariadenie_id).first()
    typ_servisu = TypServisuPrace.objects.filter(id=typ_servisu_id).first()

    # 2. Očistenie textových polí od AI halucinácií ("null")
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

    # Sledovanie uložených súborov pre prípad zlyhania databázy
    ulozene_django_subory = []

    try:
        with transaction.atomic():
            # 3. Vytvorenie hlavného záznamu
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

            # 4. Hromadný zápis materiálu so zachovaním obchodného snapshotu (ochrana pred obídením .save() v bulk_create)
            if cart_data:
                materialy_na_vytvorenie = []
                for key, item in cart_data.items():
                    # O(1) mapovanie modelu
                    model_class, _, _ = MODEL_NAME_MAP.get(item['model_name'], (None, None, None))
                    
                    if model_class:
                        ct = ContentType.objects.get_for_model(model_class)
                        raw_pk = item.get('pk')
                        clean_pk = int(raw_pk) if raw_pk is not None and str(raw_pk).isdigit() else None
                        
                        obchodny_nazov = None
                        skryt_vyrobcu = False
                        if clean_pk:
                            try:
                                diel_obj = model_class.objects.get(pk=clean_pk)
                                obchodny_nazov = getattr(diel_obj, 'obchodny_nazov', None)
                                skryt_vyrobcu = getattr(diel_obj, 'skryt_vyrobcu', False)
                            except ObjectDoesNotExist:
                                pass

                        materialy_na_vytvorenie.append(
                            PouzityMaterialVZaznamoch(
                                zaznam=zaznam,
                                content_type=ct,
                                object_id=clean_pk,
                                seriove_cislo_text=str(item.get('nazov', ''))[:100],
                                pocet=int(item.get('count', 1)),
                                obchodny_nazov=obchodny_nazov,
                                skryt_vyrobcu=skryt_vyrobcu
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
                except (OSError, Exception) as del_err:
                    logging.warning(f"Nepodarilo sa vymazať osirotený súbor {django_file.name}: {del_err}")
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
def skontroluj_suvisiace_ulohy(zakaznik_id, zariadenie_id):
    if not zariadenie_id or not zakaznik_id:
        return None, "NO_MATCH"

    ulohy = Uloha.objects.filter(
        dokoncene=False,
        zakaznik_id=zakaznik_id,
        zariadenia__id=zariadenie_id
    ).select_related('zakaznik').prefetch_related('typ_servisu', 'zariadenia', 'technici').order_by('zaciatok')
    
    pocet = ulohy.count()
    if pocet == 0:
        return None, "NO_MATCH"
        
    vysledok = []
    for u in ulohy:
        vysledok.append({
            'id': u.id,
            'zaciatok_datum': (timezone.localtime(u.zaciatok) if (u.zaciatok and timezone.is_aware(u.zaciatok)) else u.zaciatok).strftime("%d.%m.%Y") if u.zaciatok else "Neznámy dátum",
            'is_dnes': (timezone.localtime(u.zaciatok) if (u.zaciatok and timezone.is_aware(u.zaciatok)) else u.zaciatok).date() == timezone.now().date() if u.zaciatok else False,
            'typy_prace': html.escape(", ".join([str(t) for t in u.typ_servisu.all()]) or "Všeobecný servis"),
            'zariadenia': html.escape(", ".join([str(z) for z in u.zariadenia.all()]) or "Neznáme zariadenie"),
            'info': html.escape(getattr(u, 'blizsie_informacie', '') or ''),
            'zariadenia_count': len(u.zariadenia.all()),
            'defstr': html.escape(str(u))
        })

    if pocet == 1:
        if vysledok[0]['zariadenia_count'] == 1:
            return vysledok, "FULL_MATCH"
        else:
            return vysledok, f"PARTIAL_MATCH_{vysledok[0]['zariadenia_count']}"
    else:
        return vysledok, "ANOMALY"

@sync_to_async
def uzavri_ulohu_v_db(uloha_id, tg_id):
    technik = Zamestnanec.objects.filter(telegram_id=tg_id).first()
    if not technik:
        return False, None

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
        # Znovu načítame objekt bez zámku pre potreby notifikácie
        uloha_notif = Uloha.objects.get(id=uzavreta_id)
        uloha_notif.odoslat_notifikacie_adminovi()
        return True, uzavreta_id
        
    return False, None

async def close_task_from_tg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query or not update.effective_user:
        return ConversationHandler.END
    await update.callback_query.answer()
    data = update.callback_query.data or ""
    uloha_id = data.replace("close_task_", "")
    success, uzavreta_uloha_id = await uzavri_ulohu_v_db(uloha_id, update.effective_user.id)

    if success:
        text = f"✅ <b>ÚLOHA #{uzavreta_uloha_id} BOLA UZAVRETÁ!</b>\n━━━━━━━━━━━━━━━━━━━━\nSystém automaticky spároval tvoj servisný záznam a uzavrel administratívu. Majiteľ bol informovaný."
    else:
        text = "⚠️ <b>Chyba:</b> Úlohu sa nepodarilo uzavrieť (možno už bola uzavretá iným technikom)."
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Návrat do menu", callback_data="cancel_action")]]), parse_mode='HTML')
    return ConversationHandler.END

async def zacat_servis_hlasom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return ConversationHandler.END
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        "🛠️ <b>NAHLÁSENIE SERVISU</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        "Najprv mi napíš názov Zákazníka (alebo aspoň jeho časť):", 
        reply_markup=CANCEL_MARKUP, parse_mode='HTML'
    )
    return SH_ZAKAZNIK_SEARCH

async def sh_zakaznik_search_vyzva(update: Update, context: ContextTypes.DEFAULT_TYPE):
    hladany_text = update.message.text if update.message and update.message.text else ""
    zakaznici = await vyhladaj_zakaznika_pre_bot(hladany_text)

    if not zakaznici:
        await posli_docasnu_chybu(update, context, f"⚠️ Nenašiel som žiadneho zákazníka obsahujúceho '<b>{html.escape(hladany_text)}</b>'. Skús to znova:", reply_markup=CANCEL_MARKUP)
        return SH_ZAKAZNIK_SEARCH

    keyboard = [[InlineKeyboardButton(f"🏢 {z['Nazov_spolocnosti'][:50]}", callback_data=f"sh_zak_{z['id']}")] for z in zakaznici]
    keyboard.append([InlineKeyboardButton("❌ Zrušiť", callback_data="cancel_action")])
    if update.message:
        await update.message.reply_text("🤝 <b>Vyber zákazníka:</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return SH_ZAKAZNIK_SELECT

async def sh_zakaznik_select_spracuj(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return ConversationHandler.END
    await update.callback_query.answer()
    zakaznik_id = int((update.callback_query.data or "").replace("sh_zak_", ""))
    if context.user_data is not None:
        context.user_data['sh_zakaznik_id'] = zakaznik_id
    
    zariadenia = await get_zariadenia_pre_zakaznika_bot(zakaznik_id)
    if not zariadenia:
        dev_mnozno_zapor = get_industry_term('zariadenia_mnozno_zapor', 'zariadenia, technologické celky ani položky')
        await update.callback_query.edit_message_text(f"⚠️ Tento zákazník nemá evidované žiadne {dev_mnozno_zapor}. Kontaktuj dispečing.", reply_markup=CANCEL_MARKUP)
        return ConversationHandler.END

    keyboard = []
    for z in zariadenia:
        fallback_nazov = get_industry_term('zariadenie_fallback_nazov', 'Zariadenie')
        popis = (z['nazov'] or z['model_zariadenia'] or fallback_nazov)[:30]
        badge = f" [{z.get('zaruka_badge', '')}]" if z.get('zaruka_badge') else ""
        btn_text = f"{z['kategoria_display']} | {popis} ({z['vyrobne_cislo']}){badge}"[:64]
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"sh_zar_{z['id']}")])
    keyboard.append([InlineKeyboardButton("❌ Zrušiť", callback_data="cancel_action")])
    dev_title = get_industry_term('zariadenie_fallback_nazov', 'Zariadenie')
    await update.callback_query.edit_message_text(f"⚙️ <b>Vyber {dev_title.lower()}:</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return SH_ZARIADENIE_SELECT

async def sh_zariadenie_select_spracuj(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return ConversationHandler.END
    await update.callback_query.answer()
    zariadenie_id = int((update.callback_query.data or "").replace("sh_zar_", ""))
    if context.user_data is not None:
        context.user_data['sh_zariadenie_id'] = zariadenie_id
    
    typy = await get_typy_servisu_bot()
    keyboard = [[InlineKeyboardButton(f"🛠️ {t['nazov']}", callback_data=f"sh_typ_{t['id']}")] for t in typy]
    keyboard.append([InlineKeyboardButton("❌ Zrušiť", callback_data="cancel_action")])
    await update.callback_query.edit_message_text("🛠️ <b>Vyber typ vykonanej práce:</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return SH_TYP_SELECT

async def sh_typ_select_spracuj(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return ConversationHandler.END
    await update.callback_query.answer()
    typ_id = int((update.callback_query.data or "").replace("sh_typ_", ""))
    if context.user_data is not None:
        context.user_data['sh_typ_id'] = typ_id
        context.user_data['sh_cart'] = {} # Inicializácia košíka
    
    return await sh_zobraz_kosik(update, context)

async def sh_zobraz_kosik(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cart = context.user_data.get('sh_cart', {}) if context.user_data is not None else {}
    polozka_label = get_industry_term('polozka_fallback_nazov', 'Položka')
    text = f"🛒 <b>POUŽITÝ MATERIÁL A {polozka_label.upper()}</b>\n━━━━━━━━━━━━━━━━━━━━\n"
    
    if not cart:
        text += "<i>Zatiaľ neboli pridané žiadne položky.</i>\n\n"
    else:
        for key, item in cart.items():
            text += f"▪️ <b>{item['count']} {item['jednotka']}</b> - {item['nazov']} (Sklad: {item['wh_name']})\n"
        text += "\n"
        
    keyboard = [
        [InlineKeyboardButton(f"➕ Pridať ({polozka_label})", callback_data="sh_add_item")],
    ]
    if cart:
        keyboard.append([InlineKeyboardButton("❌ Odstrániť položku", callback_data="sh_remove_item")])
        
    keyboard.append([InlineKeyboardButton("➡️ Pokračovať na hlasovku", callback_data="sh_go_voice")])
    keyboard.append([InlineKeyboardButton("❌ Zrušiť servis", callback_data="cancel_action")])
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    elif update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return SH_KOSIK

async def sh_kosik_akcia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return ConversationHandler.END
    await update.callback_query.answer()
    data = update.callback_query.data or ""
    
    if data == "sh_go_voice":
        return await sh_zobraz_media_menu(update, context)
        
    elif data == "sh_remove_item":
        cart = context.user_data.get('sh_cart', {}) if context.user_data is not None else {}
        keyboard = []
        for key, item in cart.items():
            keyboard.append([InlineKeyboardButton(f"❌ {item['nazov']}", callback_data=f"sh_del_{key}")])
        keyboard.append([InlineKeyboardButton("🔙 Späť", callback_data="sh_back_kosik")])
        polozka_label = get_industry_term('polozka_fallback_nazov', 'položku').lower()
        await update.callback_query.edit_message_text(f"Vyber {polozka_label}, ktorú chceš odstrániť:", reply_markup=InlineKeyboardMarkup(keyboard))
        return SH_KOSIK
        
    elif data.startswith("sh_del_"):
        key = data.replace("sh_del_", "")
        if context.user_data is not None and key in context.user_data.get('sh_cart', {}):
            del context.user_data['sh_cart'][key]
        return await sh_zobraz_kosik(update, context)
        
    elif data == "sh_back_kosik":
        return await sh_zobraz_kosik(update, context)
        
    elif data == "sh_add_item":
        if context.user_data is not None:
            context.user_data['sh_wh_id'] = 0  # Natvrdo nastavíme Hlavný sklad (Katalóg)
        
        keyboard = []
        for val, name in CATEGORY_CHOICES:
            keyboard.append([InlineKeyboardButton(f"📂 {name}", callback_data=f"sh_cat_{val}")])
        keyboard.append([InlineKeyboardButton("🔙 Späť do košíka", callback_data="sh_back_kosik")])
        
        polozka_genitiv = get_industry_term('polozka_fallback_nazov', 'položky').lower()
        await update.callback_query.edit_message_text(f"🗂️ <b>Vyber kategóriu ({polozka_genitiv}):</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        return SH_SKLAD_CAT

async def sh_zobraz_media_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = context.user_data or {}
    text = "📸 <b>FOTOGRAFIE A VIDEO (Voliteľné)</b>\n━━━━━━━━━━━━━━━━━━━━\nChceš k záznamu priložiť fotky alebo video z opravy?"
    
    # Zistíme, čo už je nahraté
    f_pred = "✅ Nahraté" if user_data.get('sh_foto_pred') else "Pridať"
    f_po = "✅ Nahraté" if user_data.get('sh_foto_po') else "Pridať"
    v_vid = "✅ Nahraté" if user_data.get('sh_video') else "Pridať"

    keyboard = [
        [InlineKeyboardButton(f"📸 Foto PRED ({f_pred})", callback_data="sh_media_pred")],
        [InlineKeyboardButton(f"📸 Foto PO ({f_po})", callback_data="sh_media_po")],
        [InlineKeyboardButton(f"🎥 Video ({v_vid})", callback_data="sh_media_vid")],
        [InlineKeyboardButton("➡️ Pokračovať na hlasovku", callback_data="sh_media_go")],
        [InlineKeyboardButton("❌ Zrušiť servis", callback_data="cancel_action")]
    ]
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    elif update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return SH_MEDIA_MENU

async def sh_media_akcia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query or context.user_data is None:
        return ConversationHandler.END
    await update.callback_query.answer()
    data = update.callback_query.data or ""
    
    if data == "sh_media_pred":
        await update.callback_query.edit_message_text("📸 <b>Pošli fotku PRED servisom.</b>", reply_markup=CANCEL_MARKUP, parse_mode='HTML')
        return SH_MEDIA_FOTO_PRED
    elif data == "sh_media_po":
        await update.callback_query.edit_message_text("📸 <b>Pošli fotku PO servise.</b>", reply_markup=CANCEL_MARKUP, parse_mode='HTML')
        return SH_MEDIA_FOTO_PO
    elif data == "sh_media_vid":
        await update.callback_query.edit_message_text("🎥 <b>Pošli krátke video z opravy.</b>", reply_markup=CANCEL_MARKUP, parse_mode='HTML')
        return SH_MEDIA_VIDEO
    elif data == "sh_media_go":
        user_id = update.effective_user.id if update.effective_user else None
        obj, typ = await identifikuj_pouzivatela(user_id)
        technik = obj if typ == "ZAMESTNANEC" else None
        if not technik:
            await update.callback_query.edit_message_text("⚠️ <b>Chyba autorizácie:</b> Profil technika nebol nájdený.", reply_markup=CANCEL_MARKUP, parse_mode='HTML')
            return ConversationHandler.END
        context.user_data['technik_db_id'] = technik.id
        draft_text = cache.get(f"isas_quicknote_draft_{technik.id}")
        
        if draft_text:
            context.user_data['sh_draft'] = draft_text
            text = (
                "📝 <b>NAŠIEL SOM TVOJE POZNÁMKY Z DIAGNOSTIKY:</b>\n"
                f"<i>„{draft_text}“</i>\n\n"
                "<b>Ako ich chceš použiť pre servisný záznam?</b>"
            )
            keyboard = [
                [InlineKeyboardButton("📥 Importovať a rovno uložiť", callback_data="sh_draft_only")],
                [InlineKeyboardButton("🎤 Importovať a dopovedať hlasom", callback_data="sh_draft_voice")],
                [InlineKeyboardButton("🚫 Ignorovať (Nahrať iba hlasovku)", callback_data="sh_draft_ignore")],
                [InlineKeyboardButton("❌ Zrušiť servis", callback_data="cancel_action")]
            ]
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
            return SH_HLASOVKA
        else:
            context.user_data['sh_draft'] = None
            m_label = get_industry_term('meter_label', 'Prevádzkový čas')
            dev_nazov = get_industry_term('zariadenie_fallback_nazov', 'Zariadenie')
            text = (
                "🎤 <b>VŠETKO PRIPRAVENÉ</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
                "Stlač ikonku mikrofónu a povedz mi detaily.\n\n"
                f"<i>💡 Príklad:\n„{m_label} sú 4520. {dev_nazov} beží bez porúch.“</i>\n\n"
                "🎤 <b>Čakám na tvoju hlasovku...</b>"
            )
            await update.callback_query.edit_message_text(text, reply_markup=CANCEL_MARKUP, parse_mode='HTML')
            return SH_HLASOVKA

async def sh_prijmi_foto_pred(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data is not None:
        context.user_data['current_state'] = SH_MEDIA_FOTO_PRED
    return await sh_prijmi_media_core(update, context, 'sh_foto_pred')

async def sh_prijmi_foto_po(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data is not None:
        context.user_data['current_state'] = SH_MEDIA_FOTO_PO
    return await sh_prijmi_media_core(update, context, 'sh_foto_po')

async def sh_prijmi_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data is not None:
        context.user_data['current_state'] = SH_MEDIA_VIDEO
    return await sh_prijmi_media_core(update, context, 'sh_video')

async def sh_prijmi_media_core(update: Update, context: ContextTypes.DEFAULT_TYPE, storage_key: str):
    if not update.message or not update.effective_user:
        return ConversationHandler.END
    current_state = context.user_data.get('current_state') if context.user_data is not None else ConversationHandler.END
    if update.message.photo:
        file_obj = await update.message.photo[-1].get_file()
        ext = ".jpg"
    elif update.message.video:
        # 🛡️ Ochrana veľkosti súboru (Telegram limit je 20MB)
        if update.message.video.file_size and update.message.video.file_size > 20000000:
            await update.message.reply_text("⚠️ <b>Chyba:</b> Video je príliš veľké (Max 20 MB). Skús nahrať kratšie video.", reply_markup=CANCEL_MARKUP, parse_mode='HTML')
            return current_state
        file_obj = await update.message.video.get_file()
        ext = ".mp4"
    elif update.message.document:
        if update.message.document.file_size and update.message.document.file_size > 20000000:
            await update.message.reply_text("⚠️ <b>Chyba:</b> Súbor je príliš veľký (Max 20 MB).", reply_markup=CANCEL_MARKUP, parse_mode='HTML')
            return current_state
        file_obj = await update.message.document.get_file()
        ext = os.path.splitext(update.message.document.file_name)[1] if update.message.document.file_name else ".file"
    else:
        await update.message.reply_text("⚠️ Prosím, pošli platný súbor (Fotku alebo Video).", reply_markup=CANCEL_MARKUP)
        return current_state

    path = f"temp_servis_{update.effective_user.id}_{uuid.uuid4().hex[:8]}{ext}"
    
    try:
        old_path = context.user_data.get(storage_key) if context.user_data else None
        if old_path and os.path.exists(old_path):
            try:
                os.remove(old_path)
            except OSError:
                pass

        await file_obj.download_to_drive(path)
        if context.user_data is not None:
            context.user_data[storage_key] = path
    except Exception as e:
        await update.message.reply_text(f"⚠️ <b>Chyba pri sťahovaní súboru:</b> {str(e)}", reply_markup=CANCEL_MARKUP, parse_mode='HTML')
        return context.user_data.get('current_state') if context.user_data is not None else ConversationHandler.END

    return await sh_zobraz_media_menu(update, context)

async def sh_draft_akcia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return ConversationHandler.END
    await update.callback_query.answer()
    data = update.callback_query.data or ""
    
    if data == "sh_draft_only":
        if context.user_data is not None:
            context.user_data['use_draft'] = True
        # Simulujeme, že poslal hlasovku, ale spracujeme iba text
        await update.callback_query.edit_message_text("🔄 <i>Spracovávam importované poznámky...</i>", parse_mode='HTML')
        # Zavoláme funkciu pre spracovanie, ale podhodíme jej callback_query namiesto message
        return await spracuj_servis_hlasovka(update, context, is_text_only=True)
        
    elif data == "sh_draft_voice":
        if context.user_data is not None:
            context.user_data['use_draft'] = True
        text = "🎤 <b>POZNÁMKY PRIPRAVENÉ NA IMPORT</b>\n━━━━━━━━━━━━━━━━━━━━\nTeraz mi pošli hlasovku s detailmi, ktoré chceš k poznámkam doplniť."
        await update.callback_query.edit_message_text(text, reply_markup=CANCEL_MARKUP, parse_mode='HTML')
        return SH_HLASOVKA
        
    elif data == "sh_draft_ignore":
        if context.user_data is not None:
            context.user_data['use_draft'] = False
        text = "🎤 <b>POZNÁMKY ZAHODENÉ</b>\n━━━━━━━━━━━━━━━━━━━━\nPošli mi hlasovku s kompletnými detailmi servisu."
        await update.callback_query.edit_message_text(text, reply_markup=CANCEL_MARKUP, parse_mode='HTML')
        return SH_HLASOVKA

async def sh_sklad_cat_spracuj(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return ConversationHandler.END
    await update.callback_query.answer()
    cat_name = (update.callback_query.data or "").replace("sh_cat_", "")
    if context.user_data is not None:
        context.user_data['sh_cat'] = cat_name
    
    subcats = CATEGORY_MAP.get(cat_name, {})
    keyboard = []
    for sub_name, model_class in subcats.items():
        keyboard.append([InlineKeyboardButton(f"📁 {sub_name}", callback_data=f"sh_sub_{model_class.__name__}")])
    keyboard.append([InlineKeyboardButton("🔙 Späť do košíka", callback_data="sh_back_kosik")])
    
    await update.callback_query.edit_message_text(f"📂 <b>{cat_name.upper()}</b>\nZvoľ podkategóriu:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return SH_SKLAD_SUB

async def sh_sklad_sub_spracuj(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return ConversationHandler.END
    await update.callback_query.answer()
    data = update.callback_query.data or ""
    
    if data.startswith("sh_sub_"):
        model_name = data.replace("sh_sub_", "")
        if context.user_data is not None:
            context.user_data['sh_model'] = model_name
            context.user_data['sh_page'] = 0
    elif data.startswith("sh_page_"):
        if context.user_data is not None:
            context.user_data['sh_page'] = int(data.replace("sh_page_", ""))
        
    model_name = str(context.user_data.get('sh_model', '') or '') if context.user_data is not None else ""
    page = context.user_data.get('sh_page', 0) if context.user_data is not None else 0
    wh_id = context.user_data.get('sh_wh_id', 0) if context.user_data is not None else 0
    
    model_class, cat, subcat_name = await get_model_class_by_name(model_name)
    start_idx = page * 10
    end_idx = start_idx + 10
    
    total_count, stavy = await get_stavy_pre_bota(model_class, wh_id, "", start_idx, end_idx)
    
    keyboard = []
    for item, stav in stavy:
        jednotka = getattr(item, 'merna_jednotka', 'ks')
        nazov_polozky = getattr(item, 'obchodny_nazov', None) or getattr(item, 'seriove_cislo', None) or getattr(item, 'nazov', None) or str(item)
        keyboard.append([InlineKeyboardButton(f"📦 {nazov_polozky} (Dostupné: {stav} {jednotka})", callback_data=f"sh_itm_{item.pk}")])
        
    nav_buttons = []
    if page > 0: nav_buttons.append(InlineKeyboardButton("⬅️", callback_data=f"sh_page_{page-1}"))
    if end_idx < total_count: nav_buttons.append(InlineKeyboardButton("➡️", callback_data=f"sh_page_{page+1}"))
    if nav_buttons: keyboard.append(nav_buttons)
    
    keyboard.append([InlineKeyboardButton("🔙 Späť do košíka", callback_data="sh_back_kosik")])
    
    polozka_label = get_industry_term('polozka_fallback_nazov', 'položku')
    await update.callback_query.edit_message_text(f"📦 <b>VÝBER POLOŽKY</b>\nZvoľ konkrétnu {polozka_label}:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return SH_SKLAD_ITEM

async def sh_sklad_item_spracuj(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return ConversationHandler.END
    await update.callback_query.answer()
    pk = (update.callback_query.data or "").replace("sh_itm_", "")
    if context.user_data is not None:
        context.user_data['sh_pk'] = pk
    
    await update.callback_query.edit_message_text("🔢 <b>ZADAJ MNOŽSTVO</b>\nNapíš do chatu presný počet kusov (číslo), ktoré si použil:", reply_markup=CANCEL_MARKUP, parse_mode='HTML')
    return SH_SKLAD_COUNT

async def sh_sklad_count_spracuj(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return SH_SKLAD_COUNT
    pocet_str = update.message.text.strip()
    
    if not pocet_str.isdigit() or int(pocet_str) <= 0 or int(pocet_str) > 1000000:
        await posli_docasnu_chybu(update, context, "⚠️ <b>Chyba:</b> Musíš zadať platné celé číslo v rozsahu 1 až 1 000 000. Skús to znova:", reply_markup=CANCEL_MARKUP)
        return SH_SKLAD_COUNT

    if not context.user_data or 'sh_model' not in context.user_data or 'sh_pk' not in context.user_data:
        await update.message.reply_text(
            "⚠️ <b>Relácia vypršala!</b>\nZačni nahlásenie servisu znova z hlavného menu.",
            reply_markup=CANCEL_MARKUP, parse_mode='HTML'
        )
        return ConversationHandler.END
        
    count = int(pocet_str)
    model_name = context.user_data.get('sh_model')
    pk = context.user_data.get('sh_pk')
    wh_id = context.user_data.get('sh_wh_id', 0)
    
    model_class, cat_name, sub_name = await get_model_class_by_name(model_name)
    item = await get_item_instance(model_class, pk)
    
    if item:
        wh_name = "📕 Katalóg položiek"
            
        key = f"{model_name}_{pk}_{wh_id}"
        nazov_polozky = getattr(item, 'obchodny_nazov', None) or getattr(item, 'seriove_cislo', None) or getattr(item, 'nazov', None) or str(item)
        if context.user_data is not None:
            context.user_data.setdefault('sh_cart', {})[key] = {
                'model_name': model_name,
                'pk': pk,
                'count': count,
                'nazov': nazov_polozky,
                'jednotka': getattr(item, 'merna_jednotka', 'ks'),
                'wh_id': wh_id,
                'wh_name': wh_name,
                'cat_name': cat_name,
                'sub_name': sub_name
            }
        
    return await sh_zobraz_kosik(update, context)

async def spracuj_servis_hlasovka(update: Update, context: ContextTypes.DEFAULT_TYPE, is_text_only=False):
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not chat_id:
        return ConversationHandler.END

    if not context.user_data:
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ <b>Relácia vypršala!</b>\nSystém bol medzičasom reštartovaný alebo aktualizovaný. Začni prosím proces nahlásenia servisu znova z hlavného menu.", 
            reply_markup=CANCEL_MARKUP,
            parse_mode='HTML'
        )
        return ConversationHandler.END

    zakaznik_id = context.user_data.get('sh_zakaznik_id')
    zariadenie_id = context.user_data.get('sh_zariadenie_id')

    if not zakaznik_id or not zariadenie_id:
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ <b>Relácia vypršala!</b>\nSystém bol medzičasom reštartovaný alebo aktualizovaný. Začni prosím proces nahlásenia servisu znova z hlavného menu.", 
            reply_markup=CANCEL_MARKUP,
            parse_mode='HTML'
        )
        return ConversationHandler.END

    raw_cart = context.user_data.get('sh_cart')
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

    if is_text_only:
        thinking_msg = await context.bot.send_message(chat_id=chat_id, text="🔄 <i>Spracovávam importované poznámky...</i>", parse_mode='HTML')
        audio_path = None
    else:
        if not update.message or not update.message.voice:
            await posli_docasnu_chybu(update, context, "⚠️ <b>Chyba:</b> Chýba hlasová správa.", reply_markup=CANCEL_MARKUP)
            return SH_HLASOVKA
        thinking_msg = await context.bot.send_message(chat_id=chat_id, text="🎧 <i>Analyzujem hlasový záznam...</i>", parse_mode='HTML')
        voice_file = await update.message.voice.get_file()
        user_tg_id = update.effective_user.id if update.effective_user else 0
        audio_path = f"servis_{user_tg_id}_{uuid.uuid4().hex[:8]}.ogg"
        await voice_file.download_to_drive(audio_path)
        
    # Pripojíme poznámky z webu k promptu, ak si to technik vyžiadal
    if context.user_data.get('use_draft') and context.user_data.get('sh_draft'):
        ai_prompt += f"\n\nPOZNÁMKY Z DIAGNOSTIKY (Zahrň ich do analýzy ako primárny zdroj faktov):\n{context.user_data['sh_draft']}"
        
        technik_id = context.user_data.get('technik_db_id')
        if technik_id:
            cache.delete(f"isas_quicknote_draft_{technik_id}")
        context.user_data['sh_draft'] = None

    await thinking_msg.edit_text("🧠 <i>Umelá inteligencia ukladá dáta...</i>", parse_mode='HTML')
    
    try:
        raw_ai = await sync_to_async(ask_isas_ai)(user_question=ai_prompt, audio_path=audio_path, force_json=True)
            
        # Bezpečné vytiahnutie textu, ak AI vráti slovník (dict)
        if isinstance(raw_ai, dict):
            raw_ai_text = raw_ai.get("text", "")
        else:
            raw_ai_text = getattr(raw_ai, "text", str(raw_ai))
      
        try:
            ai_data = clean_and_parse_ai_json(raw_ai_text.replace('<br>', '\n'))
        except Exception as json_err:
            logging.error(f"Zlyhalo dekódovanie AI JSON výstupu: {json_err}. Raw text: {raw_ai_text}")
            raise ValueError(f"AI nevrátila platný formát JSON. Skutočná odpoveď: {raw_ai_text[:300]}")

        zakaznik_id = context.user_data.get('sh_zakaznik_id')
        zariadenie_id = context.user_data.get('sh_zariadenie_id')
        typ_id = context.user_data.get('sh_typ_id')

        cart = context.user_data.get('sh_cart', {})
        f_pred = context.user_data.get('sh_foto_pred')
        f_po = context.user_data.get('sh_foto_po')
        v_vid = context.user_data.get('sh_video')
            
        user_id = update.effective_user.id if update.effective_user else 0
        success, sprava, zaznam_data = await vytvor_zaznam_cez_ai(
            user_id, ai_data, zakaznik_id, zariadenie_id, typ_id, 
            cart_data=cart, foto_pred_path=f_pred, foto_po_path=f_po, video_path=v_vid
        )

        if success:
            gn = await sync_to_async(GlobalneNastavenia.objects.first)()
                
            base_url = gn.clean_base_url if gn else getattr(settings, 'SITE_URL', '')
            link_na_podpis = f"{base_url}/evidencia/protokol/{zaznam_data['bezpecnostny_kluc']}/"
                
            zakaznik_text = zaznam_data.get('zakaznik_nazov', "Nezistený")
            zariadenie_text = zaznam_data.get('zariadenie_nazov', "Nezistené")
            praca_text = zaznam_data.get('typ_servisu_nazov', "Nezistený")
                
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

            dev_title = get_industry_term('zariadenie_fallback_nazov', 'Zariadenie')
            text = (
                f"✅ <b>SERVIS BOL VYTVORENÝ!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🏢 <b>Zákazník:</b> {zakaznik_text}\n"
                f"⚙️ <b>{dev_title}:</b> {zariadenie_text}\n"
                f"⏱ <b>{m_label}:</b> {mh_text}\n"
                f"🛠 <b>Práca:</b> {praca_text}\n"
                f"📝 <b>SERVISNÝ PROTOKOL:</b>\n"
                f"Klikni na odkaz, skontroluj AI text a daj to podpísať zákazníkovi:\n"
                f"👉 <a href='{link_na_podpis}'>Otvoriť elektronický protokol</a>"
            )

            # 1. Najprv upravíme pôvodnú správu len na informáciu o vytvorení servisu
            await thinking_msg.edit_text(text, parse_mode='HTML')

            # --- ISAS SMART MATCH LOGIKA ---
            ulohy_list, match_type = await skontroluj_suvisiace_ulohy(zaznam_data['zakaznik_id'], zaznam_data['zariadenie_id'])
                
            if match_type == "FULL_MATCH":
                uloha = ulohy_list[0]
                smart_text = (
                    f"🤖 <b>ISAS CORE - Automatická detekcia</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"Servisný záznam bol uložený. Systém našiel otvorenú úlohu, ktorá s týmto záznamom súvisí. Mám ju rovno označiť ako dokončenú?\n\n"
                    f"📌 <b>Úloha:</b> {uloha['defstr']}\n"
                )
                if uloha.get('info'):
                    smart_text += f"ℹ️ <b>Detail zadania:</b> <i>{uloha['info']}</i>\n"
                    
                keyboard = [
                    [InlineKeyboardButton(f"✅ Áno, uzavrieť Úlohu #{uloha['id']}", callback_data=f"close_task_{uloha['id']}")],
                    [InlineKeyboardButton("❌ Nie, nechať otvorenú", callback_data="cancel_action")]
                ]
                # 2. Pošleme Smart Matcher ako ÚPLNE NOVÚ SPRÁVU
                await context.bot.send_message(chat_id=chat_id, text=smart_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
                return ConversationHandler.END
                    
            elif match_type == "ANOMALY":
                pocet_uloh = len(ulohy_list)
                if 2 <= pocet_uloh <= 4:
                    text_najdene = f"{pocet_uloh} otvorené úlohy, ktoré s týmto záznamom súvisia"
                    text_eviduje = f"{pocet_uloh} otvorené úlohy"
                else:
                    text_najdene = f"{pocet_uloh} otvorených úloh, ktoré s týmto záznamom súvisia"
                    text_eviduje = f"{pocet_uloh} otvorených úloh"
                    
                smart_text = (
                    f"🤖 <b>ISAS CORE - Automatická detekcia</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"Servisný záznam bol uložený. Systém našiel {text_najdene}.\n\n"
                    f"⚠️ <b>Systém eviduje {text_eviduje}! Ktorú si práve teraz zrealizoval?</b>\n"
                )

                keyboard = []
                zobrazene_ulohy = ulohy_list[:5]
                
                for u in zobrazene_ulohy:
                    ikona_ulohy = "💡" if u['is_dnes'] else "⚠️"
                    
                    smart_text += (
                        f"\n{ikona_ulohy} <b>Úloha:</b> {u['defstr']}\n"
                    )
                    if u.get('info'):
                        smart_text += f"ℹ️ <b>Detail zadania:</b> <i>{u['info']}</i>\n"
                            
                    btn_text = f"✅ Uzavrieť #{u['id']} ({'Dnešná' if u['is_dnes'] else u['zaciatok_datum']})"
                    keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"close_task_{u['id']}")])
                    
                if len(ulohy_list) > 5:
                    smart_text += f"\n⚠️ <i>Zobrazených je len prvých 5 úloh z celkových {len(ulohy_list)}. Ostatné nájdeš v systéme.</i>\n"
                else:
                    smart_text += "\n<i>(Ostatné môžeš zatiaľ nechať otvorené)</i>"
                        
                keyboard.append([InlineKeyboardButton("❌ Ani jednu (Nechať otvorené)", callback_data="cancel_action")])
                    
                if len(smart_text) > 4000:
                    smart_text = smart_text[:4000] + "...\n<i>(Text bol skrátený kvôli limitu)</i>"
                        
                # 2. Pošleme Smart Matcher ako ÚPLNE NOVÚ SPRÁVU
                await context.bot.send_message(chat_id=chat_id, text=smart_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
                return ConversationHandler.END

            elif match_type and match_type.startswith("PARTIAL_MATCH_"):
                uloha = ulohy_list[0]
                parts = match_type.split("_")
                try:
                    pocet_celkom = int(parts[2]) if len(parts) > 2 else 2
                except (ValueError, TypeError):
                    pocet_celkom = 2
                pocet_zostava = pocet_celkom - 1  # Odpočítame položku, ktorú práve urobil
                dev_label = get_industry_term('zariadenie_fallback_nazov', 'zariadenie').lower()
                dev_genitiv = get_industry_term('zariadenie_genitiv', 'zariadenia').lower()

                if pocet_zostava == 1:
                    slovo_jednotky = f"1 {dev_label}"
                else:
                    slovo_jednotky = f"{pocet_zostava} ({dev_genitiv})"
                    
                smart_text = (
                    f"🤖 <b>ISAS CORE - Automatická detekcia</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"Servisný záznam bol uložený. Systém našiel otvorenú úlohu, ktorá s týmto záznamom súvisí.\n\n"
                    f"⚠️ <b>Táto položka bola súčasťou väčšej hromadnej úlohy. Zostáva ti v nej vyriešiť ešte {slovo_jednotky}. Úloha preto zatiaľ zostáva otvorená.</b>\n\n"
                    f"📌 <b>Úloha:</b> {uloha['defstr']}\n"
                )
                if uloha.get('info'):
                    smart_text += f"ℹ️ <b>Detail zadania:</b> <i>{uloha['info']}</i>\n"

                if len(smart_text) > 4000:
                    smart_text = smart_text[:4000] + "...\n<i>(Text bol skrátený kvôli limitu)</i>"

                # 2. Pošleme Smart Matcher ako ÚPLNE NOVÚ SPRÁVU
                await context.bot.send_message(chat_id=chat_id, text=smart_text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Návrat do menu", callback_data="cancel_action")]]), parse_mode='HTML')
                return ConversationHandler.END

            else:
                # Fallback pre NO_MATCH (Ak nenašiel úlohy, len pridáme tlačidlo Návrat do menu k prvej správe)
                await thinking_msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Návrat do menu", callback_data="cancel_action")]]))
                return ConversationHandler.END
        else:
            await thinking_msg.edit_text(f"❌ <b>Chyba pri ukladaní záznamu:</b>\n{sprava}", reply_markup=CANCEL_MARKUP, parse_mode='HTML')

    except Exception as e:
        chyba_text = html.escape(str(e).replace('<br>', '\n'))
        await thinking_msg.edit_text(f"❌ <b>Chyba pri spracovaní:</b>\n<code>{chyba_text}</code>", parse_mode='HTML')
            
    finally:
        # Absolútna garancia zmazania VŠETKÝCH dočasných médií z disku
        audio_p = audio_path if 'audio_path' in locals() else None
        f_pr = context.user_data.get('sh_foto_pred') if context.user_data else None
        f_po = context.user_data.get('sh_foto_po') if context.user_data else None
        v_vi = context.user_data.get('sh_video') if context.user_data else None
            
        for p in [audio_p, f_pr, f_po, v_vi]:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError as file_del_err:
                    logging.warning(f"Dočasný mediálny súbor '{p}' sa nepodarilo zmazať: {file_del_err}")

        if context.user_data is not None:
            for k in ['sh_foto_pred', 'sh_foto_po', 'sh_video']:
                context.user_data.pop(k, None)

    return ConversationHandler.END

async def sh_hlasovka_chyba(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Zachytí, ak technik pošle text/fotku namiesto hlasovky."""
    await posli_docasnu_chybu(update, context, "⚠️ <b>Chyba:</b> Musíš poslať hlasovú správu (Voice Message).", reply_markup=CANCEL_MARKUP)
    return SH_HLASOVKA

async def zacat_jazdu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return ConversationHandler.END
    await update.callback_query.answer()
    # Zistíme z tlačidla, či ide o FIREMNÚ alebo SÚKROMNÚ jazdu
    data = update.callback_query.data or ""
    typ_jazdy = "FIREMNA" if "firemna" in data else "SUKROMNA"
    if context.user_data is not None:
        context.user_data['j_typ'] = typ_jazdy
    
    vozidla = await get_vozidla_list()
    keyboard = [[InlineKeyboardButton(f"🚗 {v.spz} ({v.znacka_a_model})", callback_data=f"jauto_{v.id}")] for v in vozidla]
    keyboard.append([InlineKeyboardButton("❌ Zrušiť", callback_data="cancel_action")])
    
    nazov = "💼 FIREMNÁ JAZDA" if typ_jazdy == "FIREMNA" else "🏖️ SÚKROMNÁ JAZDA"
    await update.callback_query.edit_message_text(
        f"📍 <b>{nazov}</b>\n━━━━━━━━━━━━━━━━━━━━\nKtoré auto si si zobral?", 
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML'
    )
    return J_AUTO

async def j_km_start_vyzva(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return ConversationHandler.END
    await update.callback_query.answer()
    auto_id = (update.callback_query.data or "").replace("jauto_", "")
    if context.user_data is not None:
        context.user_data['j_auto'] = auto_id
    
    if not context.user_data or 'j_typ' not in context.user_data:
        await update.callback_query.edit_message_text("⚠️ Platnosť relácie vypršala. Začni znova.", reply_markup=CANCEL_MARKUP)
        return ConversationHandler.END
        
    auto = await sync_to_async(Vozidlo.objects.filter(id=auto_id).first)()
    if not auto:
        await update.callback_query.edit_message_text("⚠️ Vozidlo už neexistuje.", reply_markup=CANCEL_MARKUP)
        return ConversationHandler.END
        
    stav_km_display = auto.aktualny_stav_km if auto.aktualny_stav_km is not None else 0
    keyboard = [
        [InlineKeyboardButton(f"✅ Súhlasí ({stav_km_display} km)", callback_data=f"jkm_auto_{stav_km_display}")],
        [InlineKeyboardButton("✍️ Je iný (Napísať ručne)", callback_data="jkm_manual")],
        [InlineKeyboardButton("❌ Zrušiť", callback_data="cancel_action")]
    ]
    await update.callback_query.edit_message_text(
        f"🔢 Aký bol stav tachometra <b>PRI ODJAZDE</b>?\n\n<i>💡 ISAS CORE eviduje: <b>{stav_km_display}</b> km.</i>", 
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML'
    )
    return J_KM_START

async def j_km_start_tlacidlo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return ConversationHandler.END
    await update.callback_query.answer()
    data = update.callback_query.data or ""
    
    if data == "jkm_manual":
        await update.callback_query.edit_message_text("✍️ Napíš mi skutočný počiatočný stav tachometra (iba číslo):", reply_markup=CANCEL_MARKUP)
        return J_KM_START 
    
    km = data.replace("jkm_auto_", "")
    if context.user_data is not None:
        context.user_data['j_km_start'] = km
    await update.callback_query.edit_message_text(f"✅ <i>Štart: {km} km.</i>\n\n🏁 Aký je stav tachometra <b>PO PRÍCHODE</b> do cieľa?", reply_markup=CANCEL_MARKUP, parse_mode='HTML')
    return J_KM_KONIEC

async def j_km_start_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return J_KM_START
    if not context.user_data or 'j_auto' not in context.user_data: return J_KM_START # Ochrana proti spamu textom
    val = update.message.text.replace(' ', '')
    if not val.isdigit() or int(val) > 2000000:
        await posli_docasnu_chybu(update, context, "⚠️ <b>Chyba:</b> Počiatočný stav tachometra musí byť reálne celé číslo (max 2 000 000 km). Skús to znova:", reply_markup=CANCEL_MARKUP)
        return J_KM_START
    context.user_data['j_km_start'] = val
    await update.message.reply_text("🏁 Výborne. Aký je stav tachometra <b>PO PRÍCHODE</b> do cieľa?", reply_markup=CANCEL_MARKUP, parse_mode='HTML')
    return J_KM_KONIEC

async def j_ciel_vyzva(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return J_KM_KONIEC
    val = update.message.text.replace(' ', '')
    if not val.isdigit() or int(val) > 2000000:
        await posli_docasnu_chybu(update, context, "⚠️ <b>Chyba:</b> Konečný stav tachometra musí byť reálne celé číslo (max 2 000 000 km). Skús to znova:", reply_markup=CANCEL_MARKUP)
        return J_KM_KONIEC

    km_s = parse_int(context.user_data.get('j_km_start') if context.user_data else None, default=None)
    km_k = int(val)
    if km_s is not None and km_k <= km_s:
        await posli_docasnu_chybu(
            update, context, 
            f"⚠️ <b>Logická chyba:</b> Konečný stav ({km_k} km) nemôže byť menší alebo rovný počiatočnému ({km_s} km)! Zadaj platný konečný stav:", 
            reply_markup=CANCEL_MARKUP
        )
        return J_KM_KONIEC

    if context.user_data is not None:
        context.user_data['j_km_koniec'] = val
    keyboard = [
        [InlineKeyboardButton("🏢 Návrat na Hlavnú firmu", callback_data="jciel_HLAVNY")],
        [InlineKeyboardButton("🏭 Presun na našu Filiálku", callback_data="jciel_FILIALKA")],
        [InlineKeyboardButton("🤝 Cesta k Zákazníkovi", callback_data="jciel_ZAKAZNIK")],
        [InlineKeyboardButton("📍 Iné miesto (Ručne napísať)", callback_data="jciel_INE")],
        [InlineKeyboardButton("❌ Zrušiť", callback_data="cancel_action")]
    ]
    await update.message.reply_text("🗺️ <b>CIEĽ CESTY (Kam si dorazil?):</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return J_CIEL

async def j_ciel_spracuj(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query or context.user_data is None:
        return ConversationHandler.END
    await update.callback_query.answer()
    volba = (update.callback_query.data or "").replace("jciel_", "")
    
    if volba == "HLAVNY":
        adresa = await get_hlavny_sklad_adresa()
        context.user_data['j_ciel_text'] = adresa
        context.user_data['j_zakaznik_id'] = None
        if context.user_data.get('j_typ') == "SUKROMNA":
            context.user_data['j_ucel'] = "Súkromná jazda"
            return await j_finish(update, context)
        await update.callback_query.edit_message_text(f"🏢 <i>Cieľ nastavený: {adresa}</i>\n\n🛠️ <b>Účel cesty:</b>\nZadaj dôvod (napr. Návrat na základňu).", parse_mode='HTML')
        return J_UCEL
        
    elif volba == "FILIALKA":
        filialky = await get_zoznam_filialok_pre_bot()
        if not filialky:
            await update.callback_query.edit_message_text("⚠️ <i>Nemáte evidované žiadne aktívne filiálky. Zvoľte inú možnosť.</i>", reply_markup=CANCEL_MARKUP, parse_mode='HTML')
            return ConversationHandler.END
        keyboard = [[InlineKeyboardButton(f"🏭 {f['nazov']}", callback_data=f"sel_filialka_{f['id']}")] for f in filialky]
        keyboard.append([InlineKeyboardButton("❌ Zrušiť", callback_data="cancel_action")])
        await update.callback_query.edit_message_text("🏭 <b>Vyber si konkrétnu filiálku:</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        return J_CIEL_FILIALKA
        
    elif volba == "ZAKAZNIK":
        await update.callback_query.edit_message_text("🤝 <b>Napíš názov Zákazníka (alebo aspoň jeho časť):</b>", reply_markup=CANCEL_MARKUP, parse_mode='HTML')
        return J_CIEL_ZAKAZNIK_SEARCH
        
    elif volba == "INE":
        await update.callback_query.edit_message_text("📍 <b>Napíš presné miesto cieľa (Mesto, Ulica):</b>", reply_markup=CANCEL_MARKUP, parse_mode='HTML')
        return J_CIEL_TEXT

async def j_ciel_filialka_spracuj(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query or context.user_data is None:
        return ConversationHandler.END
    await update.callback_query.answer()
    filialka_id = int((update.callback_query.data or "").replace("sel_filialka_", ""))
    filialka = await sync_to_async(Filialka.objects.filter(id=filialka_id).first)()
    if not filialka:
        await update.callback_query.edit_message_text("⚠️ Zvolená filiálka už v systéme neexistuje.", reply_markup=CANCEL_MARKUP, parse_mode='HTML')
        return ConversationHandler.END
    context.user_data['j_ciel_text'] = f"Filiálka: {filialka.nazov}"
    context.user_data['j_zakaznik_id'] = None
    if context.user_data.get('j_typ') == "SUKROMNA":
        context.user_data['j_ucel'] = "Súkromná jazda"
        return await j_finish(update, context)
    await update.callback_query.edit_message_text(f"🏭 <i>Cieľ nastavený: {filialka.nazov}</i>\n\n🛠️ <b>Účel cesty:</b>\nZadaj dôvod výjazdu:", parse_mode='HTML')
    return J_UCEL

async def j_ciel_zakaznik_search_vyzva(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return J_CIEL_ZAKAZNIK_SEARCH
    hladany_text = update.message.text if update.message.text else ""
    zakaznici = await vyhladaj_zakaznika_pre_bot(hladany_text)
    
    if not zakaznici:
        await posli_docasnu_chybu(update, context, f"⚠️ Nenašiel som žiadneho zákazníka obsahujúceho '<b>{html.escape(hladany_text)}</b>'. Skús to znova:", reply_markup=CANCEL_MARKUP)
        return J_CIEL_ZAKAZNIK_SEARCH

    keyboard = [[InlineKeyboardButton(f"🏢 {z['Nazov_spolocnosti']}", callback_data=f"sel_zakaznik_{z['id']}")] for z in zakaznici]
    keyboard.append([InlineKeyboardButton("❌ Zrušiť", callback_data="cancel_action")])
    await update.message.reply_text("🤝 <b>Výsledky hľadania (Vyber zákazníka):</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return J_CIEL_ZAKAZNIK_SELECT

async def j_ciel_zakaznik_select_spracuj(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query or context.user_data is None:
        return ConversationHandler.END
    await update.callback_query.answer()
    zakaznik_id = int((update.callback_query.data or "").replace("sel_zakaznik_", ""))
    zakaznik = await sync_to_async(Zakaznik.objects.filter(id=zakaznik_id).first)()
    if not zakaznik:
        await update.callback_query.edit_message_text("⚠️ Zvolený zákazník už v systéme neexistuje.", reply_markup=CANCEL_MARKUP, parse_mode='HTML')
        return ConversationHandler.END
    context.user_data['j_ciel_text'] = f"Zákazník: {zakaznik.Nazov_spolocnosti}"
    context.user_data['j_zakaznik_id'] = zakaznik.id
    if context.user_data.get('j_typ') == "SUKROMNA":
        context.user_data['j_ucel'] = "Súkromná jazda"
        return await j_finish(update, context)
    await update.callback_query.edit_message_text(f"🏢 <i>Cieľ nastavený: {zakaznik.Nazov_spolocnosti}</i>\n\n🛠️ <b>Účel cesty:</b>\nZadaj dôvod výjazdu (napr. Oprava, Údržba):", parse_mode='HTML')
    return J_UCEL

async def j_ciel_text_spracuj(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text or context.user_data is None:
        return J_CIEL_TEXT
    context.user_data['j_ciel_text'] = update.message.text
    context.user_data['j_zakaznik_id'] = None
    if context.user_data.get('j_typ') == "SUKROMNA":
        context.user_data['j_ucel'] = "Súkromná jazda"
        return await j_finish(update, context)
    await update.message.reply_text("🛠️ <b>Účel cesty:</b>\nZadaj dôvod výjazdu:", reply_markup=CANCEL_MARKUP, parse_mode='HTML')
    return J_UCEL

async def j_ucel_vyzva(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text or context.user_data is None:
        return J_UCEL
    # Zachytenie textu z predchádzajúceho kroku (ak technik písal ručne)
    context.user_data['j_ciel_text'] = update.message.text
    
    if context.user_data.get('j_typ') == "SUKROMNA":
        context.user_data['j_ucel'] = "Súkromná jazda"
        return await j_finish(update, context) # Súkromná jazda preskakuje spolujazdcov
        
    await update.message.reply_text("🛠️ <b>Účel cesty:</b>\nZadaj dôvod výjazdu (napr. Oprava, Nákup).", reply_markup=CANCEL_MARKUP, parse_mode='HTML')
    return J_UCEL

async def j_spolujazdci_vyzva(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text or context.user_data is None:
        return J_UCEL
    context.user_data['j_ucel'] = update.message.text
    
    if context.user_data.get('j_typ') == "SUKROMNA":
        return await j_finish(update, context)

    def _get_kolegovia(u_id):
        qs = Zamestnanec.objects.filter(Q(typ_pomeru__in=['IN', 'KO']) | Q(pracovna_rola='TE'))
        if u_id:
            qs = qs.exclude(telegram_id=u_id)
        return list(qs.order_by('priezvisko', 'meno')[:20])

    user_id = update.effective_user.id if update.effective_user else None
    kolegovia = await sync_to_async(_get_kolegovia)(user_id)
    
    keyboard = [[InlineKeyboardButton("👤 Išiel som SÁM", callback_data="spolu_nikto")]]
    for k in kolegovia:
        keyboard.append([InlineKeyboardButton(f"👥 Išiel so mnou: {k.meno} {k.priezvisko}", callback_data=f"spolu_{k.id}")])
    keyboard.append([InlineKeyboardButton("❌ Zrušiť", callback_data="cancel_action")])

    await update.message.reply_text("👥 <b>SPOLUJAZDCI (Diéty)</b>\nBol s tebou v aute kolega?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return J_SPOLUJAZDEC

async def j_spolujazdec_spracuj(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query or context.user_data is None:
        return ConversationHandler.END
    await update.callback_query.answer()
    data = update.callback_query.data or ""
    if data != "spolu_nikto":
        context.user_data['j_spolujazdec_id'] = int(data.replace("spolu_", ""))
    else:
        context.user_data['j_spolujazdec_id'] = None

    domaci_stat, _ = await get_domaci_stat_a_mena_pre_bot()
    keyboard = [
        [InlineKeyboardButton(f"🏠 {domaci_stat} (Tuzemsko)", callback_data="jstat_DOMACI_STAT")],
        [InlineKeyboardButton("🌍 Zahraničie", callback_data="jstat_Zahraničie")],
        [InlineKeyboardButton("❌ Zrušiť", callback_data="cancel_action")]
    ]
    await update.callback_query.edit_message_text("🌍 <b>ŠTÁT CIEĽA (Diéty)</b>\n━━━━━━━━━━━━━━━━━━━━\nV akom štáte sa nachádzal cieľ cesty?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return J_STAT_CIELA

async def j_stat_ciela_spracuj(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query or context.user_data is None:
        return ConversationHandler.END
    await update.callback_query.answer()
    zvoleny_stat = (update.callback_query.data or "").replace("jstat_", "")
    if zvoleny_stat == "DOMACI_STAT":
        domaci_stat, _ = await get_domaci_stat_a_mena_pre_bot()
        context.user_data['j_stat_ciela'] = domaci_stat
    else:
        context.user_data['j_stat_ciela'] = zvoleny_stat

    keyboard = [
        [InlineKeyboardButton("❌ Nie, nič z toho", callback_data="jdiety_NIC")],
        [InlineKeyboardButton("🛏️ Iba Nocľah", callback_data="jdiety_NOCLAH")],
        [InlineKeyboardButton("🍽️ Iba Stravu (Obed/Večera)", callback_data="jdiety_STRAVA")],
        [InlineKeyboardButton("🛏️ + 🍽️ Nocľah aj Stravu", callback_data="jdiety_OBOJE")],
        [InlineKeyboardButton("❌ Zrušiť", callback_data="cancel_action")]
    ]
    await update.callback_query.edit_message_text("🏨 <b>NOCĽAH A STRAVA</b>\n━━━━━━━━━━━━━━━━━━━━\nMal si na tejto ceste zabezpečený bezplatný nocľah alebo stravu?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return J_NOCLAH_STRAVA

async def j_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sprava_obj = None
    data = "jdiety_NIC"
    if update.callback_query:
        await update.callback_query.answer()
        data = update.callback_query.data or "jdiety_NIC"
        cb_msg = update.callback_query.message
        if isinstance(cb_msg, Message):
            sprava_obj = cb_msg
    elif update.message:
        sprava_obj = update.message

    if not sprava_obj or not update.effective_user:
        chat_id = update.effective_chat.id if update.effective_chat else None
        if chat_id:
            await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ <b>Relácia vypršala!</b>",
                reply_markup=CANCEL_MARKUP,
                parse_mode='HTML'
            )
        return ConversationHandler.END

    # Ochrana pred stratou dát relácie (Bot restart / session timeout)
    if not context.user_data or 'j_auto' not in context.user_data:
        await sprava_obj.reply_text(
            "⚠️ <b>Relácia vypršala!</b>\nZadaj prosím jazdu znova od začiatku z hlavného menu.",
            reply_markup=CANCEL_MARKUP,
            parse_mode='HTML'
        )
        return ConversationHandler.END

    noclah = data in ["jdiety_NOCLAH", "jdiety_OBOJE"]
    strava = data in ["jdiety_STRAVA", "jdiety_OBOJE"]
    stat_ciela = context.user_data.get('j_stat_ciela', None)
    spolujazdec_id = context.user_data.get('j_spolujazdec_id', None)

    success, msg = await ulozit_jazdu_db(
        update.effective_user.id,
        context.user_data.get('j_auto'),
        context.user_data.get('j_typ', 'FIREMNA'),
        context.user_data.get('j_km_start'),
        context.user_data.get('j_km_koniec'),
        context.user_data.get('j_ciel_text', 'Neznámy cieľ'),
        context.user_data.get('j_ucel', 'Neuvedený účel'),
        context.user_data.get('j_zakaznik_id'),
        spolujazdec_id,
        stat_ciela,
        noclah,
        strava
    )
    
    text = f"✅ <b>JAZDA ULOŽENÁ!</b>\n━━━━━━━━━━━━━━━━━━━━\n{msg}" if success else f"{msg}\n\n<i>Začni proces znova.</i>"
    
    # Kompletné prečistenie kľúčov jazdy z pamäte
    for k in ['j_auto', 'j_typ', 'j_km_start', 'j_km_koniec', 'j_ciel_text', 'j_ucel', 'j_zakaznik_id', 'j_spolujazdec_id', 'j_stat_ciela']:
        context.user_data.pop(k, None)
    
    await sprava_obj.reply_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Návrat do menu", callback_data="cancel_action")]]), parse_mode='HTML')
    return ConversationHandler.END

async def zacat_tankovanie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return ConversationHandler.END
    vozidla = await get_vozidla_list()
    keyboard = [[InlineKeyboardButton(f"⛽ {v.spz}", callback_data=f"tauto_{v.id}")] for v in vozidla]
    keyboard.append([InlineKeyboardButton("❌ Zrušiť", callback_data="cancel_action")])
    await update.callback_query.edit_message_text("⛽ <b>TANKOVANIE</b>\n━━━━━━━━━━━━━━━━━━━━\nKtoré auto si natankoval?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return T_AUTO

async def t_km(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return ConversationHandler.END
    if context.user_data is not None:
        context.user_data['t_auto'] = (update.callback_query.data or "").replace("tauto_", "")
    # Pomoc pre technika - uisťujeme sa, že zadá číslo
    await update.callback_query.edit_message_text("🔢 Aký je aktuálny <b>stav tachometra</b> (km) pri tankovaní?\n\n<i>(Zadaj iba celé číslo, napr. 145000)</i>", reply_markup=CANCEL_MARKUP, parse_mode='HTML')
    return T_KM

async def t_litre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return T_KM
    val = update.message.text.replace(' ', '')
    if not val.isdigit():
        await posli_docasnu_chybu(update, context, "⚠️ <b>Chyba:</b> Stav tachometra musí byť celé číslo. Skús to znova:", reply_markup=CANCEL_MARKUP)
        return T_KM
    
    if context.user_data is not None:
        context.user_data['t_km'] = val
    await update.message.reply_text("💧 Koľko <b>litrov</b> si natankoval? (napr. 52.4):", reply_markup=CANCEL_MARKUP, parse_mode='HTML')
    return T_LITRE

async def t_cena(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return T_LITRE
    val = update.message.text
    c_val = val.replace(",", ".").replace(' ', '') if val else ""
    dec_litre = parse_decimal(c_val, default=None)
    if dec_litre is None or dec_litre <= Decimal('0.00'):
        await posli_docasnu_chybu(update, context, "⚠️ <b>Chyba:</b> Počet litrov musí byť kladné číslo väčšie ako nula (napr. 52.4). Skús to znova:", reply_markup=CANCEL_MARKUP)
        return T_LITRE
        
    if context.user_data is not None:
        context.user_data['t_litre'] = c_val
    _, mena_firmy = await get_domaci_stat_a_mena_pre_bot()
    await update.message.reply_text(f"💳 Aká bola <b>celková cena</b> s DPH ({mena_firmy})? (napr. 65.20):", reply_markup=CANCEL_MARKUP, parse_mode='HTML')
    return T_CENA

async def t_platba(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return T_CENA
    val = update.message.text
    c_val = val.replace(",", ".").replace(' ', '') if val else ""

    _, mena_firmy = await get_domaci_stat_a_mena_pre_bot()
    dec_cena = parse_decimal(c_val, default=None)
    if dec_cena is None or dec_cena <= Decimal('0.00'):
        await posli_docasnu_chybu(update, context, f"⚠️ <b>Chyba:</b> Celková cena musí byť kladná suma v mene {mena_firmy} (napr. 65.20). Skús to znova:", reply_markup=CANCEL_MARKUP)
        return T_CENA
        
    if context.user_data is not None:
        context.user_data['t_cena'] = c_val
    keyboard = [
        [InlineKeyboardButton("💳 Palivová / Fleetová karta", callback_data="pay_PALIVOVA")],
        [InlineKeyboardButton("📱 Banková platobná karta", callback_data="pay_PLATOBNA")],
        [InlineKeyboardButton("💵 Hotovosť", callback_data="pay_HOTOVOST")],
        [InlineKeyboardButton("❌ Zrušiť", callback_data="cancel_action")]
    ]
    await update.message.reply_text("💳 Vyber spôsob platby:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return T_PLATBA

async def t_platba_spracuj(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Zachytenie platby. Tu systém rozhodne, či sa pýta na pumpu, alebo to preskočí."""
    if not update.callback_query or not update.effective_user or context.user_data is None:
        return ConversationHandler.END
    await update.callback_query.answer()
    context.user_data['t_platba'] = (update.callback_query.data or "").replace("pay_", "")
    
    tg_id = update.effective_user.id
    stanica_id, stanica_nazov = await get_predvolena_cerpacka(tg_id)
    
    if stanica_id:
        context.user_data['t_stanica_id'] = stanica_id
        await update.callback_query.edit_message_text(
            f"⛽ <i>Použitá tvoja predvolená ČS: <b>{stanica_nazov}</b></i>\n\n"
            f"📸 Teraz odfotografuj bloček a pošli mi ho sem:", 
            reply_markup=CANCEL_MARKUP, parse_mode='HTML'
        )
        return T_FOTO
    else:
        stanice = await get_zoznam_cerpaciek()
        keyboard = []
        for s in stanice:
            keyboard.append([InlineKeyboardButton(f"⛽ {s['nazov']}", callback_data=f"stanica_{s['id']}_save")])
            
        keyboard.append([InlineKeyboardButton("🔍 Iná čerpacia stanica (Vyhľadať)", callback_data="stanica_hladat")])
        keyboard.append([InlineKeyboardButton("❌ Zrušiť", callback_data="cancel_action")])
        
        await update.callback_query.edit_message_text(
            "🏢 <b>Kde si tankoval?</b>\n"
            "<i>(Zvolená možnosť sa ti uloží ako predvolená)</i>",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML'
        )
        return T_STANICA

async def t_stanica_hladat_vyzva(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ak technik nenašiel svoju pumpu, vyzveme ho, nech napíše názov."""
    if not update.callback_query:
        return ConversationHandler.END
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        "🔍 <b>VYHĽADÁVANIE ČERPACEJ STANICE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Napíš mi aspoň časť názvu dodávateľa (napríklad 'Shell' alebo 'Tesco'):",
        reply_markup=CANCEL_MARKUP, parse_mode='HTML'
    )
    return T_STANICA_SEARCH

async def t_stanica_search_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Zoberieme text, čo technik napísal, a prehľadáme tabuľku Dodavatel cez dedikovanú funkciu."""
    if not update.message:
        return T_STANICA_SEARCH
    hladany_text = update.message.text if update.message.text else ""
    dodavatelia = await vyhladaj_cerpacku(hladany_text)
    
    if not dodavatelia:
        await posli_docasnu_chybu(update, context, f"⚠️ Nenašiel som žiadneho dodávateľa obsahujúceho '<b>{html.escape(hladany_text)}</b>'.\nSkús napísať iný názov:", reply_markup=CANCEL_MARKUP)
        return T_STANICA_SEARCH

    keyboard = [
        [InlineKeyboardButton(f"⛽ {d['nazov']}", callback_data=f"stanica_{d['id']}_save")]
        for d in dodavatelia
    ]
    keyboard.append([InlineKeyboardButton("❌ Zrušiť", callback_data="cancel_action")])
    
    await update.message.reply_text(
        "🔍 <b>VÝSLEDKY VYHĽADÁVANIA:</b>\nVyber si zo zoznamu nižšie:",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML'
    )
    return T_STANICA

async def t_stanica_spracuj(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Zachytenie výberu pumpy a uloženie predvoľby."""
    if not update.callback_query or not update.effective_user:
        return ConversationHandler.END
    await update.callback_query.answer()
    data = (update.callback_query.data or "").replace("stanica_", "").split("_")
    stanica_id = int(data[0])
    
    # Uložíme výber do RAM bota pre tento bloček
    if context.user_data is not None:
        context.user_data['t_stanica_id'] = stanica_id
    
    # Uložíme to trvalo do databázy ako jeho default
    nazov = await set_predvolena_cerpacka(update.effective_user.id, stanica_id)
    nazov_zobrazenie = nazov or "Zvolená stanica"
    
    await update.callback_query.edit_message_text(
        f"✅ <i>Stanica <b>{nazov_zobrazenie}</b> bola uložená ako tvoja predvolená.</i>\n\n"
        f"📸 Teraz odfotografuj bloček a pošli mi ho sem:", 
        reply_markup=CANCEL_MARKUP, parse_mode='HTML'
    )
    return T_FOTO

async def t_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user or not update.effective_chat:
        return ConversationHandler.END

    if not update.message.photo and not update.message.document:
        await posli_docasnu_chybu(update, context, "⚠️ <b>Chyba:</b> Prosím, odfoť alebo pošli fotografiu bločku (JPG/PNG).", reply_markup=CANCEL_MARKUP)
        return T_FOTO

    # Ak je vstup správny, upraceme prípadnú starú chybovú hlášku cez centrálny GC
    await vymaz_docasne_spravy(context, update.effective_chat.id, keys=['err_msg'])

    # Zaistenie stiahnutia súboru bez pádu bota
    try:
        if update.message.photo:
            file_obj = await update.message.photo[-1].get_file()
        elif update.message.document:
            file_obj = await update.message.document.get_file()

        path = f"temp_tankovanie_{update.effective_user.id}_{uuid.uuid4().hex[:8]}.jpg"
        await file_obj.download_to_drive(path)
        
        try:
            if not context.user_data or 't_auto' not in context.user_data:
                await update.message.reply_text(
                    "⚠️ <b>Relácia vypršala!</b>\nZačni evidenciu tankovania znova z hlavného menu.",
                    reply_markup=CANCEL_MARKUP,
                    parse_mode='HTML'
                )
                return ConversationHandler.END

            stanica_id = context.user_data.get('t_stanica_id')

            success, upozornenie = await ulozit_tankovanie_db(
                update.effective_user.id, 
                context.user_data.get('t_auto'), 
                context.user_data.get('t_km'), 
                context.user_data.get('t_litre'), 
                context.user_data.get('t_cena'), 
                context.user_data.get('t_platba'), 
                stanica_id,
                path
            )
        finally:
            # BEZPEČNOSŤ DEVOPS: Vždy zmazať temp súbor s korektným odchytením OSError
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError as err:
                    logging.warning(f"Dočasný bloček {path} sa nepodarilo zmazať: {err}")
        
        if success:
            text = "✅ <b>TANKOVANIE ULOŽENÉ</b>\n━━━━━━━━━━━━━━━━━━━━\nBloček a dáta boli bezpečne archivované do ISAS CORE."
            if upozornenie:
                text += f"\n{upozornenie}"
        else:
            text = f"{upozornenie}\n\n<i>Začni proces evidencie tankovania znova z menu.</i>"

        if context.user_data is not None:
            for k in ['t_auto', 't_km', 't_litre', 't_cena', 't_platba', 't_stanica_id']:
                context.user_data.pop(k, None)
            
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Hlavné menu", callback_data="cancel_action")]]), parse_mode='HTML')
    
    except Exception as e:
        await update.message.reply_text(f"❌ <b>Nastala chyba pri ukladaní bločku:</b>\n{str(e)}\n\nSkús to znova, prosím.", parse_mode='HTML')
        return T_FOTO

    return ConversationHandler.END

async def start_ai_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query or not update.effective_user or context.user_data is None:
        return ConversationHandler.END
    context.user_data['ai_history'] = "" 
    tg_id = update.effective_user.id
    
    obj, typ = await identifikuj_pouzivatela(tg_id)
    
    if typ == "ZAMESTNANEC" and obj:
        django_user = getattr(obj, 'user', None)
        is_superuser = getattr(django_user, 'is_superuser', False)
        
        if is_superuser or obj.typ_pomeru == 'KO' or obj.pracovna_rola == 'AD':
            text = (
                "🧠 <b>ISAS CORE AI</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                "👋 <b>Ahoj šéfe!</b> Som tvoj elitný asistent pre riadenie firmy.\n\n"
                "Vidím do všetkých modulov a som pripravený na hĺbkovú analýzu:\n"
                "💰 <b>Financie:</b> Cash-flow, pohľadávky, faktúry, ERP.\n"
                "🤝 <b>Obchod:</b> Ziskovosť, dodávatelia, ponuky, objednávky.\n"
                f"⚙️ <b>Operatíva:</b> Úlohy, SLA reklamácie, {get_industry_term('zariadenia_mnozno', 'zariadenia')}, predikcie.\n"
                "📦 <b>Sklad:</b> Stavy, pohyby, košík, výpočet trás.\n"
                "👨‍🔧 <b>HR a Fleet:</b> Mzdy, dovolenky, tankovania, STK.\n"
                "🧠 <b>Systém:</b> Čítanie PDF, schémy, maily, štatistiky.\n\n"
                "<i>Čo dnes vyriešime? (Napíš text, pošli hlasovku alebo fotku)</i> 🚀"
            )
        elif obj.pracovna_rola == 'DI':
            text = (
                "🧠 <b>ISAS CORE AI</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                "👋 <b>Ahoj!</b> Som tvoj asistent pre riadenie operatívy a dispečingu.\n\n"
                "Mám prehľad o dianí vo firme:\n"
                f"⚙️ <b>Operatíva:</b> Úlohy, SLA reklamácie, {get_industry_term('zariadenia_mnozno', 'zariadenia')}, vstupy.\n"
                "📦 <b>Sklad:</b> Stavy skladu, pohyby, výpočet trás.\n"
                "🚚 <b>Fleet:</b> Knihy jázd, tankovania, STK, PZP.\n"
                "🧠 <b>Systém:</b> Čítanie PDF, schémy, maily.\n\n"
                "<i>S čím ti môžem pomôcť? (Napíš text, pošli hlasovku alebo fotku)</i> 🚀"
            )
        elif obj.pracovna_rola == 'OB':
            text = (
                "🧠 <b>ISAS CORE AI</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                "👋 <b>Ahoj!</b> Som tvoj obchodný asistent.\n\n"
                "Pomôžem ti pri príprave obchodných prípadov:\n"
                "🤝 <b>Obchod a CRM:</b> Zákazníci, cenové ponuky a stav objednávok.\n"
                "📦 <b>Sklad:</b> Dostupnosť tovaru, predajné ceny a výpočet trás.\n"
                f"⚙️ <b>Technika:</b> Parametre ({get_industry_term('zariadenie_genitiv', 'zariadenia')}) a katalógy.\n"
                "🚚 <b>Fleet:</b> Moje jazdy a tankovania.\n\n"
                "<i>S čím ti môžem pomôcť? (Napíš text, pošli hlasovku alebo fotku)</i> 🚀"
            )
        else:
            text = (
                "🧠 <b>ISAS CORE AI</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                "👋 <b>Ahoj!</b> Som tvoj technický asistent.\n\n"
                "Som tu, aby som ti uľahčil prácu:\n"
                f"🛠️ <b>Moja práca:</b> Pracovné postupy (SOP), technické detaily ({get_industry_term('zariadenie_genitiv', 'zariadenia')}).\n"
                "📦 <b>Sklad:</b> Dostupnosť dielov, výpočet trás.\n"
                "🚚 <b>Fleet:</b> Moje jazdy a tankovania.\n"
                "🧠 <b>Systém:</b> Čítanie PDF manuálov a schém.\n\n"
                "<i>Čo pre teba môžem zistiť? (Napíš text, pošli hlasovku alebo fotku)</i> 🚀"
            )
    elif typ == "SUKROMNA" or (obj and getattr(obj, 'typ_pomeru', '') == 'Súkromná osoba'):
        text = (
            "🧠 <b>ISAS CORE AI</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            "👋 <b>Ahoj!</b>\n\n"
            "Tvoj profil ťa oprávňuje výhradne na správu tvojho vozidla:\n"
            "🚗 <b>Jazdy:</b> Evidencia súkromných a firemných jázd.\n"
            "⛽ <b>Tankovanie:</b> Evidencia tankovania a nákladov.\n"
            "📍 <b>Nastavenia:</b> Predvolená čerpacia stanica.\n\n"
            "<i>Čo pre teba môžem zaznamenať? (Napíš text alebo pošli hlasovku)</i> 🚀"
        )
    else:
        text = (
            "🧠 <b>ISAS CORE AI</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            "👋 <b>Ahoj!</b>\n\n"
            "Tvoj používateľský účet nemá priradený profil zamestnanca alebo súkromnej osoby. Moje funkcie sú pre teba momentálne obmedzené.\n\n"
            "<i>Ak potrebuješ plný prístup, kontaktuj administrátora.</i> 🚀"
        )

    await update.callback_query.edit_message_text(
        text, 
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Ukončiť chat", callback_data="cancel_action")]]), 
        parse_mode='HTML'
    )
    return AI_CHAT

async def posli_bezpecnu_ai_odpoved_tg(update: Update, status_msg, odpoved_text: str):
    """
    🛡️ ISAS BULLETPROOF SENDER:
    1. Delí správy nad 3900 znakov (ochrana pred limitom 4096 znakov).
    2. Zachytáva 'Can't parse entities' chyby (ak AI vráti nevalidné HTML) a odošle správu v čistom texte.
    """
    if not update.message:
        return
    cisty_text = odpoved_text.replace('<br>', '\n')
    cancel_btn = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Ukončiť chat", callback_data="cancel_action")]])
    chunks = [cisty_text[i:i+3900] for i in range(0, len(cisty_text), 3900)] if len(cisty_text) > 3900 else [cisty_text]

    try:
        await status_msg.edit_text(chunks[0], reply_markup=cancel_btn if len(chunks) == 1 else None, parse_mode='HTML')
    except BadRequest:
        # Fallback pri nevalidných HTML tagoch od AI
        await status_msg.edit_text(chunks[0], reply_markup=cancel_btn if len(chunks) == 1 else None)

    for idx, chunk in enumerate(chunks[1:], start=1):
        is_last = (idx == len(chunks) - 1)
        try:
            await update.message.reply_text(chunk, reply_markup=cancel_btn if is_last else None, parse_mode='HTML')
        except BadRequest:
            await update.message.reply_text(chunk, reply_markup=cancel_btn if is_last else None)

async def handle_ai_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user or not update.message.text or context.user_data is None:
        return AI_CHAT
    msg = await update.message.reply_text("🤔 <i>Spracovávam...</i>", parse_mode='HTML')
    tg_id = update.effective_user.id
    lock_key = f"ai_lock_tg_{tg_id}"

    if cache.get(lock_key):
        await msg.edit_text("⏳ <b>Stále premýšľam nad predchádzajúcou otázkou.</b>", parse_mode='HTML')
        return AI_CHAT

    ai_ctx = None
    try:
        cache.set(lock_key, True, timeout=120)
        ai_ctx = await ziskaj_ai_user_kontext(tg_id)
        if not ai_ctx:
            raise Exception("Neautorizovaný prístup.")

        dnes = timezone.now().date()
        ma_db_profil = bool(ai_ctx['user_kwargs'].get('pouzivatel') or ai_ctx['user_kwargs'].get('sukromna_osoba'))
        if ma_db_profil:
            pouzite_dnes = await sync_to_async(AIAuditLog.objects.filter(**ai_ctx['user_kwargs'], cas_dopytu__date=dnes).count)()
        else:
            pouzite_dnes = cache.get(f"ai_limit_tg_{tg_id}_{dnes}", 0)

        if pouzite_dnes >= ai_ctx['denny_limit']:
            await msg.edit_text(f"🛑 <b>Dosiahli ste denný limit ({ai_ctx['denny_limit']} správ).</b>", parse_mode='HTML')
            return AI_CHAT

        if not ma_db_profil:
            cache.set(f"ai_limit_tg_{tg_id}_{dnes}", pouzite_dnes + 1, timeout=86400)

        audit_log = await sync_to_async(AIAuditLog.objects.create)(**ai_ctx['user_kwargs'], otazka=update.message.text, zdroj_poziadavky='TELEGRAM')

        res_dict = await sync_to_async(ask_isas_ai)(
            user_question=update.message.text, 
            history_text=context.user_data.get('ai_history', ""),
            aktualny_pouzivatel=ai_ctx['aktualny_pouzivatel'], 
            rola_pouzivatela=ai_ctx['rola_pouzivatela'], 
            typ_pomeru=ai_ctx['typ_pomeru'],
            is_superuser=ai_ctx['is_superuser'],
            ma_pristup_k_financiam=ai_ctx['ma_pristup_k_financiam'],
            subjekt_id=ai_ctx.get('subjekt_id')
        )
        odpoved_text = res_dict.get("text", "Bez odpovede.")

        in_tokens, out_tokens = res_dict.get("in_tokens", 0), res_dict.get("out_tokens", 0)
        cached_tokens = res_dict.get("cached_tokens", 0)
        audit_log.vstupne_tokeny, audit_log.vystupne_tokeny = in_tokens, out_tokens
        audit_log.presna_cena_usd = Decimal(str(vypocitaj_ai_cenu_usd(in_tokens, out_tokens, cached_tokens=cached_tokens) or 0)).quantize(Decimal('0.000001'))
        audit_log.odpoved = odpoved_text
        audit_log.uspesne_dokoncene = True
        await sync_to_async(audit_log.save)()

        pouzivatel_label = ai_ctx.get('aktualny_pouzivatel') or "Používateľ"
        nova_historia = context.user_data.get('ai_history', "") + f"\n{pouzivatel_label}: {update.message.text}\nAI: {odpoved_text}"
        context.user_data['ai_history'] = nova_historia[-3000:]

        await posli_bezpecnu_ai_odpoved_tg(update, msg, odpoved_text)
    except Exception as e:
        detail_html = f"\n<i>Detail: {safe_tg(str(e)[:300])}</i>" if (ai_ctx and ai_ctx.get('typ') == 'ZAMESTNANEC') else ""
        await msg.edit_text(f"❌ <b>AI modul zlyhal.</b>{detail_html}", reply_markup=CANCEL_MARKUP, parse_mode='HTML')
    finally:
        cache.delete(lock_key)
    return AI_CHAT

async def handle_ai_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user or not update.message.voice or context.user_data is None:
        return AI_CHAT
    msg = await update.message.reply_text("🎧 <i>Počúvam...</i>", parse_mode='HTML')
    tg_id = update.effective_user.id
    lock_key = f"ai_lock_tg_{tg_id}"

    if cache.get(lock_key):
        await msg.edit_text("⏳ <b>Stále premýšľam nad predchádzajúcou otázkou.</b>", parse_mode='HTML')
        return AI_CHAT

    path = f"ai_voice_{tg_id}_{uuid.uuid4().hex[:8]}.ogg"
    ai_ctx = None

    try:
        cache.set(lock_key, True, timeout=120)
        ai_ctx = await ziskaj_ai_user_kontext(tg_id)
        if not ai_ctx:
            raise Exception("Neautorizovaný prístup.")

        dnes = timezone.now().date()
        ma_db_profil = bool(ai_ctx['user_kwargs'].get('pouzivatel') or ai_ctx['user_kwargs'].get('sukromna_osoba'))
        if ma_db_profil:
            pouzite_dnes = await sync_to_async(AIAuditLog.objects.filter(**ai_ctx['user_kwargs'], cas_dopytu__date=dnes).count)()
        else:
            pouzite_dnes = cache.get(f"ai_limit_tg_{tg_id}_{dnes}", 0)

        if pouzite_dnes >= ai_ctx['denny_limit']:
            await msg.edit_text(f"🛑 <b>Dosiahli ste denný limit ({ai_ctx['denny_limit']} správ).</b>", parse_mode='HTML')
            return AI_CHAT

        voice_file = await update.message.voice.get_file()
        await voice_file.download_to_drive(path)

        if not ma_db_profil:
            cache.set(f"ai_limit_tg_{tg_id}_{dnes}", pouzite_dnes + 1, timeout=86400)

        audit_log = await sync_to_async(AIAuditLog.objects.create)(**ai_ctx['user_kwargs'], otazka="[Hlasová správa]", zdroj_poziadavky='TELEGRAM')

        res_dict = await sync_to_async(ask_isas_ai)(
            user_question=None, history_text=context.user_data.get('ai_history', ""), audio_path=path,
            aktualny_pouzivatel=ai_ctx['aktualny_pouzivatel'], rola_pouzivatela=ai_ctx['rola_pouzivatela'], typ_pomeru=ai_ctx['typ_pomeru'],
            is_superuser=ai_ctx['is_superuser'], ma_pristup_k_financiam=ai_ctx['ma_pristup_k_financiam'],
            subjekt_id=ai_ctx.get('subjekt_id')
        )
        odpoved_text = res_dict.get("text", "Bez odpovede.")

        in_tokens, out_tokens = res_dict.get("in_tokens", 0), res_dict.get("out_tokens", 0)
        cached_tokens = res_dict.get("cached_tokens", 0)
        audit_log.vstupne_tokeny, audit_log.vystupne_tokeny = in_tokens, out_tokens
        audit_log.presna_cena_usd = Decimal(str(vypocitaj_ai_cenu_usd(in_tokens, out_tokens, cached_tokens=cached_tokens) or 0)).quantize(Decimal('0.000001'))
        audit_log.odpoved = odpoved_text
        audit_log.uspesne_dokoncene = True
        await sync_to_async(audit_log.save)()

        pouzivatel_label = ai_ctx.get('aktualny_pouzivatel') or "Používateľ"
        nova_historia = context.user_data.get('ai_history', "") + f"\n{pouzivatel_label}: [Príloha/Hlasová správa]\nAI: {odpoved_text}"
        context.user_data['ai_history'] = nova_historia[-3000:]

        await posli_bezpecnu_ai_odpoved_tg(update, msg, odpoved_text)
    except Exception as e:
        detail_html = f"\n<i>Detail: {safe_tg(str(e)[:300])}</i>" if (ai_ctx and ai_ctx.get('typ') == 'ZAMESTNANEC') else ""
        await msg.edit_text(f"❌ <b>AI modul zlyhal.</b>{detail_html}", reply_markup=CANCEL_MARKUP, parse_mode='HTML')
    finally:
        cache.delete(lock_key)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
    return AI_CHAT

async def handle_ai_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user or not update.message.photo or context.user_data is None:
        return AI_CHAT
    tg_id = update.effective_user.id
    obj, typ = await identifikuj_pouzivatela(tg_id)
    
    # 🛡️ RBAC OCHRANA: Multimodálne prílohy môžu posielať výhradne zamestnanci
    if typ != "ZAMESTNANEC":
        await update.message.reply_text("⚠️ <b>Prístup zamietnutý:</b> Analýza fotografií a dokumentov je dostupná výhradne pre zamestnancov firmy.", parse_mode='HTML')
        return AI_CHAT

    msg = await update.message.reply_text("👁️ <i>Analyzujem obrázok...</i>", parse_mode='HTML')
    lock_key = f"ai_lock_tg_{tg_id}"

    if cache.get(lock_key):
        await msg.edit_text("⏳ <b>Stále premýšľam nad predchádzajúcou otázkou.</b>", parse_mode='HTML')
        return AI_CHAT

    path = f"ai_photo_{tg_id}_{uuid.uuid4().hex[:8]}.jpg"
    user_text = update.message.caption or "Analyzuj tento obrázok a poskytni podrobné informácie v kontexte našej firmy."
    ai_ctx = None

    try:
        cache.set(lock_key, True, timeout=120)
        ai_ctx = await ziskaj_ai_user_kontext(tg_id)
        if not ai_ctx:
            raise Exception("Neautorizovaný prístup.")

        dnes = timezone.now().date()
        ma_db_profil = bool(ai_ctx['user_kwargs'].get('pouzivatel') or ai_ctx['user_kwargs'].get('sukromna_osoba'))
        if ma_db_profil:
            pouzite_dnes = await sync_to_async(AIAuditLog.objects.filter(**ai_ctx['user_kwargs'], cas_dopytu__date=dnes).count)()
        else:
            pouzite_dnes = cache.get(f"ai_limit_tg_{tg_id}_{dnes}", 0)

        if pouzite_dnes >= ai_ctx['denny_limit']:
            await msg.edit_text(f"🛑 <b>Dosiahli ste denný limit ({ai_ctx['denny_limit']} správ).</b>", parse_mode='HTML')
            return AI_CHAT

        photo_file = await update.message.photo[-1].get_file()
        await photo_file.download_to_drive(path)

        if not ma_db_profil:
            cache.set(f"ai_limit_tg_{tg_id}_{dnes}", pouzite_dnes + 1, timeout=86400)

        audit_log = await sync_to_async(AIAuditLog.objects.create)(**ai_ctx['user_kwargs'], otazka=f"[Fotografia] {user_text}", zdroj_poziadavky='TELEGRAM')

        res_dict = await sync_to_async(ask_isas_ai)(
            user_question=user_text, 
            history_text=context.user_data.get('ai_history', ""), 
            file_paths=[path],
            aktualny_pouzivatel=ai_ctx['aktualny_pouzivatel'], 
            rola_pouzivatela=ai_ctx['rola_pouzivatela'], 
            typ_pomeru=ai_ctx['typ_pomeru'],
            is_superuser=ai_ctx['is_superuser'],
            ma_pristup_k_financiam=ai_ctx['ma_pristup_k_financiam'],
            subjekt_id=ai_ctx.get('subjekt_id')
        )
        odpoved_text = res_dict.get("text", "Bez odpovede.")

        in_tokens, out_tokens = res_dict.get("in_tokens", 0), res_dict.get("out_tokens", 0)
        cached_tokens = res_dict.get("cached_tokens", 0)
        audit_log.vstupne_tokeny, audit_log.vystupne_tokeny = in_tokens, out_tokens
        audit_log.presna_cena_usd = Decimal(str(vypocitaj_ai_cenu_usd(in_tokens, out_tokens, cached_tokens=cached_tokens) or 0)).quantize(Decimal('0.000001'))
        audit_log.odpoved = odpoved_text
        audit_log.uspesne_dokoncene = True
        await sync_to_async(audit_log.save)()

        pouzivatel_label = ai_ctx.get('aktualny_pouzivatel') or "Používateľ"
        nova_historia = context.user_data.get('ai_history', "") + f"\n{pouzivatel_label}: [Fotografia] {user_text}\nAI: {odpoved_text}"
        context.user_data['ai_history'] = nova_historia[-3000:]

        await posli_bezpecnu_ai_odpoved_tg(update, msg, odpoved_text)
    except Exception as e:
        detail_html = f"\n<i>Detail: {safe_tg(str(e)[:300])}</i>" if (ai_ctx and ai_ctx.get('typ') == 'ZAMESTNANEC') else ""
        await msg.edit_text(f"❌ <b>AI modul zlyhal.</b>{detail_html}", reply_markup=CANCEL_MARKUP, parse_mode='HTML')
    finally:
        cache.delete(lock_key)
        if os.path.exists(path): 
            try: os.remove(path)
            except Exception: pass
    return AI_CHAT

async def handle_ai_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """📎 Spracovanie dokumentov (PDF, DOCX, XLSX, TXT) a videí v AI chate."""
    if not update.message or not update.effective_user or context.user_data is None:
        return AI_CHAT
    tg_id = update.effective_user.id
    obj, typ = await identifikuj_pouzivatela(tg_id)
    
    if typ != "ZAMESTNANEC":
        await update.message.reply_text("⚠️ <b>Prístup zamietnutý:</b> Analýza dokumentov je dostupná výhradne pre zamestnancov firmy.", parse_mode='HTML')
        return AI_CHAT

    doc = update.message.document or update.message.video
    if not doc:
        return AI_CHAT

    # Limit 25 MB
    if doc.file_size and doc.file_size > 25 * 1024 * 1024:
        await update.message.reply_text("🛑 <b>Súbor je príliš veľký.</b> Maximálna povolená veľkosť je 25 MB.", parse_mode='HTML')
        return AI_CHAT

    msg = await update.message.reply_text("📄 <i>Sťahujem a analyzujem dokument...</i>", parse_mode='HTML')
    lock_key = f"ai_lock_tg_{tg_id}"

    if cache.get(lock_key):
        await msg.edit_text("⏳ <b>Stále premýšľam nad predchádzajúcou otázkou.</b>", parse_mode='HTML')
        return AI_CHAT

    orig_name = getattr(doc, 'file_name', None) or ("video.mp4" if update.message.video else "dokument.pdf")
    ext = os.path.splitext(orig_name)[1].lower() or ".pdf"
    path = f"ai_doc_{tg_id}_{uuid.uuid4().hex[:8]}{ext}"
    user_text = update.message.caption or f"Analyzuj priložený súbor ({orig_name}) a poskytni k nemu podrobný prehľad a relevantné fakty."
    ai_ctx = None

    try:
        cache.set(lock_key, True, timeout=120)
        ai_ctx = await ziskaj_ai_user_kontext(tg_id)
        if not ai_ctx:
            raise Exception("Neautorizovaný prístup.")

        dnes = timezone.now().date()
        ma_db_profil = bool(ai_ctx['user_kwargs'].get('pouzivatel') or ai_ctx['user_kwargs'].get('sukromna_osoba'))
        if ma_db_profil:
            pouzite_dnes = await sync_to_async(AIAuditLog.objects.filter(**ai_ctx['user_kwargs'], cas_dopytu__date=dnes).count)()
        else:
            pouzite_dnes = cache.get(f"ai_limit_tg_{tg_id}_{dnes}", 0)

        if pouzite_dnes >= ai_ctx['denny_limit']:
            await msg.edit_text(f"🛑 <b>Dosiahli ste denný limit ({ai_ctx['denny_limit']} správ).</b>", parse_mode='HTML')
            return AI_CHAT

        doc_file = await doc.get_file()
        await doc_file.download_to_drive(path)

        if not ma_db_profil:
            cache.set(f"ai_limit_tg_{tg_id}_{dnes}", pouzite_dnes + 1, timeout=86400)

        audit_log = await sync_to_async(AIAuditLog.objects.create)(**ai_ctx['user_kwargs'], otazka=f"[{orig_name}] {user_text}", zdroj_poziadavky='TELEGRAM')

        res_dict = await sync_to_async(ask_isas_ai)(
            user_question=user_text, 
            history_text=context.user_data.get('ai_history', ""), 
            file_paths=[path],
            aktualny_pouzivatel=ai_ctx['aktualny_pouzivatel'], 
            rola_pouzivatela=ai_ctx['rola_pouzivatela'], 
            typ_pomeru=ai_ctx['typ_pomeru'],
            is_superuser=ai_ctx['is_superuser'],
            ma_pristup_k_financiam=ai_ctx['ma_pristup_k_financiam'],
            subjekt_id=ai_ctx.get('subjekt_id')
        )
        odpoved_text = res_dict.get("text", "Bez odpovede.")

        in_tokens, out_tokens = res_dict.get("in_tokens", 0), res_dict.get("out_tokens", 0)
        cached_tokens = res_dict.get("cached_tokens", 0)
        audit_log.vstupne_tokeny, audit_log.vystupne_tokeny = in_tokens, out_tokens
        audit_log.presna_cena_usd = Decimal(str(vypocitaj_ai_cenu_usd(in_tokens, out_tokens, cached_tokens=cached_tokens) or 0)).quantize(Decimal('0.000001'))
        audit_log.odpoved = odpoved_text
        audit_log.uspesne_dokoncene = True
        await sync_to_async(audit_log.save)()

        pouzivatel_label = ai_ctx.get('aktualny_pouzivatel') or "Používateľ"
        nova_historia = context.user_data.get('ai_history', "") + f"\n{pouzivatel_label}: [{orig_name}] {user_text}\nAI: {odpoved_text}"
        context.user_data['ai_history'] = nova_historia[-3000:]

        await posli_bezpecnu_ai_odpoved_tg(update, msg, odpoved_text)
    except Exception as e:
        detail_html = f"\n<i>Detail: {safe_tg(str(e)[:300])}</i>" if (ai_ctx and ai_ctx.get('typ') == 'ZAMESTNANEC') else ""
        await msg.edit_text(f"❌ <b>AI modul zlyhal.</b>{detail_html}", reply_markup=CANCEL_MARKUP, parse_mode='HTML')
    finally:
        cache.delete(lock_key)
        if os.path.exists(path): 
            try: os.remove(path)
            except Exception: pass
    return AI_CHAT

async def handle_text_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text or not update.effective_user or not update.effective_chat:
        return
    text = update.message.text.strip()
    text_lower = text.lower()
    
    if text in ["🔓 OTVORIŤ TERMINÁL", "📋 HLAVNÉ MENU"] or text_lower in ["menu", "hlavne menu", "hlavné menu"]:
        try:
            await update.message.delete() # Zmaže textovú správu od užívateľa
        except Exception:
            pass
        await start(update, context)
        
    elif text == "🔒 UZAMKNÚŤ TERMINÁL":
        try:
            await update.message.delete()
        except Exception:
            pass
        await lock_terminal(update, context)
        
    # Ak technik klikne na fyzické tlačidlo "Zrušiť" alebo staré "Odmietam"
    elif text_lower in ["🚫 zrušiť", "zrušiť", "zrusit", "🚫 odmietam", "odmietam"]:
        obj, typ = await identifikuj_pouzivatela(update.effective_user.id)
        if obj:
            # Keďže si to rozmyslel a nechce zdieľať polohu, vrátime mu v DB stav na False
            if hasattr(obj, 'zdiela_polohu'):
                obj.zdiela_polohu = False
                await sync_to_async(obj.save)(update_fields=['zdiela_polohu'])
            
            # 🧹 Garbage Collector: Zmažeme starú výzvu aj starú kotvu cez centrálny GC
            await vymaz_docasne_spravy(context, update.effective_chat.id, keys=['loc_prompt_msg_id', 'anchor_msg_id'])

            anchor_msg = await update.message.reply_text("🔄 <i>Zdieľanie zrušené. Načítavam menu...</i>", reply_markup=REPLY_KEYBOARD_ACTIVE, parse_mode='HTML')
            if context.user_data is not None:
                context.user_data['anchor_msg_id'] = anchor_msg.message_id
            
            await zobraz_hlavne_menu(update.message, context, obj, typ)

    else:
        # Ak technik napíše do menu akýkoľvek iný text, potichu ho zmažeme
        try:
            await update.message.delete()
        except Exception:
            pass

async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.contact or not update.effective_user or not update.effective_chat:
        return

    contact = update.message.contact

    # 🛡️ DEFENSIVE: Kontrola cudzieho kontaktu len vtedy, ak Telegram poslal user_id
    if contact.user_id and contact.user_id != update.effective_user.id:
        await update.message.reply_text(
            "🚨 <b>Bezpečnostná chyba:</b> Môžeš zdieľať iba SVOJ vlastný kontakt, nie cudzí!", 
            parse_mode='HTML'
        )
        return

    # 🛡️ DEFENSIVE: Dôkladná normalizácia čísla (odstránenie medzier, pomlčiek a nečíselných znakov)
    phone = re.sub(r'[^0-9]', '', str(contact.phone_number))
    osoba, meno = await find_and_update_pouzivatel(phone, update.effective_user.id)
    if osoba:
        # Zmazanie chybovej hlášky pred úspešným vstupom
        await vymaz_docasne_spravy(context, update.effective_chat.id, keys=['err_msg'])
        await update.message.reply_text(f"✅ Prístup povolený: {meno}", reply_markup=ReplyKeyboardRemove())
        await start(update, context)
    else:
        # 🧹 Garbage Collector pre cudzích ľudí cez centrálny GC
        await vymaz_docasne_spravy(context, update.effective_chat.id, keys=['err_msg'])
            
        err = await update.message.reply_text(
            "❌ <b>Prístup zamietnutý!</b>\n"
            "Tvoje telefónne číslo neevidujeme v systéme ISAS CORE. Pre povolenie prístupu kontaktuj administrátora.", 
            reply_markup=ReplyKeyboardRemove(), parse_mode='HTML'
        )
        if context.user_data is not None:
            context.user_data['err_msg'] = err.message_id

async def lock_terminal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not chat_id:
        return ConversationHandler.END
    
    # Zmažeme staré hlavné menu a starú tapetu cez centrálny GC
    await vymaz_docasne_spravy(context, chat_id, keys=['last_menu_msg_id', 'lock_msg_id'])

    if context.user_data: 
        context.user_data.clear() 
        
    caption_text = (
        "🔒 <b>ISAS CORE TERMINÁL UZAMKNUTÝ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Tvoja relácia bola bezpečne ukončená.\n\n"
        "Pre opätovné prebudenie klikni na tlačidlo nižšie."
    )
    
    markup = REPLY_KEYBOARD_LOCKED

    # Zmažeme správu, z ktorej bol príkaz zavolaný)
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.delete_message()
        except Exception:
            pass
    elif update.message:
        try:
            await update.message.delete()
        except Exception:
            pass

    # 2. Cesta k tvojej "Lock Screen" tapete
    lock_image_path = os.path.join(settings.BASE_DIR, 'Evidencia', 'static', 'img', 'isas_lockscreen.jpg')

    # 3. Odoslanie Tapety (Ak existuje) a zapamätanie si jej ID
    if os.path.exists(lock_image_path):
        with open(lock_image_path, 'rb') as photo:
            msg = await context.bot.send_photo(
                chat_id=chat_id,
                photo=photo,
                caption=caption_text,
                reply_markup=markup,
                parse_mode='HTML'
            )
            if context.user_data is not None:
                context.user_data['lock_msg_id'] = msg.message_id
    else:
        # Ak obrázok na serveri ešte nemáš, bot nespadne, pošle len text
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=caption_text,
            reply_markup=markup,
            parse_mode='HTML'
        )
        if context.user_data is not None:
            context.user_data['lock_msg_id'] = msg.message_id
        
    return ConversationHandler.END

async def ukaz_vstupy_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query or not update.effective_user or not update.effective_chat:
        return ConversationHandler.END
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("🔄 <i>Načítavam Knihu Vstupov a sťahujem priepustky...</i>", parse_mode='HTML')
    
    vstupy = await get_dnesne_vstupy(update.effective_user.id)
    
    if not vstupy:
        text = (
            "🚧 <b>KNIHA VSTUPOV</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Máš čistý štít! Aktuálne nemáš evidované žiadne plánované vstupy na dnešný deň (ani do budúcna)."
        )
        await update.callback_query.edit_message_text(text, reply_markup=CANCEL_MARKUP, parse_mode='HTML')
        return ConversationHandler.END

    chat_id = update.effective_chat.id if update.effective_chat else None
    msg_id = getattr(getattr(update.callback_query, 'message', None), 'message_id', None)
    if chat_id and msg_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass

    if context.user_data is not None:
        context.user_data['last_menu_msg_id'] = None
        context.user_data['vstupy_msg_ids'] = []

    for v in vstupy:
        if v['stav'] == 'SCHVALENE': ikona = "✅"
        elif v['stav'] == 'ZAMIETNUTE': ikona = "❌"
        else: ikona = "⏳"
        
        text = f"{ikona} <b>{v['fabrika']}</b> ({v['datum']})\n"
        text += f"👨‍🔧 Zostava: {v['kolegovia']}\n"
        text += f"🚗 Vozidlá: {v['auta']}\n"
        text += f"📌 Status: <i>{v['stav_display']}</i>\n"

        # 🛡️ DEFENSIVE: Ak je vstup už schválený SBS alebo zamietnutý, tlačidlo zmeny auta vôbec neponúkame
        if v['stav'] in ['SCHVALENE', 'ZAMIETNUTE']:
            markup = None
        else:
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Zmeniť vozidlo pre výjazd", callback_data=f"zmen_auto_vstup_{v['id']}")]])
        
        # Ak existuje PDF priepustka, pošleme ju technikovi priamo ako dokument!
        if v['pdf_cesta'] and os.path.exists(v['pdf_cesta']):
            info_text = "\n\n📄 <b>PRIEPUSTKA DO VRECKA:</b>\n<i>Predložte pri vstupe na vrátnici.</i>"
            # 🛡️ DEFENSIVE: Garantujeme, že variabilný text sa oreže EŠTE PRED spojením s HTML tagmi
            max_base_len = 1024 - len(info_text) - 10
            base_text = text if len(text) <= max_base_len else (text[:max_base_len].rsplit(' ', 1)[0] + "...")
            caption_safe = base_text + info_text
            msg = None
            try:
                with open(v['pdf_cesta'], 'rb') as pdf_file:
                    msg = await context.bot.send_document(
                        chat_id=update.effective_chat.id, 
                        document=pdf_file, 
                        caption=caption_safe,
                        reply_markup=markup,
                        parse_mode='HTML'
                    )
                    if context.user_data is not None:
                        context.user_data['vstupy_msg_ids'].append(msg.message_id)
            except Exception as pdf_send_err:
                logging.warning(f"⚠️ Zlyhalo odoslanie PDF priepustky cez Telegram: {pdf_send_err}")

            # 🛡️ DEFENSIVE: Ak sa odoslanie dokumentu nepodarilo, spoľahlivo pošleme aspoň textovú kartu
            if not msg:
                msg = await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=caption_safe,
                    reply_markup=markup,
                    parse_mode='HTML'
                )
                if context.user_data is not None:
                    context.user_data['vstupy_msg_ids'].append(msg.message_id)
        else:
            # Ak ešte PDF nie je vygenerované, pošleme aspoň info s inštrukciami
            if v['instrukcie']:
                text += f"\n⚠️ <b>Inštrukcie od firmy:</b>\n<i>{v['instrukcie']}</i>"
                
            msg = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=text,
                reply_markup=markup,
                parse_mode='HTML'
            )
            if context.user_data is not None:
                context.user_data.setdefault('vstupy_msg_ids', []).append(msg.message_id)

    msg_back = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Pre návrat do hlavného menu stlač tlačidlo nižšie.",
        reply_markup=CANCEL_MARKUP,
        parse_mode='HTML'
    )
    if context.user_data is not None:
        context.user_data.setdefault('vstupy_msg_ids', []).append(msg_back.message_id)
    
    return ConversationHandler.END

async def zmenit_auto_vstup_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Zobrazí zoznam áut pre zmenu v konkrétnej priepustke."""
    if not update.callback_query:
        return ConversationHandler.END
    await update.callback_query.answer()
    data = update.callback_query.data or ""
    povolenie_id = data.replace("zmen_auto_vstup_", "")
    
    vozidla = await get_vozidla_list() # Funkciu už máš
    keyboard = []
    for v in vozidla:
        popis_modelu = f" ({v.znacka_a_model})" if getattr(v, 'znacka_a_model', None) else ""
        spz_text = getattr(v, 'spz', '') or f"Vozidlo #{v.id}"
        keyboard.append([InlineKeyboardButton(f"🚗 {spz_text}{popis_modelu}", callback_data=f"set_car_vstup_{povolenie_id}_{v.id}")])
    keyboard.append([InlineKeyboardButton("❌ Zrušiť", callback_data="cancel_action")])
    
    # 🛡️ DEFENSIVE: Kontrola prítomnosti dokumentu aj popisku (ochrana pred BadRequest pri Telegram médiách)
    vyzva_text = "🔄 <b>ZMENA VOZIDLA PRE VSTUP</b>\n━━━━━━━━━━━━━━━━━━━━\nDo ktorého auta si práve presadol?"
    cb_msg = update.callback_query.message if update.callback_query else None
    if getattr(cb_msg, 'document', None) or getattr(cb_msg, 'caption', None):
        await update.callback_query.edit_message_caption(
            caption=vyzva_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
    else:
        await update.callback_query.edit_message_text(
            vyzva_text, 
            reply_markup=InlineKeyboardMarkup(keyboard), 
            parse_mode='HTML'
        )
    return ConversationHandler.END

async def zmenit_auto_vstup_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bezpečne zmení vozidlo cez centrálnu backendovú logiku."""
    if not update.callback_query or not update.effective_user:
        return ConversationHandler.END
    await update.callback_query.answer()
    
    # Ochrana pred neošetreným IndexError/ValueError pri poškodenom callbacku
    # Bezpečná detekcia mediálnej správy podľa typu prílohy aj existencie popisku
    cb_msg = update.callback_query.message if update.callback_query else None
    is_media = bool(getattr(cb_msg, 'document', None) or getattr(cb_msg, 'caption', None))

    try:
        data = (update.callback_query.data or "").replace("set_car_vstup_", "").split("_")
        povolenie_id = int(data[0])
        auto_id = int(data[1])
    except (IndexError, ValueError, AttributeError):
        err_text = "⚠️ <b>Chyba:</b> Neplatné údaje požiadavky."
        if is_media:
            await update.callback_query.edit_message_caption(caption=err_text, parse_mode='HTML')
        else:
            await update.callback_query.edit_message_text(err_text, parse_mode='HTML')
        return ConversationHandler.END
    
    if is_media:
        await update.callback_query.edit_message_caption(caption="🔄 <i>Spracovávam zmenu vozidla...</i>", reply_markup=None, parse_mode='HTML')
    else:
        await update.callback_query.edit_message_text("🔄 <i>Spracovávam zmenu vozidla...</i>", reply_markup=None, parse_mode='HTML')

    from Evidencia.tasks import vykonaj_zmenu_vozidla_vstupu
    try:
        success, text = await sync_to_async(vykonaj_zmenu_vozidla_vstupu)(
            povolenie_id=povolenie_id,
            auto_id=auto_id,
            user_identifier={'telegram_id': update.effective_user.id}
        )
    except Exception as e:
        logging.error(f"🚨 Chyba pri zmene vozidla cez Telegram bota: {e}")
        text = "❌ <b>Chyba systému:</b> Zmenu vozidla sa nepodarilo spracovať kvôli chybe spojenia s databázou. Skús to znova alebo kontaktuj dispečing."

    final_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Návrat", callback_data="cancel_action")]])
    if is_media:
        await update.callback_query.edit_message_caption(caption=text[:1024], reply_markup=final_markup, parse_mode='HTML')
    else:
        await update.callback_query.edit_message_text(text, reply_markup=final_markup, parse_mode='HTML')
    return ConversationHandler.END

async def zacat_presun(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query or not update.effective_user:
        return ConversationHandler.END
    await update.callback_query.answer()
    filialky = await get_zoznam_filialok_pre_bot(update.effective_user.id)
    
    if not filialky:
        await update.callback_query.edit_message_text(
            "⚠️ <b>Prístup zamietnutý</b>\nNemáš priradený prístup k žiadnej filiálke/skladu. Kontaktuj administrátora.",
            reply_markup=CANCEL_MARKUP, parse_mode='HTML'
        )
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton("🏢 Hlavný sklad", callback_data="pzdroj_0")]]
    for f in filialky:
        keyboard.append([InlineKeyboardButton(f"🏭 {f['nazov']}", callback_data=f"pzdroj_{f['id']}")])
    keyboard.append([InlineKeyboardButton("❌ Zrušiť", callback_data="cancel_action")])
    
    await update.callback_query.edit_message_text(
        "🔄 <b>MEDZISKLADOVÝ PRESUN</b>\n━━━━━━━━━━━━━━━━━━━━\n⬇️ <b>ODKIAĽ</b> presúvaš tovar? (Zdroj)", 
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML'
    )
    return P_ZDROJ

async def presun_vyber_ciela(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query or not update.effective_user:
        return ConversationHandler.END
    await update.callback_query.answer()
    zdroj_id = int((update.callback_query.data or "").replace("pzdroj_", ""))
    if context.user_data is not None:
        context.user_data['p_zdroj_id'] = zdroj_id
        
    filialky = await get_zoznam_filialok_pre_bot(update.effective_user.id)
    
    keyboard = []
    if zdroj_id != 0:
        keyboard.append([InlineKeyboardButton("🏢 Hlavný sklad", callback_data="pciel_0")])
        
    for f in filialky:
        if f['id'] != zdroj_id: # Nemôže presunúť sám do seba
            keyboard.append([InlineKeyboardButton(f"🏭 {f['nazov']}", callback_data=f"pciel_{f['id']}")])
            
    keyboard.append([InlineKeyboardButton("🔙 Späť na výber zdroja", callback_data="presun_start")])
    
    await update.callback_query.edit_message_text(
        "🔄 <b>MEDZISKLADOVÝ PRESUN</b>\n━━━━━━━━━━━━━━━━━━━━\n⬆️ <b>KAM</b> presúvaš tovar? (Cieľ)", 
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML'
    )
    return P_CIEL

async def presun_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return ConversationHandler.END
    await update.callback_query.answer()
    data = update.callback_query.data or ""
    
    if data.startswith("pciel_") and data != "pciel_back":
        if context.user_data is not None:
            context.user_data['p_ciel_id'] = int(data.replace("pciel_", ""))
        
    keyboard = []
    for val, name in CATEGORY_CHOICES:
        keyboard.append([InlineKeyboardButton(f"📂 {name}", callback_data=f"pcat_{val}")])
    keyboard.append([InlineKeyboardButton("❌ Zrušiť presun", callback_data="cancel_action")])
    
    await update.callback_query.edit_message_text(
        f"🗂️ <b>VÝBER KATEGÓRIE</b>\n━━━━━━━━━━━━━━━━━━━━\nZvoľ hlavnú kategóriu presúvaného tovaru:", 
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML'
    )
    return P_CAT

async def presun_sub_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return ConversationHandler.END
    await update.callback_query.answer()
    cat_name = (update.callback_query.data or "").replace("pcat_", "")
    if context.user_data is not None:
        context.user_data['p_cat'] = cat_name
    
    subcats = CATEGORY_MAP.get(cat_name, {})
    keyboard = []
    for sub_name, model_class in subcats.items():
        keyboard.append([InlineKeyboardButton(f"📁 {sub_name}", callback_data=f"psub_{model_class.__name__}")])
        
    # Návrat na výber kategórie (nepotrebujeme znova vyberať cieľ)
    keyboard.append([InlineKeyboardButton("🔙 Späť na kategórie", callback_data="pciel_back")])
    
    await update.callback_query.edit_message_text(
        f"📂 <b>{cat_name.upper()}</b>\n━━━━━━━━━━━━━━━━━━━━\nZvoľ podkategóriu:", 
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML'
    )
    return P_SUB

async def _render_presun_list(message_or_query, context, model_name, is_message=False):
    model_class, cat, subcat_name = await get_model_class_by_name(model_name)
    if not model_class:
        text = "⚠️ <b>Chyba:</b> Kategória tovaru nebola nájdená. Spustite presun znova z menu."
        if is_message:
            await message_or_query.reply_text(text, reply_markup=CANCEL_MARKUP, parse_mode='HTML')
        else:
            await message_or_query.edit_message_text(text, reply_markup=CANCEL_MARKUP, parse_mode='HTML')
        return

    user_data = context.user_data or {}
    search_term = str(user_data.get('p_search', "") or "").strip()
    page = int(user_data.get('p_page', 0) or 0)
    zdroj_id = int(user_data.get('p_zdroj_id', 0) or 0)
    
    start_idx = page * ITEMS_PER_PAGE
    end_idx = start_idx + ITEMS_PER_PAGE
    
    total_count, stavy = await get_stavy_pre_bota(model_class, zdroj_id, search_term, start_idx, end_idx)
    
    keyboard = []
    for item, stav in stavy:
        jednotka = getattr(item, 'merna_jednotka', 'ks')
        nazov_polozky = getattr(item, 'obchodny_nazov', None) or getattr(item, 'seriove_cislo', None) or getattr(item, 'nazov', None) or str(item)
        btn_text = f"📦 {nazov_polozky} (Dostupné: {stav} {jednotka})"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"pitm_{model_name}_{item.pk}")])
    
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Predchádzajúca", callback_data=f"ppage_{model_name}_{page-1}"))
    if end_idx < total_count:
        nav_buttons.append(InlineKeyboardButton("Ďalšia ➡️", callback_data=f"ppage_{model_name}_{page+1}"))
    if nav_buttons:
        keyboard.append(nav_buttons)

    if search_term:
        keyboard.append([InlineKeyboardButton(f"❌ Zrušiť filter: '{search_term}'", callback_data=f"pclr_{model_name}")])
    else:
        keyboard.append([InlineKeyboardButton("🔍 Hľadať (Názov/Číslo)", callback_data=f"pask_{model_name}")])

    keyboard.append([InlineKeyboardButton("🔙 Späť na podkategórie", callback_data=f"pcat_{cat}")])
    
    text = (
        f"📦 <b>VÝBER POLOŽKY NA PRESUN</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📁 <b>Kategória:</b> {subcat_name.upper()}\n"
    )
    if search_term: text += f"🔍 <i>Filter: {search_term}</i>\n"
    
    markup = InlineKeyboardMarkup(keyboard)
    if is_message:
        await message_or_query.reply_text(text, reply_markup=markup, parse_mode='HTML')
    else:
        await message_or_query.edit_message_text(text, reply_markup=markup, parse_mode='HTML')

async def presun_item_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return ConversationHandler.END
    await update.callback_query.answer()
    data = update.callback_query.data or ""
    
    if data.startswith("psub_"):
        model_name = data.replace("psub_", "")
        if context.user_data is not None:
            context.user_data['p_page'] = 0
            context.user_data['p_search'] = ""
    elif data.startswith("ppage_"):
        parts = data.split("_")
        model_name = parts[1]
        if context.user_data is not None:
            context.user_data['p_page'] = int(parts[2])
    elif data.startswith("pclr_"):
        model_name = data.replace("pclr_", "")
        if context.user_data is not None:
            context.user_data['p_page'] = 0
            context.user_data['p_search'] = ""
    elif data.startswith("pback_"):
        model_name = data.replace("pback_", "")
    else:
        model_name = str(context.user_data.get('p_model', '') or '') if context.user_data is not None else ""
        
    if context.user_data is not None:
        context.user_data['p_model'] = model_name
    await _render_presun_list(update.callback_query, context, model_name, is_message=False)
    return P_ITEM

async def presun_ask_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return ConversationHandler.END
    await update.callback_query.answer()
    model_name = (update.callback_query.data or "").replace("pask_", "")
    if context.user_data is not None:
        context.user_data['p_model'] = model_name
    
    keyboard = [[InlineKeyboardButton("🔙 Zrušiť hľadanie", callback_data=f"pback_{model_name}")]]
    await update.callback_query.edit_message_text(
        "🔍 <b>VYHĽADÁVANIE POLOŽKY</b>\n━━━━━━━━━━━━━━━━━━━━\nNapíš názov alebo časť sériového čísla a odošli ako bežnú správu.",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML'
    )
    return P_SEARCH_WAIT

async def presun_handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return P_ITEM
    if context.user_data is not None:
        context.user_data['p_search'] = update.message.text or ""
        context.user_data['p_page'] = 0 
    model_name = str(context.user_data.get('p_model', '') or '') if context.user_data is not None else ""
    await _render_presun_list(update.message, context, model_name, is_message=True)
    return P_ITEM

async def presun_zadat_pocet_vyzva(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return ConversationHandler.END
    await update.callback_query.answer()
    parts = (update.callback_query.data or "").split("_")
    if context.user_data is not None and len(parts) > 2:
        context.user_data['p_model'] = parts[1]
        context.user_data['p_pk'] = parts[2]
    
    await update.callback_query.edit_message_text(
        "🔢 <b>ZADAJ MNOŽSTVO</b>\n━━━━━━━━━━━━━━━━━━━━\nNapíš do chatu presný počet kusov (číslo), ktoré chceš presunúť:",
        reply_markup=CANCEL_MARKUP, parse_mode='HTML'
    )
    return P_COUNT

async def presun_zadat_pocet_spracuj(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text or not update.effective_user:
        return P_COUNT

    pocet_str = update.message.text.strip()
    if not pocet_str.isdigit() or int(pocet_str) <= 0 or int(pocet_str) > 1000000:
        await posli_docasnu_chybu(update, context, "⚠️ <b>Chyba:</b> Musíš zadať platné celé číslo v rozsahu 1 až 1 000 000. Skús to znova:", reply_markup=CANCEL_MARKUP)
        return P_COUNT

    if not context.user_data or 'p_model' not in context.user_data or 'p_pk' not in context.user_data:
        await update.message.reply_text(
            "⚠️ <b>Relácia vypršala!</b>\nZačni presun znova z hlavného menu.",
            reply_markup=CANCEL_MARKUP, parse_mode='HTML'
        )
        return ConversationHandler.END
        
    count = int(pocet_str)
    
    success, sprava = await uloz_medziskladovy_presun_db(
        update.effective_user.id,
        context.user_data.get('p_zdroj_id', 0),
        context.user_data.get('p_ciel_id', 0),
        context.user_data.get('p_model'),
        context.user_data.get('p_pk'),
        count
    )
    
    if success:
        text = f"✅ <b>PRESUN VYKONANÝ</b>\n━━━━━━━━━━━━━━━━━━━━\n{sprava}"
    else:
        text = f"❌ <b>CHYBA PRESUNU</b>\n━━━━━━━━━━━━━━━━━━━━\n{sprava}"

    if context.user_data is not None:
        for k in ['p_zdroj_id', 'p_ciel_id', 'p_cat', 'p_sub', 'p_model', 'p_pk', 'p_search', 'p_page']:
            context.user_data.pop(k, None)
        
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Návrat do menu", callback_data="cancel_action")]]), parse_mode='HTML')
    return ConversationHandler.END

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """
    ISAS CORE: Globálny zachytávač chýb. 
    Ak akákoľvek funkcia v botovi nečakane spadne, táto funkcia to zachytí a informuje technika.
    """
    # 1. Zalogujeme chybu do konzoly servera pre teba (Admina)
    logging.error(msg="Výnimka pri spracovaní Telegram update-u:", exc_info=context.error)

    # 2. Zistíme, či chyba vznikla v chate s konkrétnym technikom
    eff_chat = update.effective_chat if isinstance(update, Update) else None
    if eff_chat:
        chat_id = eff_chat.id
        
        # 3. Vytvoríme bezpečnú a krátku chybovú hlášku pre technika
        error_message = str(context.error)
        if len(error_message) > 200:
            error_message = error_message[:200] + "..."
            
        text = (
            "❌ <b>KRITICKÁ CHYBA SYSTÉMU</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "ISAS Core narazil na neočakávaný problém pri spracovaní tvojej požiadavky.\n\n"
            f"<b>Detail:</b> <code>{html.escape(error_message)}</code>\n\n"
            "<i>Skús akciu zopakovať, alebo sa vráť do hlavného menu. Ak problém pretrváva, kontaktuj dispečing.</i>"
        )
        
        # 4. Odošleme správu technikovi
        try:
            await context.bot.send_message(
                chat_id=chat_id, 
                text=text, 
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Hlavné menu", callback_data="cancel_action")]]),
                parse_mode='HTML'
            )
        except TelegramError as tg_send_err:
            logging.critical(f"Kritické zlyhanie: Nepodarilo sa odoslať chybovú správu do chatu {chat_id}: {tg_send_err}")

async def vysavac_balastu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    ISAS CORE: Ultimátny Garbage Collector. 
    Zachytí akýkoľvek nežiaduci obsah (nálepky, gify, zblúdené fotky mimo procesov) a potichu ho zmaže.
    """
    if update.message:
        try:
            await update.message.delete()
        except TelegramError:
            pass

def main():
    TEXT_FILTER = filters.TEXT & ~filters.COMMAND & ~filters.Text(["🔒 UZAMKNÚŤ TERMINÁL", "🔓 OTVORIŤ TERMINÁL", "📋 HLAVNÉ MENU"])
    
    max_retries = 6
    for pokus in range(max_retries):
        try:
            gn = GlobalneNastavenia.objects.first()
            if not gn or not gn.telegram_bot_token:
                logging.error("CRITICAL: Telegram Bot Token nie je nastavený v Globálnych Nastaveniach! Skontrolujte Administráciu.")
                return
            bot_token = gn.telegram_bot_token
            break  # Úspešné pripojenie
        except Exception as e:
            logging.warning(f"Pripojenie k DB zlyhalo (Pokus {pokus+1}/{max_retries}). Čakám 5 sekúnd... ({e})")
            time.sleep(5)
    else:
        logging.error("FATAL: Nepodarilo sa pripojiť k databáze ani po opakovaných pokusoch. Skript sa ukončuje.")
        return

    # Trvalá pamäť (PicklePersistence) s vyhradeným priečinkom pre Docker Volume
    persistence_dir = os.path.join(settings.BASE_DIR, 'data')
    os.makedirs(persistence_dir, exist_ok=True) # Automaticky vytvorí priečinok 'data', ak neexistuje
    persistence_path = os.path.join(persistence_dir, 'isas_bot_data.pickle')
    persistence = PicklePersistence(filepath=persistence_path)

    async def post_init(application: Application):
        try:
            await application.bot.set_my_commands([
                BotCommand("menu", "📋 Hlavné menu"),
                BotCommand("start", "🔄 Reštartovať terminál"),
                BotCommand("stop", "🔒 Uzamknúť terminál"),
            ])
        except Exception as e:
            logging.warning(f"Zlyhala registrácia príkazov v menu Telegramu: {e}")

    app = (
        Application.builder()
        .token(bot_token)
        .persistence(persistence)
        .post_init(post_init)
        .read_timeout(120)
        .write_timeout(120)
        .connect_timeout(120)
        .pool_timeout(120)
        .build()
    )
    
    faktura_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(zacat_fakturu, pattern="^menu_faktura$")],
        states={
            F_FOTO: [MessageHandler(filters.PHOTO | filters.Document.ALL, spracuj_fakturu_subor)]
        },
        fallbacks=[
            CallbackQueryHandler(cancel, pattern="^cancel_action$"),
            MessageHandler(filters.Text("🔒 UZAMKNÚŤ TERMINÁL"), lock_terminal),
            MessageHandler(filters.Text("📋 HLAVNÉ MENU"), cancel),
            CommandHandler("start", start),
            CommandHandler("menu", start)
        ],
        per_message=False
    )

    sklad_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(sklad_vyber_skladu, pattern="^sklad_main$")],
        states={
            S_WH: [
                CallbackQueryHandler(sklad_main_menu, pattern="^sklad_main_"),
                CallbackQueryHandler(cancel, pattern="^cancel_action$")
            ],
            S_MAIN: [
                CallbackQueryHandler(sklad_sub_menu, pattern="^cat_"),
                CallbackQueryHandler(sklad_vyber_skladu, pattern="^sklad_start$") # Návrat z kategórií na výber skladu
            ],
            S_SUB: [
                CallbackQueryHandler(sklad_item_list, pattern="^sub_"), 
                CallbackQueryHandler(sklad_main_menu, pattern="^sklad_main_") # Ak sa vráti, už si pamätá wh_id z user_data
            ],
            S_ITEM: [
                CallbackQueryHandler(sklad_item_action, pattern="^(itm_|edit:)"),
                CallbackQueryHandler(sklad_vyber_typu_dokladu, pattern="^save_item$"),
                CallbackQueryHandler(sklad_item_save, pattern="^doklad_"),
                CallbackQueryHandler(sklad_item_list, pattern="^(page_|clrsearch_|back_to_list_|sub_)"),
                CallbackQueryHandler(ask_for_search, pattern="^asksrch_"),
                CallbackQueryHandler(sklad_sub_menu, pattern="^cat_")
            ],
            S_SEARCH_WAIT: [
                MessageHandler(TEXT_FILTER, handle_search_text),
                CallbackQueryHandler(sklad_item_list, pattern="^back_to_list_")
            ]
        },
        fallbacks=[
            CallbackQueryHandler(cancel, pattern="^cancel_action$"),
            MessageHandler(filters.Text("🔒 UZAMKNÚŤ TERMINÁL"), lock_terminal),
            MessageHandler(filters.Text("📋 HLAVNÉ MENU"), cancel),
            CommandHandler("start", start),
            CommandHandler("menu", start)
        ],
        per_message=False
    )

    presun_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(zacat_presun, pattern="^presun_start$")],
        states={
            P_ZDROJ: [CallbackQueryHandler(presun_vyber_ciela, pattern="^pzdroj_")],
            P_CIEL: [
                CallbackQueryHandler(presun_main_menu, pattern="^pciel_"),
                CallbackQueryHandler(zacat_presun, pattern="^presun_start$")
            ],
            P_CAT: [
                CallbackQueryHandler(presun_sub_menu, pattern="^pcat_"),
                CallbackQueryHandler(presun_main_menu, pattern="^pciel_back$")
            ],
            P_SUB: [
                CallbackQueryHandler(presun_item_list, pattern="^psub_"),
                CallbackQueryHandler(presun_main_menu, pattern="^pciel_back$")
            ],
            P_ITEM: [
                CallbackQueryHandler(presun_zadat_pocet_vyzva, pattern="^pitm_"),
                CallbackQueryHandler(presun_item_list, pattern="^(ppage_|pclr_|pback_|psub_)"),
                CallbackQueryHandler(presun_ask_search, pattern="^pask_"),
                CallbackQueryHandler(presun_sub_menu, pattern="^pcat_")
            ],
            P_SEARCH_WAIT: [
                MessageHandler(TEXT_FILTER, presun_handle_search),
                CallbackQueryHandler(presun_item_list, pattern="^pback_")
            ],
            P_COUNT: [MessageHandler(TEXT_FILTER, presun_zadat_pocet_spracuj)]
        },
        fallbacks=[
            CallbackQueryHandler(cancel, pattern="^cancel_action$"),
            MessageHandler(filters.Text("🔒 UZAMKNÚŤ TERMINÁL"), lock_terminal),
            MessageHandler(filters.Text("📋 HLAVNÉ MENU"), cancel),
            CommandHandler("start", start),
            CommandHandler("menu", start)
        ],
        per_message=False
    )

    jazda_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(zacat_jazdu, pattern="^menu_jazdy_(firemna|sukromna)$")],
        states={
            J_AUTO: [CallbackQueryHandler(j_km_start_vyzva, pattern="^jauto_")],
            # ZMENA: KM Start teraz počúva na kliknutie (Tlačidlo Súhlasí) aj na TEXT
            J_KM_START: [
                CallbackQueryHandler(j_km_start_tlacidlo, pattern="^jkm_"),
                MessageHandler(TEXT_FILTER, j_km_start_text)
            ],
            J_KM_KONIEC: [MessageHandler(TEXT_FILTER, j_ciel_vyzva)],
            
            J_CIEL: [CallbackQueryHandler(j_ciel_spracuj, pattern="^jciel_")],
            J_CIEL_FILIALKA: [CallbackQueryHandler(j_ciel_filialka_spracuj, pattern="^sel_filialka_")],
            J_CIEL_ZAKAZNIK_SEARCH: [MessageHandler(TEXT_FILTER, j_ciel_zakaznik_search_vyzva)],
            J_CIEL_ZAKAZNIK_SELECT: [CallbackQueryHandler(j_ciel_zakaznik_select_spracuj, pattern="^sel_zakaznik_")],
            J_CIEL_TEXT: [MessageHandler(TEXT_FILTER, j_ciel_text_spracuj)],
            
            J_UCEL: [MessageHandler(TEXT_FILTER, j_spolujazdci_vyzva)],
            J_SPOLUJAZDEC: [CallbackQueryHandler(j_spolujazdec_spracuj, pattern="^spolu_")],
            J_STAT_CIELA: [CallbackQueryHandler(j_stat_ciela_spracuj, pattern="^jstat_")],
            J_NOCLAH_STRAVA: [CallbackQueryHandler(j_finish, pattern="^jdiety_")]
        },
        fallbacks=[
            CallbackQueryHandler(cancel, pattern="^cancel_action$"),
            MessageHandler(filters.Text("🔒 UZAMKNÚŤ TERMINÁL"), lock_terminal),
            MessageHandler(filters.Text("📋 HLAVNÉ MENU"), cancel),
            CommandHandler("start", start),
            CommandHandler("menu", start)
        ],
        per_message=False
    )

    tankovanie_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(zacat_tankovanie, pattern="^menu_tankovanie$")],
        states={
            T_AUTO: [CallbackQueryHandler(t_km, pattern="^tauto_")],
            T_KM: [MessageHandler(TEXT_FILTER, t_litre)],
            T_LITRE: [MessageHandler(TEXT_FILTER, t_cena)],
            T_CENA: [MessageHandler(TEXT_FILTER, t_platba)],
            T_PLATBA: [CallbackQueryHandler(t_platba_spracuj, pattern="^pay_")],
            T_STANICA: [
                CallbackQueryHandler(t_stanica_hladat_vyzva, pattern="^stanica_hladat$"),
                CallbackQueryHandler(t_stanica_spracuj, pattern="^stanica_")
            ],
            T_STANICA_SEARCH: [MessageHandler(TEXT_FILTER, t_stanica_search_result)],
                
            T_FOTO: [MessageHandler(filters.PHOTO | filters.Document.ALL, t_finish)],
        },
        fallbacks=[
            CallbackQueryHandler(cancel, pattern="^cancel_action$"),
            MessageHandler(filters.Text("🔒 UZAMKNÚŤ TERMINÁL"), lock_terminal),
            MessageHandler(filters.Text("📋 HLAVNÉ MENU"), cancel),
            CommandHandler("start", start),
            CommandHandler("menu", start)
        ],
        per_message=False
    )

    servis_hlas_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(zacat_servis_hlasom, pattern="^menu_servis_hlas$")],
        states={
            SH_ZAKAZNIK_SEARCH: [MessageHandler(TEXT_FILTER, sh_zakaznik_search_vyzva)],
            SH_ZAKAZNIK_SELECT: [CallbackQueryHandler(sh_zakaznik_select_spracuj, pattern="^sh_zak_")],
            SH_ZARIADENIE_SELECT: [CallbackQueryHandler(sh_zariadenie_select_spracuj, pattern="^sh_zar_")],
            SH_TYP_SELECT: [CallbackQueryHandler(sh_typ_select_spracuj, pattern="^sh_typ_")],
            SH_KOSIK: [CallbackQueryHandler(sh_kosik_akcia, pattern="^(sh_go_voice|sh_remove_item|sh_del_|sh_back_kosik|sh_add_item)$")],
            SH_MEDIA_MENU: [CallbackQueryHandler(sh_media_akcia, pattern="^sh_media_")],
            SH_MEDIA_FOTO_PRED: [MessageHandler(filters.PHOTO | filters.Document.ALL | filters.VIDEO, sh_prijmi_foto_pred)],
            SH_MEDIA_FOTO_PO: [MessageHandler(filters.PHOTO | filters.Document.ALL | filters.VIDEO, sh_prijmi_foto_po)],
            SH_MEDIA_VIDEO: [MessageHandler(filters.PHOTO | filters.Document.ALL | filters.VIDEO, sh_prijmi_video)],
            SH_SKLAD_CAT: [
                CallbackQueryHandler(sh_sklad_cat_spracuj, pattern="^sh_cat_"),
                CallbackQueryHandler(sh_kosik_akcia, pattern="^sh_back_kosik$")
            ],
            SH_SKLAD_SUB: [
                CallbackQueryHandler(sh_sklad_sub_spracuj, pattern="^(sh_sub_|sh_page_)"),
                CallbackQueryHandler(sh_kosik_akcia, pattern="^sh_back_kosik$")
            ],
            SH_SKLAD_ITEM: [
                CallbackQueryHandler(sh_sklad_item_spracuj, pattern="^sh_itm_"),
                CallbackQueryHandler(sh_sklad_sub_spracuj, pattern="^sh_page_"),
                CallbackQueryHandler(sh_kosik_akcia, pattern="^sh_back_kosik$")
            ],
            SH_SKLAD_COUNT: [MessageHandler(TEXT_FILTER, sh_sklad_count_spracuj)],
            SH_HLASOVKA: [
                CallbackQueryHandler(sh_draft_akcia, pattern="^sh_draft_"),
                MessageHandler(filters.VOICE, spracuj_servis_hlasovka),
                MessageHandler(filters.ALL & ~filters.VOICE & ~filters.COMMAND, sh_hlasovka_chyba)
            ]
        },
        fallbacks=[
            CallbackQueryHandler(cancel, pattern="^cancel_action$"),
            MessageHandler(filters.Text("🔒 UZAMKNÚŤ TERMINÁL"), lock_terminal),
            MessageHandler(filters.Text("📋 HLAVNÉ MENU"), cancel),
            CommandHandler("start", start),
            CommandHandler("menu", start)
        ],
        per_message=False
    )

    ai_chat_handler = ConversationHandler(
            entry_points=[CallbackQueryHandler(start_ai_chat, pattern="^menu_ai_chat$")],
            states={AI_CHAT: [
                MessageHandler(TEXT_FILTER, handle_ai_chat), 
                MessageHandler(filters.VOICE, handle_ai_voice),
                MessageHandler(filters.PHOTO, handle_ai_photo),
                MessageHandler(filters.Document.ALL | filters.VIDEO, handle_ai_document)
            ]},
            fallbacks=[
                CallbackQueryHandler(cancel, pattern="^cancel_action$"),
                MessageHandler(filters.Text("🔒 UZAMKNÚŤ TERMINÁL"), lock_terminal),
                MessageHandler(filters.Text("📋 HLAVNÉ MENU"), cancel),
                CommandHandler("start", start),
                CommandHandler("menu", start)
            ],
            per_message=False
        )

    app.add_handler(faktura_handler) 
    app.add_handler(sklad_handler)
    app.add_handler(presun_handler)
    app.add_handler(jazda_handler)
    app.add_handler(tankovanie_handler)
    app.add_handler(servis_hlas_handler)
    app.add_handler(ai_chat_handler)
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_menu))
    app.add_handler(MessageHandler(filters.CONTACT, handle_contact))
    app.add_handler(CallbackQueryHandler(cancel, pattern="^cancel_action$"))

    app.add_handler(CommandHandler("stop", lock_terminal))
    app.add_handler(CallbackQueryHandler(lock_terminal, pattern="^lock_terminal$"))

    app.add_handler(CallbackQueryHandler(close_task_from_tg, pattern="^close_task_"))
    app.add_handler(CallbackQueryHandler(ukaz_vstupy_menu, pattern="^menu_vstupy$"))
    app.add_handler(CallbackQueryHandler(ukaz_moje_ulohy, pattern="^menu_moje_ulohy$"))
    app.add_handler(CallbackQueryHandler(zmenit_auto_vstup_start, pattern="^zmen_auto_vstup_"))
    app.add_handler(CallbackQueryHandler(zmenit_auto_vstup_save, pattern="^set_car_vstup_"))
    app.add_handler(CallbackQueryHandler(cancel, pattern="^cancel_action$"))

    app.add_handler(CallbackQueryHandler(menu_nastavenia, pattern="^menu_nastavenia$"))
    app.add_handler(CallbackQueryHandler(toggle_tg, pattern="^tog_tg$"))
    app.add_handler(CallbackQueryHandler(toggle_mail, pattern="^tog_mail$"))
    app.add_handler(CallbackQueryHandler(force_location_action, pattern="^force_location$"))
    app.add_handler(CallbackQueryHandler(turn_off_location_action, pattern="^turn_off_location$"))
    app.add_handler(CallbackQueryHandler(reset_cerpacky, pattern="^zmen_cs$"))

    app.add_handler(CallbackQueryHandler(sos_trigger_action, pattern="^sos_trigger$"))
    app.add_handler(CallbackQueryHandler(sos_cancel_action, pattern="^sos_cancel$"))
    
    app.add_handler(MessageHandler(filters.LOCATION | (filters.LOCATION & filters.UpdateType.EDITED_MESSAGE), handle_location))

    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, vysavac_balastu))
    app.add_error_handler(global_error_handler)

    # 🛡️ ARCHITECT BULLETPROOF TIMEZONE RESOLVER:
    # Ak je v Django USE_TZ=False, čas pre pripomienky MUSÍ byť naive (tzinfo=None), inak nastane kolízia s naive DB.
    if getattr(settings, 'USE_TZ', False):
        try:
            bot_tz = timezone.get_current_timezone()
        except Exception:
            try:
                bot_tz = ZoneInfo(getattr(settings, 'TIME_ZONE', 'UTC'))
            except Exception:
                bot_tz = datetime.timezone.utc
    else:
        bot_tz = None

    cas_pripomienky = datetime.time(hour=17, minute=0, tzinfo=bot_tz)
    
    if app.job_queue:
        app.job_queue.run_daily(automaticka_pripomienka_uloh, time=cas_pripomienky)
    else:
        logging.error("CRITICAL: JobQueue nie je dostupná! Automatické pripomienky nebudú fungovať. Nainštaluj modul: pip install \"python-telegram-bot[job-queue]\"")
    
    app.run_polling()

if __name__ == '__main__':
    main()