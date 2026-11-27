import json
import logging

from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import ObjectDoesNotExist

logger = logging.getLogger(__name__)
from django.db.models import F, Q
from django.http import JsonResponse
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe
from django.utils import timezone

from .models import WebNastavenia, WebBlok, WebFAQ, Clanok, WebhookEndpoint, WebhookLog
from ISASaplikacia.admin_core import (
    ISASPremiumAdmin,
    ISASSingletonAdminMixin,
    ISASReadOnlyAdminMixin,
    ISASBaseRelatedFilter,
    ISASRelatedZakaznikFilter,
    ISASDateRangeFilter,
)
from ISASaplikacia.utils import (
    render_isas_badge, render_isas_layout, render_isas_header_card,
    render_isas_singleton_card, render_isas_singleton_row, render_isas_key_value,
    render_isas_image, render_isas_text, render_isas_link_btn
)

class StavPublikacieClankuFilter(admin.SimpleListFilter):
    """100% defenzívny filter publikovania blogových článkov."""
    title = "🚀 Stav publikovania"
    parameter_name = "stav_publikacie"

    def lookups(self, request, model_admin):
        return (
            ('live', '🚀 Publikované (Verejné na webe)'),
            ('planovane', '⏳ Naplánované (Budúci dátum)'),
            ('koncept', '🔴 Koncept / Deaktivovaný'),
        )

    def queryset(self, request, queryset):
        today = timezone.now().date()
        val = self.value()
        if val == 'live':
            # Zohľadňuje aj prípady, kedy je článok aktívny a dátum vydania je dnes alebo nie je striktne zadaný
            return queryset.filter(je_aktivny=True).filter(Q(datum_vydania__lte=today) | Q(datum_vydania__isnull=True))
        if val == 'planovane':
            return queryset.filter(je_aktivny=True, datum_vydania__gt=today)
        if val == 'koncept':
            return queryset.filter(je_aktivny=False)
        return queryset


class CircuitBreakerStavFilter(admin.SimpleListFilter):
    """100% defenzívny filter stavu ochrany endpointov s ošetrením NULL a zablokovaných stavov."""
    title = "🛡️ Stav ochrany (Circuit Breaker)"
    parameter_name = "circuit_stav"

    def lookups(self, request, model_admin):
        return (
            ('zdrave', '🟢 Zdravé (0 zlyhaní)'),
            ('varovanie', '⚠️ Nestabilné (Zlyháva pod limitom)'),
            ('prerusene', '🚨 Prerušený obvod (Zablokované)'),
        )

    def queryset(self, request, queryset):
        val = self.value()
        if val == 'zdrave':
            return queryset.filter(je_aktivny=True).filter(Q(zlyhania_po_sebe=0) | Q(zlyhania_po_sebe__isnull=True))
        if val == 'varovanie':
            return queryset.filter(je_aktivny=True, zlyhania_po_sebe__gt=0, zlyhania_po_sebe__lt=F('max_zlyhani'))
        if val == 'prerusene':
            return queryset.filter(
                Q(je_aktivny=False) |
                Q(zlyhania_po_sebe__gte=F('max_zlyhani')) |
                (Q(dovod_deaktivacie__isnull=False) & ~Q(dovod_deaktivacie=''))
            )
        return queryset


class HttpStatusGroupFilter(admin.SimpleListFilter):
    """100% defenzívna kategorizácia HTTP odpovedí vrátane sieťových timeoutov (NULL a HTTP 0)."""
    title = "🌐 HTTP Odpoveď"
    parameter_name = "http_skupina"

    def lookups(self, request, model_admin):
        return (
            ('2xx', '🟢 2xx Úspech (Doručené)'),
            ('4xx', '⚠️ 4xx Chyba klienta (Zlá URL / Auth)'),
            ('5xx', '🔴 5xx Chyba cieľového servera'),
            ('timeout', '🔌 Bez odozvy (Timeout / Drop / HTTP 0)'),
        )

    def queryset(self, request, queryset):
        val = self.value()
        if val == '2xx':
            return queryset.filter(http_status__gte=200, http_status__lte=299)
        if val == '4xx':
            return queryset.filter(http_status__gte=400, http_status__lte=499)
        if val == '5xx':
            return queryset.filter(http_status__gte=500, http_status__lte=599)
        if val == 'timeout':
            return queryset.filter(Q(http_status__isnull=True) | Q(http_status=0))
        return queryset

class WebhookEndpointFilter(ISASBaseRelatedFilter):
    title, parameter_name, isas_default_field, isas_icon, isas_order_by = (
        '🔗 Endpoint', 'endpoint', 'endpoint', '🔗', 'nazov'
    )

