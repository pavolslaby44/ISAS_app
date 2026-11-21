from django.core.cache import cache
from django.conf import settings
from decimal import Decimal
from django.apps import apps
from django.db.models import Sum
from django.utils import timezone

from .models import FiremnaIdentita
from .utils import get_google_maps_api_key, get_kurz_k_usd
from Evidencia.utils_helper import get_firemna_mena
from Evidencia.industry_loader import get_industry_term

def firemna_identita_web(request):
    """
    Globálny kontext procesor pre dynamické URL verejného webu:
    ⚡ 100% STABILNÝ: Využíva Cache (0 DB záťaž na bežné requesty).
    🛡️ EDGECASE-PROOF: Ošetruje localhost (HTTP), produkciu (HTTPS) aj zlé URL formáty.
    """
    cache_key = 'isas_dynamicky_web_firmy'
    try:
        web_url = cache.get(cache_key)
    except Exception:
        web_url = None

    if web_url is None:
        web_url = "/"
        
        # 1. Priorita: WebBuilder (pk=1 LIVE)
        try:
            WebNastaveniaModel = apps.get_model('WebBuilder', 'WebNastavenia')
            web_nastavenia = WebNastaveniaModel.objects.filter(pk=1).only('domena').first() if WebNastaveniaModel else None
            if web_nastavenia and web_nastavenia.domena and web_nastavenia.domena.strip():
                raw_domena = web_nastavenia.domena.strip()
                if raw_domena.startswith(('http://', 'https://')):
                    web_url = raw_domena.rstrip('/')
                else:
                    cista_domena = raw_domena.strip('/')
                    is_local = any(local_host in cista_domena for local_host in ['localhost', '127.0.0.1', '0.0.0.0'])
                    protokol = "http" if is_local else "https"
                    web_url = f"{protokol}://{cista_domena}"
        except Exception:
            web_url = "/"

        # 2. Fallback: FiremnaIdentita (ak WebBuilder nemá vyplnenú doménu)
        if web_url == "/":
            try:
                identita = FiremnaIdentita.objects.filter(pk=1).only('web').first() or FiremnaIdentita.objects.only('web').first()
                if identita and identita.web and identita.web.strip():
                    raw_web = identita.web.strip()
                    if raw_web.startswith(('http://', 'https://')):
                        web_url = raw_web.rstrip('/')
                    else:
                        cista_web = raw_web.strip('/')
                        is_local = any(local_host in cista_web for local_host in ['localhost', '127.0.0.1', '0.0.0.0'])
                        protokol = "http" if is_local else "https"
                        web_url = f"{protokol}://{cista_web}"
            except Exception:
                web_url = "/"

        # Uložíme do cache na 1 hodinu (automaticky sa premazáva pri uložení v admini)
        try:
            cache.set(cache_key, web_url, 3600)
        except Exception:
            pass

    # 🌍 MENOVÁ LOKALIZÁCIA: Dynamické načítanie meny firmy cez centrálny helper
    firemna_mena = get_firemna_mena()

    return {
        'dynamicky_web_firmy': web_url,
        'GOOGLE_MAPS_API_KEY': get_google_maps_api_key(),
        'firemna_mena': firemna_mena,
        'GAUGE_UNIT': get_industry_term('gauge_unit', 'LOAD'),
    }

def ai_costs_context(request):
    """
    Globálny kontext procesor pre zobrazenie nákladov na AI.
    🔒 HARD-SECURITY: Finančné náklady vidí výhradne Majiteľ a Administratíva.
    """
    if not request.user.is_authenticated or not request.user.is_staff:
        return {}
        
    user = request.user
    # Vyhodnotenie oprávnení na základe modelu Zamestnanec s ochranou voči RelatedObjectDoesNotExist
    zamestnanec = getattr(user, 'zamestnanec', None)
    typ_pomeru = getattr(zamestnanec, 'typ_pomeru', None)
    pracovna_rola = getattr(zamestnanec, 'pracovna_rola', None)

    is_majitel = user.is_superuser or (typ_pomeru == 'KO')
    is_admin = (pracovna_rola == 'AD')
    
    # Ak používateľ nie je majiteľ ani administrátor, vrátime prázdny kontext (skryjeme badge)
    if not (is_majitel or is_admin):
        return {}

    # 🌍 Dynamické zistenie meny firmy cez centrálny helper
    mena_firmy = get_firemna_mena()

    # ⚡ VÝKONNOSTNÁ CACHE: Výpočet nákladov sa robí max 1x za 5 minút
    cache_key = f'ai_naklady_mesiac_{mena_firmy.lower()}_cache'
    try:
        spolu_mena = cache.get(cache_key)
    except Exception:
        spolu_mena = None

    if spolu_mena is None:
        AIAuditLogModel = apps.get_model('Evidencia', 'AIAuditLog')
        if not AIAuditLogModel:
            return {}

        # 🛡️ ČASOVÁ INTEGRITA: Defenzívna normalizácia na lokálny čas bez naive/aware kolízie
        now = timezone.now()
        teraz = timezone.localtime(now) if (now and timezone.is_aware(now)) else now

        ai_naklady = AIAuditLogModel.objects.filter(
            cas_dopytu__year=teraz.year, 
            cas_dopytu__month=teraz.month,
            uspesne_dokoncene=True
        ).aggregate(spolu_usd=Sum('presna_cena_usd'))
        
        spolu_usd = ai_naklady.get('spolu_usd') or Decimal('0.00')

        # 💱 Živý dynamický kurz bez technického dlhu (s poctivým fallbackom na reálne USD)
        kurz = get_kurz_k_usd(mena_firmy)
        if kurz is not None:
            spolu_vysledok = (spolu_usd * kurz).quantize(Decimal('0.01'))
            vysledna_mena = mena_firmy
        else:
            spolu_vysledok = spolu_usd.quantize(Decimal('0.01'))
            vysledna_mena = 'USD'
        
        vysledok_balicek = {'suma': spolu_vysledok, 'mena': vysledna_mena}

        try:
            cache.set(cache_key, vysledok_balicek, 300)  # Uložené na 5 minút
        except Exception:
            pass
    else:
        vysledok_balicek = spolu_mena if isinstance(spolu_mena, dict) else {'suma': spolu_mena, 'mena': mena_firmy}

    return {
        'ai_naklady_mesiac': vysledok_balicek.get('suma', Decimal('0.00')),
        'ai_naklady_mena': vysledok_balicek.get('mena', mena_firmy),
        'ai_naklady_mesiac_eur': vysledok_balicek.get('suma', Decimal('0.00'))  # 🛡️ Zero-Regression spätná kompatibilita
    }