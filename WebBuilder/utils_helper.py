from datetime import datetime, timedelta, time as dt_time, date as dt_date
from decimal import Decimal
import re
import zoneinfo
from django.apps import apps
from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from django.urls import reverse, NoReverseMatch
from django.utils import timezone

from Evidencia.industry_loader import (
    get_industry_term,
    is_esg_audit_supported,
    HAS_HEAT_RECOVERY,
    HAS_VTZ_INSPECTION,
    HAS_FPLYNY_AUDIT,
    HAS_VIBRO_DIAGNOSTICS,
    HAS_CAPEX_CALCULATOR,
)

def get_b2b_client_and_discount(request):
    """🛡️ ISAS BEZPEČNOSTNÝ HELPER: Vráti B2B klienta a jeho zľavu (Ochrana pred AttributeError)."""
    user = getattr(request, 'user', None)
    is_auth = user.is_authenticated() if callable(getattr(user, 'is_authenticated', None)) else getattr(user, 'is_authenticated', False)
    if not is_auth or not getattr(user, 'is_active', False):
        return None, Decimal('0.00')

    profil = None
    for attr in ('zakaznik_profil', 'zakaznik'):
        try:
            kandidat = getattr(user, attr, None)
            if kandidat and not getattr(kandidat, 'je_interny_profil', False) and hasattr(kandidat, 'b2b_zlava_percenta'):
                profil = kandidat
                break
        except Exception:
            continue

    tenant = getattr(request, 'tenant', None)
    if tenant and (getattr(tenant, 'je_interny_profil', False) or not hasattr(tenant, 'b2b_zlava_percenta')):
        tenant = None

    if tenant:
        is_staff = bool(getattr(user, 'is_staff', False))
        b2b_klient = tenant if (profil == tenant or is_staff) else None
    else:
        b2b_klient = profil
            
    raw_zlava = getattr(b2b_klient, 'b2b_zlava_percenta', None) if b2b_klient else None

    try:
        zlava = Decimal(str(raw_zlava).strip().replace(',', '.')) if raw_zlava is not None else Decimal('0.00')
    except Exception:
        zlava = Decimal('0.00')

    if zlava < Decimal('0.00'):
        zlava = Decimal('0.00')
    elif zlava > Decimal('100.00'):
        zlava = Decimal('100.00')
    zlava = zlava.quantize(Decimal('0.01'))
    return b2b_klient, zlava

def get_eshop_models():
    """🛒 ISAS E-COMMERCE: Dynamicky vráti výhradne skladové modely v Evidencii a aktívnom odvetví."""
    try:
        cielove_modely = []
        app_labels = ['Evidencia']
        ind_mod = getattr(settings, 'INDUSTRY_MODULE', None)
        if ind_mod:
            app_labels.append(str(ind_mod).strip().lower())
        for label in app_labels:
            try:
                cfg = apps.get_app_config(label)
                for model in cfg.get_models():
                    if not model._meta.abstract and not model._meta.proxy:
                        fields = {f.name for f in model._meta.fields}
                        if 'zobrazit_na_webe' in fields and 'pocet_na_sklade' in fields:
                            cielove_modely.append(model)
            except Exception:
                continue
        return cielove_modely
    except Exception:
        return []