@admin.register(WebNastavenia)
class WebNastaveniaAdmin(ISASSingletonAdminMixin, ISASPremiumAdmin):
    list_display = (
        'get_core_vizual',
        'get_brand_vizual',
        'get_seo_vizual'
    )
    list_display_links = ('get_core_vizual',)

    fieldsets = (
        ('🌐 1. DOMÉNA A SMEROVANIE', {
            'description': '<b>💡 ISAS Nápoveda:</b> Zadajte čistú verejnú doménu bez protokolu (napr. <code>indsas.com</code>). Náš Middleware na ňu automaticky presmeruje návštevníkov. 🌍',
            'fields': ('domena',)
        }),
        ('🎨 2. BRAND A VIZUÁLNA IDENTITA', {
            'description': '<b>✨ Vizuál:</b> Nastavte primárnu farbu tlačidiel, líšt a nahrajte reprezentatívne logo webu. 🖼️',
            'fields': (('hlavna_farba', 'logo'),)
        }),
        ('🚦 3. LIVE SLA A DOSTUPNOSŤ', {
            'description': '<b>FOMO Marketing:</b> Zobrazte zákazníkom v reálnom čase, aká je aktuálna vyťaženosť kapacít. Môžete definovať vlastné texty pre jednotlivé stavy, alebo ich nechať prázdne pre neutrálny systémový fallback. ⏱️',
            'fields': ('zobrazit_live_sla', ('sla_text_optimalna', 'sla_text_zvysena', 'sla_text_kriticka'))
        }),
        ('🔍 4. SEO A VYHĽADÁVAČE (GOOGLE)', {
            'description': '<b>📈 Optimalizácia:</b> Titulok a meta popis, ktoré sa zobrazia vo výsledkoch vyhľadávania na Google a pri zdieľaní na sociálnych sieťach. 📱',
            'fields': ('titulok_webu', 'meta_popis')
        }),
    )

    # --- ⚙️ ISAS CORE KONFIGURÁCIA ---
    isas_title = "⚙️ Nastavenia verejného webu"
    isas_preloader_text = "⏳ Vstupujete do sekcie konfigurácie webového servera..."
    isas_enable_detective = False
    isas_delete_success_message = "✨ Nastavenia webu boli úspešne premazané! 🧹"
    isas_magia_kwargs = {
        'typ_zobrazenia': 'karta',
        'vyzaduje_potvrdenie': True,
        'potvrdenie_text': "Tento krok natrvalo vymaže nastavenia verejného webu a vráti systém do východzieho stavu. 💥",
    }
    isas_modern_toggles = True

    # --- 🪝 HOOKS PRE FAST ACTION ---
    def fast_action_custom_validation(self, request, action_type, valid_ids, **kwargs):
        if not request.user.is_superuser:
            return JsonResponse({"status": "error", "message": "❌ Zásah do konfigurácie webu povolený iba pre Superadmina!"}, status=403)
        return None

    @admin.display(description='🌐 Web Core')
    def get_core_vizual(self, obj):
        # 🏷️ ARCHITEKTÚRA: Explicitné rozlíšenie LIVE (pk=1) vs DRAFT (pk=2)
        je_live = (getattr(obj, 'pk', None) == 1)
        verzia_badge = render_isas_badge("PRODUKCIA (LIVE)", "🚀", "success", wrapper=False) if je_live else render_isas_badge("KONCEPT (DRAFT)", "🧪", "warning", wrapper=False)
        has_domena = bool(getattr(obj, 'domena', None) and str(obj.domena).strip())
        status_badge = render_isas_badge("WEB ONLINE", "✅", "success", wrapper=False) if has_domena else render_isas_badge("ČAKÁ NA DOMÉNU", "⏳", "neutral", wrapper=False)
        domena_val = escape(obj.domena) if has_domena else render_isas_text("Nenastavená", muted=True)
        
        # 🛡️ DEFENSIVE: Vyhodnotenie konfigurácie vlastných SLA textov
        sla_aktivne = getattr(obj, 'zobrazit_live_sla', False)
        has_custom_sla = any([
            getattr(obj, 'sla_text_optimalna', None),
            getattr(obj, 'sla_text_zvysena', None),
            getattr(obj, 'sla_text_kriticka', None)
        ])
        sla_popis = " (Vlastné texty)" if has_custom_sla else " (Systémový fallback)"
        sla_val = render_isas_badge(f"LIVE SLA AKTÍVNE{sla_popis}", "⏱️", "warning", wrapper=False) if sla_aktivne else render_isas_text("Vypnuté", muted=True)

        # 🛡️ DEFENSIVE URL RESOLUTION: Bezpečná konštrukcia cieľovej URL pre lokálny vývoj aj produkciu
        raw_dom = str(getattr(obj, 'domena', '') or '').strip().lower()
        if raw_dom:
            clean_dom = raw_dom.replace('https://', '').replace('http://', '').split('/')[0].strip()
            proto = "http://" if getattr(settings, 'DEBUG', False) else "https://"
            # V lokálnom vývoji defenzívne doplníme dev port zo SITE_URL, ak nie je explicitne zadaný v doméne
            if getattr(settings, 'DEBUG', False) and ':' not in clean_dom:
                site_url = getattr(settings, 'SITE_URL', '') or ''
                clean_site = site_url.replace('https://', '').replace('http://', '').split('/')[0]
                dev_port = clean_site.split(':')[1] if ':' in clean_site else "8000"
                clean_dom = f"{clean_dom}:{dev_port}"
            target_url = f"{proto}{clean_dom}/"
            btn_tlacidlo = render_isas_link_btn(
                url=target_url,
                text="Otvoriť webstránku",
                icon="🌐",
                type_color="success",
                full_width=True,
                target="_blank",
                tooltip="Zobraziť verejnú stránku v novom okne"
            )
        else:
            btn_tlacidlo = render_isas_link_btn(
                url="#",
                text="Web bez domény",
                icon="🔒",
                type_color="dashed",
                full_width=True,
                tooltip="Pre zobrazenie stránky najprv zadajte a uložte verejnú doménu",
                custom_style="opacity: 0.6; cursor: not-allowed;"
            )

        obsah = render_isas_layout(format_html('{}{}{}{}{}{}',
            verzia_badge,
            status_badge,
            render_isas_key_value("Verejná Doména", domena_val, icon="🌍"),
            render_isas_key_value("Stav Smerovania", "Middleware aktívny" if has_domena else "Neaktívne", icon="🔀"),
            render_isas_key_value("FOMO / SLA Widget", sla_val, icon="🚦"),
            btn_tlacidlo
        ), layout="col", gap=8)
        
        karta_titul = "ISAS WEB ENGINE [LIVE]" if je_live else "ISAS WEB ENGINE [DRAFT]"
        karta_ikona = "🚀" if je_live else "🧪"
        return render_isas_header_card(karta_ikona, karta_titul, obsah)

    @admin.display(description='🎨 Brand a Vizuál')
    def get_brand_vizual(self, obj):
        # 🛡️ STABILITA: Kontrola .name zabráni ValueError, ak by v DB ostal visieť prázdny odkaz na súbor
        logo_html = render_isas_image(obj.logo.url, width=44, height=44) if (obj.logo and obj.logo.name) else render_isas_text("🚫 Bez loga", muted=True)
        farba_box = render_isas_text(obj.hlavna_farba, size=11, color="#0f172a", weight=800, dot_color=obj.hlavna_farba, align="left")

        r1 = render_isas_singleton_row(render_isas_layout(
            format_html('{}{}', 
                render_isas_key_value("Logo Webstránky", logo_html, icon="🖼️"),
                render_isas_key_value("Primárna Farba", farba_box, icon="🎨")
            ),
            layout="col", gap=6
        ), is_dashed=False)

        return render_isas_singleton_card("Vizuálna Identita", "🎨", "#3b82f6", r1)

    @admin.display(description='🔍 SEO a Google')
    def get_seo_vizual(self, obj):
        titulok = obj.titulok_webu if obj.titulok_webu else render_isas_text("Nenastavený", muted=True)
        popis = render_isas_text(f"{obj.meta_popis[:90]}..." if obj.meta_popis else "Nenastavený", muted=not obj.meta_popis, align="left")

        r1 = render_isas_singleton_row(render_isas_layout(format_html('{}{}',
            render_isas_key_value("Titulok (Title Tag)", titulok, icon="🏷️"),
            render_isas_key_value("Meta Popis (Description)", popis, icon="📄")
        ), layout="col", gap=6), is_dashed=False)

        return render_isas_singleton_card("SEO & Metadata", "🔍", "#10b981", format_html('{}', r1))

