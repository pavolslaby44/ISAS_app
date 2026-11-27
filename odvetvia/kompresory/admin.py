"""
odvetvia/kompresory/admin.py

⚖️ ISAS INDUSTRIAL COMPLIANCE ADMIN – ODVETVIE: KOMPRESORY A STLAČENÝ VZDUCH
Správa odvetvových lehôt revízií (VTZ, F-plyny, elektro, filtre) a národnej legislatívnej matice.
Plne integrované do ISAS Premium UI Component Factory, Detective Search a Fast Action API.
"""

import unicodedata

from django.apps import apps
from django.contrib import admin
from django.utils.html import escape
from django import forms
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.db.models.fields import NOT_PROVIDED
from django.http import JsonResponse
from django.utils.html import format_html

from ISASaplikacia.admin import ISASPremiumAdmin, ISASSingletonAdminMixin
from ISASaplikacia.utils import (
    render_isas_badge,
    render_isas_header_card,
    render_isas_singleton_card,
    render_isas_singleton_row,
    render_isas_layout,
    render_isas_key_value,
    render_isas_flex_between,
    render_isas_text,
)
from .models import KompresoryNastavenia, KompresoryStatLokalizacia
from .config import COMPLIANCE_ADMIN_MEDIA


# ==============================================================================
# ⚙️ 1. GLOBÁLNE ODVETVOVÉ NASTAVENIA (SINGLETON)
# ==============================================================================