def get_work_shifts(gn, target_date):
    """📅 ISAS SMART BOOKING: Vypočíta začiatok, stred a koniec pracovnej doby pre daný deň."""
    if gn is None:
        try:
            GlobalneNastaveniaModel = apps.get_model('ISASaplikacia', 'GlobalneNastavenia')
            gn = GlobalneNastaveniaModel.objects.first()
        except Exception:
            gn = None

    t_start = getattr(gn, 'pracovny_cas_od', None)
    if isinstance(t_start, datetime):
        t_start = t_start.time()
    elif isinstance(t_start, str):
        try:
            t_start = dt_time.fromisoformat(t_start.strip())
        except Exception:
            t_start = dt_time(7, 30)
    elif not isinstance(t_start, dt_time):
        t_start = dt_time(7, 30)
    if t_start.tzinfo is not None or t_start.microsecond != 0:
        t_start = t_start.replace(tzinfo=None, microsecond=0)

    t_end = getattr(gn, 'pracovny_cas_do', None)
    if isinstance(t_end, datetime):
        t_end = t_end.time()
    elif isinstance(t_end, str):
        try:
            t_end = dt_time.fromisoformat(t_end.strip())
        except Exception:
            t_end = dt_time(16, 0)
    elif not isinstance(t_end, dt_time):
        t_end = dt_time(16, 0)
    if t_end.tzinfo is not None or t_end.microsecond != 0:
        t_end = t_end.replace(tzinfo=None, microsecond=0)
    
    # Ochrana pred chybným nastavením v ERP (Koniec pred začiatkom)
    if t_end <= t_start:
        t_start, t_end = dt_time(7, 30), dt_time(16, 0)
        
    # Defenzívna poistka pre target_date (podpora pre aware datetime, naive datetime, str aj date)
    def _extract_safe_date(dt_obj):
        if not dt_obj:
            return None
        if isinstance(dt_obj, datetime):
            if timezone.is_aware(dt_obj):
                if getattr(settings, 'USE_TZ', False):
                    c_cas = timezone.localtime(dt_obj) if (dt_obj and timezone.is_aware(dt_obj)) else dt_obj
                    return c_cas.date()
                try:
                    tz_name = getattr(settings, 'TIME_ZONE', 'UTC') or 'UTC'
                    return dt_obj.astimezone(zoneinfo.ZoneInfo(tz_name)).date()
                except Exception:
                    return dt_obj.date()
            return dt_obj.date()
        if isinstance(dt_obj, dt_date):
            return dt_obj
        return None

    if isinstance(target_date, datetime):
        c_date = _extract_safe_date(target_date)
    elif isinstance(target_date, str):
        s_date = target_date.strip()
        try:
            parsed_dt = datetime.fromisoformat(s_date)
            c_date = _extract_safe_date(parsed_dt)
        except Exception:
            try:
                c_date = dt_date.fromisoformat(s_date[:10])
            except Exception:
                c_date = None
    elif isinstance(target_date, dt_date):
        c_date = target_date
    else:
        c_date = None

    if isinstance(c_date, datetime):
        c_date = c_date.date()
    elif not isinstance(c_date, dt_date):
        if getattr(settings, 'USE_TZ', False):
            c_now = timezone.now()
            c_cas = timezone.localtime(c_now) if (c_now and timezone.is_aware(c_now)) else c_now
            c_date = c_cas.date()
        else:
            try:
                tz_name = getattr(settings, 'TIME_ZONE', 'UTC') or 'UTC'
                c_date = datetime.now(zoneinfo.ZoneInfo(tz_name)).date()
            except Exception:
                c_date = datetime.now().date()

    work_start = datetime.combine(c_date, t_start)
    work_end = datetime.combine(c_date, t_end)

    # 🛡️ ISAS ARCHITECT: Dynamický rešpekt voči nastaveniu USE_TZ (naive pri False, aware pri True)
    if getattr(settings, 'USE_TZ', False):
        work_start = timezone.make_aware(work_start) if not timezone.is_aware(work_start) else work_start
        work_end = timezone.make_aware(work_end) if not timezone.is_aware(work_end) else work_end
    else:
        # 🛡️ POISTKA PRE USE_TZ=False: timezone.make_naive() nesmie byť volané (vyvolalo by fatálny ValueError)
        if timezone.is_aware(work_start):
            try:
                tz_name = getattr(settings, 'TIME_ZONE', 'UTC') or 'UTC'
                work_start = work_start.astimezone(zoneinfo.ZoneInfo(tz_name)).replace(tzinfo=None)
            except Exception:
                work_start = work_start.replace(tzinfo=None)
        if timezone.is_aware(work_end):
            try:
                tz_name = getattr(settings, 'TIME_ZONE', 'UTC') or 'UTC'
                work_end = work_end.astimezone(zoneinfo.ZoneInfo(tz_name)).replace(tzinfo=None)
            except Exception:
                work_end = work_end.replace(tzinfo=None)
    
    midpoint = (work_start + (work_end - work_start) / 2).replace(microsecond=0)
    
    return work_start, midpoint, work_end

def get_active_web_blocks(cache_ver=1, is_preview=False):
    """🧱 Vráti množinu aktívnych typov WebBlok pre verziu LIVE alebo DRAFT (cachované podľa web_cache_version)."""
    verzia = 'DRAFT' if is_preview else 'LIVE'
    blocks_cache_key = f'isas_active_web_blocks_v{cache_ver}_{verzia.lower()}'
    cached_blocks = None
    try:
        cached_blocks = cache.get(blocks_cache_key)
    except Exception:
        cached_blocks = None

    if cached_blocks is not None:
        return set(cached_blocks)

    typy_list = []
    try:
        WebBlokModel = apps.get_model('WebBuilder', 'WebBlok')
        if WebBlokModel:
            typy_list = list(
                WebBlokModel.objects.filter(je_aktivny=True, verzia=verzia)
                .values_list('typ', flat=True)
                .distinct()
            )
    except Exception:
        typy_list = []

    try:
        cache.set(blocks_cache_key, typy_list, 3600)
    except Exception:
        pass
    return set(typy_list)