class WebBlokObrazokFilter(admin.SimpleListFilter):
    title = '🖼️ Obrázok / Grafika'
    parameter_name = 'ma_obrazok'

    def lookups(self, request, model_admin):
        return (
            ('ano', '🖼️ Obsahuje obrázok'),
            ('nie', '📝 Iba text (bez obrázka)'),
        )

    def queryset(self, request, queryset):
        if self.value() == 'ano':
            return queryset.exclude(Q(obrazok='') | Q(obrazok__isnull=True))
        if self.value() == 'nie':
            return queryset.filter(Q(obrazok='') | Q(obrazok__isnull=True))
        return queryset

@admin.register(WebBlok)
class WebBlokAdmin(ISASPremiumAdmin):
    isas_title = "🧱 Webové bloky"
    isas_preloader_text = "⏳ Vstupujete do sekcie web blokov..."
    isas_enable_fast_action = True
    isas_modern_toggles = True
    isas_clean_choice_fields = {
        'typ': '🧱 Zvoľte typ bloku',
        'verzia': '🏷️ Zvoľte verziu bloku',
    }
    _full_lock = ['poradie', 'nadpis', 'podnadpis', 'tlacidlo_text', 'obrazok']
    _cta_media_lock = {
        'lock': ['tlacidlo_text', 'obrazok'],
        'unlock': ['poradie', 'nadpis', 'podnadpis'],
    }
    _subpage_lock = {
        'lock': ['poradie'],
        'unlock': ['nadpis', 'podnadpis', 'tlacidlo_text', 'obrazok'],
    }
    isas_value_based_locks = {
        'trigger_field': 'typ',
        'soft_lock': True,
        'rules': {
            'WARRANTY_CLAIM': {'lock': _full_lock},
            'TICKET': {'lock': _full_lock},
            'BRANDS': {
                'lock': ['podnadpis', 'tlacidlo_text', 'obrazok'],
                'unlock': ['poradie', 'nadpis'],
            },
            'SERVICES': _cta_media_lock,
            'RENTAL': _cta_media_lock,
            'PARTS': _cta_media_lock,
            'REFERENCES': _cta_media_lock,
            'TEAM': _cta_media_lock,
            'ENGINEERING_HUB': _cta_media_lock,
            'CONTACT': _cta_media_lock,
            'IOT_DEMO': _cta_media_lock,
            'BOOKING': _cta_media_lock,
            'FAQ': _cta_media_lock,
            'BLOG_TEASER': _cta_media_lock,
            'SLA_CALCULATOR': {
                'lock': ['poradie', 'tlacidlo_text', 'obrazok'],
                'unlock': ['nadpis', 'podnadpis'],
            },
            'BUYBACK': _subpage_lock,
        },
        'default_rule': {
            'unlock': _full_lock,
        },
    }
    del _full_lock, _cta_media_lock, _subpage_lock

    isas_delete_success_message = "✨ Webové bloky boli úspešne odstránené!"
    isas_magia_kwargs = {
        'live_search_endpoint': "detective-search/",
        'live_search_ikona': "🧱",
    }

    list_display = ['get_poradie_vizual', 'get_typ_vizual', 'get_verzia_vizual', 'get_nadpis_vizual', 'get_media_vizual', 'je_aktivny']
    list_display_links = ['get_poradie_vizual', 'get_typ_vizual', 'get_nadpis_vizual']
    list_select_related = []
    list_filter = ['verzia', 'typ', 'je_aktivny', WebBlokObrazokFilter]
    search_fields = ('nadpis', 'podnadpis', 'tlacidlo_text')
    ordering = ('poradie', 'pk')

    fieldsets = (
        ('🧱 1. TYP A ZOBRAZENIE', {
            'description': '<b>💡 ISAS Nápoveda:</b> Zvoľte funkciu bloku, jeho verziu a poradie od vrchu stránky (nižšie číslo = vyššia pozícia). 🔢',
            'fields': (('typ', 'verzia'), ('poradie', 'je_aktivny'))
        }),
        ('📝 2. TEXTOVÝ OBSAH', {
            'description': '<b>✍️ Copywriting:</b> Hlavný nadpis a podrobný text/podnadpis pre tento blok. 📄',
            'fields': ('nadpis', 'podnadpis')
        }),
        ('🖼️ 3. MÉDIÁ A INTERAKCIA', {
            'description': '<b>🔘 Výzva k akcii (CTA):</b> Text na tlačidle (platí najmä pre Hero banner) a ilustračný obrázok na pozadí. 🚀',
            'fields': (('tlacidlo_text', 'obrazok'),)
        }),
    )

    # --- 🪝 HOOKS PRE FAST ACTION ---
    def fast_action_custom_validation(self, request, action_type, valid_ids, **kwargs):
        # 🛡️ BEZPEČNOSŤ: Kontrolujeme reálne databázové oprávnenie, nie iba prístup do adminu
        if action_type == 'delete' and not request.user.has_perm('WebBuilder.delete_webblok'):
            return JsonResponse({"status": "error", "message": "⛔ Nemáte oprávnenie na mazanie webových blokov."}, status=403)
        return None

    # --- 🕵️ ISAS DETECTIVE SEARCH (Live Search API) ---
    def get_detective_queryset(self, request, term):
        return self.get_queryset(request).filter(
            Q(nadpis__icontains=term) | 
            Q(typ__icontains=term) |
            Q(podnadpis__icontains=term) |
            Q(tlacidlo_text__icontains=term)
        )[:10]

    def format_detective_result(self, obj):
        safe_name = obj.nadpis or obj.get_typ_display()
        return {
            'id': obj.id,
            'name': safe_name,
            'search_value': safe_name,
            'cat': 'WEB BLOK',
            'rola': 'Aktívny ✅' if obj.je_aktivny else 'Neaktívny ❌'
        }

    @admin.display(description="🔢 Pozícia", ordering="poradie")
    def get_poradie_vizual(self, obj):
        return render_isas_badge(
            text=str(obj.poradie),
            prefix_text="Pozícia:",
            icon="🔢",
            type_color="neutral",
            monospace=True,
            wrapper=False
        )

    @admin.display(description="🧱 Typ bloku", ordering="typ")
    def get_typ_vizual(self, obj):
        farby = {
            'HERO': 'purple',
            'ABOUT': 'info',
            'SERVICES': 'success',
            'RENTAL': 'primary',
            'PARTS': 'warning',
            'BRANDS': 'warning',
            'REFERENCES': 'primary',
            'TEAM': 'info',
            'TICKET': 'dark',
            'ENGINEERING_HUB': 'dark',
            'CONTACT': 'danger',
            'IOT_DEMO': 'info',
            'CAREER': 'success',
            'SLA_CALCULATOR': 'warning',
            'BUYBACK': 'primary',
            'WARRANTY_CLAIM': 'danger',
            'BOOKING': 'success',
            'FAQ': 'info',
            'BLOG_TEASER': 'primary'
        }
        return render_isas_badge(obj.get_typ_display(), type_color=farby.get(obj.typ, 'neutral'), wrapper=False)

    @admin.display(description="🏷️ Verzia", ordering="verzia")
    def get_verzia_vizual(self, obj):
        konfiguracia_verzie = {
            'LIVE': {'color': 'success', 'icon': '🚀'},
            'DRAFT': {'color': 'warning', 'icon': '🧪'},
        }
        cfg = konfiguracia_verzie.get(obj.verzia, {'color': 'neutral', 'icon': '🏷️'})
        popis_verzie = escape(obj.get_verzia_display() or "Nenastavená")
        return render_isas_badge(
            text=popis_verzie,
            icon=cfg['icon'],
            type_color=cfg['color'],
            wrapper=False
        )

    @admin.display(description="📝 Obsah bloku")
    def get_nadpis_vizual(self, obj):
        nadpis = render_isas_text(obj.nadpis or "(Bez nadpisu)", size=13, weight=900, color="#0f172a", align="left", muted=not obj.nadpis)
        podnadpis = render_isas_text(f"{obj.podnadpis[:70]}..." if obj.podnadpis else "", size=10, color="#64748b", weight=600, align="left") if obj.podnadpis else ""
        return render_isas_layout(format_html('{}{}', nadpis, podnadpis), layout="col", gap=4, center=False)

    @admin.display(description="🖼️ Médiá a CTA")
    def get_media_vizual(self, obj):
        items = []
        # 🛡️ STABILITA: Kontrola .name zabráni ValueError
        if obj.obrazok and obj.obrazok.name:
            items.append(render_isas_image(obj.obrazok.url, width=40, height=40))
        if obj.tlacidlo_text:
            items.append(render_isas_link_btn("#", obj.tlacidlo_text, type_color="dark", wrapper=False))
            
        if not items:
            return render_isas_text("Bez médií", muted=True)
        return render_isas_layout(mark_safe("".join(items)), layout="row", gap=8, center=True)