@admin.register(KompresoryNastavenia)
class KompresoryNastaveniaAdmin(ISASSingletonAdminMixin, ISASPremiumAdmin):
    """
    🏛️ Singleton administrácia pre centrálnu konfiguráciu odvetvových revízií.
    Umožňuje servisnému manažérovi nastaviť predpísané periódy bez prístupu k citlivým firemným údajom.
    """
    list_display = (
        'get_core_vizual',
        'get_vtz_lehoty_styled',
        'get_fplyn_lehoty_styled',
        'get_technologie_styled',
    )
    list_display_links = ('get_core_vizual',)

    isas_title = "⚙️ Nastavenia odvetvia: Kompresory a VTZ"
    isas_preloader_text = "⏳ Načítavam odvetvovú konfiguráciu revízií..."
    isas_enable_detective = False  # Singleton má iba 1 riadok, live search je vypnutý
    isas_magia_kwargs = {
        'typ_zobrazenia': 'karta',
        'live_search_endpoint': None,
        'vyzaduje_potvrdenie': False,
    }

    # 🚀 ONBOARDING GUARDIAN: Zaručí, že riadok Singletonu v DB vždy existuje a changelist nikdy nie je prázdny
    def changelist_view(self, request, extra_context=None):
        self.model.get_solo()
        return super().changelist_view(request, extra_context=extra_context)

    # 🛡️ BEZPEČNOSTNÝ ZÁMOK (Tlačidlo v UI ostáva, mazanie je 100% zablokované)
    def delete_model(self, request, obj):
        raise PermissionDenied("❌ Zmazanie odvetvových nastavení je prísne zakázané!")

    def delete_queryset(self, request, queryset):
        raise PermissionDenied("❌ Hromadné mazanie odvetvových nastavení je prísne zakázané!")

    def fast_action_custom_validation(self, request, action_type, valid_ids, **kwargs):
        res = super().fast_action_custom_validation(request, action_type, valid_ids, **kwargs)
        if res:
            return res
        if action_type == 'delete':
            return JsonResponse({
                "status": "error", 
                "message": "❌ Zmazanie odvetvových nastavení je z bezpečnostných dôvodov prísne zakázané!"
            }, status=403)
        return None

    fieldsets = (
        ('🛢️ 1. VYHRADENÉ TLAKOVÉ ZARIADENIA (VTZ) & POISTNÉ VENTILY', {
            'description': '<b>⚖️ Smernica PED 2014/68/EÚ & EN ISO 4126:</b> Zákonné periódy bezpečnostných prehliadok tlakových nádob a preskúšania poistných ventilov kompresorov a vzdušníkov.',
            'fields': (
                ('lehota_vtz_prevadzkova_roky', 'lehota_vtz_vnutorna_roky'),
                ('lehota_vtz_tlakova_roky', 'lehota_vtz_poistny_ventil_roky'),
                'lehota_vtz_manometer_roky',
            )
        }),
        ('❄️ 2. F-PLYNY A CHLADIACE OKRUHY (NARIADENIE EÚ 2024/573)', {
            'description': '<b>🌍 Enviro legislatíva chladív:</b> Zákonné periódy kontrol tesnosti chladiacich sušičiek a chillerov podľa ekvivalentu CO₂ v tonách.',
            'fields': (
                ('lehota_fplyn_5_az_50t_mesiace', 'lehota_fplyn_50_az_500t_mesiace', 'lehota_fplyn_nad_500t_mesiace'),
            )
        }),
        ('⚡ 3. ELEKTROREVÍZIE A BEZPEČNOSŤ (EN NORMY)', {
            'description': '<b>🛡️ Bezpečnostné normy strojov:</b> Odborné prehliadky elektroinštalácie a preventívna obmena vysokotlakových hadíc podľa EN ISO 4414.',
            'fields': (
                ('lehota_vtz_elektro_roky', 'lehota_tlakove_hadice_roky'),
            )
        }),
        ('🌪️ 4. TECHNOLÓGIE ÚPRAVY STLAČENÉHO VZDUCHU', {
            'description': '<b>🧪 Údržba kvality médií:</b> Životnosť adsorpčného sušiva, servisné sady separátorov oleja z vody a kalibrácia analyzátorov plynov.',
            'fields': (
                ('lehota_desikant_roky', 'lehota_odlucovac_filtre_roky'),
                ('lehota_filter_vlozky_roky', 'lehota_kalibracia_o2_roky'),
            )
        }),
    )

    @property
    def media(self):
        """Dynamické pripojenie doložky o zodpovednosti z odvetvovej konfigurácie."""
        base_media = super().media
        if COMPLIANCE_ADMIN_MEDIA and isinstance(COMPLIANCE_ADMIN_MEDIA, dict):
            base_media = base_media + forms.Media(
                css=COMPLIANCE_ADMIN_MEDIA.get('css'),
                js=COMPLIANCE_ADMIN_MEDIA.get('js')
            )
        return base_media

    # 🛡️ DEFENSIVE FORMATTERS: Ochrana pred None a NOT_PROVIDED hodnotami
    def _val_r(self, obj, field_name):
        val = getattr(obj, field_name, None)
        if val is None:
            try:
                def_val = self.model._meta.get_field(field_name).default
                val = def_val if def_val is not NOT_PROVIDED else "—"
            except Exception:
                val = "—"
        return f"{val} r." if val != "—" and val is not None else "—"

    def _val_m(self, obj, field_name):
        val = getattr(obj, field_name, None)
        if val is None:
            try:
                def_val = self.model._meta.get_field(field_name).default
                val = def_val if def_val is not NOT_PROVIDED else "—"
            except Exception:
                val = "—"
        return f"{val} mes." if val != "—" and val is not None else "—"

    # --- 🎨 KARTY DO CHANGELISTU (ISAS COMPONENT FACTORY) ---

    @admin.display(description='⚙️ Odvetvie')
    def get_core_vizual(self, obj):
        badge = render_isas_badge("AKTÍVNE ODVETVIE", "🌀", "info", wrapper=False)
        desc = render_isas_text("Kompresory & VTZ", size=11, color="#0f172a", weight=800)
        obsah = render_isas_layout(format_html('{}{}', badge, desc), layout="col", gap=6)
        return render_isas_header_card("🌀", "KOMPRESORY", obsah)

    @admin.display(description='🛢️ VTZ & Tlakové nádoby')
    def get_vtz_lehoty_styled(self, obj):
        r1 = render_isas_singleton_row(render_isas_layout(format_html('{}{}',
            render_isas_flex_between("Prevádzková revízia:", self._val_r(obj, 'lehota_vtz_prevadzkova_roky')),
            render_isas_flex_between("Vnútorná skúška:", self._val_r(obj, 'lehota_vtz_vnutorna_roky'))
        ), layout="col", gap=4), is_dashed=False)

        r2 = render_isas_singleton_row(render_isas_layout(format_html('{}{}',
            render_isas_flex_between("Tlaková skúška:", self._val_r(obj, 'lehota_vtz_tlakova_roky')),
            render_isas_flex_between("Poistný ventil:", self._val_r(obj, 'lehota_vtz_poistny_ventil_roky'))
        ), layout="col", gap=4))

        r3 = render_isas_singleton_row(
            render_isas_flex_between("Overenie manometra:", self._val_r(obj, 'lehota_vtz_manometer_roky'))
        )
        return render_isas_singleton_card("VTZ & Tlakové nádoby", "🛢️", "#2563eb", format_html('{}{}{}', r1, r2, r3))

    @admin.display(description='❄️ F-plyny & Chladivá')
    def get_fplyn_lehoty_styled(self, obj):
        r1 = render_isas_singleton_row(render_isas_layout(format_html('{}{}{}',
            render_isas_flex_between("5 až 50 t CO₂ eq:", self._val_m(obj, 'lehota_fplyn_5_az_50t_mesiace')),
            render_isas_flex_between("50 až 500 t CO₂ eq:", self._val_m(obj, 'lehota_fplyn_50_az_500t_mesiace')),
            render_isas_flex_between("Nad 500 t CO₂ eq:", self._val_m(obj, 'lehota_fplyn_nad_500t_mesiace'))
        ), layout="col", gap=4), is_dashed=False)

        r2 = render_isas_singleton_row(
            render_isas_flex_between("Elektrorevízia strojov:", self._val_r(obj, 'lehota_vtz_elektro_roky'))
        )
        return render_isas_singleton_card("F-plyny & Chladivá", "❄️", "#0ea5e9", format_html('{}{}', r1, r2))

    @admin.display(description='🌪️ Úprava vzduchu & Komponenty')
    def get_technologie_styled(self, obj):
        r1 = render_isas_singleton_row(render_isas_layout(format_html('{}{}',
            render_isas_flex_between("Výmena sušiva:", self._val_r(obj, 'lehota_desikant_roky')),
            render_isas_flex_between("Tlakové hadice:", self._val_r(obj, 'lehota_tlakove_hadice_roky'))
        ), layout="col", gap=4), is_dashed=False)

        r2 = render_isas_singleton_row(render_isas_layout(format_html('{}{}',
            render_isas_flex_between("Filtre odlučovača:", self._val_r(obj, 'lehota_odlucovac_filtre_roky')),
            render_isas_flex_between("Vložky filtrácie:", self._val_r(obj, 'lehota_filter_vlozky_roky'))
        ), layout="col", gap=4))

        r3 = render_isas_singleton_row(
            render_isas_flex_between("Kalibrácia O₂ senzora:", self._val_r(obj, 'lehota_kalibracia_o2_roky'))
        )
        return render_isas_singleton_card("Komponenty & Filtre", "🌪️", "#10b981", format_html('{}{}{}', r1, r2, r3))