def get_nav_katalog_menu(cache_ver=1, is_preview=False, aktivne_bloky=None):
    """🧭 DATA-DRIVEN DYNAMICKÁ NAVIGÁCIA (Single Source of Truth viazaná na aktívne bloky a reálny obsah)."""
    ind_mod = str(getattr(settings, 'INDUSTRY_MODULE', 'generic') or 'generic').strip().lower()
    ver_tag = 'draft' if is_preview else 'live'
    katalog_cache_key = f'isas_nav_katalog_menu_v{cache_ver}_ind_{ind_mod}_{ver_tag}'
    nav_katalog_menu = None
    try:
        nav_katalog_menu = cache.get(katalog_cache_key)
    except Exception:
        nav_katalog_menu = None

    if nav_katalog_menu is None:
        nav_katalog_menu = []
        if aktivne_bloky is None:
            aktivne_bloky = get_active_web_blocks(cache_ver=cache_ver, is_preview=is_preview)

        def _url_exists(name):
            try:
                reverse(name)
                return True
            except (NoReverseMatch, Exception):
                return False

        # 1. Služby & Servis (iba ak je zapnutý blok SERVICES a existuje publikovaná služba so slugom)
        try:
            TypServisuPraceModel = apps.get_model('Evidencia', 'TypServisuPrace')
        except (LookupError, ValueError):
            TypServisuPraceModel = None

        if 'SERVICES' in aktivne_bloky and _url_exists('webbuilder:sluzby_list') and TypServisuPraceModel:
            try:
                if TypServisuPraceModel.objects.filter(zobrazit_na_webe=True).exclude(slug__isnull=True).exclude(slug__exact='').exists():
                    nav_katalog_menu.append({
                        'url_name': 'webbuilder:sluzby_list',
                        'ikona': '🛠️',
                        'nazov': get_industry_term('sluzby_nav_label', 'Služby & Servis'),
                    })
            except Exception:
                pass

        # 2. Náhradné diely / E-shop (iba ak je zapnutý blok PARTS a existuje zverejnená položka so slugom)
        if 'PARTS' in aktivne_bloky and _url_exists('webbuilder:eshop_list'):
            eshop_models = get_eshop_models()
            has_eshop_data = False
            for m in eshop_models:
                try:
                    m_fields = {f.name for f in m._meta.fields}
                    if 'slug' not in m_fields or 'zobrazit_na_webe' not in m_fields:
                        continue
                    if m.objects.filter(zobrazit_na_webe=True).exclude(slug__isnull=True).exclude(slug__exact='').exists():
                        has_eshop_data = True
                        break
                except Exception:
                    continue
            if has_eshop_data:
                nav_katalog_menu.append({
                    'url_name': 'webbuilder:eshop_list',
                    'ikona': '⚙️',
                    'nazov': get_industry_term('eshop_nav_label', get_industry_term('nav_katalog_label', 'Katalóg produktov')),
                })

        # 3. Požičovňa zariadení (iba ak je zapnutý blok RENTAL a existuje prenajímateľné zariadenie so slugom)
        if 'RENTAL' in aktivne_bloky and _url_exists('webbuilder:rental_list'):
            has_rental_data = False
            try:
                ServisovaneZariadenieModel = apps.get_model('Evidencia', 'ServisovaneZariadenie')
                if ServisovaneZariadenieModel:
                    has_rental_data = ServisovaneZariadenieModel.objects.filter(
                        je_urcene_na_prenajom=True, zobrazit_na_webe_prenajom=True
                    ).exclude(slug__isnull=True).exclude(slug__exact='').exists()
            except Exception:
                pass

            if has_rental_data:
                nav_katalog_menu.append({
                    'url_name': 'webbuilder:rental_list',
                    'ikona': get_industry_term('rental_fallback_icon', '🔄'),
                    'nazov': get_industry_term('rental_nav_label', get_industry_term('rental_title', 'Požičovňa zariadení')),
                })

        # 4. Inžinierske & Diagnostické Centrum (iba ak je zapnutý blok ENGINEERING_HUB a odvetvie podporuje aspoň 1 nástroj)
        if 'ENGINEERING_HUB' in aktivne_bloky and _url_exists('webbuilder:nastroje'):
            if is_esg_audit_supported() or HAS_HEAT_RECOVERY or HAS_VTZ_INSPECTION or HAS_FPLYNY_AUDIT or HAS_VIBRO_DIAGNOSTICS or HAS_CAPEX_CALCULATOR:
                nav_katalog_menu.append({
                    'url_name': 'webbuilder:nastroje',
                    'ikona': '🔬',
                    'nazov': get_industry_term('hub_nav_label', 'Inžinierske centrum'),
                })

        try:
            cache.set(katalog_cache_key, nav_katalog_menu, 300)
        except Exception:
            pass
    return nav_katalog_menu or []