@admin.register(WebFAQ)
class WebFAQAdmin(ISASPremiumAdmin):
    isas_title = "❓ Často kladené otázky (FAQ)"
    isas_preloader_text = "⏳ Vstupujete do sekcie otázok a odpovedí..."
    isas_enable_fast_action = True
    isas_modern_toggles = True
    isas_delete_success_message = "✨ FAQ otázky boli úspešne odstránené!"
    isas_clean_choice_fields = {
        'kategoria': '📂 Zvoľte kategóriu otázky',
    }
    isas_magia_kwargs = {
        'live_search_endpoint': "detective-search/",
        'live_search_ikona': "❓",
    }

    list_display = ['get_poradie_vizual', 'get_kategoria_vizual', 'get_otazka_vizual', 'je_aktivna']
    list_display_links = ['get_otazka_vizual']
    list_filter = ['kategoria', 'je_aktivna']
    search_fields = ['otazka', 'odpoved', 'kategoria', 'slug']
    ordering = ['kategoria', 'poradie', '-id']
    readonly_fields = ['slug']

    fieldsets = (
        ('❓ Otázka a Odpoveď', {
            'fields': ('kategoria', 'otazka', 'odpoved')
        }),
        ('⚙️ Nastavenia zobrazenia', {
            'fields': ('slug', 'poradie', 'je_aktivna')
        }),
    )

    def fast_action_custom_validation(self, request, action_type, valid_ids, **kwargs):
        if action_type == 'delete' and not request.user.has_perm('WebBuilder.delete_webfaq'):
            return JsonResponse({"status": "error", "message": "⛔ Nemáte oprávnenie na mazanie FAQ položiek."}, status=403)
        return None

    # --- 🕵️ ISAS DETECTIVE SEARCH (Live Search API) ---
    def get_detective_queryset(self, request, term):
        return self.get_queryset(request).filter(
            Q(otazka__icontains=term) |
            Q(odpoved__icontains=term) |
            Q(kategoria__icontains=term) |
            Q(slug__icontains=term)
        ).distinct()[:10]

    def format_detective_result(self, obj):
        safe_name = obj.otazka or "(Bez otázky)"
        kategoria_nazov = obj.get_kategoria_display() if hasattr(obj, 'get_kategoria_display') else str(obj.kategoria or "VŠEOBECNÉ")
        return {
            'id': obj.id,
            'name': safe_name,
            'search_value': safe_name,
            'cat': f"❓ {kategoria_nazov.upper()}",
            'rola': 'Aktívna ✅' if obj.je_aktivna else 'Skrytá ❌'
        }

    @admin.display(description="🔢 Pozícia", ordering="poradie")
    def get_poradie_vizual(self, obj):
        return render_isas_badge(
            text=str(obj.poradie),
            icon="🔢",
            type_color="neutral",
            monospace=True,
            wrapper=False
        )

    @admin.display(description="📂 Kategória", ordering="kategoria")
    def get_kategoria_vizual(self, obj):
        return render_isas_badge(
            text=obj.get_kategoria_display() if hasattr(obj, 'get_kategoria_display') else str(obj.kategoria or "Všeobecné"),
            icon="📂",
            type_color="info",
            wrapper=False
        )

    @admin.display(description="❓ Otázka a Odpoveď", ordering="otazka")
    def get_otazka_vizual(self, obj):
        otazka = render_isas_text(escape(obj.otazka or ""), size=13, weight=800, color="#0f172a", align="left")
        odpoved_nahlad = escape(f"{obj.odpoved[:90]}..." if obj.odpoved and len(obj.odpoved) > 90 else (obj.odpoved or ""))
        odpoved = render_isas_text(odpoved_nahlad, size=10, color="#64748b", weight=500, align="left") if odpoved_nahlad else ""
        return render_isas_layout(format_html('{}{}', otazka, odpoved), layout="col", gap=2, center=False)