# ==============================================================================
# 🗺️ 2. NÁRODNÉ LEHOTY VTZ (MULTI-COUNTRY MATICA)
# ==============================================================================

@admin.register(KompresoryStatLokalizacia)
class KompresoryStatLokalizaciaAdmin(ISASPremiumAdmin):
    """
    🗺️ Administrácia špecifických národných lehôt VTZ pre jednotlivé štáty (napr. SK, CZ, DE, AT).
    Umožňuje nadradiť globálne odvetvové lehoty predpismi danej krajiny.
    """
    list_display = (
        'get_stat_badge',
        'get_vtz_nadoba_styled',
        'get_bezpecnost_styled',
    )
    list_display_links = ('get_stat_badge',)
    list_filter = ('stat__je_clen_eu',)
    autocomplete_fields = ['stat']
    search_fields = ['stat__nazov', 'stat__iso_kod', 'stat__aliasy']
    ordering = ['stat__iso_kod']

    isas_title = "🗺️ Národné lehoty VTZ pre tlakové zariadenia"
    isas_preloader_text = "⏳ Načítavam národnú legislatívnu maticu..."
    isas_delete_success_message = "✅ Úspešne odstránené pravidlo národných lehôt!"
    isas_autocomplete_fk_fields = {'stat': '🗺️ Vyberte štát (krajinu pôsobnosti)'}
    isas_magia_kwargs = {
        'search_placeholder': "🔍 Vyhľadávanie...",
    }

    # 🚀 ZERO N+1 OPTIMALIZÁCIA: Okamžité spojenie tabuliek jedným SQL dopytom
    def get_queryset(self, request):
        return super().get_queryset(request).select_related('stat')

    # 🛡️ INTEGRITY GUARD: Vylúčenie už priradených štátov pri pridávaní (100% ochrana pred duplicitou)
    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "stat" and request and request.path.rstrip('/').endswith('/add'):
            StatLokalizaciaModel = apps.get_model('ISASaplikacia', 'StatLokalizacia')
            kwargs["queryset"] = StatLokalizaciaModel.objects.exclude(kompresory_lehoty__isnull=False)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    # 🛡️ OCHRANA SÍDLA FIRMY: Zákaz vymazania pravidla pre štát, v ktorom sídli materská spoločnosť
    def _over_ochranu_sidla(self, obj):
        try:
            FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
            aktivne_iso = FiremnaIdentitaModel.get_aktivna_krajina_iso() if hasattr(FiremnaIdentitaModel, 'get_aktivna_krajina_iso') else None
            if aktivne_iso and aktivne_iso not in ('NEZADANE', 'INYKRAJINA') and obj.stat and obj.stat.iso_kod:
                if str(obj.stat.iso_kod).strip().upper() == str(aktivne_iso).strip().upper():
                    return f"🛑 Štát '{obj.stat.iso_kod}' je sídlom materskej firmy. Národné odvetvové lehoty sídla je zakázané vymazať!"
        except Exception:
            pass
        return None

    def has_delete_permission(self, request, obj=None):
        if obj and self._over_ochranu_sidla(obj):
            return False
        return super().has_delete_permission(request, obj)

    def delete_model(self, request, obj):
        dovod = self._over_ochranu_sidla(obj)
        if dovod:
            raise PermissionDenied(dovod)
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            dovod = self._over_ochranu_sidla(obj)
            if dovod:
                raise PermissionDenied(dovod)
        super().delete_queryset(request, queryset)

    def fast_action_custom_validation(self, request, action_type, valid_ids, **kwargs):
        res = super().fast_action_custom_validation(request, action_type, valid_ids, **kwargs)
        if res:
            return res
        if action_type == 'delete' and valid_ids:
            for obj in self.get_queryset(request).filter(pk__in=valid_ids):
                dovod = self._over_ochranu_sidla(obj)
                if dovod:
                    return JsonResponse({"status": "error", "message": dovod}, status=403)
        return None

    # 🕵️ ISAS DETECTIVE LIVE SEARCH: 100% diakriticky a typovo odolné vyhľadávanie
    def get_detective_queryset(self, request, term):
        if not term or not str(term).strip():
            return self.get_queryset(request).none()

        term_str = str(term).strip()
        term_clean = unicodedata.normalize('NFKD', term_str).encode('ascii', 'ignore').decode('utf-8').strip().lower()

        filtre = (
            Q(stat__nazov__icontains=term_str) |
            Q(stat__iso_kod__iexact=term_str) |
            Q(stat__iso_kod__icontains=term_str) |
            Q(stat__aliasy__icontains=term_str)
        )
        if term_clean:
            filtre |= Q(stat__aliasy__icontains=term_clean)

        term_num = term_str.rstrip('rmd. ').strip()
        if term_num.isdigit() and len(term_num) <= 5:
            cislo = int(term_num)
            filtre |= (
                Q(lehota_vtz_prevadzkova_roky=cislo) |
                Q(lehota_vtz_vnutorna_roky=cislo) |
                Q(lehota_vtz_tlakova_roky=cislo) |
                Q(lehota_vtz_poistny_ventil_roky=cislo) |
                Q(lehota_vtz_elektro_roky=cislo) |
                Q(lehota_vtz_manometer_roky=cislo)
            )

        qs = self.get_queryset(request).filter(filtre)
        if term_clean and not qs.exists():
            qs = self.get_queryset(request).filter(
                Q(stat__nazov__icontains=term_clean) |
                Q(stat__aliasy__icontains=term_clean)
            )
        return qs.order_by('stat__nazov', 'pk')[:10]

    def format_detective_result(self, obj):
        iso = getattr(obj.stat, 'iso_kod', '??') if obj.stat else '??'
        nazov = getattr(obj.stat, 'nazov', str(obj.stat)) if obj.stat else 'Neznámy štát'

        # 🛡️ DYNAMICKÝ REKAPITULAČNÝ RIADOK VŠETKÝCH NÁRODNÝCH LEHÔT (VTZ + BEZPEČNOSŤ)
        prev = f"{obj.lehota_vtz_prevadzkova_roky}r" if obj.lehota_vtz_prevadzkova_roky is not None else "Globál"
        vnut = f"{obj.lehota_vtz_vnutorna_roky}r" if obj.lehota_vtz_vnutorna_roky is not None else "Globál"
        tlak = f"{obj.lehota_vtz_tlakova_roky}r" if obj.lehota_vtz_tlakova_roky is not None else "Globál"
        pv = f"{obj.lehota_vtz_poistny_ventil_roky}r" if obj.lehota_vtz_poistny_ventil_roky is not None else "Globál"
        el = f"{obj.lehota_vtz_elektro_roky}r" if obj.lehota_vtz_elektro_roky is not None else "Globál"
        man = f"{obj.lehota_vtz_manometer_roky}r" if obj.lehota_vtz_manometer_roky is not None else "Globál"

        detail_lehoty = f"Prev:{prev} Vnútr:{vnut} Tlak:{tlak} | PV:{pv} El:{el} Man:{man}"

        return {
            'id': obj.id,
            'name': f"{iso} - {nazov}",
            'search_value': iso if iso and iso != '??' else nazov,
            'rola': f"🛢️ {detail_lehoty}",
            'cat': '🗺️ LEHOTY VTZ',
        }

    fieldsets = (
        ('🗺️ ŠTÁT A PÔSOBNOSŤ', {
            'description': 'Zvoľte štát, pre ktorý definujete špecifické zákonné lehoty revízií.',
            'fields': ('stat',)
        }),
        ('🛢️ ZÁKONNÉ LEHOTY VTZ DANÉHO ŠTÁTU', {
            'description': 'Lehoty prehliadok tlakových nádob v rokoch (ak pole necháte prázdne, použije sa globálna lehota odvetvia).',
            'fields': (
                ('lehota_vtz_prevadzkova_roky', 'lehota_vtz_vnutorna_roky', 'lehota_vtz_tlakova_roky'),
            )
        }),
        ('🛡️ BEZPEČNOSŤ, POISTNÉ VENTILY A METROLÓGIA', {
            'description': 'Predpísané lehoty skúšok poistných ventilov, elektroinštalácií a overovania manometrov.',
            'fields': (
                ('lehota_vtz_poistny_ventil_roky', 'lehota_vtz_elektro_roky', 'lehota_vtz_manometer_roky'),
            )
        }),
    )

    @admin.display(description='🗺️ Štát', ordering='stat__iso_kod')
    def get_stat_badge(self, obj):
        if not obj.stat:
            return render_isas_badge("Nezadaný štát", icon="❓", type_color="danger")
        iso = getattr(obj.stat, 'iso_kod', '??')
        nazov = getattr(obj.stat, 'nazov', str(obj.stat))
        return render_isas_badge(f"{iso} - {nazov}", icon="🗺️", type_color="info")

    @admin.display(description='🛢️ Lehoty nádob (Prev / Vnútr / Tlak)')
    def get_vtz_nadoba_styled(self, obj):
        has_vtz = any(getattr(obj, f, None) is not None for f in [
            'lehota_vtz_prevadzkova_roky', 'lehota_vtz_vnutorna_roky', 'lehota_vtz_tlakova_roky'
        ])
        if not has_vtz:
            badge = render_isas_badge("Globálne lehoty", icon="⚙️", type_color="neutral", wrapper=False)
            popis = render_isas_text("Podľa odvetvia", size=10, color="#94a3b8", muted=True)
            return render_isas_layout(format_html('{}{}', badge, popis), layout="col", center=True, gap=2)

        prev = f"{obj.lehota_vtz_prevadzkova_roky} r." if obj.lehota_vtz_prevadzkova_roky is not None else "—"
        vnut = f"{obj.lehota_vtz_vnutorna_roky} r." if obj.lehota_vtz_vnutorna_roky is not None else "—"
        tlak = f"{obj.lehota_vtz_tlakova_roky} r." if obj.lehota_vtz_tlakova_roky is not None else "—"
        txt = format_html(
            '<span style="font-weight:800;color:#1e40af;">{}</span> / '
            '<span style="font-weight:800;color:#0284c7;">{}</span> / '
            '<span style="font-weight:800;color:#b91c1c;">{}</span>',
            prev, vnut, tlak
        )
        return render_isas_text(txt, size=11, align="center")

    @admin.display(description='🛡️ Bezpečnosť (PV / Elektro / Manometer)')
    def get_bezpecnost_styled(self, obj):
        has_sec = any(getattr(obj, f, None) is not None for f in [
            'lehota_vtz_poistny_ventil_roky', 'lehota_vtz_elektro_roky', 'lehota_vtz_manometer_roky'
        ])
        if not has_sec:
            badge = render_isas_badge("Globálne lehoty", icon="⚙️", type_color="neutral", wrapper=False)
            popis = render_isas_text("Podľa odvetvia", size=10, color="#94a3b8", muted=True)
            return render_isas_layout(format_html('{}{}', badge, popis), layout="col", center=True, gap=2)

        pv = f"{obj.lehota_vtz_poistny_ventil_roky} r." if obj.lehota_vtz_poistny_ventil_roky is not None else "—"
        el = f"{obj.lehota_vtz_elektro_roky} r." if obj.lehota_vtz_elektro_roky is not None else "—"
        man = f"{obj.lehota_vtz_manometer_roky} r." if obj.lehota_vtz_manometer_roky is not None else "—"
        txt = format_html(
            '<span style="font-weight:800;color:#15803d;">🛡️ {}</span> | '
            '<span style="font-weight:800;color:#b45309;">⚡ {}</span> | '
            '<span style="font-weight:800;color:#475569;">⏱️ {}</span>',
            pv, el, man
        )
        return render_isas_text(txt, size=11, align="center")