def get_web_nav_flags(cache_ver=1, is_preview=False, aktivne_bloky=None):
    """🧭 Vráti príznaky viditeľnosti sekcií v hlavičke, pätičke a Hero banneri podľa aktívnych blokov a obsahu."""
    ver_tag = 'draft' if is_preview else 'live'
    flags_cache_key = f'isas_web_nav_flags_v{cache_ver}_{ver_tag}'
    cached_flags = None
    try:
        cached_flags = cache.get(flags_cache_key)
    except Exception:
        cached_flags = None

    if isinstance(cached_flags, dict):
        return cached_flags

    if aktivne_bloky is None:
        aktivne_bloky = get_active_web_blocks(cache_ver=cache_ver, is_preview=is_preview)

    dnes = timezone.localdate() if getattr(settings, 'USE_TZ', False) else timezone.now().date()

    show_referencie = False
    if 'REFERENCES' in aktivne_bloky:
        try:
            ServisnyZaznamModel = apps.get_model('Evidencia', 'ServisnyZaznam')
            if ServisnyZaznamModel:
                show_referencie = ServisnyZaznamModel.objects.filter(
                    zobrazit_na_webe=True, dokoncene=True
                ).filter(
                    (Q(foto_po__isnull=False) & ~Q(foto_po='')) |
                    (Q(foto_pred__isnull=False) & ~Q(foto_pred='')) |
                    (Q(video__isnull=False) & ~Q(video=''))
                ).exclude(slug__isnull=True).exclude(slug__exact='').exists()
        except Exception:
            show_referencie = False

    show_blog = False
    if 'BLOG_TEASER' in aktivne_bloky:
        try:
            ClanokModel = apps.get_model('WebBuilder', 'Clanok')
            if ClanokModel:
                qs_blog = ClanokModel.objects.all() if is_preview else ClanokModel.objects.filter(
                    je_aktivny=True, datum_vydania__lte=dnes
                )
                show_blog = qs_blog.exclude(slug__isnull=True).exclude(slug__exact='').exists()
        except Exception:
            show_blog = False

    show_kariera = False
    if 'CAREER' in aktivne_bloky:
        try:
            PoziciaModel = apps.get_model('Evidencia', 'PracovnaPozicia')
            if PoziciaModel:
                qs_poz = PoziciaModel.objects.all() if is_preview else PoziciaModel.objects.filter(
                    je_aktivna=True
                ).filter(Q(platnost_do__isnull=True) | Q(platnost_do__gte=dnes))
                show_kariera = qs_poz.exclude(slug__isnull=True).exclude(slug__exact='').exists()
        except Exception:
            show_kariera = False

    flags = {
        'show_referencie': show_referencie,
        'show_blog': show_blog,
        'show_kariera': show_kariera,
        'show_ticket': 'TICKET' in aktivne_bloky,
        'show_reklamacie': 'WARRANTY_CLAIM' in aktivne_bloky,
        'show_kontakt': 'CONTACT' in aktivne_bloky,
        'show_services_block': 'SERVICES' in aktivne_bloky,
    }

    try:
        cache.set(flags_cache_key, flags, 300)
    except Exception:
        pass
    return flags


def sanitizuj_verejne_html(html_vstup: str) -> str:
    """Odstráni nebezpečné tagy, event handlery (on*) a nebezpečné URL protokoly (javascript:, data:) z HTML pre |safe šablóny."""
    if not html_vstup:
        return ""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(str(html_vstup), "html.parser")
    zakazane_tagy = {
        "script", "style", "iframe", "object", "embed", "applet", "svg",
        "math", "form", "input", "button", "textarea", "select", "link", "meta", "base",
    }
    for tag in soup.find_all(list(zakazane_tagy)):
        tag.decompose()

    povolene_tagy = {
        "p", "br", "b", "strong", "i", "em", "u", "s", "del", "small", "mark",
        "sub", "sup", "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6",
        "blockquote", "pre", "code", "a", "img", "video", "source", "figure",
        "figcaption", "table", "thead", "tbody", "tr", "th", "td", "caption",
        "colgroup", "col", "hr", "span", "div",
    }
    url_atributy = {"href", "src", "poster"}

    for tag in list(soup.find_all(True)):
        if tag.name not in povolene_tagy:
            tag.unwrap()
            continue
        for attr in list(tag.attrs.keys()):
            attr_l = attr.lower()
            if attr_l.startswith("on") or attr_l in {"srcdoc", "formaction", "xlink:href"}:
                del tag.attrs[attr]
                continue
            if attr_l in url_atributy:
                val = str(tag.attrs.get(attr) or "").strip()
                val_norm = re.sub(r'[\x00-\x20]+', '', val).lower()
                if not val_norm.startswith(("https://", "http://", "/", "#", "mailto:", "tel:")):
                    del tag.attrs[attr]
    return str(soup)