@admin.register(Clanok)
class ClanokAdmin(ISASPremiumAdmin):
    isas_title = "📰 Odborné články (Blog)"
    isas_preloader_text = "⏳ Vstupujete do sekcie článkov a blogov..."
    isas_enable_fast_action = True
    isas_modern_toggles = True
    isas_delete_success_message = "✨ Odborné články boli úspešne odstránené!"
    isas_clean_choice_fields = {
        'kategoria': '📂 Zvoľte kategóriu článku',
    }
    isas_autocomplete_fk_fields = {
        'autor': '✍️ Začnite písať meno autora...',
    }
    isas_magia_kwargs = {
        'live_search_endpoint': "detective-search/",
        'live_search_ikona': "📰",
    }

    list_display = ['get_status_vizual', 'get_obrazok_vizual', 'get_nadpis_vizual', 'get_kategoria_vizual', 'autor', 'datum_vydania', 'je_aktivny']
    list_display_links = ['get_nadpis_vizual']
    list_select_related = ['autor']
    autocomplete_fields = ('autor',)
    isas_date_filter_field = 'datum_vydania'
    list_filter = [StavPublikacieClankuFilter, 'kategoria', 'je_aktivny', ISASDateRangeFilter]
    search_fields = ['nadpis', 'perex', 'obsah', 'autor__meno', 'autor__priezvisko', 'seo_titulok', 'meta_popis']
    ordering = ('-datum_vydania', '-id')
    readonly_fields = ['slug']

    fieldsets = (
        ('📝 1. HLAVNÝ OBSAH', {
            'description': '<b>💡 ISAS Copywriting:</b> Nadpis, kategória a hlavný text článku s podporou formátovania.',
            'fields': (('nadpis', 'kategoria'), 'autor', 'perex', 'obsah')
        }),
        ('🖼️ 2. ILUSTRAČNÝ OBRÁZOK', {
            'description': '<b>📸 Vizuál:</b> Titulný obrázok článku a alternatívny text pre Google Obrázky a nevidiacich.',
            'fields': (('obrazok', 'obrazok_alt'),)
        }),
        ('🔍 3. SEO A VYHĽADÁVAČE (GOOGLE)', {
            'description': '<b>📈 Optimalizácia:</b> Špecifický titulok a meta popis pre zobrazenie vo výsledkoch Google vyhľadávania.',
            'fields': ('seo_titulok', 'meta_popis')
        }),
        ('⚙️ 4. PUBLIKOVANIE A SMEROVANIE', {
            'description': '<b>📅 Plánovanie:</b> Nastavte dátum vydania. Článok s budúcim dátumom sa automaticky zverejní až v daný deň.',
            'fields': (('datum_vydania', 'je_aktivny'), 'slug')
        }),
    )

    # --- 🪝 HOOKS PRE FAST ACTION ---
    def fast_action_custom_validation(self, request, action_type, valid_ids, **kwargs):
        if action_type == 'delete' and not request.user.has_perm('WebBuilder.delete_clanok'):
            return JsonResponse({"status": "error", "message": "⛔ Nemáte oprávnenie na mazanie odborných článkov."}, status=403)
        return None

    # --- 🕵️ ISAS DETECTIVE SEARCH (Live Search API) ---
    def get_detective_queryset(self, request, term):
        return self.get_queryset(request).select_related('autor').filter(
            Q(nadpis__icontains=term) |
            Q(perex__icontains=term) |
            Q(obsah__icontains=term) |
            Q(slug__icontains=term) |
            Q(kategoria__icontains=term) |
            Q(seo_titulok__icontains=term) |
            Q(autor__meno__icontains=term) |
            Q(autor__priezvisko__icontains=term)
        ).distinct()[:10]

    def format_detective_result(self, obj):
        safe_name = obj.nadpis or "(Bez nadpisu)"
        kategoria_nazov = obj.get_kategoria_display() or "BLOG"

        # Dynamické rozlíšenie stavu publikovania pre Detective lištu
        if not obj.je_aktivny:
            stav_badge = "🔴 Koncept"
        elif obj.datum_vydania and obj.datum_vydania > timezone.now().date():
            stav_badge = f"⏳ Plánované ({obj.datum_vydania.strftime('%d.%m.')})"
        else:
            stav_badge = "🚀 Publikované"

        autor_info = f" • ✍️ {obj.autor}" if obj.autor else ""
        cas_info = f" • ⏱️ {obj.cas_citania} min" if getattr(obj, 'cas_citania', None) else ""

        return {
            'id': obj.id,
            'name': safe_name,
            'search_value': safe_name,
            'cat': f"📰 {kategoria_nazov.upper()}",
            'rola': f"{stav_badge}{autor_info}{cas_info}"
        }

    @admin.display(description="🚦 Stav", ordering="datum_vydania")
    def get_status_vizual(self, obj):
        if not obj.je_aktivny:
            return render_isas_badge("Koncept / Neaktívny", icon="🔴", type_color="danger", wrapper=False)
        if obj.datum_vydania and obj.datum_vydania > timezone.now().date():
            return render_isas_badge(f"Plánované ({obj.datum_vydania.strftime('%d.%m.%Y')})", icon="⏳", type_color="warning", wrapper=False)
        return render_isas_badge("Publikované", icon="🚀", type_color="success", wrapper=False)

    @admin.display(description="🖼️ Titulka")
    def get_obrazok_vizual(self, obj):
        if obj.obrazok and obj.obrazok.name:
            return render_isas_image(obj.obrazok.url, width=44, height=44, alt=obj.obrazok_alt or obj.nadpis)
        return render_isas_text("🚫 Bez fotky", muted=True)

    @admin.display(description="📝 Článok", ordering="nadpis")
    def get_nadpis_vizual(self, obj):
        nadpis_text = escape(obj.nadpis or "(Bez nadpisu)")
        nadpis = render_isas_text(nadpis_text, size=13, weight=800, color="#0f172a", align="left", muted=not obj.nadpis)
        cas = getattr(obj, 'cas_citania', None)
        citanie = render_isas_text(f"⏱️ cca {cas} min. čítania", size=10, color="#64748b", weight=600, align="left") if cas else ""
        return render_isas_layout(format_html('{}{}', nadpis, citanie), layout="col", gap=2, center=False)

    @admin.display(description="📂 Kategória", ordering="kategoria")
    def get_kategoria_vizual(self, obj):
        farby = {
            'NOVINKY': 'purple',
            'NAVODY': 'success',
            'TECHNOLOGIE': 'info',
            'PRIPADOVE_STUDIE': 'primary'
        }
        kategoria_text = escape(obj.get_kategoria_display() or "Nezaradené")
        return render_isas_badge(kategoria_text, type_color=farby.get(obj.kategoria, 'neutral'), wrapper=False)

class WebhookLogInline(admin.TabularInline):
    model = WebhookLog
    extra = 0
    fields = ('cas_odoslania', 'event_id', 'pokus', 'http_status', 'trvanie_ms', 'uspesne')
    readonly_fields = ('cas_odoslania', 'event_id', 'pokus', 'http_status', 'trvanie_ms', 'uspesne')
    can_delete = False
    max_num = 0

    def get_queryset(self, request):
        # 🛡️ OCHRANA PRED OOM & ASSERTION ERROR: Bezpečné obmedzenie na 15 logov bez zneplatnenia QuerySetu
        qs = super().get_queryset(request)
        posledne_ids = list(qs.order_by('-cas_odoslania').values_list('id', flat=True)[:15])
        return qs.filter(id__in=posledne_ids).order_by('-cas_odoslania')

@admin.register(WebhookEndpoint)
class WebhookEndpointAdmin(ISASPremiumAdmin):
    isas_title = "🔗 API Integrácie (Webhooks)"
    isas_preloader_text = "⏳ Vstupujete do sekcie webhook endpointov..."
    isas_enable_fast_action = True
    isas_modern_toggles = True
    isas_clean_choice_fields = {
        'udalost': '⚡ Zvoľte sledovanú udalosť',
    }
    isas_autocomplete_fk_fields = {
        'zakaznik': '🏢 Začnite písať názov partnera...',
    }
    isas_magia_kwargs = {
        'live_search_endpoint': "detective-search/",
        'live_search_ikona': "🔗",
        'custom_buttons_html': '<button type="button" id="btn-test-ping" class="isas-action-btn btn-blue" data-action="test_ping">🏓 Test Ping</button>',
        'ignore_default_action_ids': ['btn-test-ping'],
    }

    isas_action_modals = {
        'btn-test-ping': {
            'type': 'direct_action',
            'action': 'test_ping',
            'confirm': True,
            'confirm_title': 'Test spojenia (Ping) 🏓',
            'confirm_text': 'Naozaj chcete odoslať testovací ping signál na vybrané webhook endpointy?',
            'loading_text': '⏳ ODOSIELAM PING...',
            'success_text': '🏓 PING ODOSLANÝ',
            'require_selection': True,
            'min_selection': 1,
            'error_msg': 'Vyberte aspoň jeden webhook endpoint na testovanie.',
            'reload': False
        }
    }

    # --- 🕵️ ISAS DETECTIVE SEARCH (Live Search API) ---
    def get_detective_queryset(self, request, term):
        return self.get_queryset(request).filter(
            Q(nazov__icontains=term) |
            Q(url__icontains=term) |
            Q(udalost__icontains=term) |
            Q(zakaznik__Nazov_spolocnosti__icontains=term)
        ).select_related('zakaznik')[:10]

    def format_detective_result(self, obj):
        klient_nazov = getattr(obj.zakaznik, 'Nazov_spolocnosti', str(obj.zakaznik))
        nazov_endpointu = obj.nazov or obj.url
        safe_name = f"{klient_nazov} — {nazov_endpointu}"
        udalost_label = obj.get_udalost_display() if hasattr(obj, 'get_udalost_display') else str(obj.udalost)
        
        return {
            'id': obj.id,
            'name': safe_name,
            'search_value': safe_name,
            'cat': f"🔗 {udalost_label}",
            'rola': 'Aktívny ✅' if obj.je_aktivny else 'Vypnutý 🔴'
        }

    list_display = ['get_zakaznik_vizual', 'get_endpoint_vizual', 'get_udalost_vizual', 'get_status_vizual', 'get_circuit_vizual']
    list_display_links = ['get_endpoint_vizual']
    list_select_related = ['zakaznik']
    autocomplete_fields = ('zakaznik',)
    list_filter = ['je_aktivny', CircuitBreakerStavFilter, 'udalost', ISASRelatedZakaznikFilter]
    search_fields = ['zakaznik__Nazov_spolocnosti', 'nazov', 'url']
    readonly_fields = ['zlyhania_po_sebe', 'dovod_deaktivacie', 'get_dokumentacia']
    inlines = [WebhookLogInline]

    fieldsets = (
        ('🏢 Zákazník a Udalosť', {
            'fields': ('zakaznik', 'nazov', 'udalost', 'je_aktivny')
        }),
        ('🔗 Konfigurácia Endpointu', {
            'fields': ('url', 'tajny_kluc', 'vlastne_hlavicky')
        }),
        ('🛡️ Circuit Breaker (Ochrana)', {
            'fields': ('max_zlyhani', 'zlyhania_po_sebe', 'dovod_deaktivacie'),
            'classes': ('collapse',)
        }),
        ('📖 Dokumentácia pre IT oddelenie klienta', {
            'fields': ('get_dokumentacia',),
            'classes': ('collapse',)
        }),
    )

    def perform_fast_action(self, request, action_type, valid_ids, **kwargs):
        if action_type == 'test_ping':
            if not self.has_change_permission(request):
                return JsonResponse({"status": "error", "message": "⛔ Nemáte oprávnenie na testovanie webhook endpointov."}, status=403)
            from .tasks import send_ping_webhook_task
            endpoints = self.model.objects.filter(id__in=valid_ids, je_aktivny=True)
            odoslane = 0
            chyby_broker = 0
            for endpoint in endpoints:
                try:
                    send_ping_webhook_task.delay(endpoint.id)
                    odoslane += 1
                except Exception as exc:
                    chyby_broker += 1
                    logger.warning(f"Výpadok Celery/Redis brokera pri odosielaní pingu pre endpoint #{endpoint.id}: {exc}")
                    messages.warning(request, f"Výpadok brokera: Testovací ping pre endpoint #{endpoint.id} sa nepodarilo zaradiť do fronty.")

            if odoslane > 0:
                sprava = f"🏓 Úspešne odoslaný Test Ping na {odoslane} aktívnych endpointov."
                if chyby_broker > 0:
                    sprava += f" (⚠️ {chyby_broker} zlyhalo kvôli výpadku brokera)."
                return JsonResponse({"status": "success", "message": sprava})
            if chyby_broker > 0:
                return JsonResponse({"status": "error", "message": "⚠️ Nepodarilo sa odoslať Test Ping kvôli nedostupnosti Celery/Redis brokera."}, status=503)
            return JsonResponse({"status": "error", "message": "⚠️ Medzi označenými záznamami sa nenachádzajú žiadne aktívne endpointy."}, status=400)

        return super().perform_fast_action(request, action_type, valid_ids, **kwargs)

    @admin.display(description="🏢 Zákazník", ordering="zakaznik__Nazov_spolocnosti")
    def get_zakaznik_vizual(self, obj):
        if not obj.zakaznik:
            return render_isas_text("Globálny / Bez partnera", muted=True)
        nazov = escape(getattr(obj.zakaznik, 'Nazov_spolocnosti', str(obj.zakaznik)))
        return render_isas_badge(nazov, icon="🏢", type_color="info", wrapper=False)

    @admin.display(description="🔗 Názov a URL endpointu", ordering="nazov")
    def get_endpoint_vizual(self, obj):
        nazov = render_isas_text(escape(obj.nazov or "Bez názvu"), size=13, weight=800, color="#0f172a", align="left")
        url_text = escape(obj.url or "")
        url_badge = render_isas_badge(url_text, icon="🌐", monospace=True, type_color="neutral", copy_value=obj.url, wrapper=False) if obj.url else ""
        return render_isas_layout(format_html('{}{}', nazov, url_badge), layout="col", gap=3, center=False)

    @admin.display(description="⚡ Typ udalosti", ordering="udalost")
    def get_udalost_vizual(self, obj):
        label = escape(obj.get_udalost_display() if hasattr(obj, 'get_udalost_display') else str(obj.udalost))
        return render_isas_badge(label, icon="⚡", type_color="purple", wrapper=False)

    @admin.display(description="🛡️ Circuit Breaker", ordering="zlyhania_po_sebe")
    def get_circuit_vizual(self, obj):
        zlyhania = obj.zlyhania_po_sebe or 0
        max_limit = obj.max_zlyhani or 10
        stav_color = "danger" if zlyhania >= max_limit else ("warning" if zlyhania > 0 else "neutral")
        return render_isas_badge(
            text=f"{zlyhania} / {max_limit}",
            icon="🛡️",
            prefix_text="Zlyhania:",
            type_color=stav_color,
            monospace=True,
            wrapper=False
        )

    @admin.display(description="🚦 Stav")
    def get_status_vizual(self, obj):
        if not obj.je_aktivny:
            popis = f"Vypnuté ({obj.dovod_deaktivacie})" if obj.dovod_deaktivacie else "Vypnuté"
            return render_isas_badge(popis, icon="🔴", type_color="danger", wrapper=False)
        zlyhania = obj.zlyhania_po_sebe or 0
        if zlyhania > 0:
            max_limit = obj.max_zlyhani or 10
            return render_isas_badge(f"Zlyháva ({zlyhania}/{max_limit})", icon="⚠️", type_color="warning", wrapper=False)
        return render_isas_badge("Aktívne", icon="✅", type_color="success", wrapper=False)

    @admin.display(description="📖 Ukážka overenia podpisu (HMAC SHA-256)")
    def get_dokumentacia(self, obj):
        secret_raw = (obj.tajny_kluc or "").replace('\\', '\\\\').replace('"', '\\"')
        kod = (
            "import hmac\n"
            "import hashlib\n\n"
            "def verify_isas_webhook(payload_body, received_signature):\n"
            f"    secret = \"{secret_raw}\"\n"
            "    expected_signature = hmac.new(\n"
            "        secret.encode('utf-8'),\n"
            "        payload_body.encode('utf-8'),\n"
            "        hashlib.sha256\n"
            "    ).hexdigest()\n\n"
            "    return hmac.compare_digest(expected_signature, received_signature)"
        )
        return render_isas_text(kod, size=11, color="#0f172a", custom_style="font-family: monospace; white-space: pre; background: #f8fafc; padding: 12px; border-radius: 8px; border: 1px solid #cbd5e1; display: block;")

@admin.register(WebhookLog)
class WebhookLogAdmin(ISASReadOnlyAdminMixin, ISASPremiumAdmin):
    isas_title = "📜 Záznamy Webhookov (Logy)"
    isas_preloader_text = "⏳ Vstupujete do sekcie histórie doručenia webhookov..."
    isas_enable_fast_action = True
    isas_magia_kwargs = {
        'live_search_endpoint': "detective-search/",
        'live_search_ikona': "📜",
        'bez_tlacidla_odstranit': True,
        'custom_buttons_html': '<button type="button" id="btn-resend-failed" class="isas-action-btn btn-orange" data-action="resend_failed">🔄 Znovu odoslať zlyhané</button>',
        'ignore_default_action_ids': ['btn-resend-failed'],
    }

    def fast_action_custom_validation(self, request, action_type, valid_ids, **kwargs):
        if action_type == 'delete':
            return JsonResponse({"status": "error", "message": "🔒 Auditné záznamy webhookov nie je možné mazať."}, status=403)
        return None

    isas_action_modals = {
        'btn-resend-failed': {
            'type': 'direct_action',
            'action': 'resend_failed',
            'confirm': True,
            'confirm_title': 'Opätovné odoslanie webhookov 🔄',
            'confirm_text': 'Naozaj chcete znovu zaradiť vybrané zlyhané webhooky do odosielacieho radu?',
            'loading_text': '⏳ ZARAĎUJEM...',
            'success_text': '🔄 ZARADENÉ',
            'require_selection': True,
            'min_selection': 1,
            'error_msg': 'Vyberte aspoň jeden zlyhaný záznam zo zoznamu.',
            'reload': True
        }
    }

    # --- 🕵️ ISAS DETECTIVE SEARCH (Live Search API) ---
    def get_detective_queryset(self, request, term):
        return self.get_queryset(request).filter(
            Q(event_id__icontains=term) |
            Q(endpoint__nazov__icontains=term) |
            Q(endpoint__zakaznik__Nazov_spolocnosti__icontains=term) |
            Q(odpoved_servera__icontains=term) |
            Q(http_status__icontains=term)
        ).select_related('endpoint', 'endpoint__zakaznik')[:10]

    def format_detective_result(self, obj):
        endpoint_nazov = obj.endpoint.nazov if obj.endpoint else "Neznámy endpoint"
        event_str = str(obj.event_id)
        safe_name = f"#{event_str[:8]} • {endpoint_nazov}"
        
        status_label = f"HTTP {obj.http_status}" if obj.http_status else "BEZ ODPOVEDE"
        vysledok = "Doručené ✅" if obj.uspesne else "Zlyhalo ❌"
        cas_str = obj.cas_odoslania.strftime('%d.%m. %H:%M:%S') if obj.cas_odoslania else ""

        return {
            'id': obj.id,
            'name': safe_name,
            'search_value': event_str,
            'cat': f"📜 {status_label} • Pokus #{obj.pokus}",
            'rola': f"{vysledok} • 🕒 {cas_str}"
        }

    list_display = ['get_cas_vizual', 'get_endpoint_vizual', 'get_event_vizual', 'get_pokus_vizual', 'get_http_status_vizual', 'get_trvanie_vizual', 'get_uspesne_vizual']
    list_display_links = ['get_event_vizual']
    list_select_related = ['endpoint', 'endpoint__zakaznik']
    isas_date_filter_field = 'cas_odoslania'
    list_filter = ['uspesne', 'pokus', HttpStatusGroupFilter, WebhookEndpointFilter, ISASDateRangeFilter]
    search_fields = ['event_id', 'endpoint__nazov', 'odpoved_servera']
    ordering = ['-cas_odoslania']
    
    readonly_fields = [f.name for f in WebhookLog._meta.fields] + ['get_pretty_payload']
    exclude = ['payload']

    fieldsets = (
        ('📋 Identifikácia doručenia', {
            'fields': ('endpoint', 'event_id', 'pokus', 'cas_odoslania', 'uspesne')
        }),
        ('🌐 HTTP Prenos a Výsledok', {
            'fields': ('http_status', 'trvanie_ms', 'odpoved_servera', 'request_headers', 'response_headers')
        }),
        ('📦 Odoslané dáta (Payload)', {
            'fields': ('get_pretty_payload',),
            'classes': ('collapse',)
        }),
    )

    def perform_fast_action(self, request, action_type, valid_ids, **kwargs):
        if action_type == 'resend_failed':
            if not (request.user.is_superuser or (self.has_view_permission(request) and request.user.has_perm('WebBuilder.change_webhookendpoint'))):
                return JsonResponse({"status": "error", "message": "⛔ Nemáte oprávnenie na opätovné odosielanie webhookov."}, status=403)
            from .tasks import send_webhook_task
            logs = self.model.objects.filter(id__in=valid_ids, uspesne=False).select_related('endpoint')
            odoslane = 0
            preskocene_neaktivne = 0
            chyby_broker = 0
            for log in logs:
                if not log.endpoint or not log.endpoint.je_aktivny:
                    preskocene_neaktivne += 1
                    continue
                try:
                    payload_dict = json.loads(log.payload) if log.payload else {}
                except Exception as e:
                    return JsonResponse({"status": "error", "message": f"Chyba pri spracovaní payloadu logu #{log.id}: {str(e)}"}, status=400)

                try:
                    send_webhook_task.delay(log.endpoint_id, payload_dict, str(log.event_id))
                    odoslane += 1
                except Exception as exc:
                    chyby_broker += 1
                    logger.warning(f"Výpadok Celery/Redis brokera pri opätovnom odoslaní webhook logu #{log.id}: {exc}")
                    messages.warning(request, f"Výpadok brokera: Znovuzaradenie webhook logu #{log.id} zlyhalo.")

            if odoslane > 0:
                msg = f"🔄 Úspešne zaradených {odoslane} webhookov do fronty na opätovné odoslanie."
                if preskocene_neaktivne > 0:
                    msg += f" (⚠️ {preskocene_neaktivne} preskočených kvôli deaktivovanému endpointu)."
                if chyby_broker > 0:
                    msg += f" (⚠️ {chyby_broker} zlyhalo kvôli výpadku brokera)."
                return JsonResponse({"status": "success", "message": msg})
            if chyby_broker > 0:
                return JsonResponse({"status": "error", "message": f"⚠️ Nepodarilo sa zaradiť webhooky do fronty kvôli výpadku Celery/Redis brokera ({chyby_broker} zlyhaní)."}, status=503)
            if preskocene_neaktivne > 0:
                return JsonResponse({"status": "error", "message": f"⚠️ Všetky vybrané logy ({preskocene_neaktivne}) patria pod deaktivované endpointy (Circuit Breaker). Najprv aktivujte endpoint."}, status=400)
            return JsonResponse({"status": "error", "message": "⚠️ Medzi vybranými záznamami nie sú žiadne zlyhané webhooky."}, status=400)

        return super().perform_fast_action(request, action_type, valid_ids, **kwargs)

    @admin.display(description="🚦 Výsledok", ordering="uspesne")
    def get_uspesne_vizual(self, obj):
        if obj.uspesne:
            return render_isas_badge("Doručené", icon="✅", type_color="success", wrapper=False)
        return render_isas_badge("Zlyhanie", icon="❌", type_color="danger", wrapper=False)

    @admin.display(description="🌐 HTTP Status", ordering="http_status")
    def get_http_status_vizual(self, obj):
        if not obj.http_status:
            return render_isas_text("Bez odozvy", muted=True)
        type_color = "success" if 200 <= obj.http_status < 300 else "danger"
        return render_isas_badge(str(obj.http_status), prefix_text="HTTP", type_color=type_color, monospace=True, wrapper=False)

    @admin.display(description="📦 Odoslané dáta (Payload JSON)")
    def get_pretty_payload(self, obj):
        if not obj.payload:
            return render_isas_text("Žiadny payload", muted=True)
        try:
            parsed = json.loads(obj.payload)
            pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
        except Exception:
            pretty = obj.payload
        return render_isas_text(escape(pretty), size=11, color="#0f172a", custom_style="font-family: monospace; white-space: pre-wrap; background: #f8fafc; padding: 12px; border-radius: 8px; border: 1px solid #cbd5e1; display: block; max-height: 400px; overflow-y: auto;")

    @admin.display(description="🕒 Čas odoslania", ordering="cas_odoslania")
    def get_cas_vizual(self, obj):
        if not obj.cas_odoslania:
            return render_isas_text("—", muted=True)
        return render_isas_badge(obj.cas_odoslania.strftime('%d.%m.%Y %H:%M:%S'), icon="🕒", monospace=True, type_color="neutral", wrapper=False)

    @admin.display(description="🔗 Cieľový Endpoint", ordering="endpoint__nazov")
    def get_endpoint_vizual(self, obj):
        if not obj.endpoint_id:
            return render_isas_text("Odstránený endpoint", muted=True)
        klient = ""
        nazov = "Neznámy endpoint"
        try:
            endpoint_obj = obj.endpoint
            if endpoint_obj:
                nazov = endpoint_obj.nazov or endpoint_obj.url or "Endpoint"
                if endpoint_obj.zakaznik_id:
                    klient = getattr(endpoint_obj.zakaznik, 'Nazov_spolocnosti', '')
        except ObjectDoesNotExist:
            return render_isas_text("Odstránený endpoint", muted=True)
        klient_html = render_isas_text(escape(klient), size=10, color="#64748b", weight=700, align="left") if klient else ""
        nazov_html = render_isas_text(escape(nazov), size=12, weight=800, color="#0f172a", align="left")
        return render_isas_layout(format_html('{}{}', nazov_html, klient_html), layout="col", gap=2, center=False)

    @admin.display(description="🆔 Event ID (UUID)", ordering="event_id")
    def get_event_vizual(self, obj):
        event_str = str(obj.event_id)
        short_id = f"#{event_str[:8]}"
        return render_isas_badge(short_id, icon="📋", monospace=True, copy_value=event_str, tooltip="Kliknutím skopírujete celé UUID", wrapper=False)

    @admin.display(description="🔢 Pokus", ordering="pokus")
    def get_pokus_vizual(self, obj):
        pokus_val = obj.pokus or 1
        color = "neutral" if pokus_val == 1 else ("warning" if pokus_val <= 3 else "danger")
        return render_isas_badge(f"#{pokus_val}", icon="🔢", type_color=color, monospace=True, wrapper=False)

    @admin.display(description="⏱️ Odozva", ordering="trvanie_ms")
    def get_trvanie_vizual(self, obj):
        if obj.trvanie_ms is None:
            return render_isas_text("—", muted=True)
        color = "success" if obj.trvanie_ms < 500 else ("warning" if obj.trvanie_ms < 1500 else "danger")
        return render_isas_badge(f"{obj.trvanie_ms} ms", icon="⏱️", monospace=True, type_color=color, wrapper=False)